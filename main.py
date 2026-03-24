"""
Budget Enforcer — Cloud Run service for Vertex AI spend control.

This service has two enforcement mechanisms:

1. BILLING-BASED (POST /): Triggered by GCP Billing Budget via Pub/Sub.
   Accurate but delayed — GCP billing data lags 12-24 hours. This is the
   "ground truth" enforcement that fires when real billing data confirms
   the budget is exceeded.

2. FLUX-BASED (GET /check-usage): Triggered every 5 minutes by Cloud
   Scheduler. Estimates spend in near-real-time using actual token counts
   from Cloud Monitoring, with per-model pricing and cache awareness.

   The estimator queries the publisher/online_serving/token_count metric,
   which provides per-model, per-token-type counts (input, output,
   cache_read, cache_write). It then applies the correct price for each
   model and token type, including cache discounts.

   If token data is unavailable (metric path changes, new model not yet
   reporting), it falls back to counting API calls (response_count) and
   applying a conservative per-call cost estimate.

   An ENFORCEMENT_TOLERANCE scalar (default 1.0) controls how aggressively
   the estimator triggers. Set <1.0 to enforce early (prefer interruption
   over overspend), >1.0 to allow some overshoot (reduce false positives).

Both mechanisms disable the same consumer SA keys via the IAM API.
"""
import base64
import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from google.cloud import iam_admin_v1
from google.cloud import monitoring_v3
from flask import Flask, request


app = Flask(__name__)


# ---------------------------------------------------------------------------
# Pricing tables
#
# Per-model pricing in USD per 1 million tokens.
# Source: https://cloud.google.com/vertex-ai/generative-ai/pricing
#         https://platform.claude.com/docs/en/about-claude/pricing
#
# When a model is not in this table, the estimator falls back to the
# FALLBACK_PRICING entry. Set that conservatively (use the most expensive
# model you might encounter) so unknown models don't slip past the budget.
# ---------------------------------------------------------------------------

