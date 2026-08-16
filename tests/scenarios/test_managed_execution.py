from pathlib import Path

from agentic_analytics.models import AnalysisSession, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.settings import Settings


def _runtime(tmp_path: Path, workspace: Path, *, timeout: int = 5) -> Runtime:
    return Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            workspace_base_dir=tmp_path / "managed",
            allowed_workspace_roots=[workspace],
            execution_backend="docker",
            docker_image="agentic-analytics-exec:test",
            execution_timeout_seconds=timeout,
            max_execution_timeout_seconds=max(timeout, 5),
        )
    )


def test_managed_execution_generates_hashed_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace)
    session = AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    runtime.sessions.add(session)
    code = """
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
Path('outputs').mkdir(exist_ok=True)
pd.DataFrame({'x': [1, 2], 'y': [3, 4]}).to_csv('outputs/result.csv', index=False)
plt.plot([1, 2], [3, 4])
plt.savefig('outputs/chart.png')
print('done')
"""
    try:
        record = runtime.execution.execute_python(session, code, timeout_seconds=10)
        assert record.status.value == "succeeded"
        assert record.stdout_preview.strip() == "done"
        assert len(record.artifact_ids) == 2
        artifacts = runtime.artifacts.list(session.id)
        assert {artifact.kind.value for artifact in artifacts} == {"chart", "dataset"}
        assert all(len(artifact.sha256) == 64 for artifact in artifacts)
        assert all(
            artifact.relative_path.startswith(".agentic-analytics/artifacts/")
            for artifact in artifacts
        )
    finally:
        runtime.execution_backend.close_session(session.id)


def test_managed_execution_timeout_destroys_runaway_container(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace, timeout=1)
    session = AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    runtime.sessions.add(session)
    record = runtime.execution.execute_python(
        session, "while True:\n    pass\n", timeout_seconds=1
    )
    assert record.status.value == "timed_out"
