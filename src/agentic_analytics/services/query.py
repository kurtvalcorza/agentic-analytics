from __future__ import annotations

import contextlib
import json
import re
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.models import (
    AnalysisSession,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionType,
    SourceKind,
)
from agentic_analytics.repositories import ExecutionRepository, SourceRepository
from agentic_analytics.settings import Settings

from .artifact_registry import ArtifactLimitError, ArtifactRegistry
from .inspector import fingerprint_file
from .workspace import WorkspaceService

_SOURCE_REF = re.compile(r"source\(\s*['\"](?P<id>src_[0-9a-f]{32})['\"]\s*\)", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|copy|attach|detach|install|load|call|pragma|set|"
    r"export|import|vacuum|read_csv|read_csv_auto|read_parquet|parquet_scan|csv_scan|read_json|"
    r"read_text|read_blob|glob|sqlite_scan|postgres_scan|httpfs|duckdb_secrets|"
    # Catalog/configuration introspection can leak absolute host paths from view definitions.
    r"duckdb_views|duckdb_tables|duckdb_columns|duckdb_constraints|duckdb_databases|"
    r"duckdb_settings|duckdb_functions|duckdb_schemas|duckdb_temporary_files|duckdb_extensions|"
    r"information_schema|pg_catalog|sqlite_master|sqlite_temp_master)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_QUOTED_IDENT = re.compile(r'"(?:[^"]|"")*"')

# The preview is streamed one row at a time so no more than a single (already cell-capped) row
# is ever materialized in Python before the byte budget is re-checked.
_MIN_PREVIEW_CELL_CHARS = 16
# How often the spill watchdog samples the growing Parquet file to bound bytes written to disk.
_SPILL_POLL_SECONDS = 0.02

# Types whose values are inherently small; passed through the preview projection unchanged so
# their original JSON type is preserved (an int stays an int). Every other type is bounded in
# SQL before it is materialized in Python.
_SMALL_SCALAR_PREFIXES = (
    "BOOLEAN",
    "BOOL",
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
    "NUMERIC",
    "DATE",
    "TIME",
    "TIMESTAMP",
    "INTERVAL",
    "UUID",
)
_TEXT_PREFIXES = ("VARCHAR", "CHAR", "BPCHAR", "TEXT", "STRING")
_BLOB_PREFIXES = ("BLOB", "BYTEA", "VARBINARY")


def _cell_bound_expr(name: str, sql_type: str, cell_cap: int) -> str:
    """Return a projection expression that caps one column's cell size inside SQL.

    Bounding at the SQL layer keeps DuckDB from handing a multi-megabyte string/blob (or a
    large nested value) to Python via ``fetchall`` before any Python-side truncation runs, so
    the configured cell budget bounds server memory as well as the response payload.
    """

    ident = '"' + name.replace('"', '""') + '"'
    upper = sql_type.upper()
    if upper.startswith(_SMALL_SCALAR_PREFIXES):
        return ident
    limit = cell_cap + 1  # one extra unit so downstream truncation detection can fire
    if upper.startswith(_BLOB_PREFIXES):
        return f"{ident}[1:{limit}] AS {ident}"
    if upper.startswith(_TEXT_PREFIXES):
        return f"substr({ident}, 1, {limit}) AS {ident}"
    # Nested/other types (LIST, STRUCT, MAP, JSON, ENUM, ...) may be arbitrarily large; render a
    # bounded textual preview. The full-fidelity value remains in the spilled artifact.
    return f"substr(CAST({ident} AS VARCHAR), 1, {limit}) AS {ident}"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _executable_text(sql: str) -> str:
    """Strip string literals and quoted identifiers so the denylist matches only executable SQL.

    This keeps benign data values and quoted column names (for example ``WHERE op = 'update'``)
    from being rejected as commands while still catching real forbidden keywords/functions.
    """

    without_strings = _STRING_LITERAL.sub(" ", sql)
    return _QUOTED_IDENT.sub(" ", without_strings)


class QueryRejected(ValueError):
    pass


class QueryExecutionError(RuntimeError):
    pass


class QueryService:
    def __init__(
        self,
        sources: SourceRepository,
        executions: ExecutionRepository,
        workspace: WorkspaceService,
        artifacts: ArtifactRegistry,
        settings: Settings,
    ) -> None:
        self.sources = sources
        self.executions = executions
        self.workspace = workspace
        self.artifacts = artifacts
        self.settings = settings

    @staticmethod
    def _validate_sql(sql: str) -> str:
        normalized = sql.strip()
        if normalized.endswith(";"):
            normalized = normalized[:-1].rstrip()
        if not normalized or not _ALLOWED_START.search(normalized):
            raise QueryRejected("only SELECT or WITH analytical queries are allowed")
        executable = _executable_text(normalized)
        if ";" in executable:
            raise QueryRejected("multiple SQL statements are not allowed")
        if _FORBIDDEN.search(executable):
            raise QueryRejected("query contains a forbidden command or external access function")
        return normalized

    def _secure_connection(
        self, connection: duckdb.DuckDBPyConnection, paths: list[str]
    ) -> None:
        allowed_paths = ", ".join(_sql_string(path) for path in paths)
        connection.execute(f"SET allowed_paths = [{allowed_paths}]")
        connection.execute("SET autoinstall_known_extensions = false")
        connection.execute("SET autoload_known_extensions = false")
        connection.execute("SET allow_community_extensions = false")
        connection.execute("SET enable_external_access = false")
        # Bound memory before locking configuration so a single query cannot exhaust the host.
        connection.execute(f"SET memory_limit = {_sql_string(self.settings.query_memory_limit)}")
        connection.execute("SET lock_configuration = true")

    @staticmethod
    def _prepare_spill_path(archive_base: Path) -> Path:
        """Create the archive directory and return the validated spill file path.

        Resolves and checks the created directory stays inside the archive root, rejecting a
        symlinked component before anything is written through it.
        """

        base = archive_base
        base.mkdir(parents=True, exist_ok=True)
        resolved_base = base.resolve(strict=True)
        # archive_base is ``<archive_root>/<session>/<execution>``; the resolved directory must
        # remain under the archive root (its parents[1]).
        archive_root = archive_base.parent.parent.resolve(strict=False)
        if resolved_base != archive_root and archive_root not in resolved_base.parents:
            raise QueryRejected("spill directory escapes the artifact archive root")
        return resolved_base / "query-result.parquet"

    def _preview_projection(
        self, connection: duckdb.DuckDBPyConnection, result_table: str
    ) -> tuple[str, int]:
        """Build a SELECT list that caps each column's cell size in SQL for the preview.

        The per-cell cap is the smaller of the configured cell budget and an even share of the
        preview byte budget across the columns, so a single fully populated row cannot exceed
        the preview budget even when every column is individually large. Returns the projection
        and the effective per-cell cap so the streaming reader can apply the same bound.
        """

        described = connection.execute(f'DESCRIBE "{result_table}"').fetchall()
        column_count = max(1, len(described))
        # Halve the per-column share to leave headroom for JSON structure (quotes, commas).
        row_share = self.settings.max_result_preview_bytes // column_count // 2
        cell_cap = min(
            self.settings.max_result_cell_chars,
            max(_MIN_PREVIEW_CELL_CHARS, row_share),
        )
        projection = ", ".join(
            _cell_bound_expr(str(row[0]), str(row[1]), cell_cap) for row in described
        )
        return projection, cell_cap

    @staticmethod
    def _row_bytes(row: list[Any]) -> int:
        return len(json.dumps(row, default=str).encode("utf-8"))

    def _shrink_row(self, row: list[Any], budget: int) -> list[Any]:
        """Trim a single row's string cells until it fits the byte budget.

        Used only to guarantee a non-empty preview: when even the first streamed row exceeds the
        whole budget, its largest string cells are halved repeatedly so at least a bounded slice
        is returned instead of an empty preview or an over-budget row.
        """

        shrunk = list(row)
        while self._row_bytes(shrunk) > budget:
            index = max(
                (i for i, value in enumerate(shrunk) if isinstance(value, str) and value),
                key=lambda i: len(shrunk[i]),
                default=None,
            )
            if index is None:
                break  # nothing left to trim (only small non-string cells remain)
            shrunk[index] = shrunk[index][: len(shrunk[index]) // 2]
        return shrunk

    def _fetch_preview_bounded(
        self, connection: duckdb.DuckDBPyConnection, sql: str, limit: int, cell_cap: int
    ) -> tuple[list[str], list[list[Any]], bool]:
        """Stream a preview whose cells were already SQL-capped, bounding total bytes as well.

        Rows are pulled one at a time under the wall-clock interrupt so at most a single
        (already cell-capped) row is materialized before the byte budget is re-checked. The
        budget bounds every row including the first — an oversized first row is shrunk to fit
        rather than kept whole — so the preview can never exceed ``max_result_preview_bytes`` in
        server memory. Returns (columns, rows, truncated) where ``truncated`` marks a preview
        that dropped rows/cells the full artifact still carries.
        """

        budget = self.settings.max_result_preview_bytes
        timed_out = threading.Event()

        def _interrupt() -> None:
            timed_out.set()
            connection.interrupt()

        timer = threading.Timer(self.settings.query_timeout_seconds, _interrupt)
        timer.start()
        try:
            cursor = connection.execute(sql)
            columns = [str(item[0]) for item in (cursor.description or [])]
            kept: list[list[Any]] = []
            used = 0
            truncated = False
            while len(kept) < limit:
                raw = cursor.fetchone()
                if raw is None:
                    break
                row: list[Any] = []
                for value in (_json_value(item) for item in raw):
                    # A cell at the SQL cap length means its source value was longer and was
                    # trimmed for the preview; flag it so the full value is preserved by spilling.
                    # The trim is also a defensive backstop for a type the projection passed
                    # through.
                    if isinstance(value, str) and len(value) > cell_cap:
                        value = value[:cell_cap]
                        truncated = True
                    row.append(value)
                size = self._row_bytes(row)
                if used + size > budget:
                    truncated = True
                    if not kept:
                        # Guarantee at least one bounded row rather than an empty preview.
                        kept.append(self._shrink_row(row, budget))
                    break
                kept.append(row)
                used += size
            return columns, kept, truncated
        except duckdb.Error as exc:
            if timed_out.is_set():
                raise TimeoutError("query exceeded the configured execution time limit") from exc
            raise
        finally:
            timer.cancel()

    @staticmethod
    def _scalar_int(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
        row = connection.execute(sql).fetchone()
        return int(row[0]) if row else 0

    def _run_bounded(
        self, connection: duckdb.DuckDBPyConnection, sql: str
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Execute ``sql`` with a wall-clock interrupt so runaway queries cannot hang the server."""

        timed_out = threading.Event()

        def _interrupt() -> None:
            timed_out.set()
            connection.interrupt()

        timer = threading.Timer(self.settings.query_timeout_seconds, _interrupt)
        timer.start()
        try:
            cursor = connection.execute(sql)
            columns = [str(item[0]) for item in (cursor.description or [])]
            rows = cursor.fetchall()
            return columns, rows
        except duckdb.Error as exc:
            if timed_out.is_set():
                raise TimeoutError("query exceeded the configured execution time limit") from exc
            raise
        finally:
            timer.cancel()

    def _write_spill_bounded(
        self,
        connection: duckdb.DuckDBPyConnection,
        copy_sql: str,
        spill_path: Path,
        byte_ceiling: int,
    ) -> None:
        """Run the spill ``COPY`` under both a time and a bytes-written bound.

        A watchdog thread samples the growing Parquet file and interrupts the write once it
        crosses ``byte_ceiling``, so an oversized spill cannot fill the disk before a post-write
        size check would reject it — DuckDB flushes row groups incrementally, so the bytes
        actually written are bounded to roughly the ceiling plus one row group. A final exact
        check catches a small over-ceiling file that completed between samples.
        """

        timed_out = threading.Event()
        over_limit = threading.Event()
        stop_watch = threading.Event()

        def _interrupt_timeout() -> None:
            timed_out.set()
            connection.interrupt()

        def _watch_size() -> None:
            while not stop_watch.wait(_SPILL_POLL_SECONDS):
                try:
                    if spill_path.stat().st_size > byte_ceiling:
                        over_limit.set()
                        connection.interrupt()
                        return
                except FileNotFoundError:
                    continue

        timer = threading.Timer(self.settings.query_timeout_seconds, _interrupt_timeout)
        watcher = threading.Thread(target=_watch_size, daemon=True)
        timer.start()
        watcher.start()
        try:
            connection.execute(copy_sql)
        except duckdb.Error as exc:
            if over_limit.is_set():
                raise ArtifactLimitError(
                    f"spill exceeded the artifact byte limit of {byte_ceiling}"
                ) from exc
            if timed_out.is_set():
                raise TimeoutError("query exceeded the configured execution time limit") from exc
            raise
        finally:
            stop_watch.set()
            timer.cancel()
            watcher.join(timeout=1)
        if spill_path.exists() and spill_path.stat().st_size > byte_ceiling:
            raise ArtifactLimitError(
                f"spill is {spill_path.stat().st_size} bytes; artifact limit is {byte_ceiling}"
            )

    def execute(
        self, session: AnalysisSession, sql: str, max_rows: int | None = None
    ) -> dict[str, Any]:
        normalized = self._validate_sql(sql)
        requested_limit = max_rows if max_rows is not None else self.settings.max_query_rows
        limit = min(max(requested_limit, 1), self.settings.max_query_rows)
        source_ids = list(
            dict.fromkeys(match.group("id") for match in _SOURCE_REF.finditer(normalized))
        )
        if not source_ids:
            raise QueryRejected("query must reference at least one registered source('src_...')")

        resolved_sources: list[tuple[str, SourceKind, str, dict[str, Any]]] = []
        for source_id in source_ids:
            source = self.sources.get(session.id, source_id)
            if source.relative_path is None:
                raise QueryRejected("URI/database sources are not supported by local query")
            resolved_path = self.workspace.resolve_file(
                session.workspace_root, source.relative_path
            )
            # Recompute the fingerprint at execution start so results are always attributable to
            # the exact bytes queried; a source modified after inspection is rejected.
            current = fingerprint_file(resolved_path)
            if current.get("sha256") != source.fingerprint.get("sha256"):
                raise QueryRejected(
                    f"source {source_id} changed since inspection; re-inspect before querying"
                )
            resolved_sources.append((source_id, source.kind, str(resolved_path), current))

        execution_id = new_id(EntityType.EXECUTION)
        # Spill to the out-of-workspace archive (server-controlled) so managed code cannot
        # corrupt the artifact and a workspace .agentic-analytics symlink cannot redirect the
        # write outside its boundary.
        archive_base = self.artifacts.archive_base(session.id, execution_id)
        spill_path = self._prepare_spill_path(archive_base)

        connection = duckdb.connect(database=":memory:")
        fingerprints: dict[str, Any] = {}
        rewritten = normalized
        # Unguessable per-execution view/table names cannot be shadowed by a caller CTE.
        token = uuid4().hex
        result_table = f"_result_{token}"
        try:
            allowed_paths = [item[2] for item in resolved_sources]
            allowed_paths.append(str(spill_path))
            self._secure_connection(connection, allowed_paths)
            for index, (source_id, kind, file_path, fingerprint) in enumerate(resolved_sources):
                view_name = f"_src_{token}_{index}"
                path_literal = _sql_string(file_path)
                if kind is SourceKind.CSV:
                    reader = f"read_csv({path_literal}, strict_mode = true)"
                elif kind is SourceKind.PARQUET:
                    reader = f"read_parquet({path_literal})"
                else:
                    raise QueryRejected(f"unsupported source kind: {kind}")
                connection.execute(f'CREATE TEMP VIEW "{view_name}" AS SELECT * FROM {reader}')
                pattern = re.compile(
                    rf"source\(\s*['\"]{re.escape(source_id)}['\"]\s*\)", re.IGNORECASE
                )
                rewritten = pattern.sub(f'"{view_name}"', rewritten)
                fingerprints[source_id] = fingerprint

            started = datetime.now(UTC)
            # Materialize the full result ONCE, bounded by a row cap and the interrupt timer, so
            # the preview and the spilled artifact are the same evaluation (deterministic even
            # for nondeterministic SQL) and materialization cannot run unbounded.
            spill_cap = self.settings.max_spill_rows
            materialize_sql = (
                f'CREATE TEMP TABLE "{result_table}" AS '
                f"SELECT * FROM ({rewritten}) AS _q LIMIT {spill_cap + 1}"
            )
            try:
                self._run_bounded(connection, materialize_sql)
                materialized = self._scalar_int(
                    connection, f'SELECT count(*) FROM "{result_table}"'
                )
                # Cap each column's cells in SQL, then stream the preview under the byte budget
                # so an oversized string/blob (or many wide rows) never fully materializes in
                # Python.
                projection, preview_cell_cap = self._preview_projection(
                    connection, result_table
                )
                columns, serial_rows, preview_truncated = self._fetch_preview_bounded(
                    connection,
                    f'SELECT {projection} FROM "{result_table}" LIMIT {limit + 1}',
                    limit,
                    preview_cell_cap,
                )
            except (duckdb.Error, TimeoutError) as exc:
                self._persist_failure(session, normalized, limit, source_ids, fingerprints, exc)
                raise QueryExecutionError(str(exc)) from exc

            row_truncated = materialized > limit
            truncated = row_truncated or preview_truncated
            spill_capped = materialized > spill_cap

            artifact_id: str | None = None
            artifact_ids: list[str] = []
            if truncated:
                spill_literal = _sql_string(str(spill_path))
                # Bound the spill in bytes as it is written (a watchdog interrupts the COPY once
                # the file crosses the ceiling) and in time (wall-clock interrupt), so an
                # oversized spill cannot fill the disk; register_file then enforces the precise
                # per-file and session-cumulative quotas before the artifact is recorded.
                byte_ceiling = min(
                    self.settings.max_artifact_bytes,
                    self.settings.max_total_artifact_bytes,
                )
                try:
                    self._write_spill_bounded(
                        connection,
                        f'COPY "{result_table}" TO {spill_literal} '
                        "(FORMAT PARQUET, COMPRESSION ZSTD)",
                        spill_path,
                        byte_ceiling,
                    )
                    artifact = self.artifacts.register_file(
                        session.id,
                        execution_id,
                        spill_path,
                        lineage={
                            "change": "query_result_spill",
                            "source_ids": source_ids,
                        },
                        metadata={
                            "format": "parquet",
                            "preview_rows": len(serial_rows),
                            "query_truncated": True,
                            "spill_row_count": materialized,
                            "spill_truncated": spill_capped,
                        },
                    )
                except (duckdb.Error, TimeoutError, ArtifactLimitError) as exc:
                    # Never register a partial or over-limit spill: drop the file and record the
                    # failure so the oversized artifact does not silently bypass the quota.
                    with contextlib.suppress(FileNotFoundError):
                        spill_path.unlink()
                    self._persist_failure(
                        session, normalized, limit, source_ids, fingerprints, exc
                    )
                    raise QueryExecutionError(str(exc)) from exc
                artifact_id = artifact.id
                artifact_ids.append(artifact.id)

            execution = ExecutionRecord(
                id=execution_id,
                session_id=session.id,
                execution_type=ExecutionType.MANAGED_SQL,
                status=ExecutionStatus.SUCCEEDED,
                request={"sql": normalized, "max_rows": limit},
                source_ids=source_ids,
                source_fingerprints=fingerprints,
                started_at=started,
                completed_at=datetime.now(UTC),
                runtime={"backend": "duckdb", "duckdb": duckdb.__version__},
                result_preview={"columns": columns, "rows": serial_rows},
                truncated=truncated,
                artifact_ids=artifact_ids,
            )
            self.executions.add(execution)
            return {
                "execution_id": execution.id,
                "columns": columns,
                "rows": serial_rows,
                "row_count_returned": len(serial_rows),
                "truncated": truncated,
                "artifact_id": artifact_id,
            }
        finally:
            connection.close()

    def _persist_failure(
        self,
        session: AnalysisSession,
        sql: str,
        limit: int,
        source_ids: list[str],
        fingerprints: dict[str, Any],
        exc: BaseException,
    ) -> None:
        status = (
            ExecutionStatus.TIMED_OUT
            if isinstance(exc, TimeoutError)
            else ExecutionStatus.FAILED
        )
        record = ExecutionRecord(
            session_id=session.id,
            execution_type=ExecutionType.MANAGED_SQL,
            status=status,
            request={"sql": sql, "max_rows": limit},
            source_ids=source_ids,
            source_fingerprints=fingerprints,
            completed_at=datetime.now(UTC),
            runtime={"backend": "duckdb", "duckdb": duckdb.__version__},
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        self.executions.add(record)
