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
    """Endpoint resolution order: mDNS → static → QR-hint. Returns the
    single best-guess ``(host, port)`` — kept for callers that only
    want one. The clone path uses ``_candidate_endpoints`` (try each)."""
    cands = _candidate_endpoints(peer_entry)
    return cands[0] if cands else None


def _candidate_endpoints(peer_entry):
    """EVERY plausible ``(host, port)`` for this peer, best-first: the
    live mDNS-resolved endpoint, then manual static_endpoints, then
    observed endpoints. Deduped. The clone/peek tries each until one
    connects — a peer advertises an address on every interface (wifi,
    USB-tether usb0, hotspot) and only some are routable from HERE.
    Field 2026-07-23: the phone had no route to the sender's wifi IP
    (192.168.31.60 → 'Network is unreachable') but a cable/10.x address
    was reachable; the single-endpoint resolver picked the dead one and
    every clone attempt failed, so the user re-accepted the offer six
    times. Trying each candidate fixes that."""
    out = []
    seen = set()

    def _add(host, port):
        try:
            key = (str(host), int(port))
        except (ValueError, TypeError):
            return
        if key[0] and key not in seen:
            seen.add(key)
            out.append(key)

    pid = peer_entry.get('peer_id', '')
    if pid:
        mdns = _lan_discovery.get_endpoint(pid)
        if mdns is not None:
            _add(mdns[0], mdns[1])
    for source in ('static_endpoints', 'endpoints'):
        for raw in (peer_entry.get(source) or []):
            try:
                host, port = str(raw).rsplit(':', 1)
                _add(host, int(port))
            except (ValueError, TypeError):
                continue
    return out


def _build_pool_manager(expected_fp):
    """TLS-pinned urllib3 PoolManager for talking to a paired peer's
    LAN listener. Same shape as ``lan_push._build_ssl_context`` —
    keeps the two modules aligned without sharing state."""
    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        raise RuntimeError('this daemon has no LAN identity')
    # Refuse to build an UNPINNED pool. urllib3's ``assert_fingerprint``
    # is skipped when falsy — an empty fp (corrupted / partial
    # peers.json entry) would otherwise connect with NO identity check
    # at all, since CA validation is deliberately off in this design
    # (identity = fingerprint pin, not chain). 0.54.64.
    if not expected_fp:
        raise RuntimeError(
            'peer has no recorded TLS fingerprint — refusing '
            'unpinned connection')
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
    # Silence urllib3's InsecureRequestWarning for these pools: it
    # fires because we skip CA-chain validation, but peer identity IS
    # verified — by SHA-256 fingerprint pin (``assert_fingerprint``
    # below) against peers.json, per the LAN trust design. The warning
    # alarmed a log reader in the field (2026-07-24) over a connection
    # that is in fact TLS-encrypted and pinned. Verified-HTTPS paths
    # (github, CAWL) never emit this warning, so nothing real is
    # masked.
    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    return urllib3.PoolManager(
        ssl_context=ctx,
        assert_hostname=False,
        assert_fingerprint=expected_fp,
        # See ``lan_push._push_to_peer`` for why this is needed:
        # urllib3 overwrites our context's verify_mode with
        # ``resolve_cert_reqs(cert_reqs)`` — passing 'CERT_NONE'
        # explicitly preserves the unverified behavior.
        cert_reqs='CERT_NONE',
        # ONE attempt per address (0.55.4). urllib3's default retries
        # three times, and the clone path already walks every candidate
        # endpoint — so an unreachable peer cost candidates × 3 × the
        # 5 s connect timeout, minutes of a progress popup for a
        # verdict that was settled on the first refusal (field
        # 2026-07-27, the `Retrying (Retry(total=2…))` triplets). The
        # retry that matters here is the NEXT ADDRESS, not the same
        # dead one again.
        retries=urllib3.Retry(total=0, connect=0, read=0,
                              redirect=0, status=0),
    )


