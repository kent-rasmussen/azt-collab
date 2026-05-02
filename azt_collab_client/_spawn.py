"""Spawn helpers for azt_collabd subprocess launches.

Sister apps may sit next to the canonical ``azt-collab/`` repo without
``azt_collabd`` on ``sys.path``. ``python -m azt_collabd`` then exits
with ``No module named azt_collabd`` and the caller (open_server_ui or
the loopback auto-spawn) sees a successful Popen but a dead daemon.

``_locate_azt_collabd_parent`` returns a directory to prepend to
PYTHONPATH so ``import azt_collabd`` works in the child. ``''`` means
no injection is needed (already importable, or genuinely missing).
"""

import os


def _locate_azt_collabd_parent():
    try:
        import azt_collabd  # noqa: F401
        return ''
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.dirname(here),
        os.path.join(os.path.dirname(os.path.dirname(here)), 'azt-collab'),
    )
    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, 'azt_collabd')):
            return candidate
    return ''


def build_spawn_env(extra_path=''):
    """Return an env dict for Popen with PYTHONPATH prepended if needed."""
    env = os.environ.copy()
    parent = extra_path or _locate_azt_collabd_parent()
    if parent:
        existing = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = (
            parent + (os.pathsep + existing if existing else ''))
    return env
