import hashlib
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("relative_state", ["state", "custom/state,files"])
def test_docker_hides_configured_state_and_preserves_existing_archives(
    tmp_path: Path, relative_state: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / relative_state
    runtime = Runtime.create(
        Settings(
            state_dir=state,
            allowed_workspace_roots=[workspace],
            execution_backend="docker",
            docker_image="agentic-analytics-exec:test",
        )
    )
    session = AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    runtime.sessions.add(session)
    sentinel = state / "sentinel.txt"
    sentinel.write_text("SERVER_PRIVATE", encoding="utf-8")
    try:
        first = runtime.execution.execute_python(
            session, "from pathlib import Path\nPath('result.txt').write_text('original')"
        )
        assert first.status.value == "succeeded", first.stderr_preview
        artifact = runtime.artifacts.get(session.id, first.artifact_ids[0])
        archive = runtime.artifact_registry.archived_path(artifact)
        archive_relative = archive.relative_to(workspace).as_posix()
        code = f"""
from pathlib import Path
root = Path({relative_state!r})
print('state_entries=' + str(list(root.iterdir())))
for label, path in [('state', root / 'sentinel.txt'), ('archive', Path({archive_relative!r})),
                    ('script', Path('/run/agentic-analytics/request.py'))]:
    try:
        path.write_text('corrupted')
        print(label + '=writable')
    except OSError:
        print(label + '=blocked')
Path('allowed-output.txt').write_text('allowed')
"""
        result = runtime.execution.execute_python(session, code)
        assert result.status.value == "succeeded", result.stderr_preview
        assert "state_entries=[]" in result.stdout_preview
        assert "state=blocked" in result.stdout_preview
        assert "archive=blocked" in result.stdout_preview
        assert "script=blocked" in result.stdout_preview
        assert sentinel.read_text(encoding="utf-8") == "SERVER_PRIVATE"
        assert archive.read_text(encoding="utf-8") == "original"
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == artifact.sha256
        assert len(result.artifact_ids) == 1
        assert (
            runtime.artifacts.get(session.id, result.artifact_ids[0]).relative_path
            == "allowed-output.txt"
        )
    finally:
        runtime.execution_backend.close_session(session.id)


def test_docker_registered_sources_and_hardlink_aliases_remain_readonly(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_path = workspace / "input,original.csv"
    source_bytes = b"x\n1\n2\n"
    source_path.write_bytes(source_bytes)
    (workspace / "alias.csv").hardlink_to(source_path)
    runtime = Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="docker",
            docker_image="agentic-analytics-exec:test",
        )
    )
    session = AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    runtime.sessions.add(session)
    source, _ = runtime.inspector.inspect(session, source_path.name)
    assert source.read_only
    code = """
from pathlib import Path
for name in ['input,original.csv', 'alias.csv']:
    path = Path(name)
    print(name + '=readable:' + str(path.read_text().startswith('x')))
    try:
        path.write_text('corrupted')
        print(name + '=writable')
    except OSError:
        print(name + '=blocked')
    try:
        path.unlink()
        print(name + '=deletable')
    except OSError:
        print(name + '=undeletable')
try:
    Path('new-alias.csv').hardlink_to('input,original.csv')
    print('new_alias=created')
except OSError:
    print('new_alias=blocked')
Path('result.txt').write_text('allowed')
"""
    try:
        # Omitting source_ids must not remove read-only protection from registered inputs.
        result = runtime.execution.execute_python(session, code)
        assert result.status.value == "succeeded", result.stderr_preview
        for name in (source_path.name, "alias.csv"):
            assert f"{name}=readable:True" in result.stdout_preview
            assert f"{name}=blocked" in result.stdout_preview
            assert f"{name}=undeletable" in result.stdout_preview
            assert (workspace / name).read_bytes() == source_bytes
        assert "new_alias=blocked" in result.stdout_preview
        assert len(result.artifact_ids) == 1
    finally:
        runtime.execution_backend.close_session(session.id)


def test_docker_protected_ancestors_cannot_be_renamed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = workspace / "shared/state-parent/state"
    source_path = workspace / "shared/source-parent/input.csv"
    alias = workspace / "aliases/deep/alias.csv"
    source_path.parent.mkdir(parents=True)
    alias.parent.mkdir(parents=True)
    source_bytes = b"x\n1\n2\n"
    source_path.write_bytes(source_bytes)
    alias.hardlink_to(source_path)
    runtime = Runtime.create(
        Settings(
            state_dir=state,
            allowed_workspace_roots=[workspace],
            execution_backend="docker",
            docker_image="agentic-analytics-exec:test",
        )
    )
    session = AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    runtime.sessions.add(session)
    sentinel = state / "sentinel.txt"
    sentinel.write_bytes(b"SERVER_PRIVATE")
    runtime.inspector.inspect(session, source_path.relative_to(workspace).as_posix())
    ancestors = ["shared/state-parent", "shared/source-parent", "shared", "aliases/deep", "aliases"]
    code = f"""
from pathlib import Path
for index, name in enumerate({ancestors!r}):
    try:
        Path(name).rename('moved-' + str(index))
        print(name + '=movable')
    except OSError as exc:
        print(name + '=' + str(exc.errno))
Path('shared/allowed-output.txt').write_text('allowed')
Path('shared/source-parent/allowed-output.txt').write_text('allowed')
Path('aliases/deep/allowed-output.txt').write_text('allowed')
"""
    try:
        record = runtime.execution.execute_python(session, code)
        assert record.status.value == "succeeded", record.stderr_preview
        for name in ancestors:
            # Linux EBUSY proves the path itself is a mount boundary, rather than a
            # failed rename caused by an earlier rename making this path disappear.
            assert f"{name}=16" in record.stdout_preview
        assert sentinel.read_bytes() == b"SERVER_PRIVATE"
        assert source_path.read_bytes() == source_bytes
        assert alias.read_bytes() == source_bytes
        assert {item.relative_path for item in runtime.artifacts.list(session.id)} == {
            "shared/allowed-output.txt",
            "shared/source-parent/allowed-output.txt",
            "aliases/deep/allowed-output.txt",
        }
    finally:
        runtime.execution_backend.close_session(session.id)
