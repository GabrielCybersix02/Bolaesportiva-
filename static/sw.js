// Service worker minimo: so existe para o navegador oferecer "Instalar app". Nao guarda nada em cache (o bilhete e sempre ao vivo).
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
