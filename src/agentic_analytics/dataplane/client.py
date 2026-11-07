"""Server-side client for the isolated DuckDB data-plane worker (SEC-02).

Every DuckDB operation runs in a fresh, short-lived worker process. The server only sends
already-authorized absolute paths and validated, rewritten SQL, and only reads back a bounded
JSON response. A worker that crashes, exhausts its memory, hangs, or floods its output is killed
and reported as a failed operation; the MCP server process is never affected.

This is a failure-containment and resource boundary, not a privilege boundary: the worker runs
as the server's OS user, so a remote-code-execution flaw in DuckDB would run with the server's
filesystem permissions (but with a scrubbed environment and DuckDB's own file restrictions).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, Any

from agentic_analytics.settings import Settings

# Seconds allowed beyond an operation's own deadline before the worker is killed. Covers
# interpreter start-up and the worker's graceful in-process interrupt.
_KILL_GRACE_SECONDS = 10.0
_MAX_STDERR_BYTES = 64 * 1024
# Scratch directories older than this belong to workers whose server died mid-operation.
_STALE_SCRATCH_SECONDS = 24 * 3600


class DataPlaneError(RuntimeError):
    """A data-plane operation failed. ``category`` is stable; ``error_type`` is diagnostic.

    Categories: ``timeout``, ``out_of_memory``, ``disk_limit``, ``spill_limit``,
    ``source_read``, ``sql``, ``crashed``, ``response_too_large``, ``internal``.
    """

    def __init__(self, category: str, error_type: str, message: str) -> None:
        super().__init__(message or f"data-plane worker failed ({category})")
        self.category = category
        self.error_type = error_type


def _read_bounded(
    stream: IO[bytes], buffer: bytearray, limit: int, overflow: threading.Event
) -> None:
    for chunk in iter(lambda: stream.read(65536), b""):
        if len(buffer) + len(chunk) > limit:
            overflow.set()
            buffer.extend(chunk[: max(0, limit - len(buffer))])
            continue  # keep draining so the worker never blocks on a full pipe
        buffer.extend(chunk)
    stream.close()


def _write_request(stream: IO[bytes], request: bytes) -> None:
    with contextlib.suppress(OSError):  # BrokenPipeError once the worker exits or is killed
        stream.write(request)
    with contextlib.suppress(OSError):
        stream.close()


class DataPlaneClient:
    # Seconds allowed beyond an operation's own deadline before the worker is killed.
    kill_grace_seconds = _KILL_GRACE_SECONDS

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.work_root = settings.state_dir.resolve(strict=False) / "tmp" / "dataplane"
        self._swept = False

    def limits(self, timeout_seconds: float) -> dict[str, Any]:
        """Engine limits shared by every operation."""

        return {
            "timeout_seconds": timeout_seconds,
            "memory_limit": self.settings.query_memory_limit,
            "preview_bytes": self.settings.max_result_preview_bytes,
            "cell_chars": self.settings.max_result_cell_chars,
            "threads": self.settings.data_plane_threads,
            "max_temp_bytes": self.settings.data_plane_max_temp_bytes,
        }

    def _os_limits(self, deadline_seconds: float) -> dict[str, Any]:
        threads = self.settings.data_plane_threads
        return {
            "address_space_bytes": self.settings.data_plane_memory_bytes,
            # CPU seconds across every DuckDB thread plus the interpreter itself.
            "cpu_seconds": math.ceil(deadline_seconds * (threads + 1)) + 5,
            "max_file_bytes": self.settings.data_plane_max_file_bytes,
            "max_open_files": 256,
        }

    @staticmethod
    def worker_command() -> list[str]:
        # -I: ignore PYTHON* variables, user site-packages, and the working directory, so a
        # file in the workspace or scratch directory can never shadow a module.
        return [sys.executable, "-I", "-m", "agentic_analytics.dataplane.worker"]

    @staticmethod
    def _environment() -> dict[str, str]:
        # Never hand server secrets (API keys, tokens) to a process parsing untrusted files.
        env = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            # glibc reserves a 64 MiB malloc arena per thread by default, which can exhaust the
            # worker's address-space limit with many DuckDB threads. Bound the arena count.
            "MALLOC_ARENA_MAX": "2",
        }
        # Coverage measurement settings only (no credentials), so test runs can measure the
        # worker the same way they measure the server.
        env.update({key: value for key, value in os.environ.items() if key.startswith("COVERAGE_")})
        if os.name == "nt":  # pragma: no cover - Windows needs these to start Python
            for name in ("SYSTEMROOT", "TEMP", "TMP"):
                if name in os.environ:
                    env[name] = os.environ[name]
        return env

    @staticmethod
    def _kill_group(pgid: int) -> None:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)

    def _kill(self, proc: subprocess.Popen[bytes]) -> None:
        self._kill_group(proc.pid)
        with contextlib.suppress(OSError):
            proc.kill()

    def _run_worker(
        self, workdir: Path, request: bytes, deadline_seconds: float
    ) -> tuple[int | None, bytes, bool, bool]:
        """Run the worker; return (returncode, stdout, stdout_overflowed, timed_out).

        The worker is always reaped, even if setting up the pipes or threads fails. The process
        group is only signalled while it is known to exist (before the leader is reaped, or
        while a descendant still holds the pipes), so a recycled group id is never targeted.
        """

        proc = subprocess.Popen(
            self.worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=self._environment(),
            start_new_session=os.name == "posix",
        )
        reaped = False
        try:
            assert proc.stdin and proc.stdout and proc.stderr
            out = bytearray()
            err = bytearray()
            out_overflow = threading.Event()
            readers = [
                threading.Thread(
                    target=_read_bounded,
                    args=(
                        proc.stdout,
                        out,
                        self.settings.data_plane_max_response_bytes,
                        out_overflow,
                    ),
                ),
                threading.Thread(
                    target=_read_bounded,
                    args=(proc.stderr, err, _MAX_STDERR_BYTES, threading.Event()),
                ),
            ]
            # The request is written from its own thread, so a worker that never reads stdin
            # cannot block the server on a full pipe: the deadline below still applies, and
            # killing the worker closes the pipe, which releases the writer.
            writer = threading.Thread(target=_write_request, args=(proc.stdin, request))
            for thread in (*readers, writer):
                thread.start()
            timed_out = False
            try:
                proc.wait(timeout=deadline_seconds + self.kill_grace_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill(proc)  # leader not reaped yet: its group id is still ours
            proc.wait()
            reaped = True
            writer.join(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
            if any(thread.is_alive() for thread in (*readers, writer)):
                # A descendant still holds the pipes, so the group still exists; kill it.
                self._kill_group(proc.pid)
                for thread in (*readers, writer):
                    thread.join(timeout=5)
            return proc.returncode, bytes(out), out_overflow.is_set(), timed_out
        finally:
            if not reaped:
                self._kill(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=30)

    def _sweep_stale(self) -> None:
        """Remove scratch directories left by workers whose server died mid-operation.

        Only directories older than any possible operation are removed, so concurrent server
        processes sharing this state directory are never disturbed.
        """

        cutoff = time.time() - _STALE_SCRATCH_SECONDS
        with contextlib.suppress(OSError):
            for entry in self.work_root.iterdir():
                with contextlib.suppress(OSError):
                    if entry.is_dir() and entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)

    def call(self, op: str, payload: dict[str, Any], *, deadline_seconds: float) -> dict[str, Any]:
        """Run one operation in a fresh worker and return its result or raise DataPlaneError."""

        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._swept:
            self._sweep_stale()
            self._swept = True
        workdir = Path(tempfile.mkdtemp(prefix=f"{op}-", dir=self.work_root))
        try:
            payload = dict(payload)
            payload["limits"] = {**payload["limits"], "temp_directory": str(workdir / "duckdb")}
            request = json.dumps(
                {"op": op, "payload": payload, "os_limits": self._os_limits(deadline_seconds)}
            ).encode("utf-8")
            returncode, out, out_overflow, timed_out = self._run_worker(
                workdir, request, deadline_seconds
            )
            if timed_out:
                raise DataPlaneError(
                    "timeout", "TimeoutError", "query exceeded the configured execution time limit"
                )
            if out_overflow:
                raise DataPlaneError(
                    "response_too_large",
                    "ResponseTooLarge",
                    "data-plane response exceeded the configured size limit",
                )
            try:
                response = json.loads(bytes(out)) if out else None
            except json.JSONDecodeError:
                response = None
            if not isinstance(response, dict):
                code = returncode
                how = (
                    f"signal {signal.Signals(-code).name}"
                    if code is not None and code < 0 and -code in signal.Signals._value2member_map_
                    else f"exit code {code}"
                )
                # A worker killed by its CPU or memory limit (or a native crash) lands here.
                raise DataPlaneError(
                    "crashed", "WorkerCrashed", f"data-plane worker terminated abnormally ({how})"
                )
            if "error" in response:
                error = response["error"]
                raise DataPlaneError(
                    str(error.get("category", "internal")),
                    str(error.get("type", "Error")),
                    str(error.get("message", "")),
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise DataPlaneError("internal", "MalformedResponse", "malformed worker response")
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
