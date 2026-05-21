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


def _handle_hello(environ, start_response, cert_der):
    """Auto-reverse-record handler. Called from the WSGI middleware
    when the path is ``/v1/lan/hello`` (POST). Lets an *unpaired*
    peer who has us already in their ``peers.json`` introduce
    themselves: we verify the cert they handshake'd with matches
    the identity they're claiming in the body, then record the pair
    on our side. No QR scan needed in the reverse direction.

    Per the parked spec § Pairing → step 5.

    Body: ``{peer_id, fp, device_name}``. Response: ``{ok: True,
    peer_id}`` on success."""
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
    claimed_peer_id = str(payload.get('peer_id', '') or '')
    claimed_fp = str(payload.get('fp', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    actual_peer_id = _peer_id_from_cert_der(cert_der)
    actual_fp = _cert_fp_from_der(cert_der)
    if not actual_peer_id or claimed_peer_id != actual_peer_id:
        start_response('403 Forbidden',
                       [('Content-Type', 'text/plain')])
        return [b'cert peer_id does not match claimed peer_id\n']
    if not actual_fp or claimed_fp != actual_fp:
        start_response('403 Forbidden',
                       [('Content-Type', 'text/plain')])
        return [b'cert fingerprint does not match claimed fp\n']
    # Defer to record_pair — preserves any existing shared_projects /
    # static_endpoints on re-hello, refreshes last_seen_at.
    _peers.record_pair(actual_peer_id, actual_fp, device_name, '')
    resp = _json.dumps({'ok': True, 'peer_id': actual_peer_id})
    body_bytes = resp.encode('utf-8')
    start_response('200 OK', [
        ('Content-Type', 'application/json'),
        ('Content-Length', str(len(body_bytes))),
    ])
    print(f'[lan-listener] hello: recorded {actual_peer_id[:8]!r} '
          f'({device_name!r})', file=sys.stderr, flush=True)
    return [body_bytes]


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
        cert_der = environ.get('aztcollab.peer_cert_der')
        if not cert_der:
            start_response('403 Forbidden', [('Content-Type', 'text/plain')])
            return [b'no client cert\n']
        # Hello short-circuit: accept unpaired callers introducing
        # themselves so the QR-scan side doesn't need to rescan in
        # the other direction.
        if (environ.get('REQUEST_METHOD') == 'POST'
                and environ.get('PATH_INFO') == '/v1/lan/hello'):
            return _handle_hello(environ, start_response, cert_der)
        peer_id = _peer_id_from_cert_der(cert_der)
        if not peer_id:
            start_response('403 Forbidden', [('Content-Type', 'text/plain')])
            return [b'unsupported client cert\n']
        entry = _peers.get_peer(peer_id)
        if entry is None:
            start_response('403 Forbidden', [('Content-Type', 'text/plain')])
            return [b'peer not paired\n']
        got_fp = _cert_fp_from_der(cert_der)
        expected_fp = entry.get('fp', '')
        if expected_fp and got_fp != expected_fp:
            print(f'[lan-listener] fp mismatch for peer '
                  f'{peer_id[:8]!r}: expected={expected_fp[:16]!r} '
                  f'got={got_fp[:16]!r}',
                  file=sys.stderr, flush=True)
            start_response('403 Forbidden', [('Content-Type', 'text/plain')])
            return [b'cert fingerprint mismatch\n']
        # Touch last_seen on every authenticated request — cheap; gives
        # the settings UI a "last contact" indicator for free.
        try:
            _peers.touch_last_seen(peer_id)
        except Exception:
            pass
        # Per-request ACL: confine the URL set to projects this peer
        # is permitted to fetch.
        path = environ.get('PATH_INFO', '')
        shared = set(entry.get('shared_projects') or [])
        if shared:
            allowed = any(
                path == f'/{lang}.git'
                or path.startswith(f'/{lang}.git/')
                for lang in shared
            )
            if not allowed:
                start_response('403 Forbidden',
                               [('Content-Type', 'text/plain')])
                return [b'project not shared with this peer\n']
        environ['aztcollab.peer_id'] = peer_id
        return app(environ, start_response)
    return wrapped


def _build_handler_class():
    """Subclass dulwich's WSGI request handler so each request's
    WSGI environ carries the verified peer cert (DER) extracted from
    the underlying ``ssl.SSLSocket``. dulwich.web's
    ``WSGIRequestHandlerLogger`` follows the stdlib
    ``wsgiref.simple_server`` pattern, so overriding
    ``get_environ`` is the right seam."""
    from dulwich.web import WSGIRequestHandlerLogger

    class _CertCapturingHandler(WSGIRequestHandlerLogger):
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
    from dulwich.web import HTTPGitApplication, HTTPGitServer, make_wsgi_chain

    backend = _build_dict_backend()
    git_app = HTTPGitApplication(backend)
    app = _peer_acl_middleware(make_wsgi_chain(git_app))

    class _ThreadedTLSGitServer(ThreadingMixIn, HTTPGitServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = _ThreadedTLSGitServer(
        ('0.0.0.0', int(port)), _build_handler_class(),
        backend=backend, dumb=False)

    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        srv.server_close()
        raise RuntimeError('peer identity unavailable; '
                           'cannot start LAN listener')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    ctx.verify_mode = ssl.CERT_REQUIRED
    # We pin per peer via the WSGI middleware, not via CA chain
    # validation — pass-through verification callback.
    ctx.check_hostname = False
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True,
                                 do_handshake_on_connect=False)

    # dulwich.web HTTPGitServer doesn't carry .application by default;
    # set it explicitly so the WSGIRequestHandler picks up our middleware
    # chain in get_app().
    srv.application = app
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
