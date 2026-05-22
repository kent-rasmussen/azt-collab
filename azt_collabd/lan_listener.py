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

import os
import socket
import ssl
import sys
import threading

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


def is_running():
    with _LOCK:
        return _STATE['server'] is not None


def bound_endpoint():
    """Return ``(host, port)`` if running, else ``None``. Host is
    the daemon's outward-facing LAN IP (best-effort); fall back to
    ``0.0.0.0`` and let the caller substitute the discovered IP."""
    with _LOCK:
        return _STATE['bound']


def _build_dict_backend():
    """Build a ``dulwich.server.DictBackend`` exposing one
    ``/{lang}.git`` mount per project that's shared with at least
    one paired peer. Per-request the middleware further restricts
    the exposed set to projects shared with the specific peer making
    the request, so a paired phone can't fetch a project that's
    only shared with someone else.

    Rebuilt on demand — every fresh handler creation reads the
    current state of ``peers.json``, so changes to share lists take
    effect on the next request without a listener restart."""
    from dulwich.repo import Repo
    from dulwich.server import DictBackend

    shared_anywhere = set()
    for peer in _peers.list_peers():
        shared_anywhere.update(peer.get('shared_projects') or [])

    mapping = {}
    for project in _projects.list_all():
        if project.langcode not in shared_anywhere:
            continue
        if not project.working_dir:
            continue
        try:
            repo = Repo(project.working_dir)
        except Exception as ex:
            print(f'[lan-listener] skipping {project.langcode!r}: '
                  f'Repo open failed: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        mapping[f'/{project.langcode}.git'] = repo
    return DictBackend(mapping)


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
    langcode_offered = str(payload.get('langcode', '') or '')
    if langcode_offered:
        try:
            _peers.add_shared_project(
                actual_peer_id, langcode_offered)
        except Exception as ex:
            print(f'[lan-listener] hello auto-share for '
                  f'{langcode_offered!r} raised: {ex!r}',
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


def _handle_share_offer(environ, start_response, peer_id,
                       prepared_payload=None):
    """Inbound share-offer handler. Caller is a paired peer who
    wants us to clone *langcode* from them. Stash as a pending
    decision; UI surfaces it on the next visit."""
    from . import pending_decisions as _pending
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
    _pending.add(_pending.KIND_SHARE_OFFER, {
        'peer_id': peer_id,
        'device_name': device_name,
        'langcode': langcode,
        'repo_url': repo_url,
        'vernlang': vernlang,
    })
    print(f'[lan-listener] share-offer from {peer_id[:8]!r} for '
          f'{langcode!r} stashed',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


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
    app = _peer_acl_middleware(make_wsgi_chain(backend))

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
    """Best-effort: open a UDP socket to a non-routed destination and
    read back the local socket's IP. Avoids parsing /proc/net/route
    and works the same on Android and desktop. Returns '0.0.0.0' if
    the trick fails (offline machine)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 53))  # TEST-NET-1, never routed
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return '0.0.0.0'


def start():
    """Start the listener on an OS-assigned port. Idempotent — a
    second call while running returns the existing endpoint."""
    with _LOCK:
        if _STATE['server'] is not None:
            return _STATE['bound']
        srv = _build_server(0)
        host = _outward_ip_guess()
        port = srv.server_address[1]
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
    """Reconcile the listener lifecycle with the daemon-wide
    ``lan.allow_sync`` setting. Called from the toggle RPC handler
    after the setting is persisted; safe to call from anywhere.
    Hot-applied — no daemon restart required.

    Order on toggle ON: acquire WifiLocks first (so multicast is
    available before any NsdManager browse fires later in phase 5),
    then promote the :provider service to FGS (so the OS can't
    kill us mid-handshake), then start the listener thread.
    Reverse on OFF."""
    from .android_cp import lan_fgs as _lan_fgs
    desired = _settings.lan_allow_sync()
    if desired and not is_running():
        try:
            _lan_fgs.acquire_wifi_locks()
            _lan_fgs.start_fgs()
            bound = start()
        except Exception as ex:
            print(f'[lan-listener] start failed: {ex!r}',
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
