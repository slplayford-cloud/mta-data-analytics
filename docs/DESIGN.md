# MTA Tracker — v2 Design

**Status:** proposed · **Date:** 2026-09-02 · **Supersedes:** current `main`

---

## 1. Context

The current system works but is built as a single-purpose live map. It polls 8 subway GTFS-RT
feeds every 15s, snapshots each trip's predicted schedule at first sight, records per-stop
departures to Supabase, and streams positions to a Leaflet map over WebSocket.

Three things it cannot do today, which are the goals of v2:

1. **Realtime mapping** is capped. Raster tiles + a hand-rolled 2D canvas of `ctx.arc()` dots
   tops out around 300 trains, and the wire format throws away most of what the feeds carry.
2. **There is no analytics.** `src/analytics.py` is a 0-byte file. There is no historical
   query path, no rollups, no retention policy, and no partitioning — so "query past months
   and years" would table-scan an unbounded heap.
3. **There is no insight layer.** Nothing predicts, ranks, or explains delay.

There is also a correctness problem that blocks both #2 and #3 (see §3.1): the "delay" the
system records today is *not* delay against the timetable.

### Decisions taken

| Area | Decision |
|---|---|
| Map renderer | **Mapbox GL JS v3** (vector, GPU) |
| Historical store | **Supabase Postgres only** — partitions + rollups, no second database |
| Prediction | **LightGBM** gradient-boosted models trained on our own history |
| Natural language | **Local LLM via Ollama** — no external API, no data egress |
| Feed scope | **Subway GTFS-RT only**, but fully exploited |

---

## 2. What we keep, change, and delete

**Keep as-is.** These are good and carry forward untouched:
- `web/js/WebSocketClient.js`, `web/js/InfoPanel.js`, `web/js/LineFilter.js` — all three are
  100% Leaflet-free, pure DOM/WebSocket. They port to Mapbox with zero changes.
- `src/cache.py`'s core idea: read static GTFS once at boot, pre-serialize to `orjson` bytes,
  serve as zero-copy responses. Extend it, don't replace it.
- The departure-detection trick in `src/ingestion.py` — a stop vanishing from
  `stop_time_updates` means the train just left it. That inference stays; only its *baseline*
  changes.
- `TrainState._interpolateAlongShape()` (`web/js/TrainManager.js:111`) — the binary search over
  cumulative shape distances is sound and is reused verbatim by the new renderer.

**Delete.**
- `src/graph.py` (120 lines) — dead. Zero importers anywhere. It queries duckdb tables
  (`stops`, `stop_times`, `trips`, `transfers`) that **no code in this repo ever creates**, and
  its SQL references columns (`stop_times.parent_station`, `arrival_seconds`) that don't exist
  in raw GTFS. Dropping it also drops `duckdb` and `networkx` from deps — they are pinned
  solely for this file.
- `src/analytics.py` — empty; replaced by a real `src/analytics/` package.
- The whole Leaflet dependency chain, including the four `.leaflet-*` CSS overrides
  (`web/css/style.css:208-221`).

**Rewrite.** `poller.py` (capture everything), `cache.py` (add the timetable index),
`server.py` (split routers), the entire map frontend, and the Supabase schema.

---

## 3. Foundations (do these first — everything else depends on them)

### 3.1 Fix the delay baseline — the single highest-value change

Today `delay_seconds` is computed against a snapshot of the *realtime prediction* taken when
the trip first appeared (`src/ingestion.py:48-81`). The README states this plainly: "Delays are
computed without static GTFS data."

That baseline drifts. It means "late relative to what the MTA guessed when this train showed
up," not "late relative to the published timetable." For a live map it is passable. For
year-scale analytics and for an ML target variable it is close to unusable — the label is
partly a function of the MTA's own prediction error, and it is not comparable across days.

The RT `trip_id` is a **suffix** of the static `trip_id` after the first `_`:

```
static trips.txt : ASP26GEN-1038-Sunday-00_000600_1..S03R
RT   trip_id     :                         000600_1..S03R
```

`static/stop_times.txt` (563,533 rows) holds the true scheduled `arrival_time` for every stop
of every trip, and `calendar.txt` maps `service_id` to day-of-week.

> **Corrected 2026-09-09.** `service_id` is *not* simply {Weekday, Saturday, Sunday}. The
> bundle published 2026-08-26 carries seven, several of them date-scoped —
> `Sunday-H-20260908-20261031`, `Saturday-H-20260526-20260907`. Resolve the service date
> against `calendar.txt`'s `start_date`/`end_date` ranges *and* the day-of-week columns; never
> by day name alone. That feed's `feed_version` is
> `20260826-X-long-term-supplement-trip-ids`, so the MTA appears to be moving supplement trip
> ids into the base bundle — worth re-measuring the §3.1b tier mix after each refresh.

**But that join only covers about three quarters of live trips.** Measured against all 8 feeds
on a Wednesday at 10:12am — 770 active trips:

| Join result | Share | What it is |
|---|---|---|
| Exact `trip_id` match | **72.6%** | Runs as timetabled |
| Path known, origin time not | **4.8%** | Extra trip inserted on a scheduled pattern |
| Path code entirely unknown | **22.6%** | Diversions, CBTC lines, novel patterns |

The misses are not random. Per-route exact-match rate:

```
R 100%   E 100%   3 100%   G 100%   A  98%   1  98%   M  97%   J  96%
N  94%   6  91%   Q  88%   5  76%   2  72%   D  62%   4  53%   C  50%
H  50%   B  48%   F  47%   W  17%   7   0%   L   0%   SI  0%
```

### 3.1a Why trips go unmatched

Four distinct causes, each needing a different answer. Three are structural, not surge-related:

