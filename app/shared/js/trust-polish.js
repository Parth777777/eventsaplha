/* trust-polish.js — site-wide trust + transparency enhancer.
 *
 * Two opt-in patterns; both safe to add to any HTML element:
 *
 *   data-metric="<methodology-anchor>"
 *     Appends "Why?" link → methodology.html#<anchor>
 *     Example:
 *       <span data-metric="alpha-score">Alpha 78/100</span>
 *
 *   data-updated="<ISO timestamp>"
 *     Renders a small relative "Updated 2m ago" footer beneath the element,
 *     refreshed every 30s. Uses Intl.RelativeTimeFormat where available.
 *     Example:
 *       <div data-updated="2026-05-18T10:34:00Z">…</div>
 *
 * Idempotent. Watches for new nodes added by widgets.js / charts.js.
 * Skips landing/login/signup/onboarding pages.
 */
(function () {
  'use strict';
  if (window.__aeTrustPolishMounted) return;
  window.__aeTrustPolishMounted = true;

  const SKIP = /(^|\/)(landing|login|signup|onboarding)\.html/i;
  if (SKIP.test(location.pathname)) return;

  const rtf = (typeof Intl !== 'undefined' && Intl.RelativeTimeFormat)
    ? new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
    : null;

  function relTime(iso) {
    const t = Date.parse(iso);
    if (!t || Number.isNaN(t)) return iso;
    const delta = (t - Date.now()) / 1000;  // seconds
    const abs = Math.abs(delta);
    let value, unit;
    if (abs < 60)         { value = Math.round(delta);        unit = 'second'; }
    else if (abs < 3600)  { value = Math.round(delta / 60);   unit = 'minute'; }
    else if (abs < 86400) { value = Math.round(delta / 3600); unit = 'hour';   }
    else                  { value = Math.round(delta / 86400); unit = 'day';   }
    return rtf ? rtf.format(value, unit) : (value + ' ' + unit + 's');
  }

  function whyHref(metric) {
    const slug = String(metric || '').toLowerCase().replace(/[^a-z0-9-]/g, '-');
    return 'methodology.html#' + slug;
  }

  function addWhyLinks(root) {
    (root || document).querySelectorAll('[data-metric]').forEach((el) => {
      if (el.dataset.aeWhyMounted) return;
      el.dataset.aeWhyMounted = '1';
      const a = document.createElement('a');
      a.href = whyHref(el.dataset.metric);
      a.className = 'ae-why';
      a.textContent = 'Why?';
      a.setAttribute('aria-label', 'Methodology for ' + el.dataset.metric);
      a.style.cssText = 'margin-left:6px;font-size:11px;color:var(--link,var(--text-tertiary));text-decoration:underline;text-underline-offset:2px;font-weight:500;';
      el.appendChild(a);
    });
  }

  function refreshTimestamps(root) {
    (root || document).querySelectorAll('[data-updated]').forEach((el) => {
      if (!el.dataset.aeTsMounted) {
        el.dataset.aeTsMounted = '1';
        const span = document.createElement('span');
        span.className = 'ae-ts';
        span.style.cssText = 'display:inline-block;margin-top:6px;font-size:10px;color:var(--text-tertiary);letter-spacing:0.04em;';
        el.appendChild(span);
      }
      const span = el.querySelector('.ae-ts');
      if (span) span.textContent = '· Updated ' + relTime(el.dataset.updated);
    });
  }

  function tick() {
    try { addWhyLinks(document); } catch (_) {}
    try { refreshTimestamps(document); } catch (_) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tick);
  } else {
    tick();
  }
  setInterval(tick, 30000);

  // Watch for dynamically added elements
  try {
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        m.addedNodes && m.addedNodes.forEach((n) => {
          if (n.nodeType !== 1) return;
          if (n.matches && (n.matches('[data-metric]') || n.matches('[data-updated]'))) {
            addWhyLinks(n.parentNode || document);
            refreshTimestamps(n.parentNode || document);
          } else if (n.querySelectorAll) {
            addWhyLinks(n);
            refreshTimestamps(n);
          }
        });
      }
    }).observe(document.body, { childList: true, subtree: true });
  } catch (_) {}
})();
