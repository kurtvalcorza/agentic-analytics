from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from .workspace import WorkspaceService

_SOURCE_REF = re.compile(r"source\(\s*['\"](?P<id>src_[0-9a-f]{32})['\"]\s*\)", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|copy|attach|detach|install|load|call|pragma|set|"
    r"export|import|vacuum|read_csv|read_csv_auto|read_parquet|parquet_scan|csv_scan|read_json|"
    r"read_text|read_blob|glob|sqlite_scan|postgres_scan|httpfs|duckdb_secrets)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)


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


class QueryRejected(ValueError):
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
            raise QueryRejected("only SELECT, WITH, or EXPLAIN analytical queries are allowed")
        if ";" in normalized:
            raise QueryRejected("multiple SQL statements are not allowed")
        if _FORBIDDEN.search(normalized):
            raise QueryRejected("query contains a forbidden command or external access function")
        return normalized

    @staticmethod
    def _secure_connection(connection: duckdb.DuckDBPyConnection, paths: list[str]) -> None:
        allowed_paths = ", ".join(_sql_string(path) for path in paths)
        connection.execute(f"SET allowed_paths = [{allowed_paths}]")
        connection.execute("SET autoinstall_known_extensions = false")
        connection.execute("SET autoload_known_extensions = false")
        connection.execute("SET allow_community_extensions = false")
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")

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
            resolved_sources.append(
                (source_id, source.kind, str(resolved_path), source.fingerprint)
            )

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
        try:
            allowed_paths = [item[2] for item in resolved_sources]
            allowed_paths.append(str(spill_path))
            self._secure_connection(connection, allowed_paths)
            for index, (source_id, kind, file_path, fingerprint) in enumerate(resolved_sources):
                view_name = f"_source_{index}"
                path_literal = _sql_string(file_path)
                if kind is SourceKind.CSV:
                    reader = f"read_csv({path_literal}, strict_mode = true)"
                elif kind is SourceKind.PARQUET:
                    reader = f"read_parquet({path_literal})"
                else:
                    raise QueryRejected(f"unsupported source kind: {kind}")
                connection.execute(
                    f'CREATE TEMP VIEW "{view_name}" AS SELECT * FROM {reader}'
                )
                pattern = re.compile(
                    rf"source\(\s*['\"]{re.escape(source_id)}['\"]\s*\)", re.IGNORECASE
                )
                rewritten = pattern.sub(f'"{view_name}"', rewritten)
                fingerprints[source_id] = fingerprint

            started = datetime.now(UTC)
            bounded_sql = f"SELECT * FROM ({rewritten}) AS _bounded LIMIT {limit + 1}"
            cursor = connection.execute(bounded_sql)
            columns = [str(item[0]) for item in (cursor.description or [])]
            raw_rows = cursor.fetchall()
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
