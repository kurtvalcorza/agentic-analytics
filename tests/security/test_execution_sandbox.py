from pathlib import Path

from agentic_analytics.models import AnalysisSession, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.settings import Settings


def test_docker_execution_isolates_host_secrets_network_and_other_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    other_secret = workspace_b / "other-secret.txt"
    other_secret.write_text("OTHER_SESSION_SECRET", encoding="utf-8")
    monkeypatch.setenv("AGENTIC_ANALYTICS_TEST_SECRET", "HOST_SECRET")
    runtime = Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[tmp_path],
            execution_backend="docker",
            docker_image="agentic-analytics-exec:test",
            execution_timeout_seconds=5,
            max_execution_timeout_seconds=5,
        )
    )
    session = AnalysisSession(workspace_root=str(workspace_a), mode=SessionMode.STRICT)
    runtime.sessions.add(session)
    code = f"""
import os
import pathlib
import socket
print('secret=' + str(os.getenv('AGENTIC_ANALYTICS_TEST_SECRET')))
print('other=' + str(pathlib.Path({str(other_secret)!r}).exists()))
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
    print('network=open')
except OSError:
    print('network=blocked')
"""
    try:
        record = runtime.execution.execute_python(session, code, timeout_seconds=5)
        assert record.status.value == "succeeded"
        assert "secret=None" in record.stdout_preview
        assert "other=False" in record.stdout_preview
        assert "network=blocked" in record.stdout_preview
        assert "HOST_SECRET" not in record.stdout_preview
        assert "OTHER_SESSION_SECRET" not in record.stdout_preview
    finally:
        runtime.execution_backend.close_session(session.id)
