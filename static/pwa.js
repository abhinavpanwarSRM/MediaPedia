// MediaPedia PWA - shared across all pages
(function() {
  var VAPID_PUBLIC_KEY = 'BNCfhD-i3gRMYPV7Q4Ky3pslclijDLbtNn8NPUUIIQt92xHGr-nMFeWmMbQplU_J3SgBo_lKmQKbDVPG5s7pPU4';
  var _swReg = null;
  var _deferredInstallPrompt = null;

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
      if (result.outcome === 'accepted') showPwaToast('MediaPedia installed! 🎉');
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
        if ('Notification' in window && Notification.permission === 'granted') {
          autoResubscribe(reg);
        }
        registerPeriodicSync(reg);
      });
    }).catch(function(err) { console.warn('SW reg failed:', err); });
  }

  function autoResubscribe(reg) {
    if (!('PushManager' in window)) return;
    fetch('/api/push/vapid_public_key')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d.key) return;
        reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(d.key)
        }).then(function(sub) {
          fetch('/api/push/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(sub.toJSON())
          });
        }).catch(function() {});
      }).catch(function() {});
  }

  function registerPeriodicSync(reg) {
    if (!('periodicSync' in reg)) return;
    navigator.permissions.query({ name: 'periodic-background-sync' })
      .then(function(s) {
        if (s.state !== 'granted') return;
        reg.periodicSync.register('check-invites', { minInterval: 30 * 60 * 1000 }).catch(function() {});
      }).catch(function() {});
  }

  function urlBase64ToUint8Array(b) {
    var pad = '='.repeat((4 - b.length % 4) % 4);
    var base64 = (b + pad).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  // ── Badge ──
  function updateBadge(count) {
    if (!('setAppBadge' in navigator)) return;
    count > 0 ? navigator.setAppBadge(count).catch(function(){}) : navigator.clearAppBadge().catch(function(){});
  }

  // Check pending invites on every page load and update badge
  fetch('/api/party/pending_invites')
    .then(function(r) { return r.json(); })
    .then(function(invites) {
      if (!Array.isArray(invites)) return;
      updateBadge(invites.length);
    }).catch(function() {});

  // Clear badge when tab becomes visible
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible') updateBadge(0);
  });

  // ── Toast helper ──
  function showPwaToast(msg) {
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);' +
      'background:#1db954;color:#000;padding:0.6rem 1.5rem;border-radius:40px;' +
      'font-weight:700;font-size:0.85rem;z-index:99999;pointer-events:none;font-family:sans-serif;';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function() { t.remove(); }, 3000);
  }

  // Expose globally for party page
  window._pwa = { swReg: function() { return _swReg; }, urlBase64ToUint8Array: urlBase64ToUint8Array };
})();
