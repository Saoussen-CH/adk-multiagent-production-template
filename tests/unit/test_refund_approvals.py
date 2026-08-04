"""Tests for backend.app.refund_approvals — the execute half of the HITL refund flow.

Task 8 made process_refund STAGE a PENDING_APPROVAL request into the
`refund_requests` Firestore collection instead of executing. This module
(Task 9) is the deterministic backend code that approves/rejects/expires
those staged requests and, only on approval, actually writes the refund
record to the `refunds` collection.

Note on fixtures/mock shape (corrections verified directly against this
repo's code, since the task-9 plan brief's Firestore-API assumptions were
wrong in the same ways Task 8's were):
- `MockCollection` has no `.add()` method — only `.document(id).set(dict)`,
  `.document(id).get()`, `.document(id).delete()`, `.where(field, op, value)`
  (returning an object with only `.stream()`/`.get()`, no further chaining),
  and `.stream()` over the whole collection. The `_stage` helper below
  therefore generates its own document id and calls `.document(id).set(...)`
  rather than the brief's sketched `.add(...)`.
- expires_at values are chosen relative to the real current date (not the
  brief's illustrative 2026-07-17/07-20 dates, which are already in the past
  by the time this suite runs) so that "not yet expired" vs "stale" is
  unambiguous regardless of when the test executes: the "not stale" case
  uses a fixed far-future date, and the "stale" case uses a fixed
  well-in-the-past date.

Note on fixtures/mock shape, UPDATED for Task 7 (multi-tenant provider
architecture): this file originally built its own private
`MockFirestoreClient()` per test and passed it straight into
`approve_refund(db, ...)`, since every `refund_approvals` function took
`db` as an explicit first argument with no other Firestore access. That
stopped being sufficient once Task 7 changed `approve_refund` to execute
refunds via `get_provider(tenant_id).execute_refund(...)` instead of
writing to `db.collection("refunds")` directly — `get_provider` resolves
its Firestore handle through `get_db_client`, which is patched by
`tests/unit/conftest.py`'s autouse `mock_backends` fixture to a *shared*
`MockFirestoreClient` instance (the `mock_db` fixture), not whatever
private instance a test constructs for itself. Passing a private instance
as `db` therefore silently desynced: `refund_requests` reads/writes went
to the private instance, but the `refunds` write (via the provider) went
to the shared one, so assertions against the private `db.collection(
"refunds")` saw nothing.

This file now follows the same pattern already established by
`tests/unit/test_refund_staging.py` for exactly this reason: it never
constructs its own `MockFirestoreClient()`. Instead, `_active_db_client()`
returns `get_provider("test-tenant")._db` — the actual instance the
autouse `mock_backends`/`_seed_default_test_tenant` fixtures (tests/unit/
conftest.py) have wired up for this test — and every helper/test in this
file reads and writes through that same instance, whether the access is
"direct" (`_stage`, assertions) or indirect (via `approve_refund`'s
provider call). `_seed_default_test_tenant` (autouse) seeds a `"tenants/
test-tenant"` config pointing at this same mock, so `get_provider(
"test-tenant")` resolves without error. `_stage()` accordingly now writes
`"tenant_id": "test-tenant"` onto every staged doc, matching what the real
`process_refund` (Task 6) stages and what `approve_refund` (Task 7) now
requires (`request["tenant_id"]`).
"""

import uuid

import pytest

from backend.app.refund_approvals import (
    APPROVING,
    ApprovalError,
    approve_refund,
    expire_stale,
    find_stuck_approving,
    list_pending,
    reject_refund,
)

NOT_STALE_EXPIRES_AT = "2099-01-01T10:00:00+00:00"
STALE_EXPIRES_AT = "2020-01-01T10:00:00+00:00"

TEST_TENANT_ID = "test-tenant"


def _active_db_client():
    """Return the Firestore mock actually used by get_provider("test-tenant").

    Must be used for every read/write in this file (not a private
    MockFirestoreClient()) so that direct `db.collection(...)` access here
    and `approve_refund`'s internal `get_provider(tenant_id)._db` access
    (Task 7) always agree on the same instance. See module docstring.
    """
    from customer_support_mas.providers.registry import get_provider

    return get_provider(TEST_TENANT_ID)._db


