"""Tests for the daemon-owned CAWL cache — index + image binaries —
and the per-project image_repo wire shape.

What's covered:

- ``cawl.get_index(repo)`` fetches from GitHub on cold cache and
  writes to ``$AZT_HOME/cawl/<owner>/<repo>/index.json``.
- A fresh cache (within TTL) is served without hitting the network.
- A stale cache (past TTL) triggers a refresh.
- A refresh failure with a cached copy on disk returns that copy
  (stale-cache fallback).
- A refresh failure with no cache returns ``{}``.
- Empty ``repo`` short-circuits to ``{}`` without any network call.
- Concurrent ``get_index`` for the same repo coalesce to one fetch.
- Two repos can fetch in parallel — the lock is per-cache-file,
  not module-wide.
- ``cawl.get_image_path(repo, basename)`` cold-fetches a binary,
  writes to ``<repo-dir>/images/<basename>``, returns the path.
- Image cache hit returns the path without re-fetching.
- Image fetch failure with no cache → None.
- Image fetch failure WITH cache → returns existing cached path
  (i.e. doesn't blow up on the cached file).
- Path-traversal basenames are rejected.
- ``resolve_image_repo`` prefers per-project field, falls back to
  daemon-global config.
- Two projects sharing a repo share one cache directory (the
  whole point of repo-scoped caching).
- ``Project.cawl_image_repo`` round-trips through
  ``register`` / ``set_cawl_image_repo`` / ``get``.
- Endpoint ``GET /v1/projects/<lang>/cawl/index`` resolves the
  project's repo and serves the cached/fetched dict.
- HTTP-handler binary route at
  ``GET /v1/projects/<lang>/cawl/images/<basename>`` is recognised
  by ``_match_cawl_image_path``.
- ContentProvider ``_resolve_path`` resolves CAWL routes to the
  shared cache; rejects write modes.
- Status code mirror drift (none for this release — additive
  endpoints only — but checking via the dispatch coverage).
"""

import json
import os
import threading
import time
import urllib.error

import pytest

from azt_collabd import cawl
from azt_collabd import config as _config
from azt_collabd import projects as projects_mod
from azt_collabd import server as srv
from azt_collabd.android_cp import service as cp_service


# ── Helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_cawl_state(monkeypatch):
    """Pin a known global default repo and clear any module-level
    fetch-lock state from a previous test."""
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo='kent/images'))
    # The cawl module has a module-level fetch-lock registry; new
    # cache files per test_dir mean new lock entries, no conflict —
    # but reset for cleanliness so a wedged lock from a buggy test
    # can't cross-contaminate.
    monkeypatch.setattr(cawl, '_fetch_locks', {})
    yield


def _stub_tree_response(paths):
    return json.dumps({
        'tree': [{'path': p, 'type': 'blob'} for p in paths],
    }).encode('utf-8')


class _FakeUrlopen:
    """Drop-in for urllib.request.urlopen. Records call count and
    can serve different responses per URL via ``per_url``."""

    def __init__(self, body=None, exc=None, per_url=None):
        self._body = body
        self._exc = exc
        self._per_url = per_url or {}
        self.calls = 0
        self.url_history = []

    def __call__(self, req, timeout=None):
        self.calls += 1
        url = getattr(req, 'full_url', None) or str(req)
        self.url_history.append(url)
        if url in self._per_url:
            entry = self._per_url[url]
            if isinstance(entry, Exception):
                raise entry
            return _FakeResp(entry)
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._body)


class _FakeResp:
    def __init__(self, body):
        self._body = body or b''

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


# ── Project record carries cawl_image_repo ───────────────────────────────


def test_project_register_round_trips_cawl_image_repo():
    p = projects_mod.register(
        'sw-x-kent', '/tmp/swproj',
        cawl_image_repo='someone/cawl-fork')
    assert p.cawl_image_repo == 'someone/cawl-fork'
    # Reload from disk and the value persists.
    again = projects_mod.get('sw-x-kent')
    assert again is not None
    assert again.cawl_image_repo == 'someone/cawl-fork'


