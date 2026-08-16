from __future__ import annotations

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
        workspace_root = Path(session.workspace_root).resolve(strict=True)
        spill_path = (
            workspace_root
            / ".agentic-analytics"
            / "artifacts"
            / execution_id
            / "query-result.parquet"
        )
        spill_path.parent.mkdir(parents=True, exist_ok=True)

        connection = duckdb.connect(database=":memory:")
        fingerprints: dict[str, Any] = {}
        rewritten = normalized
        # Unguessable per-execution view names cannot be shadowed by a caller-defined CTE.
        token = uuid4().hex
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
            bounded_sql = f"SELECT * FROM ({rewritten}) AS _bounded LIMIT {limit + 1}"
            try:
                columns, raw_rows = self._run_bounded(connection, bounded_sql)
            except (duckdb.Error, TimeoutError) as exc:
                self._persist_failure(session, normalized, limit, source_ids, fingerprints, exc)
                raise QueryExecutionError(str(exc)) from exc

            truncated = len(raw_rows) > limit
            serial_rows = [[_json_value(value) for value in row] for row in raw_rows[:limit]]

            artifact_id: str | None = None
            artifact_ids: list[str] = []
            if truncated:
                spill_literal = _sql_string(str(spill_path))
                connection.execute(
                    f"COPY ({rewritten}) TO {spill_literal} "
                    "(FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                artifact = self.artifacts.register_file(
                    session.id,
                    execution_id,
                    workspace_root,
                    spill_path,
                    lineage={
                        "change": "query_result_spill",
                        "source_ids": source_ids,
                    },
                    metadata={
                        "format": "parquet",
                        "preview_rows": len(serial_rows),
                        "query_truncated": True,
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
