/* ============================================================
   THEME — Dark/Light toggle SWITCH with localStorage persist
   Auto-mounts an iOS-style sliding switch on every page.
   Loaded synchronously in <head> so the persisted theme is
   applied before paint (no flash-of-wrong-theme).
   ============================================================ */
(function (global) {
  'use strict';

  const KEY = 'tickwave:theme';

  function get() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }
  function set(theme) {
    if (theme !== 'light' && theme !== 'dark') theme = 'dark';
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem(KEY, theme); } catch (_) {}
    document.dispatchEvent(new CustomEvent('themechange', { detail: theme }));
  }
  function toggle() { set(get() === 'dark' ? 'light' : 'dark'); }

  // Apply persisted theme synchronously to avoid flash
  function applyPersisted() {
    let saved = 'dark';
    try { saved = localStorage.getItem(KEY) || 'dark'; } catch (_) {}
    document.documentElement.setAttribute('data-theme', saved);
  }
  applyPersisted();

  // Auto-mount the sliding switch after DOM is ready
  function mountSwitch() {
    if (document.querySelector('.theme-switch') || document.querySelector('.theme-toggle')) return;
    const sw = document.createElement('button');
    sw.className = 'theme-switch';
    sw.setAttribute('aria-label', 'Toggle dark/light theme');
    sw.setAttribute('role', 'switch');
    sw.setAttribute('aria-checked', get() === 'light' ? 'true' : 'false');
    sw.innerHTML = `
      <span class="theme-switch__icon left">
        <span class="material-symbols-outlined">dark_mode</span>
      </span>
      <span class="theme-switch__icon right">
        <span class="material-symbols-outlined">light_mode</span>
      </span>
      <span class="theme-switch__thumb">
        <span class="material-symbols-outlined icon-on-thumb-dark">dark_mode</span>
        <span class="material-symbols-outlined icon-on-thumb-light">light_mode</span>
      </span>
    `;
    sw.addEventListener('click', () => {
      toggle();
      sw.setAttribute('aria-checked', get() === 'light' ? 'true' : 'false');
    });
    document.body.appendChild(sw);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountSwitch);
  } else {
    mountSwitch();
  }

  global.Theme = { get, set, toggle };
})(window);
