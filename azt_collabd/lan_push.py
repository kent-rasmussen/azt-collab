"""
LAN fan-out push (parked design phase 6).

After every drain pass's github push attempt, the scheduler calls
``fan_out(project)`` here to opportunistically deliver the same
commits to every paired peer that has this project in its
``shared_projects`` and a known endpoint (mDNS-resolved or static).

LAN delivery is **opportunistic redundancy**: success here does
NOT clear the ``pending_push`` flag. Only a successful github push
does (the suite stays github-authoritative; LAN is the cheap
sneakernet that helps two phones in the same room without
re-running the whole flow over a metered link). Per the spec's
"GitHub convergence" property.

TLS pinning: we trust *only* the fingerprint recorded in
``peers.json`` for each peer. A pinned-fingerprint mismatch fires
``S.LAN_FP_MISMATCH`` (logged here; surfaced peer-side by a future
listener-side hello-handshake handler).

Per-target failure is logged but never raised — a slow / killed
peer can't take down the github drain it rides alongside.
"""

from __future__ import annotations

import hashlib
import ssl
import socket
import sys
import tempfile
import threading
import time as _time_mod

from . import lan_discovery as _lan_discovery
from . import peer_id as _peer_id
from . import peers as _peers
from . import status as S
from .locks import LockTimeout, project_lock


