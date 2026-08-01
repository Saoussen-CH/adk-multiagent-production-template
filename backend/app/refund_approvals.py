"""Deterministic refund approval/rejection/expiry (HITL step 2 — the execute half).

Task 8 made ``process_refund`` STAGE a ``PENDING_APPROVAL`` request into the
``refund_requests`` Firestore collection instead of executing a refund. This
module is the other half of that human-in-the-loop design:

    Execution lives here and only here. The agent stages; a human approves
    via the API; this code moves the money.

None of the LLM-facing agent tools ever write to the ``refunds`` collection
directly anymore — only ``approve_refund`` below does, and only after the
dual-control and idempotency gates pass.

All functions here are plain, side-effect-explicit functions that take a raw
Firestore client (or an in-memory test double with the same surface) as their
first argument — e.g. ``Database(project_id, database_id).db`` in production,
or ``tests.mock_firestore.MockFirestoreClient()`` in tests. Nothing here
imports ``backend.app.database`` or holds any module-level client state, so
callers (Task 10's API endpoints, or a scheduled job for ``expire_stale``)
inject whichever client they already have.

Concurrency note: a real Firestore-backed deployment should run
``approve_refund``'s read-check-write inside ``db.transaction()`` so a
concurrent double-approve can't race between the status re-read and the
write. The in-memory mock used by this module's tests has no transaction
API, so this implementation instead does a plain synchronous
read-check-write: the request document is re-read, its status is checked
against ``PENDING_APPROVAL`` immediately before any write, and no other
write happens in between within the same function call. That is sufficient
to make the double-approve test in this suite pass deterministically, but it
is NOT equivalent to a real Firestore transaction under genuine concurrent
callers (e.g. two API requests racing in separate processes). Wiring
``db.transaction()`` around this logic is deferred to when this module is
pointed at real Firestore (tracked as follow-up, not silently dropped).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"

REFUND_REQUESTS_COLLECTION = "refund_requests"
REFUNDS_COLLECTION = "refunds"


class ApprovalError(Exception):
    """Raised for any invalid approval/rejection/expiry transition.

    ``code`` is one of:
      - "not_found": no refund_requests doc with that request_id.
      - "not_pending": the request is no longer PENDING_APPROVAL (already
        approved, rejected, expired, or a concurrent call already moved it) —
        this is the idempotency gate that prevents double-refunding.
      - "self_approval": approver_id matches the original requester's
        user_id (dual control — a requester cannot approve their own
        refund).
      - "not_approver": reserved for Task 10's API-layer authorization
        dependency (checking the approver against an allowed-approvers
        list). Not raised by this module.
    """

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _get_request(db, request_id: str) -> Dict[str, Any]:
    """Read a refund_requests doc by id, or raise ApprovalError('not_found')."""
    snap = db.collection(REFUND_REQUESTS_COLLECTION).document(request_id).get()
    if not snap.exists:
        raise ApprovalError("not_found", f"No refund request found with id {request_id!r}")
    return snap.to_dict()


def list_pending(db) -> List[Dict[str, Any]]:
    """Return all PENDING_APPROVAL refund requests, each with its request_id."""
    pending = []
    for snap in db.collection(REFUND_REQUESTS_COLLECTION).stream():
        data = snap.to_dict()
        if data.get("status") == PENDING_APPROVAL:
            entry = dict(data)
            entry["request_id"] = snap.id
            pending.append(entry)
    return pending


def approve_refund(db, request_id: str, approver_id: str) -> Dict[str, Any]:
    """Approve a pending refund request and execute the refund.

    Dual control: the approver may not be the original requester.
    Idempotent: re-checks status == PENDING_APPROVAL immediately before
    writing anything, so a retried/double-clicked approval raises
    ApprovalError("not_pending") on the second call instead of writing a
    second refund record.
    """
    request = _get_request(db, request_id)
    doc_ref = db.collection(REFUND_REQUESTS_COLLECTION).document(request_id)

    # Dual control — checked as soon as the request is known to exist, before
    # any status/eligibility check or write. This must come before the
    # not_pending check below: if the original requester attempts to
    # "approve" their own request regardless of its current status (even one
    # already resolved by someone else), that is a self-approval *attempt*
    # and must be reported as such — not masked behind a generic not_pending
    # code — so audit/alerting logic built on these error codes can see it.
    if approver_id == request.get("user_id"):
        raise ApprovalError("self_approval", "Approver cannot be the original requester")

    # Idempotency gate — re-read-and-check immediately before any write.
    # No write happens between this check and the writes below, so this is
    # the single point where a double-approve is caught (see module
    # docstring for the real-Firestore-transaction caveat).
    if request.get("status") != PENDING_APPROVAL:
        raise ApprovalError("not_pending", f"Refund request {request_id!r} is not pending approval")

    order_id = request["order_id"]
    user_id = request["user_id"]  # the ORIGINAL requester, not the approver
    items = request["items"]
    refund_amount = request["refund_amount"]
    reason = request["reason"]
    reason_category = request["reason_category"]

    # Execute: write the refund record. This is CLOSE to, but not byte-identical
    # with, the shape the old (pre-HITL) process_refund wrote directly to
    # "refunds":
    #   - "items" here preserves every field already on the staged item dict
    #     (product_id/name/qty/price for real order items) and adds the same
    #     per-item "refund_amount" the old code computed, so for real order
    #     data the resulting shape matches. Unlike the old code, this does
    #     NOT hard-require "name" to be present (item.get, not item[]) —
    #     deliberately, since the PENDING_APPROVAL doc this reads is written
    #     by process_refund()'s own item shape, not re-derived from "orders"
    #     here, and this module shouldn't assume its exact keys.
    #   - "original_order_total" from the old record is intentionally NOT
    #     reproduced: the PENDING_APPROVAL request doc this function reads
    #     (see `request` above) only carries order_id/user_id/items/
    #     refund_amount/reason/reason_category — it never stored the order's
    #     total. Recovering it here would mean this module doing its own
    #     "orders" collection lookup, which is more coupling than this
    #     execute-only module (see module docstring) should take on for a
    #     field nothing currently reads. If a consumer needs it, prefer
    #     having process_refund() capture order_data.get("total") into the
    #     request doc at staging time, not adding a lookup here.
    refund_id = f"REF-{order_id.replace('ORD-', '')}-{uuid.uuid4().hex[:8]}"
    refund_record = {
        "refund_id": refund_id,
        "order_id": order_id,
        "customer_id": user_id,
        "reason": reason,
        "reason_category": reason_category,
        "status": "pending",  # the refund's OWN lifecycle status (pending/cancelled)
        # Intentionally naive (no tzinfo), unlike approved_at/rejected_at below —
        # this mirrors the pre-HITL process_refund code exactly (verified against
        # git history). Do not "fix" this into timezone-aware; that would be a
        # regression in the refund record's historical shape.
        "created_at": datetime.now().isoformat(),
        "items": [
            {**item, "refund_amount": item.get("price", 0) * item.get("qty", 1)}
            for item in items
        ],
        "total_refund_amount": refund_amount,
    }
    db.collection(REFUNDS_COLLECTION).document(refund_id).set(refund_record)

    # Mark the approval request itself as APPROVED. Writing the refund doc
    # above (scanned by customer_support_mas's duplicate-refund detection)
    # IS the complete "mark items refunded" side effect — nothing else to do.
    updated_request = dict(request)
    updated_request.update(
        {
            "status": APPROVED,
            "approver_id": approver_id,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "refund_id": refund_id,
        }
    )
    doc_ref.set(updated_request)

    return {"status": "approved", "refund_id": refund_id}


def reject_refund(db, request_id: str, approver_id: str, note: str = "") -> Dict[str, Any]:
    """Reject a pending refund request. Never writes to the refunds collection."""
    request = _get_request(db, request_id)
    doc_ref = db.collection(REFUND_REQUESTS_COLLECTION).document(request_id)

    if request.get("status") != PENDING_APPROVAL:
        raise ApprovalError("not_pending", f"Refund request {request_id!r} is not pending approval")

    updated_request = dict(request)
    updated_request.update(
        {
            "status": REJECTED,
            "approver_id": approver_id,
            "rejected_at": datetime.now(timezone.utc).isoformat(),
            "rejection_note": note,
        }
    )
    doc_ref.set(updated_request)

    return {"status": "rejected"}


def expire_stale(db) -> int:
    """Flip PENDING_APPROVAL requests whose expires_at has passed to EXPIRED.

    Returns the number of requests flipped.
    """
    now = datetime.now(timezone.utc)
    flipped = 0

    for snap in db.collection(REFUND_REQUESTS_COLLECTION).stream():
        request = snap.to_dict()
        if request.get("status") != PENDING_APPROVAL:
            continue

        expires_at_str = request.get("expires_at")
        if not expires_at_str:
            continue

        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at > now:
            continue

        updated_request = dict(request)
        updated_request["status"] = EXPIRED
        db.collection(REFUND_REQUESTS_COLLECTION).document(snap.id).set(updated_request)
        flipped += 1

    return flipped
