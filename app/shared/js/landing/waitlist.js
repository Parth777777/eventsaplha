/* waitlist.js — handles the waitlist form on the landing page.
 *
 * Flow:
 *  1. User enters email → POST /api/waitlist/join
 *  2. On success: swap form for a "You're #N" card with copyable referral link
 *  3. Auto-applies ?ref=... from URL if present
 */

const $  = (s, r = document) => r.querySelector(s);

function setMsg(text, ok = false) {
  const m = $('#lp-form-msg');
  if (!m) return;
  m.textContent = text || '';
  m.classList.toggle('ok', !!ok);
}

function showSuccess({ position, share_url, founder_locked }) {
  $('#lp-waitlist-form').setAttribute('hidden', '');
  const card = $('#lp-waitlist-success');
  if (!card) return;
  $('#lp-success-num').textContent = position.toLocaleString('en-IN');
  $('#lp-success-msg').textContent = founder_locked
    ? 'Founder pricing locked for life.'
    : 'You\'re in the queue.';
  $('#lp-share-input').value = share_url || window.location.origin + window.location.pathname;
  card.removeAttribute('hidden');
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function onSubmit(e) {
  e.preventDefault();
  const emailEl = $('#lp-email');
  const email = (emailEl.value || '').trim();
  if (!email || !/.+@.+\..+/.test(email)) {
    setMsg('Looks like an invalid email. Try again?');
    emailEl.focus();
    return;
  }
  setMsg('Reserving your spot…', true);

  const ref = new URLSearchParams(location.search).get('ref') || '';
  try {
    const r = await fetch('/api/waitlist/join', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, ref, source: 'landing' }),
    });
    const j = await r.json();
    if (!r.ok || !j.success) {
      setMsg(j.error || 'Something went wrong. Try again in a moment.');
      return;
    }
    setMsg('Welcome aboard.', true);
    showSuccess({
      position:       j.position,
      share_url:      j.share_url,
      founder_locked: j.founder_locked,
    });
    // Cheap analytics — fire even when Plausible isn't installed.
    try { window.plausible && window.plausible('waitlist_submit', { props: { founder: j.founder_locked } }); } catch (_) {}
  } catch (err) {
    setMsg("Network issue — try again in a few seconds.");
  }
}

function wireCopy() {
  const btn = $('#lp-share-copy');
  const input = $('#lp-share-input');
  if (!btn || !input) return;
  btn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      btn.textContent = 'Copied';
      setTimeout(() => btn.innerHTML = '<span class="material-symbols-outlined">content_copy</span> Copy', 1400);
    } catch (_) {
      input.select();
      document.execCommand && document.execCommand('copy');
    }
  });
}

export function initWaitlist() {
  const form = $('#lp-waitlist-form');
  if (!form) return;
  form.addEventListener('submit', onSubmit);
  wireCopy();
}
