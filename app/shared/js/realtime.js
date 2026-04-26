/* realtime.js — SSE client + freshness UI helpers + status banner.
 *
 * Loaded on every page. Exposes window.Realtime with:
 *   subscribe({channels, ticker, min_alpha, forensic_band, replay, onEvent}) -> {close}
 *   freshness(publishedISO) -> {label, color, age_min}
 *   tierBadge(tier) -> {label, color}
 *   forensicBadge(band) -> {label, color}
 *   sourceConfirmationBadge(status) -> {label, color}
 *   mountStatusBanner(elementId)  // adds the "last updated Xm ago" chip
 *
 * The SSE client auto-reconnects with backoff and keeps the most-recent buffer
 * available via Realtime.recent so newly-mounted components can show context
 * without waiting for the next push.
 */
(function () {
  const API_BASE = (window.API_BASE || '/api').replace(/\/+$/, '');
  const STREAM_URL = API_BASE + '/stream';

  const buffer = [];
  const BUFFER_MAX = 200;
  const subscribers = new Set();

  function pushBuffer(ev) {
    buffer.push(ev);
    if (buffer.length > BUFFER_MAX) buffer.splice(0, buffer.length - BUFFER_MAX);
  }

  // ---- SSE CLIENT ----------------------------------------------------------
  function subscribe(opts) {
    opts = opts || {};
    const params = new URLSearchParams();
    if (opts.channels) params.set('channels', [].concat(opts.channels).join(','));
    if (opts.ticker) params.set('ticker', opts.ticker);
    if (opts.min_alpha != null) params.set('min_alpha', opts.min_alpha);
    if (opts.forensic_band) params.set('forensic_band', opts.forensic_band);
    if (opts.replay) params.set('replay', opts.replay);

    let es = null;
    let closed = false;
    let backoff = 1000;
    const maxBackoff = 30000;

    function connect() {
      if (closed) return;
      try {
        es = new EventSource(STREAM_URL + (params.toString() ? '?' + params.toString() : ''));
      } catch (e) {
        scheduleReconnect();
        return;
      }
      es.onopen = () => { backoff = 1000; };
      const handle = (rawData) => {
        try {
          const ev = JSON.parse(rawData);
          pushBuffer(ev);
          subscribers.forEach((cb) => { try { cb(ev); } catch (e) {} });
          if (opts.onEvent) { try { opts.onEvent(ev); } catch (e) {} }
        } catch (e) {}
      };
      es.onmessage = (e) => handle(e.data);
      ['hello', 'news', 'scored', 'alert'].forEach((ch) => {
        es.addEventListener(ch, (e) => handle(e.data));
      });
      es.onerror = () => {
        try { es.close(); } catch (e) {}
        scheduleReconnect();
      };
    }

    function scheduleReconnect() {
      if (closed) return;
      const wait = backoff;
      backoff = Math.min(backoff * 2, maxBackoff);
      setTimeout(connect, wait);
    }

    connect();
    if (opts.onEvent) subscribers.add(opts.onEvent);

    return {
      close: () => {
        closed = true;
        try { es && es.close(); } catch (e) {}
        if (opts.onEvent) subscribers.delete(opts.onEvent);
      },
    };
  }

  // ---- FRESHNESS -----------------------------------------------------------
  function ageMinutes(isoOrEpoch) {
    if (!isoOrEpoch) return null;
    let t;
    if (typeof isoOrEpoch === 'number') {
      t = isoOrEpoch < 1e12 ? isoOrEpoch * 1000 : isoOrEpoch;
    } else {
      t = Date.parse(isoOrEpoch);
    }
    if (!t || isNaN(t)) return null;
    return (Date.now() - t) / 60000;
  }

  function freshness(isoOrEpoch) {
    const m = ageMinutes(isoOrEpoch);
    if (m == null) return { label: '', color: '#8c909f', age_min: null };
    if (m < 60) return { label: 'BREAKING', color: '#4ee6b8', age_min: m };
    if (m < 360) return { label: 'FRESH', color: '#5b6cff', age_min: m };
    if (m < 1440) return { label: 'TODAY', color: '#9aa0c8', age_min: m };
    if (m < 4320) return { label: 'RECENT', color: '#8c909f', age_min: m };
    return { label: 'STALE', color: '#5a5d6a', age_min: m };
  }

  function ageLabel(isoOrEpoch) {
    const m = ageMinutes(isoOrEpoch);
    if (m == null) return '';
    if (m < 1) return 'just now';
    if (m < 60) return Math.floor(m) + 'm ago';
    const h = m / 60;
    if (h < 24) return Math.floor(h) + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  // ---- BADGE HELPERS -------------------------------------------------------
  const TIER_INFO = {
    1: { label: 'T1 EXCHANGE', color: '#4ee6b8' },
    2: { label: 'T2 PRESS',    color: '#5b6cff' },
    3: { label: 'T3 AGGREG',   color: '#9aa0c8' },
    4: { label: 'T4 UNVERIF',  color: '#ff7a8a' },
  };
  function tierBadge(tier) { return TIER_INFO[tier] || { label: '', color: '#8c909f' }; }

  const FORENSIC_INFO = {
    clean:               { label: 'CLEAN',        color: '#4ee6b8' },
    unverified:          { label: 'UNVERIFIED',   color: '#f9d423' },
    suspicious:          { label: 'SUSPICIOUS',   color: '#ff9466' },
    likely_manipulated:  { label: 'MANIPULATED',  color: '#ff7a8a' },
  };
  function forensicBadge(band) { return FORENSIC_INFO[band] || null; }

  const CONFIRM_INFO = {
    tier1:       { label: '✓ EXCHANGE',  color: '#4ee6b8' },
    confirmed:   { label: '✓ CONFIRMED', color: '#4ee6b8' },
    single:      { label: '1 SOURCE',    color: '#f9d423' },
    unconfirmed: { label: 'UNCONFIRMED', color: '#ff7a8a' },
  };
  function sourceConfirmationBadge(status) { return CONFIRM_INFO[status] || null; }

  function badgeChip(label, color, opts) {
    opts = opts || {};
    const fontSize = opts.size === 'lg' ? '11px' : '9px';
    const padding = opts.size === 'lg' ? '4px 10px' : '2px 7px';
    return `<span style="display:inline-block;padding:${padding};border-radius:6px;font-size:${fontSize};font-weight:800;text-transform:uppercase;letter-spacing:0.05em;background:${color}1a;color:${color};border:1px solid ${color}33;">${label}</span>`;
  }

  // ---- STATUS BANNER -------------------------------------------------------
  let _statusTimer = null;

  async function fetchStatus() {
    try {
      const r = await fetch(API_BASE + '/status', { cache: 'no-store' });
      if (!r.ok) return null;
      const j = await r.json();
      return j.success ? j.data : null;
    } catch (e) { return null; }
  }

  function renderStatusBanner(el, st) {
    if (!st) {
      el.innerHTML = '<span style="font-size:10px;color:#8c909f;">status unavailable</span>';
      return;
    }
    const sc = st.scraper || {};
    const lastMin = sc.last_run_age_min;
    let color = '#4ee6b8';
    let txt = 'Live';
    if (lastMin == null) { color = '#8c909f'; txt = 'Initializing'; }
    else if (sc.last_error) { color = '#ff7a8a'; txt = 'Scraper error'; }
    else if (lastMin > 15) { color = '#ff9466'; txt = `Stale ${Math.round(lastMin)}m`; }
    else if (lastMin > 5) { color = '#f9d423'; txt = `Updated ${Math.round(lastMin)}m ago`; }
    else { color = '#4ee6b8'; txt = `Live · ${Math.max(1, Math.round(lastMin))}m`; }

    const subs = (st.stream && st.stream.subscribers) || 0;
    const events1h = (st.db && st.db.events_1h) || 0;
    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;font-size:10px;font-family:'JetBrains Mono',monospace;">
        <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${color};box-shadow:0 0 6px ${color};"></span>
        <span style="color:${color};font-weight:700;">${txt}</span>
        <span style="color:#5a5d6a;">·</span>
        <span style="color:#9aa0c8;">${events1h} events / 1h</span>
        <span style="color:#5a5d6a;">·</span>
        <span style="color:#9aa0c8;">${subs} live</span>
      </div>`;
  }

  function mountStatusBanner(elementId, intervalSec) {
    const el = typeof elementId === 'string' ? document.getElementById(elementId) : elementId;
    if (!el) return null;
    intervalSec = intervalSec || 30;
    const tick = async () => {
      const st = await fetchStatus();
      renderStatusBanner(el, st);
    };
    tick();
    if (_statusTimer) clearInterval(_statusTimer);
    _statusTimer = setInterval(tick, intervalSec * 1000);
    return { stop: () => clearInterval(_statusTimer) };
  }

  // ---- EXPORT --------------------------------------------------------------
  window.Realtime = {
    subscribe,
    freshness,
    ageLabel,
    ageMinutes,
    tierBadge,
    forensicBadge,
    sourceConfirmationBadge,
    badgeChip,
    mountStatusBanner,
    fetchStatus,
    get recent() { return buffer.slice(); },
  };
})();
