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

### Redeploy after a code change

Editing `main.py` (or anything else baked into the container) requires
a fresh image, a `terraform.tfvars` digest bump, and an apply. Because
we pin `container_image` by `@sha256:` digest, each rebuild produces a
real Terraform diff and Cloud Run rolls a new revision in-place — the
service is not destroyed, so its IAM policies (including the critical
Pub/Sub invoker binding) stay attached.

```bash
# 1. Build, push, and capture the digest in one go (from repo root)
gcloud builds submit --tag gcr.io/$PROJECT_ID/budget-enforcer . \
  --format='value(results.images[0].digest)'
# Output: sha256:abc123...

# 2. Update terraform.tfvars with the new digest:
#      container_image = "gcr.io/$PROJECT_ID/budget-enforcer@sha256:abc123..."

# 3. Apply — plain apply, no -replace needed
cd terraform/
terraform apply

# 4. Smoke-test the billing path (see "Smoke test after any redeploy"
#    below) — takes 10 seconds, catches invoker-binding drift.
```

**Do NOT use `terraform apply -replace=google_cloud_run_v2_service.…`**
to roll a new image. That destroys and recreates the service, which
wipes its attached IAM bindings. The separate `iam_member` resource
then becomes stale in state and drops out on next refresh, silently
breaking the billing-alert path. The digest-bump flow above avoids
this entirely.

The consumer SA and its JSON keys are not in Terraform state, so they
are untouched by any apply. Slack webhook, budget thresholds, and
Cloud Scheduler jobs are re-applied from `terraform.tfvars` with no
change in value.

### Smoke test after any redeploy

After any `terraform apply` that touches the Cloud Run service, verify
the billing-alert path is still wired end-to-end. This takes ~10 seconds
and catches the exact drift mode described above.

```bash
# Publish a sub-threshold test message. costAmount < budgetAmount so
# keys will NOT be disabled — the service just acknowledges and returns.
gcloud pubsub topics publish tf-budget-alerts \
  --project=$PROJECT_ID \
  --message='{"budgetAmount":100,"costAmount":1,"budgetDisplayName":"healthcheck"}'

# Wait ~10s, then check logs. Expect HTTP 200 and text
# "Budget alert received but threshold not met".
# If you see 403, the invoker IAM binding is broken — run
# `terraform plan` and look for a missing
# google_cloud_run_v2_service_iam_member.invoker.
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=tf-budget-enforcer" \
  --project=$PROJECT_ID --limit=3 \
  --format="table(timestamp, httpRequest.status, textPayload)"
```

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
| Slack "spend estimator unavailable" warning | Both Cloud Monitoring queries (`token_count` and `response_count`) failed: API outage, throttling, or the admin SA lost `monitoring.viewer`. Keys are **not** disabled; only the billing path enforces until it recovers | Check the logged error in Cloud Run logs; `terraform apply` restores IAM bindings. Repeats at most every `ESTIMATOR_ALERT_INTERVAL_MINUTES` (default 60) per instance |
| Scheduler job shows 403 | Invoker SA lost `roles/run.invoker` | `terraform apply` restores it |

---

# AWS: Amazon Bedrock

The same pattern on AWS, in `aws/`: `handler.py` is the Lambda, and
`terraform/` is a reusable module that the deploying repo calls.  What
differs from GCP:

| GCP | AWS |
|---|---|
| Consumer service account, JSON key | Consumer IAM user, one access key |
| `DisableServiceAccountKey` | `UpdateAccessKey --status Inactive` (every active key) |
| Billing budget -> Pub/Sub -> Cloud Run | AWS Budgets (100% actual) -> SNS -> Lambda |
| Cloud Scheduler -> `/check-usage`, 48h window | EventBridge every 5 min, window = calendar month (UTC) |
| Cloud Monitoring `token_count` | CloudWatch `AWS/BedrockMantle` `TotalInputTokens`/`TotalOutputTokens` (Model) and `AWS/Bedrock` `InputTokenCount`/`OutputTokenCount` (ModelId) |
| A restore can be revoked again at once (see the R2 warning) | An override tag on the consumer user suppresses revocation until a set time |

