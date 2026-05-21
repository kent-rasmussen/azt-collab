"""
Sticky-bound service body for the aztcollab APK.

p4a's PythonService Java glue invokes this module on the service
process when AZTServiceProviderhost.start() (or Android-driven
respawn under START_STICKY) creates the service. Two jobs:

  1. Boot azt_collabd inside the service process so AZTCollabProvider
     callbacks are wired (install_callbacks) and any leftover
     scheduler jobs from the previous daemon process get reconciled
     to JOB_INTERRUPTED (reconcile_on_startup). Peers polling on a
     stale job_id then receive a typed transient-failure result
     instead of silence.

  2. Idle-stop loop. Wake every IDLE_CHECK_SECONDS; if no peers are
     bound AND no provider activity for IDLE_TIMEOUT_SECONDS, call
     stopSelf() and let the JVM unwind. The next peer
     ContentResolver call wakes the process again via Android's
     provider lazy-spawn contract; this re-runs the same module so
     reconcile happens again. Module-level code MUST be idempotent.

Why no foreground notification: the suite design wants the service
visible to Android (via bindService raising OOM priority) but not to
the user. SIL field linguists already see notifications from the host
peer (recorder); a second always-on notification would be noise.
Cost: under heavy memory pressure Android will kill us sooner than a
foreground service would be killed. Recovery is via START_STICKY plus
the unconditional provider lazy-spawn — see CLAUDE.md "Recovery
semantics" for the full matrix.
"""

import os
import sys
import time


# ── File-based boot diagnostic ──────────────────────────────────────────────
#
# logcat is unreachable from this point in service.py module load:
# p4a does NOT redirect stdio for PythonService (only PythonActivity),
# and the bridge that fixes that lives further down this file
# (``_bridge_stdio_to_logcat``). If service.py dies before reaching
# the bridge — which is exactly what the field-log silent-:provider-
# death pattern looks like — every ``print()`` and every Python
# traceback goes to a black hole.
#
# So write boot phase markers to a file under the app's private
# filesDir at every step. The file survives process death. The
# RecoveryActivity (loaded from classes.dex, never from the bundle)
# reads and displays this file via a "Show diagnostic log" button,
# so the field user can surface it even when nothing else works.
#
# Path matches Android's standard private-storage location for this
# package. UID-isolated, writable to this process without any
# permission. Hardcoded because resolving via jnius would itself
# need jnius to be healthy, which may be the very thing failing.
_BOOT_DIAG_PATH = os.path.join(
    os.environ.get('ANDROID_PRIVATE',
                   '/data/data/org.atoznback.aztcollab/files'),
    'service_boot.log')

# Keep the file from growing unbounded — every spawn appends, so a
# crash loop would otherwise fill flash. 100 KB is plenty for ~500
# spawn cycles' worth of trace.
try:
    if (os.path.exists(_BOOT_DIAG_PATH)
            and os.path.getsize(_BOOT_DIAG_PATH) > 100 * 1024):
        # Truncate by renaming + reopening — atomic, preserves any
        # in-flight file handle from a previous (dying) spawn.
        os.rename(_BOOT_DIAG_PATH, _BOOT_DIAG_PATH + '.prev')
except Exception:
    pass

# Keep a real OS-level handle for faulthandler to write SIGSEGV
# tracebacks into. Buffered = False so the trace lands on disk
# before the process dies.
try:
    _BOOT_DIAG_FD = open(_BOOT_DIAG_PATH, 'a', buffering=1)
except Exception:
    _BOOT_DIAG_FD = None


def _diag(phase):
    """Write a timestamped boot phase marker to the diag file.
    Best-effort: any failure silently swallowed (the file is for
    diagnostics, not control flow)."""
    if _BOOT_DIAG_FD is None:
        return
    try:
        _BOOT_DIAG_FD.write(
            f'[{time.time():.3f} pid={os.getpid()}] {phase}\n')
        _BOOT_DIAG_FD.flush()
    except Exception:
        pass


_diag('module_load_start')


import faulthandler
import threading

_diag('imports_done')

# Service process starts with a fresh interpreter; the path setup that
# main.py does for the Activity process must be repeated here so
# ``import azt_collabd`` resolves to the bundled package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _candidate in (_HERE, _PARENT):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

_diag('path_setup_done')

