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
# Known discrepancy: on this machine, `gcloud iap web add-iam-policy-binding --help`
# only accepts --resource-type=app-engine|backend-services (no `agent-registry`
# choice, no `--endpoint` flag) — the installed SDK (482.0.0) predates this
# Preview resource type entirely (latest available is 578.0.0; the "gcloud
# Preview Commands" component isn't installed either). This is NOT the same
# class of bug as the two confirmed-wrong doc claims (trust domain, bool env
# var) — it could not be verified either way here. Script follows refs as
# written. See docs/MCP_FEDEX.md "Known Preview caveats".
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
# Command shape: refs/scale/Route Agent Runtime traffic through Agent Gateway.md,
# step 3.
# ------------------------------------------------------------------------------
echo "Registering the Agent Engine with Agent Registry..."
AGENT_ENDPOINT_ID=$(gcloud agent-registry services create "customer-support-order-agent" \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --display-name="Customer Support Order Agent (Agent Engine)" \
  --endpoint-spec-type=no-spec \
  --interfaces="[{url=\"https://${REGION}-aiplatform.googleapis.com\",protocolBinding=\"jsonrpc\"}]" \
  --format="value(registryResource)")
echo "Registered agent endpoint: ${AGENT_ENDPOINT_ID}"

# ------------------------------------------------------------------------------
# 2. Register the fedex-tracking-mcp Cloud Run service as an Agent Registry
# service entry. Same command shape as step 1, pointed at the deployed MCP URL.
# protocolBinding="jsonrpc" matches the actual wire protocol (MCP over
# streamable HTTP is JSON-RPC 2.0 — confirmed against the deployed server's own
# `initialize` response during Task 3's smoke test).
# ------------------------------------------------------------------------------
echo "Registering fedex-tracking-mcp with Agent Registry..."
MCP_ENDPOINT_ID=$(gcloud agent-registry services create "fedex-tracking-mcp" \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --display-name="FedEx Tracking MCP Server" \
  --endpoint-spec-type=no-spec \
  --interfaces="[{url=\"${MCP_FEDEX_URL}\",protocolBinding=\"jsonrpc\"}]" \
  --format="value(registryResource)")
echo "Registered MCP endpoint: ${MCP_ENDPOINT_ID}"

# ------------------------------------------------------------------------------
# 3. Grant the agent identity principal roles/iap.egressor on the MCP endpoint.
# Agent Gateway default-denies all egress until this binding exists (refs/Govern/
# Agent Gateway/Set up Agent Gateway.md, "Set up agent identity and permissions").
# Command: refs/scale/Route Agent Runtime traffic through Agent Gateway.md, step 4.
# MEMBER format (principal://TRUST_DOMAIN/resources/aiplatform/ENGINE_RESOURCE)
# is from that same step. Trust domain is agents.global.proj-PROJECT_NUMBER...,
# NOT agents.global.project-PROJECT_NUMBER... as Google's docs claim elsewhere —
# this is one of the two confirmed-wrong doc bugs in this repo (see
# terraform/modules/core/iam.tf and CLAUDE.md); applied here for consistency
# even though this specific refs doc doesn't spell out "proj-" vs "project-"
# itself.
# ------------------------------------------------------------------------------
MEMBER="principal://agents.global.proj-${PROJECT_NUMBER}.system.id.goog/resources/aiplatform/${ENGINE_RESOURCE}"
echo "Granting roles/iap.egressor to ${MEMBER} on ${MCP_ENDPOINT_ID}..."
gcloud iap web add-iam-policy-binding \
  --resource-type=agent-registry \
  --endpoint="$MCP_ENDPOINT_ID" \
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
echo "Done. The gateway is deployed in audit-only (dry-run IAP) mode by default"
echo "(ops/setup_agent_gateway.sh) — traffic is logged, not blocked, until you"
echo "flip iamEnforcementMode away from DRY_RUN. See docs/MCP_FEDEX.md."
