"""Regression tests for the 2026-07-10 fd-exhaustion incident
(agenda/daemon_fd_leak_emfile_hardening.md).

A dulwich ``Repo`` holds open pack/index fds until ``.close()``, and
reference cycles inside dulwich mean GC does not reliably release
them. The daemon's hot paths (the ~10 s status poll, the per-gesture
commit family, the LAN listener's per-request ``open_repository``)
opened Repos without closing them; on the karlap desktop that
exhausted the process fd table (``OSError(24)``) in under a day and
wedged the listener, the drain loop, and ``/v1/health``.

These tests pin the fixes:

- ``repo_status_summary`` closes its Repo (the dominant leak).
- ``_track_opened_repos`` closes everything ``_get_repo`` hands out,
  and the public entry points run inside such a scope.
- ``peers.list_peers(strict=True)`` raises on a transient read
  failure instead of degrading to an empty allowlist (which the LAN
  listener read as "nothing is shared with anyone").
- The loopback transport treats an HTTP-error health answer as
  alive-but-degraded (no respawn, no server.json deletion) and
  applies a cooldown after a failed spawn (no spawn storm).
"""

import os

import pytest

from dulwich import porcelain
from dulwich.repo import Repo

from azt_collabd import repo as repo_mod


@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / 'proj'
    d.mkdir()
    porcelain.init(str(d))
    (d / 'x.lift').write_text('<lift version="0.13"></lift>')
    return str(d)


@pytest.fixture
def close_counter(monkeypatch):
    """Count ``Repo.close`` calls made during the test."""
    closed = []
    orig_close = Repo.close

    def counting_close(self):
        closed.append(self)
        return orig_close(self)

    monkeypatch.setattr(Repo, 'close', counting_close)
    return closed


def test_repo_status_summary_closes_repo(project_dir, close_counter):
    out = repo_mod.repo_status_summary(project_dir)
    assert out is not None
    assert close_counter, \
        'repo_status_summary must close its Repo — this is the ' \
        '~10 s status-poll path that exhausted the fd table'


def test_track_opened_repos_closes_on_exit(project_dir, close_counter):
    with repo_mod._track_opened_repos():
        r = repo_mod._get_repo(project_dir)
        assert r is not None
        assert not close_counter, 'must stay open inside the scope'
    assert close_counter, 'scope exit must close tracked repos'


def test_track_opened_repos_nested_scopes(project_dir, close_counter):
    with repo_mod._track_opened_repos():
        outer = repo_mod._get_repo(project_dir)
        with repo_mod._track_opened_repos():
            inner = repo_mod._get_repo(project_dir)
            assert inner is not None
        assert len(close_counter) == 1, \
            'inner scope closes only its own repo'
        assert outer is not None
    assert len(close_counter) == 2


def test_get_repo_untracked_outside_scope(project_dir, close_counter):
    r = repo_mod._get_repo(project_dir)
    assert r is not None
    assert not close_counter, \
        'outside a scope the CALLER owns closing'
    r.close()


def test_commit_repo_entry_point_closes(project_dir, close_counter):
    res = repo_mod.commit_repo(project_dir, 'Test Person')
    assert res is not None
    assert close_counter, \
        'commit_repo (the debounced per-gesture path) must close ' \
        'every repo it opened'


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'),
                    reason='needs linux /proc for fd counting')
def test_status_poll_fd_count_is_bounded(project_dir):
    """Direct fd regression: 30 simulated status polls must not
    grow the process's open-fd count. Pre-fix each poll leaked at
    least one fd."""
    def _fd_count():
        return len(os.listdir('/proc/self/fd'))

    repo_mod.repo_status_summary(project_dir)   # warm caches
    before = _fd_count()
    for _ in range(30):
        repo_mod.repo_status_summary(project_dir)
    after = _fd_count()
    assert after - before <= 2, \
        f'fd count grew from {before} to {after} over 30 polls'


# ── peers.json transient-read strictness ────────────────────────────────


def test_list_peers_strict_raises_on_unreadable(azt_home):
    from azt_collabd import peers as peers_mod
    # Make peers.json unreadable-as-a-file: a directory raises
    # IsADirectoryError (an OSError) on open — same class of
    # failure as the EMFILE case.
    os.makedirs(peers_mod._peers_path(), exist_ok=True)
    assert peers_mod.list_peers() == [], \
        'non-strict callers keep the degrade-to-empty contract'
    with pytest.raises(OSError):
        peers_mod.list_peers(strict=True)


def test_list_peers_strict_missing_file_is_empty(azt_home):
    from azt_collabd import peers as peers_mod
    assert peers_mod.list_peers(strict=True) == [], \
        'missing file means "no peers", not an error'


# ── loopback transport: no spawn storm ──────────────────────────────────


def test_server_alive_treats_http_error_as_alive(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    from azt_collab_client.transports import loopback as lb

    def raising_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(
            url, 500, 'Internal Server Error', {}, io.BytesIO(b''))

    monkeypatch.setattr(urllib.request, 'urlopen', raising_urlopen)
    monkeypatch.setattr(lb.LoopbackTransport, '_pid_alive',
                        staticmethod(lambda pid: True))
    t = lb.LoopbackTransport()
    assert t._server_alive({'port': 1, 'pid': 1}) is True, \
        'a daemon that ANSWERS (even 500) is alive-but-degraded — ' \
        'respawning over it manufactures a spawn storm'


def test_spawn_cooldown_prevents_storm(monkeypatch):
    from azt_collab_client.transports import loopback as lb
    from azt_collab_client.transports import ServerUnavailable

    spawns = []
    monkeypatch.setattr(lb, '_SPAWN_WAIT', 0.05)
    monkeypatch.setattr(
        lb.subprocess, 'Popen',
        lambda *a, **k: spawns.append(a) or None)

    t = lb.LoopbackTransport()
    monkeypatch.setattr(t, '_autospawn_enabled', lambda: True)

    def unavailable():
        raise ServerUnavailable('no server.json')

    monkeypatch.setattr(t, '_read_server_info', unavailable)

    assert t._spawn_server() is False
    assert len(spawns) == 1
    # Immediately retried (the poll-every-5s shape): cooldown must
    # suppress the second Popen.
    assert t._spawn_server() is False
    assert len(spawns) == 1, \
        'second spawn within the cooldown window must be suppressed'
