// FX Alert PWA - Service Worker
// 최소 조건 충족용 (fetch handler 필수)

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// 🔥 핵심: fetch 핸들러가 반드시 있어야 PWA로 인정됨
self.addEventListener("fetch", (event) => {
  // 네트워크 그대로 통과 (캐시 안 함)
  event.respondWith(fetch(event.request));
});
