/**
 * StationManager — renders station circles on the map and handles click events.
 */
export class StationManager {
  constructor(mapManager, stationsGeoJSON, infoPanel) {
    this._map = mapManager;
    this._geojson = stationsGeoJSON;
    this._infoPanel = infoPanel;
    this._SOURCE = 'stations-src';
    this._LAYER = 'stations-layer';
    this._LABELS = 'stations-labels';
  }

  init(beforeLayerId) {
    this._map.addGeoJSONSource(this._SOURCE, this._geojson);

    // Circles — small at low zoom, larger when zoomed in
    this._map.addCircleLayer(
      this._LAYER,
      this._SOURCE,
      '#ffffff',
      ['interpolate', ['linear'], ['zoom'], 9, 2.5, 13, 5, 16, 8],
      beforeLayerId,
    );

    // Station name labels — only visible at zoom ≥ 13
    this._map.addSymbolLayer(
      this._LABELS,
      this._SOURCE,
      ['get', 'name'],
      13,
      beforeLayerId,
    );

    // Click to show arrivals
    this._map.onLayerClick(this._LAYER, e => this._handleClick(e));
  }

  async _handleClick(e) {
    if (!e.features || !e.features.length) return;
    const props = e.features[0].properties;
    const stationId = props.id;

    try {
      const resp = await fetch(`/api/station/${encodeURIComponent(stationId)}/arrivals`);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      // Inject station name from the clicked feature
      data.station_name = props.name;
      if (data.arrivals) {
        data.arrivals = data.arrivals.map(a => ({ ...a, station_name: props.name }));
      }
      this._infoPanel.showStation(data);
    } catch (err) {
      console.warn('Failed to load arrivals:', err);
      this._infoPanel.showStation({ station_id: stationId, arrivals: [] });
    }
  }

  /** Layer id — used by TrainManager to render trains above stations. */
  get layerId() { return this._LAYER; }
}
