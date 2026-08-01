# ==============================================================================
# PROD environment — watches main branch, deploys to prod GCP project
# ==============================================================================
# Usage:
#   make bootstrap-tfstate ENV=prod   # one-time: create GCS bucket + upload tfvars
#   make infra-up ENV=prod            # terraform init + apply

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # Remote state — bucket + prefix injected via -backend-config at init time
  backend "gcs" {}
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

module "core" {
  source = "../../modules/core"

  # Environment
  environment = "prod"

  # Required
  project_id          = var.project_id
  staging_bucket_name = var.staging_bucket_name
  github_owner        = var.github_owner

  # Optional — override defaults as needed
  region                               = var.region
  firestore_location                   = var.firestore_location
  firestore_database_id                = var.firestore_database_id
  ar_repo_name                         = var.ar_repo_name
  cloud_run_service_name               = var.cloud_run_service_name
  github_repo                          = var.github_repo
  google_managed_sas_exist             = var.google_managed_sas_exist
  github_connected                     = var.github_connected
  agent_engine_resource_name           = var.agent_engine_resource_name
  cloudbuild_connection_name           = var.cloudbuild_connection_name
  cloudbuild_repo_name                 = var.cloudbuild_repo_name
  model_armor_enabled                  = var.model_armor_enabled
  model_armor_floor_mode               = var.model_armor_floor_mode
  fedex_secrets_enabled                = var.fedex_secrets_enabled
  tfstate_bucket_name                  = var.tfstate_bucket_name
  canary_traffic_percent               = var.canary_traffic_percent
  canary_check_since                   = var.canary_check_since
  canary_check_min_sessions            = var.canary_check_min_sessions
  canary_check_relative_threshold      = var.canary_check_relative_threshold
  canary_check_absolute_floor          = var.canary_check_absolute_floor
  enable_quality_alerts                = var.enable_quality_alerts
  quality_alert_task_success_threshold = var.quality_alert_task_success_threshold
  quality_alert_notification_channels  = var.quality_alert_notification_channels
}