# Enable Python's native crash-handler so SIGSEGV / SIGABRT emit a
# Python-side traceback (one per thread) before the process dies.
# Routes to the boot-diag file (a real OS-level fd, unlike the
# logcat bridge below). Without this, a jnius-side NPE shows up as
# a libc tombstone with jnius.so frames we can't read symbols for —
# useless for pinpointing the Python line. With it, we get the
# actual call site as text in service_boot.log milliseconds before
# the tombstone. Cheap (a few hundred bytes of static allocation;
# signal handler runs only on crash). The ``all_threads=True`` flag
# is what makes this useful for the ``:provider`` process where the
# crash thread is rarely the main thread.
if _BOOT_DIAG_FD is not None:
    try:
        faulthandler.enable(file=_BOOT_DIAG_FD, all_threads=True)
    except Exception:
        # Fall back to stderr — useless on :provider but won't
        # crash module load.
        try:
            faulthandler.enable(file=sys.stderr, all_threads=True)
        except Exception:
            pass
else:
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception:
        pass

_diag('faulthandler_enabled')


def _thread_excepthook(args):
    """Log uncaught exceptions in worker threads with thread name +
    traceback. Without this, a thread that raises and exits
    silently leaves no trace in logcat — the daemon's commit /
    watcher / cawl-prefetch threads have all bitten us this way at
    least once. Routes to stderr so logcat captures it."""
    import traceback
    try:
        print(f'[thread-excepthook] thread={args.thread.name!r} '
              f'exc={args.exc_type.__name__}: {args.exc_value}',
              file=sys.stderr, flush=True)
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback,
            file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass


threading.excepthook = _thread_excepthook

_diag('thread_excepthook_set')


def _bridge_stdio_to_logcat():
    """Redirect ``sys.stdout`` and ``sys.stderr`` to ``android.util.Log``
    under tag ``python``.

    p4a auto-redirects stdio for ``PythonActivity`` (the picker UI's
    process, where Kivy peers run), but not for ``PythonService``
    (this `:provider` process, where the daemon — server.py,
    scheduler.py, repo.py — runs after the ContentProvider lazy-spawn).
    Without this bridge every ``print(..., file=sys.stderr)`` from the
    daemon side goes to a black hole, so a sync chain that's
    functionally correct (RPC returns a job_id, the timer fires, the
    repo gets pushed) appears in logcat as if nothing happened.

    Idempotent / no-op outside Android (jnius unavailable). Best-effort:
    a missing ``android.util.Log`` symbol falls through silently rather
    than wedging the daemon."""
    try:
        from jnius import autoclass
    except ImportError:
        return
    try:
        Log = autoclass('android.util.Log')
    except Exception:
        return

    class _LogcatWriter:
        def __init__(self, tag, level_fn):
            self.tag = tag
            self._level_fn = level_fn
            self._buf = ''

        def write(self, s):
            if not isinstance(s, str):
                try:
                    s = s.decode('utf-8', 'replace')
                except Exception:
                    s = str(s)
            self._buf += s
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                if line:
                    try:
                        self._level_fn(self.tag, line)
                    except Exception:
                        pass

        def flush(self):
            if self._buf:
                try:
                    self._level_fn(self.tag, self._buf)
                except Exception:
                    pass
                self._buf = ''

        def isatty(self):
            return False

    try:
        sys.stdout = _LogcatWriter('python', Log.i)
        sys.stderr = _LogcatWriter('python', Log.e)
    except Exception:
        pass


_diag('before_bridge_stdio')
_bridge_stdio_to_logcat()
_diag('after_bridge_stdio')


# Boot-timing instrumentation. Anchored at module load so each
# trace line is relative to "Python service entered". Both the
# peer (azt_collab_client.ui.bootstrap) and this daemon-side
# emit ``[boot-trace-*]`` lines; the parser at
# ``tests/integration/parse_boot_traces.py`` joins them on
# logcat wall-clock timestamps.
_proc_start_monotonic = time.monotonic()


def _boot_trace(phase, **fields):
    elapsed = time.monotonic() - _proc_start_monotonic
    extras = ''
    if fields:
        extras = ' ' + ' '.join(f'{k}={v}' for k, v in fields.items())
    print(f'[boot-trace-daemon] phase={phase} t={elapsed:.3f}{extras}',
          flush=True)
    _diag(f'boot_trace:{phase}')


