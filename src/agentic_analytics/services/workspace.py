from __future__ import annotations

from pathlib import Path
from typing import ClassVar


class WorkspaceAuthorizationError(PermissionError):
    pass


class WorkspaceService:
    """Canonical path authorization and supported-data discovery."""

    SUPPORTED_SUFFIXES: ClassVar[frozenset[str]] = frozenset({".csv", ".parquet"})

    def __init__(self, allowed_roots: list[Path]) -> None:
        if not allowed_roots:
            raise ValueError("at least one allowed workspace root is required")
        self.allowed_roots = [root.resolve(strict=False) for root in allowed_roots]

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def authorize_workspace(self, requested: str | Path) -> Path:
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = self.allowed_roots[0] / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise WorkspaceAuthorizationError(f"workspace does not resolve: {requested}") from exc
        if not resolved.is_dir():
            raise WorkspaceAuthorizationError("workspace_root must be a directory")
        if not any(self._inside(resolved, root) for root in self.allowed_roots):
            raise WorkspaceAuthorizationError("workspace is outside authorized roots")
        return resolved

    def resolve_file(self, workspace_root: str | Path, relative_path: str | Path) -> Path:
        # Re-check the (possibly persisted) root against the current allowlist so a session
        # whose root was later removed from configuration can no longer read its files.
        root = self.authorize_workspace(workspace_root)
        requested = Path(relative_path)
        if requested.is_absolute():
            raise WorkspaceAuthorizationError("source paths must be relative to the workspace")
        if ".." in requested.parts:
            raise WorkspaceAuthorizationError("source paths must not traverse parent directories")
        try:
            resolved = (root / requested).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise WorkspaceAuthorizationError(f"source does not resolve: {relative_path}") from exc
        if not self._inside(resolved, root):
            raise WorkspaceAuthorizationError("source resolves outside the authorized workspace")
        if not resolved.is_file():
            raise WorkspaceAuthorizationError("source must resolve to a regular file")
        return resolved

    def discover(self, workspace_root: str | Path, recursive: bool = True) -> list[Path]:
        root = self.authorize_workspace(workspace_root)
        iterator = root.rglob("*") if recursive else root.glob("*")
        discovered: list[Path] = []
        for candidate in iterator:
            if candidate.suffix.lower() not in self.SUPPORTED_SUFFIXES:
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, RuntimeError):
                continue
            if resolved.is_file() and self._inside(resolved, root):
                discovered.append(resolved)
        return sorted(set(discovered))

    @staticmethod
    def relative_to_workspace(workspace_root: str | Path, path: Path) -> str:
        root = Path(workspace_root).resolve(strict=True)
        return path.resolve(strict=True).relative_to(root).as_posix()
