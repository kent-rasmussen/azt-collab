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
                'AZT LAN sync',
                NotificationManager.IMPORTANCE_LOW)
            channel.setDescription(
                'AZT Collaboration sharing with nearby devices.')
            nm.createNotificationChannel(channel)
        builder = NotificationCompat(ctx, _NOTIFICATION_CHANNEL_ID)
        builder.setContentTitle(
            'AZT Collaboration: sharing with nearby devices')
        builder.setContentText('Tap to manage paired phones.')
        builder.setSmallIcon(ctx.getApplicationInfo().icon)
        builder.setOngoing(True)
        return builder.build()
    except Exception as ex:
        print(f'[lan-fgs] notification build failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None


def start_fgs():
    """Promote the ``:provider`` service to a foreground service of
    type ``specialUse``. No-op on desktop. Idempotent."""
    with _LOCK:
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
            ServiceInfo = autoclass(
                'android.content.pm.ServiceInfo')
            Build = autoclass('android.os.Build$VERSION')
            # Call Service.startForeground directly. The Service
            # base class has had startForeground(int, Notification)
            # since API 5 and the 3-arg overload with
            # foregroundServiceType since API 29. Skip the
            # ServiceCompat wrapper — jnius static-method resolution
            # against the androidx helper class doesn't see the
            # ``startForeground`` overloads on this build.
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


def stop_fgs():
    """Demote out of foreground state. No-op on desktop. Idempotent."""
    with _LOCK:
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
            # Same reason as start_fgs: call Service.stopForeground
            # directly. The boolean removeNotification arg has been
            # there since API 5; the Service.STOP_FOREGROUND_REMOVE
            # int variant only exists in N+ via ServiceCompat which
            # jnius can't see here.
            svc.stopForeground(True)
        except Exception as ex:
            print(f'[lan-fgs] stopForeground failed: {ex!r}',
                  file=sys.stderr, flush=True)
        _STATE['foreground'] = False


def acquire_wifi_locks():
    """Acquire ``WIFI_MODE_FULL_HIGH_PERF`` + ``MulticastLock`` so
    Wi-Fi stays awake and multicast packets reach us. The high-perf
    mode is the real battery cost the user pays for while the LAN
    toggle is on. No-op on desktop. Idempotent."""
    with _LOCK:
        if _STATE['wifi_lock'] is not None and \
                _STATE['multicast_lock'] is not None:
            return
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
            if _STATE['wifi_lock'] is None:
                lock = wifi.createWifiLock(
                    WifiManager.WIFI_MODE_FULL_HIGH_PERF,
                    'azt_collab_lan_sync')
                lock.setReferenceCounted(False)
                lock.acquire()
                _STATE['wifi_lock'] = lock
            if _STATE['multicast_lock'] is None:
                mlock = wifi.createMulticastLock(
                    'azt_collab_lan_sync')
                mlock.setReferenceCounted(False)
                mlock.acquire()
                _STATE['multicast_lock'] = mlock
            print('[lan-fgs] acquired WifiLock + MulticastLock',
                  file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[lan-fgs] wifi lock acquire failed: {ex!r}',
                  file=sys.stderr, flush=True)


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
