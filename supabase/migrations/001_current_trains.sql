CREATE TABLE IF NOT EXISTS public.current_trains (
    trip_id       TEXT PRIMARY KEY,
    route_id      TEXT NOT NULL,
    direction     CHAR(1),
    headsign      TEXT,
    loc_stop_id   TEXT,         -- platform stop id, e.g. "109N"
    loc_station   TEXT,         -- parent station id, e.g. "109"
    status        TEXT,         -- STOPPED_AT | IN_TRANSIT_TO | INCOMING_AT
    next_stop     TEXT,
    next_arr      TIMESTAMPTZ,
    delay_seconds INTEGER,
    shape_id      TEXT,
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ct_loc    ON current_trains(loc_station);
CREATE INDEX IF NOT EXISTS idx_ct_next   ON current_trains(next_stop);
