from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agentic_analytics.models import AnalysisSession, ExecutionStatus

from .base import BackendResult


class DockerExecutionError(RuntimeError):
    pass


class DockerBackend:
    name = "docker"
    conformant = True

    def __init__(
        self,
        image: str,
        *,
        memory: str = "1g",
        cpus: float = 1.0,
        pids_limit: int = 128,
    ) -> None:
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit

    @staticmethod
    def _session_suffix(session_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "-", session_id)[:48]

    def _container_name(self, session_id: str) -> str:
        return f"agentic-analytics-{self._session_suffix(session_id)}"

    @staticmethod
    def _run(
        args: list[str], *, check: bool = False, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def _is_running(self, name: str) -> bool:
        result = self._run(["inspect", "-f", "{{.State.Running}}", name])
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def _exists(self, name: str) -> bool:
        result = self._run(["inspect", name])
        return result.returncode == 0

    def _image_exists(self) -> bool:
        return self._run(["image", "inspect", self.image]).returncode == 0

    def _ensure_container(self, session: AnalysisSession) -> str:
        name = self._container_name(session.id)
        if self._is_running(name):
            return name
        if self._exists(name):
            started = self._run(["start", name], check=False)
            if started.returncode != 0:
                raise DockerExecutionError(started.stderr.strip() or "failed to start container")
            return name
        if not self._image_exists():
            raise DockerExecutionError(
                f"execution image {self.image!r} is not available; build docker/Dockerfile.exec"
            )
        workspace = str(Path(session.workspace_root).resolve(strict=True))
        result = self._run(
            [
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "agentic-analytics.managed=true",
                "--label",
                f"agentic-analytics.session={session.id}",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m",
                "--memory",
                self.memory,
                "--cpus",
                str(self.cpus),
                "--pids-limit",
                str(self.pids_limit),
                "-e",
                "HOME=/tmp",
                "-e",
                "MPLCONFIGDIR=/tmp/matplotlib",
                "-e",
                "MPLBACKEND=Agg",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "PYTHONUNBUFFERED=1",
                "-v",
                f"{workspace}:/workspace:rw",
                "-w",
                "/workspace",
                self.image,
            ],
            timeout=60,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or "failed to create execution container"
            raise DockerExecutionError(message)
        return name

    def execute(
        self,
        session: AnalysisSession,
        script_path: Path,
        timeout_seconds: int,
    ) -> BackendResult:
        name = self._ensure_container(session)
        workspace = Path(session.workspace_root).resolve(strict=True)
        relative_script = script_path.resolve(strict=True).relative_to(workspace).as_posix()
        try:
            result = self._run(
                [
                    "exec",
                    "-w",
                    "/workspace",
                    name,
                    "python",
                    f"/workspace/{relative_script}",
                ],
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            self.close_session(session.id)
            return BackendResult(
                status=ExecutionStatus.TIMED_OUT,
                stderr=f"execution exceeded {timeout_seconds} seconds",
                runtime={"backend": self.name, "image": self.image, "network": "none"},
            )
        status = ExecutionStatus.SUCCEEDED if result.returncode == 0 else ExecutionStatus.FAILED
        return BackendResult(
            status=status,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            runtime={"backend": self.name, "image": self.image, "network": "none"},
        )

    def close_session(self, session_id: str) -> None:
        name = self._container_name(session_id)
        self._run(["rm", "-f", name], timeout=30)
