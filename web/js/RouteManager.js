/**
 * RouteManager — manages the 28 route-geometry line layers (colored track lines).
 */
export class RouteManager {
  constructor(mapManager, routesMeta) {
    this._map = mapManager;
    // route_id → { color, short_name }
    this._routes = new Map(routesMeta.map(r => [r.route_id, r]));
    // route_id → layer id string
    this._layerIds = new Map();
    this._visible = new Set(routesMeta.map(r => r.route_id));
  }

  /**
   * Add a route's GeoJSON geometry as a line layer.
   * @param {string} routeId
   * @param {object} geojson  GeoJSON FeatureCollection of LineStrings
   * @param {string|undefined} beforeLayerId  Insert below this layer (for z-ordering)
   */
  addRouteLayer(routeId, geojson, beforeLayerId) {
    const meta = this._routes.get(routeId);
    if (!meta) return;

    const sourceId = `route-src-${routeId}`;
    const layerId = `route-line-${routeId}`;

    this._map.addGeoJSONSource(sourceId, geojson);
    this._map.addLineLayer(
      layerId,
      sourceId,
      `#${meta.color}`,
      ['interpolate', ['linear'], ['zoom'], 9, 1.5, 13, 2.5, 16, 4.5],
      beforeLayerId,
    );

    this._layerIds.set(routeId, layerId);

    // Apply initial visibility
    if (!this._visible.has(routeId)) {
      this._map.setLayerVisibility(layerId, false);
    }
  }

  setRouteVisible(routeId, visible) {
    if (visible) {
      this._visible.add(routeId);
    } else {
      this._visible.delete(routeId);
    }
    const layerId = this._layerIds.get(routeId);
    if (layerId) this._map.setLayerVisibility(layerId, visible);
  }

  isVisible(routeId) {
    return this._visible.has(routeId);
  }

  /** Returns the layer id that should be rendered just above the route lines. */
  get topLayerId() {
    const ids = [...this._layerIds.values()];
    return ids.length ? ids[ids.length - 1] : undefined;
  }
}
