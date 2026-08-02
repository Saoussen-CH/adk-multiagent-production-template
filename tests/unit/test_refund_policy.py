"""get_active_policy(tenant_id, as_of) reads tenants/{tenant_id}/refund_policy/{version}
from Firestore — a subcollection, per Task 6's tenant-scoped design
(customer_support_mas/agents/refund/policy.py). Every other refund test
only ever exercises the empty-subcollection fallback to DEFAULT_POLICY;
these tests prove the actual write-then-read round trip through
tests/mock_firestore.py's document().collection() nesting, and the
"latest effective_from <= as_of wins" selection logic, both of which are
otherwise never exercised by anything that would fail if either broke.

Uses the "test-tenant" tenant already seeded by the autouse
`_seed_default_test_tenant` fixture (tests/unit/conftest.py), and the
`mock_db` instance the autouse `mock_backends` fixture wires up as the
live backend for get_provider(tenant_id)._db / load_tenant_config — so no
extra monkeypatching is needed here.
"""

from customer_support_mas.agents.refund.policy import DEFAULT_POLICY, get_active_policy

TENANT_ID = "test-tenant"


def _seed_policy_version(mock_db, tenant_id: str, version: int, effective_from: str, window_days: int) -> None:
    (
        mock_db.collection("tenants")
        .document(tenant_id)
        .collection("refund_policy")
        .document(str(version))
        .set(
            {
                "version": version,
                "effective_from": effective_from,
                "window_days": window_days,
                "reason_codes": [{"code": "damaged", "label": "Product arrived damaged", "eligible": True}],
            }
        )
    )


def test_get_active_policy_falls_back_to_default_when_unseeded(mock_db):
    """Baseline: no refund_policy documents seeded for this tenant -> DEFAULT_POLICY.
    This is the path every other refund test exercises; asserting it here
    too makes the contrast with the seeded-version tests below explicit."""
    policy = get_active_policy(TENANT_ID, as_of="2026-01-15")

    assert policy is DEFAULT_POLICY


def test_get_active_policy_returns_seeded_version_not_default(mock_db):
    """A single seeded version is round-tripped back through
    tenants/{tenant_id}/refund_policy/{version} — proves the mock's
    document().collection().stream() nesting actually works, not just that
    the fallback path (the only thing exercised elsewhere) returns something."""
    _seed_policy_version(mock_db, TENANT_ID, version=2, effective_from="2025-06-01", window_days=14)

    policy = get_active_policy(TENANT_ID, as_of="2025-12-01")

    assert policy is not DEFAULT_POLICY
    assert policy["version"] == 2
    assert policy["window_days"] == 14


def test_get_active_policy_picks_latest_effective_from_leq_as_of(mock_db):
    """Three versions seeded. as_of falls between v2's and v3's
    effective_from, so v2 must win: v1 is superseded, v3 hasn't taken
    effect yet. This is the actual "judge the order by the policy in
    effect when it happened" logic get_active_policy exists for."""
    _seed_policy_version(mock_db, TENANT_ID, version=1, effective_from="2020-01-01", window_days=30)
    _seed_policy_version(mock_db, TENANT_ID, version=2, effective_from="2025-06-01", window_days=14)
    _seed_policy_version(mock_db, TENANT_ID, version=3, effective_from="2026-06-01", window_days=7)

    policy = get_active_policy(TENANT_ID, as_of="2025-12-01")

    assert policy["version"] == 2
    assert policy["window_days"] == 14

    # And as_of on/after v3's effective_from picks v3, not v1 or v2.
    later_policy = get_active_policy(TENANT_ID, as_of="2026-07-01")

    assert later_policy["version"] == 3
    assert later_policy["window_days"] == 7


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