def _no_shared_subnet(candidates):
    """True when NONE of *candidates* is on a subnet this device holds
    an address on (0.55.8).

    Diagnostic only — never a gate. A routed network can legitimately
    be reachable off-subnet, so we still dial every candidate; this
    just explains the failure afterwards, in the terms the user can
    act on: "that computer is on a network this device isn't joined
    to" rather than a bare timeout.

    Field 2026-07-27, an hour of confusion: a computer correctly
    reported `Listening on 10.191.129.91:55870` — an address on a
    phone-hotspot subnet — while this phone had moved to
    `192.168.31.x`. Nothing was stale or lying; the two were on
    different networks. Hotspot subnets make this the NORMAL case
    rather than an edge one: the hotspot exists only while its host
    shares it, everyone on it gets a 10.x lease, and the moment a
    device joins ordinary wifi instead it silently loses every peer
    that stayed behind. Yet the symptom is a bare "unreachable peer",
    indistinguishable from a peer that is asleep, wedged, or holding a
    bad address. /24 comparison is a heuristic, which is exactly why
    this annotates rather than decides."""
    try:
        from . import lan_listener as _lan_listener
        mine = set()
        for ip in (_lan_listener._interface_ipv4s() or []):
            if '.' in ip:
                mine.add(ip.rsplit('.', 1)[0])
        if not mine:
            return False        # we know nothing; claim nothing
        for host, _port in candidates:
            host = str(host or '')
            if '.' in host and host.rsplit('.', 1)[0] in mine:
                return False
        return True
    except Exception:
        return False


def _peek_remote_refs(url, expected_fp):
    """``ls-remote`` against a LAN URL. Returns a dict
    ``{ref_name: sha_hex}`` or ``None`` on any failure.

    **None means "couldn't ask", NOT "no overlap"** (corrected
    0.55.3). The old docstring claimed treating None as no-overlap was
    safe "because the worst that does is refuse a collision we
    couldn't confirm was related" — but the caller's refusal text tells
    the user a DIFFERENT project of that name exists and invites them
    to rename or remove it. On an unreachable peer that advised
    deleting data on the strength of a network timeout. Callers must
    branch on None before judging relatedness."""
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
        # Dump the full traceback + any filename the OSError carries.
        # A bare FileNotFoundError(2) here (field 2026-07-23) gives no
        # clue which file is missing; the greedy _is_local_tls_error
        # then mislabels it. The traceback names the exact path so the
        # real cause is fixable instead of guessed.
        import traceback as _tb
        _fn = getattr(ex, 'filename', None)
        print(f'[lan-clone] clone raised for {langcode!r} '
              f'(file={_fn!r}):\n{_tb.format_exc()}',
              file=sys.stderr, flush=True)
        return '', (f'clone_failed: {ex!r}'
                    + (f' file={_fn!r}' if _fn else ''))
    finally:
        _PROGRESS['active'] = False
    lift_path = _find_lift_in(dest_dir)
    if not lift_path:
        return '', 'no_lift_in_clone'
    return lift_path, ''


