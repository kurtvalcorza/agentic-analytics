import asyncio
from agentic_analytics.server import mcp

def test_canonical_tool_discovery():
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {"create_session", "list_sources", "inspect_source", "query_data",
        "execute_python", "register_external_execution", "register_evidence", "list_evidence",
        "validate_analysis", "challenge_analysis", "list_artifacts", "get_artifact"}
    assert all(tool.inputSchema for tool in tools)
