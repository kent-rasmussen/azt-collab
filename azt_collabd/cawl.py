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
import threading
import time
import urllib.error
import urllib.request

from . import config as _config
from .net import _ensure_ssl
from . import projects as _projects
from .paths import azt_home


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


def _make_prefetch_state(requested):
    return {
        'requested': requested,
        'completed': 0,
        'failed': 0,
        'started_at': time.time(),
        'finished': False,
        'finished_at': None,
    }


def _prefetch_worker(repo, paths):
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
    its existing ``[cawl] image fetch failed`` path."""
    for path in paths:
        target = get_image_path(repo, path)
        with _cache_status_lock:
            state = _prefetch_state.get(repo)
            if state is None:
                # State got nuked (daemon shutting down, or a new
                # prefetch replaced ours). Bail.
                return
            if target is not None:
                state['completed'] += 1
            else:
                state['failed'] += 1
    with _cache_status_lock:
        state = _prefetch_state.get(repo)
        if state is not None:
            state['finished'] = True
            state['finished_at'] = time.time()


def start_prefetch(repo, paths):
    """Kick off a background prefetch of *paths* for *repo*. The
    daemon's worker iterates the list and warms the cache; peers
    poll ``cache_status`` for progress.

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
    requested = len(paths)
    with _cache_status_lock:
        existing = _prefetch_state.get(repo)
        if (existing is not None
                and existing.get('requested') == requested
                and not existing.get('finished')):
            return dict(existing)
        # Replace state. The previous worker (if any) will notice
        # the change on its next loop iteration via the state-
        # identity check and exit. Worst case: it bumps a counter
        # on the new state once before exiting; harmless.
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
        target=_prefetch_worker, args=(repo, paths),
        name=f'cawl-prefetch-{repo}', daemon=True)
    _prefetch_threads[repo] = t
    t.start()
    return snapshot


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
    """Return ``(cached_count, total_count)`` for the image cache
    of *repo*.

    If a daemon-driven prefetch is active or completed for this
    repo, the counts come from that job's state — the peer asked
    the daemon to warm a specific working set, so progress
    against *that* set is what's meaningful. Banner becomes
    "M completed of N requested" and ends at 100% by
    construction.

    Otherwise (no prefetch ever started for this repo this
    daemon-session), the fallback semantics are "files on disk
    vs. all image-shaped entries in the index". That total is a
    structural over-count vs. what most peers actually warm
    (the canonical CAWL repo has 2-4 variants per identifier,
    peers typically use one); the banner plateaus at the peer's
    working-set size, not the full index. Daemon-driven prefetch
    (via ``start_prefetch``) is the way to get an accurate
    progress bar.

    Returns ``(0, 0)`` when the repo is empty or the index isn't
    loaded. After the first call per repo, polling cost is
    effectively zero — dict lookups, no I/O."""
    repo = (repo or '').strip()
    if not repo:
        return 0, 0
    pf = get_prefetch_state(repo)
    if pf is not None:
        # Active or completed prefetch — its numbers are
        # authoritative for the indicator. "completed + failed"
        # accounts for paths where the daemon resolved the
        # request (either cached or returned a hit), even if
        # individual fetches failed. We surface only "completed"
        # for the banner so failures stay visible as "stuck
        # short of total" rather than "100% with silent
        # failures".
        return pf['completed'], pf['requested']
    return _walk_image_count(repo), _count_index_images(repo)


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
    """If ``rel_path`` is a flat basename, look it up in the
    repo's cached index and return the canonical (possibly
    nested) path. If already nested, or not in the index,
    return ``rel_path`` unchanged.

    Why this exists: peers may extract a flat basename from a
    CAWL identifier and pass it here, but the canonical image
    may live under a category subdir
    (``0001_body/<basename>.png``). The index already records
    full paths from the GitHub tree; using it as a basename →
    full-path resolver bridges the gap without forcing every
    peer to track the category prefix.

    No fresh fetch — we only consult the on-disk cache /
    bundled seed (whichever ``_read_cached_index`` returns).
    If the index isn't cached yet, we return ``rel_path`` as-is
    and let the network fetch attempt 404 honestly."""
    if '/' in rel_path:
        return rel_path
    cached = _read_cached_index(repo)
    if cached is None:
        return rel_path
    files = cached.get('files') or []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        full = entry.get('path')
        if isinstance(full, str) and os.path.basename(full) == rel_path:
            return full
    return rel_path


def get_image_path(repo, rel_path):
    """Return an absolute filesystem path to the cached image
    bytes for ``(repo, rel_path)``, fetching from GitHub if not
    yet cached. ``rel_path`` may be a flat filename
    (``cawl-1234.png``) or a nested path
    (``0001_body/foo.png``) — CAWL repos commonly nest. A flat
    basename that matches an index entry's basename is
    automatically resolved to that entry's full nested path
    (see ``_resolve_basename_via_index``).

    Returns ``None`` if:

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
        return None
    if not _looks_safe_rel_path(rel_path):
        print(f'[cawl] get_image_path: unsafe input rel_path='
              f'{original_rel_path!r}',
              file=sys.stderr, flush=True)
        return None
    # Canonicalize flat basename → nested path via the index,
    # so subsequent operations (cache target, fetch URL) all
    # use the path GitHub actually has the file at. Already-
    # nested paths pass through unchanged — no log needed for
    # the common case where the peer sends the canonical path
    # straight through (which is what the prefetch worker does
    # and what stage-2 peers do).
    if '/' not in rel_path:
        rel_path = _resolve_basename_via_index(repo, rel_path)
        if rel_path != original_rel_path:
            print(f'[cawl] get_image_path: resolved basename '
                  f'{original_rel_path!r} → {rel_path!r}',
                  file=sys.stderr, flush=True)
        elif _read_cached_index(repo) is not None:
            # Peer sent a flat basename, we tried the index, no
            # match. Log because this is a real "not found in
            # index" case the peer may want to know about.
            print(f'[cawl] get_image_path: flat basename not in '
                  f'index: {original_rel_path!r}',
                  file=sys.stderr, flush=True)
    if not _looks_safe_rel_path(rel_path):
        print(f'[cawl] get_image_path: post-resolve unsafe '
              f'rel_path={rel_path!r}',
              file=sys.stderr, flush=True)
        return None
    target = _resolve_image_target(repo, rel_path)
    if target is None:
        return None
    if os.path.isfile(target):
        return target
    with _lock_for(target):
        if os.path.isfile(target):
            return target
        try:
            data = _fetch_image_bytes_from_github(repo, rel_path)
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
            return None
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
            return None
        return target
