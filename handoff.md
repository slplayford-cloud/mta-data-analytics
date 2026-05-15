# MTA Data Analytics — Handoff

## Goal

Build a real-time NYC subway analytics system with an object-oriented graph interface, eventually extended with machine learning. Stations and trains are first-class Python dataclasses connected in a NetworkX directed graph, backed by a local DuckDB database. The system combines static GTFS schedule data with live GTFS-RT feeds (via `nyct-gtfs`) to track train positions, delays, and service patterns — and will eventually train ML models on the accumulated realtime data.

The project is being built by the user as a learning exercise. Do not write full implementations unprompted — explain concepts, review code, and debug specific issues on request.

---

## Project Status

**Phase 1 (Core system) — IN PROGRESS.** The user is implementing each module themselves following `study_guide.md`. No source modules exist yet beyond what's listed below.

**Phase 2 (ML layer) — PLANNED, not started.** See ML section below.

---

## Repository Layout

```
mta-data-analytics/
├── src/
│   ├── __init__.py          # to be created (makes src a package)
│   ├── pull_data.py         # EXISTS — to be deleted when ingestion.py is ready
│   ├── models.py            # to be created
│   ├── db.py                # to be created
│   ├── graph.py             # to be created
│   ├── ingestion.py         # to be created (replaces pull_data.py)
│   └── analytics.py         # to be created
├── static/                  # static GTFS CSVs — already downloaded, do not re-fetch
│   ├── stops.txt            # 1,488 rows
│   ├── routes.txt           # 28 rows
│   ├── trips.txt            # 20,310 rows
│   ├── stop_times.txt       # 562,755 rows (largest)
│   ├── calendar.txt         # 3 rows (Sunday / Saturday / Weekday service IDs)
│   ├── transfers.txt        # 613 rows
│   ├── shapes.txt           # 149,423 rows — NOT loaded into DB (map rendering only)
│   └── calendar_dates.txt   # empty — NOT loaded
├── study_guide.md           # self-study walkthrough for the user
├── handoff.md               # this file
├── requirements.txt         # see Dependencies section
├── pyrightconfig.json       # basedpyright, loose mode
└── .venv/                   # Python 3.12 venv
```

---

## Dependencies

**Installed (requirements.txt):**
```
requests==2.34.0
gtfs-realtime-bindings==1.0.0     # will become unused once nyct-gtfs is in use
nyct-gtfs==2.1.0
httpx==0.28.1
duckdb==1.2.2
networkx==3.4.2
```

**Python version:** 3.12 (linuxbrew). The venv was recreated at Python 3.12 to fix a protobuf C extension incompatibility with Python 3.14. Run scripts from the project root with `.venv/bin/python`.

**Environment variable:** `MTA_API_KEY` — no longer required by `nyct-gtfs` as of v2.0.0, but may still be set in the environment.

---

## Static GTFS Data — Schemas and Key Facts

All files are CSV with a header row. Loaded into DuckDB by `db.py`.

### stops.txt
Columns: `stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station`

Two types of rows:
- **Parent stations** (`location_type=1`, `parent_station` empty): e.g. `101,Van Cortlandt Park-242 St,...,1,`
- **Directional platforms** (`location_type` empty, `parent_station` set): e.g. `101N,...,,101` and `101S,...,,101`

Graph nodes are parent stations. `stop_times` references platform stop_ids. The relationship is always `platform_stop_id[:-1] == parent_station` — this formula is used everywhere to map between them.

### routes.txt
Columns: `route_id, agency_id, route_short_name, route_long_name, route_desc, route_type, route_url, route_color, route_text_color, route_sort_order`

28 routes. `route_id` is the single-letter/number line identifier (e.g. `"A"`, `"1"`). `route_color` is a 6-char hex string without `#`.

### trips.txt
Columns: `route_id, trip_id, service_id, trip_headsign, direction_id, shape_id`

20,310 rows. `trip_id` is the full static form, e.g. `"AFA25GEN-1038-Sunday-00_000600_1..S03R"`. The realtime feed uses a short form (`rt_trip_key`): `"000600_1..S03R"`. This is extracted during CSV loading via `regexp_extract(trip_id, '_(\d+\_.+)$', 1)` and stored as a separate column for joins.

`service_id` values: `"Sunday"`, `"Saturday"`, `"Weekday"` (only 3).

### stop_times.txt
Columns: `trip_id, stop_id, arrival_time, departure_time, stop_sequence`

