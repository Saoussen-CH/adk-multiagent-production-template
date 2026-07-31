"""Proves ADK McpToolset can discover tools from the actual FedEx MCP server
process (stdio transport). No LLM, no network: the server runs in mock mode
as a subprocess. This is the contract test between agent-side and server-side."""
import os
import sys

import pytest

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters


@pytest.mark.asyncio
async def test_mcp_toolset_discovers_track_shipment():
    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "mcp_servers.fedex_tracking.server_stdio"],
                env={**os.environ, "FEDEX_MOCK": "true"},
            ),
            timeout=20.0,
        ),
        tool_filter=["track_shipment"],
    )
    try:
        tools = await toolset.get_tools()
        names = [t.name for t in tools]
        assert names == ["track_shipment"]
    finally:
        await toolset.close()
