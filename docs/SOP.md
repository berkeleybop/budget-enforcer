# Budget Enforcer SOP

## Overview

This document covers **deployment, operations, and recovery** for the
budget-enforcer.

**These commands are typically run by Claude Code on your behalf.** When
you ask Claude to deploy, check spend, change the budget, or recover
from enforcement, these are the commands it uses. They're documented
here so you can:

- **Understand** what Claude is doing when it operates the system
- **Verify** Claude's work by checking the same outputs
- **Operate manually** if you need to work without Claude

> **For initial setup**, follow `docs/MANUAL_STEPS.md` and then
> `terraform apply`. This SOP is for understanding, operating, and
> recovering the system after it's deployed.

## How it works

```
GCP Billing ──► Pub/Sub topic ──► Cloud Run (POST /) ──► Disables keys
  12-24h lag                        budget-enforcer

Cloud Scheduler ──► Cloud Run (GET /check-usage) ──► Disables keys
  every 5 min         estimates spend from token counts
```

Both paths disable the consumer SA's JSON keys via the IAM API. When
keys are disabled, applications (e.g. Claude Code) can no longer call
Vertex AI. A Slack notification is sent if configured.

## Account model — three identities

| Identity | Role | Purpose |
|---|---|---|
| **You** (your @lbl.gov Google account) | Owner | Full control; runs Terraform; browser-based steps |
| **Admin SA** (`tf-budget-enforcer-admin@...`) | Editor, SA Key Admin, etc. | Runs the Cloud Run service; disables consumer keys |
| **Consumer SA** (`tf-vertex-ai-consumer@...`) | Vertex AI User | Used by applications; **gets its keys disabled** |

> **Critical:** `SERVICE_ACCOUNT_EMAIL` must point at the consumer SA,
> never the admin SA. Terraform enforces this by construction, but if
> you ever set it manually, double-check. Pointing it at the admin SA
> locks you out.

---

## Deployment — what Terraform does

When you run `terraform apply`, here's what happens. These are the
equivalent `gcloud` commands for each resource, so you can understand
what's being created and manually fix things if needed.

### APIs enabled

```bash
gcloud services enable run.googleapis.com
gcloud services enable pubsub.googleapis.com
gcloud services enable iam.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable cloudscheduler.googleapis.com
gcloud services enable cloudresourcemanager.googleapis.com
gcloud services enable monitoring.googleapis.com
gcloud services enable billingbudgets.googleapis.com
```

### Service accounts created

```bash
# Consumer SA — used by applications, gets keys disabled
gcloud iam service-accounts create tf-vertex-ai-consumer \
  --display-name="tf Vertex AI API Consumer"

# Admin SA — runs the Cloud Run service, disables consumer keys
gcloud iam service-accounts create tf-budget-enforcer-admin \
  --display-name="tf Budget Enforcer Admin"

# Invoker SA — used by Pub/Sub to authenticate to Cloud Run
gcloud iam service-accounts create tf-pubsub-invoker \
  --display-name="tf Pub/Sub Cloud Run Invoker"
```

### IAM bindings

Consumer SA gets Vertex AI access:
```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:tf-vertex-ai-consumer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/aiplatform.serviceAgent"
# Also: roles/aiplatform.viewer
```

Admin SA gets elevated permissions:
```bash
# roles/editor, roles/iam.serviceAccountKeyAdmin,
# roles/serviceusage.serviceUsageAdmin, roles/resourcemanager.projectIamAdmin,
# roles/run.admin, roles/monitoring.viewer
```

### Container build and Cloud Run deployment

```bash
# Build (you do this manually before terraform apply)
gcloud builds submit --tag gcr.io/$PROJECT_ID/budget-enforcer .

# Terraform creates the Cloud Run service with these env vars:
#   SERVICE_ACCOUNT_EMAIL = tf-vertex-ai-consumer@...  (consumer, not admin!)
#   GCP_PROJECT_ID        = your project ID
#   FLUX_BUDGET           = your monthly_budget_amount
#   FLUX_WINDOW_HOURS     = 48
#   ENFORCEMENT_TOLERANCE = 1.0
#   COST_PER_CALL_FALLBACK = 0.30
#   SLACK_WEBHOOK_URL     = (if configured)
```

### Pub/Sub wiring