1. **Different `trip_id` convention on some lines.** The L emits degenerate path codes
   (`L..S`, `L..N`) where static has `L..S01R`, `L..N02R`. Route 7 likewise never matches.
   Both are CBTC-operated. No amount of string handling fixes this — the identifier simply
   carries less information than the static one.
2. **A formatting inconsistency on SIR.** RT emits `SI.N03R` with one dot; static has
   `SI..N03R` with two. Normalizing `^(SI)\.(?!\.)` → `SI..` recovers 5 of 9 SIR trips.
   A one-line fix that takes an entire line from 0% to 56%.
3. **Diversion variants.** Path codes containing `X` — `2..N01X010`, `4..N06X045`,
   `5..N66X001` — are reroute patterns generated for planned work. They exist only in
   supplemental schedules the base GTFS bundle doesn't carry.
4. **Genuinely extra trips** — the surge-demand case. Gap trains, put-ins, and short-turns
   inserted to fill a headway hole. These run a *known* pattern at an *unscheduled* time.

<a id="sr-warning"></a>
> **The standard GTFS-RT signal for this does not work here.** `schedule_relationship` was
> `SCHEDULED` for all 770 trips — NYCT never sets `ADDED` or `UNSCHEDULED`. We cannot ask the
> feed which trips are extra; we have to infer it.

### 3.1b Resolve the baseline in tiers

Match on **stop pattern**, not on `trip_id`. Across 20,309 static trips there are only **218
distinct (route, stop-pattern) signatures** — a small, cacheable catalog. Measured recovery:

| Tier | Method | Coverage | `baseline_source` |
|---|---|---|---|
| 1 | Exact `trip_id` (+ SIR normalization) | 73.2% | `timetable` |
| 2 | Exact stop-pattern match | +8.7% | `pattern` |
| 3 | Contiguous-subsequence of a pattern | +13.1% | `pattern_tail` |
| 4 | Empirical, from our own history | +6.2% | `empirical` |
| — | Cold start, nothing available | — | `none` |

**Tiers 1–3 recover 93.8%.** Tier 3 matters because a train underway reports only its
*remaining* stops, so its stop list is a suffix of the full pattern, never the whole thing.

Tier 4 is the residue: trips whose stop combination appears in no static pattern. Critically,
these have **zero off-route stops** — every stop they serve belongs to the route. They are
novel short-turns and skip-stop patterns, not new geography. So a baseline built from our own
recorded median inter-stop runtimes for `(route, direction, stop_pair, hour-of-week)` covers
them properly.

For tiers 2–4 there is no scheduled *clock time* for the origin, so absolute
"minutes late vs timetable" is undefined. What is always computable is how long the train took
between two stops versus how long that segment should take. That drives the metric change below.

### 3.1c Three metrics, not one

| Column | Defined for | Meaning |
|---|---|---|
| `runtime_deviation` | **All tiers** | Actual inter-stop runtime − baseline runtime. **The primary metric and the ML target.** |
| `delay_vs_schedule` | Tier 1 only | Actual − published timetable. Absolute lateness; `NULL` elsewhere. |
| `delay_vs_prediction` | All trips | Actual − RT prediction at trip start. Measures MTA forecast error. |

`headway_seconds` (§5.2) needs no schedule at all, so it stays valid for every trip regardless
of tier — which makes it the most robust service-quality metric we have.

Every row records `baseline_source`, so analytics can filter to tier 1 for absolute claims and
use all tiers for relative ones. Never average across tiers without saying so.

### 3.1d Extra trips are a signal, not noise

A cluster of tier-2/3 trips on a route means the MTA added service — responding to crowding, or
recovering from an incident. That is one of the more interesting things in the data, and today
it is invisible.

Record `is_supplemental` on the trip and surface added-service rate as a first-class series:
per route, per hour, count of trips with `baseline_source != 'timetable'`. It becomes an ML
feature (§7.1), a live map indicator, and one of the better insight cards — *"6 extra trains
added on the F in the last hour, 3× the Wednesday norm."*

### 3.1e Build it

A **pattern catalog** in `src/cache.py`, alongside the existing timetable index:

```
timetable  : (service_id, rt_trip_suffix) → [(stop_id, stop_sequence, arrival_seconds), ...]
patterns   : (route_id, stop_tuple)       → pattern_id, [inter-stop runtime seconds]
pattern_tails : suffix-trie over stop_tuple → pattern_id     # tier 3 lookup
```

Keyed on seconds-past-midnight (values exceed 86400 for post-midnight trips — GTFS does this
deliberately and `src/models.py:StopTime` already documents it). Resolve the service date with
the existing `_derive_service_date()` (`src/poller.py:30`), which already handles the before-3AM
rollover. Build cost is a one-time ~35MB CSV pass at boot; cache to disk exactly as
`_build_stop_shape_index()` already does.

The tier-3 lookup must not be the naive scan used to measure this — build a suffix trie over the
218 patterns so resolution is O(pattern length), not O(218 × length).

Patterns observed in RT but absent from static get **written back to the catalog** with an
observation count. Over a few weeks this accumulates the real operating patterns the static feed
omits, and tier 4 shrinks on its own.

**A second, retroactive source exists.** The MTA publishes the *operated* schedule — supplements
and diversions included — as `MTA Subway Schedules` on data.ny.gov. It carries the diversion path
codes the static bundle lacks, so it can re-label historical `stop_visits` and raise tier 2–4
trips to true-timetable quality. It lags about three months, so it cannot serve live resolution;
the tiered resolver above is still required. See §6.4.

### 3.2 Write the missing migrations

Three of the four tables the app depends on have **no DDL in the repo at all** — `trip_schedules`,
`stop_visits`, and `stop_info` exist only in production, inferred from `.table(...)` calls. The
schema has already drifted: `src/poller.py:155` strips `loc_stop_id` from every upsert with the
comment *"if the column doesn't exist in the current schema,"* even though
`001_current_trains.sql` declares it.

