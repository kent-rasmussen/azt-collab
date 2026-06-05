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
# peer_id_hex → (host, port, last_seen_monotonic). Mutated by the
# browse callback; read by the scheduler's fan-out path. The
# timestamp is a ``time.monotonic()`` value; entries older than
# ``_ENDPOINT_TTL_S`` are treated as missing by ``get_endpoint``
# so a peer that restarted on a new ephemeral port stops getting
# hammered by the fan-out loop after the TTL elapses (was: cached
# forever until manual LAN toggle flip OR the 3-failure
# restart-browse threshold tripped). See audit finding #6.
_endpoints = {}


def _fire_arrival(peer_id_hex):
    """Spawn a worker thread that runs ``lan_push.sweep_peer`` for a
    paired peer that just transitioned from absent → present in
    mDNS. Lazy import to avoid an ``lan_push → lan_discovery →
    lan_push`` cycle at module load (``lan_push`` already imports
    this module for ``_resolve_endpoint``).

    Best-effort: per-peer failures are isolated. ``sweep_peer``
    walks every shared project with the peer and pushes only the
    ones they're behind on (via ``_push_to_peer``'s pre-flight
    ls-remote no-op short-circuit). For an in-sync peer this is
    one ls-remote round-trip per shared project — cheap, and
    discovers the "we should be talking" case for free.

    Gated on the peer actually being in ``peers.json``: a non-
    paired peer arriving on mDNS isn't a sweep target. Their
    arrival shows up in the "Nearby (unpaired)" UI for the user
    to act on; no auto-push without a prior pair gesture."""
    def _worker():
        try:
            from . import peers as _peers
            if _peers.get_peer(peer_id_hex) is None:
                return
            from . import lan_push as _lan_push
            _lan_push.sweep_peer(peer_id_hex)
        except Exception as ex:
            print(f'[lan-discovery] arrival sweep '
                  f'{peer_id_hex[:8]!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
    threading.Thread(target=_worker, daemon=True,
                     name='lan-arrival-sweep').start()

# Per-peer device_name cache. Populated by the discovery callbacks
# when a peer's mDNS TXT carries the ``device_name`` field (since
# 0.50.39 — older peers don't advertise it, so older entries
# remain empty here and the UI falls back to the peer_id prefix).
# Keyed by peer_id, value is the decoded UTF-8 string. Separate
# from ``_endpoints`` so the existing 3-tuple shape stays stable
# for all the fan-out / scheduler code that depends on it.
_device_names = {}

# Endpoint cache TTL. Sized for the field workflow: an mDNS
# responder re-announces every 30-120 s under standard zeroconf
# defaults, so 5 min covers ~5 announce cycles of headroom before
# we'd treat an entry as stale. A peer that's gone offline (or
# restarted on a new port) stops getting fan-out requests after
# ~5 min; the next mDNS announce repopulates the entry.
_ENDPOINT_TTL_S = 300.0


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
    """Return ``(host, port)`` last seen for *peer_id_hex* via
    mDNS, or ``None`` if not cached OR cached value is older than
    ``_ENDPOINT_TTL_S`` (default 5 min). Per-process in-memory
    cache; safe from any thread."""
    import time
    with _LOCK:
        entry = _endpoints.get(peer_id_hex)
        if entry is None:
            return None
        host, port, last_seen = entry
        if time.monotonic() - last_seen > _ENDPOINT_TTL_S:
            # Expired — drop so the next call doesn't repay this
            # comparison cost on every iteration.
            del _endpoints[peer_id_hex]
            return None
        return (host, port)


def known_device_names():
    """Return a copy of the ``peer_id → device_name`` map populated
    by the discovery callbacks from mDNS TXT. Empty dict if no
    peer has advertised the field yet (pre-0.50.39 peers don't).
    Used by ``_h_lan_nearby_unpaired`` so the UI can render a
    real name for unpaired peers instead of falling back to the
    peer_id prefix. Since 0.50.39."""
    with _LOCK:
        return dict(_device_names)


def known_endpoints():
    """Return a copy of the (non-expired) ``peer_id → (host, port)``
    map. For diagnostics + the scheduler's fan-out planner."""
    import time
    now = time.monotonic()
    with _LOCK:
        return {pid: (h, p)
                for pid, (h, p, ts) in _endpoints.items()
                if now - ts <= _ENDPOINT_TTL_S}


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


def _zc_props(peer_id_hex, fp_hex, device_name=''):
    """Build the TXT record dict for zeroconf advertisement.
    ``device_name`` was added in 0.50.39 — peers running older
    daemons won't advertise it, and discovery callbacks default
    to empty when the key is missing. UTF-8 encoded so non-ASCII
    device names (e.g., user-set names with diacritics) survive
    the round trip."""
    return {
        b'peer_id': peer_id_hex.encode('ascii'),
        b'fp': fp_hex.encode('ascii'),
        b'v': str(PROTOCOL_VERSION).encode('ascii'),
        b'device_name': (device_name or '').encode('utf-8'),
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
        properties=_zc_props(peer_id_hex, fp_hex, device_name),
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
            device_name = (props.get(b'device_name') or b'').decode(
                'utf-8', 'ignore')
            import time as _time
            now = _time.monotonic()
            # Transition detection (0.50.45): the peer is
            # "arriving" if there's no prior endpoint, the prior
            # entry is past TTL, or the host/port changed (peer
            # rebound to a new port — Wi-Fi flap or daemon
            # respawn). On transition, fire ``_fire_arrival``
            # which sweeps any shared projects the peer is
            # behind on.
            with _LOCK:
                prev = _endpoints.get(peer_id)
                is_arrival = (
                    prev is None
                    or (now - prev[2]) > _ENDPOINT_TTL_S
                    or prev[0] != host
                    or prev[1] != int(info.port))
                _endpoints[peer_id] = (host, int(info.port), now)
                if device_name:
                    _device_names[peer_id] = device_name
            print(f'[lan-discovery] add {peer_id[:8]!r} → '
                  f'{host}:{info.port}'
                  + (f' name={device_name!r}' if device_name else '')
                  + (' [arrival]' if is_arrival else ''),
                  file=sys.stderr, flush=True)
            if is_arrival:
                _fire_arrival(peer_id)

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


def _txt_props_for_nsd(serviceInfo, peer_id_hex, fp_hex,
                       device_name=''):
    """Stamp TXT records onto a NsdServiceInfo via ``setAttribute``.
    The Java API accepts String values and stores them internally as
    UTF-8 bytes; the receiving side reads them back through
    ``getAttributes()`` as ``Map<String, byte[]>``.

    ``device_name`` added in 0.50.39 so the "Nearby (unpaired)"
    UI can show a real name instead of the peer_id prefix.
    Pre-0.50.39 peers don't advertise it and the discovery side
    defaults to empty."""
    serviceInfo.setAttribute('peer_id', peer_id_hex)
    serviceInfo.setAttribute('fp', fp_hex)
    serviceInfo.setAttribute('v', str(PROTOCOL_VERSION))
    if device_name:
        serviceInfo.setAttribute('device_name', device_name)


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
                device_name = _decode_nsd_attr(attrs, 'device_name')
                import time as _time
                now = _time.monotonic()
                # Transition detection (0.50.45) — see the zeroconf
                # ``_record`` for the rationale. Same shape on NSD.
                with _LOCK:
                    prev = _endpoints.get(peer_id)
                    is_arrival = (
                        prev is None
                        or (now - prev[2]) > _ENDPOINT_TTL_S
                        or prev[0] != host
                        or prev[1] != port)
                    _endpoints[peer_id] = (host, port, now)
                    if device_name:
                        _device_names[peer_id] = device_name
                print(f'[lan-discovery] nsd resolved '
                      f'{peer_id[:8]!r} → {host}:{port}'
                      + (f' name={device_name!r}'
                         if device_name else '')
                      + (' [arrival]' if is_arrival else ''),
                      file=sys.stderr, flush=True)
                if is_arrival:
                    _fire_arrival(peer_id)
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
        _txt_props_for_nsd(info, peer_id_hex, fp_hex, device_name)
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
