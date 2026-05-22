/* edge-analyzer.js — site-wide fundamental analyzer modal.
 *
 * Any element with `data-edge-analyze="<TICKER>"` opens a modal showing:
 *   - Fundamental score (0-100) + tier
 *   - Positives + red flags
 *   - Component breakdown
 *   - Insider buys / earnings whisper / smart-money bulk deals
 *
 * Mounted via bootstrap.js so it works on every page (stock.html, events.html,
 * etc), not just index.html.
 *
 * Exposes: window.EdgeAnalyzer.open(ticker)
 */
(function () {
  if (window.EdgeAnalyzer) return;

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (v, d) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d != null ? d : 2);

  function ensureStyles() {
    if (document.getElementById('edge-analyzer-css')) return;
    const s = document.createElement('style');
    s.id = 'edge-analyzer-css';
    s.textContent = `
      #edgeAnalyzerBackdrop {
        animation: edgeFadeIn 200ms ease-out;
      }
      @keyframes edgeFadeIn {
        from { background:rgba(0,0,0,0); backdrop-filter:blur(0); }
        to   { background:rgba(0,0,0,0.78); backdrop-filter:blur(8px); }
      }
      #edgeAnalyzerCard {
        animation: edgeCardIn 280ms cubic-bezier(0.16, 1, 0.3, 1);
      }
      @keyframes edgeCardIn {
        from { transform: translateY(14px) scale(0.985); opacity: 0; }
        to   { transform: translateY(0) scale(1);          opacity: 1; }
      }
      .ea-arc { transform: rotate(-90deg); transition: stroke-dashoffset 700ms cubic-bezier(0.16, 1, 0.3, 1); }
      .ea-section { margin-top: 22px; }
      .ea-sect-h {
        font: 700 9.5px 'Inter', sans-serif;
        letter-spacing: 0.18em; text-transform: uppercase;
        color: var(--text-tertiary); margin-bottom: 10px;
        display: flex; align-items: center; gap: 8px;
      }
      .ea-sect-h::after {
        content: ''; flex: 1; height: 1px;
        background: linear-gradient(90deg, var(--border-strong), transparent);
      }
      .ea-pill {
        display: inline-flex; align-items: center; gap: 5px;
        font: 600 11px 'Inter', sans-serif;
        padding: 5px 11px; border-radius: 999px;
        margin: 2px 3px 2px 0; line-height: 1.3;
      }
      .ea-pill-pos { background: var(--bull-dim); color: var(--bull);
                     border: 1px solid var(--bull-border); }
      .ea-pill-neg { background: var(--bear-dim); color: var(--bear);
                     border: 1px solid var(--bear-border); }
      .ea-comp {
        display: grid; grid-template-columns: 1fr auto;
        align-items: center; gap: 14px;
        padding: 11px 14px; border-radius: 12px;
        background: var(--surface-2);
        border: 1px solid var(--border-subtle);
        margin-bottom: 6px;
        transition: border-color 120ms;
      }
      .ea-comp:hover { border-color: var(--border-strong); }
      .ea-comp-lbl { font: 700 12.5px 'Inter', sans-serif; color: var(--text-primary);
                      letter-spacing: -0.005em; }
      .ea-comp-sub { font: 600 10.5px 'Geist Mono', monospace;
                      color: var(--text-tertiary); margin-top: 2px; font-variant-numeric: tabular-nums; }
      .ea-comp-num { font: 800 16px 'Geist Mono', monospace;
                      font-variant-numeric: tabular-nums; line-height: 1;
                      padding: 6px 10px; border-radius: 8px; }
      .ea-extra-block {
        background: var(--surface-2);
        border: 1px solid var(--border-subtle);
        border-radius: 12px; padding: 12px 14px; margin-top: 8px;
      }
      .ea-quarter-pill {
        display: inline-flex; gap: 4px; align-items: baseline;
        font: 700 11px 'Geist Mono', monospace;
        padding: 4px 10px; border-radius: 8px;
        margin: 3px 4px 3px 0; line-height: 1.2;
      }
    `;
    document.head.appendChild(s);
  }

  function ensureModal() {
    ensureStyles();
    let backdrop = document.getElementById('edgeAnalyzerBackdrop');
    if (backdrop) return backdrop;
    backdrop = document.createElement('div');
    backdrop.id = 'edgeAnalyzerBackdrop';
    backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9998;display:none;align-items:flex-start;justify-content:center;padding:36px 16px 64px;overflow-y:auto;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);';
    /* Modal chrome now reads tokens — was a hardcoded dark gradient that
       bled through light mode as a "black spot". Bull/bear semantic colors
       inside the body are intentional and remain. */
    backdrop.innerHTML = `
      <div id="edgeAnalyzerCard" style="background:var(--surface-1);border:1px solid var(--border-strong);border-radius:22px;max-width:760px;width:100%;padding:0;box-shadow:var(--shadow-lg);max-height:88vh;overflow:hidden;display:flex;flex-direction:column;">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:18px 24px;border-bottom:1px solid var(--border-subtle);background:var(--surface-2);">
          <div style="display:flex;align-items:center;gap:10px;">
            <span style="width:6px;height:6px;border-radius:50%;background:var(--bull);box-shadow:0 0 12px var(--bull-dim);"></span>
            <h3 id="edgeAnalyzerTitle" style="font:800 14px 'Inter',sans-serif;color:var(--text-primary);letter-spacing:0.02em;">Fundamental analysis</h3>
          </div>
          <button id="edgeAnalyzerClose" style="background:transparent;border:none;color:var(--text-secondary);font-size:22px;cursor:pointer;line-height:1;width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;transition:all 120ms;" onmouseover="this.style.background='var(--surface-3)';this.style.color='var(--text-primary)';" onmouseout="this.style.background='transparent';this.style.color='var(--text-secondary)';">×</button>
        </div>
        <div id="edgeAnalyzerBody" style="padding:24px 26px;overflow-y:auto;flex:1;"></div>
      </div>`;
    document.body.appendChild(backdrop);
    const close = () => { backdrop.style.display = 'none'; };
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
    backdrop.querySelector('#edgeAnalyzerClose').addEventListener('click', close);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && backdrop.style.display !== 'none') close();
    });
    return backdrop;
  }

  async function open(ticker) {
    ticker = (ticker || '').trim().toUpperCase();
    if (!ticker) return;
    // Hard delegate to the neome-style analyzer in index.html whenever
    // it's wired on the page — guarantees direct callers (stock.html
    // retry button, map.html, etc.) all route through the new narrative
    // modal and never see the legacy 405/404 endpoints.
    if (typeof window.openAnalyzer === 'function' && window.openAnalyzer !== open) {
      return window.openAnalyzer(ticker);
    }
    const backdrop = ensureModal();
    const title = document.getElementById('edgeAnalyzerTitle');
    const body  = document.getElementById('edgeAnalyzerBody');
    title.textContent = 'Analyzing ' + ticker + '…';
    body.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-secondary);">Computing fundamental score…</div>';
    backdrop.style.display = 'flex';

    let payload = null;
    let quota = null;
    let httpStatus = 200;
    let quotaError = null;
    let fetchError = null;
    const token = localStorage.getItem('tw_token') || localStorage.getItem('auth_token') || '';
    const authHeaders = token ? { Authorization: 'Bearer ' + token } : {};
    try {
      const r = await fetch('/api/fundamentals/score/' + encodeURIComponent(ticker), { headers: authHeaders });
      httpStatus = r.status;
      const j = await r.json();
      if (r.status === 429 || j.error === 'analysis_quota_exceeded') {
        quotaError = j;
      } else if (!r.ok) {
        fetchError = j && (j.error || j.message) ? (j.error || j.message) : ('HTTP ' + r.status);
      } else {
        payload = j && j.data;
        quota = j && j.quota;
      }
    } catch (e) {
      fetchError = (e && e.message) || 'Network error';
    }

    // ── Daily-cap exhausted: render upgrade-required modal ────────────
    if (quotaError) {
      const used = quotaError.used_today || 0;
      const cap  = quotaError.limit || 3;
      const tier = quotaError.tier || 'free';
      title.textContent = 'Daily analysis limit reached';
      body.innerHTML = `
        <div style="padding:32px 28px;text-align:center;">
          <div style="width:80px;height:80px;margin:0 auto 18px;background:var(--accent-dim);border-radius:50%;display:flex;align-items:center;justify-content:center;">
            <span class="material-symbols-outlined" style="font-size:40px;color:var(--accent);">hourglass_empty</span>
          </div>
          <div style="font:800 22px 'DM Sans',sans-serif;color:var(--text-primary);margin-bottom:8px;">
            You've used ${used}/${quotaError.limit || cap} analyses today
          </div>
          <div style="font:500 14px 'DM Sans',sans-serif;color:var(--text-secondary);line-height:1.55;max-width:420px;margin:0 auto 22px;">
            Fundamental analysis is our headline feature. Free users get 3 unique tickers/day. Upgrade for more depth.
          </div>
          <div style="display:flex;flex-direction:column;gap:10px;max-width:340px;margin:0 auto;">
            <a href="pricing.html" style="background:var(--accent);color:var(--surface-1);padding:13px 22px;border-radius:10px;text-decoration:none;font:700 14px 'Inter',sans-serif;display:flex;align-items:center;justify-content:center;gap:6px;">
              See plans · Starter 30/day · Pro unlimited
              <span class="material-symbols-outlined" style="font-size:16px;">arrow_forward</span>
            </a>
            <div style="font:500 11px 'Inter',sans-serif;color:var(--text-tertiary);">
              Resets at midnight IST · current tier: <strong>${esc(tier)}</strong>
            </div>
          </div>
        </div>`;
      return;
    }

    if (fetchError || !payload || payload.error || payload.score == null) {
      const errMsg = fetchError || (payload && payload.error) || 'no score returned';
      console.warn('[EdgeAnalyzer] fundamental score fetch failed:', errMsg);
      body.innerHTML = `
        <div style="padding:30px;text-align:center;">
          <div style="font:700 18px 'DM Sans',sans-serif;color:var(--text-primary);margin-bottom:6px;">Couldn't analyze ${esc(ticker)}</div>
          <div style="font:500 13px 'Inter',sans-serif;color:var(--text-secondary);margin-bottom:14px;">
            ${esc(String(errMsg))}
          </div>
          <button onclick="EdgeAnalyzer.open('${esc(ticker)}')" style="background:var(--accent,#5b6cff);color:#fff;border:0;padding:8px 18px;border-radius:8px;font:600 13px 'Inter',sans-serif;cursor:pointer;">
            Retry
          </button>
        </div>`;
      return;
    }

    const TIER_C = { strong: '#2dd4aa', decent: '#8eb4e0', weak: '#e6b84a', avoid: '#f26b6b' };
    const c = TIER_C[payload.tier] || 'var(--text-secondary)';
    title.textContent = 'Fundamental analysis · ' + ticker;

    const positives = (payload.positives || []).map(p =>
      `<span class="ea-pill ea-pill-pos">${esc(p)}</span>`).join('');
    const flags = (payload.red_flags || []).map(p =>
      `<span class="ea-pill ea-pill-neg">${esc(p)}</span>`).join('');

    const comp = payload.components || {};
    const compRow = (lbl, score, sub) => {
      const tone = score > 0 ? '#2dd4aa' : score < 0 ? '#f26b6b' : 'var(--text-tertiary)';
      const bg   = score > 0 ? 'rgba(45,212,170,0.10)' :
                   score < 0 ? 'rgba(242,107,107,0.10)' : 'rgba(141,148,168,0.08)';
      return `<div class="ea-comp">
        <div>
          <div class="ea-comp-lbl">${lbl}</div>
          <div class="ea-comp-sub">${sub || '—'}</div>
        </div>
        <div class="ea-comp-num" style="color:${tone};background:${bg};">${score > 0 ? '+' : ''}${score}</div>
      </div>`;
    };

    // Animated arc score: SVG circle stroke-dashoffset animation
    const R = 50, CIRC = 2 * Math.PI * R;
    const fillFrac = Math.max(0, Math.min(1, payload.score / 100));
    const dashOffset = CIRC * (1 - fillFrac);

    // Quota chip: "3/5 today · upgrade" — renders top-right of the modal hero
    const quotaChip = (() => {
      if (!quota) return '';
      const tier = quota.tier || 'free';
      const used = quota.used_today;
      const lim  = quota.limit;
      const rem  = quota.remaining;
      if (lim == null) {
        return `<div style="display:inline-flex;align-items:center;gap:6px;padding:4px 10px;background:var(--bull-dim);color:var(--bull);border:1px solid var(--bull-border);border-radius:999px;font:700 10.5px 'Inter',sans-serif;letter-spacing:0.04em;">
          <span class="material-symbols-outlined" style="font-size:13px;">all_inclusive</span>
          ${esc(tier)} · unlimited
        </div>`;
      }
      const color = rem === 0 ? '#f26b6b' : rem <= 1 ? '#e6b84a' : '#8eb4e0';
      return `<div style="display:inline-flex;align-items:center;gap:6px;padding:4px 10px;background:${color}14;color:${color};border:1px solid ${color}55;border-radius:999px;font:700 10.5px 'Inter',sans-serif;letter-spacing:0.04em;">
        ${used}/${lim} today
        ${tier === 'free' ? `<a href="pricing.html" style="margin-left:4px;color:${color};text-decoration:underline;">upgrade</a>` : ''}
      </div>`;
    })();

    body.innerHTML = `
      <!-- Hero: score + ticker + tier label + quota chip -->
      <div style="display:flex;justify-content:flex-end;margin-bottom:8px;">${quotaChip}</div>
      <div style="display:grid;grid-template-columns:130px 1fr;gap:22px;align-items:center;margin-bottom:6px;">
        <div style="position:relative;width:130px;height:130px;">
          <svg width="130" height="130" viewBox="0 0 130 130">
            <circle cx="65" cy="65" r="${R}" fill="none" stroke="rgba(141,148,168,0.12)" stroke-width="6"/>
            <circle class="ea-arc" cx="65" cy="65" r="${R}" fill="none"
                    stroke="${c}" stroke-width="6" stroke-linecap="round"
                    stroke-dasharray="${CIRC.toFixed(2)}"
                    stroke-dashoffset="${CIRC.toFixed(2)}"
                    style="transform-origin:65px 65px;"/>
          </svg>
          <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;">
            <div style="font:800 30px 'Geist Mono',monospace;color:${c};line-height:1;font-variant-numeric:tabular-nums;">${payload.score}</div>
            <div style="font:700 9px 'Inter',sans-serif;letter-spacing:0.16em;text-transform:uppercase;color:var(--text-tertiary);margin-top:2px;">/ 100</div>
          </div>
        </div>
        <div>
          <div style="font:900 22px 'DM Sans',sans-serif;color:${c};letter-spacing:-0.01em;">${esc(payload.label || '')}</div>
          <div style="font:700 10.5px 'Inter',sans-serif;letter-spacing:0.16em;text-transform:uppercase;color:var(--text-tertiary);margin-top:4px;">
            ${esc(payload.tier || '')} · ${esc(ticker)}
          </div>
          <div style="display:flex;gap:10px;margin-top:14px;font:600 11px 'Inter',sans-serif;color:var(--text-secondary);">
            <div><span style="color:#2dd4aa;">●</span> ${(payload.positives || []).length} positives</div>
            <div><span style="color:#f26b6b;">●</span> ${(payload.red_flags || []).length} red flags</div>
            <div><span style="color:var(--text-tertiary);">●</span> ${Object.keys(comp).length} components</div>
          </div>
        </div>
      </div>

      <!-- AI explainer slot — populates after the parallel fetch below -->
      <div id="edgeAnalyzerExplainer" style="margin-top:18px;"></div>

      ${positives ? `<div class="ea-section">
        <div class="ea-sect-h">What's working</div>
        <div>${positives}</div>
      </div>` : ''}

      ${flags ? `<div class="ea-section">
        <div class="ea-sect-h">Red flags</div>
        <div>${flags}</div>
      </div>` : ''}

      <div class="ea-section">
        <div class="ea-sect-h">Component breakdown</div>
        ${compRow('Valuation',             comp.valuation?.score             || 0, 'PE ' + fmt(comp.valuation?.pe, 1) + '   PB ' + fmt(comp.valuation?.pb, 1))}
        ${compRow('Profitability',         comp.profitability?.score         || 0, 'ROE ' + fmt(comp.profitability?.roe_pct, 1) + '%   Op margin ' + fmt(comp.profitability?.operating_margin_pct, 1) + '%')}
        ${compRow('Growth',                comp.growth?.score                || 0, 'Revenue ' + fmt(comp.growth?.revenue_growth_pct, 1) + '%   EPS ' + fmt(comp.growth?.earnings_growth_pct, 1) + '%')}
        ${compRow('Leverage',              comp.leverage?.score              || 0, 'D/E ' + fmt(comp.leverage?.debt_to_equity, 2))}
        ${compRow('Cashflow',              comp.cashflow?.score              || 0, comp.cashflow?.free_cashflow > 0 ? 'Free cash flow positive' : 'Free cash flow negative')}
        ${compRow('Promoter pledge',       comp.promoter_pledge?.score       || 0, comp.promoter_pledge?.pledge_pct == null ? 'No data' : fmt(comp.promoter_pledge.pledge_pct, 1) + '%')}
        ${compRow('Institutional holding', comp.institutional?.score         || 0, fmt(comp.institutional?.held_pct_institutions, 1) + '%')}
      </div>

      <!-- Peer rank + score trajectory slot -->
      <div id="edgeAnalyzerPeerRank" style="margin-top:18px;"></div>
      <div id="edgeAnalyzerHistory"  style="margin-top:8px;"></div>

      <div id="edgeAnalyzerExtras"></div>`;

    // Animate the score arc into view after first paint
    requestAnimationFrame(() => {
      const arc = document.querySelector('#edgeAnalyzerBackdrop .ea-arc');
      if (arc) arc.style.strokeDashoffset = dashOffset.toFixed(2);
    });

    // ── New Phase 4.5 sections: AI explainer + peer rank + trajectory ──
    // Fired in parallel so the modal feels snappy.
    (async function loadFundamentalsExtras() {
      const [exJ, prJ, hsJ] = await Promise.all([
        fetch('/api/fundamentals/explain/' + encodeURIComponent(ticker), {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders),
          body: JSON.stringify({ score_payload: payload }),
        }).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch('/api/fundamentals/peer-rank/' + encodeURIComponent(ticker) + '?score=' + (payload.score || 0), { headers: authHeaders }).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch('/api/fundamentals/history/'   + encodeURIComponent(ticker), { headers: authHeaders }).then(r => r.ok ? r.json() : null).catch(() => null),
      ]);

      // AI explainer
      const ex = exJ && exJ.data;
      if (ex && ex.bottom_line) {
        const compMap = ex.components || {};
        const compRows = Object.entries(compMap).map(([k, v]) => `
          <div style="display:grid;grid-template-columns:130px 1fr;gap:10px;padding:6px 0;border-bottom:1px dashed var(--border-subtle);font:500 12.5px 'Inter',sans-serif;">
            <div style="color:var(--text-tertiary);text-transform:capitalize;font-weight:600;">${esc(k.replace(/_/g,' '))}</div>
            <div style="color:var(--text-secondary);line-height:1.55;">${esc(v)}</div>
          </div>`).join('');
        document.getElementById('edgeAnalyzerExplainer').innerHTML = `
          <div style="background:linear-gradient(135deg,var(--accent-dim),var(--info-dim));border:1px solid var(--accent);border-radius:14px;padding:18px 20px;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
              <span class="material-symbols-outlined" style="font-size:18px;color:var(--accent);">auto_awesome</span>
              <div style="font:800 10px 'Inter',sans-serif;letter-spacing:0.16em;text-transform:uppercase;color:var(--accent);">AI explainer · grounded in this score</div>
            </div>
            <div style="font:700 15px 'DM Sans',sans-serif;color:var(--text-primary);line-height:1.5;margin-bottom:12px;">
              ${esc(ex.bottom_line)}
            </div>
            <details style="margin-bottom:12px;">
              <summary style="cursor:pointer;font:600 11px 'Inter',sans-serif;color:var(--text-secondary);letter-spacing:0.04em;text-transform:uppercase;">
                Why each component scored — show
              </summary>
              <div style="margin-top:8px;">${compRows}</div>
            </details>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px;">
              <div style="background:var(--bear-dim);border-left:3px solid var(--bear);border-radius:8px;padding:10px 14px;">
                <div style="font:700 10px 'Inter',sans-serif;letter-spacing:0.12em;text-transform:uppercase;color:var(--bear);margin-bottom:4px;">Risk callout</div>
                <div style="font:500 12.5px 'DM Sans',sans-serif;color:var(--text-primary);line-height:1.5;">${esc(ex.risk_callout || '—')}</div>
              </div>
              <div style="background:var(--bull-dim);border-left:3px solid var(--bull);border-radius:8px;padding:10px 14px;">
                <div style="font:700 10px 'Inter',sans-serif;letter-spacing:0.12em;text-transform:uppercase;color:var(--bull);margin-bottom:4px;">Next step</div>
                <div style="font:500 12.5px 'DM Sans',sans-serif;color:var(--text-primary);line-height:1.5;">${esc(ex.action || '—')}</div>
              </div>
            </div>
            <div style="font:500 9.5px 'Inter',sans-serif;letter-spacing:0.1em;color:var(--text-tertiary);margin-top:10px;text-align:right;">
              ${ex.cached ? 'cached' : 'fresh'} · ${esc(ex.source || 'groq')}
            </div>
          </div>`;
      }

      // Peer rank
      const pr = prJ && prJ.data;
      if (pr && pr.sector) {
        const pct = pr.percentile;
        const pctColor = pct == null ? 'var(--text-tertiary)' : pct >= 75 ? '#2dd4aa' : pct >= 50 ? '#8eb4e0' : pct >= 25 ? '#e6b84a' : '#f26b6b';
        document.getElementById('edgeAnalyzerPeerRank').innerHTML = `
          <div class="ea-section" style="margin-top:0;">
            <div class="ea-sect-h">Peer rank · ${esc(pr.sector)}</div>
            <div style="display:flex;align-items:center;gap:14px;background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:10px;padding:14px 16px;">
              <div style="text-align:center;min-width:90px;">
                <div style="font:800 26px 'Geist Mono',monospace;color:${pctColor};line-height:1;">${pct == null ? '—' : pct + '%'}</div>
                <div style="font:600 9.5px 'Inter',sans-serif;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-tertiary);margin-top:3px;">percentile</div>
              </div>
              <div style="flex:1;font:500 12.5px 'DM Sans',sans-serif;color:var(--text-secondary);line-height:1.55;">
                ${pct == null
                  ? esc(pr.note || 'Need more peer analyses to compute the rank — try analyzing the top sector names.')
                  : `Beats <strong style="color:var(--text-primary);">${pct}%</strong> of ${pr.total} ${esc(pr.sector)} peers · sector avg <strong style="color:var(--text-primary);">${pr.peer_avg}</strong>`}
              </div>
            </div>
          </div>`;
      }

      // Score trajectory (sparkline)
      const hs = (hsJ && hsJ.data) || [];
      if (hs.length >= 2) {
        const W = 320, H = 60, PAD = 4;
        const scores = hs.map(p => p.score);
        const mn = Math.min(...scores, 0), mx = Math.max(...scores, 100);
        const x = (i) => PAD + (W - 2 * PAD) * i / (hs.length - 1);
        const y = (s) => H - PAD - (H - 2 * PAD) * (s - mn) / Math.max(1, mx - mn);
        const path = hs.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)} ${y(p.score).toFixed(1)}`).join(' ');
        const last = hs[hs.length - 1].score, first = hs[0].score, delta = last - first;
        const dColor = delta > 0 ? '#2dd4aa' : delta < 0 ? '#f26b6b' : '#8eb4e0';
        document.getElementById('edgeAnalyzerHistory').innerHTML = `
          <div class="ea-section" style="margin-top:14px;">
            <div class="ea-sect-h">Score trajectory · ${hs.length} snapshots</div>
            <div style="display:flex;align-items:center;gap:14px;background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:10px;padding:14px 16px;">
              <svg width="${W}" height="${H}" style="flex-shrink:0;">
                <path d="${path}" stroke="${dColor}" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" />
                <circle cx="${x(hs.length - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="3" fill="${dColor}" />
              </svg>
              <div style="font:500 12px 'DM Sans',sans-serif;color:var(--text-secondary);">
                <div><strong style="color:${dColor};font-size:14px;">${delta > 0 ? '+' : ''}${delta}</strong> from first snapshot</div>
                <div style="margin-top:2px;color:var(--text-tertiary);font-size:11px;">last: ${last} · first: ${first}</div>
              </div>
            </div>
          </div>`;
      }
    })();

    // Lazy-load extra signals (free for everyone — count gate is on the score endpoint)
    const extras = document.getElementById('edgeAnalyzerExtras');
    const [whJ, inJ, bdJ] = await Promise.all([
      fetch('/api/edge/earnings/whisper/' + encodeURIComponent(ticker),    { headers: authHeaders }).then(r => r.json()).catch(() => null),
      fetch('/api/edge/insider-buys/'    + encodeURIComponent(ticker),     { headers: authHeaders }).then(r => r.json()).catch(() => null),
      fetch('/api/edge/bulk-deal-crossref/' + encodeURIComponent(ticker),  { headers: authHeaders }).then(r => r.json()).catch(() => null),
    ]);
    const blocks = [];
    const wh = whJ && whJ.data;
    if (wh && (wh.history || []).length) {
      const hist = wh.history.slice(0, 4).map(h => {
        const col = h.tag === 'beat' ? '#2dd4aa' : h.tag === 'miss' ? '#f26b6b' : '#8eb4e0';
        return `<span class="ea-quarter-pill" style="background:${col}14;color:${col};border:1px solid ${col}33;">
          <span style="font-weight:700;">${esc((h.period || '').slice(0, 7))}</span>
          <span style="color:${col};opacity:0.85;">${h.tag} ${h.surprise_pct >= 0 ? '+' : ''}${h.surprise_pct}%</span>
        </span>`;
      }).join('');
      blocks.push(`<div class="ea-section">
        <div class="ea-sect-h">Earnings track record</div>
        <div class="ea-extra-block">
          <div>${hist}</div>
          ${wh.streak_label ? `<div style="font:700 11px 'Inter',sans-serif;color:#e6b84a;margin-top:8px;">${esc(wh.streak_label)}</div>` : ''}
        </div>
      </div>`);
    }
    const ins = inJ && inJ.data;
    if (ins && ins.tag) {
      const col = String(ins.tag).startsWith('promoter') ? '#2dd4aa' : '#8eb4e0';
      blocks.push(`<div class="ea-section">
        <div class="ea-sect-h">Insider activity</div>
        <div class="ea-extra-block" style="border-color:${col}33;">
          <div style="font:700 12px 'DM Sans',sans-serif;color:${col};">${esc(ins.label)}</div>
          ${(ins.buys || []).slice(0, 3).map(b =>
            `<div style="font:600 11px 'Inter',sans-serif;color:var(--text-secondary);margin-top:5px;display:flex;gap:8px;align-items:baseline;"><span style="font-family:'Geist Mono',monospace;color:var(--text-tertiary);">${esc(b.date || '')}</span><span style="color:var(--text-primary);">${esc(b.person || '')}</span><span style="color:var(--text-tertiary);font-size:10px;">${esc(b.designation || 'insider')}</span></div>`
          ).join('')}
        </div>
      </div>`);
    }
    const bd = bdJ && bdJ.data;
    if (bd && bd.tag) {
      const col = (bd.tag === 'smart_buy' || bd.tag === 'smart_accumulating') ? '#2dd4aa' :
                  (bd.tag === 'smart_sell') ? '#f26b6b' : '#8eb4e0';
      blocks.push(`<div class="ea-section">
        <div class="ea-sect-h">Bulk / block deals</div>
        <div class="ea-extra-block" style="border-color:${col}33;">
          <div style="font:700 12px 'DM Sans',sans-serif;color:${col};">${esc(bd.label)}</div>
          ${(bd.deals || []).slice(0, 4).map(d =>
            `<div style="font:600 11px 'Inter',sans-serif;color:var(--text-secondary);margin-top:5px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;">
              <span style="font-family:'Geist Mono',monospace;color:var(--text-tertiary);">${esc(d.date || '')}</span>
              <span style="color:var(--text-primary);">${esc((d.client || '').slice(0, 36))}</span>
              <span style="color:${d.side === 'BUY' ? '#2dd4aa' : '#f26b6b'};font-family:'Geist Mono',monospace;">${esc(d.side || '')}</span>
              ${d.value_cr ? `<span style="color:var(--text-tertiary);font-family:'Geist Mono',monospace;">₹${d.value_cr}cr</span>` : ''}
              ${d.smart_money ? '<span style="color:#2dd4aa;font-size:10px;">★ smart money</span>' : ''}
            </div>`
          ).join('')}
        </div>
      </div>`);
    }
    extras.innerHTML = blocks.join('');
  }

  // Global delegated click — any [data-edge-analyze] element opens the modal
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-edge-analyze]');
    if (!t) return;
    const tk = t.dataset.edgeAnalyze;
    if (!tk) return;
    e.preventDefault(); e.stopPropagation();
    // Prefer the neome-style analyzer in index.html if it's wired — it has
    // the narrative bullets + chart + chat input + a "Full page" CTA, and
    // it doesn't call the broken /api/fundamentals/explain|history|peer-rank
    // endpoints that 404/405 from this legacy modal. Falls back to the
    // legacy open() only when the host page hasn't loaded openAnalyzer.
    if (typeof window.openAnalyzer === 'function') {
      window.openAnalyzer(tk);
    } else {
      open(tk);
    }
  }, { capture: true });

  window.EdgeAnalyzer = { open };
})();
