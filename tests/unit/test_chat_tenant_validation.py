"""/api/chat must reject an unknown tenant_id before it is used for anything.

Final-review finding I1. `request.tenant_id` is a caller-supplied string that
went straight into `check_tenant_rate_limit(...)` with no validation:

  - any client could burn another tenant's rate-limit budget just by claiming
    their id, and
  - `rate_limiting._buckets` is a module-level dict keyed on that string, so
    arbitrary values grew it without bound — an unauthenticated memory DoS,
  - and an unknown tenant wasn't rejected at all: it survived into a tool
    call, where `TenantNotFoundError` was swallowed by `@tool_error_handler`
    into a vague chat reply instead of a clean HTTP error.

The fix resolves the tenant first, so these tests assert both the rejection
*and* its ordering relative to the rate limiter.
"""

import os

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault(
    "AGENT_ENGINE_RESOURCE_NAME",
    "projects/test-project/locations/us-central1/reasoningEngines/123",
)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import main as main_module  # noqa: E402
from backend.app.main import app  # noqa: E402

client = TestClient(app)

KNOWN_TENANT = "test-tenant"  # seeded by tests/unit/conftest.py's autouse fixture
UNKNOWN_TENANT = "not-a-real-tenant"


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state():
    """Both rate limiters keep module-level state; reset each between tests.

    `customer_support_mas.rate_limiting` is the per-tenant limiter under test.
    `backend.app.rate_limiter` is the pre-existing per-user/IP limiter that
    guards the endpoint; without resetting it, the high-volume tests below
    trip it and every later test in this module gets a 429.
    """
    from backend.app.rate_limiter import rate_limiter
    from customer_support_mas import rate_limiting

    rate_limiting._buckets.clear()
    rate_limiter.reset()
    yield
    rate_limiting._buckets.clear()
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def _reset_overrides():
    yield
    app.dependency_overrides.clear()


def _chat(tenant_id, message="hello", user_id="anon-smoke"):
    return client.post(
        "/api/chat",
        headers={"X-User-Id": user_id},
        json={"message": message, "tenant_id": tenant_id},
    )


def test_unknown_tenant_is_rejected_with_404(monkeypatch):
    called = {}

    def _should_not_run(*args, **kwargs):
        called["agent"] = True
        raise AssertionError("the agent must never be queried for an unknown tenant")

    monkeypatch.setattr(main_module.agent_client, "query_agent", _should_not_run)

    response = _chat(UNKNOWN_TENANT)

    assert response.status_code == 404
    assert UNKNOWN_TENANT in response.json()["detail"]
    assert "agent" not in called


def test_unknown_tenant_never_consumes_a_rate_limit_bucket():
    """The DoS half of the finding: an arbitrary string must not be able to
    create an entry in the process-wide bucket dict."""
    from customer_support_mas import rate_limiting

    for _ in range(5):
        _chat(UNKNOWN_TENANT)

    assert UNKNOWN_TENANT not in rate_limiting._buckets
    assert rate_limiting._buckets == {}


def test_unknown_tenant_cannot_drain_a_real_tenants_budget(monkeypatch):
    """Claiming someone else's id used to spend their budget. Now the request
    is rejected before the bucket is touched at all — so a known tenant's
    budget is untouched by traffic aimed at an unknown one."""
    from customer_support_mas import rate_limiting

    for _ in range(200):
        _chat(UNKNOWN_TENANT)

    assert rate_limiting.check_tenant_rate_limit(KNOWN_TENANT) is True


def test_validation_runs_before_the_tenant_rate_limiter(monkeypatch):
    """Ordering matters: validating after rate limiting would still let an
    unknown id allocate a bucket."""
    seen = []

    monkeypatch.setattr(
        main_module,
        "check_tenant_rate_limit",
        lambda tenant_id, *a, **kw: seen.append(tenant_id) or True,
    )

    _chat(UNKNOWN_TENANT)

    assert seen == [], f"rate limiter was consulted for an unvalidated tenant: {seen}"


def test_known_tenant_still_reaches_the_agent(monkeypatch):
    """The guard must not reject legitimate traffic."""

    async def _fake_query(user_id, agent_engine_session_id, message, tenant_id):
        assert tenant_id == KNOWN_TENANT
        return "hello back", "agent-session-1", []

    monkeypatch.setattr(main_module.agent_client, "query_agent", _fake_query)
    monkeypatch.setattr(main_module.db, "create_session", lambda **kw: "internal-session-1")
    monkeypatch.setattr(main_module.db, "save_message", lambda *a, **kw: None)

    response = _chat(KNOWN_TENANT)

    assert response.status_code == 200, response.text
    assert response.json()["response"] == "hello back"


def test_tenant_id_is_required_on_the_request_body():
    """No default tenant: a body without tenant_id is a 422, not an implicit
    fallback."""
    response = client.post(
        "/api/chat",
        headers={"X-User-Id": "anon-smoke"},
        json={"message": "hello"},
    )

    assert response.status_code == 422
