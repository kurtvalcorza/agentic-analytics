from pathlib import Path

import pytest

from agentic_analytics.execution_backends import SubprocessDevBackend
from agentic_analytics.models import AnalysisSession, SessionMode
from agentic_analytics.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    SourceRepository,
)
from agentic_analytics.services.artifact_registry import ArtifactRegistry
from agentic_analytics.services.execution import ExecutionPolicyError, ExecutionService
from agentic_analytics.services.workspace import WorkspaceService
from agentic_analytics.settings import Settings


def _service(tmp_path: Path) -> tuple[ExecutionService, AnalysisSession]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        state_dir=tmp_path / "state",
        allowed_workspace_roots=[workspace],
        execution_backend="subprocess_dev",
    )
    service = ExecutionService(
        SubprocessDevBackend(),
        ExecutionRepository(settings.state_dir),
        SourceRepository(settings.state_dir),
        ArtifactRegistry(ArtifactRepository(settings.state_dir)),
        WorkspaceService([workspace]),
        settings,
    )
    return service, AnalysisSession(workspace_root=str(workspace), mode=SessionMode.PERMISSIVE)


def test_dev_backend_executes_only_permissive_sessions(tmp_path: Path) -> None:
    service, permissive = _service(tmp_path)
    record = service.execute_python(permissive, "print(2 + 2)", timeout_seconds=5)
    assert record.status.value == "succeeded"
    assert record.stdout_preview.strip() == "4"

    strict = AnalysisSession(workspace_root=permissive.workspace_root, mode=SessionMode.STRICT)
    with pytest.raises(ExecutionPolicyError):
        service.execute_python(strict, "print('no')", timeout_seconds=5)
