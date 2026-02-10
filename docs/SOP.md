# Budget Enforcer SOP

## Overview

GCP Billing sends alert to Pub/Sub topic -> Cloud Run service receives
Pub/Sub message -> Service disables the **API consumer** service account's
JSON key via IAM API -> App can no longer make Vertex AI API calls -> Admin
is notified and can investigate -> Keys can be re-enabled via `gcloud` CLI
-> Service can resume once budget is addressed.

## Account Model — Three Identities

There are three distinct identities used in this setup. Mixing them up will
lock you out of admin operations.

| Identity | Email | Role | Purpose |
|---|---|---|---|
| **Personal (Owner)** | smoxon@... (your Google login) | Owner | IAM policy bindings on Cloud Run (Editor cannot do `run.services.setIamPolicy`) |
| **Admin SA** | `nmdc-llm-admin-service@nmdc-llm.iam.gserviceaccount.com` | Editor, Service Account Key Admin, Service Usage Admin, Project IAM Admin | Runs budget-enforcer Cloud Run service; **performs** key disabling |
| **API consumer SA** | `nmdc-llm-service-account@nmdc-llm.iam.gserviceaccount.com` | Vertex AI User | Used by your application to call Vertex AI; **gets its keys disabled** by budget-enforcer |

> **Critical:** `SERVICE_ACCOUNT_EMAIL` must point at the **API consumer SA**,
> never the admin SA. If you point it at the admin SA, the budget enforcer
> disables the very account that manages the project — locking you out.

## Prerequisites

- `gcloud` CLI installed locally
- An "admin" service account (`nmdc-llm-admin-service`) with the
  following roles: **Service Account Key Admin**, **Service Usage Admin**,
  **Editor**, and **Project IAM Admin**
- A separate "API consumer" service account (`nmdc-llm-service-account`)
  used by your application for Vertex AI calls
- The admin service account's JSON key file downloaded locally
- Access to a personal Google account with **Owner** role on the project
  (required for IAM policy binding on Cloud Run — the admin service account's
  Editor role does not include `run.services.setIamPolicy`)

---

## 1. Setup variables

```bash
export PROJECT_ID=<your-gcp-project-id>
export KEY_FILE=<path-to-admin-sa-json-key>
export ADMIN_SA=<admin-service-account-name>
export CONSUMER_SA=<consumer-service-account-name>
```

## 2. Authenticate and configure project

> **Run as: Admin SA**

```bash
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID
```

## 3. Enable required APIs

> **Run as: Admin SA**

```bash
gcloud services enable run.googleapis.com
gcloud services enable pubsub.googleapis.com
gcloud services enable iam.googleapis.com
gcloud services enable cloudbuild.googleapis.com
```

## 4. Create Pub/Sub topic

> **Run as: Admin SA**

```bash
gcloud pubsub topics create budget-alerts-01 --project=$PROJECT_ID
```

Verify:

```bash
gcloud pubsub topics list --project=$PROJECT_ID
```

## 5. Deploy Cloud Run service

> **Run as: Admin SA**

> **Important:** Run this command from the `budget-enforcer` repo directory.
> The `--source .` flag uploads the current directory — if run from `~` or
> another location, it will attempt to upload everything in that directory.

> **Important:** The `--set-env-vars` flag and its value must be on the same
> line with no line breaks.

> **Critical:** `SERVICE_ACCOUNT_EMAIL` must be the **API consumer SA**
> (`$CONSUMER_SA`), NOT the admin SA. The budget enforcer disables keys on
> whichever account this variable points to.

```bash
cd /path/to/budget-enforcer

gcloud run deploy budget-enforcer --source . --platform managed --region us-central1 --no-allow-unauthenticated --set-env-vars SERVICE_ACCOUNT_EMAIL=${CONSUMER_SA}@${PROJECT_ID}.iam.gserviceaccount.com,GCP_PROJECT_ID=${PROJECT_ID},USAGE_HOURLY_LIMIT=10.00,USAGE_DAILY_LIMIT=50.00
```

