from __future__ import annotations

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

from .artifact_registry import ArtifactRegistry
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

    def _bound_preview(
        self, rows: list[list[Any]]
    ) -> tuple[list[list[Any]], bool]:
        """Cap the preview by per-cell size and total bytes; return (rows, was_truncated)."""

        cell_cap = self.settings.max_result_cell_chars
        truncated = False
        capped_rows: list[list[Any]] = []
        for row in rows:
            capped_row: list[Any] = []
            for value in row:
                if isinstance(value, str) and len(value) > cell_cap:
                    capped_row.append(value[:cell_cap])
                    truncated = True
                else:
                    capped_row.append(value)
            capped_rows.append(capped_row)

        budget = self.settings.max_result_preview_bytes
        kept: list[list[Any]] = []
        used = 0
        for row in capped_rows:
            size = len(json.dumps(row, default=str).encode("utf-8"))
            if kept and used + size > budget:
                truncated = True
                break
            kept.append(row)
            used += size
        return kept, truncated

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
                columns, preview_raw = self._run_bounded(
                    connection, f'SELECT * FROM "{result_table}" LIMIT {limit + 1}'
                )
                materialized = self._scalar_int(
                    connection, f'SELECT count(*) FROM "{result_table}"'
                )
            except (duckdb.Error, TimeoutError) as exc:
                self._persist_failure(session, normalized, limit, source_ids, fingerprints, exc)
                raise QueryExecutionError(str(exc)) from exc

            row_truncated = len(preview_raw) > limit
            serial_rows = [[_json_value(value) for value in row] for row in preview_raw[:limit]]
            # Bound the preview by cell size and total bytes so a low-row-count result with a
            # very large/wide value cannot exhaust memory or model context.
            serial_rows, budget_truncated = self._bound_preview(serial_rows)
            truncated = row_truncated or budget_truncated
            spill_capped = materialized > spill_cap

            artifact_id: str | None = None
            artifact_ids: list[str] = []
            if truncated:
                spill_literal = _sql_string(str(spill_path))
                connection.execute(
                    f'COPY "{result_table}" TO {spill_literal} '
                    "(FORMAT PARQUET, COMPRESSION ZSTD)"
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
