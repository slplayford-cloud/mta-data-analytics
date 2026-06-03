/**
 * SubwayApp — bootstraps all managers and wires them together.
 *
 * Boot sequence (optimised):
 *  1. Kick off ALL data fetches in parallel — routes, stations, shape-index, ALL shapes
 *  2. Init LineFilter (pure DOM, no map needed)
 *  3. Wait for Leaflet + data fetches to complete (both resolve nearly instantly)
 *  4. Add route line layers, station circles, train layer
 *  5. Connect WebSocket → trains appear immediately from cached last payload
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
      // ── 1. Parallel fetch of ALL static data + wait for map ───────────────
      // All-shapes fetched in the same batch — one request instead of 28.
      const [routesMeta, stationsGeoJSON, shapeIdx, allShapesGeoJSON] = await Promise.all([
        fetch('/api/routes').then(r => r.json()),
        fetch('/api/stations').then(r => r.json()),
        fetch('/api/shape-index').then(r => r.json()),
        fetch('/api/all-shapes').then(r => r.json()),
        this._mapManager.waitForLoad(),  // map init runs concurrently
      ]);

      // ── Build lookup structures ───────────────────────────────────────────

      // stop_id → human-readable name (seeded immediately from GeoJSON, enriched
      // below from stop_info). TrainManager derives its own coord lookup from the
      // same GeoJSON and resolves platform ids (e.g. "L06N") to the parent stop
      // by stripping the suffix, so no separate N/S coord map is needed here.
      const stopNameMap = new Map();
      for (const f of stationsGeoJSON.features) {
        const { id, name } = f.properties;
        if (name) stopNameMap.set(id, name);
      }
      // Set names now so station clicks work immediately
      this._infoPanel.setStopNames(stopNameMap);

      // Enrich with full stop_info table in the background — adds platform-level
      // accuracy and any stops not in the parent-station GeoJSON
      fetch('/api/stop-info')
        .then(r => r.ok ? r.json() : null)
        .then(info => {
          if (!info) return;
          for (const [id, s] of Object.entries(info)) {
            if (s && s.name) stopNameMap.set(id, s.name);
          }
        })
        .catch(() => {});

      // shape_id → [[lon,lat],...] for interpolation
      const shapeGeomMap = new Map();
      // route_id → [GeoJSON Feature,...] for route line layers
      const shapesByRoute = new Map();

      for (const feat of allShapesGeoJSON.features) {
        const sid = feat.properties?.shape_id;
        const rid = feat.properties?.route_id;
        if (sid && feat.geometry?.coordinates) {
          shapeGeomMap.set(sid, feat.geometry.coordinates);
        }
        if (rid) {
          if (!shapesByRoute.has(rid)) shapesByRoute.set(rid, []);
          shapesByRoute.get(rid).push(feat);
        }
      }

      // ── 2. Route toggle buttons ───────────────────────────────────────────
      this._routeManager = new RouteManager(this._mapManager, routesMeta);
      this._infoPanel.setRouteColors(routesMeta);
      this._lineFilter.init(routesMeta, (routeId, visible) => {
        this._routeManager.setRouteVisible(routeId, visible);
        if (this._trainManager) this._trainManager.setRouteVisible(routeId, visible);
      });

      // ── 3. Route line layers ──────────────────────────────────────────────
      for (const [route_id, features] of shapesByRoute) {
        this._routeManager.addRouteLayer(
          route_id,
          { type: 'FeatureCollection', features },
        );
      }

      // ── 4. Station circles + labels ───────────────────────────────────────
      this._stationManager = new StationManager(
        this._mapManager, stationsGeoJSON, this._infoPanel,
      );
      this._stationManager.init();

      // ── 5. Train layer ────────────────────────────────────────────────────
      this._trainManager = new TrainManager(
        this._mapManager,
        routesMeta,
        shapeIdx,
        shapeGeomMap,
        stationsGeoJSON,
      );
      this._trainManager.init();
      this._trainManager.onTrainClick(train => this._infoPanel.showTrain(train));

      loadingEl.classList.add('hidden');

      // ── 6. WebSocket ──────────────────────────────────────────────────────
      // Server sends last snapshot immediately on connect — trains appear at once.
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
