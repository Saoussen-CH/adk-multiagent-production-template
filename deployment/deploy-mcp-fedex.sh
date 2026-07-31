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

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')
echo ""
echo "Deployed: ${URL}"
echo "Set MCP_FEDEX_URL=${URL}/mcp in the env file and redeploy the Agent Engine to enable the toolset."
echo "Grant the agent identity invoker access (see docs/MCP_FEDEX.md):"
echo "  gcloud run services add-iam-policy-binding ${SERVICE} --region=${REGION} --project=${PROJECT_ID} \\"
echo "    --member='principalSet://agents.global.proj-PROJECT_NUMBER.system.id.goog/attribute.platformContainer/aiplatform/projects/PROJECT_NUMBER' \\"
echo "    --role='roles/run.invoker'"