```bash
# Topic that receives billing alerts
gcloud pubsub topics create tf-budget-alerts

# Subscription that pushes to Cloud Run with OIDC authentication
# This is the critical piece — without OIDC auth, Cloud Run returns 403
gcloud pubsub subscriptions create tf-budget-alerts-sub \
  --topic=tf-budget-alerts \
  --push-endpoint=$CLOUD_RUN_URL \
  --push-auth-service-account=tf-pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com

# Invoker SA gets permission to call Cloud Run
# (Terraform manages this as a separate resource so it survives redeployments)
gcloud run services add-iam-policy-binding tf-budget-enforcer \
  --region=us-central1 \
  --member="serviceAccount:tf-pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

### Cloud Scheduler

```bash
# Hits /check-usage every 5 minutes for real-time spend estimation
gcloud scheduler jobs create http tf-check-vertex-usage \
  --schedule="*/5 * * * *" \
  --uri="${CLOUD_RUN_URL}/check-usage" \
  --http-method=GET \
  --oidc-service-account-email=tf-pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com \
  --oidc-token-audience="${CLOUD_RUN_URL}"
```

### Billing budget

```bash
# Created via Terraform's google_billing_budget resource
# Scoped to ALL services (not just Vertex AI — Claude charges bill
# under a marketplace service category)
# Thresholds at 50%, 75%, 90%, 95%, 100%
# Connected to the tf-budget-alerts Pub/Sub topic
```

---

## Day-to-day operations

### Check current spend estimate

Trigger the flux estimator manually and check the logs:

```bash
# Trigger
gcloud scheduler jobs run tf-check-vertex-usage \
  --project=$PROJECT_ID --location=us-central1

# Check logs (wait ~10 seconds)
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=tf-budget-enforcer" \
  --project=$PROJECT_ID --limit=5 \
  --format="table(timestamp, textPayload, httpRequest.status)"
```

### Check token usage and estimated cost

Query Cloud Monitoring directly for per-model token counts:

```bash
START_TIME=$(date -u -d '2 days ago' +%Y-%m-%dT%H:%M:%SZ)
END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -s \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/$PROJECT_ID/timeSeries?filter=metric.type%3D%22aiplatform.googleapis.com%2Fpublisher%2Fonline_serving%2Ftoken_count%22&interval.startTime=${START_TIME}&interval.endTime=${END_TIME}"
```

### Change the budget

Edit `monthly_budget_amount` in `terraform/terraform.tfvars`, then:

```bash
cd terraform/
terraform apply
```

This updates both the GCP billing budget and the flux estimator's
`FLUX_BUDGET` in a single operation.

### Verify the pipeline end-to-end

Send a test message (**this will disable the consumer key**):

```bash
gcloud pubsub topics publish tf-budget-alerts \
  --project=$PROJECT_ID \
  --message='{"budgetAmount": 0.01, "costAmount": 0.02, "budgetDisplayName": "test"}'
```

Then verify: Cloud Run logs show 200, consumer key is disabled, Slack
notification was sent. Re-enable the key afterward (see Recovery below).

### Tear down everything

```bash
cd terraform/
terraform destroy
```

This removes all `tf-` prefixed resources. APIs are left enabled
(`disable_on_destroy = false`).

---

## Recovery: when keys are disabled

When the budget-enforcer disables keys (either from a billing alert or
the flux estimator), follow these steps to restore service.

### R1: Re-enable the consumer SA key

```bash
# List keys to find the disabled one
gcloud iam service-accounts keys list \
  --iam-account=tf-vertex-ai-consumer@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID

# Re-enable it (replace KEY_ID with the ID from above)
gcloud iam service-accounts keys enable KEY_ID \
  --iam-account=tf-vertex-ai-consumer@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

Confirm the key is active (DISABLED column should be empty):

```bash
gcloud iam service-accounts keys list \
  --iam-account=tf-vertex-ai-consumer@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

### R2: Decide whether to adjust the budget

If the budget was legitimately exceeded:
- Increase `monthly_budget_amount` in `terraform.tfvars` and
  `terraform apply`

If it was a false positive from the flux estimator:
- Increase `enforcement_tolerance` (e.g. 1.2 to allow 20% overshoot)
- Or increase `cost_per_call_fallback` if the fallback was used

> **Warning:** GCP re-evaluates budget thresholds every time you save
> a budget. If current spend already exceeds the new threshold, it will
> fire immediately and disable the key you just re-enabled. Temporarily
> increase the budget above current spend, then adjust down next month.

### R3: Verify the pipeline still works

After re-enabling the key, confirm the Pub/Sub -> Cloud Run path is
intact (it can break after Cloud Run redeployments):

```bash
# Check the invoker IAM binding exists
gcloud run services get-iam-policy tf-budget-enforcer \
  --project=$PROJECT_ID --region=us-central1
