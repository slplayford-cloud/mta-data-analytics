"""Tests for departure detection.

The feed never announces a departure. It publishes the stops a train has left to
make, and a stop dropping out of that list between polls is the only evidence
the train has left it. These tests drive that inference with stub feed objects,
so they need neither the network nor the GTFS bundle.
"""

from datetime import date, datetime

from src.ingestion import Ingestor, parent_station, stu_time
from src.schedule import Pattern, Resolution, ScheduledStop

SERVICE_DATE = date(2026, 9, 9)


class FakeUpdate:
    def __init__(self, stop_id, arrival, track=None):
        self.stop_id = stop_id
        self.arrival = arrival
        self.departure = None
        self.actual_track = track


class FakeTrip:
    def __init__(self, trip_id, route_id, updates):
        self.trip_id = trip_id
        self.route_id = route_id
        self.stop_time_updates = updates
        self.direction = "S"
        self.headsign_text = "Test"
        self.shape_id = "1..S03R"
        self.nyc_train_id = "01 1234+ TST"


class FakeSchedule:
    """Stands in for ScheduleIndex, returning a fixed resolution."""

    def __init__(self, resolution):
        self._resolution = resolution

    def resolve(self, route_id, trip_id, observed_stops, service_date):
        return self._resolution


def timetable_resolution():
    """Tier 1: absolute scheduled times, 120s between each stop."""
    return Resolution(
        tier="timetable",
        stops=(
            ScheduledStop("101S", 1, 36000),   # 10:00:00
            ScheduledStop("103S", 2, 36120),   # 10:02:00
            ScheduledStop("104S", 3, 36240),   # 10:04:00
        ),
        pattern=None,
    )


def pattern_resolution():
    """Tier 2: runtimes only, no absolute clock times."""
    pattern = Pattern(
        route_id="1",
        stop_tuple=("101S", "103S", "104S"),
        runtimes=(120, 120),
        trip_count=10,
    )
    return Resolution(tier="pattern",
                      stops=tuple(ScheduledStop(s, i + 1, None)
                                  for i, s in enumerate(pattern.stop_tuple)),
                      pattern=pattern)


# ── helpers ───────────────────────────────────────────────────────────────────

def test_parent_station_strips_direction_suffix():
    assert parent_station("109N") == "109"
    assert parent_station("109S") == "109"


def test_parent_station_leaves_bare_ids_alone():
    assert parent_station("109") == "109"


def test_stu_time_rejects_the_epoch_placeholder():
    """nyct-gtfs returns 1970 for unset protobuf timestamps, not None."""
    assert stu_time(datetime(1970, 1, 1)) is None
    assert stu_time(None) is None
    real = datetime(2026, 9, 9, 10, 0)
    assert stu_time(real) == real


# ── departure detection ───────────────────────────────────────────────────────

def test_first_sighting_produces_a_trip_and_no_departures():
    ingestor = Ingestor(FakeSchedule(timetable_resolution()))
    trip = FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])

    new_trips, visits = ingestor.observe([trip], SERVICE_DATE)

    assert [t.trip_id for t in new_trips] == ["t1"]
    assert visits == []


def test_a_stop_dropping_out_is_recorded_as_a_departure():
    ingestor = Ingestor(FakeSchedule(timetable_resolution()))
    first = FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])
    ingestor.observe([first], SERVICE_DATE)

    # 101S is gone: the train has left it.
    second = FakeTrip("t1", "1", [FakeUpdate("103S", datetime(2026, 9, 9, 10, 2))])
    _, visits = ingestor.observe([second], SERVICE_DATE)

    assert len(visits) == 1
    assert visits[0].stop_id == "101S"
    assert visits[0].parent_station == "101"


def test_on_time_arrival_has_zero_delay_against_the_timetable():
    ingestor = Ingestor(FakeSchedule(timetable_resolution()))
    on_time = datetime(2026, 9, 9, 10, 0)
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", on_time), FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])], SERVICE_DATE)

    _, visits = ingestor.observe(
        [FakeTrip("t1", "1", [FakeUpdate("103S", datetime(2026, 9, 9, 10, 2))])],
        SERVICE_DATE,
    )
    assert visits[0].delay_vs_schedule == 0


