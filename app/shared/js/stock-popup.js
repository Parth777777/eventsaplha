/* stock-popup.js — instant ticker quick-view modal.
 *
 * Click any element with [data-ticker="XXX"] anywhere in the app ? opens a
 * centered modal with: company name, sector, last price/change, alpha score,
 * recent news headlines, top signal, and an AI summary (Groq) on demand.
 *
 * Click "Open full page ?" to navigate to stock.html.
 *
 * Auto-mounts: nothing to call. Just include this script and add the
 *   data-ticker  attribute to any clickable element.
 *
 * To suppress popup on a specific element (e.g. a chip whose default action
 * SHOULD be navigation), add  data-ticker-link="full"  next to data-ticker.
 */
(function () {
    if (window.StockPopup) return; // singleton

    // Visible startup log. If you don't see "v21" here, your browser is
    // serving a cached older version — unregister the service worker and
    // hard refresh (Ctrl+Shift+R) to pick up the latest.
    const SP_VERSION = 'v21-mini-chart+deep-details';
    try { console.log('[StockPopup] ' + SP_VERSION + ' loaded'); } catch (_) {}

    let modal = null;
    let currentTicker = null;
    let abortCtl = null;

    function injectStyles() {
        if (document.getElementById('sp-styles')) return;
        const s = document.createElement('style');
        s.id = 'sp-styles';
        s.textContent = `
            .sp-overlay {
                position: fixed; inset: 0; z-index: 9000;
                background: rgba(8,12,18,0.78); backdrop-filter: blur(4px);
                align-items: center; justify-content: center;
                opacity: 0; transition: opacity 140ms ease;
                padding: 20px;
                display: none;            /* Hidden by default — open() toggles to flex */
                pointer-events: none;
            }
            .sp-overlay.open { display: flex; opacity: 1; pointer-events: auto; }
            .sp-modal {
                width: 100%; max-width: 720px; max-height: 90vh; overflow: auto;
                background: linear-gradient(180deg, rgba(17,23,32,0.97), rgba(13,17,24,0.97));
                border: 1px solid rgba(141,180,224,0.25);
                border-radius: 14px;
                box-shadow: 0 24px 80px rgba(0,0,0,0.7);
                color: var(--text-primary);
                transform: translateY(8px) scale(0.98);
                transition: transform 160ms ease;
            }
            .sp-overlay.open .sp-modal { transform: translateY(0) scale(1); }
            .sp-head {
                display: flex; align-items: flex-start; justify-content: space-between;
                gap: 12px; padding: 16px 18px;
                border-bottom: 1px solid rgba(37,48,64,0.5);
            }
            .sp-head-l { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
            .sp-brand {
                width: 40px; height: 40px;
                flex: 0 0 40px;
                border-radius: 50%;
                overflow: hidden;
                background: #fff;
                display: inline-flex; align-items: center; justify-content: center;
                box-shadow: 0 0 0 1px rgba(255,255,255,0.12), 0 2px 8px rgba(0,0,0,0.55);
            }
            .sp-brand img {
                width: 100%; height: 100%;
                object-fit: contain;
                padding: 4px;
                box-sizing: border-box;
                background: transparent;
                border: 0;
                box-shadow: none;
                margin: 0;
                display: block;
            }
            .sp-head .tk {
                font-family: 'Geist Mono', monospace;
                font-weight: 800; font-size: 22px; color: #fff;
                letter-spacing: -0.01em; line-height: 1;
            }
            .sp-head .co {
                font-size: 11px; color: var(--text-secondary); margin-top: 4px;
                font-weight: 500;
            }
            .sp-head .meta {
                font-size: 9px; color: var(--text-tertiary); margin-top: 6px;
                text-transform: uppercase; letter-spacing: 0.1em; font-weight: 800;
            }
            .sp-close {
                background: transparent; border: none; cursor: pointer;
                color: var(--text-secondary); padding: 4px; line-height: 0;
                border-radius: 6px;
            }
            .sp-close:hover { color: #fff; background: rgba(141,180,224,0.1); }

            .sp-body { padding: 14px 18px; }

            .sp-stat-row {
                display: grid; grid-template-columns: repeat(3, 1fr);
                gap: 1px; background: rgba(37,48,64,0.4);
                border-radius: 8px; overflow: hidden;
                margin-bottom: 14px;
            }
            .sp-mini-chart-wrap {
                position: relative;
                background: rgba(8,12,18,0.55);
                border: 1px solid rgba(37,48,64,0.5);
                border-radius: 10px;
                padding: 8px 8px 6px;
                margin-bottom: 14px;
            }
            .sp-mini-chart-wrap canvas {
                width: 100%; height: 120px; display: block;
            }
            .sp-mini-chart-meta {
                display: flex; justify-content: space-between; align-items: center;
                margin-top: 4px;
                font-size: 9px; color: var(--text-tertiary);
                text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;
            }
            .sp-period-pills { display: inline-flex; gap: 3px; }
            .sp-period-pills button {
                background: transparent; border: 1px solid rgba(37,48,64,0.6);
                color: var(--text-secondary); font-size: 9px; font-weight: 800;
                padding: 3px 7px; border-radius: 999px; cursor: pointer;
                letter-spacing: 0.06em; text-transform: uppercase;
                transition: all 80ms;
            }
            .sp-period-pills button:hover { color: var(--text-primary); border-color: rgba(141,180,224,0.5); }
            .sp-period-pills button.on {
                color: #2dd4aa; border-color: rgba(45,212,170,0.5);
                background: rgba(45,212,170,0.08);
            }
            .sp-stat {
                background: rgba(13,17,24,0.92); padding: 10px 12px;
                text-align: center;
            }
            .sp-stat .lbl {
                font-size: 8.5px; color: var(--text-tertiary); font-weight: 800;
                text-transform: uppercase; letter-spacing: 0.12em;
                margin-bottom: 4px;
            }
            .sp-stat .val {
                font-family: 'Geist Mono', monospace;
                font-size: 16px; font-weight: 800; color: var(--text-primary);
            }
            .sp-stat.up .val   { color: #2dd4aa; }
            .sp-stat.dn .val   { color: #f26b6b; }
            .sp-stat.flat .val { color: var(--text-secondary); }

            .sp-section {
                margin-top: 16px;
                border-top: 1px solid rgba(37,48,64,0.4);
                padding-top: 12px;
            }
            .sp-section .h {
                font-size: 9px; font-weight: 800; color: var(--text-secondary);
                text-transform: uppercase; letter-spacing: 0.14em;
                margin-bottom: 8px;
            }
            .sp-news {
                display: flex; flex-direction: column; gap: 6px;
            }
            .sp-news a {
                color: var(--text-primary); text-decoration: none;
                font-size: 12px; line-height: 1.45;
                padding: 6px 9px; border-radius: 6px;
                background: rgba(13,17,24,0.45);
                border: 1px solid rgba(37,48,64,0.4);
                transition: border-color 100ms;
            }
            .sp-news a:hover { border-color: rgba(141,180,224,0.4); color: #fff; }
            .sp-news .meta {
                font-size: 9px; color: var(--text-tertiary); margin-top: 3px;
                font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
            }

            .sp-ai-btn {
                display: inline-flex; align-items: center; gap: 6px;
                padding: 6px 12px; border-radius: 999px;
                background: linear-gradient(135deg, rgba(141,180,224,0.18), rgba(167,139,250,0.18));
                border: 1px solid rgba(141,180,224,0.4);
                color: #c9d3e6; font-size: 10px; font-weight: 700;
                letter-spacing: 0.06em; text-transform: uppercase;
                cursor: pointer; transition: all 100ms;
            }
            .sp-ai-btn:hover { color: #fff; border-color: rgba(141,180,224,0.7); }
            .sp-ai-btn:disabled { opacity: 0.6; cursor: wait; }
            .sp-ai-out {
                margin-top: 8px; padding: 10px 12px;
                background: rgba(141,180,224,0.06);
                border: 1px solid rgba(141,180,224,0.2);
                border-radius: 8px;
                font-size: 12px; line-height: 1.55; color: var(--text-primary);
                display: none;
            }
            .sp-ai-out.show { display: block; }
            .sp-ai-out .src {
                display: inline-block; font-size: 8.5px;
                color: #a78bfa; font-weight: 800;
                text-transform: uppercase; letter-spacing: 0.1em;
                margin-right: 6px;
            }

            .sp-foot {
                display: flex; justify-content: space-between; align-items: center;
                padding: 12px 18px;
                border-top: 1px solid rgba(37,48,64,0.5);
                background: rgba(8,12,18,0.4);
            }
            .sp-full-link {
                color: #8eb4e0; text-decoration: none;
                font-size: 11px; font-weight: 700;
                letter-spacing: 0.04em; text-transform: uppercase;
                display: inline-flex; align-items: center; gap: 4px;
            }
            .sp-full-link:hover { color: #fff; }
            .sp-watchlist-btn {
                background: transparent; border: 1px solid rgba(141,148,168,0.3);
                color: var(--text-secondary); padding: 5px 10px; border-radius: 6px;
                font-size: 10px; font-weight: 700; cursor: pointer;
                letter-spacing: 0.04em; text-transform: uppercase;
                transition: all 100ms;
            }
            .sp-watchlist-btn:hover { color: #e6b84a; border-color: #e6b84a; }
            .sp-watchlist-btn.on { color: #e6b84a; border-color: #e6b84a;
                background: rgba(230,184,74,0.12); }

            .sp-skel {
                background: linear-gradient(90deg, rgba(60,72,90,0.18), rgba(60,72,90,0.32), rgba(60,72,90,0.18));
                background-size: 200% 100%;
                animation: sp-shimmer 1.4s infinite linear;
                border-radius: 4px;
            }
            @keyframes sp-shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }

            /* === Restored deep-detail sections (forensics, volume, promoter, etc.) === */
            .sp-mini-row {
                display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px;
                margin-bottom: 8px;
            }
            .sp-mini-cell {
                padding: 8px; background: rgba(13,17,24,0.7); border-radius: 8px;
                text-align: center; border: 1px solid rgba(37,48,64,0.4);
            }
            .sp-mini-cell .lbl {
                font-size: 8.5px; color: var(--text-secondary); text-transform: uppercase;
                letter-spacing: 0.1em; font-weight: 700;
            }
            .sp-mini-cell .val {
                font-size: 14px; font-weight: 800;
                font-family: 'Geist Mono', monospace; margin-top: 3px;
            }
            .sp-gauge-card {
                padding: 12px; background: rgba(13,17,24,0.7);
                border: 1px solid rgba(37,48,64,0.4); border-radius: 10px;
                text-align: center;
            }
            .sp-pill {
                display: inline-block; padding: 2px 8px; border-radius: 10px;
                font-size: 9px; font-weight: 700; margin: 2px;
            }
            .sp-list {
                background: rgba(13,17,24,0.7); border: 1px solid rgba(37,48,64,0.4);
                border-radius: 8px; max-height: 160px; overflow-y: auto;
            }
            .sp-list-row {
                padding: 6px 9px; border-bottom: 1px solid rgba(37,48,64,0.35);
                font-size: 11px;
            }
            .sp-list-row:last-child { border-bottom: none; }
            .sp-empty {
                padding: 10px; color: var(--text-tertiary); font-size: 10px; text-align: center;
            }
            .sp-spark-wrap {
                position: relative; background: rgba(8,12,18,0.7);
                border: 1px solid rgba(37,48,64,0.4); border-radius: 8px;
                padding: 4px;
            }
            .sp-spark-wrap canvas { width: 100%; height: 70px; display: block; }
            .sp-spark-wrap .lbl {
                position: absolute; top: 6px; left: 10px; font-size: 9px;
                color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.1em;
            }
            .sp-grid-2 { display: grid; grid-template-columns: 1.2fr 1fr; gap: 10px; }
            .sp-tl-row { display: flex; gap: 0; padding: 0; }
            .sp-tl-spine {
                flex-shrink: 0; width: 22px; display: flex;
                flex-direction: column; align-items: center;
            }
            .sp-tl-dot {
                width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; z-index: 2;
            }
            .sp-tl-line { width: 1px; flex: 1; background: rgba(37,48,64,0.5); min-height: 22px; }
        `;
        document.head.appendChild(s);
    }

    function ensureModal() {
        if (modal) return modal;
        modal = document.createElement('div');
        modal.className = 'sp-overlay';
        modal.innerHTML = `<div class="sp-modal" role="dialog" aria-modal="true"></div>`;
        modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
        document.body.appendChild(modal);
        return modal;
    }

    function _esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g,
            c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function fmtPx(v) {
        if (v == null || isNaN(v)) return '—';
        const n = Number(v);
        if (n >= 1000) return '?' + n.toLocaleString('en-IN', { maximumFractionDigits: 0 });
        return '?' + n.toFixed(2);
    }
    function fmtPct(v) {
        if (v == null || isNaN(v)) return '—';
        const n = Number(v);
        return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
    }
    function alphaColor(a) {
        if (a == null) return 'var(--text-tertiary)';
        if (a >= 65) return '#2dd4aa';
        if (a >= 50) return '#e6b84a';
        return 'var(--text-secondary)';
    }

    function renderShell(ticker, company) {
        const inner = modal.querySelector('.sp-modal');
        // Use a plain <img> inside the .sp-brand chip — the CSS gives the
        // chip its perfect circle + white fill, so we don't want CompanyLogo's
        // own background/border fighting our wrapper.
        const logoUrl = (window.CompanyLogo && CompanyLogo.url) ? CompanyLogo.url(ticker) : null;
        const headerLogo = logoUrl
            ? `<img src="${logoUrl}" alt="${_esc(ticker)}" loading="lazy" decoding="async"/>`
            : '';
        inner.innerHTML = `
            <div class="sp-head">
                <div class="sp-head-l">
                    <span class="sp-brand" data-no-logo>${headerLogo}</span>
                    <div>
                        <div class="tk">${_esc(ticker)}</div>
                        <div class="co">${_esc(company || 'Loading…')}</div>
                        <div class="meta" id="sp-meta-line">Quick view · ${SP_VERSION}</div>
                    </div>
                </div>
                <div style="display:flex;align-items:center;gap:6px;">
                    <button class="sp-analyze" data-edge-analyze="${_esc(ticker)}" title="Open full fundamental analysis"
                            style="display:inline-flex;align-items:center;gap:4px;padding:5px 10px;border:1px solid rgba(45,212,170,0.4);background:rgba(45,212,170,0.10);color:#2dd4aa;border-radius:6px;cursor:pointer;font:700 10px 'Inter',sans-serif;letter-spacing:0.06em;text-transform:uppercase;">
                        <span class="material-symbols-outlined" style="font-size:14px;">analytics</span>Analyze
                    </button>
                    <button class="sp-close" aria-label="Close">
                        <span class="material-symbols-outlined" style="font-size:22px;">close</span>
                    </button>
                </div>
            </div>
            <div class="sp-body">
                <div class="sp-stat-row" id="sp-stats">
                    <div class="sp-stat flat"><div class="lbl">Last</div><div class="val">—</div></div>
                    <div class="sp-stat flat"><div class="lbl">Today</div><div class="val">—</div></div>
                    <div class="sp-stat flat"><div class="lbl">Alpha</div><div class="val">—</div></div>
                </div>
                <div class="sp-mini-chart-wrap" id="sp-mini-chart-wrap">
                    <!-- AdvancedChart (compact) mounts here so the popup chart looks
                         identical to stock.html's. Legacy canvas kept hidden for
                         downstream code that targets #sp-mini-chart by id. -->
                    <div id="sp-mini-chart-adv" style="width:100%;height:170px;"></div>
                    <canvas id="sp-mini-chart" width="640" height="140" style="display:none;"></canvas>
                    <div class="sp-mini-chart-meta">
                        <span id="sp-mini-chart-label" style="font:600 10px 'Geist Mono',monospace;color:var(--text-tertiary);">drag chart to zoom · dbl-click to reset</span>
                        <span class="sp-period-pills" id="sp-mini-chart-pills">
                            <button data-p="1D">1D</button>
                            <button data-p="1W">1W</button>
                            <button data-p="1M" class="on">1M</button>
                            <button data-p="3M">3M</button>
                            <button data-p="6M">6M</button>
                            <button data-p="1Y">1Y</button>
                        </span>
                    </div>
                </div>
                <div class="sp-section">
                    <div class="h">Top signal</div>
                    <div id="sp-signal" style="font-size:12px;color:var(--text-secondary);">Loading…</div>
                </div>
                <div class="sp-section">
                    <div class="h">Why this signal</div>
                    <div id="sp-reasoning" style="font-size:11px;color:var(--text-tertiary);">Loading…</div>
                </div>
                <div class="sp-section">
                    <div class="h">Volume / OBV (60d)</div>
                    <div id="sp-volume" style="font-size:11px;color:var(--text-tertiary);">Loading…</div>
                </div>
                <div class="sp-section">
                    <div class="h">Forensics intelligence</div>
                    <div id="sp-forensics" style="font-size:11px;color:var(--text-tertiary);">Loading…</div>
                </div>
                <div class="sp-section">
                    <div class="h">Promoter intelligence</div>
                    <div id="sp-promoter" style="font-size:11px;color:var(--text-tertiary);">Loading…</div>
                </div>
                <div class="sp-section">
                    <div class="h">Event timeline</div>
                    <div id="sp-timeline" style="font-size:11px;color:var(--text-tertiary);">Loading…</div>
                </div>
                <div class="sp-section">
                    <div class="h" style="display:flex;align-items:center;justify-content:space-between;">
                        Recent news
                        <button class="sp-ai-btn" id="sp-ai-btn">
                            <span class="material-symbols-outlined" style="font-size:13px;">auto_awesome</span>
                            AI summary
                        </button>
                    </div>
                    <div class="sp-ai-out" id="sp-ai-out"></div>
                    <div class="sp-news" id="sp-news"></div>
                </div>
            </div>
            <div class="sp-foot">
                <a class="sp-full-link" href="stock.html?ticker=${encodeURIComponent(ticker)}">
                    Open full page <span class="material-symbols-outlined" style="font-size:14px;">arrow_forward</span>
                </a>
                <button class="sp-watchlist-btn" id="sp-wl-btn">
                    <span class="material-symbols-outlined" style="font-size:11px;vertical-align:-1px;">star</span>
                    Watch
                </button>
            </div>
        `;
        inner.querySelector('.sp-close').addEventListener('click', close);
        inner.querySelector('#sp-ai-btn').addEventListener('click', loadAISummary);
        inner.querySelector('#sp-wl-btn').addEventListener('click', toggleWatch);
        // Period pills on the mini chart — bind once at shell render time.
        const pills = inner.querySelector('#sp-mini-chart-pills');
        if (pills) {
            pills.addEventListener('click', (e) => {
                const b = e.target && e.target.closest('button[data-p]');
                if (!b || !currentTicker) return;
                pills.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
                loadMiniChart(currentTicker, b.dataset.p);
            });
        }
    }

    async function open(ticker) {
        if (!ticker) return;
        injectStyles();
        ensureModal();
        ticker = String(ticker).trim().toUpperCase();
        currentTicker = ticker;
        renderShell(ticker, null);
        modal.classList.add('open');
        document.documentElement.style.overflow = 'hidden';

        // Fetch primary detail first so we can pull event_id for reasoning.
        if (abortCtl) abortCtl.abort();
        abortCtl = (typeof AbortController === 'function') ? new AbortController() : null;
        let eventId = null;
        let detailJson = null;
        // Step 1 — network fetch only. Don't let renderDetail throwing be
        // misreported as a network failure ("Couldn't fetch quick info.").
        try {
            const res = await fetch(`/api/stock/${encodeURIComponent(ticker)}`,
                { signal: abortCtl ? abortCtl.signal : undefined });
            detailJson = await res.json();
        } catch (e) {
            if (e.name !== 'AbortError') {
                try { console.error('[StockPopup] detail fetch failed', e); } catch (_) {}
                renderError("Couldn't fetch quick info.");
            }
        }
        // Step 2 — render. Errors here are render bugs, not network bugs.
        if (detailJson) {
            try {
                if (detailJson.success !== false) {
                    const data = detailJson.data || detailJson;
                    renderDetail(data);
                    eventId = (data.signal && data.signal.event_id) || data.event_id || null;
                } else {
                    renderError('Stock not found in database.');
                }
            } catch (e) {
                try { console.error('[StockPopup] renderDetail crash', e, detailJson); } catch (_) {}
                renderError('Render error — see console.');
            }
        }

        // All other panels run in parallel so the modal fills out fast.
        const tasks = [
            fetch(`/api/stock/${encodeURIComponent(ticker)}/news?limit=4`)
                .then(r => r.json()).then(j => renderNews((j && j.data) || []))
                .catch(() => renderNews([])),
            loadMiniChart(ticker, '1M'),
            loadVolume(ticker),
            loadForensics(ticker),
            loadPromoter(ticker),
            loadTimeline(ticker),
            loadReasoning(eventId),
        ];
        await Promise.allSettled(tasks);
    }

    function renderDetail(data) {
        if (!data || !modal) return;
        const inner = modal.querySelector('.sp-modal');
        const ticker = (data.ticker || data.signal?.ticker || currentTicker || '').toUpperCase();
        const company = data.company || data.signal?.company || data.profile?.name || '';
        const sector  = data.sector  || data.profile?.sector || '';
        const px = data.price ?? data.last_price ?? data.signal?.entry_price;
        const chg = data.change_pct ?? data.signal?.change_pct;
        const alpha = data.alpha_score ?? data.signal?.alpha_score;
        const sentiment = data.sentiment || data.signal?.sentiment;

        const head = inner.querySelector('.sp-head');
        head.querySelector('.co').textContent = company || '—';
        head.querySelector('.meta').textContent =
            [sector, sentiment ? sentiment.toUpperCase() : '', data.exchange || ''].filter(Boolean).join(' · ');

        const stats = inner.querySelector('#sp-stats');
        const chgClass = chg == null ? 'flat' : chg > 0 ? 'up' : chg < 0 ? 'dn' : 'flat';
        stats.children[0].querySelector('.val').textContent = fmtPx(px);
        const t1 = stats.children[1];
        t1.className = 'sp-stat ' + chgClass;
        t1.querySelector('.val').textContent = fmtPct(chg);
        const a = stats.children[2];
        const av = (alpha != null) ? Number(alpha).toFixed(0) : '—';
        a.querySelector('.val').textContent = av;
        a.querySelector('.val').style.color = alphaColor(alpha);

        const sigEl = inner.querySelector('#sp-signal');
        const sig = data.signal || data;
        if (sig && sig.headline) {
            sigEl.innerHTML = `
                <div style="font-weight:700;color:var(--text-primary);line-height:1.45;">${_esc((sig.headline || '').slice(0, 200))}</div>
                <div style="margin-top:4px;font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;">
                    ${_esc((sig.event_type || 'news').replace(/_/g, ' '))} · ${_esc(sig.source || '')}
                </div>
            `;
        } else {
            sigEl.textContent = 'No active signal in last 7 days.';
        }
    }

    function renderNews(items) {
        const el = modal && modal.querySelector('#sp-news');
        if (!el) return;
        if (!items || !items.length) {
            el.innerHTML = `<div style="font-size:11px;color:var(--text-tertiary);">No recent news for this ticker.</div>`;
            return;
        }
        el.innerHTML = items.slice(0, 4).map(n => `
            <a href="${_esc(n.link || '#')}" target="_blank" rel="noopener">
                ${_esc((n.title || n.headline || '').slice(0, 140))}
                <div class="meta">${_esc((n.source || '').replace(/_/g, ' '))} · ${
                    window.NewsTime ? '<span style="color:inherit">' + (NewsTime.classify(n.published_at || n.created_at).label) + '</span>'
                                    : (n.age_hours ? Math.round(n.age_hours) + 'h ago' : '')
                }</div>
            </a>
        `).join('');
    }

    function renderError(msg) {
        if (!modal) return;
        modal.querySelector('#sp-signal').textContent = msg;
        modal.querySelector('#sp-news').innerHTML =
            `<div style="font-size:11px;color:#f26b6b;">${_esc(msg)}</div>`;
    }

    async function loadAISummary() {
        if (!currentTicker) return;
        const btn = modal.querySelector('#sp-ai-btn');
        const out = modal.querySelector('#sp-ai-out');
        if (out.classList.contains('show')) {
            out.classList.remove('show');
            return;
        }
        if (out.dataset.loaded === '1') {
            out.classList.add('show');
            return;
        }
        btn.disabled = true;
        out.classList.add('show');
        out.innerHTML = `<div class="sp-skel" style="height:14px;width:80%;"></div>
                         <div class="sp-skel" style="height:14px;width:65%;margin-top:6px;"></div>`;
        try {
            // Use the existing /api/news/summarize: pull the top news article
            // for this ticker, summarize it.
            const r = await fetch(`/api/stock/${encodeURIComponent(currentTicker)}/news?limit=1`);
            const nj = await r.json();
            const top = (nj && nj.data && nj.data[0]) || null;
            if (!top) {
                out.innerHTML = `<span class="src">No news</span>Nothing recent to summarize.`;
                out.dataset.loaded = '1';
                return;
            }
            const url = `/api/news/summarize?event_id=${encodeURIComponent(top.event_id || '')}` +
                        (top.title ? `&title=${encodeURIComponent(top.title)}` : '') +
                        (top.summary ? `&body=${encodeURIComponent((top.summary || '').slice(0, 800))}` : '');
            const sr = await fetch(url);
            const sj = await sr.json();
            const data = (sj && sj.data) || {};
            const tag = data.source === 'groq' ? 'AI · Groq' : 'TL;DR';
            out.innerHTML = `<span class="src">${_esc(tag)}</span>${_esc(data.summary || 'No summary available.')}`;
            out.dataset.loaded = '1';
        } catch (e) {
            out.innerHTML = `<span class="src" style="color:#f26b6b;">Error</span>Couldn't generate summary.`;
        } finally {
            btn.disabled = false;
        }
    }

    async function toggleWatch() {
        if (!currentTicker) return;
        const btn = modal.querySelector('#sp-wl-btn');
        try {
            const isOn = btn.classList.contains('on');
            const method = isOn ? 'DELETE' : 'POST';
            const r = await fetch(`/api/watchlist/${encodeURIComponent(currentTicker)}`, {
                method, headers: { 'Content-Type': 'application/json' },
            });
            if (r.ok) {
                btn.classList.toggle('on');
                btn.innerHTML = btn.classList.contains('on')
                    ? `<span class="material-symbols-outlined" style="font-size:11px;vertical-align:-1px;font-variation-settings:'FILL' 1;">star</span> Watching`
                    : `<span class="material-symbols-outlined" style="font-size:11px;vertical-align:-1px;">star</span> Watch`;
            }
        } catch (e) { /* silently fail */ }
    }

    // ============ Mini price chart =========================================
    // UI labels (1D/1W/1M/3M/6M/1Y) ? yfinance period strings.
    const _PERIOD_MAP = { '1D': '1d', '1W': '5d', '1M': '1mo', '3M': '3mo', '6M': '6mo', '1Y': '1y' };

    // Lazy-load AdvancedChart on demand so the popup chart matches the
    // main stock.html chart on EVERY page (not just stock.html itself).
    // Previously the popup fell back to a canvas variant on pages that
    // didn't explicitly load advanced-chart.js — looked different and
    // sometimes blank.
    function ensureAdvancedChart() {
        return new Promise((resolve) => {
            if (window.AdvancedChart) return resolve(true);
            const existing = document.querySelector('script[data-tw-advanced-chart]');
            if (existing) {
                existing.addEventListener('load', () => resolve(!!window.AdvancedChart));
                existing.addEventListener('error', () => resolve(false));
                return;
            }
            const s = document.createElement('script');
            s.src = './shared/js/advanced-chart.js';
            s.async = true;
            s.setAttribute('data-tw-advanced-chart', '1');
            s.addEventListener('load', () => resolve(!!window.AdvancedChart));
            s.addEventListener('error', () => resolve(false));
            document.head.appendChild(s);
        });
    }

    // Try AdvancedChart compact first so visuals match stock.html. Falls back
    // to the legacy hand-rolled canvas only if AdvancedChart can't be loaded.
    async function loadMiniChart(ticker, period) {
        if (!modal) return;
        const advHost = modal.querySelector('#sp-mini-chart-adv');
        const yfPer = _PERIOD_MAP[period] || period;
        if (advHost) {
            const ok = await ensureAdvancedChart();
            if (ok && window.AdvancedChart) {
                if (advHost.__advChart && advHost.__advChart.setPeriod) {
                    advHost.__advChart.setPeriod(yfPer);
                } else {
                    try {
                        advHost.__advChart = await AdvancedChart.mount(advHost, ticker, { period: yfPer, compact: true });
                    } catch (e) {
                        console.warn('[StockPopup] AdvancedChart.mount failed:', e);
                    }
                }
                return;
            }
        }
        // Legacy fallback --- (kept verbatim so older browsers still work)
        const cvs = modal.querySelector('#sp-mini-chart');
        const label = modal.querySelector('#sp-mini-chart-label');
        if (!cvs) return;
        cvs.style.display = 'block';
        if (label) label.textContent = 'Loading ' + period + ' chart…';
        let yfPeriod = yfPer;
        async function fetchPeriod(p) {
            try {
                const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/chart?period=${encodeURIComponent(p)}`);
                return await r.json();
            } catch (_) { return null; }
        }
        let res = await fetchPeriod(yfPeriod);
        let raw = (res && res.success && Array.isArray(res.data)) ? res.data : [];
        // For 1D: yfinance returns nothing after-hours / on holidays. Fall
        // back to last trading day's session (2d gives the previous session
        // when today is empty).
        if (period === '1D' && raw.filter(d => d && d.close != null).length < 2) {
            res = await fetchPeriod('2d');
            raw = (res && res.success && Array.isArray(res.data)) ? res.data : [];
        }
        // Drop NaN/null closes — otherwise the line plunges to ?0
        const data = raw.filter(d => d && d.close != null && isFinite(Number(d.close)));
        const ctx = cvs.getContext('2d');
        const w = cvs.width, h = cvs.height;
        if (data.length < 2) {
            ctx.clearRect(0, 0, w, h);
            if (label) label.textContent = 'No chart data';
            ctx.fillStyle = 'var(--text-tertiary)';
            ctx.font = '11px Inter, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('No chart data', w / 2, h / 2);
            cvs.onmousemove = cvs.onmouseleave = null;
            return;
        }
        const closes = data.map(d => Number(d.close));
        const min = Math.min(...closes), max = Math.max(...closes), range = (max - min) || 1;
        const first = closes[0], last = closes[closes.length - 1];
        const isUp = last >= first;
        const lineColor = isUp ? '#2dd4aa' : '#f26b6b';
        const fillRgb = isUp ? '45,212,170' : '242,107,107';
        const pad = { t: 10, b: 14, l: 4, r: 4 };
        const cW = w - pad.l - pad.r, cH = h - pad.t - pad.b;
        const pts = data.map((d, i) => ({
            x: pad.l + (i / (data.length - 1)) * cW,
            y: pad.t + (1 - (Number(d.close) - min) / range) * cH,
            close: Number(d.close),
            date: d.date,
        }));

        function draw(hoverIdx) {
            ctx.clearRect(0, 0, w, h);
            // Gradient fill
            const grad = ctx.createLinearGradient(0, pad.t, 0, h);
            grad.addColorStop(0, `rgba(${fillRgb},0.32)`);
            grad.addColorStop(0.7, `rgba(${fillRgb},0.08)`);
            grad.addColorStop(1, `rgba(${fillRgb},0)`);
            ctx.beginPath();
            ctx.moveTo(pts[0].x, pts[0].y);
            for (let i = 1; i < pts.length; i++) {
                const cp = (pts[i].x - pts[i - 1].x) * 0.3;
                ctx.bezierCurveTo(pts[i - 1].x + cp, pts[i - 1].y, pts[i].x - cp, pts[i].y, pts[i].x, pts[i].y);
            }
            ctx.lineTo(pts[pts.length - 1].x, h - pad.b);
            ctx.lineTo(pts[0].x, h - pad.b);
            ctx.closePath();
            ctx.fillStyle = grad; ctx.fill();
            // Line
            ctx.beginPath();
            ctx.moveTo(pts[0].x, pts[0].y);
            for (let i = 1; i < pts.length; i++) {
                const cp = (pts[i].x - pts[i - 1].x) * 0.3;
                ctx.bezierCurveTo(pts[i - 1].x + cp, pts[i - 1].y, pts[i].x - cp, pts[i].y, pts[i].x, pts[i].y);
            }
            ctx.lineWidth = 1.75; ctx.strokeStyle = lineColor; ctx.stroke();
            // Last-point dot
            const lp = pts[pts.length - 1];
            ctx.beginPath(); ctx.arc(lp.x, lp.y, 2.5, 0, Math.PI * 2);
            ctx.fillStyle = lineColor; ctx.fill();

            // Sticky crosshair on last point by default, hover crosshair otherwise
            const idx = hoverIdx != null ? hoverIdx : pts.length - 1;
            const hp = pts[idx];
            if (!hp) return;
            ctx.strokeStyle = hoverIdx != null ? 'rgba(141,148,168,0.55)' : 'rgba(141,148,168,0.28)';
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 3]);
            ctx.beginPath(); ctx.moveTo(hp.x, pad.t); ctx.lineTo(hp.x, h - pad.b); ctx.stroke();
            ctx.setLineDash([]);
            // Dot at crosshair point
            ctx.beginPath(); ctx.arc(hp.x, hp.y, 3.5, 0, Math.PI * 2);
            ctx.fillStyle = lineColor; ctx.fill();
            ctx.strokeStyle = 'var(--surface-0)'; ctx.lineWidth = 2; ctx.stroke();
            // Tooltip
            const txt = `${hp.date} · ?${hp.close.toFixed(2)}`;
            ctx.font = '11px Inter, sans-serif';
            const tw = ctx.measureText(txt).width + 14;
            let tx = hp.x + 10; if (tx + tw > w - 2) tx = hp.x - tw - 10;
            ctx.fillStyle = 'rgba(7,9,13,0.92)';
            ctx.fillRect(tx, 3, tw, 18);
            ctx.strokeStyle = 'rgba(141,148,168,0.3)'; ctx.lineWidth = 1;
            ctx.strokeRect(tx + 0.5, 3.5, tw - 1, 17);
            ctx.fillStyle = 'var(--text-primary)';
            ctx.textAlign = 'left';
            ctx.fillText(txt, tx + 7, 16);
        }

        draw(null);
        cvs.style.cursor = 'crosshair';
        cvs.onmousemove = (e) => {
            const rect = cvs.getBoundingClientRect();
            const mx = (e.clientX - rect.left) * (w / rect.width);
            let nearest = 0, best = Infinity;
            for (let i = 0; i < pts.length; i++) {
                const d = Math.abs(pts[i].x - mx);
                if (d < best) { best = d; nearest = i; }
            }
            draw(nearest);
        };
        cvs.onmouseleave = () => draw(null);

        // Period summary in the label
        const pctMove = ((last - first) / first) * 100;
        const sign = pctMove >= 0 ? '+' : '';
        if (label) {
            label.innerHTML = `<span style="color:var(--text-primary);font-weight:800">${period}</span>
                <span style="color:${lineColor};margin-left:8px">${sign}${pctMove.toFixed(2)}%</span>
                <span style="color:var(--text-tertiary);margin-left:8px">?${min.toFixed(0)} – ?${max.toFixed(0)}</span>`;
        }
    }

    // ============ Deep-detail loaders (ported from legacy popup) ============
    function _alphaColor(a) {
        if (a == null) return 'var(--text-tertiary)';
        if (a >= 65) return '#2dd4aa';
        if (a >= 50) return '#e6b84a';
        return 'var(--text-secondary)';
    }
    function _fmtTime(iso) {
        if (!iso) return '';
        try {
            const d = new Date(iso);
            const diffH = (Date.now() - d.getTime()) / 36e5;
            if (diffH < 1) return Math.round(diffH * 60) + 'm';
            if (diffH < 24) return Math.round(diffH) + 'h';
            return Math.round(diffH / 24) + 'd';
        } catch (_) { return ''; }
    }
    function _setEmpty(id, msg) {
        const el = modal && modal.querySelector('#' + id);
        if (el) el.innerHTML = `<div class="sp-empty">${_esc(msg)}</div>`;
    }

    async function loadReasoning(eventId) {
        const host = modal && modal.querySelector('#sp-reasoning');
        if (!host) return;
        if (!eventId) { _setEmpty('sp-reasoning', 'No active signal to explain.'); return; }
        let res;
        try {
            const r = await fetch(`/api/signal/${encodeURIComponent(eventId)}/reasoning`);
            res = await r.json();
        } catch (e) { res = null; }
        if (!res?.success) { _setEmpty('sp-reasoning', 'Reasoning unavailable for this signal.'); return; }
        const d = res.data || {};
        const meta = d.meta || {};
        const codeColor = d.why_ticker_code === 'direct' ? '#2dd4aa'
                       : d.why_ticker_code === 'sector' ? '#e6b84a' : 'var(--text-secondary)';
        const codeLabel = d.why_ticker_code === 'direct' ? 'DIRECT'
                       : d.why_ticker_code === 'sector' ? 'SECTOR LINK' : 'KEYWORD MATCH';
        const bullets = (d.bullets || []).map(b => {
            const m = b.match(/^([A-Za-z ]+):\s*(.*)$/);
            if (!m) return `<li style="margin-bottom:6px;color:var(--text-primary)">${_esc(b)}</li>`;
            /* Semantic colors map to CSS tokens so chips render correctly
               in both light + dark mode without per-theme branching here. */
            const labelColor = {
                'Ticker':     'var(--info)',
                'Direction':  'var(--bull)',
                'Magnitude':  'var(--caution)',
                'Confidence': 'var(--accent)',
                'Risks':      'var(--bear)',
                'Source':     'var(--text-secondary)',
            }[m[1]] || 'var(--text-secondary)';
            return `<li style="margin-bottom:6px;line-height:1.5;color:var(--text-secondary)">
                <span style="color:${labelColor};font-weight:800;text-transform:uppercase;font-size:9px;letter-spacing:.1em">${_esc(m[1])}</span>
                <span style="margin-left:6px">${_esc(m[2])}</span></li>`;
        }).join('');
        host.innerHTML = `
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
                <span class="sp-pill" style="background:${codeColor}22;color:${codeColor}">${codeLabel}</span>
                ${meta.empirical_event_hit_rate != null
                    ? `<span class="sp-pill" style="background:var(--info-dim);color:var(--info);border:1px solid var(--info-border)">${_esc((meta.event_type||'').toUpperCase())} HIT-RATE ${(meta.empirical_event_hit_rate*100).toFixed(0)}% (n=${meta.event_prior_samples})</span>`
                    : ''}
                ${meta.sector ? `<span class="sp-pill" style="background:var(--bull-dim);color:var(--bull);border:1px solid var(--bull-border)">${_esc(meta.sector)}</span>` : ''}
            </div>
            ${d.primary_driver ? `<div style="font-size:11px;color:var(--text-primary);font-style:italic;margin-bottom:8px;line-height:1.5">${_esc(d.primary_driver)}</div>` : ''}
            <ul style="list-style:none;padding:0;margin:0;font-size:11px">${bullets}</ul>`;
    }

    async function loadTimeline(ticker) {
        const host = modal && modal.querySelector('#sp-timeline');
        if (!host) return;
        let res;
        try {
            const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/timeline`);
            res = await r.json();
        } catch (e) { res = null; }
        const events = res?.data || [];
        if (!events.length) { _setEmpty('sp-timeline', 'No events recorded for this stock yet.'); return; }
        const maxAlpha = Math.max(...events.map(e => e.alpha_score || 0), 1);
        host.innerHTML = events.slice(0, 8).map((ev, i) => {
            const sent = ev.sentiment || 'neutral';
            const sentColor = sent === 'bullish' ? '#2dd4aa' : sent === 'bearish' ? '#f26b6b' : '#a78bfa';
            const alpha = ev.alpha_score || 0;
            const alphaPct = Math.round((alpha / maxAlpha) * 100);
            const evType = (ev.event_type || '').replace(/_/g, ' ');
            const preds = ev.predictions || {};
            const isLast = i === Math.min(events.length, 8) - 1;
            let chips = '';
            for (const h of ['1D','3D','20D']) {
                const p = preds[h]; if (!p) continue;
                const ret = p.predicted || 0;
                const c = ret >= 0 ? '#2dd4aa' : '#f26b6b';
                const hit = p.hit === true || p.hit === 1 ? ' ?' : p.hit === false || p.hit === 0 ? ' ?' : '';
                chips += `<span style="font-size:7px;padding:1px 4px;border-radius:3px;background:${c}1f;color:${c};font-family:Geist Mono,monospace;margin-right:3px">${h}:${ret>=0?'+':''}${ret.toFixed(1)}%${hit}</span>`;
            }
            return `<div class="sp-tl-row">
                <div class="sp-tl-spine">
                    <div class="sp-tl-dot" style="background:${sentColor}"></div>
                    ${!isLast ? '<div class="sp-tl-line"></div>' : ''}
                </div>
                <div style="flex:1;min-width:0;padding-bottom:${isLast?'2':'10'}px;margin-left:4px">
                    <div style="display:flex;gap:8px;align-items:start">
                        <div style="flex:1;min-width:0">
                            <div style="font-size:11px;color:var(--text-primary);font-weight:600;line-height:1.35">${_esc((ev.headline || evType).slice(0, 90))}</div>
                            <div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin-top:2px">
                                <span style="font-size:8px;color:var(--text-tertiary)">${_fmtTime(ev.created_at)}</span>
                                <span style="font-size:8px;padding:1px 4px;border-radius:3px;background:${sentColor}1f;color:${sentColor};font-weight:700;text-transform:uppercase">${_esc(evType)}</span>
                                ${chips}
                            </div>
                        </div>
                        <div style="flex-shrink:0;width:52px;text-align:right">
                            <div style="font-size:11px;font-weight:800;font-family:Geist Mono,monospace;color:${_alphaColor(alpha)};line-height:1">${alpha.toFixed(0)}</div>
                            <div style="width:100%;height:3px;background:rgba(37,48,64,0.5);border-radius:2px;margin-top:3px;overflow:hidden">
                                <div style="width:${alphaPct}%;height:100%;background:${_alphaColor(alpha)};border-radius:2px"></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>`;
        }).join('');
    }

    async function loadVolume(ticker) {
        const host = modal && modal.querySelector('#sp-volume');
        if (!host) return;
        let res;
        try {
            const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/volume`);
            res = await r.json();
        } catch (e) { res = null; }
        if (!res?.success || !res.data?.series) { _setEmpty('sp-volume', 'No volume history available.'); return; }
        const a = res.data.analysis || {};
        const s = res.data.series || {};
        const surge = a.vol_surge_ratio || 0;
        const div = a.obv_divergence_flag;
        const divColor = div === 'bearish' ? '#f26b6b' : div === 'bullish' ? '#2dd4aa' : 'var(--text-secondary)';
        const surgeColor = surge >= 2.5 ? '#e6b84a' : 'var(--text-secondary)';
        host.innerHTML = `
            <div class="sp-mini-row">
                <div class="sp-mini-cell"><div class="lbl">Surge vs 20d</div><div class="val" style="color:${surgeColor}">${surge.toFixed(2)}×</div></div>
                <div class="sp-mini-cell"><div class="lbl">OBV divergence</div><div class="val" style="color:${divColor};font-size:12px;text-transform:uppercase">${_esc(div || 'none')}</div></div>
                <div class="sp-mini-cell"><div class="lbl">Pre-news</div><div class="val" style="color:${a.unexplained_volume?'#e6b84a':'var(--text-secondary)'};font-size:12px">${a.unexplained_volume?'unexplained':'explained'}</div></div>
            </div>
            <div class="sp-spark-wrap">
                <canvas id="sp-obv-spark" width="600" height="70"></canvas>
                <div class="lbl">OBV · 60d</div>
            </div>`;
        const cvs = modal.querySelector('#sp-obv-spark');
        const data = Array.isArray(s.obv)
            ? s.obv.filter(v => v != null && isFinite(Number(v))).map(Number)
            : [];
        if (!cvs || data.length < 2) return;
        const ctx = cvs.getContext('2d');
        const w = cvs.width, h = cvs.height, pad = 6;
        const min = Math.min(...data), max = Math.max(...data), range = (max - min) || 1;
        const line = divColor;
        ctx.clearRect(0, 0, w, h);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = line;
        ctx.beginPath();
        data.forEach((v, i) => {
            const x = pad + (i / (data.length - 1)) * (w - pad * 2);
            const y = h - pad - ((v - min) / range) * (h - pad * 2);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        ctx.lineTo(w - pad, h - pad); ctx.lineTo(pad, h - pad); ctx.closePath();
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, line + '33'); grad.addColorStop(1, 'transparent');
        ctx.fillStyle = grad; ctx.fill();
    }

    async function loadForensics(ticker) {
        const host = modal && modal.querySelector('#sp-forensics');
        if (!host) return;
        let res;
        try {
            const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/forensics-full`);
            res = await r.json();
        } catch (e) { res = null; }
        if (!res?.success) { _setEmpty('sp-forensics', 'Forensics unavailable.'); return; }
        const d = res.data || {};
        const pd = d.pump_dump || {};
        const recent = (d.recent_signals || []).filter(s => s.score != null);
        const latest = recent[0];
        const pumpColor = pd.band === 'likely_pump' ? '#f26b6b'
                       : pd.band === 'suspicious' ? '#e6b84a' : '#2dd4aa';
        const manipColor = latest?.band === 'likely_manipulated' ? '#f26b6b'
                         : latest?.band === 'unverified' ? '#e6b84a' : '#2dd4aa';
        const pumpScore = pd.pump_score || 0;
        const circ = 2 * Math.PI * 26;
        const dash = (pumpScore / 100) * circ;
        const evHtml = (pd.evidence || []).slice(0, 6).map(e => `
            <div class="sp-list-row">
                <div style="color:var(--text-primary)">${_esc(e.detail || '')}</div>
                <div style="color:var(--text-secondary);font-size:9px;margin-top:2px">+${e.score} pts · ${_esc((e.code||'').replace(/_/g,' '))}</div>
            </div>`).join('') || '<div class="sp-empty">no pump-pattern evidence</div>';
        const reasonsHtml = (latest?.reasons || []).map(r =>
            `<span class="sp-pill" style="background:var(--bear-dim);color:#f26b6b">${_esc(r.replace(/_/g,' '))}</span>`
        ).join('') || '<span style="color:var(--text-tertiary);font-size:10px">no manipulation flags</span>';
        const recentHtml = recent.slice(0, 5).map(s => {
            const c = s.band === 'likely_manipulated' ? '#f26b6b'
                    : s.band === 'unverified' ? '#e6b84a' : '#2dd4aa';
            return `<div class="sp-list-row">
                <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
                    <span style="color:var(--text-primary);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc((s.headline||'').slice(0,70))}</span>
                    <span style="color:${c};font-weight:800;font-size:10px">${s.score}</span>
                </div>
            </div>`;
        }).join('') || '<div class="sp-empty">no recent signals</div>';
        host.innerHTML = `
            <div class="sp-grid-2" style="margin-bottom:10px">
                <div class="sp-gauge-card">
                    <div style="font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Pump-Dump Risk</div>
                    <div style="position:relative;width:72px;height:72px;margin:4px auto">
                        <svg width="72" height="72" viewBox="0 0 72 72">
                            <circle cx="36" cy="36" r="26" fill="none" stroke="rgba(141,148,168,0.2)" stroke-width="6"/>
                            <circle cx="36" cy="36" r="26" fill="none"
                                stroke="${pumpColor}" stroke-width="6" stroke-linecap="round"
                                stroke-dasharray="${circ}" stroke-dashoffset="${circ - dash}"
                                transform="rotate(-90 36 36)"/>
                            <text x="36" y="41" text-anchor="middle"
                                font-family="Geist Mono,monospace" font-size="18" font-weight="800"
                                fill="${pumpColor}">${pumpScore}</text>
                        </svg>
                    </div>
                    <div style="font-size:10px;color:${pumpColor};text-transform:uppercase;font-weight:800">${_esc(pd.band || 'unknown')}</div>
                </div>
                <div class="sp-gauge-card" style="text-align:left">
                    <div style="font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.1em">Latest article</div>
                    <div style="font-size:24px;font-weight:800;color:${manipColor};font-family:'Geist Mono',monospace;margin:4px 0">${latest?.score != null ? latest.score : '—'}</div>
                    <div style="font-size:10px;color:${manipColor};text-transform:uppercase;font-weight:700;margin-bottom:4px">${_esc(latest?.band?.replace(/_/g,' ') || 'no recent signal')}</div>
                    <div>${reasonsHtml}</div>
                </div>
            </div>
            <div style="margin-bottom:8px">
                <div style="font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Evidence</div>
                <div class="sp-list">${evHtml}</div>
            </div>
            <div>
                <div style="font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Recent signals · score</div>
                <div class="sp-list">${recentHtml}</div>
            </div>`;
    }

    let _chartLoadPromise = null;
    function _ensureChartJs() {
        if (window.Chart) return Promise.resolve(true);
        if (_chartLoadPromise) return _chartLoadPromise;
        _chartLoadPromise = new Promise((resolve) => {
            const s = document.createElement('script');
            s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js';
            s.onload = () => resolve(true);
            s.onerror = () => resolve(false);
            document.head.appendChild(s);
        });
        return _chartLoadPromise;
    }

    async function loadPromoter(ticker) {
        const host = modal && modal.querySelector('#sp-promoter');
        if (!host) return;
        const chartReady = _ensureChartJs();
        let res;
        try {
            const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}/promoter/insights`);
            res = await r.json();
        } catch (e) { res = null; }
        // ALWAYS render the structure — fill missing fields with '—' rather
        // than blanking the section. Empty values were a constant complaint.
        const ok = !!(res && res.success);
        const d  = (res && res.data) || {};
        const cur = d.current || {};
        const quarters = d.quarters || [];
        const flags = d.red_flags || [];
        const insights = d.insights || [];
        const sebi = d.sebi_disclosures || [];
        const stale = !ok || !d.has_data;

        const statBox = (label, value, color) => `
            <div class="sp-mini-cell">
                <div class="lbl">${label}</div>
                <div class="val" style="color:${value == null ? 'var(--text-tertiary)' : color};font-size:13px">${value == null ? '—' : value.toFixed(2) + '%'}</div>
            </div>`;
        const flagBadge = (f) => {
            const p = f.severity === 'critical' ? { bg: 'var(--bear-dim)', fg: '#f26b6b' }
                    : f.severity === 'warn'    ? { bg: '#3a3214', fg: '#e6b84a' }
                                               : { bg: 'var(--info-dim)', fg: '#8eb4e0' };
            return `<span class="sp-pill" title="${_esc(f.detail || '')}" style="background:${p.bg};color:${p.fg}">${_esc(f.label || '')}</span>`;
        };
        const sebiHtml = (sebi.slice(0, 4).map(s => {
            const c = s.transaction_type === 'buy' ? '#2dd4aa' : s.transaction_type === 'sell' ? '#f26b6b' : '#a78bfa';
            return `<div class="sp-list-row">
                <div style="color:var(--text-secondary);font-size:9px">${_esc(s.transaction_date || '')} · ${_esc(s.disclosure_type || '')}</div>
                <div style="color:${c}">${_esc(s.transaction_type || '')} ${s.quantity ? '· ' + Number(s.quantity).toLocaleString() + ' sh' : ''} ${_esc(s.person_name || '')}</div>
            </div>`;
        }).join('')) || '<div class="sp-empty">no SEBI disclosures in window</div>';

        const insightsBlock = insights.length
            ? `<div style="padding:8px 10px;background:rgba(13,17,24,0.7);border:1px solid rgba(37,48,64,0.4);border-radius:8px;margin-bottom:10px;font-size:11px;color:var(--text-primary);line-height:1.6">
                ${insights.map(i => `<div>• ${_esc(i)}</div>`).join('')}
              </div>`
            : `<div style="padding:8px 10px;background:rgba(13,17,24,0.55);border:1px dashed rgba(141,148,168,0.18);border-radius:8px;margin-bottom:10px;font-size:11px;color:var(--text-secondary);line-height:1.6">
                <em>No specific promoter insights this quarter — pledge/buyback/exit triggers will populate here once filed.</em>
              </div>`;
        const flagsBlock = flags.length
            ? `<div style="margin-bottom:10px">${flags.map(flagBadge).join('')}</div>`
            : `<div style="margin-bottom:10px"><span class="sp-pill" style="background:rgba(45,212,170,0.10);color:#2dd4aa">No red flags</span></div>`;
        const staleNote = stale
            ? `<div style="padding:8px 10px;margin-bottom:10px;background:rgba(230,184,74,0.06);border:1px solid rgba(230,184,74,0.22);border-radius:8px;font-size:11px;color:#e6b84a;line-height:1.5;">
                 <strong>Quarterly snapshot pending.</strong> Promoter/FII/DII numbers refresh after every shareholding-pattern filing — usually within 10 days of quarter end.
               </div>`
            : '';
        host.innerHTML = `
            ${staleNote}
            ${insightsBlock}
            ${flagsBlock}
            <div class="sp-mini-row" style="grid-template-columns:repeat(4,1fr)">
                ${statBox('Promoter', cur.promoter_pct, '#8eb4e0')}
                ${statBox('Pledge', cur.promoter_pledge_pct, (cur.promoter_pledge_pct || 0) > 20 ? '#f26b6b' : 'var(--text-secondary)')}
                ${statBox('FII', cur.fii_pct, '#2dd4aa')}
                ${statBox('DII', cur.dii_pct, '#e6b84a')}
            </div>
            <div style="display:grid;grid-template-columns:1fr 1.4fr;gap:8px;margin-bottom:8px">
                <div class="sp-gauge-card" style="padding:8px">
                    <div style="font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Breakdown</div>
                    <canvas id="sp-promoter-donut" width="180" height="140" style="width:100%;max-height:140px"></canvas>
                    ${stale ? '<div style="font-size:10px;color:var(--text-tertiary);margin-top:6px;text-align:center;">awaiting filing</div>' : ''}
                </div>
                <div class="sp-gauge-card" style="padding:8px">
                    <div style="font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">8-quarter history</div>
                    <canvas id="sp-promoter-history" width="280" height="140" style="width:100%;max-height:140px"></canvas>
                    ${stale ? '<div style="font-size:10px;color:var(--text-tertiary);margin-top:6px;text-align:center;">history will appear once 2+ quarters are filed</div>' : ''}
                </div>
            </div>
            <div>
                <div style="font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">SEBI disclosures</div>
                <div class="sp-list">${sebiHtml}</div>
            </div>`;
        const chartOk = await chartReady;
        if (!chartOk || !window.Chart) return;
        if (stale) return; // no data ? skip chart drawing
        const donut = modal.querySelector('#sp-promoter-donut');
        if (donut) {
            const others = Math.max(0, 100 - (cur.promoter_pct || 0) - (cur.fii_pct || 0) - (cur.dii_pct || 0));
            new Chart(donut, {
                type: 'doughnut',
                data: { labels: ['Promoter','FII','DII','Public/Other'], datasets: [{
                    data: [cur.promoter_pct || 0, cur.fii_pct || 0, cur.dii_pct || 0, others],
                    backgroundColor: ['#8eb4e0','#2dd4aa','#e6b84a','rgba(141,148,168,0.4)'],
                    borderColor: 'var(--surface-0)', borderWidth: 2,
                }]},
                options: { responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { position:'bottom', labels: { color:'var(--text-primary)', font:{size:9}, boxWidth: 9 } } } },
            });
        }
        const hist = modal.querySelector('#sp-promoter-history');
        if (hist && quarters.length) {
            new Chart(hist, {
                type: 'line',
                data: { labels: quarters.map(q => (q.quarter_end || '').slice(0,7)),
                    datasets: [
                        { label: 'Promoter %', data: quarters.map(q => q.promoter_pct), borderColor:'#8eb4e0', backgroundColor:'#8eb4e022', tension: 0.25, borderWidth: 2, pointRadius: 2 },
                        { label: 'Pledge %',   data: quarters.map(q => q.promoter_pledge_pct), borderColor:'#f26b6b', backgroundColor:'#f26b6b22', tension: 0.25, borderWidth: 2, pointRadius: 2 },
                        { label: 'FII %',      data: quarters.map(q => q.fii_pct), borderColor:'#2dd4aa', backgroundColor:'#2dd4aa22', tension: 0.25, borderWidth: 1.5, pointRadius: 2 },
                    ] },
                options: { responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { labels: { color:'var(--text-primary)', font:{size:9}, boxWidth: 9 } } },
                    scales: { x: { ticks: { color:'var(--text-secondary)', font:{size:8} }, grid: { color:'rgba(37,48,64,0.4)' } },
                              y: { ticks: { color:'var(--text-secondary)', font:{size:8}, callback: v => v+'%' }, grid: { color:'rgba(37,48,64,0.4)' } } } },
            });
        }
    }

    function close() {
        if (!modal) return;
        modal.classList.remove('open');
        document.documentElement.style.overflow = '';
        if (abortCtl) abortCtl.abort();
        currentTicker = null;
    }

    // Global click delegation. Listening on `window` in capture phase plus
    // stopImmediatePropagation guarantees we beat every other delegated
    // handler — notably event-card.js, which has its own document-level
    // `.ec-ticker` click that wants to navigate straight to stock.html.
    //
    // Triggers on any of: [data-ticker], .ec-ticker, .stock-pop-trigger.
    // Bypass: data-ticker-link="full" or data-no-popup, or modifier-key click
    // (cmd/ctrl/shift/middle), so power users can still open in a new tab.
    function _resolveTickerEl(e) {
        const t = e.target;
        if (!t || !t.closest) return null;
        return t.closest('[data-ticker], [data-stock-ticker], .ec-ticker, .stock-pop-trigger');
    }
    function _onTickerClick(e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
        const el = _resolveTickerEl(e);
        if (!el) return;
        if (el.dataset && el.dataset.tickerLink === 'full') return;
        if (el.hasAttribute && el.hasAttribute('data-no-popup')) return;
        let tk = (el.getAttribute && (el.getAttribute('data-ticker') ||
                                        el.getAttribute('data-stock-ticker'))) || '';
        if (!tk && el.classList && el.classList.contains('ec-ticker')) {
            tk = (el.textContent || '').trim();
        }
        tk = String(tk || '').trim();
        if (!tk || tk === '—' || tk === 'None') return;
        // Skip on the stock detail page itself — clicking a peer ticker
        // should refresh that page, not stack a modal over the same view.
        if (/\/stock\.html(\?|$|#)/.test(location.pathname + location.search)) return;
        e.preventDefault();
        e.stopPropagation();
        if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
        try { console.debug('[StockPopup] open', tk); } catch (_) {}
        StockPopup.open(tk);
    }
    // Capture phase on window — beats every other handler, including any
    // bubble-phase delegated handler on document.
    window.addEventListener('click', _onTickerClick, true);
    document.addEventListener('click', _onTickerClick, true);  // belt-and-braces for older browsers

    // Escape closes
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && modal && modal.classList.contains('open')) close();
    });

    window.StockPopup = { open, close };
})();
