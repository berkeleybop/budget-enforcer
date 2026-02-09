"""
Cloud Run service to disable service account keys when budget is exceeded.

Two mechanisms:
1. POST / — Triggered by Pub/Sub messages from GCP Billing Budget alerts.
2. GET /check-usage — Triggered by Cloud Scheduler every 5 minutes.
   Queries Cloud Monitoring for real-time token usage and disables keys
   if cost thresholds are exceeded.
"""
import base64
import json
import os
import time
from collections import defaultdict

from flask import Flask, request
from google.cloud import iam_admin_v1
from google.cloud import monitoring_v3


app = Flask(__name__)


# Service account email to disable
SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")

# Token usage monitoring thresholds (USD)
USAGE_HOURLY_LIMIT = float(os.environ.get("USAGE_HOURLY_LIMIT", "10.00"))
USAGE_DAILY_LIMIT = float(os.environ.get("USAGE_DAILY_LIMIT", "50.00"))

# Vertex AI model pricing per 1M tokens (USD)
# Source: https://cloud.google.com/vertex-ai/generative-ai/pricing
#         https://platform.claude.com/docs/en/about-claude/pricing
PRICING = {
    "claude-opus-4-6":           {"input": 5.00,  "output": 25.00},
    "claude-opus-4-5":           {"input": 5.00,  "output": 25.00},
    "claude-opus-4-1":           {"input": 15.00, "output": 75.00},
    "claude-opus-4":             {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-5":         {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4":           {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":          {"input": 1.00,  "output": 5.00},
    "claude-haiku-3-5":          {"input": 0.80,  "output": 4.00},
    "gemini-2.0-flash":          {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash-001":      {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash-lite":     {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash-lite-001": {"input": 0.075, "output": 0.30},
    "gemini-2.5-flash":          {"input": 0.30,  "output": 2.50},
    "gemini-2.5-pro":            {"input": 1.25,  "output": 10.00},
}

# Cache pricing multipliers (relative to base input price)
CACHE_MULTIPLIERS = {
    "input":                1.0,
    "cache_read_input":     0.1,
    "cache_write_input":    1.25,
    "cache_write_5m_input": 1.25,
    "cache_write_1h_input": 2.0,
}


@app.route("/", methods=["POST"])
def handle_budget_alert():
   """Handle budget alert from Pub/Sub."""
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
           return f"Budget exceeded (${cost_amount} >= ${budget_amount}). Service account keys disabled.", 200

   return "Budget alert received but threshold not met", 200


def disable_service_account_keys():
   """Disable all keys for the configured service account."""
   client = iam_admin_v1.IAMClient()

   req = iam_admin_v1.ListServiceAccountKeysRequest(
       name=f"projects/{PROJECT_ID}/serviceAccounts/{SERVICE_ACCOUNT_EMAIL}"
   )

   keys = client.list_service_account_keys(request=req)

   for key in keys.keys:
       if key.key_type == iam_admin_v1.ListServiceAccountKeysRequest.KeyType.USER_MANAGED:
           disable_req = iam_admin_v1.DisableServiceAccountKeyRequest(
               name=key.name
           )
           client.disable_service_account_key(request=disable_req)
           print(f"Disabled key: {key.name}")


def query_token_usage(project_id, seconds_ago):
    """Query Cloud Monitoring for Vertex AI token usage over a time window."""
    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{project_id}"

    now = time.time()
    seconds = int(now)
    nanos = int((now - seconds) * 10**9)

    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": seconds, "nanos": nanos},
            "start_time": {"seconds": seconds - seconds_ago, "nanos": nanos},
        }
    )

    results = client.list_time_series(
        request={
            "name": project_name,
            "filter": 'metric.type = "aiplatform.googleapis.com/publisher/online_serving/token_count"',
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )

    totals = defaultdict(lambda: defaultdict(int))
    for ts in results:
        model = ts.resource.labels.get("model_user_id", "unknown")
        token_type = ts.metric.labels.get("type", "unknown")
        for point in ts.points:
            totals[model][token_type] += int(point.value.int64_value)

    return totals


def estimate_cost(totals):
    """Estimate USD cost from aggregated token counts."""
    total_cost = 0.0
    for model, types in totals.items():
        prices = PRICING.get(model)
        if not prices:
            print(f"WARNING: No pricing data for model '{model}', skipping.")
            continue

        for token_type, count in types.items():
            if token_type in CACHE_MULTIPLIERS:
                multiplier = CACHE_MULTIPLIERS[token_type]
                total_cost += (count / 1_000_000) * prices["input"] * multiplier

        output_tokens = types.get("output", 0)
        total_cost += (output_tokens / 1_000_000) * prices["output"]

    return total_cost


@app.route("/check-usage", methods=["GET"])
def check_usage():
    """Check Vertex AI token usage and disable keys if thresholds exceeded.

    Called periodically by Cloud Scheduler.
    """
    if not PROJECT_ID:
        return {"error": "GCP_PROJECT_ID not configured"}, 500

    results = {}
    action_taken = False

    # Check 1-hour rolling window
    try:
        hourly_totals = query_token_usage(PROJECT_ID, 3600)
        hourly_cost = estimate_cost(hourly_totals)
        results["hourly_cost"] = round(hourly_cost, 6)
        results["hourly_limit"] = USAGE_HOURLY_LIMIT
        results["hourly_exceeded"] = hourly_cost >= USAGE_HOURLY_LIMIT

        if hourly_cost >= USAGE_HOURLY_LIMIT:
            print(f"ALERT: Hourly token cost ${hourly_cost:.4f} exceeds limit ${USAGE_HOURLY_LIMIT:.2f}")
            disable_service_account_keys()
            action_taken = True
            results["action"] = "keys_disabled"
            results["reason"] = f"Hourly cost ${hourly_cost:.4f} >= limit ${USAGE_HOURLY_LIMIT:.2f}"
    except Exception as e:
        print(f"ERROR querying hourly usage: {e}")
        results["hourly_error"] = str(e)

    # Check 24-hour rolling window (skip if already disabled)
    if not action_taken:
        try:
            daily_totals = query_token_usage(PROJECT_ID, 86400)
            daily_cost = estimate_cost(daily_totals)
            results["daily_cost"] = round(daily_cost, 6)
            results["daily_limit"] = USAGE_DAILY_LIMIT
            results["daily_exceeded"] = daily_cost >= USAGE_DAILY_LIMIT

            if daily_cost >= USAGE_DAILY_LIMIT:
                print(f"ALERT: Daily token cost ${daily_cost:.4f} exceeds limit ${USAGE_DAILY_LIMIT:.2f}")
                disable_service_account_keys()
                action_taken = True
                results["action"] = "keys_disabled"
                results["reason"] = f"Daily cost ${daily_cost:.4f} >= limit ${USAGE_DAILY_LIMIT:.2f}"
        except Exception as e:
            print(f"ERROR querying daily usage: {e}")
            results["daily_error"] = str(e)

    if not action_taken:
        results["action"] = "none"

    print(f"Usage check complete: {results}")
    return results, 200


if __name__ == "__main__":
   port = int(os.environ.get("PORT", 8080))
   app.run(host="0.0.0.0", port=port)
