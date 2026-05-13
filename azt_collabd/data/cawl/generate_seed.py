#!/usr/bin/env python3
"""Generate / refresh the CAWL index seed for a given image repo.

Run from the azt-collab repo root::

    # explicit repo
    python azt_collabd/data/cawl/generate_seed.py SIL-CAWL/cawl-images

    # or via env var (matches the daemon's runtime configuration knob)
    AZT_CAWL_IMAGE_REPO=SIL-CAWL/cawl-images \\
        python azt_collabd/data/cawl/generate_seed.py

The script:

1. Resolves ``owner/repo`` from argv or ``AZT_CAWL_IMAGE_REPO``.
2. Hits GitHub's tree-listing endpoint via the same code path the
   daemon uses (``azt_collabd.cawl._fetch_index_from_github``) so
   the seed's wire shape is guaranteed to match what
   ``get_index`` would otherwise have to fetch live.
3. Writes the result to
   ``azt_collabd/data/cawl/<owner>/<repo>/index.json``.
4. Prints the byte count and ``fetched_at`` so the maintainer can
   confirm the seed is current.

When to re-run:

- Each release cut (natural cadence — keeps the seed from drifting
  too far past the daemon's 24h TTL on install day).
- Whenever the image repo gets new entries the maintainer wants
  available on install-day-no-network devices.
- Never as part of automated CI — the script's purpose is to
  produce a reproducible, dated artifact checked into the repo,
  not a build-time fetch.

Rate limit awareness: this script hits the same
``api.github.com/repos/<repo>/git/trees/HEAD?recursive=1``
endpoint subject to the 60/hour/IP cap. Don't loop it.
"""

import json
import os
import sys

# Allow `python azt_collabd/data/cawl/generate_seed.py …` from the
# repo root by ensuring the repo root is on sys.path before we
# import the package. (Without this, the script would only run via
# `python -m azt_collabd.data.cawl.generate_seed`, which is
# cumbersome enough to be a regression vs. the one-liner this
# replaces.)
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from azt_collabd import cawl  # noqa: E402


def _resolve_repo(argv):
    """Pick ``owner/repo`` from argv → env var → daemon-global
    default (in that priority). Exits with a helpful message
    only if all three are empty, which shouldn't happen unless
    a fork has explicitly cleared the default.

    Why three sources: argv is for ad-hoc generation of a
    non-canonical seed (e.g. a fork's image set); env var matches
    how the recorder configures the daemon at runtime, so the
    same shell env that runs the suite can run this script with
    no extra ceremony; the default falls through to the
    canonical SIL CAWL repo
    (``azt_collabd.config._CAWL_IMAGE_REPO_DEFAULT``)."""
    if len(argv) >= 2:
        repo = argv[1].strip()
        if repo:
            return repo
    env = (os.environ.get('AZT_CAWL_IMAGE_REPO') or '').strip()
    if env:
        return env
    from azt_collabd import config as _cfg
    default = (_cfg.cawl_image_repo() or '').strip()
    if default:
        return default
    print(
        'error: no repo specified and no daemon-global default.\n'
        '\n'
        'Pass owner/repo as the first argument, or set the\n'
        'AZT_CAWL_IMAGE_REPO environment variable, or restore\n'
        'the default in azt_collabd/config.py'
        ' (_CAWL_IMAGE_REPO_DEFAULT). Example:\n'
        '\n'
        '    python azt_collabd/data/cawl/generate_seed.py '
        '<owner>/<repo>\n',
        file=sys.stderr)
    sys.exit(2)


def _seed_target_path(repo):
    """Where the generated seed lands. Mirrors the on-disk cache
    layout the daemon uses (``$AZT_HOME/cawl/<owner>/<repo>/
    index.json``) so the seed mechanism's path lookup picks it up
    without any further config."""
    owner, _, name = repo.partition('/')
    return os.path.join(_HERE, owner, name, 'index.json')


def main(argv):
    repo = _resolve_repo(argv)
    if '/' not in repo:
        print(f"error: {repo!r} is not an owner/repo slug.",
              file=sys.stderr)
        sys.exit(2)
    print(f'fetching CAWL index for {repo}…', file=sys.stderr)
    try:
        payload = cawl._fetch_index_from_github(repo)
    except Exception as ex:
        print(f'error: fetch failed: {type(ex).__name__}: {ex}',
              file=sys.stderr)
        sys.exit(1)
    target = _seed_target_path(repo)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    serialised = json.dumps(payload, indent=2, sort_keys=True)
    tmp = target + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(serialised)
        f.write('\n')
    os.replace(tmp, target)
    n_files = len(payload.get('files') or [])
    print(
        f'wrote {target}\n'
        f'  fetched_at: {payload["fetched_at"]}\n'
        f'  files:      {n_files}\n'
        f'  bytes:      {len(serialised)}',
        file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
