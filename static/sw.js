/* Loop service worker: cache the app shell's static files so the
   installed app loads fast and survives a patchy connection. Pages
   themselves always go to the network first, so content never goes
   stale from the cache. No user data is cached here. */
const CACHE = 'loop-static-v3';
const STATIC_FILES = ['/static/style.css', '/static/icon.svg', '/manifest.json'];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(STATIC_FILES))
      .catch(() => {})
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) {
    return;
  }
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.json') {
    event.respondWith(
      caches.match(event.request).then((hit) => hit || fetch(event.request))
    );
  }
  // All other requests (pages, forms, uploads) go straight to the network.
});