PRICING = {
    # Anthropic — Claude (prices per 1M tokens, USD)
    # Source: https://docs.anthropic.com/en/docs/about-claude/models
    "claude-opus-4-6":       {"input": 5.00,  "output": 25.00},
    "claude-opus-4-5":       {"input": 5.00,  "output": 25.00},
    "claude-opus-4-1":       {"input": 15.00, "output": 75.00},
    "claude-opus-4":         {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":     {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-5":     {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4":       {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":      {"input": 1.00,  "output": 5.00},
    "claude-haiku-3-5":      {"input": 0.80,  "output": 4.00},
    # Google — Gemini (prices per 1M tokens, USD)
    # Source: https://cloud.google.com/vertex-ai/generative-ai/pricing
    "gemini-2.0-flash":          {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash-001":      {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash-lite":     {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash-lite-001": {"input": 0.075, "output": 0.30},
    "gemini-2.5-flash":          {"input": 0.30,  "output": 2.50},
    "gemini-2.5-flash-lite":     {"input": 0.10,  "output": 0.40},
    "gemini-2.5-pro":            {"input": 1.25,  "output": 10.00},
    "gemini-3.0-flash":          {"input": 0.50,  "output": 3.00},
    "gemini-3.0-pro":            {"input": 2.00,  "output": 12.00},
}

# Used for models not in the PRICING table. Set to the most expensive
# model you expect to encounter so unknown models are overestimated
# rather than underestimated.
FALLBACK_PRICING = {"input": 15.00, "output": 75.00}

# Vertex AI regional endpoints (e.g. us-east5) may charge a 10% premium
# over global endpoint pricing. The PRICING table above uses base (global)
# prices. This multiplier is applied to all cost calculations to account
# for the regional premium. Set to 1.0 if using the global endpoint.
REGIONAL_PREMIUM = float(os.environ.get("REGIONAL_PREMIUM", "1.10"))

# Cache pricing multipliers (relative to the model's base input price).
# Cache reads are much cheaper than standard input. Cache writes cost
# slightly more. Claude Code uses caching heavily, so these multipliers
# significantly affect cost estimates.
#
# Source: https://platform.claude.com/docs/en/about-claude/pricing
CACHE_MULTIPLIERS = {
    "input":                1.0,    # standard input
    "cache_read_input":     0.1,    # cache hits: 90% cheaper
    "cache_write_input":    1.25,   # 5-min TTL cache write (default)
    "cache_write_5m_input": 1.25,   # 5-min TTL cache write (explicit)
    "cache_write_1h_input": 2.0,    # 1-hour TTL cache write
}

# Token types that should be priced at the output rate
OUTPUT_TYPES = {"output"}


# ---------------------------------------------------------------------------
# Configuration (from environment variables set by Terraform)
# ---------------------------------------------------------------------------

SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")

# How far back to look for API usage (hours). Should exceed the maximum
# billing data lag (~24-48h) so there's no blind spot between when calls
# are made and when billing catches up.
FLUX_WINDOW_HOURS = float(os.environ.get("FLUX_WINDOW_HOURS", "48"))

# Budget threshold for the flux estimator (USD). Should match
# monthly_budget_amount in Terraform. The billing-based enforcement
# (POST /) gets its budget from the Pub/Sub message directly.
FLUX_BUDGET = float(os.environ.get("FLUX_BUDGET", "0"))

# Scalar on the budget threshold:
#   0.8 = enforce at 80% of budget (conservative, prefer early cutoff)
#   1.0 = enforce at exactly the budget (default)
#   1.2 = allow 20% overspend before enforcing (tolerant)
ENFORCEMENT_TOLERANCE = float(os.environ.get("ENFORCEMENT_TOLERANCE", "1.0"))

# Fallback: upper-bound cost per API call (USD). Used only when the token
# count metric returns no data. Conservative default assumes Opus pricing.
COST_PER_CALL_FALLBACK = float(os.environ.get("COST_PER_CALL_FALLBACK", "0.30"))


# ---------------------------------------------------------------------------
# POST / — Billing-based enforcement (Pub/Sub from GCP Billing Budget)
# ---------------------------------------------------------------------------

@app.route("/", methods=["POST"])
def handle_budget_alert():
    """Handle budget alert from Pub/Sub.

    GCP Billing evaluates budget thresholds periodically. When the 100%
    threshold is crossed, it publishes a message to the Pub/Sub topic.
    The message contains the actual cost and budget amount.

    This is the "ground truth" enforcement — accurate, but delayed by
    12-24 hours due to billing data lag.
    """
    envelope = request.get_json()
    if not envelope:
        return "No Pub/Sub message received", 400

    if "message" not in envelope:
        return "Invalid Pub/Sub message format", 400

    pubsub_message = envelope["message"]

    if "data" in pubsub_message:
        data = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        budget_notification = json.loads(data)

        cost_amount = budget_notification.get("costAmount", 0)
        budget_amount = budget_notification.get("budgetAmount", 0)

        if cost_amount >= budget_amount:
            disable_service_account_keys()
            return (
                f"Budget exceeded (${cost_amount} >= ${budget_amount}). "
                f"Service account keys disabled.",
                200,
            )

    return "Budget alert received but threshold not met", 200


# ---------------------------------------------------------------------------
# GET /check-usage — Flux-based enforcement (Cloud Scheduler, every 5 min)
# ---------------------------------------------------------------------------

@app.route("/check-usage", methods=["GET"])
def check_usage():
    """Estimate current spend and enforce if it exceeds the budget.

    This endpoint bridges the 12-24h gap in GCP billing data by computing
    spend from Cloud Monitoring metrics. It runs every 5 minutes via Cloud
    Scheduler.

    Primary estimator: queries publisher/online_serving/token_count for
    actual per-model, per-token-type counts. This works for both Anthropic
    (Claude) and Google (Gemini) models and accounts for cache pricing.

    Fallback estimator: if the token metric returns no data, falls back to
    counting API calls (response_count) and multiplying by a conservative
    per-call cost estimate.
    """
    if FLUX_BUDGET <= 0:
        return json.dumps({
            "status": "skipped",
            "reason": "FLUX_BUDGET not set or zero",
        }), 200, {"Content-Type": "application/json"}

    threshold = FLUX_BUDGET * ENFORCEMENT_TOLERANCE

    # Try token-based estimation first (precise, per-model pricing)
    result = _estimate_spend_from_tokens()

    # If token metrics returned nothing, fall back to call counting
    if result.get("total_tokens", 0) == 0 and result.get("estimated_spend", 0) == 0:
        fallback = _estimate_spend_from_call_count()
        if fallback.get("total_calls", 0) > 0:
            result = fallback
            print(
                f"Token metrics returned no data. "
                f"Fell back to call_count estimator."
            )

    estimated_spend = result["estimated_spend"]
    result["threshold"] = threshold
    result["flux_budget"] = FLUX_BUDGET
    result["enforcement_tolerance"] = ENFORCEMENT_TOLERANCE

    if estimated_spend >= threshold:
        disable_service_account_keys()
        result["action"] = "keys_disabled"
        print(
            f"Flux estimate ${estimated_spend:.2f} >= threshold "
            f"${threshold:.2f} (budget ${FLUX_BUDGET:.2f} x "
            f"{ENFORCEMENT_TOLERANCE}). Keys disabled."
        )
    else:
        result["action"] = "none"
        print(
            f"Flux estimate ${estimated_spend:.2f} < threshold "
            f"${threshold:.2f}. No action."
        )

    return json.dumps(result), 200, {"Content-Type": "application/json"}


def _estimate_spend_from_tokens():
    """Estimate spend using actual token counts with per-model pricing.

    Queries the publisher/online_serving/token_count metric from Cloud
    Monitoring. This metric provides:
    - resource.labels.model_user_id: the model name (e.g. "claude-opus-4-6")
    - metric.labels.type: the token type ("input", "output",
      "cache_read_input", "cache_write_input", etc.)

    Each model is priced according to the PRICING table. Cache reads are
    90% cheaper than standard input (CACHE_MULTIPLIERS). Unknown models
    use FALLBACK_PRICING (conservative, assumes most expensive model).

    This approach was adapted from:
    https://github.com/microbiomedata/nmdc-metadata-suggestor-ai-tool/blob/main/scripts/vertex_usage.py
    """
    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=FLUX_WINDOW_HOURS)

    interval = monitoring_v3.TimeInterval(
        start_time=window_start,
        end_time=now,
    )

    query_filter = (
        'metric.type = '
        '"aiplatform.googleapis.com/publisher/online_serving/token_count"'
    )

    # Aggregate token counts by model and token type
    # { "claude-opus-4-6": { "input": 12345, "output": 6789, ... }, ... }
    totals = defaultdict(lambda: defaultdict(int))
    total_tokens = 0

    try:
        results = client.list_time_series(
            request={
                "name": f"projects/{PROJECT_ID}",
                "filter": query_filter,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
        for ts in results:
            model = ts.resource.labels.get("model_user_id", "unknown")
            token_type = ts.metric.labels.get("type", "unknown")
            for point in ts.points:
                count = point.value.int64_value
                totals[model][token_type] += count
                total_tokens += count
    except Exception as e:
        print(f"Error querying token_count metric: {e}")
        return {
            "mode": "token",
            "error": str(e),
            "total_tokens": 0,
            "estimated_spend": 0,
        }

    # Compute cost per model, respecting cache multipliers
    model_costs = {}
    total_spend = 0.0

    for model, token_types in totals.items():
        prices = PRICING.get(model, FALLBACK_PRICING)
        model_cost = 0.0

        for token_type, count in token_types.items():
            if token_type in OUTPUT_TYPES:
                # Output tokens: flat output price
                cost = (count / 1_000_000) * prices["output"]
            elif token_type in CACHE_MULTIPLIERS:
                # Input-family tokens: base input price * cache multiplier
                multiplier = CACHE_MULTIPLIERS[token_type]
                cost = (count / 1_000_000) * prices["input"] * multiplier
            else:
                # Unknown token type: price at base input rate
                cost = (count / 1_000_000) * prices["input"]

            model_cost += cost

        # Apply regional endpoint premium (e.g. 10% for us-east5)
        model_cost *= REGIONAL_PREMIUM

        model_costs[model] = {
            "tokens": dict(token_types),
            "cost": round(model_cost, 4),
            "pricing_source": "known" if model in PRICING else "fallback",
        }
        total_spend += model_cost

    return {
        "mode": "token",
        "window_hours": FLUX_WINDOW_HOURS,
        "total_tokens": total_tokens,
        "models": model_costs,
        "estimated_spend": round(total_spend, 2),
    }


def _estimate_spend_from_call_count():
    """Fallback: estimate spend by counting API calls.

    Used only when the token_count metric returns no data. Counts
    successful API calls (response_count with status 200) and multiplies
    by COST_PER_CALL_FALLBACK — a conservative upper-bound that assumes
    every call is the most expensive model.

    This is less precise than token-based estimation but works even if
    the token metric path changes or a new model doesn't report tokens.
    """
    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=FLUX_WINDOW_HOURS)

    interval = monitoring_v3.TimeInterval(
        start_time=window_start,
        end_time=now,
    )

    query_filter = (
        'metric.type = '
        '"aiplatform.googleapis.com/prediction/online/response_count" '
        'AND metric.labels.response_code = "200"'
    )

    total_calls = 0
    try:
        results = client.list_time_series(
            request={
                "name": f"projects/{PROJECT_ID}",
                "filter": query_filter,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
        for ts in results:
            for point in ts.points:
                total_calls += point.value.int64_value
    except Exception as e:
        print(f"Error querying response_count metric: {e}")
        return {
            "mode": "call_count_fallback",
            "error": str(e),
            "estimated_spend": 0,
        }

    estimated_spend = total_calls * COST_PER_CALL_FALLBACK * REGIONAL_PREMIUM

    return {
        "mode": "call_count_fallback",
        "window_hours": FLUX_WINDOW_HOURS,
        "total_calls": total_calls,
        "cost_per_call": COST_PER_CALL_FALLBACK,
        "estimated_spend": round(estimated_spend, 2),
    }


# ---------------------------------------------------------------------------
# GET /status — Health check and current configuration
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET"])
def status():
    """Return current configuration and pricing table for debugging."""
    return json.dumps({
        "service_account_email": SERVICE_ACCOUNT_EMAIL,
        "project_id": PROJECT_ID,
        "flux_budget": FLUX_BUDGET,
        "enforcement_tolerance": ENFORCEMENT_TOLERANCE,
        "flux_window_hours": FLUX_WINDOW_HOURS,
        "cost_per_call_fallback": COST_PER_CALL_FALLBACK,
        "regional_premium": REGIONAL_PREMIUM,
        "known_models": list(PRICING.keys()),
    }), 200, {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Shared: disable consumer SA keys
# ---------------------------------------------------------------------------

def disable_service_account_keys():
    """Disable all user-managed keys for the configured service account.

    Only disables USER_MANAGED keys (the JSON key files distributed to
    developers). Google-managed system keys are left alone — they're
    internal to GCP and disabling them would break the service account
    itself.
    """
    client = iam_admin_v1.IAMClient()

    list_request = iam_admin_v1.ListServiceAccountKeysRequest(
        name=f"projects/{PROJECT_ID}/serviceAccounts/{SERVICE_ACCOUNT_EMAIL}"
    )

    keys = client.list_service_account_keys(request=list_request)

    for key in keys.keys:
        if key.key_type == iam_admin_v1.ListServiceAccountKeysRequest.KeyType.USER_MANAGED:
            disable_request = iam_admin_v1.DisableServiceAccountKeyRequest(
                name=key.name
            )
            client.disable_service_account_key(request=disable_request)
            print(f"Disabled key: {key.name}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
