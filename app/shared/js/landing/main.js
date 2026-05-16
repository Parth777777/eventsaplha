/* main.js — Tickwave landing page orchestrator.
 * Boots every sub-module, wires GSAP scroll reveals, magnetic CTAs,
 * the hero mock stack, and the alpha simulator (mirrors the Python engine).
 */
import { initMarquee }              from './marquee.js';
import { initCounters }             from './counters.js';
import { initWaitlist }             from './waitlist.js';
import { initInteractiveFeatures }  from './features.js';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ──────────────────────────────────────────────────────────────────
 * Live data helpers. The hero phone now shows a real screenshot, so
 * the only live thing left in the hero is the three orbiting chips.
 * ────────────────────────────────────────────────────────────────── */
async function fetchTopSignals(limit) {
  // Prefer scored signals; fall back to event feed if signals table is empty.
  try {
    const r = await fetch('/api/signals?limit=' + limit);
    const j = await r.json();
    const sigs = (j && j.data) || [];
    if (sigs.length) return sigs;
  } catch (_) {}
  try {
    const r = await fetch('/api/feed?limit=' + limit);
    const j = await r.json();
    return ((j && j.data) || [])
      .filter(e => (e.companies && e.companies.length) || e.ticker)
      .map(e => ({
        ticker: e.ticker || (e.companies && e.companies[0]) || '',
        alpha_score: e.impact_score || (e.magnitude ? e.magnitude * 10 : 0),
        headline: e.title || e.headline || e.summary || '',
        event_type: e.event_type,
        age_hours: e.age_hours,
        volume_multiplier: e.volume_multiplier,
      }));
  } catch (_) { return []; }
}

async function loadHeroOrbits() {
  try {
    const items = await fetchTopSignals(2);
    const o1 = items[0], o2 = items[1];
    if (o1 && $('#lp-orbit-1')) {
      const el = $('#lp-orbit-1');
      const valEl = el.querySelector('.lp-orbit-val');
      const tkEl  = el.querySelector('.lp-orbit-tk');
      if (valEl) valEl.textContent = Math.round(o1.alpha_score || 0);
      if (tkEl)  tkEl.textContent  = o1.ticker || '';
    }
    if (o2 && $('#lp-orbit-2')) {
      const el = $('#lp-orbit-2');
      const valEl = el.querySelector('.lp-orbit-val');
      if (valEl) valEl.textContent =
        (o2.event_type || 'news').replace(/_/g, ' ').toUpperCase().slice(0, 14);
    }
  } catch (_) {}
}

/* ──────────────────────────────────────────────────────────────────
 * Hero mock — cursor parallax tilt on the phone frame.
 * ────────────────────────────────────────────────────────────────── */
function initHeroTilt() {
  const wrap  = $('.lp-hero-mock');
  const frame = $('.lp-phone');
  if (!wrap || !frame) return;
  if (matchMedia('(pointer:coarse)').matches) return; // skip on touch

  let rx = -14, ry = 6, tx = rx, ty = ry, rAF = 0;
  const onMove = (e) => {
    const r = wrap.getBoundingClientRect();
    const dx = (e.clientX - r.left - r.width / 2) / r.width;
    const dy = (e.clientY - r.top - r.height / 2) / r.height;
    tx = -14 + dx * 10;
    ty = 6 - dy * 6;
    if (!rAF) rAF = requestAnimationFrame(loop);
  };
  const loop = () => {
    rx += (tx - rx) * 0.08;
    ry += (ty - ry) * 0.08;
    frame.style.transform = `rotateY(${rx}deg) rotateX(${ry}deg)`;
    rAF = (Math.abs(tx - rx) > 0.05 || Math.abs(ty - ry) > 0.05)
      ? requestAnimationFrame(loop) : 0;
  };
  window.addEventListener('mousemove', onMove);
}

/* ──────────────────────────────────────────────────────────────────
 * Magnetic CTAs.
 * Buttons / nav pills with [data-magnet] pull toward the cursor.
 * ────────────────────────────────────────────────────────────────── */
function initMagneticCTAs() {
  if (matchMedia('(pointer:coarse)').matches) return;
  $$('[data-magnet]').forEach((el) => {
    let raf = 0;
    el.addEventListener('mousemove', (e) => {
      const r = el.getBoundingClientRect();
      const dx = (e.clientX - r.left - r.width / 2) * 0.25;
      const dy = (e.clientY - r.top - r.height / 2) * 0.25;
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        el.style.transform = `translate(${dx}px, ${dy}px)`;
      });
    });
    el.addEventListener('mouseleave', () => {
      el.style.transform = '';
    });
  });
}

/* ──────────────────────────────────────────────────────────────────
 * Scroll reveals — GSAP if loaded, else IntersectionObserver fallback.
 * ────────────────────────────────────────────────────────────────── */
