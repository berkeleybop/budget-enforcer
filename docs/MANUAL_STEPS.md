# Manual Steps

These steps require a browser or human judgment and cannot be fully
automated. Claude Code will walk you through them and tell you when
each is needed.

- **Steps 1, 2, 6, 7** — You do these in a browser (Cloud Console,
  Slack). Claude will tell you what to click and what values to enter.
- **Steps 3, 4, 5** — Claude runs these for you via `gcloud` and
  `terraform` commands. They're documented here so you can understand
  what's happening or run them manually if needed.

Everything beyond these steps is handled by `terraform apply` —
see `terraform/`.

## Prerequisites: who you need to be

Before starting, make sure you understand which identity to use. There
are two options, and **you should pick one and use it for all steps
below** unless a step explicitly says otherwise.

### Option A: Personal Google account (recommended for first-time setup)

Log in with your LBL email (e.g. `yourname@lbl.gov`) via the browser
for Cloud Console steps, and via CLI for command-line steps:

```bash
gcloud auth login                        # For gcloud commands
gcloud auth application-default login    # For Terraform
gcloud config set project YOUR_PROJECT_ID
```

**Required role:** Owner on the GCP project. If Science IT created the
project for you, you should already be Owner. Verify with:

```bash
gcloud projects get-iam-policy YOUR_PROJECT_ID \
  --format="table(bindings.role,bindings.members)" \
  --flatten="bindings[].members" \
  --filter="bindings.members:yourname@lbl.gov"
```

You should see `roles/owner` in the output.

### Option B: Service account with elevated permissions

If you have an existing admin service account with a JSON key:

```bash
gcloud auth activate-service-account --key-file=PATH_TO_KEY.json
export GOOGLE_APPLICATION_CREDENTIALS=PATH_TO_KEY.json
gcloud config set project YOUR_PROJECT_ID
```

**Required roles on the service account:**
- `roles/editor`
- `roles/resourcemanager.projectIamAdmin`
- `roles/iam.serviceAccountKeyAdmin`
- `roles/run.admin`

> **Common pitfall:** The `roles/editor` role does NOT include
> `run.services.setIamPolicy`. If you get a 403 error about this
> permission during `terraform apply`, your service account also needs
> `roles/resourcemanager.projectIamAdmin` (which grants it indirectly).
> Alternatively, switch to your personal Owner account (Option A).

### Which identity does what?

| Step | Who | Why |
|---|---|---|
| 1. Get GCP project | You (browser) | Request to Science IT |
| 2. Enable models | You (browser) | Model Garden is UI-only |
| 3. Consumer SA key | Same as step 5 | Needs `iam.serviceAccountKeyAdmin` |
| 4. Build container | Same as step 5 | Needs `cloudbuild.builds.create` |
| 5. Terraform apply | Owner or admin SA | Creates all resources |
| 6. Billing admins | You (browser) | Billing account access |
| 7. Slack webhook | You (browser) | Slack workspace access |

**You do NOT need to switch identities between steps.** If you
authenticate as Owner (Option A), that single identity has all the
permissions needed for every step. If you use a service account
(Option B), it works for steps 3-5 but you'll still need your browser
(as yourself) for steps 1, 2, 6, and 7.

---

## 1. Get a GCP project from Science IT

> **Run as:** yourself (browser)

- Contact your institution's IT / cloud admin group to provision a new
  GCP project under your organization's billing account. You'll
  typically need a project code or cost center to charge against —
  ask your group lead or grant administrator if you don't have one.
- They will attach the project to your account in GCP.
- Note the **project ID** (not the display name) — you'll need it for
  `terraform.tfvars`.

## 2. Enable Anthropic models in Model Garden

> **Run as:** yourself (browser, logged into Cloud Console)

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

## 3. Build and push the container image

> **Run as:** Owner (Option A) or admin SA (Option B)

Before the first `terraform apply`, build and push the container:

```bash
cd /path/to/budget-enforcer
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/budget-enforcer .
```

Then set `container_image` in your `terraform.tfvars`.

## 4. Run Terraform

> **Run as:** Same identity as step 3. Do not switch.

```bash
cd terraform/
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your project details

terraform init
terraform plan    # Review what will be created
terraform apply
```

If you see a 403 error about `run.services.setIamPolicy`, your service
account is missing `roles/resourcemanager.projectIamAdmin`. Either add
that role or switch to your personal Owner account (see Prerequisites).

## 5. Create the consumer SA JSON key

> **Run as:** Same identity as steps 3-4. Do not switch.

After `terraform apply` creates the service accounts, create a JSON
key for the consumer SA. This is intentionally kept out of Terraform
to avoid storing secrets in Terraform state.

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

## 6. Verify billing admins and notification recipients

> **Run as:** yourself (browser, Cloud Console)

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

> **Run as:** yourself (browser, Slack)

When the budget-enforcer disables keys, it can post a notification to
a Slack channel so your team is immediately aware. This is optional —
if not configured, the enforcer still works but only logs to Cloud Run.

### Create a Slack app with incoming webhook

> **Do not use the legacy "Incoming Webhooks" custom integration** — it
> is deprecated by Slack and will be removed. Use a Slack app instead.

1. Go to `https://api.slack.com/apps`
2. Click **Create New App** > **From scratch**
3. Name it (e.g. `Budget Enforcer`) and select your workspace
4. Click **Create App**
5. In the app settings, click **Incoming Webhooks** in the left sidebar
6. Toggle **Activate Incoming Webhooks** to **On**
7. Click **Add New Webhook to Workspace**
8. Select the channel where alerts should go (e.g. `#infrastructure`
   or `#budget-alerts`)
9. Click **Allow**
10. Copy the **Webhook URL** (looks like:
    `https://hooks.slack.com/services/T.../B.../xxx`)

### Configure the webhook

Add it to your `terraform.tfvars`:

```hcl
slack_webhook_url = "https://hooks.slack.com/services/T.../B.../xxx"
```

Then apply (same identity as step 4):

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
