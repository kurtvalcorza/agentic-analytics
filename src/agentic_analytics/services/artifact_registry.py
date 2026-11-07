from __future__ import annotations

import contextlib
import hashlib
import os
import threading
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


_CHUNK = 1024 * 1024


class ArtifactLimitError(RuntimeError):
    pass


class ArtifactIntegrityError(RuntimeError):
    """A workspace file changed while it was being archived, so it cannot be attested."""


@dataclass(frozen=True, slots=True)
class FileMeta:
    """Change-detection metadata for a workspace file.

    ``ctime_ns`` and the inode identity catch rewrites that deliberately preserve size and
    mtime (``os.utime`` cannot set ctime), and replacement of a file by a different inode.
    """

    size_bytes: int
    mtime_ns: int
    ctime_ns: int = 0
    inode: int = 0
    device: int = 0

    @classmethod
    def of(cls, info: os.stat_result) -> FileMeta:
        return cls(info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)


def _sha256(path: Path) -> tuple[str, int]:
    """Return the digest and byte count of exactly the bytes read in one pass."""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _private_mkdir(path: Path) -> None:
    # Archive directories hold copies of analytical outputs; keep them owner-only.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def snapshot_workspace(
    workspace_root: Path, *, excluded_roots: tuple[Path, ...] = ()
) -> dict[str, FileMeta]:
    """Record cheap file metadata (size + mtime) for every workspace file.

    Only metadata is captured here; contents are hashed later and only for files whose
    metadata shows they were created or modified, so unchanged multi-gigabyte inputs are
    never read twice per execution.
    """

    root = workspace_root.resolve(strict=True)
    excluded = tuple(path.resolve(strict=False) for path in excluded_roots)
    snapshot: dict[str, FileMeta] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if any(
            resolved == excluded_root or excluded_root in resolved.parents
            for excluded_root in excluded
        ):
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == _INTERNAL_DIR:
            continue
        snapshot[relative.as_posix()] = FileMeta.of(path.stat())
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
        # Per-session lock so the read-check-write of the cumulative byte budget is atomic:
        # concurrent spills/executions in one session cannot both observe the same remaining
        # budget and register artifacts whose combined size exceeds max_total_bytes.
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def archive_base(self, session_id: str, execution_id: str) -> Path:
        return self.archive_root / session_id / execution_id

    def archived_path(self, artifact: Artifact) -> Path:
        return self.archive_base(artifact.session_id, artifact.execution_id or "") / (
            artifact.relative_path
        )

    def session_artifact_bytes(self, session_id: str) -> int:
        """Total bytes already registered for a session."""

        return sum(item.size_bytes for item in self.repository.list(session_id))

    def remaining_session_budget(self, session_id: str) -> int:
        """Bytes a new artifact may still consume before the session-cumulative cap is hit."""

        return max(0, self.max_total_bytes - self.session_artifact_bytes(session_id))

    def spill_byte_ceiling(self, session_id: str) -> int:
        """Largest a single spilled artifact may be given the per-file and remaining-session caps.

        A spill watchdog uses this so a write is interrupted at the *remaining* session budget,
        not the full cumulative ceiling, keeping the temporary file from over-writing before
        registration would reject it.
        """

        return min(self.max_artifact_bytes, self.remaining_session_budget(session_id))

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
        # Enforce the same byte ceilings as workspace-file registration so a spilled query
        # result (or any pre-written file) cannot bypass the configured artifact quotas.
        if stat.st_size > self.max_artifact_bytes:
            raise ArtifactLimitError(
                f"artifact {resolved.name} is {stat.st_size} bytes; per-file limit is "
                f"{self.max_artifact_bytes}"
            )
        # Hash outside the lock (it can be expensive) then reserve budget and persist atomically.
        # Size and digest describe the same single read of the archived bytes.
        sha256, size_bytes = _sha256(resolved)
        if size_bytes > self.max_artifact_bytes:
            raise ArtifactLimitError(
                f"artifact {resolved.name} is {size_bytes} bytes; per-file limit is "
                f"{self.max_artifact_bytes}"
            )
        with contextlib.suppress(OSError):
            resolved.chmod(0o600)
        artifact = Artifact(
            session_id=session_id,
            execution_id=execution_id,
            kind=_artifact_kind(resolved),
            display_name=resolved.name,
            relative_path=resolved.relative_to(archive_base).as_posix(),
            media_type=_media_type(resolved),
            size_bytes=size_bytes,
            sha256=sha256,
            lineage=lineage or {},
            metadata=metadata or {},
        )
        with self._session_lock(session_id):
            existing_bytes = self.session_artifact_bytes(session_id)
            if existing_bytes + size_bytes > self.max_total_bytes:
                raise ArtifactLimitError(
                    f"registering {resolved.name} ({size_bytes} bytes) would exceed the session "
                    f"artifact byte budget of {self.max_total_bytes}"
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

        # Reserve budget against the session-cumulative cap and copy under the session lock so a
        # concurrent execution/spill in the same session cannot pass its own check against the
        # same remaining budget and let the combined size exceed max_total_bytes.
        with self._session_lock(session_id):
            existing_bytes = self.session_artifact_bytes(session_id)
            if existing_bytes + total_bytes > self.max_total_bytes:
                raise ArtifactLimitError(
                    f"execution would bring session artifacts to {existing_bytes + total_bytes} "
                    f"bytes; limit is {self.max_total_bytes}"
                )
            return self._copy_and_register(
                session_id, execution_id, root, changed, before, after
            )

    def _copy_and_register(
        self,
        session_id: str,
        execution_id: str,
        root: Path,
        changed: list[str],
        before: dict[str, FileMeta],
        after: dict[str, FileMeta],
    ) -> list[Artifact]:
        archive_base = self.archive_base(session_id, execution_id).resolve(strict=False)
        artifacts: list[Artifact] = []
        for relative in changed:
            source = root / relative
            # Reject a source that resolves outside the workspace (for example a symlink managed
            # code created that points at a host file).
            resolved_source = source.resolve(strict=True)
            if resolved_source != root and root not in resolved_source.parents:
                continue
            if not resolved_source.is_file():
                continue

            destination = archive_base / relative
            _private_mkdir(destination.parent)
            # Never write through a symlinked archive destination: the resolved parent must stay
            # beneath the (managed-inaccessible) archive base.
            resolved_parent = destination.parent.resolve(strict=True)
            if resolved_parent != archive_base and archive_base not in resolved_parent.parents:
                raise ArtifactLimitError(
                    f"artifact destination for {relative} escapes the archive root"
                )

            sha256, size_bytes = self._archive_file(
                resolved_source, destination, expected=after[relative]
            )
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

    def _archive_file(
        self, source: Path, destination: Path, *, expected: FileMeta
    ) -> tuple[str, int]:
        """Copy ``source`` to ``destination`` atomically, attesting the archived bytes.

        The digest and size are computed from exactly the bytes written to a private temporary
        file in the destination directory, which is fsynced and then renamed into place, so the
        recorded SHA-256 always describes the published archive. If the source's metadata
        differs from the post-execution snapshot, or changes during the copy, the copy is
        discarded and the artifact is refused rather than attested with mixed content.
        """

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        temp = destination.with_name(f".{destination.name}.{os.urandom(8).hex()}.partial")
        fd = os.open(temp, flags, 0o600)
        published = False
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
                if FileMeta.of(os.fstat(src.fileno())) != expected:
                    raise ArtifactIntegrityError(
                        f"artifact {source.name} changed after execution finished"
                    )
                digest = hashlib.sha256()
                size = 0
                for chunk in self._read_chunks(src):
                    digest.update(chunk)
                    dst.write(chunk)
                    size += len(chunk)
                if size > self.max_artifact_bytes:
                    raise ArtifactLimitError(
                        f"artifact {source.name} grew to {size} bytes; per-file limit is "
                        f"{self.max_artifact_bytes}"
                    )
                if FileMeta.of(os.fstat(src.fileno())) != expected:
                    raise ArtifactIntegrityError(
                        f"artifact {source.name} was modified while it was being archived"
                    )
                dst.flush()
                os.fsync(dst.fileno())
            if os.stat(temp, follow_symlinks=False).st_size != size:
                raise ArtifactIntegrityError(f"archived copy of {source.name} is incomplete")
            # Never write through a symlinked destination: replace() swaps the directory entry.
            os.replace(temp, destination)
            published = True
            return digest.hexdigest(), size
        finally:
            if not published:
                with contextlib.suppress(FileNotFoundError):
                    temp.unlink()

    @staticmethod
    def _read_chunks(stream: Any) -> Any:
        return iter(lambda: stream.read(_CHUNK), b"")

    def discard_created_files(
        self, workspace_root: Path, before: dict[str, FileMeta], after: dict[str, FileMeta]
    ) -> int:
        """Delete files an execution created (never pre-existing ones); return bytes freed.

        Used when a workload is terminated for exceeding its disk budget so its partial output
        does not keep consuming host storage. Modified pre-existing files are left in place.
        """

        root = workspace_root.resolve(strict=True)
        freed = 0
        for relative, meta in after.items():
            if relative in before:
                continue
            path = root / relative
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            # Only remove the exact regular file observed in the snapshot.
            if path.is_symlink() or FileMeta.of(info) != meta:
                continue
            parent = path.parent.resolve(strict=True)
            if parent != root and root not in parent.parents:
                continue
            with contextlib.suppress(OSError):
                path.unlink()
                freed += meta.size_bytes
        return freed
