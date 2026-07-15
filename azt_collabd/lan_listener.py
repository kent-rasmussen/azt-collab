"""
LAN sync HTTPS listener (parked design in
``docs/local_lan_sync_stub.md``, phase 4).

When the daemon-wide ``lan.allow_sync`` toggle is on, ``apply_toggle()``
spins up a threaded ``dulwich.web``-backed HTTPS server bound to
``0.0.0.0:0`` (OS-assigned port). When it's off, ``apply_toggle()``
tears the server down. Hot-applied — flipping the toggle does NOT
require a daemon restart, per ``feedback_hot_toggle_not_restart``.

Auth model (per the parked spec):

  - TLS server cert is the daemon's per-device ``peer.crt`` (loaded
    from ``azt_collabd.peer_id``).
  - Client cert is *required* (``ctx.verify_mode = CERT_REQUIRED``)
    but its CA chain is *not* validated — we pin per-peer via the
    paired-peers list. The ``set_verify`` callback accepts every
    cert; the WSGI middleware then extracts the ed25519 pubkey
    from the DER and looks it up in ``peers.json``. Unknown peer →
    403. Known peer + fingerprint mismatch → 403 (logged so the
    user can see something's off).

Listener body uses ``dulwich.web.HTTPGitApplication`` wrapped in the
standard ``GunzipFilter`` + ``LimitedInputFilter`` chain via
``make_wsgi_chain``. The ``DictBackend`` exposes one ``/{lang}.git``
path per project that's shared with at least one peer; per-request,
the middleware further filters that set down to projects shared
with the specific peer-id making the request.

Concurrency: ``ThreadingMixIn`` so two paired phones in the same
room can fetch simultaneously. Per-project write serialization
comes from the existing ``azt_collabd.locks.project_lock`` flock,
which receive-pack callers acquire at the daemon entry point.

Lifetime: the listener thread is a daemon thread and stops cleanly
on ``stop()``. On Android, the parent ``:provider`` service runs
``startForeground(specialUse)`` while the listener is up — that
plumbing lives in ``azt_collabd.android_cp.service`` (phase 4
Android-side, not yet wired) and is a no-op on desktop.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
import time as _time

from . import lan_discovery as _lan_discovery
from . import paths as _paths
from . import peer_id as _peer_id
from . import peers as _peers
from . import projects as _projects
from . import settings as _settings
from . import store as _store


_LOCK = threading.Lock()
_STATE = {
    'server': None,
    'thread': None,
    'bound': None,  # (host, port) once running
}


# In-memory tracker for "user just displayed a QR offering this
# langcode" gestures. Indexed by langcode → unix timestamp of the
# most-recent display. ``_handle_hello_bodyauth`` consults this
# before auto-sharing a langcode that arrives in an unpaired peer's
# hello.
#
# Why: without this gate, an attacker on the LAN can POST
# ``/v1/lan/hello`` with peer_id=any, fp=any, langcode=X and our
# daemon would (a) record them as paired and (b) add X to their
# shared_projects allowlist — at which point the dulwich smart-
# protocol handler accepts ``GET /X.git/info/refs`` from them and
# they can exfiltrate the project. The CERT_NONE TLS design
# intentionally can't pin client certs (stdlib ssl limitation, see
# ``_build_server``), so the only binding we have is "the user
# gestured by showing a QR for X within the last few minutes."
# The QR display is the user-consent signal; if no recent QR for
# this langcode exists, the hello records the pair but refuses
# auto-share.
#
# Validity is driven by the QR actually being ON SCREEN, not a blind
# timer (0.52.26). The share-QR popup heartbeats ``record_qr_offered``
# every ~10 s while displayed and calls ``clear_qr_offer`` when it
# closes. So this keepalive window only has to outlast one heartbeat
# interval — it is NOT a guess at "how long the user might keep the QR
# up" (the old 10-minute TTL). Consequences: a display that's closed
# (or whose app is killed / backgrounded) self-expires within seconds
# instead of staying armed for 10 minutes, and a QR the user
# deliberately keeps up stays valid for as long as it's shown.
_QR_OFFER_KEEPALIVE_S = 30.0
_pending_qr_offers = {}   # langcode (str) → last-heartbeat unix ts


def record_qr_offered(langcode):
    """Heartbeat: the share QR for *langcode* is currently displayed.
    Called on QR open and every ~10 s while it stays up. Consulted by
    the hello handler to gate auto-share. Empty langcode is a no-op
    (pair-only QR, no project share)."""
    if not langcode:
        return
    _pending_qr_offers[str(langcode)] = _time.time()


def qr_offer_active(langcode):
    """True if a share QR for *langcode* is currently being displayed
    (a heartbeat landed within ``_QR_OFFER_KEEPALIVE_S``). **Multi-use**
    — does NOT consume the offer, so one displayed QR can share to
    several peers who scan it (the workshop "show it to the room" case).
    The offer is revoked by ``clear_qr_offer`` (screen closed) or by the
    heartbeat lapsing (display gone). This "valid while shown" model
    replaced the single-use + 10-minute TTL in 0.52.26."""
    if not langcode:
        return False
    key = str(langcode)
    ts = _pending_qr_offers.get(key)
    if ts is None:
        return False
    if _time.time() - ts > _QR_OFFER_KEEPALIVE_S:
        _pending_qr_offers.pop(key, None)
        return False
    return True


def clear_qr_offer(langcode):
    """Revoke a share offer immediately — called when the QR popup
    closes. No-op if none pending / empty langcode."""
    if langcode:
        _pending_qr_offers.pop(str(langcode), None)


def is_running():
    with _LOCK:
        return _STATE['server'] is not None


def bound_endpoint():
    """Return ``(host, port)`` if running, else ``None``. Host is
    the daemon's outward-facing LAN IP (best-effort); fall back to
    ``0.0.0.0`` and let the caller substitute the discovered IP."""
    with _LOCK:
        return _STATE['bound']


class _DynamicBackend:
    """dulwich Backend that resolves ``open_repository`` against
    the *current* state of ``projects.json`` and ``peers.json`` —
    not a snapshot taken at listener-start. New share_offer arrivals
    (which mutate ``shared_projects``) immediately show up in the
    serving set without a listener restart; rolled-back shares
    immediately stop serving. Tradeoff is one ``peers.json`` read
    per request; the file is tiny and cached at the OS level so
    the cost is negligible vs the network/git work that follows.

    **fd hygiene (0.54.1).** dulwich's web handlers never close the
    Repo the backend hands them, and a Repo holds pack/index fds
    that GC does not reliably release (reference cycles). Every
    phone poll therefore leaked fds until the 2026-07-10 EMFILE
    incident wedged the whole daemon. Each Repo opened here is
    recorded thread-locally; ``_repo_closing_middleware`` closes
    them when the WSGI response for that request finishes (same
    thread — the server is thread-per-request).
    """

    def __init__(self):
        self._thread_repos = threading.local()

    def _track(self, repo):
        lst = getattr(self._thread_repos, 'repos', None)
        if lst is None:
            lst = self._thread_repos.repos = []
        lst.append(repo)
        return repo

    def close_thread_repos(self):
        lst = getattr(self._thread_repos, 'repos', None)
        if not lst:
            return
        self._thread_repos.repos = []
        for r in lst:
            try:
                r.close()
            except Exception:
                pass

    def open_repository(self, path):
        from dulwich.errors import NotGitRepository
        from dulwich.repo import Repo
        # ``path`` shape varies across dulwich call sites in two
        # axes:
        #   - encoding: the GET ``/info/refs`` handler in
        #     ``dulwich.web`` passes str (sliced from the URL
        #     string); the smart-protocol POST handler in
        #     ``dulwich.server`` (UploadPackHandler /
        #     ReceivePackHandler init) passes bytes from the
        #     wire-protocol parser.
        #   - shape: some sites pass the repo prefix
        #     (``/baf.git`` or ``baf.git``), others pass the full
        #     URL path (``/baf.git/info/refs``).
        # Pre-0.45.28 we only handled str; the POST path raised
        # ``TypeError: a bytes-like object is required, not 'str'``
        # at the ``lstrip('/')`` below, dulwich returned 500, and
        # the pusher logged ``[lan-merge] fetch from '<peer>'
        # failed: GitProtocolError('unexpected http resp 500 ...')``.
        raw = path or ''
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        norm = raw.lstrip('/')
        if '.git' in norm:
            langcode = norm.split('.git', 1)[0]
        else:
            langcode = norm.split('/', 1)[0]
        print(f'[lan-listener] open_repository: raw={raw!r} → '
              f'langcode={langcode!r}',
              file=sys.stderr, flush=True)
        # Gate: project must appear in at least one paired peer's
        # shared_projects allowlist. This IS the access control on
        # the listener (TLS layer is CERT_NONE since stdlib ssl
        # can't pin self-signed client certs). Future-harden with
        # signed-message body auth to gate per-peer rather than
        # union-of-all-peers.
        try:
            peers_list = _peers.list_peers(strict=True)
        except Exception as ex:
            # Transient registry-read failure (fd exhaustion, EIO):
            # do NOT read as "empty allowlist / nothing shared" —
            # that silently unshared every project during the
            # 2026-07-10 EMFILE incident. Refuse THIS request as
            # transient; the peer retries on its next pass.
            print(f'[lan-listener] defer {langcode!r}: peer '
                  f'registry unreadable ({ex!r}) — transient, '
                  f'NOT treating as unshared',
                  file=sys.stderr, flush=True)
            raise NotGitRepository(
                'peer registry unreadable (transient)') from ex
        shared_anywhere = set()
        for peer in peers_list:
            shared_anywhere.update(peer.get('shared_projects') or [])
        if langcode not in shared_anywhere:
            print(f'[lan-listener] reject {langcode!r}: not in any '
                  f'peer\'s shared_projects '
                  f'(shared_anywhere={sorted(shared_anywhere)!r})',
                  file=sys.stderr, flush=True)
            raise NotGitRepository(
                f'project {langcode!r} is not shared with any peer')
        project = _projects.get(langcode)
        if project is None or not project.working_dir:
            print(f'[lan-listener] reject {langcode!r}: not '
                  f'registered (project={project!r})',
                  file=sys.stderr, flush=True)
            raise NotGitRepository(
                f'project {langcode!r} not registered')
        try:
            return self._track(Repo(project.working_dir))
        except Exception as ex:
            print(f'[lan-listener] open repo {langcode!r} failed: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            raise NotGitRepository(
                f'project {langcode!r} repo failed to open') from ex


def _build_dict_backend():
    """Return the dynamic backend. Kept under the old name so the
    rest of ``_build_server`` reads identically; switched from a
    static ``DictBackend(mapping)`` (snapshot at listener-start)
    to ``_DynamicBackend`` (re-reads ``peers.json`` on each
    request) so new share_offer arrivals work without a listener
    restart."""
    return _DynamicBackend()


class _ClosingBody:
    """WSGI response wrapper: when the server finishes with the
    response (PEP 3333 guarantees a ``close()`` call), close every
    dulwich Repo the backend opened for this request's thread."""

    def __init__(self, inner, backend):
        self._inner = inner
        self._backend = backend

    def __iter__(self):
        return iter(self._inner)

    def close(self):
        try:
            if hasattr(self._inner, 'close'):
                self._inner.close()
        finally:
            self._backend.close_thread_repos()


