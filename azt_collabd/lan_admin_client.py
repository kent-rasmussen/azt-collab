"""
Build a transport that drives ANOTHER device's daemon (0.55.117).

Daemon-side half of "settings at a distance". Everything that must not
live in the client package lives here: the private key, ``peers.json``,
and the fingerprint-pinned TLS dial. The client's
``transports/lan_admin.py`` holds only envelope construction, and takes
the dial and the signer from this module.

Why pinned rather than unverified: peers already authenticate each other
by SHA-256 certificate fingerprint (``urllib3`` ``assert_fingerprint``),
and ``lan_push`` warns that a falsy fingerprint SILENTLY skips pinning
(0.54.64). A remote-settings channel must not be weaker than the data
channel beside it, so a peer with no recorded ``fp`` is refused outright
rather than dialled unpinned.
"""

import json
import sys

from . import peer_id as _peer_id
from . import peers as _peers


def _endpoints_for(entry):
    """Addresses to try, mDNS-learned first then static (the hotspot-host
    fallback), de-duplicated in order."""
    out = []
    for e in list(entry.get('endpoints') or []) + \
            list(entry.get('static_endpoints') or []):
        e = str(e or '').strip()
        if e and e not in out:
            out.append(e)
    return out


_cache = {}                      # peer_id → (signature, transport)
_cache_lock = None


def make_transport(peer_id):
    """Cached wrapper (0.55.132) — see ``_build_transport``.

    ``/v1/lan/relay`` calls this for EVERY relayed RPC, and a remote
    settings screen polls several endpoints every 5 s across several
    threads. Uncached that meant: a fresh SSL context and urllib3 pool
    manager per call (so no connection reuse, and a TLS handshake each
    time), plus one log line per call — field 2026-07-29 shows ~30 in
    twelve seconds, drowning the log.

    Keyed on the peer's identity AND its address list, so a re-pair or a
    new mDNS address rebuilds rather than being served stale."""
    global _cache_lock
    if _cache_lock is None:
        import threading as _th
        _cache_lock = _th.Lock()
    entry = _peers.get_peer(str(peer_id)) or {}
    sig = (str(entry.get('fp', '') or ''),
           tuple(_endpoints_for(entry)))
    with _cache_lock:
        hit = _cache.get(str(peer_id))
        if hit is not None and hit[0] == sig:
            return hit[1]
    tr = _build_transport(peer_id)
    with _cache_lock:
        _cache[str(peer_id)] = (sig, tr)
    return tr


