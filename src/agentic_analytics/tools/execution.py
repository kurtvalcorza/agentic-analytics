from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from agentic_analytics.runtime import Runtime

from .errors import paginate, tool_errors


def register_execution_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(description="Execute Python in the configured managed analytical sandbox.")
    @tool_errors
    def execute_python(
        session_id: str,
        code: str,
        source_ids: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        with runtime.sessions.active(session_id) as session:
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
            "source_attestation": record.source_attestation.value,
            "error": record.error,
        }

    @server.tool(
        description=(
            "List immutable artifacts registered for an analysis session, one bounded page at "
            "a time (use next_offset to continue)."
        )
    )
    @tool_errors
    def list_artifacts(
        session_id: str,
        execution_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        artifacts = runtime.artifacts.list(session_id)
        if execution_id is not None:
            artifacts = [item for item in artifacts if item.execution_id == execution_id]
        # Stable order so offset pagination is consistent across calls.
        artifacts.sort(key=lambda item: (item.created_at, item.id))
        page = paginate(artifacts, limit, offset, runtime.settings.max_list_page_size)
        items = page.pop("page")
        return {"artifacts": [item.model_dump(mode="json") for item in items], **page}

    @server.tool(description="Return canonical artifact metadata and a resolvable resource URI.")
    @tool_errors
    def get_artifact(session_id: str, artifact_id: str) -> dict[str, Any]:
        artifact = runtime.artifacts.get(session_id, artifact_id)
        # This URI is backed by a registered MCP resource (see build_server), so an MCP-only
        # client can read the artifact bytes without host filesystem access.
        return {
            "artifact": artifact.model_dump(mode="json"),
            "uri": f"agentic-analytics://sessions/{session_id}/artifacts/{artifact_id}",
        }
