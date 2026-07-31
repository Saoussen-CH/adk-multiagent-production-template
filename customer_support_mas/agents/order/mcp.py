"""Env-gated FedEx MCP toolset for the order agent.

When MCP_FEDEX_URL is unset the order agent is byte-for-byte its v1 self —
no MCP dependency is exercised at runtime. When set (deployed engines get it
via ENV_VARS in deployment/deploy.py), the agent gains the read-only
track_shipment tool served by the fedex-tracking-mcp Cloud Run service,
with egress governed per docs/MCP_FEDEX.md.
"""
import os
from typing import Optional

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams


def build_fedex_toolset() -> Optional[McpToolset]:
    url = os.environ.get("MCP_FEDEX_URL")
    if not url:
        return None
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url),
        tool_filter=["track_shipment"],
    )
