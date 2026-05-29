# MTA Data Analytics

A real-time NYC subway analytics system that combines static GTFS schedule data with live GTFS-RT feeds to track train positions, delays, and service patterns. Train and station data is stored in a local DuckDB database and connected in a NetworkX graph for spatial queries.

---

## Architecture

```
nyct-gtfs (live feed)
        │
        ▼
src/ingestion.py   ←── Poller (every 30s)
        │
        ├──► src/db.py          DuckDB persistence layer
        │         │
        │         ├── static tables   (stops, routes, trips, stop_times, calendar, transfers)
        │         └── realtime tables (train_observations, stop_predictions)
        │
        └──► src/graph.py       SubwayGraph (NetworkX DiGraph)
                  │
                  ├── nodes: parent stations
                  └── edges: scheduled travel times + transfer walks

src/models.py      Pure dataclasses (Station, Route, Trip, StopTime, Train)
src/analytics.py   Query functions over accumulated observations (planned)
```

### Data flow

1. **Static data** — GTFS CSV files in `static/` are loaded into DuckDB once on first run. Trips get an `rt_trip_key` derived column so static schedules can be joined against live feed trip IDs.
2. **Live polling** — `Poller._poll_once()` fetches all 8 MTA feed groups, converts each underway trip into a `Train` object, looks up the scheduled arrival for each remaining stop, stores the observation and per-stop predictions, then updates the graph with current train positions.
3. **Delay computation** — for each train, the first upcoming stop with a predicted arrival is matched against the static schedule. `delay_seconds = predicted − scheduled` (positive = late, negative = early, `None` = no schedule match).

<img width="2034" height="1291" alt="diagram" src="https://github.com/user-attachments/assets/46966d5f-f856-4cf6-820a-4efe218e9e26" />

---

## Project Layout

```
mta-data-analytics/
├── src/
│   ├── models.py       Dataclasses: Station, Route, Trip, StopTime, Train
│   ├── db.py           DuckDB connection, schema, CSV loading, write/read API
│   ├── graph.py        SubwayGraph wrapping NetworkX DiGraph
│   ├── ingestion.py    Poller class + entry point
│   └── analytics.py    Query functions (in progress)
├── static/             Static GTFS CSVs (do not modify)
│   ├── stops.txt           1,488 rows
│   ├── routes.txt          28 rows
│   ├── trips.txt           20,310 rows
│   ├── stop_times.txt      562,755 rows
│   ├── calendar.txt        3 rows (Weekday / Saturday / Sunday)
│   └── transfers.txt       613 rows
├── mta.duckdb          Local database file (created on first run)
├── pyproject.toml
└── requirements.txt
```

---

## Setup

**Requirements:** Python 3.12 (protobuf C extension is incompatible with Python 3.14+).

