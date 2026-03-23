# budget-enforcer

Automatic budget enforcement for Vertex AI on GCP. When spending exceeds
a configured threshold, this service disables the API consumer service
account's keys, immediately halting Vertex AI API calls.

## Budget limit

The monthly spending limit is set via the `monthly_budget_amount` variable
in `terraform/terraform.tfvars` (default: **$100**). When actual spend
reaches this amount, the service automatically disables the consumer
service account's keys, immediately stopping all Vertex AI API calls.

To change it, edit `terraform.tfvars` and re-apply:

```bash
cd terraform/
# Edit terraform.tfvars: monthly_budget_amount = 200
terraform apply
```

See `terraform/variables.tf` for related settings including real-time
usage limits (`usage_hourly_limit`, `usage_daily_limit`) that can catch
runaway spend faster than GCP Billing notifications (which lag 12-24h).

## How it works

1. GCP Billing detects spend has crossed the budget threshold
2. Billing sends a notification to a Pub/Sub topic
3. Pub/Sub pushes the message to a Cloud Run service (this repo)
4. The service disables the consumer service account's JSON keys via IAM API
5. Applications using those keys (e.g. Claude Code) can no longer make API calls
6. An admin re-enables the keys once the budget situation is addressed

## Architecture

Three service accounts, each with a distinct role:

| Identity | Role | Purpose |
|---|---|---|
| **Personal (Owner)** | Owner | IAM policy bindings on Cloud Run |
| **Admin SA** | Editor, SA Key Admin, etc. | Runs this Cloud Run service; disables consumer keys |
| **Consumer SA** | Vertex AI User | Used by applications; gets its keys disabled |

## Setup

### Quick start (new project)

1. Complete the manual prerequisites in [`docs/MANUAL_STEPS.md`](docs/MANUAL_STEPS.md)
2. Copy and fill in your variables:
   ```bash
   cd terraform/
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars with your project details
   ```
3. Build and push the container:
   ```bash
   gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/budget-enforcer .
   ```
4. Deploy:
   ```bash
   terraform init
   terraform plan    # Review what will be created
   terraform apply
   ```
   > **Note:** State is stored locally on your machine (no shared backend).
   > See `terraform/backend.tf` for details on state management and how
   > to take over an existing deployment.
5. Create the consumer SA JSON key (see `docs/MANUAL_STEPS.md` step 3)
6. Distribute the key and Claude Code env config to developers:
   ```bash
   terraform output claude_code_env_snippet
   ```

### Existing deployment

See [`docs/SOP.md`](docs/SOP.md) for manual deployment steps, recovery
procedures, and troubleshooting.

## Repository structure

```
main.py                         # Cloud Run service (Flask)
Dockerfile                      # Container definition
requirements.txt                # Python dependencies
terraform/
  main.tf                       # All GCP resources
  variables.tf                  # Input variables (self-documenting)
  outputs.tf                    # Operational outputs + Claude Code config
  versions.tf                   # Provider version constraints
  backend.tf                    # Remote state config (optional)
  terraform.tfvars.example      # Template for your variables
docs/
  SOP.md                        # Full operational runbook
  MANUAL_STEPS.md               # Steps that cannot be automated
```

## Recovery

When keys are disabled by the budget-enforcer:

```bash
# Re-enable the consumer SA key
gcloud iam service-accounts keys enable KEY_ID \
  --iam-account=CONSUMER_SA_EMAIL
```

See [`docs/SOP.md`](docs/SOP.md) for full recovery procedures including
budget reset, IAM binding restoration, and end-to-end verification.

## License

BSD 3-Clause. See [LICENSE](LICENSE).
