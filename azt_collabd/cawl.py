"""
CAWL image-URL index + image-binary cache, daemon-owned.

The CAWL → image map is *suite-scoped* infrastructure shared across
every peer on a device. Pre-0.37 each peer fetched the listing
directly from ``api.github.com/repos/<image_repo>/git/trees/HEAD?
recursive=1`` on every project load and held the result in a
per-peer in-memory dict — three structural problems:

1. **Rate limit.** GitHub's unauthenticated REST cap is 60/hour/IP;
   a tight rebuild loop or multi-peer device exhausted it
   trivially, leaving the resolver dict empty and entries without
   locally-cached images rendering with no illustration.
2. **Per-peer duplication.** N peers × N copies of the cache on
   disk, sandbox-isolated so they can't share.
3. **Install-day no-network.** A fresh install with no
   connectivity couldn't bootstrap the index at all.

This module moves both the **index** (tree listing) and the
**image binaries** to daemon ownership. One fetch per device per
``_INDEX_TTL_SECONDS`` window for the index; one fetch per image
ever (binaries don't expire). Stale-cache fallback on every
network failure so a flaky GitHub never makes things worse than
the pre-migration peer behaviour.

### Repo selection

CAWL is a *project setting* — different projects can point at
different image_repos (vanity sets, fork, culturally specific
imagery). The repo slug is stored on the daemon's Project record
(``projects.json[<lang>].cawl_image_repo``); the daemon-global
``config.cawl_image_repo()`` is the fallback default for projects
that don't have an override set.

### Cache layout

```
$AZT_HOME/cawl/
    <owner>/<repo>/
        index.json
        images/
            cawl-1234.jpg
            cawl-5678.png
            ...
```

The repo-slug-keyed structure means N projects sharing one repo
share one cache directory — no duplication across projects.
Different repos get their own subdirectories so they don't
trample each other.
"""

import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from . import config as _config
from .net import _ensure_ssl
from . import projects as _projects
from .paths import azt_home


# Load-time marker. Distinctive 0.50.34+ line so a single grep of
# the daemon log tells us whether the deployed cawl.py is current
# or whether the bundle is partially stale. The combined daemon
# fingerprint alone can shift from any single-file edit (e.g., a
# ``__version__`` bump) without proving this specific module is
# fresh — and that's exactly the failure shape we hit when the
# 0.50.30 ``cache_status`` fix appeared deployed (version probe
# said 0.50.30, overall fingerprint shifted between rebuilds) but
# the bug behaviour persisted because cawl.py itself wasn't
# updated. If you don't see this line in the daemon log after a
# claimed 0.50.34+ deploy, the deploy didn't pick up cawl.py.
# Compare against ``module_fingerprints()`` for the same answer in
# structured form.
sys.stderr.write(
    '[cawl] module loaded; v0.50.34+ — tuple-returning '
    'get_image_path; worker bumps source under '
    "_cache_status_lock alongside state['completed'] += 1\n")
sys.stderr.flush()


# Refresh window for the index. The CAWL → image-URL mapping
# changes slowly (new images get added, rarely renamed);
# refreshing once per device per day amortises the GitHub
# round-trip to a level the unauthenticated rate limit can
# sustain even on a shared-IP lab / CI host. Image binaries
# don't expire — once cached, they stay cached.
_INDEX_TTL_SECONDS = 24 * 60 * 60

# Cap on the daemon's outbound HTTP calls. Short enough that a
# wedged network doesn't stall a project load while still allowing
# GitHub's typical p99 (~5s) some slack.
_FETCH_TIMEOUT_SECONDS = 15

# GitHub tree-listing endpoint. ``HEAD`` follows the repo's
# default branch so a fork can rename ``main`` → ``master``
# (or vice versa) without breaking the lookup. ``recursive=1``
# flattens nested directories — image repos in the suite tend
# to put files at top level, but the flag costs nothing.
_GITHUB_TREE_URL = (
    'https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1')

# Raw-content URL template. Lives on a different rate-limit
# domain (raw.githubusercontent.com is effectively unmetered for
# normal anonymous use) so per-image fetches don't burn through
# the API-side 60/hr budget the index fetch consumes.
_RAW_URL_TEMPLATE = (
    'https://raw.githubusercontent.com/{repo}/HEAD/{path}')


# Coalesce concurrent fetches per cache file: two peers asking
# for the same resource at the same time shouldn't both hit the
# network. Path-keyed so two different images can fetch in
# parallel without serializing.
_fetch_locks_lock = threading.Lock()
_fetch_locks = {}




def _lock_for(path):
    with _fetch_locks_lock:
        lk = _fetch_locks.get(path)
        if lk is None:
            lk = threading.Lock()
            _fetch_locks[path] = lk
        return lk


def cache_root():
    """``$AZT_HOME/cawl/`` — created on first write."""
    return os.path.join(azt_home(), 'cawl')


def _repo_cache_dir(repo):
    """Per-repo cache subdirectory under ``cache_root()``. The
    ``owner/repo`` slug becomes a two-segment path:
    ``$AZT_HOME/cawl/<owner>/<repo>/``. Two projects pointing at
    the same repo share this directory; two projects pointing at
    different repos get separate subtrees."""
    return os.path.join(cache_root(), repo)


# Memoisation for the no-prefetch-active branch of
# cache_status. When a peer has driven a ``start_prefetch`` call
# the daemon uses per-job state for accurate progress reporting
# (see ``_prefetch_state`` below). Outside that, ``cache_status``
# falls back to "on-disk count vs. index image-count".
#
# The on-disk count uses a short-TTL ``os.walk`` cache rather
# than a counter + lazy-seed: the previous incremental approach
# had subtle race conditions (when does the seed walk happen
# relative to in-flight ``_note_image_cached`` increments?), and
# when it went wrong it failed silently with a wrong-looking
# total in the UI. A TTL'd ``os.walk`` is accurate by
# construction — at the cost of one walk every ``_WALK_TTL``
# seconds. For the canonical 1700-image set that's ~50 ms; at
# 0.5s TTL with 1 Hz polling, half the polls hit the cache and
# the other half walk. Comfortable on a phone.
_WALK_TTL_SECONDS = 0.5
_cache_status_lock = threading.Lock()
_walk_cache = {}    # repo -> (timestamp, count)
_total_count_cache = {}        # repo -> (mtime, count)


# Daemon-driven prefetch state. A peer that wants the daemon to
# warm a working set of CAWL images calls ``start_prefetch(repo,
# paths)``; the daemon spawns a background thread that iterates
# the paths via ``get_image_path`` (which serves from cache or
# fetches on demand). The thread maintains a per-repo progress
# record the ``cache_status`` poll reads back.
#
# Why daemon-driven: the peer used to iterate the list itself,
# one ``CAWLHandle.open_read`` per entry, and the daemon answered
# each request independently. That worked but left the daemon
# ignorant of "the full set being warmed", so its ``cache_status``
# could only report "on-disk count vs. all-cacheable-files in the
# index" — a misleading total because the peer's working set is
# typically a subset (one variant per CAWL identifier, where the
# canonical repo has 2-4 variants per ID). Daemon-driven puts the
# iteration and progress tracking on the daemon, which is the
# party that actually does the fetch + cache work.
_prefetch_state = {}           # repo -> dict (see _make_prefetch_state)
_prefetch_threads = {}         # repo -> Thread


# After this many consecutive offline-class failures inside the
# prefetch worker, bail out rather than burn through the rest of
# the list logging the same DNS error N times. Trip count is low
# because real GitHub fetches succeed in <500 ms; three back-to-
# back URLError failures means the device dropped connectivity,
# not that a few individual files happen to be missing.
_PREFETCH_CONSECUTIVE_FAIL_LIMIT = 3


