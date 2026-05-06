"""Tests for ``azt_collab_client.ui.bootstrap``.

Covers test_plan.md §6.1 (decline path), §7.1 (idempotence), §10.4
(decline memory), §10.5 (server-package-absent vs. unreachable
disambiguation). Mocked-Auto: patches over ``check_server_compat``,
``urlopen``, jnius shims, and ``Clock.schedule_once`` so the helper's
worker thread + UI marshaling fire deterministically.
"""

import io
import json
import threading
from unittest.mock import patch

import pytest

from azt_collab_client.ui import bootstrap as bs


# ── helpers ───────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        self._buf = io.BytesIO(payload)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release(tag, asset='peer.apk', prerelease=False):
    return {
        'tag_name': tag,
        'prerelease': prerelease,
        'draft': False,
        'assets': [{
            'name': asset,
            'size': 1234,
            'browser_download_url': 'https://example/peer.apk',
        }],
    }


def _drain_workers():
    """bootstrap spawns daemon threads. Wait briefly for them so the
    test sees terminal callbacks."""
    for t in threading.enumerate():
        if t.daemon and t is not threading.main_thread():
            t.join(timeout=2.0)


@pytest.fixture(autouse=True)
def reset_running_flag():
    """Each test starts with the idempotence guard cleared."""
    bs._running = False
    yield
    bs._running = False


@pytest.fixture
def inline_clock(monkeypatch):
    """Run Clock.schedule_once callbacks synchronously so tests
    don't have to wait for the next frame."""
    monkeypatch.setattr(
        'kivy.clock.Clock.schedule_once',
        lambda fn, _t: fn(0))


# ── desktop ───────────────────────────────────────────────────────────────

def test_desktop_calls_on_done_immediately(desktop):
    """Non-android hosts: helper is a no-op; on_done fires."""
    done = []
    bs.bootstrap(
        peer_repo='owner/repo',
        peer_version='1.0.0',
        peer_asset_filename='peer.apk',
        on_done=lambda: done.append(True),
    )
    assert done == [True]


def test_desktop_does_not_set_running_flag(desktop):
    """The idempotence guard isn't set on desktop; back-to-back
    calls both reach on_done."""
    done = []
    bs.bootstrap(
        peer_repo='owner/repo', peer_version='1.0.0',
        peer_asset_filename='peer.apk',
        on_done=lambda: done.append('a'))
    bs.bootstrap(
        peer_repo='owner/repo', peer_version='1.0.0',
        peer_asset_filename='peer.apk',
        on_done=lambda: done.append('b'))
    assert done == ['a', 'b']


# ── idempotence (Android) ─────────────────────────────────────────────────

def test_second_bootstrap_call_is_suppressed(android, inline_clock):
    """test_plan.md §7.1 / §10.3: a second bootstrap() in the same
    process must be a no-op until the first finishes."""
    done = []

    def _slow_compat():
        # Simulate a slow probe by sleeping; the second call must
        # see _running=True and bail before this returns.
        import time
        time.sleep(0.05)
        return {'ok': True}

    with patch.object(bs, 'check_server_compat',
                      side_effect=_slow_compat), \
            patch('urllib.request.urlopen',
                  return_value=_FakeResponse([_release('1.0.0')])):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: done.append('first'))
        # Second call before the first thread runs the compat probe.
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: done.append('second'))
        _drain_workers()
    # Only the first call's on_done should have fired.
    assert done == ['first']


def test_running_flag_clears_after_workflow(android, inline_clock):
    """Once a workflow terminates (no_update branch), a fresh
    bootstrap() call should be admitted."""
    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': True}), \
            patch('urllib.request.urlopen',
                  return_value=_FakeResponse([_release('1.0.0')])):
        done = []
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: done.append('a'))
        _drain_workers()
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: done.append('b'))
        _drain_workers()
    assert done == ['a', 'b']


# ── decline memory ────────────────────────────────────────────────────────

def test_decline_persists_to_config(android, inline_clock):
    """test_plan.md §10.4: declining a self-update writes the
    declined version to config.json so the next launch doesn't
    re-prompt for the same release."""
    bs._record_decline('owner/repo', '2.0.0')
    assert bs._declined_version('owner/repo') == '2.0.0'
    assert bs._declined_version('other/repo') == ''


def test_decline_skips_prompt_for_same_version(android, inline_clock):
    """If the user already declined v2.0.0, a probe finding 2.0.0
    again must NOT re-prompt — fall through to no_update."""
    bs._record_decline('owner/repo', '2.0.0')

    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': True}), \
            patch('urllib.request.urlopen',
                  return_value=_FakeResponse([_release('2.0.0')])):
        done_count = {'n': 0}
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: done_count.__setitem__(
                'n', done_count['n'] + 1))
        _drain_workers()
    assert done_count['n'] == 1


def test_decline_clears_when_upstream_moves(android, inline_clock):
    """If the user declined v2.0.0 but upstream now publishes
    v2.1.0, the prompt must re-fire (the stored decline doesn't
    apply to the new version string)."""
    bs._record_decline('owner/repo', '2.0.0')

    prompt_fired = {'v': False}

    def _fake_prompt(ctx, latest):
        prompt_fired['v'] = True

    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': True}), \
            patch('urllib.request.urlopen',
                  return_value=_FakeResponse([_release('2.1.0')])), \
            patch.object(bs, '_prompt_self_update',
                         side_effect=_fake_prompt):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: None)
        _drain_workers()
    assert prompt_fired['v'] is True


# ── server-package disambiguation ─────────────────────────────────────────

def test_server_unreachable_with_package_present_skips_install_prompt(
        android, inline_clock):
    """test_plan.md §10.5: server is installed but daemon
    unreachable → don't prompt to install. Continue to self-check."""
    install_prompt_fired = {'v': False}
    self_check_called = {'v': False}

    def _fake_install_prompt(*_a):
        install_prompt_fired['v'] = True

    def _fake_check_self(*_a, **_kw):
        self_check_called['v'] = True

    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': False,
                                    'error': 'server_unreachable'}), \
            patch.object(bs, '_server_package_installed',
                         return_value=True), \
            patch.object(bs, '_prompt_server_install',
                         side_effect=_fake_install_prompt), \
            patch.object(bs, '_check_self',
                         side_effect=_fake_check_self):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: None)
        _drain_workers()

    assert install_prompt_fired['v'] is False
    assert self_check_called['v'] is True


def test_server_unreachable_with_package_absent_prompts_install(
        android, inline_clock):
    """The classic install-needed case: server APK isn't installed,
    bootstrap prompts."""
    install_prompt_fired = {'v': False}

    def _fake_install_prompt(*_a):
        install_prompt_fired['v'] = True

    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': False,
                                    'error': 'server_unreachable'}), \
            patch.object(bs, '_server_package_installed',
                         return_value=False), \
            patch.object(bs, '_prompt_server_install',
                         side_effect=_fake_install_prompt):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: None)
        _drain_workers()

    assert install_prompt_fired['v'] is True


# ── client_too_old jumps to self-update ───────────────────────────────────

def test_client_too_old_jumps_to_self_update(android, inline_clock):
    """When the daemon reports we're too old, skip the server
    prompt entirely and go straight to self-update."""
    self_check_kwargs = {}

    def _fake_check_self(ctx, **kw):
        self_check_kwargs.update(kw)

    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': False,
                                    'error': 'client_too_old',
                                    'min_required': '0.30.0'}), \
            patch.object(bs, '_check_self',
                         side_effect=_fake_check_self):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='0.27.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: None)
        _drain_workers()

    assert self_check_kwargs.get('force_prompt') is True
