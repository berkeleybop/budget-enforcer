"""
Budget Enforcer, AWS adapter: Lambda handler for Amazon Bedrock spend control.

The same pattern as the GCP service (../main.py), on AWS:

- A dedicated IAM user (the "consumer") holds the only credential an
  application uses for Bedrock: one access key.  Enforcement sets every
  active key of that user Inactive; recovery sets it Active again.
- BILLING PATH: AWS Budgets publishes to an SNS topic when actual spend
  reaches 100% of the budget; SNS invokes this function, which revokes.
  Accurate but late: AWS documents billing data as refreshed at least once
  a day.
- FLUX PATH: an EventBridge schedule (every 5 minutes) invokes this
  function, which sums token counts from CloudWatch over the window
  (default: the current calendar month, UTC), prices them, and revokes
  when the estimate reaches budget x tolerance.

Metrics read:
  AWS/BedrockMantle  TotalInputTokens, TotalOutputTokens   (dimension Model)
  AWS/Bedrock        InputTokenCount, OutputTokenCount     (dimension ModelId)
Mantle publishes no separate cache counts, so input tokens are priced at the
model's cache-write rate: an upper bound whatever the cache mix.

Operator override: an IAM tag on the consumer user,
  budget-enforcer-override-until = <ISO 8601 UTC time>
suppresses revocation until that time (both paths), so a manual restore is
not undone by the next run.  It is reported on Slack and expires by itself.

Estimator failure (CloudWatch errors): warn on Slack, keep running, do not
revoke.  The billing path still enforces.

Runtime: Python 3.12 Lambda; stdlib and the boto3 bundled with the runtime.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3


# ---------------------------------------------------------------------------
# Pricing, USD per 1M tokens.  Source: AWS Price List
# AmazonBedrockFoundationModels, us-east-1, publication 2026-09-30, regional
# ("standard") endpoint.  "input" is the 5-minute cache-write rate, not the
# base input rate: see the module docstring.  Keys are the model ids as they
# appear in the CloudWatch Model / ModelId dimension.
# Check on every model change; an unknown model uses FALLBACK_PRICING.
# ---------------------------------------------------------------------------
PRICING = {
    "anthropic.claude-opus-5":   {"input": 6.875, "output": 27.50},
    "anthropic.claude-opus-5-5": {"input": 5.50,  "output": 22.00},
    "amazon.titan-embed-text-v2:0": {"input": 0.02, "output": 0.0},
}
# For a model missing from PRICING.  Deliberately above every current Claude
# rate on Bedrock (Claude 3 Opus: input 15.00, cache write 18.75, output
# 75.00), so an unlisted model is overestimated, never under.
FALLBACK_PRICING = {"input": 18.75, "output": 75.00}

OVERRIDE_TAG = "budget-enforcer-override-until"

METRIC_SOURCES = (
    # namespace, model dimension, input metric, output metric
    ("AWS/BedrockMantle", "Model", "TotalInputTokens", "TotalOutputTokens"),
    ("AWS/Bedrock", "ModelId", "InputTokenCount", "OutputTokenCount"),
)


def _env_float(name, default):
    return float(os.environ.get(name, default))


CONSUMER_USER = os.environ.get("CONSUMER_USER", "")
MONTHLY_BUDGET = _env_float("MONTHLY_BUDGET", "0")
ENFORCEMENT_TOLERANCE = _env_float("ENFORCEMENT_TOLERANCE", "1.0")
# "month" = since 00:00 UTC on the 1st; a number = that many hours back.
FLUX_WINDOW = os.environ.get("FLUX_WINDOW", "month")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DEPLOYMENT_NAME = os.environ.get("DEPLOYMENT_NAME", "budget-enforcer")
# Optional SNS topic (the module's alarms topic, which mails the operators):
# every revocation is also published there, so a Slack outage at the moment
# of revocation does not leave it unannounced.
ALARM_TOPIC_ARN = os.environ.get("ALARM_TOPIC_ARN", "")
# Models the consumer may call (comma-separated ids, set by Terraform).
# Always queried, because ListMetrics only returns series with data in the
# last two weeks, and month-to-date must include earlier usage.
CONFIGURED_MODELS = [m.strip() for m in
                     os.environ.get("CONFIGURED_MODELS", "").split(",") if m.strip()]
ESTIMATOR_ALERT_INTERVAL_MINUTES = _env_float(
    "ESTIMATOR_ALERT_INTERVAL_MINUTES", "60")

_last_estimator_alert = None
_sns = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context=None, now=None, iam=None, cloudwatch=None,
                   sns=None):
    """Route an SNS (billing) or scheduled (flux) invocation."""
    global _sns
    now = now or datetime.now(timezone.utc)
    iam = iam or boto3.client("iam")
    cloudwatch = cloudwatch or boto3.client("cloudwatch")
    _sns = sns if sns is not None else (
        boto3.client("sns") if ALARM_TOPIC_ARN else None)

    if not CONSUMER_USER:
        raise RuntimeError("CONSUMER_USER is not set")

    records = event.get("Records") or []
    if records and records[0].get("EventSource") == "aws:sns":
        return handle_budget_notification(records, now, iam)
    return check_usage(now, iam, cloudwatch)


# ---------------------------------------------------------------------------
# Billing path
# ---------------------------------------------------------------------------

def handle_budget_notification(records, now, iam):
    """AWS Budgets reached its enforcing threshold; revoke.

    Only the 100% notification is wired to the SNS topic (lower thresholds
    are email-only), so any message on the topic means "revoke".
    """
    sns_record = records[0].get("Sns") or {}
    subject = sns_record.get("Subject") or "AWS Budgets"
    message = (sns_record.get("Message") or "")[:500]
    reason = f"Billing path: {subject}. {message}".strip()
    print(reason)
    return enforce(reason, now, iam)


# ---------------------------------------------------------------------------
# Flux path
# ---------------------------------------------------------------------------

def window_start(now):
    if FLUX_WINDOW == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(hours=float(FLUX_WINDOW))


def check_usage(now, iam, cloudwatch):
    if MONTHLY_BUDGET <= 0:
        return {"status": "skipped", "reason": "MONTHLY_BUDGET not set"}

    threshold = MONTHLY_BUDGET * ENFORCEMENT_TOLERANCE
    start = window_start(now)
    try:
        totals = token_totals(cloudwatch, start, now)
    except Exception as e:  # noqa: BLE001 -- any failure means "no figure"
        result = {"action": "none", "estimator_error": str(e),
                  "threshold": threshold}
        warn_estimator_unavailable(str(e), now)
        print(json.dumps(result))
        return result

    estimate, per_model = price(totals)
    result = {
        "window_start": start.isoformat(),
        "estimated_spend": round(estimate, 2),
        "threshold": threshold,
        "per_model": per_model,
    }
    if estimate >= threshold:
        reason = (f"Flux estimate: ${estimate:.2f} >= ${threshold:.2f} "
                  f"(${MONTHLY_BUDGET:.2f} budget x {ENFORCEMENT_TOLERANCE}) "
                  f"since {start:%Y-%m-%d %H:%M} UTC")
        result.update(enforce(reason, now, iam))
    else:
        result["action"] = "none"
    print(json.dumps(result))
    return result


def token_totals(cloudwatch, start, end):
    """Sum input and output tokens per model over [start, end)."""
    totals = {}
    for namespace, dim, in_metric, out_metric in METRIC_SOURCES:
        # Discovery sees only the last two weeks; the configured and priced
        # models are queried regardless, so early-month usage still counts.
        models = (set(_models_with_metric(cloudwatch, namespace, in_metric, dim))
                  | set(_models_with_metric(cloudwatch, namespace, out_metric, dim))
                  | set(CONFIGURED_MODELS) | set(PRICING))
        for model in sorted(models):
            for metric_name, kind in ((in_metric, "input"), (out_metric, "output")):
                total = _sum_metric(cloudwatch, namespace, metric_name,
                                    dim, model, start, end)
                if total:
                    totals.setdefault(model, {"input": 0, "output": 0})
                    totals[model][kind] += total
    return totals


def _models_with_metric(cloudwatch, namespace, metric_name, dim):
    models = set()
    kwargs = {"Namespace": namespace, "MetricName": metric_name}
    while True:
        page = cloudwatch.list_metrics(**kwargs)
        for m in page.get("Metrics", []):
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            # Only the series with exactly the model dimension, so a model
            # is not counted once per extra dimension (e.g. Project).
            if set(dims) == {dim}:
                models.add(dims[dim])
        token = page.get("NextToken")
        if not token:
            return sorted(models)
        kwargs["NextToken"] = token


def _sum_metric(cloudwatch, namespace, metric_name, dim, value, start, end):
    total = 0.0
    kwargs = {
        "MetricDataQueries": [{
            "Id": "m",
            "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric_name,
                           "Dimensions": [{"Name": dim, "Value": value}]},
                # 5-minute buckets: retained 63 days, so a whole month fits,
                # and the first hour of a month is not blind.
                "Period": 300,
                "Stat": "Sum",
            },
        }],
        "StartTime": start,
        "EndTime": end,
    }
    while True:
        page = cloudwatch.get_metric_data(**kwargs)
        for r in page.get("MetricDataResults", []):
            # GetMetricData reports failures in-band with HTTP 200.
            # "PartialData" is normal pagination (a NextToken follows).
            status = r.get("StatusCode", "Complete")
            if status not in ("Complete", "PartialData"):
                raise RuntimeError(
                    f"GetMetricData {namespace}/{metric_name} {value}: "
                    f"{status} {r.get('Messages')}")
            total += sum(r.get("Values", []))
        token = page.get("NextToken")
        if not token:
            return total
        kwargs["NextToken"] = token


def price(totals):
    estimate, per_model = 0.0, {}
    for model, t in totals.items():
        p = PRICING.get(model)
        if p is None:
            print(f"WARNING: model '{model}' not in PRICING; "
                  f"using FALLBACK_PRICING")
            p = FALLBACK_PRICING
        cost = (t["input"] * p["input"] + t["output"] * p["output"]) / 1e6
        per_model[model] = {"input": t["input"], "output": t["output"],
                            "cost": round(cost, 2),
                            "pricing": "known" if model in PRICING else "fallback"}
        estimate += cost
    return estimate, per_model


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def override_until(iam, now):
    """The override expiry if one is in force, else None."""
    tags, kwargs = [], {"UserName": CONSUMER_USER}
    while True:
        page = iam.list_user_tags(**kwargs)
        tags += page.get("Tags", [])
        if not page.get("IsTruncated"):
            break
        kwargs["Marker"] = page["Marker"]
    for tag in tags:
        if tag["Key"] == OVERRIDE_TAG:
            value = tag["Value"].strip()
            if value[-1:] in ("Z", "z"):
                value = value[:-1] + "+00:00"
            try:
                until = datetime.fromisoformat(value)
            except ValueError:
                print(f"WARNING: unparseable {OVERRIDE_TAG}={tag['Value']!r}; ignored")
                return None
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return until if until > now else None
    return None


def enforce(reason, now, iam):
    until = override_until(iam, now)
    keys = iam.list_access_keys(UserName=CONSUMER_USER).get(
        "AccessKeyMetadata", [])
    active = [k["AccessKeyId"] for k in keys if k["Status"] == "Active"]

    if until is not None:
        print(f"Override in force until {until.isoformat()}; not revoking. {reason}")
        return {"action": "override", "override_until": until.isoformat()}

    if not active:
        # Already revoked: stay quiet, so a tripped budget does not post
        # every five minutes.
        return {"action": "already_revoked"}

    disabled = []
    for key_id in active:
        iam.update_access_key(UserName=CONSUMER_USER, AccessKeyId=key_id,
                              Status="Inactive")
        disabled.append(key_id)
    print(f"Disabled {len(disabled)} key(s) of {CONSUMER_USER}: {reason}")
    publish_alarm(
        f"{DEPLOYMENT_NAME}: budget enforcer disabled {len(disabled)} key(s)",
        f"{len(disabled)} key(s) of {CONSUMER_USER} disabled.\n"
        f"Reason: {reason}\nRecovery: docs/SOP.md, R-AWS.")
    post_slack(
        f":rotating_light: *Budget Enforcer triggered* ({DEPLOYMENT_NAME}): "
        f"{len(disabled)} key(s) of `{CONSUMER_USER}` disabled\n"
        f"*Reason:* {reason}\n"
        f"*Recovery:* find the cause first.  Then set the key Active and add "
        f"an override so the next check does not revoke it again:\n"
        f"`aws iam tag-user --user-name {CONSUMER_USER} --tags "
        f"Key={OVERRIDE_TAG},Value=<UTC time>` then "
        f"`aws iam update-access-key --user-name {CONSUMER_USER} "
        f"--access-key-id <id> --status Active`.  See the AWS section of "
        f"`docs/SOP.md`."
    )
    return {"action": "keys_disabled", "keys": disabled}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def warn_estimator_unavailable(error, now):
    global _last_estimator_alert
    print(f"WARNING: flux estimator unavailable: {error}")
    if (_last_estimator_alert is not None and now - _last_estimator_alert <
            timedelta(minutes=ESTIMATOR_ALERT_INTERVAL_MINUTES)):
        return
    _last_estimator_alert = now
    post_slack(
        f":warning: *Budget Enforcer: spend estimator unavailable* "
        f"({DEPLOYMENT_NAME})\n*Error:* {error[:300]}\n"
        f"Keys are *not* disabled.  Until it recovers, only the billing path "
        f"enforces the budget (about a day behind).  Repeats at most every "
        f"{ESTIMATOR_ALERT_INTERVAL_MINUTES:g} minutes per instance."
    )


def publish_alarm(subject, text):
    """Publish to ALARM_TOPIC_ARN (email).  Logs, never raises."""
    if not (ALARM_TOPIC_ARN and _sns):
        return
    try:
        _sns.publish(TopicArn=ALARM_TOPIC_ARN, Subject=subject[:99],
                     Message=text)
    except Exception as e:  # noqa: BLE001 -- notification must not block
        print(f"Alarm topic publish failed: {e}")


def post_slack(text):
    """Post one message to SLACK_WEBHOOK_URL.  Logs, never raises."""
    if not SLACK_WEBHOOK_URL:
        return
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Slack notification sent (status {resp.status})")
    except (urllib.error.URLError, OSError) as e:
        print(f"Slack notification failed: {e}")