def _stage(
    db,
    order_id="ORD-12345",
    user_id="demo-user-001",
    expires_at=NOT_STALE_EXPIRES_AT,
    tenant_id=TEST_TENANT_ID,
):
    """Write a PENDING_APPROVAL refund_requests doc, mirroring what
    process_refund (Task 8, tenant-scoped since Task 6) stages, and return
    its request_id.
    """
    request_id = f"REFREQ-{uuid.uuid4().hex[:8]}"
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
            "expires_at": expires_at,
        }
    )
    return request_id


def test_list_pending_ignores_another_tenants_requests_in_the_same_database():
    """Defence in depth for finding C4. The primary guard is that each tenant's
    refund_requests live in that tenant's own Firestore database; this is the
    guard that still holds if two tenants are ever mis-configured onto one
    database (which is exactly what finding I2's uniqueness check exists to
    prevent).
    """
    db = _active_db_client()
    ours = _stage(db)
    theirs = _stage(db, order_id="ORD-77777", tenant_id="other-tenant")

    pending = list_pending(db, TEST_TENANT_ID)

    request_ids = [p["request_id"] for p in pending]
    assert ours in request_ids
    assert theirs not in request_ids


def test_approve_refuses_a_request_belonging_to_another_tenant():
    """Reported as not_found, not a distinct error: an approver acting for one
    tenant must not be able to probe whether a request_id exists elsewhere."""
    db = _active_db_client()
    rid = _stage(db, tenant_id="other-tenant")

    with pytest.raises(ApprovalError) as exc_info:
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")

    assert exc_info.value.code == "not_found"
    assert list(db.collection("refunds").stream()) == []
    assert db.collection("refund_requests").document(rid).get().to_dict()["status"] == "PENDING_APPROVAL"


def test_reject_refuses_a_request_belonging_to_another_tenant():
    db = _active_db_client()
    rid = _stage(db, tenant_id="other-tenant")

    with pytest.raises(ApprovalError) as exc_info:
        reject_refund(db, TEST_TENANT_ID, rid, "approver-1")

    assert exc_info.value.code == "not_found"
    assert db.collection("refund_requests").document(rid).get().to_dict()["status"] == "PENDING_APPROVAL"


def test_expire_stale_leaves_another_tenants_requests_alone():
    db = _active_db_client()
    ours = _stage(db, expires_at=STALE_EXPIRES_AT)
    theirs = _stage(db, order_id="ORD-77777", expires_at=STALE_EXPIRES_AT, tenant_id="other-tenant")

    flipped = expire_stale(db, TEST_TENANT_ID)

    assert flipped == 1
    assert db.collection("refund_requests").document(ours).get().to_dict()["status"] == "EXPIRED"
    assert db.collection("refund_requests").document(theirs).get().to_dict()["status"] == "PENDING_APPROVAL"


def test_list_pending_returns_staged():
    db = _active_db_client()
    rid = _stage(db)

    pending = list_pending(db, TEST_TENANT_ID)

    assert [p["request_id"] for p in pending] == [rid]
    assert pending[0]["order_id"] == "ORD-12345"
    assert pending[0]["status"] == "PENDING_APPROVAL"


def test_list_pending_excludes_non_pending():
    db = _active_db_client()
    _stage(db)
    rid2 = _stage(db, order_id="ORD-99999")
    approve_refund(db, TEST_TENANT_ID, rid2, approver_id="approver-1")

    pending = list_pending(db, TEST_TENANT_ID)

    assert len(pending) == 1
    assert pending[0]["order_id"] == "ORD-12345"


def test_approve_executes_once():
    db = _active_db_client()
    rid = _stage(db)

    result = approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")

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
    # Per-item refund_amount is computed and added (mirrors pre-HITL process_refund).
    assert refund_doc["items"] == [
        {"item_id": "ITEM-1", "product_id": "ITEM-1", "price": 49.99, "refund_amount": 49.99}
    ]
    assert "created_at" in refund_doc

    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "APPROVED"
    assert request_doc["approver_id"] == "approver-1"
    assert request_doc["refund_id"] == result["refund_id"]
    assert "approved_at" in request_doc

    # Second approval attempt: must be rejected as not_pending and must NOT
    # write a second refund record (this is the money-safety invariant).
    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")
    assert exc.value.code == "not_pending"
    assert len(list(db.collection("refunds").stream())) == 1  # still exactly one