def test_project_register_none_preserves_existing_cawl_image_repo():
    """Re-registering without passing cawl_image_repo (or passing
    None) preserves the previously-set value. Empty string clears."""
    projects_mod.register(
        'sw', '/tmp/sw', cawl_image_repo='a/b')
    # No-cawl re-register preserves.
    projects_mod.register('sw', '/tmp/sw2')
    p = projects_mod.get('sw')
    assert p.cawl_image_repo == 'a/b'
    # Explicit empty string clears.
    projects_mod.register('sw', '/tmp/sw2', cawl_image_repo='')
    p = projects_mod.get('sw')
    assert p.cawl_image_repo == ''


def test_set_cawl_image_repo_updates_existing_project():
    projects_mod.register('en', '/tmp/en')
    projects_mod.set_cawl_image_repo('en', 'kent/special-cawl')
    p = projects_mod.get('en')
    assert p.cawl_image_repo == 'kent/special-cawl'


def test_set_cawl_image_repo_on_missing_project_is_silent():
    """Setter on an unregistered langcode is a no-op (and doesn't
    raise). The handler-side guard is what surfaces the 404 — the
    storage layer just persists nothing."""
    projects_mod.set_cawl_image_repo('nonexistent', 'x/y')
    assert projects_mod.get('nonexistent') is None


# ── resolve_image_repo precedence ────────────────────────────────────────


def test_resolve_image_repo_prefers_project_field():
    projects_mod.register('sw', '/tmp/swp', cawl_image_repo='per-proj/repo')
    assert cawl.resolve_image_repo('sw') == 'per-proj/repo'


def test_resolve_image_repo_falls_back_to_global_when_project_empty(monkeypatch):
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo='global/repo'))
    projects_mod.register('sw', '/tmp/swp')   # no cawl_image_repo
    assert cawl.resolve_image_repo('sw') == 'global/repo'


def test_resolve_image_repo_empty_when_both_unset(monkeypatch):
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo=''))
    projects_mod.register('sw', '/tmp/swp')
    assert cawl.resolve_image_repo('sw') == ''


def test_resolve_image_repo_unknown_langcode_falls_to_global(monkeypatch):
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo='global/repo'))
    # ``resolve_image_repo`` on an unknown langcode shouldn't crash —
    # it just doesn't find a per-project value and falls through.
    assert cawl.resolve_image_repo('mystery') == 'global/repo'


# ── cawl.get_index(repo) ─────────────────────────────────────────────────


def test_get_index_returns_empty_when_repo_unset(monkeypatch):
    fake = _FakeUrlopen(body=b'should-not-be-called')
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    assert cawl.get_index('') == {}
    assert fake.calls == 0


def test_get_index_cold_fetch_writes_per_repo_cache(monkeypatch):
    body = _stub_tree_response(['cawl-1.jpg', 'cawl-2.png'])
    fake = _FakeUrlopen(body=body)
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    result = cawl.get_index('kent/images')
    assert fake.calls == 1
    assert result['repo'] == 'kent/images'
    paths = sorted(f['path'] for f in result['files'])
    assert paths == ['cawl-1.jpg', 'cawl-2.png']
    # Cache path is now per-repo.
    cached_path = cawl.index_path('kent/images')
    assert cached_path.endswith(
        os.path.join('cawl', 'kent', 'images', 'index.json'))
    on_disk = json.load(open(cached_path))
    assert on_disk == result


def test_get_index_fresh_cache_served_without_network(monkeypatch):
    body = _stub_tree_response(['cawl-1.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', _FakeUrlopen(body=body))
    first = cawl.get_index('kent/images')
    # Second call: assert no network.
    fake2 = _FakeUrlopen(exc=AssertionError('should not be called'))
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake2)
    second = cawl.get_index('kent/images')
    assert second == first
    assert fake2.calls == 0


