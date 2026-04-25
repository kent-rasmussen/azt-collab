"""
Android ContentProvider transport.

Discovery: enumerates installed ContentProviders, picks ones whose
authority ends in ``.aztcollab``, and pings each via
``ContentResolver.call(uri, "ping", null, null)`` until one responds.
The first responder wins (multiple providers all read the same
``$AZT_HOME``, so it doesn't matter which serves the call).

Auth: relies on Android signature-level <permission>. Anything that
reaches this code already passed the install-time signature check;
the transport just funnels JSON through ``call(method, arg, extras)``.

This transport is only constructible on Android (pyjnius required).
``pick_transport()`` falls back to LoopbackTransport everywhere else.
"""

import json

from . import Transport, ServerUnavailable


_AUTHORITY_SUFFIX = '.aztcollab'


def discover():
    """Return an AndroidContentProviderTransport bound to the first
    matching authority, or None if nothing answers."""
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
        pm = activity.getPackageManager()
        resolver = activity.getContentResolver()
        # 0 here = PackageManager.GET_META_DATA off; we just need authorities.
        providers = pm.queryContentProviders(None, 0, 0)
        if providers is None:
            return None
        for i in range(providers.size()):
            info = providers.get(i)
            authority = info.authority
            if authority is None or not authority.endswith(_AUTHORITY_SUFFIX):
                continue
            uri = Uri.parse(f'content://{authority}/v1/health')
            try:
                bundle = resolver.call(uri, 'ping', None, None)
                if bundle is not None:
                    return AndroidContentProviderTransport(authority)
            except Exception:
                continue
    except Exception:
        return None
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
        uri = self._Uri.parse(f'content://{self.authority}{path}')
        extras = self._Bundle()
        if body is not None:
            extras.putString('body', json.dumps(body))
        bundle = self._resolver.call(uri, method, None, extras)
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
