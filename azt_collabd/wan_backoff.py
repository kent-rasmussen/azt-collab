"""WAN push backoff with persistent state.

Per-project exponential backoff for WAN push attempts. Replaces the
pre-0.50 "fire every connectivity-poll tick when online" model with
"fire when the curve says it's time."

The curve doubles from 30 s up to a 24 h cap. Field contexts can be
offline 14 d at a time; at the cap we probe once a day, which is
~365× less radio chatter than the old model without sacrificing
eventual recovery. User can always reset the curve with a sync
nudge.

State is persisted to ``$AZT_HOME/wan_state.json`` because a
24 h backoff is meaningless if every daemon respawn (Android OOM,
APK reinstall, manual restart) resets to "try now." Schema per
langcode::

    {
      "consecutive_failures": 5,
      "last_attempt_at": 1748534400.0,
      "next_attempt_at": 1748538000.0
    }

Daemon lifecycle is **not** an intent signal (0.50.45): a
respawn does not reset, clear, or shorten the curve.
``reset_due_times_on_startup`` is retained as a no-op for
historic / external-caller compatibility but the scheduler no
longer calls it. Pre-0.50.45 the call gave every project a
free immediate retry on startup; on Android the daemon respawns
often enough (OOM, APK self-update, sticky-service restart)
that the effective cap was the respawn cadence, not the
documented 24 h. Now only ``record_success`` and ``nudge``
clear / advance the curve.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Optional

from .paths import azt_home


_BASE_S = 30.0
_CAP_S = 24 * 3600.0
_MAX_FAILURES_TRACKED = 30  # enough headroom to reach the cap


def _state_path() -> str:
    return os.path.join(azt_home(), 'wan_state.json')


def _load() -> dict:
    try:
        with open(_state_path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as ex:
        # Don't clobber a parse-failed file — keep it for forensics.
        # Behave as "no state" so the scheduler attempts on the next
        # opportunity (worst case: extra attempt when offline).
        print(f'[wan_backoff] load failed: {ex!r}',
              file=sys.stderr, flush=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.wan_state.', suffix='.tmp',
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _curve_seconds(consecutive_failures: int) -> float:
    """Backoff delay for the next attempt after *N* consecutive
    failures. ``0`` means due immediately."""
    if consecutive_failures <= 0:
        return 0.0
    n = min(int(consecutive_failures), _MAX_FAILURES_TRACKED)
    return min(_BASE_S * (2 ** (n - 1)), _CAP_S)


def next_due_at(langcode: str) -> float:
    """Unix timestamp when the next WAN attempt is due. ``0.0`` =
    immediately, no record on file."""
    entry = _load().get(langcode) or {}
    try:
        return float(entry.get('next_attempt_at', 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_due(langcode: str, now: Optional[float] = None) -> bool:
    if now is None:
        now = time.time()
    return now >= next_due_at(langcode)


def consecutive_failures(langcode: str) -> int:
    entry = _load().get(langcode) or {}
    try:
        return int(entry.get('consecutive_failures', 0) or 0)
    except (TypeError, ValueError):
        return 0


def record_success(langcode: str) -> None:
    """Reset the curve. Called after a successful push."""
    state = _load()
    if langcode in state:
        del state[langcode]
        _save(state)


def record_failure(langcode: str) -> None:
    """Advance the curve. Persists across restart so the curve
    survives daemon respawn.

    An explicit failure return is a *clean finish* of the attempt —
    not a process-death interruption — so it clears
    ``push_inflight_since`` while PRESERVING ``interrupted_count``
    (the Layer-2 escalation signal survives ordinary network
    failures; only ``record_success`` / never-getting-killed clears
    it). See ``note_interrupted_on_startup``."""
    state = _load()
    entry = dict(state.get(langcode) or {})
    try:
        failures = int(entry.get('consecutive_failures', 0) or 0) + 1
    except (TypeError, ValueError):
        failures = 1
    now = time.time()
    entry['consecutive_failures'] = failures
    entry['last_attempt_at'] = now
    entry['next_attempt_at'] = now + _curve_seconds(failures)
    entry.pop('push_inflight_since', None)   # clean finish, not a kill
    state[langcode] = entry
    _save(state)


def nudge(langcode: str) -> None:
    """User pressed sync. Clear ``next_attempt_at`` so the next
    drain pass fires immediately. Keep ``consecutive_failures``
    so a fresh failure re-enters the curve at the same step —
    one bad nudge shouldn't reset weeks of accumulated backoff
    to zero."""
    state = _load()
    if langcode not in state:
        return
    state[langcode]['next_attempt_at'] = 0.0
    _save(state)


# ── push-interruption tracking (0.52.21, Layer 2) ──────────────────
#
# Distinguishes "this push keeps getting killed mid-flight" from
# "ordinary network failure". A push that starts sets
# ``push_inflight_since``; a push that *finishes* (success OR explicit
# failure) clears it. If the daemon restarts and the marker is still
# set, the previous attempt was killed (OOM / idle-stop / APK update)
# before it could finish → ``note_interrupted_on_startup`` bumps
# ``interrupted_count``. A high ``interrupted_count`` while online is
# the Layer-3 escalation trigger (run-to-completion under FGS).


def mark_push_started(langcode: str) -> None:
    """Record that a WAN push for *langcode* has begun. Cleared by
    ``mark_push_finished`` / ``record_failure`` / ``record_success``.
    If it survives to the next daemon startup, the attempt was
    killed (see ``note_interrupted_on_startup``)."""
    state = _load()
    entry = dict(state.get(langcode) or {})
    entry['push_inflight_since'] = time.time()
    state[langcode] = entry
    _save(state)


def mark_push_finished(langcode: str) -> None:
    """Clear the in-flight marker after an attempt returns (any
    outcome). A clean return means the process was NOT killed
    mid-push, so this attempt should not count as interrupted."""
    state = _load()
    entry = state.get(langcode)
    if not isinstance(entry, dict) or 'push_inflight_since' not in entry:
        return
    entry = dict(entry)
    entry.pop('push_inflight_since', None)
    state[langcode] = entry
    _save(state)


def interrupted_count(langcode: str) -> int:
    entry = _load().get(langcode) or {}
    try:
        return int(entry.get('interrupted_count', 0) or 0)
    except (TypeError, ValueError):
        return 0


def note_interrupted_on_startup() -> list:
    """Called once per daemon startup (from
    ``scheduler.reconcile_on_startup``). Any project whose
    ``push_inflight_since`` is still set had its push killed before it
    could finish — bump ``interrupted_count`` and clear the marker.
    Returns the list of affected langcodes for logging.

    ``interrupted_count`` is only reset by ``record_success`` (which
    deletes the whole entry) — so it accumulates across the repeated
    kill/respawn cycle that characterises the stuck-diverged-push
    pathology, and drops to zero the moment the push actually lands."""
    state = _load()
    affected = []
    for langcode, entry in list(state.items()):
        if not isinstance(entry, dict) or 'push_inflight_since' not in entry:
            continue
        entry = dict(entry)
        try:
            entry['interrupted_count'] = int(
                entry.get('interrupted_count', 0) or 0) + 1
        except (TypeError, ValueError):
            entry['interrupted_count'] = 1
        entry.pop('push_inflight_since', None)
        state[langcode] = entry
        affected.append(langcode)
    if affected:
        _save(state)
    return affected


def bump_escalation_attempts(langcode: str) -> int:
    """Increment + persist the count of run-to-completion visits that
    did NOT converge, and return the new value. The scheduler uses
    this as a battery safety valve: after enough non-converging
    escalated visits it gives up escalating (``clear_interrupted``)
    and falls back to the normal backoff curve until the next user
    Sync / online edge. Reset by ``record_success`` (entry delete) or
    ``clear_interrupted``."""
    state = _load()
    entry = dict(state.get(langcode) or {})
    try:
        n = int(entry.get('escalation_attempts', 0) or 0) + 1
    except (TypeError, ValueError):
        n = 1
    entry['escalation_attempts'] = n
    state[langcode] = entry
    _save(state)
    return n


def clear_interrupted(langcode: str) -> None:
    """Drop the Layer-2/3 escalation signals for *langcode* without
    touching the backoff curve. Called when escalation should stop —
    a permanent (no-access) failure, or the escalation giveup cap.
    The project reverts to ordinary backoff-gated draining."""
    state = _load()
    entry = state.get(langcode)
    if not isinstance(entry, dict):
        return
    entry = dict(entry)
    changed = False
    for k in ('interrupted_count', 'escalation_attempts',
              'push_inflight_since'):
        if entry.pop(k, None) is not None:
            changed = True
    if changed:
        state[langcode] = entry
        _save(state)


def reset_due_times_on_startup() -> None:
    """Deprecated no-op since 0.50.45. Pre-0.50.45 this cleared
    every project's ``next_attempt_at`` so a daemon respawn gave
    a free immediate retry. The scheduler no longer calls it
    because frequent Android respawns (OOM, APK self-update,
    sticky-service restart) made the 24 h cap effectively
    unreachable. Retained as a no-op so any external caller
    importing the name doesn't break at import time. Use
    ``nudge(langcode)`` for user-intent resets; ``record_success``
    handles natural curve resets."""
    return


def snapshot() -> dict:
    """Read-only view of the full state. For diagnostics /
    project_status enrichment."""
    return _load()