```bash
# Create and activate venv
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

> If recreating the venv on a machine with multiple Python versions, use the explicit path:
> `/home/linuxbrew/.linuxbrew/opt/python@3.12/bin/python3.12 -m venv .venv`

---

## Running

All commands must be run from the **project root** — relative paths for `static/` and `mta.duckdb` depend on it.

### Start the poller

```bash
.venv/bin/python src/ingestion.py
```

On first run this loads the static GTFS data into `mta.duckdb` (~562K rows), builds the graph, then begins polling every 30 seconds. Output:

```
2026-05-28 20:37:01,549 INFO poll complete — 385 trains (service_date 2026-05-28)
2026-05-28 20:37:31,821 INFO poll complete — 391 trains (service_date 2026-05-28)
```

Stop with `Ctrl+C` — SIGINT and SIGTERM are handled cleanly (no mid-write interruption).

### Verify the database

```bash
.venv/bin/python src/db.py
```

Prints row counts for all static tables and runs a `get_scheduled_arrival` sanity check against today's schedule.

---

## Database Schema

### Static tables

| Table | Rows | Key columns |
|---|---|---|
| `stops` | 1,488 | `stop_id` (platform or parent), `parent_station` |
| `routes` | 28 | `route_id` (e.g. `"A"`), `route_color` |
| `trips` | 20,310 | `trip_id`, `rt_trip_key` (derived, for RT joins) |
| `stop_times` | 562,755 | `trip_id`, `stop_id`, `arrival_seconds`, `stop_sequence` |
| `calendar` | 3 | `service_id` (`Weekday`/`Saturday`/`Sunday`), day flags |
| `transfers` | 613 | `from_stop`, `to_stop`, `transfer_time` |

Arrival and departure times are stored as **integer seconds past midnight** (not SQL `TIME`) to support GTFS overnight trips where `HH` exceeds 23.

### Realtime tables

**`train_observations`** — one row per train per poll cycle.

| Column | Type | Notes |
|---|---|---|
| `id` | `UINTEGER` | Auto from sequence |
| `observed_at` | `TIMESTAMPTZ` | UTC timestamp of the poll |
| `service_date` | `DATE` | GTFS service date (before 3 AM → previous day) |
| `rt_trip_id` | `VARCHAR` | Live feed trip ID |
| `route_id` | `VARCHAR` | e.g. `"A"` |
| `direction` | `CHAR(1)` | `N` or `S` |
| `location_stop_id` | `VARCHAR` | Current platform stop ID |
| `location_parent_station` | `VARCHAR` | `location_stop_id[:-1]` |
| `location_status` | `VARCHAR` | `STOPPED_AT` / `INCOMING_AT` / `IN_TRANSIT_TO` |
| `delay_seconds` | `INTEGER` | Positive = late, negative = early, NULL = no match |
| `next_stop_id` | `VARCHAR` | First upcoming stop |
| `next_stop_predicted_arrival` | `TIMESTAMPTZ` | Predicted arrival at next stop |

**`stop_predictions`** — one row per remaining stop per train per poll. Foreign key to `train_observations.id`. Intended as training labels for future ML models.

| Column | Type | Notes |
|---|---|---|
| `observation_id` | `UINTEGER` | FK → `train_observations.id` |
| `stop_id` | `VARCHAR` | Platform stop ID |
| `stop_sequence` | `SMALLINT` | 0-based index within remaining stops |
| `predicted_arrival` | `TIMESTAMPTZ` | Feed-provided predicted arrival |
| `scheduled_arrival_seconds` | `INTEGER` | Seconds past midnight from static schedule |

---

## Feed Groups

The MTA publishes 8 GTFS-RT feed endpoints, each covering a set of lines:

| Key | Lines |
|---|---|
| `1` | 1 2 3 4 5 6 7 S |
| `A` | A C E H FS |
| `B` | B D F M |
| `G` | G |
| `J` | J Z |
| `N` | N Q R W |
| `L` | L |
| `SI` | Staten Island Railway |

---

## Key Design Decisions

| Decision | Reason |
|---|---|
| `nyct-gtfs` over raw protobuf | Cleaner API, no API key required, handles NYCT extensions |
| DuckDB over Postgres/SQLite | In-process, no server, fast analytical queries, native CSV import |
| `arrival_seconds` as integer | Python `time` can't represent >24:00:00; overnight GTFS times are common |
| `rt_trip_key` derived column | Static and live trip IDs differ in prefix; precomputed join key avoids per-query regex |
| Parent stations as graph nodes | Platforms are directional; stations are the meaningful unit for analytics |
| Edge weights = median travel time | Computed in SQL with `MEDIAN()`; robust to outliers from delays or express skips |
| `stop_predictions` table | Per-stop arrival predictions accumulated for future ML training labels |
| `service_date` stored separately | Avoids re-deriving from timestamps in every query; handles overnight trips correctly |
| `_meta` guard table | Makes static data loading idempotent — safe to call `initialize()` on every startup |

---

## Planned: ML Layer (Phase 2)

After accumulating several weeks of observations, the following models are planned:

- **Delay propagation** (regression) — given delay at stop A, predict delay at downstream stop B
- **On-time probability** (binary classification) — predict P(on time) at trip origin
- **Anomaly detection** (unsupervised, Isolation Forest) — flag unusual service before incidents are reported
- **Service pattern clustering** (K-means) — identify distinct service regimes by route/hour/delay
- **Time series forecasting** (Prophet or LSTM) — hourly average delay per route

Planned modules: `src/features.py`, `src/train.py`, `src/predictor.py`.
Additional dependencies when starting Phase 2: `scikit-learn`, `xgboost` or `lightgbm`, `joblib`.
