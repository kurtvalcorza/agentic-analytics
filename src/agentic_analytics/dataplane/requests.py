"""Typed requests from the server to the data-plane worker (C10).

One dataclass per operation is shared by both ends: the server builds it, the client turns it
into the JSON sent on the worker's stdin, and the worker decodes and validates it before
dispatch. Each request names the files it reads (:meth:`read_paths`) and the directories it
writes (:meth:`write_dirs`), and the sandbox grants are built from those methods alone, so a
new payload key can never widen or narrow what the worker may touch by accident.

Standard library only: the worker imports this before DuckDB, and it must stay importable in
the confined process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Self


class RequestError(ValueError):
    """A worker request is malformed: missing, unknown or mistyped fields."""


# --- strict field readers -------------------------------------------------------------------


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestError(f"{what} must be an object")
    return value


def _keys(
    data: dict[str, Any],
    what: str,
    required: set[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> None:
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise RequestError(f"{what} is missing {sorted(missing)}")
    if unknown:
        raise RequestError(f"{what} has unknown fields {sorted(unknown)}")


def _str(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise RequestError(f"{key} must be a string")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    value = data[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise RequestError(f"{key} must be an integer")


def _float(data: dict[str, Any], key: str) -> float:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"{key} must be a number")
    return float(value)


def _bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be a boolean")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    return None if data.get(key) is None else _int(data, key)


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    return None if data.get(key) is None else _str(data, key)


def _absolute(data: dict[str, Any], key: str) -> str:
    value = _str(data, key)
    if not os.path.isabs(value):
        raise RequestError(f"{key} must be an absolute path")
    return value


# --- shared parts ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineLimits:
    """DuckDB limits shared by every operation."""

    timeout_seconds: float
    memory_limit: str
    preview_bytes: int
    cell_chars: int
    threads: int | None = None
    temp_directory: str | None = None
    max_temp_bytes: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_limit": self.memory_limit,
            "preview_bytes": self.preview_bytes,
            "cell_chars": self.cell_chars,
            "threads": self.threads,
            "temp_directory": self.temp_directory,
            "max_temp_bytes": self.max_temp_bytes,
        }

    @classmethod
    def from_json(cls, value: Any) -> EngineLimits:
        data = _object(value, "limits")
        _keys(
            data,
            "limits",
            {"timeout_seconds", "memory_limit", "preview_bytes", "cell_chars"},
            {"threads", "temp_directory", "max_temp_bytes"},
        )
        return cls(
            timeout_seconds=_float(data, "timeout_seconds"),
            memory_limit=_str(data, "memory_limit"),
            preview_bytes=_int(data, "preview_bytes"),
            cell_chars=_int(data, "cell_chars"),
            threads=_optional_int(data, "threads"),
            temp_directory=_optional_str(data, "temp_directory"),
            max_temp_bytes=_optional_int(data, "max_temp_bytes"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceView:
    """One registered source exposed to a query as a temporary view."""

    view: str
    kind: str
    path: str

    def to_json(self) -> dict[str, Any]:
        return {"view": self.view, "kind": self.kind, "path": self.path}

    @classmethod
    def from_json(cls, value: Any) -> SourceView:
        data = _object(value, "source")
        _keys(data, "source", {"view", "kind", "path"})
        return cls(view=_str(data, "view"), kind=_str(data, "kind"), path=_absolute(data, "path"))


# --- operations -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryRequest:
    """Materialize, preview, and optionally spill one validated, rewritten query."""

    op: ClassVar[str] = "query"
    limits: EngineLimits
    sources: tuple[SourceView, ...]
    sql: str
    result_table: str
    limit: int
    spill_cap: int
    spill_path: str
    spill_byte_ceiling: int
    require_complete: bool = False

    def read_paths(self) -> tuple[str, ...]:
        return tuple(source.path for source in self.sources)

    def write_dirs(self) -> tuple[str, ...]:
        # The per-execution archive directory that receives the spill.
        return (os.path.dirname(self.spill_path),)

    def with_limits(self, limits: EngineLimits) -> Self:
        return replace(self, limits=limits)

    def to_json(self) -> dict[str, Any]:
        return {
            "limits": self.limits.to_json(),
            "sources": [source.to_json() for source in self.sources],
            "sql": self.sql,
            "result_table": self.result_table,
            "limit": self.limit,
            "spill_cap": self.spill_cap,
            "spill_path": self.spill_path,
            "spill_byte_ceiling": self.spill_byte_ceiling,
            "require_complete": self.require_complete,
        }

    @classmethod
    def from_json(cls, value: Any) -> QueryRequest:
        data = _object(value, "query request")
        _keys(
            data,
            "query request",
            {
                "limits",
                "sources",
                "sql",
                "result_table",
                "limit",
                "spill_cap",
                "spill_path",
                "spill_byte_ceiling",
            },
            {"require_complete"},
        )
        sources = data["sources"]
        if not isinstance(sources, list):
            raise RequestError("sources must be a list")
        return cls(
            limits=EngineLimits.from_json(data["limits"]),
            sources=tuple(SourceView.from_json(item) for item in sources),
            sql=_str(data, "sql"),
            result_table=_str(data, "result_table"),
            limit=_int(data, "limit"),
            spill_cap=_int(data, "spill_cap"),
            spill_path=_absolute(data, "spill_path"),
            spill_byte_ceiling=_int(data, "spill_byte_ceiling"),
            require_complete=_bool(data, "require_complete"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileRequest:
    """Schema, row/null/duplicate counts, and a bounded sample of one source."""

    op: ClassVar[str] = "profile"
    limits: EngineLimits
    path: str
    kind: str
    sample_limit: int
    max_profile_columns: int

    def read_paths(self) -> tuple[str, ...]:
        return (self.path,)

    def write_dirs(self) -> tuple[str, ...]:
        return ()

    def with_limits(self, limits: EngineLimits) -> Self:
        return replace(self, limits=limits)

    def to_json(self) -> dict[str, Any]:
        return {
            "limits": self.limits.to_json(),
            "path": self.path,
            "kind": self.kind,
            "sample_limit": self.sample_limit,
            "max_profile_columns": self.max_profile_columns,
        }

    @classmethod
    def from_json(cls, value: Any) -> ProfileRequest:
        data = _object(value, "profile request")
        _keys(
            data,
            "profile request",
            {"limits", "path", "kind", "sample_limit", "max_profile_columns"},
        )
        return cls(
            limits=EngineLimits.from_json(data["limits"]),
            path=_absolute(data, "path"),
            kind=_str(data, "kind"),
            sample_limit=_int(data, "sample_limit"),
            max_profile_columns=_int(data, "max_profile_columns"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TableStatsRequest:
    """Counts used by the duplicate and missingness validators."""

    op: ClassVar[str] = "table_stats"
    limits: EngineLimits
    path: str
    kind: str
    null_counts: bool = False
    distinct: bool = False
    duplicate_keys: tuple[str, ...] = field(default=())

    def read_paths(self) -> tuple[str, ...]:
        return (self.path,)

    def write_dirs(self) -> tuple[str, ...]:
        return ()

    def with_limits(self, limits: EngineLimits) -> Self:
        return replace(self, limits=limits)

    def to_json(self) -> dict[str, Any]:
        return {
            "limits": self.limits.to_json(),
            "path": self.path,
            "kind": self.kind,
            "null_counts": self.null_counts,
            "distinct": self.distinct,
            "duplicate_keys": list(self.duplicate_keys),
        }

    @classmethod
    def from_json(cls, value: Any) -> TableStatsRequest:
        data = _object(value, "table_stats request")
        _keys(
            data,
            "table_stats request",
            {"limits", "path", "kind"},
            {"null_counts", "distinct", "duplicate_keys"},
        )
        keys = data.get("duplicate_keys", [])
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise RequestError("duplicate_keys must be a list of strings")
        return cls(
            limits=EngineLimits.from_json(data["limits"]),
            path=_absolute(data, "path"),
            kind=_str(data, "kind"),
            null_counts=_bool(data, "null_counts"),
            distinct=_bool(data, "distinct"),
            duplicate_keys=tuple(keys),
        )


WorkerRequest = QueryRequest | ProfileRequest | TableStatsRequest

REQUEST_TYPES: dict[str, type[QueryRequest] | type[ProfileRequest] | type[TableStatsRequest]] = {
    QueryRequest.op: QueryRequest,
    ProfileRequest.op: ProfileRequest,
    TableStatsRequest.op: TableStatsRequest,
}


def decode_request(op: Any, payload: Any) -> WorkerRequest:
    """Validate a decoded JSON payload into the typed request for ``op``."""

    if not isinstance(op, str) or op not in REQUEST_TYPES:
        raise RequestError(f"unknown operation: {op!r}")
    return REQUEST_TYPES[op].from_json(payload)
