from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agentic_analytics.models import AnalysisSession
from agentic_analytics.repositories import SourceRepository
from agentic_analytics.services.inspector import InspectorService
from agentic_analytics.services.workspace import WorkspaceService
from agentic_analytics.settings import Settings


def _service(
    tmp_path: Path,
    workspace: Path,
    *,
    max_profile_columns: int = 200,
) -> InspectorService:
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
        max_profile_columns=max_profile_columns,
    )
    return InspectorService(
        SourceRepository(settings.state_dir), WorkspaceService([workspace]), settings
    )


def test_inspector_profiles_parquet(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pq.write_table(
        pa.table({"group": ["a", "b", "a"], "value": [1, 2, 3]}),
        workspace / "sample.parquet",
    )
    service = _service(tmp_path, workspace)
    session = AnalysisSession(workspace_root=str(workspace))

    source, profile = service.inspect(session, "sample.parquet", sample_rows=2)

    assert source.kind.value == "parquet"
    assert profile["row_count"] == 3
    assert [column["name"] for column in profile["schema"]] == ["group", "value"]
    assert profile["sample_truncated"] is True


def test_inspector_marks_wide_profile_as_truncated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    columns = ",".join(f"c{index}" for index in range(5))
    values = ",".join(str(index) for index in range(5))
    (workspace / "wide.csv").write_text(f"{columns}\n{values}\n", encoding="utf-8")
    service = _service(tmp_path, workspace, max_profile_columns=2)
    session = AnalysisSession(workspace_root=str(workspace))

    _, profile = service.inspect(session, "wide.csv")

    assert len(profile["schema"]) == 5
    assert len(profile["null_counts"]) == 2
    assert profile["profile_truncated"] is True


def test_inspector_rejects_empty_or_malformed_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "empty.csv").write_text("", encoding="utf-8")
    (workspace / "malformed.csv").write_text('a,b\n1,"unterminated\n', encoding="utf-8")
    service = _service(tmp_path, workspace)
    session = AnalysisSession(workspace_root=str(workspace))

    with pytest.raises(duckdb.Error):
        service.inspect(session, "empty.csv")
    with pytest.raises(duckdb.Error):
        service.inspect(session, "malformed.csv")
