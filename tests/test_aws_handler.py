"""Tests for aws/handler.py with in-memory fakes for IAM and CloudWatch.

Run: python3 -m pytest tests/
"""
import importlib
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aws"))

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("CONSUMER_USER", "app-bedrock-agent")
    monkeypatch.setenv("MONTHLY_BUDGET", "300")
    monkeypatch.setenv("ENFORCEMENT_TOLERANCE", "1.0")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.invalid/hook")
    import handler as h
    h = importlib.reload(h)
    h.posts = []
    monkeypatch.setattr(h, "post_slack", lambda text: h.posts.append(text))
    return h


class FakeIAM:
    def __init__(self, keys=("AKIA1",), status="Active", tags=()):
        self.keys = {k: status for k in keys}
        self.tags = list(tags)
        self.updates = []

    def list_access_keys(self, UserName):
        return {"AccessKeyMetadata": [
            {"AccessKeyId": k, "Status": s} for k, s in self.keys.items()]}

    def update_access_key(self, UserName, AccessKeyId, Status):
        self.keys[AccessKeyId] = Status
        self.updates.append((AccessKeyId, Status))

    def list_user_tags(self, UserName):
        return {"Tags": self.tags}


class FakeCW:
    """series: {(namespace, metric, dim, model): [hourly values]}"""
    def __init__(self, series=None, fail=None):
        self.series = series or {}
        self.fail = fail

    def list_metrics(self, Namespace, MetricName, NextToken=None):
        if self.fail:
            raise RuntimeError(self.fail)
        out = []
        for (ns, metric, dim, model) in self.series:
            if ns == Namespace and metric == MetricName:
                out.append({"Dimensions": [{"Name": dim, "Value": model}]})
                # a second series with an extra dimension must be ignored
                out.append({"Dimensions": [{"Name": "Project", "Value": "default"},
                                           {"Name": dim, "Value": model}]})
        return {"Metrics": out}

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime, NextToken=None):
        m = MetricDataQueries[0]["MetricStat"]["Metric"]
        d = m["Dimensions"][0]
        vals = self.series.get((m["Namespace"], m["MetricName"], d["Name"], d["Value"]), [])
        return {"MetricDataResults": [{"Values": vals}]}


def opus(input_tokens, output_tokens):
    return {
        ("AWS/BedrockMantle", "TotalInputTokens", "Model", "anthropic.claude-opus-5"): [input_tokens],
        ("AWS/BedrockMantle", "TotalOutputTokens", "Model", "anthropic.claude-opus-5"): [output_tokens],
    }


def run(h, iam, cw, event=None):
    return h.lambda_handler(event or {"source": "aws.events"}, now=NOW, iam=iam, cloudwatch=cw)


def test_under_budget_no_action(handler):
    iam = FakeIAM()
    r = run(handler, iam, FakeCW(opus(1_000_000, 20_000)))
    assert r["action"] == "none"
    assert r["estimated_spend"] == pytest.approx(6.875 + 0.55, abs=0.01)
    assert iam.updates == [] and handler.posts == []


def test_over_budget_revokes_every_active_key(handler):
    iam = FakeIAM(keys=("AKIA1", "AKIA2"))
    r = run(handler, iam, FakeCW(opus(40_000_000, 1_000_000)))  # 275 + 27.5
    assert r["action"] == "keys_disabled"
    assert sorted(iam.updates) == [("AKIA1", "Inactive"), ("AKIA2", "Inactive")]
    assert len(handler.posts) == 1


def test_already_revoked_is_quiet(handler):
    iam = FakeIAM(status="Inactive")
    r = run(handler, iam, FakeCW(opus(40_000_000, 1_000_000)))
    assert r["action"] == "already_revoked"
    assert iam.updates == [] and handler.posts == []


def test_override_suppresses_revocation(handler):
    iam = FakeIAM(tags=[{"Key": "budget-enforcer-override-until",
                         "Value": "2026-10-21T00:00:00Z"}])
    r = run(handler, iam, FakeCW(opus(40_000_000, 1_000_000)))
    assert r["action"] == "override"
    assert iam.updates == []


def test_expired_override_is_ignored(handler):
    iam = FakeIAM(tags=[{"Key": "budget-enforcer-override-until",
                         "Value": "2026-10-19T00:00:00Z"}])
    r = run(handler, iam, FakeCW(opus(40_000_000, 1_000_000)))
    assert r["action"] == "keys_disabled"


def test_unparseable_override_is_ignored(handler):
    iam = FakeIAM(tags=[{"Key": "budget-enforcer-override-until", "Value": "soon"}])
    assert run(handler, iam, FakeCW(opus(40_000_000, 1_000_000)))["action"] == "keys_disabled"


def test_unknown_model_uses_fallback_pricing(handler):
    series = {("AWS/BedrockMantle", "TotalInputTokens", "Model", "anthropic.claude-future-9"): [1_000_000]}
    r = run(handler, FakeIAM(), FakeCW(series))
    assert r["per_model"]["anthropic.claude-future-9"]["pricing"] == "fallback"
    assert r["estimated_spend"] == pytest.approx(6.875, abs=0.01)


def test_titan_counted_from_bedrock_namespace(handler):
    series = {("AWS/Bedrock", "InputTokenCount", "ModelId", "amazon.titan-embed-text-v2:0"): [50_000_000]}
    r = run(handler, FakeIAM(), FakeCW(series))
    assert r["estimated_spend"] == pytest.approx(1.0, abs=0.01)


def test_extra_dimension_series_not_double_counted(handler):
    # FakeCW lists each model twice (with and without Project); only one counts
    r = run(handler, FakeIAM(), FakeCW(opus(1_000_000, 0)))
    assert r["estimated_spend"] == pytest.approx(6.875, abs=0.01)


def test_estimator_failure_warns_and_does_not_revoke(handler):
    iam = FakeIAM()
    r = run(handler, iam, FakeCW(fail="throttled"))
    assert r["action"] == "none" and "throttled" in r["estimator_error"]
    assert iam.updates == []
    assert len(handler.posts) == 1
    run(handler, iam, FakeCW(fail="throttled"))       # rate-limited
    assert len(handler.posts) == 1


def test_billing_notification_revokes(handler):
    iam = FakeIAM()
    event = {"Records": [{"EventSource": "aws:sns",
                          "Sns": {"Subject": "AWS Budgets: cat-bedrock has exceeded your alert threshold",
                                  "Message": "ACTUAL Amount: $301.20"}}]}
    r = run(handler, iam, FakeCW(), event)
    assert r["action"] == "keys_disabled"
    assert iam.updates == [("AKIA1", "Inactive")]


def test_billing_notification_respects_override(handler):
    iam = FakeIAM(tags=[{"Key": "budget-enforcer-override-until",
                         "Value": "2026-10-21T00:00:00+00:00"}])
    event = {"Records": [{"EventSource": "aws:sns", "Sns": {"Message": "x"}}]}
    assert run(handler, iam, FakeCW(), event)["action"] == "override"


def test_window_is_calendar_month(handler):
    assert handler.window_start(NOW) == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_missing_consumer_user_fails_loudly(handler, monkeypatch):
    monkeypatch.setattr(handler, "CONSUMER_USER", "")
    with pytest.raises(RuntimeError):
        run(handler, FakeIAM(), FakeCW())
