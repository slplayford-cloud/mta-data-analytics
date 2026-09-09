"""Checks your schema against what the application needs.

These inspect the *live* database, so they only pass once you have written
supabase/migrations/003_redefine_tables.sql and applied it. Until then they skip
with a message rather than failing, so `pytest` stays readable while you work.

They deliberately check structure, not your exact choices. There is more than one
defensible schema here; these assert the properties the rest of the code depends
on, and leave the rest to you.
"""

from __future__ import annotations

import pytest

from src.config import config

pytestmark = pytest.mark.skipif(
    not config.database_url,
    reason="DATABASE_URL not set; see .env.example",
)


def query(sql, params=None):
    from src.queries import run
    return run(sql, params)


@pytest.fixture(scope="module")
def tables() -> set[str]:
    try:
        rows = query("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
    except Exception as exc:
        pytest.skip(f"cannot reach the database: {exc}")
    return {row["table_name"] for row in rows}


def columns_of(table: str) -> dict[str, dict]:
    rows = query("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %(table)s
    """, {"table": table})
    return {row["column_name"]: row for row in rows}


def indexed_columns(table: str) -> str:
    rows = query("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = %(table)s
    """, {"table": table})
    return " ".join(row["indexdef"] for row in rows).lower()


def require(tables: set[str], name: str) -> None:
    if name not in tables:
        pytest.skip(f"{name} not created yet -- see migration 003")


# ── the enum ──────────────────────────────────────────────────────────────────

def test_baseline_source_enum_exists():
    """src/schedule.py returns exactly these four values."""
    rows = query("""
        SELECT e.enumlabel FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'baseline_source'
    """)
    if not rows:
        pytest.skip("baseline_source not created yet -- see migration 003")
    assert {row["enumlabel"] for row in rows} == {
        "timetable", "pattern", "pattern_tail", "none",
    }


# ── tables ────────────────────────────────────────────────────────────────────

def test_stop_patterns_table(tables):
    require(tables, "stop_patterns")
    cols = columns_of("stop_patterns")
    for name in ("route_id", "stop_tuple", "runtimes", "from_static"):
        assert name in cols, f"stop_patterns needs a {name} column"


def test_trips_table(tables):
    require(tables, "trips")
    cols = columns_of("trips")
    for name in ("trip_id", "service_date", "route_id", "baseline"):
        assert name in cols, f"trips needs a {name} column"


def test_trips_is_unique_per_service_date(tables):
    """trip_id is reused across days, so it cannot be the key on its own.

    If this fails, Tuesday's trip silently overwrites Monday's.
    """
    require(tables, "trips")
    rows = query("""
        SELECT c.conkey, c.contype FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'trips' AND c.contype IN ('p', 'u')
    """)
    assert rows, "trips has no primary key or unique constraint"

    key_columns = query("""
        SELECT a.attname FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
        WHERE t.relname = 'trips' AND c.contype IN ('p', 'u')
    """)
    names = {row["attname"] for row in key_columns}
    assert "service_date" in names and "trip_id" in names, (
        f"the key must include both service_date and trip_id; got {names}"
    )


def test_stop_visits_table(tables):
    require(tables, "stop_visits")
    cols = columns_of("stop_visits")
    for name in ("service_date", "trip_id", "stop_id", "actual_arrival",
                 "runtime_deviation", "delay_vs_schedule", "delay_vs_prediction"):
        assert name in cols, f"stop_visits needs a {name} column"


def test_stop_visits_delay_columns_are_nullable(tables):
    """About 28% of trips have no timetable, so absolute delay must be NULLable.

    A NOT NULL here would force the ingestion code to write 0, which reads as
    "on time" and is the exact confusion this project exists to avoid.
    """
    require(tables, "stop_visits")
    cols = columns_of("stop_visits")
    for name in ("delay_vs_schedule", "runtime_deviation", "delay_vs_prediction"):
        if name in cols:
            assert cols[name]["is_nullable"] == "YES", f"{name} must allow NULL"


def test_stop_visits_supports_station_lookups(tables):
    """The most common query filters by station and date range."""
    require(tables, "stop_visits")
    definitions = indexed_columns("stop_visits")
    assert "parent_station" in definitions or "stop_id" in definitions, (
        "no index supports looking up visits by station -- run the queries in "
        "docs/03-queries.md through explain() and see what the planner does"
    )


def test_current_trains_table(tables):
    require(tables, "current_trains")
    cols = columns_of("current_trains")
    for name in ("trip_id", "route_id", "loc_station", "updated_at"):
        assert name in cols, f"current_trains needs a {name} column"


# ── access control ────────────────────────────────────────────────────────────

def test_rls_enabled(tables):
    """Supabase exposes public tables over PostgREST using a browser-side key."""
    expected = {"stop_patterns", "trips", "stop_visits", "current_trains"}
    present = expected & tables
    if not present:
        pytest.skip("tables not created yet -- see migration 003")

    rows = query("""
        SELECT c.relname, c.relrowsecurity FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = ANY(%(names)s)
    """, {"names": list(present)})
    unprotected = [row["relname"] for row in rows if not row["relrowsecurity"]]
    assert not unprotected, (
        f"row-level security is off for {unprotected}; anyone with the anon key "
        f"can read them"
    )
