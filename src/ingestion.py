#!/usr/bin/env python3
"""
Turn the realtime feed into a record of what actually happened.

The feed never says "this train departed". It publishes the stops a train has
left to make, and that list shrinks. A stop disappearing between two polls means
the train has just left it, and the last arrival time predicted for that stop is
the closest thing to an observed arrival the feed offers.

Each departure is measured three ways, because no single number is honest for
every trip:

  runtime_deviation    how much longer the train took between the last two stops
                       than the schedule allows. Defined for every trip we can
                       resolve to a pattern, so it is the comparable metric.
  delay_vs_schedule    minutes late against the published timetable. Only
                       meaningful for trips found in the timetable.
  delay_vs_prediction  how far the MTA's own prediction moved. Measures forecast
                       error, not lateness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, Sequence

from src.schedule import Resolution

log = logging.getLogger(__name__)

# nyct-gtfs returns the epoch for unset protobuf timestamps rather than None.
_MIN_VALID = datetime(2000, 1, 1)

# The NYCT spec: if a vehicle timestamp is more than this far behind the feed
# header, the train is not moving and countdown clocks should stop.
STALL_THRESHOLD_SECONDS = 90


def stu_time(value: datetime | None) -> datetime | None:
    """Reject the epoch placeholder nyct-gtfs uses for unset fields."""
    return value if value is not None and value > _MIN_VALID else None


def arrival_of(update: StopUpdate) -> datetime | None:
    return stu_time(update.arrival) or stu_time(update.departure)


def parent_station(stop_id: str) -> str:
    """Strip the N/S platform suffix: "109N" -> "109"."""
    return stop_id[:-1] if stop_id and stop_id[-1] in "NSEW" else stop_id


class StopUpdate(Protocol):
    """The part of a GTFS-RT stop_time_update this module reads."""

    # Read-only members, so a supplier with a narrower type still satisfies this.
    @property
    def stop_id(self) -> str: ...
    @property
    def arrival(self) -> datetime | None: ...
    @property
    def departure(self) -> datetime | None: ...


class FeedTrip(Protocol):
    """The part of a nyct-gtfs Trip this module reads.

    Declared structurally rather than importing the concrete class, so departure
    detection can be tested without constructing protobuf messages.
    """

    @property
    def trip_id(self) -> str: ...
    @property
    def route_id(self) -> str: ...
    @property
    def direction(self) -> str | None: ...
    @property
    def headsign_text(self) -> str | None: ...
    @property
    def shape_id(self) -> str | None: ...
    @property
    def stop_time_updates(self) -> Sequence[StopUpdate]: ...


class ScheduleResolver(Protocol):
    """The single method Ingestor needs from ScheduleIndex."""

    def resolve(
        self,
        route_id: str,
        trip_id: str,
        observed_stops: tuple[str, ...],
        service_date: date,
    ) -> Resolution: ...


@dataclass
class StopVisit:
    """One observed departure, ready to write."""

    service_date: date
    trip_id: str
    stop_id: str
    parent_station: str
    stop_sequence: int | None
    scheduled_arrival: datetime | None
    predicted_arrival: datetime | None
    actual_arrival: datetime | None
    runtime_deviation: int | None
    delay_vs_schedule: int | None
    delay_vs_prediction: int | None
    actual_track: str | None

    def as_row(self) -> dict:
        return {
            "service_date": self.service_date.isoformat(),
            "trip_id": self.trip_id,
            "stop_id": self.stop_id,
            "parent_station": self.parent_station,
            "stop_sequence": self.stop_sequence,
            "scheduled_arrival": _iso(self.scheduled_arrival),
            "predicted_arrival": _iso(self.predicted_arrival),
            "actual_arrival": _iso(self.actual_arrival),
            "runtime_deviation": self.runtime_deviation,
            "delay_vs_schedule": self.delay_vs_schedule,
            "delay_vs_prediction": self.delay_vs_prediction,
            "actual_track": self.actual_track,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class TrackedTrip:
    """Everything we remember about one trip between polls."""

    trip_id: str
    service_date: date
    route_id: str
    direction: str | None
    headsign: str | None
    shape_id: str | None
    nyc_train_id: str | None
    resolution: Resolution

    # The feed's own prediction for each stop, captured the first time we saw
    # the trip. Comparing against this measures MTA forecast error.
    predictions_at_start: dict[str, datetime] = field(default_factory=dict)
    # Stops still ahead of the train as of the previous poll.
    remaining: dict[str, datetime | None] = field(default_factory=dict)
    # Where the train was last seen departing, to measure the next segment.
    last_departure: tuple[str, datetime] | None = None
    recorded: set[str] = field(default_factory=set)

    def sequence_of(self, stop_id: str) -> int | None:
        for stop in self.resolution.stops:
            if stop.stop_id == stop_id:
                return stop.sequence
        return None

    def as_row(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "service_date": self.service_date.isoformat(),
            "route_id": self.route_id,
            "direction": self.direction,
            "headsign": self.headsign,
            "shape_id": self.shape_id,
            "nyc_train_id": self.nyc_train_id,
            "baseline": self.resolution.tier,
            "is_supplemental": self.resolution.is_supplemental,
            "scheduled_stops": [
                {
                    "stop_id": stop.stop_id,
                    "seq": stop.sequence,
                    "sched_arr": stop.arrival_seconds,
                }
                for stop in self.resolution.stops
            ],
        }


class Ingestor:
    """Tracks live trips and emits a StopVisit for each observed departure.

    Holds no database handle. Callers poll `observe()` with the current feed
    contents and write whatever it returns.
    """

    def __init__(self, schedule: ScheduleResolver) -> None:
        self._schedule = schedule
        self._trips: dict[str, TrackedTrip] = {}

    @property
    def tracked(self) -> dict[str, TrackedTrip]:
        return self._trips

    def observe(
        self, trips: Sequence[FeedTrip], service_date: date
    ) -> tuple[list[TrackedTrip], list[StopVisit]]:
        """Fold one poll's worth of feed data in.

        Returns the trips seen for the first time and the departures detected
        since the previous call.
        """
        new_trips: list[TrackedTrip] = []
        visits: list[StopVisit] = []

        for trip in trips:
            tracked = self._trips.get(trip.trip_id)
            if tracked is None:
                tracked = self._begin(trip, service_date)
                self._trips[trip.trip_id] = tracked
                new_trips.append(tracked)

            visits.extend(self._detect_departures(trip, tracked))

        self._drop_finished({trip.trip_id for trip in trips})
        return new_trips, visits

    def _begin(self, trip: FeedTrip, service_date: date) -> TrackedTrip:
        stops = tuple(update.stop_id for update in trip.stop_time_updates)
        resolution = self._schedule.resolve(
            trip.route_id, trip.trip_id, stops, service_date
        )
        predictions = {
            update.stop_id: arrival
            for update in trip.stop_time_updates
            if (arrival := arrival_of(update)) is not None
        }
        return TrackedTrip(
            trip_id=trip.trip_id,
            service_date=service_date,
            route_id=trip.route_id,
            direction=trip.direction,
            headsign=trip.headsign_text,
            shape_id=trip.shape_id,
            nyc_train_id=_safe(trip, "nyc_train_id"),
            resolution=resolution,
            predictions_at_start=predictions,
        )

    def _detect_departures(
        self, trip: FeedTrip, tracked: TrackedTrip
    ) -> list[StopVisit]:
        """A stop that vanished from the remaining list has just been departed."""
        current = {
            update.stop_id: arrival_of(update)
            for update in trip.stop_time_updates
        }
        tracks = {
            update.stop_id: _safe(update, "actual_track")
            for update in trip.stop_time_updates
        }

        visits: list[StopVisit] = []
        for stop_id, predicted_now in tracked.remaining.items():
            if stop_id in current or stop_id in tracked.recorded:
                continue
            visit = self._record(tracked, stop_id, predicted_now, tracks.get(stop_id))
            if visit is not None:
                visits.append(visit)
                tracked.recorded.add(stop_id)
                if visit.actual_arrival is not None:
                    tracked.last_departure = (stop_id, visit.actual_arrival)

        tracked.remaining = current
        return visits

    def _record(
        self,
        tracked: TrackedTrip,
        stop_id: str,
        actual: datetime | None,
        track: str | None,
    ) -> StopVisit | None:
        resolution = tracked.resolution
        scheduled = resolution.scheduled_arrival(stop_id, tracked.service_date)
        predicted = tracked.predictions_at_start.get(stop_id)

        delay_vs_schedule = _delta(actual, scheduled)
        delay_vs_prediction = _delta(actual, predicted)
        runtime_deviation = self._runtime_deviation(tracked, stop_id, actual)

        return StopVisit(
            service_date=tracked.service_date,
            trip_id=tracked.trip_id,
            stop_id=stop_id,
            parent_station=parent_station(stop_id),
            stop_sequence=tracked.sequence_of(stop_id),
            scheduled_arrival=scheduled,
            predicted_arrival=predicted,
            actual_arrival=actual,
            runtime_deviation=runtime_deviation,
            delay_vs_schedule=delay_vs_schedule,
            delay_vs_prediction=delay_vs_prediction,
            actual_track=track,
        )

    @staticmethod
    def _runtime_deviation(
        tracked: TrackedTrip, stop_id: str, actual: datetime | None
    ) -> int | None:
        """How much longer this segment took than the schedule allows for it."""
        if actual is None or tracked.last_departure is None:
            return None
        previous_stop, previous_time = tracked.last_departure
        baseline = tracked.resolution.baseline_runtime(previous_stop, stop_id)
        if baseline is None:
            return None
        observed = (actual - previous_time).total_seconds()
        return int(observed - baseline)

    def _drop_finished(self, active_ids: set[str]) -> None:
        for trip_id in list(self._trips):
            if trip_id not in active_ids:
                del self._trips[trip_id]


def _delta(actual: datetime | None, reference: datetime | None) -> int | None:
    if actual is None or reference is None:
        return None
    return int((actual - reference).total_seconds())


def _safe(obj, attribute: str):
    """Read an optional nyct-gtfs property that raises when the field is absent."""
    try:
        return getattr(obj, attribute)
    except Exception:
        return None
