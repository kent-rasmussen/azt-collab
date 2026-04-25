"""
Runtime configuration backed by ``$AZT_HOME/config.json`` (separate
from azt_collabd.config which holds the static GitHub App identity).

Keys:
    sync.debounce_ms          — debounce window for request_sync (ms)
    sync.merge_retry_max      — placeholder for the merge driver step
    sync.connectivity_poll_s  — interval for the connectivity watcher (s)

Env-var overrides take precedence at startup:
    AZT_SYNC_DEBOUNCE_MS
    AZT_SYNC_MERGE_RETRY_MAX
    AZT_SYNC_CONNECTIVITY_POLL_S
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
}
_ENV_MAP = {
    'sync.debounce_ms': 'AZT_SYNC_DEBOUNCE_MS',
    'sync.merge_retry_max': 'AZT_SYNC_MERGE_RETRY_MAX',
    'sync.connectivity_poll_s': 'AZT_SYNC_CONNECTIVITY_POLL_S',
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
