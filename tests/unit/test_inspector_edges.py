import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agentic_analytics.models import AnalysisSession
from agentic_analytics.repositories import SourceRepository
from agentic_analytics.services.inspector import InspectorService, SourceInspectionError
from agentic_analytics.services.workspace import WorkspaceService
from agentic_analytics.settings import Settings


def _service(
    tmp_path: Path,
    workspace: Path,
    *,
    max_profile_columns: int = 200,
    max_result_preview_bytes: int = 256 * 1024,
    max_result_cell_chars: int = 8192,
) -> InspectorService:
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
        max_profile_columns=max_profile_columns,
        max_result_preview_bytes=max_result_preview_bytes,
        max_result_cell_chars=max_result_cell_chars,
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
    malformed = b"a,b\r\n1,2\n3,4\r\n"
    (workspace / "malformed.csv").write_bytes(malformed)
    service = _service(tmp_path, workspace)
    session = AnalysisSession(workspace_root=str(workspace))
    with pytest.raises(SourceInspectionError, match="empty"):
        service.inspect(session, "empty.csv")
    with pytest.raises(SourceInspectionError, match="could not be inspected"):
        service.inspect(session, "malformed.csv")


@pytest.mark.parametrize(
    "value", ["x" * 100000, "界" * 10000, "\\" * 10000], ids=["ascii", "unicode", "escape"]
)
def test_inspector_bounds_cell_and_sample_bytes(tmp_path: Path, value: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.csv").write_text(f"value\n{value}\n", encoding="utf-8")
    service = _service(
        tmp_path, workspace, max_result_preview_bytes=1024, max_result_cell_chars=256
    )
    source, profile = service.inspect(AnalysisSession(workspace_root=str(workspace)), "large.csv")
    assert source.row_count == 1
    assert len(profile["sample"]) == 1
    assert len(profile["sample"][0]["value"]) <= 256
    assert len(json.dumps(profile["sample"]).encode("utf-8")) <= 1024
    assert profile["sample_truncated"] is True


def test_inspector_bounds_wide_metadata_and_preserves_canonical_schema(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    names = [f"column_{index}_" + "x" * 80 for index in range(1000)]
    pq.write_table(
        pa.table({name: [index] for index, name in enumerate(names)}), workspace / "wide.parquet"
    )
    service = _service(tmp_path, workspace, max_result_preview_bytes=1024)
    session = AnalysisSession(workspace_root=str(workspace))
    source, profile = service.inspect(session, "wide.parquet")
    assert len(source.schema_) == 1000
    assert len(service.sources.get(session.id, source.id).schema_) == 1000
    assert 0 < len(profile["schema"]) < 1000
    metadata = {"schema": profile["schema"], "null_counts": profile["null_counts"]}
    assert len(json.dumps(metadata).encode("utf-8")) <= 1024
    assert len(json.dumps(profile["sample"]).encode("utf-8")) <= 1024
    assert profile["sample_truncated"] is True
    assert profile["profile_truncated"] is True
    assert set(profile["sample"][0]) == {column["name"] for column in profile["schema"]}


def test_inspector_caps_cells_before_fetching_from_duckdb(tmp_path: Path, monkeypatch) -> None:
    import duckdb

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pq.write_table(
        pa.table({"text": ["x" * 100000], "nested": [list(range(100000))]}),
        workspace / "large.parquet",
    )
    service = _service(tmp_path, workspace, max_result_preview_bytes=1024, max_result_cell_chars=64)
    connect = duckdb.connect
    fetched = []

    class ObservedConnection:
        def __init__(self, *args, **kwargs):
            self.connection = connect(*args, **kwargs)
            self.sampling = False

        def execute(self, sql, parameters=None):
            self.sampling = sql.startswith("SELECT substr(")
            self.connection.execute(sql, parameters)
            return self

        def fetchone(self):
            row = self.connection.fetchone()
            if self.sampling and row is not None:
                fetched.append(row)
            return row

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(duckdb, "connect", ObservedConnection)
    _, profile = service.inspect(AnalysisSession(workspace_root=str(workspace)), "large.parquet")
    assert fetched and all(len(cell) == 65 for cell in fetched[0])
    assert all(len(cell) == 64 for cell in profile["sample"][0].values())
    assert profile["sample_truncated"] is True


def test_inspector_handles_column_name_larger_than_response_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pq.write_table(pa.table({"x" * 10000: [1]}), workspace / "long-name.parquet")
    service = _service(tmp_path, workspace, max_result_preview_bytes=1024)
    source, profile = service.inspect(
        AnalysisSession(workspace_root=str(workspace)), "long-name.parquet"
    )
    assert len(source.schema_[0]["name"]) == 10000
    assert profile["schema"] == []
    assert profile["sample"] == []
    assert profile["sample_truncated"] is True
    assert profile["profile_truncated"] is True


def test_inspector_does_not_mark_exact_cell_limit_as_truncated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "exact.csv").write_text("value\n" + "x" * 64 + "\n", encoding="utf-8")
    service = _service(tmp_path, workspace, max_result_cell_chars=64)
    _, profile = service.inspect(AnalysisSession(workspace_root=str(workspace)), "exact.csv")
    assert profile["sample"] == [{"value": "x" * 64}]
    assert profile["sample_truncated"] is False