Every table gets a checked-in migration before any v2 work lands. Also add RLS — there is
currently none, and the tables are exposed through PostgREST with default role grants.

### 3.3 Refresh the static GTFS feed — it expires this week

`static/calendar.txt` ends `20260907`. The bundled static feed goes stale in five days, which
will silently break the new timetable baseline.

Add `scripts/refresh_gtfs.py` to download and unpack the current MTA static bundle, rebuild
both derived indexes, and fail loudly if `feed_info`/`calendar` end-dates are within 14 days.
Also stop committing the raw feed: ~42MB of regenerable upstream text is tracked in git
(`stop_times.txt` alone is 34.7MB, no LFS). Gitignore `static/*.txt` and fetch on setup.

---

## 4. Pillar 1 — Realtime map

### 4.1 Capture everything the feeds carry

`nyct_gtfs` exposes considerably more than the 12 fields `_build_row()` currently keeps
(`src/poller.py:167`). Add, per train:

| Field | Source | Why it matters |
|---|---|---|
| `nyc_train_id` | `Trip.nyc_train_id` | Stable physical train identity across trips — enables consist tracking and turnaround analysis |
| `last_position_update` | `Trip.last_position_update` | **Stall detection.** The NYCT spec says: if header time − vehicle timestamp > 90s, the train is not moving and countdowns should freeze |
| `current_stop_sequence_index` | `Trip.current_stop_sequence_index` | Exact position along route; removes the shape-index guesswork |
| `has_delay_alert` | `Trip.has_delay_alert` | MTA's own delay flag |
| `scheduled_track` / `actual_track` | `StopTimeUpdate` | Track-level detail; `unexpected_track_arrival` is a strong reroute/disruption signal |
| `departure_time` | `Trip.departure_time` | Origin terminal departure — the spec's recommended stall reference |
| `trip_replacement_periods` | `NYCTFeed` | Which parts of the schedule the RT feed claims to replace |
| `last_generated` | `NYCTFeed.last_generated` | Feed staleness per division; surfaced in the UI |

Also switch the poller to `NYCTFeed.refresh_async()` (it exists, uses httpx) instead of the
current sync `requests.get` inside a `ThreadPoolExecutor` — the poller becomes an asyncio task
on the server's own loop instead of a daemon thread, which removes the
`run_coroutine_threadsafe` bridge in `WebSocketManager.schedule_broadcast()`.

### 4.2 Wire protocol: delta frames

Today every WebSocket frame is the **full** train list, JSON-encoded, sent every 15s
(`src/poller.py:139`), and the client rebuilds its whole map from it
(`web/js/WebSocketClient.js:36`). With the richer payload above and ~800 trains, full snapshots
get expensive fast.

Change to: **one snapshot on connect, deltas thereafter.**

```
{ type: "snapshot", ts, trains: [...] }          // on connect only
{ type: "delta", ts, upsert: [...], remove: [...] }  // every poll
```

Only fields that changed go in `upsert`. Static-per-trip fields (`route_id`, `direction`,
`headsign`, `shape_id`, `nyc_train_id`) are sent once and never repeated. Realistically this is
a 5–10× bandwidth reduction. Keep JSON initially — measure before reaching for a binary codec;
`orjson` + gzip is already fast and the complexity budget is better spent elsewhere.

### 4.3 Mapbox GL JS v3

The Leaflet coupling surface is narrow — roughly a dozen call sites, all enumerated below — so
this is a contained port, not a rewrite of the frontend.

| Leaflet (today) | Mapbox GL v3 |
|---|---|
| `L.map()` + `L.tileLayer(stadiamaps)` | `new mapboxgl.Map({ style: 'mapbox://styles/mapbox/standard' })` |
| dark raster tiles | `map.setConfigProperty('basemap','lightPreset','night')` |
| `L.geoJSON()` per route | one `geojson` source + `line` layers |
| `L.canvas()` + `L.circleMarker()` ×496 | one `circle` layer + one `symbol` layer for labels |
| `latLngToContainerPoint()` | `map.project()` |
| manual hit-test in `_nearestTrain()` | `map.queryRenderedFeatures()` |
| `map.on('move zoom viewreset')` | separate `map.on('move')` / `map.on('zoom')` |
| zoom-stepped radius in JS | `['interpolate',['linear'],['zoom'],...]` paint expression |

**Trains.** Replace the detached-canvas overlay entirely. Trains become a `geojson` source
driven by a 30fps `requestAnimationFrame` loop calling `source.setData()` with a preallocated
feature array. 800 point features per `setData` is ~1ms — well within budget, and it buys GPU
rendering, free zoom-responsive styling via expressions, and native picking.

Keep `TrainState` and its interpolation logic; it just emits features instead of `ctx.arc()`
calls. Two fixes to carry over:
- `_precomputeIndices()` measures distance in raw lon/lat degrees
  (`web/js/TrainManager.js:78-86`), which is anisotropic at NYC's latitude. Scale longitude by
  `cos(40.75°) ≈ 0.758`.
- `_nearestTrain()` returns the *first* hit within radius, not the nearest
  (`web/js/TrainManager.js:294`). `queryRenderedFeatures` makes this moot.
- Add the `destroy()` / `cancelAnimationFrame` that the current loop lacks.

**Routes — the big visual win.** All trunk routes currently draw on top of each other, so the
Lexington Ave trunk renders as one line, not four. Mapbox's `line-offset` paint property, with
a per-route offset computed from trunk membership, gives the classic parallel-lines subway map.
This is the change that will most obviously *look* like an upgrade.

