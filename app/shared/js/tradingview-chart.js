/* tradingview-chart.js — TradingView Advanced Chart widget wrapper.
 *
 * Free TradingView widget (no key) with NSE/BSE symbol mapping, dark/light
 * theme detection, sensible defaults (candles + Volume + RSI + 50/200 MA),
 * and a graceful fallback to AdvancedChart canvas when tv.js can't load
 * (adblocker, offline, region block).
 *
 * Usage:
 *   TradingViewChart.mount('#host', 'RELIANCE');
 *   TradingViewChart.mount('#host', 'TATAMOTORS', { interval: 'D', height: 540 });
 *   TradingViewChart.mountIndex('#host', 'NIFTY');
 *   TradingViewChart.resolveSymbol('RELIANCE');  // -> "NSE:RELIANCE"
 *
 * The script tag (tv.js) is loaded once, lazily. Subsequent mounts reuse it.
 */
(function () {
  if (window.TradingViewChart) return;

  const TV_SRC = 'https://s3.tradingview.com/tv.js';

  // Index ticker → TradingView symbol. Indian indices use NSE / BSE prefixes.
  const INDEX_MAP = {
    NIFTY:       'NSE:NIFTY',
    NIFTY50:     'NSE:NIFTY',
    BANKNIFTY:   'NSE:BANKNIFTY',
    FINNIFTY:    'NSE:CNXFINANCE',
    MIDCPNIFTY:  'NSE:NIFTY_MID_SELECT',
    SENSEX:      'BSE:SENSEX',
    BANKEX:      'BSE:BANKEX',
    INDIAVIX:    'NSE:INDIAVIX',
    VIX:         'NSE:INDIAVIX',
  };

  // Commodities/currencies that come through this widget — TradingView exposes
  // MCX-tracked spot under their own provider prefixes.
  const COMMODITY_MAP = {
    GOLD:    'TVC:GOLD',
    SILVER:  'TVC:SILVER',
    BRENT:   'TVC:UKOIL',
    CRUDE:   'TVC:USOIL',
    USDINR:  'FX_IDC:USDINR',
    EURINR:  'FX_IDC:EURINR',
  };

  // ---------- tv.js loader (single-flight) ----------
  let _tvLoading = null;
  function loadTV() {
    if (window.TradingView && typeof window.TradingView.widget === 'function') {
      return Promise.resolve(true);
    }
    if (_tvLoading) return _tvLoading;
    _tvLoading = new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = TV_SRC;
      s.async = true;
      s.onload  = () => resolve(true);
      s.onerror = () => resolve(false);
      document.head.appendChild(s);
      // Hard timeout — adblockers may stall the request silently
      setTimeout(() => resolve(!!(window.TradingView && window.TradingView.widget)), 8000);
    });
    return _tvLoading;
  }

  function currentTheme() {
    try {
      const t = document.documentElement.getAttribute('data-theme');
      if (t === 'light') return 'light';
    } catch (_) {}
    return 'dark';
  }

  // resolveSymbol: NEVER returns empty. An empty `symbol` field gets silently
  // replaced with AAPL by TradingView's widget — that was the "AAPL opening
  // on Reliance" bug. Anything falsy now falls back to NSE:NIFTY so the
  // viewer always sees an Indian symbol.
  function resolveSymbol(ticker, opts) {
    const tk = String(ticker || '').toUpperCase().trim();
    if (!tk) {
      // eslint-disable-next-line no-console
      console.warn('[TradingViewChart] empty ticker passed — falling back to NSE:NIFTY');
      return 'NSE:NIFTY';
    }
    if (opts && opts.exchange) return `${opts.exchange.toUpperCase()}:${tk}`;
    if (tk.indexOf(':') !== -1) return tk;          // already prefixed
    if (INDEX_MAP[tk]) return INDEX_MAP[tk];
    if (COMMODITY_MAP[tk]) return COMMODITY_MAP[tk];
    return `NSE:${tk}`;                             // default to NSE for equities
  }

  function ensureStyles() {
    if (document.getElementById('tvchart-css')) return;
    const s = document.createElement('style');
    s.id = 'tvchart-css';
    s.textContent = `
      .tvchart-wrap { position:relative; display:flex; flex-direction:column; gap:6px;
        width:100%; height:100%; min-height:460px; }
      .tvchart-host { flex:1; min-height:0; position:relative;
        background:var(--surface-1, #0d1118);
        border:1px solid var(--border-subtle, rgba(255,255,255,0.06));
        border-radius:14px; overflow:hidden; }
      .tvchart-host iframe { display:block; width:100% !important; height:100% !important; border:0; }
      .tvchart-status { position:absolute; inset:0; display:flex; align-items:center;
        justify-content:center; color:var(--text-tertiary, #6a7484);
        font:500 13px 'Plus Jakarta Sans',sans-serif; pointer-events:none; gap:8px; }
      .tvchart-status .dot { width:8px; height:8px; border-radius:50%;
        background:var(--accent, #8eb4e0); animation:tvc-pulse 1.2s infinite ease-in-out; }
      @keyframes tvc-pulse { 0%,100% { opacity:0.35; } 50% { opacity:1; } }
      .tvchart-credit { font:600 10px 'Plus Jakarta Sans',sans-serif;
        color:var(--text-tertiary, #6a7484);
        text-align:right; padding-right:8px; opacity:0.72; }
      .tvchart-credit a { color:inherit; text-decoration:none; }
      .tvchart-credit a:hover { color:var(--accent, #8eb4e0); text-decoration:underline; }
      .tvchart-wrap.compact { min-height:240px; }
      .tvchart-wrap.compact .tvchart-host { min-height:240px; border-radius:8px; }

      /* Fullscreen TradingView terminal — overlays the page when the user
         opts into the heavy-weight technical charts. Default UI keeps the
         lightweight in-house canvas chart; terminal is invoked on demand. */
      .tvc-terminal-overlay {
        position:fixed; inset:0; z-index:9999;
        background:var(--surface-0, #080c12);
        display:flex; flex-direction:column;
        animation:tvc-term-in 180ms ease-out;
      }
      @keyframes tvc-term-in { from { opacity:0; } to { opacity:1; } }
      .tvc-terminal-bar {
        display:flex; align-items:center; justify-content:space-between;
        padding:14px 22px; gap:14px;
        background:var(--surface-1, #0d1118);
        border-bottom:1px solid var(--border-subtle, rgba(255,255,255,0.06));
      }
      .tvc-terminal-title { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; min-width:0; }
      .tvc-terminal-eyebrow {
        font:700 10px 'Plus Jakarta Sans',sans-serif;
        letter-spacing:0.14em; text-transform:uppercase;
        color:var(--text-secondary, #8a94a8);
      }
      .tvc-terminal-ticker {
        font:800 18px 'Geist Mono',monospace; letter-spacing:0.02em;
        color:var(--text-primary, #dde3ef);
      }
      .tvc-terminal-symbol {
        font:600 11px 'Geist Mono',monospace;
        color:var(--text-tertiary, #6a7484);
        background:rgba(141,148,168,0.10);
        padding:3px 8px; border-radius:5px;
      }
      .tvc-terminal-close {
        background:transparent;
        border:1px solid var(--border-subtle, rgba(255,255,255,0.10));
        color:var(--text-secondary, #8a94a8);
        padding:7px 12px; border-radius:8px; cursor:pointer;
        display:inline-flex; align-items:center; gap:6px;
        font:700 11px 'Plus Jakarta Sans',sans-serif; letter-spacing:0.06em;
        text-transform:uppercase;
        transition:all 120ms ease;
      }
      .tvc-terminal-close:hover {
        color:var(--text-primary, #fff);
        border-color:var(--text-secondary, #8a94a8);
        background:rgba(255,255,255,0.04);
      }
      .tvc-terminal-close .material-symbols-outlined { font-size:18px; }
      .tvc-terminal-body {
        flex:1; padding:14px 22px 22px;
        display:flex; min-height:0; min-width:0;
      }
      .tvc-terminal-body > .tvchart-wrap { flex:1; min-height:0; }
      .tvc-terminal-body > .tvchart-wrap > .tvchart-host { min-height:0; height:100%; }
    `;
    document.head.appendChild(s);
  }

  // ---------- Mount ----------
  // Keep track of active widgets so theme changes can re-mount them in place.
  const _instances = new WeakMap();

  async function mount(container, ticker, opts) {
    ensureStyles();
    opts = opts || {};
    if (typeof container === 'string') container = document.querySelector(container);
    if (!container) return null;
    container.innerHTML = '';

    const compact = !!opts.compact;
    const wrap = document.createElement('div');
    wrap.className = 'tvchart-wrap' + (compact ? ' compact' : '');
    const host = document.createElement('div');
    host.className = 'tvchart-host';
    const hostId = 'tvc-' + Math.random().toString(36).slice(2, 10);
    host.id = hostId;
    if (opts.height) {
      const h = typeof opts.height === 'number' ? opts.height + 'px' : opts.height;
      host.style.height = h;
      host.style.minHeight = h;
      wrap.style.minHeight = h;
    }
    const status = document.createElement('div');
    status.className = 'tvchart-status';
    status.innerHTML = '<span class="dot"></span>Loading TradingView chart…';
    host.appendChild(status);
    wrap.appendChild(host);
    if (!opts.hideCredit) {
      const credit = document.createElement('div');
      credit.className = 'tvchart-credit';
      credit.innerHTML = 'Chart by <a href="https://www.tradingview.com/" target="_blank" rel="noopener">TradingView</a>';
      wrap.appendChild(credit);
    }
    container.appendChild(wrap);

    // Resolve symbol once, up-front. Crucial: we use a direct iframe embed
    // instead of `new TradingView.widget()` — the script-based widget had a
    // habit of silently falling back to AAPL when its internal symbol
    // resolver disliked something. With a hand-built iframe URL, the symbol
    // travels as a query param and TradingView's embed page reads it
    // verbatim — no script middleware to drop it.
    const symbol = resolveSymbol(ticker, opts);
    const theme = opts.theme || currentTheme();
    const interval = opts.interval || 'D';
    const style = String(opts.style != null ? opts.style : 1);
    const studies = opts.studies || (compact ? [] : [
      'MASimple@tv-basicstudies',
      'Volume@tv-basicstudies',
      'RSI@tv-basicstudies',
    ]);
    const params = new URLSearchParams({
      symbol:               symbol,
      interval:             interval,
      theme:                theme,
      style:                style,
      locale:               'en',
      timezone:             'Asia/Kolkata',
      toolbarbg:            'rgba(0,0,0,0)',
      studies:              JSON.stringify(studies),
      hideideas:            '1',
      withdateranges:       (opts.withDateRanges !== false && !compact) ? '1' : '0',
      'allow_symbol_change': (opts.allowSymbolChange !== false) ? '1' : '0',
      save_image:           '0',
      hide_top_toolbar:     (opts.hideTopToolbar || compact) ? '1' : '0',
      hide_side_toolbar:    (opts.hideSideToolbar || compact) ? '1' : '0',
      details:              '0',
      autosize:             '1',
      enable_publishing:    '0',
    });
    const iframeUrl = `https://s.tradingview.com/widgetembed/?${params.toString()}`;

    // eslint-disable-next-line no-console
    console.log('[TradingViewChart] mount', { ticker, symbol, theme, interval });

    status.remove();
    const iframe = document.createElement('iframe');
    iframe.src             = iframeUrl;
    iframe.allowTransparency = 'true';
    iframe.scrolling       = 'no';
    iframe.frameBorder     = '0';
    iframe.allow           = 'fullscreen';
    iframe.title           = `${ticker} TradingView chart`;
    iframe.style.cssText   = 'display:block;width:100%;height:100%;border:0;';
    host.appendChild(iframe);

    // If TradingView is unreachable (adblocker, offline), the iframe stays
    // blank. Detect that and fall back to AdvancedChart so the slot is never
    // empty. The load handler fires only on success; we use a timer guard.
    let didLoad = false;
    iframe.addEventListener('load', () => { didLoad = true; });
    setTimeout(() => {
      if (didLoad) return;
      if (!window.AdvancedChart || typeof window.AdvancedChart.mount !== 'function') return;
      // eslint-disable-next-line no-console
      console.warn('[TradingViewChart] iframe never fired load — falling back to AdvancedChart');
      try { iframe.remove(); } catch (_) {}
      window.AdvancedChart.mount(
        host,
        String(ticker).toUpperCase().replace(/^.*:/, ''),
        { period: opts.period || '1y', compact: compact }
      );
    }, 6000);

    _instances.set(container, { iframe, ticker, opts });

    // Re-mount widget on theme toggle so candle/grid colors flip cleanly.
    // We observe the <html> data-theme attribute (set by theme-toggle.js).
    let themeT;
    try {
      const obs = new MutationObserver((muts) => {
        const themeChanged = muts.some(m => m.attributeName === 'data-theme');
        if (!themeChanged) return;
        clearTimeout(themeT);
        themeT = setTimeout(() => {
          const inst = _instances.get(container);
          if (inst) mount(container, inst.ticker, inst.opts);
        }, 80);
      });
      obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      _instances.get(container)._themeObs = obs;
    } catch (_) { /* MutationObserver unsupported — accept stale theme */ }

    return {
      symbol,
      ticker,
      iframe,
      remount: () => mount(container, ticker, opts),
      setSymbol: (newTicker, newOpts) => mount(container, newTicker, Object.assign({}, opts, newOpts || {})),
    };
  }

  function mountIndex(container, index, opts) {
    const sym = INDEX_MAP[(index || '').toUpperCase()] || index;
    return mount(container, sym, Object.assign({ allowSymbolChange: false }, opts || {}));
  }

  function mountCommodity(container, sym, opts) {
    const s = COMMODITY_MAP[(sym || '').toUpperCase()] || sym;
    return mount(container, s, Object.assign({ allowSymbolChange: false }, opts || {}));
  }

  // ---------- Fullscreen terminal ----------
  // Opens TradingView in a dedicated full-window overlay. The page's primary
  // chart stays on the in-house canvas renderer; this is the heavy-weight
  // technical-analysis surface users opt into.
  function openTerminal(ticker, opts) {
    ensureStyles();
    opts = opts || {};
    closeTerminal();  // singleton

    const tk = String(ticker || '').toUpperCase();
    const symbol = resolveSymbol(tk, opts);

    const overlay = document.createElement('div');
    overlay.id = 'tvc-terminal';
    overlay.className = 'tvc-terminal-overlay';
    overlay.innerHTML = `
      <div class="tvc-terminal-bar">
        <div class="tvc-terminal-title">
          <span class="tvc-terminal-eyebrow">Technical Terminal · TradingView</span>
          <span class="tvc-terminal-ticker">${tk}</span>
          <span class="tvc-terminal-symbol">${symbol}</span>
        </div>
        <button type="button" class="tvc-terminal-close" data-tvc-close>
          <span class="material-symbols-outlined">close</span>
          <span>Close</span>
        </button>
      </div>
      <div class="tvc-terminal-body" id="tvc-terminal-body"></div>
    `;
    document.body.appendChild(overlay);

    // Lock background scroll
    overlay.dataset.prevOverflow = document.body.style.overflow || '';
    document.body.style.overflow = 'hidden';

    // ESC closes
    const onKey = (e) => { if (e.key === 'Escape') closeTerminal(); };
    document.addEventListener('keydown', onKey);
    overlay.__onKey = onKey;

    overlay.querySelector('[data-tvc-close]').addEventListener('click', closeTerminal);

    // Mount the widget inside the body — fills via flex:1
    mount('#tvc-terminal-body', tk, Object.assign({
      interval: 'D',
      studies: [
        'MASimple@tv-basicstudies',
        'Volume@tv-basicstudies',
        'RSI@tv-basicstudies',
        'MACD@tv-basicstudies',
      ],
      withDateRanges: true,
      allowSymbolChange: true,
      hideCredit: true,
    }, opts));

    return overlay;
  }

  function closeTerminal() {
    const el = document.getElementById('tvc-terminal');
    if (!el) return;
    if (el.__onKey) document.removeEventListener('keydown', el.__onKey);
    document.body.style.overflow = el.dataset.prevOverflow || '';
    el.remove();
  }

  window.TradingViewChart = {
    mount, mountIndex, mountCommodity, resolveSymbol, loadTV,
    openTerminal, closeTerminal,
  };
})();
