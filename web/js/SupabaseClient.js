/**
 * SupabaseRealtimeClient — subscribes to postgres_changes on current_trains.
 * Maintains an in-memory cache so TrainManager always gets full state.
 */

// These are the public (anon) credentials — safe to expose in browser JS.
// The anon key can only read/write rows that RLS policies allow.
const SUPABASE_URL      = 'https://glcbohvrgoemncnpvprg.supabase.co';
const SUPABASE_ANON_KEY = window.__SUPABASE_ANON_KEY__ || '';

export class SupabaseRealtimeClient {
  constructor() {
    if (!SUPABASE_ANON_KEY) {
      console.warn('[SupabaseClient] SUPABASE_ANON_KEY not set — set window.__SUPABASE_ANON_KEY__');
    }
    // supabase-js is loaded as a global UMD script (window.supabase)
    this._client = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
    this._cache  = new Map();   // trip_id → row
    this._cb     = null;
    this._channel = null;
  }

  onTrains(cb) { this._cb = cb; }

  async connect() {
    // 1. Fetch the current snapshot before subscribing to avoid the initial gap
    await this._fetchInitialState();

    // 2. Subscribe to all changes on current_trains
    this._channel = this._client
      .channel('current-trains-changes')
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'current_trains' },
        payload => this._handleChange(payload),
      )
      .subscribe(status => {
        if (status === 'SUBSCRIBED') {
          console.log('[SupabaseClient] Realtime subscribed');
        }
      });
  }

  async _fetchInitialState() {
    try {
      const { data, error } = await this._client
        .from('current_trains')
        .select('*');

      if (error) throw error;
      for (const row of data ?? []) {
        this._cache.set(row.trip_id, row);
      }
      console.log(`[SupabaseClient] Initial state: ${this._cache.size} trains`);
      this._emit();
    } catch (err) {
      console.warn('[SupabaseClient] Failed to fetch initial state:', err);
    }
  }

  _handleChange(payload) {
    const { eventType, new: newRow, old: oldRow } = payload;

    if (eventType === 'DELETE') {
      const id = oldRow?.trip_id;
      if (id) this._cache.delete(id);
    } else {
      // INSERT or UPDATE
      if (newRow?.trip_id) this._cache.set(newRow.trip_id, newRow);
    }

    this._emit();
  }

  _emit() {
    if (this._cb) this._cb([...this._cache.values()]);
  }
}
