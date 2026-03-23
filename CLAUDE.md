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
  charges bill under a marketplace service category.
- **The Pub/Sub OIDC auth binding is fragile.** It can break after Cloud
  Run redeployments. Terraform manages it as a separate resource to
  prevent this, but be aware during manual operations.

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
- `project_id` — GCP project ID
- `billing_account_id` — GCP billing account ID
- `container_image` — Built container image URI

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

## Common tasks

- **Add a new GCP API**: Add to `local.required_apis` in `terraform/main.tf`
- **Change budget threshold**: Update `monthly_budget_amount` in `terraform.tfvars`
- **Change usage limits**: Update `usage_hourly_limit` / `usage_daily_limit` in `terraform.tfvars`
- **Update the Flask app**: Edit `main.py`, rebuild container, update `container_image`
- **Recovery after key disable**: See `docs/SOP.md` recovery section (R1-R4)

## Style and conventions

- Terraform files use the standard HashiCorp style (`terraform fmt`)
- Python follows the existing minimal style in `main.py`
- Comments explain *why*, not *what*
- Variable descriptions in `variables.tf` are the primary documentation
  for infrastructure configuration
