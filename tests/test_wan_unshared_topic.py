"""Validates the 0.53.3 sync-count redefinition in ``azt_collabd.repo``:

- ``_origin_topic_ref_tips`` — collects ``refs/remotes/origin/azt-pending-*``.
- ``_wan_unshared`` — counts commits whose bytes are NOT on github, i.e.
  not reachable from ``origin/main`` NOR any per-device topic ref. This
  is what makes the count TICK DOWN as a chunked topic-push advances the
  topic ref, instead of staying pinned at the full divergence until the
  final merge.
- ``_main_merged`` — True only when the local tip is fully on
  ``origin/main`` (the gate for the "OK" state; a project whose bytes are
  all on a topic ref but not yet merged is "WAN-0", not "OK").
- ``_at_risk`` — excludes topic tips too (bytes on github pre-merge are
  not at risk).

The topology built by ``_chain`` is a linear history c0..c5 on
``refs/heads/main``; the tests then place ``origin/main`` and a topic ref
at various points and assert the counts.
"""

from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from azt_collabd import repo as azt_repo


_MAIN = b'refs/heads/main'
_ORIGIN_MAIN = b'refs/remotes/origin/main'
_TOPIC = b'refs/remotes/origin/azt-pending-nml-dev1'


def _chain(tmp_path, n=6, with_url=True):
    """Init a repo with a linear chain of *n* commits on
    ``refs/heads/main``. Returns ``(repo, [c0..c{n-1}])`` (SHA bytes in
    commit order). Sets a github-shaped origin URL unless *with_url* is
    False (the LAN-only case).

    Commits are built at the object-store level (not ``do_commit``,
    which isn't present in every dulwich build) so the fixture is
    version-robust and deterministic — distinct blob content per commit
    → distinct tree/commit SHAs."""
    r = Repo.init(str(tmp_path))
    if with_url:
        cfg = r.get_config()
        cfg.set((b'remote', b'origin'), b'url',
                b'https://github.com/x/y.git')
        cfg.write_to_path()
    store = r.object_store
    shas = []
    parent = None
    for i in range(n):
        blob = Blob.from_string(f'content {i}\n'.encode())
        store.add_object(blob)
        tree = Tree()
        tree.add(b'file.txt', 0o100644, blob.id)
        store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        if parent is not None:
            commit.parents = [parent]
        commit.author = commit.committer = b'T <t@example.com>'
        commit.author_time = commit.commit_time = 1_700_000_000 + i
        commit.author_timezone = commit.commit_timezone = 0
        commit.encoding = b'UTF-8'
        commit.message = f'c{i}'.encode()
        store.add_object(commit)
        parent = commit.id
        shas.append(commit.id)
    r.refs[_MAIN] = shas[-1]
    return r, shas


# ── _origin_topic_ref_tips ───────────────────────────────────────────────

def test_topic_ref_tips_collected(tmp_path):
    r, c = _chain(tmp_path)
    assert azt_repo._origin_topic_ref_tips(r) == []
    r.refs[_TOPIC] = c[3]
    assert azt_repo._origin_topic_ref_tips(r) == [c[3]]


# ── _wan_unshared counts down as the topic ref advances ──────────────────

def test_wan_unshared_pinned_without_topic(tmp_path):
    """origin/main behind, no topic ref → full divergence (c1..c5)."""
    r, c = _chain(tmp_path)          # local tip = c5
    r.refs[_ORIGIN_MAIN] = c[0]
    assert azt_repo._wan_unshared(r, 'main') == 5


def test_wan_unshared_ticks_down_with_topic(tmp_path):
    """A topic ref partway up the chain is counted as on-github, so
    only the commits above it remain unshared."""
    r, c = _chain(tmp_path)
    r.refs[_ORIGIN_MAIN] = c[0]
    r.refs[_TOPIC] = c[2]            # 3 commits uploaded to the topic ref
    assert azt_repo._wan_unshared(r, 'main') == 3   # c3, c4, c5


def test_wan_unshared_zero_when_all_uploaded_but_unmerged(tmp_path):
    """Topic ref at the local tip → nothing left to upload (WAN-0)
    even though origin/main hasn't merged yet."""
    r, c = _chain(tmp_path)
    r.refs[_ORIGIN_MAIN] = c[0]
    r.refs[_TOPIC] = c[5]
    assert azt_repo._wan_unshared(r, 'main') == 0


def test_wan_unshared_zero_when_merged(tmp_path):
    r, c = _chain(tmp_path)
    r.refs[_ORIGIN_MAIN] = c[5]
    assert azt_repo._wan_unshared(r, 'main') == 0


def test_wan_unshared_lan_only_walks_full_history(tmp_path):
    """No origin URL → LAN-only friction signal: whole history counts,
    and topic refs (if any orphaned) are ignored."""
    r, c = _chain(tmp_path, with_url=False)
    assert azt_repo._wan_unshared(r, 'main') == 6


# ── _main_merged gate ────────────────────────────────────────────────────

def test_main_merged_false_when_behind(tmp_path):
    r, c = _chain(tmp_path)
    r.refs[_ORIGIN_MAIN] = c[3]      # local c5 not contained in origin c3
    assert azt_repo._main_merged(r, 'main') is False


def test_main_merged_false_in_wan0_finishing_window(tmp_path):
    """All bytes on the topic ref but origin/main behind → NOT merged
    (this is the WAN-0 / finishing state, not OK)."""
    r, c = _chain(tmp_path)
    r.refs[_ORIGIN_MAIN] = c[0]
    r.refs[_TOPIC] = c[5]
    assert azt_repo._wan_unshared(r, 'main') == 0
    assert azt_repo._main_merged(r, 'main') is False


def test_main_merged_true_when_origin_at_tip(tmp_path):
    r, c = _chain(tmp_path)
    r.refs[_ORIGIN_MAIN] = c[5]
    assert azt_repo._main_merged(r, 'main') is True


def test_main_merged_false_when_lan_only(tmp_path):
    r, c = _chain(tmp_path, with_url=False)
    assert azt_repo._main_merged(r, 'main') is False


# ── _at_risk also excludes topic tips ────────────────────────────────────

def test_at_risk_excludes_topic_tips(tmp_path, monkeypatch):
    """With a paired peer behind at c0 and a topic ref at c3, at_risk
    counts only commits above BOTH — c4, c5."""
    r, c = _chain(tmp_path)
    r.refs[_ORIGIN_MAIN] = c[0]
    r.refs[_TOPIC] = c[3]
    from azt_collabd import peers as _peers
    monkeypatch.setattr(
        _peers, 'peer_main_shas_for',
        lambda langcode: [c[0].decode('ascii')])
    assert azt_repo._at_risk(r, 'main', 'nml') == 2
