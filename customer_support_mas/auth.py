"""
Authorization decorators for tool ownership verification.

Tenant-aware: every check routes through get_provider(tenant_id) instead of
a raw Firestore db_client — tenant_id comes from tool_context.state (see
customer_support_mas/tenancy/context.py), required on every call, never
defaulted (Global Constraints,
docs/superpowers/plans/2026-08-02-multi-tenant-provider-architecture.md).
"""

import logging
from functools import wraps
from typing import Callable, Optional

from google.adk.tools.tool_context import ToolContext

from customer_support_mas.providers.models import Invoice, Order
from customer_support_mas.providers.registry import get_provider
from customer_support_mas.tenancy.context import get_tenant_id

logger = logging.getLogger(__name__)


def audit_log(
    user_id: str, action: str, resource_type: str, resource_id: str, success: bool, details: Optional[str] = None
):
    """Log access attempts for security compliance."""
    if success:
        logger.info(f"[AUDIT] AUTHORIZED: {user_id} -> {action} on {resource_type}/{resource_id}")
    else:
        logger.warning(f"[AUDIT] DENIED: {user_id} -> {action} on {resource_type}/{resource_id} - {details}")


def requires_order_ownership(func: Callable) -> Callable:
    """Verifies the user owns the order before executing the tool.

    Fetches the order once via the tenant's provider, injects it as
    `_order_data` (a dict, same shape tools already expect — see
    dataclasses.asdict below) so downstream tool code doesn't change.
    """

    @wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        tool_context = kwargs.get("tool_context")
        order_id = kwargs.get("order_id")

        if tool_context is None:
            for arg in args:
                if isinstance(arg, ToolContext):
                    tool_context = arg
                    break

        if order_id is None and args:
            for arg in args:
                if isinstance(arg, str) and arg.startswith("ORD-"):
                    order_id = arg
                    break

        if tool_context is None:
            logger.error(f"[AUTH] No tool_context provided to {func.__name__}")
            return {"status": "error", "message": "Internal error: missing context"}

        user_id = tool_context.user_id
        tenant_id = get_tenant_id(tool_context)
        action = func.__name__

        provider = get_provider(tenant_id)
        order = provider.get_order(tenant_id, order_id)

        if order is None:
            audit_log(user_id, action, "order", order_id, False, "Order not found")
            return {"status": "error", "message": f"Order {order_id} not found"}

        if order.customer_id != user_id:
            verified_order_ids = tool_context.state.get("verified_order_ids", [])
            if order_id not in verified_order_ids:
                audit_log(user_id, action, "order", order_id, False, f"Belongs to {order.customer_id}")
                return {"status": "error", "message": f"You don't have permission to access order {order_id}"}
            audit_log(user_id, action, "order", order_id, True, "via conversation-scoped order verification")
        else:
            audit_log(user_id, action, "order", order_id, True)

        kwargs["_order_data"] = _order_to_dict(order)
        kwargs["_order_id"] = order_id

        return func(*args, **kwargs)

    return wrapper


def requires_invoice_ownership(func: Callable) -> Callable:
    """Verifies the user owns the invoice before executing the tool."""

    @wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        tool_context = kwargs.get("tool_context")
        invoice_id = kwargs.get("invoice_id")

        if tool_context is None:
            for arg in args:
                if isinstance(arg, ToolContext):
                    tool_context = arg
                    break

        if invoice_id is None and args:
            for arg in args:
                if isinstance(arg, str) and arg.startswith("INV-"):
                    invoice_id = arg
                    break

        if tool_context is None:
            return {"status": "error", "message": "Internal error: missing context"}

        user_id = tool_context.user_id
        tenant_id = get_tenant_id(tool_context)
        action = func.__name__

        provider = get_provider(tenant_id)
        invoice = provider.get_invoice(tenant_id, invoice_id)

        if invoice is None:
            audit_log(user_id, action, "invoice", invoice_id, False, "Invoice not found")
            return {"status": "error", "message": f"Invoice {invoice_id} not found"}

        if invoice.customer_id != user_id:
            audit_log(user_id, action, "invoice", invoice_id, False, f"Belongs to {invoice.customer_id}")
            return {"status": "error", "message": f"You don't have permission to access invoice {invoice_id}"}

        audit_log(user_id, action, "invoice", invoice_id, True)
        kwargs["_invoice_data"] = _invoice_to_dict(invoice)
        kwargs["_invoice_id"] = invoice_id

        return func(*args, **kwargs)

    return wrapper


