/* event-card.js — shared news/event card renderer.
 *
 * Single template used by Newsroom (events.html), global.html, policy.html,
 * social.html, earnings.html, ipo.html, stock.html news tab, index.html
 * Must Read strip, and the alerts.html Top News rail.
 *
 * Exposes window.EventCard with:
 *   render(item, opts)   -> HTML string for one card
 *   renderList(items)    -> HTML string for a list (with empty state)
 *   fetchFeed(params)    -> Promise<Array> hitting /api/feed
 *   mountFeed(elId, params, opts)  -> render + return reload() handle
 *
 * Item shape (the unified /api/feed payload):
 *   {id, event_id, category, title, summary, source, source_tier,
 *    alpha_score, sentiment, magnitude, impact_score, companies (array),
 *    published_at, age_hours, freshness, forensic_band, event_type, link}
 */
(function () {
  const API_BASE = (window.API_BASE || '/api').replace(/\/+$/, '');

  // ---- formatting helpers ----
  function timeAgo(iso) {
    if (!iso) return '';
    const t = (typeof iso === 'number') ? (iso < 1e12 ? iso * 1000 : iso) : Date.parse(iso);
    if (!t || isNaN(t)) return '';
    const m = (Date.now() - t) / 60000;
    if (m < 1) return 'just now';
    if (m < 60) return Math.floor(m) + 'm ago';
    const h = m / 60;
    if (h < 24) return Math.floor(h) + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  function categoryLabel(cat) {
    return ({
      corporate: 'Corporate',
      earnings: 'Earnings',
      policy: 'Policy',
      ipo: 'IPO',
      geopolitical: 'Geopolitical',
      social: 'Social',
      commodity: 'Commodity',
      forensic: 'Forensic',
      bulk_deal: 'Bulk Deal',
      fo: 'F&O',
    })[cat] || (cat || 'News');
  }

  function sentimentClass(s) {
    const v = (s || '').toLowerCase();
    if (v === 'bullish' || v === 'positive') return 'bull';
    if (v === 'bearish' || v === 'negative') return 'bear';
    return 'neutral';
  }

  function tierBadge(tier) {
    if (!tier) return '';
    const labels = { 1: 'T1 verified', 2: 'press', 3: 'aggregator', 4: 'social' };
    return labels[tier] || '';
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ---- card template ----
  function render(item, opts) {
    opts = opts || {};
    const sent = sentimentClass(item.sentiment);
    const sentColor = sent === 'bull' ? 'var(--rf-bull)' :
                       sent === 'bear' ? 'var(--rf-bear)' : 'var(--rf-text-mute)';

    const title = escapeHtml(item.title || 'Event');
    const summary = escapeHtml((item.summary || '').slice(0, 360));
    const score = Math.round(item.impact_score || 0);
    const alpha = item.alpha_score != null ? Number(item.alpha_score).toFixed(0) : null;
    const cat = categoryLabel(item.category);
    const source = escapeHtml((item.source || '').replace(/_/g, ' '));
    const fresh = item.freshness || (item.age_hours != null && item.age_hours < 2 ? 'BREAKING' : '');
    const tier = tierBadge(item.source_tier);
    const tickers = (item.companies || []).slice(0, 5);

    const link = item.link || '#';
    const linkAttrs = item.link ? `href="${escapeHtml(item.link)}" target="_blank" rel="noopener"` : 'href="#" onclick="return false;"';

    const forensic = item.forensic_band && item.forensic_band !== 'clean'
      ? `<span class="ec-badge ec-forensic" title="Forensic flag">${escapeHtml(item.forensic_band).replace(/_/g, ' ')}</span>` : '';

    const tickerChips = tickers.length ? `
      <div class="ec-tickers">
        ${tickers.map(t => `<span class="ec-ticker" data-ticker="${escapeHtml(t)}">${escapeHtml(t)}</span>`).join('')}
      </div>` : '';

    const evId = item.event_id || item.id || '';
    const tldrBtn = evId
      ? `<button class="ec-tldr-btn" data-tldr-event="${escapeHtml(evId)}" title="AI summary"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:-2px;">auto_awesome</span> TL;DR</button>`
      : '';

    // The whole card carries data-ticker pointing at the first company so any
    // click on the card body (title, summary, meta) opens the stock popup —
    // not just the small chip pills. The TL;DR button + the link inside the
    // title both opt out via data-no-popup so they keep their original action.
    const primaryTicker = tickers[0] || '';
    // data-no-logo: the card uses data-ticker only to wire the popup click —
    // it's NOT a chip itself, so company-logo.js's auto-decorator must skip
    // it. (Without this, a 14px logo gets injected at position-0 of the flex
    // card and collapses the title column to one word per line.)
    const cardTickerAttr = primaryTicker
      ? `data-ticker="${escapeHtml(primaryTicker)}" data-no-logo`
      : '';

    return `
      <article class="ec-card ec-${sent}${primaryTicker ? ' ec-card--clickable' : ''}" data-category="${escapeHtml(item.category || '')}" data-id="${escapeHtml(evId)}" ${cardTickerAttr}>
        <div class="ec-rail" style="background:${sentColor};"></div>
        <div class="ec-impact">
          <div class="ec-impact-num">${score}</div>
          <div class="ec-impact-label">impact</div>
          ${alpha != null ? `<div class="ec-alpha"><span>${alpha}</span><label>α</label></div>` : ''}
        </div>
        <div class="ec-body">
          <div class="ec-meta">
            <span class="ec-cat">${escapeHtml(cat)}</span>
            ${source ? `<span class="ec-src">${source}</span>` : ''}
            ${fresh ? `<span class="ec-badge ec-fresh">${escapeHtml(fresh)}</span>` : ''}
            ${tier ? `<span class="ec-badge ec-tier">${escapeHtml(tier)}</span>` : ''}
            ${forensic}
            ${window.NewsTime ? NewsTime.renderPill(item.published_at || item.created_at, { showAge: false }) : ''}
            <span class="ec-spacer"></span>
            ${tldrBtn ? tldrBtn.replace('<button ', '<button data-no-popup ') : ''}
            <span class="ec-time">${timeAgo(item.published_at || item.created_at)}</span>
          </div>
          <h3 class="ec-title"><a ${linkAttrs} data-no-popup>${title}</a></h3>
          ${summary ? `<p class="ec-summary">${summary}</p>` : ''}
          <div class="ec-tldr" data-tldr-mount hidden></div>
          ${tickerChips}
        </div>
      </article>`;
  }

  function renderList(items, opts) {
    opts = opts || {};
    if (!items || !items.length) {
      return `<div class="ec-empty">
        <div class="ec-empty-title">${escapeHtml(opts.emptyTitle || 'No items yet')}</div>
        <div class="ec-empty-sub">${escapeHtml(opts.emptySub || 'Check back in a few minutes.')}</div>
      </div>`;
    }
    return `<div class="ec-list">${items.map(it => render(it, opts)).join('')}</div>`;
  }

  // ---- data ----
  async function fetchFeed(params) {
    params = params || {};
    const qs = new URLSearchParams();
    Object.keys(params).forEach(k => {
      const v = params[k];
      if (v != null && v !== '') qs.set(k, v);
    });
    const url = API_BASE + '/feed' + (qs.toString() ? '?' + qs.toString() : '');
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      const json = await res.json();
      return Array.isArray(json.data) ? json.data : [];
    } catch (e) {
      return [];
    }
  }

  function mountFeed(elId, params, opts) {
    opts = opts || {};
    const el = typeof elId === 'string' ? document.getElementById(elId) : elId;
    if (!el) return { reload: () => {} };
    el.innerHTML = `<div class="ec-loading">Loading…</div>`;
    let currentParams = Object.assign({}, params || {});

    async function reload(extra) {
      if (extra) currentParams = Object.assign({}, currentParams, extra);
      const items = await fetchFeed(currentParams);
      el.innerHTML = renderList(items, opts);
      if (opts.onRendered) try { opts.onRendered(items); } catch (e) {}
      return items;
    }
    reload();
    return { reload, getParams: () => currentParams };
  }

  // Wire ticker chips → stock.html navigation (event delegation, page-wide).
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (t && t.classList && t.classList.contains('ec-ticker')) {
      const sym = t.getAttribute('data-ticker');
      if (sym) location.href = `stock.html?ticker=${encodeURIComponent(sym)}`;
    }
  });

  // TL;DR button → fetch /api/news/summarize, inline-mount the result.
  // Tolerant to a clicked <span> inside the button.
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest && e.target.closest('button[data-tldr-event]');
    if (!btn) return;
    e.preventDefault(); e.stopPropagation();
    const evId = btn.getAttribute('data-tldr-event');
    if (!evId) return;
    const card = btn.closest('.ec-card');
    const mount = card && card.querySelector('[data-tldr-mount]');
    if (!mount) return;
    if (mount.dataset.loaded === '1') {
      mount.hidden = !mount.hidden;
      return;
    }
    btn.disabled = true;
    const prev = btn.innerHTML;
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:-2px;">hourglass_empty</span>';
    try {
      const res = await fetch(API_BASE + '/news/summarize?event_id=' + encodeURIComponent(evId), { credentials: 'same-origin' });
      const json = await res.json();
      const data = (json && json.data) || {};
      const summary = data.summary || 'No summary available.';
      const src = data.source === 'groq' ? 'AI · Groq' : 'TL;DR';
      mount.innerHTML = `<strong class="ec-tldr-tag">${escapeHtml(src)}</strong> ${escapeHtml(summary)}`;
      mount.dataset.loaded = '1';
      mount.hidden = false;
    } catch (err) {
      mount.innerHTML = `<span style="color:var(--rf-bear);">Couldn't summarize.</span>`;
      mount.hidden = false;
    } finally {
      btn.disabled = false;
      btn.innerHTML = prev;
    }
  });

  window.EventCard = {
    render, renderList, fetchFeed, mountFeed,
    categoryLabel, timeAgo,
  };
})();
