"""NOTES #3 — LAN-shared CAWL cache between paired peers.

Smoke tests for the listener endpoint shape (`_handle_cawl_fetch_bodyauth`)
and the requester helper (`_fetch_image_bytes_from_lan_peer`).

The actual cross-peer LAN handshake is exercised in the manual
smoke matrix (two phones on the same Wi-Fi); these tests pin the
in-process plumbing: body validation, peer-auth gating, basename
canonicalization, cache hit / miss responses, and the wire-format
contract between requester and listener.
"""

import io
import os

import pytest

from azt_collabd import cawl
from azt_collabd import lan_listener


# ── helpers ────────────────────────────────────────────────────────────


def _wsgi_environ_post(body_bytes):
    """Minimal WSGI environ for a POST with a JSON body."""
    return {
        'REQUEST_METHOD': 'POST',
        'PATH_INFO': '/v1/lan/cawl_fetch',
        'CONTENT_LENGTH': str(len(body_bytes)),
        'CONTENT_TYPE': 'application/json',
        'wsgi.input': io.BytesIO(body_bytes),
    }


class _StartResponseRecorder:
    """Capture WSGI status + headers."""

    def __init__(self):
        self.status = None
        self.headers = None

    def __call__(self, status, headers):
        self.status = status
        self.headers = headers
        return lambda _b: None


def _seed_cached_image(azt_home_dir, repo, rel_path, content):
    """Write a fake image into the daemon's CAWL cache."""
    target = os.path.join(
        str(azt_home_dir), 'cawl', repo, 'images', rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'wb') as f:
        f.write(content)
    return target


# ── _handle_cawl_fetch_bodyauth ───────────────────────────────────────


def test_handler_rejects_unpaired_peer(azt_home, monkeypatch):
    """A caller whose peer_id isn't in peers.json gets 403."""
    from azt_collabd import peers
    monkeypatch.setattr(peers, 'get_peer', lambda pid: None)
    import json
    body = json.dumps({
        'peer_id': 'a' * 64,
        'fp': 'b' * 64,
        'owner': 'kent',
        'repo': 'images',
        'rel_path': 'foo.png',
    }).encode('utf-8')
    rec = _StartResponseRecorder()
    response = lan_listener._handle_cawl_fetch_bodyauth(
        _wsgi_environ_post(body), rec)
    assert rec.status.startswith('403'), rec.status
    body_text = b''.join(response).decode('utf-8')
    assert 'not_paired' in body_text


def test_handler_rejects_fp_mismatch(azt_home, monkeypatch):
    """A paired peer whose claimed fp doesn't match peers.json gets 403."""
    from azt_collabd import peers
    monkeypatch.setattr(peers, 'get_peer',
                        lambda pid: {'peer_id': pid, 'fp': 'c' * 64})
    import json
    body = json.dumps({
        'peer_id': 'a' * 64,
        'fp': 'b' * 64,   # claimed
        'owner': 'kent',
        'repo': 'images',
        'rel_path': 'foo.png',
    }).encode('utf-8')
    rec = _StartResponseRecorder()
    response = lan_listener._handle_cawl_fetch_bodyauth(
        _wsgi_environ_post(body), rec)
    assert rec.status.startswith('403'), rec.status


def test_handler_404_when_not_cached(azt_home, monkeypatch):
    """A valid request for an image the daemon doesn't have cached
    returns 404, not an upstream-fetch attempt. The listener serves
    cache only."""
    from azt_collabd import peers
    monkeypatch.setattr(
        peers, 'get_peer',
        lambda pid: {'peer_id': pid, 'fp': 'b' * 64})
    # Also stub the index resolver so it doesn't touch the network.
    monkeypatch.setattr(
        cawl, '_resolve_basename_via_index',
        lambda repo, basename: (basename, False))
    import json
    body = json.dumps({
        'peer_id': 'a' * 64,
        'fp': 'b' * 64,
        'owner': 'kent',
        'repo': 'images',
        'rel_path': 'foo.png',
    }).encode('utf-8')
    rec = _StartResponseRecorder()
    response = lan_listener._handle_cawl_fetch_bodyauth(
        _wsgi_environ_post(body), rec)
    assert rec.status.startswith('404'), rec.status
    body_text = b''.join(response).decode('utf-8')
    assert 'not_cached' in body_text


