/* Tickwave theme toggle.
   - Persists to localStorage as 'tw-theme' (values: 'dark' | 'light')
   - Default = dark
   - No-flash: applies theme synchronously before paint via inline init
   - Injects a fixed-position toggle button on every page
*/
(function () {
    'use strict';

    var STORAGE_KEY = 'tw-theme';
    var DEFAULT_THEME = 'dark';

    function getStored() {
        try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
    }
    function setStored(v) {
        try { localStorage.setItem(STORAGE_KEY, v); } catch (e) {}
    }

    function applyTheme(theme) {
        var root = document.documentElement;
        root.setAttribute('data-theme', theme);
        // Tailwind dark-mode helper
        if (theme === 'dark') root.classList.add('dark');
        else root.classList.remove('dark');
    }

    // Apply ASAP (avoids flash). Called inline via the small bootstrap below.
    var initial = getStored() || DEFAULT_THEME;
    applyTheme(initial);

    function toggle() {
        var current = document.documentElement.getAttribute('data-theme') || DEFAULT_THEME;
        var next = current === 'dark' ? 'light' : 'dark';
        applyTheme(next);
        setStored(next);
        window.dispatchEvent(new CustomEvent('tw-theme-change', { detail: { theme: next } }));
    }

    function injectToggle() {
        if (document.getElementById('rf-theme-toggle')) return;
        var btn = document.createElement('button');
        btn.id = 'rf-theme-toggle';
        btn.className = 'rf-theme-toggle';
        btn.type = 'button';
        btn.setAttribute('aria-label', 'Toggle theme');
        btn.innerHTML = [
            '<svg class="rf-icon-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
            '<svg class="rf-icon-moon" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
        ].join('');
        btn.addEventListener('click', toggle);
        document.body.appendChild(btn);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectToggle);
    } else {
        injectToggle();
    }

    // Expose for programmatic control
    window.TwTheme = {
        get: function () { return document.documentElement.getAttribute('data-theme') || DEFAULT_THEME; },
        set: function (theme) {
            if (theme !== 'dark' && theme !== 'light') return;
            applyTheme(theme); setStored(theme);
        },
        toggle: toggle,
    };
})();
