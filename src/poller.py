#!/usr/bin/env python3
"""
Poller — polls all MTA GTFS-RT feeds every 30s, updates Supabase
`current_trains`, and broadcasts train positions to connected WebSocket clients.

Runs in a daemon thread; use Poller.run() as the thread target.
"""

import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Callable

import orjson
from supabase import Client
from nyct_gtfs import NYCTFeed
from nyct_gtfs.trip import Trip

from src import ingestion

log = logging.getLogger(__name__)

FEED_GROUPS    = ["1", "A", "B", "G", "J", "N", "L", "SI"]
POLL_INTERVAL  = 30.0


def _derive_service_date(now: datetime) -> date:
    """Before 3 AM local time, the service date is still yesterday."""
    if now.hour < 3:
        return (now - timedelta(days=1)).date()
    return now.date()


class Poller:
    def __init__(
        self,
        db: Client,
        interval: float = POLL_INTERVAL,
        ws_broadcast: Callable[[bytes], None] | None = None,
    ) -> None:
        self._db          = db
        self._interval    = interval
        self._ws_broadcast = ws_broadcast   # called after each poll with orjson bytes
        self._running     = False
        self._stop_event  = threading.Event()
        self._feeds: list[NYCTFeed] = [
            NYCTFeed(line, fetch_immediately=False) for line in FEED_GROUPS
        ]

    def run(self) -> None:
        """Blocking. Run as daemon thread target."""
        self._running = True
        log.info("Poller started (feeds: %s)", FEED_GROUPS)
        while self._running:
            t0 = time.monotonic()
            try:
                self._poll_once()
            except Exception:
                log.exception("Poll cycle failed")
            elapsed = time.monotonic() - t0
            self._stop_event.wait(max(0.0, self._interval - elapsed))

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    # ── internals ─────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        now = datetime.now()

        # Parallel refresh of all 8 feeds
        with ThreadPoolExecutor(max_workers=len(self._feeds)) as ex:
            futures = {ex.submit(f.refresh): f for f in self._feeds}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    log.warning("Feed refresh failed", exc_info=True)

        # Collect all trips with an assigned train
        all_trips: list[Trip] = []
        for feed in self._feeds:
            try:
                all_trips.extend(feed.filter_trips(train_assigned=True))
            except Exception:
                log.warning("filter_trips failed", exc_info=True)

        active_ids: set[str] = {t.trip_id for t in all_trips}

        # Run existing ingestion logic (snapshots + stop visit recording)
        for trip in all_trips:
            try:
                if trip.trip_id not in ingestion._schedules:
                    try:
                        ingestion.snapshot_schedule(self._db, trip)
                    except Exception:
                        # Supabase error (rate limit, schema mismatch, etc.) — skip, don't crash
                        ingestion._schedules.setdefault(trip.trip_id, {})
                        log.debug("snapshot_schedule failed for %s", trip.trip_id, exc_info=True)
                if not trip.underway:
                    continue
                current = {
                    stu.stop_id: (stu.arrival or stu.departure, stu.stop_name)
                    for stu in trip.stop_time_updates
                }
                if trip.trip_id in ingestion._last_predictions:
                    for stop_id, (pred_arr, stop_name) in ingestion._last_predictions[trip.trip_id].items():
                        if stop_id not in current:
                            try:
                                ingestion.record_stop_visit(self._db, trip, stop_id, pred_arr, stop_name)
                            except Exception:
                                log.debug("record_stop_visit failed", exc_info=True)
                ingestion._last_predictions[trip.trip_id] = current
            except Exception:
                log.debug("Ingestion error for %s", trip.trip_id, exc_info=True)

        # Clean up departed trips
        for trip_id in list(ingestion._last_predictions):
            if trip_id not in active_ids:
                del ingestion._last_predictions[trip_id]
        for trip_id in list(ingestion._schedules):
            if trip_id not in active_ids:
                try:
                    self._db.table("trip_schedules") \
                        .update({"is_active": False}) \
                        .eq("trip_id", trip_id) \
                        .execute()
                except Exception:
                    pass
                del ingestion._schedules[trip_id]

        # Build current state rows (includes all fields for WS broadcast)
        rows = [self._build_row(trip, now) for trip in all_trips if trip.underway]

        # Broadcast to WebSocket clients FIRST (fast path, doesn't wait for DB)
        if self._ws_broadcast and rows:
            payload = orjson.dumps({
                "type": "trains",
                "ts": now.timestamp(),
                "trains": rows,
            })
            try:
                self._ws_broadcast(payload)
            except Exception:
                log.warning("WS broadcast failed", exc_info=True)

        # Upsert to Supabase for persistence (best-effort; DB schema may lag code)
        # Strip loc_stop_id if the column doesn't exist in the current schema
        if rows:
            try:
                db_rows = [{k: v for k, v in r.items() if k != "loc_stop_id"} for r in rows]
                self._db.table("current_trains").upsert(db_rows, on_conflict="trip_id").execute()
            except Exception:
                log.debug("current_trains upsert failed", exc_info=True)

        # Delete stale rows from current_trains
        try:
            if active_ids:
                self._db.table("current_trains") \
                    .delete() \
                    .not_.in_("trip_id", list(active_ids)) \
                    .execute()
        except Exception:
            log.debug("current_trains cleanup failed", exc_info=True)

        log.info(
            "Poll done — %d trips total, %d underway, %d tracked",
            len(all_trips), len(rows), len(ingestion._last_predictions),
        )

    def _build_row(self, trip: Trip, now: datetime) -> dict:
        """Map a nyct_gtfs Trip to a current_trains row dict."""
        stus = trip.stop_time_updates
        next_stu = stus[0] if stus else None

        # trip.location is a stop_id string (e.g. "109N"), trip.location_status is a string
        loc_stop_id: str | None = trip.location  # platform stop_id
        loc_status: str | None = trip.location_status

        # Parent station = strip trailing N/S/E/W direction letter
        loc_station: str | None = None
        if loc_stop_id:
            loc_station = loc_stop_id[:-1] if loc_stop_id and loc_stop_id[-1] in "NSEWnsew" else loc_stop_id

        next_stop: str | None = None
        next_arr: str | None = None
        delay_seconds: int | None = None

        if next_stu:
            next_stop = next_stu.stop_id
            arr = next_stu.arrival or next_stu.departure
            if arr:
                next_arr = arr.isoformat()
                sched_entry = ingestion._schedules.get(trip.trip_id, {}).get(next_stop)
                if sched_entry:
                    sched_dt = sched_entry[0]  # tuple is (datetime, stop_sequence)
                    delay_seconds = int((arr - sched_dt).total_seconds())

        return {
            "trip_id":       trip.trip_id,
            "route_id":      trip.route_id,
            "direction":     trip.direction,
            "headsign":      trip.headsign_text,
            "loc_stop_id":   loc_stop_id,   # platform-level (e.g. "109N")
            "loc_station":   loc_station,   # parent station (e.g. "109")
            "status":        loc_status,
            "next_stop":     next_stop,
            "next_arr":      next_arr,
            "delay_seconds": delay_seconds,
            "shape_id":      trip.shape_id,
            "updated_at":    now.isoformat(),
        }
