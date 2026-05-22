/* site-motion.js — site-wide motion helper.
 *
 * Vanilla equivalents of the Framer Motion behaviors the UX brief asked for:
 *
 *   - whileInView stagger reveal:
 *       Any container with [data-motion-stagger] (or matching one of the
 *       default selectors) gets its direct children faded + slid up
 *       (y: 12 → 0) in sequence when scrolled into view.
 *
 *   - layout (smooth height):
 *       [data-expandable] toggles between display: grid 0fr ↔ 1fr via CSS,
 *       so opening an accordion smoothly pushes content below. Toggle via
 *       SiteMotion.toggle(el) or by clicking [data-expand-trigger="<id>"].
 *
 *   - Animated number changes:
 *       Wrap any updating numeric in <span class="num-flash" data-num="42.5">.
 *       Update by calling SiteMotion.setNum(el, 47.2) — rolls the digit, then
 *       briefly flashes mint/rose depending on direction. Also auto-detects
 *       textContent changes on .num-flash via MutationObserver.
 *
 * Respects prefers-reduced-motion fully (bails out).
 * Auto-boots on DOMContentLoaded — no init needed.
 */
(function (root) {
  'use strict';

  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ──────────────────────────────────────────────────────────────────────────
  // 1. STAGGER REVEAL — whileInView + stagger
  //    Defaults: any direct children of containers matching these selectors
  // ──────────────────────────────────────────────────────────────────────────
  var DEFAULT_STAGGER_TARGETS = [
    '[data-motion-stagger]',
    '.rf-kpi-grid', '.fc-grid', '.fc-hero', '.fo-grid',
    '.qp-grid', '.qp-outputs',
    '.tw-home-movers', '.tw-hero-feed-wrap',
    '.ae-card-grid', '.signal-list',
  ];

  function markStaggerChildren(root) {
    if (reduce) return;
    DEFAULT_STAGGER_TARGETS.forEach(function (sel) {
      (root || document).querySelectorAll(sel).forEach(function (container) {
        if (container.dataset.motionInit) return;
        container.dataset.motionInit = '1';
        var kids = container.children;
        for (var i = 0; i < kids.length; i++) {
          var k = kids[i];
          if (!k.hasAttribute('data-motion')) k.setAttribute('data-motion', 'up');
          k.style.transitionDelay = Math.min(i * 50, 360) + 'ms';
        }
      });
    });
    // Page-level data-motion="up" elements outside the staggered containers
    // also reveal individually when scrolled into view.
  }

  var io = null;
  function setupObserver() {
    if (reduce) {
      // No animation — reveal immediately
      document.querySelectorAll('[data-motion]').forEach(function (el) {
        el.classList.add('is-in');
      });
      return;
    }
    io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-in');
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: '-40px 0px -40px 0px', threshold: 0.05 });
    document.querySelectorAll('[data-motion]').forEach(function (el) {
      // If element is already in viewport on load, reveal it immediately so
      // there's no jarring "everything pops in after scroll"
      var r = el.getBoundingClientRect();
      var inView = r.top < (window.innerHeight - 40) && r.bottom > 40;
      if (inView) {
        // Small frame delay so the transition fires
        requestAnimationFrame(function () { el.classList.add('is-in'); });
      } else {
        io.observe(el);
      }
    });
  }

  // Re-scan when content is dynamically inserted (works with any framework or
  // ad-hoc JS that appends DOM later). Throttled.
  var rescanQueued = false;
  function rescan() {
    if (rescanQueued) return;
    rescanQueued = true;
    setTimeout(function () {
      rescanQueued = false;
      markStaggerChildren(document);
      if (!io) return;
      document.querySelectorAll('[data-motion]:not(.is-in)').forEach(function (el) {
        if (el.dataset.motionObserved) return;
        el.dataset.motionObserved = '1';
        var r = el.getBoundingClientRect();
        var inView = r.top < window.innerHeight && r.bottom > 0;
        if (inView) requestAnimationFrame(function () { el.classList.add('is-in'); });
        else io.observe(el);
      });
    }, 120);
  }
  var mo = new MutationObserver(rescan);

  // ──────────────────────────────────────────────────────────────────────────
  // 2. EXPANDABLE — smooth height transition (CSS handles grid 0fr ↔ 1fr)
  // ──────────────────────────────────────────────────────────────────────────
  function toggleExpand(el) {
    if (!el) return;
    var open = el.getAttribute('data-open') === 'true';
    el.setAttribute('data-open', open ? 'false' : 'true');
  }

  // Click delegation: [data-expand-trigger="id"] toggles #id
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-expand-trigger]');
    if (!t) return;
    e.preventDefault();
    var id = t.getAttribute('data-expand-trigger');
    var target = id ? document.getElementById(id) : t.nextElementSibling;
    if (target) {
      toggleExpand(target);
      // Rotate caret if present
      var caret = t.querySelector('[data-expand-caret]');
      if (caret) caret.style.transform = (target.getAttribute('data-open') === 'true') ? 'rotate(180deg)' : 'rotate(0)';
    }
  });

  // ──────────────────────────────────────────────────────────────────────────
  // 3. NUMBER COUNT-UP + FLASH on value changes
  // ──────────────────────────────────────────────────────────────────────────
  var animState = new WeakMap();
  function parseNum(s) {
    var m = String(s).match(/-?[\d,]+(\.\d+)?/);
    return m ? parseFloat(m[0].replace(/,/g, '')) : null;
  }
  function fmtLike(template, n) {
    var hasCurrency = /[₹$€£]/.test(template);
    var sym = (template.match(/[₹$€£]/) || [''])[0];
    var hasPct = /%$/.test(template.trim());
    var negPrefix = /^[-−]/.test(template) ? '−' : '';
    var decimals = (template.match(/\.(\d+)/) || ['', '2'])[1].length;
    var abs = Math.abs(n);
    var s = abs.toLocaleString('en-IN', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    return negPrefix + (hasCurrency ? sym : '') + s + (hasPct ? '%' : '');
  }
  function animateValue(el, from, to, opts) {
    opts = opts || {};
    var prior = animState.get(el);
    if (prior) cancelAnimationFrame(prior);
    var start = performance.now();
    var dur = opts.duration || 320;
    var tpl = el.dataset.tpl || el.textContent;
    el.dataset.tpl = tpl;
    function step(now) {
      var t = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - t, 3);
      var cur = from + (to - from) * eased;
      el.textContent = fmtLike(tpl, cur);
      if (t < 1) animState.set(el, requestAnimationFrame(step));
      else { animState.delete(el); flash(el, to > from ? 'up' : 'down'); }
    }
    animState.set(el, requestAnimationFrame(step));
  }
  function flash(el, dir) {
    el.classList.add('num-flash');
    el.classList.remove('is-flash-up', 'is-flash-down');
    el.classList.add('is-flash-' + (dir === 'up' ? 'up' : 'down'));
    setTimeout(function () { el.classList.remove('is-flash-up', 'is-flash-down'); }, 600);
  }
  function setNum(el, to) {
    if (!el || typeof to !== 'number' || !isFinite(to)) return;
    var from = parseNum(el.textContent);
    if (from == null || from === to) {
      el.textContent = fmtLike(el.dataset.tpl || el.textContent, to);
      return;
    }
    animateValue(el, from, to);
  }
  // Auto-observe .num-flash for external text changes
  var numObs = new MutationObserver(function (mutations) {
    mutations.forEach(function (m) {
      var el = m.target.nodeType === 3 ? m.target.parentNode : m.target;
      if (!el || !el.classList || !el.classList.contains('num-flash')) return;
      var newTxt = el.textContent;
      var to = parseNum(newTxt);
      var fromAttr = el.dataset.lastVal;
      var from = fromAttr != null ? parseFloat(fromAttr) : null;
      if (to == null) return;
      if (from == null || isNaN(from) || from === to) {
        el.dataset.lastVal = to;
        return;
      }
      // Re-animate (overwrites direct text update)
      el.dataset.tpl = newTxt;
      animateValue(el, from, to);
      el.dataset.lastVal = to;
    });
  });
  function attachNumObservers(root) {
    (root || document).querySelectorAll('.num-flash').forEach(function (el) {
      if (el.dataset.numObserved) return;
      el.dataset.numObserved = '1';
      var initial = parseNum(el.textContent);
      if (initial != null) el.dataset.lastVal = initial;
      numObs.observe(el, { childList: true, characterData: true, subtree: true });
    });
  }

  // ──────────────────────────────────────────────────────────────────────────
  // BOOT
  // ──────────────────────────────────────────────────────────────────────────
  function boot() {
    markStaggerChildren(document);
    setupObserver();
    attachNumObservers(document);
    mo.observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // Public API
  root.SiteMotion = {
    reveal: function (el) { if (el) el.classList.add('is-in'); },
    setNum: setNum,
    toggle: toggleExpand,
    flash: flash,
    rescan: rescan,
  };
})(typeof window !== 'undefined' ? window : globalThis);
