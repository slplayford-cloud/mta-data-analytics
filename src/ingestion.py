#!/usr/bin/env python3
"""
Collects data from MTA realtime feed and persists observations to DuckDB.
Run from project root: python src/ingestion.py
"""

import logging
import signal
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from nyct_gtfs import NYCTFeed

from src.db import get_connection, get_scheduled_arrival, initialize, write_observation
from src.graph import SubwayGraph
from src.models import Train

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

FEED_GROUPS = ("1", "A", "B", "G", "J", "N", "L", "SI")
_NYC_TZ = ZoneInfo("America/New_York")


def _derive_service_date(dt: datetime) -> date:
    local = dt.astimezone(_NYC_TZ)
    if local.hour < 3:
        return (local - timedelta(days=1)).date()
    return local.date()


def parse_train(trip, observed_at: datetime) -> Train:
    location = trip.location
    return Train(
        trip_id=trip.trip_id,
        route_id=trip.route_id,
        direction=trip.direction,
        headsign=getattr(trip, "headsign_text", None),
        location_stop_id=location,
        location_parent_station=location[:-1] if location else None,
        location_status=trip.location_status if trip.location_status else None,
        last_position_update=trip.last_position_update,
        has_delay_alert=False,
        observed_at=observed_at,
        delay_seconds=None,
    )


def compute_delay(
    conn,
    train: Train,
    stop_time_updates,
    service_date: date,
) -> int | None:
    midnight = datetime(
        service_date.year, service_date.month, service_date.day, tzinfo=_NYC_TZ
    )
    for stu in stop_time_updates:
        if stu.arrival is None:
            continue
        sched = get_scheduled_arrival(conn, train.trip_id, stu.stop_id, service_date)
        if sched is None:
            continue
        predicted_s = int((stu.arrival.astimezone(_NYC_TZ) - midnight).total_seconds())
        return predicted_s - sched
    return None


class Poller:
    def __init__(
        self,
        conn,
        graph: SubwayGraph,
        interval_seconds: int = 30,
    ) -> None:
        self._conn = conn
        self._graph = graph
        self._interval = interval_seconds
        self._running = False

    def run(self) -> None:
        self._running = True
        while self._running:
            start = time.monotonic()
            try:
                self._poll_once()
            except Exception as exc:
                logging.error("poll cycle failed: %s", exc)
            elapsed = time.monotonic() - start
            remaining = self._interval - elapsed
            if remaining > 0 and self._running:
                time.sleep(remaining)

    def stop(self) -> None:
        self._running = False

    def _poll_once(self) -> None:
        observed_at = datetime.now(tz=timezone.utc)
        service_date = _derive_service_date(observed_at)
        trains: list[Train] = []

        for feed_key in FEED_GROUPS:
            try:
                feed = NYCTFeed(feed_specifier=feed_key)
                trips = feed.filter_trips(underway=True)
            except Exception as exc:
                logging.error("feed %s: %s", feed_key, exc)
                continue

            for trip in trips:
                try:
                    train = parse_train(trip, observed_at)
                    midnight = datetime(
                        service_date.year, service_date.month, service_date.day,
                        tzinfo=_NYC_TZ,
                    )

                    # Only include stops with predicted arrivals. stu.arrival is a
                    # naive local-time datetime (fromtimestamp); make it UTC-aware
                    # before storing in TIMESTAMPTZ.
                    # StopTimeUpdate exposes no stop_sequence, so seq is a 0-based
                    # relative index within the remaining stops list.
                    stop_preds: list[tuple[str, int, datetime | None, int | None]] = []
                    for seq, stu in enumerate(trip.stop_time_updates):
                        if stu.arrival is None:
                            continue
                        arr = stu.arrival.astimezone(timezone.utc)
                        sched = get_scheduled_arrival(
                            self._conn, train.trip_id, stu.stop_id, service_date
                        )
                        stop_preds.append((stu.stop_id, seq, arr, sched))

                    # Derive delay from the first stop that has a matching schedule.
                    train.delay_seconds = None
                    for _, _, arr, sched in stop_preds:
                        if sched is not None:
                            predicted_s = int(
                                (arr.astimezone(_NYC_TZ) - midnight).total_seconds()
                            )
                            train.delay_seconds = predicted_s - sched
                            break

                    write_observation(
                        self._conn, train, service_date, stop_preds or None
                    )
                    trains.append(train)
                except Exception as exc:
                    logging.warning(
                        "trip %s: %s", getattr(trip, "trip_id", "?"), exc
                    )

        self._graph.update_trains(trains)
        logging.info(
            "poll complete — %d trains (service_date %s)", len(trains), service_date
        )


if __name__ == "__main__":
    conn = get_connection("mta.duckdb")
    initialize(conn, static_dir="static/")
    graph = SubwayGraph(conn)
    graph.build()
    poller = Poller(conn, graph)

    def _handle_signal(sig, frame):  # type: ignore[type-arg]
        logging.info("shutting down (signal %d)…", sig)
        poller.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    poller.run()
    conn.close()
