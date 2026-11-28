"""Server-side client for the isolated DuckDB data-plane worker (SEC-02).

Every DuckDB operation runs in a fresh, short-lived worker process. The server only sends
already-authorized absolute paths and validated, rewritten SQL, and only reads back a bounded
JSON response. A worker that crashes, exhausts its memory, hangs, or floods its output is killed
and reported as a failed operation; the MCP server process is never affected.

The worker runs as the server's OS user, but on Linux it confines itself with Landlock and a
seccomp filter before DuckDB is loaded: it can read only the runtime and the operation's
authorized source files, write only its private scratch directory and the operation's own
output directory, and open no socket, run no program, and start no child process. Code running
inside a compromised worker therefore cannot reach the workspace, server state, other sessions'
archives, the Docker socket, or the network. The worker also exits when the server does. See
``sandbox.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import IO, Any

from agentic_analytics.layout import archive_dir, dataplane_scratch_dir
from agentic_analytics.settings import Settings

from .requests import EngineLimits, WorkerRequest
from .sandbox import SandboxPolicy

_log = logging.getLogger(__name__)

# Seconds allowed beyond an operation's own deadline before the worker is killed. Covers
# interpreter start-up and the worker's graceful in-process interrupt.
_KILL_GRACE_SECONDS = 10.0
_MAX_STDERR_BYTES = 64 * 1024
# Scratch directories older than this belong to workers whose server died mid-operation.
_STALE_SCRATCH_SECONDS = 24 * 3600


class DataPlaneError(RuntimeError):
    """A data-plane operation failed. ``category`` is stable; ``error_type`` is diagnostic.

    ``str(error)`` is the public message: safe to return to MCP clients and to store in
    execution records, because server paths are already redacted from it. ``detail`` holds the
    worker's raw text for server-side logs only; never return it to a client.

    Categories: ``timeout``, ``out_of_memory``, ``disk_limit``, ``spill_limit``,
    ``source_read``, ``sql``, ``crashed``, ``response_too_large``, ``sandbox_unavailable``,
    ``internal``.
    """

    def __init__(
        self, category: str, error_type: str, message: str, *, detail: str | None = None
    ) -> None:
        super().__init__(message or f"data-plane worker failed ({category})")
        self.category = category
        self.error_type = error_type
        self.detail = detail


_REDACTED_ROOT = "<path>"


def _absolute_strings(value: Any) -> list[str]:
    """Every absolute-path string anywhere in a request payload."""

    if isinstance(value, str):
        return [value] if os.path.isabs(value) else []
    if isinstance(value, dict):
        return [found for item in value.values() for found in _absolute_strings(item)]
    if isinstance(value, (list, tuple)):
        return [found for item in value for found in _absolute_strings(item)]
    return []


def redact_paths(message: str, files: list[str], roots: list[str]) -> str:
    """Replace server paths in ``message``: a file by its name, a root by ``<path>``.

    DuckDB quotes SQL literals with doubled single quotes, so each path is also matched in that
    form. Longer paths are replaced first, so a file is named before its directory is redacted.
    """

    replacements: dict[str, str] = {}
    for root in roots:
        replacements[root] = _REDACTED_ROOT
    for path in files:
        replacements[path] = Path(path).name or _REDACTED_ROOT
    for original, public in list(replacements.items()):
        replacements.setdefault(original.replace("'", "''"), public)
    for original in sorted(replacements, key=len, reverse=True):
        if original and original not in {"/", os.sep}:
            message = message.replace(original, replacements[original])
    return message


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
    # Ask the kernel to kill the worker when the server exits (Linux PR_SET_PDEATHSIG). The
    # worker starts its portable parent-polling watchdog only when the death signal could not
    # be armed (macOS, a failed prctl, or this flag off); tests turn the flag off to
    # exercise the watchdog alone.
    parent_death_signal = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.work_root = dataplane_scratch_dir(settings.state_dir.resolve(strict=False))
        self._swept = False

    def limits(self, timeout_seconds: float) -> EngineLimits:
        """Engine limits shared by every operation."""

        return EngineLimits(
            timeout_seconds=timeout_seconds,
            memory_limit=self.settings.query_memory_limit,
            preview_bytes=self.settings.max_result_preview_bytes,
            cell_chars=self.settings.max_result_cell_chars,
            threads=self.settings.data_plane_threads,
            max_temp_bytes=self.settings.data_plane_max_temp_bytes,
        )

    def _os_limits(self, deadline_seconds: float) -> dict[str, Any]:
        threads = self.settings.data_plane_threads
        return {
            "address_space_bytes": self.settings.data_plane_memory_bytes,
            # CPU seconds across every DuckDB thread plus the interpreter itself.
            "cpu_seconds": math.ceil(deadline_seconds * (threads + 1)) + 5,
            "max_file_bytes": self.settings.data_plane_max_file_bytes,
            "max_open_files": 256,
        }

    def sandbox_policy(self, request: WorkerRequest, workdir: Path) -> SandboxPolicy:
        """Least-privilege filesystem policy for one operation.

        Built only from the request's declared paths (C10). Read: its authorized source files.
        Write: the private scratch directory plus the request's own output directories (for a
        query, the per-execution archive directory that receives the spill).
        """

        return SandboxPolicy(
            mode=self.settings.data_plane_sandbox,
            read_files=request.read_paths(),
            write_dirs=(str(workdir), *request.write_dirs()),
            protected_roots=self.protected_roots(),
        )

    def protected_roots(self) -> tuple[str, ...]:
        """Directories no sandbox grant may cover: workspaces, server state, and archives.

        The archive root and the data-plane scratch root live inside ``state_dir``.
        """

        settings = self.settings
        roots = [
            *settings.allowed_workspace_roots,
            settings.workspace_base_dir,
            settings.state_dir,
            archive_dir(settings.state_dir),
        ]
        return tuple(dict.fromkeys(os.path.realpath(root) for root in roots))

    def _redaction_roots(self, workdir: Path) -> list[str]:
        """Server directories never named in a public message, as given and as resolved."""

        settings = self.settings
        given = [
            *settings.allowed_workspace_roots,
            settings.workspace_base_dir,
            settings.state_dir,
            self.work_root,
            workdir,
        ]
        roots = [str(Path(root).expanduser().absolute()) for root in given]
        return list(dict.fromkeys([*roots, *self.protected_roots(), str(workdir.resolve())]))

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
        return env

    @staticmethod
    def _kill_group(pgid: int) -> None:
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

        # PR_SET_PDEATHSIG fires when the *thread* that forked the worker exits, not the whole
        # server process. This thread therefore must outlive the worker: it blocks in
        # proc.wait() below until the worker is reaped, and must keep doing so.
        proc = subprocess.Popen(
            self.worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=self._environment(),
            start_new_session=True,
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

    def call(self, request: WorkerRequest, *, deadline_seconds: float) -> dict[str, Any]:
        """Run one operation in a fresh worker and return its result or raise DataPlaneError."""

        op = request.op

        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._swept:
            self._sweep_stale()
            self._swept = True
        workdir = Path(tempfile.mkdtemp(prefix=f"{op}-", dir=self.work_root))
        # Collected before the scratch temp_directory is added: it sits under workdir, a root.
        request_files = _absolute_strings(request.to_json())
        try:
            request = request.with_limits(
                replace(request.limits, temp_directory=str(workdir / "duckdb"))
            )
            envelope = json.dumps(
                {
                    "op": op,
                    "payload": request.to_json(),
                    "os_limits": self._os_limits(deadline_seconds),
                    "sandbox": self.sandbox_policy(request, workdir).to_json(),
                    "parent": {"pid": os.getpid(), "pdeathsig": self.parent_death_signal},
                }
            ).encode("utf-8")
            returncode, out, out_overflow, timed_out = self._run_worker(
                workdir, envelope, deadline_seconds
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
                category = str(error.get("category", "internal"))
                error_type = str(error.get("type", "Error"))
                raw = str(error.get("message", ""))
                # DuckDB errors can quote the rewritten SQL and so the absolute source path.
                public = redact_paths(raw, request_files, self._redaction_roots(workdir))
                _log.debug("data-plane %s failed (%s, %s): %s", op, category, error_type, raw)
                raise DataPlaneError(category, error_type, public, detail=raw)
            result = response.get("result")
            if not isinstance(result, dict):
                raise DataPlaneError("internal", "MalformedResponse", "malformed worker response")
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
