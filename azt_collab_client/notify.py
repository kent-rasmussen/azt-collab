"""ContentObserver subscription API for peer apps.

Peers register a callback that fires whenever the daemon notifies
the project's status URI (HEAD advance, peer-observation update,
post-receive reset, toggle flip — every event that changes a
``project_status`` field). Per CLIENT_INTEGRATION.md § 17b's
notification-driven polling cadence: subscribe once on activity
foreground / project load, re-poll ``project_status`` on every
callback fire, drop the background tick to a low heartbeat
(60-120s) for sanity backstop.

API:

  subscribe_project_changes(langcode, callback) → token
      Wakes ``callback(uri_str)`` when the daemon notifies the
      per-project status URI for *langcode*.

  subscribe_global_changes(callback) → token
      Wakes ``callback(uri_str)`` when the daemon notifies the
      parent status URI (toggle flips) OR any per-project URI
      under it (uses ContentResolver's notifyForDescendants=True
      so a single registration catches everything).

  unsubscribe(token)
      Release the registration. Idempotent; safe to call on a
      token that's already been unsubscribed.

Returns ``None`` for the token off Android — peers should treat
that as "subscription not available, fall back to polling" and
continue working without push wakeups (the loopback daemon doesn't
have a ContentProvider, so peers there just poll).

Lifetime: callbacks run on the binder thread that delivered the
notification (no Handler passed to ContentObserver). If your
peer needs them on the UI thread, marshal in the callback
(``Clock.schedule_once(...)`` on Kivy). The pyjnius proxy AND
the Java observer are held strong-ref'd in module state so
neither side gets GC'd between events.
"""

import sys
import threading
import uuid


# token -> (uri_obj, observer_java_instance, callback_proxy)
_subscriptions = {}
_subscriptions_lock = threading.Lock()


def _is_android():
    try:
        from kivy.utils import platform
        return platform == 'android'
    except Exception:
        return False


def _app_context():
    """Return the running Android Context (Activity preferred for
    peer apps; Service as fallback). None off Android."""
    if not _is_android():
        return None
    try:
        from jnius import autoclass
    except ImportError:
        return None
    for cls_name, attr in (
        ('org.kivy.android.PythonActivity', 'mActivity'),
        ('org.kivy.android.PythonService', 'mService'),
    ):
        try:
            cls = autoclass(cls_name)
            ctx = getattr(cls, attr, None)
            if ctx is not None:
                return ctx
        except Exception:
            continue
    return None


def _make_callback_proxy(py_callback):
    """Build a PythonJavaClass implementing
    ``AZTStatusObserver$OnChangeCallback`` and forwarding to
    *py_callback*."""
    from jnius import PythonJavaClass, java_method

    class _CallbackProxy(PythonJavaClass):
        __javainterfaces__ = [
            'org/atoznback/aztcollab/'
            'AZTStatusObserver$OnChangeCallback']
        __javacontext__ = 'app'

        @java_method('(Ljava/lang/String;)V')
        def onChanged(self, uri):
            try:
                py_callback(uri or '')
            except Exception as ex:
                print(f'[notify] callback raised: {ex!r}',
                      file=sys.stderr, flush=True)

    return _CallbackProxy()


def _subscribe_uri(uri_str, callback, notify_for_descendants):
    """Internal: create observer, register against *uri_str*, store
    the strong-refs, return a token. None on failure / non-Android."""
    if not _is_android():
        return None
    ctx = _app_context()
    if ctx is None:
        return None
    try:
        from jnius import autoclass
        Uri = autoclass('android.net.Uri')
        Observer = autoclass(
            'org.atoznback.aztcollab.AZTStatusObserver')
        cb_proxy = _make_callback_proxy(callback)
        # Handler=null means the binder thread delivers onChange.
        # That's fine for our use — the callback queues a follow-up
        # project_status RPC, which itself is non-blocking.
        observer = Observer(None, cb_proxy)
        uri_obj = Uri.parse(uri_str)
        ctx.getContentResolver().registerContentObserver(
            uri_obj, bool(notify_for_descendants), observer)
        token = uuid.uuid4().hex
        with _subscriptions_lock:
            _subscriptions[token] = (uri_obj, observer, cb_proxy)
        return token
    except Exception as ex:
        print(f'[notify] subscribe to {uri_str!r} raised: {ex!r}',
              file=sys.stderr, flush=True)
        return None


def subscribe_project_changes(langcode, callback):
    """Register *callback* to fire when the daemon notifies
    ``content://org.atoznback.aztcollab/status/<langcode>``.

    Returns an opaque token (string) to pass to ``unsubscribe()``,
    or ``None`` off Android / on registration failure.

    The callback receives the notified URI string as its only
    argument; typically ignore it and immediately call
    ``project_status(langcode)`` to fetch fresh state. Multiple
    rapid wakeups can land back-to-back during a sync cascade —
    debouncing on the peer side (collapse all callbacks fired
    within ~200 ms into one re-poll) cuts redundant RPC."""
    if not langcode:
        return None
    uri = f'content://org.atoznback.aztcollab/status/{langcode}'
    return _subscribe_uri(uri, callback, notify_for_descendants=False)


def subscribe_global_changes(callback):
    """Register *callback* to fire for daemon-wide events
    (toggle flips, peer-list mutations) AND every per-project
    notification (via ``notifyForDescendants=True``).

    Used by project-list / picker UIs that render multiple
    projects' badges. Returns token / None per
    ``subscribe_project_changes``."""
    uri = 'content://org.atoznback.aztcollab/status'
    return _subscribe_uri(uri, callback, notify_for_descendants=True)


def unsubscribe(token):
    """Release the registration identified by *token*. Idempotent;
    safe on a token that's already been released or that was None
    (off-Android / failed-subscribe). Drops the strong-refs so the
    Python callback proxy and Java observer can be GC'd."""
    if not token:
        return
    with _subscriptions_lock:
        entry = _subscriptions.pop(token, None)
    if entry is None:
        return
    _uri_obj, observer, _cb_proxy = entry
    ctx = _app_context()
    if ctx is None:
        return
    try:
        ctx.getContentResolver().unregisterContentObserver(observer)
    except Exception as ex:
        print(f'[notify] unsubscribe raised: {ex!r}',
              file=sys.stderr, flush=True)
