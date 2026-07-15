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


def record_pair(peer_id, fp, device_name, endpoint=''):
    """Insert or update a peer entry on pair-accept. Preserves
    existing ``shared_projects`` and ``static_endpoints`` if the
    peer is already known (re-pair just refreshes the cert
    fingerprint and the QR-captured endpoint). Returns the
    canonical entry."""
    with _LOCK:
        data = _load_raw()
        peers = dict(data.get('peers') or {})
        existing = _normalize_entry(peers.get(peer_id, {}))
        entry = {
            'device_name': str(device_name or ''),
            'fp': str(fp or ''),
            'endpoints': [endpoint] if endpoint else (
                existing['endpoints'] or []),
            'static_endpoints': existing['static_endpoints'],
            'shared_projects': existing['shared_projects'],
            'paired_at': existing['paired_at'] or _now_iso(),
            'last_seen_at': _now_iso(),
        }
        peers[str(peer_id)] = entry
        data['peers'] = peers
        _save_raw(data)
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
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
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
        peers[peer_id] = entry
        data['peers'] = peers
        _save_raw(data)
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