def test_get_index_stale_cache_refreshes(monkeypatch):
    os.makedirs(cawl._repo_cache_dir('kent/images'), exist_ok=True)
    stale = {
        'repo': 'kent/images', 'branch': 'HEAD',
        'fetched_at': int(time.time()) - (cawl._INDEX_TTL_SECONDS + 60),
        'files': [{'path': 'cawl-old.jpg', 'url': 'https://example/'}],
    }
    with open(cawl.index_path('kent/images'), 'w') as f:
        json.dump(stale, f)
    body = _stub_tree_response(['cawl-new.jpg'])
    fake = _FakeUrlopen(body=body)
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    result = cawl.get_index('kent/images')
    assert fake.calls == 1
    assert [f['path'] for f in result['files']] == ['cawl-new.jpg']


def test_get_index_refresh_failure_returns_stale_cache(monkeypatch):
    os.makedirs(cawl._repo_cache_dir('kent/images'), exist_ok=True)
    stale = {
        'repo': 'kent/images', 'branch': 'HEAD',
        'fetched_at': int(time.time()) - (cawl._INDEX_TTL_SECONDS + 60),
        'files': [{'path': 'cawl-old.jpg', 'url': 'https://example/'}],
    }
    with open(cawl.index_path('kent/images'), 'w') as f:
        json.dump(stale, f)
    monkeypatch.setattr(
        cawl.urllib.request, 'urlopen',
        _FakeUrlopen(exc=urllib.error.URLError('rate limit exceeded')))
    result = cawl.get_index('kent/images')
    assert result == stale


def test_get_index_refresh_failure_no_cache_returns_empty(monkeypatch):
    monkeypatch.setattr(
        cawl.urllib.request, 'urlopen',
        _FakeUrlopen(exc=urllib.error.URLError('DNS')))
    assert cawl.get_index('kent/images') == {}


def test_get_index_per_repo_caches_dont_collide(monkeypatch):
    """Two repos populate separate cache subdirectories and don't
    trample each other."""
    body_a = _stub_tree_response(['cawl-A.jpg'])
    body_b = _stub_tree_response(['cawl-B.jpg'])
    per_url = {
        'https://api.github.com/repos/foo/imgs/git/trees/HEAD?recursive=1':
            body_a,
        'https://api.github.com/repos/bar/imgs/git/trees/HEAD?recursive=1':
            body_b,
    }
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(per_url=per_url))
    a = cawl.get_index('foo/imgs')
    b = cawl.get_index('bar/imgs')
    assert [f['path'] for f in a['files']] == ['cawl-A.jpg']
    assert [f['path'] for f in b['files']] == ['cawl-B.jpg']
    assert os.path.isfile(cawl.index_path('foo/imgs'))
    assert os.path.isfile(cawl.index_path('bar/imgs'))


def test_get_index_concurrent_same_repo_coalesces(monkeypatch):
    body = _stub_tree_response(['cawl-1.jpg'])

    class _SlowUrlopen:
        def __init__(self):
            self.calls = 0
            self._mu = threading.Lock()
        def __call__(self, req, timeout=None):
            with self._mu:
                self.calls += 1
            time.sleep(0.05)
            return _FakeResp(body)

    fake = _SlowUrlopen()
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    results = [None, None]
    def _go(i):
        results[i] = cawl.get_index('kent/images')
    threads = [threading.Thread(target=_go, args=(i,)) for i in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert fake.calls == 1
    assert results[0] == results[1] is not None


# ── cawl.get_image_path(repo, basename) ─────────────────────────────────


def test_get_image_path_cold_fetch_writes_cache(monkeypatch):
    payload = b'\x89PNG\r\n\x1a\nfake-png-bytes'
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=payload))
    target = cawl.get_image_path('kent/images', 'cawl-1234.png')
    assert target is not None
    assert target.endswith(os.path.join(
        'cawl', 'kent', 'images', 'images', 'cawl-1234.png'))
    assert open(target, 'rb').read() == payload


