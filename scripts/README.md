# scripts/

Local operator tools for inspecting Vertex AI spend on a deployment of
this budget-enforcer. They query Cloud Monitoring directly and apply the
same pricing logic as `main.py`, so their output matches the service's
`/check-usage` endpoint but can span arbitrary time windows.

## What's here

- **`spend_report.py`** — CLI. Per-model cost breakdown for any window.
- **`plot_spend.py`** — CLI. Stacked bar chart (PNG) of per-model spend
  over time, with optional workshop-window annotation.
- **`_pricing.py`** — shared PRICING / cache / regional-premium logic.
  Mirrors `main.py`; update both together when prices change.
- **`_monitoring.py`** — shared Cloud Monitoring paginating query helper.

## Auth

Uses the active `gcloud` identity. Any principal with
`roles/monitoring.viewer` on the project can run these — no service
account keys needed.

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud auth login    # or activate-service-account, whichever applies
```

## Usage — spend_report.py

```bash
# Last 48 hours (matches /check-usage)
scripts/spend_report.py --project YOUR_PROJECT_ID --hours 48

# Last 7 days
scripts/spend_report.py --project YOUR_PROJECT_ID --hours 168

# Arbitrary window (workshop day retrospective)
scripts/spend_report.py --project YOUR_PROJECT_ID \
    --start 2026-04-14T13:00Z --end 2026-04-14T21:00Z
```

## Usage — plot_spend.py

Requires `matplotlib`. If the system Python's matplotlib is broken by a
numpy ABI mismatch (a `numpy.core.multiarray failed to import` error on
import), use an isolated venv:

```bash
uv venv --python 3.10 /tmp/plotenv/.venv
uv pip install --python /tmp/plotenv/.venv/bin/python 'numpy<2' matplotlib
alias py=/tmp/plotenv/.venv/bin/python
```

Then:

```bash
# Workshop day retrospective, 15-minute buckets, with workshop window shaded
py scripts/plot_spend.py --project YOUR_PROJECT_ID \
    --start 2026-04-14T13:00Z --end 2026-04-14T21:00Z \
    --bucket-minutes 15 \
    --workshop-start 2026-04-14T14:00Z \
    --workshop-end 2026-04-14T18:00Z \
    --output /tmp/workshop_spend.png

# Last 24h at 30-minute granularity
py scripts/plot_spend.py --project YOUR_PROJECT_ID --hours 24 \
    --output /tmp/spend_24h.png
```

All times on the command line are UTC (ISO 8601 with `Z`). The chart
renders x-axis labels in a display timezone; `--tz-offset-hours -7`
(default) and `--tz-label PT` match BBOP's workshop cadence — adjust
for your own location.

## Keeping pricing in sync

`_pricing.py` duplicates the PRICING / CACHE_MULTIPLIERS / REGIONAL_PREMIUM
tables from `main.py`. This is intentional: the scripts should run without
installing the service's Flask / google-cloud dependencies. When model
prices change (see CLAUDE.md "Pricing table maintenance"), update both
files together.
