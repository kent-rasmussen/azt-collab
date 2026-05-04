"""Suite-wide peer-app preferences — generic key/value store.

Same persistence model as ``recent`` and ``i18n``: backed by
``$AZT_HOME/config.json`` under a top-level ``peer_prefs`` object so
peer apps (recorder, viewer, future) converge without an explicit
coordination channel. When the recorder picks Ocean theme, the next
viewer launch lands on Ocean too.

There is no transient mode and no peer-private mirror — one store,
one source of truth (see the global "one preference, one store"
rule). If you find yourself reaching for a per-app
``prefs.json::theme``, stop: that's the value this module owns.

For the values the suite already has dedicated APIs for, prefer those
over a generic key here:

- UI language        → ``azt_collab_client.i18n``
- Last project       → ``azt_collab_client.last_project`` /
                       ``azt_collab_client.set_last_project``
- Committer name     → ``azt_collab_client.get_contributor`` /
                       ``azt_collab_client.set_contributor``

Anything else genuinely-peer-shared (theme, collapse-state UX flags,
recorder-specific selectors that a future viewer might also want)
goes through ``peer_pref`` / ``set_peer_pref``. Keys are flat strings
under the ``peer_prefs`` namespace; pick names that read cleanly
across apps (e.g. ``theme`` not ``recorder_theme``).

Public API::

    from azt_collab_client import peer_pref, set_peer_pref

    set_peer_pref('theme', 'Ocean')
    peer_pref('theme', default='Ocean')   # 'Ocean'
"""

import json
import os

from .paths import azt_home


def _config_path():
    return os.path.join(azt_home(), 'config.json')


def _load_config():
    try:
        with open(_config_path()) as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_config(d):
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def peer_pref(key, default=None):
    """Return the persisted value for *key*, or *default* if unset.
    The value type is whatever ``set_peer_pref`` last stored under
    that key; callers are responsible for type discipline."""
    return (_load_config().get('peer_prefs') or {}).get(key, default)


def set_peer_pref(key, value):
    """Persist *value* under *key* in the suite-wide store. Pass
    ``None`` to clear the key (the entry is removed entirely so a
    future ``peer_pref(key, default=...)`` falls through to the
    default)."""
    cfg = _load_config()
    bucket = cfg.setdefault('peer_prefs', {})
    if value is None:
        bucket.pop(key, None)
    else:
        bucket[key] = value
    _save_config(cfg)
