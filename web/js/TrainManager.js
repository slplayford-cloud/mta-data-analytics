/**
 * TrainManager — animates ~300 train positions at 30fps on a plain HTML Canvas
 * overlaid on the Leaflet map. No WebGL, no GeoJSON worker, no JSON
 * serialisation — just ctx.arc() calls per frame.
 *
 * The canvas sits outside Leaflet's pane system (pointer-events: none) so
 * Leaflet's station click handlers work normally. Train clicks are detected
 * via a hit-test on the map's 'click' event.
 */

// ── TrainState ─────────────────────────────────────────────────────────────
// Unchanged from original — handles position interpolation along shape geometry.

class TrainState {
  constructor(row, receivedAt, shapeGeom, shapeIdx, routeColors, stopCoords, shapePrefix) {
    this._shapeGeom   = shapeGeom;
    this._shapeIdx    = shapeIdx;
    this._routeColors = routeColors;
    this._stopCoords  = stopCoords;
    this._shapePrefix = shapePrefix;
    this._lastPos     = null;
    this._fromIdx     = -1;
    this._toIdx       = -1;
    this._resolvedShapeId = null;
    this._color       = '#888888';
    this._precomp     = null;
    this.applyUpdate(row, receivedAt);
  }

  applyUpdate(row, receivedAt) {
    this._lastPos   = this._fromIdx >= 0 ? this.interpolatedPosition(receivedAt) : null;
    this.tripId     = row.trip_id;
    this.routeId    = row.route_id;
    this.direction  = row.direction;
    this.headsign   = row.headsign;
    this.locStopId  = row.loc_stop_id;
    this.locStation = row.loc_station;
    this.status     = row.status;
    this.nextStop   = row.next_stop;
    this.nextArr    = row.next_arr ? new Date(row.next_arr).getTime() / 1000 : null;
    this.delay      = row.delay_seconds ?? null;
    this.rawShapeId = row.shape_id;
    this.receivedAt = receivedAt;
    this._color     = '#' + (this._routeColors.get(this.routeId) || '888888');
    this._resolveShapeId();
    this._precomputeIndices();
  }

  _resolveShapeId() {
    if (!this.rawShapeId) { this._resolvedShapeId = null; return; }
    if (this._shapeGeom.has(this.rawShapeId)) { this._resolvedShapeId = this.rawShapeId; return; }
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
    this._precomp = null;
    const sid = this._resolvedShapeId;
    if (!sid) { this._fromIdx = -1; this._toIdx = -1; return; }
    const idx = this._shapeIdx[sid];
    if (!idx) { this._fromIdx = -1; this._toIdx = -1; return; }
    this._fromIdx = idx[this.locStopId] ?? idx[this.locStation] ?? -1;
    this._toIdx   = idx[this.nextStop]  ?? -1;
    if (this._fromIdx >= 0 && this._toIdx >= 0 && this._fromIdx !== this._toIdx) {
      const coords   = this._shapeGeom.get(sid);
      if (coords) {
        const segStart = Math.min(this._fromIdx, this._toIdx);
        const segEnd   = Math.max(this._fromIdx, this._toIdx);
        const n        = segEnd - segStart + 1;
        const lens     = new Float64Array(n);
        let total = 0;
        for (let i = 1; i < n; i++) {
          const a  = coords[segStart + i - 1];
          const b  = coords[segStart + i];
          const dx = b[0] - a[0], dy = b[1] - a[1];
          total += Math.sqrt(dx * dx + dy * dy);
          lens[i] = total;
        }
        this._precomp = { coords, lens, total, segStart };
      }
    }
  }

