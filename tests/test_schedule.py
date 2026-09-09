"""Tests for the schedule resolver.

The pure functions run anywhere. The tests that need the GTFS bundle skip when
it is absent, since static/ is no longer committed -- run
`python scripts/refresh_gtfs.py` first to exercise them.
"""

from datetime import date

import pytest

from src.config import config
from src.schedule import (
    ScheduleIndex,
    absolute_time,
    gtfs_seconds,
    normalise_trip_id,
)

pytestmark = pytest.mark.filterwarnings("ignore")


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_sir_single_dot_is_normalised():
    """Realtime emits SI.N03R; static has SI..N03R. Without this the line never matches."""
    assert normalise_trip_id("059150_SI.N03R") == "059150_SI..N03R"


def test_existing_double_dot_is_left_alone():
    assert normalise_trip_id("059150_SI..N03R") == "059150_SI..N03R"


def test_other_routes_are_untouched():
    assert normalise_trip_id("055250_1..N15R") == "055250_1..N15R"


def test_trip_id_without_path_is_returned_as_is():
    assert normalise_trip_id("weird") == "weird"


def test_gtfs_seconds_past_midnight():
    assert gtfs_seconds("00:06:00") == 360
    assert gtfs_seconds("12:30:45") == 45045


def test_gtfs_seconds_allows_hours_past_24():
    """GTFS encodes a 00:30 trip on the previous service day as 24:30."""
    assert gtfs_seconds("24:30:00") == 88200


def test_absolute_time_rolls_past_midnight():
    result = absolute_time(date(2026, 9, 9), 88200)
    assert (result.year, result.month, result.day, result.hour) == (2026, 9, 10, 0)


# ── index built from the real bundle ──────────────────────────────────────────

@pytest.fixture(scope="module")
def index() -> ScheduleIndex:
    if not (config.static_dir / "trips.txt").exists():
        pytest.skip("static GTFS bundle not present; run scripts/refresh_gtfs.py")
    built = ScheduleIndex(config.static_dir)
    built.build()
    return built


def test_weekday_resolves_to_weekday_service(index: ScheduleIndex):
    # 2026-09-09 is a Wednesday.
    assert "Weekday" in index.service_ids_for(date(2026, 9, 9))


def test_date_scoped_service_ids_are_most_specific_first(index: ScheduleIndex):
    """The MTA ships "Sunday" and "Sunday-H-<range>" together; prefer the narrower."""
    services = index.service_ids_for(date(2026, 9, 13))   # a Sunday
    assert services, "expected Sunday service"
    assert len(services[0]) >= len(services[-1])
    assert all("Saturday" not in service for service in services)


def test_holiday_exception_removes_weekday_service(index: ScheduleIndex):
    """calendar_dates.txt removes Weekday and adds Sunday on Labor Day."""
    labor_day = date(2026, 9, 7)   # a Monday
    services = index.service_ids_for(labor_day)
    assert "Weekday" not in services
    assert any(service.startswith("Sunday") for service in services)


def test_unknown_trip_with_no_stops_resolves_to_none(index: ScheduleIndex):
    result = index.resolve("1", "999999_nonsense", (), date(2026, 9, 9))
    assert result.tier == "none"
    assert result.pattern is None
    assert not result.has_absolute_times


def test_patterns_have_one_runtime_per_gap(index: ScheduleIndex):
    for pattern in index.patterns.values():
        assert len(pattern.runtimes) == len(pattern.stop_tuple) - 1


def test_full_pattern_resolves_to_tier_two(index: ScheduleIndex):
    """A trip on a scheduled pattern but an unscheduled time is an extra train."""
    pattern = max(index.patterns.values(), key=lambda p: p.trip_count)
    result = index.resolve(
        pattern.route_id, "000000_unscheduled", pattern.stop_tuple, date(2026, 9, 9),
    )
    assert result.tier == "pattern"
    assert result.is_supplemental
    # No absolute schedule exists for an unscheduled departure time.
    assert not result.has_absolute_times


def test_tail_of_pattern_resolves_to_tier_three(index: ScheduleIndex):
    """A train underway reports only its remaining stops."""
    pattern = max(index.patterns.values(), key=lambda p: p.trip_count)
    tail = pattern.stop_tuple[len(pattern.stop_tuple) // 2:]
    result = index.resolve(
        pattern.route_id, "000000_underway", tail, date(2026, 9, 9),
    )
    assert result.tier in ("pattern", "pattern_tail")
    assert result.is_supplemental


def test_baseline_runtime_matches_the_pattern(index: ScheduleIndex):
    pattern = max(index.patterns.values(), key=lambda p: p.trip_count)
    result = index.resolve(
        pattern.route_id, "000000_x", pattern.stop_tuple, date(2026, 9, 9),
    )
    runtime = result.baseline_runtime(pattern.stop_tuple[0], pattern.stop_tuple[1])
    assert runtime == pattern.runtimes[0]


def test_baseline_runtime_is_none_for_unconnected_stops(index: ScheduleIndex):
    pattern = max(index.patterns.values(), key=lambda p: p.trip_count)
    result = index.resolve(
        pattern.route_id, "000000_x", pattern.stop_tuple, date(2026, 9, 9),
    )
    assert result.baseline_runtime("nowhere", "nowhere-else") is None
