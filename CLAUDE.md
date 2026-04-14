# CLAUDE.md — Budget Enforcer

## Your role

You (Claude) are the primary operator of this system. Developers work
with you to deploy, configure, monitor, and recover the budget enforcer.
When a developer asks you to do something with this repo, you should:

1. Read this file for context and constraints
2. Use Terraform (`terraform/`) for infrastructure changes
3. Use `gcloud` commands for operations and recovery (see `docs/SOP.md`)
4. Use `docs/MANUAL_STEPS.md` to guide the developer through browser-
   based steps that you cannot do (Model Garden, Slack setup, etc.)

The developer makes decisions. You execute. The docs serve both of you.

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

Terraform can run as either a **personal Google account** or a
**service account** with sufficient permissions. In testing, a service
account with Editor + Project IAM Admin + Service Account Key Admin
roles was sufficient to create all resources, including the Cloud Run
IAM binding that the manual SOP requires Owner for.

The key permission requirements are:
- **Cloud Run IAM bindings**: The manual SOP requires Owner because
  `gcloud run services add-iam-policy-binding` needs
  `run.services.setIamPolicy`. Terraform's IAM resources use a
  different API path and work with Project IAM Admin.
- **Billing budget creation**: Requires access to the billing account.
  The provider uses `billing_project` and `user_project_override` to
  route Billing Budget API calls through your project's quota (without
  these, ADC gets a 403 "requires a quota project" error).

If using a personal account:
```bash
gcloud auth application-default login
```

