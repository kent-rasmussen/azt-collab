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
import sys
import tempfile

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


def _resolve_endpoint(peer_entry):
    """Endpoint resolution order per the spec: mDNS-cached → static
    endpoints → QR-hint endpoint. Returns ``(host, port)`` or
    ``None``."""
    pid = peer_entry.get('peer_id', '')
    mdns = _lan_discovery.get_endpoint(pid) if pid else None
    if mdns is not None:
        return mdns
    for source in ('static_endpoints', 'endpoints'):
        for raw in (peer_entry.get(source) or []):
            try:
                host, port = raw.rsplit(':', 1)
                return (host, int(port))
            except (ValueError, TypeError):
                continue
    return None


def _build_ssl_context(expected_fp):
    """Build a client-side SSL context that authenticates the peer's
    cert by *fingerprint* rather than CA chain.

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


def _push_to_peer(project, peer_entry):
    """Single push attempt against one paired peer. Returns
    ``True`` on success, ``False`` on any failure. Logs detail
    rather than raising."""
    from dulwich import porcelain
    pid = peer_entry.get('peer_id', '')
    expected_fp = peer_entry.get('fp', '')
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
    try:
        ctx = _build_ssl_context(expected_fp)
    except Exception as ex:
        print(f'[lan-push] context build failed for {pid[:8]!r}: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return False
    # dulwich's HttpGitClient uses urllib3 underneath; constructing
    # a urllib3 PoolManager with our custom SSL context is the
    # documented seam for client-side TLS knobs. Fingerprint
    # verification happens through urllib3's assert_fingerprint
    # (formatted as "sha256:HEXSTRING") — that catches a
    # cert mismatch at handshake completion.
    try:
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=expected_fp,
            # urllib3 clobbers ``ctx.verify_mode`` with
            # ``resolve_cert_reqs(cert_reqs)`` inside
            # ``_ssl_wrap_socket_and_match_hostname``. With
            # ``cert_reqs=None`` (default) that resolves to
            # CERT_REQUIRED, undoing our ``ctx.verify_mode=CERT_NONE``
            # and producing ``CERTIFICATE_VERIFY_FAILED: self signed
            # certificate`` even though our context was built
            # unverified. Passing ``'CERT_NONE'`` here makes urllib3
            # set our context's verify_mode to CERT_NONE too — same
            # value we already had, just survives the override.
            cert_reqs='CERT_NONE',
        )
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
    pre_peer_head = _peek_peer_main(url, pm, pid)
    # Honest per-peer observation: any time ls-remote returns a
    # peer's main SHA we record it. Drives the honest
    # ``lan_unshared`` and ``at_risk`` counts (since 0.47.0; was
    # the conflated ``unshared_commits`` pre-0.47) — see
    # ``repo._lan_unshared`` and ``peers.peer_main_shas_for``.
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
        print(f'[lan-push] {pid[:8]!r} already at '
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
        _consec_failures.pop(pid, None)  # success: reset counter
        _record_reachable(pid)  # clear fast-fail gate
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
            print(f'[lan-push] {pid[:8]!r}: peer at '
                  f'{pre_peer_head[:12]!r} is NOT ancestor of '
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
        if ('Connection refused' in msg
                or 'Errno 111' in msg
                or 'NewConnectionError' in msg):
            _lan_discovery.invalidate_endpoint(pid)
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
            else:
                cause = 'unspecified connection failure'
            print(f'[lan-push] {pid[:8]!r} at {host}:{port} '
                  f'refused / unreachable: {cause} — invalidated '
                  f'mDNS cache for re-resolve',
                  file=sys.stderr, flush=True)
            # Fast-fail gate (0.50.49): record the observation so
            # subsequent push / sweep / signalling calls within
            # the cooldown skip without re-paying the retry storm.
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
            post_peer_head = _peek_peer_main(url, pm, pid)
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
        import urllib3
        pm = urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=expected_fp,
            cert_reqs='CERT_NONE',
        )
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
        if isinstance(main, bytes):
            return main.decode('ascii')
        return main
    except Exception as ex:
        print(f'[lan-push] ls-remote peek failed for {pid[:8]!r}: '
              f'{ex!r}', file=sys.stderr, flush=True)
        return None


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

    Runs entirely under ``project_lock``: fetch writes packs to
    the local object store, ``_merge_diverged`` mutates the
    working tree + index + HEAD, and the post-merge push reads
    the freshly-committed merge SHA. Without the lock, any of
    these can interleave with a concurrent ``commit_project``,
    ``atomic_finalize``, or post-receive reset — same hazards
    the github sync path locks against (see
    ``_sync_repo_locked`` in repo.py). LAN delivery is
    opportunistic; a 5 s timeout means we skip this round if the
    project is busy and the next drain pass retries.
    """
    try:
        with project_lock(project.working_dir, timeout=5.0):
            return _merge_then_push_locked(
                project, url, pm, pid, host, port)
    except LockTimeout:
        print(f'[lan-merge] {pid[:8]!r}: project busy — deferring '
              f'merge; next drain pass will retry',
              file=sys.stderr, flush=True)
        return False


