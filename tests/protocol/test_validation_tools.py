from pathlib import Path

import pytest

from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


@pytest.mark.anyio
async def test_unlinked_material_claim_cannot_validate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("value\n1\n2\n", encoding="utf-8")
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

    result = await server.call_tool(
        "validate_analysis",
        {
            "session_id": session_id,
            "claim_texts": ["This final claim has no evidence."],
            "checks": ["evidence_coverage"],
        },
    )
    assert result.structured_content is not None
    assert result.structured_content["status"] == "blocked"
    assert result.structured_content["checks_run"] == ["evidence_coverage"]
    codes = {finding["code"] for finding in result.structured_content["findings"]}
    assert "MISSING_MATERIAL_EVIDENCE" in codes
