"""
Billing-related tools for the customer support system.

This module contains all tools for invoices, payments, and refunds.
All tools verify ownership using decorators - users can only access their own billing data.
"""

import logging

from google.adk.tools.tool_context import ToolContext

from customer_support_mas.auth import (
    requires_authenticated_user,
    requires_invoice_ownership,
    requires_order_ownership,
)
from customer_support_mas.error_handling import tool_error_handler
from customer_support_mas.providers.registry import get_provider
from customer_support_mas.tenancy.context import get_tenant_id
from customer_support_mas.validation import (
    validate_invoice_id,
    validate_order_id,
    validation_error_response,
)

logger = logging.getLogger(__name__)


# =============================================================================
# INVOICE TOOLS (ownership verified)
# =============================================================================


@tool_error_handler
@requires_invoice_ownership
def get_invoice(invoice_id: str, tool_context: ToolContext, _invoice_data: dict = None, **kwargs) -> dict:
    """Get invoice by invoice ID (e.g., INV-2025-001). Only accessible if the invoice belongs to you.

    Args:
        invoice_id: The invoice ID to retrieve
        tool_context: ADK ToolContext (automatically injected)
        _invoice_data: Pre-fetched invoice data (injected by decorator)
    """
    # Input validation (decorator handles authorization after this)
    is_valid, error_msg = validate_invoice_id(invoice_id)
    if not is_valid:
        return validation_error_response(error_msg)

    return {"status": "success", "invoice": {"invoice_id": invoice_id, **_invoice_data}}


@tool_error_handler
@requires_order_ownership
def get_invoice_by_order_id(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Get invoice by order ID. Only accessible if the order belongs to you."""
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    tenant_id = get_tenant_id(tool_context)
    provider = get_provider(tenant_id)
    invoice = provider.get_invoice_by_order(tenant_id, order_id)

    if invoice is None:
        return {"status": "not_found", "message": f"No invoice found for order {order_id}"}

    return {"status": "success", "invoice": invoice.as_response_dict()}


@tool_error_handler
@requires_authenticated_user
def get_my_invoices(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get all invoices for the authenticated user."""
    tenant_id = get_tenant_id(tool_context)
    logger.info(f"[BILLING] Fetching all invoices for user: {_user_id}")

    provider = get_provider(tenant_id)
    invoices = provider.list_invoices_for_customer(tenant_id, _user_id)

    if invoices:
        logger.info(f"[BILLING] Found {len(invoices)} invoices for user {_user_id}")
        return {
            "status": "success",
            "invoices": [i.as_response_dict() for i in invoices],
            "total_invoices": len(invoices),
        }

    logger.info(f"[BILLING] No invoices found for user {_user_id}")
    return {"status": "no_invoices", "message": "No invoices found for your account."}


# =============================================================================
# PAYMENT TOOLS (ownership verified)
# =============================================================================


@tool_error_handler
@requires_order_ownership
def check_payment_status(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Check payment status for an order. Only accessible if the order belongs to you."""
    tenant_id = get_tenant_id(tool_context)
    provider = get_provider(tenant_id)
    payment = provider.get_payment(tenant_id, order_id)

    if payment is None:
        return {"status": "not_found", "message": f"No payment record found for order {order_id}"}

    return {"status": "success", "payment": payment.as_response_dict()}


@tool_error_handler
@requires_authenticated_user
def get_my_payments(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get all payment records for the authenticated user."""
    tenant_id = get_tenant_id(tool_context)
    logger.info(f"[BILLING] Fetching all payments for user: {_user_id}")

    provider = get_provider(tenant_id)
    payments = provider.list_payments_for_customer(tenant_id, _user_id)

    if payments:
        logger.info(f"[BILLING] Found {len(payments)} payments for user {_user_id}")
        return {
            "status": "success",
            "payments": [p.as_response_dict() for p in payments],
            "total_payments": len(payments),
        }

    logger.info(f"[BILLING] No payments found for user {_user_id}")
    return {"status": "no_payments", "message": "No payment records found for your account."}


# =============================================================================
# REFUND INFO TOOLS (delegate to refund.tools, tenant-scoped)
# =============================================================================


@tool_error_handler
def get_acceptable_refund_reasons(tool_context: ToolContext) -> dict:
    """List acceptable and unacceptable refund reasons."""
    tenant_id = get_tenant_id(tool_context)
    from customer_support_mas.agents.refund.tools import get_acceptable_refund_reasons as _get_reasons

    return _get_reasons(tenant_id)