Serve simplified geometry: the 257 shapes total ~151K vertices (4.7MB). Apply
Douglas–Peucker server-side at a few tolerances and pick by zoom. `GZipMiddleware` is already
mounted (`src/server.py:167`).

**New live layers** now that the data exists: stalled-train styling driven by
`last_position_update` drift, a station-level heat layer colored by current mean
`delay_vs_schedule`, and per-division feed-staleness indicators.

**Token handling.** Mapbox needs `mapboxgl.accessToken`. The frontend has **no build step**
(raw ES modules, CDN `<script>`), so add a tiny `GET /api/config` returning
`{ mapboxToken }` read from env. Use a URL-restricted public token (`pk.`), never a secret one.

---

## 5. Pillar 2 — Analytics on Postgres

### 5.1 The volume constraint drives the design

Rough steady-state estimate, ~800 active trains averaging a stop every ~2 minutes:

| Grain | Rows/day | Rows/year |
|---|---|---|
| `stop_visits` (event) | ~430K | **~157M** |
| position snapshots @15s | ~2.1M | ~75B — **never store these** |

157M narrow rows/year is fine for Postgres *if and only if* it is partitioned and aggregated.
Unpartitioned with the current wide schema (it stores `stop_name` and `route_id` as text on
every row) it is not.

**Three-tier retention:**

| Tier | Grain | Retention | Approx size |
|---|---|---|---|
| `stop_visits` | one row per departure | **90 days**, monthly partitions | ~40M rows, ~4GB with indexes |
| `visits_hourly` | (route, station, direction, hour) | **forever** | ~13M rows/yr, ~1GB/yr |
| `visits_daily` | (route, station, direction, day) | **forever** | ~550K rows/yr, negligible |

Narrow the raw row first: drop `stop_name` (derivable from `stop_info`), drop `route_id` and
`direction` (derivable from `trip_id`), use `int`/`smallint` over `text` wherever possible.
That roughly halves it.

### 5.2 Schema

```sql
CREATE TYPE baseline_source AS ENUM
    ('timetable', 'pattern', 'pattern_tail', 'empirical', 'none');

CREATE TABLE stop_visits (
    service_date   DATE        NOT NULL,
    trip_id        TEXT        NOT NULL,
    stop_id        TEXT        NOT NULL,
    stop_sequence  SMALLINT,

    scheduled_arrival  TIMESTAMPTZ,  -- tier 1 only          (§3.1b)
    predicted_arrival  TIMESTAMPTZ,  -- RT snapshot at trip start
    actual_arrival     TIMESTAMPTZ,

    runtime_deviation    INTEGER,    -- all tiers. PRIMARY metric + ML target
    delay_vs_schedule    INTEGER,    -- tier 1 only, else NULL
    delay_vs_prediction  INTEGER,
    headway_seconds      INTEGER,    -- gap to prior train, same route+direction+stop
    dwell_seconds        SMALLINT,

    baseline    baseline_source NOT NULL,   -- which tier resolved this row
    pattern_id  INTEGER,                    -- FK → stop_patterns
    actual_track TEXT,

    PRIMARY KEY (service_date, trip_id, stop_id)
) PARTITION BY RANGE (service_date);

-- The pattern catalog: 218 signatures from static, plus patterns
-- discovered in RT and written back (§3.1e).
CREATE TABLE stop_patterns (
    pattern_id   SERIAL PRIMARY KEY,
    route_id     TEXT NOT NULL,
    direction    CHAR(1),
    stop_tuple   TEXT[] NOT NULL,
    runtimes     INTEGER[],          -- baseline inter-stop seconds
    from_static  BOOLEAN NOT NULL,
    times_seen   INTEGER DEFAULT 0,
    first_seen   DATE,
    UNIQUE (route_id, stop_tuple)
);
```

`baseline` is `NOT NULL` deliberately: every row must declare how its numbers were derived, so
no query can silently mix absolute and relative measures.

`headway_seconds` is new and important — headway regularity is what riders actually experience,
and it is a far better service-quality metric than mean delay. It is cheap to compute at insert
via a window over the previous visit at the same `(route, direction, stop_id)`.

Rollups are **materialized tables refreshed incrementally**, not views — a `pg_cron` job every
hour aggregating only the last two hours, plus a nightly full-day pass. Materialized *views*
would require a full refresh over the whole partition set and are the wrong tool here.

BRIN indexes on `service_date` (naturally clustered, ~100× smaller than btree); btree on
`(stop_id, service_date)` and `(trip_id)`.

### 5.3 API and UI

New `src/analytics/` package and an `/api/analytics/*` router, all reading from rollups, never
from raw partitions:

- `/on-time?route=&from=&to=&grain=` — on-time % (|delay| ≤ 60s) time series
- `/heatmap?metric=&at=` — per-station values for map choropleth
- `/headway?route=&direction=&stop=` — headway distribution and bunching rate
- `/worst?dim=station|route|hour&window=` — ranked problem list
- `/compare?a=&b=` — period-over-period diff

Frontend gets a second view (`web/analytics.html`) sharing the same Mapbox instance and route
metadata. Charts via a small library — no build step exists, so an ES-module chart lib loaded
from CDN, consistent with how Mapbox itself will be loaded.

Two views come from open data rather than our own pipeline (§6.4): a **ridership** panel —
hourly entries per complex, overlaid on our measured delay so crowding and lateness can be read
together — and a dedicated **origin-destination flow map**, the single most striking thing in
the MTA catalog, which needs no realtime plumbing.

---

## 6. Derived data and the feature store

Everything below is computed from data we already collect. It serves two audiences at once —
analytics charts for humans, and features for the model in §7 — so it is defined once here
rather than twice.

### 6.1 Segments are the missing unit

Everything today is keyed on *stops*. But delay happens *between* stops, and the structure of
the network is what propagates it. Derived from the static feed:

