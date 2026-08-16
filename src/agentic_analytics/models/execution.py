from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from agentic_analytics.ids import EntityType, new_id

from .common import CanonicalModel, UtcDatetime, utc_now


class ExecutionType(StrEnum):
    MANAGED_PYTHON = "managed_python"
    MANAGED_SQL = "managed_sql"
    SEMANTIC = "semantic"
    EXTERNAL = "external"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CANCELLED,
        }


class ExecutionRecord(CanonicalModel):
    id: str = Field(default_factory=lambda: new_id(EntityType.EXECUTION))
    session_id: str
    execution_type: ExecutionType
    status: ExecutionStatus = ExecutionStatus.PENDING
    request: dict[str, Any]
    source_ids: list[str] = Field(default_factory=list)
    source_fingerprints: dict[str, Any] = Field(default_factory=dict)
    started_at: UtcDatetime = Field(default_factory=utc_now)
    completed_at: UtcDatetime | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    stdout_preview: str = ""
    stderr_preview: str = ""
    result_preview: Any | None = None
    truncated: bool = False
    artifact_ids: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_terminal_timestamp(self) -> ExecutionRecord:
        if self.status.terminal and self.completed_at is None:
            raise ValueError("terminal executions require completed_at")
        if not self.status.terminal and self.completed_at is not None:
            raise ValueError("non-terminal executions must not set completed_at")
        return self
