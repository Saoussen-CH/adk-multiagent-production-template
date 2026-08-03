"""The tenant_id backfill must stamp the legacy documents and only those.

Why this exists
---------------
`backend/app/database.py`'s `_belongs_to_tenant` lets a document with no
`tenant_id` field through for *any* tenant — deliberate leniency for
documents written before the account layer was tenant-scoped. Nothing existed
to end that leniency, so it was permanent rather than transitional: re-point
`provider_config.database_id` at a different tenant (which
`customer_support_mas/tenancy/config.py` permits after
`invalidate_tenant_config_cache()`) and the new tenant silently inherits every
un-stamped document in that database, with the one check that would have
stopped it disabled by exactly the `None` case.

`ops/backfill_tenant_ids.py` is the missing step. These tests pin down the
three properties that make it safe to run against real data:

  1. it stamps un-stamped documents (users, sessions, tokens, and the
     messages subcollection under each session);
  2. it is idempotent — a second run changes nothing;
  3. it never touches a document that already carries a tenant_id, whether
     that is this tenant's (leave alone) or another tenant's (leave alone
     AND report, because that is evidence of a real isolation failure, not a
     document to fix).
"""

import pytest

from ops.backfill_tenant_ids import backfill_tenant_ids

TENANT = "test-tenant"
OTHER_TENANT = "other-tenant"


@pytest.fixture
def db(mock_db_factory):
    """An empty in-memory Firestore standing in for one tenant's database."""
    return mock_db_factory("test-tenant-db")


def _seed_mixed(db):
    """A database as it looks mid-migration: some documents written before
    the account layer carried tenant_id, some after."""
    # users: one legacy, one already stamped
    db.collection("users").document("legacy-user").set({"user_id": "legacy-user", "email": "old@example.com"})
    db.collection("users").document("new-user").set(
        {"user_id": "new-user", "email": "new@example.com", "tenant_id": TENANT}
    )

    # tokens: one legacy, one already stamped
    db.collection("tokens").document("legacy-token").set({"user_id": "legacy-user"})
    db.collection("tokens").document("new-token").set({"user_id": "new-user", "tenant_id": TENANT})

    # sessions: one legacy (with two legacy messages), one already stamped
    # (with one stamped message)
    db.collection("sessions").document("legacy-session").set(
        {"session_id": "legacy-session", "user_id": "legacy-user", "is_active": True}
    )
    for message_id in ("m1", "m2"):
        db.collection("sessions").document("legacy-session").collection("messages").document(message_id).set(
            {"message_id": message_id, "role": "user", "content": "hi"}
        )

    db.collection("sessions").document("new-session").set(
        {"session_id": "new-session", "user_id": "new-user", "is_active": True, "tenant_id": TENANT}
    )
    db.collection("sessions").document("new-session").collection("messages").document("m3").set(
        {"message_id": "m3", "role": "user", "content": "hi", "tenant_id": TENANT}
    )


def _tenant_of(db, collection, doc_id):
    return db.collection(collection).document(doc_id).get().to_dict().get("tenant_id")


def test_legacy_documents_are_stamped(db):
    _seed_mixed(db)

    backfill_tenant_ids(db, TENANT)

    assert _tenant_of(db, "users", "legacy-user") == TENANT
    assert _tenant_of(db, "tokens", "legacy-token") == TENANT
    assert _tenant_of(db, "sessions", "legacy-session") == TENANT


def test_legacy_messages_under_a_session_are_stamped_too(db):
    """The subcollection is the easy half to forget: `get_session_messages`
    filters on `_belongs_to_tenant` per message, so leaving messages
    un-stamped leaves the same leniency load-bearing for transcripts."""
    _seed_mixed(db)

    backfill_tenant_ids(db, TENANT)

    messages = db.collection("sessions").document("legacy-session").collection("messages")
    stamped = {snap.id: snap.to_dict().get("tenant_id") for snap in messages.stream()}
    assert stamped == {"m1": TENANT, "m2": TENANT}


def test_already_stamped_documents_are_left_exactly_as_they_were(db):
    _seed_mixed(db)
    before = db.collection("users").document("new-user").get().to_dict().copy()

    results = backfill_tenant_ids(db, TENANT)

    assert db.collection("users").document("new-user").get().to_dict() == before
    assert results["users"].already_stamped == 1
    assert results["users"].stamped == 1


def test_running_twice_changes_nothing(db):
    """Idempotence: a one-off script that is not safe to re-run is a script
    nobody can safely run at all."""
    _seed_mixed(db)

    backfill_tenant_ids(db, TENANT)
    snapshot_after_first = {
        name: {doc.id: dict(doc.to_dict()) for doc in db.collection(name).stream()}
        for name in ("users", "sessions", "tokens")
    }

    second = backfill_tenant_ids(db, TENANT)

    assert {
        name: {doc.id: dict(doc.to_dict()) for doc in db.collection(name).stream()}
        for name in ("users", "sessions", "tokens")
    } == snapshot_after_first
    assert sum(r.stamped for r in second.values()) == 0
    assert second["users"].already_stamped == 2


