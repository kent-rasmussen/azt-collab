"""
Android foreground-service promotion for the LAN listener (parked
design phase 4).

When the daemon-wide ``lan.allow_sync`` toggle goes on, the listener
thread starts (see ``azt_collabd.lan_listener``); on Android we also
promote the ``:provider`` service to a foreground service of type
``specialUse``. That keeps the host process from being killed under
memory pressure while peers are actively syncing, and is the only
Android-policy-compliant way to bind a server socket that needs to
accept inbound connections while the user has the app in the
background.

Three reasons the FGS promotion is a separate module from the rest
of the listener:

  1. **Desktop is a no-op.** The same ``start_fgs() / stop_fgs()``
     entry points are safe to call everywhere; the listener's
     state machine doesn't have to branch on platform.
  2. **pyjnius lazy-init is foot-gun-prone.** Any jnius call from
     a worker thread that hasn't been pre-warmed on the UI thread
     SEGVs at NULL classloader. The listener thread is a worker;
     keeping the jnius calls in their own module makes it easier
     to audit that every call is on the right thread.
  3. **Java-side wiring may evolve.** The manifest's
     ``foregroundServiceType="specialUse"`` declaration lives in
     ``p4a_hook.py``'s ``_AZTCOLLAB_SERVICE_BLOCK``; the
     ``<property>`` subtype inner element + matching uses-permission
     entries (``FOREGROUND_SERVICE_SPECIAL_USE``) need to land
     together. As of the parked-spec phase-4 commit, the manifest
     side of the change is still TODO — see CHANGELOG entry for
     this version. This module's calls are guarded so an APK
     missing the manifest pieces just fails the
     ``startForeground`` call and the listener falls back to the
     existing sticky-bound + START_STICKY kill-resistance.

The notification copy is minimal — the parked spec calls for
"AZT Collaboration: sharing with nearby devices" but actually
constructing the Notification (Builder, channel, PendingIntent)
needs more Java/Android-API plumbing than is worth doing inline.
We use a tiny stub the user can refine via the daemon settings UI
once the FGS path is exercised on real hardware.
"""

from __future__ import annotations

import sys
import threading


_LOCK = threading.Lock()
_STATE = {
    'foreground': False,
    'wifi_lock': None,
    'multicast_lock': None,
}
# Reference-counted arm/disarm (0.50+). Each in-flight operation
# increments its counter; when both counters drop to zero AND
# ``lan.passive_discovery`` is off, ``arm_release_for_operation``
# tears the FGS + locks down.
#
# ``discovery``: bursts of mDNS query/listen. Needs MulticastLock +
# FGS (so the process stays alive long enough for replies to land).
# ``transfer``: outbound/inbound push or clone. Needs WifiLock +
# FGS (radio in high-perf so the pack doesn't stall).
_REF = {
    'discovery': 0,
    'transfer': 0,
}


_NOTIFICATION_ID = 0xAC0B1A    # arbitrary, uniquely ours
_NOTIFICATION_CHANNEL_ID = 'azt_collab_lan_sync'
_FGS_SUBTYPE = 'lan-peer-git-sync'


def _on_android():
    try:
        import jnius  # noqa: F401
        return True
    except ImportError:
        return False


def _get_service():
    """Return the running ``PythonService.mService`` instance, or
    None if jnius isn't available or the service isn't up. Probe
    order mirrors ``paths._android_files_dir`` — the static field
    is sometimes None on early binder threads."""
    try:
        from jnius import autoclass
    except ImportError:
        return None
    try:
        PythonService = autoclass('org.kivy.android.PythonService')
        svc = PythonService.mService
        if svc is not None:
            return svc
    except Exception:
        pass
    try:
        ActivityThread = autoclass('android.app.ActivityThread')
        app = ActivityThread.currentApplication()
        if app is not None:
            # Application isn't a Service, but we can still use its
            # context for the notification + WifiManager. Caller
            # branches: real startForeground needs a Service.
            return app
    except Exception:
        pass
    return None


