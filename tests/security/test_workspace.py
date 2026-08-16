from pathlib import Path

import pytest

from agentic_analytics.services.workspace import WorkspaceAuthorizationError, WorkspaceService


def test_workspace_rejects_parent_and_absolute_source_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("x\n1\n", encoding="utf-8")
    service = WorkspaceService([workspace])
    with pytest.raises(WorkspaceAuthorizationError):
        service.resolve_file(workspace, "../outside.csv")
    with pytest.raises(WorkspaceAuthorizationError):
        service.resolve_file(workspace, str((workspace / "data.csv").resolve()))


def test_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n1\n", encoding="utf-8")
    link = workspace / "escape.csv"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    service = WorkspaceService([workspace])
    with pytest.raises(WorkspaceAuthorizationError):
        service.resolve_file(workspace, "escape.csv")
    assert service.discover(workspace) == []
