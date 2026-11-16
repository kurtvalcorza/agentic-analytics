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
   died), the supervisor sends SIGKILL to every other process in the container with
   ``kill(-1)`` and repeats until ``/proc`` lists no process but itself (zombie entries
   count as alive; if that does not happen within 10 s the run fails). Only then does it
   write an HMAC-authenticated verdict. A run counts as
   having exhausted its quota when free space ever fell to ``quota_threshold`` or free inodes
   reached zero, whatever the script's exit code.
4. The server polls for the verdict with ``docker exec`` under its own deadline. Unless the
   run failed, it then streams the outputs out with ``docker exec tar``. The tar stream is
   capped, and :func:`extract_outputs` validates every member (count, size, type, and both
   POSIX and Windows path rules, names that differ only by case) before writing anything.
   The files are staged in a private directory and renamed into place only when all of them
   have been written; renames do not follow symlinks and never write through hardlinks. On
   any error the outputs already moved are removed and the files they replaced are restored.
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
import shutil
import stat
import tarfile
import tempfile
import unicodedata
import uuid
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
# Outputs are staged under this name inside outputs/ and moved into place only when the whole
# archive has been written. Archive members may not use the prefix.
STAGING_PREFIX = ".agentic-analytics-staging-"
# Per-run directory (next to the request script) that keeps the earlier versions of outputs a
# run replaced, so they can be put back if artifact registration fails.
RESTORE_DIR_NAME = "outputs-restore"
RESTORE_MANIFEST = "manifest.json"

_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.IGNORECASE
)
_WINDOWS_FORBIDDEN = set('<>:"|?*\\') | {chr(code) for code in range(32)}


