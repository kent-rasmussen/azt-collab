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
    since this process started. Reads a cached flag updated by the
    main-thread poller ``_self_update_poller``.

    Pre-0.43.23 this called ``_pkg_last_update_time()`` inline on
    every dispatch — two jnius method invocations per RPC, all
    running on the Java dispatch thread. The dispatch thread is a
    Python-spawned worker that attaches to JVM with the
    bootclassloader; under sustained jnius traffic from there, one
    of the calls eventually NPE'd inside ``art::JNI::CallObject-
    MethodA`` and SIGSEGV'd ``:provider``. Field log baf 2026-05-20
    captured the tombstone at pid=8859 tid=8912 (Thread-3) right
    after a cawl image FD serve — the Activity Manager then
    cascade-killed the peer ("depends on provider in dying proc")
    and the user saw the recorder die on every other restart.

    Now the PackageManager poll runs on a 60-second main-thread
    timer (started from ``install_callbacks``). Dispatch reads the
    flag with zero jnius calls. Up-to-60s detection latency for a
    package replace is well within the existing
    ``_schedule_exit_for_update`` 0.5 s grace window, so the
    user-visible behaviour is unchanged."""
    return _self_updated_flag


# Set by ``_self_update_poller`` on the main thread; read by the
# dispatch callback on Thread-3. Single-writer / single-reader bool
# — no lock needed (Python's GIL covers the load/store).
_self_updated_flag = False


def _self_update_poller():
    """Main-thread loop: every ``_SELF_UPDATE_POLL_S`` seconds, ask
    PackageManager whether the running APK's ``lastUpdateTime`` has
    advanced past the snapshot taken at ``install_callbacks``. On a
    positive read, flip ``_self_updated_flag`` to True; the next
    dispatch picks it up and schedules the exit. The poller itself
    never exits — it just stops being relevant after the flag flips.

    Runs on a daemon Timer thread spawned from the main thread
    after ``install_callbacks`` is called from
    ``server_apk/main.py``. The thread inherits the app classloader
    via that main-thread chain, so its jnius calls (
    ``_pkg_last_update_time`` → ``PackageManager.getPackageInfo``)
    run from a properly-initialised JNIEnv — distinct from the
    Java-spawned dispatch thread that crashed pre-0.43.23.

    60 s is a deliberate compromise: faster polling gains nothing
    (the only consumer is the schedule-exit gate, which can
    tolerate up-to-respawn-cadence latency) and burns flash / CPU
    on a hot device. On most upgrade flows the next peer call
    arrives within a second of install completion anyway, so the
    user sees the new daemon on their first interaction."""
    global _self_updated_flag
    if _self_updated_flag:
        return  # already flipped; nothing to do
    try:
        current = _pkg_last_update_time()
    except Exception as ex:
        # Treat any failure as "no signal" — the next tick retries.
        # A persistent failure leaves the flag False forever, which
        # is the conservative answer (better than spurious exits).
        print(f'[android_cp] self-update poll failed: {ex}',
              file=sys.stderr, flush=True)
        current = None
    if current is not None and _initial_pkg_update_time is not None:
        if current > _initial_pkg_update_time:
            _self_updated_flag = True
            print(f'[android_cp] self-update detected: '
                  f'apk_update_time {_initial_pkg_update_time} → '
                  f'{current}',
                  file=sys.stderr, flush=True)
            return
    t = threading.Timer(
        _SELF_UPDATE_POLL_S, _self_update_poller)
    t.name = 'self-update-poll'
    t.daemon = True
    t.start()


_SELF_UPDATE_POLL_S = 60.0


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
    t = threading.Timer(0.5, lambda: os._exit(0))
    t.name = 'self-update-exit'
    t.daemon = True
    t.start()


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

    # Kick off the main-thread self-update poller. The first tick
    # runs after ``_SELF_UPDATE_POLL_S`` seconds; ``_check_self_
    # updated`` reads a cached flag the poller maintains so the
    # dispatch callback (on Thread-3) never touches jnius itself.
    # See ``_self_update_poller`` for the full rationale on why
    # this had to move off the dispatch path.
    t = threading.Timer(
        _SELF_UPDATE_POLL_S, _self_update_poller)
    t.name = 'self-update-poll-init'
    t.daemon = True
    t.start()

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
    the project's actual ``working_dir`` from projects.json — or, for
    CAWL paths, under ``$AZT_HOME/cawl/<owner>/<repo>/...``. Returns
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

    - ``<lang>/<file>.lift``         — top-level LIFT file
    - ``<lang>/audio/<file>``        — sibling audio recording
    - ``<lang>/images/<file>``       — sibling image asset
    - ``<lang>/cawl/index.json``     — CAWL image-URL index for this
                                       project's image_repo
    - ``<lang>/cawl/images/<file>``  — CAWL image binary; fetched
                                       lazily on first access

    The first segment is the **langcode** — the daemon's
    ``projects.json`` key — not the on-disk directory name. CAWL
    paths look project-scoped at the URI layer but the underlying
    files live under ``$AZT_HOME/cawl/<owner>/<repo>/...`` so
    multiple projects pointing at the same image_repo share one
    on-disk cache."""
    if not rel:
        return None
    rel = rel.lstrip('/')
    parts = rel.split('/')
    # Reject empty segments and parent-traversal anywhere.
    if any(p in ('', '..', '.') for p in parts):
        return None
    from .. import projects as _projects

    # CAWL routing: <lang>/cawl/index.json or <lang>/cawl/images/<base>.
    # Resolution leaves the project's working_dir entirely; the cache
    # lives under $AZT_HOME/cawl/<owner>/<repo>/... keyed by the
    # project's cawl_image_repo (per-project field, falling back to
    # the daemon-global default).
    if len(parts) >= 2 and parts[1] == 'cawl':
        return _resolve_cawl_path(parts[0], parts[2:], mode)

    # Atomic-commit two-phase routing: <lang>/_atomic_pending/<token>.
    # Peers writing a large LIFT / media file open this URI for write
    # to ship the bytes via FD (Binder size cap doesn't apply to the
    # FD path), then call ``POST /v1/projects/<lang>/atomic_finalize``
    # to atomic-rename the scratch file into the canonical location
    # under ``project_lock``. The two phases together preserve the
    # same atomicity contract that ``atomic_commit_bytes``'s single-
    # RPC path offers — but split so bytes don't have to fit in a
    # Bundle. Scratch files live under
    # ``<working_dir>/.azt_atomic_pending/<token>``; the finalize
    # endpoint reads them by token. See _h_project_atomic_finalize.
    if len(parts) == 3 and parts[1] == '_atomic_pending':
        return _resolve_atomic_pending_path(parts[0], parts[2], mode)

    # Existing project-scoped shapes.
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


