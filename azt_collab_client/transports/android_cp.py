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
    server-APK authority if it answers ``ping``, else None.

    Side effect on success (Phase B2): kicks off a peer-side
    ``bindService`` against the server APK's
    ``AZTServiceProviderhost`` via ``AZTServiceConnector.ensureBound``.
    The bind raises ``:provider``'s OOM priority for as long as the
    peer is alive, defeating Android 15's app freezer that would
    otherwise suspend the daemon's Python interpreter mid-init on
    cached processes — exactly the symptom that produced the
    R500-tablet "AZT Collaboration not responding" reports.

    Best-effort: any failure (no jnius, missing connector class,
    bind refused) is logged and silenced. The transport still
    works against the ContentProvider without the bind; on
    freezer-affected devices it just degrades to the pre-B2
    behaviour."""
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
        # Phase B2: hold a service binding so :provider stays warm
        # across the peer's session. Idempotent; safe to invoke on
        # every discover() (only the first invocation actually binds;
        # subsequent calls no-op while the bind is alive). Async —
        # we don't wait for onServiceConnected. The peer's compat
        # probe retry loop handles the daemon-boot latency naturally.
        try:
            Connector = autoclass(
                'org.atoznback.aztcollab.AZTServiceConnector')
            Connector.ensureBound(activity)
        except Exception as ex:
            import sys as _sys
            print(f'[android_cp] AZTServiceConnector.ensureBound '
                  f'failed: {ex}',
                  file=_sys.stderr, flush=True)
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
            raise ServerUnavailable(
                f'provider call failed: {ex}',
                kind='transport_error')

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
        # Always-on first-try probes added in 0.41.16 instrument
        # every RPC so a field tester without adb can deliver a
        # first-try-fails trail. Suppress for high-frequency
        # polling paths (cache_status at 1 Hz) where the probe
        # is pure noise — "first-try" semantically doesn't apply
        # to the Nth call of a polling loop.
        suppress_probe = path.endswith('/cawl/cache_status')
        if not suppress_probe:
            from .._debug import first_try_log
            first_try_log('transport.call.pre',
                          method=method, path=path)
        bundle = self._resolver.call(uri, method, path, extras)
        if not suppress_probe:
            first_try_log('transport.call.post',
                          method=method, path=path,
                          bundle_null=bundle is None)
        if bundle is None:
            # Most common cause: signature-grant denial (peer's APK
            # signed with a different key than the suite keystore)
            # or the provider authority not actually present.
            # Structural; bootstrap's warmup loop fails fast on this
            # kind rather than waiting the full 60s budget.
            raise ServerUnavailable(
                'provider returned null',
                kind='null_bundle')
        status = bundle.getInt('status', 500)
        json_str = bundle.getString('json') or ''
        try:
            response = json.loads(json_str) if json_str else {}
        except Exception:
            raise ServerUnavailable(
                f'provider returned non-JSON: {json_str[:200]!r}',
                kind='transport_error')
        # Mirror loopback semantics: the dispatcher returns its dict
        # regardless of status; surface 5xx as ServerUnavailable so
        # callers retry through the transport seam.
        if status >= 500:
            error_code = response.get('error', '')
            kind = ('daemon_not_ready'
                    if error_code == 'daemon_not_ready'
                    else 'http_5xx')
            raise ServerUnavailable(
                f'provider HTTP {status}: '
                f'{error_code or "unknown"}',
                kind=kind)
        return response
