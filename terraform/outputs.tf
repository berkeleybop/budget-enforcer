# -----------------------------------------------------------------------------
# Operational outputs
#
# After "terraform apply", these values are available via "terraform output".
# They provide the key information needed for day-to-day operations and
# for configuring Claude Code on developer machines.
# -----------------------------------------------------------------------------

output "cloud_run_url" {
  value       = google_cloud_run_v2_service.budget_enforcer.uri
  description = "URL of the budget-enforcer Cloud Run service."
}

output "consumer_sa_email" {
  value       = google_service_account.consumer.email
  description = "Email of the API consumer service account. Use this to create JSON keys and distribute to developers."
}

output "admin_sa_email" {
  value       = google_service_account.admin.email
  description = "Email of the admin service account. Used by Cloud Run. NEVER point SERVICE_ACCOUNT_EMAIL at this."
}

output "invoker_sa_email" {
  value       = google_service_account.invoker.email
  description = "Email of the Pub/Sub invoker service account."
}

output "pubsub_topic" {
  value       = google_pubsub_topic.budget_alerts.name
  description = "Pub/Sub topic that receives billing budget alerts."
}

output "pubsub_subscription" {
  value       = google_pubsub_subscription.budget_alerts.name
  description = "Pub/Sub subscription that pushes alerts to Cloud Run."
}

# -----------------------------------------------------------------------------
# Claude Code configuration snippet
#
# After creating a JSON key for the consumer SA (see docs/MANUAL_STEPS.md),
# developers can use these env vars to configure Claude Code.
# -----------------------------------------------------------------------------

output "claude_code_env_snippet" {
  value = <<-EOT
    # Claude Code configuration for Vertex AI
    export CLAUDE_CODE_USE_VERTEX=1
    export CLOUD_ML_REGION=${var.vertex_ai_region}
    export ANTHROPIC_VERTEX_PROJECT_ID=${var.project_id}
    export DISABLE_PROMPT_CACHING=1
    export DISABLE_NON_ESSENTIAL_MODEL_CALLS=1
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/your-key.json
    # Set model names from Model Garden (check for latest versions):
    # export ANTHROPIC_MODEL='claude-sonnet-4-5@20250929'
    # export ANTHROPIC_SMALL_FAST_MODEL='claude-haiku-4-5@20251001'
  EOT
  description = "Shell snippet for developers to configure Claude Code with this project's Vertex AI setup."
}
