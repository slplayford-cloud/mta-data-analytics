#!/usr/bin/env python3
"""
Poll every subway feed, record what happened, and publish positions.

Runs as an asyncio task on the server's own event loop. Each cycle refreshes all
eight feeds concurrently, folds the result into the Ingestor, persists trips and
departures, and hands a position payload to whatever wants to broadcast it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from nyct_gtfs import NYCTFeed
from nyct_gtfs.trip import Trip
from supabase import Client

from src.ingestion import STALL_THRESHOLD_SECONDS, Ingestor, arrival_of, parent_station, _safe
from src.schedule import ScheduleIndex

log = logging.getLogger(__name__)

# One specifier per feed URL. The "1" feed carries routes 1-7 and the shuttles.
FEED_GROUPS = ["1", "A", "B", "G", "J", "N", "L", "SI"]

# Supabase rejects very large request bodies; chunk bulk writes.
_WRITE_CHUNK = 500


def service_date_for(now: datetime) -> date:
    """Before 3am the service date is still the previous day."""
    return (now - timedelta(days=1)).date() if now.hour < 3 else now.date()


@dataclass(frozen=True)
class TrainPosition:
    """A train as the map needs it. Field names are the wire format."""

    trip_id: str
    route_id: str
    direction: str | None
    headsign: str | None
    shape_id: str | None
    nyc_train_id: str | None

    loc_stop_id: str | None
    loc_station: str | None
    status: str | None
    stop_index: int | None

    next_stop: str | None
    next_arr: str | None
    delay_seconds: int | None

    scheduled_track: str | None
    actual_track: str | None

    last_movement_at: str | None
    is_stalled: bool
    has_delay_alert: bool
    baseline: str

    def as_db_row(self, service_date: date, now: datetime) -> dict:
        row = asdict(self)
        row["service_date"] = service_date.isoformat()
        row["updated_at"] = now.isoformat()
        # Not a column -- the map uses it, the table does not store it.
        row.pop("baseline", None)
        return row


class Poller:
    """Drives one poll cycle per interval."""

    def __init__(
        self,
        db: Client,
        schedule: ScheduleIndex,
        interval: float = 15.0,
        on_positions=None,
    ) -> None:
        self._db = db
        self._schedule = schedule
        self._interval = interval
        self._on_positions = on_positions
        self._ingestor = Ingestor(schedule)
        self._feeds = [NYCTFeed(group, fetch_immediately=False) for group in FEED_GROUPS]
        self._running = False

    async def run(self) -> None:
        self._running = True
        log.info("poller started (%d feeds, %.0fs interval)", len(self._feeds), self._interval)
        while self._running:
            started = asyncio.get_running_loop().time()
            try:
                await self._poll_once()
            except Exception:
                log.exception("poll cycle failed")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, self._interval - elapsed))

    def stop(self) -> None:
        self._running = False

    # ── one cycle ────────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        now = datetime.now()
        today = service_date_for(now)

        observed = await self._refresh_all()
        if not observed:
            log.warning("no trips returned by any feed")
            return

        trips = [trip for trip, _ in observed]
        new_trips, visits = self._ingestor.observe(trips, today)

        # Publish before persisting: the map should not wait on the database.
        positions = [
            self._position(trip, feed_time)
            for trip, feed_time in observed
            if trip.underway
        ]
        if self._on_positions and positions:
            self._on_positions(positions, now)

        await self._persist(new_trips, visits, positions, today, now)

        tiers = _tier_counts(self._ingestor)
        log.info(
            "poll: %d trips, %d underway, %d new, %d departures | %s",
            len(trips), len(positions), len(new_trips), len(visits), tiers,
        )

    async def _refresh_all(self) -> list[tuple[Trip, datetime | None]]:
        """Refresh every feed concurrently.

        Each trip is paired with the timestamp its feed was generated. Feeds
        refresh at different rates, so staleness has to be judged per feed
        rather than against the wall clock.
        """
        async def refresh(feed: NYCTFeed) -> list[tuple[Trip, datetime | None]]:
            try:
                await feed.refresh_async()
                generated = _safe(feed, "last_generated")
                return [(trip, generated) for trip in feed.filter_trips(train_assigned=True)]
            except Exception:
                log.warning("feed refresh failed", exc_info=True)
                return []

        results = await asyncio.gather(*(refresh(feed) for feed in self._feeds))
        return [pair for group in results for pair in group]

    def _position(self, trip: Trip, feed_time: datetime | None) -> TrainPosition:
        updates = trip.stop_time_updates
        next_update = updates[0] if updates else None
        tracked = self._ingestor.tracked.get(trip.trip_id)

        next_stop = next_update.stop_id if next_update else None
        next_arrival = arrival_of(next_update) if next_update else None

        # Lateness against whatever baseline resolved for this trip. For a
        # pattern match there is no absolute schedule, so this stays None and
        # the map simply shows no delay rather than a fabricated one.
        delay = None
        if tracked and next_stop and next_arrival:
            scheduled = tracked.resolution.scheduled_arrival(next_stop, tracked.service_date)
            if scheduled:
                delay = int((next_arrival - scheduled).total_seconds())

        # The NYCT spec measures a stall as the gap between the vehicle's own
        # timestamp and the feed header, not the wall clock -- otherwise a feed
        # that is simply late makes every train in it look stopped.
        last_movement = _safe(trip, "last_position_update")
        reference = feed_time or datetime.now()
        stalled = bool(
            last_movement
            and (reference - last_movement).total_seconds() > STALL_THRESHOLD_SECONDS
        )

        location = trip.location
        return TrainPosition(
            trip_id=trip.trip_id,
            route_id=trip.route_id,
            direction=trip.direction,
            headsign=trip.headsign_text,
            shape_id=trip.shape_id,
            nyc_train_id=_safe(trip, "nyc_train_id"),
            loc_stop_id=location,
            loc_station=parent_station(location) if location else None,
            status=trip.location_status,
            stop_index=_safe(trip, "current_stop_sequence_index"),
            next_stop=next_stop,
            next_arr=next_arrival.isoformat() if next_arrival else None,
            delay_seconds=delay,
            scheduled_track=_safe(next_update, "scheduled_track") if next_update else None,
            actual_track=_safe(next_update, "actual_track") if next_update else None,
            last_movement_at=last_movement.isoformat() if last_movement else None,
            is_stalled=stalled,
            has_delay_alert=bool(_safe(trip, "has_delay_alert")),
            baseline=tracked.resolution.tier if tracked else "none",
        )

    # ── persistence ──────────────────────────────────────────────────────────

    async def _persist(
        self,
        new_trips,
        visits,
        positions: list[TrainPosition],
        today: date,
        now: datetime,
    ) -> None:
        """Write to Supabase off the event loop. Failures are logged, not fatal."""
        def write() -> None:
            if new_trips:
                self._upsert("trips", [t.as_row() for t in new_trips],
                             "service_date,trip_id")
            if visits:
                self._upsert("stop_visits", [v.as_row() for v in visits],
                             "service_date,trip_id,stop_id")
            if positions:
                rows = [p.as_db_row(today, now) for p in positions]
                self._upsert("current_trains", rows, "trip_id")
                self._delete_departed({p.trip_id for p in positions})

        await asyncio.to_thread(write)

    def _upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        for start in range(0, len(rows), _WRITE_CHUNK):
            chunk = rows[start:start + _WRITE_CHUNK]
            try:
                self._db.table(table).upsert(chunk, on_conflict=on_conflict).execute()
            except Exception:
                log.warning("%s upsert failed (%d rows)", table, len(chunk), exc_info=True)

    def _delete_departed(self, active_ids: set[str]) -> None:
        try:
            self._db.table("current_trains").delete() \
                .not_.in_("trip_id", list(active_ids)).execute()
        except Exception:
            log.debug("current_trains cleanup failed", exc_info=True)


def _tier_counts(ingestor: Ingestor) -> str:
    counts: dict[str, int] = {}
    for tracked in ingestor.tracked.values():
        counts[tracked.resolution.tier] = counts.get(tracked.resolution.tier, 0) + 1
    return " ".join(f"{tier}={count}" for tier, count in sorted(counts.items()))
