#!/usr/bin/env bash
# Register internal Google API hostnames in Agent Registry as spec-less
# `endpoints`, and grant the project's Agent Identity principalSet
# roles/iap.egressor on each. This is the missing prerequisite for safely
# flipping Agent Gateway's IAP authz extension out of DRY_RUN
# (ops/setup_agent_gateway.sh): confirmed live that enforcing without this
# breaks the engine's own internal platform calls (e.g.
# aiplatform.mtls.googleapis.com) because the gateway default-denies any
# destination hostname not present in the registry, regardless of
# enforcement mode. See docs/MCP_FEDEX.md section 7 ("Second gap") for the
# full root-cause writeup.
#
# API list and the 5-URL-variant-per-API scheme (global, mTLS, locational,
# locational-mTLS, regional/REP) are copied from Google's own reference
# implementation: the cloudnet-agent-gateway codelab's
# terraform/modules/agent-registry-endpoints/{main,variables}.tf in
# GoogleCloudPlatform/cloud-networking-solutions. That module uses native
# Terraform resources (google_agent_registry_service with
# endpoint_spec.type = NO_SPEC) — unavailable to this repo without a
# hashicorp/google provider major-version bump (v7.42.0 has these resources,
# the v6.50.0 already applied to dev's live state does not; see
# docs/MCP_FEDEX.md section 6). This script reimplements the same outcome
# via gcloud/REST, consistent with ops/register_agent_registry.sh.
#
# KEY FINDING, confirmed live, not assumed from docs: `--endpoint-spec-type`
# is a THIRD spec-type flag (distinct from --agent-spec-type and
# --mcp-server-spec-type), and unlike `--agent-spec-type=no-spec`
# (ops/register_agent_registry.sh finding #2 — NEVER projects a discoverable
# resource), `--endpoint-spec-type=no-spec` DOES project a real, discoverable
# `endpoints` resource at a system-generated ID
# (agentregistry-00000000-...), same pattern as MCP servers registered with
# real tool-spec content. Verified via `gcloud agent-registry endpoints
# describe <generated-id>` returning the registered interface, not
# NOT_FOUND. `gcloud iap web add-iam-policy-binding --endpoint=<id>` is a
# real, distinct flag (sibling to --agent/--mcp-server) and was confirmed
# live to accept the binding.
#
# Usage: bash ops/register_platform_endpoints.sh [env-file]
set -euo pipefail

ENV_FILE="${1:-.env}"
PROJECT_ID=$(grep '^GOOGLE_CLOUD_PROJECT=' "$ENV_FILE" | cut -d= -f2-)
REGION=$(grep '^GOOGLE_CLOUD_LOCATION=' "$ENV_FILE" | cut -d= -f2- || echo us-central1)

if [ -z "$PROJECT_ID" ]; then
  echo "Error: GOOGLE_CLOUD_PROJECT is not set in $ENV_FILE." >&2
  exit 1
fi

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
# Project-scoped trust domain, matching every other binding in this repo
# (terraform/modules/core/iam.tf, ops/register_agent_registry.sh) — this
# project has no parent organization, so the org-scoped form the codelab's
# own grant_agent_mcp_egress.sh uses does not apply here. See
# docs/MCP_FEDEX.md section 7's "Trust domain discrepancy" note.
MEMBER="principalSet://agents.global.proj-${PROJECT_NUMBER}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_NUMBER}"

# API id -> display name. Copied verbatim from the codelab's
# agent-registry-endpoints module default for var.google_apis.
declare -A APIS=(
  [aiplatform]="Vertex AI Platform"
  [cloudresourcemanager]="Cloud Resource Manager"
  [global-discoveryengine]="Global Discovery Engine"
  [discoveryengine]="Discovery Engine"
  [logging]="Logging"
  [monitoring]="Monitoring"
  [oauth2]="OAuth2"
  [telemetry]="Telemetry"
  [trace]="Trace"
  [agentregistry]="Agent Registry"
  [iap]="Identity-Aware Proxy"
  [iamcredentials]="IAM Credentials"
)

REGISTERED=0
GRANTED=0

# register_one <service_id> <display_name> <url>
# Idempotent create/update of one endpoint, then grant roles/iap.egressor on
# its projected resource ID.
register_one() {
  local service_id="$1" display_name="$2" url="$3"
  local registry_resource endpoint_id

  echo "--- ${service_id} (${url}) ---"
  if gcloud agent-registry services describe "$service_id" --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
    registry_resource=$(gcloud agent-registry services update "$service_id" \
      --project="$PROJECT_ID" --location="$REGION" \
      --endpoint-spec-type=no-spec \
      --interfaces="protocolBinding=jsonrpc,url=${url}" \
      --format="value(registryResource)")
  else
    registry_resource=$(gcloud agent-registry services create "$service_id" \
      --project="$PROJECT_ID" --location="$REGION" \
      --display-name="$display_name" \
      --endpoint-spec-type=no-spec \
      --interfaces="protocolBinding=jsonrpc,url=${url}" \
      --format="value(registryResource)")
  fi
  endpoint_id=$(basename "$registry_resource")
  REGISTERED=$((REGISTERED + 1))

  gcloud iap web add-iam-policy-binding \
    --resource-type=agent-registry \
    --endpoint="$endpoint_id" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --member="$MEMBER" \
    --role=roles/iap.egressor >/dev/null
  GRANTED=$((GRANTED + 1))
  echo "  registered + granted: ${endpoint_id}"
}

for id in "${!APIS[@]}"; do
  name="${APIS[$id]}"

  # Base id variant — pad service_id to the 4-63 char / [a-z][a-z0-9-]* rule
  # when the raw id is under 4 chars (only "iap" in this list); the URL host
  # stays the real API id regardless.
  base_service_id="$id"
  if [ "${#id}" -lt 4 ]; then
    base_service_id="${id}-endpoint"
  fi
  register_one "$base_service_id" "$name" "https://${id}.googleapis.com"

  register_one "${id}-mtls" "${name} mTLS" "https://${id}.mtls.googleapis.com"
  register_one "${REGION}-${id}" "${name} Locational" "https://${REGION}-${id}.googleapis.com"
  register_one "${REGION}-${id}-mtls" "${name} Locational mTLS" "https://${REGION}-${id}.mtls.googleapis.com"
  register_one "${id}-${REGION}-rep" "${name} Regional (REP)" "https://${id}.${REGION}.rep.googleapis.com"
done

echo ""
echo "Done. Registered/updated ${REGISTERED} endpoints, granted roles/iap.egressor on ${GRANTED}."
echo "This is the prerequisite for ops/setup_agent_gateway.sh's IAP authz extension"
echo "to be safely flipped out of DRY_RUN — see docs/MCP_FEDEX.md section 7."
