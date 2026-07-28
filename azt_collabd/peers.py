"""
Paired-peers registry for the LAN sync transport (parked design in
``docs/local_lan_sync_stub.md``, phase 1).

Persists ``$AZT_HOME/peers.json``:

  {
    "peers": {
      "<peer_id_hex>": {
        "device_name": "...",
        "fp": "<their sha256>",
        "endpoints": ["192.168.1.42:8443"],
        "static_endpoints": [],
        "shared_projects": ["fra", "tpi"],
        "paired_at": "2026-05-19T14:30:00Z",
        "last_seen_at": "2026-05-19T16:45:12Z"
      }
    }
  }

Daemon-owned, written via sibling-tempfile + ``os.replace`` so a
crash during write can't leave the file half-flushed (one of the
load-bearing obligations in ``azt_collab_client/CLAUDE.md`` §
"Daemon obligations").

``endpoints`` is a session-volatile mirror used by the scheduler's
fan-out path; ``static_endpoints`` is the user-managed durable list
for the hotspot-host-fixed-IP fallback (phase 7). Discovery
(mDNS) does **not** persist into either — it's a per-process
in-memory cache.
"""

import json
import os
import sys
import tempfile
import threading
import time

from . import paths as _paths


_LOCK = threading.Lock()


def _peers_path():
    return os.path.join(_paths.azt_home(), 'peers.json')


