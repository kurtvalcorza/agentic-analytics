import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Filesystem authority is always resolved server-side."""

    model_config = SettingsConfigDict(env_prefix="AGENTIC_ANALYTICS_", extra="ignore")

    state_dir: Path = Field(default=Path(".agentic-analytics/state"))
    workspace_base_dir: Path = Field(default=Path(".agentic-analytics/workspaces"))
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
    data_plane_memory_bytes: int = Field(default=3 * 1024**3, ge=256 * 1024**2)
    data_plane_max_file_bytes: int = Field(default=2 * 1024**3, ge=1024**2)
    data_plane_threads: int = Field(default=2, ge=1, le=64)
    data_plane_max_response_bytes: int = Field(default=64 * 1024**2, ge=1024**2)
    execution_backend: Literal["docker", "subprocess_dev"] = "docker"
    docker_image: str = "agentic-analytics-exec:dev"
    docker_memory: str = "1g"
    docker_cpus: float = Field(default=1.0, gt=0, le=8)
    docker_pids_limit: int = Field(default=128, ge=16, le=4096)
    # Explicit non-root container identity ("uid:gid"). Defaults to the server's own identity on
    # POSIX hosts and to the image's unprivileged user elsewhere; uid 0 is always refused.
    docker_user: str | None = Field(default=None, pattern=r"^[0-9]+:[0-9]+$")
    execution_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_execution_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_output_chars: int = Field(default=65536, ge=1024, le=1_000_000)
    max_artifacts_per_execution: int = Field(default=64, ge=1, le=10_000)
    max_artifact_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_total_artifact_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    # Budget for bytes/files one execution may add to its writable workspace, enforced while it
    # runs (not only at artifact registration), plus a host free-space reserve.
    max_execution_output_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_execution_output_files: int = Field(default=10_000, ge=1)
    min_free_disk_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    max_list_page_size: int = Field(default=200, ge=1, le=10_000)
    max_discovery_scan_entries: int = Field(default=100_000, ge=1)
    max_validation_findings: int = Field(default=200, ge=1, le=10_000)
    max_spill_rows: int = Field(default=1_000_000, ge=1)
    max_result_preview_bytes: int = Field(default=256 * 1024, ge=1024)
    max_result_cell_chars: int = Field(default=8192, ge=64)
    log_level: str = "INFO"

    def normalized_allowed_roots(self) -> list[Path]:
        return [root.expanduser().resolve(strict=False) for root in self.allowed_workspace_roots]

    def ensure_directories(self) -> None:
        # Server state (records, archived artifacts, request scripts) may contain sensitive
        # analytical data, so it is private to the server's user regardless of umask.
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix" and self.state_dir.stat().st_uid == os.getuid():
            self.state_dir.chmod(0o700)
        self.workspace_base_dir.mkdir(parents=True, exist_ok=True)
