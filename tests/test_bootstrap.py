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

# Both ``from azt_collab_client.ui import bootstrap as bs`` and
# ``import azt_collab_client.ui.bootstrap as bs`` bind ``bs`` to
# the *function*, not the module: ``ui/__init__.py`` does
# ``from .bootstrap import bootstrap``, which shadows the
# submodule attribute on the ``ui`` package, and both import
# forms ultimately resolve via attribute traversal. We need the
# module for ``patch.object(bs, '<symbol>')`` and for accessing
# the module's helpers (``_record_decline`` etc.), so reach into
# ``sys.modules`` directly — which holds the unshadowed module
# object the import system loaded before the shadow assignment
# happened.
import sys
import azt_collab_client.ui.bootstrap  # ensures the module is loaded
bs = sys.modules['azt_collab_client.ui.bootstrap']


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
    """bootstrap spawns daemon threads, and the inner self-update
    probe spawns a second daemon thread from inside the outer one.
    A single ``threading.enumerate()`` pass only sees threads alive
    at enumeration time, so the inner probe is missed if it starts
    after we snapshot. Re-enumerate after every join sweep until
    nothing new appears."""
    import time as _t
    deadline = _t.time() + 4.0
    while _t.time() < deadline:
        live = [t for t in threading.enumerate()
                if t.daemon and t is not threading.main_thread()
                and t.is_alive()]
        if not live:
            return
        for t in live:
            t.join(timeout=max(0.05, deadline - _t.time()))


@pytest.fixture(autouse=True)
def reset_bootstrap_state():
    """Each test starts with bootstrap's process-local state cleared:

    - ``_running`` (idempotence guard)
    - ``_release_cache`` in ``update.py``: ``_fetch_latest`` keeps
      a per-process cache keyed by repo with a TTL. Without this
      reset, the first test's ``_release('X.Y.Z')`` mock sticks
      around for subsequent tests regardless of how they patch
      ``urllib.request.urlopen`` — every later test sees the cached
      tag from the first one.
    - ``_last_seen_digest`` storage on disk: bootstrap records the
      GitHub asset's sha256 on tap, and ``_record_decline`` /
      ``_consume_decline`` round-trip through the same config file.
      The autouse ``azt_home`` fixture (in conftest.py) gives each
      test a fresh ``$AZT_HOME``, so the on-disk state is already
      reset — nothing extra needed here."""
    from azt_collab_client.ui import update as _upd
    bs._running = False
    _upd._release_cache.clear()
    yield
    bs._running = False
    _upd._release_cache.clear()


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
    """Declining a self-update writes the declined version to
    config.json. ``_declined_version`` is a non-destructive peek;
    it should return the recorded value without clearing it."""
    bs._record_decline('owner/repo', '2.0.0')
    assert bs._declined_version('owner/repo') == '2.0.0'
    assert bs._declined_version('owner/repo') == '2.0.0'  # still there
    assert bs._declined_version('other/repo') == ''


def test_consume_decline_one_shot_clears_after_match(android, inline_clock):
    """``_consume_decline`` is the consuming half of the decline
    contract: matches once, returns True, clears the entry. The
    next call against the same version returns False because the
    entry is gone — which is what produces the set-skip-set-skip
    cadence (decline → skip one launch → re-prompt)."""
    bs._record_decline('owner/repo', '2.0.0')
    assert bs._consume_decline('owner/repo', '2.0.0') is True
    # Consumed: stored decline is now cleared.
    assert bs._declined_version('owner/repo') == ''
    # Second consult with the same version doesn't skip — the
    # caller will prompt again.
    assert bs._consume_decline('owner/repo', '2.0.0') is False


def test_consume_decline_returns_false_when_version_mismatches(
        android, inline_clock):
    """If the stored decline is for 2.0.0 but upstream is now
    2.1.0, ``_consume_decline`` returns False (so the caller
    prompts) and leaves the stale 2.0.0 entry alone (so a rollback
    to 2.0.0 doesn't double-prompt)."""
    bs._record_decline('owner/repo', '2.0.0')
    assert bs._consume_decline('owner/repo', '2.1.0') is False
    # Stale entry retained.
    assert bs._declined_version('owner/repo') == '2.0.0'


def test_decline_skips_prompt_for_same_version(android, inline_clock):
    """If the user declined v2.0.0, the *next* probe finding 2.0.0
    must NOT re-prompt — fall through to no_update. The decline
    is consumed at this consult so the launch after this one
    would prompt again."""
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
    # And the decline was consumed by this bootstrap — the
    # next launch would prompt again.
    assert bs._declined_version('owner/repo') == ''


