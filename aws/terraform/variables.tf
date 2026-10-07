variable "name" {
  type        = string
  description = <<-EOT
    Short deployment name.  Prefixes every resource (Lambda, role, budget,
    topics) and appears in Slack messages.  Example: "cat-bedrock".
  EOT
}

variable "consumer_user_name" {
  type        = string
  description = <<-EOT
    IAM user that holds the application's only Bedrock credential (one
    access key).  The enforcer disables this user's keys.  Created here;
    its access key is NOT created by Terraform (keeps the secret out of
    state): create it by hand once, see docs/SOP.md, AWS section.
  EOT
}

variable "monthly_budget_usd" {
  type        = number
  description = "Monthly spend cap in USD, for both enforcement paths."
}

variable "enforcement_tolerance" {
  type        = number
  default     = 1.0
  description = "Flux path revokes at monthly_budget_usd x this.  <1 earlier, >1 later."
}

variable "flux_window" {
  type        = string
  default     = "month"
  description = "\"month\" (since the 1st, UTC) or a number of hours."
}

variable "allowed_mantle_models" {
  type        = list(string)
  description = <<-EOT
    Model ids the consumer may call on the bedrock-mantle endpoint
    (bedrock-mantle:CreateInference), matched with the bedrock-mantle:Model
    condition key.  Example: ["anthropic.claude-opus-5"].
  EOT
}

variable "allowed_runtime_model_arns" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Foundation-model ARNs the consumer may call with bedrock:InvokeModel on
    the bedrock-runtime endpoint (embeddings, for example).
  EOT
}

variable "alert_emails" {
  type        = list(string)
  description = <<-EOT
    Recipients of the budget threshold emails and of the enforcer-health
    alarms.  SNS email subscriptions must be confirmed from the mailbox.
  EOT
}

variable "email_thresholds_percent" {
  type        = list(number)
  default     = [50, 75, 90, 95, 100]
  description = "Actual-spend thresholds that send email.  100 also triggers enforcement."
}

variable "slack_webhook_url" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Slack incoming webhook for revocations and estimator warnings.  Empty disables."
}

variable "schedule_expression" {
  type        = string
  default     = "rate(5 minutes)"
  description = "How often the flux path runs."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags applied to every resource that supports them."
}
