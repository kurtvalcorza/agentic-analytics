from pathlib import Path

import pytest

from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


@pytest.mark.anyio
async def test_capability_reflects_conformant_backend(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = dict(state_dir=tmp_path / "state", allowed_workspace_roots=[workspace])

    docker_server = build_server(Settings(**base, execution_backend="docker"))
    created = await docker_server.call_tool("create_session", {"workspace_root": "."})
    assert created.structured_content is not None
    assert created.structured_content["capabilities"]["managed_python"] is True

    dev_server = build_server(
        Settings(state_dir=tmp_path / "state2", allowed_workspace_roots=[workspace],
                 execution_backend="subprocess_dev")
    )
    created_dev = await dev_server.call_tool("create_session", {"workspace_root": "."})
    assert created_dev.structured_content is not None
    assert created_dev.structured_content["capabilities"]["managed_python"] is False


@pytest.mark.anyio
async def test_artifact_resource_is_resolvable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = build_server(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
        )
    )
    created = await server.call_tool(
        "create_session", {"workspace_root": ".", "mode": "permissive"}
    )
    assert created.structured_content is not None
    session_id = created.structured_content["session_id"]
    code = (
        "from pathlib import Path\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        "Path('outputs/r.txt').write_text('hi')\n"
        "print('ok')\n"
    )
    executed = await server.call_tool("execute_python", {"session_id": session_id, "code": code})
    assert executed.structured_content is not None
    artifact_ids = executed.structured_content["artifact_ids"]
    assert len(artifact_ids) == 1
    got = await server.call_tool(
        "get_artifact", {"session_id": session_id, "artifact_id": artifact_ids[0]}
    )
    assert got.structured_content is not None
    uri = got.structured_content["uri"]
    contents = list(await server.read_resource(uri))
    assert contents and contents[0].content == b"hi"


@pytest.mark.anyio
async def test_create_session_rejects_overlapping_active_workspace(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    server = build_server(
        Settings(state_dir=tmp_path / "state", allowed_workspace_roots=[tmp_path])
    )
    await server.call_tool("create_session", {"workspace_root": str(tmp_path)})
    # The tool layer surfaces the overlap rejection as a tool error.
    with pytest.raises(Exception, match="overlaps an active session"):
        await server.call_tool("create_session", {"workspace_root": str(nested)})


@pytest.mark.anyio
async def test_close_session_releases_workspace_for_reuse(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        allowed_workspace_roots=[tmp_path],
        execution_backend="subprocess_dev",
    )
    server = build_server(settings)
    first = await server.call_tool("create_session", {"workspace_root": str(tmp_path)})
    assert first.structured_content is not None
    session_id = first.structured_content["session_id"]

    # An active session still blocks an overlapping create.
    with pytest.raises(Exception, match="overlaps an active session"):
        await server.call_tool("create_session", {"workspace_root": str(tmp_path)})

    closed = await server.call_tool("close_session", {"session_id": session_id})
    assert closed.structured_content is not None
    assert closed.structured_content["status"] == "completed"

    # After close, the same workspace can back a fresh session.
    second = await server.call_tool("create_session", {"workspace_root": str(tmp_path)})
    assert second.structured_content is not None
    assert second.structured_content["session_id"] != session_id

    # Closing is idempotent.
    again = await server.call_tool("close_session", {"session_id": session_id})
    assert again.structured_content is not None
    assert again.structured_content["status"] == "completed"

    # Rejects a non-terminal target status.
    with pytest.raises(Exception, match=r"completed.*cancelled|cancelled.*completed"):
        await server.call_tool(
            "close_session",
            {"session_id": second.structured_content["session_id"], "status": "active"},
        )


@pytest.mark.anyio
async def test_closed_session_stays_reusable_after_restart(tmp_path: Path) -> None:
    # A fresh server over the same persistent state_dir models a process restart: the closed
    # status must survive so the workspace is not re-locked by stale persisted state.
    settings = Settings(
        state_dir=tmp_path / "state",
        allowed_workspace_roots=[tmp_path],
        execution_backend="subprocess_dev",
    )
    server = build_server(settings)
    created = await server.call_tool("create_session", {"workspace_root": str(tmp_path)})
    assert created.structured_content is not None
    await server.call_tool(
        "close_session", {"session_id": created.structured_content["session_id"]}
    )

    restarted = build_server(settings)
    reopened = await restarted.call_tool("create_session", {"workspace_root": str(tmp_path)})
    assert reopened.structured_content is not None
    assert reopened.structured_content["session_id"] != created.structured_content["session_id"]


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