| | |
|---|---|
| Distinct directed segments `(from_stop → to_stop)` | **1,149** |
| Segments shared by 2+ routes | **641 (56%)** |
| Maximum routes on one segment | **5** — B/D/F/FX/M on the 6th Ave trunk (`D15→D21`) |

**That 56% is the coupling mechanism.** A delay on the B does not stay on the B; it occupies
6th Ave trunk segments that the D, F and M need. Any model that treats routes as independent
cannot see this. Segment occupancy is the single most causally direct feature available.

Add a `segments` table derived at boot, and key the derived tables below on segment rather
than stop wherever the quantity is about movement rather than about a platform.

### 6.2 Three tiers of derived data

**Tier A — live network state.** Computed every poll in memory, drives the live map, and is the
feature vector at inference time. Persisted at 5-minute resolution, never at raw poll rate.

| Quantity | Grain | Why it predicts delay |
|---|---|---|
| `active_trains` | route × direction | Supply. The user's ask; also the denominator for everything else |
| `segment_occupancy` | segment | **Trains between you and the next station.** The direct physical cause |
| `headway_to_leader` / `_follower` | train | Bunching is self-reinforcing — the strongest short-horizon signal |
| `bunched_pairs`, `gap_count` | route × direction | Headway below 50% / above 150% of scheduled |
| `stalled_trains` | route | `last_position_update` drift > 90s (§4.1). A leading indicator of blockage |
| `terminal_punctuality` | terminal | Trailing 5 origin departures. Delay propagates outward from terminals |
| `runtime_deviation` p50/p90 | route × direction | Rolling 15 / 30 / 60 min. Momentum |
| `dwell_p90` | stop | **The only crowding proxy in the feed** — long dwells mean heavy boarding |

`dwell_p90` is worth dwelling on. There is no ridership number in GTFS-RT, but dwell time is a
direct consequence of how many people are boarding. It is the closest thing to a demand signal
available, and it connects straight back to the surge-demand question in §3.1.

**Tier B — one row per completed trip.** Roughly 7,000 trips/day, ~2.5M rows/year. Small, cheap,
and the richest table in the system.

```sql
CREATE TABLE trip_summary (
    service_date      DATE NOT NULL,
    trip_id           TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    direction         CHAR(1),
    pattern_id        INTEGER,          -- §3.1e — REQUIRED for any runtime average
    baseline          baseline_source NOT NULL,
    is_supplemental   BOOLEAN,
    nyc_train_id      TEXT,             -- physical train, for turnaround chaining

    full_route_time     INTEGER,        -- origin → terminus, actual seconds
    baseline_route_time INTEGER,        -- same pattern, same hour-of-week
    origin_delay        INTEGER,        -- how late it started
    terminal_delay      INTEGER,        -- how late it finished
    max_delay           INTEGER,
    recovered_seconds   INTEGER,        -- origin_delay − terminal_delay; negative = fell behind
    worst_segment_id    INTEGER,        -- where it lost the most time
    turnaround_seconds  INTEGER,        -- gap since this train's previous trip ended

    PRIMARY KEY (service_date, trip_id)
);
```

> **Gotcha on "average full route time."** A naive `AVG(full_route_time)` per route is
> meaningless, because short-turns are mixed in. Measured from the static feed: the A ranges
> **10 to 117 minutes**, the 3 from 18 to 74, the R from 12.5 to 92. Always group by
> `pattern_id`. Median F full-route time is 96.5 min; median 4 is 66.5 min — but only within a
> pattern.

`turnaround_seconds` is the other propagation mechanism worth capturing: a train arriving late
departs late on its next trip. It requires chaining trips by `nyc_train_id` (§4.1), which is
exactly why that field is worth collecting.

**Tier C — rolling baselines.** Recomputed nightly, all small, all permanent.

| Table | Key | Rows |
|---|---|---|
| `segment_baseline` | route × direction × segment × hour-of-week | 1,149 × 168 ≈ **193K** |
| `dwell_baseline` | stop × route × hour-of-week | ≈ 250K |
| `headway_baseline` | route × direction × hour-of-week | ≈ 9K |
| `turnaround_baseline` | terminal × route × hour-of-week | ≈ 20K |

Each stores median, p90, and `n`. Computed from a trailing 8-week window. Rows with `n < 20`
are marked low-confidence and fall back to the day-type median rather than being trusted.

Storage for the whole derived layer:

| Table | Rows/day | Rows/year |
|---|---|---|
| `network_state` (5-min) | ~16K | ~5.9M |
| `segment_hourly` | ~27.6K | ~10M |
| `trip_summary` | ~7K | ~2.5M |
| Tier C baselines | — | ~470K total, rewritten nightly |

Under 20M rows/year combined — trivial next to `stop_visits`, and all of it permanent.

### 6.3 The leakage boundary

<a id="leakage"></a>
This is the part that silently ruins the model if it is wrong, and it will not announce itself —
a leaking model looks *excellent* in validation and fails in production. Four hard rules:

1. **Every feature uses only data timestamped strictly before the prediction moment.** No
   exceptions, including "harmless" ones.
2. **Baselines exclude the current service date.** A trailing 8-week window ending
   *yesterday* — never a window that includes today.
3. **Rolling windows are trailing, never centered.** A 60-minute window means the 60 minutes
   before now, not 30 either side.
4. **Nothing from the same trip's future stops.** Obvious in principle, easy to violate when
   joining `trip_summary` back onto `stop_visits`.

`is_supplemental` is safe as a per-trip flag — it is known at trip start. But *"supplemental
trips added on this route today"* is only safe as a trailing count. Compute it over the last 60
minutes, not the calendar day.

Enforce this structurally, not by discipline: build features through a single
`features_at(trip_id, timestamp)` function that takes the prediction timestamp as an argument
and physically cannot read past it. Every training row and every inference call goes through
the same function.

