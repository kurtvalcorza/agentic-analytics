from __future__ import annotations

import contextlib
import importlib.metadata
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_analytics.dataplane import DataPlaneClient, DataPlaneError
from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.models import (
    AnalysisSession,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionType,
    SourceAttestation,
    SourceKind,
)
from agentic_analytics.repositories import ExecutionRepository, SourceRepository
from agentic_analytics.settings import Settings

from .artifact_registry import ArtifactLimitError, ArtifactRegistry
from .inspector import fingerprint_file
from .sql_tokens import SqlToken, tokenize_sql
from .workspace import WorkspaceService

# Read from package metadata: the server process never imports DuckDB itself (SEC-02).
_DUCKDB_VERSION = importlib.metadata.version("duckdb")
_SOURCE_ID = re.compile(r"src_[0-9a-f]{32}", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|copy|attach|detach|install|load|call|pragma|set|"
    r"export|import|vacuum|read_csv|read_csv_auto|read_parquet|parquet_scan|csv_scan|read_json|"
    r"read_text|read_blob|glob|sqlite_scan|postgres_scan|httpfs|duckdb_secrets|query|query_table|"
    # Catalog/configuration introspection can leak absolute host paths from view definitions.
    r"duckdb_views|duckdb_tables|duckdb_columns|duckdb_constraints|duckdb_databases|"
    r"duckdb_settings|duckdb_functions|duckdb_schemas|duckdb_temporary_files|duckdb_extensions|"
    r"information_schema|pg_catalog|sqlite_master|sqlite_temp_master)\b",
    re.IGNORECASE,
)
_CATALOG_RELATIONS = {
    "information_schema",
    "pg_catalog",
    "sqlite_master",
    "sqlite_temp_master",
}


