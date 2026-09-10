import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest

from agentic_analytics.models import SessionStatus
from agentic_analytics.repositories import SessionRepository
from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings(
        state_dir=tmp_path / "state",
        workspace_base_dir=tmp_path / "generated",
        allowed_workspace_roots=[workspace],
        execution_backend="subprocess_dev",
    )


@pytest.mark.anyio
async def test_closed_session_cannot_access_reassigned_workspace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    server = build_server(settings)
    first = await server.call_tool("create_session", {"mode": "permissive"})
    old_id = first.structured_content["session_id"]
    await server.call_tool("close_session", {"session_id": old_id})
    second = await server.call_tool("create_session", {"mode": "permissive"})
    assert second.structured_content["session_id"] != old_id
    cases = [
        ("execute_python", {"code": "from pathlib import Path; Path('old-write').touch()"}),
        ("list_sources", {}),
        ("inspect_source", {"source": "data.csv"}),
        ("query_data", {"sql": "SELECT 1"}),
        ("register_evidence", {"classification": "source_fact", "claim": "old"}),
        ("validate_analysis", {}),
    ]
    for name, arguments in cases:
        with pytest.raises(Exception, match="session is closed"):
            await server.call_tool(name, {"session_id": old_id, **arguments})
    assert not (settings.allowed_workspace_roots[0] / "old-write").exists()
    historical = await server.call_tool("list_artifacts", {"session_id": old_id})
    assert historical.structured_content["count"] == 0


def test_reassignment_waits_for_admitted_operation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    server = build_server(settings)
    repo = SessionRepository(settings.state_dir)
    created = anyio.run(server.call_tool, "create_session", {"mode": "permissive"})
    session_id = created.structured_content["session_id"]
    admitted = threading.Event()
    release = threading.Event()

    def operation() -> None:
        with repo.active(session_id):
            admitted.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=3) as pool:
        held = pool.submit(operation)
        assert admitted.wait(5)
        closing = pool.submit(
            anyio.run, server.call_tool, "close_session", {"session_id": session_id}
        )
        try:
            deadline = time.monotonic() + 5
            while repo.get(session_id, session_id).status is SessionStatus.ACTIVE:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            creating = pool.submit(
                anyio.run, server.call_tool, "create_session", {"mode": "permissive"}
            )
            assert not closing.done()
            assert not creating.done()
        finally:
            release.set()
        held.result(timeout=5)
        closing.result(timeout=5)
        replacement = creating.result(timeout=5)
        assert replacement.structured_content["session_id"] != session_id


@pytest.mark.anyio
async def test_server_state_is_not_discoverable_or_inspectable(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "secret.csv").write_text("x\nsecret\n")
    server = build_server(
        Settings(
            state_dir=state,
            workspace_base_dir=tmp_path / "generated",
            allowed_workspace_roots=[tmp_path],
            execution_backend="subprocess_dev",
        )
    )
    with pytest.raises(Exception, match="inside server state"):
        await server.call_tool("create_session", {"workspace_root": str(state)})
    created = await server.call_tool("create_session", {"workspace_root": str(tmp_path)})
    session_id = created.structured_content["session_id"]
    listed = await server.call_tool("list_sources", {"session_id": session_id})
    assert listed.structured_content["count"] == 0
    with pytest.raises(Exception, match="server state"):
        await server.call_tool(
            "inspect_source", {"session_id": session_id, "source": "state/secret.csv"}
        )


def test_reassignment_drains_terminal_session_after_closer_crash(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    server = build_server(settings)
    repo = SessionRepository(settings.state_dir)
    created = anyio.run(server.call_tool, "create_session", {"mode": "permissive"})
    session_id = created.structured_content["session_id"]
    started = threading.Event()

    def replacement():
        started.set()
        return anyio.run(server.call_tool, "create_session", {"mode": "permissive"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with repo.active(session_id) as session:
            # Simulate the durable state left when a closer crashes before draining
            # an operation admitted by a different MCP server process.
            session.status = SessionStatus.COMPLETED
            repo.update(session)
            future = pool.submit(replacement)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        reopened = future.result(timeout=5)
        assert reopened.structured_content["session_id"] != session_id
