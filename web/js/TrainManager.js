/**
 * TrainManager — animates live train positions.
 *
 * The feed gives a position roughly every 15 seconds. Drawing that directly
 * makes trains teleport, so each train is interpolated along its real route
 * geometry between where it was last seen and where it is predicted next.
 *
 * Positions go into one GeoJSON source refreshed at 30fps. Mapbox draws and hit
 * tests them on the GPU, so there is no canvas overlay and no manual picking.
 */

const FRAME_MS = 1000 / 30;

// A degree of longitude is shorter than a degree of latitude, by cos(latitude).
// Without this correction, distances along east-west track are overstated and
// trains drift ahead of their true position on crosstown segments.
const LON_SCALE = Math.cos(40.75 * Math.PI / 180);

const clamp01 = value => (value < 0 ? 0 : value > 1 ? 1 : value);


/** One train's position state, interpolated between feed updates. */
class TrainState {
  constructor(row, receivedAt, context) {
    this._context  = context;
    this._lastPos  = null;
    this._segment  = null;
    this.apply(row, receivedAt);
  }

  apply(row, receivedAt) {
    // Snapshot where we currently think it is, so a new update animates from
    // there rather than jumping.
    this._lastPos = this._segment ? this.positionAt(receivedAt) : null;

    Object.assign(this, row);
    this.receivedAt   = receivedAt;
    this._nextArrTime = row.next_arr ? Date.parse(row.next_arr) / 1000 : null;
    this._color       = this._context.routeColors.get(row.route_id) ?? '888888';

    this._resolveShape();
    this._buildSegment();
  }

  get color()  { return `#${this._color}`; }

  /** Realtime shape ids are sometimes a prefix of the static ones. */
  _resolveShape() {
    const { shapeGeom, shapePrefix } = this._context;
    const raw = this.shape_id;
    if (!raw)                { this._shapeId = null; return; }
    if (shapeGeom.has(raw))  { this._shapeId = raw;  return; }

    const cached = shapePrefix.get(raw);
    if (cached) { this._shapeId = cached; return; }

    for (const key of shapeGeom.keys()) {
      if (key.startsWith(raw)) {
        shapePrefix.set(raw, key);
        this._shapeId = key;
        return;
      }
    }
    this._shapeId = null;
  }

  /**
   * Precompute the run of shape vertices between the train's current stop and
   * its next one, with cumulative distances, so each frame is a binary search
   * rather than a walk.
   */
  _buildSegment() {
    this._segment = null;
    const { shapeGeom, shapeIndex } = this._context;
    if (!this._shapeId || !this.next_stop) return;

    const coords = shapeGeom.get(this._shapeId);
    const index  = shapeIndex[this._shapeId];
    if (!coords || !index) return;

    const from = index[this.loc_stop_id] ?? index[this.loc_station];
    const to   = index[this.next_stop];
    if (from === undefined || to === undefined || to <= from) return;

    const slice = coords.slice(from, to + 1);
    if (slice.length < 2) return;

    const lengths = new Float64Array(slice.length);
    let total = 0;
    for (let i = 1; i < slice.length; i++) {
      const dx = (slice[i][0] - slice[i - 1][0]) * LON_SCALE;
      const dy =  slice[i][1] - slice[i - 1][1];
      total += Math.hypot(dx, dy);
      lengths[i] = total;
    }
    if (total <= 0) return;

    this._segment = { coords: slice, lengths, total };
  }

  stationCoords(stopId) {
    if (!stopId) return null;
    const { stopCoords } = this._context;
    return stopCoords.get(stopId) ?? stopCoords.get(stopId.slice(0, -1)) ?? null;
  }

  positionAt(now) {
    if (this.status === 'STOPPED_AT') {
      return this.stationCoords(this.loc_stop_id) ?? this.stationCoords(this.loc_station) ?? this._lastPos;
    }
    if (!this._nextArrTime || !this.next_stop) {
      return this.stationCoords(this.loc_station) ?? this._lastPos;
    }

    const span = this._nextArrTime - this.receivedAt;
    if (span <= 0) return this.stationCoords(this.next_stop) ?? this._lastPos;

    const progress = clamp01((now - this.receivedAt) / span);
    if (this._segment) return this._alongShape(progress);

    // No usable geometry — fall back to a straight line between stations.
    const from = this._lastPos ?? this.stationCoords(this.loc_station);
    const to   = this.stationCoords(this.next_stop);
    if (!from || !to) return from ?? to;
    return [
      from[0] + (to[0] - from[0]) * progress,
      from[1] + (to[1] - from[1]) * progress,
    ];
  }