_boot_trace('module_loaded')


# Idle-stop policy. Tunable but sized for typical SIL field-recorder
# sessions: a quick edit-record-pick burst easily fits in 5 minutes,
# while a longer offline-edit-then-go-online flow doesn't keep the
# service running needlessly. The next peer call wakes us again.
IDLE_CHECK_SECONDS = 30
IDLE_TIMEOUT_SECONDS = 300


def _stop_self():
    """Best-effort PythonService.stopSelf so the host process exits
    cleanly. Falls through silently if pyjnius isn't usable (e.g. the
    service body is being smoke-tested outside Android)."""
    try:
        from jnius import autoclass
        PythonService = autoclass('org.kivy.android.PythonService')
        svc = PythonService.mService
        if svc is not None:
            svc.stopSelf()
    except Exception as ex:
        print(f'[service] stopSelf failed: {ex}', flush=True)


def _bound_count():
    """Read AZTServiceProviderhost.sBoundCount via pyjnius. Returns 0
    on any pyjnius / classloader failure so the idle-stop loop errs on
    the side of believing nobody is bound."""
    try:
        from jnius import autoclass
        Service = autoclass(
            'org.atoznback.aztcollab.AZTServiceProviderhost')
        return int(Service.getBoundCount())
    except Exception:
        return 0


def _apk_path():
    """Return the running APK's filesystem path, or '' if jnius isn't
    available (desktop tests, broken pyjnius)."""
    try:
        from jnius import autoclass
        PythonService = autoclass('org.kivy.android.PythonService')
        ctx = PythonService.mService.getApplicationContext()
        return ctx.getApplicationInfo().sourceDir
    except Exception as ex:
        print(f'[service] _apk_path failed: {ex}', flush=True)
        return ''


def _bundle_dir():
    """``_python_bundle/`` inside the service's filesDir. p4a's C
    bootstrap extracted it on first launch; we may need to re-extract
    on APK update."""
    return os.path.join(_HERE, '_python_bundle')


