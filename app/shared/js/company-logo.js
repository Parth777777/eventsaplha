/**
 * Tickwave - Company Logo helper.
 * Resolves a ticker -> img tag via the /api/logo/<ticker> backend route which
 * proxies parqet.com (NS/BO) with disk caching + SVG initials fallback.
 *
 * Usage:
 *   CompanyLogo.img('RELIANCE')              // 28px round img
 *   CompanyLogo.img('TCS', { size: 48 })     // larger
 *   CompanyLogo.img('TCS', { round: false }) // square
 *   CompanyLogo.url('TCS')                   // just the URL
 *
 * The backend always returns a valid image (real PNG or SVG initials),
 * so there's no need for an onerror handler in the markup.
 */
(function () {
  function url(ticker) {
    if (!ticker) return null;
    const t = String(ticker).trim().toUpperCase();
    if (!t || t === '-' || t === 'NA' || t === 'NULL') return null;
    // Strip ".NS" / ".BO" suffixes if the caller passed them
    return '/api/logo/' + encodeURIComponent(t.replace(/\.(NS|BO)$/i, ''));
  }

  function escape(s) {
    return s == null ? '' : String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /**
   * @param {string} ticker
   * @param {object} [opts]
   * @param {number} [opts.size=28]    pixel size (square)
   * @param {boolean} [opts.round=true] round corners (circle)
   * @param {string} [opts.alt]        alt text (defaults to ticker)
   * @param {string} [opts.cls]        extra CSS classes
   * @returns {string} HTML string
   */
  function img(ticker, opts) {
    opts = opts || {};
    const u = url(ticker);
    if (!u) return '';
    const size = opts.size || 14;                       // small + inline by default
    const round = opts.round !== false;
    const alt = escape(opts.alt || ticker);
    const cls = opts.cls ? ' ' + escape(opts.cls) : '';
    const inline = opts.inline !== false;               // auto-spaced for inline use
    // Subtle chip styling — soft shadow ring instead of hard border, so the
    // logo reads as a brand mark on a dark surface without painting a harsh
    // white square. Real brand PNGs sit on the white interior; the shadow ring
    // gives it depth without competing with the foreground.
    const style = [
      'width:' + size + 'px',
      'height:' + size + 'px',
      'object-fit:contain',
      'background:#fff',
      'flex-shrink:0',
      'display:inline-block',
      'vertical-align:middle',
      round ? 'border-radius:' + (size / 2) + 'px' : 'border-radius:3px',
      'box-shadow:0 0 0 1px rgba(255,255,255,0.08), 0 1px 2px rgba(0,0,0,0.45)',
      inline ? 'margin-right:5px' : '',
    ].filter(Boolean).join(';');
    return '<img class="tw-logo' + cls + '" src="' + u + '" alt="' + alt
         + '" loading="lazy" decoding="async" style="' + style + '" />';
  }

  /**
   * Build a "logo + name" inline group. Convenient for tables/cards.
   *   CompanyLogo.namePill('RELIANCE', 'Reliance Industries', { size: 24 })
   */
  function namePill(ticker, name, opts) {
    opts = opts || {};
    const t = escape(ticker || '');
    const n = escape(name || ticker || '');
    const gap = opts.gap || 8;
    return '<span class="tw-logo-pill" style="display:inline-flex;align-items:center;gap:'
         + gap + 'px;min-width:0;">'
         + img(ticker, opts)
         + '<span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
         + '<span style="font-weight:600;">' + n + '</span>'
         + (ticker && ticker !== name ? '<span style="color:var(--rf-text-dim,#8a94a8);font-size:11px;margin-left:6px;font-family:Geist Mono,JetBrains Mono,monospace;">' + t + '</span>' : '')
         + '</span></span>';
  }

  // ── Auto-decorate ───────────────────────────────────────────────────────
  // Walks the DOM and prepends a small logo to every element marked as a
  // ticker reference: [data-ticker], [data-stock-ticker], .ec-ticker, or
  // .stock-pop-trigger. Idempotent (skips already-decorated elements) and
  // respects opt-out via data-no-logo / .tw-no-logo. Runs on DOM ready and
  // re-runs on dynamic insertions via a MutationObserver — so SPA pages and
  // AJAX-rendered lists get logos without each renderer calling us.
  const AUTO_SELECTOR = '[data-ticker], [data-stock-ticker], .ec-ticker, .stock-pop-trigger';

  function _decorateOne(el) {
    if (!el || el.nodeType !== 1) return;
    if (el.dataset && el.dataset.twLogoApplied === '1') return;
    if (el.hasAttribute && (el.hasAttribute('data-no-logo') || el.classList.contains('tw-no-logo'))) return;
    // Honour an opt-out anywhere up the tree, so a container can shut off
    // logos for everything it owns (e.g. dense tables).
    if (el.closest && el.closest('[data-no-logo],.tw-no-logo') && el.closest('[data-no-logo],.tw-no-logo') !== el) return;
    if (el.querySelector && el.querySelector(':scope > .tw-logo')) {
      el.dataset.twLogoApplied = '1';
      return;
    }
    let tk = (el.getAttribute && (el.getAttribute('data-ticker') || el.getAttribute('data-stock-ticker'))) || '';
    if (!tk && el.classList && (el.classList.contains('ec-ticker') || el.classList.contains('stock-pop-trigger'))) {
      tk = (el.textContent || '').trim();
    }
    tk = String(tk || '').trim().toUpperCase();
    if (!tk || tk === '—' || tk === 'NONE' || tk === '-' || tk === 'NA') return;
    // Pick a default size that reads well at the host's font scale: ticker
    // chips deserve a slightly larger mark; everything else gets a compact 16.
    const explicit = parseInt((el.dataset && el.dataset.logoSize) || '', 10);
    const isChip = el.classList && (el.classList.contains('ec-ticker') || el.classList.contains('stock-pop-trigger'));
    const size = explicit || (isChip ? 18 : 16);
    // No parent restyling — the logo carries its own margin-right and
    // vertical-align:middle, so it slots into inline flow without forcing
    // the host element into flex layout (which broke table-cell alignment
    // in the earlier pass).
    const html = img(tk, { size: size, round: true, cls: 'tw-auto', inline: true });
    if (!html) return;
    el.insertAdjacentHTML('afterbegin', html);
    el.dataset.twLogoApplied = '1';
  }

  function autoDecorate(root) {
    root = root || document;
    if (!root.querySelectorAll) return;
    root.querySelectorAll(AUTO_SELECTOR).forEach(_decorateOne);
  }

  function _initAuto() {
    autoDecorate(document);
    if (typeof MutationObserver === 'undefined') return;
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        m.addedNodes && m.addedNodes.forEach((n) => {
          if (!n || n.nodeType !== 1) return;
          if (n.matches && n.matches(AUTO_SELECTOR)) _decorateOne(n);
          if (n.querySelectorAll) autoDecorate(n);
        });
      }
    });
    mo.observe(document.body || document.documentElement, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initAuto);
  } else {
    _initAuto();
  }

  window.CompanyLogo = { url: url, img: img, namePill: namePill, autoDecorate: autoDecorate };
})();
