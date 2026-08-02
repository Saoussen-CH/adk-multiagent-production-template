"""FirestoreProvider must reproduce today's single-store Firestore logic
exactly, scoped by tenant_id via a per-tenant named database. These tests
seed two tenants' databases with overlapping order IDs to prove tenant
scoping works, not just that queries succeed."""
import pytest

from customer_support_mas.providers.firestore_provider import FirestoreProvider


@pytest.fixture
def two_tenant_dbs(mock_db_factory):
    """mock_db_factory: a fixture (added this task, see conftest.py) that
    returns a fresh MockFirestoreClient per call, keyed by name, so two
    tenants' data never share a collection dict."""
    db_a = mock_db_factory("tenant-a-db")
    db_b = mock_db_factory("tenant-b-db")

    db_a.collection("orders").document("ORD-1").set(
        {"customer_id": "user-1", "status": "Delivered", "total": 100.0, "items": []}
    )
    db_b.collection("orders").document("ORD-1").set(
        {"customer_id": "user-2", "status": "Processing", "total": 200.0, "items": []}
    )
    return db_a, db_b


def test_get_order_scoped_by_tenant_database(two_tenant_dbs, monkeypatch):
    db_a, db_b = two_tenant_dbs
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: {"tenant-a-db": db_a, "tenant-b-db": db_b}[database_id],
    )

    provider_a = FirestoreProvider({"database_id": "tenant-a-db"})
    provider_b = FirestoreProvider({"database_id": "tenant-b-db"})

    order_a = provider_a.get_order("tenant-a", "ORD-1")
    order_b = provider_b.get_order("tenant-b", "ORD-1")

    assert order_a.status == "Delivered"
    assert order_a.total == 100.0
    assert order_b.status == "Processing"
    assert order_b.total == 200.0


def test_get_order_not_found_returns_none(two_tenant_dbs, monkeypatch):
    db_a, _ = two_tenant_dbs
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: db_a,
    )
    provider = FirestoreProvider({"database_id": "tenant-a-db"})

    assert provider.get_order("tenant-a", "ORD-NONEXISTENT") is None


def test_verify_order_ownership_success(two_tenant_dbs, monkeypatch):
    db_a, _ = two_tenant_dbs
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: db_a,
    )
    provider = FirestoreProvider({"database_id": "tenant-a-db"})

    is_authorized, order, error = provider.verify_order_ownership("tenant-a", "ORD-1", "user-1")

    assert is_authorized is True
    assert order.order_id == "ORD-1"
    assert error == ""


def test_verify_order_ownership_wrong_customer(two_tenant_dbs, monkeypatch):
    db_a, _ = two_tenant_dbs
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: db_a,
    )
    provider = FirestoreProvider({"database_id": "tenant-a-db"})

    is_authorized, order, error = provider.verify_order_ownership("tenant-a", "ORD-1", "wrong-user")

    assert is_authorized is False
    assert order is None
    assert "permission" in error.lower()


def test_execute_refund_writes_refunds_collection(two_tenant_dbs, monkeypatch):
    db_a, _ = two_tenant_dbs
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: db_a,
    )
    provider = FirestoreProvider({"database_id": "tenant-a-db"})

    result = provider.execute_refund(
        "tenant-a", "ORD-1", "user-1", items=[{"product_id": "PROD-1", "name": "Widget", "price": 50.0, "qty": 1}], amount=50.0
    )

    assert result.success is True
    assert result.refund_id is not None
    refund_doc = db_a.collection("refunds").document(result.refund_id).get().to_dict()
    assert refund_doc["order_id"] == "ORD-1"
    assert refund_doc["total_refund_amount"] == 50.0


def test_search_products_keyword_fallback_scoped_by_tenant_database(two_tenant_dbs, monkeypatch):
    """search_products must never fall back to RAG when RAG isn't reachable
    in tests — this proves the keyword path alone is tenant-scoped, since
    the RAG path (embedding-based) is exercised only via a mocked
    get_rag_search in test_search_products_uses_rag_when_available below."""
    db_a, db_b = two_tenant_dbs
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: {"tenant-a-db": db_a, "tenant-b-db": db_b}[database_id],
    )
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_rag_search",
        lambda database_id: (_ for _ in ()).throw(Exception("RAG unavailable in this test")),
    )
    db_a.collection("products").document("PROD-A1").set(
        {"name": "Wireless Keyboard", "price": 49.99, "category": "accessories", "keywords": ["keyboard"]}
    )
    db_b.collection("products").document("PROD-B1").set(
        {"name": "Wireless Keyboard", "price": 999.99, "category": "accessories", "keywords": ["keyboard"]}
    )

    provider_a = FirestoreProvider({"database_id": "tenant-a-db"})
    results = provider_a.search_products("tenant-a", "keyboard")

    assert len(results) == 1
    assert results[0].product_id == "PROD-A1"
    assert results[0].price == 49.99


def test_search_products_uses_rag_when_available(two_tenant_dbs, monkeypatch):
    db_a, _ = two_tenant_dbs
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: db_a,
    )

    class FakeRag:
        def search(self, query, limit=5):
            return [{"id": "PROD-RAG-1", "name": "RAG Result", "price": 12.0, "category": "misc"}]

    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_rag_search", lambda database_id: FakeRag()
    )
    provider_a = FirestoreProvider({"database_id": "tenant-a-db"})

    results = provider_a.search_products("tenant-a", "anything")

    assert len(results) == 1
    assert results[0].product_id == "PROD-RAG-1"
