self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.registration.unregister().then(() => caches.keys()).then(keys => Promise.all(keys.map(k => caches.delete(k))))));
