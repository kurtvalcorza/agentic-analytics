from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from agentic_analytics.ids import PROTOCOL_VERSION, EntityType, is_canonical_id, new_id

from .common import CanonicalModel, utc_now


class SessionMode(StrEnum):
    STRICT = "strict"
    PERMISSIVE = "permissive"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AnalysisSession(CanonicalModel):
    id: str = Field(default_factory=lambda: new_id(EntityType.SESSION))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    workspace_root: str
    mode: SessionMode = SessionMode.STRICT
    status: SessionStatus = SessionStatus.ACTIVE
    protocol_version: str = PROTOCOL_VERSION
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not is_canonical_id(value, EntityType.SESSION):
            raise ValueError("session id must use the ses_ canonical ID format")
        return value

    @field_validator("workspace_root")
    @classmethod
    def normalize_workspace_root(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workspace_root must not be empty")
        return str(Path(value).expanduser().resolve(strict=False))