def requires_authenticated_user(func: Callable) -> Callable:
    """Ensures the user is authenticated. Extracts user_id and tenant_id."""

    @wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        tool_context = kwargs.get("tool_context")

        if tool_context is None:
            for arg in args:
                if isinstance(arg, ToolContext):
                    tool_context = arg
                    break

        if tool_context is None:
            return {"status": "error", "message": "Internal error: missing context"}

        user_id = tool_context.user_id

        if not user_id:
            logger.warning(f"[AUTH] Unauthenticated access attempt to {func.__name__}")
            return {"status": "error", "message": "Authentication required"}

        # Validates tenant_id is present too — raises MissingTenantError if
        # not, which tool_error_handler (outermost decorator) turns into a
        # graceful error response.
        get_tenant_id(tool_context)

        logger.info(f"[AUTH] User {user_id} calling {func.__name__}")

        kwargs["_user_id"] = user_id
        return func(*args, **kwargs)

    return wrapper


def verify_order_ownership(
    order_id: str,
    user_id: str,
    tenant_id: str,
    action: str = "access",
    verified_order_ids: Optional[list[str]] = None,
) -> tuple[bool, Optional[dict], str]:
    """Verify that an order belongs to the authenticated user, for workflow
    tools where decorators don't fit (SequentialAgent tools controlling
    escalation) — see agents/refund/tools.py.

    tenant_id is now a required keyword argument — every call site in
    refund/tools.py is updated in Task 6 to pass it via get_tenant_id(tool_context).

    `verified_order_ids` is the conversation-scoped grant from
    verify_order_access (tool_context.state["verified_order_ids"]) — an
    order in that list is authorized even for a caller whose user_id
    doesn't match the order's customer_id, but ONLY for that specific
    order_id. Optional and defaults to none, so existing call sites that
    don't yet thread it through keep their current (correct) behavior.
    """
    provider = get_provider(tenant_id)
    order = provider.get_order(tenant_id, order_id)

    if order is None:
        audit_log(user_id, action, "order", order_id, False, "Order not found")
        return False, None, f"Order {order_id} not found"

    if order.customer_id != user_id:
        if not verified_order_ids or order_id not in verified_order_ids:
            audit_log(user_id, action, "order", order_id, False, f"Belongs to {order.customer_id}")
            return False, None, f"You don't have permission to access order {order_id}"
        audit_log(user_id, action, "order", order_id, True, "via conversation-scoped order verification")
    else:
        audit_log(user_id, action, "order", order_id, True)

    return True, _order_to_dict(order), ""


def _order_to_dict(order: Order) -> dict:
    return {
        "customer_id": order.customer_id,
        "status": order.status,
        "date": order.date,
        "items": order.items,
        "subtotal": order.subtotal,
        "tax": order.tax,
        "total": order.total,
        "carrier": order.carrier,
        "tracking_number": order.tracking_number,
        "estimated_delivery": order.estimated_delivery,
        "delivered_date": order.delivered_date,
        "shipping_address": order.shipping_address,
        "timeline": order.timeline,
    }


def _invoice_to_dict(invoice: Invoice) -> dict:
    """The `_invoice_data` payload injected into decorated tools. Delegates to
    Invoice.as_response_dict() so the decorator path and the direct
    provider path can never disagree about which invoice fields survive —
    they used to, and the decorator's hand-written subset silently dropped
    date/due_date/items/subtotal/tax/total. `invoice_id` is stripped because
    every consuming tool supplies it itself."""
    invoice_data = invoice.as_response_dict()
    invoice_data.pop("invoice_id", None)
    return invoice_data
