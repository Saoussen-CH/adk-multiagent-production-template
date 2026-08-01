# FedEx Tracking MCP — Deploy, Govern, and Migrate

Runbook for the FedEx shipment-tracking MCP server: how it's deployed, how its
egress is governed with Agent Gateway + Agent Registry, and the future path to
Agent Identity Auth Manager once that surface leaves Preview.

## 1. What this is

`mcp_servers/fedex_tracking/` is a standalone FastMCP server that wraps the
FedEx Track API (OAuth2 client-credentials) behind a single tool,
`track_shipment`. It ships with a **mock mode** (`FEDEX_MOCK=true`, the
default) so dev and eval work end-to-end with no real FedEx credentials —
the mock returns deterministic tracking payloads from `fedex_client.py`.
Setting `FEDEX_MOCK=false` switches to live calls against FedEx's API using
`FEDEX_CLIENT_ID` / `FEDEX_CLIENT_SECRET` / `FEDEX_API_BASE`.

The **order agent** (`customer_support_mas/agents/order/`) is the only
consumer. `customer_support_mas/agents/order/mcp.py`'s `build_fedex_toolset()`
is env-gated on `MCP_FEDEX_URL`: when that variable is unset, the order agent
is byte-for-byte its pre-FedEx self and never touches ADK's `McpToolset` /
`StreamableHTTPConnectionParams` machinery at all — nothing changes for
existing deployments that don't opt in.

This closes part of spec `docs/superpowers/specs/2026-07-17-v2-distributed-architecture-design.md`
decision **D3** (Agent Gateway egress governs order-agent → FedEx MCP traffic
with tool-level IAM) and sets up **D4** (FedEx credential custody).

## 2. Deploy

```bash
make deploy-mcp-fedex ENV=dev
```

This runs `deployment/deploy-mcp-fedex.sh`, which builds the container from
`mcp_servers/fedex_tracking/Dockerfile` via Cloud Build and deploys it to
Cloud Run as `fedex-tracking-mcp`, `--no-allow-unauthenticated` (it is never
publicly reachable). It grants `roles/run.invoker` to the **FedEx MCP
invoker SA** — not to the Agent Identity principal directly, see section 7
for why — and prints the binding command as a fallback if
`FEDEX_MCP_INVOKER_SA_EMAIL` isn't yet set in the env file:

```bash
gcloud run services add-iam-policy-binding fedex-tracking-mcp \
  --region=REGION --project=PROJECT_ID \
  --member='serviceAccount:FEDEX_MCP_INVOKER_SA_EMAIL' \
  --role='roles/run.invoker'
```

After deploying:

1. Apply Terraform if you haven't already (creates the invoker SA — see
   section 7) and set both env vars in the target env file
   (`.env` / `.env.staging` / `.env.prod`):
   - `MCP_FEDEX_URL=<printed-url>/mcp`
   - `FEDEX_MCP_INVOKER_SA_EMAIL=<terraform output fedex_mcp_invoker_email>`
2. Redeploy the Agent Engine so the order agent picks up the toolset:
   `make deploy-agent-engine ENV=dev`.
3. Manually verify the live-tracking path with
   `tests/post_deploy/datasets/fedex_tracking_cases.json` — this is a
   deliberately **opt-in** dataset, not wired into any cloudbuild
   `_EVAL_DATASET` release gate (those still point at
   `tests/post_deploy/datasets/post_deploy_cases.json`, which contains no
   FedEx case), because the case can only pass once `MCP_FEDEX_URL` is
   actually set on the target engine and no cloudbuild pipeline sets it
   today:

   ```bash
   PYTHONPATH=. python tests/eval_vertex.py \
     --agent-engine-id <id> \
     --dataset tests/post_deploy/datasets/fedex_tracking_cases.json \
     --custom-inference
   ```

## 3. Real FedEx credentials

By default `fedex-tracking-mcp` runs in mock mode and needs no secrets. To
onboard real FedEx API credentials:

1. Enable the Secret Manager resources (currently `count = 0` unless flagged
   on): in `terraform/environments/<env>/terraform.tfvars`, set

   ```hcl
   fedex_secrets_enabled = true
   ```

