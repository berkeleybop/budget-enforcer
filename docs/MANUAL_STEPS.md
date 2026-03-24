# Manual Steps

These steps cannot be automated with Terraform and must be done by hand.
Everything else is handled by `terraform apply` — see `terraform/`.

## 1. Get a GCP project from Science IT

- Contact Science IT (Tim Fong <tyfong@lbl.gov>) to create a new GCP
  project tied to your project code (ask Chris and Nomi for the code).
- Science IT will attach the project to your LBL account in GCP.
- Note the **project ID** (not the display name) — you'll need it for
  `terraform.tfvars`.

## 2. Enable Anthropic models in Model Garden

This is UI-only — there is no API or Terraform resource for it.

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Select your project
3. Search for "model garden"
4. For each model you need (Haiku, Sonnet, Opus):
   - Search for the model
   - Click "Enable"
   - If prompted, enable the Vertex AI API
   - Fill out the purchase questionnaire:
     - Business name: Lawrence Berkeley National Laboratory
     - Business website: https://lbl.gov
     - Contact email: yourusername@lbl.gov
     - Headquartered: United States of America
     - Industry: Government
     - Intended users: Internal employees and external users
     - Use cases: Scientific research
     - Additional AUP requirements: No
   - Select **us-east5** for region
5. Note the exact model IDs (e.g. `claude-sonnet-4-5@20250929`)

## 3. Create the consumer SA JSON key

After `terraform apply` creates the service accounts, you need to
manually create a JSON key for the consumer SA. This is intentionally
kept out of Terraform to avoid storing secrets in Terraform state.

```bash
# Get the consumer SA email from Terraform output
CONSUMER_EMAIL=$(cd terraform && terraform output -raw consumer_sa_email)

# Create a JSON key
gcloud iam service-accounts keys create consumer-key.json \
  --iam-account="$CONSUMER_EMAIL"
```

> **This JSON key is what you distribute to developers.** Anyone with
> this key can make Vertex AI API calls (and incur costs). Distribute
> with care.

> **This key is gitignored** (*.json in .gitignore). Never commit it.

## 4. Build and push the container image

Before the first `terraform apply`, build and push the container:

```bash
cd /path/to/budget-enforcer
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/budget-enforcer .
```

Then set `container_image` in your `terraform.tfvars`.

## 5. Authenticate for Terraform

Terraform can run as your **personal Google account** (Owner role) or
a service account with sufficient permissions (Editor + Project IAM
Admin + Service Account Key Admin). See `CLAUDE.md` for details.

```bash
# Personal account:
gcloud auth application-default login

# Or service account:
gcloud auth activate-service-account --key-file=PATH_TO_KEY.json
export GOOGLE_APPLICATION_CREDENTIALS=PATH_TO_KEY.json
```

## 6. Verify billing admins and notification recipients

Budget alert emails (50%/75%/90%/95% thresholds) are sent by GCP to
billing admins and project owners. Verify that the right people receive
these early-warning emails — they're your first signal before enforcement.

### Check who receives billing alerts

1. Go to **Cloud Console > Billing > Account Management**
2. Look at the **Permissions** panel on the right
3. Anyone with `Billing Account Administrator` or `Billing Account User`
   will receive budget alert emails
4. Project owners also receive them

Or via CLI (requires billing account access):

```bash
gcloud billing accounts get-iam-policy BILLING_ACCOUNT_ID
```

### Add a billing admin

If your team lead or PI should receive budget alerts:

1. Go to **Cloud Console > Billing > Account Management**
2. Click **Add Principal** in the Permissions panel
3. Enter their email
4. Assign role: `Billing Account User` (for alerts only) or
   `Billing Account Administrator` (for full billing management)

> **Note:** This controls who gets GCP's built-in email alerts for the
> sub-100% thresholds (50%, 75%, 90%, 95%). The budget-enforcer's own
> notifications (Slack) are separate — see step 7 below.

## 7. Set up Slack notifications (optional)

When the budget-enforcer disables keys, it can post a notification to
a Slack channel so your team is immediately aware. This is optional —
if not configured, the enforcer still works but only logs to Cloud Run.

### Create a Slack webhook

1. Go to your Slack workspace's app management:
   `https://YOUR_WORKSPACE.slack.com/apps`
2. Search for **Incoming Webhooks** and add it
3. Choose the channel where alerts should go (e.g. `#infrastructure`
   or `#budget-alerts`)
4. Copy the webhook URL (looks like:
   `https://hooks.slack.com/services/T.../B.../xxx`)

### Configure the webhook

Add it to your `terraform.tfvars`:

```hcl
slack_webhook_url = "https://hooks.slack.com/services/T.../B.../xxx"
```

Then apply:

```bash
cd terraform/
terraform apply
```

> **The webhook URL is sensitive** — it's marked `sensitive` in Terraform
> so it won't appear in plan output, and `terraform.tfvars` is gitignored.
> Never commit it.

### What the notification looks like

When keys are disabled, the Slack message includes:
- Which project was affected
- Which service account's keys were disabled
- Why (billing alert or flux estimate with dollar amounts)
- A pointer to the recovery docs
