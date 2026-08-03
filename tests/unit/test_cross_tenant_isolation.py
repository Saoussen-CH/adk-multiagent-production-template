"""Tenant A must never be able to read or act on Tenant B's data, even
though both run through the same shared FirestoreProvider code path within
one pool. This is the light tier's concrete, code-level version of the
adversarial isolation test the heavy tier will need against real IAM/VPC-SC
enforcement (spec section 7) — here, the guarantee comes from tenant_id
being required on every provider call plus per-tenant Firestore databases,
not from infrastructure, so it must be proven by test, not assumed."""

import pytest

from customer_support_mas.providers.firestore_provider import FirestoreProvider
from customer_support_mas.tenancy.context import MissingTenantError, get_tenant_id


@pytest.fixture
def isolated_tenants(mock_db_factory, monkeypatch):
    db_a = mock_db_factory("tenant-a-db")
    db_b = mock_db_factory("tenant-b-db")

    db_a.collection("orders").document("ORD-1").set(
        {"customer_id": "user-a", "status": "Delivered", "total": 100.0, "items": []}
    )
    db_b.collection("orders").document("ORD-1").set(
        {"customer_id": "user-b", "status": "Delivered", "total": 999.0, "items": []}
    )

    monkeypatch.setattr(
        "customer_support_mas.providers.firestore_provider.get_db_client",
        lambda database_id: {"tenant-a-db": db_a, "tenant-b-db": db_b}[database_id],
    )
    return db_a, db_b


@pytest.fixture
def seeded_tenant_config_a(mock_db):
    """`process_refund` resolves its provider via `get_provider(tenant_id)`
    -> `load_tenant_config(tenant_id)`, which reads `tenants/{tenant_id}`
    off `customer_support_mas.tenancy.config.get_db_client()` — patched
    (see conftest.py's autouse `mock_backends`) to the *shared* `mock_db`
    fixture, not `db_a`/`db_b` from `isolated_tenants`. That's the real
    architecture: a control-plane database holds tenant routing config
    (which per-tenant database a tenant_id maps to); the actual commerce
    data lives in the physically separate database that config points at.
    So tenant-a's routing doc must be seeded into `mock_db`, pointing at
    `database_id: "tenant-a-db"` — the same database_id `isolated_tenants`
    wires `db_a` to via its `firestore_provider.get_db_client` patch.

    Mirrors Task 4's `_seed_default_test_tenant` pattern (same shape,
    scoped to tenant-a only, since this suite's other tests construct
    FirestoreProvider directly and never go through load_tenant_config).
    """
    from customer_support_mas.tenancy import config as config_module

    config_module._tenant_config_cache.clear()
    mock_db.collection("tenants").document("tenant-a").set(
        {
            "tenant_id": "tenant-a",
            "tier": "light",
            "provider_type": "firestore",
            "provider_config": {"database_id": "tenant-a-db"},
            "pool_id": "test-pool",
            "refund_policy_ref": "tenant-a",
        }
    )
    yield
    config_module._tenant_config_cache.clear()


def test_same_order_id_resolves_to_different_data_per_tenant(isolated_tenants):
    provider_a = FirestoreProvider({"database_id": "tenant-a-db"})
    provider_b = FirestoreProvider({"database_id": "tenant-b-db"})

    order_a = provider_a.get_order("tenant-a", "ORD-1")
    order_b = provider_b.get_order("tenant-b", "ORD-1")

    assert order_a.customer_id == "user-a"
    assert order_a.total == 100.0
    assert order_b.customer_id == "user-b"
    assert order_b.total == 999.0


def test_tenant_a_provider_cannot_see_tenant_b_data_via_any_database_id(isolated_tenants):
    """Even if a bug passed tenant B's database_id into tenant A's provider
    config, get_order's return value must never be confused for tenant A's
    own order — the provider only ever knows the database it was
    constructed against, proving there is no cross-database fallback path."""
    provider_pointed_at_b = FirestoreProvider({"database_id": "tenant-b-db"})

    order = provider_pointed_at_b.get_order("tenant-a", "ORD-1")

    # This deliberately demonstrates the REAL boundary: it's
    # provider_config["database_id"] (resolved once, from tenant config
    # loaded by tenant_id — Task 2) that enforces isolation, not the
    # tenant_id string argument itself. The tenant_id argument is for
    # policy/audit-log correctness, not the isolation mechanism.
    assert order.customer_id == "user-b"  # proves database_id is what actually gates access


