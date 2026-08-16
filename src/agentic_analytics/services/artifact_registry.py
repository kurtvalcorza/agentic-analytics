from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_analytics.models import Artifact, ArtifactKind
from agentic_analytics.repositories import ArtifactRepository

_INTERNAL_DIR = ".agentic-analytics"

_MEDIA_TYPES: dict[str, str] = {
    ".csv": "text/csv",
    ".parquet": "application/vnd.apache.parquet",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".html": "text/html",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".sql": "application/sql",
}


class ArtifactLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FileMeta:
    size_bytes: int
    mtime_ns: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_workspace(workspace_root: Path) -> dict[str, FileMeta]:
    """Record cheap file metadata (size + mtime) for every workspace file.

    Only metadata is captured here; contents are hashed later and only for files whose
    metadata shows they were created or modified, so unchanged multi-gigabyte inputs are
    never read twice per execution.
    """

    root = workspace_root.resolve(strict=True)
    snapshot: dict[str, FileMeta] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == _INTERNAL_DIR:
            continue
        stat = path.stat()
        snapshot[relative.as_posix()] = FileMeta(stat.st_size, stat.st_mtime_ns)
    return snapshot


def _media_type(path: Path) -> str:
    # Map known suffixes explicitly rather than relying on the host MIME database, which does
    # not always know newer types such as .parquet.
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


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
    def __init__(
        self,
        repository: ArtifactRepository,
        archive_root: Path,
        *,
        max_artifacts: int = 64,
        max_artifact_bytes: int = 100 * 1024 * 1024,
        max_total_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.repository = repository
        # The archive lives outside every executable workspace so managed code (which only
        # sees its workspace mount) can never overwrite or delete a persisted artifact.
        self.archive_root = archive_root.resolve(strict=False)
        self.max_artifacts = max_artifacts
        self.max_artifact_bytes = max_artifact_bytes
        self.max_total_bytes = max_total_bytes

    def archive_base(self, session_id: str, execution_id: str) -> Path:
        return self.archive_root / session_id / execution_id

    def archived_path(self, artifact: Artifact) -> Path:
        return self.archive_base(artifact.session_id, artifact.execution_id or "") / (
            artifact.relative_path
        )

    def register_file(
        self,
        session_id: str,
        execution_id: str,
        path: Path,
        *,
        lineage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """Register a file already written into this execution's archive base.

        The file lives outside every executable workspace, so managed code cannot later
        corrupt it, and its archive-relative path resolves through ``archived_path``.
        """

        archive_base = self.archive_base(session_id, execution_id).resolve(strict=False)
        resolved = path.resolve(strict=True)
        if resolved != archive_base and archive_base not in resolved.parents:
            raise ValueError("artifact path must remain inside the execution archive")
        stat = resolved.stat()
        artifact = Artifact(
            session_id=session_id,
            execution_id=execution_id,
            kind=_artifact_kind(resolved),
            display_name=resolved.name,
            relative_path=resolved.relative_to(archive_base).as_posix(),
            media_type=_media_type(resolved),
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
        before: dict[str, FileMeta],
        after: dict[str, FileMeta],
    ) -> list[Artifact]:
        root = workspace_root.resolve(strict=True)
        changed = sorted(
            path for path, state in after.items() if path not in before or before[path] != state
        )

        # Enforce aggregate limits before hashing or copying so a single execution cannot
        # exhaust host disk or keep the server busy long after its timeout.
        if len(changed) > self.max_artifacts:
            raise ArtifactLimitError(
                f"execution produced {len(changed)} artifacts; limit is {self.max_artifacts}"
            )
        total_bytes = 0
        for relative in changed:
            size = after[relative].size_bytes
            if size > self.max_artifact_bytes:
                raise ArtifactLimitError(
                    f"artifact {relative} is {size} bytes; per-file limit is "
                    f"{self.max_artifact_bytes}"
                )
            total_bytes += size
        if total_bytes > self.max_total_bytes:
            raise ArtifactLimitError(
                f"execution produced {total_bytes} bytes of artifacts; limit is "
                f"{self.max_total_bytes}"
            )

        archive_base = self.archive_base(session_id, execution_id).resolve(strict=False)
        artifacts: list[Artifact] = []
        for relative in changed:
            source = (root / relative)
            # Reject a source that resolves outside the workspace (for example a symlink managed
            # code created that points at a host file).
            resolved_source = source.resolve(strict=True)
            if resolved_source != root and root not in resolved_source.parents:
                continue
            if not resolved_source.is_file():
                continue

            destination = archive_base / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Never write through a symlinked archive destination: the resolved parent must stay
            # beneath the (managed-inaccessible) archive base.
            resolved_parent = destination.parent.resolve(strict=True)
            if resolved_parent != archive_base and archive_base not in resolved_parent.parents:
                raise ArtifactLimitError(
                    f"artifact destination for {relative} escapes the archive root"
                )

            sha256 = _sha256(resolved_source)
            self._copy_bytes(resolved_source, destination)
            size_bytes = destination.stat().st_size
            artifact = Artifact(
                session_id=session_id,
                execution_id=execution_id,
                kind=_artifact_kind(resolved_source),
                display_name=resolved_source.name,
                relative_path=relative,
                media_type=_media_type(resolved_source),
                size_bytes=size_bytes,
                sha256=sha256,
                lineage={
                    "original_relative_path": relative,
                    "change": "created" if relative not in before else "modified",
                    "archive_root": _INTERNAL_DIR + "/artifacts",
                },
            )
            self.repository.add(artifact)
            artifacts.append(artifact)
        return artifacts

    @staticmethod
    def _copy_bytes(source: Path, destination: Path) -> None:
        # Copy contents without following a symlinked destination file.
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        with source.open("rb") as src, destination.open("wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