def _atomic_write(target_path, data):
    target_dir = os.path.dirname(target_path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.peers.', suffix='.tmp',
                               dir=target_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, target_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _load_raw(strict=False):
    path = _peers_path()
    try:
        with open(path, 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
    except FileNotFoundError:
        return {'peers': {}}
    except (OSError, ValueError) as ex:
        print(f'[peers] failed to load {path!r}: {ex!r}',
              file=sys.stderr, flush=True)
        # ``strict`` callers (the LAN listener's allowlist gate)
        # must distinguish "no peers paired" from "the registry is
        # unreadable right now": collapsing a transient read failure
        # (fd exhaustion, EIO) into an empty allowlist silently
        # unshares every project (field incident 2026-07-10).
        # Non-strict callers keep the old degrade-to-empty.
        if strict:
            raise
        return {'peers': {}}
    if not isinstance(data, dict) or not isinstance(
            data.get('peers'), dict):
        return {'peers': {}}
    return data


def _save_raw(data):
    payload = json.dumps(data, indent=2, sort_keys=True).encode('utf-8')
    _atomic_write(_peers_path(), payload)


def _normalize_entry(entry):
    """Coerce a raw peers.json entry to the canonical shape used by
    callers. Tolerant of missing keys (a hand-edited or older file
    shouldn't crash the daemon)."""
    if not isinstance(entry, dict):
        entry = {}
    raw_lsm = entry.get('last_seen_main') or {}
    last_seen_main = {}
    if isinstance(raw_lsm, dict):
        for k, v in raw_lsm.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                last_seen_main[k] = v
    raw_lcl = entry.get('last_covered_local') or {}
    last_covered_local = {}
    if isinstance(raw_lcl, dict):
        for k, v in raw_lcl.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                last_covered_local[k] = v
    # Why the last reach ATTEMPT didn't become an observation (0.55.20).
    # Kept strictly separate from ``project_seen_at``: an attempt is not
    # an observation, and must never advance the board's as-of stamp.
    raw_probe = entry.get('probe') or {}
    probe = {}
    if isinstance(raw_probe, dict):
        for k, v in raw_probe.items():
            if isinstance(k, str) and isinstance(v, dict):
                probe[k] = {
                    'outcome': str(v.get('outcome', '') or ''),
                    'at': str(v.get('at', '') or ''),
                    'detail': str(v.get('detail', '') or '')[:200],
                }
    raw_psa = entry.get('project_seen_at') or {}
    project_seen_at = {}
    if isinstance(raw_psa, dict):
        for k, v in raw_psa.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                project_seen_at[k] = v
    return {
        'device_name': str(entry.get('device_name', '') or ''),
        'fp': str(entry.get('fp', '') or ''),
        'endpoints': [str(e) for e in (entry.get('endpoints') or [])
                      if isinstance(e, str)],
        'static_endpoints': [
            str(e) for e in (entry.get('static_endpoints') or [])
            if isinstance(e, str)],
        'shared_projects': [
            str(s) for s in (entry.get('shared_projects') or [])
            if isinstance(s, str)],
        'paired_at': str(entry.get('paired_at', '') or ''),
        'last_seen_at': str(entry.get('last_seen_at', '') or ''),
        # Do we have EVIDENCE the other side also holds this pairing?
        # (0.54.97) Set when our hello reached them (they recorded us)
        # or when their hello reached us (they clearly have us).
        # ``False`` means the pairing may be one-sided — both users
        # consented, but the confirmation never got delivered — which
        # is the state the arrival-time heal exists to close. Missing
        # key reads as False so pairings made before this shipped get
        # one heal attempt on their next arrival; the cost is a single
        # hello per peer per arrival until it lands.
        'pair_confirmed': bool(entry.get('pair_confirmed', False)),
        # Langcodes this peer's user DECLINED from us (0.54.98).
        # ``_handle_share_offer`` re-stashed an offer on every inbound
        # POST, so a decline was undone by the sender's next burst
        # whenever we couldn't reach them to nack — the offer recurred
        # forever. Checked before stashing; cleared by a later accept
        # or by us offering that project to them ourselves.
        'declined_shares': [
            str(s) for s in (entry.get('declined_shares') or [])
            if isinstance(s, str)],
        # Langcodes we have EVIDENCE this peer also shares with us
        # (0.54.98) — either a delivery to them succeeded (their side
        # mounts it) or they offered it to us (it's in their
        # allowlist). ``shared_projects`` minus this is the set that
        # may be one-sided, which the arrival heal re-offers. A
        # one-sided share is not "one-way sync": the side without the
        # entry doesn't MOUNT the project, so the other side's pushes
        # 404 and nothing moves either way.
        'shares_confirmed': [
            str(s) for s in (entry.get('shares_confirmed') or [])
            if isinstance(s, str)],
        # Per-project SHA of this peer's main as last observed via
        # ls-remote / verified push. Keyed by langcode. Drives the
        # honest ``lan_unshared`` / ``at_risk`` computation (was
        # the conflated ``unshared_commits`` in 0.46.x) — walks
        # HEAD excluding the union of all paired peers' observed-
        # current-main SHAs. ``lan_unshared=0`` only when at least
        # one paired peer is actually at our HEAD or descended from
        # it. Replaces the project-wide ``last_lan_pushed_sha``
        # field that recorded what *we* shipped rather than what
        # the peer *has*, producing false-positive LANOK on
        # diverged histories.
        'last_seen_main': last_seen_main,
        # Per-project SHA of OUR OWN commit last CONFIRMED contained
        # in this peer's state (verified push, no-op "already at",
        # peer-contains-local ancestry check, post-receive peek).
        # Unlike ``last_seen_main`` — which may be a peer-side
        # commit we never fetched — this is always a commit WE
        # hold, so the sync-status walkers can fall back to it when
        # the peer's head isn't in our object store. Without the
        # fallback, an unknown peer head made the walkers return 0
        # (OK-on-uncertainty) and the indicator claimed "all
        # shared" over pending local commits (field catch
        # 2026-07-11). Keyed by langcode. Since 0.54.5.
        'last_covered_local': last_covered_local,
        # Per-project ISO time of the last observation recorded for
        # that project (either setter above) — the honest "(date)"
        # for the sync board's per-row statuses: 'N to send' is
        # computed against coverage observed THEN, which can be
        # older than the peer-level ``last_seen_at`` (bumped by any
        # project's contact). Since 0.54.70.
        'project_seen_at': project_seen_at,
        # {langcode: {outcome, at, detail}} — outcome is one of
        # 'ok' | 'timeout' | 'no_route' | 'refused' | 'not_served' |
        # 'error'. Lets the board separate "we could not reach them"
        # (connectivity) from "we reached them and they would not serve
        # this project" (reciprocation) — indistinguishable before
        # 0.55.20, which is the ambiguity Kent could not diagnose
        # around.
        'probe': probe,
    }


def list_peers(strict=False):
    """Return a list of ``{peer_id, device_name, fp, endpoints,
    static_endpoints, shared_projects, paired_at, last_seen_at}``
    dicts. Empty list if no peers / file missing. With
    ``strict=True`` a transient read failure RAISES (OSError /
    ValueError) instead of returning empty — see ``_load_raw``."""
    with _LOCK:
        data = _load_raw(strict=strict)
    out = []
    for peer_id, entry in (data.get('peers') or {}).items():
        norm = _normalize_entry(entry)
        norm['peer_id'] = str(peer_id)
        out.append(norm)
    return out


def get_peer(peer_id):
    """Return the canonical-shape entry for *peer_id*, or ``None``
    if not paired."""
    with _LOCK:
        data = _load_raw()
    entry = (data.get('peers') or {}).get(peer_id)
    if entry is None:
        return None
    norm = _normalize_entry(entry)
    norm['peer_id'] = str(peer_id)
    return norm


def _invalidate_sync_board():
    """Tell the peer-sync board (repo.py) its cached rows are stale
    because a peer mutation changed them. Lazy import breaks the
    repo→peers module cycle; best-effort (the board has a staleness
    backstop anyway)."""
    try:
        from . import repo as _repo
        _repo.invalidate_peer_sync()
    except Exception:
        pass


def set_pair_confirmed(peer_id, confirmed=True):
    """Record whether we have evidence the OTHER side holds this
    pairing (0.54.97). Called when our hello reached them, or when
    their hello reached us. No-op for an unknown peer."""
    with _LOCK:
        data = _load_raw()
        peers = data.get('peers') or {}
        entry = peers.get(str(peer_id))
        if entry is None:
            return False
        if bool(entry.get('pair_confirmed', False)) == bool(confirmed):
            return True
        entry['pair_confirmed'] = bool(confirmed)
        peers[str(peer_id)] = entry
        data['peers'] = peers
        _save_raw(data)
    _invalidate_sync_board()
    return True


def add_unpair_tombstone(peer_id):
    """Remember that the USER deliberately unpaired *peer_id*
    (0.54.97).

    Load-bearing for the arrival-time pairing heal: a peer that still
    holds a stale record of us will keep saying hello, and the hello
    handler records pairings for any caller (body-claimed identity —
    see ``lan_listener._build_server``). Without a tombstone, an
    auto-heal would silently resurrect a pairing the user had
    revoked, which is a completely different situation from a
    confirmation that failed to arrive (Kent 2026-07-27: *"Not the
    same as if one side unpaired, or didn't accept"*). Cleared by a
    fresh local pairing gesture."""
    with _LOCK:
        data = _load_raw()
        tombs = data.get('unpaired') or {}
        tombs[str(peer_id)] = _now_iso()
        data['unpaired'] = tombs
        _save_raw(data)
    return True


def is_unpair_tombstoned(peer_id):
    """True when the user unpaired this peer and hasn't re-paired."""
    with _LOCK:
        data = _load_raw()
        tombs = data.get('unpaired') or {}
        return str(peer_id) in tombs


def clear_unpair_tombstone(peer_id):
    """Drop the tombstone — a fresh, user-gestured pairing supersedes
    the earlier revocation."""
    with _LOCK:
        data = _load_raw()
        tombs = data.get('unpaired') or {}
        if str(peer_id) not in tombs:
            return False
        tombs.pop(str(peer_id), None)
        data['unpaired'] = tombs
        _save_raw(data)
    return True


def record_pair(peer_id, fp, device_name, endpoint='', endpoints=None):
    """Insert or update a peer entry on pair-accept. Preserves
    existing ``shared_projects`` and ``static_endpoints`` if the
    peer is already known (re-pair just refreshes the cert
    fingerprint and the QR-captured endpoint(s)). Returns the
    canonical entry.

    ``endpoints`` (0.54.35): the full candidate ``ip:port`` list from
    the QR — every interface the peer advertised (wifi, USB-tether
    usb0, hotspot). The dialer tries each, so a peer reachable over any
    link works regardless of which one the peer's default route is on.
    Falls back to the single ``endpoint`` (older QRs), then to the
    existing list."""
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        existing = _normalize_entry(peers.get(peer_id, {}))
        if endpoints:
            eps = []
            for e in endpoints:
                e = str(e or '')
                if e and e not in eps:
                    eps.append(e)
        elif endpoint:
            eps = [endpoint]
        else:
            eps = existing['endpoints'] or []
        entry = {
            'device_name': str(device_name or ''),
            'fp': str(fp or ''),
            'endpoints': eps,
            'static_endpoints': existing['static_endpoints'],
            'shared_projects': existing['shared_projects'],
            'paired_at': existing['paired_at'] or _now_iso(),
            'last_seen_at': _now_iso(),
            # Preserved across a re-pair; the caller sets it once it
            # knows whether the other side was told (0.54.97).
            'pair_confirmed': bool(existing.get('pair_confirmed',
                                                False)),
        }
        peers[str(peer_id)] = entry
        data['peers'] = peers
        # A pairing recorded now supersedes any earlier revocation —
        # this call only happens behind a user gesture (QR scan,
        # accept, or an inbound hello the handler already vetted).
        tombs = data.get('unpaired') or {}
        if str(peer_id) in tombs:
            tombs.pop(str(peer_id), None)
            data['unpaired'] = tombs
        _save_raw(data)
    _invalidate_sync_board()  # a new/updated pairing changes the board
    out = dict(entry)
    out['peer_id'] = str(peer_id)
    return out


def remove_peer(peer_id):
    """Forget a peer. Returns True if the peer existed, False
    otherwise."""
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return False
        del peers[peer_id]
        data['peers'] = peers
        _save_raw(data)
    return True


def set_shared_projects(peer_id, langcodes):
    """Replace this peer's outbound project allowlist. Returns the
    updated entry, or ``None`` if the peer isn't paired."""
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return None
        entry = _normalize_entry(peers[peer_id])
        entry['shared_projects'] = sorted({
            str(l) for l in (langcodes or []) if l})
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
    out = dict(entry)
    out['peer_id'] = str(peer_id)
    return out


def _set_entry_list(peer_id, key, langcode, present):
    """Add/remove *langcode* in a per-peer list field. Returns True
    when the file changed."""
    with _LOCK:
        data = _load_raw()
        peers = data.get('peers') or {}
        entry = peers.get(str(peer_id))
        if entry is None:
            return False
        current = [str(s) for s in (entry.get(key) or [])
                   if isinstance(s, str)]
        want = str(langcode)
        if present and want in current:
            return True
        if not present and want not in current:
            return True
        if present:
            current.append(want)
        else:
            current = [s for s in current if s != want]
        entry[key] = sorted(set(current))
        peers[str(peer_id)] = entry
        data['peers'] = peers
        _save_raw(data)
    _invalidate_sync_board()
    return True


def add_declined_share(peer_id, langcode):
    """Remember that this peer's user DECLINED *langcode* from us
    (0.54.98) so a re-arriving offer can be dropped instead of
    re-stashed. Without it, a decline only stuck when we could reach
    the sender to nack — the exact case that fails."""
    return _set_entry_list(peer_id, 'declined_shares', langcode, True)


def is_declined_share(peer_id, langcode):
    entry = get_peer(peer_id)
    if entry is None:
        return False
    return str(langcode) in (entry.get('declined_shares') or [])


def clear_declined_share(peer_id, langcode):
    """Drop the suppression — a later accept, or us offering that
    project to them, supersedes the earlier decline."""
    return _set_entry_list(peer_id, 'declined_shares', langcode, False)


def set_share_confirmed(peer_id, langcode, confirmed=True):
    """Record whether we have evidence this peer ALSO shares
    *langcode* with us (0.54.98): a successful delivery to them, or
    an offer of it from them."""
    return _set_entry_list(peer_id, 'shares_confirmed', langcode,
                           bool(confirmed))


def unconfirmed_shares(peer_id):
    """Langcodes we share with this peer but have no evidence they
    share back — the set the arrival heal re-offers."""
    entry = get_peer(peer_id)
    if entry is None:
        return []
    confirmed = set(entry.get('shares_confirmed') or [])
    return [s for s in (entry.get('shared_projects') or [])
            if s not in confirmed]


def add_shared_project(peer_id, langcode):
    """Convenience for the per-project share gesture (phase 3).
    Returns the updated entry or ``None``."""
    entry = get_peer(peer_id)
    if entry is None:
        return None
    shared = set(entry['shared_projects'])
    shared.add(str(langcode))
    return set_shared_projects(peer_id, sorted(shared))


def remove_shared_project(peer_id, langcode):
    """Symmetric counterpart to ``add_shared_project``. Returns the
    updated entry or ``None``."""
    entry = get_peer(peer_id)
    if entry is None:
        return None
    shared = set(entry['shared_projects'])
    shared.discard(str(langcode))
    return set_shared_projects(peer_id, sorted(shared))


def set_static_endpoints(peer_id, endpoints):
    """Replace this peer's static-endpoint fallback list (phase 7).
    Returns the updated entry or ``None``."""
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return None
        entry = _normalize_entry(peers[peer_id])
        entry['static_endpoints'] = [
            str(e) for e in (endpoints or []) if e]
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
    out = dict(entry)
    out['peer_id'] = str(peer_id)
    return out


def promote_endpoint(peer_id, endpoint, max_endpoints=8):
    """Move *endpoint* (``'host:port'``) to the HEAD of this peer's
    ``endpoints`` list, inserting it if new (0.54.99).

    The mirror of ``demote_static_endpoint``: that one exists because a
    dead address must stop being re-dialed, and this one exists because
    a PROVEN address should be tried first. Resolution reads the lists
    head-first, so ordering the list by recency of evidence is the
    whole mechanism — no guessing which of a peer's addresses is
    "the real one" (Kent 2026-07-27: *"prioritize an address by putting
    it first … always trying first the last one we heard from"*).

    Two things count as evidence, and they're the same event seen from
    the two ends:

    - we RECEIVED a request from that address (its source host is
      proven reachable in the direction that matters);
    - we SENT to it successfully.

    mDNS stays authoritative and is consulted before these lists —
    this is what to do when mDNS has nothing, which on a tethered
    Android host is most of the time.

    Capped at *max_endpoints* so a device that roams between many
    networks doesn't accumulate an unbounded tail of dead addresses;
    the oldest (least recently proven) fall off the end."""
    ep = str(endpoint or '').strip()
    if not ep or ':' not in ep:
        return False
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if str(peer_id) not in peers:
            return False
        entry = _normalize_entry(peers[str(peer_id)])
        current = [str(e) for e in (entry.get('endpoints') or []) if e]
        if current and current[0] == ep:
            return True          # already first; nothing to write
        reordered = [ep] + [e for e in current if e != ep]
        entry['endpoints'] = reordered[:max(1, int(max_endpoints))]
        peers[str(peer_id)] = entry
        data['peers'] = peers
        _save_raw(data)
    _invalidate_sync_board()
    return True


def demote_static_endpoint(peer_id, endpoint):
    """Move *endpoint* (``'host:port'``) to the TAIL of the peer's
    ``static_endpoints`` (and legacy ``endpoints``) lists. Called by
    the fan-out path after a connect to that address failed
    (refused / connect-timeout), so the next fallback resolution —
    which reads the lists head-first — tries a different candidate
    instead of re-dialing a dead address forever (stale-peer-address
    incidents 2026-07-10/11). No-op when the endpoint isn't listed
    or is already last. Returns True when something moved."""
    moved = False
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return False
        entry = _normalize_entry(peers[peer_id])
        for key in ('static_endpoints', 'endpoints'):
            current = [str(e) for e in (entry.get(key) or []) if e]
            if endpoint in current and current[-1] != endpoint:
                entry[key] = ([e for e in current if e != endpoint]
                              + [endpoint])
                moved = True
        if not moved:
            return False
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
    return True


def set_peer_last_seen_main(peer_id, langcode, sha):
    """Record a paired peer's ``refs/heads/main`` SHA for a given
    project, as last observed via ls-remote or verified push.
    Drives ``repo._lan_unshared`` and ``repo._at_risk`` (v0.47.0;
    were combined as ``server._unshared_commit_count`` in 0.46.x) —
    walks HEAD excluding the union of every paired peer's most-
    recent observed-main SHA for this project. Updates monotonic
    in spirit: callers call this only on actual observations.

    Returns True if the peer existed (and the value was written),
    False otherwise. Empty / falsy ``langcode`` or ``sha`` are
    no-ops.
    """
    if not peer_id or not langcode or not sha:
        return False
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return False
        entry = _normalize_entry(peers[peer_id])
        last_seen_main = dict(entry.get('last_seen_main') or {})
        last_seen_main[str(langcode)] = str(sha)
        entry['last_seen_main'] = last_seen_main
        # Ref-level observation of THIS project → bump its
        # per-project stamp. This is the board's "(date)": it moves
        # ONLY when we re-obtained the information the row's judgment
        # is made from (their main's SHA), never on mere contact —
        # "don't move the stamp because we saw the phone" (Kent
        # 2026-07-24). The peer-level last_seen_at (contact) rides
        # along, since an observation implies contact.
        obs = dict(entry.get('project_seen_at') or {})
        obs[str(langcode)] = _now_iso()
        entry['project_seen_at'] = obs
        entry['last_seen_at'] = _now_iso()
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
    _invalidate_sync_board()  # timestamp (and possibly state) moved
    return True


def set_peer_probe_result(peer_id, langcode, outcome, detail=''):
    """Record the outcome of the last reach ATTEMPT for this project.

    Deliberately does NOT touch ``project_seen_at`` or ``last_seen_at``:
    an attempt is not an observation, and a failed attempt certainly
    isn't. This exists so the board can say WHY a row is stale —
    'unreachable' (timeout / no route / refused) is a connectivity
    problem, while 'not_served' means the peer answered and declined to
    serve the project, which is a sharing/reciprocation problem. Before
    0.55.20 both looked identical: a row that simply stopped updating.

    ``outcome='ok'`` clears the condition after a successful peek."""
    if not peer_id or not langcode or not outcome:
        return False
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return False
        entry = dict(peers[peer_id] or {})
        probe = dict(entry.get('probe') or {})
        probe[str(langcode)] = {
            'outcome': str(outcome),
            'at': _now_iso(),
            'detail': str(detail or '')[:200],
        }
        entry['probe'] = probe
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
    _invalidate_sync_board()
    return True


def set_peer_covered_local(peer_id, langcode, sha):
    """Record that *sha* — one of OUR OWN commits — is confirmed
    contained in *peer_id*'s state for *langcode*. Called from the
    delivery-confirmation paths (verified push, "already at" no-op,
    peer-contains-local ancestry check, post-receive peek match).
    The sync-status walkers fall back to this when the peer's
    ``last_seen_main`` isn't in our object store — see
    ``_normalize_entry``. Same contract as
    ``set_peer_last_seen_main``: only call on actual observations;
    empty args are no-ops."""
    if not peer_id or not langcode or not sha:
        return False
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return False
        entry = _normalize_entry(peers[peer_id])
        covered = dict(entry.get('last_covered_local') or {})
        covered[str(langcode)] = str(sha)
        entry['last_covered_local'] = covered
        # Verified containment = ref-level observation of THIS
        # project; same stamp discipline as set_peer_last_seen_main.
        obs = dict(entry.get('project_seen_at') or {})
        obs[str(langcode)] = _now_iso()
        entry['project_seen_at'] = obs
        entry['last_seen_at'] = _now_iso()
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
    _invalidate_sync_board()  # a delivery changed a peer's coverage
    return True


def peer_coverage_for(langcode):
    """Return ``[(last_seen_main_sha, last_covered_local_sha)]`` —
    one tuple per paired peer that has at least one of the two
    recorded for *langcode* (either element may be ``''``). The
    sync-status walkers exclude the peer's main when they hold it,
    else fall back to the covered-local commit; a peer with
    neither usable contributes no exclusion (honest LAN-N)."""
    if not langcode:
        return []
    with _LOCK:
        data = _load_raw()
    out = []
    for entry in (data.get('peers') or {}).values():
        norm = _normalize_entry(entry)
        main = (norm.get('last_seen_main') or {}).get(langcode, '')
        covered = (norm.get('last_covered_local') or {}).get(
            langcode, '')
        if main or covered:
            out.append((main, covered))
    return out


def peers_sharing_project(langcode):
    """Return the list of ``peer_id``s whose ``shared_projects`` list
    contains *langcode*. Used by post-publish fan-out and other paths
    that need to notify every paired peer who has this project on
    their allow-list (e.g. a newly-set ``remote_url`` propagating
    across the LAN so peer Publish doesn't create a duplicate github
    repo). Empty list if no peer has shared this langcode.
    """
    if not langcode:
        return []
    langcode = str(langcode)
    with _LOCK:
        data = _load_raw()
    out = []
    for peer_id, entry in (data.get('peers') or {}).items():
        norm = _normalize_entry(entry)
        if langcode in norm['shared_projects']:
            out.append(str(peer_id))
    return out


def touch_last_seen(peer_id):
    """Bump ``last_seen_at`` to now on an authenticated handshake
    (phase 4 calls this). Returns ``True`` if the peer existed."""
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        if peer_id not in peers:
            return False
        entry = _normalize_entry(peers[peer_id])
        entry['last_seen_at'] = _now_iso()
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
    return True
