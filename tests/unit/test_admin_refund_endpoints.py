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
from tests.mock_firestore import MockFirestoreClient  # noqa: E402

client = TestClient(app)


def _stage_pending(db, order_id="ORD-12345", user_id="demo-user-001"):
    """Write a PENDING_APPROVAL refund_requests doc directly into the mock,
    mirroring what Task 8's process_refund stages. Returns the request_id.
    """
    request_id = f"REFREQ-{order_id}"
    db.collection("refund_requests").document(request_id).set(
        {
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


@pytest.fixture
def mock_db(monkeypatch):
    """Swap main.db.db (the raw Firestore client) for an isolated in-memory mock."""
    fake = MockFirestoreClient()
    monkeypatch.setattr(main_module.db, "db", fake)
    return fake


def _authenticate_as(user_id: str):
    app.dependency_overrides[get_current_user] = lambda: user_id


# =============================================================================
# 401 — unauthenticated
# =============================================================================


def test_pending_requires_auth_401():
    """No Authorization header at all → get_current_user returns None →
    require_approver must raise 401, not 403."""
    response = client.get("/api/admin/refunds/pending")
    assert response.status_code == 401


def test_approve_requires_auth_401():
    response = client.post("/api/admin/refunds/REFREQ-whatever/approve")
    assert response.status_code == 401


def test_reject_requires_auth_401():
    response = client.post("/api/admin/refunds/REFREQ-whatever/reject", json={"note": "x"})
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

    response = client.get("/api/admin/refunds/pending")

    assert response.status_code == 403
    # No leakage of pending-request data in the 403 body.
    body = response.json()
    assert "requests" not in body


def test_pending_403_when_user_has_no_role_field(monkeypatch, mock_db):
    _authenticate_as("demo-user-001")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"user_id": uid})  # no "role" key

    response = client.get("/api/admin/refunds/pending")

    assert response.status_code == 403


def test_pending_403_when_user_doc_missing(monkeypatch, mock_db):
    _authenticate_as("ghost-user")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: None)

    response = client.get("/api/admin/refunds/pending")

    assert response.status_code == 403


def test_approve_requires_approver_role_403(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("demo-user-001")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "customer"})

    response = client.post(f"/api/admin/refunds/{rid}/approve")

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

    response = client.get("/api/admin/refunds/pending")

    assert response.status_code == 200
    body = response.json()
    assert [r["request_id"] for r in body["requests"]] == [rid]
    assert body["requests"][0]["order_id"] == "ORD-12345"


def test_approve_happy_path_200(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post(f"/api/admin/refunds/{rid}/approve")

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

    first = client.post(f"/api/admin/refunds/{rid}/approve")
    assert first.status_code == 200

    second = client.post(f"/api/admin/refunds/{rid}/approve")
    assert second.status_code == 409

    # Still exactly one refund record — the money-safety invariant.
    assert len(list(mock_db.collection("refunds").stream())) == 1


def test_approve_not_found_returns_404(monkeypatch, mock_db):
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post("/api/admin/refunds/REFREQ-does-not-exist/approve")

    assert response.status_code == 404


def test_approve_self_approval_returns_403(monkeypatch, mock_db):
    rid = _stage_pending(mock_db, user_id="approver-1")
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post(f"/api/admin/refunds/{rid}/approve")

    assert response.status_code == 403
    assert list(mock_db.collection("refunds").stream()) == []


def test_reject_happy_path_200(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    response = client.post(f"/api/admin/refunds/{rid}/reject", json={"note": "no evidence"})

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

    first = client.post(f"/api/admin/refunds/{rid}/reject", json={})
    assert first.status_code == 200

    second = client.post(f"/api/admin/refunds/{rid}/reject", json={})
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

    def _boom(db):
        raise RuntimeError("firestore hiccup")

    monkeypatch.setattr(main_module.refund_approvals, "list_pending", _boom)

    response = client.get("/api/admin/refunds/pending")

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to list pending refunds"}
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_approve_unexpected_error_returns_500(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    def _boom(db, request_id, approver_id):
        raise RuntimeError("firestore hiccup")

    monkeypatch.setattr(main_module.refund_approvals, "approve_refund", _boom)

    response = client.post(f"/api/admin/refunds/{rid}/approve")

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to approve refund"}
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_reject_unexpected_error_returns_500(monkeypatch, mock_db):
    rid = _stage_pending(mock_db)
    _authenticate_as("approver-1")
    monkeypatch.setattr(main_module.db, "get_user", lambda uid: {"role": "approver"})

    def _boom(db, request_id, approver_id, note=""):
        raise RuntimeError("firestore hiccup")

    monkeypatch.setattr(main_module.refund_approvals, "reject_refund", _boom)

    response = client.post(f"/api/admin/refunds/{rid}/reject", json={})

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to reject refund"}
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
