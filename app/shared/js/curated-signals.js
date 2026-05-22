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
  // Cap any single event_type in the slate so during earnings season the
  // picks aren't 8 deep on "earnings" — surface order wins, M&A, capacity
  // expansion, insider activity, policy reactions etc. so the slate
  // reflects the full multi-signal pipeline, not just one trigger type.
  const MAX_PER_EVENT_TYPE = 3;
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
  // Addendum 2026-05-18 — render the "N sources" credibility chip + a
  // collapsible dropdown of source names. cluster_size==1 gets a yellow
  // "single source" tag (still shown, but lower visual weight). 2-4 gets
  // a neutral chip; 5+ gets the green "confirmed" chip.
  function renderSourceChip(sig) {
    const n = Math.max(1, Number(sig.cluster_size || (sig.sources || []).length || 1));
    const sources = Array.isArray(sig.sources) ? sig.sources.filter(Boolean) : [];
    let bg, fg, border, label;
    if (n >= 5)      { bg = 'var(--bull-dim)';    fg = 'var(--bull)';    border = 'var(--bull-border)';    label = `Confirmed by ${n} sources`; }
    else if (n >= 2) { bg = 'var(--accent-dim)';  fg = 'var(--accent)';  border = 'var(--border-strong)';  label = `${n} sources`; }
    else             { bg = 'var(--caution-dim)'; fg = 'var(--caution)'; border = 'var(--caution-border)'; label = 'Single source'; }

    const detailsId = 'srcs-' + Math.random().toString(36).slice(2, 9);
    const sourceList = sources.length
      ? sources.slice(0, 8).map(s => `<span class="curated-src-pill">${escapeHtml(s)}</span>`).join('')
      : '';
    const moreLabel = sources.length > 8 ? ` <span style="color:var(--text-tertiary);font-size:10px;">+${sources.length - 8} more</span>` : '';

    return `<details class="curated-src-details" id="${detailsId}">
      <summary class="curated-src-chip" style="background:${bg};color:${fg};border:1px solid ${border};">
        <span class="curated-src-dot" style="background:${fg};"></span>
        ${label}
      </summary>
      ${sources.length ? `<div class="curated-src-list">${sourceList}${moreLabel}</div>` : ''}
    </details>`;
  }

  function convictionLabel(alpha) {
    /* Labels softened from "High / Moderate / Watch" → analytics framing
       per SEBI compliance (no broker-recommendation language). Colors read
       semantic tokens so chips remain legible in both themes. */
    if (alpha >= 80) return { label: 'Strong signal',   color: 'var(--bull)' };
    if (alpha >= 65) return { label: 'Moderate signal', color: 'var(--caution)' };
    return                  { label: 'Watchlist',       color: 'var(--info)' };
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
      // Defense-in-depth: never surface a social-derived signal as a
      // recommendation. The DB query already filters these but a stale
      // row could slip through after schema migrations.
      const nt = String(s.news_type || '').toLowerCase();
      if (nt === 'social_buzz' || nt === 'social' || nt === 'reddit'
          || nt === 'twitter' || nt === 'telegram') continue;
      const src = String(s.source || '').toLowerCase();
      if (/\b(reddit|\/r\/|twitter|nitter|stocktwits)\b|t\.me\/|^telegram/.test(src)) continue;
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
    const eventTypeCount = new Map();
    let megaCapUsed = 0;
    const picked = [];
    for (const s of pool) {
      const sec = (s.sector || 'OTHER').toUpperCase();
      const sc  = sectorCount.get(sec) || 0;
      const evt = (s.event_type || 'news').toLowerCase();
      const ec  = eventTypeCount.get(evt) || 0;
      const isMega = MEGA_CAPS.has((s.ticker || '').toUpperCase());
      if (sc >= MAX_PER_SECTOR) continue;
      if (ec >= MAX_PER_EVENT_TYPE) continue;  // diversify across triggers
      if (isMega && megaCapUsed >= MAX_MEGA_CAP_IN_SLATE) continue;
      picked.push(s);
      sectorCount.set(sec, sc + 1);
      eventTypeCount.set(evt, ec + 1);
      if (isMega) megaCapUsed += 1;
      if (picked.length >= n) break;
    }
    // Backfill if quota left us short. Relax the event_type cap last so a
    // genuinely earnings-heavy week can still surface 4+ earnings picks,
    // but only after we've exhausted the other-trigger pool.
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
      ctx.fillStyle = 'var(--text-tertiary)';
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
      ctx.strokeStyle = 'var(--surface-0)'; ctx.lineWidth = 2; ctx.stroke();
      const txt = `${hp.date} · ₹${hp.close.toFixed(2)}`;
      ctx.font = '10px Inter, sans-serif';
      const tw = ctx.measureText(txt).width + 12;
      let tx = hp.x + 8; if (tx + tw > W - 2) tx = hp.x - tw - 8;
      ctx.fillStyle = 'rgba(7,9,13,0.92)';
      ctx.fillRect(tx, 2, tw, 16);
      ctx.strokeStyle = 'rgba(141,148,168,0.3)'; ctx.lineWidth = 1;
      ctx.strokeRect(tx + 0.5, 2.5, tw - 1, 15);
      ctx.fillStyle = 'var(--text-primary)';
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
      const c = FUND_COLOR[fund.tier] || 'var(--text-secondary)';
      fundChip = `<span class="curated-fund-chip" data-edge-analyze="${escapeHtml(sig.ticker)}"
        title="Click for full fundamental analysis · ${escapeHtml(fund.label || '')} (${fund.score}/100)"
        style="background:${c}1f;color:${c};border:1px solid ${c}55;cursor:pointer;">F&nbsp;${fund.score}</span>`;

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

    // Sector chip (always visible — on every card)
    const sectorRaw = (sig.sector || '').toString().trim();
    const sectorLabel = sectorRaw ? sectorRaw.replace(/_/g, ' ').toUpperCase() : 'SECTOR —';
    const sectorChip = `<span class="curated-sector-chip" title="Sector: ${escapeHtml(sectorLabel)}">${escapeHtml(sectorLabel)}</span>`;

    // ── Multi-signal evidence chips ─────────────────────────────────────────
    // Surface WHY this pick is good beyond just alpha_score — these chips
    // make the multi-signal pipeline visible on the card:
    //   • Forensic quality (manipulation_score → clean/unverified/suspicious)
    //   • Volume confirmation (price move backed by elevated volume?)
    //   • Regime alignment (already-trending vs counter-trend)
    // Only shown when there's something signal-worthy to say so the card
    // stays scannable.
    const evidenceChips = [];
    const mScore = Number(sig.manipulation_score);
    if (!Number.isNaN(mScore)) {
      let lbl, col, tip;
      if      (mScore <  20) { lbl = '✓ CLEAN';     col = '#2dd4aa'; tip = 'Forensic checks clean'; }
      else if (mScore <  50) { lbl = 'UNVERIFIED';  col = '#e6b84a'; tip = 'Forensic checks unverified'; }
      else if (mScore <  70) { lbl = '⚠ SUSPICIOUS';col = '#f29090'; tip = 'Forensic flags raised'; }
      else                   { lbl = '⚠ MANIP';     col = '#f26b6b'; tip = 'Forensic: likely manipulated'; }
      evidenceChips.push(
        `<span class="curated-evidence-chip" title="${escapeHtml(tip)} (score ${Math.round(mScore)}/100)"
               style="color:${col};border-color:${col}55;background:${col}1a;">${lbl}</span>`
      );
    }
    const vMult = Number(sig.volume_multiplier);
    if (!Number.isNaN(vMult) && vMult >= 1.3) {
      const isHigh = vMult >= 2.0;
      const col = isHigh ? '#2dd4aa' : '#8eb4e0';
      const lbl = isHigh ? `VOL ${vMult.toFixed(1)}×` : `VOL ↑`;
      evidenceChips.push(
        `<span class="curated-evidence-chip" title="Volume ${vMult.toFixed(2)}× the 20-day avg"
               style="color:${col};border-color:${col}55;background:${col}1a;">${lbl}</span>`
      );
    }
    const regime = (sig.regime || '').toString().toLowerCase();
    if (regime === 'spike_up' || regime === 'spike_down') {
      const isUp = regime === 'spike_up';
      const col = isUp ? '#2dd4aa' : '#f26b6b';
      evidenceChips.push(
        `<span class="curated-evidence-chip" title="Price regime: ${escapeHtml(regime)} (strength ${(Number(sig.regime_strength) || 0).toFixed(2)})"
               style="color:${col};border-color:${col}55;background:${col}1a;">${isUp ? 'TRENDING ↑' : 'TRENDING ↓'}</span>`
      );
    }
    const evidenceHtml = evidenceChips.join('');

    // Multi-timeframe price change strip — values hydrated after render
    return `
    <div class="curated-card" data-ticker="${escapeHtml(sig.ticker)}">
      <div class="curated-head">
        <div class="curated-head-l">
          <div class="curated-tk-row">
            <span class="curated-tk-big" data-edge-analyze="${escapeHtml(sig.ticker)}" title="Click for full fundamental analysis" style="cursor:pointer;">${escapeHtml(sig.ticker || '—')}</span>
            <span class="curated-conv-chip" style="background:${conv.color}22;color:${conv.color};border:1px solid ${conv.color}55;">${conv.label}</span>
            ${fundChip}
            ${sectorChip}
            <span class="curated-evtype">${escapeHtml(evType)}</span>
            ${evidenceHtml}
            <span class="curated-day-chg" data-role="day-chg">—</span>
          </div>
          <!-- Addendum 2026-05-18: prefer our canonical_headline (computed
               from cluster) over the publisher's headline. The synthesized
               summary line that follows is our paraphrase, NOT the RSS blurb. -->
          <div class="curated-headline">${escapeHtml((sig.synthesized_headline || sig.canonical_headline || sig.headline || '').slice(0, 140))}</div>
          ${sig.synthesized_summary ? `<div class="curated-synth-summary">${escapeHtml(sig.synthesized_summary)}</div>` : ''}
          ${renderSourceChip(sig)}
        </div>
        <div class="curated-alpha-block" style="--c:${conv.color}">
          <div class="curated-alpha-num">${alpha}</div>
          <div class="curated-alpha-lbl">ALPHA</div>
        </div>
      </div>

      <div class="curated-chart-tabs" data-target="${canvasId}">${tabsHtml}</div>
      <div id="${canvasId}" class="curated-mini-chart curated-mini-chart--adv" data-ticker="${escapeHtml(sig.ticker)}" style="width:100%;height:100px;"></div>

      <!-- OHLCV strip — open / high / low / close / volume — hydrated after fetch -->
      <div class="curated-ohlcv" data-role="ohlcv">
        <div class="curated-ohlcv-cell"><div class="lbl">Open</div><div class="val" data-ohlcv="open">—</div></div>
        <div class="curated-ohlcv-cell"><div class="lbl">High</div><div class="val" data-ohlcv="high">—</div></div>
        <div class="curated-ohlcv-cell"><div class="lbl">Low</div><div class="val" data-ohlcv="low">—</div></div>
        <div class="curated-ohlcv-cell"><div class="lbl">Close</div><div class="val" data-ohlcv="close">—</div></div>
        <div class="curated-ohlcv-cell"><div class="lbl">Volume</div><div class="val" data-ohlcv="volume">—</div></div>
      </div>

      <!-- Multi-timeframe price change strip — hydrated after chart loads -->
      <div class="curated-change-strip" data-role="change-strip">
        <div class="curated-change"><div class="lbl">Today</div><div class="val" data-tf="1d">—</div></div>
        <div class="curated-change"><div class="lbl">5-day</div><div class="val" data-tf="5d">—</div></div>
        <div class="curated-change"><div class="lbl">1-month</div><div class="val" data-tf="1mo">—</div></div>
      </div>

      <!-- Statistical reference levels — phrased as analytics, not a trade
           recommendation. "Indicative" badges visually demote these from
           "broker pick" to "analyst worksheet" framing (SEBI safer). -->
      <div class="curated-metrics-header">
        Statistical reference — not a trade recommendation
      </div>
      <div class="curated-metrics">
        <div class="curated-metric">
          <div class="curated-metric-lbl">Reference entry <span class="curated-ind">indicative</span></div>
          <div class="curated-metric-val">${fmtPrice(sig.entry_price)}</div>
        </div>
        <div class="curated-metric">
          <div class="curated-metric-lbl">Indicative level (${sig.target_horizon || '3D'}) <span class="curated-ind">indicative</span></div>
          <div class="curated-metric-val">${fmtPrice(sig.target_price)} <span class="curated-target-pct ${tgtClass}">${tgtPctTxt}</span></div>
        </div>
        <div class="curated-metric">
          <div class="curated-metric-lbl">Indicative horizon</div>
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

      <!-- Methodology disclosure — expandable explainer per card. Falls back
           to a static label on browsers without <details> support. -->
      <details class="curated-method">
        <summary>How this signal was computed</summary>
        <div class="curated-method__body">
          Derived from a public ${escapeHtml(sig.event_type || 'corporate event')} signal
          sourced from <strong>${escapeHtml(sig.first_seen_source || sig.source || 'exchange/news feed')}</strong>.
          Alpha score (${(sig.alpha_score || 0).toFixed(0)}/100) reflects historical
          event-outcome hit rate in this category, adjusted for current market regime.
          Reference levels above are statistical extrapolations from prior similar
          events — not analyst price targets. Full methodology:
          <a href="methodology.html">/methodology</a>.
        </div>
      </details>

      <!-- Per-card SEBI disclaimer -->
      <div class="curated-disclaimer">
        Informational only — not investment advice.
        <a href="disclosures.html">Why</a>
      </div>
    </div>`;
  }

  // ── Hydration: wire chart tabs + load all periods to fill change strip ────
  // Tabs use the period IDs `1d / 5d / 1mo / 3mo` which map directly to
  // yfinance periods — same identifiers AdvancedChart uses internally.
  function bindTabs(card) {
    const tabBars = card.querySelectorAll('.curated-chart-tabs');
    tabBars.forEach((bar) => {
      const host = document.getElementById(bar.dataset.target);
      if (!host) return;
      bar.addEventListener('click', async (e) => {
        const btn = e.target.closest('button[data-period]');
        if (!btn) return;
        bar.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
        const inst = host.__advChart;
        if (inst && inst.setPeriod) inst.setPeriod(btn.dataset.period);
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
    // Mount AdvancedChart in compact mode — same visual language as the big
    // chart on stock.html. Drag-to-zoom enabled out of the box.
    const chartHost = card.querySelector('.curated-mini-chart--adv');
    if (chartHost && window.AdvancedChart) {
      const inst = await AdvancedChart.mount(chartHost, ticker, { period: '1mo', compact: true });
      chartHost.__advChart = inst;
    } else {
      const legacy = card.querySelector('canvas');
      if (legacy) await drawMiniChart(legacy, ticker, { period: '1mo' });
    }

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

    // Day-change pill in head + OHLCV strip (one fetch, two destinations)
    const todayPill = card.querySelector('[data-role="day-chg"]');
    const ohlcvRow  = card.querySelector('[data-role="ohlcv"]');
    if (todayPill || ohlcvRow) {
      try {
        const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}`);
        const j = await r.json();
        const data = (j && (j.data || j)) || {};
        const price = data.price || {};
        const chg = price.change_pct ?? data.change_pct ?? data.signal?.change_pct;
        if (todayPill) {
          if (chg != null && !isNaN(chg)) {
            const c = colorPct(chg);
            todayPill.textContent = (chg >= 0 ? '+' : '') + Number(chg).toFixed(2) + '% today';
            todayPill.style.color = c;
            todayPill.style.background = `color-mix(in srgb, ${c} 14%, transparent)`;
            todayPill.style.borderColor = `color-mix(in srgb, ${c} 30%, transparent)`;
          } else {
            todayPill.textContent = '— today';
          }
        }
        if (ohlcvRow) {
          const fmtN = (v) => (v == null || !isFinite(v) || v === 0)
            ? '—' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 });
          const fmtVol = (v) => {
            if (!v || !isFinite(v)) return '—';
            if (v >= 1e7) return (v / 1e7).toFixed(2) + 'Cr';
            if (v >= 1e5) return (v / 1e5).toFixed(2) + 'L';
            if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
            return String(v);
          };
          const set = (k, val) => {
            const el = ohlcvRow.querySelector(`[data-ohlcv="${k}"]`);
            if (el) el.textContent = val;
          };
          set('open',  fmtN(price.day_open));
          set('high',  fmtN(price.day_high));
          set('low',   fmtN(price.day_low));
          set('close', fmtN(price.price));
          set('volume', fmtVol(price.volume));
          // Tint close cell by direction
          const closeCell = ohlcvRow.querySelector('[data-ohlcv="close"]');
          if (closeCell && chg != null) closeCell.style.color = colorPct(chg);
        }
      } catch (_) {
        if (todayPill) todayPill.textContent = '— today';
      }
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
        const c = isTail ? '#2dd4aa' : isHead ? '#f26b6b' : 'var(--text-secondary)';
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
      .curated-card { background:var(--surface-0); border:1px solid rgba(141,148,168,0.14);
        border-radius:12px; padding:14px; display:flex; flex-direction:column; gap:10px;
        transition: border-color 160ms, transform 160ms; cursor:pointer; }
      .curated-card:hover { border-color:rgba(141,180,224,0.5); transform:translateY(-2px); }
      .curated-card--dense { padding:10px 12px; gap:6px; }

      .curated-row { display:flex; align-items:center; gap:8px; }
      .curated-row--meta { font-family:'Geist Mono',monospace; font-size:10px; color:var(--text-secondary); }
      .curated-rank { font-family:'Geist Mono',monospace; font-size:9px; color:var(--text-tertiary); font-weight:800; }
      .curated-tk { font-family:'Geist Mono',monospace; font-size:13px; font-weight:800; color:var(--text-primary); flex:1; }
      .curated-alpha-pill { font-family:'Geist Mono',monospace; font-size:11px; font-weight:800;
        padding:2px 8px; border-radius:999px; color:var(--c);
        background:color-mix(in srgb, var(--c) 12%, transparent); }

      .curated-mini-chart { width:100%; height:100px; display:block; background:rgba(7,9,13,0.5);
        border:1px solid rgba(141,148,168,0.08); border-radius:6px; }
      .curated-card--dense .curated-mini-chart { height:56px; border-radius:4px; }

      .curated-chart-tabs { display:flex; gap:4px; padding:4px 0; }
      .curated-chart-tabs button {
        font:600 9.5px 'Inter',sans-serif; padding:3px 8px; border-radius:999px;
        border:1px solid rgba(141,148,168,0.16); background:transparent; color:var(--text-secondary);
        cursor:pointer; transition:all 100ms; letter-spacing:0.06em;
      }
      .curated-chart-tabs button:hover { color:var(--text-primary); border-color:rgba(141,180,224,0.4); }
      .curated-chart-tabs button.on { color:#2dd4aa; border-color:rgba(45,212,170,0.4);
        background:rgba(45,212,170,0.08); }
      .curated-chart-tabs--dense button { font-size:9px; padding:2px 6px; }

      .curated-conv { font-weight:700; }
      .curated-pct.bull { color:#2dd4aa; }
      .curated-pct.bear { color:#f26b6b; }
      .curated-hold { color:var(--text-tertiary); margin-left:auto; }

      .curated-head { display:flex; gap:12px; align-items:flex-start; }
      .curated-head-l { flex:1; min-width:0; }
      .curated-tk-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px; }
      .curated-tk-big { font-family:'Geist Mono',monospace; font-size:17px; font-weight:800; color:var(--text-primary); }
      .curated-conv-chip { font-size:9px; font-weight:700; letter-spacing:0.08em;
        text-transform:uppercase; padding:2px 8px; border-radius:4px; }
      .curated-evtype { font-size:9px; color:var(--text-secondary); text-transform:uppercase;
        letter-spacing:0.1em; font-weight:700; }
      .curated-day-chg { font:700 10px 'Geist Mono',monospace; padding:2px 8px;
        border-radius:999px; color:var(--text-secondary);
        background:rgba(141,148,168,0.12);
        border:1px solid rgba(141,148,168,0.2);
        letter-spacing:0.04em; }
      .curated-headline { font-size:12px; color:var(--text-primary); line-height:1.4;
        display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
      /* Addendum 2026-05-18 — synthesized 1-line fact + N-sources chip */
      .curated-synth-summary { font-size:11px; color:var(--text-secondary);
        line-height:1.4; margin-top:3px; font-style:italic;
        display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
      .curated-src-details { margin-top:5px; }
      .curated-src-details > summary {
        list-style:none; display:inline-flex; align-items:center; gap:5px;
        cursor:pointer; user-select:none; font:600 10px 'Inter',sans-serif;
        letter-spacing:0.04em; padding:3px 9px; border-radius:999px;
        transition:filter 100ms ease;
      }
      .curated-src-details > summary::-webkit-details-marker { display:none; }
      .curated-src-details > summary::after {
        content:'▾'; margin-left:2px; font-size:9px; opacity:0.7;
        transition:transform 120ms ease;
      }
      .curated-src-details[open] > summary::after { transform:rotate(180deg); }
      .curated-src-details > summary:hover { filter:brightness(1.1); }
      .curated-src-dot { width:5px; height:5px; border-radius:50%; flex-shrink:0; }
      .curated-src-list { margin-top:6px; display:flex; flex-wrap:wrap; gap:5px;
        padding:6px 8px; background:var(--surface-2); border:1px solid var(--border-subtle);
        border-radius:8px; }
      .curated-src-pill { font:600 10px 'Inter',sans-serif; padding:2px 7px;
        background:var(--surface-1); border:1px solid var(--border-subtle);
        border-radius:4px; color:var(--text-secondary); }

      .curated-alpha-block { text-align:right; flex-shrink:0; }
      .curated-alpha-num { font-family:'Geist Mono',monospace; font-size:26px;
        font-weight:800; color:var(--c); line-height:1; }
      .curated-alpha-lbl { font-size:9px; color:var(--text-tertiary); font-weight:700;
        letter-spacing:0.16em; margin-top:2px; }

      .curated-change-strip { display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
        background:rgba(37,48,64,0.4); border-radius:6px; overflow:hidden; }
      .curated-change { background:rgba(13,17,24,0.92); padding:6px 10px; display:flex;
        flex-direction:column; gap:1px; }
      .curated-change .lbl { font-size:8.5px; color:var(--text-tertiary); text-transform:uppercase;
        letter-spacing:0.1em; font-weight:700; }
      .curated-change .val { font:700 12px 'Geist Mono',monospace;
        font-variant-numeric:tabular-nums; color:var(--text-secondary); }

      /* OHLCV strip (Open / High / Low / Close / Volume) */
      .curated-ohlcv { display:grid; grid-template-columns:repeat(5,1fr); gap:1px;
        background:rgba(37,48,64,0.4); border-radius:6px; overflow:hidden; }
      .curated-ohlcv-cell { background:rgba(13,17,24,0.92); padding:6px 8px;
        display:flex; flex-direction:column; gap:2px; min-width:0; }
      .curated-ohlcv-cell .lbl { font-size:8.5px; color:var(--text-tertiary); text-transform:uppercase;
        letter-spacing:0.1em; font-weight:700; }
      .curated-ohlcv-cell .val { font:700 11px 'Geist Mono',monospace;
        font-variant-numeric:tabular-nums; color:var(--text-primary);
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

      /* Sector chip (always on card head row) */
      .curated-sector-chip { font:700 9px 'Inter',sans-serif; letter-spacing:0.08em;
        text-transform:uppercase; padding:2px 8px; border-radius:4px;
        background:rgba(141,148,168,0.10); color:#a8b1c7;
        border:1px solid rgba(141,148,168,0.22); white-space:nowrap; }

      .curated-metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
        background:rgba(37,48,64,0.4); border-radius:8px; overflow:hidden; }
      .curated-metric { padding:8px 10px; background:rgba(13,17,24,0.92); display:flex;
        flex-direction:column; gap:2px; }
      .curated-metric-lbl { font-size:8.5px; color:var(--text-tertiary); text-transform:uppercase;
        letter-spacing:0.1em; font-weight:700; }
      .curated-metric-val { font:700 12px 'Geist Mono',monospace; color:var(--text-primary); }
      .curated-target-pct { font-size:10px; margin-left:4px; }
      .curated-target-pct.bull { color:#2dd4aa; }
      .curated-target-pct.bear { color:#f26b6b; }

      .curated-preds { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; }
      .curated-pred { background:rgba(13,17,24,0.6); border:1px solid rgba(141,148,168,0.1);
        border-radius:6px; padding:7px 10px; text-align:center; }
      .curated-pred-h { font-size:9px; color:var(--text-tertiary); font-weight:700;
        letter-spacing:0.12em; text-transform:uppercase; }
      .curated-pred-v { font:700 13px 'Geist Mono',monospace; margin-top:2px;
        font-variant-numeric:tabular-nums; }

      .curated-trend-note { display:flex; align-items:flex-start; gap:8px;
        font-size:11px; color:var(--text-primary); line-height:1.5;
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
        .curated-ohlcv { grid-template-columns: repeat(5, 1fr); }
        .curated-ohlcv-cell { padding:5px 6px; }
        .curated-ohlcv-cell .val { font-size:10px; }
      }
      /* Very narrow phones — collapse OHLCV from 5 → 3 (drops Open/High off
         the strip; Volume + Low + Close are the three a trader needs at a
         glance). Card padding tightens, sector chip wraps below ticker. */
      @media (max-width: 380px) {
        .curated-card { padding: 12px; gap: 8px; }
        .curated-ohlcv { grid-template-columns: repeat(3, 1fr); }
        .curated-ohlcv-cell:nth-child(1),
        .curated-ohlcv-cell:nth-child(2) { display: none; }
        .curated-alpha-num { font-size: 22px; }
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
