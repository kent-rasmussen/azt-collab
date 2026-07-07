"""
Loopback HTTP/JSON server.

Binds 127.0.0.1 on an OS-assigned port and writes
``$AZT_HOME/server.json`` with ``{port, token, pid, version}`` so clients
can discover the endpoint. Every request (except ``GET /v1/health``)
requires ``Authorization: Bearer <token>``.

Endpoints:
    GET  /v1/health                           unauthenticated liveness probe
    GET  /v1/online                           wraps net._has_internet
    GET  /v1/credentials/status               describes what's configured
    POST /v1/credentials/host                 {host}
    POST /v1/credentials/github/tokens        {access_token, refresh_token,
                                               username, token_time?}
    POST /v1/credentials/github/app_installed {installed}
    POST /v1/credentials/gitlab               {username, token}
    POST /v1/credentials/gitlab/test          {username?, token?}
                                              — falls back to stored creds
                                                if body fields are absent
    POST /v1/credentials/migrate_from_prefs   {prefs_path}
    GET  /v1/recent/last_project              → {langcode}
    POST /v1/recent/last_project              {langcode}
                                              — explicit override; every
                                                langcode-bound endpoint
                                                already auto-stamps via
                                                _touch_project
    POST /v1/sync                             {project_dir, contributor}
                                              — creds come from the store
"""

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import signal
import socketserver
import sys
import threading
import time as _time

from . import auth
from . import cawl as _cawl
from . import config as _config
from . import lan_listener as _lan_listener
from . import peer_id as _peer_id
from . import peers as _peers
from . import settings as _settings
from . import projects
from . import scheduler
from . import store
from .locks import project_lock
from .net import _has_internet
from .paths import azt_home, server_info_path
from . import repo as repo_mod
from .repo import sync_repo as _sync_repo, repo_status_summary as _repo_status
from .status import Result, Status
from . import status as S
from . import __version__ as _VERSION
from . import MIN_CLIENT_VERSION as _MIN_CLIENT_VERSION
from . import __fingerprint__ as _FINGERPRINT
from ._fingerprint import module_fingerprints as _module_fingerprints
_MODULES = _module_fingerprints()

# Kept alive for the server's lifetime so the flock on server.lock stays held.
_server_lock_fd = None
_started_at = 0.0


_AZTCOLLAB_AUTHORITY = 'org.atoznback.aztcollab'


def _on_android():
    """Detect Android via jnius availability. Used by the API-response
    URI conversion below — the daemon's projects.json stores
    filesystem paths (the daemon needs them for dulwich), but peer
    apps on Android can't open() those paths because they're inside
    the server APK's private filesDir. So API responses convert
    lift_path to a content:// URI on Android, which peers feed
    directly to LiftHandle (which transparently handles both
    filesystem paths and content URIs)."""
    try:
        import jnius  # noqa: F401
        return True
    except ImportError:
        return False


def _project_for_api(p):
    """Convert a Project to its API-facing dict.

    Two adaptations:

    * On Android, replaces ``lift_path`` (a filesystem path inside
      the server APK's sandbox, useless to peer apps in other
      packages) with the equivalent content URI under our
      ContentProvider authority. The URI shape mirrors what the
      picker emits in its result Intent —
      ``content://org.atoznback.aztcollab/<lang>/<basename>`` — so
      peers that called LiftHandle on the picker URI can do the same
      with the Project from list_projects / open_project on later
      runs without knowing about the path-vs-URI distinction.

    * Adds a ``lift_exists`` boolean computed against the actual
      filesystem path. The daemon's projects.json can outlive the
      LIFT file (the file may be deleted out-of-band: user wipe,
      external rm, sync conflict resolution, etc.). Peers that resolve
      a recent / favourite project to a Project record need to know
      whether the file is still openable BEFORE they hand the URI to
      LiftHandle and crash on a not-found. UI can hide / mark / offer
      re-clone based on this flag.
    """
    d = p.to_dict()
    fs_path = p.lift_path  # always the filesystem path, pre-URI
    d['lift_exists'] = bool(fs_path and os.path.isfile(fs_path))
    if _on_android() and d.get('lift_path'):
        # If lift_path is already a URI (e.g. registered by a future
        # caller that did its own conversion), don't double-wrap.
        if not d['lift_path'].startswith('content://'):
            basename = os.path.basename(d['lift_path'])
            d['lift_path'] = (
                f'content://{_AZTCOLLAB_AUTHORITY}/'
                f'{p.langcode}/{basename}')
    return d


def _state_dir():
    p = os.path.join(azt_home(), 'state')
    os.makedirs(p, exist_ok=True)
    return p


def _crash_log_path():
    return os.path.join(_state_dir(), 'crash.log')


def _started_path():
    return os.path.join(_state_dir(), 'started.json')


def _last_crash_summary():
    path = _crash_log_path()
    try:
        with open(path) as f:
            data = f.read()
    except FileNotFoundError:
        return None
    if not data:
        return None
    # Each crash is appended as a JSON line; take the last one
    last = ''
    for line in data.splitlines():
        if line.strip():
            last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except Exception:
        return {'raw': last[-200:]}


def _last_started_summary():
    try:
        with open(_started_path()) as f:
            return json.load(f)
    except Exception:
        return None


def _record_started():
    global _started_at
    import time
    _started_at = time.time()
    try:
        with open(_started_path(), 'w') as f:
            json.dump({'pid': os.getpid(), 'ts': _started_at,
                       'version': _VERSION}, f)
    except OSError:
        pass


def _record_crash(exc, where=''):
    import time
    import traceback
    try:
        with open(_crash_log_path(), 'a') as f:
            f.write(json.dumps({
                'ts': time.time(),
                'pid': os.getpid(),
                'version': _VERSION,
                'where': where,
                'type': type(exc).__name__,
                'message': str(exc)[:500],
                'tb': ''.join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__))[-2000:],
            }) + '\n')
    except OSError:
        pass


def _acquire_server_lock(lock_path):
    """Take an exclusive flock on *lock_path* so only one azt_collabd per
    AZT_HOME runs at a time. Returns the file descriptor on success, or
    None if another instance holds the lock. On platforms without fcntl
    (Windows), returns the fd without real locking (first-come wins by
    server.json existence instead)."""
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        import fcntl as _fcntl
    except ImportError:
        return fd
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.truncate(fd, 0)
        os.write(fd, f'{os.getpid()}\n'.encode())
    except OSError:
        pass
    return fd


# ---------------------------------------------------------------------------
# Transport-agnostic dispatch table
#
# Both the HTTP handler below and the future Android ContentProvider call
# dispatch(method, path, body) → (status: int, response: dict). Auth is
# the caller's responsibility; the dispatcher trusts that anything reaching
# it has been authorized. /v1/health is the only path the dispatcher handles
# specially: callers may invoke it without auth.
# ---------------------------------------------------------------------------

UNAUTHENTICATED_PATHS = ('/v1/health',)


def _h_health(_body):
    # Test hook: if ``$AZT_HOME/_debug_force_503`` exists, return
    # 503 so the bootstrap can exercise the "AZT Collaboration not
    # responding" popup that fires after warm-up retries exhaust.
    # Toggle without restarting the daemon — the file presence is
    # checked per-request, so create / remove takes effect on the
    # next /v1/health probe.
    #
    # On Android (where ``$AZT_HOME`` is the server APK's private
    # filesDir, not user-writable from outside the app), create
    # the sentinel via adb:
    #
    #     adb shell run-as org.atoznback.aztcollab \
    #         touch files/azt/_debug_force_503
    #
    # Remove with ``rm files/azt/_debug_force_503`` to restore
    # normal behaviour. On desktop the sentinel lives at
    # ``~/.local/share/azt/_debug_force_503``.
    if os.path.exists(os.path.join(azt_home(), '_debug_force_503')):
        return 503, {"ok": False, "error": "daemon_not_ready",
                     "debug": "sentinel file present"}
    payload = {
        "ok": True, "version": _VERSION,
        "min_client_version": _MIN_CLIENT_VERSION,
        # Content fingerprint of the deployed daemon code (.py
        # contents under ``azt_collabd/`` + ``azt_collab_client/``,
        # SHA-256, first 16 hex chars). Independent of
        # ``__version__``: two bundles claiming the same version
        # but built from different bytes have different
        # fingerprints. Lets diagnosis distinguish "deploy didn't
        # take" from "code is fine, behaviour is wrong". Compare
        # against `python -m azt_collabd fingerprint` run from the
        # source tree you expect to be deployed. Since 0.50.31.
        "fingerprint": _FINGERPRINT,
        # Per-module fingerprint breakdown — one hash per .py /
        # .pyc file in the daemon's combined bundle. Diagnostic
        # when the combined ``fingerprint`` shifts but only some
        # files actually changed: compare modules dicts and the
        # diverging entries point at the stale files. Since
        # 0.50.34, after the 0.50.30 telemetry fix appeared
        # deployed (overall fingerprint changed) but the bug
        # behaviour persisted because cawl.py specifically was
        # stale — overall hash can't reveal partial-staleness,
        # per-module breakdown can.
        "modules": _MODULES,
        "pid": os.getpid(),
        "started_at": _started_at,
    }
    crash = _last_crash_summary()
    if crash is not None:
        payload['last_crash'] = crash
    # Ungraceful-shutdown detection: the previous daemon process
    # exited without running atexit (SIGSEGV / SIGKILL / OOM-kill /
    # ``os._exit``). Surfaced separately from ``last_crash`` because
    # ``last_crash`` is written by the Python excepthook (caught
    # exception, daemon still alive to write it), while
    # ``last_native_crash`` is detected on the NEXT startup from a
    # sentinel-file diff (signal handler bypassed Python entirely).
    # See ``azt_collabd/crash_marker.py``.
    try:
        from . import crash_marker
        native = crash_marker.read_last_native_crash(azt_home())
        if native is not None:
            payload['last_native_crash'] = native
    except Exception:
        pass
    return 200, payload


def _h_online(_body):
    return 200, {"ok": True, "online": _has_internet()}


def _h_credentials_status(_body):
    return 200, {"ok": True, **store.get_status()}


def _refuse_if_contributor_unset():
    """Return a 200 / non-ok refusal body when contributor is empty,
    or None when set. LAN signalling endpoints call this to surface
    CONTRIBUTOR_UNSET before doing anything that would advertise an
    anonymous device on the LAN.

    Same shape as the existing GH-publish refusal — peer routes via
    the existing status-code dispatch."""
    if store.get_contributor():
        return None
    return 200, {"ok": True,
                 "result": Result().add(
                     S.CONTRIBUTOR_UNSET).to_dict()}


def _h_get_contributor(_body):
    return 200, {"ok": True, "contributor": store.get_contributor()}


def _h_set_contributor(body):
    name = body.get('contributor', '')
    ok = store.set_contributor(name)
    if not ok:
        # ``set_contributor`` refused — input failed
        # ``is_valid_contributor`` (needs at least one
        # alphanumeric character). Surface so the peer can
        # render a clear error rather than silently leaving
        # the user with the old value and no feedback.
        return 200, {
            "ok": False,
            "error": "invalid_contributor",
            "detail": "Contributor name must contain at least "
                      "one letter or digit.",
            "contributor": store.get_contributor(),
        }
    return 200, {"ok": True, "contributor": store.get_contributor()}


def _h_get_device_name(_body):
    """Return the daemon's stored device-name label. Auto-populates
    from the OS on first read (Android: ``Settings.Global.DEVICE_NAME``
    → ``Build.MANUFACTURER + MODEL``; desktop: ``socket.gethostname()``),
    so the response is never empty after the first call.

    Used as the disambiguator in the git commit author email slot
    (``<contributor>@<device_name>``) so the same human committing
    from multiple devices is still groupable by name on GitHub while
    distinguishable by device in raw git metadata. Peers surface this
    in the daemon settings UI for the user to override if they want
    a friendlier label than the OS default."""
    return 200, {"ok": True, "device_name": store.get_device_name()}


def _h_set_device_name(body):
    """Persist a user-chosen device-name label. Empty string clears,
    re-triggering OS autodetection on next read. Whitespace is
    stripped before persist."""
    name = body.get('device_name', '')
    store.set_device_name(name)
    return 200, {"ok": True, "device_name": store.get_device_name()}


def _h_lan_peer_id(_body):
    """Return this daemon's LAN peer identity. Phase 1 of the LAN
    sync transport (parked design in ``docs/local_lan_sync_stub.md``).

    Response: ``{ok: True, peer_id, fp, device_name}``. Lazy-creates
    the ed25519 keypair + self-signed X.509 cert on first call. If
    ``cryptography`` is unavailable on this platform, returns
    ``{ok: False, error: 'identity_unavailable', detail: …}``.

    Broad Exception catch on top of the RuntimeError catch: any
    unexpected error during ``ensure()`` would otherwise propagate
    to the HTTP handler as a 500 and the peer would see "request
    failed" with no diagnostic. Wrapping here lets the peer-side
    popup render the actual error text so the user (and us) can
    diagnose without grovelling through logcat."""
    try:
        info = _peer_id.ensure()
    except RuntimeError as ex:
        return 200, {"ok": False, "error": "identity_unavailable",
                     "detail": str(ex)}
    except Exception as ex:
        import traceback
        print(f'[server] lan/peer_id raised: {type(ex).__name__}: '
              f'{ex}\n{traceback.format_exc()}',
              file=sys.stderr, flush=True)
        return 200, {"ok": False, "error": "identity_unavailable",
                     "detail": f"{type(ex).__name__}: {ex}"}
    return 200, {
        "ok": True,
        "peer_id": info['peer_id'],
        "fp": info['fp'],
        "device_name": store.get_device_name(),
    }


def _h_lan_list_peers(_body):
    """Return the daemon's paired-peers list. Phase 1 of the LAN
    sync transport. Response: ``{ok: True, peers: [...]}``. Empty
    list if nobody has been paired yet."""
    return 200, {"ok": True, "peers": _peers.list_peers()}


def _h_lan_pair_qr(body):
    """Return the JSON payload to QR-encode for pairing this daemon
    with another device. Phase 2 of the LAN sync transport.

    Body: ``{endpoint: 'ip:port'}`` — the LAN endpoint to advertise
    to the peer who'll scan the QR. Phase 4 will populate this from
    the listener's own bound port; for the phase-2 RPC layer the
    caller passes it (empty string is allowed for the desktop
    two-$AZT_HOME smoke test).

    Response: ``{ok: True, payload: {v, peer_id, fp, endpoint,
    device_name}}`` — the caller renders ``json.dumps(payload)``
    into a QR via ``segno``."""
    try:
        info = _peer_id.ensure()
    except RuntimeError as ex:
        return 200, {"ok": False, "error": "identity_unavailable",
                     "detail": str(ex)}
    body = body or {}
    endpoint = str(body.get('endpoint', '') or '')
    if not endpoint:
        # Auto-populate from the running listener when present.
        bound = _lan_listener.bound_endpoint()
        if bound:
            endpoint = f'{bound[0]}:{bound[1]}'
    # Optional combined-pair-share-clone fields. When the user taps
    # "Share {langcode} project" → "Show QR code", the daemon UI
    # passes the active langcode here; we look up the registered
    # remote_url + vernlang so the receiver gets the complete
    # bundle in one scan per the parked-spec "Combined scan flow".
    #
    # ``vernlang`` is the linguistic code for LIFT entries being
    # *analyzed* (the value LIFT writers stamp). Distinct from
    # ``langcode`` (project key); we send both because a project
    # named ``MyEnglishProject`` analyzes vernlang ``en`` —
    # the receiver needs vernlang separately to write entries
    # correctly. ``effective_vernlang()`` falls back to langcode
    # for projects registered before the field existed.
    langcode = str(body.get('langcode', '') or '')
    repo_url = ''
    vernlang = ''
    if langcode:
        try:
            proj = projects.get(langcode)
            if proj is not None:
                repo_url = str(getattr(proj, 'remote_url', '') or '')
                vernlang = proj.effective_vernlang()
        except Exception as ex:
            print(f'[server] pair_qr: project lookup failed for '
                  f'{langcode!r}: {ex!r}',
                  file=sys.stderr, flush=True)
    payload = {
        'v': 1,
        'peer_id': info['peer_id'],
        'fp': info['fp'],
        'endpoint': endpoint,
        'device_name': store.get_device_name(),
        'langcode': langcode,
        'repo_url': repo_url,
        'vernlang': vernlang,
    }
    # Bind the user-gesture (displaying a QR for this langcode)
    # to subsequent auto-share gating in ``lan_listener``. Without
    # this, an attacker on the LAN can hello with any langcode and
    # get auto-shared into the corresponding project. See
    # ``lan_listener._pending_qr_offers`` for the rationale.
    if langcode:
        try:
            _lan_listener.record_qr_offered(langcode)
        except Exception as ex:
            print(f'[server] record_qr_offered failed: {ex!r}',
                  file=sys.stderr, flush=True)
    return 200, {"ok": True, "payload": payload}


def _h_lan_qr_keepalive(body):
    """``POST /v1/lan/pair/qr/keepalive`` — heartbeat from a share-QR
    screen that's currently displayed (0.52.26). Re-stamps the offer so
    ``qr_offer_active`` stays true while the QR is on screen. The screen
    calls this every ~10 s; the offer self-expires seconds after the
    heartbeats stop (screen closed / app killed). Body: ``{langcode}``."""
    langcode = str((body or {}).get('langcode', '') or '')
    if not langcode:
        return 200, {"ok": False, "error": "missing_langcode"}
    try:
        _lan_listener.record_qr_offered(langcode)
    except Exception as ex:
        print(f'[server] qr keepalive failed: {ex!r}',
              file=sys.stderr, flush=True)
    return 200, {"ok": True}


def _h_lan_qr_close(body):
    """``POST /v1/lan/pair/qr/close`` — the share-QR screen closed;
    revoke the offer immediately rather than waiting out the keepalive
    grace (0.52.26). Body: ``{langcode}``."""
    langcode = str((body or {}).get('langcode', '') or '')
    if not langcode:
        return 200, {"ok": False, "error": "missing_langcode"}
    try:
        _lan_listener.clear_qr_offer(langcode)
    except Exception as ex:
        print(f'[server] qr close failed: {ex!r}',
              file=sys.stderr, flush=True)
    return 200, {"ok": True}


