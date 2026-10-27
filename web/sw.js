// AdBot service worker: caches the app shell so it opens instantly; data (/api) is always live.
const CACHE = "adbot-shell-v21";
const SHELL = ["/", "/web/app.css", "/web/app.js", "/manifest.webmanifest", "/static/icon-192.png", "/static/apple-touch-icon.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return; // never cache data
  // network first (so updates show up right away), cache as fallback when offline
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && SHELL.includes(url.pathname)) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request).then(m => m || caches.match("/"))));
});
