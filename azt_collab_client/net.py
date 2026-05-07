"""Client-side SSL setup.

p4a doesn't bundle system CA certs into the Android Python runtime,
so a vanilla ``urllib.request.urlopen('https://api.github.com/...')``
fails with "unable to get local issuer certificate" the moment the
peer's bootstrap or self-update probe touches GitHub.

This module is the client mirror of ``azt_collabd/net.py``'s SSL
patch — slimmer because the client doesn't speak dulwich (no
urllib3.PoolManager surface to handle). Just patches
``ssl.create_default_context`` so stdlib ``urllib.request`` finds a
CA bundle.

Hard rule 3 (no daemon import) is why this can't just call
``azt_collabd.net._ensure_ssl``. The two run in different processes
on Android (server APK vs. peer APK) and need their own SSL state
anyway.

Idempotent — calls after the first are no-ops. Safe to call from
every urlopen site without paying repeat cost.
"""

import os
import ssl


_patched = False


def _find_ca_bundle():
    """Return path to a CA bundle, or None.

    Order of attempts mirrors the daemon's: certifi (preferred,
    bundled via buildozer requirements), then certifi-extracted-
    from-zip on Android (where certifi.where() may point inside
    a zip the OS can't read directly), then system locations."""
    # certifi (preferred — bundled via buildozer requirements)
    try:
        import certifi
        ca = certifi.where()
        if os.path.isfile(ca):
            return ca
    except ImportError:
        pass
    # On Android, certifi's cacert.pem may be inside a zip; extract
    # it to a writable location and use that.
    try:
        import certifi  # noqa: F401  (just to confirm it imports)
        import importlib.resources as _res
        priv = os.environ.get('ANDROID_PRIVATE', '')
        if priv:
            dest = os.path.join(priv, 'cacert.pem')
            if not os.path.exists(dest):
                data = _res.read_binary('certifi', 'cacert.pem')
                with open(dest, 'wb') as f:
                    f.write(data)
            return dest
    except Exception:
        pass
    # Common Linux / Android system locations.
    for path in ('/etc/ssl/certs/ca-certificates.crt',
                 '/system/etc/security/cacerts'):
        if os.path.exists(path):
            return path
    return None


def _ensure_ssl():
    """Patch stdlib ``ssl`` so urllib's HTTPS calls find a CA
    bundle. Call once before any ``urllib.request.urlopen`` against
    HTTPS — the bootstrap / update / share flows all do this. Other
    callers can rely on the patch persisting for the rest of the
    process."""
    global _patched
    if _patched:
        return
    ca = _find_ca_bundle()
    if ca:
        _orig = ssl.create_default_context

        def _ctx_with_ca(*a, **kw):
            kw.setdefault('cafile', ca)
            return _orig(*a, **kw)

        ssl.create_default_context = _ctx_with_ca
        ssl._create_default_https_context = _ctx_with_ca
    else:
        # Last-resort fallback: disable verification. Bad on a
        # hostile network, but better than 100% failure on a device
        # with no usable CA bundle (rare; mostly buildozer recipes
        # that strip certifi by accident).
        def _unverified_ctx(*_a, **_kw):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        ssl._create_default_https_context = _unverified_ctx
    _patched = True
