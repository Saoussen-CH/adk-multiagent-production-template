"""Tests for backend.app.refund_approvals — the execute half of the HITL refund flow.

Task 8 made process_refund STAGE a PENDING_APPROVAL request into the
`refund_requests` Firestore collection instead of executing. This module
(Task 9) is the deterministic backend code that approves/rejects/expires
those staged requests and, only on approval, actually writes the refund
record to the `refunds` collection.

Note on fixtures/mock shape (corrections verified directly against this
repo's code, since the task-9 plan brief's Firestore-API assumptions were
wrong in the same ways Task 8's were):
- There is no repo-wide `mock_db` fixture usable here; `tests/mock_firestore.
  MockFirestoreClient()` is instantiated directly per test instead. It takes
  no arguments and self-seeds any collection not present in
  customer_support_mas's seed data (including `refund_requests` and
  `refunds`) as empty.
- `MockCollection` has no `.add()` method — only `.document(id).set(dict)`,
  `.document(id).get()`, `.document(id).delete()`, `.where(field, op, value)`
  (returning an object with only `.stream()`/`.get()`, no further chaining),
  and `.stream()` over the whole collection. The `_stage` helper below
  therefore generates its own document id and calls `.document(id).set(...)`
  rather than the brief's sketched `.add(...)`.
- All refund_approvals functions take `db` as an explicit first argument, so
  no patching of module-level client state is needed — each test just builds
  its own MockFirestoreClient and passes it straight in.
- expires_at values are chosen relative to the real current date (not the
  brief's illustrative 2026-07-17/07-20 dates, which are already in the past
  by the time this suite runs) so that "not yet expired" vs "stale" is
  unambiguous regardless of when the test executes: the "not stale" case
  uses a fixed far-future date, and the "stale" case uses a fixed
  well-in-the-past date.
"""

import uuid

import pytest

from backend.app.refund_approvals import (
    ApprovalError,
    approve_refund,
    expire_stale,
    list_pending,
    reject_refund,
)
from tests.mock_firestore import MockFirestoreClient

NOT_STALE_EXPIRES_AT = "2099-01-01T10:00:00+00:00"
STALE_EXPIRES_AT = "2020-01-01T10:00:00+00:00"


def _stage(
    db,
    order_id="ORD-12345",
    user_id="demo-user-001",
    expires_at=NOT_STALE_EXPIRES_AT,
):
    """Write a PENDING_APPROVAL refund_requests doc, mirroring what
    process_refund (Task 8) stages, and return its request_id.
    """
    request_id = f"REFREQ-{uuid.uuid4().hex[:8]}"
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
            "expires_at": expires_at,
        }
    )
    return request_id


def test_list_pending_returns_staged():
    db = MockFirestoreClient()
    rid = _stage(db)

    pending = list_pending(db)

    assert [p["request_id"] for p in pending] == [rid]
    assert pending[0]["order_id"] == "ORD-12345"
    assert pending[0]["status"] == "PENDING_APPROVAL"


def test_list_pending_excludes_non_pending():
    db = MockFirestoreClient()
    _stage(db)
    rid2 = _stage(db, order_id="ORD-99999")
    approve_refund(db, rid2, approver_id="approver-1")

    pending = list_pending(db)

    assert len(pending) == 1
    assert pending[0]["order_id"] == "ORD-12345"


def test_approve_executes_once():
    db = MockFirestoreClient()
    rid = _stage(db)

    result = approve_refund(db, rid, approver_id="approver-1")

    assert result["status"] == "approved"
    assert result["refund_id"]
    refunds = list(db.collection("refunds").stream())
    assert len(refunds) == 1

    refund_doc = refunds[0].to_dict()
    assert refund_doc["refund_id"] == result["refund_id"]
    assert refund_doc["order_id"] == "ORD-12345"
    assert refund_doc["customer_id"] == "demo-user-001"  # original requester, not approver
    assert refund_doc["reason"] == "damaged"
    assert refund_doc["reason_category"] == "product_defect"
    assert refund_doc["status"] == "pending"  # refund's own lifecycle status
    assert refund_doc["total_refund_amount"] == 49.99
    assert refund_doc["items"] == [{"item_id": "ITEM-1", "product_id": "ITEM-1", "price": 49.99}]
    assert "created_at" in refund_doc

    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "APPROVED"
    assert request_doc["approver_id"] == "approver-1"
    assert request_doc["refund_id"] == result["refund_id"]
    assert "approved_at" in request_doc

    # Second approval attempt: must be rejected as not_pending and must NOT
    # write a second refund record (this is the money-safety invariant).
    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, rid, approver_id="approver-1")
    assert exc.value.code == "not_pending"
    assert len(list(db.collection("refunds").stream())) == 1  # still exactly one


def test_approve_not_found():
    db = MockFirestoreClient()

    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, "REFREQ-does-not-exist", approver_id="approver-1")

    assert exc.value.code == "not_found"
    assert list(db.collection("refunds").stream()) == []


def test_self_approval_blocked():
    db = MockFirestoreClient()
    rid = _stage(db, user_id="approver-1")

    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, rid, approver_id="approver-1")

    assert exc.value.code == "self_approval"
    assert list(db.collection("refunds").stream()) == []
    # The request must remain untouched (still pending) after a blocked self-approval.
    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "PENDING_APPROVAL"


def test_reject_does_not_execute():
    db = MockFirestoreClient()
    rid = _stage(db)

    result = reject_refund(db, rid, "approver-1", note="no evidence")

    assert result["status"] == "rejected"
    assert list(db.collection("refunds").stream()) == []

    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "REJECTED"
    assert request_doc["approver_id"] == "approver-1"
    assert request_doc["rejection_note"] == "no evidence"


def test_reject_not_found():
    db = MockFirestoreClient()

    with pytest.raises(ApprovalError) as exc:
        reject_refund(db, "REFREQ-does-not-exist", "approver-1")

    assert exc.value.code == "not_found"


def test_reject_twice_is_not_pending():
    db = MockFirestoreClient()
    rid = _stage(db)
    reject_refund(db, rid, "approver-1")

    with pytest.raises(ApprovalError) as exc:
        reject_refund(db, rid, "approver-1")

    assert exc.value.code == "not_pending"


def test_expire_stale_flips_only_past_deadline():
    db = MockFirestoreClient()
    fresh_id = _stage(db)  # far-future expires_at — must remain pending
    stale_id = _stage(db, order_id="ORD-77777", expires_at=STALE_EXPIRES_AT)

    flipped = expire_stale(db)

    assert flipped == 1
    assert db.collection("refund_requests").document(stale_id).get().to_dict()["status"] == "EXPIRED"
    assert db.collection("refund_requests").document(fresh_id).get().to_dict()["status"] == "PENDING_APPROVAL"


def test_expire_stale_ignores_already_resolved_requests():
    db = MockFirestoreClient()
    rid = _stage(db, expires_at=STALE_EXPIRES_AT)
    approve_refund(db, rid, approver_id="approver-1")

    flipped = expire_stale(db)

    assert flipped == 0
    assert db.collection("refund_requests").document(rid).get().to_dict()["status"] == "APPROVED"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
