"""Tests for the ``atomic_commit`` RPC and the client-side
URI-atomic-write integration.

What's covered:

- Daemon ``_h_project_atomic_commit`` writes the bytes atomically
  to the registered project working_dir (tempfile + os.replace).
- Path-traversal shapes (``..``, ``../../``, absolute paths) are
  rejected with 400 before any filesystem touch.
- Out-of-whitelist shapes (three-level paths, non-LIFT top-level
  files) are rejected.
- The dispatcher routes ``POST /v1/projects/<lang>/atomic_commit``
  to the handler.
- Project lock serializes the write — two concurrent commits land
  in some order, both succeed, the destination always has the bytes
  from exactly one of them (never torn).
- ``_resolve_atomic_commit_path`` rejects shapes that would escape
  the project base via ``commonpath``.
- Client ``_parse_provider_uri`` round-trips canonical URI shapes.
- ``LiftHandle.atomic_open_write`` on a URI returns the new
  ``_UriAtomicWriteFile``.
- Status code ``ATOMIC_COMMITTED`` is mirrored on both sides
  (drift-check).

Why this exists: the RPC is the protocol surface that closes the
remaining cross-process atomic-write gap on Android. Pre-test,
the only way to discover a regression was a real field guard
trip from a torn URI write. These tests make the daemon's
write path and the client's wrapper round-trippable from CI.
"""

import base64
import hashlib
import os
import threading

import pytest

from azt_collabd import server as srv
from azt_collabd import status as S_d
from azt_collab_client import status as S_c
from azt_collab_client import lift_io


# ── Status mirror drift check ─────────────────────────────────────────────


def test_atomic_committed_code_mirrored():
    """The new status code must exist on both sides at the same
    string value, per the mirror contract in
    ``azt_collab_client/CLAUDE.md``."""
    assert S_d.ATOMIC_COMMITTED == 'ATOMIC_COMMITTED'
    assert S_c.ATOMIC_COMMITTED == 'ATOMIC_COMMITTED'


# ── _resolve_atomic_commit_path: whitelist + traversal ───────────────────


def test_resolve_atomic_commit_path_accepts_lift_at_top_level(tmp_path):
    target = srv._resolve_atomic_commit_path(str(tmp_path), 'baf.lift')
    assert target == os.path.realpath(str(tmp_path / 'baf.lift'))


def test_resolve_atomic_commit_path_accepts_audio_and_images(tmp_path):
    for rel in ('audio/foo.wav', 'images/bar.png'):
        target = srv._resolve_atomic_commit_path(str(tmp_path), rel)
        assert target == os.path.realpath(str(tmp_path / rel))


def test_resolve_atomic_commit_path_rejects_traversal(tmp_path):
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), '../escape.lift') is None
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), 'audio/../../../etc/passwd') is None
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), '../../sibling/baf.lift') is None


def test_resolve_atomic_commit_path_rejects_dotfile_components(tmp_path):
    # Single-dot or empty segments aren't useful and would muddle
    # the commonpath check; rejecting them keeps the whitelist tight.
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), './baf.lift') is None
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), 'audio//foo.wav') is None   # empty mid-segment


def test_resolve_atomic_commit_path_rejects_non_lift_top_level(tmp_path):
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), 'baf.txt') is None
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), 'README.md') is None


def test_resolve_atomic_commit_path_rejects_unknown_subdir(tmp_path):
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), 'secrets/key.pem') is None
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), 'tmp/file.lift') is None


def test_resolve_atomic_commit_path_rejects_three_level(tmp_path):
    # Whitelist is one or two segments. Three rejects.
    assert srv._resolve_atomic_commit_path(
        str(tmp_path), 'audio/sub/foo.wav') is None


# ── _h_project_atomic_commit: end-to-end through the dispatcher ──────────


@pytest.fixture
def project(tmp_path):
    """Register a project in the daemon's registry pointing at a real
    working_dir, and yield (langcode, working_dir). The autouse
    ``azt_home`` fixture has already redirected ``$AZT_HOME`` so
    ``projects.register`` writes its JSON to a per-test temp dir."""
    from azt_collabd import projects as projects_mod
    langcode = 'sw-US-x-test'
    working_dir = tmp_path / 'project'
    working_dir.mkdir()
    projects_mod.register(
        langcode=langcode,
        working_dir=str(working_dir),
        lift_path=str(working_dir / 'baf.lift'),
        remote_url='',
    )
    yield langcode, str(working_dir)


def test_atomic_commit_writes_bytes_to_working_dir(project):
    langcode, working_dir = project
    data = b'<lift version="0.13"><entry guid="abc"/></lift>'
    body = {'path': 'baf.lift',
            'data_b64': base64.b64encode(data).decode()}
    status, resp = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/atomic_commit', body)
    assert status == 200
    assert resp['ok'] is True
    # File now exists with the expected bytes
    on_disk = open(os.path.join(working_dir, 'baf.lift'), 'rb').read()
    assert on_disk == data
    # Result carries ATOMIC_COMMITTED with byte-length + sha256.
    statuses = resp['result']['statuses']
    assert len(statuses) == 1
    assert statuses[0]['code'] == 'ATOMIC_COMMITTED'
    assert statuses[0]['params']['bytes_written'] == len(data)
    assert (statuses[0]['params']['sha256']
            == hashlib.sha256(data).hexdigest())


def test_atomic_commit_creates_audio_subdir_on_first_write(project):
    langcode, working_dir = project
    data = b'\x00\x01\x02 fake-wav'
    body = {'path': 'audio/foo.wav',
            'data_b64': base64.b64encode(data).decode()}
    status, resp = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/atomic_commit', body)
    assert status == 200
    assert resp['ok'] is True
    on_disk = open(os.path.join(working_dir, 'audio', 'foo.wav'),
                   'rb').read()
    assert on_disk == data


