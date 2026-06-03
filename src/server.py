#!/usr/bin/env python3
"""
FastAPI server — serves pre-built static GeoJSON from the StaticDataCache,
streams live train positions over WebSocket, and answers station arrival
queries from in-memory state (no Supabase round-trip per click).

Run:  python run_server.py
"""

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

import orjson
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRouter
from starlette.middleware.gzip import GZipMiddleware
from supabase import create_client, Client

from src.cache import StaticDataCache
from src.poller import Poller
from src import ingestion

# ── Singletons ────────────────────────────────────────────────────────────────

_cache: StaticDataCache | None = None
_db: Client | None = None
_poller: Poller | None = None
_ws_manager: "WebSocketManager | None" = None
_stop_info_bytes: bytes | None = None
_stop_info_map: dict[str, dict] = {}  # stop_id → {name, lat, lon, ...}

# 1-hour cache header for immutable static GTFS data
_STATIC_CACHE = "public, max-age=3600, immutable"
# No cache for live data
_NO_CACHE = "no-cache, no-store"


# ── WebSocket manager ─────────────────────────────────────────────────────────

class WebSocketManager:
    """Thread-safe manager for connected WebSocket clients."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._last_payload: bytes | None = None
        self._last_trains: list[dict] = []  # in-memory snapshot for arrivals queries

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        if self._last_payload:
            try:
                await ws.send_bytes(self._last_payload)
            except Exception:
                pass

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    @property
    def last_trains(self) -> list[dict]:
        return self._last_trains

    async def broadcast(self, payload: bytes, trains: list[dict]) -> None:
        self._last_payload = payload
        self._last_trains = trains
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_bytes(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    def schedule_broadcast(self, payload: bytes, trains: list[dict], loop: asyncio.AbstractEventLoop) -> None:
        asyncio.run_coroutine_threadsafe(self.broadcast(payload, trains), loop)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cache, _db, _poller, _ws_manager, _stop_info_bytes

    _db = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

    if hasattr(ingestion, "load_active_schedules"):
        try:
            ingestion.load_active_schedules(_db)
        except Exception as e:
            print(f"[server] schedule warm-up skipped: {e}")

    _cache = StaticDataCache()
    _cache.build()

    # Load stop_info once at startup — static GTFS data, no per-request DB hit.
    # PostgREST caps a single response at 1000 rows; stop_info has ~1488, so we
    # page through with .range() until a short page signals the end. Without this
    # everything alphabetically after "G20" (incl. all L-line stops) is dropped,
    # which is what made station_name come back null for e.g. L06.
    try:
        _PAGE = 1000
        offset = 0
        while True:
            page = (
                _db.table("stop_info")
                .select("stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station")
                .range(offset, offset + _PAGE - 1)
                .execute()
            )
            for row in page.data:
                _stop_info_map[row["stop_id"]] = {
                    "name":   row["stop_name"],
                    "lat":    row["stop_lat"],
                    "lon":    row["stop_lon"],
                    "type":   row["location_type"],
                    "parent": row["parent_station"],
                }
            if len(page.data) < _PAGE:
                break
            offset += _PAGE
        _stop_info_bytes = orjson.dumps(_stop_info_map)
        logging.getLogger(__name__).info("stop_info loaded: %d stops", len(_stop_info_map))
    except Exception as e:
        logging.getLogger(__name__).warning("stop_info load failed: %s", e)
        _stop_info_bytes = orjson.dumps({})

    _ws_manager = WebSocketManager()

    loop = asyncio.get_running_loop()
    _poller = Poller(
        _db,
        ws_broadcast=lambda p, rows: _ws_manager.schedule_broadcast(p, rows, loop),
    )
    t = threading.Thread(target=_poller.run, daemon=True, name="gtfs-poller")
    t.start()

    yield

    if _poller:
        _poller.stop()
        t.join(timeout=5)


app = FastAPI(lifespan=lifespan, title="MTA Subway Map API")
app.add_middleware(GZipMiddleware, minimum_size=1024)
router = APIRouter(prefix="/api")


# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/stations")
async def get_stations() -> Response:
    return Response(
        content=_cache.stations_geojson,
        media_type="application/json",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/routes")
async def get_routes() -> Response:
    return Response(
        content=orjson.dumps(_cache.routes_meta),
        media_type="application/json",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/shapes/{route_id}")
async def get_shapes(route_id: str) -> Response:
    data = _cache.get_shapes_geojson(route_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No shapes for route {route_id!r}")
    return Response(
        content=data,
        media_type="application/json",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/all-shapes")
async def get_all_shapes() -> Response:
    """All route shapes in one request — eliminates 28 separate fetches at page load."""
    return Response(
        content=_cache.all_shapes_bytes,
        media_type="application/json",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/shape-index")
async def get_shape_index() -> Response:
    return Response(
        content=_cache.shape_index_bytes,
        media_type="application/json",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/stop-info")
async def get_stop_info() -> Response:
    """All stops from Supabase stop_info table, keyed by stop_id. Cached 1h."""
    return Response(
        content=_stop_info_bytes,
        media_type="application/json",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/station/{station_id}/arrivals")
async def get_station_arrivals(station_id: str) -> Response:
    """
    Answers from in-memory train state — no Supabase round-trip.
    loc_station is parent station id; next_stop is platform-level (e.g. "110N").
    """
    trains = _ws_manager.last_trains if _ws_manager else []
    arrivals = [
        t for t in trains
        if t.get("loc_station") == station_id
        or (t.get("next_stop") or "").startswith(station_id)
    ]
    arrivals.sort(key=lambda t: t.get("next_arr") or "")

    # Resolve station name server-side so the client always gets it
    station_name: str | None = None
    if station_info := _stop_info_map.get(station_id):
        station_name = station_info.get("name")

    return Response(
        content=orjson.dumps({
            "station_id":   station_id,
            "station_name": station_name,
            "arrivals":     arrivals[:12],
        }),
        media_type="application/json",
        headers={"Cache-Control": _NO_CACHE},
    )


@router.get("/train/{trip_id}")
async def get_train_detail(trip_id: str) -> Response:
    trains = _ws_manager.last_trains if _ws_manager else []
    match = next((t for t in trains if t.get("trip_id") == trip_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Train not found")
    return Response(
        content=orjson.dumps(match),
        media_type="application/json",
        headers={"Cache-Control": _NO_CACHE},
    )


# ── WebSocket ──────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await _ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        _ws_manager.disconnect(websocket)
    except Exception:
        _ws_manager.disconnect(websocket)


# ── Mount ──────────────────────────────────────────────────────────────────────


class NoCacheStaticFiles(StaticFiles):
    """Serve web assets with `no-cache` so browsers always revalidate.

    ES module imports (app.js → ./MapManager.js, …) are fetched directly by the
    browser and would otherwise be cached aggressively, leaving users on stale
    code after a deploy. `no-cache` forces a conditional request; unchanged files
    still return a fast 304 (StaticFiles sends an ETag), so the cost is one
    round-trip, not a full re-download.
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
