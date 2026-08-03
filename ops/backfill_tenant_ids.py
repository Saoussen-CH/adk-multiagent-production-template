"""
Backfill `tenant_id` onto legacy backend account documents
==========================================================
Run this ONCE per tenant database, after deploying the tenant-scoped account
layer and before tightening `Database._belongs_to_tenant`.

Why this exists
---------------
`backend/app/database.py`'s `_belongs_to_tenant` treats a document with **no**
`tenant_id` field as belonging to whichever tenant asks. That leniency is
deliberate — documents written before the account layer became tenant-scoped
carry no `tenant_id`, and rejecting them would have locked existing users out
of their own accounts on deploy day — but it was only ever meant to be
temporary, and nothing existed to make it so.

Left in place it is a real hole, not just an untidy one. `provider_config.
database_id` can be re-pointed at a different tenant (`customer_support_mas/
tenancy/config.py` allows it after `invalidate_tenant_config_cache()`), e.g.
when a tenant is retired and its database re-used. The new tenant would then
inherit every un-stamped legacy document in that database as its own, and the
one check that would normally stop it — `_belongs_to_tenant` — is exactly the
check the `None` case disables.

This script closes that by stamping the documents, so the leniency stops being
load-bearing for any database it has been run against.

What it touches
---------------
The four places the backend writes account data, all in the tenant's own
Firestore database (resolved through the *same* `load_tenant_config` /
`get_db_client` path the runtime uses, so it cannot be aimed at the wrong
database by hand):

    users/{user_id}
    sessions/{session_id}
    sessions/{session_id}/messages/{message_id}
    tokens/{token}

Rules
-----
- A document with no `tenant_id` is stamped with this tenant's id.
- A document already stamped with this tenant's id is left untouched
  (so the script is idempotent — running it twice changes nothing).
- A document stamped with a **different** tenant's id is left untouched and
  reported. That is not a document to fix; it is evidence that two tenants'
  data is sitting in one database, which this script must not paper over.
  The process exits non-zero so an operator sees it.
- Messages under a foreign session are skipped with it, for the same reason.

Scan before write
-----------------
Every collection (and the messages subcollection under every session) is
scanned and classified FIRST, with no writes. Only if that scan finds zero
foreign documents anywhere in the database does a second pass actually write
the stamps — for a dry run, that second pass never runs at all; it just
reports what it would have done.

This ordering matters because a database containing so much as one
foreign-tagged document can no longer be trusted to classify "no tenant_id"
as "our tenant's legacy document" — it might just as easily be the *other*
tenant's own not-yet-migrated document, indistinguishable by that field
alone. So once contamination is confirmed anywhere, nothing gets written
anywhere in that invocation, not just to the document that tipped it off.
Writing collection-by-collection as you go — stamping "users" before you've
even looked at "tokens" — would have already turned some of those ambiguous
documents into permanent, silent misattributions by the time the foreign
document surfaces.

Usage:
    # from the repo root, with the target environment's .env loaded
    PYTHONPATH=. python ops/backfill_tenant_ids.py --tenant-id acme-electronics --dry-run
    PYTHONPATH=. python ops/backfill_tenant_ids.py --tenant-id acme-electronics

    # or: make backfill-tenant-ids ENV=dev TENANT=acme-electronics
"""

import argparse
import sys
from dataclasses import dataclass, field

# Collections whose documents the backend writes for a single tenant.
ACCOUNT_COLLECTIONS = ("users", "sessions", "tokens")
SESSIONS_COLLECTION = "sessions"
MESSAGES_SUBCOLLECTION = "messages"


@dataclass
class BackfillResult:
    """Per-collection tallies, and the foreign documents found."""

    stamped: int = 0
    already_stamped: int = 0
    foreign: int = 0
    foreign_examples: list = field(default_factory=list)

    def merge(self, other: "BackfillResult") -> None:
        self.stamped += other.stamped
        self.already_stamped += other.already_stamped
        self.foreign += other.foreign
        self.foreign_examples.extend(other.foreign_examples)


