"""verify_order_owner proves order-number + email ownership without
requiring the caller to be logged in as the order's actual customer_id."""
import pytest

from customer_support_mas.providers.firestore_provider import FirestoreProvider


@pytest.fixture
def patched_db(mock_db_factory, monkeypatch):
    """The single mock Firestore instance that FirestoreProvider._db resolves
    to for the rest of this module's fixtures/tests — patched once here so
    that any test needing to write extra documents (e.g. a second order) can
    do so into the *same* store the provider actually reads from, instead of
    creating a disconnected instance that get_db_client never returns."""
    db = mock_db_factory("verify-owner-db")
    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: db,
    )
    return db


@pytest.fixture
def provider_with_order_and_user(patched_db):
    patched_db.collection("orders").document("ORD-90001").set(
        {"customer_id": "cust-1", "status": "Delivered", "items": []}
    )
    patched_db.collection("users").document("cust-1").set(
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


def test_order_with_no_matching_user_account_does_not_verify(provider_with_order_and_user, patched_db):
    """An order whose customer_id has no account document (e.g. data
    inconsistency) must fail closed, not raise. ORD-90002 is written into
    patched_db — the same mock Firestore instance provider_with_order_and_user's
    FirestoreProvider._db already resolves to (both fixtures depend on the
    same patched_db, so pytest hands them the identical instance) — so this
    genuinely exercises the "order exists but its customer has no user
    account" branch, rather than merely re-triggering "order not found"."""
    patched_db.collection("orders").document("ORD-90002").set(
        {"customer_id": "ghost-user", "status": "Pending", "items": []}
    )
    assert provider_with_order_and_user.verify_order_owner("test-tenant", "ORD-90002", "anyone@example.com") is False