# Per-peer consecutive "refused / unreachable" failure counter.
# Reset on every successful contact (push, no-op confirmation, or
# share-offer round-trip). After ``_RESTART_DISCOVERY_THRESHOLD``
# consecutive failures we call ``lan_discovery.restart_browse()``
# — equivalent to the user manually flipping LAN off+on, which
# was observed in the field to recover stale NsdManager state.
# Counter goes back to 0 after restart so we don't restart in a
# tight loop.
def _route_hint(host):
    """`` [no route from any local address]`` or `` [would route from
    <ip>]`` — appended to a dial-failure line so "the peer is on a
    different network" is one line, not a cross-machine IP comparison
    (0.55.65).

    Field 2026-07-28: four subnets in play at once
    (`10.191.129.x`, `10.184.19.x`, `192.168.31.x`, `192.168.124.x`),
    some devices multi-homed across two. Half a day of `no_route` and
    connect-timeout volume was peers dialing addresses on segments they
    had no path to — obvious only after lining up IPs from two
    machines' logs by hand.

    A UDP ``connect`` is the right probe: it sends nothing, it asks the
    kernel's routing table which local source address would be used to
    reach *host*, and it fails outright when no route exists. Better
    than enumerating interfaces, which answers "what have I got"
    rather than "can I get there" — and ``_interface_ipv4s`` is
    ``SIOCGIFCONF``-based, so it returns nothing on Windows, where this
    is most needed.

    Returns '' on any unexpected error: a diagnostic must never be what
    breaks the path it is describing."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.settimeout(0.5)
            probe.connect((host, 9))        # discard port; no packet sent
            local = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return (' [NO ROUTE from any local address — the peer is on a '
                'network this device cannot reach; same-subnet is a '
                'requirement, not a preference]')
    except Exception:
        return ''
    if not local or local.startswith('127.'):
        return ''
    return f' [would route from {local}]'


_consec_failures = {}   # peer_id_hex → int
_RESTART_DISCOVERY_THRESHOLD = 3

# Per-peer "we just saw this one unreachable" timestamp (monotonic).
# Set by ``_record_unreachable``; checked by
# ``_recently_unreachable`` at the top of every push / signalling
# helper to short-circuit before any urllib3 retry storm. Cleared
# on observed success. The cooldown window (default 60s) is sized
# to "skip an entire burst's worth of fan-out / sweep attempts
# without waiting on retries" — a peer that actually comes back
# within the window will be re-tried on the next mDNS arrival
# (which clears the gate by going through ``_record_reachable``).
#
# Pre-0.50.49 a paired-but-absent peer cost ~23 seconds per
# burst (3 urllib3 retries × ~2.3s connect timeout × multiple
# projects in the sweep). With this gate, the first attempt logs
# the failure and every subsequent attempt within the cooldown
# returns False in microseconds.
_unreachable_at = {}    # peer_id_hex → monotonic timestamp
_UNREACHABLE_COOLDOWN_S = 60.0


def _recently_unreachable(peer_id):
    """Return True iff ``peer_id`` was observed unreachable within
    the cooldown window. Caller should short-circuit (return False
    for push helpers, etc.) when this returns True."""
    import time as _time
    ts = _unreachable_at.get(peer_id)
    if ts is None:
        return False
    return (_time.monotonic() - ts) < _UNREACHABLE_COOLDOWN_S


def _record_unreachable(peer_id):
    """Mark *peer_id* as unreachable. Called from the network-
    error paths in ``_push_to_peer`` / ``_https_post_to_peer``."""
    import time as _time
    _unreachable_at[peer_id] = _time.monotonic()


def _record_reachable(peer_id):
    """Clear the unreachable gate for *peer_id*. Called from the
    successful-contact paths (push 2xx, no-op confirmation,
    share-offer round-trip)."""
    _unreachable_at.pop(peer_id, None)
    _endpoint_dead.pop(str(peer_id), None)


# Per-ENDPOINT dead marker (0.55.83): ``peer_id → {(host, port): ts}``.
#
# ``_unreachable_at`` above is keyed per PEER, which conflates "this
# address is dead" with "this device is unreachable". Field 2026-07-28:
# a phone was recorded at ``10.184.19.6`` (a previous network) while
# actually live at ``192.168.31.179``. One timeout against the stale
# address condemned the whole peer for the cooldown, even though another
# endpoint in the mDNS→static→QR ladder would have worked.
#
# So: mark the ENDPOINT dead and let resolution move down the ladder;
# only when every known endpoint has failed at connect level does the
# peer itself get gated. Cleared wholesale on any successful contact,
# and on an mDNS arrival (a fresh announcement is new information about
# every address).
_endpoint_dead = {}
_ENDPOINT_DEAD_COOLDOWN_S = 60.0


def _record_endpoint_dead(peer_id, host, port):
    """Mark one (host, port) of *peer_id* as unreachable."""
    import time as _time
    try:
        _endpoint_dead.setdefault(str(peer_id), {})[
            (str(host), int(port))] = _time.monotonic()
    except Exception:
        pass


def _endpoint_recently_dead(peer_id, host, port):
    """True iff this specific endpoint failed within the cooldown."""
    import time as _time
    try:
        ts = _endpoint_dead.get(str(peer_id), {}).get(
            (str(host), int(port)))
    except Exception:
        return False
    if ts is None:
        return False
    return (_time.monotonic() - ts) < _ENDPOINT_DEAD_COOLDOWN_S


def clear_endpoint_dead(peer_id):
    """Forget every dead-endpoint marker for *peer_id* — called on an
    mDNS arrival, which is fresh information about where they are."""
    _endpoint_dead.pop(str(peer_id), None)


def _resolve_endpoint(peer_entry):
    """Endpoint resolution order per the spec: mDNS-cached → static
    endpoints → QR-hint endpoint. Returns ``(host, port)`` or
    ``None``."""
    pid = peer_entry.get('peer_id', '')
    # SKIP ENDPOINTS THAT JUST FAILED, RATHER THAN THE WHOLE PEER
    # (0.55.83). A peer can hold one stale address and one live one at
    # the same moment — field: a phone recorded at 10.184.19.6 from an
    # earlier network while actually reachable at 192.168.31.179. The
    # per-peer gate condemned the device on the stale address; walking
    # past just that entry finds the live one.
    #
    # Collected rather than returned-first so an all-dead ladder still
    # returns something: better to retry a known-bad address than to
    # report "no endpoint" and stop trying entirely.
    candidates = []
    mdns = _lan_discovery.get_endpoint(pid) if pid else None
    if mdns is not None:
        candidates.append(mdns)
    for source in ('static_endpoints', 'endpoints'):
        for raw in (peer_entry.get(source) or []):
            try:
                host, port = raw.rsplit(':', 1)
                candidates.append((host, int(port)))
            except (ValueError, TypeError):
                continue
    if not candidates:
        return None
    for host, port in candidates:
        if not _endpoint_recently_dead(pid, host, port):
            return (host, port)
    print(f'[lan-push] {str(pid)[:8]!r}: every known endpoint failed '
          f'within the last {int(_ENDPOINT_DEAD_COOLDOWN_S)}s '
          f'({len(candidates)} tried) — retrying the first anyway',
          file=sys.stderr, flush=True)
    return candidates[0]


def _build_ssl_context(expected_fp):
    """Build a client-side SSL context that authenticates the peer's
    cert by *fingerprint* rather than CA chain.

    Raises when *expected_fp* is empty: the pool layer pins via
    urllib3 ``assert_fingerprint``, which silently SKIPS pinning on a
    falsy value — and with CA validation deliberately off, that would
    be a genuinely unverified connection (0.54.64).

    We can't use ``ctx.set_verify`` with a callback that consults the
    peer's cert because Python's ``ssl`` doesn't expose a verify
    callback at the application layer. Instead we leave the context
    in unverified mode and check the fingerprint on the resulting
    socket after handshake (caller's job — passes the context into
    dulwich's HttpGitClient and inspects the connection)."""
    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        raise RuntimeError('this daemon has no LAN identity '
                           '(cryptography unavailable?)')
    if not expected_fp:
        raise RuntimeError(
            'peer has no recorded TLS fingerprint — refusing '
            'unpinned connection')
    # ``ssl._create_unverified_context()`` is the documented
    # idiom for "skip CA validation entirely." A manually-built
    # ``SSLContext(PROTOCOL_TLS_CLIENT)`` followed by
    # ``verify_mode=CERT_NONE`` *should* do the same thing, but
    # in practice the TLS_CLIENT default bakes the verify-required
    # behavior in deeper than the attribute set unwinds — we got
    # ``CERTIFICATE_VERIFY_FAILED: self signed certificate`` on
    # handshake despite the override. The underscored helper
    # short-circuits the verification flag at construction time.
    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


def _verify_fingerprint(ssl_sock, expected_fp, peer_id):
    """Compare the SHA-256 of the peer's DER cert to *expected_fp*.
    Returns True on match, logs+False on mismatch."""
    der = ssl_sock.getpeercert(binary_form=True)
    if not der:
        print(f'[lan-push] no peer cert from {peer_id[:8]!r}; '
              f'refusing', file=sys.stderr, flush=True)
        return False
    got = hashlib.sha256(der).hexdigest()
    if got != expected_fp:
        print(f'[lan-push] {S.LAN_FP_MISMATCH} for {peer_id[:8]!r}: '
              f'expected={expected_fp[:16]!r} got={got[:16]!r}',
              file=sys.stderr, flush=True)
        return False
    return True


def _pinned_pool_manager(ctx, expected_fp, connect, read):
    """Fingerprint-pinned urllib3 PoolManager with bounded timeouts
    and NO urllib3-level retries (push bodies are non-rewindable
    generators — a retry resends the remainder and the peer reads
    mid-pack garbage; our sweep/merge machinery is the retry layer).

    Timeout sizing (field 2026-07-21): urllib3 keeps the CONNECT
    timeout on the socket for the entire request-SEND phase — it
    only switches to the read timeout just before reading the
    response — so ``connect`` is also the per-write stall ceiling
    while uploading a pack. 5 s tripped mid-upload under receiver
    backpressure ("The write operation timed out") on every big
    push, while the peer went on to ingest and apply the full pack.
    Peeks and tiny-body calls keep connect tight for fast dead-
    endpoint failure; pushes get a generous value.

    ``cert_reqs='CERT_NONE'``: urllib3 clobbers ``ctx.verify_mode``
    with ``resolve_cert_reqs(cert_reqs)`` inside
    ``_ssl_wrap_socket_and_match_hostname``; the default (None)
    resolves to CERT_REQUIRED, undoing our unverified context and
    failing on self-signed peer certs. Identity is pinned via
    ``assert_fingerprint`` instead."""
    import urllib3
    return urllib3.PoolManager(
        ssl_context=ctx,
        assert_hostname=False,
        assert_fingerprint=expected_fp,
        cert_reqs='CERT_NONE',
        timeout=urllib3.Timeout(connect=connect, read=read),
        retries=False,
    )


_push_inflight = set()           # {(peer_id, langcode)} being pushed
_push_inflight_lock = threading.Lock()


def _their_shared_projects(pid, peer_entry=None):
    """What the peer told us THEY share with us, from the hello manifest
    (0.55.50).

    Returns a list — **including an empty one, which is an answer** — or
    ``None`` when they have never told us (pre-0.55.50 peer, or no hello
    yet). Callers must distinguish those: ``None`` means "no information",
    not "shares nothing".

    Prefers the entry already in hand; falls back to a registry lookup.
    There is no getter in ``peers`` — only ``set_their_shared_projects``
    — so this reads the field the way ``sweep_peer`` does rather than
    inventing an accessor."""
    if isinstance(peer_entry, dict):
        v = peer_entry.get('their_shared_projects')
        if isinstance(v, list):
            return v
    if not pid:
        return None
    try:
        for e in (_peers.list_peers() or []):
            if str(e.get('peer_id', '')) == str(pid):
                v = e.get('their_shared_projects')
                return v if isinstance(v, list) else None
    except Exception:
        return None
    return None


def _push_to_peer(project, peer_entry):
    """Single push attempt against one paired peer. Returns
    ``True`` on success, ``False`` on any failure. Logs detail
    rather than raising.

    **One attempt per (peer, project) at a time (0.55.55).** Several
    triggers fire independently — mDNS arrival sweep, listener-bind
    sweep, reverse delivery, fan-out, the burst — and nothing stopped
    them running the same push concurrently. 0.55.52 guarded the fetch;
    the work BEFORE it was still duplicated, and that work is not cheap.

    Watchdog dump, tablet, 2026-07-28 12:56: six threads
    (`lan-reverse-deliver` ×3, `lan-arrival-sweep` ×2,
    `lan-bind-sweep`) all inside `_peer_is_ancestor_of_local` →
    dulwich walk → `_lookup_in_packs` at once, with **fds=1607** (up
    from ~280). No fd leak — that function closes its repo — but each
    ``Repo()`` opens every pack file, so N concurrent walks multiply the
    fd peak N-fold and duplicate the CPU exactly N times for one answer.

    Keyed separately from ``_fetch_inflight`` so the nested fetch guard
    inside ``_merge_then_push`` can't see this entry and skip itself."""
    from dulwich import porcelain
    pid = peer_entry.get('peer_id', '')
    expected_fp = peer_entry.get('fp', '')
    # LAN TOGGLE GATE (0.55.68). ``lan.allow_sync`` off must mean off in
    # BOTH directions. Until now it stopped the listener and nothing
    # else: this module never consulted the setting at all (zero
    # references), so every outbound trigger — sweep, fan-out, reverse
    # delivery, arrival, burst — kept dialing peers with LAN switched
    # off. Field 2026-07-28, desktop:
    #
    #   19:23:57  [lan-listener] stopped          ← toggled off
    #   19:24:03  [lan-push] dialing …
    #   19:24:21  [lan-push] dialing …   (and on, for minutes)
    #
    # Same defect shape as ``work_offline`` before 0.55.42: the toggle
    # was enforced where it was cheap to enforce, not where the network
    # is actually touched. A user turning LAN off is asking us to leave
    # the radio alone; half-honouring that is worse than not offering
    # the switch, because the battery cost continues invisibly.
    #
    # Placed at this seam for the same reason as the in-flight and
    # one-sided-share guards: every trigger funnels through here.
    try:
        from . import settings as _settings
        if not _settings.lan_allow_sync():
            print(f'[lan-push] {str(pid or "")[:8]!r}: LAN sync is off — '
                  f'not dialing (the toggle stops outbound too, not just '
                  f'the listener)', file=sys.stderr, flush=True)
            return False
    except Exception as ex:
        print(f'[lan-push] lan_allow_sync check raised: {ex!r} — '
              f'proceeding', file=sys.stderr, flush=True)
    _pkey = (str(pid or ''), str(getattr(project, 'langcode', '') or ''))
    # ONE-SIDED SHARE GATE (0.55.61). Moved here from ``sweep_peer``,
    # which was the ONLY trigger consulting the hello manifest — reverse
    # delivery, mDNS arrival and ``lan_burst_now`` all call this function
    # directly, so they dialed straight into a known one-sided share and
    # ate a bare ``NotGitRepository``. Every trigger funnels through
    # here, same reason the in-flight guard lives at this seam.
    #
    # Refusing to PUSH (not just fetch) is deliberate and matches the
    # per-peer ACL: their listener requires the project be shared with us
    # in BOTH directions, so a push into a project they don't share with
    # us is refused on arrival. Dialing is futile, not merely impolite.
    if _pkey[1]:
        _theirs = _their_shared_projects(pid, peer_entry)
        if isinstance(_theirs, list) and _pkey[1] not in _theirs:
            print(f'[lan-push] {pid[:8]!r} {_pkey[1]!r}: ONE-SIDED '
                  f'SHARE — we share it with them, they do not share it '
                  f'with us (their grants: {sorted(_theirs)!r}). Not '
                  f'dialing; their ACL would refuse this anyway. Ask '
                  f'them to share {_pkey[1]!r}, or stop sharing it '
                  f'with them', file=sys.stderr, flush=True)
            return False
    with _push_inflight_lock:
        if _pkey in _push_inflight:
            print(f'[lan-push] {pid[:8]!r} {_pkey[1]!r}: push already '
                  f'in flight — skipping this trigger (duplicate work '
                  f'for one answer)', file=sys.stderr, flush=True)
            return False
        _push_inflight.add(_pkey)
    try:
        return _push_to_peer_inner(project, peer_entry, pid, expected_fp)
    finally:
        with _push_inflight_lock:
            _push_inflight.discard(_pkey)


def _push_to_peer_inner(project, peer_entry, pid, expected_fp):
    """Body of ``_push_to_peer``; see it for the contract and for why
    the in-flight guard exists. Split so the guard has one release
    point."""
    from dulwich import porcelain
    # Fast-fail gate (0.50.49): skip the connect attempt entirely
    # if this peer was unreachable within the cooldown window.
    # Saves ~7s per attempt (3 urllib3 retries × 2.3s connect
    # timeout) when the peer is genuinely absent.
    if _recently_unreachable(pid):
        print(f'[lan-push] {pid[:8]!r} recently unreachable; '
              f'skipping (fast-fail)',
              file=sys.stderr, flush=True)
        return False
    endpoint = _resolve_endpoint(peer_entry)
    if endpoint is None:
        print(f'[lan-push] no endpoint for {pid[:8]!r}; skipping',
              file=sys.stderr, flush=True)
        return False
    host, port = endpoint
    url = f'https://{host}:{port}/{project.langcode}.git'
    # Dial-time summary (always-emit rule): without this line the
    # only trace of an attempt was its eventual failure warning,
    # minutes later under a slow timeout — "did my trigger do
    # anything?" was unanswerable from the log (field 2026-07-21).
    print(f'[lan-push] dialing {pid[:8]!r} at {host}:{port} '
          f'for {project.langcode!r}',
          file=sys.stderr, flush=True)
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-push] context build failed for {pid[:8]!r}: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return False
    # dulwich's HttpGitClient uses urllib3 underneath; a PoolManager
    # with our custom SSL context is the documented seam for client-
    # side TLS knobs (fingerprint pinning via assert_fingerprint).
    # Two pms because of the urllib3 connect-timeout-governs-send
    # quirk (see _pinned_pool_manager): the peeks must fail fast on
    # dead endpoints (connect=5), while the push's connect value is
    # really the per-write stall ceiling during pack upload — 5 s
    # aborted every big push mid-stream (field 2026-07-21). A
    # straight-to-push miss on a dead endpoint pays 30 s once; the
    # fast-fail gate covers the cooldown after that.
    try:
        peek_pm = _pinned_pool_manager(ctx, expected_fp,
                                       connect=5, read=10)
        pm = _pinned_pool_manager(ctx, expected_fp,
                                  connect=30, read=180)
    except Exception as ex:
        print(f'[lan-push] urllib3 pool manager failed for '
              f'{pid[:8]!r}: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    # Pre-flight: ask the peer for its current main-branch SHA via
    # ls-remote (cheap — ref advertisement only, no pack transfer).
    # We use this to (a) skip the push entirely when peer is already
    # at our HEAD, and (b) log a meaningful before/after when we
    # actually advance the peer. Without this the porcelain.push
    # "success" line is ambiguous between real delivery and no-op.
    local_head = _local_head_sha(project)
    pre_peer_head = _peek_peer_main(url, peek_pm, pid)
    # Honest per-peer observation: any time ls-remote returns a
    # peer's main SHA we record it. Drives the honest
    # ``lan_unshared`` and ``at_risk`` counts (since 0.47.0; was
    # the conflated ``unshared_commits`` pre-0.47) — see
    # ``repo._lan_unshared`` and ``peers.peer_coverage_for``.
    if pre_peer_head:
        try:
            _peers.set_peer_last_seen_main(
                pid, project.langcode, pre_peer_head)
        except Exception:
            pass
    if pre_peer_head is None:
        # Couldn't ls-remote; proceed with the push attempt anyway.
        # The log below will say "in-sync? unknown" — we still get
        # a clear error if the push fails.
        pass
    elif local_head and pre_peer_head == local_head:
        # Name the PROJECT (0.55.51). ``dialing`` says which project;
        # the outcome lines did not, so a grep could not tell you which
        # of a peer's projects an outcome belonged to. Kent read a
        # diverged 'nml' and an in-sync 'baf' — same peer, 8 s apart —
        # as a contradiction, because both lines showed only SHAs.
        print(f'[lan-push] {pid[:8]!r} {project.langcode!r} already at '
              f'{local_head[:12]!r} — no-op',
              file=sys.stderr, flush=True)
        # The no-op confirms the peer has our SHA. Both the
        # legacy project-wide field (back-compat) and the new
        # per-peer record get the observation.
        try:
            from . import projects as _projects
            _projects.set_last_lan_pushed_sha(
                project.langcode, local_head)
        except Exception:
            pass
        # Confirmed containment of OUR commit — record the
        # covered-local coverage the sync-status walkers fall
        # back to when the peer's head isn't in our store.
        try:
            _peers.set_peer_covered_local(
                pid, project.langcode, local_head)
        except Exception:
            pass
        _consec_failures.pop(pid, None)  # success: reset counter
        _record_reachable(pid)  # clear fast-fail gate
        # A completed exchange for this project proves the peer's
        # listener MOUNTS it — i.e. the share exists on their side too
        # (0.54.98). One-sided shares 404 here instead, so reaching
        # this point is the evidence the heal is looking for.
        try:
            _peers.set_share_confirmed(pid, project.langcode, True)
        except Exception:
            pass
        # This address just worked — put it first so the next dial
        # starts there (0.54.99, mirror of demote-on-failure).
        try:
            _peers.promote_endpoint(pid, f'{host}:{int(port)}')
        except Exception:
            pass
        return True

    # Pre-flight fast-forward check (since 0.46.4). dulwich's
    # smart-protocol receive-pack on the listener side uses
    # ``set_if_equals(ref, expected_old, new)`` — a stale-write
    # guard, NOT a fast-forward check. If our ``expected_old``
    # matches the peer's current main (which it does because we
    # just read it via ls-remote), dulwich happily ACCEPTS a non-
    # FF push as a silent force-overwrite. The peer's
    # ``refs/heads/main`` gets reset to our HEAD; the peer's own
    # commits stay in the object store but the ref no longer
    # points at them. Field-observed result: each phone pushed
    # to the other and each phone's local commits silently fell
    # off the ref while still being shown locally because HEAD
    # was decoupled from main on the receive side. Both phones
    # rendered LANOK on diverged histories (the recorder team's
    # 2026-05-26 report).
    #
    # Defend client-side: if the peer's current main is NOT an
    # ancestor of our local HEAD, that's a divergence — go
    # through ``_merge_then_push`` (lift-aware three-way fetch
    # + merge + push) rather than letting porcelain.push do the
    # force-overwrite. This is the pre-flight complement to the
    # 0.45.46 post-flight verify, which detected silent NAKs
    # (HTTP-level success, protocol-level rejection). The
    # silent-overwrite case here is the opposite shape: protocol-
    # level success that we shouldn't have asked for.
    if pre_peer_head and pre_peer_head != local_head:
        if not _peer_is_ancestor_of_local(project, pre_peer_head):
            print(f'[lan-push] {pid[:8]!r} {project.langcode!r}: peer '
                  f'at {pre_peer_head[:12]!r} is NOT ancestor of '
                  f'local {local_head[:12]!r} — would be force-'
                  f'overwrite; routing through merge instead',
                  file=sys.stderr, flush=True)
            return _merge_then_push(
                project, url, pm, pid, host, port)

    try:
        porcelain.push(
            project.working_dir,
            remote_location=url,
            refspecs=[b'HEAD:refs/heads/main'],
            pool_manager=pm,
        )
    except TypeError:
        # Older dulwich without pool_manager kwarg — fall back to
        # the version that constructs its own pool. We lose
        # fingerprint pinning in this branch; log loudly so the user
        # knows to upgrade dulwich.
        print(f'[lan-push] dulwich does not support pool_manager= '
              f'kwarg; refusing unpinned push to {pid[:8]!r}',
              file=sys.stderr, flush=True)
        return False
    except Exception as ex:
        cls = type(ex).__name__
        # Connection-level failures (peer restarted on a new port,
        # peer process died, network blip) → invalidate the cached
        # mDNS endpoint so the next discovery refresh can fill in
        # the current address. Otherwise the cache pins us to a
        # stale port for the rest of this daemon's lifetime — and
        # NsdManager doesn't always fire an update event when a
        # same-name service rebinds to a new port. The error
        # string is the most portable signal across urllib3 +
        # dulwich + ssl stacks.
        msg = str(ex)
        # Connect-phase timeouts join the recovery path (0.54.3):
        # dialing an address from a previous network life (e.g. a
        # hotspot-era 10.42.0.x ghost) produces ConnectTimeoutError,
        # not ECONNREFUSED — pre-0.54.3 that skipped this whole
        # block, so the stale address was never invalidated OR
        # demoted and got re-dialed every fan-out (field repro
        # 2026-07-11, agenda/lan_stale_peer_address.md). Matching is
        # deliberately connect-phase only ('connect timeout' /
        # ConnectTimeoutError) — a READ timeout mid-transfer means
        # the address was fine.
        _is_connect_timeout = ('ConnectTimeoutError' in msg
                               or 'connect timeout' in msg)
        _is_connect_class = ('Connection refused' in msg
                             or 'Errno 111' in msg
                             or 'NewConnectionError' in msg
                             or _is_connect_timeout)
        if not _is_connect_class and local_head:
            # Delivered-despite-lost-response check (0.54.13). Field
            # shape 2026-07-21: the connection dies at/after end of
            # upload but the peer has ALREADY ingested and applied
            # the pack (desktop `git log` showed our commits at its
            # HEAD while we logged failures and re-pushed the same
            # pack every burst, tracebacking the peer each time).
            # If the peer's main now IS the head we just pushed,
            # that IS delivery — record it and stop the loop. Only
            # for non-connect-class errors: if we never connected,
            # the peek is a wasted dial.
            try:
                post_head = _peek_peer_main(url, peek_pm, pid)
            except Exception:
                post_head = None
            if post_head and post_head == local_head:
                print(f'[lan-push] {pid[:8]!r} {project.langcode!r}: '
                      f'push errored ({cls}) '
                      f'but peer main is AT our head '
                      f'{local_head[:12]!r} — delivered, response '
                      f'lost; recording success',
                      file=sys.stderr, flush=True)
                try:
                    _peers.set_peer_last_seen_main(
                        pid, project.langcode, post_head)
                except Exception:
                    pass
                try:
                    _peers.set_peer_covered_local(
                        pid, project.langcode, post_head)
                except Exception:
                    pass
                _consec_failures.pop(pid, None)
                _record_reachable(pid)
                return True
        if _is_connect_class:
            _lan_discovery.invalidate_endpoint(pid)
            # Demote the dialed address in the persisted fallback
            # lists so the next resolution (mDNS-miss path) tries a
            # different candidate instead of the same dead head.
            # Skip when THIS device has no network at all
            # (ENETUNREACH) — the address may be fine.
            if not ('Errno 101' in msg
                    or 'Network is unreachable' in msg):
                try:
                    if _peers.demote_static_endpoint(
                            pid, f'{host}:{port}'):
                        print(f'[lan-push] demoted stale endpoint '
                              f'{host}:{port} for {pid[:8]!r}',
                              file=sys.stderr, flush=True)
                except Exception as dem_ex:
                    print(f'[lan-push] endpoint demotion failed: '
                          f'{dem_ex!r}', file=sys.stderr, flush=True)
            # Distinguish "this device has no network at all"
            # (errno 101 ENETUNREACH — no default route on any
            # interface) from "peer specifically unreachable on
            # this network" (errno 113 EHOSTUNREACH — we have a
            # network but can't reach this IP) from generic
            # connection refused (errno 111 ECONNREFUSED — IP
            # reachable but the listener isn't accepting on the
            # port we tried; usually means peer process / listener
            # is down or rebound). The three errnos point at very
            # different field problems and the previous lumped log
            # line forced a back-and-forth of "is this phone
            # online?" before diagnosis could even start.
            if 'Errno 101' in msg or 'Network is unreachable' in msg:
                cause = ('this device has no network route '
                         '(ENETUNREACH) — check WiFi / airplane '
                         'mode on THIS device')
            elif ('Errno 113' in msg
                  or 'No route to host' in msg):
                cause = (f'no route to {host} on this network '
                         f'(EHOSTUNREACH) — peer device is likely '
                         f'offline or on a different network')
            elif 'Errno 111' in msg or 'Connection refused' in msg:
                cause = (f'{host}:{port} refused the connection '
                         f'(ECONNREFUSED) — peer daemon / listener '
                         f'is down or rebound to a different port')
            elif _is_connect_timeout:
                cause = (f'connect to {host}:{port} timed out — '
                         f'address is likely stale (recorded on a '
                         f'previous network) or the peer is asleep')
            else:
                cause = 'unspecified connection failure'
            print(f'[lan-push] {pid[:8]!r} at {host}:{port} '
                  f'refused / unreachable: {cause}{_route_hint(host)} '
                  f'— invalidated mDNS cache for re-resolve',
                  file=sys.stderr, flush=True)
            # Fast-fail gate (0.50.49): record the observation so
            # subsequent push / sweep / signalling calls within
            # the cooldown skip without re-paying the retry storm.
            #
            # Endpoint AND peer (0.55.83). The endpoint marker lets
            # ``_resolve_endpoint`` walk past this address to another one
            # in the ladder; the peer marker is the coarse gate that
            # stops a fan-out storm. Marking only the peer condemned a
            # device that was reachable at a different address.
            _record_endpoint_dead(pid, host, port)
            _record_unreachable(pid)
            # Track consecutive failures; after the threshold, do
            # what manually toggling LAN off+on would do — restart
            # discovery to clear NsdManager's internal stale-
            # advertisement state. Just clearing our cache isn't
            # enough when the peer rebound to a new port and
            # NsdManager hasn't surfaced an update event for the
            # rebind. Reset counter after the restart so we don't
            # restart again on the very next failure.
            n = _consec_failures.get(pid, 0) + 1
            _consec_failures[pid] = n
            if n >= _RESTART_DISCOVERY_THRESHOLD:
                print(f'[lan-push] {pid[:8]!r}: {n} consecutive '
                      f'refused — restarting discovery to clear '
                      f'stale NsdManager state',
                      file=sys.stderr, flush=True)
                try:
                    _lan_discovery.restart_browse()
                except Exception as restart_ex:
                    print(f'[lan-push] restart_browse raised: '
                          f'{restart_ex!r}',
                          file=sys.stderr, flush=True)
                _consec_failures[pid] = 0
            return False
        if cls == 'DivergedBranches':
            # Try the lift-aware three-way merge path: fetch peer's
            # commits into our local repo, hand off to
            # ``repo._merge_diverged`` (the same code the daemon
            # uses against github), then retry the push. Same
            # truncation / catastrophic-loss guards apply because
            # ``three_way_merge`` is remote-agnostic. Per the
            # parked-spec § Conflict semantics — "No new merge
            # code; lift_merge handles divergent histories
            # identically regardless of which remote the
            # divergence came from."
            return _merge_then_push(project, url, pm, pid, host, port)
        print(f'[lan-push] push to {pid[:8]!r} at {host}:{port} '
              f'failed: {ex!r}', file=sys.stderr, flush=True)
        return False
    # Post-flight verify: dulwich's smart-protocol receive-pack
    # NAKs a non-FF push via the protocol body — porcelain.push
    # returns WITHOUT raising in that case (HTTP layer succeeded;
    # only the per-ref update was rejected). Field repro
    # (2026-05-26): two phones recorded concurrently and ended up
    # at divergent SHAs; both phones logged ``advanced ...`` per
    # the absence of an exception, neither actually delivered
    # anything, and the peers stayed diverged across multiple
    # drain ticks. The fix: re-ls-remote and compare. If the
    # peer's main isn't at our local HEAD after the push, treat
    # it as a silent non-FF and fall through to
    # ``_merge_then_push`` (lift-aware three-way fetch + merge +
    # push — same code path the ``DivergedBranches`` exception
    # already triggers above).
    if local_head:
        try:
            post_peer_head = _peek_peer_main(url, peek_pm, pid)
        except Exception as ex:
            post_peer_head = None
            print(f'[lan-push] post-flight ls-remote raised: '
                  f'{ex!r}; assuming push landed',
                  file=sys.stderr, flush=True)
        if post_peer_head and post_peer_head != local_head:
            print(f'[lan-push] {pid[:8]!r}: push returned 200 but '
                  f'peer main still at {post_peer_head[:12]!r} '
                  f'(expected {local_head[:12]!r}) — silent non-FF '
                  f'rejection; falling through to merge',
                  file=sys.stderr, flush=True)
            return _merge_then_push(
                project, url, pm, pid, host, port)
    # Push really did land. Compare what we pushed (local_head)
    # against what the peer had before. ``in-sync`` when
    # pre_peer_head was already equal (we already short-circuit
    # above for that, so this only fires when the ls-remote pre-
    # check itself was unreachable). ``advanced`` gives the user
    # a clear before/after they can correlate with their commit
    # history.
    if pre_peer_head is None:
        print(f'[lan-push] pushed {project.langcode!r} → '
              f'{pid[:8]!r} at {host}:{port} (pre-state unknown)',
              file=sys.stderr, flush=True)
    else:
        print(f'[lan-push] advanced {pid[:8]!r} main: '
              f'{pre_peer_head[:12]!r} → '
              f'{(local_head or "?")[:12]!r}',
              file=sys.stderr, flush=True)
    # Record the SHA we delivered so ``project_status`` can compute
    # ``lan_unshared`` and ``at_risk`` (the LAN/intersection axes
    # of the 5-state sync indicator). Two bookkeeping fields:
    #   - ``last_lan_pushed_sha`` (project-wide): kept for back-
    #     compat with anything still reading it.
    #   - per-peer ``last_seen_main`` in peers.json: the post-flight
    #     verify just confirmed the peer is at local_head, so this
    #     is a verified observation. ``repo._lan_unshared`` and
    #     ``repo._at_risk`` (v0.47.0; was ``server._unshared_commit_count``
    #     in 0.46.x) walk against this.
    if local_head:
        try:
            from . import projects as _projects
            _projects.set_last_lan_pushed_sha(
                project.langcode, local_head)
        except Exception as ex:
            print(f'[lan-push] set_last_lan_pushed_sha raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
        try:
            _peers.set_peer_last_seen_main(
                pid, project.langcode, local_head)
        except Exception as ex:
            print(f'[lan-push] set_peer_last_seen_main raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
        # Verified delivery of OUR commit — record covered-local
        # coverage for the walkers' unknown-peer-head fallback.
        try:
            _peers.set_peer_covered_local(
                pid, project.langcode, local_head)
        except Exception as ex:
            print(f'[lan-push] set_peer_covered_local raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
        # peer_main_shas changed → lan_unshared / at_risk on our side
        # just dropped (and the peer's project_status also changed,
        # but that's the peer's daemon to broadcast). Push-notify
        # observers on this device.
        try:
            from .android_cp import notify as _notify
            _notify.notify_project_changed(project.langcode)
        except Exception:
            pass
    _consec_failures.pop(pid, None)  # success: reset counter
    return True


def _local_head_sha(project):
    """Return the local repo's current HEAD as hex SHA, or ``''``
    on failure. Used by the push pre/post-flight to differentiate
    delivery from no-op."""
    try:
        from dulwich.repo import Repo
        repo = Repo(project.working_dir)
        try:
            head = repo.refs[b'HEAD']
            return head.decode('ascii') if isinstance(head, bytes) \
                else str(head)
        finally:
            repo.close()
    except Exception:
        return ''


def _peer_is_ancestor_of_local(project, peer_sha_hex):
    """Is *peer_sha_hex* an ancestor of our local HEAD?

    Returns True if yes — meaning a normal ``porcelain.push`` to
    update peer's main to our HEAD is a fast-forward and safe.
    False otherwise: peer has commits we don't (or histories
    diverged); the push would be a force-overwrite under dulwich's
    smart-protocol receive-pack (which uses ``set_if_equals``, a
    stale-write guard, NOT an FF check) and would silently clobber
    the peer's local progress. In that case the caller should
    route to ``_merge_then_push`` (lift-aware three-way merge).

    Walks local HEAD's ancestry looking for the peer's commit.
    For typical AZT field projects (~hundreds of commits) this is
    fast even on phones; we cap at 10k commits as a safety net so
    a pathological history never hangs the drain.

    Returns False on any exception or when the peer's commit
    isn't in our object store at all.
    """
    if not peer_sha_hex:
        return False
    try:
        from dulwich.repo import Repo
        repo = Repo(project.working_dir)
        try:
            peer_sha = peer_sha_hex.encode('ascii')
            try:
                local_head = repo.refs[b'HEAD']
            except KeyError:
                return False
            # Object-store membership is a cheap pre-filter — if
            # peer's commit isn't even in our store, it can't be
            # an ancestor.
            if peer_sha not in repo.object_store:
                return False
            try:
                walker = repo.get_walker(include=[local_head])
                for i, entry in enumerate(walker):
                    if entry.commit.id == peer_sha:
                        return True
                    if i > 10000:
                        # Safety cap; let merge path take over on
                        # implausibly-long histories.
                        return False
            except Exception:
                return False
            return False
        finally:
            try:
                repo.close()
            except Exception:
                pass
    except Exception:
        return False


def peek_peer_head(peer_id, langcode):
    """Public peek-only helper (0.50.50). Resolves *peer_id*'s
    endpoint, builds a TLS-pinned pool, ls-remotes their main
    branch on *langcode*. Returns the SHA (hex string) or None.

    Cheaper than ``_push_to_peer`` — no push attempt, no
    post-flight, just one ls-remote round-trip. Used by the
    receiver-side ``_refresh_peer_last_seen_after_receive`` flow
    (0.50.50): after our listener accepts a push, we don't know
    which paired peer originated it from the smart-protocol
    metadata alone, so we peek each candidate peer's main and
    update ``last_seen_main`` for the ones at our new HEAD.

    Honors the fast-fail gate — a recently-unreachable peer
    returns None without paying connect timeouts. On observed
    success, also clears the gate."""
    if not peer_id or not langcode:
        return None
    if _recently_unreachable(peer_id):
        return None
    entry = _peers.get_peer(peer_id)
    if entry is None:
        return None
    expected_fp = entry.get('fp', '')
    if not expected_fp:
        return None
    endpoint = _resolve_endpoint(entry)
    if endpoint is None:
        return None
    host, port = endpoint
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-peek] context build failed for '
              f'{peer_id[:8]!r}: {ex!r}',
              file=sys.stderr, flush=True)
        return None
    try:
        # Tight bounds: peeks must fail fast on dead endpoints
        # (see _pinned_pool_manager for the timeout semantics).
        pm = _pinned_pool_manager(ctx, expected_fp,
                                  connect=5, read=10)
    except Exception as ex:
        print(f'[lan-peek] pool build failed for '
              f'{peer_id[:8]!r}: {ex!r}',
              file=sys.stderr, flush=True)
        return None
    url = f'https://{host}:{int(port)}/{langcode}.git'
    sha = _peek_peer_main(url, pm, peer_id)
    if sha:
        _record_reachable(peer_id)
    else:
        # ls-remote failure usually means connect failure; record
        # so subsequent attempts skip via fast-fail.
        _record_unreachable(peer_id)
    return sha


def _peek_peer_main(url, pm, pid):
    """ls-remote the peer's listener for the peer's current
    canonical commit. Returns the SHA (hex string) or ``None``
    if we couldn't reach the peer / parse the response. Cheap —
    protocol round-trip only, no packfile transfer.

    **Prefers ``HEAD`` over ``refs/heads/main``** (since 0.46.4).
    Reason: dulwich's smart-protocol receive-pack uses
    ``set_if_equals`` and ACCEPTS non-FF pushes as silent force-
    overwrites. After a force-overwrite, the peer's
    ``refs/heads/main`` reflects what *we* pushed, not what the
    peer actually has locally. The peer's own latest commits
    live at their ``HEAD`` (which is detached from main once
    main has been clobbered). Reading ``HEAD`` first gives us
    the peer's actual current state — which is what the FF
    check and merge logic need. Falls back to ``refs/heads/main``
    when ``HEAD`` is absent (rare; bare-mode or odd setups).
    """
    try:
        from dulwich.client import HttpGitClient
        from urllib.parse import urlparse
        parsed = urlparse(url)
        base = f'{parsed.scheme}://{parsed.netloc}'
        path = parsed.path or '/'
        client = HttpGitClient(base, pool_manager=pm)
        refs = client.get_refs(path)
        # Dulwich returns either ``{ref_bytes: sha_bytes}`` directly
        # or a wrapper with ``.refs``. Handle both.
        if hasattr(refs, 'refs'):
            refs = refs.refs
        main = refs.get(b'HEAD') or refs.get(b'refs/heads/main')
        _record_probe(url, pid, 'ok')
        if isinstance(main, bytes):
            return main.decode('ascii')
        return main
    except Exception as ex:
        # Derive the langcode from the URL path ('/nml.git' → 'nml') so
        # the classifier can consult the share manifest without a
        # signature change at every call site (same trick
        # ``_record_probe`` uses).
        _lang = ''
        try:
            _tail = str(url or '').rstrip('/').rsplit('/', 1)[-1]
            _lang = _tail[:-4] if _tail.endswith('.git') else _tail
        except Exception:
            _lang = ''
        outcome = _classify_peek_failure(ex, pid=pid, langcode=_lang)
        _record_probe(url, pid, outcome, repr(ex))
        _why = {
            'unshared': " — they don't list it as shared with us; a "
                        "user must grant it, retrying won't help",
            'absent': ' — they DO list it as shared but cannot serve it '
                      'yet (clone in flight, or a transient registry '
                      'read); worth retrying',
            'not_served': ' — refused, reason unknown (no share '
                          'manifest from them yet)',
        }.get(outcome, '')
        print(f'[lan-push] ls-remote peek failed for {pid[:8]!r} '
              f'{_lang!r} [{outcome}]: {ex!r}{_why}',
              file=sys.stderr, flush=True)
        return None


def _classify_peek_failure(ex, pid='', langcode=''):
    """Bucket a failed peek so the board can distinguish a CONNECTIVITY
    problem from a RECIPROCATION one (0.55.20).

    Kent 2026-07-27: *"I haven't been able to distinguish between such
    problems and more normal 'I can't be bothered to reciprocate'
    problems."* The two have completely different remedies — move the
    devices onto one network, versus fix a share — and both presented
    identically as a row that stopped updating.

    - ``timeout`` / ``no_route`` / ``refused`` — we never got an answer:
      connectivity. (``refused`` is host-up-listener-down, which is
      worth separating: the network is fine.)
    - ``unshared`` / ``absent`` / ``not_served`` — they ANSWERED and
      would not serve this project; see below for how the three are
      told apart.
    - ``error`` — anything else; the detail string carries it.

    **`NotGitRepository` is seven different answers (0.55.60).** Kent:
    *"are we still reporting NGR for 'not sharing that with you'?"* We
    were. `lan_listener.open_repository` raises that one exception for:
    not registered, ghost directory, clone-in-flight, **peer registry
    temporarily unreadable**, not-shared-with-this-peer, not-shared-with-
    anyone, and repo-failed-to-open. Two of those are retryable within
    seconds; two need a user to grant a share and will never succeed on
    their own. Calling them all ``not_served`` asserted a cause we hadn't
    established.

    The reason cannot come over the wire: our listener raises it with a
    useful message, but dulwich's *client* mints its own exception from
    the HTTP failure, which is why the field log shows a bare
    ``NotGitRepository()`` with empty args.

    So disambiguate out-of-band with the hello share manifest (0.55.50),
    which already records what each peer says it shares:

    - manifest says they DON'T share it → ``unshared``. A user has to
      grant it; dialing again changes nothing.
    - manifest says they DO share it → ``absent``. They mean to serve it
      but can't yet — mid-clone, or a transient registry read. Worth
      retrying.
    - no manifest yet → ``not_served``, the honest "they refused and we
      don't know why.\""""
    name = type(ex).__name__
    text = f'{ex!r} {ex}'.lower()
    if 'notgitrepository' in name.lower():
        if pid and langcode:
            theirs = _their_shared_projects(pid)
            if isinstance(theirs, list):
                return ('absent' if langcode in theirs else 'unshared')
        return 'not_served'
    if 'timed out' in text or 'timeout' in text:
        return 'timeout'
    if 'no route to host' in text or 'errno 113' in text:
        return 'no_route'
    if 'connection refused' in text or 'errno 111' in text:
        return 'refused'
    if 'unreachable' in text or 'errno 101' in text:
        return 'no_route'
    return 'error'


def _record_probe(url, pid, outcome, detail=''):
    """Persist a peek outcome against (peer, project). The langcode is
    derived from the URL path (``/nml.git`` → ``nml``) so this needs no
    signature change at the several ``_peek_peer_main`` call sites."""
    try:
        from urllib.parse import urlparse
        path = (urlparse(url).path or '').lstrip('/')
        langcode = path.split('.git', 1)[0] if '.git' in path \
            else path.split('/', 1)[0]
        if langcode and pid:
            _peers.set_peer_probe_result(pid, langcode, outcome, detail)
    except Exception:
        pass


_merge_fail = {}                 # {(pid, langcode): {n, next_at, head}}
_merge_fail_lock = threading.Lock()
_MERGE_BACKOFF_BASE_S = 60.0
_MERGE_BACKOFF_MAX_S = 900.0


def _merge_attempt_due(pid, langcode):
    """Is a divergence-merge attempt against this peer+project due?

    **Repeated doomed merges had no backoff at all (0.55.62).** Field
    2026-07-28, one peer over 15 minutes: six full merge attempts, each
    burning a ~90 s fetch, every one ending

        [lan-merge] fetch from '80570dd9' failed:
            GitProtocolError('EOF occurred in violation of protocol')
        … then: unexpected http resp 500 for …/nml.git/git-upload-pack

    `lan_backoff` governs the post-commit burst, but reverse delivery and
    mDNS arrival call straight into the push path, so nothing throttled
    the retry. Two radios, two CPUs, two batteries, every two minutes,
    for a fetch that cannot succeed while the far end can't serve.

    Same shape as the reset-failure curve: 60 s doubling to 15 min,
    cleared by any success. A peer that comes back healthy is picked up on
    the next tick after its window, not minutes later."""
    key = (str(pid or ''), str(langcode or ''))
    with _merge_fail_lock:
        st = _merge_fail.get(key)
        if not st:
            return True
        remain = st.get('next_at', 0.0) - _time_mod.time()
        if remain <= 0:
            return True
    print(f'[lan-merge] {key[0][:8]!r} {key[1]!r}: skipping — '
          f'{st.get("n", 0)} consecutive failure(s), next attempt in '
          f'{int(remain)}s', file=sys.stderr, flush=True)
    return False


def _note_merge_attempt_head(pid, langcode, peer_head):
    """Record the peer head this attempt is aiming at, and say so when it
    MOVED since the last attempt (0.55.62).

    A moving target is its own failure mode and was invisible: I only
    caught it by hand-diffing six log lines. If the peer commits faster
    than we can fetch-and-merge, no amount of retrying converges — the
    remedy is on that device, not here."""
    key = (str(pid or ''), str(langcode or ''))
    head = str(peer_head or '')[:12]
    with _merge_fail_lock:
        st = _merge_fail.get(key) or {}
        prev = str(st.get('head', '') or '')
        if prev and head and prev != head:
            st['head'] = head
            _merge_fail[key] = st
            moved = prev
        else:
            st['head'] = head
            _merge_fail[key] = st
            moved = ''
    if moved:
        print(f'[lan-merge] {key[0][:8]!r} {key[1]!r}: peer head MOVED '
              f'{moved} → {head} since our last attempt — they are '
              f'committing faster than we can absorb; retrying alone '
              f'will not converge', file=sys.stderr, flush=True)


def _note_merge_failure(pid, langcode, phase):
    """Advance the merge-failure curve for this peer+project."""
    key = (str(pid or ''), str(langcode or ''))
    with _merge_fail_lock:
        st = _merge_fail.get(key) or {}
        n = int(st.get('n', 0)) + 1
        delay = min(_MERGE_BACKOFF_BASE_S * (2 ** (n - 1)),
                    _MERGE_BACKOFF_MAX_S)
        st['n'] = n
        st['next_at'] = _time_mod.time() + delay
        _merge_fail[key] = st
    print(f'[lan-merge] {key[0][:8]!r} {key[1]!r}: {phase} failed '
          f'({n} consecutive) — next attempt in {int(delay)}s',
          file=sys.stderr, flush=True)


def clear_merge_failure(pid, langcode):
    """Called on a successful merge+push: drop the curve entirely."""
    key = (str(pid or ''), str(langcode or ''))
    with _merge_fail_lock:
        _merge_fail.pop(key, None)


def _merge_then_push(project, url, pm, pid, host, port):
    """Divergence-recovery path for the LAN fan-out. Fetches the
    peer's commits over our pinned-TLS pool, runs the daemon's
    existing lift-aware three-way merge against them
    (``repo._merge_diverged`` — same code path as github sync),
    then retries the push as a fast-forward. Returns ``True`` on
    successful merge+push, ``False`` on any failure.

    Reuses the daemon's merge truncation / catastrophic-loss
    guards by going through ``_merge_diverged``; conflicts get
    the standard ``<azt-lift-conflict>`` annotation and a
    forensic diagnostic dump under
    ``<working_dir>/.azt-collab/diagnostics/``. The merge commit
    has both parents (our HEAD + peer HEAD), bot author.

    **Two phases since 0.55.24: fetch UNLOCKED, then mutate LOCKED.**
    The fetch only adds content-addressed objects, so it is safe outside
    the lock; ``_merge_diverged`` (working tree + index + HEAD) and the
    post-merge push read/write local state and stay inside it, against
    the same hazards the github path locks against (concurrent
    ``commit_project``, ``atomic_finalize``, post-receive reset — see
    ``_sync_repo_locked`` in repo.py). LAN delivery is opportunistic; a
    5 s timeout means we skip this round if the project is busy and the
    next drain pass retries — and that timeout is now bounded by LOCAL
    work only, instead of being a hostage to a peer's socket.
    """
    # PHASE 1 — fetch the peer's objects with NO LOCK HELD (0.55.24).
    #
    # This used to happen inside the lock. Watchdog stack, field
    # 2026-07-27 22:07:
    #
    #   project lock '60206912458536ae.lock' held 147s by
    #   thread 'lan-reverse-deliver'
    #     lan_push.py:986  _merge_then_push_locked
    #     dulwich/client.py:1671 fetch → _read_side_band64k_data
    #     ssl.py:1167 read            ← parked here, 147 s
    #
    # A peer on a slow or half-dead link therefore held the project
    # against every other worker for as long as its socket took, which
    # is what produced `post-receive reset 'nml': lock busy (5s
    # timeout)` every 20–40 s for minutes on all four devices: inbound
    # data landed and could never be absorbed because an OUTBOUND merge
    # was parked in a socket read.
    #
    # Safe to hoist because a fetch only ADDS to the object store, and
    # git objects are content-addressed: two writers cannot disagree
    # about what a SHA means, and nothing observes these objects until a
    # ref points at them — which still happens under the lock in phase
    # 2. Refs, index, working tree and HEAD are untouched here.
    _lang = str(getattr(project, 'langcode', '') or '')
    if not _merge_attempt_due(pid, _lang):
        return False
    peer_head = _fetch_peer_objects_unlocked(project, url, pm, pid)
    if peer_head is None:
        _note_merge_failure(pid, _lang, 'fetch')
        return False
    _note_merge_attempt_head(pid, _lang, peer_head)
    # PHASE 2 — everything that mutates local state, under the lock.
    # Now bounded by local work only, so the 5 s timeout means what it
    # says instead of being a hostage to the network.
    try:
        with project_lock(project.working_dir, timeout=5.0):
            ok = _merge_then_push_locked(
                project, url, pm, pid, host, port,
                peer_head=peer_head)
        if ok:
            clear_merge_failure(pid, _lang)
        else:
            _note_merge_failure(pid, _lang, 'merge')
        return ok
    except LockTimeout:
        # NOT a failure of this peer or project — our own lock was busy.
        # Deliberately does NOT advance the curve: penalising a peer for
        # our local contention would push a healthy link out to 15 min.
        print(f'[lan-merge] {pid[:8]!r}: project busy — deferring '
              f'merge; next drain pass will retry (backoff curve '
              f'untouched — local contention, not their fault)',
              file=sys.stderr, flush=True)
        return False


_fetch_inflight = set()          # {(peer_id, langcode)} being fetched
_fetch_inflight_lock = threading.Lock()


def _fetch_peer_objects_unlocked(project, url, pm, pid):
    """Populate our object store from *pid*'s refs, holding NO lock.

    Returns the peer's canonical head as a hex ``bytes`` sha, or None if
    the fetch failed or the peer exposed no usable ref. Also records the
    tip observation — that's a read of their refs, valid regardless of
    what we do with it next, and doing it here means a peer whose merge
    later defers on a busy lock still leaves an honest board entry.

    Prefers ``HEAD`` over ``refs/heads/main`` for the same reason
    ``_peek_peer_main`` does: after a force-overwrite the peer's real
    state lives at HEAD.

    **One fetch per (peer, project) at a time (0.55.52).** Until 0.55.24
    this ran under ``project_lock``, which — besides protecting local
    state — had the accidental effect of SERIALISING these fetches: one
    merge at a time meant one pack request at a time. Hoisting the fetch
    out of the lock fixed the freeze and removed that serialisation, so
    every trigger (sweep, fan-out, reverse delivery, burst, scheduler)
    began fetching simultaneously.

    Field 2026-07-28: five concurrent `git-upload-pack` requests to one
    tablet inside the same millisecond, again six inside 400 ms. It
    answered with read timeouts, `HTTP 500`, hangups and
    `ConnectionResetError(104)` — a tablet cannot serve six simultaneous
    packs of a large history. Every merge failed at the fetch, so the
    desktop's head sat unchanged for 63 minutes while the tablet's moved
    23 times.

    The lock must NOT come back (that was the 147 s stall), so the
    serialisation gets its own guard: no lock held, network-only,
    keyed per peer+project. A loser returns None and the caller retries
    on its next pass — cheaper than a request the peer will drop."""
    from dulwich.repo import Repo
    key = (str(pid or ''), str(getattr(project, 'langcode', '') or ''))
    with _fetch_inflight_lock:
        if key in _fetch_inflight:
            print(f'[lan-merge] {pid[:8]!r} '
                  f'{key[1]!r}: fetch already in flight — skipping this '
                  f'pass (avoids piling concurrent upload-packs on one '
                  f'peer)', file=sys.stderr, flush=True)
            return None
        _fetch_inflight.add(key)
    try:
        return _fetch_peer_objects_inner(project, url, pm, pid)
    finally:
        with _fetch_inflight_lock:
            _fetch_inflight.discard(key)


def _fetch_peer_objects_inner(project, url, pm, pid):
    """Body of ``_fetch_peer_objects_unlocked``; see it for contract.
    Split so the in-flight guard has a single release point."""
    from dulwich.repo import Repo
    repo = None
    try:
        repo = Repo(project.working_dir)
    except Exception as ex:
        print(f'[lan-merge] open repo failed (fetch phase): {ex!r}',
              file=sys.stderr, flush=True)
        return None
    try:
        from dulwich.client import HttpGitClient
        from urllib.parse import urlparse
        parsed = urlparse(url)
        base = f'{parsed.scheme}://{parsed.netloc}'
        path = parsed.path or '/'
        client = HttpGitClient(base, pool_manager=pm)
        fetch_result = client.fetch(path, repo)
    except Exception as ex:
        print(f'[lan-merge] fetch from {pid[:8]!r} failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None
    finally:
        try:
            repo.close()
        except Exception:
            pass
    peer_refs = getattr(fetch_result, 'refs', None) or {}
    peer_head = peer_refs.get(b'HEAD') \
        or peer_refs.get(b'refs/heads/main')
    if peer_head is None:
        print(f'[lan-merge] {pid[:8]!r}: no main / HEAD ref in fetch '
              f'result; refs={list(peer_refs.keys())!r}',
              file=sys.stderr, flush=True)
        return None
    try:
        _peers.set_peer_last_seen_main(
            pid, project.langcode,
            peer_head.decode('ascii', 'replace')
            if isinstance(peer_head, bytes) else str(peer_head))
    except Exception as ex:
        print(f'[lan-merge] set_peer_last_seen_main {pid[:8]!r} '
              f'raised: {ex!r}', file=sys.stderr, flush=True)
    return peer_head


def _merge_then_push_locked(project, url, pm, pid, host, port,
                            peer_head=None):
    """*peer_head* (hex ``bytes``) comes from
    ``_fetch_peer_objects_unlocked``, which has already populated our
    object store outside the lock (0.55.24). It is required; the inline
    fetch that used to live here is gone, because holding the lock
    across a socket read is the regression this split exists to end."""
    from dulwich import porcelain
    from dulwich.repo import Repo
    from . import repo as _repo_mod

    try:
        repo = Repo(project.working_dir)
    except Exception as ex:
        print(f'[lan-merge] open repo failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    try:
        # Stash-and-reapply pattern for pending working-tree edits.
        #
        # ``_merge_diverged`` walks committed trees and overwrites
        # the working tree with the merged committed state — so an
        # unstaged edit on this peer would silently get clobbered.
        # Field-observed symptom: red ``+N`` lingering after a
        # swipe means the user has edits we haven't committed yet
        # (porcelain.add occasionally no-ops in edge cases the
        # field has seen but we haven't fully diagnosed).
        #
        # Three-step protection:
        #   1. Snapshot working-tree bytes for every unstaged-mod
        #      path BEFORE anything runs.
        #   2. Try the pre-commit. If it succeeds (COMMITTED_LOCAL)
        #      or there's nothing to commit (NOTHING_TO_COMMIT),
        #      drop the snapshot — the edits are either captured
        #      as a real commit (one of the merge's parents) or
        #      didn't exist.
        #   3. If the pre-commit failed (or raised), keep the
        #      snapshot. After ``_merge_diverged`` writes the merge
        #      result, ``reapply_snapshot_after_merge`` writes the
        #      snapshot back — lift-aware for ``.lift`` paths
        #      (lift_merge three-way), keep-ours for other paths.
        snapshot = _repo_mod.snapshot_unstaged_paths(
            repo, project.working_dir)
        pre_merge_head_sha = None
        try:
            pre_merge_head_sha = repo.refs[b'HEAD']
        except Exception:
            pass
        try:
            from . import store as _store_mod
            contributor = _store_mod.get_contributor() or 'AZT'
            pre_result = _repo_mod.Result()
            _repo_mod._commit_step_locked(
                repo, project.working_dir, contributor, pre_result)
            if pre_result.has(_repo_mod.S.COMMITTED_LOCAL):
                # Edits are now in a real commit; the merge will
                # include them as a parent. Snapshot no longer
                # needed.
                snapshot = {}
                try:
                    new_head = repo.refs[b'HEAD']
                    print(f'[lan-merge] {pid[:8]!r}: auto-committed '
                          f'pending working-tree edits before merge '
                          f'→ {new_head[:12].decode()}',
                          file=sys.stderr, flush=True)
                except Exception:
                    pass
            elif pre_result.has(_repo_mod.S.NOTHING_TO_COMMIT):
                # Clean working tree. Snapshot would be empty
                # anyway, but be explicit.
                snapshot = {}
            else:
                # COMMIT_FAILED, COMMIT_REPEATEDLY_FAILED, or
                # similar. Snapshot stays held for post-merge
                # reapply. Don't lose user data even when the
                # committer is mis-behaving.
                if snapshot:
                    print(f'[lan-merge] {pid[:8]!r}: pre-merge '
                          f'commit returned '
                          f'codes={pre_result.codes()!r}; will '
                          f'reapply {len(snapshot)} working-tree '
                          f'path(s) after merge',
                          file=sys.stderr, flush=True)
        except Exception as ex:
            # Pre-commit raised. Keep snapshot for post-merge
            # reapply.
            if snapshot:
                print(f'[lan-merge] {pid[:8]!r}: pre-merge commit '
                      f'raised {ex!r}; will reapply '
                      f'{len(snapshot)} working-tree path(s) '
                      f'after merge',
                      file=sys.stderr, flush=True)
            else:
                print(f'[lan-merge] {pid[:8]!r}: pre-merge commit '
                      f'raised {ex!r}; no working-tree edits to '
                      f'preserve, proceeding',
                      file=sys.stderr, flush=True)

        # The fetch and the tip resolution both happened in
        # ``_fetch_peer_objects_unlocked``, before this lock was taken
        # (0.55.24) — see ``_merge_then_push`` for why the network step
        # must not run in here. The peer's objects are already in our
        # store and ``peer_head`` is what that phase resolved, so from
        # here down everything is local work.
        if peer_head is None:
            print(f'[lan-merge] {pid[:8]!r}: no peer_head supplied; '
                  f'caller must fetch first',
                  file=sys.stderr, flush=True)
            return False
        # The tip observation is recorded by the fetch phase (0.55.11
        # put it on the merge path so every downstream branch —
        # converged / FF / we're-behind / identical-trees / three-way —
        # leaves the board with a real ``last_seen_main``; 0.55.24 moved
        # it earlier, which also means a merge that later defers on a
        # busy lock still leaves an honest board entry).
        try:
            local_head = repo.refs[b'HEAD']
        except KeyError:
            print(f'[lan-merge] local HEAD missing; skipping',
                  file=sys.stderr, flush=True)
            return False

        if local_head == peer_head:
            # Already same tip — push was probably a transient
            # race; nothing to merge.
            print(f'[lan-merge] {pid[:8]!r}: heads converged '
                  f'between push and merge fetch; nothing to do',
                  file=sys.stderr, flush=True)
            return True

        # Ancestry short-circuits — never mint an empty-diff merge
        # commit when one side already contains the other. A merge
        # of two commits where one is an ancestor of the other has
        # a tree identical to a parent (writes_done=0); such commits
        # ping-pong between LAN peers, growing parallel empty-merge
        # chains (the 0.46.x loop family — the github phase-b sync
        # path guards the same way at repo.py). Only applied on a
        # clean working tree (``not snapshot``): with pending edits
        # the merge is legitimate (non-empty) and the reapply path
        # below must run. Neither branch mutates the working tree,
        # so there is no data-loss surface here.
        if not snapshot:
            try:
                peer_in_local = _repo_mod._is_ancestor(
                    repo, peer_head, local_head)
            except Exception:
                peer_in_local = False
            if peer_in_local:
                # We already contain the peer's history — local is
                # strictly ahead. Skip the merge; push our head so
                # THEY fast-forward to us (HEAD descends from
                # peer_head, so it's a valid FF for the peer).
                print(f'[lan-merge] {pid[:8]!r}: local '
                      f'{local_head[:12]!r} already contains peer '
                      f'{peer_head[:12]!r} — FF-push, no merge commit',
                      file=sys.stderr, flush=True)
                try:
                    repo.close()
                except Exception:
                    pass
                try:
                    porcelain.push(
                        project.working_dir,
                        remote_location=url,
                        refspecs=[b'HEAD:refs/heads/main'],
                        pool_manager=pm,
                    )
                except Exception as ex:
                    print(f'[lan-merge] FF-push to {pid[:8]!r} '
                          f'failed: {ex!r}',
                          file=sys.stderr, flush=True)
                    return False
                print(f'[lan-push] FF-pushed {project.langcode!r} → '
                      f'{pid[:8]!r} at {host}:{port} (no merge)',
                      file=sys.stderr, flush=True)
                return True
            try:
                local_in_peer = _repo_mod._is_ancestor(
                    repo, local_head, peer_head)
            except Exception:
                local_in_peer = False
            if local_in_peer:
                # Peer is strictly ahead and contains all our work:
                # this is a FAST-FORWARD, and we take it ourselves
                # (0.55.45).
                #
                # Until now we declined and waited for the peer's own
                # fan-out to push its head to us, on the grounds that
                # advancing here would mean a hard reset down a
                # "less-guarded" path. Two things make that wrong:
                #
                # 1. **LAN sync is push-only, so a device that is
                #    behind has no way to catch itself up.** It can
                #    only wait to be pushed to — and when the reverse
                #    direction doesn't work (the one-way reachability
                #    that dominates this rig), it waits forever while
                #    its board reads "to merge". Field 2026-07-28: the
                #    desktop sat at 5358186c while two peers held
                #    42d89722, rediscovering it every few minutes for
                #    35+ minutes, logging "convergence is reached" each
                #    time. It was not reached.
                # 2. **We already hold their objects.** Since 0.55.24
                #    the fetch happens before this check, so peer_head
                #    is in our store. Nothing needs downloading and
                #    nothing needs merging — a fast-forward has no
                #    conflict surface at all.
                #
                # The "less-guarded" objection is answered by reusing
                # the SAME function the receive path uses:
                # ``_reset_working_tree_after_receive`` (its truncation
                # guards, atomic-pending check, and notify). And this
                # whole branch is inside ``if not snapshot:`` — the
                # working tree is CLEAN — so the reset destroys no
                # uncommitted work. That is what makes it safe here and
                # would not have been safe in the general merge case.
                old_head = local_head
                ff_ref = b'refs/heads/main'
                try:
                    active = porcelain.active_branch(repo)
                    if active:
                        ff_ref = b'refs/heads/' + active
                except Exception:
                    pass    # detached HEAD → handled below
                moved = False
                try:
                    # CAS, not a blind write: if a concurrent commit
                    # advanced us between the ancestry check and here,
                    # this fails and we leave it to the next pass
                    # rather than clobbering that commit.
                    moved = bool(repo.refs.set_if_equals(
                        ff_ref, old_head, peer_head))
                    if moved and repo.refs[b'HEAD'] != peer_head:
                        # Detached HEAD: moving the branch didn't move
                        # us. Move HEAD too, same CAS discipline.
                        repo.refs.set_if_equals(
                            b'HEAD', old_head, peer_head)
                except Exception as ex:
                    print(f'[lan-merge] {pid[:8]!r}: FF ref update '
                          f'failed: {ex!r} — staying behind; next pass '
                          f'retries', file=sys.stderr, flush=True)
                    moved = False
                try:
                    repo.close()
                except Exception:
                    pass
                if not moved:
                    print(f'[lan-merge] {pid[:8]!r}: peer '
                          f'{peer_head[:12]!r} contains local '
                          f'{old_head[:12]!r} but our ref moved under '
                          f'us — no FF this pass',
                          file=sys.stderr, flush=True)
                    return True
                print(f'[lan-merge] {pid[:8]!r}: peer '
                      f'{peer_head[:12]!r} contains local '
                      f'{old_head[:12]!r} — we were behind; '
                      f'fast-forwarded ourselves to '
                      f'{peer_head[:12]!r} (no merge, clean tree)',
                      file=sys.stderr, flush=True)
                # Working tree + index still reflect the old head;
                # sync them through the guarded receive-path reset.
                # Function-local import: lan_listener imports this
                # module, so a top-level import would be circular.
                try:
                    from . import lan_listener as _lan_listener
                    _lan_listener._reset_working_tree_after_receive(
                        project.langcode)
                except Exception as ex:
                    print(f'[lan-merge] {pid[:8]!r}: post-FF working '
                          f'tree reset raised: {ex!r} — refs advanced; '
                          f'the reset queue / next commit_project will '
                          f'absorb it', file=sys.stderr, flush=True)
                # We are now AT the peer's head: they hold everything
                # we hold, and we hold everything they hold.
                for _sha in (peer_head,):
                    try:
                        _peers.set_peer_covered_local(
                            pid, project.langcode,
                            _sha.decode('ascii', 'replace')
                            if isinstance(_sha, bytes) else str(_sha))
                    except Exception:
                        pass
                return True
            # Divergent history but IDENTICAL trees — neither head is
            # an ancestor of the other, yet their content matches. The
            # empty-merge ping-pong (F8): a stale peer (pre-0.46.5)
            # keeps minting content-identical "Merge origin/main"
            # commits; three-way-merging them here would mint YET
            # ANOTHER empty-diff commit and feed the loop (history
            # bloat — wan_unshared/at_risk climb — and false reload
            # popups). We can't FF either way (no ancestry), so NO-OP:
            # don't merge, don't push. Heads stay divergent but
            # content-identical; the next REAL edit on either side
            # produces a non-empty merge that converges them. This is
            # our immunity even when the peer generator isn't updated
            # (fixing the generator = updating that stale peer).
            try:
                trees_equal = (repo[local_head].tree
                               == repo[peer_head].tree)
            except Exception:
                trees_equal = False
            if trees_equal:
                print(f'[lan-merge] {pid[:8]!r}: divergent heads '
                      f'{local_head[:12]!r}/{peer_head[:12]!r} have '
                      f'identical trees — empty-merge loop averted; '
                      f'no merge, no push',
                      file=sys.stderr, flush=True)
                try:
                    repo.close()
                except Exception:
                    pass
                return True

        # Memory pre-flight — same gate the github merge path uses
        # so we don't OOM-kill the :provider service mid-merge on a
        # low-memory device. Returns a Status if memory is below
        # ``sync.min_free_mem_mb_for_merge``; we skip the merge,
        # the next drain re-reads memory and proceeds when it
        # recovers.
        mem_status = _repo_mod._check_memory_for_merge()
        if mem_status is not None:
            print(f'[lan-merge] {pid[:8]!r}: skipping merge — '
                  f'{mem_status.code} '
                  f'(available={mem_status.params.get("mem_available_mb")}'
                  f' min={mem_status.params.get("min_required_mb")})',
                  file=sys.stderr, flush=True)
            return False

        print(f'[lan-merge] {pid[:8]!r}: local={local_head[:12]!r} '
              f'peer={peer_head[:12]!r} — running three-way merge',
              file=sys.stderr, flush=True)
        try:
            merged_sha, conflicts = _repo_mod._merge_diverged(
                repo, project.working_dir, 'main',
                local_head, peer_head)
        except _repo_mod.UnrelatedHistoriesError as ex:
            print(f'[lan-merge] REFUSED: {ex} — this peer\'s '
                  f'{project.langcode!r} is a DIFFERENT project '
                  f'sharing the langcode; no data changed on '
                  f'either side. Unshare the project from this '
                  f'peer (or rename one of the two) to stop these '
                  f'attempts.', file=sys.stderr, flush=True)
            return False
        except Exception as ex:
            print(f'[lan-merge] three-way merge raised: {ex!r}',
                  file=sys.stderr, flush=True)
            return False
        print(f'[lan-merge] merged → {merged_sha[:12]!r} '
              f'(conflicts={len(conflicts)})',
              file=sys.stderr, flush=True)

        # Snapshot reapply path: pre-commit failed (or raised),
        # so the snapshot still holds the user's unstaged edits.
        # ``_merge_diverged`` just overwrote them; restore now
        # with lift-aware merging so the user's work survives.
        # After the reapply, attempt a second commit so the
        # reapplied content lands on top of the merge commit and
        # is what we push to the peer.
        if snapshot:
            try:
                applied, conflicts_n = (
                    _repo_mod.reapply_snapshot_after_merge(
                        repo, project.working_dir, snapshot,
                        pre_merge_head_sha))
                if applied:
                    print(f'[lan-merge] {pid[:8]!r}: reapplied '
                          f'{applied} working-tree path(s) after '
                          f'merge (conflicts={conflicts_n})',
                          file=sys.stderr, flush=True)
                # Second commit pass — bundles the reapplied
                # working-tree edits on top of the merge commit.
                # If this ALSO fails to commit, the snapshot is
                # at least on disk in working_tree; the next
                # drain's commit_project will retry. User data
                # is preserved either way.
                post_result = _repo_mod.Result()
                _repo_mod._commit_step_locked(
                    repo, project.working_dir, contributor,
                    post_result)
                if post_result.has(_repo_mod.S.COMMITTED_LOCAL):
                    try:
                        new_head = repo.refs[b'HEAD']
                        print(f'[lan-merge] {pid[:8]!r}: '
                              f'reapplied snapshot committed on '
                              f'top of merge → '
                              f'{new_head[:12].decode()}',
                              file=sys.stderr, flush=True)
                    except Exception:
                        pass
                else:
                    print(f'[lan-merge] {pid[:8]!r}: post-merge '
                          f'commit returned '
                          f'codes={post_result.codes()!r}; '
                          f'snapshot stays in working tree, next '
                          f'drain retries',
                          file=sys.stderr, flush=True)
            except Exception as ex:
                print(f'[lan-merge] {pid[:8]!r}: snapshot reapply '
                      f'raised {ex!r}; user edits may be in '
                      f'working tree, not in HEAD',
                      file=sys.stderr, flush=True)
    finally:
        try:
            repo.close()
        except Exception:
            pass

    # Push the merged commit. Now fast-forward from peer's POV
    # because the merge commit has peer_head as one parent.
    try:
        porcelain.push(
            project.working_dir,
            remote_location=url,
            refspecs=[b'HEAD:refs/heads/main'],
            pool_manager=pm,
        )
    except Exception as ex:
        print(f'[lan-merge] post-merge push to {pid[:8]!r} failed: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return False
    print(f'[lan-push] pushed merged {project.langcode!r} → '
          f'{pid[:8]!r} at {host}:{port}',
          file=sys.stderr, flush=True)
    return True


def hello_to_peer(host, port, expected_fp, device_name='',
                  langcode='', out=None, peer_id_hint=''):
    """Initiate a TLS hello handshake to *host*:*port*, pinning
    *expected_fp*, and POST our identity to ``/v1/lan/hello`` so the
    remote daemon auto-reverse-records us.

    ``langcode``, when non-empty, tells the remote side which
    project we just LAN-cloned from them; the remote's listener
    adds that langcode to its ``shared_projects`` allowlist for us
    so the share is symmetric without the owner needing to tap
    Share explicitly. Empty langcode = pair-only handshake
    (legacy / no project context).

    Returns ``True`` on success, ``False`` on any failure. Logs
    detail; never raises.

    *out*, when a dict is passed, receives ``share_refused`` /
    ``share_refused_reason`` if the remote recorded the pair but
    REFUSED the langcode we asked for (its share-QR offer had lapsed).
    The return stays ``True`` in that case — the hello succeeded; it's
    the project that didn't come with it — so callers that only check
    the bool are unaffected (0.55.3).

    Called from the daemon's ``_h_lan_pair_accept`` right after a
    successful QR-scan recording, so the remote side doesn't need a
    separate QR scan in the other direction (parked spec § Pairing
    step 5)."""
    import json
    try:
        from . import peer_id as _peer_id_mod
        ident = _peer_id_mod.ensure()
    except Exception as ex:
        print(f'[lan-hello] our identity unavailable: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-hello] context build failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    try:
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=expected_fp,
            cert_reqs='CERT_NONE',
        )
    except Exception as ex:
        print(f'[lan-hello] urllib3 pool manager failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    # Carry our own listener endpoint so the remote daemon can push
    # back to us later (LAN fan-out). Without this, the owner-side
    # peers.json entry for us has no endpoint and ``_resolve_endpoint``
    # has nothing to feed dulwich. mDNS discovery would fill it on
    # later sessions, but that path isn't reliable on every network
    # (AP isolation, hotspot, etc.) — the QR / hello pair is the
    # baseline.
    # Subnet-matched (0.54.99): advertise OUR address on the same
    # subnet as *host*, not the default-route guess. We are talking to
    # them at *host*, so an address of ours in that /24 is one they can
    # actually dial — the previous guess left multi-homed hosts telling
    # a tethered phone to reach them on wifi.
    our_endpoint = _our_endpoint_for(host)
    # Our half of the share manifest (0.55.50): the projects we grant
    # THIS peer. Sending it here — on the one authenticated round trip
    # both sides already make — is what stops a pair greeting
    # successfully and only discovering a per-project disagreement much
    # later, as a reasonless NotGitRepository from a git route.
    #
    # ``expected_fp`` identifies who we're talking to, but the peer_id is
    # what keyed our grants, so resolve it by fingerprint.
    ours_for_them = []
    try:
        for _p in (_peers.list_peers() or []):
            if (_p.get('fp') or '') == expected_fp:
                ours_for_them = sorted(_p.get('shared_projects') or [])
                break
    except Exception as ex:
        print(f'[lan-hello] building our manifest raised: {ex!r}',
              file=sys.stderr, flush=True)
    body = json.dumps({
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'device_name': device_name,
        'langcode': langcode,
        'endpoint': our_endpoint,
        'shared_with_you': ours_for_them,
    }).encode('utf-8')
    url = f'https://{host}:{int(port)}/v1/lan/hello'
    try:
        resp = pm.request(
            'POST', url, body=body,
            headers={'Content-Type': 'application/json'},
            timeout=urllib3.Timeout(connect=5, read=10),
            retries=False,
        )
    except Exception as ex:
        # FEED THE UNREACHABLE GATE (0.55.83). ``hello_to_peer`` builds
        # its own PoolManager rather than going through
        # ``_https_post_to_peer``, so it never recorded reachability —
        # and the very next step in the same sweep re-paid the full 5 s
        # connect timeout against an address hello had just proven dead.
        # Field 2026-07-28: hello failed at 20:27:09.127, the share-offer
        # to the SAME endpoint started 145 ms later and timed out again,
        # then three dials followed. ~25 s establishing what the first
        # line already knew.
        #
        # Only connect-level failures count. A 4xx, a TLS pin mismatch or
        # a body rejection proves the peer IS reachable; recording those
        # would suppress pushes that would have worked (the same
        # conflation fixed in ``_classify_peek_failure`` in 0.55.60).
        _t = f'{ex!r} {ex}'.lower()
        if any(k in _t for k in ('timed out', 'timeout', 'no route',
                                 'errno 113', 'connection refused',
                                 'errno 111', 'unreachable',
                                 'errno 101')):
            try:
                if peer_id_hint:
                    _record_unreachable(peer_id_hint)
            except Exception:
                pass
        print(f'[lan-hello] POST to {host}:{port} failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    if resp.status != 200:
        print(f'[lan-hello] {host}:{port} returned status '
              f'{resp.status}: {resp.data!r}',
              file=sys.stderr, flush=True)
        return False
    # Did they REFUSE the project we asked for? (0.55.3) The hello
    # carries a langcode when we've just scanned a project-share QR,
    # and the owner grants it only while that QR's offer is still
    # armed. A refusal here is the real cause of a clone that then
    # fails with ``NotGitRepository``, so hand it to the caller
    # through *out* rather than leaving it in the peer's log — the
    # bool return is unchanged, since the hello itself succeeded.
    # Decode unconditionally now (0.55.50): the reply carries THEIR half
    # of the manifest, which must be recorded whether or not the caller
    # passed *out*.
    try:
        import json as _json_mod
        decoded = _json_mod.loads((resp.data or b'').decode(
            'utf-8', 'replace') or '{}')
    except Exception as ex:
        decoded = {}
        print(f'[lan-hello] response decode raised: {ex!r}',
              file=sys.stderr, flush=True)
    if isinstance(decoded, dict):
        theirs = decoded.get('shared_with_you')
        their_pid = str(decoded.get('peer_id', '') or '')
        if isinstance(theirs, list) and their_pid:
            try:
                _theirs = [str(x) for x in theirs if isinstance(x, str)]
                # SAY WHAT THEY ACTUALLY REPORTED (0.55.153). Without this
                # there is no way to tell "they told us nothing" from "we
                # failed to record what they told us" — the exact ambiguity
                # that made peer 80570dd9's empty manifest undiagnosable
                # while it was simultaneously offering us the project.
                print(f'[lan-hello] {their_pid[:8]!r} reports sharing '
                      f'{sorted(_theirs)!r} with us',
                      file=sys.stderr, flush=True)
                _peers.set_their_shared_projects(their_pid, _theirs)
                # Same reciprocal heal as the listener side (0.55.148);
                # both halves of the hello exchange record the manifest,
                # so both must act on it or the fix depends on which
                # device happened to dial first.
                _peers.reciprocate_shares(their_pid, _theirs)
            except Exception as ex:
                print(f'[lan-hello] recording their manifest raised: '
                      f'{ex!r}', file=sys.stderr, flush=True)
        if out is not None:
            refused = str(decoded.get('share_refused', '') or '')
            if refused:
                out['share_refused'] = refused
                out['share_refused_reason'] = str(
                    decoded.get('share_refused_reason', '') or '')
                print(f'[lan-hello] {host}:{port} did NOT share '
                      f'{refused!r} — its QR offer had lapsed',
                      file=sys.stderr, flush=True)
    print(f'[lan-hello] auto-reverse-recorded on {host}:{port}',
          file=sys.stderr, flush=True)
    return True


def _our_endpoint_str():
    """Return our LAN listener's ``host:port`` as a string, or ''
    if the listener isn't bound. Used by signalling payloads
    (pair_request, hello) that need to advertise where the
    remote side can reach us back."""
    from . import lan_listener as _lan_listener
    bound = _lan_listener.bound_endpoint()
    return f'{bound[0]}:{bound[1]}' if bound else ''


def _our_endpoint_for(peer_host):
    """Our listener address **on the same subnet as *peer_host***,
    falling back to the default-route guess (0.54.99).

    We advertise a single ``endpoint`` in signalling payloads, and the
    remote records it as the way to reach us. Guessing our
    default-route address gets that wrong on any multi-homed host: with
    four USB-tether subnets plus wifi, the default route is one of five
    and almost never the one a given phone is on — so the phone stored
    an address it can never reach, and Retry re-dialled that same
    stored value forever (Kent 2026-07-27: *"why can a phone attached
    by USB to a computer not reach the computer, when the computer is
    clearly reaching the phone?"* — it could; it was dialling the
    wrong address for it).

    We know which address they reached us on, or which we reached them
    on, so pick ours from the same /24. That is a plain-old-LAN
    heuristic, not a routing table, but it is exactly right for the
    tether and hotspot cases this exists for, and it degrades to the
    previous behaviour when nothing matches.

    Works against UNMODIFIED peers: it fills the existing single
    ``endpoint`` field, so an old phone records a reachable address
    without any change on its side."""
    from . import lan_listener as _lan_listener
    host = str(peer_host or '').strip()
    fallback = _our_endpoint_str()
    if not host or '.' not in host:
        return fallback
    want = host.rsplit('.', 1)[0] + '.'
    try:
        for cand in _lan_listener.bound_endpoints_all():
            if cand.split(':', 1)[0].startswith(want):
                return cand
    except Exception:
        pass
    return fallback


def _https_post_signalling(host, port, path, payload):
    """Best-effort HTTPS POST to a discovered-but-not-yet-paired
    peer's listener. Used for pair_request / pair_response which
    can't pin the receiver's fp yet (we don't have it until the
    pair is recorded).

    Threat model same as ``hello_to_peer`` /
    ``_handle_share_offer_bodyauth``: identity is body-claimed
    under encrypted-but-unauthenticated transport, with the
    user gesture (Pair tap) as the consent signal. The body
    carries the sender's ed25519 pubkey which IS the peer_id.

    Returns ``(status_code, body_bytes)`` on success,
    ``(0, b'')`` on any failure (logged).
    """
    import json
    import ssl
    try:
        ctx = ssl._create_unverified_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # Load our own cert so the peer can ID us via fp from the
        # body (which references our peer_id = ed25519 pubkey).
        from . import peer_id as _peer_id_mod
        cert_path = _peer_id_mod.cert_path()
        key_path = _peer_id_mod.key_path()
        if cert_path and key_path:
            ctx.load_cert_chain(certfile=cert_path,
                                keyfile=key_path)
    except Exception as ex:
        print(f'[lan-push] signalling ctx build failed: {ex!r}',
              file=sys.stderr, flush=True)
        return 0, b''
    try:
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            cert_reqs='CERT_NONE',
        )
    except Exception as ex:
        print(f'[lan-push] signalling pool manager failed: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return 0, b''
    url = f'https://{host}:{int(port)}{path}'
    try:
        resp = pm.request(
            'POST', url,
            body=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            timeout=urllib3.Timeout(connect=5, read=10),
            retries=False,
        )
    except Exception as ex:
        print(f'[lan-push] signalling POST {url} failed: {ex!r}',
              file=sys.stderr, flush=True)
        return 0, b''
    return resp.status, resp.data


def _https_post_to_peer(peer_id, path, payload, force=False):
    """Generic best-effort HTTPS POST to a paired peer's LAN
    listener. Resolves the peer's endpoint via the standard
    mDNS→static→QR ladder, builds a TLS-pinned PoolManager, and
    submits the payload. Returns ``(status_code, body_bytes)`` on
    success, ``(0, b'')`` on any failure (logged). ``force=True``
    bypasses the recently-unreachable fast-fail gate — for
    user-gesture paths where the operator just changed the world
    (plugged a cable in) and a stale gate reading must not win."""
    import json
    entry = _peers.get_peer(peer_id)
    if entry is None:
        return 0, b''
    expected_fp = entry.get('fp', '')
    if not expected_fp:
        return 0, b''
    # Fast-fail gate (0.50.49): same as ``_push_to_peer``. A
    # signalling POST (share_offer / hello / share_unshared) to a
    # peer that's currently unreachable would otherwise pay the
    # 5s connect timeout. Skip when we've seen them down recently.
    # LAN OFF MEANS NO LAN NETWORK ACTIVITY (0.55.85). 0.55.68 gated the
    # git dials in ``_push_to_peer`` but not the signalling POSTs, so with
    # the toggle off a sweep still spent 5 s per peer on share_offer /
    # hello connect timeouts — on the very link a WAN push was using.
    #
    # Kent 2026-07-29: *"if the user has clicked share LAN off and work
    # offline off, then WAN should be able to flow freely without being
    # impeded by LAN in any way."* Exactly right, and it is the same
    # half-enforced-toggle defect as work_offline (0.55.42) and the LAN
    # dials (0.55.68): honoured where it was cheap, not where the network
    # is touched.
    #
    # ``force`` still wins: that is the user-gesture path (QR pair,
    # cable), where the operator is deliberately reaching out and the
    # toggle they just changed may not be the one being read here.
    if not force:
        try:
            from . import settings as _settings
            if not _settings.lan_allow_sync():
                print(f'[lan-push] {str(peer_id)[:8]!r}: LAN sync is off '
                      f'— not sending {path!r} (the toggle stops '
                      f'signalling POSTs too, not just git dials)',
                      file=sys.stderr, flush=True)
                return 0, b''
        except Exception as ex:
            print(f'[lan-push] lan_allow_sync check raised: {ex!r} — '
                  f'proceeding', file=sys.stderr, flush=True)
    if not force and _recently_unreachable(peer_id):
        return 0, b''
    endpoint = _resolve_endpoint(entry)
    if endpoint is None:
        print(f'[lan-push] no endpoint for {peer_id[:8]!r}; '
              f'skipping POST {path}',
              file=sys.stderr, flush=True)
        return 0, b''
    host, port = endpoint
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-push] context build failed: {ex!r}',
              file=sys.stderr, flush=True)
        return 0, b''
    try:
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=expected_fp,
            cert_reqs='CERT_NONE',
        )
    except Exception as ex:
        print(f'[lan-push] urllib3 pool manager failed: {ex!r}',
              file=sys.stderr, flush=True)
        return 0, b''
    url = f'https://{host}:{int(port)}{path}'
    try:
        resp = pm.request(
            'POST', url,
            body=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            timeout=urllib3.Timeout(connect=5, read=10),
            retries=False,
        )
    except Exception as ex:
        print(f'[lan-push] POST {url} failed: {ex!r}',
              file=sys.stderr, flush=True)
        _record_unreachable(peer_id)
        return 0, b''
    if 200 <= resp.status < 300:
        _record_reachable(peer_id)
        # Proven address → head of the list (0.54.99).
        try:
            _peers.promote_endpoint(peer_id, f'{host}:{int(port)}')
        except Exception:
            pass
    return resp.status, resp.data


def _identity_claim_payload():
    """The standard body-auth identity block for outbound listener
    POSTs (``peer_id`` + ``fp`` + ``device_name``) — the same claim
    shape ``send_share_offer`` ships; receivers cross-check it
    against their ``peers.json`` record. Returns ``None`` when the
    local identity can't be established."""
    try:
        from . import peer_id as _peer_id_mod
        ident = _peer_id_mod.ensure()
    except Exception:
        return None
    from . import store as _store
    return {
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'device_name': _store.get_device_name(),
    }


def fetch_diagnostics_from_peer(peer_id, read_timeout_s=180):
    """Pull a paired peer's diagnostics bundle over the LAN/cable
    link (0.54.74). POSTs our identity claim to the peer's
    ``/v1/lan/diagnostics_pull``; the peer stages its standard
    share bundle (snapshot + per-day daemon logs, one tar.gz —
    the same collection as its local Share-diagnostics button)
    and streams the archive back.

    User-gesture path: skips the recently-unreachable fast-fail
    gate (the operator just plugged the cable in), and uses a
    generous read timeout — a slow field machine builds + gzips
    the bundle inline before the first response byte.

    Returns ``(status_code, archive_name, data_bytes)``;
    ``(0, '', b'')`` on transport failure (logged)."""
    import json
    entry = _peers.get_peer(peer_id)
    if entry is None:
        return 0, '', b''
    expected_fp = entry.get('fp', '')
    if not expected_fp:
        return 0, '', b''
    endpoint = _resolve_endpoint(entry)
    if endpoint is None:
        print(f'[lan-pull] no endpoint for {peer_id[:8]!r}',
              file=sys.stderr, flush=True)
        return 0, '', b''
    host, port = endpoint
    payload = _identity_claim_payload()
    if payload is None:
        return 0, '', b''
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-pull] context build failed: {ex!r}',
              file=sys.stderr, flush=True)
        return 0, '', b''
    try:
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=expected_fp,
            cert_reqs='CERT_NONE',
        )
    except Exception as ex:
        print(f'[lan-pull] urllib3 pool manager failed: {ex!r}',
              file=sys.stderr, flush=True)
        return 0, '', b''
    url = f'https://{host}:{int(port)}/v1/lan/diagnostics_pull'
    try:
        resp = pm.request(
            'POST', url,
            body=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            timeout=urllib3.Timeout(connect=5,
                                    read=float(read_timeout_s)),
            retries=False,
        )
    except Exception as ex:
        print(f'[lan-pull] POST {url} failed: {ex!r}',
              file=sys.stderr, flush=True)
        _record_unreachable(peer_id)
        return 0, '', b''
    if 200 <= resp.status < 300:
        _record_reachable(peer_id)
    try:
        name = resp.headers.get('X-AZT-Archive-Name', '') or ''
    except Exception:
        name = ''
    return resp.status, name, resp.data


def send_restart_request(peer_id):
    """Ask a paired peer's daemon to restart itself (0.54.74) — the
    remote leg of wedge recovery
    (agenda/pull_diagnostics_over_peer_link.md). Cooperative only:
    reaches the wedged-ALIVE class, where the peer's listener
    thread still serves while its scheduler threads are stuck on
    ``project_lock``/network — the common field wedge. A fully
    dead daemon has no listener; the per-platform self-heal covers
    that case (desktop: the owner's client polls auto-respawn;
    Android: the ContentProvider contract). User-gesture path —
    bypasses the fast-fail gate. Returns the HTTPS status code
    (0 on transport failure)."""
    payload = _identity_claim_payload()
    if payload is None:
        return 0
    status, _body = _https_post_to_peer(
        peer_id, '/v1/lan/restart_daemon', payload, force=True)
    return status


def send_share_offer(peer_id, langcode, repo_url='', vernlang=''):
    """Notify a paired peer that we'd like to share *langcode* with
    them. Sent over LAN as a best-effort HTTPS POST. ``vernlang``
    is the project's linguistic code (== ``langcode`` when the two
    weren't separated). The recipient listener short-circuits on
    this path and stashes a pending decision (or no-ops if they
    already have it).

    Returns ``(status, dispatch)`` since 0.50.43:

    - *status* is the HTTPS response code (0 on transport failure).
    - *dispatch* is the receiver's per-state classification:
      ``noop`` (already have project, URL matches),
      ``no_url`` (already have project, sender carried no URL),
      ``stashed_share`` (receiver didn't have project; clone-offer
      stashed as pending decision),
      ``stashed_adopt_origin`` (receiver had project but no
      remote_url; URL adopt prompt stashed),
      ``stashed_conflict`` (URLs differ; conflict prompt stashed).
      ``''`` when the receiver didn't return the field (pre-0.50.43
      daemon) or the call didn't reach the receiver.

    The sender uses ``dispatch`` for user feedback: "Already in
    sync" vs. "Sent (waiting on other phone)" vs. "Sent but URL
    conflict on other phone." Pre-0.50.43 the receiver always
    returned a bare ``{ok: True}`` so legacy callers should treat
    a 2xx with empty dispatch as "delivered, outcome unknown".
    """
    if not _peer_id.cert_path():
        return 0, ''
    try:
        from . import peer_id as _peer_id_mod
        ident = _peer_id_mod.ensure()
    except Exception:
        return 0, ''
    from . import store as _store
    payload = {
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'device_name': _store.get_device_name(),
        'langcode': langcode,
        'repo_url': repo_url,
        'vernlang': vernlang,
    }
    status, body = _https_post_to_peer(
        peer_id, '/v1/lan/share_offer', payload)
    dispatch = ''
    if 200 <= status < 300 and body:
        try:
            import json as _json
            decoded = _json.loads(body.decode('utf-8'))
            if isinstance(decoded, dict):
                dispatch = str(decoded.get('dispatch', '') or '')
        except Exception:
            pass
    return status, dispatch


def send_share_unshared(peer_id, langcode):
    """Symmetric-unshare notification (0.50.44). The local user
    has unshared *langcode* with the paired peer; tell them so
    they can mirror the allowlist removal on their side and stop
    auto-fanout to us for this langcode. Best-effort fire-and-
    forget. Returns True on a 2xx response."""
    if not _peer_id.cert_path():
        return False
    try:
        from . import peer_id as _peer_id_mod
        ident = _peer_id_mod.ensure()
    except Exception:
        return False
    payload = {
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'langcode': langcode,
    }
    status, _ = _https_post_to_peer(
        peer_id, '/v1/lan/share_unshared', payload)
    return 200 <= status < 300


def share_declined(peer_id, langcode):
    """Best-effort nack back to the owner after we declined their
    share-offer. Owner's listener clears their pending side and
    pulls us out of the project's shared_projects allowlist."""
    if not _peer_id.cert_path():
        return False
    try:
        from . import peer_id as _peer_id_mod
        ident = _peer_id_mod.ensure()
    except Exception:
        return False
    payload = {
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'langcode': langcode,
    }
    status, _ = _https_post_to_peer(
        peer_id, '/v1/lan/share_declined', payload)
    return 200 <= status < 300


# Per-peer sweep debounce — see the ``sweep_peer`` docstring.
_sweep_last_start = {}
_sweep_gate_lock = threading.Lock()
_SWEEP_DEBOUNCE_S = 8.0


# Re-ask a peer what they share with us at most this often (0.55.152). One
# hello is a single small TLS round trip, but the skip path it guards runs on
# every sweep, so it needs a floor.
_MANIFEST_REFRESH_MIN_INTERVAL_S = 300.0
_manifest_refresh_at = {}       # peer_id → monotonic time of last hello


def _refresh_their_manifest(peer_id, entry):
    """Say hello so the peer re-sends its ``shared_with_you`` manifest.

    The hello response handler records it (and, since 0.55.148, reciprocates),
    so this function's only job is to make the round trip happen. Returns True
    if the hello was delivered.

    Exists because the manifest is otherwise written only on mDNS arrival: a
    peer that grants us a project while already on the network produces no
    arrival, so nothing re-asks and the stale "they share nothing" stands
    indefinitely."""
    from . import store as _store_mod
    ep = _resolve_endpoint(entry)
    if ep is None:
        print(f'[lan-sweep] {peer_id[:8]!r}: no reachable address to re-ask '
              f'for their share list', file=sys.stderr, flush=True)
        return False
    host, port = ep
    return bool(hello_to_peer(
        host, int(port), entry.get('fp', ''),
        _store_mod.get_device_name(), langcode='', peer_id_hint=peer_id))


def sweep_peer(peer_id, exclude_langcode=''):
    """Push every shared project with *peer_id* where the peer
    isn't already at our HEAD. Used by:

    - mDNS arrival (peer just became reachable — catch them up
      on every stale project, not just whichever we last committed)
    - Fan-out tail (opportunistic multi-project sweep when we're
      already pushing one project, the rest are nearly-free)
    - Daemon listener-bind (we just came up — sweep every paired
      peer with a known endpoint)

    ``exclude_langcode`` lets the fan-out caller skip the project
    it just pushed; ``_push_to_peer``'s pre-flight ls-remote would
    no-op the second push anyway, but the round-trip costs more
    than the skip.

    Returns a dict ``{langcode: bool}`` of per-project outcomes.
    Empty dict if the peer isn't paired or we don't share anything
    with them, or when a sweep for this peer started within the
    debounce window. Per-project failures are isolated and logged.

    Debounce (0.54.3): a daemon restart fires the listener-bind
    sweep AND the first mDNS-arrival sweep within the same second;
    both peeked "peer behind" and both pushed, producing doubled
    ``advanced`` / ``1/1 delivered`` log lines and a wasted
    duplicate pack upload (field log 2026-07-11 17:14). One sweep
    per peer per window is enough — the triggers all mean the same
    thing ("peer just became reachable / we just came up")."""
    from . import projects as _proj
    now = _time_mod.monotonic()
    with _sweep_gate_lock:
        last = _sweep_last_start.get(peer_id, 0.0)
        if now - last < _SWEEP_DEBOUNCE_S:
            print(f'[lan-sweep] {peer_id[:8]!r}: debounced '
                  f'(a sweep started {now - last:.1f}s ago)',
                  file=sys.stderr, flush=True)
            return {}
        _sweep_last_start[peer_id] = now
    entry = _peers.get_peer(peer_id)
    if entry is None:
        return {}
    # Don't re-dial a peer we just found unreachable (0.54.89). This
    # is a BACKGROUND path — unlike the user-gesture paths, which
    # deliberately bypass the gate — and an absent peer costs real
    # foreground time: field 2026-07-27, a stale tether address burned
    # 30 s on ONE project (connect timeout, then the push attempt),
    # and a wifi peer that had left the subnet cost ~19 s across two.
    # Sweeps fire on every mDNS arrival, and a phone flapping between
    # tether and wifi produces one every ~30 s.
    if _recently_unreachable(peer_id):
        print(f'[lan-sweep] {peer_id[:8]!r}: skipped — seen '
              f'unreachable within the last '
              f'{_UNREACHABLE_COOLDOWN_S:.0f}s',
              file=sys.stderr, flush=True)
        return {}
    # Complete an interrupted pairing handshake (0.54.97). Both users
    # consented — someone invited, someone accepted — and only the
    # confirmation failed to arrive, leaving the pairing one-sided: we
    # hold them, they don't hold us, so everything we send gets
    # rejected. Now that they're reachable, say hello; their handler
    # records us and the pairing becomes mutual. Distinct from a
    # revocation (they tombstone us and refuse) and from a
    # never-accepted invite (no hello is ever sent), neither of which
    # this can manufacture.
    if not entry.get('pair_confirmed', False):
        try:
            from . import peer_id as _peer_id_mod
            from . import store as _store_mod
            ident = _peer_id_mod.ensure()
            ep = _resolve_endpoint(entry)
            if ep is not None:
                host, port = ep
                print(f'[lan-sweep] {peer_id[:8]!r}: pairing not '
                      f'confirmed — saying hello to complete it',
                      file=sys.stderr, flush=True)
                # Pass the peer id so a connect-level failure records
                # unreachability (0.55.83). Only the AUTOMATIC sweep does
                # this; the two pair-accept call sites in server.py are
                # user gestures walking a candidate address list, and
                # shouldn't be gating themselves.
                if hello_to_peer(host, int(port), entry.get('fp', ''),
                                 _store_mod.get_device_name(),
                                 langcode='', peer_id_hint=peer_id):
                    _peers.set_pair_confirmed(peer_id, True)
                    entry = _peers.get_peer(peer_id) or entry
                    print(f'[lan-sweep] {peer_id[:8]!r}: pairing '
                          f'confirmed both ways',
                          file=sys.stderr, flush=True)
                else:
                    print(f'[lan-sweep] {peer_id[:8]!r}: hello not '
                          f'delivered; will retry on a later arrival',
                          file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[lan-sweep] {peer_id[:8]!r} pairing heal raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
    # Complete an interrupted SHARE the same way (0.54.98). A share we
    # hold that the peer doesn't is not "one-way sync": their listener
    # won't mount the project, so our pushes 404 and nothing moves in
    # either direction — functionally identical to not sharing, which
    # is why it has to heal rather than sit there looking fine. Safe to
    # re-offer: their handler no-ops when they already have it, and
    # (since the decline suppression above) drops it outright if their
    # user declined, nacking us so we roll the share back.
    try:
        for lang in _peers.unconfirmed_shares(peer_id):
            print(f'[lan-sweep] {peer_id[:8]!r}: share of {lang!r} '
                  f'unconfirmed — re-offering',
                  file=sys.stderr, flush=True)
            try:
                send_share_offer(peer_id, lang)
            except Exception as ex:
                print(f'[lan-sweep] {peer_id[:8]!r} re-offer of '
                      f'{lang!r} raised: {ex!r}',
                      file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[lan-sweep] {peer_id[:8]!r} share heal raised: {ex!r}',
              file=sys.stderr, flush=True)
    shared = entry.get('shared_projects') or []
    # What THEY consented to, learned from the hello manifest (0.55.50).
    # ``None`` = never told (pre-0.55.50 peer, or no hello yet) → behave
    # exactly as before. A list, INCLUDING an empty one, is an answer.
    theirs = _their_shared_projects(peer_id, entry)
    # REFRESH A MANIFEST THAT SAYS "NOTHING" BEFORE ACTING ON IT (0.55.152).
    #
    # ``their_shared_projects`` is only written by the hello exchange, and
    # hello fires on mDNS *arrival*. So a peer who grants us a project while
    # already present produces no arrival transition, no hello, and no
    # refresh — and we go on refusing to dial on a cached empty list for as
    # long as both daemons stay up. Field 2026-07-30: the grant was visibly
    # in place on peer 80570dd9 (its own settings page listed 'nml' as
    # Shared) while this side kept logging ``their grants: []`` and skipping.
    #
    # Only when the cache says they share NOTHING while we share something
    # with them — the one combination that is both actionable and cheap to
    # be wrong about. A stale non-empty list costs a refused peek; a stale
    # empty one costs the entire collaboration.
    if shared and isinstance(theirs, list) and not theirs:
        _now = _time_mod.monotonic()
        _last = _manifest_refresh_at.get(peer_id, 0.0)
        if (_now - _last) >= _MANIFEST_REFRESH_MIN_INTERVAL_S:
            _manifest_refresh_at[peer_id] = _now
            print(f'[lan-sweep] {peer_id[:8]!r}: cached manifest says they '
                  f'share nothing while we share {sorted(shared)!r} — '
                  f'saying hello to re-ask before skipping',
                  file=sys.stderr, flush=True)
            try:
                _refresh_their_manifest(peer_id, entry)
                entry = _peers.get_peer(peer_id) or entry
                theirs = _their_shared_projects(peer_id, entry)
            except Exception as ex:
                print(f'[lan-sweep] {peer_id[:8]!r}: manifest refresh '
                      f'raised: {ex!r} — proceeding on the cached answer',
                      file=sys.stderr, flush=True)
    out = {}
    for langcode in shared:
        if langcode == exclude_langcode:
            continue
        # STOP DIALING for a project the peer hasn't consented to
        # (0.55.50). A grant is mutual consent to collaborate: if they
        # don't share it with us, our pushes are refused and our peeks
        # tell us nothing — and before the manifest existed we had no
        # way to know, so we dialed forever. Field 2026-07-28: the
        # desktop peeked the phone for 'nml' 22 times in 15 minutes
        # while the phone granted it nothing.
        #
        # Kept after 0.55.61 moved the authoritative gate into
        # ``_push_to_peer``: this one skips before the call so the sweep
        # can record the per-project result and log one aggregate line,
        # rather than every sweep emitting a refusal per project.
        if isinstance(theirs, list) and langcode not in theirs:
            print(f'[lan-sweep] {peer_id[:8]!r}: skipping {langcode!r} '
                  f'— ONE-SIDED SHARE. We share it with them; they do '
                  f'not share it with us (their grants: {theirs!r}). '
                  f'Not dialing. Ask them to share {langcode!r}, or '
                  f'stop sharing it with them',
                  file=sys.stderr, flush=True)
            out[langcode] = False
            continue
        # Stop after the FIRST project that proves the peer is
        # unreachable: every remaining project would pay the same
        # connect timeout for the same absent device. ``_push_to_peer``
        # records the gate on its failure paths, so this reads what it
        # just learned.
        if _recently_unreachable(peer_id):
            print(f'[lan-sweep] {peer_id[:8]!r}: aborting remaining '
                  f'project(s) — peer went unreachable mid-sweep',
                  file=sys.stderr, flush=True)
            break
        try:
            project = _proj.get(langcode)
        except Exception:
            project = None
        if project is None:
            continue
        try:
            out[langcode] = _push_to_peer(project, entry)
        except Exception as ex:
            print(f'[lan-sweep] {peer_id[:8]!r} {langcode!r} '
                  f'raised: {ex!r}', file=sys.stderr, flush=True)
            out[langcode] = False
    if out:
        ok_count = sum(1 for v in out.values() if v)
        print(f'[lan-sweep] {peer_id[:8]!r}: '
              f'{ok_count}/{len(out)} delivered '
              f'(excluded={exclude_langcode!r})',
              file=sys.stderr, flush=True)
    return out


def fan_out(project):
    """Push ``project`` to every paired peer that has its langcode
    in ``shared_projects`` and an in-memory or static endpoint.

    Returns a dict ``{peer_id: bool}`` of per-target outcomes —
    callers may log the summary, but the daemon's scheduler treats
    LAN delivery as opportunistic and does not clear pending_push
    based on it.

    Since 0.50.45: after pushing *project* to each candidate, fires
    ``sweep_peer`` for that peer (excluding *project*) so any OTHER
    shared projects the peer is behind on catch up in the same
    radio window. "We're already talking to B; tell them about Y
    too." Past-work-not-being-committed cases catch up naturally.

    Safe to call from any thread; per-peer failures are isolated."""
    all_peers = _peers.list_peers()
    candidates = [e for e in all_peers
                  if e.get('peer_id')
                  and project.langcode in (
                      e.get('shared_projects') or [])]
    # Always log the gate decision so it's observable whether the
    # fan-out actually fired. Empty paired list AND no eligible
    # candidates are both common starting states; silent skip used
    # to make it impossible to tell "fan-out ran but no targets"
    # apart from "fan-out never fired."
    print(f'[lan-fanout] {project.langcode!r}: '
          f'paired={len(all_peers)} '
          f'sharing_this={len(candidates)}',
          file=sys.stderr, flush=True)
    # 0.46.7 diagnostic surface (renamed in 0.47.0): fire
    # ``_wan_unshared`` once per drain so the ``[wan-unshared]``
    # trace is visible regardless of whether a peer app is
    # foregrounded and polling status. Picker / recorder normally
    # drive ``_h_project_status`` (which calls the three walker
    # helpers), but on devices where the server APK is the only
    # thing open (e.g., right after a Restart server tap), no peer
    # polls, so the diagnostic never fired. Rate-limit (output-
    # change-only) still applies — steady-state drains emit nothing.
    try:
        from dulwich.repo import Repo
        from . import repo as _repo_mod
        try:
            _diag_repo = Repo(project.working_dir)
        except Exception:
            _diag_repo = None
        if _diag_repo is not None:
            # Use the project's actual HEAD branch — not a hardcoded
            # 'main'. A project that ended up on ``refs/heads/master``
            # (LAN clone from a peer whose source git config defaulted
            # to master, or any user-renamed branch) was emitting a
            # ``[count-ahead]`` line for the orphan ``refs/heads/main``
            # ref while ``_h_project_status`` reported the master
            # walk — two unrelated numbers in the log per drain tick
            # for the same project. Pre-fix value 'main' was the
            # assumption from the 0.46.7 diagnostic patch; revisit if
            # LAN-cloned projects ever standardize on a single branch.
            try:
                head_ref = _diag_repo.refs.read_ref(b'HEAD')
                if head_ref and head_ref.startswith(b'refs/heads/'):
                    branch = head_ref[len(b'refs/heads/'):].decode(
                        'utf-8', 'replace')
                else:
                    branch = 'main'
            except Exception:
                branch = 'main'
            try:
                _repo_mod._wan_unshared(_diag_repo, branch)
            except Exception:
                pass
            try:
                _diag_repo.close()
            except Exception:
                pass
    except Exception:
        pass
    out = {}
    for entry in candidates:
        peer_id = entry['peer_id']
        out[peer_id] = _push_to_peer(project, entry)
        # Opportunistic multi-project sweep (0.50.45). The radio
        # is up, the TLS handshake is warm, and we already
        # resolved this peer's endpoint — push any other shared
        # projects they're behind on while we're here.
        try:
            sweep_peer(peer_id, exclude_langcode=project.langcode)
        except Exception as ex:
            print(f'[lan-fanout] sweep_peer {peer_id[:8]!r} '
                  f'raised: {ex!r}', file=sys.stderr, flush=True)
    return out
