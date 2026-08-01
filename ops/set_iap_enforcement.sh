#!/usr/bin/env bash
# Toggle Agent Gateway's IAP REQUEST_AUTHZ extension between audit-only
# (DRY_RUN) and enforcing. Separated from ops/setup_agent_gateway.sh (which
# always deploys DRY_RUN on first setup) so flipping enforcement — and,
# critically, REVERTING it — is a single fast, deliberate command.
#
# DO NOT run `enforce` before ops/register_platform_endpoints.sh has been
# run successfully: the gateway default-denies any destination hostname
# absent from Agent Registry regardless of enforcement mode, and confirmed
# live in this repo that enforcing without the internal-hostname
# registrations breaks the engine's own internal platform calls (e.g.
# aiplatform.mtls.googleapis.com), not just unauthorized traffic. See
# docs/MCP_FEDEX.md section 7.
#
# The enforce/dry-run distinction is a single YAML field, per the
# cloudnet-agent-gateway codelab (terraform/terraform.tfvars':
# agent_gateway_iap_iam_enforcement_mode = "DRY_RUN" | null — "Once we have
# validated our deployment we will update the enforcement mode to null to
# enforce the policies"): metadata.iamEnforcementMode = "DRY_RUN" for
# audit-only, or the key OMITTED ENTIRELY (not set to some other string) to
# enforce. Re-importing the authz-extension with the updated YAML is what
# applies the change — same import command either way.
#
# Usage: bash ops/set_iap_enforcement.sh [env-file] dry-run|enforce
set -euo pipefail

ENV_FILE="${1:-.env}"
MODE="${2:-}"
AUTHZ_EXTENSION_NAME="customer-support-iap-authz-ext"

if [ "$MODE" != "dry-run" ] && [ "$MODE" != "enforce" ]; then
  echo "Usage: bash ops/set_iap_enforcement.sh [env-file] dry-run|enforce" >&2
  exit 1
fi

PROJECT_ID=$(grep '^GOOGLE_CLOUD_PROJECT=' "$ENV_FILE" | cut -d= -f2-)
REGION=$(grep '^GOOGLE_CLOUD_LOCATION=' "$ENV_FILE" | cut -d= -f2- || echo us-central1)

if [ -z "$PROJECT_ID" ]; then
  echo "Error: GOOGLE_CLOUD_PROJECT is not set in $ENV_FILE." >&2
  exit 1
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

if [ "$MODE" = "dry-run" ]; then
  cat >"${WORKDIR}/iap-request-authz-extension.yaml" <<EOF
name: ${AUTHZ_EXTENSION_NAME}
service: iap.googleapis.com
failOpen: true
timeout: 1s
metadata:
  iamEnforcementMode: "DRY_RUN"
  iapPolicyVersion: "V1"
EOF
  echo "Setting IAP authz extension to DRY_RUN (audit-only, nothing blocked)..."
else
  cat >"${WORKDIR}/iap-request-authz-extension.yaml" <<EOF
name: ${AUTHZ_EXTENSION_NAME}
service: iap.googleapis.com
failOpen: true
timeout: 1s
metadata:
  iapPolicyVersion: "V1"
EOF
  echo "Setting IAP authz extension to ENFORCE (unauthorized/unregistered egress will be blocked)..."
  echo "Make sure ops/register_platform_endpoints.sh has been run successfully first."
fi

gcloud beta service-extensions authz-extensions import "$AUTHZ_EXTENSION_NAME" \
  --source="${WORKDIR}/iap-request-authz-extension.yaml" \
  --location="$REGION" \
  --project="$PROJECT_ID"

echo ""
echo "Verifying..."
gcloud beta service-extensions authz-extensions describe "$AUTHZ_EXTENSION_NAME" \
  --location="$REGION" \
  --project="$PROJECT_ID" \
  --format="value(metadata)"

if [ "$MODE" = "enforce" ]; then
  echo ""
  echo "ENFORCING NOW. Test immediately:"
  echo "  1. FedEx MCP call still works (order agent track_shipment)."
  echo "  2. The engine's own internal platform calls still work (aiplatform, logging, etc.)."
  echo "If EITHER breaks, revert immediately:"
  echo "  bash ops/set_iap_enforcement.sh ${ENV_FILE} dry-run"
fi
