from pathlib import Path

import pytest

from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


@pytest.mark.anyio
async def test_execution_tool_schemas_are_host_neutral(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = build_server(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
        )
    )
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {"execute_python", "list_artifacts", "get_artifact"} <= set(tools)
    properties = tools["execute_python"].input_schema["properties"]
    assert {"session_id", "code", "source_ids", "timeout_seconds"} <= set(properties)
    for name in ("execute_python", "list_artifacts", "get_artifact"):
        serialized = str(tools[name].input_schema).lower()
        assert "claude" not in serialized
        assert "codex" not in serialized
        assert "antigravity" not in serialized
