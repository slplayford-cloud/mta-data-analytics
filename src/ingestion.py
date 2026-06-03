#!/usr/bin/env python3
"""
MTA real-time ingestion — tracks expected vs actual arrivals per station.
Only tracks trips from their origin. Mid-trip trains are ignored.

Run: python -m src.ingestion
"""

import os
import time
import logging
from datetime import datetime
from typing import Any

from supabase import create_client, Client
from nyct_gtfs import NYCTFeed
from nyct_gtfs.trip import Trip

log = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds

# ── in-memory state ───────────────────────────────────────────────────────────

# trip_id → {stop_id → (scheduled_arrival, stop_sequence)}
# Only populated for trips captured before departure — i + 1 is accurate
# when stop_time_updates contains the full route (train not yet underway).
_schedules: dict[str, dict[str, tuple[datetime, int]]] = {}

# trip_id → {stop_id → (predicted_arrival, stop_name)}
# Rebuilt every poll. A stop disappearing = train departed that stop.
_last_predictions: dict[str, dict[str, tuple[datetime | None, str | None]]] = {}

# ── supabase ──────────────────────────────────────────────────────────────────

def get_client() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])



def snapshot_schedule(db: Client, trip: Trip) -> None:
    """
    Capture the full route as the schedule baseline.
    Only called when trip.underway is False — at that point stop_time_updates
    contains every stop on the route, so i + 1 is the correct sequence.
    """
    stops: list[dict[str, Any]] = []
    sched: dict[str, tuple[datetime, int]] = {}
    for i, stu in enumerate(trip.stop_time_updates):
        t: datetime | None = stu.arrival or stu.departure
        seq: int = i + 1
        stops.append({
            "stop_id":   stu.stop_id,
            "seq":       seq,
            "stop_name": stu.stop_name,
            "sched_arr": t.isoformat() if t else None,
        })
        if t:
            sched[stu.stop_id] = (t, seq)

    if not stops:
        return

    # Set in-memory cache before the DB write so a write failure
    # doesn't cause an infinite retry loop on the next poll.
    _schedules[trip.trip_id] = sched

    db.table("trip_schedules").upsert({
        "trip_id":    trip.trip_id,
        "start_date": trip.start_date.isoformat(),
        "route_id":   trip.route_id,
        "direction":  trip.direction,
        "shape_id":   trip.shape_id,
        "stops":      stops,
    }, ignore_duplicates=True).execute()

    log.debug(f"Snapshotted {trip.trip_id} ({len(stops)} stops)")


def record_stop_visit(
    db: Client,
    trip: Trip,
    stop_id: str,
    predicted_arrival: datetime | None,
    stop_name: str | None,
) -> None:
    """Write a completed stop visit. delay_seconds = actual - scheduled."""
    entry: tuple[datetime, int] | None = _schedules.get(trip.trip_id, {}).get(stop_id)
    scheduled: datetime | None = entry[0] if entry else None
    stop_sequence: int | None = entry[1] if entry else None

    delay_seconds: int | None = None
    if scheduled and predicted_arrival:
        delay_seconds = int((predicted_arrival - scheduled).total_seconds())

    db.table("stop_visits").insert({
        "trip_id":           trip.trip_id,
        "start_date":        trip.start_date.isoformat(),
        "route_id":          trip.route_id,
        "direction":         trip.direction,
        "stop_id":           stop_id,
        "parent_station":    stop_id[:-1],  # strip N/S suffix
        "stop_name":         stop_name,
        "stop_sequence":     stop_sequence,
        "scheduled_arrival": scheduled.isoformat() if scheduled else None,
        "actual_arrival":    predicted_arrival.isoformat() if predicted_arrival else None,
        "delay_seconds":     delay_seconds,
    }).execute()

    log.debug(
        f"{trip.route_id} {trip.direction} | {stop_name or stop_id} | "
        f"seq={stop_sequence} delay={delay_seconds}s"
    )


# ── poll ──────────────────────────────────────────────────────────────────────

def poll(db: Client, feeds: list[NYCTFeed]) -> None:
    all_trips: list[Trip] = []
    for feed in feeds:
        feed.refresh()
        all_trips.extend(feed.filter_trips(train_assigned=True))
    active_ids: set[str] = {t.trip_id for t in all_trips}

    for trip in all_trips:
        trip_id: str = trip.trip_id

        if trip_id not in _schedules:
            snapshot_schedule(db, trip)

        if not trip.underway:
            continue

        # Build current predictions for remaining stops
        current: dict[str, tuple[datetime | None, str | None]] = {
            stu.stop_id: (stu.arrival or stu.departure, stu.stop_name)
            for stu in trip.stop_time_updates
        }

        # Any stop in last poll but not this one → train just departed it
        if trip_id in _last_predictions:
            for stop_id, (pred_arr, stop_name) in _last_predictions[trip_id].items():
                if stop_id not in current:
                    record_stop_visit(db, trip, stop_id, pred_arr, stop_name)

        _last_predictions[trip_id] = current

    # Clean up trips that have left the feed (completed or cancelled)
    for trip_id in list(_last_predictions):
        if trip_id not in active_ids:
            del _last_predictions[trip_id]

    for trip_id in list(_schedules):
        if trip_id not in active_ids:
            db.table("trip_schedules") \
              .update({"is_active": False}) \
              .eq("trip_id", trip_id) \
              .execute()
            del _schedules[trip_id]

    pre_dep: int = sum(1 for t in all_trips if t.trip_id in _schedules and not t.underway)
    skipped: int = sum(1 for t in all_trips if t.trip_id not in _schedules and t.underway)
    log.warning(
        f"Active: {len(active_ids)} | "
        f"Pre-departure: {pre_dep} | "
        f"Tracking: {len(_last_predictions)} | "
        f"Skipped (joined mid-trip): {skipped}"
    )


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    FEED_LINES: list[str] = ["1", "N"]

    db: Client = get_client()
    feeds: list[NYCTFeed] = [NYCTFeed(line, fetch_immediately=False) for line in FEED_LINES]

    while True:
        try:
            poll(db, feeds)
        except Exception:
            log.exception("Poll failed")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
