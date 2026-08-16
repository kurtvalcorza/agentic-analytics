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
    execution_backend: Literal["docker", "subprocess_dev"] = "docker"
    docker_image: str = "agentic-analytics-exec:dev"
    docker_memory: str = "1g"
    docker_cpus: float = Field(default=1.0, gt=0, le=8)
    docker_pids_limit: int = Field(default=128, ge=16, le=4096)
    execution_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_execution_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_output_chars: int = Field(default=65536, ge=1024, le=1_000_000)
    max_artifacts_per_execution: int = Field(default=64, ge=1, le=10_000)
    max_artifact_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_total_artifact_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_validation_findings: int = Field(default=200, ge=1, le=10_000)
    max_spill_rows: int = Field(default=1_000_000, ge=1)
    max_result_preview_bytes: int = Field(default=256 * 1024, ge=1024)
    max_result_cell_chars: int = Field(default=8192, ge=64)
    log_level: str = "INFO"

    def normalized_allowed_roots(self) -> list[Path]:
        return [root.expanduser().resolve(strict=False) for root in self.allowed_workspace_roots]

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_base_dir.mkdir(parents=True, exist_ok=True)
