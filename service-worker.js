const offlineCache = 'lazy-remote-offline-v2';
const offlinePage = '/offline.html';
const offlineAssets = ['/remote.css', '/assets/logo.svg'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(offlineCache).then(cache => cache.addAll([offlinePage, ...offlineAssets])));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith('lazy-remote-offline-') && name !== offlineCache) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  // The control UI and all API requests stay on the network. Only the offline
  // notice and its public assets can be served from the cache.
  const home = request.mode === 'navigate' && (url.pathname === '/' || url.pathname === '/index.html');
  if (!home && !offlineAssets.includes(url.pathname)) return;
  event.respondWith(fetch(request).catch(async error => {
    const cache = await caches.open(offlineCache);
    const response = await cache.match(home ? offlinePage : url.pathname);
    if (response) return response;
    throw error;
  }));
});
