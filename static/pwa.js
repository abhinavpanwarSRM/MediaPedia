// MediaPedia PWA - Enhanced Notification Support
(function() {
  var VAPID_PUBLIC_KEY = 'BNCfhD-i3gRMYPV7Q4Ky3pslclijDLbtNn8NPUUIIQt92xHGr-nMFeWmMbQplU_J3SgBo_lKmQKbDVPG5s7pPU4';
  var _swReg = null;
  var _deferredInstallPrompt = null;
  var _subscription = null;

  // ── Install prompt ──
  window.addEventListener('beforeinstallprompt', function(e) {
    e.preventDefault();
    _deferredInstallPrompt = e;
    showInstallBanner();
  });

  function showInstallBanner() {
    if (document.getElementById('pwa-install-banner')) return;
    var banner = document.createElement('div');
    banner.id = 'pwa-install-banner';
    banner.style.cssText =
      'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);' +
      'background:#1a1a1a;border:1px solid #1db954;border-radius:16px;' +
      'padding:0.8rem 1.2rem;z-index:99999;display:flex;align-items:center;' +
      'gap:0.8rem;box-shadow:0 4px 20px rgba(0,0,0,0.6);max-width:90vw;' +
      'animation:pwaSlideUp 0.3s ease;font-family:sans-serif;';
    banner.innerHTML =
      '<style>@keyframes pwaSlideUp{from{opacity:0;transform:translateX(-50%) translateY(20px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}</style>' +
      '<span style="font-size:1.2rem">📲</span>' +
      '<span style="font-size:0.88rem;color:#ccc">Install MediaPedia for the best experience</span>' +
      '<button id="pwa-install-btn" style="background:#1db954;border:none;border-radius:8px;' +
      'padding:0.35rem 0.9rem;font-weight:700;cursor:pointer;color:#000;font-size:0.82rem;white-space:nowrap;">Install</button>' +
      '<button id="pwa-dismiss-btn" style="background:none;border:none;color:#888;font-size:1.2rem;cursor:pointer;padding:0 0.2rem;">&times;</button>';
    document.body.appendChild(banner);
    document.getElementById('pwa-install-btn').onclick = installPWA;
    document.getElementById('pwa-dismiss-btn').onclick = function() { banner.remove(); };
  }

  function installPWA() {
    var banner = document.getElementById('pwa-install-banner');
    if (banner) banner.remove();
    if (!_deferredInstallPrompt) return;
    _deferredInstallPrompt.prompt();
    _deferredInstallPrompt.userChoice.then(function(result) {
      if (result.outcome === 'accepted') {
        showPwaToast('MediaPedia installed! 🎉');
        // After installation, try to register for push again with better permissions
        setTimeout(initPushNotifications, 2000);
      }
      _deferredInstallPrompt = null;
    });
  }

  // ── Service Worker ──
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations().then(function(regs) {
      var stale = regs.filter(function(r) { return r.scope !== window.location.origin + '/'; });
      return Promise.all(stale.map(function(r) { return r.unregister(); }));
    }).then(function() {
      return navigator.serviceWorker.register('/sw.js', { scope: '/' });
    }).then(function(reg) {
      _swReg = reg;
      reg.ready.then(function() {
        // Check if we already have permission
        if ('Notification' in window) {
          if (Notification.permission === 'granted') {
            initPushNotifications(reg);
          } else if (Notification.permission === 'default') {
            // Auto-request permission after a delay
            setTimeout(requestNotificationPermission, 3000);
          }
        }
        registerPeriodicSync(reg);
        // Listen for push subscription changes
        setupPushSubscriptionListener(reg);
      });
    }).catch(function(err) { console.warn('SW reg failed:', err); });
  }

  // ── Request Notification Permission with Better UX ──
  function requestNotificationPermission() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'granted') {
      if (_swReg) initPushNotifications(_swReg);
      return;
    }
    
    // Show a nice permission request dialog
    var dialog = document.createElement('div');
    dialog.style.cssText = 
      'position:fixed;bottom:20%;left:50%;transform:translateX(-50%);' +
      'background:#1a1a1a;border:1px solid #1db954;border-radius:16px;' +
      'padding:1.5rem;z-index:99999;max-width:400px;width:90%;' +
      'box-shadow:0 8px 30px rgba(0,0,0,0.8);text-align:center;font-family:sans-serif;';
    dialog.innerHTML = 
      '<div style="font-size:2.5rem;margin-bottom:0.5rem;">🔔</div>' +
      '<div style="color:#fff;font-size:1.1rem;font-weight:600;margin-bottom:0.5rem;">Get Party Invites & Notifications</div>' +
      '<div style="color:#aaa;font-size:0.9rem;margin-bottom:1.5rem;line-height:1.4;">' +
      'Never miss a party invite or message. Allow notifications to stay connected even when you\'re not here.' +
      '</div>' +
      '<div style="display:flex;gap:0.8rem;justify-content:center;">' +
      '<button id="notif-yes" style="background:#1db954;border:none;border-radius:8px;padding:0.6rem 1.5rem;font-weight:700;cursor:pointer;color:#000;">Allow</button>' +
      '<button id="notif-no" style="background:#333;border:none;border-radius:8px;padding:0.6rem 1.5rem;font-weight:700;cursor:pointer;color:#aaa;">Not Now</button>' +
      '</div>';
    document.body.appendChild(dialog);
    
    document.getElementById('notif-yes').onclick = function() {
      dialog.remove();
      Notification.requestPermission().then(function(perm) {
        if (perm === 'granted' && _swReg) {
          initPushNotifications(_swReg);
          showPwaToast('Notifications enabled! 🔔');
        }
      });
    };
    document.getElementById('notif-no').onclick = function() {
      dialog.remove();
    };
  }

  // ── Initialize Push Notifications ──
  function initPushNotifications(reg) {
    if (!reg || !('PushManager' in window)) return;
    
    // Check if already subscribed
    reg.pushManager.getSubscription().then(function(sub) {
      if (sub) {
        _subscription = sub;
        // Ensure subscription is up to date
        sendSubscriptionToServer(sub);
        return;
      }
      
      // Not subscribed, get VAPID key and subscribe
      fetch('/api/push/vapid_public_key')
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (!d.key) {
            console.warn('No VAPID public key from server');
            return;
          }
          return reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(d.key)
          });
        })
        .then(function(sub) {
          if (sub) {
            _subscription = sub;
            return sendSubscriptionToServer(sub);
          }
        })
        .catch(function(err) {
          console.warn('Push subscription failed:', err);
          // Retry after a delay if it was a network issue
          if (err.message && err.message.includes('network')) {
            setTimeout(function() { initPushNotifications(reg); }, 5000);
          }
        });
    }).catch(function(err) {
      console.warn('Push getSubscription failed:', err);
    });
  }

  function sendSubscriptionToServer(sub) {
    return fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON())
    }).then(function(res) {
      if (res.ok) {
        console.log('Push subscription sent to server');
        return res.json();
      }
      throw new Error('Server returned ' + res.status);
    }).catch(function(err) {
      console.warn('Failed to send subscription to server:', err);
    });
  }

  // ── Listen for subscription changes ──
  function setupPushSubscriptionListener(reg) {
    // Re-subscribe if the push service changes
    navigator.serviceWorker.addEventListener('pushsubscriptionchange', function(e) {
      e.waitUntil(
        reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY)
        }).then(function(newSub) {
          _subscription = newSub;
          return sendSubscriptionToServer(newSub);
        })
      );
    });
  }

  // ── Periodic Sync ──
  function registerPeriodicSync(reg) {
    if (!('periodicSync' in reg)) return;
    
    // Check if we have permission for periodic sync
    navigator.permissions.query({ name: 'periodic-background-sync' })
      .then(function(status) {
        if (status.state === 'granted') {
          // Unregister existing sync first
          reg.periodicSync.unregister('check-invites').catch(function() {});
          // Register with 15-minute interval
          return reg.periodicSync.register('check-invites', { 
            minInterval: 15 * 60 * 1000 
          });
        } else if (status.state === 'prompt') {
          // Request permission for periodic sync
          // This usually happens when the user has never allowed it
          requestPeriodicSyncPermission(reg);
        }
      })
      .then(function() {
        console.log('Periodic sync registered');
      })
      .catch(function(err) {
        console.warn('Periodic sync registration failed:', err);
      });
  }

  function requestPeriodicSyncPermission(reg) {
    // Show a dialog explaining periodic sync
    var dialog = document.createElement('div');
    dialog.style.cssText = 
      'position:fixed;bottom:20%;left:50%;transform:translateX(-50%);' +
      'background:#1a1a1a;border:1px solid #1db954;border-radius:16px;' +
      'padding:1.5rem;z-index:99999;max-width:400px;width:90%;' +
      'box-shadow:0 8px 30px rgba(0,0,0,0.8);text-align:center;font-family:sans-serif;';
    dialog.innerHTML = 
      '<div style="font-size:2.5rem;margin-bottom:0.5rem;">🔄</div>' +
      '<div style="color:#fff;font-size:1.1rem;font-weight:600;margin-bottom:0.5rem;">Stay Updated in Background</div>' +
      '<div style="color:#aaa;font-size:0.9rem;margin-bottom:1.5rem;line-height:1.4;">' +
      'Allow MediaPedia to check for new invites and messages in the background, even when you\'re not using the app.' +
      '</div>' +
      '<div style="display:flex;gap:0.8rem;justify-content:center;">' +
      '<button id="periodic-yes" style="background:#1db954;border:none;border-radius:8px;padding:0.6rem 1.5rem;font-weight:700;cursor:pointer;color:#000;">Allow</button>' +
      '<button id="periodic-no" style="background:#333;border:none;border-radius:8px;padding:0.6rem 1.5rem;font-weight:700;cursor:pointer;color:#aaa;">Not Now</button>' +
      '</div>';
    document.body.appendChild(dialog);
    
    document.getElementById('periodic-yes').onclick = function() {
      dialog.remove();
      // Try to register periodic sync again after permission is granted
      navigator.permissions.query({ name: 'periodic-background-sync' })
        .then(function(status) {
          if (status.state === 'granted') {
            registerPeriodicSync(reg);
          }
        });
    };
    document.getElementById('periodic-no').onclick = function() {
      dialog.remove();
    };
  }

  // ── Badge Management ──
  function updateBadge(count) {
    if (!('setAppBadge' in navigator)) return;
    if (count > 0) {
      navigator.setAppBadge(count).catch(function() {});
    } else {
      navigator.clearAppBadge().catch(function() {});
    }
  }

  // ── Check pending invites on page load ──
  function checkPendingInvites() {
    fetch('/api/party/pending_invites')
      .then(function(r) { return r.json(); })
      .then(function(invites) {
        if (!Array.isArray(invites)) return;
        updateBadge(invites.length);
        if (invites.length > 0) {
          // Show a subtle notification banner
          showInviteBanner(invites);
        }
        return invites;
      })
      .catch(function() {});
  }

  function showInviteBanner(invites) {
    var existing = document.getElementById('invite-banner');
    if (existing) return;
    
    var banner = document.createElement('div');
    banner.id = 'invite-banner';
    banner.style.cssText = 
      'position:fixed;top:0;left:0;right:0;background:#1db954;color:#000;' +
      'padding:12px 16px;z-index:99998;text-align:center;font-weight:600;' +
      'font-family:sans-serif;font-size:0.9rem;cursor:pointer;' +
      'box-shadow:0 2px 10px rgba(0,0,0,0.2);';
    banner.textContent = '🎉 You have ' + invites.length + ' pending party invite' + (invites.length > 1 ? 's' : '') + '! Click to view.';
    banner.onclick = function() {
      window.location.href = '/party';
    };
    document.body.prepend(banner);
    // Auto-remove after 10 seconds
    setTimeout(function() {
      if (banner.parentNode) banner.remove();
    }, 10000);
  }

  // ── Visibility change handler ──
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible') {
      // Check for new invites when tab becomes visible
      checkPendingInvites();
      // Re-subscribe if needed
      if (_swReg && 'PushManager' in window) {
        _swReg.pushManager.getSubscription().then(function(sub) {
          if (!sub) {
            // Subscription was lost, re-subscribe
            initPushNotifications(_swReg);
          }
        }).catch(function() {});
      }
    }
  });

  // ── Network status handling ──
  window.addEventListener('online', function() {
    // Re-sync when coming back online
    if (_swReg && 'sync' in _swReg) {
      _swReg.sync.register('sync-pending-actions').catch(function() {});
    }
    // Re-check invites
    checkPendingInvites();
  });

  // ── Initial setup ──
  // Check for invites on page load
  checkPendingInvites();

  // Check for push notification support
  if ('Notification' in window && Notification.permission === 'granted' && _swReg) {
    initPushNotifications(_swReg);
  }

  // ── URL helpers ──
  function urlBase64ToUint8Array(b) {
    var pad = '='.repeat((4 - b.length % 4) % 4);
    var base64 = (b + pad).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function showPwaToast(msg) {
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);' +
      'background:#1db954;color:#000;padding:0.6rem 1.5rem;border-radius:40px;' +
      'font-weight:700;font-size:0.85rem;z-index:99999;pointer-events:none;font-family:sans-serif;';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function() { t.remove(); }, 3000);
  }

  // ── Expose globally ──
  window._pwa = { 
    swReg: function() { return _swReg; }, 
    urlBase64ToUint8Array: urlBase64ToUint8Array,
    checkPendingInvites: checkPendingInvites,
    initPushNotifications: initPushNotifications
  };
})();