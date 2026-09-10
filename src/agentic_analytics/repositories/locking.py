"""Process-wide and cross-process coordination for one persistent state store."""

from __future__ import annotations

import errno
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

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
            if sys.platform == "win32":
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        time.sleep(0.05)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
