# Changelog

Two packages live here. Versions move together for now (the client
embeds `MIN_SERVER_VERSION`, so when the wire format changes we bump
both); patch-level bumps in one without the other are fine.

- **azt_collabd** — server / daemon. Source of truth: `azt_collabd.__version__` (re-imported by `server.py` as `_VERSION` for the wire response).
- **azt_collab_client** — client library. Source of truth: `azt_collab_client.__version__`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely.

## 0.44.13 — positive cache for system-resolver hits (Starlink DNS round-trip elimination)

### Why

`azt_collabd/net.py` already had a DoH fallback (Cloudflare 1.1.1.1)
for *failed* system DNS — the "Cameroon" fix. But the fallback only
fires when ``socket.getaddrinfo`` raises ``gaierror``. On Starlink,
where system DNS works but each lookup is slow (satellite RTT +
distant resolver placement = multi-second per query), the fallback
never engages and we pay full DNS cost on every connection.

Counted against the field log: a typical drain through chunk-halving
(50 → 25 → 12 → 6 → 3 → 1, then chunk_n=1 retries) opens ~14
connections to ``github.com:443`` (2 per attempt: ``GET info/refs``
and ``POST git-receive-pack``), each preceded by a fresh system DNS
call. The periodic ``_has_internet()`` probe at
``sync.connectivity_poll_s`` (default 30 s) adds 2 more lookups per
tick. On a slow resolver every one of those is satellite-RTT.

### What landed

- ``azt_collabd/net.py``: ``_SYSTEM_CACHE`` dict + lock, 5-min TTL
  (matches DoH cache). Keyed on ``(host, port)``; populated on
  successful system-resolver returns; checked before calling
  ``_orig_getaddrinfo``. Hostname-shaped only (gated on
  ``_looks_like_hostname``) — numeric IPs / AF_UNIX paths fall
  through to the original ~O(1) path.
- New ``_RESOLVER_STATE`` value ``'system-cache'`` for cache hits.
  ``resolver_state()`` docstring updated.

### Impact

During a sync session, ``github.com`` resolves at most once per 5
min instead of ~14+ times per drain. Eliminates 13+ system-DNS
round-trips per drain pass on slow resolvers. Effect on the baf
tester's 408 pattern: TBD — DNS is one of several suspects
(server-side budget, dulwich-on-Android pump, TLS instability). If
the 408 was DNS-eating-the-budget, this fixes it; if not, we'll see
the 408s persist with the same shape and need to keep looking.

### Wire format

None. Pure internal optimisation; no new endpoints, no new
status codes, no MIN_CLIENT_VERSION bump.

## 0.44.12 — Phase A persistence gate + commit-time large-file flag + clearer bail message

### Why

0.44.11 shipped a single size-based gate (bail when the chunk_n=1
pack > 10 MB). Field logs from the baf tester on Starlink showed
the gate was the wrong shape: chunk_n=1 packs as small as 276 KB
still 408'd at GitHub's receive-pack endpoint in ~20–30 s,
consistently, indefinitely. The 10 MB gate never fires for a tiny
pack like that — so the daemon spun all the way to
MAX_CONSECUTIVE_FAILURES (12) on every drain, burning ~10 min per
cycle for nothing. And the "single commit too big for your
connection" wording in the bail message was actively misleading
on a fast pipe.

### What landed

- **Persistence gate.** ``_push_chunked_to_ref`` now tracks
  ``chunk_n_1_failures``. After the *second* chunk_n=1 failure
  (regardless of pack size), bail with
  ``S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET``. This catches the
  "server keeps rejecting our pushes for reasons we can't see"
  case the field showed.
- **Default size-gate budget lowered** from 10 MB to 3 MB
  (``sync.commit_pack_byte_budget``). The original 10 MB was
  calibrated on a guess; field data shows even ~7 MB packs 408
  reliably on observed connections, so 10 MB rarely fired even
  when it should have. 3 MB still leaves room for a healthy
  single-commit pack on a working pipe but trips faster on
  observably-bad ones. (The size gate fires on the *first*
  chunk_n=1 failure when bytes > budget; the persistence gate
  fires on the *second* failure regardless.)
- **Bail message rewritten.** The old wording said "single
  commit is too large for this connection" — wrong on Starlink
  where tiny packs also fail. New message: "Could not push to
  GitHub: the server kept rejecting our push attempts
  ({commit_sha}, {raw_bytes:,} bytes). May be a connection
  problem or a GitHub-side issue — try again later or on a
  different network." The ``Status`` params now carry
  ``reason='oversize'|'exhausted'`` so future UI / log
  consumers can differentiate.
- **Data-quality flag at commit time.** ``_commit_step_locked``
  now scans every just-made commit for files above
  ``data_quality.large_audio_byte_threshold`` (default 500 KB)
  and emits ``S.LARGE_AUDIO_FILE_DETECTED`` plus a
  ``[data-quality]`` stderr line per offender. The suite
  recorder is for word-list elicitation; multi-MB files almost
  always mean a phrase / text was recorded by mistake.
  Informational — doesn't block the commit; daemon log becomes
  the audit trail.
- **Helpers.** ``_check_large_files_in_commit(repo, commit_sha,
  threshold)`` walks ``dulwich.diff_tree.tree_changes`` for the
  new commit vs its first parent.
- **Classifier script.** ``tools/classify_pending.py`` — offline
  per-commit stats (files / total bytes / max-file) for an
  existing diverged range. Useful for triaging an already-stuck
  repo (run against a desktop clone with the full pending
  history).
- **Translations** for the new bail wording and
  ``LARGE_AUDIO_FILE_DETECTED`` (English + French).

### Wire format

Additive only — new status code ``LARGE_AUDIO_FILE_DETECTED``
and new ``reason`` param on ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET``.
Old peers fall through to the generic ``[CODE] params``
formatter or ignore the new param. No ``MIN_CLIENT_VERSION``
bump.

### What this doesn't fix

The actual cause of baf's stuck push remains unknown — the
server-side 408s at ~20–30 s on packs of any size, including
276 KB, on a fast Starlink pipe. The persistence gate just
gives up faster and tells the user something honest. Real fixes
require diagnosing whether dulwich-on-Android, GitHub's edge,
or something in between is the culprit (see "Next test"
discussion below in CHANGELOG conversation).

## 0.44.11 — Phase A pre-flight pack-size diagnostic + first-cut budget bail

### Why

Field log from a 0.44.10 tester (baf, ~150 MB / 424-commit backlog)
showed Phase A chunk-halving running all the way down to
``chunk_n=1`` and still getting HTTP 408 from
``git-receive-pack`` at ~29 s per attempt — i.e. the bytes that
need to cross the wire for a *single* commit don't fit inside the
server's per-request timeout on this connection. The architecture
(chunk-halving to fit pack size into per-request budget) bottoms
out at 1 commit per chunk; there's no smaller unit to fall back
to. Before 0.44.11 this just spun on ``MAX_CONSECUTIVE_FAILURES``
with no indication to the user that the problem isn't going to
fix itself by waiting.

This release added a pre-flight pack-size estimate to every Phase A
attempt (traced) and a typed
``S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` bail at chunk_n=1 when
the single-commit pack exceeded the per-attempt budget
(initially 10 MB). See 0.44.12 for the follow-up persistence
gate + budget retune after field evidence showed the size-only
gate was insufficient.

### What landed

- ``azt_collabd/repo.py``: ``_estimate_delta_size(repo, have_sha,
  want_sha)`` — walks ``dulwich.object_store.MissingObjectFinder``
  and sums ``raw_length()`` over the missing-objects set. One
  call per Phase A attempt, traced as
  ``[sync-trace] topic-push pack-size: N objects, X bytes``.
- ``_push_chunked_to_ref`` adds a size-only bail at chunk_n=1
  when ``raw_bytes > commit_pack_byte_budget()``.
- ``_push_step_locked`` handles the new status code alongside
  ``TOPIC_BRANCH_CONFLICT``.
- ``azt_collabd/settings.py``: new ``commit_pack_byte_budget()``
  (initially 10 MB; retuned to 3 MB in 0.44.12).
- ``azt_collabd/status.py`` + ``azt_collab_client/status.py``:
  ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` mirrored.
- ``azt_collab_client/translate.py``: initial bail message
  (replaced in 0.44.12 once field evidence showed the
  "too large for this connection" wording was wrong), plus the
  previously-missing entry for ``TOPIC_BRANCH_CONFLICT``.
- French translations.

### Wire format

Additive status code. No ``MIN_CLIENT_VERSION`` bump.

## 0.44.10 — topic-branch Phase D + startup janitor: clean up merged topic-branches

### Why

After 0.44.9 (Phases A + B + C), the topic-branch
`azt-pending-<lang>-<device>` is left on the server even after
the merge commit lands on `main`. Cosmetic clutter, not a
functional problem — but accumulates over time, and a few
versions of 0.44.x already left orphans on test repos. Phase D
deletes the ref on the success path; the janitor cleans up
stragglers (Phase D delete that didn't fire, daemon kill right
after Phase C, etc.).

### Phase D — delete topic-branch on Phase C success

After `S.PUSHED` is added in Phase C's success branch, call
`_delete_remote_topic_branch(repo, remote_url, username,
token, topic_ref_name)` which pushes a delete refspec
(`':refs/heads/<topic>'`) and drops the local mirror. Failure
is non-fatal; logged but doesn't block the sync result. Worst
case: orphan ref on the server, picked up by the janitor on the
next startup.

### Janitor — once-per-project-per-daemon-lifetime sweep

`_maybe_run_janitor(repo, project_dir, username, token,
remote_url, branch)` runs right after the fetch in
`_push_step_locked`. Memoized in `_JANITOR_SWEPT_PROJECTS` so
the sweep network cost is one-time per (project, daemon
lifetime); in steady state (Phase D ran on the last success)
the sweep finds zero refs and is essentially free.

Conservative scope:

- **Only our own device's refs.** Suffix match on the
  sanitised device_name. Other devices' orphans stay; their
  owning device's next sync sweeps them. Refusing to touch
  anyone else's ref avoids false-positive deletes — if Device
  B is mid-Phase-A from a checkpoint we don't know about,
  deleting their topic-branch would force them to restart.
- **Only merged-into-main refs.** Reachability from
  `refs/remotes/origin/<branch>` is git's safety contract for
  "every commit on this ref is also reachable from main, so
  dropping it loses nothing." The check uses our existing
  `_is_ancestor(topic_tip, main_tip)`.

### Files touched

- `azt_collabd/repo.py`:
  - `_delete_remote_topic_branch(...)` helper — Phase D's
    delete primitive.
  - `_janitor_sweep_topic_branches(...)` — the actual sweep
    loop over our own refs.
  - `_maybe_run_janitor(...)` — idempotent wrapper memoized
    by project_dir.
  - `_JANITOR_SWEPT_PROJECTS` module-level set.
  - Wired `_delete_remote_topic_branch` into Phase C's
    success branch.
  - Wired `_maybe_run_janitor` into `_push_step_locked` right
    after `remote_sha` is read (so the janitor has a valid
    main tip to validate ancestry against).

### Field-tester migration path (unchanged)

Same flow as 0.44.9 — Phase A chunks, Phase B re-checks, Phase
C promotes. Now Phase D also deletes the topic-branch on
success, and any orphan from earlier 0.44.x test builds gets
swept on the next daemon spawn (visible in the log as
`[sync-trace] janitor: sweeping N merged topic-branch(es)`).

## 0.44.9 — topic-branch Phases B + C: explicit re-fetch / re-merge / promote loop

### Why

0.44.8 shipped Phase A (chunked push to a per-device topic-branch)
and relied on the existing direct-push loop to handle Phase B
(re-fetch + conditional re-merge) and Phase C (promote merge
commit to main) implicitly. That works for the common no-race
case and for one bounded race (where the existing post-non-FF
retry re-merges once inside the direct-push loop), but has a
hole: if a re-merge inside the direct-push loop produces another
non-FF state, chunk-halving spins on `DivergedBranches`. Also
the implicit path doesn't re-enter Phase A after a re-merge —
it just retries the direct push — so a re-merge during a flaky
window could leave the merge stuck.

This release adds explicit Phase B and Phase C as a bounded
loop, replacing the "fall through to direct-push" behaviour
after a successful Phase A.

### Phase B — re-fetch + conditional re-merge

After Phase A returns success:

1. Re-fetch `origin` (auth errors short-circuit; transient
   network failures continue with stale local mirror).
2. Read the local mirror of `refs/remotes/origin/<branch>`.
3. If unchanged from when we started the run → skip to Phase C.
4. If changed:
   - **New remote is reachable from our `local_sha`**: our
     existing merge already includes the remote's new tip; no
     re-merge needed. Proceed to Phase C.
   - **Our `local_sha` is reachable from the new remote**:
     remote advanced past us during Phase A (e.g., someone else
     merged our changes upstream). Fast-forward local; surface
     `S.PULLED`; return — nothing to push.
   - **Diverged again**: re-run `_merge_diverged` against the
     new remote tip. Memory pre-flight check
     (`_check_memory_for_merge`) runs first; refuses with
     `S.INSUFFICIENT_MEMORY_FOR_MERGE` if low. New merge commit
     becomes `local_sha`. Conflicts surface as before.

### Phase C — promote merge commit to main

After Phase B leaves us with the right `local_sha`:

1. Set `refs/heads/<branch>` to `local_sha` defensively.
2. `porcelain.push(remote_url, '<branch>:<branch>')`.
   Pack negotiation sees the topic-branch ref Phase A
   populated + `main` + any other server refs; excludes every
   reachable object. The pack contains only the merge commit
   + tree + merged LIFT bytes (a few MB) and completes inside
   the server's per-request timeout.
3. On success: advance local mirror of `refs/remotes/origin/<branch>`,
   clear stale chunk_n hints, emit `S.PUSHED`, return.
4. On `_is_non_ff_rejection`: main moved between Phase B and
   Phase C (sub-second race window). Loop back to Phase B —
   re-fetch, re-evaluate, re-push.
5. On 401 / 403: surface auth-related status, return.
6. On other (network / server transient): emit `S.PUSH_FAILED`;
   next drain re-runs Phase A (which short-circuits on
   already-uploaded objects via the server's topic-branch ref)
   then Phase B + C.

### Bounded loop

Phase B + Phase C run in a loop with `MAX_PROMOTE_RETRIES = 5`
iterations. If main keeps moving under us through every retry
(extremely hot race window), we bail with `S.PUSH_FAILED`;
next drain tries again. The Phase A short-circuit on resume
means cost-of-retry across drains is small.

### Files touched

- `azt_collabd/repo.py`: replaced the "fall through to existing
  direct-push loop" comment-and-trace after Phase A success
  with the explicit Phase B + C loop. ~150 lines of new code
  inside `_push_step_locked`. The existing direct-push loop
  below is unchanged and remains the path for pure-FF cases
  (the `can_direct_push == True` branch).

### Field-tester migration path (unchanged from 0.44.8)

Same as 0.44.8 — Phase A pushes diverged history to
`azt-pending-baf-<device>` in chunks, Phase B detects whether
main moved (won't have, on the tester's repo), Phase C pushes
the existing `c115b64c` merge commit to main as a tiny pack.
Difference vs. 0.44.8: if main *did* move during Phase A's
multi-minute upload, this release handles it cleanly with the
explicit B + C loop instead of relying on the direct-push
loop's after-the-fact handling.

## 0.44.8 — topic-branch Phase A: chunked upload of diverged history

### Why

0.44.7 added the routing decision; this release acts on it. When
`_all_commits_descend_from(remote, local)` returns False — the
typical post-merge state where the local-side parent chain of the
merge commit doesn't descend from the current remote — direct push
+ chunk-halving can't help (every intermediate the chunk picker
selects gets `DivergedBranches` from the server). Phase A
sidesteps the topology problem by pushing diverged commits to a
per-device topic-branch first; the audio blobs land in 5–20 MB
chunks that fit inside GitHub's per-request timeout. Once all
chunks are on the server (under the topic-branch ref), the
existing direct-push loop runs immediately after and pushes the
merge commit to `main` — pack negotiation excludes everything
already on the server (via any ref, including topic-branch), so
the main push contains only the merge commit + tree + merged
LIFT bytes. That pack completes in seconds.

### What landed

New helpers in `azt_collabd/repo.py`:

- `_topic_branch_name(langcode, device_name)` — returns
  `azt-pending-<sanitized-lang>-<sanitized-device>`. Per-device
  naming so two devices syncing the same project simultaneously
  don't clobber each other's topic ref; per-project so one device
  working on multiple LIFT projects keeps them separate.
- `_push_chunked_to_ref(repo, project_dir, username, token,
  remote_url, target_sha, topic_ref_name, branch_for_main)` —
  adaptive chunked push to the topic-branch. Reads
  `refs/remotes/origin/<topic_ref_name>` (populated by the
  earlier fetch) for the server-side tip and resumes from there
  if a prior attempt got partway. Refuses with
  `S.TOPIC_BRANCH_CONFLICT` if the server's topic-branch tip
  isn't an ancestor of our target (another device using the same
  device_name). Reuses the existing chunk-halving heuristic on
  per-chunk network failure. No DivergedBranches handling needed
  — topic-branch is ours alone by naming convention.

New status codes:

- `S.TOPIC_BRANCH_CONFLICT` (`azt_collabd/status.py` +
  `azt_collab_client/status.py` mirror). Params:
  `topic_branch`, `server_tip`. User remedy: change device_name
  to something unique in the daemon settings UI.

New settings knob:

- `sync.topic_branch_chunk_size` (default 50) — initial chunk_n
  for Phase A. Lower for slower networks. Halves adaptively on
  per-chunk failure.

Wiring in `_push_step_locked`:

- After the merge handling, when the routing decision says
  topic-branch, invoke `_push_chunked_to_ref` with
  `target_sha=local_sha`. On success, fall through to the
  existing direct-push loop — that push of the merge commit to
  main now negotiates a tiny pack since the server already has
  every reachable object.
- On `S.TOPIC_BRANCH_CONFLICT`: surface the status + add
  `PUSH_FAILED` for peer routing; return.
- On consecutive-failures-cap exit: surface
  `S.SYNC_GIVING_UP_TRANSIENT` + `S.PUSH_FAILED`; next drain
  cycle re-reads the server's topic-branch tip and resumes.

### How resume works (no on-disk state)

The server's topic-branch ref IS the progress record. Each
successful chunk push lands on the server; on the next drain the
existing fetch repopulates `refs/remotes/origin/<topic>` and
`_push_chunked_to_ref` picks up where the last run left off
without any local state file. If the daemon was killed
mid-chunk-push, the chunk that was in flight either landed (and
the server now has it) or didn't (no partial commit, since git's
push is atomic per refspec). Either way, the next drain reads the
authoritative server-side tip and walks forward from there.

### Migration / current stuck tester

The stuck baf tester (424 commits ahead of remote, ~150 MB pack
timing out at GitHub's ~7-minute per-request ceiling) should
unstick on next sync after installing 0.44.8:

1. Routing detects non-FF (the 422 pre-merge commits don't
   descend from current remote `main`) → topic-branch route.
2. Phase A pushes the merge commit `c115b64c` to
   `azt-pending-baf-<device>` in chunks of ~50 commits each.
   Each chunk uploads ~10–25 MB. Wall-clock for the full
   sequence: tens of minutes on a slow network, but each
   individual upload survives the per-request timeout.
3. Existing direct-push runs after Phase A returns. Pushes
   `c115b64c` to `refs/heads/main`. Pack contains only the merge
   commit + tree (no audio blobs — those are now reachable via
   the topic-branch ref). Completes in seconds.

The merge commit they already produced (with 1700
azt-lift-conflict annotations) lands on main unchanged. Topic
branch stays on the server temporarily; janitor (step 5,
deferred to a later release) cleans it up.

### What's not in this release

Per the spec (step 4 of "Implementation order"):

- **Phase B** (re-fetch + conditional re-merge if `main` moved
  during the long Phase A upload) — for now handled implicitly
  by the existing post-non-FF retry inside the direct-push loop.
  Race window: if `main` moved during Phase A and the post-Phase
  direct push gets `DivergedBranches`, the existing code does a
  re-merge inside the direct-push loop. Works but doesn't
  re-enter Phase A if the re-merge produces another non-FF
  state.
- **Phase D** (cleanup — delete topic-branch ref via dulwich's
  delete-refspec push). Leaving the topic ref on the server is
  cosmetic clutter, not functional. Janitor will sweep these on
  a future release.
- **Step 6** UI status emission (`S.UPLOADING_IN_PIECES` with
  per-chunk progress). Phase A's progress is visible in
  `[sync-trace] topic-push attempt …` lines in the daemon log
  for now.

### Files touched

- `azt_collabd/repo.py`: `_topic_branch_name`,
  `_push_chunked_to_ref`, routing wiring in `_push_step_locked`.
- `azt_collabd/settings.py`: `topic_branch_chunk_size()`.
- `azt_collabd/status.py` + `azt_collab_client/status.py`:
  `TOPIC_BRANCH_CONFLICT`.

## 0.44.7 — push-routing diagnostic (step 1 of topic-branch push)

### Why

Field user 2026-05-21 is stuck pushing a 424-commit post-merge state to GitHub: the merge commit is one atomic git object, the 681 new audio blobs referenced by the merge can only travel as one ~150 MB pack, and GitHub's `git-receive-pack` is timing out at ~7 minutes before the upload completes. Chunk-halving against `main` can't help because every intermediate the chunk picker selects (one of the ~422 pre-merge commits on the local-side parent chain of the merge commit) doesn't descend from the current remote `main` — `DivergedBranches` at every smaller chunk.

The proper architectural fix is to push diverged commits to a topic branch first (the audio blobs land there in small chunks, each upload survives GitHub's per-request timeout), then promote the existing merge commit to `main` as a tiny pack since all blobs are already on the server. The local LIFT-aware merge stays on the device, unchanged.

That implementation lands in stages. This release is **step 1**: the routing decision, observable in the trace but not yet acted on.

### What changed

New helper `_all_commits_descend_from(repo, ancestor_sha, descendant_sha)` in `azt_collabd/repo.py`. O(N) one-pass walk of the delta between ancestor and descendant; returns True iff every commit in the delta has the ancestor as one of its ancestors. False if the delta touches a third branch (typical: post-merge state where the local-side parent chain of the merge commit doesn't descend from the current remote).

New trace line in `_push_step_locked` just before the existing push loop:

```
[sync-trace] route: direct-push (diagnostic — current build still takes direct path)
[sync-trace] route: topic-branch (diagnostic — current build still takes direct path)
```

No behavior change. The existing direct-push + chunk-halving path runs for every push as before. The trace lets us verify on real field data that the routing rule correctly identifies non-FF states (the stuck tester's next sync should log `topic-branch`) before Phase A is added in the next release.

### Files touched

- `azt_collabd/repo.py`: added `_all_commits_descend_from` next to `_is_ancestor`; added the diagnostic trace at the entry to the push loop.

### What ships next (step 2+)

Tracked in the topic-branch spec laid out in conversation. Phase A (chunked push to `refs/heads/azt-pending-<lang>-<device>`) is the next code drop. Then Phase B (re-fetch + conditional re-merge), Phase C (promote merge commit to `main` — tiny pack since blobs already on server), Phase D (cleanup). Sync-state persistence in `$AZT_HOME/sync_state.json` for resume across daemon respawns.

## 0.44.6 — low-memory device politeness pass: three deferred OOM headroom fixes

### Why

The 0.44.4 audit (`project_oom_followups_after_0.44.4.md`) found
three sibling OOM-prone patterns beyond the `_walk_tree` /
`_merge_diverged` fix that already shipped in 0.44.4. None were
on the critical path of the field user's stuck-merge symptom, so
they were deferred to this release. Each closes a small amount
of unnecessary RAM pressure that becomes painful on tight-heap
Android devices when stacked with other concurrent work.

### Fix 1 — `atomic_recovery._reconcile_orphan` gates on memory

`azt_collabd/atomic_recovery.py:_recover_under_lock` calls
`lift_merge.three_way_merge` to reconcile orphan-pending LIFT
files. Same ~150 MB peak as `_merge_diverged`. It runs from
`reconcile_on_startup` — exactly when memory may be tight (the
daemon process is just spawning, competing with the picker
activity for heap). Silent OOM-kill there would lose the entire
recovery batch with no signal.

Imports `_check_memory_for_merge` from `.repo` and refuses the
merge when free memory is below `sync.min_free_mem_mb_for_merge`.
On refusal:

- Orphan stays on disk (still valid; the file is byte-complete)
- New `summary['skipped_low_memory']` counter increments
- Returns from `_recover_under_lock` early — if we can't fit
  this merge, we won't fit the next either, no point burning
  time on a doomed loop

Next startup with more memory available runs the recovery.

### Fix 2 — `_h_atomic_commit` streams SHA-256 + byte count

`azt_collabd/server.py:_h_atomic_commit` was reading the entire
pending audio file (3–10 MB) into a Python bytes object just to
compute `len(data)` and `hashlib.sha256(data).hexdigest()` for
the `ATOMIC_COMMITTED` response. Replaced with the canonical
streaming-hash pattern:

```python
h = hashlib.sha256()
bytes_written = 0
with open(pending_real, 'rb') as f:
    for chunk in iter(lambda: f.read(64 * 1024), b''):
        h.update(chunk)
        bytes_written += len(chunk)
```

Peak heap: 64 KB instead of file size. Minor on its own, but
stacks badly when concurrent with a LIFT merge or push pack-
build — those can already push the heap close to its cap on
low-end devices.

### Fix 3 — loopback HTTP body cap

`azt_collabd/server.py:_read_json` was reading the full
`Content-Length` from `rfile` with no upper bound. Desktop-
only (Android peers use ContentProvider), so not Android-OOM-
relevant, but a buggy peer / test harness that sends a wrong
`Content-Length` would let the daemon allocate gigabytes
before the JSON parse failed. Added a 64 MB cap — far past
any legitimate body on this path (largest legit is a credential
blob, <1 KB) — that returns a quick refusal instead.

### Files touched

- `azt_collabd/atomic_recovery.py`: imported
  `_check_memory_for_merge` from `.repo`; added pre-flight at
  the `three_way_merge` call site; new `skipped_low_memory`
  counter on `summary`.
- `azt_collabd/server.py`: streaming hash in
  `_h_atomic_commit`; 64 MB `_MAX_BODY_BYTES` cap in
  `_read_json`.

### Not yet wired (intentional)

Defense-in-depth equivalents on the loopback transport for
other large-body endpoints (e.g. credential upload) — those
already inherit the new `_read_json` cap since they share the
helper. The 64 MB cap can be tightened later if any legitimate
path approaches it; current ceiling is the credential blob at
under 1 KB, so headroom is ~64,000× what's actually needed.

## 0.44.5 — self-update no longer locks out users with crowded Downloads folders

### Why

Field user 2026-05-21: in-app Update failed with `Install failed:
JVM exception occurred: failed to build unique file:
/storage/.../aztcollab.apk`. The user's Downloads folder had
accumulated 30+ orphan `aztcollab.apk` / `aztcollab (1).apk` /
… `aztcollab (32).apk` entries from prior install attempts,
hitting Android's `MediaProvider.buildUniqueFile()` retry cap.
Once the cap is hit there's no recovery from the Update button
— the user's only option was to open the Files app and manually
delete all those orphans.

### Fix — `azt_collab_client/ui/update.py:_media_store_uri`

Two layers of defence:

1. **Pre-insert cleanup.** New `_clear_prior_downloads()` queries
   MediaStore Downloads for rows whose `_display_name` matches
   the asset name (`aztcollab.apk`) and that this app owns, then
   deletes each. Scoped-storage permissions limit us to our own
   rows — which is exactly what we want, since the orphans are
   from our own prior install attempts. Other apps' files of the
   same name stay put.

2. **Timestamped fallback.** If the canonical-name insert still
   fails (other apps own enough same-named files to push us over
   the cap, or the scoped-storage delete refused), retry once
   with a UTC-timestamped display name
   (`aztcollab-20260521-145922.apk`). The install Intent only
   needs the `content://` URI; the system installer reads the
   APK manifest for the user-facing app identity, so the
   timestamped filename never appears in any UI.

Logs `[update] cleared N prior MediaStore Downloads row(s)
named X` when cleanup happens; logs the fallback case explicitly
when the timestamped name is used.

### Manual recovery for users already locked out

Users who are stuck on the pre-0.44.5 build can self-recover
without any rebuild:

1. Open the Files app
2. Navigate to Internal storage → Download
3. Delete every `aztcollab.apk` / `aztcollab (N).apk`
4. Re-tap Update

Then they pick up 0.44.5+ via the in-app updater and the issue
doesn't recur.

### Files touched

- `azt_collab_client/ui/update.py`: added
  `_clear_prior_downloads`; refactored `_media_store_uri` to
  pre-clear + retry with timestamped name.

## 0.44.4 — `_merge_diverged` no longer slurps every audio file into Python (OOM-kill on merge fixed); pre-flight memory check

### Why

Field log baf 2026-05-21 15:45 → 15:52 (after 0.44.3 was
installed): every drain cycle did the right thing — fetched,
saw remote had moved to `7a0e17`, logged `merge_diverged
begin` — and then the daemon process died silently at 49 s,
65 s, 127 s mid-merge with no traceback. The shrinking
intervals (127 → 65 → 49) were the giveaway: heap-fragmentation
accelerating until the OOM-killer caught a smaller spike. The
respawn boot logs never showed `reconcile_on_startup:
interrupted=N` either, because nothing in the merge path was
job-tracked.

Root cause: `_walk_tree` in `azt_collabd/repo.py` returned
`path → blob.data (bytes)` for **every file** in a commit tree.
`_merge_diverged` called it three times (base, head, remote) at
the top of the function. For a 1700-entry baf project with
audio per entry, each side pulled ~200 MB of audio bytes into a
Python dict, totalling ~600 MB peak before the LIFT XML parse
even started. Android's `:provider` service heap cap (~256–512
MB depending on device) couldn't fit that, so the process was
OOM-killed before any merge work happened. Same bug existed in
the fast-forward path (`_apply_tree_to_workdir`) — that's now
fixed by the same change.

### Fix — SHA-only tree walk, lazy byte loading

- `_walk_tree` returns `dict[path → blob sha (bytes)]` instead of
  `dict[path → blob.data]`. ~tens of KB per snapshot regardless
  of audio file count, vs hundreds of MB before.
- New `_blob_bytes(repo, sha)` helper resolves a single blob to
  bytes on demand.
- `_merge_diverged` compares by SHA (git's invariant: identical
  content → identical SHA), loads bytes only when:
  - About to call `lift_merge.three_way_merge` on a `.lift`
    file (then `del b_bytes, o_bytes, t_bytes` immediately after
    the merge returns so the peak is "one LIFT merge worth",
    not "every file in every snapshot")
  - About to write a path whose target SHA differs from the
    HEAD SHA (skips the no-op rewrite of files whose content
    didn't change — most of the working tree)
- `_apply_tree_to_workdir` (fast-forward path) gets the same
  treatment.

Peak memory budget after the change:

- 3 × `_walk_tree` SHA dicts: ~150 KB
- LIFT XML parse + merge state (the actual cost): ~100–150 MB
- One blob read at a time during apply: max ~1 MB (largest
  audio file)
- **Total: ~150–200 MB**, comfortably under Android's heap cap.

### Pre-flight memory check (defensive)

Even with the slurp gone, the LIFT XML parse + merge step still
wants ~150 MB peak. On a device where another app is hogging
RAM the merge could still OOM. Added a pre-flight check that
reads `MemAvailable` from `/proc/meminfo` and refuses the merge
if it's below `sync.min_free_mem_mb_for_merge` (default 200
MB). Returns a typed `S.INSUFFICIENT_MEMORY_FOR_MERGE` with
`mem_available_mb` and `min_required_mb` params; the next drain
cycle re-reads memory and proceeds when it recovers. Beats the
silent OOM-kill: the user (and the log) get a clear "not
enough memory right now, will retry" instead of a process
disappearance.

All three `_merge_diverged` call sites are gated:
- `sync_repo` needs_merge path
- Forced-merge after non-FF-no-progress
- Retry-merge after race-on-fetch

On non-Linux platforms `/proc/meminfo` isn't readable; the check
treats that as passing (returns None). Desktop / sandbox doesn't
OOM-kill the way Android's `:provider` does.

### New tracing

`_merge_diverged` now emits `[merge-trace] _walk_tree done
base=N head=N remote=N`, then `[merge-trace] resolution done
writes=N deletes=N conflicts=N`, then `[merge-trace] apply done
writes_done=N deletes=N`. The pre-0.44.4 log shape was just
`merge_diverged begin` followed by silence — impossible to tell
whether the death was during walk, resolution, or write.

### Files touched

- `azt_collabd/repo.py`: rewrote `_walk_tree`, added
  `_blob_bytes`, `_mem_available_mb`, `_check_memory_for_merge`;
  refactored `_merge_diverged` body around (kind, value) tuples
  in `merged_writes`; gated all three `_merge_diverged` call
  sites on the memory check.
- `azt_collabd/status.py` + `azt_collab_client/status.py`:
  added `INSUFFICIENT_MEMORY_FOR_MERGE` (with mirrored comment
  on the client side documenting the routing contract).
- `azt_collab_client/translate.py`: translation for the new
  code with `{mem_available_mb}` / `{min_required_mb}` params.
- `azt_collabd/settings.py`: `min_free_mem_mb_for_merge()`
  knob, default 200 MB.
- `azt_collab_client/CLIENT_INTEGRATION.md` §17: new row in
  the routing table (silent in auto-sync, translated toast in
  user-initiated sync), the silence list in the auto-commit
  example and the transient-toast branch in the
  user-initiated example both updated, constants list extended.

### Tuning notes

`sync.min_free_mem_mb_for_merge` is conservatively set at 200
MB. If a device is consistently triggering the pre-flight
refusal but the merge would actually succeed (post-fix it
should fit in ~150 MB), drop to 150 via
`AZT_HOME/config.json`. Setting to 0 disables the check
entirely.

## 0.44.3 — persisted chunk_n hint now uses the smallest attempted, not the last (in-call revert no longer wipes progress)

### Why

0.44.2 introduced cross-drain hint persistence but the within-call
"revert to full local tip" path defeated it. Field log baf 2026-
05-21 14:31 → 14:58 showed the loop:

```
14:45:22 resuming with hint chunk_n=211     ← good, hint worked
14:45:23 push raised: DivergedBranches at 211
14:45:25 non-FF — reverting to full local tip
14:45:25 push attempt chunk_n=422           ← back to full
14:58:15 408 timeout at chunk_n=422
14:58:15 remembered chunk_n=422             ← LAST chunk_n, not smallest
         next drain cycle will start at 211 ← back where we began
```

Result: indefinite loop at hint=211, since the budget always
caught the post-revert full attempt and persisted that value.
Net progress across cycles: zero.

### Fix

Track `smallest_attempted_n` across the entire `_push_step_locked`
call. On budget-exceeded or consecutive-failures-cap exit, persist
**that** value instead of the current `chunk_n`. Each cycle then
halves the actual floor, not the post-revert ceiling.

Expected behavior for the stuck device:

```
Cycle 1: try 422 → budget → smallest=422 → store 211
Cycle 2: hint=211 → try 211 → DivergedBranches → revert to 422 →
         budget at 422 → smallest=211 → store 105
Cycle 3: hint=105 → try 105 → … → smallest=105 → store 52
… halves about every drain cycle (5-15 min each) until a chunk
  fits the network window.
```

Each cycle makes real progress now, regardless of whether the
within-call revert kicks in.

### Files

- `azt_collabd/repo.py` — added `smallest_attempted_n` tracking;
  used in both budget-exceeded and consecutive-failures-cap
  persist sites.

### What still doesn't fix itself

The within-call "non-FF with no remote movement — reverting to
full local tip" escalation still re-enlarges chunk_n inside a
single call, burning the rest of the budget on a doomed full-tip
attempt. That's wasted time per cycle but no longer wasted
progress across cycles. A future fix could cap the revert at
`smallest_attempted_n` instead of going all the way to `local_sha`,
but that touches the diagnostic-escalation contract more
substantially — defer until we see whether the across-call
convergence alone is enough.

## 0.44.2 — push loop remembers failed `chunk_n` across drain cycles so backlogs converge on slow networks

### Why

Field log baf 2026-05-21 09:38 → 11:11 (daemon 0.44.0): device
had 419 unpushed commits accumulated over ~24 hours of flaky
connectivity. Each scheduler drain cycle:

1. `push attempt target=966b6714 chunk_n=419 consecutive_failures=0`
2. ~12 minutes later: `POST .../git-receive-pack 408` (GitHub server
   gave up on the slow upload)
3. `push budget exceeded (300s) — giving up; pending commits
   requeued for next sync`
4. 30 s later: drain cycle fires again, **starts at chunk_n=419**
5. Loop repeats indefinitely; backlog grows; backlog never drains.

The 0.43.22 chunk-halving adaptive loop *does* exist — it would
bisect 419 → 209 → … → 1 inside a single call until it finds a
size the network can sustain. But each single call typically can
only do one push attempt before the 300 s budget cuts it off, and
the next call has no memory of what just failed. Every drain
cycle wastes 5–13 minutes on a chunk_n the previous cycle already
proved was too big.

(See the 0.43.18 session at 12:46–13:21 in the same log: chunk
halving 302 → 151 → 75 → 37 → 18 → 9 → 4 → 2 → 1 worked when
given uninterrupted time. The budget — necessary to free the
project lock for other clients — defeated it once it was added.)

### What

In-process `_LAST_FAILED_CHUNK_N` dict in `azt_collabd/repo.py`
keyed by `project_dir`. Three touch points:

1. **Loop preamble:** if the dict has a hint for this project, use
   `_pick_intermediate_sha(repo, remote_sha, local_sha, hint)` as
   the initial `target_sha` instead of `local_sha`. Logged as
   `[sync-trace] resuming with hint chunk_n=N`.
2. **Budget exceeded / consecutive_failures cap:** before
   returning `SYNC_GIVING_UP_TRANSIENT` / `PUSH_FAILED`, store
   `max(1, chunk_n // 2)` for this project. Next drain cycle
   picks it up via (1).
3. **Full successful push (`PUSHED`):** clear the entry. The
   network just demonstrated it can handle the current size; no
   constraint to carry forward.

Net effect for the user's stuck device:

- Cycle 1: start at chunk_n=419, budget expires at chunk_n=419 →
  store hint=209
- Cycle 2: start at chunk_n=209, budget expires → store hint=104
- Cycle 3: start at chunk_n=104, budget expires → store hint=52
- … converges on a chunk_n the network can sustain inside 300 s
- Once a push at the converged size succeeds, the loop continues
  with `working_batch_n` locked in and drains the queue.

Across daemon restarts the dict resets — that's fine, the next
cycle is at most one 300 s budget worse off than otherwise.

### Files

- `azt_collabd/repo.py` — `_LAST_FAILED_CHUNK_N` dict;
  `_hint_chunk_n`, `_remember_failed_chunk_n`,
  `_clear_failed_chunk_n` helpers; three integration points in
  `_push_step_locked`.

### Recovery for currently-stuck field user (0.43.20 with 419-commit backlog)

Their 0.43.20 has the un-budgeted halving loop (worked over ~35
minutes per push in the 0.43.18 log) and 0.44.0/0.44.1 has the
budgeted-but-non-adaptive loop (never converges). 0.44.2 is the
first version that both bounds individual attempts AND converges
across drain cycles.

```bash
adb install -r path/to/0.44.2.apk
# tap AZT Collaboration once (Activity-side bundle refresh)
# then leave the device on a reliable network for an hour
```

The drain loop will work its way down to a working chunk_n over
the course of a few cycles (5–15 minutes per cycle); once it
finds one, it locks in and drains the 419 commits over the next
~30 minutes. No user action required between install and "drained."

## 0.44.1 — service-side bundle re-extract was extracting the WRONG asset; replace with marker invalidation

### Why

0.44.0's `_extract_bundle_from_apk` extracted
`assets/private.tar.gz` into `_python_bundle/`. But
`private.tar.gz` contains **app code** (`main.py`, `service.py`),
not the Python bundle. The Python bundle (`stdlib.zip`,
`modules/`, `site-packages/`) ships in
`lib/<abi>/libpybundle.so`, which p4a's `PythonUtil.unpackPyBundle`
extracts with prefix `"pybundle"` stripped to produce
`_python_bundle/`.

End result of the wrong extract: `_python_bundle/` was
structurally present (so bootstrap.c said "exists" and
proceeded) but functionally empty — no `stdlib.zip`, no
`modules/`, no `site-packages/`. Python's interpreter init
succeeded at the C level, but `_bridge_stdio_to_logcat` failed
to import anything (no Python stdlib reachable), prints went to
`/dev/null`, process died silently. **Exactly the silent
:provider death symptom this whole 0.43.22–0.44.0 chain was
supposed to fix.** We've been chasing the wrong root cause
since 0.43.22 — every "stale unpack" fix made things worse by
overwriting `_python_bundle/` with the wrong contents.

Field log 22:59:02 shows the loop cleanly:
1. :provider boots from a pre-swap bundle → `[boot-trace-daemon]`
   lines fire, `_maybe_reextract_python_bundle` detects stale
2. `_extract_bundle_from_apk` extracts `private.tar.gz` (app
   code) into `_python_bundle.new/`, swaps, exits
3. Next :provider spawn loads from the corrupted bundle →
   silent death → cascade-kill of peer

### Fix

`_maybe_reextract_python_bundle` no longer extracts. On stale
mtime detection it:

1. **Deletes `files/app/private.version` and
   `files/app/libpybundle.version`** — the markers
   `PythonUtil.unpackAsset` and `unpackPyBundle` use to skip
   re-extract. With them gone, the next picker Activity launch
   triggers a proper re-extract.
2. **Logs a clear warning** that the bundle is stale and the
   user should open AZT Collaboration to refresh.
3. **Stamps our own marker forward** so we don't repeat the
   invalidation on every spawn.
4. **Does NOT touch `_python_bundle/`** — daemon continues with
   the existing (possibly stale) code until the picker
   refreshes things.

`_extract_bundle_from_apk` is kept in the file as a deprecated
stub with a long comment explaining why it was wrong; it has
no callers.

### Trade-off

The "peer opens before picker after `install -r`" case now
runs the daemon on stale code until the user opens AZT
Collaboration. This is **the pre-0.43.22 default behavior**.
Worse than a hypothetical "perfect" recovery, but vastly better
than a silently-corrupted bundle.

Recovery path for `install -r` is now:

1. `adb install -r bin/aztcollab.apk` (or sideload)
2. **Tap AZT Collaboration once** before opening the peer.
   PythonActivity sees the version mismatch (which
   `private_version` advances per build), runs
   `recursiveDelete(files/app/)` + extract → fresh bundle.
3. Open the peer. `:provider` lazy-spawns from the now-fresh
   bundle.

If the peer is opened first by mistake:
- `:provider` runs on stale code (functional, just not the new
  version)
- The new service.py (once loaded) will detect the mtime
  mismatch on FIRST spawn and invalidate the markers
- Next picker launch then triggers the proper extract

### Recovery for currently-stuck device

If you have the 0.44.0 broken bundle on disk:

1. Tap AZT Collaboration on the device. PythonActivity should
   detect the mismatch and run a proper extract.
2. If that fails (which would mean PythonActivity's own path
   is also broken), `adb install -r 0.44.1.apk` first, then
   tap AZT Collaboration.

### Files

- `server_apk/service.py` — `_maybe_reextract_python_bundle`
  now invalidates markers instead of extracting;
  `_extract_bundle_from_apk` deprecated with explanatory
  comment.

## 0.44.0 — stuck-bundle recovery milestone: bz2 fix + Java recovery hatch + boot diagnostic

Consolidation release. Public 0.43.27 shipped with a latent
self-perpetuating broken state (the bz2-broken stale-unpack code
from 0.43.22 onward) that left any device which `install -r`'d
between 0.43.22 and 0.43.31 unable to refresh `_python_bundle/`
without losing `$AZT_HOME`. 0.44.0 packages the full recovery
chain into one release suitable for the public update channel.

The development history is in the 0.43.32–0.43.38 entries below;
the net surface for an end user is:

- **Stale-unpack actually works on `install -r` going forward.**
  bz2 import is optional in `_extract_bundle_from_apk`; gzip
  handles the modern `private.tar.gz` path. (was 0.43.32)
- **`azt_home()` is cached.** The OpenFile ContentProvider
  callback no longer burns 3-4 JNI calls per FD serve from the
  Binder dispatch thread — that was the 15:00:12 binder-thread
  NPE class. (was 0.43.32)
- **File-based boot diagnostic.** Service.py writes phase markers
  to `<filesDir>/service_boot.log` from the very first lines of
  module load. The last line before a process dies tells us
  exactly where it died, surviving any later stdio failure.
  `faulthandler` tracebacks land in the same file. (was 0.43.35)
- **`BundleResetReceiver` (Java).** Pure-Java BroadcastReceiver
  that wipes `files/app/_python_bundle/` AND the `.version`
  markers (`private.version` + `libpybundle.version`) — loads
  from `classes.dex`, never from `_python_bundle/`, so it fires
  even when every Python entrypoint is unrunnable. Reachable via
  `adb shell am broadcast -a org.atoznback.aztcollab.RESET_PYTHON_BUNDLE
  -p org.atoznback.aztcollab`. (was 0.43.33 + 0.43.34's marker fix)
- **`RecoveryActivity` (Java, hidden).** Class declared in the
  manifest, exported, but without a LAUNCHER intent-filter — end
  users see one launcher icon. Support / future re-enablement
  reaches it via `adb shell am start -n
  org.atoznback.aztcollab/.RecoveryActivity`. (was 0.43.36)

### Hard rule reminder

`adb uninstall` and `pm clear` are NOT recovery paths — they
wipe `$AZT_HOME` (projects, recordings, credentials, jobs.json).
`adb install -r` preserves data and now (with the 0.44.0 bz2
fix) actually refreshes the bundle on every reinstall.

For currently-stuck devices, the recovery is:

```bash
adb install -r path/to/0.44.0.apk
# tap AZT Collaboration on the device
```

The build's fresh `private_version` resource string mismatches
what's on disk → PythonActivity's UnpackFilesTask re-extracts
`_python_bundle/` from the new APK assets → daemon boots from
fresh code. `$AZT_HOME` preserved throughout.

### Unfinished

The original silent `:provider` boot-death root cause is still
unidentified. The boot diagnostic in 0.44.0 catches and locates
any future occurrence; pinpointing the existing case requires a
recurrence to read the diag from.

## 0.43.36 — hide AZT Recovery launcher icon (keep the class + receiver for later)

### Why

0.43.35's RecoveryActivity unstuck the field device, but the
second launcher icon ("AZT Recovery") sitting next to the
normal "AZT Collaboration" icon is noise for the >99% of users
who never need it. Recovery surfaces should appear only when
the underlying stuck-bundle bug class returns in the wild.

### What

Removed the `<intent-filter>` (MAIN + LAUNCHER) from the
`<activity>` declaration. The `RecoveryActivity` class,
`BundleResetReceiver`, file-based boot diagnostic, and the
"Show service boot log" button all remain in the APK. End
users see one launcher icon.

Recovery is still reachable for future use:

- **Field support with adb access:**
  ```
  adb shell am start -n org.atoznback.aztcollab/.RecoveryActivity
  ```
  Activity stays `exported="true"`, same-package addressing
  works.
- **Daemon-broadcast path:**
  ```
  adb shell am broadcast -a org.atoznback.aztcollab.RESET_PYTHON_BUNDLE -p org.atoznback.aztcollab
  ```
  The receiver is unchanged — `RESET_PYTHON_BUNDLE` action
  still wipes the bundle + `.version` markers.
- **Future release re-add:** if the stuck-bundle class
  re-emerges and we want users to self-recover without
  rebuilding, a future p4a_hook.py revision can put the
  LAUNCHER intent-filter back. The Activity code itself
  doesn't change.

### Files

- `/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py` —
  removed the LAUNCHER intent-filter block from
  `_BUNDLE_RESET_RECEIVER_BLOCK` injection. Activity is now
  self-closing without any intent-filter.

## 0.43.35 — file-based boot diagnostic for `:provider` silent death + RecoveryActivity log viewer

### Why

0.43.34 successfully re-extracted `_python_bundle/` on field rebuild,
confirming the bundle-stale recovery chain works end-to-end. But
`:provider` STILL dies within 17-65 ms of spawn with no Python
output anywhere — the original cascade-kill root cause that
predated the bundle issue. Bundle was a distraction; the real bug
is that something in service.py module load aborts Python silently.

`faulthandler.enable(file=sys.stderr)` from 0.43.32 didn't help
because PythonService doesn't redirect stderr to logcat (only
PythonActivity does). The traceback was being written, just to a
file descriptor going nowhere.

Worse: service.py dies so early that `_bridge_stdio_to_logcat`
hasn't installed yet, so even ordinary `print()` calls are
invisible. Without ANY logcat output between `Run user program`
(bootstrap.c's last line) and `Python for android ended.`
(bootstrap.c's exit line), we have no signal at all.

### What

Phase markers written to a real file from the very first lines
of service.py module load. The file:

- Lives at `<filesDir>/service_boot.log` (hardcoded path; can't
  use jnius-based resolution because jnius may be the thing
  failing).
- Survives process death (line-buffered + flushed per write).
- Receives a phase marker at every checkpoint:
  `module_load_start`, `imports_done`, `path_setup_done`,
  `faulthandler_enabled`, `thread_excepthook_set`,
  `before_bridge_stdio`, `after_bridge_stdio`, then
  `boot_trace:module_loaded`, `boot_trace:main_entered`,
  `boot_trace:before_import_azt_collabd`, etc.
- Is the file `faulthandler.enable(file=...)` writes its
  SIGSEGV tracebacks into, so a native crash also lands here.
- Rotates at 100 KB (renamed to `.prev`) so the diag doesn't
  fill flash under repeated crash loops.

The **last line** of this file before a process dies tells us
EXACTLY where it died.

### Surface

`RecoveryActivity` gets a second button — "Show service boot
log" — that reads `<filesDir>/service_boot.log` (and `.prev`)
and displays the last ~200 lines in a scrollable monospace
TextView. Pure Java, doesn't depend on Python or the daemon —
works even when everything else is broken. Field users surface
the diagnostic without adb.

### Files

- **`server_apk/service.py`** — `_diag(phase)` helper at the
  top of module load (before any import that could fail), and
  `faulthandler.enable(file=_BOOT_DIAG_FD)` pointed at the diag
  file instead of stderr.
- **`android/src/main/java/.../RecoveryActivity.java`** — added
  "Show service boot log" button + scrollable display.

### Recovery procedure for current stuck device

```bash
cd server_apk && bash build.sh
adb install -r bin/aztcollab.apk
# tap AZT Collaboration on the device
# if it doesn't boot, tap AZT Recovery → "Show service boot log"
# the LAST line before each spawn died tells us where to look
```

The build-version bump alone re-extracts `_python_bundle/`
(version mismatch between disk's `private.version` and the new
APK's `private_version` string). After that, the diag file
accumulates phase markers from each :provider spawn.

## 0.43.34 — `BundleResetReceiver` also wipes `.version` markers (0.43.33's wipe wasn't enough)

### Why

0.43.33 shipped the receiver + RecoveryActivity, and field testing
confirmed both surfaces work to remove `_python_bundle/`. But
re-extract on next Activity launch **didn't fire** — UnpackFilesTask
saw the surviving `private.version` / `libpybundle.version` markers
matched the APK's `private_version` string resource and short-
circuited the extract. Bundle stayed missing forever; field-log
showed `_python_bundle does not exist...should we expect a crash
soon?` repeating across 100+ process spawns over 2 minutes.

The relevant p4a code (in `PythonUtil.unpackAsset` /
`unpackPyBundle`) keys the extract on a version comparison, not
on directory existence. Wiping the directory alone is invisible
to that path. The previous `_python_bundle/` was extracted by
the same APK build that's on disk now, so the markers match
the APK and "no extract needed."

### Fix

`BundleResetReceiver` now also deletes:
- `files/app/private.version`
- `files/app/libpybundle.version`

Both markers gone → next UnpackFilesTask reads an empty
`diskVersion`, mismatches the APK's `private_version` →
`recursiveDelete(target)` wipes any remaining files/app/ junk
→ `extractTar` lays down fresh `_python_bundle/` from the APK.

### Recovery for the currently-stuck device

Two paths land you the same place:

**Path A (rebuild-and-reinstall, easier):**

```bash
cd server_apk && bash build.sh
adb install -r bin/aztcollab.apk
# tap AZT Collaboration on the device
```

A new build's `private_version` (timestamp-based) differs from
what's on disk → UnpackFilesTask sees the mismatch → extracts
fresh `_python_bundle/`. No need to touch AZT Recovery this
time; the version mismatch alone unsticks the install.

**Path B (use 0.43.34's fixed AZT Recovery):**

```bash
adb install -r bin/aztcollab.apk
# tap AZT Recovery → Repair sync service → Close
# tap AZT Collaboration
```

The fixed receiver wipes the markers too, so even without an
intervening APK-version bump the re-extract fires.

Both paths preserve `$AZT_HOME` (projects, recordings,
credentials, daemon.log).

## 0.43.33 — Java-only `BundleResetReceiver` + `RecoveryActivity` (second launcher icon) for stuck `_python_bundle/`

### Why

0.43.32 fixed the bz2-broken stale-unpack code, so devices going
forward can self-refresh their `_python_bundle/` on `install -r`.
But 0.43.27 already shipped publicly, and any device that
`install -r`'d between 0.43.22 and 0.43.31 is stuck — the loaded
`service.py` (from the stale bundle) still has the broken bz2
import, so the 0.43.32 fix on disk can't activate. The only ways
out of the stuck state were data-destructive (`adb uninstall`,
`pm clear` — both wipe `$AZT_HOME` with the user's projects,
credentials, jobs.json). Unacceptable for field-deployed peers
that hold weeks of recording work.

### What

A pure-Java `BroadcastReceiver` that wipes
`files/app/_python_bundle/` from inside the app's UID
(satisfying Android's UID isolation) without depending on
`_python_bundle/` (satisfying the "Python is broken" failure
mode). Java classes load from the APK's `classes.dex`, never
from the extracted Python bundle, so this works even when every
Python entrypoint is unrunnable.

After the wipe, the next picker Activity launch triggers p4a's
Activity-side extract-on-missing branch, which re-extracts from
the fresh APK assets. The `:provider` service then lazy-spawns
from the now-current bundle and the daemon recovers. `$AZT_HOME`
(`files/azt/`) is **not touched** — the wipe is scoped strictly
to `files/app/_python_bundle/`.

The 0.43.12 attempt failed because it fired auto-wipe on
`MY_PACKAGE_REPLACED` and the service-side bootstrap (at that
time) couldn't re-extract on missing dir, crash-looping under
`START_STICKY`. The 0.43.33 receiver:

- Fires **only manually** via a custom action
  (`org.atoznback.aztcollab.RESET_PYTHON_BUNDLE`), never
  automatically. Never on package replace.
- Recovery flow guides the user to open the Activity (which
  *does* extract on missing) before any peer triggers a service
  spawn, so the service always boots from a present bundle.

### Two recovery surfaces

**The receiver alone isn't enough for field deployment.** Field
machines (SIL linguists' tablets) don't have adb. They need a
purely in-app path that works when the picker Activity can't
boot — which is exactly the case when this recovery is needed
(`:provider` cascade-killing the picker means the picker won't
stay open long enough for the user to find a settings button).

So 0.43.33 ships two surfaces:

1. **`BundleResetReceiver`** — fires on the custom action
   `org.atoznback.aztcollab.RESET_PYTHON_BUNDLE`. Available via
   `adb shell am broadcast` for developers on stuck-but-USB-
   connected devices, and from inside the app via
   `sendBroadcast` from `RecoveryActivity`.
2. **`RecoveryActivity`** — a **second launcher icon** labeled
   "AZT Recovery". Pure Java, no SDL, no Kivy, no Python — so
   it launches even when every Python entrypoint in the APK is
   unrunnable. UI is built programmatically (no layout XML
   resource) so no res-pipeline changes were needed. The
   "Repair sync service" button fires the same broadcast, so
   both surfaces converge on the same wipe code.

The two icons let a field user recover from a stuck install
without leaving the device — open app drawer, tap "AZT
Recovery", tap "Repair sync service", tap "Close", reopen "AZT
Collaboration" normally.

### Files

- **New:** `android/src/main/java/org/atoznback/aztcollab/BundleResetReceiver.java`
- **New:** `android/src/main/java/org/atoznback/aztcollab/RecoveryActivity.java`
- **Touched (one-time sandbox carve-out):**
  `/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py` —
  added `_inject_bundle_reset_receiver` (gated on
  `dist_name == 'aztcollab'`), which injects both the
  `<receiver>` AND the `<activity>` with `MAIN`/`LAUNCHER`
  intent-filter, mirroring the existing
  `_inject_self_replace_receiver` pattern.

### Recovery procedure (field — no adb)

1. Open the device's app drawer.
2. Tap the **AZT Recovery** icon (second launcher icon, next to
   AZT Collaboration).
3. Tap **Repair sync service**.
4. After ~1 second, tap **Close**.
5. Tap the **AZT Collaboration** launcher icon to reopen the
   picker. p4a's Activity bootstrap re-extracts `_python_bundle/`
   from the fresh APK assets.

`$AZT_HOME` (projects, recordings, credentials, jobs.json) is
preserved throughout. The wipe is scoped strictly to
`files/app/_python_bundle/`.

### Recovery procedure (developer — with adb)

```bash
adb install -r server_apk/bin/aztcollab.apk
adb shell am broadcast \
    -a org.atoznback.aztcollab.RESET_PYTHON_BUNDLE \
    -p org.atoznback.aztcollab
# then tap AZT Collaboration on the device
```

If the broadcast doesn't take the first time (Android's
broadcast queue can drop receivers during heavy backoff), wait
~30 s and re-fire. Confirm via `adb shell dumpsys activity
services org.atoznback.aztcollab` — the `Restarting services`
backoff section should empty out once `:provider` boots cleanly.

### What this *doesn't* fix

The underlying root-cause crash (the silent `:provider` death
within 90 ms of spawn that triggered the bind/unbind cascade)
hasn't been pinpointed yet — but with a fresh bundle that
includes 0.43.32's `faulthandler.enable(all_threads=True)`, any
future crash will dump a Python traceback to logcat before the
process dies. Diagnosis-blocker removed.

## 0.43.32 — stale-unpack survives missing `_bz2`; cache `azt_home()`; faulthandler for `:provider`

### Why

Two field bugs falling out of the 2026-05-20 baf logs.

**Stale-unpack always silently failed.** `_extract_bundle_from_apk`
imported `bz2` at the top, but p4a's default build doesn't include
the `_bz2` C extension, so the import raised `ModuleNotFoundError`
before any decompression ran. Every "stale marker → re-extract"
attempt failed at the import boundary; the function returned False
to the caller's `except Exception`, the daemon kept loading old
Python code from the existing `_python_bundle/`, and the user
saw "I rebuilt and the bug is still there" on every iteration.
This made the entire 0.43.22 stale-unpack mechanism a no-op for
anyone whose p4a build matched the default — i.e. everyone.

**ContentProvider `openFile` callback path was crashing
`:provider` under sustained cawl-image traffic.** Tombstone at
pid=23550 tid=23558 (binder:23550_1) showed
`art::JNI::CallObjectMethodA` NPE in the same class as the
pre-0.43.23 dispatch-thread crash. Cause: `_resolve_path`
(the openFile callback's hot path) called `azt_home()`, which
re-fired `ActivityThread.currentApplication().getFilesDir()
.getAbsolutePath()` — 3-4 JNI invocations on the Java Binder
dispatch thread per FD serve. The 0.43.23 fix moved
`_check_self_updated` off the Dispatch path; the **OpenFile path
was untouched** and burned more JNI per call than DispatchCallback
ever did.

### Changes

1. **`bz2` is now optional in `_extract_bundle_from_apk`.** Wrapped
   in try/except; if `_bz2` is missing, the bz2 decoder is omitted
   from the decompressor list and `private.tar.gz` (gzip) still
   handles the modern p4a build path. Only the legacy
   `private.mp3` (bz2-renamed-to-dodge-asset-compression) path
   requires `_bz2`, which current p4a doesn't emit.

2. **`azt_collabd/paths.py:azt_home()` caches its result.** First
   call hits jnius (or platform fallbacks); every subsequent call
   reads a module global. Cache is safe — the Android `filesDir`
   value is UID-scoped and never changes for the lifetime of a
   process. Module-level `_AZT_HOME_CACHE` documents the field-log
   incident.

3. **`faulthandler.enable(file=sys.stderr, all_threads=True)`** at
   the top of `server_apk/service.py` module load. Next SIGSEGV
   from `:provider` will dump a Python-side traceback per thread
   into logcat under tag `python` before the process dies. With
   logcat-bridged stderr, this means the actual `*.py:line` site
   of the crash is now visible without symbols for `jnius.so`.

4. **`threading.excepthook`** installed to log uncaught worker-
   thread exceptions with the thread's `name`. Background failures
   in `azt_collabd-watcher`, `commit-fire-<lang>`,
   `cawl-prefetch-<repo>`, and the new `self-update-poll*` Timer
   threads now surface to logcat instead of silently disappearing.

5. **Name the remaining unnamed `threading.Timer` instances** in
   `azt_collabd/android_cp/service.py` —
   `self-update-poll-init`, `self-update-poll`,
   `self-update-exit`. Pairs with the existing memory note that
   every daemon thread must be named so future tombstones
   self-identify.

### Files touched

- `server_apk/service.py` — `bz2` optional; `faulthandler.enable`;
  `threading.excepthook`.
- `azt_collabd/paths.py` — `azt_home()` cache.
- `azt_collabd/android_cp/service.py` — Timer naming.
- `azt_collab_client/__init__.py` — version bump to 0.43.32.

## 0.43.22 — sync push loop: stop the 35-minute hang on flaky-DNS networks

### Why

Field log baf 2026-05-20 captured the disaster cleanly. A user
tapped Sync on a metered tether; the daemon spent **35 minutes**
losing a fight that could have ended in one HTTPS request:

1. `[12:47:32] [collab.store] github refresh failed: Token refresh
   network error: <urlopen error timed out>` — the proactive token
   refresh failed at sync setup. The daemon kept the existing
   access token in play. It was about to expire.
2. DNS to github.com was flapping at ~5 % uptime — DoH AAAA+A
   timing out, system resolver dead, brief recoveries between
   long outages.
3. The push retry loop saw `NameResolutionError` and *halved
   chunk_n*: 302 → 151 → 75 → 37 → 18 → 9 → 4 → 2 → 1. Halving
   pack size doesn't fix DNS — same number of TCP connections
   needed regardless — so the loop was bisecting on the wrong
   axis.
4. When DNS finally cooperated for one request at chunk_n=1,
   the server returned **401** — the access token had gone past
   its 8 h cliff during the storm. The daemon ate the 401, kept
   trying, and only stopped when `consecutive_failures` hit 12.
5. Final codes: `['COMMITTED_LOCAL', 'PULL_FAILED', 'PUSH_FAILED',
   'AUTH_REFRESH_STALE']`. The AUTH bit was emitted post-hoc by
   `_annotate_with_auth_health`, not because the loop noticed.

A second sync attempt on the same project the same day uploaded
the pack successfully at chunk_n=20 (server reported
`DivergedBranches(old=9ee637c..., new=580e2a49...)` — meaning the
push *landed* on the server) but the read side of the connection
dropped with `IncompleteRead(0 bytes)`. The daemon reverted to
"push full local tip" instead of trusting the server's report,
re-pushed, looped through more DivergedBranches → IncompleteRead
cycles, then got OOM-killed mid-retry at the 10-minute mark.

### Changes (push loop hardening)

Five mitigations, none of which is "DNS caching" but one of which
extends the existing DoH negative cache:

1. **Pre-flight credentials probe** in `_push_step_locked`. When
   `store.github_refresh_state()['broken']` is True AND the
   remote is a GitHub URL, run `test_github_credentials(token)`
   against `api.github.com/user` (15 s cap) before entering the
   retry loop. If the token is rejected, emit `AUTH_REQUIRED` +
   return — the loop never starts. Best-effort: any exception
   in the probe falls through to the normal loop.
2. **401 short-circuit** in both fetch and push paths. `_is_http_401`
   matches both dulwich's typed `HTTPUnauthorized` and the bare
   `\b401\b` message shape. Treating 401 as a transient network
   failure (pre-0.43.22 behaviour, because nothing matched it)
   was the proximate cause of the 12-failure halving storm.
3. **DNS-class failures no longer halve** `chunk_n`. New branch
   in the retry-after-backoff section: `if
   _is_dns_resolution_failure(exc): continue` (after the back-off
   sleep, holding target + working_batch_n). When DNS recovers
   we resume on the chunk size that previously worked. Halving
   on DNS failure was pure overhead — pack size has zero effect
   on whether the resolver returns an address.
4. **Trust `DivergedBranches`'s reported remote tip.** The
   exception's `args[0]` is the server's authoritative view of
   the ref ("current_sha"); when refetch fails (e.g.
   `IncompleteRead` mid-handshake), use the rejection's reported
   SHA as `new_remote` and write it to
   `refs/remotes/origin/<branch>` so the next iteration doesn't
   re-discover it. New helper `_extract_diverged_remote(exc)`.
5. **Wall-clock budget on the push loop.** New setting
   `sync.push_budget_s` (default 300 s, env
   `AZT_SYNC_PUSH_BUDGET_S`, 0 disables). When the loop's
   monotonic elapsed time exceeds the budget on a network-class
   failure, emit the new `SYNC_GIVING_UP_TRANSIENT` status code
   carrying `budget_s` + `commits_pending` and bail with
   `PUSH_FAILED`. The pending commits stay queued; the next
   sync run picks them up. This bounds wedged sessions so the
   project lock frees for other operations.

### Changes (DoH resolver)

6. **Exponential negative-cache TTL** in `net.py`. The existing
   5 s negative cache was poisoning sustained-outage scenarios:
   every DNS lookup paid the full 2.5 s DoH round-trip, so on a
   35-minute storm the resolver alone burned ~12 minutes of
   wall time. Now consecutive failures for the same host extend
   the negative TTL exponentially: 5 s → 10 s → 20 s → 40 s →
   60 s (`_DOH_NEGATIVE_TTL_MAX_S`). A single positive resolve
   resets the counter, so a brief outage followed by reconnect
   returns to the tight 5 s probe cadence. Cache value tuple
   widened from `(expiry, records)` to
   `(expiry, records, neg_count)`; in-process so no migration.

### Changes (status + translation)

- `S.SYNC_GIVING_UP_TRANSIENT` added in both
  `azt_collabd/status.py` and the `azt_collab_client/status.py`
  mirror. Carries `budget_s` and `commits_pending` params.
- French + English translations added; matches the auto/user
  silence contract (auto-sync silences this code, user-initiated
  Sync surfaces the toast).
- `sync.push_budget_s` documented in `settings.py`.

### Files

- `azt_collabd/repo.py` — `_is_http_401`,
  `_extract_diverged_remote`, fetch 401 short-circuit, push 401
  short-circuit, pre-flight probe, DNS-no-halve branch,
  DivergedBranches authoritative-remote, wall-clock budget.
- `azt_collabd/status.py` — `SYNC_GIVING_UP_TRANSIENT`.
- `azt_collabd/settings.py` — `sync.push_budget_s` default + env
  map + `push_budget_s()` accessor.
- `azt_collabd/net.py` — exponential negative TTL.
- `azt_collab_client/__init__.py` — version 0.43.21 → 0.43.22.
- `azt_collab_client/status.py` — mirror.
- `azt_collab_client/translate.py` — English string.
- `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  — French translation.

### Restart-server button: layout + force-kill fallback

Two follow-ups on the 0.43.20 button after a field session.

- **Stacked instead of side-by-side.** French labels are too long
  to share a row at phone widths: "Partager le journal du service"
  + "Redémarrer le service" overflow the dp(52) row regardless of
  spacing. Moved Restart to its own row below Share daemon log.
  English is unaffected.
- **Bootstrap reboot-to-apply popup auto-resolves via cooperative
  restart.** `_prompt_server_reboot_to_apply` in
  `azt_collab_client/ui/bootstrap.py` fires when the peer detects
  `installed > running` on the server APK (new APK on disk, old
  daemon still in `:provider`). Pre-0.43.22 it surfaced a
  "restart your device" popup. The peer can't `Process.killProcess`
  across UID boundaries, but it CAN ask the running daemon to
  restart itself via `POST /v1/admin/restart` (shipped 0.43.20).
  Try that first; on RESTARTING re-probe compat via
  `_post_install_continuation`. Only fall through to the popup
  when the daemon is too old to know the endpoint — the genuinely-
  stuck pre-0.43.20 case where reboot is the only way out.
- **Loop guard on the cooperative restart**, attached to ``ctx``.
  Tracks attempted `(installed, running)` pairs in
  `ctx._cooperative_restart_attempts`. If `_post_install_continuation`
  re-probes and the SAME running version comes back, the restart
  didn't actually load new code — the respawned daemon re-imported
  a stale `_python_bundle/` from filesDir. Without this guard the
  cooperative restart loops forever at ~once per 2 s, burning
  battery. With it: one attempt per pair, then fall through to
  the popup. Updated popup body to explain the stale-unpack
  cause rather than blaming Android's package-replace process
  preservation.
- **Service-side stale-unpack fix** in `server_apk/service.py`.
  The bootstrap loop guard above prevents the symptomatic cycle
  but doesn't fix the underlying issue. p4a's C bootstrap
  extracts `assets/private.*` to `_python_bundle/` only when the
  directory is missing — never on APK update — so on reinstall
  the respawned `:provider` Python interpreter imports the
  previous APK's code from disk. Documented as a TODO in
  `SuiteSelfReplaceReceiver.java`'s NOTE on stale p4a unpack
  (Activity-launch-from-receiver or service-side
  extract-on-missing; this is the service-side branch).
  - `_maybe_reextract_python_bundle()` runs in `service.py:main()`
    BEFORE `import azt_collabd`. Compares the running APK's
    mtime (via `ApplicationInfo.sourceDir`) to a marker file at
    `_python_bundle/.apk_mtime`. On mismatch: re-extracts
    `assets/private.{tar.gz,tar,mp3}` to `_python_bundle.new/`,
    atomically renames the old bundle aside, swaps the new one
    in, writes the marker, and `os._exit(0)`s. Android's
    ContentProvider auto-spawn brings up a fresh `:provider`
    that imports the new code. First-launch path stamps the
    marker without re-extracting (p4a's C code already
    extracted from this APK).
  - Handles all three p4a asset names (`.tar.gz`, `.tar`,
    `.mp3` — the renamed bz2 from older p4a that dodges
    Android's auto-compression) and detects the decompressor by
    trying gzip → bz2 → plain in order.
  - Atomic swap via rename so concurrent peer calls (which can
    trigger lazy-spawn during the swap window) either see the
    pre-update bundle or the post-update bundle, never a
    half-written one.
  - Best-effort: any failure (no jnius, can't read APK, extract
    error) falls through. Worst case the daemon runs old code
    one more cycle and the bootstrap loop guard surfaces the
    popup.
- **Version strip refreshes after restart.** The
  `client X · server Y` strip at the bottom of the settings page
  is populated by ``_probe_server_version`` at app startup and was
  never re-fetched afterwards. After a successful Restart the
  daemon's version changed but the strip stayed frozen at the old
  value, so the restart looked like it did nothing visible.
  ``SettingsScreen._refresh_version_strip_after_restart`` now
  schedules a fresh ``_probe_server_version`` call on the running
  App (works for both ``CollabUIApp`` desktop and ``PickerApp``
  Android since both expose the method) 2 s after the cooperative
  or force-kill restart fires, so the strip rerenders with the
  new daemon version. Best-effort: silent failure leaves the strip
  stale but doesn't break anything else.
- **Force-kill fallback** in `SettingsScreen.restart_server`. The
  cooperative path (`POST /v1/admin/restart`) returns
  `SERVER_ERROR` against a daemon too old to know the endpoint —
  which is the exact scenario users tap the button for ("I just
  installed a new APK, the running daemon is still the old code,
  please get rid of it"). The toast previously said "Could not
  reach the sync service to restart it", leaving the user with
  no recovery path. Now: on `SERVER_ERROR` / `SERVER_UNAVAILABLE`,
  fall through to the same kill-by-PID mechanism
  `SuiteSelfReplaceReceiver` uses on
  `ACTION_MY_PACKAGE_REPLACED`:
  - **Android**: jnius → `ActivityManager.getRunningAppProcesses`
    → `Process.killProcess(pid)` for each non-self PID (same UID,
    no permission needed, bypasses the `:provider` service's
    `IMPORTANCE_SERVICE` pin). `killBackgroundProcesses(pkg)` as
    belt-and-braces fallback for any process the enumeration
    missed.
  - **Desktop**: read `$AZT_HOME/server.json::pid`, `os.kill(pid,
    SIGTERM)`. Auto-spawn picks the daemon back up on the next
    RPC.
  Either path: settings UI process is unaffected (different
  process from the daemon on both platforms). Successful kill
  surfaces the same "Sync service is restarting…" toast the
  cooperative path uses; failure adds a parenthetical detail
  string ("no sibling processes found to kill", "no pid in
  server.json", etc.) to the original "Could not reach" toast so
  the user has a diagnostic.

### Connectivity watcher now starts on Android (auto-push restored)

Field log baf 2026-05-20 17:22:58–17:25:41: `COMMITTED_LOCAL`
fires, peer polls `/v1/projects/.../status` every 10 s for
3+ minutes, **no `[scheduler] drain pushes:` line ever appears**.
User-gestured Sync via the Sync button works (path through
`_h_project_sync`); only the auto-drain is broken.

Root cause: `server_apk/service.py` (the Android `:provider`
entry point) calls `scheduler.reconcile_on_startup()` but
**never `scheduler.start_watcher()`**. The watcher is wired only
in `server.run()` (the desktop loopback entry path). On Android
the daemon commits locally on every `commit_project` RPC,
sets `pending_push=true` in projects.json, and then ... nothing.
No thread to drive the drain loop.

Every Android peer on every version of `azt_collabd` has been in
this state since the commit/push split landed (0.43.0). The
visible symptom — "+N commits ahead of github" with no
auto-push — has been there the whole time; the Sync button
masks it because users routinely tap it.

Fix: one line in `server_apk/service.py::main()` immediately
after `reconcile_on_startup()`:

```python
scheduler.start_watcher()
```

Restores parity with the desktop daemon. Watcher ticks every
`sync.connectivity_poll_s` (30 s default), checks
`_has_internet()` + `sync.work_offline` + 60 s post-online
grace, then calls `_drain_pending_push()` which pushes any
`pending_push=true` projects.

### Sweep pre-0.37 `.cawl_image_urls.json` orphans on daemon startup

Field log baf 2026-05-20 surfaced `[data-loss-risk] uncommittable
file in project_dir: '.cawl_image_urls.json'` on every commit step
for projects that existed before the 0.37 CAWL daemon migration.

Pre-0.37, peers maintained per-project URL caches at
`<working_dir>/.cawl_image_urls.json`. 0.37 moved ownership to the
daemon (`$AZT_HOME/cawl/index.json`, 24h TTL, lock-coalesced) and
peers stopped writing the per-project file — but on devices that
crossed the migration boundary, the existing files were never
cleaned up. Nothing in current code reads or writes them (grep
returns zero hits across `azt_collabd/` and `azt_collab_client/`);
they're inert bytes that the staging filter flags every commit.

`reconcile_on_startup` now calls `_sweep_legacy_orphans()` which
iterates `projects.json::*::working_dir` and `os.remove`s any
known-stale files. Currently just `.cawl_image_urls.json`;
catalogue lives in `_LEGACY_ORPHAN_PATHS` for future migrations to
extend. Idempotent (missing files are a no-op), best-effort (per-
file failures log and continue), runs outside the scheduler lock
(touches FS, not the jobs registry). One log line per file
removed: `[scheduler] orphan sweep: removed 'en-UY-x-kent'/
.cawl_image_urls.json (pre-migration leftover)`.

### `:provider` SIGSEGV fix — self-update poll off the dispatch thread

Field log baf 2026-05-20 16:03:42 captured a `:provider` tombstone
on a 6GB device (so not memory pressure):

```
F libc : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault
        addr 0x0 in tid 8912 (Thread-3), pid 8859 (collab:provider)
F DEBUG : Cause: null pointer dereference
F DEBUG :   #00 art::InvokeVirtualOrInterfaceWithJValues
F DEBUG :   #01 art::JNI<false>::CallObjectMethodA
F DEBUG :   #02 jnius.so
F DEBUG :   #03 jnius.so
F DEBUG :   #04 libpython3.11.so (_PyObject_MakeTpCall+328)
```

The cascade was visible in the same trace:

```
ActivityManager: Killing 8874:org.atoznback.aztrecorder ... depends
on provider org.atoznback.aztcollab/.AZTCollabProvider in dying
proc org.atoznback.aztcollab:provider
```

**Root cause** — the documented jnius-on-worker pattern (see
memory `feedback_jnius_prewarm_main_thread.md`). The dispatch
callback in `azt_collabd/android_cp/service.py` called
`_check_self_updated()` on every RPC, which did two
`PackageManager` JNI calls per call. The Java-side dispatch
thread is a Python worker attached to the JVM with the
bootclassloader; sustained jnius traffic from there eventually
NPE'd `art::JNI::CallObjectMethodA`. Cawl image prefetch (~3–4
RPCs/sec) made this a near-certainty per session.

**Fix** — move the PackageManager poll off the dispatch path.
- `_self_update_poller()` runs on a main-thread Timer
  (60 s cadence). It calls `_pkg_last_update_time()`,
  compares to the snapshot, and flips a module-level bool
  on detection.
- `_check_self_updated()` now reads the bool. Zero jnius
  per RPC. Single-writer / single-reader bool — GIL covers
  the race.
- 60 s detection latency is fine: the only consumer is the
  `_schedule_exit_for_update` gate, and the user's next
  ContentResolver call after install almost always arrives
  within seconds anyway.
- Belt-and-braces: `server_apk/main.py` step 2a.1 extends
  the existing jnius prewarm to cover `PythonService`,
  `Context.getPackageManager`, and
  `PackageManager.getPackageInfo` from the main thread. Any
  future worker-thread caller of these gets the cached
  classloader bindings without paying the bootclassloader
  penalty.

**Memory budget**: the change adds ZERO new threads (the Timer
re-spawns itself with `threading.Timer` after each tick, same
as the connectivity poller). Memory delta ≈ one Python bool.
Appropriate for the 6GB target.

### Drive-by drift fixes (caught while running the 0.43.22 test suite)

- `_ensure_remote_repo` no longer returns `REMOTE_CREATE_FAILED`
  on URLs without an `owner/repo` path (e.g.
  `http://127.0.0.1:NNNN/` from `dulwich.web`-fronted local
  servers, or any LAN git host with a flat-root URL). The
  parse-failure path now falls through to the unknown-host
  branch (`True, None` — let push attempt and surface the real
  error). Unblocks all four `test_local_git_remote.py` tests
  that had been failing since 0.43.21.
- `test_local_git_remote.py::test_pull_repo_fetches_updates_from_local_server`
  and `::test_sync_repo_round_trip_via_local_server` asserted
  `result.has(S.COMMITTED)`, but `commit_repo` / `sync_repo`
  correctly emit `COMMITTED_LOCAL` (the 0.43.0 split between
  commit-and-push and commit-only). `S.COMMITTED` is only used
  by `init_repo`'s initial commit. Tests updated to assert
  `S.COMMITTED_LOCAL`.
- Translation drift: three msgids that AST-walked in source
  but were absent from the French .po — added with French
  strings. `'Network reachable, but the sync host could not
  be resolved.'` (DNS_RESOLUTION_FAILED toast),
  `'Servers'` (section label in daemon settings),
  `'Work offline:'` (toggle label in daemon settings).
- Translation drift (Restart server, 0.43.20-era):
  `'Restart server'`, `'Sync service is restarting…'`,
  `'Could not reach the sync service to restart it.'`, and
  `'Restart request returned an unexpected response.'` were
  added to the .po as `msgstr ""` placeholders when the
  Restart-server button shipped in 0.43.20, but never
  populated. In a French locale that meant `_('Restart
  server')` returned `''` — the button rendered with an
  empty label between "Partager le journal du service" and
  the end of the row, looking like a phantom-sized gap.
  Populated with French translations.

### Not done (intentional follow-ups)

- The "duplicate poller" pattern visible from the peer side
  (status flood at ~2 Hz, 2× "cache warm" log line, two
  `commit_project` calls 30 s apart returning NOTHING_TO_COMMIT)
  is on the peer, not the daemon. The daemon log shows clean
  single-commit debounce for hours of work. Fix lives in
  `azt_recorder` — out of lane for this repo.
- Pre-0.43.22 also lacked typed status emission during long
  syncs (peer polls `project_status` for minutes with no signal
  beyond "running"). `SYNC_GIVING_UP_TRANSIENT` is the first
  step; in-progress status emission (e.g.
  `SYNC_PROGRESS(chunk_n, retries)`) is a separate feature.

## 0.43.21 — startup probe gives up fast on DNS failure + local-HTTP-git-remote test coverage

### Why (startup probe)

A field session from baf (2026-05-20) showed bootstrap stalling
~20 seconds on a presplash with no visible activity when DNS
resolution of `api.github.com` failed (`[Errno 7] No address
associated with hostname`). Trace: `compat_ok t=2.389` →
`bootstrap_done t=22.553`.

Root cause: `_fetch_latest` in `azt_collab_client/ui/update.py`
used `urlopen(timeout=15)` against `/releases?per_page=20`, then
caught **any** exception and fell through to
`_fetch_latest_singleton` which did **a second**
`urlopen(timeout=15)` against `/releases/latest`. On a dead
resolver, both calls fail for the same reason — so we paid the
DNS-timeout budget twice for a doomed retry. The singleton
fallback was designed for "listing endpoint returned junk /
HTTP error" cases where the network is fine; on URLError-class
failures it's pure overhead.

A user staring at a black presplash for 20 s thinks the app has
crashed and force-quits. dd61da3 ("hopefully final fix for
Cameroon DNS errors") fixed the sync-path 25-minute push hang
but didn't touch this bootstrap path.

### Changes (startup probe)

- `_PROBE_TIMEOUT_S = 5` constant introduced in
  `azt_collab_client/ui/update.py`; replaces the hard-coded
  `timeout=15` on both `_fetch_latest` and
  `_fetch_latest_singleton`. Worst-case startup stall now
  caps at one ~5 s timeout instead of two ~15 s timeouts.
- `_fetch_latest`'s `except Exception:` split into three
  branches:
  - `urllib.error.HTTPError` → fall back to singleton (server
    refused the listing; network is fine).
  - `urllib.error.URLError` → `raise` immediately (DNS /
    connect-refused / TLS botch; singleton fails the same way).
  - Bare `Exception` → fall back to singleton (JSON parse
    failure, captive-portal HTML — connection works, body was
    junk).
- Tests added in `tests/test_check_for_update.py`:
  - `test_fetch_latest_falls_back_on_http_error` (404 listing
    → singleton tried).
  - `test_fetch_latest_raises_on_url_error_without_singleton_retry`
    (URLError → call count == 1, no doomed retry).
  - `test_fetch_latest_uses_probe_timeout` (timeout pinned to
    `_PROBE_TIMEOUT_S`).

### Why (local-HTTP-git-remote test coverage)

The daemon's remote handling has always been host-agnostic in
principle — `Project.remote_url` accepts any HTTP/HTTPS git
URL — but the only host exercised in the field is github.com.
A team running gitea / forgejo / gogs / git-daemon on a laptop
on the office LAN is a perfectly valid convergence point, and
the parked LAN-sync spec in `docs/local_lan_sync_stub.md`
builds on dulwich.web as its in-process listener (same library,
same WSGI shape). Without a CI test, a github-ism (substring
matching on a github-specific error string, host-header
assumptions in dulwich, a credentials-store lookup keyed on
`github.com`) could silently break the non-github path and we
wouldn't notice until a field user with a local gitea filed a
bug.

### Changes (local-HTTP-git-remote test coverage)

- `tests/test_local_git_remote.py` added. Fixture spins up a
  dulwich-backed HTTP git server in a background thread serving
  a bare repo. Five tests cover:
  - The fixture itself responds to `info/refs?service=git-upload-pack`.
  - `init_repo` initializes + commits + pushes; bare-side refs
    reflect the push.
  - `clone_repo` against the local server produces a working
    dir matching the seeded content.
  - `pull_repo` brings updates committed on a second working
    dir through the local server.
  - `sync_repo` round-trips two working dirs (commit + push
    under one lock) converging through the local server.
- Auth integration on non-github hosts is deliberately out of
  scope here — the fixture serves unauthenticated HTTP; the
  github-flavoured credentials paths are exercised by their
  own tests. The LAN-sync spec implementation phase will add
  cert-based auth tests on top of this scaffolding.

## 0.43.20 — sync trace honesty + push timeouts + non-FF retry escalation

### Why

Field log from baf (2026-05-19, ~5 hours of intermittent DNS) surfaced
four distinct sync-loop bugs:

1. The `[sync-trace] fetch done` line was logged unconditionally, so
   a fetch that hit `Max retries exceeded (NameResolutionError)`
   looked identical to a healthy fetch — downstream `local_sha` /
   `remote_sha` reads from the (stale) tracking ref then drove
   adaptive-batching decisions that couldn't possibly converge.
2. A single push attempt held `project_lock` for 25 minutes
   (19:11 → 19:36, ending in `SSLEOFError`) because `porcelain.push`
   was called with no timeout — urllib3's default is no read
   timeout, so a stalled SSL upload sits there until the kernel
   keepalive eventually breaks the socket. Every other client RPC
   during that window got `BUSY`.
3. A `DivergedBranches` push rejection followed by a re-fetch
   showing no remote movement entered the "local still ahead"
   recovery branch and looped: same `target_sha`, same `chunk_n`,
   same rejection on the next iteration. The server's rejection
   was authoritative — it had something we couldn't see, or our
   intermediate-target pack didn't include the full ancestry — but
   the loop trusted the local ancestor walk over the server.
4. The "local still ahead" branch (legitimate concurrent-pusher
   case where remote advanced to something our local descends
   from) reset `target_sha`, `working_batch_n`, and `backoff_s`
   to scratch, so every concurrent-push race threw away whatever
   chunk size adaptive batching had found to work. `chunk_n=89 →
   chunk_n=719` reset observed repeatedly in the same trace.

### Changes

`azt_collabd/repo.py`:

- `[sync-trace] fetch done` moved inside the success path; the
  except adds a `[sync-trace] fetch failed: <repr(exc)>` line and
  still surfaces `PULL_FAILED` on the `Result` so the downstream
  try-push-anyway behaviour is preserved.
- `_socket_timeout(seconds)` context manager wraps `porcelain.fetch`
  (60 s) and `porcelain.push` (180 s) — sets
  `socket.setdefaulttimeout` for the body, restores on exit. urllib3
  starts fresh connections on pool exhaustion (the
  `Starting new HTTPS connection (N)` trace lines confirm this), so
  the timeout reliably bounds each socket's I/O. DoH calls in
  `net.py` pass explicit `timeout=` to `urlopen` which override the
  default, so DoH stays at its 2.5 s budget.
- `nonff_no_progress_streak` tracker added to the push loop. When a
  push raises non-FF AND the re-fetch shows no remote movement:
  - First hit on an intermediate target (`target_sha != local_sha`):
    revert to the full local tip via the standard
    `refs/heads/<branch>` refspec. The temp-ref pack-negotiation
    path may be the culprit; the full-tip path bypasses it.
  - First hit on the full local tip: force a `_merge_diverged`
    against the current `remote_sha`. The server's rejection is
    authoritative; our local ancestor walk is wrong somewhere.
  - Second hit on the full local tip: bail with `PUSH_FAILED`.
    We can't reconcile from here without external input.
- The legitimate "still ahead" branch (re-fetch saw remote actually
  advance, but to something our local descends from) now `continue`s
  with `target_sha` + `working_batch_n` + `backoff_s` preserved —
  the post-reconciliation reset is confined to the diverged-merge
  branch where it belongs.

### Also in this bump: "Restart server" button

Companion fix surfacing the daemon-restart capability that the
sync-loop hardening above relies on for "give me a fresh process
right now" recovery.

- `POST /v1/admin/restart` (`azt_collabd/server.py::_h_admin_restart`):
  responds OK immediately and then, after a 0.5 s flush delay,
  terminates the daemon. Desktop loopback: `os.execv` replaces the
  process image with a fresh `python -m azt_collabd`, inheriting
  env / cwd / PYTHONPATH; the new daemon re-acquires `server.lock`
  and writes a new `server.json`. Android `:provider`:
  `os._exit(0)`, Android's ContentProvider auto-spawn revives the
  process on the next peer call and `Service.onCreate` re-runs
  `reconcile_on_startup()`.
- `azt_collab_client.restart_server()`: thin wrapper around the
  endpoint. Returns a `Result` carrying either `RESTARTING`
  (informational; daemon accepted and is in flight), or the
  standard `SERVER_UNAVAILABLE` / `SERVER_ERROR` transport-failure
  shapes. Never raises — UI handlers call without try/except.
- `RESTARTING` status code added to both
  `azt_collabd/status.py` and `azt_collab_client/status.py`
  (mirrors; client copy is decode-only) plus a translation in
  `translate.py`. params: `transport=`'desktop' | 'android' |
  'unknown'`.
- `azt_collabd/ui/app.py::SettingsScreen.restart_server` +
  KV `restart_server_btn` next to "Share daemon log" under
  Diagnostic log. Method runs the blocking RPC off the UI thread
  and shows status in the existing `daemon_log_status` label.
  Settings UI lives in a separate process on both desktop
  (`python -m azt_collabd ui`) and Android (PythonActivity vs.
  `:provider`), so triggering a daemon restart doesn't kill the
  UI in either case.

### Wire compat

Sync-loop hardening: nothing changes on the wire. Failures still
surface as `PUSH_FAILED` (with `DNS_RESOLUTION_FAILED` peer-routing
in the existing `_add_push_failure` path).

Restart-server: one new status code (`RESTARTING`) and one new
endpoint (`POST /v1/admin/restart`). Older clients calling the
new endpoint would get a 404; older daemons receiving a
`restart_server()` call would 404 and the client wrapper
surfaces `SERVER_ERROR`. No `MIN_CLIENT_VERSION` floor bump
needed — peers don't depend on the endpoint for any existing
flow.

### Test fixture: `test_local_git_remote.py` mkdir

The `local_git_server` fixture was erroring at setup against the
installed dulwich (`FileNotFoundError: '<tmp>/remote.git/branches'`
inside `Repo._init_maybe_bare`) because newer dulwich expects the
`controldir` path to exist before `init_bare`. Added an explicit
`remote_path.mkdir()` so the fixture matches the dulwich contract.
The five tests in that file (LAN-sync prep — see 0.43.19) now get
past setup; the sanity test passes, but four of them surface
latent assertion failures (push / clone / pull / sync against a
dulwich-backed local HTTP server without auth wiring). Those are
unrelated to this bump's daemon-side changes — they're testing
LAN-sync prep code that's parked anyway, and the fixture fix
only made the latent failures visible. Left for a dedicated
LAN-sync session.

## 0.43.19 — LAN sync design spec drafted (parked)

### Why

`docs/local_lan_sync_stub.md` was a sketch; expanding it to a real
spec after researching mDNS-on-Android (Android 17 will gate raw
mDNS sockets behind a new runtime permission — `NsdManager` with
`FLAG_SHOW_PICKER` is the escape hatch), Android 14+ foreground-
service rules (`specialUse` is the right type; `dataSync` has a
6h/24h cap that's a footgun for an always-on toggle), dulwich's
HTTP server seam (`HTTPGitApplication` + `make_wsgi_chain`
covers both upload-pack and receive-pack out of the box), and
offline-first peer-to-peer git patterns (Syncthing-style identity
+ Radicle's namespace separation of identity from endpoint).

### Changes

- `docs/local_lan_sync_stub.md` rewritten as a design spec with
  eight load-bearing decisions locked, concrete touchpoints
  enumerated, and a short list of items deferred to
  prototyping. Still parked — no implementation in this bump.
- Onramp section added so a fresh agent (post-/clear) can pick
  the work up without re-litigating the 23 rejected
  alternatives. Includes a phased implementation plan (8
  phases, each independently smokable), a reading list of
  upstream CLAUDE.md files + relevant memory entries, the
  status codes to add, desktop-only smoke recipes for phases
  1-4, and an eight-question self-test.

### Decisions captured (so they're not relitigated)

- Topology: GitHub-authoritative star + opportunistic LAN fan-out.
  No peer-graph / gossip state — pairing is explicit per-pair,
  propagation is implicit in git's ref-advertisement dedup.
- Identity: per-device ed25519 keypair at `$AZT_HOME/peer_id`,
  separate from the suite signing-keystore fingerprint.
- Pairing: one-way QR (A shows, B scans), auto-reverse-record on
  B's first authenticated fetch.
- Auth: pinned TLS cert on LAN (cert handshake = identity proof);
  loopback bearer token unchanged.
- Listener: `dulwich.web` hosted in the existing `:provider`
  process, promoted to a `specialUse` foreground service while a
  daemon-wide "Allow LAN sync" toggle is on.
- Discovery: Android `NsdManager` via pyjnius
  (`FLAG_SHOW_PICKER`); `python-zeroconf` on desktop; QR /
  manual-IP fallback for hotspot scenarios where mDNS is
  silently blocked.
- Project sharing: per-direction allowlist after pairing, set
  from the daemon settings UI; no QR for the second step.

## 0.43.18 — daemon log rotates on toggle-on; previous session preserved in `.prev`

### Why

The "Save daemon log to file" toggle's `truncate=True` path
opened the log file in `'w'` mode, wiping any prior content. A
remote tester flipping the toggle to start a fresh investigation
would lose the previous investigation's evidence — confirmed in
the field after the user said "the toggle just erased the sync
trace we'd been waiting to see." Remote debugging across time
zones and a language barrier doesn't have the bandwidth to
re-ask "do exactly the same thing again so I can capture the
same log."

### Change

`azt_collabd/server.py::install_stdio_tee(truncate=True)` now
rotates first:

1. If `$AZT_HOME/daemon.log` exists, rename it to
   `daemon.log.prev` (overwriting any prior `.prev`).
2. Open `daemon.log` fresh in `'w'` mode (effectively empty
   since the rename took the prior content).
3. Write the `(fresh session, daemon X.Y.Z)` banner.

Respawns (the `truncate=False` path, fired by
`maybe_install_stdio_tee` at every `:provider` boot) continue to
append to `daemon.log` unchanged — rotation only happens at the
explicit toggle-on gesture. This avoids the otherwise-pathological
case where idle-stop respawns rotate the `.prev` away every few
minutes.

`azt_collabd/ui/app.py::share_daemon_log` now passes
`prev_path=data['log_path'] + '.prev'` to `share_log_file`. The
existing `_bundle_log_blob` silently skips the previous-session
section when the path doesn't exist (first toggle-on, no prior
file to rotate), so the share bundle gracefully degrades to
just the current-session block when there's no `.prev`.

Net behaviour:

- **First toggle-on ever:** no rotation, fresh log, share shows
  one `=== current session ===` block.
- **Subsequent toggle-on:** prior log rotated to `.prev`, new
  `.log` starts fresh, share shows
  `=== previous session === / === current session ===`.
- **Daemon respawn (toggle stays on):** appends to current `.log`
  with an `(appending — daemon X.Y.Z respawn)` banner as section
  break. `.prev` is untouched.

### Notes

- Matches the peer (`azt_recorder.log` / `azt_recorder.log.prev`)
  rotate-on-launch pattern that the share helper was already
  written to support — just plumbed through to the daemon side.
- No wire-format change; no `MIN_CLIENT_VERSION` change.
- Rotation is best-effort: if the rename fails (disk full,
  permissions, whatever), we log the failure to `sys.__stderr__`
  and continue opening `.log` in `'w'` mode — so the toggle-on
  gesture still gives the user a clean log even if rotation
  couldn't preserve the prior session.

## 0.43.17 — drop the "Email daemon log" button from the settings UI

### Why

The settings UI had two side-by-side buttons for shipping the
daemon log: "Share daemon log" and "Email daemon log".

- **Share** reads the file directly from disk, includes the
  `=== current session (<path>) [daemon X.Y.Z] ===` header
  (with the daemon-version tag added in 0.43.8), and sends as
  a real file attachment via `Intent.ACTION_SEND` + MediaStore +
  `EXTRA_STREAM`. Arbitrary size, picker offers email apps
  alongside messaging / file-saver / cloud-paste.
- **Email** used `Intent.ACTION_SENDTO` with the log inlined into
  the body parameter of a `mailto:` URI. That made the log
  payload subject to (a) the daemon's 256 KB RPC truncation cap,
  (b) URI size limits in the email app and Android's URI parser,
  and (c) loss of the session-header decoration.

For "send the daemon log to the developer", **Share is strictly
better** — the user picks their email app from the chooser and
the attachment is a full-fidelity file. The Email button was
the strictly-worse path of the two; offering both was confusing
and invited testers to pick the worse one.

### Changes

`azt_collabd/ui/app.py`:

- KV layout: the `RecBtn` for `_('Email daemon log')` is gone;
  the remaining "Share daemon log" button stretches to fill the
  row.
- `email_daemon_log` method removed.
- `_dispatch_daemon_log` collapsed into `share_daemon_log`
  (single channel, no branch). Docstring rewritten to record
  the rationale so the next maintainer doesn't re-add an Email
  button without re-reading why it was removed.

`azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`:

- `msgid "Email daemon log"` entry dropped.

### Notes

- `azt_collab_client.ui.share.email_text` itself is **not**
  removed — peers may still want a "send the developer a short
  note" mailto-only path for other contexts. The helper just
  stops being called from `azt_collabd.ui.app`.
- No wire-format change. Pure UI cleanup.

## 0.43.16 — fix push regression introduced in 0.43.13's non-FF guard

### Regression

0.43.13 added a "safety" branch to the push retry loop in
`_push_step_locked`: when a non-FF rejection lands AND the
post-rejection fetch reports `new_remote == local_sha`, bail with
`PUSH_FAILED` rather than claiming `S.PUSHED`. The reasoning was
"non-FF but the server already shows our tip means we're in an
inconsistent state we can't recover from in-loop; claiming PUSHED
would silently lose data."

That reasoning was wrong. The actual common path that hits this
branch is adaptive batching: an earlier network failure halved
`target_sha` to an intermediate ancestor of `local_sha`. While
the loop is shrinking, the server (concurrent peer push, prior
in-flight commit, anything) advances to `local_sha`. The peer's
next attempt pushes `target_sha:refs/heads/<branch>`. Server
rejects: target_sha is an ancestor of the server's current tip,
so the push would move the ref backwards → non-FF rejection
shape. We re-fetch, `new_remote = local_sha` (matches local).

In that case **the server already holds `local_sha`** — we are
in sync. The pre-0.43.13 code correctly claimed `S.PUSHED` here.
0.43.13's bail turned this into a spurious failure, and a phone
that had been adaptive-batching its way through a slow link
would stop progressing.

### Fix

Removed the `if non_ff: bail` arm. The equal-local-new_remote
branch now claims `PUSHED` for *both* race and non-FF triggers —
which is correct, because in both cases the server's view of the
branch matches our local tip. Comment block at the call site
updated to record the 0.43.13 → 0.43.16 history so the next
maintainer doesn't reintroduce the same "safety" guard.

### Also: `new_remote is None` guard

0.43.13's gate change (`(new_remote and new_remote != remote_sha)
or non_ff`) could enter the reconciliation block with
`new_remote = None` when the only trigger was `non_ff` and the
local repo hadn't yet written `refs/remotes/origin/<branch>` (a
freshly-cloned-but-empty-tracking-ref state). The ancestor
checks below would then walk against `None` and raise. New gate:
`new_remote and ((new_remote != remote_sha) or non_ff)`. The
``new_remote`` truthy check now governs both arms, so `non_ff`
without a real tracking SHA falls through to the unfamiliar-
exception backoff path instead of crashing the loop.

### Notes

- No wire-format change. Pure daemon-side fix.
- The 0.43.13 `_format_push_error` rewrite (tuple-of-bytes
  → "remote rejected ref update (likely non-fast-forward …)") is
  unchanged and still in effect — that part of 0.43.13 was right
  on its own.
- A peer that had stopped syncing due to this regression should
  resume on the first user-initiated Sync gesture against a
  0.43.16+ daemon. No state to clear.

## 0.43.15 — daemon-log file captures everything (`stdout` + `stderr`), and the tee auto-installs on every `:provider` respawn

### Field report (and a finished thought)

A user with "Save daemon log to file" toggled on shared a log
whose only content was the banner line:

```
=== current session (.../daemon.log) ===
[01:51:44] [daemon-log] mirroring stderr to '…' (fresh session,
                                                 daemon 0.43.9)
```

Two distinct gaps explained that empty log; both are closed here:

1. **The tee only captured `sys.stderr`.** Boot-trace prints
   (`print(f'[boot-trace-daemon] phase=…')` in
   `server_apk/service.py` and `server.py`) go via `sys.stdout` by
   default. Most other daemon diagnostics use explicit
   `file=sys.stderr` — those *were* captured — but
   `[boot-trace-daemon]`, `[service] entering idle-stop loop`,
   and a scattering of other status prints went to logcat only.
2. **On Android, the tee only installed via the UI toggle and
   never on `:provider` boot.** `server.run()`'s
   `_maybe_install_stderr_tee` call is the loopback HTTP server
   path — never executed in the server APK's `:provider` process.
   So even with the persisted toggle on, every daemon respawn
   after an idle auto-stop started with no tee until the user
   re-touched the UI toggle.

### Changes

`azt_collabd/server.py`:

- `_StderrTee` → `_StdioTee`. Generalised to wrap any stream;
  two instances are installed in tandem (one over `sys.stdout`,
  one over `sys.stderr`) sharing a single underlying file *and* a
  shared `[bool]` start-of-line flag so per-line `[HH:MM:SS] `
  stamping stays correct when the two streams interleave.
- `install_stderr_tee` → `install_stdio_tee`: opens the log file
  once, installs both `_StdioTee` instances atomically.
- `uninstall_stderr_tee` → `uninstall_stdio_tee`: restores both
  originals and closes the file.
- `_maybe_install_stderr_tee` → `maybe_install_stdio_tee`. Public
  now (no leading underscore) because the Android service body
  calls it from outside this package.
- All four internal call sites updated; banner wording changed
  from "mirroring stderr to …" to "mirroring stdio to …".

`server_apk/service.py`:

- `main()` now calls `azt_collabd.server.maybe_install_stdio_tee()`
  immediately after `import azt_collabd`. This is the Android
  equivalent of `server.run()`'s desktop-path install. Subsequent
  `_boot_trace` lines (`configured`, `before_install_callbacks`,
  `after_install_callbacks`, `before_reconcile`, `after_reconcile`,
  `entering_idle_loop`) and every later `[recent]` / `[cawl]` /
  `[commit-*]` print land in the on-disk log alongside the existing
  stderr stream.
- Lines emitted *before* `import azt_collabd` (`module_loaded`,
  `main_entered`, `before_import_azt_collabd`) still only reach
  logcat. Capturing those would require a Python-side read of the
  toggle without `azt_collabd` imported — possible but adds
  duplication of path-resolution logic. Out of scope for this
  bump; the captured tail is the diagnostically valuable part.

### Notes

- No wire-format change; `MIN_CLIENT_VERSION` and
  `MIN_SERVER_VERSION` untouched.
- Existing `daemon.log` files keep working — the file format
  (per-line `[HH:MM:SS] ` prefix, plain text) is unchanged. Just
  more lines land in it now.
- Hot-toggle still works: flip "Save daemon log to file" off in
  the settings UI and *both* tees uninstall atomically; flip back
  on and they install together against a fresh-session log.

## 0.43.14 — revert 0.43.12 `_python_bundle/` wipe; shorten DoH negative cache

### Urgent: revert the 0.43.12 wipe

0.43.12 added a step to `SuiteSelfReplaceReceiver.onReceive` that
recursively wiped `files/app/_python_bundle/` after a package
replace, so the next spawn would force p4a to re-extract from the
new APK's assets. That theory was: any spawn that finds the dir
missing will trigger p4a's "extract from APK" branch.

**That's true for the Activity bootstrap but NOT for the
`:provider` service bootstrap.** P4a's service bootstrap expects
`_python_bundle/` to already be present and imports from it —
there is no extract-on-missing branch. After the wipe, every
lazy-respawn of `:provider` hit
`_python_bundle does not exist...should we expect a crash soon?`
in p4a's bootstrap, exited, and was respawned by `START_STICKY`
into the same broken state. Field log from a 0.43.12 install:

```
[python] _python_bundle does not exist…should we expect a crash soon?
[python] Initializing Python for Android   (next respawn, PID 1919)
[python] _python_bundle does not exist…should we expect a crash soon?
[python] Initializing Python for Android   (next respawn, PID 1944)
[python] _python_bundle does not exist…should we expect a crash soon?
```

Receiver reverted to its pre-0.43.12 three-step shape (per-PID
reap → `killBackgroundProcesses` fallback → self-kill). The
import and helper method added in 0.43.12 are gone. The class
Javadoc carries a NOTE explaining why the wipe is *not* there, so
the next person to try this re-derives the conclusion instead of
rediscovering the crash-loop.

The stale-unpack problem the wipe was meant to solve is still
real — `/v1/health` will keep reporting the old `__version__` on
a server-APK replace if only the Service has been respawned since.
A correct fix needs to also cause the Activity to run (so its
bootstrap re-extracts) or add an extract-on-missing branch on the
service side. Out of scope for this hotfix.

### Also: DoH negative-cache window shrunk 30 s → 5 s

`net.py:_DOH_NEGATIVE_TTL_S` controls how long
`_patched_getaddrinfo` remembers a failed Cloudflare-1.1.1.1
lookup before retrying. The pre-0.43.14 value was 30 s — wide
enough that a single transient DoH miss during a Starlink
satellite handover (~15 s) or a connectivity blip would silently
disable the fallback for a user-initiated push retry that arrived
within 30 s of the miss. Field reports of "DNS resolution failed"
in a loop, on networks where the user's browser kept working,
trace to this poisoning window.

5 s is the new value:

- Still long enough to absorb urllib3's in-loop retry storm
  (~3 attempts within ~2 s) so we don't pay the 2.5 s DoH timeout
  on every one.
- Short enough that any human-perceived retry (tap Sync, see
  error, wait, tap Sync again) gets a fresh DoH probe.
- Background `_has_internet()` ticks still debounce via the
  positive-cache TTL (300 s) once a probe actually lands.

Comment block updated to spell out the Starlink-handover
rationale so the next maintainer doesn't widen the window again.

### Notes

- Hotfix release; ship over 0.43.12 / 0.43.13 directly. Users
  caught in the 0.43.12 crash-loop install **this** APK and the
  next replace's receiver runs from the new code (Step 1 reaps
  the surviving `:provider`, Step 3 self-kills), then peer's next
  call lazy-spawns a clean `:provider` against the existing
  `_python_bundle/` — which is still the old version's code, but
  at least it's not crash-looping. Versions reconcile naturally
  as Activity boots happen.
- Carries the 0.43.13 work below (push error formatting +
  non-fast-forward recovery) since 0.43.13 was never released.

## 0.43.13 — diagnosable `PUSH_FAILED` messages + non-fast-forward push recovery

### Field report

Peer log on a phone running a couple versions behind:

```
Échec de l'envoi : (b'810ef46cd8378ca8e0a199a54fd2d765035d706d',
                    b'd7d4c0b95b76977b15ec39da109f77aa9917e7a0')
```

The raw `(b'old_sha', b'new_sha')` repr is dulwich
`UpdateRefsError`'s default `__str__` when its `args` is a tuple
of byte SHAs (the (old, new) pair the server reported as
rejected) and the exception has no override. `repo.py`'s
`_add_push_failure` was calling `str(exc)` directly, so the
useless tuple landed in `Result.params['error']`, was passed
through `_('Push failed: {error}')` → `Échec de l'envoi : {error}`,
and reached the user as a meaningless line. Worse, the underlying
cause (almost always non-fast-forward) was invisible to anyone
reading the log.

### Two fixes

**1. `_format_push_error(exc)`** in `azt_collabd/repo.py`. When
`str(exc)` is the bytes-tuple shape, rewrite it as `'remote
rejected ref update (likely non-fast-forward — remote has commits
not present locally): <raw>'`. Falls through to `str(exc)` for
informative shapes (network errors, HTTP 4xx). Wired into
`_add_push_failure`, the `PULL_FAILED` site after the initial
fetch, and the `_merge_diverged` failure path. Net result: every
`Result.params['error']` from a push/pull failure is now a
sentence a user (or maintainer reading a shared log) can act on.

**2. `_is_non_ff_rejection(exc)` + retry-loop gate change.** The
push retry loop already re-fetches after a failure and reconciles
when `new_remote != remote_sha` (race-with-concurrent-pusher).
The gate was too conservative for the case observed here: a
genuine non-fast-forward where our `refs/remotes/origin/<branch>`
cache happens to already match the server's tip (we fetched on a
prior iteration but the merge never happened, or we're being
adversarially-served stale fetch responses, etc.). The rejection
itself proves the server has something we don't, regardless of
what the mirror ref says.

New gate: `(new_remote and new_remote != remote_sha) or
_is_non_ff_rejection(exc)`. When non-FF is detected, the loop
enters the same four-case reconciliation
(equal / remote-ancestor / local-ancestor / diverged) used for
the race case, with one extra safety branch: in the
`local_sha == new_remote` arm, if the trigger was a non-FF
rejection (not a race), the loop bails with the formatted error
instead of claiming `S.PUSHED` — claiming success on a
genuinely-rejected push would silently lose the data on the next
round.

### Notes

- Pure daemon-side fix; no wire-format change, no peer rebuild
  required. The corrected error string travels through the
  existing `Result.params['error']` slot and the existing
  `_('Push failed: {error}')` translation site, so any peer at
  any version sees the improved message as soon as the daemon
  serving them is 0.43.13+.
- The retry loop's `MAX_CONSECUTIVE_FAILURES = 12` cap is
  unchanged; non-FF detection just shortens the cap-burning loop
  in the case where reconciliation would have succeeded if we'd
  let it run.

## 0.43.12 — `SuiteSelfReplaceReceiver` wipes the stale p4a unpack on package replace

### The replace-but-stale-Python loop, finally

Symptom: every server-APK update left `/v1/health` reporting the
*old* `__version__` from on-device Python code, even though the
new APK was demonstrably on disk and PackageManager reported its
new `versionName`. The peer's bootstrap correctly detected the
mismatch (`installed > running`) and popped the "Restart your
device" loop on every install — even after force-stopping
`:provider`, even after the receiver fired and reaped the daemon
process.

### Root cause

p4a's bootstrap unpacks the APK's bundled Python code to
`files/app/_python_bundle/` on the Activity's first launch, then
short-circuits subsequent launches whenever the dir already
exists — including launches from a different (newer) APK after a
package replace. The ContentProvider's `:provider` service
bootstrap doesn't unpack at all; it just imports from
`_python_bundle/site-packages/`. So a replace install left the
on-disk APK at version N+1 while every lazy-respawned `:provider`
kept reading version N's Python code from the stale unpack
directory, and `/v1/health` forever reported N. Force-stopping
`:provider` didn't help because the lazy-respawn loaded the same
stale dir; the receiver firing didn't help because all it did was
kill processes that were going to be respawned against the same
stale dir.

Smoking gun: aapt-dumping the installed APK's manifest at 0.43.11
returned `versionName=0.43.11`, while the running daemon's
`/v1/health` reported `0.43.9` — same UID, two different versions
of the same code.

### Fix

`SuiteSelfReplaceReceiver` now wipes `files/app/_python_bundle/`
as Step 1 of `onReceive` (before the process kills below). The
wipe runs synchronously before `onReceive` returns; existing
interpreters in memory are unaffected (their code is already
loaded), but the next lazy-spawn — Activity OR Service — finds
no bundle and falls into p4a's C bootstrap "no bundle present,
extract from APK assets" branch. Existing receiver work (per-PID
kill, `killBackgroundProcesses`, self-kill) follows unchanged,
renumbered to Steps 2–4. Receiver's Javadoc rewritten to
document the unpack semantics so the next person reading the
file understands why Step 1 exists.

`deleteRecursive` helper added; same-UID file deletion needs no
permission. Logs success/failure under the existing
`SuiteSelfReplace` tag so a tester's logcat answers "did the
wipe land" definitively.

### Notes

- No wire-format change; `MIN_CLIENT_VERSION` and
  `MIN_SERVER_VERSION` untouched (both still 0.43.11 from the
  test-scaffolding bump in 0.43.10).
- The receiver lives in every suite APK via the existing
  `add_src` path. Server APK gets the load-bearing path
  (wipe + `KILL_BACKGROUND_PROCESSES` permission); peer APKs
  get the wipe + a no-op kill fallback, which is harmless: a
  peer that updates while its own `:provider`-equivalent is
  alive doesn't exist (peers have no long-lived service
  process).
- Takes effect on the install of *this* APK: Android registers
  the new APK's components before dispatching
  `MY_PACKAGE_REPLACED`, so it's the new (wipe-enabled) receiver
  that runs during this very replace. No manual workaround
  needed for this update or any future one.

## 0.43.9 — ContentProvider transport absorbs the daemon cold-spawn race transparently

### Peer first-launch crash after server-APK reap

Field report: peer launched, "load and crash"; tapping the icon a
second time worked. Logcat for the working launch shows
`[boot-trace-daemon] phase=before_install_callbacks t=0.878` →
`phase=after_install_callbacks t=1.925` — a ~1 s window during
which Android has registered the `org.atoznback.aztcollab`
ContentProvider authority but the Python body inside `:provider`
hasn't yet installed the dispatch callback. The Java side returns
a null `Bundle` for any peer call during that window.

Bootstrap's existing fast-fail policy treats three consecutive
`null_bundle` errors as a structural signature/install failure
and renders the "AZT Collaboration is unresponsive" popup
(`_NULL_BUNDLE_FAIL_FAST = 3`, adaptive backoff
`(0.2, 0.4, 0.8) s` — total 1.4 s). That's *less* than the
~1.9 s cold spawn the new (0.43.7) `SuiteSelfReplaceReceiver`
makes more common: reliable reaping of `:provider` during the
suite-replace receiver means the next peer call lazy-spawns
from scratch instead of finding a survivor. Pre-0.43.7, the
old daemon process limped along across the APK replace and the
peer never hit the cold-spawn window — the same code path
that now fails was effectively gated behind a now-fixed bug.

Even after bootstrap, post-launch RPCs had no retry on
`null_bundle` at all — they went through `rpc.call`'s single
re-discover and raised. If the daemon idle-stopped while the
peer was open (5 min default), the next main-thread call would
crash the peer the same way.

### Fix: transparent retry inside the ContentProvider transport

`azt_collab_client/transports/android_cp.py`:

- `AndroidContentProviderTransport._raw_call` now retries on
  `bundle is None` with backoff
  `(0.1, 0.2, 0.4, 0.8, 1.6) s` — 3.1 s total budget. Covers
  the observed cold-spawn import (~1.9 s) plus margin. Safe
  for POSTs as well as GETs: a null Bundle means the Python
  dispatch never ran, so no work was done on the daemon side.
  Records `null_retries=N` on the `transport.call.post` first-
  try log line for diagnostic visibility.
- `discover()` applies the same backoff to its initial `ping`
  with delays `(0.0, 0.2, 0.4, 0.8, 1.6) s`, since the discovery
  ping is frequently the call that *causes* the lazy spawn.

`null_bundle` only escapes the transport now after the 3 s
budget is exhausted — i.e. when the failure really is
persistent (signature-grant denial, authority gone). Bootstrap's
existing fast-fail on the kind still works for that case;
docstring in `transports/__init__.py` updated to spell out
that the kind is now reserved for genuinely unrecoverable
failures.

### Notes

- Pure client-side fix; no daemon changes, no wire-format
  change, `MIN_CLIENT_VERSION` untouched.
- Peer rebuild required to pick up the fix (the symlinked
  `azt_collab_client` is consumed at peer-build time). Running
  daemon doesn't need to be updated.
- The 3 s budget is paid on the slow path only; healthy
  steady-state calls return on the first attempt with zero
  overhead.

## 0.43.8 — `_touch_project` no longer rewrites `config.json` on every hot endpoint; daemon log gets per-line timestamps and a version-tagged share header

### Phone-slowness fix

Field report: phone visibly slow while working through a CAWL pass.
Daemon log shows the cause — `_touch_project('baf')` firing 10–15× per
displayed entry (one per `cawl_image_bytes`, one per `project_status`
poll, one per `get_audio`, …), and **every** call rewrites
`$AZT_HOME/config.json` to disk (load → atomic temp + rename) and
emits a `[recent]` log line that the daemon-log mirror also writes to
disk. On Android internal storage that is the dominant per-action
cost.

The diagnostic comment at `_h_cawl_cache_status` already noted that
the 1 Hz prefetch-progress poll was intentionally excluded from
`_touch_project` for this exact reason, but every other langcode-bound
endpoint kept paying the toll.

Fix in two layers:

1. **`server._touch_project`** now reads
   `store.get_last_langcode()` first and short-circuits the disk
   write + log line when the value is already current. The cheap
   `auto_prefetch` re-trigger still fires (it has its own 30 s
   per-repo throttle and is what drives circuit-recovery when
   connectivity returns), so behaviour is unchanged for any caller
   that wasn't already in the "stamp the same value again" steady
   state.
2. **`store.set_last_langcode`** gained a module-level
   `_last_langcode_cache` that holds the last successfully-stamped
   value in memory. Writes that match the cache are a no-op (no disk
   read, no disk write); writes that don't match update the cache
   in lock-step with `_save_config_file`. `get_last_langcode` reads
   from the cache so the server-side check above is also free.

Defense in depth: either layer alone would eliminate the flooding;
both together mean any future caller (settings UI, picker, sister
app) gets the same short-circuit without remembering to apply it.

### Daemon-log diagnostic enhancements

Two readability gains for testers / maintainers reading
`daemon.log` after the heartbeat traffic went quiet:

1. **Per-line timestamps in the file mirror.** `_StderrTee` now
   prefixes each new line written to the daemon-log file with
   `[HH:MM:SS] `. Original stderr (logcat / terminal) is left
   unchanged — logcat already supplies its own time column, and
   double-stamping would just clutter `adb logcat` output.
   Implementation tracks an at-start-of-line flag across calls
   so a `print(x)` that lands as separate `write('text')` +
   `write('\n')` calls still gets exactly one stamp at the head
   of the line.

   Motivation: with `_touch_project` no longer firing on every
   RPC, the remaining log traffic doesn't supply a passage-of-
   time signal on its own. The stamp closes that gap — a tester
   can now see "5-second gap here, then a burst" without
   correlating against external state.

2. **Server version in the daemon-log banner _and_ the share
   header.** `install_stderr_tee`'s on-start banner now reads
   `(fresh session, daemon 0.43.8)` / `(appending — daemon
   0.43.8 respawn)`, so the on-disk file is self-describing.
   The `share_log_file` bundle header also reads
   `$AZT_HOME/server.json :: version` and renders the
   current-session line as
   `=== current session (<path>) [daemon X.Y.Z] ===`. Falls
   back silently to the path-only form if `server.json` isn't
   readable.

### Notes

- No wire-format change; `MIN_CLIENT_VERSION` is untouched.
- Sole on-disk effect: `config.json :: recent.last_langcode` now
  gets one write per actual project switch instead of one per
  langcode-bound RPC. The persisted value is identical.
- `[recent] _touch_project` lines in the daemon log are now a useful
  signal again — one per real change — rather than a per-call
  heartbeat.

## 0.43.7 — `SuiteSelfReplaceReceiver` actually reaps the `:provider` process; `azt_collab_client/CLAUDE.md` split into rules + `docs/rationale/`

### Doc reorganisation: rules vs. rationale

`azt_collab_client/CLAUDE.md` had grown to ~900 lines, mostly
"architectural rationale" sections that triggered "large file
will impact performance" notices on every boot. Split along the
existing rationale-section headers:

- `azt_collab_client/CLAUDE.md` (now ~315 lines) holds the
  **rules / invariants / hard contracts** — hard rules,
  daemon-owned state, transport facade, public API surface
  rules, status-code contract, the new index of rationale
  files. Reading just this file is sufficient to avoid
  breaking the client.
- `azt_collab_client/docs/rationale/<topic>.md` holds the
  *why* behind each subsystem. Eight files: `sync.md`,
  `lift_access.md`, `cawl.md`, `i18n.md`, `identity.md`,
  `ui.md`, `lowpower.md`, `project_switch.md`, plus a
  `README.md` index in the same directory.

New hard rule (#9 in CLAUDE.md): **rules live in CLAUDE.md;
rationale lives in `docs/rationale/`**. Rules never go into
rationale files. If a "do X / don't do Y" emerges from a
why-discussion, it goes up into CLAUDE.md (or
CLIENT_INTEGRATION.md if it's a peer contract) with the
rationale file providing the justification via a forward link.

Net effect: every daily session boot loads ~315 lines instead
of ~900; subsystem rationale is one Read away when needed.
Convention previously recorded in memory as "CLAUDE =
philosophy / rationale / architecture" supersedes to "CLAUDE
= rules / invariants; rationale lives in docs/rationale/".

### `SuiteSelfReplaceReceiver` actually reaps the `:provider` process

**Motivation.** Sideloading the server APK via `adb install -r` (and
every other install pathway) left the old daemon process running.
Symptom: the "Restart your device to switch to the newer version"
popup (`_prompt_server_reboot_to_apply` in
`azt_collab_client/ui/bootstrap.py`) fired on every install, despite
the receiver having been in place since 0.41.28.

**Diagnosis.** `SuiteSelfReplaceReceiver.onReceive` called
`ActivityManager.killBackgroundProcesses(getPackageName())`. That API
only kills processes whose importance is at or below the BACKGROUND
threshold. The server APK's `:provider` process hosts
`AZTServiceProviderhost` as a `START_STICKY` started service
(`AZTServiceProviderhost.java:121`), which pins the process at
`IMPORTANCE_SERVICE` — *above* the background threshold. So
`killBackgroundProcesses` was a documented no-op against the very
process the receiver was meant to reap. The new APK's bytes were on
disk, but every peer ContentResolver call routed into the still-alive
old `:provider` interpreter, which kept reporting the old
`__version__` from `/v1/health`, which tripped the installed-vs-
running mismatch in `_prompt_server_update`.

It worked occasionally — but only on OEMs that aggressively kill
all processes of a replaced package during install. That was
incidental, not contracted.

**Fix.** `SuiteSelfReplaceReceiver` now enumerates running processes
via `ActivityManager.getRunningAppProcesses()` (which always returns
the caller's own same-UID processes) and calls
`android.os.Process.killProcess(pid)` on each non-receiver PID
before falling back to `killBackgroundProcesses`. Same-UID
`killProcess` needs no permission and ignores process importance,
so a `START_STICKY`-pinned `:provider` gets reaped directly. The
existing `killBackgroundProcesses` call is kept as a belt-and-braces
fallback for anything enumeration might miss but is in a reapable
state.

**Files.**

- `android/src/main/java/org/atoznback/aztcollab/SuiteSelfReplaceReceiver.java`
  — three-step body (per-PID kill, then `killBackgroundProcesses`,
  then self-kill); top-of-file Javadoc rewritten to spell out the
  importance-state gotcha so the next person reading the file
  understands why step 1 exists.

**No wire change; `MIN_CLIENT_VERSION` unchanged.** Build-tooling
unchanged: the receiver lives at `android/src/main/java/...`,
compiled into every APK via `android.add_src`; the manifest
`<receiver>` and the server-APK-only `KILL_BACKGROUND_PROCESSES`
`<uses-permission>` injection in `p4a_hook.py:_inject_self_replace_receiver`
both stay (the permission still backs the step-2 fallback on the
server APK).

**User-visible effect.** Once every suite APK in the field is at
0.43.7+, the reboot-to-apply popup becomes unreachable on
package-replace as originally designed. Pre-0.43.7 installs still
need the transitional popup; that branch stays in
`_prompt_server_update`.

## 0.43.6 — `recent.last_langcode` empty-state invariant; first-boot picker-cancel carve-out for `App.stop()`

**Motivation.** Field report 2026-05-18 — user toggled daemon logging
on, asked to switch projects, peer GET ``/v1/recent/last_project`` →
~2 ms later Kivy logged ``[INFO] [Base] Leaving application in
progress... Python for android ended.`` The peer's ``on_resume`` had
interpreted an unexpected ``last_project() == ''`` as "no project
loaded" and called ``App.stop()`` while a sync was mid-flight. Daemon
process kept running and finished the in-flight ``[commit]`` 250 ms
later, but the user perceived the whole thing as a crash.

Diagnosis showed the empty return could in principle leak from a
transient I/O hiccup, a malformed RPC, or a peer accidentally
sending empty — and nothing in the daemon refused to land ``''`` on
disk. This release closes that escape path.

**Daemon-side invariants.**

- ``store.set_last_langcode()`` **refuses empty input** (warns to
  stderr and no-ops). No legitimate caller passes empty;
  defense-in-depth against transient bugs that could otherwise mimic
  a user gesture.
- ``POST /v1/recent/last_project`` with empty body returns
  ``400 empty_langcode``. No legitimate peer issues that POST —
  picker-cancel is a no-op end-to-end, not an RPC. If a peer is
  sending empty, surface the bug rather than silently absorbing.
- ``_h_get_last_project`` emits a one-line transition log on every
  observed change to the returned value (``'<prev>' → '<new>'``,
  sentinel ``'<unset>'`` differentiating the first call per
  process). High-frequency 1 Hz poll path stays log-free; transitions
  give us the diagnostic shape we'd want if this regression ever
  recurs in the field.

**Peer-side contract revision (`CLIENT_INTEGRATION.md` § 14a).**
``last_project()`` returns ``''`` legitimately exactly once in a
device's lifetime — first boot, before any project has ever been
touched. Picker-cancel is a no-op: the picker issues no RPC and no
write, the daemon's ``last_langcode`` is unchanged across the
gesture, and the ``on_resume`` comparison naturally no-ops because
the peer's ``_current_langcode`` is unchanged too. Don't add a
"clear" RPC for picker-cancel; don't write ``''`` from any path.

**One ``App.stop()`` case is carved out: first-boot picker-cancel.**
Fresh install, peer has no ``_current_langcode``, bootstrap opens
the picker, user backs out without picking. The peer has literally
nothing to display and the user has signaled "not now." ``stop()``
is correct *only* in that state — the discriminator is the peer's
own ``_current_langcode is None`` plus a non-selection return from
the picker, not the empty return from ``last_project()``. The
May-18 failure mode (peer calling ``stop()`` with a loaded project
on its way to a project-switch reload) remains banned; the
"What NOT to do" section in § 14a is updated to reflect the
exception explicitly.

No wire-format change. No new status codes. The transition log line
is the only new diagnostic surface; it costs one stderr write per
actual change, which on Android adds maybe ten lines per session.

### azt_collabd 0.43.5 / azt_collab_client 0.43.5 — DoH fallback resolver; DNS_RESOLUTION_FAILED status; watcher logs resolver path on online edge

**DNS-over-HTTPS fallback resolver (`azt_collabd/net.py:_patch_resolver`).**
Field reports keep showing the same pattern: the user can browse the
internet but `dulwich` raises ``NameResolutionError: Failed to resolve
'github.com' ([Errno 7] No address associated with hostname)``. The
distinguishing characteristic is that the failure mode is **app-
specific** — system browser is fine, Python's resolver path is not.
Causes range from per-app data restrictions, captive-portal Wi-Fi in
limbo, broken Private DNS, IPv6-only networks without DNS64, stale
negative caches after a brief outage. Bad-internet resilience is a
primary requirement of the suite (West / Central African field
deployments routinely hit this), and the previous behaviour was just
to surface PUSH_FAILED and hope the user knew to wait.

This release installs a fallback resolver as a side effect of
``_ensure_ssl()`` (which already ran before any dulwich op):
``socket.getaddrinfo`` is monkey-patched. The system resolver runs
**first**, unchanged — on a healthy network the patch is a passthrough
and the DoH dependency is dormant. On ``socket.gaierror``, the patch
issues one DoH JSON query to Cloudflare at the **literal IP**
``https://1.1.1.1/dns-query`` (cert SAN covers ``1.1.1.1`` so TLS
validates against the certifi roots without itself needing DNS; the
literal-IP form makes the DoH path itself loop-free because
``getaddrinfo('1.1.1.1', 443)`` is satisfied by libc without
triggering DNS). Both ``A`` and ``AAAA`` records are queried — the
synthetic ``addrinfo`` list returns AAAA records first to mirror the
order a v6-preferring stack would produce, so urllib3 / socket's
sequential iteration gets a happy-eyeballs-shaped retry path.

Results are cached for 5 minutes keyed by ``(host, port)`` so the
push-retry loop's reconnection attempts don't refire DoH on every
iteration. The lookup is gated on the host *looking like* a hostname
(numeric IPs and AF_UNIX paths bypass), and the patch never
substitutes for the system answer — if system DNS returns a valid
empty answer for a name that just doesn't exist, the DoH path doesn't
run. A new ``_RESOLVER_STATE`` module-global records which path served
the last lookup; ``azt_collabd.net.resolver_state()`` exposes it as
``'system' | 'doh' | 'fail' | 'unknown'``.

**Connectivity watcher logs the resolver path on online edges.**
``scheduler._watcher_loop``'s offline → online edge handler emits
``[watcher] online edge — resolver path: <path>`` so a developer
skimming a field log can tell whether the DoH path was load-bearing
during the session. Healthy networks always show ``system``;
``doh`` flags resolver-class fragility that's silently being routed
around. No new log volume on per-tick basis — edges only.

**New status: ``DNS_RESOLUTION_FAILED``.** Mirrored in both
``azt_collabd/status.py`` and ``azt_collab_client/status.py`` (per the
mirror discipline of hard rule #3), with a translation in
``azt_collab_client/translate.py``. Emitted from
``_add_push_failure(result, exc)`` in ``repo.py`` alongside the
existing ``PUSH_FAILED`` when the exception string matches DNS markers
(``nameresolutionerror``, ``no address associated``, ``failed to
resolve``, etc.) — meaning *both* system DNS and the DoH fallback
failed for the same hostname. Distinct from ``PUSH_FAILED`` so peers
can route silently in the auto-sync path (per the auto/user contract
in ``azt_collab_client/CLAUDE.md`` § "Peer contract: why auto-sync
must be silent"). On user-initiated Sync the peer should toast the
translated message ("Network reachable, but the sync host could not
be resolved…") without navigating — there's no settings change that
will fix this; the daemon will retry automatically when the
underlying network state clears.

**What this does not do** (documented in the README's *DNS
resilience* section): it doesn't fix networks that are genuinely
offline, doesn't bypass per-app firewalls that block 443 outbound,
and doesn't catch the case where DNS succeeds but the resulting IP
is unreachable. Those remain visible as ``PUSH_FAILED`` on the next
sync attempt.

No wire-format change. ``MIN_CLIENT_VERSION`` stays at ``0.43.4``
on the daemon; an older client receiving ``DNS_RESOLUTION_FAILED``
falls through ``translate_status``'s default branch and renders
``[DNS_RESOLUTION_FAILED] {}`` — ugly but non-fatal, and exceptional
in practice (the DoH path makes the underlying NameResolutionError
class of failure rare).

### azt_collabd 0.43.4 / azt_collab_client 0.43.4 — Push-retry no-op merge guard + FF case; settings UI shows the daemon's actual version; collab settings reorder; adaptive push batching for bad internet; one canonical version

**Single source of truth for the suite version.** The two
``__version__`` literals diverged in the field (2026-05-18:
daemon at 0.43.3, client at 0.43.1 → settings strip read
``client 0.43.1  ·  server 0.43.3`` and testers thought the
build was incomplete). Rather than keep two literals + a
"remember to bump both" discipline + a UI explanation for
why they're shown separately, the version now lives in one
place:

- Canonical literal: ``azt_collab_client/__init__.py``
  (``__version__ = "0.43.4"``).
- Re-export: ``azt_collabd/__init__.py`` does
  ``from azt_collab_client import __version__`` so anything
  reading ``azt_collabd.__version__`` keeps working
  unchanged.
- External tooling pointed at the canonical: buildozer
  (``server_apk/buildozer.spec.tmpl :: version.filename``)
  and the presplash generator (``appinfo.py ::
  FILE_W_VERSION``) both read from
  ``azt_collab_client/__init__.py`` directly via regex / file
  read.
- Import direction stays correct: daemon imports client
  (allowed); client never imports daemon (hard rule per
  ``azt_collab_client/CLAUDE.md`` — would break the
  "client works in a separate APK from the daemon" guarantee).

The previously-stated "patch-level bumps in one without the
other are fine" rule is retired — there's literally only one
``__version__`` to bump now. CHANGELOG headers continue to
list both package names (``azt_collabd X.Y.Z / azt_collab_client
X.Y.Z``) as a readability courtesy; the numbers always match.

Surfaced by a field daemon log on 2026-05-18: a transient
``RemoteDisconnected`` on push, followed by GitHub returning
HTTP 400 on the immediate retry, cascaded into **two
unnecessary "merge" commits** before the third push attempt
succeeded. The pre-push branch correctly identified
``local ahead of remote`` (so no initial merge), but the
retry-after-push-failure branch unconditionally re-merged on
every attempt regardless of ancestor relationship, producing
no-op merge commits (two parents, zero files changed) and an
extra push round-trip for each.

- **Retry path uses the same 4-case structure as pre-push.**
  ``_push_step_locked``'s push-retry block now distinguishes:
  (a) remote unchanged → push retry only; (b) local ahead of
  remote (the field log case) → push retry only; (c) remote
  advanced past local → fast-forward local before retry,
  otherwise the next push is non-FF rejected; (d) truly
  diverged → merge. Previously case (b) was guarded but case
  (c) silently fell through to "skip merge, push stale local
  SHA" which would have non-FF rejected on every subsequent
  retry. Diagnostic from the field:
  ``[sync-trace] retry merge done merged_sha=…`` appearing
  with ``base=N ours=N theirs=N`` (all equal) was the
  signature; new traces are
  ``[sync-trace] retry: remote unchanged, push retry only``,
  ``[sync-trace] retry: local still ahead of remote, push
  retry only``, ``[sync-trace] retry: remote advanced;
  fast-forward local``.
- **``CollabUIApp`` version strip now reflects the running
  daemon, not the UI subprocess's compile-time version.**
  Previously the bottom-of-settings ``client X · server Y``
  string used ``azt_collabd.__version__`` from the UI process,
  which equalled the daemon's version only on a coherent
  install — the moment a user updated one half (e.g. ran an
  in-place ``adb install -r`` on the server APK while a
  desktop settings UI was still pointed at an older daemon),
  the strip silently lied. ``on_start`` now spawns the same
  ``_probe_server_version`` worker the picker app uses (off
  the UI thread), calling ``check_server_compat()`` and
  rendering the daemon's reported version into the strip;
  ``server ?`` until the probe lands, ``server ? (reason)``
  if the daemon is unreachable / too old / too new. Field
  utility: lets a maintainer answer "what server is actually
  running?" without adb-shelling into the device.
- **Collab settings page reorganised.** Three field-driven
  changes to the in-daemon Settings page:
    1. **Interface language at the top, no section header.**
       The language-switcher row is the page's first widget
       (after the optional back button) — the row of language-
       name buttons is self-evident, the ``Interface
       language`` SectionLabel was visual noise.
       Share/Update follow, then the contributor field.
    2. **New ``Servers`` section groups everything that talks
       to the network.** GitHub button, GitLab button, the
       conditional Publish row, the ``Work offline:`` toggle,
       and the ``Cache images:`` toggle now sit together under
       one header. Previously Work offline + Cache images had
       their own SectionLabels, a "Suppress push:" prefix row,
       a "Wordlist images" header that dynamically appended
       the wordlist name, and a multiline status BodyLabel
       underneath — all collapsed to a single line each. The
       row's GREEN-vs-SURFACE highlight on yes/no continues
       to show the active state; the status BodyLabels were
       redundant.
    3. **``GitHub Settings`` button reflects daemon answer on
       first open, even on a cold daemon.** Previously the
       button label was driven by a single try-block holding
       both ``get_credentials_status()`` and ``is_online()``;
       any exception in either RPC bailed the whole refresh
       and left the KV-default ``Connect to GitHub`` label in
       place even when the daemon eventually answered
       ``confirmed=true``. Field symptom: button state on
       first open of the settings page was inconsistent
       across launches. Split into two independent try blocks
       so the buttons always update on whatever credentials
       info we have; ``is_online`` failure now degrades the
       Status block but not the action buttons. Plus: detect
       the ``ServerUnavailable`` fallback dict (no
       ``confirmed`` key under ``github``) and schedule one
       follow-up ``refresh()`` 1.5 s later so the daemon-cold-
       start race resolves itself silently.
    4. **Daemon log no longer wiped by daemon respawn.**
       ``install_stderr_tee`` opened the file in ``'w'`` mode
       on every install, including the
       ``_maybe_install_stderr_tee`` call at daemon startup
       — so an idle auto-stop (5 min) or OOM-kill of the
       server APK's ``:provider`` process followed by a
       lazy-respawn for the next peer RPC truncated the file
       and left only the freshly-printed ``[daemon-log]
       mirroring stderr to …`` line. Field symptom: testers
       reported "I turned the log on, used the app, hit
       Share — got one line." Fix: ``install_stderr_tee``
       gained a ``truncate`` kwarg (default ``False`` →
       append mode); only the explicit user toggle-on path
       (``_h_set_daemon_log_to_file(enabled=True)``) passes
       ``truncate=True`` to start a clean session. The
       mirroring-stderr line is annotated ``(fresh
       session)`` vs ``(appending — daemon respawn)`` so a
       reader can tell which install they're looking at.
       ``_h_get_daemon_log`` already caps reads to the last
       256 KB, so unbounded growth across many respawns is a
       non-issue.
    5. **Debug "Toggle service not responding" surface
       removed from the Settings page.** The
       ``$AZT_HOME/_debug_force_503`` sentinel daemon-side
       (``server.py:_h_health``) is unchanged — testers can
       still plant it via ``adb shell run-as
       org.atoznback.aztcollab touch
       files/azt/_debug_force_503`` — and the
       ``toggle_debug_503`` / ``_refresh_debug_503_state`` /
       ``_debug_503_path`` methods on ``SettingsScreen`` stay
       for REPL-callable convenience and future UI re-add.
       Just the KV widgets (SectionLabel + NavBtn + state
       BodyLabel) are gone — debug clutter the typical user
       never wanted to see.
- **Client conformity contract:
  ``CLIENT_INTEGRATION.md`` § 17b adds the "Badge refresh
  obligation".** Daemon has no peer-push channel; in the
  split-commit world peers MUST re-call
  ``project_status(langcode)`` after every sync gesture, on a
  5-15 s background tick, and on ``on_resume``. The gesture's
  own ``Result`` no longer encodes push state (push happens
  on the daemon's drain loop, not in the gesture's
  transaction), so peers that read ``commits_ahead`` only
  from the gesture result leave the badge stuck at
  last-gesture-time forever. Closes a 2026-05-18 field
  report where a peer's ``(+160)`` indicator persisted
  despite the daemon log showing successful
  ``[sync-rpc] done: codes=['NOTHING_TO_COMMIT', 'PUSHED']``.
  Migration-checklist item 5 added.
- **Adaptive push batching for bad-internet large queues.**
  ``_push_step_locked`` now starts with the historical
  single-transaction push of the local tip — no extra
  round-trips on the happy path. On a network-class push
  failure (``RemoteDisconnected`` / ``IncompleteRead`` /
  ``NameResolutionError`` / GitHub's ``unexpected http resp
  4xx`` from a server-side timeout on a slow upload), the
  loop halves the commit count it tries to push next: parks
  an intermediate SHA under ``refs/azt-collab/partial_push``
  and pushes that. Halves again on further failure. Once a
  partial push lands, locks in the working batch size and
  drains the remaining chunks at that size until the queue
  clears. Exponential backoff (1 s → 16 s cap) between
  retries. Bounded by 12 consecutive failures (resets on any
  successful push) — comfortably above ``log2(160)`` for
  queues of 160+ commits with retry headroom. The
  race-with-another-peer's-push path (re-fetch shows the
  remote moved) is preserved as a parallel branch: when
  detected, the four-case ancestor logic resolves it
  (FF / merge / no-op / already-in-sync) and the adaptive
  loop resets to push the new tip from scratch. Helpers
  added: ``_is_network_push_failure(exc)``,
  ``_count_commits_between(repo, ancestor, descendant)``,
  ``_pick_intermediate_sha(repo, base, tip, n)``. New
  trace lines: ``[sync-trace] push loop begin
  commits_to_push=N``, ``[sync-trace] push attempt
  target=<8hex> chunk_n=N``, ``[sync-trace] retry: halving
  chunk_n N → N/2``, ``[sync-trace] batch size locked at
  N``. Closes the low-power-phone-on-bad-internet failure
  mode where a 160-commit backlog produced a single ~10 MB
  pack that GitHub's git-receive-pack timed out on, with no
  recovery short of finding better Wi-Fi.
- **Client conformity contract: ``CLIENT_INTEGRATION.md``
  § 14a now explicitly prohibits ``App.stop()`` as the
  project-switch reload mechanism.** Closes a 2026-05-18
  field report: user enabled daemon logging, asked to switch
  projects, peer ``GET /v1/recent/last_project`` → 2 ms
  later Kivy logged
  ``[INFO] [Base] Leaving application in progress... Python
  for android ended.`` Daemon process kept running and
  finished the in-flight commit 250 ms after the peer
  exited; user perceived it as "the app crashed when I
  switched projects." Android does NOT auto-restart a peer
  that exits via ``App.stop()``, so the user has to
  relaunch from the home screen every time they switch. The
  contract already documented "reload via the same code
  path the picker-result handler uses"; the new explicit ❌
  bullet names the anti-pattern and the field-log signature
  so the next peer audit catches it.
- **CAWL: root-level images no longer log spurious "flat
  basename not in index".** ``_resolve_basename_via_index``
  returned the same string in two distinct cases — "matched
  but the canonical path *is* the basename" (e.g.
  ``Image-Not-Found.png`` at the top of
  ``kent-rasmussen/images_CAWL``) and "no entry matched" —
  and the caller in ``get_image_path`` couldn't distinguish
  them. Every fetch of a root-level image logged ``[cawl]
  get_image_path: flat basename not in index: …`` even
  though the asset was in the index and the subsequent fetch
  succeeded. Field log 2026-05-18 showed the spurious line
  with no follow-up ``image fetch failed``, exactly because
  the asset was present. Fix: ``_resolve_basename_via_index``
  now returns ``(resolved_path, found)``; the caller logs the
  "not in index" line only when ``found is False`` and a
  cached index exists. Root-level matches stay silent. Six rules for
  how peers must shed load instead of leaning on daemon-side
  protections: single-in-flight guard per (RPC, project);
  auto-paths use ``commit_project`` not ``sync_project``;
  peer-side debounce of the Sync button; budgeted background
  polls (5-15 s for the active project, 30 s+ for per-
  project iterations, stop when backgrounded); ``S.BUSY``
  is "back off" not "retry"; per-event triggers not
  per-status-change triggers. Closes a 2026-05-18 field
  report where a peer fired six parallel
  ``POST /v1/projects/<lang>/sync`` per gesture; the
  daemon's project_lock did its job by refusing them with
  ``S.BUSY``, but the peer surfaced each refusal as
  "Une autre synchronisation est en cours" toast — a wall
  of noise the user couldn't act on. ``S.BUSY`` added to
  the § 17 routing table (silent on both auto and user
  paths) and to the § 17 constants list. § 17 auto-sync
  code example updated to use ``commit_project`` +
  ``poll_job`` (it was still showing the pre-0.43
  ``sync_project`` shape, which is exactly the
  anti-pattern § 17c Rule 2 closes). Daemon-side
  protections summarised at the end of § 17c so future
  peer maintainers know what NOT to reinvent.

### azt_collabd 0.43.1 / azt_collab_client 0.43.1 — Polish + bootstrap parity guard

Patch follow-up to 0.43.0. One small behaviour change in the
bootstrap probe (the parity guard); everything else is pure
cleanup.

- **Bootstrap: no "newer available" popup at version parity.**
  Closes NOTES_TO_DAEMON.md filed by azt-recorder 1.45.0
  (2026-05-15). `_peer_update_with_confirm._probe` was firing
  the self-update popup with the *currently-installed* version
  in the message when (a) the user had just `adb install -r`'d
  the freshly-published release, (b) `peer_version == latest`,
  and (c) the last-seen-digest peer_pref was a leftover from
  an earlier version's session. Phantom "digest_changed=True
  at parity" signal. New `at_version_parity` flag suppresses
  `digest_changed` whenever `peer_version == latest`; the
  parity-with-stale-baseline case folds into the existing
  silent re-baseline branch (was `unknown_baseline`-only, now
  also covers `stale_baseline_at_parity`). Cost: a legitimate
  same-tag re-upload won't pop until either the maintainer
  bumps the tag or the next dev-loop install picks up the new
  bytes naturally. Benefit: dev-loop installs of just-
  published builds no longer surface a same-version-to-self
  prompt.
- **i18n: peer-side language re-sync hook +
  ``translate.tr`` fallback dropped.** Closes
  NOTES_TO_DAEMON.md filed by azt-recorder 1.45.0
  (2026-05-16) — "only `'More info'` renders translated" on
  the voluntary-update popup. Root cause was the peer's
  `add_fallback` target capturing the client `_current` at
  peer startup and never refreshing when bootstrap's
  `_sync_ui_language_with_daemon` swapped the client catalog
  out from under it. The `translate.tr` second-chance retry
  to `_client_tr` (the "if host returned msgid, try client"
  safety net) hid the issue for some strings but not
  others, depending on whether the host catalog returned the
  msgid unchanged. New `i18n.subscribe_language_change(cb)`
  API: peers register a callback that re-creates their own
  `gettext.translation` in the new language and re-calls
  `add_fallback(client_i18n.gettext_translation())` so the
  chain keeps pointing at the *current* client catalog.
  `set_language` invokes every subscriber after the swap +
  persist; failures are logged, not raised. `translate.tr`
  simplified to a straight delegation — no retry. Peer
  obligation documented in `CLIENT_INTEGRATION.md` § 6.

- **French catalog filled.** 32 previously-empty ``msgstr``
  entries translated, covering the GitHub-collaborator-invite
  flow, the bootstrap install / update / reboot prompts,
  popups (``Invite collaborator``, ``owner/repository``,
  ``Sending invitation…``, ``Invite failed: {error}``,
  ``Open AZT Collaboration``, ``More info``), update.py
  status strings (``Downloading…``, asset-filename failures),
  ``Sync was interrupted; please retry.``, and ``Please set
  your name in the sync settings before publishing or
  syncing.`` ``Project-Id-Version`` header bumped to match.
- **41 catalog orphans removed.** Stale ``msgid`` entries
  from features that were removed or rephrased: ``Active
  host``, ``Copy URL``, ``Disconnect GitHub`` / ``GitLab``,
  ``GitHub`` / ``GitLab`` standalone, ``GitLab credentials``,
  ``Refresh``, ``Save``, ``Save daemon log to file`` / ``Stop
  saving daemon log``, ``Set GitLab credentials``, ``Test
  connection``, ``Update needed`` / ``Update this app`` /
  ``Updating {name}…``, four bootstrap strings now phrased
  differently, two ``Opening {uri}…`` variants, ``Tap
  "Begin"…``, ``Connected as {username}.`` (kept the
  ``…Credentials saved.`` variant), ``Error setting host:
  {error}``, ``Saved for {username}.``, ``Copied to
  clipboard.`` / ``Could not copy: {error}``, ``Could not
  open settings: {error}``, ``e.g. Kent Rasmussen``, ``Install
  it to enable sync, then reopen this app.``, ``Paste the
  repository URL here``, and ``Share this app`` (orphan
  beside the live ``Share app``). Also deduplicated ``Share
  this app`` which appeared twice — both removed since
  neither was referenced.
- **``examples/sister_app.py`` rewritten** as a read-only
  daemon survey. Previously took a working-tree path and
  did ``register_project`` + ``commit_project`` — a
  bootstrap demo masquerading as a peer. Now takes no
  args and prints every field the client gets from the
  daemon: reachability + version compat, contributor +
  device_name + credentials, ``sync.work_offline``,
  registered project list, full ``project_status`` for
  ``last_project()`` including the new 0.43.0 fields.
  Interactive prompt with ``p`` to open the picker
  subprocess, ``s`` to open the daemon settings UI
  subprocess, ``r`` to refresh, ``q`` to quit. After
  ``p`` returns the survey re-prints so changes are
  immediately visible. References in ``README.md`` /
  ``CLAUDE.md`` updated to match the no-arg form.
- **Test fix.** ``test_scheduler_run_sync_refuses_when_
  contributor_unset`` renamed to ``test_scheduler_run_
  commit_refuses…`` and now calls ``_run_commit`` (the
  0.43.0 rename of ``_run_sync``).

### azt_collabd 0.43.0 / azt_collab_client 0.43.0 — Split commit and push; daemon-driven push policy; ``sync.work_offline`` toggle

Closes the NOTES_TO_DAEMON.md item filed by azt-recorder
1.43.1 (2026-05-15): debounced ``request_sync`` skipped the
commit step entirely while offline, so a field-session of
swipes piled up dirty files with ``commits_ahead=0,
n_changes=N`` rather than the per-swipe commits a user
expects. Synchronous ``sync_project`` (Sync button) committed
fine under the same offline conditions, proving the commit
step itself wasn't network-gated — only the debounced
pipeline was misordered.

Rather than patch the early-return inside ``_run_sync``, the
whole commit/push relationship is rethought: peers decide
where to cut a commit, the daemon decides when (and whether)
to push.

- **``commit_project(langcode)`` replaces ``request_sync``**
  (client + daemon). Same debounce / async / job_id /
  poll_job machinery — narrower contract. The RPC is now
  commit-only: it stages, commits, marks ``pending_push``,
  and returns. No fetch, no merge, no push. The old
  ``request_sync`` name kept as a backwards-compat alias in
  the client; the old ``/v1/projects/<lang>/sync_async``
  URL routes to the new ``commit`` handler on the server.
  Old peer code keeps working — the only behavioural change
  is the result no longer carries ``PUSHED``. Migrate
  result-handling that polls for ``PUSHED`` over to the
  scheduler-driven model.
- **Push moves entirely to the scheduler's drain loop**
  (``azt_collabd/scheduler.py``). The connectivity watcher
  tracks ``_online_since`` on offline→online edges and only
  fires the drain once ``now - _online_since >=
  settings.post_online_grace_s`` (default 60 s). Brief
  tethers the user enabled for something else don't burn
  their MB on pending pushes. The drain also respects the
  ``sync.work_offline`` master toggle.
- **``sync.work_offline`` toggle** —
  ``GET/POST /v1/config/work_offline``, persisted to
  ``$AZT_HOME/config.json``. When on, the watcher drain is
  a no-op and the user-gestured Sync button (``sync_project``)
  returns ``S.WORK_OFFLINE_ENABLED`` without attempting
  any push. Commits via ``commit_project`` are unaffected;
  only push is suppressed. Toggling OFF fires an immediate
  drain so the user doesn't wait a full
  ``connectivity_poll_s`` tick.
- **``S.WORK_OFFLINE_ENABLED`` status code** (mirrored
  daemon + client). Peers route the same way they handle
  ``AUTH_REQUIRED``: toast + ``open_server_ui()`` to the
  daemon settings screen anchored on the toggle.
- **Daemon settings UI**: new "Work offline" section with
  yes/no buttons, in ``azt_collabd/ui/app.py`` (above
  Diagnostic log). State refreshes on screen entry; toggling
  OFF fires the immediate drain server-side.
- **``ProjectStatus.work_offline``** carries the
  daemon-wide bool on every ``project_status`` response so
  peers can render a badge alongside ``commits_ahead`` —
  "5 commits waiting · offline mode" — without a second
  RPC.
- **``repo.py`` factored** into ``commit_repo``
  (stage + commit, no network) and ``push_repo`` (fetch +
  merge + push, no commit). ``sync_repo`` kept as the
  combined entry point for the user-gestured Sync button and
  legacy ``commit_audio_and_sync``; internally it now calls
  ``_commit_step_locked`` then ``_push_step_locked`` under
  one project lock.
- **``scheduler._drain_stuck_commits`` is now commit-only**
  (calls ``commit_repo`` instead of ``sync_repo``). Push for
  recovered commits happens via the regular drain pass.
- **``scheduler.is_online_cached()``** exposes the watcher's
  most recent observation as a module-level bool read —
  callers that don't need a fresh 3–6 s TCP probe should
  use this instead of ``net._has_internet``. Internal
  caller-only for now; not on the wire.
- **MIN_CLIENT_VERSION / MIN_SERVER_VERSION** lock-stepped
  at 0.43.0. Hard requirement: a 0.43 peer against a pre-
  0.43 daemon would still lose offline commits (the bug
  this release fixes); a pre-0.43 peer against a 0.43
  daemon would never observe ``PUSHED`` codes from
  ``commit_project`` and could mis-render its sync state.

### azt_collabd 0.42.0 / azt_collab_client 0.42.0 — Package-replacement: receiver reaps in-APK; drop peer KILL_BACKGROUND_PROCESSES; installed-vs-running reboot prompt

Follow-up to 0.41.28's suite-wide ``SuiteSelfReplaceReceiver``.
The earlier design paired a manifest receiver in every APK with a
peer-side ``killBackgroundProcesses(<server_pkg>)`` backstop on the
Check-again paths to handle OEMs that don't auto-kill the old
process during a package replace. The backstop required peers to
declare ``KILL_BACKGROUND_PROCESSES`` in their own
``android.permissions``, which is one more permission to explain
if/when the suite ever goes through a store review.

The new design moves the reap into the receiver itself: the
freshly-installed APK's receiver calls
``killBackgroundProcesses(getPackageName())`` (its own package's
old-code processes) before the self-kill. No cross-package kill;
no peer permission needed. A small peer-side reboot prompt
covers the migration window for users whose currently-installed
daemon is pre-0.42 (no in-receiver reap).

Minor bump because the receiver behaviour change is observable
across the suite and the peer permission surface area shrinks —
worth lock-stepping daemon + client at 0.42.0 to make the
"upgrade past this line" point unambiguous.

- **``SuiteSelfReplaceReceiver``** now calls
  ``ActivityManager.killBackgroundProcesses(getPackageName())``
  before ``Process.killProcess(myPid())``. ``SecurityException``
  is caught and logged — if the permission injection were to fail
  for any reason, the receiver still self-kills (i.e. degrades to
  the 0.41.28 behaviour on that APK).
- **``p4a_hook.py:_inject_self_replace_receiver``**: the
  ``<receiver>`` block stays on every suite APK; the
  ``<uses-permission android:name=
  "android.permission.KILL_BACKGROUND_PROCESSES" />`` element
  is gated on ``dist_name == 'aztcollab'`` so only the server
  APK ends up with the permission in its merged manifest. Peers
  compile the same Java receiver class but their manifest
  declares only the ``<receiver>``; at runtime the receiver's
  reap call hits ``SecurityException``, is caught, and it falls
  through to its self-kill — same net behaviour as the 0.41.28
  peer receiver, with no peer permission to explain. Anchor
  fixed to ``<application `` (with trailing space) so the
  injection doesn't land inside the explanatory comment in
  ``server_apk/manifest_extras.xml`` (whose prose mentions the
  literal ``<application>``). Idempotent via the
  ``self-replace-permission-injection`` sentinel.
  Reported by azt_recorder 1.42.29 via NOTES_TO_DAEMON.md.
- **Peer-side backstop removed.**
  ``azt_collab_client.ui.bootstrap._kill_server_background`` is
  deleted. The Check-again paths simply invalidate the release
  cache and re-enter ``_check_server`` — the next bind picks up
  the new code from the freshly-installed APK whose receiver did
  the reap during install.
- **Installed-on-disk vs. running detection.**
  ``azt_collab_client.ui.bootstrap._installed_server_version``
  reads the server APK's ``versionName`` via
  ``PackageManager.getPackageInfo``. ``_prompt_server_update``
  compares that to the version /v1/health reports; if installed
  > running, the user has sideloaded the new APK but Android
  kept the old daemon process alive (the pre-0.42 case where
  the receiver doesn't auto-reap). Instead of asking the user
  to re-download, ``_prompt_server_reboot_to_apply`` surfaces a
  "You have {installed} installed; running process is {running}.
  Restart your device to switch to the newer version." popup
  (Check again + Quit + maintainer mailto). Pure transition
  helper — once every field daemon is at 0.42 or newer the
  receiver's in-APK reap fires and the comparison should never
  trigger.
- **§ 2 (peer permissions)** updated: ``KILL_BACKGROUND_PROCESSES``
  no longer listed. The new note explicitly tells peer maintainers
  not to add it themselves.
- **§ 19 (package-replacement contract)** rewritten: the
  two-step receiver contract (reap then self-kill), the
  permission injection model, and the "peers MUST NOT add this
  permission" rule replace the prior peer-backstop section. The
  rollout-window note covers what happens for users still on
  pre-0.42 server APKs — peer surfaces the reboot prompt
  automatically.

No wire-format change. ``MIN_SERVER_VERSION`` / ``MIN_CLIENT_VERSION``
unchanged — the new behaviour is internal to the Java receiver,
the build-time manifest injection, and the peer-side bootstrap
flow.

### azt_collabd 0.41.29 / azt_collab_client 0.41.27 — Atomic-write orphan auto-recovery

Background. The ``atomic_open_write`` protocol is two-phase: peer
streams full LIFT bytes into ``<working_dir>/.azt_atomic_pending/
<token>``, then a separate ``atomic_finalize`` RPC renames the
scratch over the real LIFT. A crash, daemon kill, or transport
break between the two phases leaves the scratch on disk —
complete, well-formed LIFT, but never landed. The two orphans
field-reported in this session were exactly that: complete LIFT
files, sitting in ``.azt_atomic_pending/``, never finalized.

- **New module** ``azt_collabd/atomic_recovery.py``. Scans each
  registered project's ``.azt_atomic_pending/`` directory for
  orphan files ≥ 60 s old (skip in-flight Phase-1 writes) and
  classifies each:

  - Hash-equal to current LIFT → delete (confirmable garbage).
  - All shared guids byte-identical in canonical XML AND no
    orphan-only entries → delete (subset; no new info).
  - Otherwise → run ``lift_merge.three_way_merge(base=b'',
    ours=current, theirs=orphan)``. Write merged bytes
    atomically to the LIFT path, commit as ``"Recovered orphan
    from <iso-timestamp>"`` (author + committer = suite bot,
    same identity used for cross-peer merges). Conflicts get
    the existing ``<annotation name="azt-lift-conflict">``
    treatment — peers / viewers that already surface those
    annotations see recovery conflicts without any new code.
  - Merge raises (corrupt XML, broken byte stream from an
    interrupted Phase-1 write that *looked* > 60 s old) or
    any of lift_merge's guard kinds fire (parse-error,
    truncation-suspected, catastrophic-output) → move the
    orphan to ``.azt_atomic_orphans/unmergeable/<token>.lift``
    for manual inspection.

- **Scheduler integration.** ``scheduler._drain_atomic_orphans``
  runs every watcher tick (default 30 s) alongside the existing
  stuck-commit drain. Cheap when nothing is pending (single
  ``os.listdir`` on a typically-empty directory). Each
  non-trivial outcome logs to the daemon log.

- **ProjectStatus diagnostic.** New field
  ``n_recovered_today: int`` on the project_status response and
  the client-side ``ProjectStatus`` dataclass — purely
  informational, zero on healthy projects, positive when
  Phase-1-only writes were merged back in. Resets at the day
  boundary via ``last_recovery_day`` in projects.json.

- **No user-facing prompt.** In a no-delete-of-LIFT-entries world
  the merge is unambiguously lossless (orphan only ever has
  guids that current also has, plus potentially new field
  content); a "Merge or Discard?" prompt would ask users a
  question most aren't competent to answer, and the safe answer
  ("merge") is the only reasonable default anyway. Conflicts
  flow through the existing annotation channel.

- Versions: daemon 0.41.29 / client 0.41.27. Additive on the
  wire (new ProjectStatus field; pre-0.41.27 clients ignore
  unknown keys; pre-0.41.29 daemons emit nothing for it). No
  MIN floor bumps needed.

### azt_collabd 0.41.28 / azt_collab_client 0.41.26 — Suite-wide package-replacement handling: APK install now reaches the running process

Symptom this closes: a user sideloads the required server APK in
response to the peer's ``client_too_old`` prompt, relaunches the
peer, and still hits the same "AZT collab x.y.z or newer is
required" popup. The new APK is on disk; the OLD process is
still serving the provider with the OLD version. "Wait for an
update" is the wrong instruction — the update is right there.

- **Suite-wide ``MY_PACKAGE_REPLACED`` receiver.** New Java class
  ``org.atoznback.aztcollab.SuiteSelfReplaceReceiver`` at
  ``android/src/main/java/...`` handles the broadcast by
  self-killing the receiving process. ``p4a_hook.py`` grows
  ``_inject_self_replace_receiver`` to inject the manifest
  ``<receiver>`` into every APK in the suite (NOT gated on
  ``dist_name`` — server + every peer get it). Manifest receiver,
  NOT runtime: some Android versions / OEMs kill the old process
  as part of the replace, so a runtime-registered receiver
  wouldn't be alive to receive the broadcast; manifest receivers
  cold-start the new APK's code to deliver, which is exactly
  what we want.
- **Peer-side backstop.**
  ``azt_collab_client.ui.bootstrap._kill_server_background``
  dispatches
  ``ActivityManager.killBackgroundProcesses(<server package>)``
  from the Check-again paths in ``_do_check_again``. Belt-and-
  braces for the rollout window before every field server APK
  ships the receiver, and for the rare case where the
  ``MY_PACKAGE_REPLACED`` broadcast didn't fire. Harmless when
  the server is healthy (the next call lazy-spawns from the
  current APK either way), curative when it's stale.
- **Peer permission.** ``KILL_BACKGROUND_PROCESSES`` added to
  the required peer permissions in ``CLIENT_INTEGRATION.md``
  § 2. Normal-protection, no runtime grant prompt. Without it
  the helper raises and falls through to the legacy behaviour
  (user's next launch eventually picks up the new code once
  the OS recycles the old process for its own reasons).
- **Contract codification.** New § 19 "Package-replacement
  handling" in ``CLIENT_INTEGRATION.md`` formalises the rule:
  every suite APK MUST self-handle ``MY_PACKAGE_REPLACED``;
  peers MAY backstop with ``killBackgroundProcesses``; peers
  MUST NOT assume on-disk APK matches the running server
  process without verifying.

### azt_collabd 0.41.27 / azt_collab_client 0.41.25 — COMMIT_REPEATEDLY_FAILED + scheduler-driven retry: catch the "164 files in one commit" pattern even when the user is idle

User report: production commits arriving with ~164 files apiece, hours
or days of recording sessions, after long silent stretches where
nothing pushed at all. The pattern is "failure to commit for some
time, followed by a successful catchup commit." Until now the daemon
shipped a one-shot ``S.COMMIT_FAILED`` per failed attempt with no
across-attempts memory, so a streak of failures looked indistinguish-
able from one unlucky retry — the user kept recording, files piled up
on the device's daemon-private filesDir, and the eventual catchup
commit hid the magnitude of the gap.

- New status code ``S.COMMIT_REPEATEDLY_FAILED``: surfaced when the
  same project has hit ``S.COMMIT_FAILED`` two-or-more times in a
  row. Counter persisted at ``projects.json :: <langcode>
  .commit_failure_count``, bumped on every COMMIT_FAILED branch,
  cleared on every successful commit. Threshold = 2 because
  dulwich's ``porcelain.commit`` essentially only raises on
  persistent conditions (index corruption, refs problem, disk
  full, broken repo state); one failure can be a fluke, two means
  the underlying problem isn't self-healing. ``count`` and the
  last dulwich ``error`` ride the status params.
- **Scheduler-driven retry.** The connectivity-watcher loop now
  also drains stuck commits every tick (default 30 s) with
  exponential backoff (30, 60, 120, … s, capped at 1 hour). An
  idle device with a failed commit gets a second look without
  the user having to gesture the peer; recovery from a
  transient cause (lock released, disk freed, daemon restart)
  clears the counter automatically. Implementation:
  ``scheduler._drain_stuck_commits`` in
  ``azt_collabd/scheduler.py``.
- **``ProjectStatus`` exposes the streak (diagnostic).** The
  ``project_status`` RPC response gains
  ``commit_failure_count`` + ``last_commit_failure_at`` +
  ``last_commit_error`` for diagnostic surfaces (settings
  screens showing "last commit error: …"). The alarm itself
  still flows through ``result.statuses`` only — the counter
  persists between gestures, so the next peer-driven sync
  after a background failure naturally sees the elevated
  counter and carries ``COMMIT_REPEATEDLY_FAILED`` on its
  result. Peers do not need to synthesize the alarm from the
  polled count; § 17a in ``CLIENT_INTEGRATION.md``
  documents this explicitly.
- Routing: ``CLIENT_INTEGRATION.md`` § 17 lands the code in the
  same never-silenced bucket as ``DATA_LOSS_RISK`` — auto-sync
  still must surface it (silencing would hide active data loss
  in exactly the catchup-commit pattern the bug was filed
  against). The auto-sync code shape now iterates and surfaces
  both codes before the silencing branches consume the result.
- Translation: client catalog + French ``.po`` carry a
  data-loss-class user-visible message that names "Settings →
  Diagnostic log → Log server activity = yes, then Share daemon
  log so we can investigate" — same shape as the
  ``DATA_LOSS_RISK`` message, since the investigation surface
  is identical (the daemon log will show *why* the commits
  failed).
- ``MIN_CLIENT_VERSION`` ↑ 0.41.25, ``MIN_SERVER_VERSION``
  ↑ 0.41.27 — new status code + new ProjectStatus fields;
  pre-this-version clients have no translation and no
  poll-surface, falling back to the auto-sync result iteration
  alone.

### azt_collabd 0.41.21 / azt_collab_client 0.41.21 — Scan QR: fix IntentIntegrator autoclass path + bundle AndroidX transitively; multi-density server-APK presplash

Plus, adopting the multi-density splash pattern from
``NOTES_TO_DAEMON.md`` "be eager when you have room to" §9 for the
server APK itself:

- ``generate_presplash.py`` rewritten to emit one PNG per Android
  density bucket (ldpi 0.75x → xxxhdpi 4x, mdpi 320×533 baseline)
  under ``server_apk/presplash_variants/drawable-<bucket>/presplash.png``.
  Fonts and icon are scaled per bucket so each variant is sharp
  at its native size. The legacy hdpi-sized
  ``server_apk/presplash.png`` is also rewritten as the
  ``presplash.filename`` rare-fallback.
- ``server_apk/buildozer.spec.tmpl`` grows an
  ``android.add_resources`` listing pointing at the six bucket
  variants, so Android's resource resolver picks the right one at
  install / launch time. No runtime PIL-resize on first boot.

Run ``python generate_presplash.py`` once before each release
build to refresh the version stamp; the produced
``presplash_variants/`` is build output (gitignore candidate) but
the spec entry is permanent.

Plus the rest of the "be eager when you have room to" asks
filed under the same note:

- **``CLIENT_INTEGRATION.md`` § 18 "Low-power adaptive policy"**
  documents the three rules (OS signals not user toggle;
  automatic for resource decisions, user-facing for content /
  workflow; pre-built variants beat runtime regeneration),
  the gate-vs-don't-gate inventory, the multi-density
  ``android.add_resources`` recipe, the verification block,
  and the diagnostic-logging shape. ``CLAUDE.md`` carries the
  rationale (why automatic, why build-time-work-in-the-build).
- **``azt_collab_client.lowpower``** ships as a new module —
  the JNI plumbing peers were duplicating, plus a single source
  of truth for the thresholds (3 GB / 6 GB tier cuts, 0.15
  availMem ratio, 720 px lowMemory downsample). API:
  ``total_ram_mb()``, ``memory_state()``, ``is_low_memory()``,
  ``is_metered_network()``, ``have_room_for_prefetch()``,
  ``ram_tier()``, ``densityDpi()``, ``dpi_to_bucket()``,
  ``identify_drawable_variant()``, ``log_presplash_variant()``.
  Thresholds are module-level constants, override before first
  call. ``AZT_FORCE_LOW_MEMORY=1`` env flips every signal to its
  budget-device value for local testing.
- **Diagnostic recipe corrected.** The first-pass recipes
  (``Drawable.getIntrinsicWidth/Height()``,
  ``BitmapDrawable.getBitmap().getDensity()``) both reported
  device-scaled state and silently collapsed every bucket on
  any given device. ``identify_drawable_variant`` uses
  ``BitmapFactory.decodeResource`` with ``inJustDecodeBounds=
  true`` + ``inScaled=false`` instead — ``opts.outWidth`` /
  ``opts.inDensity`` then carry the native pixel width / source
  folder density of the file Android actually picked, so the
  bucket name can be identified unambiguously.
- **Server APK logs its own variant.** ``server_apk/main.py``
  calls ``log_presplash_variant(tag='presplash:server')`` at
  startup; sister apps log under their own distinct tag (e.g.
  ``'presplash'``) so combined logcat is grep-able.

**Daemon-driven CAWL prefetch: offline-gate + circuit breaker.**
0.41.4 added daemon-side offline backoff; 0.41.8 dropped it
because "the peer has a circuit breaker"; 0.41.11 moved iteration
into the daemon's ``_prefetch_worker`` and the peer's circuit
breaker silently stopped applying (it lived in the old per-image
peer iteration model). Net result on an offline boot: the daemon
hammered DNS for every entry in the requested paths list,
producing logcat spam shaped like ``[cawl] image fetch failed for
… URLError: <urlopen error [Errno 7] No address associated with
hostname>`` repeated N times in ~40 ms intervals.

``_prefetch_worker`` now:

1. Checks ``net._has_internet()`` once at start. If offline,
   marks state ``skipped_offline=True`` / ``finished=True`` and
   returns immediately — no iteration, no spam.
2. Tracks consecutive failures inside the loop. After
   ``_PREFETCH_CONSECUTIVE_FAIL_LIMIT`` (3) back-to-back
   ``get_image_path`` failures, marks ``circuit_open=True`` /
   ``finished=True`` and bails. Real fetches succeed in
   <500 ms; three offline-class failures bunched together mean
   the device dropped connectivity, not three individually
   missing files.

``_make_prefetch_state`` grows two fields (``skipped_offline``,
``circuit_open``) and a ``started_at`` timestamp.

**``cache_status`` surface widened.** ``cache_status(repo)`` now
returns a dict instead of a ``(cached, total)`` tuple:

```
{'cached': int, 'total': int,
 'offline': bool, 'circuit_open': bool,
 'finished': bool}
```

The ``GET /v1/projects/<lang>/cawl/cache_status`` HTTP response
gains the same three flags. When the worker was offline-skipped,
``cached`` falls back to the actually-on-disk count via
``_walk_image_count`` — so a device with prior cache shows e.g.
"1247 / 3000 (offline)" instead of "0 / 3000" each offline boot.

**Daemon settings UI banner** rendered three ways now:

- normal: ``Caching images: M / N (network in use — please stay online)``
- offline-skipped: ``Image cache: M / N (offline — will resume when online)``
- circuit-broken: ``Image cache: M / N (paused — connectivity lost)``

Old peers reading only ``cached`` / ``total`` from the response
keep working — the new flags are additive.

**Stage A: daemon-driven auto-prefetch.** The daemon now owns
the "warm the CAWL image cache" decision instead of waiting
for a peer-driven ``cawl/prefetch`` POST. ``_touch_project``
(which fires on every langcode-bound endpoint) now also calls
``cawl.auto_prefetch(repo)``. ``auto_prefetch``:

- Resolves the full index image path set via the cached
  index (no network).
- Throttles to at most one trigger per repo per 30 s, so the
  1 Hz cache-status poll doesn't re-probe ``_has_internet``
  every second.
- Defers to ``start_prefetch``'s existing idempotency. A
  running prefetch with matching paths is a no-op; a finished
  prefetch (success OR offline-skipped) restarts, which is the
  natural retry path when connectivity may have returned.

Peers may continue to POST ``cawl/prefetch`` with their own
working-set list — useful when the peer wants to warm a
subset different from the full index. The endpoint is
backward-compatible. Stage B (peer-side removal of the POST)
ships in a later peer release; today's change is additive.

**Offline → online auto-resume.** The scheduler's
connectivity watcher already fires on the offline → online
edge (every ``connectivity_poll_s``, default 30 s). On that
edge it now also calls ``cawl.on_online_edge()`` which clears
the auto_prefetch throttle for any repo whose last state was
``skipped_offline`` or ``circuit_open`` and re-fires
``auto_prefetch``. Cache warming resumes within ~30 s of
network return with no user action required.

The cache-status banner poll **stays at 1 Hz** even on
offline / circuit_open state — the response is just
in-memory dict lookups, and the ``[first-try]`` probe for the
cache_status path is already suppressed (see below). Keeping
the poll running is what makes the banner auto-update from
"offline — will resume" to live progress when
``on_online_edge`` does its work.

**CAWL prefetch policy: one variant per id (default) vs. all
variants.** New config knob
``$AZT_HOME/config.json :: cawl.prefetch_all_variants``,
default ``False`` — daemon's auto_prefetch warms one image
per CAWL id (the file whose basename contains the canonical
``__`` preferred-variant marker, falling back to the first
file in the id directory if no variant carries the marker).
Set to ``True`` to warm every image-shaped index entry —
heavier on network and disk but useful for users who want
the full set offline.

API surface:

- Daemon: ``store.get_cawl_prefetch_all_variants`` /
  ``store.set_cawl_prefetch_all_variants(bool)``.
- HTTP: ``GET / POST /v1/config/cawl_prefetch_all_variants``,
  body ``{enabled: bool}``.
- Client: ``azt_collab_client.get_cawl_prefetch_all_variants``
  / ``set_cawl_prefetch_all_variants``.
- Filter logic: ``cawl._filter_preferred_variant_per_id``
  applied inside ``_index_image_paths`` whenever
  ``prefetch_all_variants`` is False.

Flipping the policy doesn't retroactively re-warm an
in-flight worker; the next ``auto_prefetch`` trigger
(project-load, scheduler edge) picks up the new path set.
Existing on-disk cache entries are kept either way.

**Daemon SettingsScreen highlights missing contributor on
entry.** Peers that route a ``S.CONTRIBUTOR_UNSET`` sync
failure through ``open_server_ui()`` previously dropped the
user onto the settings page with no indication of *which*
field was the blocker — the peer-side translated toast
("Please set your name…") could flash for under a second
and be eaten by the screen transition. On screen entry, if
``contributor_input`` is empty and not already focused, the
input now takes focus (keyboard pops up on Android) and the
inline hint reads "Required: your name is used for commit
authorship; sync and publish refuse until this is set." in
the red status colour. Saving a non-empty value clears the
hint back to the normal "Saved." confirmation.

**``[data-loss-risk]`` detection + new ``S.DATA_LOSS_RISK``
status.** ``_stage_audio`` and ``_sync_repo_locked`` now walk
``project_dir`` for any file outside the staging filter
(``audio/`` / ``images/`` / ``*.lift`` / ``.git/`` /
``.azt_atomic_pending/`` / ``.azt-collab/`` / known top-level
files like ``.gitignore``). Anything else is a peer writing
to a path the daemon won't commit — silent data loss class.
Each finding emits ``[data-loss-risk] uncommittable file in
project_dir: <rel>`` to stderr (so a tester-shared daemon log
makes the issue obvious), and the sync ``Result`` carries
``S.DATA_LOSS_RISK`` with ``count`` and ``sample`` (up to 5
paths) params.

**Peer contract** (``CLIENT_INTEGRATION.md`` § 17): this status
is **never silenced**. Auto-sync and user-initiated sync both
surface the translated toast unconditionally, urging the user
to enable "Log server activity" and share the daemon log.
Status is bucketed separately from the config-class /
transport-class statuses that auto-sync silences, because this
one represents active data loss, not a configuration glitch.

**``[stage-audio]`` / ``[commit-audio]`` diagnostic logs.**
Field report: testers record 1000+ audio files but only ~146
land in each commit (and only 4 commits total). Without
``adb`` access to the remote testers' phones we can't run
``ls audio/`` or ``git status`` directly. Daemon-side logging
in ``_stage_audio`` now emits a one-liner per pass with the
counts that disambiguate the gap:

```
[commit-audio] start project_dir='…/projects/baf' contributor=…
[stage-audio]  project_dir='…/projects/baf'
               on_disk_audio=1042 on_disk_images=12
               status.unstaged=0 status.untracked=898
               paths_to_add=898
[commit-audio] _stage_audio returned n=898
[commit-audio] committed n=898 sha=abc123def456
```

- ``on_disk_audio`` ≫ ``status.untracked`` → ``porcelain.status``
  is truncating large untracked sets; the gap is dulwich's,
  not the peer's.
- ``on_disk_audio`` ≈ ~146 → peer write path is dropping
  bytes; gap is upstream.
- ``status.untracked`` ≈ ~146 and ``on_disk_audio`` ≈ ~146
  ≈ ``paths_to_add`` over multiple syncs → user's record
  count is overcounting attempts vs. successes.

Remote tester recipe: daemon settings UI → "Log server
activity: yes" → record + sync → "Email daemon log". ``<_PickerRoot>`` hardcodes ``back_to:
'picker'`` on the SettingsScreen instance — correct in
external mode (settings reached from picker via the gear,
back should pop to picker), but wrong in internal mode:
settings is the root the user reached from outside the
Activity (launcher tap or peer's ``open_server_ui()``), so
the KV Back button navigating to picker dumped the user on
a screen they never asked for. ``PickerApp.build`` now
clears ``back_to`` on the settings screen in internal mode,
which trips the KV's
``height: dp(48) if root.back_to else 0`` gating and hides
the button entirely. The OS back path (``_navigate_back``
internal branch) remains the only way to leave settings,
letting Android finish() the Activity and return the user
to wherever they came from.

**``Switch project`` button promoted out of the gated row.**
Previously sat alongside Grant collaborator + Share repo QR
inside ``project_actions_row``, which is hidden when the
current project has no remote. Switch is meaningful before
publish too (user may want to abandon an unpublished project
for another), so it now lives in its own always-visible RecBtn
directly under the gated row — same vertical position
relative to the rest of the screen, but unconditionally
tappable.

**``project_actions_row`` hides via detach instead of just
``height: 0``.** Same Kivy touch-intercept bug ``publish_row``
already worked around: a BoxLayout with ``height: 0, opacity:
0`` still has its children at their declared sizes in the
widget tree, so their ``on_press`` handlers receive taps at
coordinates that visually belong to buttons higher up.
Symptoms: tapping ``Connect to GitHub`` fired
``grant_collaborator()`` (the row's first button), tapping
``Publish`` (when present) fired ``switch_project()`` (the
row's third button), tapping ``Connect to GitLab`` looked
like a no-op (Share-repo-QR's ``_pick_publish_candidate``
returned ``None``). ``_refresh_project_actions_row`` now
detaches all three children when hiding and reattaches when
showing — mirror of ``_detach_publish_children`` /
``_reattach_publish_children`` already in place.

**Edge-to-edge: status bar no longer hides the picker's gear
icon.** Android 15+ enforces edge-to-edge by default — the
status bar overlays the app window unless we opt back into
the pre-API-35 reserved-inset behaviour. Top-of-screen
widgets (the picker's gear, every screen's TopBar) sat
under the status bar; bottom-anchored widgets would have
sat under the gesture bar the same way. ``PickerApp.on_start``
now calls ``WindowCompat.setDecorFitsSystemWindows(window,
True)`` on the Activity's UI thread (via p4a's
``android.runnable.run_on_ui_thread`` helper), restoring
inset reservation. Available because we already pull
``androidx.appcompat`` (which transitively brings
``androidx.core.view``).

**``PickerApp.font_name`` alias.** Settings UI code that opens
modals (``share_repo_qr``, ``grant_collaborator``) reads
``App.get_running_app().font_name`` directly — fine under the
old ``CollabUIApp`` which exposed ``font_name`` as a class
attribute, but ``PickerApp`` only had the private ``_font_name``.
Under the unified PickerApp on Android, tapping ``Share this
repo (QR)`` (and ``Grant collaborator access``) raised
``AttributeError: 'PickerApp' object has no attribute
'font_name'`` and Kivy's event-loop catch buried it — the user
saw "tap does nothing." New ``@property font_name``  on
PickerApp returns ``_font_name`` so both callsites resolve
identically across host App classes.

**UX cleanup after the picker+settings merge.**

- **Share-repo QR popup** — dropped the "Copy URL" button.
  Close is the only remaining action; the URL is visible
  above the QR for users who'd rather read it than scan it.
- **Install / update popup** — "Open install page" relabeled
  to ``More info`` and moved RIGHT of the Install button so
  the affirmative action lands where the eye expects.
- **Install / update popup status line** — split out of
  ``body_label`` into a dedicated ``status_label`` rendered
  in the ACCENT colour, bold, sp(15). "Tap install again to
  confirm" and other transient status messages now read as
  the current call-to-action instead of vanishing into the
  wall of explanatory text above.
- **Contributor input hint** — changed from a specific
  example name to ``first_name last_name`` (generic).
- **Contributor "Required" message** — ``contributor_msg``
  label now auto-grows on ``texture_size`` so the multi-line
  warning isn't truncated when the SettingsScreen surfaces
  it on entry.

**Ungraceful-shutdown detection via sentinel file.** New
``azt_collabd/crash_marker.py``: on startup, writes
``$AZT_HOME/process_running.json`` with this process's pid +
started_at, registers an ``atexit`` hook to delete it on
clean shutdown. On the NEXT startup, a leftover sentinel
means the previous process bypassed atexit (SIGSEGV, SIGKILL,
OOM-kill, ``os._exit``, kernel-level kill); a one-line
summary lands in ``$AZT_HOME/last_native_crash.json``.

``GET /v1/health`` now surfaces it alongside the existing
``last_crash``:

```
{"ok": true, ...,
 "last_native_crash": {
   "detected_at": 1747234567.123,
   "previous_pid": 12917,
   "previous_started_at": 1747234389.456,
   "signal": "",
   "thread_name": "",
   "approx_pc": "",
   "detection_source": "ungraceful-shutdown sentinel"}}
```

``last_crash`` and ``last_native_crash`` are complementary:
the former is written by the daemon's Python excepthook from
the dying process (caught exception, Python alive to write
it); the latter is detected on the *next* startup from
sentinel-file diff (signal handler bypassed Python entirely).
A peer's `[server-crash]` log helper can mirror both.

Closes NOTES_TO_DAEMON.md "Daemon-side surface for native
crashes" by the pragmatic route — no JNI sigaction handler,
no async-signal-safe C extension. ``signal`` / ``thread_name``
/ ``approx_pc`` ship as empty strings reserved for a future
sigaction-driven shape: when a real handler lands, it
populates them in the dying process before ``_exit()``, peers
see richer detail with no schema change.

**"Switch project" button on the daemon settings UI + unified
picker/settings Kivy app.** New ``Switch project`` button in
the "Current project" row, sibling to Grant collaborator and
Share-repo-QR. Tapping it navigates to the project picker
in-process — no Intent, no Activity transition — and the
picker's submit handler stamps the new langcode via
``set_last_project`` and navigates back to settings.

The unification: the server APK used to run two separate
Kivy Apps (``CollabUIApp`` for settings, ``PickerApp`` for the
picker), one chosen at startup from the launching Intent
action. With ``PythonActivity`` being ``singleTask`` (p4a
default), firing PICK_PROJECT on ourselves wouldn't spawn a
fresh Activity — Android would route through
``onNewIntent`` on the existing one. So the only path to an
in-process switch is one Kivy App that hosts both screen
sets. ``PickerApp`` (which already had ``SettingsScreen`` as
a sibling for the picker → gear → settings flow) is the
unified home; ``server_apk/main.py`` always invokes it now,
passing ``launch_mode='external'`` for PICK_PROJECT Intents
(existing peer-driven behaviour, picker is initial screen,
submit fires setResult/finish) or ``launch_mode='internal'``
otherwise (settings is initial screen, picker submit writes
``last_project`` + navigates back to settings).

``_navigate_back`` branches on ``_launch_mode``:
- external: existing behaviour (back from picker exits the
  Activity with setResult, etc.).
- internal: back from settings returns False so Android
  closes the Activity (matching pre-0.41.22 ``CollabUIApp``
  semantics); back from picker / langpicker navigates to
  settings instead of finishing the Activity.

``PickerApp.on_resume`` added: refreshes the active screen
when the Activity comes back to the foreground.

**Pairs with the peer-side ``CLIENT_INTEGRATION.md`` § 14a
contract.** The daemon-side button is a no-op for the peer's
loaded view until peers ship the ``App.on_resume`` ↔
``last_project()`` reconciliation hook documented there. Ship
the daemon button now; peers adopt the on_resume hook in
their next release; the UX is coherent end-to-end at that
point. Mismatched timing degrades gracefully — the user
lands back on the previous project (the old pre-button
behaviour), nothing destructive.

**Diagnostic log section follows the same binary-toggle
pattern.** The single ``Save daemon log to file`` /
``Stop saving daemon log`` button is replaced by a row reading
``Log server activity:`` followed by two side-by-side buttons
— ``yes`` and ``no`` — with the active state highlighted in
the GREEN accent. Status line underneath is preserved (it
shows the log file path / "log capture is off" / byte count
on screen entry). Share + Email buttons below stay disabled
while logging is off and re-enable once the user picks
``yes``. Same convention as the wordlist row, the language
selector, etc.

**Daemon settings UI exposes the toggle.** New section on the
SettingsScreen, between "Refresh Status" and "Diagnostic log".
Section label reads ``Wordlist ({name}) images`` where
``{name}`` is the active project's wordlist (derived from the
image-repo slug — ``kent-rasmussen/images_CAWL`` →
``CAWL`` — via the new ``cawl.wordlist_name`` helper). Row
underneath reads ``Cache images:`` followed by two side-by-
side buttons — ``1 per line`` and ``all`` — with the active
mode highlighted in the GREEN accent (matching the language-
selector row's convention). Label updates on each
``refresh()`` so switching projects between visits to the
SettingsScreen renames the section to match.

**``cache_status`` cached count capped at ``requested`` in
offline-skipped state.** The walk-count fallback I introduced
this release (so an offline boot with prior cache shows e.g.
"1247 / 3000 (offline)" instead of "0 / 3000") counts every
file in the on-disk cache directory, which accumulates across
working sets and past sessions. Peer-reported case had the
disk holding 2220 files while the current ``requested`` was
1661, producing a "cache warm: 2220/1661" banner that tripped
peer-side "fully warm, hide and stop polling" logic and
looked like a daemon accounting bug. ``cache_status`` now
returns ``min(walk_image_count, requested)`` in the
offline-skipped branch; the active and circuit_open branches
were already accurate.

**jnius pre-warm at server-APK startup (main thread).** A
tombstone caught during the intermittent ``:provider`` crash
showed ``art::JNI::CallObjectMethodA`` SEGV at NULL on
``Thread-4`` (an unnamed Python-spawned thread, NOT our
prefetch worker). Two daemon-side helpers — ``paths.azt_home``
and ``store._autodetect_device_name`` — do their first jnius
work lazily on whichever thread happens to need the value
first. Python-spawned threads attach to the JVM via
pyjnius's auto-attach with the bootclassloader; first-time
``CallObjectMethodA`` on app-context fields from those
threads is the leading suspect for the NULL deref (per the
0.33.x classloader-attach precedent).

``server_apk/main.py`` now calls ``azt_home()`` and
``get_device_name()`` once on the main thread, immediately
after ``install_callbacks``. Both then serve from cached
state (process memory / config.json) for every subsequent
caller on any thread — no JNI dispatch from background
workers needed.

**Named all unnamed daemon-side worker threads.** The
``Thread-4`` in the tombstone could have been any of several
unnamed ``threading.Thread`` / ``threading.Timer`` spawns in
the daemon. Naming them lets the next crash backtrace
identify the worker directly:

- ``sync-fire-<langcode>`` (Timer / immediate sync workers
  in ``scheduler.py``)
- ``gh-device-flow-<id>`` (GitHub device-flow OAuth polling)
- ``clone-<id>`` (clone-job worker)
- ``httpd-shutdown`` (graceful loopback HTTP shutdown)

The CAWL prefetch worker was already named.

**``start_prefetch`` no longer spawns a second worker while
one is already running.** Pre-fix: a different ``requested``
count between calls would replace the state dict and start a
new thread; the old worker kept iterating and writing to the
new dict via ``_prefetch_state.get(repo)``. With Stage A
shipping ``auto_prefetch`` from every ``_touch_project``
*and* pre-Stage-B peers still POSTing their own
``cawl_prefetch`` working set, two workers regularly arrived
on overlapping timelines — both doing urllib/SSL fetches +
jnius-cached class work simultaneously. Leading suspect for
a NULL-deref SIGSEGV in the daemon's ``:provider`` process
~2 s after the second prefetch POST.

New behaviour: if an unfinished worker exists for the repo,
``start_prefetch`` returns its state and does NOT start
another. Different repos still proceed independently
(``_prefetch_state`` is repo-keyed). The peer's working
subset of a daemon-warmed full index will see its targets
populated by the running worker — no semantic loss.

**Bootstrap self-update no longer proposes installing an older
release over a locally-installed newer build.** The probe used
``needs_update = version_newer OR digest_changed OR mandatory``,
where ``digest_changed`` would trip on any GitHub-side asset
change. When a developer adb-sideloads a version newer than the
latest published tag and then any GitHub release publishes a
new digest, the probe would propose downgrading. New
``local_newer`` gate suppresses ``digest_changed`` when the
installed version is strictly above the latest tag. ``mandatory``
overrides remain unchanged — server-told-too-old still prompts
regardless. Diagnostic ``[bootstrap] _probe`` log line now
includes the ``local_newer`` boolean.

**Server APK no longer ships maintainer scripts.** The
``source.include_exts = py`` glob was sweeping two
maintainer-only Python files into ``classes.dex`` /
``private.tar``:

- ``server_apk/test_install.py`` — desktop integration
  smoke-test for the kill-recovery flow; sibling to
  ``test_install.sh``.
- ``azt_collabd/data/cawl/generate_seed.py`` — script that
  regenerates the bundled CAWL index JSON from GitHub at
  release-cut time.

Neither has any runtime role; both are now in
``source.exclude_patterns`` so they stay out of the APK.

**``[first-try]`` probe suppressed for cache_status polls.**
The always-on first-try diagnostic probes added in 0.41.16
were valuable for the no-adb field tester but emitted two
lines per cache_status poll at 1 Hz — pure noise on a normal
session. Transport now suppresses the probe when
``path.endswith('/cawl/cache_status')``. "First-try"
semantically doesn't apply to the Nth call of a polling
loop; all other RPC calls remain fully instrumented.

**Docs reorg — NOTES_TO_DAEMON.md is a live queue only.** The
two "standing notice" items that had accumulated there are
promoted to canonical homes:

- "Daemon is the sole authoritative source" (daemon-owned
  state table + four daemon obligations) → ``CLAUDE.md`` hard
  rule #8 + new "Daemon-owned state" section. It's an
  architectural invariant the client architecture depends on;
  ``CLAUDE.md`` is the right shelf.
- "Project-bound surfaces now in daemon UI (Phase 3)" →
  ``CLIENT_INTEGRATION.md`` § 12b "Project-bound actions live
  in the daemon settings UI", with the Phase-1 / Phase-3
  sequencing constraint preserved. It's peer-facing direction;
  the contract is the right shelf.

NOTES preamble tightened to call out the antipattern
explicitly: standing rules belong in ``CLAUDE.md`` /
``CLIENT_INTEGRATION.md``, not in the queue file. Otherwise
the queue silently turns into a reference shelf and stops
being a queue.

---

Two coupled bugs surfaced when testing the picker's "Scan QR"
affordance against the 0.41.20 server APK. The first masked the
second; both had to be fixed to make the button work.

Two coupled bugs surfaced when testing the picker's "Scan QR"
affordance against the 0.41.20 server APK. The first masked the
second; both had to be fixed to make the button work.

**1. Autoclass path corrected** in
``azt_collab_client/ui/qr_scan.py``. Was
``com.journeyapps.barcodescanner.IntentIntegrator``; the class
actually lives at
``com.google.zxing.integration.android.IntentIntegrator`` — the
journeyapps AAR re-ships ZXing's original IntentIntegrator at its
historical package path even though the rest of the library is
under ``com.journeyapps.barcodescanner``. Module docstring updated
to call out the mismatch.

**2. AndroidX transitive deps listed explicitly** in
``server_apk/buildozer.spec.tmpl``. The zxing-android-embedded
4.3.0 POM declares its AndroidX deps (fragment, appcompat) as
``implementation`` rather than ``api``, so Gradle uses them to
compile the AAR's own classes but does NOT propagate them to the
consuming APK's classes.dex. Result: the journeyapps classes
reference ``androidx.fragment.app.Fragment`` /
``FragmentActivity`` / ``AppCompatActivity`` but the Android
verifier can't resolve those references at class-load time, and
``autoclass(...IntentIntegrator)`` raises
``NoClassDefFoundError: Landroidx/fragment/app/Fragment;`` even
with the autoclass path fix in (1).

New ``android.gradle_dependencies``:

```
com.journeyapps:zxing-android-embedded:4.3.0,
androidx.appcompat:appcompat:1.6.1,
androidx.fragment:fragment:1.6.2,
org.jetbrains.kotlin:kotlin-stdlib:1.8.20,
org.jetbrains.kotlin:kotlin-stdlib-jdk7:1.8.20,
org.jetbrains.kotlin:kotlin-stdlib-jdk8:1.8.20
```

Listing appcompat + fragment explicitly forces Gradle to pull them
into the project classpath, so the dex actually carries the
``Landroidx/...`` implementations the journeyapps code references.

The three kotlin-stdlib pins resolve a transitive-version conflict
that surfaced as ``:checkReleaseDuplicateClasses`` failing with
``Duplicate class kotlin.collections.jdk8.CollectionsJDK8Kt``:
``androidx.fragment:1.6.2 → lifecycle-runtime:2.6.2`` pulls
``kotlin-stdlib:1.8.20`` (the post-merge artifact that already
ships the JDK7/JDK8 helper classes), while the same lifecycle-
runtime transitively pulls ``kotlinx-coroutines-android:1.6.4 →
kotlin-stdlib-jdk{7,8}:1.6.21`` (the pre-merge split artifacts
that also ship them). Forcing the ``-jdk7`` / ``-jdk8`` resolution
up to 1.8.20 lands on the empty metadata-only redirect artifacts
Kotlin started shipping at 1.8 once the split was deprecated, so
the duplicate-class collision disappears with no functional
change to anything else in the build.

**Build note.** Re-run ``server_apk/build_buildozer_spec.sh`` to
regenerate ``buildozer.spec`` from the template after pulling, then
``buildozer android clean && buildozer android release`` — the dist
tree caches Gradle resolution, so a clean is required to pick up the
new dependency list.

**Floor:** no bumps. Server APK rebuild required to ship the fix
since qr_scan runs inside the picker subprocess hosted by the server
APK, and the AndroidX deps need to be in *that* APK's dex.

### azt_collabd 0.41.20 / azt_collab_client 0.41.20 — docs: routing table moves to contract; atomic_open_write note added

Docs-only release closing two long-standing gaps between
``CLAUDE.md`` (philosophy / rationale) and
``CLIENT_INTEGRATION.md`` (conformity contract). Per the
docs-separation rule, conformity material belongs in the
contract; rationale belongs in CLAUDE.md.

**Sync-result routing table moved to contract** as new
``CLIENT_INTEGRATION.md`` § 17 (Routing on sync results). Full
table of status codes × auto-sync vs. user-initiated sync
behaviour, the canonical code shape for both contexts, and an
explicit ``S.*`` constant reference noting which constants
shipped in 0.41.13 (``SERVER_UNAVAILABLE`` /
``SERVER_ERROR``). The CLAUDE.md "Peer contract: routing on
sync results" section now carries only the rationale (per-
code meanings, the pre-0.34.1 anti-pattern, why the auto/user
distinction lives peer-side) and points to the contract for
the actual table + code.

**``atomic_open_write`` FD-path documented in § 8** of the
contract. Pre-0.41.7 the URI form of ``atomic_open_write``
shipped LIFT bytes as base64 inside the JSON-RPC body and hit
Binder's ~1 MB per-transaction cap on Android — silent
failure for LIFT > ~700 KB. Peers rebuilding against 0.41.7+
pick up the two-phase FD-write + finalize protocol
transparently; the contract now notes the rebuild-for-large-
LIFTs implication so peer maintainers know it's a free
correctness win.

No code changes in this release; docs only.

### azt_collabd 0.41.19 / azt_collab_client 0.41.19 — `share_log_file` + French translations + docs

Follow-on to 0.41.18 in response to recorder 1.41.24's filing
(NOTES_TO_DAEMON.md 2026-05-13). Two changes:

**``share_log_file(log_path, prev_path=None, ...)`` helper**
added to ``azt_collab_client/ui/share.py``. Reads a log file
(plus optional previous-session log) from disk, bundles into
one ``text/plain`` blob with section breaks, inserts into
MediaStore Downloads to get a real ``content://`` URI, and
dispatches an ``Intent.ACTION_SEND`` with ``EXTRA_STREAM``.

Unlike ``share_text``, this attaches as a real file (receivers
can save it; payload size isn't bounded by Intent extras), and
unlike ``share_running_apk`` it handles two source files +
sets a sensible default ``display_name``. Mirrors the
MediaStore-insert pattern from ``share_running_apk`` so the
underlying jnius dance is shared at the call-site level.

Recorder will replace its peer-side stand-in with one
``share_log_file(log_path=_LOG_PATH, prev_path=…)`` call once
it picks up 0.41.19.

**Daemon UI's "Share daemon log" migrates to
``share_log_file``** (reading ``$AZT_HOME/daemon.log`` from
disk directly — both daemon-UI and daemon-proper processes
share filesDir on Android, so file-based access works without
an additional RPC for the bytes). Email button still uses
``email_text`` since ``ACTION_SENDTO`` with a ``mailto:`` URI
restricts the picker to email apps.

**French translations** added for all 0.41.17-0.41.19 strings:
"Diagnostic log", "Save daemon log to file", "Stop saving
daemon log", "Share daemon log", "Email daemon log", and
related status / error messages. Plus the helper-side
``Share log`` / ``AZT log`` / ``Could not share log`` /
``Log file is empty`` / ``Log file: {path}`` strings.

**Docs.** ``CLIENT_INTEGRATION.md`` § 14b now lists all three
share helpers (``share_text``, ``email_text``,
``share_log_file``) with picking-between guidance.
``azt_collab_client/CLAUDE.md`` carries the rationale for the
share-module extraction and the daemon-log toggle's
hot-toggle design.

### azt_collabd 0.41.18 / azt_collab_client 0.41.18 — share helpers extracted + email-log button

Follow-on to 0.41.17. Two changes:

**``share_text`` and ``email_text`` extracted into
``azt_collab_client/ui/share.py``** alongside the existing
``share_running_apk``. Both reusable by any peer:

- ``share_text(text, subject='', chooser_title='', on_error=None)``
  — ``Intent.ACTION_SEND`` with ``EXTRA_TEXT``. Any
  ``text/plain``-handling share target accepts it.
- ``email_text(text, to='', subject='', on_error=None)`` —
  ``Intent.ACTION_SENDTO`` with a ``mailto:`` URI. Restricts the
  picker to email apps only.

The daemon UI's "Share daemon log" button now delegates to
``share_text`` instead of inlining the JNI dance.

**"Email daemon log" button** added to the Diagnostic log
section alongside "Share daemon log". Uses ``email_text`` for
the email-only picker affordance — better UX than the generic
share sheet when the user's intent is specifically "send this
to the developer".

### azt_collabd 0.41.17 / azt_collab_client 0.41.17 — daemon-log-to-file toggle + share button

Remote tester can't run logcat, so daemon-side diagnostic
output (``[boot-trace-daemon]``, ``[cawl]``, ``[recent]``,
``[first-try]`` from the daemon UI / picker subprocess) was
unreachable. Added:

- **Config knob** ``logging.daemon_log_to_file`` in
  ``$AZT_HOME/config.json``. Default off.
- **Stderr tee** in the daemon. When the toggle is on,
  ``sys.stderr`` is wrapped to mirror writes to the original
  destination (logcat) AND to ``$AZT_HOME/daemon.log``. Tee
  is hot-installable / hot-removable — no daemon restart
  needed.
- **Endpoints.** ``POST /v1/logging/daemon_log_to_file`` (set
  toggle, install/remove tee in-process); ``GET
  /v1/logging/daemon_log`` (returns log contents + current
  toggle state + file path).
- **Settings UI.** New "Diagnostic log" section with two
  buttons: "Save daemon log to file" (toggle) and "Share
  daemon log" (Android ``Intent.ACTION_SEND`` with the log
  content as ``EXTRA_TEXT``). Status line under shows current
  state + file size.
- **Client wrappers.** ``set_daemon_log_to_file(enabled)`` /
  ``get_daemon_log()``.

The share intent uses ``EXTRA_TEXT`` (text/plain) rather than a
file-URI attachment so any text-handling share target accepts
it — email composers, messaging apps, file savers. Daemon
truncates to the last 256 KB to fit comfortably in an intent
extra; the diagnostic value lives in the tail anyway.

### azt_collabd 0.41.16 / azt_collab_client 0.41.16 — first-try probes always-on for this build

Remote tester can't run logcat (the device they have access
to isn't local). The previous env-var gate
(``AZT_DEBUG_FIRST_TRY=1``) was the wrong shape — they have
no way to set env vars on the device. Flipping the
``first_try_log`` gate to always-on for this build so the
probes write to peer stderr (which lands in
``/sdcard/azt_recorder.log``) without the tester having to
configure anything.

Restore the env gate after the crash is diagnosed; the gate's
``if not os.environ.get('AZT_DEBUG_FIRST_TRY'): return`` is
preserved in the module docstring for easy reinstatement.

### azt_collabd 0.41.15 — ContentProvider waits for Python callbacks (H5 defensive fix)

The "first-try-fails, second-try-works" pattern reported on
the Tecno KN4 (Helio G81, 4 GB RAM, Android 16) is most likely
Android killing the daemon's ``:provider`` process during a
user's brief navigation away (settings screen, etc.) and then
lazy-spawning it on the next peer call. The respawn race:
Android creates the AZTCollabProvider Java object and routes
the incoming peer call to it on a binder thread, while
Python's ``install_callbacks()`` is still initializing on
SDLThread. The provider sees ``sDispatch == null`` /
``sOpenFile == null`` and returns ``daemon_not_ready``, which
the peer surfaces as a crash. Second tap: Python is now
initialized, callbacks registered, call succeeds.

Defensive fix in ``AZTCollabProvider.java``: ``call()`` and
``openFile()`` now wait up to 3 seconds (50 ms polling) for
the Python callback to register before returning the
"daemon_not_ready" error. On a healthy respawn, Python
finishes ``install_callbacks()`` in well under a second and
the first peer call queues briefly behind it instead of
failing. On a truly-down daemon, the 3 s timeout still fires
and the failure surfaces the same as before.

Harmless if the bug wasn't H5: the wait loop only runs while
the callbacks are null, which is the respawn window only.
``ping`` requests (used by discovery probes) still bypass the
wait so transport-discovery latency is unaffected.

### azt_collabd 0.41.14 / azt_collab_client 0.41.14 — env-gated first-try-fails diagnostics

User reported a transient crash on the SettingsScreen →
"select new project" path: first try crashes, second works.
Nothing in logcat suggests a cause. Added probes for five
hypotheses, all gated behind ``AZT_DEBUG_FIRST_TRY=1`` so
they're inert when the env var isn't set. New helper
``azt_collab_client/_debug.py`` provides ``first_try_log``.
Probes:

- H1 (cache poll leaks past screen leave): in
  ``SettingsScreen._stop_cawl_cache_poll`` and
  ``_tick_cawl_cache_status`` — logs Clock event lifecycle
  + current screen at every tick.
- H2 (picker cold-start race): in
  ``ProjectPickerScreen.on_enter`` and ``_populate_projects``
  + ``picker_app.main`` — timestamps each phase.
- H3 (subprocess invocation): in ``picker_app.main`` entry
  + return — argv + dt.
- H4 (URI grant not propagated): in
  ``lift_io._open_content_uri`` — wraps
  ``openFileDescriptor`` with explicit exception logging
  so any swallowed ``SecurityException`` surfaces.
- H5 (daemon respawn drops the call): in
  ``transports.android_cp.call`` — logs bundle-null on
  return.

Enable with ``adb shell setprop … AZT_DEBUG_FIRST_TRY 1``
or by setting the env var in the launch path. When set,
``[first-try] <label> k=v ...`` lines appear in logcat at
each probe site.

### azt_collabd 0.41.13 / azt_collab_client 0.41.13 — CAWL: TTL-cached os.walk + quieter resolve logs + S.SERVER_UNAVAILABLE / S.SERVER_ERROR constants

**Cache-count undercount fixed.** 0.41.10's incremental
counter (lazy-seed + per-fetch increment) had a race that
produced an undercount in the wild (peer warmed 1661, daemon
reported 1257). Tracing didn't fully pin the race but the
failure-mode (silently wrong UI total) is bad enough that I
replaced the scheme rather than patching it. ``_walk_image_count``
is now a TTL-cached ``os.walk`` — 500 ms TTL, ~50 ms uncached
on the canonical 1700-image set, near-zero CPU at 1 Hz
polling. Accurate by construction: it counts what's actually
on disk, no event-based bookkeeping that can drift. Dropped
``_note_image_cached``, ``_cached_image_count``,
``_cached_count_seeded`` and their call sites. The TTL-cached
walk fallback is only used when no prefetch job is active for
the repo (otherwise the prefetch state still wins; same logic
as 0.41.11).

**Quieter resolution logs.** The
``[cawl] get_image_path: no index-resolution for X`` line was
firing for already-nested paths even though those paths
needed no resolution and the fetch was succeeding. Net effect
was a logcat full of scary "no index-resolution" lines for
calls that were working fine. Now: pass-through is silent;
the "flat basename not in index" case still logs because
that's a real "peer asked for something the daemon doesn't
know" situation.

**``S.SERVER_UNAVAILABLE`` / ``S.SERVER_ERROR`` constants.**
The peer-routing example in ``azt_collab_client/CLAUDE.md``
("Peer contract: routing on sync results") shows
``result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR)`` but
``status.py`` didn't actually export those constants —
``AttributeError`` at runtime for conformant peer code. The
string literals were always emitted on results
(``Status('SERVER_UNAVAILABLE', …)`` from the wrappers'
transport-failure branches); only the typing-aid constants
were missing. Added to both ``azt_collab_client/status.py``
and the mirrored ``azt_collabd/status.py``. Peer note filed
2026-05-13 against 1.41.16 was the trigger.

### azt_collabd 0.41.12 / azt_collab_client 0.41.12 — quiet the 1 Hz poll logs

Three log-noise sources fired once per second once the
cache-status poll kicked in:

- ``[recent] GET /v1/recent/last_project → 'X' (from ...)``
  (daemon-side, ``_h_get_last_project``).
- ``[recent] last_project → 'X'`` (client-side wrapper).
- Both happen because the daemon UI's poll resolved
  ``last_project()`` every tick to know which project to
  query.

Fixes:

- Drop the success log from both sites. Error paths still
  log (``ServerUnavailable``, ``not ok``); they're rare and
  useful. The setter (``_h_set_last_project``) still logs
  because it's a real state change.
- Daemon UI now resolves the langcode once at poll start
  (``_start_cawl_cache_poll``) and reuses it across ticks.
  The user doesn't switch projects while sitting on the
  settings screen, so two RPCs/sec just to re-confirm the
  same langcode was overhead with no benefit.

### azt_collabd 0.41.11 / azt_collab_client 0.41.11 — daemon-driven CAWL prefetch + accurate progress

The 0.41.9 cache-status indicator reported "files on disk vs.
all image-shaped entries in the index". For the canonical
``kent-rasmussen/images_CAWL`` set that's "959 / 3231" — the
total is the count of every image file across all variants,
but the peer typically warms one variant per CAWL identifier
(~1661 files). The banner plateaued at ~1661/3231 with no way
for the user to tell whether the system was done. Misleading
for the indicator's core purpose ("don't disconnect, you're
not done yet").

Root cause: the peer iterated its working-set and called
``get_image_path`` per entry. The daemon saw a stream of
independent requests with no "session" concept; its
progress-reporting could only count the on-disk count
against the index's total — a structural over-count.

This release flips the iteration. Peer hands the daemon a
single list of paths via ``POST /v1/projects/<lang>/cawl/
prefetch``; daemon spawns a background worker that iterates
the list, warms each path through ``get_image_path`` (cache
hit or GitHub fetch), and tracks per-job ``requested`` /
``completed`` / ``failed`` counters. ``cache_status`` now
reads from that job state when a prefetch has run, so the
progress bar reflects work the peer actually wants done.

**Endpoints.**

- ``POST /v1/projects/<lang>/cawl/prefetch`` with body
  ``{paths: [...]}`` — kicks off the worker, returns
  ``{requested, completed, finished}`` immediately. Idempotent:
  a second call with the same paths-set against an active job
  returns the existing state; a call with a different set
  replaces it.
- ``GET /v1/projects/<lang>/cawl/cache_status`` — unchanged
  wire shape (``{image_repo, cached, total}``). When a
  prefetch is active or completed for the repo, ``cached`` =
  job's ``completed`` and ``total`` = job's ``requested``,
  giving a progress bar that ends at 100%. Falls back to the
  old "on-disk vs. index" semantics when no prefetch has run.

**Client wrapper.** ``cawl_prefetch(langcode, paths)`` returns
the initial state dict; peers chain it with the existing
``cawl_cache_status(langcode)`` poll for progress display.

**On-demand path untouched.** ``CAWLHandle(...).open_read``
still works for any individual image; daemon serves from
cache or fetches on demand exactly as before. The new path
is just for bulk warming; individual reads share the same
cache.

**Daemon UI banner.** Updated to reflect the same numbers —
when a prefetch is in flight, the daemon UI's top-banner
shows "Caching images: M / N (network in use — please stay
online)" against the job's actual counts. Auto-hides when
``cached >= total``.

**Logging.** Removed ``_touch_project`` from the
``cache_status`` handler — a 1 Hz status poll isn't "user is
working on this project" signal and was flooding logcat with
``[recent] _touch_project`` lines.

**Peer adoption.** Contract updated at
``CLIENT_INTEGRATION.md`` § 10 with the new wiring shape and
the rationale ("daemon-driven, not peer-driven"). Peers
calling ``CAWLHandle.open_read`` in a loop for bulk warming
should migrate to ``cawl_prefetch`` for accurate progress;
their on-demand single-image calls don't need to change.

### azt_collabd 0.41.10 — CAWL cache-status: top-banner placement + 1 Hz poll + memoised counts

Follow-on to 0.41.9. Three iterations on the cache-status
indicator after first contact with the user:

**Banner moves to the top of the SettingsScreen.** Previously
the status line was inside the bottom Status section, which by
design is low-attention "what's going on" diagnostic info. The
caching indicator's whole purpose is the *opposite* — grab
attention so the user doesn't disconnect network. Now lives
as a pinned BoxLayout banner directly under the TopBar, above
the ScrollView, so it can't scroll out of view. Accent-coloured
background + bold text + an explicit "please stay online"
clause in the message.

**Poll interval drops to 1 Hz.** 5-second polls made the
counter look broken — 10+ images cached per refresh produced
visible-but-jumpy progress. 1 Hz feels live.

**``cache_status`` is now near-zero cost per call.** The 1 Hz
poll needed this: the previous implementation did one
``os.walk`` over the daemon-owned images dir (~50-100 ms for
the canonical 1700-image set) plus one ``_read_cached_index``
JSON parse per call. At 1 Hz that's 5-10% daemon CPU, hot
enough to notice on a phone.

Memoisation:

- ``_cached_image_count[repo]`` is an in-memory counter. Lazy
  ``os.walk`` seeds it once per repo per daemon process;
  ``_note_image_cached(repo)`` increments it on every
  successful image-fetch + cache-write. Reset on daemon
  restart (re-seeds on first call). No invalidation needed —
  the counter only grows because cached images aren't
  removed.
- ``_total_count_cache[repo]`` is mtime-keyed. The
  ``_count_index_images`` lookup parses the cached index file
  once per (repo, index mtime); subsequent calls return the
  cached count. Invalidates automatically when ``get_index``
  refreshes and rewrites the cache file.

Net: cold-start ``cache_status`` is one ``os.walk`` + one JSON
parse; steady-state is a dict lookup + an ``os.path.getmtime``.

### azt_collabd 0.41.9 / azt_collab_client 0.41.9 — CAWL cache-status endpoint

The first cold-cache prefetch on the canonical
``kent-rasmussen/images_CAWL`` repo pulls ~1660 image binaries
sequentially through the daemon; on a typical mobile connection
this takes minutes during which the user has no in-app
indication that the daemon is using their network. They might
naturally disconnect Wi-Fi between gestures and end up with a
half-warm cache (every uncached image then has to fetch on
demand, which is exactly what the prefetch was avoiding).

This adds a project-scoped cache-status endpoint peers can
poll on a short interval to drive a "Caching images: M / N"
indicator:

- Daemon: ``GET /v1/projects/<lang>/cawl/cache_status`` →
  ``{ok, image_repo, cached, total}`` where ``cached`` is the
  count of image files in the on-disk cache for the project's
  resolved image_repo and ``total`` is the image-shaped index
  entries.
- Client: ``cawl_cache_status(langcode)`` wrapper returns the
  same shape as a dict (no Result wrapper — this is a status
  query, not a state-changing op). Empty values on any
  transport / not-found failure so the peer can poll
  unconditionally without exception handling.

Cost: one ``os.walk`` over the daemon-owned images dir per
poll. Bounded by total_count (~1700 files for the canonical
set); fast enough for a 5-second poll interval. No network.

**Daemon UI mirror.** The settings screen
(``python -m azt_collabd ui`` / ``open_server_ui()``) also
surfaces this progress: a small line in the Status section
that says "Caching images: M / N (network in use)" while a
prefetch is running, auto-hides when the cache catches up.
Polled on a 5-second ``Clock.schedule_interval`` while the
SettingsScreen is visible; cancelled in ``on_leave`` so it
doesn't wake the daemon for a screen the user can't see.

**Peer-side adoption.** The recorder / future viewer should
mirror the same indicator on their own loading screen so
users see the progress without navigating into Sync Settings.
Contract documented in
``azt_collab_client/CLIENT_INTEGRATION.md`` § 10 with the
copy-paste shape.

### azt_collabd 0.41.8 — CAWL: drop daemon-side offline backoff (peer has a circuit breaker already)

0.41.4 added a daemon-side 60s offline backoff that suppressed
``[cawl] image fetch failed`` log spam when a peer iterated a
~1700-image set on a device with no network. After diagnosis
of an "55 images succeed, then 10 fail silently" pattern in
the wild, the backoff was actively making things harder to
debug: when the daemon went silent it was impossible to tell
from the peer side whether a fetch had been attempted at all
or whether the daemon had short-circuited on a stale backoff
window. The peer already has its own circuit breaker that
suppresses pulls after N consecutive failures, so the daemon
log-spam concern was solved peer-side anyway.

This release rips out the daemon-side backoff entirely.
``get_image_path`` and ``get_index`` now attempt the fetch on
every cache miss (lock-coalesced, as before) and emit a
verbose log line per failure. The peer's circuit breaker (in
``lift.py: _CAWLImageResolver._pull``) is the right place for
the "stop trying after N failures" policy.

Removed: ``_OFFLINE_BACKOFF_SECONDS``, ``_offline_state_lock``,
``_offline_until``, ``_offline_suppressed``,
``_is_in_offline_backoff``, ``_note_fetch_failure``,
``_note_fetch_success``. The ``http.client.HTTPException``
catch added in 0.41.4 stays — InvalidURL et al. still must not
escape uncaught.

### azt_collabd 0.41.7 / azt_collab_client 0.41.7 — atomic_open_write via FD + finalize (Binder cap on writes)

Same Binder per-transaction cap (~1 MB) that broke the CAWL
index read in 0.41.2 also breaks the LIFT atomic write for
projects whose LIFT exceeds ~700 KB (base64 inflates 1.33× and
the JSON envelope blows past the cap). Symptom is identical:
``ContentResolver.call`` Bundle drops on the way to the
daemon, transport raises, ``atomic_commit_bytes`` returns
``SERVER_UNAVAILABLE``, ``_UriAtomicWriteFile.commit`` raises
``IOError``, the audio-save path (or any other write through
``LiftHandle.atomic_open_write``) fails. The user-visible
break is "stopping a recording loses the entry"; the daemon
never sees the call so there's no daemon-side log.

**Two-phase write.** Bytes now cross the IPC boundary via the
ContentProvider FD path (no Binder size cap), then a tiny
RPC finalizes the atomic rename under ``project_lock``:

1. Peer generates ``token = secrets.token_hex(16)``, opens
   ``content://<auth>/<lang>/_atomic_pending/<token>`` for
   write via ``ContentResolver.openFileDescriptor``, writes
   the buffered bytes through that kernel FD. The daemon's
   ``_resolve_path`` routes ``_atomic_pending`` to
   ``<working_dir>/.azt_atomic_pending/<token>`` (new write-
   only route, token-validated against
   ``^[A-Za-z0-9_-]{1,64}$``).
2. Peer calls ``POST /v1/projects/<lang>/atomic_finalize``
   with ``{token, path}``. The daemon validates the path
   against the same whitelist
   ``atomic_commit`` uses (``<file>.lift`` /
   ``audio/<file>`` / ``images/<file>``), reads the pending
   file for size + sha256, then ``os.replace``s it under
   ``project_lock``. Returns ``ATOMIC_COMMITTED`` with the
   same params shape as ``atomic_commit_bytes``.

**Atomicity preserved.** The rename still happens under the
project lock so concurrent peer-vs-peer / peer-vs-merge
writers can't tear the destination. Phase 1 writes to a
unique per-token scratch path so two concurrent peers don't
collide on the pending file either. The only new failure
surface is "phase 1 succeeds, phase 2 fails": the scratch
file may linger under ``.azt_atomic_pending/`` on the
daemon. No automatic GC yet — operationally OK since the
total scratch volume is bounded by recent failed writes.
The daemon best-effort unlinks on rename failure.

**Backward compatibility.** ``_UriAtomicWriteFile.commit``
falls back to the legacy single-RPC ``atomic_commit_bytes``
path if phase 1 raises (pre-0.41.7 daemon that doesn't know
``_atomic_pending``) or phase 2 returns ``SERVER_ERROR``
(daemon missing the route). The legacy path still works
against any 0.36.0+ daemon for payloads under the cap, so
small-LIFT projects don't regress on a mixed-version
deployment.

**Where the bump goes.** The daemon side ships in the server
APK. The client-side rewrite of ``_UriAtomicWriteFile.commit``
+ the new ``atomic_finalize_pending`` wrapper lives in
``azt_collab_client`` — peer apps bundle this at build time,
so peers must be rebuilt to pick up the new write flow. Once
a peer is on 0.41.7+, atomic LIFT writes of any size up to
~10 MB cross cleanly.

### azt_collabd 0.41.6 — CAWL: literal-%20 in filename + drop defensive url-field re-encode

Surfaced once 0.41.5 got us past the flat-basename resolution
to a real fetch URL: a subset of canonical ``kent-rasmussen/
images_CAWL`` filenames literally contain ``%20`` as part of
the filename (not as URL encoding for a space). The actual
on-disk filenames look like
``2d%20minimalistic%20black%20and%20white%20line%20art%20of%20right%20elbow__bw.png``.

``Uri.getPath()`` on Android URL-decodes once, so the Python
side receives the literal-character filename — including the
literal ``%20`` substrings. The previous
``quote(rel_path, safe='/%')`` preserved those ``%``
characters unchanged into the URL, so GitHub decoded ``%20`` →
space and looked for a file with literal spaces, which doesn't
exist — 404.

Fix: ``quote(rel_path, safe='/')`` (drop ``%`` from safe). The
``%`` in literal-``%20`` filenames now encodes to ``%25``,
producing ``%2520`` in the URL. GitHub decodes once → literal
``%20`` → matches the actual filename on disk.

Also removed the defensive ``url``-field re-encoding in
``_h_cawl_index`` that 0.41.3 added. With the new encoding
rules, ``_fetch_index_from_github`` now emits correctly-
encoded ``url`` fields, and the defensive re-encode would
double-encode them (``%2520`` → ``%252520``) and break peers
that actually use ``entry['url']``. The current Stage-2 peers
use ``CAWLHandle`` (which goes through the daemon's fetch
path, not the per-entry ``url``), so removing the defensive
layer doesn't regress anything we ship.

**Decision log: idempotent encoding vs. canonical encoding.**
0.41.4's ``safe='/%'`` was an attempt at idempotent encoding —
"if input is already encoded, don't double-encode." That logic
is fundamentally ambiguous: ``%20`` in the input means either
"encoded space" or "literal ``%20`` in the filename" and we
can't tell from input alone. The canonical-encoding rule
(always encode ``%`` to ``%25``) gives consistent semantics:
peer-side input is always literal-character (URI decoding
gives that for free), daemon always encodes once for HTTP.
Peers that want to pass pre-encoded paths are broken under
this model — they shouldn't.

### azt_collabd 0.41.5 — CAWL: flat-basename → nested-path resolution via index

The canonical ``kent-rasmussen/images_CAWL`` repo keeps images
under category subdirs (``0001_body/<basename>.png``,
``0002_head/<basename>.png``, …). A peer parsing the index
typically extracts a CAWL identifier + a flat basename, then
calls ``CAWLHandle(langcode, basename).open_read()`` with just
the basename — it doesn't need to track the category prefix
because every category is part of the same CAWL set.

Before this fix the daemon would receive that flat basename
and ask GitHub for ``HEAD/<basename>.png`` (top level) — which
returns 404 because the file actually lives at
``HEAD/0001_body/<basename>.png``. After the offline-backoff
kicked in on the first 404, all subsequent image fetches in
the same minute also went silent → user-visible "no images."

New helper ``_resolve_basename_via_index(repo, rel_path)``:
if ``rel_path`` is a flat basename (no ``/``) and the index
has exactly that basename under some nested path, the daemon
canonicalizes to the nested path before computing the cache
target / fetch URL. Both the on-disk cache and the GitHub
request use the canonical nested path; subsequent flat-basename
requests for the same file hit the cache directly because the
canonicalization is deterministic.

If the index isn't cached yet, ``_resolve_basename_via_index``
returns ``rel_path`` unchanged so the network fetch attempt
fails honestly (rather than silently rewriting to a wrong
path). The index seed is bundled in the APK, so this only
matters in the rare case where the seed is missing and a
network fetch hasn't run yet.

### azt_collabd 0.41.4 — CAWL: SSL via certifi + URL encoding + offline-backoff coalescing

Three related image-fetch fixes that surfaced once 0.41.3's
slim index let the peer actually request binaries.

**URL encoding for paths with spaces / unsafe chars.** Both the
per-file ``url`` field in ``_fetch_index_from_github``'s emitted
index and the request URL in ``_fetch_image_bytes_from_github``
now percent-encode the path component. ``safe='/%'`` so the
encoding is idempotent (won't double-encode an already-encoded
input). CAWL filenames commonly include spaces / commas /
parens; raw URLs containing those raise
``http.client.InvalidURL`` at ``_validate_path`` time, before
the request goes out. The except clause in ``get_image_path``
also now catches ``http.client.HTTPException`` (parent of
``InvalidURL``); previously it only caught ``OSError`` /
``URLError`` and an InvalidURL escaped uncaught past the
offline-backoff handler, turning into a Java-side
``FileNotFoundException`` with no peer-visible
``[cawl] image fetch failed`` log.

The rest of this entry covers two related fixes that ride on
the same release:

Two related image-fetch fixes that surfaced once 0.41.3's slim
index let the peer actually request binaries.

**SSL bundle on Android.** ``_fetch_index_from_github`` and
``_fetch_image_bytes_from_github`` were using raw
``urllib.request.urlopen`` without calling ``net._ensure_ssl()``
first. Every other network site in the daemon does call it; the
CAWL module was the lone holdout. On p4a Android (no system CA
store) this manifested as ``SSL: CERTIFICATE_VERIFY_FAILED``
for every image fetch. Fix: both call sites now call
``_ensure_ssl()`` before the urlopen. The patch is idempotent
and globally monkey-patches ``ssl._create_default_https_context``
to use certifi's bundle, so once it has run any stdlib HTTPS
works.

**Offline backoff (per-process, shared between index + image
fetches).** When a connect-class urllib error fires
(URLError / OSError / TimeoutError), the daemon now enters a
60s cooldown during which subsequent CAWL fetch attempts
short-circuit silently. Without this, a peer iterating a
~1700-image set on a fully-offline device (or one with
broken DNS / SSL) spammed logcat with 1700 near-identical
``[cawl] image fetch failed`` lines, drowning real signal.
Coalesced semantics:

- First failure in a fresh window → one verbose log line
  identifying the repo + cause.
- Subsequent failures in the same window → silent skip
  (no network attempt, no log).
- Any successful fetch → backoff cleared immediately + one
  ``[cawl] network recovered`` log with the suppressed count.

The window is per-daemon-process module-state; restart
clears it. ``_OFFLINE_BACKOFF_SECONDS = 60`` is tuned for
"long enough that a 1700-image swipe-prefetch loop quiets
down, short enough that the user reconnecting wifi gets
images within a minute". Index lookups in the window serve
from cache (stale OK) per the existing fallback policy.

### azt_collabd 0.41.3 — slim CAWL index over JSON-RPC (image extensions only)

Server-side companion to 0.41.2's FD-route client fix. The
0.41.2 fix only helps peers that have rebuilt against the
updated ``azt_collab_client``; existing peer installs keep
calling ``cawl_index`` over the JSON-RPC path and keep
receiving an empty response because the ~1.5 MB index Bundle
exceeds the Binder per-transaction cap.

Per the recorder peer's 2026-05-13 filing
(NOTES_TO_DAEMON.md), the daemon now filters the index
response to ``.png`` / ``.jpg`` / ``.jpeg`` paths before
serializing. The canonical ``kent-rasmussen/images_CAWL`` repo
includes ~3700 non-image blobs (README, LICENSE, .gitignore)
that every peer's parser discards on receipt anyway —
filtering server-side just stops shipping bytes nobody uses.
Reduces the wire from ~5479 entries (~1.5 MB) to ~1700
entries (~470 KB), well under the Binder ceiling.

**Where the filter applies.** Only the JSON-RPC dispatch
(``_h_cawl_index``). The file-route URI
(``<lang>/cawl/index.json`` via ContentProvider, which 0.41.2
clients use on Android) still serves the raw cache file
unfiltered — file FDs have no Binder size cap, so there's no
reason to slim, and the peer self-filters on extension in
either case. So:

- Pre-0.41.2 peer + 0.41.3 daemon → JSON-RPC path, ~470 KB,
  fits, peer gets a populated index without rebuilding.
- 0.41.2+ peer + 0.41.3 daemon → FD path, full index,
  unaffected.
- Pre-0.41.2 peer + pre-0.41.3 daemon → JSON-RPC path, full
  index, Binder drops the Bundle, peer reads empty (the
  regression).

**Decision log: filter at serve, not at cache.** The on-disk
cache (``$AZT_HOME/cawl/<owner>/<repo>/index.json``) keeps
the canonical full set GitHub returned. Cache stays repo-
faithful; serve-time filter is cheap and reversible. A
future endpoint that wants the full set (admin UI, indexing
tool) can read the cache directly.

### azt_collabd 0.41.2 / azt_collab_client 0.41.2 — CAWL index over file FD on Android (Binder 1 MB cap)

Patch fix: the daemon was serving the populated CAWL index
(``files=5479`` in the success log added in 0.41.1), but the
peer's ``cawl_index(langcode)`` wrapper read ``{}`` — peer
logged ``[cawl] _load: ... repo='' files=0`` and never
requested any images. Root cause: ``ContentResolver.call``
ships responses as a ``Bundle`` over Binder, which caps
single transactions at ~1 MB. The populated index
(~1.5 MB with 5000+ entries × long GitHub raw-content URLs)
exceeds the cap; the Bundle is dropped on the way back, the
peer's transport raises (caught by the wrapper as
``ServerUnavailable``), and the wrapper returns ``{}``. The
daemon-side success log fires regardless because the handler
ran — the gap is in the IPC return trip, not the dispatch.

**Fix.** On Android, ``cawl_index`` now reads the on-disk
index file directly via the ContentProvider's existing file
route (``<lang>/cawl/index.json``). ``ContentResolver.openFile
Descriptor`` returns a kernel FD with no Binder size cap; the
peer reads the JSON bytes and parses locally. The daemon's
``_resolve_cawl_path`` already populated the cache via
``cawl.get_index`` before returning the path, so the file is
guaranteed present (seed-on-cold-cache covers the
no-network-on-install case). Desktop loopback HTTP has no
such cap and keeps the JSON-RPC path.

**Client (azt_collab_client):**

- ``cawl_index(langcode)`` now branches on platform:
  Android → file-route via new ``lift_io._cawl_index_via_fd``
  helper; desktop → existing ``GET
  /v1/projects/<lang>/cawl/index`` over loopback HTTP.
  Empty-on-failure contract preserved on both paths.
- New ``lift_io._cawl_index_via_fd(langcode)`` — opens
  ``content://<authority>/<lang>/cawl/index.json`` via
  ``_open_content_uri``, reads, parses. Same URI shape
  ``CAWLHandle`` uses for image bytes.

**Decision log.** Not changing the daemon-side wire shape;
the JSON-RPC endpoint stays correct and serves desktop. The
asymmetry (HTTP path on desktop, FD path on Android) lives
on the client side because that's where the Binder cap is
visible. A future symmetric refactor could move both peers
to the FD path uniformly, but desktop has no FD provider
available without adding a new loopback file-serving
endpoint — not worth the surface area for an IPC-layer
workaround.

### azt_collabd 0.41.1 / azt_collab_client 0.41.1 — CAWL nested paths + success-path logging

Patch fix: 0.41.0's CAWL image fetching silently rejected any
``rel_path`` containing ``/``, which is exactly the shape the
canonical ``kent-rasmussen/images_CAWL`` repo uses
(``0001_body/foo.png``-style category subdirs). Net effect:
the seed index was served fine, but every per-image request
returned silently → peers saw no images, no daemon log line
recorded the failure. This release accepts nested rel-paths
and adds success-path logging so the next similar gap is
visible from logcat alone.

**CAWL daemon (azt_collabd/cawl.py):**

- ``_looks_safe_basename`` → ``_looks_safe_rel_path``. Accepts
  ``/`` between components; rejects ``..``/``.``, absolute
  paths, backslashes, and empty components.
- ``get_image_path`` now takes ``rel_path`` (not ``basename``).
  Composes the on-disk target under
  ``<cache_root>/<repo>/images/<rel_path>``; verifies
  containment with ``realpath`` + ``commonpath`` (belt-and-
  braces against symlink tricks). Creates intermediate cache
  subdirs on first write.
- ``_fetch_image_bytes_from_github`` URL-encodes each path
  component for the raw URL (``urllib.parse.quote(path,
  safe='/')`` — keeps slashes between components intact;
  encodes spaces, commas, parens, etc. that CAWL filenames
  commonly contain).

**Transport routing:**

- ``android_cp._resolve_cawl_path`` accepts 2+ segments under
  ``images/`` (was strict ``[images, basename]``). Joins
  remaining segments back into the rel-path that
  ``cawl.get_image_path`` validates.
- ``server._match_cawl_image_path`` accepts 7+ segments; per-
  component URL-decodes via ``urllib.parse.unquote``; rejects
  post-decode traversal tricks (``%2E%2E`` → ``..`` and
  ``%2F`` → ``/`` inside a single segment).

**Client (azt_collab_client/lift_io.py):**

- ``CAWLHandle(langcode, rel_path)`` — the ``basename`` arg
  renamed to ``rel_path``. ``handle.basename`` kept as a
  read-only alias for back-compat with peer log lines.
- ``CAWLHandle.open_read`` URL-encodes the rel-path with
  ``urllib.parse.quote(safe='/')`` before composing the
  ``content://`` URI or the loopback HTTP URL. Slashes
  between components preserved; unsafe characters percent-
  encoded.

**Success-path logging — new in both endpoints:**

- ``[cawl] served index for repo=… langcode=… files=N`` on
  every successful ``_h_cawl_index``. ``files=0`` is the
  early-warning signal that something's upstream-wrong
  (empty seed, mis-resolved repo, …).
- ``[cawl] served image repo=… path=… bytes=…`` on every
  successful ``_h_cawl_image_bytes``.
- ``[cawl] image rejected: project_not_found / no_image_repo_configured``
  and ``[cawl] image unavailable: repo=… path=…`` on the
  refusal paths. The 0.41.0 bug went unseen because none of
  the success-or-rejection paths logged — only the
  network-fetch-failed path did.

**Tests:** new in ``tests/test_cawl.py``:

- ``test_get_image_path_accepts_nested_rel_path`` — regression
  test for the 0.41.0 bug.
- ``test_get_image_path_accepts_spaces_and_special_chars`` —
  CAWL filenames in the canonical repo have these.
- ``test_fetch_url_encodes_path_components`` — slashes
  preserved, unsafe chars percent-encoded.
- ``test_match_cawl_image_path_accepts_nested_path``,
  ``_url_decodes_components``,
  ``_rejects_traversal_post_decode``,
  ``_rejects_slash_in_decoded_segment``.
- ``test_h_cawl_image_bytes_serves_nested_rel_path`` —
  end-to-end through the binary handler.
- ``test_resolve_path_cawl_image_accepts_nested`` — through
  the ContentProvider routing.
- Existing path-traversal test updated to remove
  ``sub/file.jpg`` from the rejected-shapes list and add
  ``a/../b`` / ``foo//bar`` as new rejection cases.

**Floor:** patch bump. No wire-format change beyond accepting
strictly more rel-path shapes. Pre-0.41.1 daemons reject
nested paths silently; pre-0.41.1 clients that pass flat
basenames work against 0.41.1 daemons unchanged (the new
endpoints accept both).

## [0.41.0] - 2026-05-12

### azt_collabd 0.41.0 / azt_collab_client 0.41.0 — collaborator UI consolidation + QR share/scan

Project-bound actions (Grant collaborator, Share repo) move into
the daemon settings UI's SettingsScreen, bound to the
``last_project()`` the daemon already tracks. Peers shrink to
a single "Open Sync Settings" button (``open_server_ui()``) for
these flows — same pattern as the GitHub Connect / GitLab
forms already on this screen.

Plus a QR pair: the daemon UI generates a QR of the published
repo URL ("Share this repo"), and the picker's clone flow
scans QRs to pre-fill its URL textbox.

**Daemon UI (azt_collabd/ui/app.py):**

- New ``project_actions_row`` in SettingsScreen, gated on
  ``last_project()`` resolving to a project that has a
  ``remote_url``. Mutually exclusive with the existing
  ``publish_row`` (which is gated on no remote) — the user
  sees one "what can I do with this project" surface at a
  time, appropriate to the project's current state.
- ``Grant collaborator access`` button → invokes the shared
  ``grant_collaborator_popup(langcode=last_project())``. The
  popup itself already lives in
  ``azt_collab_client.ui.popups`` (no work needed there).
- ``Share this repo (QR)`` button → opens a new
  ``_show_share_repo_qr_popup`` that renders the remote URL
  as a QR via segno + a Kivy ``Image`` widget, with a "Copy
  URL" fallback that goes through ``kivy.core.clipboard``.
- ``SettingsScreen.publish()`` updated for the 0.40.0 wire —
  no longer passes ``contributor=`` to ``init_project``
  (daemon reads from store). Adds ``S.CONTRIBUTOR_UNSET`` to
  the publish-failed-codes set so the publish msg routes
  correctly when the user hasn't entered their name yet.
- ``CollabUIApp.font_name`` now an instance attribute (was
  a local in ``build()``) so screens can pass it to shared
  popups for visual consistency (CharisSIL across daemon UI
  surfaces).

**QR generation (segno):**

- New requirement: ``segno`` in
  ``server_apk/buildozer.spec`` (and ``.tmpl``). Pure-Python,
  ~50 KB, no native deps. PNG output uses Pillow which Kivy
  already pulls in.
- ``_show_share_repo_qr_popup(url, langcode, font_name)`` —
  module-level helper in ``app.py``. Generates the QR with
  ``error='M'`` (15% correction, good camera tolerance) and
  ``scale=8`` (~250 px square in the popup). Falls back to
  ``_show_segno_missing_popup`` on desktop installs where
  segno isn't pip-installed.

**QR scan (zxing-android-embedded):**

- New requirement: ``com.journeyapps:zxing-android-embedded:4.3.0``
  in ``android.gradle_dependencies`` (server APK only). ~500 KB
  AAR; pulls in the camera-preview CaptureActivity + barcode
  decoder. Android-only.
- New permission: ``CAMERA`` in ``android.permissions``. ZXing
  requests the runtime grant itself at CaptureActivity launch;
  we only need the manifest entry.
- New module ``azt_collab_client/ui/qr_scan.py``:
  ``scan_qr(on_result, on_cancel, prompt)`` launches ZXing's
  ``IntentIntegrator``, reads ``SCAN_RESULT`` from
  ``onActivityResult``, marshals the callback to the Kivy main
  thread. ``available()`` is the cheap probe peers use to gate
  the UI affordance on platforms where ZXing isn't bundled.
- ``clone_url_popup`` (the picker's clone-by-URL flow) grows a
  "Scan QR" button next to the URL textbox when
  ``qr_scan.available()`` is True. On scan success the
  textbox is filled with the decoded URL and the existing
  ``_refresh_label_from_url`` derives the langcode. Desktop
  / no-ZXing builds keep the original "paste URL" UI.

**Floor:** no bumps. Daemon UI additions are server-side only;
peer wire surfaces unchanged. Pre-0.41 daemons / clients
interoperate normally (peers just don't see the new daemon-UI
buttons, which is fine — those weren't there before either).

**Recorder follow-up (deferred to a NOTES_TO_PEERS item back
to the recorder team).** Now that the consolidated surface
exists in the daemon UI, the recorder's CollabScreen Publish +
Grant collaborator sub-screens become redundant. Strip-out
follows Phase 3 from NOTES_TO_DAEMON.md (don't combine with
this release — peer that strips before daemon grows the
replacement loses the feature entirely until both sides
converge).

### azt_collabd 0.40.0 / azt_collab_client 0.40.0 — commit author moves to daemon; device-name disambiguator

Two coordinated changes, one release:

1. **Contributor name is strictly daemon-owned now.** Peers no
   longer pass a commit-author name on the wire; daemon endpoints
   ignore any ``body['contributor']`` and read the stored value
   directly. If no name is set, commit-issuing endpoints refuse
   with the new ``S.CONTRIBUTOR_UNSET`` status — peers route the
   user to the daemon settings UI (``open_server_ui()``) to set
   their name rather than silently producing meaningless
   ``"Recorder"`` commits.
2. **New ``device_name`` field** disambiguates commits when the
   same human contributes from multiple devices. The git author
   email slot becomes ``<safe_contributor>@<safe_device>`` so
   GitHub's author-aggregation still groups by person, while
   ``git log --format='%ae'`` differentiates by device. Auto-
   populates from the OS on first read (Android:
   ``Settings.Global.DEVICE_NAME`` → ``Build.MANUFACTURER +
   MODEL``; desktop: ``socket.gethostname()``); user can override
   via the settings UI for a friendlier label.

**Why this lands together.** Both are corollaries of the
"daemon is the sole authoritative source for per-user state"
rule (NOTES_TO_DAEMON.md, recorder 1.41.3 filing). Pre-0.40 the
contributor name was duplicated across peer and daemon, with
the peer's pass-through silently winning even when the user
typed a name in the daemon UI; the literal ``"Recorder"``
default in the client wrapper turned every peer that didn't
override it into a commit signed "Recorder". 0.40 closes both
issues by removing the wire surface and replacing the
placeholder ``@device`` email slot with a real disambiguator.

**Wire changes:**

- ``POST /v1/projects/init`` — ignores ``body['contributor']``.
- ``POST /v1/projects/<lang>/sync`` — ignores ``body['contributor']``.
- ``POST /v1/projects/<lang>/sync_async`` — ignores
  ``body['contributor']``; the enqueued job runs against the
  stored contributor at exec time. If unset, scheduler returns
  ``Result(CONTRIBUTOR_UNSET)`` which peers see via
  ``poll_job(job_id)``.
- ``GET /v1/config/device_name`` — new. Returns the stored or
  auto-detected device name (always non-empty after first read).
- ``POST /v1/config/device_name`` — new. Sets / clears the
  override. Whitespace stripped; empty clears and re-triggers
  autodetect on next read.

**Client API changes:**

- ``init_project(working_dir, remote_url, branch='main')`` —
  ``contributor`` kwarg removed.
- ``sync_project(langcode)`` — ``contributor`` parameter
  removed.
- ``request_sync(langcode)`` — ``contributor`` parameter
  removed.
- New ``get_device_name()`` / ``set_device_name(name)``
  wrappers, exported in ``__all__``.
- New ``S.CONTRIBUTOR_UNSET`` status code, translation in
  ``translate.py``.

**Daemon-side changes:**

- ``store.resolve_contributor`` **removed**. Was the host of the
  ``'Recorder'`` fallback. Any in-tree caller that still imports
  it fails at import time — fail-loud.
- ``store.get_device_name`` / ``store.set_device_name`` new.
  Auto-populates on first read; persists the autodetect so
  subsequent reads are stable.
- ``repo._default_author(contributor, device_name=None)`` —
  ``device_name=None`` lazy-looks-up via ``store.get_device_name()``;
  ``''`` explicitly skips the lookup (deterministic test
  output). Email slot is ``<safe_contributor>@<safe_device>``;
  the literal ``@device`` placeholder is gone.
- ``scheduler.Job`` no longer carries a ``contributor`` field;
  ``request_sync(langcode)`` signature drops the second
  positional. Pre-0.40 ``jobs.json`` entries with the field
  decode cleanly (ignored on load).
- ``_h_init_project`` / ``_h_project_sync`` refuse upfront with
  ``S.CONTRIBUTOR_UNSET`` when ``store.get_contributor()`` is
  empty. ``_h_project_sync_async`` enqueues unconditionally;
  the scheduler's exec-time re-check at ``_run_sync`` is the
  defence-in-depth.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bumps. Pre-0.40 clients that still pass ``contributor`` in the
body get their value silently ignored (strict improvement —
the override they sent was the bug). Pre-0.40 daemons talking
to a 0.40 client work unchanged (the client just doesn't send
the field). No corruption surface, no forced cut-over.

### azt_collabd 0.39.0 / azt_collab_client 0.39.0 — per-project `repo_slug` field

Closes the recorder 1.41.3 ask from
``NOTES_TO_DAEMON.md``: the GitHub-repo-name override that the
publish path uses now lives on the daemon's project record, not
in peer prefs.

**Why this is needed.** Most projects publish to a repo named
after the project's ``langcode``, and the daemon's
``projects.json`` key is the right value to display. But a user
can legitimately want a *different* repo name (vanity slug,
project-style naming convention, collision avoidance with an
existing GitHub repo) without changing the LIFT
``<form lang="…">`` tag. Pre-1.41.3 the recorder persisted that
override as a suite-wide ``peer_pref`` scalar
(``collab_langcode``), which was wrong on two counts:
peer-prefs are global but the override is per-project, and
peer-side storage of project-identity data violates the
no-daemon-owned-caches rule (also documented in
``NOTES_TO_DAEMON.md`` "Daemon is now the sole authoritative
source"). 1.41.3 dropped the peer-side mirror; this release
gives the data its canonical daemon-side home.

**Wire shape:**

- ``Project.repo_slug`` field (string, default empty).
  Returned by ``open_project`` / ``project_status`` /
  ``list_projects`` so peers can read it without an extra
  round-trip.
- New endpoint ``POST /v1/projects/<lang>/repo_slug`` —
  body ``{repo_slug: '<name>'}``. Whitespace stripped before
  persist. Empty string explicitly clears (callers fall back
  to using ``langcode``). 404 on unknown project, 400 on
  missing field.
- New client wrapper ``set_repo_slug(langcode, slug)`` —
  returns the updated ``Project`` or ``None`` on transport
  failure / unknown project, same shape as
  ``set_cawl_image_repo``.
- ``register_project`` now accepts ``repo_slug=…`` for the
  initial-creation path (alongside the existing
  ``cawl_image_repo`` kwarg). ``None`` preserves any existing
  value; ``''`` explicitly clears.

**Default-semantics rule for callers:** unset / empty
``repo_slug`` is the typical case — callers should treat that
as equal to ``langcode``. The daemon does NOT auto-fill the
field with the langcode; the field stays empty until the user
explicitly overrides. That keeps "did the user actually choose
a different name?" decidable from the data alone.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bumps. The field is purely additive — pre-0.39 daemons don't
emit it, the client-side dataclass defaults to ``''`` for
forward-compat. A 0.39 client calling ``set_repo_slug``
against a pre-0.39 daemon gets ``None`` (the endpoint returns
404), which is the same failure shape every other setter
wrapper uses.

**Removed from NOTES_TO_DAEMON.md:** the "Per-project
repo-slug override (publish path)" entry — shipped.

### azt_collabd 0.38.1 / azt_collab_client 0.38.1 — CAWL index seed (install-day-no-network)

Closes the install-day-no-network gap that Stages 1+2 (0.38.0)
couldn't solve on their own: a freshly-installed device that has
never reached GitHub now has a bundled CAWL index to serve, so
peers can render illustrations on first launch.

**Daemon side:**

- New ``_seed_index_if_bundled(repo)`` in ``azt_collabd/cawl.py``.
  ``get_index(repo)`` calls it before going to the network when
  the on-disk cache file is missing — copies the bundled asset
  from ``azt_collabd/data/cawl/<owner>/<repo>/index.json`` into
  ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` if it exists.
- Silently no-ops when (a) no seed is shipped for the requested
  repo, (b) the cache already has data, (c) the bundled JSON is
  malformed. The first case is the common one: only the
  suite-canonical repo is typically seeded; fork / per-project-
  override repos keep their old install-day behaviour (no
  illustrations until the network fetch lands).
- The seed is treated as an ordinary cache entry once written:
  fresh-within-TTL → serve directly; past-TTL → attempt a
  network refresh and fall back to the seed if offline (the
  pre-existing stale-cache fallback). When the device first
  gets online, the next refresh overwrites the seed with current
  data.

**Bundle layout:**

```
azt_collabd/data/cawl/
    <owner>/<repo>/
        index.json
```

Subdirectory name matches the on-disk cache layout exactly.
``azt_collabd/data/cawl/generate_seed.py`` is the maintainer
script: with no args it refreshes the suite-canonical seed
(daemon-global default ``kent-rasmussen/images_CAWL``); pass
``owner/repo`` or set ``AZT_CAWL_IMAGE_REPO`` for a fork /
non-canonical image set. Uses the same
``cawl._fetch_index_from_github`` codepath the daemon does at
runtime, writing to the right directory.
``azt_collabd/data/cawl/README.md`` documents the wire shape +
when to re-run.

The daemon-global ``cawl_image_repo`` default is no longer empty
— recorder 1.41.3 removed its own hard-coded fallback under the
no-daemon-owned-caches rule, so the daemon is now the sole
source of this slug at runtime. Default set to
``kent-rasmussen/images_CAWL`` to preserve the recorder's
pre-1.41.3 behavior; fork shipping a different CAWL set should
override via ``azt_collabd.configure(cawl_image_repo=…)`` or
``AZT_CAWL_IMAGE_REPO``.

**Buildozer:**

- ``server_apk/buildozer.spec`` and ``buildozer.spec.tmpl`` add
  ``json`` to ``source.include_exts`` so the bundled seed lands
  in the APK. No other build-config changes; new seed
  directories under ``azt_collabd/data/cawl/`` are picked up
  automatically.

**What's NOT bundled, and why:**

The image binaries themselves are explicitly **not** in the
seed. 1701 images at 50–200 KB each ≈ 100–300 MB per APK
release — wrong trade for a one-time first-launch UX gain.
Image rendering on day-one without connectivity simply doesn't
happen; the user gets illustrations once the device first
reaches ``raw.githubusercontent.com``, with the daemon-side
lazy cache (shipped in 0.38.0) covering steady-state perfectly
fine. If a future session proposes "bundle the whole CAWL
image set in the APK", that's a re-litigation of a 2026-05-12
decision — answer is no.

**Floor:** patch bump (no wire-format change). The seed is
purely additive on the daemon side; pre-0.38.1 clients get
exactly the same wire shape from ``GET /v1/projects/<lang>/
cawl/index`` — they just benefit from a populated cache they
didn't have to fetch themselves.

### azt_collabd 0.38.0 / azt_collab_client 0.38.0 — CAWL Stage 2: per-project image_repo, image-binary RPC, first non-JSON endpoint

Completes the CAWL daemon-side migration that 0.37.0 started, and
corrects 0.37.0's daemon-global ``cawl_image_repo`` stopgap to a
per-project field on the Project record. CAWL is now a fully
daemon-owned suite-scoped resource: peers consume both the index
and the image binaries via the daemon, with one cache per repo
per device regardless of peer count.

**The reframing.** 0.37.0 used a daemon-global ``cawl_image_repo``
configured at daemon startup. That conflicted with the
"sole authoritative source" architectural invariant the recorder
1.41.3 just established (NOTES_TO_DAEMON.md): per-project
identity / configuration data belongs on the project record, not
in peer prefs and not in daemon-global config. Different projects
can legitimately point at different image sets (vanity fork,
culturally specific imagery, etc.) so the slug is a per-project
override with a daemon-global fallback.

**Project record (azt_collabd/projects.py):**

- New ``Project.cawl_image_repo`` field (string, default empty).
  Empty falls back to the daemon-global default; non-empty
  overrides for this project.
- ``register(..., cawl_image_repo=None)`` accepts the kwarg.
  ``None`` preserves any previously-set value across
  re-registration; empty string explicitly clears.
- New ``set_cawl_image_repo(langcode, repo)`` setter for the
  endpoint.
- Client-side ``Project`` mirror gains the field with default
  empty (forward-compat with pre-0.38 daemons that don't emit it).

**Cache module (azt_collabd/cawl.py):**

- ``get_index(repo)`` now takes a repo slug. Cache moves to
  ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` so multiple
  projects pointing at the same image_repo share one cache
  directory.
- New ``get_image_path(repo, basename)``: lazy fetch from
  ``raw.githubusercontent.com``, cache at
  ``<owner>/<repo>/images/<basename>``, return absolute
  filesystem path. Path-traversal-safe basename validation.
  ``None`` when fetch fails and no prior cached copy exists.
- New ``resolve_image_repo(langcode)``: per-project value
  preferred; daemon-global ``config.cawl_image_repo()`` is the
  fallback for projects without an override.
- Lock-coalesced fetches keyed by cache file path (not module-
  wide), so two repos can fetch in parallel without
  serializing.

**Endpoints:**

- ``GET /v1/projects/<lang>/cawl/index`` (replaces the 0.37.0
  ``GET /v1/cawl/index``). Daemon resolves the project's
  cawl_image_repo internally; response carries
  ``index_repo`` alongside ``index`` so peers can see which
  repo answered.
- ``GET /v1/projects/<lang>/cawl/images/<basename>`` returns
  **raw binary image bytes**. First non-JSON endpoint on the
  loopback HTTP server; new ``_send_bytes`` handler bypasses
  JSON dispatch (the dispatch table stays JSON-only). Content-
  type derived from the file extension
  (``image/jpeg``/``png``/``gif``/``webp`` known; falls back
  to ``application/octet-stream``).
- ``POST /v1/projects/<lang>/cawl_image_repo`` setter for the
  per-project override. Body ``{cawl_image_repo: 'owner/repo'}``;
  empty string explicitly clears.

**ContentProvider (azt_collabd/android_cp/service.py):**

- ``_resolve_path`` extended with two new shapes:
  ``<lang>/cawl/index.json`` (3-seg, triggers lazy index fetch)
  and ``<lang>/cawl/images/<basename>`` (4-seg, triggers lazy
  image fetch).
- CAWL paths resolve to ``$AZT_HOME/cawl/<owner>/<repo>/...``
  (away from the per-project working_dir) so the dedup-by-repo
  property of the cache layer is preserved on Android.
- Write modes (``w``/``a``) rejected — peers don't write CAWL
  files. Returns ``None`` so the Java side surfaces
  ``FileNotFoundException``.

**Client side:**

- ``cawl_index()`` → ``cawl_index(langcode)``. The 0.37.0
  shape is gone; pre-0.38 callers must pass a langcode.
- New ``set_cawl_image_repo(langcode, repo)`` wrapper.
- New ``CAWLHandle(langcode, basename).open_read()`` —
  binary file-like for a CAWL image. Branches transport
  internally: Android opens the ContentProvider URI
  (zero-copy via kernel FD); desktop hits the loopback HTTP
  endpoint and returns ``io.BytesIO`` wrapping the response.
  Read-only (peers don't write CAWL images). Raises
  ``FileNotFoundError`` on 404 / no cached copy / fetch
  failure; raises ``ServerUnavailable`` on transport failure.
- All exposed via ``__all__`` and re-exported.

**What's left (not in 0.38.0):**

- ⏳ APK-bundled INDEX seed (independent piece; ~50 KB asset
  in ``server_apk/assets/cawl/index.json`` so install-day-no-
  network gets a populated index without GitHub access).
  Image binaries are NOT bundled — a 100–300 MB per-release
  payload is the wrong trade; lazy daemon caching covers the
  steady state. Filed in NOTES_TO_DAEMON.md.
- ⏳ Peer migration in the recorder (swap direct
  ``urllib.request.urlopen(raw_url)`` + per-peer cache for
  ``CAWLHandle.open_read()``; UI affordance to set
  ``cawl_image_repo`` per project). Lives in the recorder
  repo; not blocked on anything here.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bump. The 0.38.0 endpoints are net-new; pre-0.38 clients
calling them get 404, the wrappers return ``{}`` / ``None``,
peers continue to use their pre-migration paths. Older clients
talking to a 0.38 daemon work unchanged — they just don't call
the new endpoints. The 0.37.0 ``GET /v1/cawl/index`` endpoint
is removed; no peer has adopted it yet (0.37.0 didn't ship a
real release).

**Bootstrap decline cadence: one-shot, not permanent.**
``bootstrap()``'s self-update decline mechanism (the "Not now"
button on the peer self-update prompt and the
already-declined branch of the server-too-old install prompt)
is now **one-shot**. Recording a decline suppresses exactly
the next launch's prompt and clears, so the cadence is
prompt → decline → skip → prompt → decline → skip → … rather
than the previous "never ask again for this version" shape.
A new upstream version still invalidates the stored value
the same way (exact-string compare).

Motivation: permanent decline-by-version painted users into
a corner where reconsidering required waiting for the next
upstream tag. One-shot gives the user a launch's breathing
room without trapping them. Implementation: new
``_consume_decline(repo, version)`` does the read-then-clear
in one step; ``_declined_version`` stays as a non-destructive
peek for tests / diagnostic use.

### azt_collabd 0.37.0 / azt_collab_client 0.37.0 — daemon-owned CAWL image-URL index cache

Moves CAWL image-URL index ownership from the peer to the daemon
to close out the 60/hr GitHub rate-limit symptom reported in
NOTES_TO_DAEMON.md (filed 2026-05-11). The fundamental reframing:
the index is *suite-scoped* shared infrastructure, not
peer-scoped, so it belongs on the daemon and peers consume it.

**Why this is needed.** Pre-0.37, each peer hit
``api.github.com/repos/<image_repo>/git/trees/HEAD?recursive=1``
directly on every project load and cached the result in a
per-peer in-memory dict. GitHub caps unauthenticated REST at
60 requests / hour / IP, which a dev rebuild loop, CI run, or
multi-peer device blows trivially. Once exhausted, the
resolver returns empty for the rest of the session and entries
without a locally-cached image render with no illustration.
Three structural failure modes flow from peer ownership:

1. **Rate limit exhaustion** — described above.
2. **Per-peer duplication** — N peers on the same device each
   do the same work; Android's sandbox prevents sharing even
   the cache file.
3. **Install-day-no-network** — a fresh install with no
   connectivity has no way to populate the index, so
   first-launch UX is "no illustrations" regardless of what's
   on disk.

The recommendation discussion (transcript 2026-05-12) ranked
three hosting fixes (bundle in APK / proxy through daemon /
sign with GitHub App token) and then re-framed: the deeper
problem is *where the cache lives*. Moving ownership to the
daemon serves the same data, removes the per-peer fan-out,
and lets the time-bounded refresh policy do the rate-limit
work once per device per day.

**Daemon side (`azt_collabd/cawl.py`):**

- New module owns ``$AZT_HOME/cawl/index.json``.
  ``get_index(force_refresh=False)`` returns the index dict,
  refreshing from GitHub on cache miss / past-TTL / explicit
  force. TTL is 24h (``_INDEX_TTL_SECONDS``).
- Lock-coalesced fetch: two peers calling ``get_index`` on a
  cold cache result in exactly one network round-trip; the
  second caller reads the freshly-written file.
- Stale-cache fallback: a network failure with a cached copy
  on disk returns the cached copy (even if past TTL). A
  network failure with no cache returns ``{}``. Peer code
  treats ``{}`` the same way it treated its pre-migration
  empty resolver dict, so there's no new "daemon failed"
  branch to write.
- Daemon stays naming-convention-agnostic. The wire shape is
  ``{repo, branch='HEAD', fetched_at, files: [{path, url}]}``.
  Peers do the filename → CAWL-identifier mapping themselves
  (the recorder has its own convention; future peers may
  differ).

**Config (`azt_collabd/config.py`):**

- New ``cawl_image_repo`` config kwarg on ``configure()``,
  with ``AZT_CAWL_IMAGE_REPO`` env-var override. Empty
  default — peers must configure it before any fetch
  happens. An unconfigured daemon short-circuits
  ``get_index()`` to ``{}`` without any network call, so a
  misconfigured launch doesn't silently hammer the wrong
  GitHub repo.

**Wire shape:**

```
GET /v1/cawl/index
→ {
    "ok": true,
    "index": {
        "repo":       "<owner>/<repo>",
        "branch":     "HEAD",
        "fetched_at": <unix-seconds>,
        "files": [
            {"path": "cawl-1234.jpg",
             "url":  "https://raw.githubusercontent.com/.../cawl-1234.jpg"},
            ...
        ]
    }
  }
```

Empty dict at ``index`` (``{}``) means "no images known" —
same shape peers got from an empty pre-migration resolver,
so no new failure branch is required.

**Client side:**

- New ``cawl_index()`` wrapper in
  ``azt_collab_client/__init__.py``. Returns the dict on
  success, ``{}`` on transport failure or empty daemon
  response. No raw ``ServerUnavailable`` reaches the caller.
- Re-exported from ``__all__``.

**What this fixes vs. what's left.**

- ✅ Index fetch no longer per-peer. One daemon-side fetch
  per device per 24h TTL.
- ✅ Rate-limit blow-up under dev rebuild / multi-peer use.
- ✅ Stale-cache survival across GitHub outages.
- ⏳ Image *binaries* still fetched by peers directly from
  ``raw.githubusercontent.com``. That endpoint is on a much
  more permissive rate-limit domain (effectively unmetered
  for normal use), so it isn't the bottleneck. Migration to
  a daemon-served provider URI for binaries is Stage 2 — same
  shape (suite-scoped resource, daemon ownership, peer
  consumes via provider) but a larger touch (every peer's
  image-resolution path). Filed as the remaining piece in
  NOTES_TO_DAEMON.md after the 0.37.0 cut.
- ⏳ Install-day-with-no-network still has no bundled
  index seed. ~50 KB index JSON shipped as a server APK
  asset would close the gap; daemon copies into
  ``$AZT_HOME/cawl/`` on first start if empty. NOT
  bundling image binaries — 100–300 MB per release is
  the wrong shape for Android distribution; daemon-side
  lazy caching (Stage 2 RPC) is how the binary
  deduplication / cross-peer-sharing wins land.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
is bumped. The endpoint is purely additive — a pre-0.37 client
calling ``cawl_index()`` gets ``{}`` from a 0.37 daemon and is
fine; a 0.37 client calling ``cawl_index()`` against a pre-0.37
daemon hits a 404, the wrapper falls back to ``{}``, and the
peer continues to resolve illustrations from its own legacy
fetch path. Peers migrate at their own pace; no hard cut-over.

### azt_collabd 0.36.0 / azt_collab_client 0.36.0 — `atomic_commit` RPC for URI atomic writes, MIN_SERVER_VERSION lifted hard to 0.36.0

Closes the last cross-process atomic-write gap: peers writing
LIFT / audio / image bytes through a ``content://`` URI on
Android now ship the full payload to the daemon, which performs
the tempfile + ``os.replace`` atomic write in its own process.

**Why this is needed.** On Android the daemon's working_dir
lives in the standalone server APK's private filesDir. Peers
write to it via ``ContentResolver.openFileDescriptor``, which
returns an FD into the daemon's filesystem. ``ftruncate(fd, 0)``
+ subsequent writes through that FD are NOT atomic from any
other observer's perspective: a concurrent peer write, or the
daemon's own merge-output write, can see torn bytes mid-write.
The 2026-05-12 ``baf`` repro showed exactly this — two peer
serializations interleaved through the FD path produced
malformed XML which the daemon then misparsed catastrophically.

The 0.35.4 client added a path-keyed lock so a single peer
process can't race with itself, but cross-peer-process and
peer-vs-daemon races stayed open. 0.36.0 closes them.

**Wire shape:**

```
POST /v1/projects/<lang>/atomic_commit
{
  "path": "<rel_path>",
  "data_b64": "<base64-encoded-bytes>"
}
```

``rel_path`` is one of ``<file>.lift``, ``audio/<file>``,
``images/<file>`` — same whitelist as the ContentProvider's
``_resolve_path``. Path-traversal and out-of-whitelist shapes
return 400 before any filesystem touch.

The daemon serializes the write through ``project_lock`` (so it
can't overlap with a sync's merge-output write or another
atomic_commit) and writes via tempfile + ``os.replace`` in its
own process. The destination is always a complete copy of one
version, never a torn mix.

Response: ``{ok: True, result: {statuses: [{code: 'ATOMIC_COMMITTED',
params: {bytes_written, sha256}}]}}``. The sha256 lets the peer
verify the bytes that landed match what it sent.

**Client side:**

- New ``atomic_commit_bytes(langcode, rel_path, data) -> Result``
  wrapper in ``azt_collab_client/__init__.py``. Transport
  failures translate to ``SERVER_UNAVAILABLE`` / ``SERVER_ERROR``
  per the existing contract; the peer never sees a raw
  ``ServerUnavailable``.
- New ``_UriAtomicWriteFile`` in ``lift_io.py`` buffers writes
  in memory and ships them on commit. Memory cost: ~1.33× the
  file size (base64 encoding) during the encode-and-send window.
  For LIFT (tens of MB at worst) this is fine.
- ``LiftHandle.atomic_open_write`` on a URI now returns
  ``_UriAtomicWriteFile`` (the 0.35.4 fallback to plain
  ``open_write`` is gone). Filesystem-path callers still get
  the local ``_AtomicWriteFile`` (tempfile + ``os.replace`` in
  the peer process).
- ``MediaHandle`` inherits the same shape — audio and image
  atomic writes also go through the RPC on URI projects.

**Floor:** ``MIN_SERVER_VERSION 0.35.4 → 0.36.0`` (hard).
Peers rebuilt against 0.36.0 clients won't pair with pre-0.36.0
daemons, so the install/update prompt fires before any sync
attempt. Pre-0.36.0 clients are still allowed against a 0.36.0
daemon — they just don't get the atomic-URI-write benefit (the
old client doesn't know about the endpoint). MIN_CLIENT_VERSION
does NOT bump for that reason: the new endpoint is additive,
not breaking.

### azt_collabd 0.35.4 / azt_collab_client 0.35.4 — atomic LIFT writes from peers, forensic dumps on every guard trip, MIN_SERVER_VERSION lifted to 0.35.4

Two complementary pieces from the same investigation:

**Client side (`azt_collab_client/lift_io.py`):**

- `LiftHandle.open_write` is now **serialized within the peer
  process via a path-keyed reentrant lock**. Two threads of the
  same peer calling `open_write` on the same target queue
  rather than race. Pre-0.35.4 a rapid-succession `open_write`
  pattern (e.g., the recorder serializing the LIFT twice in
  close succession after two audio captures) could interleave
  at the byte level — each `open_write` opens an independent FD
  and `ftruncate(0)`s the file, so two writes from offset 0
  produce malformed bytes with torn tag boundaries. The
  `baf` 2026-05-12 repro shows two same-lang `<gloss>` elements
  with one's `<text>` mid-stream embedded in the other's; that's
  the signature.
- New `LiftHandle.atomic_open_write` context manager. Writes go
  to a sibling tempfile with a random suffix; on clean exit
  (`__exit__` with no exception), `os.replace` atomically renames
  the tempfile over the destination. On exception, tempfile is
  removed and the destination is untouched. Filesystem paths get
  true atomic semantics; content:// URIs fall back to the
  lock-protected `open_write` (ContentResolver has no clean
  atomic-rename for arbitrary Provider URIs). Two concurrent
  `atomic_open_write` calls on the same destination are safe:
  each writes its own random-suffixed tempfile, and whichever
  `os.replace` runs last wins — the destination is *always* a
  complete copy of one version, never torn.
- `MediaHandle` inherits both behaviors transparently
  (audio and image writes also benefit).

**Daemon side (`azt_collabd/lift_merge.py` + `repo.py`):**

- New `build_diagnostic_xml` / `diagnostic_filename` /
  `is_guard_kind` / `DIAGNOSTICS_SUBDIR` helpers in
  `lift_merge.py`. The diagnostic XML schema captures:
  - `guard` kind, daemon version, UTC timestamp.
  - `merge-context`: lift path + the three commit SHAs
    (local / remote / base) so the bytes are reachable via
    `git show` from any clone.
  - `process`: pid, ppid, executable, cwd.
  - `thread`: name + ident of the thread that hit the guard,
    plus the names + idents of every other live thread (so
    a concurrent-call hypothesis can be tested from the
    dump).
  - `caller-stack`: the `traceback.extract_stack` slice at
    guard-fire time, file + line + function for each frame.
  - `filesystem-state`: stat results for the working-tree
    LIFT path, .git directory, and `.azt-collab/diagnostics`
    so disk-full or permission anomalies are recoverable.
  - `inputs`: per side, byte length, sha256, parsed entry
    count, parse-error message (when parsing failed).
  - `merged`: byte length, sha256, entry count, parse-error
    (when applicable).
  - `conflict-fields`: the diagnostic strings the guard
    produced (e.g., the `_looks_truncated` or
    `_looks_catastrophic_output` message).
  - `recent-trace`: a slice of the in-process ring buffer
    (`_TRACE_RING_SIZE = 500` entries, default last 120 s)
    capturing the daemon's pre-guard activity. Every
    `[sync-trace]` / `[merge-trace]` / `[merge-diag]` line
    in `azt_collabd` now routes through `lift_merge.trace()`,
    which appends to the ring AND prints to stderr — so
    dumps carry the same time-precise log slice that logcat
    would have shown if anyone had been looking.
- `repo._merge_diverged` now dumps the diagnostic to
  `<working_dir>/.azt-collab/diagnostics/<utc>-<guard>-<nonce>.xml`
  whenever a guard fires on a .lift merge. The file gets staged
  into the merge commit by the existing `_stage_all` call and
  pushed to the remote alongside the safe merge result. A
  pre-existing `_write_merge_diagnostic` helper does the write
  via tempfile + `os.replace` so a half-written diagnostic can't
  be staged.

  Best-effort: a diagnostic-write failure logs to stderr and
  the merge proceeds. We don't want the audit trail to block
  the merge if e.g. the disk is full.

  **User isn't bothered.** The file lives under a hidden
  `.azt-collab/` directory and is mentioned only in
  `[merge-diag]` log lines. The intent is forensic — when a
  guard fires (rare; ideally never), the daemon team or a
  future-LLM analysis can `git log .azt-collab/diagnostics/`
  on any clone of the repo, find the dump, and reconstruct
  exactly what the merger saw. No console prompts, no UI
  surfacing.

**Versioning + floor:**

- `azt_collabd 0.35.3 → 0.35.4`.
- `azt_collab_client 0.35.3 → 0.35.4`.
- `MIN_SERVER_VERSION 0.35.3 → 0.35.4` hard. Pre-0.35.4 daemons
  still have all the guards (those landed in 0.35.1–0.35.3) but
  log guard trips to stderr only — Android logcat is ephemeral
  and not retrievable. The user explicitly asked that every
  guard firing be recoverable from the repo for post-hoc
  analysis; pinning the floor here is the discipline that
  enforces it.

**Why this is the right shape, in one sentence:** any future
guard firing automatically leaves a small structured XML file
in git that says exactly what the merger saw — without the
user having to do anything, and without polluting their LIFT
or audio data.

### azt_collabd 0.35.3 / azt_collab_client 0.35.3 — output-side catastrophic-loss guard, MIN_SERVER_VERSION lifted hard to 0.35.3

The closed merge note (filed 2026-05-11, "merge driver reorders
entries to guid order") was reopened on 2026-05-12 with new
commit-level evidence from the `baf` project: merge commit
`679c102` produced 1 entry from inputs of 1702 and 1700 entries
(base ~1700). The input-side truncation guard added in 0.35.1
**cannot have fired** for these inputs (both well above the
threshold). Yet the daemon's `lift_merge.three_way_merge`
committed a 1-entry merge result, annotated `azt-lift-conflict
value="theirs"` on the surviving entry, with the original guid.

**Bug-shape analysis** (recorded so the institutional
knowledge survives even if the proximate cause is never
narrowed):

The surviving entry's annotation form — single entry,
`value="theirs"`, original (non-`-theirs`-suffixed) guid — is
produced by exactly one pre-v3 code path: the `delete-modify`
branch, which fires when `ours_entries.get(guid) is None`
while `theirs_entries.get(guid)` is present (with content
differing from base). For 1699 of the 1700 entries to be
dropped through that branch's sibling code (`if _canon(b) ==
_canon(t): continue   # they didn't change it; we deleted`),
**the merger's internal `ours_entries` view had to be
near-empty at the moment of the merge**. Yet `git show
dc69264:baf.lift | grep -c '<entry '` shows 1702 entries in
the committed blob.

That contradiction — committed blob is full, the merger's view
was near-empty — points at a non-deterministic ordering
problem we can't reproduce without the daemon logs from the
exact minute (`Tue May 12 13:17:40 UTC 2026`). Plausible
proximate causes ranked by likelihood:

1. **Two `_merge_diverged` calls raced.** Concurrent syncs
   (the auto-sync on project select + a manual sync, or two
   peers' sync requests on the daemon, or any debounce race)
   each independently called `_walk_tree` on a working-tree
   snapshot. If one snapshot caught a mid-write LIFT (peer's
   `MediaHandle.open_write` had `ftruncate(0)`'d the file at
   `lift_io._open_content_uri` line 211 but the subsequent
   write hadn't completed), the merger saw a truncated ours
   and committed the destructive merge. The OTHER call (with
   the full file) then committed `dc69264` on top — making
   the committed blob look healthy in retrospect.
2. **Mid-write commit, immediately rewritten.** A peer's
   write to the LIFT was interrupted (process killed, OOM,
   activity teardown) right after `ftruncate(0)` and before
   the bulk write completed. The daemon's commit_audio_and_sync
   captured the truncated state, merged, and committed
   `679c102`. The peer restarted, finished writing, and
   committed `dc69264` later — making the "before" commit
   look healthy in retrospect.
3. **A path-level cache or staging issue** in
   `_merge_diverged` returning blob bytes that don't match
   what's now at `git show dc69264:baf.lift`. Less likely
   given dulwich reads commit→tree→blob deterministically,
   but not ruled out.

Without the daemon logs of that minute, we cannot prove
which (if any) of these was the proximate cause. **What we
CAN do is make the next occurrence harmless.**

**Most likely proximate cause** (refined after the user's
follow-up observation that the surviving entry shape —
single entry, `value="theirs"`, ORIGINAL guid — uniquely
identifies the **delete-modify** branch, not modify-modify
— which forces the conclusion that `ours_entries` was empty
at merge time):

`grep -c '<entry '` is a regex line count, not an XML
validator. If `dc69264:baf.lift` had 1702 `<entry ` text
matches AND a structural XML defect somewhere (unclosed tag,
embedded null byte, bad encoding sequence, anything ET
refuses), `git show` prints the raw bytes (succeeds — git
doesn't validate) but `ET.fromstring` raises `ParseError`.
The pre-0.35.2 `_parse` caught `ParseError` and **silently
returned an empty LIFT doc with no signal back to the
caller** — so the merger's `ours_entries` came back empty,
every guid hit the delete-modify branch (1699 dropped via
`continue   # they didn't change it; we deleted`, 1 emitted
as a theirs-annotated entry).

That fits the evidence exactly without invoking races or
staging mysteries. The pre-0.35.2 silent-ParseError-masking
was already addressed by 0.35.2's `_parse` returning
`(root, error_msg)` — the merger now refuses to commit when
ours/theirs fails to parse. The output-side guard in 0.35.3
is a complementary defense: catches the symptom whatever the
proximate cause, including ones we haven't thought of.

**Forensic trace added.** `three_way_merge` now logs
`[merge-trace] path=... base=N ours=M theirs=K
ours_err='...'` at the start of every invocation. Next
time anything looks weird, the logs themselves answer "did
`_parse` mask an error, or did the merger genuinely see N
entries?" without needing forensic git archaeology.

**Output-side `_looks_catastrophic_output` guard.** Refuses
to commit a merge whose entry count is < 1/4 of the smaller
healthy input. Skips small projects (base < 50 entries; the
ratio doesn't generalize at small scale) and skips when an
input was itself tiny relative to base (input-side guard had
jurisdiction; don't double-attribute). When triggered: keeps
the larger input intact verbatim, emits a single
`catastrophic-merge-output` Conflict carrying the full count
diagnostic. Defense-in-depth: catches the symptom regardless
of which proximate cause produced the algorithmic loss inside
the merger.

For the actual `baf` numbers (1, 1702, 1700, 1700): the guard
fires unambiguously. Even if a future bug produces some other
algorithmic loss, as long as the output is dramatically
smaller than the inputs, the guard catches it.

**Why this isn't redundant with the input guard.** The input
guard (`_looks_truncated`) checks the SHAPE of the inputs —
useful when one input arrived obviously truncated. The output
guard checks the SHAPE of the result — useful when both
inputs looked healthy at parse time but the algorithm lost
data internally. They're independent layers. The bug repro
above is the canonical case where input-side guard CANNOT
fire (both 1700-ish) but output-side guard MUST fire (1).

**`MIN_SERVER_VERSION` lifted 0.35.1 → 0.35.3 (hard).** The
proximate cause for the 0.35.1 collapse is undetermined and
could recur; forcing the floor ensures every peer paired with
a daemon has the output guard. Standard discipline matching
prior hard floors (0.34.0 sync, 0.34.1 reorder, 0.35.1
input-truncation).

**Tests** (`tests/test_lift_merge.py`):

- `test_full_sides_one_entry_differs_keeps_all_entries`:
  100-entry base/ours/theirs where only one entry differs.
  Output must contain 100 entries (with a field-level
  conflict on the differing one). Locks in the v3 recursive
  merge's correctness for this case, and via the output
  guard ensures even a regression in the recursive merge
  doesn't slip past.
- `test_catastrophic_output_guard_fires_directly`: direct
  unit tests of `_looks_catastrophic_output` covering: the
  bug numbers (trip), healthy output (skip), 50%-delete
  (skip), small project (skip), already-tiny-input (skip,
  input guard's territory).

**Wire-compat.** Additive: existing conflict kinds and
result shape unchanged. New `catastrophic-merge-output`
Conflict kind shows up only if the guard fires. Existing
peers see this as a generic CONFLICTS result; new peers can
distinguish via `Conflict.kind`.

### azt_collabd 0.35.2 — LIFT merge: recursive field-level conflict resolution + parse-error guard

The v1/v2 merge produced "two whole entries with synthetic
``-theirs`` guid suffix" on every modify-modify conflict — correct
but unresolvable in practice. A 1700-line entry conflict where the
only divergence is a `<text>` byte is invisible to the user; they
won't sit and diff two thousand lines to find the one that
differs. Field reports confirmed: nobody resolves these.

**v3 recursive merge.** Conflicts now express at the **narrowest
LIFT-multi level** that contains the divergence. A same-lang
``<text>`` conflict produces two same-lang ``<form>`` siblings each
carrying its own text and a single ``<annotation
name="azt-lift-conflict" value="ours|theirs"/>`` marker — one
``<entry>``, one ``<lexical-unit>``, two ``<form>``s. A
``<pronunciation>`` conflict duplicates at pronunciation level
(entry-level otherwise stays single). A gloss-text conflict
duplicates at the ``<gloss>`` level inside a single sense. Only
when a conflict genuinely can't be narrowed (entry-attribute
differences with no element-children divergence) does the
whole-entry duplication fallback kick in — with the synthetic
guid suffix kept for that rare case.

Implementation: ``_merge_pair`` + ``_walk_children`` recursive
helpers, plus a ``_MULTI`` policy table mapping
``(parent_tag, child_tag)`` → schema multiplicity. Unknown pairs
default to multi (safer to over-allow than under-allow). The
entry-level ``<annotation name="azt-lift-conflict" value="conflict">``
marker now carries a ``<trait name="azt-lift-conflict-fields"
value="...">`` listing slash-delimited paths from the entry root
to each conflict site (e.g.,
``lexical-unit/form[lang=en],sense[id=A]/gloss[lang=en]``) — peer-
side resolvers can jump to the conflicting sub-elements without
re-walking the merged tree.

**Parse-error guard.** ``_parse`` no longer masks
``ET.ParseError`` silently. When ``ours`` or ``theirs`` fails to
parse (mid-write truncation that breaks XML, etc.), the merge
refuses entirely — keeps the side that parsed cleanly, surfaces
a ``parse-error`` Conflict in the result. Pre-0.35.2 the silent
mask + the merge body's "absent from ours = ours deleted" rule
combined to produce catastrophically destructive merges when
the input was structurally invalid. Detection is now at the
input layer where the data still tells us what's wrong.

**Empty-side guard, small-project case.** The 0.35.1 truncation
guard only triggered on ≥50-entry projects (the ratio threshold
needs absolute size to avoid false-positives on legitimate small
edits). The empty-side case — ours has 0 entries while base has
any and theirs has any — now triggers regardless of project
size. Catches a 5-entry project where one peer's write got
``ftruncate(0)``'d mid-flight. False-positive only for users
who *intentionally* clear every entry, which doesn't happen in
this suite's peer flows.

**Wire-compat.** Same shape: emits LIFT bytes, peers read them
as normal LIFT. Old peers reading the v3 output see the
duplicated forms/glosses as normal LIFT content (forms ARE
schema-multi inside multitext containers; same-lang siblings are
schema-valid even if semantically "one per writing system"
conventionally) — no crashes, just a peer that doesn't recognise
the new annotation pattern. The ``conflict-fields`` trait value
changed from flat tag names to slash-delimited paths; peers
parsing it should treat the value as opaque text or a
comma-separated path list. No ``MIN_SERVER_VERSION`` bump —
peers don't actively consume the conflict format yet.

**Tests.** ``tests/test_lift_merge.py`` adds coverage for the
same-lang text case, pronunciation case, parse-error case,
empty-side small-project case, one-sided-change-no-conflict
clean path, and the entry-level marker's path-list trait.

### azt_collab_client 0.35.2 — peers may write image bytes through the provider (gate removed)

`MediaHandle(path_or_uri, kind='image').open_write()` no longer
raises `PermissionError`. 0.18.0 through 0.35.1 raised under an
"images are read-only from peers; the daemon owns image
additions" rule, which on inspection turned out to be an
**unsubstantiated policy** — every mention of it (`lift_io.py:149-170`,
`CLAUDE.md`'s cross-package-access section, the 0.18.0 CHANGELOG
entry) asserted the rule but none cited a driving concern.
The recorder team filed a NOTES_TO_DAEMON entry (2026-05-12)
showing that the rule made the entire in-app image-selection
feature silently no-op on URI projects, with four call sites
gated off (`_download_and_set`, `_copy_and_set`,
`_save_remote_image`, and the workers under it).

**Decision:** symmetry with audio. The daemon's provider
already supports image writes (`_resolve_path`'s
`_ALLOWED_MEDIA_DIRS = ('audio', 'images')` whitelist
auto-mkdirs the parent on first write). The two-write pattern
(image bytes through `MediaHandle`, illustration ref through
`LiftHandle`) is the same shape audio uses today and has
worked correctly in the field. Binary-conflict resolution on
basename collisions falls through to `repo._merge_diverged`'s
existing `non-lift-modify-modify` branch (merging-side wins
on disk, both versions remain in git history) — same handling
audio's `.wav` files get.

**Wire-compat:** purely client-side change. Daemon-side
provider already supports image writes; no daemon code changed.
Older peers paired with 0.35.2+ daemons keep their old
`PermissionError` gate (they bundle the old client) so no
behavior change there. Newer peers paired with older daemons:
the daemon's provider has always allowed image writes through
`_resolve_path`, so this works wire-side, but pre-0.35.1
daemons have the merge-truncation gap — the existing 0.35.1
`MIN_SERVER_VERSION` hard floor blocks that pairing anyway. No
floor bump for 0.35.2.

**Peer call-site cleanup** (recorder, viewer, future peers):
drop the `is_uri: return` gates that were routing around the
removed PermissionError. Use the same `MediaHandle` shape as
audio. No new endpoint, no wrapper, no recorder-side
infrastructure work beyond removing the gates.

### azt_collabd 0.35.1 / azt_collab_client 0.35.1 — LIFT merge: truncation guard + field-level conflict annotations; MIN_SERVER_VERSION lifted hard to 0.35.1

Field-reported 2026-05-12 (NOTES_TO_DAEMON.md, closed): a peer's
post-merge LIFT shrank from ~1700 entries to 1, leaving only the
single conflicting entry annotated `azt-lift-conflict="theirs"`.
The reporter hypothesized a sibling bug to the closed reorder-by-guid
issue ("union computed wrong"); the current code actually walks
`union(ours, theirs)` correctly, so that hypothesis didn't fit. The
real shape: `ours` arrived at the merge with a near-empty entry
list while `base` and `theirs` had the full template. Every base
entry absent from `ours` and unchanged in `theirs` then took the
"they didn't change it; we deleted it" branch — correctly, given
the inputs — producing the 1-entry destructive merge. The merge
algorithm wasn't lying; the **inputs** were corrupted upstream
(peer-side write race, partial commit, or sandbox sync hiccup
between recorder and daemon — not narrowed in this session).

**Defensive guard (`_looks_truncated`).** When all three sides
have non-trivial entry counts AND one side's count is less than
1/50 of the other AND the larger side has ≥50 entries, refuse
the destructive merge. Keep the larger side intact (unchanged
bytes; whatever was in the merge commit before this fix would
have been destructive, so we bias toward preserving data), and
return a single `Conflict(kind='truncation-suspected', fields=[…])`
in the result. Upstream callers see `S.CONFLICTS` and surface
the diagnostic; nothing destructive lands in git. Thresholds
are intentionally conservative — legitimate large-scale
deletions still go through (you can delete up to 98% of a
project in one commit without tripping the guard).

**Field-level conflict info.** Per-entry conflicts (modify-modify,
add-add) now annotate the `<annotation name="azt-lift-conflict">`
element with a `<trait name="azt-lift-conflict-fields" value="…">`
sub-element listing the LIFT child-element keys that actually
diverged — `lexical-unit`, `citation`, `field[type=SILCAWL]`,
`sense[id=…]`, `pronunciation`, etc. The `Conflict` dataclass
gains a `fields: list[str]` parallel field, surfaced via
`to_dict()` for any peer-side merge UI. Lets a recorder
(re-recording audio for a sense) ignore conflicts that don't
touch `pronunciation`; lets a viewer (or future merge resolver)
focus the user on the specific sub-elements that need attention
instead of asking them to diff the whole entry by eye.

Modify-delete / delete-modify conflicts don't carry field info —
the conflict there is "entry exists vs doesn't," not
sub-element divergence.

**Wire-compat:** additive. Older peers see the new trait as
unknown LIFT content (which their LIFT readers tolerate by
design — annotations are extensible) and the new
`truncation-suspected` Conflict kind as just another conflict
they surface generically.

**`MIN_SERVER_VERSION` raised 0.35.0 → 0.35.1 (hard).** Per the
reporter's ask #5: pre-0.35.1 daemons have no truncation guard,
so a peer paired with one can still hit the destructive merge.
The floor bump prevents that pairing — sync refuses with
`server_too_old` until the user updates the server APK. Same
discipline as the 0.34.1 reorder fix.

### azt_collabd 0.35.0 / azt_collab_client 0.35.0 — surface broken GitHub refresh-token state with a deadline-aware toast; codify auto/user sync contract

Field-observed in this session's first sync trace: the daemon's
``get_valid_github_token`` had been silently swallowing
``incorrect_client_credentials`` from the OAuth refresh endpoint
("Return the old token — it might still work"). That's a humane
fallback in the short term, but it converts an 8-hour countdown
into a silent cliff: once the existing access token expires, every
authenticated git op starts failing with no user-visible warning
that the user needs to re-auth.

**Daemon side.** ``azt_collabd/store.py``:
``get_valid_github_token`` now records ``refresh_broken=True`` +
the error string + the check timestamp on refresh failure, and
clears the flag on a subsequent successful refresh.
``set_github_tokens`` (called by the device-flow completion path)
also clears the flag — fresh tokens supersede any prior
refresh-failure state. ``get_status`` exposes
``github.refresh_broken`` and ``github.access_token_expires_at``
(unix timestamp = ``token_time + 8h``) so peers can read the
state via the existing credentials-status RPC without polling a
new endpoint. New helper ``github_refresh_state()`` returns the
same fields for daemon-internal use.

**New status code:** ``S.AUTH_REFRESH_STALE`` (mirrored in
``azt_collab_client/status.py``). Carries
``params['expires_at']``. Appended to every sync result —
``_h_project_sync`` and ``scheduler._run_sync`` both call a
shared ``server._annotate_with_auth_health(res)`` after running
the sync, so the status piggybacks on whatever the underlying op
returned (typically ``PUSHED + AUTH_REFRESH_STALE`` during the
access-token's last hour of life).

**Client side.** ``azt_collab_client/translate.py`` adds a
handler for ``S.AUTH_REFRESH_STALE`` that renders
"GitHub session needs re-authentication — current access
expires {deadline}. Open GitHub Connect and tap Re-authenticate."
``_format_deadline`` converts ``expires_at`` to a relative
phrase ("in 47 minutes", "in 3 hours", "now (already expired)")
so the user reads how much runway they have without dragging
timezone / locale plumbing into a one-shot string. The
"refresh-broken" state is also visible to peers via
``get_credentials_status() → github.refresh_broken`` for
peers that want a startup banner.

**Peer contract.** Documented in
``azt_collab_client/CLAUDE.md`` § "Peer contract: routing on
sync results" — auto-sync silences this code (per the existing
auto/user contract; we don't disrupt mid-flow); user-initiated
sync surfaces ``translate_status(status)`` as a toast. No
routing — the toast text already names GitHub Connect /
Re-authenticate as the next step. The state clears when the
user completes a fresh device flow.

**Wire compatibility.** Purely additive at the wire layer:
older peers paired with a 0.35.0 daemon see the new code as
an unknown status (verbose-but-non-fatal translate fallback);
older daemons paired with a 0.35.0 peer never emit the code,
so the peer never branches on it.

**``MIN_SERVER_VERSION`` raised 0.34.1 → 0.35.0** anyway, as a
*soft* requirement (no wire incompatibility to enforce). The
real reason: the peer contract changes in CLAUDE.md (auto-sync
silencing config-class codes, user-initiated routing /
toasting, deadline-aware ``AUTH_REFRESH_STALE`` handling) need
peer rebuilds to take effect. Bumping the floor forces every
peer paired with a 0.35.0+ daemon to rebuild against the
0.35.0 client, where the contract is documented and the
``AUTH_REFRESH_STALE`` translation is wired. Without the bump,
peers can keep running their pre-0.35.0 client and silently
disrupt project flows on auto-sync — the exact symptom that
surfaced as the "selected B got A" picker complaint earlier
in the 0.34.x development cycle.

### azt_collabd 0.34.1 / azt_collab_client 0.34.1 — LIFT merge preserves document order, MIN_SERVER_VERSION lifted to 0.34.1

Field-reported by the recorder team (NOTES_TO_DAEMON.md, 2026-05-11):
the very first real merge on any project rewrites the LIFT file
into guid-alphabetical order, irreversibly destroying the project's
semantic document order (template-driven SILCAWL order for new
projects; whatever the contributor established otherwise). The
change is committed and pushed before any peer can observe it, and
ElementTree round-trips preserve whatever order they parse, so all
subsequent edits cement the scrambled order. Repro confirmed
against `kent-rasmussen/sw-US-x-kent`: the merge commit `29d1266`
puts the entry whose guid sorts first (`002b6d2c-…` → SILCAWL
1572) at the top of the file, with every entry following in strict
guid order.

**Root cause.** `azt_collabd/lift_merge.py:three_way_merge`
walked `sorted(all_guids)` and appended to `merged_root` in that
order. Deterministic, yes — but the wrong determinism.

**Fix.** Walk `ours` in document order, then theirs-only entries
in theirs's document order. Anchoring on `ours` is the
conventional "the merging side keeps the order it was already
working against" pick, and it makes merge commits diffable: only
actually-changed entries move, instead of the whole 1700-entry
file appearing to be rewritten. Base-only guids (deleted on both
sides) are naturally excluded — they were a `continue` no-op in
the old loop body anyway. Same body, new traversal.

**MIN_SERVER_VERSION raised 0.34.0 → 0.34.1.** Pre-0.34.1 daemons
will commit and push a scrambled file on the next merge, with no
peer-visible warning before the damage hits git history. Hard
gate is preferable to silent fallback. Peers paired with a 0.34.0
daemon get the standard `server_too_old` bootstrap prompt.

**Repair for already-scrambled projects is deliberately manual**,
not automated, and unlikely ever to be. The natural order is
application-meaningful (SILCAWL row for template-derived projects;
headword for free-form lexica; sometimes a contributor's
deliberate manual sequence). A unilateral "re-sort everyone's
LIFT" utility can't know which of those applies, and silently
re-ordering a contributor's intentional sequence is the same
class of damage as the original bug. Project owners who want to
restore a known template order on a scrambled project do it by
hand, as one explicit commit, with explicit understanding of
what they're choosing to lose.

### azt_collabd 0.34.0 / azt_collab_client 0.34.0 — sync correctness: three load-bearing fixes, MIN_SERVER_VERSION lifted to 0.34.0

Two-device sync between Android peers was silently broken across the
entire 0.33.x line: after the first race between two phones pushing,
the daemon entered a state where every subsequent sync attempt
acted on a phantom remote, produced malformed merge commits, lost
the same race three times, and finally surfaced `PUSH_FAILED` or
(worse) a misleading `REPO_NOT_AUTHORIZED` against an unrelated
GitHub install. Three independent bugs were stacked; fixing one only
exposed the next. They're shipped together as a single minor bump.

`azt_collab_client.MIN_SERVER_VERSION` is raised from `0.31.0` to
`0.34.0`. A peer that gets through bootstrap will refuse to talk to
any older daemon — the user is forced to install/update the server
APK before sync re-engages. This is intentional: pre-0.34 daemons
*appear* to work and quietly corrupt local repo state by accumulating
malformed merge commits and never advancing
`refs/remotes/origin/<branch>`, so silent fallback is worse than the
hard gate.

**(1) `_merge_diverged` now produces real two-parent merge commits.**
The pre-0.34 code called `porcelain.commit(repo, merge_heads=[remote_sha], ...)`,
but dulwich 1.2.1's `porcelain.commit` doesn't expose `merge_heads`
as a public kwarg (it's an internal-only path used by `amend=True`).
The call raised `TypeError`, fell into a legacy graft-the-parent-
after-the-fact fallback (commit without merge_heads, then mutate
`commit.parents` post-hoc + re-add to the object store), which
silently produced a commit whose stored parents were `[local_sha]`
only. GitHub's `git-receive-pack` correctly rejected the push as
`DivergedBranches` because the "merge" commit didn't actually
contain `remote_sha` as an ancestor. Fix: drop down to
`repo.get_worktree().commit(merge_heads=[remote_sha])` —
the worktree-level API DOES accept `merge_heads` and sets
`c.parents = [old_head, *merge_heads]` atomically before writing
the object and advancing the ref. The graft fallback is removed;
the worktree API is in dulwich's public surface since 1.0.

**(2) HTTP 403 detection no longer false-positives on hex SHAs.**
The pre-0.34 code checked `'403' in str(exc)` to decide whether a
dulwich push exception was a real auth failure. dulwich's
`DivergedBranches.__str__` expands to `"(b'<current_sha>', b'<new_sha>')"`
— two 40-char hex SHAs. Random hex contains the trigraph `'403'`
~1 push in 250 by chance; the field trace had
`e41db428f68e9f7f6334`**`037`**`345d6450...`, which matched. The
false positive routed a diverged-branch failure through
`diagnose_403`, exiting the sync flow with a bogus
`REPO_NOT_AUTHORIZED` before the retry/merge could run. Fix:
`re.search(r'\b403\b', str(exc))` via a new `_is_http_403` helper
applied to all four call sites. Word boundaries don't fire inside
an all-word-char hex SHA but do fire in dulwich's
`"unexpected http resp 403 for <url>"` `GitProtocolError` message.

**(2b) `diagnose_403` now scopes by repo owner.**
When a real 403 *does* happen, `diagnose_403` was calling
`check_app_installed(token)` without `account_login`, so it grabbed
the first install in `/user/installations` whose `app_slug` matched.
A user who's a collaborator on five orgs that each installed
azt-collaboration got the first listed (`MattGyverLee` in the field
trace, install id 121228993, `selected` repos) instead of the
repo's own owner (`kent-rasmussen`, install 130605088, `all` repos),
and the follow-on `check_repo_in_installation` correctly answered
"no" — surfacing `REPO_NOT_AUTHORIZED` for a repo the user actually
has access to via their personal install. Fix: parse the repo
owner from `remote_url` first and pass it as `account_login` so
the install inspected is the one that should host the repo.

**(3) `porcelain.fetch` / `porcelain.pull` are called with the
remote NAME, not the URL.** Dulwich's `porcelain/__init__.py:fetch`
only runs `_import_remote_refs` (which writes
`refs/remotes/<name>/<branch>`) when `get_remote_repo` could resolve
the first positional arg back to a configured `[remote "<name>"]`
section. Passing a URL always misses (no section is named
`https://...`), so `remote_name = None` and the gate at line 4550
skips the ref import. The pack transferred successfully (HTTP 200
in logs) but the local tracking ref stayed frozen at whatever
`porcelain.clone` wrote at project-create time. Every subsequent
sync read a stale `remote_sha` from `refs/remotes/origin/<branch>`
and acted on a phantom state of the world. Field trace: actual
remote tip moved to `76201a5d…` ~25 minutes before the user opened
the recorder, but the daemon kept reading
`new_remote=42535766…` (the clone-time SHA) on every retry fetch,
merged against the phantom, and lost the push race three times in
a row. Fix: pass `'origin'` to every `porcelain.fetch` / `pull`
call site in `azt_collabd/repo.py`. Dulwich resolves `'origin'` via
`[remote "origin"]` (which `_init_repo_locked` /
`_clone_repo_locked` always populate), uses the `username` /
`password` kwargs we still pass explicitly, and — critically — runs
`_import_remote_refs` so the tracking ref advances on each fetch.
Push paths are unchanged: they already advance the tracking ref
manually via `repo.refs[remote_ref] = local_sha` after a successful
push (the line landed earlier for the `(+N)`-counter regression).

**Observability.** The retry-path fetch + merge in
`_sync_repo_locked` was wrapped in a bare `except Exception: pass`
— failures inside `_merge_diverged` were swallowed. Added
`[sync-trace]` lines for the retry fetch SHA, the retry merge SHA,
and any exception so future divergence loops are diagnosable from
logcat alone. (Reading the field trace was load-bearing for
isolating bugs 2 and 3.)

**(4) Auto-update download URL now matches the actually-published
asset name — and tolerates peer literals that drift from it.** The
recorder's bootstrap was wired with
`peer_asset_filename='azt_recorder.apk'` (Python-pkg underscore
form), but the published GitHub asset for `kent-rasmussen/azt-recorder
v1.39.0` is `aztrecorder.apk` (Android-package-segment form, no
underscore — matches `buildozer.spec → package.name`). The
`releases/latest/download/<wrong-name>` redirect 404'd; users got
"Download failed: HTTP Error 404" on tapping Update. The convention
itself is consistent (published name = `package.name`); only two
call sites typed the wrong form.

Two-layer fix in the client so peer-side typos can't break this
again:

- `default_asset_filename()` helper in `azt_collab_client.ui.update`
  derives ``<activity.getPackageName().rsplit('.', 1)[-1]>.apk`` from
  the running peer at runtime. `asset_filename` /
  `peer_asset_filename` in `check_for_update` /
  `install_apk_from_url` / `bootstrap` / `share_running_apk` are
  now keyword-optional and default to that helper. The recorder's
  call sites drop their explicit literals; new peers don't need
  to pass the name at all.
- **Resilient fallback** for peers that DO pass an explicit name
  (forks, older peer code rebuilt against the new client without
  having dropped the literal): the asset-lookup paths in
  `check_for_update` and `install_apk_from_url` now retry with
  the runtime-derived name when the explicit name isn't found in
  the release. `install_apk_from_url` further sources the download
  URL from the release JSON's `browser_download_url` rather than
  the caller-baked `releases/latest/download/<name>` URL, so a
  wrong peer-baked literal can't poison the actual download.
  Logged via `[update] explicit asset … not in release; falling
  back to derived …` so the drift is visible in logcat.
- Forks publishing under a non-default scheme still pass
  `asset_filename=` and get exactly that — fallback only fires
  when the explicit name returns no match.

### azt_collab_client 0.33.7 — docs/ cleanup: prune shipped plans, organise residual work
- **``docs/daemon_boot_plan.md``** rewritten as status-first.
  Phase A and Phase B2 marked SHIPPED with measured outcomes;
  Phase B1 + Phase C trimmed to "not shipped / not worth
  shipping unless …" notes with the trigger conditions
  spelled out. Cost-model speculation replaced with measured
  numbers from R500-class slow tablet (2026-05-09 harness
  run).
- **``docs/github_connect_ux_audit.md``** —
  recommended-implementation-order list at the bottom
  refreshed: items #1–#7 are done/declined (audit-trail
  strikethroughs preserved per the doc's own rule); items
  #8–#13 re-prioritised in current order. No content removed.
- **``docs/p4a_hook_picker_intent.md``** reduced to a redirect
  stub. The PICK_PROJECT intent-filter injection it described
  shipped in v0.28.x and is now in
  ``p4a_hook.py:_inject_pick_project``.
- **``docs/STATUS.md``** added. One-page index of "what
  shipped recently" + "what's open, prioritised" across all
  the docs in this directory. Reference docs
  (``research_notes_2026-05.md``, ``test_plan.md``,
  ``CLIENT_INTEGRATION.md``) listed but not duplicated.

### azt_collab_client 0.33.6 — measurement-driven decisions documented (Q2 + Q3 answered)
- **Q2 (doze) ANSWERED.** Measured on R500 tablet
  post-Phase-B2: doze runs (peer wait 49-68ms, daemon boot
  600-770ms) statistically indistinguishable from baseline
  (45-66ms / 593-1131ms). The Android-15 issue was the
  freezer, not doze proper. Phase B2's
  ``BIND_ABOVE_CLIENT`` is sufficient; no
  foreground-service-with-type variant needed.
- **Q3 (prewarm) ANSWERED.** Daemon Python boot ~600ms
  steady-state, ~1.1s first-cold-of-session. Prewarm
  overlap window ~1.9s. With prewarm, daemon boot fits
  entirely inside Kivy init; peer wait is **~50–60ms**.
  Without prewarm, peer wait would be the full
  ~600–1000ms.
- ``CLIENT_INTEGRATION.md`` § 3 reframed: prewarm in
  ``App.build()`` is now **required** for every peer (was
  "optional, measure first"). Cost of always calling it is
  essentially zero on devices where it doesn't help, and
  it's a 10× UX improvement on slow tablets.
- ``docs/daemon_boot_plan.md`` Q2 + Q3 marked answered with
  the measured numbers; remaining content kept for context.

### azt_collabd 0.33.0 + azt_collab_client 0.33.5 — bindService alone bootstraps Python (Service.onCreate self-delivers onStartCommand)
- **0.33.4 wasn't enough.** The connector's peer-side
  ``startService`` ALSO threw
  ``BackgroundServiceStartNotAllowedException``: cold-start
  ``App.build()`` fires before the peer's UID has been
  promoted to foreground (logcat shows
  ``UidRecord{...CEM bg:+50ms}``). Android 12+ blocks even
  cross-package starts from that state.
- **Real fix: server APK side.**
  ``AZTServiceProviderhost.onCreate`` now overrides Service
  lifecycle to self-deliver ``onStartCommand`` with
  ``getDefaultIntent(this, "")``. PythonService's normal start
  path runs on every Service creation, including from
  ``bindService`` with ``BIND_AUTO_CREATE``. So peers can
  start the daemon with ``bindService`` alone — which
  Android allows from background contexts. No more
  cross-package ``startService`` needed at all.
- **Connector simplified.**
  ``AZTServiceConnector.ensureBound`` no longer constructs
  Intent extras or calls ``startService``. Just one
  ``bindService`` with ``BIND_AUTO_CREATE | BIND_ABOVE_CLIENT``.
  All the Python-startup logic lives in the server APK's
  Service ``onCreate`` override.
- **Both APKs need rebuild + reinstall.** Server APK gets the
  new ``onCreate``; peer gets the simplified connector. Server
  APK bumped to 0.33.0 (lock-step with this change set).
- **Backward compat.** Legacy callers that still call
  ``startService`` (e.g., ``AZTCollabProvider.onCreate``'s
  fallback start, foreground caller paths) hit
  ``PythonService.onStartCommand`` which short-circuits when
  ``mService != null`` — Python only starts once regardless of
  start-path mix.

### azt_collab_client 0.33.4 — connector also startService (Android 12+ background-start fix)
- **Smoking gun on R500.** Logcat showed
  ``W ActivityManager: Background start not allowed: service
  Intent { cmp=...AZTServiceProviderhost }`` followed by
  ``E AZTCollabProvider:
  BackgroundServiceStartNotAllowedException`` thrown from
  ``AZTServiceProviderhost.start`` invoked from
  ``AZTCollabProvider.onCreate``. Android 12+ blocks
  ``startService`` from background contexts; the server APK's
  ``:provider`` lazy-spawn is a background context, so the
  Provider's ``onCreate`` self-start has been silently failing
  on every cold call. Python never started → no
  ``[boot-trace-daemon]`` lines, every peer compat probe got
  ``daemon_not_ready``, every B2 test was running against a
  daemon that had never finished init.
- **Fix.** ``AZTServiceConnector.ensureBound`` now does a two-
  step startup from the peer's *foreground* context (which
  Android allows): (1) ``startService`` with the full
  PythonService Intent extras (``serviceEntrypoint=service.py``
  etc., mirrored from
  ``AZTServiceProviderhost.getDefaultIntent`` on the server APK
  side; ``createPackageContext`` resolves the server APK's
  ``filesDir`` so ``androidPrivate`` / ``pythonHome`` /
  ``pythonPath`` aim at the right tree); (2) ``bindService``
  for OOM priority + freezer mitigation as before. The
  Provider's onCreate self-start stays in place as a no-op
  fallback for non-peer callers but is no longer load-bearing.
- **Why ``Provider.onCreate``'s start fails but the connector's
  start succeeds.** The Provider's runs in the server APK's
  own background process; the connector's runs in the peer's
  foreground process. Android 12+ allows the latter.

### azt_collab_client 0.33.3 — prewarm now binds on the main thread (worker JNI classloader scope fix)
- **Symptom.** R500 logcat showed
  ``[android_cp] AZTServiceConnector.ensureBound failed:
  ClassNotFoundException`` from the prewarm worker even after
  the connector ``.java`` was confirmed compiled into the APK
  (verified by ``unzip -p classes.dex | grep AZTServiceConnector``).
  The bind eventually succeeded — but only later, from the main
  thread via some other ``rpc.call`` path. So prewarm wasn't
  actually doing its B2 job: by the time the bind landed, the
  daemon's ``:provider`` process had already started cold-spawn
  with no priority hint.
- **Cause.** ``threading.Thread`` workers on Android attach to
  the JVM with the system bootclassloader, not the app
  classloader; pyjnius's ``autoclass`` calls from those workers
  can't find app-defined classes like our connector.
- **Fix.** ``prewarm()`` now does the autoclass + ``ensureBound``
  call synchronously on the caller's thread (typically the
  peer's ``App.build()`` main-thread context) BEFORE spawning
  the worker that does ``check_server_compat``. The bind is
  active at the earliest possible cold-start moment instead of
  racing the daemon's lazy-spawn. Worker still calls
  ``check_server_compat`` so the daemon-warmup retry loop has
  something to do; its (still failing) autoclass in
  ``discover()`` is a logged no-op since the bind is already
  in place.

### azt_collab_client 0.33.2 — point peer's android.add_src at canonical path, not the symlink
- 0.33.1 said ``android.add_src = android/src/main/java`` (via
  the peer's ``android/`` symlink). User report: still
  ``ClassNotFoundException`` on rebuild. Diagnosis: buildozer's
  ``android.add_src`` does not reliably follow symlinks; the
  copy/merge step lands an empty tree (or a broken symlink) in
  the dist's ``src/main/java/``.
- ``CLIENT_INTEGRATION.md`` § 2 now mirrors the server APK's
  approach: ``android.add_src = ../azt-collab/android/src/main/java``
  — pointing at the canonical filesystem path directly,
  bypassing the symlink. Brute-force fallback (copy the file
  into the peer repo) documented but flagged as brittle.

### azt_collab_client 0.33.1 — document peer's android.add_src requirement for B2
- 0.33.0 framing said "no peer code change required" — true for
  Python source, but peers DO need a one-line ``buildozer.spec``
  addition (``android.add_src = android/src/main/java``) for the
  new Java connector to compile into their APK. Otherwise:
  ``[android_cp] AZTServiceConnector.ensureBound failed:
  ClassNotFoundException`` on every cold start, bind never
  happens, freezer mitigation degrades to pre-B2.
- ``CLIENT_INTEGRATION.md`` § 2 now lists the line as required;
  § 3's "Automatic since 0.33.0" subsection points at the spec
  line when troubleshooting the ClassNotFoundException symptom.
- Server APK already had the equivalent line
  (``android.add_src = ../android/src/main/java`` in
  ``server_apk/buildozer.spec.tmpl``); peers historically didn't
  need one because the suite's Java tree was server-internal.
  B2 makes it shared.

### azt_collab_client 0.33.0 — Phase B2: peer holds bindService for OOM priority
- **Symptom this fixes.** R500-class Android-15 tablets showed
  endless ``daemon_not_ready`` 503s on cold start: peers
  triggered ``:provider`` lazy-spawn fine (Java provider
  responded), but Python's ``install_callbacks()`` never
  completed because the OS's app freezer suspended the cached
  ``:provider`` process mid-init. User-visible "AZT
  Collaboration not responding" + the manual "Open AZT
  Collaboration" workaround that papered over it.
- **Fix.** New peer-side Java connector
  ``android/src/main/java/org/atoznback/aztcollab/
  AZTServiceConnector.java`` issues ``bindService`` against
  the server APK's existing ``AZTServiceProviderhost`` with
  ``BIND_AUTO_CREATE | BIND_ABOVE_CLIENT``. Inheriting the
  peer's foreground priority defeats the freezer; the
  ``:provider`` process stays alive and Python finishes init.
  Bonus: ``:provider`` stays warm across the peer's session,
  so 2nd / 3rd / Nth RPCs in the same session don't re-pay
  daemon-cold-start.
- **Wire-up.** ``azt_collab_client/transports/android_cp.py``'s
  ``discover()`` calls ``Connector.ensureBound(activity)`` after
  the canonical ping succeeds. Idempotent; the connector is a
  static singleton. Async — we don't wait for
  ``onServiceConnected``; the existing compat-probe retry loop
  handles the bind-vs-Python-init race naturally.
- **No peer code change required.** Every peer that imports
  the client gets the new behaviour by virtue of bumping the
  bundled client. ``CLIENT_INTEGRATION.md`` § 3 documents the
  automatic behaviour + the diagnostic surface
  (``AZTServiceConnector.isBound()`` and ``dumpsys activity
  processes`` priority bucket).
- **No server APK change required.** ``AZTServiceProviderhost.
  onBind`` was already returning a stub ``Binder`` and tracking
  ``sBoundCount`` from the original sticky-bound design;
  peers were just never binding. Server APK can stay at 0.32.1.
- **Plan-doc** (``docs/daemon_boot_plan.md``) updated to mark
  B2 shipped + record the verification commands.

### azt_collab_client 0.32.2 — document prewarm + boot-trace harness in CLIENT_INTEGRATION.md
- New § 3 sub-section "Optional: pre-warm in ``App.build()``"
  documenting the ``prewarm()`` hook, its tradeoff, and the
  sentinel / env-var toggles for measurement runs. Peers
  considering cold-start tuning find the integration shape
  here rather than having to read ``bootstrap.py`` source.
- New § 13 sub-section "Boot-trace instrumentation" listing
  the peer + daemon phase labels and warning maintainers not
  to filter ``[boot-trace-*]`` lines out of their logcat
  pipelines.
- New § 13 sub-section "Cold-start measurement harness"
  pointing at ``tests/integration/measure_boot.sh`` +
  ``tests/integration/README.md`` and giving a default
  threshold (`peer wait` > 5 s on the slow-tablet target) for
  when to wire ``prewarm()`` before tagging a peer release.
- Also: ``measure_boot.sh`` switched from ``monkey`` to
  ``am start -W`` for launching the peer (with monkey as a
  fallback). monkey reports nonzero exits in cases that are
  actually fine — its exit code reflects internal event
  counts, not just dispatch success — and the script's
  ``set -e`` was killing the run after iteration 1.
  ``am force-stop`` and ``adb logcat -c`` now also tolerate
  nonzero exits.

### azt_collabd 0.32.1 + azt_collab_client 0.32.1 — boot-trace instrumentation + prewarm hook + measurement harness
- **Daemon-side ``_boot_trace``** in ``server_apk/service.py``
  emits ``[boot-trace-daemon] phase=<label> t=<elapsed>`` at
  every cost-center: ``module_loaded``, ``main_entered``,
  ``before_import_azt_collabd`` /
  ``after_import_azt_collabd``, ``configured``,
  ``before_install_callbacks`` / ``after_install_callbacks``,
  ``before_reconcile`` / ``after_reconcile``,
  ``entering_idle_loop``. Cheap; safe to leave on (≈ 10 lines
  per cold start).
- **Peer-side ``_boot_trace``** in
  ``azt_collab_client/ui/bootstrap.py`` mirrors with
  ``[boot-trace-peer] phase=<label>``: ``bootstrap_called``,
  ``compat_probe attempt=N``, ``compat_ok``,
  ``bootstrap_done``, plus prewarm phases.
- **New ``azt_collab_client.ui.bootstrap.prewarm()`` hook**:
  peers call it from ``App.build()`` to fire a single
  ``check_server_compat`` on a background thread, overlapping
  daemon lazy-spawn with Kivy initialisation. Idempotent;
  no-op on non-Android. Toggleable for measurement via
  ``$AZT_HOME/_no_prewarm`` sentinel or ``AZT_BOOT_PREWARM=0``
  env var so the harness can compare scenarios on the same
  APK without rebuilding.
- **Measurement harness** at
  ``tests/integration/measure_boot.sh`` drives a real device
  through ``baseline``, ``doze``, ``prewarm``, and
  ``doze+prewarm`` scenarios, capturing logcat boot-trace
  lines and producing per-iteration summaries via
  ``tests/integration/parse_boot_traces.py``. Doze is forced
  via ``dumpsys deviceidle force-idle``; prewarm toggling via
  the sentinel file (peer must be debuggable for ``run-as``).
  README at ``tests/integration/README.md`` documents
  prerequisites + scenario semantics.
- **Plan-doc updated** (``docs/daemon_boot_plan.md``):
  open-questions Q2 (doze) and Q3 (prewarm) now have explicit
  measurement plans pointing at the harness; Q1 (loopback
  ``kind``) remains as a deferred Phase A loose end.

### azt_collab_client 0.32.0 — daemon-warmup Phase A: adaptive backoff, diagnostic surface, fail-fast on null bundle
- **Adaptive backoff** in ``bootstrap._check_server``'s warmup
  retry loop. Replaces the fixed 2s interval with a schedule
  that ramps short → long: 0.2s, 0.4s, 0.8s, 1.6s, then plateaus
  at 2.0s. Fast devices that have a daemon ready by attempt 2
  now land in <1s instead of paying 2s+; slow devices keep the
  same ~60s total budget.
- **Diagnostic surface in the connecting popup.** New detail
  line under the "Connecting to AZT Collaboration service…"
  header shows ``Attempt N of 30  ·  Xs elapsed  ·  <kind>``
  where ``<kind>`` is the transport's coarse failure category
  (``daemon_not_ready`` while Python boots, ``null_bundle`` on
  signature-grant denial, etc.). Updates each retry. The
  unresponsive popup also surfaces last-error kind, total wait,
  and ``PackageManager``-reported server APK versionName, so the
  user / maintainer-email loop has actionable detail without
  needing adb access.
- **Fail-fast on ``null_bundle``.** Previously every
  ``ServerUnavailable`` was retried for the full 60s budget,
  including ``ContentResolver.call`` returning ``null`` — which
  is structurally unrecoverable (signature mismatch, provider
  authority missing). After 3 consecutive ``null_bundle``
  responses (≈0.6s on the new schedule) we jump to the
  unresponsive popup so the user can act on the real problem.
  ``daemon_not_ready`` and any other progress-bearing kind reset
  the streak — those still get the full warmup.
- **``ServerUnavailable.kind``** added to
  ``azt_collab_client.transports``. Recognised values:
  ``daemon_not_ready``, ``null_bundle``,
  ``server_apk_not_installed``, ``http_5xx``, ``transport_error``,
  ``http`` (loopback), ``''`` (unspecified). Existing call sites
  that ``except ServerUnavailable`` keep working; new sites can
  read ``ex.kind`` for fail-fast vs keep-retrying decisions.
  ``check_server_compat`` threads it into the result dict
  (``compat['kind']``).
- **Phase B + C planned** in ``docs/daemon_boot_plan.md``:
  provider-state in 503 body, ``bindService`` for OOM priority,
  optional daemon-side lazy imports if the new diagnostics show
  ``import azt_collabd`` is the dominant cost on slow tablets.

### azt_collab_client 0.31.5 — reframe smooth-UI section as a principle, not a recipe
- Per maintainer ask: § "Smooth UI across reloads" in
  ``CLIENT_INTEGRATION.md`` was framed as a
  recorder-specific recipe. Rewritten to lead with the
  **principle** (peers across the suite, whatever their
  model layer): same context; visible changes evident
  (including real upstream deletions, which propagate
  normally — LIFT workflows rarely delete but the principle
  doesn't paper over it); no other navigation; **suspend
  client-side filters that would hide the current view**
  (the failure mode is e.g. a "don't show past data" toggle
  excluding an entry the user is mid-edit because the data
  clock advanced — drop the filter for this view rather
  than swap the entry out). Recorder-flavoured snippet
  retained as one concrete realisation, not the contract.

### azt_collabd 0.31.2 + azt_collab_client 0.31.4 — sync fast-forward writes working tree; clone URL prefilled
- **Bug.** User report: "collaboration between clients on a
  project on two different phones is not smooth — each phone
  tracks its own changes but is unaware of others, even
  apparently when the user clicks on sync."
- **Cause.** ``_sync_repo_locked``'s fast-forward branch
  updated ``repo.refs[branch_ref] = remote_sha`` but never
  materialised the new tree to the working directory. Phone B
  fetched Phone A's commits, fast-forwarded the branch ref,
  but the LIFT file on disk stayed at Phone B's pre-sync
  bytes. Peers reading via ``LiftHandle`` got stale content
  and the UI looked unchanged. The diverged-merge branch was
  fine (``_merge_diverged`` already writes blobs to the
  working tree); only the fast-forward branch was the
  silently-broken case.
- **Fix.** New helper
  ``azt_collabd.repo._apply_tree_to_workdir(repo, project_dir,
  old_sha, new_sha)`` walks the diff between the two trees,
  writes added/modified blobs to the working tree, removes
  files that are gone in the new tree, and resets the index
  via ``repo.reset_index(new_tree)`` (with a ``_stage_all``
  fallback for older dulwich). Called from the fast-forward
  branch after the ref update. Diff-driven so unrelated
  untracked files (audio recordings the user just made and
  hasn't committed yet) aren't disturbed.
- **Peer-side principle documented** (peer follow-up, not in
  this bump). When the on-disk bytes change underneath a
  peer (``S.PULLED`` after sync, future ``MERGED_REMOTE``,
  re-clone, etc.), the user's view refreshes *in place*:
  same screen / entry / scroll position, fresh content,
  nothing else moves. If the entry the user is viewing was
  deleted upstream, keep the in-memory copy visible with a
  non-blocking notice rather than yanking them to a blank
  state. Sync is a refresh, not a navigation event. Spelled
  out as a principle (not a recipe) in § "Smooth UI across
  reloads" of ``CLIENT_INTEGRATION.md`` so each peer
  implements it through whatever model layer it has.
- **Clone-URL popup pre-fill.** ``clone_url_popup``'s URL
  field is now pre-populated with ``https://github.com/`` so
  phone-keyboard users can paste / type just ``owner/repo``
  instead of the full URL. Cursor lands at the end on open
  via ``Clock.schedule_once``. Submit-time guards: refuse
  empty / prefix-only input; if the user pasted a full URL
  *after* the prefix without first overwriting (so the field
  reads ``https://github.com/https://github.com/owner/repo``),
  take the rightmost protocol marker as the real URL start.

### azt_collab_client 0.31.3 — document grant_collaborator in CLIENT_INTEGRATION.md
- New § 10 "Granting collaborator access" in
  ``azt_collab_client/CLIENT_INTEGRATION.md``: covers the peer
  integration pattern (per-project settings only, never global),
  the project-disambiguation guarantee (peers pass langcode, the
  daemon resolves the repo — peers must NOT pre-resolve URLs),
  the full Result-status code list, translation pointer, and
  v1 scope (GitHub-only, invite-only, default ``push``).
- ``grant_collaborator_popup`` added to the "What the suite does
  *for* you" reference list at the bottom of the contract.
- Recovery / Testing sections renumbered 11 / 12.

### azt_collabd 0.31.1 + azt_collab_client 0.31.2 — grant-collaborator endpoint + popup
- **New endpoint** ``POST /v1/projects/<lang>/collaborators`` —
  invites a GitHub user as a collaborator on the repo backing
  ``langcode``. Looks the repo up via the project's
  ``remote_url`` so peers only have to pass a langcode (project
  disambiguation guaranteed server-side; no chance of peer-side
  URL handling targeting the wrong repo). Body:
  ``{username, level='push'}``.
- **Refactored** ``auth.add_collaborator`` to return ``'invited'``
  / ``'already'`` and raise on real errors (was: silent print +
  swallowed). The internal caller in ``repo._publish_repo``
  already wraps in ``try/except`` so its fire-and-forget
  semantics are preserved.
- **Status codes added** in both ``azt_collabd/status.py`` and
  ``azt_collab_client/status.py``:
  ``COLLABORATOR_INVITED``, ``COLLABORATOR_ALREADY``,
  ``COLLABORATOR_INVITE_FAILED``, ``INVALID_USERNAME``,
  ``NOT_GITHUB_REMOTE``. Plus translations in
  ``azt_collab_client/translate.py``.
- **Client wrapper** ``azt_collab_client.grant_collaborator(
  langcode, username, level='push')`` returns a ``Result``;
  re-exported from ``__all__``.
- **Reusable popup** ``azt_collab_client.ui.grant_collaborator_popup(
  langcode, on_done=None, font_name=...)``. Opens a popup that
  displays the project's langcode + remote URL prominently
  (project disambiguation is the load-bearing UX guarantee
  here), takes a username, calls ``grant_collaborator``, and
  surfaces translated outcomes. Auto-dismisses 2 s after
  success / "already a collaborator"; stays up on failures so
  the user can retry.
- **Peer integration** is per-peer (recorder / viewer / future):
  add a button to the project-context settings surface that
  calls ``grant_collaborator_popup(langcode=<current>)``. The
  button belongs in *project* settings, not global settings —
  the operation is meaningless without a specific project.
- **v1 scope.** GitHub-only (GitLab has different invite
  semantics; can be added by extending
  ``_parse_github_owner_repo`` and the dispatch). Invite-only
  (no list-existing or revoke yet, but the popup screen leaves
  room for either if you want them later).

### azt_collab_client 0.31.1 — Android 15 process-freezer workaround
- **Symptom.** On a budget Android 15 tablet (R500_V_US),
  cold-start peers showed "Connecting to AZT Collaboration
  service…" for the full 60 s daemon-warm-up budget, then
  fell through to "AZT Collaboration not responding."
  Verified: with the server APK launcher activity in the
  foreground, the same peer reaches the daemon in 5–10 s.
- **Cause.** Android 15's app freezer keeps the server APK's
  ``:provider`` process frozen even after a peer's
  ``ContentResolver.call`` triggers lazy-spawn — Python
  callbacks never finish registering inside the warm-up
  budget. Yesterday-vs-today framing was inconclusive; this
  is plausibly always-broken on certain ROMs and only
  surfaced today.
- **Workaround.** ``install_server_apk_popup`` gains an
  ``on_open_app`` parameter; when set it adds an "Open AZT
  Collaboration" button. ``_prompt_server_unresponsive``
  wires this to a callback that fires
  ``PackageManager.getLaunchIntentForPackage`` for
  ``org.atoznback.aztcollab``, then re-enters ``_check_server``
  on a 2 s delay with a fresh retry budget + a re-shown
  connecting popup. The launcher activity foregrounding
  un-freezes the package's process group, so when the user
  switches back to the peer, the next compat probe lands.
  Cheaper recovery than reinstalling the server APK.
- **Real fix later.** Peers should ``bindService`` to
  ``AZTServiceProviderhost`` while foregrounded so OOM
  priority prevents freezer interference in the first place.
  That's a Java change; deferred.

### azt_collabd 0.31.0 + azt_collab_client 0.31.0 — minor bump for pre-distribution test
- Lock-step minor bump on both packages, with both floors moved
  to 0.31.0 (``MIN_CLIENT_VERSION`` on the daemon,
  ``MIN_SERVER_VERSION`` on the client). Folds in everything
  since 0.30.0: server-APK boot-on-lazy-spawn, sticky-bound
  ``:provider`` host, self-update auto-exit, GitHub-connect UX
  rewrite (state-machine + suspended-install detection +
  Verify-setup re-test affordance), pre-install APK validation
  (parse + signature + asset.digest cache freshness), bootstrap
  flow with mandatory vs voluntary distinction, server→peer
  language mirror, blocked-popup mailto link + Check-again,
  same-tag re-upload detection via ``last_seen_digest``, install
  poll lifecycle, suite-wide CONFIRMED-vs-CONNECTED gating, and
  the various translation additions and Kivy touch-routing fixes.
  Anything older talking to a 0.31 peer (or vice versa) gets a
  clean ``client_too_old`` / ``server_too_old`` and is routed
  through the bootstrap update flow.

### azt_collab_client 0.30.47 — mandatory-mode probe always forces prompt
- **Regression from 0.30.46.** Recording ``gh_digest`` on
  Update-tap fixed the voluntary loop, but introduced a hole on
  the mandatory path: if the install failed (user cancelled at
  the Android installer screen, signature mismatch, etc.), the
  next ``_probe`` saw ``last_seen == gh_digest`` →
  ``digest_changed=False`` → ``needs_update=False`` →
  ``_show_no_newer_release`` fired even though GitHub actually
  has the right bytes (we just failed to install them).
- **Fix.** Replaced ``legacy_mandatory_force`` (only handled
  the first-run unknown-baseline case) with ``mandatory_force``
  (always forces the prompt in mandatory mode when the release
  feed returned something to download). Digest comparison is for
  "is there something new to offer?" — irrelevant when the daemon
  has already declared the client too old.
- **Clarification.** The 0.30.46 changelog framing "stuck at
  old version with no prompt" was sloppy. Voluntary install fail
  is benign — the client was already compatible (otherwise the
  daemon would have returned ``client_too_old`` and the mandatory
  path would have been used). The user runs a working older
  build and can retry via the peer's in-app Update button.
  Mandatory path is enforced terminal — no chance of leaking the
  user into project loading without a working daemon.

### azt_collab_client 0.30.46 — record gh_digest as last_seen on Update tap
- **Bug.** User report: "I click update on a voluntary screen,
  and it doesn't download… because it seems to be running from
  cache, this window keeps coming up since the digest is still
  different from gh." Trace:
  ``last_seen='3687f3…' gh_digest='efb3ae…'
  version_newer=False digest_changed=True``.
  Cache check correctly skipped the download (bytes already at
  ``efb3ae…``), install fired, but ``_record_last_seen_digest``
  was never called outside the first-run baseline branch — so
  ``last_seen`` stayed at ``3687f3…`` forever. Next bootstrap
  saw ``digest_changed=True`` and re-prompted.
- **Fix.** ``_prompt_self_update`` now receives ``gh_digest``
  from ``_probe`` and records it as the new ``last_seen`` baseline
  on Update-button tap. Recording at tap time (vs. install-complete)
  is the practical compromise: same-tag re-uploads don't flip
  versionName, and self-installs kill our process during install,
  so we have no reliable in-process completion signal. If the
  install ultimately fails the user is stuck at the old version
  with no further prompt — recoverable via the in-settings
  Update button or a manual reinstall.

### azt_collabd 0.30.45 + azt_collab_client 0.30.45 — re-bump for mandatory-update test pass
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.45** to force the
  ``client_too_old`` path on any peer bundling an older client
  (continuing test pass for the digest-change decline fix).

### azt_collabd 0.30.44 + azt_collab_client 0.30.44 — decline-memory ignores same-tag re-uploads
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.44** to force the
  ``client_too_old`` path on any peer bundling an older client
  (test pass for the digest-change decline fix).
- **Bug.** User report: "Check again doesn't currently show a new
  apk online, despite a different sha256." Trace confirmed the
  probe was correctly setting ``digest_changed=True``
  (``last_seen='3687f35493c3'`` ≠ ``gh_digest='cceb3fc2ba05'``),
  yet no prompt appeared. Cause: the decline-memory check in
  ``_peer_update_with_confirm._probe`` only compared the version
  tag — a previous "Not now" tap against ``1.37.24`` masked
  every subsequent re-upload at the same tag, regardless of
  digest. The original comment ("a re-upload of a declined
  version still plausibly came with the user's prior decline
  intact") turned out to be wrong: a re-upload IS a different
  build the user has not been asked about.
- **Fix.** Decline check now skips when ``digest_changed`` is
  True, treating the new bytes as a fresh release. Also
  belt-and-braces gates the check on ``not mandatory`` so the
  ``client_too_old`` path can't be silenced by a stray decline
  entry from an earlier voluntary cycle.

### azt_collabd 0.30.43 + azt_collab_client 0.30.43 — mirror daemon UI language to peer + clearer mandatory-update wording
- **Language sync server → peer.** User report: switching the
  server APK's settings UI to French didn't translate any
  bootstrap-side popups in the peer (recorder / viewer).
  Cause: ``$AZT_HOME/config.json::ui.language`` is the
  persistence path, but on Android ``$AZT_HOME`` is per-process
  private (server APK has its own filesDir, each peer has its
  own), so file-system writes from the server's settings UI
  never reached peer disk. Fix:
  - New daemon endpoint ``GET /v1/config/ui_language`` returns
    the server-side persisted language (handler
    ``_h_get_ui_language`` in ``azt_collabd/server.py``).
  - New client wrapper ``get_server_ui_language()`` in
    ``azt_collab_client/__init__.py``.
  - ``bootstrap._sync_ui_language_with_daemon()`` runs at
    ``_check_server`` entry (immediately after a successful
    ``check_server_compat``) and applies the daemon's language
    via ``i18n.set_language`` so all peer-side popups + status
    text track. Best-effort: silent on RPC failure, peer
    keeps its local pref.
- **Mandatory-update wording.** User report: viewer at 0.8.2
  saw "AZT Viewer 0.8.2 is required" — confusing when 0.8.2
  is the current version (same-tag re-upload case where
  digest changed). Now branches in
  ``_prompt_self_update``:
  - ``latest_version != peer_version`` (genuinely newer
    release): "{name} {peer_v} is too old for the AZT
    Collaboration service. Tap Update to install {name}
    {latest}, or Quit to close this app." Both versions named.
  - ``latest_version == peer_version`` (same-tag re-upload —
    digest_changed=True triggered the prompt): "A new build
    of {name} {version} is available. The current build is
    too old for the AZT Collaboration service. Tap Update to
    install the new build, or Quit to close this app." No
    longer phrases the same version as both "what you have"
    and "what's required".
- Email-link-in-the-update-popup ask is deferred —
  ``install_server_apk_popup``'s body Label doesn't have
  ``markup=True`` yet, and the change isn't a one-liner.
  Tracked separately.

### azt_collabd 0.30.42 + azt_collab_client 0.30.42 — bump MIN_CLIENT_VERSION to 0.30.42
- Final test-pass bump on the daemon (``MIN_CLIENT_VERSION =
  "0.30.42"``) to exercise the mandatory-self-update flow now
  that ``_prompt_self_update`` calls ``App.stop()`` on
  install completion. Server APK rebuild required; peer at
  0.30.41 trips ``client_too_old`` and gets the mandatory
  Update / Quit popup.
- Drop ``MIN_CLIENT_VERSION`` back to a real-world floor
  before any release that ships in the public update
  channel. Same goes for ``MIN_SERVER_VERSION`` on the client
  side.

### azt_collabd 0.30.41 + azt_collab_client 0.30.41 — close peer cleanly after a self-install
- User report: "When I clicked update, it downloaded and
  installed fine, but then I found myself back at the same
  popup. I closed and restarted fine, but users shouldn't have
  to do that." Android usually kills the running peer during a
  self-install, but not always — on some devices the peer
  survives, comes back to foreground after the system
  installer dismisses, and the popup is right where it was.
  No way to recover without a manual restart.
- Fix: ``_prompt_self_update`` now passes the peer's own
  package name as ``install_target_package`` (read from
  ``PythonActivity.mActivity.getPackageName()``) so
  ``install_apk_from_url`` runs the post-install poll loop
  on it, and ``on_install_complete`` calls
  ``App.get_running_app().stop()`` so the running peer exits
  the moment ``PackageManager`` reports the new versionName.
  Next user launch lands on the new APK; no manual restart.
- Safe both ways: if Android kills us during install (the
  common case), the poll thread dies with us — no leak. If
  Android doesn't kill us, the poll fires, we self-stop. The
  pre-0.30.41 comment about "polling our own package would
  block forever" was wrong: while the system installer is in
  foreground the Kivy Clock pauses, but it resumes when we
  come back, the poll detects the change, and we exit.

### azt_collabd 0.30.40 + azt_collab_client 0.30.40 — distinguish mandatory peer self-update from voluntary
- ``_prompt_self_update`` was using the same body + dismiss
  action for both the voluntary "newer version available" path
  and the mandatory ``client_too_old`` path. Declining a
  mandatory update via "Not now" silently fell through to
  ``on_done`` and dropped the user into the peer with a daemon
  that wouldn't talk to them.
- New ``mandatory`` parameter on ``_prompt_self_update``
  (forwarded from ``_check_self`` via
  ``_peer_update_with_confirm``):
  - **Voluntary** (``mandatory=False``, default): existing body
    "A newer version of this app ({version}) is available." —
    Update button + Not now (dismiss). Decline memory still
    applies so we don't re-prompt for the same version.
  - **Mandatory** (``mandatory=True``, the ``client_too_old`` +
    newer-version-exists path): body "{name} {version} is
    required to use the AZT Collaboration service. Tap Update
    to download and install it, or Quit to close this app." —
    Update button + Quit (action=``'quit'``, peer
    ``App.stop()``). No decline memory: a mandatory update
    can't usefully be remembered as "declined".
- Net effect: declining a mandatory update closes the app,
  matching the ``_show_release_too_old`` /
  ``_show_no_newer_release`` Quit semantics. Symmetric with
  ``_prompt_server_update``'s "Update" button + the existing
  install popup's Quit button on the server-side path.

### azt_collabd 0.30.39 + azt_collab_client 0.30.39 — bump MIN_CLIENT_VERSION to 0.30.39
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.39)
  so a peer at 0.30.38 paired with this server APK trips
  ``client_too_old``. Continues exercising the new
  digest-aware probe + version-anchor popup from 0.30.37/.38.
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.38 to actually trigger client_too_old.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.38 + azt_collab_client 0.30.38 — detect same-tag re-uploads via asset digest persistence
- Per maintainer ask: the version-tuple "any newer release"
  probe in ``_peer_update_with_confirm._probe`` couldn't see a
  re-uploaded asset on the same release tag. Common in dev
  iteration (push fix, keep tag), and unavoidable when a tag
  is hot-fixed in place. The new branch catches it via a
  digest-change check.
- ``_last_seen_digest(repo)`` and
  ``_record_last_seen_digest(repo, digest)`` persist the
  GitHub ``asset.digest`` per peer repo via the existing
  ``peer_pref`` / ``set_peer_pref`` store
  (``peer_prefs.last_seen_digests`` dict, keyed by
  ``owner/repo``).
- ``_probe`` now reads ``asset.digest`` from the latest
  release JSON, compares against the persisted last-seen, and
  treats EITHER a newer version tag OR a changed digest as
  "newer available". A trace line ``[bootstrap] _probe …
  version_newer=… digest_changed=…`` prints both signals so
  flaky cases are diagnosable from logcat.
- First-run baseline: if no digest is on file for ``repo``,
  the current GitHub digest gets recorded as the starting
  point so subsequent probes can detect change. Misses the
  perverse "re-uploaded between install and first launch"
  edge case but covers the dev-iteration use case cleanly.
- Storage scope: ``peer_prefs`` writes to whatever
  ``$AZT_HOME/config.json`` resolves to in the calling peer's
  process (peer-private on Android, shared on desktop), which
  is correct — each peer tracks its own repo independently.

### azt_collabd 0.30.37 + azt_collab_client 0.30.37 — fix client_too_old popup: version anchors + drop nonsensical pre-flight + Check-again tracing
- User report: "Recorder is too old" popup gave no version
  information either on screen or in the email body, and Check
  again seemed to do nothing.
- **Drop the pre-flight comparison in ``_check_self`` for the
  client_too_old path.** ``required_min`` from the daemon refers
  to the **client library** version (``azt_collab_client.
  __version__``), not the peer-app version. The recorder peer
  bumps its own version (1.34.0, …) independently of the client
  lib, so comparing recorder release tags to client-lib version
  numbers is meaningless — recorder 1.34.0 vs lib 0.30.36 has no
  defined order. The pre-flight either trivially passed or
  trivially failed depending on which way the major-version
  numbers happened to land. Replaced with the simpler "is there
  ANY newer peer release available" check that
  ``_peer_update_with_confirm`` already performs. We can't
  inspect a remote APK's bundled client-lib version without
  downloading it, so any-newer-version is the only honest signal.
- **Version anchors in the popup body and email.**
  ``_show_no_newer_release`` now takes ``required_client_lib``
  and ``bundled_client_lib`` and surfaces all four version
  values: peer name + peer version, bundled client lib (this
  build), and required client lib (from the daemon's compat
  handshake). Body reads e.g. "Recorder 1.34.0 is too old for
  the AZT Collaboration service. This build bundles client
  library 0.30.35; the service requires 0.30.36 or newer. No
  newer Recorder release is published yet." Email body lists
  the same anchors as labelled lines so the maintainer has the
  full mismatch in one read.
- **Check again tracing.** ``[bootstrap] Check again pressed —
  invalidating release cache + re-entering _check_server`` now
  prints when the button fires; ``_check_server`` is wrapped in
  try/except so any exception during the retry surfaces in
  logcat instead of silently dying in the worker thread. Lets
  us tell apart "Check again ran but result is the same"
  (visible re-render) from "Check again failed silently" (no
  trace lines after).

### azt_collabd 0.30.36 + azt_collab_client 0.30.36 — bump MIN_CLIENT_VERSION to 0.30.36
- ``MIN_CLIENT_VERSION`` raised to 0.30.36 (peer wandered to
  0.30.35, so we need to stay one ahead). Server APK rebuild
  required to pick up the new floor; peer at 0.30.35 trips
  ``client_too_old`` against this daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.35 + azt_collab_client 0.30.35 — bump MIN_CLIENT_VERSION to 0.30.35
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.35)
  so a peer at 0.30.34 paired with this server APK trips
  ``client_too_old``. Lets us exercise the new
  ``_show_no_newer_release`` popup (parity with
  ``_show_release_too_old``: Check again + mailto + Quit, no
  fall-through to ``on_done``).
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.34 so the test triggers cleanly.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.34 + azt_collab_client 0.30.34 — Update-needed popup parity + don't drop into client on dismiss
- ``_show_info`` (the "Update needed" single-button popup that
  fired on the ``client_too_old`` + no-newer-release branch) had
  two problems:
  1. UI didn't match ``_show_release_too_old``: only an OK
     button, no mailto link, no Check-again.
  2. After the user tapped OK, ``_check_self`` fell through to
     ``_on_done_and_release`` — host loaded a project, daemon
     refused subsequent RPCs, user stuck in a half-broken UI.
- Refactor: extracted the popup body into
  ``_show_update_blocked_popup(ctx, body_text, mailto_subject,
  mailto_body)``. Two callers — the existing
  ``_show_release_too_old`` and a new
  ``_show_no_newer_release`` — share the same Check-again /
  Quit / ``[ref=email]`` mailto-link UI vocabulary. The "Update
  needed" / OK / fall-through popup is gone.
- ``_check_self``'s on_no_update force_prompt branch now
  surfaces ``_show_no_newer_release`` and **does not** call
  ``_on_done_and_release``. Popup is terminal — Quit stops the
  app via ``App.stop()`` (no half-broken UI), Check again
  invalidates the release cache + re-runs ``_check_server``.

### azt_collabd 0.30.33 + azt_collab_client 0.30.33 — bump MIN_CLIENT_VERSION to 0.30.33
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.33)
  so any peer at 0.30.32 or earlier paired with a 0.30.33 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Continues exercising the symmetric self-update flow now that
  the bootstrap install-cache fix is in.
- Server APK rebuild required to pick up the new floor; peer
  can stay at 0.30.32 (or older) to actually trigger
  client_too_old when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.32 + azt_collab_client 0.30.32 — install_apk_from_url: validate cache against GitHub digest
- User report: bootstrap reused a cached 0.30.28 server APK
  instead of downloading the published 0.30.29, then Android
  rejected the install with "package appears to be invalid"
  (downgrade attempt). Root cause: ``install_apk_from_url``
  is the direct-URL path (called from
  ``install_server_apk_popup`` which bootstrap dispatches),
  with no GitHub-API cross-check. ``_has_fresh_download`` ran
  in sidecar mode — cached file matched its sidecar SHA, so
  reused — but sidecar mode can't tell "intact bytes" from
  "intact-but-stale-version bytes". ``check_for_update`` got
  the digest-mode fix in 0.30.22; ``install_apk_from_url``
  was still on the old path.
- Fix: new optional ``repo`` parameter on
  ``install_apk_from_url``. When supplied, the worker fetches
  the GitHub release JSON, finds the matching asset's
  ``digest`` (sha256:hex), and threads it through to
  ``_has_fresh_download`` as ``expected_sha256``. Stale
  caches with the right SHA-vs-sidecar but wrong SHA-vs-
  GitHub now fall through to a fresh download. Failure of
  the metadata fetch (network glitch) falls back to sidecar
  mode rather than blocking the install.
- ``install_server_apk_popup`` accepts and forwards the
  ``repo`` parameter; bootstrap's four call sites
  (_prompt_server_install / _prompt_server_update /
  _prompt_server_unresponsive / _prompt_self_update) all
  pass ``ctx.server_repo`` or ``ctx.peer_repo`` as
  appropriate.
- Net: the same digest-driven cache-freshness story that
  applies to settings-screen "Update this app" now also
  applies to bootstrap's "Install / Update AZT
  Collaboration" popup and to peer self-update from the
  bootstrap path.

### azt_collabd 0.30.31 + azt_collab_client 0.30.31 — log SHA check result in _has_fresh_download
- ``_has_fresh_download`` ran the cache integrity check
  (digest-mode against GitHub's asset digest, sidecar mode
  otherwise) but did so silently — user reported "no SHA log
  line, is this a regression?" against 0.30.28. Test was
  running, just invisible.
- Added a single ``[update] _has_fresh_download: ...`` print
  per call that names the mode (digest / sidecar), shows the
  truncated file SHA + expected SHA, and the boolean match
  result. Also prints early-exit reasons (file missing, hash
  failed, sidecar missing, sidecar empty, sidecar read
  error) so a False return tells you *why*. No behavior
  change.

### azt_collabd 0.30.30 + azt_collab_client 0.30.30 — bump MIN_CLIENT_VERSION to 0.30.30
- ``MIN_CLIENT_VERSION`` raised to 0.30.30 on the daemon so any
  peer running 0.30.29 or earlier paired with a 0.30.30 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Lets us exercise the symmetric ``_check_self`` /
  release-too-old / Check-again flow on the *peer* side
  (which previously we'd only proven for the server-too-old
  direction).
- Server APK rebuild required to pick up this floor change;
  peer can stay at 0.30.29 (or older) to actually trigger the
  client_too_old branch when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.29 + azt_collab_client 0.30.29 — bump MIN_SERVER_VERSION to 0.30.28
- ``MIN_SERVER_VERSION`` raised to 0.30.28 so the latest peer
  build keeps triggering ``server_too_old`` against any server
  APK at 0.30.27 or earlier (continuing the testing thread for
  the release-too-old / Check-again flow).
- Drop back to a real-world floor before any release that
  ships in the public update channel.

### azt_collabd 0.30.28 + azt_collab_client 0.30.28 — debug rebuild
- No code change; bump for a fresh peer APK to retest the
  Check-again cache invalidation on the device.

### azt_collabd 0.30.27 + azt_collab_client 0.30.27 — invalidate release cache on Check again
- 0.30.26's Check again button re-ran ``_check_server`` but the
  per-process release-JSON cache (5-minute TTL on
  ``_release_cache``) returned the stale "too old" entry, so
  the popup re-rendered immediately against the same data and
  the user saw no change. Closing/reopening the peer wiped the
  cache and worked.
- Fix: new ``invalidate_release_cache(repo=None)`` helper in
  ``azt_collab_client/ui/update.py``. Bootstrap's Check-again
  handler drops both ``ctx.server_repo`` and ``ctx.peer_repo``
  entries before re-running ``_check_server`` so the next
  probe re-fetches GitHub.

### azt_collabd 0.30.26 + azt_collab_client 0.30.26 — release-too-old popup: mailto link + Check again
- "Or build from source" was unhelpful for the SIL field-
  linguist user base — they're not going to. Replaced with
  "or [send the developer an Email]" rendered as a Kivy-
  markup ``[ref=email]`` link styled with underline + accent
  blue. Tapping it opens the user's MUA via ``mailto:`` with
  a pre-filled subject ("{name}: required version not yet
  released") and body containing the version mismatch info.
  No-MUA degradation is graceful — Android shows the standard
  "no app to handle this" toast.
- New ``azt_collab_client.MAINTAINER_EMAIL`` constant
  (``kent_rasmussen@sil.org``) so forks can override in their
  own client build (no env-var hook yet — change at the
  source line, rebuild). Matches the address in
  ``SECURITY.md``.
- Added a "Check again" button alongside Quit. Dismisses the
  popup and re-enters ``_check_server`` on a worker thread —
  same code path as bootstrap's compat probe, so a
  freshly-published release that meets the floor flows
  through to the install popup without the user having to
  restart the peer.

### azt_collabd 0.30.25 + azt_collab_client 0.30.25 — pre-flight release version vs required minimum + clearer body text
- User reported: bootstrap got server_too_old, popped the
  "Update AZT Collaboration?" dialog, the user tapped Update,
  and the latest published release was *also* too old to
  satisfy ``MIN_SERVER_VERSION``. Result: download succeeds,
  install succeeds, peer hits ``server_too_old`` again. Wasted
  bandwidth + a confused user.
- Fix in ``azt_collab_client/ui/bootstrap.py``:
  - ``_release_meets_minimum(repo, required_min)`` fetches
    GitHub's latest tag for the given release feed and
    compares to the required floor. Returns ``(ok, latest,
    error)``.
  - ``_show_release_too_old(...)`` is a new one-button popup
    that surfaces "{name} {required} or newer is required, but
    the latest available release is {latest}. Wait for an
    update or build from source." with a Quit affordance.
    Replaces the install popup when the upstream release feed
    can't satisfy the floor.
  - ``_prompt_server_update`` now takes ``min_required``,
    pre-flights the server APK release feed, and surfaces the
    "release too old" popup instead of opening the install
    popup if upstream can't help. Body text on the install
    popup itself now reads "{name} {required} or newer is
    required (you have {current})" — replaces the redundant
    "AZT Collaboration service (AZT Collaboration)" wording
    the user flagged.
  - Symmetric pre-flight on the ``client_too_old`` path:
    ``_check_self`` accepts ``required_min`` (forwarded from
    the daemon's compat response) and runs the same release-
    feed check before going into ``_peer_update_with_confirm``.
    A peer running ahead of GitHub's latest publish gets the
    same "wait for an update" popup instead of a useless
    download.
- Client-only rebuild needed to exercise this — bootstrap
  runs in the peer's process. Server APK can stay at whatever
  older version is on the device (that's what triggers
  ``server_too_old`` in the first place).

### azt_collabd 0.30.24 + azt_collab_client 0.30.24 — debug rebuild + MIN_SERVER_VERSION = 0.30.24 to exercise old-server flow
- ``MIN_SERVER_VERSION`` bumped to 0.30.24 deliberately, so a
  peer carrying this client paired with a server APK at 0.30.23
  or earlier hits ``check_server_compat()`` →
  ``server_too_old``. Lets us walk the bootstrap "Update AZT
  Collaboration?" prompt end-to-end without backdating any real
  wire-format change. **Drop back to a real-world floor before
  any release that ships in the public update channel.**
- No other code change; daemon and client are otherwise identical
  to 0.30.23.

### azt_collabd 0.30.23 + azt_collab_client 0.30.23 — debug rebuild
- No code change; bump for a fresh APK to retest the
  ``asset.digest``-driven cache-freshness check on the device.

### azt_collabd 0.30.22 + azt_collab_client 0.30.22 — verify cached APK against GitHub's authoritative ``asset.digest``
- 0.30.21 caught stale-version caches via versionName parsing.
  Per maintainer suggestion: GitHub's REST release-asset
  metadata exposes a SHA-256 ``digest`` field
  (``"digest": "sha256:<hex>"``, added 2025-06-03), so we can
  do an authoritative cache-freshness check without paying for
  a re-download.
- ``_has_fresh_download`` now takes an optional
  ``expected_sha256`` arg. When supplied (asset has a digest),
  it's the strong check — file SHA must equal the GitHub
  digest to reuse. When not (legacy assets pre-2025-06), it
  falls back to the existing sidecar self-consistency check.
- ``_worker`` parses ``asset.digest`` (strips the ``sha256:``
  prefix) and threads it through. Catches three failure modes
  in one place:
  - same-version-different-bytes (re-uploaded asset),
  - corrupted on-disk cache,
  - stale-version cache from a previous Update cycle.
  The versionName check from 0.30.21 is kept as a belt-and-
  braces layer for legacy assets where the digest is null and
  the SHA fallback can't tell same-bytes-different-version
  from a normal cache hit.

### azt_collabd 0.30.21 + azt_collab_client 0.30.21 — invalidate cached APK when versionName ≠ latest
- User repro: tapped Update on 0.30.19 → cache reused → install
  ran but version stayed at 0.30.19 (the cached APK was from
  the previous update cycle). ``_has_fresh_download`` only
  validates that the cached file's bytes match the sidecar's
  SHA — i.e. on-disk integrity. It does NOT check that the
  cached file is the version we're now trying to install.
- Fix: in ``check_for_update``'s ``_worker``, after the
  fresh-download check passes, ``_apk_parse_info`` reads the
  cached APK's ``versionName`` and compares it to ``latest``.
  Mismatch → remove the cached file + sidecar so the
  download branch runs and we fetch the right version.
- Diagnostic line ``[update] cache stale: cached_version=...
  != latest=...; discarding ...`` prints when the discard
  fires, so future repros are visible in logcat.
- Why not also fix this in ``install_apk_from_url``: that
  path doesn't know the "expected version" (URL is
  redirect-style and opaque); peers using it typically run
  install-once via bootstrap, so cache staleness is much less
  of a problem.

### azt_collabd 0.30.20 + azt_collab_client 0.30.20 — log parse_info + signature_matches result
- 0.30.18's pre-install validation didn't tell us which check
  outcome we got — user reports "same invalid response" against
  0.30.18 + 0.30.19. Did the parse pass? Did signature compare
  match? Returned None? We can't tell from logcat without an
  explicit print at each point.
- Added two trace lines:
  - ``[update] parse_info: ok pkg=... versionName=... path=...``
    (or ``parse_info: None path=...``) right after
    ``_apk_parse_info`` runs. Surfaces the APK's actual package
    name and version so we can spot manifest-level mismatches
    (e.g. wrong ``packageName``).
  - ``[update] signature_matches_installed: True/False/None``
    right after ``_signature_matches_installed`` runs. ``True``
    = match (install should succeed signature-wise),
    ``False`` = mismatch (we'd surface our error and abort),
    ``None`` = couldn't determine (off Android, app not
    installed, jnius unavailable, exception path).
- No behavior change.

### azt_collabd 0.30.19 + azt_collab_client 0.30.19 — debug rebuild
- No code change; bump for a fresh APK to retest 0.30.18's
  pre-install validation + suspended-messaging on the device.

### azt_collabd 0.30.18 + azt_collab_client 0.30.18 — pre-install validation + step-by-step suspended messaging
- **Pre-install APK validation in ``check_for_update`` and
  ``install_apk_from_url``.** Before firing the install Intent
  we now run two checks via ``PackageManager``:
  - ``getPackageArchiveInfo(dest, GET_SIGNATURES)`` to confirm
    the downloaded APK is parseable. A null result means the
    download was truncated / corrupted; the cached file +
    sidecar are removed and the user gets a "could not be
    parsed; try again to re-download" error rather than the
    bare Android "package appears to be invalid" complaint
    after dispatching the Intent.
  - ``signature_matches_installed(dest, package)`` —
    compares the APK's signing certificate against the
    currently-installed app's certificate. On mismatch we
    surface "Downloaded APK is signed with a different key
    than the installed app... Uninstall first, then tap
    Update again — or rebuild from source with the matching
    keystore." That replaces the cryptic "App not installed
    as package appears to be invalid" Android error and
    points the user at both fixes.
  - Helpers (``_apk_parse_info``,
    ``_signature_matches_installed``,
    ``_installed_version_name``,
    ``_android_package_manager``) live alongside the existing
    install-Intent code in ``update.py``. All three return
    ``None`` off Android / when pyjnius is unavailable so the
    desktop / non-Android paths short-circuit cleanly.
- **Diagnostic line** at the start of ``_install_on_ui``:
  ``[update] pre-install check: pkg=... installed_version=...
  running_version=... latest=...``. Tells us whether the
  device-installed version diverges from the running code's
  ``__version__``. They should match in normal use; diverging
  values are a hot-patch / dev workflow signal.
- **Suspended-state messaging.** Old "Resume it at {url}"
  was unhelpful — gave a URL but no idea what to do on the
  page. ``GitHubConnectScreen`` now stashes the
  ``installation_id`` from the Verify-setup probe and:
  - ``install_app`` opens
    ``settings/installations/<installation_id>`` (the
    install's configure page directly) instead of the
    generic install URL.
  - The accompanying message reads "Tap 'Install GitHub App'
    below to open the install's configure page on GitHub,
    then scroll to the bottom and tap 'Unsuspend'." —
    walks the user through the actual GitHub UI flow.
  - ``_render_message`` and ``_test_done`` both branch on
    ``_suspended_installation_id`` so the message survives
    re-renders (language change, screen re-entry).
- ``S.APP_SUSPENDED`` translation (used by sync 403 path,
  which doesn't have the in-screen "Install GitHub App
  below" affordance) updated to the same self-contained
  step-by-step shape: "GitHub App installation is suspended
  at {url}. Open it, scroll to the bottom, and tap
  'Unsuspend'."

### azt_collabd 0.30.17 + azt_collab_client 0.30.17 — restore Share icon
- 0.30.11's Share/Update simplification dropped the
  share-icon Image. Restoring per user preference: the
  ``SHARE_ICON`` KV macro and ``icon_path('share_dark')``
  format-arg are back, with the Image positioned as a
  left-overlay (``x: self.parent.x + dp(12)``) inside the
  half-width Share button. Text "Share" stays centered; no
  ``padding: [dp(52), 0]`` needed since a single-word label
  doesn't collide with the icon.

### azt_collabd 0.30.16 + azt_collab_client 0.30.16 — Android back button pops sub-screens to settings
- ``CollabUIApp`` (the standalone server APK settings host)
  didn't bind Android's hardware back button, so a back-press
  from GitHubConnectScreen / GitLabFormScreen fell through to
  ``App.stop`` and closed the app — losing the user's settings
  session mid-setup. Picker_app already had this hook;
  CollabUIApp didn't.
- Added ``CollabUIApp.on_start`` to bind
  ``Window.on_keyboard`` and ``_on_back_button`` to consume
  key 27 by popping ``sm.current = 'settings'`` from any
  sub-screen. Settings-screen back returns False to let Kivy
  / Android close the app the normal way.

### azt_collabd 0.30.15 + azt_collab_client 0.30.15 — swap primary button to "Verify setup" after install_app
- User report: at step 2, ``install_app`` opens the browser
  with a message that says "return here and tap Verify
  setup", but the primary button still reads "Install GitHub
  App" — there's no Verify setup button to tap. The user
  comes back from GitHub, reads the instruction, can't find
  the button it names, gets stuck.
- Fix: ``install_app()`` now flips ``gh_primary_btn``'s
  ``text`` to "Verify setup" and ``_action`` to ``verify``
  right after opening the browser, so the affordance the
  message promises actually exists. If the user returns
  without installing (cancelled, navigated away),
  ``test_github_credentials`` reports
  ``app_installed=False`` and ``_test_done`` re-runs
  ``on_pre_enter`` which puts the button back to "Install
  GitHub App" — they can retry without screen drift.
- Auto-polling for install completion was considered and
  rejected for v1: poll cadence vs. GitHub-API quota is a
  real trade-off, and the swap-and-tap workflow is bounded
  by user attention anyway. Easy to add later if needed.

### azt_collabd 0.30.14 + azt_collab_client 0.30.14 — log account.login on /user/installations + per-account install matching
- 0.30.13's trace showed three azt-collaboration installs in
  ``/user/installations`` while the user said they only ever
  saw one at ``github.com/settings/installations``. We don't
  yet know who owns the other two — could be orgs the user
  belongs to, could be stale state, could be something else
  GitHub is doing. ``check_app_installed`` now logs
  ``account.login`` alongside the existing fields so the next
  probe answers "whose installs are these?" directly.
- ``check_app_installed`` gained an optional
  ``account_login`` parameter that narrows the match to the
  installation whose ``account.login`` matches (case-
  insensitive). When omitted, the legacy "first match by
  app_slug" behavior is preserved.
- ``test_github_credentials`` now passes
  ``server_username`` (the user's own GitHub login) as
  ``account_login`` so Verify setup checks for the install
  on the user's own account, not "any install we can see."
  This fixes a real bug observed: user uninstalled their
  personal install but was still a member of orgs that had
  ``azt-collaboration`` installed; the unscoped match
  reported ``installed=True`` against an org install and
  the screen continued to show "Setup complete." With this
  change, Verify setup correctly returns
  ``installed=False`` once the personal install is gone,
  and the screen regresses to step 2.
- ``diagnose_403`` (sync 403 path) does NOT yet take the
  repo's owner into account; that's the next change once we
  see the per-account data and confirm the matching strategy
  is right. Currently still uses the legacy unscoped
  ``check_app_installed``, so this release narrows just the
  Verify-setup path.

### azt_collabd 0.30.13 + azt_collab_client 0.30.13 — log raw /user/installations to diagnose stuck suspended state
- 0.30.12 still showed "Setup Complete" against a suspended
  install on the user's device, even though both server APK
  and client are 0.30.12. So the suspended-detection code is
  running but either ``inst.suspended_at`` isn't being set on
  the install we're looking at, or the slug match isn't
  hitting the entry. Need data to disambiguate.
- ``check_app_installed`` now logs:
  - All ``(app_slug, id, suspended_at)`` tuples returned by
    ``/user/installations`` so we can see whether the user's
    install is in the list at all and what GitHub is reporting
    for ``suspended_at``.
  - The matched entry (if any) with its suspended_at and
    repository_selection.
  - The final ``result`` dict.
  - HTTPError / general Exception (was silently caught).
- ``_h_test_github`` now logs the test_github_credentials
  return value (valid / app_installed / app_suspended /
  installation_id) plus what it actually wrote to the store.
- Pure tracing — no behavior change. Build, retry Verify
  setup against the suspended install, and the next logcat
  will tell us whether the suspended detection is firing or
  whether GitHub is reporting the install as not-suspended
  for some reason.

### azt_collabd 0.30.12 + azt_collab_client 0.30.12 — fix suspended-state message overwrite race
- 0.30.11 set the suspended-message ``gh_message.text``
  immediately in ``_test_done``, but ``self.on_pre_enter()``
  on the line above schedules ``_refresh_state`` for the next
  frame, and ``_render_message`` there overwrites the field
  with the step-N default ("Setup complete..." / "Now install
  the GitHub App..."). User report: Verify setup against a
  suspended install kept showing "Setup Complete, connected
  as ...".
- Same defer-past-render dance the AuthError handler uses.
  Suspended message now goes through a second
  ``Clock.schedule_once`` so it lands after ``_refresh_state``
  completes.
- Note: this fix requires the server APK to ALSO be running
  0.30.11+ (or this 0.30.12). Older daemons' ``check_app_installed``
  matches solely on ``app_slug`` and reports
  ``installed=True`` for any installation, so a suspended
  install never gets the ``app_suspended=True`` flag and the
  client never enters the suspended-message branch. If you
  see "Setup Complete" after rebuilding the recorder but not
  the server APK, that's why — rebuild + reinstall the server
  APK and Verify setup again.

### azt_collabd 0.30.11 + azt_collab_client 0.30.11 — detect suspended GitHub App installs + simpler Share/Update buttons
- **Suspended-install detection.** User repro: paused the
  azt-collaboration App installation in their GitHub settings,
  the connect screen still showed "Setup complete (App
  installed)", and sync silently failed with codes
  ``['NOTHING_TO_COMMIT', 'REPO_NOT_AUTHORIZED']`` because the
  daemon's ``check_app_installed`` only matched on
  ``app_slug`` and reported ``installed=True`` for any
  installation including suspended ones. Fix:
  ``check_app_installed`` now reads ``inst.suspended_at`` —
  ``installed=True`` requires the install to be active, and a
  new ``suspended=True`` field is set when the App is on file
  but paused. ``installation_id`` is also returned in the
  suspended case so the UI can construct the resume URL
  (``settings/installations/<id>``) instead of the generic
  install page.
- **New status code ``S.APP_SUSPENDED``** plus translation:
  "GitHub App installation is suspended. Resume it at {url}."
  ``diagnose_403`` returns this for sync 403s when the install
  is suspended; ``test_github_credentials`` exposes
  ``app_suspended`` + ``installation_id`` alongside
  ``app_installed`` so the connect screen's Verify setup path
  surfaces a precise message and link instead of regressing
  silently to the step-2 "Install" prompt.
- **Connect screen ``_test_done`` handles suspended.** When
  Verify setup runs against a suspended install, the message
  becomes "GitHub App installation is suspended. Resume it at
  github.com/settings/installations/<id>." — the user can tap
  the URL or open it manually, resume on GitHub, and re-run
  Verify setup to confirm.
- **Wire format change.**
  ``POST /v1/credentials/github/test`` response now carries
  ``app_suspended`` and ``installation_id`` alongside the
  existing ``app_installed`` field. Older clients ignore the
  extras; newer clients against older daemons see ``False`` /
  ``None`` defaults (which is the correct
  "no-suspended-state-known" reading).
- **Settings: simpler Share / Update.** Two stacked
  full-width "Share this app" / "Update this app" buttons
  collapsed into one half-width row labelled just "Share" and
  "Update". Drops the share icon (one less asset to ship and
  less visual noise; the action is obvious from the label).
  ``SHARE_ICON`` KV macro and the ``share_icon=icon_path(...)``
  format-arg removed; ``icon_path`` import dropped.

### azt_collabd 0.30.10 + azt_collab_client 0.30.10 — keep "Verify setup" available after setup completes
- Once the user reached step 4 (everything verified),
  ``_render_primary`` was hiding ``gh_primary_btn`` entirely —
  forcing them to Re-authenticate (which means re-typing the
  8-field code) just to confirm the connection is still
  healthy. User asked for a non-destructive "test settings"
  affordance from the verified state.
- Fix: ``_render_primary`` now keeps the button visible at step
  4 with the same ``Verify setup`` label and ``verify`` action
  that step 3 uses. ``test()`` is idempotent (just hits
  ``api.github.com/user`` with the saved token); a successful
  re-test stays at step 4, a failure surfaces by regressing the
  screen state to step 2 / step 1 / "Token rejected" — a single
  tap that doubles as a diagnostic.
- Step-4 message updated to "Setup complete. Connected as
  {username}. Tap Verify setup any time to re-test." so users
  notice the affordance is intentional.

### azt_collabd 0.30.9 + azt_collab_client 0.30.9 — detach publish_row children to free the GitLab button
- User report: "GitLab button still doesn't respond until 10-12
  clicks" while the adjacent GitHub button works fine. Same root
  cause as the earlier ``gh_primary_btn`` issue: ``publish_row``
  is the next BoxLayout below ``gl_action_btn`` and stays at
  ``height=0`` for users with no project. BoxLayout's
  ``_do_layout`` still positions ``publish_btn`` (a RecBtn with
  ``on_press``) at its explicit ``dp(52)`` height under the
  collapsed parent, and Kivy's dispatch loop visits every child
  regardless. The combination intermittently swallows touches
  near gl_action_btn. (Why "intermittent" rather than "always":
  the touch points hover near the bottom edge of gl_action_btn /
  spacing area, where Kivy's hit-test math is sensitive to the
  exact tap coordinate.)
- Fix: ``SettingsScreen._refresh_publish_row`` now detaches
  ``publish_row``'s children when hiding the row (via
  ``_detach_publish_children`` / ``_reattach_publish_children``,
  matching the pattern used in ``GitHubConnectScreen`` for
  ``gh_manage_box`` / ``gh_device_flow_box``). A parent with no
  children cannot dispatch on_touch_down to anything, so the
  hidden publish_btn can no longer eat taps meant for the
  GitLab button. Idempotent on both sides.
- The detach is keyed off the same condition that hides the
  row, so users with an active publishable project (the
  "should-show-publish" case) keep the row's full functionality.

### azt_collabd 0.30.8 + azt_collab_client 0.30.8 — Connect-button gating + Disconnect inside settings + web-flow plan
- **Settings GitHub/GitLab buttons gated on ``confirmed``, not
  ``connected``.** User reported the canonical footgun: install
  failed midway, gh.connected stayed True (token was saved),
  refresh() flipped the button to "Disconnect GitHub", and
  the only tap available was the one that wiped the partial
  work. ``refresh()`` now reads ``gh.confirmed`` /
  ``gl.confirmed`` and renders:
  - Not verified → ``Connect to GitHub`` / ``Connect to GitLab``
    in green; tap navigates to the connect screen which
    auto-resumes from the user's current step (server state is
    the source of truth).
  - Verified → ``GitHub Settings`` / ``GitLab Settings`` in the
    neutral surface color; tap opens the same screen, now
    showing the manage view.
- **Disconnect moved inside each screen.** GitHubConnectScreen
  already had Disconnect in its manage box;
  ``GitLabFormScreen`` gained one in this release (visible via
  ``gl_manage_box`` only when a token is on file). Rationale: a
  fat-finger Disconnect from the main settings has a real cost —
  re-auth on GitHub means re-typing the 8-field code, re-auth
  on GitLab means re-pasting a PAT. Audit doc #6 + #7 updated
  to reflect this.
- **Removed** ``SettingsScreen.gh_action`` /
  ``connect_github`` / ``disconnect_github`` /
  ``disconnect_gitlab``. The KV buttons call ``app.go(...)``
  directly; the disconnect helpers live on each respective
  screen instead.
- **Web-flow migration plan** drafted at
  ``docs/web_flow_migration_plan.md``. Research finding: GitHub
  Apps' OAuth web flow accepts PKCE but still requires
  ``client_secret`` on the token exchange (per
  github.blog/changelog 2025-07-14 + community/discussions
  #15752), so a pure-PKCE mobile-safe flow is not legal. The
  plan documents (a) a Phase-1 ``tests/probe_pkce.py`` that
  validates this finding against the live API, (b) a web-flow
  architecture using embedded ``client_secret`` in the server
  APK only, with PKCE as defense-in-depth and device flow as
  the universal fallback, and (c) the open decision points
  (embed-secret tradeoff, fork story, sunset window for device
  flow). Not yet approved for implementation.
- **PKCE probe script.** ``tests/probe_pkce.py`` (intentionally
  not auto-collected by pytest — no ``test_`` prefix). Walks
  the user through up to three browser authorizations and
  validates the four cases laid out in the plan: PKCE param
  acceptance, PKCE-no-secret rejection, PKCE-with-secret
  success, secret-only-no-PKCE success. Exits non-zero on any
  deviation, so it doubles as a regression check if GitHub
  later changes its stance.

### azt_collabd 0.30.7 + azt_collab_client 0.30.7 — revert bogus URL prefill (audit doc #1 was based on a false premise)
- Audit doc #1 assumed GitHub's OAuth Device Flow returns
  ``verification_uri_complete`` (RFC 8628 §3.2) or at least
  honors ``https://github.com/login/device?user_code=ABCD-1234``
  to prefill the code field. After actually researching this:
  - GitHub's documented response is exactly
    ``device_code, user_code, verification_uri, expires_in,
    interval`` — ``verification_uri_complete`` is OPTIONAL per
    the spec and GitHub omits it. The canonical ``cli/oauth``
    Go reference impl and ``octokit/auth-oauth-device.js``
    both parse the field defensively for spec compliance but
    receive empty strings against github.com.
  - GitHub's ``/login/device`` page silently ignores the
    ``?user_code=...`` query parameter. No prefill happens.
  - A Jan-2024 GitHub change adds a "select Continue on an
    account" confirmation step in front of the code form
    unconditionally, even for single-account users. The user
    confirmed seeing this with one account.
- Reverted 0.30.5's URL-suffix construction. The fallback chain
  is now defensive only: use ``verification_uri_complete`` if a
  future GitHub change starts returning it, otherwise the bare
  ``verification_uri``. No more constructed query strings.
- ``docs/github_connect_ux_audit.md`` #1 updated with the
  research finding and links to the canonical references so the
  next person doesn't rediscover the false premise.
- The user-visible flow against the current GitHub: the user
  taps Begin → user_code displayed in our app + auto-copied to
  clipboard → bare ``/login/device`` URL opens in browser →
  GitHub's account-confirmation step → 8-field code form (no
  paste support either) → user types each digit → authorize.
  Polling worker picks up the resulting authorization within
  ~5s. Not a great UX, but it's the only path GitHub provides.

### azt_collabd 0.30.6 + azt_collab_client 0.30.6 — worker tracing + fix message overwrite
- Detach fix from 0.30.4 worked: ``gh_primary_btn`` now receives
  touches and ``primary_action: action='begin'`` fires correctly.
- New diagnostics in ``_worker``: trace device_flow_start,
  device_flow_poll completion, save_github_tokens success,
  app_installed probe result, _done firing, and AuthError /
  Exception paths. Intent: pin down why the screen doesn't
  advance to step 2 after the user authorizes — currently we
  can't tell if polling stalled, save failed silently, or _done
  ran but credentials_status didn't reflect.
- Bug fix: error handlers (AuthError / generic Exception) called
  ``self.on_pre_enter()`` (which schedules ``_refresh_state``)
  *then* set ``gh_message.text = 'Failed: ...'``. The deferred
  ``_refresh_state`` ran on the next frame and overwrote the
  message with step-1's "Tap Begin..." default. Now the
  Failed message is set via a second ``Clock.schedule_once``
  so it lands after ``_refresh_state`` and survives. Implication
  for the user: when polling actually times out, they'll
  finally see why instead of silently bouncing to step 1.

### azt_collabd 0.30.5 + azt_collab_client 0.30.5 — build the prefilled device-flow URL ourselves
- Audit doc #1 (the "Manual code-copy step in browser" win) was
  betting on GitHub returning ``verification_uri_complete`` in the
  device-flow response. Per RFC 8628 that field is OPTIONAL and
  GitHub elects to omit it for OAuth Device Flow — confirmed
  by the user landing on the bare code-entry page after Begin.
- Fix: when ``verification_uri_complete`` isn't in the response,
  build the prefilled URL ourselves by appending
  ``?user_code=<user_code>`` to the bare URL. GitHub's
  ``/login/device`` page reads the query parameter and prefills
  the code field, so the user still lands on "Authorize?"
  directly. If a future GitHub change starts returning
  ``verification_uri_complete`` we use it as-is and skip the
  suffix.

### azt_collabd 0.30.4 + azt_collab_client 0.30.4 — detach hidden box children so they can't intercept touches
- 0.30.3's diagnostics confirmed it: when the user taps inside
  ``gh_primary_btn``'s content y-range (Window y=1013-1070
  mapped to content y=305-362, well inside the button's
  pos=275 / top=405 range), the Window touch fires but
  ``gh_primary_btn``'s ``on_touch_down`` probe never does. So a
  sibling earlier in dispatch order is silently consuming the
  touch before the Begin button gets a chance — even though
  ``Widget.on_touch_down`` should have short-circuited via
  ``self.disabled and self.collide_point(...)`` on the hidden
  ``gh_manage_box`` / ``gh_device_flow_box``.
- Suspect: ``BoxLayout._do_layout`` keeps positioning children at
  their explicit heights even when the parent's ``height=0``, so
  Re-auth NavBtn (in the hidden manage box) lives at content
  y=85-205 with disabled=True. The disabled-eats-touch contract
  is supposed to handle this, but in this layout it didn't —
  some children were intercepting touches and others weren't,
  inconsistently. The mechanism's failure mode is opaque enough
  that fighting it with more flags (``disabled``, ``opacity``,
  ``height``) just shifts which configurations break.
- Fix: hide-by-detach. ``_hide_device_flow`` / ``_hide_manage``
  now call ``remove_widget`` on each child of the box; show
  re-adds them in original order from a per-box snapshot. A
  parent with no children cannot dispatch ``on_touch_down`` to
  anything, so there's no way for a hidden manage/device-flow
  child to intercept touches that should reach Begin /
  Install GitHub App / Verify setup. The snapshot stays
  strong-ref'd while detached so widgets don't GC.
- Idempotent: re-detach is a no-op if already detached;
  re-attach is a no-op if the snapshot is empty.

### azt_collabd 0.30.3 + azt_collab_client 0.30.3 — deeper Begin button diagnostics
- 0.30.2 told us the button is at ``pos=[50, 275]
  size=[980, 130] disabled=False opacity=1.0`` — sane — but
  ``state`` never flips on tap, so touch_down isn't reaching the
  button. Sibling NavBtns (Back, Create-account) in the same
  ScrollView work fine, so this isn't a global ScrollView /
  ButtonBehavior thing.
- Added two more probes:
  - ``btn.bind(on_touch_down=...)`` on the primary button so we
    log whether the button receives the dispatched event from
    its parent BoxLayout (with ``inside=collide_point(touch)``).
  - ``Window.bind(on_touch_down=...)`` so we log every raw touch
    Kivy receives, with the touch's reported position. Lets us
    correlate "where the user actually touched" against the
    button's pos and confirm whether the touch even arrives at
    the Window level.
- Expected next-run output on a tap:
  ``WINDOW touch_down: pos=(X, Y) inside_primary_btn=True/False``
  followed (or not) by ``gh_primary_btn on_touch_down: ...
  inside=...``. The combination tells us whether the touch
  arrives at all, whether it's at the right position, and
  whether the parent dispatches it to the button.

### azt_collabd 0.30.2 + azt_collab_client 0.30.2 — Begin button diagnostics
- 0.30.1's ``on_press`` switch did not help the Begin button:
  user reports ``_refresh_state`` still fires but neither
  ``primary_action`` nor any state log appears on tap, while the
  sibling Back / Create-account buttons (also inside the same
  ScrollView) work fine. So it isn't ScrollView vs. on_release —
  it's something specific to ``gh_primary_btn``.
- Added two diagnostics that will fire on the next attempt:
  - ``_render_primary`` logs the button's resolved ``pos`` /
    ``size`` / ``disabled`` / ``opacity`` after the render
    finishes. We need this to confirm the button is at the
    coordinates the user is tapping.
  - One-shot bind on the button's ``state`` property so every
    'normal' → 'down' transition surfaces in logcat. Button's
    state machine flips on touch_down regardless of whether the
    on_press / on_release events fire — so this lets us tell
    apart the two failure modes:
      * State changes but no ``primary_action`` log → event
        dispatch broken (binding lost?).
      * State never changes → touches aren't reaching the button
        (layout / hit-test problem; e.g. a hidden sibling box
        overlaps the touch zone).

### azt_collabd 0.30.1 + azt_collab_client 0.30.1 — switch settings/connect-screen action buttons to on_press
- User-reported: Begin (device-flow start) "still does nothing"
  even after the layout / id-resolution fixes in 0.29.1. The
  ``[github-connect] _refresh_state`` trace fires on screen
  entry, confirming the button is rendered with ``_action='begin'``
  and ``disabled=False``, but no ``primary_action`` log appears
  on tap.
- Diagnosis: classic Kivy ScrollView vs. Button issue. ScrollView
  records every touch_down for scroll-distance evaluation; if the
  user's finger jiggles even ~dp(20) during the press (easy on a
  touchscreen), ScrollView grabs the touch on touch_up and the
  child Button's state machine never fires ``on_release``. The
  user's previous complaint that the GitLab settings button
  "resists pressing in most cases. if I hit it randomly for
  awhile, it does eventually activate" is the same root cause —
  the rare clean tap was the one without enough movement.
- Fix: switch every action button inside a scrolled region from
  ``on_release`` to ``on_press`` so the dispatch fires on
  touch_down, before ScrollView decides whether to claim the
  touch. Affected sites: SettingsScreen Back / Share / Update /
  Publish / GitHub action / GitLab action / Refresh / Debug-503;
  GitHubConnectScreen primary / signup / Copy / Re-authenticate /
  Disconnect / Back; GitLabFormScreen Verify / Back; the dynamic
  language-selector buttons. Trade-off: actions can no longer be
  cancelled by sliding off the button before lifting the finger —
  fine for these recoverable flows (every button either
  navigates, opens the browser, or kicks off an RPC the daemon
  treats idempotently). Popups and modals are not affected;
  they're not inside a ScrollView.

### azt_collabd 0.30.0 + azt_collab_client 0.30.0 — boot Python on ContentProvider lazy-spawn; auto-exit on self-update
- **Server APK couldn't be reached after fresh install unless the
  user opened it manually.** ``AZTCollabProvider.onCreate()`` was a
  one-line ``return true`` — Android's ContentProvider lazy-spawn
  brought up the ``:provider`` host process and instantiated the
  Provider, but did not start any Service in that process, so
  Python never booted, ``install_callbacks()`` never ran, and the
  ``sDispatch`` slot stayed null. Every ``call()`` then fell
  through to the existing ``daemon_not_ready`` 503 fallback. The
  user's repro: install server APK → open recorder → recorder
  can't reach daemon → user opens server APK launcher → daemon
  finally boots → recorder works on the next attempt.
  ``onCreate()`` now calls ``AZTServiceProviderhost.start(ctx,
  "")`` so PythonService is started alongside the Provider on the
  same lazy-spawn. The boot is async (Python service thread
  spawns separately); the very first peer call may still race
  the boot and surface ``daemon_not_ready``, but ``rpc.call``'s
  existing transport-level retry sees the populated callbacks on
  the second attempt — the user no longer has to babysit the
  install.
- **Auto-exit on self-update.**
  ``azt_collabd.android_cp.service`` now snapshots the package's
  ``PackageManager.lastUpdateTime`` at ``install_callbacks()``
  time and re-reads it after every dispatch. If it has advanced,
  we schedule an ``os._exit(0)`` 500 ms later, so the in-flight
  binder reply has time to land and the next peer
  ContentResolver call lazy-spawns the freshly-installed code.
  Belt-and-braces: Android's package installer normally kills
  the upgraded process for us, but custom-ROM battery savers and
  ``adb pm install -r`` can leave the old daemon running with
  stale code while the new APK is on disk — that produced the
  "after updating, peer connects to old daemon until I
  force-stop the server APK" symptom. Adds one PackageManager
  call per dispatch (cheap; cached by Android in the same
  process).
- Lock-step minor bump because the Provider Java change requires
  a full server-APK rebuild — peers that update without the
  matching server-APK rebuild will still hit the daemon-boot
  race on lazy-spawn and have to open the server APK manually.

### azt_collabd 0.29.2 + azt_collab_client 0.29.2 — fix server-APK crash on KV format
- ``register_kv`` was raising ``KeyError: 'uri'`` from
  ``KV_TEMPLATE.format(font_name=..., share_icon=...)`` because a
  comment I added in 0.29.1 quoted ``"Opening {uri}\n..."`` as
  prose inside the KV string. Python's ``str.format`` reads
  ``{uri}`` as a substitution placeholder regardless of whether
  it sits inside a KV comment. The peer's "Could not open
  project picker: unexpected_cancel" + ``KeyError: 'uri'`` in
  ``register_kv`` traceback is the symptom — the server APK
  crashes during start-up, the picker activity returns
  RESULT_CANCELED with data, peer treats it as the picker
  anomaly retry path and gives up.
- Comment now spells the placeholder as "URL" without braces; the
  same risk applies to any ``{name}``-shaped token in KV
  comments, which is documented in the comment for the next
  editor.

### azt_collabd 0.29.1 + azt_collab_client 0.29.1 — fix unresponsive Begin / GitLab buttons after 0.29.0 restructure
- **GitHubConnectScreen "Begin" did nothing.** Two compounding
  causes, both shipped fixes:
  1. ``primary_action`` was re-fetching credentials_status to pick
     a step every tap. If the freshly-rendered button label said
     "Begin" but the daemon's status said the user was already at
     step 4 (stale from a prior session), the dispatcher fell
     through every elif and silently no-op'd. ``_render_primary``
     now stamps an ``_action`` attribute (``begin`` / ``install`` /
     ``verify``) on the button each time it renders; the
     dispatcher uses that, with a label-based fallback, and
     finally defaults to ``begin()`` so a button labelled "Begin"
     always begins.
  2. ``on_pre_enter`` accessed ``self.ids.gh_user_code`` and
     similar directly. On Kivy ≥ 2.3 the rule's nested children
     can lag a frame after the screen is added, so the early
     accesses raised an ``ObservableDict`` AttributeError mid-
     setup, leaving the screen in KV-default state with no
     ``_action`` tagged. ``on_pre_enter`` now defers via
     ``Clock.schedule_once`` (matches what ``SettingsScreen`` was
     already doing); every helper uses ``self.ids.get(...)`` so a
     genuinely-missing widget no longer takes the whole pass
     down.
- **GitLab/GitHub action buttons "resisted pressing"** in the
  settings screen: tapping the sibling RecBtn fired
  ``contributor_input.on_focus``, which called
  ``save_contributor()`` synchronously — the RPC blocked the UI
  thread for a few hundred ms during which Kivy still received
  the touch but couldn't dispatch the on_release until the RPC
  returned. ``save_contributor`` now runs the ``set_contributor``
  call on a worker thread; the "Saved." flash flips through
  ``Clock.schedule_once`` on the UI thread.
- **Layout-shift fix.** The new ``gh_preflight`` and ``gh_message``
  ``BodyLabel`` widgets used the
  ``height: self.texture_size[1] + dp(8)`` growing pattern; on first
  paint the texture is computed against width=0, so the label
  starts ~30 dp tall and grows as the layout settles, pushing
  every button below it down. Replaced with explicit
  ``height: dp(80)`` so the BoxLayout's ``minimum_height`` is
  stable from frame 0 — no more "tap where the button used to
  be" misses.
- **Tracing.** Added ``[github-connect]`` print lines on
  ``primary_action`` / ``begin`` / ``_refresh_state`` so a flaky
  field repro can be diagnosed from logcat without rebuilding
  with extra logging. Cheap.

### azt_collabd 0.29.0 + azt_collab_client 0.29.0 — GitHub connect-flow UX restructure (audit doc #1–#7)
- **#1 ``verification_uri_complete``**:
  ``GitHubConnectScreen._worker`` now prefers GitHub's
  ``verification_uri_complete`` (URL with the user_code prefilled)
  over the bare ``verification_uri``. Users land on GitHub's
  Authorize? page directly instead of the code-entry detour.
  Falls back to ``verification_uri`` then the bare URL if
  GitHub's response shape ever changes.
- **#2 + #3 step-indicator + pre-flight + no auto-fire**: the
  GitHub connect screen is now organised as three explicit
  stages — *1. Authorize this device* → *2. Install GitHub App*
  → *3. Verify setup* — rendered as a colour/bold-coded
  indicator. A single state-aware "primary" button presents
  only the next required action; ``on_pre_enter`` derives the
  current step from server flags
  (``connected`` / ``app_installed`` / ``confirmed``), so a
  partial setup that picks back up later resumes from where it
  stopped (lost network, browser bail-out, app close all
  recoverable). Pre-flight body text explains what GitHub is and
  that a free account is required; the device flow is never
  auto-fired — the user always taps *Begin* / *Install GitHub
  App* / *Verify setup* explicitly. ``_render_message`` /
  ``_render_steps`` / ``_render_primary`` / ``_render_manage``
  handle the four screen shapes.
- **#4 "Verify setup" rename**: both GitHub and GitLab
  "Test connection" buttons are relabelled "Verify setup" — the
  old label sounded like an optional diagnostic but is actually
  the gate that flips ``confirmed=True``. Status messages
  referencing "Test connection" are updated to match.
- **#5 create-account link**: a "Create a GitHub account
  (free)" NavBtn just below the pre-flight panel opens
  ``https://github.com/signup`` in the user's browser. Pre-flight
  body text also names the account-required precondition so the
  user isn't surprised by GitHub's sign-in/up page.
- **#6 simplified host buttons**: ``SettingsScreen`` now shows a
  single state-aware GitHub button (label flips Connect ↔
  Disconnect from ``credentials_status``) instead of two
  parallel buttons, and a single ``GitLab`` button that opens
  the GitLab settings form. Connection details for both hosts
  remain in the Status block below.
- **#7 declined**: no "Are you sure?" disconnect popup —
  per-maintainer preference; with #1 landed an accidental
  Disconnect costs one tap to redo, and the GitHub App on the
  GitHub account is untouched, so re-Authorize is the only step
  needed. Audit doc records the rationale.
- French .po updated with the new strings; old "Test
  connection" / "Tap Test connection" entries left in place
  (translation-coverage drift detector only flags missing
  msgids, not orphans).
- Lock-step minor bump because the connect-flow restructure
  changes a daemon-side UI subprocess that peers spawn through
  ``open_server_ui()`` — version-string display in the settings
  footer flags the cut.

### azt_collabd 0.28.27 + azt_collab_client 0.28.27 — 60s warmup budget + "Try again" affordance
- ``_DAEMON_WARMUP_RETRIES`` raised again, 15 → 30 (30s → 60s).
  User-reported: 30s wasn't enough on their device — "next boot
  fails, the following one succeeds" — confirming Java-side cold
  spawn time can exceed 30s after ``pm clear`` or fresh install.
- New ``on_retry`` parameter on
  ``install_server_apk_popup``; when supplied, adds a "Try again"
  button to the popup. Bootstrap's
  ``_prompt_server_unresponsive`` now passes
  ``on_retry=_post_install_continuation`` so users on truly slow
  hardware can keep waiting past the 60s budget without having
  to download fresh (Install) or close the app (Quit). Tap →
  popup dismisses → 2s warm-up wait → fresh compat probe.
- Side effect: layouts the popup with up to 4 buttons in the
  action row (Quit | Try again | Open install page | Install).
  Each is text-wrap-bound so labels fit even on narrow screens.

### azt_collabd 0.28.26 + azt_collab_client 0.28.26 — SHA-256 reuse check, drop the time window
- Replaced 0.28.25's mtime-window heuristic with definitive
  SHA-256 verification. After a successful download, ``update.py``
  writes the file's SHA-256 to a sidecar
  ``<asset>.sha256``. ``_has_fresh_download(path)`` now reuses
  the staged file iff (a) the file exists, (b) the sidecar
  exists, (c) recomputing the file's SHA matches the sidecar.
- Eliminates the "10 minutes might not be enough for everyone"
  concern — reuse works regardless of how long the user spent in
  the "Install unknown apps" Settings detour, and regardless of
  device speed.
- Cost: SHA-256 of a typical APK takes ~1–3 seconds on phone
  hardware. Negligible compared to the 10–30 seconds of
  re-download it replaces, especially on slow connections.
- Side benefit: catches APK corruption between download and
  install. If the file gets damaged somehow (rare, but possible
  on flaky storage), the sidecar mismatch forces a re-download
  rather than dispatching a corrupted install Intent.
- ``_save_download_sha(path)`` is non-fatal on failure: a
  missing sidecar just means the next reuse check returns False
  and we redownload, same as before this change.

### azt_collabd 0.28.25 + azt_collab_client 0.28.25 — reuse a recent download instead of re-fetching
- User reported the install flow was downloading the APK twice
  when Android required "Install unknown apps" permission: first
  download → permission detour → user grants → re-tap Install →
  re-download (10–30s wasted).
- New ``_has_fresh_download(path)`` helper checks whether the
  staged file at ``$AZT_HOME/updates/<asset>`` was last modified
  within ``_REUSE_DOWNLOAD_AGE_S`` (10 minutes). Both
  ``install_apk_from_url`` and ``check_for_update``'s download
  paths now skip the download when the file is fresh enough,
  surfacing ``Using already-downloaded file…`` status in place
  of the percentage progress.
- 10-minute window is conservative: long enough to cover the
  typical "user popped to Settings and came back" duration even
  on slow devices; short enough that a stale APK from a
  previous session (yesterday's launch, etc.) won't be
  installed when there's a newer release available.
- ``_download`` writes to ``<path>.part`` and only renames on
  success, so a present ``<path>`` is always a complete
  download — the freshness check doesn't need to validate
  partial-file recovery.

### azt_collabd 0.28.24 + azt_collab_client 0.28.24 — extend daemon-warm-up budget for cold starts
- ``_DAEMON_WARMUP_RETRIES`` raised from 5 to 15 (10s → 30s
  budget). The 503 ``daemon_not_ready`` response that fires
  during cold starts comes from ``AZTCollabProvider.java``'s
  ``sDispatch == null`` check, NOT from my Python sentinel
  hook — it's the genuine "Python interpreter not yet loaded"
  state. After ``pm clear`` or a fresh install the cold start
  can run 15–25 seconds because the dex cache needs rebuilding,
  and the previous 10s budget was tripping users into the
  "AZT Collaboration not responding" popup unnecessarily.
- Warm-cache normal launches still exit the retry loop on the
  first compat-probe success (1–3s typical), so the longer
  budget isn't user-visible in steady state. Cold-start users
  see up to 30s of "Connecting to AZT Collaboration service…"
  popup before falling through.
- Diagnostic clarification noted in the comment block: the 503
  has two possible sources (the test sentinel + the Java
  provider's startup gate); when troubleshooting, check that
  the daemon process is actually alive
  (``adb shell ps -A | grep aztcollab``) — if not, it's the
  Java side and the only fix is waiting longer or warming up
  the dex cache.

### azt_collabd 0.28.23 + azt_collab_client 0.28.23 — visible "Connecting…" popup during retries
- 0.28.19 added the daemon-warm-up retry loop, but the
  ``Connecting to AZT Collaboration service…`` status only flowed
  to ``ctx.on_status`` (host's status sink, often invisible).
  Result: 10s of empty-state peer UI with no feedback while the
  retries ran.
- New modal ``_show_connecting_popup`` opens on the first retry
  with a centred "Connecting to AZT Collaboration service…"
  message. ``auto_dismiss=False`` so the user can't tap past it.
  Dismissed on every terminal branch — ``compat ok``,
  ``server_too_old``, ``client_too_old``, retries-exhausted, or
  raised exception — before the next branch's UI fires (so the
  unresponsive popup doesn't stack on top of the connecting one).
- Mutable ``connecting_popup`` slot added to ``_Ctx`` so the
  show / dismiss helpers can find each other across the worker-
  thread boundary without a module-level dict. Idempotent: if a
  popup is already up, ``_show_connecting_popup`` is a no-op.

### azt_collabd 0.28.22 + azt_collab_client 0.28.22 — toggle the debug-503 sentinel from settings UI
- 0.28.21's sentinel could only be created via
  ``adb shell run-as``, which fails on release-signed APKs
  (``run-as`` requires the package to be debuggable).
- New "Debug (testing)" section in the daemon settings UI
  (``SettingsScreen``) with a toggle button that creates / removes
  ``$AZT_HOME/_debug_force_503``. State indicator below shows
  whether ``/v1/health`` is currently forced to 503 or responding
  normally. Always visible — production users tapping it just see
  "service unavailable" until they tap again.
- Test workflow: tap the server APK launcher icon (it's an
  installed app with its own icon) → settings UI opens directly
  (bypasses bootstrap; settings calls don't go through
  ``/v1/health``) → toggle Debug → close → launch peer →
  bootstrap retries 5×2s → unresponsive popup fires. Re-open
  server APK to toggle off when done.

### azt_collabd 0.28.21 + azt_collab_client 0.28.21 — debug hook to test the "not responding" popup
- Daemon's ``/v1/health`` (the compat handshake endpoint) now
  returns ``503 daemon_not_ready`` when
  ``$AZT_HOME/_debug_force_503`` exists. Toggle without restarting
  the daemon — the file presence is checked per-request. Create
  via ``adb shell run-as org.atoznback.aztcollab touch
  files/azt/_debug_force_503``; remove with the equivalent ``rm``.
- Lets the bootstrap workflow's daemon-warm-up retry path
  (``_DAEMON_WARMUP_RETRIES = 5`` × 2s = 10s) exhaust deterministically,
  exercising the "AZT Collaboration not responding" popup added
  in 0.28.20. Without this, manually triggering the unresponsive
  state required either killing the daemon mid-spawn (race-prone)
  or breaking the install (signature mismatch — heavy).

### azt_collabd 0.28.20 + azt_collab_client 0.28.20 — modal recovery popup when daemon stays unresponsive
- 0.28.19's retry-with-backoff fixed the common case (daemon
  warming up settles within 1–3s) but still fell through to
  ``_check_self`` → ``on_done`` after the retry budget exhausted.
  Result: the same bouncing-out behaviour the user originally
  reported, just delayed by 10s. ``on_done`` fired, peer's
  startup tried daemon RPCs, hit ``ServerUnavailable``, picker
  fired and failed, picker emitted CANCEL, peer closed via the
  picker-cancel rule.
- **Real fix**: when the warm-up retries exhaust without daemon
  response, bootstrap now shows a modal popup
  (``_prompt_server_unresponsive``) — same canonical
  ``install_server_apk_popup`` as the missing-server case, with
  a body reading "AZT Collaboration is installed but did not
  respond. It may still be starting up; wait a moment, then tap
  Install to reinstall it, or Quit to close this app and try
  again later." Title: "AZT Collaboration not responding".
- The popup gives the user three explicit recovery options
  (Reinstall, Open install page, Quit) instead of silently
  bouncing them out of the app. Modal blocking means the peer
  stays in the foreground until the user makes a choice, and
  ``on_done`` is not fired (so the peer doesn't run its
  post-bootstrap startup against a daemon that isn't there).
- Reinstall path: standard download+install via
  ``install_apk_from_url``; on completion the post-install
  continuation re-runs ``_check_server`` and on_done fires from
  the healthy path. Quit path: closes the peer cleanly so the
  next launch starts fresh.

### azt_collabd 0.28.19 + azt_collab_client 0.28.19 — retry on daemon-warm-up race at startup
- **Symptom user reported:** opening peer A, log shows
  ``[bootstrap] AZT Collaboration installed but unreachable.
  Continuing offline.`` followed by ``[recent] last_project:
  ServerUnavailable: provider HTTP 503: daemon_not_ready``, then
  Android brings the previously-foregrounded peer B to the
  front. Sequence: bootstrap fires ``on_done`` thinking everything
  is fine because the server APK is installed; peer's normal
  startup tries ``last_project()`` while the daemon is still
  warming up; the daemon's ContentProvider returns 503; peer's
  picker logic kicks in, fails with the same 503; picker emits
  ``RESULT_CANCELED``; the picker-cancel rule from
  ``CLIENT_INTEGRATION.md`` § 5 closes the peer; Android brings
  the most-recent task forward.
- **Fix:** ``_check_server`` now retries the compat probe with
  backoff (``_DAEMON_WARMUP_RETRIES=5``,
  ``_DAEMON_WARMUP_INTERVAL_S=2.0`` → 10s budget total) when the
  server APK is installed but unreachable. Status flips to
  ``Connecting to AZT Collaboration service…`` during retries.
  Android lazy-spawns the server APK's Python interpreter on the
  first ContentResolver call; this typically settles within
  1–3 seconds, well under the budget.
- If the retries exhaust, we still fall through to
  ``Continuing offline.`` and ``_check_self`` (which fires
  ``on_done``). At that point the daemon is genuinely unreachable
  (crashed, hardware glitch, signature mismatch denied us access)
  and bootstrap can't fix it; the peer's host code is responsible
  for handling ``ServerUnavailable`` on its post-on_done RPCs.
  Recommend defensive try/except around the first 1–2 RPCs the
  host makes after ``on_done`` — already in
  ``CLIENT_INTEGRATION.md`` § 4 ("log the failure and continue,
  not pop their own dialog").

### azt_collabd 0.28.18 + azt_collab_client 0.28.18 — move CLIENT_INTEGRATION.md into the symlinked package
- ``CLIENT_INTEGRATION.md`` moved from ``docs/`` (canonical-repo
  only) to ``azt_collab_client/`` (symlinked into every peer).
  Peers now see the integration contract through their existing
  symlink without needing a separate ``azt-collab/`` checkout.
  Old ``docs/CLIENT_INTEGRATION.md`` is reduced to a one-line
  redirect for anyone with the old path bookmarked.
- Added missing section on ``on_done`` semantics (introduced in
  0.28.5 but the doc never reflected it). Renumbered subsections
  to fix a duplicate ``## 6`` heading. Added the
  ``install_apk_from_url`` entry to the "what the suite does for
  you" list (added in 0.28.10, doc never updated).
- ``azt_collab_client/CLAUDE.md`` updated to point at the new
  in-package location.

### azt_collabd 0.28.17 + azt_collab_client 0.28.17 — peer self-update gets the same progress UI as server install
- **Peer self-update now uses the same popup as the server case**
  with progress visible in the body. Previously
  ``_prompt_self_update`` showed a Yes/No popup that dismissed on
  Update tap, then ran ``install_apk_from_url`` "in the background"
  with status flowing only to the host's ``on_status`` sink — which
  meant the user saw nothing until the install finished. Now
  bootstrap calls ``install_server_apk_popup`` (parameterized for
  the peer's own APK) so the same body-label progress (downloading
  %, retrying status, "Installing…", "Installed.") is on screen
  through the entire flow. Closes the user-reported "looks like
  it's stuck — Update just means OK" symptom.
- **``install_server_apk_popup`` is now a generic install/update
  popup**. New parameters:
  - ``direct_url`` — overrides the composed download URL.
  - ``asset_filename`` — overrides the on-disk staging name +
    MediaStore display name.
  - ``open_page_url`` — overrides the "Open install page" target
    so self-update points at the peer's release page instead of
    the server's.
  - ``dismiss_label`` — overrides the dismiss button label.
  - ``dismiss_action`` — ``'quit'`` (default; closes app) for the
    server case; ``'dismiss'`` for self-update where declining
    means "stick with current version, peer keeps running".
  - ``install_target_package=''`` — explicit "no polling" sentinel
    for self-update where the install kills the running peer
    process.
- **Self-update decline records the version only on Not-now tap.**
  Previously the decline was recorded synchronously in
  ``_prompt_self_update``'s decline handler. The new popup-based
  flow records on dismiss, but only when no install was started
  — if the user tapped Update and the install kicked off, no
  decline gets recorded (the next launch will detect the new
  version naturally instead).
- ``_yes_no`` helper removed; no callers left after the refactor.
- Lock-step bump 0.28.16 → 0.28.17 with ``MIN_SERVER_VERSION``
  raised to match (continues the user's "test the server-update
  path" workflow — every iteration of the bump fires the
  too-old-server prompt for a peer that bundles the new client).

### azt_collabd 0.28.16 + azt_collab_client 0.28.16 — bump MIN_SERVER_VERSION to test server-update path
- Lock-step debug bump 0.28.15 → 0.28.16 across both packages.
- ``azt_collab_client.MIN_SERVER_VERSION`` raised 0.27.0 → 0.28.16.
  Forces a rebuilt peer (which bundles client 0.28.16) to refuse
  any server APK older than 0.28.16. Test path: install peer with
  bundled 0.28.16, leave the older server APK (0.28.15 or earlier)
  on the device, launch peer → ``check_server_compat`` returns
  ``server_too_old`` → bootstrap fires
  ``_prompt_server_update`` → install popup shows the
  "Update AZT Collaboration?" body with the "Update" button label
  and pre-filled current_server_version (so the daemon doesn't
  redownload an identical release if there isn't actually a
  newer one published).
- ``MIN_CLIENT_VERSION`` (in azt_collabd) stays at 0.27.0 — this
  bump is for testing the server-too-old path, not client-too-old.

### azt_collabd 0.28.15 + azt_collab_client 0.28.15 — fix Install button stuck after "unknown apps" detour
- **"Tap Update again" message corrected** to use the actual
  install-button label. The popup's button is "Install" (or
  whatever ``install_label`` the caller passed), not "Update", so
  the previous message ("…then tap Update again") was wrong for
  every caller except the settings-screen Update buttons. Now uses
  ``{label}`` substitution and the popup passes its own button
  text.
- **New ``on_user_action_needed`` callback** in both
  ``install_apk_from_url`` and ``check_for_update``. Fires when
  the install path stalls because Android needs the user to flip
  "Install unknown apps" for this peer in Settings. Without this,
  the popup's Install button stayed disabled forever after we
  routed the user to settings — only Quit was active. The popup
  now wires this callback to re-enable Install + Open install
  page so the user can come back from Settings and retry.
- ``install_label`` parameter added to both functions so callers
  can override the label used in the "tap {label} again"
  message. Defaults to "Install" for ``install_apk_from_url``,
  "Update" for ``check_for_update`` (matching their
  conventional UX context).

### azt_collabd 0.28.14 + azt_collab_client 0.28.14 — fix language-toggle inertness + URL overflow in error
- **Language toggle in `install_server_apk_popup` now actually
  switches.** The handler called ``popup.dismiss()`` then
  ``install_server_apk_popup(...)`` synchronously from inside a
  Button.on_release — Kivy silently no-ops that re-entrance in
  some versions because the original popup is mid-dismiss. Fix:
  defer dismiss + relaunch via ``Clock.schedule_once(..., 0)`` so
  the touch handler returns first. Also added stderr logging at
  every step (``[install_popup] language switch: fr``,
  ``[install_popup] dismiss raised:`` …) so any future failure is
  diagnosable via ``adb logcat``.
- **Long URLs in error messages now wrap inside the popup.**
  ``_download``'s 404-with-URL surface for a 60-character GitHub
  asset URL was running off the body label because Kivy Labels
  only break at whitespace and URLs have none. New
  ``_wrappable_url(url)`` helper in ``update.py`` inserts a real
  ``\n`` after each ``/`` (only when the URL is over 50 chars) so
  the URL renders across multiple lines inside the popup body.
  Display is uglier but legible — readable URLs trump pretty
  ones for diagnosis.

### azt_collabd 0.28.13 + azt_collab_client 0.28.13 — diagnostic logging + browser-like headers in download
- ``_download`` now logs to stderr (visible in ``adb logcat``) at
  every meaningful step: the URL it's about to GET, the redirect
  target if any, the HTTP status, and the URL the server actually
  served the 404 from (``HTTPError.url``). Disambiguates
  "github.com returned 404" from "github.com 302'd to the CDN, CDN
  returned 404" — different diagnoses (asset truly missing vs.
  bot-pattern rejection on the CDN edge or expired-token edge
  case).
- Added ``Accept: */*`` and updated the User-Agent string to
  ``'azt-collab-updater/1 (+curl-compat)'``. Some GitHub CDN edges
  return 404 to bare-pattern UAs; mimicking curl removes that
  variable.
- Diagnostic-only round; the underlying 404 puzzle (browser works,
  ``gh release view`` confirms the asset, but Python urllib gets
  404 three times) is still being investigated. The new logging
  should make the next reproduction definitively diagnosable.

### azt_collabd 0.28.12 + azt_collab_client 0.28.12 — popup polish: language toggle, version footer, URL in error
- **Discrete language toggle** at the top of
  ``install_server_apk_popup``. First-install users whose device
  locale is French (or any non-English) had no way to switch
  language since the popup blocks the settings UI; now there's a
  small row of language buttons (current bolded, others tappable)
  that dismisses + re-opens the popup with the chosen language.
  Only shown when ``i18n.available_languages()`` returns more than
  one — desktop hosts running an English-only build won't see it.
- **Version footer** at the bottom of the popup
  (``client X.Y.Z``). Subtle / dim, mirrors the version strip
  pattern from the daemon settings UI. Helps diagnose which
  client build is actually live when reproducing UI bugs across
  versions.
- **Download error includes the URL we tried.** When the asset
  download fails (404 from the well-known direct URL, or any
  other transport error), the surfaced message now appends the
  URL on a new line. Lets the user eyeball the URL against what
  their browser successfully fetches — most 404s on this path
  come from an asset-name mismatch on the GitHub release, not a
  transport bug.

### azt_collabd 0.28.11 + azt_collab_client 0.28.11 — popup button wrapping, URL composition cleanup
- **Install popup button text wraps now.** Previously "Open install
  page" and "Quit AZT Recorder" / "Quit AZT Viewer" got clipped on
  narrower screens because Kivy Buttons don't wrap by default.
  ``text_size`` is now bound to button size on all three buttons
  (``halign='center'`` + ``valign='middle'``), and the button row
  height bumped to dp(60) to allow two-line wraps. Popup overall
  height also bumped from dp(280) to dp(300) to compensate.
- **Direct-URL composition consolidated** into the install popup's
  ``_do_install``. The hardcoded ``_DIRECT_DOWNLOAD_URL`` constant
  in ``update.py`` is gone; the popup now composes
  ``f'https://github.com/{_SERVER_REPO_DEFAULT}/releases/latest/download/{_SERVER_ASSET_DEFAULT}'``
  from the same constants the package-presence probe uses
  (``bootstrap._SERVER_REPO_DEFAULT``,
  ``bootstrap._SERVER_ASSET_DEFAULT``). Single source of truth: a
  fork that wants to point its server-install at a different
  release feed only edits the bootstrap constants.

### azt_collabd 0.28.10 + azt_collab_client 0.28.10 — direct-URL install for popup + peer self-update
- **New ``install_apk_from_url(url, asset_filename, ...)``** in
  ``azt_collab_client.ui.update``. Direct-URL alternative to
  ``check_for_update``: GETs the URL, streams to disk, dispatches
  Android's installer, optionally polls for completion via change-
  detection. No GitHub API call, no JSON parsing, no asset name
  matching, no listing-endpoint quirks. For when the caller has a
  stable redirect URL like
  ``releases/latest/download/<asset>`` and doesn't need version
  comparison.
- **`install_server_apk_popup`** now uses ``install_apk_from_url``
  for the Install button. Closes the user-reported "install
  comes back 404" symptom — the API path's ``_pick_asset`` step
  was failing on edge cases (asset-name mismatch, listing
  endpoint quirks). Direct URL bypasses the entire failure
  surface.
- **`bootstrap._do_self_install`** also migrated. Composes
  ``f'https://github.com/{peer_repo}/releases/latest/download/{peer_asset_filename}'``
  from the args bootstrap already takes. Version comparison
  still happens earlier in ``_peer_update_with_confirm`` (which
  needs the API for the small tag-name lookup); by the time we
  reach the install action, the user has confirmed the prompt
  and we just need to install whatever's at the URL. No
  ``install_target_package`` because self-install replaces the
  running peer process.
- **`_start_install_poll`** now supports two modes — pinned-
  version (used by ``check_for_update`` when it knows what
  version it just downloaded) and change-detection (used by
  ``install_apk_from_url`` which doesn't have version metadata).
  Change-detection snapshots the current installed versionName at
  start, then polls for any difference; trivially handles the
  uninstalled→installed case.
- **`_download`** now reads ``Content-Length`` from the response
  headers when the caller doesn't pre-supply ``total_bytes``, so
  progress percentages work for the direct-URL path too.
- **Settings-screen "Update this app" buttons** (CollabUIApp +
  PickerApp) stay on ``check_for_update`` because they want the
  "Up to date" message when the user taps without a newer version
  available — that's the value of the API path there.

### azt_collabd 0.28.9 + azt_collab_client 0.28.9 — restore "Open install page" semantics
- ``SERVER_APK_INSTALL_URL`` reverts to the **release page** URL
  (``https://github.com/kent-rasmussen/azt-collab/releases/latest``),
  not the direct-download asset URL. The popup's "Open install
  page" button is for users who want to read release notes or
  browse the project before installing — the page is what serves
  that purpose. The "Install" button in the same popup is the
  one-tap-to-install path; it discovers the asset URL via the
  GitHub API at runtime (asset['browser_download_url']) rather
  than from this constant.
- Effectively reverts the 0.28.3 URL-direction change. Sole
  consumer of ``SERVER_APK_INSTALL_URL`` is
  ``install_server_apk_popup._open_page``; everything else (the
  bootstrap workflow, ``check_for_update``) computes the
  direct-download URL itself.

### azt_collabd 0.28.8 + azt_collab_client 0.28.8 — fix SSL on Android urlopen
- p4a doesn't ship system CA certs into the Android Python
  runtime, so the new client-side ``urllib.request.urlopen`` calls
  in ``azt_collab_client.ui.update`` (release listing, asset
  download) fail with "unable to get local issuer certificate".
  The daemon side has had ``azt_collabd/net.py:_ensure_ssl()`` for
  this since forever, but the client can't import it (Hard rule 3:
  no daemon import; the two run in different processes on Android
  anyway).
- New ``azt_collab_client/net.py`` mirrors the daemon's SSL patch,
  slimmed for the client (no urllib3.PoolManager surface — the
  client doesn't speak dulwich). ``_ensure_ssl()`` is idempotent
  and called at the top of every urlopen site in ``update.py``.
  Finds certifi's bundle (preferred), falls back to extracting it
  from the bundled zip into ``$ANDROID_PRIVATE/cacert.pem``, then
  to common system locations, then to a verification-disabled
  context as a last resort.
- Symptom this fixes: post-popup-open SSL error on the bootstrap
  install flow's GitHub release probe — the "is a newer release
  available" call (``_fetch_latest``) and the asset-binary stream
  (``_download``) both bypassed SSL setup before this fix.

### azt_collabd 0.28.7 + azt_collab_client 0.28.7 — fix ModuleNotFoundError on popup open
- One-character relative-import bug introduced in the 0.28.4 popup
  refactor: ``azt_collab_client/ui/popups.py:369`` had
  ``from ..bootstrap import …`` (resolves to
  ``azt_collab_client.bootstrap`` — doesn't exist) instead of
  ``from .bootstrap import …`` (the correct
  ``azt_collab_client.ui.bootstrap``). The error fired the moment
  ``install_server_apk_popup`` was opened on a peer with no server
  installed, which raised inside Kivy's main loop and took the
  peer down — visible as "presplash, brief app screen, close".
  The user-reported symptom from 0.28.4 onward; the 0.28.5 / 0.28.6
  bootstrap fixes never had a chance to run because the popup
  itself couldn't load.

### azt_collabd 0.28.6 + azt_collab_client 0.28.6 — auto-resume after server install
- **Post-install continuation.** Once the install-completion poll
  watchdog confirms the new server APK is live, the popup auto-
  dismisses (after a 1-second visual confirmation showing
  "Installed.") and bootstrap re-enters its compat check. Daemon
  is now reachable, so the healthy path takes over and on_done
  fires, letting the host continue normal startup. No manual
  Quit + relaunch needed.
- New ``check_for_update(on_install_complete=...)`` parameter,
  threaded through ``_start_install_poll``. Fires only on
  confirmed completion (versionName flipped), not on the
  watchdog timeout — that branch still leaves the user with the
  "Install pending" message and the popup up.
- New ``install_server_apk_popup(on_install_complete=...)``
  parameter wires the upstream callback to (a) dismiss the popup
  after a 1s delay and (b) call the host's continuation. Bootstrap
  passes a continuation that schedules a 2-second daemon-warm-up
  pause (Android lazy-spawns the ContentProvider host on first
  call) before re-running ``_check_server``.
- If Android kills the peer process during install (memory
  pressure, system installer dominating), the popup + its
  continuation chain are gone too. Re-launch triggers a fresh
  bootstrap, which finds the daemon reachable and flows through
  the healthy path — same outcome, just one extra user action.

### azt_collabd 0.28.5 + azt_collab_client 0.28.5 — fix flash-then-die regression in 0.28.4
- **Bootstrap no longer fires on_done before the no-server popup
  opens.** In 0.28.4 the prompt branches called
  ``_on_done_and_release(ctx)`` immediately, then opened the popup
  on the next UI tick. Hosts whose ``on_done`` is wired to
  "continue normal startup" then attempted RPCs against a daemon
  that wasn't there yet, the failure cascaded into App.stop() (or
  similar in the host's error handling), and the popup that was
  about to open was killed alongside it — visible as a screen flash
  then peer shutdown.
- New ``_release_running()`` helper splits the guard release from
  the on_done notification. ``_on_done_and_release`` keeps both
  for the healthy terminal paths (server compat OK, no self-
  update needed). The two no-server branches —
  ``_prompt_server_install`` and ``_prompt_server_update`` —
  release the guard but don't fire on_done, so the host stays
  parked at whatever screen was up when ``bootstrap()`` was
  scheduled (typically a splash). Once the user installs the
  server APK and the peer relaunches, bootstrap re-fires from a
  fresh process, finds the daemon reachable, and on_done flows
  through ``_check_self`` along the healthy path.
- The already-declined-this-version branch in
  ``_prompt_server_update`` does still fire on_done (with the
  caveat that the host's first RPC will surface the daemon's
  compat error). The user explicitly chose this state by
  declining earlier; the host should handle it gracefully.

### azt_collabd 0.28.4 + azt_collab_client 0.28.4 — single canonical "no server" popup, modal blocking, Quit button, doc consolidation
- **Single popup for "no server" / "server too old" cases.** Bootstrap's
  `_prompt_server_install` and `_prompt_server_update` now both
  delegate to `install_server_apk_popup` (instead of the older
  generic Yes/No `_yes_no` helper). Result: one visual surface,
  one set of buttons, one progress sink, one decline path. Closes
  the user-reported bug where two popups stacked on first launch
  ("Could not open project picker: server_apk_not_installed" + the
  bootstrap Yes/No, OR the bootstrap Yes/No + the older
  "AZT collaboration service required" widget).
- **Popup is now modal-blocking** (`auto_dismiss=False`). The user
  can't tap past it to reach a settings screen or picker that
  would itself fail with "server_apk_not_installed". Resolves the
  user-reported "in the client settings page, asking to Select
  Project resulted in widget 1" — once bootstrap fires, settings is
  unreachable until the user installs or quits.
- **Quit button replaces "Dismiss".** Label is "Quit {App.title}"
  (e.g. "Quit AZT Recorder") — falls back to plain "Quit" if the
  host hasn't set a title. Tapping it dismisses the popup AND
  calls `App.get_running_app().stop()`. Without the server APK
  the peer can't function, so leaving it running was the wrong UX.
- **Install button shows live progress in the popup body.** While
  `check_for_update` runs, the body label updates with
  "Downloading 45%…", "Release in progress — retrying in 5s…",
  "Installing…", "Installed." (or "Install pending. Reopen this
  app when finished." on the polling-timeout branch). The popup
  stays open through the whole flow — no more "I tapped Install
  and nothing happened" because the popup dismisses-and-routes-
  elsewhere. Buttons disable while the worker runs to prevent
  double-taps.
- **`install_server_apk_popup` gained context parameters**
  (`body_message`, `current_server_version`, `install_target_package`,
  `install_label`, `title`) so the same popup serves both the
  missing-server case (default) and the too-old-server case
  (passed by `_prompt_server_update`). Different body text and
  Install-button label, same machinery.
- **Bootstrap dead code pruned.** `_do_server_install` and
  `_quit_app` removed — both were one-call helpers absorbed into
  the popup refactor. `_yes_no` survives for the self-update
  prompt (different decision: peer can keep running on decline).
- **`docs/CLIENT_INTEGRATION.md`** added — the canonical "what every
  client must do" checklist. Sections: symlinks, buildozer.spec
  permissions / signing / manifest extras, **bootstrap wiring +
  the four caller invariants**, **don't roll your own server-missing
  UI** (the source of the user-reported bugs), translation chain,
  `App.title` for the Quit button, LIFT / audio / image access,
  recovery, testing. `azt_collab_client/CLAUDE.md` now points to
  it as the canonical reference.
- Translations (fr): "Quit", "Quit {app}", and the longer
  bootstrap-prompt body text used by the popup
  (`This app needs the AZT Collaboration service ({name}) to sync
  your data. Tap Install to download and install it. Android will
  ask you to confirm before the install starts.`).

### azt_collabd 0.28.3 + azt_collab_client 0.28.3 — install-popup auto-download, asset filename fix, install-completion polling, release cache, bookkeeping
- **Asset filename fix.** Every codebase reference to the server APK
  asset was ``azt_collab.apk`` (with underscore), but the Android
  ``package.name = aztcollab`` in server_apk/buildozer.spec.tmpl
  drops separators per the suite naming table — actual published
  asset is ``aztcollab.apk``. The 5 hardcoded references —
  ``bootstrap._SERVER_ASSET_DEFAULT``, ``CollabUIApp.share_apk`` /
  ``update_app``, ``PickerApp.share_apk`` / ``update_app`` — all
  fixed. The bootstrap workflow's GitHub-API asset lookup was
  returning "no aztcollab.apk in release" 404s because of this; now
  matches the actual release feed.
- **`SERVER_APK_INSTALL_URL` is now a direct-download URL**
  pointing at
  ``https://github.com/kent-rasmussen/azt-collab/releases/latest/download/aztcollab.apk``.
  GitHub's stable redirect serves the most recent matching asset,
  so the URL doesn't need updating per release.
- **`install_server_apk_popup` triggers auto-install** instead of
  only opening the browser. Tap Install → ``check_for_update``
  fetches the latest ``aztcollab.apk`` asset, streams it to
  ``$AZT_HOME/updates/``, and dispatches Android's system
  installer. The popup's "Open install page" affordance is
  retained as a fallback for users whose Android can't trigger the
  install intent. Progress strings flow back through the popup
  body and the host's ``on_status`` sink.
- **Install-completion polling** for cross-package installs
  (server-from-peer). New ``check_for_update(install_target_package=...)``
  parameter. After dispatching the install intent, the helper
  polls ``PackageManager.getPackageInfo`` every 5s for up to 5min,
  fires ``on_status('Installed.')`` when the version flips to the
  freshly-downloaded one, and ``on_status('Install pending.
  Reopen this app when finished.')`` on timeout. Closes the
  long-standing UX wart where status hung at "Installing…" forever
  after the user backed out of the system installer or the install
  finished out-of-foreground. Self-installs (peer-from-peer) skip
  polling — the install replaces the running peer, so polling
  would block forever. Bootstrap passes the server's package name
  on its server-install path; the same path is taken when the
  ``install_server_apk_popup`` Install button fires.
- **Per-process release cache** for ``_fetch_latest``. 5-minute TTL,
  keyed by repo slug. Closes the rate-limit hazard where a
  bootstrap launch + a settings-screen Update tap + multiple peers
  behind one NAT could collectively drain the GitHub anonymous
  60/hour budget; subsequent calls within the TTL hit the cache.
- **Caller invariants** — the four contracts the bootstrap caller
  must honor (asset name match, parseable tag, prerelease flag,
  ``REQUEST_INSTALL_PACKAGES`` permission) are now consolidated as
  a top-level "Caller invariants" section in
  ``azt_collab_client/ui/bootstrap.py``. Each was scattered across
  the function docstring + the client CLAUDE.md recipe + update.py
  comments before; now a single canonical list.
- **`p4a.sign = True` removed** from the suite's
  ``server_apk/buildozer.spec.tmpl`` (separately from the earlier
  ``android.signing.*`` cleanup). Confirmed empirically: this spec
  key is also dead config; signing depends solely on the
  ``P4A_RELEASE_KEYSTORE`` env vars. Memory feedback updated.
- **Daemon version bump 0.27.0 → 0.28.3** (lock-step with client)
  to signal that this round touched both packages. No wire-format
  change; cross-floors (``MIN_CLIENT_VERSION`` /
  ``MIN_SERVER_VERSION``) stay at 0.27.0 — older clients/servers
  still talk to this daemon/client without issue.
- **`docs/p4a_hook_picker_intent.md` path-leak scrub.**
  ``/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py`` →
  ``$P4A_HOOK`` (the env-var-resolved path) for public-repo
  consumption.
- **"Not now" on server install closes the peer app.** Without
  the server APK the peer can't function (no daemon → no sync, no
  project picker), so dropping the user into a broken state is
  worse than asking them to relaunch. Server-*update* decline
  doesn't quit (peer can still work against the older server,
  bound by ``MIN_SERVER_VERSION``); self-update decline doesn't
  quit either (peer is fine at current version). New
  ``_quit_app`` helper in bootstrap.py.
- **Download retry on transient HTTP statuses** (``404, 429, 500,
  502, 503, 504``). Load-bearing case: GitHub publishes the release
  JSON before the asset binary finishes uploading, so
  ``browser_download_url`` briefly 404s. Three attempts with linear
  backoff (5s, 10s, 15s ≈ 30s total). Between attempts, the user
  sees translated "Release in progress — retrying in Ns…" so a
  hung worker thread is no longer a confusion. New
  ``on_status`` parameter on ``_download``.
- **Translations (fr)** for the new state strings: "Installed.",
  "Install pending. Reopen this app when finished.",
  "Release in progress — retrying in {s}s…", and
  "Tap Install to download and install it. Android will ask you to
  confirm before the install starts."

### azt_collab_client 0.28.1 — bootstrap hardening + first automated tests
- **Filter prereleases** from the latest-release probe in
  `update.py:_fetch_latest`. Walks `/releases?per_page=20` for the
  first stable entry; falls back to `/releases/latest` if every
  recent release is a prerelease or the listing endpoint refused.
  Closes the v0.28.0 bug where a project pushing a `vN-rc` tag would
  silently auto-install onto every peer.
- **bootstrap idempotence guard.** A second `bootstrap()` call within
  the same process now no-ops. Prevents double-prompting when an
  on_start hook fires twice during a Kivy reload or two startup
  hooks both wire the helper.
- **Decline memory.** When the user taps "Not now" on a prompt, the
  declined version is persisted to
  `$AZT_HOME/config.json :: bootstrap.declined.<repo>=<version>`.
  Subsequent launches skip the prompt for that exact version; a
  new upstream release moves us off the recorded value
  automatically (string compare, not semver).
- **Disambiguate "server APK absent" from "daemon unreachable"** by
  probing `PackageManager.getPackageInfo('org.atoznback.aztcollab')`
  before issuing the install prompt. If the package is installed but
  the daemon happens to be down (no network, OOM-killed mid-call),
  the helper now skips the install prompt and continues to the
  self-check instead of asking the user to install something that's
  already there. New status string
  "AZT Collaboration installed but unreachable. Continuing offline."
- **First automated test scaffold.** New `azt-collab/tests/` directory
  with pytest fixtures (per-test `$AZT_HOME` redirection, jnius stub,
  Kivy headless flags, platform monkeypatch) and five test modules
  covering version-tuple corner cases, GitHub-API mocks for
  `check_for_update`, bootstrap dispatch + idempotence + decline
  memory + package-presence disambiguation, the `github.confirmed`
  store lifecycle, and a translation-coverage drift detector.
  Run with `pytest tests/ -q`. CLAUDE.md updated to retire the
  "no automated test suite anywhere in the suite" claim.
- **`docs/research_notes_2026-05.md`** captures the state of the
  art for the technologies we depend on (Android 16, the March-2026
  sideloading lockdown, ACTION_VIEW deprecation in favor of
  PackageInstaller, buildozer/Kivy versions, GitHub API behavior).
  Action items are owned in the file. Refresh before each major
  release.
- **`docs/test_plan.md`** is the canonical failure-mode matrix. Every
  bug found in the bootstrap workflow lands here as a row in §10
  before it gets fixed.
- One new translation: "AZT Collaboration installed but unreachable.
  Continuing offline."

### azt_collab_client 0.28.0 — bootstrap() one-call peer entry point
- New `azt_collab_client.ui.bootstrap(...)` helper. Peers call it
  once on `App.on_start` and the helper handles, in this order:
  1. `check_server_compat()`. On `server_unreachable` →
     "Install AZT Collaboration?" Yes/No popup → `check_for_update`
     against `kent-rasmussen/azt-collab` → Android system installer.
     `server_too_old` runs the analogous "Update AZT Collaboration?"
     prompt. `client_too_old` jumps to step 2.
  2. Probe peer's own latest release on GitHub. If newer →
     "Update <peer>?" Yes/No → download+install the peer's APK.
  3. `on_done` — every up-to-date / declined / completed-install
     branch lands here so the host's normal startup always
     resumes.
  Suite UX rule encoded by this helper: **the user installs one
  APK** (the peer they opened); the standalone server APK and all
  subsequent updates are provisioned by the peer itself on first
  run. Spawns a worker thread for the version probes so first
  paint isn't blocked; popups marshal back to the Kivy UI thread.
- Android-only effects. Desktop hosts call `on_done` immediately.
- Buildozer requirement documented (`REQUEST_INSTALL_PACKAGES` in
  the peer's `android.permissions`); without it the install intent
  silently no-ops.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  the recorder, viewer, and any future peer can wire one
  ten-line `App.on_start` call and let the helper take it from
  there.
- New translations (fr): "AZT Collaboration", "Checking
  installation…", "Install AZT Collaboration?", "Update AZT
  Collaboration?", "Update {name}?", body strings for each prompt,
  "Install" / "Update" / "Not now" buttons, "Update needed" info
  popup, "Updating {name}…", "AZT Collaboration is up to date.",
  and the rare client-too-old-no-newer-release fallback message.

### azt_collabd 0.27.0 + azt_collab_client 0.27.0 — symmetric host credential flow
- **`github.confirmed` is now a stored flag**, not derived. Mirrors
  the existing GitLab semantics: set true by a successful live test,
  reset to false on any settings change (token save, app-install
  flag flip, disconnect). Per-host shape is now uniform — both
  GitHub and GitLab expose `connected` ("settings present") and
  `confirmed` ("tested OK against the host's API").
- New endpoint `POST /v1/credentials/github/test` (handler
  `_h_test_github`) and matching client wrapper
  `azt_collab_client.test_github_credentials()`. Hits
  `api.github.com/user` with the stored access token; on success
  also probes `api.github.com/user/installations` so the same Test
  button refreshes both `confirmed` and `app_installed` in one
  user gesture, matching the GitLab Test pattern.
- Auth helper `azt_collabd.auth.test_github_credentials(token)`
  added alongside the existing `test_gitlab_credentials` —
  consistent shape, same return dict (`{valid, server_username,
  app_installed, error}`).
- **`GitHubConnectScreen` is now state-aware.** Three shapes picked
  in `on_pre_enter` from `credentials_status['github']`:
  * not connected → device-flow box visible, manage hidden,
    `begin()` auto-fires.
  * connected, not confirmed → manage view (Test + Install GitHub
    App if not installed + Re-authenticate + Disconnect); device
    flow hidden; nothing auto-fires.
  * connected, confirmed → same controls plus a "(verified)"
    badge in the status line.
  Show/hide uses the Kivy hide/show pattern (height: 0, opacity: 0)
  per `~/.claude-sil/CLAUDE.md`. The screen is fully self-contained:
  Disconnect / Re-authenticate / Install-app no longer require the
  user to bounce back to settings.
- `SettingsScreen.connect_github()` reduced to a one-liner navigate.
  Auto-firing `begin()` on every entry to the screen is gone — the
  user with a token already on file isn't re-prompted for device
  flow; they get the manage view and pick Test or Re-authenticate
  themselves.
- Lock-step bump to 0.27.0 with cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.27.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.27.0. New wire endpoint
  + bundled-client peer APKs need the floor bump or version
  mismatches stay silent (ref. memory note on
  MIN_CLIENT_VERSION discipline).
- Translations (fr) for the new state-aware strings:
  "Re-authenticate", "Disconnect", "Install GitHub App",
  "Connected as {username} (verified).", the not-yet-tested and
  app-not-installed variants, "Token rejected by GitHub. Tap
  Re-authenticate.", "Could not open install page: {error}", and
  the "Opening {uri}\nWhen you finish on GitHub..." prompt.

### azt_collabd 0.26.0 + azt_collab_client 0.26.0 — in-app self-update
- New `azt_collab_client.ui.check_for_update(repo, current_version,
  asset_filename, on_status, ...)` reusable updater. Spawns a worker
  thread, polls `GET /repos/{repo}/releases/latest`, compares the
  release tag to the caller's `__version__` as a semver tuple, and on
  a newer release downloads the matching asset and dispatches
  `Intent.ACTION_VIEW` with the APK MIME type so Android's system
  installer takes over. All callbacks marshal back to the Kivy UI
  thread; non-Android hosts get a translated
  "APK install is only available on Android." through `on_error`.
- `REQUEST_INSTALL_PACKAGES` added to
  `server_apk/buildozer.spec → android.permissions`. The helper
  detects Android 8+ "Install unknown apps" gating via
  `PackageManager.canRequestPackageInstalls()` and routes the user to
  `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` on first use.
- `azt_collabd.configure(update_repo=...)` + `AZT_UPDATE_REPO` env var
  + `azt_collabd.config.update_repo()` accessor; default is
  `kent-rasmussen/azt-collab` (the canonical release feed at
  https://github.com/kent-rasmussen/azt-collab/releases/latest).
- "Update this app" button + status `BodyLabel` added to
  `<SettingsScreen>` directly under the existing "Share this app"
  row. Hosted by both `CollabUIApp.update_app` (standalone settings
  subprocess) and `PickerApp.update_app` (in-process settings reached
  from the picker's gear). Same KV `app.update_app()` resolves on
  whichever App owns the screen.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  peers (recorder targeting `kent-rasmussen/azt-recorder`, future
  viewer, …) can wire the same button into their own settings screens
  by passing their own `repo` / `__version__` / `asset_filename`.
- Translations added (fr): "Update this app", "Up to date.",
  "Checking for updates…", "Downloading {pct}%…",
  "Preparing install…", "Installing…",
  "APK install is only available on Android.", and the failure
  variants ("Update check failed: {error}", missing-tag /
  missing-asset / missing-URL detail strings, "Download failed",
  "Install failed", "Could not create download dir", and the
  Install-unknown-apps prompt).
- Lock-step bump to 0.26.0 across `azt_collabd` and `azt_collab_client`
  (no wire-format change; just keeping the cross-floors aligned now
  that the client gained shared UI surface peers will rely on).

### azt_collabd 0.25.2 — "Share this app" on the settings screen
- Added a Share-this-app row to `<SettingsScreen>` (`azt_collabd/ui/app.py`),
  positioned right under the Back NavBtn so it leads the scrollable
  body the same way the recorder's settings screen does. Hands the
  running server APK to Android's share sheet via the existing
  `azt_collab_client.ui.share_running_apk` helper — useful for
  onboarding teammates to the collab service. Hosted by both the
  standalone `python -m azt_collabd ui` (`CollabUIApp.share_apk`) and
  the in-process settings reached from the picker's gear
  (`PickerApp.share_apk`); the KV's `app.share_apk()` resolves on
  whichever App owns the screen at runtime.
- Icon (`share_dark.png`) sourced via `azt_collab_client.ui.icon_path`
  and threaded into the KV through `register_kv` next to the existing
  font-name substitution. Desktop hosts get the button too; tapping
  it surfaces the translated "APK sharing is only available on
  Android." message via the helper's `on_error` callback.
- French translations added for `Share this app`, `Share app`, `Error`,
  and the three `share_running_apk` failure messages
  (`APK sharing is only available on Android.`, the MediaStore-insert
  failure, the generic `Could not share APK:` wrapper).

### azt_collab_client 0.25.2 — public `ensure_mo` for peers
- `azt_collab_client.i18n.ensure_mo(locale_dir, domain, lang)` exposes
  the lazy `.po` → `.mo` compile path peers were previously missing.
  Peer i18n modules call it before `gettext.translation(...)` so they
  can ship `.po`-only and skip the external `msgfmt` build step the
  same way the client does. Writes the `.mo` next to the `.po`; on
  Android that's inside the APK's private filesDir, which is
  writable. See the *Internationalization (i18n)* section of
  `azt_collab_client/CLAUDE.md` for the integration recipe.
- `_ensure_mo(lang)` is now a thin wrapper around `ensure_mo` for the
  client's own domain — no behaviour change for the client itself.

### azt_collabd 0.25.1 + azt_collab_client 0.25.1 — French catalog catch-up
- Added 28 missing French translations to
  `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  covering the popup confirm-langcode flow, the clone-URL popup's
  inline `code: ` / `change code` / `OK` affordances, the daemon
  settings UI's publish row + GitLab Test screen, and the picker's
  empty-result fallback dialog. Catalog Project-Id-Version follows
  the package bump.
- Wrapped a stray ``'Open settings'`` literal in
  `azt_collabd/ui/picker_app.py:852` (clone-failure auth modal) with
  `_tr` so the translation already in the catalog actually fires.

### azt_collabd 0.25.0 + azt_collab_client 0.25.0 — synchronized release
- Lock-step version bump of both packages plus their cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.25.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.25.0. Intended to flush
  every peer APK through a rebuild so the cumulative work since the
  prior synchronization point lands in lockstep across the suite.
  The user-visible content of this release is the union of every
  entry below, plus the four-month gap of additive changes that
  preceded them. After this synchronization any peer running an
  older bundled client will surface `client_too_old` from
  `check_server_compat()` and any client talking to an older daemon
  will surface `server_too_old`, prompting an update on either side
  rather than silently degrading.
- Notable behaviour requiring lock-step:
  - `last_project` server-tracked via `/v1/recent/last_project`
    (older clients keep using their own sandbox, breaking on
    Android).
  - GitLab Test button drives `_h_test_gitlab` and the new per-host
    `confirmed` flag.
  - `Project.last_commit`, `ProjectStatus.commits_ahead` exposed on
    the wire; `_h_init_project` writes `last_sync` / `last_commit`
    back to projects.json on a successful publish.
  - `_resolve_path` reads `working_dir` from the registry; URIs
    decouple from on-disk dir naming.
  - `:provider` stdio bridge so daemon `print(..., file=sys.stderr)`
    actually reaches logcat.
  - dulwich `repo.refs[name]` (with `KeyError`) replaces the
    incorrect `.get()` call that was failing every sync silently;
    post-push remote-mirror update fixes `(+N)` indicator stickiness.

### azt_collabd 0.21.4 + azt_collab_client 0.24.0 — back from picker also returns to last project
- 0.21.3 only special-cased the BCP-47 langpicker; back from the
  *project picker screen itself* still hit the `if self.sm.current
  == 'picker': return False` early exit and let Android close the
  Activity with `RESULT_CANCELED`. The user's case from logcat —
  picker screen opens, back press, no `[picker_app]` trace at all,
  recorder receives an empty cancel — is exactly that path. New
  `_exit_to_last_project_or_cancel` helper centralises the
  emit-resumable-or-cancel shape, called from both the `picker` and
  `langpicker` branches of `_navigate_back`. Either exits the
  picker subprocess in one back-press, with the recorder receiving
  either the resumable project or a clean cancel.

### azt_collabd 0.21.3 + azt_collab_client 0.24.0 — back-from-langpicker always exits subprocess
- 0.21.1 added the langpicker→last_project special case but only
  when `last_project()` resolved; otherwise it fell through to the
  default `'picker'` target, which left the user on the project
  picker screen requiring a second back-press to actually exit.
  Now the langpicker back-press always exits the picker subprocess
  in one step: emit `last_project` if it resolves (recorder
  auto-resumes), or emit a clean cancel if it doesn't (recorder's
  `_handle_pick` silently returns to whatever it was showing).
  Either way, the user lands on the recorder, never on the project
  picker. New `_emit_cancel_and_quit` helper factors the
  `setResult(RESULT_CANCELED) + finish` shape out of
  `on_request_close` so it's reachable from anywhere.

### azt_collabd 0.21.2 + azt_collab_client 0.24.0 — URI resolution decoupled from on-disk dir name
- **Cloned project's LIFT URI returned `FileNotFoundException`.**
  `_resolve_path` in the ContentProvider was building
  `$AZT_HOME/projects/<langcode>/...` from the URI's first segment,
  but the picker_app's clone worker uses the URL's repo basename for
  `dest_dir` (e.g. `en_Demo.git` → `projects/en_Demo/`), while the
  URI handed back to peers uses the user-chosen langcode (e.g.
  `content://.../en/SILCAWL.lift`). Mismatch → resolver looked under
  the wrong directory → file-not-found → recorder's "lift namespace
  scan failed: forbidden: /en/SILCAWL.lift" log line. `_resolve_path`
  now consults `projects.get(langcode).working_dir` so URI resolution
  is independent of how the on-disk directory was named at clone
  time. Falls back to the legacy `<projects>/<langcode>` path when
  the project isn't in the registry, preserving pre-registry URIs.

### azt_collabd 0.21.1 + azt_collab_client 0.24.0 — back-from-langpicker resumes last project
- `_navigate_back` in `picker_app.py` now special-cases the
  `langpicker` screen: a user who reached Start-New and then changed
  their mind almost always wants to return to whatever project they
  had open before, not be re-parked at the project list. When
  `last_project()` resolves to a live registered project, back from
  `langpicker` emits that project's path and exits the picker
  subprocess (recorder auto-resumes). Cold-start fallback (no
  recorded last project) still goes to the picker screen so the
  user has somewhere to land.

### azt_collabd 0.21.0 + azt_collab_client 0.24.0 — sync was failing every cycle; remote-mirror not bumped after push
- **Sync was raising `AttributeError` on every cycle.** Pre-0.21.0
  `_sync_repo_locked` did `repo.refs.get(branch_ref) or repo.head()`,
  but `dulwich.refs.DiskRefsContainer` has no `.get()` method — only
  `__getitem__` (raises `KeyError`) and `read_ref()`. The call
  raised `AttributeError` post-fetch on every sync, propagated to
  `scheduler._fire`'s catch-all, and the job was marked
  `PUSH_FAILED`. Symptom: commits piled up locally but never
  pushed; "I see Audio recordings by Recorder commits showing up on
  GitHub minutes apart" was the queue draining whenever a build
  with the diagnostic try/except (added in 0.20.9) happened to be
  running. Replaced with a small `_read_ref(name)` helper that
  uses `__getitem__` + `KeyError`. Both `branch_ref` and
  `remote_ref` reads, plus the retry-loop's post-fetch read, now
  use it.
- **`commits_ahead` stuck at `(+N)` after a successful push.**
  Symptom: `[sync] done: codes=['NOTHING_TO_COMMIT', 'PUSHED']` ran
  cleanly but the recorder's indicator kept showing `(+10)`. Cause:
  `porcelain.push` advances the remote on GitHub but doesn't update
  the *local mirror* `refs/remotes/origin/<branch>` —
  `_count_commits_ahead` then compares the just-pushed `local_sha`
  against the pre-push mirror and reports the count of just-pushed
  commits as still-pending. Bumping the mirror explicitly after a
  successful push (`repo.refs[remote_ref] = local_sha`) reflects
  what CLI `git push` does and lets `commits_ahead` read 0 on the
  next status poll.

### azt_collabd 0.20.9 + azt_collab_client 0.24.0 — sync-hang trace, post-fetch ref reads
- The 0.20.8 trace narrowed the hang to the `local_sha = repo.refs.get(...)
  or repo.head()` line, so 0.20.9 splits that into separate
  `[sync-trace]` prints around `repo.refs.get(branch_ref)`,
  `repo.head()`, and `repo.refs.get(remote_ref)`. Whichever call
  doesn't produce its trailing print is where dulwich is wedging.
  Each is wrapped in try/except to surface a raise vs a hang
  cleanly.

### azt_collabd 0.20.8 + azt_collab_client 0.24.0 — sync-hang trace
- `[sync-trace]` prints between every step in `_sync_repo_locked`
  after the fetch (`fetch begin/done`, `local_sha`, `remote_sha`,
  `needs_merge`, `fast-forward`/`local ahead`/`merge_diverged
  begin/done`, `push loop begin`, `push attempt`, `push done`/`push
  raised`). Pinpoints exactly which step wedges between fetch
  returning and the function exiting — your latest log shows
  `fetch` returning HTTP 200 followed by silence until the next
  status-poll, so the hang is somewhere between `local_sha = ...`
  and the `push` HTTPS call.

### azt_collabd 0.20.7 + azt_collab_client 0.24.0 — last_sync on publish + `:provider` stdio bridge
- **Publish stamps `last_sync` / `last_commit`.** `_h_init_project`
  ran the publish (commit + push) and updated `remote_url` in
  `projects.json`, but did **not** stamp `last_sync` from the result
  codes. Sister handlers in `scheduler._run_sync` and
  `_h_project_sync` already did. Symptom: a successful publish left
  `Project.last_sync == 0`, the recorder's UI read that as "never
  synced" and kept showing the "data isn't being backed up" warning
  forever — even though the repo was sitting on github.com fully
  pushed. After this fix, a publish that returns `PUSHED` (or
  `COMMITTED_AND_PUSHED`) immediately stamps `last_sync` so the
  indicator flips on the next refresh.
- **Daemon prints now reach logcat.** `server_apk/service.py` calls
  a new `_bridge_stdio_to_logcat()` at module top that pipes
  `sys.stdout` / `sys.stderr` through `android.util.Log` under tag
  `python`. p4a auto-redirects stdio for `PythonActivity` (the
  Activity process where the picker / settings UI / recorder Kivy
  apps run), but not for `PythonService` — and the daemon
  (server.py, scheduler.py, repo.py) lives in the `:provider`
  process driven by PythonService. Pre-0.20.6 every `print` from
  daemon code went to a black hole, so functionally correct sync
  flows (RPC returns a job_id, the timer fires, the repo gets
  pushed) appeared in logcat as if nothing happened. After this fix
  the `[sync-async]` / `[sync-debounce]` / `[sync-fire]` / `[sync]`
  traces actually show up.

### azt_collabd 0.20.5 + azt_collab_client 0.24.0 — picker failure → notify + exit
- After `_pick_project_android`'s one-retry loop exhausts on
  `unexpected_cancel`, the client now schedules a Kivy modal on the
  UI thread: "The project picker failed to return a result. The app
  will now close — please reopen it." with a single OK button that
  calls `App.get_running_app().stop()` (and `os._exit(0)` as a belt-
  and-braces). Lives in the client so every peer using
  `pick_project()` gets the same fallback without per-host wiring;
  documents in the function docstring that `unexpected_cancel` is
  now a terminal status from the caller's perspective (the user
  will see the modal and exit before the caller observes the
  return value). Triggered by the user reporting empty-recorder
  windows three times in one debug session.

### azt_collabd 0.20.5 + azt_collab_client 0.23.6 — client-side request_sync trace
- `[sync-client] request_sync(<lang>, ...)` printed at every entry,
  with success/failure/transport branches. Closes the visibility gap
  between "the recorder called the RPC" and "the daemon received it"
  — if the client log is silent, the recorder never tried; if the
  client log shows a send but the daemon's `[sync-async]` is silent,
  the RPC is being dropped at the transport layer.

### azt_collabd 0.20.5 + azt_collab_client 0.23.5 — picker auto-retry on RESULT_CANCELED-with-data
- `_pick_project_android` now wraps the launch+wait in a one-retry
  loop. The picker contract is RESULT_OK→data /
  RESULT_CANCELED→no-data; the combo "non-OK + data attached"
  shouldn't be reachable normally (Android can synthesize it on
  back-press during `setResult`, or with OEM launcher tampering).
  Pre-0.23.5 we'd silently swallow that case as `'cancelled'` and
  drop the user on a recorder window with no project. Now the
  client classifies it as `'unexpected_cancel'` and re-launches the
  picker once, so the user gets another shot at choosing.
- The retry-loop refactor extracts the inner launch/wait logic into
  `_pick_project_android_once`; `attempt=` is included in the
  `[pick_project] _on_result: ...` trace so each invocation is
  identifiable in logcat.

### azt_collabd 0.20.5 + azt_collab_client 0.23.4 — full sync chain trace
- Three new diagnostics so a request → debounce → fire → run path is
  visible end-to-end in logcat: `[sync-async] <lang>` at RPC arrival
  in `_h_project_sync_async`, `[sync-debounce] <lang>` from
  `scheduler.request_sync` (so we see whether the recorder's
  `_auto_commit_sync` reached the queue), and `[sync-fire] <lang>`
  from `_fire` (so we see whether the debounce timer actually fired
  before the daemon process got recycled).

### azt_collabd 0.20.4 + azt_collab_client 0.23.4 — empty-registry disk scan
- When `_h_list_projects` returns zero entries, also print what
  `$AZT_HOME/projects/` actually contains on disk. Distinguishes
  "registry wiped but working trees survived" (recoverable: a
  future endpoint can scan + auto-register) from "filesDir gone"
  (server APK clean-installed; nothing to recover).

### azt_collabd 0.20.3 + azt_collab_client 0.23.4 — recent-state stderr trace
- `azt_collab_client/CLAUDE.md` rule #2 expanded to cover the
  *gating* failure mode (peers silently skip auto-sync on Android
  because their local filesystem check returns False) and to
  include the verbatim fix-shape snippet for `_project_has_remote`,
  so future Claude sessions touching a peer don't have to re-derive
  the daemon-served replacement.

### azt_collabd 0.20.3 + azt_collab_client 0.23.3 — recent-state stderr trace
- Diagnostic prints around every read/write of `last_langcode`:
  daemon-side `[recent] _touch_project(...) → /path/to/config.json`,
  `[recent] GET /v1/recent/last_project → 'lang' (from /path/...)`,
  and matching `POST` line; client-side
  `[recent] last_project → 'lang'` / `set_last_project(...) sent` /
  `ServerUnavailable: ...`. Pairs the path being written with the
  path being read so a divergent-`$AZT_HOME` bug shows up in logcat
  side-by-side instead of having to be inferred. No behaviour change.

### azt_collabd 0.20.2 + azt_collab_client 0.23.2 — MIN_CLIENT_VERSION floor for the recent.py RPC migration
- **`MIN_CLIENT_VERSION` raised to 0.23.0.** Pre-0.23 clients keep
  reading `$AZT_HOME/config.json::recent.last_langcode` from their
  own package's filesDir, which on Android sits in a different
  sandbox from the daemon's. The daemon stamps last_project on
  every langcode-bound RPC, but the peer's bundled client never sees
  it — recorder auto-resume falls through to the picker on every
  restart. Bumping the floor makes `check_server_compat()` return
  `client_too_old` instead of silently degrading, so the recorder
  surfaces the "please update" warning. Saved-memory note
  `feedback_min_client_version.md` documents this exact failure mode.

### azt_collabd 0.20.1 + azt_collab_client 0.23.2 — publish-row sticking after success
- **`_h_init_project` writes remote_url back to `projects.json`.**
  Symptom: a publish that returned `PUSHED` left the project's
  `Project.remote_url` empty in the registry, so the settings UI
  immediately re-rendered the publish row asking the user to publish
  again. `_init_repo` updates the *local* git config but the
  registry is a separate datastore; the back-write was missing. Now
  the daemon walks `projects.list_all()`, finds the entry whose
  `working_dir` matches, and writes the URL via
  `projects.set_remote_url`.
- **`_pick_publish_candidate` consults the live git remote.** Even
  with the back-write fix, projects published before 0.20.1 carry
  an empty cached `remote_url`. The settings UI now also reads the
  authoritative value via `project_status(langcode).remote_url`
  (which checks `.git/config`); the row hides if either source
  reports a remote. Defensive belt-and-braces so existing published
  repos behave correctly without a manual reconcile.

### azt_collabd 0.20.0 + azt_collab_client 0.23.1 — `commits_ahead` on ProjectStatus
- **`commits_ahead: int` on `ProjectStatus`.** Filed by recorder
  1.37.6 in `NOTES_TO_DAEMON.md`: the recorder's sync indicator
  needs the count of local commits not yet pushed to the remote so
  it can render `(+n)` instead of an opaque `*` marker.
  `repo_status_summary` now returns a 4-tuple
  `(branch, remote_url, n_changes, commits_ahead)`; `_h_project_status`
  forwards `commits_ahead` on the wire. Computed locally from
  `refs/heads/<branch>` vs. `refs/remotes/origin/<branch>` (no
  network round-trip), so a stale cache may under-report — the
  recorder's UX contract is "OK on uncertainty," so under-reporting
  is the right failure mode. Returns 0 whenever the local cache
  doesn't have a remote ref to compare against (no remote
  configured / never pushed). Client dataclass already had the
  field with `default 0` for forward-compat. NOTES_TO_DAEMON entry
  cleared.

### azt_collabd 0.19.1 + azt_collab_client 0.23.1 — server-canonical recent state + last_commit
- **`azt_collab_client/CLAUDE.md` rule #2 added** — "no reading
  project state from the local filesystem either," with the
  recorder's `_project_has_remote()` (dulwich.Repo on the working
  dir) called out as the canonical anti-pattern. Reads silently work
  on desktop and silently fail on Android because the daemon's
  working_dir lives in the server APK's private filesDir. Future
  peers must use `project_status(langcode)` for state-shaped checks.
- **Publish outcome message no longer clobbered by refresh.**
  `_publish_done` was setting the message *then* calling `refresh()`,
  which started by clearing `msg.text`, so the user only saw
  "Publishing..." and never the result. Reorder: refresh first, then
  set the outcome. Re-enables the button on failure.
- **`[publish]` stderr trace** in `_publish_worker`: prints the
  arguments going into `init_project` and the resulting `Result.codes()`
  (or the exception). Pairs with the `[sync-rpc]` / `[sync]` traces
  added in 0.18.2 so a publish failure has a logcat trail.

### azt_collabd 0.19.0 + azt_collab_client 0.23.0 — server-canonical recent state + last_commit
- **`last_project` is now server-tracked.** Was: each peer wrote
  `$AZT_HOME/config.json::recent.last_langcode` directly, which broke
  on Android where every peer's sandbox holds its own config.json
  (the recorder's write and the settings-UI subprocess's read landed
  in different files), and broke on desktop whenever a load path
  forgot to call `set_last_project`. Now: every langcode-bound RPC
  (`open_project`, `project_status`, `sync`, `sync_async`, `register`,
  `init`, `clone`, `from_template`, `rename`) auto-stamps via the new
  `server._touch_project` helper, and `last_project()` /
  `set_last_project()` are thin wrappers around new endpoints
  `GET`/`POST /v1/recent/last_project`. Single source of truth across
  peers and platforms; peers don't have to remember to call
  `set_last_project` from any specific load path.
- **Publish picker simplified.** With server-canonical
  `last_project`, the unpublished-projects-preference fallback in
  `SettingsScreen._pick_publish_candidate` (added in 0.18.1 to work
  around stale recorder-written state) is gone. The settings UI now
  resolves `last_project()` straight to the candidate Project; if
  that doesn't return a live project, the publish row hides — which
  is the correct UX, because nothing has been touched.
- **`Project.last_commit` field, separate from `last_sync`.** Filed
  by the recorder team in `azt_collab_client/NOTES_TO_DAEMON.md`:
  peer sync indicators couldn't distinguish "committed locally but
  not pushed" from "silently broken" because `last_sync` only
  stamped on `PUSHED` / `PULLED` / `COMMITTED_AND_PUSHED`. Daemon
  now also stamps `last_commit` on `COMMITTED_LOCAL` /
  `COMMITTED_NO_REMOTE` / `COMMITTED_AND_PUSHED` (any path where a
  commit object hit the working tree). Both fields ride on
  `Project` and `ProjectStatus`; pre-0.19 daemons that don't emit
  `last_commit` get a 0.0 default in the client dataclass for
  forward-compat. `NOTES_TO_DAEMON.md` entry deleted per its own
  instructions.
- **Sync trace lines** retained from 0.18.2: `[sync]` lines from
  `scheduler._run_sync` and `[sync-rpc]` from `_h_project_sync` so
  successful syncs show up in `adb logcat -s python`.

### azt_collabd 0.18.2 + azt_collab_client 0.22.1 — settings UX cleanup
- Sync trace lines on stderr (visible via `adb logcat -s python` on
  Android): `[sync] <lang> ... starting` / `... done: codes=[...]` from
  `scheduler._run_sync` and `[sync-rpc] ...` from `_h_project_sync`.
  Previously a successful sync emitted nothing — the structured
  `Result` carried the outcome but there was no trail in logcat to
  confirm the daemon had even seen the request.
- Publish-candidate fallback also prefers unpublished projects (the
  filtered `list_projects()` search would otherwise pick a
  more-recently-synced sibling that was already published, hiding
  the publish row even though a sibling project still needed
  publishing).
- Publish candidate falls back from `last_project()` to the
  highest-`last_sync` entry in `list_projects()` when the suite-wide
  "last opened" key is empty (older recorder load paths don't always
  write it). Diagnostic stderr lines from `_pick_publish_candidate`
  surface why the row stayed hidden.


- **GitLab "Connect" + Test button.** The settings screen's GitLab
  affordance is now labelled "Connect to GitLab" (was "Set GitLab
  credentials") to match GitHub's wording. The form screen replaces
  the bare "Save" button with a single "Test connection" button: the
  daemon-side `_h_test_gitlab` endpoint runs a live check against
  `gitlab.com/api/v4/user`, and only on success does it persist the
  credentials and stamp `gitlab.confirmed=True` in the store, so the
  user can't end up with a stored bad token. New endpoint
  `POST /v1/credentials/gitlab/test` (falls back to stored creds if
  body fields are empty) and client wrapper
  `test_gitlab_credentials(username, token)`.
- **Per-host `confirmed` flag.** `get_credentials_status()` now
  reports `github.confirmed` (derived: `connected AND app_installed`)
  and `gitlab.confirmed` (persisted; cleared on save, set on a
  successful Test). There is no longer a single "active host" — both
  hosts can be confirmed independently, and consumers (publish flow
  below, future sync flows) pick one based on context.
- **"Publish &lt;langcode&gt; data" button on the settings screen.**
  Visible only when `last_project()` resolves to a langcode whose
  project doesn't already have a remote; enabled when at least one
  host is `confirmed`. On click, single-confirmed hosts publish
  directly via `init_project(working_dir, remote_url, ...)`; both
  confirmed surfaces a small overlay so the user picks GitHub or
  GitLab. Mirrors the recorder's `do_publish` flow but moves it into
  the daemon UI, so any peer that exposes the gear can publish
  without owning the publish UI.
- **GitHub device flow no longer auto-fires on screen rebuild.**
  `GitHubConnectScreen.on_pre_enter` previously kicked the device
  flow on every entry, which meant a language-change rebuild — which
  clears + re-adds every screen — re-launched device flow even though
  the user was nowhere near the GitHub screen. The auto-start now
  lives on the explicit "Connect to GitHub" button via the new
  `SettingsScreen.connect_github()`, so language changes (and any
  other rebuild) leave the GitHub screen quiet.

### azt_collabd 0.16.0 + azt_collab_client 0.20.0 — sticky-bound server APK service + persistent scheduler jobs
- **Server APK lifetime fix.** The picker Activity now leaves the
  Python process running on Android instead of calling `App.stop()` /
  `sys.exit()`. A new sticky-bound service
  (`AZTServiceProviderhost`, `android/src/main/java/.../`) pins the
  host so `AZTCollabProvider.openFileDescriptor` can still serve the
  URI grant the picker just emitted. Pre-0.16.0 the server APK
  process exited as soon as the picker Activity finished, taking the
  provider with it and triggering Android's "depends on provider in
  dying proc" cascade SIGKILL of any peer that had received a
  `content://` URI from the picker.
- Service is sticky-bound (no foreground notification): peers get
  the bind-priority OOM hint while they're using the provider, and
  `START_STICKY` asks Android to recreate the service after a
  memory-pressure kill. Idle-stop policy (5 min of zero peers bound
  + zero provider activity) tears the service down so the design's
  "transient when idle, pinned while in use" intent is preserved.
  Manifest entry injected by `_inject_aztcollab_service` in
  `p4a_hook.py`, gated on `dist_name == 'aztcollab'`.
- **Scheduler jobs persisted to `$AZT_HOME/jobs.json`** so peer
  `poll_job(job_id)` calls survive a daemon respawn. `_store_job` and
  `_fire` write atomically on every state transition. New
  `scheduler.reconcile_on_startup()` runs from the loopback HTTP
  daemon entry (`server.run`) and the Android service entry
  (`server_apk/service.py`); marks any `PENDING` / `RUNNING` jobs
  found at startup as `DONE` + `JOB_INTERRUPTED` because their
  worker threads died with the previous process. Old `DONE`
  entries are GC'd past 1h at the same pass.
- New status code `JOB_INTERRUPTED` (`azt_collabd/status.py` and
  `azt_collab_client/status.py`, mirror) plus translation in
  `azt_collab_client/translate.py`. Peers should treat it identically
  to `SERVER_UNAVAILABLE`: transient, retryable.
- `MIN_CLIENT_VERSION` bumped to 0.20.0 — pre-0.20 clients don't have
  the `JOB_INTERRUPTED` translation and would surface the raw
  uppercase code in their UI.
- `MIN_SERVER_VERSION` bumped to 0.16.0 — pre-0.16 daemons don't
  persist jobs.json, so `poll_job` returns None for any job_id whose
  daemon has been respawned, indistinguishable from "never existed."
- Activity tracking added to `azt_collabd/android_cp/service.py`:
  `touch()`, `seconds_since_last_touch()`, `bound_client_count()`
  used by the service idle-stop loop. Every dispatch / openFile call
  bumps the touch timestamp.
- Picker app gains `on_pause` returning True so Kivy doesn't fight
  the missing GL surface after the Activity finishes.
- New `server_apk/test_install.py` (sibling of the existing adb-driven
  `test_install.sh`): 8-section desktop integration test for the
  kill-recovery flow — auto-spawn detection, jobs.json persistence,
  reconcile_on_startup, JOB_INTERRUPTED end-to-end. Run from the
  azt-collab repo root: `python server_apk/test_install.py`.

### azt_collab_client 0.19.2 — pick_project unbinds its activity-result handler after each call
- ``pick_project()`` registered a closure on
  ``android_activity.bind(on_activity_result=…)`` and never
  unbound it, so each invocation in a host session left a dangling
  handler that fired on every subsequent activity result. Logs
  showed N copies of ``[pick_project] _on_result …`` after N
  picks. Each closure wrote to its own long-since-stale
  ``holder`` so behaviour was correct for the most recent caller,
  but the JNI cost grew linearly with picks.
- New ``_unbind_handler`` helper called from inside ``_on_result``
  after ``done.set()`` (single-shot pattern) and from the timeout
  path so a much-later activity result for our request code can't
  write to a stale holder. Tracks ``bind_state['bound']`` to avoid
  unbinding a never-bound handler when ``_setup_on_ui`` failed
  early. Tolerates older Kivy / python-for-android versions that
  exposed ``bind`` without ``unbind``.

### azt_collab_client 0.19.1 — fix vanishing project list: defer ProjectPickerScreen.on_enter populate by one frame
- ``projects.json`` had the cloned project, the daemon's
  ``_h_list_projects`` would have returned it — but the picker
  never asked. ``ProjectPickerScreen.on_enter`` called
  ``_populate_projects`` synchronously, and Kivy >= 2.3 fires
  ``on_enter`` before KV-defined ids attach on the first screen
  entry. So ``self.ids.get('project_list')`` returned None, the
  populate function bailed silently, and the existing-projects
  list rendered empty — "cloned projects don't show up on next
  open". Same race the settings UI already worked around with
  ``Clock.schedule_once``; applied the same fix here.
- Added two diagnostic prints inside ``_populate_projects`` so
  any future bail (still-no-id even after the defer, or missing
  host ``list_projects`` method) surfaces in logcat instead of
  manifesting as a silent empty list.

### azt_collab_client 0.19.0 — suite-wide last-opened-project state (`recent.last_project`)
- New ``azt_collab_client.recent`` module with ``last_project()`` and
  ``set_last_project(langcode)``, persisted to
  ``$AZT_HOME/config.json`` under ``recent.last_langcode``. Re-exported
  from the package root.
- Same store as ``i18n``'s ``ui.language`` — no daemon RPC, just a
  file the client reads/writes; peers converge through the shared
  config without an explicit coordination channel. Recorder writes
  the langcode after every successful pick; the next peer launch
  (recorder, viewer, future apps) reads it at startup and lands on
  the same project.
- Resolve langcode → current path/URI via the existing
  ``open_project(langcode)`` (returns the daemon's authoritative
  ``Project`` record). ``recent.last_project()`` deliberately returns
  just the langcode, not a path — paths/URIs can shift across syncs;
  the langcode is stable.
- The "one store — suite-wide prefs AND state" rule generalises: the
  recorder's ``prefs['last_lift']`` was the second peer-private
  cache to fall under the rule (after ``prefs['ui_language']`` in
  0.16.0). Future cross-peer signals (last entry within project,
  contributor name) follow the same model.

### azt_collabd 0.14.5 — list_projects path/count diagnostic
- ``_h_list_projects`` now prints the resolved
  ``projects.json`` path it just read from plus the count and
  langcodes returned. Combined with the 0.14.4
  ``clone registered langcode=… → <path>`` print on the write
  side, the two log lines pin down whether the
  vanishing-projects bug is a write-vs-read path mismatch
  (different ``$AZT_HOME`` resolution between the two call
  sites) or a write-failed-silently issue. The actual on-disk
  content can be verified with ``adb shell run-as
  org.atoznback.aztcollab cat files/azt/projects.json``.

### azt_collabd 0.14.4 — inline langcode preview in clone popup; diagnostic prints for missing-after-relaunch projects
- Clone-URL popup gained an inline ``code: <derived>`` readout
  with an inline **change code** button right above the URL
  field. The readout updates live as the user types the URL;
  no separate confirmation step. Tapping **change code** swaps
  the readout for a small editable field — once the user takes
  control there's no auto-revert.
- Open-file flow uses the LIFT-filename-stem-derived langcode
  silently (no popup); user can rename later through whatever
  rename affordance lands.
- Diagnostic prints added in two spots so the user-reported
  "previously cloned projects don't show up on next open"
  actually points somewhere:
  - ``_clone_worker`` after ``projects.register``, prints the
    langcode and the resolved ``projects.json`` path. If the
    print appears, the registry write thinks it succeeded.
  - ``picker_app.list_projects`` (host method) prints how many
    projects came back from the daemon and which langcodes.
    If this prints 0 right after a successful clone, persistence
    or path-resolution is the issue (likely an ``$AZT_HOME``
    that differs between the clone-time process and the picker
    relaunch).

### azt_collabd 0.14.3 — clone accepts user-chosen langcode on input; rename_project endpoint
- ``POST /v1/projects/clone`` body gains optional ``langcode``;
  the picker collects an explicit value via the
  ``confirm_langcode_popup`` (client 0.18.2) before kicking the
  clone, so the project lands in ``projects.json`` keyed on the
  user's choice from the moment the daemon first sees it. No
  rename-after-the-fact in the registry. Empty ``langcode`` falls
  back to the daemon's auto-derivation from the LIFT filename /
  repo URL — matches the legacy desktop / scripted-call shape.
- ``_clone_worker`` gained an ``override_langcode`` kwarg; the
  registration step prefers it over ``derive_langcode``.
- New ``POST /v1/projects/<langcode>/rename`` endpoint
  (``_h_rename_project``) and ``projects.rename(old, new)``
  helper. Not used by the picker (which sets-on-create instead),
  but exposed for future flows that might let the user re-key a
  project after the fact (e.g., a settings-screen "rename
  project" affordance).

### azt_collabd 0.14.2 — clone job response carries canonical langcode
- Closes the azt-viewer 0.5.1 TODO ("picker should emit canonical
  langcode, not just leave the URI to be parsed"). The clone job
  response now includes ``langcode`` alongside ``lift_path`` —
  the same value the daemon just keyed the projects.json entry
  with on auto-register. ``_clone_worker`` captures it, the
  ``DONE`` job-state stash records it, ``_h_clone_status``
  passes it through.
- Source-of-truth chain (none of these need to derive from the
  URI on the peer side any more):
  - clone → daemon ``projects.derive_langcode`` → clone job →
    client ``clone_project`` returns ``langcode`` → picker stamps
    Intent extra.
  - open-file → ``register_project`` returns ``Project.langcode``
    → picker stamps Intent extra.
  - existing-project tap → ``picker.py`` populates the button
    with ``btn.langcode = projects_list_entry.langcode`` →
    ``load_lift(path, langcode)`` → picker stamps Intent extra.
  - template flow → user-typed BCP-47 (``_pending_vernlang``)
    already stamps the Intent extra.
- Backward-compatible. Old clients ignore the extra ``langcode``
  field on ``_h_clone_status`` responses; new clients hitting old
  daemons see ``langcode == ''`` and the peer's URI-parse
  fallback (defence-in-depth) still kicks in.

### azt_collab_client 0.18.3 — clone popup: only one input field active at a time
- ``clone_url_popup`` reworked so the URL field and the code field
  are mutually exclusive — only one is enabled at any moment, so
  the on-screen keyboard never argues with itself between two
  text inputs.
  - Mode A (default): code-preview row shows ``code: <derived> [change code]``;
    URL field is the active input.
  - Mode B (after tapping **change code**): code-preview row swaps
    in an editable code field with an ``[OK]`` button; the URL
    field is set to ``disabled=True`` (grays out, displays the
    typed URL, no input focus).
  - Tapping **OK** commits the typed code, re-enables the URL
    field, and swaps Mode A back in. Empty typed code clears the
    override so URL→code syncing resumes; non-empty pins the
    user's value through subsequent URL edits.
- Submit (Clone) works in either mode: takes the live code input
  in Mode B, the saved override in Mode A (post-OK), or the
  current URL-derived value otherwise. URL still required.

### azt_collab_client 0.18.2 — confirm-langcode popup: set on creation, not afterwards
- New ``ui.popups.confirm_langcode_popup(initial, on_submit)``:
  shows the auto-derived langcode in an editable field, asks the
  user to confirm or correct it. ``on_submit(chosen)`` fires on
  Confirm (and on Cancel with the original ``initial``, so the
  flow always resolves). Re-exported from ``azt_collab_client.ui``.
- ``picker_app.clone_dialog`` now derives a tentative langcode
  from the URL repo basename, runs ``confirm_langcode_popup``
  immediately after the URL submit, and only kicks the clone
  once the user confirms — passes the chosen value to
  ``clone_project(url, dest, langcode=chosen)``. The daemon
  registers under that exact key (no post-hoc rename). Helper
  ``_tentative_langcode_from_url`` mirrors the daemon's
  derivation order without requiring a filesystem path.
- ``picker_app.open_file._on_chosen`` runs
  ``confirm_langcode_popup`` after the file is picked but
  before ``register_project``; the chosen value goes to the
  registration call directly. Helper
  ``_tentative_langcode_from_lift`` strips the ``.lift``
  extension off the basename for the prefill.
- ``clone_project()`` / ``clone_project_start()`` accept an
  optional ``langcode=''`` kwarg routed into the request body;
  empty preserves the legacy auto-derivation behaviour for
  desktop scripted callers.
- New ``rename_project(old_langcode, new_langcode)`` wrapper for
  the daemon's ``/v1/projects/<langcode>/rename`` endpoint.
  Currently unused by the picker (set-on-create supersedes it),
  but exposed in ``__all__`` for peer apps that want to surface
  a "rename this project" affordance later.

### azt_collab_client 0.18.1 — clone_project carries langcode; project-list buttons stash langcode for load_lift
- ``clone_project()`` now returns ``langcode`` alongside
  ``lift_path`` / ``result`` / ``error`` on the success branch
  (DONE state). Daemon-side companion in 0.14.2.
- ``picker.py`` existing-project list now stores the project's
  canonical langcode on each button at populate time (the
  ``name`` half of the host's ``list_projects()`` tuple is already
  the langcode by contract) and passes it through on tap:
  ``app.load_lift(b.lift_path, getattr(b, 'langcode', ''))``. Host
  ``load_lift`` signature gains an optional ``langcode``
  parameter; default keeps existing single-arg callers working.

### azt_collab_client 0.18.0 — MediaHandle + audio_uri_for / image_uri_for
- New ``audio_uri_for(lift_path_or_uri, basename)`` and
  ``image_uri_for(lift_path_or_uri, basename)`` composer helpers.
  Given the picker-emitted LIFT path/URI plus a basename, return
  the sibling resource's URI (on Android-content URIs) or
  filesystem path (desktop) — so callers stay agnostic about the
  path/URI distinction. URI form composes
  ``content://<auth>/<lang>/{audio|images}/<basename>``, mirroring
  the daemon's ``_resolve_path`` whitelist. Filesystem form is
  ``os.path.join(os.path.dirname(lift_path), {audio|images}, basename)``.
- New ``MediaHandle(path_or_uri, kind='audio'|'image')`` —
  ``LiftHandle`` subclass with a ``kind`` for log lines / error
  messages, and a write-mode gate: ``open_write()`` on
  ``kind='image'`` raises ``PermissionError`` (images are
  read-only from peers; the daemon owns image additions).
- Re-exported from the package root: ``from azt_collab_client
  import MediaHandle, audio_uri_for, image_uri_for``.
- Together with ``LiftHandle`` (0.17.0), this is the full Tier 3
  cross-package toolkit the recorder migration documented in
  ``CLAUDE.md`` needs to land audio recording and image rendering
  on the new Android server-APK model.

### azt_collab_client 0.17.1 — client-first asset model: new icon_path helper, gear bundled
- New ``azt_collab_client.ui.icons`` module with public
  ``icon_path(name)`` — returns the absolute path to a bundled icon
  under ``azt_collab_client/ui/assets/icons/<name>.png`` (canonical
  location), falling back to ``assets/<name>.png`` for the legacy
  flat layout where ``gear.png`` currently lives. Returns ``''`` if
  the asset isn't bundled. Re-exported from
  ``azt_collab_client.ui.icon_path``.
- ``picker.py`` now resolves its default gear icon through
  ``icon_path('gear')`` (was a private ``_BUNDLED_GEAR`` constant
  with the same effect) so the discovery seam is reused for the
  next batch of shared icons.
- ``CLAUDE.md`` UI section documents the **client-first asset model**:
  shared-shape assets (gear, sync, share, future back/close glyphs)
  default to ``azt_collab_client/ui/assets/icons/`` so sister apps
  inherit them for free; peer-specific icons (recorder microphone /
  redo / app-icon variants) stay in the peer; existing recorder KV
  references with relative ``icons/<name>.png`` paths still work in
  the recorder's own cwd and don't need migrating.
- **Asset migration done:** ``sync_dark`` / ``sync_light`` /
  ``share_dark`` / ``share_light`` / ``gear_dark`` / ``gear_light``
  copied from ``azt_recorder/icons/`` into
  ``azt_collab_client/ui/assets/icons/``. ``icon_path('sync_dark')``
  etc. now resolve to the bundled package paths; the next sister-app
  peer (viewer, future clients) gets them with zero per-app work.
  The recorder's existing relative ``icons/<name>.png`` references
  still resolve in its own cwd, so this change is non-breaking;
  recorder-side migration to ``icon_path()`` can be opportunistic.

### azt_collab_client 0.17.0 — LiftHandle: cross-package LIFT-file access for peer apps
- New ``azt_collab_client.lift_io`` module exporting ``LiftHandle``
  and ``is_content_uri``. ``LiftHandle(path_or_uri).open_read()`` /
  ``.open_write()`` returns a binary file-like usable with
  ``ElementTree.parse`` / ``ElementTree.write`` regardless of
  whether the picker's emitted ``path`` is a filesystem path
  (desktop) or a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI (Android, new model). On the URI branch, opens via
  ``ContentResolver.openFileDescriptor`` and ``os.fdopen`` on the
  detached FD; the file owns the FD lifetime (close-on-exit
  through the context-manager protocol).
- Re-exported from the package root: ``from azt_collab_client import
  LiftHandle, is_content_uri``. Added to ``__all__``.
- **No caching layer** — every read/write hits the daemon's
  canonical copy through the provider. Lost-update protection
  relies on the daemon's serialization. The new
  ``azt_collab_client/CLAUDE.md`` "LIFT-file access" section
  spells out the migration checklist for peers (recorder first,
  viewer next): replace every ``open(lift_path)`` with
  ``LiftHandle(p).open_read() / .open_write()``; do not introduce
  a peer-side cache; do not compute sibling paths via
  ``os.path.dirname`` on a URI. Also documents the patterns NOT
  to use.

### azt_collab_client 0.16.0 — single source of truth for UI language; new public display_name + scan_catalog_languages
- ``set_language(lang)`` no longer takes a ``persist`` keyword argument.
  There is no transient mode any more — one preference, one store
  (``$AZT_HOME/config.json :: ui.language``), sticks everywhere until
  the next change. Internal apply-without-persist behaviour is now a
  private ``_apply(lang)`` used only by the auto-init-on-import path.
  Breaking change for callers that passed ``persist=False`` to apply a
  preference without rewriting the file; the new pattern is just
  ``set_language(language_pref())`` (idempotent re-write).
- New public ``i18n.display_name(code)`` — single source of truth for
  the language-code → human-name table. Peers that previously kept a
  parallel ``_DISPLAY_NAMES`` dict (the recorder's ``i18n.py`` did)
  now import this instead, eliminating the drift risk.
- New public ``i18n.scan_catalog_languages(locale_dir, domain)`` —
  walks ``<locale_dir>/<lang>/LC_MESSAGES/<domain>.{po,mo}`` and
  returns ``[(code, display_name), ...]``. Both the client's own
  ``available_languages()`` and the recorder's wrapper now share this
  shape, so peer catalogs and the client catalog enumerate
  identically.
- Updated callers: ``azt_collabd/ui/picker_app.py`` (build-time apply
  + mtime watcher) drop ``persist=False``; both calls just write the
  same value back, harmless because the mtime watcher's
  ``persisted == current_language()`` short-circuit prevents loops.

### azt_collabd 0.14.0 — content:// URIs across the picker boundary; clone auto-registers; MIN_CLIENT_VERSION → 0.17.0
- The picker (when running in the standalone server APK on Android)
  now emits a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI from ``_emit_and_quit``, instead of an absolute filesystem
  path inside the server APK's private ``filesDir``. The Intent
  carries the URI on its ``data`` field and adds
  ``FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION``
  so the calling peer can open the URI via
  ``ContentResolver.openFileDescriptor`` for the result delivery's
  lifetime.
- This removes a cross-package access bug (recorder's
  ``ElementTree.parse(path)`` raised ``[Errno 2]`` on an absolute
  path inside ``/data/user/0/org.atoznback.aztcollab/files/``,
  which the recorder's UID can't read). The provider's existing
  ``openFile`` callback (``_resolve_path`` under
  ``$AZT_HOME/projects/``) handles the URIs without changes —
  except for a leading-slash strip on ``Uri.getPath()`` so the
  path composes correctly. The single canonical copy in the
  daemon's ``$AZT_HOME`` stays the source of truth; peers don't
  cache.
- Successful clones now auto-register via
  ``projects.register(langcode, dest_dir, lift_path, remote_url)``.
  Previously a clone left the working tree on disk but no entry in
  ``projects.json``, so subsequent ``list_projects()`` /
  ``sync_project(langcode, …)`` calls couldn't find it. Failure
  is logged but doesn't fail the clone job — caller can re-register
  explicitly.
- ``MIN_CLIENT_VERSION`` raised to ``0.17.0`` because the URI shape
  is a hard contract: a peer bundling a pre-LiftHandle client would
  try to ``open()`` the URI as a path and crash on the spot. Old
  peers now get ``client_too_old`` from ``check_server_compat()``
  at startup with a clear "update this app" prompt.

### azt_collabd 0.13.21 — Bundle-based result extras to fix cross-package no_path loss
- Logcat showed the picker emitting a real lift_path
  (``/data/user/0/.../foo.lift``) via ``_emit_and_quit``, but the
  calling recorder still reported ``no_path`` — meaning the
  Intent's ``path`` extra was being lost across Android's IPC
  delivery to the peer process.
- Two likely culprits, both addressed by the same patch:
  1. ``Intent()`` with no action has been observed to drop extras
     on cross-package result delivery in some Android versions.
     The result Intent now carries the same action the recorder
     used for the request (``org.atoznback.aztcollab.PICK_PROJECT``).
  2. ``Intent.putExtra(String, String)`` is one of ~15 overloads;
     jnius's overload resolution can silently bind to a non-String
     overload when both args are CPython strings, leaving
     ``getStringExtra`` returning null on the peer side. Switched
     to the explicit, single-signature
     ``Bundle().putString('path', ...)`` →
     ``Intent.putExtras(Bundle)``.
- Added a diagnostic round-trip: the picker now reads the path
  back out of the result Intent (via ``getStringExtra``) right
  before ``setResult`` and prints it to logcat. If the verify
  print shows the path correctly but the recorder still gets
  ``no_path``, the loss is in Android's binder layer (genuinely
  rare); if the verify print is empty, the typed-Bundle approach
  also failed and we know to look further upstream.

### azt_collabd 0.13.20 — clone-flow diagnostic prints
- Added prints (to stderr → logcat ``python`` tag) at every step
  of the clone flow so the next ``no_path`` reproduction tells us
  exactly where the empty-path emission originates: ``clone worker
  starting``, ``clone returned`` (with ok / lift_path / error), the
  exception path, ``_after_clone_ok``, ``_after_clone_fail`` (with
  result codes), and ``load_lift`` (existing-project tap).
- Fixed a duplicate ``load_lift`` definition: a diagnostic-printing
  version was added without removing the original, and Python's
  last-def-wins on class bodies meant the diagnostic version was
  silently shadowed. Removed the second definition.

### azt_collabd 0.13.19 — debug bump for no_path triage
- Version-only bump so the user can verify on the picker's bottom
  strip (``server 0.13.19``) that the deployed build includes the
  ``_emit_and_quit`` empty-path guard from 0.13.15 and the
  Connect/Disconnect colour reactivity from 0.13.18.

### azt_collabd 0.13.18 — Connect/Disconnect button colour tracks connection state
- The four host action buttons (Connect / Disconnect GitHub,
  Set GitLab credentials / Disconnect GitLab) used to be statically
  green for Connect and dim for Disconnect. The visually-prominent
  button now matches the user's likely next action: Connect is
  green when not connected, Disconnect is green when connected.
  Dim button stays clickable so reconnect-to-refresh-tokens and
  similar flows still work — colour is a hint, not a gate.
- The colour swap is driven by ``refresh()`` reading
  ``credentials_status``, so the same round-trip that updates the
  status block also updates these buttons. Re-renders every time
  the screen is entered or the user taps Refresh Status.

### azt_collabd 0.13.17 — Refresh button rename + reposition
- ``Refresh`` button renamed to ``Refresh Status`` (more honest
  about what it does — it only re-pulls the credential / online
  read-out, doesn't do a sync) and moved to sit directly under the
  status block. Affordance for "I changed something in another
  window, pull the updated state" is now immediately adjacent to
  the data it refreshes, with no spacer in between.

### azt_collabd 0.13.16 — settings layout: status to the bottom, host rows compacted
- ``SettingsScreen`` reorder: actionable rows (interface language,
  contributor name, GitHub/GitLab connect+disconnect) at the top;
  the read-only ``Status`` block moved to the bottom. Users land
  here to do something, not to inspect — surfacing the controls
  first matches the visit pattern.
- GitHub and GitLab each collapsed from "section header + Connect
  row + Disconnect row" (3 rows of vertical real estate) into a
  single row with ``Connect…`` and ``Disconnect`` side-by-side.
  Brand name is implicit in the button text. About 100dp of
  vertical space recovered.

### azt_collabd 0.13.15 — server-owned contributor; auto-start device flow; loading-overlay wrap; empty-path guard
- New server-owned contributor field. ``store.get_contributor()`` /
  ``set_contributor(name)`` persist a display name to
  ``$AZT_HOME/config.json :: collab.contributor`` (sibling to
  ``ui.language`` — config, not credentials). New endpoints
  ``GET /v1/config/contributor`` and ``POST /v1/config/contributor``;
  client wrappers ``azt_collab_client.get_contributor`` and
  ``set_contributor``. ``store.get_status()`` now includes
  ``contributor`` so the settings UI gets it on the existing
  credentials-status round-trip. ``_h_project_sync``,
  ``_h_init_project``, ``_h_project_sync_async``, and
  ``scheduler._run_sync`` all route through new
  ``store.resolve_contributor(passed)`` which prefers the caller's
  explicit value, then the stored display name, then the
  ``'Recorder'`` fallback. Peers can stop carrying their own "Your
  name" preference; the suite has one source of truth on the server.
- ``SettingsScreen`` got a "Your name (appears in commits)"
  ``ThemedInput`` field with a transient "Saved." confirmation; the
  field auto-saves on focus loss. Refresh repopulates it from the
  server only when the user isn't actively editing.
- ``GitHubConnectScreen`` auto-starts the device flow on screen
  entry — no more "tap Begin to start" friction. The Begin button
  stays around as a Retry surface, re-enabled by the worker's
  failure path.
- ``picker_app._show_loading_overlay`` Label now wraps on width
  (same fix as ``_show_error``); long ``Cloning <url>...``
  messages no longer overflow both edges.
- ``picker_app._emit_and_quit`` refuses to emit an empty path. On
  Android an empty path lands at the peer's
  ``pick_project_android`` handler as ``RESULT_OK`` with no extra
  and surfaces as ``no_path``. If anything upstream tries it now,
  we log a stack trace to logcat and show an "Internal error:
  tried to return an empty path" modal so the user can pick again
  instead of bouncing back to the recorder with a cryptic failure.

### azt_collabd 0.13.14 — error-modal text wraps; auto-copy GitHub user_code
- ``picker_app._show_error`` Label was constructed with
  ``text_size=(None, None)``, which disables wrapping — long
  messages overflowed both edges of the modal. Now binds
  ``text_size`` to the Label's width so text wraps inside the
  modal panel; height stays free so the texture grows vertically
  as needed (modal's fixed height clips the bottom for very long
  messages, acceptable for typical 2–3-line errors).
- GitHubConnectScreen used to auto-copy the device-flow
  ``user_code`` to the clipboard so users could paste it into the
  GitHub device page without an extra tap; this regressed during
  the settings UI restyle. Restored the auto-copy and append the
  existing ``(code copied)`` translated suffix to the on-screen
  message when the copy succeeds (silently no-ops if Clipboard is
  unavailable, e.g. on a headless device).

### azt_collabd 0.13.13 — Android-aware ``azt_home()`` (and azt_collab_client mirror)
- ``[Errno 13] Permission denied: '/data/.local/share/azt/...'`` on
  every file op (template download, sync, etc.). p4a does not set
  ``$HOME``, so ``os.path.expanduser('~')`` resolved to ``/data`` —
  the Android system-data root, owned by ``root``, not writable by
  the app's UID. ``azt_home()`` then composed a path no app can
  write to.
- ``paths.py`` (both ``azt_collabd/`` and ``azt_collab_client/`` —
  duplicated by design) gained a ``_android_files_dir()`` helper
  that calls ``PythonActivity.mActivity.getFilesDir()`` via jnius
  and returns the app's private writable filesDir
  (``/data/user/0/<pkg>/files``). ``azt_home()`` checks that first
  on Android (after ``$AZT_HOME``, before XDG fallbacks). Desktop
  unchanged. The ``$AZT_HOME`` env-var override still wins for
  test rigs.

### azt_collabd 0.13.12 — settings Back button uses a glyph CharisSIL has
- ``SettingsScreen``'s "← Back" button used U+2190 (LEFTWARDS
  ARROW) which isn't in the CharisSIL glyph table — rendered as
  tofu under the project's default linguistic font. Swapped for
  ``«`` (U+00AB, left guillemet): present in every Latin font,
  reads as a back-pointer, and is the natural French equivalent
  too.

### azt_collabd 0.13.11 — preserve ``back_to`` across language-toggle screen rebuild
- ``_set_ui_language`` (settings UI) and ``_check_language_change``
  (picker subprocess) rebuilt the ScreenManager by recreating each
  screen with ``cls(name=name)``. That recipe loses any property
  the parent KV rule set on the *instance* (not the class) —
  notably ``back_to: 'picker'`` on ``SettingsScreen`` in
  ``picker_app._PickerRoot``. Symptom: the in-screen "← Back"
  button vanished the first time the user toggled language and
  didn't come back when toggling to English.
- Both rebuild loops now capture ``back_to`` per-screen before the
  ``clear_widgets`` and re-apply after instantiation. Generic
  enough to extend to other instance-level properties later.

### azt_collabd 0.13.10 — Android back button on picker subscreens
- Hardware back / gesture on Android does not flow through
  ``App.on_request_close`` (which only fires for the desktop X
  button). It surfaces as ``key 27`` on ``Window.on_keyboard``.
  Without an explicit binding, Kivy's default for an unhandled key
  is ``App.stop`` — so back from settings / github / gitlab /
  langpicker was closing the picker activity entirely.
- ``PickerApp.on_start`` now binds ``Window.on_keyboard`` to a new
  ``_on_back_button`` handler that delegates to ``_navigate_back``
  (extracted from the existing ``on_request_close`` logic). On a
  non-picker screen, back navigates to the screen's ``back_to``
  property (or ``'picker'`` by default); on the picker itself, back
  falls through to the normal ``RESULT_CANCELED + finish()`` exit.
  Same screen-pop dance the recorder uses.

### azt_collabd 0.13.9 — full traceback on template-download failures
- ``_h_create_project_from_template`` was masking the original
  failure type by catching ``Exception`` and returning only
  ``str(ex)``. On the device a ``PermissionError`` surfaced as
  ``provider HTTP 500: [Errno 13] Permission denied`` with no path
  or call site. Now logs the full traceback to stderr/logcat
  (``adb logcat | grep -i from_template``) and includes
  ``traceback`` and ``ExceptionType`` in the response body so the
  caller (picker / recorder) can surface them.
- Confirms in code comments that the template download is an
  anonymous public HTTPS GET — no GitHub credentials consulted.

### azt_collabd 0.13.8 — drop "Active host" toggle from settings UI
- ``SettingsScreen`` no longer renders the "Active host" SectionLabel
  + GitHub/GitLab two-button row. URL-based credential routing
  (``store.get_sync_credentials(url)`` → ``host_for_url(url)``)
  has handled every common case since 0.12.0; the toggle was
  vestigial. ``choose_host`` method dropped from ``SettingsScreen``.
  ``set_collab_host`` server endpoint and client wrapper stay around
  for wire compat (peers still calling them are safe; the value
  silently affects only the self-hosted/unknown-URL fallback path
  through ``get_collab_host()``).
- The eventual "Publish" flow for new local-only projects will
  pick credentials by inspecting which hosts have stored creds,
  prompting the user only when more than one is configured —
  rather than reading a global "active host" preference. Captured
  here so future-me doesn't reintroduce the toggle.

### azt_collabd 0.13.7 — locale files packaged in server APK
- ``server_apk/buildozer.spec`` ``source.include_exts`` was
  ``py,xml,gz,png``, silently dropping the ``.po``/``.mo`` files
  under ``azt_collab_client/locales/`` at packaging time. On the
  device, ``available_languages()`` walked an empty locale tree and
  the settings UI's language toggle only offered English regardless
  of which catalogs lived in the source tree. Added ``po,mo`` to
  the extension list. Pre-compile any language's ``.mo`` before
  rebuilding so the catalog ships pre-baked (faster first paint;
  also dodges any APK-readonly issue with the runtime
  ``_ensure_mo``):
  ``python -c "from azt_collab_client.i18n import _ensure_mo;
  _ensure_mo('fr')"``.

### azt_collabd 0.13.6 — typing_extensions in APK requirements; BodyLabel recursion fixed at class level
- Added ``typing_extensions`` to the server APK's ``requirements``
  in ``server_apk/buildozer.spec``. dulwich (and a few of its
  transitive imports) reach for ``typing_extensions`` at runtime;
  on Android it isn't pulled in by default. Previously a clone
  attempt would fail with ``ImportError: no module named
  typing_extensions`` at the moment dulwich tried to do its first
  network operation. Adding the recipe to requirements puts it on
  the APK's PYTHONPATH. **Requires a clean build**
  (``buildozer android clean && buildozer android debug deploy``)
  for p4a to pick up the new recipe.
- Promoted the ``text_size: self.width, None`` fix from
  per-instance overrides on three ``BodyLabel`` uses to the
  ``<BodyLabel@Label>`` class rule itself. Any ``BodyLabel`` whose
  ``height: self.texture_size[1] + dp(8)`` would otherwise loop
  with ``text_size: self.size`` is now safe by default. The earlier
  per-instance overrides remain (redundant but harmless).

### azt_collabd 0.13.5 — picker version-probe diagnostics
- ``picker_app._probe_server_version`` now surfaces *why* the probe
  failed when it can't show a real server version. Instead of a bare
  ``server ?``, the bottom strip renders one of
  ``server ? (server_unreachable)`` /
  ``server ? (server_too_old)`` /
  ``server ? (client_too_old)`` /
  ``server ? (<ExceptionType>: ...)``. Distinguishes transport down
  vs. version-handshake reject vs. RPC exception without needing
  ``adb logcat``. Also prints a one-line diagnostic to stderr/logcat
  so the post-mortem is in both places.

### azt_collabd 0.13.4 — debug version bump
- No code change. Version-only bump so a freshly-rebuilt server APK
  reports a different ``__version__`` from the previous build,
  letting the user verify on the picker's bottom strip
  (``client X · server Y``) that the device is actually running the
  new build vs a cached install.

### azt_collabd 0.13.3 — picker shows both versions, auth-fallback for clone failures
- Picker bottom strip now shows ``client X · server Y``. The server
  half is fetched off the UI thread via
  ``check_server_compat()``; renders ``server ?`` if the daemon is
  unreachable. Was: client only.
- ``_after_clone_fail`` got a fallback path: when the daemon's
  worker didn't run far enough to attach ``CLONE_AUTH_REQUIRED``
  (e.g. the clone-job kickoff itself failed and the result is None)
  but the error string smells like auth (401 / 403 / 404 /
  unauthorized / forbidden / not found / authentication /
  credential), the auth-prompt modal still appears with the **Open
  settings** button. Same heuristic the daemon uses, mirrored
  client-side for the result-is-None case.
- Auth-modal "Open settings" button now calls ``self.go_config()``
  (in-process screen swap) instead of the removed
  ``open_server_ui`` import. The Android Intent dance is gone from
  this flow entirely.

### azt_collabd 0.13.2 — picker hosts settings screens in-process
- Picker's gear used to call ``azt_collab_client.open_server_ui()``,
  which on Android fires ``getLaunchIntentForPackage`` on the server
  APK. Because the server APK has a single ``PythonActivity`` already
  running the picker, Android collapsed the task back to the calling
  peer (the recorder) instead of switching to settings — there was
  no path forward from the picker.
- ``azt_collabd/ui/app.py`` now exposes a top-level
  ``register_kv(font_name)`` (idempotent) that loads the settings/
  GitHub/GitLab KV. ``CollabUIApp.build`` calls it; the picker_app
  also calls it before its own KV so all class rules are in scope.
- ``picker_app._PickerRoot`` ScreenManager now carries
  ``SettingsScreen`` (with ``back_to: 'picker'``),
  ``GitHubConnectScreen``, and ``GitLabFormScreen`` alongside the
  existing ``ProjectPickerScreen`` / ``LangPickerScreen``. ``go_config()``
  is now ``self.sm.current = 'settings'`` — no Intent, no subprocess.
  Same code path on desktop and Android.
- New ``go(name)`` method on ``PickerApp`` mirrors ``CollabUIApp.go``
  so the existing settings-side KV (``app.go('github')``,
  ``app.go('gitlab')``, ``app.go('settings')``) just works in both
  hosts.
- New ``back_to`` ``StringProperty`` on ``SettingsScreen``. When set
  (the picker_app ``_PickerRoot`` KV sets it to ``'picker'``) the
  screen renders an additional **← Back** ``NavBtn`` at the top of
  the layout. Hidden / disabled in the standalone settings host
  where back has no meaning. The Android back gesture / window-close
  on non-picker screens is also intercepted by ``on_request_close``
  to navigate back instead of exiting.
- Known limitation: a peer calling ``open_server_ui()`` *while a
  picker is already up* still hits the Android launch-flag bug
  (settings doesn't appear; task may collapse to the peer). Rare in
  practice and not worth a separate ``SettingsActivity`` declaration
  yet — tracked as future work.

### azt_collabd 0.13.1 — settings UI Clock-iteration warning fix
- ``BodyLabel`` instances that combined ``text_size: self.size``
  (inherited) with ``height: self.texture_size[1] + dp(8)`` were
  forming a feedback loop on Android: texture_update changes height
  → parent BoxLayout do_layout → child resize → text_size changes →
  texture_update fires. Tolerable before; pushed past Kivy's
  per-frame Clock iteration limit by the new language-toggle row
  and the wrapping of every settings-UI string in ``_(...)``.
  Surgical fix on the three offending BodyLabels (status_label,
  gh_message, the gitlab-form intro): override
  ``text_size: self.width, None`` so the wrap width is bound but
  height flows from content alone, breaking the cycle.

### azt_collabd 0.13.0 — settings UI translatable, language toggle, picker live retranslation
- ``SettingsScreen`` gained an **Interface language** row at the top
  with one button per ``azt_collab_client.i18n.available_languages()``.
  Selecting a language calls ``i18n.set_language(code)`` (which
  persists to ``$AZT_HOME/config.json`` under ``ui.language``) and
  rebuilds every screen in the manager so KV ``text: _('...')``
  bindings re-evaluate against the new catalog. Same dance the
  recorder's ``ConfigScreen`` uses.
- Every visible string in ``azt_collabd/ui/app.py`` (Settings, GitHub
  device-flow, GitLab form) is now wrapped in ``_(...)``. KV imports
  ``_ azt_collab_client.translate.tr`` so subsequent
  ``set_translator``/language switches take effect.
- ``picker_app.py`` watches ``$AZT_HOME/config.json`` mtime once a
  second (Clock interval). When the persisted language changes — for
  example because the user just toggled it in a settings subprocess
  opened from the gear — the picker rebuilds its screens in place.
  The user sees the picker live-retranslate without restart.
- Apply persisted language at picker / settings startup so first
  paint is in the right language.

### azt_collabd 0.12.1 — picker gear wired to settings, both versions on settings page
- Standalone picker (``python -m azt_collabd projects``) now shows
  the settings gear in the top-right and wires it to the daemon's
  settings UI via ``open_server_ui()`` instead of the previous no-op
  stub. Rationale: first-time users land on the picker and need a
  visible path to authentication; previously they had to fail a
  clone before the auth-prompt modal offered one.
- Settings UI (``python -m azt_collabd ui``) now displays both the
  client and server versions in the bottom version strip:
  ``client 0.14.1  ·  server 0.12.1`` — used to show only the
  daemon version. The settings UI subprocess imports
  ``azt_collab_client`` for ``__version__``.
- Both version labels (settings + picker) bumped from
  ``font_size: sp(11) / color: T.TEXT_FAINT`` to
  ``sp(13) / T.TEXT_DIM`` so they're legible without straining.

### azt_collabd 0.12.0 — URL-based credential routing, CLONE_AUTH_REQUIRED, MIN_CLIENT_VERSION handshake
- New ``azt_collabd.MIN_CLIENT_VERSION = '0.14.0'`` — floor on the
  ``azt_collab_client`` version this daemon will talk to. ``/v1/health``
  now publishes ``min_client_version`` alongside ``version``. Symmetric
  to the existing client-side ``MIN_SERVER_VERSION``: when a peer ships
  a too-old client, the peer's startup handshake gets ``client_too_old``
  and can prompt the user to update. Bump this constant in lockstep
  with any wire-format addition that older clients can't decode (e.g.
  the new ``CLONE_AUTH_REQUIRED`` status added in this release).
- ``store.get_sync_credentials()`` now takes an optional remote URL and
  picks credentials by host (``github.com`` → GitHub creds,
  ``gitlab.com`` → GitLab creds), falling back to the user's saved
  ``collab_host`` only when the URL is unrecognized (self-hosted /
  empty). All call sites — ``_h_init_project``, ``_h_clone_project``,
  ``_h_project_sync`` in ``server.py`` and ``_run_sync`` in
  ``scheduler.py`` — pass the remote URL. Symptom this fixes: the user
  picks a LIFT project without first visiting Settings; the daemon
  used to send GitHub creds to a GitLab remote (or vice-versa) just
  because ``collab_host`` was the wrong value.
- New helper ``store.host_for_url(url)`` exposes the URL → host
  classifier.
- New status code ``CLONE_AUTH_REQUIRED`` (with ``host`` param). The
  clone worker now appends it after final failure when either (a) no
  token was available for the URL's host, or (b) the dulwich error
  contains 401/403/404/auth keywords. The picker UI uses this to
  branch into an auth-prompt modal instead of a generic error.
- The auth-shaped retry already in ``_clone_worker`` was extracted to
  ``_clone_error_looks_like_auth(result)`` and now also recognises
  ``not found`` / 404 (private-repo case) — previously only matched
  ``credential`` / ``auth``, so a 404-bearing failure skipped the
  anonymous retry.

### azt_collab_client 0.15.2 — Android ContentProvider path delivery fix
- **Critical bug fix.** The Android transport
  (``azt_collab_client/transports/android_cp.py``) built a URI like
  ``content://<authority><path>`` and called
  ``ContentResolver.call(uri, method, None, extras)``. But the
  ``call(Uri, method, arg, extras)`` overload only delivers
  ``method``, ``arg``, and ``extras`` to
  ``ContentProvider.call(method, arg, extras)`` — the URI's path is
  consumed by provider routing and never reaches the dispatch.
- AZTCollabProvider.java reads ``arg`` as the path
  (``cb.dispatch(method, arg != null ? arg : "", body)``), so on the
  daemon side every RPC was being dispatched with ``path=""``,
  producing ``{ok: False, error: 'not_found'}``. User-visible
  symptom: every clone / list / sync / credential RPC silently
  routed to the dispatcher's catch-all 404 branch.
- Fix: pass the dispatch path as ``arg`` instead of None. The URI
  shrinks to just the authority (no path component) since it's only
  used for provider routing now. One-line change at the call site.
- This was a long-standing bug that likely went unnoticed because
  legacy peers symlinked ``azt_collabd`` and used the loopback
  transport (Python interpreter in-process). Peers on the new
  ContentProvider-only model would have hit it on every non-ping
  call.

### azt_collab_client 0.15.1 — picker gear icon bundled in package
- Picker KV used to reference the gear icon as a relative
  ``'icons/gear.png'`` path, which only resolves when the host's
  cwd happens to contain that file (worked from the recorder repo
  root, broke everywhere else — most visibly in the standalone
  picker subprocess, where Kivy fell back to its missing-image
  texture and the gear rendered as a white square).
- The icon is now an absolute path computed from the package
  location: ``azt_collab_client/ui/assets/gear.png``. The KV
  template injects it at ``register_kv`` time alongside ``font_name``.
- ``register_kv`` (a.k.a. ``register_picker_kv``) gained an optional
  ``gear_icon=`` kwarg so hosts that want a custom icon can pass
  one explicitly (the recorder still ships its own at
  ``azt_recorder/icons/gear.png``); default falls back to the
  package-bundled file.
- **Important**: the binary file
  ``azt_collab_client/ui/assets/gear.png`` is not committed by this
  change; copy from any peer that already has one
  (e.g. ``cp /home/kentr/bin/AZT/azt_recorder/icons/gear.png
  azt_collab_client/ui/assets/gear.png``).

### azt_collab_client 0.15.0 — own i18n domain (azt_collab_client.po), pure-Python msgfmt, fallback chain in translate.tr
- New module ``azt_collab_client.i18n`` owns gettext domain
  ``azt_collab_client``. Public API:
  ``set_language(lang, persist=True)``, ``current_language()``,
  ``available_languages()``, ``language_pref()``, ``_(msg)``,
  ``gettext_translation()``. Persists the active language to
  ``$AZT_HOME/config.json`` under ``ui.language``; auto-applies that
  preference at import so all suite subprocesses converge on the same
  language without a coordination channel.
- New locale tree
  ``azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po``
  with French translations of all client-owned strings: picker UI,
  langpicker, popups, ``translate.py`` status messages (the full
  ``S.*`` set), and the settings-UI strings now owned by the client.
- ``i18n.py`` ships a pure-Python PO→MO compiler (msgfmt-lite — single
  magic, sorted msgid array, two parallel offset tables packed via
  ``struct``). Runs lazily on first ``set_language`` whenever the
  ``.mo`` is missing or older than the ``.po``. So peers that ship
  only the ``.po`` (or contributors editing translations in-place) do
  not need a build-time ``msgfmt`` step.
- ``translate.py`` default translator changed from "try
  ``from i18n import _`` (recorder)" to "use the client catalog
  directly". ``set_translator(host_tr)`` overrides as before, but
  ``tr(msg)`` now falls **back** to the client catalog whenever the
  host translator returns ``msg`` unchanged. The fallback layer means
  embedded peers (the recorder) do not need to duplicate client
  strings into ``aztrecorder.po``: a string the recorder catalog
  doesn't know falls through to ``azt_collab_client.po``. Owns its
  own strings, no duplication, gettext-canonical.
- Behavior change to be aware of: hosts that previously relied on
  the implicit ``from i18n import _`` fallback now get the client
  catalog first. Hosts with their own catalogs should keep calling
  ``set_translator(host._)`` at startup; the new fallback in
  ``tr()`` handles client strings transparently.

### azt_collab_client 0.14.1 — picker version label clarified
- Picker bottom-strip version label changed from ``collab X.Y.Z`` to
  ``client X.Y.Z`` so users can tell client and server versions
  apart at a glance (the settings page shows both).
- ``ProjectPickerScreen`` version label sized up:
  ``font_size: sp(13)`` / ``color: T.TEXT_DIM`` (was
  ``sp(11)`` / ``T.TEXT_FAINT``). Same change applied in
  ``azt_collabd/ui/app.py`` for consistency.

### azt_collab_client 0.14.0 — auth-prompt modal on clone failure, client_too_old handshake
- ``check_server_compat()`` gained a third branch: when the server's
  ``min_client_version`` is greater than this client's ``__version__``,
  the function now returns
  ``{'ok': False, 'error': 'client_too_old', 'client_version', 'server_version', 'min_required'}``.
  Mirrors the existing ``server_too_old`` shape so peer apps can branch
  on the same dict. Forward-compatible with pre-0.12.0 daemons that
  don't publish ``min_client_version`` (treated as "no floor").
- New status mirror ``S.CLONE_AUTH_REQUIRED`` and translation:
  *"Clone failed — repository not found. This may be a private
  repository. Are you authenticated to {host}?"* (host is rendered
  Title-cased: GitHub / Gitlab).
- ``azt_collabd/ui/picker_app.py`` clone-fail flow now threads the
  daemon's ``Result`` through ``_after_clone_fail``. When the result
  carries ``CLONE_AUTH_REQUIRED``, the modal renders the translated
  prompt and an extra **Open settings** button that calls
  ``azt_collab_client.open_server_ui()``. Previously the user saw a
  bare *"Clone failed: not found"* and had no path forward — they
  would not have visited Settings before picking a project, so we
  lead them there.
- ``_show_error`` grew an optional ``extra_button=(label, callback)``
  argument so the same modal helper can host either a single Dismiss
  button (existing behavior) or a two-button row.

### azt_collabd 0.11.0 — settings UI restyle, picker typography fix
- The standalone settings UI (``python -m azt_collabd ui`` /
  launcher activity in the server APK) was using stock Kivy buttons
  with no theme, no ``font_name``, and no top bar. It looked
  unrelated to the recorder. Restyled to mirror the recorder's
  ``CollabScreen``: themed top bar (``T.SURFACE`` background,
  ``T.ACCENT`` bold title), ``BG``-painted screens, ``SectionLabel``
  / ``HeaderLabel`` / ``BodyLabel`` / ``DimLabel`` dynamic classes,
  ``ThemedInput`` for text fields, ``RecBtn`` for primary actions,
  ``NavBtn`` for secondary navigation. The host toggle now highlights
  the active host with ``T.GREEN`` (was: a disabled stock button).
- The standalone picker (``picker_app.py``) was rendering its
  ``RecBtn`` with raw ``font_size: 16`` (un-scaled pixels — tiny on
  hi-dpi phones), no ``font_name``, and a hardcoded blue
  ``(0.2, 0.6, 1, 1)`` instead of a theme colour. Replaced with the
  recorder's idiom: ``font_size: sp(16)``, ``font_name: FONT``,
  ``normal_color: T.ACCENT``. The error / loading modal overlays
  were also unstyled stock widgets; now ``T.SURFACE`` rounded
  panels with ``T.TEXT`` labels and a themed dismiss button.
- Both apps now call ``register_charis()`` from the new shared
  helper at startup. If the CharisSIL TTFs can be located (the
  recorder's ``fonts/`` dir during desktop dev, system font dirs on
  Linux, or a future ``azt_collab_client/fonts/`` location) they
  register under LabelBase name ``CharisSIL``; otherwise the apps
  fall back to ``Roboto`` silently. The standalone server APK
  doesn't currently bundle the TTFs (~20 MB), so on-device it falls
  back to Roboto — sizes and theme are aligned, glyphs are not.
  Bundling the fonts in the server APK is a follow-up.

### azt_collab_client 0.13.6 — shared CharisSIL helper, larger picker logo
- New ``azt_collab_client.ui.fonts.register_charis()`` (re-exported
  as ``azt_collab_client.ui.register_charis``). Discovers
  CharisSIL-Regular/Bold/Italic/BoldItalic TTFs across a small list
  of likely locations (canonical client ``fonts/`` slot, sibling
  recorder ``fonts/`` dir, system font dirs) and registers them
  under LabelBase name ``CharisSIL``. Returns the LabelBase name to
  use (``'CharisSIL'`` or ``'Roboto'``); idempotent.
- ``ProjectPickerScreen`` typography: logo grew from ``dp(200)`` to
  ``dp(240)``, title from ``sp(28)`` to ``sp(32)``, subtitle from
  ``sp(16)`` to ``sp(18)``. Logo gets explicit
  ``allow_stretch / keep_ratio: True`` so the larger size doesn't
  pixelate. Title / subtitle now centred with explicit
  ``halign: 'center'`` + ``text_size: self.size`` so they don't
  drift left in narrow layouts.

### azt_collab_client 0.13.5 — open_server_ui dispatches on Android + shared install popup
- ``open_server_ui()``'s docstring has long promised that on Android
  it would dispatch an Intent to the server APK's launcher activity.
  It now does. New ``_open_server_ui_android`` resolves
  ``PackageManager.getLaunchIntentForPackage('org.atoznback.aztcollab')``
  and starts the activity. Returns
  ``{'ok': True, 'launched': 'android-apk'}`` on success.
- If the APK isn't installed, the helper opens a new install-prompt
  popup (``ui.popups.install_server_apk_popup``) and returns
  ``{'ok': False, 'error': 'server_apk_not_installed', 'prompted': True}``
  so the caller knows the popup is on screen. The popup itself
  routes through ``Intent.ACTION_VIEW`` on Android and
  ``webbrowser.open`` on desktop, both pointed at
  ``SERVER_APK_INSTALL_URL``.
- New optional ``on_status`` callback on ``open_server_ui`` /
  ``install_server_apk_popup``: the popup uses it to surface
  "could not open install page — …" failures into the host's
  status bar without the host having to reach into the popup
  internals. Sister apps that don't pass ``on_status`` still work.
- ``ui.popups`` and ``ui/__init__`` export
  ``install_server_apk_popup`` so peers can also call it directly
  (e.g. from a startup-time ``ServerUnavailable``-handling path,
  not just from the settings button).
- Decouples sister apps from per-app reimplementations of
  "launch APK / show install prompt" and lets the viewer collapse
  ~60 lines of jnius / popup boilerplate.

### azt_collabd 0.10.6 — pin AZTCollabProvider callback proxies
- **Bug fix:** ``android_cp/service.install_callbacks`` was passing
  freshly-constructed ``_Dispatch()`` / ``_OpenFile()`` PythonJavaClass
  instances inline to ``Provider.registerCallbacks``. Java held refs;
  Python did not. After a GC cycle (typically within seconds of the
  picker Activity launching) the proxy instances were freed, and the
  next binder-thread call from a peer's ContentResolver into
  ``AZTCollabProvider.call`` dereferenced the dead type object. Net
  effect on hardware: ``Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR),
  fault addr 0x143`` on Thread-3, with backtrace through
  ``NativeInvocationHandler.invoke`` → ``_PyObject_GenericGetAttrWithDict``
  → ``_PyType_Lookup``. From the peer's perspective the picker
  Activity vanished and the recorder logged
  ``[activity-result] code=18247 result=0`` (default RESULT_CANCELED
  from the killed Activity).
- Fix: store strong refs to both proxies at module scope before
  handing them to Java. Validate with a clean rebuild
  (``buildozer android clean && buildozer android debug``).

### azt_collab_client 0.13.4 — gear icon source conditional
- The picker KV had an unconditional ``source: 'icons/gear.png'`` even
  when the host passed ``hide_settings_gear=True`` (size goes to 0×0
  in that case but Kivy still tries to resolve the source). The
  standalone picker app and any sister app that hides the gear was
  logging ``[ERROR ] [Image ] Not found <icons/gear.png>`` on every
  picker open. Cosmetic, not the cause of the segfault — but the
  standalone server APK has no ``icons/`` dir of its own so the line
  was particularly visible there.
- Fix: gate the source on the same ``{show_gear}`` template flag the
  size already uses (``source: 'icons/gear.png' if {show_gear} else ''``
  in ``azt_collab_client/ui/picker.py``).

### azt_collabd 0.10.5 — server APK ships non-Python assets
- **Bug fix:** `server_apk/buildozer.spec` had `source.include_exts =
  py,xml`, which silently stripped `azt_collab_client/ui/assets/
  langtags_mini.json.gz` and `azt_collab_client/azt.png` from the
  packaged APK. Net effect: tapping **Start New** in the picker
  Activity raised `FileNotFoundError: ... langtags_mini.json.gz` from
  `LangPickerScreen._load_langtags`, which crashed the Activity; the
  Activity then finished without an explicit `setResult(-1, ...)` so
  peers saw `[activity-result] code=18247 result=0` (RESULT_CANCELED
  for `_AZT_PICK_REQ_CODE`). The same was true for any flow that
  reaches the langpicker, including the path between Clone-Internet
  and the post-clone confirmation when the user backs out via the
  langpicker.
- Fix: extend `source.include_exts` to `py,xml,gz,png`. `gz` covers
  the langtags blob; `png` covers the suite icon used by `App.icon`
  in `azt_collabd/ui/picker_app.py`. Validate with a clean rebuild
  (`buildozer android clean && buildozer android debug`) — hot
  patches into `.buildozer/.../build/` only prove the patch *content*
  is right, not that the spec change actually fires.

### Suite naming convention
- Adopted a single rule for surfacing the "azt collab" / "azt
  recorder" names across systems with incompatible identifier rules
  (Python identifiers forbid `-`, Android package segments forbid
  `-`, GitHub App slugs forbid `_`). Documented in `CLAUDE.md`. Net:
  `_` is the default for code identifiers, env vars, repo dirs, and
  Python packages; the dropped form (`aztcollab`) is the Android
  package; the hyphenated form (`azt-collaboration`) is the GitHub
  App slug; the human-facing form is "AZT Collaboration" (same word
  in French and English, which keeps i18n natural for the SIL user
  base). The internal token "collab" stays in code (`azt_collabd`,
  `AZT_COLLAB_ACCESS`, `org.atoznback.aztcollab`) — only the
  human-facing surfaces and the GitHub App slug expand to
  "Collaboration" / "azt-collaboration".
- Default `app_slug` in `azt_collabd/config.py` is now
  `azt-collaboration` (was `azt-recorder`, an artifact of the
  recorder's earlier ownership of the GitHub App). Override via
  `AZT_GITHUB_APP_SLUG=` if your suite ships under a different
  registration. The same default is mirrored in
  `server_apk/main.py` and the README env-var table.
- Buildozer title ("AZT Collaboration") and prose mentions ("AZT
  Collaboration service") capitalized consistently across the tree.

### Android manifest assembly
- **Bug fix:** `buildozer.spec`'s manifest-extras key is
  `android.extra_manifest_xml`, NOT `android.manifest_extra_xml`
  (different word order). Buildozer silently ignores unknown keys,
  so before this fix every peer's extras file was being dropped on
  the floor — including the recorder's `<queries>` block, which on
  Android 11+ is required for the client's `discover()` probe to
  even see the server APK.
- New canonical peer manifest extras at
  `android/manifest_extras_peer.xml` (just the suite `<queries>`
  block; meant to be symlinked into each peer as
  `manifest_extras.xml` and referenced from buildozer.spec).
- `server_apk/manifest_extras.xml` rewritten to top-level-only
  content (just the signature `<permission>`). p4a's
  `extra_manifest_xml` injects at top level under `<manifest>`,
  before `<application>` — `<application>` wrappers there end up as
  duplicate-application errors.
- New `before_apk_assemble` step in
  `buildozer_tweaks/p4a_hook.py`:
  `_inject_aztcollab_provider`. Patches the rendered
  `AndroidManifest.xml` post-template-render to inject the
  `<provider>` declaration inside `<application>`. Gated on
  `dist_name == 'aztcollab'` so peer APKs don't accidentally
  inherit the provider declaration. (p4a's SDL2 bootstrap manifest
  template only exposes top-level injection, so the provider — which
  must live inside `<application>` — has no spec-level injection
  point.)

### Build infrastructure (NDK r29 compatibility)
- New local recipe override `recipes/sdl2_ttf/__init__.py` in
  `buildozer_tweaks/`: patches harfbuzz's `Android.mk` to add
  `-DHB_NO_PRAGMA_GCC_DIAGNOSTIC_ERROR -Wno-error=cast-function-type-strict`.
  SDL2_ttf 2.20.2 ships an old harfbuzz whose `hb.hh` promotes
  `-Wcast-function-type` to error via `#pragma GCC diagnostic`, and
  NDK r29's clang lumps the `-strict` variant into that group;
  `hb-ft.cc`'s `(FT_Generic_Finalizer)` casts then fail.
- New `recipes/kivy/__init__.py`: (a) gates kivy's
  `merge(flags, sdl2_flags)` on `not kivy_sdl2_path` so host
  pkg-config doesn't leak `-I/usr/include/SDL2` (and via that
  `sys/cdefs.h`) into the cross-compile; (b) adds
  `-Wno-error=incompatible-function-pointer-types` to CFLAGS so
  kivy 2.3.0's `cgl_gl.c` glShaderSource const-mismatch doesn't
  fail the build.
- All build patches now run from `prebuild_arch` / `before_apk_*`
  hooks — the previous `before_apk_build` placement of the harfbuzz
  and kivy patches in `p4a_hook.py` was dead code (it fires after
  `build_recipes`, way too late). Those legacy `_patch_*` functions
  in the hook are kept as harmless no-ops; safe to remove on a
  future cleanup.

### Server APK packaging
- New `server_apk/setup.sh`: idempotent symlink creator for
  `azt_collabd` and `azt_collab_client` from the parent repo into
  the server APK source dir. Run once after a fresh checkout so
  buildozer can find the daemon code. Replaces the dangling
  `../setup_from_nuke.sh` reference that used to live in
  `main.py`'s comments.
- `server_apk/buildozer.spec` now points at the proper
  `extra_manifest_xml` key and includes icon assets
  (`icon.filename`, `icon.adaptive_*`) so the launcher icon isn't
  the default Kivy logo.

### azt_collabd 0.10.3 — manifest dual-patch + on-device verification

- `_inject_aztcollab_provider` and `_inject_aztcollab_pick_intent`
  in `buildozer_tweaks/p4a_hook.py` now patch BOTH
  `AndroidManifest.xml` (the dist root) AND
  `src/main/AndroidManifest.xml` (the file gradle's default
  sourceSets actually reads). Previously patched only the dist
  root, so gradle ran against the unpatched copy and the resulting
  APK had no `<provider>` despite the dist-root manifest on disk
  looking correct. Symptom: dumpsys showed no provider yet
  `aapt dump xmltree` of the *dist root* manifest confirmed it —
  diverging because gradle's input was a different file.
- New `server_apk/test_install.sh` — 15-check on-device
  verification of the server APK: install, `<permission>`
  declaration, signature self-grant, `<provider>` registration
  (multi-source: per-package dumpsys, system-wide provider table,
  `pm dump`), direct `content query`, bundled
  `azt_collabd`/`azt_collab_client` Python modules, launcher icon
  vs. default Kivy logo, activity launches without crash, source
  symlinks, dist manifest sentinel, hook traces, installed-vs-bin
  APK md5 match, APK's own manifest, all dist manifests' patch
  status, gradle manifest config.
- New `azt_collab_client/test_peer.sh` — peer-side verification:
  walks each `org.atoznback.*` package on the device, confirms
  each requests `AZT_COLLAB_ACCESS`, was granted (signature match),
  declares the suite `<queries>` block, and signs against the
  fingerprint in `android/SUITE_FINGERPRINT`.

### azt_collabd 0.10.1 — naming default + build/manifest plumbing
- Default `_SLUG_DEFAULT` in `config.py` flipped from `'azt-recorder'`
  to `'azt-collaboration'` to match the renamed GitHub App slug.
  Same default mirrored in `server_apk/main.py`.
- All cross-cutting build / manifest / naming work above
  (NDK r29 patches, manifest assembly fix,
  `_inject_aztcollab_provider` hook, `setup.sh`) lands in this
  patch level — no daemon API change, but the server APK packaging
  pipeline becomes reliable for the first time.

### azt_collabd 0.10.0 — picker helper subprocess
- New `python -m azt_collabd projects` entry point in `__main__.py`.
  Runs `azt_collabd.ui.picker_app.PickerApp`, a single-purpose Kivy
  app that hosts the shared `ProjectPickerScreen` +
  `LangPickerScreen` and implements the create-flow callbacks
  (`open_file` / `clone_dialog` / `show_start_over` /
  `new_from_template`) internally. Every successful flow ends in
  `_emit_and_quit(path, langcode='')`, which writes
  `AZT_PICK\t<path>\t<langcode>\n` on stdout and exits 0
  (or sets the Activity result on Android — see server APK below).
  Cancel / window-close exits 1.
- New `azt_collabd/ui/picker_app.py`. Hides the gear icon on the
  shared picker (no settings of its own), mounts the langpicker for
  Start New, drives `clone_project` and `create_project_from_template`
  on worker threads, surfaces errors via an in-window modal overlay
  (window stays open for retry).
- `azt_collabd/ui/app.py` (settings UI) trimmed: no more "Projects"
  NavBar button, no `ProjectPickerScreen` mount, no host-contract
  stubs. App now uses `_AZT_ICON` from `azt_collab_client/azt.png`.
- `server_apk/main.py` reads the launching Intent action; if it's
  `org.atoznback.aztcollab.PICK_PROJECT`, mounts the picker app
  instead of the settings UI. Same `PythonActivity` handles both —
  no second Activity declaration in the manifest required (the
  Intent action is matched by the existing PythonActivity entry +
  the new `<intent-filter>` line described in
  `azt_recorder/docs/p4a_hook_picker_intent.diff`).

### azt_collab_client 0.13.3 — pick_project UI-thread JNI dispatch
- Bug fix: `_pick_project_android` was building the
  `ActivityResultListener` proxy on the worker thread that called
  `pick_project()`. Worker threads attached by jnius' thread hook
  don't carry the app `ClassLoader`, so
  `find_javaclass('org/kivy/android/PythonActivity$ActivityResultListener')`
  fell back to the system loader (which has no app inner classes)
  and raised `JavaException: ClassNotFoundException`. Net effect:
  the recorder fired the `PICK_PROJECT` Intent, the worker thread
  died before `startActivityForResult` ever ran, the recorder's
  blocking modal stayed up forever showing only "Pick a project to
  continue. Cancel" with no picker Activity behind it.
- Fix: dispatch all JNI work (autoclass lookups, Intent build,
  `resolveActivity`, `bind(on_activity_result=...)`,
  `startActivityForResult`) to Kivy's main thread via
  `Clock.schedule_once`. The Kivy main thread is the Android UI
  thread, where the app `ClassLoader` is in scope and inner-class
  resolution works. The caller's thread only blocks on the result
  `Event`. Setup itself is bounded by a 10-second `Event.wait()` so
  a wedged UI thread can't hang the caller indefinitely.
- No API change; the function still returns the same dict shapes.

### azt_collab_client 0.13.2 — test_peer.sh fingerprint extraction
- `test_peer.sh` extends signing-fingerprint detection beyond
  keytool/dumpsys (both miss v2/v3-only signed APKs) with apksigner
  + openssl-on-META-INF fallbacks. Auto-discovers apksigner under
  `ANDROID_HOME` / `ANDROID_SDK_ROOT` / buildozer's bundled SDK at
  `~/.buildozer/android/platform/android-sdk/build-tools/*/apksigner`.
- Diagnostic chain now reports every tool that was tried so a WARN
  line tells you exactly which step failed (was: a single misleading
  message that reflected only the first failure).
- Strips `SHA[- ]?(256|1):` labels before regex extraction so the
  fingerprint match can't latch onto the `56` inside `SHA256:` and
  produce off-by-one bytes (was happening on the SUITE_FINGERPRINT
  file itself).
- Detects `CN=Android Debug` in apksigner output and reports
  `peer is signed with the Android Debug keystore; SUITE_FINGERPRINT
  check skipped (only meaningful for release builds)` instead of
  failing with a spurious mismatch. Debug builds of all suite peers
  share the same default Android debug keystore, so cross-app
  signature-permission gates still work; the suite-fingerprint check
  was only ever meant for release builds.

### azt_collab_client 0.13.1 — picker resilience
- `_pick_project_android`: pre-check Intent resolvability via
  `PackageManager.resolveActivity(intent, 0)` before
  `startActivityForResult`. Returns `server_apk_not_installed`
  immediately when no Activity matches, instead of relying on
  `ActivityNotFoundException` propagation through pyjnius (some
  OEM Android builds silently no-op the call instead of throwing).
- `done.wait()` capped at 10 minutes by default (was infinite). A
  launched-but-hung picker Activity can no longer wedge the caller
  forever; callers can still pass a smaller `timeout_seconds`.

### azt_collab_client 0.13.0 — pick_project()
- New `pick_project(timeout_seconds=None)` in `__init__.py`. Same
  shape as `open_server_ui()`: subprocess spawn on desktop
  (parses `AZT_PICK\t<path>\t<langcode>` from stdout), Intent
  dispatch on Android (uses `android.activity.bind` to wait on
  `onActivityResult`; falls back to
  `{'ok': False, 'error': 'server_apk_not_installed'}` if the
  server APK isn't installed). Sister apps in any toolkit drive
  project selection through this single helper.
- `azt_collab_client/ui/picker.py`: `register_kv` (a.k.a.
  `register_picker_kv`) gains a `hide_settings_gear` kwarg. When
  True the gear icon, its hit area, and the row containing it
  collapse to zero height — used by the standalone picker app
  which has no settings of its own.
- `azt_collab_client/azt.png` — suite icon shipped alongside the
  client. Both standalone Kivy apps (`ui` + `projects`) reference
  it via `os.path.dirname(azt_collab_client.__file__) + '/azt.png'`.
- `azt_collab_client/ui/assets/langtags_mini.json.gz` — moved from
  `azt_recorder/` (deferred item from step 2). The langpicker reads
  it from this default path, so sister apps no longer need to pass
  `langtags_path=` explicitly.

### Documentation
- New `examples/non_kivy_pick.py` — tiny demo of how a non-Kivy host
  drives project selection via `subprocess.run` + the AZT_PICK
  stdout protocol. Proves the cross-toolkit contract.

### Version constants unified
- `_VERSION` was duplicated as a hard-coded
  string in `server.py` (0.9.0) while `azt_collabd.__version__` lagged
  at 0.8.0. `__version__` is now the single source of truth at 0.9.0;
  `server.py` does `from . import __version__ as _VERSION` and all
  five wire-response references (server.json, started.json,
  /v1/health body, HTTP `Server:` header) flow from there.

### Documentation
- Added `azt_collab_client/CLAUDE.md` — a self-contained guide that
  travels with the client when sister apps symlink it in, so Claude
  Code working from a sister app's tree has full client / transport
  / API guidance without needing access to the canonical
  `azt-collab/CLAUDE.md`. The top-level `CLAUDE.md` now `@`-imports
  it to avoid duplication.
- README.md audited and rewritten to match the actual tree:
  removed references to the deleted `azt_collabd_plan.xml` /
  `azt_collabd_cleanup_drafts.xml`; added `server_apk/`,
  `azt_collab_client/ui/`, and `azt_collab_client/_spawn.py` to the
  layout; updated the sister-app symlink list to peer-only
  (`azt_collab_client` + `examples` + `android`, no `azt_collabd`);
  removed the stale "fall back to loopback" Android language and the
  per-peer `<provider>` instructions; added sections for the server
  APK workflow, the picker UI re-use story, the version handshake,
  and the new client API surface (`check_server_compat`,
  `init_project`, `derive_langcode`, `create_project_from_template`,
  `clone_project*`, GitHub install URL / device flow helpers, etc.).

## [0.8.0] — 2026-04-28 — `standalone_server_apk` cleanup-draft #3 (scaffolding)

Lays down the *scaffolding* for the standalone server APK
(`org.atoznback.aztcollab`) and the client-side changes that go
with it. The APK still has to be built and signed against real
devices; what is in this commit is the source tree, manifest, and
the client-side discovery + handshake the new architecture needs.

Per the user's answers in `azt_collabd_cleanup_drafts.xml`:

- q1: peer APKs symlink `azt_collab_client` only; `azt_collabd`
  lives only in the server APK and on desktop installs. The new
  `server_apk/README_NewClient.txt` documents the symlink set.
- q3: the server APK is the *only* component that calls
  `azt_collabd.configure(app_slug=..., client_id=...,
  collaborator=...)` — peers do not. The `server_apk/main.py` boot
  reads identity from env vars with the recorder defaults.
- q4: no persistent foreground-service notification by default;
  the server APK is allowed to be transient and respawns on the
  next peer query. `server_apk/service.py` keeps the always-on
  path behind `AZT_FOREGROUND_SERVICE=1` for opt-in.
- q5: client now exposes `check_server_compat()` and a
  `MIN_SERVER_VERSION` constant. Sister apps call it once at
  startup; an old server returns `{'ok': False, 'error':
  'server_too_old'}` so the peer can surface "Please update the
  AZT Collaboration service".

### New: `server_apk/`
- `buildozer.spec` — single-purpose APK targeting
  `org.atoznback.aztcollab`, requesting the suite signature
  permission, bundling the daemon and the Java provider glue.
- `manifest_extras.xml` — `<permission>`, `<provider>`, `<service>`
  declarations spliced into the generated manifest.
- `main.py` — Kivy entrypoint: configures GitHub App identity,
  registers the ContentProvider callbacks, opens the existing
  `azt_collabd.ui.app` settings UI as the launcher activity.
- `service.py` — opt-in foreground-service stub (off by default).
- `README_NewClient.txt` — peer-app integration guide (no
  `azt_collabd` symlink, no peer `<provider>` declaration,
  signature requirement, install-prompt + min-server-version
  flow).

### azt_collab_client 0.8.0
- `transports.android_cp.discover()` probes only the canonical
  server-APK authority `org.atoznback.aztcollab`. No `.aztcollab`
  suffix fallback (no peer-hosted daemons exist; we're building).
- `pick_transport()` on Android raises
  `ServerUnavailable('server_apk_not_installed')` when the server
  APK isn't reachable, instead of silently falling back to loopback
  (which can't work on Android — no Python interpreter to spawn).
- New `check_server_compat()` helper. Returns structured outcomes
  (`server_too_old` / `server_unreachable` / `ok`) suitable for
  driving an "update / install the AZT Collaboration service" UI
  affordance.
- `MIN_SERVER_VERSION` raised to `0.7.0`.

### azt_collabd 0.8.0
- Version constant aligned with the wider 0.8 baseline. No
  wire-format changes; older clients still talk to this server.
  The version bump is the signal a peer's `check_server_compat()`
  reads when it surfaces an upgrade prompt.

## [0.7.1] — 2026-04-28 — `wire_open_server_ui_button` cleanup-draft #2

The button-wiring itself lives in each sister app's `main.py`
(`../azt_recorder/main.py` for the recorder), which is outside the
canonical-source tree. What this repo can ship is the reusable
helper + documentation so each sister app's button is a one-liner.

### azt_collab_client 0.7.1
- New `open_server_ui()` helper. Desktop: spawns
  `python -m azt_collabd ui` detached and returns
  `{'ok': True, 'pid': ...}`. Android: returns
  `{'ok': False, 'error': 'desktop_only'}` until the standalone
  server APK lands and we can dispatch via Intent. Sister-app button
  code calls this helper, not subprocess directly, so the platform
  branching only lives here.
- Re-exported from `__all__`. `__version__` and `MIN_SERVER_VERSION`
  also re-exported.

### Documentation
- New "Wiring a sync-settings button" section in `README.md` with
  the KV + Python snippet sister apps can paste.
- Quick-reference snippet updated to mention `open_server_ui`.

### azt_collabd
- Unchanged at 0.7.0.

## [0.7.0] — 2026-04-28 — `android_contentprovider_transport` cleanup-draft #1

Closes the loose ends called out in
`azt_collabd_cleanup_drafts.xml` for the ContentProvider transport.
The transport classes, Java glue, and dispatch extraction were
already in place at 0.6.0; 0.7.0 hardens behavior when providers
come and go.

### azt_collab_client 0.7.0
- `rpc.call()` and `rpc.health()` now reset the cached transport and
  re-pick on `ServerUnavailable`. A provider host that gets killed
  mid-session falls through to loopback on the next call without
  the host having to restart. Symmetrically, a provider appearing
  after a loopback startup will be picked up the next time the
  client's transport cache is invalidated.
- `transports.current_transport_name()` exposes which transport is
  in use (``loopback`` / ``android_cp``) for diagnostic surfaces.
- `__version__` and `MIN_SERVER_VERSION` are now defined at the
  package root for sister apps to read.

### azt_collabd 0.7.0
- Version constant aligned with client. `_VERSION` bumped to 0.7.0
  in `azt_collabd/server.py` and a matching `__version__` exposed
  from the package.
- No wire-format changes; clients < 0.7.0 keep working unchanged.

## [0.6.0] — pre-cleanup baseline

Snapshot of the state at the end of the 16-step migration plan
(`azt_collabd_plan.xml`). Cleanup drafts pick up from here.

### azt_collabd 0.6.0
- Loopback HTTP server with bearer token + flock single-instance guard.
- Transport-agnostic `dispatch(method, path, body)` in `server.py`.
- Connectivity watcher and debounced `request_sync` job queue.
- LIFT-aware three-way merge by `<entry guid>` with side-by-side
  conflict preservation.
- Per-project advisory `flock` locking, reentrant within a process.
- pyjnius shim (`azt_collabd/android_cp/service.py`) routing
  ContentProvider calls into the dispatch table; `openFile` streaming
  scoped to `$AZT_HOME/projects/`.
- Crash-log tail returned in `/v1/health`.

### azt_collab_client 0.6.0
- Pluggable `Transport` ABC; `pick_transport()` chooses Android
  ContentProvider when reachable, else loopback.
- Loopback transport spawns the daemon on demand, retries on
  `SERVICE_RESTARTED`.
- Decode-only `Status` / `Result` / `Project` / `ProjectStatus`.
- `translate_status` / `translate_result` for UI display; default
  English + French maps.
