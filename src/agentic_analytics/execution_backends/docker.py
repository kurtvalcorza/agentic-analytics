from __future__ import annotations

import contextlib
import csv
import io
import os
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

from agentic_analytics import limits
from agentic_analytics.layout import INTERNAL_DIR_NAME
from agentic_analytics.models import AnalysisSession, ExecutionStatus

from .base import BackendResult
from .guards import DiskGuardError, DiskLimits, WorkspaceDiskGuard, drain_bounded
from .output_quota import (
    CONTAINER_OUTPUT_DIR,
    OUTPUT_DIR_NAME,
    RESTORE_DIR_NAME,
    SUPERVISOR_PATH,
    SUPERVISOR_SOURCE,
    TAR_OVERHEAD_BYTES,
    TAR_OVERHEAD_FACTOR,
    VERDICT_MAX_BYTES,
    VERDICT_PATH,
    OutputArchiveError,
    extract_outputs,
    parse_verdict,
    quota_threshold,
    read_capped,
)

# Fixed unprivileged identity baked into docker/Dockerfile.exec. Used whenever the host cannot
# supply a non-root numeric identity (non-POSIX Docker Desktop hosts).
DEFAULT_CONTAINER_USER = "10001:10001"
_VERDICT_POLL_SECONDS = 0.2
# The supervisor enforces the script's own timeout from the moment the script starts. The
# server's backstop adds this much for container start-up and judgement, so slow start-up
# is not misreported as a script timeout while a wedged run is still killed.
_SERVER_GRACE_SECONDS = 30.0
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
        memory: str = limits.DOCKER_MEMORY,
        cpus: float = limits.DOCKER_CPUS,
        pids_limit: int = limits.DOCKER_PIDS_LIMIT,
        max_output_chars: int = limits.MAX_OUTPUT_CHARS,
        protected_state_root: Path | None = None,
        disk_limits: DiskLimits | None = None,
        user: str | None = None,
        output_quota_bytes: int = limits.DOCKER_OUTPUT_QUOTA_BYTES,
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
        self.output_quota_bytes = output_quota_bytes

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
                        f"workspace contains a socket or device node; refusing to mount it: {path}"
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
        state = (self.protected_state_root or workspace / INTERNAL_DIR_NAME / "state").resolve()
        if workspace.is_relative_to(state):
            raise DockerExecutionError("workspace must not be inside the protected state directory")
        outputs = self._outputs_dir(workspace)
        if state.is_relative_to(outputs):
            raise DockerExecutionError("server state must not be inside workspace outputs/")
        script_identity = self._identity(script)
        source_identities: set[tuple[int, int]] = set()
        for requested in readonly_paths:
            source = requested.resolve(strict=True)
            if not source.is_relative_to(workspace) or not source.is_file():
                raise DockerExecutionError("read-only sources must be regular workspace files")
            if (
                source.is_relative_to(state)
                or source.relative_to(workspace).parts[0] == INTERNAL_DIR_NAME
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
            # outputs/ is shadowed by the run's tmpfs; files there are neither visible to nor
            # writable by managed code, and copy-out refuses to overwrite protected inodes.
            if path.is_relative_to(state) or path.is_relative_to(outputs):
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
        # SEC-01: the workspace and every pinned ancestor are read-only; the run's only
        # writable output location is its quota-limited tmpfs at /workspace/outputs.
        mounts = self._mount("type=bind", f"src={workspace}", "dst=/workspace", "readonly")
        for parent in sorted(ancestors, key=lambda item: (len(item.parts), item.as_posix())):
            target = "/workspace/" + parent.relative_to(workspace).as_posix()
            mounts += self._mount("type=bind", f"src={parent}", f"dst={target}", "readonly")
        mounts += self._mount(
            "type=tmpfs",
            f"dst={CONTAINER_OUTPUT_DIR}",
            f"tmpfs-size={self.output_quota_bytes}",
            "tmpfs-mode=1777",
        )
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
        supervisor = script.parent / "agentic-analytics-supervisor.py"
        supervisor.write_text(SUPERVISOR_SOURCE, encoding="utf-8")
        mounts += self._mount(
            "type=bind", f"src={supervisor}", f"dst={SUPERVISOR_PATH}", "readonly"
        )
        return mounts

    @staticmethod
    def _outputs_dir(workspace: Path) -> Path:
        """Host ``workspace/outputs``: the tmpfs mount point and the copy-out destination."""

        outputs = workspace / OUTPUT_DIR_NAME
        if outputs.is_symlink() or (outputs.exists() and not outputs.is_dir()):
            raise DockerExecutionError("workspace outputs/ must be a real directory")
        outputs.mkdir(exist_ok=True)
        return outputs

    def _run_args(
        self,
        session: AnalysisSession,
        container: str,
        script_path: Path,
        *,
        readonly_paths: tuple[Path, ...] = (),
        timeout_seconds: int = 3600,
    ) -> list[str]:
        workspace = Path(session.workspace_root).resolve(strict=True)
        script = script_path.resolve(strict=True)
        if not script.is_file():
            raise DockerExecutionError("execution script must be a regular file")
        mounts = self._mount_args(workspace, script, readonly_paths)
        return [
            "run",
            "--rm",
            # stdin carries the per-run verdict key to the supervisor and, when closed, lets the
            # container exit after outputs have been copied out.
            "-i",
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
            # The supervisor must be PID 1 so kill(-1) clears every other process; a daemon-wide
            # `"init": true` would otherwise make an init PID 1 and every run would fail closed.
            "--init=false",
            *self._user_args(),
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m",
            # Explicit, so the memory budget in Settings counts the real /dev/shm size
            # whatever the daemon's default-shm-size is.
            "--shm-size",
            "64m",
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
            SUPERVISOR_PATH,
            str(quota_threshold(self.output_quota_bytes)),
            str(timeout_seconds),
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
        runtime: dict[str, Any] = {"backend": self.name, "image": self.image, "network": "none"}
        run_args = self._run_args(
            session,
            container,
            script_path,
            readonly_paths=readonly_paths,
            timeout_seconds=timeout_seconds,
        )

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
        key = secrets.token_hex(32)
        runtime["output_quota_bytes"] = self.output_quota_bytes
        proc = subprocess.Popen(
            ["docker", *run_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
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

        # Docker keeps an attached container's streams open until the container exits, so
        # completion of the managed script is observed through the supervisor's verdict file.
        # The container stays alive (blocked on stdin) until outputs have been copied out.
        outcome: dict[str, Any] = {"status": ExecutionStatus.FAILED}
        try:
            with contextlib.suppress(OSError):
                proc.stdin.write(key + "\n")
                proc.stdin.flush()
            verdict_ready = False
            with guard:
                deadline = time.monotonic() + timeout_seconds + _SERVER_GRACE_SECONDS
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break  # the container died before reporting
                    probe = self._exec(container, ["test", "-f", VERDICT_PATH])
                    if probe == 0:
                        verdict_ready = True
                        break
                    time.sleep(_VERDICT_POLL_SECONDS)
                timed_out = not verdict_ready and proc.poll() is None
                if timed_out:
                    kill_container()
            outcome = self._judge(
                container,
                key,
                workspace,
                readonly_paths,
                guard,
                timeout_seconds,
                timed_out,
                runtime,
                script_path.parent / RESTORE_DIR_NAME,
            )
        finally:
            # Closing stdin lets the supervisor exit; --rm then removes the container and its
            # tmpfs. rm -f covers every path where that did not happen.
            with contextlib.suppress(OSError):
                proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._run(["rm", "-f", container], timeout=30)
            out_thread.join(timeout=10)
            err_thread.join(timeout=10)
        stderr = outcome.get("stderr")
        if stderr is None and not err_buffer:
            stderr = outcome.get("stderr_default")
        return BackendResult(
            status=outcome["status"],
            stdout="".join(out_buffer),
            stderr="".join(err_buffer) if stderr is None else stderr,
            exit_code=outcome.get("exit_code"),
            runtime=outcome.get("runtime", runtime),
            stdout_truncated=stdout_truncated.is_set(),
            stderr_truncated=stderr_truncated.is_set(),
            resource_limit=outcome.get("resource_limit"),
            output_paths=outcome.get("output_paths", ()),
        )

    def _judge(
        self,
        container: str,
        key: str,
        workspace: Path,
        readonly_paths: tuple[Path, ...],
        guard: WorkspaceDiskGuard,
        timeout_seconds: int,
        timed_out: bool,
        runtime: dict[str, Any],
        restore_dir: Path,
    ) -> dict[str, Any]:
        """Decide the run's outcome while its container (and tmpfs) is still alive."""

        if guard.final_check() is not None:
            guard.reclaim()
            return {
                "status": ExecutionStatus.FAILED,
                "stderr": f"execution terminated: {guard.reason}",
                "runtime": {
                    **runtime,
                    "resource_limit": "disk",
                    "retained_output_bytes": guard.retained_bytes,
                },
                "resource_limit": "disk",
            }
        if timed_out:
            return {
                "status": ExecutionStatus.TIMED_OUT,
                "stderr_default": f"execution exceeded {timeout_seconds} seconds",
            }
        raw = self._exec_output(container, ["head", "-c", str(VERDICT_MAX_BYTES), VERDICT_PATH])
        verdict = parse_verdict(raw, key) if raw is not None else None
        if verdict is None:
            # The container died early, or the verdict is missing or forged: fail closed.
            return {
                "status": ExecutionStatus.FAILED,
                "stderr": "managed execution ended without a valid supervisor verdict",
            }
        runtime["output_min_free_bytes"] = verdict.min_free_bytes
        if verdict.timed_out:
            return {
                "status": ExecutionStatus.TIMED_OUT,
                "stderr_default": (f"execution exceeded {timeout_seconds} seconds"),
            }
        if verdict.others_remaining:
            return {
                "status": ExecutionStatus.FAILED,
                "stderr": "execution terminated: processes outlived the script and could "
                "not be stopped; outputs were discarded",
            }
        if verdict.quota_exhausted:
            return {
                "status": ExecutionStatus.FAILED,
                "stderr": (
                    "execution terminated: outputs/ reached its quota of "
                    f"{self.output_quota_bytes} bytes; partial outputs were discarded"
                ),
                "exit_code": verdict.exit_code,
                "runtime": {**runtime, "resource_limit": "disk", "retained_output_bytes": 0},
                "resource_limit": "disk",
            }
        failure, created = self._copy_out(container, workspace, readonly_paths, restore_dir)
        if failure is not None:
            return {
                "status": ExecutionStatus.FAILED,
                "stderr": f"execution terminated: {failure}",
                "exit_code": verdict.exit_code,
                "runtime": {**runtime, "resource_limit": "output_archive"},
                "resource_limit": "output_archive",
            }
        # A failed script's outputs are still copied out, as before SEC-01, so a caller can
        # inspect partial work; they are never reported as a success.
        status = ExecutionStatus.SUCCEEDED if verdict.exit_code == 0 else ExecutionStatus.FAILED
        return {
            "status": status,
            "exit_code": verdict.exit_code,
            "output_paths": tuple(str(path) for path in created),
        }

    def _copy_out(
        self,
        container: str,
        workspace: Path,
        readonly_paths: tuple[Path, ...],
        restore_dir: Path,
    ) -> tuple[str | None, list[Path]]:
        """Stream outputs/ out of the live container; return (failure, created paths).

        Earlier versions of outputs the run replaced are kept in ``restore_dir`` (server
        state, next to the request script) until the caller has registered the artifacts.
        """

        cap = self.output_quota_bytes * TAR_OVERHEAD_FACTOR + TAR_OVERHEAD_BYTES
        protected = {self._identity(path) for path in readonly_paths if path.exists()}
        tar = subprocess.Popen(
            ["docker", "exec", container, "tar", "-C", CONTAINER_OUTPUT_DIR, "-cf", "-", "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert tar.stdout is not None
        with tempfile.TemporaryFile() as spool:
            try:
                within = read_capped(tar.stdout, spool, cap)
            finally:
                if tar.poll() is None:
                    tar.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    tar.wait(timeout=30)
            if not within:
                return "outputs archive exceeded its size cap", []
            if tar.returncode != 0:
                return f"could not read outputs/ (tar exit {tar.returncode})", []
            spool.seek(0)
            try:
                created = extract_outputs(
                    spool,
                    workspace / OUTPUT_DIR_NAME,
                    self.output_quota_bytes,
                    protected,
                    max_members=self.disk_limits.max_output_files,
                    restore_dir=restore_dir,
                )
            except OutputArchiveError as exc:
                return str(exc), []
        return None, created

    def _exec(self, container: str, command: list[str]) -> int:
        """Run ``docker exec``; a hung exec counts as a failure, never an exception."""

        try:
            return self._run(["exec", container, *command], timeout=30).returncode
        except subprocess.TimeoutExpired:
            return -1

    def _exec_output(self, container: str, command: list[str]) -> str | None:
        try:
            done = self._run(["exec", container, *command], timeout=30)
        except subprocess.TimeoutExpired:
            return None
        return done.stdout if done.returncode == 0 else None

    def close_session(self, session_id: str) -> None:
        # Remove any container still labelled for this session (defensive; ephemeral runs
        # normally self-remove via --rm).
        listed = self._run(
            ["ps", "-aq", "--filter", f"label=agentic-analytics.session={session_id}"]
        )
        container_ids = [line for line in listed.stdout.split() if line]
        if container_ids:
            self._run(["rm", "-f", *container_ids], timeout=30)
