# Getting Started

This guide walks you through everything you need to understand and deploy this project: from zero to a running multi-agent AI system on Google Cloud.

---

## What is this project?

This is a **production-ready multi-agent AI customer support system** for a consumer electronics e-commerce store (think Best Buy / Newegg). It demonstrates how to build AI agents that actually work in production: not just a demo, but a fully deployable system with infrastructure-as-code, CI/CD pipelines, semantic search, memory, evaluation, and monitoring.

**The AI system handles four domains:**

| Domain | What it does |
|--------|-------------|
| Products | Search catalog, retrieve product details, check inventory |
| Orders | Track shipments, show order history, check delivery status |
| Billing | Look up invoices, payment status, receipts |
| Refunds | Validate return eligibility (30-day window), process refunds |

---

## How the system works

The system uses a **root agent** that routes each user request to the right specialist agent:

```
User message
    └─► Root Agent (Gemini 2.5 Pro)
            ├─► Product Agent  → search_products, get_product_info, check_inventory, ...
            ├─► Order Agent    → track_order, get_order_history, verify_order_access (guest step-up)
            ├─► Billing Agent  → get_invoice, check_payment_status, get_my_invoices, ...
            └─► Refund Workflow (SequentialAgent)
                    ├─► Step 1: Validate order
                    ├─► Step 2: Check refund eligibility
                    └─► Step 3: Stage for human approval (no money moves yet)
                            └─► Approver reviews (admin UI) → deterministic execution
```

**Key components:**

- **Google ADK** (Agent Development Kit): agent framework
- **Vertex AI Agent Engine**: serverless, scalable agent hosting
- **Gemini 2.5 Pro**: root agent model; **Gemini 2.5 Flash**: specialist agents (cost-optimized)
- **`CommerceProvider` abstraction**: every commerce tool (orders, invoices, products) goes through a `CommerceProvider` protocol, not raw Firestore calls directly — `FirestoreProvider` is the default implementation; a `ShopifyProvider` stub demonstrates plugging in a different commerce backend per tenant
- **Multi-tenant by design**: every request carries a `tenant_id`; there's no default/implicit tenant. Each tenant resolves to its own Firestore database (or external provider). See [Multi-tenancy](#multi-tenancy-every-request-needs-a-tenant_id) below
- **Firestore**: product catalog, orders, invoices, users — scoped per tenant
- **RAG / Vector Search**: semantic product search using `text-embedding-004` (finds "gaming laptop" when user says "gaming computer")
- **Memory Bank**: remembers user preferences across sessions
- **Cloud Run**: React frontend + FastAPI backend
- **Model Armor**: prompt injection and jailbreak protection
- **Anonymous identity**: visitors get a real, silently-issued bearer token on first load (no visible "Continue as Guest" gate) — the same auth mechanism registered users use, never a self-declared/unverified ID
- **Terraform**: all GCP infrastructure as code (APIs, IAM, Firestore, Artifact Registry, Cloud Build triggers)
- **Cloud Build**: automated CI/CD pipeline with evaluation gating

For full architecture details: [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)

### Multi-tenancy: every request needs a `tenant_id`

This is a multi-merchant product — there is no default tenant. `make seed-db` (below) automatically seeds one real tenant, `acme-electronics`, so local setup and demos work out of the box. The one thing you must set yourself is the frontend's `VITE_TENANT_ID` — see Step 4a below. Full design: [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md#multi-tenancy-tenant_id-is-required-everywhere).

### Guest order verification (no account required)

An anonymous visitor can still get help with a *specific* order: if a lookup fails because they're not logged in as that order's owner, the agent can ask for the order number + the email used to place it (`verify_order_access`). A match grants access to exactly that one order for the rest of the conversation — never a persistent credential, never broader account access, and capped at 3 failed attempts. Refunds and billing/payment tools are never reachable this way — only order status/tracking.

---

## Repository structure

```
adk-multiagent-production-template/
├── customer_support_mas/        # Production package
│   ├── agents/
│   │   ├── root/                # Coordinator agent
│   │   ├── product/              # Product agent + tools
│   │   ├── order/                # Order agent + tools (incl. guest order verification)
│   │   ├── billing/              # Billing agent + tools
│   │   └── refund/               # Refund workflow + tools (HITL staging/approval)
│   ├── providers/                # CommerceProvider protocol, FirestoreProvider, ShopifyProvider stub
│   ├── tenancy/                  # Tenant config resolution/caching (multi-tenant core)
│   ├── callbacks.py              # Memory Bank callbacks
│   ├── config.py                 # Model and agent configuration
│   ├── auth.py                   # Ownership-check decorators
│   ├── database/                 # Firestore client + fixture data (seeds the acme-electronics tenant)
│   ├── safety/                   # Model Armor plugin
│   └── services/                 # RAG search
├── mcp_servers/
│   └── fedex_tracking/           # Optional FedEx tracking MCP server (FastMCP), gated by MCP_FEDEX_URL
├── eval_wrappers/               # ADK eval CLI shims (not in production package)
├── frontend/                    # React/TypeScript UI
├── backend/                     # FastAPI backend (Cloud Run)
├── terraform/
│   ├── modules/core/            # Reusable Terraform module
│   └── environments/
│       ├── dev/                 # dev environment config
│       ├── staging/             # staging environment config
│       └── prod/                # prod environment config
├── cloudbuild/                  # Cloud Build pipeline YAML files
├── tests/
│   ├── evaluation/              # Custom eval metrics (tool_name_f1)
│   ├── unit/                    # Layer 2: single-agent eval
│   ├── integration/             # Layer 3: multi-agent handoff eval
│   ├── smoke/                   # Post-deploy smoke tests
│   ├── load/                    # Load tests (Locust)
│   ├── eval_vertex.py           # Post-deploy eval script
│   └── generate_eval_dataset.py # Eval dataset generation
├── ops/                         # Infrastructure setup scripts
└── docs/                        # Full documentation
```