def quota_threshold(quota_bytes: int) -> int:
    """Free bytes at or below which the run is judged to have exhausted its quota."""

    return max(64 * 1024, quota_bytes // 100)


SUPERVISOR_SOURCE = r"""
import contextlib, ctypes, hashlib, hmac, json, os, resource, select, signal, stat, struct
import subprocess, sys, threading, time

libc = ctypes.CDLL(None, use_errno=True)
libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: /proc/1/{mem,fd,environ} become root-only
key = sys.stdin.buffer.readline().strip()
threshold = int(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
OUT = "/workspace/outputs"
VERDICT = "/tmp/.agentic-analytics-verdict.json"
PIN_LIMIT = 20000
FD_RESERVE = 64  # descriptors never used for pinning: /proc scans and the verdict need some
KILL_SECONDS = 10.0
pin_cap = 0

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
    elif mode == 0o100000 and len(pinned) < pin_cap:
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
    # Every other /proc entry counts as alive, zombies included. A thread-group leader that
    # called SYS_exit shows state Z while its other threads keep running, so state proves
    # nothing; a real zombie disappears once it is reaped.
    me = os.getpid()
    return [int(entry) for entry in os.listdir("/proc") if entry.isdigit() and int(entry) != me]


def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def kill_others():
    # kill(-1) from PID 1 signals every process in the PID namespace in one call, so a fork
    # cannot slip between a scan and a signal. Orphans are reparented to this process and
    # reaped here. The loop ends only when /proc lists nothing but this process.
    end = time.monotonic() + KILL_SECONDS
    while True:
        with contextlib.suppress(OSError):
            os.kill(-1, signal.SIGKILL)
        reap()
        try:
            if not others():
                return True
        except OSError:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(0.005)


def raise_pin_cap():
    # Called after the script has started, so the script keeps the original soft limit.
    global pin_cap
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    unlimited = resource.RLIM_INFINITY
    if soft != unlimited:
        want = PIN_LIMIT + 256 if hard == unlimited else min(hard, PIN_LIMIT + 256)
        if want > soft:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
                soft = want
    pin_cap = PIN_LIMIT if soft == unlimited else max(0, min(PIN_LIMIT, soft - FD_RESERVE))


def write_verdict(payload):
    # /tmp is writable by the script, which may have left anything at these names. Every
    # other process is dead by now, so after the unlink nothing can recreate the staging
    # name; O_EXCL, O_NONBLOCK and the fstat make a FIFO, link or directory fail at once.
    staging = VERDICT + "." + str(os.getpid())
    with contextlib.suppress(FileNotFoundError):
        os.unlink(staging)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(staging, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("verdict staging file is not a regular file")
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(staging, VERDICT)  # atomic: the server never reads a half-written verdict


threading.Thread(target=watch_stdin, daemon=True).start()
sample()
watch_dir(OUT)
child = subprocess.Popen(
    [sys.executable, "/run/agentic-analytics/request.py"],
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
with contextlib.suppress(ValueError, OSError):
    raise_pin_cap()
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
try:
    write_verdict(json.dumps({"body": body, "mac": mac}).encode())
except OSError:
    os._exit(3)  # no verdict: the server fails the run closed as soon as the container is gone
stdin_closed.wait()
"""


class OutputArchiveError(RuntimeError):
    """The outputs archive was oversized or unsafe, or could not be written.

    Nothing from it is kept, and outputs that already existed are left as they were.
    """


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


def _fold(part: str) -> str:
    """Key under which names collide on case-insensitive or normalising filesystems."""

    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", part).casefold())


def _members(
    archive: Iterable[tarfile.TarInfo], quota_bytes: int, max_members: int
) -> list[tuple[tarfile.TarInfo, tuple[str, ...]]]:
    accepted: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    kinds: dict[tuple[str, ...], bool] = {}  # folded parts -> is_dir
    spelling: dict[tuple[str, ...], tuple[str, ...]] = {}  # folded parts -> parts as first seen
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
        if parts[0].startswith(STAGING_PREFIX):
            raise OutputArchiveError(f"output path uses a reserved name: {member.name!r}")
        folded = tuple(_fold(part) for part in parts)
        for depth in range(1, len(parts) + 1):
            # Refused on every host, so a run's outcome does not depend on the host's
            # filesystem: on Windows and macOS these names would be one entry.
            if spelling.setdefault(folded[:depth], parts[:depth]) != parts[:depth]:
                raise OutputArchiveError(
                    f"output paths differ only by case or Unicode form: {member.name!r}"
                )
        for depth in range(1, len(parts)):
            if kinds.get(folded[:depth]) is False:
                raise OutputArchiveError(f"output path conflicts with a file: {member.name!r}")
            kinds.setdefault(folded[:depth], True)
        previous = kinds.get(folded)
        if previous is not None and (previous != member.isdir() or previous is False):
            raise OutputArchiveError(f"duplicate or conflicting output path: {member.name!r}")
        kinds[folded] = member.isdir()
        total += member.size if member.isreg() else 0
        if total > quota_bytes:
            raise OutputArchiveError("outputs archive exceeds the output quota")
        accepted.append((member, parts))
    return accepted


def _refuse_unsafe_file(
    info: os.stat_result, protected_ids: set[tuple[int, int]], target: Path
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OutputArchiveError(f"output path conflicts with an existing entry: {target}")
    if info.st_nlink > 1 or (info.st_dev, info.st_ino) in protected_ids:
        raise OutputArchiveError(f"refusing to overwrite a linked or protected file: {target}")


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
        _refuse_unsafe_file(info, protected_ids, current)


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


def _keep_earlier(source: IO[bytes], info: os.stat_result, backup: Path) -> None:
    with open(backup, "xb") as sink:
        shutil.copyfileobj(source, sink, 65536)
    os.utime(backup, ns=(info.st_atime_ns, info.st_mtime_ns))


def _move_in(
    destination: Path,
    parts: tuple[str, ...],
    staged: Path,
    protected_ids: set[tuple[int, int]],
    created: list[Path],
    replaced: list[tuple[Path, Path]],
    backups: Path,
) -> None:
    """Rename one fully written staged file into place.

    A file already at the target is first copied to ``backups`` and recorded in ``replaced``.
    The rename replaces the directory entry, so nothing is ever written through an existing
    inode (a hardlink or a registered source).
    """

    target = destination.joinpath(*parts)
    backup = backups / str(len(replaced))
    if os.name == "posix":
        parent = _open_parent_posix(destination, parts, created)
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            try:
                fd = os.open(parts[-1], flags, dir_fd=parent)
            except FileNotFoundError:
                existed = False
            else:
                existed = True
                with os.fdopen(fd, "rb") as handle:
                    # Checked on the open descriptor, so a swap after validation changes nothing.
                    info = os.fstat(handle.fileno())
                    _refuse_unsafe_file(info, protected_ids, target)
                    _keep_earlier(handle, info, backup)
                replaced.append((target, backup))
            os.replace(staged, parts[-1], dst_dir_fd=parent)
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
        try:
            info = target.lstat()
        except FileNotFoundError:
            existed = False
        else:
            existed = True
            if getattr(info, "st_reparse_tag", 0):
                raise OutputArchiveError(f"refusing to write through a link: {target}")
            _refuse_unsafe_file(info, protected_ids, target)
            with open(target, "rb") as handle:
                _keep_earlier(handle, info, backup)
            replaced.append((target, backup))
        os.replace(staged, target)
    if not existed:
        created.append(target)


def _remove(created: Iterable[Path]) -> None:
    for path in reversed(list(created)):
        with contextlib.suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()


def _restore(replaced: Iterable[tuple[Path, Path]]) -> None:
    """Put earlier versions back over the outputs that replaced them (best effort)."""

    for target, backup in reversed(list(replaced)):
        with contextlib.suppress(OSError):
            try:
                os.replace(backup, target)
                continue
            except OSError:  # the backup is on another filesystem: copy, then rename
                pass
            staging = target.with_name(f"{STAGING_PREFIX}{uuid.uuid4().hex}")
            try:
                shutil.copy2(backup, staging)
                os.replace(staging, target)
            finally:
                with contextlib.suppress(OSError):
                    staging.unlink()


def extract_outputs(
    archive_file: IO[bytes],
    destination: Path,
    quota_bytes: int,
    protected: Iterable[tuple[int, int]] = (),
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    restore_dir: Path | None = None,
) -> list[Path]:
    """Validate the whole archive, stage it, then move it under ``destination``.

    Every member is first written into a private staging directory inside ``destination``.
    Only when all of them have been written are the files renamed into place, so a failure
    while reading or writing the archive leaves ``destination`` as it was. If a rename fails
    part-way, the outputs already moved are removed and the files they replaced are restored.

    Returns every file and directory this call *created* (files it replaced are not
    included). ``protected`` holds ``(st_dev, st_ino)`` identities (registered read-only
    sources) that are never replaced. With ``restore_dir``, the earlier versions of replaced
    files are kept there after a successful return, for :func:`remove_outputs`. On any error
    :class:`OutputArchiveError` is raised.
    """

    destination = destination.resolve(strict=True)
    protected_ids = set(protected)
    created: list[Path] = []
    replaced: list[tuple[Path, Path]] = []
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=destination))
        tree = stage / "new"
        tree.mkdir()
        backups = stage / "earlier" if restore_dir is None else restore_dir
        backups.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=archive_file, mode="r:") as archive:
            members = _members(archive, quota_bytes, max_members)
            for member, parts in members:
                if not _contained(destination, destination.joinpath(*parts)):
                    raise OutputArchiveError(f"output path escapes outputs/: {member.name!r}")
                _check_destination(destination, parts, member.isdir(), protected_ids)
            for member, parts in members:  # stage: nothing outside the staging directory yet
                staged = tree.joinpath(*parts)
                if member.isdir():
                    staged.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:  # pragma: no cover - regular members always have data
                    raise OutputArchiveError(f"archive member has no data: {member.name!r}")
                staged.parent.mkdir(parents=True, exist_ok=True)
                with source, open(staged, "xb") as sink:
                    shutil.copyfileobj(source, sink, 65536)
        for member, parts in members:  # commit
            if member.isdir():
                for depth in range(1, len(parts) + 1):
                    directory = destination.joinpath(*parts[:depth])
                    if not directory.exists():
                        directory.mkdir()
                        created.append(directory)
                continue
            _move_in(
                destination, parts, tree.joinpath(*parts), protected_ids, created, replaced, backups
            )
        if restore_dir is not None:
            manifest = [[str(target), str(backup)] for target, backup in replaced]
            (restore_dir / RESTORE_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    except OutputArchiveError:
        _remove(created)
        _restore(replaced)
        raise
    except (tarfile.TarError, OSError, ValueError) as exc:
        _remove(created)
        _restore(replaced)
        raise OutputArchiveError(f"could not extract outputs: {exc}") from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    return created


def remove_outputs(paths: Iterable[str], restore_dir: Path | None = None) -> None:
    """Best-effort undo of a copy-out whose registration failed.

    Removes the outputs the run created and, with ``restore_dir``, puts back the earlier
    versions of the outputs it replaced.
    """

    _remove(Path(path) for path in paths)
    if restore_dir is None:
        return
    with contextlib.suppress(OSError, ValueError, TypeError):
        manifest = json.loads((restore_dir / RESTORE_MANIFEST).read_text(encoding="utf-8"))
        _restore((Path(target), Path(backup)) for target, backup in manifest)
