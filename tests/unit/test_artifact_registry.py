from pathlib import Path

import pytest

from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.repositories import ArtifactRepository
from agentic_analytics.services.artifact_registry import (
    ArtifactLimitError,
    ArtifactRegistry,
    snapshot_workspace,
)


def _registry(tmp_path: Path, **kwargs) -> ArtifactRegistry:
    return ArtifactRegistry(
        ArtifactRepository(tmp_path / "state"), tmp_path / "state" / "artifacts", **kwargs
    )


def _ids() -> tuple[str, str]:
    return new_id(EntityType.SESSION), new_id(EntityType.EXECUTION)


def test_registry_archives_outside_workspace_with_explicit_media_type(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    (workspace / "result.parquet").write_bytes(b"PAR1data")
    after = snapshot_workspace(workspace)
    registry = _registry(tmp_path)
    session_id, execution_id = _ids()

    artifacts = registry.register_changes(session_id, execution_id, workspace, before, after)

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.relative_path == "result.parquet"
    assert artifact.media_type == "application/vnd.apache.parquet"
    archived = registry.archived_path(artifact)
    assert archived.is_file()
    assert workspace not in archived.parents


def test_registry_enforces_artifact_count_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    for index in range(3):
        (workspace / f"f{index}.csv").write_text("x\n", encoding="utf-8")
    after = snapshot_workspace(workspace)
    session_id, execution_id = _ids()

    with pytest.raises(ArtifactLimitError):
        _registry(tmp_path, max_artifacts=2).register_changes(
            session_id, execution_id, workspace, before, after
        )


def test_registry_enforces_total_byte_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    (workspace / "big.csv").write_text("x" * 4096, encoding="utf-8")
    after = snapshot_workspace(workspace)
    session_id, execution_id = _ids()

    with pytest.raises(ArtifactLimitError):
        _registry(tmp_path, max_total_bytes=1024).register_changes(
            session_id, execution_id, workspace, before, after
        )


def test_register_file_enforces_per_file_byte_limit(tmp_path: Path) -> None:
    registry = _registry(tmp_path, max_artifact_bytes=1024)
    session_id, execution_id = _ids()
    archive_base = registry.archive_base(session_id, execution_id)
    archive_base.mkdir(parents=True)
    spill = archive_base / "query-result.parquet"
    spill.write_bytes(b"x" * 4096)

    # A pre-written file over the per-file ceiling must be refused, not silently registered.
    with pytest.raises(ArtifactLimitError, match="per-file limit"):
        registry.register_file(session_id, execution_id, spill)


def test_register_file_enforces_session_total_byte_limit(tmp_path: Path) -> None:
    registry = _registry(tmp_path, max_total_bytes=6144)
    session_id, execution_id = _ids()
    archive_base = registry.archive_base(session_id, execution_id)
    archive_base.mkdir(parents=True)
    first = archive_base / "a.parquet"
    first.write_bytes(b"x" * 4096)
    registry.register_file(session_id, execution_id, first)

    second = archive_base / "b.parquet"
    second.write_bytes(b"x" * 4096)
    # The cumulative session artifact budget is enforced across registrations.
    with pytest.raises(ArtifactLimitError, match="session"):
        registry.register_file(session_id, execution_id, second)


def test_spill_byte_ceiling_reflects_remaining_session_budget(tmp_path: Path) -> None:
    registry = _registry(tmp_path, max_artifact_bytes=8192, max_total_bytes=10240)
    session_id, execution_id = _ids()
    archive_base = registry.archive_base(session_id, execution_id)
    archive_base.mkdir(parents=True)

    # With nothing registered, the ceiling is the per-file cap.
    assert registry.spill_byte_ceiling(session_id) == 8192

    first = archive_base / "a.parquet"
    first.write_bytes(b"x" * 6144)
    registry.register_file(session_id, execution_id, first)

    # After consuming 6 KiB of a 10 KiB session budget, only 4 KiB remains — the ceiling drops
    # below the per-file cap so a watchdog interrupts before over-writing.
    assert registry.spill_byte_ceiling(session_id) == 10240 - 6144


def test_snapshot_ignores_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    link = workspace / "leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    # A symlink created by managed code never enters the change set, so its target cannot be
    # copied into the archive.
    assert "leak.txt" not in snapshot_workspace(workspace)


def test_registry_rejects_symlinked_archive_destination(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    before = snapshot_workspace(workspace)
    (workspace / "sub" / "x.csv").write_text("x\n", encoding="utf-8")
    after = snapshot_workspace(workspace)
    registry = _registry(tmp_path)
    session_id, execution_id = _ids()

    archive_base = registry.archive_base(session_id, execution_id)
    archive_base.mkdir(parents=True)
    escape = tmp_path / "escape"
    escape.mkdir()
    try:
        (archive_base / "sub").symlink_to(escape, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(ArtifactLimitError, match="escapes the archive root"):
        registry.register_changes(session_id, execution_id, workspace, before, after)
