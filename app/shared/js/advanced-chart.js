/* advanced-chart.js — institutional-style chart component.
 *
 * Renders price line + 50 DMA + 200 DMA overlays + volume bars at bottom +
 * period tabs (1M / 6M / 1Y / 3Y / 5Y / 10Y / Max). Inspired by Screener.in's
 * stock chart layout — clean, monochrome, no neon.
 *
 * Usage:
 *   AdvancedChart.mount(containerEl, ticker, { period: '1y' });
 *   AdvancedChart.mount('#chartHost', 'RELIANCE');
 *
 * The container must have a stable size — the chart fills it.
 */
(function () {
  if (window.AdvancedChart) return;

  const PERIODS = [
    { id: '1mo', label: '1M' },
    { id: '6mo', label: '6M' },
    { id: '1y',  label: '1Y' },
    { id: '3y',  label: '3Y' },
    { id: '5y',  label: '5Y' },
    { id: '10y', label: '10Y' },
    { id: 'max', label: 'Max' },
  ];

  const DEFAULT_PERIOD = '1y';

  /* Brand colors stay constant across themes (price line is brand identity).
     Chrome colors (axis, grid, text, bg) read tokens — keeps the chart
     legible in light mode instead of showing as a black rectangle. */
  const BRAND = {
    price:     '#7c8cf8',  // muted indigo
    priceFill: 'rgba(124,140,248,0.10)',
    dma50:     '#f5a524',  // amber
    dma200:    '#a6adbb',  // soft slate
    volume:    'rgba(124,140,248,0.32)',
  };
  function getColors() {
    const T = (window.ThemeTokens && window.ThemeTokens.snapshot()) || {};
    return {
      price:     BRAND.price,
      priceFill: BRAND.priceFill,
      dma50:     BRAND.dma50,
      dma200:    BRAND.dma200,
      volume:    BRAND.volume,
      /* Chrome — theme-aware */
      axis:    T.borderStrong || '#3a4150',
      grid:    T.borderSubtle || 'rgba(141,148,168,0.10)',
      text:    T.textSecondary || '#8a94a8',
      textHi:  T.textPrimary || '#dde3ef',
      bg:      T.surface1 || '#0d1117',
      surface: 'transparent',
    };
  }
  /* `COLORS.X` is the historical access pattern across this file. The Proxy
     keeps that ergonomic syntax but resolves every read against the CURRENT
     theme — so a theme toggle picks up new chrome colors on the next render
     pass without touching any callsite. */
  const COLORS = new Proxy({}, {
    get: (_, key) => getColors()[key],
    has: (_, key) => key in getColors(),
    ownKeys: () => Object.keys(getColors()),
    getOwnPropertyDescriptor: (_, key) => ({
      enumerable: true, configurable: true, value: getColors()[key],
    }),
  });

  // Site-wide font stacks — mirror the fonts loaded site-wide (Plus Jakarta
  // Sans for UI, DM Sans for body, Geist Mono for numerics) so canvas-drawn
  // axis labels match the rest of the page typography. Inter was the legacy
  // value but the site never actually loads it, so labels fell back to
  // system-ui — labels now use the same font the surrounding UI uses.
  const FONT_UI   = `'Plus Jakarta Sans', 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif`;
  const FONT_MONO = `'Geist Mono', 'IBM Plex Mono', 'SF Mono', Menlo, Consolas, monospace`;

  function sma(values, window) {
    if (!Array.isArray(values) || values.length < window) return new Array(values.length).fill(null);
    const out = new Array(values.length).fill(null);
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= window) sum -= values[i - window];
      if (i >= window - 1) out[i] = sum / window;
    }
    return out;
  }

  function fmtPrice(v) {
    if (v == null || !isFinite(v)) return '—';
    return Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }
  function fmtVol(v) {
    if (!v) return '—';
    if (v >= 1e7) return (v / 1e7).toFixed(2) + 'Cr';
    if (v >= 1e5) return (v / 1e5).toFixed(2) + 'L';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return String(v);
  }
  function fmtDate(s) {
    if (!s) return '';
    const d = new Date(s);
    if (isNaN(d)) return s;
    return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' });
  }
  function shortDate(d) {
    if (!d) return '';
    const dt = new Date(d);
    if (isNaN(dt)) return d;
    return dt.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  }

  function ensureStyles() {
    if (document.getElementById('adv-chart-css')) return;
    const s = document.createElement('style');
    s.id = 'adv-chart-css';
    s.textContent = `
      /* Full-mode wrapper fills its parent so the chart stretches to the
         host's height (set by the page). Falls back to min-height:480 when
         the parent has no explicit height — never collapses. */
      .advc-wrap { background:var(--surface-1); border:1px solid var(--border-subtle);
        border-radius:18px; padding:16px 18px; display:flex; flex-direction:column; gap:12px;
        box-shadow: var(--shadow-sm);
        height:100%; min-height:480px; }
      .advc-bar { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
      .advc-bar-l, .advc-bar-r { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
      .advc-bar-r { margin-left:auto; }
      .advc-btn { font:700 11px 'Plus Jakarta Sans',sans-serif; letter-spacing:0.04em;
        padding:6px 12px; border-radius:10px; background:transparent; color:var(--text-secondary);
        border:1px solid transparent; cursor:pointer; transition:all 100ms; }
      .advc-btn:hover { color:var(--text-primary); background:var(--surface-3); }
      .advc-btn.on { color:var(--accent); background:var(--accent-dim);
        border-color:var(--accent); }
      /* Canvas wrap grows to fill the remaining space after the toolbar +
         legend rows. min-height:340 covers the legacy 380 baseline so a
         host with no explicit height still gets a usable chart. */
      .advc-canvas-wrap { position:relative; width:100%; flex:1;
        min-height:340px; overflow:hidden; }
      /* Canvas is sized in JS to exactly match the wrap. NEVER use max-width
         here — it silently squishes the pixel buffer and the axis text comes
         out warped. */
      .advc-canvas { display:block; cursor:crosshair; border-radius:12px; }
      .advc-legend { display:flex; gap:14px; align-items:center; flex-wrap:wrap;
        font:600 11px 'Plus Jakarta Sans',sans-serif; color:var(--text-secondary); }
      .advc-legend label { display:inline-flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }
      .advc-legend input { accent-color:var(--accent); cursor:pointer; }
      .advc-swatch { width:10px; height:10px; border-radius:3px; display:inline-block; }
      .advc-tooltip { position:absolute; pointer-events:none;
        background:var(--surface-2); border:1px solid var(--border-strong);
        border-radius:10px; padding:8px 10px;
        font:600 11px 'Geist Mono',monospace; color:var(--text-primary);
        box-shadow:var(--shadow-md); z-index:5;
        backdrop-filter:blur(6px); min-width:160px; display:none; }
      .advc-tooltip .ttl { font:700 10px 'Plus Jakarta Sans',sans-serif; color:var(--text-secondary);
        letter-spacing:0.08em; text-transform:uppercase; margin-bottom:4px; }
      .advc-tooltip .row { display:flex; justify-content:space-between; gap:12px; line-height:1.5; }
      .advc-tooltip .k { color:var(--text-secondary); }
      .advc-tooltip .v { color:var(--text-primary); font-variant-numeric:tabular-nums; }
      .advc-empty { display:flex; align-items:center; justify-content:center;
        height:380px; color:var(--text-tertiary); font-size:13px; }

      /* Compact embed — strip chrome and let the host container dictate size.
         Critically, height:100% with min-height:0 so the chart can't push
         out of the card/popup slot it lives in. overflow:hidden so the
         tooltip can never bleed into adjacent rows below the chart. */
      .advc-wrap--compact { background:transparent; border:0; padding:0;
        border-radius:0; box-shadow:none; gap:0;
        height:100%; min-height:0; display:flex; flex-direction:column;
        overflow:hidden; }
      .advc-wrap--compact .advc-canvas-wrap {
        height:100% !important; min-height:0; flex:1;
        position:relative; overflow:hidden; }
      .advc-wrap--compact .advc-canvas {
        border-radius:6px; }
      /* Compact tooltips are compact too — and they live INSIDE the
         canvas-wrap, so the parent's overflow:hidden clips them. */
      .advc-wrap--compact .advc-tooltip {
        font-size:10px; padding:5px 7px; min-width:120px; max-width:160px; }
      .advc-overlay { position:absolute; inset:0; pointer-events:none; }
    `;
    document.head.appendChild(s);
  }

  function buildScaffold(compact) {
    const wrap = document.createElement('div');
    wrap.className = 'advc-wrap' + (compact ? ' advc-wrap--compact' : '');
    if (compact) {
      wrap.innerHTML = `
        <div class="advc-canvas-wrap" data-role="canvas-wrap">
          <canvas class="advc-canvas" data-role="canvas"></canvas>
          <div class="advc-tooltip" data-role="tooltip"></div>
        </div>`;
      return wrap;
    }
    wrap.innerHTML = `
      <div class="advc-bar">
        <div class="advc-bar-l" data-role="periods">
          ${PERIODS.map(p => `<button class="advc-btn ${p.id === DEFAULT_PERIOD ? 'on' : ''}" data-period="${p.id}">${p.label}</button>`).join('')}
        </div>
        <div class="advc-bar-r" style="gap:10px;">
          <button class="advc-btn" data-role="reset-zoom" hidden title="Reset zoom (or double-click chart)">↺ Reset</button>
          <span class="advc-btn on" data-role="mode-price" style="cursor:default;">Price</span>
        </div>
      </div>
      <div class="advc-legend" data-role="legend">
        <label><input type="checkbox" data-toggle="price" checked> <span class="advc-swatch" style="background:${COLORS.price};"></span>Price on NSE</label>
        <label><input type="checkbox" data-toggle="dma50" checked> <span class="advc-swatch" style="background:${COLORS.dma50};"></span>50 DMA</label>
        <label><input type="checkbox" data-toggle="dma200" checked> <span class="advc-swatch" style="background:${COLORS.dma200};"></span>200 DMA</label>
        <label><input type="checkbox" data-toggle="volume" checked> <span class="advc-swatch" style="background:${COLORS.volume};"></span>Volume</label>
        <span style="margin-left:auto;color:#5a6373;font:600 10px 'Geist Mono',monospace;" data-role="caption">drag to zoom · dbl-click to reset</span>
      </div>
      <div class="advc-canvas-wrap" data-role="canvas-wrap">
        <canvas class="advc-canvas" data-role="canvas"></canvas>
        <div class="advc-tooltip" data-role="tooltip"></div>
      </div>`;
    return wrap;
  }

  async function fetchSeries(ticker, period) {
    try {
      // For long periods, sample weekly/monthly to keep payload small —
      // yfinance handles `1y` natively but 10y of daily is heavy.
      const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/chart?period=${encodeURIComponent(period)}`);
      const j = await r.json();
      const arr = (j && j.data) || [];
      return arr.filter(d => d && d.close != null && isFinite(Number(d.close)));
    } catch (_) { return []; }
  }

  function drawChart(canvas, series, opts) {
    const state = opts.state;
    const tooltip = opts.tooltip;
    const wrap = canvas.parentElement;
    const compactMode = !!(state && state.compact);

    // High-DPI sizing. Use getBoundingClientRect (sub-pixel-accurate) instead
    // of clientWidth/Height (rounded ints) — the rounding causes the canvas
    // pixel buffer to mismatch its CSS box by a fraction, which the browser
    // then SCALES into. Text rendered via fillText gets stretched/squished
    // by that scale factor and looks "fucked up". rect.width = exact CSS px.
    const rect = wrap.getBoundingClientRect();
    const cssW = Math.max(40, Math.round(rect.width  || 800));
    const measuredH = Math.round(rect.height || 0);
    const cssH = compactMode
      ? Math.max(60, measuredH || 140)               // compact: respect host
      : (measuredH > 0 ? measuredH : 400);           // full: respect host, fallback 400
    const dpr = window.devicePixelRatio || 1;
    // Set pixel buffer to integer DPR multiples to avoid fractional-pixel
    // sampling artefacts.
    canvas.width  = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    // CSS dimensions must EXACTLY match what the parent gives us — that's
    // the only way the browser doesn't scale our text.
    canvas.style.width  = cssW + 'px';
    canvas.style.height = cssH + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    if (!series.length) {
      ctx.fillStyle = COLORS.text;
      ctx.font = `500 13px ${FONT_UI}`;
      ctx.textAlign = 'center';
      ctx.fillText('No chart data', cssW / 2, cssH / 2);
      return null;
    }

    const closes  = series.map(d => Number(d.close));
    const volumes = series.map(d => Number(d.volume || 0));
    const dma50   = sma(closes, 50);
    const dma200  = sma(closes, 200);

    // Layout — tighter padding in compact mode (no axis labels needed)
    const compact = !!(state && state.compact);
    const padL = compact ? 4  : 56;
    const padR = compact ? 4  : 56;
    const padT = compact ? 10 : 12;   // breathing room above the line
    const padB = compact ? 10 : 36;
    const showVol = state.show.volume;
    const priceH = showVol
      ? Math.round((cssH - padT - padB) * (compact ? 0.78 : 0.72))
      : (cssH - padT - padB);
    const volH   = showVol ? ((cssH - padT - padB) - priceH - (compact ? 6 : 12)) : 0;
    const priceTop = padT;
    const priceBot = padT + priceH;
    const volTop = priceBot + 12;
    const volBot = volTop + volH;
    const chartW = cssW - padL - padR;

    // Y bounds (price)
    let minP = Infinity, maxP = -Infinity;
    for (let i = 0; i < closes.length; i++) {
      if (closes[i] < minP) minP = closes[i];
      if (closes[i] > maxP) maxP = closes[i];
    }
    if (state.show.dma50)  for (let i = 0; i < dma50.length;  i++) if (dma50[i]  != null) { if (dma50[i] < minP)  minP = dma50[i];  if (dma50[i] > maxP)  maxP = dma50[i]; }
    if (state.show.dma200) for (let i = 0; i < dma200.length; i++) if (dma200[i] != null) { if (dma200[i] < minP) minP = dma200[i]; if (dma200[i] > maxP) maxP = dma200[i]; }
    const padding = (maxP - minP) * 0.05 || 1;
    minP -= padding; maxP += padding;
    const pRange = maxP - minP || 1;

    let maxV = 0;
    for (let i = 0; i < volumes.length; i++) if (volumes[i] > maxV) maxV = volumes[i];

    const n = series.length;
    const x = (i) => padL + (n > 1 ? (i / (n - 1)) * chartW : chartW / 2);
    const yPrice = (v) => priceBot - ((v - minP) / pRange) * priceH;
    const yVol = (v) => volBot - (maxV ? (v / maxV) * volH : 0);

    // ── Grid ────────────────────────────────────────────────────────────
    const HGRID = compact ? 0 : 5;
    if (HGRID) {
      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i <= HGRID; i++) {
        const yy = priceTop + (priceH / HGRID) * i;
        ctx.moveTo(padL, yy + 0.5); ctx.lineTo(padL + chartW, yy + 0.5);
      }
      ctx.stroke();
    }

    if (!compact) {
      // Price-axis labels (right) — same Geist Mono used elsewhere on the
      // site for numeric/tabular values. Tabular-nums enabled so digits
      // stack vertically across rows.
      ctx.fillStyle = COLORS.text;
      ctx.font = `600 10.5px ${FONT_MONO}`;
      ctx.textAlign = 'left';
      for (let i = 0; i <= HGRID; i++) {
        const yy = priceTop + (priceH / HGRID) * i;
        const v = maxP - (pRange / HGRID) * i;
        ctx.fillText(fmtPrice(v), padL + chartW + 6, yy + 3);
      }
      // Volume-axis labels (left) — same mono stack
      ctx.textAlign = 'right';
      if (showVol) {
        ctx.fillText(fmtVol(maxV), padL - 6, volTop + 8);
        ctx.fillText(fmtVol(maxV / 2), padL - 6, volTop + volH / 2 + 4);
        ctx.fillText('0', padL - 6, volBot);
      }
      // X-axis date labels — Inter (UI font), matches the rest of the page
      ctx.textAlign = 'center';
      ctx.fillStyle = COLORS.text;
      ctx.font = `600 10.5px ${FONT_UI}`;
      const TICKS = Math.min(6, n);
      for (let i = 0; i < TICKS; i++) {
        const idx = Math.round((i / (TICKS - 1)) * (n - 1));
        ctx.fillText(shortDate(series[idx].date), x(idx), volBot + 18);
      }
    }

    // ── Volume bars ─────────────────────────────────────────────────────
    if (state.show.volume) {
      ctx.fillStyle = COLORS.volume;
      const barW = Math.max(1, Math.min(8, chartW / n - 1));
      for (let i = 0; i < n; i++) {
        const h = volBot - yVol(volumes[i]);
        if (h <= 0) continue;
        ctx.fillRect(x(i) - barW / 2, yVol(volumes[i]), barW, h);
      }
    }

    // ── DMAs ────────────────────────────────────────────────────────────
    function drawLine(values, color, width) {
      ctx.strokeStyle = color;
      ctx.lineWidth = width || 1.4;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < values.length; i++) {
        const v = values[i];
        if (v == null) { started = false; continue; }
        const px = x(i), py = yPrice(v);
        if (!started) { ctx.moveTo(px, py); started = true; }
        else          { ctx.lineTo(px, py); }
      }
      ctx.stroke();
    }

    if (state.show.dma200) drawLine(dma200, COLORS.dma200, 1.6);
    if (state.show.dma50)  drawLine(dma50,  COLORS.dma50,  1.6);

    // ── Price line + area ───────────────────────────────────────────────
    if (state.show.price) {
      // Area fill
      const grad = ctx.createLinearGradient(0, priceTop, 0, priceBot);
      grad.addColorStop(0, 'rgba(124,140,248,0.18)');
      grad.addColorStop(1, 'rgba(124,140,248,0.00)');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.moveTo(x(0), yPrice(closes[0]));
      for (let i = 1; i < n; i++) ctx.lineTo(x(i), yPrice(closes[i]));
      ctx.lineTo(x(n - 1), priceBot);
      ctx.lineTo(x(0), priceBot);
      ctx.closePath();
      ctx.fill();

      // Line
      ctx.strokeStyle = COLORS.price;
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      ctx.moveTo(x(0), yPrice(closes[0]));
      for (let i = 1; i < n; i++) ctx.lineTo(x(i), yPrice(closes[i]));
      ctx.stroke();
    }

    // ── Border ──────────────────────────────────────────────────────────
    if (!compact) {
      ctx.strokeStyle = COLORS.axis;
      ctx.lineWidth = 1;
      ctx.strokeRect(padL + 0.5, priceTop + 0.5, chartW, priceH);
      if (showVol) ctx.strokeRect(padL + 0.5, volTop + 0.5, chartW, volH);
    }

    // Return geometry for tooltip hit-testing
    return {
      n, series, closes, volumes, dma50, dma200,
      padL, padR, padT, padB, priceH, volH, priceTop, priceBot, volTop, volBot,
      chartW, cssW, cssH, minP, maxP, pRange, x, yPrice, yVol,
    };
  }

  function attachInteractivity(canvas, geom, state, tooltip, onZoomChange) {
    if (!geom) return;

    // Overlay canvas above the main chart for crosshair + drag rectangle —
    // avoids redrawing the entire chart on every mousemove. Match the main
    // canvas pixel-for-pixel so the crosshair lands exactly where the chart
    // expects it.
    let overlay = canvas.parentElement.querySelector('.advc-overlay');
    if (!overlay) {
      overlay = document.createElement('canvas');
      overlay.className = 'advc-overlay';
      overlay.style.cssText = 'position:absolute;inset:0;pointer-events:none;';
      canvas.parentElement.appendChild(overlay);
    }
    const dpr = window.devicePixelRatio || 1;
    overlay.width  = canvas.width;
    overlay.height = canvas.height;
    overlay.style.width  = canvas.style.width;
    overlay.style.height = canvas.style.height;
    const octx = overlay.getContext('2d');
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);

    function nearestIndex(mx) {
      let nearest = 0, best = Infinity;
      for (let i = 0; i < geom.n; i++) {
        const dx = Math.abs(geom.x(i) - mx);
        if (dx < best) { best = dx; nearest = i; }
      }
      return nearest;
    }

    function drawCrosshair(idx, mx, my, dragRect) {
      octx.clearRect(0, 0, geom.cssW, geom.cssH);
      if (dragRect) {
        // Drag selection rectangle (zoom-to-range)
        octx.fillStyle = 'rgba(124,140,248,0.12)';
        octx.fillRect(dragRect.x0, geom.priceTop, dragRect.x1 - dragRect.x0, geom.priceH);
        octx.strokeStyle = 'rgba(124,140,248,0.5)';
        octx.lineWidth = 1;
        octx.setLineDash([4, 3]);
        octx.strokeRect(dragRect.x0 + 0.5, geom.priceTop + 0.5, dragRect.x1 - dragRect.x0, geom.priceH);
        octx.setLineDash([]);
        return;
      }
      if (idx < 0 || idx >= geom.n) return;
      const px = geom.x(idx);
      const py = geom.yPrice(geom.closes[idx]);
      // Vertical crosshair
      octx.strokeStyle = 'rgba(141,148,168,0.45)';
      octx.lineWidth = 1;
      octx.setLineDash([3, 3]);
      octx.beginPath();
      octx.moveTo(px + 0.5, geom.priceTop);
      octx.lineTo(px + 0.5, geom.volBot);
      octx.stroke();
      // Horizontal crosshair at price level
      octx.beginPath();
      octx.moveTo(geom.padL, py + 0.5);
      octx.lineTo(geom.padL + geom.chartW, py + 0.5);
      octx.stroke();
      octx.setLineDash([]);
      // Dot at price point — ring color follows theme so it stays visible
      octx.fillStyle = COLORS.price;
      octx.strokeStyle = COLORS.bg;
      octx.lineWidth = 2;
      octx.beginPath();
      octx.arc(px, py, 4, 0, Math.PI * 2);
      octx.fill();
      octx.stroke();
    }

    function showTooltip(idx, mx, my) {
      const p = geom.series[idx];
      if (!p) { tooltip.style.display = 'none'; return; }
      const cls = geom.closes[idx];
      const vol = geom.volumes[idx];
      const d50 = geom.dma50[idx];
      const d200 = geom.dma200[idx];
      tooltip.innerHTML = `
        <div class="ttl">${fmtDate(p.date)}</div>
        <div class="row"><span class="k">Close</span><span class="v" style="color:${COLORS.price};">₹${fmtPrice(cls)}</span></div>
        ${d50 != null ? `<div class="row"><span class="k">50 DMA</span><span class="v" style="color:${COLORS.dma50};">₹${fmtPrice(d50)}</span></div>` : ''}
        ${d200 != null ? `<div class="row"><span class="k">200 DMA</span><span class="v" style="color:${COLORS.dma200};">₹${fmtPrice(d200)}</span></div>` : ''}
        <div class="row"><span class="k">Volume</span><span class="v">${fmtVol(vol)}</span></div>`;
      tooltip.style.display = 'block';
      const w = tooltip.offsetWidth || 160;
      const h = tooltip.offsetHeight || 80;
      // Horizontal: prefer right of cursor, flip left if it would overflow
      let tx = mx + 14;
      if (tx + w > geom.cssW - 4) tx = Math.max(4, mx - w - 14);
      // Vertical: anchor inside the chart area. If the chart is too short to
      // fit the tooltip above OR below the cursor without overflowing, just
      // clamp to the top edge — the canvas-wrap has overflow:hidden so
      // anything still sticking out gets clipped.
      let ty = my - h - 8;
      if (ty < 4) ty = my + 14;
      if (ty + h > geom.cssH - 4) ty = Math.max(4, geom.cssH - h - 4);
      tooltip.style.left = tx + 'px';
      tooltip.style.top  = ty + 'px';
    }

    // ── Drag-to-zoom state ───────────────────────────────────────────────
    let dragging = false;
    let dragStartX = 0;
    let dragStartIdx = 0;

    canvas.onmousemove = (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const idx = nearestIndex(mx);
      if (dragging) {
        const x0 = Math.min(dragStartX, mx);
        const x1 = Math.max(dragStartX, mx);
        drawCrosshair(-1, mx, my, { x0, x1 });
        tooltip.style.display = 'none';
        return;
      }
      drawCrosshair(idx, mx, my, null);
      showTooltip(idx, mx, my);
    };
    canvas.onmouseleave = () => {
      tooltip.style.display = 'none';
      if (!dragging) octx.clearRect(0, 0, geom.cssW, geom.cssH);
    };
    canvas.onmousedown = (e) => {
      // Only left-click in the price area starts a zoom drag
      if (e.button !== 0) return;
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      if (my < geom.priceTop || my > geom.priceBot) return;
      dragging = true;
      dragStartX = mx;
      dragStartIdx = nearestIndex(mx);
      canvas.style.cursor = 'ew-resize';
    };
    function endDrag(e) {
      if (!dragging) return;
      dragging = false;
      canvas.style.cursor = 'crosshair';
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const endIdx = nearestIndex(mx);
      const lo = Math.min(dragStartIdx, endIdx);
      const hi = Math.max(dragStartIdx, endIdx);
      octx.clearRect(0, 0, geom.cssW, geom.cssH);
      // Need at least 3 points to be a meaningful zoom
      if (hi - lo >= 2 && typeof onZoomChange === 'function') {
        onZoomChange({ lo, hi });
      }
    }
    canvas.onmouseup = endDrag;
    window.addEventListener('mouseup', endDrag);

    // Double-click resets zoom
    canvas.ondblclick = () => {
      if (typeof onZoomChange === 'function') onZoomChange(null);
    };

    // Touch (mobile) — single-finger drag = zoom select
    canvas.ontouchstart = (e) => {
      if (e.touches.length !== 1) return;
      const rect = canvas.getBoundingClientRect();
      const mx = e.touches[0].clientX - rect.left;
      const my = e.touches[0].clientY - rect.top;
      if (my < geom.priceTop || my > geom.priceBot) return;
      dragging = true; dragStartX = mx; dragStartIdx = nearestIndex(mx);
    };
    canvas.ontouchmove = (e) => {
      if (!dragging || e.touches.length !== 1) return;
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mx = e.touches[0].clientX - rect.left;
      drawCrosshair(-1, mx, 0, { x0: Math.min(dragStartX, mx), x1: Math.max(dragStartX, mx) });
    };
    canvas.ontouchend = (e) => {
      if (!dragging) return;
      const rect = canvas.getBoundingClientRect();
      const touch = (e.changedTouches && e.changedTouches[0]) || null;
      const mx = touch ? touch.clientX - rect.left : dragStartX;
      const endIdx = nearestIndex(mx);
      const lo = Math.min(dragStartIdx, endIdx);
      const hi = Math.max(dragStartIdx, endIdx);
      dragging = false;
      octx.clearRect(0, 0, geom.cssW, geom.cssH);
      if (hi - lo >= 2 && typeof onZoomChange === 'function') onZoomChange({ lo, hi });
    };
  }

  // Fonts must be loaded before we paint canvas text, otherwise the first
  // render uses generic fallbacks. Once fonts.ready resolves we cache the
  // result so subsequent mounts don't wait again.
  let _fontsReady = null;
  function fontsReady() {
    if (_fontsReady) return _fontsReady;
    if (document.fonts && document.fonts.ready) {
      _fontsReady = Promise.race([
        document.fonts.ready,
        new Promise(r => setTimeout(r, 800)),  // never block more than 800ms
      ]);
    } else {
      _fontsReady = Promise.resolve();
    }
    return _fontsReady;
  }

  async function mount(container, ticker, opts) {
    ensureStyles();
    opts = opts || {};
    const period = opts.period || DEFAULT_PERIOD;
    const compact = !!opts.compact;
    if (typeof container === 'string') container = document.querySelector(container);
    if (!container) return null;

    container.innerHTML = '';
    const root = buildScaffold(compact);
    container.appendChild(root);

    const state = {
      ticker: ticker.toUpperCase(),
      period,
      series: [],
      // Compact mode hides DMAs + volume by default — the small chart is
      // primarily a price preview, the full chart lives on stock.html
      show: compact
        ? { price: true, dma50: false, dma200: false, volume: false }
        : { price: true, dma50: true,  dma200: true,  volume: true  },
      zoom: null,  // {lo, hi} when user has drag-zoomed
      compact,
    };

    const canvas    = root.querySelector('[data-role="canvas"]');
    const tooltip   = root.querySelector('[data-role="tooltip"]');
    const caption   = root.querySelector('[data-role="caption"]');
    const periodBar = root.querySelector('[data-role="periods"]');
    const legend    = root.querySelector('[data-role="legend"]');
    const resetBtn  = root.querySelector('[data-role="reset-zoom"]');

    function viewSeries() {
      if (state.zoom) return state.series.slice(state.zoom.lo, state.zoom.hi + 1);
      return state.series;
    }

    async function load(p) {
      state.period = p;
      state.zoom = null;
      if (resetBtn) resetBtn.hidden = true;
      if (caption) caption.textContent = state.ticker + ' · ' + p.toUpperCase() + ' · loading…';
      state.series = await fetchSeries(state.ticker, p);
      if (caption) caption.textContent = state.ticker + ' · ' + p.toUpperCase() + ' · ' + state.series.length + ' bars · drag to zoom';
      render();
    }
    function render() {
      const slice = viewSeries();
      const geom = drawChart(canvas, slice, { state, tooltip });
      attachInteractivity(canvas, geom, state, tooltip, onZoom);
    }
    function onZoom(sel) {
      if (!sel) {
        state.zoom = null;
        if (resetBtn) resetBtn.hidden = true;
      } else {
        // Translate slice indices back into absolute series indices
        const base = state.zoom ? state.zoom.lo : 0;
        state.zoom = { lo: base + sel.lo, hi: base + sel.hi };
        if (resetBtn) resetBtn.hidden = false;
        if (caption) {
          const a = state.series[state.zoom.lo]?.date;
          const b = state.series[state.zoom.hi]?.date;
          if (a && b) caption.textContent = state.ticker + ' · zoomed ' + shortDate(a) + ' → ' + shortDate(b) + ' · dbl-click to reset';
        }
      }
      render();
    }

    if (periodBar) periodBar.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-period]');
      if (!btn) return;
      periodBar.querySelectorAll('[data-period]').forEach(b => b.classList.toggle('on', b === btn));
      load(btn.dataset.period);
    });

    if (legend) legend.addEventListener('change', (e) => {
      const t = e.target.closest('[data-toggle]');
      if (!t) return;
      state.show[t.dataset.toggle] = !!t.checked;
      render();
    });

    if (resetBtn) resetBtn.addEventListener('click', () => onZoom(null));

    // Compact-mode click → open the full chart on stock.html (interactive escape hatch)
    if (compact) {
      canvas.style.cursor = 'pointer';
      canvas.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        window.location.href = '/stock.html?t=' + encodeURIComponent(state.ticker);
      });
    }

    // Re-render on resize (debounced)
    let resizeT;
    window.addEventListener('resize', () => {
      clearTimeout(resizeT);
      resizeT = setTimeout(render, 120);
    });

    // Re-render on theme switch. Important: do NOT call any resize path here —
    // the canvas pixel buffer is sized from rect.width in drawChart and any
    // intermediate resize() would warp axis text. Clear the overlay first so
    // a stale crosshair from the previous theme can't ghost into the new paint.
    let themeT;
    const unsubTheme = (window.ThemeTokens && window.ThemeTokens.onChange)
      ? window.ThemeTokens.onChange(() => {
          clearTimeout(themeT);
          themeT = setTimeout(() => {
            try {
              const overlay = canvas.parentElement.querySelector('.advc-overlay');
              if (overlay) {
                const oc = overlay.getContext('2d');
                oc.clearRect(0, 0, overlay.width, overlay.height);
              }
              if (tooltip) tooltip.style.display = 'none';
            } catch (_) {}
            render();
          }, 50);
        })
      : function () {};

    // Wait for site fonts before the first paint so axis labels render in
    // the right typeface (Inter / Geist Mono) instead of generic fallbacks.
    await fontsReady();
    await load(period);
    return {
      reload: () => load(state.period),
      setPeriod: (p) => load(p),
      resetZoom: () => onZoom(null),
      state,
    };
  }

  window.AdvancedChart = { mount, PERIODS };
})();