def _maybe_reextract_python_bundle():
    """Detect a stale p4a unpack and re-extract from the new APK's
    assets if needed.

    Problem this solves: p4a's C bootstrap extracts ``assets/private.*``
    to ``_python_bundle/`` only when the directory is missing. On APK
    reinstall the directory remains, so the running daemon imports
    yesterday's Python code from disk regardless of how new the APK
    is. ``SuiteSelfReplaceReceiver`` kills the old process during
    package replace, but the respawn re-reads the same stale bundle.
    Symptom: ``/v1/admin/restart`` returns OK, the daemon dutifully
    respawns, and bootstrap's compat probe still sees the old
    version. Documented in SuiteSelfReplaceReceiver.java's NOTE on
    stale p4a unpack as "needs a different fix (Activity-launch-
    from-receiver, or a service-side extract-on-missing branch)"
    — this is the service-side branch.

    Detection: store ``_python_bundle/.apk_mtime`` carrying the APK
    file's mtime at the time the bundle was extracted/synced. On
    every service start, compare against the current APK mtime; if
    they differ (APK was replaced), the bundle is stale.

    Recovery: extract fresh ``assets/private.tar.gz`` (or
    ``private.tar`` / ``private.mp3`` — p4a's filename varies) to a
    sibling ``_python_bundle.new/``, atomically swap, write a fresh
    marker, then ``os._exit(0)``. Android's ContentProvider auto-
    spawn brings up a new ``:provider`` process which reads the now-
    fresh bundle.

    First-launch ever: bundle was just extracted by p4a's C
    bootstrap (the directory exists because we're running inside
    it), so we just stamp the marker without re-extracting.

    Best-effort: any failure (no jnius, can't read APK, extract
    error) logs and falls through. Worst case we run the old code
    one more cycle; the bootstrap loop guard in
    ``azt_collab_client/ui/bootstrap.py`` then surfaces the user
    popup instead of looping forever."""
    try:
        bundle_dir = _bundle_dir()
        marker = os.path.join(bundle_dir, '.apk_mtime')
        apk = _apk_path()
        if not apk or not os.path.isdir(bundle_dir):
            return False
        # Integer seconds, not float. Android filesystems and some
        # libc fstat implementations truncate APK mtime to whole
        # seconds on subsequent reads even though the first read can
        # carry sub-second precision (observed: first read
        # 1779309751.5585816, next read 1779309751.0). Comparing
        # floats with a half-second tolerance was tripping on this
        # difference every spawn, firing a spurious "stale unpack"
        # → ``os._exit(0)`` cycle that masqueraded as Android LMK
        # killing the daemon. Integer seconds are stable across
        # reads on every Android filesystem we've seen.
        apk_mtime = int(os.path.getmtime(apk))
        recorded = None
        try:
            with open(marker) as f:
                # ``int(float(...))`` tolerates legacy markers that
                # have a fractional component from the pre-0.43.22
                # bug above.
                recorded = int(float(f.read().strip()))
        except (OSError, ValueError):
            recorded = None
        if recorded is not None and apk_mtime == recorded:
            return False   # bundle matches running APK
        if recorded is None:
            # First launch after an update from a pre-marker daemon
            # (or first launch ever). Stamp the marker so we don't
            # spuriously re-extract every subsequent boot. p4a's
            # initial extract on this APK already ran, so the
            # bundle IS current.
            try:
                with open(marker, 'w') as f:
                    f.write(str(apk_mtime))
                print(f'[service] stamped first-run apk_mtime '
                      f'marker={apk_mtime!r}',
                      flush=True)
            except Exception as ex:
                print(f'[service] marker write failed: {ex}',
                      flush=True)
            return False
        # Marker is stale — APK was replaced and the running bundle
        # is from the previous install. We CANNOT safely re-extract
        # from inside :provider: the python bundle lives in
        # ``lib/<abi>/libpybundle.so`` (not in ``assets/``), it's
        # unpacked by p4a's PythonUtil with prefix stripping
        # (``pybundle``), and the proper unpack does
        # ``recursiveDelete(files/app/)`` which would wipe the very
        # code we're running. The 0.44.0 attempt to do it manually
        # via ``zipfile``+``tarfile`` extracted the wrong asset
        # (``private.tar.gz`` = app code, not python bundle) and
        # left ``_python_bundle/`` structurally present but
        # functionally corrupted — silent crash on next spawn.
        #
        # Safer: invalidate the .version markers PythonUtil uses,
        # so the NEXT picker Activity launch detects the mismatch
        # and runs the proper extract. Daemon continues with stale
        # code until that happens — same as the pre-0.43.22
        # default. User-visible recovery: open AZT Collaboration
        # to refresh.
        print(f'[service] stale _python_bundle/ detected: '
              f'apk_mtime={apk_mtime!r} marker={recorded!r}; '
              f'invalidating .version markers so the next picker '
              f'Activity launch re-extracts. Daemon will run on '
              f'stale bundle until then — open AZT Collaboration '
              f'to refresh now.',
              flush=True)
        try:
            parent = os.path.dirname(bundle_dir)
            for name in ('private.version', 'libpybundle.version'):
                marker_path = os.path.join(parent, name)
                try:
                    os.remove(marker_path)
                    print(f'[service] removed {name} '
                          f'(forces picker re-extract on next launch)',
                          flush=True)
                except FileNotFoundError:
                    pass
                except Exception as ex:
                    print(f'[service] could not remove {name}: '
                          f'{ex!r}', flush=True)
        except Exception as ex:
            print(f'[service] .version invalidation failed: {ex!r}',
                  flush=True)
        # Also stamp our marker forward so we don't keep invalidating
        # on every spawn — once the markers are invalidated, the next
        # picker launch is responsible for the actual refresh.
        try:
            with open(marker, 'w') as f:
                f.write(str(apk_mtime))
        except Exception:
            pass
        return False
    except Exception as ex:
        print(f'[service] stale-unpack check failed: {ex!r}',
              flush=True)
        return False


