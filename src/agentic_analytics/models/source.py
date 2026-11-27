from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import Field, model_validator

from agentic_analytics.ids import EntityType, new_id

from .common import CanonicalModel, UtcDatetime, utc_now


class SourceKind(StrEnum):
    CSV = "csv"
    PARQUET = "parquet"
    JSON = "json"
    EXCEL = "excel"
    DATABASE = "database"
    OTHER = "other"


# The formats the data plane reads: workspace discovery, source inspection, the data-reading
# validators and the engine's reader dispatch all derive from this one map.
READABLE_SOURCE_SUFFIXES: Final[dict[str, SourceKind]] = {
    ".csv": SourceKind.CSV,
    ".parquet": SourceKind.PARQUET,
}
READABLE_SOURCE_KINDS: Final[frozenset[SourceKind]] = frozenset(READABLE_SOURCE_SUFFIXES.values())


class DataSource(CanonicalModel):
    id: str = Field(default_factory=lambda: new_id(EntityType.SOURCE))
    session_id: str
    kind: SourceKind
    display_name: str
    relative_path: str | None = None
    uri: str | None = None
    read_only: bool = True
    fingerprint: dict[str, Any]
    schema_: list[dict[str, Any]] = Field(default_factory=list, alias="schema")
    row_count: int | None = Field(default=None, ge=0)
    profile: dict[str, Any] = Field(default_factory=dict)
    registered_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_location(self) -> DataSource:
        if not self.relative_path and not self.uri:
            raise ValueError("at least one of relative_path or uri is required")
        return self
