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

    THIS CLASS IS YOURS TO WRITE. Read docs/02-ingestion.md first.

    Holds no database handle by design: `observe()` takes what the feed said and
    returns what should be written, so it can be tested without a database and
    the caller decides what persistence means.

    The dataclasses above define the shape of the answer. The tests in
    tests/test_ingestion.py define its behaviour. Between them they specify this
    class completely -- what is missing is how.
    """

    def __init__(self, schedule: ScheduleResolver) -> None:
        self._schedule = schedule
        # TODO(you): Decide what state this class carries between polls.
        #   Why: A departure is only visible as a *difference* between two polls,
        #        so something has to remember the previous one. What you keep
        #        here determines what you can detect and how much memory ~800
        #        live trains cost you.
        #   Hint: TrackedTrip already models one trip's memory. You need a way to
        #         find one by trip_id on the next cycle.
        #   Verify: pytest tests/test_ingestion.py::test_trips_leaving_the_feed_are_forgotten
        self._trips: dict[str, TrackedTrip] = {}

    @property
    def tracked(self) -> dict[str, TrackedTrip]:
        """Live trips, by trip_id. Read by the poller to build map positions."""
        return self._trips

    def observe(
        self, trips: Sequence[FeedTrip], service_date: date
    ) -> tuple[list[TrackedTrip], list[StopVisit]]:
        """Fold one poll's worth of feed data in.

        Returns (trips seen for the first time, departures detected since the
        previous call). Returning rather than writing keeps this testable and
        lets the caller batch.
        """
        # TODO(you): Implement the poll cycle.
        #   Why: This is the top of the pipeline. Every cycle you must decide,
        #        for each trip in the feed, whether it is new (resolve its
        #        schedule, start tracking it) or known (diff it against last
        #        time), and separately notice trips that have vanished.
        #   Hint: Three distinct jobs, and order matters -- a trip that vanished
        #         cannot be diffed. Work out what must happen before what.
        #   Verify: pytest tests/test_ingestion.py::test_first_sighting_produces_a_trip_and_no_departures
        return [], []

    def _begin(self, trip: FeedTrip, service_date: date) -> TrackedTrip:
        """Start tracking a trip: resolve its baseline and snapshot predictions."""
        # TODO(you): Build the TrackedTrip for a newly seen trip.
        #   Why: Two things can only be captured now and never again. The
        #        schedule resolution (self._schedule.resolve) decides which of
        #        the three delay metrics are even definable for this trip. And
        #        the feed's current predictions are the baseline for measuring
        #        the MTA's own forecast error later -- capture them after the
        #        train has started moving and they are already contaminated.
        #   Hint: src/schedule.py is written for you. Look at what resolve()
        #         wants and what Resolution gives back.
        #   Verify: pytest tests/test_ingestion.py::test_forecast_error_is_tracked_separately_from_lateness
        raise NotImplementedError

    def _detect_departures(
        self, trip: FeedTrip, tracked: TrackedTrip
    ) -> list[StopVisit]:
        """Find stops the train has left since the previous poll."""
        # TODO(you): Diff this poll's remaining stops against the last one.
        #   Why: The feed never says "departed". It publishes the stops a train
        #        has left to make, and that list shrinks. A stop present last
        #        cycle and absent now is the only departure signal that exists.
        #        The last arrival time predicted for that stop before it vanished
        #        is the closest thing to an observed arrival you will get.
        #   Hint: Do not forget to update the remembered state afterwards, or you
        #         will re-detect the same departure forever. There is also a
        #         reason TrackedTrip has both `remaining` and `recorded` -- work
        #         out what each protects against.
        #   Verify: pytest tests/test_ingestion.py::test_a_stop_dropping_out_is_recorded_as_a_departure
        #           pytest tests/test_ingestion.py::test_a_stop_is_never_recorded_twice
        return []

    def _record(
        self,
        tracked: TrackedTrip,
        stop_id: str,
        actual: datetime | None,
        track: str | None,
    ) -> StopVisit | None:
        """Build the StopVisit for one departure, with all three metrics."""
        # TODO(you): Compute the three delay metrics and assemble a StopVisit.
        #   Why: This is the heart of the whole project. One number cannot be
        #        honest for every trip, because ~28% of trips have no scheduled
        #        clock time at all. delay_vs_schedule must be NULL for those --
        #        not zero, which would silently claim they ran on time.
        #   Hint: Resolution.has_absolute_times tells you which case you are in,
        #         and Resolution.scheduled_arrival() returns None when it does
        #         not apply. Let those do the deciding rather than branching on
        #         the tier string yourself.
        #   Verify: pytest tests/test_ingestion.py::test_late_arrival_reports_positive_delay
        #           pytest tests/test_ingestion.py::test_pattern_matched_trips_have_no_absolute_delay
        return None

    @staticmethod
    def _runtime_deviation(
        tracked: TrackedTrip, stop_id: str, actual: datetime | None
    ) -> int | None:
        """How much longer this segment took than the schedule allows for it."""
        # TODO(you): Measure the segment just travelled against its baseline.
        #   Why: This is the metric that works for every trip, including the ones
        #        with no timetable, because it is relative. A train can be 10
        #        minutes late overall while running this particular segment
        #        exactly on time -- those are different facts and this separates
        #        them.
        #   Hint: A segment needs two endpoints. Where does the previous one come
        #         from, and what should this return on a trip's very first
        #         observed departure? Resolution.baseline_runtime() gives you the
        #         expected seconds for a pair of stops.
        #   Verify: pytest tests/test_ingestion.py::test_runtime_deviation_measures_the_segment_against_the_baseline
        #           pytest tests/test_ingestion.py::test_first_departure_has_no_runtime_deviation
        return None

    def _drop_finished(self, active_ids: set[str]) -> None:
        """Forget trips that have left the feed."""
        # TODO(you): Remove tracking state for trips no longer in the feed.
        #   Why: Without this the process grows forever -- roughly 7,000 trips a
        #        day, each holding its full stop list. This is the difference
        #        between a service that runs for months and one that gets
        #        OOM-killed on day three.
        #   Hint: Mutating a dict while iterating it raises. There is a standard
        #         way around that.
        #   Verify: pytest tests/test_ingestion.py::test_trips_leaving_the_feed_are_forgotten


def _delta(actual: datetime | None, reference: datetime | None) -> int | None:
    """Seconds between two times, or None if either is unknown.

    Given to you: every delay metric is a subtraction where either side may be
    missing, and returning None rather than 0 for "unknown" is the distinction
    this project exists to preserve.
    """
    if actual is None or reference is None:
        return None
    return int((actual - reference).total_seconds())


def _safe(obj, attribute: str):
    """Read an optional nyct-gtfs property that raises when the field is absent."""
    try:
        return getattr(obj, attribute)
    except Exception:
        return None
