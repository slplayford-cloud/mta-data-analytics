/**
 * TrainManager — manages live train rendering at 60fps via requestAnimationFrame.
 *
 * Trains are rendered as colored circles on a single MapLibre GeoJSON source.
 * Positions are interpolated along shape geometry between WebSocket updates.
 */

const SOURCE_ID = 'trains-src';
const LAYER_ID  = 'trains-layer';

// ── TrainState ─────────────────────────────────────────────────────────────

class TrainState {
  /**
   * @param {object} row         Row from current_trains (WebSocket payload)
   * @param {number} receivedAt  Unix timestamp (seconds) when this update arrived
   * @param {Map}    shapeGeom   shape_id → [[lon,lat],...]  (exact shape IDs from shapes.txt)
   * @param {object} shapeIdx    {shape_id: {stop_id: pt_index}}
   * @param {Map}    routeColors route_id → hex color string
   * @param {Map}    stopCoords  stop_id → [lon, lat]  (all stops including platforms)
   * @param {Map}    shapePrefix route partial shape → best matching full shape_id
   */
  constructor(row, receivedAt, shapeGeom, shapeIdx, routeColors, stopCoords, shapePrefix) {
    this._shapeGeom   = shapeGeom;
    this._shapeIdx    = shapeIdx;
    this._routeColors = routeColors;
    this._stopCoords  = stopCoords;
    this._shapePrefix = shapePrefix;

    this._lastPos = null;
    this._fromIdx = -1;
    this._toIdx   = -1;
    this._resolvedShapeId = null;

    this.applyUpdate(row, receivedAt);
  }

  applyUpdate(row, receivedAt) {
    this._lastPos = this._fromIdx >= 0 ? this.interpolatedPosition(receivedAt) : null;

    this.tripId     = row.trip_id;
    this.routeId    = row.route_id;
    this.direction  = row.direction;
    this.headsign   = row.headsign;
    this.locStopId  = row.loc_stop_id;   // platform level, e.g. "109N"
    this.locStation = row.loc_station;   // parent station, e.g. "109"
    this.status     = row.status;
    this.nextStop   = row.next_stop;
    this.nextArr    = row.next_arr ? new Date(row.next_arr).getTime() / 1000 : null;
    this.delay      = row.delay_seconds ?? null;
    this.rawShapeId = row.shape_id;      // may be partial, e.g. "1..N"
    this.receivedAt = receivedAt;

    this._resolveShapeId();
    this._precomputeIndices();
  }

  // Resolve partial shape_id (e.g. "1..N") to a full shape_id (e.g. "1..N03R")
  _resolveShapeId() {
    if (!this.rawShapeId) { this._resolvedShapeId = null; return; }

    // Exact match first
    if (this._shapeGeom.has(this.rawShapeId)) {
      this._resolvedShapeId = this.rawShapeId;
      return;
    }

    // Prefix match: find any shape that starts with rawShapeId
    const cached = this._shapePrefix.get(this.rawShapeId);
    if (cached) { this._resolvedShapeId = cached; return; }

    for (const key of this._shapeGeom.keys()) {
      if (key.startsWith(this.rawShapeId)) {
        this._shapePrefix.set(this.rawShapeId, key);
        this._resolvedShapeId = key;
        return;
      }
    }
    this._resolvedShapeId = null;
  }

  _precomputeIndices() {
    const sid = this._resolvedShapeId;
    if (!sid) { this._fromIdx = -1; this._toIdx = -1; return; }

    const idx = this._shapeIdx[sid];
    if (!idx) { this._fromIdx = -1; this._toIdx = -1; return; }

    // loc_stop_id is the platform stop (e.g. "109N"), nextStop is also platform level
    this._fromIdx = idx[this.locStopId] ?? idx[this.locStation] ?? -1;
    this._toIdx   = idx[this.nextStop]  ?? -1;
  }

  /**
   * Returns [lon, lat] for the current interpolated position, or null.
   * @param {number} now  Unix seconds
   */
  interpolatedPosition(now) {
    if (!this.locStopId && !this.locStation) return this._lastPos;

    if (this.status === 'STOPPED_AT') {
      return this._stationCoords(this.locStopId) || this._stationCoords(this.locStation);
    }

    if (!this.nextArr || !this.nextStop) {
      return this._stationCoords(this.locStopId)
          || this._stationCoords(this.locStation)
          || this._lastPos;
    }

    const total = this.nextArr - this.receivedAt;
    if (total <= 0) {
      return this._stationCoords(this.nextStop) || this._lastPos;
    }
    const progress = Math.min(1, Math.max(0, (now - this.receivedAt) / total));

    // Interpolate along shape geometry if we have indices
    if (this._fromIdx >= 0 && this._toIdx >= 0 && this._fromIdx !== this._toIdx) {
      const pos = this._interpolateAlongShape(progress);
      if (pos) return pos;
    }

    // Fallback: linear lerp between stop coords
    const from = this._stationCoords(this.locStopId) || this._stationCoords(this.locStation);
    const to   = this._stationCoords(this.nextStop);
    if (from && to) {
      return [
        from[0] + (to[0] - from[0]) * progress,
        from[1] + (to[1] - from[1]) * progress,
      ];
    }
    return from || to || this._lastPos;
  }