function initScrollReveals() {
  const targets = $$([
    '.lp-section-head',
    '.lp-surface',
    '.lp-stat',
    '.lp-gallery-feature',
    '.lp-gallery-card',
    '.lp-gallery-mini',
    '.lp-forensics-copy',
    '.lp-forensics-shot',
    '.lp-pricing-card',
    '.lp-faq details',
  ].join(', '));
  if (!targets.length) return;

  if (window.gsap && window.ScrollTrigger) {
    gsap.registerPlugin(ScrollTrigger);
    targets.forEach((el) => {
      gsap.from(el, {
        y: 28, opacity: 0, duration: 0.7, ease: 'power2.out',
        scrollTrigger: { trigger: el, start: 'top 85%' }
      });
    });
    return;
  }
  // Fallback
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting) {
        e.target.style.transition = 'transform 600ms cubic-bezier(0.16,1,0.3,1), opacity 600ms';
        e.target.style.transform = 'translateY(0)';
        e.target.style.opacity = '1';
        io.unobserve(e.target);
      }
    });
  }, { threshold: 0.12 });
  targets.forEach((el) => {
    el.style.transform = 'translateY(28px)';
    el.style.opacity = '0';
    io.observe(el);
  });
}

/* ──────────────────────────────────────────────────────────────────
 * Bento 3D tilt on hover.
 * ────────────────────────────────────────────────────────────────── */
function initBentoTilt() {
  if (matchMedia('(pointer:coarse)').matches) return;
  $$('[data-tilt]').forEach((el) => {
    let raf = 0;
    el.style.transition = 'transform 220ms cubic-bezier(0.16,1,0.3,1)';
    el.addEventListener('mousemove', (e) => {
      const r = el.getBoundingClientRect();
      const dx = (e.clientX - r.left - r.width  / 2) / r.width;
      const dy = (e.clientY - r.top  - r.height / 2) / r.height;
      const rx = -dy * 6, ry = dx * 8;
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        el.style.transform = `perspective(900px) rotateX(${rx}deg) rotateY(${ry}deg) translateY(-2px)`;
      });
    });
    el.addEventListener('mouseleave', () => {
      el.style.transform = '';
    });
  });
}

/* ──────────────────────────────────────────────────────────────────
 * Alpha simulator — mirrors scraper/alpha_scoring_engine.py.
 * Updates the gauge + highlights active terms in the formula display.
 * ────────────────────────────────────────────────────────────────── */
const REGIME_WEIGHTS = {
  bull_strong:   { earnings:0.20, merger:0.16, policy:0.08, order_win:0.22, dividend:0.08, supply:0.12, insider:0.14 },
  bull_weak:     { earnings:0.22, merger:0.14, policy:0.10, order_win:0.18, dividend:0.10, supply:0.14, insider:0.12 },
  bear_strong:   { earnings:0.18, merger:0.10, policy:0.22, order_win:0.12, dividend:0.06, supply:0.20, insider:0.12 },
  bear_weak:     { earnings:0.20, merger:0.12, policy:0.18, order_win:0.14, dividend:0.08, supply:0.16, insider:0.12 },
  sideways_calm: { earnings:0.18, merger:0.18, policy:0.12, order_win:0.16, dividend:0.12, supply:0.12, insider:0.12 },
  sideways_choppy:{ earnings:0.20, merger:0.14, policy:0.14, order_win:0.16, dividend:0.10, supply:0.14, insider:0.12 },
  spike_up:      { earnings:0.16, merger:0.20, policy:0.08, order_win:0.22, dividend:0.06, supply:0.10, insider:0.18 },
  spike_down:    { earnings:0.18, merger:0.08, policy:0.18, order_win:0.10, dividend:0.06, supply:0.24, insider:0.16 },
  crisis:        { earnings:0.16, merger:0.06, policy:0.24, order_win:0.08, dividend:0.04, supply:0.22, insider:0.20 },
};
const SENTIMENT_BOOST = {
  bull_strong:   { bullish:1.35, bearish:0.60, neutral:1.00 },
  bull_weak:     { bullish:1.25, bearish:0.65, neutral:1.00 },
  bear_strong:   { bullish:1.50, bearish:0.50, neutral:0.90 },
  bear_weak:     { bullish:1.40, bearish:0.55, neutral:0.95 },
  sideways_calm: { bullish:1.20, bearish:0.85, neutral:1.00 },
  sideways_choppy:{bullish:1.10, bearish:0.90, neutral:1.00 },
  spike_up:      { bullish:1.45, bearish:0.70, neutral:1.05 },
  spike_down:    { bullish:1.40, bearish:0.55, neutral:0.85 },
  crisis:        { bullish:1.60, bearish:0.40, neutral:0.70 },
};

