"""Shared path conventions (server side).

Resolves $AZT_HOME with platform fallbacks. Duplicated in azt_collab_client
to keep the client independent of the server package.
"""

import os
import sys


def _android_files_dir():
    """Return the app's private writable ``filesDir``, or None if not
    on Android / unable to query.

    Necessary because p4a does not set ``$HOME``: a bare
    ``os.path.expanduser('~')`` resolves to ``/data``, which is the
    Android system-data root and is **not** writable by the app's
    UID. Without this path query, ``azt_home()`` would return
    ``/data/.local/share/azt`` and every subsequent file op would
    fail with ``[Errno 13] Permission denied``.

    Three probes in order of reliability:

    1. ``ActivityThread.currentApplication()`` — Android internal
       that returns the running Application object. Works in *any*
       process Android starts (Activity, Service, Provider) regardless
       of whether p4a's PythonActivity / PythonService have hit the
       point in their startup where they set their respective static
       ``mActivity`` / ``mService`` fields. This is the canonical
       way to reach a Context from anywhere in an Android process.
    2. ``PythonService.mService`` — set late in PythonService.run
       (after loadLibraries, before nativeStart). If the dispatch
       callback fires from a binder thread before the service main
       thread has set this, probe 1 covers the gap. (Also: pyjnius
       static-field reads have been known to return None for
       transiently-set Java fields under some classloader paths.)
    3. ``PythonActivity.mActivity`` — same logic for the Activity
       process. Probe 1 already covers it; this is belt-and-braces.

    All three return the same per-UID ``getFilesDir()``, so any one
    of them is sufficient."""
    try:
        from jnius import autoclass
    except ImportError:
        return None
    # Probe 1: ActivityThread.currentApplication — works in any
    # process with an Application context, which is every Android
    # process by the time user code runs.
    try:
        ActivityThread = autoclass('android.app.ActivityThread')
        app = ActivityThread.currentApplication()
        if app is not None:
            return str(app.getFilesDir().getAbsolutePath())
    except Exception:
        pass
    # Probe 2: PythonService.mService — the :provider process.
    try:
        PythonService = autoclass('org.kivy.android.PythonService')
        service = PythonService.mService
        if service is not None:
            return str(service.getFilesDir().getAbsolutePath())
    except Exception:
        pass
    # Probe 3: PythonActivity.mActivity — the Activity process.
    try:
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        if activity is not None:
            return str(activity.getFilesDir().getAbsolutePath())
    except Exception:
        pass
    return None


def azt_home():
    """Return the AZT server's home directory (created on first use by the
    server). Respects $AZT_HOME; falls back to platform conventions."""
    p = os.environ.get('AZT_HOME')
    if p:
        return p
    android_dir = _android_files_dir()
    if android_dir:
        return os.path.join(android_dir, 'azt')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/azt')
    xdg = os.environ.get('XDG_DATA_HOME') or os.path.expanduser(
        '~/.local/share')
    return os.path.join(xdg, 'azt')


def server_info_path():
    return os.path.join(azt_home(), 'server.json')
