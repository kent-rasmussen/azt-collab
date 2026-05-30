"""NOTES #2 — stable device identity for slot claims.

Validates:
- ``_later_claim`` tiebreaker chain (claimed_at → peer_id →
  device_name) produces cross-peer-deterministic winners.
- ``slot_rebind`` rewrites identity atoms without changing the
  slot's existence.
- ``slot_rebind`` refuses on missing slot / bad inputs.
"""

import os

import pytest

from azt_collabd import project_kv


def _claim(peer_id='', claimed_at='', device_name=''):
    return {'peer_id': peer_id, 'claimed_at': claimed_at,
            'device_name': device_name}


def test_later_claim_by_timestamp():
    a = _claim(peer_id='aaaa', claimed_at='2026-05-29T10:00:00Z')
    b = _claim(peer_id='bbbb', claimed_at='2026-05-29T10:00:01Z')
    assert project_kv._later_claim(a, b) is b
    assert project_kv._later_claim(b, a) is b


def test_later_claim_one_missing_timestamp_picks_dated_side():
    a = _claim(peer_id='aaaa')  # no claimed_at
    b = _claim(peer_id='bbbb', claimed_at='2026-05-29T10:00:00Z')
    assert project_kv._later_claim(a, b) is b
    assert project_kv._later_claim(b, a) is b


def test_later_claim_both_missing_timestamps_returns_none():
    a = _claim(peer_id='aaaa')
    b = _claim(peer_id='bbbb')
    assert project_kv._later_claim(a, b) is None


def test_later_claim_timestamp_tie_breaks_on_peer_id():
    """Audit-#9 fold-in: two NTP-synced claims with identical
    timestamps must converge on the same winner across both
    peers' merges. The function takes (a, b) — peer A sees its
    own as ``a``, peer B sees its own as ``a``. The result MUST
    be a property of the claim itself, not of which side it
    landed on."""
    ts = '2026-05-29T10:00:00Z'
    a = _claim(peer_id='1111111111111111111111111111111111111111'
                       '111111111111111111111111',
               claimed_at=ts, device_name='Alice')
    b = _claim(peer_id='2222222222222222222222222222222222222222'
                       '222222222222222222222222',
               claimed_at=ts, device_name='Bob')
    # On peer A's merge: a is ours, b is theirs.
    winner_from_a = project_kv._later_claim(a, b)
    # On peer B's merge: b is ours, a is theirs.
    winner_from_b = project_kv._later_claim(b, a)
    # Both peers compute the SAME winner.
    assert winner_from_a is a
    assert winner_from_b is a
    # And that winner is the claim with the lexicographically
    # smaller peer_id (deterministic property of the claim).


def test_later_claim_peer_id_non_empty_beats_empty():
    """A 0.50.9+ claim with a real peer_id wins over a legacy
    claim with the same timestamp but an empty peer_id. The
    newer daemon has a stable identity to anchor the claim."""
    ts = '2026-05-29T10:00:00Z'
    legacy = _claim(peer_id='', claimed_at=ts, device_name='Alice')
    new = _claim(peer_id='a' * 64, claimed_at=ts, device_name='Bob')
    assert project_kv._later_claim(legacy, new) is new
    assert project_kv._later_claim(new, legacy) is new


def test_later_claim_both_empty_peer_id_falls_back_to_device_name():
    """Legacy-legacy collision: both claims pre-0.50.9 with empty
    peer_ids and equal timestamps. Tiebreaker is device_name
    lexicographic (next stable atom on the claim)."""
    ts = '2026-05-29T10:00:00Z'
    a = _claim(peer_id='', claimed_at=ts, device_name='Alice')
    b = _claim(peer_id='', claimed_at=ts, device_name='Bob')
    assert project_kv._later_claim(a, b) is a
    assert project_kv._later_claim(b, a) is a


def test_later_claim_all_atoms_empty_returns_a():
    """Pathological case — every identity atom blank. The merge
    must terminate (not loop), so we return ``a`` deterministically.
    Won't converge cross-peer, but this case is impossible to
    reach from any 0.50.9+ daemon."""
    ts = '2026-05-29T10:00:00Z'
    a = _claim(claimed_at=ts)
    b = _claim(claimed_at=ts)
    assert project_kv._later_claim(a, b) is a


def test_slot_rebind_rewrites_identity(tmp_path):
    """A rebind on an existing slot file rewrites peer_id +
    device_name without changing the slot itself."""
    working_dir = str(tmp_path / 'proj')
    os.makedirs(working_dir)
    old_pid = 'a' * 64
    new_pid = 'b' * 64
    # Pre-existing claim
    project_kv.slot_claim(working_dir, old_pid, 'Old Device', '7')
    parsed = project_kv._parse_slot_file(
        os.path.join(working_dir, '.azt', 'slots', '7.txt'))
    assert parsed['peer_id'] == old_pid
    # Rebind
    ok = project_kv.slot_rebind(working_dir, new_pid,
                                'New Device', '7')
    assert ok is True
    parsed = project_kv._parse_slot_file(
        os.path.join(working_dir, '.azt', 'slots', '7.txt'))
    assert parsed['peer_id'] == new_pid
    assert parsed['device_name'] == 'New Device'


def test_slot_rebind_refuses_missing_slot(tmp_path):
    working_dir = str(tmp_path / 'proj')
    os.makedirs(working_dir)
    pid = 'a' * 64
    # No claim at slot 7 yet — rebind should refuse.
    ok = project_kv.slot_rebind(working_dir, pid, 'Dev', '7')
    assert ok is False


def test_slot_rebind_refuses_bad_peer_id(tmp_path):
    working_dir = str(tmp_path / 'proj')
    os.makedirs(working_dir)
    # Pre-existing claim
    project_kv.slot_claim(working_dir, 'a' * 64, 'Dev', '7')
    # Rebind with a too-short peer_id should refuse.
    ok = project_kv.slot_rebind(working_dir, 'short', 'Dev', '7')
    assert ok is False


def test_slot_rebind_refuses_bad_slot_name(tmp_path):
    working_dir = str(tmp_path / 'proj')
    os.makedirs(working_dir)
    project_kv.slot_claim(working_dir, 'a' * 64, 'Dev', '7')
    # Path-traversal-shaped slot names are refused.
    ok = project_kv.slot_rebind(working_dir, 'b' * 64, 'Dev',
                                '../escape')
    assert ok is False


def test_slot_rebind_purges_other_slots_held_by_same_peer(tmp_path):
    """One-slot-per-peer invariant: if this peer already holds
    slot X and rebinds slot Y to itself, slot X is dropped."""
    working_dir = str(tmp_path / 'proj')
    os.makedirs(working_dir)
    old_pid = 'a' * 64
    new_pid = 'b' * 64
    # Pre-existing claim at slot 7 (will be rebound)
    project_kv.slot_claim(working_dir, old_pid, 'OldDev', '7')
    # Pre-existing claim by new_pid at slot 3 (should be displaced)
    project_kv.slot_claim(working_dir, new_pid, 'NewDev', '3')
    assert os.path.exists(
        os.path.join(working_dir, '.azt', 'slots', '3.txt'))
    # Rebind slot 7 to new_pid — should drop slot 3.
    ok = project_kv.slot_rebind(working_dir, new_pid, 'NewDev', '7')
    assert ok is True
    assert not os.path.exists(
        os.path.join(working_dir, '.azt', 'slots', '3.txt'))
    parsed = project_kv._parse_slot_file(
        os.path.join(working_dir, '.azt', 'slots', '7.txt'))
    assert parsed['peer_id'] == new_pid