2. Apply: `make infra-up ENV=<env>` (or `terraform apply` directly under
   `terraform/environments/<env>`). This creates the
   `fedex-client-id` / `fedex-client-secret` Secret Manager secrets
   (`terraform/modules/core/secrets.tf`) with no versions — Terraform never
   holds the credential values themselves.
3. Add the actual secret values out-of-band:

   ```bash
   echo -n "<real-fedex-client-id>"     | gcloud secrets versions add fedex-client-id     --data-file=- --project=PROJECT_ID
   echo -n "<real-fedex-client-secret>" | gcloud secrets versions add fedex-client-secret --data-file=- --project=PROJECT_ID
   ```

4. Redeploy the MCP server with mock mode off:

   ```bash
   FEDEX_MOCK=false make deploy-mcp-fedex ENV=<env>
   ```

   `deployment/deploy-mcp-fedex.sh` only attaches `--set-secrets` for
   `FEDEX_CLIENT_ID` / `FEDEX_CLIENT_SECRET` when `FEDEX_MOCK != true`, so the
   Cloud Run service now reads live credentials from Secret Manager at
   startup instead of running the mock client path.

This is the interim custody model per spec D4 — see section 5 for the planned
migration once Auth Manager is stable.

## 4. Gateway + Registry

Governance is scripted, not Terraform — a deliberate choice, not (only)
because Terraform can't do it; see section 6 for the corrected reasoning.

```bash
make setup-gateway ENV=dev
```

This runs, in order:

1. **`ops/setup_agent_gateway.sh`** — enables the required APIs, creates an
   Agent Gateway named `customer-support-egress` in **Agent-to-Anywhere
   (egress)** mode, attaches an IAP-backed authorization policy (deployed in
   **audit-only / dry-run mode**, `iamEnforcementMode: "DRY_RUN"` — traffic is
   logged, never blocked, until you deliberately flip it), and PATCHes the
   existing Agent Engine's `spec.deploymentSpec.agentGatewayConfig` to route
   its egress through that gateway. It finishes by printing the gateway's
   `describe` output and a GET of the engine's own
   `agentGatewayConfig` (non-null confirms the bind took).
2. **`ops/register_agent_registry.sh`** — registers both the Agent Engine and
   the `fedex-tracking-mcp` Cloud Run service as Agent Registry service
   entries (the gateway default-denies all egress to anything unregistered),
   then grants the agent identity principal `roles/iap.egressor` on the MCP
   endpoint specifically — without this grant the gateway blocks the call
   even in audit-only mode logging shows it as denied-but-permitted-through.
   Finishes with a `describe` of both registered entries.

You can run either script standalone against a specific env file:
`bash ops/setup_agent_gateway.sh .env.staging`.

**On the audit-only / dry-run note:** leaving the IAP authorization extension
in `DRY_RUN` mode is deliberate for first rollout — it lets you validate that
the expected traffic pattern (order agent → fedex-tracking-mcp, nothing else)
shows up correctly in gateway audit logs before you start actually blocking
anything.

**Before ever flipping `iamEnforcementMode` off `DRY_RUN`**, run:

```bash
make register-platform-endpoints ENV=dev
```

This runs `ops/register_platform_endpoints.sh`, which registers 12 internal
Google APIs × 5 URL variants each (global, mTLS, locational, locational-mTLS,
regional/REP — 60 `endpoints` total) in Agent Registry and grants
`roles/iap.egressor` on each. **Skipping this step and enforcing anyway is
what broke the engine's own internal platform calls earlier** (confirmed
live: the gateway's own logs showed the denied request was an internal
`aiplatform.mtls.googleapis.com` call, not FedEx traffic) — the gateway
default-denies any destination hostname absent from the registry, regardless
of enforcement mode. See section 7 for the full root-cause writeup and the
live-verified `--endpoint-spec-type=no-spec` mechanism this script uses.

