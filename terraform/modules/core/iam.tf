# ==============================================================================
# IAM — google_project_iam_member (additive, does not replace existing bindings)
# ==============================================================================

# ------------------------------------------------------------------------------
# Vertex AI Agent Engine service account
# Runs deployed agents; needs Firestore to call tools and Vertex AI for Gemini.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "agent_engine_sa" {
  for_each = var.google_managed_sas_exist ? toset([
    "roles/datastore.user",       # Read/write Firestore (tool calls)
    "roles/aiplatform.user",      # Call Gemini / Vertex AI APIs
    "roles/storage.objectViewer", # Read staging bucket artifacts
  ]) : toset([])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.agent_engine_sa}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# Core Vertex AI service account
# Used for embeddings and direct generateContent calls.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "vertex_sa" {
  for_each = var.google_managed_sas_exist ? toset([
    "roles/datastore.user", # Firestore vector search for RAG
    "roles/aiplatform.user",
  ]) : toset([])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.vertex_sa}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# Agent Identity (identity_type=AGENT_IDENTITY in deployment/deploy.py)
#
# Agent Identity replaces the classic service accounts above with a per-agent
# SPIFFE principal — the agent_engine_sa/vertex_sa grants above do NOT apply
# to it. Only two roles come for free (aiplatform.agentContextEditor,
# aiplatform.agentDefaultAccess, covering inference/sessions/memory);
# Firestore and Cloud Logging access must be granted explicitly per
# "Use Agent Identity with Agent Runtime" — verified live: without this,
# every tool call fails with 401 Unauthenticated (not 403), since the SPIFFE
# principal has no grants at all, not just insufficient ones.
#
# Uses principalSet:// (all agents in this project), not a per-resource
# principal://, so it doesn't need the Agent Engine ID as a Terraform input —
# avoids a create-then-grant chicken-and-egg to match identity_type being
# create-only/immutable on the engine itself.
#
# Trust domain is project-scoped (agents.global.proj-PROJECT_NUMBER...)
# because this project has no parent organization; projects under an org
# would use agents.global.org-ORGANIZATION_ID.system.id.goog instead.
#
# NOTE: the reference docs say "agents.global.project-PROJECT_NUMBER..." —
# that's wrong. Verified against a real deployed engine's own reported
# spec.effective_identity: the actual trust domain is "proj-", not
# "project-" (e.g. agents.global.proj-1038615239861.system.id.goog). Using
# the docs' literal string gets rejected by the IAM API with "member ... is
# of an unknown type" — confirmed live, not a Terraform quirk.
#
# IMPORTANT: these grants alone are NOT sufficient. Agent Identity tokens
# are cryptographically bound to the agent's own X.509 cert via
# Context-Aware Access (mTLS/DPoP) — plain google-cloud-firestore/logging
# clients (what this repo's tools use) never establish that mTLS channel,
# so calls still 401 even with fully correct IAM. deployment/deploy.py's
# ENV_VARS also sets GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES
# to opt out of that binding — both pieces are required together, verified
# live against real seeded Firestore data.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "agent_identity" {
  for_each = var.google_managed_sas_exist ? toset([
    "roles/datastore.user",    # Firestore (tool calls) — not in the default identity role set
    "roles/logging.logWriter", # Cloud Logging (LoggingPlugin, OTel exporter) — same
  ]) : toset([])

  project = var.project_id
  role    = each.key
  member  = "principalSet://agents.global.proj-${local.project_number}.system.id.goog/attribute.platformContainer/aiplatform/projects/${local.project_number}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# Cloud Run default compute service account
# Runs the FastAPI backend; needs Agent Engine and Firestore.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "cloud_run_sa" {
  for_each = toset([
    # Cloud Run (FastAPI backend) roles
    "roles/aiplatform.user", # Call Agent Engine
    "roles/datastore.user",  # Read/write sessions and messages
    # CI/CD roles — compute SA is also used as the 2nd gen Cloud Build trigger SA
    # (Google-managed @cloudbuild SA is rejected by 2nd gen builds)
    "roles/aiplatform.admin",             # Deploy to Agent Engine
    "roles/artifactregistry.writer",      # Push Docker images
    "roles/run.admin",                    # Deploy Cloud Run service
    "roles/secretmanager.secretAccessor", # Read staging-bucket secret
    "roles/cloudbuild.builds.editor",     # Invoke Cloud Build triggers (Cloud Scheduler)
    "roles/iam.serviceAccountUser",       # Act as itself during Cloud Run deploy
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.cloud_run_sa}"

  depends_on = [google_project_service.apis]
}

