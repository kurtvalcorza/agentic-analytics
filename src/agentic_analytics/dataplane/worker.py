"""Isolated data-plane worker: ``python -I -m agentic_analytics.dataplane.worker``.

Reads one JSON request from stdin, applies OS resource limits, runs a single DuckDB operation,
writes one JSON response to stdout, and exits. Any crash, OOM kill, or hang is contained to
this process; the MCP server observes it as a failed operation and keeps running.

Limits applied on POSIX before any data is read:

* ``RLIMIT_AS``: address space, so DuckDB fails allocation with ``OutOfMemoryException``
  rather than exhausting host memory.
* ``RLIMIT_CPU``: CPU seconds across all threads.
* ``RLIMIT_FSIZE``: largest file the worker may write (spill and DuckDB temp files).
  ``SIGXFSZ`` is ignored so an oversized write fails with ``EFBIG`` instead of killing the
  process silently.
* ``RLIMIT_NOFILE``: open file descriptors.
* ``RLIMIT_CORE``: no core dumps of potentially sensitive data.

On hosts without the ``resource`` module (Windows) the worker still provides crash, hang, and
memory-fault containment through process separation, but not OS resource limits.
"""

from __future__ import annotations

import errno
import json
import signal
import sys
from typing import Any

_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_MESSAGE_CHARS = 2000


def _apply_limits(limits: dict[str, Any]) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX hosts
        return

    def setlimit(kind: int, value: int | None) -> None:
        if value is None:
            return
        _, hard = resource.getrlimit(kind)
        target = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(kind, (target, target))

    setlimit(resource.RLIMIT_AS, limits.get("address_space_bytes"))
    setlimit(resource.RLIMIT_CPU, limits.get("cpu_seconds"))
    setlimit(resource.RLIMIT_FSIZE, limits.get("max_file_bytes"))
    setlimit(resource.RLIMIT_NOFILE, limits.get("max_open_files"))
    setlimit(resource.RLIMIT_CORE, 0)
    if hasattr(signal, "SIGXFSZ"):
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)


def _classify(exc: BaseException) -> str:
    try:
        return _classify_duckdb(exc)
    except Exception:  # e.g. the failure was DuckDB failing to import under the memory limit
        return "out_of_memory" if isinstance(exc, MemoryError) else "internal"


def _classify_duckdb(exc: BaseException) -> str:
    import duckdb

    from .engine import SpillLimitError

    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, SpillLimitError):
        return "spill_limit"
    if isinstance(exc, (MemoryError, duckdb.OutOfMemoryException)) or _thread_exhaustion(exc):
        return "out_of_memory"
    if _file_too_large(exc):
        # RLIMIT_FSIZE (or the spill/temp file) hit its cap. Checked before the generic IO
        # mapping so it is never mistaken for an unreadable source.
        return "disk_limit"
    if isinstance(
        exc, (duckdb.InvalidInputException, duckdb.IOException, duckdb.ConversionException)
    ):
        return "source_read"
    if isinstance(exc, duckdb.Error):
        return "sql"
    return "internal"


def _file_too_large(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and exc.errno == errno.EFBIG:
        return True
    return "file too large" in str(exc).lower()


def _thread_exhaustion(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "can't start new thread" in str(exc)


# Categories whose underlying message may name server-internal paths (scratch, state, or
# archive directories). The client receives a fixed description instead.
_FIXED_MESSAGES = {
    "disk_limit": (
        "data-plane worker exceeded its file-size limit (data_plane_max_file_bytes); the "
        "query needed more temporary or output space than allowed"
    ),
}


def _respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=str))
    sys.stdout.flush()


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        _respond({"error": {"category": "internal", "type": "RequestTooLarge", "message": ""}})
        return 2
    request = json.loads(raw)
    # Limits first, before DuckDB is imported or any source byte is read.
    _apply_limits(request.get("os_limits", {}))
    try:
        from .engine import OPERATIONS

        operation = OPERATIONS[request["op"]]
        result = operation(request["payload"])
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        category = _classify(exc)
        _respond(
            {
                "error": {
                    "category": category,
                    "type": type(exc).__name__,
                    "message": _FIXED_MESSAGES.get(category, str(exc)[:_MAX_MESSAGE_CHARS]),
                }
            }
        )
        return 1
    _respond({"result": result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
