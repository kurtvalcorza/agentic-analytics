from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from agentic_analytics.ids import EntityType, new_id

from .common import CanonicalModel, UtcDatetime, utc_now

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ArtifactKind(StrEnum):
    TABLE = "table"
    CHART = "chart"
    DATASET = "dataset"
    SCRIPT = "script"
    REPORT = "report"
    FILE = "file"
    OTHER = "other"


class Artifact(CanonicalModel):
    id: str = Field(default_factory=lambda: new_id(EntityType.ARTIFACT))
    session_id: str
    execution_id: str | None = None
    kind: ArtifactKind
    display_name: str
    relative_path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: UtcDatetime = Field(default_factory=utc_now)
    lineage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("relative_path")
    @classmethod
    def reject_absolute_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or normalized in {"", "."}:
            raise ValueError("artifact relative_path must be relative")
        # A Windows drive component (``C:/foo`` or drive-relative ``C:foo``) escapes the
        # artifact root because it resolves against a per-drive current directory.
        if _WINDOWS_DRIVE_RE.match(normalized):
            raise ValueError("artifact relative_path must not include a drive component")
        if ".." in normalized.split("/"):
            raise ValueError("artifact relative_path must not traverse parents")
        return normalized

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return normalized
