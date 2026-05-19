"""
Network plumbing: SSL patching for Android (missing CA bundle), a DNS-
over-HTTPS fallback resolver for field networks that break system DNS
without breaking the browser, and a quick connectivity check.  Pure
stdlib + certifi; no Kivy, no i18n.
"""

import json
import logging
import os
import socket
import ssl
import sys
import threading
import time

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
    """Call once before any dulwich network operation. Installs SSL +
    DoH-fallback resolver patches; idempotent."""
    global _dulwich_ssl_patched
    if not _dulwich_ssl_patched:
        _patch_dulwich_ssl()
        _patch_resolver()
        _dulwich_ssl_patched = True
    _ensure_gitconfig()


# ── DoH-fallback resolver ────────────────────────────────────────────────────
# Why this exists: field deployments (mobile networks in low-bandwidth
# regions, captive-portal Wi-Fi, devices with mis-configured Private
# DNS, per-app data restrictions, IPv6-only networks without DNS64) hit
# resolver-class failures where the browser keeps working but dulwich /
# urllib.request get NameResolutionError on github.com. See
# ``S.DNS_RESOLUTION_FAILED`` in ``status.py`` for the user-facing
# rationale.
#
# Mechanism: monkey-patch ``socket.getaddrinfo``. The system resolver
# runs FIRST and unchanged; if it raises ``gaierror`` (the
# `NameResolutionError` inside dulwich is a wrapped version of this),
# we fall back to a single DNS-over-HTTPS query to Cloudflare at the
# literal IP ``1.1.1.1``. The cert at that IP has ``1.1.1.1`` as a
# Subject Alt Name (verified against the bundled certifi roots), so
# TLS validates without a CNAME lookup. The literal-IP form is the
# critical detail: it makes the DoH path itself loop-free —
# ``getaddrinfo('1.1.1.1', 443)`` is satisfied by libc without
# triggering DNS, so our patched wrapper does not recurse.
#
# This is a **fallback**, not a replacement:
#   - On healthy networks the DoH path is dead code (system resolver
#     succeeds, we never enter the except branch).
#   - The DoH path is gated on ``host`` being a plausible FQDN; numeric
#     IPs, IDN edge cases, and unrelated AF_UNIX-style lookups bypass.
#   - DoH results are cached briefly (5 min) keyed by ``(host, port)``
#     so a connection retry loop doesn't refire the DoH query.
#   - The ``_RESOLVER_STATE`` shared dict records which path served the
#     last lookup (``'system' | 'doh' | 'fail' | 'unknown'``), exposed
#     via ``resolver_state()`` for the scheduler's connectivity log.

_DOH_URL = 'https://1.1.1.1/dns-query'
# 2.5 s is tighter than the watcher's 3 s TCP timeout to github.com so
# a fully-offline _has_internet() probe (2 hosts × system gaierror →
# DoH attempt) stays bounded under the 30 s connectivity_poll_s tick.
# On a healthy network DoH responds in ~50-300 ms; the timeout is
# only the worst-case black-hole-network ceiling.
_DOH_TIMEOUT_S = 2.5
_DOH_CACHE_TTL_S = 300.0
# Negative cache: when DoH itself fails (Cloudflare unreachable,
# empty answer, JSON parse error), remember that for a short window
# so urllib3's in-loop retry storm (~3 retries within ~2 s) doesn't
# pay the 2.5 s DoH timeout each time. Kept narrow at 5 s so that
# user-driven retries (tap Sync, see error, wait, tap again) get a
# fresh probe within seconds — a 30 s window (the pre-0.43.13 value)
# was the smoking gun for sustained "DNS resolution failed" loops on
# Starlink, where a single transient miss during a satellite
# handover poisoned the cache long enough to dominate the user's
# retry cadence. Background ``_has_internet()`` ticks still
# debounce naturally via the positive-cache TTL once a probe lands;
# a successful DoH lookup also clears all outstanding negatives via
# the network-back side-effect in ``_patched_getaddrinfo``.
_DOH_NEGATIVE_TTL_S = 5.0
_DOH_CACHE = {}
_DOH_CACHE_LOCK = threading.Lock()
_RESOLVER_STATE = {'last': 'unknown'}

_orig_getaddrinfo = None