> **Note:** We use `--no-allow-unauthenticated` because only Pub/Sub should
> be able to invoke this service. Authentication is handled via the invoker
> service account created in the next step.

Capture the service URL:

```bash
export SERVICE_URL=$(gcloud run services describe budget-enforcer --region us-central1 --format="value(status.url)")
```

## 6. Create a Pub/Sub invoker service account

> **Run as: Admin SA**

This service account is used by Pub/Sub to authenticate when pushing
messages to Cloud Run. This is separate from both the admin and consumer
service accounts.

```bash
gcloud iam service-accounts create pubsub-invoker \
  --project=$PROJECT_ID \
  --display-name="Pub/Sub Cloud Run Invoker"
```

## 7. Grant the invoker service account permission to call Cloud Run

> **Run as: Personal account (smoxon) — then switch back to Admin SA**
>
> This command requires `run.services.setIamPolicy`, which the admin SA's
> Editor role does **not** include. You must run this as your personal
> Google account (Owner role).

```bash
# === SWITCH TO: Personal account (smoxon) ===
gcloud auth login
gcloud config set project $PROJECT_ID

# Grant the binding
gcloud run services add-iam-policy-binding budget-enforcer \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --member="serviceAccount:pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# === SWITCH BACK TO: Admin SA ===
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID
```

## 8. Create Pub/Sub subscription with OIDC authentication

> **Run as: Admin SA**

```bash
gcloud pubsub subscriptions create budget-alerts-sub-01 \
  --topic=budget-alerts-01 \
  --push-endpoint=$SERVICE_URL \
  --push-auth-service-account=pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

> **This is the critical step.** The `--push-auth-service-account` flag tells
> Pub/Sub to include an OIDC token signed by this service account when
> pushing to the Cloud Run endpoint. Without this, Cloud Run rejects the
> request with a 403.

If the subscription already exists, update it instead. Note that
`--push-endpoint` must be included alongside `--push-auth-service-account`
when updating, or the command will fail:

```bash
gcloud pubsub subscriptions update budget-alerts-sub-01 \
  --push-endpoint=$SERVICE_URL \
  --push-auth-service-account=pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

## 9. Set up the billing budget (manual, in Cloud Console)