562,755 rows — the largest table. `stop_id` is a platform id (e.g. `"101S"`). Times are `HH:MM:SS` strings where `HH` can exceed 23 for overnight trips (e.g. `"25:30:00"`). **Stored as integer seconds past midnight** in the DB, not as SQL TIME (which can't represent >24h). Conversion: `H*3600 + M*60 + S`. `parent_station` (`stop_id[:-1]`) is computed and stored as a derived column during load.

### calendar.txt
Columns: `service_id, monday, tuesday, wednesday, thursday, friday, saturday, sunday, start_date, end_date`

3 rows. Binary weekday flags. Used to determine which `service_id` applies to a given date when looking up scheduled arrivals.

### transfers.txt
Columns: `from_stop_id, to_stop_id, transfer_type, min_transfer_time`

613 rows. Most entries are same-stop self-transfers (`from == to`, `transfer_type=2`, 180s). Cross-station transfers (where `from != to`) become transfer edges in the NetworkX graph.

---

## Architecture

### `src/models.py`
Pure Python dataclasses. No I/O. No imports from this project.

```
Station     — parent station (graph node): station_id, name, lat, lon, routes[]
Route       — subway line: route_id, short_name, long_name, color, text_color
Trip        — static scheduled run: trip_id, route_id, service_id, headsign, direction_id, shape_id
StopTime    — schedule entry: trip_id, stop_id, parent_station, arrival_seconds, departure_seconds, stop_sequence
Train       — live observation: trip_id (RT form), route_id, direction, headsign, location_stop_id,
              location_parent_station, location_status, last_position_update, has_delay_alert,
              observed_at, delay_seconds (Optional — None if no schedule match)
```

Key design choice: `arrival_seconds` and `departure_seconds` on `StopTime` are integers (seconds past midnight), not Python `time` objects. This handles overnight GTFS times that exceed `24:00:00`.

### `src/db.py`
DuckDB connection management, schema creation, CSV loading, realtime write path.

**Public API:**
- `get_connection(db_path="mta.duckdb")` → connection
- `initialize(conn, static_dir="static/")` → idempotent; guarded by `_meta` table key `'static_loaded'`
- `write_observations(conn, trains: list[Train])` → inserts into `train_observations` + `stop_predictions`
- `lookup_scheduled_arrival(conn, rt_trip_id, stop_id, service_date)` → `int | None` (seconds past midnight)

**Static tables:** `stops`, `routes`, `trips` (with `rt_trip_key` derived column), `stop_times` (with `parent_station` and integer seconds derived), `calendar`, `transfers`, `_meta`

**Realtime tables:**
- `train_observations` — one row per train per poll. Key columns: `observed_at`, `service_date`, `rt_trip_id`, `route_id`, `direction`, `location_parent_station`, `location_status`, `delay_seconds`, `has_delay_alert`
- `stop_predictions` — one row per upcoming stop per train per poll. FK to `train_observations.id`. Stores `stop_id`, `stop_sequence`, `predicted_arrival`, `scheduled_arrival_seconds`. **This is the training label data for ML models — do not skip it.**

Shapes and calendar_dates are not loaded (shapes = map only; calendar_dates = empty file).

### `src/graph.py`
`SubwayGraph` class wrapping `nx.DiGraph`.

**Nodes:** parent station stop_ids (e.g. `"101"`). Attributes: `{"station": Station, "trains": []}`.

**Route edges:** computed by a single self-join SQL query on `stop_times` (consecutive stops on the same trip). Edge weight = median scheduled travel time in seconds across all trips for that station pair. Attributes: `{"routes": ["1","2"], "weight": 90.0, "edge_type": "route"}`. Multi-route pairs aggregate their routes list.

**Transfer edges:** from `transfers` table where `from_stop_id != to_stop_id`. Bidirectional directed edges. Attributes: `{"edge_type": "transfer", "weight": min_transfer_time}`.

**`update_trains(trains)`:** clears all node `trains` lists, re-places each `Train` at its `location_parent_station` node.

**Public API:** `build()`, `update_trains(trains)`, `get_station(id)`, `get_trains_at(id)`. Raw graph accessible as `self.graph`.

### `src/ingestion.py`
`Poller` class. Fetches all 8 GTFS-RT feed groups every 30 seconds.

**Feed groups (nyct-gtfs key → lines covered):**
```
"1"  → 1 2 3 4 5 6 7 S
"A"  → A C E H FS
"B"  → B D F M
"G"  → G
"J"  → J Z
"N"  → N Q R W
"L"  → L
"SI" → Staten Island Railway
```

**`parse_train(trip, observed_at)`** → `Train`. Derives `location_parent_station = location_stop_id[:-1]`.

**`compute_delay(conn, train, stop_time_updates, service_date)`** → `int | None`. Finds first `StopTimeUpdate` with non-None `arrival`, calls `lookup_scheduled_arrival`, returns predicted minus scheduled seconds. Positive = late.

**`_derive_service_date(dt)`** → `date`. Before 3 AM local time → previous calendar day (GTFS overnight convention: arrival_seconds can exceed 86400 for trains running after midnight).

**Polling:** drift-corrected using `time.monotonic()`. Per-feed errors caught and logged to stderr without aborting the poll cycle. SIGINT/SIGTERM handled cleanly via `poller.stop()`.

**`__main__`:** connects DB, initializes, builds graph, starts poller. Entry point is `python src/ingestion.py`.

`src/pull_data.py` is superseded by this module and should be deleted before or when `ingestion.py` is complete.

### `src/analytics.py`
Query functions over `train_observations`. All return plain `list[dict]` or `dict`.

```
avg_delay_by_route(conn, since=None)             → [{route_id, avg_delay_seconds, sample_count}]
on_time_pct_by_route(conn, threshold_s=300)      → [{route_id, on_time_pct, total_obs}]
on_time_pct_by_station(conn, station_id)         → {station_id, on_time_pct, avg_delay_seconds, total_obs}
avg_delay_by_hour(conn, route_id=None)           → [{hour, avg_delay_seconds, sample_count}]
delay_trend(conn, route_id, window_minutes=60)   → [{bucket_start, avg_delay_seconds}] (5-min buckets)
```

All queries filter `WHERE delay_seconds IS NOT NULL`. Hour extraction uses `AT TIME ZONE 'America/New_York'`.

---

## DuckDB Table Schemas (full DDL)

```sql
-- Static tables
CREATE TABLE stops (stop_id VARCHAR PRIMARY KEY, stop_name VARCHAR NOT NULL,
    stop_lat DOUBLE, stop_lon DOUBLE, location_type TINYINT, parent_station VARCHAR);

CREATE TABLE routes (route_id VARCHAR PRIMARY KEY, short_name VARCHAR,
    long_name VARCHAR, route_color VARCHAR, route_text_color VARCHAR);

CREATE TABLE trips (trip_id VARCHAR PRIMARY KEY, route_id VARCHAR NOT NULL,
    service_id VARCHAR NOT NULL, headsign VARCHAR, direction_id TINYINT,
    shape_id VARCHAR, rt_trip_key VARCHAR);
CREATE INDEX idx_trips_rt_key ON trips(rt_trip_key);

CREATE TABLE stop_times (trip_id VARCHAR NOT NULL, stop_id VARCHAR NOT NULL,
    parent_station VARCHAR NOT NULL, arrival_seconds INTEGER NOT NULL,
    departure_seconds INTEGER NOT NULL, stop_sequence SMALLINT NOT NULL,
    PRIMARY KEY (trip_id, stop_sequence));
CREATE INDEX idx_st_stop   ON stop_times(stop_id);
CREATE INDEX idx_st_parent ON stop_times(parent_station);

CREATE TABLE calendar (service_id VARCHAR PRIMARY KEY,
    monday BOOLEAN, tuesday BOOLEAN, wednesday BOOLEAN, thursday BOOLEAN,
    friday BOOLEAN, saturday BOOLEAN, sunday BOOLEAN,
    start_date DATE, end_date DATE);

CREATE TABLE transfers (from_stop_id VARCHAR NOT NULL, to_stop_id VARCHAR NOT NULL,
    transfer_type TINYINT, min_transfer_time INTEGER,
    PRIMARY KEY (from_stop_id, to_stop_id));

CREATE TABLE _meta (key VARCHAR PRIMARY KEY, value VARCHAR);

-- Realtime tables
CREATE SEQUENCE obs_seq;
CREATE TABLE train_observations (
    id                       UINTEGER DEFAULT nextval('obs_seq'),
    observed_at              TIMESTAMPTZ NOT NULL,
    service_date             DATE NOT NULL,
    rt_trip_id               VARCHAR NOT NULL,
    route_id                 VARCHAR NOT NULL,
    direction                CHAR(1),
    headsign                 VARCHAR,
    location_stop_id         VARCHAR,
    location_parent_station  VARCHAR,
    location_status          VARCHAR,
    last_position_update     TIMESTAMPTZ,
    has_delay_alert          BOOLEAN DEFAULT FALSE,
    delay_seconds            INTEGER,
    next_stop_id             VARCHAR,
    next_stop_predicted_arrival TIMESTAMPTZ);
CREATE INDEX idx_obs_route    ON train_observations(route_id, observed_at);
CREATE INDEX idx_obs_station  ON train_observations(location_parent_station, observed_at);
CREATE INDEX idx_obs_tripdate ON train_observations(rt_trip_id, service_date);

CREATE TABLE stop_predictions (
    observation_id    UINTEGER NOT NULL,
    stop_id           VARCHAR NOT NULL,
    stop_sequence     SMALLINT NOT NULL,
    predicted_arrival TIMESTAMPTZ,
    scheduled_arrival_seconds INTEGER,
    PRIMARY KEY (observation_id, stop_sequence));
```

---

## Key Design Decisions (rationale matters for future changes)

| Decision | Rationale |
|---|---|
| `nyct-gtfs` instead of raw protobuf | Cleaner API, no API key required, handles NYCT extensions |
| DuckDB over Postgres/SQLite | In-process, no server, fast analytical queries, native CSV import |
| `arrival_seconds` as integer | Python `time` can't represent >24:00:00; overnight GTFS times are common |
| `rt_trip_key` derived column | Static and realtime trip IDs differ in prefix; precomputing the join key avoids per-query regex |
| Parent stations as graph nodes | Platforms are directional artifacts; stations are the meaningful unit for analytics and routing |
| Edge weights = median travel time | Median is robust to outliers (express skips, delays); computed in SQL not Python for performance |
| `stop_predictions` table | Stores per-stop arrival predictions for every polled train — future ML training labels; cheap to write now |
| `service_date` stored separately | Avoids re-deriving from timestamps in every analytics query; handles overnight trips correctly |
| `_meta` guard table | Makes `initialize()` idempotent — safe to call on every startup without re-loading 562K rows |

---

## Phase 2 — Machine Learning (planned, not started)

**Prerequisite:** several weeks of `train_observations` + `stop_predictions` data.

**Planned modules:**
- `src/features.py` — joins `train_observations` with `stop_times`/`trips` to produce ML feature matrices
- `src/train.py` — trains delay prediction models, saves with `joblib`
- `src/predictor.py` — loads trained models, attaches predictions to `Train` objects during polling

**Target models:**
1. **Delay propagation prediction** (regression) — given current delay at stop A, predict delay at downstream stop B. Features: current delay, stops remaining, route, hour, day of week.
2. **On-time arrival probability** (binary classification) — predict P(on time) at trip origin.
3. **Anomaly detection** (unsupervised) — Isolation Forest on rolling delay windows to flag unusual service before incidents are reported.
4. **Service pattern clustering** (unsupervised) — K-means on (route, hour, avg_delay) to identify distinct service regimes.
5. **Time series forecasting** — Prophet or LSTM on hourly avg delay per route.

**Known pitfall:** label imbalance — most trains are on time, so classifiers default to "on time." Use oversampling or anomaly-detection framing.

**Dependencies to add when starting Phase 2:** `scikit-learn`, `xgboost` or `lightgbm`, `joblib`, (optionally) `prophet`, `torch` + `torch_geometric` for GNN work.

---

## Important Gotchas

- **Python 3.14 breaks protobuf C extension** — the venv must be Python 3.12. If the venv is ever recreated, use `/home/linuxbrew/.linuxbrew/opt/python@3.12/bin/python3.12`.
- **Run scripts from project root** — `python src/ingestion.py`, not `cd src && python ingestion.py`. Relative paths for `static/` and `mta.duckdb` assume project root as CWD.
- **`src/__init__.py` required** — cross-module imports (`from src.models import Train`) only work if this file exists.
- **`stop_times` arrival times > 86400s** — always verify `MAX(arrival_seconds) > 86400` after loading; if it's exactly 86400 the parsing is wrong.
- **`location_status` values** — `"STOPPED_AT"`, `"INCOMING_AT"`, `"IN_TRANSIT_TO"`. A train with no reported position has `location = None`; handle this before computing `location_parent_station`.
- **Feed freshness** — `nyct-gtfs` fetches on construction; to re-use a feed object call `feed.refresh()`. Each `NYCTFeed(key)` in the poll loop makes one HTTP request.
- **DuckDB is not thread-safe** — one connection for the polling/write path, separate `read_only=True` connections for concurrent analytics if threading is ever added.
