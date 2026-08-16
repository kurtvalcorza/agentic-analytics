from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Filesystem authority is always resolved server-side."""

    model_config = SettingsConfigDict(env_prefix="AGENTIC_ANALYTICS_", extra="ignore")

    state_dir: Path = Field(default=Path(".agentic-analytics/state"))
    workspace_base_dir: Path = Field(default=Path(".agentic-analytics/workspaces"))
    log_level: str = "INFO"

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_base_dir.mkdir(parents=True, exist_ok=True)