### 6.4 External data sources

Everything in this section is on `data.ny.gov` (Socrata), free, no key required, queryable with
`$select` / `$where` / `$group` / `$limit`. An app token lifts rate limits and is worth
registering. All figures below were verified against the live API on 2026-09-02.

**The join key is `MTA Subway Stations` (`39hk-dx4f`).** 496 rows — exactly the station count
our `StationManager` renders — carrying both `gtfs_stop_id` and `complex_id`. That single table
maps our GTFS world onto every MTA dataset below. It also carries `ada`, `structure`
(elevated/subway/open cut), `borough`, `daytime_routes`, `cbd`, and platform direction labels —
all useful as model features and map styling.

#### Ridership is published weekly, not annually

| | |
|---|---|
| Dataset | **MTA Subway Hourly Ridership: Beginning 2025** (`5wq4-mkjj`) |
| Rows | **43,835,841** |
| Coverage | 2025-01-01 → **2026-08-20** |
| Posting | **Weekly** — roughly a 13-day lag |
| Grain | hour × station complex × payment method × fare class |

Continuations: `wujg-7c2s` (2020–2024) and `t69i-h2me` (2017–2019, the pre-COVID baseline).
Together that is nine years of hourly ridership.

`ridership` and `transfers` are separate columns, and `fare_class_category` splits twelve ways
(OMNY vs MetroCard × Full Fare / Students / Seniors & Disability / Fair Fare / Unlimited).
Fare mix is itself a useful signal — student share collapses on school holidays.

Three constraints that shape how it can be used:

- **It is by station *complex*, not by route or platform.** Times Sq is one complex (611)
  covering 1/2/3, N/Q/R/W, 7 and S. Ridership cannot be attributed to a specific line there.
  The join from `gtfs_stop_id` is many-to-one.
- **It is entries plus transfers, not exits.** Destination information exists only in the
  origin-destination dataset below.
- **The 13-day lag makes it unusable as a live feature.** It is leakage-safe *only* as a
  historical baseline — "typical ridership at this complex for this hour-of-week, from prior
  weeks" — which is exactly the form §6.3 requires anyway.

That last point matters: this is the real demand signal we approximated with `dwell_p90`
(§6.2). Use both. `dwell_p90` is live but indirect; ridership is direct but lagged.

#### The daily schedules feed partly solves §3.1

**MTA Subway Schedules: 2026** (`g8es-h7gb`) — 35,552,324 rows, one per scheduled stop, with
`train_id`, `path_id`, `track`, `origin`/`destination_gtfs_stop_id`, `next_trip_time`, and
critically **`supplement_schedule_number`**. This is the *operated* schedule including
supplements — the thing the static GTFS bundle omits.

Verified: the diversion path codes missing from static are present here.

| Path code | In static GTFS | In this feed |
|---|---|---|
| `2..N01X010` | absent | **8,820 rows** |
| `5..N66X001` | absent | **4,752 rows** |
| `L..S` (RT convention) | absent | absent |

So it retroactively fixes the diversion-variant share of the 27% gap, and `next_trip_time`
supplies `turnaround_seconds` (§6.2) directly rather than by chaining.

**But the data lags about three months** — despite "Daily" posting, the latest `service_date`
available is 2026-06-02. So it is a **backfill and training-label source, not a live one.** The
tiered resolver in §3.1b remains necessary for realtime. Use this to re-label historical
`stop_visits` once, raising training-label quality for the trips that resolved to tier 2–4.

It does *not* fix the L and 7. Their path codes here are still `L..S01R`-style, confirming the
degenerate `L..S` seen in RT is an RT-side convention, not a schedule gap.

#### Benchmarks: the MTA's own delay metrics

**Customer Journey-Focused Metrics** (`r7qk-6tcy`, monthly, 2015+) publishes
`additional_platform_time`, `additional_train_time`, `over_five_mins_perc`,
`customer_journey_time` and `num_passengers` per line per month.

This is how the MTA itself measures delay. Our per-stop data is far finer, but these are the
numbers the agency and the press quote — so they are the right **validation benchmark**. If our
computed line-month delay diverges badly from `additional_train_time`, that is a bug signal.

Related monthly line-level series, all useful as context and none as live features:

| Dataset | ID | What it adds |
|---|---|---|
| Delay-Causing Incidents | `g937-7k7c` | **Why** — incidents by reporting category |
| Trains Delayed | `9zbp-wz3y` | Delay counts by category |
| Major Incidents | `ereg-mcvp` | Significant disruptions, 2015+ |
| Terminal On-Time Performance | `f6rf-2a3t` | On-time trips / scheduled trips |
| Wait Assessment | `s666-h6b7` | Headway regularity, the MTA's version of §6.2 |

**Service Alerts** (`7kct-peq7`) is the exception worth ingesting properly: 521,748 rows through
2026-07-30, one per alert, with free-text `header` and `description`. Two uses — labelling
historical disruption windows so the model can learn around them, and feeding the Ollama
narration layer (§7.2) real language about *why* a line was degraded.

#### The visualization: origin-destination flows

**MTA Subway Origin-Destination Ridership Estimate** (`y2qv-fytt` for 2025, `nqnz-e9z9` for
2022) is the annual drop — genuinely static, not updated. Grain is month × day-of-week ×
hour-of-day × origin complex × destination complex, with lat/lon on **both** ends and an
`estimated_average_ridership` weight.

That is a complete OD matrix with coordinates, which makes it the one dataset here that earns a
dedicated page. An animated flow map — arcs weighted by ridership, scrubbing through hour of
day, split by weekday/weekend — shows the city's commute inverting between morning and evening.
Mapbox renders this well natively: a `line` layer with `line-gradient` over a GeoJSON of great-
circle arcs, or a heat layer for destination density. It is the best "cool visualization" in the
catalog and it needs no realtime plumbing at all.

