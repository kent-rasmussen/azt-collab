"""
Entry point for the standalone AZT Collaboration service APK
(``org.atoznback.aztcollab``).

Five jobs:

  1. Initialize the GitHub App identity once. This APK is the only
     suite component that holds GitHub App credentials; peer apps
     never call ``azt_collabd.configure``.
  2. Register the AZTCollabProvider Java callbacks via pyjnius so
     peers' ContentResolver.call() reaches our dispatch table.
  3. Start the sticky-bound AZTServiceProviderhost so the host
     process is pinned across this Activity's teardown. Without
     this, ``activity.finish()`` from the picker ends the only
     component keeping the process alive — the JVM exits, the
     ContentProvider goes with it, and any peer that just received
     a content:// URI grant gets SIGKILL'd via the
     "depends on provider in dying proc" cascade. Starting the
     service before the picker does its work keeps the provider
     reachable until the service idle-stops itself (5 min
     of zero peer activity, see ``server_apk/service.py``).
  4. Read the launching Intent's action. If a peer fired
     ``startActivityForResult`` with action
     ``org.atoznback.aztcollab.PICK_PROJECT``, mount the picker UI;
     otherwise mount the settings UI as the standard launcher
     Activity. Same PythonActivity handles both — the Activity reads
     the Intent at startup and chooses which Kivy app to run.
  5. Run whichever Kivy app the dispatch chose.

The service is sticky-bound (no foreground notification). Under
memory pressure Android may still kill the host; the next peer
ContentResolver call lazy-spawns it again, and the service body
runs ``reconcile_on_startup()`` to mark in-flight scheduler jobs as
``JOB_INTERRUPTED``. See ``azt-collab/CLAUDE.md`` recovery semantics.
"""

import os
import sys

# When packaged with buildozer the resulting APK bundles azt_collabd
# and azt_collab_client as top-level packages (symlinked in from the
# parent repo at packaging time — `bash server_apk/setup.sh` creates
# them). When running this file from a desktop checkout, walk up one
# level so imports resolve.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _candidate in (_HERE, _PARENT):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import azt_collabd

_PICK_ACTION = 'org.atoznback.aztcollab.PICK_PROJECT'


def _launch_intent_action():
    """Return the action string of the Intent that started this
    Activity, or '' if not on Android / unable to read it."""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        intent = PythonActivity.mActivity.getIntent()
        if intent is None:
            return ''
        return intent.getAction() or ''
    except Exception:
        return ''


def _launch_source():
    """Return ``'peer'`` if the launching Intent carries the
    ``azt_launch_source=peer`` extra (set by
    ``azt_collab_client._open_server_ui_android``), else ``'user'``
    (launcher-icon tap, ``adb am start``, etc.). Distinguishes peer-
    driven settings-open from a user-direct launcher tap so the
    server-APK's on_start can pick an update-check UX appropriate
    to each: badge for peer-driven, popup for user-direct.

    Falls through to ``'user'`` on any failure — the conservative
    default matches the user-direct path's "popup on boot if
    newer" behaviour."""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        intent = PythonActivity.mActivity.getIntent()
        if intent is None:
            return 'user'
        src = intent.getStringExtra('azt_launch_source')
        if src == 'peer':
            return 'peer'
    except Exception:
        pass
    return 'user'


def _ensure_provider_service():
    """Start AZTServiceProviderhost if we're on Android. Idempotent —
    Android collapses repeat startService into a single running
    instance. The service runs ./service.py in a separate process
    thread and pins the host so the picker Activity finishing doesn't
    take the ContentProvider down with it. Falls through silently on
    desktop and on any pyjnius / classloader failure (the loopback
    auto-spawn path covers desktop, and a service-start failure on
    Android is recoverable via the next provider lazy-spawn)."""
    try:
        from kivy.utils import platform
    except Exception:
        return
    if platform != 'android':
        return
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        ServiceClass = autoclass(
            'org.atoznback.aztcollab.AZTServiceProviderhost')
        ServiceClass.start(PythonActivity.mActivity, '')
        print('[server_apk] AZTServiceProviderhost.start invoked',
              flush=True)
    except Exception as ex:
        print(f'[server_apk] service start failed: {ex}', flush=True)


