from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from agentic_analytics.runtime import Runtime


def register_execution_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(description="Execute Python in the configured managed analytical sandbox.")
    def execute_python(
        session_id: str,
        code: str,
        source_ids: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        session = runtime.sessions.get(session_id, session_id)
        record = runtime.execution.execute_python(
            session, code, source_ids=source_ids, timeout_seconds=timeout_seconds
        )
        return {
            "execution_id": record.id,
            "status": record.status.value,
            "stdout_preview": record.stdout_preview,
            "stderr_preview": record.stderr_preview,
            "truncated": record.truncated,
            "artifact_ids": record.artifact_ids,
        }

    @server.tool(description="List immutable artifacts registered for an analysis session.")
    def list_artifacts(
        session_id: str, execution_id: str | None = None
    ) -> dict[str, Any]:
        artifacts = runtime.artifacts.list(session_id)
        if execution_id is not None:
            artifacts = [item for item in artifacts if item.execution_id == execution_id]
        return {
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
            "count": len(artifacts),
        }

    @server.tool(description="Return canonical artifact metadata and a resolvable resource URI.")
    def get_artifact(session_id: str, artifact_id: str) -> dict[str, Any]:
        artifact = runtime.artifacts.get(session_id, artifact_id)
        # This URI is backed by a registered MCP resource (see build_server), so an MCP-only
        # client can read the artifact bytes without host filesystem access.
        return {
            "artifact": artifact.model_dump(mode="json"),
            "uri": f"agentic-analytics://sessions/{session_id}/artifacts/{artifact_id}",
        }
