"""Audit-doc 0.50.15 close-out: items #3, #5, #6.

- #3 Topic-branch orphan visibility: ``_count_foreign_topic_orphans``
  counts refs/remotes/origin/azt-pending-* whose device suffix
  isn't ours.
- #5 Connectivity-probe backoff: ``_adaptive_probe_interval``
  doubles per consecutive same-state tick up to a cap; reset
  helpers manipulate the streak.
- #6 LAN endpoint TTL: ``get_endpoint`` returns None for entries
  older than ``_ENDPOINT_TTL_S`` and drops them from the cache.

Item #4 (HEAD-detached typed log emission) is observability-only
and its emission lives in a deeply-nested LAN listener path
that's not unit-testable without a full mock receive-pack
pipeline. That branch is exercised manually via the field smoke
matrix — covered by the data-quality log line which is
greppable from `adb logcat`.
"""

import time

import pytest

from azt_collabd import scheduler
from azt_collabd import lan_discovery


# ── #3 — foreign topic-branch orphan count ────────────────────────────


def test_count_foreign_topic_orphans_empty_on_no_refs(tmp_path):
    """A repo with no azt-pending-* refs returns 0."""
    from dulwich.repo import Repo
    repo = Repo.init(str(tmp_path / 'r'), mkdir=True)
    try:
        from azt_collabd.repo import _count_foreign_topic_orphans
        assert _count_foreign_topic_orphans(repo) == 0
    finally:
        repo.close()


def test_count_foreign_topic_orphans_excludes_own_suffix(
        tmp_path, monkeypatch):
    """Refs ending in our device-name suffix don't count.
    Refs ending in another device's suffix do."""
    from dulwich.repo import Repo
    repo = Repo.init(str(tmp_path / 'r'), mkdir=True)
    try:
        from azt_collabd import store
        monkeypatch.setattr(store, 'get_device_name',
                            lambda: 'alice')
        # Need a commit to point the refs at — write an empty
        # tree + commit.
        from dulwich.objects import Tree, Commit
        tree = Tree()
        repo.object_store.add_object(tree)
        c = Commit()
        c.tree = tree.id
        c.author = c.committer = b'Test <test@test>'
        c.commit_time = c.author_time = 0
        c.commit_timezone = c.author_timezone = 0
        c.message = b'test'
        repo.object_store.add_object(c)
        # Our own ref (alice) — not counted.
        repo.refs[b'refs/remotes/origin/azt-pending-fr-alice'] = c.id
        # Foreign refs — counted.
        repo.refs[b'refs/remotes/origin/azt-pending-fr-bob'] = c.id
        repo.refs[b'refs/remotes/origin/azt-pending-fr-carol'] = c.id
        # Unrelated ref — not counted.
        repo.refs[b'refs/remotes/origin/main'] = c.id
        from azt_collabd.repo import _count_foreign_topic_orphans
        assert _count_foreign_topic_orphans(repo) == 2
    finally:
        repo.close()


def test_count_foreign_topic_orphans_handles_unset_device(
        tmp_path, monkeypatch):
    """When ``get_device_name`` returns '' the suffix becomes
    ``-unset`` and ALL existing real-device azt-pending refs are
    counted as foreign. Edge case — daemon contributor not yet
    set."""
    from dulwich.repo import Repo
    repo = Repo.init(str(tmp_path / 'r'), mkdir=True)
    try:
        from azt_collabd import store
        monkeypatch.setattr(store, 'get_device_name', lambda: '')
        from dulwich.objects import Tree, Commit
        tree = Tree()
        repo.object_store.add_object(tree)
        c = Commit()
        c.tree = tree.id
        c.author = c.committer = b'Test <test@test>'
        c.commit_time = c.author_time = 0
        c.commit_timezone = c.author_timezone = 0
        c.message = b'test'
        repo.object_store.add_object(c)
        repo.refs[b'refs/remotes/origin/azt-pending-fr-alice'] = c.id
        from azt_collabd.repo import _count_foreign_topic_orphans
        assert _count_foreign_topic_orphans(repo) == 1
    finally:
        repo.close()


# ── #5 — adaptive probe-backoff streak ────────────────────────────────


@pytest.fixture(autouse=True)
def reset_probe_state():
    """Each test starts with streak=0."""
    scheduler._probe_idle_streak = 0
    yield
    scheduler._probe_idle_streak = 0


def test_adaptive_probe_interval_at_streak_zero_returns_base():
    base = 30.0
    assert scheduler._adaptive_probe_interval(base) == 30.0


def test_adaptive_probe_interval_doubles_per_step():
    base = 30.0
    scheduler._bump_probe_backoff()
    assert scheduler._adaptive_probe_interval(base) == 60.0
    scheduler._bump_probe_backoff()
    assert scheduler._adaptive_probe_interval(base) == 120.0
    scheduler._bump_probe_backoff()
    assert scheduler._adaptive_probe_interval(base) == 240.0


def test_adaptive_probe_interval_caps_at_300s():
    base = 30.0
    # 30 * 2^4 = 480 > cap (300)
    for _ in range(10):
        scheduler._bump_probe_backoff()
    assert scheduler._adaptive_probe_interval(base) == 300.0


def test_reset_probe_backoff_drops_to_zero():
    base = 30.0
    for _ in range(5):
        scheduler._bump_probe_backoff()
    assert scheduler._adaptive_probe_interval(base) > 30.0
    scheduler._reset_probe_backoff(reason='test')
    assert scheduler._adaptive_probe_interval(base) == 30.0


def test_reset_probe_backoff_no_op_at_zero():
    """Reset at zero is a no-op (no log spam on every tick when
    the streak is already 0)."""
    scheduler._reset_probe_backoff(reason='test')
    assert scheduler._probe_idle_streak == 0


# ── #6 — LAN endpoint TTL ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_endpoint_cache():
    with lan_discovery._LOCK:
        lan_discovery._endpoints.clear()
    yield
    with lan_discovery._LOCK:
        lan_discovery._endpoints.clear()


def test_get_endpoint_returns_fresh_entry():
    with lan_discovery._LOCK:
        lan_discovery._endpoints['abc'] = (
            '10.0.0.1', 9999, time.monotonic())
    assert lan_discovery.get_endpoint('abc') == ('10.0.0.1', 9999)


def test_get_endpoint_returns_none_for_expired_entry():
    # Stamp the entry 10 min in the past (TTL is 5 min)
    with lan_discovery._LOCK:
        lan_discovery._endpoints['abc'] = (
            '10.0.0.1', 9999, time.monotonic() - 600.0)
    assert lan_discovery.get_endpoint('abc') is None


def test_get_endpoint_drops_expired_entry_from_cache():
    """An expired read should delete the entry so subsequent
    iterations don't repay the timestamp comparison."""
    with lan_discovery._LOCK:
        lan_discovery._endpoints['abc'] = (
            '10.0.0.1', 9999, time.monotonic() - 600.0)
    lan_discovery.get_endpoint('abc')
    with lan_discovery._LOCK:
        assert 'abc' not in lan_discovery._endpoints


def test_known_endpoints_filters_expired():
    now = time.monotonic()
    with lan_discovery._LOCK:
        lan_discovery._endpoints['fresh'] = ('10.0.0.1', 1, now)
        lan_discovery._endpoints['stale'] = (
            '10.0.0.2', 2, now - 600.0)
    out = lan_discovery.known_endpoints()
    assert 'fresh' in out
    assert 'stale' not in out


def test_get_endpoint_returns_none_for_unknown_peer():
    assert lan_discovery.get_endpoint('never-seen') is None
