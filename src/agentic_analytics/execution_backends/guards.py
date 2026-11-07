"""Resource guards shared by execution backends.

These bound what the *server host* can be made to absorb while managed code runs:

* :func:`drain_bounded` reads a child stream into a capped buffer while continuing to drain,
  so a verbose child cannot exhaust server memory.
* :class:`WorkspaceDiskGuard` watches the writable workspace during execution and reports a
  breach as soon as new output exceeds the configured byte/file budget or host free space
  falls below a reserve, so the backend can kill the workload *before* the host filesystem is
  exhausted rather than discovering oversized files only at artifact registration.

The disk guard polls, so a workload can overshoot the budget by at most its write throughput
times the poll interval. The free-space reserve exists so that overshoot cannot reach zero.
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_INTERNAL_DIR = ".agentic-analytics"


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


@dataclass(frozen=True, slots=True)
class _Usage:
    allocated_bytes: int
    files: int


def _allocated(info: os.stat_result) -> int:
    # Count allocated blocks where available so sparse files are measured by real disk use;
    # fall back to apparent size on platforms without st_blocks.
    blocks = getattr(info, "st_blocks", None)
    if blocks is None:
        return info.st_size
    return int(blocks) * 512


def _workspace_usage(root: Path, excluded: tuple[Path, ...]) -> _Usage:
    total = 0
    files = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if directory == root and entry.name == _INTERNAL_DIR:
                continue
            if any(path == item or item in path.parents for item in excluded):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
                continue
            files += 1
            if stat.S_ISREG(info.st_mode):
                total += _allocated(info)
    return _Usage(total, files)


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
        self._stop = threading.Event()
        self._baseline = _workspace_usage(workspace, self.excluded)
        self._thread = threading.Thread(target=self._run, name="workspace-disk-guard", daemon=True)

    def __enter__(self) -> WorkspaceDiskGuard:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def check(self) -> str | None:
        """Return a breach description, or ``None`` while within budget."""

        free = shutil.disk_usage(self.workspace).free
        if free < self.limits.min_free_bytes:
            return (
                f"host free disk space fell to {free} bytes, below the "
                f"{self.limits.min_free_bytes}-byte reserve"
            )
        usage = _workspace_usage(self.workspace, self.excluded)
        added_bytes = usage.allocated_bytes - self._baseline.allocated_bytes
        if added_bytes > self.limits.max_output_bytes:
            return (
                f"execution wrote {added_bytes} bytes to the workspace; output budget is "
                f"{self.limits.max_output_bytes}"
            )
        added_files = usage.files - self._baseline.files
        if added_files > self.limits.max_output_files:
            return (
                f"execution created {added_files} workspace files; output budget is "
                f"{self.limits.max_output_files}"
            )
        return None

    def _run(self) -> None:
        while not self._stop.wait(self.limits.poll_interval_seconds):
            reason = self.check()
            if reason is not None:
                self.reason = reason
                self.on_breach(reason)
                return
