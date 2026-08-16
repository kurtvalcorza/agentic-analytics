from pathlib import Path

import pytest

from agentic_analytics.models import AnalysisSession, ExecutionStatus
from agentic_analytics.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    SourceRepository,
)
from agentic_analytics.services.artifact_registry import ArtifactRegistry
from agentic_analytics.services.inspector import InspectorService
from agentic_analytics.services.query import QueryExecutionError, QueryRejected, QueryService
from agentic_analytics.services.workspace import WorkspaceAuthorizationError, WorkspaceService
from agentic_analytics.settings import Settings


def _services(tmp_path: Path, *, max_query_rows: int = 2):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.csv").write_text(
        "region,value\nNCR,1\nCAR,2\nNCR,3\n", encoding="utf-8"
    )
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
        max_query_rows=max_query_rows,
    )
    sources = SourceRepository(settings.state_dir)
    executions = ExecutionRepository(settings.state_dir)
    artifacts = ArtifactRepository(settings.state_dir)
    workspace_service = WorkspaceService([workspace])
    inspector = InspectorService(sources, workspace_service, settings)
    registry = ArtifactRegistry(artifacts, settings.state_dir / "artifacts")
    query = QueryService(sources, executions, workspace_service, registry, settings)
    session = AnalysisSession(workspace_root=str(workspace))
    source, _ = inspector.inspect(session, "sample.csv")
    return session, source, query, executions, workspace, artifacts


def test_query_is_bounded_and_persists_execution(tmp_path: Path) -> None:
    session, source, query, executions, _, artifacts = _services(tmp_path)
    sql = f"SELECT * FROM source('{source.id}') ORDER BY value"
    result = query.execute(session, sql, max_rows=2)
    assert result["row_count_returned"] == 2
    assert result["truncated"] is True
    assert result["artifact_id"] is not None
    execution = executions.get(session.id, result["execution_id"])
    assert execution.truncated is True
    assert execution.source_ids == [source.id]
    assert execution.artifact_ids == [result["artifact_id"]]
    artifact = artifacts.get(session.id, result["artifact_id"])
    assert artifact.kind.value == "dataset"
    assert artifact.relative_path.endswith("query-result.parquet")


def test_query_blocks_external_access_and_writes(tmp_path: Path) -> None:
    session, source, query, _, _, _ = _services(tmp_path)
    with pytest.raises(QueryRejected):
        query.execute(session, "SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(QueryRejected):
        query.execute(session, "SELECT * FROM read_text('/etc/passwd')")
    with pytest.raises(QueryRejected):
        query.execute(session, f"DROP TABLE source('{source.id}')")
    with pytest.raises(QueryRejected):
        query.execute(session, "SELECT 1")
    with pytest.raises(QueryRejected):
        query.execute(session, f"EXPLAIN SELECT * FROM source('{source.id}')")


def test_query_allows_forbidden_tokens_inside_literals(tmp_path: Path) -> None:
    session, source, query, _, _, _ = _services(tmp_path, max_query_rows=100)
    result = query.execute(
        session, f"SELECT * FROM source('{source.id}') WHERE region = 'update'"
    )
    assert result["row_count_returned"] == 0
    assert result["truncated"] is False


def test_query_rejects_source_modified_after_inspection(tmp_path: Path) -> None:
    session, source, query, _, workspace, _ = _services(tmp_path, max_query_rows=100)
    (workspace / "sample.csv").write_text("region,value\nNCR,9\n", encoding="utf-8")
    with pytest.raises(QueryRejected, match="changed since inspection"):
        query.execute(session, f"SELECT * FROM source('{source.id}')")


def test_query_persists_failed_execution_record(tmp_path: Path) -> None:
    session, source, query, executions, _, _ = _services(tmp_path, max_query_rows=100)
    with pytest.raises(QueryExecutionError):
        query.execute(session, f"SELECT missing_column FROM source('{source.id}')")
    records = executions.list(session.id)
    assert any(record.status is ExecutionStatus.FAILED for record in records)
    failed = next(record for record in records if record.status is ExecutionStatus.FAILED)
    assert failed.completed_at is not None
    assert failed.error is not None


def test_query_spills_oversized_low_row_result(tmp_path: Path) -> None:
    session, source, query, _, _, artifacts = _services(tmp_path, max_query_rows=100)
    # A single very large cell must still spill and be trimmed in the preview, even though the
    # row count is well under the limit.
    result = query.execute(
        session, f"SELECT repeat('x', 200000) AS big FROM source('{source.id}') LIMIT 1"
    )
    assert result["row_count_returned"] == 1
    assert result["truncated"] is True
    assert result["artifact_id"] is not None
    assert len(result["rows"][0][0]) <= 8192
    artifact = artifacts.get(session.id, result["artifact_id"])
    assert artifact.media_type == "application/vnd.apache.parquet"


def test_preview_is_bounded_before_python_materialization(tmp_path: Path) -> None:
    session, source, query, _, _, _ = _services(tmp_path, max_query_rows=100)
    # Several rows each carrying a multi-megabyte cell. The SQL projection caps every cell to
    # the configured budget, so the preview the tool returns stays small regardless of the raw
    # cell size (the value never fully materializes in Python).
    result = query.execute(
        session,
        f"SELECT repeat('x', 5000000) AS big FROM source('{source.id}')",
    )
    assert result["truncated"] is True
    assert result["artifact_id"] is not None
    for row in result["rows"]:
        assert len(row[0]) <= 8192


def test_spill_over_artifact_byte_ceiling_is_rejected(tmp_path: Path) -> None:
    import uuid

    import pyarrow as pa
    import pyarrow.parquet as pq

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # High-entropy tokens so the ZSTD-compressed spill (~tens of KiB) clearly exceeds the tiny
    # per-artifact ceiling below; a bounded row count with real data still overflows the quota.
    tokens = [uuid.UUID(int=(index * 2654435761) % (2**128)).hex for index in range(4000)]
    pq.write_table(
        pa.table({"id": list(range(4000)), "tok": tokens}), workspace / "big.parquet"
    )
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
        max_query_rows=1,
        # The floor for max_artifact_bytes is 1 KiB; the spill is far larger.
        max_artifact_bytes=1024,
    )
    sources = SourceRepository(settings.state_dir)
    executions = ExecutionRepository(settings.state_dir)
    artifacts = ArtifactRepository(settings.state_dir)
    workspace_service = WorkspaceService([workspace])
    inspector = InspectorService(sources, workspace_service, settings)
    registry = ArtifactRegistry(
        artifacts,
        settings.state_dir / "artifacts",
        max_artifact_bytes=settings.max_artifact_bytes,
        max_total_bytes=settings.max_total_artifact_bytes,
    )
    query = QueryService(sources, executions, workspace_service, registry, settings)
    session = AnalysisSession(workspace_root=str(workspace))
    source, _ = inspector.inspect(session, "big.parquet")

    # max_query_rows=1 forces a spill; the spill exceeds the 1 KiB ceiling and must be rejected
    # rather than persisted as an over-limit artifact.
    with pytest.raises(QueryExecutionError, match="limit"):
        query.execute(session, f"SELECT * FROM source('{source.id}')")

    # The oversized spill file was cleaned up and no artifact was registered.
    assert artifacts.list(session.id) == []
    records = executions.list(session.id)
    assert any(record.status is ExecutionStatus.FAILED for record in records)