_TOKEN_RE = None


def _is_safe_pending_token(token):
    """True if ``token`` is structurally safe to use as a filename
    under ``.azt_atomic_pending/``. Accepts hex / underscores /
    hyphens only — peers should pass ``secrets.token_hex(N)`` or
    similar. Rejects empty, overlong, or anything with path
    separators / shell metas."""
    global _TOKEN_RE
    if _TOKEN_RE is None:
        import re
        _TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
    return isinstance(token, str) and bool(_TOKEN_RE.match(token))


def _resolve_atomic_pending_path(langcode, token, mode):
    """Map ``<lang>/_atomic_pending/<token>`` to a per-project
    scratch file under ``<working_dir>/.azt_atomic_pending/<token>``.

    Used by ``LiftHandle.atomic_open_write`` / ``MediaHandle.
    atomic_open_write`` on Android to ship large bytes via the
    ContentProvider FD path (which has no Binder size cap) before
    a tiny ``atomic_finalize`` RPC renames the scratch into the
    canonical location under ``project_lock``.

    Write mode creates the parent dir on demand. Read mode is
    rejected — peers don't read pending files; the finalize RPC
    does on the daemon side. Returns ``None`` on token-validation
    failure or unknown langcode."""
    if not _is_safe_pending_token(token):
        return None
    if not mode or 'w' not in mode:
        # The pending file is daemon-internal — peers only ever
        # write to it. A read attempt is structurally suspicious.
        return None
    from .. import projects as _projects
    p = _projects.get(langcode)
    if p is None or not p.working_dir:
        return None
    base = os.path.realpath(p.working_dir)
    pending_dir = os.path.join(base, '.azt_atomic_pending')
    target = os.path.realpath(os.path.join(pending_dir, token))
    # Containment — token validation already rejects '/' and '..',
    # but realpath + commonpath is the belt-and-braces check that
    # catches any future structural-check gap.
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    os.makedirs(pending_dir, exist_ok=True)
    return target


def _resolve_cawl_path(langcode, rest, mode):
    """Map a CAWL provider path to an absolute file. ``rest`` is
    the path segments *after* ``<lang>/cawl/``. CAWL files are
    read-only from the peer's perspective (mode 'r' only); 'w' /
    'a' attempts return None so the Java side surfaces
    FileNotFoundException.

    Two shapes accepted:

    - ``[index.json]``    → daemon's cached index for this
                            project's image_repo. Fetches lazily
                            if missing.
    - ``[images, <path...>]`` → daemon's cached image binary at
                                ``<path>`` (relative to the repo's
                                images root). May be a flat
                                filename or a nested path
                                (e.g. ``['images', '0001_body',
                                'foo.png']``) — CAWL repos
                                commonly nest images under
                                category subdirs.

    Path-traversal-safe at two layers: the outer
    ``_resolve_path`` already rejected any ``..``/``.`` segment
    in ``rel``, so ``rest`` here is structurally clean; and
    ``cawl.get_image_path`` re-validates + does a
    ``commonpath`` containment check against the realpath of
    the cache dir. The repo lookup goes through
    ``cawl.resolve_image_repo`` so the per-project field
    overrides the daemon-global default."""
    if mode and ('w' in mode or 'a' in mode):
        return None
    from .. import projects as _projects
    p = _projects.get(langcode)
    if p is None:
        return None
    from .. import cawl as _cawl
    repo = _cawl.resolve_image_repo(langcode)
    if not repo:
        return None
    if len(rest) == 1 and rest[0] == 'index.json':
        # Triggers a fetch/refresh as needed and writes the cache
        # file. Return its path even if get_index served from
        # stale cache — the file is there either way.
        _cawl.get_index(repo)
        target = _cawl.index_path(repo)
        return target if os.path.isfile(target) else None
    if len(rest) >= 2 and rest[0] == 'images':
        # Join remaining segments back into a forward-slash
        # rel-path. ``cawl.get_image_path`` revalidates and does
        # the containment check; we just hand it the joined
        # form. Segments came from Uri.getPath() which gives us
        # the URL-decoded form already.
        rel_path = '/'.join(rest[1:])
        target, source = _cawl.get_image_path(repo, rel_path)
        # On-demand fetches during an active prefetch contribute
        # to the source counters (no-op outside any prefetch
        # window). Mirrors ``server._h_cawl_image``; see the
        # comment there. 0.50.30.
        if target is not None and source:
            _cawl._bump_source_counter(repo, source)
        return target
    return None
