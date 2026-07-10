"""``_spawn.build_spawn_env`` must inject the daemon package's parent
dir into the child's PYTHONPATH even when ``azt_collabd`` is
importable in THIS process.

Field repro (2026-07-07, desktop azt): the host makes the package
importable via a runtime ``sys.path`` insert (its discovery shim), so
the old probe ("importable → child needs nothing") passed here while
the child — which inherits only the environment — died with
``No module named azt_collabd`` on every subprocess launch (settings
UI, picker, daemon auto-spawn).
"""

import os


def test_build_spawn_env_injects_even_when_parent_importable():
    import azt_collabd
    from azt_collab_client._spawn import build_spawn_env
    expected = os.path.dirname(os.path.dirname(
        os.path.abspath(azt_collabd.__file__)))
    env = build_spawn_env()
    assert 'PYTHONPATH' in env
    assert env['PYTHONPATH'].split(os.pathsep)[0] == expected


def test_build_spawn_env_preserves_existing_pythonpath(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', '/somewhere/else')
    from azt_collab_client._spawn import build_spawn_env
    env = build_spawn_env()
    parts = env['PYTHONPATH'].split(os.pathsep)
    assert '/somewhere/else' in parts
    assert parts.index('/somewhere/else') > 0   # injected dir first
