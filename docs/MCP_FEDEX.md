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
Two are **confirmed wrong** by live testing against a real deployed engine in
this repo (not just "unverifiable" — actually contradicted by observed
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

A third item found in this task is a **gap, not a confirmed bug** — it could
not be checked either way because the installed tooling doesn't cover it yet:

- This machine's `gcloud` is version 482.0.0 (latest available: 578.0.0; the
  "gcloud Preview Commands" component is not installed). `gcloud network-services
  agent-gateways --help`, `gcloud agent-registry --help`,
  `gcloud beta service-extensions authz-extensions --help`, and
  `gcloud beta network-security authz-policies --help` all return
  `Invalid choice` — these command groups don't exist in this SDK build at
  all, so their exact flag syntax could not be cross-checked against
  `--help` and was taken verbatim from the refs docs.
- `gcloud iap web add-iam-policy-binding --help` **does** exist on this
  machine, and its `--resource-type` only accepts `app-engine` or
  `backend-services` — no `agent-registry` choice, and no `--endpoint` flag
  at all. `ops/register_agent_registry.sh` still uses
  `--resource-type=agent-registry --endpoint=...` exactly as shown in
  `refs/scale/Route Agent Runtime traffic through Agent Gateway.md`, since
  that's the only source of truth available and the absence looks like an
  SDK-version gap (Preview feature not yet rolled into this build) rather
  than the refs doc being wrong. Re-verify with a current SDK
  (`gcloud components update`) before relying on this in a real project.

By decision, Agent Gateway and Agent Registry setup stay `gcloud`/REST
scripts (`ops/setup_agent_gateway.sh`, `ops/register_agent_registry.sh`), not
Terraform — there is no Terraform provider support for either resource type
as of this writing, and re-implementing Preview REST semantics in a
`google_*` custom resource isn't worth it for a surface expected to change
before GA.
