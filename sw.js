// Service Worker - 앱을 홈 화면에 설치된 것처럼 동작시키고,
// 한 번 방문한 리소스(HTML, OCR 엔진 파일 등)를 캐시해서 다음부터는 오프라인에서도 열리게 해줍니다.
const CACHE_NAME = 'panel-tag-scanner-v1';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

// 전략: 캐시 우선, 없으면 네트워크로 받아오고 캐시에 저장(다음번엔 오프라인에서도 사용 가능)
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached);
    })
  );
});
