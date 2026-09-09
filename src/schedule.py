#!/usr/bin/env python3
"""
Resolve a realtime trip to a scheduled baseline.

The realtime feed names a trip like "055250_1..N15R". Static GTFS names the same
trip "ASP26GEN-1039-Weekday-00_055250_1..N15R" -- the realtime id is a suffix.
When that lookup succeeds we have the published timetable and can measure
absolute lateness.

It succeeds for roughly three quarters of trips. The rest are extra trains,
diversions around planned work, and lines whose realtime ids carry less
information than the static ones. For those we fall back to matching the trip's
*stop pattern* against the catalog of scheduled patterns, which gives inter-stop
runtimes even when there is no scheduled clock time to compare against.

Every resolution reports which tier produced it, so nothing downstream can
confuse "six minutes late" with "six minutes slower than this route usually is".
"""

from __future__ import annotations

import csv
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

# Realtime emits SIR path codes with one dot ("SI.N03R"); static uses two
# ("SI..N03R"). Normalising recovers the whole line, which otherwise never matches.
_SIR_SINGLE_DOT = re.compile(r"^(SI)\.(?!\.)")

# GTFS exception_type values in calendar_dates.txt
_SERVICE_ADDED = "1"
_SERVICE_REMOVED = "2"

_DOW_COLUMNS = ("monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday")


def normalise_trip_id(trip_id: str) -> str:
    """Make a realtime trip id comparable with the static suffix."""
    origin, _, path = trip_id.partition("_")
    if not path:
        return trip_id
    return f"{origin}_{_SIR_SINGLE_DOT.sub(r'\1..', path)}"


def gtfs_seconds(value: str) -> int:
    """Seconds past midnight. GTFS allows >24h for trips crossing midnight."""
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def absolute_time(service_date: date, seconds: int) -> datetime:
    """Turn seconds-past-midnight into a wall-clock time on the service date."""
    return datetime.combine(service_date, datetime.min.time()) + timedelta(seconds=seconds)


@dataclass(frozen=True)
class ScheduledStop:
    stop_id: str
    sequence: int
    # Seconds past midnight on the service date. None when the baseline came
    # from a pattern match, which carries runtimes but no absolute times.
    arrival_seconds: int | None


@dataclass(frozen=True)
class Pattern:
    route_id: str
    stop_tuple: tuple[str, ...]
    # Median observed-in-schedule seconds between consecutive stops.
    # len(runtimes) == len(stop_tuple) - 1
    runtimes: tuple[int, ...]
    trip_count: int


@dataclass(frozen=True)
class Resolution:
    """How a realtime trip was matched to a schedule."""

    tier: str                       # timetable | pattern | pattern_tail | none
    stops: tuple[ScheduledStop, ...]
    pattern: Pattern | None

    @property
    def has_absolute_times(self) -> bool:
        """True only for tier 1 -- the only tier where lateness is well defined."""
        return self.tier == "timetable"

    @property
    def is_supplemental(self) -> bool:
        """A known pattern running at a time the timetable does not schedule."""
        return self.tier in ("pattern", "pattern_tail")

    def scheduled_arrival(self, stop_id: str, service_date: date) -> datetime | None:
        if not self.has_absolute_times:
            return None
        for stop in self.stops:
            if stop.stop_id == stop_id and stop.arrival_seconds is not None:
                return absolute_time(service_date, stop.arrival_seconds)
        return None

    def baseline_runtime(self, from_stop: str, to_stop: str) -> int | None:
        """Expected seconds between two consecutive stops, from whichever tier matched."""
        if self.pattern is not None:
            stops = self.pattern.stop_tuple
            for i in range(len(stops) - 1):
                if stops[i] == from_stop and stops[i + 1] == to_stop:
                    return self.pattern.runtimes[i]
            return None

        times = {s.stop_id: s.arrival_seconds for s in self.stops}
        start, end = times.get(from_stop), times.get(to_stop)
        if start is None or end is None:
            return None
        return end - start