These differences in behaviour are deliberate:
- **Input tokens are priced at the cache-write rate.**  Mantle publishes no
  cache breakdown, so this is an upper bound.
- **Once the key is revoked, the enforcer stays quiet.**  With no active key
  it does nothing and posts nothing, so a tripped budget does not post on
  every run.
- **Estimator failure** (CloudWatch errors): a Slack warning, cat keeps
  running, nothing is revoked.  The billing path still enforces.
- **The budget is unfiltered.**  Claude bills as one Marketplace product
  per model ("Claude Opus 5 (Amazon Bedrock Edition)").  Deploy it in an
  account where this application is the only Bedrock workload.

## AWS setup

1. In the deploying repo, call `aws/terraform` as a module from a root
   configuration whose provider pins `allowed_account_ids`.  Run
   `terraform plan`, read it, and apply.  Then confirm the SNS email
   subscriptions from the mailbox; until you do, the threshold and alarm
   mails never arrive.
2. Create the consumer's access key **by hand**, never in Terraform, which
   would put it in state.  Using an admin profile:
   `aws iam create-access-key --user-name <consumer>`.  Put the key pair
   straight into the application's credentials file.  The application must
   use a named profile from that file and nothing else, so that a disabled
   key has no fallback:
   - keep `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` unset in its
     environment;
   - give its host role no Bedrock permissions.
3. Bedrock model access: an admin creates the Marketplace agreement for
   each model once, before the consumer's first call.  The consumer has no
   `aws-marketplace:*` permissions.

## AWS test (before the application depends on it)

1. `aws lambda invoke --function-name <name>-budget-enforcer --payload '{}' out.json`
   prints the estimate; expect `"action": "none"`.
2. Test revocation end to end by invoking with an event shaped like a
   budget notification:
   `{"Records":[{"EventSource":"aws:sns","Sns":{"Subject":"test","Message":"test"}}]}`.
   Expect all of:
   - `keys_disabled` in the output;
   - the key Inactive (`aws iam list-access-keys --user-name <consumer>`);
   - a Slack post;
   - a failing Bedrock call made with the key.

   Then recover as in R-AWS below, and confirm a call succeeds.

## R-AWS: recover after a revocation

Find the cause first, from the per-model CloudWatch metrics and the
application's logs.  Then do the following, so that the next 5-minute check
does not revoke the key again:

```bash
# 1. Set an override until a time of your choosing (UTC, ISO 8601)
aws iam tag-user --user-name <consumer> \
  --tags Key=budget-enforcer-override-until,Value=2026-11-01T00:00:00Z
# 2. Re-enable the key
aws iam list-access-keys --user-name <consumer>
aws iam update-access-key --user-name <consumer> --access-key-id <id> --status Active
```

The override covers both paths and expires by itself.  To remove it early:
`aws iam untag-user --user-name <consumer> --tag-keys budget-enforcer-override-until`.
Raising the budget in Terraform also works, but it loosens the cap for the
rest of the month.

## AWS troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| "spend estimator unavailable" on Slack | CloudWatch API errors (throttling, permissions) | Lambda logs in `/aws/lambda/<name>-budget-enforcer` |
| `<name>-budget-enforcer-not-running` alarm | EventBridge rule disabled, or the Lambda permission lost | `terraform plan` shows the drift |
| `<name>-budget-enforcer-errors` alarm | An exception in the handler, for example `AccessDenied` on `UpdateAccessKey` from an SCP or the role policy | Lambda logs |
| Estimate near zero while the app is busy | The model id is missing from the metric dimensions the handler reads, or the metrics are not yet published | `aws cloudwatch list-metrics --namespace AWS/BedrockMantle` |
| An unknown model in the logs (`FALLBACK_PRICING`) | The model changed; add it to `PRICING` in `aws/handler.py` | AWS Price List `AmazonBedrockFoundationModels` |
