const CACHE_NAME = "arabic-player-v23";
const STATIC_ASSETS = ["/", "/index.html", "/app.js", "/styles.css", "/manifest.json", "/config.js", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Always go network-first for API calls and JS/CSS so updates are picked up immediately
  if (
    url.hostname.includes("modal.run") ||
    url.hostname.includes("mahdid313") ||
    url.pathname.endsWith(".js") ||
    url.pathname.endsWith(".css")
  ) {
    e.respondWith(
      fetch(e.request).then((res) => {
        const clone = res.clone();
        caches.open(CACHE_NAME).then((c) => c.put(e.request, clone));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  // Network-first for HTML too so cached pages never get stale JS mismatches
  e.respondWith(
    fetch(e.request).then((res) => {
      const clone = res.clone();
      caches.open(CACHE_NAME).then((c) => c.put(e.request, clone));
      return res;
    }).catch(() => caches.match(e.request))
  );
});
