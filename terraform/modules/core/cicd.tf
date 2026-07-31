# ==============================================================================
# CI/CD — Cloud Build triggers (2nd gen) + Cloud Scheduler canary quality check
# ==============================================================================
#
# Single main branch strategy. All triggers target ^main$. Environment
# promotion happens via git tags, not branch merges.
#
# Trigger summary by environment:
#
#   ALL environments:
#     ci-pull-request   — PR to main: lint + tests; integration tests auto-enabled
#                         when customer_support_mas/ files changed
#     terraform-plan    — PR to main: infra diff
#     terraform-apply   — Push to main: infra apply (scoped to terraform/ changes)
#
#   dev only:
#     ci-cd-push-main   — Push to main: CI + deploy to dev
#
#   staging only:
#     release-staging   — Tag v*.*.*-rc.*: full CI + staging deploy + load tests + eval
#
#   prod only:
#     release              — Tag v*.*.*: shadow deploy + eval gate + canary enable
#     canary-quality-check — Manual / scheduled: real-traffic agentic eval of a
#                            live canary vs champion; promotes, rolls back, or
#                            holds. No-op if no canary is live.
#     Cloud Scheduler      — Fires canary-quality-check at midnight UTC

locals {
  repo_resource = "projects/${var.project_id}/locations/${var.region}/connections/${var.cloudbuild_connection_name}/repositories/${var.cloudbuild_repo_name}"
  is_dev        = var.environment == "dev"
  is_staging    = var.environment == "staging"
  is_prod       = var.environment == "prod"

  # Terraform state bucket — use explicit var or fall back to convention
  tfstate_bucket = var.tfstate_bucket_name != "" ? var.tfstate_bucket_name : "${var.project_id}-tf-state"

  # Environment directory used by terraform-plan and terraform-apply triggers
  env_directory = "terraform/environments/${var.environment}"

  # Shared terraform substitutions
  terraform_substitutions = {
    _ENV_DIRECTORY   = local.env_directory
    _ENVIRONMENT     = var.environment
    _TF_STATE_BUCKET = local.tfstate_bucket
  }

  # Shared app substitutions (used by dev deploy and staging release)
  app_substitutions = {
    _EVAL_PROFILE               = "standard"
    _GOOGLE_CLOUD_LOCATION      = var.region
    _REGION                     = var.region
    _FIRESTORE_DATABASE         = var.firestore_database_id
    _SERVICE_NAME               = var.cloud_run_service_name
    _AR_REPO                    = var.ar_repo_name
    _STAGING_BUCKET             = "gs://${var.staging_bucket_name}"
    _MODEL_ARMOR_ENABLED        = tostring(var.model_armor_enabled)
    _MODEL_ARMOR_TEMPLATE_ID    = var.model_armor_enabled ? google_model_armor_template.customer_support_policy[0].name : ""
    _AGENT_ENGINE_RESOURCE_NAME = var.agent_engine_resource_name
  }
}

# ==============================================================================
# SHARED TRIGGERS — all environments
# ==============================================================================

# PR checks — auto-detects customer_support_mas/ changes and runs integration
# tests when needed. No manual /gcbrun comment required.
resource "google_cloudbuild_trigger" "pr_checks" {
  count           = var.github_connected && local.is_dev ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "ci-pull-request"
  description     = "PR checks: lint + tests + integration tests when agent files changed [${var.environment}]"
  service_account = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"

  repository_event_config {
    repository = local.repo_resource
    pull_request {
      branch          = "^main$"
      comment_control = "COMMENTS_ENABLED_FOR_EXTERNAL_CONTRIBUTORS_ONLY"
    }
  }

  filename = "cloudbuild/pr-checks.yaml"

  substitutions = {
    _GOOGLE_CLOUD_LOCATION = var.region
    _FIRESTORE_DATABASE    = var.firestore_database_id
  }

  depends_on = [google_project_service.apis]
}

# Terraform Plan — PR to main shows infra diff before merge
resource "google_cloudbuild_trigger" "terraform_plan" {
  count           = var.github_connected ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "terraform-plan"
  description     = "Terraform plan: show infra diff on PR to main [${var.environment}]"
  service_account = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"

  repository_event_config {
    repository = local.repo_resource
    pull_request {
      branch          = "^main$"
      comment_control = "COMMENTS_ENABLED_FOR_EXTERNAL_CONTRIBUTORS_ONLY"
    }
  }

  filename      = "cloudbuild/terraform-plan.yaml"
  substitutions = local.terraform_substitutions
  depends_on    = [google_project_service.apis]
}

# Terraform Apply — push to main auto-applies infra changes after merge
resource "google_cloudbuild_trigger" "terraform_apply" {
  count           = var.github_connected ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "terraform-apply"
  description     = "Terraform apply: auto-apply infra on push to main [${var.environment}]"
  service_account = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"

  repository_event_config {
    repository = local.repo_resource
    push { branch = "^main$" }
  }

  included_files = ["terraform/**"]

  filename      = "cloudbuild/terraform-apply.yaml"
  substitutions = local.terraform_substitutions
  depends_on    = [google_project_service.apis]
}

