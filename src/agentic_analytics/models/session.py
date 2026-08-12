from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .base import Record, utc_now


class AnalysisSession(Record):
    id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    workspace_root: str
    mode: Literal["strict", "permissive"] = "strict"
    status: Literal["active", "completed", "cancelled", "failed"] = "active"
    protocol_version: str = "0.1.0"
    metadata: dict[str, Any] = Field(default_factory=dict)