def _doh_query(name, qtype):
    """One DoH JSON query against Cloudflare. ``qtype``: 'A' or 'AAAA'.
    Returns a list of IP literal strings; empty on any failure (caller
    treats empty as "DoH didn't help; raise the original gaierror")."""
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    url = f'{_DOH_URL}?{urlencode({"name": name, "type": qtype})}'
    req = Request(url, headers={'accept': 'application/dns-json'})
    try:
        with urlopen(req, timeout=_DOH_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as ex:
        print(f'[resolver] DoH {qtype} {name} failed: {ex}',
              file=sys.stderr, flush=True)
        return []
    # RFC 1035 type codes: A=1, AAAA=28. Filter to type-matching
    # answers so CNAME records (also returned by DoH) don't pollute
    # the address list. ``data`` field is the IP literal string.
    want_type = 1 if qtype == 'A' else 28
    return [str(a.get('data')) for a in (data.get('Answer') or [])
            if a.get('type') == want_type and a.get('data')]


def _doh_addrinfo(host, port, sock_type, proto):
    """Synthetic ``getaddrinfo`` result list via DoH. Returns [] on any
    failure or empty answer; the patched wrapper re-raises the
    original gaierror in that case (no silent NXDOMAIN fix-ups)."""
    aaaa = _doh_query(host, 'AAAA')
    a = _doh_query(host, 'A')
    out = []
    # Order AAAA first to mirror what a v6-preferring stack would do
    # if it had the records. urllib3 / socket.create_connection
    # iterate in order, so this also yields a happy-eyeballs-shaped
    # try order when IPv6 paths exist.
    for ip in aaaa:
        out.append((socket.AF_INET6, sock_type, proto, '',
                    (ip, int(port), 0, 0)))
    for ip in a:
        out.append((socket.AF_INET, sock_type, proto, '',
                    (ip, int(port))))
    return out


def _looks_like_hostname(host):
    """Cheap gate: don't DoH-fallback on numeric IPs, AF_UNIX paths,
    or empty strings. Anything with at least one dot and at least one
    letter qualifies."""
    if not isinstance(host, str) or not host or '.' not in host:
        return False
    return any(c.isalpha() for c in host)


def _patched_getaddrinfo(host, port, *args, **kwargs):
    """System resolver first; DoH on gaierror for hostname-shaped
    lookups. Records the path used in ``_RESOLVER_STATE``."""
    try:
        result = _orig_getaddrinfo(host, port, *args, **kwargs)
        _RESOLVER_STATE['last'] = 'system'
        return result
    except socket.gaierror:
        if not _looks_like_hostname(host):
            _RESOLVER_STATE['last'] = 'fail'
            raise
        now = time.time()
        cache_key = (host, port)
        with _DOH_CACHE_LOCK:
            entry = _DOH_CACHE.get(cache_key)
            if entry and entry[0] > now:
                # Positive entry has records; negative entry is
                # cached as an empty list so the lookup short-
                # circuits without paying the DoH timeout again.
                if entry[1]:
                    _RESOLVER_STATE['last'] = 'doh'
                    return entry[1]
                _RESOLVER_STATE['last'] = 'fail'
                raise
        # ``socket.getaddrinfo(host, port, family=0, type=0, proto=0,
        # flags=0)`` — derive sock_type / proto from args for the
        # synthetic tuple. Family is ignored (DoH covers both stacks
        # and the caller can iterate).
        sock_type = (args[1] if len(args) > 1
                     else kwargs.get('type', socket.SOCK_STREAM))
        proto = (args[2] if len(args) > 2
                 else kwargs.get('proto', 0))
        synth = _doh_addrinfo(host, port, sock_type, proto)
        if not synth:
            with _DOH_CACHE_LOCK:
                _DOH_CACHE[cache_key] = (
                    now + _DOH_NEGATIVE_TTL_S, [])
            _RESOLVER_STATE['last'] = 'fail'
            raise
        with _DOH_CACHE_LOCK:
            _DOH_CACHE[cache_key] = (now + _DOH_CACHE_TTL_S, synth)
        _RESOLVER_STATE['last'] = 'doh'
        print(f'[resolver] system DNS failed for {host}; '
              f'DoH returned {len(synth)} record(s)',
              file=sys.stderr, flush=True)
        return synth


def _patch_resolver():
    """Install the DoH-fallback wrapper on ``socket.getaddrinfo``.
    Idempotent."""
    global _orig_getaddrinfo
    if _orig_getaddrinfo is not None:
        return
    _orig_getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = _patched_getaddrinfo


def resolver_state():
    """Return the path used for the most recent ``getaddrinfo`` call:
    ``'system'``, ``'doh'``, ``'fail'``, or ``'unknown'`` (no lookup
    performed yet this session). Read by the scheduler so an operator
    skimming the daemon log can tell whether the DoH path is active."""
    return _RESOLVER_STATE.get('last', 'unknown')


def _has_internet():
    """Quick check for internet connectivity. Returns True if either
    sync host's HTTPS port is reachable, with the resolver path
    (system DNS vs DoH fallback) already accounted for via the
    monkey-patched ``socket.getaddrinfo``. Side effect: updates
    ``_RESOLVER_STATE`` for ``resolver_state()`` callers."""
    for host in ('github.com', 'gitlab.com'):
        try:
            socket.create_connection((host, 443), timeout=3).close()
            return True
        except OSError:
            continue
    return False
