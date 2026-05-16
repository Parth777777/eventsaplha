/* market-clock.js — sticky "session phase" banner shown on every page.
 *
 * - Pulls /api/market/clock every 60s.
 * - Renders a slim strip ABOVE every page header.
 * - Click → /events.html?fresh=1 (so traders can jump to live signal feed even when closed).
 * - Dismissable (sessionStorage flag) — once dismissed, stays hidden until a new browser session.
 *
 * Color palette mirrors the existing app tokens (bull / caution / bear / info).
 */
(function () {
  if (window.__twMarketClock) return; window.__twMarketClock = true;

  const ID = 'tw-market-clock';
  const REFRESH_MS = 60_000;
  let lastPhase = null;

  const PHASE_STYLE = {
    open:       { bg: 'rgba(45,212,170,0.08)',  fg: '#2dd4aa', dot: '#2dd4aa', pulse: true  },
    pre_open:   { bg: 'rgba(141,180,224,0.08)', fg: '#8eb4e0', dot: '#8eb4e0', pulse: true  },
    post_close: { bg: 'rgba(230,184,74,0.08)',  fg: '#e6b84a', dot: '#e6b84a', pulse: false },
    closed:     { bg: 'rgba(141,148,168,0.08)', fg: '#8a94a8', dot: '#8a94a8', pulse: false },
    weekend:    { bg: 'rgba(141,148,168,0.08)', fg: '#8a94a8', dot: '#8a94a8', pulse: false },
    holiday:    { bg: 'rgba(242,107,107,0.08)', fg: '#f26b6b', dot: '#f26b6b', pulse: false },
  };

  function ensureStyles() {
    if (document.getElementById('tw-mc-styles')) return;
    const s = document.createElement('style');
    s.id = 'tw-mc-styles';
    s.textContent = `
      #${ID} {
        position: sticky; top: 0; z-index: 80;
        display: flex; align-items: center; justify-content: center; gap: 10px;
        padding: 6px 14px;
        font: 600 11px/1.3 'Inter', system-ui, -apple-system, sans-serif;
        letter-spacing: 0.04em;
        border-bottom: 1px solid rgba(141,148,168,0.14);
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
      }
      #${ID} .tw-mc-dot {
        width: 7px; height: 7px; border-radius: 50%;
        flex-shrink: 0;
      }
      #${ID} .tw-mc-dot.pulse { animation: tw-mc-pulse 1.6s infinite; }
      @keyframes tw-mc-pulse {
        0%,100% { opacity: 1; transform: scale(1); }
        50%     { opacity: 0.4; transform: scale(0.85); }
      }
      #${ID} .tw-mc-label { font-weight: 600; }
      #${ID} .tw-mc-meta {
        color: rgba(141,148,168,0.85); font-weight: 500;
        font-family: 'Geist Mono', 'JetBrains Mono', monospace;
        font-size: 10.5px; font-variant-numeric: tabular-nums;
      }
      #${ID} .tw-mc-close {
        margin-left: 8px; background: transparent; border: 0; cursor: pointer;
        color: rgba(141,148,168,0.6); font-size: 14px; line-height: 1;
        padding: 4px 6px; border-radius: 4px;
      }
      #${ID} .tw-mc-close:hover { color: #fff; background: rgba(255,255,255,0.04); }
      @media (max-width: 600px) {
        #${ID} .tw-mc-meta { display: none; }
        #${ID} { font-size: 10.5px; }
      }
    `;
    document.head.appendChild(s);
  }

  function fmtCountdown(minutes) {
    if (minutes <= 0) return '';
    if (minutes < 60)  return minutes + 'm';
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return h + 'h ' + (m ? (m + 'm') : '0m');
  }

  function render(payload) {
    if (sessionStorage.getItem('tw_clock_dismissed') === '1') return;
    ensureStyles();
    let el = document.getElementById(ID);
    if (!el) {
      el = document.createElement('div');
      el.id = ID;
      // Insert at the very top of <body> so it sits above the existing market ticker
      document.body.insertBefore(el, document.body.firstChild);
    }
    const phase = payload.phase || 'closed';
    const style = PHASE_STYLE[phase] || PHASE_STYLE.closed;
    el.style.background = style.bg;
    el.style.color = style.fg;
    const countdown = payload.is_open ? '' :
      (payload.minutes_to_open
        ? ' · opens in ' + fmtCountdown(payload.minutes_to_open)
        : '');
    el.innerHTML = `
      <span class="tw-mc-dot ${style.pulse ? 'pulse' : ''}" style="background:${style.dot}"></span>
      <span class="tw-mc-label">${payload.label || 'Market'}</span>
      <span class="tw-mc-meta">${countdown}</span>
      <button class="tw-mc-close" aria-label="Dismiss session banner">×</button>
    `;
    el.querySelector('.tw-mc-close').addEventListener('click', (e) => {
      e.stopPropagation();
      sessionStorage.setItem('tw_clock_dismissed', '1');
      el.remove();
    });
    lastPhase = phase;
  }

  async function poll() {
    try {
      const r = await fetch('/api/market/clock');
      const j = await r.json();
      if (j && j.success && j.data) render(j.data);
    } catch (_) { /* silent — banner just stops updating */ }
  }

  // Boot on DOM ready, then poll every minute. Page-stale data is fine.
  function boot() {
    poll();
    setInterval(poll, REFRESH_MS);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
