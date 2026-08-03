"""get_provider must dispatch to the correct provider class based on the
tenant's provider_type, and must re-resolve (not serve a stale cached
provider construction) after invalidate_tenant_config_cache."""
import pytest


def test_get_provider_dispatches_firestore(mock_db, monkeypatch):
    from customer_support_mas.providers.firestore_provider import FirestoreProvider
    from customer_support_mas.providers.registry import get_provider
    from customer_support_mas.tenancy import config as config_module

    config_module.invalidate_tenant_config_cache()
    monkeypatch.setattr("customer_support_mas.tenancy.config.get_db_client", lambda *_: mock_db)
    mock_db.collection("tenants").document("tenant-fs").set(
        {
            "tenant_id": "tenant-fs",
            "tier": "light",
            "provider_type": "firestore",
            "provider_config": {"database_id": "tenant-fs-db"},
        }
    )

    provider = get_provider("tenant-fs")

    assert isinstance(provider, FirestoreProvider)
    assert provider._database_id == "tenant-fs-db"


def test_get_provider_dispatches_shopify(mock_db, monkeypatch):
    from customer_support_mas.providers.registry import get_provider
    from customer_support_mas.providers.shopify_provider import ShopifyProvider
    from customer_support_mas.tenancy import config as config_module

    config_module.invalidate_tenant_config_cache()
    monkeypatch.setattr("customer_support_mas.tenancy.config.get_db_client", lambda *_: mock_db)
    mock_db.collection("tenants").document("tenant-shop").set(
        {
            "tenant_id": "tenant-shop",
            "tier": "light",
            "provider_type": "shopify",
            "provider_config": {"shop_domain": "tenant-shop.myshopify.com"},
        }
    )

    provider = get_provider("tenant-shop")

    assert isinstance(provider, ShopifyProvider)


def test_get_provider_unknown_provider_type_raises(mock_db, monkeypatch):
    from customer_support_mas.providers.registry import get_provider
    from customer_support_mas.tenancy import config as config_module

    config_module.invalidate_tenant_config_cache()
    monkeypatch.setattr("customer_support_mas.tenancy.config.get_db_client", lambda *_: mock_db)
    mock_db.collection("tenants").document("tenant-bad").set(
        {"tenant_id": "tenant-bad", "tier": "light", "provider_type": "bigcommerce", "provider_config": {}}
    )

    with pytest.raises(ValueError):
        get_provider("tenant-bad")


def test_get_provider_reflects_config_after_cache_invalidation(mock_db, monkeypatch):
    from customer_support_mas.providers.firestore_provider import FirestoreProvider
    from customer_support_mas.providers.registry import get_provider
    from customer_support_mas.providers.shopify_provider import ShopifyProvider
    from customer_support_mas.tenancy.config import invalidate_tenant_config_cache

    monkeypatch.setattr("customer_support_mas.tenancy.config.get_db_client", lambda *_: mock_db)
    mock_db.collection("tenants").document("tenant-x").set(
        {
            "tenant_id": "tenant-x",
            "tier": "light",
            "provider_type": "firestore",
            "provider_config": {"database_id": "tenant-x-db"},
        }
    )
    invalidate_tenant_config_cache("tenant-x")
    assert isinstance(get_provider("tenant-x"), FirestoreProvider)

    # Simulate an operator migrating tenant-x to Shopify: update the doc,
    # then invalidate — without invalidation, the stale cached config
    # would still dispatch to FirestoreProvider.
    mock_db.collection("tenants").document("tenant-x").set(
        {
            "tenant_id": "tenant-x",
            "tier": "light",
            "provider_type": "shopify",
            "provider_config": {"shop_domain": "tenant-x.myshopify.com"},
        }
    )
    invalidate_tenant_config_cache("tenant-x")

    assert isinstance(get_provider("tenant-x"), ShopifyProvider)
