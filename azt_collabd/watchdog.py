"""Self-monitoring for the daemon: notice a wedge, dump the evidence,
and — if it persists — restart out of it.

Why this exists (field 2026-07-27): the daemon can be *alive* and
answering ``/v1/health`` while its real work is stuck. Health is
lock-free and served on its own HTTP thread, so it stays green
throughout. That evening produced a silent log, RPCs timing out, then
connections being accepted and dropped, four phones unable to see the
computer — and no way to tell "busy" from "broken", let alone name the
culprit. Recovery was a human noticing and pressing Restart server.

The two halves here are deliberately ordered **diagnose, then
recover**, because a restart destroys the evidence:

1. On the first sign of a stall, log one line plus every thread's
   stack (``_dump_all_thread_stacks``, via
   ``sys._current_frames()``). That is
   what identifies the stuck code on a machine we don't have access
   to, and it is why the dump happens even when restarting is
   disabled.
2. Only if the stall persists well past that, restart the process.
   Restart is cheap by design (jobs → typed ``JOB_INTERRUPTED`` for
   peer retry, transfers retried by the sender, uncommitted bytes
   power-cut-contained, the listener re-binds its remembered port,
   backoff curves survive), which is what makes automatic recovery
   defensible here at all.

Signals watched:

- **Loop heartbeats** (``scheduler.heartbeat_ages()``). ``iface-watch``
  ticks every ~3 s and ``watcher`` at most every ~60 s, so a heartbeat
  minutes old means that loop is not running.
- **Held project locks** (``locks.held_snapshot()``). A lock held for
  minutes is network I/O under the lock (the regression tracked in
  ``agenda/daemon_lock_across_network_io.md``) or a deadlock.

What this deliberately does NOT do: restart because a single RPC was
slow, or because a merge is taking a while. Thresholds are minutes,
not seconds, and a restart is rate-limited — killing legitimate work
mid-flight would trade one bad evening for a livelock on slow
machines.

Knobs (``$AZT_HOME/config.json``):

| key | default | meaning |
|---|---|---|
| ``watchdog.enabled`` | True | run the monitor at all |
| ``watchdog.warn_s`` | 120 | stall age that triggers the traceback dump |
| ``watchdog.restart_s`` | 600 | stall age that triggers a restart (0 = never) |
| ``watchdog.interval_s`` | 30 | how often to check |

Since 0.54.90.
"""

from __future__ import annotations

import os
import sys
import threading
import time

_thread = None
_stop = None
# Episode tracking: dump a traceback ONCE per stall, not every tick —
# a repeating all-thread dump would bury the log it is meant to
# illuminate.
_dumped_for_episode = False
_last_restart_at = 0.0
# Grace after startup: boot does real work (reconcile, publish
# reconcile, diag snapshot) and no loop has ticked yet, so every
# heartbeat is legitimately "missing" for a moment.
_STARTUP_GRACE_S = 90.0
_started_at = 0.0


def _cfg(key, default):
    try:
        from . import settings as _settings
        val = _settings.get(key, default)
        return default if val is None else val
    except Exception:
        return default


def _stall_report():
    """Return ``(worst_age_s, lines)`` describing anything that looks
    stalled right now. ``worst_age_s`` is 0.0 when everything is
    healthy. Never raises — a monitor that dies on a probe is worse
    than no monitor."""
    worst = 0.0
    lines = []
    try:
        from . import scheduler as _sched
        ages = _sched.heartbeat_ages() or {}
    except Exception:
        ages = {}
    for name, age in sorted(ages.items()):
        # Per-loop expectation: iface-watch ~3 s, watcher up to ~60 s
        # with backoff. Use a single generous bar rather than
        # per-loop tuning — we are looking for minutes, not jitter.
        if age > _cfg('watchdog.warn_s', 120):
            worst = max(worst, float(age))
            lines.append(f'loop {name!r} last ticked {age:.0f}s ago')
    try:
        from .locks import held_snapshot
        held = held_snapshot()
    except Exception:
        held = []
    for row in held:
        age = float(row.get('held_s') or 0)
        if age > _cfg('watchdog.warn_s', 120):
            worst = max(worst, age)
            lines.append(
                f'project lock {row.get("key")!r} held {age:.0f}s by '
                f'thread {row.get("holder")!r}')
    return worst, lines


