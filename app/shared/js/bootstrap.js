/* bootstrap.js — site-wide init.
 * Loaded after app.js on every page. Side effects only.
 *
 * Responsibilities:
 *   1. Register service worker (PWA + push)
 *   2. Inject the SEBI compliance footer
 *   3. Telemetry beacon helper (window.Telemetry)
 *   4. Onboarding gate (redirect first-time users to /onboarding.html)
 */
(function () {
  // ---- 1. Service worker
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    });
  }

  // ---- 2. SEBI / compliance footer
  function mountFooter() {
    if (document.getElementById('eaFooter')) return;
    const f = document.createElement('footer');
    f.id = 'eaFooter';
    f.style.cssText = 'margin:48px 24px 24px; padding:18px; background:#0d1118; border:1px solid #1f2336; border-radius:10px; font-size:11px; color:#9aa0c8; line-height:1.6;';
    f.innerHTML = `
      <div style="display:flex; gap:14px; align-items:flex-start; flex-wrap:wrap;">
        <span class="material-symbols-outlined" style="color:#ff9466; font-size:18px; flex-shrink:0;">gavel</span>
        <div style="flex:1; min-width:240px;">
          <b style="color:#dde3ef;">Not investment advice.</b>
          Tickwave publishes statistical research signals derived from public Indian market data (NSE/BSE filings, SEBI disclosures, RBI press, financial press). It is research tooling, not a SEBI-registered investment advisor. Past performance ≠ future results. Always do your own due diligence.
          <span style="color:#5a5d6a;">·</span>
          <a href="trust.html" style="color:#4ee6b8;">View track record</a>
          <span style="color:#5a5d6a;">·</span>
          <a href="https://www.sebi.gov.in/" target="_blank" rel="noopener" style="color:#4ee6b8;">SEBI</a>
        </div>
      </div>`;
    (document.querySelector('main') || document.body).appendChild(f);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountFooter);
  } else {
    mountFooter();
  }

  // ---- 3. Telemetry helper (POST best-effort, never blocks UI)
  window.Telemetry = {
    record: (kind, target, metadata) => {
      try {
        const body = JSON.stringify({ kind, target, metadata });
        if (navigator.sendBeacon) {
          const blob = new Blob([body], { type: 'application/json' });
          navigator.sendBeacon('/api/telemetry', blob);
        } else {
          fetch('/api/telemetry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true });
        }
      } catch (e) {}
    },
  };
  // Auto-track page view
  try { window.Telemetry.record('view', location.pathname); } catch (e) {}

  // Auto-track outbound link clicks on event cards (data-ticker attr)
  document.addEventListener('click', (e) => {
    const tk = e.target.closest('[data-ticker]');
    if (tk) {
      window.Telemetry.record('click', tk.getAttribute('data-ticker'), { page: location.pathname });
    }
  }, { capture: true });

  // ---- 4. Onboarding gate (best-effort)
  // Skip for the onboarding page itself, login/signup, and the trust page.
  const path = location.pathname;
  const skip = /onboarding|login|signup|trust/.test(path);
  if (!skip && !sessionStorage.getItem('ea_onboarded_check')) {
    sessionStorage.setItem('ea_onboarded_check', '1');
    fetch('/api/onboarding/state').then((r) => r.json()).then((j) => {
      if (j && j.success && j.onboarded === false && !localStorage.getItem('ea_skipped_onboarding')) {
        // First-time visitor — gentle nudge
        const banner = document.createElement('div');
        banner.style.cssText = 'position:fixed; bottom:16px; right:16px; max-width:340px; background:#0d1118; border:1px solid #4ee6b855; border-radius:12px; padding:14px 16px; box-shadow:0 8px 24px rgba(0,0,0,0.5); z-index:9999; font-size:12px; color:#dde3ef; font-family:DM Sans,sans-serif;';
        banner.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:12px;">
            <div>
              <div style="font-weight:800; color:#4ee6b8; margin-bottom:4px;">Welcome to Tickwave</div>
              <div style="color:#9aa0c8;">Pick 5 stocks and you're set in 60 seconds.</div>
              <div style="margin-top:10px; display:flex; gap:8px;">
                <a href="onboarding.html" style="background:#5b6cff;color:#fff;padding:6px 14px;border-radius:6px;font-weight:700;text-decoration:none;font-size:11px;">Get started →</a>
                <button onclick="localStorage.setItem('ea_skipped_onboarding','1');this.closest('div[style]').parentElement.remove();" style="background:transparent;color:#5a5d6a;border:0;cursor:pointer;font-size:11px;">Skip</button>
              </div>
            </div>
            <button onclick="this.closest('div[style*=fixed]').remove();" style="background:transparent;color:#5a5d6a;border:0;cursor:pointer;font-size:18px;line-height:1;">×</button>
          </div>`;
        document.body.appendChild(banner);
      }
    }).catch(() => {});
  }

  // ---- 5. Sidebar grouping
  // The sidebar is duplicated as plain HTML across 21 pages with links in
  // an ad-hoc order. We physically reorder the links into named sections
  // and insert section headers, so users see Markets/Research/Activity/
  // Insights cleanly grouped no matter what order the page authored.
  // Links not listed in any section are appended at the end (so a future
  // page that adds an unmapped link doesn't disappear from the sidebar).
  function groupSidebar() {
    const nav = document.querySelector('aside.sidebar nav.sidebar-nav');
    if (!nav || nav.dataset.grouped === '1') return;
    const SECTIONS = [
      { title: 'Markets',  items: ['index', 'explore', 'sectors', 'map', 'global', 'commodities'] },
      { title: 'Research', items: ['compare', 'screeners', 'earnings', 'events'] },
      { title: 'Activity', items: ['watchlist', 'alerts', 'portfolio', 'paper', 'simulator'] },
      { title: 'Insights', items: ['analytics', 'social', 'trust'] },
    ];
    const links = Array.from(nav.querySelectorAll('a.sidebar-link'));
    if (!links.length) return;
    const byHref = new Map();
    links.forEach((a) => {
      const file = (a.getAttribute('href') || '').replace(/\.html$/, '').toLowerCase().trim();
      if (file && !byHref.has(file)) byHref.set(file, a);
    });
    const used = new Set();
    // Detach all sidebar links from the nav before re-appending in section order.
    links.forEach((a) => a.remove());

    function makeHeader(title, isFirst) {
      const h = document.createElement('div');
      h.className = 'sidebar-section';
      h.textContent = title;
      h.style.cssText = 'padding:' + (isFirst ? '4px' : '12px') + ' 14px 4px; font-size:8.5px; font-weight:800; letter-spacing:0.18em; text-transform:uppercase; color:rgba(138,148,168,0.55);' + (isFirst ? '' : 'border-top:1px solid rgba(255,255,255,0.05); margin-top:6px;');
      return h;
    }

    let firstSectionRendered = false;
    SECTIONS.forEach((sec) => {
      // Collect this section's existing links (in section order, not page order)
      const matched = sec.items.map((k) => byHref.get(k)).filter(Boolean);
      if (!matched.length) return;
      nav.appendChild(makeHeader(sec.title, !firstSectionRendered));
      firstSectionRendered = true;
      matched.forEach((a) => { nav.appendChild(a); used.add(a); });
    });

    // Append any links not assigned to a section (defensive — keeps unknown
    // pages reachable rather than silently dropping them).
    const orphans = links.filter((a) => !used.has(a));
    if (orphans.length) {
      nav.appendChild(makeHeader('More', !firstSectionRendered));
      orphans.forEach((a) => nav.appendChild(a));
    }
    nav.dataset.grouped = '1';
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', groupSidebar);
  } else {
    groupSidebar();
  }

  // PWA: link the manifest if absent (avoids editing every page)
  if (!document.querySelector('link[rel="manifest"]')) {
    const l = document.createElement('link');
    l.rel = 'manifest'; l.href = '/manifest.json';
    document.head.appendChild(l);
  }
  // Add theme-color meta if missing (PWA requirement for status-bar tint)
  if (!document.querySelector('meta[name="theme-color"]')) {
    const m = document.createElement('meta');
    m.name = 'theme-color'; m.content = '#080c12';
    document.head.appendChild(m);
  }
})();
