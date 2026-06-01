// Service worker do Exercícios (PWA). Bump CACHE para forçar atualização.
const CACHE = 'exercicios-v2';
const PRECACHE = [
  '/offline',
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Requisições de outros domínios (CDNs: Leaflet, Chart.js, mapas, fontes) vão
  // direto pra rede — o SW não pode devolver resposta "opaca" pra CSS/JS de CDN.
  if (url.origin !== self.location.origin) return;

  // Navegação entre páginas: tenta rede; se cair, mostra a tela offline.
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/offline')));
    return;
  }

  // Estáticos do próprio site (CSS/ícones): stale-while-revalidate.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req)
          .then((resp) => {
            if (resp && resp.status === 200) {
              const clone = resp.clone();
              caches.open(CACHE).then((c) => c.put(req, clone));
            }
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
