from __future__ import annotations

from dataclasses import dataclass

from .repositories import ExecutionRepository, SessionRepository, SourceRepository
from .services.inspector import InspectorService
from .services.query import QueryService
from .services.workspace import WorkspaceService
from .settings import Settings


@dataclass(slots=True)
class Runtime:
    settings: Settings
    sessions: SessionRepository
    sources: SourceRepository
    executions: ExecutionRepository
    workspace: WorkspaceService
    inspector: InspectorService
    query: QueryService

    @classmethod
    def create(cls, settings: Settings | None = None) -> Runtime:
        resolved = settings or Settings()
        resolved.ensure_directories()
        sessions = SessionRepository(resolved.state_dir)
        sources = SourceRepository(resolved.state_dir)
        executions = ExecutionRepository(resolved.state_dir)
        workspace = WorkspaceService(resolved.normalized_allowed_roots())
        inspector = InspectorService(sources, workspace, resolved)
        query = QueryService(sources, executions, workspace, resolved)
        return cls(resolved, sessions, sources, executions, workspace, inspector, query)
