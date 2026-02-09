# Budget Enforcer SOP

## Overview

GCP Billing sends alert to Pub/Sub topic -> Cloud Run service receives
Pub/Sub message -> Service disables the service account JSON key via IAM
API -> App can no longer make Vertex AI API calls -> Admin is notified and
can investigate -> Keys can be re-enabled via `gcloud` CLI or Cloud Console
-> Service can resume once budget is addressed.

## Prerequisites

- `gcloud` CLI installed locally
- An "admin" service account already created in the GCP console with the
  following roles: **Service Account Key Admin**, **Service Usage Admin**,
  **Editor**, and **Project IAM Admin**
- The admin service account's JSON key file downloaded locally
- Access to a personal Google account with **Owner** role on the project
  (required for IAM policy binding on Cloud Run — the admin service account's
  Editor role does not include `run.services.setIamPolicy`)

---

## 1. Setup variables

```bash
export PROJECT_ID=nmdc-llm
export KEY_FILE=/Users/SMoxon/Desktop/nmdc-llm-1be4580146c4.json
export ADMIN_USER=nmdc-llm-admin-service
```

## 2. Authenticate and configure project

```bash
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID
```

## 3. Enable required APIs

```bash
gcloud services enable run.googleapis.com
gcloud services enable pubsub.googleapis.com
gcloud services enable iam.googleapis.com
gcloud services enable cloudbuild.googleapis.com
```

## 4. Create Pub/Sub topic

```bash
gcloud pubsub topics create budget-alerts-01 --project=$PROJECT_ID
```

Verify:

```bash
gcloud pubsub topics list --project=$PROJECT_ID
```

## 5. Deploy Cloud Run service

> **Important:** Run this command from the `budget-enforcer` repo directory.
> The `--source .` flag uploads the current directory — if run from `~` or
> another location, it will attempt to upload everything in that directory.

> **Important:** The `--set-env-vars` flag and its value must be on the same
> line with no line breaks.

```bash
cd /path/to/budget-enforcer

gcloud run deploy budget-enforcer --source . --platform managed --region us-central1 --no-allow-unauthenticated --set-env-vars SERVICE_ACCOUNT_EMAIL=${ADMIN_USER}@${PROJECT_ID}.iam.gserviceaccount.com,GCP_PROJECT_ID=${PROJECT_ID}
```

> **Note:** We use `--no-allow-unauthenticated` because only Pub/Sub should
> be able to invoke this service. Authentication is handled via the invoker
> service account created in the next step.

Capture the service URL:

```bash
export SERVICE_URL=$(gcloud run services describe budget-enforcer --region us-central1 --format="value(status.url)")
```

## 6. Create a Pub/Sub invoker service account

This service account is used by Pub/Sub to authenticate when pushing
messages to Cloud Run. This is separate from the admin service account.

```bash
gcloud iam service-accounts create pubsub-invoker \
  --project=$PROJECT_ID \
  --display-name="Pub/Sub Cloud Run Invoker"
```

## 7. Grant the invoker service account permission to call Cloud Run

> **Important:** This command requires the `run.services.setIamPolicy`
> permission, which the admin service account's Editor role does **not**
> include. You must run this as your personal Google account (with Owner
> role), then switch back to the admin service account afterward.

```bash
# Switch to personal account
gcloud auth login
gcloud config set project $PROJECT_ID

# Grant the binding
gcloud run services add-iam-policy-binding budget-enforcer \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --member="serviceAccount:pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# Switch back to admin service account
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID
```

## 8. Create Pub/Sub subscription with OIDC authentication

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
   - Services: Select "Vertex AI" only
   - Deselect all savings/discounts
4. Click **Next**
5. **Amount**:
   - Budget type: Specified amount
   - Target amount: e.g., `$0.05`
   - Include credits: No
6. **Actions**:
   - Threshold rules: 100% (remove all others)
   - Manage notifications -> **Connect a Pub/Sub topic**
   - Select: `budget-alerts-01`

> **Note:** Google Billing also sends its own email alerts when budget
> thresholds are reached (e.g., "95% of budget reached"). These emails come
> from Google directly and are **not** sent by the budget-enforcer service.
> The budget-enforcer only disables service account keys — it does not send
> notifications.

## 10. Verify the setup

Send a test Pub/Sub message simulating a budget exceeded notification:

```bash
gcloud pubsub topics publish budget-alerts-01 \
  --project=$PROJECT_ID \
  --message='{"budgetAmount": 0.05, "costAmount": 0.06, "budgetDisplayName": "test"}'
```

> **Warning:** This test message has `costAmount >= budgetAmount`, so the
> budget-enforcer will actually call `disable_service_account_keys()` and
> disable any user-managed keys on the admin service account. Be prepared to
> re-enable them afterward (see "Re-enabling keys" section below).

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
# Set variables
export PROJECT_ID=nmdc-llm
export KEY_FILE=/Users/SMoxon/Desktop/nmdc-llm-1be4580146c4.json

# Create the invoker service account (as admin)
gcloud auth activate-service-account --key-file=$KEY_FILE
gcloud config set project $PROJECT_ID

gcloud iam service-accounts create pubsub-invoker \
  --project=$PROJECT_ID \
  --display-name="Pub/Sub Cloud Run Invoker"

# Grant Cloud Run invoker permissions (requires personal/Owner account)
gcloud auth login
gcloud config set project $PROJECT_ID

gcloud run services add-iam-policy-binding budget-enforcer \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --member="serviceAccount:pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# Switch back to admin service account
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

## Re-enabling keys after budget is addressed

List keys to find disabled ones:

```bash
gcloud iam service-accounts keys list \
  --iam-account=${ADMIN_USER}@${PROJECT_ID}.iam.gserviceaccount.com
```

Re-enable a specific key:

```bash
gcloud iam service-accounts keys enable <KEY_ID> \
  --iam-account=${ADMIN_USER}@${PROJECT_ID}.iam.gserviceaccount.com
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 403 in Cloud Run logs | Pub/Sub subscription missing OIDC auth | See "Fixing an existing deployment" |
| 500 in Cloud Run logs | Code error in budget-enforcer | Check full logs with `gcloud logging read` |
| Budget email received but keys not disabled | Pub/Sub not reaching Cloud Run | Check subscription exists and points to correct endpoint |
| `PERMISSION_DENIED` on `add-iam-policy-binding` | Running as admin service account instead of Owner | Switch to personal account with `gcloud auth login` |
| `--set-env-vars` parse error | Line break in deploy command | Ensure the entire deploy command is on one line |
| Uploading too many files on deploy | Running `--source .` from wrong directory | `cd` into the budget-enforcer repo first |
