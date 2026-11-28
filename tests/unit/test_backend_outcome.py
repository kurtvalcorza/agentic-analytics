"""C7 + C12: one typed backend Outcome, and output rollback behind the backend protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.execution_backends import SubprocessDevBackend
from agentic_analytics.execution_backends.base import BackendResult, Outcome
from agentic_analytics.execution_backends.guards import (
    DiskLimits,
    WorkspaceDiskGuard,
    disk_breach_outcome,
)
from agentic_analytics.models import AnalysisSession, ExecutionStatus, SessionMode
from agentic_analytics.repositories import ArtifactRepository, ExecutionRepository, SourceRepository
from agentic_analytics.services.artifact_registry import ArtifactRegistry
from agentic_analytics.services.execution import ExecutionService
from agentic_analytics.services.workspace import WorkspaceService
from agentic_analytics.settings import Settings


def _result(outcome: Outcome, stderr: str = "captured") -> BackendResult:
    return outcome.result(
        stdout="out",
        stderr=stderr,
        runtime={"backend": "x", "image": "i"},
        stdout_truncated=False,
        stderr_truncated=True,
    )


def test_server_stderr_replaces_captured_and_runtime_facts_merge() -> None:
    result = _result(
        Outcome(
            status=ExecutionStatus.FAILED,
            exit_code=3,
            stderr="execution terminated: quota",
            resource_limit="disk",
            runtime={"resource_limit": "disk", "image": "override"},
            output_paths=("/w/outputs/a.csv",),
        )
    )

    assert result.stderr == "execution terminated: quota"
    assert result.runtime == {"backend": "x", "image": "override", "resource_limit": "disk"}
    assert (result.status, result.exit_code, result.resource_limit) == (
        ExecutionStatus.FAILED,
        3,
        "disk",
    )
    assert result.output_paths == ("/w/outputs/a.csv",)
    assert (result.stdout, result.stdout_truncated, result.stderr_truncated) == ("out", False, True)


def test_stderr_fallback_applies_only_when_nothing_was_captured() -> None:
    outcome = Outcome(status=ExecutionStatus.TIMED_OUT, stderr_fallback="exceeded 5 seconds")

    assert _result(outcome).stderr == "captured"
    assert _result(outcome, stderr="").stderr == "exceeded 5 seconds"


def test_disk_breach_outcome_reclaims_and_reports(tmp_path: Path) -> None:
    guard = WorkspaceDiskGuard(
        tmp_path, DiskLimits(max_output_bytes=10, max_output_files=5, min_free_bytes=0), print
    )
    (tmp_path / "big.bin").write_bytes(b"x" * 1000)
    assert guard.final_check() is not None

    outcome = disk_breach_outcome(guard, exit_code=0)

    assert not (tmp_path / "big.bin").exists()
    assert outcome.status is ExecutionStatus.FAILED and outcome.exit_code == 0
    assert outcome.resource_limit == "disk"
    assert outcome.stderr is not None and outcome.stderr.startswith("execution terminated:")
    assert outcome.runtime == {"resource_limit": "disk", "retained_output_bytes": 0}


class _CopyingBackend:
    """Reports one copied-in output, then records the rollback the service asks for."""

    name = "copying"
    conformant = True

    def __init__(self) -> None:
        self.rolled_back: list[tuple[BackendResult, Path]] = []

    def execute(self, session: Any, script_path: Path, timeout_seconds: int, **_: Any):
        return BackendResult(status=ExecutionStatus.SUCCEEDED, output_paths=("/w/outputs/x",))

    def rollback_outputs(self, result: BackendResult, script_path: Path) -> None:
        self.rolled_back.append((result, script_path))

    def close_session(self, session_id: str) -> None:
        return None


def test_failed_registration_asks_the_backend_to_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(state_dir=tmp_path / "state", allowed_workspace_roots=[workspace])
    backend = _CopyingBackend()
    registry = ArtifactRegistry(
        ArtifactRepository(settings.state_dir), settings.state_dir / "artifacts"
    )
    service = ExecutionService(
        backend,
        ExecutionRepository(settings.state_dir),
        SourceRepository(settings.state_dir),
        registry,
        WorkspaceService([workspace]),
        settings,
    )

    def refuse(*_: Any, **__: Any) -> list[Any]:
        raise OSError("disk full while archiving")

    monkeypatch.setattr(registry, "register_changes", refuse)
    session = AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)

    with pytest.raises(OSError, match="disk full"):
        service.execute_python(session, "print(1)", timeout_seconds=5)

    assert len(backend.rolled_back) == 1
    result, script_path = backend.rolled_back[0]
    assert result.output_paths == ("/w/outputs/x",)
    assert script_path.name == "request.py"


def test_dev_backend_rollback_is_a_no_op(tmp_path: Path) -> None:
    SubprocessDevBackend().rollback_outputs(
        BackendResult(status=ExecutionStatus.FAILED), tmp_path / "request.py"
    )
