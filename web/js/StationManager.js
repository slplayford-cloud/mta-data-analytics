/**
 * StationManager — renders ~496 station dots.
 *
 * All markers share a single L.canvas() renderer, so the whole set draws on one
 * <canvas> element instead of 496 individual SVG <path> nodes. This drops DOM
 * node count dramatically and makes pan/zoom far cheaper, while Leaflet still
 * hit-tests clicks against each circle for us.
 */
export class StationManager {
  constructor(mapManager, stationsGeoJSON, infoPanel) {
    this._map       = mapManager.leaflet;
    this._geojson   = stationsGeoJSON;
    this._infoPanel = infoPanel;
    this._renderer  = L.canvas({ padding: 0.5 });
  }

  init(_beforeLayerId) {
    const renderer = this._renderer;
    for (const feat of this._geojson.features) {
      const [lon, lat] = feat.geometry.coordinates;
      const { id, name } = feat.properties;

      L.circleMarker([lat, lon], {
        renderer,
        radius:      4,
        color:       '#aaa',
        weight:      1,
        fillColor:   '#ddd',
        fillOpacity: 1,
      })
      .addTo(this._map)
      .on('click', () => this._handleClick(id, name));
    }
  }

  async _handleClick(stationId, stationName) {
    try {
      const resp = await fetch(`/api/station/${encodeURIComponent(stationId)}/arrivals`);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      // Server includes station_name; fall back to GeoJSON-captured name if absent
      if (!data.station_name) data.station_name = stationName;
      this._infoPanel.showStation(data);
    } catch {
      this._infoPanel.showStation({ station_id: stationId, arrivals: [], station_name: stationName });
    }
  }
}
