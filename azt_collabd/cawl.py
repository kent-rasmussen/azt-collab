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

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import config as _config
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


def index_path(repo):
    """Canonical on-disk location of the cached index payload for
    a given image repo."""
    return os.path.join(_repo_cache_dir(repo), 'index.json')


def image_path(repo, basename):
    """Canonical on-disk location of a cached image binary."""
    return os.path.join(_repo_cache_dir(repo), 'images', basename)


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
    url = _GITHUB_TREE_URL.format(repo=repo)
    req = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
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
        files.append({
            'path': path,
            'url': _RAW_URL_TEMPLATE.format(repo=repo, path=path),
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
                TimeoutError) as ex:
            import sys
            print(f'[cawl] index refresh failed for {repo!r}: '
                  f'{type(ex).__name__}: {ex}; '
                  f'serving cached={cached is not None}',
                  file=sys.stderr, flush=True)
            return cached if cached is not None else {}
        _write_cached_index(repo, fresh)
        return fresh


# ── Image binaries ───────────────────────────────────────────────────────


def _looks_safe_basename(basename):
    """Reject anything that could escape the per-repo images dir
    via path-component tricks (``/``, ``..``, leading dots,
    empty). The basename is supplied by peers, so it's
    untrusted input despite arriving by URI."""
    if not basename or not isinstance(basename, str):
        return False
    if '/' in basename or '\\' in basename:
        return False
    if basename in ('.', '..'):
        return False
    # Permit hidden files like ``.gitkeep`` — those aren't a
    # security issue, just a CAWL-naming concern. Repo
    # maintainers don't ship images named that way.
    return True


def _fetch_image_bytes_from_github(repo, basename):
    """Pull a single image's bytes from
    ``raw.githubusercontent.com``. Returns the bytes on success;
    raises on transport failure."""
    url = _RAW_URL_TEMPLATE.format(repo=repo, path=basename)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read()


def get_image_path(repo, basename):
    """Return an absolute filesystem path to the cached image
    bytes for ``(repo, basename)``, fetching from GitHub if not
    yet cached. Returns ``None`` if:

    - ``repo`` is empty.
    - ``basename`` looks unsafe (path-traversal attempt).
    - The fetch fails and no prior cached copy exists.

    Once the file exists on disk, subsequent calls are O(stat) —
    no network. The fetch is lock-coalesced per
    ``(repo, basename)`` so two peers asking for the same image
    at the same time produce one round-trip."""
    repo = (repo or '').strip()
    if not repo:
        return None
    if not _looks_safe_basename(basename):
        return None
    target = image_path(repo, basename)
    if os.path.isfile(target):
        return target
    with _lock_for(target):
        if os.path.isfile(target):
            return target
        try:
            data = _fetch_image_bytes_from_github(repo, basename)
        except (urllib.error.URLError, OSError, TimeoutError) as ex:
            import sys
            print(f'[cawl] image fetch failed for '
                  f'{repo!r}/{basename!r}: '
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