def test_get_image_path_cache_hit_no_network(monkeypatch):
    target = cawl.image_path('kent/images', 'cached.jpg')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, 'wb').write(b'already-here')
    fake = _FakeUrlopen(exc=AssertionError('should not fetch'))
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    result = cawl.get_image_path('kent/images', 'cached.jpg')
    assert result == target
    assert fake.calls == 0


def test_get_image_path_fetch_failure_returns_none(monkeypatch):
    monkeypatch.setattr(
        cawl.urllib.request, 'urlopen',
        _FakeUrlopen(exc=urllib.error.URLError('no route')))
    assert cawl.get_image_path('kent/images', 'cawl-1.jpg') is None


def test_get_image_path_rejects_path_traversal():
    for bad in ('../etc/passwd', '..', '.', '/abs/path',
                'subdir/file.jpg', '\\windows'):
        assert cawl.get_image_path('kent/images', bad) is None


def test_get_image_path_empty_basename_rejected():
    assert cawl.get_image_path('kent/images', '') is None
    assert cawl.get_image_path('kent/images', None) is None


def test_get_image_path_empty_repo_returns_none(monkeypatch):
    fake = _FakeUrlopen(body=b'unused')
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    assert cawl.get_image_path('', 'cawl-1.jpg') is None
    assert fake.calls == 0


# ── Index endpoint via dispatcher ────────────────────────────────────────


def _register_project(langcode='sw-x-test', repo='kent/images',
                      tmpdir=None):
    if tmpdir is None:
        tmpdir = '/tmp/' + langcode
    os.makedirs(tmpdir, exist_ok=True)
    return projects_mod.register(
        langcode, tmpdir, cawl_image_repo=repo)


def test_endpoint_index_returns_dict_for_known_project(monkeypatch):
    _register_project()
    body = _stub_tree_response(['cawl-1.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=body))
    status, resp = srv.dispatch(
        'GET', '/v1/projects/sw-x-test/cawl/index', None)
    assert status == 200
    assert resp['ok'] is True
    assert resp['image_repo'] == 'kent/images'
    assert [f['path'] for f in resp['index']['files']] == ['cawl-1.jpg']


def test_endpoint_index_returns_404_for_unknown_project(monkeypatch):
    fake = _FakeUrlopen(body=b'unused')
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    status, resp = srv.dispatch(
        'GET', '/v1/projects/mystery/cawl/index', None)
    assert status == 404
    assert resp['ok'] is False
    assert resp['error'] == 'project_not_found'
    assert fake.calls == 0


def test_endpoint_index_uses_per_project_repo_over_global(monkeypatch):
    """The endpoint resolves the project's own image_repo, not
    the daemon-global default."""
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo='global/repo'))
    _register_project(repo='proj/repo')
    body_proj = _stub_tree_response(['cawl-proj.jpg'])
    body_global = _stub_tree_response(['cawl-global.jpg'])
    per_url = {
        'https://api.github.com/repos/proj/repo/git/trees/HEAD?recursive=1':
            body_proj,
        'https://api.github.com/repos/global/repo/git/trees/HEAD?recursive=1':
            body_global,
    }
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(per_url=per_url))
    status, resp = srv.dispatch(
        'GET', '/v1/projects/sw-x-test/cawl/index', None)
    assert status == 200
    assert resp['image_repo'] == 'proj/repo'
    assert [f['path'] for f in resp['index']['files']] == ['cawl-proj.jpg']


def test_endpoint_index_falls_back_to_global_when_project_empty(monkeypatch):
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo='global/repo'))
    _register_project(repo='')  # explicit empty
    body = _stub_tree_response(['cawl-g.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=body))
    status, resp = srv.dispatch(
        'GET', '/v1/projects/sw-x-test/cawl/index', None)
    assert status == 200
    assert resp['image_repo'] == 'global/repo'


# ── Setter endpoint ──────────────────────────────────────────────────────


def test_endpoint_set_cawl_image_repo_persists(monkeypatch):
    _register_project(repo='')
    status, resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/cawl_image_repo',
        {'cawl_image_repo': 'new/repo'})
    assert status == 200
    assert resp['ok'] is True
    assert resp['project']['cawl_image_repo'] == 'new/repo'
    p = projects_mod.get('sw-x-test')
    assert p.cawl_image_repo == 'new/repo'


def test_endpoint_set_cawl_image_repo_empty_clears(monkeypatch):
    _register_project(repo='kent/images')
    status, resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/cawl_image_repo',
        {'cawl_image_repo': ''})
    assert status == 200
    assert projects_mod.get('sw-x-test').cawl_image_repo == ''


