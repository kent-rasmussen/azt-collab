"""
mDNS / DNS-SD discovery for the LAN sync transport (parked design
phase 5).

Service type: ``_aztcollab._tcp.local.``. TXT records carry the
peer's identity advertised in the spec:

  peer_id    hex ed25519 pubkey (64 chars)
  fp         hex SHA-256 of TLS cert (64 chars)
  v          protocol version int

Instance name = ``device_name`` (daemon-owned, user-overridable).

Two platforms, one facade:

  - **Android** — ``NsdManager`` via pyjnius. Browse uses
    ``DiscoveryRequest.FLAG_SHOW_PICKER`` so the system handles the
    "choose a phone" dialog, which dodges the Android 17
    ``ACCESS_LOCAL_NETWORK`` runtime permission. The
    ``WifiManager.MulticastLock`` (held by
    ``android_cp.lan_fgs.acquire_wifi_locks``) keeps multicast
    packets reaching us.
  - **Desktop** — ``python-zeroconf``. Pure-Python, no native
    binaries.

Public API: ``start_advertise``, ``stop_advertise``, ``start_browse``,
``stop_browse``, ``get_endpoint``. All are no-ops if the platform-
specific dependency is missing — discovery just degrades silently
and the static-endpoint fallback (phase 7) carries the day.

Endpoint cache: in-memory only, ``peer_id_hex → (host, port)``.
mDNS discoveries are session-scoped and rebroadcast cheaply, so
losing the cache on daemon respawn is fine — the next browse pass
will repopulate within ~1-2 s.
"""

from __future__ import annotations

import socket
import sys
import threading


SERVICE_TYPE = '_aztcollab._tcp.local.'
SERVICE_TYPE_ANDROID = '_aztcollab._tcp.'   # NsdManager wants no trailing
PROTOCOL_VERSION = 1


_LOCK = threading.Lock()
_STATE = {
    'mode': '',                # 'zeroconf' | 'nsd' | ''
    'advertise': None,         # ServiceInfo / NsdServiceInfo handle
    'browse': None,            # listener / discovery handle
    'zc': None,                # python-zeroconf Zeroconf instance
}
# peer_id_hex → (host, port). Mutated by the browse callback; read by
# the scheduler's fan-out path.
_endpoints = {}


def _detect_mode():
    """Return ``'nsd'``, ``'zeroconf'``, or ``''`` based on what's
    importable on this platform. Caches nothing — the daemon may
    boot before its environment is ready, and reprobing on each
    call is cheap."""
    try:
        import jnius  # noqa: F401
        return 'nsd'
    except ImportError:
        pass
    try:
        import zeroconf  # noqa: F401
        return 'zeroconf'
    except ImportError:
        return ''


def get_endpoint(peer_id_hex):
    """Return ``(host, port)`` last seen for *peer_id_hex* via mDNS,
    or ``None``. Per-process in-memory cache; safe from any
    thread."""
    with _LOCK:
        return _endpoints.get(peer_id_hex)


def known_endpoints():
    """Return a copy of the full ``peer_id → (host, port)`` map.
    For diagnostics + the scheduler's fan-out planner."""
    with _LOCK:
        return dict(_endpoints)


def invalidate_endpoint(peer_id_hex):
    """Drop the cached endpoint for *peer_id_hex* so the next
    discovery refresh (or a fresh ``resolveService`` triggered by
    ``onServiceFound``) repopulates it. Used by the fan-out path
    when a connection to the cached endpoint refuses — common
    after the peer's daemon restarts and binds a new ephemeral
    port that NsdManager hasn't surfaced an update event for."""
    with _LOCK:
        _endpoints.pop(peer_id_hex, None)


def _persist_resolved_endpoint(peer_id_hex, host, port):
    """Write a freshly-resolved ``host:port`` into the paired-peer
    record's ``static_endpoints`` (a no-op if the peer isn't
    paired). Called from ``onServiceResolved`` so the static
    fallback drifts forward to track the peer's current location
    instead of staying frozen at pair-time. Idempotent: if the
    value is already at the head of the list, we skip the
    ``set_static_endpoints`` write entirely."""
    from . import peers as _peers
    entry = _peers.get_peer(peer_id_hex)
    if entry is None:
        return  # not paired — discovery may surface non-paired peers
    new_endpoint = f'{host}:{port}'
    current = list(entry.get('static_endpoints') or [])
    if current and current[0] == new_endpoint:
        return  # already-current; no write needed
    # Put the resolved one at the head; preserve any other entries
    # the user may have manually added (e.g., a hotspot-host
    # fallback) but dedupe.
    updated = [new_endpoint] + [e for e in current if e != new_endpoint]
    _peers.set_static_endpoints(peer_id_hex, updated)


