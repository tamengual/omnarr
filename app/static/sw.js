"use strict";
const version = new URL(self.location.href).searchParams.get("v") || "unknown";
const shellName = `omnarr-shell-${version}`;
const base = new URL("./", self.location.href);
const shellFiles = ["./", "index.html", "app.js", "player.js", "reader.js", "style.css", "manifest.webmanifest", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "vendor/epub.min.js", "vendor/jszip.min.js", "vendor/hls.min.js"];
const shellPaths = new Set(shellFiles.map((path) => new URL(path, base).pathname));
self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(shellName);
    await Promise.all(shellFiles.map(async (path) => {
      const url = new URL(path, base);
      const response = await fetch(url, { cache: "reload" });
      if (!response.ok) throw new Error(`Shell unavailable: ${path}`);
      await cache.put(url, response);
    }));
    await self.skipWaiting();
  })());
});
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) if (name.startsWith("omnarr-shell-") && name !== shellName) await caches.delete(name);
    await self.clients.claim();
  })());
});
self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) return;
  // Only the app shell and the readers' saved-offline URLs are ours. Everything else (audio and
  // video streams above all, which use Range requests that Safari mishandles behind a service
  // worker) goes straight to the network without the worker in the way.
  const inner = url.pathname.slice(base.pathname.length);
  if (!shellPaths.has(url.pathname) && !inner.startsWith("api/read/")) return;
  event.respondWith((async () => {
    // Bundle URLs are the sole API caching exception. Partial saves have no info marker.
    for (const name of await caches.keys()) {
      if (!name.startsWith("omnarr-offline-")) continue;
      const cache = await caches.open(name);
      const key = name.slice("omnarr-offline-".length);
      const marker = new URL(`api/read/info/${encodeURIComponent(key)}`, base);
      if (!await cache.match(marker)) continue;
      if (url.href === marker.href) {
        // The saved info holds "where you left off": ask the server first so reading done on
        // another device wins, and use the saved copy only when offline.
        try { const live = await fetch(request); if (live.ok) return live; } catch { /* offline */ }
        return await cache.match(marker);
      }
      const saved = await cache.match(request);
      if (saved) return saved;
    }
    if (!shellPaths.has(url.pathname)) return fetch(request);
    const cache = await caches.open(shellName);
    const cacheKey = new URL(url.pathname, url.origin);
    // Network first, but never wait long: a phone waking up with a slow connection would
    // otherwise sit on a blank screen. After 3 s the saved copy is used (and the fresh one
    // still updates the cache in the background for next time).
    const network = fetch(request).then(async (response) => {
      if (response.ok) await cache.put(cacheKey, response.clone());
      return response;
    });
    const timeout = new Promise((resolve) => setTimeout(() => resolve(null), 3000));
    try {
      const first = await Promise.race([network, timeout]);
      if (first && first.ok) return first;
      const saved = await cache.match(cacheKey);
      if (saved) { event.waitUntil(network.catch(() => {})); return saved; }
      return first || await network;
    } catch (error) {
      const saved = await cache.match(cacheKey);
      if (saved) return saved;
      throw error;
    }
  })());
});