Once that's run and verified (`gcloud agent-registry endpoints list
--project=... --location=...` should show 60 entries), flip enforcement by
re-running `gcloud beta service-extensions authz-extensions import` with
`iamEnforcementMode` removed from the YAML (per the codelab: set to `null`,
not "DRY_RUN") — **test carefully and be ready to revert to DRY_RUN
immediately** if anything unexpected breaks; this has broken things once
before in this repo, even in dry-run testing done incorrectly.

## 5. Auth Manager migration (future)

Spec D4 calls Secret Manager (section 3) the interim custody model, not the
end state: "FedEx credentials in Agent Identity Auth Manager (2-legged OAuth
provider): agent never holds the raw secret; access attributable to the
agent's SPIFFE ID. Fallback if Auth Manager (Preview) proves unstable: Secret
Manager."

Once Agent Identity Auth Manager is out of Preview, the migration is:

1. Configure a **2-legged OAuth (2LO) auth provider** in Auth Manager holding
   the FedEx client ID/secret (see the "Agent's own authority" row in
   `refs/Govern/Agent Identity overview.md`'s capabilities table, and the
   "Agent Identity auth manager" section of that same doc for the
   centralized-credential-vault model).
2. Point the FedEx MCP server's tool call at that auth provider instead of
   reading `FEDEX_CLIENT_ID`/`FEDEX_CLIENT_SECRET` env vars — the agent's
   SPIFFE ID authenticates to the auth manager directly, so the raw secret
   never passes through the agent's own environment or Secret Manager.
3. Remove the `fedex-client-id` / `fedex-client-secret` Secret Manager
   resources (set `fedex_secrets_enabled = false`, `terraform apply`) once
   the auth-provider path is confirmed working end-to-end.

Not done now because: Auth Manager's 2LO provider type is itself Preview
(same launch stage as Agent Gateway/Registry), and the spec's explicit
fallback clause says to use Secret Manager until it proves stable.

## 6. Known Preview caveats

Everything in this document touches Preview-stage `gcloud`/REST surfaces.
`ops/setup_agent_gateway.sh` and `ops/register_agent_registry.sh` have both
been run live end-to-end against a real project (workshop-494016), on gcloud
578.0.0 — the Agent Gateway, its IAP authz extension/policy, the engine
attachment, the MCP server's Agent Registry entry, and the IAP egressor
binding were all created and independently verified (`describe`/`get-iam-policy`
after the fact, not just trusting a 0 exit code). Findings below reflect that
run, not doc reading.

Three are **confirmed wrong** by live testing against a real deployed engine
in this repo (not just "unverifiable" — actually contradicted by observed
behavior):

- **Trust domain**: Google's docs write the SPIFFE trust domain as
  `agents.global.project-PROJECT_NUMBER.system.id.goog`. The actual value,
  read back from a real engine's own `spec.effective_identity`, is
  `agents.global.proj-PROJECT_NUMBER.system.id.goog` — `proj-`, not
  `project-`. Using the docs' literal string gets the IAM API to reject the
  member as "of an unknown type." See `terraform/modules/core/iam.tf` and
  `CLAUDE.md`. Both `ops/setup_agent_gateway.sh` and
  `ops/register_agent_registry.sh` use the confirmed `proj-` form.
- **CAA opt-out env var**: `GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES`
  must be set as the **string** `"False"`, not a Python bool — the docs show
  a bool. Not directly exercised by this task's scripts, but it's the same
  Agent Identity path these scripts attach traffic to, so it applies to any
  Agent Engine these scripts govern.
- **`--mcp-server-spec-type=no-spec` never projects a discoverable
  resource.** `gcloud agent-registry services create ...
  --mcp-server-spec-type=no-spec` succeeds and creates a raw "Service"
  entry, but `gcloud agent-registry mcp-servers describe <same-id>` returns
  `NOT_FOUND` — confirmed not a propagation delay (polled 8x over ~2.5min,
  identical error every time). `gcloud iap web add-iam-policy-binding
  --mcp-server=<same-id>` fails the same way, because that flag resolves
  against the *projected* resource, not the raw Service. Providing real spec
  content (`--mcp-server-spec-type=tool-spec --mcp-server-spec-content=<tools/
  list-shaped JSON>`) does project a resource — but at a **system-generated
  ID** (`agentregistry-00000000-...`), never the Service ID you chose; the
  real ID only appears in the `registryResource:` field of the create/update
  response. `ops/register_agent_registry.sh` registers the MCP server with
  real tool-spec content and captures that generated ID for the IAM binding.
  **Correction (re-checked live ~6 hours later, same project):**
  `--agent-spec-type=no-spec` — tested separately from the MCP case above —
  DOES eventually project a discoverable `agents/<generated-id>` resource;
  `gcloud agent-registry agents describe` on the order agent's generated ID
  returned full content, not `NOT_FOUND`. The original claim that this never
  projects was based on an immediate ~2.5min poll; whether the real
  explanation is longer eventual-consistency latency specifically for
  `agent-spec-type` (the MCP case was not re-tested at this delay) is
  unconfirmed. This doesn't change the egress binding itself (its
  `--member` is the order agent's SPIFFE principal string, addressed
  directly, not via a registry resource ID) — it means the
  "A2A-migration-unlocks-discoverability" argument this finding previously
  made does not hold as originally stated. Treat this specific item with
  more caution than the rest of this section.

One item originally in this section — `gcloud iap web add-iam-policy-binding`
not supporting `--resource-type=agent-registry` — turned out to be a genuine
SDK-version gap, not a doc bug: it appeared on 482.0.0 but is present and
correct on 578.0.0, confirmed by the live run above. `iap.googleapis.com`
does need to be explicitly enabled, though (added to
`ops/setup_agent_gateway.sh`'s API list) — it's not implied by the other
Agent Gateway APIs, and its absence produces a confusing `SERVICE_DISABLED`
error at binding time rather than at setup time.

By decision, Agent Gateway and Agent Registry setup stay `gcloud`/REST
scripts (`ops/setup_agent_gateway.sh`, `ops/register_agent_registry.sh`), not
Terraform. **Correction, checked live:** native resources now exist —
`google_network_services_agent_gateway`, `google_agent_registry_service`,
`google_agent_registry_binding`, and per-scope `google_iap_agent_registry_
{agent,endpoint,mcp_server}_iam_{binding,member,policy}` — confirmed present
in `hashicorp/google` provider `v7.42.0`'s schema, but **absent** from
`v6.50.0`, the version this repo's `dev` environment already has applied
(no `versions.tf` pins a floor anywhere in this repo, so this was
discovered, not assumed). Migrating to native Terraform would need a
provider major-version bump applied against live-managed dev state
(Firestore, IAM, Cloud Run, Secret Manager, Model Armor, etc.) — real
blast radius unrelated to Agent Gateway/Registry, and a decision for a
separate, deliberately-reviewed change, not bundled into a FedEx-MCP fix.
Staying on `gcloud`/REST scripts for now is still the right call, but no
longer because "no Terraform support exists" — that part of the original
reasoning is now out of date.

## 7. Cloud Run 401 — root cause and fix (implemented and live-verified)

Live-testing against workshop-494016 hit a second failure after fixing the
initial 403 ("Tool not found", resolved by adding `header_provider` to
`build_fedex_toolset()`): once that provider attaches an ID token fetched via
`google.oauth2.id_token.fetch_id_token()` against the ADC of the
AGENT_IDENTITY-provisioned engine, Cloud Run's IAM invoker layer rejects it
with **401 "the access token could not be verified"**. This section
documents the root cause (found by reading Google's own reference
implementation, the `cloudnet-agent-gateway` codelab and its backing repo
`GoogleCloudPlatform/cloud-networking-solutions`, `demos/agent-gateway/`) and
the fix, which is **implemented and live-verified** against workshop-494016:
Terraform applied (new SA + IAM binding), `roles/run.invoker` on
`fedex-tracking-mcp` moved from the Agent Identity principalSet to the
invoker SA, Agent Engine redeployed, and the `track_shipment` live-tracking
eval case (`tests/post_deploy/datasets/fedex_tracking_cases.json`) re-run.
Confirmed directly in the reasoning engine's Cloud Logging output for that
run: every `POST .../mcp` call to `fedex-tracking-mcp` returned `200 OK` /
`202 Accepted` (no 401), `track_shipment` executed, and the agent returned
the correct mock tracking response — the 401 is resolved. (The eval
framework's own LLM-judge scoring step separately errored on an unrelated
`events[].author` schema bug in that specific eval SDK version — a judge
harness issue, not a regression in this fix; inference itself succeeded and
is visible in the raw logs regardless of the judge outcome.) Unit tests
cover both the impersonation and direct-ADC-fallback code paths
(`tests/unit/test_order_mcp_toolset.py`).

### Root cause

Agent Identity issues workload credentials scoped to the SPIFFE/mTLS trust
domain (`agents.global.proj-PROJECT_NUMBER.system.id.goog`), meant for the
platform's own internal API verification path (`aiplatform.googleapis.com`
etc., gated by the `GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES`
opt-out). Cloud Run's `--no-allow-unauthenticated` invoker check is a
**separate, older OIDC verification path** that expects a standard
Google-signed ID token for a real IAM principal (service account or user),
not a SPIFFE-domain workload token. The two paths were never meant to
interoperate directly — the token our `header_provider` sends is well-formed
and correctly signed, just the wrong token type for what Cloud Run's
verifier understands, hence 401 rather than a clean 403.

### The fix: service-account impersonation as the bridge

`demos/agent-gateway/src/mortgage-agent/deploy_agent.py` in the codelab's
repo does not ask Cloud Run to trust Agent Identity directly. It bridges the
two trust domains via a dedicated invoker service account — implemented here
as:

1. **Dedicated invoker SA**: `google_service_account.fedex_mcp_invoker` in
   `terraform/modules/core/iam.tf`, `fedex-mcp-invoker@PROJECT.iam.gserviceaccount.com`,
   output as `fedex_mcp_invoker_email` (their equivalent: `agent_mcp_invoker_email`
   output in `terraform/modules/agent-engine`).
2. **Agent Identity → Token Creator**: `google_service_account_iam_member.agent_identity_impersonates_fedex_invoker`
   grants the project's Agent Identity principalSet
   `roles/iam.serviceAccountTokenCreator` on that SA — Agent Identity becomes
   the *caller* of `generateIdToken`, not the token's subject.
3. **SA → run.invoker**: `deployment/deploy-mcp-fedex.sh` grants
   `roles/run.invoker` on `fedex-tracking-mcp` to the invoker SA (read from
   `FEDEX_MCP_INVOKER_SA_EMAIL` in the env file), not to the Agent Identity
   principalSet — replaces the binding it printed before.
4. **Impersonation, not direct ADC**: `_fedex_id_token_headers_sync()` in
   `customer_support_mas/agents/order/mcp.py` now checks
   `FEDEX_MCP_INVOKER_SA_EMAIL`. When set, it calls
   `_impersonated_id_token_sync()`, which uses
   `google.auth.impersonated_credentials.Credentials` +
   `.IDTokenCredentials` (`target_audience=<fedex-tracking-mcp base URL>`) to
   mint a normal OIDC token for the SA principal — the type Cloud Run's
   invoker check already understands. When unset, it falls back to the
   original `_direct_adc_id_token_sync()` (direct ADC) so envs that haven't
   re-applied Terraform yet don't hard-fail.
5. **Wiring**: `deployment/deploy.py`'s `ENV_VARS` propagates
   `FEDEX_MCP_INVOKER_SA_EMAIL` to the deployed engine, conditionally, same
   pattern as `MCP_FEDEX_URL` (their pattern: `MCP_INVOKER_SA_EMAIL`).

This is a genuine architecture change (new SA + a permission hop: Agent
Identity → Token Creator → SA → ID token → Cloud Run), not a config tweak.
Done in dev (workshop-494016): `terraform apply`, the Cloud Run binding
moved to the invoker SA (old direct Agent Identity binding removed),
`FEDEX_MCP_INVOKER_SA_EMAIL` set in `.env.dev`, Agent Engine redeployed, and
`track_shipment` confirmed live to return `200 OK` end-to-end (no 401).
Staging/prod still need the same sequence — Terraform there hasn't been
applied with this change.

### Second gap: internal-hostname pre-registration before enforcing IAP (resolved and live-verified)

Separately, flipping `ops/setup_agent_gateway.sh`'s IAP authz extension out
of `DRY_RUN` broke the agent's own internal platform calls (confirmed live:
gateway logs showed the denied request was an internal
`aiplatform.mtls.googleapis.com` session-creation call, not FedEx traffic) —
reverted to `DRY_RUN` immediately, no further live testing attempted on that
path. `demos/agent-gateway/terraform/modules/agent-registry-endpoints/main.tf`
confirms the cause: internal Google APIs must be pre-registered in Agent
Registry as spec-less `endpoints` (a distinct resource type from
`mcpServers`) before the gateway lets an agent reach them at all — the
gateway default-denies unregistered destinations regardless of enforcement
mode; `DRY_RUN` only makes the *authorization* decision advisory, not the
*registry-membership* gate.

Their module registers **5 URL variants per API** (global, mTLS, locational
`us-central1-X`, locational-mTLS, regional/REP
`X.us-central1.rep.googleapis.com`) for this fixed list: `aiplatform`,
`cloudresourcemanager`, `global-discoveryengine`, `discoveryengine`,
`logging`, `monitoring`, `oauth2`, `telemetry`, `trace`, `agentregistry`,
`iap`, `iamcredentials`. Then `scripts/grant_agent_mcp_egress.sh
--bind-all-agents --endpoints` grants `roles/iap.egressor` on all of them to
the project-wide agent principalSet, via **direct REST calls**
(`agentregistry.googleapis.com/v1alpha/.../endpoints`,
`iap.googleapis.com/v1/.../iap_web/agentRegistry/endpoints/{id}:{get,set}IamPolicy`)
because `gcloud` has no `--endpoint=` flag yet for this resource type — same
gcloud-lags-docs gap already noted above for `mcpServers`.

**Implemented and live-verified against workshop-494016.**
`ops/register_platform_endpoints.sh` registers the same 12-API × 5-variant
list (60 endpoints total) as this repo's equivalent of the codelab's
Terraform module, using `gcloud agent-registry services create
--endpoint-spec-type=no-spec` (a **third**, distinct spec-type flag from
`--agent-spec-type`/`--mcp-server-spec-type` — confirmed live to actually
project a discoverable `endpoints/<generated-id>` resource, unlike
`--agent-spec-type=no-spec`'s originally-documented behavior, see the
correction in section 6) plus `gcloud iap web add-iam-policy-binding
--endpoint=<id>` for the `roles/iap.egressor` grant. Run via `make
register-platform-endpoints ENV=dev`. Hit and fixed one real bug along the
way: the order agent's own placeholder registration
(`ops/register_agent_registry.sh`) had squatted on the bare
`https://us-central1-aiplatform.googleapis.com` URL, colliding with this
script's locational `aiplatform` endpoint (Agent Registry enforces
one-service-per-interface-URL) — fixed by repointing the placeholder to a
resource-scoped URL (`.../v1/<reasoningEngine resource path>`) instead.

