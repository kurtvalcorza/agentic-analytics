from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import threading
from collections.abc import Iterator
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
        protected_state_root: Path | None = None,
    ) -> None:
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.max_output_chars = max_output_chars
        self.protected_state_root = protected_state_root

    @staticmethod
    def _session_suffix(session_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "-", session_id)[:48]

    def _container_name(self, session_id: str) -> str:
        # Unique per execution: ephemeral containers cannot be reused under a stale policy and
        # are removed (with all descendant processes) when the run ends.
        return f"agentic-analytics-{self._session_suffix(session_id)}-{uuid4().hex[:12]}"

    @staticmethod
    def _user_args() -> list[str]:
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if os.name != "posix" or getuid is None or getgid is None:
            return []
        return ["--user", f"{getuid()}:{getgid()}"]

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

    @staticmethod
    def _mount(*fields: str) -> list[str]:
        # Docker's --mount value is CSV. Quoting whole fields preserves literal commas in
        # authorized paths without allowing them to inject additional mount options.
        value = io.StringIO()
        csv.writer(value, lineterminator="").writerow(fields)
        return ["--mount", value.getvalue()]

    @staticmethod
    def _files(root: Path) -> Iterator[Path]:
        def failed(exc: OSError) -> None:
            raise DockerExecutionError(f"cannot inspect protected workspace files: {exc}")

        for directory, directories, files in os.walk(root, followlinks=False, onerror=failed):
            parent = Path(directory)
            directories[:] = [
                name
                for name in directories
                if not (parent / name).is_symlink() and not (parent / name).is_junction()
            ]
            for name in files:
                path = parent / name
                # Symlinks resolve through their target mount; bind mounting their link name
                # could instead make Docker follow a target outside the container workspace.
                if not path.is_symlink() and path.is_file():
                    yield path

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        info = path.stat()
        return info.st_dev, info.st_ino

    def _mount_args(
        self, workspace: Path, script: Path, readonly_paths: tuple[Path, ...]
    ) -> list[str]:
        state = (self.protected_state_root or workspace / ".agentic-analytics" / "state").resolve()
        if workspace.is_relative_to(state):
            raise DockerExecutionError("workspace must not be inside the protected state directory")
        script_identity = self._identity(script)
        source_identities: set[tuple[int, int]] = set()
        for requested in readonly_paths:
            source = requested.resolve(strict=True)
            if not source.is_relative_to(workspace) or not source.is_file():
                raise DockerExecutionError("read-only sources must be regular workspace files")
            if (
                source.is_relative_to(state)
                or source.relative_to(workspace).parts[0] == ".agentic-analytics"
            ):
                raise DockerExecutionError("read-only source overlaps protected runtime files")
            identity = self._identity(source)
            if identity == script_identity:
                raise DockerExecutionError("read-only source overlaps the execution script")
            source_identities.add(identity)

        # A hardlink outside the hidden state directory would otherwise expose or mutate the
        # same server record/archive inode through the writable workspace mount.
        state_identities = (
            {self._identity(path) for path in self._files(state)} if state.exists() else set()
        )
        protected_identities = source_identities | {script_identity}
        protected_files: list[Path] = []
        for path in self._files(workspace):
            if path.is_relative_to(state):
                continue
            identity = self._identity(path)
            if identity in state_identities:
                raise DockerExecutionError("workspace contains a hardlink to protected state")
            if identity in protected_identities:
                protected_files.append(path)

        # A leaf mount prevents replacing that leaf, but an unmounted ancestor can still
        # be renamed. Pin every ancestor as a writable mount boundary so state cannot be
        # moved out of its mask, and source paths cannot be replaced by moving their parent.
        # Sibling files remain writable; only the protected leaves become hidden/read-only.
        protected_leaves = [*protected_files]
        if state.is_relative_to(workspace):
            protected_leaves.append(state)
        ancestors = {
            parent
            for path in protected_leaves
            for parent in path.parents
            if parent != workspace and parent.is_relative_to(workspace)
        }
        mounts = self._mount("type=bind", f"src={workspace}", "dst=/workspace")
        for parent in sorted(ancestors, key=lambda item: (len(item.parts), item.as_posix())):
            target = "/workspace/" + parent.relative_to(workspace).as_posix()
            mounts += self._mount("type=bind", f"src={parent}", f"dst={target}")
        if state.is_relative_to(workspace):
            target = "/workspace/" + state.relative_to(workspace).as_posix()
            mounts += self._mount("type=tmpfs", f"dst={target}", "tmpfs-size=1048576", "readonly")
        for path in protected_files:
            target = "/workspace/" + path.relative_to(workspace).as_posix()
            mounts += self._mount("type=bind", f"src={path}", f"dst={target}", "readonly")
        mounts += self._mount(
            "type=bind", f"src={script}", "dst=/run/agentic-analytics/request.py", "readonly"
        )
        return mounts

    def _run_args(
        self,
        session: AnalysisSession,
        container: str,
        script_path: Path,
        *,
        readonly_paths: tuple[Path, ...] = (),
    ) -> list[str]:
        workspace = Path(session.workspace_root).resolve(strict=True)
        script = script_path.resolve(strict=True)
        if not script.is_file():
            raise DockerExecutionError("execution script must be a regular file")
        mounts = self._mount_args(workspace, script, readonly_paths)
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
            *mounts,
            "-w",
            "/workspace",
            self.image,
            "python",
            "/run/agentic-analytics/request.py",
        ]

    def _drain(
        self, stream: IO[str], buffer: list[str], truncated: threading.Event | None = None
    ) -> None:
        """Read a stream into a capped buffer, discarding overflow but continuing to drain.

        This bounds server memory to ``max_output_chars`` per stream even when managed code
        prints gigabytes within its timeout, instead of buffering everything.
        """

        total = 0
        for chunk in iter(lambda: stream.read(65536), ""):
            if len(chunk) > self.max_output_chars - total and truncated is not None:
                truncated.set()
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
        *,
        readonly_paths: tuple[Path, ...] = (),
    ) -> BackendResult:
        if not self._image_exists():
            raise DockerExecutionError(
                f"execution image {self.image!r} is not available; build docker/Dockerfile.exec"
            )
        container = self._container_name(session.id)
        runtime = {"backend": self.name, "image": self.image, "network": "none"}

        proc = subprocess.Popen(
            [
                "docker",
                *self._run_args(session, container, script_path, readonly_paths=readonly_paths),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None and proc.stderr is not None
        out_buffer: list[str] = []
        err_buffer: list[str] = []
        stdout_truncated = threading.Event()
        stderr_truncated = threading.Event()
        out_thread = threading.Thread(
            target=self._drain, args=(proc.stdout, out_buffer, stdout_truncated)
        )
        err_thread = threading.Thread(
            target=self._drain, args=(proc.stderr, err_buffer, stderr_truncated)
        )
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
                stdout_truncated=stdout_truncated.is_set(),
                stderr_truncated=stderr_truncated.is_set(),
            )
        status = ExecutionStatus.SUCCEEDED if proc.returncode == 0 else ExecutionStatus.FAILED
        return BackendResult(
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            runtime=runtime,
            stdout_truncated=stdout_truncated.is_set(),
            stderr_truncated=stderr_truncated.is_set(),
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
