from pathlib import Path
import pytest
from agentic_analytics.services.workspace import Workspace

def test_traversal_and_absolute_paths_are_rejected(workspace: Path):
    ws = Workspace(workspace)
    with pytest.raises(PermissionError): ws.resolve("../escape")
    with pytest.raises(PermissionError): ws.resolve("/etc/passwd")

def test_symlink_escape_is_rejected(workspace: Path):
    (workspace / "escape").symlink_to("/etc/passwd")
    with pytest.raises(PermissionError): Workspace(workspace).resolve("escape")
