"""The backend's own account layer must be tenant-isolated, not just commerce.

Background — the bug these tests pin down
-----------------------------------------
The 11-task provider refactor made all *commerce* data (orders, products,
invoices, refunds) physically per-tenant: one Firestore database per tenant,
resolved through `get_provider(tenant_id)`. `backend/app/database.py`'s
`Database` — which owns `users`, `sessions`, `tokens` and each session's
`messages` — was never brought along. It ran against ONE hardcoded database
(`customer-support-db`, built once at import in `main.py`) and none of its
documents carried a `tenant_id` at all. Concretely, before this fix:

  1. alice@example.com signing up with Merchant A and alice@example.com
     signing up with Merchant B collided into the same `users` document.
     `create_user`'s duplicate-email check spans all merchants, so the second
     one got "An account with this email already exists" — and if it had not,
     both would have shared one account, one password and one order history.
  2. `/api/chat`'s continuity check compared only
     `session["user_id"] != actual_user_id`; it never looked at the tenant.
     Combined with `agent_client` only writing `tenant_id` into the Agent
     Engine session's state at *creation* and never re-passing it (by
     design), a session created under Merchant A and resumed with
     `tenant_id: "merchant-b"` kept serving Merchant A's data — while
     spending Merchant B's rate-limit budget and audit trail.
  3. GET/DELETE session endpoints had no `tenant_id` at all, so one
     merchant's session list, message history and deletes were reachable
     from any merchant's context.
  4. Auth tokens lived in that same shared `tokens` collection, so a token
     minted for Merchant A authenticated requests naming Merchant B.

The fix puts the account layer in the same physical database as that
tenant's commerce data (`customer_support_mas.tenancy.config.account_database`),
resolved through the same `load_tenant_config` / `get_db_client` machinery.
Every test below fails against the pre-fix code — see the per-test notes.
"""

import os

# Must run before `backend.app.main` is imported — see the same preamble in
# tests/unit/test_admin_refund_endpoints.py for why.
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

TENANT_A = "test-tenant"  # seeded by tests/unit/conftest.py, database "test-tenant-db"
TENANT_B = "tenant-b"  # seeded by the `tenant_b` fixture below, database "tenant-b-db"

PASSWORD = "correct-horse-battery"


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Both limiters keep module-level state. `auth` allows a burst of only 3
    requests per 10s, and several tests below register/login more than that,
    so without this reset later tests get spurious 429s."""
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


@pytest.fixture
def tenant_b(mock_db, mock_db_factory, tenant_databases):
    """A second tenant, in the same pool, with its own Firestore database.

    Returns that database's client. This is the whole point of the suite: if
    the two tenants shared one store, every isolation assertion below would
    pass vacuously.
    """
    from customer_support_mas.tenancy import config as config_module

    db_b = mock_db_factory("tenant-b-db")
    tenant_databases["tenant-b-db"] = db_b

    # The control-plane database holds routing config for both tenants.
    mock_db.collection("tenants").document(TENANT_B).set(
        {
            "tenant_id": TENANT_B,
            "tier": "light",
            "provider_type": "firestore",
            "provider_config": {"database_id": "tenant-b-db"},
            "pool_id": "test-pool",
            "refund_policy_ref": TENANT_B,
        }
    )
    config_module.invalidate_tenant_config_cache()
    yield db_b
    config_module.invalidate_tenant_config_cache()


@pytest.fixture(autouse=True)
def _wire_account_stores(wire_backend_account_stores):
    return wire_backend_account_stores


@pytest.fixture(autouse=True)
def _never_call_the_real_agent(monkeypatch):
    """Every chat below is about routing, not about the agent's answer."""

    async def _fake_query(user_id, agent_engine_session_id, message, tenant_id):
        return f"reply for {tenant_id}", agent_engine_session_id or f"agent-session-{tenant_id}", []

    monkeypatch.setattr(main_module.agent_client, "query_agent", _fake_query)


# =============================================================================
# HELPERS
# =============================================================================


def _register(tenant_id, email, name="Alice", password=PASSWORD):
    return client.post(
        "/api/auth/register",
        json={"email": email, "name": name, "password": password, "tenant_id": tenant_id},
    )


def _login(tenant_id, email, password=PASSWORD):
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": password, "tenant_id": tenant_id},
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _authed_token(db_client, tenant_id, user_id=None):
    """Mint a real token in `db_client`'s database for `tenant_id`, the way
    /api/auth/anonymous or /api/auth/register do — X-User-Id is no longer a
    trusted credential, so every test needing an authenticated caller now
    needs a real token, scoped to the exact tenant database it must verify
    against (database_id follows this suite's own `f"{tenant_id}-db"`
    convention — see `tenant_databases`/`tenant_b` above).

    Pass `user_id` to mint a token for a specific, already-known identity
    (e.g. one planted directly into a mock database); omit it to create a
    fresh anonymous user and return their token, the common case.
    """
    from backend.app.database import Database

    db = Database(project_id="test-project", database_id=f"{tenant_id}-db", tenant_id=tenant_id, client=db_client)
    if user_id is not None:
        return db.create_token(user_id)
    _, token = db.create_anonymous_user()
    return token


