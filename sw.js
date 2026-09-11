// Service Worker v2
// 전략 변경:
// - 앱 페이지(index.html/네비게이션): "네트워크 우선" -> 인터넷이 되면 항상 최신 버전을 받아오고,
//   인터넷이 안 될 때만 저장해둔(캐시) 예전 버전을 보여줌. => 코드 수정 시 자동 반영됨.
// - 아이콘/매니페스트/OCR 엔진 등 무거운 정적 파일: "캐시 우선" -> 한 번 받으면 계속 재사용해서
//   매번 다시 다운로드하지 않음(속도/오프라인 안정성 유지).
const CACHE_NAME = 'panel-tag-scanner-v29';
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

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const isNavigation = event.request.mode === 'navigate' ||
    event.request.url.endsWith('/index.html') ||
    event.request.url.endsWith('/');

  if (isNavigation) {
    // 네트워크 우선: 최신 버전을 먼저 시도하고, 실패하면(오프라인) 캐시로 대체
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => caches.match(event.request).then((cached) => cached || caches.match('./index.html')))
    );
    return;
  }

  // 그 외(아이콘, manifest, OCR 엔진 등): 캐시 우선, 없으면 네트워크
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
