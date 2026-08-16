from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .ids import PROTOCOL_VERSION
from .runtime import Runtime
from .settings import Settings
from .tools.data_plane import register_data_plane_tools
from .tools.evidence import register_evidence_tools
from .tools.execution import register_execution_tools
from .tools.validation import register_validation_tools


def build_server(settings: Settings | None = None) -> MCPServer[Any]:
    runtime = Runtime.create(settings)
    server: MCPServer[Any] = MCPServer(
        "Agentic Analytics",
        instructions=(
            "Agent-agnostic analytical runtime. Core analytical capabilities are exposed as MCP "
            "tools; host adapters must not define unique correctness logic. "
            f"Canonical protocol version: {PROTOCOL_VERSION}."
        ),
    )
    register_data_plane_tools(server, runtime)
    register_execution_tools(server, runtime)
    register_evidence_tools(server, runtime)
    register_validation_tools(server, runtime)
    register_artifact_resources(server, runtime)
    return server


def register_artifact_resources(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.resource(
        "agentic-analytics://sessions/{session_id}/artifacts/{artifact_id}",
        name="managed_artifact",
        description="Bytes of a managed-execution artifact from the immutable archive.",
    )
    def read_artifact(session_id: str, artifact_id: str) -> bytes:
        artifact = runtime.artifacts.get(session_id, artifact_id)
        path = runtime.artifact_registry.archived_path(artifact)
        return path.read_bytes()


mcp = build_server()


def main() -> None:
    """Run the MCP server over stdio by default."""

    mcp.run()


if __name__ == "__main__":
    main()
