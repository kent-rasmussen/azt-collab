"""
Backwards-compatible facade over the transports layer.

Existing callers do ``from azt_collab_client.rpc import call, health,
ServerUnavailable``. After the transport refactor the actual work
lives in ``azt_collab_client.transports``; this module just delegates
to whichever transport ``pick_transport()`` returns.
"""

from .transports import ServerUnavailable, pick_transport, reset


def call(method, path, body=None, timeout=300):
    return pick_transport().call(method, path, body, timeout)


def health():
    return pick_transport().health()


__all__ = ['call', 'health', 'ServerUnavailable', 'reset']
