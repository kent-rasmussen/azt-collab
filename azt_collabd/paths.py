"""Shared path conventions (server side).

Resolves $AZT_HOME with platform fallbacks. Duplicated in azt_collab_client
to keep the client independent of the server package.
"""

import os
import sys


def azt_home():
    """Return the AZT server's home directory (created on first use by the
    server). Respects $AZT_HOME; falls back to platform conventions."""
    p = os.environ.get('AZT_HOME')
    if p:
        return p
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/azt')
    xdg = os.environ.get('XDG_DATA_HOME') or os.path.expanduser(
        '~/.local/share')
    return os.path.join(xdg, 'azt')


def server_info_path():
    return os.path.join(azt_home(), 'server.json')
