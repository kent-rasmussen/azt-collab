"""``open_server_ui`` desktop launch: "ok" means a window is up.

Field 2026-10-19 (Windows): the old launcher piped the UI's stderr,
never read it, and called a spawn ok after 0.25 s. These drive
``_settings_ui.open_desktop`` with a fake UI child instead of Kivy.
"""

import os
import signal
import sys
import textwrap

import pytest

from azt_collab_client import _settings_ui
from azt_collab_client._spawn import build_spawn_env


FAKE_UI = textwrap.dedent('''
    import sys, time
    from azt_collab_client import _settings_ui
    mode = sys.argv[1]
    token = sys.argv[sys.argv.index('--ready-token') + 1]
    if mode == 'flood':
        # More than any pipe buffer (Windows: a few KB; Linux: 64 KB).
        for _ in range(600):
            sys.stderr.write('kivy log line ' + 'x' * 180 + '\\n')
        sys.stderr.flush()
        _settings_ui.mark_ready(token)
        end = time.time() + 20
        while time.time() < end:
            _settings_ui.take_raise_request()
            time.sleep(0.05)
    elif mode == 'die':
        time.sleep(1)
        print('boom: no GL context', file=sys.stderr, flush=True)
        sys.exit(3)
    elif mode == 'silent':
        time.sleep(30)
''')


@pytest.fixture
def fake_ui(tmp_path):
    script = tmp_path / 'fake_ui.py'
    script.write_text(FAKE_UI)
    pids = []

    def argv(mode):
        return [sys.executable, str(script), mode]
    yield argv, pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _launches():
    text = ''
    for p in _settings_ui.log_files():
        with open(p, encoding='utf-8', errors='replace') as f:
            text += f.read()
    return text.count(' launch: ')


def test_floods_stderr_then_ready(fake_ui):
    argv, pids = fake_ui
    r = _settings_ui.open_desktop(argv('flood'), env=build_spawn_env(),
                                  timeout=10)
    pids.append(r.get('pid'))
    assert r['ok'] is True, r
    assert r['pid']
    assert 'raised' not in r


def test_exits_nonzero_reports_spawn_exited(fake_ui):
    argv, _pids = fake_ui
    r = _settings_ui.open_desktop(argv('die'), env=build_spawn_env(),
                                  timeout=10)
    assert r['ok'] is False
    assert r['error'] == 'spawn_exited'
    assert r['returncode'] == 3
    assert 'boom: no GL context' in r['detail']
    assert os.path.isfile(r['log'])


def test_never_ready_reports_ui_not_ready(fake_ui):
    argv, pids = fake_ui
    r = _settings_ui.open_desktop(argv('silent'), env=build_spawn_env(),
                                  timeout=1.5)
    pids.append(r.get('pid'))
    assert r['ok'] is False
    assert r['error'] == 'ui_not_ready'
    assert r['log']


def test_second_call_raises_instead_of_spawning(fake_ui):
    argv, pids = fake_ui
    first = _settings_ui.open_desktop(argv('flood'), env=build_spawn_env(),
                                      timeout=10)
    pids.append(first.get('pid'))
    assert first['ok'] is True, first
    second = _settings_ui.open_desktop(argv('flood'),
                                       env=build_spawn_env(), timeout=10)
    assert second == {'ok': True, 'pid': first['pid'], 'raised': True}
    assert _launches() == 1


def test_stale_marker_does_not_block_launch(fake_ui):
    argv, pids = fake_ui
    # A marker naming a live pid that never answers raise requests
    # (pid reuse, or a hung UI): must fall through to a fresh launch.
    _settings_ui._write_json_atomic(_settings_ui.marker_path(), {
        'pid': os.getpid(), 'token': 'old', 'ready': True,
        'raised_req': ''})
    r = _settings_ui.open_desktop(argv('flood'), env=build_spawn_env(),
                                  timeout=10, raise_timeout=0.5)
    pids.append(r.get('pid'))
    assert r['ok'] is True, r
    assert 'raised' not in r
    assert not os.path.exists(_settings_ui.raise_request_path())
