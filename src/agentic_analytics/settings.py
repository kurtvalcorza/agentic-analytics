from pathlib import Path

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
    log_level: str = "INFO"

    def normalized_allowed_roots(self) -> list[Path]:
        return [root.expanduser().resolve(strict=False) for root in self.allowed_workspace_roots]

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_base_dir.mkdir(parents=True, exist_ok=True)
