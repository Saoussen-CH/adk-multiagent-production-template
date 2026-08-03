"""An unauthenticated caller must not learn which tenants exist.

The finding these tests pin down
--------------------------------
`resolve_tenant_database` runs *before* authentication — it has to, because
auth tokens live in their tenant's own Firestore database, so the tenant must
be resolved before a token can be verified at all. It used to answer an
unknown tenant with `404 Unknown tenant_id: <id>`, while a *known* tenant
answered the identical credential-less request with 401. Diffing those two
statuses enumerated the platform's whole tenant roster, with no token, no
header and nothing to prove — and on `/api/auth/logout`, which carried no
rate limit at all, without any throttle either:

    chat     no-creds  unknown-tenant : 404   |  known-tenant : 401
    sessions no-creds  unknown-tenant : 404   |  known-tenant : 401
    logout   bad-token unknown-tenant : 404   |  known-tenant : 200
    login              unknown-tenant : 404   |  known-tenant : 401

Every case below is written as an *equality* between two responses — the same
request aimed at a known tenant and at one that does not exist — rather than
as "unknown tenant returns 401". That distinction matters: asserting a status
code would pass even if some future change made the known-tenant response
differ again, which is exactly the bug. Comparing the two responses cannot.

All of them fail against the pre-fix code (404 != the known-tenant answer).
"""

import os

# Must run before `backend.app.main` is imported — same preamble as
# tests/unit/test_backend_tenant_isolation.py.
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

KNOWN_TENANT = "test-tenant"  # seeded by tests/unit/conftest.py
UNKNOWN_TENANT = "not-a-real-tenant"

