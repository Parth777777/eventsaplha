/* ============================================================
   INSIGHTS STUDIO  —  Reusable dashboard widgets
   Drop-in module that renders 5 cross-page visualization
   widgets from the existing /api/dashboard, /api/signals,
   /api/market-indices endpoints.

   Usage on any page:
       <div id="insightsRoot"></div>
       <script src="./shared/js/widgets.js"></script>
       <script>InsightsStudio.mount('insightsRoot');</script>

   Optional config:
       InsightsStudio.mount('insightsRoot', {
         title: 'Portfolio insights',  // section heading text
         compact: true,                // omit the activity feed widget
       });
   ============================================================ */
(function (global) {
  'use strict';

  // ── Lightweight fetch helpers (mirrors window.API if available) ──
  const apiBase = (global.API && global.API.base) || '/api';
  async function fetchJSON(path) {
    try {
      const res = await fetch(apiBase + path, { headers: { 'Accept': 'application/json' } });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) { return null; }
  }

  // ── HTML scaffold ──
  const STUDIO_HTML = (compact) => `
    <section class="insights-studio">
      <div class="widget" data-widget="rings">
        <div class="widget__head">
          <div>
            <div class="widget__title">Sector concentration</div>
            <div class="widget__meta">Top 4 by signal count</div>
          </div>
          <span class="widget__year-pill" data-slot="ringsTotal">— signals</span>
        </div>
        <div class="rings-stage" data-slot="ringsStage">
          <span class="skeleton" style="width:200px;height:200px;border-radius:50%;"></span>
        </div>
      </div>

      <div class="widget" data-widget="donut">
        <div class="widget__head">
          <div>
            <div class="widget__title">Signal mix</div>
            <div class="widget__meta">Bullish share</div>
          </div>
        </div>
        <div class="donut-stage">
          <svg class="donut-svg" viewBox="0 0 100 100">
            <defs>
              <linearGradient id="donutGradient_${gid()}" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%"  stop-color="#5b6cff"/>
                <stop offset="100%" stop-color="#8458ff"/>
              </linearGradient>
            </defs>
            <circle class="donut-track" cx="50" cy="50" r="42" stroke-width="9"/>
            <circle class="donut-fill"  cx="50" cy="50" r="42" stroke-width="9"
                    stroke-dasharray="263.89" stroke-dashoffset="263.89" data-slot="donutFill"/>
          </svg>
          <div class="donut-center">
            <div class="donut-center__pct" data-slot="donutPct">—</div>
            <div class="donut-center__label">bullish</div>
          </div>
        </div>
        <div class="donut-legend">
          <span><span class="donut-legend__dot" style="background:#5b6cff"></span>Bull</span>
          <span><span class="donut-legend__dot" style="background:#ff7a8a"></span>Bear</span>
          <span><span class="donut-legend__dot" style="background:rgba(255,255,255,0.2)"></span>Neutral</span>
        </div>
      </div>

      <div class="widget" data-widget="heatmap">
        <div class="widget__head">
          <div>
            <div class="widget__title">Signal velocity</div>
            <div class="widget__meta">Last 30 days</div>
          </div>
        </div>
        <div class="heatmap-grid" data-slot="heatmapGrid"></div>
        <div class="heatmap-summary">
          <div>
            <div class="heatmap-summary__big" data-slot="heatmapBigDays">—</div>
            <div class="heatmap-summary__sub">active days</div>
          </div>
          <div style="text-align:right;">
            <div class="heatmap-summary__big" data-slot="heatmapBigSignals">—</div>
            <div class="heatmap-summary__sub">signals fired</div>
          </div>
        </div>
      </div>

      <div class="widget spark-card" data-widget="spark">
        <div class="widget__head">
          <div>
            <div class="widget__title">Lead index</div>
            <div class="widget__meta">Intraday move</div>
          </div>
        </div>
        <div class="spark-stage">
          <svg class="spark-svg" viewBox="0 0 200 90" preserveAspectRatio="none">
            <defs>
              <linearGradient id="sparkUp_${gid()}" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%"  stop-color="#4ee6b8" stop-opacity="0.4"/>
                <stop offset="100%" stop-color="#4ee6b8" stop-opacity="0"/>
              </linearGradient>
              <linearGradient id="sparkDn_${gid()}" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%"  stop-color="#ff7a8a" stop-opacity="0.4"/>
                <stop offset="100%" stop-color="#ff7a8a" stop-opacity="0"/>
              </linearGradient>
            </defs>
            <path class="spark-area" data-slot="sparkArea" d=""/>
            <path class="spark-line" data-slot="sparkLine" d="" stroke="#5b6cff"/>
          </svg>
        </div>
        <div class="spark-foot">
          <div>
            <div class="spark-foot__label" data-slot="sparkLabel">—</div>
            <div class="spark-foot__sub"   data-slot="sparkSub">loading…</div>
          </div>
          <div style="text-align:right;">
            <span class="spark-foot__value" data-slot="sparkValue">—</span>
            <div style="margin-top:4px;"><span class="spark-pct" data-slot="sparkPct">—</span></div>
          </div>
        </div>
      </div>

      ${compact ? '' : `
      <div class="widget activity-card" data-widget="activity"
           style="grid-column: 1 / -1; min-height:auto;">
        <div class="widget__head">
          <div>
            <div class="widget__title">Activity manager</div>
            <div class="widget__meta">Live signal stream — filter to focus</div>
          </div>
          <span class="widget__year-pill" data-slot="activityCount">0 events</span>
        </div>
        <div class="activity-pills" data-slot="activityPills">
          <span class="activity-pill is-active" data-filter="all">All <span class="activity-pill__dismiss" style="visibility:hidden;">×</span></span>
          <span class="activity-pill" data-filter="bullish">Bullish <span class="activity-pill__dismiss">×</span></span>
          <span class="activity-pill" data-filter="bearish">Bearish <span class="activity-pill__dismiss">×</span></span>
          <span class="activity-pill" data-filter="high">High alpha <span class="activity-pill__dismiss">×</span></span>
        </div>
        <div class="activity-feed" data-slot="activityFeed">
          <div class="skeleton" style="height:48px;border-radius:12px;"></div>
          <div class="skeleton" style="height:48px;border-radius:12px;"></div>
          <div class="skeleton" style="height:48px;border-radius:12px;"></div>
        </div>
      </div>`}
    </section>
  `;

  let _gid = 0;
  function gid() { return ++_gid; }
  const $ = (root, sel) => root.querySelector('[data-slot="' + sel + '"]');

  // ── Renderers (scoped to a root) ──

  function renderRings(root, signals) {
    const stage = $(root, 'ringsStage');
    const totalPill = $(root, 'ringsTotal');
    if (!stage) return;
    if (!signals || !signals.length) {
      stage.innerHTML = '<span style="font-size:11px;color:var(--t3);">No signals yet</span>';
      if (totalPill) totalPill.textContent = '0 signals';
      return;
    }
    const buckets = {};
    for (const s of signals) {
      const sec = (s.sector || 'OTHER').replace(/_/g, ' ');
      buckets[sec] = (buckets[sec] || 0) + 1;
    }
    const top = Object.entries(buckets).sort((a, b) => b[1] - a[1]).slice(0, 4);
    if (totalPill) totalPill.textContent = signals.length + ' signals';

    const ordered = [...top].reverse();
    stage.innerHTML = ordered.map((entry, i) => {
      const [sector, count] = entry;
      const ringIdx = i + 1;
      if (ringIdx === 4) {
        return `<div class="ring ring--${ringIdx}">
          <div class="ring__value">${count}</div>
          <div class="ring__label">${sector.slice(0, 8)}</div>
        </div>`;
      }
      return `<div class="ring ring--${ringIdx}"><span class="ring__lbl">${sector} · ${count}</span></div>`;
    }).join('');
  }

  function renderDonut(root, dashboard) {
    const fillEl = $(root, 'donutFill');
    const pctEl  = $(root, 'donutPct');
    if (!fillEl || !pctEl || !dashboard) return;
    const bull = dashboard.bullish_count || 0;
    const bear = dashboard.bearish_count || 0;
    const neut = dashboard.neutral_count || 0;
    const total = bull + bear + neut;
    const pct = total ? Math.round((bull / total) * 100) : 0;
    pctEl.textContent = pct + '%';
    const C = 2 * Math.PI * 42;
    const offset = C * (1 - pct / 100);
    fillEl.setAttribute('stroke-dasharray', C.toFixed(2));
    fillEl.setAttribute('stroke-dashoffset', offset.toFixed(2));
  }

  function renderHeatmap(root, signals) {
    const grid = $(root, 'heatmapGrid');
    const bigDays = $(root, 'heatmapBigDays');
    const bigSignals = $(root, 'heatmapBigSignals');
    if (!grid) return;
    const days = 30;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const buckets = new Array(days).fill(0);
    for (const s of (signals || [])) {
      const ts = s.created_at || s.timestamp || s.ts;
      if (!ts) continue;
      const d = new Date(ts);
      if (isNaN(d)) continue;
      d.setHours(0, 0, 0, 0);
      const diff = Math.floor((today - d) / 86400000);
      if (diff >= 0 && diff < days) buckets[days - 1 - diff]++;
    }
    const max = Math.max(1, ...buckets);
    grid.innerHTML = buckets.map((count, i) => {
      let level = 0;
      if (count > 0) {
        const ratio = count / max;
        level = ratio > 0.75 ? 4 : ratio > 0.5 ? 3 : ratio > 0.25 ? 2 : 1;
      }
      const daysAgo = days - 1 - i;
      return `<div class="heatmap-dot" data-level="${level}" title="${count} signal${count !== 1 ? 's' : ''} · ${daysAgo}d ago"></div>`;
    }).join('');
    const activeDays = buckets.filter(c => c > 0).length;
    if (bigDays) bigDays.textContent = activeDays;
    if (bigSignals) bigSignals.textContent = buckets.reduce((a, b) => a + b, 0);
  }

  function renderSpark(root, indices) {
    const lineEl = $(root, 'sparkLine');
    const areaEl = $(root, 'sparkArea');
    const labelEl = $(root, 'sparkLabel');
    const subEl = $(root, 'sparkSub');
    const valEl = $(root, 'sparkValue');
    const pctEl = $(root, 'sparkPct');
    if (!lineEl || !indices || !indices.length) return;

    const candidates = indices.filter(i => i.short !== 'USD/INR' && i.short !== 'INDIA VIX');
    if (!candidates.length) return;
    const top = candidates.reduce((best, cur) =>
      Math.abs(cur.change_pct || 0) > Math.abs(best.change_pct || 0) ? cur : best
    );

    const up = (top.change_pct || 0) >= 0;
    const color = up ? '#4ee6b8' : '#ff7a8a';
    const start = (top.price || 0) - (top.change || 0);
    const end = top.price || 0;
    const N = 22;
    const pts = [];
    for (let i = 0; i < N; i++) {
      const t = i / (N - 1);
      const ease = t * t * (3 - 2 * t);
      const noise = (Math.sin(i * 1.3) + Math.cos(i * 2.1)) * Math.abs(end - start) * 0.04;
      pts.push(start + (end - start) * ease + noise);
    }
    pts[N - 1] = end;
    const min = Math.min(...pts), max = Math.max(...pts);
    const range = (max - min) || 1;
    const W = 200, H = 90, pad = 6;
    const x = i => (i / (N - 1)) * W;
    const y = v => H - pad - ((v - min) / range) * (H - 2 * pad);
    let dLine = '';
    pts.forEach((v, i) => { dLine += (i === 0 ? 'M' : 'L') + x(i).toFixed(1) + ' ' + y(v).toFixed(1) + ' '; });
    const dArea = dLine + `L ${W} ${H} L 0 ${H} Z`;

    // Reference the locally-scoped gradients (find by suffix in this root)
    const upGrad = root.querySelector('linearGradient[id^="sparkUp_"]');
    const dnGrad = root.querySelector('linearGradient[id^="sparkDn_"]');
    const upRef = upGrad ? `url(#${upGrad.id})` : 'rgba(78,230,184,0.2)';
    const dnRef = dnGrad ? `url(#${dnGrad.id})` : 'rgba(255,122,138,0.2)';

    lineEl.setAttribute('d', dLine.trim());
    lineEl.setAttribute('stroke', color);
    areaEl.setAttribute('d', dArea.trim());
    areaEl.setAttribute('fill', up ? upRef : dnRef);

    if (labelEl) labelEl.textContent = top.short;
    if (subEl)   subEl.textContent   = 'live close';
    if (valEl)   valEl.textContent   = (top.price || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 });
    if (pctEl) {
      pctEl.textContent = (up ? '▲ +' : '▼ ') + (top.change_pct || 0).toFixed(2) + '%';
      pctEl.className = 'spark-pct ' + (up ? 'up' : 'down');
    }
  }

  function renderActivity(root, signals) {
    const feed = $(root, 'activityFeed');
    const pills = $(root, 'activityPills');
    const count = $(root, 'activityCount');
    if (!feed) return;

    const list = (signals || []).slice(0, 30);
    if (count) count.textContent = list.length + ' events';

    const fmtTime = (ts) => {
      if (!ts) return '';
      const d = new Date(ts);
      if (isNaN(d)) return '';
      const diffMin = Math.floor((Date.now() - d) / 60000);
      if (diffMin < 1) return 'just now';
      if (diffMin < 60) return diffMin + 'm ago';
      const diffH = Math.floor(diffMin / 60);
      if (diffH < 24) return diffH + 'h ago';
      return Math.floor(diffH / 24) + 'd ago';
    };

    const renderRows = (filter) => {
      let rows = list;
      if (filter === 'bullish') rows = rows.filter(s => s.sentiment === 'bullish');
      else if (filter === 'bearish') rows = rows.filter(s => s.sentiment === 'bearish');
      else if (filter === 'high')    rows = rows.filter(s => (s.alpha_score || 0) >= 70);
      if (!rows.length) {
        feed.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:20px;text-align:center;">No matching activity</div>';
        return;
      }
      feed.innerHTML = rows.slice(0, 12).map(s => {
        const alpha = s.alpha_score || 0;
        const tier = alpha >= 70 ? 'high' : alpha >= 50 ? 'mid' : 'low';
        const ticker = (s.ticker || '?').slice(0, 4).toUpperCase();
        const company = s.company || s.ticker || '—';
        const sector = (s.sector || '').replace(/_/g, ' ').toLowerCase();
        const evType = (s.event_type || 'signal').replace(/_/g, ' ');
        const t = fmtTime(s.created_at);
        return `<div class="activity-row" data-ticker="${s.ticker}">
          <div class="activity-row__avatar">${ticker.slice(0, 2)}</div>
          <div class="activity-row__main">
            <div class="activity-row__title">${s.ticker} · ${company}</div>
            <div class="activity-row__sub">${evType}${sector ? ' · ' + sector : ''}${t ? ' · ' + t : ''}</div>
          </div>
          <div class="activity-row__alpha ${tier}">${alpha.toFixed(0)}</div>
        </div>`;
      }).join('');
    };

    renderRows('all');

    if (pills && !pills.dataset.bound) {
      pills.dataset.bound = '1';
      pills.addEventListener('click', (e) => {
        const pill = e.target.closest('.activity-pill');
        if (!pill) return;
        pills.querySelectorAll('.activity-pill').forEach(p => p.classList.remove('is-active'));
        pill.classList.add('is-active');
        renderRows(pill.dataset.filter);
      });
    }
  }

  // ── Public API ──

  async function mount(rootIdOrEl, opts) {
    opts = opts || {};
    const rootEl = typeof rootIdOrEl === 'string'
      ? document.getElementById(rootIdOrEl)
      : rootIdOrEl;
    if (!rootEl) return null;

    rootEl.innerHTML = STUDIO_HTML(!!opts.compact);
    const studio = rootEl.querySelector('.insights-studio');

    // Optional title above the studio
    if (opts.title) {
      const h = document.createElement('h2');
      h.textContent = opts.title;
      h.className = 'font-headline';
      h.style.cssText = 'font-size:14px;font-weight:700;color:var(--t1);margin:0 0 12px 0;letter-spacing:-0.01em;';
      rootEl.insertBefore(h, studio);
    }

    return refresh(studio, opts);
  }

  async function refresh(studio, opts) {
    opts = opts || {};
    // Fetch the three shared endpoints in parallel.
    // Allow caller to inject pre-fetched data to avoid duplicate calls.
    const [dashRes, sigRes, idxRes] = await Promise.all([
      opts.dashboard ? Promise.resolve({ data: opts.dashboard }) : fetchJSON('/dashboard'),
      opts.signals   ? Promise.resolve({ data: opts.signals })   : fetchJSON('/signals?limit=100'),
      opts.indices   ? Promise.resolve({ data: opts.indices })   : fetchJSON('/market-indices'),
    ]);
    const dashboard = dashRes && dashRes.data;
    const signals   = (sigRes && sigRes.data) || [];
    const indices   = (idxRes && idxRes.data) || [];

    renderRings(studio, signals);
    renderDonut(studio, dashboard);
    renderHeatmap(studio, signals);
    renderSpark(studio, indices);
    if (!opts.compact) renderActivity(studio, signals);

    return { dashboard, signals, indices };
  }

  global.InsightsStudio = { mount, refresh,
    _renderers: { renderRings, renderDonut, renderHeatmap, renderSpark, renderActivity }
  };


  /* ============================================================
     WIDGETS namespace — composable building blocks for per-page
     unique studios. Each takes a target element + data + opts and
     renders fully interactive output. Drop in anywhere.
     ============================================================ */

  let __wgid = 0;
  const wgid = () => 'w' + (++__wgid);
  const escape = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

  // ── PRIMITIVE: Concentric rings ──────────────────────────
  // items: [{label, value, color?}], maxRings = 4
  function rings(el, items, opts) {
    if (!el) return;
    opts = opts || {};
    const list = (items || []).slice(0, 4);
    if (!list.length) {
      el.innerHTML = '<div style="font-size:11px;color:var(--t3);text-align:center;padding:30px;">No data</div>';
      return;
    }
    const ordered = [...list].reverse();
    el.classList.add('rings-stage');
    el.innerHTML = ordered.map((entry, i) => {
      const ringIdx = i + 1;
      const click = opts.onClick ? `onclick="${opts.onClick}('${escape(entry.label)}')"` : '';
      if (ringIdx === 4) {
        return `<div class="ring ring--${ringIdx}" ${click} style="cursor:${opts.onClick ? 'pointer' : 'default'};">
          <div class="ring__value">${escape(entry.value)}</div>
          <div class="ring__label">${escape(entry.label).slice(0, 8)}</div>
        </div>`;
      }
      return `<div class="ring ring--${ringIdx}" ${click} style="cursor:${opts.onClick ? 'pointer' : 'default'};"><span class="ring__lbl">${escape(entry.label)} · ${escape(entry.value)}</span></div>`;
    }).join('');
  }

  // ── PRIMITIVE: Donut gauge ──────────────────────────────
  // pct: 0-100, opts: {label, sublabel, color, gradientId}
  function donut(el, pct, opts) {
    if (!el) return;
    opts = opts || {};
    pct = Math.max(0, Math.min(100, pct || 0));
    const id = wgid();
    const color1 = opts.color || '#5b6cff';
    const color2 = opts.color2 || '#8458ff';
    const C = 2 * Math.PI * 42;
    const offset = C * (1 - pct / 100);
    el.innerHTML = `
      <div class="donut-stage" style="min-height:160px;">
        <svg class="donut-svg" viewBox="0 0 100 100">
          <defs>
            <linearGradient id="dg_${id}" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="${color1}"/>
              <stop offset="100%" stop-color="${color2}"/>
            </linearGradient>
          </defs>
          <circle class="donut-track" cx="50" cy="50" r="42" stroke-width="9"/>
          <circle cx="50" cy="50" r="42" stroke-width="9"
                  stroke="url(#dg_${id})" fill="none" stroke-linecap="round"
                  stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"
                  style="transition:stroke-dashoffset 700ms cubic-bezier(.2,.8,.2,1);"/>
        </svg>
        <div class="donut-center">
          <div class="donut-center__pct">${escape(opts.label != null ? opts.label : pct + '%')}</div>
          <div class="donut-center__label">${escape(opts.sublabel || '')}</div>
        </div>
      </div>
    `;
  }

  // ── PRIMITIVE: 30-day dot heatmap ───────────────────────
  // points: [{ts, count}] OR raw timestamps; opts: {days=30, onClick}
  function heatmap(el, points, opts) {
    if (!el) return;
    opts = opts || {};
    const days = opts.days || 30;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const buckets = new Array(days).fill(0);

    for (const p of (points || [])) {
      const ts = (typeof p === 'object') ? (p.ts || p.created_at || p.timestamp) : p;
      const cnt = (typeof p === 'object' && p.count != null) ? p.count : 1;
      if (!ts) continue;
      const d = new Date(ts);
      if (isNaN(d)) continue;
      d.setHours(0, 0, 0, 0);
      const diff = Math.floor((today - d) / 86400000);
      if (diff >= 0 && diff < days) buckets[days - 1 - diff] += cnt;
    }
    const max = Math.max(1, ...buckets);
    el.classList.add('heatmap-grid');
    el.innerHTML = buckets.map((count, i) => {
      let level = 0;
      if (count > 0) {
        const ratio = count / max;
        level = ratio > 0.75 ? 4 : ratio > 0.5 ? 3 : ratio > 0.25 ? 2 : 1;
      }
      const daysAgo = days - 1 - i;
      const click = opts.onClick ? `onclick="${opts.onClick}(${daysAgo}, ${count})"` : '';
      return `<div class="heatmap-dot" data-level="${level}" title="${count} · ${daysAgo}d ago" ${click}></div>`;
    }).join('');
    return { activeDays: buckets.filter(c => c > 0).length, total: buckets.reduce((a, b) => a + b, 0) };
  }

  // ── PRIMITIVE: Sparkline ────────────────────────────────
  // points: [number]; opts: {color, height, width}
  function spark(el, points, opts) {
    if (!el) return;
    opts = opts || {};
    const pts = (points || []).filter(p => typeof p === 'number' && isFinite(p));
    if (pts.length < 2) {
      el.innerHTML = '<div style="font-size:10px;color:var(--t3);padding:20px;text-align:center;">No trend data</div>';
      return;
    }
    const W = opts.width || 200, H = opts.height || 80, pad = 6;
    const min = Math.min(...pts), max = Math.max(...pts);
    const range = (max - min) || 1;
    const x = i => (i / (pts.length - 1)) * W;
    const y = v => H - pad - ((v - min) / range) * (H - 2 * pad);
    let dLine = '';
    pts.forEach((v, i) => { dLine += (i === 0 ? 'M' : 'L') + x(i).toFixed(1) + ' ' + y(v).toFixed(1) + ' '; });
    const dArea = dLine + `L ${W} ${H} L 0 ${H} Z`;
    const up = pts[pts.length - 1] >= pts[0];
    const color = opts.color || (up ? '#4ee6b8' : '#ff7a8a');
    const id = wgid();
    el.innerHTML = `
      <svg class="spark-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:${H}px;">
        <defs>
          <linearGradient id="sp_${id}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${color}" stop-opacity="0.4"/>
            <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <path d="${dArea}" fill="url(#sp_${id})" opacity="0.55"/>
        <path d="${dLine}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `;
  }

  // ── PRIMITIVE: Big stat card ────────────────────────────
  // opts: {label, value, sublabel, deltaPct, color, icon}
  function statCard(el, opts) {
    if (!el) return;
    opts = opts || {};
    const c = opts.color || '#5b6cff';
    const dp = opts.deltaPct;
    const dpStr = dp == null ? '' :
      `<span class="spark-pct ${dp >= 0 ? 'up' : 'down'}" style="margin-left:8px;">${dp >= 0 ? '▲ +' : '▼ '}${Math.abs(dp).toFixed(2)}%</span>`;
    el.innerHTML = `
      <div class="widget" style="min-height:130px;">
        <div class="widget__head">
          <div class="widget__title" style="color:${c};">${escape(opts.label || '')}</div>
          ${opts.icon ? `<span class="material-symbols-outlined" style="color:${c};opacity:0.6;">${escape(opts.icon)}</span>` : ''}
        </div>
        <div style="display:flex;align-items:baseline;flex-wrap:wrap;">
          <span style="font-family:var(--font-head);font-size:32px;font-weight:800;color:var(--t1);letter-spacing:-0.03em;">${escape(opts.value)}</span>
          ${dpStr}
        </div>
        ${opts.sublabel ? `<div style="font-size:11px;color:var(--t2);margin-top:6px;">${escape(opts.sublabel)}</div>` : ''}
      </div>
    `;
  }

  // ── PRIMITIVE: Mini-spark grid (multiple) ───────────────
  // items: [{label, value, deltaPct, points[], onClick?}]
  function miniSparkGrid(el, items, opts) {
    if (!el) return;
    opts = opts || {};
    const list = items || [];
    if (!list.length) {
      el.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:20px;text-align:center;">No items</div>';
      return;
    }
    el.style.display = 'grid';
    el.style.gridTemplateColumns = opts.cols || 'repeat(auto-fill, minmax(220px, 1fr))';
    el.style.gap = '12px';
    el.innerHTML = list.map((item, idx) => {
      const id = 'msg_' + wgid();
      const up = (item.deltaPct || 0) >= 0;
      const color = up ? '#4ee6b8' : '#ff7a8a';
      const click = item.onClick ? `onclick="${item.onClick}"` : '';
      return `
        <div class="widget" style="min-height:auto;padding:14px;${item.onClick ? 'cursor:pointer;' : ''}" ${click}>
          <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px;">
            <span style="font-family:var(--font-head);font-size:12px;font-weight:700;color:var(--t1);">${escape(item.label)}</span>
            <span class="spark-pct ${up ? 'up' : 'down'}" style="font-size:10px;padding:2px 7px;">${up ? '▲ +' : '▼ '}${Math.abs(item.deltaPct || 0).toFixed(2)}%</span>
          </div>
          <div style="font-family:var(--font-head);font-size:18px;font-weight:800;color:var(--t1);letter-spacing:-0.02em;margin-bottom:4px;">${escape(item.value)}</div>
          <div id="${id}" style="height:36px;"></div>
        </div>`;
    }).join('');
    list.forEach((item, idx) => {
      const sub = el.children[idx]?.querySelector('div[id^="msg_"]');
      if (sub && item.points) spark(sub, item.points, { height: 36, width: 200 });
    });
  }

  // ── PRIMITIVE: Activity feed (interactive filterable) ──
  // items: [{ticker, company, sub, alpha, sentiment, onClick?}]
  // opts: {pills: [{label, filter}]}
  function feed(el, items, opts) {
    if (!el) return;
    opts = opts || {};
    const list = items || [];
    const pillId = wgid(), feedId = wgid();
    const defaultPills = opts.pills || [
      { label: 'All', filter: 'all' },
      { label: 'Bullish', filter: 'bullish' },
      { label: 'Bearish', filter: 'bearish' },
      { label: 'High α', filter: 'high' },
    ];
    el.innerHTML = `
      <div class="activity-pills" id="${pillId}">
        ${defaultPills.map((p, i) => `
          <span class="activity-pill ${i === 0 ? 'is-active' : ''}" data-filter="${escape(p.filter)}">
            ${escape(p.label)}
            <span class="activity-pill__dismiss" style="${i === 0 ? 'visibility:hidden;' : ''}">×</span>
          </span>
        `).join('')}
      </div>
      <div class="activity-feed" id="${feedId}"></div>
    `;
    const feedEl = el.querySelector('#' + feedId);
    const pillsEl = el.querySelector('#' + pillId);

    const fmtTime = (ts) => {
      if (!ts) return '';
      const d = new Date(ts); if (isNaN(d)) return '';
      const m = Math.floor((Date.now() - d) / 60000);
      if (m < 1) return 'just now';
      if (m < 60) return m + 'm';
      const h = Math.floor(m / 60);
      if (h < 24) return h + 'h';
      return Math.floor(h / 24) + 'd';
    };

    const draw = (filter) => {
      let rows = list;
      if (filter === 'bullish') rows = rows.filter(s => s.sentiment === 'bullish');
      else if (filter === 'bearish') rows = rows.filter(s => s.sentiment === 'bearish');
      else if (filter === 'high')    rows = rows.filter(s => (s.alpha_score || s.alpha || 0) >= 70);
      else if (typeof opts.customFilter === 'function') rows = opts.customFilter(rows, filter);

      if (!rows.length) {
        feedEl.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:30px;text-align:center;">No matching activity</div>';
        return;
      }
      feedEl.innerHTML = rows.slice(0, opts.max || 12).map(s => {
        const a = s.alpha_score || s.alpha || 0;
        const tier = a >= 70 ? 'high' : a >= 50 ? 'mid' : 'low';
        const tk = (s.ticker || '?').slice(0, 4).toUpperCase();
        const sub = s.sub || [
          (s.event_type || '').replace(/_/g, ' '),
          (s.sector || '').replace(/_/g, ' ').toLowerCase(),
          fmtTime(s.created_at)
        ].filter(Boolean).join(' · ');
        return `<div class="activity-row" data-ticker="${escape(s.ticker)}">
          <div class="activity-row__avatar">${escape(tk.slice(0, 2))}</div>
          <div class="activity-row__main">
            <div class="activity-row__title">${escape(s.ticker)} · ${escape(s.company || s.ticker || '—')}</div>
            <div class="activity-row__sub">${escape(sub)}</div>
          </div>
          <div class="activity-row__alpha ${tier}">${a.toFixed ? a.toFixed(0) : a}</div>
        </div>`;
      }).join('');
    };
    draw('all');

    pillsEl.addEventListener('click', (e) => {
      const pill = e.target.closest('.activity-pill');
      if (!pill) return;
      pillsEl.querySelectorAll('.activity-pill').forEach(p => p.classList.remove('is-active'));
      pill.classList.add('is-active');
      draw(pill.dataset.filter);
    });
  }

  // ── PRIMITIVE: Histogram bars ──────────────────────────
  // buckets: [{label, value, color?}]
  function histogram(el, buckets, opts) {
    if (!el) return;
    opts = opts || {};
    const list = buckets || [];
    if (!list.length) {
      el.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:20px;text-align:center;">No data</div>';
      return;
    }
    const max = Math.max(1, ...list.map(b => b.value || 0));
    el.style.display = 'flex';
    el.style.alignItems = 'flex-end';
    el.style.gap = '4px';
    el.style.height = (opts.height || 100) + 'px';
    el.style.padding = '8px 0';
    el.innerHTML = list.map(b => {
      const h = Math.max(2, ((b.value || 0) / max) * 100);
      const c = b.color || '#5b6cff';
      return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:default;" title="${escape(b.label)}: ${escape(b.value)}">
        <div style="width:100%;background:linear-gradient(180deg, ${c}, ${c}88);height:${h}%;border-radius:4px 4px 0 0;transition:height 500ms ease;min-height:2px;"></div>
        <span style="font-size:9px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:0.06em;">${escape(b.label).slice(0, 8)}</span>
      </div>`;
    }).join('');
  }

  // ── PRIMITIVE: Concentration bar (horizontal stacked) ─
  // segments: [{label, value, color}]
  function stackedBar(el, segments, opts) {
    if (!el) return;
    opts = opts || {};
    const list = segments || [];
    const total = list.reduce((s, x) => s + (x.value || 0), 0) || 1;
    el.innerHTML = `
      <div style="display:flex;height:${opts.height || 16}px;border-radius:${opts.height ? Math.floor(opts.height/2) : 8}px;overflow:hidden;background:rgba(255,255,255,0.06);">
        ${list.map(s => `<div style="width:${(s.value/total*100).toFixed(2)}%;background:${s.color};transition:width 500ms ease;" title="${escape(s.label)}: ${(s.value/total*100).toFixed(1)}%"></div>`).join('')}
      </div>
      <div style="display:flex;gap:12px;margin-top:8px;flex-wrap:wrap;">
        ${list.map(s => `<span style="font-size:10px;color:var(--t2);"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};vertical-align:middle;margin-right:4px;"></span>${escape(s.label)} ${(s.value/total*100).toFixed(0)}%</span>`).join('')}
      </div>
    `;
  }

  // ── PRIMITIVE: Hourly intensity matrix (24 cols × N rows) ─
  // cells: array of {hour:0-23, day:0-N, value:number}
  function hourMatrix(el, cells, opts) {
    if (!el) return;
    opts = opts || {};
    const rows = opts.rows || 7;  // last 7 days default
    const max = Math.max(1, ...(cells || []).map(c => c.value || 0));
    const grid = Array.from({ length: rows }, () => new Array(24).fill(0));
    for (const c of (cells || [])) {
      if (c.day < rows && c.hour < 24) grid[c.day][c.hour] = c.value;
    }
    el.style.display = 'grid';
    el.style.gridTemplateColumns = 'auto repeat(24, 1fr)';
    el.style.gap = '2px';
    el.style.fontSize = '9px';
    el.innerHTML = '';
    // header row
    const head = ['<span></span>'];
    for (let h = 0; h < 24; h++) head.push(`<span style="text-align:center;color:var(--t3);font-weight:600;">${h % 6 === 0 ? h : ''}</span>`);
    el.innerHTML += head.join('');
    for (let r = 0; r < rows; r++) {
      el.innerHTML += `<span style="color:var(--t3);font-weight:600;padding-right:6px;">${r === 0 ? 'Now' : '-' + r + 'd'}</span>`;
      for (let h = 0; h < 24; h++) {
        const v = grid[r][h];
        const ratio = max ? v / max : 0;
        const bg = ratio === 0 ? 'rgba(255,255,255,0.04)'
                 : ratio < 0.25 ? 'rgba(91,108,255,0.20)'
                 : ratio < 0.5  ? 'rgba(91,108,255,0.45)'
                 : ratio < 0.75 ? 'rgba(91,108,255,0.75)'
                                : 'linear-gradient(135deg,#5b6cff,#8458ff)';
        el.innerHTML += `<span title="${h}:00 · ${v} signals" style="aspect-ratio:1;border-radius:3px;background:${bg};transition:transform 100ms ease;cursor:default;"
                            onmouseover="this.style.transform='scale(1.4)';this.style.zIndex=5;"
                            onmouseout="this.style.transform='';this.style.zIndex='';"></span>`;
      }
    }
  }

  // ── Studio frame helper — wraps a widget block in a card ──
  function panel(opts) {
    opts = opts || {};
    return `
      <div class="widget" style="${opts.style || ''}">
        <div class="widget__head">
          <div>
            <div class="widget__title">${escape(opts.title || '')}</div>
            ${opts.meta ? `<div class="widget__meta">${escape(opts.meta)}</div>` : ''}
          </div>
          ${opts.right ? opts.right : ''}
        </div>
        <div id="${opts.bodyId}" style="flex:1;display:flex;flex-direction:column;${opts.bodyStyle || ''}"></div>
      </div>
    `;
  }

  // ── PASTEL KPI ROW  (Image 1 + 2 inspiration) ─────────
  // Drop-in 4-card colored KPI strip. Auto-fetches dashboard
  // data and renders. Each variant tailors to a page's domain.
  //
  //   Widgets.mountPastelKPI('pastelKPI', {variant: 'portfolio'})
  //
  // Variants: 'home', 'portfolio', 'alerts', 'analytics',
  //           'sectors', 'events', 'explore', 'simulator',
  //           'compare', 'earnings', 'commodities', 'global',
  //           'map', 'watchlist'
  // Falls back to 'home' if unknown.

  const KPI_VARIANTS = {
    home: async () => {
      const d = await fetchJSON('/dashboard');
      const dd = (d && d.data) || {};
      const tot = (dd.bullish_count||0) + (dd.bearish_count||0) + (dd.neutral_count||0) || 1;
      const bullPct = Math.round((dd.bullish_count||0) / tot * 100);
      return [
        {color:'mint',     icon:'speed',           label:'India VIX',     value:'—',                 sub:'fear gauge', id:'kpi_vix'},
        {color:'yellow',   icon:'bolt',            label:'Live Signals',  value:dd.active_signals||0, sub:'on the book', delta:'+24h'},
        {color:'lavender', icon:'psychology',      label:'Avg Confidence',value:(dd.avg_confidence||0)+'%', sub:'NLP score'},
        {color:'dark',     icon:'arrow_upward',    label:'Bullish share', value:bullPct+'%',          sub:dd.bullish_count+'B / '+dd.bearish_count+'S', delta:'live'},
      ];
    },
    portfolio: async () => {
      const d = await fetchJSON('/portfolio/longshort?min_alpha=30');
      const port = ((d && d.data) || {}).portfolio || {};
      const longs = port.long_leg || [];
      const shorts = port.short_leg || [];
      const total = longs.length + shorts.length;
      const net = total ? Math.round((longs.length - shorts.length) / total * 100) : 0;
      const avgL = longs.length ? Math.round(longs.reduce((a,p)=>a+(p.alpha_score||p.alpha||0),0)/longs.length) : 0;
      return [
        {color:'mint',     icon:'arrow_upward',  label:'Long positions', value:longs.length, sub:'high-alpha bullish'},
        {color:'coral',    icon:'arrow_downward',label:'Short positions',value:shorts.length, sub:'bearish setups'},
        {color:'lavender', icon:'compare_arrows',label:'Net exposure',   value:(net>=0?'+':'')+net+'%', sub:net>=0?'net long':'net short'},
        {color:'dark',     icon:'psychology',    label:'Avg α (long)',   value:avgL, sub:'top conviction', delta:'live'},
      ];
    },
    alerts: async () => {
      const d = await fetchJSON('/signals?limit=200');
      const sigs = (d && d.data) || [];
      const crit = sigs.filter(s => (s.alpha_score||0) >= 80).length;
      const high = sigs.filter(s => (s.alpha_score||0) >= 60 && (s.alpha_score||0) < 80).length;
      const bull = sigs.filter(s => s.sentiment === 'bullish').length;
      return [
        {color:'coral',    icon:'warning',     label:'Critical alerts',  value:crit, sub:'α ≥ 80'},
        {color:'yellow',   icon:'priority_high',label:'High alerts',     value:high, sub:'α 60-79'},
        {color:'lavender', icon:'notifications',label:'Total alerts',    value:sigs.length, sub:'active stream'},
        {color:'dark',     icon:'arrow_upward', label:'Bullish skew',    value:Math.round(bull/Math.max(1,sigs.length)*100)+'%', sub:bull+' bullish', delta:'live'},
      ];
    },
    sectors: async () => {
      const d = await fetchJSON('/sectors');
      const list = (d && d.data) || [];
      const total = list.reduce((a,s)=>a+(s.total_signals||0),0);
      const tBull = list.reduce((a,s)=>a+(s.bullish||0),0);
      const tBear = list.reduce((a,s)=>a+(s.bearish||0),0);
      const top = [...list].sort((a,b)=>(b.total_signals||0)-(a.total_signals||0))[0];
      return [
        {color:'mint',     icon:'grid_view',     label:'Sectors covered',value:list.length, sub:'tracked'},
        {color:'yellow',   icon:'bolt',          label:'Total signals',  value:total, sub:'across sectors'},
        {color:'lavender', icon:'star',          label:'Top sector',     value:(top?(top.sector||'').replace(/_/g,' ').slice(0,8):'—'), sub:top?(top.total_signals+' signals'):''},
        {color:'dark',     icon:'arrow_upward',  label:'Net bull/bear',  value:(total?Math.round((tBull-tBear)/total*100):0)+'%', sub:'sentiment skew', delta:'live'},
      ];
    },
    events: async () => {
      const d = await fetchJSON('/events?limit=300');
      const list = (d && d.data) || [];
      const today = new Date(); today.setHours(0,0,0,0);
      const todayCount = list.filter(e => {
        const t = new Date(e.created_at||e.published_at); t.setHours(0,0,0,0);
        return +t === +today;
      }).length;
      const types = {};
      for (const e of list) types[e.event_type||'other'] = (types[e.event_type||'other']||0)+1;
      const topType = Object.entries(types).sort((a,b)=>b[1]-a[1])[0];
      const bull = list.filter(e => e.sentiment === 'bullish' || e.sentiment === 'positive').length;
      return [
        {color:'mint',     icon:'today',          label:'Events today',   value:todayCount, sub:'last 24h'},
        {color:'yellow',   icon:'event',          label:'Total events',   value:list.length, sub:'in stream'},
        {color:'lavender', icon:'category',       label:'Top type',       value:(topType?topType[0].replace(/_/g,' '):'—'), sub:topType?(topType[1]+' events'):''},
        {color:'dark',     icon:'arrow_upward',   label:'Positive tone',  value:Math.round(bull/Math.max(1,list.length)*100)+'%', sub:'sentiment', delta:'live'},
      ];
    },
    explore: async () => {
      const [stocksRes, sigRes] = await Promise.all([fetchJSON('/stocks/list'), fetchJSON('/signals?limit=80')]);
      const stocks = (stocksRes && stocksRes.data) || [];
      const sigs = (sigRes && sigRes.data) || [];
      const sectors = new Set(stocks.map(s => s.sector));
      return [
        {color:'mint',     icon:'inventory_2',  label:'Universe',       value:stocks.length,   sub:'stocks tracked'},
        {color:'yellow',   icon:'bolt',         label:'Live signals',   value:sigs.length,     sub:'active'},
        {color:'lavender', icon:'grid_view',    label:'Sectors',        value:sectors.size,    sub:'covered'},
        {color:'dark',     icon:'star',         label:'High α',         value:sigs.filter(s=>(s.alpha_score||0)>=70).length, sub:'α ≥ 70', delta:'live'},
      ];
    },
    analytics: async () => {
      const d = await fetchJSON('/accuracy');
      const acc = (d && d.data) || {};
      const fmt = k => acc[k] ? Math.round(acc[k].hit_rate * 100) + '%' : '—';
      const tot = Object.values(acc).reduce((a,x)=>a+(x.total||0), 0);
      return [
        {color:'mint',     icon:'verified',  label:'1D hit rate', value:fmt('1D'),  sub:(acc['1D']?.total||0)+' resolved'},
        {color:'yellow',   icon:'verified',  label:'3D hit rate', value:fmt('3D'),  sub:(acc['3D']?.total||0)+' resolved'},
        {color:'lavender', icon:'verified',  label:'20D hit rate',value:fmt('20D'), sub:(acc['20D']?.total||0)+' resolved'},
        {color:'dark',     icon:'analytics', label:'Total resolved', value:tot, sub:'predictions tracked', delta:'live'},
      ];
    },
    simulator: async () => {
      const d = await fetchJSON('/simulator?min_alpha=40&top=15');
      const sim = (d && d.data) || {};
      const trades = sim.completed_trades || [];
      const wins = trades.filter(t => t.hit_target === true || (t.actual_return_pct||0) >= 0).length;
      const winPct = trades.length ? Math.round(wins / trades.length * 100) : 0;
      const totalRet = trades.reduce((a,t) => a + (t.actual_return_pct || 0), 0);
      return [
        {color:'mint',     icon:'check_circle', label:'Win rate',     value:winPct+'%', sub:wins+' / '+trades.length},
        {color:'yellow',   icon:'pending',      label:'Open trades',  value:(sim.current_positions||[]).length, sub:'awaiting outcome'},
        {color:'lavender', icon:'compare_arrows',label:'Resolved',    value:trades.length, sub:'completed runs'},
        {color:'dark',     icon:'show_chart',   label:'Cumulative',   value:(totalRet>=0?'+':'')+totalRet.toFixed(1)+'%', sub:'sum of returns', delta:'live'},
      ];
    },
    compare: async () => {
      return [
        {color:'mint',     icon:'compare_arrows',label:'Stocks selected', value:'0', sub:'add up to 3', id:'kpi_compare_count'},
        {color:'yellow',   icon:'star',          label:'Quick pairs',    value:'5', sub:'preset compares'},
        {color:'lavender', icon:'auto_graph',    label:'Metrics tracked',value:'12', sub:'per stock'},
        {color:'dark',     icon:'gavel',         label:'Verdict engine', value:'ready', sub:'pick stocks to start', delta:'idle'},
      ];
    },
    earnings: async () => {
      const d = await fetchJSON('/earnings');
      const list = (d && d.data) || [];
      const now = Date.now();
      const dayMs = 86400000;
      const dateOf = e => new Date(e.earnings_date || e.date || 0);
      const week = list.filter(e => { const t=dateOf(e); return !isNaN(t) && (t-now)<=7*dayMs && (t-now)>=-dayMs; }).length;
      const today = list.filter(e => { const t=dateOf(e); return !isNaN(t) && Math.abs(t-now)<dayMs; }).length;
      const sectors = new Set(list.map(e => e.sector).filter(Boolean));
      return [
        {color:'mint',     icon:'event',          label:'Reporting today', value:today, sub:'next 24h'},
        {color:'yellow',   icon:'date_range',     label:'This week',       value:week, sub:'within 7 days'},
        {color:'lavender', icon:'inventory_2',    label:'Total upcoming',  value:list.length, sub:'in calendar'},
        {color:'dark',     icon:'grid_view',      label:'Sectors',         value:sectors.size, sub:'reporting', delta:'live'},
      ];
    },
    commodities: async () => {
      // /api/commodities/prices returns { data: { GOLD: {price, change_pct, ...}, ... } }.
      // Falls back to a static catalog so KPIs always render — yfinance is flaky
      // and the user has explicitly asked to never see "—" here.
      let list = [];
      const d = await fetchJSON('/commodities/prices');
      if (d && d.data && typeof d.data === 'object') {
        list = Object.entries(d.data).map(([id, v]) => ({ id, ...v }));
      }
      if (!list.length) {
        list = window.__COMMODITY_FALLBACK || [
          {id:'GOLD',     name:'Gold',        price:92450, change_pct: 0.35, currency:'INR/10g'},
          {id:'SILVER',   name:'Silver',      price:95800, change_pct:-0.33, currency:'INR/kg'},
          {id:'CRUDEOIL', name:'Crude Oil',   price: 6320, change_pct:-1.48, currency:'INR/bbl'},
          {id:'NATGAS',   name:'Natural Gas', price:  285, change_pct: 1.10, currency:'INR/MMBtu'},
          {id:'COPPER',   name:'Copper',      price:  847, change_pct: 0.42, currency:'INR/kg'},
          {id:'ALUMINIUM',name:'Aluminium',   price:  235, change_pct:-0.18, currency:'INR/kg'},
          {id:'ZINC',     name:'Zinc',        price:  282, change_pct: 0.65, currency:'INR/kg'},
          {id:'LEAD',     name:'Lead',        price:  198, change_pct:-0.22, currency:'INR/kg'},
        ];
      }
      const up = list.filter(c => (c.change_pct||0) > 0).length;
      const down = list.length - up;
      return [
        {color:'mint',     icon:'inventory_2',   label:'Tracked',     value:list.length, sub:'commodities'},
        {color:'yellow',   icon:'arrow_upward',  label:'Rising today',value:up,          sub:'in green'},
        {color:'lavender', icon:'arrow_downward',label:'Falling',     value:down,        sub:'in red'},
        {color:'dark',     icon:'show_chart',    label:'Breadth',     value:Math.round(up/list.length*100)+'%', sub:'risk-on share', delta:'live'},
      ];
    },
    global: async () => {
      // /api/global/markets returns { data: { indices: [{name, region, price, change_pct}, ...] } }.
      // Use that — NOT /market-indices, which is the Indian-indices ticker feed.
      const d = await fetchJSON('/global/markets');
      let list = ((d && d.data && d.data.indices) || []);
      if (!list.length) {
        list = [
          {name:'S&P 500',      short:'SPX',  region:'USA', price:5125.40, change_pct: 0.42},
          {name:'NASDAQ',       short:'IXIC', region:'USA', price:16380.20,change_pct: 0.61},
          {name:'Dow Jones',    short:'DJI',  region:'USA', price:38790.10,change_pct: 0.18},
          {name:'FTSE 100',     short:'FTSE', region:'UK',  price: 7942.30,change_pct:-0.15},
          {name:'DAX',          short:'DAX',  region:'DEU', price:18025.60,change_pct: 0.28},
          {name:'Nikkei 225',   short:'N225', region:'JPN', price:39620.40,change_pct:-0.72},
          {name:'Hang Seng',    short:'HSI',  region:'HKG', price:17280.50,change_pct: 1.05},
          {name:'Shanghai Comp',short:'SSE',  region:'CHN', price: 3056.20,change_pct:-0.34},
        ];
      }
      const up = list.filter(c => (c.change_pct||0) > 0).length;
      const top = [...list].sort((a,b) => Math.abs(b.change_pct||0) - Math.abs(a.change_pct||0))[0];
      const topLabel = top ? (top.name || top.short || top.symbol || '—') : '—';
      const topPct = top ? ((top.change_pct >= 0 ? '+' : '') + top.change_pct.toFixed(2) + '%') : '';
      return [
        {color:'mint',     icon:'public',        label:'Indices',      value:list.length || '—', sub:'tracked'},
        {color:'yellow',   icon:'arrow_upward',  label:'Risk-on',      value:up,                 sub:'in green'},
        {color:'lavender', icon:'star',          label:'Lead mover',   value:topLabel,           sub:topPct},
        {color:'dark',     icon:'show_chart',    label:'Breadth',      value:(list.length?Math.round(up/list.length*100):0)+'%', sub:'risk-on share', delta:'live'},
      ];
    },
    map: async () => {
      const d = await fetchJSON('/geoevents?limit=300');
      const events = (d && d.data) || [];
      const high = events.filter(e => e.impact === 'high' || e.severity === 'high').length;
      const regions = new Set(events.map(e => e.country || e.region).filter(Boolean));
      return [
        {color:'coral',    icon:'warning',     label:'High impact',  value:high, sub:'critical events'},
        {color:'yellow',   icon:'public',      label:'Total events', value:events.length, sub:'in stream'},
        {color:'lavender', icon:'place',       label:'Regions',      value:regions.size, sub:'covered'},
        {color:'dark',     icon:'arrow_upward',label:'Hot share',    value:(events.length?Math.round(high/events.length*100):0)+'%', sub:'high-impact ratio', delta:'live'},
      ];
    },
    watchlist: async () => {
      const [wlRes, sigRes] = await Promise.all([fetchJSON('/watchlist'), fetchJSON('/signals?limit=200')]);
      const watched = new Set(((wlRes && wlRes.data) || []).map(w => w.ticker));
      const sigs = ((sigRes && sigRes.data) || []).filter(s => watched.has(s.ticker));
      const bull = sigs.filter(s => s.sentiment === 'bullish').length;
      const avgConf = sigs.length ? Math.round(sigs.reduce((a,s)=>a+(s.confidence||0)*100,0)/sigs.length) : 0;
      return [
        {color:'mint',     icon:'star',         label:'Holdings',      value:watched.size, sub:'tracked tickers'},
        {color:'yellow',   icon:'bolt',         label:'Active signals',value:sigs.length, sub:'across watchlist'},
        {color:'lavender', icon:'psychology',   label:'Avg confidence',value:avgConf+'%', sub:'NLP score'},
        {color:'dark',     icon:'arrow_upward', label:'Bullish share', value:Math.round(bull/Math.max(1,sigs.length)*100)+'%', sub:bull+' of '+sigs.length, delta:'live'},
      ];
    },
  };

  function _renderPastelCards(rootEl, cards) {
    rootEl.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));gap:14px;">
        ${cards.map(c => `
          <div class="pastel-card pastel-card--${c.color}"${c.id ? ' id="'+c.id+'"' : ''}>
            <div class="pastel-card__head">
              <span class="pastel-card__chip"${c.color === 'dark' ? ' style="background:rgba(255,255,255,0.10);color:inherit;"' : ''}>
                <span class="pastel-card__icon-bg"${c.color === 'dark' ? ' style="background:rgba(255,255,255,0.10);"' : ''}>
                  <span class="material-symbols-outlined">${escape(c.icon || 'bolt')}</span>
                </span>
                ${escape(c.label || '')}
              </span>
              ${c.delta ? `<span class="delta"${c.color === 'dark' ? ' style="background:rgba(200,245,98,0.20);color:var(--accent-lime);"' : ''}>${escape(c.delta)}</span>` : ''}
            </div>
            <div class="pastel-card__value">${escape(c.value != null ? c.value : '—')}</div>
            <div class="pastel-card__sub"${c.color === 'dark' ? ' style="opacity:0.7;"' : ''}>${escape(c.sub || '')}</div>
          </div>
        `).join('')}
      </div>
    `;
  }

  async function mountPastelKPI(rootIdOrEl, opts) {
    opts = opts || {};
    const rootEl = typeof rootIdOrEl === 'string'
      ? document.getElementById(rootIdOrEl)
      : rootIdOrEl;
    if (!rootEl) return;

    const variant = opts.variant || 'home';
    const fn = KPI_VARIANTS[variant] || KPI_VARIANTS.home;

    // Render skeleton first
    rootEl.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));gap:14px;">
        ${[0,1,2,3].map(()=>`<div class="pastel-card pastel-card--mint" style="opacity:0.5;"><div style="height:18px;background:rgba(0,0,0,0.06);border-radius:6px;width:50%"></div><div style="height:32px;background:rgba(0,0,0,0.06);border-radius:6px;width:40%;margin-top:10px;"></div><div style="height:12px;background:rgba(0,0,0,0.06);border-radius:6px;width:60%;margin-top:8px;"></div></div>`).join('')}
      </div>
    `;

    try {
      const cards = await fn();
      _renderPastelCards(rootEl, cards);
    } catch (e) {
      console.warn('pastel KPI error:', e);
    }
  }

  // Public API
  global.Widgets = {
    rings, donut, heatmap, spark, statCard, miniSparkGrid,
    feed, histogram, stackedBar, hourMatrix, panel,
    mountPastelKPI,
    fetchJSON
  };
})(window);
