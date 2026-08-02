"""Tenant config is loaded from the `tenants` Firestore collection — no
default/fallback tenant exists. An unknown tenant_id is a hard error."""
import pytest


@pytest.fixture(autouse=True)
def _reset_tenant_cache():
    from customer_support_mas.tenancy import config as config_module

    config_module._tenant_config_cache.clear()
    yield
    config_module._tenant_config_cache.clear()


def _seed_tenant(mock_db, tenant_id, **overrides):
    doc = {
        "tenant_id": tenant_id,
        "tier": "light",
        "provider_type": "firestore",
        "provider_config": {"database_id": f"{tenant_id}-db"},
        "pool_id": "light-pool-1",
        "refund_policy_ref": tenant_id,
    }
    doc.update(overrides)
    mock_db.collection("tenants").document(tenant_id).set(doc)


def test_load_tenant_config_success(mock_db, monkeypatch):
    from customer_support_mas.tenancy.config import load_tenant_config

    monkeypatch.setattr("customer_support_mas.tenancy.config.get_db_client", lambda *_: mock_db)
    _seed_tenant(mock_db, "acme-electronics")

    config = load_tenant_config("acme-electronics")

    assert config.tenant_id == "acme-electronics"
    assert config.provider_type == "firestore"
    assert config.provider_config == {"database_id": "acme-electronics-db"}


def test_load_tenant_config_unknown_tenant_raises(mock_db, monkeypatch):
    from customer_support_mas.tenancy.config import TenantNotFoundError, load_tenant_config

    monkeypatch.setattr("customer_support_mas.tenancy.config.get_db_client", lambda *_: mock_db)

    with pytest.raises(TenantNotFoundError):
        load_tenant_config("nonexistent-tenant")


def test_load_tenant_config_is_cached(mock_db, monkeypatch):
    from customer_support_mas.tenancy import config as config_module

    monkeypatch.setattr("customer_support_mas.tenancy.config.get_db_client", lambda *_: mock_db)
    _seed_tenant(mock_db, "acme-electronics")

    first = config_module.load_tenant_config("acme-electronics")
    # Mutate the underlying doc directly — cached call must NOT see the change
    mock_db.collection("tenants").document("acme-electronics").set({"tier": "heavy"})
    second = config_module.load_tenant_config("acme-electronics")

    assert first is second
    assert second.tier == "light"