def _merge_then_push_locked(project, url, pm, pid, host, port):
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

        # The bundled dulwich's ``porcelain.fetch`` doesn't accept
        # ``pool_manager=`` even though ``porcelain.push`` does.
        # Go one level lower: build an ``HttpGitClient`` directly
        # (its ``__init__`` accepts ``pool_manager`` across every
        # version of dulwich that's shipped a ``HttpGitClient``)
        # and call ``client.fetch(path, repo)`` to populate the
        # local object store with the peer's commits.
        try:
            from dulwich.client import HttpGitClient
            from urllib.parse import urlparse
            parsed = urlparse(url)
            base = f'{parsed.scheme}://{parsed.netloc}'
            path = parsed.path or '/'
            client = HttpGitClient(base, pool_manager=pm)
            fetch_result = client.fetch(path, repo)
        except Exception as ex:
            print(f'[lan-merge] fetch from {pid[:8]!r} failed: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            return False

        # Resolve peer's canonical current commit. dulwich
        # returns refs via the FetchPackResult's ``refs`` attr.
        # Prefer ``HEAD`` over ``refs/heads/main`` for the same
        # reason ``_peek_peer_main`` does (since 0.46.4): a peer
        # whose main was force-overwritten still has its real
        # state at HEAD.
        peer_refs = getattr(fetch_result, 'refs', None) or {}
        peer_head = peer_refs.get(b'HEAD') \
            or peer_refs.get(b'refs/heads/main')
        if peer_head is None:
            print(f'[lan-merge] {pid[:8]!r}: no main / HEAD ref in '
                  f'fetch result; refs={list(peer_refs.keys())!r}',
                  file=sys.stderr, flush=True)
            return False
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
                  langcode=''):
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
    from . import lan_listener as _lan_listener
    bound = _lan_listener.bound_endpoint()
    our_endpoint = f'{bound[0]}:{bound[1]}' if bound else ''
    body = json.dumps({
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'device_name': device_name,
        'langcode': langcode,
        'endpoint': our_endpoint,
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
        print(f'[lan-hello] POST to {host}:{port} failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False
    if resp.status != 200:
        print(f'[lan-hello] {host}:{port} returned status '
              f'{resp.status}: {resp.data!r}',
              file=sys.stderr, flush=True)
        return False
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


def _https_post_to_peer(peer_id, path, payload):
    """Generic best-effort HTTPS POST to a paired peer's LAN
    listener. Resolves the peer's endpoint via the standard
    mDNS→static→QR ladder, builds a TLS-pinned PoolManager, and
    submits the payload. Returns ``(status_code, body_bytes)`` on
    success, ``(0, b'')`` on any failure (logged)."""
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
    if _recently_unreachable(peer_id):
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
    return resp.status, resp.data


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
    with them. Per-project failures are isolated and logged."""
    from . import projects as _proj
    entry = _peers.get_peer(peer_id)
    if entry is None:
        return {}
    shared = entry.get('shared_projects') or []
    out = {}
    for langcode in shared:
        if langcode == exclude_langcode:
            continue
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