def _extract_bundle_from_apk(apk_path, bundle_dir, apk_mtime):
    """DEPRECATED — DO NOT CALL. Retained as reference only.

    0.44.0 attempted to do its own bundle re-extract from
    inside :provider when a stale unpack was detected. The
    approach was wrong on two counts:

    1. **Wrong asset.** This function extracts
       ``assets/private.tar.gz``, which contains the APP CODE
       (``main.py``, ``service.py``, ...) — not the Python
       bundle. The Python bundle (``stdlib.zip``, ``modules/``,
       ``site-packages/``) ships in ``lib/<abi>/libpybundle.so``
       (a tarball-with-.so-extension to dodge Android's APK
       compression). Extracting private.tar.gz into
       ``_python_bundle/`` produces a structurally-present-but-
       functionally-empty directory: bootstrap.c sees the dir
       and proceeds, Python can't find stdlib, the bridge fails
       silently before any output reaches logcat, process dies
       silently. Exactly the cascade-kill symptom 0.44.0 was
       supposed to fix.

    2. **Wrong execution context.** Even if we extracted the
       right assets, the proper unpack
       (``PythonUtil.unpackAsset``/``unpackPyBundle``) does
       ``recursiveDelete(files/app/)`` first — which would wipe
       the very code we're running. Doing the delete + extract
       from inside :provider while it imports from that
       directory is a contract violation.

    Replaced in 0.44.1 by the marker-invalidation approach in
    ``_maybe_reextract_python_bundle``: delete the .version
    markers PythonUtil uses, let the next picker Activity
    launch trigger a proper extract. Function body kept here
    for diff review / future reference; NOT called from any
    code path in 0.44.1+."""
    import zipfile
    import tarfile
    import shutil
    import io as _io
    import gzip

    # bz2 is optional. p4a's default build doesn't include the _bz2
    # C extension, so ``import bz2`` raises ModuleNotFoundError on a
    # bare Android bundle. We only need it for the legacy
    # ``private.mp3`` asset (older p4a renamed the bz2-compressed
    # tarball to dodge Android's automatic .tar compression).
    # Current p4a ships ``private.tar.gz`` which gzip handles.
    try:
        import bz2 as _bz2_mod
        _bz2_decompress = _bz2_mod.decompress
    except ImportError:
        _bz2_decompress = None

    parent = os.path.dirname(bundle_dir)
    new_dir = bundle_dir + '.new'
    old_dir = bundle_dir + '.old'
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir)
    if os.path.exists(old_dir):
        shutil.rmtree(old_dir)
    os.makedirs(new_dir, exist_ok=True)

    decoders = [(lambda b: gzip.decompress(b), 'gzip')]
    if _bz2_decompress is not None:
        decoders.append((lambda b: _bz2_decompress(b), 'bz2'))
    decoders.append((lambda b: b, 'plain'))

    asset_data = None
    asset_used = ''
    decompressor_used = ''
    with zipfile.ZipFile(apk_path) as apkz:
        for name in ('assets/private.tar.gz',
                     'assets/private.tar',
                     'assets/private.mp3'):
            try:
                raw = apkz.read(name)
            except KeyError:
                continue
            # Try each available decoder until one yields a valid tar.
            for decoder, decoder_name in decoders:
                try:
                    cand = decoder(raw)
                    # Validate as tar by trying to open it.
                    with tarfile.open(fileobj=_io.BytesIO(cand)) as _tf:
                        _tf.getmembers()
                    asset_data = cand
                    asset_used = name
                    decompressor_used = decoder_name
                    break
                except Exception:
                    continue
            if asset_data is not None:
                break
    if asset_data is None:
        raise RuntimeError(
            f'no readable bundle asset (private.tar.gz / '
            f'private.tar / private.mp3) in {apk_path!r}')
    print(f'[service] extracting {asset_used} ({decompressor_used}) '
          f'→ {new_dir}',
          flush=True)
    with tarfile.open(fileobj=_io.BytesIO(asset_data)) as tf:
        tf.extractall(new_dir)

    # Write the marker INSIDE the new dir before swapping so it's
    # present atomically with the new code.
    with open(os.path.join(new_dir, '.apk_mtime'), 'w') as f:
        f.write(str(apk_mtime))

    # Atomic swap.
    os.rename(bundle_dir, old_dir)
    try:
        os.rename(new_dir, bundle_dir)
    except Exception:
        # Roll back if the second rename fails.
        os.rename(old_dir, bundle_dir)
        raise
    shutil.rmtree(old_dir, ignore_errors=True)
    print(f'[service] _python_bundle/ swapped; exiting so the next '
          f'spawn picks up the fresh code',
          flush=True)
    # Give the print a moment to flush through the logcat bridge.
    time.sleep(0.2)
    os._exit(0)


