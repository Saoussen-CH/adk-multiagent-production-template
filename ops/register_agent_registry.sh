#!/usr/bin/env bash
# Register the Agent Engine and the fedex-tracking-mcp server with Agent Registry,
# then grant the agent identity IAP-egressor access to the MCP endpoint so the
# egress gateway (ops/setup_agent_gateway.sh) actually lets traffic through.
#
# Preview feature. Commands are copied (with placeholders substituted for
# project/region/service values) from:
#   - refs/scale/Route Agent Runtime traffic through Agent Gateway.md
#     (step 3: `gcloud agent-registry services create`; step 4: `gcloud iap web
#     add-iam-policy-binding --resource-type=agent-registry`). This is the only
#     file of the three refs docs that contains literal gcloud syntax for
#     registration — refs/Govern/Agent Registry.md is an index page with no
#     runnable commands (it links out to "Register agents" / "Register MCP
#     servers" pages that were not mirrored locally), so both registrations
#     below reuse this one documented command shape.
#
# UPDATED after upgrading to SDK 578.0.0 (was 482.0.0) and running this LIVE
# against a real project (workshop-494016). Three real findings, confirmed
# live, not from docs:
#
#   1. `--interfaces` shorthand is `protocolBinding=X,url=Y` (no brackets, no
#      quotes) or proper JSON `'[{"protocolBinding": "X", "url": "Y"}]'` — the
#      original script used neither. Fixed to shorthand form.
#
#   2. `--mcp-server-spec-type=no-spec` creates a raw Service entry that is
#      NEVER projected into a discoverable McpServer resource — confirmed by
#      `gcloud agent-registry mcp-servers describe <service-id>` returning
#      NOT_FOUND immediately (not a propagation delay: polled 8x over
#      ~2.5min, identical error every time). `gcloud iap web
#      add-iam-policy-binding --mcp-server=<service-id>` fails the same way,
#      because that flag also resolves against the *projected* resource, not
#      the raw Service. Providing real spec content (`--mcp-server-spec-type=
#      tool-spec --mcp-server-spec-content=<tools/list-shaped JSON>`) DOES
#      project a resource — but at a system-generated ID
#      (`agentregistry-00000000-...`), never the Service ID you chose. The
#      real ID only appears in the `registryResource:` field of the
#      create/update response.
#
#      This script therefore registers the MCP server with real tool-spec
#      content (we know its schema — one tool, `track_shipment`) and captures
#      the generated `registryResource` ID for the IAM binding.
#
#      CORRECTION (re-verified live ~6 hours after the original test, same
#      project): `--agent-spec-type=no-spec` DOES eventually project a
#      discoverable `agents/<generated-id>` resource — `gcloud agent-registry
#      agents describe` on "customer-support-order-agent"'s generated ID
#      returned full content (protocols, interfaces), not NOT_FOUND. The
#      original claim that it never projects was based on an immediate
#      (~2.5min) poll; whether the true cause is longer eventual-consistency
#      latency for `agent-spec-type` specifically (vs. `mcp-server-spec-type`,
#      not re-tested at this delay) is unconfirmed. Net effect: the order
#      agent's own no-spec registration IS registry-discoverable — this does
#      not change the egress IAM binding below (its `--member` is the order
#      agent's SPIFFE principal string, addressed directly, not via a
#      registry resource ID), but the discoverability gap this finding
#      previously cited as an A2A-migration argument does not hold as
#      stated; treat with more caution than the parts of this file marked
#      "confirmed live."
#
#   3. `gcloud iap web add-iam-policy-binding --resource-type=agent-registry`
#      DOES exist on 578.0.0 (confirmed live) — the previous "could not
#      verify" caveat is resolved. `iap.googleapis.com` must be explicitly
#      enabled (added to ops/setup_agent_gateway.sh's API list) — it is not
#      implied by the other Agent Gateway APIs and its absence produces a
#      confusing SERVICE_DISABLED error at binding time, not at setup time.
#
# Usage: bash ops/register_agent_registry.sh [env-file]
set -euo pipefail

ENV_FILE="${1:-.env}"
PROJECT_ID=$(grep '^GOOGLE_CLOUD_PROJECT=' "$ENV_FILE" | cut -d= -f2-)
REGION=$(grep '^GOOGLE_CLOUD_LOCATION=' "$ENV_FILE" | cut -d= -f2- || echo us-central1)
ENGINE_RESOURCE=$(grep '^AGENT_ENGINE_RESOURCE_NAME=' "$ENV_FILE" | cut -d= -f2-)
MCP_FEDEX_URL=$(grep '^MCP_FEDEX_URL=' "$ENV_FILE" | cut -d= -f2-)

if [ -z "$PROJECT_ID" ]; then
  echo "Error: GOOGLE_CLOUD_PROJECT is not set in $ENV_FILE." >&2
  exit 1
fi
if [ -z "$ENGINE_RESOURCE" ]; then
  echo "Error: AGENT_ENGINE_RESOURCE_NAME is not set in $ENV_FILE. Deploy the Agent Engine first (make deploy-agent-engine)." >&2
  exit 1
fi
if [ -z "$MCP_FEDEX_URL" ]; then
  echo "Error: MCP_FEDEX_URL is not set in $ENV_FILE. Run 'make deploy-mcp-fedex ENV=...' first, then set MCP_FEDEX_URL and redeploy the engine." >&2
  exit 1
