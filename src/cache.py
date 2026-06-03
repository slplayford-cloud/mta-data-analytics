#!/usr/bin/env python3
"""
StaticDataCache — reads local GTFS static files once at startup,
pre-serialises everything to bytes so HTTP responses are zero-copy memory reads.
"""

import csv
import math
import orjson
from pathlib import Path


STATIC_DIR = Path(__file__).parent.parent / "static"

# MTA route colors (route_id → hex, without #)
# Derived from routes.txt; hardcoded here as a fast lookup fallback.
ROUTE_COLORS: dict[str, str] = {
    "1": "EE352E", "2": "EE352E", "3": "EE352E",
    "4": "00933C", "5": "00933C", "6": "00933C", "6X": "00933C",
    "7": "B933AD", "7X": "B933AD",
    "A": "0039A6", "C": "0039A6", "E": "0039A6",
    "B": "FF6319", "D": "FF6319", "F": "FF6319", "FX": "FF6319", "M": "FF6319",
    "G": "6CBE45",
    "J": "996633", "Z": "996633",
    "L": "A7A9AC",
    "N": "FCCC0A", "Q": "FCCC0A", "R": "FCCC0A", "W": "FCCC0A",
    "S": "808183", "GS": "808183", "FS": "808183", "H": "808183",
    "SI": "0039A6",
}


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in metres between two lat/lon points."""
    r = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


class StaticDataCache:
    """Pre-computed GeoJSON bytes and lookup tables built from local GTFS files."""

    def __init__(self, static_dir: Path = STATIC_DIR) -> None:
        self._dir = static_dir
        self._stations_geojson: bytes = b""
        self._routes_meta: list[dict] = []
        self._shapes_by_route: dict[str, bytes] = {}
        # shape_id → [(lon, lat), ...]  ordered by shape_pt_sequence
        self._shape_geometry: dict[str, list[tuple[float, float]]] = {}
        # shape_id → {stop_id → pt_index}
        self._stop_shape_idx: dict[str, dict[str, int]] = {}
        self._shape_index_bytes: bytes = b""
        # stop_id → (lat, lon)
        self._stop_coords: dict[str, tuple[float, float]] = {}

    # ── public build ──────────────────────────────────────────────────────────

    def build(self) -> None:
        print("[cache] building static data cache …")
        self._build_stop_coords()
        self._build_routes_meta()
        self._build_stations_geojson()
        self._build_shapes()
        self._build_stop_shape_index()
        print(
            f"[cache] done — {len(self._routes_meta)} routes, "
            f"{len(self._stop_coords)} stops, "
            f"{len(self._shape_geometry)} shapes"
        )

    # ── accessors ─────────────────────────────────────────────────────────────

    @property
    def stations_geojson(self) -> bytes:
        return self._stations_geojson

    @property
    def routes_meta(self) -> list[dict]:
        return self._routes_meta

    def get_shapes_geojson(self, route_id: str) -> bytes | None:
        return self._shapes_by_route.get(route_id)

    @property
    def shape_index_bytes(self) -> bytes:
        return self._shape_index_bytes

    # ── builders ──────────────────────────────────────────────────────────────

    def _build_stop_coords(self) -> None:
        with open(self._dir / "stops.txt", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    self._stop_coords[row["stop_id"]] = (
                        float(row["stop_lat"]),
                        float(row["stop_lon"]),
                    )
                except (ValueError, KeyError):
                    pass

    def _build_routes_meta(self) -> None:
        meta: list[dict] = []
        with open(self._dir / "routes.txt", newline="") as f:
            for row in csv.DictReader(f):
                rid = row["route_id"]
                color = row.get("route_color", "").strip() or ROUTE_COLORS.get(rid, "888888")
                meta.append({
                    "route_id": rid,
                    "short_name": row.get("route_short_name", rid),
                    "long_name": row.get("route_long_name", ""),
                    "color": color,
                    "text_color": row.get("route_text_color", "FFFFFF"),
                })
        self._routes_meta = meta

    def _build_stations_geojson(self) -> None:
        # Build route → stations mapping from trips + stops
        stop_routes: dict[str, set[str]] = {}
        with open(self._dir / "stops.txt", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("location_type", "").strip() == "1":
                    stop_routes[row["stop_id"]] = set()

        # Associate routes to parent stations via trips.txt stop relationships
        # Use stop_id prefixes: "101N" → parent "101"
        # We'll do a simpler pass: read all stops and collect parent → routes
        parent_routes: dict[str, set[str]] = {sid: set() for sid in stop_routes}

        # Read trips to get route → shape_id mapping
        shape_to_route: dict[str, str] = {}
        with open(self._dir / "trips.txt", newline="") as f:
            for row in csv.DictReader(f):
                sid = row.get("shape_id", "")
                rid = row.get("route_id", "")
                if sid and rid:
                    shape_to_route[sid] = rid

        features: list[dict] = []
        with open(self._dir / "stops.txt", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("location_type", "").strip() != "1":
                    continue
                try:
                    lat = float(row["stop_lat"])
                    lon = float(row["stop_lon"])
                except (ValueError, KeyError):
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "id": row["stop_id"],
                        "name": row["stop_name"],
                    },
                })

        fc = {"type": "FeatureCollection", "features": features}
        self._stations_geojson = orjson.dumps(fc)

    def _build_shapes(self) -> None:
        # Read shape_id → route_id from trips.txt
        shape_to_route: dict[str, str] = {}
        with open(self._dir / "trips.txt", newline="") as f:
            for row in csv.DictReader(f):
                sid = row.get("shape_id", "").strip()
                rid = row.get("route_id", "").strip()
                if sid and rid and sid not in shape_to_route:
                    shape_to_route[sid] = rid

        # Read shapes.txt: shape_id → [(seq, lat, lon)]
        raw: dict[str, list[tuple[int, float, float]]] = {}
        with open(self._dir / "shapes.txt", newline="") as f:
            for row in csv.DictReader(f):
                sid = row["shape_id"]
                try:
                    seq = int(row["shape_pt_sequence"])
                    lat = float(row["shape_pt_lat"])
                    lon = float(row["shape_pt_lon"])
                except (ValueError, KeyError):
                    continue
                if sid not in raw:
                    raw[sid] = []
                raw[sid].append((seq, lat, lon))

        # Sort each shape by sequence and store as (lon, lat) tuples
        for sid, pts in raw.items():
            pts.sort(key=lambda x: x[0])
            self._shape_geometry[sid] = [(p[2], p[1]) for p in pts]

        # Group shapes by route into GeoJSON FeatureCollections
        route_features: dict[str, list[dict]] = {}
        for sid, coords in self._shape_geometry.items():
            rid = shape_to_route.get(sid)
            if not rid:
                continue
            if rid not in route_features:
                route_features[rid] = []
            route_features[rid].append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": list(coords),
                },
                "properties": {"shape_id": sid, "route_id": rid},
            })

        for rid, feats in route_features.items():
            fc = {"type": "FeatureCollection", "features": feats}
            self._shapes_by_route[rid] = orjson.dumps(fc)

    def _build_stop_shape_index(self) -> None:
        """
        For each shape_id, find which coordinate index in the shape geometry
        is closest to each stop's lat/lon.

        Optimized: only indexes stops that actually appear on a shape's trips
        (from stop_times.txt). Uses a disk cache so subsequent starts are instant.
        """
        cache_file = self._dir / "_shape_stop_index.json"
        if cache_file.exists():
            print("[cache] loading shape-stop index from disk cache …")
            with open(cache_file, "rb") as f:
                data = orjson.loads(f.read())
            self._stop_shape_idx = data
            self._shape_index_bytes = orjson.dumps(data)
            return

        print("[cache] building shape-stop index (one-time, ~30s) …")

        # Step 1: shape_id → one representative trip_id (to look up stop_times)
        shape_to_trip: dict[str, str] = {}
        with open(self._dir / "trips.txt", newline="") as f:
            for row in csv.DictReader(f):
                sid = row.get("shape_id", "").strip()
                tid = row.get("trip_id", "").strip()
                if sid and tid and sid not in shape_to_trip:
                    shape_to_trip[sid] = tid

        target_trips: set[str] = set(shape_to_trip.values())

        # Step 2: trip_id → ordered list of stop_ids (from stop_times)
        trip_stops: dict[str, list[str]] = {}
        with open(self._dir / "stop_times.txt", newline="") as f:
            for row in csv.DictReader(f):
                tid = row.get("trip_id", "").strip()
                if tid not in target_trips:
                    continue
                sid = row.get("stop_id", "").strip()
                seq = int(row.get("stop_sequence", "0") or "0")
                if tid not in trip_stops:
                    trip_stops[tid] = []
                trip_stops[tid].append((seq, sid))

        # Sort by sequence
        shape_stop_list: dict[str, list[str]] = {}
        for shape_id, trip_id in shape_to_trip.items():
            stops = trip_stops.get(trip_id, [])
            stops.sort()
            shape_stop_list[shape_id] = [s for _, s in stops]

        # Step 3: For each (shape_id, stop_id), find nearest shape point
        # Only check stops that actually appear on that shape → fast
        index: dict[str, dict[str, int]] = {}

        for shape_id, coords in self._shape_geometry.items():
            if not coords:
                continue
            stops = shape_stop_list.get(shape_id, [])
            if not stops:
                continue

            stop_idx: dict[str, int] = {}
            for stop_id in stops:
                coord = self._stop_coords.get(stop_id)
                if not coord:
                    continue
                slat, slon = coord
                best_i = 0
                best_d = float("inf")
                for i, (clon, clat) in enumerate(coords):
                    d = (slat - clat) ** 2 + (slon - clon) ** 2
                    if d < best_d:
                        best_d = d
                        best_i = i
                stop_idx[stop_id] = best_i
            if stop_idx:
                index[shape_id] = stop_idx

        self._stop_shape_idx = index
        self._shape_index_bytes = orjson.dumps(index)

        # Save disk cache
        with open(cache_file, "wb") as f:
            f.write(self._shape_index_bytes)
        print(f"[cache] shape-stop index saved ({len(self._shape_index_bytes):,} bytes)")
