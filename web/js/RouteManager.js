/**
 * RouteManager — draws route geometry using L.geoJSON layers.
 *
 * L.geoJSON is used instead of L.polyline + L.canvas because:
 *  - It natively handles GeoJSON coordinate order ([lon,lat] → [lat,lon])
 *  - It is the most thoroughly tested Leaflet path for GeoJSON input
 *  - It uses Leaflet's default SVG renderer which is reliable and stable
 */
export class RouteManager {
  constructor(mapManager, routesMeta) {
    this._map    = mapManager.leaflet;
    this._routes = new Map(routesMeta.map(r => [r.route_id, r]));
    this._layers = new Map(); // route_id → L.GeoJSON layer
  }

  addRouteLayer(routeId, geojson, _beforeLayerId) {
    const meta = this._routes.get(routeId);
    if (!meta) return;

    const color   = `#${meta.color}`;
    const layer   = L.geoJSON(geojson, {
      style: { color, weight: 2.5, opacity: 0.85 },
    }).addTo(this._map);

    this._layers.set(routeId, layer);
  }

  setRouteVisible(routeId, visible) {
    const layer = this._layers.get(routeId);
    if (!layer) return;
    if (visible) layer.addTo(this._map);
    else         this._map.removeLayer(layer);
  }

  isVisible(routeId) {
    const l = this._layers.get(routeId);
    return l ? this._map.hasLayer(l) : false;
  }
}