def test_endpoint_set_cawl_image_repo_unknown_project_returns_404():
    status, resp = srv.dispatch(
        'POST', '/v1/projects/mystery/cawl_image_repo',
        {'cawl_image_repo': 'x/y'})
    assert status == 404
    assert resp['error'] == 'project_not_found'


def test_endpoint_set_cawl_image_repo_missing_field():
    _register_project()
    status, resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/cawl_image_repo', {})
    assert status == 400
    assert resp['error'] == 'missing_cawl_image_repo'


# ── Binary endpoint path matcher ────────────────────────────────────────


def test_match_cawl_image_path_accepts_well_formed():
    assert srv._match_cawl_image_path(
        '/v1/projects/sw-x-test/cawl/images/cawl-1234.jpg') \
        == ('sw-x-test', 'cawl-1234.jpg')


def test_match_cawl_image_path_rejects_non_cawl():
    assert srv._match_cawl_image_path(
        '/v1/projects/sw-x-test/sync') is None
    assert srv._match_cawl_image_path(
        '/v1/projects/sw-x-test/audio/foo.wav') is None


def test_match_cawl_image_path_rejects_missing_basename():
    assert srv._match_cawl_image_path(
        '/v1/projects/sw-x-test/cawl/images/') is None


def test_match_cawl_image_path_rejects_wrong_prefix():
    assert srv._match_cawl_image_path(
        '/v2/projects/sw-x-test/cawl/images/x.jpg') is None


# ── Image binary handler ────────────────────────────────────────────────


def test_h_cawl_image_bytes_returns_payload(monkeypatch):
    _register_project()
    payload = b'fake-png-content'
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=payload))
    status, ctype, data = srv._h_cawl_image_bytes(
        'sw-x-test', 'cawl-1.png')
    assert status == 200
    assert ctype == 'image/png'
    assert data == payload


def test_h_cawl_image_bytes_404_on_unknown_project():
    status, ctype, data = srv._h_cawl_image_bytes(
        'mystery', 'cawl-1.png')
    assert status == 404
    assert ctype == 'application/json'
    assert b'project_not_found' in data


def test_h_cawl_image_bytes_404_when_no_repo_configured(monkeypatch):
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo=''))
    _register_project(repo='')
    status, ctype, _data = srv._h_cawl_image_bytes(
        'sw-x-test', 'cawl-1.png')
    assert status == 404


def test_h_cawl_image_bytes_404_on_fetch_failure(monkeypatch):
    _register_project()
    monkeypatch.setattr(
        cawl.urllib.request, 'urlopen',
        _FakeUrlopen(exc=urllib.error.URLError('rate limit')))
    status, _ctype, _data = srv._h_cawl_image_bytes(
        'sw-x-test', 'cawl-1.png')
    assert status == 404


def test_content_type_for_extensions():
    assert srv._content_type_for('foo.jpg') == 'image/jpeg'
    assert srv._content_type_for('foo.jpeg') == 'image/jpeg'
    assert srv._content_type_for('foo.png') == 'image/png'
    assert srv._content_type_for('foo.gif') == 'image/gif'
    assert srv._content_type_for('foo.webp') == 'image/webp'
    assert srv._content_type_for('foo.unknown') == 'application/octet-stream'


# ── ContentProvider _resolve_path for CAWL routes ───────────────────────