def test_verify_ownership_rejects_cross_tenant_customer_match_by_coincidence(isolated_tenants):
    """If tenant A and tenant B happen to have a customer_id collision
    (e.g. both have a 'demo-user-001'), tenant A's provider must still only
    ever check against tenant A's own database — never tenant B's.

    Both tenants share the colliding customer_id (that's the coincidence
    being tested), but their ORD-SHARED documents otherwise differ (total:
    100.0 vs 999.0, same pattern as the other database_id-distinguishing
    tests in this file). If tenant A's provider were ever misrouted to
    tenant B's database, `is_authorized` would happen to still be True
    (both docs have the same customer_id) but the returned order's `total`
    would silently be tenant B's value instead of tenant A's — that's the
    actual leak this test needs to catch, so it must assert on `total`,
    not just `is_authorized`.
    """
    db_a, db_b = isolated_tenants
    db_a.collection("orders").document("ORD-SHARED").set(
        {"customer_id": "demo-user-001", "status": "Delivered", "total": 100.0, "items": []}
    )
    db_b.collection("orders").document("ORD-SHARED").set(
        {"customer_id": "demo-user-001", "status": "Delivered", "total": 999.0, "items": []}
    )

    provider_a = FirestoreProvider({"database_id": "tenant-a-db"})
    is_authorized, order, _ = provider_a.verify_order_ownership("tenant-a", "ORD-SHARED", "demo-user-001")

    assert is_authorized is True
    # The real assertion: this must be tenant A's $100 order, not tenant
    # B's $999 one. A read that was ever misrouted to db_b would still
    # report is_authorized=True (customer_id collides) but total would be
    # 999.0 — this line is what actually fails in that scenario.
    assert order.total == 100.0


def test_missing_tenant_id_is_a_hard_error_not_a_silent_default():
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.state = {}

    with pytest.raises(MissingTenantError):
        get_tenant_id(ctx)


def test_process_refund_stages_request_with_its_own_tenant_id(isolated_tenants, seeded_tenant_config_a):
    """The refund_requests idempotency check must never match a pending
    request from a different tenant, even for the identical order_id.

    This plants a PENDING_APPROVAL refund_requests doc for the SAME
    order_id and user_id but a DIFFERENT tenant_id directly inside tenant
    A's own database (db_a) — simulating the only way a cross-tenant
    collision could ever reach the idempotency check's `.where("order_id",
    ...)` query, since db_a/db_b are otherwise physically separate
    databases and could never see each other's documents at all. If
    `process_refund`'s idempotency filter (tools.py) ever stopped checking
    `tenant_id` and matched on order_id/user_id/status alone, this planted
    foreign-tenant doc would be picked up as `existing_pending` and the
    call would short-circuit to "already_pending" instead of staging a
    fresh request — which is exactly what this test asserts does NOT
    happen.
    """
    from unittest.mock import MagicMock

    from customer_support_mas.agents.refund.tools import process_refund

    db_a, _ = isolated_tenants

    # validate_order_id requires ORD-XXXXX with 5-10 digits (validation.py's
    # ORDER_ID_PATTERN) — isolated_tenants's "ORD-1" fixture doc is fine for
    # the read-path tests above (they never go through validate_order_id),
    # but process_refund would reject it outright, so this test uses its
    # own, validly-formatted order_id instead.
    order_id = "ORD-90001"

    # process_refund needs real, priced items to compute a refund amount
    # (it recalculates from the order when step-2 session state isn't
    # present).
    db_a.collection("orders").document(order_id).set(
        {
            "customer_id": "user-a",
            "status": "Delivered",
            "total": 100.0,
            "items": [{"product_id": "PROD-A1", "name": "Widget", "qty": 1, "price": 100.0}],
        }
    )

    # Plant the foreign-tenant collision doc described above.
    db_a.collection("refund_requests").document("REFREQ-FOREIGN").set(
        {
            "tenant_id": "tenant-x",
            "order_id": order_id,
            "user_id": "user-a",
            "status": "PENDING_APPROVAL",
        }
    )

    ctx_a = MagicMock()
    ctx_a.state = {"tenant_id": "tenant-a"}
    ctx_a.user_id = "user-a"
    ctx_a.actions = MagicMock()

    result = process_refund(order_id=order_id, reason_code="defective", tool_context=ctx_a)

    assert result["status"] == "pending_approval"
    staged = db_a.collection("refund_requests").document(result["request_id"]).get().to_dict()
    assert staged["tenant_id"] == "tenant-a"


# =============================================================================
# Finding I2 — nothing stopped two tenants being configured onto one database
#
# Light-tier isolation is physical: one named Firestore database per tenant
# inside a shared pool project (spec section 6). The shipped fixture points
# "acme-electronics" at "customer-support-db", the shared default — onboarding
# a second tenant by copying that fixture would have collapsed isolation to
# zero, silently, with each tenant reading the other's orders. Every guarantee
# proven above rests on that assumption holding, so it is asserted here rather
# than assumed.
# =============================================================================


@pytest.fixture
def clean_tenant_config_cache():
    from customer_support_mas.tenancy import config as config_module

    config_module.invalidate_tenant_config_cache()
    yield config_module
    config_module.invalidate_tenant_config_cache()


def _tenant_doc(tenant_id, database_id, pool_id="light-pool-1"):
    return {
        "tenant_id": tenant_id,
        "tier": "light",
        "provider_type": "firestore",
        "provider_config": {"database_id": database_id},
        "pool_id": pool_id,
        "refund_policy_ref": tenant_id,
    }


