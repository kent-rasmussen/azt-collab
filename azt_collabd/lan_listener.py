"""
LAN sync HTTPS listener (parked design in
``docs/local_lan_sync_stub.md``, phase 4).

When the daemon-wide ``lan.allow_sync`` toggle is on, ``apply_toggle()``
spins up a threaded ``dulwich.web``-backed HTTPS server bound to
``0.0.0.0:0`` (OS-assigned port). When it's off, ``apply_toggle()``
tears the server down. Hot-applied — flipping the toggle does NOT
require a daemon restart, per ``feedback_hot_toggle_not_restart``.

Auth model (per the parked spec):

  - TLS server cert is the daemon's per-device ``peer.crt`` (loaded
    from ``azt_collabd.peer_id``).
  - Client cert is *required* (``ctx.verify_mode = CERT_REQUIRED``)
    but its CA chain is *not* validated — we pin per-peer via the
    paired-peers list. The ``set_verify`` callback accepts every
    cert; the WSGI middleware then extracts the ed25519 pubkey
    from the DER and looks it up in ``peers.json``. Unknown peer →
    403. Known peer + fingerprint mismatch → 403 (logged so the
    user can see something's off).

Listener body uses ``dulwich.web.HTTPGitApplication`` wrapped in the
standard ``GunzipFilter`` + ``LimitedInputFilter`` chain via
``make_wsgi_chain``. The ``DictBackend`` exposes one ``/{lang}.git``
path per project that's shared with at least one peer; per-request,
the middleware further filters that set down to projects shared
with the specific peer-id making the request.

Concurrency: ``ThreadingMixIn`` so two paired phones in the same
room can fetch simultaneously. Per-project write serialization
comes from the existing ``azt_collabd.locks.project_lock`` flock,
which receive-pack callers acquire at the daemon entry point.

Lifetime: the listener thread is a daemon thread and stops cleanly
on ``stop()``. On Android, the parent ``:provider`` service runs
``startForeground(specialUse)`` while the listener is up — that
plumbing lives in ``azt_collabd.android_cp.service`` (phase 4
Android-side, not yet wired) and is a no-op on desktop.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
import time as _time

from . import lan_discovery as _lan_discovery
from . import paths as _paths
from . import peer_id as _peer_id
from . import peers as _peers
from . import projects as _projects
from . import settings as _settings
from . import store as _store


_LOCK = threading.Lock()
_STATE = {
    'server': None,
    'thread': None,
    'bound': None,  # (host, port) once running
    # Last failing step + detail from ``apply_toggle`` ('' when the
    # last attempt succeeded). Surfaced typed on the toggle RPC so the
    # settings line can NAME the failing seam instead of telling the
    # user to go read a log (0.54.75).
    'bind_error': '',
}


# In-memory tracker for "user just displayed a QR offering this
# langcode" gestures. Indexed by langcode → unix timestamp of the
# most-recent display. ``_handle_hello_bodyauth`` consults this
# before auto-sharing a langcode that arrives in an unpaired peer's
# hello.
#
# Why: without this gate, an attacker on the LAN can POST
# ``/v1/lan/hello`` with peer_id=any, fp=any, langcode=X and our
# daemon would (a) record them as paired and (b) add X to their
# shared_projects allowlist — at which point the dulwich smart-
# protocol handler accepts ``GET /X.git/info/refs`` from them and
# they can exfiltrate the project. The CERT_NONE TLS design
# intentionally can't pin client certs (stdlib ssl limitation, see
# ``_build_server``), so the only binding we have is "the user
# gestured by showing a QR for X within the last few minutes."
# The QR display is the user-consent signal; if no recent QR for
# this langcode exists, the hello records the pair but refuses
# auto-share.
#
# Validity is driven by the QR actually being ON SCREEN, not a blind
# timer (0.52.26). The share-QR popup heartbeats ``record_qr_offered``
# every ~10 s while displayed and calls ``clear_qr_offer`` when it
# closes. So this keepalive window only has to outlast one heartbeat
# interval — it is NOT a guess at "how long the user might keep the QR
# up" (the old 10-minute TTL). Consequences: a display that's closed
# (or whose app is killed / backgrounded) self-expires within seconds
# instead of staying armed for 10 minutes, and a QR the user
# deliberately keeps up stays valid for as long as it's shown.
_QR_OFFER_KEEPALIVE_S = 30.0
_pending_qr_offers = {}   # langcode (str) → last-heartbeat unix ts

# Reverse-delivery gate (0.55.13). ``_reverse_deliver`` fires from the
# git routes, which a fetching peer hits several times per exchange —
# and each delivery dials the peer, whose own reverse delivery dials
# back. Ungated (0.55.10–0.55.12) that is a mutual amplifier: ~330
# threads in ~10 s and a dead ``:provider`` process, every delivery a
# no-op. One look per contact burst is all convergence needs.
# Per-request ACL context (0.55.47). ``open_repository`` is a backend
# method with no access to the WSGI ``environ``, so the middleware —
# which has it — stashes the identified caller here. Safe because the
# listener is ``ThreadingMixIn``: one thread per request.
_acl_ctx = threading.local()


def _identify_peer_by_addr(environ):
    """Best-effort caller identity for the git routes: match
    ``REMOTE_ADDR`` against paired peers' known endpoints. Returns a
    peer_id or ''.

    Same heuristic ``_reverse_deliver_by_addr`` uses, and subject to the
    same limits — spoofable on a hostile LAN, blind to a peer on an
    address we haven't recorded. It is nonetheless the only identity the
    git smart-protocol routes offer (no body, TLS is CERT_NONE), and
    it's strictly better than authorising every caller identically."""
    try:
        host = str(environ.get('REMOTE_ADDR', '') or '').strip()
        if not host:
            return ''
        for peer in (_peers.list_peers() or []):
            for ep in (list(peer.get('endpoints') or [])
                       + list(peer.get('static_endpoints') or [])):
                if str(ep).rsplit(':', 1)[0] == host:
                    return peer.get('peer_id', '') or ''
    except Exception:
        pass
    return ''


def _project_presence(langcode):
    """``'present'`` | ``'ghost'`` | ``'materializing'`` |
    ``'unregistered'`` (0.55.47).

    Only ``'ghost'`` — REGISTERED but its directory is gone — is safe to
    self-clean. The other two are deliberately left alone:

    - ``'materializing'``: directory exists, no ``.git`` yet. Plausibly a
      clone in flight, which is the exception Kent carved out; pruning
      would cancel a share the user just accepted.
    - ``'unregistered'``: we never had it. Could be a share we accepted
      whose clone hasn't started, so refuse but don't touch grants."""
    try:
        p = _projects.get(langcode)
    except Exception:
        return 'materializing'      # can't tell → never prune
    if p is None:
        return 'unregistered'
    wd = (getattr(p, 'working_dir', '') or '').strip()
    if not wd or not os.path.isdir(wd):
        return 'ghost'
    if not os.path.isdir(os.path.join(wd, '.git')):
        return 'materializing'
    return 'present'


def _prune_ghost_shares(langcode):
    """Drop *langcode* from every paired peer's ``shared_projects``.

    Called when a request arrives for a project that is registered but
    whose directory is gone. The grant cannot be honoured by anything, so
    keeping it only guarantees the peer asks again forever — the field
    symptom was ``GET /en.git/info/refs`` served-then-failed on repeat
    (Kent 2026-07-28: *"if we don't have the project, we should clear
    it"*).

    Local only, by design: Kent explicitly does NOT want unshares
    propagated (*"we don't need to keep everyone up to date on our
    projects"*). The peer stops getting it served; whether it keeps
    asking is its own business."""
    pruned = []
    try:
        for entry in _peers.list_peers() or []:
            pid = entry.get('peer_id', '') or ''
            if pid and langcode in (entry.get('shared_projects') or []):
                try:
                    _peers.remove_shared_project(pid, langcode)
                    pruned.append(pid[:8])
                except Exception as ex:
                    print(f'[lan-listener] prune {langcode!r} from '
                          f'{pid[:8]!r} raised: {ex!r}',
                          file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[lan-listener] ghost-share prune raised: {ex!r}',
              file=sys.stderr, flush=True)
        return
    print(f'[data-quality] pruned-ghost-shares langcode={langcode!r} '
          f'peers={pruned!r} — registered but its directory is gone, so '
          f'the grant could never be served',
          file=sys.stderr, flush=True)


_REVERSE_COOLDOWN_S = 60.0
# Per-PEER concurrency, not a global counter (0.55.19). A flat cap of 2
# was starving the mechanism it was meant to protect: reverse-delivery
# slots get held for minutes by a merge waiting on project_lock, and
# every other peer then lost its only chance at an observation —
# ``reverse delivery for '74453504' skipped — 2 already in flight``
# eight times in one field log while that peer's board went stale. One
# in flight PER PEER is the real invariant (a second concurrent dial to
# the same peer is pure duplicate work); the global ceiling only exists
# so a pathological fan-out can't spawn without bound.
_REVERSE_MAX_INFLIGHT_TOTAL = 8
_reverse_gate_lock = threading.Lock()
_reverse_last_at = {}      # (peer_id, langcode) → admission unix ts
_reverse_inflight_peers = set()   # peer_ids currently being delivered to


def record_qr_offered(langcode):
    """Heartbeat: the share QR for *langcode* is currently displayed.
    Called on QR open and every ~10 s while it stays up. Consulted by
    the hello handler to gate auto-share. Empty langcode is a no-op
    (pair-only QR, no project share)."""
    if not langcode:
        return
    _pending_qr_offers[str(langcode)] = _time.time()


def qr_offer_active(langcode):
    """True if a share QR for *langcode* is currently being displayed
    (a heartbeat landed within ``_QR_OFFER_KEEPALIVE_S``). **Multi-use**
    — does NOT consume the offer, so one displayed QR can share to
    several peers who scan it (the workshop "show it to the room" case).
    The offer is revoked by ``clear_qr_offer`` (screen closed) or by the
    heartbeat lapsing (display gone). This "valid while shown" model
    replaced the single-use + 10-minute TTL in 0.52.26."""
    if not langcode:
        return False
    key = str(langcode)
    ts = _pending_qr_offers.get(key)
    if ts is None:
        return False
    if _time.time() - ts > _QR_OFFER_KEEPALIVE_S:
        _pending_qr_offers.pop(key, None)
        return False
    return True


def clear_qr_offer(langcode):
    """Revoke a share offer immediately — called when the QR popup
    closes. No-op if none pending / empty langcode."""
    if langcode:
        _pending_qr_offers.pop(str(langcode), None)


def is_running():
    with _LOCK:
        return _STATE['server'] is not None


def bound_endpoint():
    """Return ``(host, port)`` if running, else ``None``. Host is
    the daemon's outward-facing LAN IP (best-effort); fall back to
    ``0.0.0.0`` and let the caller substitute the discovered IP."""
    with _LOCK:
        return _STATE['bound']


def bind_error():
    """Last failing step of ``apply_toggle`` ('' when the listener
    came up, or hasn't been asked to). 0.54.75."""
    with _LOCK:
        return _STATE.get('bind_error', '') or ''


def _usb_net_ifaces():
    """Names of local network interfaces that are USB-attached —
    i.e. a phone/tablet presenting a tether gadget (``usb0``,
    ``enp0s20u2``, ``rndis0``, ``ncm0``…). Read from
    ``/sys/class/net/<if>/device`` whose resolved path sits under a
    USB bus; falls back to name matching where sysfs isn't readable.

    Why sysfs and not ``_interface_ipv4s``: that helper uses
    SIOCGIFCONF, which lists only interfaces that ALREADY have an
    address. The state we most need to name is exactly the one it
    can't see — the cable is plugged in and the gadget exists, but
    the phone hasn't switched USB tethering on, so no address has
    been handed out (field 2026-07-25: tablet cabled to the desktop,
    tether toggle off, and the UI said "no action needed").

    Empty list on any platform / permission problem — callers treat
    that as "can't tell", never as "no cable"."""
    out = []
    base = '/sys/class/net'
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        if name == 'lo':
            continue
        is_usb = False
        try:
            dev = os.path.realpath(os.path.join(base, name, 'device'))
            is_usb = ('/usb' in dev) or ('usb' in os.path.basename(dev))
        except OSError:
            is_usb = False
        if not is_usb:
            # Naming fallback for hosts where the sysfs symlink isn't
            # present (some Android kernels): the conventional gadget
            # names are stable enough to recognise.
            low = name.lower()
            is_usb = (low.startswith('usb')
                      or low.startswith('rndis')
                      or low.startswith('ncm'))
        if is_usb:
            out.append(name)
    return out


def link_state():
    """Classify why (or whether) this daemon is reachable by a peer,
    for the settings line + cable check. Returns one of:

    - ``'ok'``          — bound AND at least one non-loopback IPv4 to
                          be reached at.
    - ``'tether_off'``  — bound, no usable address, but a USB network
                          gadget IS present. In practice this means
                          tethering is ON and no address has arrived
                          yet (DHCP pending or failed) — NOT
                          "cable in, tethering off", because with
                          tethering off Android exposes no network
                          function and the host sees no interface to
                          detect.
    - ``'no_link'``     — bound, no usable address, no USB gadget
                          seen. Covers BOTH "nothing plugged in" and
                          "cabled but not tethered": indistinguishable
                          from here, so callers must not claim a cable
                          is absent (see the 0.54.80 wording). Correct
                          by design when a device is off network; not
                          a failure.
    - ``'not_bound'``   — the listener isn't running. A real failure;
                          pair with ``bind_error()`` for the step.
    - ``'off'``         — sharing is switched off (caller decides;
                          never returned from here).

    The distinction matters because 0.54.74 collapsed all of these
    into an empty endpoint string, so a healthy listener on an
    off-network machine was reported the same way as a failed bind —
    and the UI told the user no action was needed in a state where
    the fix was one toggle away on their tablet. 0.54.75."""
    if not is_running():
        return 'not_bound'
    if bound_endpoints_all():
        return 'ok'
    if _usb_net_ifaces():
        return 'tether_off'
    return 'no_link'


def bound_endpoints_all():
    """Every plausible ``ip:port`` for THIS listener: each non-loopback
    local IPv4 (``_interface_ipv4s``, private-first) paired with the
    bound port. Advertised in the pairing QR so a peer reaching us over
    ANY link — wifi, a USB-tether ``usb0``, a hotspot subnet — finds a
    reachable address (the receiver tries each). This is the fix for
    the single-address gap: ``_outward_ip_guess`` picks only the
    default-route IP, which on a tethering setup is the wrong one for
    the cable. Empty list when the listener isn't bound."""
    with _LOCK:
        bound = _STATE['bound']
    if not bound:
        return []
    port = bound[1]
    ips = _interface_ipv4s()
    if not ips:
        host = bound[0]
        return [f'{host}:{port}'] if host and host != '0.0.0.0' else []
    return [f'{ip}:{port}' for ip in ips]


class _DynamicBackend:
    """dulwich Backend that resolves ``open_repository`` against
    the *current* state of ``projects.json`` and ``peers.json`` —
    not a snapshot taken at listener-start. New share_offer arrivals
    (which mutate ``shared_projects``) immediately show up in the
    serving set without a listener restart; rolled-back shares
    immediately stop serving. Tradeoff is one ``peers.json`` read
    per request; the file is tiny and cached at the OS level so
    the cost is negligible vs the network/git work that follows.

    **fd hygiene (0.54.1).** dulwich's web handlers never close the
    Repo the backend hands them, and a Repo holds pack/index fds
    that GC does not reliably release (reference cycles). Every
    phone poll therefore leaked fds until the 2026-07-10 EMFILE
    incident wedged the whole daemon. Each Repo opened here is
    recorded thread-locally; ``_repo_closing_middleware`` closes
    them when the WSGI response for that request finishes (same
    thread — the server is thread-per-request).
    """

    def __init__(self):
        self._thread_repos = threading.local()

    def _track(self, repo):
        lst = getattr(self._thread_repos, 'repos', None)
        if lst is None:
            lst = self._thread_repos.repos = []
        lst.append(repo)
        return repo

    def close_thread_repos(self):
        lst = getattr(self._thread_repos, 'repos', None)
        if not lst:
            return
        self._thread_repos.repos = []
        for r in lst:
            try:
                r.close()
            except Exception:
                pass

    def open_repository(self, path):
        from dulwich.errors import NotGitRepository
        from dulwich.repo import Repo
        # ``path`` shape varies across dulwich call sites in two
        # axes:
        #   - encoding: the GET ``/info/refs`` handler in
        #     ``dulwich.web`` passes str (sliced from the URL
        #     string); the smart-protocol POST handler in
        #     ``dulwich.server`` (UploadPackHandler /
        #     ReceivePackHandler init) passes bytes from the
        #     wire-protocol parser.
        #   - shape: some sites pass the repo prefix
        #     (``/baf.git`` or ``baf.git``), others pass the full
        #     URL path (``/baf.git/info/refs``).
        # Pre-0.45.28 we only handled str; the POST path raised
        # ``TypeError: a bytes-like object is required, not 'str'``
        # at the ``lstrip('/')`` below, dulwich returned 500, and
        # the pusher logged ``[lan-merge] fetch from '<peer>'
        # failed: GitProtocolError('unexpected http resp 500 ...')``.
        raw = path or ''
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        norm = raw.lstrip('/')
        if '.git' in norm:
            langcode = norm.split('.git', 1)[0]
        else:
            langcode = norm.split('/', 1)[0]
        print(f'[lan-listener] open_repository: raw={raw!r} → '
              f'langcode={langcode!r}',
              file=sys.stderr, flush=True)
        # GATE 1 — DO WE EVEN HAVE IT? (0.55.47)
        #
        # Kent 2026-07-28: *"we shouldn't be sharing what we've
        # deleted. No idea how it got there, but if we don't have the
        # project, we should clear it (except if it's waiting on a
        # clone)."* Field: this phone served ``GET /en.git/info/refs``
        # repeatedly for a project whose working_dir was gone —
        # authorised by a stale grant in some peer's share list, then
        # failing deeper as NOT_A_REPO. Nothing pruned the grant, so it
        # recurred forever.
        #
        # Presence is checked BEFORE authorisation on purpose: a grant
        # for something we don't have is meaningless, and self-clearing
        # it here is the only place that sees the evidence.
        _presence = _project_presence(langcode)
        if _presence != 'present':
            if _presence == 'unregistered':
                print(f'[lan-listener] reject {langcode!r}: not '
                      f'registered on this device — refusing, grants '
                      f'left intact (may be an accepted share whose '
                      f'clone has not started)',
                      file=sys.stderr, flush=True)
                raise NotGitRepository(
                    f'project {langcode!r} not registered')
            if _presence == 'ghost':
                # Registered-or-granted but the directory is GONE.
                # Nothing can make this servable; drop the grants so
                # peers stop asking.
                _prune_ghost_shares(langcode)
                raise NotGitRepository(
                    f'project {langcode!r} is not on this device '
                    f'(stale share grants pruned)')
            # 'materializing' — directory exists but no .git yet, i.e.
            # plausibly a clone in flight. Refuse WITHOUT pruning: this
            # is exactly the exception Kent carved out, and a prune here
            # would cancel a share the user just accepted.
            print(f'[lan-listener] defer {langcode!r}: directory '
                  f'present but no .git yet (clone in flight?) — '
                  f'refusing this request, grants left intact',
                  file=sys.stderr, flush=True)
            raise NotGitRepository(
                f'project {langcode!r} not yet materialized')
        # GATE 2 — IS IT SHARED WITH *THIS* PEER? This IS the access
        # control on the listener (TLS is CERT_NONE since stdlib ssl
        # can't pin self-signed client certs).
        try:
            peers_list = _peers.list_peers(strict=True)
        except Exception as ex:
            # Transient registry-read failure (fd exhaustion, EIO):
            # do NOT read as "empty allowlist / nothing shared" —
            # that silently unshared every project during the
            # 2026-07-10 EMFILE incident. Refuse THIS request as
            # transient; the peer retries on its next pass.
            print(f'[lan-listener] defer {langcode!r}: peer '
                  f'registry unreadable ({ex!r}) — transient, '
                  f'NOT treating as unshared',
                  file=sys.stderr, flush=True)
            raise NotGitRepository(
                'peer registry unreadable (transient)') from ex
        # PER-PEER when we can identify the caller, union when we can't
        # (0.55.47). The git smart-protocol routes carry no identity —
        # no body, and TLS is CERT_NONE — which is *why* this was a
        # union of every paired peer's list. The consequence, confirmed
        # in the field: sharing one project with one peer made it
        # fetchable by EVERY paired peer, and the peer screen showed
        # nothing to warn you. Kent's phone served 'nml' to the desktop
        # 22 times in 15 minutes while its own screen read "no projects
        # shared" for that desktop.
        #
        # Best available identity is the source address, matched against
        # paired peers' known endpoints — the same heuristic reverse
        # delivery uses, stashed by ``_peer_acl_middleware``. Spoofable
        # on a hostile LAN; strictly better than no check at all.
        #
        # Unidentified callers FALL BACK to the union rather than being
        # refused. That is deliberate: a peer on a new subnet, or behind
        # NAT, has an address we don't know yet, and refusing it would
        # break working pairs the moment this ships. The fallback is
        # logged as a data-quality line so the frequency is visible —
        # that log is the audit needed before making per-peer strict.
        requester = getattr(_acl_ctx, 'peer_id', '') or ''
        remote = getattr(_acl_ctx, 'remote', '') or ''
        accepting = bool(getattr(_acl_ctx, 'accepting', False))
        try:
            from . import settings as _settings_mod
            strict = bool(_settings_mod.get('lan.strict_peer_acl', False))
        except Exception:
            strict = False
        # BOTH DIRECTIONS are gated by the grant (0.55.49). 0.55.48
        # exempted inbound on the reasoning that a grant says what we
        # hand out, not whose data we take. Kent corrected that, and he
        # is right: **a grant is per-project consent to collaborate with
        # that peer.** Accepting an unsolicited push means taking
        # someone's commits into our project, which needs the same
        # agreement as serving ours out. Treating a non-consented
        # delivery as something to preserve was routing around a
        # configuration error instead of surfacing it.
        #
        # Exempting inbound was also useless in practice:
        # ``_push_to_peer`` proceeds with the push when its peek fails
        # ("Couldn't ls-remote; proceed with the push attempt anyway"),
        # so refusing only the fetch leaves the push knocking anyway.
        # Gate both or gate neither.
        if requester and strict:
            granted = set()
            for peer in peers_list:
                if (peer.get('peer_id') or '') == requester:
                    granted = set(peer.get('shared_projects') or [])
                    break
            if langcode not in granted:
                print(f'[lan-listener] reject {langcode!r} '
                      f'({"inbound push" if accepting else "fetch"}) '
                      f'from {requester[:8]!r}: not shared with them '
                      f'(their grants: {sorted(granted)!r}) — per-peer '
                      f'ACL', file=sys.stderr, flush=True)
                raise NotGitRepository(
                    f'project {langcode!r} is not shared with you')
        else:
            shared_anywhere = set()
            for peer in peers_list:
                shared_anywhere.update(peer.get('shared_projects') or [])
            if langcode not in shared_anywhere:
                print(f'[lan-listener] reject {langcode!r}: not in any '
                      f'peer\'s shared_projects '
                      f'(shared_anywhere={sorted(shared_anywhere)!r})',
                      file=sys.stderr, flush=True)
                raise NotGitRepository(
                    f'project {langcode!r} is not shared with any peer')
            # THE AUDIT (0.55.48). Two different reasons to be here, and
            # only one of them is about identity — say which, because
            # this log is what decides whether `lan.strict_peer_acl` is
            # safe to turn on.
            if requester:
                granted = set()
                for peer in peers_list:
                    if (peer.get('peer_id') or '') == requester:
                        granted = set(peer.get('shared_projects') or [])
                        break
                if langcode not in granted:
                    print(f'[data-quality] acl-would-refuse langcode='
                          f'{langcode!r} peer={requester[:8]!r} '
                          f'dir={"inbound-push" if accepting else "fetch"} '
                          f'from={remote or "?"} (their grants: '
                          f'{sorted(granted)!r}) — allowed under the '
                          f'union; STRICT MODE WOULD REFUSE THIS. Grant '
                          f'the project to that peer before enabling '
                          f'lan.strict_peer_acl',
                          file=sys.stderr, flush=True)
            else:
                print(f'[data-quality] acl-fallback-union langcode='
                      f'{langcode!r} from={remote or "?"} — caller not '
                      f'identifiable by address, so per-peer enforcement '
                      f'is impossible for this request. Frequent hits '
                      f'mean strict mode would refuse real peers',
                      file=sys.stderr, flush=True)
        project = _projects.get(langcode)
        if project is None or not project.working_dir:
            print(f'[lan-listener] reject {langcode!r}: not '
                  f'registered (project={project!r})',
                  file=sys.stderr, flush=True)
            raise NotGitRepository(
                f'project {langcode!r} not registered')
        try:
            repo = self._track(Repo(project.working_dir))
        except Exception as ex:
            print(f'[lan-listener] open repo {langcode!r} failed: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            raise NotGitRepository(
                f'project {langcode!r} repo failed to open') from ex
        _warn_if_advertising_unservable_head(langcode, repo)
        _hide_unservable_refs(langcode, repo)
        return repo


