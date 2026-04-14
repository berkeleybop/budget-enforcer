#!/usr/bin/env python3
"""Render a stacked bar chart of Vertex AI model spend over a time window.

Queries Cloud Monitoring directly (same query path as spend_report.py),
applies the same PRICING / cache / regional-premium logic as main.py, and
writes a PNG. Optional --workshop-start / --workshop-end shade an annotated
band on the chart, useful for post-workshop retrospectives.

Requires matplotlib. If the system matplotlib is broken by a numpy mismatch,
create an isolated venv — see scripts/README.md.

Typical use:

    # Workshop day retrospective, 15-minute buckets
    scripts/plot_spend.py --project gene-ontology-465618 \\
        --start 2026-04-14T13:00Z --end 2026-04-14T21:00Z \\
        --bucket-minutes 15 \\
        --workshop-start 2026-04-14T14:00Z \\
        --workshop-end 2026-04-14T18:00Z \\
        --output /tmp/workshop_spend.png

    # Last 24 hours, 30-minute buckets
    scripts/plot_spend.py --project gene-ontology-465618 --hours 24 \\
        --output /tmp/spend_24h.png
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt    # noqa: E402

from _monitoring import iter_token_counts
from _pricing import token_cost


# Display names and colors for common models. Unknown models are rendered
# with a fallback color and their raw model_user_id label.
_MODEL_DISPLAY: dict[str, tuple[str, str]] = {
    "claude-opus-4-6":   ("Opus 4.6",     "#7b3f99"),
    "claude-opus-4-5":   ("Opus 4.5",     "#5e2b7a"),
    "claude-opus-4-1":   ("Opus 4.1",     "#3d1957"),
    "claude-sonnet-4-6": ("Sonnet 4.6",   "#3a86ff"),
    "claude-sonnet-4-5": ("Sonnet 4.5",   "#8ecae6"),
    "claude-haiku-4-5":  ("Haiku 4.5",    "#06a77d"),
    "count-tokens":      ("count-tokens", "#bbbbbb"),
}
_FALLBACK_COLOR = "#cc3366"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--hours", type=float, default=None,
                        help="Window ending now (overridden by --start/--end)")
    parser.add_argument("--start", type=parse_iso, default=None)
    parser.add_argument("--end", type=parse_iso, default=None)
    parser.add_argument("--bucket-minutes", type=int, default=30)
    parser.add_argument("--tz-offset-hours", type=float, default=-7.0,
                        help="Hours to offset from UTC for x-axis labels "
                             "(default -7 for PT)")
    parser.add_argument("--tz-label", default="PT",
                        help="Short label for the display timezone")
    parser.add_argument("--workshop-start", type=parse_iso, default=None)
    parser.add_argument("--workshop-end", type=parse_iso, default=None)
    parser.add_argument("--output", default="/tmp/spend.png")
    parser.add_argument("--title", default=None)
    args = parser.parse_args()

    end = args.end or datetime.now(timezone.utc)
    if args.start is not None:
        start = args.start
    else:
        start = end - timedelta(hours=args.hours if args.hours else 24)

    bucket_sec = args.bucket_minutes * 60
    bucket_starts: list[datetime] = []
    t = start
    while t < end:
        bucket_starts.append(t)
        t += timedelta(minutes=args.bucket_minutes)

    # bucket_cost[bucket_start][model] = USD
    bucket_cost: dict[datetime, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for model, ttype, pt_end, count in iter_token_counts(
        args.project, start, end,
    ):
        idx = int((pt_end - start).total_seconds() // bucket_sec)
        if 0 <= idx < len(bucket_starts):
            bucket_cost[bucket_starts[idx]][model] += token_cost(
                model, ttype, count,
            )

    models_seen = sorted({m for buckets in bucket_cost.values() for m in buckets})
    # Put known models first (in display order), then any unknowns.
    def sort_key(m: str) -> tuple[int, str]:
        try:
            return (list(_MODEL_DISPLAY).index(m), m)
        except ValueError:
            return (len(_MODEL_DISPLAY), m)
    models_seen.sort(key=sort_key)

    tz_offset = timedelta(hours=args.tz_offset_hours)
    x_vals = [bs + tz_offset for bs in bucket_starts]
    bar_width_days = args.bucket_minutes / 60 / 24 * 0.95

    fig, ax = plt.subplots(figsize=(13, 6.5))
    bottoms = [0.0] * len(bucket_starts)

    for model in models_seen:
        heights = [bucket_cost[bs].get(model, 0.0) for bs in bucket_starts]
        model_total = sum(heights)
        if model_total <= 0:
            continue
        label_name, color = _MODEL_DISPLAY.get(
            model, (model, _FALLBACK_COLOR)
        )
        ax.bar(
            x_vals, heights, bottom=bottoms, width=bar_width_days,
            align="edge", color=color,
            label=f"{label_name}  (${model_total:,.2f})",
            edgecolor="white", linewidth=0.3,
        )
        bottoms = [b + h for b, h in zip(bottoms, heights)]

    if args.workshop_start and args.workshop_end:
        ws = args.workshop_start + tz_offset
        we = args.workshop_end + tz_offset
        ax.axvspan(ws, we, alpha=0.08, color="orange",
                   label="Workshop window")
        ax.axvline(ws, color="orange", linestyle="--",
                   linewidth=1.2, alpha=0.7)
        ax.axvline(we, color="orange", linestyle="--",
                   linewidth=1.2, alpha=0.7)

    total = sum(sum(v.values()) for v in bucket_cost.values())
    title = args.title or (
        f"Vertex AI model spend — "
        f"{start.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%MZ')} "
        f"to {end.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}  "
        f"(total ${total:,.2f})"
    )
    ax.set_title(title, fontsize=12)
    ax.set_ylabel(f"Cost per {args.bucket_minutes}-min bucket (USD)")
    ax.set_xlabel(f"Time ({args.tz_label})")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I:%M%p"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.grid(axis="y", alpha=0.25)
    ax.legend(loc="upper right", fontsize=9, framealpha=0.95)
    plt.tight_layout()
    plt.savefig(args.output, dpi=140)

    print(f"wrote {args.output}  (total ${total:,.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