def test_approve_releases_claim_when_execute_refund_returns_failure(monkeypatch):
    """If provider.execute_refund() returns a failure result (not an
    exception), no money moved — the claim must be released back to
    PENDING_APPROVAL so a human can retry cleanly, not left stuck."""
    from customer_support_mas.providers.models import RefundResult

    db = _active_db_client()
    rid = _stage(db)

    class _FailingProvider:
        def execute_refund(self, **kwargs):
            return RefundResult(success=False, message="simulated provider failure")

    monkeypatch.setattr("backend.app.refund_approvals.get_provider", lambda tenant_id: _FailingProvider())

    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")

    assert exc.value.code == "refund_execution_failed"
    assert list(db.collection("refunds").stream()) == []
    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "PENDING_APPROVAL"
    # The claim (approver_id, claimed_at) must be fully released, not left
    # dangling on an otherwise-pending request.
    assert "approver_id" not in request_doc
    assert "claimed_at" not in request_doc


def test_approve_releases_claim_when_execute_refund_raises(monkeypatch):
    """Same as above but for an actual exception (e.g. a network error
    calling a real provider's API) instead of a clean failure result."""
    db = _active_db_client()
    rid = _stage(db)

    class _ExplodingProvider:
        def execute_refund(self, **kwargs):
            raise RuntimeError("simulated network failure")

    monkeypatch.setattr("backend.app.refund_approvals.get_provider", lambda tenant_id: _ExplodingProvider())

    with pytest.raises(RuntimeError):
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")

    assert list(db.collection("refunds").stream()) == []
    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "PENDING_APPROVAL"

    # The claim was fully released — a normal approval afterward must still
    # work (not permanently wedged by the failed attempt). Restore the real
    # provider first; the exploding one above was only for this attempt.
    monkeypatch.undo()
    result = approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")
    assert result["status"] == "approved"


def test_finalize_write_failure_leaves_a_safe_approving_state_not_a_double_refund(monkeypatch):
    """The core regression test for the claim/execute/finalize design: if
    the final status-update write fails AFTER execute_refund() already
    succeeded, the request must be left in APPROVING (money moved, not yet
    marked complete) rather than reverting to PENDING_APPROVAL — reverting
    would let a retry call execute_refund() a second time and genuinely
    double-refund the customer, which is exactly the failure mode
    dev.to/hadywalied's refund-agent article warns about.
    """
    from tests.mock_firestore import MockDocument

    db = _active_db_client()
    rid = _stage(db)

    original_set = MockDocument.set
    call_count = {"n": 0}

    def flaky_set(self, data):
        if self._parent_collection_name == "refund_requests" and self._doc_id == rid:
            call_count["n"] += 1
            if call_count["n"] == 2:  # 1st call = claim, 2nd call = finalize
                raise RuntimeError("simulated transient Firestore failure on finalize")
        original_set(self, data)

    monkeypatch.setattr(MockDocument, "set", flaky_set)

    with pytest.raises(RuntimeError):
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")

    monkeypatch.setattr(MockDocument, "set", original_set)

    # The refund WAS executed (money moved) before the finalize write failed.
    refunds = list(db.collection("refunds").stream())
    assert len(refunds) == 1

    # The request is left in APPROVING, not reverted to PENDING_APPROVAL —
    # this is the property that prevents a double refund on retry.
    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == APPROVING
    assert request_doc["approver_id"] == "approver-1"

    # A retry must be refused, and critically must NOT execute a second
    # refund — this is the money-safety invariant the whole fix exists for.
    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")
    assert exc.value.code == "not_pending"
    assert len(list(db.collection("refunds").stream())) == 1  # still exactly one


def test_find_stuck_approving_only_returns_old_enough_same_tenant_approving_requests():
    db = _active_db_client()
    from datetime import datetime, timedelta, timezone

    old_claimed_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    fresh_claimed_at = datetime.now(timezone.utc).isoformat()

    stuck_rid = _stage(db, order_id="ORD-11111")
    db.collection("refund_requests").document(stuck_rid).set(
        {
            **db.collection("refund_requests").document(stuck_rid).get().to_dict(),
            "status": "APPROVING",
            "claimed_at": old_claimed_at,
        }
    )

    fresh_rid = _stage(db, order_id="ORD-22222")
    db.collection("refund_requests").document(fresh_rid).set(
        {
            **db.collection("refund_requests").document(fresh_rid).get().to_dict(),
            "status": "APPROVING",
            "claimed_at": fresh_claimed_at,
        }
    )

    pending_rid = _stage(db, order_id="ORD-33333")  # still PENDING_APPROVAL, no claimed_at

    other_tenant_rid = _stage(db, order_id="ORD-44444", tenant_id="other-tenant")
    db.collection("refund_requests").document(other_tenant_rid).set(
        {
            **db.collection("refund_requests").document(other_tenant_rid).get().to_dict(),
            "status": "APPROVING",
            "claimed_at": old_claimed_at,
        }
    )

    stuck = find_stuck_approving(db, TEST_TENANT_ID, older_than_minutes=5)

    stuck_ids = [s["request_id"] for s in stuck]
    assert stuck_ids == [stuck_rid]
    assert fresh_rid not in stuck_ids
    assert pending_rid not in stuck_ids
    assert other_tenant_rid not in stuck_ids