def _build_dict_backend():
    """Return the dynamic backend. Kept under the old name so the
    rest of ``_build_server`` reads identically; switched from a
    static ``DictBackend(mapping)`` (snapshot at listener-start)
    to ``_DynamicBackend`` (re-reads ``peers.json`` on each
    request) so new share_offer arrivals work without a listener
    restart."""
    return _DynamicBackend()


class _ClosingBody:
    """WSGI response wrapper: when the server finishes with the
    response (PEP 3333 guarantees a ``close()`` call), close every
    dulwich Repo the backend opened for this request's thread."""

    def __init__(self, inner, backend):
        self._inner = inner
        self._backend = backend

    def __iter__(self):
        return iter(self._inner)

    def close(self):
        try:
            if hasattr(self._inner, 'close'):
                self._inner.close()
        finally:
            self._backend.close_thread_repos()


def _repo_closing_middleware(app, backend):
    """Outermost WSGI layer: pair every request with a
    ``close_thread_repos()`` — on the happy path via
    ``_ClosingBody.close()`` after the response is fully sent, on
    the raise path immediately. See ``_DynamicBackend`` docstring
    for the fd-leak incident this fixes."""
    def _app(environ, start_response):
        try:
            body = app(environ, start_response)
        except Exception:
            backend.close_thread_repos()
            raise
        return _ClosingBody(body, backend)
    return _app