# Credential shapes an enumerator can present without holding an account.
NO_CREDENTIAL = {}
BAD_TOKEN = {"Authorization": "Bearer definitely-not-a-real-token"}
MALFORMED_HEADER = {"Authorization": "Basic not-even-bearer"}


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Both limiters keep module-level state, and `auth` allows a burst of
    only 3 requests per 10s — without this reset, the pairs of requests these
    tests send would bleed 429s into each other."""
    from backend.app.rate_limiter import rate_limiter
    from customer_support_mas import rate_limiting

    rate_limiting._buckets.clear()
    rate_limiter.reset()
    yield
    rate_limiting._buckets.clear()
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def _wire_account_stores(wire_backend_account_stores):
    return wire_backend_account_stores


@pytest.fixture(autouse=True)
def _never_call_the_real_agent(monkeypatch):
    async def _fake_query(user_id, agent_engine_session_id, message, tenant_id):
        return "ok", agent_engine_session_id or "agent-session-1", []

    monkeypatch.setattr(main_module.agent_client, "query_agent", _fake_query)


def _probe(tenant_id, headers):
    """Every endpoint the reviewer's probe script covered, as callables taking
    a tenant_id and returning a Response."""
    return {
        "chat": lambda: client.post(
            "/api/chat",
            headers=headers,
            json={"message": "hello", "tenant_id": tenant_id},
        ),
        "sessions_list": lambda: client.get(f"/api/sessions?tenant_id={tenant_id}", headers=headers),
        "sessions_messages": lambda: client.get(
            f"/api/sessions/some-session/messages?tenant_id={tenant_id}", headers=headers
        ),
        "sessions_delete": lambda: client.delete(f"/api/sessions/some-session?tenant_id={tenant_id}", headers=headers),
        "sessions_rename": lambda: client.put(
            f"/api/sessions/some-session/rename?tenant_id={tenant_id}",
            headers=headers,
            json={"session_name": "x"},
        ),
        "login": lambda: client.post(
            "/api/auth/login",
            headers=headers,
            json={"email": "nobody@example.com", "password": "whatever", "tenant_id": tenant_id},
        ),
    }


def _assert_indistinguishable(endpoint, headers):
    """The same request, twice: known tenant vs. tenant that does not exist."""
    known = _probe(KNOWN_TENANT, headers)[endpoint]()
    unknown = _probe(UNKNOWN_TENANT, headers)[endpoint]()

    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json()), (
        f"{endpoint} leaks tenant existence: "
        f"known={known.status_code} {known.text} vs unknown={unknown.status_code} {unknown.text}"
    )
    # Belt and braces: the id must not be echoed even inside a matching body.
    assert UNKNOWN_TENANT not in unknown.text


# =============================================================================
# (a) NO CREDENTIAL AT ALL — the headline case
# =============================================================================


@pytest.mark.parametrize(
    "endpoint",
    ["chat", "sessions_list", "sessions_messages", "sessions_delete", "sessions_rename", "login"],
)
def test_no_credential_gets_the_same_answer_for_any_tenant(endpoint):
    """A caller with no token, no header and nothing to prove learns nothing."""
    _assert_indistinguishable(endpoint, NO_CREDENTIAL)


# =============================================================================
# (b) A CREDENTIAL THAT PROVES NOTHING — invalid or malformed
#
# "Present a credential first" is not enough on its own: a bad token can be
# invented for free, so it must not buy tenant-existence information either.
# =============================================================================


@pytest.mark.parametrize(
    "endpoint",
    ["chat", "sessions_list", "sessions_messages", "sessions_delete", "sessions_rename"],
)
def test_an_invented_bearer_token_gets_the_same_answer_for_any_tenant(endpoint):
    _assert_indistinguishable(endpoint, BAD_TOKEN)


@pytest.mark.parametrize("endpoint", ["chat", "sessions_list"])
def test_a_malformed_authorization_header_gets_the_same_answer_for_any_tenant(endpoint):
    _assert_indistinguishable(endpoint, MALFORMED_HEADER)


def test_the_401_detail_matches_too_not_just_the_status(monkeypatch):
    """Returning a *generic* 401 for unknown tenants would have swapped one
    oracle for another: a known tenant answers a bogus token with "Invalid or
    expired token", so an unknown tenant must say precisely that and not
    "Authentication required"."""
    unknown = client.get(f"/api/sessions?tenant_id={UNKNOWN_TENANT}", headers=BAD_TOKEN)

    assert unknown.status_code == 401
    assert unknown.json() == {"detail": "Invalid or expired token"}


# =============================================================================
# (c) LOGOUT — the unthrottled case
# =============================================================================


def test_logout_reports_the_same_success_for_an_unknown_tenant():
    """Logout deliberately reports success for a token it cannot find, so
    that it cannot be used to probe which tenant a token belongs to. The
    unknown-tenant path has to keep that promise: pre-fix it was a 404 while
    every real tenant answered 200."""
    known = client.post(f"/api/auth/logout?tenant_id={KNOWN_TENANT}", headers=BAD_TOKEN)
    unknown = client.post(f"/api/auth/logout?tenant_id={UNKNOWN_TENANT}", headers=BAD_TOKEN)

    assert known.status_code == 200 and known.json() == {"status": "logged_out"}
    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())


def test_logout_rejects_a_malformed_header_identically_for_any_tenant():
    """The header is parsed before the tenant is resolved, so the 400 does not
    become an oracle in its own right."""
    known = client.post(f"/api/auth/logout?tenant_id={KNOWN_TENANT}", headers=MALFORMED_HEADER)
    unknown = client.post(f"/api/auth/logout?tenant_id={UNKNOWN_TENANT}", headers=MALFORMED_HEADER)

    assert known.status_code == 400
    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())


def test_logout_is_rate_limited():
    """Second half of the finding: /api/auth/logout had no rate limiting at
    all, so even a *throttled* oracle was not throttled there. It now shares
    the "auth" bucket with register/login (burst limit 3 per 10s)."""
    statuses = [
        client.post(f"/api/auth/logout?tenant_id={KNOWN_TENANT}", headers=BAD_TOKEN).status_code for _ in range(6)
    ]

    assert 429 in statuses, f"logout is not rate limited: {statuses}"


# =============================================================================
# (d) THE GUARD MUST NOT BREAK LEGITIMATE TRAFFIC
# =============================================================================


def test_a_real_token_still_works_and_a_real_logout_still_revokes():
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "oracle-probe@example.com",
            "name": "Probe",
            "password": "correct-horse-battery",
            "tenant_id": KNOWN_TENANT,
        },
    )
    assert registered.status_code == 200, registered.text
    token = registered.json()["token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/api/sessions?tenant_id={KNOWN_TENANT}", headers=auth_header).status_code == 200
    assert client.post(f"/api/auth/logout?tenant_id={KNOWN_TENANT}", headers=auth_header).status_code == 200
    # Genuinely revoked, not merely reported as such.
    assert client.get(f"/api/sessions?tenant_id={KNOWN_TENANT}", headers=auth_header).status_code == 401


# =============================================================================
# (e) RESIDUAL, KNOWN AND DELIBERATE
#
# One endpoint is *designed* to serve callers who have authenticated nothing,
# so a known tenant necessarily answers 200 where an unknown one cannot. No
# status code can hide that; only changing the auth model could. The test
# below asserts the residual explicitly so it is documented in executable
# form rather than only in a report — if someone closes it, this test fails
# and should be deleted along with the endpoint's comment.
#
# The *other* residual this section used to document — anonymous /api/chat
# accepting a bare X-User-Id as an unverified claim, so a known tenant
# answered 200 to it while an unknown one answered 401 — is now closed: chat
# no longer authenticates from that header at all, so a bare X-User-Id (with
# no Authorization header) is just another no-credential request and
# answers identically for any tenant. See
# test_no_client_asserted_identity.py for the direct 401 assertion.
# =============================================================================


def test_residual_registration_still_distinguishes_by_succeeding():
    """Registration must work for a real tenant without credentials, so 200
    (known) vs 401 (unknown) is irreducible here. What the fix removed is the
    *free* probe: enumerating now means creating a real account per guess —
    rate-limited, logged, and visible in the tenant's users collection."""
    body = {"email": "residual@example.com", "name": "R", "password": "correct-horse-battery"}

    known = client.post("/api/auth/register", json={**body, "tenant_id": KNOWN_TENANT})
    unknown = client.post("/api/auth/register", json={**body, "tenant_id": UNKNOWN_TENANT})

    assert known.status_code == 200
    assert unknown.status_code == 401
    # The id is at least no longer echoed back.
    assert UNKNOWN_TENANT not in unknown.text


def test_bare_x_user_id_no_longer_distinguishes_known_from_unknown_tenant():
    """This is the residual the section header above used to describe as
    permanent: a bare X-User-Id used to authenticate chat unconditionally, so
    a known tenant answered 200 to it while an unknown one answered 401 —
    itself a tenant-existence oracle. Task 2 closes it: the header is no
    longer trusted at all, so both answers are now the identical 401 no
    credential gets."""
    anon = {"X-User-Id": "anon-whatever-i-like"}

    known = client.post("/api/chat", headers=anon, json={"message": "hi", "tenant_id": KNOWN_TENANT})
    unknown = client.post("/api/chat", headers=anon, json={"message": "hi", "tenant_id": UNKNOWN_TENANT})

    assert known.status_code == 401
    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
