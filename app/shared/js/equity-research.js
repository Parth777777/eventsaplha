/* equity-research.js — inline equity research panel.
 *
 * Renders into a page-provided host:
 *   <section id="ae-research-host" data-ticker="RELIANCE"></section>
 *
 * Bootstrap.js conditionally loads this file only when that host exists,
 * so the script + its CSS aren't paid on pages that don't use it.
 *
 * Tier behavior:
 *   - free / anon — sections 1, 2, 8 rendered; 3,4,5,6,7 show "locked"
 *     placeholders with upgrade CTA. Thesis preview (1 sentence) shown.
 *   - starter / pro — all 8 sections rendered, no locked tiles.
 */
(function () {
  'use strict';
  if (window.__aeResearchMounted) return;
  window.__aeResearchMounted = true;

  function getToken() {
    return localStorage.getItem('tw_token') || localStorage.getItem('auth_token') || '';
  }
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }
  function fmt(v) {
    if (v == null || v === '') return '—';
    if (typeof v === 'number') return v.toLocaleString('en-IN', { maximumFractionDigits: 2 });
    return String(v);
  }

  function sectionHtml(label, body, opts = {}) {
    const locked = opts.locked || false;
    return `
      <div class="ae-research-section ${locked ? 'is-locked' : ''}"
           style="background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:12px;padding:16px 18px;margin-bottom:12px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
          <div style="font-weight:700;font-size:13px;letter-spacing:-0.005em;color:var(--text-primary);">${esc(label)}</div>
          ${locked
            ? `<a href="pricing.html" style="font-size:11px;color:var(--link);text-decoration:none;font-weight:600;border:1px solid var(--link);padding:3px 9px;border-radius:999px;">Upgrade to unlock</a>`
            : ''}
        </div>
        <div class="ae-research-body" style="${locked ? 'filter:blur(4px);user-select:none;pointer-events:none;' : ''}">
          ${body}
        </div>
      </div>
    `;
  }

  function renderSnapshot(s) {
    if (!s) return '<div style="color:var(--text-tertiary);">No snapshot data.</div>';
    return `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:14px;">
        <div><div style="font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;">Sector</div>
             <div style="font-weight:600;color:var(--text-primary);margin-top:2px;">${esc(s.sector || '—')}</div></div>
        <div><div style="font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;">Company</div>
             <div style="font-weight:600;color:var(--text-primary);margin-top:2px;">${esc(s.company || s.ticker)}</div></div>
        <div><div style="font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;">Market cap (cr)</div>
             <div style="font-family:'Geist Mono',monospace;font-weight:700;color:var(--text-primary);margin-top:2px;">${fmt(s.mcap_cr)}</div></div>
        <div><div style="font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;">Last close</div>
             <div style="font-family:'Geist Mono',monospace;font-weight:700;color:var(--text-primary);margin-top:2px;">₹${fmt(s.last_close)}</div></div>
      </div>
    `;
  }

  function renderCatalysts(rows) {
    if (!rows || !rows.length) return '<div style="color:var(--text-tertiary);">No catalysts in the last 90 days.</div>';
    return rows.slice(0, 8).map((c) => {
      const sent = (c.sentiment || 'neutral').toLowerCase();
      const sentColor = sent === 'bullish' ? 'var(--bull)' : (sent === 'bearish' ? 'var(--bear)' : 'var(--text-secondary)');
      return `
        <div style="display:flex;gap:12px;align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--border-subtle);">
          <div style="font-family:'Geist Mono',monospace;font-weight:700;font-size:13px;color:${sentColor};min-width:46px;">${fmt(Math.round(c.alpha_score || 0))}</div>
          <div style="flex:1;font-size:13px;color:var(--text-secondary);line-height:1.45;">
            <div style="color:var(--text-primary);font-weight:500;">${esc(c.headline || '').slice(0, 180)}</div>
            <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">${esc(c.event_type || 'event')} · ${esc(c.sentiment || 'neutral')}</div>
          </div>
        </div>
      `;
    }).join('');
  }

  function renderFinancials(f) {
    if (!f || !Object.keys(f).length) return '<div style="color:var(--text-tertiary);">Financials data not yet ingested for this ticker.</div>';
    const labels = {
      revenue_cr: 'Revenue (cr)', pat_cr: 'PAT (cr)',
      op_margin_pct: 'Op margin %', roce_pct: 'ROCE %',
      debt_to_equity: 'D/E', pe: 'P/E', pb: 'P/B',
    };
    const cells = Object.entries(f).filter(([k, v]) => v != null).map(([k, v]) => `
      <div><div style="font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;">${esc(labels[k] || k)}</div>
           <div style="font-family:'Geist Mono',monospace;font-weight:700;color:var(--text-primary);margin-top:2px;">${fmt(v)}</div></div>
    `).join('');
    return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;">${cells}</div>`;
  }

  function renderForensic(rows) {
    if (!rows || !rows.length) return '<div style="color:var(--bull);font-weight:500;">✓ No forensic flags on record.</div>';
    return rows.map((f) => `
      <div style="padding:8px 0;border-bottom:1px solid var(--border-subtle);">
        <div style="font-weight:600;color:var(--bear);font-size:13px;">⚠ ${esc(f.flag_type || f.event_type || f.pattern || 'flag')}</div>
        <div style="font-size:12px;color:var(--text-secondary);margin-top:2px;">${esc(f.reasoning || f.detail || '')}</div>
        <div style="font-size:10px;color:var(--text-tertiary);margin-top:2px;">${esc(f.source_table || '')}</div>
      </div>
    `).join('');
  }

  function renderPeers(rows) {
    if (!rows || !rows.length) return '<div style="color:var(--text-tertiary);">No peers identified.</div>';
    return `<div style="display:flex;flex-wrap:wrap;gap:8px;">${
      rows.map((p) => `<a href="stock.html?ticker=${encodeURIComponent(p.ticker)}"
        style="padding:6px 12px;background:var(--surface-3);color:var(--text-primary);border:1px solid var(--border-subtle);border-radius:999px;text-decoration:none;font-size:12px;font-weight:600;font-family:'Geist Mono',monospace;">
        ${esc(p.ticker)}</a>`).join('')
    }</div>`;
  }

  function renderProse(text) {
    if (!text) return '<div style="color:var(--text-tertiary);">—</div>';
    return `<div style="font-size:14px;line-height:1.6;color:var(--text-primary);">${esc(text)}</div>`;
  }

  function renderMethodology(m) {
    if (!m) return '';
    return `
      <div style="font-size:12px;color:var(--text-secondary);line-height:1.5;">
        ${esc(m.disclaimer || '')}
        <br><a href="${esc(m.url || 'methodology.html')}" style="color:var(--link);font-weight:600;text-decoration:underline;text-underline-offset:3px;">Read full methodology →</a>
      </div>
    `;
  }

  function renderReport(host, report) {
    const s = report.sections || {};
    const tier = report.tier_view || 'free';
    const locked = new Set(report.locked_sections || []);
    const isLocked = (key) => locked.has(key);

    const upgradeBanner = (tier === 'free' || tier === 'anon')
      ? `<div style="display:flex;justify-content:space-between;align-items:center;background:var(--accent-dim);border:1px solid var(--accent);border-radius:12px;padding:14px 18px;margin-bottom:16px;flex-wrap:wrap;gap:10px;">
           <div>
             <div style="font-weight:700;color:var(--text-primary);font-size:14px;">You're seeing the free preview</div>
             <div style="font-size:12px;color:var(--text-secondary);margin-top:2px;">Unlock financials, forensic deep-dive, peer comparison, AI thesis and risk factors — from ₹99/mo.</div>
           </div>
           <a href="pricing.html" style="background:var(--accent);color:var(--surface-1);padding:9px 16px;border-radius:8px;text-decoration:none;font-weight:600;font-size:13px;white-space:nowrap;">See plans →</a>
         </div>`
      : '';

    const thesisBody = isLocked('thesis')
      ? `<div style="font-size:14px;line-height:1.6;color:var(--text-primary);">${esc(s.thesis_preview || 'AI-written investment thesis available with Starter or Pro.')}</div>`
      : renderProse(s.thesis);

    host.innerHTML = `
      ${upgradeBanner}
      ${sectionHtml('Company snapshot', renderSnapshot(s.snapshot))}
      ${sectionHtml('Recent catalysts · 90 days', renderCatalysts(s.catalysts))}
      ${sectionHtml('Financial snapshot', renderFinancials(s.financials), { locked: isLocked('financials') })}
      ${sectionHtml('Forensic flags', renderForensic(s.forensic), { locked: isLocked('forensic') })}
      ${sectionHtml('Peer comparison', renderPeers(s.peers), { locked: isLocked('peers') })}
      ${sectionHtml('AI investment thesis', thesisBody, { locked: isLocked('thesis') && !s.thesis_preview })}
      ${sectionHtml('Risk factors', renderProse(s.risks), { locked: isLocked('risks') })}
      ${sectionHtml('Methodology · disclaimer', renderMethodology(s.methodology))}
      <div style="text-align:center;font-size:11px;color:var(--text-tertiary);margin-top:8px;">
        ${report.cached ? 'Cached · ' : 'Fresh · '} Generated ${esc((report.generated_at || '').slice(0, 19))}
      </div>
    `;
  }

  async function mount(host) {
    const ticker = (host.dataset.ticker || '').toUpperCase();
    if (!ticker) {
      host.innerHTML = '<div style="color:var(--text-tertiary);padding:24px;">Set <code>data-ticker</code> to load research.</div>';
      return;
    }
    host.innerHTML = `<ae-empty-state mode="loading"></ae-empty-state>`;
    const tok = getToken();
    const headers = tok ? { Authorization: 'Bearer ' + tok } : {};
    try {
      const r = await fetch('/api/research/' + encodeURIComponent(ticker), { headers });
      if (r.status === 429) {
        const body = await r.json().catch(() => ({}));
        host.innerHTML = `<ae-empty-state mode="error"
          title="Daily research limit reached"
          description="You've used ${body.used_today || 0}/${body.limit || 0} reports today.${tok ? '' : ' Sign in for higher limits.'}"
          cta-label="${tok ? 'Upgrade' : 'Sign in'}"
          cta-href="${tok ? 'pricing.html' : 'login.html'}"></ae-empty-state>`;
        return;
      }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const report = await r.json();
      if (!report.success) throw new Error(report.error || 'unknown error');
      renderReport(host, report);
    } catch (e) {
      host.innerHTML = `<ae-empty-state mode="error"
        title="Couldn't load equity research"
        description="${esc(e.message)}"
        cta-label="Retry"></ae-empty-state>`;
      const es = host.querySelector('ae-empty-state');
      if (es) es.addEventListener('retry', () => mount(host));
    }
  }

  function init() {
    document.querySelectorAll('#ae-research-host, [data-ae-research]').forEach(mount);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