def _peer_id_from_cert_der(cert_der):
    """Extract the lowercase hex ed25519 pubkey from a DER-encoded
    X.509 cert. Returns '' if the cert's public key isn't ed25519
    or the parse fails — the middleware then rejects the request."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        return ''
    try:
        try:
            from cryptography.hazmat.backends import default_backend
            cert = x509.load_der_x509_certificate(
                cert_der, backend=default_backend())
        except TypeError:
            # Newer cryptography: no backend kwarg.
            cert = x509.load_der_x509_certificate(cert_der)
        pub = cert.public_key()
    except Exception:
        return ''
    if not isinstance(pub, ed25519.Ed25519PublicKey):
        return ''
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def _cert_fp_from_der(cert_der):
    import hashlib
    return hashlib.sha256(cert_der).hexdigest()


def _admin_canonical_request(nonce, method, path, body):
    """The exact bytes an admin request is signed over (0.55.117).

    Mirrored in ``azt_collab_client/transports/lan_admin.py``, which
    cannot import this module (client hard rule #3). Named rather than
    inlined so the two copies can be compared directly —
    ``lan_admin.self_check()`` does exactly that, because drift here
    surfaces only as a 403 that is indistinguishable from a missing admin
    grant. If you change this, change that."""
    import json as _j
    return _j.dumps(
        {'nonce': nonce, 'method': method, 'path': path, 'body': body},
        sort_keys=True, separators=(',', ':')).encode('utf-8')


def _handle_challenge(environ, start_response):
    """Hand out a single-use nonce for signed requests (0.55.102).

    Deliberately unauthenticated — it must be, since it is the first step
    of authenticating. Cheap and bounded on purpose: ``issue_nonce``
    expires entries after 60 s and caps the table, because this is
    reachable by anything that can open a socket to the listener."""
    from . import peer_id as _pid
    return _json_response(start_response, '200 OK',
                          {'ok': True, 'nonce': _pid.issue_nonce()})


# Read-only admin calls, rolled up per peer (0.55.184).
_ADMIN_READ_ROLLUP_S = 60.0
_admin_reads = {}          # peer -> {n, since, paths, name}
_admin_reads_lock = threading.Lock()


def _note_admin_read(peer, device_name, path):
    """Count a read-only admin call, emitting one summary line per peer
    per ``_ADMIN_READ_ROLLUP_S`` instead of one line per request.

    Flushed from the request path rather than on a timer, so a peer
    that stops polling leaves its last partial window uncounted. That
    is the right trade: this line exists to show that polling IS
    happening and at what rate, and no line is what "nobody is polling"
    should look like."""
    now = _time.monotonic()
    line = None
    with _admin_reads_lock:
        rec = _admin_reads.get(peer)
        if rec is None:
            rec = {'n': 0, 'since': now, 'paths': {},
                   'name': device_name}
            _admin_reads[peer] = rec
        rec['n'] += 1
        rec['name'] = device_name or rec['name']
        rec['paths'][path] = rec['paths'].get(path, 0) + 1
        if now - rec['since'] >= _ADMIN_READ_ROLLUP_S:
            top = sorted(rec['paths'].items(),
                         key=lambda kv: -kv[1])[:6]
            detail = ', '.join(f'{p} ×{c}' for p, c in top)
            extra = len(rec['paths']) - len(top)
            if extra > 0:
                detail += f', +{extra} more path(s)'
            line = (f'[lan-admin] served {rec["n"]} read-only '
                    f'request(s) FROM {rec["name"]} ({peer[:8]}) in the '
                    f'last {int(now - rec["since"])}s: {detail}')
            _admin_reads[peer] = {'n': 0, 'since': now, 'paths': {},
                                  'name': rec['name']}
    if line:
        print(line, file=sys.stderr, flush=True)


def _handle_admin(environ, start_response):
    """Run one local RPC on behalf of a peer that has been granted
    admin (0.55.102).

    **One endpoint carries every RPC.** Body is an envelope —
    ``{peer_id, nonce, sig, method, path, body}`` — dispatched into the
    ordinary local dispatch table, so the operator's existing settings UI
    works unmodified against a remote daemon. Kent, on my first proposal
    to forward a whitelist of specific config calls: *"if it's all the
    same, I'd rather have the same functions, just to keep it simple."*
    Fewer moving parts, and no per-endpoint work as the API grows.

    Three gates, in order, all of which must pass:

    1. **The nonce is spent** — single-use, so a captured request cannot
       be replayed even within its 60 s life.
    2. **The signature verifies** against ``peer_id``, which IS the raw
       ed25519 pubkey. This is the part the old body-auth path lacked
       entirely: it checked only that the caller *named* a paired peer,
       and peer_id is public (mDNS-advertised), so anyone on the network
       could satisfy it.
    3. **That peer holds an explicit admin grant** — a separate per-peer
       flag, NOT implied by pairing. Pairing means "share dictionary
       data"; the phones are paired too, and a lost phone must not carry
       the power to change settings on someone's desktop.

    The signature covers the nonce **and** the method+path+body, so a
    valid signature can't be lifted off one request and re-used to
    authorize a different one."""
    import json as _json
    from . import peer_id as _pid
    from . import peers as _peers

    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer = str(payload.get('peer_id', '') or '')
    nonce = str(payload.get('nonce', '') or '')
    sig = str(payload.get('sig', '') or '')
    rpc_method = str(payload.get('method', '') or 'GET').upper()
    rpc_path = str(payload.get('path', '') or '')
    rpc_body = payload.get('body')

    if not _pid.spend_nonce(nonce):
        print(f'[lan-admin] {peer[:8]!r}: refused — nonce not outstanding '
              f'(expired, already spent, or never issued by us). Fetch a '
              f'fresh one from /v1/lan/challenge per request',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'bad nonce'})

    # Sign over nonce + the request it authorizes, so a captured
    # signature can't be replayed against a DIFFERENT call.
    try:
        canon = _admin_canonical_request(
            nonce, rpc_method, rpc_path, rpc_body)
    except Exception as ex:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': f'uncanonicalizable: {ex!r}'})

    if not _pid.verify_hex(peer, canon, sig):
        print(f'[lan-admin] {peer[:8]!r}: refused — signature does not '
              f'verify against that peer_id for this exact request. Either '
              f'the caller does not hold the private key, or it signed '
              f'different bytes than it sent',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'bad signature'})

    entry = _peers.get_peer(peer) or {}
    if not entry:
        print(f'[lan-admin] {peer[:8]!r}: refused — signature is VALID but '
              f'this peer is not paired with us at all',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'not paired'})
    # LEARN THE ADDRESS FROM A PROVEN REQUEST, EVEN A REFUSED ONE (0.55.156).
    #
    # Placed above the grant check on purpose: the signature has verified, so
    # the source host provably belongs to this peer regardless of whether the
    # call is authorised. Refusing the RPC is no reason to throw away the one
    # piece of reachability evidence that is hard to come by.
    #
    # Field 2026-07-30: both machines were on 172.16.133.x while this side
    # dialled 192.168.31.179 and spent its whole 8 s budget on
    # ``2 of 7, 5 not tried`` — the working address was among the five it
    # never reached, and the peer was connecting to us throughout.
    _note_inbound_endpoint(peer, environ, payload, deliver=False)
    if not bool(entry.get('admin')):
        print(f'[lan-admin] {peer[:8]!r} ({entry.get("device_name") or "?"}): '
              f'refused {rpc_method} {rpc_path!r} — identity proven, but no '
              f'admin grant. Pairing alone does NOT grant this; someone must '
              f'enable "allow this device to change my settings" for it here',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'no admin grant'})

    # EVERY RPC, INCLUDING THE ONES THAT CHANGE GRANTS (0.55.118).
    #
    # 0.55.117 blocked ``set_admin`` / ``unpair`` / ``pair_accept`` here.
    # I added that on my own and justified it with "a peer could grant
    # itself admin permanently" — which is meaningless: a peer that has
    # admin doesn't need to re-grant itself, and the grant has no expiry
    # to refresh. Kent: *"this is security policy written by a lawyer,
    # not me."*
    #
    # What the block actually cost: you could not grant a second device
    # access to a remote machine without travelling to that machine —
    # most of the point of the feature. What it actually prevented: a
    # device you already trusted delegating to a third device, or
    # revoking your own access. Both are simply what admin MEANS; the
    # decision was made when the grant was given, by hand, on the
    # machine.
    #
    # So: no denylist. Privilege changes are LOGGED loudly instead —
    # visible after the fact beats unavailable by policy.
    _PRIVILEGED = ('/v1/lan/set_admin', '/v1/lan/unpair',
                   '/v1/lan/pair_accept', '/v1/credentials')
    _is_priv = any(rpc_path.startswith(p) for p in _PRIVILEGED)
    # SAY WHICH DIRECTION (0.55.135). This read
    # ``[lan-admin] '3a0285ec' (Kent Phone): GET '/v1/projects'`` — which
    # could equally mean "we asked the phone" or "the phone asked us".
    # Kent, reading the tablet's log: *"am I looking at the settings of the
    # phone (admin) or the tablet (grantee)?"* The answer was in the line
    # and unreadable from it.
    #
    # This side always SERVES, so say so, and name what is being changed:
    # THIS device.
    # READS ARE ROLLED UP, WRITES ARE NAMED (0.55.184).
    #
    # A single open remote-settings window polls ~7 endpoints every few
    # seconds — status, pending, peer_sync, toggle, last_project,
    # work_offline — and each one printed a line here. Field
    # 2026-07-31: reading a peer's log to diagnose a stuck push meant
    # scrolling through a screenful of this per second, generated by
    # the very window being used to read it. The log became unusable
    # for the reason it was being opened.
    #
    # A GET changes nothing, so it does not need naming individually,
    # and the old wording claimed otherwise: "it is changing THIS
    # device: GET '/v1/lan/pending'" was false on its face
    # (invariant #15). Writes and privileged calls still get their own
    # line, every time — those are the ones worth an audit trail.
    # Refusals above this point are unconditional and untouched: a
    # rolled-up read must never hide a rejected one.
    if _is_priv or rpc_method != 'GET':
        print(f'[lan-admin] serving{" PRIVILEGED" if _is_priv else ""} '
              f'request FROM {entry.get("device_name") or "?"} '
              f'({peer[:8]}) — it is changing THIS device: '
              f'{rpc_method} {rpc_path!r}', file=sys.stderr, flush=True)
    else:
        _note_admin_read(peer, entry.get('device_name') or '?', rpc_path)
    try:
        # The ordinary local dispatch table — same code path the loopback
        # server uses, so a remote caller gets identical behaviour to a
        # local one. It returns an int status; the WSGI layer wants a
        # status line.
        from .server import dispatch as _dispatch
        code, resp = _dispatch(rpc_method, rpc_path, rpc_body)
        status = {200: '200 OK', 400: '400 Bad Request',
                  401: '401 Unauthorized', 403: '403 Forbidden',
                  404: '404 Not Found',
                  500: '500 Internal Server Error'}.get(
                      int(code or 200), f'{int(code or 200)} Status')
    except Exception as ex:
        print(f'[lan-admin] {peer[:8]!r}: {rpc_method} {rpc_path!r} raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return _json_response(start_response, '500 Internal Server Error',
                              {'ok': False, 'error': repr(ex)})
    return _json_response(start_response, status, resp)


def _handle_hello_bodyauth(environ, start_response):
    """Body-auth variant of the hello handler (TLS client cert
    validation deliberately disabled — see ``_build_server`` for
    why). Reads the peer's identity from the request body and
    trusts it. The body's ``peer_id`` IS the peer's ed25519
    pubkey; a future-hardening pass should add a signature so
    we can cryptographically verify the body really came from
    the holder of that private key.

    Body: ``{peer_id, fp, device_name, langcode?, endpoint?}``.
    Response: ``{ok: True, peer_id}`` on success."""
    import json as _json
    try:
        n = int(environ.get('CONTENT_LENGTH', '0') or '0')
        if n > 0:
            raw = environ['wsgi.input'].read(n)
        else:
            raw = b''
        payload = _json.loads(raw.decode('utf-8') or '{}')
    except Exception as ex:
        start_response('400 Bad Request',
                       [('Content-Type', 'text/plain')])
        return [f'invalid body: {ex!r}\n'.encode('utf-8')]
    if not isinstance(payload, dict):
        start_response('400 Bad Request',
                       [('Content-Type', 'text/plain')])
        return [b'body must be an object\n']
    actual_peer_id = str(payload.get('peer_id', '') or '')
    actual_fp = str(payload.get('fp', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    if len(actual_peer_id) != 64 or len(actual_fp) != 64:
        start_response('400 Bad Request',
                       [('Content-Type', 'text/plain')])
        return [b'peer_id / fp wrong length\n']
    # Capture the sender's listener endpoint so future LAN fan-out
    # has somewhere to push to. Without this, our peers.json entry
    # for them holds ``endpoints=[]`` and ``_resolve_endpoint``
    # gives nothing back, silently skipping the fan-out
    # (``no endpoint for <peer_id>``). Empty incoming endpoint =
    # pre-fix sender, falls back to the legacy no-endpoint record.
    incoming_endpoint = str(payload.get('endpoint', '') or '')
    # Was this pairing deliberately REVOKED here? (0.54.97) The heal
    # that completes an interrupted handshake works by the other side
    # re-saying hello, and this handler records a pairing for any
    # caller — so without this check, a peer holding a stale record
    # would silently resurrect a pairing the user had removed. An
    # invite-plus-accept whose confirmation got lost and a revocation
    # are entirely different situations; only the first may self-heal.
    # A local gesture (QR display for the auto-share case, or a fresh
    # pair/accept) clears the tombstone.
    try:
        if _peers.is_unpair_tombstoned(actual_peer_id):
            already = _peers.get_peer(actual_peer_id) is not None
            if not already:
                print(f'[lan-listener] hello from '
                      f'{actual_peer_id[:8]!r} ({device_name!r}) '
                      f'REFUSED: this device was unpaired here — '
                      f're-pair to reconnect',
                      file=sys.stderr, flush=True)
                return _json_response(
                    start_response, '200 OK',
                    {'ok': False, 'error': 'unpaired_here'})
    except Exception as ex:
        print(f'[lan-listener] tombstone check raised: {ex!r}',
              file=sys.stderr, flush=True)
    _peers.record_pair(actual_peer_id, actual_fp,
                       device_name, incoming_endpoint)
    # Their hello proves they hold this pairing, so it's mutual from
    # here (0.54.97) — this is the receiving half of the heal.
    try:
        _peers.set_pair_confirmed(actual_peer_id, True)
    except Exception:
        pass
    # The address it arrived from beats the one it claims (0.54.99).
    _note_inbound_endpoint(actual_peer_id, environ, payload)
    # Symmetric auto-share: if the hello carried a langcode (the
    # project the scanner just LAN-cloned FROM us), add it to our
    # shared_projects allowlist for them too. Saves the owner a
    # second tap on Share after the QR scan; the underlying share
    # was the QR-show gesture itself.
    #
    # SECURITY: only fire if the user is actively DISPLAYING a QR
    # offering this langcode right now (``qr_offer_active`` — a
    # heartbeat within the keepalive window). Without this gate, anyone
    # on the
    # LAN can POST ``/v1/lan/hello`` claiming any langcode and we
    # would auto-grant them read access to that project (the git
    # smart-protocol handler accepts requests for any project in
    # the union of all paired peers' shared_projects). The QR-
    # display gesture is the user-consent signal that pins
    # langcode auto-share to a real intent. If no recent QR for
    # this langcode is on file, we still record the pair (the
    # caller went out of their way to claim an identity) but
    # refuse the auto-share — the user can still tap Share
    # manually if they meant to allow this peer.
    langcode_offered = str(payload.get('langcode', '') or '')
    share_refused = ''
    if langcode_offered:
        if qr_offer_active(langcode_offered):
            try:
                _peers.add_shared_project(
                    actual_peer_id, langcode_offered)
            except Exception as ex:
                print(f'[lan-listener] hello auto-share for '
                      f'{langcode_offered!r} raised: {ex!r}',
                      file=sys.stderr, flush=True)
        else:
            # TELL THE SCANNER (0.55.3). This refusal used to be
            # log-only: the phone asked for a langcode by name, got a
            # pair without it, and discovered the consequence three
            # steps later as ``NotGitRepository`` from a clone that
            # could never work — with nothing on screen. The scan is
            # the user's whole gesture; if it didn't grant what it
            # asked for, that belongs in the reply.
            share_refused = langcode_offered
            print(f'[lan-listener] hello from {actual_peer_id[:8]!r} '
                  f'claimed langcode={langcode_offered!r} but no '
                  f'recent QR offer for it; pair recorded, '
                  f'auto-share refused — telling the scanner',
                  file=sys.stderr, flush=True)
    # SHARE MANIFEST EXCHANGE (0.55.50). Until now hello established
    # pairing and said nothing about which projects each side is willing
    # to collaborate on, so two peers could greet successfully and only
    # discover a per-project disagreement much later — implicitly, as a
    # reasonless ``NotGitRepository`` from a git route. Kent: *"can we
    # greet each other as peers, then only later discover that we don't
    # share x project?"* Yes, and that was the bug: the desktop dialed
    # the phone for 'nml' for hours without ever learning the phone
    # would not collaborate on it.
    #
    # Both directions travel on this one authenticated exchange:
    #   - inbound  ``shared_with_you``: what the CALLER grants US.
    #   - outbound ``shared_with_you``: what WE grant the caller.
    # Each side stores the other's list as ``their_shared_projects`` and
    # uses it to stop dialing for projects the peer hasn't consented to.
    try:
        claimed = payload.get('shared_with_you')
        if isinstance(claimed, list):
            _claimed = [str(x) for x in claimed if isinstance(x, str)]
            _peers.set_their_shared_projects(actual_peer_id, _claimed)
            # Grant back what they share with us and we already hold
            # (0.55.148) — the retroactive half of the accept-time rule,
            # so peers that accepted before that shipped stop being
            # one-sided without anyone having to notice and fix it.
            _peers.reciprocate_shares(actual_peer_id, _claimed)
    except Exception as ex:
        print(f'[lan-listener] hello: recording caller manifest '
              f'raised: {ex!r}', file=sys.stderr, flush=True)
    ours_for_them = []
    try:
        _entry = _peers.get_peer(actual_peer_id) or {}
        ours_for_them = sorted(_entry.get('shared_projects') or [])
    except Exception:
        ours_for_them = []
    resp_body = {'ok': True, 'peer_id': actual_peer_id,
                 'shared_with_you': ours_for_them}
    if share_refused:
        # The QR's keepalive window (30 s) had lapsed, or its screen
        # was dismissed. Deliberately specific: the fix is "show the
        # project's Share QR again", not anything about the network.
        resp_body['share_refused'] = share_refused
        resp_body['share_refused_reason'] = 'qr_offer_expired'
    resp = _json.dumps(resp_body)
    body_bytes = resp.encode('utf-8')
    start_response('200 OK', [
        ('Content-Type', 'application/json'),
        ('Content-Length', str(len(body_bytes))),
    ])
    print(f'[lan-listener] hello: recorded {actual_peer_id[:8]!r} '
          f'({device_name!r})', file=sys.stderr, flush=True)
    return [body_bytes]


def _read_json_body(environ):
    """Read + parse the JSON body from a WSGI environ. Returns
    ``(payload_dict_or_None, error_msg)``."""
    import json as _json
    try:
        n = int(environ.get('CONTENT_LENGTH', '0') or '0')
        if n > 0:
            raw = environ['wsgi.input'].read(n)
        else:
            raw = b''
        payload = _json.loads(raw.decode('utf-8') or '{}')
    except Exception as ex:
        return None, f'invalid body: {ex!r}'
    if not isinstance(payload, dict):
        return None, 'body must be an object'
    return payload, ''


def _json_response(start_response, status_line, body_dict):
    import json as _json
    body_bytes = _json.dumps(body_dict).encode('utf-8')
    start_response(status_line, [
        ('Content-Type', 'application/json'),
        ('Content-Length', str(len(body_bytes))),
    ])
    return [body_bytes]


def _note_inbound_endpoint(peer_id, environ, payload=None,
                           langcode='', deliver=True):
    """Promote the address this request ARRIVED from to the head of
    the peer's endpoint list (0.54.99).

    Their source host is proven reachable — packets came from it — so
    it belongs first in the list our fan-out reads head-first. The
    listener port isn't the source port, so pair the arrival host with
    a port we already know: the one they advertise in this payload,
    else the port from an endpoint we already hold for them.

    Best-effort and silent on failure: this is an optimisation of which
    address to try first, never a correctness requirement."""
    try:
        host = str(environ.get('REMOTE_ADDR', '') or '').strip()
        if not host or not peer_id:
            return
        port = ''
        claimed = str((payload or {}).get('endpoint', '') or '')
        if ':' in claimed:
            port = claimed.rsplit(':', 1)[1]
        if not port:
            entry = _peers.get_peer(peer_id) or {}
            for ep in (entry.get('endpoints') or []):
                if ':' in str(ep):
                    port = str(ep).rsplit(':', 1)[1]
                    break
        if not port.isdigit():
            return
        _peers.promote_endpoint(peer_id, f'{host}:{port}')
        # ``deliver=False`` for the admin channel (0.55.156): we want the
        # address learned, but an admin request is not a sync trigger and the
        # toggle may be off entirely.
        if deliver:
            _reverse_deliver(peer_id, langcode=langcode)
    except Exception as ex:
        print(f'[lan-listener] endpoint promote raised: {ex!r}',
              file=sys.stderr, flush=True)


def _reverse_deliver_by_addr(environ, path_info):
    """Git-route counterpart of ``_reverse_deliver`` (0.55.10).

    These routes are unauthenticated by design (URL-level ACL, no body
    to claim identity in), so the ONLY signal about who is calling is
    the source address. Match it against paired peers' known endpoints;
    on a hit, treat it exactly like identified contact — look at that
    peer's refs for the project they just asked about, push what we owe,
    fetch what they have.

    Why it matters: without this, serving a fetch taught us nothing, so
    our record of a peer's refs went stale for as long as our own
    dialing failed — producing two devices reporting confidently from
    different hours (field 2026-07-27: "up to date" beside "392 to
    send, 7 hrs old"). The peer is demonstrably present at this
    instant; that is the moment to look.

    Heuristic and deliberately powerless: a match only triggers work we
    would do for that peer anyway. It grants no access and changes no
    allowlist, so a wrong guess costs one cheap sweep, not a
    permission."""
    try:
        host = str(environ.get('REMOTE_ADDR', '') or '').strip()
        if not host:
            return
        norm = str(path_info or '').lstrip('/')
        langcode = (norm.split('.git', 1)[0] if '.git' in norm
                    else norm.split('/', 1)[0])
        if not langcode:
            return
        for peer in (_peers.list_peers() or []):
            for ep in (list(peer.get('endpoints') or [])
                       + list(peer.get('static_endpoints') or [])):
                if str(ep).rsplit(':', 1)[0] == host:
                    pid = peer.get('peer_id', '')
                    # PROMOTE the arrival address before dialing back
                    # (0.55.41). Until now REMOTE_ADDR was used only to
                    # IDENTIFY the caller; the dial then took whatever
                    # ``_candidate_endpoints`` offered first, which could
                    # be — and in the field was — a dead address from a
                    # previous network, while the address that had just
                    # provably carried their request sat further down the
                    # same list.
                    #
                    # Kent 2026-07-27: peer reaches us from
                    # 192.168.124.5, log says "checking … on the address
                    # they just reached us from", and we then dial
                    # 192.168.124.153 twice and fail with EHOSTUNREACH.
                    # The message was aspirational; the code never used
                    # the arrival address. ``_note_inbound_endpoint``
                    # does exactly this promotion but was only wired to
                    # the body-authenticated routes (hello, share_offer),
                    # never to the git routes this fires from.
                    try:
                        _peers.promote_endpoint(pid, str(ep))
                    except Exception as ex:
                        print(f'[lan-listener] promote {ep!r} for '
                              f'{pid[:8]!r} raised: {ex!r}',
                              file=sys.stderr, flush=True)
                    _reverse_deliver(pid, langcode=langcode,
                                     via=str(ep))
                    return
    except Exception as ex:
        print(f'[lan-listener] reverse-by-address raised: {ex!r}',
              file=sys.stderr, flush=True)


def _reverse_deliver(peer_id, langcode='', via=''):
    """"Do I owe this peer anything?" — asked the moment they reach us
    (0.55.5). Scoped to *langcode* when the inbound request names one.

    Kent 2026-07-27: *"would it not be possible to ask, when receiving
    something from a peer: do I have anything for this peer? and if so,
    immediately send it on the address that we know is good?"* Yes, and
    it is the missing half of 0.54.99: that change promotes the address
    a peer just proved reachable, but nothing acted on it.

    Why it matters: reachability is routinely ONE-WAY. A phone whose
    stored address for us is stale (or which can't route to us at all)
    keeps failing its own fan-out — while its inbound requests arrive
    here perfectly. Convergence then waits on the broken direction. But
    at this instant we hold a proven-good address for them, so the
    working direction can carry BOTH directions' data: we push what we
    owe, and ``_push_to_peer`` fetches + merges when they're ahead.

    With a *langcode*, push exactly that project: the peer just told us
    which one they care about, so catching up the rest can wait for the
    ordinary sweep. Without one (a bare hello), fall back to
    ``sweep_peer``, which is debounced per peer and no-ops cheaply when
    they're already current. Either way on a thread, so a listener
    response never waits on git.

    **Rate-limited per (peer, project), and capped in flight
    (0.55.13).** 0.55.10 wired this to the git routes via
    ``_reverse_deliver_by_addr`` with no limiter on the langcode-scoped
    path — ``sweep_peer``'s per-peer debounce covers only the bare-hello
    fallback. Every ``info/refs`` probe therefore spawned a thread that
    dialed the peer; our dial made them serve us, their listener's
    reverse delivery dialed back, and ours fired again. Field
    2026-07-27: ~330 threads in ~10 s on one tablet, every delivery a
    no-op (``already at 'c63ce9251ae5'``), both sides SSLEOFing, and the
    ``:provider`` process dying of exhaustion — twice, on two devices.
    Convergence needs ONE look per contact burst, not one per HTTP
    request, so the cooldown costs nothing real."""
    key = (str(peer_id or ''), str(langcode or ''))
    now = _time.time()
    with _reverse_gate_lock:
        last = _reverse_last_at.get(key, 0.0)
        if now - last < _REVERSE_COOLDOWN_S:
            return
        if key[0] in _reverse_inflight_peers:
            # A dial to THIS peer is already running; a second one is
            # duplicate work. Other peers are unaffected.
            return
        if len(_reverse_inflight_peers) >= _REVERSE_MAX_INFLIGHT_TOTAL:
            print(f'[lan-listener] reverse delivery for '
                  f'{key[0][:8]!r} skipped — global ceiling '
                  f'({len(_reverse_inflight_peers)} peers in flight)',
                  file=sys.stderr, flush=True)
            return
        # Stamp at ADMISSION, not completion: a slow delivery must not
        # leave the gate open for a burst behind it.
        _reverse_last_at[key] = now
        _reverse_inflight_peers.add(key[0])
        if len(_reverse_last_at) > 512:
            for k, t in list(_reverse_last_at.items()):
                if now - t > _REVERSE_COOLDOWN_S * 10:
                    _reverse_last_at.pop(k, None)

    def _work():
        try:
            from . import lan_push as _lan_push
            if langcode:
                from . import projects as _proj
                project = _proj.get(langcode)
                if project is None:
                    return
                # Name the address, and only claim the arrival address
                # when we actually promoted it (0.55.41). The old
                # wording asserted "on the address they just reached us
                # from" unconditionally while the dial could go
                # somewhere else entirely.
                print(f'[lan-listener] reverse delivery: checking '
                      f'{langcode!r} for {peer_id[:8]!r}'
                      + (f' via {via} (their arrival address, promoted '
                         f'to head)' if via else
                         ' (no arrival address; using stored order)'),
                      file=sys.stderr, flush=True)
                entry = _peers.get_peer(peer_id)
                if entry is not None:
                    _lan_push._push_to_peer(project, entry)
                return
            _lan_push.sweep_peer(peer_id)
        except Exception as ex:
            print(f'[lan-listener] reverse delivery for '
                  f'{peer_id[:8]!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
        finally:
            with _reverse_gate_lock:
                _reverse_inflight_peers.discard(key[0])
    try:
        threading.Thread(target=_work, daemon=True,
                         name='lan-reverse-deliver').start()
    except Exception as ex:
        # Never leak the slot the gate above reserved — a spawn failure
        # would otherwise block this peer's reverse delivery forever.
        with _reverse_gate_lock:
            _reverse_inflight_peers.discard(key[0])
        print(f'[lan-listener] reverse delivery thread raised: '
              f'{ex!r}', file=sys.stderr, flush=True)


def _paired_claimant(payload):
    """Resolve the body-auth identity claim in *payload* to a PAIRED
    peer record. Returns ``(peer_id, entry)`` or ``(None, None)``.

    Stricter than the share/hello signalling handlers, which accept
    unpaired callers because pairing is what they're establishing:
    diagnostics-pull and remote-restart are privileged operations, so
    the claimant must ALREADY be paired (a prior QR gesture on this
    device — the consent boundary) and the claimed ``fp`` must match
    the fingerprint we recorded for them. Same body-auth threat model
    as the rest of the listener (identity asserted in the body under
    encrypted-but-unauthenticated TLS; see ``_build_server``)."""
    peer_id = str((payload or {}).get('peer_id', '') or '')
    claimed_fp = str((payload or {}).get('fp', '') or '')
    if len(peer_id) != 64 or len(claimed_fp) != 64:
        return None, None
    try:
        entry = _peers.get_peer(peer_id)
    except Exception as ex:
        print(f'[lan-listener] peer lookup raised: {ex!r}',
              file=sys.stderr, flush=True)
        return None, None
    if entry is None:
        return None, None
    if str(entry.get('fp', '') or '') != claimed_fp:
        return None, None
    return peer_id, entry


def _handle_diagnostics_pull(environ, start_response):
    """``POST /v1/lan/diagnostics_pull`` — serve this device's
    diagnostics bundle to a PAIRED peer over the LAN/cable link
    (0.54.74).

    Why this route exists (field, Kent 2026-07-25): the standard
    Share-diagnostics button needs the owner's server UI to be OPEN,
    which in the field means booting their UI and interrupting their
    work — on someone else's computer, with their tools. Pulling
    inverts it: the technician plugs in, taps once on THEIR OWN
    device, and walks away with the bundle. Pairing was the owner's
    consent gesture; nothing here needs a second one.

    Auth: paired-peer body claim (``_paired_claimant``). Response is
    the raw ``.tar.gz`` with ``X-AZT-Archive-Name`` naming it — the
    same artifact the owner's own Share button produces.

    Deliberately lock-free (see ``server.stage_diagnostics_bundle``):
    the point is to get logs OUT of a wedged daemon."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id, entry = _paired_claimant(payload)
    if peer_id is None:
        print('[lan-listener] diagnostics_pull refused: claimant '
              'not paired (or fp mismatch)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'not_paired'})
    try:
        from . import server as _server
        # Include what we're carrying from OTHER peers, but not the
        # requester's own bundle echoed back (0.54.78) — this is the
        # courier path: a phone that pulled from one machine serves
        # those logs onward to the next, over whatever link is
        # already up.
        staged = _server.stage_diagnostics_bundle(
            exclude_slug=_server.device_slug(
                entry.get('device_name', '')))
    except Exception as ex:
        print(f'[lan-listener] diagnostics_pull staging raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return _json_response(start_response,
                              '500 Internal Server Error',
                              {'ok': False,
                               'error': 'stage_failed'})
    try:
        with open(staged['archive_path'], 'rb') as fh:
            data = fh.read()
    except OSError as ex:
        print(f'[lan-listener] diagnostics_pull read raised: {ex!r}',
              file=sys.stderr, flush=True)
        return _json_response(start_response,
                              '500 Internal Server Error',
                              {'ok': False, 'error': 'read_failed'})
    from azt_collab_client.diagnostics import DIAGNOSTICS_MIME
    start_response('200 OK', [
        ('Content-Type', DIAGNOSTICS_MIME),
        ('Content-Length', str(len(data))),
        ('X-AZT-Archive-Name', staged['archive_name']),
    ])
    print(f'[lan-listener] diagnostics_pull served '
          f'{staged["archive_name"]!r} ({len(data)} bytes) to '
          f'{peer_id[:8]!r} ({entry.get("device_name", "")!r})',
          file=sys.stderr, flush=True)
    return [data]


def _handle_restart_daemon(environ, start_response):
    """``POST /v1/lan/restart_daemon`` — a paired peer asks this
    daemon to restart itself (0.54.74). The remote leg of wedge
    recovery: in the field the fix for a wedged daemon was opening
    the owner's settings UI and tapping Restart server, which is
    exactly the interruption the pull workflow exists to avoid.

    Reaches the **wedged-alive** class only — this listener thread
    responds while scheduler threads are stuck on ``project_lock`` /
    network I/O, which is the common field wedge. A fully dead
    daemon has no listener; desktop clients auto-respawn on their
    next poll and Android's ContentProvider contract lazy-spawns,
    so that case self-heals without us.

    Restart cost is low by design (jobs → typed ``JOB_INTERRUPTED``
    for peer retry, transfers retried by the sender, uncommitted
    bytes power-cut-contained, listener re-binds its previous port,
    backoff curves deliberately survive). Auth: paired-peer body
    claim; the same suite-signature/pairing boundary that already
    authorizes fetch + merge here."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id, entry = _paired_claimant(payload)
    if peer_id is None:
        print('[lan-listener] restart_daemon refused: claimant not '
              'paired (or fp mismatch)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False, 'error': 'not_paired'})
    print(f'[lan-listener] restart_daemon requested by '
          f'{peer_id[:8]!r} ({entry.get("device_name", "")!r})',
          file=sys.stderr, flush=True)

    def _restart_after_response():
        # Same shape as ``server._h_admin_restart``: let the response
        # flush before the process goes away, then reuse that
        # handler's platform-correct teardown (execv on desktop,
        # os._exit under Android's :provider).
        _time.sleep(0.5)
        try:
            from . import server as _server
            _server._h_admin_restart({})
        except Exception as ex:
            print(f'[lan-listener] remote restart raised: {ex!r}',
                  file=sys.stderr, flush=True)

    threading.Thread(target=_restart_after_response,
                     name='lan-remote-restart', daemon=True).start()
    return _json_response(start_response, '200 OK',
                          {'ok': True, 'restarting': True})


def _handle_share_offer_bodyauth(environ, start_response):
    """Body-auth variant of share_offer (TLS client auth disabled,
    see ``_build_server``). Reads the sender's ``peer_id`` from
    the request body and trusts it. Same body shape as before,
    just no cert cross-check."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    return _handle_share_offer(environ, start_response, peer_id,
                               prepared_payload=payload)


def _handle_share_declined_bodyauth(environ, start_response):
    """Body-auth variant of share_declined. Same as the share_offer
    body-auth wrapper — peer_id from body, no cert cross-check."""
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    return _handle_share_declined(environ, start_response, peer_id,
                                  prepared_payload=payload)


def _handle_share_unshared_bodyauth(environ, start_response):
    """Body-auth variant of share_unshared (0.50.44).

    Symmetric-unshare endpoint: phone A's user-tap "unshare X with
    B" gesture POSTs here on B's listener so B can drop A from B's
    own ``shared_projects`` allowlist for X. Without this, A's
    unshare only affected A's outbound fan-out; B's outbound fan-
    out kept firing to A (which A then no-op'd with a logged
    ``carries no repo_url; no-op (already have project)`` line).
    Symmetric unshare closes the asymmetry.

    Distinct from ``share_declined`` (which means "I'm declining
    the offer you just made") — same wire pattern, different
    semantics, separate code so future divergence is cheap.
    """
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    return _handle_share_unshared(environ, start_response, peer_id,
                                  prepared_payload=payload)


def _handle_share_offer(environ, start_response, peer_id,
                       prepared_payload=None):
    """Inbound share-offer handler. Dispatches by local state:

    - **Project not registered locally**: this is the original
      "want to clone from this peer?" path — stash
      ``KIND_SHARE_OFFER``; the user accepts via the decisions
      UI and the LAN clone follows.
    - **Project registered, local ``remote_url`` empty, incoming
      non-empty**: peer is telling us where its github origin
      lives. Stash ``KIND_ADOPT_ORIGIN`` so the user can opt into
      pushing to the same upstream (and so a future peer Publish
      adopts this URL instead of inventing a duplicate). Since
      0.50.27.
    - **Project registered, URLs match**: steady-state ping
      after every peer publishes / shares. Log + no-op so the
      user doesn't see repeated decisions for an already-known
      fact.
    - **Project registered, URLs differ**: fork case — stash
      ``KIND_REMOTE_CONFLICT`` so the user picks via
      ``_h_lan_resolve_conflict``. Since 0.50.27.
    - **Project registered, incoming ``repo_url`` empty**: peer
      doesn't know any URL either; nothing to learn. Log + no-op.

    Pre-0.50.27 behaviour was "always stash ``KIND_SHARE_OFFER``"
    regardless of local state, which double-decisioned every
    already-known share and missed the URL-conflict signal entirely.
    """
    from . import pending_decisions as _pending
    from . import projects as _projects
    if prepared_payload is not None:
        payload = prepared_payload
    else:
        payload, err = _read_json_body(environ)
        if payload is None:
            return _json_response(start_response, '400 Bad Request',
                                  {'ok': False, 'error': err})
    langcode = str(payload.get('langcode', '') or '')
    repo_url = str(payload.get('repo_url', '') or '')
    vernlang = str(payload.get('vernlang', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    if not langcode:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'langcode required'})
    # DECLINED-SHARE SUPPRESSION (0.54.98). This handler used to
    # re-stash an offer on every inbound POST, so a decline only stuck
    # while we could reach the sender to nack it — and the case where
    # we can't (one-way reachability: they reach us, we can't reach
    # them) is precisely the case that keeps re-offering. The offer
    # came back on the sender's next burst, forever. Now a declined
    # (peer, langcode) is dropped here and the nack is re-attempted;
    # the sender rolls back its own allowlist when that lands. Also
    # the prerequisite for the arrival-time share heal — without it,
    # automatic re-offers would make an unwanted offer permanent.
    try:
        if _peers.is_declined_share(peer_id, langcode):
            print(f'[lan-listener] share-offer for {langcode!r} from '
                  f'{peer_id[:8]!r} DROPPED: declined here — '
                  f're-sending the decline',
                  file=sys.stderr, flush=True)
            try:
                from . import lan_push as _lan_push
                _lan_push.share_declined(peer_id, langcode)
            except Exception as ex:
                print(f'[lan-listener] re-nack raised: {ex!r}',
                      file=sys.stderr, flush=True)
            return _json_response(start_response, '200 OK',
                                  {'ok': True,
                                   'dispatch': 'declined_here'})
    except Exception as ex:
        print(f'[lan-listener] decline-suppression check raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
    # Their offer proves this project IS in their allowlist for us, so
    # our side of the share is confirmed mutual (0.54.98).
    try:
        _peers.set_share_confirmed(peer_id, langcode, True)
    except Exception:
        pass
    # RECORD THE PROOF WHERE THE GATE LOOKS (0.55.153).
    #
    # The line above has said "their offer proves this project IS in their
    # allowlist for us" since 0.54.98 — and wrote it to ``shares_confirmed``,
    # which the one-sided-share gate does not read. That gate reads
    # ``their_shared_projects``, written only by the hello manifest.
    #
    # Field 2026-07-30, peer 80570dd9: it offered 'nml' on every sweep while
    # its hello manifest reported ``[]``, so we refused to dial a peer that
    # was actively telling us it shares the project — and its 4705 commits
    # stayed on one machine. Two channels carried the same fact; the gate
    # consulted only the one that was empty.
    try:
        if _peers.add_their_shared_project(peer_id, langcode):
            print(f'[lan-listener] share-offer from {peer_id[:8]!r} for '
                  f'{langcode!r}: recording that they share it with us — an '
                  f'offer is only sent for projects in their allowlist, so '
                  f'this outranks a hello manifest that omitted it',
                  file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[lan-listener] share-offer from {peer_id[:8]!r}: could not '
              f'record their grant of {langcode!r} ({ex!r}) — we may keep '
              f'refusing to dial them for it', file=sys.stderr, flush=True)
    # Scoped to the project they just named (0.55.5): they told us
    # which one they care about, and we now hold an address that
    # provably reaches them.
    _note_inbound_endpoint(peer_id, environ, payload,
                           langcode=langcode)
    try:
        local_proj = _projects.get(langcode)
    except Exception:
        local_proj = None
    # Ghost guard (0.54.63): a registry record whose working tree is
    # GONE (forget-with-delete residue, wiped dir, interrupted clone)
    # must not swallow re-offers forever. Without this, every
    # re-share from the owner dispatched to a silent branch
    # (no_url/noop/adopted) because "the project is registered" —
    # while the picker showed nothing and the user saw no offer
    # (field 2026-07-24: desktop 'shared' + board 'awaiting first
    # sync', client shows neither project nor offer). This handler
    # runs in the daemon process, which owns the files, so the disk
    # check is legitimate here (unlike in peer code). Heal = forget
    # the ghost record, then fall through to the normal
    # stash-clone-offer path.
    if local_proj is not None:
        _wd = str(getattr(local_proj, 'working_dir', '') or '')
        if not _wd or not os.path.isdir(os.path.join(_wd, '.git')):
            print(f'[lan-listener] share-offer for {langcode!r}: '
                  f'registry record is a GHOST (working tree '
                  f'missing at {_wd!r}) — forgetting record, '
                  f'treating offer as fresh', file=sys.stderr,
                  flush=True)
            try:
                from . import repo as _repo_mod
                _repo_mod.forget_project(langcode, delete_files=False)
            except Exception as ex:
                print(f'[lan-listener] ghost forget for '
                      f'{langcode!r} failed: {ex!r}',
                      file=sys.stderr, flush=True)
            local_proj = None
    local_url = ''
    if local_proj is not None:
        local_url = str(getattr(local_proj, 'remote_url', '') or '')
    # ``dispatch`` is echoed back in the JSON response so the
    # *sender* can show meaningful UI feedback. Otherwise every
    # outcome — known-already, freshly-stashed, conflict — looks
    # identical to the sender as a generic 200 OK. Field added in
    # 0.50.43 (additive — older senders ignore it).
    if local_proj is None:
        _pending.add(_pending.KIND_SHARE_OFFER, {
            'peer_id': peer_id,
            'device_name': device_name,
            'langcode': langcode,
            'repo_url': repo_url,
            'vernlang': vernlang,
        })
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r} stashed (clone-offer)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True,
                               'dispatch': 'stashed_share'})
    if not repo_url:
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r} carries no repo_url; no-op '
              '(already have project)',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True, 'dispatch': 'no_url'})
    from .repo import wan_url as _wan_url
    if _wan_url(local_url) == _wan_url(repo_url):
        # Compare wan-normalized so ``git@github.com:o/r.git`` and
        # ``https://github.com/o/r.git`` — the SAME repo in two
        # spellings — never surface as a remote-conflict decision
        # (field repro 2026-07-21: baf, phone popped "two remotes"
        # over one repo). Each side keeps its own stored spelling.
        spelling = ('' if local_url == repo_url
                    else ' (same repo, different spelling)')
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r}: remote_url matches local'
              f'{spelling}; no-op',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True, 'dispatch': 'noop'})
    if not local_url:
        # Auto-accept adopt-origin (0.50.58). The peer has the
        # project locally but no remote_url; the incoming offer
        # carries one. Pre-0.50.58 this stashed a
        # KIND_ADOPT_ORIGIN pending decision and waited for the
        # user to tap "accept" in the picker — which created
        # friction for the unambiguous case (project content
        # already shared via LAN, peer is just supplying the
        # github URL we don't have yet). User already consented
        # to the share by pairing the peer; receiving the URL is
        # the natural completion. Apply synchronously and report
        # ``dispatch='adopted'`` to the sender.
        #
        # KIND_REMOTE_CONFLICT (URLs differ) stays a pending
        # decision because that case is genuinely ambiguous —
        # the daemon can't tell which github repo is "canonical"
        # so the user must pick (keep_mine / use_theirs /
        # dual_publish).
        adopted = False
        try:
            _projects.set_remote_url(langcode, repo_url)
            wd = str(getattr(local_proj, 'working_dir', '') or '')
            if wd:
                from . import repo as _repo
                _repo.set_remote_origin_url(wd, repo_url)
            adopted = True
        except Exception as ex:
            print(f'[lan-listener] auto-adopt-origin for '
                  f'{langcode!r} (peer={peer_id[:8]!r} '
                  f'url={repo_url!r}) failed: {ex!r}',
                  file=sys.stderr, flush=True)
        if adopted:
            print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
                  f'for {langcode!r} auto-adopted origin '
                  f'{repo_url!r}',
                  file=sys.stderr, flush=True)
            # Push-notify any peer observing this project's status
            # URI so the settings UI re-polls and picks up the new
            # ``remote_url`` immediately. Without this the picker
            # holds its cached "publish candidate: remote_url=''"
            # snapshot until the user navigates away and back —
            # field-confirmed in 0.50.60 testing.
            try:
                from .android_cp import notify as _notify
                _notify.notify_project_changed(langcode)
            except Exception:
                pass
            return _json_response(start_response, '200 OK',
                                  {'ok': True,
                                   'dispatch': 'adopted'})
        # On failure, fall back to the pre-0.50.58 stash so the
        # user has a manual path to retry via the picker.
        _pending.add(_pending.KIND_ADOPT_ORIGIN, {
            'peer_id': peer_id,
            'device_name': device_name,
            'langcode': langcode,
            'url': repo_url,
        })
        print(f'[lan-listener] share-offer from {peer_id[:8]!r} '
              f'for {langcode!r} stashed (adopt-origin '
              f'{repo_url!r}) — auto-adopt failed above',
              file=sys.stderr, flush=True)
        return _json_response(start_response, '200 OK',
                              {'ok': True,
                               'dispatch': 'stashed_adopt_origin'})
    _pending.add(_pending.KIND_REMOTE_CONFLICT, {
        'peer_id': peer_id,
        'device_name': device_name,
        'langcode': langcode,
        'existing_url': local_url,
        'incoming_url': repo_url,
    })
    print(f'[lan-listener] share-offer from {peer_id[:8]!r} for '
          f'{langcode!r} stashed (remote-conflict '
          f'local={local_url!r} incoming={repo_url!r})',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK',
                          {'ok': True,
                           'dispatch': 'stashed_conflict'})


