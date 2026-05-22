/* Tickwave service worker — SELF-UNREGISTER MODE (2026-05-18)
 *
 * The user reported a blank popup that kept reappearing because the cached
 * SW shell was serving stale bootstrap.js + ae-components.js even after
 * source fixes shipped. We're nuking the SW entirely: on activate, every
 * cache is dropped, every client is told to reload, and the SW itself
 * unregisters so the next page load runs with NO worker in the way.
 *
 * Re-enable offline caching later by reverting this commit. For now, kill
 * order: caches → clients reload → registration.unregister().
 */
const CACHE = 'tickwave-v43-stockpro-mojibake-fix';
const SHELL = [
  '/',
  '/index.html',
  '/shared/css/tokens.css',
  '/shared/css/style.css',
  '/shared/css/redesign.css',
  '/shared/css/components.css',
  '/shared/css/disclosures.css',
  '/shared/css/search-bar.css',
  '/shared/js/app.js',
  '/shared/js/bootstrap.js',
  '/shared/js/widgets.js',
  '/shared/js/realtime.js',
  '/shared/js/theme.js',
  '/shared/js/theme-toggle.js',
  '/shared/js/theme-tokens.js',
  '/shared/js/event-card.js',
  '/shared/js/charts.js',
  '/shared/js/search-bar.js',
  '/shared/js/stock-popup.js',
  '/shared/js/stock-pro.js',
  '/shared/js/company-logo.js',
  '/shared/js/section-tabs.js',
  '/shared/js/edge-analyzer.js',
  '/shared/js/sidebar.js',
  '/shared/js/sidebar-toggle.js',
  '/shared/js/ae-bottom-nav.js',
  '/shared/js/components/ae-components.js',
  '/manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    // 1. DROP EVERY CACHE — including this one. No more stale serves.
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.clients.claim();
    // 2. Tell every controlled client to reload once with the fresh code.
    const clients = await self.clients.matchAll({ type: 'window' });
    clients.forEach((c) => {
      try { c.postMessage({ type: 'sw-activated', cache: CACHE }); } catch (_) {}
    });
    // 3. UNREGISTER SELF — next navigation runs with NO service worker,
    // so the browser always hits the network and ships the latest JS/CSS.
    try { await self.registration.unregister(); } catch (_) {}
  })());
});

// All branches MUST resolve to a Response. Returning undefined to
// e.respondWith crashes the fetch with "Failed to convert value to 'Response'"
// and the browser then shows a network-error page (the source of the
// stock.html?t=BSE failure we saw).
const OFFLINE_RESPONSE = () => new Response(
  '<!doctype html><meta charset=utf-8><title>Offline</title><h1>Offline</h1>',
  { status: 504, statusText: 'Offline and not cached',
    headers: { 'Content-Type': 'text/html; charset=utf-8' } }
);

self.addEventListener('fetch', (e) => {
  // KILL SWITCH 2026-05-18: don't intercept ANY fetches. Lets the browser
  // hit the network directly so the stale popup JS can't be served from
  // cache. The SW will unregister itself on activate anyway.
  return;
  // ---- unreachable below; kept as reference for re-enabling later ----
  // eslint-disable-next-line no-unreachable
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return;
  if (e.request.method !== 'GET') return;
  // Network-first for JS/CSS so code changes ship immediately. Fall back to
  // cache when offline so the shell still loads. HTML and other shell assets
  // use stale-while-revalidate as before.
  const isCode = /\.(js|css)(\?|$)/.test(url.pathname);
  if (isCode) {
    e.respondWith((async () => {
      try {
        const r = await fetch(e.request);
        if (r && r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        }
        return r;
      } catch (_) {
        const cached = await caches.match(e.request);
        return cached || OFFLINE_RESPONSE();
      }
    })());
    return;
  }
  e.respondWith((async () => {
    const cached = await caches.match(e.request);
    if (cached) {
      // Background revalidate so cache stays warm.
      fetch(e.request).then((r) => {
        if (r && r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        }
      }).catch(() => {});
      return cached;
    }
    try {
      const r = await fetch(e.request);
      if (r && r.ok) {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      }
      return r;
    } catch (_) {
      return OFFLINE_RESPONSE();
    }
  })());
});

// Web Push: server posts JSON to push subscription endpoint
self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch (err) {}
  const title = data.title || 'Tickwave alert';
  const options = {
    body: data.body || data.headline || '',
    icon: data.icon || undefined,
    badge: data.badge || undefined,
    tag: data.tag || (data.event_id || data.kind || 'alert'),
    data: data,
    requireInteraction: !!data.critical,
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = (e.notification.data && (e.notification.data.url || (e.notification.data.event_id ? '/events.html' : '/index.html'))) || '/index.html';
  e.waitUntil(
    clients.matchAll({ type: 'window' }).then((wins) => {
      for (const w of wins) {
        if ('focus' in w) { w.navigate(target); return w.focus(); }
      }
      return clients.openWindow(target);
    })
  );
});
