"""
LAN-side clone path for the combined pair-share-clone flow
(parked spec § "Combined scan flow", 0.45.0).

When the recipient scans a pair-QR that includes a ``langcode`` (and
optionally a ``repo_url``), or accepts a paired peer's
``share_offer``, this module:

  1. Resolves the owner's LAN endpoint (mDNS / static / QR-hint).
  2. Runs ``ls-remote`` to peek at the owner's project SHAs (cheap
     — protocol round-trip only, no packfile transfer).
  3. Compares with our local same-langcode project if any:

       - No local same-langcode project → fresh LAN clone, register.
       - Same langcode, zero shared commits → refuse
         (``LAN_PROJECT_COLLISION_UNRELATED``).
       - Same langcode, shared commits → existing project; record
         the LAN pair / share without re-cloning. Resolve any
         ``remote_url`` divergence per the table in the spec.

  4. For brand-new clones, runs ``dulwich.porcelain.clone`` over
     TLS-pinned HTTPS to ``https://{host}:{port}/{langcode}.git``,
     using *our* peer cert as client auth and pinning the owner's
     cert fingerprint via urllib3's ``assert_fingerprint``.
  5. Registers the project in ``projects.json`` (no ``remote_url``
     yet — that takes user confirmation per the always-confirm-
     adopt-origin rule).
  6. If an ``incoming_url`` was supplied, stashes a
     ``LAN_ADOPT_ORIGIN_NEEDED`` pending decision so the user can
     opt into github sync at their pace.
  7. Sets ``last_project`` so the picker resumes into the freshly-
     cloned project.

Synchronous on purpose: LAN clones are local-network fast, the
caller is a peer-side UI tap, and a sync return makes the picker
flow ("scan → in your project") trivial. Compare with
``_clone_worker`` in ``server.py`` which is async because github
clones can take minutes.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import ssl
import sys
import time as _time

from . import lan_discovery as _lan_discovery
from . import pending_decisions as _pending
from . import peer_id as _peer_id
from . import peers as _peers
from . import projects as _projects
from . import status as _S
from .paths import azt_home
from .status import Result


# Bounded wall-clock for a single ``dulwich.porcelain.clone`` over the
# LAN transport. Picked below the client's default RPC timeout (300 s
# per ``azt_collab_client.rpc.call``) so the daemon can surface a typed
# LAN_CLONE_TIMEOUT before the client gives up and routes a generic
# SERVER_ERROR. A LAN clone of a small project is seconds; large
# projects with audio can be tens of seconds. 180 s leaves headroom
# for both while keeping a wedged peer from holding the RPC open
# indefinitely.
_LAN_CLONE_TIMEOUT_S = 180.0


@contextlib.contextmanager
def _socket_timeout(seconds):
    """Set ``socket.setdefaulttimeout`` for the body; restore on exit.
    Mirror of ``repo._socket_timeout`` — duplicated rather than
    cross-imported to keep ``lan_clone`` independent of the much
    heavier ``repo`` module."""
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(prev)


def _looks_like_timeout(exc):
    """True iff *exc* (or any cause/context in its chain) is a socket
    timeout. dulwich wraps the raw exception in a few different
    shapes depending on dulwich/urllib3 version, so check the chain
    rather than just the surface type."""
    cur = exc
    seen = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.timeout):
            return True
        if isinstance(cur, TimeoutError):
            return True
        msg = str(cur).lower()
        if 'timed out' in msg or 'timeout' in msg:
            return True
        cur = getattr(cur, '__cause__', None) \
            or getattr(cur, '__context__', None)
    return False


def _is_local_tls_error(err):
    """True when the error text shows THIS side's TLS layer failing on a
    missing/unreadable local file (ssl wrapping FileNotFoundError — the
    LAN-identity peer_id/peer.crt files, typically) rather than any
    network exchange. This probes an exception repr we composed
    ourselves — never translated text — so the structured-Results rule
    is intact; classifying at the raise site would be cleaner but the
    exception arrives pre-stringified through dulwich/urllib3 wrapping.
    Field repro 2026-07-17: SSLError(FileNotFoundError(2, ...)) reported
    as 'peer didn't respond' and sent the user chasing Wi-Fi."""
    if 'SSLError' not in err:
        return False
    return ('FileNotFoundError' in err or 'No such file' in err
            or 'PermissionError' in err)


