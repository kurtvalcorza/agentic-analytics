import typer

app = typer.Typer(no_args_is_help=True)


@app.command()
def serve(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the canonical MCP interface."""
    from .server import mcp
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "streamable-http":
        mcp.settings.host, mcp.settings.port = host, port
        mcp.run(transport="streamable-http")
    else:
        raise typer.BadParameter("transport must be stdio or streamable-http")

