# Don't hold project_lock across network I/O (AZT-freezes-on-bad-network regression)

- **Scope & relationships:** azt_collabd (daemon). The client/server
  paradigm made every AZT read/write a daemon RPC that contends for
  `project_lock`; the daemon holds that same lock **across network
  transfers** (WAN push, LAN merge-then-push). On a bad link those
  transfers run long (5–30 s timeouts, multi-minute packs, a 55-min
  merge observed), so a user save that needs the lock stalls or returns
  `BUSY` — AZT's UI pauses. Pre-client, AZT read/wrote the LIFT on local
  disk and never waited on any background network op. This is the
  regression behind the field reports of "AZT unresponsive on bad
  network." Related: [[sync_status_board]] (visibility), USB transport
  (a workaround path), and the LAN merge-loop finding below.

- **Vision / done-criteria:** an AZT save/read never waits on a
  network transfer. `project_lock` is held only for **local** git work
  (stage, commit, ref read, pack build); the actual send/receive
  happens outside it (or under a distinct transfer-lock that
  user-facing RPCs don't take). Reads never take the write lock.
  User-facing write RPCs use a short lock timeout and return a clean,
  client-handled "busy, retry" — never a 300 s hang. Result: a fast
  machine on a bad link stays responsive.

- **Deadline:** none — but this is a **confirmed field regression**,
  needs ranking.
- **Waiting on:** Nothing.

## Evidence (2026-07-22)

- `_run_to_completion` (scheduler.py) explicitly yields `project_lock`
  at a wall-clock deadline "so a waiting user Sync / commit isn't
  starved with BUSY" — i.e. the push path is *known* to hold the lock
  across the transfer.
- Field log: `[submit_file] 'nml' done: codes=['BUSY']` while the WAN
  push held the lock; a 55-min convergence merge held it the whole
  time (that was also the O(files×history) resolver, fixed 0.54.31 —
  but a large legit merge still holds it for its duration).
- HTTP layer is NOT the bottleneck — daemon uses `ThreadingHTTPServer`
  (per-request threads). The contention is purely `project_lock`.

## Design sketch (to validate before building)

1. **Split the lock's phases.** Under `project_lock`: read refs,
   build/snapshot the pack (or the merge inputs). Release. Do the
   network transfer WITHOUT the lock. Re-acquire briefly only to
   advance refs / write the merge result — and re-check HEAD didn't
   move (retry the small tail if it did).
2. **Reads off the write lock.** Audit every user-facing read RPC
   (`project_status`, LIFT/audio reads) to confirm none block on the
   write lock; the status-poll config-write already uses a 2 s timeout
   (invariant #11) — extend that discipline.
3. **Short, honest busy on writes.** `submit_file` / `commit_project`
   should fail fast with a typed BUSY the client surfaces as "saving —
   retrying" rather than a long stall; never inherit the 300 s
   `rpc.call` default on the UI path.

**Correctness caution:** `project_lock` exists to keep concurrent git
mutation from corrupting the repo. Releasing it mid-operation must not
let two writers stage/commit into the same tree simultaneously. The
snapshot-transfer-reacquire pattern (send is read-only against a fixed
pack; only the ref advance needs the lock) is the safe shape; a naive
"just drop the lock during push" is not.

## Effort

Medium–large, correctness-critical. Touches `repo.sync_repo` /
`_push_repo`, `lan_push._merge_then_push`, and the scheduler drain.
Needs a 2-device test (writer + puller) on a throttled link to prove
the UI stays responsive during a push.

## Related finding — LAN merge ping-pong / non-convergent merge commits (2026-07-22, re-diagnosed 2026-07-23)

Surfaced during a bulk-ASR run. **Diagnosis settled by the commit
graph (Kent 2026-07-23), after two wrong turns of mine.** The graph
(`git log --graph` on the desktop nml) shows phone "Audio recordings
by itservices-hue" commits (e.g. `28aae638`) and desktop "A-Z+T edit"
commits (e.g. `1131b6d3`) BOTH branching from the same base
(`33e62566`) — i.e. **two writers committed concurrently**, which
correctly requires a merge. So there is NO "spurious merge on a passive
receiver" bug (that was my wrong theory) and NO "github anchor required"
requirement (also retracted — git converges peer-to-peer). The phone
WAS recording when those commits were made (the crew's session, before
Kent wiped + re-cloned it; wiping the phone can't unwind commits the
desktop already merged). The real defects, all visible in the graph as
a criss-cross staircase of merge commits that never collapses:

  - **Non-deterministic merge commit identity.** `_merge_diverged`
    produces a deterministic merge *tree* (same inputs → same tree, by
    design), but the merge *commit object* is created via
    `get_worktree().commit(...)` with the wall-clock time, so two peers
    independently merging the same two parents produce commits with the
    same tree but **different SHAs** (different commit_time). Neither
    can fast-forward to the other → `DivergedBranches` → each re-merges
    → forever. This is THE ping-pong. Fix: make the merge commit
    reproducible — fixed committer identity (already `bot_identity()`)
    + a deterministic `commit_time` derived from the inputs (e.g. max
    of parent times) + fixed tz + stable message. Then peer A and peer
    B merging (P1,P2) mint byte-identical commits → same SHA → they
    converge with no central anchor. This is the "git is democratic"
    property Kent wants; the daemon currently breaks it by stamping
    wall-clock time into merge commits.

  - **Merge is O(whole tree/LIFT), not O(changed).** Each round
    re-walks the 3050-file tree and re-normalizes all 1700 entries
    (~20–40 s) even at `conflicts=0`. Independent of the SHA bug, this
    is too slow to keep pace with a commit stream and is the blocker
    for Kent's requirement (2026-07-23): **the phone should take ASR
    updates live, as they come.** Needs an incremental merge (only
    entries changed since the last common commit). Adjacent lever: ASR
    writer commits through the debounced `commit_project` (coalesces at
    500 ms) instead of an immediate `submit_file` per transcription.
    - **SHIPPED 0.54.32 — quick-win cheap-no-op:** the `.lift` branch
      in `_merge_diverged` fired the full `three_way_merge` whenever
      `o != t`, and the cheap `o == b` / `t == b` / `o == t` fast-paths
      sat AFTER it (unreachable for `.lift`), so a merge where only one
      side touched the lexicon still parsed + normalized all 1700
      entries. Hoisted the three fast-paths above the special-case
      branches (slots / kv / .lift); the heavy merge now runs only for
      the genuine both-sides-changed case (`o != t and o != b and
      t != b`). Behaviorally identical, purely a cost cut on the
      highest-frequency path. True per-entry incremental (for the
      both-sides-changed case — only re-parse/normalize changed
      entries) remains the larger follow-on.

  - **Misleading merge message.** Every merge commit reads "Merge
    origin/main into main" even when GitHub is unreachable and the
    merge is a LAN-peer merge (`merge_commit.build_merge_message` uses
    a hardcoded source string). This sent the 2026-07-23 diagnosis
    chasing origin/anchor red herrings. Fix: label the actual source
    (LAN peer id / device name) so logs and `git log` tell the truth.

No data loss in the observed loop (`conflicts=0`, 1700 entries intact)
— it's wasted churn + battery + history bloat, not corruption.
Immediate field mitigation while unfixed: unshare the project from the
passive peer during a bulk-ASR run, or accept the harmless churn.
