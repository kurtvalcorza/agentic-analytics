from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from agentic_analytics.models import AnalysisSession, SessionMode, SessionStatus
from agentic_analytics.models.common import utc_now
from agentic_analytics.runtime import Runtime

# Terminal statuses a client may transition an active session into via close_session.
_CLOSE_STATUSES = {SessionStatus.COMPLETED, SessionStatus.CANCELLED}


class WorkspaceOverlapError(PermissionError):
    pass


def _reject_overlapping_active_sessions(runtime: Runtime, root: Path) -> None:
    """Refuse a workspace that overlaps an active session's root.

    Two active sessions sharing (or nesting under) a workspace would each mount it read-write,
    giving one session's managed code direct access to the other's files and outputs.
    """

    for existing in runtime.sessions.list_all():
        if existing.status is not SessionStatus.ACTIVE:
            continue
        other = Path(existing.workspace_root)
        if root == other or root in other.parents or other in root.parents:
            raise WorkspaceOverlapError(
                "workspace overlaps an active session; close it or use a disjoint workspace"
            )


def register_data_plane_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(description="Create an authorized analysis session for a local workspace.")
    def create_session(workspace_root: str = ".", mode: str = "strict") -> dict[str, Any]:
        authorized = runtime.workspace.authorize_workspace(workspace_root)
        _reject_overlapping_active_sessions(runtime, authorized)
        session = AnalysisSession(workspace_root=str(authorized), mode=SessionMode(mode))
        runtime.sessions.add(session)
        return {
            "session_id": session.id,
            "mode": session.mode.value,
            "protocol_version": session.protocol_version,
            "capabilities": {
                # Derived from the configured backend so clients see the tool as available when
                # a conformant managed backend (e.g. Docker) is configured.
                "managed_python": runtime.execution_backend.conformant,
                "duckdb": True,
                "external_execution_registration": False,
            },
        }

    @server.tool(
        description=(
            "Close an analysis session, releasing its workspace so a new session can reuse it "
            "and freeing managed backend resources. Status must be 'completed' or 'cancelled'."
        )
    )
    def close_session(session_id: str, status: str = "completed") -> dict[str, Any]:
        requested = SessionStatus(status)
        if requested not in _CLOSE_STATUSES:
            raise ValueError("close status must be 'completed' or 'cancelled'")
        session = runtime.sessions.get(session_id, session_id)
        if session.status is SessionStatus.ACTIVE:
            # Persist the terminal status first: this is the durable, authoritative transition
            # that releases the workspace overlap lock (survives a server restart). Backend
            # cleanup is best-effort — a missing or unavailable daemon must not wedge a session
            # in ACTIVE forever, since ephemeral containers already self-remove via --rm.
            session.status = requested
            session.updated_at = utc_now()
            runtime.sessions.update(session)
            with contextlib.suppress(Exception):
                runtime.execution_backend.close_session(session.id)
        return {
            "session_id": session.id,
            "status": session.status.value,
            "workspace_root": session.workspace_root,
        }

    @server.tool(description="Discover CSV and Parquet sources inside an authorized workspace.")
    def list_sources(session_id: str, recursive: bool = True) -> dict[str, Any]:
        session = runtime.sessions.get(session_id, session_id)
        discovered = runtime.workspace.discover(session.workspace_root, recursive=recursive)
        # Bound the model-facing response so a workspace with very many files cannot produce
        # an arbitrarily large descriptor array.
        limit = runtime.settings.max_discovered_sources
        total_discovered = len(discovered)
        paths = discovered[:limit]
        registered = {source.relative_path: source for source in runtime.sources.list(session.id)}
        items = []
        for path in paths:
            relative = runtime.workspace.relative_to_workspace(session.workspace_root, path)
            existing = registered.get(relative)
            items.append(
                {
                    "source_id": existing.id if existing else None,
                    "kind": "csv" if path.suffix.lower() == ".csv" else "parquet",
                    "display_name": path.name,
                    "relative_path": relative,
                    "size_bytes": path.stat().st_size,
                    "registered": existing is not None,
                }
            )
        return {
            "sources": items,
            "count": len(items),
            "total_discovered": total_discovered,
            "truncated": total_discovered > len(items),
            "omitted": total_discovered - len(items),
        }

    @server.tool(
        description="Register and inspect one CSV or Parquet source with bounded profiling."
    )
    def inspect_source(
        session_id: str,
        source: str,
        profile: str = "standard",
        sample_rows: int = 20,
    ) -> dict[str, Any]:
        del profile
        session = runtime.sessions.get(session_id, session_id)
        record, result = runtime.inspector.inspect(session, source, sample_rows=sample_rows)
        return {
            "source_id": record.id,
            "kind": record.kind.value,
            "fingerprint": record.fingerprint,
            "schema": result["schema"],
            "row_count": result["row_count"],
            "profile": {
                "null_counts": result["null_counts"],
                "duplicate_row_count": result["duplicate_row_count"],
                "profile_truncated": result["profile_truncated"],
            },
            "sample": result["sample"],
            "sample_truncated": result["sample_truncated"],
        }

    @server.tool(description="Run bounded read-only DuckDB SQL over registered session sources.")
    def query_data(session_id: str, sql: str, max_rows: int = 200) -> dict[str, Any]:
        session = runtime.sessions.get(session_id, session_id)
        return runtime.query.execute(session, sql, max_rows=max_rows)
