/* quant-portfolio.js — Portfolio Analytics tab.
 * Fetches /api/stock/<t>/chart for each ticker in parallel, computes Sharpe/Sortino/
 * Calmar/max DD/beta/correlations, renders heatmap + equity curve + rolling σ chart.
 */
(function () {
  'use strict';
  var initialised = false;
  document.addEventListener('qp:tab', function (e) {
    if (e.detail.tab !== 'portfolio' || initialised) return;
    init();
    initialised = true;
  });

  function init() {
    var root = document.getElementById('qpPortRoot');
    if (!root) return;
    root.innerHTML = template();

    var state = {
      tickers: 'RELIANCE,HDFCBANK,TCS,INFY,LT',
      weights: '20,20,20,20,20',
      benchmark: 'NIFTY',
      period: '1y',
    };
    bindFields(root, state);

    root.querySelector('[data-run]').addEventListener('click', run);
    root.querySelector('[data-watchlist]').addEventListener('click', loadWatchlist);
    root.querySelectorAll('[data-example]').forEach(function (b) {
      b.addEventListener('click', function () {
        var ex = EXAMPLES[b.dataset.example]; if (!ex) return;
        Object.assign(state, ex.state); syncInputs(root, state); run();
      });
    });
    root.querySelectorAll('.qp-panel-head').forEach(function (h) {
      h.addEventListener('click', function () { h.parentElement.classList.toggle('is-open'); });
    });
    var katexIv = setInterval(function () {
      if (window.katex) { Glossary.renderAll(root); clearInterval(katexIv); }
    }, 100);

    var equityChart = null, volChart = null;

    function loadWatchlist() {
      var apply = function (tks) {
        state.tickers = tks.join(',');
        state.weights = tks.map(function () { return (100 / tks.length).toFixed(1); }).join(',');
        syncInputs(root, state);
      };
      if (window.QPDemo) {
        QPDemo.safeFetch('/api/watchlist', {
          label: 'watchlist',
          demo: function () { return { data: [{ticker:'RELIANCE'},{ticker:'HDFCBANK'},{ticker:'TCS'},{ticker:'INFY'},{ticker:'LT'}] }; },
        }).then(function (j) {
          var arr = (j && j.data) || [];
          if (!arr.length) { QPDemo.toast('Watchlist is empty. Try a preset example below.', 'info'); return; }
          var tks = arr.map(function (i) { return (i.ticker || i.symbol || '').toString().toUpperCase(); }).filter(Boolean).slice(0, 10);
          apply(tks);
        });
      } else {
        fetch('/api/watchlist', { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
          .then(function (j) { var arr = (j && j.data) || []; if (arr.length) apply(arr.map(function (i) { return (i.ticker||'').toUpperCase(); }).filter(Boolean).slice(0, 10)); });
      }
    }

    document.addEventListener('qp:random', function (e) {
      if (e.detail.tab !== 'portfolio') return;
      var keys = Object.keys(EXAMPLES);
      var pick = keys[Math.floor(Math.random() * keys.length)];
      var btn = root.querySelector('[data-example="' + pick + '"]');
      if (btn) btn.click();
    });

    function run() {
      var tickers = state.tickers.split(',').map(function (s) { return s.trim().toUpperCase(); }).filter(Boolean);
      if (!tickers.length) return;
      var weights = state.weights.split(',').map(function (s) { return parseFloat(s) || 0; });
      if (weights.length !== tickers.length) {
        weights = tickers.map(function () { return 100 / tickers.length; });
      }
      var wSum = weights.reduce(function (a, b) { return a + b; }, 0) || 1;
      weights = weights.map(function (w) { return w / wSum; });

      setOut(root, 'status', 'Fetching ' + tickers.length + ' tickers + ' + state.benchmark + '…');

      var period = state.period;
      Promise.all(tickers.concat([state.benchmark]).map(function (t) {
        if (window.QPDemo) {
          return QPDemo.safeFetch('/api/stock/' + encodeURIComponent(t) + '/chart?period=' + period, {
            label: t + ' history',
            demo: function () { return { data: QPDemo.syntheticOHLC(t, periodDays(period)) }; },
          });
        }
        return fetch('/api/stock/' + encodeURIComponent(t) + '/chart?period=' + period, { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : { data: [] }; })
          .catch(function () { return { data: [] }; });
      })).then(function (results) {
        // Align by date
        var byTicker = {};
        results.forEach(function (j, i) {
          var t = i < tickers.length ? tickers[i] : state.benchmark;
          var rows = (j && j.data) || [];
          byTicker[t] = {};
          rows.forEach(function (r) { byTicker[t][r.date] = r.close; });
        });
        var bench = state.benchmark;
        var dates = Object.keys(byTicker[bench] || {}).sort();
        var aligned = {};
        var allHaveDate = function (d) {
          if (byTicker[bench][d] == null) return false;
          for (var i = 0; i < tickers.length; i++) if (byTicker[tickers[i]][d] == null) return false;
          return true;
        };
        var alignedDates = dates.filter(allHaveDate);
        if (alignedDates.length < 30) {
          setOut(root, 'status', 'Not enough overlapping history (' + alignedDates.length + ' days). Try a longer period.');
          return;
        }
        tickers.concat([bench]).forEach(function (t) {
          aligned[t] = alignedDates.map(function (d) { return byTicker[t][d]; });
        });

        // Per-ticker returns
        var returnsByT = {};
        tickers.forEach(function (t) { returnsByT[t] = Quant.dailyReturns(aligned[t]); });
        var benchReturns = Quant.dailyReturns(aligned[bench]);

        // Portfolio returns = Σ w_i · r_i
        var n = benchReturns.length;
        var portReturns = new Array(n).fill(0);
        for (var i = 0; i < n; i++) {
          for (var j = 0; j < tickers.length; j++) {
            portReturns[i] += weights[j] * returnsByT[tickers[j]][i];
          }
        }

        var rf = 0.065;
        var sharpe = Quant.sharpe(portReturns, rf);
        var sortino = Quant.sortino(portReturns, rf);
        var equity = Quant.equityCurve(portReturns, 1);
        var dd = Quant.maxDrawdown(equity);
        var calmar = Quant.calmar(portReturns);
        var portBeta = Quant.beta(portReturns, benchReturns);

        setOut(root, 'status', alignedDates.length + ' days, ' + tickers.length + ' positions vs ' + bench + '. Calculated.');
        setOut(root, 'sharpe', sharpe.toFixed(2));
        setOut(root, 'sortino', sortino === Infinity ? '∞' : sortino.toFixed(2));
        setOut(root, 'calmar', calmar === Infinity ? '∞' : calmar.toFixed(2));
        setOut(root, 'maxdd', (dd.maxDD * 100).toFixed(1) + '%');
        setOut(root, 'beta', portBeta.toFixed(2));
        setOut(root, 'annret', (Quant.annualisedReturn(portReturns) * 100).toFixed(1) + '%');
        setOut(root, 'annvol', (Quant.annualisedVol(portReturns) * 100).toFixed(1) + '%');

        renderHeatmap(returnsByT);
        renderEquity(equity, aligned[bench], alignedDates);
        renderVol(portReturns);
      });
    }

    function renderHeatmap(returnsByT) {
      var corr = Quant.correlationMatrix(returnsByT);
      var host = root.querySelector('#qpCorrHost');
      var html = '<table class="qp-heatmap"><thead><tr><th></th>';
      corr.tickers.forEach(function (t) { html += '<th>' + t + '</th>'; });
      html += '</tr></thead><tbody>';
      corr.tickers.forEach(function (t, i) {
        html += '<tr><th>' + t + '</th>';
        corr.matrix[i].forEach(function (v) {
          var c = corrColor(v);
          html += '<td style="background:' + c.bg + ';color:' + c.fg + ';">' + v.toFixed(2) + '</td>';
        });
        html += '</tr>';
      });
      html += '</tbody></table>';
      host.innerHTML = html;
    }

    function renderEquity(eq, benchCloses, dates) {
      var ctx = root.querySelector('#qpEquityChart').getContext('2d');
      if (equityChart) equityChart.destroy();
      var benchEq = []; var bs = benchCloses[0];
      for (var i = 0; i < benchCloses.length; i++) benchEq.push(benchCloses[i] / bs);
      equityChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: dates,
          datasets: [
            { label: 'Portfolio', data: eq, borderColor: '#6ddec0', backgroundColor: 'rgba(45,212,170,0.10)', borderWidth: 2, pointRadius: 0, tension: 0.1, fill: true },
            { label: state.benchmark, data: benchEq, borderColor: '#adc6ff', backgroundColor: 'transparent', borderWidth: 1.5, borderDash: [4, 4], pointRadius: 0, tension: 0.1 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 600 },
          plugins: {
            legend: { position: 'top', align: 'end', labels: { color: '#b0bccf', font: { family: 'Geist Mono', size: 10 } } },
            tooltip: tooltipOpts(),
          },
          scales: {
            x: { ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 9 }, maxTicksLimit: 10 }, grid: { display: false } },
            y: { title: { display: true, text: 'Equity (×start)', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } }, ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
        },
      });
    }

    function renderVol(returns) {
      var win = 30;
      var rollVol = [];
      var labels = [];
      for (var i = win; i <= returns.length; i++) {
        var slice = returns.slice(i - win, i);
        rollVol.push(Quant.annualisedVol(slice) * 100);
        labels.push(i);
      }
      var ctx = root.querySelector('#qpVolChart').getContext('2d');
      if (volChart) volChart.destroy();
      volChart = new Chart(ctx, {
        type: 'line',
        data: { labels: labels, datasets: [{ label: 'Rolling ' + win + 'd σ (%)', data: rollVol, borderColor: '#ffc85a', backgroundColor: 'rgba(255,200,90,0.08)', borderWidth: 2, pointRadius: 0, tension: 0.2, fill: true }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 500 },
          plugins: { legend: { display: false }, tooltip: tooltipOpts() },
          scales: {
            x: { ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 }, maxTicksLimit: 8 }, grid: { display: false } },
            y: { title: { display: true, text: 'Annualised σ (%)', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } }, ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
        },
      });
    }
  }

  function periodDays(p) {
    var m = { '6mo': 180, '1y': 252, '2y': 504, '5y': 1260 };
    return m[p] || 252;
  }
  function corrColor(v) {
    if (v > 0.7) return { bg: 'rgba(242,107,107,0.35)', fg: '#fff' };  // high corr = warning
    if (v > 0.4) return { bg: 'rgba(242,107,107,0.18)', fg: '#f0a3a3' };
    if (v > 0)   return { bg: 'rgba(141,180,224,0.10)', fg: '#adc6ff' };
    if (v < -0.4) return { bg: 'rgba(45,212,170,0.30)', fg: '#fff' };
    if (v < 0)   return { bg: 'rgba(45,212,170,0.10)', fg: '#6ddec0' };
    return { bg: 'rgba(255,255,255,0.04)', fg: '#8a94a8' };
  }
  function tooltipOpts() { return { backgroundColor: '#161d28', borderColor: 'rgba(255,255,255,0.10)', borderWidth: 1, titleColor: '#dde3ef', bodyColor: '#b0bccf', padding: 10 }; }
  function setOut(root, key, val) { var el = root.querySelector('[data-out="' + key + '"]'); if (el) el.textContent = val; }
  function bindFields(root, state) {
    ['tickers','weights','benchmark','period'].forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]'); if (!i) return;
      i.value = state[k];
      i.addEventListener('input', function () { state[k] = i.value; });
      if (i.tagName === 'SELECT') i.addEventListener('change', function () { state[k] = i.value; });
    });
  }
  function syncInputs(root, state) {
    ['tickers','weights','benchmark','period'].forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]'); if (i) i.value = state[k];
    });
  }

  var EXAMPLES = {
    diverse: {
      label: '50/50 NIFTY + GOLDBEES — diversification',
      desc: 'Classic equity + gold pair. Low correlation = real diversification.',
      state: { tickers: 'NIFTYBEES,GOLDBEES', weights: '50,50', benchmark: 'NIFTY', period: '1y' },
    },
    it: {
      label: '5 IT stocks — high correlation',
      desc: 'TCS/INFY/WIPRO/HCL/TECHM. Watch the heatmap turn red — they\'re basically the same trade.',
      state: { tickers: 'TCS,INFY,WIPRO,HCLTECH,TECHM', weights: '20,20,20,20,20', benchmark: 'NIFTY', period: '1y' },
    },
    bluechip: {
      label: 'Bluechip basket',
      desc: 'RIL, HDFC Bank, TCS, INFY, LT — sector-mixed.',
      state: { tickers: 'RELIANCE,HDFCBANK,TCS,INFY,LT', weights: '25,25,15,15,20', benchmark: 'NIFTY', period: '1y' },
    },
  };

  function template() {
    return ''
      + '<div class="qp-grid">'
      + '  <div class="qp-card">'
      + '    <div class="qp-card-title">Portfolio</div>'
      + '    <div class="qp-field"><div class="qp-label">Tickers (comma-sep)</div><input class="qp-input" data-field="tickers" placeholder="RELIANCE,HDFCBANK,TCS"></div>'
      + '    <div class="qp-field"><div class="qp-label">Weights (%)</div><input class="qp-input" data-field="weights" placeholder="20,20,20,20,20"></div>'
      + '    <div class="qp-field"><div class="qp-label">Benchmark</div><input class="qp-input" data-field="benchmark" value="NIFTY"></div>'
      + '    <div class="qp-field"><div class="qp-label">History</div><select class="qp-select" data-field="period"><option value="6mo">6 months</option><option value="1y" selected>1 year</option><option value="2y">2 years</option><option value="5y">5 years</option></select></div>'
      + '    <button class="qp-btn" data-run style="width:100%;margin-top:6px;">Calculate</button>'
      + '    <button class="qp-btn ghost" data-watchlist style="width:100%;margin-top:6px;">Load from Watchlist</button>'
      + '  </div>'

      + '  <div>'
      + '    <div class="qp-outputs">'
      + '      <div class="qp-out"><div class="qp-out-lbl" data-tip="lbl-sharpe">Sharpe</div><div class="qp-out-val bull" data-out="sharpe">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Sortino</div><div class="qp-out-val delta" data-out="sortino">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Calmar</div><div class="qp-out-val gamma" data-out="calmar">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Max DD</div><div class="qp-out-val bear" data-out="maxdd">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Beta</div><div class="qp-out-val rho" data-out="beta">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Ann. return</div><div class="qp-out-val bull" data-out="annret">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Ann. σ</div><div class="qp-out-val vega" data-out="annvol">—</div></div>'
      + '    </div>'
      + '    <div style="font:500 11px DM Sans;color:#6a7484;margin-bottom:10px;" data-out="status">Configure portfolio and click Calculate.</div>'

      + '    <div class="qp-chart"><div class="qp-chart-title">Equity curve vs benchmark</div><div style="height:280px;padding:8px;"><canvas id="qpEquityChart"></canvas></div></div>'

      + '    <div class="qp-grid" style="grid-template-columns:1.2fr 1fr;margin-top:14px;">'
      + '      <div class="qp-card"><div class="qp-card-title">Correlation heatmap</div><div id="qpCorrHost" style="overflow-x:auto;"></div></div>'
      + '      <div class="qp-chart"><div class="qp-chart-title">Rolling 30d σ</div><div style="height:240px;padding:8px;"><canvas id="qpVolChart"></canvas></div></div>'
      + '    </div>'
      + '  </div>'
      + '</div>'

      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Concept — Risk-adjusted return <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body">'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\text{Sharpe} = \\frac{E[R] - R_f}{\\sigma_R} \\cdot \\sqrt{252}"></div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\beta = \\frac{\\text{cov}(R_i, R_m)}{\\text{var}(R_m)}, \\quad \\rho(X, Y) = \\frac{\\text{cov}(X, Y)}{\\sigma_X \\sigma_Y}"></div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\text{MaxDD} = \\max_t \\frac{\\text{peak}_t - \\text{trough}_t}{\\text{peak}_t}"></div>'
      + '    <div class="qp-concept-intuition">Sharpe > 1 is decent, > 2 is rare, > 3 is suspicious (likely overfit). <strong>Correlation > 0.7</strong> between two positions means they\'re essentially the same trade — heatmap turns red. The rolling σ chart catches volatility regime shifts that point-in-time stats miss.</div>'
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
