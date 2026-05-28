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

import os
import shutil
import ssl
import sys

from . import lan_discovery as _lan_discovery
from . import pending_decisions as _pending
from . import peer_id as _peer_id
from . import peers as _peers
from . import projects as _projects
from . import status as _S
from .paths import azt_home
from .status import Result


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
    try:
        porcelain.clone(url, dest_dir, pool_manager=pm)
    except TypeError:
        # dulwich without pool_manager kwarg — same fallback as
        # lan_push. Refuse rather than fall back to unpinned TLS.
        return '', 'dulwich_pool_manager_unsupported'
    except Exception as ex:
        return '', f'clone_failed: {ex!r}'
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

    # No existing project: fresh LAN clone.
    dest_dir = _project_dest_dir(langcode)
    lift_path, err = _do_lan_clone(
        host, port, langcode, expected_fp, dest_dir)
    if err:
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