def _make_prefetch_state(requested):
    return {
        'requested': requested,
        'completed': 0,
        'failed': 0,
        'skipped_offline': False,    # device was offline at start
        'circuit_open': False,       # bailed after consecutive failures
        'started_at': time.time(),
        'finished': False,
        'finished_at': None,
        # Per-source fetch counters (0.50.21). Lets a progress
        # display tell the user whether bytes are coming from
        # the local cache (instant), a paired LAN peer (LAN
        # round-trip), or upstream GitHub (cellular round-trip).
        # When ``from_lan`` is climbing the user knows the
        # paired-peer cache is doing its job; when ``from_upstream``
        # is the only counter advancing the LAN-share path isn't
        # producing hits and bytes are being pulled over WAN.
        'from_cache': 0,
        'from_lan': 0,
        'from_upstream': 0,
        # The source the most recently successful fetch came from,
        # for a one-glance "what's serving right now" display.
        # Values: ``'cache'`` | ``'lan'`` | ``'upstream'`` | ``''``.
        'last_source': '',
    }


_SOURCE_FIELD = {
    'cache': 'from_cache',
    'lan': 'from_lan',
    'upstream': 'from_upstream',
}


def _bump_source_counter(repo, source):
    """Thread-safe bump of the per-source counter in
    ``_prefetch_state[repo]``. ``source`` is one of ``'cache'``,
    ``'lan'``, ``'upstream'``. Silently no-ops if no prefetch is
    running for *repo* (e.g. peer-driven on-demand
    ``get_image_path`` outside any prefetch window).

    Public path for on-demand callers (``server._h_cawl_image`` /
    Android ContentProvider's image-open handler) so user-driven
    fetches that land during an active prefetch still contribute
    to the source counters. The prefetch worker itself bumps
    inline (under the same lock as ``state['completed'] += 1``)
    to make "completed without source" impossible by
    construction; see ``_prefetch_worker``. 0.50.30."""
    if not source:
        return
    field = _SOURCE_FIELD.get(source)
    if field is None:
        return
    with _cache_status_lock:
        state = _prefetch_state.get(repo)
        if state is None:
            return
        state[field] = int(state.get(field, 0) or 0) + 1
        state['last_source'] = source


def _prefetch_worker(repo, paths, lan_extras=None):
    """Iterate ``paths`` and warm each via ``get_image_path``.
    Increments ``_prefetch_state[repo]['completed']`` on each
    successful resolve; ``failed`` otherwise. Updates ``finished``
    when done so the banner can stop polling.

    ``get_image_path`` is lock-coalesced per target path, so a
    concurrent on-demand request from another peer (or another
    prefetch thread for the same repo, which shouldn't happen
    given ``start_prefetch``'s idempotency) won't double-fetch.
    Cache hits return immediately; cache misses fetch from
    GitHub. Failures are logged inside ``get_image_path`` via
    its existing ``[cawl] image fetch failed`` path.

    Two offline guards keep an offline boot from hammering DNS
    for every entry in ``paths``:

    1. Connectivity gate at start. If ``_has_internet()`` is
       false, mark the state ``skipped_offline=True`` /
       ``finished=True`` and return without touching any path.
       0.41.11 moved iteration into this worker; the peer-side
       circuit breaker that lived in the old per-image
       iteration model no longer applies.
    2. Consecutive-failure circuit breaker. Three back-to-back
       fetch failures (typically DNS or connection refused)
       trip ``circuit_open=True`` and bail. A few genuinely
       missing files won't trip this — real fetches succeed in
       <500 ms while offline-class failures bunch up
       immediately and consecutively."""
    from .net import _has_internet
    if paths and not _has_internet():
        with _cache_status_lock:
            state = _prefetch_state.get(repo)
            if state is not None:
                state['skipped_offline'] = True
                state['finished'] = True
                state['finished_at'] = time.time()
        print(f'[cawl] prefetch skipped: device offline '
              f'(repo={repo!r}, requested={len(paths)})',
              file=sys.stderr, flush=True)
        return

    consecutive_fail = 0
    for path in paths:
        target, source = get_image_path(repo, path)
        with _cache_status_lock:
            state = _prefetch_state.get(repo)
            if state is None:
                # State got nuked (daemon shutting down, or a new
                # prefetch replaced ours). Bail.
                return
            if target is not None:
                state['completed'] += 1
                if source:
                    state['last_source'] = source
                    field = _SOURCE_FIELD.get(source)
                    if field is not None:
                        state[field] = int(
                            state.get(field, 0) or 0) + 1
                    # One-shot diagnostic: log the FIRST successful
                    # bump per worker session so we can confirm the
                    # worker is actually reaching this branch and
                    # writing into ``state['last_source']``. If you
                    # see this line in the daemon log but
                    # ``last_source`` is still rendered empty on
                    # the peer side, the bug is post-daemon
                    # (response read / display). 0.50.35.
                    if not state.get('_logged_first_bump'):
                        state['_logged_first_bump'] = True
                        print(f'[cawl] worker first bump: '
                              f"source={source!r} "
                              f"state['last_source']="
                              f"{state['last_source']!r} "
                              f"state['from_cache']="
                              f"{state.get('from_cache', 0)} "
                              f"state['completed']="
                              f"{state['completed']} "
                              f"repo={repo!r}",
                              file=sys.stderr, flush=True)
                else:
                    # Bug-class: target landed without a source tag.
                    # By the post-0.50.30 contract this is
                    # impossible, but keep a loud breadcrumb so a
                    # future regression doesn't silently revert the
                    # "indicator empty while files land" UX.
                    print(f'[cawl] bug: completed without source '
                          f'for {repo!r}/{path!r}',
                          file=sys.stderr, flush=True)
                consecutive_fail = 0
            else:
                state['failed'] += 1
                consecutive_fail += 1
        if consecutive_fail >= _PREFETCH_CONSECUTIVE_FAIL_LIMIT:
            with _cache_status_lock:
                state = _prefetch_state.get(repo)
                if state is not None:
                    state['circuit_open'] = True
                    state['finished'] = True
                    state['finished_at'] = time.time()
            print(f'[cawl] prefetch circuit-break after '
                  f'{consecutive_fail} consecutive failures '
                  f'(repo={repo!r}, completed={state["completed"]} '
                  f'of {state["requested"]})',
                  file=sys.stderr, flush=True)
            return
    # LAN-extras pass (0.50.14+): opportunistically grab variants
    # the WAN-policy gate would have skipped. No upstream fallback;
    # a peer-cache miss is a silent no-op so we don't waste cycles
    # or burn bandwidth. Doesn't affect requested / completed /
    # failed in the cache-status state — these are bonus images.
    lan_hits = 0
    for path in lan_extras or []:
        # Cheap pre-check: skip if already on disk (e.g. a prior
        # lan_extras pass landed it). _resolve_image_target +
        # isfile is much cheaper than the per-call peer iteration.
        try:
            t = _resolve_image_target(repo, path)
        except Exception:
            t = None
        if t is not None and os.path.isfile(t):
            continue
        extra_target, extra_source = get_image_path_lan_only(
            repo, path)
        if extra_target is not None:
            lan_hits += 1
            # lan_extras are bonus — they don't move ``completed``,
            # but they DO move the source counters so the user sees
            # "via LAN" climb when paired peers are serving variants.
            with _cache_status_lock:
                state = _prefetch_state.get(repo)
                if state is None:
                    return
                if extra_source:
                    state['last_source'] = extra_source
                    field = _SOURCE_FIELD.get(extra_source)
                    if field is not None:
                        state[field] = int(
                            state.get(field, 0) or 0) + 1
        # Re-check daemon shutdown (state could have been nuked
        # between iterations).
        with _cache_status_lock:
            if _prefetch_state.get(repo) is None:
                return
    if lan_hits:
        print(f'[cawl] prefetch lan_extras: {lan_hits} bonus '
              f'variant(s) pulled from paired peers '
              f'(repo={repo!r})',
              file=sys.stderr, flush=True)
    with _cache_status_lock:
        state = _prefetch_state.get(repo)
        if state is not None:
            state['finished'] = True
            state['finished_at'] = time.time()


