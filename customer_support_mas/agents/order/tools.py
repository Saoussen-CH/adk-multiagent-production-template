"""
Order-related tools for the customer support system.

This module contains all tools for order tracking and order history.
All tools verify ownership using decorators - users can only access their own orders.
"""

import logging

from google.adk.tools.tool_context import ToolContext

from customer_support_mas.auth import (
    requires_authenticated_user,
    requires_order_ownership,
)
from customer_support_mas.error_handling import tool_error_handler
from customer_support_mas.providers.registry import get_provider
from customer_support_mas.tenancy.context import get_tenant_id
from customer_support_mas.validation import (
    validate_order_id,
    validation_error_response,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ORDER TRACKING (requires ownership)
# =============================================================================


@tool_error_handler
@requires_order_ownership
def track_order(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Track an order by order ID. Only accessible if the order belongs to you.

    Args:
        order_id: The order ID to track (e.g., "ORD-12345")
        tool_context: ADK ToolContext (automatically injected)
        _order_data: Pre-fetched order data (injected by decorator)
    """
    # Input validation (decorator handles authorization after this)
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    # _order_data is already fetched and ownership verified by decorator
    return {
        "status": "success",
        "order": {
            "order_id": order_id,
            "status": _order_data.get("status"),
            "carrier": _order_data.get("carrier"),
            "tracking_number": _order_data.get("tracking_number"),
            "estimated_delivery": _order_data.get("estimated_delivery"),
            "timeline": _order_data.get("timeline", []),
        },
    }


@tool_error_handler
@requires_order_ownership
def get_order_details(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Get full details for a specific order. Only accessible if the order belongs to you.

    Args:
        order_id: The order ID to get details for (e.g., "ORD-12345")
        tool_context: ADK ToolContext (automatically injected)
        _order_data: Pre-fetched order data (injected by decorator)
    """
    # Input validation (decorator handles authorization after this)
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    return {
        "status": "success",
        "order": {
            "order_id": order_id,
            "date": _order_data.get("date"),
            "status": _order_data.get("status"),
            "items": _order_data.get("items", []),
            "subtotal": _order_data.get("subtotal"),
            "tax": _order_data.get("tax"),
            "total": _order_data.get("total"),
            "carrier": _order_data.get("carrier"),
            "tracking_number": _order_data.get("tracking_number"),
            "estimated_delivery": _order_data.get("estimated_delivery"),
            "delivered_date": _order_data.get("delivered_date"),
            "shipping_address": _order_data.get("shipping_address"),
            "timeline": _order_data.get("timeline", []),
        },
    }


# =============================================================================
# ORDER VERIFICATION (step-up for anonymous / not-yet-logged-in visitors)
# =============================================================================

_MAX_ORDER_VERIFICATION_ATTEMPTS = 3

_GENERIC_VERIFICATION_FAILURE = {
    "status": "error",
    "message": "Could not verify those order details. Please check the order number and email and try again.",
}

_CAP_EXCEEDED_RESPONSE = {
    "status": "error",
    "message": (
        "Too many failed attempts to verify this order. Please log in to your account, "
        "or contact support through another channel for help with this order."
    ),
}


# The failure counter is deliberately USER-scoped, not session-scoped.
# google.adk.sessions.state.State.USER_PREFIX == "user:" — per the ADK
# session-state contract, state under this prefix is meant to persist per
# user_id across ALL of that user's sessions, unlike a plain (session-scoped)
# key, which resets to zero on every new conversation — and a new
# conversation costs nothing: POST /api/chat with no session_id, up to
# RATE_LIMITS["chat"] (300/hour). That made a session-scoped cap bound
# nothing. IF the prefix's persistence holds on the deployed runtime, the
# only way to reset this counter becomes minting a whole new anonymous
# identity via POST /api/auth/anonymous, which RATE_LIMITS["auth"] already
# caps at 50/hour/IP — giving a real bound of <=150 guesses/hour/IP with no
# new infrastructure.
#
# CAVEAT (per this repo's own rule of treating Agent Engine behavior as
# unverified until tested live — see CLAUDE.md's "Reference docs mirror"
# section): this has NOT been confirmed against the deployed
# VertexAiSessionService. Its session resource only documents a single
# `sessionState` field with no separate user-state concept, and
# `get_user_state()` raises NotImplementedError with "The Vertex AI Agent
# Engine API does not expose user state independently of a session" — so
# `user:`-prefixed persistence may or may not actually hold there. Worst
# case this degrades to the old session-scoped behavior (no regression,
# just a weaker bound than the comment above describes) — but do not quote
# "<=150/hour" as a proven guarantee until it's verified with a live
# two-session check against the deployed engine.
_ORDER_VERIFICATION_FAILURES_KEY = "user:order_verification_failures"


def _record_verification_failure(tool_context: ToolContext, failures: int) -> dict:
    """Increments the failure counter and returns the response to show — the
    generic miss message, or the cap-exceeded message once the increment
    crosses the threshold.

    Called from EVERY failure path (wrong details, invalid input, provider
    exception) so all three are genuinely indistinguishable to the caller and
    all three count against the cap. Routing an exception anywhere else (e.g.
    letting it reach tool_error_handler's outer catch) would leak an oracle:
    a provider that only touches `email` when the order exists returns a
    different message for a real order than for a made-up one, and costs the
    guesser nothing against the 3-attempt cap.
    """
    new_failures = failures + 1
    tool_context.state[_ORDER_VERIFICATION_FAILURES_KEY] = new_failures
    if new_failures >= _MAX_ORDER_VERIFICATION_ATTEMPTS:
        return dict(_CAP_EXCEEDED_RESPONSE)
    return dict(_GENERIC_VERIFICATION_FAILURE)


@tool_error_handler
def verify_order_access(order_id: str, email: str, tool_context: ToolContext) -> dict:
    """Verify order-number + email ownership for a visitor who isn't logged
    in as the order's actual account — the step-up path for anonymous or
    not-yet-logged-in customers (see docs/superpowers/specs/
    2026-08-03-anonymous-identity-and-order-verification-design.md).

    On a match, grants access to exactly this one order for the rest of
    this conversation (tool_context.state["verified_order_ids"]) — never a
    persistent credential, never broader than the single order verified.

    On a mismatch, returns the exact same response whether the order simply
    doesn't exist or exists with a different email on file — the caller
    must not be able to tell those apart. Capped at 3 failed attempts per
    USER (not per conversation — see _ORDER_VERIFICATION_FAILURES_KEY above;
    a session-scoped cap would reset for free on every new chat); once
    capped, no further attempt is even sent to the provider, so the cap
    cannot be bypassed by retrying with different correct details after
    exhausting it.
    """
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    tenant_id = get_tenant_id(tool_context)

    failures = tool_context.state.get(_ORDER_VERIFICATION_FAILURES_KEY, 0)
    if failures >= _MAX_ORDER_VERIFICATION_ATTEMPTS:
        return dict(_CAP_EXCEEDED_RESPONSE)

    # Validated here rather than inside the provider: a provider that only
    # reaches `email.strip()` on the branch where the order exists turns a
    # malformed email into an order-existence oracle.
    if not isinstance(email, str) or "@" not in email:
        return _record_verification_failure(tool_context, failures)

    provider = get_provider(tenant_id)
    try:
        verified = provider.verify_order_owner(tenant_id, order_id, email)
    except Exception:
        # Deliberately caught HERE, not by the outer tool_error_handler: that
        # would return a different message than a normal miss and would not
        # count against the cap.
        logger.exception("verify_order_owner raised for order %s", order_id)
        return _record_verification_failure(tool_context, failures)

    if not verified:
        return _record_verification_failure(tool_context, failures)

    verified_order_ids = tool_context.state.get("verified_order_ids", [])
    if order_id not in verified_order_ids:
        verified_order_ids = verified_order_ids + [order_id]
    tool_context.state["verified_order_ids"] = verified_order_ids

    return {"status": "success", "message": f"Order {order_id} verified. How can I help with it?"}


# =============================================================================
# ORDER HISTORY (authenticated user - no specific order ID)
# =============================================================================


@tool_error_handler
@requires_authenticated_user
def get_order_history(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get complete order history for the authenticated user with full details."""
    tenant_id = get_tenant_id(tool_context)
    logger.info(f"[ORDER HISTORY] Fetching full order history for user: {_user_id}")

    provider = get_provider(tenant_id)
    orders = provider.list_orders_for_customer(tenant_id, _user_id)

    if orders:
        detailed_orders = [
            {
                "order_id": o.order_id,
                "date": o.date,
                "status": o.status,
                "total": o.total,
                "items": o.items,
                "carrier": o.carrier,
                "tracking_number": o.tracking_number,
                "shipping_address": o.shipping_address,
            }
            for o in orders
        ]
        logger.info(f"[ORDER HISTORY] Found {len(detailed_orders)} orders for user {_user_id}")
        return {"status": "success", "orders": detailed_orders, "total_orders": len(detailed_orders)}

    logger.info(f"[ORDER HISTORY] No orders found for user {_user_id}")
    return {"status": "no_orders", "message": "No orders found for your account."}


@tool_error_handler
@requires_authenticated_user
def get_my_order_history(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get order history summary for the authenticated user."""
    tenant_id = get_tenant_id(tool_context)
    logger.info(f"[ORDER HISTORY] Fetching order summary for user: {_user_id}")

    provider = get_provider(tenant_id)
    orders = provider.list_orders_for_customer(tenant_id, _user_id)

    if orders:
        summaries = [{"order_id": o.order_id, "date": o.date, "total": o.total, "status": o.status} for o in orders]
        logger.info(f"[ORDER HISTORY] Found {len(summaries)} orders for user {_user_id}")
        return {"status": "success", "orders": summaries}

    logger.info(f"[ORDER HISTORY] No orders found for user {_user_id}")
    return {"status": "no_orders", "message": "No orders found for your account."}