def _is_not_shared(err):
    """True when the error text shows the peer ANSWERED but its
    listener refused to serve the repo: dulwich raises
    ``NotGitRepository()`` on the listener's 404, which the peer sends
    both for "project not in any paired peer's shared_projects
    allowlist" and "project not registered here" (see
    ``lan_listener`` ``open_repository``). Same probe-our-own-repr
    caveat as ``_is_local_tls_error`` above. Field repro 2026-07-17:
    reported as 'peer didn't respond' when the phone had answered and
    the fix was sharing the project on the phone."""
    return 'NotGitRepository' in err


def _resolve_endpoint(peer_entry):
    """Endpoint resolution order: mDNS → static → QR-hint."""
    pid = peer_entry.get('peer_id', '')
    if pid:
        mdns = _lan_discovery.get_endpoint(pid)
        if mdns is not None:
            return mdns
    for source in ('static_endpoints', 'endpoints'):
        for raw in (peer_entry.get(source) or []):
            try:
                host, port = raw.rsplit(':', 1)
                return (host, int(port))
            except (ValueError, TypeError):
                continue
    return None


def _build_pool_manager(expected_fp):
    """TLS-pinned urllib3 PoolManager for talking to a paired peer's
    LAN listener. Same shape as ``lan_push._build_ssl_context`` —
    keeps the two modules aligned without sharing state."""
    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        raise RuntimeError('this daemon has no LAN identity')
    # See ``lan_push._build_ssl_context`` for why we use the
    # underscored helper instead of ``SSLContext(PROTOCOL_TLS_CLIENT)``
    # + ``verify_mode=CERT_NONE``: the latter doesn't actually skip
    # cert validation in practice (gets "self signed certificate"
    # at handshake despite the override). The underscored API is
    # the documented Python idiom for pinned-fingerprint scenarios.
    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    import urllib3
    return urllib3.PoolManager(
        ssl_context=ctx,
        assert_hostname=False,
        assert_fingerprint=expected_fp,
        # See ``lan_push._push_to_peer`` for why this is needed:
        # urllib3 overwrites our context's verify_mode with
        # ``resolve_cert_reqs(cert_reqs)`` — passing 'CERT_NONE'
        # explicitly preserves the unverified behavior.
        cert_reqs='CERT_NONE',
    )


