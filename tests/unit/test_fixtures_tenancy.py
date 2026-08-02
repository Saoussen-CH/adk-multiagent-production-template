"""fixtures.py must seed a real, named tenant — never an implicit/default
one (Global Constraints)."""


def test_get_sample_data_includes_a_real_tenant():
    from customer_support_mas.database.fixtures import get_sample_data

    data = get_sample_data()

    assert "tenants" in data
    assert len(data["tenants"]) >= 1
    tenant_id, tenant_doc = next(iter(data["tenants"].items()))
    assert tenant_doc["tenant_id"] == tenant_id
    assert tenant_doc["tier"] == "light"
    assert tenant_doc["provider_type"] == "firestore"
    assert "provider_config" in tenant_doc
    assert tenant_id != "default"
