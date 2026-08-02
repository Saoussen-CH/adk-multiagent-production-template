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
    product_id: str
    name: str
    price: float
    description: Optional[str] = None
    category: Optional[str] = None
    keywords: list[str] = field(default_factory=list)


@dataclass
class Invoice:
    invoice_id: str
    customer_id: str
    order_id: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None


@dataclass
class Payment:
    order_id: str
    customer_id: str
    status: Optional[str] = None
    amount: Optional[float] = None


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
