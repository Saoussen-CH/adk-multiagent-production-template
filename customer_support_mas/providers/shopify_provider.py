"""ShopifyProvider: stubbed Shopify Admin API backend.

Mocked responses by default (SHOPIFY_MOCK=true) — mirrors
mcp_servers/fedex_tracking/fedex_client.py's mock-mode pattern. No real
Shopify Partner account/dev store exists yet (see project memory
real-production-project-not-demo.md and docs/superpowers/specs/
2026-08-02-multi-tenant-provider-architecture-design.md section 2) — real
OAuth + Admin API + webhook sync is explicitly future work, gated on that
account existing.
"""

import os
import uuid
from typing import Optional

from customer_support_mas.providers.models import Inventory, Invoice, Order, Payment, Product, RefundResult

_MOCK_ORDERS = {
    "SHOPIFY-ORD-1001": {
        "customer_id": "shopify-customer-1",
        "status": "fulfilled",
        "items": [{"product_id": "SHOPIFY-PROD-1", "name": "Mock Shopify Product", "price": 29.99, "qty": 1}],
        "total": 29.99,
        "carrier": "UPS",
        "tracking_number": "1Z999MOCK",
    },
}

_MOCK_PRODUCTS = {
    "SHOPIFY-PROD-1": {"name": "Mock Shopify Product", "price": 29.99, "description": "A stubbed Shopify product"},
}


class ShopifyProvider:
    """Stubbed Shopify Admin API backend — implements CommerceProvider."""

    def __init__(self, provider_config: dict):
        self._shop_domain = provider_config["shop_domain"]
        self._mock = os.environ.get("SHOPIFY_MOCK", "true").lower() == "true"
        if not self._mock:
            raise NotImplementedError(
                "Real Shopify Admin API integration is not implemented yet — "
                "set SHOPIFY_MOCK=true, or see docs/superpowers/specs/"
                "2026-08-02-multi-tenant-provider-architecture-design.md section 2"
            )

    def get_order(self, tenant_id: str, order_id: str) -> Optional[Order]:
        data = _MOCK_ORDERS.get(order_id)
        if data is None:
            return None
        return Order(order_id=order_id, **data)

    def list_orders_for_customer(self, tenant_id: str, customer_id: str) -> list[Order]:
        return [Order(order_id=oid, **data) for oid, data in _MOCK_ORDERS.items() if data["customer_id"] == customer_id]

    def get_product(self, tenant_id: str, product_id: str) -> Optional[Product]:
        data = _MOCK_PRODUCTS.get(product_id)
        if data is None:
            return None
        return Product(product_id=product_id, **data)

    def get_inventory(self, tenant_id: str, product_id: str) -> Optional[Inventory]:
        if product_id not in _MOCK_PRODUCTS:
            return None
        # Shopify models stock per location; the mock exposes one location so
        # the normalized `warehouses` breakdown is still populated rather
        # than empty (matching FirestoreProvider's shape for any caller).
        return Inventory(product_id=product_id, total_stock=42, warehouses={"shopify-default": 42})

    def get_invoice(self, tenant_id: str, invoice_id: str) -> Optional[Invoice]:
        return None  # Shopify has no separate "invoice" concept for typical DTC orders

    def get_invoice_by_order(self, tenant_id: str, order_id: str) -> Optional[Invoice]:
        return None

    def list_invoices_for_customer(self, tenant_id: str, customer_id: str) -> list[Invoice]:
        return []

    def get_payment(self, tenant_id: str, order_id: str) -> Optional[Payment]:
        order = self.get_order(tenant_id, order_id)
        if order is None:
            return None
        return Payment(order_id=order_id, customer_id=order.customer_id, payment_status="paid", amount_paid=order.total)

    def list_payments_for_customer(self, tenant_id: str, customer_id: str) -> list[Payment]:
        return [
            Payment(order_id=o.order_id, customer_id=o.customer_id, payment_status="paid", amount_paid=o.total)
            for o in self.list_orders_for_customer(tenant_id, customer_id)
        ]

    def list_refunds_for_order(self, tenant_id: str, order_id: str) -> list[dict]:
        return []

    def get_reviews_for_product(self, tenant_id: str, product_id: str) -> list[dict]:
        """No mock review data modeled yet — Shopify's real Admin API has no
        native reviews resource either (it's typically a third-party app,
        e.g. Shopify Product Reviews or Judge.me); returning [] keeps this
        method's shape identical to FirestoreProvider's for any caller that
        iterates it uniformly (see agents/product/tools.py)."""
        return []

    def search_products(self, tenant_id: str, query: str, limit: int = 5) -> list[Product]:
        query_lower = query.lower()
        matches = [
            Product(product_id=pid, name=data["name"], price=data["price"], description=data.get("description"))
            for pid, data in _MOCK_PRODUCTS.items()
            if query_lower in data["name"].lower()
        ]
        return matches[:limit]

    def verify_order_ownership(
        self, tenant_id: str, order_id: str, customer_id: str
    ) -> tuple[bool, Optional[Order], str]:
        order = self.get_order(tenant_id, order_id)
        if order is None:
            return False, None, f"Order {order_id} not found"
        if order.customer_id != customer_id:
            return False, None, f"You don't have permission to access order {order_id}"
        return True, order, ""

    def verify_invoice_ownership(
        self, tenant_id: str, invoice_id: str, customer_id: str
    ) -> tuple[bool, Optional[Invoice], str]:
        return False, None, "Shopify orders have no separate invoice concept"

    def verify_order_owner(self, tenant_id: str, order_id: str, email: str) -> bool:
        """No account/email concept is modeled in the mock yet (see module
        docstring — real Shopify OAuth/Admin API sync is future work), so
        this fails closed unconditionally rather than guessing at a match,
        consistent with the "must not be able to distinguish which case
        occurred" constraint on CommerceProvider.verify_order_owner."""
        return False

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
        """Mock mode: logs and returns a fake success, matching
        fedex_client.py's mock behavior. Real mode would POST to Shopify's
        Refund API (/admin/api/refunds.json) — not implemented (see
        __init__'s NotImplementedError for the real-mode path).

        `reason`/`reason_category` are accepted (not just `**kwargs`'d away)
        to match CommerceProvider.execute_refund's actual signature —
        backend/app/refund_approvals.py's approve_refund always passes them
        as keyword args regardless of which provider tenant_id resolves to,
        so a narrower signature here would raise TypeError in production
        for a Shopify-backed tenant. This stub doesn't persist them
        anywhere (no backing store to write to yet)."""
        fake_refund_id = f"shopify-mock-refund-{uuid.uuid4().hex[:8]}"
        return RefundResult(success=True, refund_id=fake_refund_id, message="Mock Shopify refund recorded")
