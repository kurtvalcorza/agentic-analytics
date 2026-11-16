"""Quota-limited output mount for managed Docker execution (SEC-01, issue #22).

Each run gets a fresh, size-limited tmpfs at ``/workspace/outputs``. It is the only writable
place under ``/workspace``, which is otherwise mounted read-only. The kernel enforces the
quota, so a writer hits ``ENOSPC`` at the limit instead of filling the host disk. tmpfs pages
are also charged to the container's memory cgroup, so the quota is validated against
``docker_memory``.

The tmpfs lives only as long as its container, so outputs are copied out while the container
is still alive:

1. The container's main process (PID 1) is a small supervisor (``SUPERVISOR_SOURCE``, mounted
   read-only). It first makes itself non-dumpable, so its ``/proc`` entries (memory, fds,
   environment) belong to root and the managed code cannot read them. It then reads a per-run
   key from its stdin. Stdin is never inherited by the managed code, and the key is never put
   in argv or the environment.
2. The supervisor runs the managed script as a child in its own session with its own deadline.
   While the script runs it watches ``outputs/`` with inotify. On every change it samples free
   space and inodes, and it holds a read-only descriptor on every file created there. Deleting
   a file therefore does not return its space during the run; this matches the old rule that
   deletions never offset new output. A 5 ms timer samples as a fallback.
3. When the script exits (or the deadline passes, or stdin reaches EOF because the server
   died), the supervisor SIGKILLs every other process in the container and re-scans ``/proc``
   until none is left. Only then does it write an HMAC-authenticated verdict. A run counts as
   having exhausted its quota when free space ever fell to ``quota_threshold`` or free inodes
   reached zero, whatever the script's exit code.
4. The server polls for the verdict with ``docker exec`` under its own deadline. Unless the
   run failed, it then streams the outputs out with ``docker exec tar``. The tar stream is
   capped, and :func:`extract_outputs` validates every member (count, size, type, and both
   POSIX and Windows path rules) before writing anything. Writes do not follow symlinks and
   never go through hardlinks. On any error, everything already written is removed.
5. The server closes the supervisor's stdin. ``--rm``, and ``docker rm -f`` on every path,
   then remove the container together with its tmpfs.

The quota limit itself is a hard kernel guarantee. Detecting that a script hit the limit is
not sound in every case. A script can fill the quota, catch ``ENOSPC``, *truncate* the file
(which frees pages even while a descriptor is held) and exit 0 between two samples, and that
is not detected. The host is still protected, because the kernel limit holds.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import ntpath
import os
import re
import stat
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO

OUTPUT_DIR_NAME = "outputs"
CONTAINER_OUTPUT_DIR = "/workspace/outputs"
SUPERVISOR_PATH = "/run/agentic-analytics/supervisor.py"
VERDICT_PATH = "/tmp/.agentic-analytics-verdict.json"
VERDICT_MAX_BYTES = 65536
# A tar stream carries a 512-byte header per entry plus block padding, so its cap is looser
# than the quota on extracted bytes, which extract_outputs enforces exactly.
TAR_OVERHEAD_FACTOR = 2
TAR_OVERHEAD_BYTES = 1024 * 1024
DEFAULT_MAX_MEMBERS = 10_000

_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.IGNORECASE
)
_WINDOWS_FORBIDDEN = set('<>:"|?*\\') | {chr(code) for code in range(32)}


def quota_threshold(quota_bytes: int) -> int:
    """Free bytes at or below which the run is judged to have exhausted its quota."""

    return max(64 * 1024, quota_bytes // 100)


SUPERVISOR_SOURCE = r'''
import contextlib, ctypes, hashlib, hmac, json, os, resource, select, signal, struct
import subprocess, sys, threading, time

libc = ctypes.CDLL(None, use_errno=True)
libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: /proc/1/{mem,fd,environ} become root-only
key = sys.stdin.buffer.readline().strip()
threshold = int(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
OUT = "/workspace/outputs"
VERDICT = "/tmp/.agentic-analytics-verdict.json"
PIN_LIMIT = 20000
with contextlib.suppress(ValueError, OSError):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, min(hard, PIN_LIMIT + 256)), hard))

low = {"free": None, "inodes": None}
pinned = []
watches = {}
stdin_closed = threading.Event()
stop = threading.Event()
IN_MODIFY, IN_MOVED_TO, IN_CREATE = 0x2, 0x80, 0x100
ifd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)


def watch_stdin():
    sys.stdin.buffer.read()
    stdin_closed.set()


def sample():
    st = os.statvfs(OUT)
    free = st.f_bavail * st.f_frsize
    low["free"] = free if low["free"] is None else min(low["free"], free)
    if st.f_files:
        low["inodes"] = st.f_favail if low["inodes"] is None else min(low["inodes"], st.f_favail)


def track(path):
    try:
        mode = os.lstat(path).st_mode & 0o170000
    except OSError:
        return
    if mode == 0o040000:
        watch_dir(path)
    elif mode == 0o100000 and len(pinned) < PIN_LIMIT:
        with contextlib.suppress(OSError):
            pinned.append(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC))


def watch_dir(path):
    if ifd >= 0:
        wd = libc.inotify_add_watch(ifd, path.encode(), IN_MODIFY | IN_CREATE | IN_MOVED_TO)
        if wd >= 0:
            watches[wd] = path
    with contextlib.suppress(OSError):
        for entry in os.scandir(path):
            track(entry.path)


def monitor():
    while not stop.is_set():
        if ifd >= 0:
            ready = select.select([ifd], [], [], 0.005)[0]
        else:
            time.sleep(0.005)
            ready = []
        sample()
        if not ready:
            continue
        try:
            data = os.read(ifd, 65536)
        except BlockingIOError:
            continue
        offset = 0
        while offset + 16 <= len(data):
            wd, mask, _cookie, size = struct.unpack_from("iIII", data, offset)
            name = data[offset + 16 : offset + 16 + size].rstrip(b"\0").decode(errors="replace")
            offset += 16 + size
            parent = watches.get(wd)
            if parent and name and mask & (IN_CREATE | IN_MOVED_TO):
                track(os.path.join(parent, name))
        sample()


def others():
    me = os.getpid()
    alive = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open("/proc/" + entry + "/stat") as handle:
                state = handle.read().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        if state not in ("Z", "X"):
            alive.append(int(entry))
    return alive


def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def kill_others():
    for _ in range(500):
        reap()
        alive = others()
        if not alive:
            return True
        for pid in alive:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.01)
    return False


threading.Thread(target=watch_stdin, daemon=True).start()
sample()
watch_dir(OUT)
child = subprocess.Popen(
    [sys.executable, "/run/agentic-analytics/request.py"],
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
monitor_thread = threading.Thread(target=monitor, daemon=True)
monitor_thread.start()
timed_out = False
while child.poll() is None:
    if time.monotonic() >= deadline or stdin_closed.is_set():
        timed_out = True
        break
    time.sleep(0.02)
clean = kill_others()  # every process except this one, before anything is judged
code = child.poll()
stop.set()
monitor_thread.join()
sample()
exhausted = low["free"] <= threshold or low["inodes"] == 0
body = json.dumps(
    {
        "exit_code": code,
        "timed_out": timed_out,
        "quota_exhausted": exhausted,
        "min_free_bytes": low["free"],
        "others_remaining": not clean,
    },
    sort_keys=True,
)
mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
staging = VERDICT + "." + str(os.getpid())
fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
os.write(fd, json.dumps({"body": body, "mac": mac}).encode())
os.close(fd)
os.replace(staging, VERDICT)  # atomic: the server never reads a half-written verdict
stdin_closed.wait()
'''


class OutputArchiveError(RuntimeError):
    """The outputs archive was oversized or unsafe; nothing from it was kept."""


@dataclass(frozen=True, slots=True)
class Verdict:
    exit_code: int | None
    timed_out: bool
    quota_exhausted: bool
    min_free_bytes: int | None
    others_remaining: bool


def parse_verdict(raw: str, key: str) -> Verdict | None:
    """Return the supervisor's verdict, or None when it is missing, malformed, or forged."""

    try:
        envelope = json.loads(raw)
        body = str(envelope["body"])
        expected = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(envelope["mac"])):
            return None
        data = json.loads(body)
        return Verdict(
            exit_code=None if data["exit_code"] is None else int(data["exit_code"]),
            timed_out=bool(data["timed_out"]),
            quota_exhausted=bool(data["quota_exhausted"]),
            min_free_bytes=None if data["min_free_bytes"] is None else int(data["min_free_bytes"]),
            others_remaining=bool(data["others_remaining"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def read_capped(stream: IO[bytes], sink: IO[bytes], cap: int) -> bool:
    """Copy ``stream`` into ``sink``; return False (and stop) past ``cap`` bytes."""

    total = 0
    for chunk in iter(lambda: stream.read(65536), b""):
        total += len(chunk)
        if total > cap:
            return False
        sink.write(chunk)
    return True


def member_parts(name: str) -> tuple[str, ...]:
    """Validate one archive member name under POSIX *and* Windows rules; return its parts.

    The archive comes from untrusted code and the host may be Windows, so a name is accepted
    only if it is a plain relative path on both: no backslashes, colons or drive letters, UNC
    or device prefixes, reserved device names, trailing dots or spaces, control characters,
    ``..`` components, or absolute forms.
    """

    if not name or "\x00" in name:
        raise OutputArchiveError(f"invalid output path: {name!r}")
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise OutputArchiveError(f"output path is absolute: {name!r}")
    if ntpath.splitdrive(name)[0] or name.startswith(("//", "\\\\")):
        raise OutputArchiveError(f"output path has a drive or UNC prefix: {name!r}")
    if ".." in posix.parts or ".." in windows.parts:
        raise OutputArchiveError(f"output path escapes outputs/: {name!r}")
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    for part in parts:
        if _WINDOWS_FORBIDDEN.intersection(part):
            raise OutputArchiveError(f"output path has a character Windows forbids: {name!r}")
        if part.endswith((".", " ")):
            raise OutputArchiveError(f"output path ends with a dot or space: {name!r}")
        if _WINDOWS_RESERVED.match(part.rstrip(". ")):
            raise OutputArchiveError(f"output path uses a reserved device name: {name!r}")
    return parts


def _contained(root: Path, target: Path) -> bool:
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(target)
    try:
        return os.path.commonpath([real_root, real_target]) == real_root
    except ValueError:  # different drives on Windows
        return False


def _members(
    archive: tarfile.TarFile, quota_bytes: int, max_members: int
) -> list[tuple[tarfile.TarInfo, tuple[str, ...]]]:
    accepted: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    kinds: dict[tuple[str, ...], bool] = {}  # parts -> is_dir
    total = 0
    count = 0
    for member in archive:  # streamed: never materialise more than max_members headers
        count += 1
        if count > max_members + 1:  # + 1 for the archive's own "." entry
            raise OutputArchiveError(f"outputs contain more than {max_members} entries")
        if not member.isdir() and not member.isreg():
            raise OutputArchiveError(
                f"outputs may contain only regular files and directories: {member.name!r}"
            )
        if member.size < 0 or (member.isdir() and member.size != 0):
            raise OutputArchiveError(f"invalid size in archive header: {member.name!r}")
        parts = member_parts(member.name)
        if not parts:
            continue
        for depth in range(1, len(parts)):
            if kinds.get(parts[:depth]) is False:
                raise OutputArchiveError(f"output path conflicts with a file: {member.name!r}")
            kinds.setdefault(parts[:depth], True)
        previous = kinds.get(parts)
        if previous is not None and (previous != member.isdir() or previous is False):
            raise OutputArchiveError(f"duplicate or conflicting output path: {member.name!r}")
        kinds[parts] = member.isdir()
        total += member.size if member.isreg() else 0
        if total > quota_bytes:
            raise OutputArchiveError("outputs archive exceeds the output quota")
        accepted.append((member, parts))
    return accepted


def _check_destination(
    destination: Path, parts: tuple[str, ...], is_dir: bool, protected_ids: set[tuple[int, int]]
) -> None:
    current = destination
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        last = index == len(parts) - 1
        reparse: int = getattr(info, "st_reparse_tag", 0)  # Windows junctions
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise OutputArchiveError(f"refusing to write through a link: {current}")
        if not last or is_dir:
            if not stat.S_ISDIR(info.st_mode):
                raise OutputArchiveError(f"output path conflicts with an existing file: {current}")
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OutputArchiveError(f"output path conflicts with an existing entry: {current}")
        if info.st_nlink > 1 or (info.st_dev, info.st_ino) in protected_ids:
            raise OutputArchiveError(f"refusing to overwrite a linked or protected file: {current}")


def _open_parent_posix(destination: Path, parts: tuple[str, ...], created: list[Path]) -> int:
    """Walk to the parent directory without following symlinks; return its fd."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(destination, flags)
    try:
        current = destination
        for part in parts[:-1]:
            current = current / part
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
                created.append(current)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_file(
    destination: Path,
    parts: tuple[str, ...],
    source: IO[bytes],
    protected_ids: set[tuple[int, int]],
    created: list[Path],
) -> None:
    target = destination.joinpath(*parts)
    if os.name == "posix":
        parent = _open_parent_posix(destination, parts, created)
        try:
            try:
                os.lstat(parts[-1], dir_fd=parent)
                existed = True
            except FileNotFoundError:
                existed = False
            fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o644,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
    else:  # pragma: no cover - exercised on Windows hosts only
        for depth in range(1, len(parts)):
            directory = destination.joinpath(*parts[:depth])
            if not directory.exists():
                directory.mkdir()
                created.append(directory)
        if not _contained(destination, target):
            raise OutputArchiveError(f"output path escapes outputs/: {'/'.join(parts)}")
        existed = target.exists()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    if not existed:
        created.append(target)
    with os.fdopen(fd, "wb") as handle:
        info = os.fstat(handle.fileno())
        # Re-checked on the open descriptor, so a swap after validation cannot redirect it.
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink > 1
            or (info.st_dev, info.st_ino) in protected_ids
        ):
            raise OutputArchiveError(f"refusing to overwrite a linked or protected file: {target}")
        handle.truncate(0)
        while chunk := source.read(65536):
            handle.write(chunk)


def _remove(created: Iterable[Path]) -> None:
    for path in reversed(list(created)):
        with contextlib.suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()


def extract_outputs(
    archive_file: IO[bytes],
    destination: Path,
    quota_bytes: int,
    protected: Iterable[tuple[int, int]] = (),
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> list[Path]:
    """Validate the whole archive, then write it under ``destination``.

    Returns every file and directory this call *created* (pre-existing files it overwrote are
    not included), so a caller can remove them if registration later fails. ``protected``
    holds ``(st_dev, st_ino)`` identities (registered read-only sources) that are never
    overwritten. On any error everything created here is removed and
    :class:`OutputArchiveError` is raised.
    """

    destination = destination.resolve(strict=True)
    protected_ids = set(protected)
    created: list[Path] = []
    try:
        with tarfile.open(fileobj=archive_file, mode="r:") as archive:
            members = _members(archive, quota_bytes, max_members)
            for member, parts in members:
                if not _contained(destination, destination.joinpath(*parts)):
                    raise OutputArchiveError(f"output path escapes outputs/: {member.name!r}")
                _check_destination(destination, parts, member.isdir(), protected_ids)
            for member, parts in members:
                if member.isdir():
                    for depth in range(1, len(parts) + 1):
                        directory = destination.joinpath(*parts[:depth])
                        if not directory.exists():
                            directory.mkdir()
                            created.append(directory)
                    continue
                source = archive.extractfile(member)
                if source is None:  # pragma: no cover - regular members always have data
                    continue
                with source:
                    _write_file(destination, parts, source, protected_ids, created)
    except OutputArchiveError:
        _remove(created)
        raise
    except (tarfile.TarError, OSError, ValueError) as exc:
        _remove(created)
        raise OutputArchiveError(f"could not extract outputs: {exc}") from exc
    return created


def remove_outputs(paths: Iterable[str]) -> None:
    """Best-effort removal of copied-out outputs whose registration failed."""

    _remove(Path(path) for path in paths)
