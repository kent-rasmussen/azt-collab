"""
LAN fan-out push (parked design phase 6).

After every drain pass's github push attempt, the scheduler calls
``fan_out(project)`` here to opportunistically deliver the same
commits to every paired peer that has this project in its
``shared_projects`` and a known endpoint (mDNS-resolved or static).

LAN delivery is **opportunistic redundancy**: success here does
NOT clear the ``pending_push`` flag. Only a successful github push
does (the suite stays github-authoritative; LAN is the cheap
sneakernet that helps two phones in the same room without
re-running the whole flow over a metered link). Per the spec's
"GitHub convergence" property.

TLS pinning: we trust *only* the fingerprint recorded in
``peers.json`` for each peer. A pinned-fingerprint mismatch fires
``S.LAN_FP_MISMATCH`` (logged here; surfaced peer-side by a future
listener-side hello-handshake handler).

Per-target failure is logged but never raised — a slow / killed
peer can't take down the github drain it rides alongside.
"""

from __future__ import annotations

import hashlib
import ssl
import sys
import tempfile

from . import lan_discovery as _lan_discovery
from . import peer_id as _peer_id
from . import peers as _peers
from . import status as S


def _resolve_endpoint(peer_entry):
    """Endpoint resolution order per the spec: mDNS-cached → static
    endpoints → QR-hint endpoint. Returns ``(host, port)`` or
    ``None``."""
    pid = peer_entry.get('peer_id', '')
    mdns = _lan_discovery.get_endpoint(pid) if pid else None
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


def _build_ssl_context(expected_fp):
    """Build a client-side SSL context that authenticates the peer's
    cert by *fingerprint* rather than CA chain.

    We can't use ``ctx.set_verify`` with a callback that consults the
    peer's cert because Python's ``ssl`` doesn't expose a verify
    callback at the application layer. Instead we leave the context
    in unverified mode and check the fingerprint on the resulting
    socket after handshake (caller's job — passes the context into
    dulwich's HttpGitClient and inspects the connection)."""
    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        raise RuntimeError('this daemon has no LAN identity '
                           '(cryptography unavailable?)')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


def _verify_fingerprint(ssl_sock, expected_fp, peer_id):
    """Compare the SHA-256 of the peer's DER cert to *expected_fp*.
    Returns True on match, logs+False on mismatch."""
    der = ssl_sock.getpeercert(binary_form=True)
    if not der:
        print(f'[lan-push] no peer cert from {peer_id[:8]!r}; '
              f'refusing', file=sys.stderr, flush=True)
        return False
    got = hashlib.sha256(der).hexdigest()
    if got != expected_fp:
        print(f'[lan-push] {S.LAN_FP_MISMATCH} for {peer_id[:8]!r}: '
              f'expected={expected_fp[:16]!r} got={got[:16]!r}',
              file=sys.stderr, flush=True)
        return False
    return True


def _push_to_peer(project, peer_entry):
    """Single push attempt against one paired peer. Returns
    ``True`` on success, ``False`` on any failure. Logs detail
    rather than raising."""
    from dulwich import porcelain
    pid = peer_entry.get('peer_id', '')
    expected_fp = peer_entry.get('fp', '')
    endpoint = _resolve_endpoint(peer_entry)
    if endpoint is None:
        print(f'[lan-push] no endpoint for {pid[:8]!r}; skipping',
              file=sys.stderr, flush=True)
        return False
    host, port = endpoint
    url = f'https://{host}:{port}/{project.langcode}.git'
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-push] context build failed for {pid[:8]!r}: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return False
    # dulwich's HttpGitClient uses urllib3 underneath; constructing
    # a urllib3 PoolManager with our custom SSL context is the
    # documented seam for client-side TLS knobs. Fingerprint
    # verification happens through urllib3's assert_fingerprint
    # (formatted as "sha256:HEXSTRING") — that catches a
    # cert mismatch at handshake completion.
    try:
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=f'sha256:{expected_fp}',
        )
    except Exception as ex:
        print(f'[lan-push] urllib3 pool manager failed for '
              f'{pid[:8]!r}: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    try:
        porcelain.push(
            project.working_dir,
            remote_location=url,
            refspecs=[b'HEAD:refs/heads/main'],
            pool_manager=pm,
        )
    except TypeError:
        # Older dulwich without pool_manager kwarg — fall back to
        # the version that constructs its own pool. We lose
        # fingerprint pinning in this branch; log loudly so the user
        # knows to upgrade dulwich.
        print(f'[lan-push] dulwich does not support pool_manager= '
              f'kwarg; refusing unpinned push to {pid[:8]!r}',
              file=sys.stderr, flush=True)
        return False
    except Exception as ex:
        print(f'[lan-push] push to {pid[:8]!r} at {host}:{port} '
              f'failed: {ex!r}', file=sys.stderr, flush=True)
        return False
    print(f'[lan-push] pushed {project.langcode!r} → '
          f'{pid[:8]!r} at {host}:{port}',
          file=sys.stderr, flush=True)
    return True


def hello_to_peer(host, port, expected_fp, device_name=''):
    """Initiate a TLS hello handshake to *host*:*port*, pinning
    *expected_fp*, and POST our identity to ``/v1/lan/hello`` so the
    remote daemon auto-reverse-records us.

    Returns ``True`` on success, ``False`` on any failure. Logs
    detail; never raises.

    Called from the daemon's ``_h_lan_pair_accept`` right after a
    successful QR-scan recording, so the remote side doesn't need a
    separate QR scan in the other direction (parked spec § Pairing
    step 5)."""
    import json
    try:
        from . import peer_id as _peer_id_mod
        ident = _peer_id_mod.ensure()
    except Exception as ex:
        print(f'[lan-hello] our identity unavailable: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-hello] context build failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    try:
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=f'sha256:{expected_fp}',
        )
    except Exception as ex:
        print(f'[lan-hello] urllib3 pool manager failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    body = json.dumps({
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'device_name': device_name,
    }).encode('utf-8')
    url = f'https://{host}:{int(port)}/v1/lan/hello'
    try:
        resp = pm.request(
            'POST', url, body=body,
            headers={'Content-Type': 'application/json'},
            timeout=urllib3.Timeout(connect=5, read=10),
            retries=False,
        )
    except Exception as ex:
        print(f'[lan-hello] POST to {host}:{port} failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    if resp.status != 200:
        print(f'[lan-hello] {host}:{port} returned status '
              f'{resp.status}: {resp.data!r}',
              file=sys.stderr, flush=True)
        return False
    print(f'[lan-hello] auto-reverse-recorded on {host}:{port}',
          file=sys.stderr, flush=True)
    return True


def fan_out(project):
    """Push ``project`` to every paired peer that has its langcode
    in ``shared_projects`` and an in-memory or static endpoint.

    Returns a dict ``{peer_id: bool}`` of per-target outcomes —
    callers may log the summary, but the daemon's scheduler treats
    LAN delivery as opportunistic and does not clear pending_push
    based on it.

    Safe to call from any thread; per-peer failures are isolated."""
    out = {}
    for entry in _peers.list_peers():
        pid = entry.get('peer_id', '')
        if not pid:
            continue
        if project.langcode not in (entry.get('shared_projects') or []):
            continue
        out[pid] = _push_to_peer(project, entry)
    return out
