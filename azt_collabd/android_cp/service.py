"""
Pyjnius shim that wires the AZTCollabProvider Java class to the
azt_collabd dispatch table.

Call ``install_callbacks()`` once at app startup (from the recorder's
``MyApp.on_start`` is fine). It's a no-op on non-Android platforms,
so the same call is safe everywhere.

After install, sibling AZT apps that hold the suite signature can call
the provider's ``call(method, arg, extras)`` and reach
``azt_collabd.server.dispatch(...)`` with no further plumbing.

File reads (audio, images) come through ``openFile`` and route to
``_resolve_path`` below, which scopes paths to ``$AZT_HOME/projects/``
to prevent path-traversal attacks from a malicious-but-signed sibling.

Activity tracking: every dispatch / openFile call updates
``_last_touch_monotonic`` and ``onBind`` / ``onUnbind`` adjust
``_bound_count``. The sticky-bound service body
(``server_apk/service.py``) reads these via ``seconds_since_last_touch``
and ``bound_client_count`` to decide when the host process can
``stopSelf``: idle = no bound clients AND no provider activity for
``IDLE_TIMEOUT_SECONDS``. A subsequent peer ContentResolver call wakes
the process again via Android's provider lazy-spawn contract.
"""

import json
import os
import sys
import threading
import time

from .. import server as _server
from ..paths import azt_home


_installed = False

# Activity tracking for idle-stop policy.
_state_lock = threading.Lock()
_last_touch_monotonic = time.monotonic()
_bound_count = 0

# Recorded once at ``install_callbacks()`` time; used by
# ``_check_self_updated()`` to detect that the running APK was
# replaced underneath us. Android's package installer normally kills
# the process being upgraded, but custom-ROM battery savers and
# adb-side ``pm install -r`` can leave the old daemon running with
# stale code while the new APK is on disk. Without the auto-exit,
# peers keep talking to the old code and the only fix is for the
# user to force-stop the server APK by hand. milliseconds since
# epoch (Java conventions); ``None`` off Android or before
# ``install_callbacks`` is invoked.
_initial_pkg_update_time = None


def touch():
    """Mark a peer interaction. Called from the dispatch + openFile
    callbacks below. Cheap (single monotonic clock + lock); safe from
    any binder thread."""
    global _last_touch_monotonic
    with _state_lock:
        _last_touch_monotonic = time.monotonic()


def seconds_since_last_touch():
    """How long since the last peer call into the provider. Returns a
    large number on a freshly-started service that has never been
    touched (initial value is process-start time, so this is the
    seconds-since-startup until the first call)."""
    with _state_lock:
        return time.monotonic() - _last_touch_monotonic


def bound_client_count():
    """Current number of peers holding a bindService connection to the
    sticky-bound service. Updated by _on_bind / _on_unbind callbacks
    fired from the Java service class."""
    with _state_lock:
        return _bound_count


def _on_bind():
    global _bound_count
    with _state_lock:
        _bound_count += 1
    touch()


def _on_unbind():
    global _bound_count
    with _state_lock:
        _bound_count = max(0, _bound_count - 1)
    touch()

# Strong refs to the PythonJavaClass proxy instances handed to
# AZTCollabProvider.registerCallbacks. Java holds them for dispatch,
# but pyjnius does not pin them on the Python side — without these
# globals a GC cycle frees the proxies and the next binder-thread
# callback into them dereferences a freed type object. That manifests
# as SIGSEGV in _PyType_Lookup on Thread-3, with the call coming from
# AZTCollabProvider.call → NativeInvocationHandler.invoke (see
# CHANGELOG azt_collabd 0.10.6).
_dispatch_cb = None
_openfile_cb = None


def _is_android():
    try:
        from kivy.utils import platform
        return platform == 'android'
    except Exception:
        return False


