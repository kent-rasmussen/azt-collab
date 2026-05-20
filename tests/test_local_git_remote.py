"""Tests that the daemon's git pipeline works against a generic
local HTTP git server, not just GitHub.

What's covered:

- A dulwich.web ``HTTPGitApplication`` in a background thread
  serves a bare repo over plain HTTP (the fixture itself —
  sanity check that ``info/refs`` responds).
- ``init_repo`` initializes a working dir, commits, and pushes
  to the local server; the bare-side ``get_refs()`` reflects
  the push.
- ``clone_repo`` against the local server produces a working
  dir whose contents match what was seeded.
- ``pull_repo`` brings updates committed against the bare repo
  via a second working dir.
- ``sync_repo`` (the combined commit+push under one lock)
  converges two working dirs through the local server — peer A
  commits + syncs, peer B pulls, peer B commits + syncs, peer A
  pulls.

Why this exists: the daemon's remote handling is intended to be
host-agnostic. The github-mediated path is what's exercised in
the field, but a team can equally point ``Project.remote_url``
at a gitea / forgejo / gogs / git-daemon on the office LAN. The
parked LAN-sync spec (``docs/local_lan_sync_stub.md``) builds
on dulwich.web as the in-process listener, so exercising
dulwich.web as a git server in CI today gives the spec a
foundation that won't quietly rot. A future github-ism
(substring matching on a github-specific error string,
host-header assumptions in dulwich, credentials-store lookup
keyed on ``github.com``) would silently break the local-server
case otherwise.

Auth is intentionally out of scope here — the fixture serves
unauthenticated HTTP; the credentials store is exercised by
``test_contributor`` and the github-flavoured paths. These
tests assert the git-protocol round-trip works against a
non-github host; auth integration on a non-github host is a
follow-up when the LAN-sync spec implementation lands.
"""

import threading
import urllib.request
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

import pytest

from azt_collabd import repo as azt_repo
from azt_collabd import status as S


# ── Fixture: dulwich.web HTTP git server in a thread ─────────────────────


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def local_git_server(tmp_path):
    """Spin up a dulwich-backed HTTP git server in a background
    thread serving a bare repo from a temp dir.

    Yields ``(url, remote_path, bare_repo)``. The URL has a
    trailing slash; the bare repo is mounted at the URL root via
    ``DictBackend({b'/': bare})``. Shuts down cleanly on test
    exit.
    """
    from dulwich.repo import Repo
    from dulwich.server import DictBackend
    from dulwich.web import make_wsgi_chain

    remote_path = tmp_path / "remote.git"
    # dulwich ≥ 0.22 stopped auto-creating ``controldir`` inside
    # ``Repo._init_maybe_bare`` — it now expects the path to exist
    # and ``os.mkdir``s only its ``branches`` / ``hooks`` / ``info``
    # subdirectories. Without this ``mkdir`` the fixture fails with
    # ``FileNotFoundError: '<tmp>/remote.git/branches'``.
    remote_path.mkdir()
    bare = Repo.init_bare(str(remote_path))
    backend = DictBackend({b"/": bare})
    app = make_wsgi_chain(backend)

    server = make_server("127.0.0.1", 0, app,
                         server_class=_ThreadingWSGIServer)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{port}/", remote_path, bare
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ── Sanity: the fixture is reachable ─────────────────────────────────────


def test_local_git_server_fixture_responds(local_git_server):
    """``info/refs`` over HTTP returns 200 and the smart-protocol
    capability advertisement. This validates the fixture itself
    so a failure in the substantive tests below isn't ambiguous
    between "the daemon's pipeline broke" and "the test server
    didn't come up."""
    url, _, _ = local_git_server
    with urllib.request.urlopen(
            f"{url}info/refs?service=git-upload-pack", timeout=5) as resp:
        assert resp.status == 200
        body = resp.read()
    assert b"git-upload-pack" in body


# ── init_repo + push against a non-github URL ────────────────────────────


def test_init_repo_pushes_to_local_server(local_git_server, tmp_path):
    """``init_repo`` against a non-github URL initializes a repo,
    commits the working-dir contents, sets ``origin`` to the
    local server URL, and pushes. The bare repo's ``get_refs()``
    must reflect the push afterward."""
    url, _, bare = local_git_server

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "baf.lift").write_bytes(b"<lift/>\n")

    result = azt_repo.init_repo(
        str(work_dir), url, "anyuser", "anytoken",
        contributor_name="Tester")

    assert result.has(S.INITIALIZED) or result.has(S.ALREADY_INITIALIZED)
    refs = bare.get_refs()
    branch_refs = [r for r in refs if r.startswith(b"refs/heads/")]
    assert branch_refs, (
        f"bare repo should have at least one branch ref after push; "
        f"got {sorted(refs.keys())!r}")


