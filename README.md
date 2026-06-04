# MTA Data Analytics

A real-time NYC subway tracking system that polls all MTA GTFS-RT feeds every 15 seconds, records per-stop delay data for every active train, and serves a live interactive map over WebSocket.

## What it does

- Polls all 8 MTA GTFS-RT feed divisions in parallel (1/2/3/4/5/6/7, A/C/E, B/D/F/M, G, J/Z, N/Q/R/W, L, Staten Island Railway)
- Snapshots each train's predicted schedule at first sight and computes delay at every stop departure
- Writes per-stop delay records to Supabase (`trip_schedules`, `stop_visits`)
- Maintains a live `current_trains` table with position, next stop, and current delay for every active train
- Streams train positions to connected browser clients over WebSocket
- Serves a Leaflet map with real-time train dots, route shapes, and station arrival panels

## Architecture

```
MTA GTFS-RT feeds (8 divisions)
        │  15s poll
        ▼
  src/poller.py          — parallel feed refresh, ingestion orchestration, WS broadcast
  src/ingestion.py       — schedule snapshotting, departure detection, delay recording
        │
        ├── Supabase (PostgreSQL)
        │     ├── current_trains   — live position + delay per active train
        │     ├── trip_schedules   — predicted schedule snapshotted at trip start
        │     └── stop_visits      — actual vs scheduled arrival at each stop
        │
        └── src/server.py (FastAPI + uvicorn)
              ├── /api/stations    — GeoJSON station features
              ├── /api/routes      — route metadata + colors
              ├── /api/all-shapes  — all route polylines in one request
              ├── /api/station/{id}/arrivals  — answered from in-memory state
              └── /api/ws          — WebSocket stream of train positions
```

## Database tables

| Table | Purpose |
|---|---|
| `current_trains` | One row per active train; upserted every poll. Drives the live map. |
| `trip_schedules` | Full stop list + predicted arrival times, snapshotted when a trip is first seen. |
| `stop_visits` | One row per stop departure — scheduled vs actual arrival and delay in seconds. |

Delays are computed without static GTFS data. When a trip first appears in the feed the current RT predictions are snapshotted as the schedule baseline. On each subsequent poll, stops that disappear from the remaining-stops list indicate a departure; the last predicted arrival time before disappearance is written as the actual arrival, and delay is the difference from the snapshotted baseline.

## Tech Stack

| Layer | Technology |
|---|---|
| Feed parsing | `nyct-gtfs` 2.1.0 |
| API server | FastAPI + uvicorn |
| Database | Supabase (PostgreSQL) |
| Static GTFS | MTA `stop_times.txt`, `trips.txt`, `shapes.txt`, `stops.txt` |
| Frontend map | Leaflet 1.9.4 + vanilla JS |
| Serialisation | `orjson` |

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SUPABASE_URL=...
export SUPABASE_SERVICE_KEY=...

# Start the server (map + poller together)
python run_server.py

# Or run the ingestion pipeline standalone
python -m src.ingestion
```

The server is available at `http://localhost:8000`.
