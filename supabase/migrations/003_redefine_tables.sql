-- ═══════════════════════════════════════════════════════════════════════════
-- 003 — Redefine the tables around a correct delay baseline
--
-- THIS FILE IS YOURS TO WRITE. See docs/01-schema.md before you start.
--
-- Everything below is context: what each table is for, what the application
-- needs from it, and the decisions worth thinking about. The DDL is not here.
--
-- Run `pytest tests/test_schema.py` to check your work. Those tests inspect
-- the live database, so they only pass once you have applied this migration.
-- ═══════════════════════════════════════════════════════════════════════════


-- ── Why this migration exists ────────────────────────────────────────────────
--
-- The old stop_visits measured delay against a snapshot of the *realtime
-- prediction* taken when a trip first appeared. That answers "how far did the
-- MTA's guess move?", not "was the train late?". The two are different numbers
-- and the old schema had no way to tell them apart.
--
-- The rewrite records three metrics per departure and, critically, records
-- which baseline produced them. Roughly a quarter of live trips cannot be
-- matched to the published timetable at all (extra trains, diversions around
-- planned work), so for those, absolute lateness is undefined — not zero.
--
-- The schema has to make that distinction impossible to lose.


-- ── Preserve, don't destroy ──────────────────────────────────────────────────
-- These two lines are done for you. Your 373k existing rows are renamed, not
-- dropped: their delay column used a different baseline and is not comparable
-- to the new one, but the raw arrival times are still real observations.

ALTER TABLE IF EXISTS public.stop_visits     RENAME TO stop_visits_legacy;
ALTER TABLE IF EXISTS public.trip_schedules  RENAME TO trip_schedules_legacy;


-- ── 1. baseline_source ───────────────────────────────────────────────────────
--
-- How a trip's scheduled baseline was resolved. src/schedule.py returns exactly
-- one of these four strings on every resolution:
--
--   timetable     exact trip_id match against static GTFS   (~72% of trips)
--   pattern       same route, identical stop sequence        (~8%)
--   pattern_tail  remaining stops are a tail of a pattern    (~14%)
--   none          no baseline available                      (~6%)
--
-- TODO(you): Create a type for this.
--   Why: You could store it as TEXT. An ENUM costs 4 bytes instead of a
--        variable-length string on every one of ~400k rows/day, and makes an
--        invalid value a write error instead of a silent data-quality problem
--        you discover months later in a GROUP BY.
--   Hint: Postgres CREATE TYPE ... AS ENUM. Wrap it so re-running this file
--         does not error — look at how a DO block catches duplicate_object.
--   Verify: pytest tests/test_schema.py::test_baseline_source_enum_exists


-- ── 2. stop_patterns ─────────────────────────────────────────────────────────
--
-- The catalog of distinct stop sequences. Seeded from static GTFS at startup
-- (218 patterns across 20k scheduled trips), and extended when the realtime
-- feed shows a pattern the static bundle never described.
--
-- What the application needs to store:
--   route_id      the route this pattern belongs to
--   direction     N or S
--   stop_tuple    the ordered list of stop ids that defines the pattern
--   runtimes      baseline seconds between each consecutive pair of stops
--   from_static   did this come from GTFS, or did we learn it from live data?
--   times_seen    how often we have observed it
--   first_seen    when we first saw it
--
-- TODO(you): Write the CREATE TABLE.
--   Why: Two questions here are real design decisions, not typing.
--        (a) stop_tuple is an ordered list. Postgres offers TEXT[], JSONB, or a
--            child table with one row per stop. Each has different tradeoffs for
--            equality lookup, storage, and whether you can index it.
--        (b) runtimes always has exactly len(stop_tuple) - 1 entries. Can the
--            schema enforce that, and should it?
--   Hint: The hot path is "given a route and an exact stop sequence, find the
--         pattern". Whatever you choose must make that lookup fast. Also decide
--         between a natural key (route + stops) and a surrogate id — stop_visits
--         will reference this table.
--   Verify: pytest tests/test_schema.py::test_stop_patterns_table


