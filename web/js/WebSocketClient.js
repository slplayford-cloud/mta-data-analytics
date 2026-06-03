/**
 * WebSocketClient — connects to /api/ws and maintains full train state.
 * Exponential backoff reconnect; calls onTrains() with the full train array
 * after every update.
 */
export class WebSocketClient {
  constructor(path = '/api/ws') {
    this._path    = path;
    this._ws      = null;
    this._cb      = null;
    this._cache   = new Map();  // trip_id → row (full snapshot per broadcast)
    this._backoff = 1000;       // ms; doubles on each failed connect
    this._intentionalClose = false;
  }

  onTrains(cb) { this._cb = cb; }

  connect() {
    this._intentionalClose = false;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url   = `${proto}//${location.host}${this._path}`;

    this._ws = new WebSocket(url);
    this._ws.binaryType = 'arraybuffer';

    this._ws.onopen = () => {
      console.log('[WS] connected');
      this._backoff = 1000;
    };

    this._ws.onmessage = e => {
      try {
        const msg = JSON.parse(new TextDecoder().decode(e.data));
        if (msg.type === 'trains') {
          // Full state snapshot — replace cache entirely
          this._cache.clear();
          for (const t of msg.trains) this._cache.set(t.trip_id, t);
          if (this._cb) this._cb([...this._cache.values()]);
          console.log(`[WS] ${msg.trains.length} trains at ${new Date(msg.ts * 1000).toLocaleTimeString()}`);
        }
      } catch (err) {
        console.warn('[WS] parse error:', err);
      }
    };

    this._ws.onclose = () => {
      if (!this._intentionalClose) {
        console.log(`[WS] disconnected — reconnecting in ${this._backoff / 1000}s`);
        setTimeout(() => this.connect(), this._backoff);
        this._backoff = Math.min(this._backoff * 2, 30000);
      }
    };

    this._ws.onerror = err => {
      console.warn('[WS] error:', err);
    };
  }

  disconnect() {
    this._intentionalClose = true;
    this._ws?.close();
  }
}
