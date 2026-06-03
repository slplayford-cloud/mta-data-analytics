/**
 * SubwayApp — bootstraps all managers and wires them together.
 *
 * Boot sequence:
 *  1. Fetch /api/routes + /api/stations + /api/shape-index in parallel
 *  2. Init LineFilter (route toggle buttons)
 *  3. Wait for MapLibre to load
 *  4. Fetch all route shapes in parallel (28 requests at once)
 *  5. Add route line layers (map renders track geometry immediately)
 *  6. Add station circles
 *  7. Add train layer (empty until first WS message)
 *  8. Connect WebSocket → trains populate after first poll
 */

import { MapManager }       from './MapManager.js';
import { RouteManager }     from './RouteManager.js';
import { StationManager }   from './StationManager.js';
import { TrainManager }     from './TrainManager.js';
import { InfoPanel }        from './InfoPanel.js';
import { LineFilter }       from './LineFilter.js';
import { WebSocketClient }  from './WebSocketClient.js';

class SubwayApp {
  constructor() {
    this._mapManager     = new MapManager('map');
    this._infoPanel      = new InfoPanel(
      document.getElementById('info-panel'),
      document.getElementById('info-content'),
      document.getElementById('info-close'),
    );
    this._lineFilter     = new LineFilter(document.getElementById('line-filter'));
    this._routeManager   = null;
    this._stationManager = null;
    this._trainManager   = null;
    this._wsClient       = null;
  }

  async init() {
    const loadingEl = document.getElementById('loading');

    try {
      // ── 1. Parallel static data fetch ─────────────────────────────────────
      const [routesMeta, stationsGeoJSON, shapeIdx] = await Promise.all([
        fetch('/api/routes').then(r => r.json()),
        fetch('/api/stations').then(r => r.json()),
        fetch('/api/shape-index').then(r => r.json()),
      ]);

      // Build platform-level stop coords map from stations GeoJSON
      // Stations GeoJSON only has parent stations; platforms are derived:
      // e.g. station "109" → platforms "109N" and "109S" at same coords
      const allStopCoords = new Map();
      for (const f of stationsGeoJSON.features) {
        const [lon, lat] = f.geometry.coordinates;
        const id = f.properties.id;
        allStopCoords.set(id, [lon, lat]);
        allStopCoords.set(id + 'N', [lon, lat]);
        allStopCoords.set(id + 'S', [lon, lat]);
      }

      // ── 2. Route toggle buttons (before map loads — pure DOM) ──────────────
      this._routeManager = new RouteManager(this._mapManager, routesMeta);
      this._infoPanel.setRouteColors(routesMeta);
      this._lineFilter.init(routesMeta, (routeId, visible) => {
        this._routeManager.setRouteVisible(routeId, visible);
        if (this._trainManager) this._trainManager.setRouteVisible(routeId, visible);
      });

      // ── 3. Wait for MapLibre ───────────────────────────────────────────────
      await this._mapManager.waitForLoad();

      // ── 4. Parallel shape fetch (all routes at once) ──────────────────────
      const shapeFetches = routesMeta.map(r =>
        fetch(`/api/shapes/${r.route_id}`)
          .then(res => res.ok ? res.json() : null)
          .then(geojson => ({ route_id: r.route_id, geojson }))
          .catch(() => ({ route_id: r.route_id, geojson: null }))
      );
      const shapeResults = await Promise.all(shapeFetches);

      // Build shape geometry map for interpolation: shape_id → [[lon, lat], ...]
      const shapeGeomMap = new Map();
      for (const { geojson } of shapeResults) {
        if (!geojson) continue;
        for (const feat of geojson.features ?? []) {
          const sid = feat.properties?.shape_id;
          if (sid && feat.geometry?.coordinates) {
            shapeGeomMap.set(sid, feat.geometry.coordinates);
          }
        }
      }

      // ── 5. Route line layers ───────────────────────────────────────────────
      for (const { route_id, geojson } of shapeResults) {
        if (geojson) this._routeManager.addRouteLayer(route_id, geojson);
      }

      // ── 6. Station circles + labels ───────────────────────────────────────
      this._stationManager = new StationManager(
        this._mapManager, stationsGeoJSON, this._infoPanel,
      );
      this._stationManager.init();

      // ── 7. Train layer ────────────────────────────────────────────────────
      this._trainManager = new TrainManager(
        this._mapManager,
        routesMeta,
        shapeIdx,
        shapeGeomMap,
        stationsGeoJSON,
        allStopCoords,
      );
      this._trainManager.init();
      this._trainManager.onTrainClick(train => this._infoPanel.showTrain(train));

      // Hide loading overlay (map + routes are visible)
      loadingEl.classList.add('hidden');

      // ── 8. WebSocket for live train updates ───────────────────────────────
      this._wsClient = new WebSocketClient('/api/ws');
      this._wsClient.onTrains(rows => this._trainManager.update(rows));
      this._wsClient.connect();

    } catch (err) {
      console.error('SubwayApp init failed:', err);
      if (loadingEl) {
        loadingEl.querySelector('span').textContent =
          'Failed to load — check browser console';
      }
    }
  }
}

const app = new SubwayApp();
app.init();
