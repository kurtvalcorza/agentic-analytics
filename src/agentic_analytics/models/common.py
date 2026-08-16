from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Require a timezone-aware timestamp and normalize it to UTC.

    Canonical records are RFC 3339 ``date-time`` values in UTC. Naive datetimes (or any
    value whose offset cannot be determined) are rejected so persisted timestamps never
    serialize without a ``Z``/offset and never mix aware and naive values.
    """

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("timestamp must be timezone-aware with a UTC offset")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]


class CanonicalModel(BaseModel):
    """Base for persisted protocol records."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )
