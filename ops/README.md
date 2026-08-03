# Utility Scripts

This directory contains utility scripts for setup and maintenance tasks.

## Scripts

### add_embeddings.py

Adds vector embeddings to the product catalog for RAG (semantic) search.

**Purpose:** Run this ONCE after seeding your database to enable semantic search on products.

**Usage:**
```bash
python ops/add_embeddings.py --project PROJECT_ID --database DATABASE_ID
```

**Example:**
```bash
python ops/add_embeddings.py \
  --project project-ddc15d84-7238-4571-a39 \
  --database customer-support-db
```

**What it does:**
1. Connects to your Firestore database
2. Loads the Vertex AI text-embedding-004 model
3. For each product, creates a rich embedding from name, description, category, and keywords
4. Stores the 768-dimensional embedding vector in the product document
5. Enables semantic search via `customer_support_mas/services/rag_search.py`

**When to run:**
- After initial database seeding
- After adding new products in bulk
- After updating product descriptions

**Note:** This script modifies your Firestore database. Make sure you're targeting the correct project and database!

### backfill_tenant_ids.py

Stamps `tenant_id` onto backend account documents (`users`, `sessions`,
`tokens`, and each session's `messages`) that were written before the account
layer became tenant-scoped.

**Purpose:** Run this ONCE per tenant database. `backend/app/database.py`'s
`Database._belongs_to_tenant` lets a document with no `tenant_id` through for
whichever tenant asks — necessary so a deploy does not lock existing users out
of their accounts, but it means a database whose `provider_config.database_id`
is later re-pointed at a *different* tenant hands that tenant every un-stamped
document in it. Backfilling removes the ambiguity, and is a prerequisite for
ever tightening that leniency to a hard rejection.

**Usage:**
```bash
# dry run first — reports what it would stamp, writes nothing
make backfill-tenant-ids ENV=dev TENANT=acme-electronics DRY_RUN=1
make backfill-tenant-ids ENV=dev TENANT=acme-electronics

# or directly, with the environment already loaded
PYTHONPATH=. python ops/backfill_tenant_ids.py --tenant-id acme-electronics
```

**What it does:**
1. Resolves the tenant's database through the same `load_tenant_config` /
   `get_db_client` path the request path uses (there is deliberately no
   `--database` flag — that is how you stamp the wrong id on the wrong data)
2. Stamps every document that has no `tenant_id`
3. Leaves documents already carrying this tenant's id untouched — so it is
   idempotent and safe to re-run
4. Leaves documents carrying **another** tenant's id untouched, reports them,
   and exits non-zero: that is evidence of an isolation failure to
   investigate, not a document to rewrite

**When to run:** once per tenant, after deploying the tenant-scoped account
layer; and before any operator re-points a retired tenant's database at a new
tenant.

## Related

- Database seeding: `python -m customer_support_mas.database.fixtures`
- RAG search implementation: `customer_support_mas/services/rag_search.py`
- Product tools: `customer_support_mas/tools/product_tools.py`
