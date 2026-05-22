/* ============================================================
   THEME TOKENS — JS bridge to tokens.css
   ============================================================

   Provides a single API surface for widget JS to read theme-aware
   colors WITHOUT hardcoding hex values. Subscribes to the existing
   `themechange` CustomEvent dispatched by theme.js so widgets that
   paint on canvas (advanced-chart, etc.) can re-render on toggle.

   USAGE:
     // Single value
     const bg = ThemeTokens.color('--surface-1');

     // Frozen snapshot of common tokens (cheaper for canvas render loops)
     const C = ThemeTokens.snapshot();
     ctx.fillStyle = C.surface2;
     ctx.strokeStyle = C.borderStrong;
     ctx.fillText('hello', x, y, { color: C.textPrimary });

     // Re-render on theme switch
     ThemeTokens.onChange(() => myChart.render());

   The snapshot is recomputed on themechange so cached references
   stay coherent. Callers should fetch a fresh snapshot inside their
   render function, not cache it at module scope.
   ============================================================ */
(function (global) {
  'use strict';

  function color(varName) {
    if (!varName.startsWith('--')) varName = '--' + varName;
    try {
      return getComputedStyle(document.documentElement)
        .getPropertyValue(varName).trim();
    } catch (_) {
      return '';
    }
  }

  /* The most-used tokens, packed into a frozen object so canvas render
     loops can read them without 20+ getComputedStyle calls per frame.
     Keys are camelCased for ergonomics in JS. Recomputed on themechange. */
  function snapshot() {
    return Object.freeze({
      // Surfaces
      surface0:      color('--surface-0'),
      surface1:      color('--surface-1'),
      surface2:      color('--surface-2'),
      surface3:      color('--surface-3'),
      // Legacy aliases (still widely consumed in existing CSS)
      bg:            color('--bg'),
      s0:            color('--s0'),
      s1:            color('--s1'),
      s2:            color('--s2'),
      s3:            color('--s3'),
      s4:            color('--s4'),
      // Text
      textPrimary:   color('--text-primary')   || color('--t1'),
      textSecondary: color('--text-secondary') || color('--t2'),
      textTertiary:  color('--text-tertiary')  || color('--t3'),
      t1:            color('--t1'),
      t2:            color('--t2'),
      t3:            color('--t3'),
      // Borders
      borderSubtle:  color('--border-subtle') || color('--b1'),
      borderStrong:  color('--border-strong') || color('--b2'),
      b0:            color('--b0'),
      b1:            color('--b1'),
      b2:            color('--b2'),
      b3:            color('--b3'),
      // Accent + semantic
      accent:        color('--accent') || color('--info'),
      accentDim:     color('--accent-dim') || color('--info-dim'),
      bull:          color('--bull'),
      bullDim:       color('--bull-dim'),
      bear:          color('--bear'),
      bearDim:       color('--bear-dim'),
      caution:       color('--caution'),
      cautionDim:    color('--caution-dim'),
      info:          color('--info'),
      infoDim:       color('--info-dim'),
    });
  }

  /* Subscribe to theme changes. Returns an unsubscribe function. */
  function onChange(cb) {
    if (typeof cb !== 'function') return function () {};
    const handler = function (e) {
      try { cb(e && e.detail); } catch (_) {}
    };
    document.addEventListener('themechange', handler);
    return function () {
      document.removeEventListener('themechange', handler);
    };
  }

  /* Current theme ('dark' | 'light'). Cheap synchronous read. */
  function current() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }

  global.ThemeTokens = { color, snapshot, onChange, current };
})(window);
