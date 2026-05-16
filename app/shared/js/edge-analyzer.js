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

  function ensureModal() {
    let backdrop = document.getElementById('edgeAnalyzerBackdrop');
    if (backdrop) return backdrop;
    backdrop = document.createElement('div');
    backdrop.id = 'edgeAnalyzerBackdrop';
    backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:9998;display:none;align-items:flex-start;justify-content:center;padding:40px 16px;overflow-y:auto;backdrop-filter:blur(4px);';
    backdrop.innerHTML = `
      <div id="edgeAnalyzerCard" style="background:#0d1117;border:1px solid rgba(141,148,168,0.3);border-radius:16px;max-width:760px;width:100%;padding:0;box-shadow:0 20px 60px rgba(0,0,0,0.5);max-height:90vh;overflow:hidden;display:flex;flex-direction:column;">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid rgba(141,148,168,0.12);">
          <h3 id="edgeAnalyzerTitle" style="font:800 15px 'DM Sans',sans-serif;color:#f4f6fb;letter-spacing:0.02em;">Analyze</h3>
          <button id="edgeAnalyzerClose" style="background:transparent;border:none;color:#8a94a8;font-size:22px;cursor:pointer;line-height:1;">×</button>
        </div>
        <div id="edgeAnalyzerBody" style="padding:20px 22px;overflow-y:auto;flex:1;"></div>
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
    const backdrop = ensureModal();
    const title = document.getElementById('edgeAnalyzerTitle');
    const body  = document.getElementById('edgeAnalyzerBody');
    title.textContent = 'Analyzing ' + ticker + '…';
    body.innerHTML = '<div style="padding:40px;text-align:center;color:#8a94a8;">Computing fundamental score…</div>';
    backdrop.style.display = 'flex';

    let payload = null;
    try {
      const r = await fetch('/api/fundamentals/score/' + encodeURIComponent(ticker));
      const j = await r.json();
      payload = j && j.data;
    } catch (_) {}
    if (!payload || payload.error || payload.score == null) {
      body.innerHTML = '<div style="padding:30px;color:#ff9a9a;">Couldn\'t fetch fundamentals for ' + esc(ticker) +
        (payload && payload.error ? '<br><span style="color:#8a94a8;font-size:11px;">' + esc(payload.error) + '</span>' : '') + '</div>';
      return;
    }

    const TIER_C = { strong: '#2dd4aa', decent: '#8eb4e0', weak: '#e6b84a', avoid: '#f26b6b' };
    const c = TIER_C[payload.tier] || '#8a94a8';
    title.textContent = ticker + ' · ' + (payload.label || '');

    const positives = (payload.positives || []).map(p =>
      `<span style="display:inline-block;font:600 10px 'Inter',sans-serif;padding:3px 8px;border-radius:4px;background:rgba(45,212,170,0.10);color:#7ee0c0;border:1px solid rgba(45,212,170,0.22);margin:2px;">${esc(p)}</span>`).join('');
    const flags = (payload.red_flags || []).map(p =>
      `<span style="display:inline-block;font:600 10px 'Inter',sans-serif;padding:3px 8px;border-radius:4px;background:rgba(242,107,107,0.10);color:#ff9a9a;border:1px solid rgba(242,107,107,0.22);margin:2px;">${esc(p)}</span>`).join('');

    const comp = payload.components || {};
    const compRow = (lbl, score, sub) =>
      `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-radius:8px;background:rgba(13,17,24,0.6);border:1px solid rgba(141,148,168,0.08);margin-bottom:6px;">
         <div><div style="font-weight:700;color:#dde3ef;font-size:12px;">${lbl}</div>
              <div style="font-size:10px;color:#5a6373;">${sub || ''}</div></div>
         <div style="font:800 14px 'Geist Mono',monospace;color:${score > 0 ? '#2dd4aa' : score < 0 ? '#f26b6b' : '#8a94a8'};">${score > 0 ? '+' : ''}${score}</div>
       </div>`;
    const SH = 'font:800 10px Inter,sans-serif;letter-spacing:0.16em;text-transform:uppercase;color:#8a94a8;margin:14px 0 8px 0;';

    body.innerHTML = `
      <div style="display:grid;grid-template-columns:140px 1fr;gap:16px;margin-bottom:16px;">
        <div style="background:${c}1a;border:2px solid ${c}66;border-radius:12px;padding:16px;text-align:center;">
          <div style="font:900 36px 'Geist Mono',monospace;color:${c};">${payload.score}</div>
          <div style="font:800 10px Inter,sans-serif;letter-spacing:0.16em;text-transform:uppercase;color:${c};margin-top:4px;">${esc(payload.tier)}</div>
        </div>
        <div>
          <div style="${SH}">What's working</div>
          <div>${positives || '<span style="color:#5a6373;font-size:11px;">— nothing notable —</span>'}</div>
          <div style="${SH}">Red flags</div>
          <div>${flags || '<span style="color:#5a6373;font-size:11px;">— none flagged —</span>'}</div>
        </div>
      </div>
      <div style="${SH}">Component breakdown</div>
      ${compRow('Valuation', comp.valuation?.score || 0, 'PE ' + fmt(comp.valuation?.pe, 1) + ' · PB ' + fmt(comp.valuation?.pb, 1))}
      ${compRow('Profitability', comp.profitability?.score || 0, 'ROE ' + fmt(comp.profitability?.roe_pct, 1) + '% · Op margin ' + fmt(comp.profitability?.operating_margin_pct, 1) + '%')}
      ${compRow('Growth', comp.growth?.score || 0, 'Revenue ' + fmt(comp.growth?.revenue_growth_pct, 1) + '% · EPS ' + fmt(comp.growth?.earnings_growth_pct, 1) + '%')}
      ${compRow('Leverage', comp.leverage?.score || 0, 'D/E ' + fmt(comp.leverage?.debt_to_equity, 2))}
      ${compRow('Cashflow', comp.cashflow?.score || 0, comp.cashflow?.free_cashflow > 0 ? 'FCF positive' : 'FCF negative')}
      ${compRow('Promoter pledge', comp.promoter_pledge?.score || 0, comp.promoter_pledge?.pledge_pct == null ? 'no data' : fmt(comp.promoter_pledge.pledge_pct, 1) + '%')}
      ${compRow('Institutional holding', comp.institutional?.score || 0, fmt(comp.institutional?.held_pct_institutions, 1) + '%')}
      <div id="edgeAnalyzerExtras" style="margin-top:14px;"></div>`;

    // Lazy-load extra signals
    const extras = document.getElementById('edgeAnalyzerExtras');
    const [whJ, inJ, bdJ] = await Promise.all([
      fetch('/api/edge/earnings/whisper/' + encodeURIComponent(ticker)).then(r => r.json()).catch(() => null),
      fetch('/api/edge/insider-buys/'    + encodeURIComponent(ticker)).then(r => r.json()).catch(() => null),
      fetch('/api/edge/bulk-deal-crossref/' + encodeURIComponent(ticker)).then(r => r.json()).catch(() => null),
    ]);
    const blocks = [];
    const wh = whJ && whJ.data;
    if (wh && (wh.history || []).length) {
      const hist = wh.history.slice(0, 4).map(h => {
        const col = h.tag === 'beat' ? '#2dd4aa' : h.tag === 'miss' ? '#f26b6b' : '#8eb4e0';
        return `<span style="font:600 10px Geist Mono;padding:3px 8px;border-radius:4px;background:${col}1a;color:${col};border:1px solid ${col}44;margin:2px;display:inline-block;">${esc((h.period || '').slice(0, 7))} · ${h.tag} ${h.surprise_pct >= 0 ? '+' : ''}${h.surprise_pct}%</span>`;
      }).join('');
      blocks.push(`<div style="${SH}">Earnings track record</div>
        <div>${hist}${wh.streak_label ? `<div style="font-size:11px;color:#e6b84a;margin-top:4px;">${esc(wh.streak_label)}</div>` : ''}</div>`);
    }
    const ins = inJ && inJ.data;
    if (ins && ins.tag) {
      const col = String(ins.tag).startsWith('promoter') ? '#2dd4aa' : '#8eb4e0';
      blocks.push(`<div style="${SH}">Insider activity</div>
        <div style="background:${col}10;border:1px solid ${col}33;border-radius:8px;padding:10px;">
          <div style="font-weight:700;color:${col};font-size:12px;">${esc(ins.label)}</div>
          ${(ins.buys || []).slice(0, 3).map(b =>
            `<div style="font-size:10px;color:#8a94a8;margin-top:3px;">${esc(b.date || '')} · ${esc(b.person || '')} (${esc(b.designation || 'insider')})</div>`
          ).join('')}
        </div>`);
    }
    const bd = bdJ && bdJ.data;
    if (bd && bd.tag) {
      const col = (bd.tag === 'smart_buy' || bd.tag === 'smart_accumulating') ? '#2dd4aa' :
                  (bd.tag === 'smart_sell') ? '#f26b6b' : '#8eb4e0';
      blocks.push(`<div style="${SH}">Bulk / block deals</div>
        <div style="background:${col}10;border:1px solid ${col}33;border-radius:8px;padding:10px;">
          <div style="font-weight:700;color:${col};font-size:12px;">${esc(bd.label)}</div>
          ${(bd.deals || []).slice(0, 4).map(d =>
            `<div style="font-size:10px;color:#8a94a8;margin-top:3px;">${esc(d.date || '')} · ${esc((d.client || '').slice(0, 40))} · ${esc(d.side || '')} ${d.value_cr ? '₹' + d.value_cr + 'cr' : ''}${d.smart_money ? ' <span style="color:#2dd4aa;">★ smart</span>' : ''}</div>`
          ).join('')}
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
    // Don't fight in-page handlers when there's another modal-owning host
    e.preventDefault(); e.stopPropagation();
    open(tk);
  }, { capture: true });

  window.EdgeAnalyzer = { open };
})();
