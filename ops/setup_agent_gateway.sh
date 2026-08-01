#!/usr/bin/env bash
# Create an egress-mode Agent Gateway and attach the existing Agent Engine to it.
#
# Preview feature. Commands are copied (with placeholders substituted for
# project/region/service values) from:
#   - refs/Govern/Agent Gateway/Set up Agent Gateway.md
#     ("Configure Agent Gateway in Agent-to-Anywhere (egress) mode" > gcloud tab,
#     plus the "Required APIs" and "Set up agent identity and permissions" sections)
#   - refs/scale/Route Agent Runtime traffic through Agent Gateway.md
#     ("For existing agents" > "Agent-to-Anywhere" REST PATCH, and the final
#     verification GET)
#
# Known discrepancy vs the task brief's own pseudocode sketch: the brief assumed
# a top-level `agentGatewayConfig` field with `update_mask=agent_gateway_config`.
# The refs Route doc (the actual source of truth) shows the field is nested
# under `spec.deploymentSpec.agentGatewayConfig`, and the query param is
# `updateMask` (camelCase) with value `spec.deploymentSpec.agentGatewayConfig`.
# This script follows the refs doc, not the brief's sketch. See
# docs/MCP_FEDEX.md "Known Preview caveats" for the full discrepancy note,
# including why this could not be cross-checked against `gcloud ... --help` on
# this machine (the installed SDK predates these command groups entirely).
#
# Usage: bash ops/setup_agent_gateway.sh [env-file]
set -euo pipefail

ENV_FILE="${1:-.env}"
PROJECT_ID=$(grep '^GOOGLE_CLOUD_PROJECT=' "$ENV_FILE" | cut -d= -f2-)
REGION=$(grep '^GOOGLE_CLOUD_LOCATION=' "$ENV_FILE" | cut -d= -f2- || echo us-central1)
ENGINE_RESOURCE=$(grep '^AGENT_ENGINE_RESOURCE_NAME=' "$ENV_FILE" | cut -d= -f2-)
GATEWAY_NAME="customer-support-egress"
AUTHZ_EXTENSION_NAME="customer-support-iap-authz-ext"
AUTHZ_POLICY_NAME="customer-support-iap-authz-policy"

if [ -z "$PROJECT_ID" ]; then
  echo "Error: GOOGLE_CLOUD_PROJECT is not set in $ENV_FILE." >&2
  exit 1
fi
if [ -z "$ENGINE_RESOURCE" ]; then
  echo "Error: AGENT_ENGINE_RESOURCE_NAME is not set in $ENV_FILE. Deploy the Agent Engine first (make deploy-agent-engine)." >&2
  exit 1
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# ------------------------------------------------------------------------------
# 1. Enable required APIs.
# List copied from refs/Govern/Agent Gateway/Set up Agent Gateway.md, "Required APIs".
# ------------------------------------------------------------------------------
echo "Enabling required APIs for Agent Gateway..."
# Split into two calls: `gcloud services enable` rejects batches > 20
# (confirmed live: SU_MAX_BATCH_SIZE_EXCEEDED at 21).
gcloud services enable \
  compute.googleapis.com \
  networksecurity.googleapis.com \
  networkservices.googleapis.com \
  dns.googleapis.com \
  iam.googleapis.com \
  agentregistry.googleapis.com \
  aiplatform.googleapis.com \
  discoveryengine.googleapis.com \
  storage.googleapis.com \
  modelarmor.googleapis.com \
  observability.googleapis.com \
  telemetry.googleapis.com \
  monitoring.googleapis.com \
  cloudtrace.googleapis.com \
  logging.googleapis.com \
  apphub.googleapis.com \
  apptopology.googleapis.com \
  cloudapiregistry.googleapis.com \
  notebooks.googleapis.com \
  iap.googleapis.com \
  --project="$PROJECT_ID"
gcloud services enable \
  texttospeech.googleapis.com \
  dataform.googleapis.com \
  --project="$PROJECT_ID"

# ------------------------------------------------------------------------------
# 2. Create the Agent Gateway resource in Agent-to-Anywhere (egress) mode.
# YAML shape + import command: refs/Govern/Agent Gateway/Set up Agent Gateway.md,
# "Configure Agent Gateway in Agent-to-Anywhere (egress) mode" > gcloud, steps 1-2.
# The registry path format (//agentregistry.googleapis.com/projects/.../locations/...)
# is documented there too; the regional registry itself is provisioned automatically
# per project/region (Agent Registry has no separate "create registry" command in
# refs/Govern/Agent Registry.md — only IAM/API setup is required).
# ------------------------------------------------------------------------------
AGENT_REGISTRY_PATH="//agentregistry.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}"
cat >"${WORKDIR}/my-agent-gateway-egress.yaml" <<EOF
name: ${GATEWAY_NAME}
protocols:
  - MCP
