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
        import azt_collabd
        # Importable HERE does NOT mean importable in the child: a
        # child process inherits the environment, not this process's
        # runtime sys.path — and a non-Kivy host (desktop azt) makes
        # the package importable precisely via a runtime sys.path
        # insert (its discovery shim). Pre-0.53.5 this branch
        # returned '' ("no injection needed") and the child died with
        # ``No module named azt_collabd``. Inject the located parent
        # unconditionally instead — prepending a directory the child
        # could already import from (site-packages case) is harmless.
        mod_file = getattr(azt_collabd, '__file__', '')
        if mod_file:
            return os.path.dirname(os.path.dirname(
                os.path.abspath(mod_file)))
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
