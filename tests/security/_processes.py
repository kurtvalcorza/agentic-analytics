"""Process probes shared by the security tests that kill processes (T1, T2).

They read ``/proc`` where it exists (Linux) and fall back to ``kill(pid, 0)`` elsewhere.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

HAS_PROC = Path("/proc/self/task").is_dir()


def alive(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie awaiting its reaper."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    with contextlib.suppress(OSError):
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return False
    return True


def thread_count(pid: int) -> int:
    """Threads in ``pid`` per ``/proc``; 0 when it is gone or ``/proc`` is unavailable."""

    try:
        return len(os.listdir(f"/proc/{pid}/task"))
    except OSError:
        return 0


def wait_for(condition: Callable[[], object], seconds: float, interval: float = 0.02) -> bool:
    """Poll ``condition()`` until it is truthy or ``seconds`` pass; return the last result."""

    deadline = time.monotonic() + seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return bool(condition())
        time.sleep(interval)


def describe(pid: int) -> str:
    """State, parent, blocked signals and thread count of ``pid``, for failure messages."""

    try:
        lines = Path(f"/proc/{pid}/status").read_text().splitlines()
    except OSError:
        return f"pid {pid}: no /proc entry (alive={alive(pid)})"
    keep = ("State:", "PPid:", "SigBlk:", "SigIgn:", "Threads:")
    return f"pid {pid}: " + "; ".join(line for line in lines if line.startswith(keep))
