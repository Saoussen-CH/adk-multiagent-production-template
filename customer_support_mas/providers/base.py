"""CommerceProvider: the backend-agnostic interface every commerce data
source (native Firestore, Shopify, future platforms) implements.

`tenant_id` is the required first argument on every method — there is no
method signature that can be called without one. This is the concrete,
enforced (and tested — see tests/unit/test_cross_tenant_isolation.py)
guarantee behind "no implicit tenant" (Global Constraints, this plan).
"""

from typing import Optional, Protocol

from customer_support_mas.providers.models import Inventory, Invoice, Order, Payment, Product, RefundResult


class CommerceProvider(Protocol):
    def get_order(self, tenant_id: str, order_id: str) -> Optional[Order]: ...

    def list_orders_for_customer(self, tenant_id: str, customer_id: str) -> list[Order]: ...

    def get_product(self, tenant_id: str, product_id: str) -> Optional[Product]: ...

    def get_inventory(self, tenant_id: str, product_id: str) -> Optional[Inventory]: ...

    def get_invoice(self, tenant_id: str, invoice_id: str) -> Optional[Invoice]: ...

    def get_invoice_by_order(self, tenant_id: str, order_id: str) -> Optional[Invoice]: ...

    def list_invoices_for_customer(self, tenant_id: str, customer_id: str) -> list[Invoice]: ...

    def get_payment(self, tenant_id: str, order_id: str) -> Optional[Payment]: ...

    def list_payments_for_customer(self, tenant_id: str, customer_id: str) -> list[Payment]: ...

    def list_refunds_for_order(self, tenant_id: str, order_id: str) -> list[dict]: ...

    def get_reviews_for_product(self, tenant_id: str, product_id: str) -> list[dict]: ...

    def search_products(self, tenant_id: str, query: str, limit: int = 5) -> list[Product]: ...

    def verify_order_ownership(
        self, tenant_id: str, order_id: str, customer_id: str
    ) -> tuple[bool, Optional[Order], str]: ...

    def verify_order_owner(self, tenant_id: str, order_id: str, email: str) -> bool:
        """Prove order-number + email ownership without requiring the caller
        to be logged in as the order's actual customer_id — the anonymous/
        not-yet-logged-in step-up path (see docs/superpowers/specs/
        2026-08-03-anonymous-identity-and-order-verification-design.md).
        Returns False for a nonexistent order, a wrong email, or an order
        whose customer has no matching email on file — the caller must not
        be able to distinguish which case occurred."""
        ...

    def verify_invoice_ownership(
        self, tenant_id: str, invoice_id: str, customer_id: str
    ) -> tuple[bool, Optional[Invoice], str]: ...

    def execute_refund(
        self,
        tenant_id: str,
        order_id: str,
        customer_id: str,
        items: list[dict],
        amount: float,
        reason: Optional[str] = None,
        reason_category: Optional[str] = None,
    ) -> RefundResult:
        """`reason`/`reason_category` are optional pass-through metadata for
        the refund record — backend/app/refund_approvals.py's approve_refund
        (Task 7) always supplies them (sourced from the staged
        refund_requests doc's policy-derived reason label/code), and any
        implementation that persists a refund record should preserve them if
        given. They're optional here (not required positional args) so a
        provider whose native refund API doesn't have an equivalent concept
        isn't forced to invent one."""
        ...
