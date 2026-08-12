from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .base import Record, utc_now


class Artifact(Record):
    id: str
    session_id: str
    execution_id: str | None = None
    kind: Literal["table", "chart", "dataset", "script", "report", "file", "other"] = "file"
    display_name: str
    relative_path: str
    media_type: str = "application/octet-stream"
    size_bytes: int
    sha256: str
    created_at: datetime = Field(default_factory=utc_now)
    lineage: dict[str, Any] = Field(default_factory=dict)
