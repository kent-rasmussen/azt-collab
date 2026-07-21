"""Hold the Android presplash until the app is actually responsive.

Kivy removes the native presplash on the FIRST FRAME
(``kivy/base.py``: ``Clock.schedule_once(EventLoop.
remove_android_splash)`` immediately after ``EventLoop.start()``),
which in suite apps is long before the UI can respond — heavy
startup (project registry, LIFT parse, catalogs) continues after
first paint, so the user stares at a dead-looking screen and reads
it as "broken" (field 2026-07-21). ``hold()`` makes Kivy's
first-frame call a no-op; ``release()`` performs the real removal
once the host says the screen will respond.

Safety: a watchdog releases after ``HOLD_MAX_S`` regardless, so an
exception on the load path can never leave the splash up forever —
a stuck splash would be worse than the gap this fixes. The actual
``remove_presplash`` call is always marshalled to the Kivy main
thread (it crosses into the Android runtime; suite rule: never
touch JNI-adjacent surfaces from Python worker threads).

Host usage (server-APK picker does this; peers wire the same two
calls — see CLIENT_INTEGRATION.md):

    from azt_collab_client.ui import presplash_hold
    presplash_hold.hold()        # before App().run()
    ...
    presplash_hold.release()     # when the first screen is usable

Both calls are idempotent and no-ops off Android.
"""

import sys
import threading

HOLD_MAX_S = 45.0

_lock = threading.Lock()
_real_remove = None
_held = False
_released = False
_timer = None


def hold():
    """Intercept Kivy's first-frame presplash removal. Call before
    ``App().run()``. No-op off Android or if already held/released."""
    global _real_remove, _held, _timer
    with _lock:
        if _held or _released:
            return
        try:
            import android
        except ImportError:
            return
        real = getattr(android, 'remove_presplash', None)
        if real is None:
            return
        _real_remove = real
        android.remove_presplash = lambda *a, **kw: None
        _held = True
        _timer = threading.Timer(HOLD_MAX_S, _watchdog)
        _timer.daemon = True
        _timer.start()
        print(f'[presplash-hold] holding presplash until release() '
              f'(watchdog {HOLD_MAX_S:.0f}s)',
              file=sys.stderr, flush=True)


def release():
    """Remove the presplash now (on the Kivy main thread). Call when
    the first screen can actually respond. Idempotent."""
    global _released, _timer
    with _lock:
        if _released:
            return
        _released = True
        timer, _timer = _timer, None
        held = _held
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass
    if not held:
        return
    _on_main_thread(_invoke_real)


def _watchdog():
    print(f'[presplash-hold] watchdog: release() not called within '
          f'{HOLD_MAX_S:.0f}s — releasing anyway (load path may '
          f'have failed)', file=sys.stderr, flush=True)
    release()


def _on_main_thread(fn):
    try:
        from kivy.clock import Clock
        Clock.schedule_once(lambda dt: fn(), 0)
    except Exception:
        # No Kivy clock (shouldn't happen in a held app) — direct
        # call beats never releasing.
        fn()


def _invoke_real():
    real = _real_remove
    if real is None:
        return
    try:
        import android
        android.remove_presplash = real
    except Exception:
        pass
    try:
        real()
        print('[presplash-hold] presplash released',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[presplash-hold] release failed: {ex!r}',
              file=sys.stderr, flush=True)
