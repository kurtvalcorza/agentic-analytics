"""DuckDB operations executed inside the isolated data-plane worker process.

Nothing here touches server state: callers pass already-authorized absolute file paths and
receive plain JSON-serializable results. The MCP server never imports this module at runtime;
it runs in :mod:`agentic_analytics.dataplane.worker`, a separate, resource-limited process.
Tests may call these functions in-process to inspect intermediate behavior.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from agentic_analytics.services.preview import cell_bound_expr, json_value

# The preview is streamed one row at a time so no more than a single (already cell-capped) row
# is ever materialized in Python before the byte budget is re-checked.
_MIN_PREVIEW_CELL_CHARS = 16
# Absolute cap on previewed columns so a result with an extreme column count cannot produce
# unbounded column metadata or a row that no per-cell cap can shrink under the budget.
_MAX_PREVIEW_COLUMNS = 512
# Worst-case JSON bytes a pass-through (non-string) scalar cell can contribute; used to derive a
# column count that keeps even an all-scalar row within the byte budget.
_MAX_SCALAR_BYTES_PER_COL = 64
# Cap on the length of each returned column name so long aliases cannot inflate the response.
_MAX_COLUMN_NAME_CHARS = 256
# How often the spill watchdog samples the growing Parquet file to bound bytes written to disk.
_SPILL_POLL_SECONDS = 0.02


class SpillLimitError(RuntimeError):
    """The spilled result crossed its byte ceiling and was interrupted."""


class OutputWriteError(RuntimeError):
    """Writing an output (the spilled result) failed: disk full, file cap, or similar.

    Raised in place of DuckDB's IOException for the write phase so an output/resource failure
    is never reported as an unreadable source.
    """


@dataclass(frozen=True, slots=True)
class EngineLimits:
    timeout_seconds: float
    memory_limit: str
    preview_bytes: int
    cell_chars: int
    threads: int | None = None
    temp_directory: str | None = None
    max_temp_bytes: int | None = None

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> EngineLimits:
        return cls(
            timeout_seconds=float(value["timeout_seconds"]),
            memory_limit=str(value["memory_limit"]),
            preview_bytes=int(value["preview_bytes"]),
            cell_chars=int(value["cell_chars"]),
            threads=None if value.get("threads") is None else int(value["threads"]),
            temp_directory=value.get("temp_directory"),
            max_temp_bytes=(
                None if value.get("max_temp_bytes") is None else int(value["max_temp_bytes"])
            ),
        )


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def reader_sql(kind: str, path: str) -> str:
    literal = sql_string(path)
    if kind == "csv":
        return f"read_csv({literal}, strict_mode = true)"
    if kind == "parquet":
        return f"read_parquet({literal})"
    raise ValueError(f"unsupported source kind: {kind}")


def secure_connection(
    connection: duckdb.DuckDBPyConnection, allowed_paths: list[str], limits: EngineLimits
) -> None:
    """Confine DuckDB to the listed files and lock its configuration."""

    # Engine-internal locations must be set before external access is disabled, which freezes
    # them. The temp directory is the worker's private scratch directory.
    if limits.temp_directory is not None:
        connection.execute(f"SET temp_directory = {sql_string(limits.temp_directory)}")
    if limits.max_temp_bytes is not None:
        # Aggregate cap across every temp file of this operation (RLIMIT_FSIZE is per file).
        connection.execute(f"SET max_temp_directory_size = '{int(limits.max_temp_bytes)}B'")
    if limits.threads is not None:
        connection.execute(f"SET threads = {int(limits.threads)}")
    allowed = ", ".join(sql_string(path) for path in allowed_paths)
    connection.execute(f"SET allowed_paths = [{allowed}]")
    connection.execute("SET autoinstall_known_extensions = false")
    connection.execute("SET autoload_known_extensions = false")
    connection.execute("SET allow_community_extensions = false")
    connection.execute("SET enable_external_access = false")
    # Bound memory before locking configuration so a single query cannot exhaust the worker
    # before its OS address-space limit is reached.
    connection.execute(f"SET memory_limit = {sql_string(limits.memory_limit)}")
    connection.execute("SET lock_configuration = true")


@contextmanager
def deadline(connection: duckdb.DuckDBPyConnection, seconds: float) -> Iterator[threading.Event]:
    """Interrupt ``connection`` after ``seconds``; the yielded event records that it fired."""

    fired = threading.Event()

    def _interrupt() -> None:
        fired.set()
        connection.interrupt()

    timer = threading.Timer(seconds, _interrupt)
    timer.start()
    try:
        yield fired
    except duckdb.Error as exc:
        if fired.is_set():
            raise TimeoutError("query exceeded the configured execution time limit") from exc
        raise
    finally:
        timer.cancel()


def scalar_int(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    return int(row[0]) if row else 0


# --- managed SQL ------------------------------------------------------------------------------


def preview_projection(
    connection: duckdb.DuckDBPyConnection, result_table: str, limits: EngineLimits
) -> tuple[str, int, bool]:
    """Build a SELECT list that caps each column's cell size in SQL for the preview.

    Two bounds keep a single row within ``preview_bytes``:

    * The number of previewed columns is capped so even an all-scalar row (whose cells pass
      through the projection uncapped) cannot exceed the budget through column count alone,
      and column metadata stays bounded.
    * The per-cell cap is the smaller of the configured cell budget and an even share of the
      byte budget across the previewed columns, bounding the string/blob/nested cells.

    Returns the projection, the effective per-cell cap, and whether columns were dropped.
    """

    described = connection.execute(f'DESCRIBE "{result_table}"').fetchall()
    budget = limits.preview_bytes
    column_budget = max(1, budget // _MAX_SCALAR_BYTES_PER_COL)
    column_limit = min(len(described), _MAX_PREVIEW_COLUMNS, column_budget)
    shown = described[:column_limit]
    column_truncated = len(shown) < len(described)
    column_count = max(1, len(shown))
    # Halve the per-column share to leave headroom for JSON structure (quotes, commas).
    row_share = budget // column_count // 2
    cell_cap = min(limits.cell_chars, max(_MIN_PREVIEW_CELL_CHARS, row_share))
    projection = ", ".join(cell_bound_expr(str(row[0]), str(row[1]), cell_cap) for row in shown)
    return projection, cell_cap, column_truncated


def _row_bytes(row: list[Any]) -> int:
    return len(json.dumps(row, default=str).encode("utf-8"))


def _shrink_row(row: list[Any], budget: int) -> list[Any]:
    """Trim a single row's string cells until it fits the byte budget.

    Used only to guarantee a non-empty preview: when even the first streamed row exceeds the
    whole budget, its largest string cells are halved repeatedly so at least a bounded slice
    is returned instead of an empty preview or an over-budget row.
    """

    shrunk = list(row)
    while _row_bytes(shrunk) > budget:
        index = max(
            (i for i, value in enumerate(shrunk) if isinstance(value, str) and value),
            key=lambda i: len(shrunk[i]),
            default=None,
        )
        if index is None:
            break  # nothing left to trim (only small non-string cells remain)
        shrunk[index] = shrunk[index][: len(shrunk[index]) // 2]
    return shrunk


def fetch_preview_bounded(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    limit: int,
    cell_cap: int,
    limits: EngineLimits,
) -> tuple[list[str], list[list[Any]], bool]:
    """Stream a preview whose cells were already SQL-capped, bounding total bytes as well.

    Rows are pulled one at a time under the wall-clock interrupt so at most a single (already
    cell-capped) row is materialized before the byte budget is re-checked. The budget bounds
    every row including the first, so the preview never exceeds ``preview_bytes``. Returns
    (columns, rows, truncated) where ``truncated`` marks a preview that dropped rows/cells the
    full artifact still carries.
    """

    budget = limits.preview_bytes
    with deadline(connection, limits.timeout_seconds):
        cursor = connection.execute(sql)
        columns = [str(item[0])[:_MAX_COLUMN_NAME_CHARS] for item in (cursor.description or [])]
        kept: list[list[Any]] = []
        used = 0
        truncated = False
        while len(kept) < limit:
            raw = cursor.fetchone()
            if raw is None:
                break
            row: list[Any] = []
            for value in (json_value(item) for item in raw):
                # A cell at the SQL cap length means its source value was longer and was
                # trimmed for the preview; flag it so the full value is preserved by spilling.
                if isinstance(value, str) and len(value) > cell_cap:
                    value = value[:cell_cap]
                    truncated = True
                row.append(value)
            size = _row_bytes(row)
            if used + size > budget:
                truncated = True
                if not kept:
                    # Guarantee at least one bounded row rather than an empty preview.
                    kept.append(_shrink_row(row, budget))
                break
            kept.append(row)
            used += size
        return columns, kept, truncated


def write_spill_bounded(
    connection: duckdb.DuckDBPyConnection,
    copy_sql: str,
    spill_path: Path,
    byte_ceiling: int,
    limits: EngineLimits,
) -> None:
    """Run the spill ``COPY`` under both a time and a bytes-written bound.

    A watchdog thread samples the growing Parquet file and interrupts the write once it crosses
    ``byte_ceiling``. The worker's ``RLIMIT_FSIZE`` is a hard backstop beneath this. A final
    exact check catches a small over-ceiling file that completed between samples.
    """

    over_limit = threading.Event()
    stop_watch = threading.Event()

    def _watch_size() -> None:
        while not stop_watch.wait(_SPILL_POLL_SECONDS):
            try:
                if spill_path.stat().st_size > byte_ceiling:
                    over_limit.set()
                    connection.interrupt()
                    return
            except FileNotFoundError:
                continue

    watcher = threading.Thread(target=_watch_size, daemon=True)
    watcher.start()
    try:
        with deadline(connection, limits.timeout_seconds):
            connection.execute(copy_sql)
    except (duckdb.Error, TimeoutError) as exc:
        if over_limit.is_set():
            raise SpillLimitError(
                f"spill exceeded the artifact byte limit of {byte_ceiling}"
            ) from exc
        if isinstance(exc, duckdb.IOException):
            raise OutputWriteError("writing the spilled result failed") from exc
        raise
    finally:
        stop_watch.set()
        watcher.join(timeout=1)
    if spill_path.exists() and spill_path.stat().st_size > byte_ceiling:
        raise SpillLimitError(
            f"spill is {spill_path.stat().st_size} bytes; artifact limit is {byte_ceiling}"
        )


def run_query(request: dict[str, Any]) -> dict[str, Any]:
    """Materialize, preview, and optionally spill one already-validated, rewritten query."""

    limits = EngineLimits.from_json(request["limits"])
    sources: list[dict[str, str]] = request["sources"]
    spill_path = Path(request["spill_path"])
    result_table = request["result_table"]
    limit = int(request["limit"])
    spill_cap = int(request["spill_cap"])
    connection = duckdb.connect(database=":memory:")
    try:
        secure_connection(
            connection, [item["path"] for item in sources] + [str(spill_path)], limits
        )
        for item in sources:
            connection.execute(
                f'CREATE TEMP VIEW "{item["view"]}" AS SELECT * FROM '
                f"{reader_sql(item['kind'], item['path'])}"
            )
        # Materialize the full result ONCE, bounded by a row cap and the interrupt timer, so the
        # preview and the spilled artifact are the same evaluation (deterministic even for
        # nondeterministic SQL) and materialization cannot run unbounded.
        with deadline(connection, limits.timeout_seconds):
            connection.execute(
                f'CREATE TEMP TABLE "{result_table}" AS '
                f"SELECT * FROM ({request['sql']}) AS _q LIMIT {spill_cap + 1}"
            )
            materialized = scalar_int(connection, f'SELECT count(*) FROM "{result_table}"')
        projection, cell_cap, column_truncated = preview_projection(
            connection, result_table, limits
        )
        columns, rows, preview_truncated = fetch_preview_bounded(
            connection,
            f'SELECT {projection} FROM "{result_table}" LIMIT {limit + 1}',
            limit,
            cell_cap,
            limits,
        )
        truncated = materialized > limit or preview_truncated or column_truncated
        spill_capped = materialized > spill_cap
        spilled = False
        if truncated and not (spill_capped and request.get("require_complete")):
            # The table holds spill_cap + 1 rows only to detect overflow; the artifact carries
            # exactly the advertised row budget.
            write_spill_bounded(
                connection,
                f'COPY (SELECT * FROM "{result_table}" LIMIT {spill_cap}) '
                f"TO {sql_string(str(spill_path))} (FORMAT PARQUET, COMPRESSION ZSTD)",
                spill_path,
                int(request["spill_byte_ceiling"]),
                limits,
            )
            spilled = True
        return {
            "columns": columns,
            "rows": rows,
            "materialized": materialized,
            "truncated": truncated,
            "spill_capped": spill_capped,
            "spilled": spilled,
        }
    finally:
        connection.close()


# --- source inspection --------------------------------------------------------------------


def _preview_schema(
    schema: list[dict[str, Any]], null_counts: dict[str, int], budget: int
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    """Bound response metadata while leaving the canonical source schema intact."""
    shown: list[dict[str, Any]] = []
    shown_nulls: dict[str, int] = {}
    column_limit = min(512, budget // 64)
    for column in schema[:column_limit]:
        candidate = {**column, "type": column["type"][:256]}
        next_nulls = dict(shown_nulls)
        if column["name"] in null_counts:
            next_nulls[column["name"]] = null_counts[column["name"]]
        metadata = {"schema": [*shown, candidate], "null_counts": next_nulls}
        if len(json.dumps(metadata).encode("utf-8")) > budget:
            break
        shown.append(candidate)
        shown_nulls = next_nulls
    return shown, shown_nulls, shown != schema


def _sample(
    connection: duckdb.DuckDBPyConnection,
    relation: str,
    schema: list[dict[str, Any]],
    sample_limit: int,
    limits: EngineLimits,
) -> tuple[list[dict[str, Any]], bool]:
    if not schema:
        return [], True
    budget = limits.preview_bytes
    cell_cap = min(limits.cell_chars, max(16, budget // len(schema) // 2))
    projection = ", ".join(
        cell_bound_expr(column["name"], column["type"], cell_cap) for column in schema
    )
    cursor = connection.execute(f"SELECT {projection} FROM {relation} LIMIT {sample_limit}")
    sample: list[dict[str, Any]] = []
    truncated = False
    while (raw := cursor.fetchone()) is not None:
        row = {}
        for column, value in zip(schema, raw, strict=True):
            value = json_value(value)
            if isinstance(value, str) and len(value) > cell_cap:
                value = value[:cell_cap]
                truncated = True
            row[column["name"]] = value
        if len(json.dumps([*sample, row]).encode("utf-8")) > budget:
            truncated = True
            if sample:
                break
            # The first row may need a further byte cap (UTF-8/JSON escaping and keys).
            while len(json.dumps([row]).encode("utf-8")) > budget:
                key = max(
                    (key for key, value in row.items() if isinstance(value, str) and value),
                    key=lambda key: len(row[key]),
                    default=None,
                )
                if key is None:
                    return [], True
                row[key] = row[key][: len(row[key]) // 2]
        sample.append(row)
    return sample, truncated


def profile_source(request: dict[str, Any]) -> dict[str, Any]:
    """Schema, row/null/duplicate counts, and a bounded sample, in as few scans as possible."""

    limits = EngineLimits.from_json(request["limits"])
    path = request["path"]
    relation = reader_sql(request["kind"], path)
    sample_limit = int(request["sample_limit"])
    max_profile_columns = int(request["max_profile_columns"])
    connection = duckdb.connect(database=":memory:")
    try:
        secure_connection(connection, [path], limits)
        with deadline(connection, limits.timeout_seconds):
            cursor = connection.execute(f"SELECT * FROM {relation} LIMIT 0")
            schema = [
                {"name": str(column[0]), "type": str(column[1]), "nullable": True}
                for column in (cursor.description or [])
            ]
            # Row count and every profiled column's null count in a single scan.
            profiled = schema[:max_profile_columns]
            select_parts = ["count(*) AS __row_count"]
            for index, item in enumerate(profiled):
                select_parts.append(
                    f"count(*) FILTER (WHERE {sql_identifier(str(item['name']))} IS NULL) "
                    f"AS __null_{index}"
                )
            aggregate = connection.execute(
                f"SELECT {', '.join(select_parts)} FROM {relation}"
            ).fetchone()
            row_count = int(aggregate[0]) if aggregate else 0
            null_counts: dict[str, int] = {
                str(item["name"]): int(aggregate[index + 1]) if aggregate else 0
                for index, item in enumerate(profiled)
            }
            distinct_count = scalar_int(
                connection, f"SELECT count(*) FROM (SELECT DISTINCT * FROM {relation})"
            )
            shown_schema, shown_nulls, metadata_truncated = _preview_schema(
                schema, null_counts, limits.preview_bytes
            )
            # Use canonical types in SQL; the displayed type itself may have been clipped.
            sample_schema = schema[: len(shown_schema)]
            sample, cell_truncated = _sample(
                connection, relation, sample_schema, sample_limit, limits
            )
        return {
            "schema": schema,
            "profile": {
                "schema": shown_schema,
                "row_count": row_count,
                "null_counts": shown_nulls,
                "duplicate_row_count": row_count - distinct_count,
                "sample": sample,
                "sample_truncated": (
                    row_count > len(sample) or cell_truncated or len(sample_schema) < len(schema)
                ),
                "profile_truncated": metadata_truncated or len(schema) > max_profile_columns,
            },
        }
    finally:
        connection.close()


# --- validation statistics ----------------------------------------------------------------


def table_stats(request: dict[str, Any]) -> dict[str, Any]:
    """Counts used by the duplicate and missingness validators, in bounded scans."""

    limits = EngineLimits.from_json(request["limits"])
    path = request["path"]
    relation = reader_sql(request["kind"], path)
    connection = duckdb.connect(database=":memory:")
    try:
        secure_connection(connection, [path], limits)
        with deadline(connection, limits.timeout_seconds):
            result: dict[str, Any] = {}
            cursor = connection.execute(f"SELECT * FROM {relation} LIMIT 0")
            columns = [str(item[0]) for item in cursor.description or []]
            result["columns"] = columns
            if request.get("null_counts"):
                parts = ["count(*)"] + [
                    f"count(*) FILTER (WHERE {sql_identifier(column)} IS NULL)"
                    for column in columns
                ]
                row = connection.execute(f"SELECT {', '.join(parts)} FROM {relation}").fetchone()
                values = [int(value) for value in row] if row else [0] * len(parts)
                result["total"] = values[0]
                result["null_counts"] = dict(zip(columns, values[1:], strict=True))
            else:
                result["total"] = scalar_int(connection, f"SELECT count(*) FROM {relation}")
            if request.get("distinct"):
                result["distinct"] = scalar_int(
                    connection, f"SELECT count(*) FROM (SELECT DISTINCT * FROM {relation})"
                )
            keys = [str(key) for key in request.get("duplicate_keys") or []]
            unknown = [key for key in keys if key not in columns]
            if unknown:
                # Report by name rather than letting DuckDB's binder error (which quotes the
                # SQL, including the absolute source path) reach the caller.
                result["unknown_keys"] = unknown
                keys = []
            if keys:
                quoted = ", ".join(sql_identifier(str(key)) for key in keys)
                result["duplicate_key_rows"] = scalar_int(
                    connection,
                    "SELECT coalesce(sum(n - 1), 0) FROM ("
                    f"SELECT count(*) AS n FROM {relation} GROUP BY {quoted} "
                    "HAVING count(*) > 1)",
                )
            return result
    finally:
        connection.close()


OPERATIONS = {
    "query": run_query,
    "profile": profile_source,
    "table_stats": table_stats,
}
