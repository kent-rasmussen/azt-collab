"""Client-side copy of AZT_HOME resolution. Duplicated intentionally to
keep azt_collab_client free of any azt_collabd dependency."""

import os
import sys


def azt_home():
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