def _android_context():
    """Return the running Android Context (Service preferred, Activity
    as fallback) or ``None`` if neither is available. The server APK's
    daemon lives in the ``:provider`` service process where
    ``PythonService.mService`` is set; the standalone settings UI runs
    as an Activity where ``PythonActivity.mActivity`` is set instead.
    Either is sufficient for ``getPackageManager()``."""
    try:
        from jnius import autoclass
    except ImportError:
        return None
    for cls_name, attr in (
        ('org.kivy.android.PythonService', 'mService'),
        ('org.kivy.android.PythonActivity', 'mActivity'),
    ):
        try:
            cls = autoclass(cls_name)
            ctx = getattr(cls, attr, None)
            if ctx is not None:
                return ctx
        except Exception:
            continue
    return None


def _pkg_last_update_time():
    """Read ``PackageManager.getPackageInfo(...).lastUpdateTime`` for
    the running APK. Returns the timestamp (ms since epoch) or
    ``None`` if anything goes wrong (off Android, no context, JNI
    blow-up). The caller must treat ``None`` as "no signal" — never
    as "unchanged"."""
    ctx = _android_context()
    if ctx is None:
        return None
    try:
        pm = ctx.getPackageManager()
        pi = pm.getPackageInfo(ctx.getPackageName(), 0)
        return int(pi.lastUpdateTime)
    except Exception as ex:
        print(f'[android_cp] _pkg_last_update_time failed: {ex}',
              file=sys.stderr, flush=True)
        return None


def _check_self_updated():
    """``True`` iff the running APK's ``lastUpdateTime`` has advanced
    since this process started. Cheap (single PackageManager call)
    and side-effect-free; the caller decides what to do (typically
    schedule a clean exit so the next ContentResolver call lazy-
    spawns the freshly-installed code)."""
    if _initial_pkg_update_time is None:
        return False
    current = _pkg_last_update_time()
    if current is None:
        return False
    return current > _initial_pkg_update_time


_exit_scheduled = False


def _schedule_exit_for_update():
    """Fire ``os._exit(0)`` on a short delay so the in-flight binder
    response has time to return to the peer. Idempotent — once
    scheduled, repeated dispatches don't pile up exit timers."""
    global _exit_scheduled
    if _exit_scheduled:
        return
    _exit_scheduled = True
    print('[android_cp] APK was updated — exiting so the next '
          'peer call lazy-spawns the new code',
          file=sys.stderr, flush=True)
    threading.Timer(0.5, lambda: os._exit(0)).start()


def install_callbacks():
    """Register the Python dispatch + openFile callbacks with the Java
    AZTCollabProvider class. Idempotent. No-op off Android."""
    global _installed, _dispatch_cb, _openfile_cb
    global _initial_pkg_update_time
    if _installed:
        return
    if not _is_android():
        return
    try:
        from jnius import autoclass, PythonJavaClass, java_method
    except ImportError:
        return

    # Snapshot the package's lastUpdateTime so we can detect a
    # subsequent in-place upgrade. Done before any callbacks are
    # wired so a stale process never sees a "self-updated" reading
    # against an uninitialised baseline.
    _initial_pkg_update_time = _pkg_last_update_time()

    Provider = autoclass(
        'org.atoznback.aztcollab.AZTCollabProvider')
    Bundle = autoclass('android.os.Bundle')
    DispatchCallback = autoclass(
        'org.atoznback.aztcollab.AZTCollabProvider$DispatchCallback')
    OpenFileCallback = autoclass(
        'org.atoznback.aztcollab.AZTCollabProvider$OpenFileCallback')

    class _Dispatch(PythonJavaClass):
        __javainterfaces__ = [
            'org/atoznback/aztcollab/AZTCollabProvider$DispatchCallback']
        __javacontext__ = 'app'

        @java_method('(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)Landroid/os/Bundle;')
        def dispatch(self, method, path, body_json):
            touch()
            try:
                body = json.loads(body_json) if body_json else None
            except Exception:
                body = None
            try:
                status, response = _server.dispatch(method, path, body)
            except Exception as ex:
                status = 500
                response = {'ok': False, 'error': str(ex)}
            b = Bundle()
            b.putInt('status', int(status))
            try:
                b.putString('json', json.dumps(response))
            except Exception:
                b.putString('json', '{"ok":false,"error":"unserializable"}')
            # Self-update auto-exit. Done *after* response is built
            # so the binder return carries this last reply before
            # the process goes down. Next peer call hits Android's
            # provider lazy-spawn contract and gets the new code.
            if _check_self_updated():
                _schedule_exit_for_update()
            return b

    class _OpenFile(PythonJavaClass):
        __javainterfaces__ = [
            'org/atoznback/aztcollab/AZTCollabProvider$OpenFileCallback']
        __javacontext__ = 'app'

        @java_method('(Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;')
        def resolveAbsPath(self, rel, mode):
            touch()
            return _resolve_path(rel, mode)

    _dispatch_cb = _Dispatch()
    _openfile_cb = _OpenFile()
    Provider.registerCallbacks(_dispatch_cb, _openfile_cb)
    _installed = True