def _dump_state(reason, lines):
    """One summary line, the current counters, then every thread's
    stack. Written to stderr so it lands in the daemon log."""
    try:
        print(f'[watchdog] STALL DETECTED ({reason}): '
              + '; '.join(lines), file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        threads = threading.active_count()
        try:
            fds = len(os.listdir('/proc/self/fd'))
        except OSError:
            fds = -1
        print(f'[watchdog] threads={threads} fds={fds}',
              file=sys.stderr, flush=True)
    except Exception:
        pass
    _dump_all_thread_stacks()


def _dump_all_thread_stacks():
    """All-thread stacks via ``sys._current_frames()``, printed line by
    line through the ordinary log path.

    NOT ``faulthandler.dump_traceback`` (0.55.12). It needs a real file
    descriptor, and on Android ``sys.stderr`` is Kivy's
    ``ProcessingStream`` — field 2026-07-27, on the very first real
    stall the watchdog caught: ``traceback dump failed:
    AttributeError("'ProcessingStream' object has no attribute
    'fileno'")``. The dump is the entire diagnostic value of the warn
    stage, so it must not depend on the stream being an fd.

    Going through ``print`` also puts the stacks in the per-day daemon
    log, which is what 'Share diagnostics' ships — a raw-fd write could
    land outside it. Pure-Python and stream-agnostic, so it works on
    both platforms and on whatever a host has swapped stderr for."""
    try:
        print('[watchdog] --- all thread stacks follow ---',
              file=sys.stderr, flush=True)
        import traceback as _tb
        names = {}
        try:
            for t in threading.enumerate():
                names[t.ident] = t.name
        except Exception:
            pass
        frames = sys._current_frames()
        for tid, frame in list(frames.items()):
            label = names.get(tid, '?')
            print(f'[watchdog] thread {tid} ({label}):',
                  file=sys.stderr, flush=True)
            try:
                for line in _tb.format_stack(frame):
                    for sub in line.rstrip().splitlines():
                        print(f'[watchdog]   {sub}',
                              file=sys.stderr, flush=True)
            except Exception as ex:
                print(f'[watchdog]   <stack unavailable: {ex!r}>',
                      file=sys.stderr, flush=True)
        print('[watchdog] --- end thread stacks ---',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[watchdog] traceback dump failed: {ex!r}',
              file=sys.stderr, flush=True)


def _restart(worst, lines):
    """Re-exec (desktop) / exit for respawn (Android). Same mechanism
    the admin-restart RPC uses; see that handler for the platform
    split."""
    global _last_restart_at
    _last_restart_at = time.monotonic()
    print(f'[watchdog] stall persisted {worst:.0f}s — restarting the '
          f'daemon ({"; ".join(lines)})', file=sys.stderr, flush=True)
    try:
        from . import server as _server
        _server._h_admin_restart({})
    except Exception as ex:
        print(f'[watchdog] restart raised: {ex!r} — leaving the '
              f'process up', file=sys.stderr, flush=True)


def _loop():
    global _dumped_for_episode
    while _stop is not None and not _stop.is_set():
        interval = float(_cfg('watchdog.interval_s', 30) or 30)
        _stop.wait(interval)
        if _stop.is_set():
            return
        if time.monotonic() - _started_at < _STARTUP_GRACE_S:
            continue
        try:
            worst, lines = _stall_report()
        except Exception as ex:
            print(f'[watchdog] check raised: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        if not lines:
            if _dumped_for_episode:
                print('[watchdog] stall cleared — loops and locks are '
                      'moving again', file=sys.stderr, flush=True)
            _dumped_for_episode = False
            continue
        if not _dumped_for_episode:
            _dump_state('first detection', lines)
            _dumped_for_episode = True
        restart_s = float(_cfg('watchdog.restart_s', 600) or 0)
        if restart_s <= 0:
            continue
        if worst < restart_s:
            continue
        # Rate limit: never turn a slow machine into a restart loop.
        # The stall must also outlive one full cycle of our own
        # checks, which the threshold already guarantees.
        if time.monotonic() - _last_restart_at < max(600.0, restart_s):
            print('[watchdog] stall still present but a restart '
                  'happened recently — not restarting again',
                  file=sys.stderr, flush=True)
            continue
        _restart(worst, lines)


def start():
    """Start the monitor thread. Idempotent; no-op when
    ``watchdog.enabled`` is false. Call from every daemon entry point
    (``server.serve`` on desktop AND the server APK's
    ``service.py:main`` — a hook added to only one of them is a bug
    that hides for versions)."""
    global _thread, _stop, _started_at
    if not _cfg('watchdog.enabled', True):
        print('[watchdog] disabled by config', file=sys.stderr,
              flush=True)
        return
    if _thread is not None and _thread.is_alive():
        return
    _started_at = time.monotonic()
    _stop = threading.Event()
    _thread = threading.Thread(target=_loop, name='azt-watchdog',
                               daemon=True)
    _thread.start()
    print(f'[watchdog] started (warn={_cfg("watchdog.warn_s", 120)}s '
          f'restart={_cfg("watchdog.restart_s", 600)}s)',
          file=sys.stderr, flush=True)


def stop():
    global _thread, _stop
    if _stop is not None:
        _stop.set()
    _thread = None
