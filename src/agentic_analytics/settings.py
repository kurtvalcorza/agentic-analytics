import os
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import limits
from .layout import INTERNAL_DIR_NAME, archive_dir, dataplane_scratch_dir, scratch_dir

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]i?b?|b|bytes)?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "b": 1, "bytes": 1}
for _index, _prefix in enumerate("kmgt", start=1):
    _UNITS[_prefix] = _UNITS[_prefix + "b"] = 1000**_index
    _UNITS[_prefix + "i"] = _UNITS[_prefix + "ib"] = 1024**_index
# Address space the worker needs beyond DuckDB's own memory_limit: the interpreter, DuckDB's
# shared library, and per-thread stacks (glibc arenas are capped via MALLOC_ARENA_MAX).
_WORKER_BASE_BYTES = 256 * 1024**2
_WORKER_PER_THREAD_BYTES = 16 * 1024**2


def parse_size(value: str) -> int | None:
    """Parse a DuckDB-style size ("1GB", "512MiB"); None when not understood."""

    match = _SIZE.match(value)
    if match is None:
        return None
    unit = (match.group(2) or "").lower()
    return int(float(match.group(1)) * _UNITS[unit])


_DOCKER_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}
# Container tmpfs mounts other than outputs/: /tmp (128 MiB) and /dev/shm (64 MiB).
_DOCKER_SCRATCH_TMPFS_BYTES = (128 + 64) * 1024**2


def _docker_bytes(value: str) -> int | None:
    """Parse a Docker memory size (``1g``, ``512m``, ``1.5GiB``); None when unrecognised.

    Follows Docker's own rule: a decimal number, an optional binary unit from k to p, and an
    optional ``i`` and ``b``.
    """

    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?) ?([kmgtp]?)i?b?\s*", value.lower())
    if match is None:
        return None
    return int(float(match.group(1)) * _DOCKER_UNITS[match.group(2)])


