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
    # Last failing step + detail from ``apply_toggle`` ('' when the
    # last attempt succeeded). Surfaced typed on the toggle RPC so the
    # settings line can NAME the failing seam instead of telling the
    # user to go read a log (0.54.75).
    'bind_error': '',
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

# Reverse-delivery gate (0.55.13). ``_reverse_deliver`` fires from the
# git routes, which a fetching peer hits several times per exchange —
# and each delivery dials the peer, whose own reverse delivery dials
# back. Ungated (0.55.10–0.55.12) that is a mutual amplifier: ~330
# threads in ~10 s and a dead ``:provider`` process, every delivery a
# no-op. One look per contact burst is all convergence needs.
_REVERSE_COOLDOWN_S = 60.0
_REVERSE_MAX_INFLIGHT = 2
_reverse_gate_lock = threading.Lock()
_reverse_last_at = {}      # (peer_id, langcode) → admission unix ts
_reverse_inflight = [0]    # one-slot list: mutable under the lock


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


def bind_error():
    """Last failing step of ``apply_toggle`` ('' when the listener
    came up, or hasn't been asked to). 0.54.75."""
    with _LOCK:
        return _STATE.get('bind_error', '') or ''


def _usb_net_ifaces():
    """Names of local network interfaces that are USB-attached —
    i.e. a phone/tablet presenting a tether gadget (``usb0``,
    ``enp0s20u2``, ``rndis0``, ``ncm0``…). Read from
    ``/sys/class/net/<if>/device`` whose resolved path sits under a
    USB bus; falls back to name matching where sysfs isn't readable.

    Why sysfs and not ``_interface_ipv4s``: that helper uses
    SIOCGIFCONF, which lists only interfaces that ALREADY have an
    address. The state we most need to name is exactly the one it
    can't see — the cable is plugged in and the gadget exists, but
    the phone hasn't switched USB tethering on, so no address has
    been handed out (field 2026-07-25: tablet cabled to the desktop,
    tether toggle off, and the UI said "no action needed").

    Empty list on any platform / permission problem — callers treat
    that as "can't tell", never as "no cable"."""
    out = []
    base = '/sys/class/net'
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        if name == 'lo':
            continue
        is_usb = False
        try:
            dev = os.path.realpath(os.path.join(base, name, 'device'))
            is_usb = ('/usb' in dev) or ('usb' in os.path.basename(dev))
        except OSError:
            is_usb = False
        if not is_usb:
            # Naming fallback for hosts where the sysfs symlink isn't
            # present (some Android kernels): the conventional gadget
            # names are stable enough to recognise.
            low = name.lower()
            is_usb = (low.startswith('usb')
                      or low.startswith('rndis')
                      or low.startswith('ncm'))
        if is_usb:
            out.append(name)
    return out


def link_state():
    """Classify why (or whether) this daemon is reachable by a peer,
    for the settings line + cable check. Returns one of:

    - ``'ok'``          — bound AND at least one non-loopback IPv4 to
                          be reached at.
    - ``'tether_off'``  — bound, no usable address, but a USB network
                          gadget IS present. In practice this means
                          tethering is ON and no address has arrived
                          yet (DHCP pending or failed) — NOT
                          "cable in, tethering off", because with
                          tethering off Android exposes no network
                          function and the host sees no interface to
                          detect.
    - ``'no_link'``     — bound, no usable address, no USB gadget
                          seen. Covers BOTH "nothing plugged in" and
                          "cabled but not tethered": indistinguishable
                          from here, so callers must not claim a cable
                          is absent (see the 0.54.80 wording). Correct
                          by design when a device is off network; not
                          a failure.
    - ``'not_bound'``   — the listener isn't running. A real failure;
                          pair with ``bind_error()`` for the step.
    - ``'off'``         — sharing is switched off (caller decides;
                          never returned from here).

    The distinction matters because 0.54.74 collapsed all of these
    into an empty endpoint string, so a healthy listener on an
    off-network machine was reported the same way as a failed bind —
    and the UI told the user no action was needed in a state where
    the fix was one toggle away on their tablet. 0.54.75."""
    if not is_running():
        return 'not_bound'
    if bound_endpoints_all():
        return 'ok'
    if _usb_net_ifaces():
        return 'tether_off'
    return 'no_link'