def _chat(tenant_id, message="hello", session_id=None, token=None):
    body = {"message": message, "tenant_id": tenant_id}
    if session_id:
        body["session_id"] = session_id
    return client.post("/api/chat", headers=_auth(token), json=body)


def _start_session(tenant_id, token):
    """Run one chat turn and return the internal session_id it created."""
    response = _chat(tenant_id, token=token)
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


# =============================================================================
# (a) TWO TENANTS, ONE EMAIL — two accounts, not a collision
# =============================================================================


def test_same_email_registers_independently_under_two_tenants(mock_db, tenant_b):
    """Pre-fix: the second register() hit `create_user`'s duplicate-email
    check against the single shared `users` collection and came back 400
    "An account with this email already exists." Merchant B could not sign up
    a customer Merchant A already had."""
    first = _register(TENANT_A, "alice@example.com")
    second = _register(TENANT_B, "alice@example.com")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["user_id"] != second.json()["user_id"]


def test_the_two_accounts_live_in_different_databases(mock_db, tenant_b):
    """The isolation is physical, not a filter: the documents are in
    different Firestore databases, each stamped with its own tenant_id."""
    id_a = _register(TENANT_A, "alice@example.com").json()["user_id"]
    id_b = _register(TENANT_B, "alice@example.com").json()["user_id"]

    doc_a = mock_db.collection("users").document(id_a).get()
    doc_b = tenant_b.collection("users").document(id_b).get()

    assert doc_a.exists and doc_b.exists
    assert doc_a.to_dict()["tenant_id"] == TENANT_A
    assert doc_b.to_dict()["tenant_id"] == TENANT_B
    # Neither database can see the other's account.
    assert not mock_db.collection("users").document(id_b).get().exists
    assert not tenant_b.collection("users").document(id_a).get().exists


def test_credentials_do_not_carry_across_tenants(mock_db, tenant_b):
    """Registering with Merchant A must not let you log in to Merchant B.

    Pre-fix both merchants shared one `users` collection, so this login
    succeeded and handed the caller a working session with the wrong
    merchant.
    """
    _register(TENANT_A, "alice@example.com")

    response = _login(TENANT_B, "alice@example.com")

    assert response.status_code == 401
    # Generic message: it must not confirm the address exists elsewhere.
    assert response.json()["detail"] == "Invalid email or password"


def test_anonymous_users_are_created_in_their_own_tenants_database(mock_db, tenant_b):
    a = client.post("/api/auth/anonymous", json={"tenant_id": TENANT_A})
    b = client.post("/api/auth/anonymous", json={"tenant_id": TENANT_B})

    assert a.status_code == 200 and b.status_code == 200
    assert mock_db.collection("users").document(a.json()["user_id"]).get().exists
    assert tenant_b.collection("users").document(b.json()["user_id"]).get().exists
    assert not tenant_b.collection("users").document(a.json()["user_id"]).get().exists


def test_anonymous_requires_a_tenant_id():
    """This endpoint used to take no body at all. There is no default
    tenant, so a body-less call is now a 422."""
    assert client.post("/api/auth/anonymous").status_code == 422


@pytest.mark.parametrize("payload_key", ["email", "tenant_id"])
def test_register_and_login_require_a_tenant_id(payload_key):
    """tenant_id is as required as email is — no implicit fallback."""
    body = {"email": "bob@example.com", "name": "Bob", "password": PASSWORD, "tenant_id": TENANT_A}
    body.pop(payload_key)
    assert client.post("/api/auth/register", json=body).status_code == 422


# =============================================================================
# (b) A SESSION IS NOT REACHABLE FROM ANOTHER TENANT
# =============================================================================


def test_session_cannot_be_continued_by_a_request_claiming_another_tenant(mock_db, tenant_b):
    """The headline regression.

    Pre-fix, `/api/chat`'s continuity check was only
    `session["user_id"] != actual_user_id` — the tenant was never compared.
    Because agent_client writes tenant_id into Agent Engine session state
    only at creation and never re-passes it, the resumed conversation would
    have kept running against Merchant A's data under Merchant B's name.
    """
    token_a = _authed_token(mock_db, TENANT_A)
    session_id = _start_session(TENANT_A, token=token_a)

    # The caller here is a genuinely authenticated Tenant B user (their own
    # real token) — not the Tenant A owner — attempting to continue Tenant
    # A's session while claiming tenant_id=B. Without a real Tenant B
    # credential the request would 401 before ever reaching the session
    # check, which would test authentication rather than isolation.
    token_b = _authed_token(tenant_b, TENANT_B)
    response = _chat(TENANT_B, message="and what about my order?", session_id=session_id, token=token_b)

    # 404 rather than 403: confirming the id exists under another tenant
    # would itself be a cross-tenant disclosure.
    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def test_continuing_under_the_owning_tenant_still_works(mock_db, tenant_b):
    """The guard must not break legitimate continuation."""
    token_a = _authed_token(mock_db, TENANT_A)
    session_id = _start_session(TENANT_A, token=token_a)

    response = _chat(TENANT_A, message="follow-up", session_id=session_id, token=token_a)

    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == session_id