def retry_affirmed_offers(peer_id):
    """Auto-complete any share-offers this peer already affirmed.

    Called on peer arrival (mDNS transition absent → present). Walks
    the pending decisions, filters to affirmed share-offers from
    *peer_id*, and re-runs the LAN clone for each. A delivered clone
    (CLONED / REOPENED) removes the decision; a failure leaves it
    pending for the next arrival. Never raises — a peer arrival must
    not be derailed by a bookkeeping fault."""
    try:
        from . import pending_decisions as _pd
        offers = []
        for d in _pd.list_all():
            params = d.get('params') or {}
            if (d.get('kind') == _pd.KIND_SHARE_OFFER
                    and params.get('peer_id') == peer_id
                    and params.get('affirmed')):
                offers.append(d)
        if not offers:
            return
        for d in offers:
            decision_id = d.get('id', '')
            params = d.get('params') or {}
            langcode = str(params.get('langcode', '') or '')
            try:
                result = clone_from_peer(
                    peer_id, langcode,
                    incoming_url=str(params.get('repo_url', '') or ''),
                    incoming_vernlang=str(params.get('vernlang', '')
                                          or ''))
            except Exception as ex:
                print(f'[lan-clone] retry affirmed offer {langcode!r} '
                      f'from {peer_id[:8]!r} raised: {ex!r}',
                      file=sys.stderr, flush=True)
                continue
            if result.has_any(_S.LAN_PROJECT_CLONED,
                              _S.LAN_PROJECT_REOPENED):
                _pd.remove(decision_id)
                print(f'[lan-clone] affirmed offer {langcode!r} from '
                      f'{peer_id[:8]!r} auto-completed on arrival',
                      file=sys.stderr, flush=True)
            else:
                print(f'[lan-clone] affirmed offer {langcode!r} from '
                      f'{peer_id[:8]!r} still pending '
                      f'(codes={result.codes()!r})',
                      file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[lan-clone] retry_affirmed_offers {peer_id[:8]!r} '
              f'raised: {ex!r}', file=sys.stderr, flush=True)


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
    candidates = _candidate_endpoints(entry)
    if not candidates:
        result.add(_S.LAN_PEER_UNREACHABLE, peer_id=peer_id)
        return result

    existing = _projects.get(langcode)
    if existing is not None:
        # SAME REMOTE ⇒ same repo, no network needed (0.55.9).
        # ``_handle_share_offer`` already treats a matching remote_url
        # as proof of the same project ("remote_url matches local (same
        # repo, different spelling); no-op"), comparing wan-normalized
        # so ssh and https spellings of one repo count as equal
        # (0.54.11). The clone path asked the SAME question over the
        # network instead — an ls-remote peek purely to decide
        # relatedness — and every failure of that peek (timeout, 404,
        # transient registry read on the owner) blocked an answer we
        # already had locally. Field 2026-07-27: a whole evening of QR
        # scans for a project this device already held, from the very
        # repo the QR named, each one grinding through peeks to
        # rediscover it.
        incoming = str(incoming_url or '').strip()
        local_url = str(getattr(existing, 'remote_url', '') or '').strip()
        if incoming and local_url:
            try:
                from . import repo as _repo_mod
                same = (_repo_mod.wan_url(local_url)
                        == _repo_mod.wan_url(incoming))
            except Exception:
                same = (local_url == incoming)
            if same:
                print(f'[lan-clone] {langcode!r}: already here and '
                      f'remote_url matches the offer ({local_url!r}) '
                      f'— same repo, nothing to copy; skipping the '
                      f'relatedness peek',
                      file=sys.stderr, flush=True)
                try:
                    _peers.add_shared_project(peer_id, langcode)
                except Exception:
                    pass
                result.add(_S.LAN_PROJECT_REOPENED, langcode=langcode)
                return result
        # Compare via ls-remote — cheap. Try each candidate address
        # until one answers (the peer may only be routable on some).
        refs = None
        peeked = set()
        for host, port in candidates:
            peeked.add((host, int(port)))
            url = f'https://{host}:{int(port)}/{langcode}.git'
            refs = _peek_remote_refs(url, expected_fp)
            if refs is not None:
                break
        if refs is None:
            # Same re-resolve as the clone loop below (0.55.6): an
            # address the peer proved while we were dialing the stale
            # one is worth a try before we give a verdict.
            for host, port in _candidate_endpoints(
                    _peers.get_peer(peer_id) or entry):
                if (host, int(port)) in peeked:
                    continue
                url = f'https://{host}:{int(port)}/{langcode}.git'
                refs = _peek_remote_refs(url, expected_fp)
                if refs is not None:
                    break
        if refs is None:
            # WE NEVER REACHED THEM — say that, and nothing more
            # (0.55.3). ``_peek_remote_refs`` returns None on any
            # failure, and falling through to the comparison below
            # turned "couldn't ask" into "no shared commits" into
            # ``LAN_PROJECT_COLLISION_UNRELATED`` — whose UI text tells
            # the user a DIFFERENT project of this name exists and
            # invites them to rename or REMOVE it. Field 2026-07-27:
            # every candidate failed with `Network is unreachable` and
            # the user was advised to delete a project on the strength
            # of that. Unknown is not unrelated, and a verdict that can
            # cost data must never be reached by a network timeout.
            print(f'[lan-clone] {langcode!r}: could not peek any of '
                  f'{len(candidates)} candidate address(es) — '
                  f'refusing to judge whether the local project is '
                  f'related', file=sys.stderr, flush=True)
            if _no_shared_subnet(candidates):
                print(f'[lan-clone] {langcode!r}: none of the peer\'s '
                      f'addresses is on a network this device has an '
                      f'address on — different wifi / hotspot',
                      file=sys.stderr, flush=True)
                result.add(_S.LAN_PEER_OTHER_NETWORK, peer_id=peer_id,
                           langcode=langcode)
            else:
                result.add(_S.LAN_PEER_UNREACHABLE, peer_id=peer_id,
                           langcode=langcode)
            return result
        related = _shares_commits_with(refs, existing.working_dir)
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
    # Try each candidate address until one connects — a peer reachable
    # over the cable/10.x but not its wifi IP (or vice versa) still
    # clones instead of dead-ending on the first address.
    lift_path, err = '', 'no reachable endpoint'
    tried = set()
    for host, port in candidates:
        tried.add((host, int(port)))
        print(f'[lan-clone] start: {langcode!r} from {peer_id!r} '
              f'at {host}:{port}', file=sys.stderr, flush=True)
        lift_path, err = _do_lan_clone(
            host, port, langcode, expected_fp, dest_dir)
        if not err:
            break
        print(f'[lan-clone] {langcode!r} from {peer_id!r}: '
              f'{host}:{port} failed ({err}) — trying next candidate '
              f'if any', file=sys.stderr, flush=True)
    if err:
        # RE-RESOLVE, then try anything NEW (0.55.6). The candidate
        # list was snapshotted before the first dial, and the peer may
        # have reached US in the meantime — which promotes their proven
        # address into peers.json (0.54.99) after our snapshot was
        # taken. Field 2026-07-27: the phone timed out dialing a stale
        # QR address (10.191.129.91:55870) at :32.9, and the desktop
        # reached it from its real address (192.168.31.60) at :34.8 —
        # two seconds too late to be in the list, so a clone failed
        # with a working address sitting in the registry.
        fresh = [c for c in _candidate_endpoints(
            _peers.get_peer(peer_id) or entry)
            if (c[0], int(c[1])) not in tried]
        for host, port in fresh:
            print(f'[lan-clone] retry on newly-learned address '
                  f'{host}:{port} for {langcode!r}',
                  file=sys.stderr, flush=True)
            lift_path, err = _do_lan_clone(
                host, port, langcode, expected_fp, dest_dir)
            if not err:
                break
    if err:
        print(f'[lan-clone] failed: {langcode!r} from {peer_id!r} '
              f'(all {len(candidates)} candidate(s)): {err}',
              file=sys.stderr, flush=True)
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
    # Record the peer's main as observed = the tip we just cloned, so
    # the sync board shows "up to date" for this peer instead of
    # "awaiting first sync" the instant after a clone (field 2026-07-23:
    # a freshly-cloned project read "awaiting first sync", which is
    # nonsense — we literally just got their data). We DID observe their
    # main (== our new HEAD) during the clone, so this is honest
    # coverage, not a fabricated one.
    try:
        from dulwich.repo import Repo
        _head = Repo(dest_dir).head().decode('ascii', 'replace')
        if _head:
            _peers.set_peer_last_seen_main(peer_id, langcode, _head)
    except Exception as ex:
        print(f'[lan-clone] post-clone coverage record failed: '
              f'{ex!r}', file=sys.stderr, flush=True)
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
