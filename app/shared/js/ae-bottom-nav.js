/* ae-bottom-nav.js — mobile-only bottom tab bar.
 *
 * Auto-mounted via bootstrap.js on every page. Fixes the critical mobile
 * blocker where the sidebar is `hidden md:flex` and mobile users had no
 * way to navigate.
 *
 * 5 tabs: Today · Explore · Events · Watchlist · More
 * "More" opens the existing mobile sidebar drawer (handled by
 * sidebar-toggle.js which already adds `body.sidebar--open`).
 *
 * Skip pages: landing, login, signup, onboarding, admin/* — those don't
 * need the tab bar (full-page experiences or auth flows).
 */
(function () {
  'use strict';
  if (window.__aeBottomNavMounted) return;
  window.__aeBottomNavMounted = true;

  const SKIP = /(^|\/)(landing|login|signup|onboarding|admin)(\.html|\/|$)/i;
  if (SKIP.test(location.pathname)) return;

  const TABS = [
    { href: 'index.html',     icon: 'home',           label: 'Today',     match: /(^|\/)(index\.html|\/?)$/i },
    { href: 'explore.html',   icon: 'travel_explore', label: 'Explore',   match: /(^|\/)(explore|premover)\.html/i },
    { href: 'events.html',    icon: 'feed',           label: 'Events',    match: /(^|\/)(events|earnings|ipo|ma|policy|fo)\.html/i },
    { href: 'watchlist.html', icon: 'bookmark',       label: 'Watchlist', match: /(^|\/)(watchlist|alerts)\.html/i },
    { href: '#more',          icon: 'menu',           label: 'More',      match: /^never$/ },
  ];

  function activeFor(path) {
    const lower = path.toLowerCase();
    for (let i = 0; i < TABS.length; i++) {
      if (TABS[i].match.test(lower)) return i;
    }
    return -1;
  }

  function mount() {
    if (document.querySelector('ae-bottom-nav')) return;
    const nav = document.createElement('ae-bottom-nav');
    nav.setAttribute('role', 'navigation');
    nav.setAttribute('aria-label', 'Primary mobile navigation');
    const activeIdx = activeFor(location.pathname);
    TABS.forEach((tab, idx) => {
      const isMore = tab.href === '#more';
      const el = document.createElement(isMore ? 'button' : 'a');
      el.className = 'ae-bn-item';
      if (idx === activeIdx) el.classList.add('active');
      if (!isMore) el.href = tab.href;
      if (isMore) {
        el.type = 'button';
        el.setAttribute('aria-label', 'Open menu');
        el.addEventListener('click', () => {
          document.body.classList.toggle('sidebar--open');
        });
      }
      el.innerHTML = `
        <span class="material-symbols-outlined" aria-hidden="true">${tab.icon}</span>
        <span>${tab.label}</span>
      `;
      nav.appendChild(el);
    });
    document.body.appendChild(nav);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