def _handle_share_declined(environ, start_response, peer_id,
                           prepared_payload=None):
    """Inbound nack handler. The peer we shared *langcode* with
    declined. Pull them out of our shared_projects allowlist for
    that langcode so the listener stops advertising it. (Refusal
    doesn't unpair them; it just rolls back the share.)"""
    if prepared_payload is not None:
        payload = prepared_payload
    else:
        payload, err = _read_json_body(environ)
        if payload is None:
            return _json_response(start_response, '400 Bad Request',
                                  {'ok': False, 'error': err})
    langcode = str(payload.get('langcode', '') or '')
    if not langcode:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'langcode required'})
    try:
        _peers.remove_shared_project(peer_id, langcode)
    except Exception as ex:
        print(f'[lan-listener] remove_shared_project raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
    print(f'[lan-listener] {peer_id[:8]!r} declined share for '
          f'{langcode!r}; allowlist rolled back',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _handle_share_unshared(environ, start_response, peer_id,
                           prepared_payload=None):
    """Inbound symmetric-unshare handler (0.50.44). The sender's
    user has unshared *langcode* on their side; mirror that on
    ours by removing the sender from *our* ``shared_projects``
    allowlist for that langcode. Idempotent: if the sender isn't
    in our allowlist for this langcode, this is a no-op."""
    if prepared_payload is not None:
        payload = prepared_payload
    else:
        payload, err = _read_json_body(environ)
        if payload is None:
            return _json_response(start_response, '400 Bad Request',
                                  {'ok': False, 'error': err})
    langcode = str(payload.get('langcode', '') or '')
    if not langcode:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'langcode required'})
    try:
        _peers.remove_shared_project(peer_id, langcode)
    except Exception as ex:
        print(f'[lan-listener] symmetric unshare '
              f'remove_shared_project raised: {ex!r}',
              file=sys.stderr, flush=True)
    print(f'[lan-listener] {peer_id[:8]!r} symmetric-unshared '
          f'{langcode!r}; mirrored allowlist removal',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _handle_cawl_fetch_bodyauth(environ, start_response):
    """Serve a CAWL image byte stream over LAN to a paired peer
    (NOTES #3, since 0.50.14).

    Body: ``{peer_id, fp, owner, repo, rel_path}`` — same
    body-auth shape as the other signalling endpoints (peer_id/fp
    lookup against ``peers.json``; no TLS-layer client auth, see
    ``_build_server`` for why). ``rel_path`` is the full nested
    path inside the repo (e.g. ``0001_body/foo.png``); a flat
    basename is also accepted and canonicalized via the local
    index. Sending the full ``rel_path`` is preferred because
    same-basename-different-variant entries (two ``foo.png`` files
    in different id directories) need to be disambiguated for the
    "fetch all variants over LAN" case.

    Response:
      - 200 ``application/octet-stream`` with the bytes if we have
        them cached locally.
      - 404 JSON if we don't have the byte cached.
      - 403 JSON if the caller isn't a paired peer.
      - 400 JSON on malformed body.

    Why a separate endpoint vs. piggybacking on the existing
    dulwich git fallthrough: CAWL images aren't tracked in any
    project's git tree (they live under ``$AZT_HOME/cawl/...``,
    a daemon-private directory) so they're invisible to dulwich's
    smart-protocol app. A purpose-built byte server is the
    minimum surface to expose them.
    """
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    fp = str(payload.get('fp', '') or '')
    owner = str(payload.get('owner', '') or '').strip()
    repo = str(payload.get('repo', '') or '').strip()
    rel_path = str(
        payload.get('rel_path') or payload.get('basename') or ''
    ).strip()
    if len(peer_id) != 64 or len(fp) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id / fp wrong length'})
    if not owner or not repo or not rel_path:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'owner / repo / rel_path '
                                        'required'})
    # Peer auth: must be in peers.json with the claimed fp.
    entry = _peers.get_peer(peer_id)
    if entry is None or str(entry.get('fp', '') or '') != fp:
        return _json_response(start_response, '403 Forbidden',
                              {'ok': False,
                               'error': 'not_paired_or_fp_mismatch'})
    # rel_path safety: no leading slash (absolute), no ``..`` for
    # traversal, no backslashes, no hidden-file leading dot.
    # ``/`` BETWEEN components is fine (and expected) for nested
    # rel_paths like ``0001_body/foo.png``.
    if ('\\' in rel_path or '..' in rel_path
            or rel_path.startswith('.') or rel_path.startswith('/')):
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'bad_rel_path'})
    if '/' in owner or '/' in repo:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'bad_owner_or_repo'})
    from . import cawl as _cawl
    repo_slug = f'{owner}/{repo}'
    # Flat basename (no '/'): canonicalize via local index. With a
    # nested rel_path the requester has already done the
    # disambiguation — use it directly.
    if '/' not in rel_path:
        rel_path, _found = _cawl._resolve_basename_via_index(
            repo_slug, rel_path)
    target = _cawl.image_path(repo_slug, rel_path)
    if target is None or not os.path.isfile(target):
        return _json_response(start_response, '404 Not Found',
                              {'ok': False, 'error': 'not_cached'})
    try:
        with open(target, 'rb') as f:
            body = f.read()
    except OSError as ex:
        return _json_response(start_response, '500 Internal Error',
                              {'ok': False,
                               'error': f'read_failed: {ex!r}'})
    start_response('200 OK', [
        ('Content-Type', 'application/octet-stream'),
        ('Content-Length', str(len(body))),
    ])
    return [body]


def _handle_pair_request(environ, start_response):
    """Inbound Nearby-pair request from an unpaired device.

    Stashes a KIND_PAIR_REQUEST pending decision; the shared
    decisions watcher renders the popup on next poll. Body:
    ``{peer_id, fp, device_name, endpoint, langcode?}``.

    Accepts unpaired callers (this IS the gesture by which they
    become paired). Body must self-validate by carrying its own
    peer_id + fp (the peer_id IS the ed25519 pubkey, so the
    body claim is what TLS would have verified anyway under our
    CERT_NONE setup — see lan_listener._build_server for why).
    """
    from . import pending_decisions as _pending
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    fp = str(payload.get('fp', '') or '')
    device_name = str(payload.get('device_name', '') or '')
    endpoint = str(payload.get('endpoint', '') or '')
    langcode = str(payload.get('langcode', '') or '')
    if len(peer_id) != 64 or len(fp) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id / fp wrong length'})
    # The address this request actually ARRIVED from (0.54.96). Packets
    # came from there, so it is PROVEN reachable — unlike the
    # body-claimed ``endpoint``, which is only the sender's guess about
    # its own address and is routinely wrong on a multi-homed host
    # (field 2026-07-27: a desktop with four tether subnets plus wifi
    # advertised one address the phone couldn't reach, so the accepter's
    # hello-back and pair_response both failed and the pairing went
    # one-sided). REMOTE_ADDR carries the ephemeral SOURCE port, not
    # the listener port, so the resolver pairs this host with the
    # advertised port.
    from_addr = str(environ.get('REMOTE_ADDR', '') or '')
    endpoints = [str(e) for e in (payload.get('endpoints') or [])
                 if e]
    _pending.add(_pending.KIND_PAIR_REQUEST, {
        'peer_id': peer_id, 'fp': fp,
        'device_name': device_name, 'endpoint': endpoint,
        'endpoints': endpoints, 'from_addr': from_addr,
        'langcode': langcode,
    })
    print(f'[lan-listener] pair-request from {peer_id[:8]!r} '
          f'({device_name!r}) stashed',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _handle_pair_response(environ, start_response):
    """Inbound response to an outbound pair-request we sent.

    Body: ``{peer_id, accept: bool}``. Sender-side dispatch
    only updates the in-memory outbound-requests state; the
    actual peer record (if accept=True) is recorded when the
    receiver's hello-back lands via the normal hello flow.
    """
    from . import lan_pair_requests as _lpr
    payload, err = _read_json_body(environ)
    if payload is None:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False, 'error': err})
    peer_id = str(payload.get('peer_id', '') or '')
    accept = bool(payload.get('accept', False))
    if len(peer_id) != 64:
        return _json_response(start_response, '400 Bad Request',
                              {'ok': False,
                               'error': 'peer_id wrong length'})
    _lpr.record_response(peer_id, accept)
    print(f'[lan-listener] pair-response from {peer_id[:8]!r}: '
          f'{"accept" if accept else "decline"}',
          file=sys.stderr, flush=True)
    return _json_response(start_response, '200 OK', {'ok': True})


