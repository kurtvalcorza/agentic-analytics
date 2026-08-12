from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from agentic_analytics.ids import EntityType, new_id

from .common import CanonicalModel, utc_now


class ValidationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


class ValidationFindingStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"


class ValidationRunStatus(StrEnum):
    VALIDATED = "validated"
    WARNINGS = "warnings"
    BLOCKED = "blocked"


class ValidationFinding(CanonicalModel):
    id: str = Field(default_factory=lambda: new_id(EntityType.VALIDATION))
    session_id: str
    code: str = Field(min_length=1)
    severity: ValidationSeverity
    status: ValidationFindingStatus = ValidationFindingStatus.OPEN
    message: str = Field(min_length=1)
    entity_refs: list[dict[str, str]] = Field(default_factory=list)
    check: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    remediation: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ValidationRun(CanonicalModel):
    id: str = Field(default_factory=lambda: new_id(EntityType.VALIDATION_RUN))
    session_id: str
    status: ValidationRunStatus
    finding_ids: list[str] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    checks_skipped: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
