"""Phase-3 smoke test: burst-mode LAN arming.

What we verify on desktop (no jnius, no real radio):

  - ``start_burst`` increments the ``lan_fgs`` discovery ref + sets
    an expiry timestamp.
  - ``apply_toggle`` in ``lan_listener`` reads the bump and is
    willing to bring the listener up even when
    ``lan.autodiscovery=False``. (The listener itself doesn't
    actually bind here — we stub it — but the gating logic is what
    matters.)
  - Multiple ``start_burst`` calls extend the window without
    starting parallel workers.
  - The worker disarms after the window.

The actual mDNS + radio plumbing only runs on Android; that's a
manual smoke. Here we lock down the orchestration math that goes
silently wrong if a refactor breaks it (e.g. the listener never
comes up on a nudge, or the burst leaks the ref past the
window).
"""

import time

import pytest

from azt_collabd import lan_burst
from azt_collabd import settings
from azt_collabd.android_cp import lan_fgs


@pytest.fixture(autouse=True)
def reset_state():
    # Clear lan_fgs ref counts + lan_burst worker state between
    # tests so each test starts dormant.
    with lan_fgs._LOCK:
        lan_fgs._REF['discovery'] = 0
        lan_fgs._REF['transfer'] = 0
        lan_fgs._STATE['foreground'] = False
        lan_fgs._STATE['wifi_lock'] = None
        lan_fgs._STATE['multicast_lock'] = None
    # Stop any prior burst, wait for the worker to exit.
    with lan_burst._LOCK:
        lan_burst._STATE['expires_at'] = 0.0
    t = lan_burst._STATE.get('thread')
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    with lan_burst._LOCK:
        lan_burst._STATE['thread'] = None
    yield
    # Same cleanup after the test.
    with lan_burst._LOCK:
        lan_burst._STATE['expires_at'] = 0.0
    t = lan_burst._STATE.get('thread')
    if t is not None and t.is_alive():
        t.join(timeout=2.0)


def test_start_burst_arms_discovery_ref():
    """A burst bumps the lan_fgs discovery ref to 1 immediately
    (before the worker's first tick) so ``apply_toggle`` sees the
    bump on the very next call."""
    lan_burst.start_burst(window_s=2.0)
    assert lan_fgs.snapshot()['ref_discovery'] == 1
    assert lan_burst.is_active()


def test_burst_disarms_after_window():
    """Worker disarms when the window expires."""
    lan_burst.start_burst(window_s=1.0)
    assert lan_fgs.snapshot()['ref_discovery'] == 1
    # Allow worker its 1 s window + tick slack.
    time.sleep(2.5)
    assert lan_fgs.snapshot()['ref_discovery'] == 0
    assert not lan_burst.is_active()


def test_concurrent_start_extends_without_doubling_refs():
    """Two ``start_burst`` calls inside an active window share the
    same worker — ref stays at 1, expiry extends to the latest."""
    expiry1 = lan_burst.start_burst(window_s=2.0)
    expiry2 = lan_burst.start_burst(window_s=5.0)
    assert expiry2 > expiry1
    # Single worker, single ref.
    assert lan_fgs.snapshot()['ref_discovery'] == 1


def test_shorter_window_does_not_truncate_existing_burst():
    """A second nudge with a *shorter* window must not pull the
    expiry in — the longer window wins. Otherwise a quick double-
    tap could shorten what the user intended to be a longer
    burst."""
    expiry1 = lan_burst.start_burst(window_s=5.0)
    expiry2 = lan_burst.start_burst(window_s=1.0)
    assert expiry2 == expiry1


def test_lan_listener_apply_toggle_considers_burst(monkeypatch):
    """The whole point of Phase 3: ``apply_toggle`` brings the
    listener up when a burst is armed, even with
    ``lan.autodiscovery=False``. We stub the platform-side calls
    (listener bind, FGS, locks) and assert that ``apply_toggle``
    enters the "desired=True" branch."""
    from azt_collabd import lan_listener

    monkeypatch.setattr(settings, 'lan_autodiscovery', lambda: False)

    started = {'ran': False}

    def fake_start():
        started['ran'] = True
        return ('127.0.0.1', 12345)

    monkeypatch.setattr(lan_listener, 'start', fake_start)
    monkeypatch.setattr(lan_listener, 'is_running', lambda: False)
    # Stub all the platform-side helpers so apply_toggle doesn't
    # try to touch jnius or open sockets.
    monkeypatch.setattr(lan_fgs, 'acquire_wifi_locks', lambda: None)
    monkeypatch.setattr(lan_fgs, 'start_fgs', lambda: None)
    monkeypatch.setattr(lan_fgs, 'release_wifi_locks', lambda: None)
    monkeypatch.setattr(lan_fgs, 'stop_fgs', lambda: None)
    # Stub discovery so apply_toggle doesn't try mDNS.
    from azt_collabd import lan_discovery
    monkeypatch.setattr(lan_discovery, 'start_advertise',
                        lambda *a, **kw: None)
    monkeypatch.setattr(lan_discovery, 'start_browse', lambda: None)
    # Stub peer_id.ensure and store.get_device_name so apply_toggle
    # has data to feed into start_advertise.
    from azt_collabd import peer_id as _peer_id
    from azt_collabd import store as _store
    monkeypatch.setattr(
        _peer_id, 'ensure',
        lambda: {'peer_id': 'aa' * 32, 'fp': 'bb' * 32})
    monkeypatch.setattr(_store, 'get_device_name', lambda: 'test')

    # autodiscovery=False AND no burst → apply_toggle short-circuits
    # (nothing to do, listener stays down).
    lan_listener.apply_toggle()
    assert started['ran'] is False

    # Now arm a burst — apply_toggle should bring the listener up.
    lan_burst.start_burst(window_s=2.0)
    lan_listener.apply_toggle()
    assert started['ran'] is True
