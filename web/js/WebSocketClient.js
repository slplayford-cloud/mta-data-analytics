/**
 * WebSocketClient — keeps a live train set in sync with the server.
 *
 * The server sends one full snapshot on connect, then deltas. A delta carries
 * only the fields that changed, plus trip_id, so updates are merged into the
 * existing record rather than replacing it. Fields that never change for a trip
 * (route, headsign, shape) arrive once in the snapshot and are never resent.
 */

const decoder = new TextDecoder();

export class WebSocketClient {
  constructor(path = '/api/ws') {
    this._path     = path;
    this._socket   = null;
    this._trains   = new Map();   // trip_id → full train record
    this._onTrains = () => {};
    this._backoff  = 1000;
    this._closing  = false;
  }

  onTrains(callback) { this._onTrains = callback; }

  connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this._socket = new WebSocket(`${protocol}//${location.host}${this._path}`);
    this._socket.binaryType = 'arraybuffer';

    this._socket.onopen = () => { this._backoff = 1000; };

    this._socket.onmessage = event => {
      let message;
      try {
        const text = typeof event.data === 'string'
          ? event.data
          : decoder.decode(event.data);
        message = JSON.parse(text);
      } catch {
        return;   // a malformed frame should not kill the stream
      }

      if (message.type === 'snapshot')   this._applySnapshot(message);
      else if (message.type === 'delta') this._applyDelta(message);
      else return;

      this._onTrains([...this._trains.values()]);
    };

    this._socket.onclose = () => {
      if (this._closing) return;
      setTimeout(() => this.connect(), this._backoff);
      this._backoff = Math.min(this._backoff * 2, 30000);
    };

    this._socket.onerror = () => this._socket?.close();
  }

  _applySnapshot(message) {
    this._trains.clear();
    for (const train of message.trains) this._trains.set(train.trip_id, train);
  }

  _applyDelta(message) {
    for (const change of message.upsert ?? []) {
      const existing = this._trains.get(change.trip_id);
      // Merge: a delta names only what moved, so spread over the last known state.
      this._trains.set(change.trip_id,
        existing ? { ...existing, ...change } : change);
    }
    for (const tripId of message.remove ?? []) this._trains.delete(tripId);
  }

  disconnect() {
    this._closing = true;
    this._socket?.close();
  }
}
