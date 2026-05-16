/* Tickwave service worker — minimal offline-shell + push handler.
 * Keep this lean; production caching strategy lives at the CDN layer.
 */
// Bumped to v21 — popup got mini-chart + deep-detail sections + logo chip,
// and the auto-decorator landed in company-logo.js. The previous v20 cache
// was serving stale popup JS that triggered "Couldn't fetch quick info."
// on every ticker click.
const CACHE = 'tickwave-v26-edge-features';
const SHELL = [
  '/',
  '/index.html',
  '/shared/css/style.css',
  '/shared/css/refined.css',
  '/shared/css/search-bar.css',
  '/shared/js/app.js',
  '/shared/js/widgets.js',
  '/shared/js/realtime.js',
  '/shared/js/theme.js',
  '/shared/js/event-card.js',
  '/shared/js/charts.js',
  '/shared/js/search-bar.js',
  '/shared/js/stock-popup.js',
  '/shared/js/company-logo.js',
  '/shared/js/section-tabs.js',
  '/shared/js/edge-analyzer.js',
  '/manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    // Drop every old cache so stale shell assets can't be served again.
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
    // Tell every controlled client to reload once so they pick up the
    // freshly-cached shell instead of running with stale JS/CSS.
    const clients = await self.clients.matchAll({ type: 'window' });
    clients.forEach((c) => {
      try { c.postMessage({ type: 'sw-activated', cache: CACHE }); } catch (_) {}
    });
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
  const url = new URL(e.request.url);
  // Never cache API or SSE — always live.
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