# ── clone_repo against a non-github URL ──────────────────────────────────


def test_clone_repo_from_local_server(local_git_server, tmp_path):
    """``clone_repo`` against the local server produces a working
    dir whose files match what was seeded. Round-trips through
    the smart-HTTP protocol both for push (seeding) and clone."""
    url, _, _ = local_git_server

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "baf.lift").write_bytes(b'<lift version="0.13"/>\n')
    init_result = azt_repo.init_repo(
        str(seed), url, "u", "t", contributor_name="Seeder")
    assert init_result.has(S.INITIALIZED)

    dest = tmp_path / "clone"
    lift_path, clone_result = azt_repo.clone_repo(
        url, str(dest), "u", "t")

    assert clone_result.has(S.CLONED), (
        f"clone should succeed against the local server; "
        f"got codes={clone_result.codes()!r}")
    assert (dest / "baf.lift").read_bytes() == b'<lift version="0.13"/>\n'


# ── pull_repo brings updates from a non-github URL ───────────────────────


def test_pull_repo_fetches_updates_from_local_server(local_git_server,
                                                    tmp_path):
    """A second commit on the seed-side working dir is reachable
    by ``pull_repo`` on the clone-side working dir, with the
    local server in the middle."""
    url, _, _ = local_git_server

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "baf.lift").write_bytes(b"<lift/>\n")
    azt_repo.init_repo(str(seed), url, "u", "t", contributor_name="Seeder")

    dest = tmp_path / "clone"
    azt_repo.clone_repo(url, str(dest), "u", "t")
    assert (dest / "baf.lift").exists()
    assert not (dest / "added.lift").exists()

    # Seed-side adds a commit and pushes it.
    (seed / "added.lift").write_bytes(b"<added/>\n")
    commit_result = azt_repo.commit_repo(str(seed), "Seeder")
    assert commit_result.has(S.COMMITTED), (
        f"seed-side commit should land; codes={commit_result.codes()!r}")
    push_result = azt_repo.push_repo(str(seed), "u", "t")
    assert push_result.has(S.PUSHED), (
        f"seed-side push should land; codes={push_result.codes()!r}")

    # Clone-side pulls.
    pull_result = azt_repo.pull_repo(str(dest), "u", "t")
    assert (dest / "added.lift").read_bytes() == b"<added/>\n", (
        f"pulled file should be present; "
        f"pull codes={pull_result.codes()!r}")


# ── Full round-trip via sync_repo ────────────────────────────────────────


def test_sync_repo_round_trip_via_local_server(local_git_server, tmp_path):
    """Two working dirs converge through the local server using
    the legacy combined ``sync_repo`` (commit+push under one
    lock). This is the closest analogue to the field flow where
    a peer's Sync button does both halves atomically against a
    non-github host."""
    url, _, _ = local_git_server

    a = tmp_path / "a"
    a.mkdir()
    (a / "baf.lift").write_bytes(b"<lift/>\n")
    azt_repo.init_repo(str(a), url, "u", "t", contributor_name="Alice")

    b = tmp_path / "b"
    azt_repo.clone_repo(url, str(b), "u", "t")
    assert (b / "baf.lift").exists()

    # A adds a file, A syncs (commit + push).
    (a / "a_change.lift").write_bytes(b"<a/>\n")
    a_sync = azt_repo.sync_repo(str(a), "u", "t", "Alice")
    assert a_sync.has(S.COMMITTED), a_sync.codes()
    assert a_sync.has(S.PUSHED), a_sync.codes()

    # B pulls, sees A's change.
    azt_repo.pull_repo(str(b), "u", "t")
    assert (b / "a_change.lift").read_bytes() == b"<a/>\n"

    # B adds a file, B syncs.
    (b / "b_change.lift").write_bytes(b"<b/>\n")
    b_sync = azt_repo.sync_repo(str(b), "u", "t", "Bob")
    assert b_sync.has(S.PUSHED), b_sync.codes()

    # A pulls, sees B's change.
    azt_repo.pull_repo(str(a), "u", "t")
    assert (a / "b_change.lift").read_bytes() == b"<b/>\n"