**Enforcement flip, live-verified**: `ops/set_iap_enforcement.sh
.env.dev enforce` (reversible — `... dry-run` reverts instantly) flips
`iamEnforcementMode` off `DRY_RUN` by omitting the field entirely (per the
codelab: set to `null`, not another string) and re-imports the authz
extension. Tested immediately after flipping, unlike the earlier
DRY_RUN-only attempt: (1) a plain "hi" query — exercises internal platform
calls (session creation, Gemini inference) — succeeded; (2) the
`track_shipment` FedEx query succeeded end-to-end. Independently confirmed
via the gateway's own Cloud Logging output (`resource.type=
"networkservices.googleapis.com/Gateway"`, `jsonPayload.authzPolicyInfo`):
real `"result": "ALLOWED"` decisions against
`customer-support-iap-authz-policy`, with `agentGatewayInfo.agentRegistryResource`
correctly resolving to our registered `endpoints/...` resource for the
`us-central1-aiplatform.mtls.googleapis.com` call and to the `mcpServers/...`
resource for the FedEx call — proof enforcement is genuinely active and
matching both traffic categories correctly, not silently still permissive.

### Trust domain discrepancy — needs live re-verification

`grant_agent_mcp_egress.sh`'s `--bind-all-agents` mode constructs its
principal as:

```
principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_NUMBER}
```

— **org-scoped** (`org-${ORG_ID}`). Every trust-domain string elsewhere in
this repo (`terraform/modules/core/iam.tf`, `ops/register_agent_registry.sh`,
`deployment/deploy-mcp-fedex.sh`, section 6 above) uses **project-scoped**
`agents.global.proj-${PROJECT_NUMBER}.system.id.goog`, confirmed live by
reading it back from a real engine's `spec.effective_identity`. Both may be
legitimate — `proj-` for a single reasoning engine's own principal
(`principal://.../reasoningEngines/ID`), `org-` for a project-wide
`principalSet` bound at the organization level (their `--bind-all-agents`
mode) — but this is an unverified hypothesis, not a confirmed
reconciliation. Re-verify live before relying on either form for a new
binding shape.

