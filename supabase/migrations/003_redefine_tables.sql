-- Redefine the tables around a correct delay baseline.
--
-- The old stop_visits measured delay against a snapshot of the realtime
-- prediction taken when a trip first appeared -- "late versus what the MTA
-- guessed", not "late versus the timetable". The new shape records which
-- baseline was used for every row, so no query can silently mix the two.
--
-- The 373k existing rows are kept as stop_visits_legacy rather than migrated:
-- their delay_seconds is not comparable to the new column, and re-deriving it
-- needs the operated schedule, which is a separate job.

-- ── Preserve, don't destroy ───────────────────────────────────────────────────
ALTER TABLE IF EXISTS public.stop_visits RENAME TO stop_visits_legacy;
ALTER TABLE IF EXISTS public.trip_schedules RENAME TO trip_schedules_legacy;

-- ── How a trip's scheduled baseline was resolved ──────────────────────────────
-- timetable    exact trip_id match against static GTFS
-- pattern      same route + identical stop sequence as a scheduled trip
-- pattern_tail remaining stops are a contiguous run inside a scheduled pattern
-- none         no baseline available; timing columns stay NULL
DO $$ BEGIN
    CREATE TYPE baseline_source AS ENUM
        ('timetable', 'pattern', 'pattern_tail', 'none');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── Stop patterns ─────────────────────────────────────────────────────────────
-- The catalog of distinct (route, ordered stop list) signatures. Seeded from
-- static GTFS at boot; patterns seen only in the realtime feed are appended, so
-- the catalog learns the diversions the static bundle omits.
CREATE TABLE IF NOT EXISTS public.stop_patterns (
    pattern_id  SERIAL PRIMARY KEY,
    route_id    TEXT   NOT NULL,
    direction   CHAR(1),
    stop_tuple  TEXT[] NOT NULL,
    runtimes    INTEGER[],              -- baseline inter-stop seconds
    from_static BOOLEAN NOT NULL,
    times_seen  INTEGER NOT NULL DEFAULT 0,
    first_seen  DATE    NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE (route_id, stop_tuple)
);

-- ── Trips ─────────────────────────────────────────────────────────────────────
-- One row per trip seen, written when the trip is first observed.
CREATE TABLE IF NOT EXISTS public.trips (
    trip_id      TEXT NOT NULL,
    service_date DATE NOT NULL,
    route_id     TEXT NOT NULL,
    direction    CHAR(1),
    headsign     TEXT,
    shape_id     TEXT,
    nyc_train_id TEXT,                  -- physical train, stable across trips

    baseline     baseline_source NOT NULL,
    pattern_id   INTEGER REFERENCES public.stop_patterns(pattern_id),
    -- True when the trip runs a known pattern at a time the timetable does not
    -- schedule: an extra train, not a late one.
    is_supplemental BOOLEAN NOT NULL DEFAULT FALSE,

    -- Full stop list resolved at first sight: [{stop_id, seq, sched_arr}, ...]
    scheduled_stops JSONB,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,

    PRIMARY KEY (service_date, trip_id)
);

CREATE INDEX IF NOT EXISTS idx_trips_route    ON public.trips (route_id, service_date);
CREATE INDEX IF NOT EXISTS idx_trips_baseline ON public.trips (baseline);
CREATE INDEX IF NOT EXISTS idx_trips_train    ON public.trips (nyc_train_id, service_date);

-- ── Stop visits ───────────────────────────────────────────────────────────────
-- One row per observed departure.
CREATE TABLE IF NOT EXISTS public.stop_visits (
    service_date  DATE NOT NULL,
    trip_id       TEXT NOT NULL,
    stop_id       TEXT NOT NULL,          -- platform level, e.g. "109N"
    parent_station TEXT NOT NULL,         -- e.g. "109"
    stop_sequence SMALLINT,

    scheduled_arrival TIMESTAMPTZ,        -- timetable; NULL unless baseline='timetable'
    predicted_arrival TIMESTAMPTZ,        -- realtime prediction at trip start
    actual_arrival    TIMESTAMPTZ,

    -- Actual inter-stop runtime minus the baseline runtime for that segment.
    -- Defined for every tier, because it needs no absolute clock time.
    runtime_deviation   INTEGER,
    -- Actual minus published timetable. Only meaningful for baseline='timetable'.
    delay_vs_schedule   INTEGER,
    -- Actual minus the realtime prediction. Measures MTA forecast error.
    delay_vs_prediction INTEGER,

    actual_track TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (service_date, trip_id, stop_id)
);

CREATE INDEX IF NOT EXISTS idx_visits_station ON public.stop_visits (parent_station, service_date);
CREATE INDEX IF NOT EXISTS idx_visits_trip    ON public.stop_visits (service_date, trip_id);

-- ── Live train positions ──────────────────────────────────────────────────────
-- Rewritten to carry the realtime fields the previous schema discarded.
DROP TABLE IF EXISTS public.current_trains;
CREATE TABLE public.current_trains (
    trip_id      TEXT PRIMARY KEY,
    service_date DATE NOT NULL,
    route_id     TEXT NOT NULL,
    direction    CHAR(1),
    headsign     TEXT,
    nyc_train_id TEXT,

    loc_stop_id  TEXT,          -- platform, e.g. "109N"
    loc_station  TEXT,          -- parent station, e.g. "109"
    status       TEXT,          -- STOPPED_AT | IN_TRANSIT_TO | INCOMING_AT
    stop_index   SMALLINT,      -- position along the route

    next_stop    TEXT,
    next_arr     TIMESTAMPTZ,
    delay_seconds INTEGER,      -- versus the resolved baseline

    shape_id     TEXT,
    scheduled_track TEXT,
    actual_track    TEXT,

    -- Last time the train was detected moving. Stale by >90s means stalled;
    -- the NYCT spec says countdown clocks should stop in that case.
    last_movement_at TIMESTAMPTZ,
    is_stalled       BOOLEAN NOT NULL DEFAULT FALSE,
    has_delay_alert  BOOLEAN NOT NULL DEFAULT FALSE,

    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ct_station ON public.current_trains (loc_station);
CREATE INDEX IF NOT EXISTS idx_ct_route   ON public.current_trains (route_id);

-- ── Access ────────────────────────────────────────────────────────────────────
-- Every table is written by the server using the service-role key, which bypasses
-- RLS. Enabling RLS with no policy therefore blocks the anon/authenticated roles
-- entirely, which is what we want: nothing here is meant to be read directly by
-- a browser.
ALTER TABLE public.stop_patterns  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trips          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stop_visits    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.current_trains ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stop_info      ENABLE ROW LEVEL SECURITY;