def test_another_tenants_documents_are_never_re_stamped(db):
    """The hole this backfill exists to close would only get worse if the
    script "fixed" documents belonging to someone else: that is precisely the
    cross-tenant grab `_belongs_to_tenant` is meant to prevent."""
    _seed_mixed(db)
    db.collection("users").document("foreign-user").set(
        {"user_id": "foreign-user", "email": "them@example.com", "tenant_id": OTHER_TENANT}
    )

    results = backfill_tenant_ids(db, TENANT)

    assert _tenant_of(db, "users", "foreign-user") == OTHER_TENANT
    assert results["users"].foreign == 1
    assert results["users"].foreign_examples == ["users/foreign-user (tenant_id='other-tenant')"]


def test_messages_under_a_foreign_session_are_left_alone(db):
    """A session stamped for another tenant carries that tenant's transcript
    with it — including any message that predates the tenant_id field."""
    db.collection("sessions").document("foreign-session").set(
        {"session_id": "foreign-session", "user_id": "them", "tenant_id": OTHER_TENANT}
    )
    db.collection("sessions").document("foreign-session").collection("messages").document("fm1").set(
        {"message_id": "fm1", "role": "user", "content": "theirs"}
    )

    backfill_tenant_ids(db, TENANT)

    messages = db.collection("sessions").document("foreign-session").collection("messages")
    assert [snap.to_dict().get("tenant_id") for snap in messages.stream()] == [None]


def test_dry_run_writes_nothing_but_reports_what_it_would_do(db):
    _seed_mixed(db)

    results = backfill_tenant_ids(db, TENANT, dry_run=True)

    assert _tenant_of(db, "users", "legacy-user") is None
    assert _tenant_of(db, "sessions", "legacy-session") is None
    assert results["users"].stamped == 1
    assert results["tokens"].stamped == 1
    assert results["sessions"].stamped == 1
    assert results["sessions/*/messages"].stamped == 2


def test_it_resolves_the_database_the_same_way_the_request_path_does(db, monkeypatch):
    """There is deliberately no `--database` flag: aiming a stamping job at a
    database by hand is exactly how one tenant's id ends up written onto
    another tenant's documents. The tenant is resolved through
    `get_tenant_database`, i.e. the same `load_tenant_config` /
    `get_db_client` path the request path uses — which also means an unknown
    tenant, a config conflict or a tenant with no account store all raise
    before anything is written."""
    from backend.app import database as backend_database
    from ops.backfill_tenant_ids import backfill_tenant

    _seed_mixed(db)
    asked_for = []
    store = backend_database.Database(
        project_id="test-project", database_id="test-tenant-db", tenant_id=TENANT, client=db
    )
    monkeypatch.setattr(
        backend_database,
        "get_tenant_database",
        lambda tenant_id: asked_for.append(tenant_id) or store,
    )

    backfill_tenant(TENANT)

    assert asked_for == [TENANT]
    assert _tenant_of(db, "users", "legacy-user") == TENANT


def test_finding_another_tenants_documents_exits_non_zero(db):
    """The operator-facing half of that rule: foreign documents are not a
    partial success to report and move on from, they are a reason for the job
    to fail loudly."""
    from ops.backfill_tenant_ids import _print_summary

    _seed_mixed(db)
    db.collection("users").document("foreign-user").set({"user_id": "foreign-user", "tenant_id": OTHER_TENANT})

    clean = _print_summary(backfill_tenant_ids(db, TENANT, dry_run=True), dry_run=True)
    assert clean == 2  # the foreign doc is seen even in a dry run

    db.collection("users").document("foreign-user").delete()
    assert _print_summary(backfill_tenant_ids(db, TENANT, dry_run=True), dry_run=True) == 0


def test_the_backfill_makes_the_belongs_to_tenant_leniency_unnecessary(db):
    """The end-to-end point of the exercise.

    Before the backfill, a `Database` bound to a DIFFERENT tenant accepts the
    legacy documents in this database — that is the `doc_tenant is None`
    leniency, and it is what turns a re-pointed `provider_config.database_id`
    into a cross-tenant read. After the backfill, the same store rejects
    them.
    """
    from backend.app.database import Database

    _seed_mixed(db)
    usurper = Database(project_id="test-project", database_id="test-tenant-db", tenant_id=OTHER_TENANT, client=db)

    # Pre-backfill: the legacy user and session are readable by the wrong tenant.
    assert usurper.get_user("legacy-user") is not None
    assert usurper.get_session("legacy-session") is not None

    backfill_tenant_ids(db, TENANT)

    assert usurper.get_user("legacy-user") is None
    assert usurper.get_session("legacy-session") is None
    # And the rightful tenant still sees them.
    owner = Database(project_id="test-project", database_id="test-tenant-db", tenant_id=TENANT, client=db)
    assert owner.get_user("legacy-user") is not None
    assert owner.get_session("legacy-session") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
