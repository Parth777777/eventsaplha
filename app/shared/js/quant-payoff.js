/* quant-payoff.js — Strategy Payoff Builder tab (Quant Playground).
 * Renders into #qpPayoffRoot the first time the "payoff" tab activates.
 *
 * Visualisations (Chart.js):
 *   - Combined P&L vs underlying price (expiry curve solid, today curve dashed)
 *   - Profit zone shaded green, loss zone shaded red
 *   - Vertical dashed lines at each breakeven
 *   - Strike markers on x-axis
 */
(function () {
  'use strict';
  var initialised = false;

  document.addEventListener('qp:tab', function (e) {
    if (e.detail.tab !== 'payoff' || initialised) return;
    init();
    initialised = true;
  });

  // ── Strategy templates ──
  // Each leg.premium is the absolute ₹ price (positive). For longs, you pay it; for shorts, you collect it.
  // Strikes anchored to `spot` so templates work for any underlying.
  function template(name, spot) {
    var s = spot || 100;
    var T = {
      long_call:   [{ type:'long_call',  strike:s,        qty:1, premium:s*0.03 }],
      long_put:    [{ type:'long_put',   strike:s,        qty:1, premium:s*0.03 }],
      short_call:  [{ type:'short_call', strike:s*1.05,   qty:1, premium:s*0.015 }],
      short_put:   [{ type:'short_put',  strike:s*0.95,   qty:1, premium:s*0.015 }],
      bull_call:   [
        { type:'long_call',  strike:s*1.00, qty:1, premium:s*0.030 },
        { type:'short_call', strike:s*1.05, qty:1, premium:s*0.012 },
      ],
      bear_put: [
        { type:'long_put',   strike:s*1.00, qty:1, premium:s*0.030 },
        { type:'short_put',  strike:s*0.95, qty:1, premium:s*0.012 },
      ],
      iron_condor: [
        { type:'short_put',  strike:s*0.95, qty:1, premium:s*0.012 },
        { type:'long_put',   strike:s*0.90, qty:1, premium:s*0.005 },
        { type:'short_call', strike:s*1.05, qty:1, premium:s*0.012 },
        { type:'long_call',  strike:s*1.10, qty:1, premium:s*0.005 },
      ],
      butterfly: [
        { type:'long_call',  strike:s*0.95, qty:1, premium:s*0.060 },
        { type:'short_call', strike:s*1.00, qty:2, premium:s*0.030 },
        { type:'long_call',  strike:s*1.05, qty:1, premium:s*0.012 },
      ],
      straddle: [
        { type:'long_call',  strike:s, qty:1, premium:s*0.030 },
        { type:'long_put',   strike:s, qty:1, premium:s*0.030 },
      ],
      strangle: [
        { type:'long_call',  strike:s*1.05, qty:1, premium:s*0.012 },
        { type:'long_put',   strike:s*0.95, qty:1, premium:s*0.012 },
      ],
      covered_call: [
        { type:'long_stock', strike:0, qty:1, premium:s },
        { type:'short_call', strike:s*1.05, qty:1, premium:s*0.012 },
      ],
      collar: [
        { type:'long_stock', strike:0, qty:1, premium:s },
        { type:'short_call', strike:s*1.05, qty:1, premium:s*0.012 },
        { type:'long_put',   strike:s*0.95, qty:1, premium:s*0.012 },
      ],
    };
    return (T[name] || T.long_call).map(function (l) { return Object.assign({}, l); });
  }

  function init() {
    var root = document.getElementById('qpPayoffRoot');
    if (!root) return;
    root.innerHTML = tplHtml();

    var state = {
      spot: 24500,
      sigma: 0.14,
      r: 0.065,
      dte: 14,
      template: 'iron_condor',
      legs: template('iron_condor', 24500),
    };

    var chart = null;
    renderLegs();
    bindControls();
    recompute();

    function bindControls() {
      root.querySelector('[data-spot]').addEventListener('input', function (e) {
        var v = parseFloat(e.target.value); if (isNaN(v)) return;
        state.spot = v; recompute();
      });
      root.querySelector('[data-sigma]').addEventListener('input', function (e) {
        var v = parseFloat(e.target.value); if (isNaN(v)) return;
        state.sigma = v / 100; recompute();
      });
      root.querySelector('[data-dte]').addEventListener('input', function (e) {
        var v = parseFloat(e.target.value); if (isNaN(v)) return;
        state.dte = v; recompute();
      });
      root.querySelector('[data-template]').addEventListener('change', function (e) {
        state.template = e.target.value;
        state.legs = template(state.template, state.spot);
        renderLegs(); recompute();
      });
      root.querySelector('[data-add]').addEventListener('click', function () {
        state.legs.push({ type:'long_call', strike: state.spot, qty:1, premium: state.spot*0.02 });
        renderLegs(); recompute();
      });

      root.querySelectorAll('.qp-panel-head').forEach(function (h) {
        h.addEventListener('click', function () { h.parentElement.classList.toggle('is-open'); });
      });
      root.querySelectorAll('[data-example]').forEach(function (b) {
        b.addEventListener('click', function () {
          var ex = EXAMPLES[b.dataset.example];
          if (!ex) return;
          Object.assign(state, ex.state);
          state.legs = ex.legs.map(function (l) { return Object.assign({}, l); });
          syncTopInputs();
          renderLegs(); recompute();
          var c = root.querySelector('.qp-chart');
          if (c && c.scrollIntoView) c.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
      });
      var katexIv = setInterval(function () {
        if (window.katex) { Glossary.renderAll(root); clearInterval(katexIv); }
      }, 100);

      document.addEventListener('qp:random', function (e) {
        if (e.detail.tab !== 'payoff') return;
        var keys = Object.keys(EXAMPLES);
        var pick = keys[Math.floor(Math.random() * keys.length)];
        var btn = root.querySelector('[data-example="' + pick + '"]');
        if (btn) btn.click();
      });
    }
    function syncTopInputs() {
      root.querySelector('[data-spot]').value = state.spot;
      root.querySelector('[data-sigma]').value = (state.sigma * 100).toFixed(2);
      root.querySelector('[data-dte]').value = state.dte;
      root.querySelector('[data-template]').value = state.template;
    }

    function renderLegs() {
      var tbody = root.querySelector('#qpLegsBody');
      tbody.innerHTML = state.legs.map(function (leg, idx) {
        return '<tr>'
          + '<td><select class="qp-select" data-leg-type="' + idx + '">'
          +   ['long_call','short_call','long_put','short_put','long_stock','short_stock'].map(function (t) {
                return '<option value="' + t + '"' + (leg.type===t?' selected':'') + '>' + t.replace('_',' ') + '</option>';
              }).join('')
          + '</select></td>'
          + '<td><input class="qp-input" type="number" step="1" value="' + (leg.strike||0) + '" data-leg-strike="' + idx + '"' + (leg.type.indexOf('stock')>=0?' disabled':'') + '></td>'
          + '<td><input class="qp-input" type="number" step="1" min="1" value="' + (leg.qty||1) + '" data-leg-qty="' + idx + '"></td>'
          + '<td><input class="qp-input" type="number" step="0.01" value="' + (leg.premium||0).toFixed(2) + '" data-leg-premium="' + idx + '"></td>'
          + '<td><button class="qp-leg-remove" data-leg-remove="' + idx + '" aria-label="Remove leg">&times;</button></td>'
          + '</tr>';
      }).join('');
      tbody.querySelectorAll('select[data-leg-type]').forEach(function (s) {
        s.addEventListener('change', function (e) {
          var idx = +e.target.dataset.legType; state.legs[idx].type = e.target.value;
          if (state.legs[idx].type.indexOf('stock')>=0) state.legs[idx].strike = 0;
          renderLegs(); recompute();
        });
      });
      tbody.querySelectorAll('input[data-leg-strike]').forEach(function (i) {
        i.addEventListener('input', function (e) { state.legs[+e.target.dataset.legStrike].strike = +e.target.value; recompute(); });
      });
      tbody.querySelectorAll('input[data-leg-qty]').forEach(function (i) {
        i.addEventListener('input', function (e) { state.legs[+e.target.dataset.legQty].qty = +e.target.value; recompute(); });
      });
      tbody.querySelectorAll('input[data-leg-premium]').forEach(function (i) {
        i.addEventListener('input', function (e) { state.legs[+e.target.dataset.legPremium].premium = +e.target.value; recompute(); });
      });
      tbody.querySelectorAll('button[data-leg-remove]').forEach(function (b) {
        b.addEventListener('click', function (e) {
          state.legs.splice(+e.target.dataset.legRemove, 1);
          renderLegs(); recompute();
        });
      });
    }

    function recompute() {
      var range = autoRange(state.legs, state.spot);
      var poff = Quant.combinedPayoff(state.legs, range, {
        steps: 240, daysToExpiry: state.dte, sigma: state.sigma, r: state.r,
      });
      var stats = Quant.payoffStats(state.legs, range, {
        steps: 2000, daysToExpiry: state.dte, sigma: state.sigma, r: state.r,
      });

      setOut(root, 'maxProfit', stats.maxProfit == null ? '∞' : '₹' + stats.maxProfit.toFixed(2));
      setOut(root, 'maxLoss',   stats.maxLoss == null ? '−∞' : '₹' + stats.maxLoss.toFixed(2));
      setOut(root, 'netPrem',   (stats.netPremium >= 0 ? '+' : '') + '₹' + stats.netPremium.toFixed(2));
      setOut(root, 'breakeven', stats.breakevens.length ? stats.breakevens.map(function (b) { return '₹' + b.toFixed(2); }).join(' · ') : '—');

      // Margin estimate (rough SPAN approx)
      var marginEst = Math.max(Math.abs(stats.maxLoss || 0), state.spot * 0.05);
      setOut(root, 'margin', '₹' + marginEst.toFixed(0) + ' approx');

      drawChart(poff, stats);
    }

    function drawChart(poff, stats) {
      var ctx = root.querySelector('#qpPayoffChart').getContext('2d');
      if (chart) chart.destroy();

      // Build separate datasets so positive area shades green, negative red.
      var expProfit = poff.expiry.map(function (v) { return v >= 0 ? v : null; });
      var expLoss   = poff.expiry.map(function (v) { return v < 0 ? v : null; });

      // Annotations for breakeven verticals — render via custom plugin
      var beAnnotations = stats.breakevens || [];

      chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: poff.S.map(function (s) { return +s.toFixed(2); }),
          datasets: [
            {
              label: 'P&L at expiry',
              data: poff.expiry,
              borderColor: '#dde3ef',
              borderWidth: 2,
              pointRadius: 0, tension: 0,
              fill: false,
            },
            {
              label: 'Profit zone',
              data: expProfit,
              borderColor: 'transparent',
              backgroundColor: 'rgba(45,212,170,0.18)',
              fill: { target: { value: 0 } },
              pointRadius: 0,
              tension: 0,
            },
            {
              label: 'Loss zone',
              data: expLoss,
              borderColor: 'transparent',
              backgroundColor: 'rgba(242,107,107,0.18)',
              fill: { target: { value: 0 } },
              pointRadius: 0,
              tension: 0,
            },
            poff.today && poff.today[0] != null ? {
              label: "P&L today (T-" + state.dte + ")",
              data: poff.today,
              borderColor: '#adc6ff',
              borderDash: [4, 4],
              borderWidth: 1.5,
              pointRadius: 0, tension: 0.1,
              fill: false,
            } : null,
          ].filter(Boolean),
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 500 },
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: true, position: 'top', align: 'end', labels: { color: '#b0bccf', font: { family: 'Geist Mono', size: 10 }, boxWidth: 12, boxHeight: 2, filter: function (item) { return item.text.indexOf('zone') < 0; } } },
            tooltip: tooltipOpts(),
          },
          scales: {
            x: {
              title: { display: true, text: 'Underlying price at expiry (₹)', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
              ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 }, maxTicksLimit: 10 },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
            y: {
              title: { display: true, text: 'Combined P&L (₹)', color: '#6a7484', font: { family: 'Plus Jakarta Sans', size: 10, weight: '700' } },
              ticks: { color: '#6a7484', font: { family: 'Geist Mono', size: 10 } },
              grid: { color: function (ctx) { return ctx.tick.value === 0 ? 'rgba(255,255,255,0.30)' : 'rgba(255,255,255,0.04)'; } },
            },
          },
        },
        plugins: [{
          id: 'beAndStrikes',
          afterDraw: function (chart) {
            var ctx = chart.ctx;
            var xs = chart.scales.x;
            var ys = chart.scales.y;
            // breakeven vertical dashed
            beAnnotations.forEach(function (be) {
              var x = xs.getPixelForValue(be);
              if (x < xs.left || x > xs.right) return;
              ctx.save();
              ctx.strokeStyle = '#ffc85a'; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
              ctx.beginPath(); ctx.moveTo(x, ys.top); ctx.lineTo(x, ys.bottom); ctx.stroke();
              ctx.font = '700 9px Geist Mono'; ctx.fillStyle = '#ffc85a';
              ctx.fillText('BE ₹' + be.toFixed(0), x + 4, ys.top + 11);
              ctx.restore();
            });
            // strikes
            var strikes = (state.legs || []).map(function (l) { return l.strike; }).filter(function (k) { return k > 0; });
            strikes.forEach(function (k) {
              var x = xs.getPixelForValue(k);
              if (x < xs.left || x > xs.right) return;
              ctx.save();
              ctx.strokeStyle = 'rgba(141,180,224,0.30)'; ctx.setLineDash([1, 4]); ctx.lineWidth = 1;
              ctx.beginPath(); ctx.moveTo(x, ys.top); ctx.lineTo(x, ys.bottom); ctx.stroke();
              ctx.restore();
            });
            // current spot marker
            var sx = xs.getPixelForValue(state.spot);
            if (sx >= xs.left && sx <= xs.right) {
              ctx.save();
              ctx.strokeStyle = '#6ddec0'; ctx.lineWidth = 1.5;
              ctx.beginPath(); ctx.moveTo(sx, ys.top); ctx.lineTo(sx, ys.bottom); ctx.stroke();
              ctx.font = '700 9px Geist Mono'; ctx.fillStyle = '#6ddec0';
              ctx.fillText('SPOT', sx + 4, ys.bottom - 4);
              ctx.restore();
            }
          },
        }],
      });
    }
  }

  function autoRange(legs, spot) {
    var levels = [spot];
    legs.forEach(function (l) { if (l.strike > 0) levels.push(l.strike); });
    levels.sort(function (a, b) { return a - b; });
    var mid = (levels[0] + levels[levels.length - 1]) / 2;
    return [mid * 0.70, mid * 1.30];
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

  // ── Examples ──
  var EXAMPLES = {
    nifty_condor: {
      label: 'NIFTY weekly iron condor',
      desc: 'Sell 24300/24700, buy 24200/24800. Premium-collection bet on range.',
      state: { spot: 24500, sigma: 0.14, dte: 7, r: 0.065, template: 'iron_condor' },
      legs: [
        { type:'short_put',  strike: 24300, qty:1, premium: 28 },
        { type:'long_put',   strike: 24200, qty:1, premium: 12 },
        { type:'short_call', strike: 24700, qty:1, premium: 30 },
        { type:'long_call',  strike: 24800, qty:1, premium: 13 },
      ],
    },
    rel_bull: {
      label: 'RELIANCE bull call spread',
      desc: 'Limited-risk bullish bet for low cost.',
      state: { spot: 2850, sigma: 0.22, dte: 30, r: 0.065, template: 'bull_call' },
      legs: [
        { type:'long_call',  strike: 2900, qty:1, premium: 42 },
        { type:'short_call', strike: 3000, qty:1, premium: 14 },
      ],
    },
    hdfc_straddle: {
      label: 'HDFC pre-results long straddle',
      desc: 'Pure-vol bet ahead of earnings. Demonstrates how IV crush kills it post-event.',
      state: { spot: 1640, sigma: 0.28, dte: 3, r: 0.065, template: 'straddle' },
      legs: [
        { type:'long_call', strike: 1640, qty:1, premium: 32 },
        { type:'long_put',  strike: 1640, qty:1, premium: 30 },
      ],
    },
    infy_cc: {
      label: 'INFY covered call (yield enhancement)',
      desc: 'Own stock, sell OTM call. Caps upside, collects premium.',
      state: { spot: 1500, sigma: 0.20, dte: 30, r: 0.065, template: 'covered_call' },
      legs: [
        { type:'long_stock', strike: 0, qty:300, premium: 1500 },
        { type:'short_call', strike: 1560, qty:3, premium: 22 },
      ],
    },
  };

  function tplHtml() {
    return ''
      + '<div class="qp-grid">'
        // INPUTS
      + '  <div class="qp-card">'
      + '    <div class="qp-card-title">Setup</div>'
      + '    <div class="qp-field"><div class="qp-label">Spot (S)</div><input class="qp-input" type="number" step="1" data-spot value="24500"></div>'
      + '    <div class="qp-field"><div class="qp-label">IV (σ %)</div><input class="qp-input" type="number" step="0.5" data-sigma value="14"></div>'
      + '    <div class="qp-field"><div class="qp-label">Days to expiry</div><input class="qp-input" type="number" step="1" min="1" data-dte value="14"></div>'
      + '    <div class="qp-field">'
      + '      <div class="qp-label">Template</div>'
      + '      <select class="qp-select" data-template>'
      +        templateOptions()
      + '      </select>'
      + '    </div>'
      + '    <button class="qp-btn ghost" data-add style="width:100%;margin-top:6px;">+ Add custom leg</button>'
      + '  </div>'

        // OUTPUTS + CHART
      + '  <div>'
      + '    <div class="qp-outputs">'
      + '      <div class="qp-out"><div class="qp-out-lbl">Max profit</div><div class="qp-out-val bull" data-out="maxProfit">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Max loss</div><div class="qp-out-val bear" data-out="maxLoss">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Net premium</div><div class="qp-out-val" data-out="netPrem">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Breakeven(s)</div><div class="qp-out-val" style="font-size:14px;" data-out="breakeven">—</div></div>'
      + '      <div class="qp-out"><div class="qp-out-lbl">Margin <span style="font-size:8px;color:#6a7484;">EST.</span></div><div class="qp-out-val" style="font-size:15px;" data-out="margin">—</div></div>'
      + '    </div>'

      + '    <div class="qp-chart">'
      + '      <div class="qp-chart-title">Combined payoff'
      + '        <span class="legend">'
      + '          <span style="color:#dde3ef;">Expiry</span>'
      + '          <span style="color:#adc6ff;">Today</span>'
      + '          <span style="color:#6ddec0;">Spot</span>'
      + '          <span style="color:#ffc85a;">Breakeven</span>'
      + '        </span>'
      + '      </div>'
      + '      <div style="height:340px;padding:8px;"><canvas id="qpPayoffChart"></canvas></div>'
      + '    </div>'

      + '    <div class="qp-card" style="margin-top:14px;">'
      + '      <div class="qp-card-title">Legs</div>'
      + '      <table class="qp-legs">'
      + '        <thead><tr><th>Type</th><th>Strike</th><th>Qty</th><th>Premium / Entry</th><th></th></tr></thead>'
      + '        <tbody id="qpLegsBody"></tbody>'
      + '      </table>'
      + '    </div>'
      + '  </div>'
      + '</div>'

        // CONCEPT
      + '<div class="qp-panel is-open">'
      + '  <div class="qp-panel-head">▾ Concept — Combined payoff <span class="caret">&#9662;</span></div>'
      + '  <div class="qp-panel-body">'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\Pi(S) = \\sum_i q_i \\cdot \\text{payoff}_i(S)"></div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\text{Long call}: \\max(S - K, 0) - \\text{premium}, \\quad \\text{Long put}: \\max(K - S, 0) - \\text{premium}"></div>'
      + '    <div class="qp-concept-formula" data-katex-display data-katex="\\text{Short call}: \\text{premium} - \\max(S - K, 0), \\quad \\text{Short put}: \\text{premium} - \\max(K - S, 0)"></div>'
      + '    <div class="qp-concept-intuition">The expiry curve is piecewise-linear — each option leg contributes a "kink" at its strike. The "today" curve uses Black-Scholes to mark each leg at current spot, IV, and remaining DTE. Both converge at expiry. Breakevens are where the combined expiry curve crosses zero. Margin shown is a rough SPAN approximation (max-loss + buffer) — actual broker margin will differ.</div>'
      + '  </div>'
      + '</div>'

        // EXAMPLES
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
  function templateOptions() {
    var opts = [
      ['long_call', 'Long Call'], ['long_put', 'Long Put'],
      ['short_call', 'Short Call'], ['short_put', 'Short Put'],
      ['bull_call', 'Bull Call Spread'], ['bear_put', 'Bear Put Spread'],
      ['iron_condor', 'Iron Condor'], ['butterfly', 'Butterfly'],
      ['straddle', 'Long Straddle'], ['strangle', 'Long Strangle'],
      ['covered_call', 'Covered Call'], ['collar', 'Collar'],
    ];
    return opts.map(function (o) { return '<option value="' + o[0] + '"' + (o[0]==='iron_condor'?' selected':'') + '>' + o[1] + '</option>'; }).join('');
  }
})();
