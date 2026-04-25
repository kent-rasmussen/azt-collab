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
    """Raised when no transport can reach the daemon."""


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
    """Return the best transport for this platform. Cached after the
    first call. Use ``reset()`` to force re-discovery."""
    global _transport
    if _transport is not None:
        return _transport
    if _on_android():
        # Probe sibling AZT suite apps' exported ContentProviders.
        # Anything signed with the suite key qualifies; first responder
        # wins. Falls back to loopback if no provider is reachable.
        try:
            from . import android_cp
            cp = android_cp.discover()
            if cp is not None:
                _transport = cp
                return _transport
        except Exception:
            pass
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
