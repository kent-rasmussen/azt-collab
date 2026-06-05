"""LAN burst backoff with persistent state.

Commit-count-based backoff for the post-commit LAN burst trigger.
Pre-0.50.45 every commit fired a 30 s burst unconditionally, which
burned battery on devices doing solo work (no peer in the room to
hear the burst). 0.50.45 introduces a per-project counter of
"commits since the last successful LAN fan-out delivery for this
project"; bursts fire only when that counter hits a power of two.

Visited counts (n) and burst eligibility:

    n = 1 → burst (first commit since last success)
    n = 2 → burst
    n = 3 → skip
    n = 4 → burst
    n = 5, 6, 7 → skip
    n = 8 → burst
    ...

A lone worker doing 100 commits gets 7 bursts (1, 2, 4, 8, 16, 32,
64) instead of 100. The radio cost asymptotes toward zero.

State is persisted to ``$AZT_HOME/lan_state.json`` because the
whole point of the curve is to save power, and an in-memory
counter would reset to 0 on every daemon respawn — which on
Android can happen many times per day under memory pressure or
sticky-service restart. Lose-on-respawn would re-enable
post-restart bursts on every commit afterward, undermining the
backoff. Schema per langcode::

    {
      "commits_since_lan_success": 17,
      "last_burst_at": 1748534400.0,
      "last_success_at": 1748530000.0
    }

Daemon lifecycle is NOT a reset signal — same rule as the WAN
backoff fix in 0.50.45. Only:

- ``record_success(langcode)`` — at least one peer received the
  fan-out (deletes the entry, fresh curve).
- ``nudge(langcode)`` — user pressed Sync; resets the counter
  but preserves no failure-count (LAN doesn't distinguish
  failure shapes the way WAN does, and nudge already implies
  "force a burst next time").

…clear or override the curve.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Optional

from .paths import azt_home


def _state_path() -> str:
    return os.path.join(azt_home(), 'lan_state.json')


def _load() -> dict:
    try:
        with open(_state_path(), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        print(f'[lan-backoff] state load failed: {ex!r}',
              file=sys.stderr, flush=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save(state: dict) -> None:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        fd, tmp = tempfile.mkstemp(prefix='.lan_state.',
                                   suffix='.json',
                                   dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as ex:
        print(f'[lan-backoff] state save failed: {ex!r}',
              file=sys.stderr, flush=True)


def _is_power_of_two(n: int) -> bool:
    """Returns True for 1, 2, 4, 8, 16, … and False otherwise.
    Zero and negatives return False."""
    return n > 0 and (n & (n - 1)) == 0


def commits_since_success(langcode: str) -> int:
    """Read the current counter for *langcode*. Zero when no entry
    exists (e.g. first commit ever, or just-reset after success)."""
    if not langcode:
        return 0
    entry = _load().get(langcode) or {}
    try:
        return int(entry.get('commits_since_lan_success', 0) or 0)
    except (TypeError, ValueError):
        return 0


def record_commit(langcode: str) -> int:
    """Called after a successful local commit. Increments the
    per-project counter and returns the new value. The caller
    decides whether to burst based on ``_is_power_of_two(returned)``.

    Persisted before returning — a daemon kill mid-commit doesn't
    lose the count."""
    if not langcode:
        return 0
    state = _load()
    entry = state.get(langcode) or {}
    try:
        prior = int(entry.get('commits_since_lan_success', 0) or 0)
    except (TypeError, ValueError):
        prior = 0
    new = prior + 1
    entry['commits_since_lan_success'] = new
    if _is_power_of_two(new):
        entry['last_burst_at'] = time.time()
    state[langcode] = entry
    _save(state)
    return new


def should_burst(langcode: str) -> bool:
    """Predicate counterpart of ``record_commit`` for callers that
    want to peek without incrementing. Returns True if the next
    commit's burst would fire (i.e., ``commits_since_success + 1``
    is a power of two)."""
    return _is_power_of_two(commits_since_success(langcode) + 1)


def record_success(langcode: str) -> None:
    """Reset the curve. Called after ``fan_out`` (or sweep)
    delivered to at least one peer for this project. Mirrors
    ``wan_backoff.record_success``."""
    if not langcode:
        return
    state = _load()
    if langcode in state:
        state[langcode]['commits_since_lan_success'] = 0
        state[langcode]['last_success_at'] = time.time()
        _save(state)


def nudge(langcode: str) -> None:
    """User pressed Sync. Reset the counter so the next commit's
    burst is eligible. Different from ``record_success`` only in
    name + book-keeping (no ``last_success_at`` stamp — nothing
    actually succeeded yet). Idempotent."""
    if not langcode:
        return
    state = _load()
    entry = state.get(langcode) or {}
    if int(entry.get('commits_since_lan_success', 0) or 0) == 0:
        return
    entry['commits_since_lan_success'] = 0
    state[langcode] = entry
    _save(state)


def snapshot() -> dict:
    """Read-only view of the full state. For diagnostics /
    project_status enrichment."""
    return _load()