  interpolatedPosition(now) {
    if (!this.locStopId && !this.locStation) return this._lastPos;
    if (this.status === 'STOPPED_AT')
      return this._stationCoords(this.locStopId) || this._stationCoords(this.locStation);
    if (!this.nextArr || !this.nextStop)
      return this._stationCoords(this.locStopId) || this._stationCoords(this.locStation) || this._lastPos;
    const total = this.nextArr - this.receivedAt;
    if (total <= 0) return this._stationCoords(this.nextStop) || this._lastPos;
    const progress = Math.min(1, Math.max(0, (now - this.receivedAt) / total));
    if (this._precomp) {
      const pos = this._interpolateAlongShape(progress);
      if (pos) return pos;
    }
    const from = this._stationCoords(this.locStopId) || this._stationCoords(this.locStation);
    const to   = this._stationCoords(this.nextStop);
    if (from && to) return [from[0] + (to[0] - from[0]) * progress, from[1] + (to[1] - from[1]) * progress];
    return from || to || this._lastPos;
  }

  _interpolateAlongShape(progress) {
    const { coords, lens, total, segStart } = this._precomp;
    if (!total) return coords[segStart];
    const target = progress * total;
    let lo = 0, hi = lens.length - 2;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (lens[mid + 1] < target) lo = mid + 1; else hi = mid;
    }
    const span = lens[lo + 1] - lens[lo];
    const t    = span ? (target - lens[lo]) / span : 0;
    const i    = segStart + lo;
    const a    = coords[i], b = coords[i + 1];
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  }

  _stationCoords(stopId) {
    if (!stopId) return null;
    return this._stopCoords.get(stopId) || this._stopCoords.get(stopId.slice(0, -1)) || null;
  }

  color()  { return this._color; }

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
  constructor(mapManager, routesMeta, shapeIdx, shapeGeomMap, stationsGeoJSON, allStopCoords) {
    this._leaflet     = mapManager.leaflet;
    this._shapeIdx    = shapeIdx;
    this._shapeGeom   = shapeGeomMap;
    this._routeColors = new Map(routesMeta.map(r => [r.route_id, r.color]));
    this._visibleRoutes = new Set(routesMeta.map(r => r.route_id));

    this._stopCoords = new Map();
    for (const f of stationsGeoJSON.features) {
      const [lon, lat] = f.geometry.coordinates;
      this._stopCoords.set(f.properties.id, [lon, lat]);
    }
    if (allStopCoords) {
      for (const [id, coords] of allStopCoords) {
        if (!this._stopCoords.has(id)) this._stopCoords.set(id, coords);
      }
    }

    this._shapePrefix = new Map();
    this._trains      = new Map();
    this._clickCb     = null;
    this._rafId       = null;

    // The canvas that draws train circles — positioned over the Leaflet map
    this._canvas = document.createElement('canvas');
    this._ctx    = this._canvas.getContext('2d');
  }

  init() {
    // Sit above all Leaflet panes; pointer-events:none lets clicks pass through
    // to station CircleMarkers and the map itself.
    // z-index:1000 is safe here because #map has z-index:0, which creates a
    // stacking context that isolates internal z-indexes from UI overlays.
    const s = this._canvas.style;
    s.position      = 'absolute';
    s.top           = s.left = '0';
    s.width         = '100%';
    s.height        = '100%';
    s.pointerEvents = 'none';
    s.zIndex        = '1000';

    this._leaflet.getContainer().appendChild(this._canvas);
    this._resize();

    // Keep canvas sized to the container
    this._leaflet.on('resize', () => this._resize());

    // Render immediately when the map pans/zooms so trains don't lag
    this._leaflet.on('move zoom viewreset', () => {
      if (this._trains.size > 0) this._render();
    });

    // Train click: hit-test on map click event (canvas has pointer-events:none)
    this._leaflet.on('click', e => this._handleMapClick(e));

    // Pointer cursor when hovering over a train
    this._leaflet.on('mousemove', e => this._handleMouseMove(e));

    this._startLoop();
  }

  update(rows) {
    const now = Date.now() / 1000;
    const ids = new Set(rows.map(r => r.trip_id));
    for (const id of this._trains.keys()) {
      if (!ids.has(id)) this._trains.delete(id);
    }
    for (const row of rows) {
      if (this._trains.has(row.trip_id)) {
        this._trains.get(row.trip_id).applyUpdate(row, now);
      } else {
        this._trains.set(row.trip_id, new TrainState(
          row, now, this._shapeGeom, this._shapeIdx,
          this._routeColors, this._stopCoords, this._shapePrefix,
        ));
      }
    }
  }

  setRouteVisible(routeId, visible) {
    if (visible) this._visibleRoutes.add(routeId);
    else         this._visibleRoutes.delete(routeId);
  }

  onTrainClick(cb) { this._clickCb = cb; }

  // ── Private ────────────────────────────────────────────────────────────────

  _resize() {
    const el = this._leaflet.getContainer();
    this._canvas.width  = el.offsetWidth;
    this._canvas.height = el.offsetHeight;
  }

  _radius() {
    const z = this._leaflet.getZoom();
    return z < 11 ? 5 : z < 13 ? 7 : z < 15 ? 9 : 11;
  }

  _render() {
    const ctx = this._ctx;
    const w   = this._canvas.width;
    const h   = this._canvas.height;
    ctx.clearRect(0, 0, w, h);

    const now = Date.now() / 1000;
    const r   = this._radius();

    for (const [, state] of this._trains) {
      if (!this._visibleRoutes.has(state.routeId)) continue;
      const pos = state.interpolatedPosition(now);
      if (!pos) continue;

      // pos is [lon, lat]; L.latLng takes (lat, lon)
      const pt = this._leaflet.latLngToContainerPoint(L.latLng(pos[1], pos[0]));

      // Cull trains outside the visible canvas (saves draw calls)
      if (pt.x < -r || pt.x > w + r || pt.y < -r || pt.y > h + r) continue;

      ctx.beginPath();
      ctx.arc(pt.x, pt.y, r, 0, Math.PI * 2);
      ctx.fillStyle = state.color();
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth   = 1.5;
      ctx.stroke();
    }
  }

  _startLoop() {
    const FRAME_MS = 1000 / 30;
    let lastMs = 0;
    const tick = (ms) => {
      if (this._trains.size > 0 && ms - lastMs >= FRAME_MS) {
        this._render();
        lastMs = ms;
      }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  }

  _hitRadius() { return this._radius() + 5; }

  _nearestTrain(containerPt, now) {
    const hr = this._hitRadius();
    const hr2 = hr * hr;
    for (const [, state] of this._trains) {
      if (!this._visibleRoutes.has(state.routeId)) continue;
      const pos = state.interpolatedPosition(now);
      if (!pos) continue;
      const pt  = this._leaflet.latLngToContainerPoint(L.latLng(pos[1], pos[0]));
      const dx  = containerPt.x - pt.x;
      const dy  = containerPt.y - pt.y;
      if (dx * dx + dy * dy <= hr2) return state;
    }
    return null;
  }

  _handleMapClick(e) {
    if (!this._clickCb) return;
    const now = Date.now() / 1000;
    const pt  = this._leaflet.latLngToContainerPoint(e.latlng);
    const hit = this._nearestTrain(pt, now);
    // Station CircleMarker clicks stop propagation before reaching the map,
    // so if we're here a train click takes priority with no extra action needed.
    if (hit) this._clickCb(hit.toInfoObject());
  }

  _handleMouseMove(e) {
    // Throttle to ~30fps — mousemove fires on every pixel, iterating 300 trains each time is wasteful
    const ms = performance.now();
    if (ms - (this._lastMouseMs || 0) < 32) return;
    this._lastMouseMs = ms;

    const now = Date.now() / 1000;
    const pt  = this._leaflet.latLngToContainerPoint(e.latlng);
    const hit = this._nearestTrain(pt, now);
    this._leaflet.getContainer().style.cursor = hit ? 'pointer' : '';
  }
}
