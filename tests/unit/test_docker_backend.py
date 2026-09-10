import csv
import io
import threading
from pathlib import Path

import pytest

from agentic_analytics.execution_backends.docker import DockerBackend, DockerExecutionError
from agentic_analytics.models import AnalysisSession


def _mounts(args: list[str]) -> list[list[str]]:
    return [next(csv.reader([args[i + 1]])) for i, value in enumerate(args) if value == "--mount"]


def _setup(tmp_path: Path) -> tuple[Path, Path, AnalysisSession]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = tmp_path / "request.py"
    script.write_text("print('ready')", encoding="utf-8")
    return workspace, script, AnalysisSession(workspace_root=str(workspace))


@pytest.mark.parametrize(
    "relative_state", ["state", "custom/state,files", ".agentic-analytics/state"]
)
def test_masks_actual_nested_state_and_mounts_external_script(
    tmp_path: Path, relative_state: str
) -> None:
    workspace, script, session = _setup(tmp_path)
    state = workspace / relative_state
    state.mkdir(parents=True)
    backend = DockerBackend("test", protected_state_root=state)

    args = backend._run_args(session, "test", script)

    assert [
        "type=tmpfs",
        f"dst=/workspace/{relative_state}",
        "tmpfs-size=1048576",
        "readonly",
    ] in _mounts(args)
    assert [
        "type=bind",
        f"src={script}",
        "dst=/run/agentic-analytics/request.py",
        "readonly",
    ] in _mounts(args)
    assert args[-1] == "/run/agentic-analytics/request.py"


def test_state_outside_workspace_is_not_mounted(tmp_path: Path) -> None:
    _, script, session = _setup(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    args = DockerBackend("test", protected_state_root=state)._run_args(session, "test", script)
    assert not any("type=tmpfs" in mount for mount in _mounts(args))


@pytest.mark.parametrize("is_nested", [False, True])
def test_rejects_workspace_inside_state(tmp_path: Path, is_nested: bool) -> None:
    workspace, script, session = _setup(tmp_path)
    state = tmp_path if is_nested else workspace
    with pytest.raises(DockerExecutionError, match="workspace must not be inside"):
        DockerBackend("test", protected_state_root=state)._run_args(session, "test", script)


def test_sources_and_existing_hardlinks_are_readonly(tmp_path: Path) -> None:
    workspace, script, session = _setup(tmp_path)
    source = workspace / "source,one.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    alias = workspace / "alias.csv"
    alias.hardlink_to(source)
    args = DockerBackend("test")._run_args(session, "test", script, readonly_paths=(source,))

    for path in (source, alias):
        assert ["type=bind", f"src={path}", f"dst=/workspace/{path.name}", "readonly"] in _mounts(
            args
        )


def test_state_hardlink_outside_mask_is_rejected(tmp_path: Path) -> None:
    workspace, script, session = _setup(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    record = state / "record.json"
    record.write_text("{}", encoding="utf-8")
    (workspace / "exposed.json").hardlink_to(record)

    with pytest.raises(DockerExecutionError, match="hardlink to protected state"):
        DockerBackend("test", protected_state_root=state)._run_args(session, "test", script)


@pytest.mark.parametrize(
    "location",
    ["outside.csv", "workspace/state/source.csv", "workspace/.agentic-analytics/source.csv"],
)
def test_rejects_source_mounts_outside_workspace_or_overlapping_runtime(
    tmp_path: Path, location: str
) -> None:
    workspace, script, session = _setup(tmp_path)
    source = tmp_path / location
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("x\n1\n", encoding="utf-8")
    with pytest.raises(DockerExecutionError):
        DockerBackend("test", protected_state_root=workspace / "state")._run_args(
            session, "test", script, readonly_paths=(source,)
        )


def test_rejects_source_hardlink_to_script(tmp_path: Path) -> None:
    workspace, script, session = _setup(tmp_path)
    source = workspace / "source.csv"
    source.hardlink_to(script)
    with pytest.raises(DockerExecutionError, match="overlaps the execution script"):
        DockerBackend("test")._run_args(session, "test", script, readonly_paths=(source,))


def test_protected_ancestor_mounts_precede_leaf_mounts_without_duplicates(tmp_path: Path) -> None:
    workspace, script, session = _setup(tmp_path)
    state = workspace / "shared/nested/state"
    state.mkdir(parents=True)
    source = workspace / "shared/nested/input.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    mounts = _mounts(
        DockerBackend("test", protected_state_root=state)._run_args(
            session, "test", script, readonly_paths=(source,)
        )
    )
    shared_mount = ["type=bind", f"src={workspace / 'shared'}", "dst=/workspace/shared"]
    nested_mount = [
        "type=bind",
        f"src={workspace / 'shared/nested'}",
        "dst=/workspace/shared/nested",
    ]
    state_mount = [
        "type=tmpfs",
        "dst=/workspace/shared/nested/state",
        "tmpfs-size=1048576",
        "readonly",
    ]
    source_mount = [
        "type=bind",
        f"src={source}",
        "dst=/workspace/shared/nested/input.csv",
        "readonly",
    ]
    assert mounts.count(shared_mount) == mounts.count(nested_mount) == 1
    assert mounts.index(shared_mount) < mounts.index(nested_mount) < mounts.index(state_mount)
    assert mounts.index(nested_mount) < mounts.index(source_mount)


@pytest.mark.parametrize("size", [0, 1023, 1024, 1025, 200000])
def test_drain_tracks_overflow_without_retaining_it(size: int) -> None:
    backend = DockerBackend("test", max_output_chars=1024)
    stream = io.StringIO("x" * size)
    buffer: list[str] = []
    truncated = threading.Event()

    backend._drain(stream, buffer, truncated)

    assert "".join(buffer) == "x" * min(size, 1024)
    assert truncated.is_set() is (size > 1024)
    assert stream.closed
