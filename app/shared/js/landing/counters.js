/* counters.js — animates [data-counter] numbers when they enter viewport.
 * Also wires the live waitlist totals into nav/hero and the founder-tier
 * progress bar in the pricing card.
 */

const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

function animate(el, target, duration = 1400, suffix = '') {
  const start = performance.now();
  const tick = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const v = Math.round(target * easeOutCubic(t));
    el.textContent = v + suffix;
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function initScrollCounters() {
  const targets = $$('[data-counter]');
  if (!targets.length) return;
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      const el = e.target;
      const target = Number(el.dataset.counter) || 0;
      const suffix = el.dataset.suffix || '';
      animate(el, target, 1400, suffix);
      io.unobserve(el);
    });
  }, { threshold: 0.4 });
  targets.forEach((el) => io.observe(el));
}

async function loadWaitlistTotals() {
  try {
    const r = await fetch('/api/waitlist/count');
    const j = await r.json();
    const total    = (j && j.total)             || 0;
    const claimed  = (j && j.founder_claimed)   || 0;
    const cap      = (j && j.founder_cap)       || 500;

    const trust = document.querySelector('#lp-trust-count');
    if (trust) trust.textContent = total.toLocaleString('en-IN');

    const claimedEl = document.querySelector('#lp-founder-claimed');
    if (claimedEl) claimedEl.textContent = claimed.toLocaleString('en-IN');

    const bar = document.querySelector('#lp-founder-bar');
    if (bar) bar.style.width = Math.min(100, (claimed / cap) * 100).toFixed(1) + '%';
  } catch (_) {
    // Backend may not have the endpoint yet — leave defaults.
  }
}

export function initCounters() {
  initScrollCounters();
  loadWaitlistTotals();
  // Cheap poll — keeps the trust line + founder bar honest without SSE.
  setInterval(loadWaitlistTotals, 60_000);
}
