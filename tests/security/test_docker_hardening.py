"""SEC-01 / SEC-03 / OPS-06 for the Docker backend."""

import socket
from pathlib import Path

import pytest

from agentic_analytics.execution_backends import docker as docker_module
from agentic_analytics.execution_backends.docker import (
    DEFAULT_CONTAINER_USER,
    DockerBackend,
    DockerExecutionError,
)
from agentic_analytics.models import AnalysisSession
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


# The polling output budget tests were superseded by the SEC-01 quota tmpfs: see
# tests/security/test_output_quota.py.
