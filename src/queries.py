#!/usr/bin/env python3
"""
Historical queries over recorded stop visits.

THIS MODULE IS YOURS TO WRITE. Read docs/03-queries.md first.

Deliberately uses psycopg and raw SQL rather than the Supabase client. The
PostgREST query builder hides the SQL it generates, and the point of this module
is to write a query, run EXPLAIN ANALYZE on it, add an index, and watch the plan
change. You cannot do that through an abstraction that will not show you the SQL.

Every function here takes plain arguments and returns plain dicts, so the HTTP
layer in server.py stays thin and these can be exercised from a REPL.

Connection: DATABASE_URL in .env. Get it from the Supabase dashboard under
Project Settings -> Database -> Connection string (use the pooled connection).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date
from typing import Generator, LiteralString

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import DictRow, dict_row

from src.config import config

log = logging.getLogger(__name__)


# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def connection() -> Generator[psycopg.Connection[DictRow]]:
    """Yield a database connection.

    Given to you, because connection management is plumbing rather than a
    database-design lesson. Note it opens a connection per call, which is fine
    at this scale and would not be in production -- ask yourself what you would
    change if this served a thousand requests a second.
    """
    if not config.database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy it from the Supabase dashboard "
            "(Project Settings -> Database -> Connection string) into .env"
        )

    conn = psycopg.Connection[DictRow].connect(
        config.database_url, row_factory=dict_row,
    )
    try:
        yield conn
    finally:
        conn.close()


def run(query: LiteralString, params: dict | None = None) -> list[DictRow]:
    """Execute a read-only query and return rows as dicts.

    Given to you. Use this from every query function below rather than opening
    your own connection -- it keeps the SQL as the only thing that varies.

    Note the LiteralString type on `query`: psycopg will not accept a string
    built at runtime, so you cannot f-string a user-supplied value into the SQL
    even by accident. Values go in `params` as %(name)s placeholders, and the
    driver sends them separately from the statement. This is the type system
    enforcing the fix for SQL injection rather than trusting you to remember it.
    """
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(query, params or {})
        return list(cursor.fetchall())


def explain(query: LiteralString, params: dict | None = None) -> str:
    """Return the query plan for a statement, as Postgres would run it.

    Given to you, because you will want it constantly. Use it on every query you
    write below, before and after adding an index:

        from src.queries import explain
        print(explain("SELECT ... WHERE parent_station = %(station)s",
                      {"station": "127"}))

    Look for "Seq Scan" on stop_visits -- at 150M rows that is the signal that
    an index is missing or that your WHERE clause cannot use the one you have.
    """
    statement = pgsql.SQL("EXPLAIN (ANALYZE, BUFFERS) ") + pgsql.SQL(query)
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(statement, params or {})
        return "\n".join(row["QUERY PLAN"] for row in cursor.fetchall())


# ── Queries ───────────────────────────────────────────────────────────────────
#
# Five access patterns, roughly in order of difficulty. Each has a test in
# tests/test_queries.py that pins down the shape of the result.
#
# Before writing any of them, read docs/03-queries.md -- it explains what to
# look for in a plan and why the third one is harder than it looks.


def station_delays(station_id: str, start: date, end: date) -> list[dict]:
    """Delay distribution at one station over a date range.

    Returns one row per day: date, number of departures observed, and the mean,
    median and 90th percentile of runtime_deviation.
    """
    # TODO(you): Write this query.
    #   Why: The simplest useful aggregation, and the one that tells you whether
    #        your stop_visits indexes work. It filters on a station and a date
    #        range, which is the most common shape in this whole application.
    #   Hint: Postgres has percentile_cont for the median and p90 -- it is an
    #         ordered-set aggregate, so the syntax is unusual. Look it up rather
    #         than approximating with avg.
    #   Verify: pytest tests/test_queries.py::test_station_delays_shape
    #           then explain() it and check you are not sequential-scanning.
    return []


def worst_stations(start: date, end: date, limit: int = 20) -> list[dict]:
    """Stations with the worst mean runtime_deviation in a window.

    Returns station id, name, departures observed, and mean deviation, worst
    first.
    """
    # TODO(you): Write this query.
    #   Why: Introduces a trap. A station with three observed departures and one
    #        catastrophic delay will top any naive ranking by mean. Real
    #        analytics almost always needs a minimum sample size before a
    #        ranking means anything.
    #   Hint: You will need HAVING, not WHERE, to filter on an aggregate. Pick a
    #         threshold and be able to justify it. Station names live in
    #         stop_info, so this needs a join.
    #   Verify: pytest tests/test_queries.py::test_worst_stations_excludes_thin_samples
    return []


def route_delays_by_hour(route_id: str, start: date, end: date) -> list[dict]:
    """A route's mean delay by hour of day, averaged across the range.

    Returns 24 rows: hour (0-23), departures observed, mean runtime_deviation.
    """
    # TODO(you): Write this query.
    #   Why: Harder than it looks, for two reasons worth understanding.
    #        (a) Extracting hour from a timestamp in a WHERE or GROUP BY usually
    #            defeats an index on that column. Find out why, and what a
    #            functional index would do about it.
    #        (b) Subway service runs past midnight. A train departing at 00:30
    #            belongs to the previous service day -- service_date already
    #            encodes that, but hour-of-day does not. Decide what you want
    #            "hour" to mean here.
    #   Hint: Hours with no service should probably still appear as rows. Look at
    #         generate_series if you want all 24 guaranteed.
    #   Verify: pytest tests/test_queries.py::test_route_delays_by_hour_covers_all_hours
    return []


def trip_detail(service_date: date, trip_id: str) -> dict:
    """Every recorded stop for one trip, in order.

    Returns the trip's metadata (route, baseline tier, whether supplemental) plus
    a list of its stops with all three delay metrics.
    """
    # TODO(you): Write this query.
    #   Why: The first one that joins trips to stop_visits, so it depends on
    #        getting the relationship between those two tables right in your
    #        schema. If the join is awkward, that is your schema telling you
    #        something.
    #   Hint: Two queries and assembling in Python is a perfectly good answer
    #         here. One query returning a nested structure is also possible with
    #         json_agg -- try both and compare the plans and the readability.
    #   Verify: pytest tests/test_queries.py::test_trip_detail_returns_ordered_stops
    return {}


def daily_summary(day: date) -> list[dict]:
    """Per-route summary for one service day.

    Returns one row per route: trips observed, departures recorded, mean
    runtime_deviation, and what fraction of trips resolved to each baseline tier.
    """
    # TODO(you): Write this query.
    #   Why: The hardest of the five, and the one closest to real reporting work.
    #        The baseline breakdown is a pivot -- turning rows into columns -- and
    #        the counts come from trips while the delays come from stop_visits,
    #        which are different grains. Averaging across different grains
    #        incorrectly is one of the most common analytics bugs there is.
    #   Hint: FILTER (WHERE ...) on an aggregate is the clean way to pivot in
    #         Postgres. For the grain problem: work out what you are counting
    #         before you write the join, and consider whether a CTE per grain
    #         makes it clearer than one clever query.
    #   Verify: pytest tests/test_queries.py::test_daily_summary_tiers_sum_to_one
    return []
