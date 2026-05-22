/* ============================================================
   COMPLIANCE MODAL — first-load SEBI disclosure acknowledgement
   ============================================================

   Shows ONCE per acknowledged version. Versioned localStorage key
   (`tickwave:compliance-ack:v1`) so we can re-prompt when copy
   materially changes.

   Wired from bootstrap.js — auto-mounts on every page load unless:
     - localStorage has the current version ack, OR
     - we're already on /disclosures.html / /login / /signup (no
       point gating those entry pages)

   Styling lives in app/shared/css/disclosures.css (loaded by
   bootstrap.js). Markup uses class names from that stylesheet so
   nothing is hardcoded here.
   ============================================================ */
(function () {
  'use strict';
  if (window.ComplianceModal) return;

  const KEY = 'tickwave:compliance-ack:v1';
  const SKIP_PATHS = /(disclosures|login|signup|methodology)\.html$/i;

  function alreadyAcked() {
    try { return !!localStorage.getItem(KEY); } catch (_) { return false; }
  }

  function ack() {
    try { localStorage.setItem(KEY, JSON.stringify({ at: new Date().toISOString() })); } catch (_) {}
  }

  /* Returning user heuristic — if any tickwave:* localStorage key already
     exists, the user has clearly used the app before and doesn't need a
     first-load modal. Auto-ack instead so the modal stays out of the way.
     The persistent footer + per-card disclaimer still cover SEBI visibility. */
  function isReturningUser() {
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i) || '';
        if (k.startsWith('tickwave:') && k !== KEY) return true;
      }
    } catch (_) {}
    return false;
  }

  function mount() {
    if (alreadyAcked()) return;
    if (isReturningUser()) { ack(); return; }
    if (SKIP_PATHS.test(location.pathname)) return;
    if (document.getElementById('twComplianceModal')) return;

    const backdrop = document.createElement('div');
    backdrop.id = 'twComplianceModal';
    backdrop.className = 'compliance-modal__backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-labelledby', 'twComplianceTitle');

    backdrop.innerHTML = `
      <div class="compliance-modal__card">
        <div class="compliance-modal__title" id="twComplianceTitle">
          <span class="material-symbols-outlined">gavel</span>
          Before you continue
        </div>
        <div class="compliance-modal__body">
          AlphaEvent provides <strong>quantitative research and informational signals</strong>
          derived from public Indian market data. We are <strong>not</strong> a
          SEBI-registered Investment Adviser or Research Analyst, and nothing
          shown here is a recommendation to buy or sell.
          <br><br>
          Past performance does not guarantee future results. Consult a
          SEBI-registered RA or IA, and assess your own risk tolerance, before
          acting on any information.
        </div>
        <div class="compliance-modal__actions">
          <a class="compliance-modal__btn compliance-modal__btn--secondary"
             href="disclosures.html">Read full disclosures</a>
          <button class="compliance-modal__btn compliance-modal__btn--primary"
                  type="button" id="twComplianceAck">I understand</button>
        </div>
      </div>`;

    document.body.appendChild(backdrop);

    // Focus the primary button so keyboard users can press Enter
    const btn = backdrop.querySelector('#twComplianceAck');
    setTimeout(() => { try { btn.focus(); } catch (_) {} }, 50);

    function close() {
      ack();
      try { backdrop.remove(); } catch (_) {}
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) {
      // Esc closes AND acks — re-prompting on every nav was too aggressive.
      // The full disclaimer is still in the persistent footer + every
      // signal card, so SEBI visibility isn't lost just because the modal
      // got dismissed via keyboard.
      if (e.key === 'Escape') close();
    }
    btn.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    // Clicking the backdrop dismisses (and acks) — was no-op before but
    // the modal kept reappearing on every page nav, which felt like a bug.
    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) close();
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

  window.ComplianceModal = { mount, ack };
})();
