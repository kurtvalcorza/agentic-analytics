from pathlib import Path

from agentic_analytics.models import AnalysisSession
from agentic_analytics.repositories import SourceRepository
from agentic_analytics.services.inspector import InspectorService, fingerprint_file
from agentic_analytics.services.workspace import WorkspaceService
from agentic_analytics.settings import Settings


def test_inspector_profiles_csv_and_reuses_unchanged_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_path = workspace / "sample.csv"
    source_path.write_text("region,value\nNCR,1\nCAR,\nNCR,1\n", encoding="utf-8")
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
    )
    repo = SourceRepository(settings.state_dir)
    service = InspectorService(repo, WorkspaceService([workspace]), settings)
    session = AnalysisSession(workspace_root=str(workspace))
    first, profile = service.inspect(session, "sample.csv", sample_rows=2)
    second, _ = service.inspect(session, "sample.csv", sample_rows=2)
    assert first.id == second.id
    assert first.row_count == 3
    assert profile["null_counts"]["value"] == 1
    assert profile["duplicate_row_count"] == 1
    assert len(profile["sample"]) == 2
    assert profile["sample_truncated"] is True
    assert len(fingerprint_file(source_path)["sha256"]) == 64
