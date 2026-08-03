import pytest

from customer_support_mas.providers.shopify_provider import ShopifyProvider


def test_get_order_returns_mock_data(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    order = provider.get_order("tenant-shopify", "SHOPIFY-ORD-1001")

    assert order.customer_id == "shopify-customer-1"
    assert order.total == 29.99


def test_get_order_unknown_id_returns_none(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    assert provider.get_order("tenant-shopify", "NONEXISTENT") is None


def test_real_mode_raises_not_implemented(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "false")

    with pytest.raises(NotImplementedError):
        ShopifyProvider({"shop_domain": "test.myshopify.com"})


def test_execute_refund_mock_mode_succeeds(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    result = provider.execute_refund("tenant-shopify", "SHOPIFY-ORD-1001", "shopify-customer-1", [], 29.99)

    assert result.success is True
    assert result.refund_id.startswith("shopify-mock-refund-")


def test_execute_refund_accepts_reason_kwargs(monkeypatch):
    """backend/app/refund_approvals.py's approve_refund always passes
    reason/reason_category as kwargs regardless of provider — this must not
    raise TypeError for a Shopify-backed tenant."""
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    result = provider.execute_refund(
        "tenant-shopify",
        "SHOPIFY-ORD-1001",
        "shopify-customer-1",
        [],
        29.99,
        reason="Item arrived damaged",
        reason_category="damaged",
    )

    assert result.success is True


def test_list_orders_for_customer_filters_by_customer_id(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    orders = provider.list_orders_for_customer("tenant-shopify", "shopify-customer-1")
    assert len(orders) == 1
    assert orders[0].order_id == "SHOPIFY-ORD-1001"

    assert provider.list_orders_for_customer("tenant-shopify", "nonexistent-customer") == []


def test_verify_order_ownership_rejects_wrong_customer(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    ok, order, reason = provider.verify_order_ownership("tenant-shopify", "SHOPIFY-ORD-1001", "someone-else")

    assert ok is False
    assert order is None
    assert reason


def test_get_reviews_for_product_returns_empty_list(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    assert provider.get_reviews_for_product("tenant-shopify", "SHOPIFY-PROD-1") == []


def test_verify_order_owner_matches_mock_email(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    from customer_support_mas.providers.shopify_provider import ShopifyProvider

    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    assert provider.verify_order_owner("tenant-shopify", "SHOPIFY-ORD-1001", "shopify-customer-1@example.com") is True


def test_verify_order_owner_rejects_wrong_email(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    from customer_support_mas.providers.shopify_provider import ShopifyProvider

    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    assert provider.verify_order_owner("tenant-shopify", "SHOPIFY-ORD-1001", "someone-else@example.com") is False


def test_verify_order_owner_unknown_order_returns_false(monkeypatch):
    monkeypatch.setenv("SHOPIFY_MOCK", "true")
    from customer_support_mas.providers.shopify_provider import ShopifyProvider

    provider = ShopifyProvider({"shop_domain": "test.myshopify.com"})

    assert provider.verify_order_owner("tenant-shopify", "SHOPIFY-ORD-9999", "anyone@example.com") is False
