import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


@pytest.mark.anyio
async def test_large_query_returns_preview_and_spill_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rows = 20_000
    pq.write_table(
        pa.table(
            {
                "id": range(rows),
                "group": [f"g-{index % 20}" for index in range(rows)],
                "value": [index * 2 for index in range(rows)],
            }
        ),
        workspace / "large.parquet",
    )
    server = build_server(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
            max_query_rows=5,
        )
    )
    created = await server.call_tool(
        "create_session", {"workspace_root": ".", "mode": "permissive"}
    )
    assert created.structured_content is not None
    session_id = created.structured_content["session_id"]
    inspected = await server.call_tool(
        "inspect_source", {"session_id": session_id, "source": "large.parquet"}
    )
    assert inspected.structured_content is not None
    source_id = inspected.structured_content["source_id"]

    result = await server.call_tool(
        "query_data",
        {
            "session_id": session_id,
            "sql": f"SELECT * FROM source('{source_id}') ORDER BY id",
            "max_rows": 5,
        },
    )
    assert result.structured_content is not None
    body = result.structured_content
    assert body["row_count_returned"] == 5
    assert len(body["rows"]) == 5
    assert body["truncated"] is True
    assert body["artifact_id"] is not None

    artifact_result = await server.call_tool(
        "get_artifact",
        {"session_id": session_id, "artifact_id": body["artifact_id"]},
    )
    assert artifact_result.structured_content is not None
    artifact = artifact_result.structured_content["artifact"]
    assert artifact["kind"] == "dataset"
    assert artifact["media_type"] == "application/vnd.apache.parquet"
    assert artifact["metadata"]["query_truncated"] is True
    # The spill lives in the out-of-workspace archive and is retrievable through its resource.
    assert not (workspace / artifact["relative_path"]).exists()
    uri = artifact_result.structured_content["uri"]
    contents = list(await server.read_resource(uri))
    assert contents
    spilled = pq.read_table(io.BytesIO(contents[0].content))
    assert spilled.num_rows == rows
