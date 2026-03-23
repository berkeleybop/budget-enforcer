# CLAUDE.md — Budget Enforcer

## What this repo is

A GCP Cloud Run service that enforces Vertex AI spending limits by
disabling service account keys when budget thresholds are exceeded.
Used by the BBOP group at Lawrence Berkeley National Lab.

## Key concepts

- **Three-identity model**: Personal (Owner), Admin SA, Consumer SA.
  Never confuse them. The budget-enforcer disables the Consumer SA's
  keys — if it targets the Admin SA instead, it locks itself out.
- **Budget scopes to ALL services**, not just "Vertex AI". Claude model
  charges bill under a marketplace service category. A budget scoped
  only to "Vertex AI" will miss most of the actual spend.
- **The Pub/Sub OIDC auth binding is fragile.** It can break after Cloud
  Run redeployments. Terraform manages it as a separate resource to
  prevent this, but be aware during manual operations.
- **GCP Billing notifications lag 12-24 hours.** The `/check-usage`
  endpoint with Cloud Scheduler (every 5 min) fills this gap for
  real-time cost spikes, but it depends on Cloud Monitoring data
  which can also lag slightly.

## Repository layout

- `main.py` — The Flask app (Cloud Run entry point)
- `Dockerfile` — Container build
- `terraform/` — Infrastructure-as-code for all GCP resources
- `terraform/variables.tf` — All configurable parameters with descriptions
- `terraform/terraform.tfvars.example` — Template; copy to `.tfvars` and fill in
- `docs/SOP.md` — Full operational runbook (manual procedures, recovery)
- `docs/MANUAL_STEPS.md` — Steps that Terraform cannot automate

## Working with Terraform

```bash
cd terraform/
terraform init
terraform plan    # Always review before applying
terraform apply
```

Variables are in `terraform.tfvars` (gitignored). The example file
shows what's needed. Required variables:
- `project_id` — GCP project ID (the string, not the numeric project number)
- `billing_account_id` — GCP billing account ID (format: XXXXXX-XXXXXX-XXXXXX)
- `container_image` — Built container image URI

### Resource prefix

All GCP resource names are prefixed with `resource_prefix` (default: `tf`).
This allows Terraform-managed resources to coexist with manually created
ones in the same project (e.g. `tf-budget-enforcer` alongside
`budget-enforcer`). Change the prefix if deploying multiple independent
instances in one project.

### State is local-only

There is no shared remote backend. Each developer keeps their own
`terraform.tfstate` on their local machine. This means:
- If you need to manage someone else's deployment, **ask them for
  their state file** (or re-import resources into a fresh state).
- Two developers should never run `terraform apply` against the same
  GCP project without coordinating — there is no state locking.
- State files contain sensitive resource IDs. Transfer securely.
- If state is lost, resources can be reconstructed with
  `terraform import` (see `terraform/backend.tf` for guidance).

### Authentication for Terraform

Terraform must run as your **personal Google account** (Owner role),
not as a service account. This is because:
- IAM policy bindings on Cloud Run require `run.services.setIamPolicy`,
  which the admin SA's Editor role does not include.
- Billing budget creation requires billing account access.
- The provider uses `billing_project` and `user_project_override` to
  route Billing Budget API calls through your project's quota (without
  these, ADC gets a 403 "requires a quota project" error).

Authenticate with:
```bash
gcloud auth application-default login
```

## Secrets and credentials

- **Never commit JSON key files.** They are gitignored (`*.json`).
- **Never commit terraform.tfvars.** It is gitignored.
- **No real project IDs, SA emails, or keys** should appear in any
  committed file. Use variables and placeholder values only.
- Consumer SA JSON keys are created manually, not via Terraform,
  to keep secrets out of Terraform state.

## Budget limit (the most important setting)

The monthly spending cap is `monthly_budget_amount` in `terraform.tfvars`
(default: $100). When spend reaches this, ALL consumer SA keys are disabled
and Vertex AI access stops immediately. Related real-time limits:
- `usage_hourly_limit` (default: $10) — checked every 5 min via Cloud Scheduler
- `usage_daily_limit` (default: $50) — same mechanism

These are faster than GCP Billing (which can lag 12-24 hours).
All three are defined in `terraform/variables.tf` with full descriptions.

## How the enforcement chain works

Understanding the flow helps when debugging:

1. GCP Billing evaluates spend against budget thresholds periodically
2. At 100% threshold, Billing publishes a JSON message to the Pub/Sub topic
3. The Pub/Sub subscription pushes the message to the Cloud Run URL
4. Pub/Sub authenticates using the invoker SA's OIDC token
5. Cloud Run validates the OIDC token (this is where 403s happen if
   the invoker SA lacks `roles/run.invoker`)
6. `main.py` decodes the message and compares `costAmount >= budgetAmount`
7. If exceeded, it lists ALL keys on the consumer SA and disables
   every USER_MANAGED key (Google-managed keys are skipped)
8. Applications using those keys immediately lose Vertex AI access

**Where things break (in order of likelihood):**
- Invoker SA lost `roles/run.invoker` binding (after Cloud Run redeploy)
- Pub/Sub subscription missing OIDC auth config
- Budget scoped to wrong services (missing Claude marketplace charges)
- `SERVICE_ACCOUNT_EMAIL` pointing at admin SA instead of consumer SA

## Testing the pipeline

To verify the full chain without waiting for real billing:

```bash
# Send a simulated over-budget alert (THIS WILL DISABLE KEYS)
gcloud pubsub topics publish TOPIC_NAME \
  --project=PROJECT_ID \
  --message='{"budgetAmount": 5.00, "costAmount": 5.01, "budgetDisplayName": "test"}'

# Check Cloud Run logs (expect 200, not 403)
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=SERVICE_NAME" \
  --project=PROJECT_ID --limit=5 \
  --format="table(timestamp, textPayload, httpRequest.status)"

# Verify key was disabled
gcloud iam service-accounts keys list \
  --iam-account=CONSUMER_SA_EMAIL

# Re-enable the key after testing
gcloud iam service-accounts keys enable KEY_ID \
  --iam-account=CONSUMER_SA_EMAIL
```

Replace `TOPIC_NAME`, `SERVICE_NAME`, etc. with values from
`terraform output`.

## Common tasks

- **Add a new GCP API**: Add to `local.required_apis` in `terraform/main.tf`
- **Change budget threshold**: Update `monthly_budget_amount` in `terraform.tfvars`
- **Change usage limits**: Update `usage_hourly_limit` / `usage_daily_limit` in `terraform.tfvars`
- **Change resource prefix**: Update `resource_prefix` in `terraform.tfvars`
- **Update the Flask app**: Edit `main.py`, rebuild container, update `container_image`
- **Recovery after key disable**: See `docs/SOP.md` recovery section (R1-R4)
- **Get all operational values**: `terraform output`
- **Get Claude Code config**: `terraform output claude_code_env_snippet`

## Style and conventions

- Terraform files use the standard HashiCorp style (`terraform fmt`)
- Python follows the existing minimal style in `main.py`
- Comments explain *why*, not *what*
- Variable descriptions in `variables.tf` are the primary documentation
  for infrastructure configuration