_ALLOWED_MEDIA_DIRS = ('audio', 'images')


def _resolve_path(rel, mode):
    """Map a provider-supplied relative path to an absolute path under
    the project's actual ``working_dir`` from projects.json. Returns
    None on path-traversal attempts, unknown langcodes, or
    structurally-disallowed shapes so the Java side raises
    FileNotFoundException.

    ``rel`` is expected as the URI's path component (Java-side
    ``Uri.getPath()``), which always carries a leading slash. The
    ``lstrip('/')`` below normalises that.

    Allowed path shapes (defence-in-depth — even ``..`` would be
    caught by ``commonpath``, but rejecting structurally lets us also
    catch peer mistakes like asking for a sibling project's tree
    through ``<lang>/../<other>/audio/foo.wav`` and surfaces them as
    a clear FileNotFoundError instead of an opaque containment fail):

    - ``<lang>/<file>.lift``     — top-level LIFT file
    - ``<lang>/audio/<file>``    — sibling audio recording
    - ``<lang>/images/<file>``   — sibling image asset

    The first segment is the **langcode** — the daemon's
    ``projects.json`` key — not the on-disk directory name. Pre-0.21.2
    this code assumed the directory name == langcode and built
    ``$AZT_HOME/projects/<langcode>/...`` directly; that broke for
    clones whose ``dest_dir`` was URL-derived (e.g. ``en_Demo.git``
    cloned to ``projects/en_Demo/`` but the user chose langcode
    ``en``, so the URI ``content://.../en/SILCAWL.lift`` resolved to
    ``projects/en/SILCAWL.lift`` which didn't exist). Going through
    ``projects.get(langcode).working_dir`` keeps the URI form
    independent of the on-disk layout."""
    if not rel:
        return None
    rel = rel.lstrip('/')
    parts = rel.split('/')
    # Reject empty segments and parent-traversal anywhere.
    if any(p in ('', '..', '.') for p in parts):
        return None
    if len(parts) == 2:
        # <lang>/<file>.lift — the only top-level file shape.
        if not parts[1].lower().endswith('.lift'):
            return None
    elif len(parts) == 3:
        # <lang>/{audio|images}/<file>.
        if parts[1] not in _ALLOWED_MEDIA_DIRS:
            return None
    else:
        return None

    # Resolve the langcode → working_dir via the registry. Falls back
    # to ``$AZT_HOME/projects/<langcode>`` only when the project
    # isn't registered, which preserves pre-registry URIs (created
    # before the picker auto-registers) without breaking the new
    # decoupled-layout flow.
    from .. import projects as _projects
    langcode = parts[0]
    p = _projects.get(langcode)
    if p is not None and p.working_dir:
        base = os.path.realpath(p.working_dir)
        rel_under_base = os.path.join(*parts[1:])
    else:
        base = os.path.realpath(os.path.join(azt_home(), 'projects',
                                             langcode))
        rel_under_base = os.path.join(*parts[1:])
    target = os.path.realpath(os.path.join(base, rel_under_base))
    # Containment check: target must live under base. Belt-and-braces
    # alongside the structural check above; if a future symlink trick
    # bypasses the segment whitelist, this still 403s.
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    if not os.path.exists(target):
        # Allow creation in 'w'/'a' mode; mkdir -p the parent so the
        # first audio recording for a freshly-cloned project doesn't
        # need a separate mkdir RPC.
        if mode and ('w' in mode or 'a' in mode):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            return target
        return None
    return target
