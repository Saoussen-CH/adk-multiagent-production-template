# tests/unit/test_verify_order_access_tool.py
"""verify_order_access grants a narrow, conversation-scoped authorization
for exactly one order on a real order+email match — never on a guess, never
broader than the one order, and never more than 3 attempts per conversation."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tool_context_with_tenant():
    ctx = MagicMock()
    ctx.state = {"tenant_id": "test-tenant"}
    return ctx


def test_matching_order_and_email_grants_access(tool_context_with_tenant, monkeypatch):
    from customer_support_mas.agents.order.tools import verify_order_access

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=lambda t, o, e: o == "ORD-90001" and e == "alice@example.com"),
    )

    result = verify_order_access(order_id="ORD-90001", email="alice@example.com", tool_context=tool_context_with_tenant)

    assert result["status"] == "success"
    assert tool_context_with_tenant.state["verified_order_ids"] == ["ORD-90001"]


def test_wrong_email_and_nonexistent_order_return_identical_shape(tool_context_with_tenant, monkeypatch):
    """The caller must not be able to tell 'wrong email, real order' apart
    from 'order does not exist' — same enumeration-prevention rule already
    applied to tenant resolution in backend/app/main.py."""
    from customer_support_mas.agents.order.tools import verify_order_access

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=lambda t, o, e: False),
    )

    wrong_email_result = verify_order_access(
        order_id="ORD-90001", email="wrong@example.com", tool_context=tool_context_with_tenant
    )
    nonexistent_order_result = verify_order_access(
        order_id="ORD-99999", email="wrong@example.com", tool_context=tool_context_with_tenant
    )

    assert wrong_email_result == {
        "status": "error",
        "message": "Could not verify those order details. Please check the order number and email and try again.",
    }
    assert nonexistent_order_result == wrong_email_result


def test_third_failure_triggers_the_cap_message(tool_context_with_tenant, monkeypatch):
    from customer_support_mas.agents.order.tools import verify_order_access

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=lambda t, o, e: False),
    )

    verify_order_access(order_id="ORD-11111", email="wrong@example.com", tool_context=tool_context_with_tenant)
    verify_order_access(order_id="ORD-11111", email="wrong@example.com", tool_context=tool_context_with_tenant)
    third = verify_order_access(order_id="ORD-11111", email="wrong@example.com", tool_context=tool_context_with_tenant)

    assert third["status"] == "error"
    assert "log in" in third["message"].lower() or "contact" in third["message"].lower()
    assert tool_context_with_tenant.state["order_verification_failures"] == 3


def test_fourth_attempt_does_not_retry_even_with_correct_details(tool_context_with_tenant, monkeypatch):
    """Once the cap trips, no further attempt succeeds this conversation —
    even a genuinely correct order+email pair, since the tool must stop
    calling the provider at all once capped (a real safeguard against
    unbounded guessing, not just a message change)."""
    from customer_support_mas.agents.order.tools import verify_order_access

    call_count = {"n": 0}

    def _verify(tenant_id, order_id, email):
        call_count["n"] += 1
        return order_id == "ORD-11111" and email == "alice@example.com"

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=_verify),
    )

    for _ in range(3):
        verify_order_access(order_id="ORD-11111", email="wrong@example.com", tool_context=tool_context_with_tenant)

    calls_before_fourth = call_count["n"]
    fourth = verify_order_access(order_id="ORD-11111", email="alice@example.com", tool_context=tool_context_with_tenant)

    assert fourth["status"] == "error"
    assert "verified_order_ids" not in tool_context_with_tenant.state
    assert call_count["n"] == calls_before_fourth  # provider was not called a 4th time


def test_provider_exception_is_indistinguishable_from_a_normal_miss(tool_context_with_tenant, monkeypatch):
    """A provider exception must NOT escape to tool_error_handler's outer
    catch. If it did, the caller would get a different message than a normal
    wrong-details miss (an order-existence oracle — FirestoreProvider only
    touches `email` on the branch where the order exists) AND the attempt
    would not count against the 3-attempt cap, making the cap free to bypass."""
    from customer_support_mas.agents.order.tools import _GENERIC_VERIFICATION_FAILURE, verify_order_access

    def _boom(tenant_id, order_id, email):
        raise AttributeError("'NoneType' object has no attribute 'strip'")

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=_boom),
    )

    result = verify_order_access(order_id="ORD-90001", email="alice@example.com", tool_context=tool_context_with_tenant)

    assert result == _GENERIC_VERIFICATION_FAILURE
    assert tool_context_with_tenant.state["order_verification_failures"] == 1


@pytest.mark.parametrize("bad_email", [None, "not-an-email", 12345, ""])
def test_malformed_email_fails_generically_without_reaching_the_provider(
    tool_context_with_tenant, monkeypatch, bad_email
):
    """Malformed input must be rejected BEFORE the provider call — otherwise
    the provider's own handling of it varies by whether the order exists —
    and must still cost the caller one of their 3 attempts."""
    from customer_support_mas.agents.order.tools import _GENERIC_VERIFICATION_FAILURE, verify_order_access

    call_count = {"n": 0}

    def _verify(tenant_id, order_id, email):
        call_count["n"] += 1
        return True

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=_verify),
    )

    result = verify_order_access(order_id="ORD-90001", email=bad_email, tool_context=tool_context_with_tenant)

    assert result == _GENERIC_VERIFICATION_FAILURE
    assert tool_context_with_tenant.state["order_verification_failures"] == 1
    assert call_count["n"] == 0  # provider was never reached
    assert "verified_order_ids" not in tool_context_with_tenant.state


def test_verifying_one_order_does_not_grant_a_different_order(tool_context_with_tenant, monkeypatch):
    from customer_support_mas.agents.order.tools import verify_order_access

    monkeypatch.setattr(
        "customer_support_mas.agents.order.tools.get_provider",
        lambda tenant_id: MagicMock(verify_order_owner=lambda t, o, e: o == "ORD-22222" and e == "alice@example.com"),
    )

    verify_order_access(order_id="ORD-22222", email="alice@example.com", tool_context=tool_context_with_tenant)

    assert "ORD-22222" in tool_context_with_tenant.state["verified_order_ids"]
    assert "ORD-33333" not in tool_context_with_tenant.state["verified_order_ids"]
