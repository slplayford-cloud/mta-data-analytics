/**
 * InfoPanel — slide-in right-side panel showing station arrivals or train detail.
 */
export class InfoPanel {
  constructor(panelEl, contentEl, closeBtn) {
    this._panel = panelEl;
    this._content = contentEl;
    this._routeColors = new Map();

    closeBtn.addEventListener('click', () => this.hide());
  }

  setRouteColors(routesMeta) {
    for (const r of routesMeta) {
      this._routeColors.set(r.route_id, { color: r.color, textColor: r.text_color });
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  showStation(data) {
    const { station_id, arrivals } = data;

    // Find station name from first arrival or use id
    const name = arrivals[0]?.station_name || `Station ${station_id}`;

    let html = `<div class="info-title">${this._esc(name)}</div>
                <div class="info-subtitle">Upcoming arrivals</div>`;

    if (!arrivals || arrivals.length === 0) {
      html += `<div class="no-arrivals">No trains currently tracked</div>`;
    } else {
      html += arrivals.map(a => this._renderArrival(a)).join('');
    }

    this._content.innerHTML = html;
    this._open();
  }

  showTrain(train) {
    const meta = this._routeColors.get(train.route_id) || { color: '888888', textColor: 'FFFFFF' };
    const delayHtml = this._delayBadge(train.delay_seconds);
    const statusStr = this._statusLabel(train.status);

    let html = `
      <div class="info-title" style="display:flex;align-items:center;gap:8px">
        <span class="route-bullet" style="background:#${meta.color};color:#${meta.textColor}">
          ${this._esc(train.route_id)}
        </span>
        <span>${this._esc(train.headsign || 'Unknown destination')}</span>
      </div>
      <div class="info-subtitle">${statusStr}${delayHtml}</div>
    `;

    if (train.next_arr) {
      const mins = this._minutesUntil(train.next_arr);
      html += `
        <div class="info-section-header">Next stop</div>
        <div class="arrival-row">
          <div class="arrival-info">
            <div class="arrival-headsign">${this._esc(train.next_stop || 'Unknown')}</div>
          </div>
          <div class="arrival-time ${mins <= 0 ? 'due' : mins <= 2 ? 'soon' : ''}">
            ${mins <= 0 ? 'Due' : mins === 1 ? '1 min' : `${mins} min`}
          </div>
        </div>
      `;
    }

    this._content.innerHTML = html;
    this._open();
  }

  hide() {
    this._panel.classList.remove('open');
  }

  // ── Private ────────────────────────────────────────────────────────────────

  _open() {
    this._panel.classList.add('open');
  }

  _renderArrival(a) {
    const meta = this._routeColors.get(a.route_id) || { color: '888888', textColor: 'FFFFFF' };
    const mins = a.next_arr ? this._minutesUntil(a.next_arr) : null;
    const timeStr = mins === null ? '—' : mins <= 0 ? 'Due' : mins === 1 ? '1 min' : `${mins} min`;
    const timeClass = mins !== null && mins <= 0 ? 'due' : mins !== null && mins <= 2 ? 'soon' : '';
    const delayHtml = this._delayBadge(a.delay_seconds);

    return `
      <div class="arrival-row">
        <span class="route-bullet" style="background:#${meta.color};color:#${meta.textColor}">
          ${this._esc(a.route_id)}
        </span>
        <div class="arrival-info">
          <div class="arrival-headsign">${this._esc(a.headsign || 'Unknown')}</div>
          <div class="arrival-detail">${this._esc(a.direction === 'N' ? 'Uptown' : 'Downtown')}${delayHtml}</div>
        </div>
        <div class="arrival-time ${timeClass}">${timeStr}</div>
      </div>
    `;
  }

  _delayBadge(delay) {
    if (delay === null || delay === undefined) return '';
    if (Math.abs(delay) < 60) return '';
    const mins = Math.round(Math.abs(delay) / 60);
    if (delay > 0) {
      return `<span class="delay-badge">+${mins}m late</span>`;
    } else {
      return `<span class="delay-badge early">${mins}m early</span>`;
    }
  }

  _statusLabel(status) {
    if (!status) return '';
    if (status === 'STOPPED_AT') return 'Stopped at station';
    if (status === 'IN_TRANSIT_TO') return 'In transit';
    if (status === 'INCOMING_AT') return 'Arriving';
    return status;
  }

  _minutesUntil(isoOrUnix) {
    const t = typeof isoOrUnix === 'number'
      ? isoOrUnix * 1000
      : new Date(isoOrUnix).getTime();
    return Math.round((t - Date.now()) / 60000);
  }

  _esc(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}
