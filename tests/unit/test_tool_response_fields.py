"""Value-level assertions on what agent tools actually return.

Final-review finding C2: `Payment.status`/`Payment.amount` and
`Invoice.amount` were read from Firestore keys that no seeded document has
(`status`/`amount` instead of `payment_status`/`amount_due`/`amount_paid`
and `total`), so every payment and invoice reached the LLM with null money
fields — and `check_inventory` silently dropped the per-warehouse
breakdown. Nothing caught it because the existing billing tests asserted
only `result["status"] == "success"`.

Every test here therefore asserts *values*, pinned to the seeded fixture
data in `customer_support_mas/database/fixtures.py`. A field-mapping
regression that nulls a field or drops it entirely fails here loudly.

Order tools are covered too even though their mapping is currently correct
— the same class of bug is one careless rename away, and there was no
value-level coverage of `get_order_history`/`get_my_order_history` either.
"""

import pytest

# =============================================================================
# BILLING — payments
# =============================================================================


class TestPaymentFields:
    def test_check_payment_status_pending_order_carries_amount_due(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.billing.tools import check_payment_status

        result = check_payment_status(order_id="ORD-12345", tool_context=mock_tool_context_with_tenant)

        assert result["status"] == "success"
        payment = result["payment"]
        assert payment["order_id"] == "ORD-12345"
        assert payment["customer_id"] == "demo-user-001"
        assert payment["payment_status"] == "Pending"
        assert payment["amount_due"] == 1295.98
        assert payment["payment_method"] == "Credit Card (ending 4242)"
        # An unpaid order has no amount_paid — absent, not a null.
        assert "amount_paid" not in payment

    def test_check_payment_status_completed_order_carries_amount_paid(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.billing.tools import check_payment_status

        result = check_payment_status(order_id="ORD-67890", tool_context=mock_tool_context_with_tenant)

        payment = result["payment"]
        assert payment["payment_status"] == "Completed"
        assert payment["amount_paid"] == 215.99
        assert payment["payment_date"] == "2025-01-10"
        assert payment["transaction_id"] == "TXN-789456"
        assert "amount_due" not in payment

    def test_get_my_payments_returns_real_amounts_for_every_record(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.billing.tools import get_my_payments

        result = get_my_payments(tool_context=mock_tool_context_with_tenant)

        assert result["status"] == "success"
        by_order = {p["order_id"]: p for p in result["payments"]}
        assert by_order["ORD-12345"]["amount_due"] == 1295.98
        assert by_order["ORD-67890"]["amount_paid"] == 215.99
        assert by_order["ORD-11111"]["amount_paid"] == 485.99
        for payment in result["payments"]:
            assert payment["payment_status"] in ("Pending", "Completed")

    def test_payment_model_normalized_aliases_track_the_stored_fields(self):
        """`status`/`amount` are the CommerceProvider's normalized vocabulary;
        they must be derived from the stored fields, never independent."""
        from customer_support_mas.providers.models import Payment

        unpaid = Payment(order_id="ORD-1", payment_status="Pending", amount_due=10.0)
        assert unpaid.status == "Pending"
        assert unpaid.amount == 10.0

        paid = Payment(order_id="ORD-2", payment_status="Completed", amount_due=10.0, amount_paid=10.0)
        assert paid.amount == 10.0

        unknown = Payment(order_id="ORD-3")
        assert unknown.status is None
        assert unknown.amount is None


# =============================================================================
# BILLING — invoices
# =============================================================================


class TestInvoiceFields:
    def test_get_invoice_returns_line_items_dates_and_totals(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.billing.tools import get_invoice

        result = get_invoice(invoice_id="INV-2025-001", tool_context=mock_tool_context_with_tenant)

        assert result["status"] == "success"
        invoice = result["invoice"]
        assert invoice["invoice_id"] == "INV-2025-001"
        assert invoice["order_id"] == "ORD-12345"
        assert invoice["customer_id"] == "demo-user-001"
        assert invoice["date"] == "2025-01-15"
        assert invoice["due_date"] == "2025-02-15"
        assert invoice["status"] == "Pending"
        assert invoice["subtotal"] == 1199.98
        assert invoice["tax"] == 96.00
        assert invoice["total"] == 1295.98
        assert [i["description"] for i in invoice["items"]] == [
            "ProBook Laptop 15",
            "Wireless Headphones Pro",
        ]

    def test_invoice_without_a_due_date_omits_the_key(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.billing.tools import get_invoice

        invoice = get_invoice(invoice_id="INV-2025-002", tool_context=mock_tool_context_with_tenant)["invoice"]

        assert invoice["total"] == 215.99
        assert "due_date" not in invoice

    def test_get_invoice_by_order_id_returns_the_same_full_shape(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.billing.tools import get_invoice_by_order_id

        invoice = get_invoice_by_order_id(order_id="ORD-12345", tool_context=mock_tool_context_with_tenant)["invoice"]

        assert invoice["invoice_id"] == "INV-2025-001"
        assert invoice["total"] == 1295.98
        assert invoice["items"]

    def test_get_my_invoices_returns_totals_for_every_invoice(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.billing.tools import get_my_invoices

        result = get_my_invoices(tool_context=mock_tool_context_with_tenant)

        assert result["total_invoices"] == 3
        totals = {i["invoice_id"]: i["total"] for i in result["invoices"]}
        assert totals == {
            "INV-2025-001": 1295.98,
            "INV-2025-002": 215.99,
            "INV-2024-003": 485.99,
        }

    def test_invoice_amount_alias_is_the_grand_total(self):
        from customer_support_mas.providers.models import Invoice

        assert Invoice(invoice_id="INV-1", total=42.0).amount == 42.0
        assert Invoice(invoice_id="INV-2").amount is None


# =============================================================================
# PRODUCT — inventory and details
# =============================================================================


class TestProductFields:
    def test_check_inventory_keeps_the_per_warehouse_breakdown(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.product.tools import check_inventory

        result = check_inventory(product_id="PROD-001", tool_context=mock_tool_context_with_tenant)

        inventory = result["inventory"]
        assert inventory["product_id"] == "PROD-001"
        assert inventory["total_stock"] == 45
        assert inventory["warehouses"] == {"US-West": 20, "US-East": 15, "EU": 10}

    def test_get_product_details_keeps_specs_warranty_and_rating(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.product.tools import get_product_details

        product = get_product_details(product_id="PROD-002", tool_context=mock_tool_context_with_tenant)["product"]

        assert product["id"] == "PROD-002"
        assert product["price"] == 199.99
        assert product["specs"]["battery"] == "30 hours"
        assert product["warranty"] == "1 year"
        assert product["rating"] == 4.7
        assert "headphones" in product["keywords"]

    def test_get_product_info_bundles_details_inventory_and_reviews(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.product.tools import get_product_info

        result = get_product_info("PROD-001", mock_tool_context_with_tenant)

        assert result["data_fetched"] == ["details", "inventory", "reviews"]
        assert result["details"]["warranty"] == "2 years"
        assert result["inventory"]["warehouses"]["EU"] == 10
        assert result["reviews"]["avg_rating"] == 4.5


# =============================================================================
# ORDERS — currently correct, pinned so it stays that way
# =============================================================================


class TestOrderFields:
    def test_get_order_history_returns_totals_items_and_shipping(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.order.tools import get_order_history

        result = get_order_history(tool_context=mock_tool_context_with_tenant)

        assert result["status"] == "success"
        assert result["total_orders"] == 3
        by_id = {o["order_id"]: o for o in result["orders"]}

        in_transit = by_id["ORD-12345"]
        assert in_transit["status"] == "In Transit"
        assert in_transit["total"] == 1295.98
        assert in_transit["carrier"] == "FastShip"
        assert in_transit["tracking_number"] == "FS789456123"
        assert in_transit["shipping_address"]["city"] == "San Francisco"
        assert [i["product_id"] for i in in_transit["items"]] == ["PROD-001", "PROD-002"]

        assert by_id["ORD-67890"]["total"] == 215.99
        assert by_id["ORD-11111"]["total"] == 485.99

    def test_get_my_order_history_summary_carries_status_and_total(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.order.tools import get_my_order_history

        result = get_my_order_history(tool_context=mock_tool_context_with_tenant)

        by_id = {o["order_id"]: o for o in result["orders"]}
        assert by_id["ORD-12345"]["total"] == 1295.98
        assert by_id["ORD-12345"]["status"] == "In Transit"
        assert by_id["ORD-67890"]["status"] == "Delivered"
        assert all(o["date"] for o in result["orders"])

    def test_get_order_details_returns_full_money_breakdown(self, mock_tool_context_with_tenant):
        from customer_support_mas.agents.order.tools import get_order_details

        order = get_order_details(order_id="ORD-12345", tool_context=mock_tool_context_with_tenant)["order"]

        assert order["subtotal"] == 1199.98
        assert order["tax"] == 96.00
        assert order["total"] == 1295.98
        assert len(order["timeline"]) == 4


# =============================================================================
# Provider-level mapping (one layer below the tools)
# =============================================================================


@pytest.fixture
def provider():
    from customer_support_mas.providers.registry import get_provider

    return get_provider("test-tenant")


def test_firestore_provider_maps_payment_document_fields(provider):
    payment = provider.get_payment("test-tenant", "ORD-67890")

    assert payment.payment_status == "Completed"
    assert payment.amount_paid == 215.99
    assert payment.transaction_id == "TXN-789456"
    # The normalized aliases must resolve, not be None — the original bug.
    assert payment.status == "Completed"
    assert payment.amount == 215.99


def test_firestore_provider_maps_invoice_document_fields(provider):
    invoice = provider.get_invoice("test-tenant", "INV-2025-001")

    assert invoice.total == 1295.98
    assert invoice.amount == 1295.98
    assert invoice.subtotal == 1199.98
    assert invoice.tax == 96.00
    assert invoice.date == "2025-01-15"
    assert invoice.due_date == "2025-02-15"
    assert len(invoice.items) == 2


def test_firestore_provider_maps_inventory_document_fields(provider):
    inventory = provider.get_inventory("test-tenant", "PROD-003")

    assert inventory.total_stock == 78
    assert inventory.quantity == 78
    assert inventory.warehouses == {"US-West": 30, "US-East": 28, "EU": 20}


def test_firestore_provider_search_results_keep_their_description(provider):
    products = provider.search_products("test-tenant", "laptop")

    assert products
    assert all(p.description for p in products)
