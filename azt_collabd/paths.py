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


# Cached resolution of azt_home(). First call computes (which on
# Android costs 3-4 JNI calls into ActivityThread / PythonService /
# PythonActivity); every subsequent call reads this module global.
#
# Why cache: pre-0.43.30 azt_home() re-fired _android_files_dir on
# every call, which meant the ContentProvider OpenFile callback
# (which routes through _resolve_path → azt_home for cawl/audio FD
# serves) burned ~4 JNI invocations per request on the Java Binder
# dispatch thread. Under sustained traffic (cawl image prefetch
# does many openFile per second), one of those eventually NPE'd
# inside art::JNI::CallObjectMethodA — same crash class as the
# pre-0.43.23 dispatch-thread bug but on the OpenFile path that
# the earlier fix didn't cover. Field log baf 2026-05-20 caught it
# at pid=23550 tid=23558 (binder:23550_1).
#
# Safe to cache: the value depends only on the running APK's UID-
# scoped filesDir, which never changes for the lifetime of a
# process. Setting via env (AZT_HOME) is also handled by the
# cache — module reload (e.g. test rig) is the only way to flush.
_AZT_HOME_CACHE = None


def azt_home():
    """Return the AZT server's home directory (created on first use by the
    server). Respects $AZT_HOME; falls back to platform conventions.

    Cached after the first call — see ``_AZT_HOME_CACHE``."""
    global _AZT_HOME_CACHE
    if _AZT_HOME_CACHE is not None:
        return _AZT_HOME_CACHE
    p = os.environ.get('AZT_HOME')
    if p:
        _AZT_HOME_CACHE = p
        return p
    android_dir = _android_files_dir()
    if android_dir:
        _AZT_HOME_CACHE = os.path.join(android_dir, 'azt')
        return _AZT_HOME_CACHE
    if sys.platform == 'win32':
        _AZT_HOME_CACHE = _windows_azt_home()
        return _AZT_HOME_CACHE
    if sys.platform == 'darwin':
        _AZT_HOME_CACHE = os.path.expanduser(
            '~/Library/Application Support/azt')
        return _AZT_HOME_CACHE
    xdg = os.environ.get('XDG_DATA_HOME') or os.path.expanduser(
        '~/.local/share')
    _AZT_HOME_CACHE = os.path.join(xdg, 'azt')
    return _AZT_HOME_CACHE


def _windows_appdata_bases():
    """Per-user data-dir candidates, best first, EXISTING dirs only.
    Mirrored in azt_collab_client/paths.py (duplicated intentionally —
    keep in step). Env vars are conveniences and machines exist without
    %LOCALAPPDATA% (seen 2026-07-16); the shell API is authoritative and
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
    runs. Mirrored in azt_collab_client/paths.py (keep in step). Only
    when no home exists anywhere is one placed at the best candidate.
    Pre-0.54.6 there was no Windows branch (the XDG fallback produced
    ``C:\\Users\\X/.local/share\\azt``); state there is relocated once."""
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


def server_info_path():
    return os.path.join(azt_home(), 'server.json')
