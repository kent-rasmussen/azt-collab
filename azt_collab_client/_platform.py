"""Dependency-free platform detection (mirror of
``kivy.utils.platform``).

Why this exists (0.53.1): non-UI client modules used function-local
``from kivy.utils import platform`` to answer "am I on Android?".
That was invisible from Kivy hosts (recorder, viewer — Kivy is
already imported), but the first NON-Kivy consumer (desktop A-Z+T,
a tkinter app) hit it on its first RPC: ``transports.pick_transport``
imported Kivy, whose import-time argv parser rejected the host's own
``--restart`` flag and killed the process (`Core: option --restart
not recognized`). Importing a full UI toolkit to read an environment
variable was never the intent of hard rule #4 ("No Kivy at import
time at the package root") — this helper closes the gap for
call-time too.

The logic mirrors ``kivy/utils.py::_get_platform`` so Kivy hosts get
the identical answer they always got:

- ``ANDROID_ARGUMENT`` in the environment → ``'android'`` (set by
  every python-for-android bootstrap before Python starts)
- ``KIVY_BUILD`` set to ``android``/``ios`` → that value
- else by ``sys.platform``: ``win`` / ``macosx`` / ``linux`` /
  ``'unknown'``.

Only ``ui/`` modules (which require a Kivy host anyway) may keep
importing ``kivy.utils`` directly.
"""

import os
import sys


def platform():
    kb = os.environ.get('KIVY_BUILD', '')
    if 'ANDROID_ARGUMENT' in os.environ:
        return 'android'
    if kb in ('android', 'ios'):
        return kb
    if 'P4A_BOOTSTRAP' in os.environ:
        return 'android'
    if sys.platform in ('win32', 'cygwin'):
        return 'win'
    if sys.platform == 'darwin':
        return 'macosx'
    if sys.platform.startswith('linux'):
        return 'linux'
    if sys.platform.startswith('freebsd'):
        return 'linux'
    return 'unknown'


def on_android():
    return platform() == 'android'
