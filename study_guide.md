# MTA Subway Analytics — Self-Study Guide

Here's a self-study guide you can work through at your own pace. It's structured as phases matching the plan, with the concepts and docs you'll need to understand before writing each piece — without writing the code for you.

---

## Phase 0 — Project Setup

**What to do:**
- Add `duckdb` and `networkx` to `requirements.txt` and install them into your venv
- Create empty files for each module: `src/models.py`, `src/db.py`, `src/graph.py`, `src/ingestion.py`, `src/analytics.py`
- Delete `src/pull_data.py` when you're ready (it'll conflict with `ingestion.py`)

**Key concept — Python package imports across `src/`:**
When you import between files (e.g. `from src.models import Train` in `db.py`), Python needs to know `src` is a package. Create an empty `src/__init__.py`. Run scripts from the project root, not from inside `src/`.

---

## Phase 1 — `src/models.py`

**Goal:** Define all your data structures as Python dataclasses. No I/O, no imports from your own project.

**Concepts to understand:**

**Python `dataclasses`** — https://docs.python.org/3/library/dataclasses.html
- `@dataclass` generates `__init__`, `__repr__`, `__eq__` automatically from your field annotations
- Fields with default values must come after fields without. Use `field(default_factory=list)` for mutable defaults like lists — never `routes: list[str] = []`
- `from __future__ import annotations` lets you use forward references and `list[str]` syntax on Python <3.10

