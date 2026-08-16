from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .ids import PROTOCOL_VERSION
from .runtime import Runtime
from .settings import Settings
from .tools.data_plane import register_data_plane_tools
from .tools.execution import register_execution_tools


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
    return server


mcp = build_server()


def main() -> None:
    """Run the MCP server over stdio by default."""

    mcp.run()


if __name__ == "__main__":
    main()