```

You should see `tf-pubsub-invoker` listed under `roles/run.invoker`.
If it's missing, run `terraform apply` — Terraform will restore it.

### R4: Verify end-to-end (optional)

If you want to confirm the full pipeline works, send a test message
(see "Verify the pipeline end-to-end" above). Remember to re-enable
the key afterward.

---

## Emergency recovery: admin SA was disabled

This can only happen if `SERVICE_ACCOUNT_EMAIL` was manually changed to
point at the admin SA. (Terraform prevents this by construction.)

If you're locked out of the admin SA, use your personal Owner account:

```bash
gcloud auth login
gcloud config set project $PROJECT_ID

# List admin SA keys
gcloud iam service-accounts keys list \
  --iam-account=tf-budget-enforcer-admin@${PROJECT_ID}.iam.gserviceaccount.com

# Re-enable the admin SA key
gcloud iam service-accounts keys enable KEY_ID \
  --iam-account=tf-budget-enforcer-admin@${PROJECT_ID}.iam.gserviceaccount.com

# Fix the deployment — run terraform apply to restore correct config
cd terraform/
terraform apply
```

---

## Budget behavior notes

- **GCP billing data lags 12-24 hours.** The flux estimator bridges
  this gap by checking Cloud Monitoring token counts every 5 minutes.
- **Every time you edit and save a budget**, GCP immediately
  re-evaluates all thresholds. This is useful for testing but can
  trigger enforcement unexpectedly.
- **Sub-100% thresholds (50%, 75%, 90%, 95%) only send emails.** The
  budget-enforcer only disables keys at 100% (billing path) or when
  the flux estimate exceeds the tolerance-adjusted threshold.
- **Budget scopes to ALL services**, not just Vertex AI. Claude
  charges bill under a marketplace service category.

### Silent failure warning

If the Pub/Sub OIDC auth is broken, the 100% threshold will fail
silently — you won't receive a 100% email. You WILL still receive
75%/95% emails (sent by GCP directly). If you get early-warning emails
but no 100% email and keys aren't disabled, check Cloud Run logs for
403 errors and run `terraform apply` to restore the IAM binding.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Admin SA key disabled after budget alert | `SERVICE_ACCOUNT_EMAIL` pointing at admin SA | See "Emergency recovery"; `terraform apply` to restore |
| 403 in Cloud Run logs | Pub/Sub OIDC auth broken | `terraform apply` restores the invoker IAM binding |
| 500 in Cloud Run logs | Code error in budget-enforcer | Check logs: `gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=tf-budget-enforcer" --project=$PROJECT_ID --limit=10` |
| Got 75%/95% emails but no 100%, keys not disabled | Pub/Sub OIDC auth broken (silent failure) | Check Cloud Run logs for 403; `terraform apply` |
| Budget email received but keys not disabled | Pub/Sub not reaching Cloud Run | Check subscription: `gcloud pubsub subscriptions describe tf-budget-alerts-sub --project=$PROJECT_ID` |
| Budget shows low spend but real costs are high | Budget scoped to "Vertex AI" only | `terraform apply` scopes to all services by default |
| `PERMISSION_DENIED` on `terraform apply` | Missing `resourcemanager.projectIamAdmin` role | Use Owner account or add the role to your SA |
| Flux estimate is $0 but usage exists | Model not in PRICING dict in `main.py` | Add the model, rebuild container, `terraform apply` |
| Flux estimate much higher than billing | Regional premium or fallback pricing too conservative | Check `/status` endpoint; adjust `enforcement_tolerance` |
| Slack notification not sent | `SLACK_WEBHOOK_URL` empty or webhook expired | Check `terraform.tfvars`; test webhook URL manually |
| Scheduler job shows 403 | Invoker SA lost `roles/run.invoker` | `terraform apply` restores it |
