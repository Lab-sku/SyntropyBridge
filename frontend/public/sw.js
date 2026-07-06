/* L15: PWA service worker — network-first for navigation, cache-first
 * for immutable build assets (/assets/*.js|css). API requests are never
 * cached so user data / billing state stays fresh.
 */
const CACHE_VERSION = 'apihub-v1';
const PRECACHE_URLS = ['/', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then((cache) => cache.addAll(PRECACHE_URLS).catch(() => undefined))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))),
      )
      // Multi-dim review fix: purge stale /assets/ entries from the
      // current cache. Vite emits content-hashed filenames, so old
      // entries are orphans after a deploy — without this cleanup
      // they would accumulate indefinitely and eventually exhaust
      // the browser storage quota.
      .then(() => caches.open(CACHE_VERSION))
      .then((cache) =>
        cache.keys().then((reqs) =>
          Promise.all(
            reqs
              .filter((r) => new URL(r.url).pathname.startsWith('/assets/'))
              .map((r) => cache.delete(r)),
          ),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  // Never cache API / proxy / docs / health requests.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/v1/') ||
    url.pathname.startsWith('/docs') ||
    url.pathname.startsWith('/health')
  ) {
    return;
  }

  // Cache-first for build assets (immutable, content-hashed).
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.match(req).then(
        (cached) =>
          cached ||
          fetch(req).then((resp) => {
            if (resp.ok) {
              const copy = resp.clone();
              caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
            }
            return resp;
          }),
      ),
    );
    return;
  }

  // Network-first for navigation + other same-origin GETs.
  if (req.mode === 'navigate' || url.origin === self.location.origin) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          if (resp.ok && resp.type === 'basic') {
            const copy = resp.clone();
            caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          }
          return resp;
        })
        .catch(() => caches.match(req).then((c) => c || caches.match('/'))),
    );
  }
});