def bound_endpoints_all():
    """Every plausible ``ip:port`` for THIS listener: each non-loopback
    local IPv4 (``_interface_ipv4s``, private-first) paired with the
    bound port. Advertised in the pairing QR so a peer reaching us over
    ANY link — wifi, a USB-tether ``usb0``, a hotspot subnet — finds a
    reachable address (the receiver tries each). This is the fix for
    the single-address gap: ``_outward_ip_guess`` picks only the
    default-route IP, which on a tethering setup is the wrong one for
    the cable. Empty list when the listener isn't bound."""
    with _LOCK:
        bound = _STATE['bound']
    if not bound:
        return []
    port = bound[1]
    ips = _interface_ipv4s()
    if not ips:
        host = bound[0]
        return [f'{host}:{port}'] if host and host != '0.0.0.0' else []
    return [f'{ip}:{port}' for ip in ips]


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
    # Was this pairing deliberately REVOKED here? (0.54.97) The heal
    # that completes an interrupted handshake works by the other side
    # re-saying hello, and this handler records a pairing for any
    # caller — so without this check, a peer holding a stale record
    # would silently resurrect a pairing the user had removed. An
    # invite-plus-accept whose confirmation got lost and a revocation
    # are entirely different situations; only the first may self-heal.
    # A local gesture (QR display for the auto-share case, or a fresh
    # pair/accept) clears the tombstone.
    try:
        if _peers.is_unpair_tombstoned(actual_peer_id):
            already = _peers.get_peer(actual_peer_id) is not None
            if not already:
                print(f'[lan-listener] hello from '
                      f'{actual_peer_id[:8]!r} ({device_name!r}) '
                      f'REFUSED: this device was unpaired here — '
                      f're-pair to reconnect',
                      file=sys.stderr, flush=True)
                return _json_response(
                    start_response, '200 OK',
                    {'ok': False, 'error': 'unpaired_here'})
    except Exception as ex:
        print(f'[lan-listener] tombstone check raised: {ex!r}',
              file=sys.stderr, flush=True)
    _peers.record_pair(actual_peer_id, actual_fp,
                       device_name, incoming_endpoint)
    # Their hello proves they hold this pairing, so it's mutual from
    # here (0.54.97) — this is the receiving half of the heal.
    try:
        _peers.set_pair_confirmed(actual_peer_id, True)
    except Exception:
        pass
    # The address it arrived from beats the one it claims (0.54.99).
    _note_inbound_endpoint(actual_peer_id, environ, payload)
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
    share_refused = ''
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
            # TELL THE SCANNER (0.55.3). This refusal used to be
            # log-only: the phone asked for a langcode by name, got a
            # pair without it, and discovered the consequence three
            # steps later as ``NotGitRepository`` from a clone that
            # could never work — with nothing on screen. The scan is
            # the user's whole gesture; if it didn't grant what it
            # asked for, that belongs in the reply.
            share_refused = langcode_offered
            print(f'[lan-listener] hello from {actual_peer_id[:8]!r} '
                  f'claimed langcode={langcode_offered!r} but no '
                  f'recent QR offer for it; pair recorded, '
                  f'auto-share refused — telling the scanner',
                  file=sys.stderr, flush=True)
    resp_body = {'ok': True, 'peer_id': actual_peer_id}
    if share_refused:
        # The QR's keepalive window (30 s) had lapsed, or its screen
        # was dismissed. Deliberately specific: the fix is "show the
        # project's Share QR again", not anything about the network.
        resp_body['share_refused'] = share_refused
        resp_body['share_refused_reason'] = 'qr_offer_expired'
    resp = _json.dumps(resp_body)
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


