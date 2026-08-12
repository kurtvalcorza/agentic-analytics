"""Unsafe development backend; never conformant for strict mode."""

import subprocess
import sys

from .base import ExecutionResult


class SubprocessDevelopmentBackend:
    conformant = False

    def execute(self, code: str, workspace: str, timeout: int) -> ExecutionResult:
        try:
            process = subprocess.run(
                [sys.executable, "-I", "-c", code], cwd=workspace, env={"PATH": "/usr/bin:/bin"},
                text=True, capture_output=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult("timed_out", exc.stdout or "", exc.stderr or "", error={"type": "timeout"})
        return ExecutionResult(
            "succeeded" if process.returncode == 0 else "failed", process.stdout, process.stderr,
            {"provider": "subprocess-dev", "python": sys.version.split()[0]},
            None if process.returncode == 0 else {"type": "nonzero_exit", "returncode": str(process.returncode)},
        )

