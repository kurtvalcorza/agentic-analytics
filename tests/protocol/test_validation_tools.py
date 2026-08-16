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


@pytest.mark.anyio
async def test_default_check_selector_and_bounded_findings(tmp_path: Path) -> None:
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

    # The documented "default" selector must be accepted (not only an array or null).
    result = await server.call_tool(
        "validate_analysis", {"session_id": session_id, "checks": "default"}
    )
    assert result.structured_content is not None
    assert "total_findings" in result.structured_content
    assert result.structured_content["findings_truncated"] is False


@pytest.mark.anyio
async def test_empty_check_set_is_rejected(tmp_path: Path) -> None:
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

    # An explicit empty check set runs no validator; it must not report a clean pass.
    with pytest.raises(Exception, match=r"at least one check|zero coverage"):
        await server.call_tool(
            "validate_analysis", {"session_id": session_id, "checks": []}
        )


@pytest.mark.anyio
async def test_scope_field_is_accepted_and_echoed(tmp_path: Path) -> None:
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

    # The canonical contract's `scope` field must be accepted and echoed back.
    result = await server.call_tool(
        "validate_analysis",
        {"session_id": session_id, "scope": "final", "checks": "default"},
    )
    assert result.structured_content is not None
    assert result.structured_content["scope"] == "final"

    interim = await server.call_tool(
        "validate_analysis",
        {"session_id": session_id, "scope": "interim", "checks": "default"},
    )
    assert interim.structured_content is not None
    assert interim.structured_content["scope"] == "interim"

    # An unknown scope is rejected before the run executes.
    with pytest.raises(Exception, match="scope must be one of"):
        await server.call_tool(
            "validate_analysis", {"session_id": session_id, "scope": "bogus"}
        )