class ScheduleIndex:
    """Static GTFS, indexed for realtime lookup. Built once at startup."""

    def __init__(self, static_dir: Path) -> None:
        self._dir = static_dir

        # realtime trip id -> {service_id: (ScheduledStop, ...)}
        self._timetable: dict[str, dict[str, tuple[ScheduledStop, ...]]] = {}
        # service_id -> (weekday mask, start, end)
        self._calendar: dict[str, tuple[tuple[bool, ...], date, date]] = {}
        # (service_id, date) -> added/removed
        self._exceptions: dict[tuple[str, date], str] = {}
        # (route_id, stop_tuple) -> Pattern
        self._patterns: dict[tuple[str, tuple[str, ...]], Pattern] = {}
        # (route_id, trailing stop_tuple) -> Pattern, for trains already underway
        self._suffixes: dict[tuple[str, tuple[str, ...]], Pattern] = {}

    # ── build ────────────────────────────────────────────────────────────────

    def build(self) -> None:
        self._load_calendar()
        trips = self._load_trips()
        trip_stops = self._load_stop_times()
        self._build_timetable(trips, trip_stops)
        self._build_patterns(trips, trip_stops)
        print(f"[schedule] {len(self._timetable)} timetabled trips, "
              f"{len(self._patterns)} patterns, {len(self._suffixes)} suffixes")

    def _load_calendar(self) -> None:
        with open(self._dir / "calendar.txt", newline="") as f:
            for row in csv.DictReader(f):
                mask = tuple(row[day] == "1" for day in _DOW_COLUMNS)
                self._calendar[row["service_id"]] = (
                    mask,
                    datetime.strptime(row["start_date"], "%Y%m%d").date(),
                    datetime.strptime(row["end_date"], "%Y%m%d").date(),
                )

        # Holidays: Labor Day adds Sunday service and removes Weekday service.
        path = self._dir / "calendar_dates.txt"
        if not path.exists():
            return
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                day = datetime.strptime(row["date"], "%Y%m%d").date()
                self._exceptions[(row["service_id"], day)] = row["exception_type"]

    def _load_trips(self) -> dict[str, tuple[str, str]]:
        """static trip_id -> (route_id, service_id)"""
        trips: dict[str, tuple[str, str]] = {}
        with open(self._dir / "trips.txt", newline="") as f:
            for row in csv.DictReader(f):
                trips[row["trip_id"]] = (row["route_id"], row["service_id"])
        return trips

    def _load_stop_times(self) -> dict[str, list[tuple[int, str, int]]]:
        """static trip_id -> sorted [(sequence, stop_id, arrival_seconds)]"""
        stops: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
        with open(self._dir / "stop_times.txt", newline="") as f:
            for row in csv.DictReader(f):
                stops[row["trip_id"]].append((
                    int(row["stop_sequence"]),
                    row["stop_id"],
                    gtfs_seconds(row["arrival_time"]),
                ))
        for sequence in stops.values():
            sequence.sort()
        return stops

    def _build_timetable(
        self,
        trips: dict[str, tuple[str, str]],
        trip_stops: dict[str, list[tuple[int, str, int]]],
    ) -> None:
        for static_id, (_, service_id) in trips.items():
            _, _, rt_id = static_id.partition("_")
            if not rt_id:
                continue
            scheduled = tuple(
                ScheduledStop(stop_id, sequence, arrival)
                for sequence, stop_id, arrival in trip_stops.get(static_id, ())
            )
            if scheduled:
                self._timetable.setdefault(rt_id, {})[service_id] = scheduled

    def _build_patterns(
        self,
        trips: dict[str, tuple[str, str]],
        trip_stops: dict[str, list[tuple[int, str, int]]],
    ) -> None:
        # Gather every inter-stop runtime the schedule contains for each pattern,
        # then take the median so a single odd trip cannot skew the baseline.
        gaps: dict[tuple[str, tuple[str, ...]], list[list[int]]] = defaultdict(list)
        for static_id, (route_id, _) in trips.items():
            sequence = trip_stops.get(static_id)
            if not sequence or len(sequence) < 2:
                continue
            stop_tuple = tuple(stop_id for _, stop_id, _ in sequence)
            times = [arrival for _, _, arrival in sequence]
            gaps[(route_id, stop_tuple)].append(
                [b - a for a, b in zip(times, times[1:])]
            )

        for (route_id, stop_tuple), samples in gaps.items():
            runtimes = tuple(
                int(statistics.median(step)) for step in zip(*samples)
            )
            self._patterns[(route_id, stop_tuple)] = Pattern(
                route_id=route_id,
                stop_tuple=stop_tuple,
                runtimes=runtimes,
                trip_count=len(samples),
            )

        # A train already underway reports only the stops it has left to make,
        # which is a trailing run of its full pattern. Index every such tail.
        # Where two patterns share a tail, the busier one wins.
        for pattern in self._patterns.values():
            for start in range(len(pattern.stop_tuple)):
                key = (pattern.route_id, pattern.stop_tuple[start:])
                existing = self._suffixes.get(key)
                if existing is None or pattern.trip_count > existing.trip_count:
                    self._suffixes[key] = pattern

    # ── lookup ───────────────────────────────────────────────────────────────

    def service_ids_for(self, service_date: date) -> list[str]:
        """Service ids running on a date, most specific first.

        The MTA now ships date-scoped ids alongside the plain ones -- both
        "Sunday" and "Sunday-H-20260908-20261031" can be active at once. Longer
        names are the narrower ones, so try those first.
        """
        weekday = service_date.weekday()
        active: list[str] = []
        for service_id, (mask, start, end) in self._calendar.items():
            exception = self._exceptions.get((service_id, service_date))
            if exception == _SERVICE_REMOVED:
                continue
            runs = start <= service_date <= end and mask[weekday]
            if runs or exception == _SERVICE_ADDED:
                active.append(service_id)
        return sorted(active, key=len, reverse=True)

    def resolve(
        self,
        route_id: str,
        trip_id: str,
        observed_stops: tuple[str, ...],
        service_date: date,
    ) -> Resolution:
        """Match a realtime trip to the best baseline available.

        `observed_stops` is the trip's remaining stop ids, in order, as the feed
        reports them.
        """
        # Tier 1: the trip is in the timetable for a service running today.
        by_service = self._timetable.get(normalise_trip_id(trip_id))
        if by_service:
            for service_id in self.service_ids_for(service_date):
                scheduled = by_service.get(service_id)
                if scheduled:
                    return Resolution("timetable", scheduled, None)

        if not observed_stops:
            return Resolution("none", (), None)

        # Tier 2: an extra train running a scheduled pattern end to end.
        pattern = self._patterns.get((route_id, observed_stops))
        if pattern is not None:
            return Resolution("pattern", self._as_stops(pattern), pattern)

        # Tier 3: already underway, so we see a tail of the pattern.
        pattern = self._suffixes.get((route_id, observed_stops))
        if pattern is not None:
            return Resolution("pattern_tail", self._as_stops(pattern), pattern)

        return Resolution("none", (), None)

    @staticmethod
    def _as_stops(pattern: Pattern) -> tuple[ScheduledStop, ...]:
        # Pattern matches carry no absolute schedule, only relative runtimes.
        return tuple(
            ScheduledStop(stop_id, index + 1, None)
            for index, stop_id in enumerate(pattern.stop_tuple)
        )

    @property
    def patterns(self) -> dict[tuple[str, tuple[str, ...]], Pattern]:
        return self._patterns