def main():
    _boot_trace('main_entered')
    print('[service] AZTServiceProviderhost: starting Python body',
          flush=True)
    # Before importing any bundled code: detect and recover from a
    # stale p4a unpack. If the APK was replaced since the bundle was
    # last extracted, re-extract from the new APK's assets and exit;
    # the next ContentProvider auto-spawn picks up the fresh code.
    # See _maybe_reextract_python_bundle for the full rationale.
    _maybe_reextract_python_bundle()
    _boot_trace('before_import_azt_collabd')
    import azt_collabd
    _boot_trace('after_import_azt_collabd')

    # Install the daemon-log-to-file tee here on the Android side.
    # The desktop loopback path covers itself via ``server.run()``
    # which calls ``maybe_install_stdio_tee`` directly, but on
    # Android the daemon lives in ``:provider`` and ``server.run()``
    # never executes — so without this call, the user's persisted
    # "Save daemon log to file" toggle would only ever take effect
    # via the runtime POST endpoint (which patches the running
    # process), and a daemon respawn after an idle-stop would lose
    # the mirror until the user re-touched the toggle. Calling here
    # rather than at module load lets us reach
    # ``azt_collabd.store.get_daemon_log_to_file`` (the import
    # above just landed) and means subsequent ``_boot_trace`` lines
    # (``configured``, ``before_install_callbacks``, …) and every
    # ``[recent]`` / ``[cawl]`` / ``[commit-*]`` print from the
    # running daemon both land in the on-disk log.
    try:
        from azt_collabd.server import maybe_install_stdio_tee
        maybe_install_stdio_tee()
    except Exception as ex:
        print(f'[service] daemon-log tee install skipped: {ex}',
              flush=True)
    azt_collabd.configure(
        app_slug=os.environ.get('AZT_GITHUB_APP_SLUG',
                                'azt-collaboration'),
        client_id=os.environ.get('AZT_GITHUB_APP_CLIENT_ID',
                                 'Iv23li66Fo9MBReatv6i'),
        collaborator=os.environ.get('AZT_GITHUB_COLLABORATOR',
                                    'kent-rasmussen'),
    )
    _boot_trace('configured')

    # Wire the AZTCollabProvider Java callbacks. Idempotent.
    from azt_collabd.android_cp import service as cp_service
    _boot_trace('before_install_callbacks')
    cp_service.install_callbacks()
    _boot_trace('after_install_callbacks')

    # Reconcile any in-flight scheduler jobs left over from the
    # previous daemon process (kill -9, OOM, etc.). Marks PENDING /
    # RUNNING jobs as DONE+JOB_INTERRUPTED so peer poll_job calls
    # surface a typed transient-failure result.
    from azt_collabd import scheduler
    _boot_trace('before_reconcile')
    scheduler.reconcile_on_startup()
    _boot_trace('after_reconcile')

    # Start the connectivity watcher so the push-drain loop runs.
    # Without this, the Android daemon commits locally on every
    # ``commit_project`` RPC but never pushes — peers see commits
    # accumulate indefinitely with no auto-sync, and the only way
    # to publish is the user-gestured ``sync_project`` (Sync
    # button). Field log baf 2026-05-20 caught the regression:
    # 3+ minutes after ``COMMITTED_LOCAL`` with no
    # ``[scheduler] drain pushes:`` line. The watcher was wired in
    # the loopback entry path (``server.run``) but never in the
    # Android ``:provider`` entry path here. Pre-0.43.29 every
    # Android peer needed manual Sync taps to publish; this line
    # restores parity with the desktop daemon.
    scheduler.start_watcher()

    _boot_trace('entering_idle_loop')
    # Idle-stop loop. Stays alive while peers are bound or the
    # provider is in active use; stops the service when both
    # conditions clear for IDLE_TIMEOUT_SECONDS. Android may also
    # kill us under memory pressure regardless; START_STICKY brings
    # us back.
    print('[service] entering idle-stop loop '
          f'(check={IDLE_CHECK_SECONDS}s timeout={IDLE_TIMEOUT_SECONDS}s)',
          flush=True)
    while True:
        time.sleep(IDLE_CHECK_SECONDS)
        bound = _bound_count()
        idle_for = cp_service.seconds_since_last_touch()
        if bound == 0 and idle_for > IDLE_TIMEOUT_SECONDS:
            print(f'[service] idle-stop: bound={bound} '
                  f'idle_for={idle_for:.0f}s — stopSelf()',
                  flush=True)
            _stop_self()
            return


if __name__ == '__main__':
    main()
