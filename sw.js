// PGI TaskFlow Service Worker
const CACHE_NAME = 'pgi-taskflow-v1';

// Only cache the shell — not API calls
const STATIC_ASSETS = [
  '/'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  // Never intercept API calls — always go to network
  if (event.request.url.includes('/api/')) {
    return;
  }

  // For page navigations — serve from cache first, fallback to network
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('/'))
    );
    return;
  }

  // Everything else — network first
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
