# =============================================================================
# Budget Enforcer — GCP Infrastructure
#
# This configuration provisions everything needed to run the budget-enforcer
# workflow: service accounts, IAM bindings, Pub/Sub, Cloud Run, and a billing
# budget that triggers automatic key disabling when spend exceeds the threshold.
#
# Prerequisites (manual, cannot be automated):
#   1. A GCP project created by Science IT, attached to your billing account
#   2. Anthropic models enabled in Model Garden (UI-only)
#   3. Container image built and pushed (see variable "container_image")
#   4. Consumer SA JSON key created (see docs/MANUAL_STEPS.md)
#
# Run as: Your personal Google account (Owner role) via:
#   gcloud auth application-default login
# =============================================================================

# -----------------------------------------------------------------------------
# APIs — enable required services
# -----------------------------------------------------------------------------

locals {
  required_apis = [
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "iam.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "monitoring.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# Service accounts
# -----------------------------------------------------------------------------

# The API consumer SA — used by applications (e.g. Claude Code) to call
# Vertex AI. The budget-enforcer disables THIS account's keys.
resource "google_service_account" "consumer" {
  account_id   = var.consumer_sa_name
  display_name = "Vertex AI API Consumer"
  description  = "Used by applications to access Vertex AI. Keys are disabled by budget-enforcer when spend exceeds threshold."
  project      = var.project_id
}

# The admin SA — runs the budget-enforcer Cloud Run service.
# Has elevated permissions to disable the consumer SA's keys.
resource "google_service_account" "admin" {
  account_id   = var.admin_sa_name
  display_name = "Budget Enforcer Admin"
  description  = "Runs the budget-enforcer Cloud Run service. Can disable consumer SA keys. NEVER point SERVICE_ACCOUNT_EMAIL at this SA."
  project      = var.project_id
}

# The Pub/Sub invoker SA — used by Pub/Sub to authenticate when pushing
# messages to Cloud Run. Separate from both admin and consumer SAs.
resource "google_service_account" "invoker" {
  account_id   = "pubsub-invoker"
  display_name = "Pub/Sub Cloud Run Invoker"
  description  = "Used by Pub/Sub to send OIDC-authenticated requests to Cloud Run."
  project      = var.project_id
}

# -----------------------------------------------------------------------------
# IAM bindings — consumer SA
# -----------------------------------------------------------------------------

resource "google_project_iam_member" "consumer_vertex_ai_service_agent" {
  project = var.project_id
  role    = "roles/aiplatform.serviceAgent"
  member  = "serviceAccount:${google_service_account.consumer.email}"
}

resource "google_project_iam_member" "consumer_vertex_ai_viewer" {
  project = var.project_id
  role    = "roles/aiplatform.viewer"
  member  = "serviceAccount:${google_service_account.consumer.email}"
}

# -----------------------------------------------------------------------------
# IAM bindings — admin SA
# -----------------------------------------------------------------------------

resource "google_project_iam_member" "admin_editor" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_project_iam_member" "admin_sa_key_admin" {
  project = var.project_id
  role    = "roles/iam.serviceAccountKeyAdmin"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_project_iam_member" "admin_service_usage_admin" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageAdmin"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_project_iam_member" "admin_project_iam_admin" {
  project = var.project_id
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_project_iam_member" "admin_cloud_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_project_iam_member" "admin_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.admin.email}"
}

# -----------------------------------------------------------------------------
# Pub/Sub — topic and subscription
# -----------------------------------------------------------------------------

resource "google_pubsub_topic" "budget_alerts" {
  name    = "budget-alerts-01"
  project = var.project_id

  depends_on = [google_project_service.apis["pubsub.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# Cloud Run — budget-enforcer service
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "budget_enforcer" {
  name     = "budget-enforcer"
  location = var.cloud_run_region
  project  = var.project_id

  template {
    # Run as the admin SA so it has permission to disable consumer SA keys.
    service_account = google_service_account.admin.email

    containers {
      image = var.container_image

      env {
        # CRITICAL: Must point at the CONSUMER SA, never the admin SA.
        # Terraform enforces this by referencing google_service_account.consumer.
        name  = "SERVICE_ACCOUNT_EMAIL"
        value = google_service_account.consumer.email
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "USAGE_HOURLY_LIMIT"
        value = var.usage_hourly_limit
      }
      env {
        name  = "USAGE_DAILY_LIMIT"
        value = var.usage_daily_limit
      }
    }
  }

  depends_on = [
    google_project_service.apis["run.googleapis.com"],
    google_project_iam_member.admin_editor,
    google_project_iam_member.admin_sa_key_admin,
  ]
}

# Grant the invoker SA permission to call Cloud Run.
# This is a separate resource so it persists across Cloud Run redeployments —
# the most common failure mode in the manual SOP (step 7/R3).
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  name     = google_cloud_run_v2_service.budget_enforcer.name
  location = var.cloud_run_region
  project  = var.project_id
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}

# Pub/Sub subscription — pushes budget alerts to Cloud Run with OIDC auth.
# The push-auth-service-account is the critical piece that the manual SOP
# often gets wrong, causing silent 403 failures.
resource "google_pubsub_subscription" "budget_alerts" {
  name    = "budget-alerts-sub-01"
  topic   = google_pubsub_topic.budget_alerts.id
  project = var.project_id

  push_config {
    push_endpoint = google_cloud_run_v2_service.budget_enforcer.uri

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloud_run_v2_service.budget_enforcer.uri
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
}

# -----------------------------------------------------------------------------
# Cloud Scheduler — periodic usage check
# -----------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "check_usage" {
  name      = "check-vertex-usage"
  schedule  = "*/5 * * * *"
  time_zone = "America/Los_Angeles"
  project   = var.project_id
  region    = var.cloud_run_region

  http_target {
    http_method = "GET"
    uri         = "${google_cloud_run_v2_service.budget_enforcer.uri}/check-usage"

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloud_run_v2_service.budget_enforcer.uri
    }
  }

  depends_on = [google_project_service.apis["cloudscheduler.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# Billing budget
#
# Scoped to ALL services (not just Vertex AI) because Claude model charges
# are billed under a marketplace/partner service category. A Vertex AI-only
# budget would miss most of the actual spend.
# -----------------------------------------------------------------------------

resource "google_billing_budget" "vertex_ai" {
  billing_account = var.billing_account_id
  display_name    = var.budget_display_name

  budget_filter {
    projects = ["projects/${var.project_id}"]
    # No services filter — intentionally captures ALL services.
    # Claude costs bill under a marketplace service, not "Vertex AI".
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_amount)
    }
  }

  # Sub-100% thresholds send email-only early warnings.
  # Only 100% triggers the Pub/Sub message that disables keys.
  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.75
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.95
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  all_updates_rule {
    pubsub_topic = google_pubsub_topic.budget_alerts.id
  }
}
