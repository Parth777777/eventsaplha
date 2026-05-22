/* ae-components.js — canonical vanilla custom elements.
 *
 * Loaded once site-wide via bootstrap.js. No framework, no Shadow DOM
 * (so the global theme cascade reaches every element).
 *
 * Registered tags:
 *   <ae-card variant="surface|elevated|interactive">
 *   <ae-button variant="primary|secondary|ghost|danger" size="sm|md|lg" loading>
 *   <ae-skeleton w h shape="text|rect|circle">
 *   <ae-empty-state mode="loading|empty|error" icon title description cta-label cta-href>
 *   <ae-error-boundary>      (use el.mount(asyncFn) to wrap a fetch)
 *   <ae-signal-row ticker score change reason href why-href>
 *   <ae-toast type="info|success|warn|error">
 *   <ae-tab-bar>             (children = <button class="ae-tab" data-value="X">)
 *
 * Global API:
 *   window.toast(message, opts) — convenience for ae-toast singleton container
 */
(function () {
  'use strict';
  if (window.__aeComponentsLoaded) return;
  window.__aeComponentsLoaded = true;

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  // ── <ae-card> ──────────────────────────────────────────────────
  class AeCard extends HTMLElement {
    connectedCallback() {
      // No-op — styling is pure CSS. Children render as-is.
      // Optional convenience: a `title` attr renders a header automatically.
      const title = this.getAttribute('title');
      if (title && !this.querySelector('.ae-card-header')) {
        const h = document.createElement('div');
        h.className = 'ae-card-header';
        h.innerHTML = `<div class="ae-card-title">${esc(title)}</div>`;
        this.prepend(h);
      }
    }
  }

  // ── <ae-button> ────────────────────────────────────────────────
  class AeButton extends HTMLElement {
    static get observedAttributes() { return ['loading', 'disabled']; }
    connectedCallback() {
      if (this._btn) return;
      const variant = this.getAttribute('variant') || 'primary';
      const size = this.getAttribute('size') || 'md';
      const type = this.getAttribute('type') || 'button';
      const label = this.textContent.trim();
      this.textContent = '';
      const btn = document.createElement('button');
      btn.type = type;
      btn.className = 'ae-btn';
      btn.setAttribute('data-variant', variant);
      btn.setAttribute('data-size', size);
      btn.textContent = label;
      if (this.hasAttribute('disabled')) btn.disabled = true;
      if (this.hasAttribute('loading')) btn.setAttribute('data-loading', '');
      this._btn = btn;
      this.appendChild(btn);
    }
    attributeChangedCallback(name) {
      if (!this._btn) return;
      if (name === 'loading') {
        if (this.hasAttribute('loading')) this._btn.setAttribute('data-loading', '');
        else this._btn.removeAttribute('data-loading');
      }
      if (name === 'disabled') this._btn.disabled = this.hasAttribute('disabled');
    }
  }

  // ── <ae-skeleton> ──────────────────────────────────────────────
  class AeSkeleton extends HTMLElement {
    connectedCallback() {
      const w = this.getAttribute('w') || '100%';
      const h = this.getAttribute('h') || '14px';
      const shape = this.getAttribute('shape') || 'rect';
      this.className = 'skel';
      this.style.display = 'block';
      this.style.width = w;
      this.style.height = h;
      if (shape === 'circle') {
        this.style.borderRadius = '50%';
        this.style.width = h; // keep square
      } else if (shape === 'text') {
        this.style.height = '12px';
        this.style.margin = '6px 0';
      }
    }
  }

  // ── <ae-empty-state> ───────────────────────────────────────────
  class AeEmptyState extends HTMLElement {
    static get observedAttributes() { return ['mode', 'title', 'description', 'icon', 'cta-label', 'cta-href']; }
    connectedCallback() { this._render(); }
    attributeChangedCallback() { if (this.isConnected) this._render(); }
    _render() {
      const mode = this.getAttribute('mode') || 'empty';
      if (mode === 'loading') {
        this.innerHTML = `
          <span class="ae-es-skel"></span>
          <span class="ae-es-skel"></span>
          <span class="ae-es-skel"></span>
        `;
        return;
      }
      const icon = this.getAttribute('icon') || (mode === 'error' ? 'error' : 'inbox');
      const title = this.getAttribute('title') || (mode === 'error' ? "Couldn't load this" : 'Nothing here yet');
      const desc = this.getAttribute('description') || (mode === 'error'
        ? 'Network or server issue. Try again or report it.'
        : 'Once data arrives, it will appear here.');
      const ctaLabel = this.getAttribute('cta-label') || (mode === 'error' ? 'Retry' : '');
      const ctaHref = this.getAttribute('cta-href') || '';
      const secondary = mode === 'error'
        ? '<a class="ae-es-secondary" href="mailto:support@alphaevent.in?subject=AlphaEvent%20issue">Report issue</a>'
        : '';
      let cta = '';
      if (ctaLabel) {
        cta = ctaHref
          ? `<a class="ae-es-cta" href="${esc(ctaHref)}">${esc(ctaLabel)}</a>`
          : `<button class="ae-es-cta" data-ae-retry>${esc(ctaLabel)}</button>`;
      }
      this.innerHTML = `
        <span class="material-symbols-outlined ae-es-icon">${esc(icon)}</span>
        <div class="ae-es-title">${esc(title)}</div>
        <div class="ae-es-desc">${esc(desc)}</div>
        <div>${cta}${secondary}</div>
      `;
      const retry = this.querySelector('[data-ae-retry]');
      if (retry) {
        retry.addEventListener('click', () => {
          this.dispatchEvent(new CustomEvent('retry', { bubbles: true }));
        });
      }
    }
  }

  // ── <ae-error-boundary> ────────────────────────────────────────
  // Usage: const el = document.querySelector('ae-error-boundary');
  //        el.mount(async () => { const data = await fetch(...).then(r=>r.json()); return renderHTML(data); });
  class AeErrorBoundary extends HTMLElement {
    connectedCallback() {
      this._original = this.innerHTML;
      this._target = this.getAttribute('target') || '';
    }
    async mount(asyncFn) {
      this._fn = asyncFn;
      this.innerHTML = `<ae-empty-state mode="loading"></ae-empty-state>`;
      try {
        const result = await asyncFn();
        if (typeof result === 'string') this.innerHTML = result;
        else if (result instanceof Node) { this.innerHTML = ''; this.appendChild(result); }
        else if (result === null || result === undefined) {
          this.innerHTML = `<ae-empty-state mode="empty"></ae-empty-state>`;
        }
      } catch (err) {
        const tname = this._target ? esc(this._target) : 'this section';
        this.innerHTML = `
          <ae-empty-state mode="error"
            title="Couldn't load ${tname}"
            description="${esc(err.message || 'Unknown error')}"
            cta-label="Retry"></ae-empty-state>
        `;
        const es = this.querySelector('ae-empty-state');
        if (es) es.addEventListener('retry', () => this.mount(this._fn));
      }
    }
  }

  // ── <ae-signal-row> ────────────────────────────────────────────
  class AeSignalRow extends HTMLElement {
    static get observedAttributes() { return ['ticker', 'score', 'change', 'reason', 'href', 'why-href']; }
    connectedCallback() { this._render(); }
    attributeChangedCallback() { if (this.isConnected) this._render(); }
    _render() {
      const ticker = this.getAttribute('ticker') || '—';
      const score = this.getAttribute('score');
      const change = this.getAttribute('change');
      const reason = this.getAttribute('reason') || '';
      const href = this.getAttribute('href');
      const whyHref = this.getAttribute('why-href');
      const changeNum = parseFloat(change);
      const changeClass = isNaN(changeNum) ? '' : (changeNum >= 0 ? 'bull' : 'bear');
      const changeStr = isNaN(changeNum) ? '' :
        `${changeNum >= 0 ? '+' : ''}${changeNum.toFixed(2)}%`;
      const inner = `
        <div class="ae-sr-ticker">${esc(ticker)}</div>
        <div class="ae-sr-body">
          <div class="ae-sr-reason">${esc(reason)}</div>
          <div class="ae-sr-meta">
            ${whyHref ? `<a class="ae-sr-why" href="${esc(whyHref)}">Why?</a>` : ''}
          </div>
        </div>
        ${changeStr ? `<div class="ae-sr-change ${changeClass}">${changeStr}</div>` : ''}
        ${score ? `<div class="ae-sr-score">${esc(score)}</div>` : ''}
      `;
      if (href) {
        if (this.tagName === 'AE-SIGNAL-ROW') {
          // Wrap content in anchor by rendering as <a>-styled element
          this.style.cursor = 'pointer';
          this.addEventListener('click', () => { window.location.href = href; });
        }
      }
      this.innerHTML = inner;
    }
  }

  // ── <ae-toast> + window.toast() ────────────────────────────────
  class AeToast extends HTMLElement {
    connectedCallback() {
      if (this._mounted) return;
      const msg = (this.getAttribute('message') || this.textContent || '').trim();
      // HARDEN 2026-05-18: an empty toast was rendering as a blank top-strip
      // (em-dash + × only) and blocking the page. Bail entirely if there's
      // no message — caller bug or stale window.toast('') call.
      if (!msg) {
        try { this.remove(); } catch (_) {}
        return;
      }
      this._mounted = true;
      this.textContent = '';
      const m = document.createElement('div');
      m.className = 'ae-toast-msg';
      m.textContent = msg;
      const x = document.createElement('button');
      x.className = 'ae-toast-x';
      x.type = 'button';
      x.setAttribute('aria-label', 'Dismiss');
      x.textContent = '×';
      x.addEventListener('click', () => this.remove());
      this.appendChild(m);
      this.appendChild(x);
      const ttl = parseInt(this.getAttribute('ttl') || '4500', 10);
      if (ttl > 0) setTimeout(() => this.remove(), ttl);
    }
  }
  function getToastContainer() {
    let c = document.getElementById('ae-toast-container');
    if (!c) {
      c = document.createElement('div');
      c.id = 'ae-toast-container';
      document.body.appendChild(c);
    }
    return c;
  }
  window.toast = function (message, opts = {}) {
    // HARDEN 2026-05-18: silently ignore empty / whitespace-only toast calls
    // instead of mounting a blank popup that blocks the viewport.
    const s = (message == null ? '' : String(message)).trim();
    if (!s) return null;
    const t = document.createElement('ae-toast');
    t.setAttribute('type', opts.type || 'info');
    t.setAttribute('message', s);
    if (opts.ttl != null) t.setAttribute('ttl', String(opts.ttl));
    getToastContainer().appendChild(t);
    return t;
  };

  // ── <ae-tab-bar> ───────────────────────────────────────────────
  // Children must be `<button class="ae-tab" data-value="X">Label</button>`.
  // Fires a `change` event on selection with detail.value.
  class AeTabBar extends HTMLElement {
    connectedCallback() {
      if (this._bound) return;
      this._bound = true;
      this.addEventListener('click', (e) => {
        const tab = e.target.closest('.ae-tab');
        if (!tab) return;
        this.querySelectorAll('.ae-tab').forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        this.dispatchEvent(new CustomEvent('change', {
          detail: { value: tab.dataset.value || tab.textContent.trim() },
          bubbles: true
        }));
      });
    }
    get value() {
      const a = this.querySelector('.ae-tab.active');
      return a ? (a.dataset.value || a.textContent.trim()) : null;
    }
    set value(v) {
      this.querySelectorAll('.ae-tab').forEach((t) => {
        t.classList.toggle('active', (t.dataset.value || t.textContent.trim()) === v);
      });
    }
  }

  // ── Register all ───────────────────────────────────────────────
  // TOAST FEATURE FULLY DISABLED 2026-05-18 (user request: "remove that
  // feature in general"). ae-toast is no longer registered and any
  // existing tag in the DOM becomes inert / invisible (CSS hides it,
  // window.toast is a no-op). Re-add `['ae-toast', AeToast]` to restore.
  const defs = [
    ['ae-card', AeCard],
    ['ae-button', AeButton],
    ['ae-skeleton', AeSkeleton],
    ['ae-empty-state', AeEmptyState],
    ['ae-error-boundary', AeErrorBoundary],
    ['ae-signal-row', AeSignalRow],
    ['ae-tab-bar', AeTabBar],
  ];
  defs.forEach(([name, cls]) => {
    if (!customElements.get(name)) customElements.define(name, cls);
  });

  // window.toast is now a hard no-op — every call across the codebase
  // becomes silent. Prevents any code path from creating new popups.
  window.toast = function () { return null; };

  // Sweep the DOM in case the cached SW already created ae-toast nodes
  // before this script ran. Belt-and-suspenders for the active session.
  try {
    document.querySelectorAll('ae-toast, #ae-toast-container').forEach(n => n.remove());
  } catch (_) {}
})();
