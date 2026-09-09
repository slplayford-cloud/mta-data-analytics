/**
 * MapManager — wraps Mapbox GL JS v3.
 *
 * The token is fetched from the server rather than baked into the bundle, so a
 * URL-restricted public key can be rotated without a redeploy. Coordinates are
 * [lon, lat] throughout, matching GeoJSON, so the API responses need no
 * reordering on the way in.
 */

const NYC_CENTER = [-73.985, 40.748];
const NYC_BOUNDS = [[-74.42, 40.47], [-73.62, 40.95]];

export class MapManager {
  constructor(containerId, token) {
    if (!token) throw new Error('No Mapbox token');

    mapboxgl.accessToken = token;

    this._map = new mapboxgl.Map({
      container: containerId,
      style:     'mapbox://styles/mapbox/standard',
      center:    NYC_CENTER,
      zoom:      11,
      minZoom:   9,
      maxZoom:   18,
      maxBounds: NYC_BOUNDS,
      attributionControl: true,
    });

    this._map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-left');

    this._ready = new Promise(resolve => {
      this._map.on('load', () => {
        // The v3 Standard style ships lighting presets; night suits a transit map
        // and keeps the coloured route lines legible against the basemap.
        try {
          this._map.setConfigProperty('basemap', 'lightPreset', 'night');
          this._map.setConfigProperty('basemap', 'showPointOfInterestLabels', false);
        } catch {
          // Older style versions lack config properties; the map still works.
        }
        resolve();
      });
    });
  }

  waitForLoad() { return this._ready; }

  get map() { return this._map; }
}
