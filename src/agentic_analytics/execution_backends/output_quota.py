"""Quota-limited output mount for managed Docker execution (SEC-01, issue #22).

Each run gets a fresh, size-limited tmpfs at ``/workspace/outputs``: the only writable place
under ``/workspace``, which is otherwise mounted read-only. The kernel enforces the quota, so a
writer hits ``ENOSPC`` at the limit instead of filling the host disk.

The tmpfs lives only as long as its container, so outputs are copied out while the container
is still alive:

1. The container's main process is a small supervisor (``SUPERVISOR_SOURCE``, mounted read-only
   next to the request script). It runs the managed script as a child in its own session,
   samples free space and free inodes on the tmpfs while the child runs, kills the child's
   process group when it exits, and writes a verdict authenticated with a per-run key. The key
   reaches only the supervisor (on its stdin), never the managed code, so the code cannot
   forge a verdict. The verdict reports the quota as exhausted when free space ever dropped to
   ``quota_threshold`` or free inodes reached zero, even if the script caught ``ENOSPC`` and
   exited 0. The supervisor then closes its stdout and stderr and waits for stdin to close.
2. The server sees EOF on both streams, reads the verdict with ``docker exec``, and, unless the
   run failed its quota, streams the outputs out with ``docker exec tar``. The tar stream is
   capped, and :func:`extract_outputs` accepts only regular files and directories whose paths
   stay inside ``workspace/outputs``. Links, devices, absolute paths, ``..`` components, and
   archives over the quota fail the run closed; nothing is written until the whole archive
   has been validated.
3. The server closes the supervisor's stdin. The container exits, ``--rm`` removes it, and the
   tmpfs goes with it. The kill and error paths ``docker kill`` / ``docker rm -f`` the
   container, which also discards the tmpfs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

OUTPUT_DIR_NAME = "outputs"
CONTAINER_OUTPUT_DIR = "/workspace/outputs"
SUPERVISOR_PATH = "/run/agentic-analytics/supervisor.py"
VERDICT_PATH = "/tmp/.agentic-analytics-verdict.json"
# A tar stream carries a 512-byte header per entry plus block padding, so its cap is looser
# than the quota on extracted bytes, which is enforced exactly by extract_outputs.
TAR_OVERHEAD_FACTOR = 2
TAR_OVERHEAD_BYTES = 1024 * 1024


def quota_threshold(quota_bytes: int) -> int:
    """Free bytes at or below which the run is judged to have exhausted its quota."""

    return max(64 * 1024, quota_bytes // 100)


SUPERVISOR_SOURCE = r'''
import hashlib, hmac, json, os, signal, subprocess, sys, threading

OUT = "/workspace/outputs"
VERDICT = "/tmp/.agentic-analytics-verdict.json"
key = sys.stdin.buffer.readline().strip()
threshold = int(sys.argv[1])
low = {"free": None, "inodes": None}
done = threading.Event()


def sample():
    st = os.statvfs(OUT)
    free = st.f_bavail * st.f_frsize
    low["free"] = free if low["free"] is None else min(low["free"], free)
    if st.f_files:
        low["inodes"] = st.f_favail if low["inodes"] is None else min(low["inodes"], st.f_favail)


def poll():
    while not done.wait(0.02):
        sample()


sample()
child = subprocess.Popen(
    [sys.executable, "/run/agentic-analytics/request.py"],
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
watcher = threading.Thread(target=poll, daemon=True)
watcher.start()
code = child.wait()
try:
    os.killpg(child.pid, signal.SIGKILL)
except OSError:
    pass
done.set()
watcher.join()
sample()
exhausted = low["free"] <= threshold or low["inodes"] == 0
body = json.dumps(
    {"exit_code": code, "quota_exhausted": exhausted, "min_free_bytes": low["free"]},
    sort_keys=True,
)
mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
staging = VERDICT + "." + str(os.getpid())
fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
os.write(fd, json.dumps({"body": body, "mac": mac}).encode())
os.close(fd)
os.replace(staging, VERDICT)  # atomic: the server never reads a half-written verdict
sys.stdout.flush()
sys.stderr.flush()
os.close(1)
os.close(2)
sys.stdin.buffer.read()
'''


class OutputArchiveError(RuntimeError):
    """The outputs archive was oversized or unsafe; nothing was written."""


@dataclass(frozen=True, slots=True)
class Verdict:
    exit_code: int
    quota_exhausted: bool
    min_free_bytes: int | None


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
            exit_code=int(data["exit_code"]),
            quota_exhausted=bool(data["quota_exhausted"]),
            min_free_bytes=None if data["min_free_bytes"] is None else int(data["min_free_bytes"]),
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


def _members(archive: tarfile.TarFile, quota_bytes: int) -> list[tuple[tarfile.TarInfo, Path]]:
    accepted: list[tuple[tarfile.TarInfo, Path]] = []
    total = 0
    for member in archive.getmembers():
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise OutputArchiveError(f"output path escapes outputs/: {member.name!r}")
        parts = [part for part in name.parts if part not in ("", ".")]
        if not member.isdir() and not member.isreg():
            raise OutputArchiveError(
                f"outputs may contain only files and directories: {member.name!r}"
            )
        if not parts:
            continue
        total += member.size if member.isreg() else 0
        if total > quota_bytes:
            raise OutputArchiveError("outputs archive exceeds the output quota")
        accepted.append((member, Path(*parts)))
    return accepted


def _no_symlink_below(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise OutputArchiveError(f"refusing to write through a symlink: {current}")


def extract_outputs(
    archive_file: IO[bytes],
    destination: Path,
    quota_bytes: int,
    protected: Iterable[tuple[int, int]] = (),
) -> list[Path]:
    """Validate the whole archive, then write its files under ``destination``.

    ``protected`` holds ``(st_dev, st_ino)`` identities (registered read-only sources) that must
    never be overwritten, even through an existing file of the same name.
    """

    destination = destination.resolve(strict=True)
    protected_ids = set(protected)
    try:
        archive = tarfile.open(fileobj=archive_file, mode="r:")  # noqa: SIM115 - closed below
    except tarfile.TarError as exc:
        raise OutputArchiveError(f"unreadable outputs archive: {exc}") from exc
    with archive:
        try:
            members = _members(archive, quota_bytes)
        except tarfile.TarError as exc:
            raise OutputArchiveError(f"unreadable outputs archive: {exc}") from exc
        # Validate every destination before writing anything.
        for member, relative in members:
            target = destination / relative
            _no_symlink_below(destination, target)
            if member.isreg() and target.exists():
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) in protected_ids:
                    raise OutputArchiveError(f"refusing to overwrite protected path: {relative}")
        written: list[Path] = []
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        for member, relative in members:
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _no_symlink_below(destination, target)
            source = archive.extractfile(member)
            if source is None:  # pragma: no cover - regular members always have data
                continue
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o644)
            with os.fdopen(fd, "wb") as handle, source:
                while chunk := source.read(65536):
                    handle.write(chunk)
            written.append(target)
        return written