def test_handler_200_serves_cached_bytes(azt_home, monkeypatch):
    """The happy path: paired peer, valid fp, image cached at the
    canonical path. Returns the bytes with Content-Type
    application/octet-stream."""
    from azt_collabd import peers
    monkeypatch.setattr(
        peers, 'get_peer',
        lambda pid: {'peer_id': pid, 'fp': 'b' * 64})
    monkeypatch.setattr(
        cawl, '_resolve_basename_via_index',
        lambda repo, basename: (basename, False))
    content = b'\x89PNG fake bytes'
    _seed_cached_image(azt_home, 'kent/images', 'foo.png', content)
    import json
    body = json.dumps({
        'peer_id': 'a' * 64,
        'fp': 'b' * 64,
        'owner': 'kent',
        'repo': 'images',
        'rel_path': 'foo.png',
    }).encode('utf-8')
    rec = _StartResponseRecorder()
    response = lan_listener._handle_cawl_fetch_bodyauth(
        _wsgi_environ_post(body), rec)
    assert rec.status.startswith('200'), rec.status
    headers = dict(rec.headers)
    assert headers.get('Content-Type') == 'application/octet-stream'
    body_bytes = b''.join(response)
    assert body_bytes == content


def test_handler_canonicalizes_basename_via_index(azt_home, monkeypatch):
    """A flat basename request for an image cached at a nested path
    is resolved through the index. Mirrors what ``get_image_path``
    does on the local path."""
    from azt_collabd import peers
    monkeypatch.setattr(
        peers, 'get_peer',
        lambda pid: {'peer_id': pid, 'fp': 'b' * 64})

    # Index resolver returns the nested rel_path.
    def _resolve(repo, basename):
        return ('0001_body/' + basename, True)
    monkeypatch.setattr(
        cawl, '_resolve_basename_via_index', _resolve)
    content = b'nested bytes'
    _seed_cached_image(
        azt_home, 'kent/images', '0001_body/foo.png', content)
    import json
    body = json.dumps({
        'peer_id': 'a' * 64,
        'fp': 'b' * 64,
        'owner': 'kent',
        'repo': 'images',
        'rel_path': 'foo.png',
    }).encode('utf-8')
    rec = _StartResponseRecorder()
    response = lan_listener._handle_cawl_fetch_bodyauth(
        _wsgi_environ_post(body), rec)
    assert rec.status.startswith('200'), rec.status
    body_bytes = b''.join(response)
    assert body_bytes == content


def test_handler_rejects_path_traversal_in_rel_path(azt_home, monkeypatch):
    """``rel_path`` must not contain ``..``, ``\\``, or start with
    ``.`` or ``/``. A nested ``/`` mid-path IS allowed
    (``0001_body/foo.png``) — the requester sends full rel_paths
    to disambiguate same-basename-different-variant cases."""
    from azt_collabd import peers
    monkeypatch.setattr(
        peers, 'get_peer',
        lambda pid: {'peer_id': pid, 'fp': 'b' * 64})
    import json
    bad_rel_paths = [
        '../escape.png',
        'a/../escape.png',
        'a\\b.png',
        '.hidden.png',
        '/abs/path.png',
    ]
    for rp in bad_rel_paths:
        body = json.dumps({
            'peer_id': 'a' * 64,
            'fp': 'b' * 64,
            'owner': 'kent',
            'repo': 'images',
            'rel_path': rp,
        }).encode('utf-8')
        rec = _StartResponseRecorder()
        lan_listener._handle_cawl_fetch_bodyauth(
            _wsgi_environ_post(body), rec)
        assert rec.status.startswith('400'), (rp, rec.status)


def test_handler_accepts_nested_rel_path(azt_home, monkeypatch):
    """A nested ``rel_path`` like ``0001_body/foo.png`` is served
    directly — no index canonicalization needed. This is the
    preferred shape for the 0.50.14 lan_extras fetch path, which
    needs to disambiguate same-basename variants."""
    from azt_collabd import peers
    monkeypatch.setattr(
        peers, 'get_peer',
        lambda pid: {'peer_id': pid, 'fp': 'b' * 64})
    monkeypatch.setattr(
        cawl, '_resolve_basename_via_index',
        lambda repo, basename: pytest.fail(
            f'should not be called for nested rel_path: '
            f'{basename!r}'))
    content = b'nested-direct bytes'
    _seed_cached_image(
        azt_home, 'kent/images', '0001_body/foo.png', content)
    import json
    body = json.dumps({
        'peer_id': 'a' * 64,
        'fp': 'b' * 64,
        'owner': 'kent',
        'repo': 'images',
        'rel_path': '0001_body/foo.png',
    }).encode('utf-8')
    rec = _StartResponseRecorder()
    response = lan_listener._handle_cawl_fetch_bodyauth(
        _wsgi_environ_post(body), rec)
    assert rec.status.startswith('200'), rec.status
    body_bytes = b''.join(response)
    assert body_bytes == content


def test_handler_rejects_slash_in_owner_or_repo(azt_home, monkeypatch):
    from azt_collabd import peers
    monkeypatch.setattr(
        peers, 'get_peer',
        lambda pid: {'peer_id': pid, 'fp': 'b' * 64})
    import json
    body = json.dumps({
        'peer_id': 'a' * 64,
        'fp': 'b' * 64,
        'owner': 'kent/escape',
        'repo': 'images',
        'rel_path': 'foo.png',
    }).encode('utf-8')
    rec = _StartResponseRecorder()
    lan_listener._handle_cawl_fetch_bodyauth(
        _wsgi_environ_post(body), rec)
    assert rec.status.startswith('400'), rec.status