def _repo_closing_middleware(app, backend):
    """Outermost WSGI layer: pair every request with a
    ``close_thread_repos()`` — on the happy path via
    ``_ClosingBody.close()`` after the response is fully sent, on
    the raise path immediately. See ``_DynamicBackend`` docstring
    for the fd-leak incident this fixes."""
    def _app(environ, start_response):
        try:
            body = app(environ, start_response)
        except Exception:
            backend.close_thread_repos()
            raise
        return _ClosingBody(body, backend)
    return _app


def _peer_id_from_cert_der(cert_der):
    """Extract the lowercase hex ed25519 pubkey from a DER-encoded
    X.509 cert. Returns '' if the cert's public key isn't ed25519
    or the parse fails — the middleware then rejects the request."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        return ''
    try:
        try:
            from cryptography.hazmat.backends import default_backend
            cert = x509.load_der_x509_certificate(
                cert_der, backend=default_backend())
        except TypeError:
            # Newer cryptography: no backend kwarg.
            cert = x509.load_der_x509_certificate(cert_der)
        pub = cert.public_key()
    except Exception:
        return ''
    if not isinstance(pub, ed25519.Ed25519PublicKey):
        return ''
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def _cert_fp_from_der(cert_der):
    import hashlib
    return hashlib.sha256(cert_der).hexdigest()


def _handle_hello_bodyauth(environ, start_response):
    """Body-auth variant of the hello handler (TLS client cert
    validation deliberately disabled — see ``_build_server`` for
    why). Reads the peer's identity from the request body and
    trusts it. The body's ``peer_id`` IS the peer's ed25519
    pubkey; a future-hardening pass should add a signature so
    we can cryptographically verify the body really came from
    the holder of that private key.

    Body: ``{peer_id, fp, device_name, langcode?, endpoint?}``.
    Response: ``{ok: True, peer_id}`` on success."""
    import json as _json
    try:
        n = int(environ.get('CONTENT_LENGTH', '0') or '0')
        if n > 0:
            raw = environ['wsgi.input'].read(n)
        else:
            raw = b''
        payload = _json.loads(raw.decode('utf-8') or '{}')
    except Exception as ex:
        start_response('400 Bad Request',
                       [('Content-Type', 'text/plain')])
        return [f'invalid body: {ex!r}\n'.encode('utf-8')]
    if not isinstance(payload, dict):
        start_response('400 Bad Request',
                       [('Content-Type', 'text/plain')])
        return [b'body must be an object\n']
    actual_peer_id = str(payload.get('peer_id', '') or '')
    actual_fp = str(payload.get('fp', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    if len(actual_peer_id) != 64 or len(actual_fp) != 64:
        start_response('400 Bad Request',
                       [('Content-Type', 'text/plain')])
        return [b'peer_id / fp wrong length\n']
    # Capture the sender's listener endpoint so future LAN fan-out
    # has somewhere to push to. Without this, our peers.json entry
    # for them holds ``endpoints=[]`` and ``_resolve_endpoint``
    # gives nothing back, silently skipping the fan-out
    # (``no endpoint for <peer_id>``). Empty incoming endpoint =
    # pre-fix sender, falls back to the legacy no-endpoint record.
    incoming_endpoint = str(payload.get('endpoint', '') or '')
    _peers.record_pair(actual_peer_id, actual_fp,
                       device_name, incoming_endpoint)
    # Symmetric auto-share: if the hello carried a langcode (the
    # project the scanner just LAN-cloned FROM us), add it to our
    # shared_projects allowlist for them too. Saves the owner a
    # second tap on Share after the QR scan; the underlying share
    # was the QR-show gesture itself.
    #
    # SECURITY: only fire if the user is actively DISPLAYING a QR
    # offering this langcode right now (``qr_offer_active`` — a
    # heartbeat within the keepalive window). Without this gate, anyone
    # on the
    # LAN can POST ``/v1/lan/hello`` claiming any langcode and we
    # would auto-grant them read access to that project (the git
    # smart-protocol handler accepts requests for any project in
    # the union of all paired peers' shared_projects). The QR-
    # display gesture is the user-consent signal that pins
    # langcode auto-share to a real intent. If no recent QR for
    # this langcode is on file, we still record the pair (the
    # caller went out of their way to claim an identity) but
    # refuse the auto-share — the user can still tap Share
    # manually if they meant to allow this peer.
    langcode_offered = str(payload.get('langcode', '') or '')
    if langcode_offered:
        if qr_offer_active(langcode_offered):
            try:
                _peers.add_shared_project(
                    actual_peer_id, langcode_offered)
            except Exception as ex:
                print(f'[lan-listener] hello auto-share for '
                      f'{langcode_offered!r} raised: {ex!r}',
                      file=sys.stderr, flush=True)
        else:
            print(f'[lan-listener] hello from {actual_peer_id[:8]!r} '
                  f'claimed langcode={langcode_offered!r} but no '
                  f'recent QR offer for it; pair recorded, '
                  f'auto-share refused',
                  file=sys.stderr, flush=True)
    resp = _json.dumps({'ok': True, 'peer_id': actual_peer_id})
    body_bytes = resp.encode('utf-8')
    start_response('200 OK', [
        ('Content-Type', 'application/json'),
        ('Content-Length', str(len(body_bytes))),
    ])
    print(f'[lan-listener] hello: recorded {actual_peer_id[:8]!r} '
          f'({device_name!r})', file=sys.stderr, flush=True)
    return [body_bytes]


def _read_json_body(environ):
    """Read + parse the JSON body from a WSGI environ. Returns
    ``(payload_dict_or_None, error_msg)``."""
    import json as _json
    try:
        n = int(environ.get('CONTENT_LENGTH', '0') or '0')
        if n > 0:
            raw = environ['wsgi.input'].read(n)
        else:
            raw = b''
        payload = _json.loads(raw.decode('utf-8') or '{}')
    except Exception as ex:
        return None, f'invalid body: {ex!r}'
    if not isinstance(payload, dict):
        return None, 'body must be an object'
    return payload, ''


def _json_response(start_response, status_line, body_dict):
    import json as _json
    body_bytes = _json.dumps(body_dict).encode('utf-8')
    start_response(status_line, [
        ('Content-Type', 'application/json'),
        ('Content-Length', str(len(body_bytes))),
    ])
    return [body_bytes]


def _handle_share_offer_bodyauth(environ, start_response):
    """Body-auth variant of share_offer (TLS client auth disabled,
    see ``_build_server``). Reads the sender's ``peer_id`` from
    the request body and trusts it. Same body shape as before,
    just no cert cross-check."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    return _handle_share_offer(environ, start_response, peer_id,
                               prepared_payload=payload)