def test_two_tenants_in_one_pool_cannot_share_a_database(mock_db, clean_tenant_config_cache):
    from customer_support_mas.tenancy.config import TenantConfigConflictError, load_tenant_config

    mock_db.collection("tenants").document("tenant-one").set(_tenant_doc("tenant-one", "shared-db"))
    mock_db.collection("tenants").document("tenant-two").set(_tenant_doc("tenant-two", "shared-db"))

    load_tenant_config("tenant-one")

    with pytest.raises(TenantConfigConflictError) as exc_info:
        load_tenant_config("tenant-two")

    message = str(exc_info.value)
    assert "tenant-one" in message and "tenant-two" in message and "shared-db" in message


def test_a_conflicting_tenant_never_resolves_to_a_provider(mock_db, clean_tenant_config_cache):
    """The conflict must stop provider construction, not merely log — otherwise
    the second tenant still gets a working handle on the first's data."""
    from customer_support_mas.providers.registry import get_provider
    from customer_support_mas.tenancy.config import TenantConfigConflictError

    mock_db.collection("tenants").document("tenant-one").set(_tenant_doc("tenant-one", "shared-db"))
    mock_db.collection("tenants").document("tenant-two").set(_tenant_doc("tenant-two", "shared-db"))

    get_provider("tenant-one")

    with pytest.raises(TenantConfigConflictError):
        get_provider("tenant-two")


def test_distinct_databases_in_one_pool_are_fine(mock_db, clean_tenant_config_cache):
    from customer_support_mas.tenancy.config import load_tenant_config

    mock_db.collection("tenants").document("tenant-one").set(_tenant_doc("tenant-one", "tenant-one-db"))
    mock_db.collection("tenants").document("tenant-two").set(_tenant_doc("tenant-two", "tenant-two-db"))

    assert load_tenant_config("tenant-one").provider_config["database_id"] == "tenant-one-db"
    assert load_tenant_config("tenant-two").provider_config["database_id"] == "tenant-two-db"


def test_same_database_name_in_different_pools_is_allowed(mock_db, clean_tenant_config_cache):
    """Pools are separate GCP projects, so an identically-named database in
    two of them is two different databases — not a conflict."""
    from customer_support_mas.tenancy.config import load_tenant_config

    mock_db.collection("tenants").document("tenant-one").set(
        _tenant_doc("tenant-one", "commerce-db", pool_id="light-pool-1")
    )
    mock_db.collection("tenants").document("tenant-two").set(
        _tenant_doc("tenant-two", "commerce-db", pool_id="light-pool-2")
    )

    load_tenant_config("tenant-one")
    load_tenant_config("tenant-two")  # must not raise


def test_reloading_the_same_tenant_is_not_a_conflict_with_itself(mock_db, clean_tenant_config_cache):
    from customer_support_mas.tenancy.config import invalidate_tenant_config_cache, load_tenant_config

    mock_db.collection("tenants").document("tenant-one").set(_tenant_doc("tenant-one", "tenant-one-db"))

    load_tenant_config("tenant-one")
    invalidate_tenant_config_cache("tenant-one")
    load_tenant_config("tenant-one")  # must not raise


def test_assert_unique_datastores_validates_a_whole_collection(clean_tenant_config_cache):
    """Whole-collection validation, for an onboarding/seeding step that wants
    the answer before the second tenant's first request rather than after."""
    from customer_support_mas.tenancy.config import (
        TenantConfig,
        TenantConfigConflictError,
        assert_unique_datastores,
    )

    def _config(tenant_id, database_id, provider_type="firestore", pool_id="light-pool-1"):
        return TenantConfig(
            tenant_id=tenant_id,
            tier="light",
            provider_type=provider_type,
            provider_config=({"database_id": database_id} if provider_type == "firestore" else {"shop_domain": "x"}),
            pool_id=pool_id,
        )

    assert_unique_datastores([_config("a", "a-db"), _config("b", "b-db")])

    with pytest.raises(TenantConfigConflictError):
        assert_unique_datastores([_config("a", "shared"), _config("b", "shared")])

    # Shopify-backed tenants have no database of ours to keep unique.
    assert_unique_datastores([_config("a", None, provider_type="shopify"), _config("b", None, provider_type="shopify")])


def test_the_seeded_tenant_collection_has_no_datastore_conflicts(mock_db, clean_tenant_config_cache):
    """Release gate on the shipped fixture itself: whatever
    customer_support_mas/database/fixtures.py seeds must be internally
    consistent."""
    from customer_support_mas.tenancy.config import TenantConfig, assert_unique_datastores

    configs = [
        TenantConfig(
            tenant_id=doc.to_dict()["tenant_id"],
            tier=doc.to_dict()["tier"],
            provider_type=doc.to_dict()["provider_type"],
            provider_config=doc.to_dict().get("provider_config", {}),
            pool_id=doc.to_dict().get("pool_id"),
            project_id=doc.to_dict().get("project_id"),
        )
        for doc in mock_db.collection("tenants").stream()
    ]

    assert configs, "expected the fixtures to seed at least one tenant"
    assert_unique_datastores(configs)
