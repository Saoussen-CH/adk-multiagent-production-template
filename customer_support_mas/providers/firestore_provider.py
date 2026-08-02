"""FirestoreProvider: the refactor of every direct Firestore call that used
to live inline in agents/{order,product,billing,refund}/tools.py and
auth.py. Behavior for a given tenant's data is byte-identical to the
pre-multi-tenant single-store behavior — this is a refactor, not a
redesign (Global Constraints, plan
docs/superpowers/plans/2026-08-02-multi-tenant-provider-architecture.md).

Each tenant using this provider has its own named Firestore database
(provider_config["database_id"]) — physical data separation within a
shared pool project, not just a query-level filter (spec section 6).
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from customer_support_mas.database import get_db_client
from customer_support_mas.providers.models import Inventory, Invoice, Order, Payment, Product, RefundResult
from customer_support_mas.services.rag_search import get_rag_search

logger = logging.getLogger(__name__)


def _order_from_doc(order_id: str, data: dict) -> Order:
    return Order(
        order_id=order_id,
        customer_id=data.get("customer_id"),
        status=data.get("status", ""),
        date=data.get("date"),
        items=data.get("items", []),
        subtotal=data.get("subtotal"),
        tax=data.get("tax"),
        total=data.get("total"),
        carrier=data.get("carrier"),
        tracking_number=data.get("tracking_number"),
        estimated_delivery=data.get("estimated_delivery"),
        delivered_date=data.get("delivered_date"),
        shipping_address=data.get("shipping_address"),
        timeline=data.get("timeline", []),
    )


def _product_from_doc(product_id: str, data: dict) -> Product:
    return Product(
        product_id=product_id,
        name=data.get("name", ""),
        price=data.get("price", 0.0),
        description=data.get("description"),
        category=data.get("category"),
        keywords=data.get("keywords", []),
        specs=data.get("specs", {}),
        warranty=data.get("warranty"),
        rating=data.get("rating"),
    )


def _invoice_from_doc(invoice_id: str, data: dict) -> Invoice:
    """Field names here must match what invoice documents actually store
    (see customer_support_mas/database/fixtures.py). An earlier version read
    a non-existent `amount` key and dropped date/due_date/items/subtotal/
    tax/total entirely, so every invoice reached the agent as a bare
    id + status with a null amount."""
    return Invoice(
        invoice_id=invoice_id,
        customer_id=data.get("customer_id"),
        order_id=data.get("order_id"),
        date=data.get("date"),
        due_date=data.get("due_date"),
        status=data.get("status"),
        items=data.get("items", []),
        subtotal=data.get("subtotal"),
        tax=data.get("tax"),
        total=data.get("total"),
    )


def _payment_from_doc(order_id: str, data: dict) -> Payment:
    """Payment documents store `payment_status` and `amount_due`/`amount_paid`
    — not `status`/`amount`. Reading the latter silently produced
    {"status": None, "amount": None} for every payment."""
    return Payment(
        order_id=order_id,
        customer_id=data.get("customer_id"),
        payment_status=data.get("payment_status"),
        amount_due=data.get("amount_due"),
        amount_paid=data.get("amount_paid"),
        payment_method=data.get("payment_method"),
        payment_date=data.get("payment_date"),
        transaction_id=data.get("transaction_id"),
    )


def _inventory_from_doc(product_id: str, data: dict) -> Inventory:
    return Inventory(
        product_id=product_id,
        total_stock=data.get("total_stock"),
        warehouses=data.get("warehouses", {}),
    )


class FirestoreProvider:
    """Native Firestore backend — implements CommerceProvider."""

    def __init__(self, provider_config: dict):
        self._database_id = provider_config["database_id"]

    @property
    def _db(self):
        return get_db_client(self._database_id)

    def get_order(self, tenant_id: str, order_id: str) -> Optional[Order]:
        doc = self._db.collection("orders").document(order_id).get()
        if not doc.exists:
            return None
        return _order_from_doc(order_id, doc.to_dict())

    def list_orders_for_customer(self, tenant_id: str, customer_id: str) -> list[Order]:
        query = self._db.collection("orders").where("customer_id", "==", customer_id)
        return [_order_from_doc(doc.id, doc.to_dict()) for doc in query.stream()]

    def get_product(self, tenant_id: str, product_id: str) -> Optional[Product]:
        doc = self._db.collection("products").document(product_id).get()
        if not doc.exists:
            return None
        return _product_from_doc(product_id, doc.to_dict())

    def get_inventory(self, tenant_id: str, product_id: str) -> Optional[Inventory]:
        doc = self._db.collection("inventory").document(product_id).get()
        if not doc.exists:
            return None
        return _inventory_from_doc(product_id, doc.to_dict())

    def get_invoice(self, tenant_id: str, invoice_id: str) -> Optional[Invoice]:
        doc = self._db.collection("invoices").document(invoice_id).get()
        if not doc.exists:
            return None
        return _invoice_from_doc(invoice_id, doc.to_dict())

    def get_invoice_by_order(self, tenant_id: str, order_id: str) -> Optional[Invoice]:
        query = self._db.collection("invoices").where("order_id", "==", order_id)
        docs = list(query.stream())
        if not docs:
            return None
        return _invoice_from_doc(docs[0].id, docs[0].to_dict())

    def list_invoices_for_customer(self, tenant_id: str, customer_id: str) -> list[Invoice]:
        query = self._db.collection("invoices").where("customer_id", "==", customer_id)
        return [_invoice_from_doc(doc.id, doc.to_dict()) for doc in query.stream()]

    def get_payment(self, tenant_id: str, order_id: str) -> Optional[Payment]:
        doc = self._db.collection("payments").document(order_id).get()
        if not doc.exists:
            return None
        return _payment_from_doc(order_id, doc.to_dict())

    def list_payments_for_customer(self, tenant_id: str, customer_id: str) -> list[Payment]:
        query = self._db.collection("payments").where("customer_id", "==", customer_id)
        return [_payment_from_doc(doc.id, doc.to_dict()) for doc in query.stream()]

    def list_refunds_for_order(self, tenant_id: str, order_id: str) -> list[dict]:
        query = self._db.collection("refunds").where("order_id", "==", order_id)
        return [doc.to_dict() for doc in query.stream()]

    def get_reviews_for_product(self, tenant_id: str, product_id: str) -> list[dict]:
        """The `reviews` collection stores one summary document per product
        (keyed by product_id), not a queryable-by-foreign-key collection —
        unlike list_refunds_for_order there's no `.where()` here, just a
        by-ID lookup. Wrapped in a list (empty when absent) to match the
        list[dict] shape the rest of the raw-dict provider methods use."""
        doc = self._db.collection("reviews").document(product_id).get()
        if not doc.exists:
            return []
        return [doc.to_dict()]

    def search_products(self, tenant_id: str, query: str, limit: int = 5) -> list[Product]:
        """RAG (semantic) search scoped to this tenant's database, with a
        keyword fallback — ports the exact logic that used to live inline in
        agents/product/tools.py's search_products, now tenant-scoped by
        construction (get_rag_search(self._database_id) never touches
        another tenant's embeddings; the keyword fallback queries self._db,
        which is this tenant's Firestore database)."""
        try:
            rag = get_rag_search(self._database_id)
            rag_results = rag.search(query, limit=limit)
            if rag_results:
                return [
                    Product(
                        product_id=r["id"],
                        name=r.get("name", ""),
                        price=r.get("price", 0.0),
                        # `description` is in every RAG hit (see
                        # services/rag_search.py and tests/mock_rag_search.py)
                        # — dropping it made RAG-backed search results
                        # strictly poorer than keyword-fallback ones.
                        description=r.get("description"),
                        category=r.get("category"),
                    )
                    for r in rag_results
                ]
        except Exception as e:
            logger.warning(
                "[FirestoreProvider] RAG search failed for tenant %s: %s, falling back to keyword", tenant_id, e
            )

        query_lower = query.lower().strip()
        search_terms = [query_lower]
        if query_lower.endswith("s"):
            search_terms.append(query_lower[:-1])
        else:
            search_terms.append(query_lower + "s")

        results = []
        for doc in self._db.collection("products").stream():
            data = doc.to_dict()
            name = data.get("name", "").lower()
            category = data.get("category", "").lower()
            keywords = data.get("keywords", [])
            if any(term in name or term in category or term in keywords for term in search_terms):
                results.append(_product_from_doc(doc.id, data))

        return results[:limit]

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
        invoice = self.get_invoice(tenant_id, invoice_id)
        if invoice is None:
            return False, None, f"Invoice {invoice_id} not found"
        if invoice.customer_id != customer_id:
            return False, None, f"You don't have permission to access invoice {invoice_id}"
        return True, invoice, ""

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
        """Write the final refunds record — called only after human approval
        (backend/app/refund_approvals.py's approve_refund, Task 7). This is
        the money-moving step; the FirestoreProvider's version just writes a
        record (no real payment gateway is integrated yet — see project
        memory real-production-project-not-demo.md). A ShopifyProvider's
        execute_refund would call Shopify's real Refund API instead.

        `reason`/`reason_category` are optional (see CommerceProvider.
        execute_refund's docstring) but approve_refund always supplies them
        today, sourced from the staged refund_requests doc — the approver
        UI reads both fields directly off the written refund record, so
        they're included here when given rather than dropped. `items` is
        stored verbatim; approve_refund is responsible for any per-item
        enrichment (e.g. per-item refund_amount) before calling this, since
        that shape is specific to how this backend's approver UI reads
        refund records, not a general CommerceProvider concern."""
        refund_id = f"REF-{order_id.replace('ORD-', '')}-{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        refund_record = {
            "refund_id": refund_id,
            "order_id": order_id,
            "customer_id": customer_id,
            "status": "pending",
            "items": items,
            "total_refund_amount": amount,
            "created_at": now,
        }
        if reason is not None:
            refund_record["reason"] = reason
        if reason_category is not None:
            refund_record["reason_category"] = reason_category
        self._db.collection("refunds").document(refund_id).set(refund_record)
        logger.info("[FirestoreProvider] Executed refund %s for order %s: $%s", refund_id, order_id, amount)
        return RefundResult(success=True, refund_id=refund_id, message="Refund recorded")
