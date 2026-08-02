"""Normalized domain models every CommerceProvider implementation returns.

Every provider (FirestoreProvider, ShopifyProvider, future platform
providers) returns exactly these shapes regardless of the backend's native
data format — this is what makes agent tool code backend-agnostic. Field
sets match what the current tool functions already read from raw Firestore
dicts (see customer_support_mas/agents/{order,product,billing,refund}/tools.py)
so the refactor in later tasks is a rename, not a redesign.

`items` on Order/RefundRecord stays list[dict] rather than a nested
dataclass — item shape was never backend-specific (Firestore, Shopify line
items, etc. all normalize to the same {product_id, name, price, qty} dict),
so a stronger type here wouldn't serve tenant/backend abstraction, just add
a conversion layer with no payoff.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Order:
    order_id: str
    customer_id: str
    status: str
    date: Optional[str] = None
    items: list[dict] = field(default_factory=list)
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    estimated_delivery: Optional[str] = None
    delivered_date: Optional[str] = None
    shipping_address: Optional[dict] = None
    timeline: list[dict] = field(default_factory=list)


@dataclass
class Product:
    """A catalog item. `specs`/`warranty`/`rating` are part of the normalized
    shape because the product agent quotes them verbatim to customers (see
    the recorded `get_product_info` traces in
    tests/unit/product_agent_direct.test.json) — same class of silent data
    loss as Invoice/Payment above if they're dropped."""

    product_id: str
    name: str
    price: float
    description: Optional[str] = None
    category: Optional[str] = None
    keywords: list[str] = field(default_factory=list)
    specs: dict = field(default_factory=dict)
    warranty: Optional[str] = None
    rating: Optional[float] = None

    def as_response_dict(self) -> dict:
        """The detail shape agent tools return to the LLM. `id` (not
        `product_id`) is the key the agent instructions and every recorded
        eval trace already use."""
        response = {"id": self.product_id, "name": self.name, "price": self.price}
        for key in ("category", "description"):
            value = getattr(self, key)
            if value is not None:
                response[key] = value
        if self.keywords:
            response["keywords"] = self.keywords
        if self.specs:
            response["specs"] = self.specs
        if self.warranty is not None:
            response["warranty"] = self.warranty
        if self.rating is not None:
            response["rating"] = self.rating
        return response


@dataclass
class Invoice:
    """A billing document. Field names match what every invoice document
    actually stores (see customer_support_mas/database/fixtures.py) — an
    earlier version of this dataclass carried a single `amount` field that
    no backend ever wrote, so `_invoice_from_doc` mapped it to `None` and
    the agent lost the line items, dates and totals entirely."""

    invoice_id: str
    customer_id: Optional[str] = None
    order_id: Optional[str] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    status: Optional[str] = None
    items: list[dict] = field(default_factory=list)
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None

    @property
    def amount(self) -> Optional[float]:
        """Normalized single-amount view: the customer-facing grand total.
        Derived (not stored) so it can never drift from `total`, which is
        the field backends actually persist."""
        return self.total

    def as_response_dict(self) -> dict:
        """The dict shape agent tools return to the LLM. Keys absent from the
        source document stay absent (rather than becoming explicit nulls),
        matching the pre-provider behavior of spreading the raw Firestore
        document into the tool response."""
        response = {"invoice_id": self.invoice_id}
        for key in ("order_id", "customer_id", "date", "due_date", "status"):
            value = getattr(self, key)
            if value is not None:
                response[key] = value
        if self.items:
            response["items"] = self.items
        for key in ("subtotal", "tax", "total"):
            value = getattr(self, key)
            if value is not None:
                response[key] = value
        return response


@dataclass
class Payment:
    """A payment record for an order.

    `payment_status` / `amount_due` / `amount_paid` are the field names
    payment documents actually use. A previous version of this dataclass
    declared only `status` and `amount`, which matched no stored field, so
    every payment surfaced to the agent as `{"status": None, "amount":
    None}`. `status`/`amount` survive as derived aliases because they are
    the *normalized* vocabulary the CommerceProvider interface speaks."""

    order_id: str
    customer_id: Optional[str] = None
    payment_status: Optional[str] = None
    amount_due: Optional[float] = None
    amount_paid: Optional[float] = None
    payment_method: Optional[str] = None
    payment_date: Optional[str] = None
    transaction_id: Optional[str] = None

    @property
    def status(self) -> Optional[str]:
        """Normalized alias for `payment_status`."""
        return self.payment_status

    @property
    def amount(self) -> Optional[float]:
        """Normalized single-amount view: what was actually paid once the
        payment completed, otherwise what is still owed."""
        return self.amount_paid if self.amount_paid is not None else self.amount_due

    def as_response_dict(self) -> dict:
        """See Invoice.as_response_dict — absent fields stay absent."""
        response = {"order_id": self.order_id}
        for key in (
            "customer_id",
            "payment_status",
            "amount_due",
            "amount_paid",
            "payment_date",
            "transaction_id",
            "payment_method",
        ):
            value = getattr(self, key)
            if value is not None:
                response[key] = value
        return response


@dataclass
class Inventory:
    """Stock for one product. `warehouses` (per-location breakdown) is part
    of the normalized shape because the product agent surfaces it verbatim
    — dropping it silently changed what the agent could tell a customer."""

    product_id: str
    total_stock: Optional[int] = None
    warehouses: dict = field(default_factory=dict)

    @property
    def quantity(self) -> Optional[int]:
        """Normalized alias for `total_stock`."""
        return self.total_stock

    def as_response_dict(self) -> dict:
        response = {"product_id": self.product_id, "total_stock": self.total_stock}
        if self.warehouses:
            response["warehouses"] = self.warehouses
        return response


@dataclass
class RefundRecord:
    order_id: str
    status: str
    items: list[dict] = field(default_factory=list)


@dataclass
class RefundResult:
    success: bool
    refund_id: Optional[str] = None
    message: str = ""
