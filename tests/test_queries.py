"""Specifies the shape of each historical query.

Like test_schema.py these run against the live database and skip when it is not
reachable or when a query still returns the empty placeholder.

They check the *contract* -- what columns come back, what ordering, what
invariants hold -- not your SQL. How you get there is the exercise.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import queries
from src.config import config

pytestmark = pytest.mark.skipif(
    not config.database_url,
    reason="DATABASE_URL not set; see .env.example",
)

# A window wide enough to contain data once ingestion is running.
END = date.today()
START = END - timedelta(days=7)


def skip_if_unimplemented(result, name: str):
    """A still-stubbed query returns the empty placeholder."""
    if result in ([], {}):
        pytest.skip(f"{name} not implemented yet -- see src/queries.py")
    return result


@pytest.fixture(scope="module")
def has_data() -> bool:
    try:
        rows = queries.run("SELECT count(*) AS n FROM stop_visits")
    except Exception as exc:
        pytest.skip(f"stop_visits not queryable: {exc}")
    if not rows[0]["n"]:
        pytest.skip("stop_visits is empty -- run the server to collect some")
    return True


def busiest_station() -> str:
    rows = queries.run("""
        SELECT parent_station FROM stop_visits
        GROUP BY parent_station ORDER BY count(*) DESC LIMIT 1
    """)
    return rows[0]["parent_station"]


# ── station_delays ────────────────────────────────────────────────────────────

def test_station_delays_shape(has_data):
    rows = skip_if_unimplemented(
        queries.station_delays(busiest_station(), START, END), "station_delays")
    row = rows[0]
    for key in ("date", "departures", "mean", "median", "p90"):
        assert key in row, f"station_delays rows need a {key!r} key; got {sorted(row)}"


def test_station_delays_is_one_row_per_day(has_data):
    rows = skip_if_unimplemented(
        queries.station_delays(busiest_station(), START, END), "station_delays")
    dates = [row["date"] for row in rows]
    assert len(dates) == len(set(dates)), "expected one row per day, found duplicates"
    assert dates == sorted(dates), "expected rows ordered by date"


def test_station_delays_respects_the_range(has_data):
    rows = skip_if_unimplemented(
        queries.station_delays(busiest_station(), START, END), "station_delays")
    assert all(START <= row["date"] <= END for row in rows), (
        "returned a day outside the requested range -- check your BETWEEN bounds"
    )


# ── worst_stations ────────────────────────────────────────────────────────────

def test_worst_stations_shape(has_data):
    rows = skip_if_unimplemented(
        queries.worst_stations(START, END, limit=10), "worst_stations")
    for key in ("station_id", "departures", "mean"):
        assert key in rows[0], f"worst_stations rows need {key!r}; got {sorted(rows[0])}"


def test_worst_stations_respects_limit(has_data):
    rows = skip_if_unimplemented(
        queries.worst_stations(START, END, limit=5), "worst_stations")
    assert len(rows) <= 5


def test_worst_stations_is_ordered_worst_first(has_data):
    rows = skip_if_unimplemented(
        queries.worst_stations(START, END, limit=10), "worst_stations")
    means = [row["mean"] for row in rows]
    assert means == sorted(means, reverse=True), "expected worst (largest) first"


def test_worst_stations_excludes_thin_samples(has_data):
    """One bad departure at a quiet station is noise, not a finding."""
    rows = skip_if_unimplemented(
        queries.worst_stations(START, END, limit=20), "worst_stations")
    assert all(row["departures"] >= 10 for row in rows), (
        "a station with under 10 observed departures reached the ranking; add a "
        "minimum-sample threshold with HAVING"
    )


# ── route_delays_by_hour ──────────────────────────────────────────────────────

def test_route_delays_by_hour_covers_all_hours(has_data):
    rows = skip_if_unimplemented(
        queries.route_delays_by_hour("1", START, END), "route_delays_by_hour")
    hours = sorted(row["hour"] for row in rows)
    assert hours == list(range(24)), (
        f"expected all 24 hours present, got {hours} -- hours with no service "
        f"should still appear"
    )


def test_route_delays_by_hour_shape(has_data):
    rows = skip_if_unimplemented(
        queries.route_delays_by_hour("1", START, END), "route_delays_by_hour")
    for key in ("hour", "departures", "mean"):
        assert key in rows[0], f"rows need {key!r}; got {sorted(rows[0])}"


# ── trip_detail ───────────────────────────────────────────────────────────────

def test_trip_detail_returns_ordered_stops(has_data):
    sample = queries.run("""
        SELECT service_date, trip_id FROM stop_visits
        GROUP BY service_date, trip_id HAVING count(*) > 3 LIMIT 1
    """)
    if not sample:
        pytest.skip("no trip with enough recorded stops yet")

    result = skip_if_unimplemented(
        queries.trip_detail(sample[0]["service_date"], sample[0]["trip_id"]),
        "trip_detail")

    assert "stops" in result, f"expected a 'stops' key; got {sorted(result)}"
    sequences = [stop["stop_sequence"] for stop in result["stops"]]
    assert sequences == sorted(sequences), "stops must come back in route order"


def test_trip_detail_includes_the_baseline_tier(has_data):
    sample = queries.run("""
        SELECT service_date, trip_id FROM stop_visits LIMIT 1
    """)
    if not sample:
        pytest.skip("no stop visits yet")
    result = skip_if_unimplemented(
        queries.trip_detail(sample[0]["service_date"], sample[0]["trip_id"]),
        "trip_detail")
    assert "baseline" in result, (
        "trip_detail must report which tier resolved the trip -- without it a "
        "reader cannot tell whether delay_vs_schedule means anything"
    )


# ── daily_summary ─────────────────────────────────────────────────────────────

def test_daily_summary_shape(has_data):
    rows = skip_if_unimplemented(queries.daily_summary(END), "daily_summary")
    for key in ("route_id", "trips", "departures", "mean"):
        assert key in rows[0], f"rows need {key!r}; got {sorted(rows[0])}"


def test_daily_summary_is_one_row_per_route(has_data):
    rows = skip_if_unimplemented(queries.daily_summary(END), "daily_summary")
    routes = [row["route_id"] for row in rows]
    assert len(routes) == len(set(routes)), "expected one row per route"


def test_daily_summary_tiers_sum_to_one(has_data):
    """The baseline breakdown is a share of trips, so it must total 1.0.

    Getting this wrong usually means counting stop_visits rows where you meant to
    count trips -- the two are different grains and mixing them is a classic
    analytics bug.
    """
    rows = skip_if_unimplemented(queries.daily_summary(END), "daily_summary")
    tier_keys = [k for k in rows[0] if k.startswith("pct_")]
    if not tier_keys:
        pytest.skip("no per-tier share columns yet; name them pct_<tier>")

    for row in rows:
        total = sum(row[key] or 0 for key in tier_keys)
        assert abs(total - 1.0) < 0.01, (
            f"route {row['route_id']}: tier shares sum to {total:.3f}, not 1.0"
        )
