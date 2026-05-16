/* charts.js — Tickwave chart wrappers built on ApexCharts (CDN).
 *
 * Why this file exists:
 *   The original UI used a dozen flavours of custom canvas/SVG (priceChart
 *   canvas in stock.html, Widgets.spark / Widgets.miniSparkGrid SVG, hand-
 *   rolled chart in global.html). They worked but each looked different,
 *   none zoomed, and every page implemented its own crosshair from scratch.
 *
 *   This module gives every chart on the site one consistent look:
 *     - Charts.price(el, points)    full price chart (line + volume bars + crosshair tooltip)
 *     - Charts.spark(el, values)    tiny inline sparkline (used in KPI tiles, tables)
 *     - Charts.area(el, points)     gradient area chart (used on global, dashboards)
 *
 * Pages that need charts add ONE script tag:
 *   <script src="https://cdn.jsdelivr.net/npm/apexcharts@latest/dist/apexcharts.min.js"></script>
 *   <script src="./shared/js/charts.js"></script>
 *
 * The wrappers honour the existing rf-* CSS tokens so dark/light themes
 * work without per-chart configuration.
 */
(function () {
  // CSS-token-aware palette
  function readVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (_) {
      return fallback;
    }
  }
  function palette() {
    return {
      text:   readVar('--rf-text',      '#fafafa'),
      mute:   readVar('--rf-text-mute', 'rgba(255,255,255,0.55)'),
      dim:    readVar('--rf-text-dim',  'rgba(255,255,255,0.35)'),
      line:   readVar('--rf-line',      'rgba(255,255,255,0.07)'),
      bull:   readVar('--rf-bull',      '#4ade80'),
      bear:   readVar('--rf-bear',      '#f87171'),
      bg:     readVar('--rf-bg',        '#0a0a0a'),
      card:   readVar('--rf-card',      '#111111'),
    };
  }

  function ensureApex() {
    if (typeof window.ApexCharts === 'undefined') {
      console.warn('[Charts] ApexCharts not loaded — add the CDN script tag.');
      return false;
    }
    return true;
  }

  function resolveEl(idOrEl) {
    return typeof idOrEl === 'string'
      ? document.getElementById(idOrEl) || document.querySelector(idOrEl)
      : idOrEl;
  }

  // ───── PRICE CHART ─────────────────────────────────────────────────
  // Line chart with volume bars beneath, crosshair tooltip showing
  // date + price + volume. Accepts the API shape from /api/stock/<t>/chart:
  //   [{date: 'YYYY-MM-DD', close, high?, low?, volume}, ...]
  function price(el, points, opts) {
    if (!ensureApex()) return null;
    const target = resolveEl(el);
    if (!target) return null;
    points = points || [];
    opts = opts || {};
    const p = palette();

    const priceSeries = points.map(d => ({
      x: new Date(d.date).getTime(),
      y: typeof d.close === 'number' ? d.close : (d.value != null ? d.value : null),
    })).filter(pt => pt.y != null);

    const volSeries = points.map(d => ({
      x: new Date(d.date).getTime(),
      y: d.volume != null ? d.volume : 0,
    }));

    const last = priceSeries[priceSeries.length - 1];
    const first = priceSeries[0];
    const trendBull = last && first ? last.y >= first.y : true;
    const trendColor = trendBull ? p.bull : p.bear;
    const showVolume = opts.showVolume !== false && volSeries.some(v => v.y > 0);

    const config = {
      chart: {
        type: 'line',
        height: opts.height || 420,
        background: 'transparent',
        toolbar: { show: false },
        zoom: { enabled: true, type: 'x', autoScaleYaxis: true },
        animations: { enabled: !opts.disableAnim, speed: 350, easing: 'easeOut' },
        fontFamily: 'Inter, system-ui, sans-serif',
        foreColor: p.mute,
      },
      theme: { mode: 'dark' },
      series: [
        { name: 'Price', type: 'area', data: priceSeries },
        ...(showVolume ? [{ name: 'Volume', type: 'column', data: volSeries }] : []),
      ],
      stroke: { curve: 'smooth', width: [2, 0] },
      colors: [trendColor, p.dim],
      fill: {
        type: ['gradient', 'solid'],
        gradient: {
          shade: 'dark', type: 'vertical',
          opacityFrom: 0.35, opacityTo: 0.02,
          stops: [0, 100],
        },
        opacity: [0.85, 0.35],
      },
      grid: {
        borderColor: p.line,
        strokeDashArray: 3,
        padding: { left: 6, right: 12, top: 0, bottom: 0 },
        xaxis: { lines: { show: false } },
        yaxis: { lines: { show: true } },
      },
      xaxis: {
        type: 'datetime',
        axisBorder: { show: false },
        axisTicks: { color: p.line },
        labels: { style: { colors: p.dim, fontSize: '11px' }, datetimeUTC: false },
        crosshairs: { show: true, stroke: { color: p.mute, dashArray: 4, width: 1 } },
      },
      yaxis: showVolume ? [
        {
          axisBorder: { show: false },
          labels: {
            style: { colors: p.dim, fontSize: '11px' },
            formatter: (v) => v == null ? '' : '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 1 }),
          },
          tickAmount: 4,
        },
        {
          opposite: true,
          axisBorder: { show: false },
          labels: { style: { colors: p.dim, fontSize: '10px' },
                    formatter: (v) => formatVolumeShort(v) },
          max: Math.max(...volSeries.map(v => v.y || 0)) * 4 || undefined,
          tickAmount: 4,
        },
      ] : {
        axisBorder: { show: false },
        labels: {
          style: { colors: p.dim, fontSize: '11px' },
          formatter: (v) => v == null ? '' : '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 1 }),
        },
      },
      dataLabels: { enabled: false },
      legend: { show: false },
      tooltip: {
        theme: 'dark',
        x: { format: 'd MMM yyyy' },
        y: [
          { formatter: (v) => v == null ? '' : '₹' + Number(v).toFixed(2) },
          { formatter: (v) => v == null ? '' : formatVolumeShort(v) },
        ],
        fillSeriesColor: false,
        marker: { show: true },
      },
      markers: { size: 0, strokeWidth: 0, hover: { size: 5 } },
      plotOptions: { bar: { columnWidth: '60%' } },
    };

    target.innerHTML = '';
    const chart = new ApexCharts(target, config);
    chart.render();
    return chart;
  }

  function formatVolumeShort(v) {
    if (v == null) return '';
    const n = Number(v);
    if (n >= 1e7) return (n / 1e7).toFixed(1) + ' Cr';
    if (n >= 1e5) return (n / 1e5).toFixed(1) + ' L';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return n.toFixed(0);
  }

  // ───── SPARKLINE ───────────────────────────────────────────────────
  // Bare-minimum line chart for KPI tiles, table cells. No axes, no
  // tooltip by default. Accepts a flat array of numbers or {x,y} pairs.
  function spark(el, values, opts) {
    if (!ensureApex()) return null;
    const target = resolveEl(el);
    if (!target) return null;
    opts = opts || {};
    const p = palette();
    const arr = (values || []).map((v, i) => typeof v === 'number'
      ? { x: i, y: v }
      : { x: v.x, y: v.y });
    const isUp = arr.length > 1 ? arr[arr.length - 1].y >= arr[0].y : true;
    const color = opts.color || (isUp ? p.bull : p.bear);

    const config = {
      chart: {
        type: 'line',
        sparkline: { enabled: true },
        height: opts.height || 32,
        background: 'transparent',
        animations: { enabled: !opts.disableAnim, speed: 250 },
      },
      series: [{ name: opts.name || 'value', data: arr }],
      stroke: { curve: 'smooth', width: opts.strokeWidth || 1.6 },
      colors: [color],
      tooltip: opts.tooltip ? { theme: 'dark', x: { show: false } } : { enabled: false },
      markers: { size: 0 },
    };

    target.innerHTML = '';
    const chart = new ApexCharts(target, config);
    chart.render();
    return chart;
  }

  // ───── AREA CHART ──────────────────────────────────────────────────
  // Gradient area chart with axes — used on dashboards / global.
  function area(el, points, opts) {
    if (!ensureApex()) return null;
    const target = resolveEl(el);
    if (!target) return null;
    opts = opts || {};
    const p = palette();
    const series = (points || []).map(d => ({
      x: typeof d.x !== 'undefined' ? d.x : new Date(d.date).getTime(),
      y: typeof d.y !== 'undefined' ? d.y : (d.close != null ? d.close : d.value),
    })).filter(pt => pt.y != null);
    const isUp = series.length > 1 ? series[series.length - 1].y >= series[0].y : true;
    const color = opts.color || (isUp ? p.bull : p.bear);

    const config = {
      chart: {
        type: 'area',
        height: opts.height || 220,
        background: 'transparent',
        toolbar: { show: false },
        animations: { enabled: !opts.disableAnim, speed: 250 },
        fontFamily: 'Inter, system-ui, sans-serif',
        foreColor: p.mute,
      },
      series: [{ name: opts.name || 'value', data: series }],
      stroke: { curve: 'smooth', width: 2 },
      colors: [color],
      fill: {
        type: 'gradient',
        gradient: { shade: 'dark', opacityFrom: 0.4, opacityTo: 0.02, stops: [0, 100] },
      },
      grid: { borderColor: p.line, strokeDashArray: 3, padding: { left: 4, right: 8 } },
      xaxis: {
        type: opts.xType || 'datetime',
        axisBorder: { show: false },
        labels: { style: { colors: p.dim, fontSize: '10px' } },
      },
      yaxis: {
        axisBorder: { show: false },
        labels: { style: { colors: p.dim, fontSize: '10px' } },
      },
      dataLabels: { enabled: false },
      legend: { show: false },
      tooltip: { theme: 'dark' },
      markers: { size: 0, hover: { size: 4 } },
    };

    target.innerHTML = '';
    const chart = new ApexCharts(target, config);
    chart.render();
    return chart;
  }

  window.Charts = { price, spark, area, palette, formatVolumeShort };
})();
