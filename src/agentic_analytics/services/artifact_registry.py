from __future__ import annotations

import hashlib
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_analytics.models import Artifact, ArtifactKind
from agentic_analytics.repositories import ArtifactRepository

_INTERNAL_DIR = ".agentic-analytics"


@dataclass(frozen=True, slots=True)
class FileState:
    size_bytes: int
    mtime_ns: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_workspace(workspace_root: Path) -> dict[str, FileState]:
    root = workspace_root.resolve(strict=True)
    snapshot: dict[str, FileState] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == _INTERNAL_DIR:
            continue
        stat = path.stat()
        snapshot[relative.as_posix()] = FileState(stat.st_size, stat.st_mtime_ns, _sha256(path))
    return snapshot


def _artifact_kind(path: Path) -> ArtifactKind:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".svg", ".webp"}:
        return ArtifactKind.CHART
    if suffix in {".csv", ".parquet", ".json", ".xlsx"}:
        return ArtifactKind.DATASET
    if suffix in {".py", ".r", ".sql"}:
        return ArtifactKind.SCRIPT
    if suffix in {".md", ".html", ".pdf"}:
        return ArtifactKind.REPORT
    return ArtifactKind.FILE


class ArtifactRegistry:
    def __init__(self, repository: ArtifactRepository) -> None:
        self.repository = repository

    def register_file(
        self,
        session_id: str,
        execution_id: str,
        workspace_root: Path,
        path: Path,
        *,
        lineage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        root = workspace_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if resolved != root and root not in resolved.parents:
            raise ValueError("artifact path must remain inside the session workspace")
        stat = resolved.stat()
        artifact = Artifact(
            session_id=session_id,
            execution_id=execution_id,
            kind=_artifact_kind(resolved),
            display_name=resolved.name,
            relative_path=resolved.relative_to(root).as_posix(),
            media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            size_bytes=stat.st_size,
            sha256=_sha256(resolved),
            lineage=lineage or {},
            metadata=metadata or {},
        )
        return self.repository.add(artifact)

    def register_changes(
        self,
        session_id: str,
        execution_id: str,
        workspace_root: Path,
        before: dict[str, FileState],
        after: dict[str, FileState],
    ) -> list[Artifact]:
        root = workspace_root.resolve(strict=True)
        changed = sorted(
            path for path, state in after.items() if path not in before or before[path] != state
        )
        artifacts: list[Artifact] = []
        archive_root = root / _INTERNAL_DIR / "artifacts" / execution_id
        for relative in changed:
            source = (root / relative).resolve(strict=True)
            if root not in source.parents:
                continue
            destination = archive_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            state = after[relative]
            media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            artifact = Artifact(
                session_id=session_id,
                execution_id=execution_id,
                kind=_artifact_kind(source),
                display_name=source.name,
                relative_path=destination.relative_to(root).as_posix(),
                media_type=media_type,
                size_bytes=state.size_bytes,
                sha256=state.sha256,
                lineage={
                    "original_relative_path": relative,
                    "change": "created" if relative not in before else "modified",
                },
            )
            self.repository.add(artifact)
            artifacts.append(artifact)
        return artifacts
