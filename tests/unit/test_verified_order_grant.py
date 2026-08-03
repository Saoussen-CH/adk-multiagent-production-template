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


@pytest.fixture
def tool_context_with_tenant():
    """A visitor who owns nothing and has not verified anything yet — the
    grant is earned inside the test via verify_order_access, so the test
    exercises the real end-to-end path rather than a hand-seeded state key."""
    ctx = MagicMock()
    ctx.state = {"tenant_id": "test-tenant"}
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


@pytest.mark.parametrize("billing_tool_name", ["get_invoice_by_order_id", "check_payment_status"])
def test_grant_earned_via_verify_order_access_does_not_unlock_billing_tools(
    tool_context_with_tenant, monkeypatch, billing_tool_name
):
    """An order number plus an email is a deliberately weaker bar than an
    account login. It must unlock order/shipping status and nothing else —
    check_payment_status returns payment_method, transaction_id and
    customer_id. Same exclusion the refund tools already have."""
    import customer_support_mas.agents.billing.tools as billing_tools
    from customer_support_mas.agents.order.tools import verify_order_access
    from customer_support_mas.providers.models import Order

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=lambda t, o, e: o == "ORD-90001" and e == "alice@example.com"),
    )

    verified = verify_order_access(
        order_id="ORD-90001", email="alice@example.com", tool_context=tool_context_with_tenant
    )
    assert verified["status"] == "success"
    assert tool_context_with_tenant.state["verified_order_ids"] == ["ORD-90001"]

    mock_order = Order(order_id="ORD-90001", customer_id="real-owner", status="Delivered")
    monkeypatch.setattr(
        "customer_support_mas.auth.get_provider",
        lambda tenant_id: MagicMock(get_order=lambda t, oid: mock_order),
    )
    # If the guard were bypassed, these would be reached and would return data.
    monkeypatch.setattr(
        "customer_support_mas.agents.billing.tools.get_provider",
        lambda tenant_id: MagicMock(
            get_invoice_by_order=lambda t, oid: (_ for _ in ()).throw(
                AssertionError("billing provider must not be reached for a merely order-verified caller")
            ),
            get_payment=lambda t, oid: (_ for _ in ()).throw(
                AssertionError("billing provider must not be reached for a merely order-verified caller")
            ),
        ),
    )

    result = getattr(billing_tools, billing_tool_name)(
        order_id="ORD-90001", tool_context=tool_context_with_tenant
    )

    assert result["status"] == "error"
    assert "permission" in result["message"].lower()


def test_order_tools_still_accept_the_same_grant(tool_context_with_tenant, monkeypatch):
    """Positive control for the test above: the ONLY difference between the
    billing tools and the order tools here is allow_verified_grant, not the
    fixture setup — otherwise the negative test could pass for the wrong
    reason (e.g. a broken grant, or a missing tenant)."""
    import customer_support_mas.agents.order.tools as order_tools
    from customer_support_mas.providers.models import Order

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=lambda t, o, e: True),
    )
    order_tools.verify_order_access(
        order_id="ORD-90001", email="alice@example.com", tool_context=tool_context_with_tenant
    )

    mock_order = Order(order_id="ORD-90001", customer_id="real-owner", status="Delivered")
    monkeypatch.setattr(
        "customer_support_mas.auth.get_provider",
        lambda tenant_id: MagicMock(get_order=lambda t, oid: mock_order),
    )

    result = order_tools.track_order(order_id="ORD-90001", tool_context=tool_context_with_tenant)

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
