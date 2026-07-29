"""
Drive a peer's daemon by relaying through OUR OWN daemon (0.55.127).

Needed because of where the secrets live. Signing a remote admin request
requires this device's ed25519 private key, and dialling the peer requires
its pinned certificate fingerprint from ``peers.json`` — both daemon-side.
On Android the daemon is a different process (`:provider`), so the UI
cannot borrow them even in principle; on desktop it could import them, but
this package may not import ``azt_collabd`` at all (hard rule #3).

So the UI asks its own daemon to make the call:

    UI ──(local RPC)──▶ our daemon ──(signed, pinned HTTPS)──▶ peer daemon

One relay endpoint, ``POST /v1/lan/relay``, carrying the same
``{method, path, body}`` envelope the peer's ``/v1/lan/admin`` expects.
Nothing here handles keys, TLS, or fingerprints — that is the point.

Desktop uses a different path (`ui --peer`, which builds the transport in
the daemon package directly) because it can spawn a second window and keep
the two machines visibly separate. This relay is what makes the SAME
capability reachable on Android, where a second window cannot exist.
"""

from . import Transport, ServerUnavailable

_DEFAULT_TIMEOUT = 300


class RelayTransport(Transport):
    """Every call is forwarded by our own daemon to the target peer."""

    def __init__(self, peer_id, device_name=''):
        self.peer_id = str(peer_id)
        self.device_name = str(device_name or '')

    def _relay(self, method, path, body, timeout):
        # ``pick_local()``, NOT ``rpc.call`` (0.55.130). ``rpc.call`` asks
        # ``pick_transport()`` for "the current transport" — which, once
        # this process is targeted at a peer, is THIS object. It recursed
        # into itself until ``RecursionError: maximum recursion depth
        # exceeded``. The relay exists to hand a request to the LOCAL
        # daemon, so it must name local explicitly.
        from . import pick_local
        resp = pick_local().call('POST', '/v1/lan/relay', {
            'peer_id': self.peer_id,
            'method': method,
            'path': path,
            'body': body,
        }, timeout)
        if not isinstance(resp, dict):
            raise ServerUnavailable(
                f'relay to {self.peer_id[:8]} returned no answer',
                kind='http')
        if resp.get('relay_error'):
            # The peer refused or was unreachable. Surface its words —
            # they name which gate declined, or which addresses failed.
            raise ServerUnavailable(
                str(resp.get('relay_error'))[:300], kind='http')
        return resp.get('relayed') or {}

    def health(self):
        return self._relay('GET', '/v1/health', None, 20)

    def call(self, method, path, body=None, timeout=_DEFAULT_TIMEOUT):
        return self._relay(method, path, body, timeout)

    def close(self):
        pass


def build(peer_id):
    """Return a ``RelayTransport`` for *peer_id*, labelled with its device
    name so a UI banner can say which machine it is driving.

    The name is looked up locally and is cosmetic; a failure to find it
    must not stop the retarget, so it degrades to the short peer id."""
    name = ''
    try:
        from .. import lan_list_peers
        for p in lan_list_peers() or []:
            if str(p.get('peer_id', '')) == str(peer_id):
                name = str(p.get('device_name', '') or '')
                break
    except Exception:
        name = ''
    return RelayTransport(peer_id, name)