def _handle_share_declined_bodyauth(environ, start_response):
    """Body-auth variant of share_declined. Same as the share_offer
    body-auth wrapper — peer_id from body, no cert cross-check."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    return _handle_share_declined(environ, start_response, peer_id,
                                  prepared_payload=payload)


def _handle_share_unshared_bodyauth(environ, start_response):
    """Body-auth variant of share_unshared (0.50.44).

    Symmetric-unshare endpoint: phone A's user-tap "unshare X with
    B" gesture POSTs here on B's listener so B can drop A from B's
    own ``shared_projects`` allowlist for X. Without this, A's
    unshare only affected A's outbound fan-out; B's outbound fan-
    out kept firing to A (which A then no-op'd with a logged
    ``carries no repo_url; no-op (already have project)`` line).
    Symmetric unshare closes the asymmetry.

    Distinct from ``share_declined`` (which means "I'm declining
    the offer you just made") — same wire pattern, different
    semantics, separate code so future divergence is cheap.
    """
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    return _handle_share_unshared(environ, start_response, peer_id,
                                  prepared_payload=payload)


def _handle_share_offer(environ, start_response, peer_id,
                       prepared_payload=None):
    """Inbound share-offer handler. Dispatches by local state:

    - **Project not registered locally**: this is the original
      "want to clone from this peer?" path — stash
      ``KIND_SHARE_OFFER``; the user accepts via the decisions
      UI and the LAN clone follows.
    - **Project registered, local ``remote_url`` empty, incoming
      non-empty**: peer is telling us where its github origin
      lives. Stash ``KIND_ADOPT_ORIGIN`` so the user can opt into
      pushing to the same upstream (and so a future peer Publish
      adopts this URL instead of inventing a duplicate). Since
      0.50.27.
    - **Project registered, URLs match**: steady-state ping
      after every peer publishes / shares. Log + no-op so the
      user doesn't see repeated decisions for an already-known
      fact.
    - **Project registered, URLs differ**: fork case — stash
      ``KIND_REMOTE_CONFLICT`` so the user picks via
      ``_h_lan_resolve_conflict``. Since 0.50.27.
    - **Project registered, incoming ``repo_url`` empty**: peer
      doesn't know any URL either; nothing to learn. Log + no-op.

    Pre-0.50.27 behaviour was "always stash ``KIND_SHARE_OFFER``"
    regardless of local state, which double-decisioned every
    already-known share and missed the URL-conflict signal entirely.
    """
    from . import pending_decisions as _pending
    from . import projects as _projects
    if prepared_payload is not None:
        payload = prepared_payload
    else:
        payload, err = _read_json_body(environ)
        if payload is None:
            return _json_response(start_response, '400 Bad Request',
                                  {'ok': False, 'error': err})
    langcode = str(payload.get('langcode', '') or '')
    repo_url = str(payload.get('repo_url', '') or '')
    vernlang = str(payload.get('vernlang', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    if not langcode:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'langcode required'})
    try:
        local_proj = _projects.get(langcode)
    except Exception:
        local_proj = None
    local_url = ''
    if local_proj is not None:
        local_url = str(getattr(local_proj, 'remote_url', '') or '')
    # ``dispatch`` is echoed back in the JSON response so the
    # *sender* can show meaningful UI feedback. Otherwise every
    # outcome — known-already, freshly-stashed, conflict — looks
    # identical to the sender as a generic 200 OK. Field added in
    # 0.50.43 (additive — older senders ignore it).
    if local_proj is None:
        _pending.add(_pending.KIND_SHARE_OFFER, {
            'peer_id': peer_id,
            'device_name': device_name,
            'langcode': langcode,
            'repo_url': repo_url,
            'vernlang': vernlang,
        })
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r} stashed (clone-offer)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True,
                               'dispatch': 'stashed_share'})
    if not repo_url:
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r} carries no repo_url; no-op '
              '(already have project)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True, 'dispatch': 'no_url'})
    if local_url == repo_url:
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r}: remote_url matches local; no-op',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True, 'dispatch': 'noop'})
    if not local_url:
        # Auto-accept adopt-origin (0.50.58). The peer has the
        # project locally but no remote_url; the incoming offer
        # carries one. Pre-0.50.58 this stashed a
        # KIND_ADOPT_ORIGIN pending decision and waited for the
        # user to tap "accept" in the picker — which created
        # friction for the unambiguous case (project content
        # already shared via LAN, peer is just supplying the
        # github URL we don't have yet). User already consented
        # to the share by pairing the peer; receiving the URL is
        # the natural completion. Apply synchronously and report
        # ``dispatch='adopted'`` to the sender.
        #
        # KIND_REMOTE_CONFLICT (URLs differ) stays a pending
        # decision because that case is genuinely ambiguous —
        # the daemon can't tell which github repo is "canonical"
        # so the user must pick (keep_mine / use_theirs /
        # dual_publish).
        adopted = False
        try:
            _projects.set_remote_url(langcode, repo_url)
            wd = str(getattr(local_proj, 'working_dir', '') or '')
            if wd:
                from . import repo as _repo
                _repo.set_remote_origin_url(wd, repo_url)
            adopted = True
        except Exception as ex:
            print(f'[lan-listener] auto-adopt-origin for '
                  f'{langcode!r} (peer={peer_id[:8]!r} '
                  f'url={repo_url!r}) failed: {ex!r}',
                  file=sys.stderr, flush=True)
        if adopted:
            print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
                  f'for {langcode!r} auto-adopted origin '
                  f'{repo_url!r}',
                  file=sys.stderr, flush=True)
            # Push-notify any peer observing this project's status
            # URI so the settings UI re-polls and picks up the new
            # ``remote_url`` immediately. Without this the picker
            # holds its cached "publish candidate: remote_url=''"
            # snapshot until the user navigates away and back —
            # field-confirmed in 0.50.60 testing.
            try:
                from .android_cp import notify as _notify
                _notify.notify_project_changed(langcode)
            except Exception:
                pass
            return _json_response(start_response, '200 OK',
                                  {'ok': True,
                                   'dispatch': 'adopted'})
        # On failure, fall back to the pre-0.50.58 stash so the
        # user has a manual path to retry via the picker.
        _pending.add(_pending.KIND_ADOPT_ORIGIN, {
            'peer_id': peer_id,
            'device_name': device_name,
            'langcode': langcode,
            'url': repo_url,
        })
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r} stashed (adopt-origin '
              f'{repo_url!r}) — auto-adopt failed above',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True,
                               'dispatch': 'stashed_adopt_origin'})
    _pending.add(_pending.KIND_REMOTE_CONFLICT, {
        'peer_id': peer_id,
        'device_name': device_name,
        'langcode': langcode,
        'existing_url': local_url,
        'incoming_url': repo_url,
    })
    print(f'[lan-listener] share-offer from {peer_id[:8]!r} for '
          f'{langcode!r} stashed (remote-conflict '
          f'local={local_url!r} incoming={repo_url!r})',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK',
                          {'ok': True,
                           'dispatch': 'stashed_conflict'})


def _handle_share_declined(environ, start_response, peer_id,
                           prepared_payload=None):
    """Inbound nack handler. The peer we shared *langcode* with
    declined. Pull them out of our shared_projects allowlist for
    that langcode so the listener stops advertising it. (Refusal
    doesn't unpair them; it just rolls back the share.)"""
    if prepared_payload is not None:
        payload = prepared_payload
    else:
        payload, err = _read_json_body(environ)
        if payload is None:
            return _json_response(start_response, '400 Bad Request',
                                  {'ok': False, 'error': err})
    langcode = str(payload.get('langcode', '') or '')
    if not langcode:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'langcode required'})
    try:
        _peers.remove_shared_project(peer_id, langcode)
    except Exception as ex:
        print(f'[lan-listener] remove_shared_project raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
    print(f'[lan-listener] {peer_id[:8]!r} declined share for '
          f'{langcode!r}; allowlist rolled back',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _handle_share_unshared(environ, start_response, peer_id,
                           prepared_payload=None):
    """Inbound symmetric-unshare handler (0.50.44). The sender's
    user has unshared *langcode* on their side; mirror that on
    ours by removing the sender from *our* ``shared_projects``
    allowlist for that langcode. Idempotent: if the sender isn't
    in our allowlist for this langcode, this is a no-op."""
    if prepared_payload is not None:
        payload = prepared_payload
    else:
        payload, err = _read_json_body(environ)
        if payload is None:
            return _json_response(start_response, '400 Bad Request',
                                  {'ok': False, 'error': err})
    langcode = str(payload.get('langcode', '') or '')
    if not langcode:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'langcode required'})
    try:
        _peers.remove_shared_project(peer_id, langcode)
    except Exception as ex:
        print(f'[lan-listener] symmetric unshare '
              f'remove_shared_project raised: {ex!r}',
              file=sys.stderr, flush=True)
    print(f'[lan-listener] {peer_id[:8]!r} symmetric-unshared '
          f'{langcode!r}; mirrored allowlist removal',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _handle_cawl_fetch_bodyauth(environ, start_response):
    """Serve a CAWL image byte stream over LAN to a paired peer
    (NOTES #3, since 0.50.14).

    Body: ``{peer_id, fp, owner, repo, rel_path}`` — same
    body-auth shape as the other signalling endpoints (peer_id/fp
    lookup against ``peers.json``; no TLS-layer client auth, see
    ``_build_server`` for why). ``rel_path`` is the full nested
    path inside the repo (e.g. ``0001_body/foo.png``); a flat
    basename is also accepted and canonicalized via the local
    index. Sending the full ``rel_path`` is preferred because
    same-basename-different-variant entries (two ``foo.png`` files
    in different id directories) need to be disambiguated for the
    "fetch all variants over LAN" case.

    Response:
      - 200 ``application/octet-stream`` with the bytes if we have
        them cached locally.
      - 404 JSON if we don't have the byte cached.
      - 403 JSON if the caller isn't a paired peer.
      - 400 JSON on malformed body.

    Why a separate endpoint vs. piggybacking on the existing
    dulwich git fallthrough: CAWL images aren't tracked in any
    project's git tree (they live under ``$AZT_HOME/cawl/...``,
    a daemon-private directory) so they're invisible to dulwich's
    smart-protocol app. A purpose-built byte server is the
    minimum surface to expose them.
    """
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    fp = str(payload.get('fp', '') or '')
    owner = str(payload.get('owner', '') or '').strip()
    repo = str(payload.get('repo', '') or '').strip()
    rel_path = str(
        payload.get('rel_path') or payload.get('basename') or ''
    ).strip()
    if len(peer_id) != 64 or len(fp) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id / fp wrong length'})
    if not owner or not repo or not rel_path:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'owner / repo / rel_path '
                                        'required'})
    # Peer auth: must be in peers.json with the claimed fp.
    entry = _peers.get_peer(peer_id)
    if entry is None or str(entry.get('fp', '') or '') != fp:
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False,
                               'error': 'not_paired_or_fp_mismatch'})
    # rel_path safety: no leading slash (absolute), no ``..`` for
    # traversal, no backslashes, no hidden-file leading dot.
    # ``/`` BETWEEN components is fine (and expected) for nested
    # rel_paths like ``0001_body/foo.png``.
    if ('\\' in rel_path or '..' in rel_path
            or rel_path.startswith('.') or rel_path.startswith('/')):
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'bad_rel_path'})
    if '/' in owner or '/' in repo:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'bad_owner_or_repo'})
    from . import cawl as _cawl
    repo_slug = f'{owner}/{repo}'
    # Flat basename (no '/'): canonicalize via local index. With a
    # nested rel_path the requester has already done the
    # disambiguation — use it directly.
    if '/' not in rel_path:
        rel_path, _found = _cawl._resolve_basename_via_index(
            repo_slug, rel_path)
    target = _cawl.image_path(repo_slug, rel_path)
    if target is None or not os.path.isfile(target):
        return _json_response(start_response, '404 Not Found',
                              {'ok': False, 'error': 'not_cached'})
    try:
        with open(target, 'rb') as f:
            body = f.read()
    except OSError as ex:
        return _json_response(start_response, '500 Internal Error',
                              {'ok': False,
                               'error': f'read_failed: {ex!r}'})
    start_response('200 OK', [
        ('Content-Type', 'application/octet-stream'),
        ('Content-Length', str(len(body))),
    ])
    return [body]


