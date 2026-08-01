#!/usr/bin/env bash
# Deploy the FedEx tracking MCP server to Cloud Run.
# Usage: bash deployment/deploy-mcp-fedex.sh [env-file]   (default .env)
set -euo pipefail

ENV_FILE="${1:-.env}"
PROJECT_ID=$(grep '^GOOGLE_CLOUD_PROJECT=' "$ENV_FILE" | cut -d= -f2-)
REGION=$(grep '^GOOGLE_CLOUD_LOCATION=' "$ENV_FILE" | cut -d= -f2- || echo us-central1)
SERVICE="fedex-tracking-mcp"
AR_REPO="customer-support"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}:latest"
FEDEX_MOCK="${FEDEX_MOCK:-true}"   # default mock mode until real creds are onboarded

echo "Building ${IMAGE} (context: repo root)..."
gcloud builds submit . \
  --project "$PROJECT_ID" \
  --config /dev/stdin <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-t", "${IMAGE}", "-f", "mcp_servers/fedex_tracking/Dockerfile", "."]
images: ["${IMAGE}"]
options:
  logging: CLOUD_LOGGING_ONLY
EOF

DEPLOY_ARGS=(
  --image="$IMAGE"
  --region="$REGION"
  --project="$PROJECT_ID"
  --platform=managed
  --no-allow-unauthenticated
  --port=8080
  --memory=512Mi
  --min-instances=0
  --max-instances=3
  --set-env-vars="FEDEX_MOCK=${FEDEX_MOCK}"
)
if [ "$FEDEX_MOCK" != "true" ]; then
  DEPLOY_ARGS+=(--set-secrets="FEDEX_CLIENT_ID=fedex-client-id:latest,FEDEX_CLIENT_SECRET=fedex-client-secret:latest")
fi

gcloud run deploy "$SERVICE" "${DEPLOY_ARGS[@]}"

# Grant the FedEx MCP invoker SA (terraform/modules/core/iam.tf's
# fedex_mcp_invoker, output as fedex_mcp_invoker_email) run.invoker on this
# service — NOT the Agent Identity principal directly. Cloud Run's OIDC
# invoker check rejects a token minted straight from an AGENT_IDENTITY
# engine's ADC (401 "could not be verified"); the invoker SA is a normal IAM
# principal Cloud Run already understands, and the agent's Agent Identity
# only ever holds roles/iam.serviceAccountTokenCreator on it (granted by
# Terraform). See docs/MCP_FEDEX.md section 7 for the full design.
INVOKER_SA=$(grep '^FEDEX_MCP_INVOKER_SA_EMAIL=' "$ENV_FILE" | cut -d= -f2- || true)

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')
echo ""
echo "Deployed: ${URL}"
echo "Set MCP_FEDEX_URL=${URL}/mcp in the env file and redeploy the Agent Engine to enable the toolset."

if [ -n "$INVOKER_SA" ]; then
  echo "Granting roles/run.invoker to invoker SA ${INVOKER_SA}..."
  gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" --project="$PROJECT_ID" \
    --member="serviceAccount:${INVOKER_SA}" \
    --role='roles/run.invoker'
else
  echo "FEDEX_MCP_INVOKER_SA_EMAIL is not set in ${ENV_FILE} — skipping the invoker binding."
  echo "Run 'terraform output fedex_mcp_invoker_email' (terraform/modules/core), set"
  echo "FEDEX_MCP_INVOKER_SA_EMAIL in ${ENV_FILE}, then grant it manually:"
  echo "  gcloud run services add-iam-policy-binding ${SERVICE} --region=${REGION} --project=${PROJECT_ID} \\"
  echo "    --member='serviceAccount:FEDEX_MCP_INVOKER_SA_EMAIL' \\"
  echo "    --role='roles/run.invoker'"
fi