def _note_inbound_endpoint(peer_id, environ, payload=None,
                           langcode=''):
    """Promote the address this request ARRIVED from to the head of
    the peer's endpoint list (0.54.99).

    Their source host is proven reachable — packets came from it — so
    it belongs first in the list our fan-out reads head-first. The
    listener port isn't the source port, so pair the arrival host with
    a port we already know: the one they advertise in this payload,
    else the port from an endpoint we already hold for them.

    Best-effort and silent on failure: this is an optimisation of which
    address to try first, never a correctness requirement."""
    try:
        host = str(environ.get('REMOTE_ADDR', '') or '').strip()
        if not host or not peer_id:
            return
        port = ''
        claimed = str((payload or {}).get('endpoint', '') or '')
        if ':' in claimed:
            port = claimed.rsplit(':', 1)[1]
        if not port:
            entry = _peers.get_peer(peer_id) or {}
            for ep in (entry.get('endpoints') or []):
                if ':' in str(ep):
                    port = str(ep).rsplit(':', 1)[1]
                    break
        if not port.isdigit():
            return
        _peers.promote_endpoint(peer_id, f'{host}:{port}')
        _reverse_deliver(peer_id, langcode=langcode)
    except Exception as ex:
        print(f'[lan-listener] endpoint promote raised: {ex!r}',
              file=sys.stderr, flush=True)


def _reverse_deliver_by_addr(environ, path_info):
    """Git-route counterpart of ``_reverse_deliver`` (0.55.10).

    These routes are unauthenticated by design (URL-level ACL, no body
    to claim identity in), so the ONLY signal about who is calling is
    the source address. Match it against paired peers' known endpoints;
    on a hit, treat it exactly like identified contact — look at that
    peer's refs for the project they just asked about, push what we owe,
    fetch what they have.

    Why it matters: without this, serving a fetch taught us nothing, so
    our record of a peer's refs went stale for as long as our own
    dialing failed — producing two devices reporting confidently from
    different hours (field 2026-07-27: "up to date" beside "392 to
    send, 7 hrs old"). The peer is demonstrably present at this
    instant; that is the moment to look.

    Heuristic and deliberately powerless: a match only triggers work we
    would do for that peer anyway. It grants no access and changes no
    allowlist, so a wrong guess costs one cheap sweep, not a
    permission."""
    try:
        host = str(environ.get('REMOTE_ADDR', '') or '').strip()
        if not host:
            return
        norm = str(path_info or '').lstrip('/')
        langcode = (norm.split('.git', 1)[0] if '.git' in norm
                    else norm.split('/', 1)[0])
        if not langcode:
            return
        for peer in (_peers.list_peers() or []):
            for ep in (list(peer.get('endpoints') or [])
                       + list(peer.get('static_endpoints') or [])):
                if str(ep).rsplit(':', 1)[0] == host:
                    _reverse_deliver(peer.get('peer_id', ''),
                                     langcode=langcode)
                    return
    except Exception as ex:
        print(f'[lan-listener] reverse-by-address raised: {ex!r}',
              file=sys.stderr, flush=True)