  _alongShape(progress) {
    const { coords, lengths, total } = this._segment;
    const target = progress * total;

    let low = 0, high = lengths.length - 1;
    while (low < high - 1) {
      const mid = (low + high) >> 1;
      if (lengths[mid] <= target) low = mid; else high = mid;
    }

    const span = lengths[high] - lengths[low];
    const ratio = span > 0 ? (target - lengths[low]) / span : 0;
    return [
      coords[low][0] + (coords[high][0] - coords[low][0]) * ratio,
      coords[low][1] + (coords[high][1] - coords[low][1]) * ratio,
    ];
  }
}


export class TrainManager {
  constructor(mapManager, routesMeta, shapeIndex, shapeGeom, stationsGeoJSON) {
    this._map    = mapManager.map;
    this._trains = new Map();
    this._visibleRoutes = new Set(routesMeta.map(route => route.route_id));
    this._onClick = () => {};
    this._frameId = null;
    this._lastFrame = 0;

    const stopCoords = new Map();
    for (const feature of stationsGeoJSON.features) {
      stopCoords.set(feature.properties.id, feature.geometry.coordinates);
    }

    this._context = {
      routeColors: new Map(routesMeta.map(route => [route.route_id, route.color])),
      shapeIndex,
      shapeGeom,
      stopCoords,
      shapePrefix: new Map(),
    };
  }

  init() {
    this._map.addSource('trains', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    });

    this._map.addLayer({
      id:     'trains',
      type:   'circle',
      source: 'trains',
      paint: {
        'circle-radius': [
          'interpolate', ['linear'], ['zoom'],
          10, 3.5,
          13, 6,
          16, 10,
        ],
        'circle-color': ['get', 'color'],
        // A stalled train is one the MTA has not seen move; ring it in red so it
        // reads differently from a train that is merely running late.
        'circle-stroke-color': ['case', ['get', 'stalled'], '#ff4d4d', '#ffffff'],
        'circle-stroke-width': ['case', ['get', 'stalled'], 2.5, 1.5],
      },
    });

    this._map.on('click', 'trains', event => {
      const feature = event.features?.[0];
      if (!feature) return;
      const train = this._trains.get(feature.properties.trip_id);
      if (train) this._onClick(train);
    });

    this._map.on('mouseenter', 'trains', () => {
      this._map.getCanvas().style.cursor = 'pointer';
    });
    this._map.on('mouseleave', 'trains', () => {
      this._map.getCanvas().style.cursor = '';
    });

    this._startLoop();
  }

  onTrainClick(callback) { this._onClick = callback; }

  /** Called with the full train set whenever the socket updates. */
  update(rows) {
    const now = Date.now() / 1000;
    const seen = new Set();

    for (const row of rows) {
      if (!row.trip_id) continue;
      seen.add(row.trip_id);
      const existing = this._trains.get(row.trip_id);
      if (existing) existing.apply(row, now);
      else this._trains.set(row.trip_id, new TrainState(row, now, this._context));
    }

    for (const tripId of this._trains.keys()) {
      if (!seen.has(tripId)) this._trains.delete(tripId);
    }
  }

  setRouteVisible(routeId, visible) {
    if (visible) this._visibleRoutes.add(routeId);
    else this._visibleRoutes.delete(routeId);
  }

  _startLoop() {
    const frame = timestamp => {
      this._frameId = requestAnimationFrame(frame);
      if (timestamp - this._lastFrame < FRAME_MS) return;
      this._lastFrame = timestamp;
      this._render();
    };
    this._frameId = requestAnimationFrame(frame);
  }

  _render() {
    const source = this._map.getSource('trains');
    if (!source) return;

    const now = Date.now() / 1000;
    const features = [];

    for (const train of this._trains.values()) {
      if (!this._visibleRoutes.has(train.route_id)) continue;
      const position = train.positionAt(now);
      if (!position) continue;
      features.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: position },
        properties: {
          trip_id: train.trip_id,
          color:   train.color,
          stalled: Boolean(train.is_stalled),
        },
      });
    }

    source.setData({ type: 'FeatureCollection', features });
  }

  destroy() {
    if (this._frameId !== null) cancelAnimationFrame(this._frameId);
    this._frameId = null;
  }
}