def test_resolve_path_cawl_index_returns_cached_path(monkeypatch):
    _register_project()
    body = _stub_tree_response(['cawl-1.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=body))
    target = cp_service._resolve_path('/sw-x-test/cawl/index.json', 'r')
    assert target is not None
    assert os.path.isfile(target)
    assert target.endswith(
        os.path.join('cawl', 'kent', 'images', 'index.json'))


def test_resolve_path_cawl_image_returns_cached_path(monkeypatch):
    _register_project()
    payload = b'\x89PNG-bytes'
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=payload))
    target = cp_service._resolve_path(
        '/sw-x-test/cawl/images/cawl-1.png', 'r')
    assert target is not None
    assert open(target, 'rb').read() == payload


def test_resolve_path_cawl_rejects_write_mode(monkeypatch):
    _register_project()
    body = _stub_tree_response(['cawl-1.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=body))
    # 'w' / 'a' should be rejected — peers don't write CAWL files.
    assert cp_service._resolve_path(
        '/sw-x-test/cawl/index.json', 'w') is None
    assert cp_service._resolve_path(
        '/sw-x-test/cawl/images/cawl-1.png', 'w') is None


def test_resolve_path_cawl_unknown_project_returns_none():
    assert cp_service._resolve_path(
        '/mystery/cawl/index.json', 'r') is None
    assert cp_service._resolve_path(
        '/mystery/cawl/images/cawl-1.png', 'r') is None


def test_resolve_path_cawl_no_repo_configured_returns_none(monkeypatch):
    monkeypatch.setattr(_config, '_cfg',
                        dict(_config._cfg, cawl_image_repo=''))
    _register_project(repo='')
    assert cp_service._resolve_path(
        '/sw-x-test/cawl/index.json', 'r') is None


def test_resolve_path_cawl_rejects_basename_traversal(monkeypatch):
    _register_project()
    body = _stub_tree_response(['cawl-1.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=body))
    # ``..`` segments rejected by the generic empty/dot guard.
    assert cp_service._resolve_path(
        '/sw-x-test/cawl/../images/x.png', 'r') is None
    # ``cawl/images/<basename>`` with a traversing basename:
    # the cawl branch processes ``rest=['images', '../etc/passwd']``;
    # get_image_path rejects unsafe basenames.
    assert cp_service._resolve_path(
        '/sw-x-test/cawl/images/sub/file.png', 'r') is None


# ── Cross-project sharing of one repo's cache ───────────────────────────


def test_two_projects_share_one_cache_dir(monkeypatch):
    """Two projects pointing at the same image_repo populate (and
    read from) the same on-disk cache directory."""
    _register_project(langcode='sw', repo='shared/cawl', tmpdir='/tmp/sw1')
    _register_project(langcode='en', repo='shared/cawl', tmpdir='/tmp/en1')
    body = _stub_tree_response(['cawl-share.jpg'])
    fake = _FakeUrlopen(body=body)
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    # First project's load fetches.
    status, resp_sw = srv.dispatch(
        'GET', '/v1/projects/sw/cawl/index', None)
    assert status == 200
    assert fake.calls == 1
    # Second project's load reads from the same cache — no second
    # fetch.
    status, resp_en = srv.dispatch(
        'GET', '/v1/projects/en/cawl/index', None)
    assert status == 200
    assert fake.calls == 1
    assert resp_sw['index'] == resp_en['index']


# ── Bundled seed (install-day-no-network) ───────────────────────────────


def _install_bundled_seed(monkeypatch, repo, payload):
    """Make ``importlib.resources.files('azt_collabd').joinpath(
    'data', 'cawl', <owner>, <repo>, 'index.json')`` return
    ``payload`` (bytes). Patches the seed loader directly so we
    don't have to write into the real package tree just to drive
    a test."""
    encoded = (payload if isinstance(payload, bytes)
               else json.dumps(payload).encode('utf-8'))

    class _FakeTraversable:
        def __init__(self, segments):
            self._segments = segments
        def joinpath(self, *parts):
            return _FakeTraversable(self._segments + list(parts))
        def read_bytes(self):
            owner, _, name = repo.partition('/')
            expected = ['data', 'cawl', owner, name, 'index.json']
            if self._segments == expected:
                return encoded
            raise FileNotFoundError(
                f'no fake seed at segments={self._segments!r}')

    def _fake_files(pkg):
        assert pkg == 'azt_collabd'
        return _FakeTraversable([])

    import importlib.resources
    monkeypatch.setattr(
        importlib.resources, 'files', _fake_files)


def test_seed_populates_empty_cache_without_network(monkeypatch):
    """Cold cache + bundled seed exists for this repo → seed
    contents land in the cache and no network fetch happens."""
    seed_payload = {
        'repo': 'kent/images',
        'branch': 'HEAD',
        'fetched_at': int(time.time()),  # fresh; no refresh needed
        'files': [{'path': 'cawl-seed.jpg',
                   'url': 'https://example/cawl-seed.jpg'}],
    }
    _install_bundled_seed(monkeypatch, 'kent/images', seed_payload)
    fake = _FakeUrlopen(exc=AssertionError(
        'should not fetch when seed is fresh'))
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    result = cawl.get_index('kent/images')
    assert fake.calls == 0
    assert [f['path'] for f in result['files']] == ['cawl-seed.jpg']
    # Cache file is now on disk.
    assert os.path.isfile(cawl.index_path('kent/images'))


def test_seed_does_not_trample_existing_cache(monkeypatch):
    """If a cache file already exists for this repo, the seed must
    NOT overwrite it — the on-disk copy is whatever the daemon
    last fetched (or wrote), which is fresher than the build-time
    seed."""
    # Pre-populate cache.
    os.makedirs(cawl._repo_cache_dir('kent/images'), exist_ok=True)
    fresh = {
        'repo': 'kent/images', 'branch': 'HEAD',
        'fetched_at': int(time.time()),
        'files': [{'path': 'cawl-cached.jpg',
                   'url': 'https://example/cached.jpg'}],
    }
    with open(cawl.index_path('kent/images'), 'w') as f:
        json.dump(fresh, f)
    # Different seed content; should NOT win.
    seed_payload = {
        'repo': 'kent/images', 'branch': 'HEAD',
        'fetched_at': int(time.time()) - 1_000_000,
        'files': [{'path': 'cawl-seed.jpg',
                   'url': 'https://example/seed.jpg'}],
    }
    _install_bundled_seed(monkeypatch, 'kent/images', seed_payload)
    monkeypatch.setattr(
        cawl.urllib.request, 'urlopen',
        _FakeUrlopen(exc=AssertionError('should not fetch')))
    result = cawl.get_index('kent/images')
    assert [f['path'] for f in result['files']] == ['cawl-cached.jpg']


def test_seed_no_op_when_no_bundle_for_repo(monkeypatch):
    """No bundled seed → seed step is a silent no-op; the normal
    network-fetch path takes over. (Most repos won't have a seed
    — only the suite-canonical CAWL repo is typically bundled.)"""
    # The fake seed loader raises FileNotFoundError for any path,
    # simulating "no asset exists" for this repo.
    class _NoSeed:
        def joinpath(self, *_parts):
            return self
        def read_bytes(self):
            raise FileNotFoundError('no bundled seed')

    import importlib.resources
    monkeypatch.setattr(importlib.resources, 'files',
                        lambda pkg: _NoSeed())
    body = _stub_tree_response(['cawl-net.jpg'])
    fake = _FakeUrlopen(body=body)
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    result = cawl.get_index('forks/different-repo')
    assert fake.calls == 1
    assert [f['path'] for f in result['files']] == ['cawl-net.jpg']


def test_seed_falls_through_to_network_when_stale(monkeypatch):
    """A seed past the TTL is treated like any other stale cache:
    the daemon attempts a fresh network fetch. Successful fetch
    overwrites the seed with current data."""
    seed_payload = {
        'repo': 'kent/images', 'branch': 'HEAD',
        # Way past TTL.
        'fetched_at': int(time.time()) - (cawl._INDEX_TTL_SECONDS + 60),
        'files': [{'path': 'cawl-old.jpg',
                   'url': 'https://example/old.jpg'}],
    }
    _install_bundled_seed(monkeypatch, 'kent/images', seed_payload)
    body = _stub_tree_response(['cawl-fresh.jpg'])
    fake = _FakeUrlopen(body=body)
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    result = cawl.get_index('kent/images')
    assert fake.calls == 1
    assert [f['path'] for f in result['files']] == ['cawl-fresh.jpg']


def test_stale_seed_served_when_network_fails(monkeypatch):
    """The install-day-no-network case: seed is past TTL, network
    refresh fails. Daemon serves the stale seed rather than
    returning empty — peers still see *something*."""
    seed_payload = {
        'repo': 'kent/images', 'branch': 'HEAD',
        'fetched_at': int(time.time()) - (cawl._INDEX_TTL_SECONDS + 60),
        'files': [{'path': 'cawl-seeded.jpg',
                   'url': 'https://example/seeded.jpg'}],
    }
    _install_bundled_seed(monkeypatch, 'kent/images', seed_payload)
    monkeypatch.setattr(
        cawl.urllib.request, 'urlopen',
        _FakeUrlopen(exc=urllib.error.URLError('no network')))
    result = cawl.get_index('kent/images')
    # Seed survives as the served copy.
    assert [f['path'] for f in result['files']] == ['cawl-seeded.jpg']


def test_seed_rejects_malformed_bundle(monkeypatch):
    """A corrupt seed (non-JSON or not a dict) should be ignored,
    not crash get_index. The normal fetch path runs as if no
    seed existed."""
    # Non-JSON bytes.
    _install_bundled_seed(monkeypatch, 'kent/images',
                          b'this is not valid json at all')
    body = _stub_tree_response(['cawl-net.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=body))
    result = cawl.get_index('kent/images')
    assert [f['path'] for f in result['files']] == ['cawl-net.jpg']
    # And cache file from the fetch is on disk, not the corrupt seed.
    cached = json.load(open(cawl.index_path('kent/images')))
    assert [f['path'] for f in cached['files']] == ['cawl-net.jpg']


def test_seed_rejects_invalid_repo_shape(monkeypatch):
    """get_index('') / get_index('no-slash') / get_index('owner/')
    must not attempt to seed (would either fail or hit malformed
    importlib paths). The early-return in _seed_index_if_bundled
    covers this."""
    fake = _FakeUrlopen(body=b'unused')
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    # Empty: returns {} without any seed/fetch attempt.
    assert cawl.get_index('') == {}
    assert fake.calls == 0
    # Slash-less repo: passes through to fetch (no seed lookup).
    # (_seed_index_if_bundled returns early on '/' not in repo.)
    body = _stub_tree_response(['x.jpg'])
    monkeypatch.setattr(cawl.urllib.request, 'urlopen',
                        _FakeUrlopen(body=body))
    cawl.get_index('owner-without-slash')
    # No crash. The cache file would be at $AZT_HOME/cawl/
    # owner-without-slash/index.json — not a structural error.


def test_seed_works_per_endpoint(monkeypatch):
    """End-to-end: bundled seed exists for the daemon-global repo,
    a project is registered with that repo, the dispatch endpoint
    returns the seeded content without any network call."""
    seed_payload = {
        'repo': 'kent/images', 'branch': 'HEAD',
        'fetched_at': int(time.time()),
        'files': [{'path': 'cawl-from-seed.jpg',
                   'url': 'https://example/seed.jpg'}],
    }
    _install_bundled_seed(monkeypatch, 'kent/images', seed_payload)
    _register_project()   # uses kent/images via reset_cawl_state fixture
    fake = _FakeUrlopen(exc=AssertionError('no network expected'))
    monkeypatch.setattr(cawl.urllib.request, 'urlopen', fake)
    status, resp = srv.dispatch(
        'GET', '/v1/projects/sw-x-test/cawl/index', None)
    assert status == 200
    assert (resp['index']['files'][0]['path']
            == 'cawl-from-seed.jpg')
    assert fake.calls == 0
