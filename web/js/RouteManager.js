/**
 * RouteManager — draws the route lines.
 *
 * Every route goes into one GeoJSON source. Trunk segments carry several routes
 * over identical geometry, so each route is drawn with a line-offset derived
 * from how many routes share its colour group — without that, the Lexington Ave
 * trunk renders as a single line instead of four.
 */

const LINE_WIDTH = [
  'interpolate', ['linear'], ['zoom'],
  9, 1.2,
  12, 2.2,
  16, 4.5,
];

export class RouteManager {
  constructor(mapManager, routesMeta) {
    this._map    = mapManager.map;
    this._routes = new Map(routesMeta.map(route => [route.route_id, route]));
    this._offsets = this._computeOffsets(routesMeta);
  }

  /**
   * Routes sharing a colour share trunk track. Spreading them symmetrically
   * around the centreline keeps each one visible where they run together.
   */
  _computeOffsets(routesMeta) {
    const byColour = new Map();
    for (const route of routesMeta) {
      if (!byColour.has(route.color)) byColour.set(route.color, []);
      byColour.get(route.color).push(route.route_id);
    }

    const offsets = new Map();
    for (const group of byColour.values()) {
      group.sort();
      const middle = (group.length - 1) / 2;
      group.forEach((routeId, index) => offsets.set(routeId, (index - middle) * 2.2));
    }
    return offsets;
  }

  addRoutes(shapesByRoute) {
    for (const [routeId, features] of shapesByRoute) {
      const meta = this._routes.get(routeId);
      if (!meta) continue;

      const sourceId = `route-${routeId}`;
      this._map.addSource(sourceId, {
        type: 'geojson',
        data: { type: 'FeatureCollection', features },
      });

      this._map.addLayer({
        id:     sourceId,
        type:   'line',
        source: sourceId,
        slot:   'middle',        // under labels, over the basemap fill
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color':   `#${meta.color}`,
          'line-width':   LINE_WIDTH,
          'line-offset':  this._offsets.get(routeId) ?? 0,
          'line-opacity': 0.9,
        },
      });
    }
  }

  setRouteVisible(routeId, visible) {
    const layerId = `route-${routeId}`;
    if (!this._map.getLayer(layerId)) return;
    this._map.setLayoutProperty(layerId, 'visibility', visible ? 'visible' : 'none');
  }
}
