from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import IO
from uuid import uuid4

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
        max_output_chars: int = 65536,
    ) -> None:
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.max_output_chars = max_output_chars

    @staticmethod
    def _session_suffix(session_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "-", session_id)[:48]

    def _container_name(self, session_id: str) -> str:
        # Unique per execution: ephemeral containers cannot be reused under a stale policy and
        # are removed (with all descendant processes) when the run ends.
        return f"agentic-analytics-{self._session_suffix(session_id)}-{uuid4().hex[:12]}"

    @staticmethod
    def _user_args() -> list[str]:
        if os.name != "posix":
            return []
        return ["--user", f"{os.getuid()}:{os.getgid()}"]

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

    def _image_exists(self) -> bool:
        return self._run(["image", "inspect", self.image]).returncode == 0

    def _run_args(
        self, session: AnalysisSession, container: str, relative_script: str
    ) -> list[str]:
        workspace = str(Path(session.workspace_root).resolve(strict=True))
        return [
            "run",
            "--rm",
            "--name",
            container,
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
            *self._user_args(),
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m",
            # Mask the persistent server state directory so managed code cannot read or forge
            # repository records even when state_dir is nested under the mounted workspace.
            "--tmpfs",
            "/workspace/.agentic-analytics/state:rw,nosuid,nodev,size=1m",
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
            "python",
            f"/workspace/{relative_script}",
        ]

    def _drain(self, stream: IO[str], buffer: list[str]) -> None:
        """Read a stream into a capped buffer, discarding overflow but continuing to drain.

        This bounds server memory to ``max_output_chars`` per stream even when managed code
        prints gigabytes within its timeout, instead of buffering everything.
        """

        total = 0
        for chunk in iter(lambda: stream.read(65536), ""):
            if total < self.max_output_chars:
                take = chunk[: self.max_output_chars - total]
                buffer.append(take)
                total += len(take)
        stream.close()

    def execute(
        self,
        session: AnalysisSession,
        script_path: Path,
        timeout_seconds: int,
    ) -> BackendResult:
        if not self._image_exists():
            raise DockerExecutionError(
                f"execution image {self.image!r} is not available; build docker/Dockerfile.exec"
            )
        workspace = Path(session.workspace_root).resolve(strict=True)
        relative_script = script_path.resolve(strict=True).relative_to(workspace).as_posix()
        container = self._container_name(session.id)
        runtime = {"backend": self.name, "image": self.image, "network": "none"}

        proc = subprocess.Popen(
            ["docker", *self._run_args(session, container, relative_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None and proc.stderr is not None
        out_buffer: list[str] = []
        err_buffer: list[str] = []
        out_thread = threading.Thread(target=self._drain, args=(proc.stdout, out_buffer))
        err_thread = threading.Thread(target=self._drain, args=(proc.stderr, err_buffer))
        out_thread.start()
        err_thread.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Killing the ephemeral container tears down the whole process tree; --rm removes it.
            self._run(["kill", container], check=False, timeout=30)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        out_thread.join(timeout=10)
        err_thread.join(timeout=10)
        stdout = "".join(out_buffer)
        stderr = "".join(err_buffer)

        if timed_out:
            return BackendResult(
                status=ExecutionStatus.TIMED_OUT,
                stdout=stdout,
                stderr=stderr or f"execution exceeded {timeout_seconds} seconds",
                runtime=runtime,
            )
        status = ExecutionStatus.SUCCEEDED if proc.returncode == 0 else ExecutionStatus.FAILED
        return BackendResult(
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            runtime=runtime,
        )

    def close_session(self, session_id: str) -> None:
        # Remove any container still labelled for this session (defensive; ephemeral runs
        # normally self-remove via --rm).
        listed = self._run(
            ["ps", "-aq", "--filter", f"label=agentic-analytics.session={session_id}"]
        )
        container_ids = [line for line in listed.stdout.split() if line]
        if container_ids:
            self._run(["rm", "-f", *container_ids], timeout=30)