def _peek_remote_refs(url, expected_fp):
    """``ls-remote`` against a LAN URL. Returns a dict
    ``{ref_name: sha_hex}`` or ``None`` on any failure (caller
    treats None as "couldn't check; assume no overlap" — safe
    because the worst that does is refuse a collision we couldn't
    confirm was related)."""
    try:
        from dulwich.client import HttpGitClient
        pm = _build_pool_manager(expected_fp)
        client = HttpGitClient(url, pool_manager=pm)
        # ls_remote returns LsRemoteResult on newer dulwich; older
        # versions return a dict directly. Coerce to dict.
        result = client.get_refs(b'/')
        if hasattr(result, 'refs'):
            refs = result.refs
        else:
            refs = result
        out = {}
        for name, sha in (refs or {}).items():
            if isinstance(name, bytes):
                name = name.decode('utf-8', 'ignore')
            if isinstance(sha, bytes):
                sha = sha.decode('ascii', 'ignore')
            out[name] = sha
        return out
    except Exception as ex:
        print(f'[lan-clone] ls-remote against {url!r} failed: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return None


def _shares_commits_with(refs, working_dir):
    """Return True if any SHA in *refs* is present in the dulwich
    object store of the repo at *working_dir*. Empty / missing refs
    → False (we can't establish a relation, so the conservative
    answer is "no")."""
    if not refs:
        return False
    try:
        from dulwich.repo import Repo
        repo = Repo(working_dir)
    except Exception:
        return False
    try:
        store = repo.object_store
        for sha in refs.values():
            if not sha:
                continue
            try:
                if sha.encode('ascii') in store:
                    return True
            except Exception:
                continue
        return False
    finally:
        repo.close()


def _project_dest_dir(langcode):
    """Working directory for a freshly-LAN-cloned project. Mirrors
    the github-clone convention (``$AZT_HOME/projects/<langcode>``)
    so the project layout is the same regardless of how it
    arrived."""
    return os.path.join(azt_home(), 'projects', langcode)


def _find_lift_in(working_dir):
    """First ``*.lift`` in the working tree. dulwich.porcelain.clone
    drops the project files in place; we just have to find the LIFT
    so the registry knows where it is."""
    if not os.path.isdir(working_dir):
        return ''
    try:
        for name in os.listdir(working_dir):
            if name.lower().endswith('.lift'):
                return os.path.join(working_dir, name)
    except OSError:
        return ''
    return ''


# Last-line progress of the clone currently in flight, for the
# ``GET /v1/lan/clone/progress`` poll (the receive popup shows it so
# a multi-minute first copy doesn't look hung). One slot, not
# per-langcode: user-gestured receives are serial in practice, and a
# wrong-but-live line beats a frozen screen if two ever overlap.
_PROGRESS = {'active': False, 'langcode': '', 'text': '', 'ts': 0.0}


def clone_progress():
    """Snapshot of the in-flight clone's progress (see _PROGRESS)."""
    return dict(_PROGRESS)


class _ProgressStream:
    """``errstream`` for ``porcelain.clone``: dulwich writes the
    server's sideband-2 progress here (``Counting objects: 12%
    (n/m)\\r``-style, CR-redrawn). Keeps only the newest line."""

    def write(self, data):
        try:
            text = (data.decode('utf-8', 'replace')
                    if isinstance(data, bytes) else str(data))
        except Exception:
            return 0
        for piece in text.replace('\r', '\n').split('\n'):
            piece = piece.strip()
            if piece:
                _PROGRESS['text'] = piece
                _PROGRESS['ts'] = _time.time()
        return len(data) if data else 0

    def flush(self):
        pass


def _do_lan_clone(host, port, langcode, expected_fp, dest_dir):
    """Run the actual ``porcelain.clone``. Returns ``(lift_path,
    error_str)`` — empty error means success."""
    from dulwich import porcelain
    url = f'https://{host}:{int(port)}/{langcode}.git'
    try:
        pm = _build_pool_manager(expected_fp)
    except Exception as ex:
        return '', f'tls_context_build_failed: {ex}'
    if os.path.exists(dest_dir):
        # Leftover from a prior failed clone — wipe so dulwich gets
        # an empty dir to populate. (Successful clones never reach
        # this path because the collision check above catches them
        # and routes to the "reopened" branch.)
        try:
            shutil.rmtree(dest_dir)
        except OSError as ex:
            return '', f'wipe_dest_failed: {ex}'
    os.makedirs(dest_dir, exist_ok=True)
    _PROGRESS.update(active=True, langcode=langcode, text='',
                     ts=_time.time())
    try:
        with _socket_timeout(_LAN_CLONE_TIMEOUT_S):
            porcelain.clone(url, dest_dir, pool_manager=pm,
                            errstream=_ProgressStream())
    except TypeError:
        # dulwich without pool_manager kwarg — same fallback as
        # lan_push. Refuse rather than fall back to unpinned TLS.
        return '', 'dulwich_pool_manager_unsupported'
    except Exception as ex:
        if _looks_like_timeout(ex):
            return '', f'clone_timed_out: {ex!r}'
        return '', f'clone_failed: {ex!r}'
    finally:
        _PROGRESS['active'] = False
    lift_path = _find_lift_in(dest_dir)
    if not lift_path:
        return '', 'no_lift_in_clone'
    return lift_path, ''


def clone_from_peer(peer_id, langcode, incoming_url='',
                    incoming_vernlang=''):
    """Top-level entry point: LAN-clone *langcode* from *peer_id*.

    Returns a typed ``Result``. The Status codes the caller routes:

      - ``LAN_PROJECT_CLONED``: fresh clone landed.
      - ``LAN_PROJECT_REOPENED``: we already had it; bookkeeping
        only.
      - ``LAN_PROJECT_COLLISION_UNRELATED``: refuse — user must
        rename / remove first.
      - ``LAN_ADOPT_ORIGIN_NEEDED``: stashed as a pending decision;
        the result *also* carries one of the success codes above.
      - ``LAN_REMOTE_CONFLICT``: stashed as a pending decision;
        result carries the success code.
      - ``LAN_PROJECT_NOT_SHARED``: peer answered but refused to
        serve the repo (not shared with us / not registered there).
      - ``LAN_PEER_UNREACHABLE``: no endpoint resolved or clone
        connection failed.
      - ``SERVER_ERROR``: anything else.
    """
    result = Result()
    if not peer_id or not langcode:
        result.add(_S.SERVER_ERROR,
                   error='peer_id and langcode required')
        return result
    entry = _peers.get_peer(peer_id)
    if entry is None:
        result.add(_S.SERVER_ERROR, error='peer_unknown')
        return result
    expected_fp = entry.get('fp', '')
    endpoint = _resolve_endpoint(entry)
    if endpoint is None:
        result.add(_S.LAN_PEER_UNREACHABLE, peer_id=peer_id)
        return result
    host, port = endpoint
    url = f'https://{host}:{int(port)}/{langcode}.git'

    existing = _projects.get(langcode)
    if existing is not None:
        # Compare via ls-remote — cheap.
        refs = _peek_remote_refs(url, expected_fp)
        related = _shares_commits_with(
            refs or {}, existing.working_dir)
        if not related:
            result.add(_S.LAN_PROJECT_COLLISION_UNRELATED,
                       langcode=langcode)
            return result
        existing_url = str(getattr(existing, 'remote_url', '') or '')
        # Bookkeeping: record the LAN pair as a sharer of this
        # project both ways. add_shared_project is idempotent.
        try:
            _peers.add_shared_project(peer_id, langcode)
        except Exception:
            pass
        result.add(_S.LAN_PROJECT_REOPENED, langcode=langcode)
        # remote_url reconciliation, always-confirm rule.
        if incoming_url:
            if existing_url == incoming_url:
                pass
            elif not existing_url:
                _pending.add(_pending.KIND_ADOPT_ORIGIN, {
                    'peer_id': peer_id,
                    'device_name': entry.get('device_name', ''),
                    'langcode': langcode,
                    'url': incoming_url,
                })
                result.add(_S.LAN_ADOPT_ORIGIN_NEEDED,
                           langcode=langcode,
                           peer_id=peer_id,
                           device_name=entry.get('device_name', ''),
                           url=incoming_url)
            else:
                _pending.add(_pending.KIND_REMOTE_CONFLICT, {
                    'peer_id': peer_id,
                    'device_name': entry.get('device_name', ''),
                    'langcode': langcode,
                    'existing_url': existing_url,
                    'incoming_url': incoming_url,
                })
                result.add(_S.LAN_REMOTE_CONFLICT,
                           langcode=langcode,
                           peer_id=peer_id,
                           device_name=entry.get('device_name', ''),
                           existing_url=existing_url,
                           incoming_url=incoming_url)
        return result

    # No existing project: fresh LAN clone. Log start + outcome —
    # the transfer can run minutes behind a spinner, and until
    # 2026-07-17 the daemon log was silent for its whole duration
    # (an in-progress clone was indistinguishable from nothing
    # happening).
    dest_dir = _project_dest_dir(langcode)
    print(f'[lan-clone] start: {langcode!r} from {peer_id!r} '
          f'at {host}:{port}', file=sys.stderr, flush=True)
    lift_path, err = _do_lan_clone(
        host, port, langcode, expected_fp, dest_dir)
    if err:
        print(f'[lan-clone] failed: {langcode!r} from {peer_id!r}: '
              f'{err}', file=sys.stderr, flush=True)
        # Distinguish "connection stalled mid-transfer" from "could
        # not resolve / connect at all" so the UI can route the
        # right user-facing prompt. ``_do_lan_clone`` tags the
        # timeout case with the ``clone_timed_out:`` prefix.
        if err.startswith('clone_timed_out:'):
            result.add(_S.LAN_CLONE_TIMEOUT, peer_id=peer_id,
                       langcode=langcode,
                       timeout_s=_LAN_CLONE_TIMEOUT_S,
                       detail=err)
        elif _is_local_tls_error(err):
            result.add(_S.LAN_LOCAL_TLS_ERROR, peer_id=peer_id,
                       detail=err)
        elif _is_not_shared(err):
            result.add(_S.LAN_PROJECT_NOT_SHARED, peer_id=peer_id,
                       langcode=langcode, detail=err)
        else:
            result.add(_S.LAN_PEER_UNREACHABLE, peer_id=peer_id,
                       detail=err)
        return result
    # Strip the LAN listener URL from ``.git/config``'s origin.
    # ``_do_lan_clone`` runs dulwich's clone which sets origin to
    # ``https://<peer-host>:<peer-port>/<langcode>.git`` — a
    # private-IP URL that's useless as a persistent origin (peer
    # port changes per restart, and we don't fetch by URL after
    # the initial clone — fan-out uses live mDNS). Worse, the
    # publish-row "hide if remote_url present" gate treated this
    # as a github remote, so Publish never appeared and users
    # were stuck without a clear path to back up. 0.45.37.
    try:
        from . import repo as _repo
        # scope_to_paired_peers=False — we just cloned from a peer,
        # the origin URL is by construction a LAN listener URL, no
        # need to gate on the paired-peers list (which the peer is
        # likely already in via add_shared_project, but the check
        # is unnecessary here).
        _repo.strip_lan_origin_if_present(
            dest_dir, scope_to_paired_peers=False)
    except Exception as ex:
        print(f'[lan-clone] strip_lan_origin {dest_dir!r} failed: '
              f'{ex!r}', file=sys.stderr, flush=True)
    try:
        _projects.register(langcode, dest_dir,
                           lift_path=lift_path,
                           remote_url='')
        # Store the incoming vernlang so LIFT writers know which
        # language to tag — covers the multilingual case where the
        # project key (``MyEnglishProject``) doesn't match the
        # linguistic code (``en``). Empty incoming defers to the
        # ``effective_vernlang`` fallback (== langcode).
        if incoming_vernlang and incoming_vernlang != langcode:
            try:
                _projects.set_vernlang(langcode, incoming_vernlang)
            except Exception as ex:
                print(f'[lan-clone] set_vernlang failed: {ex!r}',
                      file=sys.stderr, flush=True)
    except Exception as ex:
        result.add(_S.SERVER_ERROR,
                   error=f'register_failed: {ex!r}')
        return result
    try:
        _peers.add_shared_project(peer_id, langcode)
    except Exception:
        pass
    print(f'[lan-clone] done: {langcode!r} from {peer_id!r} → '
          f'{lift_path!r}', file=sys.stderr, flush=True)
    result.add(_S.LAN_PROJECT_CLONED,
               langcode=langcode, peer_id=peer_id,
               device_name=entry.get('device_name', ''))
    # Always-confirm adopt-origin: stash a pending decision so the
    # user can opt into github sync after they see the project.
    if incoming_url:
        _pending.add(_pending.KIND_ADOPT_ORIGIN, {
            'peer_id': peer_id,
            'device_name': entry.get('device_name', ''),
            'langcode': langcode,
            'url': incoming_url,
        })
        result.add(_S.LAN_ADOPT_ORIGIN_NEEDED,
                   langcode=langcode,
                   peer_id=peer_id,
                   device_name=entry.get('device_name', ''),
                   url=incoming_url)
    return result
