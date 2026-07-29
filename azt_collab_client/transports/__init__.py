"""
Pluggable transport layer.

Today only ``LoopbackTransport`` (HTTP+JSON over 127.0.0.1) is wired in.
``AndroidContentProviderTransport`` plugs into ``pick_transport()`` here
when the Android-side work in ``azt_collabd_cleanup_drafts.xml`` lands.

A ``Transport`` knows how to:
    - call(method, path, body, timeout) → dict
    - health() → dict
    - close() — release any resources held (subprocess fds, JNI refs)
"""

from typing import Optional


class ServerUnavailable(RuntimeError):
    """Raised when no transport can reach the daemon.

    ``kind`` is a coarse machine-readable bucket so callers can pick
    fail-fast vs keep-retrying without parsing the exception message.
    Recognised values:

    - ``'daemon_not_ready'`` — provider returned a 503 with the
      ``daemon_not_ready`` body. Service is up but Python's
      ``install_callbacks()`` hasn't fired yet. Boot-in-progress;
      worth retrying.
    - ``'null_bundle'`` — ``ContentResolver.call`` returned ``null``
      *and* the ContentProvider transport's transparent retry
      (~3 s budget; see ``android_cp._NULL_BUNDLE_RETRY_BACKOFF_S``)
      didn't recover. Likely structural: signature-grant denial
      (peer's APK signed with a different key than the suite
      keystore) or the provider authority not actually being
      installed. Pre-0.43.9 every null Bundle surfaced here
      including the transient cold-daemon-spawn race; the
      transport now absorbs those so bootstrap's fast-fail on
      this kind only fires for genuinely unrecoverable failures.
    - ``'server_apk_not_installed'`` — discovery returned ``None``;
      same shape, surfaced from ``pick_transport``.
    - ``'http'`` — loopback / HTTP error from the desktop transport.
    - ``''`` — unspecified (legacy / unclassified site).

    Bootstrap's warmup retry loop uses ``kind`` to pick the budget
    and the backoff: ``daemon_not_ready`` gets the full warm-up
    schedule; ``null_bundle`` fails fast (no amount of waiting
    fixes a signature mismatch)."""

    def __init__(self, message='', kind=''):
        super().__init__(message)
        self.kind = kind


class Transport:
    """Abstract interface every concrete transport must satisfy."""

    name: str = 'abstract'

    def call(self, method, path, body=None, timeout=300):
        raise NotImplementedError

    def health(self):
        raise NotImplementedError

    def close(self):
        pass


_transport: Optional[Transport] = None


def _on_android():
    # Kivy-free on purpose (0.53.1): this runs on EVERY RPC, and
    # importing Kivy from a non-Kivy host (desktop azt) lets Kivy's
    # import-time argv parser kill the process on host flags like
    # --restart. See azt_collab_client/_platform.py.
    try:
        from .._platform import on_android
        return on_android()
    except Exception:
        return False


_remote_transport = None        # set by target_peer(); never auto-cleared
_local_transport = None         # this device's daemon, cached separately


def target_peer(transport):
    """Pin this PROCESS to a remote peer's daemon (0.55.117).

    Call once, before the first RPC — normally from ``--peer`` handling at
    startup. Deliberately has no "go back to local" counterpart: a
    process is either local or remote for its whole life, so a window can
    never be ambiguous about which machine it is changing. Whatever
    launched it must also make that visible (device name in the title).

    Passing ``None`` is accepted only to make tests symmetrical; product
    code should not un-target."""
    global _remote_transport
    _remote_transport = transport
    reset()


def target_remote_peer(peer_id):
    """Point this process at *peer_id* by asking the LOCAL daemon for a
    transport (0.55.127).

    Android's in-place retarget path. The transport needs the private key,
    ``peers.json`` and the pinned dialer — all daemon-side — and this
    package may not import ``azt_collabd`` (hard rule #3). On Android the
    daemon is in another process entirely, so it cannot hand back a live
    Python object either.

    So this builds the client half here and injects a dial that goes
    through the LOCAL daemon's admin-relay: our daemon does the pinned
    TLS and the signing, because it is the only one holding the key.

    Raises on any failure — the caller shows the reason. Never leaves the
    process half-targeted."""
    from . import rpc_relay
    tr = rpc_relay.build(peer_id)
    target_peer(tr)
    return tr


def remote_peer_label():
    """Device name of the peer this process is driving, or ''. For a UI
    banner — nothing should branch behaviour on it."""
    if _remote_transport is None:
        return ''
    return getattr(_remote_transport, 'device_name', '') or 'peer'


def clear_remote_peer():
    """Return this process to driving the LOCAL daemon (0.55.127).

    Exists only for the Android retarget path, where there is no second
    window to close. The desktop launcher deliberately has no equivalent:
    a process there is local or remote for its whole life."""
    global _remote_transport
    _remote_transport = None
    reset()


def is_remote():
    """True when this process is driving another device. UI uses it to
    label itself; nothing should branch behaviour on it."""
    return _remote_transport is not None


def pick_transport():
    """Return the right transport for this platform. Cached after the
    first call. Use ``reset()`` to force re-discovery.

    On Android: bind to the standalone server APK's ContentProvider
    or raise ``ServerUnavailable`` (no loopback fallback — there is
    no Python interpreter to spawn).

    Off Android: loopback HTTP, with auto-spawn of the daemon."""
    global _transport
    if _transport is not None:
        return _transport
    # REMOTE TARGET WINS (0.55.117). When this process has been pointed at
    # a peer, EVERY RPC goes there — that is the whole design: the same
    # settings UI, unmodified, driving another machine.
    #
    # Set once at process start (``--peer``) and never toggled, so there
    # is no mode to lose track of. Kent, on an earlier design that
    # retargeted a running UI: *"I'm not excited about this; I'm likely to
    # get confused at some point. Can we not start another ui?"* Two
    # windows side by side, each fixed to one machine.
    if _remote_transport is not None:
        _transport = _remote_transport
        return _transport
    _transport = pick_local()
    return _transport


def pick_local():
    """The transport to THIS device's daemon, ignoring any remote target
    (0.55.130).

    Needed because the relay must not route through itself.
    ``RelayTransport._relay`` called ``rpc.call``, which called
    ``pick_transport()``, which returned the remote transport — i.e. the
    relay — and recursed until ``RecursionError``. The relay's whole job is
    to hand a request to the LOCAL daemon, so it must ask for local
    explicitly rather than for "whatever is current".

    Cached separately from ``_transport`` so a retarget doesn't invalidate
    it and vice versa."""
    global _local_transport
    if _local_transport is not None:
        return _local_transport
    if _on_android():
        from . import android_cp
        cp = android_cp.discover()
        if cp is None:
            raise ServerUnavailable(
                'server_apk_not_installed',
                kind='server_apk_not_installed')
        _local_transport = cp
        return _local_transport
    from .loopback import LoopbackTransport
    _local_transport = LoopbackTransport()
    return _local_transport


def reset():
    """Drop the cached transport. Next ``pick_transport()`` re-discovers."""
    global _transport
    if _transport is not None:
        try:
            _transport.close()
        except Exception:
            pass
    _transport = None


def current_transport_name():
    """Name of whichever transport ``pick_transport`` last returned, or
    ``''`` if no call has been made yet. Diagnostic only."""
    return _transport.name if _transport is not None else ''
