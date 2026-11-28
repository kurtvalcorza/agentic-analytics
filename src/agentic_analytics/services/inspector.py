from __future__ import annotations

from pathlib import Path
from typing import Any

from agentic_analytics.dataplane import DataPlaneClient, DataPlaneError
from agentic_analytics.dataplane.requests import ProfileRequest
from agentic_analytics.integrity import fingerprint_file
from agentic_analytics.models import (
    READABLE_SOURCE_SUFFIXES,
    AnalysisSession,
    DataSource,
    SourceKind,
)
from agentic_analytics.repositories import SourceRepository
from agentic_analytics.settings import Settings

from .workspace import WorkspaceService


class SourceInspectionError(ValueError):
    pass


class InspectorService:
    def __init__(
        self,
        sources: SourceRepository,
        workspace: WorkspaceService,
        settings: Settings,
        data_plane: DataPlaneClient | None = None,
    ) -> None:
        self.data_plane = data_plane or DataPlaneClient(settings)
        self.sources = sources
        self.workspace = workspace
        self.settings = settings

    def inspect(
        self, session: AnalysisSession, source_path: str, sample_rows: int = 20
    ) -> tuple[DataSource, dict[str, Any]]:
        path = self.workspace.resolve_file(session.workspace_root, source_path)
        if path.stat().st_size == 0:
            raise SourceInspectionError("source is empty")
        kind = READABLE_SOURCE_SUFFIXES.get(path.suffix.lower())
        if kind is None:
            raise SourceInspectionError("only CSV and Parquet sources are supported")
        relative_path = self.workspace.relative_to_workspace(session.workspace_root, path)
        fingerprint = fingerprint_file(path)
        try:
            schema, profile = self._profile(path, kind, sample_rows)
        except DataPlaneError as exc:
            detail = "" if exc.category in {"source_read", "sql"} else f" ({exc.category})"
            raise SourceInspectionError(
                f"source could not be inspected: {path.name}{detail}"
            ) from exc
        existing = next(
            (
                item
                for item in self.sources.list(session.id)
                if item.relative_path == relative_path and item.fingerprint == fingerprint
            ),
            None,
        )
        if existing is not None:
            return existing, profile
        source = DataSource(
            session_id=session.id,
            kind=kind,
            display_name=path.name,
            relative_path=relative_path,
            fingerprint=fingerprint,
            schema=schema,
            row_count=profile["row_count"],
            profile={
                "null_counts": profile["null_counts"],
                "duplicate_row_count": profile["duplicate_row_count"],
                "profile_truncated": profile["profile_truncated"],
            },
        )
        self.sources.add(source)
        return source, profile

    def _profile(
        self, path: Path, kind: SourceKind, sample_rows: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Profile the source in the isolated data-plane worker (SEC-02)."""

        timeout = self.settings.data_plane_timeout_seconds
        outcome = self.data_plane.call(
            ProfileRequest(
                path=str(path),
                kind=kind.value,
                sample_limit=min(max(sample_rows, 1), self.settings.max_sample_rows),
                max_profile_columns=self.settings.max_profile_columns,
                limits=self.data_plane.limits(timeout),
            ),
            deadline_seconds=timeout,
        )
        return outcome["schema"], outcome["profile"]
