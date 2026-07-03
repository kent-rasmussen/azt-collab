"""In-memory "a WAN sync is actively running" flag.

Layer 1 of the stuck-diverged-push fix (0.52.21). The Android
``:provider`` service self-stops after ``IDLE_TIMEOUT_SECONDS`` of no
ContentProvider activity + no bound peers (``server_apk/service.py``
idle loop). That idle measure is blind to a WAN fetch/merge/push
running in a scheduler thread — so closing the UI mid-push let the
service kill the process before the *resumable* chunked push
(``repo._push_chunked_to_ref``) could bank any progress. A large
diverged history (field repro: nml, 2167 commits unpushed) then never
converged: every daemon lifetime restarted from ``fetch begin`` and
got murdered again.

This module is the shared flag the idle loop consults. The scheduler
wraps every push attempt in ``in_flight()``; the idle loop refuses to
``stopSelf()`` while the count is nonzero.

**Same process.** The daemon (scheduler watcher thread) and the
service idle loop both live in the Android ``:provider`` interpreter
(``service.py`` runs ``scheduler.start_watcher()`` then enters the
idle loop), so a module-level counter is genuinely shared between
them. On desktop there is no idle loop; the flag is harmless.

Counter, not bool: nested / concurrent pushes (drain + a user Sync)
must both be able to hold the guard without one's exit clearing the
other's.
"""

from __future__ import annotations

import threading


_lock = threading.Lock()
_count = 0


class _Guard:
    def __enter__(self):
        global _count
        with _lock:
            _count += 1
        return self

    def __exit__(self, *_exc):
        global _count
        with _lock:
            if _count > 0:
                _count -= 1
        return False


def in_flight():
    """True while at least one WAN sync is actively running. Read by
    the Android idle-stop loop before ``stopSelf()``."""
    with _lock:
        return _count > 0


def guard():
    """Context manager marking a WAN sync as in flight for its
    duration. Re-entrant / concurrent-safe via the counter."""
    return _Guard()
