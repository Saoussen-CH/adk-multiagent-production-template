"""verify_order_owner proves order-number + email ownership without
requiring the caller to be logged in as the order's actual customer_id."""
import pytest

from customer_support_mas.providers.firestore_provider import FirestoreProvider


@pytest.fixture
def provider_with_order_and_user(mock_db_factory, monkeypatch):
    db = mock_db_factory("verify-owner-db")
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: db,
    )
    db.collection("orders").document("ORD-90001").set(
        {"customer_id": "cust-1", "status": "Delivered", "items": []}
    )
    db.collection("users").document("cust-1").set(
        {"user_id": "cust-1", "email": "alice@example.com", "tenant_id": "test-tenant"}
    )
    return FirestoreProvider({"database_id": "verify-owner-db"})


def test_matching_order_and_email_verifies(provider_with_order_and_user):
    assert provider_with_order_and_user.verify_order_owner("test-tenant", "ORD-90001", "alice@example.com") is True


def test_matching_order_and_email_is_case_insensitive(provider_with_order_and_user):
    assert provider_with_order_and_user.verify_order_owner("test-tenant", "ORD-90001", "ALICE@EXAMPLE.COM") is True


def test_wrong_email_does_not_verify(provider_with_order_and_user):
    assert provider_with_order_and_user.verify_order_owner("test-tenant", "ORD-90001", "bob@example.com") is False


def test_nonexistent_order_does_not_verify(provider_with_order_and_user):
    assert provider_with_order_and_user.verify_order_owner("test-tenant", "ORD-99999", "alice@example.com") is False


def test_order_with_no_matching_user_account_does_not_verify(provider_with_order_and_user, mock_db_factory):
    """An order whose customer_id has no account document (e.g. data
    inconsistency) must fail closed, not raise."""
    db = mock_db_factory("verify-owner-db")
    db.collection("orders").document("ORD-90002").set({"customer_id": "ghost-user", "status": "Pending", "items": []})
    provider = FirestoreProvider({"database_id": "verify-owner-db"})
    assert provider.verify_order_owner("test-tenant", "ORD-90002", "anyone@example.com") is False