googleManaged:
  governedAccessPath: AGENT_TO_ANYWHERE
registries:
  - ${AGENT_REGISTRY_PATH}
EOF

echo "Creating Agent Gateway ${GATEWAY_NAME} (Agent-to-Anywhere / egress) in ${REGION}..."
gcloud network-services agent-gateways import "$GATEWAY_NAME" \
  --source="${WORKDIR}/my-agent-gateway-egress.yaml" \
  --location="$REGION" \
  --project="$PROJECT_ID"

# ------------------------------------------------------------------------------
# 3. Attach an authorization policy backed by IAP, deployed in dry-run
# (audit-only) mode first, per refs' recommendation.
# YAML + import commands: refs/Govern/Agent Gateway/Set up Agent Gateway.md,
# "gcloud" tab, step 3 (sub-steps 1-4).
# ------------------------------------------------------------------------------
cat >"${WORKDIR}/iap-request-authz-extension.yaml" <<EOF
name: ${AUTHZ_EXTENSION_NAME}
service: iap.googleapis.com
failOpen: true
timeout: 1s
metadata:
  iamEnforcementMode: "DRY_RUN"
  iapPolicyVersion: "V1"
EOF

echo "Importing IAP authorization extension (audit-only / dry-run mode)..."
gcloud beta service-extensions authz-extensions import "$AUTHZ_EXTENSION_NAME" \
  --source="${WORKDIR}/iap-request-authz-extension.yaml" \
  --location="$REGION" \
  --project="$PROJECT_ID"

cat >"${WORKDIR}/iap-request-authz-policy.yaml" <<EOF
name: ${AUTHZ_POLICY_NAME}
target:
  resources:
    - "projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${GATEWAY_NAME}"
policyProfile: REQUEST_AUTHZ
action: CUSTOM
customProvider:
  authzExtension:
    resources:
      - "projects/${PROJECT_ID}/locations/${REGION}/authzExtensions/${AUTHZ_EXTENSION_NAME}"
EOF

echo "Importing IAP authorization policy..."
gcloud beta network-security authz-policies import "$AUTHZ_POLICY_NAME" \
  --source="${WORKDIR}/iap-request-authz-policy.yaml" \
  --location="$REGION" \
  --project="$PROJECT_ID"

# ------------------------------------------------------------------------------
# 4. Attach the existing Agent Engine to the gateway (Agent-to-Anywhere / egress).
# PATCH body, field casing (spec.deploymentSpec.agentGatewayConfig.agentToAnywhereConfig)
# and updateMask: refs/scale/Route Agent Runtime traffic through Agent Gateway.md,
# "For existing agents" > "Agent-to-Anywhere".
# Per that same doc's NOTE: this PATCH does NOT change identity_type. The engine
# must already have been created with identity_type=AGENT_IDENTITY (spec D2) for
# Semantic Governance Policies to see it; that cannot be retrofitted here.
# ------------------------------------------------------------------------------
echo "Attaching Agent Engine ${ENGINE_RESOURCE} to gateway ${GATEWAY_NAME}..."
curl -X PATCH \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d "{
    \"spec\": {
      \"deploymentSpec\": {
        \"agentGatewayConfig\": {
          \"agentToAnywhereConfig\": {
            \"agentGateway\": \"projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${GATEWAY_NAME}\"
          }
        }
      }
    }
  }" \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/${ENGINE_RESOURCE}?updateMask=spec.deploymentSpec.agentGatewayConfig"

# ------------------------------------------------------------------------------
# 5. Verify.
# Gateway describe: refs/scale/... "Configure custom container (BYOC)" section
# (same describe verb, used there to pull the gateway's CA cert).
# Engine attachment GET: refs/scale/Route Agent Runtime traffic through Agent
# Gateway.md, "Verify your agent configuration" > gcloud tab. If the jq output
# is null, Runtime failed to bind to the gateway.
# ------------------------------------------------------------------------------
echo ""
echo "Verifying gateway resource..."
gcloud network-services agent-gateways describe "$GATEWAY_NAME" \
  --location="$REGION" \
  --project="$PROJECT_ID"

echo ""
echo "Verifying engine attachment (expect agentToAnywhereConfig.agentGateway, not null)..."
# python3 instead of jq: jq is not guaranteed to be installed (confirmed
# missing on this machine); python3 is a hard dependency of this repo already.
curl -s -X GET \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/${ENGINE_RESOURCE}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('spec',{}).get('deploymentSpec',{}).get('agentGatewayConfig'), indent=2))"

echo ""
echo "Gateway is in default-deny egress mode: nothing can be reached until it is"
echo "registered in Agent Registry and granted roles/iap.egressor. Next:"
echo "  bash ops/register_agent_registry.sh ${ENV_FILE}"
