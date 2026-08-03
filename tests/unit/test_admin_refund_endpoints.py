"""Tests for the refund-approval FastAPI endpoints (Task 10).

These endpoints (`backend/app/main.py`) are the API-layer wiring around Task
9's `backend.app.refund_approvals` functions:

    GET  /api/admin/refunds/pending
    POST /api/admin/refunds/{request_id}/approve
    POST /api/admin/refunds/{request_id}/reject

All three are gated by `require_approver`, a FastAPI dependency that must:
  - raise 401 if the caller is unauthenticated (`get_current_user` returned
    None because no Authorization header was supplied at all — a malformed
    or invalid token is already rejected with 401 by `get_current_user`
    itself before `require_approver` ever runs);
  - raise 403 if the caller is authenticated but their Firestore user doc
    has no `role` field or a role other than `"approver"`, WITHOUT leaking
    any pending-request data in the response body.

Notes on fixtures/setup (this file is the first test module in the repo to
import `backend.app.main`, so a few things had to be established rather than
copied from an existing pattern):
  - `backend.app.config.Settings` requires `google_cloud_project` and
    `agent_engine_resource_name` with no defaults, and raises at import time
    if unset. `tests/conftest.py` already defaults `GOOGLE_CLOUD_PROJECT` to
    "test-project" via `pytest_configure` (runs before collection), but does
    NOT set `AGENT_ENGINE_RESOURCE_NAME` — this file sets a fallback for
    that (and belt-and-suspenders for the others) at module level, before
    `backend.app.main` is imported, using `setdefault` so a real `.env`-based
    value (if present) always wins.
  - `backend.app.main` builds a real (but connection-lazy) Firestore client
    on import via `db = get_database(...)`; no network call happens until a
    read/write is issued, so import is safe in CI with no live credentials.
    Every test below replaces `main.db.db` (the raw Firestore client
    attribute, per Task 9's contract) with a fresh
    `tests.mock_firestore.MockFirestoreClient()` via monkeypatch before
    touching any endpoint, so nothing here ever talks to real Firestore.
  - Role/identity checks go through FastAPI `dependency_overrides` for
    `get_current_user` (returns a fixed user_id, bypassing the real
    Authorization-header/token-verification path) plus a monkeypatch of
    `main.db.get_user` (the `Database.get_user` method) for the role lookup
    `require_approver` performs — mirroring the plan brief's suggested
    "override `require_approver` directly, or override `get_current_user`"
    pattern, choosing the latter since it also exercises the real
    `require_approver` role-check logic instead of bypassing it.
"""

import os

# Must run before `backend.app.main` (and therefore `backend.app.config`) is
# imported below — Settings() raises ValidationError at import time if these
# required-with-no-default fields are unset. setdefault() so a real .env
# value already loaded by tests/conftest.py's pytest_configure always wins.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault(
    "AGENT_ENGINE_RESOURCE_NAME",
    "projects/test-project/locations/us-central1/reasoningEngines/123",
)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import main as main_module  # noqa: E402
from backend.app.main import app, get_current_user, require_approver  # noqa: E402

client = TestClient(app)

# The tenant tests/unit/conftest.py's autouse `_seed_default_test_tenant`
# seeds, pointing at database_id "test-tenant-db". Every admin endpoint is
# tenant-scoped now (see the C4 note on the `mock_db` fixture below), so
# every request below must name it.
TENANT_ID = "test-tenant"
PENDING_URL = f"/api/admin/refunds/pending?tenant_id={TENANT_ID}"


def _approve_url(request_id: str, tenant_id: str = TENANT_ID) -> str:
    return f"/api/admin/refunds/{request_id}/approve?tenant_id={tenant_id}"


def _reject_url(request_id: str, tenant_id: str = TENANT_ID) -> str:
    return f"/api/admin/refunds/{request_id}/reject?tenant_id={tenant_id}"


def _stage_pending(db, order_id="ORD-12345", user_id="demo-user-001", tenant_id=TENANT_ID):
    """Write a PENDING_APPROVAL refund_requests doc directly into the mock,
    mirroring what Task 8's process_refund stages. Returns the request_id.

    Includes "tenant_id" (Task 6+7): approve_refund now reads
    request["tenant_id"] to resolve the tenant's CommerceProvider before
    executing the refund, so a staged doc without it raises KeyError before
    ever reaching the dual-control/idempotency logic under test here.
    """
    request_id = f"REFREQ-{order_id}"
    db.collection("refund_requests").document(request_id).set(
        {
            "tenant_id": tenant_id,
            "order_id": order_id,
            "user_id": user_id,
            "items": [{"item_id": "ITEM-1", "product_id": "ITEM-1", "price": 49.99}],
            "refund_amount": 49.99,
            "reason": "damaged",
            "reason_category": "product_defect",
            "status": "PENDING_APPROVAL",
            "requested_at": "2026-07-17T10:00:00+00:00",
            "expires_at": "2099-01-01T10:00:00+00:00",
        }
    )
    return request_id


