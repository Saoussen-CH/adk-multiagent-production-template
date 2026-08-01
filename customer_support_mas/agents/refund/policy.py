"""Refund policy as versioned configuration, not hardcoded constants.

Real-world return policies are configured data, not application code:
a policy row (return window, reason taxonomy) owned by ops and versioned by
`effective_from` date, so an order is judged by the policy that was in
effect when it was placed/delivered — a later policy change never
retroactively applies to orders already in flight. This module is the
Firestore-backed equivalent: `get_active_policy(as_of)` picks the latest
policy version with `effective_from <= as_of`.

The refund *reason* is a fixed code the user picks from, not free text an
LLM/keyword-matcher classifies after the fact — matching how real return
flows (Amazon, Shopify, ...) work. The code-to-eligibility mapping IS the
policy; there's no ambiguous text to interpret.
"""

from datetime import datetime, timezone
from typing import Optional

from customer_support_mas.database import db_client

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

# Safe fallback for envs where the refund_policy collection hasn't been
# seeded yet (e.g. a fresh env that hasn't run `make seed-db` with the
# policy fixture) — not a hard failure, matches the pre-existing defaults.
DEFAULT_POLICY = {
    "version": 1,
    "effective_from": "2020-01-01",
    "window_days": 30,
    "reason_codes": REASON_CODES,
}


def get_active_policy(as_of: Optional[str] = None) -> dict:
    """Return the refund policy version active as of a given date.

    Args:
        as_of: "YYYY-MM-DD" date string. Defaults to today. Pass an order's
            delivered_date to judge that order by the policy in effect when
            it was delivered, not whatever the policy happens to be today.

    Returns:
        The policy version whose effective_from is the latest one
        <= as_of, or DEFAULT_POLICY if the refund_policy collection is
        empty/unseeded.
    """
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # MockCollection (tests/mock_firestore.py) only supports `==` in
    # .where(), so this reads the whole (small) collection and filters/sorts
    # in Python — identical behavior against real Firestore and the mock,
    # and fine for a reference/config collection with a handful of policy
    # versions (not a per-user data collection).
    docs = [d.to_dict() for d in db_client.collection("refund_policy").stream()]
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
