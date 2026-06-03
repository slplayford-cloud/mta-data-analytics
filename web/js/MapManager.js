/**
 * MapManager — wraps MapLibre GL JS. All other managers interact with the map
 * only through this class, never calling map.* directly.
 */
export class MapManager {
  constructor(containerId) {
    this._map = new maplibregl.Map({
      container: containerId,
      style: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
      center: [-73.985, 40.748],
      zoom: 11,
      minZoom: 9,
      maxZoom: 18,
      antialias: true,
    });
    this._loadPromise = new Promise(resolve => this._map.on('load', resolve));
  }

  waitForLoad() {
    return this._loadPromise;
  }

  get map() {
    return this._map;
  }

  // ── Sources ─────────────────────────────────────────────────────────────────

  addGeoJSONSource(id, data) {
    if (this._map.getSource(id)) return;
    this._map.addSource(id, {
      type: 'geojson',
      data,
      buffer: 0,
      tolerance: 0.5,
    });
  }

  updateGeoJSONSource(id, data) {
    const src = this._map.getSource(id);
    if (src) src.setData(data);
  }

  // ── Layers ──────────────────────────────────────────────────────────────────

  addLineLayer(id, sourceId, colorExpr, widthExpr, beforeId) {
    if (this._map.getLayer(id)) return;
    this._map.addLayer({
      id,
      type: 'line',
      source: sourceId,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': colorExpr,
        'line-width': widthExpr,
        'line-opacity': 0.85,
      },
    }, beforeId);
  }

  addCircleLayer(id, sourceId, colorExpr, radiusExpr, beforeId) {
    if (this._map.getLayer(id)) return;
    this._map.addLayer({
      id,
      type: 'circle',
      source: sourceId,
      paint: {
        'circle-color': colorExpr,
        'circle-radius': radiusExpr,
        'circle-stroke-width': 1.5,
        'circle-stroke-color': '#ffffff',
        'circle-stroke-opacity': 0.9,
      },
    }, beforeId);
  }

  addSymbolLayer(id, sourceId, textField, minZoom, beforeId) {
    if (this._map.getLayer(id)) return;
    this._map.addLayer({
      id,
      type: 'symbol',
      source: sourceId,
      minzoom: minZoom,
      layout: {
        'text-field': textField,
        'text-size': 11,
        'text-offset': [0, 1.5],
        'text-anchor': 'top',
        'text-font': ['Noto Sans Regular'],
      },
      paint: {
        'text-color': '#ffffff',
        'text-halo-color': '#000000',
        'text-halo-width': 1.5,
      },
    }, beforeId);
  }

  // ── Visibility ──────────────────────────────────────────────────────────────

  setLayerVisibility(layerId, visible) {
    if (!this._map.getLayer(layerId)) return;
    this._map.setLayoutProperty(layerId, 'visibility', visible ? 'visible' : 'none');
  }

  hasLayer(id) {
    return !!this._map.getLayer(id);
  }

  // ── Events ──────────────────────────────────────────────────────────────────

  onLayerClick(layerId, callback) {
    this._map.on('click', layerId, callback);
    this._map.on('mouseenter', layerId, () => {
      this._map.getCanvas().style.cursor = 'pointer';
    });
    this._map.on('mouseleave', layerId, () => {
      this._map.getCanvas().style.cursor = '';
    });
  }
}
