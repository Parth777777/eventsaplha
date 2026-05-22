/* stock-pro.js — broker-app-parity injectors for stock.html's existing
 * Financials / Shareholding / News tabs.
 *
 * Instead of being a separate <section>, this script auto-detects the
 * existing tab-pane[data-pane=...] containers in stock.html, prepends a
 * "pro" host into each, and renders broker-style charts + categorized
 * news into them. Legacy widgets remain below — they keep working.
 *
 * Triggers:
 *   - First render on DOMContentLoaded if those panes exist and a ticker
 *     can be resolved from window.TICKER / ?ticker= / data-ticker on the
 *     page wrapper.
 *   - Re-render when the corresponding .tab-btn is clicked (cheap, the
 *     fetch endpoints are cached server-side).
 *
 * Also exposes window.StockPro.renderFinancials / renderShareholding /
 * renderNewsPro so the stock-popup quick-view can reuse the same charts.
 */
(function () {
  'use strict';
  if (window.StockPro) return;

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const fmtINR = (v, d) => v == null || isNaN(v) ? '—'
    : Number(v).toLocaleString('en-IN', { maximumFractionDigits: d == null ? 1 : d });

  function resolveTicker() {
    // Prefer a globally-set TICKER (stock.html sets this), fall back to URL
    if (window.TICKER) return String(window.TICKER).toUpperCase();
    try {
      const p = new URLSearchParams(location.search);
      const t = (p.get('ticker') || p.get('t') || '').toUpperCase();
      if (t) return t;
    } catch (_) {}
    return '';
  }

  // ── Styles (idempotent) ─────────────────────────────────────────
  function ensureStyles() {
    if (document.getElementById('ae-stock-pro-css')) return;
    const s = document.createElement('style');
    s.id = 'ae-stock-pro-css';
    s.textContent = `
      .sp-pro-card { background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: 14px; padding: 18px 20px; margin-bottom: 14px; box-shadow: var(--shadow-sm, 0 1px 3px rgba(0,0,0,.05)); }
      .sp-pro-title { font: 600 11px 'Inter', sans-serif; letter-spacing: 0.16em; text-transform: uppercase; color: var(--text-tertiary); margin: 0 0 12px; }
      .sp-pro-subtab-row { display: flex; gap: 6px; margin: 0 0 16px; flex-wrap: wrap; }
      .sp-pro-subtab { padding: 6px 13px; border-radius: 999px; background: transparent; border: 1px solid var(--border-subtle); color: var(--text-secondary); font: 600 11px 'Inter', sans-serif; cursor: pointer; white-space: nowrap; transition: all 150ms ease; }
      .sp-pro-subtab:hover { color: var(--text-primary); border-color: var(--border-strong); }
      .sp-pro-subtab.active { background: var(--text-primary); color: var(--surface-1); border-color: var(--text-primary); }
      .sp-pro-bartitle { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
      .sp-pro-bartitle-dot { width: 8px; height: 8px; border-radius: 50%; }
      .sp-pro-bartitle-lbl { font: 600 12.5px 'Inter', sans-serif; color: var(--text-primary); }
      .sp-pro-bar-svg { width: 100%; height: auto; max-height: 260px; }
      .sp-pro-table { width: 100%; border-collapse: collapse; font: 500 13px 'Inter', sans-serif; }
      .sp-pro-table th, .sp-pro-table td { padding: 10px 12px; text-align: right; border-bottom: 1px solid var(--border-subtle); }
      .sp-pro-table th:first-child, .sp-pro-table td:first-child { text-align: left; color: var(--text-secondary); }
      .sp-pro-table thead th { font: 700 10.5px 'Inter', sans-serif; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-tertiary); background: var(--surface-2); }
      .sp-pro-table tbody td { font-family: 'Geist Mono', monospace; color: var(--text-primary); }
      .sp-pro-chip { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border-radius: 999px; font: 700 10.5px 'Inter', sans-serif; letter-spacing: 0.04em; }
      .sp-pro-chip.results,.sp-pro-chip.board,.sp-pro-chip.transfer,.sp-pro-chip.spv_formation,.sp-pro-chip.fundraising,.sp-pro-chip.capex { background: rgba(107,163,214,0.14); color: #4f6ddc; border: 1px solid rgba(107,163,214,0.4); }
      .sp-pro-chip.m_and_a { background: rgba(143,107,214,0.14); color: #6f4cd6; border: 1px solid rgba(143,107,214,0.4); }
      .sp-pro-chip.contract,.sp-pro-chip.dividend,.sp-pro-chip.buyback { background: rgba(45,212,170,0.14); color: #0d9669; border: 1px solid rgba(45,212,170,0.4); }
      .sp-pro-chip.exec_change,.sp-pro-chip.rating,.sp-pro-chip.agm { background: rgba(230,184,74,0.14); color: #b88c2c; border: 1px solid rgba(230,184,74,0.4); }
      .sp-pro-chip.legal { background: rgba(242,107,107,0.14); color: #c83838; border: 1px solid rgba(242,107,107,0.4); }
      .sp-pro-chip.other { background: rgba(141,148,168,0.14); color: var(--text-tertiary); border: 1px solid var(--border-subtle); }
      .sp-pro-news-item { padding: 14px 0; border-bottom: 1px solid var(--border-subtle); }
      .sp-pro-news-item:last-child { border-bottom: 0; }
      .sp-pro-news-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 6px; }
      .sp-pro-news-date { font: 600 11px 'Inter', sans-serif; color: var(--link, var(--accent, #4f46e5)); }
      .sp-pro-news-title { font: 600 14px 'DM Sans', sans-serif; color: var(--text-primary); line-height: 1.45; margin-bottom: 4px; }
      .sp-pro-news-summary { font: 500 12.5px 'DM Sans', sans-serif; color: var(--text-secondary); line-height: 1.55; }
      .sp-pro-news-meta { display: flex; gap: 12px; margin-top: 6px; font: 500 11px 'Inter', sans-serif; color: var(--text-tertiary); }
      .sp-pro-trend-callout { background: rgba(45,212,170,0.10); border: 1px solid rgba(45,212,170,0.25); border-radius: 10px; padding: 14px 16px; color: var(--bull, #0d9669); font: 500 13px 'DM Sans', sans-serif; margin: 12px 0; }
      .sp-pro-trend-callout.bear { background: rgba(242,107,107,0.10); border-color: rgba(242,107,107,0.25); color: var(--bear, #c83838); }
    `;
    document.head.appendChild(s);
  }

  // ── Pure-SVG bar chart ──────────────────────────────────────────
  function barChart(data, opts) {
    opts = opts || {};
    if (!data || !data.length) return '<div style="color:var(--text-tertiary);font-size:12px;padding:24px 0;text-align:center;">No data available</div>';
    const W = 720, H = 220, PADX = 40, PADY = 28;
    const color = opts.color || '#4f6ddc';
    const values = data.map((d) => Number(d.value) || 0);
    const max = Math.max(...values, 0);
    const min = Math.min(...values, 0);
    const range = max - min || 1;
    const slot = (W - 2 * PADX) / data.length;
    const barW = slot * 0.55;
    const zeroY = H - PADY - (-min / range) * (H - 2 * PADY);
    const fmt = opts.fmt || ((v) => fmtINR(v));

    const bars = data.map((d, i) => {
      const x = PADX + slot * i + (slot - barW) / 2;
      const v = Number(d.value) || 0;
      const yTop = zeroY - (v / range) * (H - 2 * PADY);
      const isNeg = v < 0;
      const h = Math.max(1, Math.abs(yTop - zeroY));
      const y = isNeg ? zeroY : yTop;
      return `
        <rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${color}" rx="3"></rect>
        <text x="${(x + barW / 2).toFixed(1)}" y="${(isNeg ? y + h + 16 : y - 6).toFixed(1)}" text-anchor="middle" style="font:600 11px 'Geist Mono',monospace;fill:var(--text-primary);">${esc(fmt(v))}</text>
        <text x="${(x + barW / 2).toFixed(1)}" y="${(H - 6).toFixed(1)}" text-anchor="middle" style="font:500 10px 'Inter',sans-serif;fill:var(--text-tertiary);">${esc(d.label)}</text>
      `;
    }).join('');
    return `<svg class="sp-pro-bar-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
        <line x1="${PADX}" y1="${zeroY.toFixed(1)}" x2="${W - PADX}" y2="${zeroY.toFixed(1)}" stroke="var(--border-subtle)" stroke-width="1"/>
        ${bars}
      </svg>`;
  }

  // ── Donut chart for shareholding ────────────────────────────────
  function donutChart(slices) {
    const total = slices.reduce((s, x) => s + (Number(x.value) || 0), 0) || 1;
    const R = 65, r = 38, cx = 90, cy = 90;
    let acc = 0;
    function arcPath(start, end) {
      const a1 = (start / total) * Math.PI * 2 - Math.PI / 2;
      const a2 = (end / total) * Math.PI * 2 - Math.PI / 2;
      const large = (end - start) / total > 0.5 ? 1 : 0;
      const x1 = cx + R * Math.cos(a1), y1 = cy + R * Math.sin(a1);
      const x2 = cx + R * Math.cos(a2), y2 = cy + R * Math.sin(a2);
      const x3 = cx + r * Math.cos(a2), y3 = cy + r * Math.sin(a2);
      const x4 = cx + r * Math.cos(a1), y4 = cy + r * Math.sin(a1);
      return `M${x1.toFixed(1)} ${y1.toFixed(1)} A${R} ${R} 0 ${large} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} L${x3.toFixed(1)} ${y3.toFixed(1)} A${r} ${r} 0 ${large} 0 ${x4.toFixed(1)} ${y4.toFixed(1)} Z`;
    }
    const arcs = slices.map((s) => {
      const v = Number(s.value) || 0;
      const path = arcPath(acc, acc + v);
      acc += v;
      return `<path d="${path}" fill="${s.color}" />`;
    }).join('');
    const legend = slices.map((s) => `
      <div style="display:flex;align-items:center;gap:8px;font:500 12.5px 'Inter',sans-serif;color:var(--text-secondary);margin:5px 0;">
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${s.color};"></span>
        <span style="color:var(--text-primary);min-width:90px;">${esc(s.label)}</span>
        <span style="font-family:'Geist Mono',monospace;font-weight:700;color:var(--text-primary);">${fmtINR(s.value, 1)}%</span>
      </div>`).join('');
    return `<div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap;"><svg width="180" height="180" viewBox="0 0 180 180">${arcs}</svg><div>${legend}</div></div>`;
  }

  // ── Financials renderer ─────────────────────────────────────────
  async function renderFinancials(container, ticker, opts) {
    opts = opts || {};
    const statement = opts.statement || 'standalone';
    const period = opts.period || 'annual';
    container.innerHTML = '<div class="sp-pro-card"><div style="padding:24px;text-align:center;color:var(--text-secondary);font-size:13px;">Loading financials…</div></div>';
    let payload = null;
    try {
      const url = `/api/stock/${encodeURIComponent(ticker)}/financials?period=${period}&statement=${statement}`;
      const r = await fetch(url);
      const j = await r.json();
      payload = j && j.data;
    } catch (_) {}

    // The existing /api/stock/<t>/financials returns 4 statement arrays:
    //   income_statement, balance_sheet, cash_flow, income_statement_q
    // Each is an array of { period, ...metric_keys }. We pull from
    // income_statement (or income_statement_q if quarterly requested).
    const stmts = payload || {};
    const incomeArr = (period === 'quarterly' && stmts.income_statement_q && stmts.income_statement_q.length)
        ? stmts.income_statement_q
        : (stmts.income_statement || []);

    if (!incomeArr.length) {
      container.innerHTML = `<div class="sp-pro-card" style="text-align:center;color:var(--text-tertiary);font-size:13px;padding:30px;">No financials data available for ${esc(ticker)}.</div>`;
      return;
    }

    // Period order: ASC for the bar charts (left=oldest), use as-is for the table headers.
    const chrono = [...incomeArr].reverse();
    function pickKey(row, keys) {
      for (const k of keys) {
        if (typeof row[k] === 'number') return row[k];
      }
      for (const k of keys) {
        const found = Object.keys(row).find(rk => rk.toLowerCase().replace(/[\s_]+/g, '') === k.toLowerCase().replace(/[\s_]+/g, ''));
        if (found && typeof row[found] === 'number') return row[found];
      }
      return null;
    }
    function toBars(keys) {
      return chrono.map((r) => ({
        label: String(r.period || '').slice(0, 7),
        value: pickKey(r, keys),
      })).filter((x) => x.value != null);
    }
    const revenue   = toBars(['Total Revenue', 'Revenue', 'Total Income', 'totalRevenue']);
    const opProfit  = toBars(['Operating Income', 'Operating Profit', 'EBIT', 'operatingIncome']);
    const netProfit = toBars(['Net Income', 'Net Profit', 'Profit After Tax', 'netIncome', 'PAT']);

    function tableCols() {
      return chrono.map((r) => String(r.period || '').slice(0, 8));
    }
    function tableRow(label, keys) {
      const cells = chrono.map((r) => {
        const v = pickKey(r, keys);
        return `<td>${v == null ? '—' : fmtINR(v)}</td>`;
      }).join('');
      return `<tr><td>${esc(label)}</td>${cells}</tr>`;
    }

    container.innerHTML = `
      <div class="sp-pro-card">
        <div class="sp-pro-title">Annual P&amp;L · ${esc(ticker)}</div>
        <div class="sp-pro-subtab-row">
          <button class="sp-pro-subtab ${statement === 'standalone' ? 'active' : ''}" data-stmt="standalone">Standalone</button>
          <button class="sp-pro-subtab ${statement === 'consolidated' ? 'active' : ''}" data-stmt="consolidated">Consolidated</button>
          <span style="flex:1;"></span>
          <button class="sp-pro-subtab ${period === 'annual' ? 'active' : ''}" data-period="annual">Annual</button>
          <button class="sp-pro-subtab ${period === 'quarterly' ? 'active' : ''}" data-period="quarterly">Quarterly</button>
        </div>

        ${revenue.length ? `
          <div style="margin-bottom:18px;">
            <div class="sp-pro-bartitle"><span class="sp-pro-bartitle-dot" style="background:#4f6ddc;"></span><span class="sp-pro-bartitle-lbl">Revenue ${period === 'annual' ? 'Annual' : 'Quarterly'}</span></div>
            ${barChart(revenue, { color: '#4f6ddc' })}
          </div>` : ''}

        ${opProfit.length ? `
          <div style="margin-bottom:18px;">
            <div class="sp-pro-bartitle"><span class="sp-pro-bartitle-dot" style="background:#f59e0b;"></span><span class="sp-pro-bartitle-lbl">Operating Profit ${period === 'annual' ? 'Annual' : 'Quarterly'}</span></div>
            ${barChart(opProfit, { color: '#f59e0b' })}
          </div>` : ''}

        ${netProfit.length ? `
          <div style="margin-bottom:18px;">
            <div class="sp-pro-bartitle"><span class="sp-pro-bartitle-dot" style="background:#e91e63;"></span><span class="sp-pro-bartitle-lbl">Net Profit ${period === 'annual' ? 'Annual' : 'Quarterly'}</span></div>
            ${barChart(netProfit, { color: '#e91e63' })}
          </div>` : ''}

        <div style="overflow-x:auto;margin-top:14px;border:1px solid var(--border-subtle);border-radius:10px;">
          <table class="sp-pro-table">
            <thead><tr><th>Indicator</th>${tableCols().map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
            <tbody>
              ${tableRow('Total Revenue',     ['Total Revenue', 'Revenue', 'Total Income', 'totalRevenue'])}
              ${tableRow('Operating Income',  ['Operating Income', 'Operating Profit', 'EBIT', 'operatingIncome'])}
              ${tableRow('Gross Profit',      ['Gross Profit', 'grossProfit'])}
              ${tableRow('Interest',          ['Interest Expense', 'Finance Costs', 'interestExpense'])}
              ${tableRow('Pretax Income',     ['Pretax Income', 'Profit Before Tax', 'pretaxIncome', 'PBT'])}
              ${tableRow('Net Income',        ['Net Income', 'Net Profit', 'Profit After Tax', 'netIncome', 'PAT'])}
            </tbody>
          </table>
        </div>
      </div>
    `;
    container.querySelectorAll('[data-stmt]').forEach((b) => b.addEventListener('click', () => renderFinancials(container, ticker, { statement: b.dataset.stmt, period })));
    container.querySelectorAll('[data-period]').forEach((b) => b.addEventListener('click', () => renderFinancials(container, ticker, { statement, period: b.dataset.period })));
  }

  // ── Shareholding renderer ──────────────────────────────────────
  async function renderShareholding(container, ticker) {
    container.innerHTML = '<div class="sp-pro-card"><div style="padding:24px;text-align:center;color:var(--text-secondary);font-size:13px;">Loading shareholding…</div></div>';
    let d = null;
    try {
      const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/shareholding`);
      const j = await r.json();
      d = j && j.data;
    } catch (_) {}
    if (!d) {
      container.innerHTML = `<div class="sp-pro-card" style="text-align:center;color:var(--text-tertiary);font-size:13px;padding:30px;">No shareholding data available for ${esc(ticker)}.</div>`;
      return;
    }

    // Adapt to whatever shape the backend returns. Common shapes:
    //   d.latest: { promoter_pct, fii_pct, mf_pct, public_pct }
    //   d.major: [ { label, pct } ]   ← yfinance-style summary
    //   d.history: [{ quarter, fii_pct, mf_pct }]
    //   d.institutional: [...], d.mutual_funds: [...]
    const latest = d.latest || {};
    let slices = [
      { label: 'Promoter', value: latest.promoter_pct, color: '#2dd4aa' },
      { label: 'FII',      value: latest.fii_pct,      color: '#e91e63' },
      { label: 'MF',       value: latest.mf_pct,       color: '#4f6ddc' },
      { label: 'Public',   value: latest.public_pct,   color: '#f59e0b' },
    ].filter((s) => s.value != null);

    // Fallback to yfinance major[] (Insiders / Institutions / Public)
    if (!slices.length && Array.isArray(d.major)) {
      const lookup = {};
      d.major.forEach((m) => { lookup[(m.label || '').toLowerCase()] = parseFloat(String(m.pct || '').replace('%', '')); });
      const ins = lookup['insiders'] || lookup['promoter'] || 0;
      const inst = lookup['institutions'] || lookup['fii'] || 0;
      const fl = lookup['public/other'] || lookup['public'] || Math.max(0, 100 - ins - inst);
      slices = [
        { label: 'Promoter/Insiders', value: ins,  color: '#2dd4aa' },
        { label: 'Institutions',      value: inst, color: '#4f6ddc' },
        { label: 'Public/Other',      value: fl,   color: '#f59e0b' },
      ].filter((s) => s.value);
    }

    const history = Array.isArray(d.history) ? d.history.slice(-6) : [];
    const fiiBars = history.filter((h) => h.fii_pct != null).map((h) => ({ label: String(h.quarter || '').slice(-5), value: h.fii_pct }));
    const mfBars  = history.filter((h) => h.mf_pct  != null).map((h) => ({ label: String(h.quarter || '').slice(-5), value: h.mf_pct  }));

    let trend = '';
    if (fiiBars.length >= 2) {
      const f = fiiBars[0].value, l = fiiBars[fiiBars.length - 1].value;
      const diff = (l - f).toFixed(2);
      if (diff > 0) trend = `<div class="sp-pro-trend-callout">FII/FPI have <strong>increased</strong> holdings from ${fmtINR(f)}% to ${fmtINR(l)}% (Δ +${diff}%).</div>`;
      else if (diff < 0) trend = `<div class="sp-pro-trend-callout bear">FII/FPI have <strong>reduced</strong> holdings from ${fmtINR(f)}% to ${fmtINR(l)}% (Δ ${diff}%).</div>`;
    }

    const holders = d.top_holders || d.holders || d.institutional || [];

    container.innerHTML = `
      <div class="sp-pro-card">
        <div class="sp-pro-title">Shareholding · ${esc(ticker)}</div>
        ${slices.length ? donutChart(slices) : `<div style="color:var(--text-tertiary);font-size:12px;padding:14px 0;">Latest breakdown not available.</div>`}

        ${fiiBars.length ? `
          <div style="margin-top:24px;">
            <div class="sp-pro-bartitle"><span class="sp-pro-bartitle-dot" style="background:#e91e63;"></span><span class="sp-pro-bartitle-lbl">Historical FII Holding</span></div>
            ${barChart(fiiBars, { color: '#e91e63', fmt: (v) => fmtINR(v) + '%' })}
          </div>` : ''}

        ${trend}

        ${mfBars.length ? `
          <div style="margin-top:8px;">
            <div class="sp-pro-bartitle"><span class="sp-pro-bartitle-dot" style="background:#f59e0b;"></span><span class="sp-pro-bartitle-lbl">Historical MF Holding</span></div>
            ${barChart(mfBars, { color: '#f59e0b', fmt: (v) => fmtINR(v) + '%' })}
          </div>` : ''}

        ${holders.length ? `
          <div style="margin-top:24px;overflow-x:auto;border:1px solid var(--border-subtle);border-radius:10px;">
            <table class="sp-pro-table">
              <thead><tr><th>Name</th><th>Category</th><th>Shares</th><th>%</th></tr></thead>
              <tbody>
                ${holders.slice(0, 15).map((h) => `
                  <tr>
                    <td style="font-weight:600;color:var(--text-primary);">${esc(h.name || h.holder || '')}</td>
                    <td style="text-align:right;color:var(--text-tertiary);font-size:11px;letter-spacing:0.04em;text-transform:uppercase;">${esc(h.category || h.type || '')}</td>
                    <td>${h.shares == null ? '—' : fmtINR(h.shares, 0)}</td>
                    <td style="font-weight:700;">${h.pct == null ? '—' : fmtINR(h.pct) + '%'}</td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>` : ''}
      </div>
    `;
  }

  // ── Wire-style News renderer ───────────────────────────────────
  async function renderNewsPro(container, ticker, only) {
    container.innerHTML = '<div class="sp-pro-card"><div style="padding:24px;text-align:center;color:var(--text-secondary);font-size:13px;">Loading corporate news…</div></div>';
    let j = null;
    try {
      const url = `/api/stock/${encodeURIComponent(ticker)}/news-pro?limit=80${only ? '&only=' + encodeURIComponent(only) : ''}`;
      const r = await fetch(url);
      j = await r.json();
    } catch (_) {}
    if (!j || !j.success) {
      container.innerHTML = `<div class="sp-pro-card" style="text-align:center;color:var(--text-tertiary);font-size:13px;padding:30px;">Corporate news endpoint not available yet.</div>`;
      return;
    }
    const items = j.data || [];
    const counts = j.counts || {};

    const CHIP_DEFS = [
      ['', 'All', items.length],
      ['results', 'Results', counts.results || 0],
      ['m_and_a', 'M&A', counts.m_and_a || 0],
      ['contract', 'Contracts', counts.contract || 0],
      ['transfer', 'Transfers', counts.transfer || 0],
      ['spv_formation', 'SPV', counts.spv_formation || 0],
      ['exec_change', 'Executive', counts.exec_change || 0],
      ['dividend', 'Dividend', counts.dividend || 0],
      ['fundraising', 'Fundraising', counts.fundraising || 0],
      ['buyback', 'Buyback', counts.buyback || 0],
      ['capex', 'Capex', counts.capex || 0],
      ['rating', 'Rating', counts.rating || 0],
      ['legal', 'Legal', counts.legal || 0],
      ['board', 'Board', counts.board || 0],
    ].filter(([slug, lbl, n]) => slug === '' || n > 0);

    const chips = CHIP_DEFS.map(([slug, lbl, n]) =>
      `<button class="sp-pro-subtab ${(only || '') === slug ? 'active' : ''}" data-only="${esc(slug)}">${esc(lbl)}${n ? ` <span style="opacity:0.7;font-weight:500;">${n}</span>` : ''}</button>`
    ).join('');

    function fmtDate(s) {
      if (!s) return '';
      try {
        const d = new Date(s);
        if (isNaN(d)) return s;
        return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase();
      } catch (_) { return s; }
    }

    const rows = items.length ? items.map((it) => {
      const cat = it.category || { slug: 'other', label: 'Update' };
      return `
        <div class="sp-pro-news-item">
          <div class="sp-pro-news-head">
            <span class="sp-pro-news-date">${fmtDate(it.published_at)}</span>
            <span class="sp-pro-chip ${esc(cat.slug)}">${esc(cat.label)}</span>
            ${it.sentiment ? `<span style="font:600 10px 'Inter',sans-serif;color:${it.sentiment === 'bullish' ? '#0d9669' : it.sentiment === 'bearish' ? '#c83838' : 'var(--text-tertiary)'};text-transform:uppercase;letter-spacing:0.08em;">● ${esc(it.sentiment)}</span>` : ''}
            ${it.alpha_score != null ? `<span style="font:700 10.5px 'Geist Mono',monospace;background:var(--accent-dim, rgba(74,92,255,.1));color:var(--accent, #4a5cff);padding:2px 7px;border-radius:6px;">α ${Math.round(it.alpha_score)}</span>` : ''}
          </div>
          <div class="sp-pro-news-title">${it.link ? `<a href="${esc(it.link)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none;">${esc(it.title)}</a>` : esc(it.title)}</div>
          ${it.summary ? `<div class="sp-pro-news-summary">${esc(it.summary).slice(0, 320)}${it.summary.length > 320 ? '…' : ''}</div>` : ''}
          <div class="sp-pro-news-meta">
            ${it.source ? `<span>${esc(it.source)}</span>` : ''}
            ${it.event_type ? `<span style="text-transform:capitalize;">${esc(it.event_type.replace(/_/g, ' '))}</span>` : ''}
          </div>
        </div>`;
    }).join('') : `<div style="padding:30px;color:var(--text-tertiary);font-size:13px;text-align:center;">No matching corporate events found.</div>`;

    container.innerHTML = `
      <div class="sp-pro-card">
        <div class="sp-pro-title">Wire-style corporate news · ${esc(ticker)}</div>
        <div class="sp-pro-subtab-row">${chips}</div>
        <div>${rows}</div>
      </div>
    `;
    container.querySelectorAll('[data-only]').forEach((b) =>
      b.addEventListener('click', () => renderNewsPro(container, ticker, b.dataset.only || null))
    );
  }

  // ── Inject hosts into the existing tab-panes on stock.html ──────
  function ensureHost(pane, hostId) {
    let host = pane.querySelector('#' + hostId);
    if (!host) {
      host = document.createElement('div');
      host.id = hostId;
      // Prepend so the new content shows ABOVE legacy widgets
      pane.insertBefore(host, pane.firstChild);
    }
    return host;
  }

  function bootStockPage() {
    ensureStyles();
    const ticker = resolveTicker();
    if (!ticker) return;

    const finPane   = document.querySelector('.tab-pane[data-pane="financials"]');
    const sharePane = document.querySelector('.tab-pane[data-pane="shareholding"]');
    const newsPane  = document.querySelector('.tab-pane[data-pane="news"]');

    // Trigger first render — financials always since it's the primary draw.
    if (finPane) {
      const host = ensureHost(finPane, 'sp-pro-fin-host');
      renderFinancials(host, ticker);
    }

    // Lazy-load shareholding + news when their tab buttons are clicked.
    const loaded = { share: false, news: false };
    const finBtn   = document.querySelector('.tab-btn[data-tab="financials"]');
    const shareBtn = document.querySelector('.tab-btn[data-tab="shareholding"]');
    const newsBtn  = document.querySelector('.tab-btn[data-tab="news"]');

    if (shareBtn && sharePane) {
      shareBtn.addEventListener('click', () => {
        if (loaded.share) return;
        loaded.share = true;
        const host = ensureHost(sharePane, 'sp-pro-share-host');
        renderShareholding(host, ticker);
      });
    }
    if (newsBtn && newsPane) {
      newsBtn.addEventListener('click', () => {
        if (loaded.news) return;
        loaded.news = true;
        const host = ensureHost(newsPane, 'sp-pro-news-host');
        renderNewsPro(host, ticker);
      });
    }
    // If the page loads directly on a non-financials tab (URL hash), eager-mount
    setTimeout(() => {
      if (!loaded.share && sharePane && sharePane.classList.contains('active')) {
        loaded.share = true;
        renderShareholding(ensureHost(sharePane, 'sp-pro-share-host'), ticker);
      }
      if (!loaded.news && newsPane && newsPane.classList.contains('active')) {
        loaded.news = true;
        renderNewsPro(ensureHost(newsPane, 'sp-pro-news-host'), ticker);
      }
    }, 800);
  }

  // ── Public API for stock-popup.js + dedicated hosts ────────────
  window.StockPro = {
    renderFinancials,
    renderShareholding,
    renderNewsPro,
    barChart,
    donutChart,
    boot: bootStockPage,
  };

  // ── Auto-boot ───────────────────────────────────────────────────
  // On stock.html — auto-inject into existing tab-panes.
  // Anywhere with <section id="ae-stock-pro" data-ticker="X"> — auto-mount as standalone.
  function init() {
    ensureStyles();
    // Existing stock.html with tab-pane structure
    if (document.querySelector('.tab-pane[data-pane="financials"]')) {
      bootStockPage();
    }
    // Standalone host (kept for future pages that want to embed pro charts)
    document.querySelectorAll('#ae-stock-pro, [data-ae-stock-pro]').forEach((host) => {
      const ticker = (host.dataset.ticker || resolveTicker() || '').toUpperCase();
      if (!ticker) return;
      const cardId = 'sp-pro-' + Math.random().toString(36).slice(2, 8);
      host.innerHTML = `<div id="${cardId}-fin"></div><div id="${cardId}-share"></div><div id="${cardId}-news"></div>`;
      renderFinancials(host.querySelector('#' + cardId + '-fin'), ticker);
      renderShareholding(host.querySelector('#' + cardId + '-share'), ticker);
      renderNewsPro(host.querySelector('#' + cardId + '-news'), ticker);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