def start_prefetch(repo, paths, lan_extras=None):
    """Kick off a background prefetch of *paths* for *repo*. The
    daemon's worker iterates the list and warms the cache; peers
    poll ``cache_status`` for progress.

    ``lan_extras`` (optional, since 0.50.14) is a list of
    additional rel_paths to opportunistically fetch from paired
    LAN peers ONLY — no upstream GitHub fallback for these. Used
    by ``auto_prefetch`` to pull extra variants beyond what the
    ``cawl.prefetch_all_variants=False`` policy allows over WAN:
    if a paired peer already has them cached on the LAN, take
    them for free; if not, skip. These don't count toward
    ``requested`` / ``completed`` / ``failed`` in the cache-
    status state — they're a side-channel bonus.

    Idempotency: if a prefetch is already running for this repo
    AND its requested-set matches *paths*, return the existing
    state without restarting. If the requested-set differs (peer
    decided to warm a different working set), replace the state
    and start a new worker — the old thread will see the state
    change and exit on its next iteration.

    Returns the current state dict::

        {'requested': N, 'completed': M, 'failed': K,
         'started_at': ts, 'finished': bool,
         'finished_at': ts | None}

    Empty *paths* returns an immediately-finished state. ``repo``
    empty or paths invalid returns None."""
    repo = (repo or '').strip()
    if not repo:
        return None
    if not isinstance(paths, (list, tuple)):
        return None
    paths = [p for p in paths if isinstance(p, str) and p]
    if lan_extras is not None and isinstance(lan_extras, (list, tuple, set)):
        lan_extras = [p for p in lan_extras if isinstance(p, str) and p]
    else:
        lan_extras = []
    requested = len(paths)
    with _cache_status_lock:
        existing = _prefetch_state.get(repo)
        if existing is not None and not existing.get('finished'):
            # A worker is already running for this repo. Don't
            # start a second one — return the existing state.
            #
            # Pre-0.41.21 we replaced the state when ``requested``
            # differed, on the theory that "old worker exits on
            # next loop iteration via state-identity check, worst
            # case it bumps the new state once before exiting,
            # harmless." That was wrong once two prefetch
            # producers (Stage A auto_prefetch with the full
            # index + pre-Stage-B peers still POSTing
            # cawl_prefetch with their working subset) started
            # arriving on overlapping timelines: two worker
            # threads simultaneously iterated paths through
            # urllib/SSL, doubled the JNI dance, and were the
            # leading suspect for a NULL-deref SIGSEGV in the
            # daemon's :provider process ~2 s after the second
            # POST.
            #
            # The peer's working subset is always a subset of the
            # auto_prefetch full index for the same repo, so the
            # in-flight worker will eventually warm everything
            # the peer cares about. Different repos key
            # ``_prefetch_state`` independently and never collide.
            return dict(existing)
        # Either no prior state or the previous worker finished.
        # Fresh start.
        state = _make_prefetch_state(requested)
        _prefetch_state[repo] = state
        snapshot = dict(state)
    if requested == 0:
        # Mark finished immediately — nothing to do. No worker
        # thread for an empty list.
        with _cache_status_lock:
            state = _prefetch_state.get(repo)
            if state is not None:
                state['finished'] = True
                state['finished_at'] = time.time()
                snapshot = dict(state)
        return snapshot
    t = threading.Thread(
        target=_prefetch_worker, args=(repo, paths, lan_extras),
        name=f'cawl-prefetch-{repo}', daemon=True)
    _prefetch_threads[repo] = t
    t.start()
    return snapshot


# Throttle for ``auto_prefetch`` — at most one trigger attempt
# per repo per this many seconds. _touch_project (and thus
# auto_prefetch) fires on every langcode-bound endpoint
# including 1 Hz cache-status polls, so the throttle is what
# keeps us from re-probing _has_internet every second.
_AUTO_PREFETCH_THROTTLE_S = 30.0
_auto_prefetch_last_at = {}    # repo -> monotonic timestamp


def wordlist_name(image_repo):
    """Best-effort wordlist label derived from an image-repo slug.

    Strips the ``images_`` / ``images-`` / ``Images_`` / ``Images-``
    prefix conventionally used to name CAWL-style image
    repositories, returning the trailing wordlist identifier.

    Examples::

        kent-rasmussen/images_CAWL → CAWL
        foo/images-paws            → paws
        foo/MyWordlist             → MyWordlist
        ''                         → ''

    Used by the daemon settings UI to label the prefetch section
    with the currently-active wordlist (so a user with multiple
    projects on different image repos can tell which one the
    toggle affects)."""
    if not image_repo:
        return ''
    last = image_repo.rsplit('/', 1)[-1]
    for prefix in ('images_', 'images-', 'Images_', 'Images-'):
        if last.startswith(prefix) and len(last) > len(prefix):
            return last[len(prefix):]
    return last


def auto_prefetch(repo):
    """Daemon-side trigger: warm the cache for *repo*'s entire
    image index. Idempotent + throttled — safe to call from
    every langcode-bound endpoint via ``_touch_project``.

    The peer no longer needs to compute a working-set list and
    POST ``cawl/prefetch``; the daemon already owns LIFT path +
    image_repo and decides for itself what to warm. The peer
    just polls ``cache_status`` for progress.

    Throttle: at most one trigger per repo per
    ``_AUTO_PREFETCH_THROTTLE_S`` seconds. Within the window,
    returns without touching state.

    Past the throttle, defers to ``start_prefetch``'s existing
    idempotency: a running worker with matching paths → no-op;
    a finished worker (success OR offline-skipped) → restart,
    which is the path that retries when network may have come
    back. ``start_prefetch`` itself returns immediately —
    iteration runs in a background thread.

    Peer's explicit ``cawl/prefetch`` POST still works for
    backward compatibility; lock-coalescing on the per-target
    fetch lock means a peer-driven + daemon-driven trigger on
    overlapping path sets won't double-fetch anything."""
    repo = (repo or '').strip()
    if not repo:
        return
    now = time.monotonic()
    last = _auto_prefetch_last_at.get(repo)
    if last is not None and (now - last) < _AUTO_PREFETCH_THROTTLE_S:
        return
    _auto_prefetch_last_at[repo] = now
    paths_wan = _index_image_paths(repo)
    if not paths_wan:
        return
    # Warm-cache short-circuit (0.50.44). Pre-fix, every throttle
    # window past, ``start_prefetch`` spawned a fresh worker that
    # re-walked the entire image index from the on-disk cache —
    # hundreds of per-path lookups burning CPU on data we already
    # had. ``cache_status`` is a cheap (memoised) ``os.walk`` plus
    # index count; if the on-disk count covers the index, the
    # worker has nothing to do, so skip the whole spawn.
    #
    # The semantics of "fully warm" here are deliberately the WAN
    # index (``paths_wan``), not ``_index_image_paths_all`` — the
    # LAN extras are opportunistic bonuses that don't gate the
    # warm-check. A peer policy of ``prefetch_all_variants=False``
    # would otherwise be considered "never warm" forever.
    try:
        status = cache_status(repo)
        cached = int(status.get('cached', 0) or 0)
        total = int(status.get('total', 0) or 0)
        if total > 0 and cached >= total:
            return
    except Exception:
        # Defensive: if cache_status raises we'd rather start a
        # worker than silently lose prefetch behaviour entirely.
        pass
    # LAN extras = the variants the WAN-policy filter dropped.
    # When ``cawl.prefetch_all_variants=True`` (everything is
    # WAN-eligible) this set is empty and we skip the second
    # pass. When the policy restricts WAN to the preferred
    # variant, the LAN side opportunistically grabs the rest if a
    # paired peer has them cached — peer-side bandwidth is free.
    all_paths = _index_image_paths_all(repo)
    lan_extras = sorted(set(all_paths) - set(paths_wan))
    start_prefetch(repo, paths_wan, lan_extras=lan_extras)


