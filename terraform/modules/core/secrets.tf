resource "google_secret_manager_secret" "staging_bucket" {
  project   = var.project_id
  secret_id = "staging-bucket"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}
resource "google_secret_manager_secret_version" "staging_bucket" {
  secret      = google_secret_manager_secret.staging_bucket.id
  secret_data = var.staging_bucket_name
  lifecycle { ignore_changes = [secret_data] }
}
resource "google_secret_manager_secret_iam_member" "cloud_build_staging_bucket" {
  project    = var.project_id
  secret_id  = google_secret_manager_secret.staging_bucket.secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${local.cloud_build_sa}"
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "fedex_client_id" {
  count     = var.fedex_secrets_enabled ? 1 : 0
  project   = var.project_id
  secret_id = "fedex-client-id"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "fedex_client_secret" {
  count     = var.fedex_secrets_enabled ? 1 : 0
  project   = var.project_id
  secret_id = "fedex-client-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# deployment/deploy-mcp-fedex.sh mounts these two secrets onto the
# fedex-tracking-mcp Cloud Run service via --set-secrets once FEDEX_MOCK is
# not "true" — that deploy fails with a permission error at the mount step
# without this grant. The service runs as the project's default compute SA
# (no --service-account override in deploy-mcp-fedex.sh), the same
# principal local.cloud_run_sa already names for the main backend.
resource "google_secret_manager_secret_iam_member" "cloud_run_fedex_client_id" {
  count      = var.fedex_secrets_enabled ? 1 : 0
  project    = var.project_id
  secret_id  = google_secret_manager_secret.fedex_client_id[0].secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${local.cloud_run_sa}"
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "cloud_run_fedex_client_secret" {
  count      = var.fedex_secrets_enabled ? 1 : 0
  project    = var.project_id
  secret_id  = google_secret_manager_secret.fedex_client_secret[0].secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${local.cloud_run_sa}"
  depends_on = [google_project_service.apis]
}
