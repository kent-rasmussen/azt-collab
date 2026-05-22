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
    if pre_peer_head is None:
        # Couldn't ls-remote; proceed with the push attempt anyway.
        # The log below will say "in-sync? unknown" — we still get
        # a clear error if the push fails.
        pass
    elif local_head and pre_peer_head == local_head:
        print(f'[lan-push] {pid[:8]!r} already at '
              f'{local_head[:12]!r} — no-op',
              file=sys.stderr, flush=True)
        # Even the no-op confirms the peer has this SHA — refresh
        # the "shared somewhere" record so the LANOK indicator
        # reflects current reality if the field was previously
        # missing or stale.
        try:
            from . import projects as _projects
            _projects.set_last_lan_pushed_sha(
                project.langcode, local_head)
        except Exception:
            pass
        return True

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
            print(f'[lan-push] {pid[:8]!r} at {host}:{port} '
                  f'refused / unreachable — invalidated mDNS cache '
                  f'for re-resolve',
                  file=sys.stderr, flush=True)
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
    # Post-flight: did we actually advance the peer? Compare what
    # we just pushed (local_head) against what the peer had before.
    # ``in-sync`` when pre_peer_head was already equal (we already
    # short-circuit above for that, so this only fires when the
    # ls-remote pre-check itself was unreachable). ``advanced``
    # gives the user a clear before/after they can correlate with
    # their commit history.
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
    # the "shared somewhere" count (LANOK indicator). We update on
    # every successful push, not just the "advanced" case, because
    # a no-op confirms the peer has at least this SHA.
    if local_head:
        try:
            from . import projects as _projects
            _projects.set_last_lan_pushed_sha(
                project.langcode, local_head)
        except Exception as ex:
            print(f'[lan-push] set_last_lan_pushed_sha raised: '
                  f'{ex!r}', file=sys.stderr, flush=True)
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


def _peek_peer_main(url, pm, pid):
    """ls-remote the peer's listener for ``refs/heads/main``.
    Returns the SHA (hex string) or ``None`` if we couldn't
    reach the peer / parse the response. Cheap — protocol
    round-trip only, no packfile transfer. Used by
    ``_push_to_peer`` to decide whether to actually push or
    short-circuit as a no-op."""
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
        main = refs.get(b'refs/heads/main') or refs.get(b'HEAD')
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
    has both parents (our HEAD + peer HEAD), bot author."""
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

        # Resolve peer's main-branch tip. dulwich returns refs
        # via the FetchPackResult's ``refs`` attr.
        peer_refs = getattr(fetch_result, 'refs', None) or {}
        peer_head = peer_refs.get(b'refs/heads/main') \
            or peer_refs.get(b'HEAD')
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
        return 0, b''
    return resp.status, resp.data


def send_share_offer(peer_id, langcode, repo_url='', vernlang=''):
    """Notify a paired peer that we'd like to share *langcode* with
    them. Sent over LAN as a best-effort HTTPS POST. ``vernlang``
    is the project's linguistic code (== ``langcode`` when the two
    weren't separated). The recipient listener short-circuits on
    this path and stashes a pending decision. Returns True on a
    2xx response."""
    if not _peer_id.cert_path():
        return False
    try:
        from . import peer_id as _peer_id_mod
        ident = _peer_id_mod.ensure()
    except Exception:
        return False
    from . import store as _store
    payload = {
        'peer_id': ident['peer_id'],
        'fp': ident['fp'],
        'device_name': _store.get_device_name(),
        'langcode': langcode,
        'repo_url': repo_url,
        'vernlang': vernlang,
    }
    status, _ = _https_post_to_peer(
        peer_id, '/v1/lan/share_offer', payload)
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


def fan_out(project):
    """Push ``project`` to every paired peer that has its langcode
    in ``shared_projects`` and an in-memory or static endpoint.

    Returns a dict ``{peer_id: bool}`` of per-target outcomes —
    callers may log the summary, but the daemon's scheduler treats
    LAN delivery as opportunistic and does not clear pending_push
    based on it.

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
    out = {}
    for entry in candidates:
        out[entry['peer_id']] = _push_to_peer(project, entry)
    return out