def _peer_acl_middleware(app):
    """WSGI middleware: extract peer-id from the verified client
    cert (captured into ``environ`` by ``_CertCapturingHandler``);
    look it up in ``peers.json``; restrict the URL set to that
    peer's ``shared_projects`` before forwarding to dulwich.web.

    Short-circuits POST ``/v1/lan/hello`` to ``_handle_hello`` —
    that endpoint deliberately accepts unpaired callers as the
    auto-reverse-record half of the pairing flow.

    Rejects with 403 on:
      - missing peer cert (shouldn't happen — ``CERT_REQUIRED``
        rejects at TLS, but defensively handle)
      - unknown peer_id (not paired, not a hello)
      - fp mismatch (paired peer's cert fingerprint differs from
        the value recorded in peers.json)
      - request URL outside the peer's shared_projects allowlist
    """
    def wrapped(environ, start_response):
        # Identity at TLS layer is currently disabled (see
        # ``_build_server`` for the rationale: stdlib ssl has no
        # "request cert but skip CA validation" mode). Peer identity
        # is asserted via the request body for the signalling
        # endpoints (hello / share_offer / share_declined); for the
        # git smart-protocol fallthrough we accept any caller on
        # the LAN and gate the URL set by the union of every
        # paired peer's ``shared_projects`` (project must be
        # shared with at least one paired peer to be served).
        # FUTURE-HARDEN: move client identity into a signed-
        # message header (ed25519 sig over the request) so paired
        # peers can be cryptographically identified per-request.
        method = environ.get('REQUEST_METHOD')
        path_info = environ.get('PATH_INFO', '')
        # ADMIN-ONLY DOOR (0.55.155). When ``lan.allow_sync`` is off we may
        # still be bound, serving nothing but the remote-settings channel,
        # so a device whose user switched LAN off can be switched back on
        # remotely. Everything else is refused here — no git smart-protocol,
        # no hello, no share offers, no diagnostics pull. "Off" still means
        # no sync; it stops meaning "unreachable forever".
        if _STATE.get('admin_only') and path_info not in (
                '/v1/lan/challenge', '/v1/lan/admin'):
            return _json_response(
                start_response, '403 Forbidden',
                {'ok': False, 'error': 'lan_sync_off',
                 'detail': 'LAN sync is off on this device; only the '
                           'remote-settings channel is served'})
        # Signalling endpoints accept unpaired callers; identity
        # claim lives in the body. They self-validate by checking
        # the body's ``peer_id``/``fp`` match each other (the
        # peer_id IS the ed25519 pubkey).
        # Challenge + admin (0.55.102). STRICT identity: nonce +
        # ed25519 signature, no unsigned fallback. Safe to be strict
        # from the first line because admin is NEW — no existing client
        # speaks it, so there is nothing to stay compatible with. The
        # signalling endpoints below keep accepting unsigned callers so
        # a staged APK rollout doesn't cut off peers still on old code.
        if method == 'GET' and path_info == '/v1/lan/challenge':
            return _handle_challenge(environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/admin':
            return _handle_admin(environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/hello':
            return _handle_hello_bodyauth(environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/share_offer':
            return _handle_share_offer_bodyauth(
                environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/share_declined':
            return _handle_share_declined_bodyauth(
                environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/share_unshared':
            return _handle_share_unshared_bodyauth(
                environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/pair_request':
            return _handle_pair_request(environ, start_response)
        if method == 'POST' and path_info == '/v1/lan/pair_response':
            return _handle_pair_response(environ, start_response)
        if (method == 'POST'
                and path_info == '/v1/lan/cawl_fetch'):
            return _handle_cawl_fetch_bodyauth(
                environ, start_response)
        # Privileged paired-peer routes (0.54.74): unlike the
        # signalling endpoints above, these require an ALREADY-paired
        # claimant (``_paired_claimant``).
        if (method == 'POST'
                and path_info == '/v1/lan/diagnostics_pull'):
            return _handle_diagnostics_pull(environ, start_response)
        if (method == 'POST'
                and path_info == '/v1/lan/restart_daemon'):
            return _handle_restart_daemon(environ, start_response)
        # Non-signalling fallthrough: dulwich.web's git smart-
        # protocol app. URL-level ACL is handled at backend-build
        # time — ``_build_dict_backend`` only mounts projects that
        # appear in at least one paired peer's ``shared_projects``,
        # so the dulwich app simply returns 404 for a URL outside
        # that set. Future-harden by re-adding the per-peer ACL
        # once client identity is signature-verified in the body.
        # Log who is asking — TLS gives no per-request identity, so
        # the remote address is the only requester signal we have,
        # and without it a serve of ``/X.git`` is unattributable in
        # the daemon log (field, 2026-07-17: could not tell which of
        # two paired machines fetched a project).
        # Identify the fetcher by ARRIVAL ADDRESS and look at them in
        # return (0.55.10). The git smart-protocol routes carry no
        # identity — no body, and TLS is CERT_NONE — so a peer fetching
        # from us taught us nothing, and our knowledge of THEIR refs
        # could only advance when WE managed to dial THEM. Field
        # 2026-07-27: a phone reporting "up to date" (fresh observation,
        # it can reach the tablet) beside a tablet reporting "392 to
        # send" stamped 7 h old — the tablet had served the phone's
        # fetches all along and never got to look back. Matching
        # REMOTE_ADDR against paired peers' endpoints is a heuristic
        # (NAT, shared hosts), so it only ever triggers the same
        # look-and-deliver we already do on identified contact; it
        # grants nothing and changes no ACL.
        _reverse_deliver_by_addr(environ, path_info)
        print(f'[lan-listener] {method} {path_info} from '
              f'{environ.get("REMOTE_ADDR", "?")}',
              file=sys.stderr, flush=True)
        # Stash the caller for ``open_repository``'s per-peer ACL
        # (0.55.47) — it's a backend method with no ``environ``.
        # Thread-local is sound here: ThreadingMixIn gives one thread
        # per request. Set unconditionally so a previous request's
        # identity on a reused thread can never leak into this one.
        _acl_ctx.peer_id = _identify_peer_by_addr(environ)
        _acl_ctx.remote = environ.get('REMOTE_ADDR', '') or ''
        # Which DIRECTION is this? ``git-upload-pack`` (and the
        # ``info/refs?service=git-upload-pack`` probe) is the peer
        # FETCHING FROM us — that's serving, and our grant governs it.
        # ``git-receive-pack`` is the peer PUSHING TO us — that's
        # accepting, and our grant has nothing to say about it. The old
        # union ACL conflated the two; strict per-peer enforcement
        # applied to a receive would refuse a delivering peer and break
        # inbound sync, which is the opposite of the intent (0.55.48).
        _p = path_info or ''
        _q = environ.get('QUERY_STRING', '') or ''
        _acl_ctx.accepting = ('git-receive-pack' in _p
                              or 'git-receive-pack' in _q)
        return app(environ, start_response)
    return wrapped


def _raise_dulwich_chunk_limit():
    """Lift dulwich's 1 MiB single-chunk receive cap (0.55.21).

    Field 2026-07-27: every `nml` push between two of Kent's machines
    died with ``dulwich.web.ChunkedEncodingError('Chunk size exceeds
    maximum allowed')`` — identically, on every retry, for hours, while
    the sync board showed the pair happily seeing each other.

    Upstream mismatch, not our bug. ``dulwich/web.py`` caps one
    dechunked chunk at 1 MiB, reasoning that the smart-http protocol
    emits at most ~64 KiB pkt-lines. But dulwich's own ``HttpGitClient``
    sends a push body as a single write under ``Transfer-Encoding:
    chunked``, so urllib3 emits ONE chunk carrying the whole packfile.
    Any repo whose pack exceeds 1 MiB can therefore never be pushed to a
    dulwich server — small histories squeak under and large ones fail
    forever.

    ``MAX_CHUNK_SIZE`` cannot be monkeypatched: ``_chunk_iter`` takes it
    as a DEFAULT ARGUMENT, bound at def time. ``ChunkReader.__init__``
    calls the module-global ``_chunk_iter``, so replacing that name is
    what takes effect.

    The cap's purpose — stop a hostile peer forcing one huge allocation —
    is preserved, just at a workable size, and dulwich's separate 1 GiB
    whole-body cap still applies. Our peers are fingerprint-pinned,
    allowlisted devices on a LAN, so the residual exposure is a paired
    device asking for one large buffer. Tunable via
    ``lan.max_chunk_bytes`` so a field case never waits on a rebuild."""
    try:
        from dulwich import web as _dweb
    except Exception as ex:
        print(f'[lan-listener] dulwich.web unavailable; chunk cap '
              f'unpatched: {ex!r}', file=sys.stderr, flush=True)
        return
    if getattr(_dweb, '_azt_chunk_cap_raised', False):
        return
    orig = getattr(_dweb, '_chunk_iter', None)
    if orig is None:
        print('[lan-listener] dulwich.web._chunk_iter absent — cap '
              'patch skipped (dulwich version without the cap?)',
              file=sys.stderr, flush=True)
        return
    try:
        want = int(_settings.get('lan.max_chunk_bytes',
                                 128 * 1024 * 1024) or 0)
    except Exception:
        want = 128 * 1024 * 1024
    if want <= 0:
        return
    total_cap = getattr(_dweb, 'MAX_REQUEST_SIZE', 1024 ** 3)

    def _chunk_iter_patched(f, max_chunk_size=None, max_total_size=None):
        return orig(f,
                    max_chunk_size=(want if max_chunk_size is None
                                    else max_chunk_size),
                    max_total_size=(total_cap if max_total_size is None
                                    else max_total_size))

    _dweb._chunk_iter = _chunk_iter_patched
    _dweb._azt_chunk_cap_raised = True
    print(f'[lan-listener] dulwich receive chunk cap raised '
          f'{getattr(_dweb, "MAX_CHUNK_SIZE", "?")} → {want} bytes',
          file=sys.stderr, flush=True)


_EARLY_HANGUP_WINDOW_S = 300.0
_EARLY_HANGUP_ESCALATE_AT = 5
_early_hangups = []          # unix timestamps, trimmed to the window
_early_hangup_lock = threading.Lock()


def _note_early_hangup():
    """Count peer-hangup-before-response events and escalate on a burst.

    Kent 2026-07-27: *"You seem unconcerned by the ssl.SSLEOFError; will
    that not bite us some day?"* — a fair challenge. One of these is
    ordinary on a mobile LAN: a screen sleeps, a phone walks out of
    range, a socket dies with no ``close_notify``. That is why it must
    not print nine frames. But it is NOT always benign, and the case
    that proves it is in the same log: the hangup at 15:48:30 followed
    the ``nml`` chunk failure, i.e. it was the TAIL OF OUR OWN ERROR —
    the body parse failed, wsgiref went to write a 500, and the sender
    had already given up. Silencing that outright would hide the
    consequence of a real bug.

    So the event stays one line and the RATE becomes the signal: a
    handful in five minutes is not weather, and says so in a form a
    log search for ``[data-quality]`` will surface."""
    try:
        now = _time.time()
        with _early_hangup_lock:
            _early_hangups.append(now)
            while _early_hangups and \
                    now - _early_hangups[0] > _EARLY_HANGUP_WINDOW_S:
                _early_hangups.pop(0)
            n = len(_early_hangups)
            burst = (n >= _EARLY_HANGUP_ESCALATE_AT
                     and n % _EARLY_HANGUP_ESCALATE_AT == 0)
        if burst:
            print(f'[data-quality] lan-early-hangups n={n} '
                  f'window={int(_EARLY_HANGUP_WINDOW_S)}s — peers are '
                  f'dropping connections before our response lands. '
                  f'Not ordinary churn at this rate: look for a '
                  f'server-side failure just before each one (a '
                  f'lan-receive-hard-failure, a 500, or a stall)',
                  file=sys.stderr, flush=True)
    except Exception:
        pass


def _install_wsgiref_quiet_disconnects():
    """Collapse peer-disconnect tracebacks that escape the body
    generator (0.55.21).

    0.54.68 wrapped the response ITERATION, which covers a peer that
    vanishes mid-packfile. But wsgiref writes the response HEADERS
    outside that generator, and a peer that hangs up before they land
    raises there instead — field 2026-07-27 15:48, a bare nine-frame
    ``ssl.SSLEOFError`` traceback through ``finish_response`` →
    ``finish_content`` → ``send_headers`` → ``send_preamble``, with no
    ``[lan-listener]`` line to explain it.

    ``BaseHandler.run`` swallows only ``ConnectionAbortedError`` /
    ``BrokenPipeError`` / ``ConnectionResetError``; ``SSLEOFError`` is an
    ``OSError`` but none of those, so it reaches ``handle_error`` →
    ``log_exception``, which dumps the traceback. Patching
    ``log_exception`` process-wide is safe here: the LAN listener is the
    only wsgiref user in this process, and it beats reimplementing
    ``WSGIRequestHandler.handle`` to inject a ServerHandler subclass
    (which would drift with the stdlib)."""
    try:
        from wsgiref import handlers as _wh
    except Exception:
        return
    if getattr(_wh.BaseHandler, '_azt_quiet_disconnects', False):
        return
    orig = _wh.BaseHandler.log_exception

    def log_exception(self, exc_info):
        ex = exc_info[1] if exc_info else None
        quiet = False
        try:
            # Self-contained on purpose: the middleware's
            # ``_is_peer_disconnect`` is nested inside another function
            # and is NOT reachable from here. Referencing it would have
            # raised NameError into the ``except`` below, silently
            # falling through to the nine-frame dump this exists to
            # prevent — a fix that quietly does nothing.
            cur, depth = ex, 0
            while cur is not None and depth < 5:
                if isinstance(cur, (ssl.SSLEOFError,
                                    ssl.SSLZeroReturnError,
                                    ConnectionResetError,
                                    ConnectionAbortedError,
                                    BrokenPipeError)):
                    quiet = True
                    break
                cur = getattr(cur, '__cause__', None) \
                    or getattr(cur, '__context__', None)
                depth += 1
        except Exception:
            quiet = False
        if quiet:
            where = ''
            try:
                env = getattr(self, 'environ', None) or {}
                where = (f' {env.get("REQUEST_METHOD", "?")} '
                         f'{env.get("PATH_INFO", "?")} from '
                         f'{env.get("REMOTE_ADDR", "?")}')
            except Exception:
                pass
            print(f'[lan-listener] peer hung up before the response '
                  f'was written: {type(ex).__name__}{where}',
                  file=sys.stderr, flush=True)
            _note_early_hangup()
            return
        return orig(self, exc_info)

    _wh.BaseHandler.log_exception = log_exception
    _wh.BaseHandler._azt_quiet_disconnects = True


def _build_handler_class():
    """Subclass the stdlib WSGI request handler so each request's
    WSGI environ carries the verified peer cert (DER) extracted from
    the underlying ``ssl.SSLSocket``. ``WSGIRequestHandler`` is the
    portable base; dulwich's ``WSGIRequestHandlerLogger`` would
    do but isn't present in every dulwich version (the same
    refactor that removed ``HTTPGitServer`` may have hidden it
    too), so we just use the stdlib class and route logs to
    stderr via ``log_message`` override."""
    from wsgiref.simple_server import WSGIRequestHandler

    class _CertCapturingHandler(WSGIRequestHandler):
        def log_message(self, fmt, *args):
            # Cheap silent logger — peer requests are normal; we
            # don't need them in stderr unless debugging.
            pass

        def get_environ(self):
            environ = super().get_environ()
            try:
                sock = self.request
                if isinstance(sock, ssl.SSLSocket):
                    der = sock.getpeercert(binary_form=True)
                    if der:
                        environ['aztcollab.peer_cert_der'] = der
            except Exception:
                pass
            return environ

    return _CertCapturingHandler


# Per-project deferred-reset queue. When the post-receive reset
# below times out trying to acquire ``project_lock`` (the tablet's
# own outgoing ``_merge_then_push`` workflow can hold it for >5 s,
# longer than the receive-pack handler's tolerance), we add the
# langcode to this set. The scheduler watcher's tick drains the
# set by retrying ``_reset_working_tree_after_receive``; on
# success, the function removes its own entry. ``_commit_repo_locked``
# (in repo.py) also drains its own langcode at the top of every
# commit attempt, so the next commit_project absorbs the pending
# reset BEFORE staging — otherwise ``_stage_all`` sees the files
# that the merge brought in as "missing from working tree" and
# commits a *delete* for them, erasing the merge. Persisted to
# ``$AZT_HOME/pending_resets.json`` so a daemon restart while
# there's still a deferred reset on the queue doesn't lose track.
# Loaded back in ``scheduler.reconcile_on_startup``.
_PENDING_RESETS_FILENAME = 'pending_resets.json'
_pending_post_receive_resets = set()
_pending_resets_lock = threading.Lock()


def _pending_resets_path():
    from .paths import azt_home
    return os.path.join(azt_home(), _PENDING_RESETS_FILENAME)


def _save_pending_resets_locked():
    """Atomic-write the pending-resets set. Caller holds
    ``_pending_resets_lock``."""
    p = _pending_resets_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f'{p}.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w') as f:
            json.dump(sorted(_pending_post_receive_resets), f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except Exception as ex:
        print(f'[lan-listener] pending-resets save failed: {ex!r}',
              file=sys.stderr, flush=True)


def _add_pending_reset(langcode):
    """Mark *langcode* as needing a deferred post-receive reset."""
    with _pending_resets_lock:
        if langcode in _pending_post_receive_resets:
            return
        _pending_post_receive_resets.add(langcode)
        _save_pending_resets_locked()


def _remove_pending_reset(langcode):
    """Clear *langcode* from the deferred-reset queue."""
    with _pending_resets_lock:
        if langcode not in _pending_post_receive_resets:
            return
        _pending_post_receive_resets.discard(langcode)
        _save_pending_resets_locked()


def has_pending_reset(langcode):
    """Public predicate — used by ``repo._commit_repo_locked`` to
    decide whether to absorb a pending reset before staging."""
    with _pending_resets_lock:
        return langcode in _pending_post_receive_resets


def load_pending_resets_from_disk():
    """Re-populate the in-memory set from
    ``$AZT_HOME/pending_resets.json`` after a daemon restart. Called
    from ``scheduler.reconcile_on_startup``. Idempotent."""
    p = _pending_resets_path()
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as ex:
        print(f'[lan-listener] pending-resets load failed: {ex!r}',
              file=sys.stderr, flush=True)
        return
    if not isinstance(data, list):
        return
    with _pending_resets_lock:
        for entry in data:
            if isinstance(entry, str):
                _pending_post_receive_resets.add(entry)
    if data:
        print(f'[lan-listener] pending-resets loaded from disk: '
              f'{sorted(_pending_post_receive_resets)!r}',
              file=sys.stderr, flush=True)


_reset_hard_failures = {}      # langcode → {count, next_at, detail}
_RESET_BACKOFF_START_S = 60.0
_RESET_BACKOFF_MAX_S = 900.0


def _note_reset_hard_failure(langcode, ex):
    """Back off a post-receive reset that fails for a NON-transient
    reason, and say so once per escalation (0.55.30).

    Field 2026-07-27: `post-receive reset 'nml' failed:
    KeyError(b'dcf320a8951496fd909c91f5159fbe03cfd8f65c')` every ~15 s
    without end — the object the reset needs is absent from the store, so
    every retry fails identically. Same shape as the chunk cap in
    0.55.21: a deterministic failure wearing transient clothing.

    **The queue entry is deliberately KEPT.** Dropping it would be worse
    than the flood: a project whose working tree never gets reset to HEAD
    shows the received merge as *deleted* in status, and the next
    ``commit_project`` would stage that deletion and erase the merge (see
    the comment at the ``_add_pending_reset`` call site, and the absorbing
    half in ``repo._commit_repo_locked``). So we keep retrying — just on
    a 60 s → 15 min curve instead of every tick — and make the condition
    legible rather than letting it hide in a repeating line."""
    detail = repr(ex)
    with _pending_resets_lock:
        st = _reset_hard_failures.get(langcode) or {
            'count': 0, 'next_at': 0.0, 'detail': ''}
        st['count'] += 1
        st['detail'] = detail
        delay = min(_RESET_BACKOFF_MAX_S,
                    _RESET_BACKOFF_START_S * (2 ** (st['count'] - 1)))
        st['next_at'] = _time.time() + delay
        _reset_hard_failures[langcode] = st
        count, wait = st['count'], delay
    missing = ''
    if isinstance(ex, KeyError) and ex.args:
        raw = ex.args[0]
        try:
            missing = (raw.decode('ascii', 'replace')
                       if isinstance(raw, bytes) else str(raw))[:40]
        except Exception:
            missing = ''
    if missing:
        print(f'[data-quality] reset-blocked-missing-object '
              f'langcode={langcode!r} object={missing} '
              f'failures={count} — the working tree cannot be reset to '
              f'HEAD because this object is not in the store, so '
              f'received data stays unabsorbed. Retrying in '
              f'{wait:.0f}s. This does not self-heal: the object has to '
              f'be recovered from a peer that still has it',
              file=sys.stderr, flush=True)
        info = _log_missing_object_classification(langcode, missing)
        # REPAIR LADDER (0.55.57), cheapest and most exact first.
        #
        # 1. Rebuild a missing TREE from the working directory. Trees are
        #    content-addressed, so this either reproduces the exact sha
        #    or writes nothing. Free, local, and the only option that
        #    works when NO peer still holds the object — which is the
        #    situation here: `dcf320a895…` is missing on more than one
        #    device.
        # 2. Roll the ref back off an incomplete tip (0.55.56) so the
        #    sender re-delivers. Only helps when the gap is recent;
        #    `heal-failed: no complete commit within 200` says it isn't.
        try:
            if info and info.get('kind') == 'tree':
                for _p in (info.get('paths') or [])[:1]:
                    if _rebuild_missing_tree_from_worktree(
                            langcode, missing, _p):
                        clear_reset_hard_failure(langcode)
                        print(f'[heal] {langcode!r}: object restored — '
                              f'the next drain pass will retry the '
                              f'working-tree reset',
                              file=sys.stderr, flush=True)
                        return
        except Exception as ex:
            print(f'[heal] {langcode!r} tree rebuild raised: {ex!r}',
                  file=sys.stderr, flush=True)
        try:
            if _heal_incomplete_tip(langcode):
                clear_reset_hard_failure(langcode)
                _remove_pending_reset(langcode)
        except Exception as ex:
            print(f'[heal] {langcode!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
    else:
        print(f'[data-quality] reset-blocked langcode={langcode!r} '
              f'failures={count} detail={detail} — retrying in '
              f'{wait:.0f}s', file=sys.stderr, flush=True)


_unservable_refs_logged = {}     # {langcode: frozenset of dropped names}


def _hide_unservable_refs(langcode, repo):
    """Omit refs we cannot serve from this repo's advertisement
    (0.55.99).

    **One bad ref poisons the whole fetch.** A peer reads
    ``info/refs``, asks for everything advertised, and dulwich's
    ``determine_wants`` raises ``GitProtocolError: Client wants invalid
    object`` for the first sha we don't actually hold — so the peer gets
    NOTHING, including the refs we could have served perfectly well.

    Field 2026-07-29, Idjop's desktop, third distinct sha in two days
    (``7315ccfa`` → ``303950c4`` → ``594127cd``): the aztobt2-ui phone
    could not fetch ``nml`` at all, though only one ref was broken. Since
    that machine keeps minting new unservable refs, "wait for it to be
    repaired" is not a strategy — a damaged repo has to stay useful for
    the parts that are intact, or LAN convergence stops for everyone who
    talks to it.

    ``HEAD`` is never filtered: if HEAD itself is unservable the repo is
    beyond partial rescue, and dropping it would break the protocol
    rather than degrade it. 0.55.63's warning already names that case.

    Membership-only test (no reachability walk) — this runs on every
    served request, and the failure it prevents is exactly a sha the
    store does not contain."""
    try:
        original = repo.get_refs
    except Exception:
        return

    def _filtered():
        refs = original()
        try:
            keep, dropped = {}, []
            for name, sha in (refs or {}).items():
                if name == b'HEAD' or not sha:
                    keep[name] = sha
                    continue
                try:
                    if sha in repo.object_store:
                        keep[name] = sha
                    else:
                        dropped.append(name)
                except Exception:
                    keep[name] = sha        # can't tell → keep offering
            if dropped:
                marker = frozenset(dropped)
                if _unservable_refs_logged.get(langcode) != marker:
                    _unservable_refs_logged[langcode] = marker
                    names = sorted(
                        n.decode('utf-8', 'replace') for n in dropped)
                    print(f'[data-quality] hiding-unservable-refs '
                          f'langcode={langcode!r} count={len(dropped)} '
                          f'{names[:6]!r} — their commit objects are not '
                          f'in our store, so advertising them makes every '
                          f'peer fetch fail outright ("Client wants '
                          f'invalid object"). Serving the rest instead',
                          file=sys.stderr, flush=True)
            return keep
        except Exception:
            return refs                     # never break the advertisement

    try:
        repo.get_refs = _filtered
    except Exception:
        pass


_unservable_head_logged = {}     # {langcode: last head reported}


def _flag_received_work_for_backup(langcode):
    """Mark *langcode* as needing a github push after a peer's work has
    landed here (0.55.70).

    ``pending_push`` was raised only by LOCAL commit / publish paths, so a
    receive-pack that fast-forwarded our head left it clear — and the
    drain, whose only trigger is that flag, honestly reported "nothing
    pending" while sitting well ahead of ``origin/main``. Field evidence:
    ``origin/main`` at ``2ed963b4`` under a long run of commits authored
    on a peer, received, merged, and never offered to github.

    Kent: *"once I FF or merge, it is now my head. If my head is behind
    github, I don't push, just because I got it from someone else?"*
    Provenance is not a reason. github here is off-site durability, so the
    consequence of the gap was data that existed only on devices staying
    that way while internet was available.

    0.55.69 taught the user-gesture path to ask ``_wan_unshared``
    directly, which is the more correct question. This is the cheap
    counterpart for the PERIODIC path: the 15 s tick can't afford a
    history walk per project, but it can honour a flag, so raising one
    here means received work gets backed up without waiting for someone
    to tap Sync.

    Never raises: a backup hint must not be able to fail a receive that
    already succeeded."""
    try:
        from . import scheduler as _scheduler
        _scheduler._set_pending_push(langcode, True)
        print(f'[lan-listener] {langcode!r}: flagged for github backup — '
              f'received work advances our head, so it needs backing up '
              f'the same as anything we committed ourselves',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[lan-listener] {langcode!r}: could not flag for github '
              f'backup: {ex!r} — a nudge will still catch it via the '
              f'ahead-of-github check', file=sys.stderr, flush=True)


def _warn_if_advertising_unservable_head(langcode, repo):
    """Name it when we are advertising a head we cannot serve (0.55.63).

    Field 2026-07-28, and this is what finally pinned it down. The tablet
    peeked a Windows peer and was told its head was `303950c45456`; ninety
    seconds later that peer's own log:

        dulwich.errors.GitProtocolError:
            Client wants invalid object b'303950c4545696825f666a77c11a29d1e35277c0'

    Same sha. The peer **advertised it in `info/refs` and then refused it
    from `upload-pack`** — so this was never a client asking for the wrong
    thing. A device in that state poisons every peer that talks to it: the
    peek succeeds, the fetch 500s, the merge can't run, and nothing in
    either log says the advertised ref was the problem.

    A cheap object-store membership test on the head, once per served
    request. Not full reachability — that would walk the history on every
    poll; this catches the case that actually bites, where the commit the
    ref names is itself absent.

    Deliberately does NOT refuse the request. Read-only diagnosis: the
    honest repair is the ladder in ``_note_reset_hard_failure``, and a
    peer whose head is merely *unreachable-deep* can still legitimately
    serve older refs. Rate-limited to once per changed head, because
    something producing a new broken head every two minutes would
    otherwise flood the log it needs to be visible in."""
    try:
        head = repo.refs.get(b'HEAD') or repo.refs.get(b'refs/heads/main')
    except Exception:
        return
    if not head:
        return
    try:
        if head in repo.object_store:
            if _unservable_head_logged.pop(langcode, None):
                print(f'[data-quality] head-servable-again '
                      f'langcode={langcode!r} head={head[:12].decode()}',
                      file=sys.stderr, flush=True)
            return
    except Exception:
        return
    hex_head = head[:12].decode('ascii', 'replace')
    if _unservable_head_logged.get(langcode) == hex_head:
        return
    _unservable_head_logged[langcode] = hex_head
    print(f'[data-quality] advertising-unservable-head '
          f'langcode={langcode!r} head={hex_head} — this ref is in our '
          f'advertisement but the commit object is NOT in our store, so '
          f'every peer that peeks us will fetch and get "Client wants '
          f'invalid object". Our writes are not landing: check whether '
          f'the repo is on cloud-synced storage (OneDrive / Dropbox) '
          f'whose files cannot rehydrate offline, or an antivirus '
          f'quarantining new git objects',
          file=sys.stderr, flush=True)


def _rebuild_missing_tree_from_worktree(langcode, want_sha, rel_path):
    """Reconstruct a missing TREE object from the working directory
    (0.55.57).

    Git objects are content-addressed, so this either reproduces exactly
    *want_sha* — closing the gap with no network — or it doesn't, and we
    add nothing. There is no way for it to write a wrong object.

    Kent's objection to this on 2026-07-28 was correct at the time: the
    post-receive reset never ran, so the working tree holds the OLD
    content, which wouldn't match a newly-arrived tree. What changed is
    the evidence — `heal-failed: no complete commit within 200` with
    `commits_walked=448` means this tree has been referenced across
    hundreds of commits. It did not newly arrive; it **vanished from the
    store**, retroactively breaking everything that referenced it. And a
    tree unchanged for 400+ commits is one whose on-disk content is very
    likely still exactly right.

    It also matters that the same object is missing on more than one
    device: a scratch-clone repair needs SOME peer to still hold it, and
    if none does, local reconstruction is the only route left.

    Returns True only if *want_sha* was produced and stored."""
    import os as _os
    import stat as _stat
    from dulwich.repo import Repo
    from dulwich.objects import Blob, Tree
    from . import projects as _projects_mod
    project = _projects_mod.get(langcode)
    if project is None or not project.working_dir:
        return False
    want = (want_sha.encode('ascii') if isinstance(want_sha, str)
            else want_sha)
    root = _os.path.join(project.working_dir, rel_path)
    if not _os.path.isdir(root):
        print(f'[heal] {langcode!r}: {rel_path!r} is not a directory on '
              f'disk — cannot rebuild its tree',
              file=sys.stderr, flush=True)
        return False
    repo = None
    try:
        repo = Repo(project.working_dir)
    except Exception as ex:
        print(f'[heal] {langcode!r}: open repo failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    staged = []          # objects to add only if the root sha matches

    def _build(path):
        """Return the Tree object for *path*, collecting new objects."""
        tree = Tree()
        for name in sorted(_os.listdir(path)):
            full = _os.path.join(path, name)
            nb = name.encode('utf-8')
            st = _os.lstat(full)
            if _stat.S_ISDIR(st.st_mode):
                sub = _build(full)
                tree.add(nb, 0o040000, sub.id)
            elif _stat.S_ISLNK(st.st_mode):
                blob = Blob.from_string(
                    _os.readlink(full).encode('utf-8'))
                staged.append(blob)
                tree.add(nb, 0o120000, blob.id)
            else:
                with open(full, 'rb') as f:
                    blob = Blob.from_string(f.read())
                staged.append(blob)
                mode = (0o100755 if st.st_mode & _stat.S_IXUSR
                        else 0o100644)
                tree.add(nb, mode, blob.id)
        staged.append(tree)
        return tree

    try:
        try:
            built = _build(root)
        except Exception as ex:
            print(f'[heal] {langcode!r}: rebuilding {rel_path!r} raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            return False
        if built.id != want:
            print(f'[data-quality] tree-rebuild-mismatch '
                  f'langcode={langcode!r} path={rel_path!r} '
                  f'want={want.decode()} got={built.id.decode()} — the '
                  f'working copy differs from what that tree recorded, '
                  f'so the content is genuinely gone. Nothing written; '
                  f'needs the scratch-clone repair from a peer that '
                  f'still holds it', file=sys.stderr, flush=True)
            return False
        added = 0
        for obj in staged:
            try:
                if obj.id not in repo.object_store:
                    repo.object_store.add_object(obj)
                    added += 1
            except Exception as ex:
                print(f'[heal] {langcode!r}: storing {obj.id!r} raised: '
                      f'{ex!r}', file=sys.stderr, flush=True)
                return False
        print(f'[data-quality] tree-rebuilt langcode={langcode!r} '
              f'path={rel_path!r} sha={want.decode()} '
              f'objects_added={added} — reconstructed byte-identically '
              f'from the working tree; the object store had lost it '
              f'while the files were intact',
              file=sys.stderr, flush=True)
        return True
    finally:
        try:
            repo.close()
        except Exception:
            pass


def _heal_incomplete_tip(langcode, max_commits=200):
    """Roll a ref back off commits whose objects never fully arrived, so
    the sender re-delivers them (0.55.56).

    Kent 2026-07-28: *"looks like something we should be doing anyway:
    heal bad transfers."* Right — this is a mechanism, not a repair for
    one project. An interrupted receive can leave refs advanced onto
    commits whose objects are only partly present, and such a tip is
    **unusable in both directions**:

    - the working-tree reset dies (`KeyError(b'dcf320a895…')`), so nothing
      is ever absorbed;
    - upload-pack refuses every fetch — `GitProtocolError: Client wants
      invalid object b'7315ccfa…'` — which is the `HTTP 500` every peer
      saw, deterministically, whatever the load.

    Probable origin here: the chunk-cap failures (fixed 0.55.21) aborting
    `add_thin_pack` mid-completion. Thin packs are completed by the
    RECEIVER, so an interrupted completion is exactly how refs end up
    ahead of the objects.

    Walks back from the tip for the newest commit whose tree enumerates
    cleanly and CASes the ref to it. Nothing is lost that was ever usable,
    and the peer still holds what it sent.

    **Refuses if any discarded commit is locally authored.** A local
    commit's objects are present by construction, so a broken range
    should contain none — but if it does, this would be data loss and a
    human has to decide. Bounded at *max_commits*; a gap deeper than that
    needs the scratch-clone repair instead."""
    from dulwich.repo import Repo
    from . import projects as _projects_mod
    from . import store as _store_mod
    project = _projects_mod.get(langcode)
    if project is None or not project.working_dir:
        return False
    try:
        me = (_store_mod.get_contributor() or '').strip()
    except Exception:
        me = ''
    repo = None
    try:
        repo = Repo(project.working_dir)
    except Exception as ex:
        print(f'[heal] {langcode!r}: open repo failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    try:
        try:
            tip = repo.refs[b'HEAD']
        except Exception:
            return False
        discarded, target, sha = [], None, tip
        for _ in range(max_commits):
            try:
                commit = repo[sha]
            except Exception:
                break                   # commit itself absent — keep going back
            complete = True
            try:
                for _e in repo.object_store.iter_tree_contents(commit.tree):
                    if _e.sha not in repo.object_store:
                        complete = False
                        break
            except Exception:
                complete = False        # tree itself unreadable
            if complete:
                target = sha
                break
            discarded.append((sha, commit))
            parents = list(commit.parents or [])
            if not parents:
                break
            sha = parents[0]
        if target is None:
            print(f'[data-quality] heal-failed langcode={langcode!r}: no '
                  f'complete commit within {max_commits} — needs the '
                  f'scratch-clone repair, not a rollback',
                  file=sys.stderr, flush=True)
            return False
        if not discarded:
            return False                # tip was fine; nothing to heal
        if me:
            for _sha, _c in discarded:
                try:
                    author = (_c.author or b'').decode('utf-8', 'replace')
                except Exception:
                    author = ''
                if me and me in author:
                    print(f'[data-quality] heal-REFUSED '
                          f'langcode={langcode!r}: '
                          f'{_sha[:12].decode()} is locally authored '
                          f'({author!r}) and its objects are incomplete. '
                          f'Rolling back would lose local work — a human '
                          f'must decide. Not touching refs',
                          file=sys.stderr, flush=True)
                    return False
        ff_ref = b'refs/heads/main'
        try:
            from dulwich import porcelain as _porc
            active = _porc.active_branch(repo)
            if active:
                ff_ref = b'refs/heads/' + active
        except Exception:
            pass
        moved = False
        try:
            moved = bool(repo.refs.set_if_equals(ff_ref, tip, target))
            if moved and repo.refs[b'HEAD'] != target:
                repo.refs.set_if_equals(b'HEAD', tip, target)
        except Exception as ex:
            print(f'[heal] {langcode!r}: ref rollback failed: {ex!r}',
                  file=sys.stderr, flush=True)
            return False
        if not moved:
            return False
        print(f'[data-quality] healed-incomplete-tip '
              f'langcode={langcode!r} from={tip[:12].decode()} '
              f'to={target[:12].decode()} discarded='
              f'{[s[:12].decode() for s, _ in discarded]!r} — those '
              f'commits arrived without all their objects (interrupted '
              f'transfer). The sender still holds them and will '
              f're-deliver',
              file=sys.stderr, flush=True)
        return True
    finally:
        try:
            repo.close()
        except Exception:
            pass


def _log_missing_object_classification(langcode, sha_hex):
    """Say what the absent object IS, once per escalation (0.55.31).

    Kent 2026-07-27: *"let's see what it is first, then find where else
    it is."* Recovery needs a source peer, and which peers could possibly
    have it depends on what it is — a missing commit is a history gap any
    descendant-holder can supply, while a missing blob is one file's
    content. Runs on the device that is already reproducing, so no manual
    step is needed; the 60 s → 15 min backoff is what keeps it cheap."""
    try:
        from . import projects as _proj
        from . import repo as _repo_mod
        project = _proj.get(langcode)
        if project is None:
            return
        info = _repo_mod.classify_missing_object(
            project.working_dir, sha_hex)
        print(f'[data-quality] missing-object-identity '
              f'langcode={langcode!r} object={info.get("object", "")} '
              f'kind={info.get("kind", "?")!r} '
              f'referrers={info.get("referrers", [])[:4]!r} '
              f'paths={info.get("paths", [])[:4]!r} '
              f'commits_walked={info.get("walked", 0)} '
              f'capped={info.get("capped", False)}',
              file=sys.stderr, flush=True)
        return info
    except Exception as ex:
        print(f'[data-quality] missing-object-identity failed for '
              f'{langcode!r}: {ex!r}', file=sys.stderr, flush=True)
    return None


def _reset_backoff_active(langcode):
    """True while a hard-failing langcode is still inside its backoff."""
    with _pending_resets_lock:
        st = _reset_hard_failures.get(langcode)
        if not st:
            return False
        return _time.time() < float(st.get('next_at', 0) or 0)


def clear_reset_hard_failure(langcode):
    """Forget a langcode's hard-failure curve — call after a successful
    reset, or after a repair that could plausibly have supplied the
    missing object."""
    with _pending_resets_lock:
        _reset_hard_failures.pop(langcode, None)


def drain_pending_resets():
    """Retry each langcode in the deferred-reset queue. Called from
    the scheduler watcher tick. Each retry goes through
    ``_reset_working_tree_after_receive`` again; on success the
    function removes its own queue entry, on continued LockTimeout
    it re-adds it (no-op in that case). Other exceptions are logged
    and the entry stays on the queue for the next tick."""
    with _pending_resets_lock:
        pending = list(_pending_post_receive_resets)
    if not pending:
        return
    for langcode in pending:
        if _reset_backoff_active(langcode):
            continue
        try:
            # WAIT for the lock here (0.55.53). The 5 s default exists
            # because the first attempt runs inside a WSGI worker, where
            # blocking would stall the peer's HTTP request. This retry
            # runs on the scheduler's watcher thread — nothing is
            # waiting on it — so a short timeout buys nothing and costs
            # everything: on a busy tablet every pass timed out and the
            # working tree never reconciled.
            #
            # Field 2026-07-28, tablet 841d43a8: `lock busy (5s
            # timeout)` nine times in twelve minutes while `nml`'s
            # project_lock was continuously contended. Each incoming
            # push advanced its refs and the reset never completed, so
            # the device accumulated ~25 head positions it could not
            # consolidate — which is why every one of them looked
            # non-ancestor to the desktop.
            _reset_working_tree_after_receive(langcode,
                                             lock_timeout_s=120)
        except Exception as ex:
            print(f'[lan-listener] drain_pending_resets '
                  f'{langcode!r}: {ex!r}',
                  file=sys.stderr, flush=True)


def _reset_working_tree_after_receive(langcode, lock_timeout_s=5):
    """After an incoming receive-pack advances HEAD via a push
    from a peer, sync this peer's working tree + index to the
    new HEAD. Without this, dulwich's receive-pack updates refs
    without touching the working tree, and every file in the
    incoming commits shows as ``staged_mod`` indefinitely (index
    matches old state, HEAD points at new tree). Field symptom
    (baf 2026-05-22): ``n_changes`` jumps by hundreds-to-
    thousands after each fast-forward push, never clears until
    a subsequent ``commit_project`` happens to absorb the
    mismatch into a commit.

    Hard reset is the right semantic here: a successful
    receive-pack means the incoming changes are now canonically
    HEAD; the working tree should reflect that. The
    ``project_lock`` serializes us against any concurrent
    ``commit_project`` / ``atomic_finalize`` (those acquire the
    same lock), so a concurrent local edit can't land at the
    moment we reset. Short timeout (5 s): if the lock is busy
    longer than that, defer rather than block the WSGI worker;
    the next ``commit_project`` will absorb the mismatch the
    old (pre-0.45.35) way. 0.45.35."""
    from . import projects as _projects
    from .locks import project_lock, LockTimeout
    from dulwich import porcelain
    from dulwich.repo import Repo

    project = _projects.get(langcode)
    if project is None or not project.working_dir:
        return
    # Atomic-pending in-flight guard. Phase 1 of the peer's
    # ``atomic_open_write`` writes bytes to
    # ``.azt_atomic_pending/<token>`` via a raw ContentProvider FD
    # — no project_lock held during the write itself. Phase 2 (the
    # ``atomic_finalize`` RPC) DOES take the lock. Between those
    # two steps, this post-receive reset can race in: it acquires
    # the lock, runs ``porcelain.reset(mode='hard')``, releases.
    # If dulwich's reset clobbers the in-flight ``<token>`` file
    # (observed in the field as ``SERVER_ERROR: pending_not_found``
    # surfacing in the peer's ``stop_recording`` path, baf
    # 2026-05-22), the peer's Phase 2 fails — and worse, the
    # recorder UI hangs in "still recording" because its post-
    # stop state transition aborted on the save error.
    #
    # The guard: defer the reset if any
    # ``.azt_atomic_pending/<token>`` is younger than the
    # ``atomic_recovery._MIN_AGE_S`` threshold (60 s) — i.e., a
    # Phase 1 write that might still be mid-flight. The next
    # incoming push (or the next ``commit_project``) will absorb
    # the index/HEAD mismatch the old way. Worst case: ``n_changes``
    # stays inflated until the next push, which is the pre-0.45.36
    # behavior — strictly no worse than before. 0.45.38.
    pending_dir = os.path.join(project.working_dir,
                               '.azt_atomic_pending')
    if os.path.isdir(pending_dir):
        try:
            from . import atomic_recovery as _ar
            min_age = _ar._MIN_AGE_S
            now = _time.time()
            youngest_age = None
            for name in os.listdir(pending_dir):
                p = os.path.join(pending_dir, name)
                try:
                    age = now - os.stat(p).st_mtime
                except OSError:
                    continue
                if youngest_age is None or age < youngest_age:
                    youngest_age = age
            if youngest_age is not None and youngest_age < min_age:
                print(f'[lan-listener] post-receive reset '
                      f'{langcode!r}: deferred — pending-write in '
                      f'flight (youngest {youngest_age:.1f}s, '
                      f'threshold {min_age:.0f}s)',
                      file=sys.stderr, flush=True)
                return
        except Exception as ex:
            print(f'[lan-listener] pending-age guard raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            # Fall through — better to do the reset than skip
            # silently when the guard itself broke.
    try:
        with project_lock(project.working_dir,
                          timeout=lock_timeout_s):
            repo = Repo(project.working_dir)
            try:
                # 0.45.39 Phase-2 guard: defer if the working tree
                # has any non-pending unstaged modifications. The
                # 0.45.38 guard above only covers Phase 1 (scratch
                # tokens under .azt_atomic_pending/). Once a peer's
                # atomic_finalize completes — os.replace moves the
                # token to the final path — the scratch is gone, so
                # the age-guard misses it, but the final file is now
                # on disk with new bytes that ``commit_project``
                # hasn't yet picked up. A ``reset --hard HEAD`` here
                # would silently revert the just-landed LIFT (or
                # audio) edit to its old HEAD content — silent data
                # loss. Defer instead; the next ``commit_project``
                # absorbs the index/HEAD mismatch the old (pre-
                # 0.45.36) way. Worst case is the ghost ``n_changes``
                # spike persists until the next commit — strictly no
                # worse than pre-0.45.36 and recoverable.
                try:
                    st = porcelain.status(repo, untracked_files='no')
                    unstaged_paths = list(st.unstaged or [])
                except Exception as ex:
                    print(f'[lan-listener] status-guard raised: '
                          f'{ex!r}', file=sys.stderr, flush=True)
                    unstaged_paths = []
                if unstaged_paths:
                    pending_prefix = b'.azt_atomic_pending/'
                    orphan_prefix = b'.azt_atomic_orphans/'
                    real_mods = [
                        p for p in unstaged_paths
                        if not (p.startswith(pending_prefix)
                                or p.startswith(orphan_prefix))]
                    if real_mods:
                        # 0.45.44: instead of deferring (which left
                        # the next commit_project to silently revert
                        # the incoming peer's content), three-way
                        # merge HEAD's tree into the working tree.
                        # Working tree ends up with both sides'
                        # edits; next commit creates a proper merge
                        # commit on top of HEAD. See
                        # ``repo.integrate_head_into_working_tree``.
                        head = []
                        for p in real_mods[:3]:
                            try:
                                head.append(
                                    p.decode('utf-8', 'replace'))
                            except Exception:
                                head.append(repr(p))
                        print(f'[lan-listener] post-receive '
                              f'{langcode!r}: {len(real_mods)} '
                              f'unstaged mod(s) — merging HEAD into '
                              f'working tree (head={head!r})',
                              file=sys.stderr, flush=True)
                        try:
                            from . import repo as _repo_mod
                            applied, n_conflicts = (
                                _repo_mod.integrate_head_into_working_tree(
                                    repo, project.working_dir))
                            if applied:
                                print(f'[lan-listener] post-receive '
                                      f'{langcode!r}: merge applied '
                                      f'(conflicts={n_conflicts}); '
                                      f'next commit_project will '
                                      f'land the merged result',
                                      file=sys.stderr, flush=True)
                                return
                            # Fell through (first commit etc.); fall
                            # back to the deferred path so the next
                            # commit at least preserves working tree.
                            print(f'[lan-listener] post-receive '
                                  f'{langcode!r}: merge bailed; '
                                  f'deferring to next commit_project',
                                  file=sys.stderr, flush=True)
                            return
                        except Exception as ex:
                            # Merge raised: safer to defer than to
                            # leave the working tree in a half-merged
                            # state.
                            print(f'[lan-listener] post-receive '
                                  f'{langcode!r}: integrate raised '
                                  f'{ex!r}; deferring',
                                  file=sys.stderr, flush=True)
                            return
                # Re-attach HEAD as symref to refs/heads/main if
                # they've decoupled (since 0.46.5). Field-observed
                # merge-loop:
                #   - ``_merge_diverged`` on the LOCAL side calls
                #     ``worktree.commit(merge_heads=[...])`` which
                #     advances HEAD's pointer (symref or detached).
                #     On some flows HEAD ends up detached at our
                #     last merge SHA.
                #   - Incoming receive-pack updates ONLY
                #     ``refs/heads/main`` via ``set_if_equals``;
                #     HEAD's detached value is untouched.
                #   - Result: HEAD = our last merge, main = peer's
                #     last push. Each drain we see "peer at <main>",
                #     local HEAD at <our merge>, FF check fails,
                #     produce another degenerate merge, push,
                #     repeat. Loop never terminates because neither
                #     side's HEAD ever realigns with the converged
                #     main.
                # Fix: when HEAD is detached and main descends from
                # (or equals) HEAD's value, re-attach HEAD as
                # symref to refs/heads/main. HEAD's content is then
                # a subset of main's, no data loss. After the
                # re-attach, the next drain on this side sees
                # local_head == peer's HEAD (both equal to main),
                # no-op short-circuits, loop ends.
                main_ref = b'refs/heads/main'
                try:
                    symrefs = repo.refs.get_symrefs()
                    head_target = symrefs.get(b'HEAD')
                except Exception:
                    head_target = None
                if head_target != main_ref:
                    try:
                        main_sha = repo.refs[main_ref]
                        head_sha_raw = repo.refs[b'HEAD']
                    except KeyError:
                        main_sha = None
                        head_sha_raw = None
                    if main_sha and head_sha_raw \
                            and main_sha != head_sha_raw:
                        # Check ancestry: HEAD's value reachable
                        # from main's history (main = HEAD's
                        # descendant). Walk main's ancestry looking
                        # for HEAD's SHA.
                        head_is_ancestor = False
                        try:
                            for entry in repo.get_walker(
                                    include=[main_sha]):
                                if entry.commit.id == head_sha_raw:
                                    head_is_ancestor = True
                                    break
                        except Exception:
                            head_is_ancestor = False
                        if head_is_ancestor:
                            try:
                                repo.refs.set_symbolic_ref(
                                    b'HEAD', main_ref)
                                print(f'[lan-listener] '
                                      f'{langcode!r}: re-attached '
                                      f'HEAD as symref to '
                                      f'refs/heads/main '
                                      f'(was detached at '
                                      f'{head_sha_raw[:12].decode()}'
                                      f'; main at '
                                      f'{main_sha[:12].decode()})',
                                      file=sys.stderr, flush=True)
                            except Exception as ex:
                                print(f'[lan-listener] '
                                      f'{langcode!r}: re-attach '
                                      f'failed: {ex!r}',
                                      file=sys.stderr, flush=True)
                        else:
                            # Audit finding #4 (0.50.15): ancestry
                            # check failed or walker raised. The
                            # re-attach is unsafe (main is NOT a
                            # descendant of HEAD; rewriting HEAD to
                            # main would silently drop the work
                            # at HEAD). Pre-0.50.15 this fell
                            # through silently and the merge-loop
                            # could resume on the next receive.
                            # Emit a structured log line — same
                            # format as other [data-quality] tags
                            # so a daemon-log search surfaces it.
                            print(f'[data-quality] '
                                  f'head-detached-no-reattach '
                                  f'langcode={langcode!r} '
                                  f'head={head_sha_raw[:12].decode()} '
                                  f'main={main_sha[:12].decode()} '
                                  f'reason=main-not-descendant-of-head',
                                  file=sys.stderr, flush=True)
                head_sha = repo.refs[b'HEAD']
                porcelain.reset(repo, mode='hard', treeish=head_sha)
                print(f'[lan-listener] post-receive reset '
                      f'{langcode!r} → HEAD '
                      f'({head_sha[:12].decode()})',
                      file=sys.stderr, flush=True)
                # Success: clear any prior deferred-reset entry for
                # this langcode. (Idempotent — no-op if not queued.)
                _remove_pending_reset(langcode)
                clear_reset_hard_failure(langcode)
                # HEAD advanced + working tree changed; push-notify
                # observers so they re-poll project_status without
                # waiting for the next background tick.
                try:
                    from .android_cp import notify as _notify
                    _notify.notify_project_changed(langcode)
                except Exception:
                    pass
                # Post-receive peer-SHA refresh (0.50.50). Receive-
                # pack didn't tell us WHICH paired peer pushed, but
                # the pusher must be at our new HEAD (they couldn't
                # have pushed a SHA they don't have). Without this
                # refresh, our ``last_seen_main[<every-paired-peer>]
                # [langcode]`` stays at whatever it was before, so
                # ``_lan_unshared`` walks excluding stale peer SHAs
                # and reports the just-received commit as "unshared"
                # — visible as LAN-1 on a project both phones are
                # actually in sync on. ls-remote each paired peer
                # sharing this langcode; update last_seen_main for
                # the ones matching our new HEAD. Background thread
                # so we don't block the WSGI worker.
                try:
                    new_sha_hex = head_sha.decode('ascii', 'replace')
                    threading.Thread(
                        target=_refresh_peer_last_seen_after_receive,
                        args=(langcode, new_sha_hex),
                        daemon=True,
                        name='lan-post-receive-refresh').start()
                except Exception as ex:
                    print(f'[lan-listener] post-receive refresh '
                          f'spawn raised: {ex!r}',
                          file=sys.stderr, flush=True)
            finally:
                try:
                    repo.close()
                except Exception:
                    pass
    except LockTimeout:
        # Lock holder is someone else's project_lock (typically this
        # device's own outgoing ``_merge_then_push`` workflow, which
        # holds the lock through the entire merge — often >5 s).
        # Queue the langcode so the scheduler's watcher tick can
        # retry, and so ``_commit_repo_locked`` can absorb it before
        # staging on the next commit_project. Without this, the
        # working tree stays out of sync with HEAD indefinitely,
        # showing as ghost ``n_changes`` (the merge files appear as
        # "deleted" in working-tree status); worse, the next
        # commit_project would stage that "delete" and erase the
        # merge. See repo._commit_repo_locked for the absorbing
        # half of this fix.
        _add_pending_reset(langcode)
        # Report the ACTUAL timeout and WHICH path timed out (0.55.54).
        # This hardcoded "5s" in its text, so after 0.55.53 gave the
        # drain retry a 120 s wait the line still claimed 5 s — and the
        # two callers were indistinguishable. Kent was reading exactly
        # this line to judge whether 0.55.53 was live.
        _which = ('drain retry' if lock_timeout_s > 5
                  else 'first attempt, in the WSGI worker')
        print(f'[lan-listener] post-receive reset {langcode!r}: lock '
              f'busy after {lock_timeout_s}s ({_which}) — queued for '
              f'retry on next scheduler tick + absorb on next '
              f'commit_project', file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[lan-listener] post-receive reset {langcode!r} '
              f'failed: {ex!r}',
              file=sys.stderr, flush=True)
        _note_reset_hard_failure(langcode, ex)


def _refresh_peer_last_seen_after_receive(langcode, new_head_sha_hex):
    """Post-receive last_seen_main refresh (0.50.50).

    After our listener accepts a push, walk every paired peer
    whose ``shared_projects`` contains *langcode* and ask them
    (via ls-remote) what SHA they hold. For any peer whose main
    matches our new HEAD, update ``last_seen_main[peer][langcode]``
    so ``_lan_unshared`` no longer reports the just-received
    commit as unshared. Runs on a worker thread off the WSGI
    request path; per-peer failures are isolated.

    Cost: one ls-remote per paired peer sharing the project.
    Fast-fail gate makes recently-unreachable peers free skips.

    Why we don't update peers whose main is BEHIND our HEAD:
    those peers may have a stale view (they pushed to us earlier
    but haven't seen our subsequent commits) OR they genuinely
    are behind. Either way we don't know they have the new SHA,
    so leaving their ``last_seen_main`` where it was is the
    "OK on uncertainty" answer — same convention the helper
    families follow."""
    try:
        from . import peers as _peers
        from . import lan_push as _lan_push
    except Exception as ex:
        print(f'[lan-listener] post-receive refresh dispatch '
              f'raised: {ex!r}', file=sys.stderr, flush=True)
        return
    try:
        candidates = []
        for entry in _peers.list_peers() or []:
            pid = entry.get('peer_id', '') or ''
            if not pid:
                continue
            shared = entry.get('shared_projects') or []
            if langcode not in shared:
                continue
            candidates.append(pid)
    except Exception as ex:
        print(f'[lan-listener] post-receive refresh candidate '
              f'enumeration raised: {ex!r}',
              file=sys.stderr, flush=True)
        return
    if not candidates:
        return
    matched = []
    for pid in candidates:
        try:
            peer_sha = _lan_push.peek_peer_head(pid, langcode)
        except Exception as ex:
            print(f'[lan-listener] post-receive refresh peek '
                  f'{pid[:8]!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        if peer_sha and peer_sha == new_head_sha_hex:
            try:
                _peers.set_peer_last_seen_main(
                    pid, langcode, peer_sha)
                # The peer is AT our new head — that's confirmed
                # containment of our own commit; record the
                # covered-local coverage the sync-status walkers
                # fall back to when this peer's head later moves
                # somewhere we haven't fetched.
                _peers.set_peer_covered_local(
                    pid, langcode, peer_sha)
                matched.append(pid[:8])
            except Exception as ex:
                print(f'[lan-listener] post-receive refresh '
                      f'set_peer_last_seen_main {pid[:8]!r} '
                      f'raised: {ex!r}',
                      file=sys.stderr, flush=True)
    print(f'[lan-listener] post-receive refresh '
          f'{langcode!r}: peers_sharing={len(candidates)} '
          f'at-our-HEAD={len(matched)} ({matched!r})',
          file=sys.stderr, flush=True)


_POST_RECEIVE_PATH_RE = None


def _post_receive_pack_middleware(inner_app):
    """WSGI middleware: catch successful receive-pack POSTs and
    schedule a working-tree reset for the affected project. See
    ``_reset_working_tree_after_receive`` for the why."""
    import re
    global _POST_RECEIVE_PATH_RE
    if _POST_RECEIVE_PATH_RE is None:
        _POST_RECEIVE_PATH_RE = re.compile(
            r'^/([^/]+)\.git/git-receive-pack$')

    def _is_peer_disconnect(ex):
        """True for the exception shapes a peer vanishing
        mid-transfer produces: raw socket resets, or dulwich's
        ``GitProtocolError`` wrapping one (raised ``from`` the socket
        error, so ``__cause__`` carries the original — field
        2026-07-24: upload-pack negotiation reset when the phone
        hopped networks printed a 40-line double traceback)."""
        if isinstance(ex, (ConnectionResetError, BrokenPipeError)):
            return True
        cause = getattr(ex, '__cause__', None)
        return isinstance(cause,
                          (ConnectionResetError, BrokenPipeError))

    def _wrapped(environ, start_response):
        method = environ.get('REQUEST_METHOD', '')
        path = environ.get('PATH_INFO', '')
        m = (_POST_RECEIVE_PATH_RE.match(path)
             if method == 'POST' else None)
        if m is None:
            # Non-receive routes (upload-pack serving a peer's fetch,
            # info/refs, …) get the same disconnect-absorbing wrap as
            # the receive path below: a peer that vanishes mid-
            # transfer is ONE log line, not a traceback. Anything
            # that isn't a disconnect re-raises untouched.
            result = inner_app(environ, start_response)

            def _absorb():
                try:
                    for chunk in result:
                        yield chunk
                except Exception as ex:
                    if not _is_peer_disconnect(ex):
                        raise
                    print(f'[lan-listener] {path!r} interrupted '
                          f'(peer disconnected mid-transfer; they '
                          f'will retry): {ex!r}',
                          file=sys.stderr, flush=True)
                finally:
                    if hasattr(result, 'close'):
                        try:
                            result.close()
                        except Exception:
                            pass
            return _absorb()

        langcode = m.group(1)
        status_holder = [None]

        def _is_hard_transfer_error(ex):
            """True when a receive failure is DETERMINISTIC — a limit or
            a framing violation — not a peer that walked away (0.55.19).

            Both classes arrive as ``OSError`` subclasses, so they were
            being reported identically: ``requests.ChunkedEncodingError``
            inherits ``RequestException`` → ``IOError`` → ``OSError`` and
            landed in the disconnect branch. But "Chunk size exceeds
            maximum allowed" is a SIZE CAP being hit; it will recur
            byte-for-byte on every retry, so "sender will retry" reads as
            reassurance about a loop that cannot terminate.

            Field 2026-07-27: 'nml' receive-pack from 10.191.129.100
            failed this way at 14:31:15, 14:32:27 and 14:38:32 — same
            error each time, no convergence, while the board showed the
            pair happily seeing each other. Kent: *"I'm a bit unclear why
            i have two computers on my desk who each claim to see the
            other, who haven't shared data."* This is why."""
            text = f'{ex!r} {ex}'.lower()
            return ('exceeds maximum' in text
                    or 'chunk size' in text
                    or 'too large' in text
                    or 'maximum allowed' in text)

        def _log_hard_transfer_error(langcode, ex):
            print(f'[data-quality] lan-receive-hard-failure '
                  f'langcode={langcode!r} error={ex!r} — NOT a '
                  f'disconnect: this recurs identically on every '
                  f'retry, so this project cannot converge over LAN '
                  f'until the cause is fixed',
                  file=sys.stderr, flush=True)
            # Name the RAISER (0.55.20). The message text
            # ("Chunk size exceeds maximum allowed") appears in no file
            # of ours, and our only ``wsgi.input`` reads are the
            # body-auth handlers — so the cap lives in a dependency and
            # cannot be located from our source. Print the defining
            # module, the __cause__/__context__ chain and the frames, so
            # the next occurrence identifies what is imposing the limit
            # instead of costing another round-trip to Kent's rig.
            try:
                import traceback as _tb
                cls = type(ex)
                print(f'[data-quality]   raiser='
                      f'{getattr(cls, "__module__", "?")}.'
                      f'{getattr(cls, "__name__", "?")}',
                      file=sys.stderr, flush=True)
                seen, cur, depth = set(), ex, 0
                while cur is not None and depth < 6:
                    if id(cur) in seen:
                        break
                    seen.add(id(cur))
                    nxt = getattr(cur, '__cause__', None) \
                        or getattr(cur, '__context__', None)
                    if nxt is not None:
                        print(f'[data-quality]   caused-by='
                              f'{type(nxt).__module__}.'
                              f'{type(nxt).__name__}: {nxt}',
                              file=sys.stderr, flush=True)
                    cur, depth = nxt, depth + 1
                for line in _tb.format_exception(
                        type(ex), ex, ex.__traceback__):
                    for sub in line.rstrip().splitlines():
                        print(f'[data-quality]   {sub}',
                              file=sys.stderr, flush=True)
            except Exception as ex2:
                print(f'[data-quality]   raiser detail failed: {ex2!r}',
                      file=sys.stderr, flush=True)

        def _capture_start(status, headers, exc_info=None):
            status_holder[0] = status
            return start_response(status, headers, exc_info)

        result = inner_app(environ, _capture_start)

        def _generator():
            try:
                for chunk in result:
                    yield chunk
            except (ValueError, ConnectionError, OSError) as ex:
                # A peer that disconnects mid-push (screen off, walked
                # out of range, killed) leaves a truncated chunked
                # body; dulwich's reader surfaces it as
                # ``ValueError: invalid literal for int() with base
                # 16: b''`` and wsgiref printed the raw 15-frame
                # traceback (field 2026-07-23). Transient by design —
                # the sender still holds the data and re-pushes; one
                # line is the whole story.
                if _is_hard_transfer_error(ex):
                    _log_hard_transfer_error(langcode, ex)
                else:
                    print(f'[lan-listener] {langcode!r} receive '
                          f'interrupted (peer disconnected '
                          f'mid-transfer; sender will retry): {ex!r}',
                          file=sys.stderr, flush=True)
            except Exception as ex:
                # dulwich wraps socket errors in GitProtocolError
                # (raised ``from`` the original, so __cause__ carries
                # it) — absorb only the disconnect class; anything
                # else re-raises untouched (0.54.68).
                if not _is_peer_disconnect(ex):
                    raise
                if _is_hard_transfer_error(ex):
                    _log_hard_transfer_error(langcode, ex)
                else:
                    print(f'[lan-listener] {langcode!r} receive '
                          f'interrupted (peer disconnected '
                          f'mid-transfer; sender will retry): {ex!r}',
                          file=sys.stderr, flush=True)
            finally:
                try:
                    s = status_holder[0] or ''
                    if s.startswith('200'):
                        _reset_working_tree_after_receive(langcode)
                        _flag_received_work_for_backup(langcode)
                except Exception as ex:
                    print(f'[lan-listener] post-receive '
                          f'middleware raised: {ex!r}',
                          file=sys.stderr, flush=True)
                if hasattr(result, 'close'):
                    try:
                        result.close()
                    except Exception:
                        pass
        return _generator()

    return _wrapped


def _build_server(port):
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer
    from dulwich.web import make_wsgi_chain

    # Both are process-wide, idempotent, and must be in place before the
    # first request is served — so they live here, on the one path every
    # bind goes through (desktop ``serve()`` and the server APK's
    # ``service.py`` both reach the listener this way, per the
    # both-entry-points rule).
    _raise_dulwich_chunk_limit()
    _install_wsgiref_quiet_disconnects()

    backend = _build_dict_backend()
    # ``make_wsgi_chain(backend, …)`` wraps ``HTTPGitApplication``
    # in GunzipFilter + LimitedInputFilter for us; don't pass an
    # already-built HTTPGitApplication or we double-wrap and
    # ``backend.open_repository`` resolves to the inner
    # HTTPGitApplication instead of the DictBackend.
    # Outer ``_post_receive_pack_middleware`` triggers the
    # working-tree reset after successful receive-pack POSTs;
    # inner ``_peer_acl_middleware`` gates access by paired-peer
    # shared_projects.
    app = _repo_closing_middleware(
        _peer_acl_middleware(
            _post_receive_pack_middleware(make_wsgi_chain(backend))),
        backend)

    # Use the stdlib WSGI server rather than dulwich's
    # ``HTTPGitServer`` — the latter was removed (or renamed) in
    # the version of dulwich p4a ships, so import-time fails on
    # Android. ``wsgiref.simple_server.WSGIServer`` + a threaded
    # mixin is the equivalent setup, plus our own request-handler
    # subclass to capture the verified peer cert.
    class _ThreadedTLSGitServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            # Quiet expected connection-lifecycle noise: peers
            # abandon pooled connections and abort uploads as part
            # of normal LAN churn (field 2026-07-21: a 30-line
            # traceback per event buried the errors that mattered).
            # One summary line for those classes; full traceback
            # for anything genuinely unexpected.
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError,
                                BrokenPipeError,
                                ConnectionAbortedError,
                                TimeoutError,
                                ssl.SSLEOFError,
                                ssl.SSLError)):
                print(f'[lan-listener] connection from '
                      f'{client_address} dropped mid-request: '
                      f'{exc!r}', file=sys.stderr, flush=True)
                return
            super().handle_error(request, client_address)

    srv = _ThreadedTLSGitServer(
        ('0.0.0.0', int(port)), _build_handler_class())
    srv.set_app(app)

    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        srv.server_close()
        raise RuntimeError('peer identity unavailable; '
                           'cannot start LAN listener')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    # Client cert validation deliberately disabled. Python's stdlib
    # ``ssl`` has no "request cert but skip CA validation" mode —
    # ``CERT_REQUIRED`` makes it validate against a CA chain we
    # don't have (peer certs are self-signed and pinned by
    # fingerprint via ``peers.json``, not chain-of-trust).
    # ``CERT_OPTIONAL`` rejects the handshake the same way when
    # the client *does* present a cert, which our peers always do.
    # ``CERT_NONE`` lets the handshake complete; the TLS channel
    # stays encrypted, the SERVER side is still pinned by the
    # client (urllib3's ``assert_fingerprint``), and peer identity
    # at the client end is asserted via the request body
    # (``peer_id`` + ``fp`` claims that ``_handle_hello`` validates
    # against the cert delivered through ``getpeercert``). A
    # future-hardening pass will move client identity into a
    # signed-message header (ed25519 sig over the request); for
    # now LAN identity is body-claimed and the user is presumed
    # in control of their LAN.
    ctx.verify_mode = ssl.CERT_NONE
    ctx.check_hostname = False
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True,
                                 do_handshake_on_connect=False)
    return srv


def _outward_ip_guess():
    """Best-effort local IP for the advertised endpoint (settings-UI
    line + pairing QR).

    Step 1: UDP-connect to a non-routed destination and read back the
    local socket's IP — resolves to the DEFAULT-ROUTE interface.
    Avoids parsing /proc/net/route and works the same on Android and
    desktop. Step 2 (0.53.6): when there IS no default route — the
    field case is a hotspot-HOST desktop with its uplink unplugged
    (repro 2026-07-07: QR advertised '0.0.0.0' and the phone at
    10.42.0.100 had nothing to connect to; the host was 10.42.0.1) —
    enumerate interface addresses via SIOCGIFCONF and pick the first
    private non-loopback one. Linux-only ioctl, guarded; other
    platforms keep the old '0.0.0.0' fallback. (A multi-homed host
    whose default route is NOT the drill network still advertises the
    wrong IP — the real fix is advertising all addresses in the QR,
    tracked in agenda/local_lan_sync_stub.md § Pairing.)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 53))  # TEST-NET-1, never routed
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith('127.') and ip != '0.0.0.0':
            return ip
    except OSError:
        pass
    for ip in _interface_ipv4s():
        return ip
    return '0.0.0.0'


def _interface_ipv4s():
    """Non-loopback IPv4 addresses of local interfaces, private
    (RFC 1918) addresses first. Empty list when enumeration isn't
    available (non-Linux without a default route)."""
    addrs = []
    try:
        import array
        import fcntl
        import struct
        bufsize = 32 * 40                  # 32 ifreq slots, 64-bit
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            buf = array.array('B', b'\0' * bufsize)
            out = fcntl.ioctl(
                s.fileno(), 0x8912,        # SIOCGIFCONF
                struct.pack('iL', bufsize, buf.buffer_info()[0]))
            outbytes = struct.unpack('iL', out)[0]
            data = buf.tobytes()[:outbytes]
            for i in range(0, outbytes, 40):
                ip = socket.inet_ntoa(data[i + 20:i + 24])
                if not ip.startswith('127.') and ip != '0.0.0.0':
                    addrs.append(ip)
        finally:
            s.close()
    except Exception:
        return []

    def _private(ip):
        return (ip.startswith('10.') or ip.startswith('192.168.')
                or any(ip.startswith(f'172.{n}.')
                       for n in range(16, 32)))
    return sorted(set(addrs), key=lambda ip: (not _private(ip), ip))


def _port_memo_path():
    return os.path.join(_paths.azt_home(), 'lan_listener_port')


def _read_preferred_port():
    """Last successfully-bound listener port, or 0 (= let the OS
    pick). Re-binding the same port across daemon restarts keeps
    every peer's cached / persisted endpoint for us valid — a
    respawn no longer strands peers dialing the old port until
    their discovery catches up (stale-peer-address incidents
    2026-07-10/11)."""
    try:
        with open(_port_memo_path()) as f:
            p = int(f.read().strip())
        return p if 1024 < p < 65536 else 0
    except Exception:
        return 0


def _write_preferred_port(port):
    try:
        tmp = f'{_port_memo_path()}.tmp'
        with open(tmp, 'w') as f:
            f.write(str(int(port)))
        os.replace(tmp, _port_memo_path())
    except Exception as ex:
        print(f'[lan-listener] port memo write failed: {ex!r}',
              file=sys.stderr, flush=True)


def start():
    """Start the listener, preferring the previously-bound port
    (see ``_read_preferred_port``) and falling back to an
    OS-assigned one when it's taken. Idempotent — a second call
    while running returns the existing endpoint."""
    with _LOCK:
        if _STATE['server'] is not None:
            return _STATE['bound']
        srv = None
        preferred = _read_preferred_port()
        if preferred:
            try:
                srv = _build_server(preferred)
            except OSError as ex:
                print(f'[lan-listener] previous port {preferred} '
                      f'unavailable ({ex!r}); binding ephemeral',
                      file=sys.stderr, flush=True)
                srv = None
        if srv is None:
            srv = _build_server(0)
        host = _outward_ip_guess()
        port = srv.server_address[1]
        _write_preferred_port(port)
        thread = threading.Thread(
            target=srv.serve_forever, name='lan-listener',
            daemon=True)
        thread.start()
        _STATE['server'] = srv
        _STATE['thread'] = thread
        _STATE['bound'] = (host, port)
        print(f'[lan-listener] started on {host}:{port}',
              file=sys.stderr, flush=True)
        return _STATE['bound']


def stop():
    """Stop the listener. Idempotent."""
    with _LOCK:
        srv = _STATE['server']
        if srv is None:
            return
        _STATE['server'] = None
        _STATE['thread'] = None
        _STATE['bound'] = None
    try:
        srv.shutdown()
    except Exception as ex:
        print(f'[lan-listener] shutdown raised: {ex!r}',
              file=sys.stderr, flush=True)
    try:
        srv.server_close()
    except Exception:
        pass
    print('[lan-listener] stopped', file=sys.stderr, flush=True)


def _admin_door_wanted():
    """True when LAN sync is off but we should still answer the admin channel.

    **Desktop only, deliberately.** On Android the listener needs the
    foreground service to survive doze, and the whole point of the toggle is
    to release the FGS and the WifiLocks — so a bound socket there would be a
    promise the OS will break within minutes. Phones get the pre-apply warning
    on the toggle instead; that is the only honest protection available to
    them (Kent 2026-07-30: "linux and windows only is OK").

    Requires a peer we have granted admin. Nobody authorised, no socket."""
    try:
        if (os.environ.get('ANDROID_ARGUMENT')
                or os.environ.get('ANDROID_BOOTLOGO')):
            return False
    except Exception:
        pass
    try:
        from . import peers as _peers
        for entry in (_peers.list_peers() or []):
            if entry.get('admin'):
                return True
    except Exception as ex:
        # Fail CLOSED: if we can't read the grants we don't know anyone
        # authorised this, and a socket bound on a guess is worse than a
        # device that needs local attention.
        print(f'[lan-listener] admin-door check could not read peers '
              f'({ex!r}) — not binding the admin channel',
              file=sys.stderr, flush=True)
    return False


def apply_toggle():
    """Reconcile the listener lifecycle with the union of:
      - ``lan.autodiscovery`` (continuous-on policy bit), and
      - ``lan_fgs`` discovery ref count > 0 (a burst is active).

    Called from the toggle RPC handler after the setting is
    persisted, from ``lan_burst.start_burst`` /
    ``lan_burst._burst_done``, and from the watcher's reconcile
    tick. Safe to call from anywhere; hot-applied — no daemon
    restart required.

    Order on UP: acquire WifiLocks first (so multicast is
    available before any NsdManager browse fires), then promote
    the :provider service to FGS (so the OS can't kill us mid-
    handshake), then start the listener thread. Reverse on
    DOWN.

    Per-step failure attribution: each phase logs its own
    ``[lan-listener] {step} failed`` line so a field log
    immediately identifies whether WifiLock, FGS promotion, or
    socket bind is the failing seam. Idempotent: when called on
    a healthy daemon, the ``not is_running()`` / ``is_running()``
    guards short-circuit so no work is done."""
    from .android_cp import lan_fgs as _lan_fgs
    # Up if either reason is active: user picked continuous, or a
    # burst is currently armed. The burst path uses
    # ``arm_for_discovery`` which bumps the ref count we're
    # reading here.
    autodiscovery = _settings.lan_autodiscovery()
    burst_armed = (_lan_fgs.snapshot().get('ref_discovery', 0) > 0)
    desired = autodiscovery or burst_armed

    def _fail(step, ex):
        # Same per-step attribution as before, plus a typed copy
        # the toggle RPC can hand the UI so the user sees WHICH
        # step failed instead of "see the daemon log" (0.54.75).
        print(f'[lan-listener] {step} failed: {ex!r}',
              file=sys.stderr, flush=True)
        with _LOCK:
            _STATE['bind_error'] = f'{step}: {ex!r}'[:200]

    # ADMIN-ONLY DOOR (0.55.155). The admin channel rides on this listener,
    # so switching LAN sync off removed the only remote way to switch it back
    # on — physical access became the sole recovery, for any device, whether
    # or not the user understood that when they tapped it.
    #
    # This is the inbound counterpart of a principle the outbound side has
    # had since 0.55.68: ``_https_post_to_peer``'s ``force`` bypasses the
    # toggle for deliberate user gestures (QR pair, cable). An admin grant is
    # a stronger gesture than those — made in advance, by this device's own
    # user, naming one specific peer.
    #
    # Gated on a granted peer existing, so no socket is bound on a device
    # where nobody authorised one.
    admin_only = (not desired) and _admin_door_wanted()
    want_listener = desired or admin_only
    # ``is_running()`` can't tell the two modes apart, so a mode flip has to
    # rebind explicitly rather than short-circuit on "already running".
    if is_running() and bool(_STATE.get('admin_only')) != admin_only:
        print(f'[lan-listener] switching to '
              f'{"admin-only" if admin_only else "full"} mode — rebinding',
              file=sys.stderr, flush=True)
        try:
            _lan_discovery.stop_browse()
            _lan_discovery.stop_advertise()
        except Exception as ex:
            print(f'[lan-listener] discovery stop raised: {ex!r}',
                  file=sys.stderr, flush=True)
        stop()
        if admin_only:
            _lan_fgs.stop_fgs()
            _lan_fgs.release_wifi_locks()
    if admin_only and not is_running():
        # No WifiLock, no FGS, no advertise, no browse, no bind sweep. Those
        # are where the radio and battery cost lives, and they stay off — the
        # door is a bound TCP socket and nothing else. Reachability therefore
        # depends on the peer having a recorded address; the port memo means
        # the previous one is usually still correct.
        with _LOCK:
            _STATE['admin_only'] = True
        try:
            bound = start()
        except Exception as ex:
            _fail('admin-door bind', ex)
            return
        with _LOCK:
            _STATE['bind_error'] = ''
        print(f'[lan-listener] LAN sync is off, but a peer holds a '
              f'remote-settings grant — serving the admin channel only on '
              f'{bound[0]}:{bound[1]} (no sync, no discovery, no radio '
              f'locks)', file=sys.stderr, flush=True)
        return
    if desired and not is_running():
        with _LOCK:
            _STATE['admin_only'] = False
        try:
            _lan_fgs.acquire_wifi_locks()
        except Exception as ex:
            _fail('acquire_wifi_locks', ex)
            return
        try:
            _lan_fgs.start_fgs()
        except Exception as ex:
            _fail('start_fgs', ex)
            _lan_fgs.release_wifi_locks()
            return
        try:
            bound = start()
        except Exception as ex:
            _fail('listener bind', ex)
            _lan_fgs.stop_fgs()
            _lan_fgs.release_wifi_locks()
            return
        with _LOCK:
            _STATE['bind_error'] = ''
        # Advertise + browse only after the listener is bound, so
        # the port we publish is real.
        try:
            ident = _peer_id.ensure()
            _lan_discovery.start_advertise(
                ident['peer_id'], ident['fp'],
                bound[1], _store.get_device_name())
            _lan_discovery.start_browse()
        except Exception as ex:
            print(f'[lan-listener] discovery start failed: {ex!r}',
                  file=sys.stderr, flush=True)
        # Listener-bind sweep (0.50.45). The radio just came up;
        # any paired peer we already know an endpoint for (from
        # ``peers.json::endpoints`` recorded at pair-time, or a
        # mDNS cache that survived a brief drop) might be
        # behind on shared projects. Fire one sweep per paired
        # peer in a worker thread so the binder returns promptly.
        # ``sweep_peer`` skips peers whose endpoint can't be
        # resolved, so it's cheap when nobody's actually
        # reachable — no harm in firing optimistically.
        def _listener_bind_sweep():
            try:
                from . import peers as _peers
                from . import lan_push as _lan_push
                for entry in _peers.list_peers():
                    pid = entry.get('peer_id', '') or ''
                    if not pid:
                        continue
                    try:
                        _lan_push.sweep_peer(pid)
                    except Exception as ex:
                        print(f'[lan-listener] bind-sweep '
                              f'{pid[:8]!r} raised: {ex!r}',
                              file=sys.stderr, flush=True)
            except Exception as ex:
                print(f'[lan-listener] bind-sweep dispatch '
                      f'raised: {ex!r}',
                      file=sys.stderr, flush=True)
        import threading as _t_mod
        _t_mod.Thread(target=_listener_bind_sweep, daemon=True,
                      name='lan-bind-sweep').start()
    elif not want_listener and is_running():
        try:
            _lan_discovery.stop_browse()
            _lan_discovery.stop_advertise()
        except Exception as ex:
            print(f'[lan-listener] discovery stop raised: {ex!r}',
                  file=sys.stderr, flush=True)
        stop()
        _lan_fgs.stop_fgs()
        _lan_fgs.release_wifi_locks()
        with _LOCK:
            _STATE['admin_only'] = False