def _handle_pair_request(environ, start_response):
    """Inbound Nearby-pair request from an unpaired device.

    Stashes a KIND_PAIR_REQUEST pending decision; the shared
    decisions watcher renders the popup on next poll. Body:
    ``{peer_id, fp, device_name, endpoint, langcode?}``.

    Accepts unpaired callers (this IS the gesture by which they
    become paired). Body must self-validate by carrying its own
    peer_id + fp (the peer_id IS the ed25519 pubkey, so the
    body claim is what TLS would have verified anyway under our
    CERT_NONE setup — see lan_listener._build_server for why).
    """
    from . import pending_decisions as _pending
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    fp = str(payload.get('fp', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    endpoint = str(payload.get('endpoint', '') or '')
    langcode = str(payload.get('langcode', '') or '')
    if len(peer_id) != 64 or len(fp) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id / fp wrong length'})
    _pending.add(_pending.KIND_PAIR_REQUEST, {
        'peer_id': peer_id, 'fp': fp,
        'device_name': device_name, 'endpoint': endpoint,
        'langcode': langcode,
    })
    print(f'[lan-listener] pair-request from {peer_id[:8]!r} '
          f'({device_name!r}) stashed',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _handle_pair_response(environ, start_response):
    """Inbound response to an outbound pair-request we sent.

    Body: ``{peer_id, accept: bool}``. Sender-side dispatch
    only updates the in-memory outbound-requests state; the
    actual peer record (if accept=True) is recorded when the
    receiver's hello-back lands via the normal hello flow.
    """
    from . import lan_pair_requests as _lpr
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    accept = bool(payload.get('accept', False))
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    _lpr.record_response(peer_id, accept)
    print(f'[lan-listener] pair-response from {peer_id[:8]!r}: '
          f'{"accept" if accept else "decline"}',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _peer_acl_middleware(app):
    """WSGI middleware: extract peer-id from the verified client
    cert (captured into ``environ`` by ``_CertCapturingHandler``);
    look it up in ``peers.json``; restrict the URL set to that
    peer's ``shared_projects`` before forwarding to dulwich.web.

    Short-circuits POST ``/v1/lan/hello`` to ``_handle_hello`` —
    that endpoint deliberately accepts unpaired callers as the
    auto-reverse-record half of the pairing flow.

    Rejects with 403 on:
      - missing peer cert (shouldn't happen — ``CERT_REQUIRED``
        rejects at TLS, but defensively handle)
      - unknown peer_id (not paired, not a hello)
      - fp mismatch (paired peer's cert fingerprint differs from
        the value recorded in peers.json)
      - request URL outside the peer's shared_projects allowlist
    """
    def wrapped(environ, start_response):
        # Identity at TLS layer is currently disabled (see
        # ``_build_server`` for the rationale: stdlib ssl has no
        # "request cert but skip CA validation" mode). Peer identity
        # is asserted via the request body for the signalling
        # endpoints (hello / share_offer / share_declined); for the
        # git smart-protocol fallthrough we accept any caller on
        # the LAN and gate the URL set by the union of every
        # paired peer's ``shared_projects`` (project must be
        # shared with at least one paired peer to be served).
        # FUTURE-HARDEN: move client identity into a signed-
        # message header (ed25519 sig over the request) so paired
        # peers can be cryptographically identified per-request.
        method = environ.get('REQUEST_METHOD')
        path_info = environ.get('PATH_INFO', '')
        # Signalling endpoints accept unpaired callers; identity
        # claim lives in the body. They self-validate by checking
        # the body's ``peer_id``/``fp`` match each other (the
        # peer_id IS the ed25519 pubkey).
        if method == 'POST' and path_info == '/v1/lan/hello':
            return _handle_hello_bodyauth(environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/share_offer':
            return _handle_share_offer_bodyauth(
                environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/share_declined':
            return _handle_share_declined_bodyauth(
                environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/share_unshared':
            return _handle_share_unshared_bodyauth(
                environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/pair_request':
            return _handle_pair_request(environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/pair_response':
            return _handle_pair_response(environ, start_response)
        if (method == 'POST'
                and path_info == '/v1/lan/cawl_fetch'):
            return _handle_cawl_fetch_bodyauth(
                environ, start_response)
        # Non-signalling fallthrough: dulwich.web's git smart-
        # protocol app. URL-level ACL is handled at backend-build
        # time — ``_build_dict_backend`` only mounts projects that
        # appear in at least one paired peer's ``shared_projects``,
        # so the dulwich app simply returns 404 for a URL outside
        # that set. Future-harden by re-adding the per-peer ACL
        # once client identity is signature-verified in the body.
        return app(environ, start_response)
    return wrapped


def _build_handler_class():
    """Subclass the stdlib WSGI request handler so each request's
    WSGI environ carries the verified peer cert (DER) extracted from
    the underlying ``ssl.SSLSocket``. ``WSGIRequestHandler`` is the
    portable base; dulwich's ``WSGIRequestHandlerLogger`` would
    do but isn't present in every dulwich version (the same
    refactor that removed ``HTTPGitServer`` may have hidden it
    too), so we just use the stdlib class and route logs to
    stderr via ``log_message`` override."""
    from wsgiref.simple_server import WSGIRequestHandler

    class _CertCapturingHandler(WSGIRequestHandler):
        def log_message(self, fmt, *args):
            # Cheap silent logger — peer requests are normal; we
            # don't need them in stderr unless debugging.
            pass

        def get_environ(self):
            environ = super().get_environ()
            try:
                sock = self.request
                if isinstance(sock, ssl.SSLSocket):
                    der = sock.getpeercert(binary_form=True)
                    if der:
                        environ['aztcollab.peer_cert_der'] = der
            except Exception:
                pass
            return environ

    return _CertCapturingHandler


# Per-project deferred-reset queue. When the post-receive reset
# below times out trying to acquire ``project_lock`` (the tablet's
# own outgoing ``_merge_then_push`` workflow can hold it for >5 s,
# longer than the receive-pack handler's tolerance), we add the
# langcode to this set. The scheduler watcher's tick drains the
# set by retrying ``_reset_working_tree_after_receive``; on
# success, the function removes its own entry. ``_commit_repo_locked``
# (in repo.py) also drains its own langcode at the top of every
# commit attempt, so the next commit_project absorbs the pending
# reset BEFORE staging — otherwise ``_stage_all`` sees the files
# that the merge brought in as "missing from working tree" and
# commits a *delete* for them, erasing the merge. Persisted to
# ``$AZT_HOME/pending_resets.json`` so a daemon restart while
# there's still a deferred reset on the queue doesn't lose track.
# Loaded back in ``scheduler.reconcile_on_startup``.
_PENDING_RESETS_FILENAME = 'pending_resets.json'
_pending_post_receive_resets = set()
_pending_resets_lock = threading.Lock()


def _pending_resets_path():
    from .paths import azt_home
    return os.path.join(azt_home(), _PENDING_RESETS_FILENAME)


def _save_pending_resets_locked():
    """Atomic-write the pending-resets set. Caller holds
    ``_pending_resets_lock``."""
    p = _pending_resets_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f'{p}.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w') as f:
            json.dump(sorted(_pending_post_receive_resets), f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except Exception as ex:
        print(f'[lan-listener] pending-resets save failed: {ex!r}',
              file=sys.stderr, flush=True)


def _add_pending_reset(langcode):
    """Mark *langcode* as needing a deferred post-receive reset."""
    with _pending_resets_lock:
        if langcode in _pending_post_receive_resets:
            return
        _pending_post_receive_resets.add(langcode)
        _save_pending_resets_locked()


def _remove_pending_reset(langcode):
    """Clear *langcode* from the deferred-reset queue."""
    with _pending_resets_lock:
        if langcode not in _pending_post_receive_resets:
            return
        _pending_post_receive_resets.discard(langcode)
        _save_pending_resets_locked()


def has_pending_reset(langcode):
    """Public predicate — used by ``repo._commit_repo_locked`` to
    decide whether to absorb a pending reset before staging."""
    with _pending_resets_lock:
        return langcode in _pending_post_receive_resets


def load_pending_resets_from_disk():
    """Re-populate the in-memory set from
    ``$AZT_HOME/pending_resets.json`` after a daemon restart. Called
    from ``scheduler.reconcile_on_startup``. Idempotent."""
    p = _pending_resets_path()
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as ex:
        print(f'[lan-listener] pending-resets load failed: {ex!r}',
              file=sys.stderr, flush=True)
        return
    if not isinstance(data, list):
        return
    with _pending_resets_lock:
        for entry in data:
            if isinstance(entry, str):
                _pending_post_receive_resets.add(entry)
    if data:
        print(f'[lan-listener] pending-resets loaded from disk: '
              f'{sorted(_pending_post_receive_resets)!r}',
              file=sys.stderr, flush=True)


def drain_pending_resets():
    """Retry each langcode in the deferred-reset queue. Called from
    the scheduler watcher tick. Each retry goes through
    ``_reset_working_tree_after_receive`` again; on success the
    function removes its own queue entry, on continued LockTimeout
    it re-adds it (no-op in that case). Other exceptions are logged
    and the entry stays on the queue for the next tick."""
    with _pending_resets_lock:
        pending = list(_pending_post_receive_resets)
    if not pending:
        return
    for langcode in pending:
        try:
            _reset_working_tree_after_receive(langcode)
        except Exception as ex:
            print(f'[lan-listener] drain_pending_resets '
                  f'{langcode!r}: {ex!r}',
                  file=sys.stderr, flush=True)


def _reset_working_tree_after_receive(langcode):
    """After an incoming receive-pack advances HEAD via a push
    from a peer, sync this peer's working tree + index to the
    new HEAD. Without this, dulwich's receive-pack updates refs
    without touching the working tree, and every file in the
    incoming commits shows as ``staged_mod`` indefinitely (index
    matches old state, HEAD points at new tree). Field symptom
    (baf 2026-05-22): ``n_changes`` jumps by hundreds-to-
    thousands after each fast-forward push, never clears until
    a subsequent ``commit_project`` happens to absorb the
    mismatch into a commit.

    Hard reset is the right semantic here: a successful
    receive-pack means the incoming changes are now canonically
    HEAD; the working tree should reflect that. The
    ``project_lock`` serializes us against any concurrent
    ``commit_project`` / ``atomic_finalize`` (those acquire the
    same lock), so a concurrent local edit can't land at the
    moment we reset. Short timeout (5 s): if the lock is busy
    longer than that, defer rather than block the WSGI worker;
    the next ``commit_project`` will absorb the mismatch the
    old (pre-0.45.35) way. 0.45.35."""
    from . import projects as _projects
    from .locks import project_lock, LockTimeout
    from dulwich import porcelain
    from dulwich.repo import Repo

    project = _projects.get(langcode)
    if project is None or not project.working_dir:
        return
    # Atomic-pending in-flight guard. Phase 1 of the peer's
    # ``atomic_open_write`` writes bytes to
    # ``.azt_atomic_pending/<token>`` via a raw ContentProvider FD
    # — no project_lock held during the write itself. Phase 2 (the
    # ``atomic_finalize`` RPC) DOES take the lock. Between those
    # two steps, this post-receive reset can race in: it acquires
    # the lock, runs ``porcelain.reset(mode='hard')``, releases.
    # If dulwich's reset clobbers the in-flight ``<token>`` file
    # (observed in the field as ``SERVER_ERROR: pending_not_found``
    # surfacing in the peer's ``stop_recording`` path, baf
    # 2026-05-22), the peer's Phase 2 fails — and worse, the
    # recorder UI hangs in "still recording" because its post-
    # stop state transition aborted on the save error.
    #
    # The guard: defer the reset if any
    # ``.azt_atomic_pending/<token>`` is younger than the
    # ``atomic_recovery._MIN_AGE_S`` threshold (60 s) — i.e., a
    # Phase 1 write that might still be mid-flight. The next
    # incoming push (or the next ``commit_project``) will absorb
    # the index/HEAD mismatch the old way. Worst case: ``n_changes``
    # stays inflated until the next push, which is the pre-0.45.36
    # behavior — strictly no worse than before. 0.45.38.
    pending_dir = os.path.join(project.working_dir,
                               '.azt_atomic_pending')
    if os.path.isdir(pending_dir):
        try:
            from . import atomic_recovery as _ar
            min_age = _ar._MIN_AGE_S
            now = _time.time()
            youngest_age = None
            for name in os.listdir(pending_dir):
                p = os.path.join(pending_dir, name)
                try:
                    age = now - os.stat(p).st_mtime
                except OSError:
                    continue
                if youngest_age is None or age < youngest_age:
                    youngest_age = age
            if youngest_age is not None and youngest_age < min_age:
                print(f'[lan-listener] post-receive reset '
                      f'{langcode!r}: deferred — pending-write in '
                      f'flight (youngest {youngest_age:.1f}s, '
                      f'threshold {min_age:.0f}s)',
                      file=sys.stderr, flush=True)
                return
        except Exception as ex:
            print(f'[lan-listener] pending-age guard raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            # Fall through — better to do the reset than skip
            # silently when the guard itself broke.
    try:
        with project_lock(project.working_dir, timeout=5):
            repo = Repo(project.working_dir)
            try:
                # 0.45.39 Phase-2 guard: defer if the working tree
                # has any non-pending unstaged modifications. The
                # 0.45.38 guard above only covers Phase 1 (scratch
                # tokens under .azt_atomic_pending/). Once a peer's
                # atomic_finalize completes — os.replace moves the
                # token to the final path — the scratch is gone, so
                # the age-guard misses it, but the final file is now
                # on disk with new bytes that ``commit_project``
                # hasn't yet picked up. A ``reset --hard HEAD`` here
                # would silently revert the just-landed LIFT (or
                # audio) edit to its old HEAD content — silent data
                # loss. Defer instead; the next ``commit_project``
                # absorbs the index/HEAD mismatch the old (pre-
                # 0.45.36) way. Worst case is the ghost ``n_changes``
                # spike persists until the next commit — strictly no
                # worse than pre-0.45.36 and recoverable.
                try:
                    st = porcelain.status(repo, untracked_files='no')
                    unstaged_paths = list(st.unstaged or [])
                except Exception as ex:
                    print(f'[lan-listener] status-guard raised: '
                          f'{ex!r}', file=sys.stderr, flush=True)
                    unstaged_paths = []
                if unstaged_paths:
                    pending_prefix = b'.azt_atomic_pending/'
                    orphan_prefix = b'.azt_atomic_orphans/'
                    real_mods = [
                        p for p in unstaged_paths
                        if not (p.startswith(pending_prefix)
                                or p.startswith(orphan_prefix))]
                    if real_mods:
                        # 0.45.44: instead of deferring (which left
                        # the next commit_project to silently revert
                        # the incoming peer's content), three-way
                        # merge HEAD's tree into the working tree.
                        # Working tree ends up with both sides'
                        # edits; next commit creates a proper merge
                        # commit on top of HEAD. See
                        # ``repo.integrate_head_into_working_tree``.
                        head = []
                        for p in real_mods[:3]:
                            try:
                                head.append(
                                    p.decode('utf-8', 'replace'))
                            except Exception:
                                head.append(repr(p))
                        print(f'[lan-listener] post-receive '
                              f'{langcode!r}: {len(real_mods)} '
                              f'unstaged mod(s) — merging HEAD into '
                              f'working tree (head={head!r})',
                              file=sys.stderr, flush=True)
                        try:
                            from . import repo as _repo_mod
                            applied, n_conflicts = (
                                _repo_mod.integrate_head_into_working_tree(
                                    repo, project.working_dir))
                            if applied:
                                print(f'[lan-listener] post-receive '
                                      f'{langcode!r}: merge applied '
                                      f'(conflicts={n_conflicts}); '
                                      f'next commit_project will '
                                      f'land the merged result',
                                      file=sys.stderr, flush=True)
                                return
                            # Fell through (first commit etc.); fall
                            # back to the deferred path so the next
                            # commit at least preserves working tree.
                            print(f'[lan-listener] post-receive '
                                  f'{langcode!r}: merge bailed; '
                                  f'deferring to next commit_project',
                                  file=sys.stderr, flush=True)
                            return
                        except Exception as ex:
                            # Merge raised: safer to defer than to
                            # leave the working tree in a half-merged
                            # state.
                            print(f'[lan-listener] post-receive '
                                  f'{langcode!r}: integrate raised '
                                  f'{ex!r}; deferring',
                                  file=sys.stderr, flush=True)
                            return
                # Re-attach HEAD as symref to refs/heads/main if
                # they've decoupled (since 0.46.5). Field-observed
                # merge-loop:
                #   - ``_merge_diverged`` on the LOCAL side calls
                #     ``worktree.commit(merge_heads=[...])`` which
                #     advances HEAD's pointer (symref or detached).
                #     On some flows HEAD ends up detached at our
                #     last merge SHA.
                #   - Incoming receive-pack updates ONLY
                #     ``refs/heads/main`` via ``set_if_equals``;
                #     HEAD's detached value is untouched.
                #   - Result: HEAD = our last merge, main = peer's
                #     last push. Each drain we see "peer at <main>",
                #     local HEAD at <our merge>, FF check fails,
                #     produce another degenerate merge, push,
                #     repeat. Loop never terminates because neither
                #     side's HEAD ever realigns with the converged
                #     main.
                # Fix: when HEAD is detached and main descends from
                # (or equals) HEAD's value, re-attach HEAD as
                # symref to refs/heads/main. HEAD's content is then
                # a subset of main's, no data loss. After the
                # re-attach, the next drain on this side sees
                # local_head == peer's HEAD (both equal to main),
                # no-op short-circuits, loop ends.
                main_ref = b'refs/heads/main'
                try:
                    symrefs = repo.refs.get_symrefs()
                    head_target = symrefs.get(b'HEAD')
                except Exception:
                    head_target = None
                if head_target != main_ref:
                    try:
                        main_sha = repo.refs[main_ref]
                        head_sha_raw = repo.refs[b'HEAD']
                    except KeyError:
                        main_sha = None
                        head_sha_raw = None
                    if main_sha and head_sha_raw \
                            and main_sha != head_sha_raw:
                        # Check ancestry: HEAD's value reachable
                        # from main's history (main = HEAD's
                        # descendant). Walk main's ancestry looking
                        # for HEAD's SHA.
                        head_is_ancestor = False
                        try:
                            for entry in repo.get_walker(
                                    include=[main_sha]):
                                if entry.commit.id == head_sha_raw:
                                    head_is_ancestor = True
                                    break
                        except Exception:
                            head_is_ancestor = False
                        if head_is_ancestor:
                            try:
                                repo.refs.set_symbolic_ref(
                                    b'HEAD', main_ref)
                                print(f'[lan-listener] '
                                      f'{langcode!r}: re-attached '
                                      f'HEAD as symref to '
                                      f'refs/heads/main '
                                      f'(was detached at '
                                      f'{head_sha_raw[:12].decode()}'
                                      f'; main at '
                                      f'{main_sha[:12].decode()})',
                                      file=sys.stderr, flush=True)
                            except Exception as ex:
                                print(f'[lan-listener] '
                                      f'{langcode!r}: re-attach '
                                      f'failed: {ex!r}',
                                      file=sys.stderr, flush=True)
                        else:
                            # Audit finding #4 (0.50.15): ancestry
                            # check failed or walker raised. The
                            # re-attach is unsafe (main is NOT a
                            # descendant of HEAD; rewriting HEAD to
                            # main would silently drop the work
                            # at HEAD). Pre-0.50.15 this fell
                            # through silently and the merge-loop
                            # could resume on the next receive.
                            # Emit a structured log line — same
                            # format as other [data-quality] tags
                            # so a daemon-log search surfaces it.
                            print(f'[data-quality] '
                                  f'head-detached-no-reattach '
                                  f'langcode={langcode!r} '
                                  f'head={head_sha_raw[:12].decode()} '
                                  f'main={main_sha[:12].decode()} '
                                  f'reason=main-not-descendant-of-head',
                                  file=sys.stderr, flush=True)
                head_sha = repo.refs[b'HEAD']
                porcelain.reset(repo, mode='hard', treeish=head_sha)
                print(f'[lan-listener] post-receive reset '
                      f'{langcode!r} → HEAD '
                      f'({head_sha[:12].decode()})',
                      file=sys.stderr, flush=True)
                # Success: clear any prior deferred-reset entry for
                # this langcode. (Idempotent — no-op if not queued.)
                _remove_pending_reset(langcode)
                # HEAD advanced + working tree changed; push-notify
                # observers so they re-poll project_status without
                # waiting for the next background tick.
                try:
                    from .android_cp import notify as _notify
                    _notify.notify_project_changed(langcode)
                except Exception:
                    pass
                # Post-receive peer-SHA refresh (0.50.50). Receive-
                # pack didn't tell us WHICH paired peer pushed, but
                # the pusher must be at our new HEAD (they couldn't
                # have pushed a SHA they don't have). Without this
                # refresh, our ``last_seen_main[<every-paired-peer>]
                # [langcode]`` stays at whatever it was before, so
                # ``_lan_unshared`` walks excluding stale peer SHAs
                # and reports the just-received commit as "unshared"
                # — visible as LAN-1 on a project both phones are
                # actually in sync on. ls-remote each paired peer
                # sharing this langcode; update last_seen_main for
                # the ones matching our new HEAD. Background thread
                # so we don't block the WSGI worker.
                try:
                    new_sha_hex = head_sha.decode('ascii', 'replace')
                    threading.Thread(
                        target=_refresh_peer_last_seen_after_receive,
                        args=(langcode, new_sha_hex),
                        daemon=True,
                        name='lan-post-receive-refresh').start()
                except Exception as ex:
                    print(f'[lan-listener] post-receive refresh '
                          f'spawn raised: {ex!r}',
                          file=sys.stderr, flush=True)
            finally:
                try:
                    repo.close()
                except Exception:
                    pass
    except LockTimeout:
        # Lock holder is someone else's project_lock (typically this
        # device's own outgoing ``_merge_then_push`` workflow, which
        # holds the lock through the entire merge — often >5 s).
        # Queue the langcode so the scheduler's watcher tick can
        # retry, and so ``_commit_repo_locked`` can absorb it before
        # staging on the next commit_project. Without this, the
        # working tree stays out of sync with HEAD indefinitely,
        # showing as ghost ``n_changes`` (the merge files appear as
        # "deleted" in working-tree status); worse, the next
        # commit_project would stage that "delete" and erase the
        # merge. See repo._commit_repo_locked for the absorbing
        # half of this fix.
        _add_pending_reset(langcode)
        print(f'[lan-listener] post-receive reset {langcode!r}: '
              f'lock busy (5s timeout) — queued for retry on next '
              f'scheduler tick + absorb on next commit_project',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[lan-listener] post-receive reset {langcode!r} '
              f'failed: {ex!r}',
              file=sys.stderr, flush=True)


def _refresh_peer_last_seen_after_receive(langcode, new_head_sha_hex):
    """Post-receive last_seen_main refresh (0.50.50).

    After our listener accepts a push, walk every paired peer
    whose ``shared_projects`` contains *langcode* and ask them
    (via ls-remote) what SHA they hold. For any peer whose main
    matches our new HEAD, update ``last_seen_main[peer][langcode]``
    so ``_lan_unshared`` no longer reports the just-received
    commit as unshared. Runs on a worker thread off the WSGI
    request path; per-peer failures are isolated.

    Cost: one ls-remote per paired peer sharing the project.
    Fast-fail gate makes recently-unreachable peers free skips.

    Why we don't update peers whose main is BEHIND our HEAD:
    those peers may have a stale view (they pushed to us earlier
    but haven't seen our subsequent commits) OR they genuinely
    are behind. Either way we don't know they have the new SHA,
    so leaving their ``last_seen_main`` where it was is the
    "OK on uncertainty" answer — same convention the helper
    families follow."""
    try:
        from . import peers as _peers
        from . import lan_push as _lan_push
    except Exception as ex:
        print(f'[lan-listener] post-receive refresh dispatch '
              f'raised: {ex!r}', file=sys.stderr, flush=True)
        return
    try:
        candidates = []
        for entry in _peers.list_peers() or []:
            pid = entry.get('peer_id', '') or ''
            if not pid:
                continue
            shared = entry.get('shared_projects') or []
            if langcode not in shared:
                continue
            candidates.append(pid)
    except Exception as ex:
        print(f'[lan-listener] post-receive refresh candidate '
              f'enumeration raised: {ex!r}',
              file=sys.stderr, flush=True)
        return
    if not candidates:
        return
    matched = []
    for pid in candidates:
        try:
            peer_sha = _lan_push.peek_peer_head(pid, langcode)
        except Exception as ex:
            print(f'[lan-listener] post-receive refresh peek '
                  f'{pid[:8]!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        if peer_sha and peer_sha == new_head_sha_hex:
            try:
                _peers.set_peer_last_seen_main(
                    pid, langcode, peer_sha)
                # The peer is AT our new head — that's confirmed
                # containment of our own commit; record the
                # covered-local coverage the sync-status walkers
                # fall back to when this peer's head later moves
                # somewhere we haven't fetched.
                _peers.set_peer_covered_local(
                    pid, langcode, peer_sha)
                matched.append(pid[:8])
            except Exception as ex:
                print(f'[lan-listener] post-receive refresh '
                      f'set_peer_last_seen_main {pid[:8]!r} '
                      f'raised: {ex!r}',
                      file=sys.stderr, flush=True)
    print(f'[lan-listener] post-receive refresh '
          f'{langcode!r}: peers_sharing={len(candidates)} '
          f'at-our-HEAD={len(matched)} ({matched!r})',
          file=sys.stderr, flush=True)


_POST_RECEIVE_PATH_RE = None


def _post_receive_pack_middleware(inner_app):
    """WSGI middleware: catch successful receive-pack POSTs and
    schedule a working-tree reset for the affected project. See
    ``_reset_working_tree_after_receive`` for the why."""
    import re
    global _POST_RECEIVE_PATH_RE
    if _POST_RECEIVE_PATH_RE is None:
        _POST_RECEIVE_PATH_RE = re.compile(
            r'^/([^/]+)\.git/git-receive-pack$')

    def _wrapped(environ, start_response):
        method = environ.get('REQUEST_METHOD', '')
        path = environ.get('PATH_INFO', '')
        m = (_POST_RECEIVE_PATH_RE.match(path)
             if method == 'POST' else None)
        if m is None:
            return inner_app(environ, start_response)

        langcode = m.group(1)
        status_holder = [None]

        def _capture_start(status, headers, exc_info=None):
            status_holder[0] = status
            return start_response(status, headers, exc_info)

        result = inner_app(environ, _capture_start)

        def _generator():
            try:
                for chunk in result:
                    yield chunk
            finally:
                try:
                    s = status_holder[0] or ''
                    if s.startswith('200'):
                        _reset_working_tree_after_receive(langcode)
                except Exception as ex:
                    print(f'[lan-listener] post-receive '
                          f'middleware raised: {ex!r}',
                          file=sys.stderr, flush=True)
                if hasattr(result, 'close'):
                    try:
                        result.close()
                    except Exception:
                        pass
        return _generator()

    return _wrapped


def _build_server(port):
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer
    from dulwich.web import make_wsgi_chain

    backend = _build_dict_backend()
    # ``make_wsgi_chain(backend, …)`` wraps ``HTTPGitApplication``
    # in GunzipFilter + LimitedInputFilter for us; don't pass an
    # already-built HTTPGitApplication or we double-wrap and
    # ``backend.open_repository`` resolves to the inner
    # HTTPGitApplication instead of the DictBackend.
    # Outer ``_post_receive_pack_middleware`` triggers the
    # working-tree reset after successful receive-pack POSTs;
    # inner ``_peer_acl_middleware`` gates access by paired-peer
    # shared_projects.
    app = _repo_closing_middleware(
        _peer_acl_middleware(
            _post_receive_pack_middleware(make_wsgi_chain(backend))),
        backend)

    # Use the stdlib WSGI server rather than dulwich's
    # ``HTTPGitServer`` — the latter was removed (or renamed) in
    # the version of dulwich p4a ships, so import-time fails on
    # Android. ``wsgiref.simple_server.WSGIServer`` + a threaded
    # mixin is the equivalent setup, plus our own request-handler
    # subclass to capture the verified peer cert.
    class _ThreadedTLSGitServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = _ThreadedTLSGitServer(
        ('0.0.0.0', int(port)), _build_handler_class())
    srv.set_app(app)

    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        srv.server_close()
        raise RuntimeError('peer identity unavailable; '
                           'cannot start LAN listener')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    # Client cert validation deliberately disabled. Python's stdlib
    # ``ssl`` has no "request cert but skip CA validation" mode —
    # ``CERT_REQUIRED`` makes it validate against a CA chain we
    # don't have (peer certs are self-signed and pinned by
    # fingerprint via ``peers.json``, not chain-of-trust).
    # ``CERT_OPTIONAL`` rejects the handshake the same way when
    # the client *does* present a cert, which our peers always do.
    # ``CERT_NONE`` lets the handshake complete; the TLS channel
    # stays encrypted, the SERVER side is still pinned by the
    # client (urllib3's ``assert_fingerprint``), and peer identity
    # at the client end is asserted via the request body
    # (``peer_id`` + ``fp`` claims that ``_handle_hello`` validates
    # against the cert delivered through ``getpeercert``). A
    # future-hardening pass will move client identity into a
    # signed-message header (ed25519 sig over the request); for
    # now LAN identity is body-claimed and the user is presumed
    # in control of their LAN.
    ctx.verify_mode = ssl.CERT_NONE
    ctx.check_hostname = False
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True,
                                 do_handshake_on_connect=False)
    return srv


def _outward_ip_guess():
    """Best-effort local IP for the advertised endpoint (settings-UI
    line + pairing QR).

    Step 1: UDP-connect to a non-routed destination and read back the
    local socket's IP — resolves to the DEFAULT-ROUTE interface.
    Avoids parsing /proc/net/route and works the same on Android and
    desktop. Step 2 (0.53.6): when there IS no default route — the
    field case is a hotspot-HOST desktop with its uplink unplugged
    (repro 2026-07-07: QR advertised '0.0.0.0' and the phone at
    10.42.0.100 had nothing to connect to; the host was 10.42.0.1) —
    enumerate interface addresses via SIOCGIFCONF and pick the first
    private non-loopback one. Linux-only ioctl, guarded; other
    platforms keep the old '0.0.0.0' fallback. (A multi-homed host
    whose default route is NOT the drill network still advertises the
    wrong IP — the real fix is advertising all addresses in the QR,
    tracked in agenda/local_lan_sync_stub.md § Pairing.)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 53))  # TEST-NET-1, never routed
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith('127.') and ip != '0.0.0.0':
            return ip
    except OSError:
        pass
    for ip in _interface_ipv4s():
        return ip
    return '0.0.0.0'


def _interface_ipv4s():
    """Non-loopback IPv4 addresses of local interfaces, private
    (RFC 1918) addresses first. Empty list when enumeration isn't
    available (non-Linux without a default route)."""
    addrs = []
    try:
        import array
        import fcntl
        import struct
        bufsize = 32 * 40                  # 32 ifreq slots, 64-bit
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            buf = array.array('B', b'\0' * bufsize)
            out = fcntl.ioctl(
                s.fileno(), 0x8912,        # SIOCGIFCONF
                struct.pack('iL', bufsize, buf.buffer_info()[0]))
            outbytes = struct.unpack('iL', out)[0]
            data = buf.tobytes()[:outbytes]
            for i in range(0, outbytes, 40):
                ip = socket.inet_ntoa(data[i + 20:i + 24])
                if not ip.startswith('127.') and ip != '0.0.0.0':
                    addrs.append(ip)
        finally:
            s.close()
    except Exception:
        return []

    def _private(ip):
        return (ip.startswith('10.') or ip.startswith('192.168.')
                or any(ip.startswith(f'172.{n}.')
                       for n in range(16, 32)))
    return sorted(set(addrs), key=lambda ip: (not _private(ip), ip))


def _port_memo_path():
    return os.path.join(_paths.azt_home(), 'lan_listener_port')


def _read_preferred_port():
    """Last successfully-bound listener port, or 0 (= let the OS
    pick). Re-binding the same port across daemon restarts keeps
    every peer's cached / persisted endpoint for us valid — a
    respawn no longer strands peers dialing the old port until
    their discovery catches up (stale-peer-address incidents
    2026-07-10/11)."""
    try:
        with open(_port_memo_path()) as f:
            p = int(f.read().strip())
        return p if 1024 < p < 65536 else 0
    except Exception:
        return 0


def _write_preferred_port(port):
    try:
        tmp = f'{_port_memo_path()}.tmp'
        with open(tmp, 'w') as f:
            f.write(str(int(port)))
        os.replace(tmp, _port_memo_path())
    except Exception as ex:
        print(f'[lan-listener] port memo write failed: {ex!r}',
              file=sys.stderr, flush=True)


def start():
    """Start the listener, preferring the previously-bound port
    (see ``_read_preferred_port``) and falling back to an
    OS-assigned one when it's taken. Idempotent — a second call
    while running returns the existing endpoint."""
    with _LOCK:
        if _STATE['server'] is not None:
            return _STATE['bound']
        srv = None
        preferred = _read_preferred_port()
        if preferred:
            try:
                srv = _build_server(preferred)
            except OSError as ex:
                print(f'[lan-listener] previous port {preferred} '
                      f'unavailable ({ex!r}); binding ephemeral',
                      file=sys.stderr, flush=True)
                srv = None
        if srv is None:
            srv = _build_server(0)
        host = _outward_ip_guess()
        port = srv.server_address[1]
        _write_preferred_port(port)
        thread = threading.Thread(
            target=srv.serve_forever, name='lan-listener',
            daemon=True)
        thread.start()
        _STATE['server'] = srv
        _STATE['thread'] = thread
        _STATE['bound'] = (host, port)
        print(f'[lan-listener] started on {host}:{port}',
              file=sys.stderr, flush=True)
        return _STATE['bound']


def stop():
    """Stop the listener. Idempotent."""
    with _LOCK:
        srv = _STATE['server']
        if srv is None:
            return
        _STATE['server'] = None
        _STATE['thread'] = None
        _STATE['bound'] = None
    try:
        srv.shutdown()
    except Exception as ex:
        print(f'[lan-listener] shutdown raised: {ex!r}',
              file=sys.stderr, flush=True)
    try:
        srv.server_close()
    except Exception:
        pass
    print('[lan-listener] stopped', file=sys.stderr, flush=True)


def apply_toggle():
    """Reconcile the listener lifecycle with the union of:
      - ``lan.autodiscovery`` (continuous-on policy bit), and
      - ``lan_fgs`` discovery ref count > 0 (a burst is active).

    Called from the toggle RPC handler after the setting is
    persisted, from ``lan_burst.start_burst`` /
    ``lan_burst._burst_done``, and from the watcher's reconcile
    tick. Safe to call from anywhere; hot-applied — no daemon
    restart required.

    Order on UP: acquire WifiLocks first (so multicast is
    available before any NsdManager browse fires), then promote
    the :provider service to FGS (so the OS can't kill us mid-
    handshake), then start the listener thread. Reverse on
    DOWN.

    Per-step failure attribution: each phase logs its own
    ``[lan-listener] {step} failed`` line so a field log
    immediately identifies whether WifiLock, FGS promotion, or
    socket bind is the failing seam. Idempotent: when called on
    a healthy daemon, the ``not is_running()`` / ``is_running()``
    guards short-circuit so no work is done."""
    from .android_cp import lan_fgs as _lan_fgs
    # Up if either reason is active: user picked continuous, or a
    # burst is currently armed. The burst path uses
    # ``arm_for_discovery`` which bumps the ref count we're
    # reading here.
    autodiscovery = _settings.lan_autodiscovery()
    burst_armed = (_lan_fgs.snapshot().get('ref_discovery', 0) > 0)
    desired = autodiscovery or burst_armed
    if desired and not is_running():
        try:
            _lan_fgs.acquire_wifi_locks()
        except Exception as ex:
            print(f'[lan-listener] acquire_wifi_locks failed: {ex!r}',
                  file=sys.stderr, flush=True)
            return
        try:
            _lan_fgs.start_fgs()
        except Exception as ex:
            print(f'[lan-listener] start_fgs failed: {ex!r}',
                  file=sys.stderr, flush=True)
            _lan_fgs.release_wifi_locks()
            return
        try:
            bound = start()
        except Exception as ex:
            print(f'[lan-listener] listener bind failed: {ex!r}',
                  file=sys.stderr, flush=True)
            _lan_fgs.stop_fgs()
            _lan_fgs.release_wifi_locks()
            return
        # Advertise + browse only after the listener is bound, so
        # the port we publish is real.
        try:
            ident = _peer_id.ensure()
            _lan_discovery.start_advertise(
                ident['peer_id'], ident['fp'],
                bound[1], _store.get_device_name())
            _lan_discovery.start_browse()
        except Exception as ex:
            print(f'[lan-listener] discovery start failed: {ex!r}',
                  file=sys.stderr, flush=True)
        # Listener-bind sweep (0.50.45). The radio just came up;
        # any paired peer we already know an endpoint for (from
        # ``peers.json::endpoints`` recorded at pair-time, or a
        # mDNS cache that survived a brief drop) might be
        # behind on shared projects. Fire one sweep per paired
        # peer in a worker thread so the binder returns promptly.
        # ``sweep_peer`` skips peers whose endpoint can't be
        # resolved, so it's cheap when nobody's actually
        # reachable — no harm in firing optimistically.
        def _listener_bind_sweep():
            try:
                from . import peers as _peers
                from . import lan_push as _lan_push
                for entry in _peers.list_peers():
                    pid = entry.get('peer_id', '') or ''
                    if not pid:
                        continue
                    try:
                        _lan_push.sweep_peer(pid)
                    except Exception as ex:
                        print(f'[lan-listener] bind-sweep '
                              f'{pid[:8]!r} raised: {ex!r}',
                              file=sys.stderr, flush=True)
            except Exception as ex:
                print(f'[lan-listener] bind-sweep dispatch '
                      f'raised: {ex!r}',
                      file=sys.stderr, flush=True)
        import threading as _t_mod
        _t_mod.Thread(target=_listener_bind_sweep, daemon=True,
                      name='lan-bind-sweep').start()
    elif not desired and is_running():
        try:
            _lan_discovery.stop_browse()
            _lan_discovery.stop_advertise()
        except Exception as ex:
            print(f'[lan-listener] discovery stop raised: {ex!r}',
                  file=sys.stderr, flush=True)
        stop()
        _lan_fgs.stop_fgs()
        _lan_fgs.release_wifi_locks()
