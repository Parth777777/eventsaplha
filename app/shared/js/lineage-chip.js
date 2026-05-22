/* lineage-chip.js — drop-in data-provenance chip.
 *
 * Brand signal for the "we are a data platform" positioning: every number on
 * every page can carry a tiny chip that shows WHERE the data came from and
 * WHEN we retrieved it. Click the chip → open the source URL (if present)
 * or copy the full lineage tooltip.
 *
 * Usage — declarative:
 *
 *   <span data-lineage-source="bse_filings"
 *         data-lineage-as-of="2026-05-12T14:32:00Z"
 *         data-lineage-url="https://www.bseindia.com/.../filing.pdf">
 *     ₹2.99 lakh crore
 *   </span>
 *
 *   <!-- Auto-decorated on DOMContentLoaded; you can also call window
 *        LineageChip.refresh() after a re-render. -->
 *
 * Usage — imperative:
 *
 *   const html = LineageChip.render({source: 'yfinance',
 *                                    as_of: '2026-05-18T09:25:00Z'});
 *   el.innerHTML = `Last price <strong>₹1,335.90</strong> ${html}`;
 */
(function () {
  if (window.LineageChip) return;

  // Canonical labels — each "source" key gets a short pretty name + color.
  // Keep this list small and stable; it's the public lexicon.
  const SOURCES = {
    bse_filings:    { label: 'BSE Filing',     color: '#2dd4aa', kind: 'tier1' },
    nse_filings:    { label: 'NSE Filing',     color: '#2dd4aa', kind: 'tier1' },
    sebi:           { label: 'SEBI',           color: '#2dd4aa', kind: 'tier1' },
    rbi:            { label: 'RBI',            color: '#2dd4aa', kind: 'tier1' },
    pib:            { label: 'PIB',            color: '#2dd4aa', kind: 'tier1' },
    yfinance:       { label: 'Yahoo Finance',  color: '#8eb4e0', kind: 'tier2' },
    reuters:        { label: 'Reuters',        color: '#8eb4e0', kind: 'tier2' },
    bloomberg:      { label: 'Bloomberg',      color: '#8eb4e0', kind: 'tier2' },
    et:             { label: 'ET',             color: '#8eb4e0', kind: 'tier2' },
    moneycontrol:   { label: 'Moneycontrol',   color: '#8eb4e0', kind: 'tier2' },
    mint:           { label: 'Mint',           color: '#8eb4e0', kind: 'tier2' },
    google_news:    { label: 'Google News',    color: '#a4afc2', kind: 'tier3' },
    classifier:     { label: 'TickerWave classifier', color: '#a78bfa', kind: 'derived' },
    causal_map:     { label: 'TickerWave causal map', color: '#a78bfa', kind: 'derived' },
    alpha_engine:   { label: 'TickerWave alpha engine', color: '#a78bfa', kind: 'derived' },
    fundamentals_score: { label: 'TickerWave F-score', color: '#a78bfa', kind: 'derived' },
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function prettySource(raw) {
    if (!raw) return { label: 'unknown', color: '#6a7484', kind: 'unknown' };
    const key = String(raw).toLowerCase().replace(/[^a-z0-9_]/g, '_');
    if (SOURCES[key]) return SOURCES[key];
    // Heuristic match for prefixes we haven't mapped yet
    for (const k of Object.keys(SOURCES)) {
      if (key.includes(k) || k.includes(key.split('_')[0])) return SOURCES[k];
    }
    return { label: raw, color: '#a4afc2', kind: 'aggregator' };
  }

  function timeAgo(iso) {
    if (!iso) return '';
    const t = (typeof iso === 'number') ? (iso < 1e12 ? iso * 1000 : iso) : Date.parse(iso);
    if (!t || isNaN(t)) return '';
    const m = (Date.now() - t) / 60000;
    if (m < 1) return 'now';
    if (m < 60) return Math.floor(m) + 'm ago';
    const h = m / 60;
    if (h < 24) return Math.floor(h) + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  function render(opts) {
    opts = opts || {};
    const meta = prettySource(opts.source);
    const ago = timeAgo(opts.as_of);
    const tip = [
      `Source: ${meta.label}`,
      opts.as_of ? `Retrieved: ${opts.as_of}` : null,
      opts.url ? `URL: ${opts.url}` : null,
      opts.note || null,
    ].filter(Boolean).join('\n');
    const inner = `${meta.label}${ago ? ' · ' + ago : ''}`;
    const tag = opts.url ? 'a' : 'span';
    const attrs = opts.url
      ? `href="${esc(opts.url)}" target="_blank" rel="noopener"`
      : '';
    return `<${tag} class="tw-lineage" data-kind="${meta.kind}"
              style="display:inline-flex;align-items:center;gap:4px;
                     padding:1px 7px;border-radius:3px;
                     font:600 9px/1.2 'Geist Mono',monospace;
                     letter-spacing:0.04em;text-decoration:none;
                     color:${meta.color};
                     background:${meta.color}1a;
                     border:1px solid ${meta.color}33;
                     margin-left:6px;vertical-align:1px;cursor:${opts.url?'pointer':'help'};"
              title="${esc(tip)}" ${attrs}>${esc(inner)}</${tag}>`;
  }

  /** Auto-decorate every element on the page that carries
   *  [data-lineage-source]. Idempotent (sets data-lineage-bound=1). */
  function refresh(root) {
    (root || document).querySelectorAll('[data-lineage-source]:not([data-lineage-bound])')
      .forEach(el => {
        try {
          const html = render({
            source: el.getAttribute('data-lineage-source'),
            as_of:  el.getAttribute('data-lineage-as-of'),
            url:    el.getAttribute('data-lineage-url'),
            note:   el.getAttribute('data-lineage-note'),
          });
          el.insertAdjacentHTML('beforeend', html);
          el.setAttribute('data-lineage-bound', '1');
        } catch (e) { /* swallow per-element */ }
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => refresh());
  } else {
    refresh();
  }

  // Re-run on DOM mutations so dynamically-rendered cards pick up chips too.
  try {
    new MutationObserver(() => refresh()).observe(
      document.documentElement, { childList: true, subtree: true }
    );
  } catch (_) {}

  window.LineageChip = { render, refresh, SOURCES };
})();
