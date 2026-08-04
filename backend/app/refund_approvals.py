"""Deterministic refund approval/rejection/expiry (HITL step 2 — the execute half).

Task 8 made ``process_refund`` STAGE a ``PENDING_APPROVAL`` request into the
``refund_requests`` Firestore collection instead of executing a refund. This
module is the other half of that human-in-the-loop design:

    Execution lives here and only here. The agent stages; a human approves
    via the API; this code moves the money.

None of the LLM-facing agent tools ever write to the ``refunds`` collection
directly anymore — only ``approve_refund`` below triggers that write, and
only after the dual-control and idempotency gates pass. As of Task 7,
``approve_refund`` no longer writes to ``refunds`` itself: it resolves the
requesting tenant's ``CommerceProvider`` (``get_provider(tenant_id)``) and
calls ``provider.execute_refund(...)``, so the actual write goes through
whatever backend that tenant is configured for (native Firestore today,
potentially Shopify or another platform later) instead of a hardcoded
Firestore collection reference here.

All functions here are plain, side-effect-explicit functions that take a raw
Firestore client (or an in-memory test double with the same surface) as their
first argument, plus the ``tenant_id`` that client belongs to. Nothing here
imports ``backend.app.database`` or holds any module-level client state, so
callers (the API endpoints in ``backend/app/main.py``, or a scheduled job for
``expire_stale``) inject whichever client they already have.

**The ``db`` handle must be the requesting tenant's own Firestore database**
— the one behind ``get_provider(tenant_id)._db``, which is where
``process_refund`` stages its ``PENDING_APPROVAL`` documents. It used to be a
single hardcoded ``database_id="customer-support-db"`` handle in
``main.py``, which happened to coincide with the one seeded tenant's
database and so masked a split brain: for any tenant configured with a
different ``database_id``, staged refunds were invisible to the approver
queue and ``approve_refund`` raised "not_found".

``tenant_id`` is also matched against each document's own ``tenant_id``
field (written at staging time) rather than trusted implicitly from the
handle. That is defence in depth, not redundancy: it is the guard that still
holds if two tenants are ever mis-configured onto the same database, and it
turns "wrong tenant for this request_id" into an explicit ``not_found``
instead of a cross-tenant write.

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

A related but distinct gap — a *sequential* retry after a partial failure,
not concurrent callers — is closed as of the claim/execute/finalize design
in ``approve_refund`` below: previously, if ``provider.execute_refund()``
succeeded but the final status-update write failed (a crash, a transient
Firestore error), the request stayed ``PENDING_APPROVAL`` and a retry (or a
confused approver re-clicking Approve) would call ``execute_refund()`` a
second time — an actual double-refund, not just a display glitch. The
status is now claimed (``APPROVING``) *before* ``execute_refund()`` runs,
so a retry's fresh status read is refused by the same ``not_pending`` gate
instead of racing past it. See ``find_stuck_approving()`` for the resulting
(safe, non-double-refunding) failure mode this leaves to detect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from customer_support_mas.providers.registry import get_provider

PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVING = "APPROVING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"

REFUND_REQUESTS_COLLECTION = "refund_requests"


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


def _get_request(db, tenant_id: str, request_id: str) -> Dict[str, Any]:
    """Read one tenant's refund_requests doc by id, or raise
    ApprovalError('not_found').

    A document belonging to a different tenant is reported as not_found
    rather than as a distinct error: an approver acting for tenant A must
    not be able to probe whether a request_id exists under tenant B.
    """
    snap = db.collection(REFUND_REQUESTS_COLLECTION).document(request_id).get()
    if not snap.exists:
        raise ApprovalError("not_found", f"No refund request found with id {request_id!r}")
    request = snap.to_dict()
    if request.get("tenant_id") != tenant_id:
        raise ApprovalError("not_found", f"No refund request found with id {request_id!r}")
    return request


def list_pending(db, tenant_id: str) -> List[Dict[str, Any]]:
    """Return this tenant's PENDING_APPROVAL refund requests, each with its
    request_id. Requests belonging to any other tenant are never returned,
    even if they happen to live in the same database."""
    pending = []
    for snap in db.collection(REFUND_REQUESTS_COLLECTION).stream():
        data = snap.to_dict()
        if data.get("status") == PENDING_APPROVAL and data.get("tenant_id") == tenant_id:
            entry = dict(data)
            entry["request_id"] = snap.id
            pending.append(entry)
    return pending


def approve_refund(db, tenant_id: str, request_id: str, approver_id: str) -> Dict[str, Any]:
    """Approve one tenant's pending refund request and execute the refund.

    Dual control: the approver may not be the original requester.
    Idempotent: re-checks status == PENDING_APPROVAL immediately before
    writing anything, so a retried/double-clicked approval raises
    ApprovalError("not_pending") on the second call instead of writing a
    second refund record.

    Three-phase claim/execute/finalize, not a single execute-then-write:
    provider.execute_refund() may be a real external call (a Shopify-backed
    tenant's Refund API, eventually), so it cannot be wrapped in the same
    Firestore transaction as the request-document write — there is no
    atomic way to guarantee both succeed or both fail together. Without the
    claim step, a caller that retried after execute_refund() succeeded but
    the final status write failed (a crash, a transient Firestore error)
    would re-enter this function, see status still PENDING_APPROVAL, and
    call execute_refund() a SECOND time — a real double-refund, not just a
    display glitch. The claim (PENDING_APPROVAL -> APPROVING) happens
    BEFORE execute_refund() runs, so a retry's fresh status re-read sees
    APPROVING and is refused by the not_pending gate instead of racing
    past it. If execute_refund() itself fails, the claim is released back
    to PENDING_APPROVAL so a human can retry cleanly — no money moved, safe
    to undo. If only the final write (APPROVING -> APPROVED) fails, the
    request is left in APPROVING: money HAS moved, the claim already
    prevents a second execute_refund() call, so this is now a safe,
    detectable inconsistency rather than a dangerous one — see
    find_stuck_approving() below.
    """
    request = _get_request(db, tenant_id, request_id)
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
    # No write happens between this check and the claim below, so this is
    # the single point where a double-approve (or a retry after a claimed-
    # but-unfinalized request) is caught (see module docstring for the
    # real-Firestore-transaction caveat on the concurrent case).
    if request.get("status") != PENDING_APPROVAL:
        raise ApprovalError("not_pending", f"Refund request {request_id!r} is not pending approval")

    # _get_request already proved request["tenant_id"] == tenant_id.
    order_id = request["order_id"]
    user_id = request["user_id"]  # the ORIGINAL requester, not the approver
    items = request["items"]
    refund_amount = request["refund_amount"]
    reason = request["reason"]
    reason_category = request["reason_category"]

    # PHASE 1: Claim. Flips PENDING_APPROVAL -> APPROVING before the
    # possibly-external execute_refund() call below, so a retry that
    # re-enters this function (and re-reads status via _get_request) is
    # refused by the not_pending check above instead of re-executing the
    # refund.
    claimed_request = dict(request)
    claimed_request.update(
        {
            "status": APPROVING,
            "approver_id": approver_id,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    doc_ref.set(claimed_request)

    # PHASE 2: Execute. Delegate the actual refund write to the requesting
    # tenant's CommerceProvider (Task 7) instead of writing to "refunds"
    # directly here. This mirrors, field for field, the shape the old
    # (pre-provider) code wrote directly to "refunds":
    #   - "items" gets the same per-item "refund_amount" the old code
    #     computed (item.get("price", 0) * item.get("qty", 1)) baked in
    #     before being handed to the provider — that enrichment is specific
    #     to how this backend's approver UI reads refund records, not a
    #     general CommerceProvider concern, so it happens here rather than
    #     inside FirestoreProvider.execute_refund. Unlike the old code, this
    #     does NOT hard-require "name" to be present (item.get, not
    #     item[]) — deliberately, since the PENDING_APPROVAL doc this reads
    #     is written by process_refund()'s own item shape, not re-derived
    #     from "orders" here, and this module shouldn't assume its exact
    #     keys.
    #   - "reason"/"reason_category" are passed through as execute_refund's
    #     optional parameters (CommerceProvider, Task 7) so they survive
    #     into the final refund record for the approver UI, which reads
    #     both fields directly.
    #   - "original_order_total" from the old record is intentionally NOT
    #     reproduced: the PENDING_APPROVAL request doc this function reads
    #     (see `request` above) only carries order_id/user_id/items/
    #     refund_amount/reason/reason_category/tenant_id — it never stored
    #     the order's total. Recovering it here would mean this module doing
    #     its own "orders" lookup, which is more coupling than this
    #     execute-only module (see module docstring) should take on for a
    #     field nothing currently reads. If a consumer needs it, prefer
    #     having process_refund() capture order_data.get("total") into the
    #     request doc at staging time, not adding a lookup here.
    items_with_refund_amount = [{**item, "refund_amount": item.get("price", 0) * item.get("qty", 1)} for item in items]

    provider = get_provider(tenant_id)
    try:
        result = provider.execute_refund(
            tenant_id=tenant_id,
            order_id=order_id,
            customer_id=user_id,
            items=items_with_refund_amount,
            amount=refund_amount,
            reason=reason,
            reason_category=reason_category,
        )
    except Exception:
        # No evidence money moved — release the claim so a human can retry
        # cleanly instead of the request being stuck in APPROVING forever.
        doc_ref.set(request)
        raise

    if not result.success:
        # Explicit failure result (not an exception) — same reasoning:
        # release the claim.
        doc_ref.set(request)
        raise ApprovalError("refund_execution_failed", result.message)

    refund_id = result.refund_id

    # PHASE 3: Finalize. If THIS write fails, the request is left in
    # APPROVING — refund executed, not yet marked APPROVED. That is now a
    # safe, detectable inconsistency (find_stuck_approving() surfaces it)
    # rather than a double-refund risk, since the claim above already
    # blocks any retry from reaching execute_refund() again.
    updated_request = dict(claimed_request)
    updated_request.update(
        {
            "status": APPROVED,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "refund_id": refund_id,
        }
    )
    doc_ref.set(updated_request)

    return {"status": "approved", "refund_id": refund_id}


def reject_refund(db, tenant_id: str, request_id: str, approver_id: str, note: str = "") -> Dict[str, Any]:
    """Reject one tenant's pending refund request. Never writes to the refunds
    collection."""
    request = _get_request(db, tenant_id, request_id)
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


def expire_stale(db, tenant_id: str) -> int:
    """Flip this tenant's PENDING_APPROVAL requests whose expires_at has passed
    to EXPIRED.

    Returns the number of requests flipped. Scoped to one tenant like every
    other function here, so a scheduled job sweeps tenants explicitly rather
    than reaching across all of them through whichever handle it happens to
    hold.
    """
    now = datetime.now(timezone.utc)
    flipped = 0

    for snap in db.collection(REFUND_REQUESTS_COLLECTION).stream():
        request = snap.to_dict()
        if request.get("status") != PENDING_APPROVAL or request.get("tenant_id") != tenant_id:
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


def find_stuck_approving(db, tenant_id: str, older_than_minutes: int = 5) -> List[Dict[str, Any]]:
    """Return this tenant's refund_requests stuck in APPROVING for longer
    than `older_than_minutes`.

    APPROVING is the safe-but-incomplete state approve_refund() leaves
    behind when provider.execute_refund() succeeded (money moved) but the
    final status-update write failed before marking the request APPROVED
    (see approve_refund's claim/execute/finalize docstring). A request
    staying APPROVING briefly (mid-call) is normal; one still APPROVING
    minutes later means the finalize write never landed and needs an
    operator's attention.

    Detection only — this does not attempt automatic recovery. Safely
    completing the finalize write requires knowing the refund_id that was
    generated, which isn't necessarily recoverable from this document alone
    once the finalize write is what would have carried it; a human should
    cross-reference the provider's own refund record (`refunds` collection,
    or the equivalent on a non-Firestore provider) before manually
    resolving one of these. Not yet wired into a scheduled job — same
    situation as expire_stale() above — surfaced here for an operator or a
    future reconciliation job to consume.
    """
    now = datetime.now(timezone.utc)
    stuck = []

    for snap in db.collection(REFUND_REQUESTS_COLLECTION).stream():
        request = snap.to_dict()
        if request.get("status") != APPROVING or request.get("tenant_id") != tenant_id:
            continue

        claimed_at_str = request.get("claimed_at")
        if not claimed_at_str:
            continue

        claimed_at = datetime.fromisoformat(claimed_at_str)
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)

        age_minutes = (now - claimed_at).total_seconds() / 60
        if age_minutes < older_than_minutes:
            continue

        entry = dict(request)
        entry["request_id"] = snap.id
        stuck.append(entry)

    return stuck
