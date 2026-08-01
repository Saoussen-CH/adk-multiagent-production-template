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
publicly reachable — only the agent identity, once granted, can invoke it).
It prints the deployed URL and the IAM invoker-binding command needed for
the Agent Engine's identity to call it directly (bypassing the gateway) or
as a fallback if the gateway path in section 4 isn't set up yet:

```bash
gcloud run services add-iam-policy-binding fedex-tracking-mcp \
  --region=REGION --project=PROJECT_ID \
  --member='principalSet://agents.global.proj-PROJECT_NUMBER.system.id.goog/attribute.platformContainer/aiplatform/projects/PROJECT_NUMBER' \
  --role='roles/run.invoker'
```

Note the trust domain: `agents.global.proj-PROJECT_NUMBER...`, **not**
`agents.global.project-PROJECT_NUMBER...` — see section 6.

After deploying:

1. Set `MCP_FEDEX_URL=<printed-url>/mcp` in the target env file
   (`.env` / `.env.staging` / `.env.prod`).
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

Governance is scripted, not Terraform (Preview `gcloud`/REST surfaces have no
Terraform provider support yet — this is an explicit decision, see section 6).

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
anything. Flip `iamEnforcementMode` to enforce once you've confirmed the
audit log looks right; re-run `gcloud beta service-extensions
authz-extensions import` with the updated YAML to apply the change.

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
- **`--mcp-server-spec-type=no-spec` / `--agent-spec-type=no-spec` never
  project a discoverable resource.** `gcloud agent-registry services create
  ... --mcp-server-spec-type=no-spec` succeeds and creates a raw "Service"
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
  The order agent has no equivalent fix in Phase 1 — it isn't an A2A agent,
  so there's no real agent-card content to provide, `no-spec` is the only
  option, and it stays unprojected/undiscoverable as an `Agent` resource.
  This doesn't block the egress binding itself (the binding's `--member` is
  the order agent's SPIFFE principal string, addressed directly, not via a
  registry resource ID) — it only affects registry-based *discovery* of the
  order agent by other agents/tooling. Live confirmation that A2A migration
  (spec Phase 2) is what actually unlocks full registry discoverability, not
  just an architectural nicety.

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
Terraform — there is no Terraform provider support for either resource type
as of this writing, and re-implementing Preview REST semantics in a
`google_*` custom resource isn't worth it for a surface expected to change
before GA.
