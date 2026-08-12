from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    return datetime.now(UTC)


class CanonicalModel(BaseModel):
    """Base for persisted protocol records."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )
