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

    # 3. Start the sticky-bound service so the host process is pinned
    #    across the upcoming activity.finish(). Must happen BEFORE the
    #    picker mounts so that even an immediate cancel-and-finish
    #    flow leaves the service running for any peer that received a
    #    URI grant from a previous pick.
    _ensure_provider_service()

    # 4. Dispatch on Intent action.
    if _launch_intent_action() == _PICK_ACTION:
        from azt_collabd.ui.picker_app import main as picker_main
        picker_main()
        return

    # 5. Default: settings UI. The daemon's loopback HTTP server is
    #    spun up lazily by the first client call (auto-spawn). On
    #    Android the in-process pyjnius shim handles RPCs directly,
    #    so the loopback server stays dormant unless the UI itself
    #    triggers a client call that misses the cache.
    from azt_collabd.ui.app import main as ui_main
    ui_main()


if __name__ == '__main__':
    main()
