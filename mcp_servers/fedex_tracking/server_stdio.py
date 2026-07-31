"""Stdio entrypoint for tests (production uses streamable-http via server.py)."""
from mcp_servers.fedex_tracking.server import mcp_app

if __name__ == "__main__":
    mcp_app.run(transport="stdio")
