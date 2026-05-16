/* section-tabs.js — premium-feel cross-page tab strip.
 *
 * Mirrors the sidebar section grouping so users on any page in a section
 * can flip to sister pages with one click. The sidebar gives an overview;
 * these tabs give in-context navigation (Stripe / Linear style).
 *
 * Detects the current page from <body data-page> or the URL pathname,
 * locates which section it belongs to, and injects a horizontal tab
 * row at the top of <main>. If the page isn't in any section (auth,
 * legal, stock detail), nothing is injected.
 */
(function () {
  // Keep in sync with scripts/sync_sidebar.py SECTIONS.
  // Home is intentionally not part of any tab group (it's the dashboard).
  const SECTIONS = {
    Markets: [
      { slug: 'explore',     label: 'Explore',     icon: 'explore' },
      { slug: 'sectors',     label: 'Sectors',     icon: 'grid_view' },
      { slug: 'map',         label: 'Heatmap',     icon: 'map' },
      { slug: 'global',      label: 'Global',      icon: 'public' },
      { slug: 'commodities', label: 'Commodities', icon: 'inventory_2' },
    ],
    News: [
      { slug: 'events',   label: 'Newsroom', icon: 'newspaper' },
      { slug: 'earnings', label: 'Earnings', icon: 'event_note' },
      { slug: 'ipo',      label: 'IPOs',     icon: 'rocket_launch' },
      { slug: 'fo',       label: 'F&O',      icon: 'candlestick_chart' },
      { slug: 'ma',       label: 'M&A',      icon: 'handshake' },
    ],
    Tools: [
      { slug: 'screeners', label: 'Screeners', icon: 'filter_alt' },
      { slug: 'compare',   label: 'Compare',   icon: 'compare_arrows' },
      { slug: 'watchlist', label: 'Watchlist', icon: 'star' },
      { slug: 'alerts',    label: 'Alerts',    icon: 'notifications_active' },
    ],
    Insights: [
      { slug: 'analytics', label: 'Analytics', icon: 'insights' },
      { slug: 'forensics', label: 'Forensics', icon: 'policy' },
      { slug: 'trust',     label: 'Trust',     icon: 'verified' },
    ],
  };

  function currentSlug() {
    const path = (location.pathname || '').toLowerCase();
    const m = path.match(/\/?([a-z0-9_-]+)\.html?$/);
    return m ? m[1] : (path.replace(/\//g, '') || 'index');
  }

  function findSection(slug) {
    for (const [name, items] of Object.entries(SECTIONS)) {
      if (items.some(i => i.slug === slug)) return { name, items };
    }
    return null;
  }

  function buildHTML(section, activeSlug) {
    const tabs = section.items.map(it => {
      const active = it.slug === activeSlug;
      return `<a href="${it.slug}.html"
                 class="tw-section-tab${active ? ' is-active' : ''}"
                 aria-current="${active ? 'page' : 'false'}">
                <span class="material-symbols-outlined" style="font-size:15px;">${it.icon}</span>
                <span>${it.label}</span>
              </a>`;
    }).join('');
    return `<nav class="tw-section-tabs" data-section="${section.name}" aria-label="${section.name} navigation">
              <span class="tw-section-tabs-label">${section.name}</span>
              <div class="tw-section-tabs-row">${tabs}</div>
            </nav>`;
  }

  function mount() {
    const slug = currentSlug();
    const sec = findSection(slug);
    if (!sec) return;
    // Don't mount twice (HMR / re-init safety).
    if (document.querySelector('.tw-section-tabs')) return;
    const main = document.querySelector('main.main-content') || document.querySelector('main');
    if (!main) return;
    const wrap = document.createElement('div');
    wrap.innerHTML = buildHTML(sec, slug);
    const node = wrap.firstElementChild;
    main.insertBefore(node, main.firstChild);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