class Settings(BaseSettings):
    """Process configuration. Filesystem authority is always resolved server-side."""

    model_config = SettingsConfigDict(env_prefix="AGENTIC_ANALYTICS_", extra="ignore")

    state_dir: Path = Field(default=Path(INTERNAL_DIR_NAME) / "state")
    workspace_base_dir: Path = Field(default=Path(INTERNAL_DIR_NAME) / "workspaces")
    allowed_workspace_roots: list[Path] = Field(default_factory=lambda: [Path.cwd()])
    max_sample_rows: int = Field(default=100, ge=1, le=1000)
    max_query_rows: int = Field(default=200, ge=1, le=10_000)
    max_profile_columns: int = Field(default=200, ge=1, le=2000)
    max_discovered_sources: int = Field(default=500, ge=1, le=100_000)
    query_timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    query_memory_limit: str = Field(default="1GB")
    # Isolated data-plane worker (SEC-02). Every DuckDB operation (query, inspection,
    # validation statistics) runs in a separate process with these OS limits.
    data_plane_timeout_seconds: float = Field(default=300.0, gt=0, le=86_400)
    data_plane_memory_bytes: int = Field(default=3 * 1024**3, ge=512 * 1024**2)
    # Caps each file the worker writes, including DuckDB's temp file for large sorts/joins.
    data_plane_max_file_bytes: int = Field(default=8 * 1024**3, ge=1024**2)
    # Aggregate DuckDB temp-spill budget per operation (max_temp_directory_size). DuckDB's own
    # default is 90% of free disk; RLIMIT_FSIZE alone only caps each individual file.
    data_plane_max_temp_bytes: int = Field(default=8 * 1024**3, ge=1024**2)
    data_plane_threads: int = Field(default=2, ge=1, le=64)
    data_plane_max_response_bytes: int = Field(default=64 * 1024**2, ge=1024**2)
    # Confinement of the data-plane worker (Landlock + seccomp). "auto": enforced on Linux,
    # failing closed when the kernel lacks either; not available on other platforms.
    # "required": fail closed on any host without it. "off": explicit opt-out.
    data_plane_sandbox: Literal["auto", "required", "off"] = "auto"
    execution_backend: Literal["docker", "subprocess_dev"] = "docker"
    docker_image: str = "agentic-analytics-exec:dev"
    docker_memory: str = limits.DOCKER_MEMORY
    docker_cpus: float = Field(default=limits.DOCKER_CPUS, gt=0, le=8)
    docker_pids_limit: int = Field(default=limits.DOCKER_PIDS_LIMIT, ge=16, le=4096)
    # Explicit non-root container identity ("uid:gid"). Defaults to the server's own identity on
    # POSIX hosts and to the image's unprivileged user elsewhere; uid 0 is always refused.
    docker_user: str | None = Field(default=None, pattern=r"^[0-9]+:[0-9]+$")
    # SEC-01 (#22): size of each run's tmpfs at /workspace/outputs, the only writable output
    # location. 256 MiB is inferred from test and quickstart outputs (CSV + PNG, a few MB),
    # not measured on real workloads; raise it for larger datasets or figures.
    docker_output_quota_bytes: int = Field(
        default=limits.DOCKER_OUTPUT_QUOTA_BYTES, ge=1024**2, le=64 * 1024**3
    )
    execution_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_execution_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_output_chars: int = Field(default=limits.MAX_OUTPUT_CHARS, ge=1024, le=1_000_000)
    max_artifacts_per_execution: int = Field(default=64, ge=1, le=10_000)
    max_artifact_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_total_artifact_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    # Budget for bytes/files one execution may add to its writable workspace, enforced while it
    # runs (not only at artifact registration), plus a host free-space reserve.
    max_execution_output_bytes: int = Field(default=limits.MAX_EXECUTION_OUTPUT_BYTES, ge=1024)
    max_execution_output_files: int = Field(default=limits.MAX_EXECUTION_OUTPUT_FILES, ge=1)
    min_free_disk_bytes: int = Field(default=limits.MIN_FREE_DISK_BYTES, ge=0)
    max_list_page_size: int = Field(default=200, ge=1, le=10_000)
    max_discovery_scan_entries: int = Field(default=100_000, ge=1)
    max_validation_findings: int = Field(default=200, ge=1, le=10_000)
    max_spill_rows: int = Field(default=1_000_000, ge=1)
    max_result_preview_bytes: int = Field(default=256 * 1024, ge=1024)
    max_result_cell_chars: int = Field(default=8192, ge=64)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_output_quota(self) -> Self:
        # tmpfs pages are charged to the container's memory cgroup, together with /tmp
        # (128 MiB) and /dev/shm (64 MiB). A quota near the memory limit would end in an OOM
        # kill instead of ENOSPC. A size that cannot be read cannot be checked, so it is refused.
        memory = _docker_bytes(self.docker_memory)
        if memory is None:
            raise ValueError(
                f"docker_memory {self.docker_memory!r} is not a size such as '1g', '512m' or "
                "'1.5g', so the output quota cannot be checked against it"
            )
        if self.docker_output_quota_bytes + _DOCKER_SCRATCH_TMPFS_BYTES > 0.8 * memory:
            raise ValueError(
                "docker_output_quota_bytes plus 192 MiB for /tmp and /dev/shm must stay below "
                "80% of docker_memory, because tmpfs pages are charged to container memory"
            )
        return self

    @model_validator(mode="after")
    def validate_data_plane_capacity(self) -> Self:
        # Reject configurations where the worker could never run: DuckDB's own memory limit
        # plus interpreter and thread overhead must fit inside the worker's address space.
        duckdb_bytes = parse_size(self.query_memory_limit)
        if duckdb_bytes is not None:
            required = (
                duckdb_bytes
                + _WORKER_BASE_BYTES
                + self.data_plane_threads * _WORKER_PER_THREAD_BYTES
            )
            if self.data_plane_memory_bytes < required:
                raise ValueError(
                    f"data_plane_memory_bytes ({self.data_plane_memory_bytes}) must be at least "
                    f"{required}: query_memory_limit plus worker and per-thread overhead"
                )
        # A spill must be able to reach its artifact size limit before the OS file cap stops it.
        if self.max_artifact_bytes > self.data_plane_max_file_bytes:
            raise ValueError("max_artifact_bytes must not exceed data_plane_max_file_bytes")
        return self

    @property
    def archive_dir(self) -> Path:
        return archive_dir(self.state_dir)

    @property
    def scratch_dir(self) -> Path:
        return scratch_dir(self.state_dir)

    @property
    def dataplane_scratch_dir(self) -> Path:
        return dataplane_scratch_dir(self.state_dir)

    def normalized_allowed_roots(self) -> list[Path]:
        return [root.expanduser().resolve(strict=False) for root in self.allowed_workspace_roots]

    def ensure_directories(self) -> None:
        # Server state (records, archived artifacts, request scripts) may contain sensitive
        # analytical data, so it is private to the server's user regardless of umask.
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.stat().st_uid == os.getuid():
            self.state_dir.chmod(0o700)
        self.workspace_base_dir.mkdir(parents=True, exist_ok=True)
