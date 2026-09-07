from pathlib import Path

import pytest

from agentic_analytics.models import AnalysisSession, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.services.artifact_registry import ArtifactLimitError, snapshot_workspace
from agentic_analytics.services.workspace import WorkspaceAuthorizationError
from agentic_analytics.settings import Settings


def _runtime(tmp_path: Path) -> tuple[Runtime, AnalysisSession, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            workspace_base_dir=tmp_path / "generated",
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
            max_artifact_bytes=1024,
        )
    )
    return (
        runtime,
        AnalysisSession(workspace_root=str(workspace), mode=SessionMode.PERMISSIVE),
        workspace,
    )


def test_script_is_written_in_server_storage_not_workspace(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    record = runtime.execution.execute_python(session, "print(__file__)")
    script = Path(record.stdout_preview.strip())
    assert script.is_relative_to(runtime.settings.state_dir.resolve())
    assert not script.is_relative_to(workspace.resolve())
    assert not script.exists()


def test_workspace_tmp_symlink_is_never_followed(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    internal = workspace / ".agentic-analytics"
    internal.mkdir()
    try:
        (internal / "tmp").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    record = runtime.execution.execute_python(session, "print(__file__)")
    assert record.status.value == "succeeded"
    assert Path(record.stdout_preview.strip()).is_relative_to(runtime.settings.state_dir.resolve())
    assert list(outside.iterdir()) == []


def test_source_free_execution_rejects_revoked_workspace(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    runtime.workspace.allowed_roots = [elsewhere]
    with pytest.raises(WorkspaceAuthorizationError, match="outside authorized"):
        runtime.execution.execute_python(session, "from pathlib import Path; Path('ran').touch()")
    assert not (workspace / "ran").exists()


def test_artifact_rejection_persists_terminal_execution(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = "from pathlib import Path; Path('big.txt').write_text('x' * 2048)"
    with pytest.raises(ArtifactLimitError):
        runtime.execution.execute_python(session, code)
    records = runtime.executions.list(session.id)
    assert len(records) == 1
    record = records[0]
    assert record.status.value == "failed"
    assert record.request["code"] == code
    assert record.error["type"] == "ArtifactLimitError"
    assert record.started_at <= record.completed_at
    assert (workspace / "big.txt").stat().st_size == 2048


def test_execution_timestamps_include_running_time(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path)
    record = runtime.execution.execute_python(session, "import time; time.sleep(0.05)")
    assert record.completed_at is not None
    assert (record.completed_at - record.started_at).total_seconds() >= 0.05


def test_snapshot_excludes_configured_state_tree(tmp_path: Path) -> None:
    state = tmp_path / "custom-state"
    state.mkdir()
    (state / "record.json").write_text("{}")
    (tmp_path / "output.txt").write_text("output")
    assert set(snapshot_workspace(tmp_path, excluded_roots=(state,))) == {"output.txt"}
