const SHARE_CACHE = 'truth-button-share-v1';

self.addEventListener('install', () => { self.skipWaiting(); });
self.addEventListener('activate', (event) => { event.waitUntil(self.clients.claim()); });

// GitHub Pages is static-only and can't handle a real POST, so this intercepts
// the share-target request in the service worker before it ever hits the network,
// stashes the shared file in the Cache Storage API, then redirects to the app
// with a flag telling it to go pick that file up.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === 'POST' && url.pathname === '/share-target/') {
    event.respondWith(handleShareTarget(event));
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
