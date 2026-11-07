"""SEC-01 / SEC-03 / OPS-06 for the Docker backend."""

import os
import socket
from pathlib import Path

import pytest

from agentic_analytics.execution_backends import docker as docker_module
from agentic_analytics.execution_backends.docker import (
    DEFAULT_CONTAINER_USER,
    DockerBackend,
    DockerExecutionError,
)
from agentic_analytics.models import AnalysisSession, ExecutionStatus, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.settings import Settings


def _user(args: list[str]) -> str:
    return args[args.index("--user") + 1]


def test_posix_server_identity_is_passed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_module.os, "name", "posix")
    monkeypatch.setattr(docker_module.os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(docker_module.os, "getgid", lambda: 4321, raising=False)

    assert DockerBackend("img")._user_args() == ["--user", "1234:4321"]


def test_root_server_is_refused_rather_than_running_container_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_module.os, "name", "posix")
    monkeypatch.setattr(docker_module.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(docker_module.os, "getgid", lambda: 0, raising=False)

    with pytest.raises(DockerExecutionError, match="container root"):
        DockerBackend("img")._user_args()
    # An operator can opt into an explicit unprivileged identity instead.
    assert DockerBackend("img", user="2000:2000")._user_args() == ["--user", "2000:2000"]


def test_explicit_uid_zero_and_malformed_users_are_refused() -> None:
    with pytest.raises(DockerExecutionError, match="container root"):
        DockerBackend("img", user="0:0")._user_args()
    with pytest.raises(ValueError):
        DockerBackend("img", user="root")


def test_non_posix_hosts_use_fixed_unprivileged_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_module.os, "name", "nt")

    assert DockerBackend("img")._user_args() == ["--user", DEFAULT_CONTAINER_USER]
    assert not DEFAULT_CONTAINER_USER.startswith("0:")


def test_settings_reject_malformed_docker_user(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path / "state", docker_user="root:root")


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix sockets")
def test_workspace_with_unix_socket_is_not_mounted(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "nested").mkdir(parents=True)
    script = tmp_path / "request.py"
    script.write_text("print('x')", encoding="utf-8")
    # Short path: AF_UNIX socket paths are length limited.
    sock_path = workspace / "nested" / "d.sock"
    if len(str(sock_path)) > 100:
        pytest.skip("temporary path too long for a Unix socket")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        session = AnalysisSession(workspace_root=str(workspace))
        with pytest.raises(DockerExecutionError, match="socket or device"):
            DockerBackend("img", user="1000:1000")._run_args(session, "c", script)
    finally:
        server.close()


@pytest.mark.skipif(os.name != "posix", reason="Docker integration runs on Linux CI")
def test_docker_output_budget_terminates_container_before_disk_exhaustion(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.csv").write_text("x\n1\n", encoding="utf-8")
    runtime = Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="docker",
            docker_image="agentic-analytics-exec:test",
            max_execution_output_bytes=1024 * 1024,
            min_free_disk_bytes=0,
        )
    )
    session = runtime.sessions.add(
        AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    )
    code = """
import os, time
uid = os.getuid()
print('uid=' + str(uid), flush=True)
with open('fill.bin', 'wb') as stream:
    for _ in range(100000):
        stream.write(b'x' * 65536)
        stream.flush()
        time.sleep(0.001)
"""
    try:
        record = runtime.execution.execute_python(session, code, timeout_seconds=60)
    finally:
        runtime.execution_backend.close_session(session.id)

    assert record.status is ExecutionStatus.FAILED
    assert record.error is not None and record.error["type"] == "resource_limit_exceeded"
    assert "uid=0" not in record.stdout_preview
    assert not (workspace / "fill.bin").exists()
    assert (workspace / "input.csv").read_text(encoding="utf-8") == "x\n1\n"


@pytest.mark.skipif(os.name != "posix", reason="Docker integration runs on Linux CI")
def test_docker_burst_writer_exiting_before_first_poll_is_reclaimed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="docker",
            docker_image="agentic-analytics-exec:test",
            max_execution_output_bytes=1024 * 1024,
            min_free_disk_bytes=0,
        )
    )
    session = runtime.sessions.add(
        AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    )
    code = "open('burst.bin', 'wb').write(b'x' * (4 * 1024 * 1024))"
    try:
        record = runtime.execution.execute_python(session, code, timeout_seconds=60)
    finally:
        runtime.execution_backend.close_session(session.id)

    assert record.error is not None and record.error["type"] == "resource_limit_exceeded"
    assert not (workspace / "burst.bin").exists()