def test_session_messages_are_not_readable_from_another_tenant(mock_db, tenant_b):
    """Pre-fix this endpoint had no tenant_id at all and read the one shared
    database, so any caller who knew a session id could read its transcript
    from any tenant context."""
    token_a = _authed_token(mock_db, TENANT_A)
    session_id = _start_session(TENANT_A, token=token_a)

    ours = client.get(f"/api/sessions/{session_id}/messages?tenant_id={TENANT_A}", headers=_auth(token_a))
    token_b = _authed_token(tenant_b, TENANT_B)
    theirs = client.get(f"/api/sessions/{session_id}/messages?tenant_id={TENANT_B}", headers=_auth(token_b))

    assert ours.status_code == 200
    assert [m["role"] for m in ours.json()["messages"]] == ["user", "assistant"]
    assert theirs.status_code == 404


def test_session_cannot_be_deleted_from_another_tenant(mock_db, tenant_b):
    token_a = _authed_token(mock_db, TENANT_A)
    session_id = _start_session(TENANT_A, token=token_a)

    token_b = _authed_token(tenant_b, TENANT_B)
    response = client.delete(f"/api/sessions/{session_id}?tenant_id={TENANT_B}", headers=_auth(token_b))

    assert response.status_code == 404
    # And it is genuinely untouched, not merely reported as missing.
    assert mock_db.collection("sessions").document(session_id).get().to_dict()["is_active"] is True


def test_session_cannot_be_renamed_from_another_tenant(mock_db, tenant_b):
    token_a = _authed_token(mock_db, TENANT_A)
    session_id = _start_session(TENANT_A, token=token_a)

    token_b = _authed_token(tenant_b, TENANT_B)
    response = client.put(
        f"/api/sessions/{session_id}/rename?tenant_id={TENANT_B}",
        headers=_auth(token_b),
        json={"session_name": "pwned"},
    )

    assert response.status_code == 404
    assert mock_db.collection("sessions").document(session_id).get().to_dict()["session_name"] != "pwned"


def test_session_list_never_crosses_tenants(mock_db, tenant_b):
    """Two merchants, two independent authenticated users: each context sees
    only its own sessions."""
    token_a = _authed_token(mock_db, TENANT_A)
    token_b = _authed_token(tenant_b, TENANT_B)
    session_a = _start_session(TENANT_A, token=token_a)
    session_b = _start_session(TENANT_B, token=token_b)

    listed_a = client.get(f"/api/sessions?tenant_id={TENANT_A}", headers=_auth(token_a)).json()["sessions"]
    listed_b = client.get(f"/api/sessions?tenant_id={TENANT_B}", headers=_auth(token_b)).json()["sessions"]

    assert [s["session_id"] for s in listed_a] == [session_a]
    assert [s["session_id"] for s in listed_b] == [session_b]


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/sessions"),
        ("get", "/api/sessions/abc/messages"),
        ("delete", "/api/sessions/abc"),
    ],
)
def test_session_endpoints_require_a_tenant_id(method, path):
    """No default tenant: the query parameter is required, not optional —
    this 422 fires on FastAPI's own parameter validation before any
    authentication dependency runs, so no credentials are needed here."""
    response = getattr(client, method)(path)
    assert response.status_code == 422


def test_stored_session_tenant_is_checked_even_within_one_database(mock_db, tenant_b):
    """Defence in depth behind the physical split.

    The primary guard is that tenant B's database simply has no such
    session. This test removes that guard — it plants a session stamped
    `tenant_id: TENANT_B` directly inside tenant A's OWN database — to prove
    the explicit comparison in `/api/chat` and `Database.get_session` is real
    and not dead code riding on the database boundary. This is the case that
    would matter if the account layer ever moved back to a shared database.
    """
    mock_db.collection("sessions").document("planted-session").set(
        {
            "session_id": "planted-session",
            "tenant_id": TENANT_B,  # belongs to B, sitting in A's database
            "user_id": "anon-shopper",
            "agent_engine_session_id": "agent-session-x",
            "session_name": "planted",
            "is_active": True,
        }
    )

    # A real token, minted for the exact user_id planted above, so the
    # user_id check passes and the tenant-mismatch check below it is the one
    # actually exercised — that ordering is the point of this test.
    token = _authed_token(mock_db, TENANT_A, user_id="anon-shopper")
    response = _chat(TENANT_A, session_id="planted-session", token=token)

    assert response.status_code == 404


