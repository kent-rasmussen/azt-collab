"""Pytest fixtures shared across the suite's test modules.

Test modules import what they need; this file provides the cross-
cutting plumbing that every test relies on:

- ``$AZT_HOME`` redirected to a temp dir per test (no risk of
  clobbering the developer's real credentials.json).
- ``kivy.utils.platform`` monkey-patched so platform-gated code
  paths are reachable from CI without an emulator.
- ``jnius`` shim registered in ``sys.modules`` so imports of
  ``ui.update`` / ``ui.bootstrap`` don't fail on a non-Android host.

There is no automated test suite anywhere else in the suite; this
is the v0.28.1 establishment. Run with ``pytest tests/ -q`` from
the repo root.
"""

import os
import sys
import types

import pytest


# ── Kivy headless ─────────────────────────────────────────────────────────

# Set before any kivy import so first-time module import doesn't try
# to open a display / parse argv. Tests that exercise actual widgets
# go through Kivy's GraphicUnitTest; the popups in update.py and
# bootstrap.py are tested via Mocked-Auto dispatch (we don't render
# them), so headless flags are sufficient.
os.environ.setdefault('KIVY_NO_ARGS', '1')
os.environ.setdefault('KIVY_NO_FILELOG', '1')
os.environ.setdefault('KIVY_NO_CONSOLELOG', '1')


# ── jnius stub ────────────────────────────────────────────────────────────

# ui.update.share imports jnius unconditionally inside its install
# path, but only at call time. ui.bootstrap imports it inside
# ``_server_package_installed``. We stub the module up front so any
# import-time access (rare, but defensive) sees something. Tests
# that need to drive specific jnius behaviour patch over the stub.

class _JniusAutoclassFake:
    """Default fake that raises whatever is asked of it. Tests that
    need finer-grained control patch ``jnius.autoclass`` directly."""

    def __call__(self, fqcn):
        raise RuntimeError(
            f'jnius.autoclass({fqcn!r}) called but no fake registered. '
            f'Patch jnius.autoclass in your test if you need this.')


def _install_jnius_stub():
    if 'jnius' in sys.modules:
        return
    mod = types.ModuleType('jnius')
    mod.autoclass = _JniusAutoclassFake()
    mod.cast = lambda _t, x: x
    sys.modules['jnius'] = mod


_install_jnius_stub()


# ── $AZT_HOME redirection ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def azt_home(tmp_path, monkeypatch):
    """Point ``$AZT_HOME`` at a per-test temp dir. Autouse so every
    test gets a clean credentials/store/config without opt-in."""
    home = tmp_path / 'azt_home'
    home.mkdir()
    monkeypatch.setenv('AZT_HOME', str(home))
    return home


# ── platform monkeypatch helper ───────────────────────────────────────────

@pytest.fixture
def android(monkeypatch):
    """Tests opting into the Android code path call this fixture.
    Patches ``kivy.utils.platform`` to ``'android'``. Returns the
    monkeypatch for further per-test adjustments."""
    import kivy.utils
    monkeypatch.setattr(kivy.utils, 'platform', 'android')
    return monkeypatch


@pytest.fixture
def desktop(monkeypatch):
    """Pin ``platform`` to a non-android value (Linux) for tests that
    assert the desktop early-return."""
    import kivy.utils
    monkeypatch.setattr(kivy.utils, 'platform', 'linux')
    return monkeypatch
