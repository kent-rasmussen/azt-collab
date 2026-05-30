"""Burst-mode LAN discovery (Phase 3 of the 0.50 sync rebuild).

When ``lan.autodiscovery=False`` (eventual default), the daemon
doesn't continuously announce + browse + hold radio locks. Instead,
a *burst* arms the LAN for a bounded window — typically triggered
by ``sync_nudge`` on either side, or by a post-commit fan-out — so
two paired phones can find each other when their users explicitly
ask to sync.

State flow per burst:

  1. ``arm_for_discovery`` increments the ref count in ``lan_fgs``;
     ``lan_listener.apply_toggle`` sees the bump and brings up the
     listener + mDNS advertise + mDNS browse + WifiLock/Mcast/FGS.
  2. The burst worker thread sleeps ``window_s``.
  3. ``disarm_for_discovery`` drops the ref. ``apply_toggle`` runs
     again; if neither autodiscovery is on nor another burst is
     active, everything tears down.

Idempotency: multiple concurrent bursts share the same window —
the *latest* requested expiry wins, so a new nudge while one is
already in flight extends rather than truncates. Implemented via
a single worker thread that watches a shared expiry timestamp.

Default window is 30 s — long enough for both phones'
``sync_nudge`` bursts to overlap even if the user taps the second
phone seconds later; short enough that an unanswered burst doesn't
drain battery much.

Note on ``lan.autodiscovery=True``: bursts are still cheap to call
in that mode — they just increment a ref count above a baseline
that already keeps the radio up. ``apply_toggle`` no-ops because
``is_running()`` is already True.
"""

from __future__ import annotations

import sys
import threading
import time


# Default burst window. Sized for "two users tap sync within
# half a minute of each other and discover each other" — the
# overlap window between two staggered nudges.
DEFAULT_WINDOW_S = 30.0

_LOCK = threading.Lock()
_STATE = {
    'expires_at': 0.0,   # monotonic time when the current window ends
    'thread': None,      # current worker thread, or None when idle
}


def start_burst(window_s: float = DEFAULT_WINDOW_S) -> float:
    """Arm a discovery burst that ends at ``now + window_s``. If a
    burst is already running and its expiry is later than the
    requested one, returns the existing expiry (no extension);
    otherwise extends the existing burst's expiry.

    Returns the absolute monotonic timestamp when the burst will
    end. Caller does not need to do anything with it — it's just
    useful for diagnostics / tests.
    """
    new_expiry = time.monotonic() + max(1.0, float(window_s))
    with _LOCK:
        existing_expiry = _STATE['expires_at']
        if new_expiry > existing_expiry:
            _STATE['expires_at'] = new_expiry
        thread = _STATE['thread']
        if thread is None or not thread.is_alive():
            t = threading.Thread(target=_burst_worker,
                                 name='lan-burst',
                                 daemon=True)
            _STATE['thread'] = t
            # Arm BEFORE the thread runs so apply_toggle sees the
            # ref bump even if the thread hasn't started yet. The
            # worker is responsible for disarming when its window
            # expires.
            _arm()
            t.start()
            print(f'[lan-burst] started, window={window_s:.1f}s',
                  file=sys.stderr, flush=True)
        else:
            # Concurrent extension; the running worker will pick
            # up the new expiry from _STATE on its next tick.
            print(f'[lan-burst] extended, new expiry=+{window_s:.1f}s',
                  file=sys.stderr, flush=True)
        return _STATE['expires_at']


def _arm():
    """Bump the discovery ref + tell the listener to bring things
    up. Both halves are idempotent; if autodiscovery is already
    True the listener is already up and ``apply_toggle`` no-ops."""
    try:
        from .android_cp import lan_fgs as _lan_fgs
        _lan_fgs.arm_for_discovery()
    except Exception as ex:
        print(f'[lan-burst] arm_for_discovery raised: {ex!r}',
              file=sys.stderr, flush=True)
    try:
        from . import lan_listener as _lan_listener
        _lan_listener.apply_toggle()
    except Exception as ex:
        print(f'[lan-burst] apply_toggle on arm raised: {ex!r}',
              file=sys.stderr, flush=True)


def _disarm():
    """Drop the discovery ref + tell the listener to reconcile.
    If autodiscovery is on, ``apply_toggle`` keeps everything up;
    otherwise it tears the listener / mDNS / locks back down."""
    try:
        from .android_cp import lan_fgs as _lan_fgs
        _lan_fgs.disarm_for_discovery()
    except Exception as ex:
        print(f'[lan-burst] disarm_for_discovery raised: {ex!r}',
              file=sys.stderr, flush=True)
    try:
        from . import lan_listener as _lan_listener
        _lan_listener.apply_toggle()
    except Exception as ex:
        print(f'[lan-burst] apply_toggle on disarm raised: {ex!r}',
              file=sys.stderr, flush=True)


def _burst_worker():
    """Sleep until the latest expiry in ``_STATE['expires_at']``,
    then disarm. Re-checks the expiry each wake so a concurrent
    ``start_burst`` extending the window keeps us alive.

    Sleep granularity is bounded so the worker reacts to a
    daemon shutdown within ~1 s even if the burst window is
    long."""
    while True:
        with _LOCK:
            expires_at = _STATE['expires_at']
        now = time.monotonic()
        remaining = expires_at - now
        if remaining <= 0:
            break
        # Wake at most every 1.0 s to catch shutdown / extension
        # without holding the lock for the full window.
        time.sleep(min(remaining, 1.0))
    with _LOCK:
        _STATE['thread'] = None
        _STATE['expires_at'] = 0.0
    _disarm()
    print('[lan-burst] window ended', file=sys.stderr, flush=True)


def is_active() -> bool:
    """True iff a burst is currently armed. Cheap; safe from any
    thread. For diagnostics / project_status enrichment."""
    with _LOCK:
        return _STATE['thread'] is not None and \
            time.monotonic() < _STATE['expires_at']


def snapshot() -> dict:
    """Diagnostic view of the current burst state."""
    with _LOCK:
        return {
            'active': (_STATE['thread'] is not None
                       and time.monotonic() < _STATE['expires_at']),
            'expires_in_s': max(0.0,
                                _STATE['expires_at'] - time.monotonic()),
        }
