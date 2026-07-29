"""
Talk to ANOTHER device's daemon over its LAN listener (0.55.117).

This is what makes "settings at a distance" work: point the ordinary
settings UI at a peer instead of at the local daemon, and every RPC
behaves as it does locally. There is no per-endpoint work here and none
is wanted — Kent, on a first design that forwarded a whitelist of config
calls: *"if it's all the same, I'd rather have the same functions, just
to keep it simple."*

**This module is deliberately pure plumbing.** It builds the envelope and
nothing else. Signing and the TLS dial are INJECTED, for two reasons:

- hard rule #3, no ``azt_collabd`` import from this package — the private
  key, ``peers.json`` and the pinned pool manager all live daemon-side;
- the existing LAN dials pin the peer's certificate by SHA-256
  fingerprint (``urllib3`` ``assert_fingerprint``). A second, unpinned
  HTTPS path here would be strictly weaker than the code beside it, for
  no benefit. The injected dialer is the same pinned one ``lan_push``
  uses.

**How a call is authorised** (daemon half: ``lan_listener._handle_admin``):

1. ``GET /v1/lan/challenge`` for a single-use nonce — fresh per call, so
   a captured request cannot be replayed.
2. Sign ``{nonce, method, path, body}`` as canonical JSON. The signature
   covers the REQUEST, not just the nonce, so a valid signature cannot be
   lifted onto a different call.
3. ``POST /v1/lan/admin`` with ``{peer_id, nonce, sig, method, path,
   body}``.

The target requires all of: nonce outstanding, signature verifying
against ``peer_id`` (which IS the raw ed25519 pubkey), and an explicit
per-peer ``admin`` grant that pairing alone does not confer.

**The canonical form must match the daemon byte for byte.** Both sides
use ``json.dumps(..., sort_keys=True, separators=(',', ':'))``. Drift
shows up only as a 403 that looks exactly like a missing grant, so
``self_check()`` below compares the two implementations directly.
"""

import json

from . import Transport, ServerUnavailable

_DEFAULT_TIMEOUT = 300


def canonical_request(nonce, method, path, body):
    """The exact bytes both sides sign. Must stay identical to
    ``azt_collabd.lan_listener._admin_canonical_request``."""
    return json.dumps(
        {'nonce': nonce, 'method': method, 'path': path, 'body': body},
        sort_keys=True, separators=(',', ':')).encode('utf-8')


class LanAdminTransport(Transport):
    """Routes every call through a peer's ``/v1/lan/admin`` endpoint.

    *dial* is ``fn(method, path, payload, timeout) -> dict`` and must
    perform a fingerprint-pinned HTTPS request to the peer;
    ``azt_collabd.lan_admin_client.make_transport`` supplies one.
    *sign* is ``fn(bytes) -> hex str``.
    """

    def __init__(self, peer_id, my_peer_id, dial, sign,
                 device_name=''):
        self.peer_id = str(peer_id)
        self.my_peer_id = str(my_peer_id)
        self.device_name = str(device_name or '')
        self._dial = dial
        self._sign = sign

    def _challenge(self, timeout):
        data = self._dial('GET', '/v1/lan/challenge', None,
                          min(timeout, 15))
        nonce = str((data or {}).get('nonce', '') or '')
        if not nonce:
            # Answering, but not on a version that has the endpoint.
            # Distinct from unreachable, and from refused.
            raise ServerUnavailable(
                f'peer {self.peer_id[:8]} issued no challenge — it is '
                f'reachable but too old for remote settings', kind='http')
        return nonce

    def health(self):
        """Liveness only. Deliberately does NOT prove we may administer
        this peer: a peer can be perfectly healthy and still refuse every
        call for want of a grant, and reporting that as "connected" would
        promise a session that can do nothing."""
        try:
            self._challenge(timeout=5)
            return {'ok': True, 'peer_id': self.peer_id}
        except ServerUnavailable:
            raise
        except Exception as ex:
            raise ServerUnavailable(
                f'peer {self.peer_id[:8]} unreachable: {ex!r}',
                kind='http')

    def call(self, method, path, body=None, timeout=_DEFAULT_TIMEOUT):
        try:
            nonce = self._challenge(timeout)
            sig = self._sign(canonical_request(nonce, method, path, body))
        except ServerUnavailable:
            raise
        except Exception as ex:
            raise ServerUnavailable(
                f'peer {self.peer_id[:8]}: challenge failed: {ex!r}',
                kind='http')
        if not sig:
            raise ServerUnavailable(
                'this device has no LAN identity to sign with — no '
                'peer_id / private key, so no peer can verify us',
                kind='http')
        try:
            return self._dial('POST', '/v1/lan/admin', {
                'peer_id': self.my_peer_id,
                'nonce': nonce,
                'sig': sig,
                'method': method,
                'path': path,
                'body': body,
            }, timeout)
        except ServerUnavailable:
            raise
        except Exception as ex:
            raise ServerUnavailable(
                f'peer {self.peer_id[:8]}: {ex!r}', kind='http')

    def close(self):
        pass


def self_check():
    """Prove this module and the daemon canonicalise identically.

    Cheap insurance against the one failure mode with no useful
    diagnostic: canonicalisation drift surfaces only as a 403 from the
    peer, indistinguishable from a missing admin grant. Only meaningful
    in a process where the daemon package is importable (desktop).

    Returns ``(ok, detail)``; never raises."""
    try:
        from azt_collabd.lan_listener import _admin_canonical_request
    except Exception as ex:
        return False, f'daemon side not importable here: {ex!r}'
    probe = ('abc', 'POST', '/v1/x', {'b': 1, 'a': [2, 3]})
    mine = canonical_request(*probe)
    theirs = _admin_canonical_request(*probe)
    if mine == theirs:
        return True, 'canonical form matches'
    return False, f'DRIFT: client={mine!r} daemon={theirs!r}'
