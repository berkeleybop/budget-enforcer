# budget-enforcer

Automatic budget enforcement for Vertex AI on GCP. When spending exceeds
a configured threshold, this service disables the API consumer service
account's keys, immediately halting Vertex AI API calls.

## How we work with this repo

This system is designed to be operated via
[Claude Code](https://claude.com/claude-code). The typical workflow is:

1. **You** make decisions (budget amount, which models to enable, when
   to deploy)
2. **Claude** reads `CLAUDE.md` for project context, uses Terraform to
   make changes, and runs `gcloud` commands for operations and recovery
3. **The docs** exist so both you and Claude understand what's happening
   — you can verify Claude's work, and Claude can reason about the
   system

You can also operate everything manually — all commands are documented
in `docs/SOP.md`. But the intended path is collaborative: you describe
what you want, Claude executes it.

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

A flux estimator checks spend every 5 minutes using API call counts,
catching runaway spend much faster than GCP Billing (which lags
12-24 hours). See "How it works" below for details.

## How it works

This service runs on Cloud Run and has **two independent ways** to
detect overspend. Both do the same thing when triggered: disable the
consumer service account's JSON keys so applications can no longer
call Vertex AI.

### Why two mechanisms?

GCP only tells you what you've spent **12-24 hours after the fact**.
If your budget is $5 and you're burning $30/hour on Claude Opus, you
could spend $360-720 before GCP even notices. So we have:

1. **The billing path** — waits for GCP's official spend numbers (accurate
   but slow)
2. **The flux estimator** — counts your API calls in real time and
   *estimates* what they probably cost (fast but approximate)

Think of it like a gas gauge: the billing path is like checking your
credit card statement (accurate, delayed), while the flux estimator is
like watching the pump's dollar counter tick up (real-time, estimated).

### The billing path (accurate, 12-24h delay)

```
GCP Billing ──► Pub/Sub topic ──► This Cloud Run service ──► Disables keys
  "You spent $5"                    (POST /)
```

This is the original mechanism. GCP Billing periodically checks actual
spend against your budget. When it crosses 100%, it sends a message
through Pub/Sub to this service. Reliable, but the 12-24h lag means
overspend can be significant.

### The flux estimator (approximate, ~5 min delay)

```
Cloud Scheduler ──► This Cloud Run service ──► Cloud Monitoring
  (every 5 min)      (GET /check-usage)          "How many API calls
                                                   in the last 48h?"
                           │
                           ▼
                     estimated_spend = call_count × cost_per_call
                           │
                     if estimated_spend >= budget × tolerance:
                           │
                           ▼
                     Disable keys
```

Every 5 minutes, Cloud Scheduler pokes the `/check-usage` endpoint.
That endpoint asks Cloud Monitoring: "how many Vertex AI API calls
happened in the last 48 hours?" It then multiplies that count by a
conservative cost-per-call estimate to get an approximate dollar amount.

### Configuration

There's only one budget number to set: `monthly_budget_amount` in your
`terraform.tfvars`. Terraform automatically passes this to both
enforcement paths.

The flux estimator has a few extra knobs (all optional, defaults work):

| Setting | What it does | Default |
|---|---|---|
| `enforcement_tolerance` | How strict to be (0.8 = cut off early, 1.2 = allow some overshoot) | 1.0 |
| `flux_window_hours` | How far back to look at usage data | 48 |
| `cost_per_call_fallback` | Per-call cost if token metrics unavailable | $0.30 |

Per-model pricing (Opus vs Haiku vs Gemini, etc.) and prompt cache
discounts are built into `main.py`. A 10% regional premium is applied
by default for non-global endpoints like us-east5. The pricing table
should be checked periodically when new model versions are released —
see [`CLAUDE.md`](CLAUDE.md) for sources and what to check.

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
CLAUDE.md                       # Project guide for Claude Code and developers
terraform/
  main.tf                       # All GCP resources
  variables.tf                  # Input variables (self-documenting)
  outputs.tf                    # Operational outputs + Claude Code config
  versions.tf                   # Provider version constraints
  backend.tf                    # State management docs
  terraform.tfvars.example      # Template for your variables
docs/
  SOP.md                        # Full operational runbook
  MANUAL_STEPS.md               # Steps that cannot be automated
```

### CLAUDE.md

[`CLAUDE.md`](CLAUDE.md) is automatically loaded by
[Claude Code](https://claude.com/claude-code) for project context. It's
also useful as a human onboarding document — it covers:

- Key concepts and common pitfalls (three-identity model, billing lag)
- How the two enforcement mechanisms work
- Terraform workflow and state management
- Secrets policy
- Pricing table maintenance checklist (what to check and when)
- Common tasks as a quick-reference

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
