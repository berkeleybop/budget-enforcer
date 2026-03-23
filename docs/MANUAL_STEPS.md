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

Terraform should run as your **personal Google account** (Owner role),
not as a service account. This gives it the permissions needed for
IAM policy bindings on Cloud Run.

```bash
gcloud auth application-default login
```
