from pathlib import Path

import pytest

from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


@pytest.mark.anyio
async def test_evidence_tools_trace_source_execution_and_interpretation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("value\n1\n2\n3\n", encoding="utf-8")
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
    inspected = await server.call_tool(
        "inspect_source", {"session_id": session_id, "source": "data.csv"}
    )
    assert inspected.structured_content is not None
    source_id = inspected.structured_content["source_id"]
    queried = await server.call_tool(
        "query_data",
        {
            "session_id": session_id,
            "sql": f"SELECT avg(value) AS mean FROM source('{source_id}')",
        },
    )
    assert queried.structured_content is not None
    execution_id = queried.structured_content["execution_id"]

    fact = await server.call_tool(
        "register_evidence",
        {
            "session_id": session_id,
            "classification": "derived_fact",
            "claim": "The observed mean was 2.0.",
            "source_ids": [source_id],
            "execution_ids": [execution_id],
            "value": {"mean": 2.0},
        },
    )
    assert fact.structured_content is not None
    fact_id = fact.structured_content["id"]
    interpretation = await server.call_tool(
        "register_evidence",
        {
            "session_id": session_id,
            "classification": "interpretation",
            "claim": "The observed values center on 2.0.",
            "evidence_ids": [fact_id],
        },
    )
    assert interpretation.structured_content is not None
    assert interpretation.structured_content["evidence_ids"] == [fact_id]

    listed = await server.call_tool(
        "list_evidence",
        {"session_id": session_id, "classification": "derived_fact"},
    )
    assert listed.structured_content is not None
    assert listed.structured_content["count"] == 1
    assert listed.structured_content["evidence"][0]["execution_ids"] == [execution_id]
