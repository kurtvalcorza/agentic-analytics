"""Process-wide and cross-process coordination for one persistent state store."""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


@contextmanager
def record_lock(path: Path) -> Iterator[None]:
    """Hold an advisory lock until the operation finishes, including on exceptions.

    Thread locks also serialize callers in the same process; file locks coordinate
    independent MCP server processes sharing the configured state directory.
    """

    path = path.resolve(strict=False)
    with _guard:
        lock = _locks.setdefault(path, threading.Lock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