def on_online_edge():
    """Called by the connectivity watcher when offline → online.

    For every repo whose last prefetch was offline-skipped or
    circuit-broken, clears the auto_prefetch throttle and
    re-fires auto_prefetch so the cache resumes warming.

    The 30 s throttle is normally what keeps ``_touch_project``
    from re-probing ``_has_internet`` every second; on an
    authoritative edge from the watcher we want to bypass it
    because the probe state has just changed.

    Idempotent — safe to call on every detected edge, even if
    no repo has a stale state."""
    with _cache_status_lock:
        stale_repos = [
            repo for repo, state in _prefetch_state.items()
            if state.get('skipped_offline')
            or state.get('circuit_open')
        ]
    for repo in stale_repos:
        _auto_prefetch_last_at.pop(repo, None)
        auto_prefetch(repo)
        print(f'[cawl] online-edge retry: repo={repo!r}',
              file=sys.stderr, flush=True)


def _index_image_paths(repo):
    """Return the list of image-shaped entry paths in the cached
    index for *repo*.

    Policy gate: ``$AZT_HOME/config.json :: cawl.prefetch_all_variants``
    (read via ``store.get_cawl_prefetch_all_variants``). Default
    False — for each CAWL id directory, returns one path: the
    first whose basename contains the ``__`` preferred-variant
    marker, falling back to the first file if no variant marker
    is present. True — returns every image-shaped entry.

    This is the **WAN-allowed** set: when the policy is False,
    upstream fetching is restricted to the preferred variant to
    save metered bandwidth. The LAN side ignores this filter (see
    ``_index_image_paths_all`` and ``_prefetch_worker``'s
    ``lan_extras`` arm) — peer-cached variants are free to take
    if they're already on the LAN.

    Empty list if the index isn't cached yet (the seed JSON or a
    successful index fetch populates it)."""
    images = _index_image_paths_all(repo)
    if not images:
        return []
    try:
        from . import store as _store
        prefetch_all = _store.get_cawl_prefetch_all_variants()
    except Exception:
        prefetch_all = False
    if prefetch_all:
        return images
    return _filter_preferred_variant_per_id(images)


def _index_image_paths_all(repo):
    """Unfiltered image-path list — every image-shaped entry in
    the index, no variant policy applied. Used as the LAN-side
    fetch list: even when ``cawl.prefetch_all_variants=False``
    restricts WAN to one image per CAWL id, the LAN side will
    happily take all variants that a paired peer has cached
    (0.50.14+). Bandwidth on LAN is essentially free; restricting
    LAN to the same filter as WAN would mean two phones on the
    same team can't share the non-preferred variants the
    first-phone-to-the-tower already downloaded.
    """
    cached = _read_cached_index(repo)
    if cached is None:
        return []
    images = []
    for entry in cached.get('files') or []:
        if not isinstance(entry, dict):
            continue
        full = entry.get('path')
        if (isinstance(full, str)
                and full.lower().endswith(('.png', '.jpg', '.jpeg'))):
            images.append(full)
    return images


def get_image_path_lan_only(repo, rel_path):
    """Try to land bytes for ``(repo, rel_path)`` from a paired
    LAN peer's cache and persist them locally. Returns
    ``(target, source)`` on success, ``(None, '')`` on miss.
    Mirror of ``get_image_path``'s return shape (0.50.30 refactor)
    so callers handle both functions uniformly.

    Distinct from ``get_image_path`` in that we do NOT fall
    through to GitHub on miss. Use this for the prefetch
    worker's ``lan_extras`` arm: variants the WAN-policy gate
    forbade us from fetching upstream, but which we'll
    opportunistically grab from LAN if available.
    """
    repo = (repo or '').strip()
    if not repo or not _looks_safe_rel_path(rel_path):
        return None, ''
    target = _resolve_image_target(repo, rel_path)
    if target is None:
        return None, ''
    if os.path.isfile(target):
        return target, 'cache'
    with _lock_for(target):
        if os.path.isfile(target):
            return target, 'cache'
        data = _fetch_image_bytes_from_lan_peer(repo, rel_path)
        if data is None:
            return None, ''
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f'{target}.tmp.{os.getpid()}'
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, target)
        except OSError as ex:
            print(f'[cawl] LAN-only cache write failed for '
                  f'{repo!r}/{rel_path!r}: {ex!r}',
                  file=sys.stderr, flush=True)
            return None, ''
    return target, 'lan'


def _filter_preferred_variant_per_id(paths):
    """Pick one image per CAWL id directory.

    CAWL repos use ``<cawl_id>/<image_name>.<ext>`` with multiple
    variants per id; the canonical preferred variant carries
    ``__`` in its basename (line-art with the ``__bw`` /
    ``__color`` / etc. suffix). For each id we return:

    - The first path whose basename contains ``__`` (the
      preferred variant), OR
    - The first path in the id directory if no variant has the
      marker (defensive fallback for ids that don't follow the
      convention).

    Stable order: ids in the order they first appear in
    ``paths``; one entry per id."""
    seen = {}
    order = []
    for path in paths:
        bits = path.split('/', 1)
        if len(bits) != 2:
            cawl_id, basename = '', path
        else:
            cawl_id, basename = bits[0], bits[1]
        entry = seen.get(cawl_id)
        if entry is None:
            entry = {'preferred': None, 'fallback': path}
            seen[cawl_id] = entry
            order.append(cawl_id)
        if entry['preferred'] is None and '__' in basename:
            entry['preferred'] = path
    return [
        (seen[cid]['preferred'] or seen[cid]['fallback'])
        for cid in order
    ]


def get_prefetch_state(repo):
    """Return a snapshot of the prefetch state for *repo*, or
    None if no prefetch has ever run for this repo in this
    daemon process. Cheap; just a dict copy under the lock."""
    repo = (repo or '').strip()
    if not repo:
        return None
    with _cache_status_lock:
        state = _prefetch_state.get(repo)
        return dict(state) if state else None


def _count_index_images(repo):
    """Image-shaped entries in the cached index for *repo*. Cheap
    after the first call per (repo, index mtime) — memoised on
    the index file's mtime so a refresh (which rewrites the
    cache file) invalidates the count automatically."""
    target = index_path(repo)
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return 0
    cached_entry = _total_count_cache.get(repo)
    if cached_entry is not None and cached_entry[0] == mtime:
        return cached_entry[1]
    cached = _read_cached_index(repo)
    if cached is None:
        return 0
    files = cached.get('files') or []
    total = sum(
        1 for f in files
        if isinstance(f, dict)
        and isinstance(f.get('path'), str)
        and f['path'].lower().endswith(('.png', '.jpg', '.jpeg')))
    _total_count_cache[repo] = (mtime, total)
    return total