def test_approve_not_found():
    db = _active_db_client()

    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, TEST_TENANT_ID, "REFREQ-does-not-exist", approver_id="approver-1")

    assert exc.value.code == "not_found"
    assert list(db.collection("refunds").stream()) == []


def test_self_approval_blocked():
    db = _active_db_client()
    rid = _stage(db, user_id="approver-1")

    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")

    assert exc.value.code == "self_approval"
    assert list(db.collection("refunds").stream()) == []
    # The request must remain untouched (still pending) after a blocked self-approval.
    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "PENDING_APPROVAL"


def test_self_approval_reported_even_when_already_resolved():
    """Check ordering: self_approval must be reported before not_pending.

    If the original requester later attempts to "approve" their own
    request — even one that's no longer PENDING_APPROVAL because a
    different (legitimate) approver already resolved it — the error code
    must still be self_approval, not a generic not_pending. Masking a
    self-approval *attempt* behind not_pending would hide a fraud-relevant
    signal from any audit/alerting logic built on these error codes.
    """
    db = _active_db_client()
    rid = _stage(db, user_id="demo-user-001")
    # A different, legitimate approver resolves the request first.
    approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")
    assert db.collection("refund_requests").document(rid).get().to_dict()["status"] == "APPROVED"

    # The original requester now attempts to "approve" their own
    # already-resolved request.
    with pytest.raises(ApprovalError) as exc:
        approve_refund(db, TEST_TENANT_ID, rid, approver_id="demo-user-001")

    assert exc.value.code == "self_approval"
    # No second refund record was written.
    assert len(list(db.collection("refunds").stream())) == 1


def test_reject_does_not_execute():
    db = _active_db_client()
    rid = _stage(db)

    result = reject_refund(db, TEST_TENANT_ID, rid, "approver-1", note="no evidence")

    assert result["status"] == "rejected"
    assert list(db.collection("refunds").stream()) == []

    request_doc = db.collection("refund_requests").document(rid).get().to_dict()
    assert request_doc["status"] == "REJECTED"
    assert request_doc["approver_id"] == "approver-1"
    assert request_doc["rejection_note"] == "no evidence"


def test_reject_not_found():
    db = _active_db_client()

    with pytest.raises(ApprovalError) as exc:
        reject_refund(db, TEST_TENANT_ID, "REFREQ-does-not-exist", "approver-1")

    assert exc.value.code == "not_found"


def test_reject_twice_is_not_pending():
    db = _active_db_client()
    rid = _stage(db)
    reject_refund(db, TEST_TENANT_ID, rid, "approver-1")

    with pytest.raises(ApprovalError) as exc:
        reject_refund(db, TEST_TENANT_ID, rid, "approver-1")

    assert exc.value.code == "not_pending"


def test_expire_stale_flips_only_past_deadline():
    db = _active_db_client()
    fresh_id = _stage(db)  # far-future expires_at — must remain pending
    stale_id = _stage(db, order_id="ORD-77777", expires_at=STALE_EXPIRES_AT)

    flipped = expire_stale(db, TEST_TENANT_ID)

    assert flipped == 1
    assert db.collection("refund_requests").document(stale_id).get().to_dict()["status"] == "EXPIRED"
    assert db.collection("refund_requests").document(fresh_id).get().to_dict()["status"] == "PENDING_APPROVAL"


def test_expire_stale_ignores_already_resolved_requests():
    db = _active_db_client()
    rid = _stage(db, expires_at=STALE_EXPIRES_AT)
    approve_refund(db, TEST_TENANT_ID, rid, approver_id="approver-1")

    flipped = expire_stale(db, TEST_TENANT_ID)

    assert flipped == 0
    assert db.collection("refund_requests").document(rid).get().to_dict()["status"] == "APPROVED"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
