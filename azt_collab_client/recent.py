"""Suite-wide "recent" state — currently just the last opened project.

The daemon is the single source of truth: every langcode-bound RPC
(open_project, project_status, sync, register, init, clone,
from_template, rename) auto-stamps the project as recent on the
server, and ``last_project()`` reads that stamp back. Peers therefore
need not call ``set_last_project`` from any specific load path —
touching the project at all keeps it current.

Pre-0.23 the value was written directly to ``$AZT_HOME/config.json``
by each peer process. That worked on desktop where every peer shares
$AZT_HOME, but on Android each app's sandbox holds its own
config.json so the recorder's write and the settings-UI subprocess's
read landed in different files. Routing through the daemon's
ContentProvider unifies both platforms.

Public API::

    from azt_collab_client import last_project, set_last_project

    last_project()                       # read; '' if unset / server down
    set_last_project('lol-x-his30100')   # explicit override

The langcode is the daemon's ``projects.json`` key — the same value
the picker emits in its result and that ``open_project()`` accepts.
``last_project()`` returns just the langcode; resolve it to a
``Project`` (and a current path/URI) via
``open_project(last_langcode)`` — which itself stamps the project as
recent again as a side effect.
"""

import sys

from .rpc import call, ServerUnavailable


def last_project() -> str:
    """Return the daemon-tracked last-opened project langcode, or
    ``''`` when the server is unreachable / no project has been
    touched yet."""
    # No success log here — callers poll this at high frequency
    # (the daemon UI's cache-status indicator reads it every
    # second to know which project to query), and a per-call log
    # floods the host's stderr. Error paths still log because
    # they're rare and useful.
    try:
        resp = call('GET', '/v1/recent/last_project')
    except ServerUnavailable as ex:
        print(f'[recent] last_project: ServerUnavailable: {ex}',
              file=sys.stderr, flush=True)
        return ''
    if not resp.get('ok'):
        print(f'[recent] last_project: not ok, resp={resp!r}',
              file=sys.stderr, flush=True)
        return ''
    return (resp.get('langcode', '') or '').strip()


def set_last_project(langcode: str) -> None:
    """Explicit override; pass ``''`` to clear. Most peers do not need
    to call this — every langcode-bound RPC already stamps the
    project as recent server-side via ``server._touch_project`` —
    but the wrapper exists for the rare case a peer wants to pin a
    different project than the one it last touched, or clear the
    slot after a delete."""
    try:
        call('POST', '/v1/recent/last_project',
             {'langcode': langcode or ''})
        print(f'[recent] set_last_project({langcode!r}) sent',
              file=sys.stderr, flush=True)
    except ServerUnavailable as ex:
        print(f'[recent] set_last_project({langcode!r}): '
              f'ServerUnavailable: {ex}',
              file=sys.stderr, flush=True)
