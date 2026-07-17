"""Client-side copy of AZT_HOME resolution. Duplicated intentionally to
keep azt_collab_client free of any azt_collabd dependency."""

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

    Three probes; see azt_collabd/paths.py for the full rationale.
    Mirrored here to keep azt_collab_client free of any azt_collabd
    dependency."""
    try:
        from jnius import autoclass
    except ImportError:
        return None
    try:
        ActivityThread = autoclass('android.app.ActivityThread')
        app = ActivityThread.currentApplication()
        if app is not None:
            return str(app.getFilesDir().getAbsolutePath())
    except Exception:
        pass
    try:
        PythonService = autoclass('org.kivy.android.PythonService')
        service = PythonService.mService
        if service is not None:
            return str(service.getFilesDir().getAbsolutePath())
    except Exception:
        pass
    try:
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        if activity is not None:
            return str(activity.getFilesDir().getAbsolutePath())
    except Exception:
        pass
    return None


def _windows_azt_home():
    """``%LOCALAPPDATA%\\azt``. Before 0.54.6 there was no Windows branch
    at all and the XDG fallback produced ``C:\\Users\\X/.local/share\\azt``
    (mixed separators; wrong convention). If a daemon already wrote state
    there, relocate it once so nothing is orphaned."""
    base = (os.environ.get('LOCALAPPDATA')
            or os.path.join(os.path.expanduser('~'), 'AppData', 'Local'))
    home = os.path.join(base, 'azt')
    legacy = os.path.join(os.path.expanduser('~/.local/share'), 'azt')
    if os.path.isdir(legacy) and not os.path.isdir(home):
        try:
            os.makedirs(base, exist_ok=True)
            os.replace(legacy, home)
        except OSError:
            home = legacy  # couldn't move: keep using the old spot
    return home


def azt_home():
    p = os.environ.get('AZT_HOME')
    if p:
        return p
    android_dir = _android_files_dir()
    if android_dir:
        return os.path.join(android_dir, 'azt')
    if sys.platform == 'win32':
        return _windows_azt_home()
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/azt')
    xdg = os.environ.get('XDG_DATA_HOME') or os.path.expanduser(
        '~/.local/share')
    return os.path.join(xdg, 'azt')


def server_info_path():
    return os.path.join(azt_home(), 'server.json')
