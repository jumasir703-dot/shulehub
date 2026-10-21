// Service worker: makes the app installable and lets the timetable view and
// the guide open without data. Bump CACHE_NAME whenever style.css, csrf.js or
// this file changes, so devices pick up the new version.
const CACHE_NAME = "shulehub-cache-v7";

const APP_SHELL = [
  "/static/style.css",
  "/static/csrf.js",
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/guide",
  "/offline",
];

// Only these pages are ever stored for offline use. Everything else (students,
// attendance, fees, staff...) holds personal or financial data behind a login,
// so it is never written to the browser's cache. The cache is also emptied when
// someone logs out (see base.html).
const OFFLINE_OK = new Set(["/view", "/guide", "/offline"]);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// - Only GET requests are handled; POSTs go straight to the network.
// - File downloads (CSV/PDF/Excel) are left alone so the browser's save flow works.
// - Static assets: cache-first.
// - Pages: network-first. A response is only stored if it is an OFFLINE_OK
//   page and was a real 200 (not a redirect to the login page).
self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  const p = url.pathname;
  if (p.endsWith(".csv") || p.endsWith(".pdf") || p.endsWith(".xlsx")) return;

  if (p.startsWith("/static/")) {
    event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
    return;
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (OFFLINE_OK.has(p) && response.ok && !response.redirected) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() =>
        (OFFLINE_OK.has(p) ? caches.match(request) : Promise.resolve(null))
          .then((cached) => cached || caches.match("/offline"))
      )
  );
});