@pytest.fixture(autouse=True)
def _reset_overrides():
    """Ensure dependency_overrides never leak between tests."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _wire_backend_db_to_mock(monkeypatch, mock_db):
    """Point `main.db.db` at the shared in-memory mock.

    There is deliberately no local `mock_db` fixture here any more — tests in
    this module use tests/unit/conftest.py's, because that is the client the
    admin endpoints genuinely reach now.

    C4: the endpoints used to read `main.db.db`, a single client hardcoded to
    `database_id="customer-support-db"`, while `process_refund` stages into
    `get_provider(tenant_id)._db` — the *tenant's own* database. The two
    coincided only for a tenant configured with that same database_id, so
    every other tenant's approval queue was silently empty and approving
    raised "not found". The endpoints now resolve the store per request via
    `resolve_refund_request_store(tenant_id)`, and conftest's autouse
    `mock_backends` already points `firestore_provider.get_db_client` and
    `tenancy.config.get_db_client` at `mock_db` (with
    `_seed_default_test_tenant` seeding "test-tenant" there) — so these tests
    now exercise the real resolution path instead of bypassing it.

    `main.db.db` is still redirected here as a belt-and-braces guard against
    any other code path reaching live Firestore during these tests.
    """
    monkeypatch.setattr(main_module.db, "db", mock_db)
    return mock_db


def _authenticate_as(user_id: str):
    app.dependency_overrides[get_current_user] = lambda: user_id


# =============================================================================
# 401 — unauthenticated
# =============================================================================


def test_pending_requires_auth_401():
    """No Authorization header at all → get_current_user returns None →
    require_approver must raise 401, not 403."""
    response = client.get(PENDING_URL)
    assert response.status_code == 401


def test_approve_requires_auth_401():
    response = client.post(_approve_url("REFREQ-whatever"))
    assert response.status_code == 401


def test_reject_requires_auth_401():
    response = client.post(_reject_url("REFREQ-whatever"), json={"note": "x"})
    assert response.status_code == 401


def test_require_approver_never_checks_role_when_unauthenticated(monkeypatch):
    """Direct unit check on ordering: 401-before-403. If require_approver
    consulted db.get_user before checking user_id, this would fail loudly
    instead of silently returning the wrong status code.
    """

    def _fail_if_called(user_id):
        pytest.fail("db.get_user must not be called when user_id is None")

    monkeypatch.setattr(main_module.db, "get_user", _fail_if_called)

    with pytest.raises(HTTPException) as exc_info:
        require_approver(user_id=None)

    assert exc_info.value.status_code == 401


# =============================================================================
# 403 — authenticated, not an approver
# =============================================================================


def test_pending_requires_approver_role_403(monkeypatch, mock_db):
    _stage_pending(mock_db)
    _authenticate_as("demo-user-001")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"user_id": uid, "role": "customer"})

    response = client.get(PENDING_URL)

    assert response.status_code == 403
    # No leakage of pending-request data in the 403 body.
    body = response.json()
    assert "requests" not in body


def test_pending_403_when_user_has_no_role_field(monkeypatch, mock_db):
    _authenticate_as("demo-user-001")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"user_id": uid})  # no "role" key

    response = client.get(PENDING_URL)

    assert response.status_code == 403


def test_pending_403_when_user_doc_missing(monkeypatch, mock_db):
    _authenticate_as("ghost-user")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: None)

    response = client.get(PENDING_URL)

    assert response.status_code == 403


def test_approve_requires_approver_role_403(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("demo-user-001")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "customer"})

    response = client.post(_approve_url(rid))

    assert response.status_code == 403
    # Refund must not have been executed.
    assert list(mock_db.collection("refunds").stream()) == []


# =============================================================================
# 200 happy path + 409 idempotency
# =============================================================================


def test_pending_returns_staged_requests(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.get(PENDING_URL)

    assert response.status_code == 200
    body = response.json()
    assert [r["request_id"] for r in body["requests"]] == [rid]
    assert body["requests"][0]["order_id"] == "ORD-12345"


def test_approve_happy_path_200(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post(_approve_url(rid))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["refund_id"]

    refunds = list(mock_db.collection("refunds").stream())
    assert len(refunds) == 1
    assert refunds[0].to_dict()["order_id"] == "ORD-12345"

    request_doc = mock_db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "APPROVED"
    assert request_doc["approver_id"] == "approver-1"


def test_approve_second_time_returns_409(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    first = client.post(_approve_url(rid))
    assert first.status_code == 200

    second = client.post(_approve_url(rid))
    assert second.status_code == 409

    # Still exactly one refund record — the money-safety invariant.
    assert len(list(mock_db.collection("refunds").stream())) == 1


def test_approve_not_found_returns_404(monkeypatch, mock_db):
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post(_approve_url("REFREQ-does-not-exist"))

    assert response.status_code == 404


def test_approve_self_approval_returns_403(monkeypatch, mock_db):
    rid = _stage_pending(mock_db, user_id="approver-1")
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post(_approve_url(rid))

    assert response.status_code == 403
    assert list(mock_db.collection("refunds").stream()) == []


def test_reject_happy_path_200(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post(_reject_url(rid), json={"note": "no evidence"})

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    request_doc = mock_db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "REJECTED"
    assert request_doc["rejection_note"] == "no evidence"
    assert list(mock_db.collection("refunds").stream()) == []


def test_reject_twice_returns_409(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    first = client.post(_reject_url(rid), json={})
    assert first.status_code == 200

    second = client.post(_reject_url(rid), json={})
    assert second.status_code == 409


# =============================================================================
# 500 — unexpected (non-ApprovalError) exceptions must be caught, logged, and
# returned as a clean JSON error body, not leaked as an unhandled server
# error / stack trace. Added after code review flagged that the mutating
# endpoints (approve/reject) — and, for consistency with the rest of
# main.py, the GET too — had no generic `except Exception` fallback, unlike
# every other endpoint in this file.
# =============================================================================


def test_pending_unexpected_error_returns_500(monkeypatch, mock_db):
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    def _boom(db, tenant_id):
        raise RuntimeError("firestore hiccup")

    monkeypatch.setattr(main_module.refund_approvals, "list_pending", _boom)

    response = client.get(PENDING_URL)

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to list pending refunds"}
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_approve_unexpected_error_returns_500(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    def _boom(db, tenant_id, request_id, approver_id):
        raise RuntimeError("firestore hiccup")

    monkeypatch.setattr(main_module.refund_approvals, "approve_refund", _boom)

    response = client.post(_approve_url(rid))

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to approve refund"}
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_reject_unexpected_error_returns_500(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    def _boom(db, tenant_id, request_id, approver_id, note=""):
        raise RuntimeError("firestore hiccup")

    monkeypatch.setattr(main_module.refund_approvals, "reject_refund", _boom)

    response = client.post(_reject_url(rid), json={})

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to reject refund"}
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


# =============================================================================
# C4 — the refund_requests split brain
#
# process_refund stages into get_provider(tenant_id)._db (the tenant's OWN
# Firestore database); the admin API used to read a single hardcoded
# database_id="customer-support-db" handle. For the one seeded tenant those
# coincide, which is exactly why nothing caught it. These tests use a SECOND
# tenant whose database_id is different, so the coincidence is gone.
# =============================================================================


@pytest.fixture
def second_tenant(mock_db, mock_db_factory, monkeypatch):
    """A second tenant in the same pool with its own, different database.

    Returns that tenant's Firestore client. Its refund_requests are
    physically elsewhere than "test-tenant"'s, so any endpoint still reading
    a fixed database can't see them.
    """
    from customer_support_mas.tenancy import config as config_module

    other_db = mock_db_factory("other-tenant-db")

    # The control-plane database holds tenant routing config for both.
    mock_db.collection("tenants").document("other-tenant").set(
        {
            "tenant_id": "other-tenant",
            "tier": "light",
            "provider_type": "firestore",
            "provider_config": {"database_id": "other-tenant-db"},
            "pool_id": "test-pool",
            "refund_policy_ref": "other-tenant",
        }
    )
    config_module.invalidate_tenant_config_cache()

    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: {"test-tenant-db": mock_db, "other-tenant-db": other_db}[database_id],
    )
    yield other_db
    config_module.invalidate_tenant_config_cache()


def _as_approver(monkeypatch, user_id="approver-1"):
    _authenticate_as(user_id)
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})


def test_pending_requires_a_tenant_id(monkeypatch, mock_db):
    """No default tenant: the query parameter is required, not optional."""
    _as_approver(monkeypatch)

    response = client.get("/api/admin/refunds/pending")

    assert response.status_code == 422


def test_second_tenants_staged_request_is_visible_on_its_own_queue(monkeypatch, mock_db, second_tenant):
    """The regression: a tenant whose database_id differs from the hardcoded
    one used to have a permanently empty approval queue."""
    rid = _stage_pending(second_tenant, order_id="ORD-77777", user_id="shopper-b", tenant_id="other-tenant")
    _as_approver(monkeypatch)

    response = client.get("/api/admin/refunds/pending?tenant_id=other-tenant")

    assert response.status_code == 200
    assert [r["request_id"] for r in response.json()["requests"]] == [rid]


def test_one_tenants_queue_never_shows_another_tenants_requests(monkeypatch, mock_db, second_tenant):
    _stage_pending(mock_db)  # tenant_id="test-tenant", in test-tenant-db
    _stage_pending(second_tenant, order_id="ORD-77777", user_id="shopper-b", tenant_id="other-tenant")
    _as_approver(monkeypatch)

    ours = client.get(PENDING_URL).json()["requests"]
    theirs = client.get("/api/admin/refunds/pending?tenant_id=other-tenant").json()["requests"]

    assert {r["request_id"] for r in ours}.isdisjoint({r["request_id"] for r in theirs})
    assert all(r["order_id"] != "ORD-77777" for r in ours)


def test_approving_another_tenants_request_id_is_404_not_a_cross_tenant_write(monkeypatch, mock_db, second_tenant):
    """Knowing a request_id must not be enough to act on it from the wrong
    tenant — and the response must not confirm the id exists elsewhere."""
    other_rid = _stage_pending(second_tenant, order_id="ORD-77777", user_id="shopper-b", tenant_id="other-tenant")
    _as_approver(monkeypatch)

    response = client.post(_approve_url(other_rid))  # TENANT_ID, not other-tenant

    assert response.status_code == 404
    assert list(second_tenant.collection("refunds").stream()) == []
    assert (
        second_tenant.collection("refund_requests").document(other_rid).get().to_dict()["status"] == "PENDING_APPROVAL"
    )


def test_approving_through_the_owning_tenant_executes_the_refund(monkeypatch, mock_db, second_tenant):
    other_rid = _stage_pending(second_tenant, order_id="ORD-77777", user_id="shopper-b", tenant_id="other-tenant")
    _as_approver(monkeypatch)

    response = client.post(_approve_url(other_rid, tenant_id="other-tenant"))

    assert response.status_code == 200, response.text
    # The refund landed in the SECOND tenant's database, not the default one.
    refunds = list(second_tenant.collection("refunds").stream())
    assert len(refunds) == 1
    assert refunds[0].to_dict()["order_id"] == "ORD-77777"
    assert list(mock_db.collection("refunds").stream()) == []


def test_rejecting_another_tenants_request_id_is_404(monkeypatch, mock_db, second_tenant):
    other_rid = _stage_pending(second_tenant, order_id="ORD-77777", user_id="shopper-b", tenant_id="other-tenant")
    _as_approver(monkeypatch)

    response = client.post(_reject_url(other_rid), json={"note": "nope"})

    assert response.status_code == 404
    assert (
        second_tenant.collection("refund_requests").document(other_rid).get().to_dict()["status"] == "PENDING_APPROVAL"
    )


def test_unknown_tenant_is_404_not_an_empty_queue(monkeypatch, mock_db):
    """An unrecognized tenant_id is a hard error, never a silent fallback."""
    _as_approver(monkeypatch)

    response = client.get("/api/admin/refunds/pending?tenant_id=no-such-tenant")

    assert response.status_code == 404
    assert "no-such-tenant" in response.json()["detail"]


def test_provider_without_a_refund_store_is_501_not_a_500(monkeypatch, mock_db):
    """A Shopify-backed tenant has no `_db`. That must surface as an explicit
    'not supported', not an AttributeError-turned-500 (finding I3's
    API-side counterpart)."""
    from customer_support_mas.tenancy import config as config_module

    mock_db.collection("tenants").document("shopify-tenant").set(
        {
            "tenant_id": "shopify-tenant",
            "tier": "light",
            "provider_type": "shopify",
            "provider_config": {"shop_domain": "mock.myshopify.com"},
            "pool_id": "test-pool",
        }
    )
    config_module.invalidate_tenant_config_cache()
    _as_approver(monkeypatch)

    response = client.get("/api/admin/refunds/pending?tenant_id=shopify-tenant")

    config_module.invalidate_tenant_config_cache()
    assert response.status_code == 501


def test_approver_bound_to_another_tenant_is_403(monkeypatch, mock_db):
    """When a user doc DOES carry a tenant_id, it is enforced. (Users have no
    such field today — see require_approver_for_tenant's note.)"""
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver", "tenant_id": "other-tenant"})

    response = client.get(PENDING_URL)

    assert response.status_code == 403


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
