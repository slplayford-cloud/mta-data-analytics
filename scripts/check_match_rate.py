#!/usr/bin/env python3
"""
Measure how well the schedule resolver covers live traffic.

Polls every realtime feed once, resolves each trip, and reports the tier mix.
Run it at different hours -- coverage is much worse overnight, when planned
track work puts trains on diversions the static bundle does not describe.

Run:  python scripts/check_match_rate.py
      python scripts/check_match_rate.py --min-coverage 90
"""

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nyct_gtfs import NYCTFeed

from src.schedule import ScheduleIndex

FEED_GROUPS = ["1", "A", "B", "G", "J", "N", "L", "SI"]
TIERS = ["timetable", "pattern", "pattern_tail", "none"]


def service_date(now: datetime) -> date:
    """Before 3am the service date is still the previous day."""
    return (now - timedelta(days=1)).date() if now.hour < 3 else now.date()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-coverage", type=float, default=0.0,
                        help="exit non-zero if tiers 1-3 fall below this percentage")
    args = parser.parse_args()

    index = ScheduleIndex(Path(__file__).parent.parent / "static")
    index.build()

    now = datetime.now()
    today = service_date(now)
    print(f"[check] {now:%Y-%m-%d %H:%M}  service date {today}")
    print(f"[check] active service ids: {index.service_ids_for(today)}\n")

    tiers: Counter[str] = Counter()
    by_route: dict[str, Counter[str]] = defaultdict(Counter)

    for group in FEED_GROUPS:
        try:
            feed = NYCTFeed(group)
        except Exception as exc:
            print(f"[check] feed {group} failed: {exc}")
            continue
        for trip in feed.trips:
            stops = tuple(update.stop_id for update in trip.stop_time_updates)
            result = index.resolve(trip.route_id, trip.trip_id, stops, today)
            tiers[result.tier] += 1
            by_route[trip.route_id][result.tier] += 1

    total = sum(tiers.values())
    if not total:
        print("[check] no trips returned by any feed")
        return 1

    print(f"{'tier':14s}{'trips':>8s}{'share':>9s}")
    for tier in TIERS:
        print(f"{tier:14s}{tiers[tier]:8d}{tiers[tier] / total * 100:8.1f}%")

    covered = total - tiers["none"]
    coverage = covered / total * 100
    print(f"\n{'COVERED':14s}{covered:8d}{coverage:8.1f}%   (tiers 1-3 of {total} trips)")

    print(f"\n{'route':7s}{'trips':>7s}{'timetable':>11s}{'pattern':>9s}{'tail':>7s}{'none':>7s}")
    for route in sorted(by_route, key=lambda r: -sum(by_route[r].values())):
        counts = by_route[route]
        route_total = sum(counts.values())
        if route_total < 5:
            continue
        print(f"{route:7s}{route_total:7d}"
              f"{counts['timetable'] / route_total * 100:10.0f}%"
              f"{counts['pattern'] / route_total * 100:8.0f}%"
              f"{counts['pattern_tail'] / route_total * 100:6.0f}%"
              f"{counts['none'] / route_total * 100:6.0f}%")

    if coverage < args.min_coverage:
        print(f"\n[check] FAIL: {coverage:.1f}% below required {args.min_coverage}%")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
