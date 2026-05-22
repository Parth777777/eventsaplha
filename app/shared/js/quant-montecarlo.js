/* quant-montecarlo.js — Monte Carlo Simulator tab (Quant Playground).
 * THE headline visualisation tab. Brownian motion paths animate ONE STEP AT A TIME
 * via requestAnimationFrame so the user literally sees the GBM build up live.
 *
 * Charts (Chart.js):
 *   1. Animated path canvas — N paths drawing step-by-step
 *   2. Terminal-price distribution histogram (rendered when animation finishes)
 *   3. Percentile fan chart (5/50/95) over time
 */
(function () {
  'use strict';
  var initialised = false;

  document.addEventListener('qp:tab', function (e) {
    if (e.detail.tab !== 'montecarlo' || initialised) return;
    init();
    initialised = true;
  });

  function init() {
    var root = document.getElementById('qpMCRoot');
    if (!root) return;
    root.innerHTML = template();

    var state = {
      ticker: '',
      S0: 24500,
      mu: 0.12,       // annualised drift
      sigma: 0.16,    // annualised vol
      days: 30,
      paths: 200,
      target: null,
      barrier: null,
      speed: 'normal',   // 'fast' | 'normal' | 'slow'
      seed: null,        // null = random
    };

    var paths = null, animFrame = null, animStep = 0;
    var pathsCanvas = root.querySelector('#qpPathsCanvas');
    var histChart = null, fanChart = null;

    bindInputs(root, state, function () { /* parameter change — just update display, don't auto-run */ });

    root.querySelector('[data-run]').addEventListener('click', run);
    root.querySelector('[data-stop]').addEventListener('click', stop);
    root.querySelector('[data-prefill]').addEventListener('click', prefillFromTicker);

    root.querySelectorAll('[data-speed]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.speed = b.dataset.speed;
        root.querySelectorAll('[data-speed]').forEach(function (x) { x.classList.toggle('is-on', x.dataset.speed === state.speed); });
      });
    });

    root.querySelectorAll('[data-example]').forEach(function (b) {
      b.addEventListener('click', function () {
        var ex = EXAMPLES[b.dataset.example];
        if (!ex) return;
        Object.assign(state, ex.state);
        syncInputs(root, state);
        run();
      });
    });

    root.querySelectorAll('.qp-panel-head').forEach(function (h) {
      h.addEventListener('click', function () { h.parentElement.classList.toggle('is-open'); });
    });

    var katexIv = setInterval(function () {
      if (window.katex) { Glossary.renderAll(root); clearInterval(katexIv); }
    }, 100);

    function prefillFromTicker() {
      var tk = (root.querySelector('[data-ticker]').value || '').trim().toUpperCase();
      if (!tk) { if (window.QPDemo) QPDemo.toast('Enter a ticker (NIFTY, RELIANCE, TCS…)', 'info'); return; }
      var btn = root.querySelector('[data-prefill]');
      btn.textContent = 'Loading…'; btn.disabled = true;
      var apply = function (closes) {
        var rets = Quant.dailyReturns(closes);
        state.S0 = closes[closes.length - 1];
        state.sigma = Quant.annualisedVol(rets);
        state.mu = Quant.annualisedReturn(rets);
        state.ticker = tk;
        syncInputs(root, state);
      };
      var finish = function () { btn.textContent = 'Pull σ from history'; btn.disabled = false; };
      if (window.QPDemo) {
        QPDemo.safeFetch('/api/stock/' + encodeURIComponent(tk) + '/chart?period=180d', {
          label: tk + ' history',
          demo: function () { return { data: QPDemo.syntheticOHLC(tk, 180) }; },
        }).then(function (j) {
          finish();
          var rows = (j && j.data) || [];
          var closes = rows.map(function (d) { return d.close; }).filter(function (c) { return c > 0; });
          if (closes.length < 30) { QPDemo.toast('Not enough history for ' + tk, 'warn'); return; }
          apply(closes);
        });
      } else {
        fetch('/api/stock/' + encodeURIComponent(tk) + '/chart?period=180d', { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
          .then(function (j) {
            finish();
            if (!j || !Array.isArray(j.data)) return;
            var closes = j.data.map(function (d) { return d.close; }).filter(function (c) { return c > 0; });
            if (closes.length >= 30) apply(closes);
          });
      }
    }

    document.addEventListener('qp:random', function (e) {
      if (e.detail.tab !== 'montecarlo') return;
      var keys = Object.keys(EXAMPLES);
      var pick = keys[Math.floor(Math.random() * keys.length)];
      var btn = root.querySelector('[data-example="' + pick + '"]');
      if (btn) btn.click();
    });

    function run() {
      stop();
      animStep = 0;
      var T = state.days / 252;      // assume trading-day grid
      paths = Quant.gbmPaths({
        S0: state.S0, mu: state.mu, sigma: state.sigma,
        T: T, steps: state.days, paths: state.paths, seed: state.seed,
      });
      setOut(root, 'progress-fill', null, '0%');
      drawPaths(0);
      drawAnimated();
      // Stat outputs computed up-front from full paths
      var dist = Quant.terminalDistribution(paths);
      setOut(root, 'mean', '₹' + dist.mean.toFixed(2));
      setOut(root, 'p5',   '₹' + dist.p5.toFixed(2));
      setOut(root, 'p50',  '₹' + dist.p50.toFixed(2));
      setOut(root, 'p95',  '₹' + dist.p95.toFixed(2));
      var rng = ((dist.p95 - dist.p5) / state.S0 * 100).toFixed(1);
      setOut(root, 'range', '±' + rng + '%');

      if (state.target != null) {
        var dir = state.target >= state.S0 ? 'above' : 'below';
        var p = Quant.probTouch(paths, state.target, dir) * 100;
        setOut(root, 'pTouch', p.toFixed(1) + '%');
      } else {
        setOut(root, 'pTouch', '—');
      }
      // VaR (1-day, % of position)
      var terminalReturns = [];
      for (var i = 0; i < paths.__paths; i++) {
        terminalReturns.push(Quant.mcPath(paths, i, state.days) / state.S0 - 1);
      }
      var var95 = Quant.historicalVaR(terminalReturns, 0.95, 100000); // ₹100k position
      setOut(root, 'var95', '₹' + var95.toFixed(0));
    }

    function stop() {
      if (animFrame) cancelAnimationFrame(animFrame);
      animFrame = null;
    }

    function drawAnimated() {
      var stepsPerFrame = state.speed === 'fast' ? 6 : state.speed === 'slow' ? 1 : 3;
      animStep += stepsPerFrame;
      if (animStep > state.days) animStep = state.days;
      drawPaths(animStep);
      var pct = (animStep / state.days * 100).toFixed(0) + '%';
      var fill = root.querySelector('[data-out="progress-fill"]');
      if (fill) fill.style.width = pct;
      if (animStep < state.days) {
        animFrame = requestAnimationFrame(drawAnimated);
      } else {
        // Done — render histogram + fan
        drawHistogram(paths);
        drawFan(paths);
      }
    }

    function drawPaths(uptoStep) {
      var c = pathsCanvas;
      var ctx = c.getContext('2d');
      var dpr = window.devicePixelRatio || 1;
      var w = c.clientWidth, h = c.clientHeight;
      if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      // Padding
      var pad = { l: 50, r: 14, t: 14, b: 28 };
      var plotW = w - pad.l - pad.r;
      var plotH = h - pad.t - pad.b;

      // Compute y-range across all paths up to the drawn step (so axes lock once at the end)
      var yMin = Infinity, yMax = -Infinity;
      for (var i = 0; i < paths.__paths; i++) {
        for (var t = 0; t <= state.days; t++) {
          var s = Quant.mcPath(paths, i, t);
          if (s < yMin) yMin = s;
          if (s > yMax) yMax = s;
        }
      }
      var ySpan = (yMax - yMin) || 1;
      yMin -= ySpan * 0.05; yMax += ySpan * 0.05;
      var xMax = state.days;

      // Grid + axes
      ctx.strokeStyle = 'rgba(255,255,255,0.04)';
      ctx.lineWidth = 1;
      ctx.fillStyle = '#6a7484';
      ctx.font = '10px Geist Mono';
      // Horizontal grid lines + y labels
      for (var k = 0; k <= 4; k++) {
        var y = pad.t + (plotH * k / 4);
        var val = yMax - (yMax - yMin) * (k / 4);
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + plotW, y); ctx.stroke();
        ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
        ctx.fillText(val.toFixed(0), pad.l - 6, y);
      }
      // X labels
      for (var t2 = 0; t2 <= 4; t2++) {
        var x = pad.l + (plotW * t2 / 4);
        var day = Math.round(xMax * t2 / 4);
        ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.fillText('d' + day, x, pad.t + plotH + 6);
      }
      // Title text
      ctx.fillStyle = '#8a94a8'; ctx.font = '700 10px Plus Jakarta Sans';
      ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      ctx.fillText('GBM PATHS · ' + paths.__paths + ' simulations · step ' + uptoStep + '/' + xMax, pad.l, 0);

      // Draw paths
      function px(t) { return pad.l + (t / xMax) * plotW; }
      function py(s) { return pad.t + plotH - ((s - yMin) / (yMax - yMin)) * plotH; }

      var nPaths = paths.__paths;
      // Stroke individual paths with slight transparency so density reads as colour
      ctx.lineWidth = 0.9;
      for (var p = 0; p < nPaths; p++) {
        var lastS = Quant.mcPath(paths, p, uptoStep);
        var endRel = (lastS - state.S0) / state.S0;
        var color = endRel >= 0
          ? 'rgba(45,212,170,' + (0.06 + 0.30 * Math.min(1, Math.abs(endRel) * 4)) + ')'
          : 'rgba(242,107,107,' + (0.06 + 0.30 * Math.min(1, Math.abs(endRel) * 4)) + ')';
        ctx.strokeStyle = color;
        ctx.beginPath();
        ctx.moveTo(px(0), py(Quant.mcPath(paths, p, 0)));
        for (var t3 = 1; t3 <= uptoStep; t3++) {
          ctx.lineTo(px(t3), py(Quant.mcPath(paths, p, t3)));
        }
        ctx.stroke();
      }
      // Starting price horizontal line
      ctx.strokeStyle = 'rgba(255,255,255,0.20)';
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(pad.l, py(state.S0)); ctx.lineTo(pad.l + plotW, py(state.S0)); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#dde3ef'; ctx.font = '700 10px Geist Mono';
      ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
      ctx.fillText('S₀ ₹' + state.S0.toFixed(0), pad.l + 4, py(state.S0) - 2);

      // Target line if set
      if (state.target != null) {
        ctx.strokeStyle = '#ffc85a'; ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(pad.l, py(state.target)); ctx.lineTo(pad.l + plotW, py(state.target)); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#ffc85a';
        ctx.fillText('Target ₹' + state.target, pad.l + 4, py(state.target) - 2);
      }
    }

    function drawHistogram(paths) {
      var ctx = root.querySelector('#qpMCHist').getContext('2d');
      if (histChart) histChart.destroy();
      // Build bins from terminal values
      var dist = Quant.terminalDistribution(paths);
      var n = 30;
      var min = dist.min, max = dist.max;
      var width = (max - min) / n;
      var bins = new Array(n).fill(0);
      var labels = new Array(n);
      dist.values.forEach(function (v) {
        var idx = Math.min(n - 1, Math.floor((v - min) / width));
        bins[idx]++;
      });
      for (var i = 0; i < n; i++) labels[i] = (min + width * (i + 0.5)).toFixed(0);
      // Colour bars by side of starting price
      var colors = labels.map(function (l) {
        return parseFloat(l) >= state.S0 ? 'rgba(45,212,170,0.7)' : 'rgba(242,107,107,0.7)';
      });
      histChart = new Chart(ctx, {
        type: 'bar',
        data: { labels: labels, datasets: [{ label: 'Paths', data: bins, backgroundColor: colors, borderWidth: 0, borderRadius: 1 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 600 },
          plugins: { legend: { display: false }, tooltip: tooltipOpts() },
          scales: {
            x: {
              title: { display: true, text: 'Terminal price (₹)', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
              ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 9 }, maxTicksLimit: 10 },
              grid: { display: false },
            },
            y: { ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
        },
      });
    }

    function drawFan(paths) {
      var bands = Quant.pathBands(paths);
      var labels = []; for (var t = 0; t <= state.days; t++) labels.push(t);
      var ctx = root.querySelector('#qpMCFan').getContext('2d');
      if (fanChart) fanChart.destroy();
      fanChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            { label: '95th %ile', data: bands.p95, borderColor: 'rgba(45,212,170,0.5)', backgroundColor: 'rgba(45,212,170,0.10)', borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: '+1' },
            { label: 'Median',    data: bands.p50, borderColor: '#dde3ef', backgroundColor: 'transparent', borderWidth: 2, pointRadius: 0, tension: 0.2, fill: false },
            { label: '5th %ile',  data: bands.p5,  borderColor: 'rgba(242,107,107,0.5)', backgroundColor: 'rgba(242,107,107,0.10)', borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 500 },
          plugins: {
            legend: { position: 'top', labels: { color: '#b0bccf', font: { family: 'Geist Mono', size: 10 } } },
            tooltip: tooltipOpts(),
          },
          scales: {
            x: { title: { display: true, text: 'Day', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } }, ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
            y: { title: { display: true, text: 'Price (₹)', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } }, ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
        },
      });
    }
  }

  // ── Helpers ──
  function tooltipOpts() {
    return {
      backgroundColor: '#161d28', borderColor: 'rgba(255,255,255,0.10)', borderWidth: 1,
      titleColor: '#dde3ef', bodyColor: '#b0bccf', padding: 10,
      titleFont: { family: 'Geist Mono', size: 11 }, bodyFont: { family: 'Geist Mono', size: 11 },
    };
  }
  function setOut(root, key, val, htmlVal) {
    var el = root.querySelector('[data-out="' + key + '"]');
    if (el) {
      if (val !== null && val !== undefined) el.textContent = val;
      if (htmlVal) el.style.width = htmlVal;
    }
  }
  function bindInputs(root, state, onChange) {
    var defs = { S0: false, mu: true, sigma: true, days: false, paths: false, target: false, barrier: false };
    Object.keys(defs).forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]');
      if (!i) return;
      var asPct = defs[k];
      i.value = asPct ? (state[k] * 100).toFixed(2) : (state[k] != null ? state[k] : '');
      i.addEventListener('input', function () {
        var v = parseFloat(i.value);
        if (i.value === '') { state[k] = null; }
        else if (!isNaN(v)) { state[k] = asPct ? v / 100 : v; }
        onChange();
      });
    });
  }
  function syncInputs(root, state) {
    Object.keys({S0:0,mu:0,sigma:0,days:0,paths:0,target:0,barrier:0}).forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]');
      if (!i) return;
      var asPct = (k === 'mu' || k === 'sigma');
      i.value = state[k] == null ? '' : asPct ? (state[k] * 100).toFixed(2) : state[k];
    });
  }

  // ── Examples ──
  var EXAMPLES = {
    nifty30: {
      label: 'NIFTY in 30 days — what\'s my range?',
      desc: 'Simulate 200 paths over a month using realistic σ. Watch the fan widen as the random walk compounds.',
      state: { S0: 24500, mu: 0.12, sigma: 0.14, days: 30, paths: 200, target: 25500, speed: 'normal', seed: null },
    },
    rel_target: {
      label: 'RELIANCE — probability of hitting ₹3000 in 60 days',
      desc: 'Target-barrier touch probability over 60 days from current spot.',
      state: { S0: 2850, mu: 0.10, sigma: 0.22, days: 60, paths: 300, target: 3000, speed: 'fast', seed: null },
    },
    eventVol: {
      label: 'High-vol day before earnings',
      desc: 'σ jacked to 35%. Notice the fan expand — vol crushes returns asymmetrically.',
      state: { S0: 1500, mu: 0.10, sigma: 0.35, days: 14, paths: 200, target: 1600, speed: 'normal', seed: null },
    },
  };

  function template() {
    return ''
      + '<div class="qp-grid">'
      + '  <div class="qp-card">'
      + '    <div class="qp-card-title">Setup</div>'

      + '    <div class="qp-field"><div class="qp-label">Spot (S₀)</div><input class="qp-input" type="number" step="0.01" data-field="S0" value="24500"></div>'
      + '    <div class="qp-field"><div class="qp-label">Annual drift (μ %)</div><input class="qp-input" type="number" step="0.5" data-field="mu" value="12.00"></div>'
      + '    <div class="qp-field"><div class="qp-label">Annual σ (%)</div><input class="qp-input" type="number" step="0.5" data-field="sigma" value="16.00"></div>'
      + '    <div class="qp-field"><div class="qp-label">Days ahead</div><input class="qp-input" type="number" step="1" min="1" max="365" data-field="days" value="30"></div>'
      + '    <div class="qp-field"><div class="qp-label">Paths</div><input class="qp-input" type="number" step="50" min="10" max="2000" data-field="paths" value="200"></div>'
      + '    <div class="qp-field"><div class="qp-label">Target price (₹) <span style="opacity:0.6;font-weight:400;text-transform:none;">optional</span></div><input class="qp-input" type="number" step="1" data-field="target" placeholder="e.g. 25500"></div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label">Animation speed</div>'
      + '      <div class="qp-radio-group">'
      + '        <button type="button" class="qp-radio" data-speed="slow">Slow</button>'
      + '        <button type="button" class="qp-radio is-on" data-speed="normal">Normal</button>'
      + '        <button type="button" class="qp-radio" data-speed="fast">Fast</button>'
      + '      </div>'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label">Pull σ from ticker</div>'
      + '      <div style="display:flex;gap:6px;">'
      + '        <input class="qp-input" type="text" data-ticker placeholder="NIFTY / RELIANCE" style="flex:1;">'
      + '        <button class="qp-btn ghost" data-prefill>Pull</button>'
      + '      </div>'
      + '    </div>'

      + '    <div style="display:flex;gap:6px;margin-top:8px;">'
      + '      <button class="qp-btn" data-run style="flex:1;">▶ Simulate</button>'
      + '      <button class="qp-btn ghost" data-stop>■ Stop</button>'
      + '    </div>'
      + '  </div>'

      + '  <div>'
      + '    <div class="qp-outputs">'
      + '      <div class="qp-out"><div class="qp-out-lbl">Median terminal</div><div class="qp-out-val" data-out="p50">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">5–95% range</div><div class="qp-out-val" style="font-size:14px;"><span data-out="p5">—</span> &nbsp;→&nbsp; <span data-out="p95">—</span></div><div class="qp-out-sub" data-out="range">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Mean terminal</div><div class="qp-out-val" data-out="mean">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">P(touch target)</div><div class="qp-out-val" style="color:#ffc85a;" data-out="pTouch">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">95% 1-day VaR <span style="font-size:8px;color:#6a7484;">per ₹1L pos</span></div><div class="qp-out-val bear" data-out="var95">—</div></div>'
      + '    </div>'

      + '    <div class="qp-chart">'
      + '      <div class="qp-chart-title">Brownian-motion paths (animated)'
      + '        <span class="legend">'
      + '          <span style="color:#6ddec0;">winners</span>'
      + '          <span style="color:#f0a3a3;">losers</span>'
      + '          <span style="color:#ffc85a;">target</span>'
      + '        </span>'
      + '      </div>'
      + '      <div style="height:360px;padding:8px;"><canvas id="qpPathsCanvas" style="width:100%;height:100%;display:block;"></canvas></div>'
      + '      <div class="qp-anim-controls">'
      + '        <div class="qp-progress"><div class="fill" data-out="progress-fill"></div></div>'
      + '      </div>'
      + '    </div>'

      + '    <div class="qp-grid" style="grid-template-columns:1fr 1fr;margin-top:14px;">'
      + '      <div class="qp-chart"><div class="qp-chart-title">Terminal-price distribution</div><div style="height:240px;padding:8px;"><canvas id="qpMCHist"></canvas></div></div>'
      + '      <div class="qp-chart"><div class="qp-chart-title">Percentile fan (5/50/95)</div><div style="height:240px;padding:8px;"><canvas id="qpMCFan"></canvas></div></div>'
      + '    </div>'
      + '  </div>'
      + '</div>'

      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Concept — Geometric Brownian Motion <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body">'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="dS = \\mu S \\, dt + \\sigma S \\, dW"></div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="S(t) = S_0 \\exp\\left[ \\left(\\mu - \\frac{\\sigma^2}{2}\\right) t + \\sigma W(t) \\right]"></div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\text{Discretised step:} \\quad S_{t+\\Delta t} = S_t \\cdot \\exp\\left[ \\left(\\mu - \\frac{\\sigma^2}{2}\\right) \\Delta t + \\sigma \\sqrt{\\Delta t} \\cdot Z \\right], \\quad Z \\sim \\mathcal{N}(0,1)"></div>'
      + '    <dl class="qp-concept-legend">'
      + '      <dt>S₀</dt><dd>Starting price</dd>'
      + '      <dt>μ</dt><dd>Annualised drift (expected return)</dd>'
      + '      <dt>σ</dt><dd>Annualised volatility</dd>'
      + '      <dt>dW</dt><dd>Wiener increment — N(0, dt)</dd>'
      + '      <dt>Z</dt><dd>Standard normal random draw</dd>'
      + '    </dl>'
      + '    <div class="qp-concept-intuition">GBM assumes log-returns are normally distributed and prices stay positive. Reality has <strong>fat tails</strong> — black-swan events happen 5–10× more often than GBM predicts. Treat the 5%/95% bands and VaR as <em>lower bounds</em> on tail risk, not guarantees.</div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\text{VaR}_\\alpha = -\\text{percentile}(R, 1-\\alpha) \\cdot \\text{position}"></div>'
      + '  </div>'
      + '</div>'

      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Examples <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body"><div class="qp-examples">'
      +     Object.keys(EXAMPLES).map(function (k) {
             var e = EXAMPLES[k];
             return '<button class="qp-example" data-example="' + k + '"><span class="arrow">▸</span><div><div class="ex-title">' + e.label + '</div><div class="ex-desc">' + e.desc + '</div></div></button>';
           }).join('')
      + '  </div></div>'
      + '</div>';
  }
})();
