/* quant-backtest.js — Historical Reactions Backtester tab.
 * Thin UI over /api/v1/backtest (backend/event_backtest.py).
 */
(function () {
  'use strict';
  var initialised = false;
  document.addEventListener('qp:tab', function (e) {
    if (e.detail.tab !== 'backtest' || initialised) return;
    init(); initialised = true;
  });

  function init() {
    var root = document.getElementById('qpBacktestRoot');
    if (!root) return;
    root.innerHTML = template();

    var state = { ticker: 'RELIANCE', event_type: 'earnings_beat' };
    bindFields(root, state);
    root.querySelector('[data-run]').addEventListener('click', run);
    root.querySelectorAll('[data-example]').forEach(function (b) {
      b.addEventListener('click', function () {
        var ex = EXAMPLES[b.dataset.example]; if (!ex) return;
        Object.assign(state, ex.state); syncInputs(root, state); run();
      });
    });
    root.querySelectorAll('.qp-panel-head').forEach(function (h) {
      h.addEventListener('click', function () { h.parentElement.classList.toggle('is-open'); });
    });
    var katexIv = setInterval(function () { if (window.katex) { Glossary.renderAll(root); clearInterval(katexIv); } }, 100);

    var gapChart = null, driftChart = null;

    function run() {
      setOut(root, 'status', 'Fetching…');
      var url = '/api/v1/backtest?ticker=' + encodeURIComponent(state.ticker) + '&event_type=' + encodeURIComponent(state.event_type);
      var done = function (d) {
        if (!d || (!d.gaps && !d.gap_pct_mean && !d.n)) {
          setOut(root, 'status', 'No historical sample for this ticker/event type.');
          return;
        }
        var n = d.n || (d.gaps && d.gaps.length) || 0;
        finish(d, n);
      };
      if (window.QPDemo) {
        QPDemo.safeFetch(url, {
          label: state.ticker + ' / ' + state.event_type,
          demo: function () { return { data: QPDemo.syntheticBacktest(state.ticker, state.event_type) }; },
        }).then(function (j) { done(j && (j.data || j)); });
      } else {
        fetch(url, { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
          .then(function (j) { done(j && (j.data || j)); });
      }
    }
    function finish(d, n) {
      setOut(root, 'status', 'Sample size: ' + n + (n < 10 ? ' — too few, treat with caution' : ''));
      setOut(root, 'n', n);
      setOut(root, 'gapMean',  fmtPct(d.avg_gap_up_pct != null ? d.avg_gap_up_pct : d.gap_pct_mean));
      setOut(root, 'fade',     d.fade_probability_pct != null ? d.fade_probability_pct.toFixed(0) + '%' : '—');
      setOut(root, 'drift5d',  fmtPct(d.avg_5day_drift_pct));
      setOut(root, 'winRate',  d.win_rate_pct != null ? d.win_rate_pct.toFixed(0) + '%' : '—');
      if (Array.isArray(d.gaps) && d.gaps.length) drawHistogram(d.gaps, '#qpGapHist', 'Gap distribution (%)');
      if (Array.isArray(d.drifts_5d) && d.drifts_5d.length) drawHistogram(d.drifts_5d, '#qpDriftHist', '5-day drift distribution (%)');
    }

    document.addEventListener('qp:random', function (e) {
      if (e.detail.tab !== 'backtest') return;
      var keys = Object.keys(EXAMPLES);
      var pick = keys[Math.floor(Math.random() * keys.length)];
      var btn = root.querySelector('[data-example="' + pick + '"]');
      if (btn) btn.click();
    });

    function drawHistogram(arr, sel, title) {
      var c = root.querySelector(sel); if (!c) return;
      var ctx = c.getContext('2d');
      var min = Math.min.apply(null, arr), max = Math.max.apply(null, arr);
      var n = 20; var w = (max - min) / n || 1;
      var bins = new Array(n).fill(0); var labels = new Array(n);
      arr.forEach(function (v) { var i = Math.min(n - 1, Math.floor((v - min) / w)); bins[i]++; });
      for (var i = 0; i < n; i++) labels[i] = (min + w * (i + 0.5)).toFixed(1) + '%';
      var colors = labels.map(function (l) { return parseFloat(l) >= 0 ? 'rgba(45,212,170,0.6)' : 'rgba(242,107,107,0.6)'; });
      if (sel === '#qpGapHist' && gapChart) gapChart.destroy();
      if (sel === '#qpDriftHist' && driftChart) driftChart.destroy();
      var chart = new Chart(ctx, {
        type: 'bar',
        data: { labels: labels, datasets: [{ label: title, data: bins, backgroundColor: colors, borderWidth: 0 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 500 },
          plugins: { legend: { display: false }, tooltip: tooltipOpts() },
          scales: {
            x: { ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 9 }, maxTicksLimit: 8 }, grid: { display: false } },
            y: { ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
        },
      });
      if (sel === '#qpGapHist') gapChart = chart; else driftChart = chart;
    }
  }

  function fmtPct(v) { if (v == null) return '—'; var s = v >= 0 ? '+' : ''; return s + Number(v).toFixed(2) + '%'; }
  function tooltipOpts() { return { backgroundColor: '#161d28', borderColor: 'rgba(255,255,255,0.10)', borderWidth: 1, titleColor: '#dde3ef', bodyColor: '#b0bccf', padding: 10 }; }
  function setOut(root, key, val) { var el = root.querySelector('[data-out="' + key + '"]'); if (el) el.textContent = val; }
  function bindFields(root, state) {
    ['ticker', 'event_type'].forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]'); if (!i) return;
      i.value = state[k];
      i.addEventListener('input', function () { state[k] = i.value; });
      if (i.tagName === 'SELECT') i.addEventListener('change', function () { state[k] = i.value; });
    });
  }
  function syncInputs(root, state) {
    ['ticker', 'event_type'].forEach(function (k) {
      var i = root.querySelector('[data-field="' + k + '"]'); if (i) i.value = state[k];
    });
  }

  var EXAMPLES = {
    rel_beat: { label: 'RELIANCE earnings beats', desc: 'How does RIL react to positive surprises historically?', state: { ticker: 'RELIANCE', event_type: 'earnings_beat' } },
    infy_cut: { label: 'INFY guidance cuts', desc: 'Gap-down and fade behaviour.', state: { ticker: 'INFY', event_type: 'guidance_cut' } },
    nifty_block: { label: 'NIFTY block deal gap-ups', desc: 'Pure noise or signal?', state: { ticker: 'NIFTY', event_type: 'block_deal' } },
  };

  function template() {
    return ''
      + '<div class="qp-grid">'
      + '  <div class="qp-card">'
      + '    <div class="qp-card-title">Query</div>'
      + '    <div class="qp-field"><div class="qp-label">Ticker</div><input class="qp-input" data-field="ticker" value="RELIANCE"></div>'
      + '    <div class="qp-field">'
      + '      <div class="qp-label">Event type</div>'
      + '      <select class="qp-select" data-field="event_type">'
      + '        <option value="earnings_beat">Earnings beat</option>'
      + '        <option value="earnings_miss">Earnings miss</option>'
      + '        <option value="guidance_cut">Guidance cut</option>'
      + '        <option value="order_win">Order win</option>'
      + '        <option value="block_deal">Block deal</option>'
      + '        <option value="insider">Insider activity</option>'
      + '        <option value="policy">Policy / regulatory</option>'
      + '      </select>'
      + '    </div>'
      + '    <button class="qp-btn" data-run style="width:100%;margin-top:6px;">Run backtest</button>'
      + '  </div>'

      + '  <div>'
      + '    <div class="qp-outputs">'
      + '      <div class="qp-out"><div class="qp-out-lbl">Sample (N)</div><div class="qp-out-val" data-out="n">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Avg gap</div><div class="qp-out-val" data-out="gapMean">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Fade prob</div><div class="qp-out-val" data-out="fade">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">5d drift</div><div class="qp-out-val" data-out="drift5d">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Win rate</div><div class="qp-out-val" data-out="winRate">—</div></div>'
      + '    </div>'
      + '    <div style="font:500 11px DM Sans;color:#6a7484;margin-bottom:10px;" data-out="status">Pick a ticker + event type and run.</div>'
      + '    <div class="qp-grid" style="grid-template-columns:1fr 1fr;">'
      + '      <div class="qp-chart"><div class="qp-chart-title">Gap distribution</div><div style="height:240px;padding:8px;"><canvas id="qpGapHist"></canvas></div></div>'
      + '      <div class="qp-chart"><div class="qp-chart-title">5-day drift distribution</div><div style="height:240px;padding:8px;"><canvas id="qpDriftHist"></canvas></div></div>'
      + '    </div>'
      + '  </div>'
      + '</div>'

      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Concept — Event-window backtest <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body">'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\text{Gap \\%} = \\frac{O_t - C_{t-1}}{C_{t-1}}, \\quad \\text{5d drift} = \\frac{C_{t+5} - C_t}{C_t}"></div>'
      + '    <div class="qp-concept-intuition">This is NOT a strategy backtester. It answers: when {ticker} had this kind of event historically, what happened? <strong>Small samples mislead</strong> — N < 10 is noise. The distribution charts show whether the "average" is hiding a wide spread.</div>'
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
