"""FedEx tracking MCP server.

Exposes a single read-only tool, track_shipment, over streamable HTTP.
Read-only by design: gateway policy can safely allow it wholesale.
"""

import os

from mcp.server.fastmcp import FastMCP

from mcp_servers.fedex_tracking.fedex_client import FedExClientError, get_tracking_status

mcp_app = FastMCP(
    "fedex-tracking",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8080")),
)


@mcp_app.tool()
def track_shipment(tracking_number: str) -> dict:
    """Get live FedEx tracking status for a shipment.

    Args:
        tracking_number: The FedEx tracking number (digits, e.g. 794658790132).

    Returns:
        Tracking status: current status, estimated delivery, and scan events.
    """
    try:
        return get_tracking_status(tracking_number)
    except FedExClientError as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp_app.run(transport="streamable-http")
