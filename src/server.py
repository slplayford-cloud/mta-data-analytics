#!/usr/bin/env python3
"""
FastAPI server.

Serves pre-built static GeoJSON, streams train positions over WebSocket, and
answers station queries from memory. The poller runs as a task on this server's
own event loop, so there is no thread bridge.

Run:  python run_server.py
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import cast

import orjson
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.routing import APIRouter
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from supabase import create_client

from src.cache import StaticDataCache
from src.config import config
from src.poller import Poller, TrainPosition
from src import queries
from src.schedule import ScheduleIndex

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

STATIC_CACHE = "public, max-age=3600, immutable"
NO_CACHE = "no-cache, no-store"

# Fields that never change for a trip. Sent once in the snapshot, omitted from
# every later delta.
_STATIC_FIELDS = frozenset(
    {"route_id", "direction", "headsign", "shape_id", "nyc_train_id"}
)


class TrainStream:
    """Holds the live train set and pushes deltas to connected clients.

    New clients get one full snapshot; everyone else gets only what changed.
    With ~800 trains and a 15s cycle that is the difference between resending
    the world every poll and sending the handful of trains that actually moved.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._trains: dict[str, dict] = {}
        self._snapshot: bytes = b""

    @property
    def trains(self) -> list[dict]:
        return list(self._trains.values())

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        if self._snapshot:
            try:
                await websocket.send_bytes(self._snapshot)
            except Exception:
                self._clients.discard(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    def publish(self, positions: list[TrainPosition], now: datetime) -> None:
        """Diff against the previous cycle and broadcast the change."""
        incoming = {position.trip_id: asdict(position) for position in positions}

        upserts: list[dict] = []
        for trip_id, train in incoming.items():
            previous = self._trains.get(trip_id)
            if previous is None:
                upserts.append(train)
                continue
            changed = {
                key: value for key, value in train.items()
                if key not in _STATIC_FIELDS and previous.get(key) != value
            }
            if changed:
                changed["trip_id"] = trip_id
                upserts.append(changed)

        removed = [trip_id for trip_id in self._trains if trip_id not in incoming]

        self._trains = incoming
        self._snapshot = orjson.dumps({
            "type": "snapshot",
            "ts": now.timestamp(),
            "trains": list(incoming.values()),
        })

        if not upserts and not removed:
            return

        payload = orjson.dumps({
            "type": "delta",
            "ts": now.timestamp(),
            "upsert": upserts,
            "remove": removed,
        })
        asyncio.get_running_loop().create_task(self._broadcast(payload))

    async def _broadcast(self, payload: bytes) -> None:
        dead = []
        for websocket in list(self._clients):
            try:
                await websocket.send_bytes(payload)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self._clients.discard(websocket)


cache: StaticDataCache
stream: TrainStream
stop_info: dict[str, dict] = {}
_stop_info_bytes: bytes = b"{}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global cache, stream, _stop_info_bytes

    db = create_client(config.supabase_url, config.supabase_service_key)

    cache = StaticDataCache(config.static_dir)
    cache.build()

    schedule = ScheduleIndex(config.static_dir)
    schedule.build()

    _stop_info_bytes = _load_stop_info(db)
    stream = TrainStream()

    poller = Poller(
        db, schedule,
        interval=config.poll_interval,
        on_positions=stream.publish,
    )
    task = asyncio.create_task(poller.run())

    yield

    poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _load_stop_info(db) -> bytes:
    """Page through stop_info once at startup.

    PostgREST caps a response at 1000 rows and the table has ~1488, so a single
    select silently drops everything after "G20" -- which is what used to make
    station names come back null for the whole L line.
    """
    page_size = 1000
    offset = 0
    try:
        while True:
            page = (
                db.table("stop_info")
                .select("stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            rows = cast(list[dict], page.data)
            for row in rows:
                stop_info[row["stop_id"]] = {
                    "name": row["stop_name"],
                    "lat": row["stop_lat"],
                    "lon": row["stop_lon"],
                }
            if len(rows) < page_size:
                break
            offset += page_size
        log.info("stop_info loaded: %d stops", len(stop_info))
    except Exception as exc:
        log.warning("stop_info load failed: %s", exc)
    return orjson.dumps(stop_info)


app = FastAPI(lifespan=lifespan, title="MTA Subway Map API")
app.add_middleware(GZipMiddleware, minimum_size=1024)
router = APIRouter(prefix="/api")


def _json(content: bytes, cache_control: str = STATIC_CACHE) -> Response:
    return Response(content=content, media_type="application/json",
                    headers={"Cache-Control": cache_control})


@router.get("/config")
async def get_config() -> Response:
    """Browser-safe settings. The Mapbox token here must be a public pk. token."""
    return _json(orjson.dumps({"mapboxToken": config.mapbox_token}), NO_CACHE)


@router.get("/stations")
async def get_stations() -> Response:
    return _json(cache.stations_geojson)


@router.get("/routes")
async def get_routes() -> Response:
    return _json(orjson.dumps(cache.routes_meta))


@router.get("/all-shapes")
async def get_all_shapes() -> Response:
    return _json(cache.all_shapes_bytes)


@router.get("/shape-index")
async def get_shape_index() -> Response:
    return _json(cache.shape_index_bytes)


@router.get("/stop-info")
async def get_stop_info() -> Response:
    return _json(_stop_info_bytes)


@router.get("/station/{station_id}/arrivals")
async def get_station_arrivals(station_id: str) -> Response:
    """Answered from the in-memory train set, so no database round trip."""
    arrivals = [
        train for train in stream.trains
        if train.get("loc_station") == station_id
        or (train.get("next_stop") or "").startswith(station_id)
    ]
    arrivals.sort(key=lambda train: train.get("next_arr") or "")

    info = stop_info.get(station_id)
    return _json(orjson.dumps({
        "station_id": station_id,
        "station_name": info.get("name") if info else None,
        "arrivals": arrivals[:12],
    }), NO_CACHE)


@router.get("/train/{trip_id}")
async def get_train(trip_id: str) -> Response:
    for train in stream.trains:
        if train.get("trip_id") == trip_id:
            return _json(orjson.dumps(train), NO_CACHE)
    raise HTTPException(status_code=404, detail="Train not found")


# ── Historical queries ────────────────────────────────────────────────────────
#
# The HTTP layer is done: parsing, validation and error handling all work. Each
# endpoint calls into src/queries.py, where the SQL is yours to write. Until
# then these return empty results rather than failing.

def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"{field} must be YYYY-MM-DD, got {value!r}")


def _query(fn, *args):
    """Run a query function, turning a missing database into a clear 503."""
    try:
        return fn(*args)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        log.exception("query failed")
        raise HTTPException(status_code=500, detail=f"query failed: {exc}")


@router.get("/history/station/{station_id}")
async def history_station(station_id: str, start: str, end: str) -> Response:
    rows = _query(queries.station_delays, station_id,
                  _parse_date(start, "start"), _parse_date(end, "end"))
    return _json(orjson.dumps(rows), NO_CACHE)


@router.get("/history/worst-stations")
async def history_worst_stations(start: str, end: str, limit: int = 20) -> Response:
    rows = _query(queries.worst_stations,
                  _parse_date(start, "start"), _parse_date(end, "end"), limit)
    return _json(orjson.dumps(rows), NO_CACHE)


@router.get("/history/route/{route_id}/by-hour")
async def history_route_by_hour(route_id: str, start: str, end: str) -> Response:
    rows = _query(queries.route_delays_by_hour, route_id,
                  _parse_date(start, "start"), _parse_date(end, "end"))
    return _json(orjson.dumps(rows), NO_CACHE)


@router.get("/history/trip/{service_date}/{trip_id}")
async def history_trip(service_date: str, trip_id: str) -> Response:
    row = _query(queries.trip_detail, _parse_date(service_date, "service_date"), trip_id)
    return _json(orjson.dumps(row), NO_CACHE)


@router.get("/history/summary/{day}")
async def history_summary(day: str) -> Response:
    rows = _query(queries.daily_summary, _parse_date(day, "day"))
    return _json(orjson.dumps(rows), NO_CACHE)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await stream.connect(websocket)
    try:
        while True:
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        stream.disconnect(websocket)
    except Exception:
        stream.disconnect(websocket)


class NoCacheStaticFiles(StaticFiles):
    """Serve web assets with no-cache so a deploy cannot strand users on old modules.

    ES module imports are fetched directly by the browser and would otherwise be
    cached hard. Unchanged files still return a fast 304.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.include_router(router)
app.mount(
    "/",
    NoCacheStaticFiles(directory=str(Path(__file__).parent.parent / "web"), html=True),
    name="web",
)
