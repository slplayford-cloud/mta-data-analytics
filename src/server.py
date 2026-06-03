#!/usr/bin/env python3
"""
FastAPI server — serves pre-built static GeoJSON from the StaticDataCache,
streams live train positions over WebSocket, and proxies Supabase queries
for station arrival data.

Run:  uvicorn src.server:app --host 0.0.0.0 --port 8000
  or: python run_server.py

Required env vars:
  SUPABASE_URL         — Supabase project URL
  SUPABASE_SERVICE_KEY — service-role key (server-side only)
"""

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Awaitable

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
from supabase import create_client, Client

from src.cache import StaticDataCache
from src.poller import Poller
from src import ingestion

# ── Singletons ────────────────────────────────────────────────────────────────

_cache: StaticDataCache | None = None
_db: Client | None = None
_poller: Poller | None = None
_ws_manager: "WebSocketManager | None" = None


# ── WebSocket manager ─────────────────────────────────────────────────────────

class WebSocketManager:
    """Thread-safe manager for connected WebSocket clients."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._last_payload: bytes | None = None  # send to new clients immediately

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        # Send last known state immediately so client doesn't wait 30s
        if self._last_payload:
            try:
                await ws.send_bytes(self._last_payload)
            except Exception:
                pass

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, payload: bytes) -> None:
        self._last_payload = payload
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_bytes(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    def schedule_broadcast(self, payload: bytes, loop: asyncio.AbstractEventLoop) -> None:
        """Thread-safe: called from the Poller background thread."""
        asyncio.run_coroutine_threadsafe(self.broadcast(payload), loop)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cache, _db, _poller, _ws_manager

    _db = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

    # Warm in-memory schedule state (if the ingestion module supports it)
    if hasattr(ingestion, "load_active_schedules"):
        try:
            ingestion.load_active_schedules(_db)
        except Exception as e:
            print(f"[server] schedule warm-up skipped: {e}")

    # Build static data cache from GTFS files
    _cache = StaticDataCache()
    _cache.build()

    # WebSocket manager
    _ws_manager = WebSocketManager()

    # Poller with broadcast wiring (get_running_loop() works correctly in async context)
    loop = asyncio.get_running_loop()
    _poller = Poller(_db, ws_broadcast=lambda p: _ws_manager.schedule_broadcast(p, loop))
    t = threading.Thread(target=_poller.run, daemon=True, name="gtfs-poller")
    t.start()

    yield

    if _poller:
        _poller.stop()
        t.join(timeout=5)


app = FastAPI(lifespan=lifespan, title="MTA Subway Map API")
router = APIRouter(prefix="/api")


# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/stations")
async def get_stations() -> Response:
    """GeoJSON FeatureCollection of all parent stations."""
    return Response(content=_cache.stations_geojson, media_type="application/json")


@router.get("/routes")
async def get_routes() -> Response:
    return Response(content=orjson.dumps(_cache.routes_meta), media_type="application/json")


@router.get("/shapes/{route_id}")
async def get_shapes(route_id: str) -> Response:
    data = _cache.get_shapes_geojson(route_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No shapes for route {route_id!r}")
    return Response(content=data, media_type="application/json")


@router.get("/shape-index")
async def get_shape_index() -> Response:
    return Response(content=_cache.shape_index_bytes, media_type="application/json")


@router.get("/station/{station_id}/arrivals")
async def get_station_arrivals(station_id: str) -> Response:
    try:
        resp = (
            _db.table("current_trains")
            .select("trip_id,route_id,direction,headsign,loc_stop_id,loc_station,status,next_stop,next_arr,delay_seconds")
            .or_(f"loc_station.eq.{station_id},next_stop.like.{station_id}%")
            .order("next_arr", nullsfirst=False)
            .limit(12)
            .execute()
        )
        return Response(
            content=orjson.dumps({"station_id": station_id, "arrivals": resp.data or []}),
            media_type="application/json",
        )
    except Exception as e:
        return Response(
            content=orjson.dumps({"station_id": station_id, "arrivals": [], "error": str(e)}),
            media_type="application/json",
        )


@router.get("/train/{trip_id}")
async def get_train_detail(trip_id: str) -> Response:
    try:
        resp = (
            _db.table("current_trains")
            .select("*")
            .eq("trip_id", trip_id)
            .limit(1)
            .execute()
        )
        if not resp.data:
            raise HTTPException(status_code=404, detail="Train not found")
        return Response(content=orjson.dumps(resp.data[0]), media_type="application/json")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── WebSocket ──────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await _ws_manager.connect(websocket)
    try:
        # Keep alive; client sends no messages in normal operation
        while True:
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        _ws_manager.disconnect(websocket)
    except Exception:
        _ws_manager.disconnect(websocket)


# ── Mount ──────────────────────────────────────────────────────────────────────

app.include_router(router)
app.mount(
    "/",
    StaticFiles(directory=str(Path(__file__).parent.parent / "web"), html=True),
    name="web",
)