#### Weather, holidays, events

- **Weather — recommended.** Precipitation, temperature and wind are genuinely predictive: rain
  and snow lengthen dwells and raise signal-failure rates. NWS (`api.weather.gov`) is free,
  hourly, needs no key, and one station is enough at this granularity. Use the *forecast as of
  the prediction time*, never the observed value — observed weather after the fact is leakage
  under rule 1.
- **Holidays and school calendar — recommended.** A static lookup table, near-zero cost, and it
  explains a good share of the residual variance on off-pattern days. Pairs with the fare-class
  student share above.
- **Event schedules — defer.** MSG, Barclays, Yankee and Mets games produce large, sharply
  localized surges, but there is no clean free feed and the scraping is ongoing maintenance. The
  effect will surface as unexplained residuals at specific complexes, which is itself a good way
  to confirm it is worth the effort.

#### Ingest shape

All of the above are batch, none are realtime. One `src/opendata/` package with a Socrata client
and a per-dataset sync job on `pg_cron`:

| Dataset | Sync | Volume |
|---|---|---|
| Stations (`39hk-dx4f`) | Monthly | 496 rows — a lookup table |
| Hourly ridership (`5wq4-mkjj`) | Weekly, incremental on `transit_timestamp` | ~180K rows/week |
| Schedules (`g8es-h7gb`) | Monthly backfill | Bulk, one-time per month |
| Line-month metrics (5 datasets) | Monthly | Hundreds of rows each |
| Service alerts (`7kct-peq7`) | Monthly, incremental on `date` | ~8K rows/month |
| OD estimate (`y2qv-fytt`) | Once | Static |

Incremental syncs page with `$where` on the timestamp plus `$offset`, storing a watermark per
dataset. Total steady-state addition is well under 10M rows/year.

### 6.5 What to build first

Ranked by predictive value per unit of effort:

1. `segment_occupancy` and `headway_to_leader` — the direct causes, cheap, live-computable
2. `segment_baseline` — the floor everything else is measured against
3. `trip_summary` with `recovered_seconds` — the recovery dynamics, small table, high value
4. `dwell_p90` — the only crowding proxy available
5. `turnaround_seconds` — the second propagation mechanism, needs `nyc_train_id`
6. `terminal_punctuality` — propagation source
7. Hourly ridership sync (§6.4) — the real demand signal, as a lagged historical baseline
8. Weather — the only exogenous signal worth the ingest
9. OD flow map — pure visualization, zero pipeline, high impact

---

## 7. Pillar 3 — Insights

### 7.1 LightGBM delay prediction

Two models, both trained on our own `stop_visits` history, both loaded in-process for
sub-millisecond inference.

**Model A — arrival delay regression.** For a train at stop *n*, predict `runtime_deviation` at
stops *n+1 … n+5*. Targeting runtime deviation rather than absolute delay means the model trains
on **all** trips, not just the 73% that match a timetable (§3.1c).

Features, all available at inference time:
- current `runtime_deviation`, and its trend over the last 3 stops
- `baseline` tier — the model should know how trustworthy its own label is
- `is_supplemental`, and the count of supplemental trips added on this route in the last hour
- `headway_seconds` to the train ahead, and to the one behind
- seconds since `last_position_update` (the stall signal from §4.1)
- hour-of-week, holiday flag
- route, direction, stop, `stop_sequence` (native categoricals — LightGBM handles these
  without one-hot)
- historical mean/p90 delay for this (stop, hour-of-week) from `visits_hourly`
- count of trains currently between this train and the next station (congestion proxy)
- `unexpected_track_arrival` on recent stops

**Model B — bunching classifier.** Binary: will headway to the following train fall below 50%
of scheduled headway within 15 minutes? Bunching is the failure riders feel most and it is
highly predictable from headway trend plus upstream dwell.

**Training loop.** Nightly job over the last 90 days of raw `stop_visits`. Walk-forward
validation — train on weeks 1–11, validate on week 12 — never a random split, which would leak
across time. Track MAE, and calibration for Model B. Version every model with its training
window and metrics; keep the previous one for one-command rollback.

Serve predictions through `/api/predict/trip/{trip_id}` and merge the next-stop prediction into
the WebSocket delta so the map can show predicted-vs-scheduled arrival directly.

**Honest expectation:** 5–15 minute horizons are genuinely predictable. Beyond ~30 minutes,
delay is dominated by exogenous incidents and the model will not beat the historical hour-of-week
baseline. Always ship that baseline alongside as the comparison — if the model doesn't beat it,
that's a real result worth seeing.

**Cold start:** LightGBM needs history that doesn't exist yet. Ship the hour-of-week baseline
first, accumulate 4–6 weeks of correctly-labeled data (§3.1 must land first), then train.

### 7.2 Local LLM via Ollama

A small local model (Qwen2.5 7B or Llama 3.1 8B) behind `src/insights/llm.py`, doing two jobs:

**Narration.** Turn model output and rollup queries into short insight cards: *"The 4/5 is
running 6 min late southbound through Manhattan, up from a 2 min average for a Tuesday 5pm.
Bunching detected between 86 St and 59 St."* Input is a compact JSON fact block assembled by
our own code — the model only writes prose, it never does arithmetic or retrieval.

**NL → SQL.** A "ask a question about the data" box, constrained hard:
- schema-in-prompt, restricted to the three rollup tables (never raw partitions)
- generated SQL runs as a **read-only role** with a statement timeout and a `LIMIT` ceiling
- parse and validate the SQL before execution; reject anything that isn't a single `SELECT`
- show the user the generated SQL alongside the result, always

