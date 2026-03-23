"""
Budget Enforcer — Cloud Run service for Vertex AI spend control.

This service has two enforcement mechanisms:

1. BILLING-BASED (POST /): Triggered by GCP Billing Budget via Pub/Sub.
   Accurate but delayed — GCP billing data lags 12-24 hours. This is the
   "ground truth" enforcement that fires when real billing data confirms
   the budget is exceeded.

2. FLUX-BASED (GET /check-usage): Triggered every 5 minutes by Cloud
   Scheduler. Estimates spend in near-real-time by combining:
   - Known billing cost (accurate but stale, from GCP Billing)
   - Estimated cost of recent API calls (approximate but current, from
     Cloud Monitoring response_count metrics)

   This fills the 12-24h billing lag gap. Without it, a user could burn
   through an entire budget before enforcement kicks in.

   The flux estimator works in two modes:
   - TOKEN mode: Uses actual token count metrics from Cloud Monitoring.
     Available for Google models (Gemini, PaLM) but NOT for Anthropic
     models (Claude). More accurate when available.
   - CALL_COUNT mode: Estimates cost per API call using a configurable
     upper-bound cost. Works for ALL models including Anthropic. Less
     precise but always available.

   An ENFORCEMENT_TOLERANCE scalar (default 1.0) controls how aggressively
   the estimator triggers. Set <1.0 to enforce early (prefer interruption
   over overspend), >1.0 to allow some overshoot (reduce false positives).

Both mechanisms disable the same consumer SA keys via the IAM API.
"""
import base64
import json
import os
from datetime import datetime, timezone, timedelta

from google.cloud import iam_admin_v1
from google.cloud import monitoring_v3
from flask import Flask, request


app = Flask(__name__)


# ---------------------------------------------------------------------------
# Configuration (from environment variables set by Terraform)
# ---------------------------------------------------------------------------

# Core settings
SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")

# Flux estimator settings
# Mode: "call_count" (works for all models) or "token" (Google models only)
FLUX_MODE = os.environ.get("FLUX_MODE", "call_count")
# How far back to look for unbilled API calls (hours)
FLUX_WINDOW_HOURS = float(os.environ.get("FLUX_WINDOW_HOURS", "48"))
# Upper-bound cost estimate per API call, by model tier (USD)
# Used only in call_count mode. Set conservatively — better to overestimate
# and enforce early than to underestimate and overshoot the budget.
COST_PER_CALL_EXPENSIVE = float(os.environ.get("COST_PER_CALL_EXPENSIVE", "0.30"))
COST_PER_CALL_CHEAP = float(os.environ.get("COST_PER_CALL_CHEAP", "0.01"))
# Budget threshold for the flux estimator (USD). This should match the
# monthly_budget_amount in Terraform. The billing-based enforcement (POST /)
# uses the budget amount from the Pub/Sub message, but the flux estimator
# needs its own copy since it doesn't receive billing data directly.
FLUX_BUDGET = float(os.environ.get("FLUX_BUDGET", "0"))
# Scalar on the budget threshold. Controls the tradeoff between early
# enforcement (false positives) and overspend (false negatives):
#   0.8 = enforce at 80% of budget (conservative)
#   1.0 = enforce at exactly the budget (default)
#   1.2 = allow 20% overspend before enforcing (tolerant)
ENFORCEMENT_TOLERANCE = float(os.environ.get("ENFORCEMENT_TOLERANCE", "1.0"))

