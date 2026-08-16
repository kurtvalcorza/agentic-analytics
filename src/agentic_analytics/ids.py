from __future__ import annotations

import re
from enum import StrEnum
from uuid import uuid4

PROTOCOL_VERSION = "0.1.0"


class EntityType(StrEnum):
    SESSION = "ses"
    SOURCE = "src"
    EXECUTION = "exe"
    ARTIFACT = "art"
    EVIDENCE = "evd"
    VALIDATION = "val"
    REPORT = "rpt"
    VALIDATION_RUN = "vrn"


_ID_RE = re.compile(r"^(ses|src|exe|art|evd|val|rpt|vrn)_[0-9a-f]{32}$")


def new_id(entity_type: EntityType | str) -> str:
    prefix = EntityType(entity_type).value
    return f"{prefix}_{uuid4().hex}"


def is_canonical_id(value: str, expected: EntityType | str | None = None) -> bool:
    if not _ID_RE.fullmatch(value):
        return False
    if expected is None:
        return True
    return value.startswith(f"{EntityType(expected).value}_")
