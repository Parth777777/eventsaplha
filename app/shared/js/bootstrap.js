/* bootstrap.js — site-wide init.
 * Loaded after app.js on every page. Side effects only.
 *
 * Responsibilities:
 *   1. Register service worker (PWA + push)
 *   2. Inject the SEBI compliance footer
 *   3. Telemetry beacon helper (window.Telemetry)
 *   4. Onboarding gate (redirect first-time users to /onboarding.html)
 *   5. Inject cross-page section tabs (premium top strip)
 */
(function () {
  // ════════════════════════════════════════════════════════════════════
  // POPUP KILL-SWITCH 2026-05-18 — runs BEFORE any other init so blank
  // toasts never get a chance to render. User feedback: "remove that
  // feature in general".
  //   1. Sweep any existing popup DOM nodes immediately
  //   2. MutationObserver removes new ones the moment they're inserted
  //   3. Stub window.toast as a no-op so nothing can mount via JS
  //   4. Unregister ALL service workers — cached stale code that mounts
  //      these popups can no longer be served on subsequent navs.
  // ════════════════════════════════════════════════════════════════════
  try {
    // Specific known offenders — kill by ID/class first.
    const KILL_SELECTORS = 'ae-toast,#ae-toast-container,.tw-break-overlay,.tw-break-card,#tw-market-clock,#tw-mc-styles,#eaInstallPrompt,#tw-clock-banner,#tw-banner,#tw-top-strip';
    // Allowed direct body children — everything else is suspicious.
    const KEEP_TAGS = new Set(['HEADER','MAIN','FOOTER','ASIDE','SCRIPT','LINK','STYLE','NOSCRIPT','META','TITLE','BUTTON']);
    const KEEP_CLASS_RE = /(theme-switch|sidebar-hamburger|sidebar-toggle|main-content|ea-footer)/;
    // Whitelist: legitimate user-triggered modals (not auto-mounted overlays).
    // The popup-killer used to nuke these one tick after they opened, so the
    // Analyze button looked dead. Anything in here is exempt from sweep().
    const KEEP_IDS = new Set([
        'edgeModalBackdrop',      // neome-style analyzer (index.html)
        'edgeAnalyzerBackdrop',   // legacy edge-analyzer.js modal
        'eventModalBackdrop',     // stock-popup detail
        'twStockPopup',           // shared stock popup
        'ae-chat-launcher',       // Planning Assistant floating button (user-triggered)
        'ae-chat-panel',          // Planning Assistant side panel (user-triggered)
    ]);

    function isPopupShaped(el) {
      // Hard whitelist for explicit, user-triggered analyzer/stock-detail modals.
      if (el && el.id && KEEP_IDS.has(el.id)) return false;
      try {
        const cs = getComputedStyle(el);
        if (cs.position !== 'fixed' && cs.position !== 'sticky') return false;
        // Has at least one close-style button inside?
        const hasClose = !!el.querySelector(
          'button[aria-label*="ismiss" i], button[aria-label*="lose" i], ' +
          '.close, .tw-close, [data-action="dismiss"]'
        )
        // Heuristic: contains literal × in textContent
        || /[×✕]/.test(el.textContent || '');
        // Tiny content area (< 80 chars of visible text) + close button = popup
        const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
        return hasClose && txt.length < 120;
      } catch (_) { return false; }
    }

    const sweep = (root) => {
      try {
        (root || document).querySelectorAll(KILL_SELECTORS)
          .forEach((n) => {
            try { console.warn('[popup-kill] removed by id/class:', n.id || n.className, n); n.remove(); } catch (_) {}
          });
        // Plus generic body-child popup-shaped scan
        const bodyKids = (document.body && Array.from(document.body.children)) || [];
        for (const el of bodyKids) {
          if (KEEP_TAGS.has(el.tagName)) continue;
          if (KEEP_CLASS_RE.test(el.className || '')) continue;
          if (el.id && KEEP_IDS.has(el.id)) continue;  // whitelisted modals
          if (isPopupShaped(el)) {
            console.warn('[popup-kill] removed by shape:', el.tagName, el.id || el.className, el);
            try { el.remove(); } catch (_) {}
          }
        }
      } catch (_) {}
    };
    sweep(document);
    // Run again after layout settles so getComputedStyle results are valid
    setTimeout(() => sweep(document), 0);
    setTimeout(() => sweep(document), 500);
    setTimeout(() => sweep(document), 2000);

    window.toast = function () { return null; };
    try {
      new MutationObserver((mutations) => {
        for (const m of mutations) {
          m.addedNodes && m.addedNodes.forEach((n) => {
            if (n.nodeType !== 1) return;
            const tag = (n.tagName || '').toLowerCase();
            const id = n.id || '';
            const cls = n.classList;
            if (tag === 'ae-toast'
                || id === 'ae-toast-container'
                || id === 'tw-market-clock'
                || id === 'tw-mc-styles'
                || (cls && (cls.contains('tw-break-overlay')
                         || cls.contains('tw-break-card')))) {
              try { console.warn('[popup-kill] mutation removed:', tag, id); n.remove(); } catch (_) {}
            }
            // Defer popup-shape check to next tick so the element has computed styles
            if (n.parentNode === document.body && !KEEP_TAGS.has(n.tagName)) {
              // Whitelisted modals (user-triggered analyzer / stock popup) bypass.
              if (!(n.id && KEEP_IDS.has(n.id))) {
                setTimeout(() => {
                  if (n.isConnected && isPopupShaped(n)) {
                    console.warn('[popup-kill] mutation shape removed:', n.tagName, n.id || n.className);
                    try { n.remove(); } catch (_) {}
                  }
                }, 0);
              }
            }
            if (n.querySelectorAll) sweep(n);
          });
        }
      }).observe(document.documentElement, { childList: true, subtree: true });
    } catch (_) {}
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.getRegistrations().then((regs) => {
        regs.forEach((r) => { try { r.unregister(); } catch (_) {} });
      }).catch(() => {});
    }
  } catch (_) {}

  // ---- -2. Premium REDESIGN layer — LAST stylesheet to load so it wins
  // specificity over every legacy hex-encoded dark style. Forces light mode
  // to look like Stripe/Linear: pure white cards, soft shadows, confident
  // black accents, generous whitespace, refined typography.
  try {
    if (!document.querySelector('link[data-tw-redesign]')) {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = './shared/css/redesign.css';
      l.setAttribute('data-tw-redesign', '1');
      // Append at end of <head> so it lands AFTER inline page <style> blocks
      document.head.appendChild(l);
    }
  } catch (_) {}

  // ---- -1. Sitewide compliance / disclosures stylesheet — consumes
  // tokens.css. Hosts the SEBI footer styles + per-card disclaimer chips
  // so widget JS no longer needs to inline dark hex values.
  try {
    if (!document.querySelector('link[data-tw-disclosures]')) {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = './shared/css/disclosures.css';
      l.setAttribute('data-tw-disclosures', '1');
      document.head.appendChild(l);
    }
  } catch (_) {}

  // ---- -0.8. Components layer — canonical <ae-*> custom elements and
  // shared a11y primitives (focus ring, skip-link, mobile bottom nav).
  // Loaded after redesign.css and disclosures.css so it wins on equal
  // specificity for its own scoped selectors.
  try {
    if (!document.querySelector('link[data-tw-components]')) {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = './shared/css/components.css';
      l.setAttribute('data-tw-components', '1');
      document.head.appendChild(l);
    }
  } catch (_) {}

  // ---- -0.78. Chat launcher styles — floating button + slide-in panel.
  // Loaded sitewide; the JS auto-mounts only outside landing/auth pages.
  try {
    if (!document.querySelector('link[data-tw-chat-css]')) {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = './shared/css/chat-launcher.css';
      l.setAttribute('data-tw-chat-css', '1');
      document.head.appendChild(l);
    }
  } catch (_) {}

  // ---- -0.7. Custom element registrations (ae-card, ae-button,
  // ae-skeleton, ae-empty-state, ae-error-boundary, ae-signal-row,
  // ae-toast, ae-tab-bar). Also exposes window.toast(...).
  try {
    if (!document.querySelector('script[data-tw-ae-components]')) {
      const s = document.createElement('script');
      s.src = './shared/js/components/ae-components.js';
      s.defer = false;
      s.setAttribute('data-tw-ae-components', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- -0.6. Mobile bottom-nav — auto-mounts a 5-item tab bar on
  // viewports ≤768px. Critical: without this, mobile users cannot
  // navigate past the page they land on (sidebar is `hidden md:flex`).
  try {
    if (!document.querySelector('script[data-tw-ae-bottom-nav]')) {
      const s = document.createElement('script');
      s.src = './shared/js/ae-bottom-nav.js';
      s.defer = true;
      s.setAttribute('data-tw-ae-bottom-nav', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- -0.55. Canonical sidebar — replaces per-page hardcoded 15+ link
  // sidebars with the consolidated 8-entry IA. Runs after DOMContentLoaded
  // so the existing <aside class="sidebar"> is in the DOM to receive it.
  try {
    if (!document.querySelector('script[data-tw-sidebar]')) {
      const s = document.createElement('script');
      s.src = './shared/js/sidebar.js';
      s.defer = true;
      s.setAttribute('data-tw-sidebar', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- -0.52. A11y enhancer — auto-adds aria-labels to icon-only buttons,
  // landmark roles, ESC-to-close on modals. Idempotent + observes new
  // buttons added later by widgets/charts.
  try {
    if (!document.querySelector('script[data-tw-a11y]')) {
      const s = document.createElement('script');
      s.src = './shared/js/a11y.js';
      s.defer = true;
      s.setAttribute('data-tw-a11y', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- -0.50. Trust polish — opt-in via [data-metric] / [data-updated].
  // Appends "Why?" → methodology.html#<slug> links beside flagged metrics
  // and renders relative timestamps that auto-refresh every 30s.
  try {
    if (!document.querySelector('script[data-tw-trust]')) {
      const s = document.createElement('script');
      s.src = './shared/js/trust-polish.js';
      s.defer = true;
      s.setAttribute('data-tw-trust', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- -0.48. TickerWave Planning Assistant — dual-mode.
  //   * Inline card if the page has `<section id="ae-chat-host" data-ticker="X">`.
  //   * Otherwise mounts a user-triggered floating launcher + side panel.
  // The launcher button + panel are whitelisted in KEEP_IDS above so the
  // popup-killer doesn't nuke them. The script itself decides whether to
  // skip landing/login/signup/onboarding/disclosures pages.
  try {
    if (!document.querySelector('script[data-tw-chat]')) {
      const s = document.createElement('script');
      s.src = './shared/js/chat-assistant.js';
      s.defer = true;
      s.setAttribute('data-tw-chat', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- -0.46. Inline equity research — only loads if a host is present.
  // Pages opt in with `<section id="ae-research-host" data-ticker="X"></section>`.
  // (Research view is unhooked from default UI as of 2026-05-18, but the
  // loader stays in case a page explicitly mounts the host.)
  if (document.querySelector('#ae-research-host, [data-ae-research]')) {
    try {
      if (!document.querySelector('script[data-tw-research]')) {
        const s = document.createElement('script');
        s.src = './shared/js/equity-research.js';
        s.defer = true;
        s.setAttribute('data-tw-research', '1');
        document.head.appendChild(s);
      }
    } catch (_) {}
  }

  // ---- -0.44. Broker-app-quality stock detail (Financials charts +
  // Shareholding pie/history + wire-style categorized News). Auto-mounts
  // on any page with `.tab-pane[data-pane="financials"]` (i.e. stock.html)
  // OR with `<section id="ae-stock-pro" data-ticker="X">`. Also used by
  // stock-popup.js via the exposed window.StockPro API.
  if (document.querySelector('.tab-pane[data-pane="financials"], #ae-stock-pro, [data-ae-stock-pro]')) {
    try {
      if (!document.querySelector('script[data-tw-stock-pro]')) {
        const s = document.createElement('script');
        s.src = './shared/js/stock-pro.js';
        s.defer = true;
        s.setAttribute('data-tw-stock-pro', '1');
        document.head.appendChild(s);
      }
    } catch (_) {}
  }

  // ---- -0.5. Theme tokens JS bridge — exports window.ThemeTokens for
  // widgets that paint on canvas / inject inline styles. Must load before
  // any widget that consumes it (edge-analyzer, advanced-chart, etc.).
  try {
    if (!document.querySelector('script[data-tw-theme-tokens]')) {
      const s = document.createElement('script');
      s.src = './shared/js/theme-tokens.js';
      s.defer = false;
      s.setAttribute('data-tw-theme-tokens', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- 0. Load shared cross-page section tabs.
  // Loaded as a separate script (rather than inlined) so it stays cacheable
  // and easy to disable. Best-effort: failure does not block any other init.
  try {
    if (!document.querySelector('script[data-tw-section-tabs]')) {
      const s = document.createElement('script');
      s.src = './shared/js/section-tabs.js';
      s.defer = true;
      s.setAttribute('data-tw-section-tabs', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- 0b. Market clock banner — DISABLED 2026-05-18. This was the
  // blank `—` + × strip the user kept seeing. /api/market/clock was
  // returning empty payloads which render as just the close button +
  // an em-dash placeholder. Re-enable once the API is hardened.
  // try {
  //   if (!document.querySelector('script[data-tw-clock]')) {
  //     const s = document.createElement('script');
  //     s.src = './shared/js/market-clock.js';
  //     s.defer = true;
  //     s.setAttribute('data-tw-clock', '1');
  //     document.head.appendChild(s);
  //   }
  // } catch (_) {}

  // ---- 0c. Breaking-news alert — DISABLED 2026-05-18 due to empty-toast
  // bug (rendered a blank top-strip with only `—` + ×). Re-enable once
  // breaking-alert.js guards against malformed signal payloads.
  // Original block kept for easy revert:
  // try {
  //   if (!document.querySelector('script[data-tw-breaking]')) {
  //     const s = document.createElement('script');
  //     s.src = './shared/js/breaking-alert.js';
  //     s.defer = true;
  //     s.setAttribute('data-tw-breaking', '1');
  //     document.head.appendChild(s);
  //   }
  // } catch (_) {}

  // ---- 0d. Edge analyzer — site-wide [data-edge-analyze] click handler
  // that opens the fundamental analysis modal on any page.
  try {
    if (!document.querySelector('script[data-tw-edge-analyzer]')) {
      const s = document.createElement('script');
      s.src = './shared/js/edge-analyzer.js';
      s.defer = true;
      s.setAttribute('data-tw-edge-analyzer', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- 0e. SEBI compliance modal — auto-mounts on first load, gated by
  // a versioned localStorage ack. Skips disclosures/login/signup pages
  // internally; safe to include on every page.
  try {
    if (!document.querySelector('script[data-tw-compliance-modal]')) {
      const s = document.createElement('script');
      s.src = './shared/js/compliance-modal.js';
      s.defer = true;
      s.setAttribute('data-tw-compliance-modal', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- 0f. Sidebar toggle — auto-mounts a desktop collapse button and a
  // mobile hamburger trigger so the sidebar can actually be dismissed.
  // Fixes user feedback: "sidebar is not closing up".
  try {
    if (!document.querySelector('script[data-tw-sidebar-toggle]')) {
      const s = document.createElement('script');
      s.src = './shared/js/sidebar-toggle.js';
      s.defer = true;
      s.setAttribute('data-tw-sidebar-toggle', '1');
      document.head.appendChild(s);
    }
  } catch (_) {}

  // ---- 1. Service worker — DISABLED 2026-05-18 (popup-cache bug).
  // The SW was serving stale bootstrap.js/ae-components.js that kept
  // mounting blank popups even after source fixes shipped. Re-enable
  // after one full week of clean production deploys.
  if (false && 'serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    });
    // When a new SW activates and posts 'sw-activated', reload once so the
    // page swaps stale JS/CSS for the freshly-cached shell. The guard
    // prevents reload loops if multiple activations fire in one session.
    let _reloaded = false;
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e && e.data && e.data.type === 'sw-activated' && !_reloaded) {
        _reloaded = true;
        try { console.log('[Tickwave] new SW activated → reloading for fresh shell', e.data.cache); } catch (_) {}
        location.reload();
      }
    });
  }

  // ---- 2. SEBI / compliance footer — styling lives in disclosures.css
  // (consumes tokens.css). Long copy is SEBI-defensible and retail-readable.
  function mountFooter() {
    if (document.getElementById('eaFooter')) return;
    const f = document.createElement('footer');
    f.id = 'eaFooter';
    f.className = 'ea-footer';
    f.innerHTML = `
      <div class="ea-footer__row">
        <span class="material-symbols-outlined ea-footer__icon">gavel</span>
        <div class="ea-footer__body">
          <b class="ea-footer__title">Informational only — not investment advice.</b>
          AlphaEvent / Tickwave provides quantitative research and informational signals derived from public Indian market data (NSE/BSE filings, SEBI disclosures, RBI press, financial news). We are not a SEBI-registered Investment Adviser or Research Analyst. Nothing here is a recommendation or solicitation. Past performance does not guarantee future results.
          <span class="ea-footer__sep">·</span>
          <a class="ea-footer__link" href="disclosures.html">Full disclosures</a>
          <span class="ea-footer__sep">·</span>
          <a class="ea-footer__link" href="trust.html">Track record</a>
          <span class="ea-footer__sep">·</span>
          <a class="ea-footer__link" href="https://scores.sebi.gov.in/" target="_blank" rel="noopener">SEBI SCORES</a>
        </div>
      </div>`;
    (document.querySelector('main') || document.body).appendChild(f);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountFooter);
  } else {
    mountFooter();
  }

  // ---- 2b. Skip-to-content link — keyboard users land on this first
  // and can jump past the sidebar/header to the page's <main>. Idempotent.
  function mountSkipLink() {
    if (document.querySelector('.skip-link')) return;
    const main = document.querySelector('main, [role="main"]');
    if (!main) return;
    if (!main.id) main.id = 'main';
    const a = document.createElement('a');
    a.href = '#' + main.id;
    a.className = 'skip-link';
    a.textContent = 'Skip to content';
    document.body.insertBefore(a, document.body.firstChild);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountSkipLink);
  } else {
    mountSkipLink();
  }

  // ---- 3. Telemetry helper (POST best-effort, never blocks UI)
  window.Telemetry = {
    record: (kind, target, metadata) => {
      try {
        const body = JSON.stringify({ kind, target, metadata });
        if (navigator.sendBeacon) {
          const blob = new Blob([body], { type: 'application/json' });
          navigator.sendBeacon('/api/telemetry', blob);
        } else {
          fetch('/api/telemetry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true });
        }
      } catch (e) {}
    },
  };
  // Auto-track page view
  try { window.Telemetry.record('view', location.pathname); } catch (e) {}

  // Auto-track outbound link clicks on event cards (data-ticker attr)
  document.addEventListener('click', (e) => {
    const tk = e.target.closest('[data-ticker]');
    if (tk) {
      window.Telemetry.record('click', tk.getAttribute('data-ticker'), { page: location.pathname });
    }
  }, { capture: true });

  // ---- 4. Onboarding gate (DISABLED 2026-05-18 due to popup-spam feedback)
  // The bottom-right "Welcome to Tickwave" banner fired on first session and
  // contributed to popup overload. Disable until the IA-redesign onboarding
  // flow lands (Track 2 Phase 4). Set `tw_force_onboarding=1` in console to
  // re-enable for testing.
  const path = location.pathname;
  const skip = /onboarding|login|signup|trust/.test(path)
    || !sessionStorage.getItem('tw_force_onboarding');
  if (!skip && !sessionStorage.getItem('ea_onboarded_check')) {
    sessionStorage.setItem('ea_onboarded_check', '1');
    fetch('/api/onboarding/state').then((r) => r.json()).then((j) => {
      if (j && j.success && j.onboarded === false && !localStorage.getItem('ea_skipped_onboarding')) {
        // First-time visitor — gentle nudge
        const banner = document.createElement('div');
        banner.style.cssText = 'position:fixed; bottom:16px; right:16px; max-width:340px; background:var(--surface-1); border:1px solid var(--accent); border-radius:12px; padding:14px 16px; box-shadow:var(--shadow-lg); z-index:9999; font-size:12px; color:var(--text-primary); font-family:var(--font-ui, "Inter", sans-serif);';
        banner.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:12px;">
            <div>
              <div style="font-weight:800; color:var(--accent); margin-bottom:4px;">Welcome to Tickwave</div>
              <div style="color:var(--text-secondary);">Pick 5 stocks and you're set in 60 seconds.</div>
              <div style="margin-top:10px; display:flex; gap:8px;">
                <a href="onboarding.html" style="background:var(--accent);color:#fff;padding:6px 14px;border-radius:6px;font-weight:700;text-decoration:none;font-size:11px;">Get started →</a>
                <button onclick="localStorage.setItem('ea_skipped_onboarding','1');this.closest('div[style]').parentElement.remove();" style="background:transparent;color:var(--text-tertiary);border:0;cursor:pointer;font-size:11px;">Skip</button>
              </div>
            </div>
            <button onclick="this.closest('div[style*=fixed]').remove();" style="background:transparent;color:var(--text-tertiary);border:0;cursor:pointer;font-size:18px;line-height:1;">×</button>
          </div>`;
        document.body.appendChild(banner);
      }
    }).catch(() => {});
  }

  // PWA: link the manifest if absent (avoids editing every page)
  if (!document.querySelector('link[rel="manifest"]')) {
    const l = document.createElement('link');
    l.rel = 'manifest'; l.href = '/manifest.json';
    document.head.appendChild(l);
  }
  // Add theme-color meta if missing (PWA requirement for status-bar tint)
  if (!document.querySelector('meta[name="theme-color"]')) {
    const m = document.createElement('meta');
    m.name = 'theme-color'; m.content = '#080c12';
    document.head.appendChild(m);
  }
})();
