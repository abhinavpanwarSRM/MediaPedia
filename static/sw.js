var CACHE = 'mediapedia-v1';
var OFFLINE_URLS = ['/party', '/static/favicon.png'];

self.addEventListener('install', function(e) {
  e.waitUntil(
    caches.open(CACHE).then(function(c) { return c.addAll(OFFLINE_URLS); })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(keys.filter(function(k) { return k !== CACHE; }).map(function(k) { return caches.delete(k); }));
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function(e) {
  // Only cache GET requests for our own origin
  if (e.request.method !== 'GET' || !e.request.url.startsWith(self.location.origin)) return;
  e.respondWith(
    fetch(e.request).catch(function() {
      return caches.match(e.request);
    })
  );
});

// ===== PUSH NOTIFICATIONS =====
self.addEventListener('push', function(e) {
  var data = {};
  try { data = e.data.json(); } catch(err) { data = { title: 'MediaPedia', body: e.data ? e.data.text() : '' }; }

  var title = data.title || 'MediaPedia Party';
  var options = {
    body: data.body || '',
    icon: '/static/favicon.png',
    badge: '/static/favicon.png',
    tag: data.tag || 'mediapedia-notify',
    renotify: true,
    data: { url: data.url || '/party' },
    actions: data.actions || []
  };

  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) ? e.notification.data.url : '/party';

  // Handle action buttons
  if (e.action === 'join' && e.notification.data && e.notification.data.party_id) {
    url = '/party/' + e.notification.data.party_id;
  } else if (e.action === 'dismiss') {
    return;
  }

  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(wins) {
      for (var i = 0; i < wins.length; i++) {
        if (wins[i].url.includes('/party')) {
          wins[i].focus();
          wins[i].navigate(url);
          return;
        }
      }
      return clients.openWindow(url);
    })
  );
});
