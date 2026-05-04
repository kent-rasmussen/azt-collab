"""
kill_recovery.py — MOVED.

The kill-recovery integration test now lives at
``server_apk/test_install.py``: it's a test, not a sister-app
integration example, and it belongs alongside ``test_install.sh`` (the
adb-driven on-device counterpart). For a sister-app integration
example see ``examples/sister_app.py``.

This shim is left so any external invocation that already points at
``examples/kill_recovery.py`` still runs the test.
"""
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TARGET = os.path.normpath(
    os.path.join(_HERE, '..', 'server_apk', 'test_install.py'))
sys.argv[0] = _TARGET
runpy.run_path(_TARGET, run_name='__main__')