# Cloud Run SA also needs access to the staging bucket at the bucket level
# (granted in infrastructure.tf on the bucket resource itself)

# ------------------------------------------------------------------------------
# Cloud Build service account
# Runs CI/CD pipelines; needs to deploy to Cloud Run and Artifact Registry.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "cloud_build_sa" {
  for_each = toset([
    "roles/datastore.user",               # Firestore access during agent eval tests
    "roles/aiplatform.user",              # Call Vertex AI Gemini during eval tests
    "roles/aiplatform.admin",             # Deploy to Agent Engine
    "roles/artifactregistry.writer",      # Push Docker images
    "roles/run.admin",                    # Deploy Cloud Run service
    "roles/storage.objectAdmin",          # Read/write staging bucket
    "roles/secretmanager.secretAccessor", # Read staging-bucket secret
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.cloud_build_sa}"

  depends_on = [google_project_service.apis]
}

# Compute SA (used by Cloud Build triggers) needs read/write access to the
# Terraform state bucket so terraform-plan and terraform-apply can run.
resource "google_storage_bucket_iam_member" "compute_sa_tfstate" {
  count  = var.github_connected ? 1 : 0
  bucket = local.tfstate_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${local.cloud_run_sa}"

  depends_on = [google_project_service.apis]
}

# Cloud Build SA needs to impersonate the Cloud Run compute SA when deploying
resource "google_service_account_iam_member" "cloud_build_impersonate_compute" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.cloud_build_sa}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# Model Armor — grant modelarmor.user to both Vertex AI service accounts
# so that Agent Engine and embedding calls can pass through Model Armor screening
# ------------------------------------------------------------------------------
# Cloud Run SA calls Model Armor API directly from the FastAPI backend
resource "google_project_iam_member" "model_armor_cloud_run" {
  count = var.model_armor_enabled ? 1 : 0

  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${local.cloud_run_sa}"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "model_armor_agent_engine" {
  count = var.model_armor_enabled && var.google_managed_sas_exist ? 1 : 0

  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${local.agent_engine_sa}"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "model_armor_vertex" {
  count = var.model_armor_enabled && var.google_managed_sas_exist ? 1 : 0

  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${local.vertex_sa}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# FedEx MCP Cloud Run invoker SA — bridges Agent Identity to Cloud Run IAM auth.
#
# Cloud Run's --no-allow-unauthenticated invoker check is a separate, older
# OIDC verification path from Agent Identity's SPIFFE/mTLS trust domain — the
# two don't interoperate directly. Confirmed live: an ID token minted straight
# from an AGENT_IDENTITY engine's ADC gets a 401 "could not be verified" from
# Cloud Run, not a clean 403. Google's own reference implementation
# (cloudnet-agent-gateway codelab, demos/agent-gateway/src/mortgage-agent/
# deploy_agent.py) bridges the two trust domains with a dedicated invoker SA:
# the agent's Agent Identity is only ever granted permission to IMPERSONATE
# this SA (serviceAccountTokenCreator); the SA itself — a normal IAM
# principal Cloud Run already understands — holds roles/run.invoker on the
# MCP Cloud Run service (granted out-of-band by deployment/deploy-mcp-fedex.sh
# after each deploy, since the Cloud Run service isn't Terraform-managed).
# See docs/MCP_FEDEX.md section 7 for the full design and open items.
# ------------------------------------------------------------------------------
resource "google_service_account" "fedex_mcp_invoker" {
  project      = var.project_id
  account_id   = "fedex-mcp-invoker"
  display_name = "FedEx MCP Cloud Run invoker (impersonated by order agent's Agent Identity)"

  depends_on = [google_project_service.apis]
}

resource "google_service_account_iam_member" "agent_identity_impersonates_fedex_invoker" {
  for_each = var.google_managed_sas_exist ? toset(["roles/iam.serviceAccountTokenCreator"]) : toset([])

  service_account_id = google_service_account.fedex_mcp_invoker.name
  role               = each.key
  member             = "principalSet://agents.global.proj-${local.project_number}.system.id.goog/attribute.platformContainer/aiplatform/projects/${local.project_number}"

  depends_on = [google_project_service.apis]
}
