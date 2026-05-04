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
import threading
import time

from .. import server as _server
from ..paths import azt_home


_installed = False

# Activity tracking for idle-stop policy.
_state_lock = threading.Lock()
_last_touch_monotonic = time.monotonic()
_bound_count = 0


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


def install_callbacks():
    """Register the Python dispatch + openFile callbacks with the Java
    AZTCollabProvider class. Idempotent. No-op off Android."""
    global _installed, _dispatch_cb, _openfile_cb
    if _installed:
        return
    if not _is_android():
        return
    try:
        from jnius import autoclass, PythonJavaClass, java_method
    except ImportError:
        return

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
    ``$AZT_HOME/projects/``. Returns None on path-traversal attempts or
    structurally-disallowed shapes so the Java side raises
    FileNotFoundException.

    ``rel`` is expected as the URI's path component (Java-side
    ``Uri.getPath()``), which always carries a leading slash. The
    ``lstrip('/')`` below makes ``os.path.join(base, rel)`` compose
    under ``base`` instead of treating ``rel`` as absolute.

    Allowed path shapes (defence-in-depth — even ``..`` would be
    caught by ``commonpath``, but rejecting structurally lets us also
    catch peer mistakes like asking for a sibling project's tree
    through ``<lang>/../<other>/audio/foo.wav`` and surfaces them as
    a clear FileNotFoundError instead of an opaque containment fail):

    - ``<lang>/<file>.lift``     — top-level LIFT file
    - ``<lang>/audio/<file>``    — sibling audio recording
    - ``<lang>/images/<file>``   — sibling image asset

    Anything else returns None. ``..`` and empty segments are
    rejected explicitly; ``/`` inside a segment is impossible after
    splitting."""
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

    base = os.path.realpath(os.path.join(azt_home(), 'projects'))
    target = os.path.realpath(os.path.join(base, rel))
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
