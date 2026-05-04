"""Shared path conventions (server side).

Resolves $AZT_HOME with platform fallbacks. Duplicated in azt_collab_client
to keep the client independent of the server package.
"""

import os
import sys


def _android_files_dir():
    """Return the running Android Activity's private writable
    ``filesDir``, or None if not on Android / unable to query.

    Necessary because p4a does not set ``$HOME``: a bare
    ``os.path.expanduser('~')`` resolves to ``/data``, which is the
    Android system-data root and is **not** writable by the app's
    UID. Without this path query, ``azt_home()`` would return
    ``/data/.local/share/azt`` and every subsequent file op would
    fail with ``[Errno 13] Permission denied``."""
    try:
        from jnius import autoclass
    except ImportError:
        return None
    try:
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        if activity is None:
            return None
        return str(activity.getFilesDir().getAbsolutePath())
    except Exception:
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
