"""
Entry point for the standalone AZT Collaboration service APK
(``org.atoznback.aztcollab``).

Four jobs:

  1. Initialize the GitHub App identity once. This APK is the only
     suite component that holds GitHub App credentials; peer apps
     never call ``azt_collabd.configure``.
  2. Register the AZTCollabProvider Java callbacks via pyjnius so
     peers' ContentResolver.call() reaches our dispatch table.
  3. Read the launching Intent's action. If a peer fired
     ``startActivityForResult`` with action
     ``org.atoznback.aztcollab.PICK_PROJECT``, mount the picker UI;
     otherwise mount the settings UI as the standard launcher
     Activity. Same PythonActivity handles both — the Activity reads
     the Intent at startup and chooses which Kivy app to run.
  4. Run whichever Kivy app the dispatch chose.

No foreground-service notification. When peers go idle Android may
stop us; the next peer call wakes us via ContentResolver.
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
__version__ = azt_collabd.__version__

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

    # 3. Dispatch on Intent action.
    if _launch_intent_action() == _PICK_ACTION:
        from azt_collabd.ui.picker_app import main as picker_main
        picker_main()
        return

    # 4. Default: settings UI. The daemon's loopback HTTP server is
    #    spun up lazily by the first client call (auto-spawn). On
    #    Android the in-process pyjnius shim handles RPCs directly,
    #    so the loopback server stays dormant unless the UI itself
    #    triggers a client call that misses the cache.
    from azt_collabd.ui.app import main as ui_main
    ui_main()


if __name__ == '__main__':
    main()
