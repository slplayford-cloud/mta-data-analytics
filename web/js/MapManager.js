/**
 * MapManager — wraps Leaflet.
 */
export class MapManager {
  constructor(containerId) {
    this._map = L.map(containerId, {
      center: [40.748, -73.985],
      zoom: 11,
      minZoom: 9,
      maxZoom: 18,
      maxBounds: L.latLngBounds([40.47, -74.42], [40.95, -73.62]),
      maxBoundsViscosity: 1.0,
      zoomSnap: 0.5,
      // No preferCanvas — we control rendering per layer type
    });

    L.tileLayer(
      'https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}@2x.png',
      {
        tileSize: 512,
        zoomOffset: -1,
        maxZoom: 20,
        attribution:
          '© <a href="https://www.stadiamaps.com/">Stadia Maps</a> ' +
          '© <a href="https://openmaptiles.org/">OpenMapTiles</a> ' +
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      },
    ).addTo(this._map);

    // whenReady fires synchronously when center+zoom are set in constructor
    this._loadPromise = new Promise(resolve => this._map.whenReady(resolve));
  }

  waitForLoad() { return this._loadPromise; }
  get leaflet()  { return this._map; }
}
