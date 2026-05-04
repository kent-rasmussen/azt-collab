"""Suite-wide "recent" state — currently just the last opened project.

Same persistence model as ``i18n``: backed by ``$AZT_HOME/config.json``
under a top-level ``recent`` object so peer apps (recorder, viewer,
future) converge without an explicit coordination channel. When the
recorder opens project X, the next viewer launch lands on the same
project.

There is no transient mode and no peer-private mirror — one store,
one source of truth (see the global "one preference, one store"
rule). If you find yourself reaching for a per-app
``prefs.json::last_lift``, stop: that's the value this module owns.

Public API::

    from azt_collab_client import last_project, set_last_project

    set_last_project('lol-x-his30100')   # write
    last_project()                       # read; '' if unset

The langcode is the daemon's ``projects.json`` key — the same value
the picker emits in its result and that ``open_project()`` accepts.
``last_project()`` returns just the langcode; resolve it to a
``Project`` (and a current path/URI) via
``open_project(last_langcode)``.
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


def last_project() -> str:
    """Return the persisted last-opened project langcode, or ``''``
    if none is recorded."""
    return (_load_config().get('recent') or {}).get('last_langcode', '')


def set_last_project(langcode: str) -> None:
    """Record *langcode* as the suite-wide last-opened project. Pass
    ``''`` to clear (e.g. after a project is deleted)."""
    cfg = _load_config()
    cfg.setdefault('recent', {})['last_langcode'] = langcode or ''
    _save_config(cfg)
