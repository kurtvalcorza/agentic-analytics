"""SQL-side cell limits for model-facing query and inspection previews.

Runs inside the confined data-plane worker; imports nothing from the server's service layer.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

# Match the whole type: VARCHAR[] and INTEGER[4] are nested types, not small scalars.
_SMALL_SCALAR = re.compile(
    r"(?:BOOLEAN|BOOL|U?TINYINT|U?SMALLINT|U?INTEGER|U?BIGINT|U?HUGEINT|"
    r"FLOAT|DOUBLE|REAL|(?:DECIMAL|NUMERIC)(?:\(\d+,\s*\d+\))?|DATE|"
    r"TIME(?: WITH TIME ZONE)?|TIMESTAMP(?:_[SMN]S| WITH TIME ZONE)?|INTERVAL|UUID)"
)
_TEXT = {"VARCHAR", "CHAR", "BPCHAR", "TEXT", "STRING"}
_BLOB = {"BLOB", "BYTEA", "VARBINARY"}


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def cell_bound_expr(name: str, sql_type: str, cell_cap: int) -> str:
    """Bound strings, blobs, and nested values before crossing the Python boundary."""
    ident = sql_identifier(name)
    upper = sql_type.upper()
    if _SMALL_SCALAR.fullmatch(upper):
        return ident
    limit = cell_cap + 1  # retain a sentinel character for truncation detection
    if upper in _BLOB:
        return f"{ident}[1:{limit}] AS {ident}"
    if upper in _TEXT:
        return f"substr({ident}, 1, {limit}) AS {ident}"
    return f"substr(CAST({ident} AS VARCHAR), 1, {limit}) AS {ident}"


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
