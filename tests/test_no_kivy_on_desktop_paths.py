"""The desktop RPC path must be importable and runnable without Kivy
ever entering ``sys.modules``.

Why: non-UI client modules used to do function-local
``from kivy.utils import platform``. Invisible from Kivy hosts
(recorder/viewer), but the first non-Kivy consumer — desktop A-Z+T,
a tkinter app — hit it on its first RPC: ``transports._on_android``
imported Kivy, whose import-time argv parser rejected the host's own
``--restart`` flag and hard-exited the app (field repro 2026-07-07,
"Core: option --restart not recognized"). Since 0.53.1 the non-UI
platform probes go through ``azt_collab_client/_platform.py``.

Runs in a fresh subprocess (not this pytest process) because other
tests legitimately import Kivy, which would mask a leak here. The
subprocess deliberately strips KIVY_NO_ARGS etc. — the path must be
safe WITHOUT the defensive env vars a host may or may not set — and
carries a hostile argv flag so any accidental Kivy import fails
loudly rather than silently succeeding on a clean argv.
"""

import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROBE = """\
import sys
sys.argv = ['main.py', '--restart']   # hostile host flag, like azt's
import azt_collab_client
from azt_collab_client import _platform
from azt_collab_client.transports import _on_android
from azt_collab_client.lift_io import LiftHandle
assert _on_android() is False
assert _platform.platform() in ('linux', 'win', 'macosx', 'unknown')
h = LiftHandle('/tmp/nonexistent-example.lift')   # constructor only
leaked = [m for m in sys.modules if m == 'kivy' or m.startswith('kivy.')]
assert not leaked, f'Kivy leaked into the desktop path: {leaked}'
print('kivy-free ok')
"""


def test_desktop_rpc_path_is_kivy_free():
    env = dict(os.environ)
    for var in ('KIVY_NO_ARGS', 'KIVY_NO_FILELOG', 'KIVY_NO_CONSOLELOG'):
        env.pop(var, None)
    out = subprocess.check_output(
        [sys.executable, '-c', _PROBE],
        cwd=_REPO_ROOT, env=env, stderr=subprocess.STDOUT)
    assert b'kivy-free ok' in out


def test_platform_helper_android_detection(monkeypatch):
    from azt_collab_client import _platform
    monkeypatch.setenv('ANDROID_ARGUMENT', '')
    assert _platform.platform() == 'android'
    assert _platform.on_android() is True
    monkeypatch.delenv('ANDROID_ARGUMENT')
    assert _platform.on_android() is False
