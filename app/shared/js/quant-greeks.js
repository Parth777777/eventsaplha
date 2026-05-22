/* quant-greeks.js — Options Pricer + Greeks tab (Quant Playground).
 * Renders into #qpGreeksRoot the first time the "greeks" tab activates.
 *
 * Visualisations (Chart.js):
 *   1. Greek curves vs spot — multi-line delta/gamma/theta/vega/rho
 *   2. Price-vs-spot at multiple DTEs — shows time decay
 *   3. Moneyness gauge — ATM/ITM/OTM visual
 */
(function () {
  'use strict';
  var initialised = false;

  document.addEventListener('qp:tab', function (e) {
    if (e.detail.tab !== 'greeks' || initialised) return;
    init();
    initialised = true;
  });

  // Read deep-link params (from fo.html / stock.html "Open in Playground" buttons)
  function urlParams() {
    var p = new URLSearchParams(location.search);
    return {
      ticker: p.get('ticker') || '',
      S: parseFloat(p.get('S')) || null,
      K: parseFloat(p.get('K')) || null,
      DTE: parseInt(p.get('DTE'), 10) || null,
      sigma: parseFloat(p.get('sigma')) || null,
      type: (p.get('type') || '').toUpperCase() === 'PE' ? 'put' : 'call',
    };
  }

  function init() {
    var root = document.getElementById('qpGreeksRoot');
    if (!root) return;
    root.innerHTML = template();

    // ── State ──
    var pre = urlParams();
    var state = {
      S: pre.S || 24500,
      K: pre.K || 24500,
      DTE: pre.DTE || 7,
      sigma: pre.sigma || 0.14,
      r: 0.065,
      q: 0,
      type: pre.type,
      marketPrice: null,    // for IV solver mode
      mode: 'price',        // 'price' (compute price from σ) or 'iv' (solve σ from price)
      ticker: pre.ticker,
    };

    // ── Wire inputs ──
    var f = bindFields(root, state, recompute);

    // Mode toggle
    root.querySelectorAll('[data-mode]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.mode = b.dataset.mode;
        root.querySelectorAll('[data-mode]').forEach(function (x) {
          x.classList.toggle('is-on', x.dataset.mode === state.mode);
        });
        root.querySelector('[data-mode-iv-only]').style.display = state.mode === 'iv' ? 'block' : 'none';
        root.querySelector('[data-mode-price-only]').style.display = state.mode === 'price' ? 'block' : 'none';
        recompute();
      });
    });

    // Call/Put toggle
    root.querySelectorAll('[data-cp]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.type = b.dataset.cp;
        root.querySelectorAll('[data-cp]').forEach(function (x) {
          x.classList.toggle('is-on', x.dataset.cp === state.type);
        });
        recompute();
      });
    });

    // NSE prefill — graceful demo fallback when live API is down
    var prefillBtn = root.querySelector('[data-prefill]');
    if (prefillBtn) prefillBtn.addEventListener('click', function () {
      var tk = (root.querySelector('[data-ticker]').value || '').trim().toUpperCase();
      if (!tk) {
        if (window.QPDemo) QPDemo.toast('Try entering NIFTY, RELIANCE, HDFCBANK… (or just hit an example below ↓)', 'info');
        return;
      }
      prefillBtn.textContent = 'Loading…';
      prefillBtn.disabled = true;
      var done = function (d, isDemo) {
        prefillBtn.textContent = 'Pull live from NSE';
        prefillBtn.disabled = false;
        if (!d) return;
        var spot = d.spot || d.last_price;
        if (spot) { state.S = spot; state.K = Math.round(spot / 50) * 50; }
        var iv = d.atm_iv || d.iv;
        if (iv) state.sigma = iv > 1 ? iv / 100 : iv;
        state.ticker = tk;
        syncInputs(root, state);
        recompute();
      };
      if (window.QPDemo) {
        QPDemo.safeFetch('/api/fo/ticker/' + encodeURIComponent(tk), {
          label: tk,
          demo: function () { return { success: true, data: QPDemo.DEMO_TICKER[tk] || { spot: 1000, atm_iv: 0.20 } }; },
        }).then(function (j) { done(j && (j.data || j)); });
      } else {
        fetch('/api/fo/ticker/' + encodeURIComponent(tk), { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
          .then(function (j) { done(j && j.data); });
      }
    });

    // Surprise-me: pick a random example when this tab is active
    document.addEventListener('qp:random', function (e) {
      if (e.detail.tab !== 'greeks') return;
      var keys = Object.keys(EXAMPLES);
      var pick = keys[Math.floor(Math.random() * keys.length)];
      var btn = root.querySelector('[data-example="' + pick + '"]');
      if (btn) btn.click();
    });

    // Examples
    root.querySelectorAll('[data-example]').forEach(function (b) {
      b.addEventListener('click', function () {
        var ex = EXAMPLES[b.dataset.example];
        if (!ex) return;
        Object.assign(state, ex.state);
        syncInputs(root, state);
        recompute();
        scrollToOutputs(root);
      });
    });

    // Collapsibles
    root.querySelectorAll('.qp-panel-head').forEach(function (h) {
      h.addEventListener('click', function () { h.parentElement.classList.toggle('is-open'); });
    });

    // Render formulas (deferred until KaTeX loads)
    var formulasReady = false;
    function renderFormulas() {
      if (formulasReady || !window.katex) return;
      formulasReady = true;
      if (window.Glossary) Glossary.renderAll(root);
    }
    var iv = setInterval(function () {
      if (window.katex) { renderFormulas(); clearInterval(iv); }
    }, 100);

    // Tooltips on labels
    if (window.Glossary) {
      [
        ['lbl-S', 'spot'], ['lbl-K', 'strike'], ['lbl-DTE', 'dte'],
        ['lbl-sigma', 'iv'], ['lbl-r', 'rho'], ['lbl-q', 'spot'],
        ['lbl-delta', 'delta'], ['lbl-gamma', 'gamma'], ['lbl-theta', 'theta'],
        ['lbl-vega', 'vega'], ['lbl-rho', 'rho'], ['lbl-price', 'premium'],
      ].forEach(function (p) {
        var el = root.querySelector('[data-tip="' + p[0] + '"]');
        if (el) Glossary.tooltip(el, p[1]);
      });
    }

    // Chart instances
    var greekChart = null;
    var decayChart = null;

    function recompute() {
      var p = { S: +state.S, K: +state.K, T: +state.DTE / 365, r: +state.r, sigma: +state.sigma, q: +state.q, type: state.type };

      // If IV mode, solve σ first
      if (state.mode === 'iv' && state.marketPrice > 0) {
        var solved = Quant.bsImpliedVol({ S: p.S, K: p.K, T: p.T, r: p.r, marketPrice: +state.marketPrice, q: p.q, type: state.type });
        if (solved && isFinite(solved)) {
          state.sigma = solved;
          p.sigma = solved;
          var sigInput = root.querySelector('[data-field="sigma"]');
          if (sigInput) sigInput.value = (solved * 100).toFixed(2);
        }
      }

      var price = Quant.bsPrice(p);
      var g = Quant.bsGreeks(p);

      // Moneyness label
      var m;
      if (state.type === 'call') {
        m = p.S > p.K * 1.02 ? 'ITM' : p.S < p.K * 0.98 ? 'OTM' : 'ATM';
      } else {
        m = p.S < p.K * 0.98 ? 'ITM' : p.S > p.K * 1.02 ? 'OTM' : 'ATM';
      }

      // Outputs
      setOut(root, 'price', '₹' + price.toFixed(2));
      setOut(root, 'delta', g.delta.toFixed(4));
      setOut(root, 'gamma', g.gamma.toFixed(5));
      setOut(root, 'theta', (g.theta < 0 ? '−' : '') + '₹' + Math.abs(g.theta).toFixed(2));
      setOut(root, 'vega',  '₹' + g.vega.toFixed(2));
      setOut(root, 'rho',   '₹' + g.rho.toFixed(2));
      setOut(root, 'moneyness', m);

      // Intrinsic / extrinsic breakdown
      var intrinsic = state.type === 'call' ? Math.max(p.S - p.K, 0) : Math.max(p.K - p.S, 0);
      var extrinsic = Math.max(price - intrinsic, 0);
      setOut(root, 'intrinsic', '₹' + intrinsic.toFixed(2));
      setOut(root, 'extrinsic', '₹' + extrinsic.toFixed(2));

      // Charts
      drawGreekChart(p);
      drawDecayChart(p);
    }

    function drawGreekChart(p) {
      var spots = [], delta = [], gamma = [], theta = [], vega = [], rho = [];
      var range = 0.20; // ±20% around spot
      var n = 80;
      for (var i = 0; i <= n; i++) {
        var s = p.S * (1 - range) + (p.S * 2 * range) * (i / n);
        spots.push(+s.toFixed(2));
        var gg = Quant.bsGreeks({ S: s, K: p.K, T: p.T, r: p.r, sigma: p.sigma, q: p.q, type: p.type });
        delta.push(+gg.delta.toFixed(5));
        gamma.push(+(gg.gamma * 100).toFixed(5));   // ×100 for readable scale
        theta.push(+gg.theta.toFixed(3));
        vega.push(+gg.vega.toFixed(3));
        rho.push(+gg.rho.toFixed(3));
      }
      var ctx = root.querySelector('#qpGreekChart').getContext('2d');
      if (greekChart) greekChart.destroy();
      greekChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: spots,
          datasets: [
            ds('Δ Delta',   delta, '#6ddec0', 'y'),
            ds('Γ Gamma×100', gamma, '#adc6ff', 'y'),
            ds('Θ Theta/day', theta, '#f0a3a3', 'y2'),
            ds('ν Vega/%',  vega,  '#ffc85a', 'y2'),
            ds('ρ Rho/%',   rho,   '#b0bccf', 'y2'),
          ],
        },
        options: chartOpts({ xTitle: 'Spot price (₹)', yLeft: 'Delta / Gamma×100', yRight: 'Theta / Vega / Rho (₹)' }),
      });
    }

    function drawDecayChart(p) {
      var dtes = [60, 30, 14, 7, 3, 1];
      var spots = [], series = dtes.map(function () { return []; });
      var range = 0.15;
      var n = 60;
      for (var i = 0; i <= n; i++) {
        var s = p.S * (1 - range) + (p.S * 2 * range) * (i / n);
        spots.push(+s.toFixed(2));
        dtes.forEach(function (d, idx) {
          var pr = Quant.bsPrice({ S: s, K: p.K, T: d / 365, r: p.r, sigma: p.sigma, q: p.q, type: p.type });
          series[idx].push(+pr.toFixed(2));
        });
      }
      var colors = ['#3a6ea8', '#5683b8', '#7299c8', '#8eb4e0', '#aeccea', '#cee0f4'];
      var ctx = root.querySelector('#qpDecayChart').getContext('2d');
      if (decayChart) decayChart.destroy();
      decayChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: spots,
          datasets: dtes.map(function (d, i) {
            return {
              label: d + 'd DTE',
              data: series[i],
              borderColor: colors[i],
              backgroundColor: 'transparent',
              borderWidth: i === 3 ? 2.5 : 1.4,
              pointRadius: 0,
              tension: 0.2,
            };
          }),
        },
        options: chartOpts({ xTitle: 'Spot price (₹)', yLeft: 'Option price (₹)', oneAxis: true }),
      });
    }
  }

  // ── Helpers ──
  function ds(label, data, color, axisID) {
    return {
      label: label, data: data,
      borderColor: color, backgroundColor: color + '22',
      borderWidth: 2, pointRadius: 0, tension: 0.25,
      yAxisID: axisID || 'y',
    };
  }
  function chartOpts(opts) {
    var o = {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 400, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top', align: 'end',
          labels: { color: '#b0bccf', boxWidth: 10, boxHeight: 10, padding: 12, font: { family: 'Geist Mono', size: 10 } },
        },
        tooltip: {
          backgroundColor: '#161d28', borderColor: 'rgba(255,255,255,0.10)', borderWidth: 1,
          titleColor: '#dde3ef', bodyColor: '#b0bccf', padding: 10,
          titleFont: { family: 'Geist Mono', size: 11 },
          bodyFont: { family: 'Geist Mono', size: 11 },
        },
      },
      scales: {
        x: {
          title: { display: !!opts.xTitle, text: opts.xTitle, color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
          ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 }, maxTicksLimit: 8 },
          grid: { color: 'rgba(255,255,255,0.04)' },
        },
        y: {
          position: 'left',
          title: { display: !!opts.yLeft, text: opts.yLeft, color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
          ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } },
          grid: { color: 'rgba(255,255,255,0.04)' },
        },
      },
    };
    if (!opts.oneAxis) {
      o.scales.y2 = {
        position: 'right',
        title: { display: !!opts.yRight, text: opts.yRight, color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
        ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } },
        grid: { drawOnChartArea: false },
      };
    }
    return o;
  }

  function setOut(root, key, val) {
    var el = root.querySelector('[data-out="' + key + '"]');
    if (el) el.textContent = val;
  }

  function bindFields(root, state, onChange) {
    var fields = ['S', 'K', 'DTE', 'sigma', 'r', 'q', 'marketPrice'];
    var inputs = {};
    fields.forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]');
      if (!i) return;
      // sigma & r & q are stored as decimal, shown as %
      var asPct = (k === 'sigma' || k === 'r' || k === 'q');
      i.value = asPct ? (state[k] * 100).toFixed(2) : state[k];
      i.addEventListener('input', function () {
        var v = parseFloat(i.value);
        if (isNaN(v)) return;
        state[k] = asPct ? v / 100 : v;
        onChange();
      });
      inputs[k] = i;
    });
    return inputs;
  }
  function syncInputs(root, state) {
    ['S', 'K', 'DTE', 'sigma', 'r', 'q', 'marketPrice'].forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]');
      if (!i) return;
      var asPct = (k === 'sigma' || k === 'r' || k === 'q');
      i.value = asPct ? (state[k] * 100).toFixed(2) : (state[k] != null ? state[k] : '');
    });
  }
  function scrollToOutputs(root) {
    var o = root.querySelector('.qp-outputs');
    if (o && o.scrollIntoView) o.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ── Worked examples (one-click load) ──
  var EXAMPLES = {
    atm7: {
      label: 'NIFTY ATM call, 7 DTE',
      desc: 'Standard weekly setup — shows fast theta decay near expiry.',
      state: { S: 24500, K: 24500, DTE: 7, sigma: 0.14, r: 0.065, q: 0, type: 'call', mode: 'price' },
    },
    otm: {
      label: 'Deep OTM lottery ticket',
      desc: 'Tiny delta, tiny premium — high gamma if it works.',
      state: { S: 24500, K: 26000, DTE: 14, sigma: 0.14, r: 0.065, q: 0, type: 'call', mode: 'price' },
    },
    eventVol: {
      label: 'Bank Nifty event-day put',
      desc: 'Inflated IV ahead of an event. Watch what happens when σ drops 30%.',
      state: { S: 52000, K: 52000, DTE: 1, sigma: 0.28, r: 0.065, q: 0, type: 'put', mode: 'price' },
    },
  };

  // ── HTML template ──
  function template() {
    return ''
      + '<div class="qp-grid">'
        // INPUTS
      + '  <div class="qp-card">'
      + '    <div class="qp-card-title">Inputs</div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label" data-tip="lbl-S">Spot (S)</div>'
      + '      <input class="qp-input" type="number" step="0.01" data-field="S">'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label" data-tip="lbl-K">Strike (K)</div>'
      + '      <input class="qp-input" type="number" step="0.01" data-field="K">'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label" data-tip="lbl-DTE">Days to expiry</div>'
      + '      <input class="qp-input" type="number" step="1" min="0" data-field="DTE">'
      + '    </div>'

      + '    <div class="qp-field" data-mode-price-only>'
      + '      <div class="qp-label" data-tip="lbl-sigma">Implied vol (σ, %)</div>'
      + '      <input class="qp-input" type="number" step="0.01" data-field="sigma">'
      + '    </div>'

      + '    <div class="qp-field" data-mode-iv-only style="display:none;">'
      + '      <div class="qp-label">Market price (₹)</div>'
      + '      <input class="qp-input" type="number" step="0.01" data-field="marketPrice" placeholder="e.g. 38">'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label" data-tip="lbl-r">Risk-free rate (r, %)</div>'
      + '      <input class="qp-input" type="number" step="0.01" data-field="r">'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label" data-tip="lbl-q">Dividend yield (q, %)</div>'
      + '      <input class="qp-input" type="number" step="0.01" data-field="q">'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label">Option type</div>'
      + '      <div class="qp-radio-group">'
      + '        <button type="button" class="qp-radio is-on" data-cp="call">Call</button>'
      + '        <button type="button" class="qp-radio" data-cp="put">Put</button>'
      + '      </div>'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label">Mode</div>'
      + '      <div class="qp-radio-group">'
      + '        <button type="button" class="qp-radio is-on" data-mode="price">Price from σ</button>'
      + '        <button type="button" class="qp-radio" data-mode="iv">Solve σ from price</button>'
      + '      </div>'
      + '    </div>'

      + '    <div class="qp-field">'
      + '      <div class="qp-label">Pull live (NSE)</div>'
      + '      <div style="display:flex;gap:6px;">'
      + '        <input class="qp-input" type="text" data-ticker placeholder="NIFTY / RELIANCE" style="flex:1;">'
      + '        <button class="qp-btn ghost" data-prefill>Pull</button>'
      + '      </div>'
      + '    </div>'

      + '  </div>'

        // OUTPUTS + CHARTS
      + '  <div>'
      + '    <div class="qp-outputs">'
      + outCell('price', 'Theoretical price', '', '')
      + outCell('moneyness', 'Moneyness', '', '')
      + outCell('intrinsic', 'Intrinsic', '', '')
      + outCell('extrinsic', 'Extrinsic (time value)', '', '')
      + '    </div>'

      + '    <div class="qp-outputs">'
      + outCell('delta', 'Delta (Δ)', 'delta', 'lbl-delta')
      + outCell('gamma', 'Gamma (Γ)', 'gamma', 'lbl-gamma')
      + outCell('theta', 'Theta (Θ/day)', 'theta', 'lbl-theta')
      + outCell('vega', 'Vega (ν / 1%)', 'vega', 'lbl-vega')
      + outCell('rho', 'Rho (ρ / 1%)', 'rho', 'lbl-rho')
      + '    </div>'

      + '    <div class="qp-chart">'
      + '      <div class="qp-chart-title">Greeks vs spot price'
      + '        <span class="legend">'
      + '          <span style="color:#6ddec0;">Δ</span>'
      + '          <span style="color:#adc6ff;">Γ×100</span>'
      + '          <span style="color:#f0a3a3;">Θ</span>'
      + '          <span style="color:#ffc85a;">ν</span>'
      + '          <span style="color:#b0bccf;">ρ</span>'
      + '        </span>'
      + '      </div>'
      + '      <div style="height:280px;padding:8px;"><canvas id="qpGreekChart"></canvas></div>'
      + '    </div>'

      + '    <div class="qp-chart" style="margin-top:14px;">'
      + '      <div class="qp-chart-title">Price decay — same option at different DTEs</div>'
      + '      <div style="height:260px;padding:8px;"><canvas id="qpDecayChart"></canvas></div>'
      + '    </div>'
      + '  </div>'
      + '</div>'

        // CONCEPT
      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Concept — Black-Scholes <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body">'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="C = S e^{-qT} N(d_1) - K e^{-rT} N(d_2), \\quad P = K e^{-rT} N(-d_2) - S e^{-qT} N(-d_1)"></div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="d_1 = \\frac{\\ln(S/K) + (r - q + \\sigma^2/2) T}{\\sigma \\sqrt{T}}, \\quad d_2 = d_1 - \\sigma\\sqrt{T}"></div>'
      + '    <dl class="qp-concept-legend">'
      + '      <dt>S</dt><dd>Spot price (₹)</dd>'
      + '      <dt>K</dt><dd>Strike price (₹)</dd>'
      + '      <dt>T</dt><dd>Time to expiry, in years (T = DTE/365)</dd>'
      + '      <dt>r</dt><dd>Risk-free rate (annual, decimal)</dd>'
      + '      <dt>q</dt><dd>Continuous dividend yield (annual, decimal)</dd>'
      + '      <dt>σ</dt><dd>Implied volatility (annual std-dev of log returns)</dd>'
      + '      <dt>N(·)</dt><dd>Cumulative standard normal distribution</dd>'
      + '    </dl>'
      + '    <div class="qp-concept-intuition">'
      + '      Black-Scholes prices an option as the expected discounted payoff under risk-neutral GBM. '
      + '      Think of <code>N(d₁)</code> as the option\'s hedge ratio (≈ delta), and <code>N(d₂)</code> as the risk-neutral probability of ending ITM. '
      + '      Treat the output as fair value — actual market prices differ by demand/supply, especially around events.'
      + '    </div>'
      + '    <div style="margin-top:14px;">'
      + '      <div class="qp-card-title" style="margin-bottom:8px;">Greek formulas</div>'
      + '      <div class="qp-concept-formula" data-katex-display data-katex="\\Delta_C = e^{-qT} N(d_1), \\quad \\Gamma = \\frac{e^{-qT}\\phi(d_1)}{S\\sigma\\sqrt{T}}, \\quad \\nu = S e^{-qT}\\phi(d_1)\\sqrt{T}"></div>'
      + '      <div class="qp-concept-formula" data-katex-display data-katex="\\Theta_C = -\\frac{S e^{-qT} \\phi(d_1) \\sigma}{2\\sqrt{T}} - r K e^{-rT} N(d_2) + q S e^{-qT} N(d_1)"></div>'
      + '    </div>'
      + '  </div>'
      + '</div>'

        // EXAMPLES
      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Examples — load with one click <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body">'
      + '    <div class="qp-examples">'
      +        Object.keys(EXAMPLES).map(function (k) {
                 var e = EXAMPLES[k];
                 return '<button class="qp-example" data-example="' + k + '">'
                      + '<span class="arrow">▸</span>'
                      + '<div><div class="ex-title">' + e.label + '</div><div class="ex-desc">' + e.desc + '</div></div>'
                      + '</button>';
               }).join('')
      + '    </div>'
      + '  </div>'
      + '</div>';
  }

  function outCell(key, label, modCls, tipKey) {
    return '<div class="qp-out">'
      + '<div class="qp-out-lbl"' + (tipKey ? ' data-tip="' + tipKey + '"' : '') + '>' + label + '</div>'
      + '<div class="qp-out-val ' + (modCls || '') + '" data-out="' + key + '">—</div>'
      + '</div>';
  }
})();
