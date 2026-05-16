/* curated-signals.js — quality-filtered, deduped, diversified top picks.
 *
 * Hard-fixes the "article says X but we suggest Y" bug by requiring each
 * signal's ticker to actually appear (or its known short-name) in the
 * headline. So TCS-on-a-TataConsumer-article gets dropped.
 *
 * Each pick is enriched with:
 *   - target_price + target_pct          (entry × (1 + best forward prediction))
 *   - holding_period
 *   - conviction_label
 * Card renders with:
 *   - period-tab mini-chart (1D / 1W / 1M / 3M) + crosshair on hover
 *     and a sticky crosshair on the last point at rest
 *   - multi-timeframe price-change strip (Today / 5D / 1M)
 *   - prediction badges (1D / 3D / 20D)
 *   - alpha-vs-trend note when the stock is in a short-term downtrend
 *
 * Public API on window.CuratedSignals:
 *   fetch(n)             -> Promise<EnrichedSignal[]>
 *   drawMiniChart(canvas, ticker, options)
 *   buildCard(sig, opts) -> HTML string
 *   hydrate(host, signals)  -> wires charts, tabs, change-strip
 */
(function () {
  if (window.CuratedSignals) return;

  const MAX_AGE_HOURS  = 168;
  const MIN_ALPHA      = 35;
  const MAX_PER_SECTOR = 2;
  const MEGA_CAPS = new Set([
    'TCS','RELIANCE','INFY','HDFCBANK','ICICIBANK','HINDUNILVR','SBIN','BHARTIARTL',
    'KOTAKBANK','LT','ITC','AXISBANK','BAJFINANCE','MARUTI','NESTLEIND','ASIANPAINT',
    'WIPRO','TECHM','HCLTECH','SUNPHARMA',
  ]);
  const MAX_MEGA_CAP_IN_SLATE = 2;

  // ── Subject-mismatch filter (Stanley/Tata-Steel bug fix at display time) ──
  // For each ticker, list every common way a headline might name the company.
  // If the headline contains NONE of these (case-insensitive, word-bounded),
  // the signal is silently dropped — it's almost certainly a mention-not-subject
  // false positive.
  const SHORT_NAMES = {
    TCS:        ['TCS','Tata Consultancy'],
    RELIANCE:   ['RIL','Reliance Industries','Reliance Ind'],
    INFY:       ['INFY','Infosys'],
    HDFCBANK:   ['HDFC Bank','HDFCBANK'],
    ICICIBANK:  ['ICICI Bank','ICICIBANK','ICICI'],
    HINDUNILVR: ['HUL','Hindustan Unilever','Hind Unilever','HINDUNILVR'],
    SBIN:       ['SBI','State Bank of India','SBIN'],
    BHARTIARTL: ['Bharti Airtel','Airtel','BHARTIARTL'],
    KOTAKBANK:  ['Kotak Mahindra','Kotak Bank','KOTAKBANK'],
    LT:         ['Larsen','L&T','L&amp;T','LT '],
    ITC:        ['ITC Ltd','ITC '],
    AXISBANK:   ['Axis Bank','AXISBANK'],
    BAJFINANCE: ['Bajaj Finance','BAJFINANCE'],
    'BAJAJ-AUTO': ['Bajaj Auto','BAJAJ-AUTO','BAJAJ AUTO'],
    BAJAJFINSV: ['Bajaj Finserv','BAJAJFINSV'],
    MARUTI:     ['Maruti Suzuki','Maruti'],
    NESTLEIND:  ['Nestle India','Nestle','NESTLEIND'],
    ASIANPAINT: ['Asian Paints','ASIANPAINT'],
    WIPRO:      ['Wipro','WIPRO'],
    TECHM:      ['Tech Mahindra','TECHM','TechM'],
    HCLTECH:    ['HCL Technologies','HCL Tech','HCLTECH'],
    SUNPHARMA:  ['Sun Pharma','SUNPHARMA'],
    TATACONSUM: ['Tata Consumer','TATACONSUM'],
    TATASTEEL:  ['Tata Steel','TATASTEEL'],
    TATAMOTORS: ['Tata Motors','TATAMOTORS'],
    COALINDIA:  ['Coal India','COALINDIA'],
    POWERGRID:  ['Power Grid','POWERGRID'],
    NTPC:       ['NTPC'],
    ONGC:       ['ONGC','Oil and Natural Gas'],
    BPCL:       ['Bharat Petroleum','BPCL'],
    IOC:        ['Indian Oil','IOC'],
    GAIL:       ['GAIL'],
    ADANIENT:   ['Adani Enterprises','ADANIENT','Adani Ent'],
    ADANIPORTS: ['Adani Ports','ADANIPORTS'],
    TITAN:      ['Titan Company','Titan ','TITAN'],
    HEROMOTOCO: ['Hero MotoCorp','Hero Moto','HEROMOTOCO'],
    CIPLA:      ['Cipla','CIPLA'],
    DRREDDY:    ['Dr Reddy','Dr. Reddy','DRREDDY'],
    TATAPOWER:  ['Tata Power','TATAPOWER'],
    EICHERMOT:  ['Eicher Motors','EICHERMOT'],
    'M&M':      ['Mahindra & Mahindra','M&M','Mahindra ','M&amp;M'],
    'M&MFIN':   ['Mahindra Finance','M&M Finance','M&MFIN'],
    HDFC:       ['HDFC ','Housing Development Finance'],
    ULTRACEMCO: ['UltraTech Cement','UltraTech','ULTRACEMCO'],
    GRASIM:     ['Grasim','GRASIM'],
    JSWSTEEL:   ['JSW Steel','JSWSTEEL'],
    HINDALCO:   ['Hindalco','HINDALCO'],
    NUVAMA:     ['Nuvama','NUVAMA'],
    THERMAX:    ['Thermax','THERMAX'],
    HFCL:       ['HFCL'],
    BSE:        ['BSE Ltd','BSE Limited','Bombay Stock Exchange','BSE shares','BSE stock'],
    CLEAN:      ['Clean Science','CLEAN','Clean Sci'],
    HONAUT:     ['Honeywell Automation','HONAUT'],
    BAJAJHLDNG: ['Bajaj Holdings','BAJAJHLDNG'],
  };

  function ageHours(iso) {
    if (!iso) return 999;
    const t = Date.parse(iso);
    if (!t) return 999;
    return (Date.now() - t) / 3_600_000;
  }
  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  // Broker / research-firm tickers. If the article headline names them in an
  // analyst posture ("Nuvama warns…", "Motilal Oswal: top picks…", "IIFL on
  // …"), the article is the broker's COMMENTARY on other stocks — not news
  // about the broker itself. Reject those signals.
  const BROKER_TICKERS = new Set([
    'NUVAMA', 'MOTILALOFS', 'ANGELONE', 'IIFL', 'ANANDRATHI', 'ICICIPRULI',
    'BSE', 'CDSL', 'CAMS', 'MCX',
  ]);
  // Verbs that, when they follow the broker name, indicate analyst commentary.
  const ANALYST_VERB_RE_SRC = '\\s+(?:warns?|says?|sees?|rates?|recommends?|advises?|targets?|cuts?|raises?|maintains?|initiates?|forecasts?|projects?|expects?|prefers?|picks?|pick|view|outlook|note|on|upgrades?|downgrades?|reiterates?)\\b';

  function isBrokerCommentary(headline, ticker, summary) {
    const tk = (ticker || '').toUpperCase();
    if (!BROKER_TICKERS.has(tk)) return false;
    const text = ((headline || '') + ' ' + (summary || '')).toLowerCase();
    if (!text.trim()) return false;
    const names = (SHORT_NAMES[tk] || [tk]).map(s => s.toLowerCase());
    for (const n of names) {
      const nameEsc = escapeRe(n);
      // "{broker}: " — clearly commentary header
      if (new RegExp('\\b' + nameEsc + '\\s*:', 'i').test(text)) return true;
      // "{broker} {analyst verb}"
      if (new RegExp('\\b' + nameEsc + ANALYST_VERB_RE_SRC, 'i').test(text)) return true;
      // "{broker}'s" — possessive in titles ("Nuvama's top picks")
      if (new RegExp('\\b' + nameEsc + "'s", 'i').test(text)) return true;
      // "{broker} report" / "{broker} note"
      if (new RegExp('\\b' + nameEsc + '\\s+(?:report|note|research|analyst|brokerage)\\b', 'i').test(text)) return true;
    }
    return false;
  }

  // Checks BOTH the headline AND the RSS summary (article preview) for any
  // known name variant of the ticker. Doubles the text our subject-mismatch
  // filter sees, catching cases where the title is ambiguous but the
  // first sentence of the article clearly names the stock.
  function headlineMatchesTicker(headline, ticker, summary) {
    const tk = (ticker || '').toUpperCase().trim();
    if (!tk) return false;
    const text = ((headline || '') + ' ' + (summary || '')).toLowerCase();
    if (!text.trim()) return false;
    const names = SHORT_NAMES[tk] || [tk];
    for (const n of names) {
      const re = new RegExp('\\b' + escapeRe(n.toLowerCase()) + '\\b', 'i');
      if (re.test(text)) return true;
    }
    return new RegExp('\\b' + escapeRe(tk) + '\\b', 'i').test(text);
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }
  function bestForwardReturn(sig) {
    const preds = sig.predictions || {};
    const order = ['3D','5D','1D','10D','20D'];
    for (const h of order) {
      const p = preds[h];
      if (p && (p.return_pct != null || p.predicted_return_pct != null)) {
        const r = p.return_pct != null ? p.return_pct : p.predicted_return_pct;
        return { horizon: h, ret_pct: r };
      }
    }
    return null;
  }
  function holdingPeriodFor(sig, fwd) {
    const a = sig.alpha_score || 0;
    if (!fwd) return '5-10d';
    if (a >= 80 && fwd.horizon === '1D') return '1-2d';
    if (a >= 80 && fwd.horizon === '3D') return '2-4d';
    if (a >= 65)                         return '3-7d';
    if (a >= 50)                         return '5-10d';
    return '10-20d';
  }
  function convictionLabel(alpha) {
    if (alpha >= 80) return { label: 'High',     color: '#2dd4aa' };
    if (alpha >= 65) return { label: 'Moderate', color: '#e6b84a' };
    return                  { label: 'Watch',    color: '#8eb4e0' };
  }
  function fmtPrice(n) {
    if (n == null || isNaN(n)) return '—';
    return '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }
  function fmtPct(n) {
    if (n == null || isNaN(n)) return '—';
    return (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%';
  }

  function enrich(sig) {
    const entry = Number(sig.entry_price || sig.price || 0);
    const fwd = bestForwardReturn(sig);
    const tgtPct = fwd ? fwd.ret_pct : null;
    const target = (entry && tgtPct != null) ? entry * (1 + tgtPct / 100) : null;
    return {
      ...sig,
      entry_price:    entry || null,
      target_price:   target,
      target_pct:     tgtPct,
      target_horizon: fwd ? fwd.horizon : null,
      holding_period: holdingPeriodFor(sig, fwd),
      conviction:     convictionLabel(sig.alpha_score || 0),
    };
  }

  async function fetchRaw(limit) {
    try {
      const r = await fetch('/api/signals?limit=' + (limit || 80));
      const j = await r.json();
      return (j && j.data) || [];
    } catch (_) { return []; }
  }

  function curate(rows, n) {
    const byTicker = new Map();
    for (const s of rows) {
      const tk = (s.ticker || '').toUpperCase();
      if (!tk) continue;
      if ((s.alpha_score || 0) < MIN_ALPHA)       continue;
      if (ageHours(s.created_at) > MAX_AGE_HOURS) continue;
      if (s.sentiment === 'bearish')              continue;
      // Subject-mismatch gate: the article must actually name this stock.
      // Check BOTH headline AND summary so we see ~5× more text.
      if (!headlineMatchesTicker(s.headline || s.title || '', tk, s.summary || '')) continue;
      // Broker-commentary gate: drop "Nuvama warns about midcaps" — the
      // article is the broker analysing OTHER stocks, not news about the broker.
      if (isBrokerCommentary(s.headline || s.title || '', tk, s.summary || '')) continue;
      const cur = byTicker.get(tk);
      if (!cur || (s.alpha_score || 0) > (cur.alpha_score || 0)) {
        byTicker.set(tk, s);
      }
    }
    let pool = Array.from(byTicker.values());
    pool.sort((a, b) => (b.alpha_score || 0) - (a.alpha_score || 0));
    const sectorCount = new Map();
    let megaCapUsed = 0;
    const picked = [];
    for (const s of pool) {
      const sec = (s.sector || 'OTHER').toUpperCase();
      const sc  = sectorCount.get(sec) || 0;
      const isMega = MEGA_CAPS.has((s.ticker || '').toUpperCase());
      if (sc >= MAX_PER_SECTOR) continue;
      if (isMega && megaCapUsed >= MAX_MEGA_CAP_IN_SLATE) continue;
      picked.push(s);
      sectorCount.set(sec, sc + 1);
      if (isMega) megaCapUsed += 1;
      if (picked.length >= n) break;
    }
    if (picked.length < n) {
      const seen = new Set(picked.map(p => (p.ticker || '').toUpperCase()));
      for (const s of pool) {
        if (picked.length >= n) break;
        const tk = (s.ticker || '').toUpperCase();
        if (seen.has(tk)) continue;
        if (MEGA_CAPS.has(tk) && megaCapUsed >= MAX_MEGA_CAP_IN_SLATE) continue;
        picked.push(s);
        seen.add(tk);
        if (MEGA_CAPS.has(tk)) megaCapUsed += 1;
      }
    }
    return picked.map(enrich);
  }

  // ── Fundamental enrichment + gate ─────────────────────────────────────────
  // After curation, fetch fundamentals for the picks and:
  //   - drop any pick whose tier == 'avoid' (poor fundamentals override news)
  //   - attach fundamentals to each signal so the card can show the chip + reasoning
  async function _fetchFundamentalsBulk(tickers) {
    if (!tickers || !tickers.length) return {};
    try {
      const url = '/api/fundamentals/score-bulk?tickers=' + encodeURIComponent(tickers.join(','));
      const r = await fetch(url);
      const j = await r.json();
      return (j && j.data) || {};
    } catch (_) { return {}; }
  }

  async function fetchCurated(n) {
    const rows = await fetchRaw(80);
    // Overshoot — we'll drop some after fundamentals gate so we still hit n
    const picks = curate(rows, (n || 5) * 2);
    const tks = [...new Set(picks.map(p => p.ticker).filter(Boolean))];
    const funds = await _fetchFundamentalsBulk(tks);
    const gated = [];
    for (const p of picks) {
      const f = funds[p.ticker];
      if (f && f.tier === 'avoid') continue; // skip structurally bad stocks
      if (f) p.fundamentals = f;
      gated.push(p);
      if (gated.length >= (n || 5)) break;
    }
    // If gating left us short, backfill from picks without fundamentals data
    if (gated.length < (n || 5)) {
      for (const p of picks) {
        if (gated.includes(p)) continue;
        gated.push(p);
        if (gated.length >= (n || 5)) break;
      }
    }
    return gated;
  }

  // ── Mini chart with period tabs + sticky-crosshair-on-last-point ──────────
  const PERIODS = [
    { id: '1d',  label: '1D' },
    { id: '5d',  label: '1W' },
    { id: '1mo', label: '1M' },
    { id: '3mo', label: '3M' },
  ];

  async function _loadSeries(ticker, period) {
    try {
      const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/chart?period=${period}`);
      const j = await r.json();
      return ((j && j.data) || []).filter(d => d && d.close != null && isFinite(Number(d.close)));
    } catch (_) { return []; }
  }

  function _drawChart(canvas, series, opts) {
    opts = opts || {};
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (!series.length) {
      ctx.fillStyle = '#5a6373';
      ctx.font = '11px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('no chart data', W / 2, H / 2);
      return null;
    }
    const closes = series.map(d => Number(d.close));
    const min = Math.min(...closes), max = Math.max(...closes), range = (max - min) || 1;
    const first = closes[0], last = closes[closes.length - 1];
    const up = last >= first;
    const line = up ? '#2dd4aa' : '#f26b6b';
    const fillRgb = up ? '45,212,170' : '242,107,107';
    const pad = { t: 8, b: 16, l: 4, r: 4 };
    const cW = W - pad.l - pad.r, cH = H - pad.t - pad.b;
    const pts = series.map((d, i) => ({
      x: pad.l + (series.length > 1 ? (i / (series.length - 1)) * cW : cW / 2),
      y: pad.t + (1 - (Number(d.close) - min) / range) * cH,
      close: Number(d.close),
      date: d.date,
    }));

    function draw(hoverIdx) {
      ctx.clearRect(0, 0, W, H);
      const grad = ctx.createLinearGradient(0, pad.t, 0, H);
      grad.addColorStop(0, `rgba(${fillRgb},0.32)`);
      grad.addColorStop(1, `rgba(${fillRgb},0)`);
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) {
        const cp = (pts[i].x - pts[i-1].x) * 0.3;
        ctx.bezierCurveTo(pts[i-1].x + cp, pts[i-1].y, pts[i].x - cp, pts[i].y, pts[i].x, pts[i].y);
      }
      ctx.lineTo(pts[pts.length-1].x, H - pad.b);
      ctx.lineTo(pts[0].x, H - pad.b);
      ctx.closePath();
      ctx.fillStyle = grad; ctx.fill();
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) {
        const cp = (pts[i].x - pts[i-1].x) * 0.3;
        ctx.bezierCurveTo(pts[i-1].x + cp, pts[i-1].y, pts[i].x - cp, pts[i].y, pts[i].x, pts[i].y);
      }
      ctx.lineWidth = 1.75; ctx.strokeStyle = line; ctx.stroke();
      const lp = pts[pts.length-1];
      ctx.beginPath(); ctx.arc(lp.x, lp.y, 2.5, 0, Math.PI*2); ctx.fillStyle = line; ctx.fill();

      // Sticky crosshair at last point by default, hover crosshair otherwise.
      const idx = hoverIdx != null ? hoverIdx : pts.length - 1;
      const hp = pts[idx];
      if (!hp) return;
      ctx.strokeStyle = hoverIdx != null ? 'rgba(141,148,168,0.55)' : 'rgba(141,148,168,0.25)';
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(hp.x, pad.t); ctx.lineTo(hp.x, H - pad.b); ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(hp.x, hp.y, 3.5, 0, Math.PI*2);
      ctx.fillStyle = line; ctx.fill();
      ctx.strokeStyle = '#0a0e14'; ctx.lineWidth = 2; ctx.stroke();
      const txt = `${hp.date} · ₹${hp.close.toFixed(2)}`;
      ctx.font = '10px Inter, sans-serif';
      const tw = ctx.measureText(txt).width + 12;
      let tx = hp.x + 8; if (tx + tw > W - 2) tx = hp.x - tw - 8;
      ctx.fillStyle = 'rgba(7,9,13,0.92)';
      ctx.fillRect(tx, 2, tw, 16);
      ctx.strokeStyle = 'rgba(141,148,168,0.3)'; ctx.lineWidth = 1;
      ctx.strokeRect(tx + 0.5, 2.5, tw - 1, 15);
      ctx.fillStyle = '#dde3ef';
      ctx.textAlign = 'left';
      ctx.fillText(txt, tx + 6, 13);
    }
    draw(null);
    canvas.style.cursor = 'crosshair';
    canvas.onmousemove = (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = (e.clientX - rect.left) * (W / rect.width);
      let nearest = 0, best = Infinity;
      for (let i = 0; i < pts.length; i++) {
        const d = Math.abs(pts[i].x - mx);
        if (d < best) { best = d; nearest = i; }
      }
      draw(nearest);
    };
    canvas.onmouseleave = () => draw(null);
    // Return summary for the parent (used by change-strip)
    return { first, last, change_pct: first ? (last - first) / first * 100 : 0, points: pts.length };
  }

  // High-level: re-draws into the canvas for any given period
  async function drawMiniChart(canvas, ticker, opts) {
    opts = opts || {};
    const period = opts.period || '1mo';
    const series = await _loadSeries(ticker, period);
    return _drawChart(canvas, series, opts);
  }

  // ── Card renderer ─────────────────────────────────────────────────────────
  function buildCard(sig, opts) {
    opts = opts || {};
    const dense = !!opts.dense;
    const idx = opts.idx != null ? opts.idx : null;
    const alpha = Math.round(sig.alpha_score || 0);
    const sent  = sig.sentiment || 'neutral';
    const conv  = sig.conviction || convictionLabel(alpha);
    const sentColor = sent === 'bullish' ? '#2dd4aa' : sent === 'bearish' ? '#f26b6b' : '#8eb4e0';
    const tgtPctTxt = sig.target_pct != null ? fmtPct(sig.target_pct) : '—';
    const tgtClass  = (sig.target_pct || 0) >= 0 ? 'bull' : 'bear';
    const evType = (sig.event_type || 'news').replace(/_/g, ' ');
    const canvasId = `mc_${(sig.ticker||'').replace(/[^A-Z0-9]/g,'')}_${Math.random().toString(36).slice(2,7)}`;
    const tabsHtml = PERIODS.map(p =>
      `<button data-period="${p.id}" class="${p.id === '1mo' ? 'on' : ''}">${p.label}</button>`
    ).join('');

    if (dense) {
      return `
      <div class="curated-card curated-card--dense" data-ticker="${escapeHtml(sig.ticker)}">
        <div class="curated-row">
          ${idx != null ? `<span class="curated-rank">#${idx + 1}</span>` : ''}
          <span class="curated-tk">${escapeHtml(sig.ticker || '—')}</span>
          <span class="curated-alpha-pill" style="--c:${conv.color}">${alpha}</span>
        </div>
        <div class="curated-chart-tabs curated-chart-tabs--dense" data-target="${canvasId}">${tabsHtml}</div>
        <canvas id="${canvasId}" width="240" height="56" class="curated-mini-chart" data-ticker="${escapeHtml(sig.ticker)}"></canvas>
        <div class="curated-row curated-row--meta">
          <span class="curated-conv" style="color:${conv.color}">${conv.label}</span>
          <span class="curated-pct ${tgtClass}">${tgtPctTxt}</span>
          <span class="curated-hold">${sig.holding_period || '5-10d'}</span>
        </div>
      </div>`;
    }

    // Fundamentals chip + reasoning row
    const fund = sig.fundamentals;
    const FUND_COLOR = {
      strong: '#2dd4aa', decent: '#8eb4e0', weak: '#e6b84a', avoid: '#f26b6b'
    };
    let fundChip = '';
    let fundRow = '';
    if (fund && fund.tier) {
      const c = FUND_COLOR[fund.tier] || '#8a94a8';
      fundChip = `<span class="curated-fund-chip" title="Fundamentals: ${escapeHtml(fund.label || '')} (${fund.score}/100)"
        style="background:${c}1f;color:${c};border:1px solid ${c}55;">F&nbsp;${fund.score}</span>`;

      const positives = (fund.positives || []).slice(0, 3)
        .map(p => `<span class="curated-fund-pos">${escapeHtml(p)}</span>`).join('');
      const flags = (fund.red_flags || []).slice(0, 3)
        .map(p => `<span class="curated-fund-neg">${escapeHtml(p)}</span>`).join('');
      if (positives || flags) {
        fundRow = `<div class="curated-fund-row">
          <div class="curated-fund-label" style="color:${c};">${escapeHtml(fund.label || '')}</div>
          <div class="curated-fund-pills">${positives}${flags}</div>
        </div>`;
      }
    }

    // Prediction badges (1D / 3D / 20D)
    const preds = sig.predictions || {};
    const predBadges = ['1D','3D','20D'].map(h => {
      const p = preds[h];
      if (!p) return '';
      const r = p.return_pct != null ? p.return_pct : p.predicted_return_pct;
      if (r == null) return '';
      const c = r >= 0 ? '#2dd4aa' : '#f26b6b';
      return `<div class="curated-pred">
        <div class="curated-pred-h">${h}</div>
        <div class="curated-pred-v" style="color:${c}">${r >= 0 ? '+' : ''}${r.toFixed(2)}%</div>
      </div>`;
    }).filter(Boolean).join('');

    // Multi-timeframe price change strip — values hydrated after render
    return `
    <div class="curated-card" data-ticker="${escapeHtml(sig.ticker)}">
      <div class="curated-head">
        <div class="curated-head-l">
          <div class="curated-tk-row">
            <span class="curated-tk-big">${escapeHtml(sig.ticker || '—')}</span>
            <span class="curated-conv-chip" style="background:${conv.color}22;color:${conv.color};border:1px solid ${conv.color}55;">${conv.label}</span>
            ${fundChip}
            <span class="curated-evtype">${escapeHtml(evType)}</span>
            <span class="curated-day-chg" data-role="day-chg">—</span>
          </div>
          <div class="curated-headline">${escapeHtml((sig.headline || '').slice(0, 140))}</div>
        </div>
        <div class="curated-alpha-block" style="--c:${conv.color}">
          <div class="curated-alpha-num">${alpha}</div>
          <div class="curated-alpha-lbl">ALPHA</div>
        </div>
      </div>

      <div class="curated-chart-tabs" data-target="${canvasId}">${tabsHtml}</div>
      <canvas id="${canvasId}" width="640" height="100" class="curated-mini-chart" data-ticker="${escapeHtml(sig.ticker)}"></canvas>

      <!-- Multi-timeframe price change strip — hydrated after chart loads -->
      <div class="curated-change-strip" data-role="change-strip">
        <div class="curated-change"><div class="lbl">Today</div><div class="val" data-tf="1d">—</div></div>
        <div class="curated-change"><div class="lbl">5-day</div><div class="val" data-tf="5d">—</div></div>
        <div class="curated-change"><div class="lbl">1-month</div><div class="val" data-tf="1mo">—</div></div>
      </div>

      <!-- Trade plan -->
      <div class="curated-metrics">
        <div class="curated-metric">
          <div class="curated-metric-lbl">Entry</div>
          <div class="curated-metric-val">${fmtPrice(sig.entry_price)}</div>
        </div>
        <div class="curated-metric">
          <div class="curated-metric-lbl">Target (${sig.target_horizon || '3D'})</div>
          <div class="curated-metric-val">${fmtPrice(sig.target_price)} <span class="curated-target-pct ${tgtClass}">${tgtPctTxt}</span></div>
        </div>
        <div class="curated-metric">
          <div class="curated-metric-lbl">Holding</div>
          <div class="curated-metric-val">${sig.holding_period || '5-10d'}</div>
        </div>
        <div class="curated-metric">
          <div class="curated-metric-lbl">Sentiment</div>
          <div class="curated-metric-val" style="color:${sentColor}">${escapeHtml(sent)}</div>
        </div>
      </div>

      <!-- Prediction badges -->
      ${predBadges ? `<div class="curated-preds">${predBadges}</div>` : ''}

      <!-- Fundamentals reasoning -->
      ${fundRow}

      <!-- Alpha-vs-trend note — hydrated when 5d trend is computed and conflicts with alpha -->
      <div class="curated-trend-note" data-role="trend-note" hidden></div>
    </div>`;
  }

  // ── Hydration: wire chart tabs + load all periods to fill change strip ────
  function bindTabs(card) {
    const tabBars = card.querySelectorAll('.curated-chart-tabs');
    tabBars.forEach((bar) => {
      const ticker = (card.dataset.ticker || '').replace(/&amp;/g, '&');
      const cvs = document.getElementById(bar.dataset.target);
      if (!cvs) return;
      bar.addEventListener('click', async (e) => {
        const btn = e.target.closest('button[data-period]');
        if (!btn) return;
        bar.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
        await drawMiniChart(cvs, ticker, { period: btn.dataset.period });
      });
    });
  }

  function colorPct(v) { return v >= 0 ? '#2dd4aa' : '#f26b6b'; }

  // Module-level sector-regime snapshot (loaded once per page)
  let _sectorRegimeCache = null;
  async function _getSectorRegime() {
    if (_sectorRegimeCache !== null) return _sectorRegimeCache;
    try {
      const r = await fetch('/api/edge/sector-regime');
      const j = await r.json();
      _sectorRegimeCache = (j && j.data) || {};
    } catch (_) { _sectorRegimeCache = {}; }
    return _sectorRegimeCache;
  }

  // Map signal.sector to the regime keys we use server-side
  function _normaliseSector(s) {
    if (!s) return null;
    const k = String(s).toUpperCase().replace(/_/g, ' ');
    if (/IT|TECH/.test(k)) return 'IT';
    if (/BANK/.test(k) && /PSU/.test(k)) return 'PSU_BANK';
    if (/BANK|FIN/.test(k)) return 'BANK';
    if (/AUTO/.test(k)) return 'AUTO';
    if (/PHARMA|HEALTHCARE/.test(k)) return 'PHARMA';
    if (/METAL|STEEL/.test(k)) return 'METAL';
    if (/ENERGY|OIL|GAS|POWER/.test(k)) return 'ENERGY';
    if (/FMCG|CONSUMER/.test(k)) return 'FMCG';
    if (/REALTY|REAL ESTATE/.test(k)) return 'REALTY';
    if (/MEDIA|ENTERTAIN/.test(k)) return 'MEDIA';
    return null;
  }

  async function hydrateOne(card, sig) {
    const ticker = sig.ticker;
    const canvas = card.querySelector('canvas');
    if (canvas) await drawMiniChart(canvas, ticker, { period: '1mo' });

    // Multi-timeframe change strip — fire all three periods in parallel
    const strip = card.querySelector('[data-role="change-strip"]');
    if (strip) {
      const periods = [['1d','1d'], ['5d','5d'], ['1mo','1mo']];
      const summaries = await Promise.all(
        periods.map(async ([tf]) => {
          const series = await _loadSeries(ticker, tf);
          if (series.length < 2) return null;
          const first = Number(series[0].close);
          const last  = Number(series[series.length - 1].close);
          if (!isFinite(first) || !isFinite(last) || !first) return null;
          return { tf, pct: (last - first) / first * 100 };
        })
      );
      summaries.forEach((s) => {
        if (!s) return;
        const v = strip.querySelector(`[data-tf="${s.tf}"]`);
        if (!v) return;
        v.textContent = (s.pct >= 0 ? '+' : '') + s.pct.toFixed(2) + '%';
        v.style.color = colorPct(s.pct);
      });

      // Alpha-vs-trend note when alpha says bullish but 5d is meaningfully down
      const fiveD = summaries.find(s => s && s.tf === '5d');
      const note = card.querySelector('[data-role="trend-note"]');
      if (note && fiveD && fiveD.pct < -2 && (sig.alpha_score || 0) >= 65) {
        note.hidden = false;
        note.innerHTML = `
          <span class="material-symbols-outlined" style="font-size:14px;color:#e6b84a;">info</span>
          High alpha is <strong>event-driven</strong> (${escapeHtml((sig.event_type || 'news').replace(/_/g,' '))}) —
          stock is down ${fiveD.pct.toFixed(2)}% over 5 sessions. Watch entry timing.`;
      }
    }

    // Day-change pill in head (Today's move)
    const todayPill = card.querySelector('[data-role="day-chg"]');
    if (todayPill) {
      try {
        const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}`);
        const j = await r.json();
        const data = (j && (j.data || j)) || {};
        const chg = data.change_pct ?? data.day_change_pct ?? data.signal?.change_pct;
        if (chg != null && !isNaN(chg)) {
          const c = colorPct(chg);
          todayPill.textContent = (chg >= 0 ? '+' : '') + Number(chg).toFixed(2) + '% today';
          todayPill.style.color = c;
          todayPill.style.background = `color-mix(in srgb, ${c} 14%, transparent)`;
          todayPill.style.borderColor = `color-mix(in srgb, ${c} 30%, transparent)`;
        } else {
          todayPill.textContent = '— today';
        }
      } catch (_) { todayPill.textContent = '— today'; }
    }

    bindTabs(card);

    // ── Edge enrichment (sector regime, leak flag, insider buys, smart money, hit rate) ──
    try {
      // Find or create the edge-chips row at the bottom of the card
      let chipsRow = card.querySelector('[data-role="edge-chips"]');
      if (!chipsRow) {
        chipsRow = document.createElement('div');
        chipsRow.setAttribute('data-role', 'edge-chips');
        chipsRow.className = 'curated-edge-chips';
        card.appendChild(chipsRow);
      }
      const chips = [];

      // 1) Sector regime
      const regimes = await _getSectorRegime();
      const sk = _normaliseSector(sig.sector);
      if (sk && regimes[sk]) {
        const r = regimes[sk];
        const isTail = r.regime.includes('tailwind');
        const isHead = r.regime.includes('headwind');
        const c = isTail ? '#2dd4aa' : isHead ? '#f26b6b' : '#8a94a8';
        const icon = isTail ? '↗' : isHead ? '↘' : '→';
        chips.push(`<span class="curated-edge-chip" style="background:${c}14;color:${c};border-color:${c}40;"
          title="${escapeHtml(r.label)}">${icon} ${sk} ${r.change_pct>=0?'+':''}${r.change_pct}%</span>`);
      }

      // 2) Leak check (fires only for fresh signals with an event_id)
      if (sig.event_id) {
        try {
          const lr = await fetch(`/api/edge/leak-check/${encodeURIComponent(sig.event_id)}`);
          const lj = await lr.json();
          const ld = lj && lj.data;
          if (ld && (ld.tier === 'strong' || ld.tier === 'moderate')) {
            const c = ld.tier === 'strong' ? '#f26b6b' : '#e6b84a';
            chips.push(`<span class="curated-edge-chip" style="background:${c}14;color:${c};border-color:${c}40;"
              title="${escapeHtml(ld.label)}">⚠ Leak ${ld.pre_window_pct>=0?'+':''}${ld.pre_window_pct}%</span>`);
          }
        } catch(_) {}
      }

      // 3) Insider buys
      try {
        const ir = await fetch(`/api/edge/insider-buys/${encodeURIComponent(ticker)}`);
        const ij = await ir.json();
        const id = ij && ij.data;
        if (id && id.tag) {
          const c = id.tag.startsWith('promoter') ? '#2dd4aa' : '#8eb4e0';
          chips.push(`<span class="curated-edge-chip" style="background:${c}14;color:${c};border-color:${c}40;"
            title="${escapeHtml(id.label)}">★ ${id.tag.replace(/_/g,' ')}</span>`);
        }
      } catch(_) {}

      // 4) Smart money (bulk-deal cross-ref)
      try {
        const sr = await fetch(`/api/edge/bulk-deal-crossref/${encodeURIComponent(ticker)}`);
        const sj = await sr.json();
        const sd = sj && sj.data;
        if (sd && (sd.tag === 'smart_buy' || sd.tag === 'smart_accumulating')) {
          chips.push(`<span class="curated-edge-chip" style="background:#2dd4aa14;color:#2dd4aa;border-color:#2dd4aa40;"
            title="${escapeHtml(sd.label)}">💰 ${sd.tag === 'smart_accumulating' ? 'Smart accum' : 'Smart buy'}</span>`);
        } else if (sd && sd.tag === 'smart_sell') {
          chips.push(`<span class="curated-edge-chip" style="background:#f26b6b14;color:#f26b6b;border-color:#f26b6b40;"
            title="${escapeHtml(sd.label)}">↘ Smart sell</span>`);
        }
      } catch(_) {}

      // 5) Source hit-rate (calibrated alpha)
      if (sig.source) {
        try {
          const hr = await fetch(`/api/edge/outcomes/source-hit-rate?source=${encodeURIComponent(sig.source)}&horizon=3d`);
          const hj = await hr.json();
          const hd = hj && hj.data;
          if (hd && hd.n >= 3) {
            const pct = Math.round(hd.hit_rate * 100);
            const c = pct >= 60 ? '#2dd4aa' : pct >= 40 ? '#8eb4e0' : '#e6b84a';
            chips.push(`<span class="curated-edge-chip" style="background:${c}14;color:${c};border-color:${c}40;"
              title="Source historical 3D hit rate (n=${hd.n})">📊 src ${pct}%</span>`);
          }
        } catch(_) {}
      }

      // 6) Earnings call sentiment (only for earnings events)
      if (sig.event_id && /earning|result/i.test(sig.event_type || '')) {
        try {
          const cr = await fetch(`/api/edge/earnings/call-sentiment/${encodeURIComponent(sig.event_id)}`);
          const cj = await cr.json();
          const cd = cj && cj.data;
          if (cd && cd.tag) {
            const up = cd.tag === 'guidance_raised' || cd.tag === 'positive_tone';
            const down = cd.tag === 'guidance_cautious' || cd.tag === 'negative_tone';
            const c = up ? '#2dd4aa' : down ? '#f26b6b' : '#e6b84a';
            chips.push(`<span class="curated-edge-chip" style="background:${c}14;color:${c};border-color:${c}40;"
              title="${escapeHtml(cd.label)}">🎙 ${cd.tag.replace(/_/g,' ')}</span>`);
          }
        } catch(_) {}
      }

      chipsRow.innerHTML = chips.join('');
      if (!chips.length) chipsRow.remove();
    } catch (e) { /* edge enrichment is best-effort */ }
  }

  function hydrate(host, signals) {
    signals.forEach((sig) => {
      const card = host.querySelector(`.curated-card[data-ticker="${(sig.ticker||'').replace(/"/g,'\\"')}"]`);
      if (card) hydrateOne(card, sig);
    });
  }

  // ── CSS ────────────────────────────────────────────────────────────────────
  function ensureStyles() {
    if (document.getElementById('curated-signals-css')) return;
    const s = document.createElement('style');
    s.id = 'curated-signals-css';
    s.textContent = `
      .curated-card { background:#10141a; border:1px solid rgba(141,148,168,0.14);
        border-radius:12px; padding:14px; display:flex; flex-direction:column; gap:10px;
        transition: border-color 160ms, transform 160ms; cursor:pointer; }
      .curated-card:hover { border-color:rgba(141,180,224,0.5); transform:translateY(-2px); }
      .curated-card--dense { padding:10px 12px; gap:6px; }

      .curated-row { display:flex; align-items:center; gap:8px; }
      .curated-row--meta { font-family:'Geist Mono',monospace; font-size:10px; color:#8a94a8; }
      .curated-rank { font-family:'Geist Mono',monospace; font-size:9px; color:#5a6373; font-weight:800; }
      .curated-tk { font-family:'Geist Mono',monospace; font-size:13px; font-weight:800; color:#f4f6fb; flex:1; }
      .curated-alpha-pill { font-family:'Geist Mono',monospace; font-size:11px; font-weight:800;
        padding:2px 8px; border-radius:999px; color:var(--c);
        background:color-mix(in srgb, var(--c) 12%, transparent); }

      .curated-mini-chart { width:100%; height:100px; display:block; background:rgba(7,9,13,0.5);
        border:1px solid rgba(141,148,168,0.08); border-radius:6px; }
      .curated-card--dense .curated-mini-chart { height:56px; border-radius:4px; }

      .curated-chart-tabs { display:flex; gap:4px; padding:4px 0; }
      .curated-chart-tabs button {
        font:600 9.5px 'Inter',sans-serif; padding:3px 8px; border-radius:999px;
        border:1px solid rgba(141,148,168,0.16); background:transparent; color:#8a94a8;
        cursor:pointer; transition:all 100ms; letter-spacing:0.06em;
      }
      .curated-chart-tabs button:hover { color:#dde3ef; border-color:rgba(141,180,224,0.4); }
      .curated-chart-tabs button.on { color:#2dd4aa; border-color:rgba(45,212,170,0.4);
        background:rgba(45,212,170,0.08); }
      .curated-chart-tabs--dense button { font-size:9px; padding:2px 6px; }

      .curated-conv { font-weight:700; }
      .curated-pct.bull { color:#2dd4aa; }
      .curated-pct.bear { color:#f26b6b; }
      .curated-hold { color:#5a6373; margin-left:auto; }

      .curated-head { display:flex; gap:12px; align-items:flex-start; }
      .curated-head-l { flex:1; min-width:0; }
      .curated-tk-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px; }
      .curated-tk-big { font-family:'Geist Mono',monospace; font-size:17px; font-weight:800; color:#f4f6fb; }
      .curated-conv-chip { font-size:9px; font-weight:700; letter-spacing:0.08em;
        text-transform:uppercase; padding:2px 8px; border-radius:4px; }
      .curated-evtype { font-size:9px; color:#8a94a8; text-transform:uppercase;
        letter-spacing:0.1em; font-weight:700; }
      .curated-day-chg { font:700 10px 'Geist Mono',monospace; padding:2px 8px;
        border-radius:999px; color:#8a94a8;
        background:rgba(141,148,168,0.12);
        border:1px solid rgba(141,148,168,0.2);
        letter-spacing:0.04em; }
      .curated-headline { font-size:12px; color:#c2c6d6; line-height:1.4;
        display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }

      .curated-alpha-block { text-align:right; flex-shrink:0; }
      .curated-alpha-num { font-family:'Geist Mono',monospace; font-size:26px;
        font-weight:800; color:var(--c); line-height:1; }
      .curated-alpha-lbl { font-size:9px; color:#5a6373; font-weight:700;
        letter-spacing:0.16em; margin-top:2px; }

      .curated-change-strip { display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
        background:rgba(37,48,64,0.4); border-radius:6px; overflow:hidden; }
      .curated-change { background:rgba(13,17,24,0.92); padding:6px 10px; display:flex;
        flex-direction:column; gap:1px; }
      .curated-change .lbl { font-size:8.5px; color:#5a6373; text-transform:uppercase;
        letter-spacing:0.1em; font-weight:700; }
      .curated-change .val { font:700 12px 'Geist Mono',monospace;
        font-variant-numeric:tabular-nums; color:#8a94a8; }

      .curated-metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
        background:rgba(37,48,64,0.4); border-radius:8px; overflow:hidden; }
      .curated-metric { padding:8px 10px; background:rgba(13,17,24,0.92); display:flex;
        flex-direction:column; gap:2px; }
      .curated-metric-lbl { font-size:8.5px; color:#5a6373; text-transform:uppercase;
        letter-spacing:0.1em; font-weight:700; }
      .curated-metric-val { font:700 12px 'Geist Mono',monospace; color:#dde3ef; }
      .curated-target-pct { font-size:10px; margin-left:4px; }
      .curated-target-pct.bull { color:#2dd4aa; }
      .curated-target-pct.bear { color:#f26b6b; }

      .curated-preds { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; }
      .curated-pred { background:rgba(13,17,24,0.6); border:1px solid rgba(141,148,168,0.1);
        border-radius:6px; padding:7px 10px; text-align:center; }
      .curated-pred-h { font-size:9px; color:#5a6373; font-weight:700;
        letter-spacing:0.12em; text-transform:uppercase; }
      .curated-pred-v { font:700 13px 'Geist Mono',monospace; margin-top:2px;
        font-variant-numeric:tabular-nums; }

      .curated-trend-note { display:flex; align-items:flex-start; gap:8px;
        font-size:11px; color:#c2c6d6; line-height:1.5;
        background:rgba(230,184,74,0.07); border:1px solid rgba(230,184,74,0.22);
        border-radius:8px; padding:8px 10px; }
      .curated-trend-note strong { color:#e6b84a; }

      /* Edge feature chips row (sector regime, leak, insider, smart money, hit rate) */
      .curated-edge-chips { display:flex; flex-wrap:wrap; gap:4px; padding-top:4px;
        border-top:1px dashed rgba(141,148,168,0.1); margin-top:2px; }
      .curated-edge-chip { font:600 9.5px 'Inter',sans-serif; padding:3px 7px;
        border-radius:4px; letter-spacing:0.04em; white-space:nowrap;
        border:1px solid; cursor:default; }

      /* Fundamentals chip + reasoning */
      .curated-fund-chip { font:700 10px 'Geist Mono',monospace; padding:2px 8px;
        border-radius:4px; letter-spacing:0.04em; white-space:nowrap; }
      .curated-fund-row { display:flex; flex-direction:column; gap:6px;
        background:rgba(13,17,24,0.55); border:1px solid rgba(141,148,168,0.12);
        border-radius:8px; padding:8px 10px; }
      .curated-fund-label { font:700 10px 'Inter',sans-serif; letter-spacing:0.06em;
        text-transform:uppercase; }
      .curated-fund-pills { display:flex; flex-wrap:wrap; gap:4px; }
      .curated-fund-pos { font:600 10px 'Inter',sans-serif; padding:2px 7px;
        border-radius:4px; background:rgba(45,212,170,0.10); color:#7ee0c0;
        border:1px solid rgba(45,212,170,0.22); }
      .curated-fund-neg { font:600 10px 'Inter',sans-serif; padding:2px 7px;
        border-radius:4px; background:rgba(242,107,107,0.10); color:#ff9a9a;
        border:1px solid rgba(242,107,107,0.22); }

      @media (max-width: 600px) {
        .curated-metrics { grid-template-columns: repeat(2, 1fr); }
        .curated-change-strip { grid-template-columns: repeat(3, 1fr); }
      }
    `;
    document.head.appendChild(s);
  }
  ensureStyles();

  window.CuratedSignals = {
    fetch: fetchCurated,
    drawMiniChart,
    buildCard,
    hydrate,
    fmtPrice, fmtPct,
    headlineMatchesTicker,   // exported for tests / inspection
    isBrokerCommentary,
  };
})();
