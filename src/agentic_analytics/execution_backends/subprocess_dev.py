from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agentic_analytics.models import AnalysisSession, ExecutionStatus

from .base import BackendResult


class SubprocessDevBackend:
    """Non-conformant local backend for development only; never valid for strict mode."""

    name = "subprocess_dev"
    conformant = False

    def execute(
        self,
        session: AnalysisSession,
        script_path: Path,
        timeout_seconds: int,
    ) -> BackendResult:
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
        try:
            result = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout
            )
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr
            )
            return BackendResult(
                status=ExecutionStatus.TIMED_OUT,
                stdout=stdout or "",
                stderr=stderr or f"execution exceeded {timeout_seconds} seconds",
                runtime={"backend": self.name, "conformant": False},
            )
        status = ExecutionStatus.SUCCEEDED if result.returncode == 0 else ExecutionStatus.FAILED
        return BackendResult(
            status=status,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            runtime={"backend": self.name, "conformant": False},
        )

    def close_session(self, session_id: str) -> None:
        del session_id
