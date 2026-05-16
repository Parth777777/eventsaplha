/* marquee.js — Live signal ticker on the landing page.
 * Pulls /api/feed?limit=20, renders cards, duplicates content for
 * seamless CSS marquee loop, refreshes every 30s.
 * Click → opens the real stock popup (reuse stock-popup.js if loaded).
 */

const ESC = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));

function alphaBand(a) {
  if (a >= 65) return 'high';
  if (a >= 50) return 'mid';
  return 'low';
}

function ageLabel(sig) {
  const h = sig.age_hours;
  if (h == null) return '';
  if (h < 1)  return Math.round(h * 60) + 'm';
  if (h < 24) return Math.round(h) + 'h';
  return Math.round(h / 24) + 'd';
}

function renderCard(sig) {
  const alpha = Math.round(sig.alpha_score || 0);
  const band  = alphaBand(alpha);
  const tk    = (sig.ticker || '—').toUpperCase();
  const logo  = `/api/logo/${encodeURIComponent(tk.replace(/\.(NS|BO)$/i,''))}`;
  return `
    <article class="lp-sig" data-ticker="${ESC(tk)}" role="button" tabindex="0"
             aria-label="Open ${ESC(tk)} signal detail">
      <div class="lp-sig-head">
        <span class="lp-sig-tk">
          <img src="${logo}" alt="" loading="lazy" decoding="async">
          ${ESC(tk)}
        </span>
        <span class="lp-sig-alpha ${band}">${alpha}</span>
      </div>
      <div class="lp-sig-headline">${ESC((sig.headline || sig.title || '').slice(0,120))}</div>
      <div class="lp-sig-meta">
        <span>${ESC((sig.event_type || 'news').replace(/_/g,' '))}</span>
        <span>·</span>
        <span>${ESC(ageLabel(sig))}</span>
      </div>
    </article>`;
}

async function fetchSignals() {
  // /api/signals carries ticker + alpha_score directly. Fall back to /api/feed
  // (event-level) so the marquee still renders even when no scored signals exist.
  try {
    const r = await fetch('/api/signals?limit=24');
    const j = await r.json();
    const sigs = (j && j.data) || [];
    if (sigs.length) return sigs;
  } catch (_) { /* fall through */ }
  try {
    const r = await fetch('/api/feed?limit=24');
    const j = await r.json();
    return ((j && j.data) || [])
      .filter(e => (e.companies && e.companies.length) || e.ticker)
      .map(e => ({
        ticker: e.ticker || (e.companies && e.companies[0]) || '',
        alpha_score: e.impact_score || (e.magnitude ? e.magnitude * 10 : 0),
        headline: e.title || e.headline || e.summary || '',
        event_type: e.event_type,
        age_hours: e.age_hours,
      }));
  } catch (_) {
    return [];
  }
}

function attachClickToPopup(host) {
  // The stock-popup global handler already listens on window for [data-ticker]
  // clicks, so we don't need to bind anything explicitly — but if it isn't
  // loaded on this page, fall back to a programmatic open if available.
  host.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('.lp-sig')) {
      e.preventDefault();
      e.target.closest('.lp-sig').click();
    }
  });
}

export async function initMarquee() {
  const host = document.querySelector('#lp-marquee');
  if (!host) return;

  // Load stock-popup.js lazily so clicks on cards open the real product modal.
  if (!window.StockPopup) {
    const s = document.createElement('script');
    s.src = '/shared/js/stock-popup.js';
    s.defer = true;
    document.head.appendChild(s);
  }
  // company-logo.js helper too (needed by the popup for the brand chip)
  if (!window.CompanyLogo) {
    const s = document.createElement('script');
    s.src = '/shared/js/company-logo.js';
    s.defer = true;
    document.head.appendChild(s);
  }

  async function render() {
    const items = await fetchSignals();
    if (!items.length) {
      host.innerHTML = `<div class="lp-marquee-loading">No live signals yet — check back in a few minutes.</div>`;
      return;
    }
    // Duplicate the set so the CSS marquee can loop seamlessly.
    const cards = items.map(renderCard).join('');
    host.innerHTML = cards + cards;
  }
  attachClickToPopup(host);
  await render();
  // Refresh every 60s; pause on hover handled by CSS animation-play-state.
  setInterval(render, 60_000);
}