def _walk_image_count(repo):
    """Count of cached image files on disk via ``os.walk``,
    cached for ``_WALK_TTL_SECONDS``. ~50-100 ms uncached on the
    canonical 1700-image set; at 0.5 s TTL with 1 Hz polling
    roughly half the polls hit the cache. Accurate-by-
    construction (no event-based bookkeeping that can drift)."""
    now = time.monotonic()
    with _cache_status_lock:
        cached_entry = _walk_cache.get(repo)
        if (cached_entry is not None
                and (now - cached_entry[0]) < _WALK_TTL_SECONDS):
            return cached_entry[1]
    # Walk outside the lock so concurrent calls don't serialize
    # on a long os.walk.
    images_dir = os.path.join(_repo_cache_dir(repo), 'images')
    if not os.path.isdir(images_dir):
        count = 0
    else:
        count = 0
        for _root, _dirs, files_in_dir in os.walk(images_dir):
            count += sum(
                1 for fn in files_in_dir
                if fn.lower().endswith(('.png', '.jpg', '.jpeg')))
    with _cache_status_lock:
        _walk_cache[repo] = (now, count)
    return count


def cache_status(repo):
    """Return a dict describing the image-cache state for *repo*.

    Shape::

        {'cached':         int   # images successfully on disk / completed
         'total':          int   # working-set size or index-image count
         'offline':        bool  # prefetch was skipped because device offline
         'circuit_open':   bool  # prefetch bailed after consecutive failures
         'finished':       bool  # no active worker thread for this repo
         }

    If a daemon-driven prefetch is active or completed for this
    repo, the counts come from that job's state — the peer asked
    the daemon to warm a specific working set, so progress
    against *that* set is what's meaningful. Banner becomes
    "M completed of N requested" and ends at 100% by construction.

    When the worker was offline-skipped (``skipped_offline``),
    ``cached`` falls back to the actually-on-disk count via
    ``_walk_image_count`` so the banner shows the most useful
    truth available (e.g. "1247 / 3000" from a prior successful
    run, not "0 / 3000" just because *this* boot couldn't fetch).
    The ``offline`` flag stays set so the peer can badge the bar
    "offline" instead of rendering it as stuck progress.

    Otherwise (no prefetch ever started for this repo this
    daemon-session), the fallback semantics are "files on disk
    vs. all image-shaped entries in the index". That total is a
    structural over-count vs. what most peers actually warm
    (the canonical CAWL repo has 2-4 variants per identifier,
    peers typically use one); the banner plateaus at the peer's
    working-set size, not the full index. Daemon-driven prefetch
    (via ``start_prefetch``) is the way to get an accurate bar."""
    repo = (repo or '').strip()
    if not repo:
        return {'cached': 0, 'total': 0, 'offline': False,
                'circuit_open': False, 'finished': True}
    pf = get_prefetch_state(repo)
    if pf is not None:
        offline = bool(pf.get('skipped_offline'))
        circuit_open = bool(pf.get('circuit_open'))
        finished = bool(pf.get('finished'))
        if offline:
            # Worker never iterated. Show what's actually on disk
            # from any prior run so the user sees a meaningful
            # baseline instead of "0 / N" each offline boot.
            # Cap at ``requested`` — ``_walk_image_count`` returns
            # the total file count in the on-disk cache directory,
            # which may exceed the current working-set size
            # (cache accumulates across sessions / past working
            # sets). Without the cap, the banner can report
            # ``cached > total``, which trips peer "cache warm,
            # hide" logic and looks like a daemon accounting bug.
            cached = min(_walk_image_count(repo), pf['requested'])
        else:
            # We surface only "completed" for the banner so
            # failures stay visible as "stuck short of total"
            # rather than "100% with silent failures".
            cached = pf['completed']
        # Per-source telemetry (0.50.21). Lets a peer-side
        # progress display tell the user whether bytes are being
        # served from a paired LAN peer's cache (free bandwidth)
        # or pulled over upstream cellular (the expensive path).
        # When ``from_lan`` is climbing and ``from_upstream`` is
        # flat, the LAN-share path (NOTES #3 / 0.50.14) is
        # working. When the inverse holds, paired peers either
        # don't have the byte cached or aren't reachable, and
        # bytes are coming over WAN. ``last_source`` is the
        # source of the most-recent successful fetch — a one-
        # glance "what's serving right now" tag.
        last_source = pf.get('last_source', '') or ''
        # Contract (0.50.30): if ``completed > 0`` and
        # ``last_source`` is still empty, something fed bytes
        # without tagging the source — the post-0.50.30 refactor
        # of ``get_image_path`` made this impossible by
        # construction, but the daemon's stderr log carries a
        # loud breadcrumb (``[cawl] bug: completed without
        # source``) AND the wire response reports
        # ``last_source='unknown'`` so peer UIs don't render an
        # empty indicator. Empty stays valid for the "no fetch
        # has happened yet this session" initial state.
        if last_source == '' and cached > 0:
            print(f'[cawl] cache_status bug: cached={cached} but '
                  f'last_source is empty (repo={repo!r})',
                  file=sys.stderr, flush=True)
            last_source = 'unknown'
        response = {
            'cached': cached, 'total': pf['requested'],
            'offline': offline, 'circuit_open': circuit_open,
            'finished': finished,
            'from_cache': int(pf.get('from_cache', 0) or 0),
            'from_lan': int(pf.get('from_lan', 0) or 0),
            'from_upstream': int(pf.get('from_upstream', 0) or 0),
            'last_source': last_source,
        }
        # Diagnostic: log the actual outbound response so we can
        # tell whether the empty ``last_source`` a peer reports is
        # what the daemon ACTUALLY sent or whether the peer
        # rewrote / dropped the field. If the daemon log shows
        # ``last_source='cache'`` on the wire and the peer's
        # `[cache-status]` line shows ``last_source=''``, the
        # bug is post-daemon. 0.50.35 — intentionally always-on
        # for now; we can rate-limit or remove after the empty-
        # ``last_source`` field investigation resolves.
        print(f'[cawl] cache_status response: '
              f"repo={repo!r} cached={response['cached']} "
              f"last_source={response['last_source']!r} "
              f"from_cache={response['from_cache']} "
              f"from_lan={response['from_lan']} "
              f"from_upstream={response['from_upstream']} "
              f"offline={response['offline']} "
              f"finished={response['finished']}",
              file=sys.stderr, flush=True)
        return response
    return {
        'cached': _walk_image_count(repo),
        'total': _count_index_images(repo),
        'offline': False, 'circuit_open': False,
        'finished': True,
        'from_cache': 0, 'from_lan': 0,
        'from_upstream': 0, 'last_source': '',
    }


def index_path(repo):
    """Canonical on-disk location of the cached index payload for
    a given image repo."""
    return os.path.join(_repo_cache_dir(repo), 'index.json')


def image_path(repo, rel_path):
    """Canonical on-disk location of a cached image binary.

    ``rel_path`` may be a flat filename or a nested rel-path
    (``0001_body/foo.png``) — the on-disk cache mirrors the
    repo's directory structure. Does NOT validate ``rel_path``;
    callers that take untrusted input go through
    ``get_image_path`` which path-traversal-checks first."""
    return os.path.join(_repo_cache_dir(repo), 'images', rel_path)


def resolve_image_repo(langcode):
    """Pick the image repo slug for ``langcode``. Priority:

    1. ``Project.cawl_image_repo`` if non-empty.
    2. Daemon-global ``config.cawl_image_repo()`` fallback.

    Returns the slug (e.g. ``'kent/images'``) or ``''`` if neither
    source has a value — callers should treat empty as "no image
    repo configured for this project" and short-circuit to an
    empty response."""
    p = _projects.get(langcode)
    if p is not None and (p.cawl_image_repo or '').strip():
        return p.cawl_image_repo.strip()
    return (_config.cawl_image_repo() or '').strip()