# ── _fetch_image_bytes_from_lan_peer ──────────────────────────────────


def test_requester_returns_none_when_no_paired_peers(azt_home, monkeypatch):
    """An empty paired-peers list means no LAN candidates; return
    None so the caller falls through to GitHub."""
    from azt_collabd import peers
    from azt_collabd import peer_id as _pid
    monkeypatch.setattr(peers, 'list_peers', lambda: [])
    monkeypatch.setattr(
        _pid, 'ensure',
        lambda: {'peer_id': 'a' * 64, 'fp': 'b' * 64})
    result = cawl._fetch_image_bytes_from_lan_peer(
        'kent/images', 'foo.png')
    assert result is None


def test_requester_returns_none_when_no_resolved_endpoints(
        azt_home, monkeypatch):
    """Paired peers exist but none have resolvable endpoints —
    return None."""
    from azt_collabd import peers
    from azt_collabd import peer_id as _pid
    from azt_collabd import lan_discovery
    monkeypatch.setattr(
        peers, 'list_peers',
        lambda: [{'peer_id': 'c' * 64, 'fp': 'd' * 64}])
    monkeypatch.setattr(lan_discovery, 'get_endpoint',
                        lambda pid: None)
    monkeypatch.setattr(
        _pid, 'ensure',
        lambda: {'peer_id': 'a' * 64, 'fp': 'b' * 64})
    result = cawl._fetch_image_bytes_from_lan_peer(
        'kent/images', 'foo.png')
    assert result is None


def test_requester_returns_none_on_bad_repo_slug(azt_home, monkeypatch):
    """A repo slug without ``owner/name`` shape is malformed for
    CAWL purposes; bail before iterating peers."""
    result = cawl._fetch_image_bytes_from_lan_peer(
        'badformat-no-slash', 'foo.png')
    assert result is None


def test_requester_returns_none_on_path_traversal_basename(
        azt_home):
    result = cawl._fetch_image_bytes_from_lan_peer(
        'kent/images', '../escape.png')
    assert result is None


def test_requester_returns_bytes_from_successful_peer(
        azt_home, monkeypatch):
    """Two paired peers, the second one has the byte. The requester
    iterates both, returning the bytes from the one that succeeded."""
    from azt_collabd import peers
    from azt_collabd import peer_id as _pid
    from azt_collabd import lan_discovery
    monkeypatch.setattr(peers, 'list_peers', lambda: [
        {'peer_id': 'c' * 64, 'fp': 'd' * 64},  # 1st: returns None
        {'peer_id': 'e' * 64, 'fp': 'f' * 64},  # 2nd: returns bytes
    ])
    monkeypatch.setattr(
        lan_discovery, 'get_endpoint',
        lambda pid: ('127.0.0.1', 9999))
    monkeypatch.setattr(
        _pid, 'ensure',
        lambda: {'peer_id': 'a' * 64, 'fp': 'b' * 64})

    expected_bytes = b'image-from-peer-2'
    call_count = {'n': 0}

    def _fake_post(host, port, expected_fp, body_json):
        call_count['n'] += 1
        if expected_fp == 'f' * 64:
            return expected_bytes
        return None
    monkeypatch.setattr(cawl, '_post_lan_cawl_fetch', _fake_post)
    result = cawl._fetch_image_bytes_from_lan_peer(
        'kent/images', '0001_body/foo.png')
    assert result == expected_bytes
    assert call_count['n'] == 2  # both peers were tried


def test_requester_short_circuits_on_first_hit(azt_home, monkeypatch):
    """If the first peer has the byte, the second isn't asked."""
    from azt_collabd import peers
    from azt_collabd import peer_id as _pid
    from azt_collabd import lan_discovery
    monkeypatch.setattr(peers, 'list_peers', lambda: [
        {'peer_id': 'c' * 64, 'fp': 'd' * 64},
        {'peer_id': 'e' * 64, 'fp': 'f' * 64},
    ])
    monkeypatch.setattr(
        lan_discovery, 'get_endpoint',
        lambda pid: ('127.0.0.1', 9999))
    monkeypatch.setattr(
        _pid, 'ensure',
        lambda: {'peer_id': 'a' * 64, 'fp': 'b' * 64})

    expected_bytes = b'image-from-peer-1'
    call_count = {'n': 0}

    def _fake_post(host, port, expected_fp, body_json):
        call_count['n'] += 1
        return expected_bytes if expected_fp == 'd' * 64 else None
    monkeypatch.setattr(cawl, '_post_lan_cawl_fetch', _fake_post)
    result = cawl._fetch_image_bytes_from_lan_peer(
        'kent/images', 'foo.png')
    assert result == expected_bytes
    assert call_count['n'] == 1  # short-circuited
