/* Tickwave Global Search — shared across all pages.
 *
 * Mounting:
 *   1. Add an empty <div data-tw-search></div> wherever the bar should live.
 *   2. Include this file (auto-mounts on DOMContentLoaded).
 *   3. Or call window.SearchBar.mount(element, opts) manually.
 *
 * Hotkeys:
 *   Cmd/Ctrl+K  → focus search (industry standard)
 *   /           → focus search (when not typing)
 *   ↑/↓         → navigate results
 *   Enter       → open active result
 *   Esc         → close panel
 */
(function () {
  const API_BASE = (window.API_BASE || '/api').replace(/\/+$/, '');
  const QUICK_LINKS = [
    { label: 'Newsroom — All news',          href: 'events.html' },
    { label: 'Newsroom — Policy',            href: 'events.html?category=policy' },
    { label: 'Newsroom — IPO',               href: 'events.html?category=ipo' },
    { label: 'Newsroom — Geopolitical',      href: 'events.html?category=geopolitical' },
    { label: 'Newsroom — Earnings',          href: 'events.html?category=earnings' },
    { label: 'Alerts',                       href: 'alerts.html' },
    { label: 'Earnings calendar',            href: 'earnings.html' },
    { label: 'Sectors heatmap',              href: 'sectors.html' },
    { label: 'Watchlist',                    href: 'watchlist.html' },
    { label: 'Global markets',               href: 'global.html' },
  ];

  const SCAFFOLD = `
    <div class="tw-search-wrap">
      <div class="tw-search" tabindex="-1">
        <span class="material-symbols-outlined tw-search__icon">search</span>
        <input type="search" autocomplete="off" spellcheck="false"
               placeholder="Search stocks, news, sectors…" />
        <kbd class="tw-search__hint" aria-hidden="true">⌘K</kbd>
      </div>
      <div class="tw-search__panel" hidden>
        <div class="tw-search__group" data-group="stocks">
          <div class="tw-search__group-head">Stocks</div>
          <div class="tw-search__group-body" data-slot="stocks">
            <div class="tw-search__hint-row">Type at least 2 characters…</div>
          </div>
        </div>
        <div class="tw-search__group" data-group="news">
          <div class="tw-search__group-head">Newsroom</div>
          <div class="tw-search__group-body" data-slot="news"></div>
        </div>
        <div class="tw-search__group" data-group="quick">
          <div class="tw-search__group-head">Jump to</div>
          <div class="tw-search__group-body" data-slot="quick"></div>
        </div>
      </div>
    </div>`;

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function mount(target, opts) {
    opts = opts || {};
    const root = (typeof target === 'string') ? document.querySelector(target) : target;
    if (!root) return null;
    if (root.dataset.twSearchMounted === '1') return null;  // idempotent
    root.dataset.twSearchMounted = '1';

    root.innerHTML = SCAFFOLD;
    const wrap = root.querySelector('.tw-search-wrap');
    const input = wrap.querySelector('input[type="search"]');
    const panel = wrap.querySelector('.tw-search__panel');
    const slotStocks = wrap.querySelector('[data-slot="stocks"]');
    const slotNews   = wrap.querySelector('[data-slot="news"]');
    const slotQuick  = wrap.querySelector('[data-slot="quick"]');
    const hint       = wrap.querySelector('.tw-search__hint');

    // OS-aware shortcut display
    const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || '');
    if (hint) hint.textContent = isMac ? '⌘K' : 'Ctrl K';

    if (opts.placeholder) input.placeholder = opts.placeholder;

    function renderQuick(q) {
      const term = (q || '').toLowerCase();
      const items = term
        ? QUICK_LINKS.filter(x => x.label.toLowerCase().includes(term))
        : QUICK_LINKS.slice(0, 5);
      slotQuick.innerHTML = items.map(x =>
        `<a href="${x.href}" class="tw-search__row">
            <span class="material-symbols-outlined" style="font-size:16px;color:var(--rf-text-dim);">north_east</span>
            <span class="news-title">${escapeHtml(x.label)}</span>
         </a>`
      ).join('');
    }

    let debounceT = 0;
    let lastQ = '';

    async function runSearch(q) {
      lastQ = q;
      renderQuick(q);
      if (!q || q.length < 2) {
        slotStocks.innerHTML = '<div class="tw-search__hint-row">Type at least 2 characters…</div>';
        slotNews.innerHTML = '';
        return;
      }

      // ── Stocks ──
      slotStocks.innerHTML = '<div class="tw-search__hint-row">Searching stocks…</div>';
      try {
        const res = await fetch(API_BASE + '/search?q=' + encodeURIComponent(q), { credentials: 'same-origin' });
        if (lastQ !== q) return;
        const json = await res.json().catch(() => ({}));
        const rows = (json && json.data) || [];
        if (!rows.length) {
          slotStocks.innerHTML = '<div class="tw-search__hint-row">No matching stocks.</div>';
        } else {
          slotStocks.innerHTML = rows.slice(0, 6).map(r => {
            const pct = r.change_pct;
            const dir = pct == null ? '' : (pct >= 0 ? 'up' : 'dn');
            const pctStr = pct == null ? '' : (pct >= 0 ? '+' : '') + Number(pct).toFixed(2) + '%';
            const priceStr = r.price ? '₹' + Number(r.price).toFixed(2) : '';
            return `<a class="tw-search__row" href="stock.html?ticker=${encodeURIComponent(r.ticker)}">
              <span class="ticker">${escapeHtml(r.ticker)}</span>
              <span class="name">${escapeHtml(r.company || '')}</span>
              <span class="meta">${priceStr}</span>
              <span class="meta ${dir}">${pctStr}</span>
            </a>`;
          }).join('');
        }
      } catch (e) {
        slotStocks.innerHTML = '<div class="tw-search__hint-row">Search failed.</div>';
      }

      // ── News (via /api/feed) ──
      slotNews.innerHTML = '<div class="tw-search__hint-row">Searching news…</div>';
      try {
        const looksTicker = /^[A-Z][A-Z0-9&]{1,14}$/.test(q.toUpperCase().trim());
        const url = looksTicker
          ? API_BASE + '/feed?ticker=' + encodeURIComponent(q.toUpperCase().trim()) + '&limit=20&hours=168'
          : API_BASE + '/feed?limit=80&hours=72';
        const res = await fetch(url, { credentials: 'same-origin' });
        if (lastQ !== q) return;
        const json = await res.json().catch(() => ({}));
        let rows = (json && json.data) || [];
        if (!looksTicker) {
          const term = q.toLowerCase();
          rows = rows.filter(it =>
            (it.title || '').toLowerCase().includes(term) ||
            (it.summary || '').toLowerCase().includes(term)
          );
        }
        if (!rows.length) {
          slotNews.innerHTML = '<div class="tw-search__hint-row">No matching news.</div>';
        } else {
          slotNews.innerHTML = rows.slice(0, 5).map(it => {
            const cat = it.category || 'news';
            const link = it.link || ('events.html?category=' + cat);
            const newWin = it.link ? ' target="_blank" rel="noopener"' : '';
            return `<a class="tw-search__row" href="${escapeHtml(link)}"${newWin}>
              <span class="news-cat">${escapeHtml(cat.replace('_', ' '))}</span>
              <span class="news-title">${escapeHtml(it.title || '')}</span>
            </a>`;
          }).join('');
        }
      } catch (e) {
        slotNews.innerHTML = '<div class="tw-search__hint-row">News search failed.</div>';
      }
    }

    function showPanel() { panel.hidden = false; }
    function hidePanel() { panel.hidden = true; }

    input.addEventListener('input', () => {
      clearTimeout(debounceT);
      const q = input.value.trim();
      showPanel();
      debounceT = setTimeout(() => runSearch(q), 180);
    });
    input.addEventListener('focus', () => { renderQuick(input.value.trim()); showPanel(); });
    document.addEventListener('click', (e) => { if (!wrap.contains(e.target)) hidePanel(); });

    // Keyboard navigation
    let activeIdx = -1;
    function getRows() { return Array.from(panel.querySelectorAll('.tw-search__row')); }
    function setActive(i) {
      const rows = getRows();
      rows.forEach((r, j) => r.classList.toggle('is-active', j === i));
      const a = rows[i]; if (a) a.scrollIntoView({ block: 'nearest' });
    }
    input.addEventListener('keydown', (e) => {
      const rows = getRows();
      if (e.key === 'ArrowDown') { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, rows.length - 1); setActive(activeIdx); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); setActive(activeIdx); }
      else if (e.key === 'Enter' && activeIdx >= 0 && rows[activeIdx]) { e.preventDefault(); rows[activeIdx].click(); }
      else if (e.key === 'Escape') { input.blur(); hidePanel(); }
      else { activeIdx = -1; }
    });

    // Global hotkeys: Cmd/Ctrl+K and "/"
    if (!window.__twSearchHotkeysBound) {
      window.__twSearchHotkeysBound = true;
      document.addEventListener('keydown', (e) => {
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        const editing = tag === 'INPUT' || tag === 'TEXTAREA' ||
                        (document.activeElement && document.activeElement.isContentEditable);
        if ((e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey)) {
          const i = document.querySelector('.tw-search input[type="search"]');
          if (i) { e.preventDefault(); i.focus(); i.select(); i.dispatchEvent(new Event('focus')); }
        } else if (e.key === '/' && !editing) {
          const i = document.querySelector('.tw-search input[type="search"]');
          if (i) { e.preventDefault(); i.focus(); }
        }
      });
    }

    return { focus: () => input.focus(), reload: () => runSearch(input.value.trim()) };
  }

  // Auto-mount: any element with [data-tw-search] becomes a search bar.
  function autoMount() {
    document.querySelectorAll('[data-tw-search]').forEach(el => mount(el));
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoMount);
  } else {
    autoMount();
  }

  window.SearchBar = { mount };
})();
