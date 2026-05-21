"""
Runtime configuration backed by ``$AZT_HOME/config.json`` (separate
from azt_collabd.config which holds the static GitHub App identity).

Keys:
    sync.debounce_ms          — debounce window for commit_project (ms)
    sync.merge_retry_max      — placeholder for the merge driver step
    sync.connectivity_poll_s  — interval for the connectivity watcher (s)
    sync.post_online_grace_s  — wait this long after an offline→online edge
                                before the watcher drains pending pushes
                                (avoids burning the user's MB on a brief
                                tether they enabled for some other reason)
    sync.work_offline         — daemon-wide bool. When true, the watcher's
                                drain is a no-op and the user-initiated
                                Sync button returns S.WORK_OFFLINE_ENABLED
                                without attempting any push. Commits still
                                happen normally; only push is suppressed.
    sync.push_budget_s        — wall-clock cap (seconds) on the adaptive
                                push loop in repo._push_step_locked. When
                                exceeded the loop emits SYNC_GIVING_UP_
                                TRANSIENT + PUSH_FAILED and bails so the
                                project lock frees for the next sync run.
                                0 disables the cap. Default 300 (5 min);
                                bumped from "no cap" to bound the field-
                                observed 35-minute chunk-halving storm on
                                flaky-DNS networks.

Env-var overrides take precedence at startup:
    AZT_SYNC_DEBOUNCE_MS
    AZT_SYNC_MERGE_RETRY_MAX
    AZT_SYNC_CONNECTIVITY_POLL_S
    AZT_SYNC_POST_ONLINE_GRACE_S
    AZT_SYNC_WORK_OFFLINE
    AZT_SYNC_PUSH_BUDGET_S
"""

import json
import os
import threading

from .paths import azt_home


_FILENAME = 'config.json'
_DEFAULTS = {
    'sync.debounce_ms': 500,
    'sync.merge_retry_max': 3,
    'sync.connectivity_poll_s': 30,
    'sync.post_online_grace_s': 60,
    'sync.work_offline': False,
    'sync.push_budget_s': 300,
}
_ENV_MAP = {
    'sync.debounce_ms': 'AZT_SYNC_DEBOUNCE_MS',
    'sync.merge_retry_max': 'AZT_SYNC_MERGE_RETRY_MAX',
    'sync.connectivity_poll_s': 'AZT_SYNC_CONNECTIVITY_POLL_S',
    'sync.post_online_grace_s': 'AZT_SYNC_POST_ONLINE_GRACE_S',
    'sync.work_offline': 'AZT_SYNC_WORK_OFFLINE',
    'sync.push_budget_s': 'AZT_SYNC_PUSH_BUDGET_S',
}

_lock = threading.Lock()


def _path():
    return os.path.join(azt_home(), _FILENAME)


def _load_raw():
    try:
        with open(_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        print(f'[collab.settings] load failed: {ex}')
        return {}


def _save_raw(data):
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _lock:
        with open(p, 'w') as f:
            json.dump(data, f, indent=2)


def get(key, default=None):
    """Return the current value for *key*. Resolution order:
    env-var override → config.json → DEFAULTS → *default*."""
    env_name = _ENV_MAP.get(key)
    if env_name and env_name in os.environ:
        raw = os.environ[env_name]
        try:
            return _coerce(key, raw)
        except (TypeError, ValueError):
            pass
    data = _load_raw()
    if key in data:
        return _coerce(key, data[key])
    return _DEFAULTS.get(key, default)


def set_(key, value):
    """Persist a value for *key* in config.json."""
    data = _load_raw()
    data[key] = value
    _save_raw(data)


def _coerce(key, value):
    """Convert *value* to the type implied by the default."""
    default = _DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ('1', 'true', 'yes', 'on')
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


# Convenience accessors
def debounce_ms():
    return max(0, int(get('sync.debounce_ms', 500)))


def merge_retry_max():
    return max(1, min(10, int(get('sync.merge_retry_max', 3))))


def connectivity_poll_s():
    return max(5, int(get('sync.connectivity_poll_s', 30)))


def post_online_grace_s():
    return max(0, int(get('sync.post_online_grace_s', 60)))


def work_offline():
    return bool(get('sync.work_offline', False))


def push_budget_s():
    """Wall-clock cap on the adaptive push loop, in seconds. 0 disables
    the cap (preserves pre-0.43.22 behaviour where the loop only exited
    on logical-attempts cap)."""
    return max(0, int(get('sync.push_budget_s', 300)))


def min_free_mem_mb_for_merge():
    """Minimum ``MemAvailable`` (from ``/proc/meminfo``) the daemon
    will allow before starting a three-way merge. Default 200 MB —
    the LIFT XML parse + merge step itself wants ~100–150 MB peak,
    plus headroom for Python interpreter + dulwich packfile reads.
    0 disables the check (desktop, or if the user knows their RAM
    headroom and accepts OOM-kill risk).

    Surfaced as ``S.INSUFFICIENT_MEMORY_FOR_MERGE`` when the check
    refuses; the next drain cycle re-reads memory and proceeds when
    it recovers. The check is a pre-flight only — we don't track
    memory mid-merge."""
    return max(0, int(get('sync.min_free_mem_mb_for_merge', 200)))


def topic_branch_chunk_size():
    """Initial chunk_n for the Phase A topic-branch push (used when
    ``_all_commits_descend_from`` reports non-FF and the direct push
    to main can't chunk-halve effectively). Lower for slower
    networks; the helper halves adaptively on per-chunk failure
    like the existing direct-push chunking. Default 50 — sized so a
    chunk of average audio-bearing commits is well under GitHub's
    per-request timeout (typical audio commit ~200–500 KB, so 50
    commits ≈ 10–25 MB pack)."""
    return max(1, int(get('sync.topic_branch_chunk_size', 50)))


def set_work_offline(value: bool):
    """Persist the work-offline toggle. Triggering an immediate
    drain on transition OFF is the scheduler's responsibility —
    this setter just writes the bit."""
    set_('sync.work_offline', bool(value))