fi

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# ------------------------------------------------------------------------------
# 1. Register the Agent Engine (order agent) as an Agent Registry service entry.
# no-spec is the only option available (not an A2A agent yet) — this entry
# stays unprojected/undiscoverable as an `Agent` resource (finding #2 above).
# Registered anyway for basic listing/audit visibility in the console.
# ------------------------------------------------------------------------------
# Interface URL is the engine's own Vertex AI resource path, NOT a bare
# "https://${REGION}-aiplatform.googleapis.com" host — Agent Registry
# enforces one-service-per-interface-URL project/region-wide, and the bare
# host collides with ops/register_platform_endpoints.sh's locational
# aiplatform endpoint registration (confirmed live: that script's `create`
# failed with "Interface URL '...' is already in use by another service"
# until this was fixed).
echo "Registering the Agent Engine with Agent Registry..."
if ! gcloud agent-registry services describe "customer-support-order-agent" --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  gcloud agent-registry services create "customer-support-order-agent" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --display-name="Customer Support Order Agent (Agent Engine)" \
    --agent-spec-type=no-spec \
    --interfaces="protocolBinding=jsonrpc,url=https://${REGION}-aiplatform.googleapis.com/v1/${ENGINE_RESOURCE}"
else
  echo "  Already registered, skipping create."
fi
echo "Registered agent service: customer-support-order-agent (no-spec — not projected as a discoverable Agent resource, see finding #2)"

# ------------------------------------------------------------------------------
# 2. Register the fedex-tracking-mcp Cloud Run service as an Agent Registry
# service entry — WITH real tool-spec content, not no-spec (finding #2). This
# is what actually projects a discoverable McpServer resource, which is what
# the IAM binding in step 3 needs to reference. Tool schema matches
# mcp_servers/fedex_tracking/server.py's track_shipment tool exactly.
# ------------------------------------------------------------------------------
echo "Registering fedex-tracking-mcp with Agent Registry (tool-spec)..."
TOOLS_JSON='{"tools":[{"name":"track_shipment","description":"Get live FedEx tracking status for a shipment.","inputSchema":{"type":"object","properties":{"tracking_number":{"type":"string","description":"The FedEx tracking number"}},"required":["tracking_number"]}}]}'
if gcloud agent-registry services describe "fedex-tracking-mcp" --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  UPDATE_OUTPUT=$(gcloud agent-registry services update "fedex-tracking-mcp" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content="$TOOLS_JSON" \
    --format="value(registryResource)")
else
  UPDATE_OUTPUT=$(gcloud agent-registry services create "fedex-tracking-mcp" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --display-name="FedEx Tracking MCP Server" \
    --interfaces="protocolBinding=jsonrpc,url=${MCP_FEDEX_URL}" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content="$TOOLS_JSON" \
    --format="value(registryResource)")
fi
# registryResource is a full path: projects/.../locations/.../mcpServers/<generated-id>
# — extract just the trailing ID for the IAM binding's --mcp-server flag.
MCP_PROJECTED_ID=$(basename "$UPDATE_OUTPUT")
echo "Registered MCP server: fedex-tracking-mcp (projected as ${MCP_PROJECTED_ID})"

# ------------------------------------------------------------------------------
# 3. Grant the agent identity principal roles/iap.egressor on the MCP server's
# PROJECTED resource ID (not the Service ID we chose — finding #2). Agent
# Gateway default-denies all egress until this binding exists.
# MEMBER format (principal://TRUST_DOMAIN/resources/aiplatform/ENGINE_RESOURCE)
# — trust domain is agents.global.proj-PROJECT_NUMBER..., NOT
# agents.global.project-PROJECT_NUMBER... as Google's docs claim elsewhere —
# one of the two confirmed-wrong doc bugs in this repo (see
# terraform/modules/core/iam.tf and CLAUDE.md).
# `iap.googleapis.com` must be enabled — done in ops/setup_agent_gateway.sh.
# ------------------------------------------------------------------------------
MEMBER="principal://agents.global.proj-${PROJECT_NUMBER}.system.id.goog/resources/aiplatform/${ENGINE_RESOURCE}"
echo "Granting roles/iap.egressor to ${MEMBER} on ${MCP_PROJECTED_ID}..."
gcloud iap web add-iam-policy-binding \
  --resource-type=agent-registry \
  --mcp-server="$MCP_PROJECTED_ID" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --member="$MEMBER" \
  --role=roles/iap.egressor

# ------------------------------------------------------------------------------
# 4. Verify (describe/list verbs follow the same --project/--location flag
# convention as the `create` call above; refs does not show a literal
# describe/list example for agent-registry services).
# ------------------------------------------------------------------------------
echo ""
echo "Verifying registry entries..."
gcloud agent-registry services describe "fedex-tracking-mcp" \
  --project="$PROJECT_ID" \
  --location="$REGION"

gcloud agent-registry services describe "customer-support-order-agent" \
  --project="$PROJECT_ID" \
  --location="$REGION"

echo ""
echo "Verifying the MCP server actually projected (expect tools: [track_shipment], not NOT_FOUND)..."
gcloud agent-registry mcp-servers describe "$MCP_PROJECTED_ID" \
  --project="$PROJECT_ID" \
  --location="$REGION"

echo ""
echo "Verifying the IAP egressor binding..."
gcloud iap web get-iam-policy \
  --resource-type=agent-registry \
  --mcp-server="$MCP_PROJECTED_ID" \
  --region="$REGION" \
  --project="$PROJECT_ID"

echo ""
echo "Done. The gateway is deployed in audit-only (dry-run IAP) mode by default"
echo "(ops/setup_agent_gateway.sh) — traffic is logged, not blocked, until you"
echo "flip iamEnforcementMode away from DRY_RUN. See docs/MCP_FEDEX.md."
