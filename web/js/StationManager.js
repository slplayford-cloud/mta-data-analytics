/**
 * StationManager — renders the ~496 station dots and their labels.
 *
 * One circle layer and one symbol layer over a single source. Mapbox does the
 * hit testing, so there is no manual proximity search, and labels are collision
 * managed against the basemap's own text.
 */

export class StationManager {
  constructor(mapManager, stationsGeoJSON, infoPanel) {
    this._map       = mapManager.map;
    this._geojson   = stationsGeoJSON;
    this._infoPanel = infoPanel;
  }

  init() {
    this._map.addSource('stations', { type: 'geojson', data: this._geojson });

    this._map.addLayer({
      id:     'stations',
      type:   'circle',
      source: 'stations',
      slot:   'middle',
      paint: {
        'circle-radius': [
          'interpolate', ['linear'], ['zoom'],
          10, 2,
          13, 3.5,
          16, 6,
        ],
        'circle-color':        '#f2f2f2',
        'circle-stroke-color': '#111',
        'circle-stroke-width': 1,
        // Below zoom 11 the dots crowd into noise, so fade them out.
        'circle-opacity':        ['interpolate', ['linear'], ['zoom'], 10, 0.25, 12, 1],
        'circle-stroke-opacity': ['interpolate', ['linear'], ['zoom'], 10, 0.25, 12, 1],
      },
    });

    this._map.addLayer({
      id:      'station-labels',
      type:    'symbol',
      source:  'stations',
      minzoom: 13,
      layout: {
        'text-field':  ['get', 'name'],
        'text-size':   11,
        'text-offset': [0, 1.1],
        'text-anchor': 'top',
        'text-optional': true,
      },
      paint: {
        'text-color':      '#e8e8e8',
        'text-halo-color': '#101018',
        'text-halo-width': 1.2,
      },
    });

    this._map.on('click', 'stations', event => {
      const feature = event.features?.[0];
      if (feature) this._showArrivals(feature.properties.id, feature.properties.name);
    });

    this._map.on('mouseenter', 'stations', () => {
      this._map.getCanvas().style.cursor = 'pointer';
    });
    this._map.on('mouseleave', 'stations', () => {
      this._map.getCanvas().style.cursor = '';
    });
  }

  async _showArrivals(stationId, stationName) {
    try {
      const response = await fetch(`/api/station/${encodeURIComponent(stationId)}/arrivals`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!data.station_name) data.station_name = stationName;
      this._infoPanel.showStation(data);
    } catch {
      this._infoPanel.showStation({
        station_id: stationId, station_name: stationName, arrivals: [],
      });
    }
  }
}
