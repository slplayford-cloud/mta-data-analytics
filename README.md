# MTA Data Analytics

This is a personal project which I am working on to explore solving a problem like NYC subway delays (something every new yorker has experienced) and build my technical skills.

## What it does

- Pulls data for 8 different train feeds from MTA realtime data(1/2/3/4/5/6/7, A/C/E, B/D/F/M, G, J/Z, N/Q/R/W, L, Staten Island Railway)
- Uses static and live data to build out expected arrival times for each stop
- Stores all the information in a personal supabase database for later analysis
- Also includes a simple (AI assisted coding) frontend in order to help users interact with the data


## Database tables

| Table | Purpose |
|---|---|
| `current_trains` | One row per active train; upserted every poll. Drives the live map. |
| `trip_schedules` | Full stop list + predicted arrival times, snapshotted when a trip is first seen. |
| `stop_visits` | One row per stop departure — scheduled vs actual arrival and delay in seconds. |

Simple database structure to start with data collection -- moving towards a more developed table for information caching

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
or use uv package manager to install

# Set environment variables
export SUPABASE_URL=...
export SUPABASE_SERVICE_KEY=...

# Start the server (map + poller together)
python run_server.py

# Or run the ingestion pipeline standalone
python -m src.ingestion
```

The server is available at `http://localhost:8000`.
