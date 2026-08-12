from pathlib import Path

import pytest

from agentic_analytics.models import AnalysisSession
from agentic_analytics.repositories import ExecutionRepository, SourceRepository
from agentic_analytics.services.inspector import InspectorService
from agentic_analytics.services.query import QueryRejected, QueryService
from agentic_analytics.services.workspace import WorkspaceService
from agentic_analytics.settings import Settings


def _services(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.csv").write_text("region,value\nNCR,1\nCAR,2\nNCR,3\n", encoding="utf-8")
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
        max_query_rows=2,
    )
    sources = SourceRepository(settings.state_dir)
    executions = ExecutionRepository(settings.state_dir)
    workspace_service = WorkspaceService([workspace])
    inspector = InspectorService(sources, workspace_service, settings)
    query = QueryService(sources, executions, workspace_service, settings)
    session = AnalysisSession(workspace_root=str(workspace))
    source, _ = inspector.inspect(session, "sample.csv")
    return session, source, query, executions


def test_query_is_bounded_and_persists_execution(tmp_path: Path) -> None:
    session, source, query, executions = _services(tmp_path)
    sql = f"SELECT * FROM source('{source.id}') ORDER BY value"
    result = query.execute(session, sql, max_rows=2)
    assert result["row_count_returned"] == 2
    assert result["truncated"] is True
    execution = executions.get(session.id, result["execution_id"])
    assert execution.truncated is True
    assert execution.source_ids == [source.id]


def test_query_blocks_external_access_and_writes(tmp_path: Path) -> None:
    session, source, query, _ = _services(tmp_path)
    with pytest.raises(QueryRejected):
        query.execute(session, "SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(QueryRejected):
        query.execute(session, f"DROP TABLE source('{source.id}')")
    with pytest.raises(QueryRejected):
        query.execute(session, "SELECT 1")
