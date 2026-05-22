/* a11y.js — site-wide accessibility enhancer.
 *
 * Auto-mounted by bootstrap.js. Idempotent — runs once on DOMContentLoaded
 * + once on every DOM mutation that adds new buttons.
 *
 * Fixes (without touching per-page HTML):
 *   1. Icon-only buttons get aria-label derived from their material-symbol
 *      glyph + title/onclick context.
 *   2. <main> gets id="main" and role="main" if missing (skip-link target).
 *   3. <nav> elements without aria-label get a guess based on context.
 *   4. <aside class="sidebar"> gets aria-label="Sidebar navigation".
 *   5. Modal/dialog containers gain ESC-to-close.
 *
 * Out of scope (per "no overlay popups" policy):
 *   - Auto-mounted toast/banner/announcer regions.
 */
(function () {
  'use strict';
  if (window.__aeA11yMounted) return;
  window.__aeA11yMounted = true;

  // Map of icon → polite default label (extend as needed)
  const ICON_LABELS = {
    'menu': 'Open menu',
    'close': 'Close',
    'search': 'Search',
    'add': 'Add',
    'edit': 'Edit',
    'delete': 'Delete',
    'settings': 'Settings',
    'notifications': 'Notifications',
    'notifications_active': 'Notifications',
    'volume_up': 'Mute alert sounds',
    'volume_off': 'Unmute alert sounds',
    'dark_mode': 'Switch to dark mode',
    'light_mode': 'Switch to light mode',
    'refresh': 'Refresh',
    'arrow_back': 'Back',
    'arrow_forward': 'Forward',
    'arrow_upward': 'Move up',
    'arrow_downward': 'Move down',
    'chevron_left': 'Previous',
    'chevron_right': 'Next',
    'expand_more': 'Expand',
    'expand_less': 'Collapse',
    'more_vert': 'More options',
    'more_horiz': 'More options',
    'help': 'Help',
    'info': 'More info',
    'star': 'Add to watchlist',
    'bookmark': 'Bookmark',
    'share': 'Share',
    'download': 'Download',
    'upload': 'Upload',
    'filter_alt': 'Filter',
    'sort': 'Sort',
    'check': 'Confirm',
    'check_circle': 'Confirmed',
    'error': 'Error',
    'warning': 'Warning',
    'home': 'Home',
  };

  function labelForButton(btn) {
    // Don't overwrite existing labels
    if (btn.hasAttribute('aria-label')) return null;
    if (btn.hasAttribute('aria-labelledby')) return null;
    const text = (btn.textContent || '').trim();
    if (text && text.length > 1 && !/^[·•·\.…]+$/.test(text)) return null;
    const ico = btn.querySelector('.material-symbols-outlined, .material-icons');
    if (!ico) return null;
    const glyph = (ico.textContent || '').trim().toLowerCase();
    return btn.getAttribute('title') || ICON_LABELS[glyph] || (glyph ? `${glyph.replace(/_/g, ' ')} button` : null);
  }

  function fixIconButtons(root) {
    (root || document).querySelectorAll('button, [role="button"]').forEach((btn) => {
      const lbl = labelForButton(btn);
      if (lbl) btn.setAttribute('aria-label', lbl);
    });
    // Anchors styled as buttons
    (root || document).querySelectorAll('a').forEach((a) => {
      if (a.hasAttribute('aria-label')) return;
      const t = (a.textContent || '').trim();
      if (t) return;
      const ico = a.querySelector('.material-symbols-outlined');
      if (!ico) return;
      const glyph = (ico.textContent || '').trim().toLowerCase();
      a.setAttribute('aria-label', a.getAttribute('title') || ICON_LABELS[glyph] || glyph.replace(/_/g, ' '));
    });
  }

  function fixLandmarks() {
    const main = document.querySelector('main');
    if (main) {
      if (!main.id) main.id = 'main';
      if (!main.hasAttribute('role')) main.setAttribute('role', 'main');
    }
    document.querySelectorAll('nav').forEach((n) => {
      if (n.hasAttribute('aria-label')) return;
      if (n.closest('.sidebar')) n.setAttribute('aria-label', 'Primary navigation');
      else if (n.closest('ae-bottom-nav')) n.setAttribute('aria-label', 'Mobile primary navigation');
      else n.setAttribute('aria-label', 'Navigation');
    });
    document.querySelectorAll('aside.sidebar').forEach((a) => {
      if (!a.hasAttribute('aria-label')) a.setAttribute('aria-label', 'Sidebar navigation');
      if (!a.hasAttribute('role')) a.setAttribute('role', 'complementary');
    });
  }

  function bindEscapeClose() {
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const open = document.querySelector('[aria-modal="true"], .modal--open, dialog[open]');
      if (open) {
        const closer = open.querySelector('[data-close], .modal-close, [aria-label="Close"]');
        if (closer && closer.click) closer.click();
      }
      // Mobile sidebar drawer
      if (document.body.classList.contains('sidebar--open')) {
        document.body.classList.remove('sidebar--open');
      }
    });
  }

  function init() {
    try { fixLandmarks(); } catch (_) {}
    try { fixIconButtons(document); } catch (_) {}
    try { bindEscapeClose(); } catch (_) {}
    // Watch for buttons added later (e.g. by widgets.js, charts.js)
    try {
      new MutationObserver((mutations) => {
        for (const m of mutations) {
          m.addedNodes && m.addedNodes.forEach((n) => {
            if (n.nodeType !== 1) return;
            if (n.matches && (n.matches('button') || n.matches('a'))) fixIconButtons(n.parentNode || document);
            else if (n.querySelectorAll) fixIconButtons(n);
          });
        }
      }).observe(document.body, { childList: true, subtree: true });
    } catch (_) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