---

## Which setup path should I follow?

| Goal | Path |
|------|------|
| I want to explore the agent locally (no cloud deploy needed) | [Quick Local Setup](#quick-local-setup) |
| I want to deploy to one GCP project | [Single Environment Deploy](#single-environment-deploy) |
| I want dev / staging / prod environments with CI/CD | [Multi-Environment Setup](#multi-environment-setup) |

---

## Quick Local Setup

Run the agent on your machine. You still need a GCP project for Vertex AI (Gemini) and Firestore, but no Cloud Run or Agent Engine deployment required.

### Step 1: Prerequisites

- GCP account with billing enabled
- `gcloud` CLI installed: [cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install)
- `uv` installed (manages Python 3.11 automatically): `curl -LsSf https://astral.sh/uv/install.sh | sh`

```bash
gcloud auth login
gcloud auth application-default login
```

### Step 2: Clone and install

```bash
git clone https://github.com/Saoussen-CH/adk-multiagent-production-template.git
cd adk-multiagent-production-template
make install
```

### Step 3: Create a GCP project and enable APIs

```bash
gcloud projects create YOUR_PROJECT_ID --name="Customer Support Agent"
gcloud config set project YOUR_PROJECT_ID
bash ops/setup_gcp.sh
```

This enables all required GCP APIs, creates the service account, and grants IAM roles.

> See [docs/PREREQUISITES.md](./docs/PREREQUISITES.md) for the full list if you want to do this manually.

### Step 4: Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```bash
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_CLOUD_STORAGE_BUCKET=gs://your-bucket-name
FIRESTORE_DATABASE=customer-support-db
```

> Full reference: [docs/ENV_SETUP.md](./docs/ENV_SETUP.md)

### Step 5: Seed the Firestore database

```bash
make setup-firestore    # creates the database (safe to run if it already exists)
make seed-db            # loads products, orders, invoices, users, and the acme-electronics tenant config
```

### Step 5a: Configure the frontend tenant (required before using the chat UI)

The frontend must know which tenant it's serving — there's no default. `make seed-db` seeds a tenant named `acme-electronics`; point the frontend at it:

```bash
cp frontend/.env.example frontend/.env
```

Edit `frontend/.env`:

```bash
VITE_TENANT_ID=acme-electronics
```

Without this, every chat/session request 404s (unknown tenant) — this is enforced, not just a convention. See [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md#multi-tenancy-tenant_id-is-required-everywhere).

### Step 6: Test the agent locally

```bash
make test-local
```

This runs the agent in-process (no Cloud Run, no Agent Engine). You should see the agent respond to a test query.

### Step 7: (Optional) Enable RAG semantic search

Without RAG, the agent uses keyword search. With RAG, "gaming computer" finds "ROG Gaming Laptop".

```bash
make vector-index    # creates the Firestore vector index
```

Check periodically until `STATE = READY` (takes 5-10 minutes):

```bash
gcloud firestore indexes composite list \
  --database=customer-support-db \
  --project=$GOOGLE_CLOUD_PROJECT
```

Then add embeddings and you're ready:

```bash
make add-embeddings
```

---

## Single Environment Deploy

Deploy the full system to one GCP project. This is the fastest path to a live, working system.

Follow [Quick Local Setup](#quick-local-setup) steps 1–7, then continue here.

### Step 8: Deploy the agent to Vertex AI Agent Engine

```bash
make deploy-agent-engine
```

This deploys the multi-agent system as a serverless Reasoning Engine and configures the Memory Bank. It takes ~5 minutes.

Expected output:
```
✓ Agent deployed successfully!
Resource name: projects/123/locations/us-central1/reasoningEngines/456
```

Copy the resource name into `.env`:
```bash
AGENT_ENGINE_RESOURCE_NAME=projects/123/locations/us-central1/reasoningEngines/456
```

> **Important:** After the first deploy, Google creates a new Vertex AI service account (`service-PROJ_NUM@gcp-sa-aiplatform-re.iam.gserviceaccount.com`). This SA needs `roles/datastore.user` to access Firestore. If you're using Terraform, re-run `make infra-up ENV=dev` after the first deploy to grant these permissions. Without this, tool calls fail with `403 Missing or insufficient permissions`.

### Step 9: Deploy the web frontend and backend

```bash
make deploy-cloud-run
```

Open the URL printed at the end of the deploy to access the chat UI.

### Step 10: Run the test suite

```bash
make test-tools        # Layer 1: pure Python, no LLM (fast, free)
make test-unit         # Layer 2: single-agent eval (LLM calls)
make test-integration  # Layer 3: multi-agent handoff eval (LLM calls)
```

Or run all three:
```bash
make test
```

> Tests use an `EVAL_PROFILE` environment variable to switch between `fast` (Rouge-1 only), `standard` (default, + tool_name_f1), and `full` (+ final_response_match_v2). See [docs/EVALUATION.md](./docs/EVALUATION.md) for details.

### Step 11: (Optional) Run post-deploy evaluation

This evaluates the live deployed agent using Vertex AI's Gen AI Evaluation Service:

```bash
make eval-post-deploy ENV=dev
# or with an explicit ID:
make eval-post-deploy AGENT_ENGINE_ID=projects/PROJECT/locations/us-central1/reasoningEngines/ENGINE_ID
```

Results appear in **Vertex AI → Experiments → post-deploy-eval**. Passing thresholds: `TOOL_USE_QUALITY ≥ 0.5` and `FINAL_RESPONSE_QUALITY ≥ 0.5`.

---

## Multi-Environment Setup

For dev / staging / prod with Terraform-managed infrastructure and Cloud Build CI/CD pipelines. This is the production-grade setup.

> For a detailed step-by-step walkthrough of this path, see [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md).

### What gets created per environment

Each environment (`dev`, `staging`, `prod`) is a separate GCP project. Terraform creates:

- All required GCP APIs enabled
- Service accounts + IAM roles
- Firestore database
- GCS bucket (artifact staging)
- Artifact Registry (Docker images)
- Model Armor template (prompt safety policy)
- Cloud Build triggers (after GitHub connection)

### Branch strategy

Single `main` branch. Environment promotion happens via git tags, not branch merges.

```
feat/* → main → (tag v*.*.*-rc.*) → staging → (tag v*.*.*) → prod canary
```

| Event | Deploy target |
|-------|--------------|
| Push to `main` | dev GCP project (auto) |
| Tag `v*.*.*-rc.*` | staging GCP project |
| Tag `v*.*.*` | prod (shadow + canary via eval gate) |

See [docs/CI_CD.md](./docs/CI_CD.md) for the full pipeline details.

---

## Try these scenarios

Once deployed, test the system with these queries in the chat UI or via `make test-local`:

**1. Semantic product search (RAG)**
```
"Find me a gaming computer"
→ Finds ROG Gaming Laptop via vector similarity
```

**2. Comprehensive product info**
```
"Tell me everything about PROD-001"
→ Details + inventory + reviews in one response
```

**3. Refund request (SequentialAgent workflow)**
```
"I want a refund for order ORD-12345"
→ Step 1: Validate order ✓
→ Step 2: Check 30-day return window ✓
→ Step 3: Staged for human approval (PENDING_APPROVAL — no money moves yet)
```
The refund isn't executed here — a human approver reviews it in the admin UI (dual control: requester ≠ approver) before any deterministic execution happens.

**4. Order tracking**
```
"Where is my order ORD-67890?"
→ Status, carrier, estimated delivery
```

**5. Guest order verification (no login)**
```
As an anonymous visitor: "Where is my order ORD-67890?"
→ Denied (not your account) → agent asks for order number + email
→ Provide the email on file for that order
→ Access granted to that one order for the rest of this conversation
```

**6. Memory across sessions**
```
Session 1: "I prefer detailed technical specs"
Session 2: "Tell me about the keyboard"
→ Agent remembers and responds with detailed specs
```

---

## Customize this project

| What to change | Where |
|---------------|-------|
| Product catalog and seed data | `customer_support_mas/database/fixtures.py` |
| Agent instructions and behavior | `customer_support_mas/agents/` |
| Add new tools (new Firestore queries) | `customer_support_mas/agents/<domain>/tools.py` |
| Change models (e.g. Flash → Pro) | `customer_support_mas/config.py` |
| Business rules (return window, etc.) | Tool implementations in `tools/` |

---

## Documentation map

| Document | What it covers |
|----------|---------------|
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | System design, agent hierarchy, data flow |
| [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) | Multi-environment setup, Terraform, CI/CD triggers |
| [docs/PREREQUISITES.md](./docs/PREREQUISITES.md) | GCP APIs, IAM roles, manual setup |
| [docs/ENV_SETUP.md](./docs/ENV_SETUP.md) | All environment variables explained |
| [docs/CI_CD.md](./docs/CI_CD.md) | Cloud Build pipelines, triggers, branch strategy |
| [docs/EVALUATION.md](./docs/EVALUATION.md) | 5-stage eval architecture + post-deploy eval |
| [docs/TESTING_SCENARIOS.md](./docs/TESTING_SCENARIOS.md) | Demo scenarios, test data, credentials |
| [docs/MEMORY_BANK.md](./docs/MEMORY_BANK.md) | Memory Bank implementation details |
| [docs/DATA_MODEL.md](./docs/DATA_MODEL.md) | Firestore collections and schema |
