/**
 * LineFilter — bottom bar of colored route toggle buttons.
 */
export class LineFilter {
  constructor(el) {
    this._el = el;
    this._onToggle = null;
    // route_id → { btn, active }
    this._buttons = new Map();
  }

  /**
   * @param {Array} routesMeta  [{route_id, short_name, color, text_color}]
   * @param {Function} onToggle  (routeId: string, visible: boolean) => void
   */
  init(routesMeta, onToggle) {
    this._onToggle = onToggle;
    this._el.innerHTML = '';

    for (const r of routesMeta) {
      const btn = document.createElement('button');
      btn.className = 'route-btn';
      btn.title = r.short_name || r.route_id;
      btn.textContent = r.short_name || r.route_id;
      btn.style.background = `#${r.color}`;
      btn.style.color = `#${r.text_color || 'FFFFFF'}`;

      let active = true;
      btn.addEventListener('click', () => {
        active = !active;
        btn.classList.toggle('inactive', !active);
        if (this._onToggle) this._onToggle(r.route_id, active);
      });

      this._el.appendChild(btn);
      this._buttons.set(r.route_id, { btn, get active() { return active; } });
    }
  }
}
