"""
Backwards-compatible facade over the transports layer.

Existing callers do ``from azt_collab_client.rpc import call, health,
ServerUnavailable``. After the transport refactor the actual work
lives in ``azt_collab_client.transports``; this module just delegates
to whichever transport ``pick_transport()`` returns.

On ServerUnavailable we drop the cached transport once and re-pick.
That recovers when an Android ContentProvider host disappears (the
hosting APK got killed / uninstalled) and the client should fall
through to loopback — or vice versa, when a provider appears after
loopback was the only option at startup.
"""

from .transports import ServerUnavailable, pick_transport, reset


def call(method, path, body=None, timeout=300):
    try:
        return pick_transport().call(method, path, body, timeout)
    except ServerUnavailable:
        reset()
        return pick_transport().call(method, path, body, timeout)


def health():
    try:
        return pick_transport().health()
    except ServerUnavailable:
        reset()
        return pick_transport().health()


__all__ = ['call', 'health', 'ServerUnavailable', 'reset']