1. Go to **Cloud Console > Billing > Budgets & alerts**
   (https://console.cloud.google.com/billing/budgets)
2. Click **Create Budget**
3. **Scope**:
   - Projects: Select your project (`nmdc-llm`)
   - Services: **Select all services** (do NOT scope to "Vertex AI" only —
     Claude and other partner model charges are billed under a separate
     service category and will not be captured by a Vertex AI-only budget)
   - Deselect all savings/discounts
4. Click **Next**
5. **Amount**:
   - Budget type: Specified amount
   - Target amount: e.g., `$0.05`
   - Include credits: No
6. **Actions**:
   - Keep threshold rules at 50%, 90%, 100% (or add 75%, 95% — these
     provide email-only early warnings; only 100% triggers key disabling)
   - Manage notifications -> **Connect a Pub/Sub topic**
   - Select: `budget-alerts-01`

> **Why "all services"?** Claude models (Opus, Sonnet, Haiku) accessed
> through Vertex AI Model Garden are billed under a marketplace/partner
> service line, not under "Vertex AI." A budget scoped only to "Vertex AI"
> will see pennies in Gemini usage while missing dollars in Claude charges.

### Budget behavior notes

- **Every time you edit and save a budget**, GCP immediately re-evaluates
  all thresholds and sends notifications for any that are currently exceeded.
  This is useful for testing but be aware it will fire Pub/Sub messages.
- **Sub-100% thresholds (50%, 75%, 90%, 95%) only send emails.** The
  budget-enforcer code only disables keys when `costAmount >= budgetAmount`
  (100%). The email thresholds serve as early warnings to you.
- **100% threshold sends both an email AND a Pub/Sub message.** The Pub/Sub
  message is what triggers the budget-enforcer to disable keys.

### Silent failure warning

> **If the budget-enforcer is misconfigured (e.g., Pub/Sub OIDC auth is
> broken), the 100% threshold will fail silently.** You will NOT receive a
> 100% email — the Pub/Sub delivery failure prevents it. You WILL still
> receive 75% and 95% emails because those are sent by Google directly and
> do not depend on the budget-enforcer.
>
> **The biggest source of failures is authentication.** Common causes:
> - `pubsub-invoker` SA lost its `roles/run.invoker` binding (e.g., after
>   a `gcloud run services update` created a new revision)
> - Pub/Sub subscription missing `--push-auth-service-account`
> - OIDC token audience mismatch after the Cloud Run URL changed
>
> If you receive 75%/95% emails but no 100% email and keys are not
> disabled, check Cloud Run logs for 403 errors. See "Fixing an existing
> deployment" below.
>
> **TODO:** Set up a budget-enforcer-error-watcher (Cloud Logging alert on
> 4xx/5xx from the budget-enforcer service) so auth failures are not silent.

## 10. Set up Cloud Scheduler for token usage monitoring

> **Run as: Admin SA**

The budget-enforcer includes a `/check-usage` endpoint that queries Cloud
Monitoring for real-time Vertex AI token usage and disables service account
keys if cost thresholds are exceeded. This fills the gap where Google
Billing notifications can be delayed 12-24 hours.

### Enable Cloud Scheduler API

```bash
gcloud services enable cloudscheduler.googleapis.com --project=$PROJECT_ID
```

### Create the scheduler job

Reuses the `pubsub-invoker` service account (already has `roles/run.invoker`).

```bash
export SERVICE_URL=$(gcloud run services describe budget-enforcer \
  --region=us-central1 --format="value(status.url)")

gcloud scheduler jobs create http check-vertex-usage \
  --project=$PROJECT_ID \
  --location=us-central1 \
  --schedule="*/5 * * * *" \
  --uri="${SERVICE_URL}/check-usage" \
  --http-method=GET \
  --oidc-service-account-email=pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com \
  --oidc-token-audience="${SERVICE_URL}"
```

### Adjust thresholds

Thresholds are set via Cloud Run environment variables:

- `USAGE_HOURLY_LIMIT` (default: $10.00) — max estimated cost in a 1-hour rolling window
- `USAGE_DAILY_LIMIT` (default: $50.00) — max estimated cost in a 24-hour rolling window

To change thresholds without redeploying:

```bash
gcloud run services update budget-enforcer \
  --region=us-central1 \
  --update-env-vars USAGE_HOURLY_LIMIT=5.00,USAGE_DAILY_LIMIT=25.00
```

### Test the scheduler manually

```bash
gcloud scheduler jobs run check-vertex-usage \
  --project=$PROJECT_ID \
  --location=us-central1
```

Then check logs:

```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=budget-enforcer AND textPayload:\"Usage check complete\"" --project=$PROJECT_ID --limit=5 --format="table(timestamp, textPayload)"
```

## 11. Verify the setup

> **Run as: Admin SA**

Send a test Pub/Sub message simulating a budget exceeded notification:

```bash
gcloud pubsub topics publish budget-alerts-01 \
  --project=$PROJECT_ID \
  --message='{"budgetAmount": 0.05, "costAmount": 0.06, "budgetDisplayName": "test"}'
```

> **Warning:** This test message has `costAmount >= budgetAmount`, so the
> budget-enforcer will actually call `disable_service_account_keys()` and
> disable any user-managed keys on the **API consumer SA**
> (`nmdc-llm-service-account`). Be prepared to re-enable them afterward
> (see "Re-enabling keys" section below).

Then check Cloud Run logs:

```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=budget-enforcer" --project=$PROJECT_ID --limit=10 --format="table(timestamp, textPayload, httpRequest.status)"
```

You should see a **200** response. If you see 403, the Pub/Sub OIDC
authentication is not configured correctly (see "Fixing an existing
deployment" below).

---

## Fixing an existing deployment

If you already deployed and have the 403 error, run these commands to fix
it without redeploying:

```bash
# Set variables (see step 1)
export PROJECT_ID=<your-gcp-project-id>
export KEY_FILE=<path-to-admin-sa-json-key>

# === RUN AS: Admin SA ===
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID

# Create the invoker service account
gcloud iam service-accounts create pubsub-invoker \
  --project=$PROJECT_ID \
  --display-name="Pub/Sub Cloud Run Invoker"

# === SWITCH TO: Personal account (smoxon) ===
# (Editor cannot do run.services.setIamPolicy)
gcloud auth login
gcloud config set project $PROJECT_ID

gcloud run services add-iam-policy-binding budget-enforcer \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --member="serviceAccount:pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# === SWITCH BACK TO: Admin SA ===
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID

# Update the existing subscription to use OIDC auth
# (--push-endpoint is required alongside --push-auth-service-account)
export SERVICE_URL=$(gcloud run services describe budget-enforcer --region us-central1 --format="value(status.url)")

gcloud pubsub subscriptions update budget-alerts-sub-01 \
  --push-endpoint=$SERVICE_URL \
  --push-auth-service-account=pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

---

## Recovery: Re-enabling keys and restoring the budget alert

When the budget-enforcer disables the consumer SA key, follow these steps
to bring everything back online. This covers: re-enabling the key, resetting
the budget so alerts fire again next cycle, restoring the Pub/Sub auth
binding (which can break after Cloud Run updates), and verifying end-to-end.

All commands below use the variables from step 1 (`$PROJECT_ID`,
`$KEY_FILE`, `$ADMIN_SA`, `$CONSUMER_SA`). Set them first if they are not
already in your shell.

### Step R1: Re-enable the consumer SA key

> **Run as: Admin SA** (it has Service Account Key Admin)

```bash
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID

# List keys to find the disabled one
gcloud iam service-accounts keys list \
  --iam-account=${CONSUMER_SA}@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID

# Re-enable it (replace KEY_ID with the ID from above)
gcloud iam service-accounts keys enable <KEY_ID> \
  --iam-account=${CONSUMER_SA}@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

Confirm the key is active:

```bash
gcloud iam service-accounts keys list \
  --iam-account=${CONSUMER_SA}@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

The `DISABLED` column should be empty for the re-enabled key.

### Step R2: Reset the budget for the next cycle

The budget has already fired its 100% notification this cycle. To ensure
it fires again next time spend crosses the threshold:

1. Go to **Billing > Budgets & alerts**
2. Click on your budget
3. Increase the **Target amount** to account for the new billing period
   (or keep the same amount — GCP resets monthly budgets automatically)
4. Verify **Services** is set to **All services** (not just Vertex AI)
5. Verify the **Pub/Sub topic** is still connected (`budget-alerts-01`)
6. Click **Save**

> **Note:** Saving the budget triggers an immediate re-evaluation. If
> current spend already exceeds the threshold, GCP will fire Pub/Sub
> notifications right away — which will disable the key you just
> re-enabled. Make sure current month spend is below the budget before
> saving, or temporarily disconnect the Pub/Sub topic while you save.

### Step R3: Restore the Pub/Sub IAM binding

`gcloud run services update` (and redeployments) can break the
`pubsub-invoker` SA's `run.invoker` binding. This is the most common
cause of silent 403 failures. Re-apply it every time you update Cloud Run.

> **Run as: Personal account (Owner)** — required for
> `run.services.setIamPolicy`

```bash
# === SWITCH TO: Personal account ===
gcloud auth login
gcloud config set project $PROJECT_ID

gcloud run services add-iam-policy-binding budget-enforcer \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --member="serviceAccount:pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

Confirm the binding is in place:

```bash
gcloud run services get-iam-policy budget-enforcer \
  --project=$PROJECT_ID \
  --region=us-central1
```

You should see `pubsub-invoker` listed under `roles/run.invoker`.

### Step R4: Verify end-to-end with a test message

> **Run as: Admin SA**

```bash
# === SWITCH BACK TO: Admin SA ===
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID

# Send a test message that will trigger key disabling
gcloud pubsub topics publish budget-alerts-01 \
  --project=$PROJECT_ID \
  --message='{"budgetAmount": 0.01, "costAmount": 0.02, "budgetDisplayName": "recovery-test"}'
```

Then check three things:

**a) Cloud Run received the message and returned 200:**

```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=budget-enforcer" \
  --project=$PROJECT_ID --limit=5 \
  --format="table(timestamp, textPayload, httpRequest.status)"
```

Look for a 200 with text containing "Budget exceeded." If you see 403,
the IAM binding from step R3 is not working — re-run it as your personal
account.

**b) Consumer SA key was disabled:**

