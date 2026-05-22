/* quant-motion.js — restrained premium motion for the playground.
 *
 * What this does (and explicitly does NOT do):
 *   ✓ Fade-up on tab content reveal (one shot, 280ms, ease-out)
 *   ✓ Soft staggered fade-in on cards and output tiles within a pane
 *   ✓ Hover lift on interactive cards (-2px translateY, no glow)
 *   ✓ Number count-up when a .qp-out-val changes value (200ms, linear)
 *   ✗ No pulsing dots, no glowing rings, no infinite animations.
 *
 * Animations respect prefers-reduced-motion and disable entirely if set.
 *
 * Attached as side-effects on DOM events. No exports beyond window.QPMotion
 * for testability.
 */
(function () {
  'use strict';

  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) return; // honour user preference completely

  // Inject the keyframes + class hooks once
  var styleEl = document.createElement('style');
  styleEl.textContent = ''
    + '@keyframes qpMotionUp { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }'
    + '.qp-motion-in { animation: qpMotionUp 320ms cubic-bezier(0.22, 1, 0.36, 1) both; }'
    + '.qp-grid > *, .qp-outputs > *, .qp-panel { transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1), border-color 220ms ease; }'
    + '.qp-card:hover, .qp-out:hover { transform: translateY(-1px); }'
    + '.qp-example:hover { transform: translateY(-1px); }';
  document.head.appendChild(styleEl);

  // Stagger-reveal whenever a tab becomes active
  function revealPane(pane) {
    if (!pane) return;
    var targets = pane.querySelectorAll('.qp-grid > *, .qp-outputs > *, .qp-panel');
    targets.forEach(function (el, i) {
      el.classList.remove('qp-motion-in');
      // Force reflow so the animation re-fires
      void el.offsetWidth;
      el.style.animationDelay = Math.min(i * 35, 280) + 'ms';
      el.classList.add('qp-motion-in');
    });
  }
  document.addEventListener('qp:tab', function (e) {
    var name = e.detail.tab;
    setTimeout(function () {
      var pane = document.querySelector('.qp-pane[data-pane="' + name + '"]');
      revealPane(pane);
    }, 30);
  });
  // First load
  window.addEventListener('load', function () {
    revealPane(document.querySelector('.qp-pane.is-active'));
  });

  // Number count-up — observe .qp-out-val and animate value changes (numbers only)
  function parseNum(s) {
    var m = String(s).match(/-?[\d,]+(\.\d+)?/);
    return m ? parseFloat(m[0].replace(/,/g, '')) : null;
  }
  function fmtLike(template, n) {
    // Preserve currency symbol, sign, and decimal places from the template
    var hasCurrency = /[₹$]/.test(template);
    var sym = (template.match(/[₹$]/) || [''])[0];
    var negPrefix = /^[-−]/.test(template) ? '−' : '';
    var decimals = (template.match(/\.(\d+)/) || ['', '2'])[1].length;
    var abs = Math.abs(n);
    var s = abs.toLocaleString('en-IN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    return negPrefix + (hasCurrency ? sym : '') + s;
  }
  var animState = new WeakMap();
  function animateValue(el, from, to) {
    if (animState.get(el)) cancelAnimationFrame(animState.get(el));
    var start = performance.now();
    var dur = 220;
    var tpl = el.dataset.tpl || el.textContent;
    el.dataset.tpl = tpl;
    function step(now) {
      var t = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - t, 3); // ease-out
      var cur = from + (to - from) * eased;
      el.textContent = fmtLike(tpl, cur);
      if (t < 1) animState.set(el, requestAnimationFrame(step));
      else animState.delete(el);
    }
    animState.set(el, requestAnimationFrame(step));
  }
  var obs = new MutationObserver(function (mutations) {
    mutations.forEach(function (m) {
      if (m.type !== 'childList' && m.type !== 'characterData') return;
      var el = m.target.nodeType === 3 ? m.target.parentNode : m.target;
      if (!el || !el.classList || !el.classList.contains('qp-out-val')) return;
      // Pull the new text BEFORE we re-animate
      var newTxt = el.textContent;
      var to = parseNum(newTxt);
      var fromAttr = el.dataset.lastVal;
      var from = fromAttr != null ? parseFloat(fromAttr) : null;
      if (to == null || from == null || isNaN(from) || isNaN(to) || from === to) {
        el.dataset.lastVal = to;
        return;
      }
      // Run animation
      el.dataset.tpl = newTxt;
      animateValue(el, from, to);
      el.dataset.lastVal = to;
    });
  });
  // Wire observer onto each .qp-out-val as it appears
  function attachObservers(root) {
    (root || document).querySelectorAll('.qp-out-val').forEach(function (el) {
      if (el.dataset.qpObserved) return;
      el.dataset.qpObserved = '1';
      var initial = parseNum(el.textContent);
      if (initial != null) el.dataset.lastVal = initial;
      obs.observe(el, { childList: true, characterData: true, subtree: true });
    });
  }
  document.addEventListener('qp:tab', function () { setTimeout(attachObservers, 50); });
  window.addEventListener('load', function () { setTimeout(attachObservers, 50); });

  window.QPMotion = { revealPane: revealPane, attachObservers: attachObservers };
})();
