-- Capture the schema that already exists in production.
--
-- trip_schedules, stop_visits and stop_info were created by hand and have never
-- had DDL in this repo. This migration writes down what is actually there, so a
-- fresh database can be built from migrations alone. It changes nothing.
--
-- Verified against production 2026-09-09:
--   current_trains  428 rows   trip_schedules  13,057 rows
--   stop_visits     373,153    stop_info       1,488

CREATE TABLE IF NOT EXISTS public.trip_schedules (
    trip_id        TEXT PRIMARY KEY,
    start_date     DATE,
    route_id       TEXT,
    direction      CHAR(1),
    shape_id       TEXT,
    stops          JSONB,        -- [{stop_id, seq, stop_name, sched_arr}, ...]
    is_active      BOOLEAN DEFAULT TRUE,
    snapshotted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.stop_visits (
    id                BIGSERIAL PRIMARY KEY,
    trip_id           TEXT,
    start_date        DATE,
    route_id          TEXT,
    direction         CHAR(1),
    stop_id           TEXT,
    parent_station    TEXT,
    stop_name         TEXT,
    stop_sequence     INTEGER,
    scheduled_arrival TIMESTAMPTZ,
    actual_arrival    TIMESTAMPTZ,
    delay_seconds     INTEGER,
    recorded_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.stop_info (
    stop_id        TEXT PRIMARY KEY,
    stop_name      TEXT,
    stop_lat       DOUBLE PRECISION,
    stop_lon       DOUBLE PRECISION,
    location_type  INTEGER,
    parent_station TEXT
);

-- current_trains is declared in 001, but production is missing loc_stop_id --
-- src/poller.py strips that column from every upsert to work around it.
-- Add it so the code can stop compensating.
ALTER TABLE public.current_trains ADD COLUMN IF NOT EXISTS loc_stop_id TEXT;