```bash
gcloud iam service-accounts keys list \
  --iam-account=${CONSUMER_SA}@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

The USER_MANAGED key should show `DISABLED: True`.

**c) Re-enable the key again** (since this was just a test):

```bash
gcloud iam service-accounts keys enable <KEY_ID> \
  --iam-account=${CONSUMER_SA}@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID
```

If all three checks pass, the budget-enforcer is fully operational.

---

### Emergency recovery — admin SA key was disabled

If `SERVICE_ACCOUNT_EMAIL` was accidentally set to the admin SA and its
keys got disabled, you cannot use the admin SA to fix it. You must use
your personal account (Owner).

```bash
# === SWITCH TO: Personal account ===
gcloud auth login
gcloud config set project $PROJECT_ID

# List admin SA keys
gcloud iam service-accounts keys list \
  --iam-account=${ADMIN_SA}@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID

# Re-enable the admin SA key
gcloud iam service-accounts keys enable <KEY_ID> \
  --iam-account=${ADMIN_SA}@${PROJECT_ID}.iam.gserviceaccount.com \
  --project=$PROJECT_ID

# Then fix the deployment to target the consumer SA instead
gcloud run services update budget-enforcer \
  --set-env-vars SERVICE_ACCOUNT_EMAIL=${CONSUMER_SA}@${PROJECT_ID}.iam.gserviceaccount.com,GCP_PROJECT_ID=${PROJECT_ID},USAGE_HOURLY_LIMIT=10.00,USAGE_DAILY_LIMIT=50.00 \
  --region=us-central1 \
  --project=$PROJECT_ID
