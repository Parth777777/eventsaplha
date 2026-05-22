/* quant.js — Pure math library for the Quant Playground.
 * No DOM, no fetch, no dependencies. All functions are pure.
 * Attach to window.Quant. Test from devtools: Quant.bsPrice({S:100,K:100,T:1,r:0.05,sigma:0.2,q:0,type:'call'}).
 *
 * Sections:
 *   - Distributions (normCdf, normPdf, normInv)
 *   - Black-Scholes (price, Greeks, implied vol)
 *   - Monte Carlo (GBM paths, terminal distribution, barrier touch)
 *   - Portfolio stats (returns, Sharpe, Sortino, max DD, correlation, beta)
 *   - Position sizing (Kelly symmetric + asymmetric)
 *   - Option payoff (per-leg + combined, breakeven solver, summary stats)
 *   - VaR (historical + parametric)
 *   - RNG helpers (Box-Muller, seeded LCG for reproducibility)
 *   - Self-test runner: Quant.__selfTest() in console.
 */
(function (root) {
  'use strict';

  // ────────────────────────────────────────────────────────────────
  // DISTRIBUTIONS
  // ────────────────────────────────────────────────────────────────

  // Standard-normal PDF
  function normPdf(x) {
    return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
  }

  // Standard-normal CDF via Abramowitz & Stegun 26.2.17 (max error ~7.5e-8)
  function normCdf(x) {
    if (x === 0) return 0.5;
    var sign = x < 0 ? -1 : 1;
    var ax = Math.abs(x);
    var k = 1 / (1 + 0.2316419 * ax);
    var w = ((((1.330274429 * k - 1.821255978) * k + 1.781477937) * k - 0.356563782) * k + 0.319381530) * k;
    var cdf = 1 - normPdf(ax) * w;
    return 0.5 * (1 + sign * (2 * cdf - 1));
  }

  // Inverse standard-normal CDF (quantile) — Beasley-Springer-Moro
  function normInv(p) {
    if (p <= 0 || p >= 1) throw new RangeError('normInv: p must be in (0,1)');
    var a = [-39.69683028665376, 220.9460984245205, -275.9285104469687,
             138.3577518672690, -30.66479806614716, 2.506628277459239];
    var b = [-54.47609879822406, 161.5858368580409, -155.6989798598866,
             66.80131188771972, -13.28068155288572];
    var c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838,
             -2.549732539343734, 4.374664141464968, 2.938163982698783];
    var d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996,
             3.754408661907416];
    var pLow = 0.02425, pHigh = 1 - pLow, q, r;
    if (p < pLow) {
      q = Math.sqrt(-2 * Math.log(p));
      return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
             ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
    }
    if (p <= pHigh) {
      q = p - 0.5; r = q * q;
      return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q /
             (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
    }
    q = Math.sqrt(-2 * Math.log(1 - p));
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }

  // ────────────────────────────────────────────────────────────────
  // BLACK-SCHOLES (with continuous dividend yield q)
  // ────────────────────────────────────────────────────────────────

  // Internal d1, d2
  function _d1(S, K, T, r, sigma, q) {
    return (Math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * Math.sqrt(T));
  }
  function _d2(d1, sigma, T) { return d1 - sigma * Math.sqrt(T); }

  function bsPrice(p) {
    var S = +p.S, K = +p.K, T = +p.T, r = +p.r, sigma = +p.sigma;
    var q = p.q != null ? +p.q : 0;
    var type = (p.type || 'call').toLowerCase();
    if (!(S > 0 && K > 0 && T > 0 && sigma > 0)) {
      // Degenerate: T=0 or sigma=0 → intrinsic value only
      var intrinsic = type === 'call' ? Math.max(S - K, 0) : Math.max(K - S, 0);
      return Math.exp(-r * Math.max(T, 0)) * intrinsic;
    }
    var d1 = _d1(S, K, T, r, sigma, q);
    var d2 = _d2(d1, sigma, T);
    if (type === 'call') {
      return S * Math.exp(-q * T) * normCdf(d1) - K * Math.exp(-r * T) * normCdf(d2);
    }
    return K * Math.exp(-r * T) * normCdf(-d2) - S * Math.exp(-q * T) * normCdf(-d1);
  }

  // Returns { delta, gamma, theta (per CALENDAR day), vega (per 1% IV change), rho (per 1% rate change) }
  function bsGreeks(p) {
    var S = +p.S, K = +p.K, T = +p.T, r = +p.r, sigma = +p.sigma;
    var q = p.q != null ? +p.q : 0;
    var type = (p.type || 'call').toLowerCase();
    if (!(S > 0 && K > 0 && T > 0 && sigma > 0)) {
      return { delta: 0, gamma: 0, theta: 0, vega: 0, rho: 0 };
    }
    var d1 = _d1(S, K, T, r, sigma, q);
    var d2 = _d2(d1, sigma, T);
    var Nd1 = normCdf(d1), Nd2 = normCdf(d2);
    var nd1 = normPdf(d1);
    var sqrtT = Math.sqrt(T);
    var expQT = Math.exp(-q * T), expRT = Math.exp(-r * T);

    var delta, theta, rho;
    if (type === 'call') {
      delta = expQT * Nd1;
      theta = -(S * expQT * nd1 * sigma) / (2 * sqrtT)
              - r * K * expRT * Nd2
              + q * S * expQT * Nd1;
      rho = K * T * expRT * Nd2;
    } else {
      delta = expQT * (Nd1 - 1);
      theta = -(S * expQT * nd1 * sigma) / (2 * sqrtT)
              + r * K * expRT * normCdf(-d2)
              - q * S * expQT * normCdf(-d1);
      rho = -K * T * expRT * normCdf(-d2);
    }
    var gamma = (expQT * nd1) / (S * sigma * sqrtT);
    var vega = S * expQT * nd1 * sqrtT;
    return {
      delta: delta,
      gamma: gamma,
      // Convert annual theta → per-calendar-day
      theta: theta / 365,
      // Convert vega → per 1% (0.01) change in sigma
      vega: vega / 100,
      // Convert rho → per 1% (0.01) change in rate
      rho: rho / 100,
    };
  }

  // Implied vol — Newton-Raphson with bisection fallback. Returns NaN if no solution.
  function bsImpliedVol(p) {
    var S = +p.S, K = +p.K, T = +p.T, r = +p.r, marketPrice = +p.marketPrice;
    var q = p.q != null ? +p.q : 0;
    var type = (p.type || 'call').toLowerCase();
    if (!(S > 0 && K > 0 && T > 0 && marketPrice > 0)) return NaN;
    // Sanity-check arbitrage bounds
    var intrinsic = type === 'call'
      ? Math.max(S * Math.exp(-q * T) - K * Math.exp(-r * T), 0)
      : Math.max(K * Math.exp(-r * T) - S * Math.exp(-q * T), 0);
    var upper = type === 'call' ? S * Math.exp(-q * T) : K * Math.exp(-r * T);
    if (marketPrice < intrinsic - 1e-6 || marketPrice > upper + 1e-6) return NaN;

    var sigma = 0.3; // seed
    for (var i = 0; i < 60; i++) {
      var price = bsPrice({ S: S, K: K, T: T, r: r, sigma: sigma, q: q, type: type });
      var diff = price - marketPrice;
      if (Math.abs(diff) < 1e-6) return sigma;
      var v = bsGreeks({ S: S, K: K, T: T, r: r, sigma: sigma, q: q, type: type }).vega * 100; // un-scale per-1%
      if (v < 1e-8) break;
      sigma -= diff / v;
      if (sigma <= 1e-6) sigma = 1e-6;
      if (sigma > 5) sigma = 5;
    }
    // Bisection fallback
    var lo = 1e-4, hi = 5;
    for (var j = 0; j < 100; j++) {
      var mid = 0.5 * (lo + hi);
      var pm = bsPrice({ S: S, K: K, T: T, r: r, sigma: mid, q: q, type: type });
      if (Math.abs(pm - marketPrice) < 1e-6) return mid;
      if (pm < marketPrice) lo = mid; else hi = mid;
    }
    return 0.5 * (lo + hi);
  }

  // ────────────────────────────────────────────────────────────────
  // RNG — Box-Muller + optional seeded LCG for reproducibility
  // ────────────────────────────────────────────────────────────────

  function _lcg(seed) {
    var state = (seed >>> 0) || 1;
    return function () {
      state = (state * 1664525 + 1013904223) >>> 0;
      return state / 4294967296;
    };
  }

  // Returns a fn () → standard-normal draw. seed=null uses Math.random.
  function normRng(seed) {
    var u = (seed == null) ? Math.random : _lcg(seed);
    var spare = null;
    return function () {
      if (spare !== null) { var v = spare; spare = null; return v; }
      var u1, u2;
      do { u1 = u(); } while (u1 <= 1e-12);
      u2 = u();
      var mag = Math.sqrt(-2 * Math.log(u1));
      spare = mag * Math.sin(2 * Math.PI * u2);
      return mag * Math.cos(2 * Math.PI * u2);
    };
  }

  // ────────────────────────────────────────────────────────────────
  // MONTE CARLO — Geometric Brownian Motion
  // ────────────────────────────────────────────────────────────────

  // Returns Float64Array[paths][steps+1] flattened as (paths * (steps+1))
  // Use Quant.mcPath(out, i, t, steps) helper to read.
  function gbmPaths(p) {
    var S0 = +p.S0, mu = +p.mu, sigma = +p.sigma;
    var T = +p.T;                          // years
    var steps = Math.max(1, p.steps | 0);
    var paths = Math.max(1, p.paths | 0);
    var dt = T / steps;
    var drift = (mu - 0.5 * sigma * sigma) * dt;
    var diff = sigma * Math.sqrt(dt);
    var rng = normRng(p.seed != null ? p.seed : null);
    var out = new Float64Array(paths * (steps + 1));
    for (var i = 0; i < paths; i++) {
      var base = i * (steps + 1);
      out[base] = S0;
      for (var t = 1; t <= steps; t++) {
        out[base + t] = out[base + t - 1] * Math.exp(drift + diff * rng());
      }
    }
    out.__paths = paths;
    out.__steps = steps;
    return out;
  }

  function mcPath(out, pathIdx, stepIdx) {
    return out[pathIdx * (out.__steps + 1) + stepIdx];
  }

  // Distribution of terminal (last-step) prices across all paths
  function terminalDistribution(out) {
    var paths = out.__paths, steps = out.__steps;
    var arr = new Float64Array(paths);
    var sum = 0;
    for (var i = 0; i < paths; i++) {
      arr[i] = out[i * (steps + 1) + steps];
      sum += arr[i];
    }
    var mean = sum / paths;
    var ss = 0;
    for (var j = 0; j < paths; j++) { var d = arr[j] - mean; ss += d * d; }
    var std = Math.sqrt(ss / paths);
    var sorted = Array.from(arr).sort(function (a, b) { return a - b; });
    return {
      mean: mean, std: std,
      p1: _pct(sorted, 0.01), p5: _pct(sorted, 0.05), p25: _pct(sorted, 0.25),
      p50: _pct(sorted, 0.50), p75: _pct(sorted, 0.75),
      p95: _pct(sorted, 0.95), p99: _pct(sorted, 0.99),
      min: sorted[0], max: sorted[sorted.length - 1],
      values: sorted,
    };
  }

  function _pct(sorted, p) {
    var idx = (sorted.length - 1) * p;
    var lo = Math.floor(idx), hi = Math.ceil(idx);
    if (lo === hi) return sorted[lo];
    return sorted[lo] + (idx - lo) * (sorted[hi] - sorted[lo]);
  }

  // Per-step percentile bands (for fan chart). Returns {p5: [...steps+1], p50: [...], p95: [...]}
  function pathBands(out) {
    var paths = out.__paths, steps = out.__steps;
    var p5 = new Array(steps + 1), p50 = new Array(steps + 1), p95 = new Array(steps + 1);
    var col = new Float64Array(paths);
    for (var t = 0; t <= steps; t++) {
      for (var i = 0; i < paths; i++) col[i] = out[i * (steps + 1) + t];
      var sorted = Array.from(col).sort(function (a, b) { return a - b; });
      p5[t] = _pct(sorted, 0.05);
      p50[t] = _pct(sorted, 0.50);
      p95[t] = _pct(sorted, 0.95);
    }
    return { p5: p5, p50: p50, p95: p95 };
  }

  // Probability that any path touches the barrier. direction: 'above'|'below'
  function probTouch(out, barrier, direction) {
    var paths = out.__paths, steps = out.__steps;
    var hits = 0;
    var above = (direction || 'above') === 'above';
    for (var i = 0; i < paths; i++) {
      var base = i * (steps + 1);
      for (var t = 0; t <= steps; t++) {
        var s = out[base + t];
        if (above ? s >= barrier : s <= barrier) { hits++; break; }
      }
    }
    return hits / paths;
  }

  // ────────────────────────────────────────────────────────────────
  // PORTFOLIO STATS
  // ────────────────────────────────────────────────────────────────

  function dailyReturns(closes) {
    var out = new Array(Math.max(0, closes.length - 1));
    for (var i = 1; i < closes.length; i++) out[i - 1] = closes[i] / closes[i - 1] - 1;
    return out;
  }

  function _mean(arr) {
    if (!arr.length) return 0;
    var s = 0; for (var i = 0; i < arr.length; i++) s += arr[i];
    return s / arr.length;
  }
  function _std(arr, mean, ddof) {
    if (arr.length < 2) return 0;
    var m = mean != null ? mean : _mean(arr);
    var s = 0; for (var i = 0; i < arr.length; i++) { var d = arr[i] - m; s += d * d; }
    return Math.sqrt(s / (arr.length - (ddof != null ? ddof : 1)));
  }

  // Annualised Sharpe assuming rf is annual; daily returns scaled by sqrt(periods).
  function sharpe(returns, rf, periods) {
    rf = rf != null ? rf : 0.065;
    periods = periods != null ? periods : 252;
    if (returns.length < 2) return 0;
    var mu = _mean(returns);
    var sigma = _std(returns, mu, 1);
    if (sigma === 0) return 0;
    var dailyRf = rf / periods;
    return ((mu - dailyRf) / sigma) * Math.sqrt(periods);
  }

  function sortino(returns, rf, periods) {
    rf = rf != null ? rf : 0.065;
    periods = periods != null ? periods : 252;
    if (returns.length < 2) return 0;
    var dailyRf = rf / periods;
    var mu = _mean(returns);
    var downside = 0, n = 0;
    for (var i = 0; i < returns.length; i++) {
      var ex = returns[i] - dailyRf;
      if (ex < 0) { downside += ex * ex; n++; }
    }
    if (n === 0) return Infinity;
    var dd = Math.sqrt(downside / n);
    if (dd === 0) return Infinity;
    return ((mu - dailyRf) / dd) * Math.sqrt(periods);
  }

  // Equity curve from returns (compounded)
  function equityCurve(returns, startCapital) {
    var s = startCapital != null ? startCapital : 1;
    var out = [s];
    for (var i = 0; i < returns.length; i++) {
      s = s * (1 + returns[i]);
      out.push(s);
    }
    return out;
  }

  // Returns { maxDD, peakIdx, troughIdx, recoveryIdx, durationDays }
  function maxDrawdown(equity) {
    var peak = equity[0], peakIdx = 0;
    var maxDD = 0, troughIdx = 0, mddPeakIdx = 0;
    for (var i = 1; i < equity.length; i++) {
      if (equity[i] > peak) { peak = equity[i]; peakIdx = i; }
      var dd = (peak - equity[i]) / peak;
      if (dd > maxDD) { maxDD = dd; troughIdx = i; mddPeakIdx = peakIdx; }
    }
    // Recovery: first index after troughIdx where equity ≥ equity[mddPeakIdx]
    var recoveryIdx = -1;
    for (var j = troughIdx + 1; j < equity.length; j++) {
      if (equity[j] >= equity[mddPeakIdx]) { recoveryIdx = j; break; }
    }
    return {
      maxDD: maxDD,
      peakIdx: mddPeakIdx,
      troughIdx: troughIdx,
      recoveryIdx: recoveryIdx,
      durationDays: (recoveryIdx > 0 ? recoveryIdx : equity.length - 1) - mddPeakIdx,
    };
  }

  // Pearson correlation
  function correlation(x, y) {
    var n = Math.min(x.length, y.length);
    if (n < 2) return 0;
    var mx = 0, my = 0;
    for (var i = 0; i < n; i++) { mx += x[i]; my += y[i]; }
    mx /= n; my /= n;
    var num = 0, sx = 0, sy = 0;
    for (var j = 0; j < n; j++) {
      var dx = x[j] - mx, dy = y[j] - my;
      num += dx * dy; sx += dx * dx; sy += dy * dy;
    }
    var den = Math.sqrt(sx * sy);
    return den === 0 ? 0 : num / den;
  }

  // returnsByTicker: { TKA: [r1,r2,...], TKB: [...] }
  // Returns { tickers, matrix } — matrix[i][j] = correlation(tickers[i], tickers[j])
  function correlationMatrix(returnsByTicker) {
    var tickers = Object.keys(returnsByTicker);
    var n = tickers.length;
    var m = new Array(n);
    for (var i = 0; i < n; i++) {
      m[i] = new Array(n);
      for (var j = 0; j < n; j++) {
        if (i === j) m[i][j] = 1;
        else if (j < i) m[i][j] = m[j][i];
        else m[i][j] = correlation(returnsByTicker[tickers[i]], returnsByTicker[tickers[j]]);
      }
    }
    return { tickers: tickers, matrix: m };
  }

  // OLS beta of asset returns vs benchmark returns
  function beta(returns, benchmarkReturns) {
    var n = Math.min(returns.length, benchmarkReturns.length);
    if (n < 2) return 0;
    var mr = 0, mb = 0;
    for (var i = 0; i < n; i++) { mr += returns[i]; mb += benchmarkReturns[i]; }
    mr /= n; mb /= n;
    var cov = 0, varB = 0;
    for (var j = 0; j < n; j++) {
      var dr = returns[j] - mr, db = benchmarkReturns[j] - mb;
      cov += dr * db; varB += db * db;
    }
    return varB === 0 ? 0 : cov / varB;
  }

  // ────────────────────────────────────────────────────────────────
  // POSITION SIZING — Kelly
  // ────────────────────────────────────────────────────────────────

  // Symmetric Kelly. winRate ∈ [0,1], avgWin & avgLoss in absolute ₹ (loss is positive).
  // Returns optimal fraction f* of bankroll to risk. Clamped to [0, 1].
  function kelly(p) {
    var pwin = +p.winRate;
    var W = Math.abs(+p.avgWin), L = Math.abs(+p.avgLoss);
    if (!(L > 0) || !(pwin >= 0 && pwin <= 1)) return 0;
    var b = W / L;
    var q = 1 - pwin;
    var f = (pwin * b - q) / b;
    return Math.max(0, Math.min(1, f));
  }

  // Asymmetric Kelly for capped payoffs (e.g., defined-risk option strategies).
  // pWin = probability of MAX win, maxWin = ₹ profit if win, maxLoss = ₹ loss if lose.
  function kellyAsymmetric(p) {
    var pwin = +p.pWin, W = Math.abs(+p.maxWin), L = Math.abs(+p.maxLoss);
    if (!(L > 0 && W > 0)) return 0;
    var q = 1 - pwin;
    var f = (pwin / L) - (q / W);
    // Normalise to fraction of bankroll (multiply by max risk per unit)
    var fFraction = f * L;
    return Math.max(0, Math.min(1, fFraction));
  }

  // ────────────────────────────────────────────────────────────────
  // OPTION PAYOFF
  // ────────────────────────────────────────────────────────────────

  // leg = { type, strike, qty, premium }
  // type ∈ 'long_call' | 'short_call' | 'long_put' | 'short_put' | 'long_stock' | 'short_stock'
  // qty in CONTRACTS or SHARES (caller decides — lot size is upstream concern).
  // premium = ₹ paid (long) or received (short, declared as positive ₹ received).
  function legPayoffAtExpiry(leg, S) {
    var k = +leg.strike, qty = +leg.qty || 1, prem = +leg.premium || 0;
    switch ((leg.type || '').toLowerCase()) {
      case 'long_call':   return qty * (Math.max(S - k, 0) - prem);
      case 'short_call':  return qty * (prem - Math.max(S - k, 0));
      case 'long_put':    return qty * (Math.max(k - S, 0) - prem);
      case 'short_put':   return qty * (prem - Math.max(k - S, 0));
      case 'long_stock':  return qty * (S - prem);     // prem = entry price
      case 'short_stock': return qty * (prem - S);
      default: return 0;
    }
  }

  // Per-leg P&L "today" (T-{daysToExpiry}) using BS for option legs, intrinsic for stock.
  function legPayoffToday(leg, S, daysToExpiry, sigma, r, q) {
    sigma = sigma != null ? sigma : 0.2;
    r = r != null ? r : 0.065;
    q = q != null ? q : 0;
    var T = Math.max(daysToExpiry, 0) / 365;
    var qty = +leg.qty || 1, prem = +leg.premium || 0;
    var t = (leg.type || '').toLowerCase();
    if (t === 'long_stock')  return qty * (S - prem);
    if (t === 'short_stock') return qty * (prem - S);
    var isCall = t.indexOf('call') >= 0;
    var isLong = t.indexOf('long') >= 0;
    var price = bsPrice({ S: S, K: +leg.strike, T: T, r: r, sigma: sigma, q: q, type: isCall ? 'call' : 'put' });
    return isLong ? qty * (price - prem) : qty * (prem - price);
  }

  // Combined payoff over a price range. Returns { S: [], expiry: [], today: [] }
  function combinedPayoff(legs, sRange, opts) {
    opts = opts || {};
    var min = sRange[0], max = sRange[1];
    var steps = opts.steps || 200;
    var dte = opts.daysToExpiry, sigma = opts.sigma, r = opts.r, q = opts.q;
    var S = new Array(steps + 1), exp = new Array(steps + 1), today = new Array(steps + 1);
    for (var i = 0; i <= steps; i++) {
      var s = min + (max - min) * (i / steps);
      S[i] = s;
      var pExp = 0, pToday = 0;
      for (var k = 0; k < legs.length; k++) {
        pExp += legPayoffAtExpiry(legs[k], s);
        if (dte != null) pToday += legPayoffToday(legs[k], s, dte, sigma, r, q);
      }
      exp[i] = pExp;
      today[i] = dte != null ? pToday : null;
    }
    return { S: S, expiry: exp, today: today };
  }

  // Summary stats: max profit, max loss, breakevens, net premium.
  // Breakevens found by linear interpolation between sign changes in the expiry curve.
  function payoffStats(legs, sRange, opts) {
    var range = sRange || _autoRange(legs);
    var poff = combinedPayoff(legs, range, opts || { steps: 1000 });
    var maxProfit = -Infinity, maxLoss = Infinity;
    var breakevens = [];
    for (var i = 0; i < poff.expiry.length; i++) {
      if (poff.expiry[i] > maxProfit) maxProfit = poff.expiry[i];
      if (poff.expiry[i] < maxLoss) maxLoss = poff.expiry[i];
      if (i > 0) {
        var a = poff.expiry[i - 1], b = poff.expiry[i];
        if ((a < 0 && b > 0) || (a > 0 && b < 0) || a === 0) {
          var sa = poff.S[i - 1], sb = poff.S[i];
          var be = a === 0 ? sa : sa + (sb - sa) * (-a / (b - a));
          breakevens.push(be);
        }
      }
    }
    var netPremium = 0;
    for (var j = 0; j < legs.length; j++) {
      var t = (legs[j].type || '').toLowerCase();
      var qty = +legs[j].qty || 1, prem = +legs[j].premium || 0;
      if (t === 'long_call' || t === 'long_put') netPremium -= qty * prem;
      else if (t === 'short_call' || t === 'short_put') netPremium += qty * prem;
      // stock legs don't contribute to "premium"; caller tracks entry separately
    }
    return {
      maxProfit: maxProfit === Infinity ? null : maxProfit,
      maxLoss: maxLoss === -Infinity ? null : maxLoss,
      breakevens: breakevens,
      netPremium: netPremium,
      sRange: range,
    };
  }

  // Auto-pick a sensible S range from strikes (±40% around midpoint of strikes/spots)
  function _autoRange(legs) {
    var levels = [];
    for (var i = 0; i < legs.length; i++) {
      if (legs[i].strike) levels.push(+legs[i].strike);
      if ((legs[i].type || '').indexOf('stock') >= 0 && legs[i].premium) levels.push(+legs[i].premium);
    }
    if (!levels.length) return [50, 150];
    levels.sort(function (a, b) { return a - b; });
    var mid = (levels[0] + levels[levels.length - 1]) / 2;
    return [mid * 0.6, mid * 1.4];
  }

  // ────────────────────────────────────────────────────────────────
  // VALUE-AT-RISK
  // ────────────────────────────────────────────────────────────────

  // Historical VaR — 1-day VaR as a positive ₹ loss given a series of historical returns.
  function historicalVaR(returns, confidence, position) {
    confidence = confidence != null ? confidence : 0.95;
    position = position != null ? position : 1;
    if (!returns.length) return 0;
    var sorted = returns.slice().sort(function (a, b) { return a - b; });
    var q = _pct(sorted, 1 - confidence);
    return -q * position; // positive number = ₹ at risk
  }

  // Parametric (variance-covariance) VaR assuming normal returns
  function parametricVaR(p) {
    var mean = +p.mean, std = +p.std;
    var position = p.position != null ? +p.position : 1;
    var confidence = p.confidence != null ? +p.confidence : 0.95;
    var z = normInv(1 - confidence); // negative (e.g., -1.645 for 95%)
    var var1day = -(mean + z * std);
    return Math.max(0, var1day) * position;
  }

  // ────────────────────────────────────────────────────────────────
  // ANNUALISATION HELPERS
  // ────────────────────────────────────────────────────────────────

  function annualisedReturn(returns, periods) {
    periods = periods != null ? periods : 252;
    var mu = _mean(returns);
    return Math.pow(1 + mu, periods) - 1;
  }
  function annualisedVol(returns, periods) {
    periods = periods != null ? periods : 252;
    return _std(returns) * Math.sqrt(periods);
  }
  function calmar(returns, periods) {
    var eq = equityCurve(returns);
    var dd = maxDrawdown(eq).maxDD;
    if (dd === 0) return Infinity;
    return annualisedReturn(returns, periods) / dd;
  }

  // ────────────────────────────────────────────────────────────────
  // SELF-TEST — call Quant.__selfTest() from console
  // ────────────────────────────────────────────────────────────────

  function __selfTest() {
    var pass = 0, fail = 0;
    function eq(name, actual, expected, tol) {
      tol = tol || 1e-3;
      var ok = Math.abs(actual - expected) < tol;
      if (ok) { pass++; console.log('%c PASS %c ' + name, 'background:#1d4d3a;color:#6ddec0', '', '→', actual.toFixed(6)); }
      else    { fail++; console.error('FAIL ' + name + ' — expected ' + expected + ', got ' + actual); }
    }
    // Textbook BS: S=100, K=100, T=1, r=0.05, σ=0.2, q=0 → call ≈ 10.4506
    eq('BS call (textbook)', bsPrice({ S: 100, K: 100, T: 1, r: 0.05, sigma: 0.2, q: 0, type: 'call' }), 10.4506);
    eq('BS put (textbook)',  bsPrice({ S: 100, K: 100, T: 1, r: 0.05, sigma: 0.2, q: 0, type: 'put' }), 5.5735);
    // Put-call parity: C - P = S*e^(-qT) - K*e^(-rT)
    var c = bsPrice({ S: 100, K: 95, T: 0.5, r: 0.05, sigma: 0.25, q: 0.02, type: 'call' });
    var p = bsPrice({ S: 100, K: 95, T: 0.5, r: 0.05, sigma: 0.25, q: 0.02, type: 'put' });
    eq('Put-call parity', c - p, 100 * Math.exp(-0.02 * 0.5) - 95 * Math.exp(-0.05 * 0.5));
    // Greeks: ATM call delta ≈ N(d1) ≈ 0.637 for the textbook case
    eq('ATM call delta', bsGreeks({ S: 100, K: 100, T: 1, r: 0.05, sigma: 0.2, q: 0, type: 'call' }).delta, 0.6368);
    // IV solver round-trip
    var price = bsPrice({ S: 100, K: 105, T: 0.25, r: 0.06, sigma: 0.3, q: 0, type: 'call' });
    eq('IV solver', bsImpliedVol({ S: 100, K: 105, T: 0.25, r: 0.06, marketPrice: price, q: 0, type: 'call' }), 0.30);
    // Kelly: 60% win, 2:1 ratio → 0.4
    eq('Kelly (60%, 2:1)', kelly({ winRate: 0.6, avgWin: 2, avgLoss: 1 }), 0.4);
    // Sharpe: zero-vol series should give 0 or Infinity (we return 0)
    eq('Sharpe (flat)', sharpe([0, 0, 0, 0, 0]), 0);
    // Sharpe of a clean 1bps/day series should be ~10
    var rr = []; for (var i = 0; i < 100; i++) rr.push(0.0001 + (i % 2 ? 0.0001 : -0.0001));
    var s = sharpe(rr, 0, 252);
    if (s > 0) { pass++; console.log('%c PASS %c Sharpe positive on positive-mean series', 'background:#1d4d3a;color:#6ddec0', ''); }
    else { fail++; console.error('FAIL Sharpe sign'); }
    // Max DD on [1, 1.5, 0.75, 1.2] → DD = (1.5 - 0.75)/1.5 = 0.5
    eq('Max DD', maxDrawdown([1, 1.5, 0.75, 1.2]).maxDD, 0.5);
    // Long-call payoff at expiry, S=110, K=100, premium=5 → 5
    eq('Long call payoff (ITM)', legPayoffAtExpiry({ type: 'long_call', strike: 100, qty: 1, premium: 5 }, 110), 5);
    eq('Long call payoff (OTM)', legPayoffAtExpiry({ type: 'long_call', strike: 100, qty: 1, premium: 5 }, 95), -5);
    // Bull call spread breakeven: long 100 @ 3, short 105 @ 1 → net debit 2 → breakeven 102
    var stats = payoffStats([
      { type: 'long_call', strike: 100, qty: 1, premium: 3 },
      { type: 'short_call', strike: 105, qty: 1, premium: 1 }
    ], [80, 130], { steps: 5000 });
    eq('Bull spread breakeven', stats.breakevens[0], 102, 0.05);
    eq('Bull spread max profit', stats.maxProfit, 3, 0.01);
    eq('Bull spread max loss', stats.maxLoss, -2, 0.01);
    // Normal-dist sanity
    eq('normCdf(0)', normCdf(0), 0.5);
    eq('normCdf(1.96)', normCdf(1.96), 0.975);
    eq('normInv(0.975)', normInv(0.975), 1.96, 0.01);
    console.log('\n%c Quant self-test: ' + pass + ' passed, ' + fail + ' failed',
      fail ? 'background:#4d1d1d;color:#ff8e8e;font-weight:bold' : 'background:#1d4d3a;color:#6ddec0;font-weight:bold');
    return { pass: pass, fail: fail };
  }

  // ────────────────────────────────────────────────────────────────
  // EXPORT
  // ────────────────────────────────────────────────────────────────

  root.Quant = {
    // distributions
    normCdf: normCdf, normPdf: normPdf, normInv: normInv,
    // Black-Scholes
    bsPrice: bsPrice, bsGreeks: bsGreeks, bsImpliedVol: bsImpliedVol,
    // Monte Carlo
    gbmPaths: gbmPaths, mcPath: mcPath,
    terminalDistribution: terminalDistribution,
    pathBands: pathBands, probTouch: probTouch,
    normRng: normRng,
    // Portfolio
    dailyReturns: dailyReturns,
    sharpe: sharpe, sortino: sortino,
    equityCurve: equityCurve, maxDrawdown: maxDrawdown,
    correlation: correlation, correlationMatrix: correlationMatrix,
    beta: beta,
    annualisedReturn: annualisedReturn, annualisedVol: annualisedVol, calmar: calmar,
    // Sizing
    kelly: kelly, kellyAsymmetric: kellyAsymmetric,
    // Payoff
    legPayoffAtExpiry: legPayoffAtExpiry, legPayoffToday: legPayoffToday,
    combinedPayoff: combinedPayoff, payoffStats: payoffStats,
    // VaR
    historicalVaR: historicalVaR, parametricVaR: parametricVaR,
    // test
    __selfTest: __selfTest,
  };
})(typeof window !== 'undefined' ? window : globalThis);