def _build_minimal_notification(ctx):
    """Build a minimal foreground-service notification. The
    notification UX in the parked spec deserves real polish; this
    is the minimum that keeps Android from killing the process
    while the LAN listener is up.

    Returns ``None`` on any failure — caller falls back to leaving
    the service in sticky-bound state."""
    try:
        from jnius import autoclass
    except ImportError:
        return None
    try:
        NotificationCompat = autoclass(
            'androidx.core.app.NotificationCompat$Builder')
        NotificationManager = autoclass(
            'android.app.NotificationManager')
        NotificationChannel = autoclass('android.app.NotificationChannel')
        Build = autoclass('android.os.Build$VERSION')
    except Exception as ex:
        print(f'[lan-fgs] missing NotificationCompat classes: {ex!r}',
              file=sys.stderr, flush=True)
        return None
    try:
        nm = ctx.getSystemService('notification')
        if Build.SDK_INT >= 26 and nm is not None:
            channel = NotificationChannel(
                _NOTIFICATION_CHANNEL_ID,
                'AZT sync',
                NotificationManager.IMPORTANCE_LOW)
            channel.setDescription(
                'AZT Collaboration keeping your work backed up '
                'and shared.')
            nm.createNotificationChannel(channel)
        builder = NotificationCompat(ctx, _NOTIFICATION_CHANNEL_ID)
        # Neutral copy: this FGS now covers both LAN peer sharing and
        # WAN github backup (0.52.21 run-to-completion), so it must
        # not claim to be only "nearby devices".
        builder.setContentTitle('AZT Collaboration: syncing')
        builder.setContentText('Keeping your work backed up and shared.')
        builder.setSmallIcon(ctx.getApplicationInfo().icon)
        builder.setOngoing(True)
        return builder.build()
    except Exception as ex:
        print(f'[lan-fgs] notification build failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None


def start_fgs():
    """Promote the ``:provider`` service to a foreground service of
    type ``specialUse``. No-op on desktop. Idempotent.

    Pre-0.50: called directly when ``lan.allow_sync`` toggled on.
    Post-0.50: still works for back-compat, but the preferred entry
    points are the ref-counted ``arm_for_*`` helpers."""
    with _LOCK:
        _start_fgs_unlocked()


def stop_fgs():
    """Demote out of foreground state. No-op on desktop. Idempotent."""
    with _LOCK:
        _stop_fgs_unlocked()


def acquire_wifi_locks():
    """Acquire ``WIFI_MODE_FULL_HIGH_PERF`` + ``MulticastLock`` so
    Wi-Fi stays awake and multicast packets reach us. The high-perf
    mode is the real battery cost the user pays for while the LAN
    toggle is on. No-op on desktop. Idempotent.

    Pre-0.50 caller path: ``lan_listener.apply_toggle`` calls this
    when the toggle goes on. Post-0.50 the preferred path is the
    ref-counted ``arm_for_discovery`` / ``arm_for_transfer`` helpers
    which only acquire what they need."""
    with _LOCK:
        _acquire_wifi_locks_unlocked(want_wifi=True, want_mcast=True)


def release_wifi_locks():
    """Release both Wi-Fi locks. No-op on desktop. Idempotent."""
    with _LOCK:
        for key in ('wifi_lock', 'multicast_lock'):
            lock = _STATE[key]
            if lock is None:
                continue
            try:
                if lock.isHeld():
                    lock.release()
            except Exception as ex:
                print(f'[lan-fgs] release {key} raised: {ex!r}',
                      file=sys.stderr, flush=True)
            _STATE[key] = None


# ── reference-counted lifecycle (0.50+) ────────────────────────────
#
# The old "hold everything while toggle is on" model is replaced by
# ref counts: each operation arms what it needs, releases on
# completion. When both counters drop to zero and the user hasn't
# opted into passive discovery, everything goes down.
#
# All four helpers are idempotent and safe to call from any thread;
# all gating is done inside ``_apply_state_locked`` to keep the
# acquire/release decisions in one place.


def _apply_state_locked():
    """Recompute desired FGS + lock state from the ref counts + the
    persisted ``lan.passive_discovery`` flag. Must be called with
    ``_LOCK`` held. Acquires and releases the underlying jnius
    handles to match desired state. Idempotent."""
    try:
        from .. import settings as _settings
        passive = _settings.lan_autodiscovery()
    except Exception:
        passive = False
    discovery_active = (_REF['discovery'] > 0)
    transfer_active = (_REF['transfer'] > 0)
    want_fgs = passive or discovery_active or transfer_active
    want_mcast = passive or discovery_active
    want_wifi = passive or transfer_active

    if want_fgs and not _STATE['foreground']:
        # _LOCK is recursive within the same thread (threading.Lock is
        # not, but we're called from already-locked context — start_fgs
        # takes _LOCK again; refactor to avoid double-acquire).
        # ``_start_fgs_unlocked`` does the platform-side work without
        # taking _LOCK.
        _start_fgs_unlocked()
    elif not want_fgs and _STATE['foreground']:
        _stop_fgs_unlocked()

    if want_mcast or want_wifi:
        _acquire_wifi_locks_unlocked(
            want_wifi=want_wifi, want_mcast=want_mcast)
    if not want_wifi and _STATE['wifi_lock'] is not None:
        _release_one_lock_unlocked('wifi_lock')
    if not want_mcast and _STATE['multicast_lock'] is not None:
        _release_one_lock_unlocked('multicast_lock')


def arm_for_discovery():
    """Increment the discovery ref count + apply state. Use during
    an mDNS burst (announce + browse + wait for replies)."""
    with _LOCK:
        _REF['discovery'] += 1
        _apply_state_locked()


def disarm_for_discovery():
    """Decrement the discovery ref count + reapply state."""
    with _LOCK:
        if _REF['discovery'] > 0:
            _REF['discovery'] -= 1
        _apply_state_locked()


def arm_for_transfer():
    """Increment the transfer ref count + apply state. Use around
    an outbound push or while accepting an inbound pack."""
    with _LOCK:
        _REF['transfer'] += 1
        _apply_state_locked()


def disarm_for_transfer():
    """Decrement the transfer ref count + reapply state."""
    with _LOCK:
        if _REF['transfer'] > 0:
            _REF['transfer'] -= 1
        _apply_state_locked()


def apply_passive_state():
    """Reconcile to the persisted ``lan.passive_discovery`` flag.
    Called on flag change so a flip-to-on raises FGS + locks even
    when no ref count is active. Idempotent."""
    with _LOCK:
        _apply_state_locked()


def snapshot():
    """Diagnostic: current state without taking actions."""
    with _LOCK:
        return {
            'foreground': bool(_STATE['foreground']),
            'wifi_lock_held': _STATE['wifi_lock'] is not None,
            'multicast_lock_held': _STATE['multicast_lock'] is not None,
            'ref_discovery': _REF['discovery'],
            'ref_transfer': _REF['transfer'],
        }


# ── internal: locked variants ─────────────────────────────────────
#
# The public start_fgs / stop_fgs / acquire_wifi_locks /
# release_wifi_locks helpers all take _LOCK themselves; the locked
# variants below do the platform-side work assuming the caller
# already holds _LOCK. Used by _apply_state_locked.


def _start_fgs_unlocked():
    if _STATE['foreground']:
        return
    if not _on_android():
        return
    svc = _get_service()
    if svc is None:
        print('[lan-fgs] no PythonService.mService; skipping',
              file=sys.stderr, flush=True)
        return
    notification = _build_minimal_notification(svc)
    if notification is None:
        print('[lan-fgs] no notification; cannot promote',
              file=sys.stderr, flush=True)
        return
    try:
        from jnius import autoclass
        ServiceInfo = autoclass('android.content.pm.ServiceInfo')
        Build = autoclass('android.os.Build$VERSION')
        if Build.SDK_INT >= 29:
            svc.startForeground(
                _NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else:
            svc.startForeground(_NOTIFICATION_ID, notification)
    except Exception as ex:
        print(f'[lan-fgs] startForeground failed: {ex!r}',
              file=sys.stderr, flush=True)
        return
    _STATE['foreground'] = True
    print('[lan-fgs] promoted to foreground (specialUse / '
          f'{_FGS_SUBTYPE})', file=sys.stderr, flush=True)


def _stop_fgs_unlocked():
    if not _STATE['foreground']:
        return
    if not _on_android():
        _STATE['foreground'] = False
        return
    svc = _get_service()
    if svc is None:
        _STATE['foreground'] = False
        return
    try:
        svc.stopForeground(True)
    except Exception as ex:
        print(f'[lan-fgs] stopForeground failed: {ex!r}',
              file=sys.stderr, flush=True)
    _STATE['foreground'] = False


def _acquire_wifi_locks_unlocked(want_wifi=True, want_mcast=True):
    if not _on_android():
        return
    try:
        from jnius import autoclass
    except ImportError:
        return
    try:
        ActivityThread = autoclass('android.app.ActivityThread')
        app = ActivityThread.currentApplication()
        if app is None:
            return
        WifiManager = autoclass('android.net.wifi.WifiManager')
        wifi = app.getSystemService('wifi')
        if wifi is None:
            return
        if want_wifi and _STATE['wifi_lock'] is None:
            lock = wifi.createWifiLock(
                WifiManager.WIFI_MODE_FULL_HIGH_PERF,
                'azt_collab_lan_sync')
            lock.setReferenceCounted(False)
            lock.acquire()
            _STATE['wifi_lock'] = lock
        if want_mcast and _STATE['multicast_lock'] is None:
            mlock = wifi.createMulticastLock('azt_collab_lan_sync')
            mlock.setReferenceCounted(False)
            mlock.acquire()
            _STATE['multicast_lock'] = mlock
    except Exception as ex:
        print(f'[lan-fgs] wifi lock acquire failed: {ex!r}',
              file=sys.stderr, flush=True)


def _release_one_lock_unlocked(key):
    lock = _STATE.get(key)
    if lock is None:
        return
    try:
        if lock.isHeld():
            lock.release()
    except Exception as ex:
        print(f'[lan-fgs] release {key} raised: {ex!r}',
              file=sys.stderr, flush=True)
    _STATE[key] = None
