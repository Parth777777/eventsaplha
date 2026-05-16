/* breaking-alert.js — full-screen alert popup for major breaking news.
 *
 * Polls /api/signals every 60s. Whenever a signal arrives that meets ALL of:
 *   - alpha_score >= 80
 *   - age_hours < 1 (truly fresh, not a stale top pick)
 *   - magnitude >= 7 (big-impact event, not background chatter)
 *   - subject-match filter passes (headline names the ticker)
 *   - we haven't already shown it this session (sessionStorage dedupe)
 * the alert renders as a centred toast over a dim overlay.
 *
 * The user can click through to the stock popup, or dismiss. Auto-dismisses
 * after 14 seconds. Stacks: if two majors land within seconds, the second
 * queues until the first is gone.
 *
 * Site-wide: loaded once via bootstrap.js. Persists across page nav because
 * each page re-runs the poll on load.
 */
(function () {
  if (window.__twBreakingAlert) return; window.__twBreakingAlert = true;

  const POLL_MS         = 60_000;
  // "Major" criteria — relaxed so traders see roughly one alert per hour.
  const MIN_ALPHA       = 65;     // was 80 — many strong signals sit 65-80
  const MIN_MAGNITUDE   = 5;      // was 7 — 5+ captures meaningful events
  const MAX_AGE_HOURS   = 3;      // was 1 — anything fresh within the last 3h qualifies
  // Hourly-floor guarantee: if no popup has fired in HOURLY_FLOOR_MS, the
  // next poll force-shows the best available signal (lower bar than 'major').
  const HOURLY_FLOOR_MS = 60 * 60_000;
  const FALLBACK_MIN_ALPHA = 50;
  const FALLBACK_MAX_AGE_HOURS = 4;
  const TOAST_LIFETIME  = 14_000;
  const SEEN_KEY        = 'tw_breaking_seen';
  const LAST_SHOWN_KEY  = 'tw_breaking_last_shown';
  const QUEUE = [];
  let   busy  = false;

  function loadSeen() {
    try { return new Set(JSON.parse(sessionStorage.getItem(SEEN_KEY) || '[]')); }
    catch (_) { return new Set(); }
  }
  function rememberSeen(id) {
    const seen = loadSeen();
    seen.add(id);
    // Keep the set bounded — last 200 ids
    const arr = Array.from(seen).slice(-200);
    try { sessionStorage.setItem(SEEN_KEY, JSON.stringify(arr)); } catch (_) {}
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }
  function ageHours(iso) {
    if (!iso) return 999;
    const t = Date.parse(iso);
    return t ? (Date.now() - t) / 3_600_000 : 999;
  }

  function injectStyles() {
    if (document.getElementById('tw-breaking-css')) return;
    const s = document.createElement('style');
    s.id = 'tw-breaking-css';
    s.textContent = `
      .tw-break-overlay {
        position: fixed; inset: 0; z-index: 9500;
        background: rgba(7,9,13,0.55); backdrop-filter: blur(2px);
        display: flex; align-items: flex-start; justify-content: center;
        padding-top: 80px; pointer-events: none;
        opacity: 0; transition: opacity 240ms cubic-bezier(0.16,1,0.3,1);
      }
      .tw-break-overlay.on { opacity: 1; pointer-events: auto; }
      .tw-break-card {
        position: relative; width: min(92vw, 560px);
        background: linear-gradient(180deg, #161b24 0%, #0a0e14 100%);
        border: 1px solid rgba(45,212,170,0.4);
        border-radius: 16px;
        padding: 22px 24px 18px;
        box-shadow: 0 24px 70px rgba(0,0,0,0.7),
                    0 0 0 1px rgba(45,212,170,0.15),
                    inset 0 1px 0 rgba(255,255,255,0.05);
        color: #dde3ef;
        transform: translateY(-12px); opacity: 0;
        transition: transform 280ms cubic-bezier(0.16,1,0.3,1), opacity 200ms;
        font-family: 'Inter', 'DM Sans', system-ui, sans-serif;
      }
      .tw-break-overlay.on .tw-break-card { transform: translateY(0); opacity: 1; }
      .tw-break-head {
        display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
      }
      .tw-break-badge {
        display: inline-flex; align-items: center; gap: 6px;
        font: 800 10px/1 'Geist Mono', monospace;
        letter-spacing: 0.18em; text-transform: uppercase;
        color: #f26b6b;
        background: rgba(242,107,107,0.12);
        border: 1px solid rgba(242,107,107,0.3);
        padding: 6px 10px; border-radius: 999px;
      }
      .tw-break-badge .dot {
        width: 6px; height: 6px; border-radius: 50%; background: #f26b6b;
        box-shadow: 0 0 10px #f26b6b;
        animation: tw-break-pulse 1.2s infinite;
      }
      @keyframes tw-break-pulse {
        0%,100% { opacity: 1; transform: scale(1); }
        50%     { opacity: 0.45; transform: scale(0.8); }
      }
      .tw-break-meta {
        flex: 1; font: 700 10px 'Geist Mono', monospace; color: #5a6373;
        text-align: right; letter-spacing: 0.04em;
      }
      .tw-break-close {
        background: transparent; border: 0; cursor: pointer;
        color: #5a6373; padding: 4px 6px; border-radius: 6px;
        font-size: 18px; line-height: 1; transition: color 100ms;
      }
      .tw-break-close:hover { color: #dde3ef; }

      .tw-break-tkrow {
        display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px;
      }
      .tw-break-tk {
        font: 800 28px 'Geist Mono', monospace; color: #f4f6fb;
        letter-spacing: -0.01em;
      }
      .tw-break-alpha {
        font: 800 16px 'Geist Mono', monospace;
        color: #2dd4aa; padding: 4px 12px; border-radius: 999px;
        background: rgba(45,212,170,0.14);
      }
      .tw-break-co {
        font-size: 12px; color: #8a94a8; margin-bottom: 12px;
      }
      .tw-break-headline {
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
        font-size: 18px; font-weight: 700; line-height: 1.35; color: #f4f6fb;
        margin-bottom: 14px; letter-spacing: -0.01em;
      }
      .tw-break-summary {
        font-size: 13px; color: #c2c6d6; line-height: 1.55; margin-bottom: 18px;
        display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
        overflow: hidden;
      }
      .tw-break-footer {
        display: flex; gap: 10px; align-items: center;
      }
      .tw-break-cta {
        flex: 1; display: inline-flex; align-items: center; justify-content: center;
        gap: 6px;
        background: linear-gradient(135deg, #2dd4aa 0%, #1ea98b 100%);
        color: #061611;
        border: 0; cursor: pointer;
        padding: 11px 16px; border-radius: 999px;
        font: 700 13px 'Inter', sans-serif; letter-spacing: 0.02em;
        transition: transform 100ms, box-shadow 100ms;
        box-shadow: 0 4px 14px rgba(45,212,170,0.25);
      }
      .tw-break-cta:hover { transform: translateY(-1px);
                            box-shadow: 0 8px 22px rgba(45,212,170,0.34); }
      .tw-break-dismiss {
        font: 600 12px 'Inter', sans-serif; color: #8a94a8;
        background: transparent; border: 1px solid rgba(141,148,168,0.2);
        padding: 9px 14px; border-radius: 999px; cursor: pointer;
        transition: color 100ms, border-color 100ms;
      }
      .tw-break-dismiss:hover { color: #dde3ef; border-color: rgba(141,148,168,0.4); }

      .tw-break-progress {
        position: absolute; left: 16px; right: 16px; bottom: 8px;
        height: 2px; background: rgba(141,148,168,0.15); border-radius: 1px;
        overflow: hidden;
      }
      .tw-break-progress > div {
        height: 100%; background: rgba(45,212,170,0.6);
        animation: tw-break-tick ${TOAST_LIFETIME}ms linear forwards;
      }
      @keyframes tw-break-tick { from { width: 100%; } to { width: 0%; } }
    `;
    document.head.appendChild(s);
  }

  function buildToast(sig, opts) {
    opts = opts || {};
    injectStyles();
    const tk = sig.ticker || '—';
    const alpha = Math.round(sig.alpha_score || 0);
    const company = sig.company || tk;
    const headline = sig.headline || '';
    const summary  = sig.summary  || '';
    const evType = (sig.event_type || 'event').replace(/_/g, ' ');
    const sentiment = sig.sentiment || 'bullish';
    const sentChip = sentiment === 'bullish' ? '#2dd4aa'
                   : sentiment === 'bearish' ? '#f26b6b' : '#8eb4e0';
    const isFallback = opts.kind === 'top_story';
    const isBearish  = sentiment === 'bearish';
    const badgeText  = isFallback ? 'Top story' : (isBearish ? 'Big move ↓' : 'Breaking');
    const badgeColor = isBearish ? '#f26b6b' : (isFallback ? '#e6b84a' : '#f26b6b');
    const cardBorder = isBearish ? 'rgba(242,107,107,0.4)'
                     : isFallback ? 'rgba(230,184,74,0.4)'
                                  : 'rgba(45,212,170,0.4)';
    const ageMins = Math.max(0, Math.round(ageHours(sig.created_at) * 60));
    const overlay = document.createElement('div');
    overlay.className = 'tw-break-overlay';
    overlay.innerHTML = `
      <div class="tw-break-card" role="alertdialog" aria-live="assertive" style="border-color:${cardBorder};box-shadow:0 24px 70px rgba(0,0,0,0.7), 0 0 0 1px ${cardBorder}, inset 0 1px 0 rgba(255,255,255,0.05);">
        <div class="tw-break-head">
          <span class="tw-break-badge" style="color:${badgeColor};background:color-mix(in srgb, ${badgeColor} 14%, transparent);border-color:color-mix(in srgb, ${badgeColor} 36%, transparent);">
            <span class="dot" style="background:${badgeColor};box-shadow:0 0 10px ${badgeColor};"></span>
            ${escapeHtml(badgeText)}
          </span>
          <span class="tw-break-meta">${escapeHtml(evType.toUpperCase())} · ${ageMins}m ago · α${alpha}</span>
          <button class="tw-break-close" aria-label="Dismiss">×</button>
        </div>
        <div class="tw-break-tkrow">
          <span class="tw-break-tk">${escapeHtml(tk)}</span>
          <span class="tw-break-alpha" style="color:${sentChip};background:color-mix(in srgb, ${sentChip} 14%, transparent);">${alpha}</span>
        </div>
        <div class="tw-break-co">${escapeHtml(company)}</div>
        <div class="tw-break-headline">${escapeHtml(headline)}</div>
        ${summary ? `<div class="tw-break-summary">${escapeHtml(summary)}</div>` : ''}
        <div class="tw-break-footer">
          <button class="tw-break-cta" data-action="open">
            See full signal
            <span class="material-symbols-outlined" style="font-size:16px;">arrow_forward</span>
          </button>
          <button class="tw-break-dismiss" data-action="dismiss">Dismiss</button>
        </div>
        <div class="tw-break-progress"><div></div></div>
      </div>`;
    return overlay;
  }

  function showOne(sig, opts) {
    opts = opts || {};
    if (busy) { QUEUE.push([sig, opts]); return; }
    busy = true;
    const overlay = buildToast(sig, opts);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('on'));

    let dismissed = false;
    function teardown() {
      if (dismissed) return; dismissed = true;
      overlay.classList.remove('on');
      setTimeout(() => {
        overlay.remove();
        busy = false;
        if (QUEUE.length) {
          const next = QUEUE.shift();
          if (Array.isArray(next)) showOne(next[0], next[1]);
          else                     showOne(next);
        }
      }, 220);
    }
    const autoTimer = setTimeout(teardown, TOAST_LIFETIME);

    overlay.addEventListener('click', (e) => {
      // Click outside the card dismisses
      if (e.target === overlay) { clearTimeout(autoTimer); teardown(); return; }
      const act = e.target.closest('[data-action]');
      if (!act) return;
      const a = act.dataset.action;
      if (a === 'dismiss') { clearTimeout(autoTimer); teardown(); }
      if (a === 'open') {
        clearTimeout(autoTimer);
        teardown();
        // Prefer in-app popup so the user keeps context
        if (window.StockPopup && sig.ticker) {
          try { StockPopup.open(sig.ticker); return; } catch (_) {}
        }
        // Fall back to stock detail page
        if (sig.ticker) location.href = '/stock.html?ticker=' + encodeURIComponent(sig.ticker);
      }
    });
    document.addEventListener('keydown', function onEsc(ev) {
      if (ev.key === 'Escape') {
        document.removeEventListener('keydown', onEsc);
        clearTimeout(autoTimer); teardown();
      }
    });
    rememberSeen(sig.event_id || sig.id || (sig.ticker + ':' + sig.created_at));
  }

  function _subjectOk(s) {
    if (window.CuratedSignals && CuratedSignals.headlineMatchesTicker) {
      return CuratedSignals.headlineMatchesTicker(s.headline || '', s.ticker, s.summary || '');
    }
    return true;
  }
  function _brokerCommentary(s) {
    if (window.CuratedSignals && CuratedSignals.isBrokerCommentary) {
      try { return CuratedSignals.isBrokerCommentary(s.headline || '', s.ticker, s.summary || ''); }
      catch (_) { return false; }
    }
    return false;
  }
  // "Major" — meets the standard threshold. Bearish is allowed (the user
  // wants to know about big bad news too, not just bullish events).
  function isMajor(s) {
    if (!s) return false;
    if ((s.alpha_score || 0) < MIN_ALPHA)        return false;
    if ((s.magnitude   || 0) < MIN_MAGNITUDE)    return false;
    if (ageHours(s.created_at) > MAX_AGE_HOURS)  return false;
    if (!_subjectOk(s))                          return false;
    if (_brokerCommentary(s))                    return false;
    return true;
  }
  // Hourly-floor — looser bar used only when 60 min have passed without
  // an alert. Guarantees the user sees something major-ish at least once
  // per session-hour even on quiet days.
  function isFallback(s) {
    if (!s) return false;
    if ((s.alpha_score || 0) < FALLBACK_MIN_ALPHA)        return false;
    if (ageHours(s.created_at) > FALLBACK_MAX_AGE_HOURS)  return false;
    if (!_subjectOk(s))                                   return false;
    if (_brokerCommentary(s))                             return false;
    return true;
  }
  function _msSinceLastShown() {
    const t = parseInt(sessionStorage.getItem(LAST_SHOWN_KEY) || '0', 10);
    if (!t) return Infinity;
    return Date.now() - t;
  }
  function _markShown() {
    try { sessionStorage.setItem(LAST_SHOWN_KEY, String(Date.now())); } catch (_) {}
  }

  async function poll() {
    try {
      const r = await fetch('/api/signals?limit=30');
      const j = await r.json();
      const items = (j && j.data) || [];
      const seen = loadSeen();
      const idOf = (s) => s.event_id || s.id || (s.ticker + ':' + s.created_at);
      const unseen = items.filter(s => !seen.has(idOf(s)));

      const major = unseen.filter(isMajor).sort((a, b) => {
        const da = (b.alpha_score || 0) - (a.alpha_score || 0);
        if (da !== 0) return da;
        return ageHours(a.created_at) - ageHours(b.created_at);
      });

      if (major.length > 0) {
        // Standard path — fire up to 2 in a burst
        major.slice(0, 2).forEach((s) => { showOne(s); _markShown(); });
        return;
      }

      // Hourly-floor: if no popup for >= HOURLY_FLOOR_MS, force-fire one
      // even if no signal meets the 'major' bar (lower fallback bar).
      if (_msSinceLastShown() >= HOURLY_FLOOR_MS) {
        const fallbacks = unseen.filter(isFallback).sort((a, b) => {
          const da = (b.alpha_score || 0) - (a.alpha_score || 0);
          if (da !== 0) return da;
          return ageHours(a.created_at) - ageHours(b.created_at);
        });
        if (fallbacks.length > 0) {
          showOne(fallbacks[0], { kind: 'top_story' });
          _markShown();
        }
      }
    } catch (_) { /* silent */ }
  }

  // ── Instant push via SSE ────────────────────────────────────────────────
  // Subscribe to /api/stream?channels=scored,alert&min_alpha=<bar> so the
  // moment the scraper produces a major signal, the popup fires WITHOUT
  // waiting for the next 60s poll. The poll stays as a safety net (in case
  // the SSE connection drops, or fallback / hourly-floor needs to kick in).
  let _es = null;
  function _connectSSE() {
    if (typeof EventSource === 'undefined') return;
    try {
      if (_es) { try { _es.close(); } catch (_) {} _es = null; }
      const url = '/api/stream?channels=scored,alert&min_alpha=' + MIN_ALPHA + '&replay=0';
      _es = new EventSource(url);
      const handlePushed = (e) => {
        if (!e || !e.data) return;
        let payload;
        try { payload = JSON.parse(e.data); } catch (_) { return; }
        if (payload._replay) return;            // ignore replays
        if (!payload || !payload.ticker)  return;
        // Skip if already seen this event_id
        const id = payload.event_id || payload.id || (payload.ticker + ':' + payload.created_at);
        const seen = loadSeen();
        if (seen.has(id)) return;
        // Apply the same client-side gate (broker filter, subject match, age, etc.)
        if (!isMajor(payload)) return;
        showOne(payload);
        _markShown();
      };
      // Listen on both named channels and the default message stream
      _es.addEventListener('scored', handlePushed);
      _es.addEventListener('alert',  handlePushed);
      _es.onmessage = handlePushed;
      // Auto-reconnect on error (EventSource normally retries, but log it once)
      _es.onerror = () => {
        try { console.debug('[breaking-alert] SSE dropped, browser will retry'); } catch (_) {}
      };
    } catch (e) {
      try { console.debug('[breaking-alert] SSE connect failed:', e); } catch (_) {}
    }
  }

  // Boot: instant SSE push + slow safety-net poll + initial catch-up.
  function boot() {
    _connectSSE();          // instant path
    setTimeout(poll, 2000); // catch any signals that landed in the last 3h before we connected
    setInterval(poll, POLL_MS);  // safety net + hourly-floor enforcement
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // Expose for debugging / manual fire
  window.BreakingAlert = { showOne, poll, reconnect: _connectSSE };
})();
