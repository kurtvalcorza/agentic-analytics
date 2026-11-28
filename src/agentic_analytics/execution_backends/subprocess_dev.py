from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from agentic_analytics import limits
from agentic_analytics.layout import INTERNAL_DIR_NAME
from agentic_analytics.models import AnalysisSession, ExecutionStatus
from agentic_analytics.procutil import drain_bounded, kill_tree

from .base import BackendResult
from .guards import DiskGuardError, DiskLimits, WorkspaceDiskGuard


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
        max_output_chars: int = limits.MAX_OUTPUT_CHARS,
        disk_limits: DiskLimits | None = None,
        protected_state_root: Path | None = None,
    ) -> None:
        self.max_output_chars = max_output_chars
        self.disk_limits = disk_limits or DiskLimits()
        self.protected_state_root = protected_state_root

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
        internal_home = workspace / INTERNAL_DIR_NAME / "dev-home"
        internal_home.mkdir(parents=True, exist_ok=True)
        env = {
            "HOME": str(internal_home),
            "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        runtime: dict[str, object] = {"backend": self.name, "conformant": False}
        excluded = (self.protected_state_root,) if self.protected_state_root else ()
        holder: list[subprocess.Popen[str]] = []

        def on_breach(_: str) -> None:
            if holder:
                kill_tree(holder[0])

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
            start_new_session=True,
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
                kill_tree(proc)
                proc.wait(timeout=30)
        # A surviving descendant could hold the pipes open, so the group is swept even after
        # the leader is reaped. This reaches every descendant, including ones that closed
        # their pipes; the group id stays reserved while any member is alive.
        kill_tree(proc)
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
