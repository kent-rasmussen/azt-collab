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
    - ``'null_bundle'`` — ``ContentResolver.call`` returned ``null``.
      Most common cause is signature-grant denial (peer's APK signed
      with a different key than the suite keystore) or the provider
      authority not actually being installed. Structural; retrying
      doesn't help.
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
    try:
        from kivy.utils import platform
        return platform == 'android'
    except Exception:
        return False


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
    if _on_android():
        from . import android_cp
        cp = android_cp.discover()
        if cp is None:
            raise ServerUnavailable(
                'server_apk_not_installed',
                kind='server_apk_not_installed')
        _transport = cp
        return _transport
    from .loopback import LoopbackTransport
    _transport = LoopbackTransport()
    return _transport


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