# =============================================================================
# (c) TOKENS ARE TENANT-SCOPED — the resolve-tenant-before-auth ordering
# =============================================================================


def test_a_token_issued_by_one_tenant_does_not_authenticate_against_another(mock_db, tenant_b):
    """Pre-fix, `verify_token` read one shared `tokens` collection, so a
    token minted by Merchant A authenticated a request naming Merchant B."""
    token = _register(TENANT_A, "alice@example.com").json()["token"]

    ours = client.get(f"/api/sessions?tenant_id={TENANT_A}", headers={"Authorization": f"Bearer {token}"})
    theirs = client.get(f"/api/sessions?tenant_id={TENANT_B}", headers={"Authorization": f"Bearer {token}"})

    assert ours.status_code == 200
    assert theirs.status_code == 401


def test_a_token_is_verified_against_the_tenant_named_in_the_chat_body(mock_db, tenant_b):
    """/api/chat is the one endpoint whose tenant_id arrives in the body
    rather than the query string, so it authenticates inline instead of via
    the `get_current_user` dependency. Same guarantee either way."""
    token = _register(TENANT_A, "alice@example.com").json()["token"]

    ours = _chat(TENANT_A, token=token)
    theirs = _chat(TENANT_B, token=token)

    assert ours.status_code == 200, ours.text
    assert theirs.status_code == 401


def test_logout_only_revokes_within_its_own_tenant(mock_db, tenant_b):
    token = _register(TENANT_A, "alice@example.com").json()["token"]

    # A logout aimed at the wrong tenant reports success (it must not be
    # usable to probe which tenant a token belongs to) but revokes nothing.
    wrong = client.post(f"/api/auth/logout?tenant_id={TENANT_B}", headers={"Authorization": f"Bearer {token}"})
    assert wrong.status_code == 200
    assert (
        client.get(f"/api/sessions?tenant_id={TENANT_A}", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )

    right = client.post(f"/api/auth/logout?tenant_id={TENANT_A}", headers={"Authorization": f"Bearer {token}"})
    assert right.status_code == 200
    assert (
        client.get(f"/api/sessions?tenant_id={TENANT_A}", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_tokens_are_stamped_and_stored_in_their_own_tenants_database(mock_db, tenant_b):
    token = _register(TENANT_A, "alice@example.com").json()["token"]

    doc = mock_db.collection("tokens").document(token).get()
    assert doc.exists
    assert doc.to_dict()["tenant_id"] == TENANT_A
    assert not tenant_b.collection("tokens").document(token).get().exists


# =============================================================================
# CONFIG-LEVEL: account stores are unique per tenant too
# =============================================================================


def test_two_tenants_may_not_share_one_account_database():
    """Light-tier isolation is per-database. Two tenants pointing their
    account stores at one database would collide their user accounts exactly
    as two tenants sharing a commerce database collide their orders — so the
    same uniqueness rule covers both."""
    from customer_support_mas.tenancy.config import TenantConfig, TenantConfigConflictError, assert_unique_datastores

    def _shopify_tenant(tenant_id, accounts_db):
        return TenantConfig(
            tenant_id=tenant_id,
            tier="light",
            provider_type="shopify",
            provider_config={"shop_domain": f"{tenant_id}.myshopify.com"},
            pool_id="test-pool",
            account_database_id=accounts_db,
        )

    # Distinct account databases are fine.
    assert_unique_datastores([_shopify_tenant("a", "a-accounts"), _shopify_tenant("b", "b-accounts")])

    with pytest.raises(TenantConfigConflictError):
        assert_unique_datastores([_shopify_tenant("a", "shared"), _shopify_tenant("b", "shared")])


def test_a_tenant_with_no_account_database_is_a_503_not_a_shared_fallback(mock_db):
    """A provider with no Firestore database of its own and no explicit
    `account_database_id` has nowhere to put accounts. That is an explicit
    misconfiguration — never a silent fall back to a shared default, which
    is the failure mode this whole change exists to remove."""
    from customer_support_mas.tenancy import config as config_module

    mock_db.collection("tenants").document("storeless").set(
        {
            "tenant_id": "storeless",
            "tier": "light",
            "provider_type": "shopify",
            "provider_config": {"shop_domain": "mock.myshopify.com"},
            "pool_id": "test-pool",
        }
    )
    config_module.invalidate_tenant_config_cache()

    response = _chat("storeless")

    config_module.invalidate_tenant_config_cache()
    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable for this tenant"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
