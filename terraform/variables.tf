# -----------------------------------------------------------------------------
# Project
# -----------------------------------------------------------------------------

variable "project_id" {
  type        = string
  description = <<-EOT
    GCP project ID (not the display name). Find this in the downloaded
    service account JSON under "project_id", or in Cloud Console.
  EOT
}

variable "billing_account_id" {
  type        = string
  description = <<-EOT
    GCP billing account ID (format: XXXXXX-XXXXXX-XXXXXX). Find this under
    Cloud Console > Billing > Account Management. Required to create the
    budget alert that triggers budget enforcement.
  EOT
}

# -----------------------------------------------------------------------------
# Service account names
# -----------------------------------------------------------------------------

variable "consumer_sa_name" {
  type        = string
  default     = "vertex-ai-consumer"
  description = <<-EOT
    Name for the API consumer service account. This SA is used by your
    application (e.g. Claude Code) to call Vertex AI. When the budget is
    exceeded, the budget-enforcer disables THIS account's keys.
  EOT
}

variable "admin_sa_name" {
  type        = string
  default     = "budget-enforcer-admin"
  description = <<-EOT
    Name for the admin service account. This SA runs the Cloud Run
    budget-enforcer service and has permissions to disable the consumer
    SA's keys. NEVER point SERVICE_ACCOUNT_EMAIL at this SA — doing so
    would lock you out of the project.
  EOT
}

# -----------------------------------------------------------------------------
# Regions
# -----------------------------------------------------------------------------

variable "vertex_ai_region" {
  type        = string
  default     = "us-east5"
  description = <<-EOT
    Region for Vertex AI model access. As of early 2026, "global" is
    slightly cheaper, but us-east5 is recommended for data residency.
  EOT
}

variable "cloud_run_region" {
  type        = string
  default     = "us-central1"
  description = "Region for Cloud Run and Cloud Scheduler."
}

# -----------------------------------------------------------------------------
# Budget
# -----------------------------------------------------------------------------

variable "monthly_budget_amount" {
  type        = number
  default     = 100
  description = <<-EOT
    Monthly budget threshold in USD. When actual spend reaches this amount,
    the budget-enforcer disables the consumer SA's keys, halting all
    Vertex AI API calls. Set conservatively — you can always increase it.

    IMPORTANT: The budget scopes to ALL services, not just "Vertex AI",
    because Claude model charges are billed under a marketplace service
    category that a Vertex AI-only budget would miss entirely.
  EOT
}

variable "budget_display_name" {
  type        = string
  default     = "vertex-ai-monthly-budget"
  description = "Display name for the billing budget in Cloud Console."
}

# -----------------------------------------------------------------------------
# Usage thresholds (Cloud Run env vars for real-time monitoring)
# -----------------------------------------------------------------------------

variable "usage_hourly_limit" {
  type        = string
  default     = "10.00"
  description = <<-EOT
    Max estimated cost (USD) in a 1-hour rolling window. If the
    /check-usage endpoint detects spend above this, it disables keys
    immediately — faster than waiting for GCP Billing (which can lag
    12-24 hours).
  EOT
}

variable "usage_daily_limit" {
  type        = string
  default     = "50.00"
  description = <<-EOT
    Max estimated cost (USD) in a 24-hour rolling window. Same mechanism
    as usage_hourly_limit but for daily spend.
  EOT
}

# -----------------------------------------------------------------------------
# Container image
# -----------------------------------------------------------------------------

variable "container_image" {
  type        = string
  description = <<-EOT
    Full container image URI for the budget-enforcer. Build and push with:
      gcloud builds submit --tag gcr.io/YOUR_PROJECT/budget-enforcer ../
    Then set this variable to: gcr.io/YOUR_PROJECT/budget-enforcer
    Or use Artifact Registry: REGION-docker.pkg.dev/YOUR_PROJECT/REPO/budget-enforcer
  EOT
}