def main():
    # 0. Diagnostic: which presplash bucket landed at install time.
    #    No-op on desktop / on jnius failure. Distinct tag from peer
    #    logs so a combined logcat is grep-able.
    try:
        from azt_collab_client.lowpower import log_presplash_variant
        log_presplash_variant(tag='presplash:server')
    except Exception as ex:
        print(f'[server_apk] presplash diag skipped: {ex}', flush=True)

    # 1. Server APK is the canonical Github App identity holder.
    #    Override the defaults if your suite ships under a different
    #    GitHub App slug.
    azt_collabd.configure(
        app_slug=os.environ.get('AZT_GITHUB_APP_SLUG', 'azt-collaboration'),
        client_id=os.environ.get('AZT_GITHUB_APP_CLIENT_ID',
                                 'Iv23li66Fo9MBReatv6i'),
        collaborator=os.environ.get('AZT_GITHUB_COLLABORATOR',
                                    'kent-rasmussen'),
    )

    # 2. Register the ContentProvider callbacks. No-op on desktop so
    #    this same script can be smoke-tested with
    #    `python server_apk/main.py`.
    try:
        from azt_collabd.android_cp import service as _cp
        _cp.install_callbacks()
    except Exception as ex:
        print(f'[server_apk] provider install skipped: {ex}')

    # 2a. Pre-warm jnius-touching state on the main thread. Both
    #    helpers below have lazy-init paths that call into pyjnius
    #    autoclass + JNI method invocation on first read. Calling
    #    them HERE forces that work to happen on the daemon main
    #    thread (which inherits the app classloader) instead of
    #    deferring to whichever background Python thread happens to
    #    need the value first (Timer-spawned sync workers, HTTP
    #    server threads, etc.) — Python-spawned threads attach to
    #    the JVM with the bootclassloader, which has triggered
    #    NULL-deref SIGSEGV in art::JNI::CallObjectMethodA on field
    #    reads against app context. After this warmup both helpers
    #    serve from cached state (config.json / process memory) for
    #    every subsequent caller on any thread.
    try:
        from azt_collabd import store as _store
        from azt_collabd.paths import azt_home as _azt_home
        _azt_home()
        _store.get_device_name()
    except Exception as ex:
        print(f'[server_apk] jnius prewarm skipped: {ex}', flush=True)
    # 2a.1. Belt-and-braces prewarm of the PackageManager classes
    # used by ``android_cp/service.py::_pkg_last_update_time``.
    # The 60 s self-update poller runs on a main-spawned Timer
    # thread, but the dispatch callback (Thread-3) historically
    # also touched these classes — moved off that path in 0.43.23
    # but kept the prewarm here so any future code that ends up
    # calling getPackageManager/getPackageInfo from a worker
    # thread starts with the classes already cached. Field log
    # baf 2026-05-20 captured the original SIGSEGV in
    # ``art::JNI::CallObjectMethodA`` here.
    try:
        from jnius import autoclass
        _PythonService = autoclass('org.kivy.android.PythonService')
        ctx = getattr(_PythonService, 'mService', None)
        if ctx is not None:
            # Resolve the chain we'll later traverse from the
            # dispatch thread. These calls cache the classloader
            # bindings so subsequent worker-thread invocations
            # don't pay the bootclassloader penalty.
            pm = ctx.getPackageManager()
            if pm is not None:
                pm.getPackageInfo(ctx.getPackageName(), 0)
    except Exception as ex:
        print(f'[server_apk] PackageManager prewarm skipped: {ex}',
              flush=True)

    # 2b. Crash-marker bookkeeping. Detect "previous process didn't
    #    run atexit" (SIGSEGV / SIGKILL / OOM-kill / kernel kill —
    #    anything that bypasses normal teardown), write the
    #    one-liner to $AZT_HOME/last_native_crash.json so /v1/health
    #    can surface it to peers, then arm the sentinel for THIS
    #    process so the next startup can do the same. Best-effort:
    #    failures here don't block daemon startup.
    try:
        from azt_collabd import crash_marker as _crash
        from azt_collabd.paths import azt_home as _azt_home
        _home = _azt_home()
        _crash.record_ungraceful_shutdown_if_any(_home)
        _crash.arm_graceful_shutdown_marker(_home)
    except Exception as ex:
        print(f'[server_apk] crash_marker setup skipped: {ex}',
              flush=True)

    # 3. Start the sticky-bound service so the host process is pinned
    #    across the upcoming activity.finish(). Must happen BEFORE the
    #    picker mounts so that even an immediate cancel-and-finish
    #    flow leaves the service running for any peer that received a
    #    URI grant from a previous pick.
    _ensure_provider_service()

    # 4. Run the unified picker+settings Kivy app. Initial screen +
    #    submit-handler branch depend on launch_mode:
    #    - PICK_PROJECT intent (peer-driven): launch_mode='external',
    #      picker is initial; submit fires setResult/finish.
    #    - no intent / launcher tap (user opened the server APK to
    #      tweak settings): launch_mode='internal', settings is
    #      initial; the Switch-project button there can navigate to
    #      the picker in-process and the picker's submit returns to
    #      settings instead of finishing the Activity.
    from azt_collabd.ui.picker_app import main as picker_main
    if _launch_intent_action() == _PICK_ACTION:
        picker_main(launch_mode='external')
    else:
        picker_main(launch_mode='internal',
                    launch_source=_launch_source())


if __name__ == '__main__':
    main()
