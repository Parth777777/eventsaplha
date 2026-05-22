/* quant-demo.js — Friendliness layer for the Quant Playground.
 *
 * Provides:
 *   - Demo data so every "Pull" / "Run" button works even when the live API is down
 *   - Safe-fetch wrappers that fall back to demo data automatically + show friendly toast
 *   - Confetti-free fun helpers: random example picker, gentle empty-state copy
 *   - Soft toast (no scary alert() calls)
 *
 * Attached to window.QPDemo.
 */
(function (root) {
  'use strict';

  // ── DEMO DATA — realistic NSE-shaped values so the math still produces sensible outputs ──
  var DEMO_TICKER = {
    NIFTY:     { spot: 24567.30, atm_iv: 0.142, lotsize: 25,  sector: 'Index' },
    BANKNIFTY: { spot: 52384.10, atm_iv: 0.168, lotsize: 15,  sector: 'Index' },
    RELIANCE:  { spot:  2847.55, atm_iv: 0.218, lotsize: 250, sector: 'Energy' },
    HDFCBANK:  { spot:  1645.30, atm_iv: 0.192, lotsize: 550, sector: 'Banking' },
    TCS:       { spot:  4123.80, atm_iv: 0.205, lotsize: 175, sector: 'IT' },
    INFY:      { spot:  1502.20, atm_iv: 0.211, lotsize: 400, sector: 'IT' },
    WIPRO:     { spot:   542.40, atm_iv: 0.236, lotsize: 1500,sector: 'IT' },
    HCLTECH:   { spot:  1841.10, atm_iv: 0.218, lotsize: 350, sector: 'IT' },
    TECHM:     { spot:  1656.70, atm_iv: 0.244, lotsize: 600, sector: 'IT' },
    LT:        { spot:  3784.20, atm_iv: 0.225, lotsize: 250, sector: 'Industrial' },
    NIFTYBEES: { spot:   267.20, atm_iv: 0.142, lotsize: 1,   sector: 'ETF' },
    GOLDBEES:  { spot:    65.40, atm_iv: 0.118, lotsize: 1,   sector: 'ETF' },
  };

  // Generate realistic synthetic OHLC for any ticker (or use a known anchor)
  function syntheticOHLC(ticker, days, seed) {
    var meta = DEMO_TICKER[ticker.toUpperCase()] || { spot: 1000, atm_iv: 0.20 };
    var rngState = (seed || hashString(ticker)) >>> 0;
    function rand() { rngState = (rngState * 1664525 + 1013904223) >>> 0; return rngState / 4294967296; }
    function norm() {
      var u1 = rand(); var u2 = rand();
      if (u1 < 1e-12) u1 = 1e-12;
      return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
    }
    var dailySigma = meta.atm_iv / Math.sqrt(252);
    var dailyMu = 0.10 / 252;
    var s = meta.spot * Math.exp(-dailyMu * days); // start price = end price walked back
    var out = []; var today = new Date();
    for (var i = days; i >= 0; i--) {
      var date = new Date(today.getTime() - i * 24 * 3600 * 1000);
      var iso = date.toISOString().slice(0, 10);
      out.push({ date: iso, close: +s.toFixed(2), open: +s.toFixed(2), high: +s.toFixed(2), low: +s.toFixed(2), volume: Math.round(rand() * 1e6) });
      s = s * Math.exp(dailyMu + dailySigma * norm());
    }
    return out;
  }
  function hashString(s) {
    var h = 5381; for (var i = 0; i < s.length; i++) h = (h * 33) ^ s.charCodeAt(i);
    return h >>> 0;
  }

  // Demo backtest payload (matches /api/v1/backtest shape)
  function syntheticBacktest(ticker, eventType) {
    var seed = hashString(ticker + '_' + eventType);
    var rngState = seed >>> 0;
    function rand() { rngState = (rngState * 1664525 + 1013904223) >>> 0; return rngState / 4294967296; }
    var n = 14 + Math.floor(rand() * 26);
    var gaps = [], drifts = [];
    var sign = (eventType.indexOf('beat') >= 0 || eventType.indexOf('win') >= 0) ? 1 : (eventType.indexOf('miss') >= 0 || eventType.indexOf('cut') >= 0 ? -1 : 0);
    for (var i = 0; i < n; i++) {
      gaps.push(+((sign * 1.5 + (rand() - 0.5) * 4)).toFixed(2));
      drifts.push(+((sign * 2.5 + (rand() - 0.5) * 6)).toFixed(2));
    }
    var avgGap = gaps.reduce(function (a, b) { return a + b; }, 0) / n;
    var avgDrift = drifts.reduce(function (a, b) { return a + b; }, 0) / n;
    var wins = drifts.filter(function (d) { return d > 0; }).length;
    var fades = gaps.filter(function (g, idx) { return Math.sign(g) !== Math.sign(drifts[idx]); }).length;
    return {
      n: n,
      gaps: gaps, drifts_5d: drifts,
      avg_gap_up_pct: +avgGap.toFixed(2),
      avg_5day_drift_pct: +avgDrift.toFixed(2),
      win_rate_pct: +((wins / n) * 100).toFixed(1),
      fade_probability_pct: +((fades / n) * 100).toFixed(1),
    };
  }

  // ── Safe fetch with friendly demo fallback ──
  // safeFetch('/api/...', { demo: <demoFn>, label: 'Reliance chain' })
  //   → returns parsed JSON if live; else calls demo() and returns its result with __demo=true
  function safeFetch(url, opts) {
    opts = opts || {};
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (j) {
        if (j && (j.success !== false) && (j.data || Array.isArray(j) || j.last_price || j.spot)) {
          return j;
        }
        // Fallback
        var demoData = opts.demo ? opts.demo() : null;
        if (demoData) {
          if (opts.label) toast('Live data unavailable — using demo "' + opts.label + '". Math still works the same.', 'info');
          demoData.__demo = true;
          return demoData;
        }
        toast('Live data unavailable. Try a different ticker or load an example below.', 'warn');
        return null;
      });
  }

  // ── Soft toast (replaces all alert() calls) ──
  var toastHost = null;
  function ensureHost() {
    if (toastHost) return toastHost;
    toastHost = document.createElement('div');
    toastHost.id = 'qpToastHost';
    toastHost.style.cssText = 'position:fixed;bottom:88px;right:22px;display:flex;flex-direction:column;gap:8px;z-index:200;pointer-events:none;max-width:340px;';
    document.body.appendChild(toastHost);
    return toastHost;
  }
  function toast(msg, kind) {
    kind = kind || 'info';
    var bg = kind === 'warn' ? 'rgba(230,184,74,0.95)'
           : kind === 'success' ? 'rgba(125,240,200,0.95)'
           : 'rgba(20,25,35,0.96)';
    var border = kind === 'warn' ? 'rgba(230,184,74,1)'
               : kind === 'success' ? 'rgba(125,240,200,1)'
               : 'rgba(173,198,255,0.40)';
    var fg = kind === 'info' ? '#dde3ef' : '#07090d';
    var el = document.createElement('div');
    el.style.cssText = 'pointer-events:auto;background:' + bg + ';border:1px solid ' + border + ';color:' + fg + ';padding:11px 14px;border-radius:2px;font:600 12px/1.45 DM Sans;box-shadow:0 8px 24px rgba(0,0,0,0.5);transform:translateX(20px);opacity:0;transition:all 220ms ease;';
    el.textContent = msg;
    ensureHost().appendChild(el);
    requestAnimationFrame(function () { el.style.transform = 'translateX(0)'; el.style.opacity = '1'; });
    setTimeout(function () {
      el.style.opacity = '0'; el.style.transform = 'translateX(20px)';
      setTimeout(function () { el.remove(); }, 240);
    }, 4200);
  }

  // ── Random example picker ──
  // Call from a tab's init: QPDemo.attachRandomExample(rootEl, examplesObj, loadFn)
  function attachRandomExample(rootEl, examples, loadFn) {
    var keys = Object.keys(examples);
    if (!keys.length) return;
    var btn = rootEl.querySelector('[data-random]');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var pick = keys[Math.floor(Math.random() * keys.length)];
      loadFn(pick, examples[pick]);
      toast('Loaded: ' + examples[pick].label, 'success');
    });
  }

  // ── Greet: friendly inline hint on first tab visit ──
  function maybeGreet(tab) {
    try {
      var k = 'qp:greet:' + tab;
      if (sessionStorage.getItem(k)) return;
      sessionStorage.setItem(k, '1');
      var greetings = {
        greeks: 'New here? Hit "▸ Load:" on one of the examples below to see the Greeks come alive.',
        payoff: 'Try the iron condor example — it\'s the most common premium-selling strategy in India.',
        montecarlo: 'Click "▶ Simulate" — the paths animate as they generate. Try "slow" speed first.',
        sizing: 'Most retail traders over-bet. Compare half-Kelly vs 2% fixed below — notice the drawdown profile.',
        portfolio: 'Click "Load from Watchlist" or pick a preset example. Heatmap warns when positions are too correlated.',
        backtest: 'This isn\'t a strategy backtester — it tells you how a ticker has historically reacted to an event type.',
        glossary: 'Search any term, or open a tool tab and hover the "?" next to any input label.',
      };
      var msg = greetings[tab];
      if (msg) toast(msg, 'info');
    } catch (_) {}
  }

  root.QPDemo = {
    DEMO_TICKER: DEMO_TICKER,
    syntheticOHLC: syntheticOHLC,
    syntheticBacktest: syntheticBacktest,
    safeFetch: safeFetch,
    toast: toast,
    attachRandomExample: attachRandomExample,
    maybeGreet: maybeGreet,
  };
  // Replace native alert sitewide for the playground page (defensive — many existing modules call alert)
  if (typeof window !== 'undefined') {
    var nativeAlert = window.alert;
    window.alert = function (msg) {
      try { toast(String(msg), 'warn'); }
      catch (_) { nativeAlert.call(window, msg); }
    };
  }
  // Wire greet on tab change
  document.addEventListener('qp:tab', function (e) { setTimeout(function () { maybeGreet(e.detail.tab); }, 200); });
})(typeof window !== 'undefined' ? window : globalThis);
