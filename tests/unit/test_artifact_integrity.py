"""INT-04 / OPS-02: archived artifact bytes always match their recorded digest."""

import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.repositories import ArtifactRepository
from agentic_analytics.services import artifact_registry as registry_module
from agentic_analytics.services.artifact_registry import (
    ArtifactIntegrityError,
    ArtifactRegistry,
    snapshot_workspace,
)
from agentic_analytics.settings import Settings

SES = new_id(EntityType.SESSION)
EXE = new_id(EntityType.EXECUTION)


def _registry(tmp_path: Path) -> tuple[ArtifactRegistry, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    return ArtifactRegistry(ArtifactRepository(state), state / "artifacts"), workspace


def test_snapshot_detects_rewrite_preserving_size_and_mtime(tmp_path: Path) -> None:
    _, workspace = _registry(tmp_path)
    target = workspace / "out.csv"
    target.write_bytes(b"a,b\n1,2\n")
    original = target.stat()
    before = snapshot_workspace(workspace)
    time.sleep(0.05)
    target.write_bytes(b"a,b\n9,9\n")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    after = snapshot_workspace(workspace)

    assert target.stat().st_size == original.st_size
    assert target.stat().st_mtime_ns == original.st_mtime_ns
    assert before["out.csv"] != after["out.csv"]


def test_archived_bytes_match_recorded_digest_and_are_private(tmp_path: Path) -> None:
    registry, workspace = _registry(tmp_path)
    before = snapshot_workspace(workspace)
    (workspace / "result.csv").write_bytes(b"x\n1\n2\n")
    after = snapshot_workspace(workspace)

    [artifact] = registry.register_changes(SES, EXE, workspace, before, after)

    archived = registry.archived_path(artifact)
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == artifact.sha256
    assert archived.stat().st_size == artifact.size_bytes
    if os.name == "posix":
        assert stat.S_IMODE(archived.stat().st_mode) == 0o600
        assert stat.S_IMODE(archived.parent.stat().st_mode) == 0o700
    # No temporary partial files are left next to the published archive.
    assert [path.name for path in archived.parent.iterdir()] == ["result.csv"]


def test_file_modified_after_snapshot_is_refused(tmp_path: Path) -> None:
    registry, workspace = _registry(tmp_path)
    before = snapshot_workspace(workspace)
    target = workspace / "result.csv"
    target.write_bytes(b"x\n1\n")
    after = snapshot_workspace(workspace)
    time.sleep(0.05)
    target.write_bytes(b"x\n2\n")

    with pytest.raises(ArtifactIntegrityError, match="changed after execution"):
        registry.register_changes(SES, EXE, workspace, before, after)
    assert registry.repository.list(SES) == []
    assert not any(registry.archive_base(SES, EXE).rglob("*.csv"))


def test_file_modified_during_copy_is_refused_and_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, workspace = _registry(tmp_path)
    before = snapshot_workspace(workspace)
    target = workspace / "result.csv"
    target.write_bytes(b"x" * 10_000)
    after = snapshot_workspace(workspace)
    monkeypatch.setattr(registry_module, "_CHUNK", 1024)
    original = ArtifactRegistry._read_chunks

    def mutate_mid_copy(stream: Any) -> Any:
        for index, chunk in enumerate(original(stream)):
            if index == 1:
                time.sleep(0.05)
                with target.open("r+b") as writer:
                    writer.write(b"y" * 1024)
            yield chunk

    monkeypatch.setattr(ArtifactRegistry, "_read_chunks", staticmethod(mutate_mid_copy))

    with pytest.raises(ArtifactIntegrityError, match="while it was being archived"):
        registry.register_changes(SES, EXE, workspace, before, after)
    assert registry.repository.list(SES) == []
    archive = registry.archive_base(SES, EXE)
    assert not archive.exists() or list(archive.rglob("*")) == []


def test_discard_created_files_keeps_preexisting_files(tmp_path: Path) -> None:
    registry, workspace = _registry(tmp_path)
    (workspace / "keep.csv").write_bytes(b"keep")
    before = snapshot_workspace(workspace)
    (workspace / "keep.csv").write_bytes(b"modified")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "new.bin").write_bytes(b"x" * 100)
    after = snapshot_workspace(workspace)

    freed = registry.discard_created_files(workspace, before, after)

    assert freed == 100
    assert not (workspace / "nested" / "new.bin").exists()
    assert (workspace / "keep.csv").read_bytes() == b"modified"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_state_directory_is_private(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", allowed_workspace_roots=[tmp_path])
    settings.ensure_directories()

    assert stat.S_IMODE(settings.state_dir.stat().st_mode) == 0o700
