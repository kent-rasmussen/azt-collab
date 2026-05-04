"""
Android ContentProvider transport.

Discovery: probes the single canonical server-APK authority
``org.atoznback.aztcollab``. If the server APK is installed and
responding to ``ping``, the transport binds to it. Otherwise
``discover()`` returns None and the client surfaces an install
prompt — there is no peer-hosted fallback (cleanup-draft #3).

Auth: relies on Android signature-level <permission>. Anything that
reaches this code already passed the install-time signature check;
the transport just funnels JSON through ``call(method, arg, extras)``.

This transport is only constructible on Android (pyjnius required).
"""

import json

from . import Transport, ServerUnavailable


CANONICAL_AUTHORITY = 'org.atoznback.aztcollab'


def discover():
    """Return an AndroidContentProviderTransport bound to the canonical
    server-APK authority if it answers ``ping``, else None."""
    try:
        from jnius import autoclass
    except ImportError:
        return None
    try:
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Uri = autoclass('android.net.Uri')
        activity = PythonActivity.mActivity
        if activity is None:
            return None
        resolver = activity.getContentResolver()
        uri = Uri.parse(f'content://{CANONICAL_AUTHORITY}/v1/health')
        try:
            bundle = resolver.call(uri, 'ping', None, None)
        except Exception:
            return None
        if bundle is None:
            return None
        return AndroidContentProviderTransport(CANONICAL_AUTHORITY)
    except Exception:
        return None


class AndroidContentProviderTransport(Transport):
    name = 'android_cp'

    def __init__(self, authority):
        self.authority = authority
        self._lazy_init()

    def _lazy_init(self):
        from jnius import autoclass
        self._Uri = autoclass('android.net.Uri')
        self._Bundle = autoclass('android.os.Bundle')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        self._resolver = PythonActivity.mActivity.getContentResolver()

    def health(self):
        return self._raw_call('GET', '/v1/health', None, timeout=5)

    def call(self, method, path, body=None, timeout=300):
        # timeout is advisory; ContentResolver.call has no built-in
        # cancellation. We rely on the daemon to bound long ops.
        try:
            return self._raw_call(method, path, body, timeout=timeout)
        except ServerUnavailable:
            raise
        except Exception as ex:
            raise ServerUnavailable(f'provider call failed: {ex}')

    def _raw_call(self, method, path, body, timeout):
        # ContentResolver.call(uri, method, arg, extras) consumes the
        # URI's authority for provider routing but does NOT deliver the
        # URI's path to ContentProvider.call(method, arg, extras). Pass
        # the dispatch path as ``arg`` — that's the channel the Java
        # side reads (AZTCollabProvider.java line ~82,
        # ``cb.dispatch(method, arg != null ? arg : "", body)``).
        # Without this the daemon dispatch sees an empty path on every
        # call and replies ``{ok: False, error: 'not_found'}``.
        uri = self._Uri.parse(f'content://{self.authority}')
        extras = self._Bundle()
        if body is not None:
            extras.putString('body', json.dumps(body))
        bundle = self._resolver.call(uri, method, path, extras)
        if bundle is None:
            raise ServerUnavailable('provider returned null')
        status = bundle.getInt('status', 500)
        json_str = bundle.getString('json') or ''
        try:
            response = json.loads(json_str) if json_str else {}
        except Exception:
            raise ServerUnavailable(
                f'provider returned non-JSON: {json_str[:200]!r}')
        # Mirror loopback semantics: the dispatcher returns its dict
        # regardless of status; surface 5xx as ServerUnavailable so
        # callers retry through the transport seam.
        if status >= 500:
            raise ServerUnavailable(
                f'provider HTTP {status}: '
                f'{response.get("error", "unknown")}')
        return response