```

After fixing the env var, follow steps R3 and R4 above to restore the
IAM binding and verify end-to-end.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| **Admin SA key disabled after budget alert** | `SERVICE_ACCOUNT_EMAIL` pointing at admin SA instead of consumer SA | See "Emergency recovery" section; update env var to `$CONSUMER_SA` |
| 403 in Cloud Run logs | Pub/Sub subscription missing OIDC auth | See "Fixing an existing deployment" |
| 500 in Cloud Run logs | Code error in budget-enforcer | Check full logs with `gcloud logging read` |
| Got 75%/95% emails but no 100% email, keys not disabled | Pub/Sub OIDC auth broken — 100% notification failed silently | Check Cloud Run logs for 403; re-run IAM binding from step 7 |
| Budget email received but keys not disabled | Pub/Sub not reaching Cloud Run | Check subscription exists and points to correct endpoint |
| Budget shows low spend but real costs are high | Budget scoped to "Vertex AI" only — Claude charges billed under a different service | Edit budget scope to **all services** (see step 9) |
| `PERMISSION_DENIED` on `add-iam-policy-binding` | Running as admin SA instead of personal account | Switch to personal account with `gcloud auth login` |
| `--set-env-vars` parse error | Line break in deploy command | Ensure the entire deploy command is on one line |
| Uploading too many files on deploy | Running `--source .` from wrong directory | `cd` into the budget-enforcer repo first |
| `/check-usage` returns `hourly_error` | Cloud Run SA lacks monitoring.viewer role | Grant `roles/monitoring.viewer` to the Cloud Run service account |
| Scheduler job shows 403 | OIDC auth not configured or invoker SA lacks run.invoker | Re-run IAM binding command from step 7 |
| Cost shows $0 but usage exists | Model not in PRICING dict | Add the model to PRICING in `main.py` and redeploy |
