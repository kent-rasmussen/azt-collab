# CAWL image access rationale

> **Conformity contract** — peer call shape, migration checklist
> — is in `CLIENT_INTEGRATION.md` §§ 10 + 11. This file is the
> *why*.

CAWL (the image set + URL index) is **suite-scoped**: identical
for every project, peer, device on a given install. The
suite's three resource buckets:

- **Project-scoped** (LIFT, audio, project images) — daemon
  owns, peers consume via provider URIs.
- **Suite-scoped** (CAWL index + image binaries) — also
  daemon-owned. One-daemon-per-device gives the dedup for free.
- **Peer-scoped** (UI state, in-memory render caches) —
  peer-owned. Only category that belongs in peer `filesDir`.

Pre-0.37 CAWL was peer-scoped, which produced (1) GitHub
rate-limit exhaustion, (2) N peers × ~100–300 MB on-disk
duplication with no cross-peer sharing on Android, (3) no
install-day-no-network bootstrap. Daemon ownership fixes
all three. Cache: `$AZT_HOME/cawl/<owner>/<repo>/{index.json,
images/<basename>}`, keyed by repo slug — N projects sharing
one image_repo share one cache dir.

**Per-project `cawl_image_repo`.** Resolution chain: per-project
override → daemon-global default (`_CAWL_IMAGE_REPO_DEFAULT` in
`azt_collabd/config.py`) → empty. Different projects can
legitimately point at different image sets (fork, culturally
specific, internal mirror).

**Index seeded in the APK; binaries are not.** APK ships
`azt_collabd/data/cawl/<owner>/<repo>/index.json` (~50 KB) so
install-day-no-network devices have something to serve. Image
binaries (1701 × 50–200 KB ≈ 100–300 MB per release) are
explicitly NOT bundled — daemon-side lazy cache covers the
steady state. Closed decision (2026-05-12); refuse re-litigation.

**Daemon-driven prefetch + offline-aware (0.41.21).** Daemon's
`_touch_project` calls `cawl.auto_prefetch(repo)` (throttled
30s/repo), warms the full index in a background thread.
`_prefetch_worker` gates on `_has_internet()` at start and
circuit-breaks after 3 consecutive failures; `cache_status`
exposes both flags so peers render "offline — will resume"
rather than "0/N". `cawl.on_online_edge()` hooks the scheduler's
existing connectivity watcher so recovery rides the same
single-authority signal (30s latency at default poll). Peer-side
poll stays at 1 Hz even offline so the banner observes the
next status flip; per-poll cost is negligible.

**Migration was two-stage** (peer index → daemon; peer binaries
→ daemon; then trigger ownership flip). All stages shipped by
0.41.21. Historic detail in CHANGELOG 0.37, 0.38, 0.41.11,
0.41.21.