def _scan_collection(collection, tenant_id: str, label: str) -> tuple:
    """Classify every document in one collection reference. Never writes.

    Returns `(result, to_stamp)`: the per-document tallies, plus the ids of
    the documents that need `tenant_id` stamped — left for a caller-controlled
    write pass to actually apply, once every collection has been scanned and
    it is confirmed safe to write anything at all.

    The stream is drained into a list first — mutating documents while
    iterating a live query is asking for trouble on either backend, and this
    function does not mutate anyway, but the same helper is reused to collect
    ids for the write pass that follows.
    """
    result = BackfillResult()
    to_stamp: list = []

    for snapshot in list(collection.stream()):
        data = snapshot.to_dict() or {}
        existing = data.get("tenant_id")

        if existing == tenant_id:
            result.already_stamped += 1
            continue

        if existing is not None:
            result.foreign += 1
            result.foreign_examples.append(f"{label}/{snapshot.id} (tenant_id={existing!r})")
            print(f"   ⚠️  {label}/{snapshot.id} belongs to tenant {existing!r} — left untouched")
            continue

        result.stamped += 1
        to_stamp.append(snapshot.id)

    return result, to_stamp


def _stamp_documents(collection, doc_ids: list, tenant_id: str) -> None:
    """Write `tenant_id` onto exactly the documents the scan phase found
    un-stamped — nothing else.

    Writes go through `collection.document(doc_id).update(...)` rather than
    `snapshot.reference.update(...)`: both work against real Firestore, but
    only the former also works against the in-memory client the tests use
    (tests/mock_firestore.py's snapshots carry no `.reference`).
    """
    for doc_id in doc_ids:
        collection.document(doc_id).update({"tenant_id": tenant_id})


def backfill_tenant_ids(db, tenant_id: str, dry_run: bool = False) -> dict:
    """Stamp `tenant_id` onto every un-stamped account document in `db`.

    Two phases, in this order, across the WHOLE database — never per
    collection:

    1. Scan every collection (and every session's messages subcollection),
       classifying each document. Nothing is written in this phase.
    2. Only if that scan found zero foreign documents anywhere does a write
       pass run, stamping exactly the documents the scan flagged as needing
       it. A dry run skips this phase entirely and just reports.

    See the module docstring ("Scan before write") for why a foreign document
    found in, say, "tokens" must block writes to "users" too, not just to
    itself.

    Args:
        db: a Firestore client already bound to THIS tenant's database.
        tenant_id: the id to stamp.
        dry_run: report what would change without writing anything.

    Returns:
        {collection_label: BackfillResult}, including the synthetic
        "sessions/*/messages" label for the messages subcollections.
    """
    results: dict = {}
    # label -> (collection_ref, [doc_ids to stamp]), built during the scan
    # phase and only consumed if the write phase actually runs.
    pending: dict = {}

    for name in ACCOUNT_COLLECTIONS:
        print(f"\n📁 {name}")
        collection = db.collection(name)
        result, to_stamp = _scan_collection(collection, tenant_id, name)
        results[name] = result
        pending[name] = (collection, to_stamp)
        print(f"   stamped={result.stamped} already={result.already_stamped} foreign={result.foreign}")

    # Messages live in a subcollection under each session, so they are reached
    # per session rather than by a collection-group query — which keeps this
    # working against the in-memory test client and avoids needing a
    # collection-group index on a one-off job.
    print(f"\n📁 {SESSIONS_COLLECTION}/*/{MESSAGES_SUBCOLLECTION}")
    messages = BackfillResult()
    messages_label = f"{SESSIONS_COLLECTION}/*/{MESSAGES_SUBCOLLECTION}"
    for snapshot in list(db.collection(SESSIONS_COLLECTION).stream()):
        session_tenant = (snapshot.to_dict() or {}).get("tenant_id")
        if session_tenant is not None and session_tenant != tenant_id:
            # Another tenant's session: its transcript is theirs too. Not
            # scanned at all, so it can never end up in `pending` either.
            print(f"   ⚠️  skipping messages of foreign session {snapshot.id} (tenant_id={session_tenant!r})")
            continue
        label = f"{SESSIONS_COLLECTION}/{snapshot.id}/{MESSAGES_SUBCOLLECTION}"
        sub = db.collection(SESSIONS_COLLECTION).document(snapshot.id).collection(MESSAGES_SUBCOLLECTION)
        result, to_stamp = _scan_collection(sub, tenant_id, label)
        messages.merge(result)
        pending[label] = (sub, to_stamp)
    print(f"   stamped={messages.stamped} already={messages.already_stamped} foreign={messages.foreign}")
    results[messages_label] = messages

    # Abort phase: if the scan found a foreign document ANYWHERE, stop here
    # — before writing a single document, in a real run or a dry run alike.
    # A dry run would not have written anyway; a real run must not, because
    # by now we know this database may hold another tenant's still-unstamped
    # documents too, indistinguishable from ours by `tenant_id` alone.
    total_foreign = sum(result.foreign for result in results.values())
    if total_foreign or dry_run:
        return results

    # Write phase: only reached with zero foreign documents found anywhere.
    for collection, to_stamp in pending.values():
        _stamp_documents(collection, to_stamp, tenant_id)

    return results