  _interpolateAlongShape(progress) {
    const coords = this._shapeGeom.get(this._resolvedShapeId);
    if (!coords) return null;

    const start = Math.min(this._fromIdx, this._toIdx);
    const end   = Math.max(this._fromIdx, this._toIdx);
    const seg   = coords.slice(start, end + 1);
    if (seg.length < 2) return null;

    // Cumulative arc length along segment
    const lens = [0];
    for (let i = 1; i < seg.length; i++) {
      const dx = seg[i][0] - seg[i-1][0];
      const dy = seg[i][1] - seg[i-1][1];
      lens.push(lens[i-1] + Math.sqrt(dx*dx + dy*dy));
    }
    const total = lens[lens.length - 1];
    if (!total) return seg[0];

    const target = progress * total;
    let lo = 0, hi = lens.length - 2;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (lens[mid + 1] < target) lo = mid + 1; else hi = mid;
    }
    const t = (target - lens[lo]) / ((lens[lo + 1] - lens[lo]) || 1);
    return [
      seg[lo][0] + (seg[lo+1][0] - seg[lo][0]) * t,
      seg[lo][1] + (seg[lo+1][1] - seg[lo][1]) * t,
    ];
  }

  _stationCoords(stopId) {
    if (!stopId) return null;
    // Try exact, then parent (strip last char if direction letter)
    return this._stopCoords.get(stopId)
        || this._stopCoords.get(stopId.slice(0, -1))
        || null;
  }

  color() {
    return '#' + (this._routeColors.get(this.routeId) || '888888');
  }

  toInfoObject() {
    return {
      trip_id:       this.tripId,
      route_id:      this.routeId,
      headsign:      this.headsign,
      direction:     this.direction,
      status:        this.status,
      delay_seconds: this.delay,
      next_stop:     this.nextStop,
      next_arr:      this.nextArr,
    };
  }
}

// ── TrainManager ───────────────────────────────────────────────────────────

export class TrainManager {
  /**
   * @param {MapManager}  mapManager
   * @param {Array}       routesMeta     [{route_id, color, ...}]
   * @param {object}      shapeIdx       Pre-built shape→stop→index from /api/shape-index
   * @param {Map}         shapeGeomMap   Map<shape_id, [[lon,lat],...]>
   * @param {object}      stationsGeoJSON  Parent station GeoJSON
   * @param {Map}         allStopCoords  All stop coords including platforms (built in app.js)
   */
  constructor(mapManager, routesMeta, shapeIdx, shapeGeomMap, stationsGeoJSON, allStopCoords) {
    this._map         = mapManager;
    this._shapeIdx    = shapeIdx;
    this._shapeGeom   = shapeGeomMap;
    this._routeColors = new Map(routesMeta.map(r => [r.route_id, r.color]));
    this._visibleRoutes = new Set(routesMeta.map(r => r.route_id));

    // All stop coords: parent stations from GeoJSON + platforms from allStopCoords
    this._stopCoords = new Map();

    // Add parent stations from GeoJSON
    for (const f of stationsGeoJSON.features) {
      const [lon, lat] = f.geometry.coordinates;
      this._stopCoords.set(f.properties.id, [lon, lat]);
    }

    // Add platform-level stops passed in
    if (allStopCoords) {
      for (const [id, coords] of allStopCoords) {
        if (!this._stopCoords.has(id)) {
          this._stopCoords.set(id, coords);
        }
      }
    }

    // Cache for partial shape_id → resolved full shape_id
    this._shapePrefix = new Map();

    // trip_id → TrainState
    this._trains = new Map();
    this._rafId  = null;
    this._clickCb = null;
  }

  init() {
    this._map.addGeoJSONSource(SOURCE_ID, { type: 'FeatureCollection', features: [] });
    this._map.addCircleLayer(
      LAYER_ID,
      SOURCE_ID,
      ['get', '_color'],
      ['interpolate', ['linear'], ['zoom'], 9, 6, 14, 10, 17, 15],
    );
    this._map.onLayerClick(LAYER_ID, e => this._handleClick(e));
    this._startLoop();
  }

  /** Called by WebSocketClient when new train data arrives (full snapshot). */
  update(rows) {
    const now = Date.now() / 1000;
    const incomingIds = new Set(rows.map(r => r.trip_id));

    for (const id of this._trains.keys()) {
      if (!incomingIds.has(id)) this._trains.delete(id);
    }

    for (const row of rows) {
      if (this._trains.has(row.trip_id)) {
        this._trains.get(row.trip_id).applyUpdate(row, now);
      } else {
        this._trains.set(row.trip_id, new TrainState(
          row, now,
          this._shapeGeom, this._shapeIdx,
          this._routeColors, this._stopCoords,
          this._shapePrefix,
        ));
      }
    }
  }

  setRouteVisible(routeId, visible) {
    if (visible) this._visibleRoutes.add(routeId);
    else         this._visibleRoutes.delete(routeId);
  }

  onTrainClick(cb) { this._clickCb = cb; }

  // ── Animation loop ─────────────────────────────────────────────────────────

  _startLoop() {
    const tick = () => {
      this._render();
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  }

  _render() {
    const now = Date.now() / 1000;
    const features = [];

    for (const [id, state] of this._trains) {
      if (!this._visibleRoutes.has(state.routeId)) continue;
      const pos = state.interpolatedPosition(now);
      if (!pos) continue;

      features.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: pos },
        properties: {
          id,
          _color:    state.color(),
          routeId:   state.routeId,
          headsign:  state.headsign,
          delay:     state.delay,
          status:    state.status,
          direction: state.direction,
        },
      });
    }

    this._map.updateGeoJSONSource(SOURCE_ID, { type: 'FeatureCollection', features });
  }

  _handleClick(e) {
    if (!this._clickCb || !e.features?.length) return;
    const props = e.features[0].properties;
    const state = this._trains.get(props.id);
    if (state) this._clickCb(state.toInfoObject());
  }
}
