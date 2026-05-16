/* features.js — interactivity for the 8 landing-page feature cards.
 *
 * Each card type gets:
 *   - animate-on-view (visualisation draws/rolls/fills in)
 *   - hover/tap micro-interactions (cells highlight, badges pulse, etc.)
 *   - occasional "live update" pulse so the page feels alive
 */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ────────────────────────────────────────────────────────────
 * Easings + helpers.
 * ──────────────────────────────────────────────────────────── */
const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);

function tweenNumber(el, from, to, duration, format) {
  const start = performance.now();
  const tick = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const v = from + (to - from) * easeOutCubic(t);
    el.textContent = format ? format(v) : Math.round(v);
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function tweenAttr(el, attr, from, to, duration, easing) {
  easing = easing || easeOutCubic;
  const start = performance.now();
  const tick = (now) => {
    const t = Math.min(1, (now - start) / duration);
    el.setAttribute(attr, String(from + (to - from) * easing(t)));
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

/* ────────────────────────────────────────────────────────────
 * Card-specific animations.
 * ──────────────────────────────────────────────────────────── */

// Newsroom — animated stream. SVG already has built-in <animate>; we
// add a click → spawn a new dot rippling outward at a random Y.
function bindStream(card) {
  const art = card.querySelector('.lp-feat-art-stream svg');
  if (!art) return;
  card.addEventListener('click', (e) => {
    if (!card.matches(':hover')) return;
    const ns = 'http://www.w3.org/2000/svg';
    const dot = document.createElementNS(ns, 'circle');
    const y = 22 + Math.floor(Math.random() * 4) * 22;
    const colors = ['#2dd4aa', '#8eb4e0', '#e6b84a', '#a78bfa'];
    dot.setAttribute('cy', String(y));
    dot.setAttribute('r', '3.5');
    dot.setAttribute('fill', colors[Math.floor(Math.random() * 4)]);
    art.appendChild(dot);
    const start = performance.now();
    const run = (now) => {
      const t = Math.min(1, (now - start) / 2200);
      dot.setAttribute('cx', String(-10 + t * 260));
      dot.setAttribute('opacity', String(1 - t * 0.4));
      if (t < 1) requestAnimationFrame(run); else dot.remove();
    };
    requestAnimationFrame(run);
  });
}

// Signal Scanner — multi-arc ring + counter rolling up to 2,341.
function animateRing(card) {
  const svg = card.querySelector('.lp-feat-art-ring svg');
  if (!svg) return;
  const arcs = svg.querySelectorAll('circle[stroke-dasharray]');
  arcs.forEach((arc) => {
    const dash = arc.getAttribute('stroke-dasharray');
    arc.setAttribute('stroke-dasharray', dash);
    arc.style.strokeDashoffset = '276';
    requestAnimationFrame(() => {
      arc.style.transition = 'stroke-dashoffset 1100ms cubic-bezier(0.16,1,0.3,1)';
      arc.style.strokeDashoffset = (arc.dataset.offset || '0');
    });
  });
  const num = svg.querySelector('text');
  if (num) tweenNumber(num, 0, 2341, 1400, (v) => Math.round(v).toLocaleString('en-IN'));
}
function bindRingHover(card) {
  const svg = card.querySelector('.lp-feat-art-ring svg');
  if (!svg) return;
  card.addEventListener('mouseenter', () => {
    svg.style.transition = 'transform 700ms cubic-bezier(0.16,1,0.3,1)';
    svg.style.transform = 'rotate(8deg)';
  });
  card.addEventListener('mouseleave', () => {
    svg.style.transform = 'rotate(0deg)';
  });
}

// Sector heatmap — cells fade in with stagger; hover shows sector name.
const SECTOR_TOOLTIP = ['BFSI','IT','PHARMA','AUTO','POWER','FMCG','METALS','MEDIA','OIL','REALTY','TEXTILE','INFRA'];
const SECTOR_PCT     = ['+12.1%','+7.4%','+5.8%','+2.1%','+30.4%','-0.8%','+18.7%','-12.5%','-3.2%','+55.6%','-25.0%','-8.4%'];

function animateHeatmap(card) {
  const cells = card.querySelectorAll('.lp-heat-grid span');
  cells.forEach((c, i) => {
    c.style.opacity = '0';
    c.style.transform = 'scale(0.6)';
    setTimeout(() => {
      c.style.transition = 'opacity 360ms, transform 420ms cubic-bezier(0.16,1,0.3,1)';
      c.style.opacity = '1';
      c.style.transform = 'scale(1)';
    }, i * 50);
  });
}
function bindHeatmapHover(card) {
  const cells = card.querySelectorAll('.lp-heat-grid span');
  const pill = card.querySelector('.lp-feat-pill');
  if (!cells.length || !pill) return;
  const original = pill.innerHTML;
  cells.forEach((c, i) => {
    c.style.cursor = 'pointer';
    c.addEventListener('mouseenter', () => {
      pill.innerHTML = `<strong>${SECTOR_TOOLTIP[i] || '—'}</strong> ${SECTOR_PCT[i] || ''}`;
      c.style.transform = 'scale(1.08)';
      c.style.zIndex = '2';
    });
    c.addEventListener('mouseleave', () => {
      pill.innerHTML = original;
      c.style.transform = 'scale(1)';
    });
  });
}

// F&O — option-chain bars grow from baseline.
function animateFO(card) {
  const rects = card.querySelectorAll('.lp-feat-art-fo rect');
  rects.forEach((r, i) => {
    const finalY = parseFloat(r.getAttribute('y'));
    const finalH = parseFloat(r.getAttribute('height'));
    r.setAttribute('y', '80');
    r.setAttribute('height', '0');
    setTimeout(() => {
      r.style.transition = 'all 700ms cubic-bezier(0.16,1,0.3,1)';
      r.setAttribute('y', String(finalY));
      r.setAttribute('height', String(finalH));
    }, 80 + i * 40);
  });
}
function bindFOHover(card) {
  // Hover a bar → highlight + show strike index in the pill.
  const rects = card.querySelectorAll('.lp-feat-art-fo rect');
  const pill = card.querySelector('.lp-feat-pill');
  if (!rects.length || !pill) return;
  const orig = pill.innerHTML;
  rects.forEach((r, idx) => {
    r.style.transition = 'opacity 160ms, transform 200ms';
    r.style.cursor = 'pointer';
    r.addEventListener('mouseenter', () => {
      rects.forEach((rr) => { rr.style.opacity = '0.25'; });
      r.style.opacity = '1';
      const strike = 23000 + (idx - 5) * 100;
      pill.innerHTML = `<strong>Strike</strong> ${strike.toLocaleString('en-IN')}`;
    });
    r.addEventListener('mouseleave', () => {
      rects.forEach((rr) => { rr.style.opacity = ''; });
      pill.innerHTML = orig;
    });
  });
}

// Pre-movers — sparkline draws in with stroke-dasharray trick.
function animateSpark(card) {
  const paths = card.querySelectorAll('.lp-feat-art-spark path');
  paths.forEach((p) => {
    if (p.getAttribute('fill') === 'none' || !p.getAttribute('fill')) {
      try {
        const len = p.getTotalLength();
        p.style.strokeDasharray = String(len);
        p.style.strokeDashoffset = String(len);
        requestAnimationFrame(() => {
          p.style.transition = 'stroke-dashoffset 1400ms cubic-bezier(0.16,1,0.3,1)';
          p.style.strokeDashoffset = '0';
        });
      } catch (_) {}
    } else {
      // The fill (area under curve) — fade in
      p.style.opacity = '0';
      setTimeout(() => {
        p.style.transition = 'opacity 800ms';
        p.style.opacity = '1';
      }, 800);
    }
  });
}

// Forensics — gauge fills 0 → 78 + number counts.
function animateForensicsGauge(card) {
  const ring = card.querySelector('.lp-feat-art-forensics circle:nth-of-type(2)');
  const num  = card.querySelector('.lp-feat-art-forensics text');
  if (!ring) return;
  const CIRC = 251;
  ring.setAttribute('stroke-dasharray', '0 ' + CIRC);
  requestAnimationFrame(() => {
    ring.style.transition = 'stroke-dasharray 1500ms cubic-bezier(0.16,1,0.3,1)';
    ring.setAttribute('stroke-dasharray', '196 ' + CIRC);
  });
  if (num) tweenNumber(num, 0, 78, 1500);
}
function bindForensicsHover(card) {
  // Click the card body → number ticks up a couple of points then back (simulates a live re-score)
  card.addEventListener('click', (e) => {
    if (e.target.closest('a, button')) return;
    const num = card.querySelector('.lp-feat-art-forensics text');
    if (!num) return;
    const cur = parseInt(num.textContent, 10) || 78;
    tweenNumber(num, cur, cur + 3, 600);
    setTimeout(() => tweenNumber(num, cur + 3, 78, 800), 700);
  });
}

// Policy — badges slide in from the left in sequence.
function animatePolicy(card) {
  const badges = card.querySelectorAll('.lp-policy-badge');
  badges.forEach((b, i) => {
    b.style.transform = 'translateX(-16px)';
    b.style.opacity = '0';
    setTimeout(() => {
      b.style.transition = 'transform 420ms cubic-bezier(0.16,1,0.3,1), opacity 360ms';
      b.style.transform = 'translateX(0)';
      b.style.opacity = '1';
    }, 100 + i * 110);
  });
}
function bindPolicyPulse(card) {
  // Every ~6s, briefly highlight one badge as if a fresh circular came in.
  const badges = card.querySelectorAll('.lp-policy-badge');
  if (!badges.length) return;
  let timer;
  const start = () => {
    timer = setInterval(() => {
      const b = badges[Math.floor(Math.random() * badges.length)];
      const orig = b.style.background;
      b.style.transition = 'background 280ms, color 280ms';
      b.style.background = 'rgba(45,212,170,0.18)';
      b.style.color = '#2dd4aa';
      setTimeout(() => { b.style.background = orig; b.style.color = ''; }, 700);
    }, 5800);
  };
  // Only run when card is in viewport
  new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting && !timer) start();
      else if (!e.isIntersecting && timer) { clearInterval(timer); timer = null; }
    });
  }, { threshold: 0.4 }).observe(card);
}