def _h_lan_pair_accept(body):
    """Record a peer into ``peers.json`` from a scanned-QR payload.
    Phase 2 of the LAN sync transport.

    Body: ``{payload: {v, peer_id, fp, endpoint, device_name}}``.

    Response: ``{ok: True, result: {statuses: [LAN_PAIRED]}, peer:
    {...}}``. Re-pair (same peer_id, new fp) refreshes the fingerprint
    and the QR-captured endpoint but preserves existing
    ``shared_projects`` and ``static_endpoints``. Phase 4's TLS
    handshake is the catch for the fingerprint actually being live;
    the LAN_FP_MISMATCH code lives there.

    A v0 / unknown / missing-required-field payload returns
    ``{ok: False, error: 'bad_payload', detail: ...}`` so the picker
    UI can show a clear "QR data looked wrong" message."""
    refusal = _refuse_if_contributor_unset()
    if refusal is not None:
        return refusal
    payload = (body or {}).get('payload') or {}
    if not isinstance(payload, dict):
        return 200, {"ok": False, "error": "bad_payload",
                     "detail": "payload is not an object"}
    v = payload.get('v')
    peer_id = str(payload.get('peer_id', '') or '')
    fp = str(payload.get('fp', '') or '')
    endpoint = str(payload.get('endpoint', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    if v != 1:
        return 200, {"ok": False, "error": "bad_payload",
                     "detail": f"unsupported version: {v!r}"}
    if not peer_id or not fp:
        return 200, {"ok": False, "error": "bad_payload",
                     "detail": "peer_id / fp missing"}
    # 32-byte ed25519 pubkey = 64 hex chars; sha256 fp = 64 hex chars.
    if len(peer_id) != 64 or len(fp) != 64:
        return 200, {"ok": False, "error": "bad_payload",
                     "detail": "peer_id / fp wrong length"}
    entry = _peers.record_pair(peer_id, fp, device_name, endpoint)
    # Best-effort auto-reverse-record: introduce ourselves to the
    # remote listener so the user doesn't have to scan a QR in the
    # other direction. Network / TLS / unreachable failures are
    # non-fatal — the local record is the durable state, and the
    # next sync that lands on the remote will trip the listener's
    # paired-peer check (which fires LAN_FP_MISMATCH if needed).
    if endpoint:
        try:
            host, port_str = endpoint.rsplit(':', 1)
            from . import lan_push as _lan_push
            # Pass the QR's ``langcode`` so the remote's hello
            # handler can add it to their shared_projects allowlist
            # for us in the same gesture — symmetric share without a
            # second tap on the owner side. Empty payload langcode
            # = pair-only QR (no auto-share).
            qr_langcode = str(payload.get('langcode', '') or '')
            _lan_push.hello_to_peer(
                host, int(port_str), fp,
                store.get_device_name(),
                langcode=qr_langcode)
        except Exception as ex:
            print(f'[server] hello to {peer_id[:8]!r} raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
    result = Result()
    result.add(S.LAN_PAIRED, peer_id=peer_id, device_name=device_name)
    return 200, {"ok": True, "result": result.to_dict(),
                 "peer": entry}


def _h_lan_unshare_project(body):
    """Remove a project from a paired peer's outbound share list.
    Inverse of ``_h_lan_send_share_offer`` (which is the share-WITH-
    notification path the client wrapper actually drives).

    Symmetric since 0.50.44: after the local allowlist removal,
    fires a best-effort ``send_share_unshared`` courtesy POST to
    the peer so they can drop *us* from *their* allowlist for the
    same langcode. Without this, the peer keeps auto-fanning-out
    to us on every commit even after we've unshared, and our
    listener no-ops the offer with a logged "carries no repo_url"
    line (visible asymmetry, the user's mental model is "we are
    no longer talking about this project"). Best-effort: failure
    to reach the peer doesn't roll back the local removal.

    Body / response shapes match the inverse pattern.
    """
    langcode = str((body or {}).get('langcode', '') or '')
    peer_id = str((body or {}).get('peer_id', '') or '')
    if not langcode or not peer_id:
        return 200, {"ok": False, "error": "bad_request",
                     "detail": "langcode + peer_id required"}
    entry = _peers.remove_shared_project(peer_id, langcode)
    if entry is None:
        return 200, {"ok": False, "error": "peer_unknown",
                     "detail": f"peer_id {peer_id[:8]!r} not paired"}
    # Best-effort: tell the peer so they mirror the unshare.
    try:
        from . import lan_push as _lan_push
        _lan_push.send_share_unshared(peer_id, langcode)
    except Exception as ex:
        print(f'[server] send_share_unshared peer={peer_id[:8]!r} '
              f'lang={langcode!r} raised: {ex!r}',
              file=sys.stderr, flush=True)
    return 200, {"ok": True, "peer": entry}


def _h_lan_get_toggle(_body):
    """Return the daemon-wide LAN-sync toggle state and the listener's
    bound endpoint if running. Response:
    ``{ok: True, on: bool, endpoint: 'ip:port' or ''}``."""
    on = _settings.lan_allow_sync()
    bound = _lan_listener.bound_endpoint()
    endpoint = f'{bound[0]}:{bound[1]}' if bound else ''
    return 200, {"ok": True, "on": on, "endpoint": endpoint}


def _h_lan_set_toggle(body):
    """Flip the daemon-wide LAN-sync toggle and reconcile the
    listener lifecycle. Body: ``{on: bool}``. Hot-applied — listener
    + (later) NsdManager + FGS promotion happen synchronously.

    Response: ``{ok: True, on, endpoint}`` after reconciliation.
    Turning on requires ``contributor`` set — without it the
    advertised peer label would be anonymous (since 0.47.7
    device_name derives from contributor + autodetect), so we
    refuse with ``CONTRIBUTOR_UNSET`` and the peer UI routes the
    user to the contributor field."""
    desired = bool((body or {}).get('on', False))
    if desired:
        refusal = _refuse_if_contributor_unset()
        if refusal is not None:
            return refusal
    _settings.set_lan_allow_sync(desired)
    try:
        _lan_listener.apply_toggle()
    except Exception as ex:
        print(f'[server] lan toggle apply failed: {ex!r}',
              file=sys.stderr, flush=True)
    on = _settings.lan_allow_sync()
    bound = _lan_listener.bound_endpoint()
    endpoint = f'{bound[0]}:{bound[1]}' if bound else ''
    # Daemon-wide change — push-notify all observers (every project's
    # rendering may shift modes / suffix). Reaches only descendants-
    # mode subscribers on the parent status URI.
    try:
        from .android_cp import notify as _notify
        _notify.notify_global_changed()
    except Exception:
        pass
    return 200, {"ok": True, "on": on, "endpoint": endpoint}


def _h_lan_clone(body):
    """LAN-clone a paired peer's project. Phase 4-6+ combined flow:
    pair → clone-over-LAN → register (no auto-origin-adopt; adopt is
    confirmed via a separate pending decision).

    Body: ``{peer_id, langcode, remote_url?}``. Synchronous; LAN is
    fast and the picker UX needs the result inline.

    Response: ``{ok: True, result: <Result dict>}`` carrying one of
    ``LAN_PROJECT_CLONED`` / ``LAN_PROJECT_REOPENED`` /
    ``LAN_PROJECT_COLLISION_UNRELATED`` (+ optional
    ``LAN_ADOPT_ORIGIN_NEEDED`` / ``LAN_REMOTE_CONFLICT`` overlay)."""
    from . import lan_clone as _lan_clone_mod
    peer_id = str((body or {}).get('peer_id', '') or '')
    langcode = str((body or {}).get('langcode', '') or '')
    remote_url = str((body or {}).get('remote_url', '') or '')
    vernlang = str((body or {}).get('vernlang', '') or '')
    # Active vs passive gesture. True (default, for back-compat
    # with pre-0.47.7 callers that don't pass it) = QR scan /
    # Nearby pair / explicit clone — move last_project to the
    # newly-acquired project so the picker resumes into it.
    # False = passive accept of an incoming share-offer popup —
    # the project lands in the registry without hijacking what
    # the user is working on. See CLIENT_INTEGRATION.md § 20a.
    user_initiated = bool((body or {}).get('user_initiated', True))
    if not peer_id or not langcode:
        return 200, {"ok": False, "error": "bad_request",
                     "detail": "peer_id + langcode required"}
    if not _settings.lan_allow_sync():
        r = Result()
        r.add(S.LAN_TOGGLE_OFF)
        return 200, {"ok": True, "result": r.to_dict()}
    result = _lan_clone_mod.clone_from_peer(
        peer_id, langcode, incoming_url=remote_url,
        incoming_vernlang=vernlang)
    if (user_initiated
            and result.has_any(S.LAN_PROJECT_CLONED,
                               S.LAN_PROJECT_REOPENED)):
        try:
            store.set_last_langcode(langcode)
        except Exception:
            pass
    return 200, {"ok": True, "result": result.to_dict()}


def _h_lan_pending(_body):
    """List pending UI decisions (share offers, pair requests,
    adopt-origin prompts, remote conflicts). Powers the shared
    decisions watcher (``azt_collab_client.ui.decisions``)."""
    from . import pending_decisions as _pending
    return 200, {"ok": True, "decisions": _pending.list_all()}


def _h_lan_nearby_unpaired(_body):
    """Return mDNS-discovered devices NOT in our peers.json.
    Powers the peer-side "Nearby (unpaired)" list with Pair
    buttons. Empty list if discovery hasn't surfaced anyone or
    LAN sync is off.

    For each discovered ``peer_id`` we know the endpoint (host,
    port) from mDNS; the fingerprint and device_name come from
    a cached snapshot the listener built on the last discovery
    sweep. To stay safe against stale mDNS data (peer rebound a
    new port, peer_id renamed), we surface only the
    endpoint-resolvable subset.
    """
    from . import lan_discovery as _lan_discovery
    from . import peers as _peers
    from . import peer_id as _peer_id
    paired_ids = {p['peer_id'] for p in _peers.list_peers()}
    # Self-filter (0.50.39): the daemon advertises its own service
    # on mDNS, and the discovery callbacks happily record it into
    # ``known_endpoints``. Without explicitly skipping our own
    # peer_id here, the Nearby-unpaired list shows the local
    # device — confusing UX. Treat self as if already paired.
    try:
        self_peer_id = _peer_id.ensure().get('peer_id', '') or ''
    except Exception:
        self_peer_id = ''
    if self_peer_id:
        paired_ids = set(paired_ids) | {self_peer_id}
    device_names = _lan_discovery.known_device_names()
    out = []
    for peer_id, (host, port) in _lan_discovery.known_endpoints().items():
        if peer_id in paired_ids:
            continue
        out.append({
            'peer_id': peer_id,
            # ``fp`` still empty pre-pair — TXT carries only
            # ``peer_id`` / ``fp`` / ``v`` / ``device_name``; we
            # could expose ``fp`` too but it's not the user-
            # visible string. The peer record gets the cert
            # fingerprint at pair-accept time.
            'fp': '',
            # device_name surfaced from TXT since 0.50.39. Empty
            # when the discovered peer is on pre-0.50.39 code
            # and doesn't advertise the field; UI falls back to
            # peer_id prefix.
            'device_name': device_names.get(peer_id, '') or '',
            'endpoint': f'{host}:{int(port)}',
        })
    return 200, {"ok": True, "peers": out}


def _h_lan_pair_request_send(body):
    """Initiate a Nearby-pair request to *peer_id*.

    Body: ``{peer_id, langcode?}``. Looks up the peer's endpoint
    in the mDNS cache, POSTs to their listener's
    ``/v1/lan/pair_request``, and records an in-memory outbound
    entry so the peer UI can poll the response state.
    """
    from . import lan_discovery as _lan_discovery
    from . import lan_pair_requests as _lpr
    from . import lan_push as _lan_push
    from . import peer_id as _peer_id_mod
    peer_id = str((body or {}).get('peer_id', '') or '')
    langcode = str((body or {}).get('langcode', '') or '')
    if len(peer_id) != 64:
        return 200, {"ok": False, "error": "bad_request",
                     "detail": "peer_id wrong length"}
    refusal = _refuse_if_contributor_unset()
    if refusal is not None:
        return refusal
    if not _settings.lan_allow_sync():
        r = Result()
        r.add(S.LAN_TOGGLE_OFF)
        return 200, {"ok": True, "result": r.to_dict()}
    endpoint = _lan_discovery.get_endpoint(peer_id)
    if endpoint is None:
        r = Result()
        r.add(S.LAN_PEER_UNREACHABLE, peer_id=peer_id)
        return 200, {"ok": True, "result": r.to_dict()}
    host, port = endpoint
    try:
        ident = _peer_id_mod.ensure()
    except Exception as ex:
        return 200, {"ok": False, "error": "no_lan_identity",
                     "detail": repr(ex)}
    # The receiver's listener uses CERT_NONE + body-claimed
    # identity. The sender's outbound request goes through the
    # same lan_push pinned-context machinery. Since we don't yet
    # know the receiver's fp (mDNS didn't carry it forward into
    # the endpoint cache), we use a non-pinned TLS context for
    # this single signalling POST — same threat model as
    # share_offer / hello_to_peer (assertion is in the body).
    body_obj = {
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'device_name': store.get_device_name(),
        'endpoint': _lan_push._our_endpoint_str(),
        'langcode': langcode,
    }
    status, _resp = _lan_push._https_post_signalling(
        host, int(port), '/v1/lan/pair_request', body_obj)
    if status != 200:
        r = Result()
        r.add(S.LAN_PEER_UNREACHABLE, peer_id=peer_id,
              detail=f'http_status={status}')
        return 200, {"ok": True, "result": r.to_dict()}
    _lpr.record_sent(peer_id, langcode=langcode)
    r = Result()
    r.add(S.LAN_PAIR_REQUEST_PENDING, peer_id=peer_id,
          langcode=langcode)
    return 200, {"ok": True, "result": r.to_dict()}


def _h_lan_pair_request_resolve(body):
    """Resolve an inbound KIND_PAIR_REQUEST.

    Body: ``{decision_id, accept: bool}``.

    Accept path: record the peer locally + send hello-back +
    POST pair_response{accept:true} so the sender's spinner
    clears. The hello-back records the pair on the sender side
    via the standard hello flow.

    Decline path: POST pair_response{accept:false} only; no
    peer record on either side.

    Either way, the pending decision is removed."""
    from . import pending_decisions as _pending
    from . import lan_push as _lan_push
    from . import peer_id as _peer_id_mod
    decision_id = str((body or {}).get('decision_id', '') or '')
    accept = bool((body or {}).get('accept', False))
    if accept:
        refusal = _refuse_if_contributor_unset()
        if refusal is not None:
            return refusal
    decision = _pending.get(decision_id) if decision_id else None
    if (decision is None
            or decision.get('kind') != _pending.KIND_PAIR_REQUEST):
        return 200, {"ok": False, "error": "not_found"}
    params = decision.get('params') or {}
    peer_id = str(params.get('peer_id', '') or '')
    fp = str(params.get('fp', '') or '')
    device_name = str(params.get('device_name', '') or '')
    endpoint = str(params.get('endpoint', '') or '')
    # The sender's pair-context langcode is recorded for future
    # use (per-pair auto-share when histories related) but not
    # acted on at pair time today — share is its own gesture.
    result = Result()
    sender_host, sender_port = '', 0
    if endpoint:
        try:
            sender_host, port_str = endpoint.rsplit(':', 1)
            sender_port = int(port_str)
        except (ValueError, TypeError):
            sender_host, sender_port = '', 0
    if accept:
        _peers.record_pair(peer_id, fp, device_name, endpoint)
        # Hello-back: standard flow records the pair on the
        # sender side. langcode='' here — we don't auto-share
        # at pair time (per the architecture-discussion
        # decision; explicit per-project share comes later).
        # The mutual-share contract for the QR/share-offer
        # paths handles auto-sharing in their own gestures.
        if sender_host:
            try:
                _lan_push.hello_to_peer(
                    sender_host, sender_port, fp,
                    store.get_device_name(), langcode='')
            except Exception as ex:
                print(f'[server] pair-accept hello-back to '
                      f'{peer_id[:8]!r} raised: {ex!r}',
                      file=sys.stderr, flush=True)
        result.add(S.LAN_PAIR_REQUEST_ACCEPTED, peer_id=peer_id,
                   device_name=device_name)
    else:
        result.add(S.LAN_PAIR_REQUEST_DECLINED, peer_id=peer_id)
    # Best-effort pair_response → sender's listener. Non-fatal;
    # the standard hello-back already informed the sender on
    # accept, and decline still works (just no spinner update).
    if sender_host:
        try:
            ident = _peer_id_mod.ensure()
            _lan_push._https_post_signalling(
                sender_host, sender_port, '/v1/lan/pair_response',
                {'peer_id': ident['peer_id'], 'accept': accept})
        except Exception as ex:
            print(f'[server] pair-response to {peer_id[:8]!r} '
                  f'raised: {ex!r}', file=sys.stderr, flush=True)
    _pending.remove(decision_id)
    return 200, {"ok": True, "result": result.to_dict()}


def _h_lan_pair_request_status(body):
    """One-shot poll of the outbound pair-request state for a
    peer. Body: ``{peer_id}``. Returns ``{ok, state}`` where
    state is 'pending' | 'accepted' | 'declined' | 'timeout' |
    'none'. Terminal states clear on read (see
    ``lan_pair_requests.status_for``)."""
    from . import lan_pair_requests as _lpr
    peer_id = str((body or {}).get('peer_id', '') or '')
    if len(peer_id) != 64:
        return 200, {"ok": False, "error": "bad_request"}
    status = _lpr.status_for(peer_id)
    if status is None:
        return 200, {"ok": True, "state": "none"}
    return 200, {"ok": True,
                 "state": status.get('state', 'pending'),
                 "langcode": status.get('langcode', ''),
                 "device_name": status.get('device_name', '')}


def _h_lan_accept_offer(body):
    """Accept a pending share-offer: triggers the LAN clone for the
    referenced peer + langcode. Removes the pending decision only
    when the clone actually delivered a project (CLONED / REOPENED)
    so a transient failure (peer offline, project not yet committed
    owner-side, LAN race) leaves the offer in place for the user to
    retry without re-asking the owner to re-share.

    Body: ``{decision_id}``."""
    from . import pending_decisions as _pending
    from . import lan_clone as _lan_clone_mod
    decision_id = str((body or {}).get('decision_id', '') or '')
    decision = _pending.get(decision_id) if decision_id else None
    if (decision is None
            or decision.get('kind') != _pending.KIND_SHARE_OFFER):
        return 200, {"ok": False, "error": "not_found"}
    params = decision.get('params') or {}
    result = _lan_clone_mod.clone_from_peer(
        str(params.get('peer_id', '') or ''),
        str(params.get('langcode', '') or ''),
        incoming_url=str(params.get('repo_url', '') or ''),
        incoming_vernlang=str(params.get('vernlang', '') or ''))
    delivered = result.has_any(
        S.LAN_PROJECT_CLONED, S.LAN_PROJECT_REOPENED)
    if delivered:
        _pending.remove(decision_id)
        # Passive clone: do NOT touch last_project on share-offer
        # accept. The project lands in the registry; the user
        # explicitly opens it later via the picker. See
        # CLIENT_INTEGRATION.md § 20a "passive clone" rule.
    else:
        # Keep the pending decision so the user can retry once the
        # owner-side issue clears (project committed at least once,
        # owner back on LAN, etc.). The decision_id stays addressable
        # for the next accept_offer call.
        print(f'[server] accept_offer {decision_id!r}: clone did '
              f'not deliver (codes={result.codes()!r}); pending '
              f'kept for retry', file=sys.stderr, flush=True)
    return 200, {"ok": True, "result": result.to_dict()}


def _h_lan_decline_offer(body):
    """Decline a pending share-offer. Best-effort nack to the
    sender. Body: ``{decision_id}``."""
    from . import pending_decisions as _pending
    from . import lan_push as _lan_push
    decision_id = str((body or {}).get('decision_id', '') or '')
    decision = _pending.get(decision_id) if decision_id else None
    if (decision is None
            or decision.get('kind') != _pending.KIND_SHARE_OFFER):
        return 200, {"ok": False, "error": "not_found"}
    params = decision.get('params') or {}
    _pending.remove(decision_id)
    # Best-effort nack to the sender so their UI / log reflects it.
    try:
        _lan_push.share_declined(
            str(params.get('peer_id', '') or ''),
            str(params.get('langcode', '') or ''))
    except Exception as ex:
        print(f'[server] share_declined nack raised: {ex!r}',
              file=sys.stderr, flush=True)
    return 200, {"ok": True}


def _h_lan_adopt_origin(body):
    """Resolve an adopt-origin pending decision. On accept, set
    ``origin`` for the project; on decline, just remove the
    decision. Body: ``{decision_id, accept: bool}``."""
    from . import pending_decisions as _pending
    decision_id = str((body or {}).get('decision_id', '') or '')
    accept = bool((body or {}).get('accept', False))
    decision = _pending.get(decision_id) if decision_id else None
    if (decision is None
            or decision.get('kind') != _pending.KIND_ADOPT_ORIGIN):
        return 200, {"ok": False, "error": "not_found"}
    params = decision.get('params') or {}
    result = Result()
    if accept:
        langcode = str(params.get('langcode', '') or '')
        url = str(params.get('url', '') or '')
        try:
            projects.set_remote_url(langcode, url)
        except Exception as ex:
            result.add(S.SERVER_ERROR,
                       error=f'set_remote_url failed: {ex!r}')
            return 200, {"ok": True, "result": result.to_dict()}
        # Mirror the adoption to ``.git/config``. Without this the
        # registry says "remote_url=X" but the working tree's git
        # config has no origin, so the next push has no remote to
        # send to. Pre-0.50.27 the registry write alone was enough
        # only because publish flowed through ``init_project`` which
        # rewrote ``.git/config`` itself — silent adoption never
        # touched the config and the push silently no-op'd.
        try:
            proj = projects.get(langcode)
            wd = (proj.working_dir if proj is not None else '') or ''
            if wd:
                from . import repo as _repo
                _repo.set_remote_origin_url(wd, url)
        except Exception as ex:
            print(f'[adopt-origin] set_remote_origin_url '
                  f'{langcode!r}: {ex!r}',
                  file=sys.stderr, flush=True)
        result.add(S.LAN_PROJECT_ADOPTED_REMOTE,
                   langcode=langcode, url=url)
    _pending.remove(decision_id)
    return 200, {"ok": True, "result": result.to_dict()}


def _h_lan_resolve_conflict(body):
    """Resolve a remote_conflict pending decision. Body:
    ``{decision_id, mode}`` where mode is one of:

      - ``'use_theirs'`` — replace local ``remote_url`` with theirs.
      - ``'keep_mine'`` — leave local ``remote_url`` unchanged.
      - ``'dual_publish'`` — leave local unchanged; the dual-push
        mechanism is a follow-up (no daemon-side action here, just
        a tag on the decision so the user's choice is recorded)."""
    from . import pending_decisions as _pending
    decision_id = str((body or {}).get('decision_id', '') or '')
    mode = str((body or {}).get('mode', '') or '')
    decision = _pending.get(decision_id) if decision_id else None
    if (decision is None
            or decision.get('kind') != _pending.KIND_REMOTE_CONFLICT):
        return 200, {"ok": False, "error": "not_found"}
    params = decision.get('params') or {}
    langcode = str(params.get('langcode', '') or '')
    incoming_url = str(params.get('incoming_url', '') or '')
    if mode == 'use_theirs':
        try:
            projects.set_remote_url(langcode, incoming_url)
        except Exception as ex:
            return 200, {"ok": False,
                         "error": f'set_remote_url failed: {ex!r}'}
        # Mirror to ``.git/config`` — see ``_h_lan_adopt_origin``
        # for rationale. Without this the registry says one thing
        # and the working tree's origin says another, and the next
        # push uses the old (or empty) URL. Since 0.50.27.
        try:
            proj = projects.get(langcode)
            wd = (proj.working_dir if proj is not None else '') or ''
            if wd:
                from . import repo as _repo
                _repo.set_remote_origin_url(wd, incoming_url)
        except Exception as ex:
            print(f'[resolve-conflict] set_remote_origin_url '
                  f'{langcode!r}: {ex!r}',
                  file=sys.stderr, flush=True)
    elif mode == 'dual_publish':
        # Record incoming as a secondary remote. The push paths
        # (``_push_repo_locked`` / ``_sync_repo_locked``) call
        # ``_push_extras_step`` after the primary; each entry in
        # ``extra_remotes`` is published every push pass, with
        # per-URL ``EXTRA_REMOTE_PUSHED`` / ``EXTRA_REMOTE_PUSH_FAILED``
        # surfaced separately. Secondaries are best-effort and a
        # failure on one doesn't block the others. Per
        # CLIENT_INTEGRATION.md § 20a "Use both" — user picked
        # to publish to both Internet locations.
        try:
            projects.add_extra_remote(langcode, incoming_url)
        except Exception as ex:
            return 200, {"ok": False,
                         "error": f'add_extra_remote failed: {ex!r}'}
    elif mode != 'keep_mine':
        return 200, {"ok": False,
                     "error": f'unknown mode: {mode!r}'}
    _pending.remove(decision_id)
    return 200, {"ok": True}


def _h_lan_send_share_offer(body):
    """Local-side helper called from the daemon settings UI when the
    user taps "Share project with paired phone Y". Updates our
    shared_projects allowlist AND fires the courtesy offer to Y's
    listener so Y sees a pending decision on their side.

    Body: ``{peer_id, langcode}``."""
    from . import lan_push as _lan_push
    peer_id = str((body or {}).get('peer_id', '') or '')
    langcode = str((body or {}).get('langcode', '') or '')
    if not peer_id or not langcode:
        return 200, {"ok": False, "error": "bad_request"}
    refusal = _refuse_if_contributor_unset()
    if refusal is not None:
        return refusal
    # Gate on the daemon-wide LAN toggle: without it the courtesy
    # POST below fails at connect time and the receiver never sees
    # the offer. Bookkeeping the allowlist change in that case
    # would silently desync the two sides' views of "what's shared
    # with whom." Refuse with LAN_TOGGLE_OFF so the UI can route
    # the user to the toggle before retrying.
    if not _settings.lan_allow_sync():
        return 200, {"ok": False, "error": "lan_toggle_off"}
    # Pre-flight: the LAN clone on the receiving side will fail
    # with a generic 404 if our project doesn't have a usable git
    # state (working_dir exists + .git/ exists + HEAD ref is born).
    # Refuse the share gesture here so the user sees a clear reason
    # instead of "I tapped Share, the other phone got an offer,
    # accepted, and nothing happened." Typical trip: fresh project
    # the user created but never recorded into — no commits yet, so
    # dulwich's smart-protocol returns no refs and the cloner sees
    # 404. The fix is "record at least one entry first"; surface
    # that here.
    proj = projects.get(langcode)
    if proj is None:
        return 200, {"ok": False, "error": "project_unknown"}
    project_wd = proj.working_dir or ''
    if not project_wd or not os.path.isdir(
            os.path.join(project_wd, '.git')):
        return 200, {
            "ok": False,
            "error": "project_not_initialised",
            "detail": (
                "This project has no git repository yet. "
                "Record at least one entry, then try sharing again."
            ),
        }
    try:
        from dulwich.repo import Repo
        _r = Repo(project_wd)
        try:
            _r.refs[b'HEAD']
        except (KeyError, Exception) as ex:
            _r.close()
            return 200, {
                "ok": False,
                "error": "project_unborn",
                "detail": (
                    f"This project has no commits yet ({ex!r}). "
                    "Record at least one entry, then try sharing "
                    "again."
                ),
            }
        _r.close()
    except Exception as ex:
        return 200, {
            "ok": False,
            "error": "project_unreadable",
            "detail": f'{ex!r}',
        }
    entry = _peers.add_shared_project(peer_id, langcode)
    if entry is None:
        return 200, {"ok": False, "error": "peer_unknown"}
    # Look up our own remote_url + vernlang for this project so the
    # offer carries them. receiver uses repo_url for the always-
    # confirm adopt-origin prompt and vernlang to tag the LIFT
    # writes correctly post-clone.
    repo_url = ''
    vernlang = ''
    try:
        proj = projects.get(langcode)
        if proj is not None:
            repo_url = str(getattr(proj, 'remote_url', '') or '')
            vernlang = proj.effective_vernlang()
    except Exception:
        pass
    post_status = 0
    dispatch = ''
    try:
        post_status, dispatch = _lan_push.send_share_offer(
            peer_id, langcode, repo_url, vernlang=vernlang)
    except Exception as ex:
        print(f'[server] send_share_offer raised: {ex!r}',
              file=sys.stderr, flush=True)
    # Sender-side success line (0.50.43): previously the daemon
    # was silent on this path, so the user had no way to confirm
    # from logs whether the courtesy POST actually fired. Print
    # one line per call regardless of outcome — peer_id prefix,
    # langcode, HTTPS status (0 on transport failure), receiver
    # dispatch (or '' if pre-0.50.43 receiver / unreachable).
    print(f'[server] send_share_offer peer={peer_id[:8]!r} '
          f'lang={langcode!r} post_status={post_status} '
          f'dispatch={dispatch!r}',
          file=sys.stderr, flush=True)
    return 200, {"ok": True, "peer": entry,
                 "post_status": int(post_status or 0),
                 "dispatch": str(dispatch or '')}


def _h_lan_set_static_endpoints(body):
    """Replace a paired peer's static-endpoint fallback list. Phase
    7 of the LAN sync transport — covers the "I know this paired
    phone's current IP because I asked them" recovery path when
    mDNS is blocked (AP isolation, hotspot, etc.).

    Body: ``{peer_id, endpoints: ['ip:port', ...]}``. Empty list
    clears.

    Response: ``{ok: True, peer}`` on success;
    ``{ok: False, error: 'peer_unknown'}`` if the peer isn't paired."""
    peer_id = str((body or {}).get('peer_id', '') or '')
    raw = (body or {}).get('endpoints') or []
    if not peer_id:
        return 200, {"ok": False, "error": "bad_request",
                     "detail": "peer_id required"}
    if not isinstance(raw, list):
        return 200, {"ok": False, "error": "bad_request",
                     "detail": "endpoints must be a list"}
    endpoints = [str(e) for e in raw if isinstance(e, str) and e]
    entry = _peers.set_static_endpoints(peer_id, endpoints)
    if entry is None:
        return 200, {"ok": False, "error": "peer_unknown"}
    return 200, {"ok": True, "peer": entry}


def _h_lan_unpair(body):
    """Remove a peer from ``peers.json``. Companion to
    ``_h_lan_pair_accept``. Body: ``{peer_id}``. Response: typed
    Result with ``LAN_UNPAIRED`` on success;
    ``{ok: False, error: 'peer_unknown'}`` if the peer wasn't
    paired."""
    peer_id = str((body or {}).get('peer_id', '') or '')
    if not peer_id:
        return 200, {"ok": False, "error": "bad_request",
                     "detail": "peer_id required"}
    removed = _peers.remove_peer(peer_id)
    if not removed:
        return 200, {"ok": False, "error": "peer_unknown"}
    result = Result()
    result.add(S.LAN_UNPAIRED, peer_id=peer_id)
    return 200, {"ok": True, "result": result.to_dict()}


def _h_get_cawl_prefetch_all_variants(_body):
    """Read the CAWL prefetch policy.

    Response: ``{ok: True, enabled: bool}``. False (default)
    means daemon prefetches one variant per CAWL id (the
    preferred-variant filter in ``cawl._index_image_paths``).
    True means prefetch every image-shaped index entry."""
    return 200, {"ok": True,
                 "enabled": store.get_cawl_prefetch_all_variants()}


def _h_get_work_offline(_body):
    """Read the daemon-wide work-offline toggle.

    When true, the connectivity watcher's drain is a no-op and the
    user-initiated Sync button returns ``S.WORK_OFFLINE_ENABLED``
    without attempting any push. Commits via ``commit_project``
    are unaffected; only push is suppressed."""
    from . import settings as _settings
    return 200, {"ok": True, "work_offline": _settings.work_offline()}


def _h_set_work_offline(body):
    """``POST /v1/config/work_offline`` with ``{enabled: bool}``.
    Persists to ``$AZT_HOME/config.json :: sync.work_offline``.

    Toggling OFF fires an immediate push-drain so the user doesn't
    have to wait a full ``connectivity_poll_s`` tick to see their
    pending commits go out."""
    from . import settings as _settings
    from . import scheduler as _scheduler
    prev = _settings.work_offline()
    enabled = bool(body.get('enabled', False))
    _settings.set_work_offline(enabled)
    if prev and not enabled:
        try:
            _scheduler.drain_pushes_now()
        except Exception as ex:
            print(f'[work_offline] drain_pushes_now failed: {ex}',
                  file=sys.stderr, flush=True)
    # Daemon-wide change — push-notify all observers.
    try:
        from .android_cp import notify as _notify
        _notify.notify_global_changed()
    except Exception:
        pass
    return 200, {"ok": True, "work_offline": enabled}


def _h_sync_nudge(body):
    """``POST /v1/sync/nudge`` with optional ``{langcode}`` —
    the unified "try sync now" gesture. Resets WAN backoff for
    one project (or all, if no langcode supplied), fires an
    immediate WAN push pass, and fires a LAN burst-discovery +
    fan-out so paired peers in the room get the latest. Same
    semantics as the sync icon: "I'm telling you to try
    everything right now, regardless of backoff."

    Always returns ``{ok: True}`` — failures are visible via
    the next ``project_status`` poll. The point of the nudge
    is *attempt scheduling*, not delivery confirmation; if
    nothing was actually pending or every attempt failed, the
    user sees that via the same status indicators they were
    already watching.
    """
    langcode = str((body or {}).get('langcode', '') or '')
    from . import scheduler as _scheduler
    try:
        _scheduler.drain_pushes_now(langcode=langcode)
    except Exception as ex:
        print(f'[sync_nudge] drain raised: {ex!r}',
              file=sys.stderr, flush=True)
    # LAN: arm a burst (Phase 3), then fan out. ``start_burst`` is
    # a no-op increment when ``lan.autodiscovery=True`` (the radio
    # is already up); when it's False the burst brings up the
    # listener + mDNS + locks for ``DEFAULT_WINDOW_S`` (default
    # 30 s) so paired peers' parallel bursts can discover us.
    try:
        from . import lan_burst as _lan_burst
        _lan_burst.start_burst()
    except Exception as ex:
        print(f'[sync_nudge] start_burst raised: {ex!r}',
              file=sys.stderr, flush=True)
    # Sync = user-equivalent intent. Force the LAN backoff curve
    # eligible for the project(s) involved so the next post-commit
    # bursts also fire (not just this one). Idempotent.
    try:
        from . import lan_backoff as _lan_backoff
        if langcode:
            _lan_backoff.nudge(langcode)
        else:
            for lang in projects._load_raw():
                _lan_backoff.nudge(lang)
    except Exception as ex:
        print(f'[sync_nudge] lan_backoff.nudge raised: {ex!r}',
              file=sys.stderr, flush=True)
    try:
        if langcode:
            p = projects.get(langcode)
            if p is not None:
                from . import lan_push as _lan_push
                results = _lan_push.fan_out(p)
                if results and any(results.values()):
                    try:
                        from . import lan_backoff as _lan_backoff
                        _lan_backoff.record_success(langcode)
                    except Exception:
                        pass
        else:
            data = projects._load_raw()
            for lang in data:
                p = projects.get(lang)
                if p is not None:
                    try:
                        from . import lan_push as _lan_push
                        results = _lan_push.fan_out(p)
                        if results and any(results.values()):
                            try:
                                from . import lan_backoff as \
                                    _lan_backoff
                                _lan_backoff.record_success(lang)
                            except Exception:
                                pass
                    except Exception as ex:
                        print(f'[sync_nudge] LAN fan-out '
                              f'{lang!r} raised: {ex!r}',
                              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[sync_nudge] LAN dispatch raised: {ex!r}',
              file=sys.stderr, flush=True)
    return 200, {"ok": True}


def _h_lan_debug(langcode, _body):
    """``GET /v1/projects/<lang>/lan_debug`` — diagnostic dump for
    comparing what each phone sees for a given project. No side
    effects, read-only. Added 0.50.45 specifically to chase the
    WAN-302 vs WAN-17 disparity: when two phones report different
    ``wan_unshared`` counts, hit this endpoint on each side and
    compare the fields directly to find where they diverge
    (HEAD SHA, branch name, ancestry depth, refs present).

    Response::

        {"ok": True,
         "langcode": "<project>",
         "head_branch": "main",
         "head_sha": "abc123…",
         "ancestor_count_from_head": 302,
         "has_origin_url": false,
         "origin_url": "",
         "tracking_ref_sha": null,
         "remote_refs_present": ["refs/remotes/origin/main", ...],
         "branches_present": ["refs/heads/main", ...],
         "wan_unshared": 302}

    Errors return ``{"ok": False, "error": "..."}`` so any failed
    field surfaces a reason rather than silently returning 0.
    """
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found",
                     "langcode": langcode}
    if not p.working_dir or not os.path.isdir(
            os.path.join(p.working_dir, '.git')):
        return 200, {"ok": False, "error": "no_repo",
                     "langcode": langcode}
    try:
        from dulwich.repo import Repo
        repo_obj = Repo(p.working_dir)
    except Exception as ex:
        return 200, {"ok": False, "error": "repo_open_failed",
                     "detail": repr(ex), "langcode": langcode}
    out = {"ok": True, "langcode": langcode}
    try:
        # HEAD branch + SHA. ``read_ref(b'HEAD')`` follows
        # symbolic refs and returns the resolved SHA — useless
        # for getting the branch name back out. Use
        # ``get_symrefs`` to get the symbolic-ref mapping; HEAD
        # in there points at ``refs/heads/<branch>`` for an
        # attached HEAD, or is missing for a detached HEAD.
        head_branch = ''
        head_sha = ''
        try:
            symrefs = repo_obj.refs.get_symrefs()
            target = symrefs.get(b'HEAD')
            if target and target.startswith(b'refs/heads/'):
                head_branch = target[len(b'refs/heads/'):] \
                    .decode('utf-8', 'replace')
        except Exception:
            pass
        try:
            head_sha_b = repo_obj.refs[b'HEAD']
            head_sha = head_sha_b.decode('ascii', 'replace')
        except Exception:
            pass
        out['head_branch'] = head_branch
        out['head_sha'] = head_sha
        # Ancestor count
        ancestor_count = 0
        if head_sha:
            try:
                walker = repo_obj.get_walker(
                    include=[head_sha.encode('ascii')])
                ancestor_count = sum(1 for _ in walker)
            except Exception as ex:
                out['ancestor_count_error'] = repr(ex)
        out['ancestor_count_from_head'] = ancestor_count
        # Origin URL
        origin_url = ''
        try:
            url = repo_obj.get_config().get(
                (b'remote', b'origin'), b'url')
            try:
                origin_url = url.decode('utf-8', 'replace').strip()
            except Exception:
                origin_url = ''
        except KeyError:
            origin_url = ''
        out['origin_url'] = origin_url
        out['has_origin_url'] = bool(origin_url)
        # Tracking ref
        tracking_sha = None
        if head_branch:
            ref_name = (b'refs/remotes/origin/'
                        + head_branch.encode('ascii'))
            try:
                tracking_sha = repo_obj.refs[ref_name].decode(
                    'ascii', 'replace')
            except KeyError:
                pass
            except Exception as ex:
                out['tracking_ref_error'] = repr(ex)
        out['tracking_ref_sha'] = tracking_sha
        # All refs present (for spotting orphaned branches)
        remote_refs = []
        branches = []
        try:
            for ref in repo_obj.refs.allkeys():
                ref_s = ref.decode('utf-8', 'replace')
                if ref_s.startswith('refs/heads/'):
                    branches.append(ref_s)
                elif ref_s.startswith('refs/remotes/'):
                    remote_refs.append(ref_s)
        except Exception as ex:
            out['refs_walk_error'] = repr(ex)
        out['branches_present'] = sorted(branches)
        out['remote_refs_present'] = sorted(remote_refs)
        # The current wan_unshared reading for cross-check.
        try:
            from . import repo as _repo_mod
            out['wan_unshared'] = int(
                _repo_mod._wan_unshared(
                    repo_obj, head_branch or 'main'))
        except Exception as ex:
            out['wan_unshared_error'] = repr(ex)
    finally:
        try:
            repo_obj.close()
        except Exception:
            pass
    return 200, out


def _h_lan_burst(_body):
    """``POST /v1/lan/burst`` — bring the LAN radio up for a 30s
    discovery burst without firing WAN drain or fan-out. The peer
    calls this on lifecycle events ("user opened the picker",
    "Activity onResume") to give the daemon a chance to find
    paired peers in the room when the daemon isn't already
    bursting.

    Distinct from ``/v1/sync/nudge`` which also drains WAN +
    fans out for every project — this is the lightweight
    "just listen for a minute" gesture.

    Sweep on arrival (via the discovery transition detector,
    0.50.45) handles the "now that I see B, push them anything
    they're behind on" half. So burst → mDNS finds peers →
    arrival callback → sweep → catch-up. No periodic anything.

    Always returns ``{ok: True}`` — radio-up is a request, not a
    delivery confirmation.
    """
    try:
        from . import lan_burst as _lan_burst
        _lan_burst.start_burst()
    except Exception as ex:
        print(f'[lan_burst] start_burst raised: {ex!r}',
              file=sys.stderr, flush=True)
    return 200, {"ok": True}


def _h_set_cawl_prefetch_all_variants(body):
    """``POST /v1/config/cawl_prefetch_all_variants`` with
    ``{enabled: bool}``. Persists to
    ``$AZT_HOME/config.json :: cawl.prefetch_all_variants``.

    Flipping the policy doesn't retro-trigger a prefetch — the
    next ``auto_prefetch`` (e.g. next project-load or
    scheduler edge) will pick up the new path set."""
    enabled = bool(body.get('enabled', False))
    store.set_cawl_prefetch_all_variants(enabled)
    return 200, {"ok": True, "enabled": enabled}


def _h_get_ui_language(_body):
    """Return the daemon-side UI language persisted in
    ``$AZT_HOME/config.json::ui.language``.

    Why this needs to be a daemon endpoint: ``$AZT_HOME`` resolves
    to the calling process's *private* filesDir on Android (server
    APK has its own, each peer has its own). The language picker
    lives in the server APK's settings UI, which writes to the
    *server's* config.json. Peers reading their own config.json
    never see that value, so peer-side dialogs (bootstrap popups,
    in-particular) stay in English even when the user has picked
    French in the server UI. This endpoint exposes the canonical
    server-side preference so peers can mirror at startup."""
    import json
    import os
    from . import paths
    cfg_path = os.path.join(paths.azt_home(), 'config.json')
    try:
        with open(cfg_path) as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        return 200, {"ok": True, "language": ''}
    lang = ((cfg.get('ui') or {}).get('language', '') or '')
    return 200, {"ok": True, "language": str(lang)}


def _h_cawl_index(langcode, _body):
    """``GET /v1/projects/<lang>/cawl/index`` — serve the
    daemon-owned CAWL image-URL index for ``langcode``'s repo.

    Resolves the project's ``cawl_image_repo`` (per-project field,
    falling back to the daemon-global default for projects without
    an override), then returns the cached/fetched index dict for
    that repo. Two projects pointing at the same repo share a
    single cache directory under
    ``$AZT_HOME/cawl/<owner>/<repo>/index.json``, so the dedup
    happens transparently without the peer needing to know the
    repo slug.

    Why this lives on the daemon: peers used to hit GitHub
    directly on every project load and the unauthenticated 60/hr
    API rate limit got exhausted by even modest development /
    multi-peer use. The daemon caches once per device per TTL
    window (24h default), per repo, and a network failure falls
    back to the stale cache rather than returning empty so peers
    can resolve illustrations even when GitHub is unreachable.

    Returns an empty ``index`` dict when no repo is configured for
    this project — same shape peers got pre-migration from an
    empty resolver, so no new failure branch is required."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    repo = _cawl.resolve_image_repo(langcode)
    try:
        index = _cawl.get_index(repo)
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    # Filter to image-shaped entries before serializing. The
    # canonical CAWL repo includes README / LICENSE / .gitignore
    # blobs that every peer's parser discards anyway — emitting
    # them just inflates the response. The ~5500-entry full
    # index is ~1.5 MB serialized, which exceeds Android's
    # Binder ~1 MB per-transaction cap and silently drops the
    # response Bundle on the way back to the peer; filtering to
    # image extensions cuts it to ~1700 entries (~470 KB) and
    # the round-trip fits comfortably. File-route consumers
    # (peers using ``cawl_index`` 0.41.2+ on Android) read the
    # raw cache file via openFileDescriptor and self-filter, so
    # this only affects the JSON-RPC dispatch path. See
    # NOTES_TO_DAEMON.md "CAWL index response lost in transit"
    # 2026-05-13 for the diagnostic chain.
    if isinstance(index, dict):
        files = index.get('files') or []
        slim_files = [
            f for f in files
            if isinstance(f, dict)
            and isinstance(f.get('path'), str)
            and f['path'].lower().endswith(
                ('.png', '.jpg', '.jpeg'))
        ]
        index = dict(index)
        index['files'] = slim_files
    n_files = len((index or {}).get('files') or [])
    print(f'[cawl] served index for repo={repo!r} '
          f'langcode={langcode!r} files={n_files}',
          file=sys.stderr, flush=True)
    return 200, {"ok": True, "index": index, "image_repo": repo}


def _h_cawl_image_bytes(langcode, rel_path):
    """Return ``(status, content_type, data_bytes)`` for the cached
    CAWL image binary, fetching it lazily if not yet on disk.

    ``rel_path`` may be a flat filename or a nested path (CAWL
    repos commonly nest images under category subdirs:
    ``0001_body/foo.png``). The matcher
    (``_match_cawl_image_path``) URL-decodes each component
    before passing in, so this function sees the same on-disk
    form the index emitted.

    Not part of the JSON dispatch table because it returns binary
    bytes, not a JSON dict. The HTTP handler routes the path
    directly to this function (bypassing ``dispatch``) and emits
    via ``_send_bytes``. The ContentProvider transport gets the
    same bytes via ``openFile`` → ``_resolve_path`` returning the
    cached file's absolute path, which calls into this function
    indirectly through ``cawl.get_image_path``.

    Status codes:
        200 — bytes available (cache hit or successful fetch).
        404 — project unknown, rel_path rejected, OR fetch failed
              and no cached copy exists. Logged on stderr; the
              peer should fall through to its no-image rendering.
        500 — unexpected internal error.

    No 502 distinct from 404: peers don't distinguish "image not
    in repo" from "couldn't reach repo" — both end in "no
    illustration for this entry", and the daemon's stale-cache
    fallback already covers the recoverable case."""
    p = projects.get(langcode)
    if p is None:
        print(f'[cawl] image rejected: project_not_found '
              f'langcode={langcode!r}',
              file=sys.stderr, flush=True)
        return 404, 'application/json', \
            b'{"ok":false,"error":"project_not_found"}'
    _touch_project(langcode)
    repo = _cawl.resolve_image_repo(langcode)
    if not repo:
        print(f'[cawl] image rejected: no_image_repo_configured '
              f'langcode={langcode!r}',
              file=sys.stderr, flush=True)
        return 404, 'application/json', \
            b'{"ok":false,"error":"no_image_repo_configured"}'
    target, source = _cawl.get_image_path(repo, rel_path)
    if target is None:
        # ``get_image_path`` already logs ``[cawl] image fetch
        # failed`` when the network attempt fails. We log a
        # second line here so cases where the rel_path itself
        # was rejected (path-traversal etc.) — which produce no
        # fetch-attempt log — are still visible.
        print(f'[cawl] image unavailable: repo={repo!r} '
              f'path={rel_path!r}',
              file=sys.stderr, flush=True)
        return 404, 'application/json', \
            b'{"ok":false,"error":"image_unavailable"}'
    # On-demand fetches during an active prefetch contribute to
    # the source counters too (no-op outside any prefetch window
    # via the silent ``state is None`` guard in
    # ``_bump_source_counter``). 0.50.30: bumping moved out of
    # ``get_image_path`` so the prefetch worker can guarantee
    # "completed without source" is impossible; this preserves
    # the pre-refactor behaviour for on-demand callers.
    if source:
        _cawl._bump_source_counter(repo, source)
    try:
        with open(target, 'rb') as f:
            data = f.read()
    except OSError as ex:
        return 500, 'application/json', \
            json.dumps({"ok": False,
                        "error": f'cache_read: {ex}'}).encode()
    # Success log — keeps ops visibility into the read path
    # (which previously had none). ``rel_path`` (not ``target``)
    # so the line is comparable across cache dir relocations.
    print(f'[cawl] served image repo={repo!r} path={rel_path!r} '
          f'bytes={len(data)}',
          file=sys.stderr, flush=True)
    return 200, _content_type_for(rel_path), data


_CONTENT_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.svg': 'image/svg+xml',
}


def _content_type_for(basename):
    ext = os.path.splitext(basename)[1].lower()
    return _CONTENT_TYPES.get(ext, 'application/octet-stream')


def _h_set_cawl_image_repo(langcode, body):
    """``POST /v1/projects/<lang>/cawl_image_repo`` — persist the
    per-project image_repo override. Body: ``{cawl_image_repo:
    'owner/repo'}``. Empty string explicitly clears the override,
    falling the project back to the daemon-global default."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    repo = body.get('cawl_image_repo')
    if repo is None or not isinstance(repo, str):
        return 400, {"ok": False, "error": "missing_cawl_image_repo"}
    projects.set_cawl_image_repo(langcode, repo.strip())
    return 200, {"ok": True, "project": _project_for_api(
        projects.get(langcode))}


def _h_set_repo_slug(langcode, body):
    """``POST /v1/projects/<lang>/repo_slug`` — persist the
    per-project GitHub-repo-name override for the publish path.
    Body: ``{repo_slug: 'my-vanity-name'}``. Empty string
    explicitly clears the override; callers should treat unset
    / empty as equal to ``langcode``.

    Used by peers (recorder ``CollabScreen.do_publish``) to
    persist a textbox value the user typed before publishing.
    The pre-1.41.3 recorder kept this as a suite-wide
    ``peer_pref`` scalar; per the no-daemon-owned-caches rule
    that was wrong (per-project data in a global pref, and
    project-identity data on the peer side at all). This
    endpoint is the canonical home."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    slug = body.get('repo_slug')
    if slug is None or not isinstance(slug, str):
        return 400, {"ok": False, "error": "missing_repo_slug"}
    projects.set_repo_slug(langcode, slug.strip())
    return 200, {"ok": True, "project": _project_for_api(
        projects.get(langcode))}


def _h_github_install_url(_body):
    """Return the configured GitHub App install URL (canonical source is
    azt_collabd.config). Peers use this so they don't have to import
    daemon internals just to send the user to install the App."""
    from . import config as _config
    try:
        url = _config.install_url()
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    return 200, {"ok": True, "url": url}


def _h_github_client_id(_body):
    """Return the configured GitHub App client_id."""
    from . import config as _config
    try:
        client_id = _config.get().get('client_id', '')
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    return 200, {"ok": True, "client_id": client_id}


# ── GitHub App device flow (server-driven) ────────────────────────────
#
# Peers used to import ``device_flow_start`` / ``device_flow_poll`` from
# ``azt_collabd.auth`` and run the whole flow in their own process. Now
# the server owns the flow: the peer kicks it off, polls a job-style
# status endpoint, and the server (a) hands back the user_code right
# away, then (b) polls GitHub itself, and on success writes tokens
# into the credentials store. The peer never touches a token.

_device_flow_jobs = {}   # job_id -> dict
_device_flow_lock = threading.Lock()


def _device_flow_finalize(job_id, token_data):
    """Save tokens to the store, look up the username, check whether
    the GitHub App is installed, and mark the job DONE."""
    from . import auth as _auth
    access_token = token_data.get('access_token', '')
    username = ''
    app_installed = False
    try:
        username = _auth.get_github_username(access_token) or ''
    except Exception:
        username = ''
    try:
        store.set_github_tokens(
            access_token=access_token,
            refresh_token=token_data.get('refresh_token', ''),
            username=username,
        )
    except Exception:
        pass
    try:
        app_installed = bool(
            _auth.check_app_installed(access_token).get('installed', False))
        store.set_github_app_installed(app_installed)
    except Exception:
        app_installed = False
    with _device_flow_lock:
        job = _device_flow_jobs.get(job_id)
        if job is not None:
            job.update({
                'state': 'DONE',
                'username': username,
                'app_installed': app_installed,
            })


def _device_flow_worker(job_id, device_code, interval, expires_in):
    from . import auth as _auth
    try:
        token_data = _auth.device_flow_poll(
            device_code, interval=interval, expires_in=expires_in)
    except _auth.AuthError as ex:
        with _device_flow_lock:
            job = _device_flow_jobs.get(job_id)
            if job is not None:
                job.update({'state': 'FAILED',
                            'error': ex.status.code,
                            'error_params': ex.status.params})
        return
    except Exception as ex:
        with _device_flow_lock:
            job = _device_flow_jobs.get(job_id)
            if job is not None:
                job.update({'state': 'FAILED', 'error': str(ex)})
        return
    _device_flow_finalize(job_id, token_data)


def _h_github_device_flow_start(_body):
    """Begin device flow. Returns the user_code to display, the
    verification URI to open, an expiry, and a job_id the peer should
    poll."""
    from . import auth as _auth
    try:
        resp = _auth.device_flow_start()
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    job_id = secrets.token_hex(8)
    interval = int(resp.get('interval', 5))
    expires_in = int(resp.get('expires_in', 900))
    with _device_flow_lock:
        _device_flow_jobs[job_id] = {
            'state': 'POLLING',
            'username': '',
            'app_installed': False,
            'error': '',
            'error_params': {},
        }
    t = threading.Thread(
        target=_device_flow_worker,
        args=(job_id, resp.get('device_code', ''), interval, expires_in),
        daemon=True,
        name=f'gh-device-flow-{job_id[:8]}',
    )
    t.start()
    return 200, {
        "ok": True,
        "job_id": job_id,
        "user_code": resp.get('user_code', ''),
        "verification_uri": resp.get(
            'verification_uri', 'https://github.com/login/device'),
        "expires_in": expires_in,
        "interval": interval,
    }


def _h_github_device_flow_status(job_id, _body):
    with _device_flow_lock:
        job = _device_flow_jobs.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": "job_not_found"}
        return 200, {"ok": True, **job}


def _h_set_host(body):
    host = body.get('host', '')
    if host not in ('github', 'gitlab'):
        return 400, {"ok": False, "error": "invalid_host"}
    store.set_collab_host(host)
    return 200, {"ok": True}


def _h_set_github_tokens(body):
    access_token = body.get('access_token', '')
    if not access_token:
        return 400, {"ok": False, "error": "missing_access_token"}
    store.set_github_tokens(
        access_token=access_token,
        refresh_token=body.get('refresh_token', ''),
        username=body.get('username', ''),
        token_time=body.get('token_time'),
    )
    # A-nudge (0.52.24): fresh credentials can fix any access-class
    # blocker (auth expiry, or a token that now sees the repo). Clear the
    # WAN backoff for every access-blocked project so they retry now.
    try:
        scheduler.nudge_access_blocked_projects()
    except Exception:
        pass
    return 200, {"ok": True}


def _h_set_github_app_installed(body):
    store.set_github_app_installed(bool(body.get('installed', False)))
    return 200, {"ok": True}


def _h_set_gitlab(body):
    username = body.get('username', '')
    token = body.get('token', '')
    if not username or not token:
        return 400, {"ok": False, "error": "missing_username_or_token"}
    store.set_gitlab(username, token)
    return 200, {"ok": True}


def _h_test_github(body):
    """Validate the stored GitHub access token by hitting
    ``api.github.com/user``. Mirrors ``_h_test_gitlab`` so the UI's
    per-host Test buttons share a shape.

    Refresh path: the token may be near expiry. We pull through
    ``store.get_valid_github_token`` which proactively refreshes when
    the access token is older than 7 hours, so a successful test
    after a refresh persists the new token automatically (set on
    refresh by ``store.set_github_tokens``, which also clears
    ``confirmed`` — we re-set it on the success branch below).

    On a successful test we also persist the freshly-probed
    ``app_installed`` flag (the GitHub probe is on the same token,
    one extra HTTP round-trip already done inside
    ``test_github_credentials``) so the connect screen's "Install
    GitHub App" CTA shows up only when warranted."""
    from . import auth as _auth
    username, token = store.get_valid_github_token()
    if not token:
        store.set_github_confirmed(False)
        return 200, {"ok": True, "valid": False,
                     "server_username": "",
                     "app_installed": False,
                     "error": "missing_token"}
    info = _auth.test_github_credentials(token)
    import sys
    print(f'[_h_test_github] test_github_credentials returned: '
          f'valid={info.get("valid")} '
          f'app_installed={info.get("app_installed")} '
          f'app_suspended={info.get("app_suspended")} '
          f'installation_id={info.get("installation_id")}',
          file=sys.stderr, flush=True)
    if info.get('valid'):
        # Refresh username if the user renamed on GitHub since last
        # connect; harmless on the typical "name unchanged" path.
        server_user = info.get('server_username', '') or username
        if server_user and server_user != username:
            store.set_github_tokens(
                access_token=token,
                username=server_user,
            )
        store.set_github_app_installed(bool(info.get('app_installed')))
        store.set_github_confirmed(True)
        print(f'[_h_test_github] saved: app_installed='
              f'{bool(info.get("app_installed"))!r} confirmed=True',
              file=sys.stderr, flush=True)
    else:
        store.set_github_confirmed(False)
    return 200, {"ok": True, **info}


def _h_test_gitlab(body):
    """Validate GitLab credentials by hitting ``/api/v4/user``. If
    ``username`` / ``token`` are absent in the body, fall back to the
    stored values so callers can re-test what's already saved without
    having the user retype the PAT.

    On a successful test the credentials are persisted and the
    ``confirmed`` flag is set, so the UI's single Test button covers
    both save and verify in one user gesture."""
    username = body.get('username', '') or ''
    token = body.get('token', '') or ''
    if not username or not token:
        stored_user, stored_token = store.get_gitlab()
        username = username or stored_user
        token = token or stored_token
    from . import auth as _auth
    info = _auth.test_gitlab_credentials(username, token)
    if info.get('valid'):
        store.set_gitlab(username, token)
        store.set_gitlab_confirmed(True)
    return 200, {"ok": True, **info}


def _h_migrate_from_prefs(body):
    prefs_path = body.get('prefs_path', '')
    if not prefs_path:
        return 400, {"ok": False, "error": "missing_prefs_path"}
    summary = store.migrate_from_prefs(prefs_path)
    return 200, {"ok": True, **summary}


def _h_list_projects(_body):
    items = [_project_for_api(p) for p in projects.list_all()]
    # Diagnostic: confirms the path the dispatcher reads from and
    # the resulting count. If the count is 0 here right after a
    # successful clone-register elsewhere in the same logcat, we
    # have an AZT_HOME mismatch between the two call sites
    # (different process? jnius Activity null at one point?).
    print(f'[server] list_projects: {len(items)} entries from '
          f'{projects.projects_path()!r} → '
          f'{[i["langcode"] for i in items]!r}',
          file=sys.stderr, flush=True)
    # Empty registry → also report what's on disk under
    # ``$AZT_HOME/projects/`` so we can distinguish "registry wiped
    # but the working trees survived" (recoverable: scan + register)
    # from "the entire filesDir is gone" (e.g. server APK clean-
    # install). Only run when the registry says zero — the directory
    # listing is cheap but pointless when we already have an answer.
    if not items:
        try:
            projects_dir = os.path.join(azt_home(), 'projects')
            if os.path.isdir(projects_dir):
                disk_dirs = sorted(os.listdir(projects_dir))
            else:
                disk_dirs = None
            print(f'[server] list_projects: registry empty; '
                  f'projects_dir={projects_dir!r} '
                  f'on_disk={disk_dirs!r}',
                  file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[server] list_projects: disk-scan failed: {ex}',
                  file=sys.stderr, flush=True)
    return 200, {"ok": True, "projects": items}


def _annotate_with_auth_health(res):
    """Append ``S.AUTH_REFRESH_STALE`` to *res* when the persisted
    GitHub refresh state says the refresh path is broken.

    Called on every sync result so peers running the user-initiated
    sync path (per the auto/user contract in
    ``azt_collab_client/CLAUDE.md``) can surface a deadline-aware
    toast — the user has ~1h between the failed refresh attempt
    and the access-token cliff, and the only useful action is
    re-running the device flow at GitHub Connect.

    Idempotent: re-syncs while the state is broken keep emitting
    the status; ``set_github_tokens`` after a successful device
    flow clears ``refresh_broken`` and the status stops appearing.

    Auto-sync peers MUST silence this code (see the same contract);
    nothing breaks if they don't, but the user gets a popup at an
    inopportune moment."""
    state = store.github_refresh_state()
    if not state['broken']:
        return
    res.add(S.AUTH_REFRESH_STALE,
            expires_at=state['expires_at'],
            error=state['error'])


def _touch_project(langcode):
    """Stamp *langcode* as the device's most-recently-touched project,
    and ask CAWL to auto-warm its image cache.

    Called from every langcode-bound endpoint so peers don't have to
    remember to write ``set_last_project``; opening a project to read
    it (``_h_get_project``), checking its sync state
    (``_h_project_status``), or syncing it (``_h_project_sync``)
    naturally marks it recent. Single source of truth across peers
    and platforms — fixes the Android-sandbox split where the
    recorder's $AZT_HOME and the daemon's $AZT_HOME are different
    files.

    Also fires ``cawl.auto_prefetch(repo)`` so the daemon owns the
    "warm the image cache" decision. The peer no longer has to
    POST ``cawl/prefetch`` (though the endpoint still works for
    peers that explicitly want a different working set than the
    full index). ``auto_prefetch`` is throttled per repo so the
    1 Hz cache-status poll doesn't re-trigger every second.

    Short-circuit when the value is already current: hot endpoints
    (cawl_image, get_audio, project_status) fire this 10–15× per
    UI interaction. Rewriting the same value to ``config.json`` on
    every call burns flash wear and floods the daemon log mirror
    (visibly slow phone, see 0.43.8). ``store.set_last_langcode``
    also short-circuits internally; both layers cache so that
    neither path lands a redundant write."""
    if not langcode:
        return
    try:
        already_current = (store.get_last_langcode() == langcode)
    except Exception:
        already_current = False
    if not already_current:
        try:
            store.set_last_langcode(langcode)
            print(f'[recent] _touch_project({langcode!r}) → '
                  f'{store._config_path()!r}',
                  file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[recent] _touch_project({langcode!r}) failed: {ex}',
                  file=sys.stderr, flush=True)
    try:
        repo = _cawl.resolve_image_repo(langcode)
        if repo:
            _cawl.auto_prefetch(repo)
    except Exception as ex:
        print(f'[recent] auto_prefetch({langcode!r}) failed: {ex}',
              file=sys.stderr, flush=True)


_last_project_logged = ['<unset>']


def _h_get_last_project(_body):
    # No per-call success log — peers poll this at high frequency
    # (the daemon UI's cache-status indicator reads
    # ``last_project()`` every second to know which project to
    # query), and a per-call log floods logcat. The setter
    # (``_h_set_last_project``) still logs because it's a real
    # state change, not a poll.
    #
    # *Transition* logging IS on, because the previously-silent
    # case "GET returned empty when we expected a langcode" is
    # exactly the diagnostic shape that explained a peer
    # ``App.stop()`` during project-switch (2026-05-18 field
    # report): the peer's reload path interpreted empty as "no
    # project loaded" and shut down. ``_last_project_logged``
    # holds the previous response so we emit one line per actual
    # change, not per poll. Sentinel ``'<unset>'`` differentiates
    # the first call this process ever served.
    val = store.get_last_langcode()
    if val != _last_project_logged[0]:
        print(f'[recent] GET /v1/recent/last_project transition '
              f'{_last_project_logged[0]!r} → {val!r}',
              file=sys.stderr, flush=True)
        _last_project_logged[0] = val
    return 200, {"ok": True, "langcode": val}


def _h_set_last_project(body):
    """Explicit override. Most peers shouldn't need to call this —
    every langcode-bound endpoint already stamps via ``_touch_project``
    — but the wrapper exists so peers that want to pin a different
    project than the one they just touched have an affordance.

    Empty ``langcode`` is **refused** (no-op + stderr warning). The
    daemon-side invariant is that ``recent.last_langcode`` never
    lands as a stored ``''`` on disk; the only legitimate empty state
    is "key absent" (first boot). Picker-cancel is a peer-side
    gesture that issues no RPC at all — the ``on_resume`` comparison
    naturally no-ops because the daemon's ``last_langcode`` is
    unchanged. See ``azt_collab_client/CLIENT_INTEGRATION.md`` § 14a."""
    val = (body.get('langcode', '') or '').strip()
    if not val:
        print('[recent] POST /v1/recent/last_project refused empty '
              '(picker-cancel must not POST; this is a peer bug)',
              file=sys.stderr, flush=True)
        return 400, {"ok": False, "error": "empty_langcode"}
    store.set_last_langcode(val)
    print(f'[recent] POST /v1/recent/last_project ← {val!r} '
          f'(to {store._config_path()!r})',
          file=sys.stderr, flush=True)
    return 200, {"ok": True}


def _h_get_project(langcode, _body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_derive_langcode(body):
    """Compute a langcode for a working dir / LIFT path. Pure function;
    just exposes ``projects.derive_langcode`` to peers so they don't
    have to import daemon internals to register a project."""
    working_dir = body.get('working_dir', '')
    lift_path = body.get('lift_path', '')
    if not working_dir:
        return 400, {"ok": False, "error": "missing_working_dir"}
    return 200, {"ok": True,
                 "langcode": projects.derive_langcode(
                     working_dir, lift_path)}


def _h_init_project(body):
    """Initialize a git repo at *working_dir*, set the remote, and push.
    Server uses store-resident credentials and contributor — peers do
    not pass tokens or the commit author name."""
    from .repo import init_repo as _init_repo
    working_dir = body.get('working_dir', '')
    remote_url = body.get('remote_url', '')
    branch = body.get('branch', 'main')
    # 0.40.0: contributor comes from store only. Peer-passed
    # ``body['contributor']`` is ignored (peer-side mirror of the
    # name is the anti-pattern this contract closes; see
    # NOTES_TO_DAEMON.md "sole authoritative source"). Empty stored
    # value refuses with CONTRIBUTOR_UNSET so the user sets a real
    # name before any commit lands.
    contributor = store.get_contributor()
    if not contributor:
        print(f'[publish] init_project refused: CONTRIBUTOR_UNSET '
              f'(working_dir={working_dir!r} remote={remote_url!r})',
              file=sys.stderr, flush=True)
        return 200, {"ok": True,
                     "result": Result().add(
                         S.CONTRIBUTOR_UNSET).to_dict()}
    if not working_dir or not remote_url:
        print(f'[publish] init_project bad request: '
              f'working_dir={working_dir!r} remote={remote_url!r}',
              file=sys.stderr, flush=True)
        return 400, {"ok": False,
                     "error": "missing_working_dir_or_remote_url"}
    git_user, token = store.get_sync_credentials(remote_url)
    if not token:
        # AUTH_REQUIRED pre-check: no stored token for this remote's
        # host. Without this log line a tester sharing daemon-log
        # sees absolutely nothing about the publish click — same
        # blind spot the 0.50.52 ``_ensure_remote_repo`` fix
        # closed at the github-API layer, but one level higher.
        print(f'[publish] init_project AUTH_REQUIRED: no stored '
              f'token for remote={remote_url!r} '
              f'(git_user={git_user!r})',
              file=sys.stderr, flush=True)
        return 200, {"ok": True,
                     "result": Result().add(S.AUTH_REQUIRED).to_dict()}
    try:
        result = _init_repo(working_dir, remote_url, git_user, token,
                            branch, contributor)
    except Exception as ex:
        print(f'[publish] init_project raised: {type(ex).__name__}: {ex}',
              file=sys.stderr, flush=True)
        return 500, {"ok": False, "error": str(ex)}
    # On success update the registry to reflect the new state:
    #
    #   * ``_touch_project`` — publish is active use; mark recent.
    #   * ``set_remote_url``  — _init_repo writes the local git config
    #     but the registry is a separate datastore; without this
    #     back-write the publish row stays visible after a successful
    #     publish.
    #   * ``set_last_sync`` on PUSHED — without it the recorder's
    #     "not backed up" warning persists forever after a successful
    #     publish, because the indicator reads ``last_sync == 0`` as
    #     "never synced." Sister handlers in ``scheduler._run_commit`` /
    #     ``_h_project_sync`` already stamp this; init_project was the
    #     odd one out.
    #   * ``set_last_commit`` on COMMITTED / COMMITTED_AND_PUSHED —
    #     same idea for the commit timestamp peers display alongside.
    codes = result.codes()
    # Publish-rollback signal: if ``_ensure_remote_repo`` failed, the
    # daemon side has already stripped ``.git/config``'s
    # ``[remote "origin"]`` section. Mirror that in the registry so
    # the picker's ``_refresh_publish_row`` gate (which prefers
    # ``project_status.remote_url`` but falls back to
    # ``Project.remote_url``) sees an empty URL on both sides and
    # shows the Publish button again for a retry. See 0.50.52.
    remote_create_failed = 'REMOTE_CREATE_FAILED' in codes
    published_langcode = ''
    try:
        for p in projects.list_all():
            if os.path.abspath(p.working_dir) == os.path.abspath(working_dir):
                _touch_project(p.langcode)
                published_langcode = p.langcode
                if remote_create_failed:
                    if p.remote_url:
                        projects.set_remote_url(p.langcode, '')
                elif remote_url and p.remote_url != remote_url:
                    projects.set_remote_url(p.langcode, remote_url)
                if ('PUSHED' in codes
                        or 'COMMITTED_AND_PUSHED' in codes):
                    projects.set_last_sync(p.langcode)
                if ('COMMITTED' in codes
                        or 'COMMITTED_AND_PUSHED' in codes
                        or 'COMMITTED_LOCAL' in codes
                        or 'COMMITTED_NO_REMOTE' in codes):
                    projects.set_last_commit(p.langcode)
                break
    except Exception:
        pass
    # Fan out the (possibly newly-set) remote_url to every paired peer
    # who has this project on their allow-list, so peer-side
    # ``_do_publish`` adopts the URL instead of inventing a duplicate
    # github repo under their own namespace. Best-effort; per-peer
    # failures don't block the response. The share-offer wire format
    # already carried ``repo_url`` (since 0.45.0), so older peers see
    # this as a familiar ``LAN_SHARE_OFFER`` arrival — they'll stash
    # the URL as a pending decision the user can accept. New peers
    # (0.50.27+) dispatch on local state per ``_handle_share_offer``.
    #
    # Run in a background daemon thread so unreachable peers (each
    # with a 15 s urllib3 timeout in ``_https_post_to_peer``) don't
    # block the publish RPC's response.
    #
    # Gate on ``PUSHED`` (0.50.52): pre-0.50.52 this fired whenever
    # the project existed and the peer supplied a non-empty
    # ``remote_url``, regardless of whether ``_init_repo`` actually
    # got data onto the remote. The result was peers adopting URLs
    # for repos that didn't exist (``REMOTE_CREATE_FAILED``) or
    # were empty (``PUSH_FAILED``), then hitting
    # ``NotGitRepository()`` on every drain forever — the same
    # stuck state the user reported. ``PUSHED`` is the minimum
    # signal that the github repo is real and carries the project's
    # data; peers adopting that URL will get a working clone.
    publish_landed = ('PUSHED' in codes
                      or 'COMMITTED_AND_PUSHED' in codes)
    if published_langcode and remote_url and publish_landed:
        _spawn_publish_fanout(published_langcode, remote_url)
    return 200, {"ok": True, "result": result.to_dict()}


def _spawn_publish_fanout(langcode, url):
    """Background fan-out of a successful publish to every paired
    peer that has *langcode* on its share allow-list. Sends a
    ``share_offer`` carrying *url* so peers can adopt the github
    URL instead of inventing a duplicate repo under their own
    namespace.

    Called from ``_h_init_project`` on PUSHED, and from
    ``reconcile_publish_state_on_startup``'s auto-retry success
    path. Gated callers: only invoke on a publish that
    definitively landed on the remote.

    Best-effort: per-peer failures log and continue. Runs in a
    daemon thread so unreachable peers (15 s urllib3 timeout per
    peer) don't block the caller."""
    def _worker(langcode_, url_):
        try:
            from . import peers as _peers
            from . import lan_push as _lan_push
            proj_after = projects.get(langcode_)
            vernlang_ = (proj_after.effective_vernlang()
                         if proj_after is not None
                         else langcode_)
            for peer_id in _peers.peers_sharing_project(langcode_):
                try:
                    _lan_push.send_share_offer(
                        peer_id, langcode_, url_,
                        vernlang=vernlang_)
                except Exception as ex:
                    print(f'[publish-fanout] send_share_offer '
                          f'peer={peer_id[:8]!r} '
                          f'lang={langcode_!r}: {ex!r}',
                          file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[publish-fanout] worker failure '
                  f'lang={langcode_!r}: {ex!r}',
                  file=sys.stderr, flush=True)
    threading.Thread(
        target=_worker, args=(langcode, url),
        daemon=True,
        name=f'publish-fanout-{langcode}').start()


# Clone is run as a job so the peer can poll progress lines without the
# HTTP request hanging for the whole download.
_clone_jobs = {}
_clone_lock = threading.Lock()


_AUTH_ERROR_KEYWORDS = (
    '401', '403', '404',
    'unauthorized', 'forbidden', 'not found',
    'authentication', 'credential',
)


def _clone_error_looks_like_auth(result):
    """True if any CLONE_FAILED status carries an error message that
    smells like 401/403/404 / auth-related. Used to decide whether to
    retry anonymously and (after final failure) whether to surface
    CLONE_AUTH_REQUIRED to the UI."""
    for st in result.statuses:
        if st.code != 'CLONE_FAILED':
            continue
        msg = (st.params.get('error', '') or '').lower()
        if any(k in msg for k in _AUTH_ERROR_KEYWORDS):
            return True
    return False


def _clone_worker(job_id, remote_url, dest_dir, username, token,
                  retry_anonymous_on_auth_fail=True,
                  override_langcode='',
                  override_vernlang=''):
    from .repo import clone_repo as _clone_repo

    def _on_progress(line):
        with _clone_lock:
            job = _clone_jobs.get(job_id)
            if job is not None:
                job['progress'].append(line)
                # Cap history so a chatty repo doesn't grow unbounded.
                if len(job['progress']) > 500:
                    del job['progress'][:-500]

    try:
        had_token = bool(token)
        lift_path, result = _clone_repo(
            remote_url, dest_dir, username, token, on_progress=_on_progress)
        if not lift_path and retry_anonymous_on_auth_fail \
                and _clone_error_looks_like_auth(result):
            lift_path, result = _clone_repo(
                remote_url, dest_dir, '', '',
                on_progress=_on_progress)

        # Final-failure auth diagnosis: if both attempts failed with
        # auth-shaped errors, OR we never had creds for the URL's host,
        # tell the UI so it can prompt the user to authenticate.
        if not lift_path and result.has(S.CLONE_FAILED):
            host = store.host_for_url(remote_url) or store.get_collab_host()
            if (not had_token) or _clone_error_looks_like_auth(result):
                result.add(S.CLONE_AUTH_REQUIRED, host=host)

        # Auto-register on success so later list_projects /
        # sync_project / project_status calls find this clone in the
        # registry. Best-effort: a registry write failure shouldn't
        # mark the whole clone as failed (the working tree is on
        # disk; a future explicit register_project call recovers).
        # Capture the canonical langcode for the job response so
        # peers don't have to parse it back out of the LIFT URI
        # (azt-viewer 0.5.1 filed the TODO; the URI parse worked
        # only by coincidence — when working dir basename == langcode).
        # Picker collects an explicit langcode from the user before
        # kicking the clone (confirm-langcode popup); pass it through
        # via ``override_langcode`` so the project is keyed on the
        # user's choice from the moment ``projects.json`` first sees
        # it. Empty override falls back to the filesystem-derived
        # default — matches the desktop / scripted-call behaviour.
        job_langcode = ''
        if lift_path:
            try:
                job_langcode = (
                    (override_langcode or '').strip()
                    or projects.derive_langcode(dest_dir, lift_path))
                projects.register(job_langcode, dest_dir,
                                  lift_path=lift_path,
                                  remote_url=remote_url)
                # Stamp vernlang separately if it differs from the
                # project-key langcode. Pre-0.45.0 the two were
                # conflated; new clones can carry the user's
                # confirmed vernlang from the clone-url popup so
                # LIFT writers tag entries correctly even when the
                # project name doesn't match the linguistic code
                # (``MyEnglishProject`` analyzing ``en``).
                if (override_vernlang
                        and override_vernlang != job_langcode):
                    try:
                        projects.set_vernlang(job_langcode,
                                              override_vernlang)
                    except Exception as ex:
                        print(f'[server] set_vernlang failed: '
                              f'{ex!r}', file=sys.stderr, flush=True)
                _touch_project(job_langcode)
                # Confirm the registry write hit disk (the user
                # reported previously-cloned projects not showing
                # up on next open — most likely the auto-register
                # is silently failing). projects_path() should be
                # ``$AZT_HOME/projects.json`` — if it's missing
                # after this print, persistence is the issue.
                print(f'[server] clone registered langcode='
                      f'{job_langcode!r} → {projects.projects_path()!r}',
                      file=sys.stderr, flush=True)
            except Exception as ex:
                print(f'[server] clone auto-register failed: '
                      f'{type(ex).__name__}: {ex}',
                      file=sys.stderr, flush=True)

        with _clone_lock:
            job = _clone_jobs.get(job_id)
            if job is not None:
                job.update({
                    'state': 'DONE',
                    'lift_path': lift_path or '',
                    'langcode': job_langcode,
                    'result': result.to_dict(),
                })
    except Exception as ex:
        with _clone_lock:
            job = _clone_jobs.get(job_id)
            if job is not None:
                job.update({'state': 'FAILED', 'error': str(ex)})


def _h_create_project_from_template(body):
    template_url = (body.get('template_url') or '').strip()
    vernlang = (body.get('vernlang') or '').strip()
    dest_dir = (body.get('dest_dir') or '').strip()
    if not vernlang or not dest_dir:
        return 400, {"ok": False,
                     "error": "missing_vernlang_or_dest_dir"}
    if not template_url:
        template_url = _config.default_template_url()
    try:
        p = projects.create_from_template(
            template_url, vernlang, dest_dir)
        _touch_project(p.langcode)
    except (ValueError, RuntimeError) as ex:
        return 400, {"ok": False, "error": str(ex)}
    except Exception as ex:
        # Log a full traceback to stderr/logcat so the failure mode is
        # diagnosable without a debugger. The Android log tag is
        # "python" — find it with
        # ``adb logcat | grep -E '\[server\] from_template'``.
        import traceback
        tb = traceback.format_exc()
        print(f'[server] from_template failed: {type(ex).__name__}: {ex}\n'
              f'  template_url={template_url!r}\n'
              f'  vernlang={vernlang!r}\n'
              f'  dest_dir={dest_dir!r}\n'
              f'{tb}',
              file=sys.stderr, flush=True)
        return 500, {"ok": False,
                     "error": f'{type(ex).__name__}: {ex}',
                     "traceback": tb}
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_clone_project(body):
    remote_url = body.get('remote_url', '')
    dest_dir = body.get('dest_dir', '')
    override_langcode = (body.get('langcode') or '').strip()
    override_vernlang = (body.get('vernlang') or '').strip()
    if not remote_url or not dest_dir:
        return 400, {"ok": False,
                     "error": "missing_remote_url_or_dest_dir"}
    git_user, token = store.get_sync_credentials(remote_url)
    job_id = secrets.token_hex(8)
    with _clone_lock:
        _clone_jobs[job_id] = {
            'state': 'CLONING',
            'progress': [],
            'lift_path': '',
            'result': None,
            'error': '',
        }
    t = threading.Thread(
        target=_clone_worker,
        args=(job_id, remote_url, dest_dir, git_user, token),
        kwargs={'override_langcode': override_langcode,
                'override_vernlang': override_vernlang},
        daemon=True,
        name=f'clone-{job_id[:8]}',
    )
    t.start()
    return 200, {"ok": True, "job_id": job_id}


def _h_clone_status(job_id, body):
    """Return the current state plus *new* progress lines since
    ``last_index`` (caller-tracked). Defaults to all-so-far on first call."""
    last_index = 0
    try:
        last_index = int(body.get('last_index', 0)) if body else 0
    except (TypeError, ValueError):
        last_index = 0
    with _clone_lock:
        job = _clone_jobs.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": "job_not_found"}
        progress = job['progress']
        new_lines = progress[last_index:] if last_index < len(progress) \
            else []
        # Convert lift_path to a content URI on Android so peers can
        # feed it directly to LiftHandle (same logic as
        # _project_for_api). Clone-job state stores filesystem paths
        # because the daemon needs them for dulwich; the API boundary
        # is where we adapt to peer-visibility rules.
        lift_path = job.get('lift_path', '')
        langcode = job.get('langcode', '')
        if (lift_path and langcode and _on_android()
                and not lift_path.startswith('content://')):
            basename = os.path.basename(lift_path)
            lift_path = (
                f'content://{_AZTCOLLAB_AUTHORITY}/{langcode}/{basename}')
        return 200, {
            "ok": True,
            "state": job.get('state', 'CLONING'),
            "progress": new_lines,
            "next_index": len(progress),
            "lift_path": lift_path,
            "langcode": langcode,
            "result": job.get('result'),
            "error": job.get('error', ''),
        }


def _h_rename_project(langcode, body):
    """Rename a project's langcode key in projects.json. Used by the
    picker's confirm-langcode popup when the user overrides the
    auto-derived value for a clone / open-file project."""
    new_langcode = (body.get('new_langcode') or '').strip()
    if not new_langcode:
        return 400, {"ok": False, "error": "missing_new_langcode"}
    try:
        p = projects.rename(langcode, new_langcode)
    except ValueError as ex:
        return 400, {"ok": False, "error": str(ex)}
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    # The just-renamed project becomes recent — both because the user
    # is actively interacting with it and because if it *was* the
    # recent one, last_project still points at the old langcode and
    # peers would silently resolve nothing.
    _touch_project(new_langcode)
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_register_project(body):
    langcode = body.get('langcode', '')
    working_dir = body.get('working_dir', '')
    lift_path = body.get('lift_path', '')
    remote_url = body.get('remote_url', '')
    if not langcode or not working_dir:
        return 400, {"ok": False,
                     "error": "missing_langcode_or_working_dir"}
    working_dir = os.path.abspath(working_dir)
    if lift_path:
        lift_path = os.path.abspath(lift_path)
    if not remote_url:
        remote_url = projects.derive_remote_url(working_dir)
    try:
        p = projects.register(langcode, working_dir, lift_path, remote_url)
    except projects.WorkingDirAlreadyRegistered as ex:
        return 409, {"ok": False,
                     "error": "working_dir_already_registered",
                     "existing_langcode": ex.existing_langcode}
    # Desktop-adopt hardening: whole-tree staging (``_stage_all`` is
    # ``add -A``) would otherwise commit azt's emailed-backup
    # variants, reports, and PDFs. Idempotent; harmless on recorder
    # projects (they never produce these files).
    added = repo_mod.ensure_ignore_patterns(working_dir)
    if added:
        print(f'[register] {langcode!r}: appended '
              f'{len(added)} azt ignore pattern(s) to .gitignore',
              file=sys.stderr, flush=True)
    _touch_project(langcode)
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_project_sync(langcode, body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    # 0.43.0: the Sync button is the user-gestured "bump push" —
    # if work_offline is on, refuse with a typed status the peer
    # routes to the settings screen. Auto-sync paths (which now
    # go through commit_project, not this endpoint) never see
    # this code because they don't push at all.
    from . import settings as _settings
    if _settings.work_offline():
        print(f'[sync-rpc] {langcode!r} → WORK_OFFLINE_ENABLED',
              file=sys.stderr, flush=True)
        res = Result().add(S.WORK_OFFLINE_ENABLED)
        return 200, {"ok": True, "result": res.to_dict()}
    # 0.40.0: contributor from store, not body. See _h_init_project.
    contributor = store.get_contributor()
    if not contributor:
        print(f'[sync-rpc] {langcode!r} → CONTRIBUTOR_UNSET',
              file=sys.stderr, flush=True)
        res = Result().add(S.CONTRIBUTOR_UNSET)
        return 200, {"ok": True, "result": res.to_dict()}
    print(f'[sync-rpc] {langcode!r} contributor={contributor!r} '
          f'remote_url={p.remote_url!r}',
          file=sys.stderr, flush=True)
    git_user, token = store.get_sync_credentials(p.remote_url)
    if not token:
        print(f'[sync-rpc] {langcode!r} → AUTH_REQUIRED',
              file=sys.stderr, flush=True)
        res = Result().add(S.AUTH_REQUIRED)
        return 200, {"ok": True, "result": res.to_dict()}
    try:
        # 0.52.21: user-gestured Sync can be a long fetch+merge+push
        # on a diverged history. Hold the in-flight guard so the
        # Android idle-stop loop doesn't kill the process mid-sync if
        # the user closes the app right after tapping Sync (same
        # killer as the auto-drain path — see sync_flight.py).
        from . import sync_flight
        with sync_flight.guard():
            res = _sync_repo(p.working_dir, git_user, token, contributor)
    except Exception as ex:
        print(f'[sync-rpc] {langcode!r} raised: {ex}',
              file=sys.stderr, flush=True)
        return 500, {"ok": False, "error": str(ex)}
    _annotate_with_auth_health(res)
    codes = res.codes()
    print(f'[sync-rpc] {langcode!r} done: codes={codes!r}',
          file=sys.stderr, flush=True)
    if ('PUSHED' in codes or 'PULLED' in codes
            or 'COMMITTED_AND_PUSHED' in codes):
        projects.set_last_sync(langcode)
    if ('COMMITTED_LOCAL' in codes or 'COMMITTED_NO_REMOTE' in codes
            or 'COMMITTED_AND_PUSHED' in codes):
        projects.set_last_commit(langcode)
    if 'PUSHED' in codes or 'COMMITTED_AND_PUSHED' in codes:
        scheduler._set_pending_push(langcode, False)
    elif 'COMMITTED_LOCAL' in codes:
        scheduler._set_pending_push(langcode, True)
    # ``head_sha`` (0.53.0): post-sync HEAD so the caller can update
    # its cached base (and decide whether a reload is needed after
    # PULLED) without a follow-up project_status poll. Top-level key
    # — old clients' Result decode ignores it.
    return 200, {"ok": True, "result": res.to_dict(),
                 "head_sha": repo_mod.head_sha_of(p.working_dir)}


def _h_project_status(langcode, _body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    # Auto-migrate pre-0.45.37 LAN-cloned projects: if origin
    # points at a private-IP URL (a peer's LAN listener), strip
    # it so the publish-row gate sees an empty remote_url and
    # Publish becomes visible. Idempotent — no-op on healthy
    # github/gitlab origins.
    try:
        from .repo import strip_lan_origin_if_present as _strip_lan
        _strip_lan(p.working_dir)
    except Exception as ex:
        print(f'[project_status] strip_lan_origin {langcode!r} '
              f'failed: {ex!r}',
              file=sys.stderr, flush=True)
    summary = _repo_status(p.working_dir)
    branch, remote_url, n_changes, wan_unshared = ('', '', 0, 0)
    if summary is not None:
        branch, remote_url, n_changes, wan_unshared = summary
    # ``lan_unshared`` and ``at_risk`` need the langcode for peer
    # lookup, which ``repo_status_summary`` doesn't carry; open
    # the project repo once more and compute them inline. Both
    # return 0 on any failure (matching the OK-on-uncertainty
    # contract of ``_wan_unshared``).
    lan_unshared = 0
    at_risk = 0
    # main_merged: True only when the local branch tip is fully on
    # github's main — the gate for the "OK" sync state. Since 0.53.3
    # ``wan_unshared`` counts DOWN as a chunked topic-push uploads
    # history and reaches 0 when all bytes are on github but BEFORE
    # the final merge; ``main_merged`` distinguishes that "WAN-0,
    # finishing" window from a genuinely backed-up "OK" project. False
    # on any uncertainty so the UI never falsely claims "backed up".
    main_merged = False
    try:
        from dulwich.repo import Repo as _Repo
        from .repo import _lan_unshared as _calc_lan_unshared
        from .repo import _at_risk as _calc_at_risk
        from .repo import _main_merged as _calc_main_merged
        _diag_repo = None
        try:
            _diag_repo = _Repo(p.working_dir)
        except Exception:
            _diag_repo = None
        if _diag_repo is not None and branch:
            try:
                lan_unshared = _calc_lan_unshared(
                    _diag_repo, branch, langcode)
            except Exception:
                lan_unshared = 0
            try:
                at_risk = _calc_at_risk(
                    _diag_repo, branch, langcode)
            except Exception:
                at_risk = 0
            try:
                main_merged = _calc_main_merged(_diag_repo, branch)
            except Exception:
                main_merged = False
            try:
                _diag_repo.close()
            except Exception:
                pass
    except Exception:
        pass
    api = _project_for_api(p)
    # HEAD SHA — uniform change signal for peers (CLIENT_INTEGRATION.md
    # § 17b Background refresh obligation). Bumps on every HEAD
    # advance: local commit, incoming receive-pack, merge commit.
    # Empty when the project has no commits yet (pre-init, or
    # pre-first-commit). Cheap to read; held off the dulwich
    # ``refs`` lookup, no full status walk required.
    head_sha = ''
    # Foreign-device topic-branch orphan visibility (audit
    # finding #3, 0.50.15). Count refs/remotes/origin/azt-pending-*
    # entries that do NOT carry our own device_name suffix —
    # those are orphans left by other devices' incomplete
    # uploads. The janitor (``repo._maybe_run_janitor``) only
    # sweeps OUR own; foreign orphans stay until their owner
    # device returns, which can be never. Surface the count so
    # a user troubleshooting "why is this remote so heavy" can
    # see them.
    foreign_topic_orphan_count = 0
    try:
        from dulwich.repo import Repo
        _r = Repo(p.working_dir)
        try:
            _h = _r.refs[b'HEAD']
            if isinstance(_h, bytes):
                head_sha = _h.decode('ascii', 'replace')
            else:
                head_sha = str(_h)
            try:
                from .repo import _count_foreign_topic_orphans
                foreign_topic_orphan_count = \
                    _count_foreign_topic_orphans(_r)
            except Exception:
                foreign_topic_orphan_count = 0
        finally:
            try:
                _r.close()
            except Exception:
                pass
    except Exception:
        head_sha = ''
    # Stuck-commit telemetry: peers polling status surface
    # COMMIT_REPEATEDLY_FAILED once count >= 2 (matches the
    # daemon's own threshold). The scheduler retries failed
    # commits in the background with exponential backoff, so
    # ``commit_failure_count`` reflects the running streak of
    # retries, not just the count at user-gesture time.
    raw = projects._load_raw().get(langcode, {})
    commit_failure_count = int(raw.get('commit_failure_count', 0) or 0)
    last_commit_failure_at = float(
        raw.get('last_commit_failure_at', 0.0) or 0.0)
    last_commit_error = raw.get('last_commit_error', '') or ''
    # Access-class reason the last WAN sync failed (0.52.24, req 1.1):
    # AUTH_REQUIRED / REPO_NO_ACCESS / REPO_NOT_AUTHORIZED / APP_SUSPENDED
    # / … — a typed status CODE (not translated text) the peer routes to
    # a persistent "sync blocked: <reason>" banner instead of silently
    # backing off. Empty when the last sync had no access problem.
    last_sync_error = raw.get('last_sync_error', '') or ''
    last_sync_error_at = float(raw.get('last_sync_error_at', 0.0) or 0.0)
    # Atomic-recovery diagnostic counter; resets at the day
    # boundary. Purely informational — the recovery happens
    # daemon-side without any user gesture; this field lets a
    # settings / diagnostic screen show "we picked up N
    # un-finalized writes today" without the peer needing to
    # know the underlying protocol.
    import time as _time
    today = _time.strftime('%Y-%m-%d')
    if raw.get('last_recovery_day') == today:
        n_recovered_today = int(raw.get('recovered_today', 0) or 0)
    else:
        n_recovered_today = 0
    from . import settings as _settings
    # Diagnostic trace: each picker poll emits one short line with
    # the fields field-testers most need when triaging a stuck
    # indicator (uncommitted-changes ``n_changes``, stuck-commit
    # streak ``commit_failure_count``, and the dulwich error
    # string from the latest failure). Lets a tester reading the
    # daemon log see whether commits are *failing* (count climbs,
    # err present) vs *not being requested* (count stays at 0
    # while n_changes climbs). 0.45.29.
    if n_changes or commit_failure_count or last_commit_error:
        err_tail = last_commit_error[:120] if last_commit_error else ''
        print(f'[project_status] {langcode!r} n_changes={n_changes} '
              f'wan_unshared={wan_unshared} '
              f'lan_unshared={lan_unshared} at_risk={at_risk} '
              f'commit_fail={commit_failure_count} '
              f'last_err={err_tail!r}',
              file=sys.stderr, flush=True)
    return 200, {
        "ok": True,
        "langcode": langcode,
        "branch": branch,
        "remote_url": remote_url or p.remote_url,
        "n_changes": n_changes,
        # Sync-status accounting (v0.47.0 — replaces the pre-0.47
        # ``commits_ahead`` + ``unshared_commits`` pair). Three
        # counts feed the 5-state indicator per § 17b:
        #   wan_unshared — commits not on github (was commits_ahead)
        #   lan_unshared — commits not on any paired peer
        #   at_risk     — commits on neither channel (intersection)
        # Clean rename + new field; old field names are gone. Old
        # peers get a decode-time miss and refuse via the
        # MIN_SERVER_VERSION check.
        "wan_unshared": wan_unshared,
        "lan_unshared": lan_unshared,
        "at_risk": at_risk,
        # Merge gate for the "OK" state (since 0.53.3). Since
        # ``wan_unshared`` now counts down during a chunked topic-push
        # and hits 0 once all bytes are on github but before the final
        # merge, ``wan_unshared == 0`` no longer implies "backed up".
        # "OK" requires ``main_merged`` too; ``wan_unshared == 0 and
        # not main_merged`` is the "WAN-0, finishing" window. Older
        # peers that don't read this key default it True (§ 17b),
        # preserving pre-0.53 behaviour (OK when wan==0).
        "main_merged": main_merged,
        "last_commit": p.last_commit,
        "last_sync": p.last_sync,
        "working_dir": p.working_dir,
        "lift_path": api['lift_path'],
        # Per-project metadata that peers occasionally need on the
        # status response without a separate ``open_project`` call.
        # Pre-0.39 callers ignore unknown keys.
        "repo_slug": p.repo_slug,
        "cawl_image_repo": p.cawl_image_repo,
        # Stuck-commit telemetry (since 0.41.27). Pre-0.41.27
        # callers ignore the unknown keys; the daemon emits zero
        # values when the project is healthy.
        "commit_failure_count": commit_failure_count,
        "last_commit_failure_at": last_commit_failure_at,
        "last_commit_error": last_commit_error,
        # Access-class sync blocker (0.52.24). Typed status code + when.
        # Empty string when the last sync had no access problem.
        "last_sync_error": last_sync_error,
        "last_sync_error_at": last_sync_error_at,
        # Atomic-recovery diagnostic (since 0.41.27): count of
        # orphan LIFT scratches the daemon auto-recovered today.
        # Zero on a healthy project; positive when Phase-1-only
        # writes were merged back in.
        "n_recovered_today": n_recovered_today,
        # Work-offline state (since 0.43.0). Daemon-wide bool, not
        # per-project; carried here so peers can render the
        # offline / LAN-only suffix without a second RPC.
        "work_offline": _settings.work_offline(),
        # LAN-sync toggle (since 0.45.0). Carried alongside
        # work_offline so peers can render the joint state — the
        # combination work_offline=on + lan_allow_sync=on is
        # "LAN-only", which is delivery-active (paired phones get
        # commits) and shouldn't be labeled as "offline" in the
        # peer sync-indicator. Same daemon-wide-bool-on-per-
        # project-response shape as work_offline.
        "lan_allow_sync": _settings.lan_allow_sync(),
        "lan_pushed_sha": projects.get_last_lan_pushed_sha(langcode),
        # Uniform HEAD-advance signal (since 0.45.45). Peers
        # polling project_status use a change in this field as
        # the trigger for ``_refresh_in_place`` per
        # CLIENT_INTEGRATION.md § 17b. Bumps on local commit,
        # incoming LAN receive-pack, and merge commits — the
        # complete set of events that mutate the on-disk view of
        # this project the peer is rendering.
        "head_sha": head_sha,
        # Foreign-device topic-branch orphan count (since 0.50.15,
        # audit finding #3). Number of
        # ``refs/remotes/origin/azt-pending-*`` refs whose
        # device-name suffix isn't ours — i.e. orphans left by
        # other devices' incomplete uploads that our janitor
        # can't safely sweep (false-positive risk if their device
        # is mid-Phase-A). Informational only; peers can render a
        # warning or a "this remote is messy" diagnostic without
        # acting on it. Pre-0.50.15 peers ignore the unknown key.
        "foreign_topic_orphan_count": foreign_topic_orphan_count,
    }


def _h_set_project_last_sync(langcode, body):
    ts = body.get('timestamp')
    projects.set_last_sync(langcode, ts)
    return 200, {"ok": True}


def _parse_github_owner_repo(remote_url):
    """Extract ``(owner, repo)`` from a GitHub remote URL. Returns
    ``None`` if the URL isn't a recognisable GitHub remote.

    Accepted shapes:
      - ``https://github.com/<owner>/<repo>`` (with or without ``.git``)
      - ``http://github.com/<owner>/<repo>``
      - ``git@github.com:<owner>/<repo>``"""
    if not remote_url:
        return None
    import re
    url = remote_url.strip().rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    m = re.match(r'^https?://github\.com/([^/]+)/([^/]+)$', url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r'^git@github\.com:([^/]+)/([^/]+)$', url)
    if m:
        return m.group(1), m.group(2)
    return None


def _h_grant_collaborator(langcode, body):
    """``POST /v1/projects/<lang>/collaborators``. Invite a GitHub
    user as a collaborator on the repo backing ``langcode``.

    Looks the repo up via the project's ``remote_url`` so the peer
    only has to pass a langcode — the server-side lookup eliminates
    any "wrong project" risk from peer-side URL handling. Returns
    a Result-shaped response with one of:
    ``COLLABORATOR_INVITED``, ``COLLABORATOR_ALREADY``,
    ``INVALID_USERNAME``, ``NO_REMOTE``, ``NOT_GITHUB_REMOTE``,
    ``AUTH_REQUIRED``, or ``COLLABORATOR_INVITE_FAILED``.

    Permission level defaults to ``push`` (matches typical SIL
    collaborator flow); callers can override via ``body['level']``."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    username = (body.get('username') or '').strip()
    permission = (body.get('level') or 'push').strip() or 'push'
    if not username:
        res = Result().add(S.INVALID_USERNAME)
        return 200, {"ok": True, "result": res.to_dict()}
    if not p.remote_url:
        res = Result().add(S.NO_REMOTE)
        return 200, {"ok": True, "result": res.to_dict()}
    parsed = _parse_github_owner_repo(p.remote_url)
    if parsed is None:
        res = Result().add(S.NOT_GITHUB_REMOTE,
                           remote_url=p.remote_url)
        return 200, {"ok": True, "result": res.to_dict()}
    owner, repo_name = parsed
    _git_user, token = store.get_sync_credentials(p.remote_url)
    if not token:
        res = Result().add(S.AUTH_REQUIRED)
        return 200, {"ok": True, "result": res.to_dict()}
    try:
        outcome = auth.add_collaborator(
            owner, repo_name, username, token, permission=permission)
    except Exception as ex:
        print(f'[collab-grant] {langcode!r} {owner}/{repo_name} '
              f'{username!r} → {type(ex).__name__}: {ex}',
              file=sys.stderr, flush=True)
        res = Result().add(
            S.COLLABORATOR_INVITE_FAILED, error=str(ex),
            owner_repo=f'{owner}/{repo_name}', username=username)
        return 200, {"ok": True, "result": res.to_dict()}
    res = Result()
    if outcome == 'already':
        res.add(S.COLLABORATOR_ALREADY, username=username,
                owner_repo=f'{owner}/{repo_name}')
    else:
        res.add(S.COLLABORATOR_INVITED, username=username,
                owner_repo=f'{owner}/{repo_name}',
                permission=permission)
    # A-nudge (0.52.24): a grant may have just unblocked THIS project (the
    # same-device case where the granter is also the blocked peer). Clear
    # its WAN backoff so it retries now rather than waiting out the curve.
    # Harmless when the project wasn't blocked.
    try:
        scheduler.nudge_project(langcode)
    except Exception:
        pass
    return 200, {"ok": True, "result": res.to_dict()}


def _h_project_kv_get(langcode, key, _body):
    """``GET /v1/projects/<lang>/kv/<key>`` — read a scalar
    project-KV value (string). Returns ``{value}`` (empty
    string for unset).

    Storage is ``<working_dir>/.azt/kv/<key>.txt`` (per
    ``project_kv`` module)."""
    from . import project_kv as _pkv
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not _pkv._is_safe_name(key):
        return 400, {"ok": False, "error": "bad_key"}
    return 200, {"ok": True, "value": _pkv.kv_get(p.working_dir, key)}


def _h_project_kv_set(langcode, key, body):
    """``POST /v1/projects/<lang>/kv/<key>`` — write a scalar
    project-KV value and fire a debounced commit so it
    propagates to paired peers via the existing sync
    pipeline. Body: ``{value: str}``.

    Held under ``project_lock`` so an incoming LAN receive
    can't race past our write and ``reset --hard`` over the
    new file before its ``status`` check sees it as unstaged.
    See ``lan_listener._reset_working_tree_after_receive``
    which serializes on the same lock and inspects working-
    tree mods inside that critical section.
    """
    from . import project_kv as _pkv
    from .locks import LockTimeout
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not _pkv._is_safe_name(key):
        return 400, {"ok": False, "error": "bad_key"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    value = body.get('value', '')
    if value is None:
        value = ''
    try:
        with project_lock(p.working_dir, timeout=10):
            _pkv.kv_set(p.working_dir, key, str(value))
    except LockTimeout:
        return 503, {"ok": False, "error": "busy"}
    except Exception as ex:
        return 500, {"ok": False,
                     "error": f'kv_set raised: {ex!r}'}
    _touch_project(langcode)
    job_id = scheduler.commit_project(langcode)
    return 200, {"ok": True, "value": str(value), "job_id": job_id}


def _h_project_kv_list(langcode, _body):
    """``GET /v1/projects/<lang>/kv`` — return every KV entry
    as ``{kv: {key: value, ...}}``."""
    from . import project_kv as _pkv
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    return 200, {"ok": True, "kv": _pkv.kv_list(p.working_dir)}


def _h_project_slots_list(langcode, _body):
    """``GET /v1/projects/<lang>/slots`` — return current slot
    claims as ``{slots: {slot: {peer_id, claimed_at, device_name}, ...}}``.

    Slots are typically integers stringified (``"1"``, ``"2"``…)
    but the storage layer permits any safe filename — peer UIs
    decide the slot keyspace."""
    from . import project_kv as _pkv
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    return 200, {"ok": True,
                 "slots": _pkv.slot_list(p.working_dir)}


def _h_project_slot_claim(langcode, body):
    """``POST /v1/projects/<lang>/slots/claim`` — claim a slot.
    Body: ``{slot: str}``. Atomically (locally) displaces any
    prior claim by this peer on a different slot; convergent
    against simultaneous claims of the same slot by other
    peers (post-merge resolver picks the later ``claimed_at``).

    Identity (``peer_id`` + ``device_name``) comes from the
    daemon's own LAN identity / contributor — peers do not pass
    these on the wire (same pattern as commit identity)."""
    from . import project_kv as _pkv
    from . import peer_id as _peer_id_mod
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    slot = body.get('slot')
    if not isinstance(slot, (str, int)) or not str(slot):
        return 400, {"ok": False, "error": "missing_slot"}
    slot = str(slot)
    if not _pkv._is_safe_name(slot):
        return 400, {"ok": False, "error": "bad_slot"}
    refusal = _refuse_if_contributor_unset()
    if refusal is not None:
        return refusal
    try:
        ident = _peer_id_mod.ensure()
    except Exception as ex:
        return 500, {"ok": False,
                     "error": f'no_lan_identity: {ex!r}'}
    # Held under project_lock for the same reason as kv_set —
    # closes the race where a LAN receive's status-check could
    # miss our pending claim and hard-reset over it.
    from .locks import LockTimeout
    try:
        with project_lock(p.working_dir, timeout=10):
            ok = _pkv.slot_claim(p.working_dir, ident['peer_id'],
                                 store.get_device_name(), slot)
    except LockTimeout:
        return 503, {"ok": False, "error": "busy"}
    if not ok:
        return 400, {"ok": False, "error": "claim_refused"}
    _touch_project(langcode)
    job_id = scheduler.commit_project(langcode)
    return 200, {"ok": True, "slot": slot, "job_id": job_id}


def _h_project_slot_rebind(langcode, slot, _body):
    """``POST /v1/projects/<lang>/slots/<slot>/rebind`` — rewrite
    the identity atoms (peer_id + device_name) of an existing
    slot claim to this daemon's current values.

    Used by the user-driven recovery flow (0.50.9+) when the
    daemon's ``peer_id`` changed since the slot was claimed
    (server-APK reinstall regenerated the LAN identity; user
    cleared app data) but the user knows the slot is still
    theirs. The peer-side guard rail is a confirm popup driven
    by a contributor-name match against the existing claim's
    ``device_name``; this RPC is just the persistence half.

    Refreshes ``claimed_at`` to now, so a rebind wins any
    concurrent claim by another peer in the merge.

    Returns 400 ``slot_not_found`` if no claim exists at that
    slot (rebind only retags existing claims; it doesn't create
    new ones — for that the peer uses ``slot_claim`` directly).
    """
    from . import project_kv as _pkv
    from . import peer_id as _peer_id_mod
    from .locks import LockTimeout
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    slot = str(slot or '')
    if not slot or not _pkv._is_safe_name(slot):
        return 400, {"ok": False, "error": "bad_slot"}
    refusal = _refuse_if_contributor_unset()
    if refusal is not None:
        return refusal
    try:
        ident = _peer_id_mod.ensure()
    except Exception as ex:
        return 500, {"ok": False,
                     "error": f'no_lan_identity: {ex!r}'}
    try:
        with project_lock(p.working_dir, timeout=10):
            ok = _pkv.slot_rebind(p.working_dir, ident['peer_id'],
                                  store.get_device_name(), slot)
    except LockTimeout:
        return 503, {"ok": False, "error": "busy"}
    if not ok:
        return 400, {"ok": False, "error": "slot_not_found"}
    _touch_project(langcode)
    job_id = scheduler.commit_project(langcode)
    return 200, {"ok": True, "slot": slot, "job_id": job_id}


def _h_project_slot_release(langcode, _body):
    """``POST /v1/projects/<lang>/slots/release`` — release
    every slot held by this peer. Returns the list of slots
    that were released so the UI can render "you released
    slot N". Idempotent — empty list if we held nothing."""
    from . import project_kv as _pkv
    from . import peer_id as _peer_id_mod
    from .locks import LockTimeout
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    try:
        ident = _peer_id_mod.ensure()
    except Exception as ex:
        return 500, {"ok": False,
                     "error": f'no_lan_identity: {ex!r}'}
    try:
        with project_lock(p.working_dir, timeout=10):
            released = _pkv.slot_release(p.working_dir,
                                         ident['peer_id'])
    except LockTimeout:
        return 503, {"ok": False, "error": "busy"}
    if released:
        _touch_project(langcode)
        scheduler.commit_project(langcode)
    return 200, {"ok": True, "released": released}


def _h_project_commit(langcode, _body):
    """``POST /v1/projects/<lang>/commit`` — schedule a debounced
    commit. As of 0.43.0 commit and push are split: this endpoint
    only commits (replacing the old ``sync_async`` which did both).
    Push happens on the connectivity watcher's drain loop based on
    online state + post-online grace + work_offline. Peers call
    this per group of related changes.

    Contributor is read from the daemon's store at exec time; if
    unset, the job result carries ``S.CONTRIBUTOR_UNSET``. Peers
    poll via ``poll_job(job_id)``."""
    p = projects.get(langcode)
    if p is None:
        print(f'[commit-rpc] {langcode!r} → project_not_found',
              file=sys.stderr, flush=True)
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    job_id = scheduler.commit_project(langcode)
    print(f'[commit-rpc] {langcode!r} enqueued job_id={job_id!r}',
          file=sys.stderr, flush=True)
    return 200, {"ok": True, "job_id": job_id}


def _h_get_job(job_id, _body):
    job = scheduler.get_job(job_id)
    if job is None:
        return 404, {"ok": False, "error": "job_not_found"}
    return 200, {"ok": True, **job.to_dict()}


# ── Atomic file write ─────────────────────────────────────────────────────
#
# Peers on Android reach the daemon's filesystem via the
# ContentProvider's openFileDescriptor — they receive an FD into a
# file under the daemon's private filesDir. ``ftruncate(fd, 0)`` +
# write through that FD is NOT atomic from the perspective of any
# other reader (another peer, the daemon's own merge-output writer):
# a concurrent observer can see torn bytes mid-write. The
# 2026-05-12 ``baf`` repro is one realization of this — two LIFT
# serializations interleaved through the FD path and produced
# malformed XML.
#
# The endpoint below closes the gap: the peer sends the full file
# bytes inline (base64 in the JSON body), the daemon serializes
# the write through the project lock, and writes via tempfile +
# os.replace. The destination is always a complete copy of one
# version, never a torn mix.

_ATOMIC_COMMIT_ALLOWED_DIRS = ('audio', 'images')


def _resolve_atomic_commit_path(working_dir, rel):
    """Validate ``rel`` against the atomic-commit whitelist and
    return the absolute path under *working_dir*, or None if the
    shape is rejected.

    Allowed shapes (mirror of the ContentProvider's
    ``_resolve_path`` whitelist, scoped to a known working_dir):

    - ``<file>.lift``           — top-level LIFT file
    - ``audio/<file>``          — sibling audio
    - ``images/<file>``         — sibling image

    Any other shape — empty segments, ``..``, three-level paths —
    is rejected before any filesystem touch. A final
    ``os.path.commonpath`` check guards against symlink-based
    escapes."""
    if not rel or not isinstance(rel, str):
        return None
    rel = rel.lstrip('/')
    parts = rel.split('/')
    if any(p in ('', '..', '.') for p in parts):
        return None
    if len(parts) == 1:
        if not parts[0].lower().endswith('.lift'):
            return None
    elif len(parts) == 2:
        if parts[0] not in _ATOMIC_COMMIT_ALLOWED_DIRS:
            return None
    else:
        return None
    base = os.path.realpath(working_dir)
    target = os.path.realpath(os.path.join(base, *parts))
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    return target


def _h_project_atomic_commit(langcode, body):
    """POST /v1/projects/<lang>/atomic_commit

    Request body::

        {"path": "<rel_path>", "data_b64": "<base64-encoded-bytes>"}

    Writes the bytes atomically to ``<working_dir>/<rel_path>``.
    The write goes through a sibling tempfile + ``os.replace``,
    serialized via ``project_lock`` so it can't overlap with a
    sync's merge-output write or another atomic_commit. The
    destination is never torn.

    Returns ``ATOMIC_COMMITTED`` (with ``bytes_written`` and
    ``sha256``) on success. Path-validation failures return 400;
    filesystem failures return 500."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    rel = body.get('path') or ''
    data_b64 = body.get('data_b64') or ''
    if not isinstance(rel, str) or not rel:
        return 400, {"ok": False, "error": "missing_path"}
    if not isinstance(data_b64, str):
        return 400, {"ok": False, "error": "invalid_data"}
    try:
        data = base64.b64decode(data_b64, validate=True)
    except Exception as ex:
        return 400, {"ok": False, "error": f"base64_decode: {ex}"}
    target = _resolve_atomic_commit_path(p.working_dir, rel)
    if target is None:
        return 400, {"ok": False, "error": "path_rejected"}
    _touch_project(langcode)
    tmp = f'{target}.tmp.{os.getpid()}.{secrets.token_hex(8)}'
    try:
        with project_lock(p.working_dir):
            os.makedirs(os.path.dirname(target) or '.', exist_ok=True)
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, target)
    except Exception as ex:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        return 500, {"ok": False, "error": str(ex)}
    # Auto-fire a debounced commit (default behaviour). Same race
    # + same fix as the sibling ``_h_project_atomic_finalize``.
    # Peer may pass ``commit_after: false`` (0.50.51) to suppress
    # the auto-commit when they own the commit boundary
    # themselves (e.g. recorder swipe = "I accept this take";
    # writes during playback / re-record should NOT commit). See
    # CLIENT_INTEGRATION § 11 step 6 + NOTES history.
    if body.get('commit_after', True):
        try:
            scheduler.commit_project(langcode)
        except Exception as ex:
            print(f'[atomic_commit] auto-commit schedule failed for '
                  f'{langcode!r}: {ex!r}',
                  file=sys.stderr, flush=True)
    res = Result().add(S.ATOMIC_COMMITTED,
                       bytes_written=len(data),
                       sha256=hashlib.sha256(data).hexdigest())
    return 200, {"ok": True, "result": res.to_dict()}


def _h_project_submit_file(langcode, body):
    """POST /v1/projects/<lang>/submit_file  (0.53.0)

    Base-aware whole-file write + synchronous commit — the desktop
    A-Z+T save primitive. Request body::

        {"path": "<rel_path>",          # atomic-commit whitelist
         "staged_path": "/abs/…part",   # sibling file, same dir
         "base_sha": "<hex|''>",        # HEAD the caller edited on
         "message": "…"}                # optional commit message

    The caller writes its full serialization to ``staged_path``
    (a sibling of the target, so the handoff is a same-filesystem
    ``os.replace`` — no byte copy through the RPC body, unlike
    ``atomic_commit``'s base64 path). The daemon, under
    ``project_lock``: fast-path replaces + commits when HEAD ==
    ``base_sha``; otherwise three-way-merges (blob at base, blob at
    HEAD, staged bytes) so a merge that landed since the caller's
    base is never clobbered. See ``repo.submit_file`` for the full
    semantics and status codes.

    Response: ``{ok, result, head_sha}`` — ``head_sha`` is the
    post-commit HEAD, the caller's next base. Desktop-only by
    design (Android peers use surgical writes; there is no
    cross-process staged-file handoff through the ContentProvider)."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    rel = body.get('path') or ''
    staged = body.get('staged_path') or ''
    base_sha = body.get('base_sha') or ''
    message = body.get('message') or ''
    if not isinstance(rel, str) or not rel:
        return 400, {"ok": False, "error": "missing_path"}
    if not isinstance(staged, str) or not staged:
        return 400, {"ok": False, "error": "missing_staged_path"}
    if not isinstance(base_sha, str):
        return 400, {"ok": False, "error": "invalid_base_sha"}
    target = _resolve_atomic_commit_path(p.working_dir, rel)
    if target is None:
        return 400, {"ok": False, "error": "path_rejected"}
    staged_real = os.path.realpath(staged)
    # Staged file must be a sibling of the target: same directory ⇒
    # same filesystem (os.replace stays atomic) and inside the
    # whitelisted tree (no path smuggling), and must not BE the
    # target (a replace onto itself would no-op and then unlink).
    if (not os.path.isfile(staged_real)
            or os.path.dirname(staged_real) != os.path.dirname(target)
            or staged_real == target):
        return 400, {"ok": False, "error": "staged_rejected"}
    rel_clean = '/'.join(rel.lstrip('/').split('/'))
    contributor = store.get_contributor()
    _touch_project(langcode)
    try:
        res, head_sha = repo_mod.submit_file(
            p.working_dir, rel_clean, staged_real, base_sha,
            contributor, message=message or None)
    except Exception as ex:
        print(f'[submit_file] {langcode!r} raised: {ex!r}',
              file=sys.stderr, flush=True)
        return 500, {"ok": False, "error": str(ex)}
    codes = res.codes()
    print(f'[submit_file] {langcode!r} done: codes={codes!r} '
          f'head={head_sha[:12]!r}', file=sys.stderr, flush=True)
    if 'COMMITTED_LOCAL' in codes:
        # Same post-commit side effects as the debounced commit
        # worker: pending-push for the WAN drain, last_commit stamp,
        # LAN backoff/burst + fan-out so a desktop commit converges
        # to paired peers exactly like a recorder commit.
        scheduler._set_pending_push(langcode, True)
        scheduler.after_committed_local(langcode, p)
    return 200, {"ok": True, "result": res.to_dict(),
                 "head_sha": head_sha}


def _h_set_audio(langcode, body):
    """``POST /v1/projects/<lang>/set_audio``

    Body: ``{guid, lang, filename}``. Surgically writes
    ``<citation>/<form lang={lang}><text>{filename}</text></form>``
    on the entry with the given guid, without round-tripping the
    full LIFT through ElementTree peer-side. The "other forms left
    intact" guarantee and byte-stable-outside-the-entry semantics
    are documented in ``azt_collabd/lift_surgery.py``.

    Auto-fires a debounced commit on success (mirrors the
    ``_h_project_atomic_commit`` pattern), and a
    ``notify_project_changed`` so ContentObserver peers wake fast.

    Returns the typed ``Result`` from
    ``lift_surgery.set_audio`` — ``S.AUDIO_SET`` /
    ``S.AUDIO_SET_NO_CHANGE`` on success;
    ``S.ENTRY_NOT_FOUND`` / ``S.LIFT_INVALID`` / ``S.BUSY`` on
    failure. Since 0.50.29."""
    from . import lift_surgery as _lift_surgery
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    guid = str(body.get('guid', '') or '').strip()
    lang = str(body.get('lang', '') or '').strip()
    filename = str(body.get('filename', '') or '').strip()
    if not guid:
        return 400, {"ok": False, "error": "missing_guid"}
    if not lang:
        return 400, {"ok": False, "error": "missing_lang"}
    if not filename:
        return 400, {"ok": False, "error": "missing_filename"}
    if not p.lift_path:
        return 400, {"ok": False, "error": "no_lift_path"}
    _touch_project(langcode)
    result = _lift_surgery.set_audio(
        p.working_dir, p.lift_path, guid, lang, filename)
    if result.has(S.AUDIO_SET):
        # ``commit_after=False`` suppresses the auto-commit (since
        # 0.50.51) so peers with explicit commit boundaries (e.g.
        # recorder's swipe = "I accept this take") can keep
        # buffered edits out of git history until the boundary
        # fires.
        if body.get('commit_after', True):
            try:
                scheduler.commit_project(langcode)
            except Exception as ex:
                print(f'[set_audio] auto-commit schedule failed for '
                      f'{langcode!r}: {ex!r}',
                      file=sys.stderr, flush=True)
        try:
            from .android_cp import notify as _notify
            _notify.notify_project_changed(langcode)
        except Exception:
            pass
    return 200, {"ok": True, "result": result.to_dict()}


def _h_set_illustration(langcode, body):
    """``POST /v1/projects/<lang>/set_illustration``

    Body: ``{guid, href}``. Surgically writes
    ``<sense>/<illustration href={href}/>`` on the first sense of
    the entry with the given guid (creating ``<sense>`` if absent).
    See ``_h_set_audio`` for the contract shape; this is the
    sibling endpoint for image saves. Since 0.50.29."""
    from . import lift_surgery as _lift_surgery
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    guid = str(body.get('guid', '') or '').strip()
    href = str(body.get('href', '') or '').strip()
    if not guid:
        return 400, {"ok": False, "error": "missing_guid"}
    if not href:
        return 400, {"ok": False, "error": "missing_href"}
    if not p.lift_path:
        return 400, {"ok": False, "error": "no_lift_path"}
    _touch_project(langcode)
    result = _lift_surgery.set_illustration(
        p.working_dir, p.lift_path, guid, href)
    if result.has(S.ILLUSTRATION_SET):
        # See ``_h_set_audio`` for the rationale on
        # ``commit_after`` (0.50.51 opt-out).
        if body.get('commit_after', True):
            try:
                scheduler.commit_project(langcode)
            except Exception as ex:
                print(f'[set_illustration] auto-commit schedule failed '
                      f'for {langcode!r}: {ex!r}',
                      file=sys.stderr, flush=True)
        try:
            from .android_cp import notify as _notify
            _notify.notify_project_changed(langcode)
        except Exception:
            pass
    return 200, {"ok": True, "result": result.to_dict()}


def _h_cawl_prefetch(langcode, body):
    """``POST /v1/projects/<lang>/cawl/prefetch`` — start a
    daemon-driven prefetch of a working-set of CAWL image paths.

    Request body::

        {"paths": ["0001_body/foo.png",
                   "0002_skin_of_man/bar.png", ...]}

    The daemon spawns a background worker that iterates the
    paths and warms the cache via ``get_image_path`` (which
    serves from on-disk cache or fetches from GitHub). Returns
    immediately with the initial state; peers poll
    ``cache_status`` for progress.

    Why this exists (and the legacy per-image path doesn't
    cover it): the peer used to iterate the list itself,
    one IPC + one ``get_image_path`` per entry. That worked
    but left the daemon ignorant of the total size of the work
    being done, so its progress reporting could only count
    on-disk-vs.-all-index-files — a misleading total. Moving
    the iteration into the daemon means the progress reporter
    knows what it's reporting against, by construction.

    Idempotent: a peer that calls ``prefetch`` twice with the
    same paths gets back the existing state, not a second
    worker. The on-demand ``get_image_path`` path
    (``content://<auth>/<lang>/cawl/images/<rel_path>``) is
    unaffected — peers can still request individual images
    whenever they need them; the daemon serves from cache or
    fetches on demand exactly as before."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    paths = body.get('paths')
    if not isinstance(paths, list):
        return 400, {"ok": False, "error": "missing_paths"}
    _touch_project(langcode)
    repo = _cawl.resolve_image_repo(langcode)
    if not repo:
        return 200, {"ok": True, "image_repo": "",
                     "requested": 0, "completed": 0,
                     "finished": True}
    state = _cawl.start_prefetch(repo, paths)
    if state is None:
        return 400, {"ok": False, "error": "invalid_paths"}
    return 200, {"ok": True, "image_repo": repo,
                 "requested": state['requested'],
                 "completed": state['completed'],
                 "finished": state['finished']}


def _h_cawl_cache_status(langcode, _body):
    """``GET /v1/projects/<lang>/cawl/cache_status`` — return cache
    progress for the image_repo backing this project.

    Response::

        {"ok": True,
         "image_repo":   "<owner/repo>",
         "cached":       <int>,    # images on disk / completed
         "total":        <int>,    # working-set size or index count
         "offline":      <bool>,   # prefetch was skipped because offline
         "circuit_open": <bool>,   # prefetch bailed on consecutive fails
         "finished":     <bool>}   # worker idle for this repo

    Peers poll this on a short interval (5-10 s) while a CAWL
    prefetch is running so they can show a "Caching images: M / N"
    indicator. When ``offline`` is true the peer should badge the
    bar as "offline" rather than render "0 / N" as live progress.

    Both counts are cheap — index lookup is in-memory, on-disk
    count is one ``os.walk`` over the daemon-owned images dir
    (memoised under a short TTL). No network.

    Returns ``cached == 0`` / ``total == 0`` when the project has
    no image_repo configured or the index isn't yet loaded; peers
    should treat that as "nothing to show"."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    # No ``_touch_project`` here: this is a read-only progress
    # poll the indicator runs at 1 Hz. Treating each poll as
    # project activity floods logcat with ``[recent]
    # _touch_project`` lines (one per second per active poller)
    # without adding any real "user is working on X" signal.
    repo = _cawl.resolve_image_repo(langcode)
    if not repo:
        return 200, {"ok": True, "image_repo": "",
                     "cached": 0, "total": 0,
                     "offline": False, "circuit_open": False,
                     "finished": True}
    status = _cawl.cache_status(repo)
    return 200, {"ok": True, "image_repo": repo, **status}


_SHARE_BUNDLE_TTL_S = 3600  # 1 hour
_SHARE_BUNDLE_FILENAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,128}$')


def _h_prepare_share_bundle(_body):
    """``POST /v1/diagnostics/prepare_share_bundle`` — stage the
    snapshot + per-day daemon logs at
    ``$AZT_HOME/.shares/<token>/<filename>`` so the picker /
    settings ``Share diagnostics`` button can ship URIs under
    THIS APK's authority (``content://org.atoznback.aztcollab/
    _shares/<token>/<filename>``) instead of MediaStore
    Downloads URIs.

    Why this exists: Signal refuses MediaStore Downloads URIs
    (its receive-side security policy whitelist) but accepts
    URIs from the sender's own ContentProvider authority.
    Field-diagnosed via 0.52.12 logcat 2026-06-22: Gmail
    accepted the bundle, Signal flashed-and-back. Migrating
    the share to URIs under ``org.atoznback.aztcollab``
    bypasses Signal's policy refusal because the URIs come
    from the same APK initiating the share.

    Body: ``{}`` (no input).

    Returns ``{"ok": True, "token": "<hex>", "items":
    [{"display_name": "<basename>",
      "uri_path": "_shares/<token>/<basename>"}, ...]}``.
    Items order: snapshot first, then per-day logs
    oldest-first (matches ``get_daemon_log_files`` order).

    Cleans up stale share bundles (>1h old) on every call so a
    user who taps Share diagnostics and never picks a target
    doesn't leak. The TTL is generous because some receivers
    (e.g. Signal) hold the URI in the compose draft and don't
    read the file until the message is actually sent — minutes
    later. 1h is enough for plausible compose-and-send flows.

    Since 0.52.13."""
    import secrets
    import shutil
    home = azt_home()
    shares_root = os.path.join(home, '.shares')
    os.makedirs(shares_root, exist_ok=True)

    # Sweep stale bundles BEFORE creating the new one so a
    # stuck-or-abandoned share doesn't accumulate forever.
    now = _time.time()
    try:
        for name in os.listdir(shares_root):
            sub = os.path.join(shares_root, name)
            if not os.path.isdir(sub):
                continue
            try:
                age = now - os.path.getmtime(sub)
            except OSError:
                continue
            if age > _SHARE_BUNDLE_TTL_S:
                try:
                    shutil.rmtree(sub, ignore_errors=True)
                    print(f'[share-bundle] swept stale '
                          f'{name!r} (age={int(age)}s)',
                          file=sys.stderr, flush=True)
                except Exception:
                    pass
    except OSError as ex:
        print(f'[share-bundle] sweep listdir raised: {ex!r}',
              file=sys.stderr, flush=True)

    token = secrets.token_hex(16)
    bundle_dir = os.path.join(shares_root, token)
    os.makedirs(bundle_dir, exist_ok=True)

    # Single gzipped-tar archive. The container FORMAT (tar.gz + name +
    # MIME) is the suite-shared helper ``azt_collab_client.diagnostics``
    # — see that module for the zip→tar.gz rationale (a field mail server
    # strips ``.zip``) and the single-attachment / Signal constraints.
    # Only the COLLECTION is daemon-specific (snapshot + per-day daemon
    # logs); the format lives in the client so it can't drift from the
    # peers' own builders again (was implemented twice; recorder shipped
    # stale ``.zip`` for a build). Daemon imports client = allowed.
    from azt_collab_client.diagnostics import (
        build_diagnostics_targz, diagnostics_archive_name)
    stamp = _time.strftime('%Y%m%d_%H%M%S')
    archive_name = diagnostics_archive_name(slug='', stamp=stamp)
    archive_path = os.path.join(bundle_dir, archive_name)

    # 1. Snapshot (in-memory content).
    try:
        snapshot = _build_diagnostic_snapshot()
    except Exception as ex:
        print(f'[share-bundle] snapshot build raised: {ex!r}',
              file=sys.stderr, flush=True)
        snapshot = ''

    # 2. Per-day daemon logs inside the retention window (real files).
    import datetime as _dt
    try:
        retention = _settings.log_retention_days()
    except Exception:
        retention = 3
    try:
        today_d = _dt.date.fromisoformat(_today_str())
    except ValueError:
        today_d = None
    keep_dates = set()
    if today_d is not None:
        for k in range(retention):
            keep_dates.add(
                (today_d - _dt.timedelta(days=k)).isoformat())

    log_files = [
        (os.path.basename(src_path), src_path)
        for date_str, src_path in _iter_daemon_log_files()
        if not (keep_dates and date_str not in keep_dates)
    ]
    content_items = (
        [(f'azt_snapshot_{stamp}.txt', snapshot)] if snapshot else [])
    try:
        written = build_diagnostics_targz(
            archive_path, file_items=log_files,
            content_items=content_items)
    except OSError as ex:
        print(f'[share-bundle] archive write raised: {ex!r}',
              file=sys.stderr, flush=True)
        return 500, {"ok": False, "error": "share_write_failed"}
    log_entries = max(0, written - (1 if snapshot else 0))

    items = [{
        'display_name': archive_name,
        'uri_path': f'_shares/{token}/{archive_name}',
    }]

    archive_bytes = 0
    try:
        archive_bytes = os.path.getsize(archive_path)
    except OSError:
        pass
    print(f'[share-bundle] prepared token={token!r} '
          f'archive={archive_name!r} '
          f'entries=snapshot:{1 if snapshot else 0}+'
          f'logs:{log_entries} '
          f'size={archive_bytes}',
          file=sys.stderr, flush=True)
    return 200, {"ok": True, "token": token, "items": items}


def _h_append_log(body):
    """``POST /v1/logging/append`` — write a one-line trace from
    a peer process (picker UI, recorder app, etc.) into the
    always-on daemon log so a remote tester's diagnostic share
    captures behaviour from BOTH sides of the daemon /
    UI-process boundary.

    Body: ``{"tag": str, "line": str}``. ``tag`` is a short
    bracketed prefix (typically ``share_files`` /
    ``picker.on_enter`` etc.) so the bundled log groups peer
    traces by subsystem. ``line`` is the human-readable
    payload — kept under a few hundred chars by the caller.

    Since 0.52.11. Born from the field-debug session where
    Signal's flash-and-return symptom couldn't be diagnosed
    without seeing the picker process's share-time decisions:
    its ``print()`` calls land in logcat which is invisible
    on testers' devices without adb. Routing through this
    endpoint puts them in the daemon log, which is shareable
    via the picker's ``Share diagnostics`` button (or via
    ``adb pull`` from the daemon's filesDir).

    Bounded length: each line is capped at 1024 chars
    server-side (longer payloads silently truncated with an
    explicit ``…[truncated]`` suffix) so a misbehaving peer
    can't fill the log with one call. Best-effort: any write
    failure is logged to the original stderr (which on the
    daemon process IS the on-disk log) and the RPC still
    returns ok=True so the peer's diagnostic path isn't
    derailed by a stalled write."""
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    tag = str(body.get('tag') or 'peer')[:64]
    raw_line = str(body.get('line') or '')
    MAX = 1024
    if len(raw_line) > MAX:
        raw_line = raw_line[:MAX] + '…[truncated]'
    try:
        print(f'[{tag}] {raw_line}',
              file=sys.stderr, flush=True)
    except Exception:
        pass
    return 200, {"ok": True}


def _h_set_daemon_log_to_file(_body):
    """``POST /v1/logging/daemon_log_to_file`` — removed in 0.52.7.

    Pre-0.52.7 this flipped the stderr-to-file mirror on/off.
    Logging is now always-on (per-day file in ``$AZT_HOME`` with
    3-day retention; see CHANGELOG 0.52.5 / 0.52.6 / 0.52.7),
    so the endpoint has nothing to toggle.

    Returns HTTP 410 Gone with a typed body so any pre-0.52.7
    peer that calls it gets an explicit "this is gone, your
    code is out of date" response instead of a silent
    misbehaviour. Scheduled for outright deletion in a later
    release once peer apps have caught up."""
    return 410, {"ok": False,
                 "error": "endpoint_removed",
                 "message": "Logging is always-on since 0.52.7. "
                            "Update the peer app to drop this call."}


def _dump_lan_debug_snapshot():
    """Emit one ``[lan-debug]`` log line per registered project
    with the full ``lan_debug`` payload (HEAD branch / SHA,
    ancestor count, origin URL, tracking ref, all local branches,
    all remote refs, current ``wan_unshared`` reading). Since
    0.52.7, fired once per daily log file: on the initial install
    when today's file is empty, and on each midnight rotation
    after the new day's file is opened. This anchors every
    per-day file with a "state of the world at start of day"
    snapshot a triager can read top-to-bottom — no separate
    button required, since logging is always-on."""
    import json as _json
    try:
        data = projects._load_raw()
    except Exception as ex:
        print(f'[lan-debug] _load_raw raised: {ex!r}',
              file=sys.stderr, flush=True)
        return
    langs = sorted(data.keys())
    print(f'[lan-debug] snapshot start: {len(langs)} project(s)',
          file=sys.stderr, flush=True)
    for langcode in langs:
        try:
            _, resp = _h_lan_debug(langcode, {})
        except Exception as ex:
            print(f'[lan-debug] {langcode!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        print(f'[lan-debug] {langcode!r} '
              + _json.dumps(resp, sort_keys=True),
              file=sys.stderr, flush=True)
    print('[lan-debug] snapshot end',
          file=sys.stderr, flush=True)


def _build_diagnostic_snapshot():
    """Multi-line text describing the daemon's view of ``$AZT_HOME``
    state. Used by the picker's "Share diagnostics" button so a user
    stuck on an empty picker can ship a snapshot for remote support
    without having to first navigate into a project (the existing
    Share-daemon-log path is reachable only via the daemon settings
    UI, which on the recorder side is unreachable from the picker).

    Surfaces:

    - daemon version
    - ``$AZT_HOME`` path
    - ``projects.json`` state (size, mtime, parsed entries)
    - on-disk subdirectories with ``.git`` / LIFT / audio presence
    - which subdirs are registered vs orphan (the "registry says
      no projects, but bytes on disk" case lands here)
    - relevant config (lan.allow_sync)
    - short peer-id tag

    Plain text suitable for embedding in the shared daemon log.
    Never raises — every section catches its own exceptions and
    emits an inline error marker rather than failing the whole
    snapshot.
    """
    lines = ['=== AZT Collab diagnostic snapshot ===',
             f'daemon_version: {_VERSION}']
    try:
        home = azt_home()
        lines.append(f'AZT_HOME: {home}')
    except Exception as ex:
        lines.append(f'AZT_HOME: <error: {ex!r}>')
        lines.append('=== end snapshot ===')
        return '\n'.join(lines) + '\n'

    registered_dirs = set()
    pj_path = projects.projects_path()
    try:
        pj_size = os.path.getsize(pj_path)
    except OSError:
        pj_size = -1
    if pj_size >= 0:
        try:
            pj_mtime = _time.strftime(
                '%Y-%m-%d %H:%M:%S',
                _time.localtime(os.path.getmtime(pj_path)))
        except OSError:
            pj_mtime = '<unknown>'
    else:
        pj_mtime = '<missing>'
    lines.append(
        f'projects.json: path={pj_path} size={pj_size} mtime={pj_mtime}')
    if pj_size > 0:
        try:
            with open(pj_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            entries = (list(data.keys())
                       if isinstance(data, dict) else [])
            lines.append(f'projects.json entries: {len(entries)}')
            for k in entries:
                ent = data.get(k) or {}
                wd = ent.get('working_dir', '')
                lp = ent.get('lift_path', '')
                ru = ent.get('remote_url', '')
                wd_ok = bool(wd) and os.path.isdir(wd)
                lp_ok = bool(lp) and os.path.isfile(lp)
                lines.append(
                    f'  [{k}] working_dir={wd!r} exists={wd_ok} '
                    f'lift_path={lp!r} exists={lp_ok} '
                    f'remote_url={ru!r}')
                if wd:
                    try:
                        registered_dirs.add(os.path.realpath(wd))
                    except Exception:
                        pass
        except (OSError, ValueError) as ex:
            lines.append(f'projects.json: PARSE_ERROR {ex!r}')
    elif pj_size == 0:
        lines.append('projects.json: EMPTY (zero bytes)')
    else:
        lines.append('projects.json: MISSING')

    # Projects live under ``$AZT_HOME/projects/<langcode>/`` (see
    # ``lan_clone._project_dir`` — the daemon-side canonical
    # convention). Listing the home root is useful only for
    # context (CAWL dir, peer.crt, etc.), not for project scan.
    lines.append('AZT_HOME root listing (context only):')
    try:
        for n in sorted(os.listdir(home)):
            p = os.path.join(home, n)
            if os.path.isdir(p):
                lines.append(f'  {n}/')
            else:
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = -1
                lines.append(f'  {n} ({sz} bytes)')
    except OSError as ex:
        lines.append(f'  <listdir error: {ex!r}>')

    projects_dir = os.path.join(home, 'projects')
    lines.append(f'projects directory: {projects_dir}')
    if not os.path.isdir(projects_dir):
        lines.append('  <missing — no projects dir on disk>')
    else:
        try:
            names = sorted(os.listdir(projects_dir))
        except OSError as ex:
            names = []
            lines.append(f'  <listdir error: {ex!r}>')
        if not names:
            lines.append('  <empty>')
        for n in names:
            p = os.path.join(projects_dir, n)
            if not os.path.isdir(p) or n.startswith('.'):
                continue
            has_git = os.path.isdir(os.path.join(p, '.git'))
            lift_files = []
            try:
                for fn in os.listdir(p):
                    if fn.lower().endswith('.lift'):
                        lift_files.append(fn)
            except OSError:
                pass
            audio_count = 0
            try:
                adir = os.path.join(p, 'audio')
                if os.path.isdir(adir):
                    audio_count = sum(1 for _ in os.listdir(adir))
            except OSError:
                pass
            try:
                registered = os.path.realpath(p) in registered_dirs
            except Exception:
                registered = False
            lines.append(
                f'  [{n}] has_git={has_git} lift={lift_files} '
                f'audio_count={audio_count} registered={registered}')

    try:
        lines.append(
            f'config.lan.allow_sync: {_settings.lan_allow_sync()}')
    except Exception as ex:
        lines.append(f'config.lan.allow_sync: <error: {ex!r}>')

    try:
        info = _peer_id.ensure()
        pid = (info or {}).get('peer_id') or ''
        lines.append(f'peer_id: {pid[:8] if pid else "<unset>"}')
    except Exception as ex:
        lines.append(f'peer_id: <error: {ex!r}>')

    lines.append('=== end snapshot ===')
    return '\n'.join(lines) + '\n'


def _h_get_diagnostic_snapshot(_body):
    """``GET /v1/diagnostics/snapshot`` — registry / filesystem state
    for remote-support diagnostics. Always succeeds (each section
    in ``_build_diagnostic_snapshot`` catches its own errors).
    Surfaced by the picker's Share-diagnostics button so a user
    stuck on an empty picker can ship a snapshot without first
    selecting a project."""
    try:
        text = _build_diagnostic_snapshot()
    except Exception as ex:
        text = f'=== snapshot generation failed: {ex!r} ===\n'
    return 200, {'ok': True, 'text': text}


def _h_get_daemon_log(_body):
    """``GET /v1/logging/daemon_log`` — read the daemon log file
    contents as text. Used by the daemon UI's "Share daemon log"
    button to attach the log to an Android share intent.

    Returns ``{"ok": True, "log": "<text>", "log_path":
    "<path>", "bytes": <int>}``. Empty ``log`` (with bytes=0)
    when the file doesn't exist yet (toggle never enabled, or
    enabled but no output yet). Truncated to the last 256 KB if
    larger — typical share-intent payloads have practical size
    limits, and the recent tail is where diagnostic value lives."""
    path = daemon_log_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return 200, {"ok": True, "log": "", "log_path": path,
                     "bytes": 0}
    MAX = 256 * 1024
    try:
        with open(path, 'r', encoding='utf-8',
                  errors='replace') as f:
            if size > MAX:
                f.seek(size - MAX)
                # Discard up to the next newline so the truncation
                # doesn't land mid-line — easier to read.
                f.readline()
                head_note = (f'[log truncated: showing last '
                             f'{MAX} bytes of {size}-byte file]\n')
                text = head_note + f.read()
            else:
                text = f.read()
    except OSError as ex:
        return 500, {"ok": False, "error": str(ex)}
    return 200, {"ok": True, "log": text, "log_path": path,
                 "bytes": size,
                 "enabled": True}


def _h_get_daemon_log_files(_body):
    """``GET /v1/logging/daemon_log_files`` — read the per-day
    daemon log files inside the retention window
    (``logging.retention_days``, default 3). Used by the
    multi-file share path (since 0.52.6) so the picker can ship
    today's log alongside the prior days in a single
    ``ACTION_SEND_MULTIPLE`` dispatch.

    Returns ``{"ok": True, "files": [...], "retention_days":
    <int>, "enabled": <bool>}`` where each ``files`` entry is
    ``{"date": "YYYY-MM-DD", "filename": "<basename>",
    "content": "<text>", "bytes": <int>}``. Order: oldest first
    so a tester reading top-to-bottom gets chronological flow.

    Each ``content`` is tail-truncated to 256 KB (matching the
    legacy single-file ``_h_get_daemon_log`` cap). The truncation
    header is identical so existing log-reader tooling needs no
    changes.

    Bootstrap behaviours:
    - Files matching a non-tagged basename
      (``daemon-YYYY-MM-DD.log`` — written before the peer_id
      was readable) ARE included; the date is what matters for
      retention bundling.
    - Cross-tag boundary on a single device (peer_id changed
      across releases) IS included — same reason.
    - Files outside the retention window are NOT returned even
      if still on disk (covers the case where retention was
      lowered between the prune sweep and this call)."""
    try:
        retention = _settings.log_retention_days()
    except Exception:
        retention = 3
    MAX = 256 * 1024
    import datetime as _dt
    try:
        today_d = _dt.date.fromisoformat(_today_str())
    except ValueError:
        today_d = None
    keep = set()
    if today_d is not None:
        for k in range(retention):
            keep.add((today_d - _dt.timedelta(days=k)).isoformat())
    files = []
    for date_str, path in _iter_daemon_log_files():
        if keep and date_str not in keep:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        try:
            with open(path, 'r', encoding='utf-8',
                      errors='replace') as f:
                if size > MAX:
                    f.seek(size - MAX)
                    f.readline()
                    head_note = (f'[log truncated: showing last '
                                 f'{MAX} bytes of {size}-byte file]\n')
                    text = head_note + f.read()
                else:
                    text = f.read()
        except OSError as ex:
            print(f'[daemon-log] read failed for {path!r}: {ex}',
                  file=sys.stderr, flush=True)
            continue
        files.append({
            'date': date_str,
            'filename': os.path.basename(path),
            'content': text,
            'bytes': size,
        })
    return 200, {"ok": True, "files": files,
                 "retention_days": retention,
                 "enabled": True}


def _h_admin_restart(_body):
    """``POST /v1/admin/restart`` — restart the daemon process.

    Responds OK immediately and then, after a short delay so the HTTP
    response can flush, terminates the current daemon process. The
    next client RPC re-discovers the daemon:

    * **Desktop loopback**: ``os.execv`` replaces the current process
      image with a fresh ``python -m azt_collabd``. The new process
      inherits the env (PYTHONPATH, AZT_HOME, etc.) and re-acquires
      ``server.lock``, writes a new ``server.json`` (new pid + token),
      so clients see ``SERVICE_RESTARTED`` on their next call.
    * **Android ``:provider``**: ``os._exit(0)`` exits the process.
      Android's ``ContentProvider`` contract lazy-spawns a fresh
      ``:provider`` process on the next peer ``ContentResolver``
      call, and ``Service.onCreate`` re-runs ``service.py`` which
      calls ``reconcile_on_startup()`` to flip in-flight jobs to
      ``JOB_INTERRUPTED``.

    Returns ``{"ok": True, "restarting": True, "transport": "desktop"
    | "android"}``. The ``transport`` hint lets the caller surface
    "Restarting…" UI appropriate to the platform; on Android the
    process is gone within the second, on desktop the re-exec can
    take a few seconds while the new interpreter boots.

    Auth: callers must already have ``Authorization: Bearer <token>``
    (this is in the standard authenticated POST set, not in
    ``UNAUTHENTICATED_PATHS``). On Android the ContentProvider
    transport enforces the signature-level
    ``AZT_COLLAB_ACCESS`` permission, so peer apps can call this iff
    they're suite-signed — there's no admin-token concept on top of
    that.
    """
    is_android = _on_android()
    transport_label = 'android' if is_android else 'desktop'

    def _restart_after_response():
        # Give the HTTP / ContentProvider response time to flush back
        # to the caller before we yank the process out from under it.
        # 0.5 s is empirically enough on desktop (Python's HTTPServer
        # acks before the handler returns) and Android (Binder return
        # is synchronous, so the peer has the response in hand the
        # moment the dispatch returns).
        _time.sleep(0.5)
        try:
            print(f'[azt_collabd] /v1/admin/restart fired '
                  f'({transport_label}); exiting', flush=True)
        except Exception:
            pass
        if is_android:
            # Don't re-exec on Android — there's no Python interpreter
            # to spawn standalone, and the `:provider` process is
            # owned by the OS. START_STICKY + ContentProvider's
            # unconditional auto-spawn handle the rest.
            os._exit(0)
        # Desktop: re-exec the current Python with ``-m azt_collabd``.
        # ``os.execv`` keeps the same PID and inherits env vars
        # (PYTHONPATH, AZT_HOME, etc.). The flock on server.lock is
        # released by the kernel on the old image's teardown; the
        # new image re-acquires it.
        try:
            os.execv(sys.executable,
                     [sys.executable, '-m', 'azt_collabd'])
        except Exception as ex:
            print(f'[azt_collabd] restart os.execv failed: {ex!r} — '
                  f'falling back to plain exit; the next client '
                  f'call will auto-spawn',
                  flush=True)
            os._exit(0)

    threading.Thread(
        target=_restart_after_response,
        name='admin-restart',
        daemon=True,
    ).start()
    return 200, {"ok": True, "restarting": True,
                 "transport": transport_label}


def _h_project_atomic_finalize(langcode, body):
    """POST /v1/projects/<lang>/atomic_finalize

    Request body::

        {"token": "<hex-token>", "path": "<rel_path>"}

    Atomically renames ``<working_dir>/.azt_atomic_pending/<token>``
    (a scratch file the peer wrote via the ContentProvider FD path)
    to ``<working_dir>/<rel_path>``. Used by
    ``LiftHandle.atomic_open_write`` / ``MediaHandle.atomic_open_write``
    on Android to bypass the Binder per-transaction size cap that
    blocks shipping a large body via ``atomic_commit_bytes``.

    The peer-side flow is two-phase: (1) write bytes to
    ``content://<auth>/<lang>/_atomic_pending/<token>`` via
    ``ContentResolver.openFileDescriptor`` — kernel FD, no IPC
    size limit; (2) call this endpoint with the token + final
    rel_path to atomic-rename under ``project_lock``.

    The lock guarantees the rename can't overlap a sync's merge-
    output write or another peer's atomic_commit; net effect is
    the same as the single-RPC ``atomic_commit_bytes`` for small
    payloads, just split so bytes don't have to fit in a Bundle.

    Returns ``ATOMIC_COMMITTED`` (with ``bytes_written`` and
    ``sha256``) on success. Missing-pending-file returns 404
    (peer probably forgot to write first); path-validation
    failures return 400; filesystem failures return 500."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    token = body.get('token') or ''
    rel = body.get('path') or ''
    # Token validation matches the ContentProvider's
    # ``_is_safe_pending_token`` — hex / underscore / hyphen, 1-64
    # chars. Reject anything else before any filesystem touch.
    import re
    if not isinstance(token, str) or not re.match(
            r'^[A-Za-z0-9_-]{1,64}$', token):
        return 400, {"ok": False, "error": "invalid_token"}
    final_target = _resolve_atomic_commit_path(p.working_dir, rel)
    if final_target is None:
        return 400, {"ok": False, "error": "path_rejected"}
    pending = os.path.join(p.working_dir, '.azt_atomic_pending', token)
    # Containment for the pending path — token is structurally clean
    # but defence-in-depth.
    pending_real = os.path.realpath(pending)
    base_real = os.path.realpath(p.working_dir)
    try:
        if os.path.commonpath([base_real, pending_real]) != base_real:
            return 400, {"ok": False, "error": "pending_path_rejected"}
    except ValueError:
        return 400, {"ok": False, "error": "pending_path_rejected"}
    if not os.path.isfile(pending_real):
        return 404, {"ok": False, "error": "pending_not_found"}
    _touch_project(langcode)
    # Stream the pending file through SHA-256 + a byte counter
    # rather than slurping it into RAM. A typical audio recording
    # is 3–10 MB — bounded, but on a low-memory device every
    # multi-MB allocation matters, especially when it can stack
    # with a concurrent LIFT merge or push pack-build. 64 KB
    # chunks keep peak heap at ~64 KB instead of the file size
    # (0.44.6+).
    try:
        h = hashlib.sha256()
        bytes_written = 0
        with open(pending_real, 'rb') as f:
            for chunk in iter(lambda: f.read(64 * 1024), b''):
                h.update(chunk)
                bytes_written += len(chunk)
    except OSError as ex:
        return 500, {"ok": False, "error": f"read_pending: {ex}"}
    try:
        with project_lock(p.working_dir):
            os.makedirs(os.path.dirname(final_target) or '.',
                        exist_ok=True)
            os.replace(pending_real, final_target)
    except Exception as ex:
        # Clean up pending file if the rename failed — don't leave
        # turds under .azt_atomic_pending/.
        try:
            if os.path.isfile(pending_real):
                os.unlink(pending_real)
        except OSError:
            pass
        return 500, {"ok": False, "error": str(ex)}
    # Auto-fire a debounced commit so the just-finalized bytes
    # always get absorbed into a git commit. Without this, a race
    # window exists: peer fires commit_project after save N-1, the
    # daemon's 500 ms debounce timer fires and starts the commit,
    # then save N's atomic_finalize lands AFTER the commit's
    # ``_stage_all`` but before the next peer-side commit_project
    # fires. The result is n_changes > 0 forever — the bytes are
    # on disk but no commit captures them. Field-observed
    # 2026-05-27: phone showed sticky ``+1 red`` (n_changes=1) for
    # the rest of the session after a recording cycle whose final
    # atomic_finalize raced with the in-flight commit.
    #
    # ``scheduler.commit_project`` is debounced + idempotent — if
    # the peer ALSO fires commit_project (the usual peer-side
    # contract), bursts of finalize-driven schedules coalesce with
    # the peer's call into a single commit run. Cheap (sets / resets
    # a 500 ms timer) and side-effect-free if there's nothing to
    # commit (the next ``_stage_all`` sees a clean index and the
    # job returns ``NOTHING_TO_COMMIT``).
    # Peer can suppress the auto-commit via ``commit_after=false``
    # (0.50.51) when they own the commit boundary themselves.
    if body.get('commit_after', True):
        try:
            scheduler.commit_project(langcode)
        except Exception as ex:
            print(f'[atomic_finalize] auto-commit schedule failed for '
                  f'{langcode!r}: {ex!r}',
                  file=sys.stderr, flush=True)
    res = Result().add(S.ATOMIC_COMMITTED,
                       bytes_written=bytes_written,
                       sha256=h.hexdigest())
    return 200, {"ok": True, "result": res.to_dict()}


def _match_cawl_image_path(path):
    """If ``path`` is ``/v1/projects/<lang>/cawl/images/<rel_path>``,
    return ``(langcode, rel_path)``. Else ``None``.

    ``rel_path`` may be a flat filename or a nested path (CAWL
    repos commonly nest images under category subdirs:
    ``0001_body/foo.png``). On the wire the rel-path is
    URL-encoded; this function URL-decodes each component
    before joining so ``cawl.get_image_path`` sees the same
    on-disk path the index emitted.

    Path-traversal rejection: ``..``/``.``/empty components are
    rejected here as belt-and-braces. ``cawl.get_image_path``
    also re-validates and does a ``commonpath`` containment
    check — defence-in-depth — but rejecting early keeps the
    matcher's contract clean."""
    if not path.startswith('/v1/projects/'):
        return None
    parts = path.split('/')
    # Minimum 7 segments: ['', 'v1', 'projects', '<lang>', 'cawl',
    # 'images', '<rel_path[0]>']. Nested paths add more.
    if len(parts) < 7:
        return None
    if parts[4] != 'cawl' or parts[5] != 'images':
        return None
    langcode = parts[3]
    if not langcode:
        return None
    from urllib.parse import unquote as _urlunquote
    rel_segments = []
    for seg in parts[6:]:
        if not seg:
            return None
        decoded = _urlunquote(seg)
        if decoded in ('.', '..') or '/' in decoded or '\\' in decoded:
            # Reject post-decode tricks (an encoded ``%2f`` would
            # decode to ``/`` and let a single segment become a
            # path; reject that). ``cawl.get_image_path``
            # catches this too but rejecting at the matcher
            # keeps the contract tight.
            return None
        rel_segments.append(decoded)
    if not rel_segments:
        return None
    rel_path = '/'.join(rel_segments)
    return langcode, rel_path


def dispatch(method, path, body):
    """Route a request. Returns ``(status: int, response: dict)``.

    Auth is the *caller's* responsibility — bearer-token check on HTTP,
    UID check on Android ContentProvider — except for paths in
    ``UNAUTHENTICATED_PATHS``, which any caller may use as a liveness
    probe.
    """
    if method == 'GET':
        if path == '/v1/health':
            return _h_health(body)
        if path == '/v1/online':
            return _h_online(body)
        if path == '/v1/credentials/status':
            return _h_credentials_status(body)
        if path == '/v1/config/contributor':
            return _h_get_contributor(body)
        if path == '/v1/config/device_name':
            return _h_get_device_name(body)
        if path == '/v1/credentials/github/install_url':
            return _h_github_install_url(body)
        if path == '/v1/credentials/github/client_id':
            return _h_github_client_id(body)
        if path.startswith('/v1/credentials/github/device_flow/'):
            parts = path.split('/')
            if len(parts) == 6 and parts[5]:
                return _h_github_device_flow_status(parts[5], body)
        if path == '/v1/recent/last_project':
            return _h_get_last_project(body)
        if path == '/v1/logging/daemon_log':
            return _h_get_daemon_log(body)
        if path == '/v1/logging/daemon_log_files':
            return _h_get_daemon_log_files(body)
        if path == '/v1/diagnostics/snapshot':
            return _h_get_diagnostic_snapshot(body)
        if path == '/v1/config/ui_language':
            return _h_get_ui_language(body)
        if path == '/v1/config/cawl_prefetch_all_variants':
            return _h_get_cawl_prefetch_all_variants(body)
        if path == '/v1/config/work_offline':
            return _h_get_work_offline(body)
        if path == '/v1/lan/peer_id':
            return _h_lan_peer_id(body)
        if path == '/v1/lan/peers':
            return _h_lan_list_peers(body)
        if path == '/v1/lan/toggle':
            return _h_lan_get_toggle(body)
        if path == '/v1/lan/pending':
            return _h_lan_pending(body)
        if path == '/v1/lan/nearby_unpaired':
            return _h_lan_nearby_unpaired(body)
        if path == '/v1/projects':
            return _h_list_projects(body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 4 and parts[3]:
                return _h_get_project(parts[3], body)
            if len(parts) == 5 and parts[4] == 'status':
                return _h_project_status(parts[3], body)
            if len(parts) == 6 and parts[4] == 'cawl' \
                    and parts[5] == 'index':
                return _h_cawl_index(parts[3], body)
            if len(parts) == 6 and parts[4] == 'cawl' \
                    and parts[5] == 'cache_status':
                return _h_cawl_cache_status(parts[3], body)
            if len(parts) == 5 and parts[4] == 'lan_debug':
                return _h_lan_debug(parts[3], body)
            if len(parts) == 5 and parts[4] == 'kv':
                return _h_project_kv_list(parts[3], body)
            if len(parts) == 6 and parts[4] == 'kv':
                return _h_project_kv_get(parts[3], parts[5], body)
            if len(parts) == 5 and parts[4] == 'slots':
                return _h_project_slots_list(parts[3], body)
        if path.startswith('/v1/jobs/'):
            parts = path.split('/')
            if len(parts) == 4 and parts[3]:
                return _h_get_job(parts[3], body)
        return 404, {"ok": False, "error": "not_found"}

    if method == 'POST':
        if path == '/v1/credentials/host':
            return _h_set_host(body)
        if path == '/v1/config/contributor':
            return _h_set_contributor(body)
        if path == '/v1/config/device_name':
            return _h_set_device_name(body)
        if path == '/v1/config/cawl_prefetch_all_variants':
            return _h_set_cawl_prefetch_all_variants(body)
        if path == '/v1/config/work_offline':
            return _h_set_work_offline(body)
        if path == '/v1/lan/pair/qr':
            return _h_lan_pair_qr(body)
        if path == '/v1/lan/pair/qr/keepalive':
            return _h_lan_qr_keepalive(body)
        if path == '/v1/lan/pair/qr/close':
            return _h_lan_qr_close(body)
        if path == '/v1/lan/pair/accept':
            return _h_lan_pair_accept(body)
        if path == '/v1/lan/unshare_project':
            return _h_lan_unshare_project(body)
        if path == '/v1/lan/unpair':
            return _h_lan_unpair(body)
        if path == '/v1/lan/toggle':
            return _h_lan_set_toggle(body)
        if path == '/v1/lan/burst':
            return _h_lan_burst(body)
        if path == '/v1/lan/static_endpoints':
            return _h_lan_set_static_endpoints(body)
        if path == '/v1/lan/clone':
            return _h_lan_clone(body)
        if path == '/v1/lan/accept_offer':
            return _h_lan_accept_offer(body)
        if path == '/v1/lan/decline_offer':
            return _h_lan_decline_offer(body)
        if path == '/v1/lan/adopt_origin':
            return _h_lan_adopt_origin(body)
        if path == '/v1/lan/resolve_conflict':
            return _h_lan_resolve_conflict(body)
        if path == '/v1/lan/send_share_offer':
            return _h_lan_send_share_offer(body)
        if path == '/v1/lan/pair_request_send':
            return _h_lan_pair_request_send(body)
        if path == '/v1/lan/pair_request_resolve':
            return _h_lan_pair_request_resolve(body)
        if path == '/v1/lan/pair_request_status':
            return _h_lan_pair_request_status(body)
        if path == '/v1/credentials/github/device_flow/start':
            return _h_github_device_flow_start(body)
        if path == '/v1/credentials/github/tokens':
            return _h_set_github_tokens(body)
        if path == '/v1/credentials/github/app_installed':
            return _h_set_github_app_installed(body)
        if path == '/v1/credentials/gitlab':
            return _h_set_gitlab(body)
        if path == '/v1/credentials/gitlab/test':
            return _h_test_gitlab(body)
        if path == '/v1/credentials/github/test':
            return _h_test_github(body)
        if path == '/v1/credentials/migrate_from_prefs':
            return _h_migrate_from_prefs(body)
        if path == '/v1/logging/daemon_log_to_file':
            return _h_set_daemon_log_to_file(body)
        if path == '/v1/logging/append':
            return _h_append_log(body)
        if path == '/v1/diagnostics/prepare_share_bundle':
            return _h_prepare_share_bundle(body)
        if path == '/v1/admin/restart':
            return _h_admin_restart(body)
        if path == '/v1/sync/nudge':
            return _h_sync_nudge(body)
        if path == '/v1/recent/last_project':
            return _h_set_last_project(body)
        if path == '/v1/projects/register':
            return _h_register_project(body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 5 and parts[4] == 'rename':
                return _h_rename_project(parts[3], body)
        if path == '/v1/projects/derive_langcode':
            return _h_derive_langcode(body)
        if path == '/v1/projects/init':
            return _h_init_project(body)
        if path == '/v1/projects/from_template':
            return _h_create_project_from_template(body)
        if path == '/v1/projects/clone':
            return _h_clone_project(body)
        if path.startswith('/v1/projects/clone/'):
            parts = path.split('/')
            if len(parts) == 5 and parts[4]:
                return _h_clone_status(parts[4], body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 5 and parts[4] == 'sync':
                return _h_project_sync(parts[3], body)
            if len(parts) == 5 and parts[4] == 'sync_async':
                # 0.43.0: ``sync_async`` was renamed to ``commit``
                # (commit-only — push moved to the daemon's drain
                # loop). Old peers keep working as long as they're
                # at MIN_CLIENT_VERSION or above; below that the
                # bootstrap install-update popup forces a rebuild.
                return _h_project_commit(parts[3], body)
            if len(parts) == 5 and parts[4] == 'commit':
                return _h_project_commit(parts[3], body)
            if len(parts) == 5 and parts[4] == 'atomic_commit':
                return _h_project_atomic_commit(parts[3], body)
            if len(parts) == 5 and parts[4] == 'atomic_finalize':
                return _h_project_atomic_finalize(parts[3], body)
            if len(parts) == 5 and parts[4] == 'submit_file':
                return _h_project_submit_file(parts[3], body)
            if len(parts) == 5 and parts[4] == 'set_audio':
                return _h_set_audio(parts[3], body)
            if len(parts) == 5 and parts[4] == 'set_illustration':
                return _h_set_illustration(parts[3], body)
            if len(parts) == 6 and parts[4] == 'cawl' \
                    and parts[5] == 'prefetch':
                return _h_cawl_prefetch(parts[3], body)
            if len(parts) == 5 and parts[4] == 'last_sync':
                return _h_set_project_last_sync(parts[3], body)
            if len(parts) == 5 and parts[4] == 'collaborators':
                return _h_grant_collaborator(parts[3], body)
            if len(parts) == 5 and parts[4] == 'cawl_image_repo':
                return _h_set_cawl_image_repo(parts[3], body)
            if len(parts) == 5 and parts[4] == 'repo_slug':
                return _h_set_repo_slug(parts[3], body)
            if len(parts) == 6 and parts[4] == 'kv':
                return _h_project_kv_set(parts[3], parts[5], body)
            if len(parts) == 6 and parts[4] == 'slots' \
                    and parts[5] == 'claim':
                return _h_project_slot_claim(parts[3], body)
            if len(parts) == 6 and parts[4] == 'slots' \
                    and parts[5] == 'release':
                return _h_project_slot_release(parts[3], body)
            if len(parts) == 7 and parts[4] == 'slots' \
                    and parts[6] == 'rebind':
                return _h_project_slot_rebind(
                    parts[3], parts[5], body)
        return 404, {"ok": False, "error": "not_found"}

    return 405, {"ok": False, "error": "method_not_allowed"}


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"azt_collabd/{_VERSION}"
    _token: str = ""   # populated by run()

    def log_message(self, fmt, *args):
        pass

    def _auth_ok(self):
        hdr = self.headers.get('Authorization', '')
        prefix = 'Bearer '
        if not hdr.startswith(prefix):
            return False
        return secrets.compare_digest(hdr[len(prefix):], type(self)._token)

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, status, content_type, data):
        """Binary response. Used by the CAWL-image endpoint; the
        dispatch table is JSON-only so this is the only non-JSON
        path in the server. Adopted to avoid base64-wrapping
        image payloads, which would inflate them ~1.33× for no
        useful purpose."""
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # Defensive cap on loopback HTTP body size. Loopback is the
    # desktop transport (Android peers use ContentProvider); the
    # largest legitimate body on this path is a credential blob
    # (<1 KB) or a project-config write (<10 KB). 64 MB is far
    # past anything legit while preventing accidental DoS from a
    # buggy desktop peer / test harness that sends a multi-GB
    # Content-Length and OOMs the daemon during the
    # ``self.rfile.read(n)`` allocation.
    _MAX_BODY_BYTES = 64 * 1024 * 1024

    def _read_json(self):
        n = int(self.headers.get('Content-Length', '0') or '0')
        if n <= 0:
            return {}
        if n > self._MAX_BODY_BYTES:
            print(f'[server] rejecting request body of {n} bytes '
                  f'(cap {self._MAX_BODY_BYTES})',
                  file=sys.stderr, flush=True)
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return None

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except Exception as ex:
            _record_crash(ex, where='handle_one_request')
            raise

    def _serve(self, method, body):
        if self.path not in UNAUTHENTICATED_PATHS and not self._auth_ok():
            return self._send_json(401,
                                   {"ok": False, "error": "unauthorized"})
        status, response = dispatch(method, self.path, body)
        self._send_json(status, response)

    def do_GET(self):
        # Binary endpoint: route directly so we can stream raw
        # image bytes (the dispatch table is JSON-only).
        binary_match = _match_cawl_image_path(self.path)
        if binary_match is not None:
            if not self._auth_ok():
                return self._send_json(
                    401, {"ok": False, "error": "unauthorized"})
            langcode, rel_path = binary_match
            try:
                status, content_type, data = _h_cawl_image_bytes(
                    langcode, rel_path)
            except Exception as ex:
                return self._send_json(
                    500, {"ok": False, "error": str(ex)})
            return self._send_bytes(status, content_type, data)
        self._serve('GET', None)

    def do_POST(self):
        body = self._read_json()
        if body is None:
            return self._send_json(400,
                                   {"ok": False, "error": "bad_json"})
        self._serve('POST', body)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn,
                           http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def daemon_log_path():
    """Absolute path to **today's** daemon log file (since 0.52.5).
    Used by the picker / settings-UI Share buttons to attach the
    current-day log, and by the read RPC to serve the recent tail.

    Daily-rotation naming: ``daemon-<tag>-YYYY-MM-DD.log`` (or
    ``daemon-YYYY-MM-DD.log`` when the peer_id tag isn't readable
    yet on a bootstrap-fresh install). The date is the local-clock
    date at call time — at 23:59:59 this returns yesterday's path
    and at 00:00:00 it returns today's, which is the same property
    ``_StdioTee`` uses to rotate lazily on the first write past
    midnight.

    Pre-0.52.5 this returned a single ``daemon-<tag>.log`` that
    grew without bound across daemon respawns; the field log
    showed 40+ MB files spanning weeks. The per-day filename plus
    ``settings.log_retention_days()`` together bound the on-disk
    cost to ``retention × ~daily-volume``."""
    return _daemon_log_path_for(_today_str())


def _today_str():
    return _time.strftime('%Y-%m-%d')


def _daemon_log_path_for(date_str):
    """Path to the daemon log file for a specific YYYY-MM-DD date.
    Used by ``daemon_log_path()`` for today and by retention /
    multi-day-read helpers for other days. Same filename shape
    in both cases so the matching glob / parsing logic in
    ``_iter_daemon_log_files`` is single-source.

    Suffix is ``_log.txt`` (since 0.52.20) rather than the
    pre-0.52.20 ``.log`` so that text editors with extension-
    based syntax detection treat the file as text. Reception-
    side this matters when a support engineer opens the file
    from the diagnostic zip — some editors (e.g. Sublime,
    several Android text viewers) skip ``.log`` files entirely
    or treat them as binary. ``_log.txt`` keeps the "log"
    signal in the basename for grep / triage while making the
    actual extension ``.txt``."""
    tag = _log_peer_tag_str()
    if tag:
        name = f'daemon-{tag}-{date_str}_log.txt'
    else:
        name = f'daemon-{date_str}_log.txt'
    return os.path.join(azt_home(), name)


# Matches both the 0.52.20+ ``_log.txt`` suffix and the
# pre-0.52.20 ``.log`` suffix so retention sweeps stranded
# files from an upgrade without needing a one-shot migration
# pass. Each form independently captures the date in group(1).
_DAEMON_LOG_DATE_RE = re.compile(
    r'^daemon-(?:[0-9a-f]{8}-)?(\d{4}-\d{2}-\d{2})'
    r'(?:_log\.txt|\.log)$')


def _iter_daemon_log_files():
    """Yield ``(date_str, abspath)`` for every per-day daemon log
    in ``$AZT_HOME``, sorted oldest-first. Used by retention
    pruning and by the (Phase 2) multi-day read RPC.

    Matches both tagged (``daemon-<8hex>-<date>_log.txt``) and
    untagged (``daemon-<date>_log.txt``) forms, plus the
    pre-0.52.20 ``.log`` extension for upgrade continuity.
    Other files in ``$AZT_HOME`` are skipped — the regex anchors
    on the full basename so a stray ``daemon-foo.log`` from an
    earlier release doesn't get picked up by retention."""
    home = azt_home()
    try:
        names = os.listdir(home)
    except OSError:
        return
    matches = []
    for name in names:
        m = _DAEMON_LOG_DATE_RE.match(name)
        if m:
            matches.append((m.group(1), os.path.join(home, name)))
    matches.sort()
    for date_str, path in matches:
        yield date_str, path


def _prune_daemon_log_retention():
    """Delete daily log files older than
    ``settings.log_retention_days()``. Called on every tee install
    (cheap — one ``listdir`` + bounded ``unlink``) and lazily after
    each midnight rotation inside ``_StdioTee``.

    Today's file is never pruned regardless of how the math works
    out — the retention setter is min-clamped to 1, but the
    explicit ``date_str == today`` guard belt-and-suspenders that
    so a clock-skew edge case can't wipe the live file."""
    try:
        retention = _settings.log_retention_days()
    except Exception:
        retention = 3
    today = _today_str()
    keep = set()
    # Build the keep set forward from today so a local-clock
    # change doesn't accidentally widen retention; we always keep
    # the last `retention` distinct on-disk dates rather than a
    # window relative to today.
    import datetime as _dt
    try:
        today_d = _dt.date.fromisoformat(today)
    except ValueError:
        return
    for k in range(retention):
        keep.add((today_d - _dt.timedelta(days=k)).isoformat())
    for date_str, path in _iter_daemon_log_files():
        if date_str == today:
            continue
        if date_str in keep:
            continue
        try:
            os.unlink(path)
            print(f'[daemon-log] retention prune: {path!r} '
                  f'(date={date_str}, keep={sorted(keep)})',
                  file=sys.__stderr__, flush=True)
        except OSError as ex:
            print(f'[daemon-log] retention prune failed for '
                  f'{path!r}: {ex}',
                  file=sys.__stderr__, flush=True)


# Cache: the first 8 hex chars of the daemon's ed25519 peer_id,
# spliced into every log line stamp so a tester comparing two
# phones' logs can tell them apart at a glance. Same prefix the
# user already sees on per-peer lines (``[lan-push] '07c089f2'
# ...``), so no new vocabulary. Sentinel ``None`` = not yet
# attempted; ``''`` = attempted and unavailable (peer_id couldn't
# be read — log without a tag rather than retry on every write).
_log_peer_tag = None


def _log_peer_tag_str():
    """Return the short peer-id tag to splice into log line stamps.

    Lazily resolved on first call: reading the peer_id triggers
    file I/O (and ed25519 cert generation on a fresh install),
    neither of which we want to do at module import. After the
    first call the result is cached for the life of the process,
    so subsequent log writes pay nothing.

    A read failure (cryptography missing during bootstrap, file
    locked, etc.) caches the empty string so we don't retry on
    every write — log lines then look unchanged from the pre-
    0.45.25 format.
    """
    global _log_peer_tag
    if _log_peer_tag is not None:
        return _log_peer_tag
    try:
        from azt_collabd import peer_id as _peer_id
        hex_str = _peer_id.peer_id_hex() or ''
        _log_peer_tag = hex_str[:8] if hex_str else ''
    except Exception:
        _log_peer_tag = ''
    return _log_peer_tag


class _LogSession:
    """Shared writer behind both ``_StdioTee`` instances. Owns the
    open file handle for *today's* log and rotates lazily when the
    local-clock date changes (since 0.52.5).

    Rotation is checked on every write rather than on a wall-clock
    timer because the daemon can sit idle for hours at a time —
    a timer would fire midnight rotation only if the process was
    awake to receive it. Lazy check makes the rotation cost zero
    when nothing's being logged and a single ``stat``-equivalent
    cost (``_time.strftime``) per write batch otherwise. Concurrent
    writes from stdout/stderr tees serialise through the lock so
    two threads can't both observe a date change and both rotate.

    The session also serves as the shared SOL (start-of-line) flag
    used by the per-line stamp prefix — same role as the ``_sol_state``
    list pre-0.52.5, now stored as a single attribute since the
    session is the natural place for "next byte begins a new line"
    state."""

    def __init__(self):
        # Reentrant lock so the post-rotation start-of-day
        # diagnostic dump (which calls back into ``print`` →
        # ``_StdioTee.write`` → ``_LogSession.write``) can run
        # while ``_rotate_locked`` still holds the lock without
        # deadlocking. Pre-0.52.7 there was no dump call inside
        # the lock and a plain ``threading.Lock`` sufficed.
        self._lock = threading.RLock()
        self._current_date = None
        self._file = None
        self._sol = True

    def open(self):
        """Open today's log file. Idempotent — multiple calls
        across install paths just re-target the current handle."""
        with self._lock:
            date_str = _today_str()
            path = _daemon_log_path_for(date_str)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError as ex:
                print(f'[daemon-log] cannot mkdir for {path!r}: {ex}',
                      file=sys.__stderr__, flush=True)
                return False
            try:
                fh = open(path, 'a', buffering=1, encoding='utf-8',
                          errors='replace')
            except OSError as ex:
                print(f'[daemon-log] cannot open {path!r}: {ex}',
                      file=sys.__stderr__, flush=True)
                return False
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._file = fh
            self._current_date = date_str
            self._sol = True
            return True

    def close(self):
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._file = None
            self._current_date = None
            self._sol = True

    def current_path(self):
        with self._lock:
            if self._current_date is None:
                return None
            return _daemon_log_path_for(self._current_date)

    def write(self, data):
        """Write a possibly-multi-line text payload to the session
        file with per-line stamp prefix
        ``[YYYY-MM-DD HH:MM:SS,mmm <peer_id_short>] `` (since
        0.52.5; pre-0.52.5 was ``[HH:MM:SS <tag>] ``). Rotates the
        file if the local-clock date changed since the last
        write."""
        if not data:
            return
        with self._lock:
            if self._file is None:
                # Tee uninstall in flight or open() failed; drop.
                return
            today = _today_str()
            if today != self._current_date:
                self._rotate_locked(today)
            if not isinstance(data, str):
                try:
                    self._file.write(data)
                except Exception:
                    pass
                return
            stamp = _format_log_stamp()
            pieces = data.split('\n')
            at_sol = self._sol
            out = []
            for k, piece in enumerate(pieces):
                has_newline_after = k < len(pieces) - 1
                if at_sol and piece:
                    out.append(stamp)
                out.append(piece)
                if has_newline_after:
                    out.append('\n')
                    at_sol = True
                elif piece:
                    at_sol = False
            self._sol = at_sol
            try:
                self._file.write(''.join(out))
            except Exception:
                pass

    def _rotate_locked(self, new_date):
        """Caller holds ``self._lock``. Close today's file, open
        tomorrow's, and prune older-than-retention files. Logged
        on both sides of the rotation so a tester reading the
        bundle sees the cutover explicitly."""
        old_date = self._current_date
        try:
            self._file.write(
                f'[daemon-log] rotating: {old_date!r} → {new_date!r}\n')
        except Exception:
            pass
        try:
            self._file.close()
        except Exception:
            pass
        new_path = _daemon_log_path_for(new_date)
        try:
            self._file = open(new_path, 'a', buffering=1,
                              encoding='utf-8', errors='replace')
            self._current_date = new_date
            self._sol = True
            self._file.write(
                f'[daemon-log] rotated from {old_date!r}\n')
        except OSError as ex:
            print(f'[daemon-log] rotation open failed '
                  f'({new_path!r}): {ex}',
                  file=sys.__stderr__, flush=True)
            self._file = None
            self._current_date = None
            self._sol = True
        # Retention prune off the lock would race a concurrent
        # write attempting to rotate; doing it here under the lock
        # is fine since prune is bounded ``listdir`` + a few
        # ``unlink``s.
        try:
            _prune_daemon_log_retention()
        except Exception as ex:
            print(f'[daemon-log] post-rotation prune raised: {ex!r}',
                  file=sys.__stderr__, flush=True)
        # Anchor the fresh day's file with a start-of-day
        # diagnostic snapshot (same content the install path
        # emits for a fresh-of-day install). Lock is reentrant
        # so the dump's ``print`` calls re-acquire safely.
        try:
            _dump_lan_debug_snapshot()
        except Exception as ex:
            print(f'[lan-debug-dump] post-rotation snapshot '
                  f'raised: {ex!r}',
                  file=sys.__stderr__, flush=True)

    def flush(self):
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                except Exception:
                    pass


def _format_log_stamp():
    """Format the per-line stamp prefix:
    ``[YYYY-MM-DD HH:MM:SS,mmm <peer_id_short>] ``. Matches Python
    ``logging``'s default ``,mmm`` for the fractional second so a
    grep across recorder + daemon logs is one expression.

    The tag is appended when available; absent on a bootstrap-
    fresh install before peer_id is generated, in which case the
    stamp falls back to ``[YYYY-MM-DD HH:MM:SS,mmm] ``."""
    now = _time.time()
    ms = int((now - int(now)) * 1000)
    base = _time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime(now))
    tag = _log_peer_tag_str()
    if tag:
        return f'[{base},{ms:03d} {tag}] '
    return f'[{base},{ms:03d}] '


# Process-wide shared log session. Both stdout and stderr tees
# delegate writes here so they target the same file with one
# consistent SOL state.
_log_session = _LogSession()


class _StdioTee:
    """File-like that mirrors writes to the original stream (logcat /
    terminal) AND to the shared ``_LogSession`` (since 0.52.5;
    pre-0.52.5 the tee held its own file handle and the SOL flag
    was a shared list — the session encapsulates both now to make
    lazy midnight rotation safe across the stdout+stderr pair).

    Boot-trace prints (``print(f'[boot-trace-daemon] phase=…',
    flush=True)`` in ``service.py`` and ``server.py``) go to
    ``sys.stdout``; the bulk of structured diagnostics
    (``[recent] _touch_project``, ``[cawl]``, ``[commit-*]``) use
    ``print(..., file=sys.stderr)``. Both ends route through one
    session so a tester sharing the file gets ordered output
    regardless of which stream emitted any given line.
    """

    def __init__(self, original, session):
        self._orig = original
        self._session = session

    def write(self, data):
        try:
            self._orig.write(data)
        except Exception:
            pass
        try:
            self._session.write(data)
        except Exception:
            pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass
        try:
            self._session.flush()
        except Exception:
            pass

    # Pass-through attributes some libraries probe for (isatty, etc.)
    def __getattr__(self, name):
        return getattr(self._orig, name)


# Module-level state for hot-toggle. Lets the daemon install /
# remove the tee without a process restart in response to the
# settings-UI toggle.
_stdio_tee_installed = False
_stdio_tee_stdout_original = None
_stdio_tee_stderr_original = None


def install_stdio_tee(truncate=False):
    """Begin mirroring BOTH ``sys.stdout`` and ``sys.stderr`` to
    today's daemon log file. Idempotent: a second call while a
    tee is already installed is a no-op.

    Since 0.52.5 the file is per-day
    (``daemon-<tag>-YYYY-MM-DD.log``) and the underlying
    ``_LogSession`` rotates lazily on the first write past
    midnight, so this function no longer needs an explicit "fresh
    session" rotation step. The *truncate* parameter is kept for
    backwards compatibility with the existing toggle-on RPC but
    is now effectively informational — the only behaviour
    difference is the banner line written on install (``fresh
    session`` vs ``respawn``). Pre-0.52.5 ``truncate=True`` moved
    the previous file to ``<path>.prev``; that mechanism is
    obsolete now that per-day rotation bounds file growth by
    retention.

    Retention pruning runs on every install — cheap (one
    ``listdir`` + bounded ``unlink``) and the right place to
    catch stale files left behind by a release that bumped the
    retention window down.

    Safe to call from outside the daemon's main thread — replaces
    the global ``sys.stdout`` and ``sys.stderr`` references, and
    concurrent writes end up on the original-stream branch of the
    tee (best-effort write-through) until the swap completes."""
    global _stdio_tee_installed, _stdio_tee_stdout_original
    global _stdio_tee_stderr_original
    if _stdio_tee_installed:
        return True
    # Capture today's file pre-existing size BEFORE the open so
    # we can tell "fresh-of-day file (fire start-of-day snapshot)"
    # apart from "respawn-within-day appending to existing file
    # (don't repeat the snapshot)". A missing file is treated as
    # size 0 — same outcome.
    pre_path = _daemon_log_path_for(_today_str())
    try:
        pre_size = os.path.getsize(pre_path)
    except OSError:
        pre_size = 0
    if not _log_session.open():
        return False
    try:
        _prune_daemon_log_retention()
    except Exception as ex:
        print(f'[daemon-log] retention prune raised on install: '
              f'{ex!r}',
              file=sys.__stderr__, flush=True)
    _stdio_tee_stdout_original = sys.stdout
    _stdio_tee_stderr_original = sys.stderr
    sys.stdout = _StdioTee(_stdio_tee_stdout_original, _log_session)
    sys.stderr = _StdioTee(_stdio_tee_stderr_original, _log_session)
    _stdio_tee_installed = True
    path = _log_session.current_path() or daemon_log_path()
    print(f'[daemon-log] mirroring stdio to {path!r} '
          f'(daemon {_VERSION} fingerprint={_FINGERPRINT})',
          file=sys.stderr, flush=True)
    if pre_size == 0:
        # First write into today's file. Anchor the day with a
        # start-of-day diagnostic snapshot so a tester reading
        # the bundle top-to-bottom has the baseline state. Pre-
        # 0.52.7 this fired only from the (now-removed) toggle-on
        # gesture; with always-on logging the equivalent moment
        # is "we just opened a fresh per-day file."
        try:
            _dump_lan_debug_snapshot()
        except Exception as ex:
            print(f'[lan-debug-dump] snapshot raised: {ex!r}',
                  file=sys.stderr, flush=True)
    return True


def uninstall_stdio_tee():
    """Stop mirroring stdio to the daemon log file. Restores the
    original ``sys.stdout`` and ``sys.stderr``. Idempotent."""
    global _stdio_tee_installed, _stdio_tee_stdout_original
    global _stdio_tee_stderr_original
    if not _stdio_tee_installed:
        return
    print('[daemon-log] stopping stdio mirror',
          file=sys.stderr, flush=True)
    sys.stdout = _stdio_tee_stdout_original
    sys.stderr = _stdio_tee_stderr_original
    _log_session.close()
    _stdio_tee_stdout_original = None
    _stdio_tee_stderr_original = None
    _stdio_tee_installed = False


def maybe_install_stdio_tee():
    """Called at daemon process startup (loopback ``run()`` on
    desktop, and ``server_apk/service.py`` on Android). Installs
    the tee unconditionally since 0.52.7 — logging is always-on
    (per-day rotation + 3-day retention bound disk cost). Failure
    to open the log file (read-only filesystem, disk full,
    permissions) is swallowed: the daemon still runs, the
    original stdout / stderr still carry diagnostics in the
    parent process / logcat, only the on-disk capture is missing.

    The function name is retained for ABI compatibility with
    ``server_apk/service.py`` and out-of-tree desktop launchers
    that call it by name; the ``maybe_`` prefix is now historical
    (always-on means "yes, install"). Public (no leading
    underscore) for the same reason."""
    try:
        install_stdio_tee()
    except Exception as ex:
        print(f'[daemon-log] install_stdio_tee raised on '
              f'startup: {ex!r}',
              file=sys.__stderr__, flush=True)


def run(host='127.0.0.1', port=0):
    """Start the server. Blocks until interrupted. Writes server.json on
    bind and removes it on shutdown. Exits non-zero if another
    azt_collabd is already running against the same $AZT_HOME."""
    global _server_lock_fd
    home = azt_home()
    os.makedirs(home, exist_ok=True)

    # Install the daemon-log-to-file tee before anything else so
    # we capture the boot trace. Idempotent and silent when the
    # toggle is off.
    maybe_install_stdio_tee()

    # Single-instance guard — flock on $AZT_HOME/server.lock
    lock_path = os.path.join(home, 'server.lock')
    _server_lock_fd = _acquire_server_lock(lock_path)
    if _server_lock_fd is None:
        print(f'[azt_collabd] another instance already holds '
              f'{lock_path}', flush=True)
        sys.exit(1)

    token = secrets.token_urlsafe(32)
    _Handler._token = token
    httpd = _ThreadingHTTPServer((host, port), _Handler)
    bound_port = httpd.server_address[1]
    info = {
        "port": bound_port,
        "token": token,
        "pid": os.getpid(),
        "version": _VERSION,
    }
    info_path = server_info_path()
    fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(info, f)
    print(f'[azt_collabd] listening on {host}:{bound_port} '
          f'(home={home})', flush=True)
    _record_started()

    # Crash hook for any unhandled exception that escapes a request handler
    # or the watcher thread.
    def _excepthook(exc_type, exc, tb):
        try:
            _record_crash(exc, where='excepthook')
        finally:
            sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    # Mark any in-flight jobs left over from a previous daemon process
    # (kill -9, OOM, container restart) as JOB_INTERRUPTED so peers
    # polling on stale job_ids get a typed transient-failure result.
    # Must run BEFORE start_watcher so the watcher sees a consistent
    # job table.
    scheduler.reconcile_on_startup()

    # One-shot retroactive cleanup for the pre-0.50.52 Publish bug.
    # Strips ``.git/config`` origin when the registry has no URL — a
    # mismatch fingerprint that's unreachable from any post-0.50.52
    # code path, so it can only be left over from an older daemon's
    # silent publish failure. After this runs, the picker's
    # publish-row gate sees both sides empty and shows the Publish
    # button so the user can re-click. See
    # ``docs/Publish_errors.md``.
    try:
        from . import repo as _repo
        _repo.reconcile_publish_state_on_startup()
    except Exception as ex:
        print(f'[azt_collabd] reconcile_publish_state failed: '
              f'{ex!r}', file=sys.stderr, flush=True)

    # Boot-time diagnostic snapshot + orphan-working-dir auto-repair.
    # Logs the picker's Share-diagnostics text into the daemon log
    # (so every startup leaves a snapshot trail) and re-registers
    # any subdir of $AZT_HOME that looks like a project (has .git
    # + LIFT) but isn't keyed in projects.json. Repair is one-way:
    # never removes entries, never alters working-tree contents.
    # Belongs after reconcile so the post-reconcile registry state
    # is what gets snapshotted.
    try:
        from . import repo as _repo
        _repo.diagnose_and_repair_registry_on_startup()
    except Exception as ex:
        print(f'[azt_collabd] diagnose_and_repair_registry failed: '
              f'{ex!r}', file=sys.stderr, flush=True)

    # Start the connectivity watcher so projects with pending_push get
    # drained on offline→online transitions.
    scheduler.start_watcher()

    # Auto-start the LAN listener if the persisted toggle is on.
    # ``lan.allow_sync`` survives a daemon restart in config.json
    # but the listener thread / WifiLock / FGS state don't, so
    # without this reconciliation a daemon respawn would leave us
    # in the "toggle says yes, listener says no" split-brain state
    # — paired peers' fan-out would silently fail with no endpoint
    # to bind to. Idempotent: ``apply_toggle`` is a no-op when the
    # listener's already running.
    try:
        _lan_listener.apply_toggle()
    except Exception as ex:
        print(f'[azt_collabd] lan_listener startup apply failed: '
              f'{ex!r}', file=sys.stderr, flush=True)

    def _graceful(signum, frame):
        print(f'[azt_collabd] signal {signum}, shutting down', flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True,
                         name='httpd-shutdown').start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('[azt_collabd] interrupted', flush=True)
    finally:
        scheduler.stop_watcher()
        try:
            os.remove(info_path)
        except OSError:
            pass
        httpd.server_close()
