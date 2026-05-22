/* quant-sizing.js — Position Sizing tab (Kelly + variants).
 * Renders into #qpSizingRoot the first time the "sizing" tab activates.
 *
 * Visualisations:
 *   1. Side-by-side bars — Kelly / Half-Kelly / Quarter / Fixed-Frac (Chart.js)
 *   2. Long-run equity simulation — 252 trades repeated for each fraction (Chart.js)
 */
(function () {
  'use strict';
  var initialised = false;

  document.addEventListener('qp:tab', function (e) {
    if (e.detail.tab !== 'sizing' || initialised) return;
    init();
    initialised = true;
  });

  function init() {
    var root = document.getElementById('qpSizingRoot');
    if (!root) return;
    root.innerHTML = template();

    var state = {
      mode: 'symmetric',
      winRate: 0.60, avgWin: 2000, avgLoss: 1000, capital: 100000,
      pWin: 0.75, maxWin: 1500, maxLoss: 4500,  // for asymmetric (defined-risk options)
    };

    var bars = null, equityChart = null;

    bindFields(root, state, recompute);

    root.querySelectorAll('[data-mode]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.mode = b.dataset.mode;
        root.querySelectorAll('[data-mode]').forEach(function (x) { x.classList.toggle('is-on', x.dataset.mode === state.mode); });
        root.querySelector('[data-sym]').style.display = state.mode === 'symmetric' ? 'block' : 'none';
        root.querySelector('[data-asym]').style.display = state.mode === 'asymmetric' ? 'block' : 'none';
        recompute();
      });
    });

    root.querySelectorAll('[data-example]').forEach(function (b) {
      b.addEventListener('click', function () {
        var ex = EXAMPLES[b.dataset.example];
        if (!ex) return;
        Object.assign(state, ex.state);
        syncInputs(root, state);
        recompute();
      });
    });

    root.querySelectorAll('.qp-panel-head').forEach(function (h) {
      h.addEventListener('click', function () { h.parentElement.classList.toggle('is-open'); });
    });

    if (window.Glossary) {
      [['lbl-winrate','kelly'], ['lbl-kelly','kelly'], ['lbl-half','half_kelly'], ['lbl-fixed','fixed_fractional']].forEach(function (p) {
        var el = root.querySelector('[data-tip="' + p[0] + '"]');
        if (el) Glossary.tooltip(el, p[1]);
      });
    }

    var katexIv = setInterval(function () {
      if (window.katex) { Glossary.renderAll(root); clearInterval(katexIv); }
    }, 100);

    document.addEventListener('qp:random', function (e) {
      if (e.detail.tab !== 'sizing') return;
      var keys = Object.keys(EXAMPLES);
      var pick = keys[Math.floor(Math.random() * keys.length)];
      var btn = root.querySelector('[data-example="' + pick + '"]');
      if (btn) btn.click();
    });

    function recompute() {
      var f, label;
      if (state.mode === 'symmetric') {
        f = Quant.kelly({ winRate: state.winRate, avgWin: state.avgWin, avgLoss: state.avgLoss });
        label = 'Symmetric Kelly';
      } else {
        f = Quant.kellyAsymmetric({ pWin: state.pWin, maxWin: state.maxWin, maxLoss: state.maxLoss });
        label = 'Asymmetric Kelly';
      }
      var half = f * 0.5, quarter = f * 0.25;
      var fixed = 0.02;  // 2% fixed-fractional convention

      setOut(root, 'kelly',   (f * 100).toFixed(1) + '%');
      setOut(root, 'half',    (half * 100).toFixed(1) + '%');
      setOut(root, 'quarter', (quarter * 100).toFixed(1) + '%');
      setOut(root, 'fixed',   (fixed * 100).toFixed(1) + '%');

      var rupees = function (frac) { return '₹' + Math.round(state.capital * frac).toLocaleString('en-IN'); };
      setOut(root, 'kellyR',   rupees(f));
      setOut(root, 'halfR',    rupees(half));
      setOut(root, 'quarterR', rupees(quarter));
      setOut(root, 'fixedR',   rupees(fixed));

      drawBars([f, half, quarter, fixed]);
      drawEquity([f, half, quarter, fixed]);
    }

    function drawBars(fractions) {
      var ctx = root.querySelector('#qpSizingBars').getContext('2d');
      if (bars) bars.destroy();
      bars = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: ['Full Kelly', 'Half-Kelly', 'Quarter Kelly', '2% Fixed'],
          datasets: [{
            label: 'Fraction of bankroll',
            data: fractions.map(function (f) { return +(f * 100).toFixed(2); }),
            backgroundColor: ['#6ddec0', '#adc6ff', '#ffc85a', '#b0bccf'],
            borderColor: ['#6ddec0', '#adc6ff', '#ffc85a', '#b0bccf'],
            borderWidth: 0,
            borderRadius: 4,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 600 },
          plugins: { legend: { display: false }, tooltip: tooltipOpts() },
          scales: {
            x: { ticks: { color: '#b0bccf', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } }, grid: { display: false } },
            y: {
              title: { display: true, text: '% of bankroll', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
              ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 }, callback: function (v) { return v + '%'; } },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
          },
        },
      });
    }

    function drawEquity(fractions) {
      // Simulate 252 trades for each fraction, 50 paths each, plot the median path.
      var trades = 252, paths = 50;
      var seriesData = fractions.map(function (f) { return simMedian(f, trades, paths); });
      var ctx = root.querySelector('#qpSizingEquity').getContext('2d');
      if (equityChart) equityChart.destroy();
      var colors = ['#6ddec0', '#adc6ff', '#ffc85a', '#b0bccf'];
      var labels = ['Full Kelly', 'Half-Kelly', 'Quarter', '2% Fixed'];
      var x = []; for (var t = 0; t <= trades; t++) x.push(t);
      equityChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: x,
          datasets: seriesData.map(function (d, i) {
            return {
              label: labels[i] + ' (median ×' + d[d.length - 1].toFixed(2) + ')',
              data: d, borderColor: colors[i], backgroundColor: 'transparent',
              borderWidth: 2, pointRadius: 0, tension: 0.1,
            };
          }),
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 700 },
          plugins: { legend: { position: 'top', labels: { color: '#b0bccf', font: { family: 'Geist Mono', size: 10 } } }, tooltip: tooltipOpts() },
          scales: {
            x: {
              title: { display: true, text: 'Trade number', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
              ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 }, maxTicksLimit: 8 },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
            y: {
              title: { display: true, text: 'Equity (×starting)', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
              ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } },
              grid: { color: 'rgba(255,255,255,0.04)' },
              type: 'logarithmic',
            },
          },
        },
      });
    }

    function simMedian(f, trades, paths) {
      var p = state.mode === 'symmetric' ? state.winRate : state.pWin;
      var W = state.mode === 'symmetric' ? state.avgWin : state.maxWin;
      var L = state.mode === 'symmetric' ? state.avgLoss : state.maxLoss;
      var allPaths = [];
      for (var k = 0; k < paths; k++) {
        var eq = [1], v = 1;
        for (var t = 0; t < trades; t++) {
          var win = Math.random() < p;
          if (win) v = v * (1 + f * W / L);  // win returns f * R where R = W/L
          else v = v * (1 - f);              // loss removes f of bankroll
          if (v < 1e-4) v = 1e-4;
          eq.push(v);
        }
        allPaths.push(eq);
      }
      // Per-step median
      var med = [];
      for (var i = 0; i <= trades; i++) {
        var col = allPaths.map(function (a) { return a[i]; }).sort(function (a, b) { return a - b; });
        med.push(col[Math.floor(col.length / 2)]);
      }
      return med;
    }
  }

  function tooltipOpts() {
    return {
      backgroundColor: '#161d28', borderColor: 'rgba(255,255,255,0.10)', borderWidth: 1,
      titleColor: '#dde3ef', bodyColor: '#b0bccf', padding: 10,
      titleFont: { family: 'Geist Mono', size: 11 }, bodyFont: { family: 'Geist Mono', size: 11 },
    };
  }
  function setOut(root, key, val) {
    var el = root.querySelector('[data-out="' + key + '"]');
    if (el) el.textContent = val;
  }
  function bindFields(root, state, onChange) {
    ['winRate','avgWin','avgLoss','capital','pWin','maxWin','maxLoss'].forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]');
      if (!i) return;
      var asPct = (k === 'winRate' || k === 'pWin');
      i.value = asPct ? (state[k] * 100).toFixed(1) : state[k];
      i.addEventListener('input', function () {
        var v = parseFloat(i.value);
        if (isNaN(v)) return;
        state[k] = asPct ? v / 100 : v;
        onChange();
      });
    });
  }
  function syncInputs(root, state) {
    ['winRate','avgWin','avgLoss','capital','pWin','maxWin','maxLoss'].forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]');
      if (!i) return;
      var asPct = (k === 'winRate' || k === 'pWin');
      i.value = asPct ? (state[k] * 100).toFixed(1) : state[k];
    });
  }

  var EXAMPLES = {
    classic: {
      label: '60% win, 2:1 win:loss',
      desc: 'Textbook setup — full Kelly = 40%, half-Kelly = 20%.',
      state: { mode: 'symmetric', winRate: 0.60, avgWin: 2000, avgLoss: 1000, capital: 100000 },
    },
    coinflip: {
      label: '55% edge on a coinflip-style bet',
      desc: 'Small edge → tiny optimal bet. Shows why "more conviction = more size" only works with real edge.',
      state: { mode: 'symmetric', winRate: 0.55, avgWin: 1000, avgLoss: 1000, capital: 100000 },
    },
    iron: {
      label: 'Iron condor with 75% prob profit',
      desc: 'Asymmetric defined-risk option strategy. Win small, lose big — Kelly often tiny.',
      state: { mode: 'asymmetric', pWin: 0.75, maxWin: 1500, maxLoss: 4500, capital: 100000 },
    },
  };

  function template() {
    return ''
      + '<div class="qp-grid">'
      + '  <div class="qp-card">'
      + '    <div class="qp-card-title">Inputs</div>'
      + '    <div class="qp-field">'
      + '      <div class="qp-label">Mode</div>'
      + '      <div class="qp-radio-group">'
      + '        <button type="button" class="qp-radio is-on" data-mode="symmetric">Symmetric</button>'
      + '        <button type="button" class="qp-radio" data-mode="asymmetric">Defined-risk</button>'
      + '      </div>'
      + '    </div>'

      + '    <div data-sym>'
      + '      <div class="qp-field"><div class="qp-label" data-tip="lbl-winrate">Win rate (%)</div><input class="qp-input" type="number" step="0.5" data-field="winRate"></div>'
      + '      <div class="qp-field"><div class="qp-label">Avg win (₹)</div><input class="qp-input" type="number" step="100" data-field="avgWin"></div>'
      + '      <div class="qp-field"><div class="qp-label">Avg loss (₹)</div><input class="qp-input" type="number" step="100" data-field="avgLoss"></div>'
      + '    </div>'

      + '    <div data-asym style="display:none;">'
      + '      <div class="qp-field"><div class="qp-label">P(win) %</div><input class="qp-input" type="number" step="0.5" data-field="pWin"></div>'
      + '      <div class="qp-field"><div class="qp-label">Max win (₹)</div><input class="qp-input" type="number" step="100" data-field="maxWin"></div>'
      + '      <div class="qp-field"><div class="qp-label">Max loss (₹)</div><input class="qp-input" type="number" step="100" data-field="maxLoss"></div>'
      + '    </div>'

      + '    <div class="qp-field"><div class="qp-label">Total capital (₹)</div><input class="qp-input" type="number" step="1000" data-field="capital"></div>'
      + '  </div>'

      + '  <div>'
      + '    <div class="qp-outputs">'
      + '      <div class="qp-out"><div class="qp-out-lbl" data-tip="lbl-kelly">Full Kelly</div><div class="qp-out-val bull" data-out="kelly">—</div><div class="qp-out-sub" data-out="kellyR">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl" data-tip="lbl-half">Half-Kelly</div><div class="qp-out-val delta" data-out="half">—</div><div class="qp-out-sub" data-out="halfR">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Quarter Kelly</div><div class="qp-out-val gamma" data-out="quarter">—</div><div class="qp-out-sub" data-out="quarterR">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl" data-tip="lbl-fixed">2% Fixed-Fractional</div><div class="qp-out-val rho" data-out="fixed">—</div><div class="qp-out-sub" data-out="fixedR">—</div></div>'
      + '    </div>'
      + '    <div class="qp-chart"><div class="qp-chart-title">Bet size per trade</div><div style="height:240px;padding:8px;"><canvas id="qpSizingBars"></canvas></div></div>'
      + '    <div class="qp-chart" style="margin-top:14px;"><div class="qp-chart-title">Simulated equity over 252 trades (median, log scale)</div><div style="height:280px;padding:8px;"><canvas id="qpSizingEquity"></canvas></div></div>'
      + '  </div>'
      + '</div>'

      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Concept — Kelly Criterion <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body">'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="f^* = \\frac{p \\cdot b - q}{b}"></div>'
      + '    <dl class="qp-concept-legend">'
      + '      <dt>f*</dt><dd>Optimal fraction of bankroll to risk</dd>'
      + '      <dt>p</dt><dd>Probability of winning</dd>'
      + '      <dt>q</dt><dd>1 − p (probability of losing)</dd>'
      + '      <dt>b</dt><dd>Win/loss ratio (₹ won per ₹ lost)</dd>'
      + '    </dl>'
      + '    <div class="qp-concept-intuition">Kelly maximises long-run <em>geometric</em> growth. Full Kelly is mathematically optimal but emotionally brutal — drawdowns of 30–40% are normal. Most practitioners run half-Kelly, which captures ~75% of the growth with ~25% of the drawdown variance.</div>'
      + '    <div style="margin-top:12px;">'
      + '      <div class="qp-card-title" style="margin-bottom:8px;">Asymmetric (defined-risk options)</div>'
      + '      <div class="qp-concept-formula" data-katex-display data-katex="f^* = \\frac{p}{L} - \\frac{q}{W}"></div>'
      + '      <div class="qp-concept-intuition">For capped payoffs (iron condors, spreads), the symmetric formula doesn\'t apply. Use W = max profit, L = max loss. The simulation chart shows median equity for full / half / quarter Kelly and a fixed-2% baseline so you can compare drawdown profiles.</div>'
      + '    </div>'
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