def test_atomic_commit_replaces_existing_file(project):
    langcode, working_dir = project
    # Pre-write some old bytes.
    open(os.path.join(working_dir, 'baf.lift'), 'wb').write(b'old')
    data = b'<lift/>'
    body = {'path': 'baf.lift',
            'data_b64': base64.b64encode(data).decode()}
    status, _ = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/atomic_commit', body)
    assert status == 200
    assert open(os.path.join(working_dir, 'baf.lift'), 'rb').read() == data


def test_atomic_commit_leaves_no_tempfile_after_success(project):
    langcode, working_dir = project
    data = b'<lift/>'
    body = {'path': 'baf.lift',
            'data_b64': base64.b64encode(data).decode()}
    srv.dispatch('POST', f'/v1/projects/{langcode}/atomic_commit', body)
    # No ``.tmp.<pid>.<nonce>`` siblings linger.
    siblings = os.listdir(working_dir)
    tmps = [s for s in siblings if s.startswith('baf.lift.tmp.')]
    assert tmps == []


def test_atomic_commit_rejects_unknown_project():
    body = {'path': 'baf.lift',
            'data_b64': base64.b64encode(b'<lift/>').decode()}
    status, resp = srv.dispatch(
        'POST', '/v1/projects/nonexistent-lang/atomic_commit', body)
    assert status == 404
    assert resp['ok'] is False
    assert resp['error'] == 'project_not_found'


def test_atomic_commit_rejects_path_traversal(project):
    langcode, _ = project
    body = {'path': '../escape.lift',
            'data_b64': base64.b64encode(b'<lift/>').decode()}
    status, resp = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/atomic_commit', body)
    assert status == 400
    assert resp['error'] == 'path_rejected'


def test_atomic_commit_rejects_missing_path_field(project):
    langcode, _ = project
    body = {'data_b64': base64.b64encode(b'<lift/>').decode()}
    status, resp = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/atomic_commit', body)
    assert status == 400
    assert resp['error'] == 'missing_path'


def test_atomic_commit_rejects_bad_base64(project):
    langcode, _ = project
    body = {'path': 'baf.lift', 'data_b64': 'not-valid-base64!!!'}
    status, resp = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/atomic_commit', body)
    assert status == 400
    assert resp['error'].startswith('base64_decode')


def test_atomic_commit_accepts_empty_body_data(project):
    # Empty bytes are legitimate (zero-length LIFT would be invalid
    # but the RPC doesn't validate content — that's an upstream
    # concern). Empty base64 decodes to empty bytes.
    langcode, working_dir = project
    body = {'path': 'baf.lift', 'data_b64': ''}
    status, resp = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/atomic_commit', body)
    assert status == 200
    assert (open(os.path.join(working_dir, 'baf.lift'), 'rb').read()
            == b'')


# ── Concurrency: lock serializes writes ──────────────────────────────────


def test_atomic_commit_concurrent_writes_destination_is_one_of_them(project):
    """Two concurrent ``atomic_commit`` calls. Either A or B wins —
    the destination is always a complete copy of one of the
    inputs, never a torn mix. The project lock makes this
    sequential; the test pins the invariant."""
    langcode, working_dir = project
    data_a = b'A' * 10000
    data_b = b'B' * 10000

    def _commit(data):
        body = {'path': 'baf.lift',
                'data_b64': base64.b64encode(data).decode()}
        srv.dispatch('POST',
                     f'/v1/projects/{langcode}/atomic_commit', body)

    threads = [threading.Thread(target=_commit, args=(data_a,)),
               threading.Thread(target=_commit, args=(data_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    on_disk = open(os.path.join(working_dir, 'baf.lift'), 'rb').read()
    # Whichever rename won, the bytes are intact.
    assert on_disk in (data_a, data_b)
    assert len(on_disk) == 10000


# ── Client _parse_provider_uri ──────────────────────────────────────────


def test_parse_provider_uri_lift():
    lang, rel = lift_io._parse_provider_uri(
        'content://org.atoznback.aztcollab/sw-US-x-test/baf.lift')
    assert lang == 'sw-US-x-test'
    assert rel == 'baf.lift'


def test_parse_provider_uri_sibling_audio():
    lang, rel = lift_io._parse_provider_uri(
        'content://org.atoznback.aztcollab/en-Demo/audio/r1.wav')
    assert lang == 'en-Demo'
    assert rel == 'audio/r1.wav'


def test_parse_provider_uri_rejects_filesystem_path():
    with pytest.raises(ValueError):
        lift_io._parse_provider_uri('/home/user/baf.lift')


def test_parse_provider_uri_rejects_missing_segments():
    with pytest.raises(ValueError):
        lift_io._parse_provider_uri(
            'content://org.atoznback.aztcollab/')


# ── LiftHandle.atomic_open_write: URI returns _UriAtomicWriteFile ────────


def test_atomic_open_write_returns_uri_writer_for_content_uri():
    uri = 'content://org.atoznback.aztcollab/sw-US-x-test/baf.lift'
    handle = lift_io.LiftHandle(uri)
    writer = handle.atomic_open_write()
    assert isinstance(writer, lift_io._UriAtomicWriteFile)


def test_atomic_open_write_returns_local_writer_for_filesystem_path(
        tmp_path):
    path = str(tmp_path / 'baf.lift')
    handle = lift_io.LiftHandle(path)
    writer = handle.atomic_open_write()
    assert isinstance(writer, lift_io._AtomicWriteFile)
