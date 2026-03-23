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
# by estimating spend from Cloud Monitoring metrics. It runs every 5 min
# via Cloud Scheduler hitting the /check-usage endpoint.
#
# Two modes are available:
#   "call_count" — counts API calls and multiplies by a per-call cost
#     estimate. Works for ALL models including Anthropic (Claude).
#   "token" — uses actual token count metrics from Cloud Monitoring.
#     Only works for Google-native models (Gemini, PaLM). Anthropic
#     models do NOT populate token count metrics.
#
# If you use only Anthropic models, use "call_count" (the default).
# If you use only Google models, use "token" for better accuracy.
# If you use a mix, use "call_count" as the safe fallback.
# -----------------------------------------------------------------------------

variable "flux_mode" {
  type        = string
  default     = "call_count"
  description = <<-EOT
    Spend estimation mode for the /check-usage endpoint:
    - "call_count": Counts API calls, applies per-call cost estimate.
      Works for ALL models (Anthropic, Google, etc.). Less precise but
      universally available. Use this for Claude.
    - "token": Uses actual token counts from Cloud Monitoring. More
      precise but ONLY works for Google-native models (Gemini, PaLM).
      Anthropic models do not populate these metrics.
  EOT

  validation {
    condition     = contains(["call_count", "token"], var.flux_mode)
    error_message = "flux_mode must be \"call_count\" or \"token\"."
  }
}

variable "flux_window_hours" {
  type        = number
  default     = 48
  description = <<-EOT
    How many hours of recent API call data to include in the spend
    estimate. The window should be longer than the maximum billing data
    lag (~24-48h) to avoid a blind spot between when calls are made and
    when billing catches up. As billing data arrives and the billing-based
    enforcement (POST /) takes over, the overlapping call data ages out
    of this window naturally — preventing double-counting.
  EOT
}

variable "cost_per_call_expensive" {
  type        = string
  default     = "0.30"
  description = <<-EOT
    Upper-bound cost estimate (USD) per API call for expensive models
    (e.g. Claude Opus). Used in call_count mode. Set this conservatively
    — it's better to overestimate and enforce slightly early than to
    underestimate and overshoot the budget. A typical Opus call with
    ~5K input + ~1K output tokens costs roughly $0.15-0.30.
  EOT
}

variable "cost_per_call_cheap" {
  type        = string
  default     = "0.01"
  description = <<-EOT
    Upper-bound cost estimate (USD) per API call for cheap models
    (e.g. Claude Haiku). Used in call_count mode. Currently all calls
    are priced at the expensive rate as a conservative default, since
    we cannot reliably distinguish model deployments. This variable is
    available for future use when deployment-to-model mapping is added.
  EOT
}

variable "enforcement_tolerance" {
  type        = string
  default     = "1.0"
  description = <<-EOT
    Scalar on the budget threshold that controls how aggressively the
    flux estimator enforces. This tunes the tradeoff between false
    positives (keys disabled before real spend hits the budget) and
    overspend (letting calls through while billing catches up).

    Examples:
      0.8 = enforce at 80% of budget (conservative, prefer early cutoff)
      1.0 = enforce at exactly the budget amount (default)
      1.2 = allow up to 20% overspend before enforcing (tolerant)

    This only affects the flux estimator (/check-usage). The billing-
    based enforcement (POST /) always triggers at exactly 100%.
  EOT
}

# Legacy settings (kept for backward compatibility)

variable "usage_hourly_limit" {
  type        = string
  default     = "0"
  description = "Legacy: max cost in a 1-hour window. Superseded by flux estimator."
}

variable "usage_daily_limit" {
  type        = string
  default     = "0"
  description = "Legacy: max cost in a 24-hour window. Superseded by flux estimator."
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
