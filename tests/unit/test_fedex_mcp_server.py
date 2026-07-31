"""Tests the MCP server surface: tool discovery + invocation (mock mode)."""

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_servers.fedex_tracking.server import mcp_app


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setenv("FEDEX_MOCK", "true")


async def test_track_shipment_tool_is_discoverable():
    async with create_connected_server_and_client_session(mcp_app._mcp_server) as session:
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        assert "track_shipment" in names


async def test_track_shipment_returns_mock_status():
    async with create_connected_server_and_client_session(mcp_app._mcp_server) as session:
        result = await session.call_tool("track_shipment", {"tracking_number": "794658790132"})
        text = result.content[0].text
        assert "In transit" in text
        assert "794658790132" in text
