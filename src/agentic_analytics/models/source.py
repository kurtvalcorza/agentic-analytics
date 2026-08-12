from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Record, utc_now


class DataSource(Record):
    id: str
    session_id: str
    kind: Literal["csv", "parquet", "json", "excel", "database", "other"]
    display_name: str
    relative_path: str | None = None
    uri: str | None = None
    read_only: bool = True
    fingerprint: dict[str, Any]
    schema_: list[dict[str, Any]] = Field(default_factory=list, alias="schema")
    row_count: int | None = None
    profile: dict[str, Any] = Field(default_factory=dict)
    registered_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def has_location(self) -> "DataSource":
        if not self.relative_path and not self.uri:
            raise ValueError("a source requires relative_path or uri")
        return self
