const SHARE_CACHE = 'truth-button-share-v1';
const SHELL_CACHE = 'truth-button-shell-v1';
// Just the core app shell — the page every tool (including the Offline MP3
// Library) actually lives on. Deliberately NOT precaching the per-tool
// libraries (pdf-lib.min.js, pdf.min.js, lame.min.js, qrcode.min.js) or the
// other standalone landing pages — those tools already require network for
// other reasons (e.g. backend API calls), so caching just the shell is what
// makes the fully-client-side tools (like the MP3 library) actually work
// offline, without taking on the scope of a full site-wide offline mode.
const SHELL_FILES = ['/', '/index.html', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_FILES))
      .then(() => self.skipWaiting())
  );
});
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((name) => name !== SHARE_CACHE && name !== SHELL_CACHE).map((name) => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

// GitHub Pages is static-only and can't handle a real POST, so this intercepts
// the share-target request in the service worker before it ever hits the network,
// stashes the shared file in the Cache Storage API, then redirects to the app
// with a flag telling it to go pick that file up.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === 'POST' && url.pathname === '/share-target/') {
    event.respondWith(handleShareTarget(event));
    return;
  }

  // Network-first for the app shell, falling back to cache — stays fresh
  // whenever online, and is what actually makes the page (and every
  // fully-client-side tool on it) load with zero network once offline.
  if (event.request.method === 'GET' && SHELL_FILES.includes(url.pathname)) {
    event.respondWith(
      fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      }).catch(() => caches.match(event.request))
    );
  }
});

async function handleShareTarget(event) {
  try {
    const formData = await event.request.formData();
    const file = formData.get('shared_media');
    if (file) {
      const cache = await caches.open(SHARE_CACHE);
      await cache.put('/shared-file', new Response(file, { headers: { 'Content-Type': file.type, 'X-File-Name': encodeURIComponent(file.name || '') } }));
    }
  } catch (e) { /* fall through to redirect regardless — the app shows its own error if nothing was cached */ }
  return Response.redirect('/?shared=1', 303);
}
