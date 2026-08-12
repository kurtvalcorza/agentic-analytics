from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .base import Record, utc_now


class ValidationFinding(Record):
    id: str
    session_id: str
    code: str
    severity: Literal["info", "warning", "error", "blocking"]
    status: Literal["open", "resolved", "accepted"] = "open"
    message: str
    entity_refs: list[dict[str, Any]] = Field(default_factory=list)
    check: str
    details: dict[str, Any] = Field(default_factory=dict)
    remediation: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ValidationRun(Record):
    id: str
    session_id: str
    status: Literal["validated", "warnings", "blocked"]
    checks_run: list[str]
    checks_skipped: list[str] = Field(default_factory=list)
    checks_inconclusive: list[str] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)
