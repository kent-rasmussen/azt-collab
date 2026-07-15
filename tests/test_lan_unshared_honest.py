"""Honest LAN-unshared fallback (0.54.5).

Field catch 2026-07-11: with the phone offline and its recorded head
(``last_seen_main``) pointing at a commit the desktop never fetched,
the sync-status walkers hit ``MissingCommitError`` and returned 0 —
so the indicator said "all shared" over six pending local commits.

Fix under test: each delivery-confirmation path records
``last_covered_local`` (a commit WE hold, proven contained in the
peer); ``repo._peer_exclude_shas`` excludes the peer's main when we
hold it, falls back to the covered commit when we don't, and treats
a peer with neither as covering NOTHING — so the count degrades to
"N commits since last confirmed coverage" instead of a false 0.
"""

import pytest

from dulwich import porcelain
from dulwich.repo import Repo

from azt_collabd import peers as peers_mod
from azt_collabd import repo as repo_mod


PID = 'a' * 64
UNKNOWN = 'f' * 40   # a head we never fetched


def _seed_peer(main='', covered=''):
    entry = {
        'device_name': 'Kent Phone',
        'fp': 'f' * 64,
        'shared_projects': ['en'],
    }
    if main:
        entry['last_seen_main'] = {'en': main}
    if covered:
        entry['last_covered_local'] = {'en': covered}
    peers_mod._save_raw({'peers': {PID: entry}})


@pytest.fixture
def three_commit_repo(tmp_path):
    d = tmp_path / 'proj'
    d.mkdir()
    porcelain.init(str(d))
    shas = []
    for name in ('a.txt', 'b.txt', 'c.txt'):
        (d / name).write_text(name)
        porcelain.add(str(d), paths=[str(d / name)])
        sha = porcelain.commit(
            str(d), message=name.encode(),
            author=b'T <t@t>', committer=b'T <t@t>')
        shas.append(sha.decode('ascii')
                    if isinstance(sha, bytes) else str(sha))
    repo = Repo(str(d))
    branch = porcelain.active_branch(repo).decode('utf-8')
    yield repo, branch, shas
    repo.close()


def test_peer_at_head_counts_zero(three_commit_repo, azt_home):
    repo, branch, (c1, c2, c3) = three_commit_repo
    _seed_peer(main=c3)
    assert repo_mod._lan_unshared(repo, branch, 'en') == 0
    assert repo_mod._at_risk(repo, branch, 'en') == 0


def test_unknown_peer_head_falls_back_to_covered(three_commit_repo,
                                                 azt_home):
    """The field shape: peer's recorded head is a commit we never
    fetched, but delivery of c1 was once confirmed. Honest answer:
    the 2 commits since c1 — pre-fix this read 0 ("all shared")."""
    repo, branch, (c1, c2, c3) = three_commit_repo
    _seed_peer(main=UNKNOWN, covered=c1)
    assert repo_mod._lan_unshared(repo, branch, 'en') == 2
    assert repo_mod._at_risk(repo, branch, 'en') == 2


def test_unknown_peer_head_no_coverage_counts_everything(
        three_commit_repo, azt_home):
    """Peer exists but nothing is confirmed anywhere: every commit
    is unconfirmed — full count, not OK-on-uncertainty 0."""
    repo, branch, (c1, c2, c3) = three_commit_repo
    _seed_peer(main=UNKNOWN)
    assert repo_mod._lan_unshared(repo, branch, 'en') == 3
    assert repo_mod._at_risk(repo, branch, 'en') == 3


def test_no_paired_peers_stays_zero(three_commit_repo, azt_home):
    """The existing convention survives: nobody paired → no LAN
    destination to be behind on → 0."""
    repo, branch, _ = three_commit_repo
    assert repo_mod._lan_unshared(repo, branch, 'en') == 0
    assert repo_mod._at_risk(repo, branch, 'en') == 0


def test_covered_local_setter_and_coverage_reader(azt_home):
    _seed_peer(main=UNKNOWN)
    assert peers_mod.set_peer_covered_local(PID, 'en', 'a' * 40)
    assert peers_mod.peer_coverage_for('en') == \
        [(UNKNOWN, 'a' * 40)]
    # Unknown peer / empty args are no-ops.
    assert not peers_mod.set_peer_covered_local('b' * 64, 'en', 'x')
    assert not peers_mod.set_peer_covered_local(PID, '', 'x')
    assert not peers_mod.set_peer_covered_local(PID, 'en', '')
    # Normalization keeps the field across unrelated writes.
    peers_mod.set_peer_last_seen_main(PID, 'en', 'c' * 40)
    assert peers_mod.peer_coverage_for('en') == \
        [('c' * 40, 'a' * 40)]
