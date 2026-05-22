/* sidebar.js — single source of truth for the desktop sidebar.
 *
 * Auto-mounted by bootstrap.js. On DOMContentLoaded, finds the page's
 * `<aside class="sidebar">` (which currently hard-codes 15+ links per
 * page) and replaces its contents with the canonical 8-entry IA. This
 * keeps the legacy mobile drawer behavior intact (sidebar-toggle.js
 * still toggles `body.sidebar--open`) while presenting a focused nav.
 *
 * IA decisions (from the 6-week production plan):
 *   PRIMARY:  Today · Explore · Events · Sectors · Watchlist · Tools · Insights
 *   LAB:      experimental / demoted pages (social, global, commodities,
 *             premover-classic, map-extras)
 *
 * Pages collapsed:
 *   premover.html      → Explore (view toggle)
 *   earnings/ipo/ma/policy/fo.html → Events (filter chips)
 *   alerts.html        → Watchlist (tab)
 *   map.html           → Sectors (heatmap tab)
 *   paper/simulator    → Tools › Practice
 *
 * Skip pages: landing, login, signup, onboarding — they manage their own chrome.
 */
(function () {
  'use strict';
  if (window.__aeSidebarMounted) return;
  window.__aeSidebarMounted = true;

  const SKIP = /(^|\/)(landing|login|signup|onboarding)\.html/i;
  if (SKIP.test(location.pathname)) return;

  const IA = {
    primary: [
      { href: 'index.html',     icon: 'home',           label: 'Today',     match: /(^|\/)(index\.html|\/?)$/i },
      { href: 'explore.html',   icon: 'travel_explore', label: 'Explore',   match: /(^|\/)(explore|premover)\.html/i },
      { href: 'events.html',    icon: 'feed',           label: 'Events',    match: /(^|\/)(events|ipo|ma|policy|fo)\.html/i },
      { href: 'sectors.html',   icon: 'grid_view',      label: 'Sectors',   match: /(^|\/)(sectors|map)\.html/i },
      { href: 'watchlist.html', icon: 'bookmark',       label: 'Watchlist', match: /(^|\/)(watchlist|alerts)\.html/i },
    ],
    tools: [
      { href: 'screeners.html', icon: 'filter_alt',     label: 'Screeners', match: /screeners\.html/i },
      { href: 'compare.html',   icon: 'compare_arrows', label: 'Compare',   match: /compare\.html/i },
      { href: 'paper.html',     icon: 'science',        label: 'Practice',  match: /(paper|simulator)\.html/i },
    ],
    data: [
      { href: 'earnings.html',             icon: 'auto_graph',        label: 'Earnings & forecast', match: /earnings\.html/i },
      { href: 'filings.html',              icon: 'description',       label: 'Filings archive',     match: /(^|\/)filings\.html(?!#transcripts)/i },
      { href: 'filings.html#transcripts',  icon: 'record_voice_over', label: 'Concall transcripts', match: /filings\.html#transcripts/i },
      { href: 'api-docs.html',             icon: 'code',              label: 'API · v1',       match: /api-docs\.html/i },
    ],
    insights: [
      { href: 'analytics.html',   icon: 'insights',    label: 'Analytics',    match: /analytics\.html/i },
      { href: 'forensics.html',   icon: 'policy',      label: 'Red flags',    match: /forensics\.html/i },
      { href: 'trust.html',       icon: 'verified',    label: 'Track record', match: /trust\.html/i },
      { href: 'methodology.html', icon: 'menu_book',   label: 'Methodology',  match: /methodology\.html/i },
    ],
    lab: [
      { href: 'social.html',      icon: 'forum',     label: 'Social',      match: /social\.html/i },
      { href: 'global.html',      icon: 'public',    label: 'Global',      match: /global\.html/i },
      { href: 'commodities.html', icon: 'inventory_2', label: 'Commodities', match: /commodities\.html/i },
    ],
  };

  function activeFor(path, items) {
    const lower = (path + (location.hash || '')).toLowerCase();
    return items.find((it) => it.match.test(lower));
  }

  function linkHTML(item, activeItem) {
    const cls = item === activeItem ? 'sidebar-link active' : 'sidebar-link';
    return `<a class="${cls}" href="${item.href}">
      <span class="material-symbols-outlined sidebar-icon" aria-hidden="true">${item.icon}</span>${item.label}
    </a>`;
  }

  function render(sidebar) {
    const all = [...IA.primary, ...IA.tools, ...IA.data, ...IA.insights, ...IA.lab];
    const active = activeFor(location.pathname, all);

    // Preserve any existing header (logo) — JS only replaces the link list.
    const header = sidebar.querySelector('.sidebar-header');
    const headerHTML = header ? header.outerHTML : `
      <div class="sidebar-header">
        <a href="index.html" style="display:flex;align-items:center;gap:10px;text-decoration:none;color:inherit;">
          <div style="width:28px;height:28px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:var(--surface-1);font-weight:800;">A</div>
          <div>
            <div style="font-weight:700;font-size:14px;color:var(--text-primary);">AlphaEvent</div>
            <div class="sidebar-brand-sub">Tickwave</div>
          </div>
        </a>
      </div>`;

    // Preserve toggle button at the bottom (sidebar-toggle.js may inject one).
    const toggle = sidebar.querySelector('.sidebar-toggle');
    const toggleHTML = toggle ? toggle.outerHTML : '';

    sidebar.innerHTML = `
      ${headerHTML}
      <nav aria-label="Primary navigation">
        ${IA.primary.map((i) => linkHTML(i, active)).join('')}

        <div class="sidebar-section">Tools</div>
        ${IA.tools.map((i) => linkHTML(i, active)).join('')}

        <div class="sidebar-section">Data</div>
        ${IA.data.map((i) => linkHTML(i, active)).join('')}

        <div class="sidebar-section">Insights</div>
        ${IA.insights.map((i) => linkHTML(i, active)).join('')}

        <div class="sidebar-section" style="opacity:0.7;">Lab <span style="font-size:9px;padding:1px 5px;background:var(--caution-dim);color:var(--caution);border-radius:4px;margin-left:4px;">BETA</span></div>
        ${IA.lab.map((i) => linkHTML(i, active)).join('')}
      </nav>
      ${toggleHTML}
    `;
  }

  function mount() {
    const sidebar = document.querySelector('.sidebar');
    if (!sidebar) return;
    try { render(sidebar); }
    catch (e) { console.warn('[sidebar] render failed', e); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
