/**
 * SubwayApp — boots the map.
 *
 * All static data is fetched in parallel with map initialisation, so the only
 * real wait is whichever of the two is slower. Trains appear as soon as the
 * socket delivers its opening snapshot.
 */

import { MapManager }      from './MapManager.js';
import { RouteManager }    from './RouteManager.js';
import { StationManager }  from './StationManager.js';
import { TrainManager }    from './TrainManager.js';
import { InfoPanel }       from './InfoPanel.js';
import { LineFilter }      from './LineFilter.js';
import { WebSocketClient } from './WebSocketClient.js';

class SubwayApp {
  constructor() {
    this._infoPanel = new InfoPanel(
      document.getElementById('info-panel'),
      document.getElementById('info-content'),
      document.getElementById('info-close'),
    );
    this._lineFilter = new LineFilter(document.getElementById('line-filter'));
  }

  async init() {
    const loading = document.getElementById('loading');

    try {
      const { mapboxToken } = await fetch('/api/config').then(r => r.json());
      if (!mapboxToken) {
        this._fail('No Mapbox token configured. Set MAPBOX_TOKEN in .env and restart the server.');
        return;
      }

      const mapManager = new MapManager('map', mapboxToken);

      const [routesMeta, stations, shapeIndex, allShapes] = await Promise.all([
        fetch('/api/routes').then(r => r.json()),
        fetch('/api/stations').then(r => r.json()),
        fetch('/api/shape-index').then(r => r.json()),
        fetch('/api/all-shapes').then(r => r.json()),
        mapManager.waitForLoad(),
      ]);

      // shape_id → coordinates, for interpolating trains along real track.
      // route_id → features, for drawing the lines.
      const shapeGeom    = new Map();
      const shapesByRoute = new Map();
      for (const feature of allShapes.features) {
        const { shape_id: shapeId, route_id: routeId } = feature.properties ?? {};
        if (shapeId && feature.geometry?.coordinates) {
          shapeGeom.set(shapeId, feature.geometry.coordinates);
        }
        if (routeId) {
          if (!shapesByRoute.has(routeId)) shapesByRoute.set(routeId, []);
          shapesByRoute.get(routeId).push(feature);
        }
      }

      const stopNames = new Map();
      for (const feature of stations.features) {
        if (feature.properties.name) {
          stopNames.set(feature.properties.id, feature.properties.name);
        }
      }
      this._infoPanel.setStopNames(stopNames);
      this._infoPanel.setRouteColors(routesMeta);

      // Platform-level names arrive later and only improve labels, so this is
      // deliberately not awaited.
      fetch('/api/stop-info')
        .then(response => (response.ok ? response.json() : null))
        .then(info => {
          if (!info) return;
          for (const [stopId, stop] of Object.entries(info)) {
            if (stop?.name) stopNames.set(stopId, stop.name);
          }
        })
        .catch(() => {});

      const routeManager = new RouteManager(mapManager, routesMeta);
      routeManager.addRoutes(shapesByRoute);

      new StationManager(mapManager, stations, this._infoPanel).init();

      const trainManager = new TrainManager(
        mapManager, routesMeta, shapeIndex, shapeGeom, stations,
      );
      trainManager.init();
      trainManager.onTrainClick(train => this._infoPanel.showTrain(train));

      this._lineFilter.init(routesMeta, (routeId, visible) => {
        routeManager.setRouteVisible(routeId, visible);
        trainManager.setRouteVisible(routeId, visible);
      });

      loading.classList.add('hidden');

      const socket = new WebSocketClient('/api/ws');
      socket.onTrains(rows => trainManager.update(rows));
      socket.connect();

    } catch (error) {
      console.error('SubwayApp init failed:', error);
      this._fail('Failed to load — check the browser console.');
    }
  }

  _fail(message) {
    const loading = document.getElementById('loading');
    if (!loading) return;
    loading.classList.remove('hidden');
    loading.querySelector('.spinner')?.remove();
    loading.querySelector('span').textContent = message;
  }
}

new SubwayApp().init();