Small local models are meaningfully weaker at SQL generation than frontier models, so the value
here comes from constraining the surface: few tables, a fixed set of query shapes, and validation
that fails closed. Design it so a wrong query is visibly wrong rather than silently plausible.

Ollama runs as a separate local service; the app degrades gracefully to no-narration if it is
unreachable.

---

## 8. Cross-cutting

**Config.** Two env vars exist today, both read unguarded via `os.environ[...]`, with no
`.env.example` anywhere — `python-dotenv` is pinned but never imported. Add a `src/config.py`
with a Pydantic settings model, an `.env.example`, and startup validation that fails with a
clear message. New vars: `MAPBOX_TOKEN`, `OLLAMA_HOST`, `ANALYTICS_DB_ROLE`.

**Testing.** There is currently no test file, no CI, no linter runner, and no `package.json`
anywhere in the repo. The only static analysis is a `[tool.basedpyright]` block with nothing
invoking it. Add: pytest with fixtures built from recorded GTFS-RT protobuf frames (so
ingestion is testable offline and deterministically), ruff, and a GitHub Actions workflow
running ruff + basedpyright + pytest.

**Dependencies.** `requirements.txt` and `pyproject.toml` are hand-maintained duplicate pinned
lists that already differ in name casing and can drift independently. `uv.lock` is committed —
make `pyproject.toml` the single source and generate `requirements.txt` from it, or drop it.

---

## 9. Delivery phases

Each phase is independently shippable and leaves the app working.

| # | Phase | Contents | Gate |
|---|---|---|---|
| 0 | **Foundations** | Migrations for all 4 tables + RLS; `refresh_gtfs.py`; timetable index; **pattern catalog + tiered baseline resolver (§3.1b)**; `runtime_deviation` recorded; delete `graph.py`/`analytics.py`; config module; pytest + CI | ≥93% of trips resolve to tier 1–3 over a full day; tier mix logged per route; CI green |
| 1 | **Full capture** | All §4.1 fields; async poller; delta protocol; stall detection; headway at insert; **`segments` table + live `segment_occupancy` (§6.1)** | Live map unchanged visually, payload 5×+ smaller; occupancy computed every poll |
| 2 | **Mapbox** | Port map; `line-offset` trunk lines; query-based picking; delay heat layer; stall styling | 800 trains at 60fps; visual parity + parallel trunks |
| 3 | **Analytics** | Partitions; rollup tables + `pg_cron`; `/api/analytics/*`; analytics view; **`trip_summary` + `network_state` + Tier C baselines (§6.2)**; **OD flow map (§6.4)** | Year-range query returns < 500ms; `trip_summary` populated per completed trip |
| 4 | **Baseline insights** | Hour-of-week baseline; anomaly flags vs baseline; insight cards; Ollama narration; **`src/opendata/` sync — stations, ridership, alerts (§6.4)**; weather + holidays; **`features_at()` (§6.3)** | Cards render; baseline beats naive; leakage test passes; ridership joins to our stations |
| 5 | **ML** | LightGBM A + B; nightly training; predictions in WS; NL→SQL | Model beats hour-of-week baseline on walk-forward MAE |

Phase 5 requires ~6 weeks of data from Phase 0, so the calendar gap is real and expected.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| **Static GTFS expires 2026-09-07** | Phase 0 item. Blocks the entire delay-baseline fix. |
| **27% of live trips don't match static by `trip_id`** | Measured, not assumed. Tiered pattern resolution recovers 93.8%; the rest use an empirical baseline. Every row records its `baseline` tier (§3.1b) |
| Routes 7 and L never match by `trip_id` at all | Structural — CBTC lines emit degenerate path codes. Pattern matching is the only route; verify tier-2/3 recovery per-line before Phase 3 |
| Tier-4 empirical baseline is circular — built from our own data | Only used for patterns static never had. Requires 4+ weeks of history; until then `runtime_deviation` is NULL for those trips, not guessed |
| Static bundle lacks supplemental/diversion schedules | Pattern write-back (§3.1e) accumulates real operating patterns over time. Revisit if MTA publishes supplemental GTFS separately |
| Postgres-only cost at 157M rows/yr | 90-day raw retention is the control. Revisit if raw retention needs to grow |
| Mapbox billing past 50k loads/mo | Verify current pricing before Phase 2; the port keeps a MapLibre escape hatch since the layer API is near-identical |
| Local LLM generates wrong-but-plausible SQL | Read-only role, rollup tables only, validate-before-execute, always show the SQL |
| **Feature leakage — a leaking model validates beautifully and fails live** | Every feature goes through one `features_at(trip_id, timestamp)` function that cannot read past its timestamp (§6.3). Add a test that shifts the timestamp back and asserts features change |
| Naive `AVG(full_route_time)` mixes short-turns with full runs | Always group by `pattern_id`. The A ranges 10–117 min; an ungrouped average is meaningless (§6.2) |
| Ridership is per station *complex*, not per route | Cannot attribute ridership to a line at shared complexes (Times Sq = 1/2/3/7/N/Q/R/W/S). The join is many-to-one; say so wherever the number is shown |
| Open-data schema or dataset ID changes upstream | Sync jobs validate expected columns and fail loudly rather than writing partial rows |
| ML underperforms the baseline | Ship the baseline as a first-class feature, not just a comparison. It's useful on its own |

---

## 11. Open questions

1. Retain raw `stop_visits` longer than 90 days? Longer history improves training but drives
   Supabase storage cost linearly.
2. Should `nyc_train_id` consist tracking be a Phase 1 feature or deferred? It enables
   turnaround analysis but has no UI yet.
3. Analytics as a second page, or a mode toggle inside the existing single-page app?
4. Weather ingest in Phase 4 as proposed, or earlier? It is cheap, but the feature is only
   testable once there is enough history to train against.