**Python `typing`** — https://docs.python.org/3/library/typing.html
- `Optional[X]` means the value can be `X` or `None`. Equivalent to `X | None` in Python 3.10+
- Use it wherever a field might not be populated (e.g. a train that hasn't reported its position yet)

**GTFS data model — key relationships to encode in your dataclasses:**
- A `Route` is a line (e.g. the A train). A `Trip` is one specific run of that route on a given day.
- A `Station` is a physical place with a `parent_station` stop_id (e.g. `"101"`). Platforms are directional children (e.g. `"101N"`, `"101S"`). Your graph nodes will be stations, but `stop_times` reference platforms.
- `StopTime` links a trip to a stop with scheduled arrival/departure times. GTFS expresses times as `HH:MM:SS` strings, where `HH` can exceed 23 for overnight trips (e.g. `25:30:00` means 1:30 AM the next day). **Store these as integer seconds past midnight**, not as Python `time` objects (which can't represent >24h).
- `Train` is your live realtime object — not in the static GTFS at all. It comes from the live feed and maps onto the static data via `trip_id`.

**Done when:** You can run `python -c "from src.models import Station, Train, StopTime; print('ok')"` without errors.

---

## Phase 2 — `src/db.py`

**Goal:** A module that creates and manages your DuckDB database. Two responsibilities: (1) set up schema and load static CSVs once; (2) provide write/read functions for realtime data.

**Concepts to understand:**

**DuckDB** — https://duckdb.org/docs/
- DuckDB is an in-process analytical database — no server, just a file. Think SQLite but columnar and fast for analytics.
- Key difference from SQLite: DuckDB can `read_csv_auto('file.csv')` directly in SQL — you don't need to manually parse CSVs in Python first.
- Connect with `duckdb.connect('mta.duckdb')` for a persistent file, or `duckdb.connect(':memory:')` for testing.
- Execute SQL: `conn.execute("SELECT ...")`. Fetch results: `.fetchone()` (one row tuple), `.fetchall()` (list of tuples), `.fetchdf()` (pandas DataFrame).
- Parameters: always use `?` placeholders (`conn.execute("SELECT * FROM t WHERE id = ?", [value])`), never f-strings with user data.
- Relevant DuckDB docs sections: https://duckdb.org/docs/data/csv/overview, https://duckdb.org/docs/sql/introduction, https://duckdb.org/docs/sql/data_types/overview

**SQL DDL concepts you'll use:**
- `CREATE TABLE IF NOT EXISTS` — idempotent table creation
- `CREATE INDEX IF NOT EXISTS` — add indexes after table creation for query performance
- `PRIMARY KEY`, `NOT NULL` — constraints
- `TIMESTAMPTZ` vs `TIMESTAMP` — use `TIMESTAMPTZ` for anything with timezone info (DuckDB stores it in UTC)
- `CREATE SEQUENCE` + `nextval()` — auto-incrementing IDs

**DuckDB string functions you'll need for CSV loading:**
- `string_split(str, delimiter)` — returns a list (1-indexed in DuckDB). Use `[1]`, `[2]`, etc.
- `regexp_extract(str, pattern, group)` — extract a capture group from a regex
- `left(str, n)` — first N characters
- `length(str)` — string length
- `CAST(x AS INTEGER)` — type conversion

**Arrival time parsing challenge:**
The `stop_times.txt` has times like `"00:06:00"` and `"25:30:00"`. You can't cast these to SQL `TIME` because >24h values will error. Instead, split on `':'` and compute: `hours * 3600 + minutes * 60 + seconds`. Do this in the SQL `INSERT` statement during CSV loading.

**`rt_trip_key` extraction challenge:**
Static trip IDs look like `"AFA25GEN-1038-Sunday-00_000600_1..S03R"`. The realtime feed uses a short form: `"000600_1..S03R"`. You need a way to join them. Use `regexp_extract` to pull out everything after the last `_` that precedes a digit sequence. Verify your regex works on several sample trip IDs before loading all 20K rows.

**Idempotency — guard against double-loading:**
Create a `_meta` table with key/value pairs. Before loading CSVs, check: `SELECT value FROM _meta WHERE key = 'static_loaded'`. If it exists, skip loading. After loading, insert `('static_loaded', 'true')` into `_meta`.

**Public API to implement:**
```python
def get_connection(db_path: str = "mta.duckdb") -> duckdb.DuckDBPyConnection
def initialize(conn, static_dir: str = "static/") -> None   # idempotent
def write_observations(conn, trains: list[Train]) -> None
def lookup_scheduled_arrival(conn, rt_trip_id: str, stop_id: str, service_date: date) -> Optional[int]
```

**Static table schemas to create:**

```sql
CREATE TABLE IF NOT EXISTS stops (
    stop_id        VARCHAR PRIMARY KEY,
    stop_name      VARCHAR NOT NULL,
    stop_lat       DOUBLE,
    stop_lon       DOUBLE,
    location_type  TINYINT,      -- 1=parent station, NULL=platform
    parent_station VARCHAR
);

CREATE TABLE IF NOT EXISTS routes (
    route_id         VARCHAR PRIMARY KEY,
    short_name       VARCHAR,
    long_name        VARCHAR,
    route_color      VARCHAR,
    route_text_color VARCHAR
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id      VARCHAR PRIMARY KEY,
    route_id     VARCHAR NOT NULL,
    service_id   VARCHAR NOT NULL,
    headsign     VARCHAR,
    direction_id TINYINT,
    shape_id     VARCHAR,
    rt_trip_key  VARCHAR  -- e.g. "000600_1..S03R" (for joining with realtime data)
);
CREATE INDEX IF NOT EXISTS idx_trips_rt_key ON trips(rt_trip_key);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_id           VARCHAR NOT NULL,
    stop_id           VARCHAR NOT NULL,   -- platform id e.g. "101S"
    parent_station    VARCHAR NOT NULL,   -- stop_id[:-1]
    arrival_seconds   INTEGER NOT NULL,
    departure_seconds INTEGER NOT NULL,
    stop_sequence     SMALLINT NOT NULL,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_st_stop   ON stop_times(stop_id);
CREATE INDEX IF NOT EXISTS idx_st_parent ON stop_times(parent_station);

CREATE TABLE IF NOT EXISTS calendar (
    service_id VARCHAR PRIMARY KEY,
    monday BOOLEAN, tuesday BOOLEAN, wednesday BOOLEAN,
    thursday BOOLEAN, friday BOOLEAN, saturday BOOLEAN, sunday BOOLEAN,
    start_date DATE,
    end_date   DATE
);

CREATE TABLE IF NOT EXISTS transfers (
    from_stop_id      VARCHAR NOT NULL,
    to_stop_id        VARCHAR NOT NULL,
    transfer_type     TINYINT,
    min_transfer_time INTEGER,
    PRIMARY KEY (from_stop_id, to_stop_id)
);

CREATE TABLE IF NOT EXISTS _meta (key VARCHAR PRIMARY KEY, value VARCHAR);
```

**Skip:** `shapes.txt` (map rendering only) and `calendar_dates.txt` (empty file).

**Realtime observations schemas to create:**

```sql
CREATE SEQUENCE IF NOT EXISTS obs_seq;
CREATE TABLE IF NOT EXISTS train_observations (
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
    delay_seconds            INTEGER,      -- NULL if no schedule match; positive=late
    next_stop_id             VARCHAR,
    next_stop_predicted_arrival TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_obs_route    ON train_observations(route_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_station  ON train_observations(location_parent_station, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_tripdate ON train_observations(rt_trip_id, service_date);

-- Per-stop predictions (training labels for future ML models)
CREATE TABLE IF NOT EXISTS stop_predictions (
    observation_id    UINTEGER NOT NULL,  -- FK to train_observations.id
    stop_id           VARCHAR NOT NULL,
    stop_sequence     SMALLINT NOT NULL,
    predicted_arrival TIMESTAMPTZ,
    scheduled_arrival_seconds INTEGER,
    PRIMARY KEY (observation_id, stop_sequence)
);
```

**Done when:**
```python
conn = duckdb.connect(":memory:")
initialize(conn, static_dir="static/")
assert conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 1488
assert conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 20310
assert conn.execute("SELECT MAX(arrival_seconds) FROM stop_times").fetchone()[0] > 86400
```

---

## Phase 3 — `src/graph.py`

**Goal:** Build a `SubwayGraph` class wrapping a `networkx.DiGraph`. Stations as nodes, scheduled travel times as edge weights.

**Concepts to understand:**

**NetworkX** — https://networkx.org/documentation/stable/
- `nx.DiGraph()` — directed graph. Edges have direction (A→B is not the same as B→A).
- Add nodes: `G.add_node("101", station=Station(...), trains=[])` — arbitrary keyword args become node attributes
- Add edges: `G.add_edge("101", "103", weight=90.0, routes=["1"])` — same for edge attributes
- Access node attributes: `G.nodes["101"]["station"]`
- Access edge attributes: `G["101"]["103"]["weight"]`
- Check edge exists: `G.has_edge("101", "103")`
- Neighbors: `G.successors("101")` (nodes reachable from 101), `G.predecessors("101")`
- Relevant docs: https://networkx.org/documentation/stable/tutorial.html, https://networkx.org/documentation/stable/reference/classes/digraph.html

**The platform → station mapping:**
`stop_times` references platform IDs like `"101S"`. Your graph nodes are parent stations like `"101"`. Every platform stop_id ends in `N` or `S` — so `parent_station = stop_id[:-1]`. Use this formula everywhere; don't look it up in the database for each individual stop.

**Building edges efficiently:**
Don't loop through 562K stop_time rows in Python — it'll be slow and complex. Instead, write a single SQL query that joins `stop_times` to itself on `trip_id` with `stop_sequence + 1`, computes the travel time between each pair of consecutive stops, and aggregates by `(route_id, from_station, to_station)` using `MEDIAN()`. DuckDB handles this in seconds. The result is a clean list of `(route_id, from_station, to_station, median_seconds)` tuples to iterate in Python.

**SQL query to compute route edges:**
```sql
WITH consecutive AS (
    SELECT t.route_id,
           st1.parent_station AS from_station,
           st2.parent_station AS to_station,
           (st2.arrival_seconds - st1.departure_seconds) AS travel_seconds
    FROM stop_times st1
    JOIN stop_times st2 ON st1.trip_id = st2.trip_id
                       AND st2.stop_sequence = st1.stop_sequence + 1
    JOIN trips t ON st1.trip_id = t.trip_id
    WHERE st2.arrival_seconds > st1.departure_seconds
      AND st1.parent_station != st2.parent_station
)
SELECT route_id, from_station, to_station,
       MEDIAN(travel_seconds) AS median_travel_seconds
FROM consecutive
GROUP BY route_id, from_station, to_station;
```

**Multi-route edges:**
Some station pairs are served by multiple routes (e.g. the 2 and 3 both stop at several of the same stations). When you add an edge that already exists, update its `routes` list and recalculate the combined `weight` rather than overwriting. Use `G.has_edge(a, b)` before calling `G.add_edge`.

**Transfer edges:**
The `transfers.txt` file has pairs of stops that are walkable transfers. Add bidirectional directed edges (two separate `add_edge` calls) for each transfer with `edge_type="transfer"` and `weight=min_transfer_time` in seconds.

**Updating live train positions:**
Each time the poller runs, you'll want to update which trains are at which station. The simplest approach: clear the `trains` list on every node, then re-populate from the latest poll results. This is `O(nodes + trains)` and fast.

**Class API to implement:**
```python
class SubwayGraph:
    graph: nx.DiGraph

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None
    def build(self) -> None
    def update_trains(self, trains: list[Train]) -> None
    def get_station(self, station_id: str) -> Optional[Station]
    def get_trains_at(self, station_id: str) -> list[Train]
```

**Done when:**
```python
g = SubwayGraph(conn)
g.build()
G = g.graph
print(G.number_of_nodes())   # ~496 parent stations
print(G.number_of_edges())   # several thousand
print(G["101"]["103"])        # edge between Van Cortlandt and 238 St on the 1 train
```

---

## Phase 4 — `src/ingestion.py`

**Goal:** A `Poller` class that fetches all 8 live feeds every 30 seconds, converts trips to `Train` objects, computes delays against the schedule, writes observations to DuckDB, and updates the graph.

**Concepts to understand:**

**nyct-gtfs** — https://github.com/Andrew-Dickinson/nyct-gtfs
- `NYCTFeed("A")` fetches the ACE/FS/H feed immediately. Feed groups: `"1"` (1-7/S), `"A"` (ACE), `"B"` (BDFM), `"G"`, `"J"` (JZ), `"N"` (NQRW), `"L"`, `"SI"`
- `feed.filter_trips(underway=True)` — only trains currently moving (not scheduled future trips)
- `trip.route_id`, `trip.direction`, `trip.headsign_text`, `trip.location` (current platform stop_id), `trip.location_status`, `trip.last_position_update`, `trip.stop_time_updates`
- Each `StopTimeUpdate`: `.stop_id`, `.stop_name`, `.arrival` (a `datetime` or `None`)

**Python `datetime` and timezones** — https://docs.python.org/3/library/datetime.html
- Always use timezone-aware datetimes for feed timestamps: `datetime.now(tz=timezone.utc)`
- `nyct-gtfs` returns `datetime` objects — check if they're timezone-aware before arithmetic
- Converting a datetime to seconds past midnight: `(dt - datetime.combine(dt.date(), time.min, tzinfo=dt.tzinfo)).total_seconds()`

**Service date convention:**
GTFS schedules use a "service date" concept: overnight trains (running after midnight) belong to the *previous* calendar day's schedule. A train running at 1:30 AM Tuesday is on Monday's schedule, with `arrival_seconds = 25 * 3600 + 30 * 60`. Derive the service date: if current local time is before 3 AM, use yesterday's date.

**Delay computation:**
For each live train, find the first `StopTimeUpdate` with a non-None `arrival`. Call `lookup_scheduled_arrival` with the RT trip ID, that stop's ID, and the service date. Subtract the scheduled seconds from the predicted seconds-past-midnight to get delay. Positive = late, negative = early, `None` = no schedule match found.

**Polling loop with drift correction:**
A naive `time.sleep(30)` loop will drift over time (each poll takes some seconds to run). Use `time.monotonic()` to measure elapsed time and sleep only the *remainder*:
```python
start = time.monotonic()
self._poll_once()
elapsed = time.monotonic() - start
time.sleep(max(0.0, 30 - elapsed))
```

**Signal handling for clean shutdown** — https://docs.python.org/3/library/signal.html
- Handle `SIGINT` (Ctrl+C) and `SIGTERM` gracefully by setting a flag that stops the loop, rather than letting the process die mid-write.

**Error isolation:**
Wrap each feed fetch in its own `try/except`. A single feed returning a 503 shouldn't kill the whole poll — log the error to stderr and continue with the other 7 feeds.

**`stop_predictions` table:**
For each train's `stop_time_updates`, write a row per upcoming stop to the `stop_predictions` table. This is the training label data for future ML models. Each row: `observation_id` (FK to `train_observations`), `stop_id`, `stop_sequence`, `predicted_arrival`.

**Key functions/classes to implement:**
```python
FEED_GROUPS = ["1", "A", "B", "G", "J", "N", "L", "SI"]

def parse_train(trip, observed_at: datetime) -> Train
def compute_delay(conn, train: Train, stop_time_updates, service_date: date) -> Optional[int]
def _derive_service_date(dt: datetime) -> date

class Poller:
    def __init__(self, conn, graph: SubwayGraph, interval_seconds: int = 30)
    def run(self) -> None      # blocking; uses time.monotonic for drift-corrected sleep
    def stop(self) -> None
    def _poll_once(self) -> None
```

**`__main__` block structure:**
```python
if __name__ == "__main__":
    conn = get_connection("mta.duckdb")
    initialize(conn, static_dir="static/")
    graph = SubwayGraph(conn)
    graph.build()
    poller = Poller(conn, graph)
    # register SIGINT/SIGTERM handlers to call poller.stop()
    poller.run()
    conn.close()
```

**Done when:**
Running `python src/ingestion.py` produces stderr output every ~30 seconds, and `mta.duckdb` grows in size. Query `SELECT COUNT(*) FROM train_observations` — it should increment with each poll.

---

## Phase 5 — `src/analytics.py`

**Goal:** Query functions over the accumulated `train_observations` data. All functions take a DuckDB connection, return plain Python dicts/lists.

**Concepts to understand:**

**Analytical SQL patterns you'll use:**

`GROUP BY` + aggregation:
```sql
SELECT route_id, AVG(delay_seconds), COUNT(*)
FROM train_observations
WHERE delay_seconds IS NOT NULL
GROUP BY route_id
ORDER BY AVG(delay_seconds) DESC
```

Time-of-day grouping — use DuckDB's timezone-aware extraction:
```sql
EXTRACT(hour FROM observed_at AT TIME ZONE 'America/New_York') AS hour
```

Rolling windows for `delay_trend` — DuckDB has `time_bucket` for this:
```sql
time_bucket(INTERVAL '5 minutes', observed_at) AS bucket
```
See DuckDB time functions: https://duckdb.org/docs/sql/functions/timestamp

**Parameterized optional filters:**
When a parameter like `since` or `route_id` is optional (`None` means "all"), build the WHERE clause conditionally in Python rather than trying to pass `NULL` as a parameter that disables a clause. Simplest approach: build a list of condition strings and join them with `AND`.

**Returning dicts instead of tuples:**
`conn.execute(...).fetchdf()` returns a pandas DataFrame — easy to convert to `list[dict]` with `.to_dict(orient='records')`. Or use `conn.execute(...).fetchall()` and zip with column names manually.

**Functions to implement:**

| Function | Returns |
|---|---|
| `avg_delay_by_route(conn, since=None)` | `[{route_id, avg_delay_seconds, sample_count}]` ordered desc |
| `on_time_pct_by_route(conn, threshold_s=300, since=None)` | `[{route_id, on_time_pct, total_obs}]` |
| `on_time_pct_by_station(conn, station_id, threshold_s=300)` | `{station_id, on_time_pct, avg_delay_seconds, total_obs}` |
| `avg_delay_by_hour(conn, route_id=None)` | `[{hour, avg_delay_seconds, sample_count}]` for hours 0–23 |
| `delay_trend(conn, route_id, window_minutes=60)` | `[{bucket_start, avg_delay_seconds}]` in 5-min buckets |

All queries filter `WHERE delay_seconds IS NOT NULL`.

**Done when:**
After a few hours of data collection, `avg_delay_by_route(conn)` returns a non-empty list with realistic delay values, and `avg_delay_by_hour(conn)` shows a pattern (delays tend to be worse during rush hours).

---

## General References

| Topic | Link |
|---|---|
| DuckDB docs | https://duckdb.org/docs/ |
| DuckDB CSV import | https://duckdb.org/docs/data/csv/overview |
| DuckDB SQL functions | https://duckdb.org/docs/sql/functions/overview |
| DuckDB time functions | https://duckdb.org/docs/sql/functions/timestamp |
| NetworkX tutorial | https://networkx.org/documentation/stable/tutorial.html |
| NetworkX DiGraph API | https://networkx.org/documentation/stable/reference/classes/digraph.html |
| Python dataclasses | https://docs.python.org/3/library/dataclasses.html |
| Python datetime | https://docs.python.org/3/library/datetime.html |
| Python signal handling | https://docs.python.org/3/library/signal.html |
| Python typing | https://docs.python.org/3/library/typing.html |
| GTFS static spec | https://gtfs.org/documentation/schedule/reference/ |
| GTFS-RT spec | https://gtfs.org/documentation/realtime/reference/ |
| nyct-gtfs README | https://github.com/Andrew-Dickinson/nyct-gtfs |

---

## Order of attack and suggested checkpoints

```
Week 1:  models.py → db.py (schema + CSV loading) → verify row counts
Week 2:  graph.py → verify node/edge structure manually
Week 3:  ingestion.py → get first poll writing to disk
Week 4+: Let poller run, fix bugs, start analytics.py as data accumulates
```

Come back and ask for help if you get stuck on a specific concept, want something explained more deeply, or want a function reviewed without having it rewritten for you.