# Legacy settings (from the original /check-usage implementation)
USAGE_HOURLY_LIMIT = float(os.environ.get("USAGE_HOURLY_LIMIT", "0"))
USAGE_DAILY_LIMIT = float(os.environ.get("USAGE_DAILY_LIMIT", "0"))


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

    This endpoint bridges the 12-24h gap in GCP billing data by estimating
    spend from Cloud Monitoring metrics. It runs every 5 minutes via Cloud
    Scheduler.

    The estimation works in two modes:

    TOKEN mode (FLUX_MODE="token"):
      Queries actual input/output token counts from Cloud Monitoring.
      Only works for Google-native models (Gemini, PaLM). Anthropic
      models on Vertex AI do NOT populate these metrics.

    CALL_COUNT mode (FLUX_MODE="call_count"):
      Queries the response_count metric (which works for ALL models
      including Anthropic) and multiplies by a configurable cost-per-call
      estimate. Less precise but universally available.

    The estimated spend is compared against:
      FLUX_BUDGET * ENFORCEMENT_TOLERANCE

    If exceeded, consumer SA keys are disabled immediately.
    """
    if FLUX_BUDGET <= 0:
        return json.dumps({
            "status": "skipped",
            "reason": "FLUX_BUDGET not set or zero",
        }), 200, {"Content-Type": "application/json"}

    threshold = FLUX_BUDGET * ENFORCEMENT_TOLERANCE

    if FLUX_MODE == "token":
        result = _estimate_spend_from_tokens()
    else:
        result = _estimate_spend_from_call_count()

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


def _estimate_spend_from_call_count():
    """Estimate spend by counting API calls and applying a per-call cost.

    This is the universal estimator — it works for all model providers
    (Anthropic, Google, etc.) because the response_count metric is always
    populated by Vertex AI.

    The cost-per-call values (COST_PER_CALL_EXPENSIVE, COST_PER_CALL_CHEAP)
    should be set to upper-bound estimates. It's better to overestimate and
    trigger enforcement slightly early than to underestimate and overshoot.

    We don't currently distinguish which deployment ID corresponds to which
    model. As a conservative default, all calls are priced at the expensive
    rate. If you know your deployment IDs, you could map them here.
    """
    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=FLUX_WINDOW_HOURS)

    interval = monitoring_v3.TimeInterval(
        start_time=window_start,
        end_time=now,
    )

    # Query response_count for successful (200) calls only.
    # Failed calls don't incur token costs.
    query_filter = (
        'metric.type = "aiplatform.googleapis.com/prediction/online/response_count" '
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
        print(f"Error querying Cloud Monitoring: {e}")
        return {
            "mode": "call_count",
            "error": str(e),
            "estimated_spend": 0,
        }

    # Conservative: price all calls at the expensive rate.
    # This overestimates if some calls are Haiku, but that's the safe
    # direction — we'd rather enforce early than late.
    estimated_spend = total_calls * COST_PER_CALL_EXPENSIVE

    return {
        "mode": "call_count",
        "window_hours": FLUX_WINDOW_HOURS,
        "total_calls": total_calls,
        "cost_per_call": COST_PER_CALL_EXPENSIVE,
        "estimated_spend": round(estimated_spend, 2),
    }


def _estimate_spend_from_tokens():
    """Estimate spend using actual token count metrics.

    This is the precise estimator — it uses input/output token counts
    reported by Cloud Monitoring. However, it ONLY works for Google-native
    models (Gemini, PaLM). Anthropic models on Vertex AI do not populate
    these metrics, so this will return zero for Claude.

    If you're using only Anthropic models, set FLUX_MODE="call_count".
    If you're using a mix, you may need to run both estimators (future work).
    """
    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=FLUX_WINDOW_HOURS)

    interval = monitoring_v3.TimeInterval(
        start_time=window_start,
        end_time=now,
    )

    input_tokens = 0
    output_tokens = 0

    for metric_type, counter in [
        ("aiplatform.googleapis.com/prediction/online/input_token_count", "input"),
        ("aiplatform.googleapis.com/prediction/online/output_token_count", "output"),
    ]:
        query_filter = f'metric.type = "{metric_type}"'
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
                    if counter == "input":
                        input_tokens += point.value.int64_value
                    else:
                        output_tokens += point.value.int64_value
        except Exception as e:
            print(f"Error querying {metric_type}: {e}")

    # Default pricing (Gemini 1.5 Pro as reference; adjust as needed)
    input_price_per_mtk = float(os.environ.get("TOKEN_INPUT_PRICE_PER_MTK", "3.50"))
    output_price_per_mtk = float(os.environ.get("TOKEN_OUTPUT_PRICE_PER_MTK", "10.50"))

    estimated_spend = (
        (input_tokens / 1_000_000) * input_price_per_mtk
        + (output_tokens / 1_000_000) * output_price_per_mtk
    )

    return {
        "mode": "token",
        "window_hours": FLUX_WINDOW_HOURS,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_spend": round(estimated_spend, 2),
    }


# ---------------------------------------------------------------------------
# GET /status — Health check and current configuration
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET"])
def status():
    """Return current configuration for debugging and verification."""
    return json.dumps({
        "service_account_email": SERVICE_ACCOUNT_EMAIL,
        "project_id": PROJECT_ID,
        "flux_mode": FLUX_MODE,
        "flux_budget": FLUX_BUDGET,
        "enforcement_tolerance": ENFORCEMENT_TOLERANCE,
        "flux_window_hours": FLUX_WINDOW_HOURS,
        "cost_per_call_expensive": COST_PER_CALL_EXPENSIVE,
        "cost_per_call_cheap": COST_PER_CALL_CHEAP,
        "usage_hourly_limit": USAGE_HOURLY_LIMIT,
        "usage_daily_limit": USAGE_DAILY_LIMIT,
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
