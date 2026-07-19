// Add this at the top of sw.js
var NOTIFICATION_CACHE = "mediapedia-notifications-v1";
var MAX_NOTIFICATIONS = 50;

// Enhanced push event with better handling
self.addEventListener("push", function (e) {
  var data = {};
  try {
    data = e.data.json();
  } catch (err) {
    data = {
      title: "MediaPedia",
      body: e.data ? e.data.text() : "New notification",
    };
  }

  var title = data.title || "MediaPedia Party";
  var options = {
    body: data.body || "",
    icon: "/static/favicon.png",
    badge: "/static/favicon.png",
    tag: data.tag || "mediapedia-notify-" + Date.now(),
    renotify: true,
    requireInteraction: true, // Keep notification visible until user interacts
    silent: false,
    data: {
      url: data.url || "/party",
      party_id: data.party_id || null,
      timestamp: Date.now(),
      title: title,
      body: data.body || "",
    },
    actions: data.actions || [{ action: "open", title: "Open App" }],
  };

  e.waitUntil(
    Promise.all([
      self.registration.showNotification(title, options),
      // Store notification for later retrieval
      storeNotification({
        title: title,
        body: data.body || "",
        url: data.url || "/party",
        timestamp: Date.now(),
      }),
      // Update badge count
      updateBadgeCount(1),
    ]),
  );
});

// Store notification in cache for retrieval when app opens
function storeNotification(data) {
  return caches.open(NOTIFICATION_CACHE).then(function (cache) {
    var key = "notification-" + data.timestamp;
    // Clean old notifications
    return cache.keys().then(function (keys) {
      if (keys.length >= MAX_NOTIFICATIONS) {
        // Remove oldest notification
        var oldest = keys.sort()[0];
        cache.delete(oldest);
      }
      return cache.put(key, new Response(JSON.stringify(data)));
    });
  });
}

// Update badge count
function updateBadgeCount(increment) {
  if (!("getNotifications" in self.registration)) return Promise.resolve();
  return self.registration.getNotifications().then(function (notifications) {
    var count = notifications.length;
    if ("setAppBadge" in self) {
      return self.setAppBadge(count);
    }
    return Promise.resolve();
  });
}

// Message handler for getting cached notifications
self.addEventListener("message", function (event) {
  if (event.data && event.data.type === "GET_CACHED_NOTIFICATIONS") {
    event.waitUntil(
      caches.open(NOTIFICATION_CACHE).then(function (cache) {
        return cache.keys().then(function (keys) {
          var notifications = [];
          var promises = keys.map(function (key) {
            return cache.match(key).then(function (response) {
              if (response) {
                return response.json().then(function (data) {
                  notifications.push(data);
                  // Remove after reading
                  cache.delete(key);
                });
              }
            });
          });
          return Promise.all(promises).then(function () {
            if (event.ports && event.ports.length) {
              event.ports[0].postMessage(notifications);
            }
          });
        });
      }),
    );
  }
});

var CACHE = "mediapedia-v2";
var OFFLINE_URLS = [
  "/",
  "/party",
  "/static/favicon.png",
  "/static/pwa.js",
  "/static/offline.html",
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) {
      return c.addAll(OFFLINE_URLS);
    }),
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys
          .filter(function (k) {
            return k !== CACHE;
          })
          .map(function (k) {
            return caches.delete(k);
          }),
      );
    }),
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (e) {
  if (
    e.request.method !== "GET" ||
    !e.request.url.startsWith(self.location.origin)
  )
    return;

  var url = e.request.url;
  var isStatic = url.includes("/static/");

  if (isStatic) {
    // Cache-first for static assets
    e.respondWith(
      caches.match(e.request).then(function (cached) {
        var fetchPromise = fetch(e.request).then(function (response) {
          if (response && response.status === 200) {
            var clone = response.clone();
            caches.open(CACHE).then(function (c) {
              c.put(e.request, clone);
            });
          }
          return response;
        });
        return cached || fetchPromise;
      }),
    );
  } else {
    // Network-first for pages, fall back to cache then offline
    e.respondWith(
      fetch(e.request).catch(function () {
        return caches.match(e.request).then(function (cached) {
          return cached || caches.match("/static/offline.html");
        });
      }),
    );
  }
});

// ===== NOTIFICATION CLICK =====
self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var url =
    e.notification.data && e.notification.data.url
      ? e.notification.data.url
      : "/party";

  if (
    e.action === "join" &&
    e.notification.data &&
    e.notification.data.party_id
  ) {
    url = "/party/" + e.notification.data.party_id;
    // Dismiss the invite in DB when user taps Join
    fetch("/api/party/dismiss_invite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ party_id: e.notification.data.party_id }),
    }).catch(function () {});
  } else if (e.action === "dismiss") {
    // Dismiss invite silently when user taps Dismiss
    if (e.notification.data && e.notification.data.party_id) {
      fetch("/api/party/dismiss_invite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ party_id: e.notification.data.party_id }),
      }).catch(function () {});
    }
    return;
  }

  e.waitUntil(
    clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (wins) {
        for (var i = 0; i < wins.length; i++) {
          if (wins[i].url.includes("/party")) {
            wins[i].focus();
            wins[i].navigate(url);
            return;
          }
        }
        return clients.openWindow(url);
      }),
  );
});

// ===== NOTIFICATION CLOSE (swipe away) =====
self.addEventListener("notificationclose", function (e) {
  // When user swipes away a party invite notification, auto-dismiss it in DB
  var data = e.notification.data || {};
  if (
    data.party_id &&
    e.notification.tag &&
    e.notification.tag.startsWith("party-invite-")
  ) {
    fetch("/api/party/dismiss_invite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ party_id: data.party_id }),
    }).catch(function () {});
  }
  // Clear app badge when all notifications are dismissed
  self.registration.getNotifications().then(function (notifications) {
    if (notifications.length === 0 && "clearAppBadge" in self) {
      self.clearAppBadge();
    }
  });
});

// ===== BACKGROUND SYNC (queued actions when offline) =====
self.addEventListener("sync", function (e) {
  if (e.tag === "sync-pending-actions") {
    e.waitUntil(flushPendingActions());
  }
});

function flushPendingActions() {
  return self.clients.matchAll().then(function (clients) {
    clients.forEach(function (client) {
      client.postMessage({ type: "FLUSH_PENDING" });
    });
  });
}

// ===== PERIODIC BACKGROUND SYNC (check pending invites) =====
self.addEventListener("periodicsync", function (e) {
  if (e.tag === "check-invites") {
    e.waitUntil(checkAndNotifyInvites());
  }
});

function checkAndNotifyInvites() {
  return fetch("/api/party/pending_invites")
    .then(function (r) {
      return r.json();
    })
    .then(function (invites) {
      if (!Array.isArray(invites) || !invites.length) return;
      return Promise.all(
        invites.map(function (inv) {
          return self.registration
            .getNotifications({ tag: "party-invite-" + inv.party_id })
            .then(function (existing) {
              if (existing.length) return; // already shown
              return self.registration.showNotification("🎉 Party Invite!", {
                body:
                  inv.invited_by +
                  ' invited you to join "' +
                  inv.party_name +
                  '"',
                icon: "/static/favicon.png",
                badge: "/static/favicon.png",
                tag: "party-invite-" + inv.party_id,
                data: { url: "/party/" + inv.party_id, party_id: inv.party_id },
                actions: [
                  { action: "join", title: "Join Party" },
                  { action: "dismiss", title: "Dismiss" },
                ],
              });
            });
        }),
      );
    })
    .catch(function () {});
}