-- ── 3. trips ─────────────────────────────────────────────────────────────────
--
-- One row per trip observed, written the first time we see it. Roughly 7,000
-- rows per day.
--
-- What the application needs to store:
--   trip_id, service_date, route_id, direction, headsign, shape_id
--   nyc_train_id      the physical train, stable across trips it operates
--   baseline          which tier resolved this trip
--   pattern_id        which pattern it matched, if any
--   is_supplemental   an extra train, not a scheduled one
--   scheduled_stops   the resolved stop list with scheduled times
--   first_seen_at, completed_at
--
-- TODO(you): Write the CREATE TABLE and its indexes.
--   Why: trip_id alone is NOT unique — the MTA reuses ids across service days,
--        so the same trip_id legitimately appears on Monday and Tuesday. Getting
--        this wrong means one day silently overwriting another.
--   Hint: Think about what uniquely identifies a trip, then about which columns
--         appear in WHERE clauses. docs/03-queries.md lists the queries that will
--         hit this table; index for those, not for every column.
--   Verify: pytest tests/test_schema.py::test_trips_table


-- ── 4. stop_visits ───────────────────────────────────────────────────────────
--
-- The core table. One row per observed departure, roughly 400,000 rows per day
-- — about 150 million a year. This is the one where your decisions actually
-- show up in query time and storage cost.
--
-- What the application needs to store:
--   service_date, trip_id, stop_id, parent_station, stop_sequence
--   scheduled_arrival    from the timetable
--   predicted_arrival    the realtime prediction captured at trip start
--   actual_arrival       when it really got there
--   runtime_deviation    actual segment time minus baseline segment time
--   delay_vs_schedule    actual minus timetable
--   delay_vs_prediction  actual minus prediction
--   actual_track, recorded_at
--
-- TODO(you): Write the CREATE TABLE and its indexes.
--   Why: Several things collide here.
--        (a) What is the natural primary key? A departure is identified by
--            something, and getting it right makes re-writes idempotent — the
--            poller may observe the same departure twice.
--        (b) delay_vs_schedule is only meaningful when baseline = 'timetable'.
--            It must be NULL otherwise. Where does that invariant live: in the
--            application, in a CHECK constraint, or both?
--        (c) stop_id is platform-level ("109N"); parent_station is the station
--            ("109"). Storing both is denormalized. Justify it or remove it.
--   Hint: At 150M rows/year, an index you do not need is expensive and an index
--         you do need is the difference between 50ms and 30 seconds. Write the
--         table first, run the queries from docs/03-queries.md with EXPLAIN
--         ANALYZE, and add indexes based on what you actually see.
--   Verify: pytest tests/test_schema.py::test_stop_visits_table


-- ── 5. current_trains ────────────────────────────────────────────────────────
--
-- Live position of every active train, roughly 800 rows, rewritten every poll.
-- This one is a cache, not history — it is the only table with a fundamentally
-- different access pattern from the rest.
--
-- What the application needs to store:
--   trip_id, service_date, route_id, direction, headsign, nyc_train_id
--   loc_stop_id, loc_station, status, stop_index
--   next_stop, next_arr, delay_seconds
--   shape_id, scheduled_track, actual_track
--   last_movement_at, is_stalled, has_delay_alert, updated_at
--
-- TODO(you): Write the CREATE TABLE and its indexes.
--   Why: Every row is replaced every 15 seconds, forever. That write pattern has
--        consequences in Postgres that an append-only table does not have —
--        worth understanding what UPDATE actually does to a row under MVCC, and
--        what that means for this table over days of uptime.
--   Hint: This table is small and entirely rebuilt each cycle. Ask whether it
--         needs to be a table at all, given the server already holds live state
--         in memory and the map reads from there. If you keep it, what is it
--         actually for — restart recovery, or something else?
--   Verify: pytest tests/test_schema.py::test_current_trains_table


-- ── 6. Access control ────────────────────────────────────────────────────────
--
-- Every table here is written by the server using the service-role key, which
-- bypasses row-level security entirely. Nothing in this schema is meant to be
-- read directly by a browser.
--
-- TODO(you): Decide what to do about RLS and write it.
--   Why: Supabase exposes every public table over PostgREST by default. A table
--        with RLS disabled and no policy is readable by anyone holding your anon
--        key, which ships to the browser. Enabling RLS with no policy blocks the
--        anon and authenticated roles while leaving the service role unaffected.
--   Hint: Work out which roles need access before writing any policy. The
--         simplest correct answer here may be no policies at all.
--   Verify: pytest tests/test_schema.py::test_rls_enabled