def _build_transport(peer_id):
    """Return a ``LanAdminTransport`` for *peer_id*, or raise
    ``RuntimeError`` with a reason a person can act on.

    Refuses — rather than degrades — when it cannot establish who it is
    talking to. Every failure here is a configuration fact the operator
    can fix, so each says what to do."""
    from azt_collab_client.transports.lan_admin import LanAdminTransport

    entry = _peers.get_peer(str(peer_id))
    if entry is None:
        raise RuntimeError(
            f'{str(peer_id)[:8]} is not a paired peer — pair with it '
            f'first (QR), then grant remote settings on that device')
    expected_fp = str(entry.get('fp', '') or '')
    if not expected_fp:
        # Do NOT fall back to an unpinned dial. urllib3 skips pinning on
        # a falsy fingerprint, so this would look like it worked while
        # accepting any certificate.
        raise RuntimeError(
            f'{str(peer_id)[:8]} has no recorded certificate fingerprint, '
            f'so its identity cannot be verified — re-pair with it '
            f'rather than connecting unverified')
    mine = _peer_id.peer_id_hex()
    if not mine:
        raise RuntimeError(
            'this device has no LAN identity (is the cryptography '
            'package present?) — nothing can verify our requests')
    endpoints = _endpoints_for(entry)
    if not endpoints:
        raise RuntimeError(
            f'{str(peer_id)[:8]} has no known address — it must be seen '
            f'on this network (or given a static endpoint) first')

    from . import lan_push as _lan_push

    def _dial(method, path, payload, timeout):
        """Try each known address; return the first JSON answer.

        A peer legitimately has several addresses and only one live — the
        same reason ``lan_push`` sweeps candidates. Collect the failures
        so the error names every address tried instead of only the last.

        **Bounded overall, not just per address (0.55.126).** Connect
        timeout is 5 s each, and a peer can easily carry six recorded
        addresses (field: Kent Phone had exactly that), so a sweep of
        stale ones burns 30 s — past the launcher's 8 s grant probe and
        past the button's 15 s wait, which would then report "opened" for
        a process about to exit 2. Kent asked whether it dials the others
        if the first doesn't take; it does, and that is precisely why the
        total needs a ceiling.
        """
        import time as _t
        deadline = _t.monotonic() + max(8.0, float(timeout))
        # Same context builder the data channel uses — it loads OUR
        # client cert/key so the peer sees a cert at all, and leaves
        # chain validation off because pinning is done by fingerprint on
        # the pool manager below.
        ctx = _lan_push._build_ssl_context(expected_fp)
        pm = _lan_push._pinned_pool_manager(
            ctx, expected_fp, connect=5, read=max(10, int(timeout)))
        errors = []
        skipped = 0
        for ep in endpoints:
            if _t.monotonic() >= deadline:
                # Say how many we never got to. Reporting only the
                # addresses tried would imply the list was exhausted.
                skipped += 1
                continue
            url = f'https://{ep}{path}'
            try:
                if payload is None:
                    resp = pm.request(method, url)
                else:
                    resp = pm.request(
                        method, url,
                        body=json.dumps(payload).encode('utf-8'),
                        headers={'Content-Type': 'application/json'})
                raw = resp.data.decode('utf-8') or '{}'
                if resp.status == 404 and 'not supported' in raw:
                    # VERSION SKEW, not a refusal (0.55.145). This exact
                    # sentence is dulwich's ``HTTPGitApplication`` 404 —
                    # OUR route table didn't match, so the request fell
                    # through to the git backend, which rejected it. That
                    # only happens when the peer's daemon predates the
                    # endpoint we just asked for.
                    #
                    # Worth its own branch because the raw form cost a
                    # field evening: the grant was in place, TLS and
                    # pinning were fine, the canonical form matched, and
                    # the target even LOGGED the incoming GET before
                    # 404ing it. Every local check said yes. A fleet
                    # updates one device at a time, so this will recur —
                    # say which side is behind.
                    # Say what is KNOWN (the route is absent), not which of
                    # the two causes it is. Both produce this byte-for-byte:
                    # older code, OR a stale server process still holding
                    # the port while the restarted one took an ephemeral.
                    # Claiming "too old" would have been wrong in the field
                    # case that prompted this — that peer reported 0.55.144.
                    raise RuntimeError(
                        f"that device's collaboration server has no "
                        f"remote-settings endpoint — it is running older "
                        f"code, or an old server process is still holding "
                        f"the port (asked {ep} for {path})")
                if resp.status >= 400:
                    # An ANSWER, not a transport failure: the peer is
                    # reachable and declined. Stop trying other addresses
                    # — they would all decline identically — and let the
                    # caller see which gate refused.
                    raise RuntimeError(
                        f'HTTP {resp.status} from {ep}: {raw[:200]}')
                return json.loads(raw)
            except RuntimeError:
                raise
            except Exception as ex:
                errors.append(f'{ep}: {ex!r}')
                continue
        _more = (f', {skipped} not tried (time budget spent)'
                 if skipped else '')
        raise RuntimeError(
            f'no address answered for {str(peer_id)[:8]} — tried '
            f'{len(endpoints) - skipped} of {len(endpoints)}{_more}: '
            f'{"; ".join(errors)[:400]}')

    # Matching direction wording (0.55.135): on THIS side we are the one
    # doing the changing, so say that plainly. Read together, the two
    # halves of a session are unambiguous —
    #   operator's log: "we are changing <device>"
    #   target's log:   "serving request FROM <device> … changing THIS device"
    print(f'[lan-admin-client] WE are changing '
          f'{entry.get("device_name") or "?"} ({str(peer_id)[:8]}) — '
          f'not this device. via {endpoints}, '
          f'pinned to fp {expected_fp[:16]}…',
          file=sys.stderr, flush=True)
    return LanAdminTransport(
        peer_id=str(peer_id),
        my_peer_id=mine,
        dial=_dial,
        sign=_peer_id.sign_hex,
        device_name=str(entry.get('device_name', '') or ''),
    )
