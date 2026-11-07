"""Resource guards shared by execution backends.

These bound what the *server host* can be made to absorb while managed code runs:

* :func:`drain_bounded` reads a child stream into a capped buffer while continuing to drain,
  so a verbose child cannot exhaust server memory.
* :class:`WorkspaceDiskGuard` watches the writable workspace during execution and reports a
  breach as soon as new output exceeds the configured byte/entry budget or host free space
  falls below a reserve, so the backend can kill the workload *before* the host filesystem is
  exhausted rather than discovering oversized files only at artifact registration.

Accounting model and its limits:

* Usage is measured per directory entry (files, directories, symlinks, special files) keyed by
  path and inode. New entries count in full; pre-existing entries count only their growth.
  Deleting unrelated files never offsets new output.
* Any entry the guard cannot measure (for example a directory the workload made unreadable)
  is treated as a breach: the guard fails closed.
* A final synchronous check runs after the workload exits, so a writer that finishes before
  the first poll is still caught.
* The guard polls while the workload runs, so it can overshoot by its write throughput times
  the poll interval. The free-space reserve exists so that overshoot cannot reach zero. It is
  detection and termination, not a filesystem quota.
* On a breach, :meth:`WorkspaceDiskGuard.reclaim` removes every entry the workload created.
  Growth of files that existed before the run is reported but never reverted.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO


def drain_bounded(
    stream: IO[str],
    buffer: list[str],
    max_chars: int,
    truncated: threading.Event | None = None,
) -> None:
    """Read ``stream`` into ``buffer`` keeping at most ``max_chars``; keep draining the rest."""

    total = 0
    for chunk in iter(lambda: stream.read(65536), ""):
        if len(chunk) > max_chars - total and truncated is not None:
            truncated.set()
        if total < max_chars:
            take = chunk[: max_chars - total]
            buffer.append(take)
            total += len(take)
    stream.close()


@dataclass(frozen=True, slots=True)
class DiskLimits:
    """Budget for bytes/files managed code may add to its workspace during one execution."""

    max_output_bytes: int = 512 * 1024 * 1024
    max_output_files: int = 10_000
    min_free_bytes: int = 256 * 1024 * 1024
    poll_interval_seconds: float = 0.25


class DiskGuardError(RuntimeError):
    """The workspace could not be measured before execution, so no budget can be enforced."""


# Entry identity: relative path, device, inode, and whether it is a directory.
_Key = tuple[str, int, int, bool]


def _allocated(info: os.stat_result) -> int:
    # Count allocated blocks where available so sparse files are measured by real disk use;
    # fall back to apparent size on platforms without st_blocks.
    blocks = getattr(info, "st_blocks", None)
    if blocks is None:
        return info.st_size
    return int(blocks) * 512


def _scan(root: Path, excluded: tuple[Path, ...]) -> dict[_Key, int]:
    """Map every workspace entry below ``root`` to its allocated bytes.

    Raises ``OSError`` for anything that cannot be measured, except entries that vanished
    between listing and stat (a normal race with a running workload).
    """

    entries: dict[_Key, int] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            listing = list(os.scandir(directory))
        except FileNotFoundError:
            continue
        for entry in listing:
            path = Path(entry.path)
            if any(path == item or item in path.parents for item in excluded):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            is_dir = stat.S_ISDIR(info.st_mode)
            key = (path.relative_to(root).as_posix(), info.st_dev, info.st_ino, is_dir)
            entries[key] = _allocated(info)
            if is_dir:
                stack.append(path)
    return entries


class WorkspaceDiskGuard:
    """Background watchdog that calls ``on_breach`` once when the output budget is exceeded."""

    def __init__(
        self,
        workspace: Path,
        limits: DiskLimits,
        on_breach: Callable[[str], None],
        *,
        excluded_roots: tuple[Path, ...] = (),
    ) -> None:
        self.workspace = workspace
        self.limits = limits
        self.on_breach = on_breach
        self.excluded = tuple(path.resolve(strict=False) for path in excluded_roots)
        self.reason: str | None = None
        self.retained_bytes = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        try:
            self._baseline = _scan(workspace, self.excluded)
        except OSError as exc:
            raise DiskGuardError(
                f"cannot measure workspace before execution; refusing to run: {exc}"
            ) from exc
        self._thread = threading.Thread(target=self._run, name="workspace-disk-guard", daemon=True)

    def __enter__(self) -> WorkspaceDiskGuard:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def check(self) -> str | None:
        """Return a breach description, or ``None`` while within budget."""

        try:
            free = shutil.disk_usage(self.workspace).free
            current = _scan(self.workspace, self.excluded)
        except OSError as exc:
            return f"workspace could not be fully measured (failing closed): {exc}"
        if free < self.limits.min_free_bytes:
            return (
                f"host free disk space fell to {free} bytes, below the "
                f"{self.limits.min_free_bytes}-byte reserve"
            )
        added_bytes = 0
        added_entries = 0
        for key, allocated in current.items():
            before = self._baseline.get(key)
            if before is None:
                added_entries += 1
                added_bytes += allocated
            else:
                added_bytes += max(0, allocated - before)
        if added_bytes > self.limits.max_output_bytes:
            return (
                f"execution wrote {added_bytes} bytes to the workspace; output budget is "
                f"{self.limits.max_output_bytes}"
            )
        if added_entries > self.limits.max_output_files:
            return (
                f"execution created {added_entries} workspace entries; output budget is "
                f"{self.limits.max_output_files}"
            )
        return None

    def _record(self, reason: str) -> bool:
        with self._lock:
            if self.reason is not None:
                return False
            self.reason = reason
            return True

    def final_check(self) -> str | None:
        """Synchronously re-check after the workload has exited (catches sub-poll writers)."""

        if self.reason is None:
            reason = self.check()
            if reason is not None:
                self._record(reason)
        return self.reason

    def reclaim(self) -> None:
        """Remove every entry the workload created; report growth it left in old files.

        Call only after the workload has been terminated. Symlinks are removed, never
        followed, and directories the workload made unreadable are reopened (same owner)
        so their contents can be removed.
        """

        created: list[Path] = []
        stack = [self.workspace]
        while stack:
            directory = stack.pop()
            try:
                listing = list(os.scandir(directory))
            except PermissionError:
                with contextlib.suppress(OSError):
                    directory.chmod(0o700)
                try:
                    listing = list(os.scandir(directory))
                except OSError:
                    continue
            except OSError:
                continue
            for entry in listing:
                path = Path(entry.path)
                if any(path == item or item in path.parents for item in self.excluded):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                relative = path.relative_to(self.workspace).as_posix()
                key = (relative, info.st_dev, info.st_ino, is_dir)
                if key not in self._baseline:
                    created.append(path)
                if is_dir:
                    stack.append(path)
        # Deepest first so directories are empty by the time they are removed.
        for path in sorted(created, key=lambda item: len(item.parts), reverse=True):
            with contextlib.suppress(OSError):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
                    path.rmdir()
                else:
                    path.unlink()
        try:
            remaining = _scan(self.workspace, self.excluded)
        except OSError:
            return
        self.retained_bytes = sum(
            max(0, allocated - self._baseline.get(key, 0)) for key, allocated in remaining.items()
        )

    def _run(self) -> None:
        while not self._stop.wait(self.limits.poll_interval_seconds):
            reason = self.check()
            if reason is not None and self._record(reason):
                self.on_breach(reason)
                return
