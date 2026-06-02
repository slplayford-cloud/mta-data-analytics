#!/usr/bin/env python3
"""
MTA real-time ingestion — tracks expected vs actual arrivals per station.
Schedule baseline loaded from Supabase stop_times; RT feed used for departure detection.

Run: python -m src.ingestion
"""

import os
import re
import time
import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, cast

from supabase import create_client, Client
from nyct_gtfs import NYCTFeed
from nyct_gtfs.trip import Trip

log = logging.getLogger(__name__)

FEED_LINE     = "1"
POLL_INTERVAL = 30  # seconds

# ── in-memory state ───────────────────────────────────────────────────────────

# trip_id → {stop_id → scheduled_arrival}
# Loaded from Supabase on startup; written when a new trip is first seen.
_schedules: dict[str, dict[str, datetime]] = {}

# trip_id → {stop_id → (predicted_arrival, stop_name, stop_sequence)}
# Rebuilt every poll. A stop disappearing = train departed that stop.
_last_predictions: dict[str, dict[str, tuple[datetime | None, str | None, int]]] = {}

# ── supabase ──────────────────────────────────────────────────────────────────

def get_client() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _fetch_schedule_from_static(
    db: Client,
    rt_trip_id: str,
    start_date: date,
) -> dict[str, datetime]:
    """
    Query stop_times for the scheduled arrivals for a given RT trip.
    Uses a LIKE prefix on rt_trip_key to handle the static/RT id mismatch.
    Returns {stop_id → scheduled_arrival datetime}, empty dict if no match.
    """
    prefix = re.sub(r'(\.\.[NS]X?)\w+$', r'\1', rt_trip_id) + '%'
    response = (
        db.table("stop_times")
        .select("stop_id, arrival_seconds")
        .like("rt_trip_key", prefix)
        .order("stop_sequence")
        .execute()
    )
    rows = cast(list[dict[str, Any]], response.data)
    if not rows:
        return {}
    midnight = datetime.combine(start_date, dtime.min)
    return {
        row["stop_id"]: midnight + timedelta(seconds=int(row["arrival_seconds"]))
        for row in rows
    }


def load_active_schedules(db: Client) -> None:
    """
    Warm _schedules from trip_schedules for any trips already underway
    at startup (crash recovery). New trips will lazy-load from stop_times.
    """
    response = (
        db.table("trip_schedules")
        .select("trip_id, stops")
        .eq("is_active", True)
        .execute()
    )
    rows = cast(list[dict[str, Any]], response.data)
    for row in rows:
        sched: dict[str, datetime] = {}
        for stop in cast(list[dict[str, Any]], row["stops"]):
            sched_arr: str | None = stop.get("sched_arr")
            if sched_arr:
                sched[stop["stop_id"]] = datetime.fromisoformat(sched_arr)
        _schedules[row["trip_id"]] = sched
    log.info(f"Warmed {len(_schedules)} active schedules from Supabase")


def snapshot_schedule(db: Client, trip: Trip) -> None:
    """
    Build the schedule baseline for a trip on first observation.
    Lazily fetches scheduled times from stop_times (static GTFS).
    Falls back to RT prediction for any stop not found in static data.
    """
    static = _fetch_schedule_from_static(db, trip.trip_id, trip.start_date)
    if static:
        log.debug(f"Static schedule for {trip.trip_id} ({len(static)} stops)")
    else:
        log.warning(f"No static match for {trip.trip_id}, using RT predictions as baseline")

    stops: list[dict[str, Any]] = []
    sched: dict[str, datetime] = {}
    for i, stu in enumerate(trip.stop_time_updates):
        t: datetime | None = static.get(stu.stop_id) or stu.arrival or stu.departure
        stops.append({
            "stop_id":   stu.stop_id,
            "seq":       i + 1,
            "stop_name": stu.stop_name,
            "sched_arr": t.isoformat() if t else None,
        })
        if t:
            sched[stu.stop_id] = t

    if not stops:
        return

    db.table("trip_schedules").insert({
        "trip_id":    trip.trip_id,
        "start_date": trip.start_date.isoformat(),
        "route_id":   trip.route_id,
        "direction":  trip.direction,
        "shape_id":   trip.shape_id,
        "stops":      stops,
    }).execute()

    _schedules[trip.trip_id] = sched
    log.debug(f"Snapshotted schedule for {trip.trip_id} ({len(stops)} stops)")


def record_stop_visit(
    db: Client,
    trip: Trip,
    stop_id: str,
    predicted_arrival: datetime | None,
    stop_name: str | None,
    stop_sequence: int,
) -> None:
    """Write a completed stop visit. delay_seconds = actual - scheduled."""
    scheduled: datetime | None = _schedules.get(trip.trip_id, {}).get(stop_id)

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
        f"delay={delay_seconds}s"
    )


# ── poll ──────────────────────────────────────────────────────────────────────

def poll(db: Client, feed: NYCTFeed) -> None:
    feed.refresh()

    # train_assigned=True catches trips ~30 min before departure for clean snapshots,
    # as well as all currently running trains.
    all_trips: list[Trip] = feed.filter_trips(line_id=FEED_LINE, train_assigned=True)
    active_ids: set[str] = {t.trip_id for t in all_trips}

    for trip in all_trips:
        trip_id: str = trip.trip_id

        # First time we've seen this trip — capture schedule baseline
        if trip_id not in _schedules:
            snapshot_schedule(db, trip)

        # Departure detection only makes sense once the train is moving
        if not trip.underway:
            continue

        # Build current predictions for remaining stops
        current: dict[str, tuple[datetime | None, str | None, int]] = {
            stu.stop_id: (stu.arrival or stu.departure, stu.stop_name, i + 1)
            for i, stu in enumerate(trip.stop_time_updates)
        }

        # Any stop in last poll but not this one → train just departed it
        if trip_id in _last_predictions:
            for stop_id, (pred_arr, stop_name, seq) in _last_predictions[trip_id].items():
                if stop_id not in current:
                    record_stop_visit(db, trip, stop_id, pred_arr, stop_name, seq)

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

    log.info(f"Active trips: {len(active_ids)} | Tracking: {len(_last_predictions)}")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    db: Client = get_client()
    feed: NYCTFeed = NYCTFeed(FEED_LINE, fetch_immediately=False)

    load_active_schedules(db)

    while True:
        try:
            poll(db, feed)
        except Exception:
            log.exception("Poll failed")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
