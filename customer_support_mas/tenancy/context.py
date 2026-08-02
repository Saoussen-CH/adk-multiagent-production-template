"""Read tenant_id out of ADK's per-session state.

tenant_id is set once, at session creation, by the FastAPI backend (Task 7)
from the value the embedding support widget passes on the chat request — it
does not change for the life of a session. Every tool function calls
get_tenant_id(tool_context) before touching any provider; a missing value
is a hard error, matching the "no implicit tenant" constraint.
"""


class MissingTenantError(Exception):
    """tool_context.state has no tenant_id — the session was created without
    one, which should be impossible once Task 7 lands. Treat as a bug, not
    a recoverable condition."""


def get_tenant_id(tool_context) -> str:
    tenant_id = tool_context.state.get("tenant_id")
    if not tenant_id:
        raise MissingTenantError(
            "tool_context.state has no tenant_id — every session must be created with one"
        )
    return tenant_id
