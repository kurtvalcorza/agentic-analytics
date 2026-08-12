from mcp.server import MCPServer

from .ids import PROTOCOL_VERSION

mcp = MCPServer(
    "Agentic Analytics",
    instructions=(
        "Agent-agnostic analytical runtime. Core analytical capabilities are exposed as MCP "
        "tools; host adapters must not define unique correctness logic. "
        f"Canonical protocol version: {PROTOCOL_VERSION}."
    ),
)


def main() -> None:
    """Run the MCP server over stdio by default."""

    mcp.run()


if __name__ == "__main__":
    main()