def _reverse_deliver(peer_id, langcode=''):
    """"Do I owe this peer anything?" — asked the moment they reach us
    (0.55.5). Scoped to *langcode* when the inbound request names one.

    Kent 2026-07-27: *"would it not be possible to ask, when receiving
    something from a peer: do I have anything for this peer? and if so,
    immediately send it on the address that we know is good?"* Yes, and
    it is the missing half of 0.54.99: that change promotes the address
    a peer just proved reachable, but nothing acted on it.

    Why it matters: reachability is routinely ONE-WAY. A phone whose
    stored address for us is stale (or which can't route to us at all)
    keeps failing its own fan-out — while its inbound requests arrive
    here perfectly. Convergence then waits on the broken direction. But
    at this instant we hold a proven-good address for them, so the
    working direction can carry BOTH directions' data: we push what we
    owe, and ``_push_to_peer`` fetches + merges when they're ahead.

    With a *langcode*, push exactly that project: the peer just told us
    which one they care about, so catching up the rest can wait for the
    ordinary sweep. Without one (a bare hello), fall back to
    ``sweep_peer``, which is debounced per peer and no-ops cheaply when
    they're already current. Either way on a thread, so a listener
    response never waits on git.

    **Rate-limited per (peer, project), and capped in flight
    (0.55.13).** 0.55.10 wired this to the git routes via
    ``_reverse_deliver_by_addr`` with no limiter on the langcode-scoped
    path — ``sweep_peer``'s per-peer debounce covers only the bare-hello
    fallback. Every ``info/refs`` probe therefore spawned a thread that
    dialed the peer; our dial made them serve us, their listener's
    reverse delivery dialed back, and ours fired again. Field
    2026-07-27: ~330 threads in ~10 s on one tablet, every delivery a
    no-op (``already at 'c63ce9251ae5'``), both sides SSLEOFing, and the
    ``:provider`` process dying of exhaustion — twice, on two devices.
    Convergence needs ONE look per contact burst, not one per HTTP
    request, so the cooldown costs nothing real."""
    key = (str(peer_id or ''), str(langcode or ''))
    now = _time.time()
    with _reverse_gate_lock:
        last = _reverse_last_at.get(key, 0.0)
        if now - last < _REVERSE_COOLDOWN_S:
            return
        if _reverse_inflight[0] >= _REVERSE_MAX_INFLIGHT:
            print(f'[lan-listener] reverse delivery for '
                  f'{key[0][:8]!r} skipped — '
                  f'{_reverse_inflight[0]} already in flight',
                  file=sys.stderr, flush=True)
            return
        # Stamp at ADMISSION, not completion: a slow delivery must not
        # leave the gate open for a burst behind it.
        _reverse_last_at[key] = now
        _reverse_inflight[0] += 1
        if len(_reverse_last_at) > 512:
            for k, t in list(_reverse_last_at.items()):
                if now - t > _REVERSE_COOLDOWN_S * 10:
                    _reverse_last_at.pop(k, None)

    def _work():
        try:
            from . import lan_push as _lan_push
            if langcode:
                from . import projects as _proj
                project = _proj.get(langcode)
                if project is None:
                    return
                print(f'[lan-listener] reverse delivery: checking '
                      f'{langcode!r} for {peer_id[:8]!r} on the '
                      f'address they just reached us from',
                      file=sys.stderr, flush=True)
                entry = _peers.get_peer(peer_id)
                if entry is not None:
                    _lan_push._push_to_peer(project, entry)
                return
            _lan_push.sweep_peer(peer_id)
        except Exception as ex:
            print(f'[lan-listener] reverse delivery for '
                  f'{peer_id[:8]!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
        finally:
            with _reverse_gate_lock:
                _reverse_inflight[0] = max(0, _reverse_inflight[0] - 1)
    try:
        threading.Thread(target=_work, daemon=True,
                         name='lan-reverse-deliver').start()
    except Exception as ex:
        # Never leak the slot the gate above reserved — a spawn failure
        # would otherwise permanently shrink the in-flight budget.
        with _reverse_gate_lock:
            _reverse_inflight[0] = max(0, _reverse_inflight[0] - 1)
        print(f'[lan-listener] reverse delivery thread raised: '
              f'{ex!r}', file=sys.stderr, flush=True)


def _paired_claimant(payload):
    """Resolve the body-auth identity claim in *payload* to a PAIRED
    peer record. Returns ``(peer_id, entry)`` or ``(None, None)``.

    Stricter than the share/hello signalling handlers, which accept
    unpaired callers because pairing is what they're establishing:
    diagnostics-pull and remote-restart are privileged operations, so
    the claimant must ALREADY be paired (a prior QR gesture on this
    device — the consent boundary) and the claimed ``fp`` must match
    the fingerprint we recorded for them. Same body-auth threat model
    as the rest of the listener (identity asserted in the body under
    encrypted-but-unauthenticated TLS; see ``_build_server``)."""
    peer_id = str((payload or {}).get('peer_id', '') or '')
    claimed_fp = str((payload or {}).get('fp', '') or '')
    if len(peer_id) != 64 or len(claimed_fp) != 64:
        return None, None
    try:
        entry = _peers.get_peer(peer_id)
    except Exception as ex:
        print(f'[lan-listener] peer lookup raised: {ex!r}',
              file=sys.stderr, flush=True)
        return None, None
    if entry is None:
        return None, None
    if str(entry.get('fp', '') or '') != claimed_fp:
        return None, None
    return peer_id, entry


def _handle_diagnostics_pull(environ, start_response):
    """``POST /v1/lan/diagnostics_pull`` — serve this device's
    diagnostics bundle to a PAIRED peer over the LAN/cable link
    (0.54.74).

    Why this route exists (field, Kent 2026-07-25): the standard
    Share-diagnostics button needs the owner's server UI to be OPEN,
    which in the field means booting their UI and interrupting their
    work — on someone else's computer, with their tools. Pulling
    inverts it: the technician plugs in, taps once on THEIR OWN
    device, and walks away with the bundle. Pairing was the owner's
    consent gesture; nothing here needs a second one.

    Auth: paired-peer body claim (``_paired_claimant``). Response is
    the raw ``.tar.gz`` with ``X-AZT-Archive-Name`` naming it — the
    same artifact the owner's own Share button produces.

    Deliberately lock-free (see ``server.stage_diagnostics_bundle``):
    the point is to get logs OUT of a wedged daemon."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id, entry = _paired_claimant(payload)
    if peer_id is None:
        print('[lan-listener] diagnostics_pull refused: claimant '
              'not paired (or fp mismatch)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'not_paired'})
    try:
        from . import server as _server
        # Include what we're carrying from OTHER peers, but not the
        # requester's own bundle echoed back (0.54.78) — this is the
        # courier path: a phone that pulled from one machine serves
        # those logs onward to the next, over whatever link is
        # already up.
        staged = _server.stage_diagnostics_bundle(
            exclude_slug=_server.device_slug(
                entry.get('device_name', '')))
    except Exception as ex:
        print(f'[lan-listener] diagnostics_pull staging raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return _json_response(start_response,
                              '500 Internal Server Error',
                              {'ok': False,
                               'error': 'stage_failed'})
    try:
        with open(staged['archive_path'], 'rb') as fh:
            data = fh.read()
    except OSError as ex:
        print(f'[lan-listener] diagnostics_pull read raised: {ex!r}',
              file=sys.stderr, flush=True)
        return _json_response(start_response,
                              '500 Internal Server Error',
                              {'ok': False, 'error': 'read_failed'})
    from azt_collab_client.diagnostics import DIAGNOSTICS_MIME
    start_response('200 OK', [
        ('Content-Type', DIAGNOSTICS_MIME),
        ('Content-Length', str(len(data))),
        ('X-AZT-Archive-Name', staged['archive_name']),
    ])
    print(f'[lan-listener] diagnostics_pull served '
          f'{staged["archive_name"]!r} ({len(data)} bytes) to '
          f'{peer_id[:8]!r} ({entry.get("device_name", "")!r})',
          file=sys.stderr, flush=True)
    return [data]


def _handle_restart_daemon(environ, start_response):
    """``POST /v1/lan/restart_daemon`` — a paired peer asks this
    daemon to restart itself (0.54.74). The remote leg of wedge
    recovery: in the field the fix for a wedged daemon was opening
    the owner's settings UI and tapping Restart server, which is
    exactly the interruption the pull workflow exists to avoid.

    Reaches the **wedged-alive** class only — this listener thread
    responds while scheduler threads are stuck on ``project_lock`` /
    network I/O, which is the common field wedge. A fully dead
    daemon has no listener; desktop clients auto-respawn on their
    next poll and Android's ContentProvider contract lazy-spawns,
    so that case self-heals without us.

    Restart cost is low by design (jobs → typed ``JOB_INTERRUPTED``
    for peer retry, transfers retried by the sender, uncommitted
    bytes power-cut-contained, listener re-binds its previous port,
    backoff curves deliberately survive). Auth: paired-peer body
    claim; the same suite-signature/pairing boundary that already
    authorizes fetch + merge here."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id, entry = _paired_claimant(payload)
    if peer_id is None:
        print('[lan-listener] restart_daemon refused: claimant not '
              'paired (or fp mismatch)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'not_paired'})
    print(f'[lan-listener] restart_daemon requested by '
          f'{peer_id[:8]!r} ({entry.get("device_name", "")!r})',
          file=sys.stderr, flush=True)

    def _restart_after_response():
        # Same shape as ``server._h_admin_restart``: let the response
        # flush before the process goes away, then reuse that
        # handler's platform-correct teardown (execv on desktop,
        # os._exit under Android's :provider).
        _time.sleep(0.5)
        try:
            from . import server as _server
            _server._h_admin_restart({})
        except Exception as ex:
            print(f'[lan-listener] remote restart raised: {ex!r}',
                  file=sys.stderr, flush=True)

    threading.Thread(target=_restart_after_response,
                     name='lan-remote-restart', daemon=True).start()
    return _json_response(start_response, '200 OK',
                          {'ok': True, 'restarting': True})


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
    # DECLINED-SHARE SUPPRESSION (0.54.98). This handler used to
    # re-stash an offer on every inbound POST, so a decline only stuck
    # while we could reach the sender to nack it — and the case where
    # we can't (one-way reachability: they reach us, we can't reach
    # them) is precisely the case that keeps re-offering. The offer
    # came back on the sender's next burst, forever. Now a declined
    # (peer, langcode) is dropped here and the nack is re-attempted;
    # the sender rolls back its own allowlist when that lands. Also
    # the prerequisite for the arrival-time share heal — without it,
    # automatic re-offers would make an unwanted offer permanent.
    try:
        if _peers.is_declined_share(peer_id, langcode):
            print(f'[lan-listener] share-offer for {langcode!r} from '
                  f'{peer_id[:8]!r} DROPPED: declined here — '
                  f're-sending the decline',
                  file=sys.stderr, flush=True)
            try:
                from . import lan_push as _lan_push
                _lan_push.share_declined(peer_id, langcode)
            except Exception as ex:
                print(f'[lan-listener] re-nack raised: {ex!r}',
                      file=sys.stderr, flush=True)
            return _json_response(start_response, '200 OK',
                                  {'ok': True,
                                   'dispatch': 'declined_here'})
    except Exception as ex:
        print(f'[lan-listener] decline-suppression check raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
    # Their offer proves this project IS in their allowlist for us, so
    # our side of the share is confirmed mutual (0.54.98).
    try:
        _peers.set_share_confirmed(peer_id, langcode, True)
    except Exception:
        pass
    # Scoped to the project they just named (0.55.5): they told us
    # which one they care about, and we now hold an address that
    # provably reaches them.
    _note_inbound_endpoint(peer_id, environ, payload,
                           langcode=langcode)
    try:
        local_proj = _projects.get(langcode)
    except Exception:
        local_proj = None
    # Ghost guard (0.54.63): a registry record whose working tree is
    # GONE (forget-with-delete residue, wiped dir, interrupted clone)
    # must not swallow re-offers forever. Without this, every
    # re-share from the owner dispatched to a silent branch
    # (no_url/noop/adopted) because "the project is registered" —
    # while the picker showed nothing and the user saw no offer
    # (field 2026-07-24: desktop 'shared' + board 'awaiting first
    # sync', client shows neither project nor offer). This handler
    # runs in the daemon process, which owns the files, so the disk
    # check is legitimate here (unlike in peer code). Heal = forget
    # the ghost record, then fall through to the normal
    # stash-clone-offer path.
    if local_proj is not None:
        _wd = str(getattr(local_proj, 'working_dir', '') or '')
        if not _wd or not os.path.isdir(os.path.join(_wd, '.git')):
            print(f'[lan-listener] share-offer for {langcode!r}: '
                  f'registry record is a GHOST (working tree '
                  f'missing at {_wd!r}) — forgetting record, '
                  f'treating offer as fresh', file=sys.stderr,
                  flush=True)
            try:
                from . import repo as _repo_mod
                _repo_mod.forget_project(langcode, delete_files=False)
            except Exception as ex:
                print(f'[lan-listener] ghost forget for '
                      f'{langcode!r} failed: {ex!r}',
                      file=sys.stderr, flush=True)
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
    from .repo import wan_url as _wan_url
    if _wan_url(local_url) == _wan_url(repo_url):
        # Compare wan-normalized so ``git@github.com:o/r.git`` and
        # ``https://github.com/o/r.git`` — the SAME repo in two
        # spellings — never surface as a remote-conflict decision
        # (field repro 2026-07-21: baf, phone popped "two remotes"
        # over one repo). Each side keeps its own stored spelling.
        spelling = ('' if local_url == repo_url
                    else ' (same repo, different spelling)')
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r}: remote_url matches local'
              f'{spelling}; no-op',
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
    # The address this request actually ARRIVED from (0.54.96). Packets
    # came from there, so it is PROVEN reachable — unlike the
    # body-claimed ``endpoint``, which is only the sender's guess about
    # its own address and is routinely wrong on a multi-homed host
    # (field 2026-07-27: a desktop with four tether subnets plus wifi
    # advertised one address the phone couldn't reach, so the accepter's
    # hello-back and pair_response both failed and the pairing went
    # one-sided). REMOTE_ADDR carries the ephemeral SOURCE port, not
    # the listener port, so the resolver pairs this host with the
    # advertised port.
    from_addr = str(environ.get('REMOTE_ADDR', '') or '')
    endpoints = [str(e) for e in (payload.get('endpoints') or [])
                 if e]
    _pending.add(_pending.KIND_PAIR_REQUEST, {
        'peer_id': peer_id, 'fp': fp,
        'device_name': device_name, 'endpoint': endpoint,
        'endpoints': endpoints, 'from_addr': from_addr,
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
        # Privileged paired-peer routes (0.54.74): unlike the
        # signalling endpoints above, these require an ALREADY-paired
        # claimant (``_paired_claimant``).
        if (method == 'POST'
                and path_info == '/v1/lan/diagnostics_pull'):
            return _handle_diagnostics_pull(environ, start_response)
        if (method == 'POST'
                and path_info == '/v1/lan/restart_daemon'):
            return _handle_restart_daemon(environ, start_response)
        # Non-signalling fallthrough: dulwich.web's git smart-
        # protocol app. URL-level ACL is handled at backend-build
        # time — ``_build_dict_backend`` only mounts projects that
        # appear in at least one paired peer's ``shared_projects``,
        # so the dulwich app simply returns 404 for a URL outside
        # that set. Future-harden by re-adding the per-peer ACL
        # once client identity is signature-verified in the body.
        # Log who is asking — TLS gives no per-request identity, so
        # the remote address is the only requester signal we have,
        # and without it a serve of ``/X.git`` is unattributable in
        # the daemon log (field, 2026-07-17: could not tell which of
        # two paired machines fetched a project).
        # Identify the fetcher by ARRIVAL ADDRESS and look at them in
        # return (0.55.10). The git smart-protocol routes carry no
        # identity — no body, and TLS is CERT_NONE — so a peer fetching
        # from us taught us nothing, and our knowledge of THEIR refs
        # could only advance when WE managed to dial THEM. Field
        # 2026-07-27: a phone reporting "up to date" (fresh observation,
        # it can reach the tablet) beside a tablet reporting "392 to
        # send" stamped 7 h old — the tablet had served the phone's
        # fetches all along and never got to look back. Matching
        # REMOTE_ADDR against paired peers' endpoints is a heuristic
        # (NAT, shared hosts), so it only ever triggers the same
        # look-and-deliver we already do on identified contact; it
        # grants nothing and changes no ACL.
        _reverse_deliver_by_addr(environ, path_info)
        print(f'[lan-listener] {method} {path_info} from '
              f'{environ.get("REMOTE_ADDR", "?")}',
              file=sys.stderr, flush=True)
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

    def _is_peer_disconnect(ex):
        """True for the exception shapes a peer vanishing
        mid-transfer produces: raw socket resets, or dulwich's
        ``GitProtocolError`` wrapping one (raised ``from`` the socket
        error, so ``__cause__`` carries the original — field
        2026-07-24: upload-pack negotiation reset when the phone
        hopped networks printed a 40-line double traceback)."""
        if isinstance(ex, (ConnectionResetError, BrokenPipeError)):
            return True
        cause = getattr(ex, '__cause__', None)
        return isinstance(cause,
                          (ConnectionResetError, BrokenPipeError))

    def _wrapped(environ, start_response):
        method = environ.get('REQUEST_METHOD', '')
        path = environ.get('PATH_INFO', '')
        m = (_POST_RECEIVE_PATH_RE.match(path)
             if method == 'POST' else None)
        if m is None:
            # Non-receive routes (upload-pack serving a peer's fetch,
            # info/refs, …) get the same disconnect-absorbing wrap as
            # the receive path below: a peer that vanishes mid-
            # transfer is ONE log line, not a traceback. Anything
            # that isn't a disconnect re-raises untouched.
            result = inner_app(environ, start_response)

            def _absorb():
                try:
                    for chunk in result:
                        yield chunk
                except Exception as ex:
                    if not _is_peer_disconnect(ex):
                        raise
                    print(f'[lan-listener] {path!r} interrupted '
                          f'(peer disconnected mid-transfer; they '
                          f'will retry): {ex!r}',
                          file=sys.stderr, flush=True)
                finally:
                    if hasattr(result, 'close'):
                        try:
                            result.close()
                        except Exception:
                            pass
            return _absorb()

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
            except (ValueError, ConnectionError, OSError) as ex:
                # A peer that disconnects mid-push (screen off, walked
                # out of range, killed) leaves a truncated chunked
                # body; dulwich's reader surfaces it as
                # ``ValueError: invalid literal for int() with base
                # 16: b''`` and wsgiref printed the raw 15-frame
                # traceback (field 2026-07-23). Transient by design —
                # the sender still holds the data and re-pushes; one
                # line is the whole story.
                print(f'[lan-listener] {langcode!r} receive '
                      f'interrupted (peer disconnected mid-transfer; '
                      f'sender will retry): {ex!r}',
                      file=sys.stderr, flush=True)
            except Exception as ex:
                # dulwich wraps socket errors in GitProtocolError
                # (raised ``from`` the original, so __cause__ carries
                # it) — absorb only the disconnect class; anything
                # else re-raises untouched (0.54.68).
                if not _is_peer_disconnect(ex):
                    raise
                print(f'[lan-listener] {langcode!r} receive '
                      f'interrupted (peer disconnected mid-transfer; '
                      f'sender will retry): {ex!r}',
                      file=sys.stderr, flush=True)
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

        def handle_error(self, request, client_address):
            # Quiet expected connection-lifecycle noise: peers
            # abandon pooled connections and abort uploads as part
            # of normal LAN churn (field 2026-07-21: a 30-line
            # traceback per event buried the errors that mattered).
            # One summary line for those classes; full traceback
            # for anything genuinely unexpected.
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError,
                                BrokenPipeError,
                                ConnectionAbortedError,
                                TimeoutError,
                                ssl.SSLEOFError,
                                ssl.SSLError)):
                print(f'[lan-listener] connection from '
                      f'{client_address} dropped mid-request: '
                      f'{exc!r}', file=sys.stderr, flush=True)
                return
            super().handle_error(request, client_address)

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
        def _fail(step, ex):
            # Same per-step attribution as before, plus a typed copy
            # the toggle RPC can hand the UI so the user sees WHICH
            # step failed instead of "see the daemon log" (0.54.75).
            print(f'[lan-listener] {step} failed: {ex!r}',
                  file=sys.stderr, flush=True)
            with _LOCK:
                _STATE['bind_error'] = f'{step}: {ex!r}'[:200]
        try:
            _lan_fgs.acquire_wifi_locks()
        except Exception as ex:
            _fail('acquire_wifi_locks', ex)
            return
        try:
            _lan_fgs.start_fgs()
        except Exception as ex:
            _fail('start_fgs', ex)
            _lan_fgs.release_wifi_locks()
            return
        try:
            bound = start()
        except Exception as ex:
            _fail('listener bind', ex)
            _lan_fgs.stop_fgs()
            _lan_fgs.release_wifi_locks()
            return
        with _LOCK:
            _STATE['bind_error'] = ''
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
