# CAWL index seeds

Bundled seed assets for the daemon's CAWL index cache. The seed
mechanism (`azt_collabd/cawl.py: _seed_index_if_bundled`) copies
one of these files into `$AZT_HOME/cawl/<owner>/<repo>/index.json`
the first time the daemon is asked for an index it doesn't yet
have on disk — closing the install-day-no-network gap that
Stages 1+2 of the CAWL daemon-side migration couldn't solve on
their own (a freshly-installed device has no cache and no
network → no images).

## Layout

One subdirectory tree per image repo, mirroring the on-disk
cache layout the daemon uses:

```
azt_collabd/data/cawl/
    <owner>/<repo>/
        index.json
```

The slug used for the directory must exactly match the
`cawl_image_repo` value the daemon resolves for projects that
should benefit from this seed (the per-project field, or the
daemon-global fallback set via
`azt_collabd.configure(cawl_image_repo=…)` or the
`AZT_CAWL_IMAGE_REPO` env var). Different repo slug → seed
silently isn't used; first launch still has to hit GitHub.

## index.json shape

Identical to the daemon-cache shape served by
`GET /v1/projects/<lang>/cawl/index`:

```json
{
    "repo": "<owner>/<repo>",
    "branch": "HEAD",
    "fetched_at": 1715520000,
    "files": [
        {"path": "cawl-1234.jpg",
         "url":  "https://raw.githubusercontent.com/<owner>/<repo>/HEAD/cawl-1234.jpg"},
        {"path": "cawl-5678.png",
         "url":  "https://raw.githubusercontent.com/<owner>/<repo>/HEAD/cawl-5678.png"}
    ]
}
```

`fetched_at` should be the unix-seconds timestamp at which the
seed was generated (i.e. when the maintainer last ran the
fetch). The TTL (`_INDEX_TTL_SECONDS` in `cawl.py`, 24h by
default) is applied against this; if the device has been online
since the build, the daemon refreshes from GitHub and overwrites
this seed with current data.

## Generation

Use `generate_seed.py` in this directory. The simplest case
refreshes the canonical suite seed — whatever the daemon-global
default resolves to (single source of truth:
`azt_collabd/config.py:_CAWL_IMAGE_REPO_DEFAULT`, read via
`config.cawl_image_repo()`):

```bash
python azt_collabd/data/cawl/generate_seed.py
```

Or pass an explicit slug for a fork / non-canonical image set:

```bash
python azt_collabd/data/cawl/generate_seed.py owner/repo
# or via env var (matches the daemon's runtime config knob)
AZT_CAWL_IMAGE_REPO=owner/repo \
    python azt_collabd/data/cawl/generate_seed.py
```

Resolution order: argv → `AZT_CAWL_IMAGE_REPO` env var →
`config.cawl_image_repo()` default. The script's first stderr
line echoes the resolved slug so you can confirm without
re-checking the default.

The script uses the same `cawl._fetch_index_from_github`
codepath the daemon does at runtime, so the seed's wire shape
is guaranteed to match what `get_index` would otherwise have
fetched live. Writes to `azt_collabd/data/cawl/<owner>/<repo>/
index.json` and prints the file count + byte count so you can
sanity-check the result.

Re-run periodically — each release cut is a natural cadence —
so the seed doesn't drift too far past the daemon's 24h TTL on
install-day devices. The script hits the rate-limited
`api.github.com` endpoint (60/hour/IP unauthenticated); don't
loop it.

## What NOT to bundle

**Image binaries.** A 100–300 MB payload per APK release is the
wrong trade — slow install, mobile-data hostile, and the
daemon-side lazy cache covers steady-state image fetching
without bundling. The cache populates lazily on first image
render once the user has connectivity; subsequent peers /
sessions read from the daemon cache with no GitHub round-trip.
See `azt_collab_client/NOTES_TO_DAEMON.md` for the 2026-05-12
decision.

## Build configuration

The seed file is picked up by buildozer because
`server_apk/buildozer.spec` lists `json` in
`source.include_exts`. Adding new seed directories under here
needs no further build-config change.

On desktop installs, the seed lives inside the Python package
directory and is read via `importlib.resources` —
filesystem-portable across Linux / macOS / Windows without
special handling.
