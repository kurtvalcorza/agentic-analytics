from pathlib import Path

import pytest

from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


@pytest.mark.anyio
async def test_data_plane_tool_schemas_are_host_neutral(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
    )
    server = build_server(settings)
    tools = await server.list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert {"create_session", "list_sources", "inspect_source", "query_data"} <= set(by_name)
    create_properties = by_name["create_session"].input_schema["properties"]
    assert set(create_properties) == {"workspace_root", "mode"}
    query_properties = by_name["query_data"].input_schema["properties"]
    assert {"session_id", "sql", "max_rows"} <= set(query_properties)
    for tool in by_name.values():
        serialized = str(tool.input_schema).lower()
        assert "claude" not in serialized
        assert "codex" not in serialized
        assert "antigravity" not in serialized


@pytest.mark.anyio
async def test_tools_complete_csv_discovery_and_query(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("group,value\na,1\nb,2\na,3\n", encoding="utf-8")
    settings = Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
    )
    server = build_server(settings)
    created = await server.call_tool("create_session", {"workspace_root": ".", "mode": "strict"})
    assert created.structured_content is not None
    session_id = created.structured_content["session_id"]
    listed = await server.call_tool("list_sources", {"session_id": session_id})
    assert listed.structured_content is not None
    assert listed.structured_content["count"] == 1
    assert listed.structured_content["truncated"] is False
    assert listed.structured_content["total_discovered"] == 1
    inspected = await server.call_tool(
        "inspect_source", {"session_id": session_id, "source": "data.csv", "sample_rows": 2}
    )
    assert inspected.structured_content is not None
    # Inspection statistics are nested under `profile` per the canonical contract.
    profile = inspected.structured_content["profile"]
    assert "null_counts" in profile
    assert "duplicate_row_count" in profile
    source_id = inspected.structured_content["source_id"]
    queried = await server.call_tool(
        "query_data",
        {
            "session_id": session_id,
            "sql": f"SELECT count(*) AS n FROM source('{source_id}')",
            "max_rows": 10,
        },
    )
    assert queried.structured_content is not None
    assert queried.structured_content["rows"] == [[3]]
