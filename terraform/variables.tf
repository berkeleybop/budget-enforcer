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
# Naming
# -----------------------------------------------------------------------------

variable "resource_prefix" {
  type        = string
  default     = "tf"
  description = <<-EOT
    Prefix for all named GCP resources (service accounts, Pub/Sub topics,
    Cloud Run services, etc.). Use this to distinguish Terraform-managed
    resources from manually created ones in the same project. For example,
    with prefix "tf", Cloud Run becomes "tf-budget-enforcer", the consumer
    SA becomes "tf-vertex-ai-consumer", etc.
  EOT
}

variable "consumer_sa_name" {
  type        = string
  default     = "vertex-ai-consumer"
  description = <<-EOT
    Base name for the API consumer service account (prefixed with
    resource_prefix). This SA is used by your application (e.g. Claude Code)
    to call Vertex AI. When the budget is exceeded, the budget-enforcer
    disables THIS account's keys.
  EOT
}

variable "admin_sa_name" {
  type        = string
  default     = "budget-enforcer-admin"
  description = <<-EOT
    Base name for the admin service account (prefixed with resource_prefix).
    This SA runs the Cloud Run budget-enforcer service and has permissions
    to disable the consumer SA's keys. NEVER point SERVICE_ACCOUNT_EMAIL
    at this SA — doing so would lock you out of the project.
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
  default     = ""
  description = <<-EOT
    Display name for the billing budget in Cloud Console. If empty,
    defaults to "{resource_prefix}-vertex-ai-monthly-budget".
  EOT
}

locals {
  budget_display_name = var.budget_display_name != "" ? var.budget_display_name : "${var.resource_prefix}-vertex-ai-monthly-budget"
}

# -----------------------------------------------------------------------------
# Flux estimator — real-time spend estimation
#
# GCP billing data lags 12-24 hours. The flux estimator bridges this gap
# by querying Cloud Monitoring for actual token counts every 5 minutes.
# It uses the publisher/online_serving/token_count metric, which works
# for BOTH Anthropic (Claude) and Google (Gemini) models and provides
# per-model, per-token-type breakdowns including cache pricing.
#
# If the token metric returns no data (e.g. new model not yet reporting),
# it falls back to counting API calls and applying a conservative
# per-call cost estimate.
#
# Pricing is built into main.py (the PRICING dict). When model prices
# change, update main.py, rebuild the container, and terraform apply.
# -----------------------------------------------------------------------------

variable "flux_window_hours" {
  type        = number
  default     = 48
  description = <<-EOT
    How many hours of recent API usage to include in the spend estimate.
    Should exceed the maximum billing data lag (~24-48h) so there's no
    blind spot. As billing data arrives and the billing-based enforcement
    (POST /) takes over, the overlapping usage ages out of this window
    naturally — preventing double-counting.
  EOT
}

variable "enforcement_tolerance" {
  type        = string
  default     = "1.0"
  description = <<-EOT
    Scalar on the budget threshold that controls how aggressively the
    flux estimator enforces. Tunes the tradeoff between false positives
    (keys disabled before real spend hits the budget) and overspend
    (letting calls through while billing catches up).

    Examples:
      0.8 = enforce at 80% of budget (conservative, prefer early cutoff)
      1.0 = enforce at exactly the budget amount (default)
      1.2 = allow up to 20% overspend before enforcing (tolerant)

    This only affects the flux estimator (/check-usage). The billing-
    based enforcement (POST /) always triggers at exactly 100%.
  EOT
}

variable "cost_per_call_fallback" {
  type        = string
  default     = "0.30"
  description = <<-EOT
    Fallback: upper-bound cost per API call (USD). Used ONLY when the
    token count metric returns no data. Assumes every call costs as much
    as the most expensive model (~$0.30 for Opus). In normal operation
    the token-based estimator is used instead, which has per-model
    pricing and cache awareness.
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