def test_preview_first_row_stays_within_byte_budget(tmp_path: Path) -> None:
    session, source, query, _, _, _ = _services(tmp_path, max_query_rows=100)
    # A single row with many individually large columns. The per-cell cap is derived from the
    # preview byte budget and column count, so even this first row cannot exceed the budget.
    import json

    from agentic_analytics.settings import Settings as _Settings

    budget = _Settings().max_result_preview_bytes
    columns = ", ".join(f"repeat('x', 20000) AS c{index}" for index in range(60))
    result = query.execute(session, f"SELECT {columns} FROM source('{source.id}') LIMIT 1")
    assert result["row_count_returned"] == 1
    assert result["truncated"] is True
    preview_bytes = len(json.dumps(result["rows"], default=str).encode("utf-8"))
    assert preview_bytes <= budget
    assert all(len(cell) <= 8192 for cell in result["rows"][0])


def test_spill_watchdog_bounds_oversized_write(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A multi-megabyte, multi-row-group spill: the COPY watchdog must interrupt the write once
    # the file crosses the tiny ceiling, and the partial file must be cleaned up.
    count = 300_000
    tokens = [format((index * 2654435761) % (2**64), "016x") for index in range(count)]
    pq.write_table(
        pa.table({"id": list(range(count)), "tok": tokens}), workspace / "big.parquet"
    )
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
        max_query_rows=1,
        max_artifact_bytes=1024,
    )
    sources = SourceRepository(settings.state_dir)
    executions = ExecutionRepository(settings.state_dir)
    artifacts = ArtifactRepository(settings.state_dir)
    workspace_service = WorkspaceService([workspace])
    inspector = InspectorService(sources, workspace_service, settings)
    registry = ArtifactRegistry(
        artifacts,
        settings.state_dir / "artifacts",
        max_artifact_bytes=settings.max_artifact_bytes,
        max_total_bytes=settings.max_total_artifact_bytes,
    )
    query = QueryService(sources, executions, workspace_service, registry, settings)
    session = AnalysisSession(workspace_root=str(workspace))
    source, _ = inspector.inspect(session, "big.parquet")

    with pytest.raises(QueryExecutionError, match="limit"):
        query.execute(session, f"SELECT * FROM source('{source.id}')")

    # No over-limit artifact registered and no spill file left behind in the archive.
    assert artifacts.list(session.id) == []
    archive_root = settings.state_dir / "artifacts"
    leftover = list(archive_root.rglob("query-result.parquet")) if archive_root.exists() else []
    assert leftover == []
    assert any(record.status is ExecutionStatus.FAILED for record in executions.list(session.id))


def test_query_view_names_cannot_be_shadowed_by_cte(tmp_path: Path) -> None:
    session, source, query, _, _, _ = _services(tmp_path, max_query_rows=100)
    sql = (
        f"WITH _source_0 AS (SELECT 999 AS value) "
        f"SELECT value FROM source('{source.id}') ORDER BY value"
    )
    result = query.execute(session, sql)
    assert result["rows"] == [[1], [2], [3]]


def test_query_reauthorizes_workspace_root(tmp_path: Path) -> None:
    session, source, _, executions, _, _ = _services(tmp_path, max_query_rows=100)
    # A service whose allowlist no longer contains the session root must refuse to read.
    narrowed = WorkspaceService([tmp_path / "elsewhere"])
    (tmp_path / "elsewhere").mkdir()
    narrowed_settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[tmp_path / "elsewhere"],
        max_query_rows=100,
    )
    query = QueryService(
        SourceRepository(tmp_path / "state"),
        executions,
        narrowed,
        ArtifactRegistry(
            ArtifactRepository(tmp_path / "state"), tmp_path / "state" / "artifacts"
        ),
        narrowed_settings,
    )
    with pytest.raises(WorkspaceAuthorizationError):
        query.execute(session, f"SELECT * FROM source('{source.id}')")
