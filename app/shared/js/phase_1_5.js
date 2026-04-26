// Phase 1.5 frontend wire-up.
// Include AFTER app.js. Initializes commodities, global, and auto-jargon-annotation
// on any page that has the standardized hooks.
//
// Hook elements:
//   <div data-alpha="commodities-impact"></div>   → renders commodity + sector cards
//   <div data-alpha="global-overnight"></div>     → renders overnight-setup card
//   <div data-alpha="promoter-card" data-ticker="RELIANCE"></div>
//   <div data-alpha="news-split"></div>           → renders articles + buzz + filings columns
//   any element with class "jargon-scan" gets auto-annotated on load
//   elements with data-event-id get a forensic badge injected on load

(function () {
  if (typeof window === 'undefined') return;
  if (typeof API === 'undefined') return;

  function fmtPct(v) {
    if (v == null || isNaN(v)) return '—';
    const sign = v > 0 ? '+' : '';
    const cls = v > 0 ? 'color:#2dd4aa' : v < 0 ? 'color:#f26b6b' : '';
    return `<span style="${cls}">${sign}${(+v).toFixed(2)}%</span>`;
  }

  async function renderCommoditiesImpact(host) {
    host.innerHTML = '<em style="opacity:.6">loading commodities impact…</em>';
    try {
      const res = await API.get('/commodities/impact');
      if (!res?.success) throw new Error('fetch failed');
      const d = res.data;
      const commoditiesHtml = Object.entries(d.commodities || {}).map(([name, v]) => `
        <div class="card" style="padding:12px;border:1px solid #2a3142;border-radius:8px;min-width:140px">
          <div style="opacity:.6;font-size:12px;text-transform:uppercase">${name.replace('_', ' ')}</div>
          <div style="font-size:20px;font-weight:600;margin-top:4px">${(+v.close).toFixed(2)}</div>
          <div>${fmtPct(v.pct_change)}</div>
        </div>`).join('');
      const macroHtml = (d.macro_themes || []).map(m => `
        <div style="padding:8px 12px;background:#141a28;border-radius:6px;margin-bottom:6px">
          <b>${m.theme}</b> <span style="opacity:.5">×${m.magnitude_boost}</span>
          <div style="font-size:12px;opacity:.75;margin-top:4px">
            ${(m.example_headlines || []).slice(0,2).map(h => `• ${h}`).join('<br>')}
          </div>
        </div>`).join('') || '<em style="opacity:.5">no active macro themes</em>';
      const sectorHtml = (d.sector_impact || []).map(s => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1f2735">
          <span>${s.sector}</span>
          <span style="color:${s.direction === 'bullish' ? '#2dd4aa' : s.direction === 'bearish' ? '#f26b6b' : '#9aa0aa'}">
            ${s.direction} · ${s.expected_pct > 0 ? '+' : ''}${s.expected_pct}%
          </span>
        </div>`).join('');
      host.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px">
          <div>
            <div style="opacity:.7;font-size:13px;margin-bottom:8px">Active macro themes</div>
            ${macroHtml}
          </div>
          <div>
            <div style="opacity:.7;font-size:13px;margin-bottom:8px">Commodities</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px">${commoditiesHtml}</div>
          </div>
          <div>
            <div style="opacity:.7;font-size:13px;margin-bottom:8px">Sector impact (Indian)</div>
            ${sectorHtml}
          </div>
        </div>
        <div style="opacity:.4;font-size:11px;margin-top:10px">Updated ${d.asof}</div>`;
    } catch (e) {
      host.innerHTML = `<div style="opacity:.6">commodities impact unavailable: ${e.message}</div>`;
    }
  }

  async function renderGlobalOvernight(host) {
    host.innerHTML = '<em style="opacity:.6">loading overnight setup…</em>';
    try {
      const res = await API.get('/global/overnight');
      if (!res?.success) throw new Error('fetch failed');
      const d = res.data;
      const pred = d.prediction || {};
      const dirColor = pred.direction === 'bullish' ? '#2dd4aa' : pred.direction === 'bearish' ? '#f26b6b' : '#9aa0aa';
      const idx = Object.entries(d.indices || {}).map(([name, v]) => `
        <div style="padding:10px;border:1px solid #2a3142;border-radius:6px;min-width:120px">
          <div style="opacity:.6;font-size:11px;text-transform:uppercase">${name}</div>
          <div style="font-size:16px;font-weight:600">${(+v.close).toFixed(2)}</div>
          <div>${fmtPct(v.pct_change)}</div>
        </div>`).join('');
      host.innerHTML = `
        <div style="padding:16px;border:1px solid #2a3142;border-radius:10px;margin-bottom:16px">
          <div style="opacity:.6;font-size:12px;text-transform:uppercase">Expected Nifty open bias</div>
          <div style="font-size:28px;font-weight:700;color:${dirColor};margin-top:4px">
            ${pred.direction || 'neutral'} · ${pred.predicted_pct > 0 ? '+' : ''}${pred.predicted_pct || 0}%
          </div>
          <div style="opacity:.7;font-size:12px">confidence ${Math.round((pred.confidence || 0) * 100)}%</div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px">${idx}</div>
        <div style="opacity:.4;font-size:11px;margin-top:10px">Updated ${d.asof}</div>`;
    } catch (e) {
      host.innerHTML = `<div style="opacity:.6">overnight setup unavailable: ${e.message}</div>`;
    }
  }

  async function renderPromoterCard(host) {
    const ticker = host.dataset.ticker;
    if (!ticker) return;
    host.innerHTML = '<em style="opacity:.6">loading promoter data…</em>';
    try {
      const res = await API.get(`/stock/${ticker}/promoter`);
      if (!res?.success) throw new Error('fetch failed');
      const d = res.data;
      const qRows = (d.quarters || []).map(q => `
        <tr>
          <td style="opacity:.6">${q.quarter_end}</td>
          <td>${(+q.promoter_pct || 0).toFixed(2)}%</td>
          <td style="color:${(+q.promoter_pledge_pct || 0) > 20 ? '#f26b6b' : ''}">${(+q.promoter_pledge_pct || 0).toFixed(2)}%</td>
          <td>${(+q.fii_pct || 0).toFixed(2)}%</td>
        </tr>`).join('');
      const flags = Object.entries(d.trend_flags || {}).filter(([k, v]) => v).map(([k]) => `
        <span style="background:#3a1418;color:#f26b6b;padding:2px 8px;border-radius:10px;font-size:11px;margin-right:4px">${k.replace(/_/g, ' ')}</span>
      `).join('') || '<span style="color:#2dd4aa;font-size:12px">No red flags</span>';
      host.innerHTML = `
        <div style="padding:16px;border:1px solid #2a3142;border-radius:10px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <div style="font-weight:600">Promoter / shareholding</div>
            <div>${flags}</div>
          </div>
          <table style="width:100%;font-size:13px;border-collapse:collapse">
            <thead><tr style="opacity:.6"><th align=left>Qtr</th><th align=left>Promoter</th><th align=left>Pledge</th><th align=left>FII</th></tr></thead>
            <tbody>${qRows || '<tr><td colspan=4 style="opacity:.5">no data yet</td></tr>'}</tbody>
          </table>
        </div>`;
    } catch (e) {
      host.innerHTML = `<div style="opacity:.6">promoter data unavailable: ${e.message}</div>`;
    }
  }

  function _newsItemHtml(item) {
    const ts = item.published_at || item.created_at || '';
    const eventId = item.event_id ? ` data-event-id="${item.event_id}"` : '';
    const source = item.source || 'unknown';
    // Inline badges: sentiment + social-priority + news-type chip
    const sentColor = item.sentiment === 'bullish' ? '#4edea3'
                    : item.sentiment === 'bearish' ? '#ffb4ab' : '#8c909f';
    const typeChip = item.news_type === 'social_buzz'
      ? '<span style="background:#3a2a14;color:#f2a96b;padding:1px 6px;border-radius:8px;font-size:9px;margin-right:4px">social</span>'
      : item.news_type === 'filing'
        ? '<span style="background:#14331f;color:#2dd4aa;padding:1px 6px;border-radius:8px;font-size:9px;margin-right:4px">filing</span>'
        : '';
    return `
      <div style="padding:10px;border-bottom:1px solid #1f2735"${eventId}>
        <div style="font-size:13px;line-height:1.4">${item.title || ''}</div>
        <div style="margin-top:4px;font-size:11px;opacity:.75">
          ${typeChip}
          <span>${source}</span>
          ${item.sentiment ? ` · <span style="color:${sentColor}">${item.sentiment}</span>` : ''}
          ${ts ? ` · <span style="opacity:.6">${String(ts).slice(0,16).replace('T',' ')}</span>` : ''}
        </div>
      </div>`;
  }

  // Decorate any rendered container with priority-boost chips + forensic badges
  async function attachAllBadges(root) {
    if (!root) return;
    attachForensicBadges(root);
    if (typeof PriorityChip === 'undefined') return;
    // Pull priority clusters in one batch and mark any matching event_id on screen
    try {
      const r = await API.get('/events/priority');
      const clusters = (r?.data || []).reduce((m, c) => { m[c.cluster_hash] = c; return m; }, {});
      root.querySelectorAll('[data-event-id]').forEach(el => {
        const id = el.dataset.eventId;
        const c = clusters[id];
        if (c && !el.dataset.priorityAttached) {
          el.dataset.priorityAttached = '1';
          const chip = PriorityChip.render(c.first_seen_source, null);
          if (chip) el.insertAdjacentHTML('beforeend', ' ' + chip);
        }
      });
    } catch (e) { /* silent */ }
  }

  async function renderNewsSplit(host) {
    host.innerHTML = '<em style="opacity:.6">loading news…</em>';
    try {
      const res = await API.get('/news?type=all&limit=30');
      if (!res?.success) throw new Error('fetch failed');
      const { articles = [], buzz = [], filings = [] } = res.data || {};
      host.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">
          <div>
            <div style="padding:6px 10px;background:#142a3a;color:#8eb4e0;font-weight:600;font-size:12px;border-radius:6px 6px 0 0;text-transform:uppercase;letter-spacing:.5px">
              News articles · ${articles.length}
            </div>
            <div style="max-height:480px;overflow:auto;border:1px solid #2a3142;border-top:none;border-radius:0 0 6px 6px">
              ${articles.map(_newsItemHtml).join('') || '<div style="padding:16px;opacity:.5;font-size:12px">no articles yet</div>'}
            </div>
          </div>
          <div>
            <div style="padding:6px 10px;background:#3a2a14;color:#f2a96b;font-weight:600;font-size:12px;border-radius:6px 6px 0 0;text-transform:uppercase;letter-spacing:.5px">
              Social buzz · ${buzz.length}
            </div>
            <div style="max-height:480px;overflow:auto;border:1px solid #2a3142;border-top:none;border-radius:0 0 6px 6px">
              ${buzz.map(_newsItemHtml).join('') || '<div style="padding:16px;opacity:.5;font-size:12px">no social posts yet — configure REDDIT/TELEGRAM creds in .env</div>'}
            </div>
          </div>
          <div>
            <div style="padding:6px 10px;background:#14331f;color:#2dd4aa;font-weight:600;font-size:12px;border-radius:6px 6px 0 0;text-transform:uppercase;letter-spacing:.5px">
              Official filings · ${filings.length}
            </div>
            <div style="max-height:480px;overflow:auto;border:1px solid #2a3142;border-top:none;border-radius:0 0 6px 6px">
              ${filings.map(_newsItemHtml).join('') || '<div style="padding:16px;opacity:.5;font-size:12px">no filings yet</div>'}
            </div>
          </div>
        </div>`;
      // attach forensic + priority-boost chips after render
      attachAllBadges(host);
    } catch (e) {
      host.innerHTML = `<div style="opacity:.6">news split unavailable: ${e.message}</div>`;
    }
  }

  async function renderUnexplainedVolume(host) {
    host.innerHTML = '<em style="opacity:.6">loading volume anomalies…</em>';
    try {
      const res = await API.get('/signals/unexplained-volume');
      if (!res?.success) throw new Error('fetch failed');
      const rows = res.data || [];
      if (!rows.length) {
        host.innerHTML = '<div style="opacity:.5;font-size:12px;text-align:center;padding:12px">No volume anomalies flagged yet.</div>';
        return;
      }
      host.innerHTML = `
        <div style="font-size:11px;opacity:.7;margin-bottom:6px">Stocks with OBV divergence or unexplained volume surge — "someone knows something."</div>
        ${rows.slice(0, 20).map(r => {
          const divColor = r.obv_divergence_flag === 'bearish' ? '#ffb4ab' : r.obv_divergence_flag === 'bullish' ? '#4edea3' : '#8c909f';
          return `
            <div style="padding:8px;border-bottom:1px solid #1f2735;display:flex;justify-content:space-between;align-items:center" data-ticker="${r.ticker}">
              <div>
                <b style="color:#dfe2eb">${r.ticker}</b>
                <span style="opacity:.7;font-size:11px;margin-left:6px">${(r.headline || '').slice(0,80)}</span>
              </div>
              <div style="display:flex;gap:6px;align-items:center">
                ${r.volume_confirmation ? '<span style="color:#f2c96b;font-size:10px">surge</span>' : ''}
                ${r.obv_divergence_flag ? `<span style="color:${divColor};font-size:10px;text-transform:uppercase">${r.obv_divergence_flag}</span>` : ''}
                ${r.manipulation_score != null ? ForensicBadge.render(r.manipulation_score, r.manipulation_score >= 70 ? 'likely_manipulated' : r.manipulation_score >= 40 ? 'unverified' : 'clean') : ''}
              </div>
            </div>`;
        }).join('')}`;
    } catch (e) {
      host.innerHTML = `<div style="opacity:.6">volume anomalies unavailable: ${e.message}</div>`;
    }
  }

  async function attachForensicBadges(root) {
    if (typeof ForensicBadge === 'undefined') return;
    const hosts = root.querySelectorAll('[data-event-id]');
    for (const el of hosts) {
      const eid = el.dataset.eventId;
      if (!eid || el.dataset.forensicLoaded) continue;
      el.dataset.forensicLoaded = '1';
      try {
        const r = await API.get(`/signal/${encodeURIComponent(eid)}/forensics`);
        if (r?.success && r.data?.score) {
          const s = r.data.score;
          const badge = ForensicBadge.render(s.score, s.band);
          el.insertAdjacentHTML('beforeend', ' ' + badge);
        }
      } catch (e) { /* silent */ }
    }
  }

  async function run() {
    document.querySelectorAll('[data-alpha="commodities-impact"]').forEach(renderCommoditiesImpact);
    document.querySelectorAll('[data-alpha="global-overnight"]').forEach(renderGlobalOvernight);
    document.querySelectorAll('[data-alpha="promoter-card"]').forEach(renderPromoterCard);
    document.querySelectorAll('[data-alpha="news-split"]').forEach(renderNewsSplit);
    document.querySelectorAll('[data-alpha="unexplained-volume"]').forEach(renderUnexplainedVolume);
    if (typeof Jargon !== 'undefined') {
      document.querySelectorAll('.jargon-scan').forEach(el => Jargon.annotate(el));
    }
    attachForensicBadges(document);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }

  window.AlphaPhase15 = { run, renderCommoditiesImpact, renderGlobalOvernight, renderPromoterCard, renderNewsSplit, renderUnexplainedVolume, attachForensicBadges, attachAllBadges };
})();