def _read_cached_index(repo):
    """Return the cached index dict for *repo*, or None if absent
    / malformed."""
    try:
        with open(index_path(repo), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_cached_index(repo, data):
    """Tempfile + rename so a partial write can't be read by a
    concurrent peer call. Best-effort — logs but doesn't raise on
    filesystem failures (the in-memory result still gets returned)."""
    target = index_path(repo)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f'{target}.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, target)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _fetch_index_from_github(repo):
    """Hit the GitHub tree API once. Returns a dict in the wire
    shape this module guarantees (repo / branch-ish / fetched_at /
    files). Raises on transport failure; the caller decides
    whether to fall back to a stale cache."""
    _ensure_ssl()
    url = _GITHUB_TREE_URL.format(repo=repo)
    req = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    from urllib.parse import quote as _urlquote
    tree = payload.get('tree') or []
    files = []
    for entry in tree:
        if not isinstance(entry, dict):
            continue
        if entry.get('type') != 'blob':
            continue
        path = entry.get('path') or ''
        if not path:
            continue
        # URL-encode the path for the per-file ``url`` field —
        # GitHub returns raw paths in the tree listing
        # ("0001_body/2d minimalistic ... .png") which are valid
        # filesystem paths but illegal in URLs. Peers iterating the
        # index would otherwise hit ``http.client.InvalidURL`` on
        # every image whose name contains a space, paren, comma,
        # etc. — common in CAWL filenames. ``safe='/'`` preserves
        # slashes between path components so the URL hierarchy is
        # intact; spaces / unsafe chars become %20 etc.
        encoded_path = _urlquote(path, safe='/')
        files.append({
            'path': path,
            'url': _RAW_URL_TEMPLATE.format(
                repo=repo, path=encoded_path),
        })
    return {
        'repo': repo,
        'branch': 'HEAD',
        'fetched_at': int(time.time()),
        'files': files,
    }


def _is_fresh(data):
    """True if the cached payload was fetched within the TTL."""
    if not isinstance(data, dict):
        return False
    fetched_at = data.get('fetched_at')
    if not isinstance(fetched_at, (int, float)):
        return False
    return (time.time() - fetched_at) < _INDEX_TTL_SECONDS


def _seed_index_if_bundled(repo):
    """Populate ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` from a
    bundled seed asset if one exists for *repo* and the cache
    file isn't already on disk.

    Seed source: ``azt_collabd/data/cawl/<owner>/<repo>/index.json``
    (a Python-package data file shipped inside the server APK +
    desktop install). Maintainer drops the file there as part of
    the release process; build adds it to the APK because
    ``server_apk/buildozer.spec`` includes ``json`` in
    ``source.include_exts``.

    Closes install-day-no-network: a freshly-installed device
    that has never reached GitHub still has *something* to
    serve, so peers can render illustrations on first launch.
    Subsequent fetches refresh as normal — once the seed's TTL
    expires, ``get_index`` attempts a network refresh and only
    falls back to the seed when offline (the standard stale-
    cache fallback). When the device DOES get online, the next
    refresh overwrites the seed with current data.

    Silent no-op when:

    - ``repo`` is empty or malformed (no ``owner/repo`` shape).
    - No bundled asset exists for this repo. (The seed is keyed
      by repo slug; only the suite-canonical CAWL repo is
      typically seeded. Fork / custom-repo projects don't get
      the seed benefit — their first launch still has to hit
      GitHub. Acceptable trade vs. bundling N seeds in every
      APK release.)
    - The cache file is already on disk. (Don't trample a real
      cached copy with the build-time seed.)
    - The bundled JSON is malformed. (Don't corrupt the cache
      with bad data.)
    """
    repo = (repo or '').strip()
    if '/' not in repo:
        return
    target = index_path(repo)
    if os.path.isfile(target):
        return
    owner, _, name = repo.partition('/')
    if not owner or not name:
        return
    try:
        from importlib.resources import files as _resource_files
    except ImportError:
        # Python < 3.9 — no bundled-asset support.
        return
    try:
        seed_path = (_resource_files('azt_collabd')
                     .joinpath('data', 'cawl', owner, name,
                               'index.json'))
        raw = seed_path.read_bytes()
    except (FileNotFoundError, AttributeError, ValueError,
            OSError, ModuleNotFoundError):
        return
    try:
        seed = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(seed, dict):
        return
    _write_cached_index(repo, seed)


def get_index(repo, force_refresh=False):
    """Return the cached/fetched index dict for *repo*.

    Returns ``{}`` when no cache exists AND the network fetch
    fails AND no bundled seed exists. Callers should treat empty
    ``files`` as "no images known" — same shape peers got
    pre-migration from an empty resolver, so peer code doesn't
    need a daemon-error branch.

    Seed-on-cold-cache: if no on-disk cache file exists yet and
    the server APK ships a bundled seed for this repo, the seed
    is copied into the cache before the freshness check. This
    closes install-day-no-network for the index lookup;
    subsequent refreshes overwrite normally. See
    ``_seed_index_if_bundled``.

    Stale-cache fallback: if a refresh attempt fails and a cached
    copy exists — even past TTL — return that copy. A stale index
    is strictly better than no index."""
    repo = (repo or '').strip()
    if not repo:
        return {}
    cached = _read_cached_index(repo)
    if cached is None:
        # Cold cache. Try the bundled seed before going to network
        # so an install-day-no-network device still has data to
        # serve. No-op if no seed is shipped for this repo.
        _seed_index_if_bundled(repo)
        cached = _read_cached_index(repo)
    if cached is not None and not force_refresh and _is_fresh(cached):
        return cached
    # Refresh path. Lock-coalesce so two peers don't both fetch
    # the same repo's index at the same time.
    with _lock_for(index_path(repo)):
        cached = _read_cached_index(repo)
        if cached is not None and not force_refresh and _is_fresh(cached):
            return cached
        try:
            fresh = _fetch_index_from_github(repo)
        except (urllib.error.URLError, OSError, ValueError,
                TimeoutError, http.client.HTTPException) as ex:
            import sys
            print(f'[cawl] index refresh failed for {repo!r}: '
                  f'{type(ex).__name__}: {ex}; '
                  f'serving cached={cached is not None}',
                  file=sys.stderr, flush=True)
            return cached if cached is not None else {}
        _write_cached_index(repo, fresh)
        return fresh


# ── Image binaries ───────────────────────────────────────────────────────


def _looks_safe_rel_path(rel_path):
    """Reject anything that could escape the per-repo images dir
    via path-component tricks. Accepts nested rel-paths
    (``0001_body/foo.png``) — CAWL repos commonly nest images
    under category folders — but rejects:

    - Empty / non-string input.
    - Absolute paths (leading ``/`` or ``\\``).
    - Any ``..`` or ``.`` component (path traversal).
    - Backslashes (Windows-style separators; we only accept
      forward-slash POSIX-style paths on the wire so the URI
      and HTTP forms match the on-disk normalisation).
    - Empty components (``foo//bar`` is suspicious; reject).

    Pre-0.41.1 this function was called ``_looks_safe_basename``
    and rejected any ``/`` outright. That silently broke CAWL
    fetching for any repo whose images are nested under
    category subdirs (the canonical ``kent-rasmussen/images_CAWL``
    has paths like ``0001_body/<filename>.png``). The rename
    documents the new semantics — ``rel_path`` is a relative
    path that may contain forward slashes between components."""
    if not rel_path or not isinstance(rel_path, str):
        return False
    if '\\' in rel_path:
        return False
    if rel_path.startswith('/'):
        return False
    parts = rel_path.split('/')
    for p in parts:
        if not p:                  # empty component → ``foo//bar``
            return False
        if p in ('.', '..'):
            return False
    return True


def _fetch_image_bytes_from_lan_peer(repo, rel_path):
    """Try paired LAN peers for cached image bytes before going to
    GitHub (NOTES #3, since 0.50.14).

    When two phones share a project, both prefetch the same CAWL
    image set independently — pre-0.50.14 each downloaded the full
    set from GitHub (1700+ images for SILCAWL), wasting bandwidth
    on metered field links. This helper asks each currently-
    resolved paired peer "do you have this byte cached?" via the
    LAN listener's ``/v1/lan/cawl_fetch`` endpoint. First 200
    response wins; 404 / connection failure moves to the next
    peer. Returns ``None`` (caller falls through to GitHub) if no
    peer has it.

    Cost shape: each unhit-then-hit lookup is one TLS handshake
    + one round-trip on the LAN, ~tens of ms per image on a quiet
    Wi-Fi. For a 1700-image prefetch where peer A has all the
    bytes and peer B is the requester, that's ~30 s of LAN work
    vs. minutes-to-hours of upstream cellular download. The win
    grows on slower upstream / metered links.

    The "two peers prefetch in parallel from cold" case isn't the
    target — both peers' caches are still empty so neither can
    serve the other. The case this fixes is "second peer arrives
    after first finished" or "first peer is online, second peer
    has a metered link."

    Quietly returns None on any failure: this is an optional
    optimization step before the GitHub fetch.

    ``rel_path`` is the path inside the repo. We send it through
    to the peer verbatim; a nested rel_path
    (``0001_body/foo.png``) disambiguates the same-basename-
    different-variant case, while a flat basename gets
    canonicalized via the receiving daemon's index (same
    fallback as ``get_image_path``).
    """
    try:
        from . import peer_id as _peer_id
        from . import peers as _peers
        from . import lan_discovery as _lan_discovery
    except ImportError:
        return None
    # Our own identity — needed for body-auth on the request.
    try:
        ident = _peer_id.ensure()
    except RuntimeError:
        return None
    our_peer_id = ident.get('peer_id', '')
    our_fp = ident.get('fp', '')
    if not our_peer_id or not our_fp:
        return None
    # ``repo`` is the ``<owner>/<name>`` slug for CAWL repos.
    if '/' not in repo:
        return None
    owner, name = repo.split('/', 1)
    # Send the full rel_path. Flat basenames work too (the
    # listener canonicalizes via its index), but a nested rel_path
    # disambiguates the same-basename-different-variant case —
    # ``0001_body/foo.png`` and ``0002_other/foo.png`` would
    # otherwise both flatten to ``foo.png`` and the listener could
    # only return one.
    if (not rel_path or '..' in rel_path
            or rel_path.startswith('.')
            or rel_path.startswith('/')):
        return None
    paired = []
    try:
        paired = list(_peers.list_peers() or [])
    except Exception:
        return None
    if not paired:
        return None
    body_json = json.dumps({
        'peer_id': our_peer_id,
        'fp': our_fp,
        'owner': owner,
        'repo': name,
        'rel_path': rel_path,
    }).encode('utf-8')
    for entry in paired:
        peer_id = entry.get('peer_id', '')
        expected_fp = entry.get('fp', '')
        if not peer_id or not expected_fp:
            continue
        endpoint = _lan_discovery.get_endpoint(peer_id)
        if endpoint is None:
            # Static endpoint fallback — same shape as
            # lan_clone._resolve_endpoint.
            for source in ('static_endpoints', 'endpoints'):
                for raw in (entry.get(source) or []):
                    try:
                        h, p = raw.rsplit(':', 1)
                        endpoint = (h, int(p))
                        break
                    except (ValueError, TypeError):
                        continue
                if endpoint is not None:
                    break
        if endpoint is None:
            continue
        host, port = endpoint
        bytes_or_none = _post_lan_cawl_fetch(
            host, port, expected_fp, body_json)
        if bytes_or_none is not None:
            print(f'[cawl] LAN-peer cache hit: {peer_id[:8]!r} '
                  f'served {rel_path!r} '
                  f'({len(bytes_or_none)} bytes)',
                  file=sys.stderr, flush=True)
            return bytes_or_none
    return None


def _post_lan_cawl_fetch(host, port, expected_fp, body_json):
    """POST to a peer's ``/v1/lan/cawl_fetch`` endpoint. Returns
    the response bytes on 200, ``None`` on 404 / connection
    failure / TLS mismatch / any other error.

    Same TLS-pinning shape as ``lan_clone._build_pool_manager``
    and ``lan_push._build_ssl_context`` (we trust the peer's
    self-signed cert by fingerprint, not by CA chain)."""
    try:
        from . import peer_id as _peer_id
    except ImportError:
        return None
    cert_path = _peer_id.cert_path()
    key_path = _peer_id.key_path()
    if not cert_path or not key_path:
        return None
    try:
        import ssl as _ssl
        import urllib3 as _urllib3
        ctx = _ssl._create_unverified_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        pm = _urllib3.PoolManager(
            ssl_context=ctx,
            assert_hostname=False,
            assert_fingerprint=expected_fp,
            cert_reqs='CERT_NONE',
            timeout=_urllib3.Timeout(connect=2.0, read=15.0),
            retries=False,
        )
        resp = pm.request(
            'POST',
            f'https://{host}:{int(port)}/v1/lan/cawl_fetch',
            body=body_json,
            headers={'Content-Type': 'application/json'})
        if resp.status == 200:
            return resp.data
        return None
    except Exception:
        return None


def _fetch_image_bytes_from_github(repo, rel_path):
    """Pull a single image's bytes from
    ``raw.githubusercontent.com``. Returns the bytes on success;
    raises on transport failure.

    ``rel_path`` may include forward slashes between path
    components (e.g. ``0001_body/foo.png``); each component is
    URL-encoded for the raw URL (slashes between components are
    preserved; spaces, commas, and other unsafe characters are
    percent-encoded). The repo slug itself never contains
    unsafe characters, so it doesn't need encoding."""
    _ensure_ssl()
    from urllib.parse import quote as _urlquote
    # ``safe='/'`` — only slashes between components are safe. The
    # ``%`` character is NOT safe here, even though it might look
    # like it's already URL-encoding: the canonical CAWL repo has
    # filenames that literally contain ``%20`` as part of the
    # filename (not as URL encoding for a space). Those files'
    # paths come back from ``Uri.getPath()`` URL-decoded once, so
    # the Python-side ``rel_path`` has literal ``%20`` as part of
    # the filename. To fetch via HTTP, that ``%`` must be encoded
    # to ``%25``, producing ``%2520`` in the URL — which GitHub
    # decodes once back to literal ``%20`` and matches the file on
    # disk. With ``safe='/%'`` (the previous attempt at being
    # idempotent), the literal ``%20`` would survive unencoded into
    # the URL, GitHub would decode it as a space, and the lookup
    # would 404. So we encode aggressively here and trust that
    # peers pass us literal-character paths (which they get for
    # free from URI decoding).
    encoded_path = _urlquote(rel_path, safe='/')
    url = _RAW_URL_TEMPLATE.format(repo=repo, path=encoded_path)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read()


def _resolve_image_target(repo, rel_path):
    """Compose the absolute cache path for an image and verify
    it stays under the repo's images directory.

    Belt-and-braces against path-traversal: ``_looks_safe_rel_path``
    is the structural check; this is the realpath-based
    containment check that catches symlink tricks or any
    structural-check gap that creeps in later. Returns the
    absolute target path on success, ``None`` on containment
    failure."""
    base = os.path.realpath(
        os.path.join(_repo_cache_dir(repo), 'images'))
    target = os.path.realpath(
        os.path.join(_repo_cache_dir(repo), 'images', rel_path))
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    return target


def _resolve_basename_via_index(repo, rel_path):
    """Try to resolve a flat basename to its canonical (possibly
    nested) index path. Returns ``(resolved_path, found)``:

    - ``resolved_path``: canonical path from the index when the
      basename matched, otherwise ``rel_path`` unchanged.
    - ``found``: True iff an index entry's basename equals
      ``rel_path``.

    The ``found`` flag exists to distinguish "matched but path
    equals basename" (a legitimate root-level file in the repo,
    e.g. ``Image-Not-Found.png`` at the top of
    ``kent-rasmussen/images_CAWL``) from "no entry matched".
    Pre-fix the function returned the same string in both cases
    and the caller couldn't tell them apart — every fetch of a
    root-level image logged ``flat basename not in index`` even
    though the index had it and the fetch then succeeded. Field
    log 2026-05-18 showed the spurious line with no follow-up
    ``image fetch failed`` confirming the asset was present.

    Why this resolver exists at all: peers may extract a flat
    basename from a CAWL identifier and pass it here, but the
    canonical image may live under a category subdir
    (``0001_body/<basename>.png``). The index already records
    full paths from the GitHub tree; using it as a basename →
    full-path resolver bridges the gap without forcing every
    peer to track the category prefix.

    No fresh fetch — we only consult the on-disk cache /
    bundled seed (whichever ``_read_cached_index`` returns).
    Non-flat input and missing-cache cases return
    ``(rel_path, False)``; the caller's existing
    ``_read_cached_index(repo) is not None`` gate handles the
    distinguishing logic for the "no cache" vs "cache but no
    match" log decision."""
    if '/' in rel_path:
        return rel_path, False
    cached = _read_cached_index(repo)
    if cached is None:
        return rel_path, False
    files = cached.get('files') or []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        full = entry.get('path')
        if isinstance(full, str) and os.path.basename(full) == rel_path:
            return full, True
    return rel_path, False


def get_image_path(repo, rel_path):
    """Return ``(absolute filesystem path, source)`` for the cached
    image bytes for ``(repo, rel_path)``, fetching from GitHub or
    a paired LAN peer if not yet cached. ``rel_path`` may be a
    flat filename (``cawl-1234.png``) or a nested path
    (``0001_body/foo.png``) — CAWL repos commonly nest. A flat
    basename that matches an index entry's basename is
    automatically resolved to that entry's full nested path (see
    ``_resolve_basename_via_index``).

    *source* is one of:

    - ``'cache'`` — bytes were already on disk.
    - ``'lan'`` — fetched from a paired LAN peer's cache.
    - ``'upstream'`` — fetched from GitHub.
    - ``''`` — only when *target* is ``None`` (no bytes produced).

    By construction, ``target is not None`` implies a non-empty
    *source*. The 0.50.30 refactor moved source-counter bumping
    out of this function — callers (the prefetch worker, the
    on-demand HTTP / ContentProvider handlers) now do the bump
    explicitly via ``_bump_source_counter(repo, source)`` so the
    "completed without source" drift the pre-0.50.30 inline-bump
    pattern silently allowed becomes impossible.

    Returns ``(None, '')`` if:

    - ``repo`` is empty.
    - ``rel_path`` is malformed (path-traversal attempt; absolute
      path; backslashes; empty components).
    - The containment check fails (symlink trick, etc.).
    - The fetch fails and no prior cached copy exists.

    Once the file exists on disk, subsequent calls are O(stat) —
    no network. The fetch is lock-coalesced per
    ``(repo, rel_path)`` so two peers asking for the same image
    at the same time produce one round-trip."""
    import sys
    original_rel_path = rel_path
    repo = (repo or '').strip()
    if not repo:
        print(f'[cawl] get_image_path: empty repo for '
              f'rel_path={original_rel_path!r}',
              file=sys.stderr, flush=True)
        return None, ''
    if not _looks_safe_rel_path(rel_path):
        print(f'[cawl] get_image_path: unsafe input rel_path='
              f'{original_rel_path!r}',
              file=sys.stderr, flush=True)
        return None, ''
    # Canonicalize flat basename → nested path via the index,
    # so subsequent operations (cache target, fetch URL) all
    # use the path GitHub actually has the file at. Already-
    # nested paths pass through unchanged — no log needed for
    # the common case where the peer sends the canonical path
    # straight through (which is what the prefetch worker does
    # and what stage-2 peers do).
    if '/' not in rel_path:
        rel_path, found_in_index = _resolve_basename_via_index(
            repo, rel_path)
        if rel_path != original_rel_path:
            print(f'[cawl] get_image_path: resolved basename '
                  f'{original_rel_path!r} → {rel_path!r}',
                  file=sys.stderr, flush=True)
        elif not found_in_index and _read_cached_index(repo) is not None:
            # Peer sent a flat basename, we tried the index, no
            # match. Log because this is a real "not found in
            # index" case the peer may want to know about.
            # (A root-level file in the repo — index path equals
            # the basename — hits ``found_in_index=True`` with
            # ``rel_path`` unchanged, and is NOT logged here.)
            print(f'[cawl] get_image_path: flat basename not in '
                  f'index: {original_rel_path!r}',
                  file=sys.stderr, flush=True)
    if not _looks_safe_rel_path(rel_path):
        print(f'[cawl] get_image_path: post-resolve unsafe '
              f'rel_path={rel_path!r}',
              file=sys.stderr, flush=True)
        return None, ''
    target = _resolve_image_target(repo, rel_path)
    if target is None:
        return None, ''
    if os.path.isfile(target):
        return target, 'cache'
    with _lock_for(target):
        if os.path.isfile(target):
            return target, 'cache'
        # NOTES #3 (0.50.14): before paying for an upstream
        # round-trip, ask paired LAN peers. Quietly returns None
        # if no paired peer has the byte cached or LAN isn't
        # available; the GitHub fetch is unchanged.
        data = _fetch_image_bytes_from_lan_peer(repo, rel_path)
        source = 'lan' if data is not None else ''
        if data is None:
            try:
                data = _fetch_image_bytes_from_github(repo, rel_path)
                source = 'upstream'
            except (urllib.error.URLError, OSError, TimeoutError,
                    http.client.HTTPException) as ex:
                # ``http.client.HTTPException`` covers ``InvalidURL``
                # (raised when ``_validate_path`` sees control chars
                # in the URL — e.g., literal spaces from an un-encoded
                # filename) and other http.client-level errors that
                # don't extend OSError, which urllib's do_open
                # wouldn't otherwise wrap in URLError.
                # Log every failure verbosely — the peer-side circuit
                # breaker handles spam suppression after N consecutive
                # failures, so the daemon doesn't need its own coalescer.
                # A silent daemon-side backoff actively hurt diagnosis
                # (0.41.4-0.41.7): when the daemon went silent it was
                # impossible to tell from the peer side whether the
                # fetch had been attempted at all.
                print(f'[cawl] image fetch failed for '
                      f'{repo!r}/{rel_path!r}: '
                      f'{type(ex).__name__}: {ex}',
                      file=sys.stderr, flush=True)
                return None, ''
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f'{target}.tmp.{os.getpid()}'
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, target)
        except OSError as ex:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            import sys
            print(f'[cawl] image cache write failed for '
                  f'{target!r}: {type(ex).__name__}: {ex}',
                  file=sys.stderr, flush=True)
            return None, ''
        return target, source
