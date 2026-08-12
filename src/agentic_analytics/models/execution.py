from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .base import Record, utc_now


class ExecutionRecord(Record):
    id: str
    session_id: str
    execution_type: Literal["managed_python", "managed_sql", "semantic", "external"]
    status: Literal["pending", "running", "succeeded", "failed", "timed_out", "cancelled"]
    request: dict[str, Any]
    source_ids: list[str] = Field(default_factory=list)
    source_fingerprints: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    stdout_preview: str = ""
    stderr_preview: str = ""
    result_preview: Any = None
    truncated: bool = False
    artifact_ids: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
