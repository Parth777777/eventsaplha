/* Tickwave service worker — minimal offline-shell + push handler.
 * Keep this lean; production caching strategy lives at the CDN layer.
 */
const CACHE = 'tickwave-v1';
const SHELL = [
  '/',
  '/index.html',
  '/shared/css/style.css',
  '/shared/js/app.js',
  '/shared/js/widgets.js',
  '/shared/js/realtime.js',
  '/shared/js/theme.js',
  '/manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Never cache API or SSE — always live.
  if (url.pathname.startsWith('/api/')) return;
  // Stale-while-revalidate for shell assets only.
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const network = fetch(e.request).then((r) => {
        if (r && r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        }
        return r;
      }).catch(() => cached);
      return cached || network;
    })
  );
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
