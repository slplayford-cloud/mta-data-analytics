/**
 * StationManager — renders station dots as L.circleMarker (SVG renderer).
 */
export class StationManager {
  constructor(mapManager, stationsGeoJSON, infoPanel) {
    this._map       = mapManager.leaflet;
    this._geojson   = stationsGeoJSON;
    this._infoPanel = infoPanel;
  }

  init(_beforeLayerId) {
    for (const feat of this._geojson.features) {
      const [lon, lat] = feat.geometry.coordinates;
      const { id, name } = feat.properties;

      L.circleMarker([lat, lon], {
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
      const data  = await resp.json();
      data.station_name = stationName;
      this._infoPanel.showStation(data);
    } catch {
      this._infoPanel.showStation({ station_id: stationId, arrivals: [], station_name: stationName });
    }
  }
}
