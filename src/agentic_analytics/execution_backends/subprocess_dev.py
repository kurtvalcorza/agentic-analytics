from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from agentic_analytics.models import AnalysisSession, ExecutionStatus

from .base import BackendResult
from .guards import DiskGuardError, DiskLimits, WorkspaceDiskGuard, drain_bounded


class SubprocessDevBackend:
    """Non-conformant local backend for development only; never valid for strict mode.

    It runs trusted code natively on the host and is not a security boundary. It still bounds
    captured output, workspace disk growth, and cleans up the whole process tree so a runaway
    development script cannot exhaust server memory or host disk.
    """

    name = "subprocess_dev"
    conformant = False

    def __init__(
        self,
        *,
        max_output_chars: int = 65536,
        disk_limits: DiskLimits | None = None,
        protected_state_root: Path | None = None,
    ) -> None:
        self.max_output_chars = max_output_chars
        self.disk_limits = disk_limits or DiskLimits()
        self.protected_state_root = protected_state_root

    @staticmethod
    def _kill_tree(proc: subprocess.Popen[str]) -> None:
        if os.name == "posix":
            # The child leads its own session, so this reaches every descendant it spawned,
            # including ones that outlive the direct child (the group persists while any
            # member does, so its id cannot be reused by an unrelated process meanwhile).
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            with contextlib.suppress(OSError):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
        with contextlib.suppress(OSError):
            proc.kill()

    def execute(
        self,
        session: AnalysisSession,
        script_path: Path,
        timeout_seconds: int,
        *,
        readonly_paths: tuple[Path, ...] = (),
    ) -> BackendResult:
        # Host-native development execution cannot enforce read-only mounts.
        del readonly_paths
        workspace = Path(session.workspace_root).resolve(strict=True)
        internal_home = workspace / ".agentic-analytics" / "dev-home"
        internal_home.mkdir(parents=True, exist_ok=True)
        env = {
            "HOME": str(internal_home),
            "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        runtime: dict[str, object] = {"backend": self.name, "conformant": False}
        excluded = (self.protected_state_root,) if self.protected_state_root else ()
        holder: list[subprocess.Popen[str]] = []

        def on_breach(_: str) -> None:
            if holder:
                self._kill_tree(holder[0])

        try:
            guard = WorkspaceDiskGuard(
                workspace, self.disk_limits, on_breach, excluded_roots=excluded
            )
        except DiskGuardError as exc:
            raise PermissionError(str(exc)) from exc
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
        holder.append(proc)
        assert proc.stdout is not None and proc.stderr is not None
        out_buffer: list[str] = []
        err_buffer: list[str] = []
        stdout_truncated = threading.Event()
        stderr_truncated = threading.Event()
        threads = [
            threading.Thread(
                target=drain_bounded,
                args=(proc.stdout, out_buffer, self.max_output_chars, stdout_truncated),
            ),
            threading.Thread(
                target=drain_bounded,
                args=(proc.stderr, err_buffer, self.max_output_chars, stderr_truncated),
            ),
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        with guard:
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_tree(proc)
                proc.wait(timeout=30)
        # A surviving grandchild could hold the pipes open; reap the whole group regardless.
        self._kill_tree(proc)
        for thread in threads:
            thread.join(timeout=10)
        stdout = "".join(out_buffer)
        stderr = "".join(err_buffer)
        resource_limit: str | None = None
        exit_code: int | None = proc.returncode
        # Catch writers that finished before the first poll, then reclaim their output.
        if guard.final_check() is not None:
            guard.reclaim()
            status = ExecutionStatus.FAILED
            stderr = f"execution terminated: {guard.reason}"
            resource_limit = "disk"
            runtime["resource_limit"] = "disk"
            runtime["retained_output_bytes"] = guard.retained_bytes
        elif timed_out:
            status = ExecutionStatus.TIMED_OUT
            stderr = stderr or f"execution exceeded {timeout_seconds} seconds"
            exit_code = None
        else:
            status = ExecutionStatus.SUCCEEDED if proc.returncode == 0 else ExecutionStatus.FAILED
        return BackendResult(
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            runtime=runtime,
            stdout_truncated=stdout_truncated.is_set(),
            stderr_truncated=stderr_truncated.is_set(),
            resource_limit=resource_limit,
        )

    def close_session(self, session_id: str) -> None:
        del session_id