function computeAlpha({ magnitude, confidence, sentiment, days, event, regime }) {
  const base = Math.min(65, magnitude * 5 + confidence * 18);
  const weights = REGIME_WEIGHTS[regime] || REGIME_WEIGHTS.bull_strong;
  const avg = Object.values(weights).reduce((a,b)=>a+b,0) / Object.values(weights).length;
  const eventW = Math.min(1.5, (weights[event] || avg) / avg);
  const sentRaw = (SENTIMENT_BOOST[regime] || SENTIMENT_BOOST.bull_strong)[sentiment] || 1.0;
  const sentimentAdj = 1.0 + (sentRaw - 1.0) * 0.7;
  const timing = Math.max(0.05, Math.exp(-0.35 * days));
  const quality = 1.0;     // assume large cap in the playground
  const sector  = 1.05;    // mild positive
  const rs      = 1.05;
  const diverge = 1.0;
  const narrative = 1.05;
  const surprise  = 1.0;
  const flow      = 0.95 + confidence * 0.20;
  const alpha = base * eventW * sentimentAdj * timing * quality * sector * rs * diverge * narrative * surprise * flow;
  return Math.max(0, Math.min(100, alpha));
}

function initAlphaSim() {
  const ring  = $('#lp-gauge-ring');
  const num   = $('#lp-gauge-score');
  const band  = $('#lp-gauge-band');
  if (!ring) return;

  const inputs = $$('[data-sim]');
  const outs   = Object.fromEntries(
    $$('[data-sim-out]').map((o) => [o.dataset.simOut, o])
  );
  const factorEls = $$('#lp-formula-body span');
  const CIRC = 2 * Math.PI * 84;

  function readState() {
    const st = {};
    inputs.forEach((el) => {
      const key = el.dataset.sim;
      st[key] = (el.type === 'range') ? Number(el.value) : el.value;
    });
    return st;
  }

  function render() {
    const st = readState();
    Object.entries(st).forEach(([k, v]) => {
      if (outs[k]) outs[k].textContent = (typeof v === 'number') ? (Number.isInteger(v) ? v : v.toFixed(2)) : v;
    });
    const alpha = computeAlpha(st);
    const a = Math.round(alpha);
    num.textContent = a;
    const dashOffset = CIRC - (alpha / 100) * CIRC;
    ring.setAttribute('stroke-dashoffset', String(dashOffset));

    // Band styling
    const grade = a >= 65 ? 'high' : a >= 50 ? 'mid' : 'low';
    num.classList.remove('high','mid','low'); num.classList.add(grade);
    band.classList.remove('high','mid','low'); band.classList.add(grade);
    band.textContent = a >= 80 ? 'Top pick' : a >= 65 ? 'Strong'
                     : a >= 50 ? 'Tradeable' : 'Noise';

    // Highlight active factors based on inputs
    const active = new Set(['base','timing','flow','narrative']);
    if (st.event !== 'news')          active.add('weight');
    if (st.sentiment !== 'neutral')   active.add('sentiment');
    if (st.regime.startsWith('bear') || st.regime === 'crisis') active.add('diverge');
    factorEls.forEach((el) => el.classList.toggle('lp-active', active.has(el.dataset.factor)));
  }

  inputs.forEach((el) => el.addEventListener('input', render));
  render();
}

/* ──────────────────────────────────────────────────────────────────
 * Forensics gauge — fills as section scrolls into view.
 * ────────────────────────────────────────────────────────────────── */
function initForensicsGauge() {
  const ring = $('#lp-fgauge');
  const num  = $('#lp-fgauge-num');
  const sec  = $('#forensics');
  if (!ring || !sec) return;

  const CIRC = 352;
  let played = false;
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting || played) return;
      played = true;
      const target = 78;
      const start = performance.now();
      const duration = 1400;
      const tick = (now) => {
        const t = Math.min(1, (now - start) / duration);
        const eased = 1 - Math.pow(1 - t, 3);
        const v = target * eased;
        ring.setAttribute('stroke-dashoffset', String(CIRC - (v / 100) * CIRC));
        num.textContent = Math.round(v);
        if (t < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
      io.unobserve(e.target);
    });
  }, { threshold: 0.3 });
  io.observe(sec);
}

/* ──────────────────────────────────────────────────────────────────
 * Helpers.
 * ────────────────────────────────────────────────────────────────── */
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

/* ──────────────────────────────────────────────────────────────────
 * Boot.
 * ────────────────────────────────────────────────────────────────── */
function boot() {
  initMagneticCTAs();
  initHeroTilt();
  initBentoTilt();
  initAlphaSim();
  initForensicsGauge();
  initCounters();
  initMarquee();
  initWaitlist();
  initInteractiveFeatures();
  // Hero phone now shows a real screenshot — only the orbiting chips
  // need live data. They poll /api/signals every 30s.
  loadHeroOrbits();
  setInterval(loadHeroOrbits, 30_000);
  // GSAP scroll reveals fire on next tick to let GSAP CDN finish loading.
  setTimeout(initScrollReveals, 60);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
