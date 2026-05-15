"""Ungraceful-shutdown detection for the daemon process.

Pragmatic substitute for a real JNI ``sigaction`` handler.
Rather than catching SIGSEGV in the dying process (which
requires async-signal-safe C code and JNI plumbing the
daemon doesn't want to take on yet), we detect the
*aftermath* on the next startup:

- Process write a sentinel file
  ``$AZT_HOME/process_running.json`` at startup with ``{pid,
  started_at}``.
- ``atexit`` removes the sentinel on clean shutdown.
- Next startup checks for the sentinel. If present (and the
  pid differs from the current one, which it will unless the
  same PID was somehow recycled), the previous process exited
  without running ``atexit`` — SIGSEGV, SIGKILL, OOM-kill,
  ``os._exit``, kernel panic, anything that bypasses normal
  teardown. Record a one-line summary to
  ``$AZT_HOME/last_native_crash.json`` so ``/v1/health``
  can surface it to peers.

What we DON'T capture (because we're not a signal handler):

- Which signal killed the previous process.
- Which thread it died in.
- The faulting PC or stack.

For that level of detail, a future change can wire a C-level
``sigaction`` handler (via a small native extension or
ctypes-driven libc call) that writes ``last_native_crash.json``
from the dying process itself with the richer metadata. The
schema accommodates that — ``signal`` / ``thread_name`` /
``approx_pc`` are present with empty defaults.

Note: ``os._exit`` and kernel-level kills both bypass
``atexit``, so this catches them too. That's a feature; any
abrupt termination the daemon didn't authorise is worth
surfacing.
"""

import atexit
import json
import os
import sys
import time


_SENTINEL_NAME = 'process_running.json'
_CRASH_RECORD_NAME = 'last_native_crash.json'


def _sentinel_path(azt_home):
    return os.path.join(azt_home, _SENTINEL_NAME)


def _crash_record_path(azt_home):
    return os.path.join(azt_home, _CRASH_RECORD_NAME)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.tmp.{os.getpid()}'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as ex:
        print(f'[crash_marker] write {path!r} failed: {ex}',
              file=sys.stderr, flush=True)


def record_ungraceful_shutdown_if_any(azt_home):
    """Called at startup. If a stale sentinel is found, the previous
    process didn't run ``atexit`` — record an ungraceful-shutdown
    crash record under ``$AZT_HOME/last_native_crash.json``.

    Idempotent against re-invocation: writing the record once is
    enough. A second startup with no sentinel (sentinel was deleted
    by atexit on the previous clean shutdown) is a no-op.
    """
    sentinel = _sentinel_path(azt_home)
    stale = _read_json(sentinel)
    if not stale:
        return None
    prev_pid = stale.get('pid')
    prev_started_at = stale.get('started_at')
    if prev_pid == os.getpid():
        # Same pid (rare; can happen on PID-reuse on long-lived
        # devices). Treat as clean — we have no signal to claim
        # otherwise.
        return None
    record = {
        'detected_at': time.time(),
        'previous_pid': prev_pid,
        'previous_started_at': prev_started_at,
        # Reserved for a future signal-handler-driven shape; empty
        # for now so peers reading the record don't have to branch
        # on shape version. When a real sigaction handler lands, it
        # populates these in the dying process before _exit().
        'signal': '',
        'thread_name': '',
        'approx_pc': '',
        'detection_source': 'ungraceful-shutdown sentinel',
    }
    _write_json(_crash_record_path(azt_home), record)
    print(f'[crash_marker] previous process (pid={prev_pid}) '
          f'exited without running atexit — recorded as ungraceful '
          f'shutdown',
          file=sys.stderr, flush=True)
    return record


def arm_graceful_shutdown_marker(azt_home):
    """Called at startup, after ``record_ungraceful_shutdown_if_any``.
    Writes the sentinel for THIS process and registers an
    ``atexit`` hook to remove it on clean shutdown.

    If clean shutdown runs, the sentinel goes away and the next
    startup sees nothing to record. If anything else terminates
    this process (signal, OOM-kill, ``os._exit``), the sentinel
    stays in place and the next startup surfaces the unclean
    exit."""
    sentinel = _sentinel_path(azt_home)
    _write_json(sentinel, {
        'pid': os.getpid(),
        'started_at': time.time(),
    })
    atexit.register(_unlink_sentinel, sentinel)


def _unlink_sentinel(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def read_last_native_crash(azt_home):
    """Return the persisted ``last_native_crash`` record dict, or
    None if there's nothing to surface. Read-only; ``/v1/health``
    consults this so peers can mirror the record in their own log
    after a daemon SIGSEGV (or other ungraceful exit) they
    otherwise wouldn't see."""
    return _read_json(_crash_record_path(azt_home))


def clear_last_native_crash(azt_home):
    """Remove the crash record. Optional — peers don't need to
    call this; the record is overwritten on the next ungraceful
    shutdown anyway. Exposed as an admin-tool seam for tests and
    for a future "clear diagnostics" affordance."""
    try:
        os.unlink(_crash_record_path(azt_home))
    except OSError:
        pass
