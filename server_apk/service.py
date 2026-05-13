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

# Service process starts with a fresh interpreter; the path setup that
# main.py does for the Activity process must be repeated here so
# ``import azt_collabd`` resolves to the bundled package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _candidate in (_HERE, _PARENT):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)


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


_bridge_stdio_to_logcat()


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


def main():
    _boot_trace('main_entered')
    print('[service] AZTServiceProviderhost: starting Python body',
          flush=True)
    _boot_trace('before_import_azt_collabd')
    import azt_collabd
    _boot_trace('after_import_azt_collabd')
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