def test_decline_prompts_again_on_next_launch_after_skip(
        android, inline_clock):
    """The one-shot contract in action: launch 1 declines, launch
    2 silently skips (consuming the decline), launch 3 re-prompts.
    The stored decline must be gone by launch 3 so the prompt
    actually fires."""
    bs._record_decline('owner/repo', '2.0.0')

    prompt_fired = {'count': 0}

    def _fake_prompt(ctx, latest, mandatory=False, gh_digest=''):
        prompt_fired['count'] += 1

    # Launch 2: should silently skip (no prompt), consume the decline.
    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': True}), \
            patch('urllib.request.urlopen',
                  return_value=_FakeResponse([_release('2.0.0')])), \
            patch.object(bs, '_prompt_self_update',
                         side_effect=_fake_prompt):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: None)
        _drain_workers()
    assert prompt_fired['count'] == 0
    assert bs._declined_version('owner/repo') == ''

    # Launch 3: same upstream version, but the decline is gone —
    # prompt fires this time.
    with patch.object(bs, 'check_server_compat',
                      return_value={'ok': True}), \
            patch('urllib.request.urlopen',
                  return_value=_FakeResponse([_release('2.0.0')])), \
            patch.object(bs, '_prompt_self_update',
                         side_effect=_fake_prompt):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: None)
        _drain_workers()
    assert prompt_fired['count'] == 1


def test_decline_does_not_apply_when_upstream_moves(
        android, inline_clock):
    """If the user declined v2.0.0 but upstream now publishes
    v2.1.0, the prompt must re-fire (the stored decline doesn't
    apply to the new version string) and the stored 2.0.0 entry
    is left alone — clearing it would mean a rollback to 2.0.0
    re-prompts immediately."""
    bs._record_decline('owner/repo', '2.0.0')

    prompt_fired = {'v': False}

    def _fake_prompt(ctx, latest, mandatory=False, gh_digest=''):
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
    # The 2.0.0 entry must NOT have been cleared (mismatch → leave alone).
    assert bs._declined_version('owner/repo') == '2.0.0'


# ── server-package disambiguation ─────────────────────────────────────────

def test_server_unreachable_with_package_present_enters_warmup_retry(
        android, inline_clock):
    """Server APK is installed but the daemon isn't responding —
    the bootstrap should enter the warmup retry loop (the Android
    case is the ContentProvider host lazy-spawning) instead of
    prompting to install / falling through to self-update. Neither
    is useful: install would reinstall a daemon that's already
    there; self-update can't make the daemon answer.

    After all retries exhaust, ``_prompt_server_unresponsive``
    fires with actionable options (reinstall / open / quit).
    ``_check_self`` is never called on this branch — there is no
    "skip to peer update" path when the daemon is just slow.

    Pinned to: ``bootstrap.py:993-1056`` (the
    ``server_unreachable`` + ``_server_package_installed()`` is
    True arm)."""
    install_prompt_fired = {'v': False}
    self_check_called = {'v': False}
    unresponsive_prompt_fired = {'v': False}
    warmup_attempts = {'n': 0}

    def _fake_install_prompt(*_a):
        install_prompt_fired['v'] = True

    def _fake_check_self(*_a, **_kw):
        self_check_called['v'] = True

    def _fake_unresponsive(*_a):
        unresponsive_prompt_fired['v'] = True

    def _counting_compat():
        warmup_attempts['n'] += 1
        return {'ok': False, 'error': 'server_unreachable',
                'kind': 'daemon_not_ready'}

    # Drop the retry budget so the test doesn't burn the whole
    # 30-retry production budget; one retry's enough to prove the
    # warmup loop fired.
    with patch.object(bs, 'check_server_compat',
                      side_effect=_counting_compat), \
            patch.object(bs, '_server_package_installed',
                         return_value=True), \
            patch.object(bs, '_prompt_server_install',
                         side_effect=_fake_install_prompt), \
            patch.object(bs, '_check_self',
                         side_effect=_fake_check_self), \
            patch.object(bs, '_prompt_server_unresponsive',
                         side_effect=_fake_unresponsive), \
            patch.object(bs, '_show_connecting_popup',
                         side_effect=lambda *_a: None), \
            patch.object(bs, '_update_connecting_popup',
                         side_effect=lambda *_a: None), \
            patch.object(bs, '_dismiss_connecting_popup',
                         side_effect=lambda *_a: None), \
            patch.object(bs, '_DAEMON_WARMUP_RETRIES', 1):
        bs.bootstrap(
            peer_repo='owner/repo', peer_version='1.0.0',
            peer_asset_filename='peer.apk',
            on_done=lambda: None)
        _drain_workers()

    # Warmup retry fired at least once (initial probe + one retry).
    assert warmup_attempts['n'] >= 2
    # The install prompt did NOT fire — package is already there.
    assert install_prompt_fired['v'] is False
    # _check_self did NOT fire — the daemon is unreachable; falling
    # through to peer-update wouldn't help.
    assert self_check_called['v'] is False
    # After retries exhaust, the unresponsive popup is the terminal
    # state (gives the user reinstall / open / quit options).
    assert unresponsive_prompt_fired['v'] is True


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