# ==============================================================================
# DEV TRIGGER — push to main deploys to dev
# ==============================================================================

resource "google_cloudbuild_trigger" "push_main_dev" {
  count           = var.github_connected && local.is_dev ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "ci-cd-push-main"
  description     = "CI + CD: standard eval + deploy to dev on push to main [dev]"
  service_account = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"

  repository_event_config {
    repository = local.repo_resource
    push { branch = "^main$" }
  }

  filename = "cloudbuild/cloudbuild-deploy.yaml"

  substitutions = merge(local.app_substitutions, {
    _RUN_LOAD_TESTS = "false" # load tests run on staging rc tags only
  })

  depends_on = [google_project_service.apis]
}

# ==============================================================================
# STAGING TRIGGER — rc tag deploys to staging
# ==============================================================================

resource "google_cloudbuild_trigger" "release_staging" {
  count           = var.github_connected && local.is_staging ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "release-staging"
  description     = "Staging release: full CI + deploy + load tests + eval on rc tag [staging]"
  service_account = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"

  repository_event_config {
    repository = local.repo_resource
    push {
      tag = "^v[0-9]+\\.[0-9]+\\.[0-9]+-rc\\.[0-9]+$"
    }
  }

  filename = "cloudbuild/release-staging.yaml"

  substitutions = merge(local.app_substitutions, {
    _AGENT_ENGINE_DISPLAY_NAME = "customer-support-multiagent-staging"
  })

  depends_on = [google_project_service.apis]
}

# ==============================================================================
# PROD TRIGGERS — release tag, canary quality check
# ==============================================================================

# Release — prod tag v*.*.* (no rc suffix): shadow deploy + eval gate + canary
resource "google_cloudbuild_trigger" "release" {
  count           = var.github_connected && local.is_prod ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "release"
  description     = "Prod release: shadow deploy + post-deploy eval gate + canary enable on prod tag [prod]"
  service_account = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"

  repository_event_config {
    repository = local.repo_resource
    push {
      # Matches v1.2.3 but NOT v1.2.3-rc.1 ($ anchors the end)
      tag = "^v[0-9]+\\.[0-9]+\\.[0-9]+$"
    }
  }

  filename = "cloudbuild/release.yaml"

  substitutions = merge(local.app_substitutions, {
    _EVAL_PROFILE           = "standard"
    _CANARY_TRAFFIC_PERCENT = tostring(var.canary_traffic_percent)
  })

  depends_on = [google_project_service.apis]
}

# Canary quality check — manual dispatch + Cloud Scheduler: compares a live
# canary against its champion on REAL production traffic (via the Agent
# Engine Sessions API), scored on agentic behavior. No-op if no canary is
# live. See tests/eval_agent_platform.py's `canary-check` mode.
resource "google_cloudbuild_trigger" "nightly" {
  count           = var.github_connected && local.is_prod ? 1 : 0
  project         = var.project_id
  location        = var.region
  name            = "canary-quality-check"
  description     = "Canary quality check: real-traffic agentic eval vs champion, drives promote/rollback [prod]"
  service_account = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"

  source_to_build {
    repository = local.repo_resource
    ref        = "refs/heads/main"
    repo_type  = "GITHUB"
  }

  filename = "cloudbuild/canary-quality-check.yaml"

  substitutions = {
    _GOOGLE_CLOUD_LOCATION           = var.region
    _FIRESTORE_DATABASE              = var.firestore_database_id
    _STAGING_BUCKET                  = "gs://${var.staging_bucket_name}"
    _CANARY_CHECK_SINCE              = var.canary_check_since
    _CANARY_CHECK_MIN_SESSIONS       = tostring(var.canary_check_min_sessions)
    _CANARY_CHECK_RELATIVE_THRESHOLD = tostring(var.canary_check_relative_threshold)
    _CANARY_CHECK_ABSOLUTE_FLOOR     = tostring(var.canary_check_absolute_floor)
  }

  depends_on = [google_project_service.apis]
}

# Cloud Scheduler — fires the canary quality check at midnight UTC.
# Safe to run even with no canary live (the job no-ops in that case); while a
# canary IS live, each run either promotes, rolls back, or holds (exit 2) if
# not enough real sessions have accumulated yet since the last check.
resource "google_cloud_scheduler_job" "nightly_eval" {
  count       = var.github_connected && local.is_prod ? 1 : 0
  project     = var.project_id
  region      = var.region
  name        = "canary-quality-check"
  description = "Trigger canary quality check (real-traffic agentic eval) daily at midnight UTC"
  schedule    = "0 0 * * *"
  time_zone   = "UTC"

  http_target {
    http_method = "POST"
    uri         = "https://cloudbuild.googleapis.com/v1/projects/${var.project_id}/locations/${var.region}/triggers/${google_cloudbuild_trigger.nightly[0].trigger_id}:run"
    body        = base64encode("{}")
    oauth_token { service_account_email = local.cloud_run_sa }
  }

  depends_on = [
    google_project_service.apis,
    google_cloudbuild_trigger.nightly,
    google_project_iam_member.cloud_run_sa,
  ]
}
