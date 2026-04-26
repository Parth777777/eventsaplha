// AlphaFX — tasteful GSAP animations across the app.
//
// Philosophy: small, fast, non-blocking. Animations should *reward* the eye,
// not get in the way. All helpers degrade silently to no-ops if GSAP fails to load.
//
// Usage: include AFTER app.js on any page. AlphaFX auto-runs a one-shot boot
// sequence on DOMContentLoaded and exposes helpers on window.AlphaFX.

(function () {
  if (typeof window === 'undefined') return;

  const GSAP_SRC = 'https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js';
  let gsapPromise = null;

  function loadGsap() {
    if (window.gsap) return Promise.resolve(window.gsap);
    if (gsapPromise) return gsapPromise;
    gsapPromise = new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = GSAP_SRC;
      s.onload = () => resolve(window.gsap || null);
      s.onerror = () => resolve(null);
      document.head.appendChild(s);
    });
    return gsapPromise;
  }

  async function withGsap(fn) {
    const gsap = await loadGsap();
    if (!gsap) return null;
    try { return fn(gsap); } catch (e) { return null; }
  }

  // Run a GSAP "from" callback only if GSAP is available *soon* (<400ms).
  // Prevents content from flashing off when GSAP loads after it's already rendered.
  async function withGsapFresh(fn, maxDelayMs = 400) {
    const t0 = Date.now();
    const gsap = await loadGsap();
    if (!gsap) return null;
    if (Date.now() - t0 > maxDelayMs) return null;
    try { return fn(gsap); } catch (e) { return null; }
  }

  // Prefer stagger animations on anything marked data-fx-card or .card / .sidebar-link on load
  async function bootPageFx() {
    const bootAt = Date.now();
    await withGsap((gsap) => {
      // If GSAP took longer than 400ms to load, the page is already fully
      // visible to the user — running `from({opacity:0})` would flash content
      // off and on, so bail.
      if (Date.now() - bootAt > 400) return;
      // Cards & primary surfaces fade + lift
      const cards = document.querySelectorAll(
        '[data-fx-card], .card, .card-animate, [data-alpha]'
      );
      if (cards.length) {
        gsap.from(cards, {
          opacity: 0,
          y: 14,
          duration: 0.45,
          ease: 'power2.out',
          stagger: 0.04,
          clearProps: 'opacity,transform',
        });
      }
      // Sidebar links subtle slide-in
      const links = document.querySelectorAll('.sidebar-link, [data-nav]');
      if (links.length) {
        gsap.from(links, {
          opacity: 0, x: -8, duration: 0.3, ease: 'power2.out', stagger: 0.03,
          clearProps: 'opacity,transform',
        });
      }
      // Big headline numbers — any [data-fx-count] gets a count-up
      document.querySelectorAll('[data-fx-count]').forEach((el) => {
        const target = parseFloat(el.dataset.fxCount || el.textContent || '0');
        el.textContent = '0';
        const obj = { v: 0 };
        gsap.to(obj, {
          v: target,
          duration: 0.9,
          ease: 'power2.out',
          onUpdate: () => { el.textContent = obj.v.toFixed(target % 1 === 0 ? 0 : 2); },
        });
      });
      // Pulse any critical-severity elements
      document.querySelectorAll('.fx-pulse-critical, [data-fx-pulse="critical"]').forEach((el) => {
        gsap.fromTo(el, { scale: 1 },
          { scale: 1.06, duration: 0.6, yoyo: true, repeat: 3, ease: 'sine.inOut', transformOrigin: 'center' });
      });
    });
  }

  // Popup open/close animations
  async function popupOpen(overlay) {
    if (!overlay) return;
    // Stamp open time so late-arriving GSAP can bail instead of re-hiding content
    const openedAt = Date.now();
    overlay.dataset._fxOpenedAt = String(openedAt);
    await withGsap((gsap) => {
      // If GSAP finally loaded after the popup is already visible for >400ms,
      // skip the "from" animation so we don't flash the content invisible.
      if (Date.now() - openedAt > 400) return;
      const content = overlay.querySelector('#stockPopupContent') || overlay.firstElementChild;
      gsap.fromTo(overlay, { opacity: 0 }, { opacity: 1, duration: 0.22, ease: 'power2.out' });
      if (content) {
        gsap.fromTo(content,
          { y: 40, scale: 0.96, opacity: 0 },
          { y: 0, scale: 1, opacity: 1, duration: 0.35, ease: 'back.out(1.4)' });
      }
    });
  }

  async function popupClose(overlay, done) {
    const gsap = await loadGsap();
    if (!gsap || !overlay) { if (done) done(); return; }
    const content = overlay.querySelector('#stockPopupContent') || overlay.firstElementChild;
    gsap.to(content, { y: 20, scale: 0.97, opacity: 0, duration: 0.2, ease: 'power2.in' });
    gsap.to(overlay, {
      opacity: 0, duration: 0.2, delay: 0.05, ease: 'power2.in',
      onComplete: () => { overlay.style.display = 'none'; overlay.style.opacity = ''; if (done) done(); },
    });
  }

  async function popupContentIn(contentEl) {
    await withGsap((gsap) => {
      if (!contentEl) return;
      // Stagger children sections
      const sections = contentEl.querySelectorAll(':scope > div > div, :scope > div');
      if (sections.length) {
        gsap.from(sections, {
          opacity: 0, y: 8, duration: 0.3, ease: 'power2.out', stagger: 0.03,
          clearProps: 'opacity,transform',
        });
      }
    });
  }

  // Counter: tween a DOM text node from 0 → target
  async function countUp(selector, target, decimals) {
    await withGsap((gsap) => {
      const el = typeof selector === 'string' ? document.querySelector(selector) : selector;
      if (!el) return;
      const t = parseFloat(target);
      if (isNaN(t)) return;
      const obj = { v: 0 };
      const d = decimals != null ? decimals : (t % 1 === 0 ? 0 : 2);
      gsap.to(obj, {
        v: t, duration: 0.9, ease: 'power2.out',
        onUpdate: () => { el.textContent = obj.v.toFixed(d); },
      });
    });
  }

  // SVG stroke-dashoffset tween for gauges
  async function gaugeFill(selector, targetOffset, circ) {
    await withGsap((gsap) => {
      const el = typeof selector === 'string' ? document.querySelector(selector) : selector;
      if (!el) return;
      el.setAttribute('stroke-dashoffset', String(circ));
      gsap.to(el, {
        attr: { 'stroke-dashoffset': targetOffset },
        duration: 1.0, ease: 'power2.out',
      });
    });
  }

  // Pulse-critical: draw attention to a single element
  async function pulseCritical(el) {
    await withGsap((gsap) => {
      if (!el) return;
      gsap.fromTo(el, { scale: 1 },
        { scale: 1.08, duration: 0.5, yoyo: true, repeat: 2, ease: 'sine.inOut', transformOrigin: 'center' });
    });
  }

  // Hover lift for any .fx-lift element (delegated)
  function wireHoverLift() {
    document.addEventListener('mouseover', async (e) => {
      const el = e.target.closest && e.target.closest('.fx-lift, [data-fx-lift]');
      if (!el || el.dataset._fxHover === '1') return;
      el.dataset._fxHover = '1';
      await withGsap((gsap) => {
        gsap.to(el, { y: -2, duration: 0.2, ease: 'power2.out' });
      });
    });
    document.addEventListener('mouseout', async (e) => {
      const el = e.target.closest && e.target.closest('.fx-lift, [data-fx-lift]');
      if (!el || el.dataset._fxHover !== '1') return;
      el.dataset._fxHover = '0';
      await withGsap((gsap) => {
        gsap.to(el, { y: 0, duration: 0.25, ease: 'power2.out' });
      });
    });
  }

  // Watch [data-alpha] containers for newly-rendered content and stagger-reveal
  function observeDataAlphaRenders() {
    const containers = document.querySelectorAll('[data-alpha]');
    const mo = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.addedNodes && m.addedNodes.length) {
          const direct = Array.from(m.addedNodes).filter((n) => n.nodeType === 1);
          if (direct.length) {
            // Use Fresh variant so a late GSAP load doesn't flash content
            withGsapFresh((gsap) => {
              gsap.from(direct, {
                opacity: 0, y: 6, duration: 0.3, ease: 'power2.out', stagger: 0.02,
                clearProps: 'opacity,transform',
              });
            });
          }
        }
      }
    });
    containers.forEach((c) => mo.observe(c, { childList: true, subtree: false }));
  }

  // Pulse critical-severity badges as they appear (delegate)
  function wirePulseBadges() {
    const pulseIfCritical = (root) => {
      root.querySelectorAll('.forensic-badge[data-score]').forEach((el) => {
        const sc = parseInt(el.dataset.score || '0', 10);
        if (sc >= 70 && !el.dataset._fxPulsed) {
          el.dataset._fxPulsed = '1';
          pulseCritical(el);
        }
      });
    };
    pulseIfCritical(document);
    const mo = new MutationObserver(() => pulseIfCritical(document));
    mo.observe(document.body, { childList: true, subtree: true });
  }

  function boot() {
    // Kick off GSAP load immediately — don't block
    loadGsap();
    bootPageFx();
    wireHoverLift();
    observeDataAlphaRenders();
    wirePulseBadges();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  window.AlphaFX = {
    loadGsap,
    popupOpen,
    popupClose,
    popupContentIn,
    countUp,
    gaugeFill,
    pulseCritical,
  };
})();