def _source_references(tokens: list[SqlToken]) -> list[tuple[int, int, str]]:
    references = []
    for index, token in enumerate(tokens[:-3]):
        if token.kind not in {"identifier", "quoted_identifier"} or token.value.lower() != "source":
            continue
        opening, argument, closing = tokens[index + 1 : index + 4]
        if (
            opening.value == "("
            and closing.value == ")"
            and argument.kind in {"string", "quoted_identifier"}
            and _SOURCE_ID.fullmatch(argument.value)
        ):
            references.append((token.start, closing.end, argument.value))
    return references


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
        data_plane: DataPlaneClient | None = None,
    ) -> None:
        self.data_plane = data_plane or DataPlaneClient(settings)
        self.sources = sources
        self.executions = executions
        self.workspace = workspace
        self.artifacts = artifacts
        self.settings = settings

    @staticmethod
    def _validate_sql(sql: str) -> str:
        normalized = sql.strip()
        try:
            tokens = tokenize_sql(normalized)
        except ValueError as exc:
            raise QueryRejected(str(exc)) from exc
        if tokens and tokens[-1].kind == "symbol" and tokens[-1].value == ";":
            # Comments may follow the optional terminating semicolon.
            normalized = normalized[: tokens[-1].start].rstrip()
            tokens.pop()
        if (
            not tokens
            or tokens[0].kind != "identifier"
            or tokens[0].value.lower() not in {"select", "with"}
        ):
            raise QueryRejected("only SELECT or WITH analytical queries are allowed")
        for index, token in enumerate(tokens):
            if token.kind == "symbol" and token.value == ";":
                raise QueryRejected("multiple SQL statements are not allowed")
            following = tokens[index + 1].value if index + 1 < len(tokens) else ""
            preceding = tokens[index - 1].value.lower() if index else ""
            # Quoting does not disable a function call or catalog relation. Ordinary quoted
            # column names and aliases remain data identifiers, including names like "update".
            executable = token.kind == "identifier" or (
                token.kind == "quoted_identifier"
                and (
                    following == "("
                    or (
                        token.value.lower() in _CATALOG_RELATIONS
                        and (
                            following == "."
                            or preceding in {"from", "join", "only", "lateral", ",", "."}
                        )
                    )
                )
            )
            if executable and _FORBIDDEN.fullmatch(token.value):
                raise QueryRejected(
                    "query contains a forbidden command or external access function"
                )
        return normalized

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

    def execute(
        self,
        session: AnalysisSession,
        sql: str,
        max_rows: int | None = None,
        *,
        require_complete: bool = False,
    ) -> dict[str, Any]:
        """Run bounded SQL and report whether the *full result* is available.

        ``truncated`` means the inline preview is partial. ``result_complete`` says whether the
        preview (when not truncated) or the spilled artifact holds every result row; it is false
        when the result exceeded ``max_spill_rows`` and the artifact holds only a prefix (an
        arbitrary one unless the query has a total ``ORDER BY``). With ``require_complete`` such
        a result fails instead of producing a partial artifact.
        """

        normalized = self._validate_sql(sql)
        requested_limit = max_rows if max_rows is not None else self.settings.max_query_rows
        limit = min(max(requested_limit, 1), self.settings.max_query_rows)
        references = _source_references(tokenize_sql(normalized))
        source_ids = list(dict.fromkeys(source_id for _, _, source_id in references))
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

        fingerprints = {source_id: fingerprint for source_id, _, _, fingerprint in resolved_sources}
        # Unguessable per-execution view/table names cannot be shadowed by a caller CTE.
        token = uuid4().hex
        view_names = {
            source_id: f"_src_{token}_{index}"
            for index, (source_id, _, _, _) in enumerate(resolved_sources)
        }
        rewritten = normalized
        for start, end, source_id in reversed(references):
            rewritten = rewritten[:start] + f'"{view_names[source_id]}"' + rewritten[end:]
        # The artifact row budget is never smaller than the preview row limit. Otherwise a
        # result could overflow the spill cap while still fitting the preview, leaving
        # result_complete=False with no artifact. With this floor, overflowing the cap always
        # also truncates the preview, so a capped result is always spilled.
        spill_cap = max(self.settings.max_spill_rows, limit)
        timeout = self.settings.query_timeout_seconds
        payload = {
            "sources": [
                {"view": view_names[source_id], "kind": kind.value, "path": file_path}
                for source_id, kind, file_path, _ in resolved_sources
            ],
            "sql": rewritten,
            "result_table": f"_result_{token}",
            "limit": limit,
            "spill_cap": spill_cap,
            "spill_path": str(spill_path),
            # Bound the spill as it is written at the *remaining* session budget, so a session
            # that already holds artifacts cannot over-write before registration rejects it.
            "spill_byte_ceiling": self.artifacts.spill_byte_ceiling(session.id),
            "require_complete": require_complete,
            "limits": self.data_plane.limits(timeout),
        }
        started = datetime.now(UTC)
        try:
            # Materialize, preview, and spill each have their own deadline inside the worker.
            outcome = self.data_plane.call("query", payload, deadline_seconds=3 * timeout)
        except DataPlaneError as exc:
            # Never register a partial or over-limit spill: drop the file and audit the failure.
            with contextlib.suppress(FileNotFoundError):
                spill_path.unlink()
            self._persist_failure(session, normalized, limit, source_ids, fingerprints, exc)
            raise QueryExecutionError(str(exc)) from exc

        columns: list[str] = outcome["columns"]
        serial_rows: list[list[Any]] = outcome["rows"]
        materialized = int(outcome["materialized"])
        truncated = bool(outcome["truncated"])
        spill_capped = bool(outcome["spill_capped"])
        # Invariant relied on by clients: an incomplete result always has a truncated preview
        # and (unless require_complete refused it) a published, capped artifact.
        assert truncated or not spill_capped
        if spill_capped and require_complete:
            incomplete = QueryExecutionError(
                f"query result exceeds max_spill_rows ({spill_cap}); refusing to produce a "
                "partial result because require_complete was set. Aggregate or filter the "
                "query, or raise AGENTIC_ANALYTICS_MAX_SPILL_ROWS."
            )
            self._persist_failure(
                session, normalized, limit, source_ids, fingerprints, incomplete
            )
            raise incomplete
        artifact_rows = min(materialized, spill_cap)

        artifact_id: str | None = None
        artifact_ids: list[str] = []
        if outcome["spilled"]:
            try:
                artifact = self.artifacts.register_file(
                    session.id,
                    execution_id,
                    spill_path,
                    lineage={"change": "query_result_spill", "source_ids": source_ids},
                    metadata={
                        "format": "parquet",
                        "preview_rows": len(serial_rows),
                        "query_truncated": True,
                        "spill_row_count": artifact_rows,
                        "spill_truncated": spill_capped,
                        "result_complete": not spill_capped,
                        "max_spill_rows": spill_cap,
                    },
                )
            except ArtifactLimitError as exc:
                with contextlib.suppress(FileNotFoundError):
                    spill_path.unlink()
                self._persist_failure(session, normalized, limit, source_ids, fingerprints, exc)
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
            # DuckDB can only reach the registered source views (allowed_paths, external
            # access disabled), so source use is runtime-enforced, not merely declared.
            source_attestation=SourceAttestation.ENFORCED,
            started_at=started,
            completed_at=datetime.now(UTC),
            runtime=self._runtime(),
            result_preview={
                "columns": columns,
                "rows": serial_rows,
                "result_complete": not spill_capped,
                "spill_truncated": spill_capped,
            },
            truncated=truncated,
            artifact_ids=artifact_ids,
        )
        self.executions.add(execution)
        completeness_note = (
            f"Result exceeded max_spill_rows ({spill_cap}); the artifact holds only "
            f"{artifact_rows} rows, an arbitrary subset unless the query has a total "
            "ORDER BY. Do not treat it as the complete result."
            if spill_capped
            else None
        )
        return {
            "execution_id": execution.id,
            "columns": columns,
            "rows": serial_rows,
            "row_count_returned": len(serial_rows),
            "truncated": truncated,
            "artifact_id": artifact_id,
            # Full-result completeness, distinct from preview truncation.
            "result_complete": not spill_capped,
            "spill_truncated": spill_capped,
            "max_spill_rows": spill_cap,
            "artifact_row_count": artifact_rows if artifact_id is not None else None,
            "result_row_count": None if spill_capped else materialized,
            "completeness_note": completeness_note,
            "source_attestation": SourceAttestation.ENFORCED.value,
        }

    @staticmethod
    def _runtime() -> dict[str, Any]:
        return {"backend": "duckdb", "duckdb": _DUCKDB_VERSION, "isolation": "process"}

    def _persist_failure(
        self,
        session: AnalysisSession,
        sql: str,
        limit: int,
        source_ids: list[str],
        fingerprints: dict[str, Any],
        exc: BaseException,
    ) -> None:
        timed_out = isinstance(exc, DataPlaneError) and exc.category == "timeout"
        status = ExecutionStatus.TIMED_OUT if timed_out else ExecutionStatus.FAILED
        error: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, DataPlaneError):
            # Report the worker-side error type (e.g. BinderException) and its category.
            error = {"type": exc.error_type, "category": exc.category, "message": str(exc)}
        record = ExecutionRecord(
            session_id=session.id,
            execution_type=ExecutionType.MANAGED_SQL,
            status=status,
            request={"sql": sql, "max_rows": limit},
            source_ids=source_ids,
            source_fingerprints=fingerprints,
            completed_at=datetime.now(UTC),
            runtime=self._runtime(),
            error=error,
        )
        self.executions.add(record)