def backfill_tenant(tenant_id: str, dry_run: bool = False) -> dict:
    """Resolve `tenant_id` to its account database the same way the request
    path does, then backfill it.

    Deliberately no `--database` flag: pointing a stamping job at a database
    by hand is how you stamp one tenant's id onto another tenant's documents.
    `get_tenant_database` raises for an unknown tenant, a tenant sharing a
    database with another (TenantConfigConflictError) or one with no account
    store configured — all three are reasons not to write anything.
    """
    from backend.app.database import get_tenant_database

    store = get_tenant_database(tenant_id)
    print("=" * 60)
    print("BACKFILL tenant_id — BACKEND ACCOUNT DOCUMENTS")
    print("=" * 60)
    print(f"Tenant:   {tenant_id}")
    print(f"Database: {store.database_id}")
    print(f"Mode:     {'DRY RUN (no writes)' if dry_run else 'WRITING'}")

    return backfill_tenant_ids(store.db, tenant_id, dry_run=dry_run)


def _print_summary(results: dict, dry_run: bool) -> int:
    total = BackfillResult()
    for result in results.values():
        total.merge(result)

    print("\n" + "=" * 60)
    # A foreign document anywhere aborts the write phase entirely (see
    # backfill_tenant_ids), so "stamped" below is a candidate count, not an
    # applied one, whenever foreign > 0 — same as a dry run.
    verb = "would stamp" if (dry_run or total.foreign) else "stamped"
    print(f"{verb}: {total.stamped}   already stamped: {total.already_stamped}   foreign: {total.foreign}")
    print("=" * 60)

    if total.foreign:
        print("\n❌ Documents belonging to a DIFFERENT tenant were found in this database:")
        for example in total.foreign_examples[:20]:
            print(f"   - {example}")
        if len(total.foreign_examples) > 20:
            print(f"   ... and {len(total.foreign_examples) - 20} more")
        print(
            "\nThey were NOT modified. Two tenants' documents in one database means the\n"
            "isolation this backfill exists to protect is already broken — investigate\n"
            "before doing anything else."
        )
        return 2

    if not dry_run and total.stamped:
        print(
            "\n✅ Every account document in this database now carries a tenant_id.\n"
            "   Run this for every tenant before tightening the `doc_tenant is None`\n"
            "   leniency in backend/app/database.py's _belongs_to_tenant."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill tenant_id onto legacy backend account documents")
    parser.add_argument("--tenant-id", type=str, required=True, help="Tenant to backfill (must exist in `tenants`)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")

    args = parser.parse_args()
    results = backfill_tenant(args.tenant_id, dry_run=args.dry_run)
    return _print_summary(results, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
