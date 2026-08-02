"""Per-tenant rate limiting — prevents one light-tier tenant from starving
others sharing the same pool project's GCP quota (spec section 6).

In-process token-bucket implementation for this phase: correct for a
single backend instance, approximate (each instance has its own bucket)
once the backend scales to multiple Cloud Run instances. This is an
explicit, accepted limitation for now — swap check_tenant_rate_limit's
internals for a Memorystore/Redis-backed sliding window when horizontal
scaling makes shared state necessary; the function signature and every
caller stay the same, so that swap doesn't ripple.
"""

import time

DEFAULT_LIMIT = 60
DEFAULT_WINDOW_SECONDS = 60.0

_buckets: dict[str, list[float]] = {}


def check_tenant_rate_limit(
    tenant_id: str, limit: int = DEFAULT_LIMIT, window_seconds: float = DEFAULT_WINDOW_SECONDS
) -> bool:
    """Return True if this request is allowed under tenant_id's rate limit,
    False if it should be rejected/throttled. Recording the request as
    consumed is NOT done here on a False return — callers should reject the
    request outright, not count denied attempts against the budget."""
    now = time.monotonic()
    bucket = _buckets.setdefault(tenant_id, [])

    # Drop timestamps outside the current window
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)

    if len(bucket) >= limit:
        return False

    bucket.append(now)
    return True