### Small fix bundled in — turned out to be a no-op, checked not assumed

The reference `deploy_agent.py` sets `ADK_ENABLE_MCP_GRACEFUL_ERROR_HANDLING=true`
so a denied MCP call (gateway/IAP 403) fails the tool call fast instead of
hanging the agent turn on a broken-stream `TaskGroup`/`TimeoutError`.
Checked directly against this repo's installed `google-adk==2.4.0`
(`google/adk/features/_feature_registry.py`): `FeatureName._MCP_GRACEFUL_ERROR_HANDLING`
already has `default_on=True` in this version — confirmed independently by
the `[EXPERIMENTAL] feature FeatureName._MCP_GRACEFUL_ERROR_HANDLING is
enabled` warning `tests/integration/test_fedex_mcp_wiring.py` already emits
with no env var set. Setting the env var here would be a no-op; not added.
The reference repo pins `google-adk==1.34.0` (a different version line),
where the default may differ — worth rechecking only if this repo's ADK
pin ever changes.

## 8. Two eval bugs found and fixed while verifying the section 7 fix

Verifying the SA-impersonation fix live via
`tests/post_deploy/datasets/fedex_tracking_cases.json` surfaced two
unrelated, pre-existing bugs — one in `tests/eval_vertex.py`, one in the
root coordinator's routing instruction. Both fixed and live-verified (3/3
clean runs) against workshop-494016.

