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
"""

import json
import os

from .. import server as _server
from ..paths import azt_home


_installed = False

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
            return _resolve_path(rel, mode)

    _dispatch_cb = _Dispatch()
    _openfile_cb = _OpenFile()
    Provider.registerCallbacks(_dispatch_cb, _openfile_cb)
    _installed = True


def _resolve_path(rel, mode):
    """Map a provider-supplied relative path to an absolute path under
    ``$AZT_HOME/projects/``. Returns None on path-traversal attempts so
    the Java side raises FileNotFoundException.

    ``rel`` is expected as the URI's path component (Java-side
    ``Uri.getPath()``), which always carries a leading slash. The
    ``lstrip('/')`` below makes ``os.path.join(base, rel)`` compose
    under ``base`` instead of treating ``rel`` as absolute."""
    if not rel:
        return None
    rel = rel.lstrip('/')
    base = os.path.realpath(os.path.join(azt_home(), 'projects'))
    target = os.path.realpath(os.path.join(base, rel))
    # Containment check: target must live under base.
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    if not os.path.exists(target):
        # Allow creation in 'w' mode; deny otherwise.
        if mode and ('w' in mode or 'a' in mode):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            return target
        return None
    return target
