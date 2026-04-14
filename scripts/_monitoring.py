"""Shared Cloud Monitoring query helper for spend-reporting scripts.

Pulls the `aiplatform.googleapis.com/publisher/online_serving/token_count`
metric over an arbitrary time window, yielding (model, token_type, end_time,
count) tuples. Uses gcloud's access token for auth so scripts work as any
identity with monitoring read permission (no service-account key needed).
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

_METRIC_FILTER = (
    'metric.type='
    '"aiplatform.googleapis.com/publisher/online_serving/token_count"'
)


def gcloud_access_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def iter_token_counts(
    project: str,
    start: datetime,
    end: datetime,
    access_token: str | None = None,
) -> Iterator[tuple[str, str, datetime, int]]:
    """Yield (model, token_type, point_end_time, count) for the window."""
    if access_token is None:
        access_token = gcloud_access_token()

    params = {
        "filter": _METRIC_FILTER,
        "interval.startTime": start.astimezone(timezone.utc)
                                  .isoformat().replace("+00:00", "Z"),
        "interval.endTime":   end.astimezone(timezone.utc)
                                 .isoformat().replace("+00:00", "Z"),
        "view": "FULL",
        "pageSize": "1000",
    }
    page_token: str | None = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        url = (
            f"https://monitoring.googleapis.com/v3/projects/{project}"
            f"/timeSeries?" + urllib.parse.urlencode(params)
        )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req) as response:
            data = json.load(response)

        for ts in data.get("timeSeries", []):
            model = ts["resource"]["labels"].get("model_user_id", "unknown")
            ttype = ts["metric"]["labels"].get("type", "unknown")
            for point in ts.get("points", []):
                pt_end = datetime.fromisoformat(
                    point["interval"]["endTime"].replace("Z", "+00:00")
                )
                count = int(point["value"].get("int64Value", 0))
                if count:
                    yield model, ttype, pt_end, count

        page_token = data.get("nextPageToken")
        if not page_token:
            return