### 8.1 `tests/eval_vertex.py` dropped the `author` field on intermediate events

The first eval run after the section 7 fix landed failed with `400
INVALID_ARGUMENT ... turns[0].events[1].author / events[2].author: Required
field is not set`, even though inference itself had already succeeded
(confirmed via raw Cloud Logging). Root cause, found by reading the
installed `vertexai` SDK directly rather than guessing: `_run_custom_inference`
built each `intermediate_events` entry as `{"event_id": ..., "content":
...}`, dropping the top-level `author` field that
`google.adk.events.Event` always carries (confirmed live: a raw
`async_stream_query()` event dict has `author='customer_support'` right
there in its keys) and that the eval judge API's schema requires. Fix: add
`"author": evt.get("author", "")` alongside `event_id`/`content` in
`tests/eval_vertex.py`'s `intermediate.append(...)` call. This almost
certainly also affected `tool_use_quality_v1`/`final_response_quality_v1`
scoring for every other case in `tests/post_deploy/datasets/post_deploy_cases.json`
(same code path) — not just the new FedEx case — meaning the CI release
gate's judge-scored metrics may have been silently erroring project-wide
before this fix, not just for FedEx. Worth confirming next time the release
gate runs.

### 8.2 Root coordinator sometimes routed the FedEx query out-of-scope

After fixing 8.1, a second, intermittent failure appeared: some runs logged
`-> 1 events, 0 intermediate ...: I'm sorry, I can't help with that. I can
assist with products, orders, and billing.` — the root coordinator
classified "give me a live FedEx courier status update for tracking number
X" as OUT-OF-SCOPE instead of routing it to `order_agent`, so
`track_shipment` never got called (not a regression from anything in this
document — a pre-existing routing-instruction gap, just never exercised by
an eval case until now). `customer_support_mas/agents/root/agent.py`'s
ROUTING RULES rule 2 said only "ORDERS (tracking, history, delivery
status)", with no cue that FedEx/courier phrasing belongs there. Fix:
reworded to "ORDERS (tracking, history, delivery status, live FedEx/courier
status updates by tracking number)". Redeployed and re-ran the eval case 3
times post-fix: 3/3 clean `Tool Use Quality` + `Final Response Quality`
PASS at 1.0000. This is an LLM routing decision, not deterministic code, so
occasional misroutes can still recur — this fix reduces the failure rate,
it doesn't guarantee zero.