def test_late_arrival_reports_positive_delay():
    ingestor = Ingestor(FakeSchedule(timetable_resolution()))
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 1, 30)),   # 90s late
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])], SERVICE_DATE)

    _, visits = ingestor.observe(
        [FakeTrip("t1", "1", [FakeUpdate("103S", datetime(2026, 9, 9, 10, 2))])],
        SERVICE_DATE,
    )
    assert visits[0].delay_vs_schedule == 90


def test_pattern_matched_trips_have_no_absolute_delay():
    """Tier 2 has no scheduled clock time, so lateness is undefined -- not zero."""
    ingestor = Ingestor(FakeSchedule(pattern_resolution()))
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])], SERVICE_DATE)

    _, visits = ingestor.observe(
        [FakeTrip("t1", "1", [FakeUpdate("103S", datetime(2026, 9, 9, 10, 2))])],
        SERVICE_DATE,
    )
    assert visits[0].delay_vs_schedule is None


def test_runtime_deviation_measures_the_segment_against_the_baseline():
    """Second departure runs the 120s segment in 180s: 60s slower than scheduled."""
    ingestor = Ingestor(FakeSchedule(pattern_resolution()))
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 3)),
        FakeUpdate("104S", datetime(2026, 9, 9, 10, 5)),
    ])], SERVICE_DATE)

    # Leaves 101S at 10:00.
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 3)),
        FakeUpdate("104S", datetime(2026, 9, 9, 10, 5)),
    ])], SERVICE_DATE)

    # Leaves 103S at 10:03 — 180s for a segment budgeted at 120s.
    _, visits = ingestor.observe(
        [FakeTrip("t1", "1", [FakeUpdate("104S", datetime(2026, 9, 9, 10, 5))])],
        SERVICE_DATE,
    )
    assert visits[0].stop_id == "103S"
    assert visits[0].runtime_deviation == 60


def test_first_departure_has_no_runtime_deviation():
    """There is no previous stop to measure a segment from."""
    ingestor = Ingestor(FakeSchedule(pattern_resolution()))
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])], SERVICE_DATE)

    _, visits = ingestor.observe(
        [FakeTrip("t1", "1", [FakeUpdate("103S", datetime(2026, 9, 9, 10, 2))])],
        SERVICE_DATE,
    )
    assert visits[0].runtime_deviation is None


def test_a_stop_is_never_recorded_twice():
    ingestor = Ingestor(FakeSchedule(timetable_resolution()))
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])], SERVICE_DATE)

    remaining = [FakeTrip("t1", "1", [FakeUpdate("103S", datetime(2026, 9, 9, 10, 2))])]
    _, first = ingestor.observe(remaining, SERVICE_DATE)
    _, second = ingestor.observe(remaining, SERVICE_DATE)

    assert len(first) == 1
    assert second == []


def test_trips_leaving_the_feed_are_forgotten():
    ingestor = Ingestor(FakeSchedule(timetable_resolution()))
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),
    ])], SERVICE_DATE)
    assert "t1" in ingestor.tracked

    ingestor.observe([], SERVICE_DATE)
    assert ingestor.tracked == {}


def test_forecast_error_is_tracked_separately_from_lateness():
    """delay_vs_prediction measures the MTA's own forecast drift, not the train."""
    ingestor = Ingestor(FakeSchedule(timetable_resolution()))
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0)),       # predicted 10:00
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])], SERVICE_DATE)

    # Reappears predicting 10:00:30, then departs.
    ingestor.observe([FakeTrip("t1", "1", [
        FakeUpdate("101S", datetime(2026, 9, 9, 10, 0, 30)),
        FakeUpdate("103S", datetime(2026, 9, 9, 10, 2)),
    ])], SERVICE_DATE)
    _, visits = ingestor.observe(
        [FakeTrip("t1", "1", [FakeUpdate("103S", datetime(2026, 9, 9, 10, 2))])],
        SERVICE_DATE,
    )
    assert visits[0].delay_vs_prediction == 30
