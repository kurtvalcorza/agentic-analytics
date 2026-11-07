from __future__ import annotations

import csv
import io
import os
import re
import stat
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import IO
from uuid import uuid4

from agentic_analytics.models import AnalysisSession, ExecutionStatus

from .base import BackendResult
from .guards import DiskGuardError, DiskLimits, WorkspaceDiskGuard, drain_bounded

# Fixed unprivileged identity baked into docker/Dockerfile.exec. Used whenever the host cannot
# supply a non-root numeric identity (non-POSIX Docker Desktop hosts).
DEFAULT_CONTAINER_USER = "10001:10001"
_USER_PATTERN = re.compile(r"^[0-9]+:[0-9]+$")


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
        disk_limits: DiskLimits | None = None,
        user: str | None = None,
    ) -> None:
        if user is not None and not _USER_PATTERN.fullmatch(user):
            raise ValueError("docker user must be a numeric 'uid:gid' pair")
        self.image = image
        self.disk_limits = disk_limits or DiskLimits()
        self.user = user
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

    def _user_args(self) -> list[str]:
        """Always pass an explicit, non-root container identity.

        POSIX hosts reuse the server's own uid/gid so bind-mounted workspace writes keep the
        caller's ownership. Hosts without a numeric identity use the image's fixed unprivileged
        user. A root server is refused rather than silently running managed code as uid 0;
        operators can opt into a specific identity with ``docker_user``.
        """

        if self.user is not None:
            uid = int(self.user.split(":", 1)[0])
            if uid == 0:
                raise DockerExecutionError(
                    "refusing to run managed code as container root (docker_user uid 0)"
                )
            return ["--user", self.user]
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if os.name != "posix" or getuid is None or getgid is None:
            return ["--user", DEFAULT_CONTAINER_USER]
        if getuid() == 0:
            raise DockerExecutionError(
                "refusing to run managed code as container root: the server is running as "
                "root. Run it as an unprivileged user or set AGENTIC_ANALYTICS_DOCKER_USER "
                "to a non-root 'uid:gid' that can write the workspace."
            )
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
                try:
                    mode = path.lstat().st_mode
                except OSError as exc:
                    failed(exc)
                # A Unix socket (for example a Docker daemon or agent control socket) or a device
                # node would give managed code a control channel that --network none does not
                # cover, so a workspace containing one is never mounted.
                if stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    raise DockerExecutionError(
                        "workspace contains a socket or device node; refusing to mount it: "
                        f"{path}"
                    )
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
            # Explicit mode: Docker otherwise copies the (private 0700) state directory's mode
            # onto a root-owned mask, leaving managed code an unreadable rather than empty dir.
            mounts += self._mount(
                "type=tmpfs", f"dst={target}", "tmpfs-size=1048576", "tmpfs-mode=0555", "readonly"
            )
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

        drain_bounded(stream, buffer, self.max_output_chars, truncated)

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
        run_args = self._run_args(session, container, script_path, readonly_paths=readonly_paths)

        def kill_container(_: str = "") -> None:
            # Killing the ephemeral container tears down the whole process tree; --rm removes it.
            self._run(["kill", container], check=False, timeout=30)

        # Baseline workspace usage before the container can write anything.
        workspace = Path(session.workspace_root).resolve(strict=True)
        excluded = (self.protected_state_root,) if self.protected_state_root else ()
        try:
            guard = WorkspaceDiskGuard(
                workspace, self.disk_limits, kill_container, excluded_roots=excluded
            )
        except DiskGuardError as exc:
            raise DockerExecutionError(str(exc)) from exc
        proc = subprocess.Popen(
            ["docker", *run_args],
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
        with guard:
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_container()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
        out_thread.join(timeout=10)
        err_thread.join(timeout=10)
        stdout = "".join(out_buffer)
        stderr = "".join(err_buffer)

        # Catch writers that finished before the first poll, then reclaim their output.
        if guard.final_check() is not None:
            guard.reclaim()
            return BackendResult(
                status=ExecutionStatus.FAILED,
                stdout=stdout,
                stderr=f"execution terminated: {guard.reason}",
                exit_code=proc.returncode,
                runtime={
                    **runtime,
                    "resource_limit": "disk",
                    "retained_output_bytes": guard.retained_bytes,
                },
                stdout_truncated=stdout_truncated.is_set(),
                stderr_truncated=stderr_truncated.is_set(),
                resource_limit="disk",
            )
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
