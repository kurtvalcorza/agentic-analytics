from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from agentic_analytics.models import AnalysisSession, SessionMode
from agentic_analytics.runtime import Runtime


def register_data_plane_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(description="Create an authorized analysis session for a local workspace.")
    def create_session(workspace_root: str = ".", mode: str = "strict") -> dict[str, Any]:
        authorized = runtime.workspace.authorize_workspace(workspace_root)
        session = AnalysisSession(workspace_root=str(authorized), mode=SessionMode(mode))
        runtime.sessions.add(session)
        return {
            "session_id": session.id,
            "mode": session.mode.value,
            "protocol_version": session.protocol_version,
            "capabilities": {
                "managed_python": False,
                "duckdb": True,
                "external_execution_registration": False,
            },
        }

    @server.tool(description="Discover CSV and Parquet sources inside an authorized workspace.")
    def list_sources(session_id: str, recursive: bool = True) -> dict[str, Any]:
        session = runtime.sessions.get(session_id, session_id)
        paths = runtime.workspace.discover(session.workspace_root, recursive=recursive)
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
        return {"sources": items, "count": len(items)}

    @server.tool(description="Register and inspect one CSV or Parquet source with bounded profiling.")
    def inspect_source(
        session_id: str,
        source: str,
        profile: str = "standard",
        sample_rows: int = 20,
    ) -> dict[str, Any]:
        del profile
        session = runtime.sessions.get(session_id, session_id)
        record, result = runtime.inspector.inspect(session, source, sample_rows=sample_rows)
        return {"source_id": record.id, "kind": record.kind.value, "fingerprint": record.fingerprint, **result}

    @server.tool(description="Run bounded read-only DuckDB SQL over registered session sources.")
    def query_data(session_id: str, sql: str, max_rows: int = 200) -> dict[str, Any]:
        session = runtime.sessions.get(session_id, session_id)
        return runtime.query.execute(session, sql, max_rows=max_rows)
