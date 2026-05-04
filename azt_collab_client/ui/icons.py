"""Shared icon assets for the suite UI.

Client-first asset model: shared-shape icons (gear, sync, share, etc.)
live under ``azt_collab_client/ui/assets/icons/`` so every sister app
that imports the client gets them for free. Peer-specific icons (the
recorder's microphone, redo, app-icon variants) stay in the peer.

Usage::

    from azt_collab_client.ui import icon_path
    register_picker_kv(font_name=FONT, gear_icon=icon_path('gear'))

    # Or use directly in a host's KV via #:set:
    register_kv(font_name=FONT, sync_icon=icon_path('sync_dark'))

The helper returns an absolute path (or ``''`` if the asset isn't
bundled). Callers that need a peer-supplied override (a recorder with
its own theming, say) pass the override path explicitly to whatever
consumer accepts one — there is no implicit cwd-based search, because
the standalone picker / settings subprocesses run with cwd outside
the host's repo and relative paths break there.
"""

import os


_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')


def icon_path(name):
    """Return absolute path to a client-bundled icon, or ``''`` if not
    bundled. ``name`` is the icon's stem (no ``.png``). Searches
    ``assets/icons/<name>.png`` first (canonical) then
    ``assets/<name>.png`` (legacy flat layout — gear.png currently
    lives there)."""
    for candidate in (
        os.path.join(_ASSETS, 'icons', name + '.png'),
        os.path.join(_ASSETS, name + '.png'),
    ):
        if os.path.isfile(candidate):
            return candidate
    return ''
