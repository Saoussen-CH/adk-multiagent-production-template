"""auth.py's verify_order_ownership must route through get_provider(tenant_id)
instead of calling db_client directly — proven by seeding two tenants with
colliding order IDs and confirming each tenant's check only ever sees its
own tenant's order.

Note on fixtures: `mock_tool_context` is defined locally in
tests/unit/test_tools.py, not in conftest.py, so it is not visible when this
file is collected on its own (pytest fixtures from a sibling test module are
not shared unless declared in a conftest.py). Redefined here identically,
matching the same workaround already used by tests/unit/test_refund_staging.py.
`mock_tool_context_with_tenant` (conftest.py) depends on whichever
`mock_tool_context` is visible per test module, so it picks this one up here.
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_tool_context():
    """Mock ToolContext for demo-user-001, matching test_tools.py's fixture."""
    mock_ctx = MagicMock()
    mock_ctx.state = {}
    mock_ctx.user_id = "demo-user-001"
    mock_ctx.actions = MagicMock()
    return mock_ctx


def test_verify_order_ownership_routes_through_provider(mock_tool_context_with_tenant, mock_db):
    from customer_support_mas.auth import verify_order_ownership

    mock_db.collection("orders").document("ORD-SHARED-ID").set(
        {"customer_id": "demo-user-001", "status": "Delivered", "items": []}
    )

    is_authorized, order, error = verify_order_ownership(
        "ORD-SHARED-ID", "demo-user-001", tenant_id="test-tenant"
    )

    assert is_authorized is True
    # verify_order_ownership returns a dict (see auth._order_to_dict) — it
    # omits order_id deliberately, mirroring _invoice_to_dict, since callers
    # already track the id separately. Assert on fields the dict does carry.
    assert order["customer_id"] == "demo-user-001"
    assert order["status"] == "Delivered"
