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


@pytest.fixture(autouse=True)
def _wire_account_stores(wire_backend_account_stores):
    """Point the backend's per-tenant account stores at in-memory mocks.

    /api/chat writes its session and messages to the tenant's OWN database
    now (there is no module-level `main.db` any more), so without this the
    happy-path test would try to reach live Firestore.
    """
    return wire_backend_account_stores


def _chat(tenant_id, message="hello", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/api/chat",
        headers=headers,
        json={"message": message, "tenant_id": tenant_id},
    )


def _authed_token(mock_db, tenant_id="test-tenant"):
    """Mint a real anonymous token in `mock_db` for `tenant_id`, the way
    /api/auth/anonymous does — X-User-Id is no longer a trusted credential,
    so any test that needs an authenticated caller now needs one of these."""
    from backend.app.database import Database

    db = Database(project_id="test-project", database_id=f"{tenant_id}-db", tenant_id=tenant_id, client=mock_db)
    _, token = db.create_anonymous_user()
    return token


def test_unknown_tenant_is_rejected_before_the_agent_is_ever_queried(monkeypatch):
    """The rejection is now a 401 that does not name the tenant.

    It used to be `404 Unknown tenant_id: <id>`, which — running as it does
    ahead of authentication — told an entirely unauthenticated caller which
    tenant ids exist. The rejection itself is unchanged and is what this test
    is really about; the *shape* of it is pinned down in
    tests/unit/test_tenant_existence_oracle.py.
    """
    called = {}

    def _should_not_run(*args, **kwargs):
        called["agent"] = True
        raise AssertionError("the agent must never be queried for an unknown tenant")

    monkeypatch.setattr(main_module.agent_client, "query_agent", _should_not_run)

    response = _chat(UNKNOWN_TENANT)

    assert response.status_code == 401
    assert UNKNOWN_TENANT not in response.text
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


def test_known_tenant_still_reaches_the_agent(monkeypatch, mock_db):
    """The guard must not reject legitimate traffic.

    The session and its messages are written through the real
    `Database` bound to the tenant's own (mocked) database — no
    `create_session`/`save_message` stubs — so this also pins down that the
    tenant account store is reachable end to end.
    """

    async def _fake_query(user_id, agent_engine_session_id, message, tenant_id):
        assert tenant_id == KNOWN_TENANT
        return "hello back", "agent-session-1", []

    monkeypatch.setattr(main_module.agent_client, "query_agent", _fake_query)

    token = _authed_token(mock_db, tenant_id=KNOWN_TENANT)
    response = _chat(KNOWN_TENANT, token=token)

    assert response.status_code == 200, response.text
    assert response.json()["response"] == "hello back"

    # The session landed in the TENANT's database, stamped with its tenant_id.
    session_id = response.json()["session_id"]
    session = mock_db.collection("sessions").document(session_id).get().to_dict()
    assert session["tenant_id"] == KNOWN_TENANT


def test_tenant_config_conflict_is_a_503_that_leaks_nothing(monkeypatch):
    """A misconfiguration must not echo the conflict message to the caller.

    TenantConfigConflictError's message names BOTH colliding tenant ids and
    the shared database name. It used to fall through to the generic
    `except Exception` handler, which returns
    `f"Error processing request: {e}"` — i.e. the whole message, verbatim, in
    a 500 body.
    """
    from customer_support_mas.tenancy.config import TenantConfigConflictError

    def _conflict(tenant_id):
        raise TenantConfigConflictError(
            f"Tenants 'victim-tenant' and {tenant_id!r} both resolve to database "
            "'shared-secret-db' in scope 'test-pool'"
        )

    monkeypatch.setattr(main_module, "load_tenant_config", _conflict)

    response = _chat(KNOWN_TENANT)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable for this tenant"}
    assert "victim-tenant" not in response.text
    assert "shared-secret-db" not in response.text


def test_refund_store_tenant_config_conflict_is_a_503_that_leaks_nothing(monkeypatch):
    """Same finding, second call site: resolve_refund_request_store."""
    from fastapi import HTTPException

    from customer_support_mas.tenancy.config import TenantConfigConflictError

    def _conflict(tenant_id):
        raise TenantConfigConflictError(
            f"Tenants 'victim-tenant' and {tenant_id!r} both resolve to database 'shared-secret-db'"
        )

    monkeypatch.setattr(main_module, "get_provider", _conflict)

    with pytest.raises(HTTPException) as exc_info:
        main_module.resolve_refund_request_store(KNOWN_TENANT)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Service temporarily unavailable for this tenant"
    assert "victim-tenant" not in str(exc_info.value.detail)
    assert "shared-secret-db" not in str(exc_info.value.detail)


def test_tenant_id_is_required_on_the_request_body():
    """No default tenant: a body without tenant_id is a 422, not an implicit
    fallback. Pydantic body validation happens before authentication, so this
    is unaffected by whether a token is presented — no header needed here."""
    response = client.post(
        "/api/chat",
        json={"message": "hello"},
    )

    assert response.status_code == 422
