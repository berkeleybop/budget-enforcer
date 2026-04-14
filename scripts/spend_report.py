#!/usr/bin/env python3
"""Print a per-model Vertex AI spend report for an arbitrary time window.

Queries Cloud Monitoring directly (no service auth needed — uses the active
gcloud identity's monitoring read permission). Applies the same PRICING /
cache / regional-premium logic as main.py so numbers match /check-usage.

Typical use:

    # Last 48 hours (matches /check-usage)
    scripts/spend_report.py --project gene-ontology-465618 --hours 48

    # Last week retrospective
    scripts/spend_report.py --project gene-ontology-465618 --hours 168

    # Arbitrary window
    scripts/spend_report.py --project gene-ontology-465618 \\
        --start 2026-04-14T13:00Z --end 2026-04-14T21:00Z
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from _monitoring import iter_token_counts
from _pricing import is_known, token_cost


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="GCP project ID")
    parser.add_argument("--hours", type=float, default=None,
                        help="Window size ending now, in hours (default 48)")
    parser.add_argument("--start", type=parse_iso, default=None,
                        help="ISO start time (overrides --hours)")
    parser.add_argument("--end", type=parse_iso, default=None,
                        help="ISO end time (defaults to now)")
    args = parser.parse_args()

    end = args.end or datetime.now(timezone.utc)
    if args.start is not None:
        start = args.start
    else:
        start = end - timedelta(hours=args.hours if args.hours else 48)

    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for model, ttype, _pt_end, count in iter_token_counts(
        args.project, start, end,
    ):
        totals[model][ttype] += count

    per_model_cost: dict[str, float] = {}
    grand_total = 0.0
    for model, types in totals.items():
        cost = sum(token_cost(model, t, n) for t, n in types.items())
        per_model_cost[model] = cost
        grand_total += cost

    print(f"Project:   {args.project}")
    print(f"Window:    {start.isoformat()} → {end.isoformat()}")
    print(f"Duration:  {(end - start).total_seconds() / 3600:.1f} hours")
    print(f"Total:     ${grand_total:,.2f}")
    print()
    print(f"{'MODEL':<28} {'COST':>10}  {'TOKENS':>12}  {'SOURCE':<8}")
    print("-" * 64)
    for model in sorted(per_model_cost, key=lambda m: -per_model_cost[m]):
        total_tokens = sum(totals[model].values())
        source = "known" if is_known(model) else "FALLBACK"
        print(
            f"{model:<28} ${per_model_cost[model]:>9,.4f}  "
            f"{total_tokens:>12,}  {source:<8}"
        )

    any_fallback = any(not is_known(m) for m in totals)
    if any_fallback:
        print()
        print("WARNING: one or more models fell through to FALLBACK_PRICING.")
        print("  Add them to scripts/_pricing.py (and main.py) for accurate costs.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