def _zc_props(peer_id_hex, fp_hex):
    return {
        b'peer_id': peer_id_hex.encode('ascii'),
        b'fp': fp_hex.encode('ascii'),
        b'v': str(PROTOCOL_VERSION).encode('ascii'),
    }


def _start_advertise_zeroconf(peer_id_hex, fp_hex, port, device_name):
    from zeroconf import Zeroconf, ServiceInfo
    zc = _STATE.get('zc') or Zeroconf()
    _STATE['zc'] = zc
    instance = (device_name or 'AZT device') + '.' + SERVICE_TYPE
    addrs = []
    try:
        ip = socket.gethostbyname(socket.gethostname())
        addrs.append(socket.inet_aton(ip))
    except Exception:
        pass
    info = ServiceInfo(
        type_=SERVICE_TYPE,
        name=instance,
        port=int(port),
        properties=_zc_props(peer_id_hex, fp_hex),
        addresses=addrs,
    )
    try:
        zc.register_service(info, allow_name_change=True)
    except Exception as ex:
        print(f'[lan-discovery] zeroconf register failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None
    return info


def _stop_advertise_zeroconf():
    info = _STATE.get('advertise')
    zc = _STATE.get('zc')
    if info is None or zc is None:
        return
    try:
        zc.unregister_service(info)
    except Exception as ex:
        print(f'[lan-discovery] zeroconf unregister raised: {ex!r}',
              file=sys.stderr, flush=True)


def _zc_listener_class():
    """Build a ServiceListener subclass that mutates ``_endpoints``
    on add/update/remove. Lazy import so a desktop without
    python-zeroconf doesn't crash at module import."""
    from zeroconf import ServiceListener

    class _Listener(ServiceListener):
        def __init__(self, zc):
            self._zc = zc

        def add_service(self, zc, type_, name):
            self._record(zc, type_, name)

        def update_service(self, zc, type_, name):
            self._record(zc, type_, name)

        def remove_service(self, zc, type_, name):
            self._forget(name)

        def _record(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if info is None:
                return
            props = info.properties or {}
            peer_id = (props.get(b'peer_id') or b'').decode('ascii',
                                                            'ignore')
            if len(peer_id) != 64:
                return
            host = None
            for raw in (info.addresses or []):
                try:
                    host = socket.inet_ntoa(raw)
                    break
                except OSError:
                    continue
            if not host:
                return
            with _LOCK:
                _endpoints[peer_id] = (host, int(info.port))
            print(f'[lan-discovery] add {peer_id[:8]!r} → '
                  f'{host}:{info.port}',
                  file=sys.stderr, flush=True)

        def _forget(self, name):
            # We don't carry a name→peer_id map; the next browse pass
            # repopulates. Simplest correct policy is to leave stale
            # entries — the scheduler will try them and fall through
            # to static endpoints on connect failure.
            pass

    return _Listener


def _start_browse_zeroconf():
    from zeroconf import ServiceBrowser
    zc = _STATE.get('zc')
    if zc is None:
        from zeroconf import Zeroconf
        zc = Zeroconf()
        _STATE['zc'] = zc
    listener = _zc_listener_class()(zc)
    browser = ServiceBrowser(zc, SERVICE_TYPE, listener)
    return browser


def _stop_browse_zeroconf():
    browser = _STATE.get('browse')
    if browser is None:
        return
    try:
        browser.cancel()
    except Exception:
        pass


# ── Android NsdManager path ────────────────────────────────────────────────
#
# pyjnius pattern: Java callback interfaces are implemented via
# ``PythonJavaClass`` subclasses with ``@java_method`` decorators
# carrying the JNI method signature. The Java side holds the listener
# instance; we must keep a Python-side strong ref or GC will free
# the proxy and the next callback SEGVs (same gotcha as
# ``azt_collabd/android_cp/service.py``'s ``_dispatch_cb`` /
# ``_openfile_cb`` globals).

_nsd_register_listener = None     # strong ref
_nsd_discovery_listener = None    # strong ref
_nsd_resolve_listeners = []       # one per pending resolve


def _txt_props_for_nsd(serviceInfo, peer_id_hex, fp_hex):
    """Stamp TXT records onto a NsdServiceInfo via ``setAttribute``.
    The Java API accepts String values and stores them internally as
    UTF-8 bytes; the receiving side reads them back through
    ``getAttributes()`` as ``Map<String, byte[]>``."""
    serviceInfo.setAttribute('peer_id', peer_id_hex)
    serviceInfo.setAttribute('fp', fp_hex)
    serviceInfo.setAttribute('v', str(PROTOCOL_VERSION))


def _decode_nsd_attr(attrs_map, key):
    """Pull a TXT value as a Python str from the
    ``Map<String, byte[]>`` returned by ``getAttributes()``."""
    if not attrs_map.containsKey(key):
        return ''
    raw = attrs_map.get(key)
    if raw is None:
        return ''
    # raw is a Java byte[] — convert via bytes(...) over the iterable.
    try:
        return bytes(raw).decode('utf-8', 'ignore')
    except Exception:
        return ''


def _build_register_listener_class():
    from jnius import PythonJavaClass, java_method

    class _RegisterListener(PythonJavaClass):
        __javainterfaces__ = [
            'android/net/nsd/NsdManager$RegistrationListener']
        __javacontext__ = 'app'

        @java_method('(Landroid/net/nsd/NsdServiceInfo;I)V')
        def onRegistrationFailed(self, serviceInfo, errorCode):
            print(f'[lan-discovery] nsd register failed: '
                  f'errorCode={errorCode}',
                  file=sys.stderr, flush=True)

        @java_method('(Landroid/net/nsd/NsdServiceInfo;I)V')
        def onUnregistrationFailed(self, serviceInfo, errorCode):
            print(f'[lan-discovery] nsd unregister failed: '
                  f'errorCode={errorCode}',
                  file=sys.stderr, flush=True)

        @java_method('(Landroid/net/nsd/NsdServiceInfo;)V')
        def onServiceRegistered(self, serviceInfo):
            try:
                name = serviceInfo.getServiceName()
            except Exception:
                name = '?'
            print(f'[lan-discovery] nsd registered: {name!r}',
                  file=sys.stderr, flush=True)

        @java_method('(Landroid/net/nsd/NsdServiceInfo;)V')
        def onServiceUnregistered(self, serviceInfo):
            print('[lan-discovery] nsd unregistered',
                  file=sys.stderr, flush=True)

    return _RegisterListener


def _build_resolve_listener_class():
    """Each ``resolveService`` call needs its own ResolveListener
    instance — some Android versions reject concurrent resolves on
    the same listener. We append the freshly-built listener to
    ``_nsd_resolve_listeners`` so it survives the dispatch."""
    from jnius import PythonJavaClass, java_method

    class _ResolveListener(PythonJavaClass):
        __javainterfaces__ = [
            'android/net/nsd/NsdManager$ResolveListener']
        __javacontext__ = 'app'

        @java_method('(Landroid/net/nsd/NsdServiceInfo;I)V')
        def onResolveFailed(self, serviceInfo, errorCode):
            print(f'[lan-discovery] nsd resolve failed: '
                  f'errorCode={errorCode}',
                  file=sys.stderr, flush=True)
            try:
                _nsd_resolve_listeners.remove(self)
            except ValueError:
                pass

        @java_method('(Landroid/net/nsd/NsdServiceInfo;)V')
        def onServiceResolved(self, serviceInfo):
            try:
                attrs = serviceInfo.getAttributes()
                peer_id = _decode_nsd_attr(attrs, 'peer_id')
                if len(peer_id) != 64:
                    return
                host = serviceInfo.getHost().getHostAddress()
                port = int(serviceInfo.getPort())
                with _LOCK:
                    _endpoints[peer_id] = (host, port)
                print(f'[lan-discovery] nsd resolved '
                      f'{peer_id[:8]!r} → {host}:{port}',
                      file=sys.stderr, flush=True)
                # Persist the freshly-resolved endpoint into the
                # paired-peer record's ``static_endpoints``. Without
                # this, ``static_endpoints`` stays frozen at
                # pair-time forever — and after a daemon respawn
                # (mDNS state empty) we'd fall back to the
                # pair-time port, which is invariably stale because
                # the peer rebinds ``0.0.0.0:0`` each start. The
                # field log baf 2026-05-22 showed exactly this:
                # cached static endpoint pointed at ``46553`` from
                # an earlier session, peer was actually at
                # ``42539``, fanout hammered the dead port for
                # ages. Persisting on every successful resolve
                # makes the static drift forward to track the
                # peer's current location. Idempotent — the
                # underlying ``set_static_endpoints`` is a no-op
                # if the value is unchanged.
                try:
                    _persist_resolved_endpoint(peer_id, host, port)
                except Exception as ex:
                    print(f'[lan-discovery] persist resolved '
                          f'endpoint {peer_id[:8]!r} failed: '
                          f'{ex!r}',
                          file=sys.stderr, flush=True)
            except Exception as ex:
                print(f'[lan-discovery] resolve callback raised: '
                      f'{ex!r}', file=sys.stderr, flush=True)
            finally:
                try:
                    _nsd_resolve_listeners.remove(self)
                except ValueError:
                    pass

    return _ResolveListener


def _build_discovery_listener_class():
    from jnius import PythonJavaClass, java_method, autoclass

    NsdManager = autoclass('android.net.nsd.NsdManager')

    def _get_app_nsd_manager():
        try:
            ActivityThread = autoclass('android.app.ActivityThread')
            app = ActivityThread.currentApplication()
            if app is None:
                return None
            return app.getSystemService('servicediscovery')
        except Exception:
            return None

    class _DiscoveryListener(PythonJavaClass):
        __javainterfaces__ = [
            'android/net/nsd/NsdManager$DiscoveryListener']
        __javacontext__ = 'app'

        @java_method('(Ljava/lang/String;I)V')
        def onStartDiscoveryFailed(self, serviceType, errorCode):
            print(f'[lan-discovery] nsd start discovery failed: '
                  f'errorCode={errorCode}',
                  file=sys.stderr, flush=True)

        @java_method('(Ljava/lang/String;I)V')
        def onStopDiscoveryFailed(self, serviceType, errorCode):
            print(f'[lan-discovery] nsd stop discovery failed: '
                  f'errorCode={errorCode}',
                  file=sys.stderr, flush=True)

        @java_method('(Ljava/lang/String;)V')
        def onDiscoveryStarted(self, serviceType):
            print('[lan-discovery] nsd discovery started',
                  file=sys.stderr, flush=True)

        @java_method('(Ljava/lang/String;)V')
        def onDiscoveryStopped(self, serviceType):
            print('[lan-discovery] nsd discovery stopped',
                  file=sys.stderr, flush=True)

        @java_method('(Landroid/net/nsd/NsdServiceInfo;)V')
        def onServiceFound(self, serviceInfo):
            # NsdManager hands us a stub at this stage (name+type
            # only). Kick off a resolve to get the IP+port+TXT.
            nsd = _get_app_nsd_manager()
            if nsd is None:
                return
            try:
                listener = _build_resolve_listener_class()()
                _nsd_resolve_listeners.append(listener)
                nsd.resolveService(serviceInfo, listener)
            except Exception as ex:
                print(f'[lan-discovery] resolveService raised: '
                      f'{ex!r}', file=sys.stderr, flush=True)

        @java_method('(Landroid/net/nsd/NsdServiceInfo;)V')
        def onServiceLost(self, serviceInfo):
            # We don't keep a name→peer_id map so we can't precisely
            # invalidate. Leave the cached endpoint and let the
            # scheduler's drain try → fail → fall through to static
            # endpoints.
            pass

    return _DiscoveryListener


def _get_nsd_manager():
    from jnius import autoclass
    try:
        ActivityThread = autoclass('android.app.ActivityThread')
        app = ActivityThread.currentApplication()
        if app is None:
            return None
        return app.getSystemService('servicediscovery')
    except Exception as ex:
        print(f'[lan-discovery] NsdManager lookup failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None


def _start_advertise_nsd(peer_id_hex, fp_hex, port, device_name):
    global _nsd_register_listener
    try:
        from jnius import autoclass
        nsd = _get_nsd_manager()
        if nsd is None:
            return None
        NsdManager = autoclass('android.net.nsd.NsdManager')
        NsdServiceInfo = autoclass('android.net.nsd.NsdServiceInfo')
        info = NsdServiceInfo()
        # NsdManager renames on collision; the user-visible label
        # stays unique. Underlying peer_id (in TXT) is what the
        # daemon uses anyway.
        info.setServiceName(device_name or 'AZT device')
        info.setServiceType(SERVICE_TYPE_ANDROID)
        info.setPort(int(port))
        _txt_props_for_nsd(info, peer_id_hex, fp_hex)
        listener = _build_register_listener_class()()
        nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, listener)
        _nsd_register_listener = listener
        return info
    except Exception as ex:
        print(f'[lan-discovery] NsdManager advertise failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None


def _stop_advertise_nsd():
    global _nsd_register_listener
    if _nsd_register_listener is None:
        return
    try:
        nsd = _get_nsd_manager()
        if nsd is not None:
            nsd.unregisterService(_nsd_register_listener)
    except Exception as ex:
        print(f'[lan-discovery] unregisterService raised: {ex!r}',
              file=sys.stderr, flush=True)
    _nsd_register_listener = None


def _start_browse_nsd():
    global _nsd_discovery_listener
    try:
        from jnius import autoclass
        nsd = _get_nsd_manager()
        if nsd is None:
            return None
        NsdManager = autoclass('android.net.nsd.NsdManager')
        listener = _build_discovery_listener_class()()
        # FLAG_SHOW_PICKER + DiscoveryRequest.Builder is an Android-16+
        # API that dodges the upcoming ACCESS_LOCAL_NETWORK runtime
        # permission. Falling back to the legacy ``discoverServices``
        # for now — it works on every supported version and the
        # ACCESS_LOCAL_NETWORK enforcement only ramps in for apps
        # targeting SDK 37. Bump to the picker API once we target 36+.
        nsd.discoverServices(
            SERVICE_TYPE_ANDROID, NsdManager.PROTOCOL_DNS_SD, listener)
        _nsd_discovery_listener = listener
        return listener
    except Exception as ex:
        print(f'[lan-discovery] NsdManager browse failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None


def _stop_browse_nsd():
    global _nsd_discovery_listener
    if _nsd_discovery_listener is None:
        return
    try:
        nsd = _get_nsd_manager()
        if nsd is not None:
            nsd.stopServiceDiscovery(_nsd_discovery_listener)
    except Exception as ex:
        print(f'[lan-discovery] stopServiceDiscovery raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
    _nsd_discovery_listener = None


def start_advertise(peer_id_hex, fp_hex, port, device_name=''):
    """Advertise this daemon on mDNS. No-op if neither
    python-zeroconf nor pyjnius is available. Idempotent."""
    with _LOCK:
        if _STATE['advertise'] is not None:
            return
        mode = _STATE['mode'] or _detect_mode()
        _STATE['mode'] = mode
        if mode == 'zeroconf':
            _STATE['advertise'] = _start_advertise_zeroconf(
                peer_id_hex, fp_hex, port, device_name)
        elif mode == 'nsd':
            _STATE['advertise'] = _start_advertise_nsd(
                peer_id_hex, fp_hex, port, device_name)


def stop_advertise():
    """Withdraw the mDNS advertisement. Sends a goodbye packet via
    zeroconf / NsdManager so paired peers' caches drop us
    immediately."""
    with _LOCK:
        mode = _STATE['mode']
        if mode == 'zeroconf':
            _stop_advertise_zeroconf()
        elif mode == 'nsd':
            _stop_advertise_nsd()
        _STATE['advertise'] = None


def start_browse():
    """Start browsing for peer daemons. Discovered endpoints land
    in the in-memory cache; query via ``get_endpoint``. Idempotent."""
    with _LOCK:
        if _STATE['browse'] is not None:
            return
        mode = _STATE['mode'] or _detect_mode()
        _STATE['mode'] = mode
        if mode == 'zeroconf':
            _STATE['browse'] = _start_browse_zeroconf()
        elif mode == 'nsd':
            _STATE['browse'] = _start_browse_nsd()


def stop_browse():
    """Stop browsing. The in-memory endpoint cache is *not* cleared
    — paired peers we discovered are still reachable via the cached
    endpoint until the next session, and the scheduler's drain loop
    can use them. Restart browse to re-resolve."""
    with _LOCK:
        mode = _STATE['mode']
        if mode == 'zeroconf':
            _stop_browse_zeroconf()
        elif mode == 'nsd':
            _stop_browse_nsd()
        _STATE['browse'] = None


def restart_browse():
    """Stop and re-start discovery. Clears NsdManager's internal
    state cache so the next ``onServiceFound`` event surfaces the
    peer's *current* mDNS advertisement, not whatever it had
    buffered before the peer rebound to a new port. Equivalent to
    the user manually flipping the LAN toggle off+on — observed
    in the field (baf 2026-05-22) to recover from "cached endpoint
    refuses, mDNS doesn't re-resolve" without re-pairing.

    Idempotent and safe to call when not already browsing.
    Clears the in-memory endpoint cache so callers can't use the
    stale value while resolution is in flight."""
    print(f'[lan-discovery] restart_browse: '
          f'clearing endpoint cache + restarting discovery',
          file=sys.stderr, flush=True)
    stop_browse()
    with _LOCK:
        _endpoints.clear()
    start_browse()