If using a service account:
```bash
gcloud auth activate-service-account --key-file=PATH_TO_KEY.json
export GOOGLE_APPLICATION_CREDENTIALS=PATH_TO_KEY.json
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

## Two enforcement mechanisms

The budget enforcer has two independent enforcement paths. Both
disable the same consumer SA keys, but they operate on different
timescales and data sources.

### 1. Billing-based (POST /) — accurate but slow

1. GCP Billing evaluates spend against budget thresholds periodically
2. At 100% threshold, Billing publishes a JSON message to the Pub/Sub topic
3. The Pub/Sub subscription pushes the message to the Cloud Run URL
4. Pub/Sub authenticates using the invoker SA's OIDC token
5. Cloud Run validates the OIDC token (this is where 403s happen if
   the invoker SA lacks `roles/run.invoker`)
6. `main.py` decodes the message and compares `costAmount >= budgetAmount`
7. If exceeded, keys are disabled

**Lag: 12-24 hours** (GCP billing data delay). This is the ground truth
but it means a user could burn $50+ past a $5 budget before it fires.

### 2. Flux-based (GET /check-usage) — precise and fast

1. Cloud Scheduler hits `/check-usage` every 5 minutes
2. The endpoint queries Cloud Monitoring for actual token counts
   (`publisher/online_serving/token_count` — works for both Anthropic
   and Google models)
3. It computes cost per model using the PRICING table in `main.py`,
   including cache discounts and the regional premium
4. It compares against: `FLUX_BUDGET * ENFORCEMENT_TOLERANCE`
5. If exceeded, keys are disabled and Slack is notified (if configured)

**Lag: 3-5 minutes** (Cloud Monitoring data delay).

If token metrics return no data, falls back to counting API calls
(`response_count`) with a conservative per-call cost estimate.

### Enforcement tolerance

The `ENFORCEMENT_TOLERANCE` scalar controls how aggressively the flux
estimator triggers:
- `0.8` = enforce at 80% of budget (conservative, prefer interruption)
- `1.0` = enforce at exactly the budget (default)
- `1.2` = allow 20% overspend (tolerant, fewer false positives)

This only affects the flux estimator. The billing-based path always
enforces at exactly 100%.

### Notifications

When keys are disabled, two notification channels can fire:
- **GCP budget alert emails** (50/75/90/95/100% thresholds) — sent to
  billing admins and project owners. See `docs/MANUAL_STEPS.md` step 6.
- **Slack webhook** (optional) — posts immediately when the enforcer
  disables keys, with project, SA, reason, and recovery pointer.
  See `docs/MANUAL_STEPS.md` step 7 for setup.

**Where things break (in order of likelihood):**
- Invoker SA lost `roles/run.invoker` binding (after Cloud Run redeploy)
- Pub/Sub subscription missing OIDC auth config
- `FLUX_BUDGET` set to 0 or not matching `monthly_budget_amount`
- Budget scoped to wrong services (missing Claude marketplace charges)
- `SERVICE_ACCOUNT_EMAIL` pointing at admin SA instead of consumer SA
- Slack webhook URL expired or channel archived (notifications fail silently)

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
- **Tune enforcement aggressiveness**: Update `enforcement_tolerance` in `terraform.tfvars`
- **Switch to Google models**: Set `flux_mode = "token"` in `terraform.tfvars`
- **Adjust cost-per-call estimate**: Update `cost_per_call_expensive` in `terraform.tfvars`
- **Change resource prefix**: Update `resource_prefix` in `terraform.tfvars`
- **Update the Flask app**: Edit `main.py`, rebuild container, `terraform apply`
- **Recovery after key disable**: See `docs/SOP.md` recovery section (R1-R4)
- **Get all operational values**: `terraform output`
- **Get Claude Code config**: `terraform output claude_code_env_snippet`
- **Set up Slack notifications**: Add `slack_webhook_url` in `terraform.tfvars`
- **Check billing admins**: See `docs/MANUAL_STEPS.md` step 6
- **Check flux estimator status**: `curl -s CLOUD_RUN_URL/status | python3 -m json.tool`

## Pricing table maintenance

The `PRICING` dict in `main.py` contains per-model token costs used by
the flux estimator. **Check and update this periodically** — model prices
change when new versions are released or old models are deprecated.

Sources:
- Anthropic: https://docs.anthropic.com/en/docs/about-claude/models
- Google: https://cloud.google.com/vertex-ai/generative-ai/pricing

Things to check:
- New model versions added (e.g. a new Opus or Gemini release)
- Price changes on existing models
- Deprecated models removed from availability
- `REGIONAL_PREMIUM` (default 1.10 for us-east5) — verify the 10%
  regional surcharge still applies for your endpoint region
- `CACHE_MULTIPLIERS` — verify cache pricing hasn't changed
- `FALLBACK_PRICING` — should match the most expensive model you
  might encounter, so unknown models overestimate rather than under.
  **Keep this aggressive.** The current value (Opus 4.1 at $15/$75
  per MTok) is intentionally the worst-case "disaster pricing"
  ceiling, not a value to right-size against currently-enabled
  models. If a new model appears that the estimator doesn't know
  about, we want it overestimated so the budget enforcer fires
  early rather than letting spend slip past. Do not lower this
  just because the actively-enabled models are cheaper.

When updating: edit `main.py`, rebuild the container, `terraform apply`.

### Pseudo-models in the token_count metric

The `publisher/online_serving/token_count` metric doesn't only report
real billable models. Anthropic's pre-flight utility endpoints show up
as pseudo-models with `model_user_id` values that look like model names
but aren't. Known pseudo-models:

- `count-tokens` — the Anthropic `count_tokens` API endpoint. Reports
  0 input / 0 output tokens per call (it counts tokens, it doesn't
  generate them).

These have no billing impact, but if they aren't in the `PRICING`
dict they trip the "unknown model" warning we log on fallback, which
creates log noise. Keep them in `PRICING` with `{"input": 0, "output": 0}`
so the warning stays reserved for genuinely-unknown real models.

If a new unknown-model warning appears in Cloud Run logs, first check
whether it's a pseudo-model (0 tokens, looks like an API endpoint name)
or a real billable model. Add pseudo-models at zero cost; add real
models at their actual price.

## Style and conventions

- Terraform files use the standard HashiCorp style (`terraform fmt`)
- Python follows the existing minimal style in `main.py`
- Comments explain *why*, not *what*
- Variable descriptions in `variables.tf` are the primary documentation
  for infrastructure configuration
