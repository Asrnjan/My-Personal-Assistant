// Service worker: offline app shell, API network-first, Web Push.
'use strict';

const VERSION = 'v6';
const SHELL_CACHE = `cos-shell-${VERSION}`;
const DATA_CACHE = `cos-data-${VERSION}`;
const FONT_CACHE = `cos-fonts-${VERSION}`;

const SHELL_FILES = [
  './',
  'index.html',
  'app.js',
  'cluster.js',
  'styles.css',
  'manifest.webmanifest',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'icons/maskable-512.png',
  'icons/apple-touch-icon-180.png',
  'icons/badge-96.png',
];

const scopeUrl = (path) => new URL(path, self.registration.scope).href;
const SHELL_URLS = new Set(SHELL_FILES.map(scopeUrl));
// API reads worth keeping for offline viewing.
const CACHEABLE_API = ['/brief/today', '/followups', '/actions'];
const FONT_HOSTS = new Set(['fonts.googleapis.com', 'fonts.gstatic.com']);

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_FILES.map(scopeUrl)))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  const keep = new Set([SHELL_CACHE, DATA_CACHE, FONT_CACHE]);
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('cos-') && !keep.has(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function cacheFirst(request, cacheName, fallbackUrl) {
  const cache = await caches.open(cacheName);
  const cached = (await cache.match(request, { ignoreSearch: request.mode === 'navigate' }))
    || (fallbackUrl ? await cache.match(fallbackUrl) : undefined);
  const network = fetch(request)
    .then((res) => {
      // Refresh the cached copy in the background (stale-while-revalidate).
      if (res && res.ok) cache.put(request.mode === 'navigate' && fallbackUrl ? fallbackUrl : request, res.clone());
      return res;
    })
    .catch(() => undefined);
  if (cached) return cached;
  const res = await network;
  if (res) return res;
  return new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
}

async function networkFirst(request) {
  const cache = await caches.open(DATA_CACHE);
  try {
    const res = await fetch(request);
    if (res && res.ok) cache.put(request, res.clone());
    return res;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ detail: 'You are offline' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);

  if (FONT_HOSTS.has(url.hostname)) {
    event.respondWith(cacheFirst(request, FONT_CACHE));
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/ws' || url.pathname.startsWith('/tts')) return;

  const bare = url.origin + url.pathname;
  // Only the app's own page is served from the cache. Other pages on the same server
  // (/docs, /health, the OAuth callback) must load from the backend.
  if (request.mode === 'navigate') {
    if (SHELL_URLS.has(bare)) event.respondWith(cacheFirst(request, SHELL_CACHE, scopeUrl('index.html')));
    return;
  }
  if (SHELL_URLS.has(bare)) {
    event.respondWith(cacheFirst(request, SHELL_CACHE));
    return;
  }
  if (CACHEABLE_API.some((p) => url.pathname === p || url.pathname.startsWith(`${p}/`))) {
    event.respondWith(networkFirst(request));
  }
  // Everything else (other API calls) goes straight to the network.
});

self.addEventListener('push', (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch {
      data = { body: event.data.text() };
    }
  }
  const title = data.title || 'Chief of Staff';
  const options = {
    body: data.body || 'Your brief is ready.',
    icon: scopeUrl('icons/icon-192.png'),
    badge: scopeUrl('icons/badge-96.png'),
    tag: data.tag || 'cos-brief',
    renotify: true,
    data: { url: data.url || '/?autostart=brief' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || '/?autostart=brief', self.location.origin).href;
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of all) {
      if (new URL(client.url).origin === self.location.origin && 'focus' in client) {
        await client.focus();
        client.postMessage({ type: 'navigate', url: target });
        return;
      }
    }
    if (self.clients.openWindow) await self.clients.openWindow(target);
  })());
});
