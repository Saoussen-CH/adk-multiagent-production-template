"""Per-tenant rate limiting prevents one tenant from starving others
sharing the same light-tier pool project's quota (spec section 6). In-process
token bucket for this phase — no Memorystore/Redis instance exists yet;
the interface is designed so swapping to a Redis-backed implementation
later doesn't change any caller (see module docstring)."""

import time

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    from customer_support_mas import rate_limiting

    rate_limiting._buckets.clear()
    yield
    rate_limiting._buckets.clear()


def test_first_request_always_allowed():
    from customer_support_mas.rate_limiting import check_tenant_rate_limit

    assert check_tenant_rate_limit("tenant-a", limit=5, window_seconds=60) is True


def test_exceeding_limit_within_window_is_denied():
    from customer_support_mas.rate_limiting import check_tenant_rate_limit

    for _ in range(5):
        assert check_tenant_rate_limit("tenant-a", limit=5, window_seconds=60) is True

    assert check_tenant_rate_limit("tenant-a", limit=5, window_seconds=60) is False


def test_different_tenants_have_independent_limits():
    from customer_support_mas.rate_limiting import check_tenant_rate_limit

    for _ in range(5):
        check_tenant_rate_limit("tenant-a", limit=5, window_seconds=60)

    assert check_tenant_rate_limit("tenant-a", limit=5, window_seconds=60) is False
    assert check_tenant_rate_limit("tenant-b", limit=5, window_seconds=60) is True


def test_limit_resets_after_window_expires():
    """Timing-sensitive by nature (it's testing wall-clock window expiry),
    so this uses a generous window (0.3s) and sleep margin (0.5s) rather
    than the tightest values that would still pass locally — a 0.1s/0.15s
    window/sleep pair is close enough that CI scheduling jitter or GC
    pauses could make the post-sleep assertion observe less elapsed time
    than intended and flake. This margin trades ~0.5s of test time for
    that risk going away; still fast enough for test-tools' "fast" bar."""
    from customer_support_mas.rate_limiting import check_tenant_rate_limit

    for _ in range(3):
        check_tenant_rate_limit("tenant-a", limit=3, window_seconds=0.3)
    assert check_tenant_rate_limit("tenant-a", limit=3, window_seconds=0.3) is False

    time.sleep(0.5)
    assert check_tenant_rate_limit("tenant-a", limit=3, window_seconds=0.3) is True


def test_denied_request_does_not_consume_budget():
    """A False return must not itself be recorded as a consumed slot —
    otherwise repeatedly polling a saturated bucket would keep pushing the
    window's oldest timestamp forward and the limit would never recover
    within the nominal window."""
    from customer_support_mas.rate_limiting import check_tenant_rate_limit

    for _ in range(2):
        assert check_tenant_rate_limit("tenant-a", limit=2, window_seconds=60) is True

    # Poll several times while saturated — none of these should count.
    for _ in range(5):
        assert check_tenant_rate_limit("tenant-a", limit=2, window_seconds=60) is False

    from customer_support_mas import rate_limiting

    assert len(rate_limiting._buckets["tenant-a"]) == 2
