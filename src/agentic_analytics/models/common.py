from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CanonicalModel(BaseModel):
    """Base for persisted protocol records."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
