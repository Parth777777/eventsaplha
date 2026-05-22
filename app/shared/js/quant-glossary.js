/* quant-glossary.js — single source of truth for every term in the Quant Playground.
 * Attaches to window.Glossary. Powers:
 *   - tooltips on input labels (Glossary.tooltip(el, term))
 *   - Concept panels (Glossary.formula(term))
 *   - the standalone Glossary tab (Glossary.all(), Glossary.search(q))
 *
 * Each entry shape:
 *   {
 *     id: 'delta',
 *     term: 'Delta (Δ)',
 *     plain: 'one-sentence definition',
 *     formal: 'full paragraph',
 *     units: 'optional unit/range hint for tooltips',
 *     formula: 'LaTeX, no $ delimiters, optional',
 *     example: 'worked example with numbers',
 *     see_also: ['gamma', 'theta'],
 *     used_in: ['greeks', 'payoff'],
 *   }
 */
(function (root) {
  'use strict';

  var TERMS = [
    // ── Spot / strike / moneyness ─────────────────────────────────
    { id: 'spot', term: 'Spot price (S)',
      plain: 'The current market price of the underlying asset.',
      formal: 'The price at which the underlying (stock, index, commodity) is trading right now in the cash market. Black-Scholes uses spot as the starting point for projecting future possible prices.',
      units: '₹ per share / index point',
      example: 'If RELIANCE last traded at ₹2,847, the spot price S = 2847.',
      see_also: ['strike', 'moneyness'], used_in: ['greeks', 'payoff', 'montecarlo'] },

    { id: 'strike', term: 'Strike price (K)',
      plain: 'The price at which an option can be exercised.',
      formal: 'The pre-agreed price at which the holder of a call can buy (or holder of a put can sell) the underlying. Strike is fixed at contract inception. The distance between spot and strike determines whether the option is ITM, ATM, or OTM.',
      units: '₹ per share / index point',
      example: 'A NIFTY 24500 CE call lets the holder buy NIFTY at 24500, regardless of where spot ends up at expiry.',
      see_also: ['spot', 'moneyness', 'premium'], used_in: ['greeks', 'payoff'] },

    { id: 'premium', term: 'Premium',
      plain: 'The price paid (or received) for an option contract.',
      formal: 'Premium is the upfront cost to buy an option, or the credit received for selling one. It has two parts: intrinsic value (how much it would be worth if exercised now) + extrinsic value (time value + IV). Premium decays toward intrinsic value as expiry approaches — this decay is theta.',
      units: '₹ per share (×lot size = total ₹)',
      example: 'A RELIANCE 2900 CE with 7 DTE trades at ₹38. Buyer pays 38 × 250 (lot) = ₹9,500 upfront.',
      see_also: ['intrinsic', 'extrinsic', 'theta'], used_in: ['greeks', 'payoff'] },

    { id: 'intrinsic', term: 'Intrinsic value',
      plain: 'What an option is worth if exercised immediately.',
      formal: 'For a call: max(S − K, 0). For a put: max(K − S, 0). Intrinsic value is non-negative. An OTM option has zero intrinsic value — its entire premium is extrinsic (time + vol).',
      formula: '\\text{Call intrinsic} = \\max(S - K, 0), \\quad \\text{Put intrinsic} = \\max(K - S, 0)',
      example: 'NIFTY at 24700. The 24500 CE has intrinsic value = 200. The 24500 PE has intrinsic = 0 (OTM).',
      see_also: ['extrinsic', 'moneyness'], used_in: ['greeks', 'payoff'] },

    { id: 'extrinsic', term: 'Extrinsic value (time value)',
      plain: 'Premium minus intrinsic value — what you pay for time + uncertainty.',
      formal: 'Extrinsic value = premium − intrinsic value. It compensates the seller for the chance the option goes further ITM before expiry. Driven by DTE and IV. Always positive; decays to zero at expiry.',
      example: 'NIFTY 24500 CE trades at ₹240 with NIFTY at 24700. Intrinsic = 200, extrinsic = 40. That ₹40 evaporates entirely by expiry day.',
      see_also: ['intrinsic', 'theta', 'iv'], used_in: ['greeks'] },

    { id: 'moneyness', term: 'Moneyness (ITM / ATM / OTM)',
      plain: 'Where the strike sits relative to spot.',
      formal: 'ITM (in-the-money): would profit if exercised now (call: S > K, put: S < K). ATM (at-the-money): strike ≈ spot. OTM (out-of-the-money): no intrinsic value. Moneyness drives delta, gamma profile, and premium composition.',
      example: 'Spot 100. 95 CE = ITM (intrinsic 5). 100 CE = ATM. 110 CE = OTM (intrinsic 0).',
      see_also: ['intrinsic', 'delta'], used_in: ['greeks', 'payoff'] },

    { id: 'dte', term: 'Days to expiry (DTE)',
      plain: 'Calendar days until the option contract expires.',
      formal: 'Time remaining until expiry, typically in calendar days. In BS, T is expressed in years (T = DTE / 365). Theta accelerates as DTE shrinks — final week often sees half the remaining time value erode.',
      units: 'days',
      example: 'Weekly NIFTY expiring Thursday, today is Monday → DTE = 3. T = 3/365 ≈ 0.0082 years.',
      see_also: ['theta', 'premium'], used_in: ['greeks', 'payoff', 'montecarlo'] },

    // ── Volatility ────────────────────────────────────────────────
    { id: 'iv', term: 'Implied volatility (IV, σ)',
      plain: "The market's forecast of future volatility, backed out from option prices.",
      formal: 'IV is the σ that, when fed into Black-Scholes, returns the current market price. It is forward-looking and reflects supply/demand for options. Quoted annualised. High IV → expensive options. IV spikes around events (earnings, RBI, budget) and crushes after.',
      units: 'annualised, decimal (0.14 = 14%)',
      example: 'NIFTY ATM weekly trading at IV = 14% implies a 1-day std-dev move of ≈ 14% / √252 = 0.88%.',
      see_also: ['hv', 'iv_rank', 'vega'], used_in: ['greeks', 'montecarlo'] },

    { id: 'hv', term: 'Historical volatility (HV, realised vol)',
      plain: 'Actual past volatility measured from price history.',
      formal: 'Standard deviation of daily log-returns, annualised by √252. Backward-looking, unlike IV. The gap (IV − HV) is the "vol risk premium" — typically positive (option sellers get paid).',
      formula: 'HV = \\text{std}(\\ln(S_t / S_{t-1})) \\cdot \\sqrt{252}',
      example: 'NIFTY 30-day HV = 11% means its actual price wiggle over the last month annualises to 11%.',
      see_also: ['iv'], used_in: ['montecarlo'] },

    { id: 'iv_rank', term: 'IV Rank',
      plain: 'Where current IV sits in its 1-year range, 0–100.',
      formal: 'IV Rank = (IV_now − IV_min_1y) / (IV_max_1y − IV_min_1y) × 100. IVR > 70 = options expensive (favour sellers). IVR < 30 = cheap (favour buyers). Cleaner than raw IV for comparing across tickers.',
      example: 'RELIANCE IV is 28% today. Over the past year it ranged 18%–42%. IVR = (28−18)/(42−18) = 42.',
      see_also: ['iv', 'iv_percentile'], used_in: ['greeks'] },

    { id: 'iv_percentile', term: 'IV Percentile',
      plain: 'Fraction of days in past year IV was below current, 0–100.',
      formal: 'Count of trading days in the last 252 sessions where IV was below today\'s IV, divided by 252. Less skewed by outliers than IV Rank — both should usually agree.',
      see_also: ['iv_rank'], used_in: ['greeks'] },

    // ── Greeks ────────────────────────────────────────────────────
    { id: 'delta', term: 'Delta (Δ)',
      plain: 'How much the option price changes per ₹1 move in the underlying.',
      formal: 'First derivative of option price w.r.t. spot. Call delta ranges 0 → 1 (deep OTM → deep ITM). Put delta ranges −1 → 0. ATM ≈ 0.5 (call) or −0.5 (put). Also approximates the option\'s probability of finishing ITM.',
      formula: '\\Delta_{\\text{call}} = e^{-qT} N(d_1), \\quad \\Delta_{\\text{put}} = e^{-qT} (N(d_1) - 1)',
      units: 'dimensionless, [−1, +1]',
      example: 'RELIANCE 2900 CE has delta 0.45. If spot moves from 2880 → 2890 (+₹10), the option gains roughly 0.45 × 10 = ₹4.50.',
      see_also: ['gamma', 'moneyness'], used_in: ['greeks', 'payoff'] },

    { id: 'gamma', term: 'Gamma (Γ)',
      plain: 'How much delta changes per ₹1 move in the underlying.',
      formal: 'Second derivative of option price w.r.t. spot. Always positive for long options. Highest for ATM short-dated options. High gamma = delta swings fast = position becomes very directional very quickly.',
      formula: '\\Gamma = \\frac{e^{-qT} \\phi(d_1)}{S \\sigma \\sqrt{T}}',
      units: 'per ₹',
      example: 'ATM call with gamma 0.012. Underlying moves up ₹5 → delta increases by 0.012 × 5 = 0.06.',
      see_also: ['delta'], used_in: ['greeks'] },

    { id: 'theta', term: 'Theta (Θ)',
      plain: 'How much option value decays per calendar day, all else equal.',
      formal: 'Time decay. Long options have negative theta (lose value); short options positive (gain). Accelerates non-linearly near expiry — square-root-of-time decay. Theta is what option SELLERS collect.',
      formula: '\\Theta_{\\text{call}} = -\\frac{S e^{-qT} \\phi(d_1) \\sigma}{2\\sqrt{T}} - rKe^{-rT}N(d_2) + qSe^{-qT}N(d_1)',
      units: '₹ per day',
      example: 'NIFTY 24500 CE with 5 DTE has theta = −18. Holding overnight costs you ₹18 per lot, all else equal.',
      see_also: ['vega', 'dte'], used_in: ['greeks', 'payoff'] },

    { id: 'vega', term: 'Vega (ν)',
      plain: 'How much option price changes per 1% change in IV.',
      formal: 'Sensitivity to volatility. Long options have positive vega; short are negative. Vega is highest for ATM long-dated options. Earnings-day buyers eat vega crush — IV drops 30%+ in minutes after results.',
      formula: '\\nu = S e^{-qT} \\phi(d_1) \\sqrt{T}',
      units: '₹ per 1% IV change',
      example: 'Long NIFTY straddle with combined vega 240. If IV drops from 14% → 12% post-event, you lose 2 × 240 = ₹480 per lot.',
      see_also: ['iv', 'theta'], used_in: ['greeks'] },

    { id: 'rho', term: 'Rho (ρ)',
      plain: 'How much option price changes per 1% change in interest rate.',
      formal: 'Sensitivity to risk-free rate. Calls have positive rho, puts negative. Smallest of the Greeks for retail use — only meaningful for long-dated options or in regimes of fast rate change.',
      formula: '\\rho_{\\text{call}} = K T e^{-rT} N(d_2)',
      units: '₹ per 1% rate change',
      example: 'A 1-year ATM call with rho 12 gains ₹12 if RBI hikes 100 bps.',
      see_also: ['delta'], used_in: ['greeks'] },

    // ── Options market metrics ────────────────────────────────────
    { id: 'oi', term: 'Open Interest (OI)',
      plain: 'Total outstanding option contracts not yet closed.',
      formal: 'Number of contracts that have been opened but not yet offset or exercised. Rising OI = new positions building. Falling OI = positions unwinding. Combined with price action it signals conviction.',
      see_also: ['pcr'], used_in: ['greeks'] },

    { id: 'pcr', term: 'Put/Call Ratio (PCR)',
      plain: 'Ratio of put OI to call OI — contrarian sentiment proxy.',
      formal: 'PCR = total put OI / total call OI. PCR > 1.3 often signals over-pessimism (potential bounce). PCR < 0.7 = over-optimism (potential top). Treat as confirmation, not signal.',
      example: 'NIFTY PCR = 0.65 going into expiry. Most option writers are short puts, market is bullish-leaning. Aggressive rally can squeeze put-sellers.',
      see_also: ['oi'], used_in: ['greeks'] },

    { id: 'max_pain', term: 'Max Pain',
      plain: 'Strike where total option buyer losses are maximum at expiry.',
      formal: 'The strike at which the combined value of all open call + put positions is minimum. Theory: option sellers (institutions) pin the price near this strike at expiry to maximise buyer losses. Useful as a magnet level, not a forecast.',
      see_also: ['oi'], used_in: ['greeks'] },

    // ── Strategy structures ───────────────────────────────────────
    { id: 'vertical_spread', term: 'Vertical Spread',
      plain: 'Buy one option, sell another of the same type at a different strike, same expiry.',
      formal: 'Bull call spread = long lower-strike call + short higher-strike call. Bear put spread = long higher-strike put + short lower-strike put. Defined risk + defined reward; both legs share expiry, type, underlying.',
      see_also: ['iron_condor', 'butterfly'], used_in: ['payoff'] },

    { id: 'iron_condor', term: 'Iron Condor',
      plain: 'Sell an OTM call spread + sell an OTM put spread — bet that price stays in a range.',
      formal: '4 legs: short OTM call, long higher OTM call (cap upside risk), short OTM put, long lower OTM put (cap downside risk). Profits if underlying stays between the short strikes at expiry. Premium-collection / theta-positive strategy.',
      example: 'NIFTY at 24500, weekly: sell 24700 CE, buy 24800 CE, sell 24300 PE, buy 24200 PE. Max profit = net credit. Max loss = wing width − credit.',
      see_also: ['vertical_spread', 'butterfly', 'theta'], used_in: ['payoff'] },

    { id: 'butterfly', term: 'Butterfly Spread',
      plain: 'Buy 1, sell 2, buy 1 — narrow profit zone at the middle strike.',
      formal: 'Long lower strike + short 2× middle strike + long upper strike (calls or puts). Maximum profit if underlying pins the middle strike at expiry. Cheap; needs precise direction + pin.',
      see_also: ['iron_condor'], used_in: ['payoff'] },

    { id: 'straddle', term: 'Straddle',
      plain: 'Long call + long put at the same ATM strike — bet on a big move either way.',
      formal: 'Long straddle profits if underlying moves more than the combined premium in either direction. Used pre-event (earnings, RBI). Vulnerable to IV crush post-event. Short straddle = opposite bet (range-bound).',
      example: 'Pre-Infosys results: long 1500 straddle for ₹80 combined. Needs stock to close < 1420 or > 1580 to profit.',
      see_also: ['strangle', 'vega'], used_in: ['payoff'] },

    { id: 'strangle', term: 'Strangle',
      plain: 'Long OTM call + long OTM put — cheaper straddle, needs bigger move.',
      formal: 'Strikes are out-of-the-money on both sides. Lower premium than a straddle but breakevens are further apart. Common pre-event hedge.',
      see_also: ['straddle'], used_in: ['payoff'] },

    { id: 'covered_call', term: 'Covered Call',
      plain: 'Own 100 shares + sell 1 OTM call — yield enhancement on existing long.',
      formal: 'Long stock + short OTM call. Collects premium for capping upside above the strike. Income-generation strategy. Risk = stock falling sharply (same as outright long).',
      see_also: ['collar'], used_in: ['payoff'] },

    { id: 'collar', term: 'Collar',
      plain: 'Long stock + short OTM call + long OTM put — fence the position.',
      formal: 'Wraps a stock position with a short call (caps upside, collects premium) and a long put (caps downside). Often "zero-cost" if call premium = put premium. Used to lock in profits cheaply.',
      see_also: ['covered_call'], used_in: ['payoff'] },

    { id: 'calendar_spread', term: 'Calendar Spread',
      plain: 'Sell near-term + buy further-dated option at the same strike.',
      formal: 'Profits from theta decay on the short leg + time value retained in the long leg. Best in low-IV environments. Vega-positive (gains if IV expands). Needs price to stay near strike.',
      see_also: ['theta', 'vega'], used_in: ['payoff'] },

    // ── Stochastic models ─────────────────────────────────────────
    { id: 'gbm', term: 'Geometric Brownian Motion (GBM)',
      plain: 'The standard model for simulating future stock prices.',
      formal: 'Assumes log-returns are normally distributed and continuous-time price follows dS = μ·S·dt + σ·S·dW. Underpins Black-Scholes. Real markets deviate: fat tails, volatility clustering, jumps — GBM is a starting point, not the truth.',
      formula: 'dS = \\mu S \\, dt + \\sigma S \\, dW',
      see_also: ['drift', 'wiener'], used_in: ['montecarlo', 'greeks'] },

    { id: 'drift', term: 'Drift (μ)',
      plain: "Expected average return of an asset per unit time.",
      formal: 'The deterministic component of GBM. In real-world simulations, drift is often estimated from historical mean return. In risk-neutral pricing (BS), drift is replaced by the risk-free rate.',
      units: 'annualised, decimal',
      see_also: ['gbm'], used_in: ['montecarlo'] },

    { id: 'wiener', term: 'Wiener process (W)',
      plain: 'Mathematical model for pure randomness over time.',
      formal: 'Continuous-time random walk where increments are normal with variance proportional to time elapsed. dW ~ N(0, dt). The "random kick" driving GBM.',
      see_also: ['gbm'], used_in: ['montecarlo'] },

    // ── Risk metrics ──────────────────────────────────────────────
    { id: 'var', term: 'Value-at-Risk (VaR)',
      plain: 'Worst expected loss at a confidence level over a time horizon.',
      formal: '95% 1-day VaR of ₹50,000 means: on a typical day, there is 95% confidence the loss will not exceed ₹50,000. The other 5% it could be worse — VaR says nothing about HOW much worse (that\'s CVaR).',
      formula: 'VaR_\\alpha = -\\text{percentile}(R, 1-\\alpha) \\cdot \\text{position}',
      example: 'Portfolio worth ₹10L, daily std-dev 1.2%, normal returns. 95% VaR ≈ 1.645 × 1.2% × 10L = ₹19,740.',
      see_also: ['cvar', 'max_drawdown'], used_in: ['montecarlo', 'portfolio'] },

    { id: 'cvar', term: 'Conditional VaR (CVaR / Expected Shortfall)',
      plain: 'Average loss in the worst α% of cases — the "what if VaR is breached".',
      formal: 'Mean of returns worse than the VaR threshold. More conservative than VaR; captures tail severity. Preferred for fat-tailed assets (equities, options).',
      see_also: ['var'], used_in: ['portfolio'] },

    { id: 'max_drawdown', term: 'Max Drawdown',
      plain: 'Largest peak-to-trough decline in equity over a period.',
      formal: 'Max over t of (peak − trough) / peak. Measures worst historical pain. Time underwater (recovery duration) matters as much as the depth.',
      formula: '\\text{MDD} = \\max_t \\frac{\\text{peak}_t - \\text{trough}_t}{\\text{peak}_t}',
      example: 'Portfolio peaks at ₹12L, falls to ₹9L, recovers. Max DD = (12 − 9) / 12 = 25%.',
      see_also: ['calmar'], used_in: ['portfolio'] },

    // ── Performance ratios ────────────────────────────────────────
    { id: 'sharpe', term: 'Sharpe Ratio',
      plain: 'Excess return per unit of total volatility — risk-adjusted return.',
      formal: '(annual return − risk-free rate) / annualised volatility. Sharpe > 1 is decent, > 2 is rare, > 3 is suspicious (likely overfit). Penalises upside vol same as downside, which is its main critique.',
      formula: '\\text{Sharpe} = \\frac{E[R] - R_f}{\\sigma_R} \\cdot \\sqrt{252}',
      example: 'Strategy returns 18% annually with 15% σ. Risk-free 6.5%. Sharpe = (18 − 6.5) / 15 = 0.77.',
      see_also: ['sortino', 'calmar'], used_in: ['portfolio'] },

    { id: 'sortino', term: 'Sortino Ratio',
      plain: 'Like Sharpe but only penalises downside volatility.',
      formal: 'Numerator same as Sharpe. Denominator uses only negative excess returns. Better fit for strategies with positive skew (upside vol shouldn\'t hurt you).',
      see_also: ['sharpe'], used_in: ['portfolio'] },

    { id: 'calmar', term: 'Calmar Ratio',
      plain: 'Annual return divided by max drawdown.',
      formal: 'Calmar = annualised return / |max drawdown|. Captures "return per unit of worst pain". Calmar > 0.5 is acceptable, > 1 is good, > 3 is exceptional.',
      see_also: ['sharpe', 'max_drawdown'], used_in: ['portfolio'] },

    { id: 'beta', term: 'Beta (β)',
      plain: 'How much a stock/portfolio moves per 1-unit move in the benchmark.',
      formal: 'OLS regression slope of asset returns on benchmark returns. β = 1 moves with NIFTY. β > 1 amplifies. β < 1 dampens. β < 0 (rare) inverts. Beta is unstable across regimes.',
      formula: '\\beta = \\frac{\\text{cov}(R_i, R_m)}{\\text{var}(R_m)}',
      example: 'HDFCBANK β to NIFTY = 0.95 → moves slightly less than the market. RELIANCE β = 1.1 → amplifies.',
      see_also: ['alpha', 'correlation'], used_in: ['portfolio'] },

    { id: 'alpha', term: 'Alpha (α)',
      plain: 'Return above what beta alone would predict.',
      formal: 'In CAPM: α = R_i − [R_f + β·(R_m − R_f)]. Positive alpha = skill (or luck). Most discretionary alpha is luck on small samples; quant alpha is structural and decays.',
      see_also: ['beta'], used_in: ['portfolio'] },

    { id: 'correlation', term: 'Correlation (ρ)',
      plain: 'How much two assets move together, scaled to [−1, +1].',
      formal: 'Pearson correlation of returns. ρ = +1 perfectly co-moving, 0 unrelated, −1 inverse. Correlations rise toward 1 in crashes ("everything sells off together") — diversification fails when you need it most.',
      formula: '\\rho(X, Y) = \\frac{\\text{cov}(X, Y)}{\\sigma_X \\sigma_Y}',
      example: 'TCS and INFY daily-return correlation = 0.82 → essentially the same exposure. Adding both is barely diversification.',
      see_also: ['beta'], used_in: ['portfolio'] },

    // ── Sizing ────────────────────────────────────────────────────
    { id: 'kelly', term: 'Kelly Criterion',
      plain: 'The bet size that maximises long-run compound growth.',
      formal: 'f* = (p·b − q) / b, where p = win probability, q = 1 − p, b = win/loss ratio. Mathematically optimal but emotionally brutal — drawdowns are large. Most practitioners use half-Kelly.',
      formula: 'f^* = \\frac{p b - q}{b}',
      example: '60% win rate, 2:1 win:loss → f* = (0.6·2 − 0.4)/2 = 0.4. Risk 40% of bankroll per trade for max growth. Most use half-Kelly = 20%.',
      see_also: ['half_kelly', 'fixed_fractional'], used_in: ['sizing'] },

    { id: 'half_kelly', term: 'Half-Kelly',
      plain: 'Use half the Kelly fraction — same edge, smaller drawdowns.',
      formal: 'Risking 0.5 × f* delivers ~75% of full-Kelly growth with ~25% of the drawdown variance. Standard recommendation for systematic traders.',
      see_also: ['kelly'], used_in: ['sizing'] },

    { id: 'fixed_fractional', term: 'Fixed-Fractional Sizing',
      plain: 'Always risk the same fraction of current bankroll per trade.',
      formal: 'Risk X% of equity per trade (typically 0.5%–2%). Self-correcting: positions shrink during drawdowns, grow during win streaks. Simpler than Kelly; doesn\'t require win-rate estimates.',
      see_also: ['kelly'], used_in: ['sizing'] },

    // ── Backtest / event terms ────────────────────────────────────
    { id: 'gap', term: 'Gap',
      plain: 'Difference between today\'s open and yesterday\'s close.',
      formal: 'Gap % = (open − prior_close) / prior_close. Gap-ups follow positive overnight news; gap-downs follow negative. Gap-and-fade = open higher, close lower (failed breakout).',
      formula: '\\text{Gap \\%} = \\frac{O_t - C_{t-1}}{C_{t-1}}',
      see_also: ['fade_prob'], used_in: ['backtest'] },

    { id: 'fade_prob', term: 'Fade Probability',
      plain: 'Conditional probability that a gap reverses intraday.',
      formal: 'P(close_t < open_t | gap_up) for gap-ups, or P(close_t > open_t | gap_down) for gap-downs. High fade prob = the move is typically sold into. Often regime-dependent (works in range, fails in trend).',
      see_also: ['gap'], used_in: ['backtest'] },

    { id: 'drift_5d', term: '5-Day Drift',
      plain: 'Average return 1 → 5 trading days after an event.',
      formal: 'Mean (close_t+5 − close_t) / close_t over the historical sample of event occurrences. Often used to detect "post-earnings drift" — stocks that beat continue rising for ~5 sessions before mean-reverting.',
      see_also: ['gap'], used_in: ['backtest'] },

    // ── Misc ─────────────────────────────────────────────────────
    { id: 'span', term: 'SPAN Margin',
      plain: 'Margin required by the exchange to hold an options/futures position.',
      formal: 'NSE/BSE use the SPAN (Standard Portfolio Analysis of Risk) algorithm. It calculates the worst-case loss across 16 risk scenarios (price + vol shocks) and charges that as initial margin. Defined-risk option strategies (spreads, condors) need less margin than naked short options.',
      see_also: [], used_in: ['payoff'] },
  ];

  var INDEX = {};
  TERMS.forEach(function (t) { INDEX[t.id] = t; });

  function get(id) { return INDEX[id] || null; }
  function all() { return TERMS.slice(); }
  function search(q) {
    if (!q) return all();
    var n = String(q).toLowerCase();
    return TERMS.filter(function (t) {
      return t.id.indexOf(n) >= 0
          || t.term.toLowerCase().indexOf(n) >= 0
          || (t.plain || '').toLowerCase().indexOf(n) >= 0;
    });
  }

  // Render an info tooltip on an element. Lightweight — no positioning lib.
  // Usage: Glossary.tooltip(labelEl, 'delta');
  // Adds a `?` icon after the element and shows a popover on hover/focus.
  function tooltip(targetEl, termId) {
    var t = get(termId);
    if (!t || !targetEl) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'qp-info';
    btn.setAttribute('aria-label', 'About ' + t.term);
    btn.setAttribute('data-term', termId);
    btn.innerHTML = '?';
    targetEl.appendChild(btn);
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      _showPopover(btn, t);
    });
    btn.addEventListener('mouseenter', function () { _showPopover(btn, t, true); });
    btn.addEventListener('mouseleave', function () { _hidePopover(true); });
  }

  var _activePop = null;
  function _showPopover(anchor, t, isHover) {
    _hidePopover();
    var p = document.createElement('div');
    p.className = 'qp-popover' + (isHover ? ' is-hover' : '');
    p.innerHTML =
      '<div class="qp-pop-term">' + _esc(t.term) + '</div>' +
      '<div class="qp-pop-plain">' + _esc(t.plain) + '</div>' +
      (t.units ? '<div class="qp-pop-units">Units: ' + _esc(t.units) + '</div>' : '') +
      '<a class="qp-pop-more" href="playground.html?tool=glossary#' + encodeURIComponent(t.id) + '">Full definition &rarr;</a>';
    document.body.appendChild(p);
    var r = anchor.getBoundingClientRect();
    p.style.position = 'fixed';
    p.style.left = Math.min(window.innerWidth - 280, r.left) + 'px';
    p.style.top = (r.bottom + 6) + 'px';
    p.style.zIndex = 9999;
    _activePop = p;
    if (!isHover) {
      setTimeout(function () {
        document.addEventListener('click', _outsideClickHandler, { once: true });
      }, 0);
    }
  }
  function _outsideClickHandler(e) {
    if (_activePop && !_activePop.contains(e.target)) _hidePopover();
  }
  function _hidePopover(hoverOnly) {
    if (!_activePop) return;
    if (hoverOnly && !_activePop.classList.contains('is-hover')) return;
    _activePop.parentNode && _activePop.parentNode.removeChild(_activePop);
    _activePop = null;
  }
  function _esc(s) {
    return String(s || '').replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Render KaTeX into an element if KaTeX is loaded; fall back to <code>.
  function formula(latex, el, opts) {
    opts = opts || {};
    if (!el) return;
    if (root.katex && typeof root.katex.render === 'function') {
      try {
        root.katex.render(latex, el, { throwOnError: false, displayMode: !!opts.display, output: 'html' });
        return;
      } catch (e) {
        // fall through to fallback
      }
    }
    el.innerHTML = '<code class="qp-formula-fallback">' + _esc(latex) + '</code>';
  }

  // Bulk: scan a container and render all <span data-katex="..."></span>
  function renderAll(container) {
    container = container || document;
    var nodes = container.querySelectorAll('[data-katex]');
    for (var i = 0; i < nodes.length; i++) {
      formula(nodes[i].getAttribute('data-katex'), nodes[i],
              { display: nodes[i].hasAttribute('data-katex-display') });
    }
  }

  root.Glossary = {
    get: get,
    all: all,
    search: search,
    tooltip: tooltip,
    formula: formula,
    renderAll: renderAll,
    _terms: TERMS,
  };
})(typeof window !== 'undefined' ? window : globalThis);