// Stock dossier — timeline dots cascade left-to-right.
function animateTimeline(card) {
  const dots = card.querySelectorAll('.lp-feat-art-timeline g');
  const tracker = card.querySelector('.lp-feat-art-timeline circle[r="6"]');
  dots.forEach((g, i) => {
    g.style.opacity = '0';
    g.style.transform = 'translateY(6px)';
    setTimeout(() => {
      g.style.transition = 'opacity 360ms, transform 420ms cubic-bezier(0.16,1,0.3,1)';
      g.style.opacity = '1';
      g.style.transform = 'translateY(0)';
    }, 100 + i * 140);
  });
  if (tracker) {
    tracker.style.opacity = '0';
    setTimeout(() => {
      tracker.style.transition = 'opacity 500ms';
      tracker.style.opacity = '1';
    }, 100 + dots.length * 140);
  }
}

/* ────────────────────────────────────────────────────────────
 * Dispatcher.
 * ──────────────────────────────────────────────────────────── */
function animateCard(card) {
  if (card.querySelector('.lp-feat-art-stream'))    {} // already animated by SVG <animate>
  if (card.querySelector('.lp-feat-art-ring'))      animateRing(card);
  if (card.querySelector('.lp-feat-art-heat'))      animateHeatmap(card);
  if (card.querySelector('.lp-feat-art-fo'))        animateFO(card);
  if (card.querySelector('.lp-feat-art-spark'))     animateSpark(card);
  if (card.querySelector('.lp-feat-art-forensics')) animateForensicsGauge(card);
  if (card.querySelector('.lp-feat-art-policy'))    animatePolicy(card);
  if (card.querySelector('.lp-feat-art-timeline'))  animateTimeline(card);
}

function bindCardInteractions(card) {
  bindStream(card);
  bindRingHover(card);
  bindHeatmapHover(card);
  bindFOHover(card);
  bindForensicsHover(card);
  bindPolicyPulse(card);
}

export function initInteractiveFeatures() {
  const cards = $$('.lp-feat');
  if (!cards.length) return;
  cards.forEach(bindCardInteractions);
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      animateCard(e.target);
      io.unobserve(e.target);
    });
  }, { threshold: 0.3 });
  cards.forEach((c) => io.observe(c));
}
