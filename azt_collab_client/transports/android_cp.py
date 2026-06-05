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
        # Tolerate the daemon cold-spawn race here too: discovery's
        # ping is often the very call that triggers Android's lazy
        # spawn of ``:provider``, so the first attempt commonly
        # returns null while Python imports. Same justification as
        # ``_raw_call``'s retry (see there); kept short so a real
        # "server APK not installed" still falls through to the
        # install prompt quickly.
        bundle = None
        for delay in (0.0, 0.2, 0.4, 0.8, 1.6):
            if delay:
                import time as _time
                _time.sleep(delay)
            try:
                bundle = resolver.call(uri, 'ping', None, None)
            except Exception:
                return None
            if bundle is not None:
                break
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

    # Backoff for the transparent ``null_bundle`` retry below.
    # Cumulative budget: 0.1+0.2+0.4+0.8+1.6 = 3.1 s, which covers
    # the observed daemon cold-spawn import (~1.9 s on a mid-range
    # Android device — see ``[boot-trace-daemon]`` ``module_loaded``
    # → ``after_install_callbacks``) plus margin. If the daemon
    # truly isn't there (signature mismatch / authority gone, fresh
    # install with bundle not yet extracted) we surface
    # ``null_bundle`` after burning this 3 s budget; the outer
    # bootstrap path then renders its "unresponsive" popup / fires
    # ``_open_server_apk_launcher`` per § 0.50.5.
    #
    # **The 3 s blocks whichever thread called us.** Peers MUST NOT
    # call into the transport from the main UI thread — see
    # CLIENT_INTEGRATION.md § 17c Rule 7. We don't shorten this
    # budget to defend against peer-side misuse; that would degrade
    # the common cold-spawn case (peer waits one more bootstrap
    # warmup tick) for everyone, just to make a single misbehaving
    # peer survive Android's ANR watchdog.
    _NULL_BUNDLE_RETRY_BACKOFF_S = (0.1, 0.2, 0.4, 0.8, 1.6)

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
        # Transparent retry on null Bundle. Two known producers:
        #
        #   1. Cold-daemon spawn race. The peer's call lazy-spawns
        #      ``:provider``; Android registers the provider authority
        #      before the Python body has imported
        #      ``AZTServiceProviderhost`` and installed the dispatch
        #      callback. The Java provider's ``call()`` sees ``cb ==
        #      null`` and returns a null Bundle. Resolves on its own
        #      once the Python import lands (~2 s).
        #
        #   2. Persistent structural failure. Signature-grant
        #      denial, provider authority absent. No amount of
        #      waiting fixes it.
        #
        # We can't distinguish (1) from (2) at call time, so we
        # retry case (1) blind and let case (2) bleed through after
        # the budget. Retrying is safe for both GETs and POSTs:
        # null Bundle means Python dispatch never ran, so no work
        # was done on the daemon side. Pre-0.43.9 this was treated
        # as fail-fast "structural", which crashed the peer on its
        # first call when the daemon happened to be idle-stopped
        # or had just been reaped (0.43.7's
        # SuiteSelfReplaceReceiver fix made reaping reliable, which
        # also made cold-spawn races strictly more common).
        bundle = None
        attempt = 0
        max_attempts = len(self._NULL_BUNDLE_RETRY_BACKOFF_S)
        while True:
            bundle = self._resolver.call(uri, method, path, extras)
            if bundle is not None:
                break
            if attempt >= max_attempts:
                break
            import time as _time
            _time.sleep(self._NULL_BUNDLE_RETRY_BACKOFF_S[attempt])
            attempt += 1
        # Only log when something interesting happened — the daemon
        # returned null (structural failure) or the retry loop fired
        # at least once (cold-spawn race). Routine RPCs stay silent;
        # 0.41.16's always-on pre+post pair was load-bearing for the
        # 2026-05 Tecno KN4 diagnosis, but post-0.43.9 the retry path
        # is the fix for the cold-spawn race those probes detected,
        # so every routine call shipped ``bundle_null=False
        # null_retries=0`` — pure noise that drowned /sdcard logs.
        if bundle is None or attempt > 0:
            from .._debug import first_try_log
            first_try_log('transport.call.post',
                          method=method, path=path,
                          bundle_null=bundle is None,
                          null_retries=attempt)
        if bundle is None:
            raise ServerUnavailable(
                f'provider returned null after {attempt} retries',
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
