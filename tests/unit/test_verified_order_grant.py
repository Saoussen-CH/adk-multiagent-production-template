"""An order verified this conversation via verify_order_access must be
accessible through the normal ownership-checked tools, even for a caller
whose user_id doesn't match the order's customer_id — and must grant
nothing for any OTHER order."""
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tool_context_anonymous_but_verified():
    ctx = MagicMock()
    ctx.state = {"tenant_id": "test-tenant", "verified_order_ids": ["ORD-90001"]}
    ctx.user_id = "anon-does-not-own-anything"
    return ctx


def test_requires_order_ownership_accepts_the_conversation_grant(tool_context_anonymous_but_verified, monkeypatch):
    from customer_support_mas.auth import requires_order_ownership
    from customer_support_mas.providers.models import Order

    mock_order = Order(order_id="ORD-90001", customer_id="real-owner", status="Delivered")
    monkeypatch.setattr(
        "customer_support_mas.auth.get_provider",
        lambda tenant_id: MagicMock(get_order=lambda t, oid: mock_order),
    )

    @requires_order_ownership
    def fake_tool(order_id, tool_context, _order_data=None, **kwargs):
        return {"status": "success", "order_data": _order_data}

    result = fake_tool(order_id="ORD-90001", tool_context=tool_context_anonymous_but_verified)

    assert result["status"] == "success"


def test_requires_order_ownership_does_not_grant_a_different_unverified_order(
    tool_context_anonymous_but_verified, monkeypatch
):
    from customer_support_mas.auth import requires_order_ownership
    from customer_support_mas.providers.models import Order

    other_order = Order(order_id="ORD-DIFFERENT", customer_id="real-owner", status="Delivered")
    monkeypatch.setattr(
        "customer_support_mas.auth.get_provider",
        lambda tenant_id: MagicMock(get_order=lambda t, oid: other_order),
    )

    @requires_order_ownership
    def fake_tool(order_id, tool_context, _order_data=None, **kwargs):
        return {"status": "success"}

    result = fake_tool(order_id="ORD-DIFFERENT", tool_context=tool_context_anonymous_but_verified)

    assert result["status"] == "error"
