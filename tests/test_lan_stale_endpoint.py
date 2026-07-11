"""Regression tests for the stale-peer-address family
(agenda/lan_stale_peer_address.md, incidents 2026-07-10/11).

Shape of the bug: fan-out dialed an address from a previous network
life (hotspot ghost ``10.42.0.100:40425``) while the peer was
announcing a fresh one. Three cooperating causes, each pinned here:

- the desktop (zeroconf) discovery path never persisted resolved
  endpoints, so ``static_endpoints`` stayed frozen at pair-time
  (``_persist_resolved_endpoint`` drift semantics);
- nothing demoted a fallback endpoint that failed to connect, so
  the frozen head was re-dialed forever
  (``peers.demote_static_endpoint``);
- a daemon restart always rebound a NEW random port, invalidating
  every peer's cached endpoint at once (listener port memo).
"""

import pytest

from azt_collabd import peers as peers_mod


PID = 'a' * 64
GHOST = '10.42.0.100:40425'
FRESH = '192.168.10.23:39391'


@pytest.fixture
def paired_peer(azt_home):
    peers_mod._save_raw({'peers': {PID: {
        'device_name': 'Kent Phone',
        'fp': 'f' * 64,
        'endpoints': [GHOST],
        'static_endpoints': [GHOST, FRESH],
        'shared_projects': ['en'],
    }}})
    return PID


# ── demotion ─────────────────────────────────────────────────────────────


def test_demote_moves_failing_endpoint_to_tail(paired_peer):
    assert peers_mod.demote_static_endpoint(PID, GHOST) is True
    entry = peers_mod.get_peer(PID)
    assert entry['static_endpoints'] == [FRESH, GHOST], \
        'the fresh endpoint must become the fallback head'
    # Legacy ``endpoints`` list: GHOST was its only entry — already
    # last, so it stays put there.
    assert entry['endpoints'] == [GHOST]


def test_demote_already_last_is_noop(paired_peer):
    peers_mod.demote_static_endpoint(PID, GHOST)
    assert peers_mod.demote_static_endpoint(PID, GHOST) is False


def test_demote_unknown_endpoint_or_peer_is_noop(paired_peer):
    assert peers_mod.demote_static_endpoint(PID, '1.2.3.4:5') is False
    assert peers_mod.demote_static_endpoint('b' * 64, GHOST) is False


# ── resolution order uses the (drifting) head ────────────────────────────


def test_resolve_endpoint_prefers_mdns_then_static_head(monkeypatch,
                                                        paired_peer):
    from azt_collabd import lan_push as lp
    entry = peers_mod.get_peer(PID)

    monkeypatch.setattr(lp._lan_discovery, 'get_endpoint',
                        lambda pid: ('192.168.10.23', 39391))
    assert lp._resolve_endpoint(entry) == ('192.168.10.23', 39391)

    # mDNS miss (cache expired) → static head. Pre-fix this was the
    # hotspot ghost forever; after one demotion it's the fresh one.
    monkeypatch.setattr(lp._lan_discovery, 'get_endpoint',
                        lambda pid: None)
    assert lp._resolve_endpoint(entry) == ('10.42.0.100', 40425)
    peers_mod.demote_static_endpoint(PID, GHOST)
    entry = peers_mod.get_peer(PID)
    assert lp._resolve_endpoint(entry) == ('192.168.10.23', 39391)


# ── resolved-endpoint persistence (the desktop-parity fix) ──────────────


def test_persist_resolved_endpoint_drifts_static_head(paired_peer):
    from azt_collabd import lan_discovery as ld
    ld._persist_resolved_endpoint(PID, '192.168.10.23', 39391)
    entry = peers_mod.get_peer(PID)
    assert entry['static_endpoints'][0] == FRESH
    assert GHOST in entry['static_endpoints'], \
        'older entries are preserved (hotspot fallback), just demoted'
    # Idempotent — already at head, no reordering.
    ld._persist_resolved_endpoint(PID, '192.168.10.23', 39391)
    assert peers_mod.get_peer(PID)['static_endpoints'][0] == FRESH


def test_persist_resolved_endpoint_unpaired_is_noop(azt_home):
    from azt_collabd import lan_discovery as ld
    ld._persist_resolved_endpoint('c' * 64, '192.168.10.9', 1234)
    assert peers_mod.get_peer('c' * 64) is None


# ── negative cache vs announcements ──────────────────────────────────────


def test_announcement_clears_unreachable_gate(azt_home):
    """The phone-side variant (2026-07-11 ~17:19): the fast-fail
    gate stayed set while the peer was announcing its new port on
    mDNS, so every sweep skipped it. Any announcement from a
    gated peer must clear the gate."""
    from azt_collabd import lan_discovery as ld
    from azt_collabd import lan_push as lp
    lp._record_unreachable(PID)
    try:
        assert lp._recently_unreachable(PID) is True
        ld._clear_unreachable_on_announcement(PID)
        assert lp._recently_unreachable(PID) is False
        # No-op (and no raise) when the gate isn't set.
        ld._clear_unreachable_on_announcement(PID)
        assert lp._recently_unreachable(PID) is False
    finally:
        lp._unreachable_at.pop(PID, None)


# ── listener port memo ───────────────────────────────────────────────────


def test_listener_port_memo_roundtrip(azt_home):
    from azt_collabd import lan_listener as ll
    assert ll._read_preferred_port() == 0, 'no memo yet → OS-assigned'
    ll._write_preferred_port(45793)
    assert ll._read_preferred_port() == 45793


def test_listener_port_memo_rejects_garbage(azt_home):
    import os
    from azt_collabd import lan_listener as ll
    with open(ll._port_memo_path(), 'w') as f:
        f.write('not-a-port')
    assert ll._read_preferred_port() == 0
    with open(ll._port_memo_path(), 'w') as f:
        f.write('80')   # below the sanity floor
    assert ll._read_preferred_port() == 0
    os.unlink(ll._port_memo_path())
