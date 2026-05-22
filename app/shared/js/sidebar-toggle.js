/* ============================================================
   SIDEBAR TOGGLE — pin / unpin the sidebar's expanded state.
   ============================================================
   The base sidebar architecture (style.css) is a hover-expand: 52px slim
   icon rail by default, expands to 220px when the mouse is over it.
   This toggle PINS the expanded state so the user doesn't have to keep
   hovering. Click again to release back to hover-only behaviour.

   - Desktop: toggles `.sidebar--pinned-open` (locked at 240px).
   - Mobile (<768px): the button becomes a close-drawer trigger; a
     separate hamburger trigger is mounted to OPEN the drawer.

   State persisted to localStorage (`tickwave:sidebar:pinned`).

   Loaded once via bootstrap.js. Idempotent.
   ============================================================ */
(function () {
  'use strict';
  if (window.SidebarToggle) return;

  const KEY_PINNED = 'tickwave:sidebar:pinned';
  const MOBILE_BP = 768;

  function isMobile() { return window.innerWidth <= MOBILE_BP; }

  function applyPersisted(sidebar) {
    try {
      const pinned = localStorage.getItem(KEY_PINNED) === '1';
      if (pinned && !isMobile()) {
        sidebar.classList.add('sidebar--pinned-open');
      }
    } catch (_) {}
  }

  function mount() {
    const sidebar = document.querySelector('aside.sidebar, .sidebar');
    if (!sidebar) return;
    if (sidebar.querySelector('.sidebar-toggle')) return;  // already mounted

    applyPersisted(sidebar);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'sidebar-toggle';
    btn.setAttribute('aria-label', 'Pin/unpin sidebar');
    btn.title = 'Click to pin sidebar open';
    btn.innerHTML = '<span class="material-symbols-outlined">chevron_right</span>';
    // Append at the very end of the sidebar column. Original sidebar uses
    // flex-direction: column, so margin-top:auto in CSS pushes the toggle
    // to the bottom (clears the auth-status div that lives at the foot).
    sidebar.appendChild(btn);

    btn.addEventListener('click', () => {
      if (isMobile()) {
        // Mobile: close the drawer
        document.body.classList.remove('sidebar--open');
        sidebar.classList.remove('sidebar--mobile-open');
        sidebar.style.display = '';
      } else {
        const pinned = sidebar.classList.toggle('sidebar--pinned-open');
        btn.title = pinned ? 'Click to unpin' : 'Click to pin sidebar open';
        try { localStorage.setItem(KEY_PINNED, pinned ? '1' : '0'); } catch (_) {}
      }
    });

    // ---- Mobile: also mount a hamburger trigger in the page top area so
    // the user can OPEN the drawer (the sidebar's own toggle becomes a
    // close button once open). We only show the hamburger below 768px.
    let hamburger = document.querySelector('.sidebar-hamburger');
    if (!hamburger) {
      hamburger = document.createElement('button');
      hamburger.type = 'button';
      hamburger.className = 'sidebar-hamburger';
      hamburger.setAttribute('aria-label', 'Open menu');
      hamburger.innerHTML = '<span class="material-symbols-outlined">menu</span>';
      document.body.appendChild(hamburger);
      // CSS for hamburger lives here so we don't depend on disclosures.css
      const s = document.createElement('style');
      s.textContent = `
        .sidebar-hamburger {
          display: none;
          position: fixed;
          top: 12px;
          left: 12px;
          z-index: 60;
          width: 40px;
          height: 40px;
          border-radius: 10px;
          background: var(--surface-1);
          border: 1px solid var(--border-subtle);
          color: var(--text-primary);
          cursor: pointer;
          align-items: center;
          justify-content: center;
          box-shadow: var(--shadow-sm);
        }
        .sidebar-hamburger .material-symbols-outlined { font-size: 20px; }
        @media (max-width: 768px) {
          .sidebar-hamburger { display: flex; }
          /* Force-hide the sidebar by default on mobile, even if some
             page forgot the Tailwind \`hidden md:flex\` classes. */
          .sidebar:not(.sidebar--mobile-open) {
            transform: translateX(-110%);
            transition: transform 280ms cubic-bezier(0.16,1,0.3,1);
          }
          .sidebar.sidebar--mobile-open {
            display: flex !important;
            transform: translateX(0);
            transition: transform 280ms cubic-bezier(0.16,1,0.3,1);
          }
        }
      `;
      document.head.appendChild(s);
    }
    hamburger.addEventListener('click', () => {
      sidebar.classList.add('sidebar--mobile-open');
      // Tailwind's `hidden` sets display:none — override it on the element
      sidebar.style.display = 'flex';
      document.body.classList.add('sidebar--open');
    });

    // Backdrop click closes the drawer
    document.body.addEventListener('click', (e) => {
      if (!document.body.classList.contains('sidebar--open')) return;
      if (sidebar.contains(e.target)) return;
      if (hamburger.contains(e.target)) return;
      document.body.classList.remove('sidebar--open');
      sidebar.classList.remove('sidebar--mobile-open');
      sidebar.style.display = '';  // let CSS reassert
    });

    // Esc closes on mobile
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && document.body.classList.contains('sidebar--open')) {
        document.body.classList.remove('sidebar--open');
        sidebar.classList.remove('sidebar--mobile-open');
        sidebar.style.display = '';
      }
    });
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }
  ready(mount);

  window.SidebarToggle = { mount };
})();
