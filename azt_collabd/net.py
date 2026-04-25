"""
Network plumbing: SSL patching for Android (missing CA bundle) and a quick
connectivity check.  Pure stdlib + certifi; no Kivy, no i18n.
"""

import logging
import os
import ssl

# Suppress dulwich debug/info logging (gitconfig path spam)
logging.getLogger('dulwich').setLevel(logging.WARNING)


# ── SSL fix for Android (missing CA bundle) ──────────────────────────────────
# On Android, p4a doesn't ship system CA certs.  dulwich's
# default_urllib3_manager passes ca_certs=None to urllib3.PoolManager, which
# then tries system certs and fails.  We patch default_urllib3_manager itself
# to inject the certifi CA bundle (or disable verification as a last resort).

def _find_ca_bundle():
    """Return path to a CA bundle, or None."""
    # certifi (preferred — bundled via buildozer requirements)
    try:
        import certifi
        ca = certifi.where()
        if os.path.isfile(ca):
            return ca
    except ImportError:
        pass
    # On Android, certifi's cacert.pem may be inside a zip; extract it
    try:
        import certifi
        import importlib.resources as _res
        # Write the bundle to a writable location
        priv = os.environ.get('ANDROID_PRIVATE', '')
        if priv:
            dest = os.path.join(priv, 'cacert.pem')
            data = _res.read_binary('certifi', 'cacert.pem')
            with open(dest, 'wb') as f:
                f.write(data)
            return dest
    except Exception:
        pass
    # Common Linux / Android system locations
    for path in ('/etc/ssl/certs/ca-certificates.crt',
                 '/system/etc/security/cacerts'):
        if os.path.exists(path):
            return path
    return None


def _patch_dulwich_ssl():
    """Monkey-patch urllib3 and stdlib ssl so all HTTPS works on Android."""
    ca = _find_ca_bundle()

    # Patch urllib3.PoolManager (used by dulwich)
    import urllib3
    _orig_init = urllib3.PoolManager.__init__

    def _patched_init(self, *a, **kw):
        if ca:
            if kw.get('ca_certs') is None:
                kw['ca_certs'] = ca
            kw.setdefault('cert_reqs', 'CERT_REQUIRED')
        else:
            kw['cert_reqs'] = 'CERT_NONE'
            kw.pop('ca_certs', None)
        _orig_init(self, *a, **kw)

    urllib3.PoolManager.__init__ = _patched_init

    # Patch ssl.create_default_context (used by urllib.request.urlopen)
    if ca:
        _orig_ctx = ssl.create_default_context
        def _ctx_with_ca(*a, **kw):
            kw.setdefault('cafile', ca)
            return _orig_ctx(*a, **kw)
        ssl.create_default_context = _ctx_with_ca
        ssl._create_default_https_context = _ctx_with_ca
    else:
        def _unverified_ctx(*a, **kw):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        ssl._create_default_https_context = _unverified_ctx


_dulwich_ssl_patched = False
_dulwich_env_patched = False


def _ensure_gitconfig():
    """Create an empty ~/.gitconfig so dulwich doesn't warn about missing files."""
    global _dulwich_env_patched
    if _dulwich_env_patched:
        return
    _dulwich_env_patched = True
    home = os.environ.get('HOME', '')
    if not home:
        home = os.environ.get('ANDROID_PRIVATE', '')
    if not home:
        return
    os.environ['HOME'] = home
    gitconfig = os.path.join(home, '.gitconfig')
    if not os.path.exists(gitconfig):
        try:
            with open(gitconfig, 'w') as f:
                f.write('[core]\n')
        except OSError:
            pass


def _ensure_ssl():
    """Call once before any dulwich network operation."""
    global _dulwich_ssl_patched
    if not _dulwich_ssl_patched:
        _patch_dulwich_ssl()
        _dulwich_ssl_patched = True
    _ensure_gitconfig()


def _has_internet():
    """Quick check for internet connectivity."""
    import socket
    for host in ('github.com', 'gitlab.com'):
        try:
            socket.create_connection((host, 443), timeout=3).close()
            return True
        except OSError:
            continue
    return False
