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


def _windows_appdata_bases():
    """Per-user data-dir candidates, best first, EXISTING dirs only.
    Env vars are conveniences and machines exist without %LOCALAPPDATA%
    (seen 2026-07-16); the shell API is authoritative and
    env-independent. None of these are OneDrive-synced locations.
    A LIST (not one winner) so _windows_azt_home can pin to wherever a
    home already exists."""
    bases = []
    try:
        import ctypes  # CSIDL_LOCAL_APPDATA == 28
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(
                None, 28, None, 0, buf) == 0:
            bases.append(buf.value)
    except Exception:
        pass
    appdata = os.environ.get('APPDATA', '')
    bases += [
        os.environ.get('LOCALAPPDATA'),
        os.path.join(os.path.dirname(appdata), 'Local')
            if appdata else None,
        appdata or None,  # Roaming: fine for this small state
        os.path.join(os.environ.get('USERPROFILE', ''),
                     'AppData', 'Local')
            if os.environ.get('USERPROFILE') else None,
        os.path.expanduser('~'),  # floor: always exists
    ]
    out = []
    for b in bases:
        if b and os.path.isdir(b) and b not in out:
            out.append(b)
    return out or [os.path.expanduser('~')]


def _windows_azt_home():
    """``<per-user appdata>\\azt``, SELF-PINNING: an ``azt`` home already
    existing under ANY candidate base is adopted as-is, so the location
    is fixed by first creation and immune to environment drift between
    runs (env vars appearing/disappearing, launch-context differences).
    Only when no home exists anywhere is one placed at the best
    candidate. Pre-0.54.6 there was no Windows branch at all (the XDG
    fallback produced ``C:\\Users\\X/.local/share\\azt``); state written
    there is relocated once."""
    bases = _windows_appdata_bases()
    for b in bases:
        home = os.path.join(b, 'azt')
        if os.path.isdir(home):
            return home  # pinned by prior use
    home = os.path.join(bases[0], 'azt')
    legacy = os.path.join(os.path.expanduser('~/.local/share'), 'azt')
    if os.path.isdir(legacy):
        try:
            os.replace(legacy, home)
        except OSError:
            return legacy  # couldn't move: keep using the old spot
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
