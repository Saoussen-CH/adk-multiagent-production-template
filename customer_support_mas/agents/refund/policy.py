"""Refund policy as versioned configuration, tenant-scoped.

Each tenant's refund_policy documents live under a tenant-scoped path —
this repo's mock Firestore and real Firestore both support subcollections,
so `tenants/{tenant_id}/refund_policy/{version}` keeps one tenant's policy
history fully separate from another's, consistent with the "no implicit
tenant" constraint everywhere else in this plan.

The refund *reason* is a fixed code the user picks from, not free text an
LLM/keyword-matcher classifies after the fact — matching how real return
flows (Amazon, Shopify, ...) work. The code-to-eligibility mapping IS the
policy; there's no ambiguous text to interpret.
"""

from datetime import datetime, timezone
from typing import Optional

from customer_support_mas.providers.registry import get_provider
from customer_support_mas.tenancy.config import load_tenant_config

REASON_CODES = [
    {"code": "defective", "label": "Product has a defect or malfunction", "eligible": True},
    {"code": "damaged", "label": "Product arrived damaged", "eligible": True},
    {"code": "wrong_item", "label": "Received wrong item", "eligible": True},
    {"code": "not_as_described", "label": "Product doesn't match description", "eligible": True},
    {"code": "missing_parts", "label": "Product missing parts or accessories", "eligible": True},
    {"code": "quality_issue", "label": "Product quality below expectations", "eligible": True},
    {"code": "arrived_late", "label": "Product arrived significantly late", "eligible": True},
    {"code": "duplicate_order", "label": "Accidentally ordered twice", "eligible": True},
    {"code": "changed_mind", "label": "Changed my mind / No longer want it", "eligible": False},
    {"code": "found_cheaper", "label": "Found it cheaper elsewhere", "eligible": False},
    {"code": "no_longer_need", "label": "No longer need the product", "eligible": False},
    {"code": "gift_unwanted", "label": "Gift recipient didn't want it", "eligible": False},
    {"code": "ordering_mistake", "label": "Ordered by mistake (but item is fine)", "eligible": False},
]

# Safe fallback for tenants where the refund_policy collection hasn't been
# seeded yet (e.g. a genuinely new tenant) — not a hard failure, matches
# the pre-existing defaults.
DEFAULT_POLICY = {
    "version": 1,
    "effective_from": "2020-01-01",
    "window_days": 30,
    "reason_codes": REASON_CODES,
}


def get_active_policy(tenant_id: str, as_of: Optional[str] = None) -> dict:
    """Return the refund policy version active for this tenant as of a given date.

    tenant_id is required — there is no cross-tenant default policy. Falls
    back to DEFAULT_POLICY only if this specific tenant has no
    refund_policy documents seeded yet (a genuinely new tenant), not as a
    substitute for a missing tenant_id.

    Args:
        tenant_id: The tenant whose policy to look up.
        as_of: "YYYY-MM-DD" date string. Defaults to today. Pass an order's
            delivered_date to judge that order by the policy in effect when
            it was delivered, not whatever the policy happens to be today.

    Returns:
        The policy version whose effective_from is the latest one
        <= as_of, or DEFAULT_POLICY if this tenant's refund_policy
        subcollection is empty/unseeded.
    """
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    provider = get_provider(tenant_id)
    config = load_tenant_config(tenant_id)
    # Narrow, deliberate exception: CommerceProvider doesn't (and shouldn't)
    # expose a generic "raw collection" method, since that would leak
    # Firestore-specific concepts into the backend-agnostic interface.
    # Policy storage stays a FirestoreProvider-specific implementation
    # detail for now. Guarded so a future non-Firestore provider doesn't
    # crash here — it just gets DEFAULT_POLICY.
    db = provider._db if hasattr(provider, "_db") else None
    if db is None:
        return DEFAULT_POLICY

    policy_ref = config.refund_policy_ref or tenant_id
    # MockCollection (tests/mock_firestore.py) only supports `==` in
    # .where(), so this reads the whole (small) subcollection and
    # filters/sorts in Python — identical behavior against real Firestore
    # and the mock, and fine for a reference/config collection with a
    # handful of policy versions (not a per-user data collection).
    docs = [d.to_dict() for d in db.collection("tenants").document(policy_ref).collection("refund_policy").stream()]
    candidates = [p for p in docs if p and p.get("effective_from", "9999-99-99") <= as_of]

    if not candidates:
        return DEFAULT_POLICY

    return max(candidates, key=lambda p: p["effective_from"])


def get_reason_code(policy: dict, code: str) -> Optional[dict]:
    """Look up a reason code's {code, label, eligible} within a policy version."""
    for reason in policy.get("reason_codes", []):
        if reason.get("code") == code:
            return reason
    return None
