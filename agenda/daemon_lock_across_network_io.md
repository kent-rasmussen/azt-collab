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

  **Amendment 2026-07-28 (evening): the WAN half is NOT primarily a
  network-I/O problem, and the phase-split alone will not fix it.**
  Watchdog, desktop, during the 816-commit `nml` topic push —
  `project_lock 'e1081fb4390d0ac0.lock' held 145s`:

  ```
  _h_test_github → drain_pushes_now → _drain_pending_push → _attempt_push
    → push_repo → _push_repo_locked → _push_step_locked
    → _push_chunked_to_ref → _pick_intermediate_sha → _is_ancestor
    → repo[sha] → object_store.get_raw → pack.read_zlib_chunks
  ```

  **No network frame anywhere in that stack.** It is parked in
  `read_zlib_chunks` on a LOCAL pack file: `_pick_intermediate_sha`
  calls `_is_ancestor` per candidate to choose chunk boundaries, and
  each call walks history and decompresses pack objects. Across ~816
  commits that behaves quadratically, hence minutes rather than
  seconds.

  Contrast with the 2026-07-27 stall on the same lock, which WAS
  network (`lan-reverse-deliver` parked 147 s in `ssl.read`). Two
  different causes wearing one symptom, which is why "hold the lock
  across network I/O" undersold the problem.

  So the WAN work splits in two:
  1. **Hoist the transfer out of the lock** (the original plan, still
     right for the send/receive phase).
  2. **Make chunk-boundary selection cheap** — one ordered walk, or
     generation numbers / commit-graph lookups, instead of repeated
     pairwise `_is_ancestor`. Without this, the lock is still held for
     minutes on a large push even with the transfer hoisted out, and
     every LAN post-receive reset behind it keeps timing out at 5 s.

  **This item is why the big project never finishes backing up — raise
  its severity accordingly.** The long lock hold has three surfaces,
  not one:

  - AZT saves stall or return `BUSY` (the symptom that opened the item);
  - LAN post-receive resets time out at 5 s (all day 2026-07-28), so
    received data sits unabsorbed;
  - **the watchdog restarts the daemon over the push.** Field, ten
    minutes into the 816-commit `nml` push:

    ```
    [watchdog] stall persisted 625s — restarting the daemon
        (project lock … held 625s by 'Thread-54'; … held 313s by 'Thread-356')
    ```

    A large first push cannot complete inside `watchdog.restart_s`
    (600 s), so every attempt was killed, marked interrupted, and
    re-entered the backoff curve — looking identical to a network
    failure from outside. Mitigated in 0.55.73 by making the watchdog
    honour `sync_flight.in_flight()`, but that is a guard around the
    symptom: while the lock is held for minutes, everything queued
    behind it still suffers.

  Related same-day finding, fixed in 0.55.73: a `BUSY` (lock-timeout)
  push result was charged to `wan_backoff` **and** recorded as a GitHub
  *access* error, so self-contention inflated the curves and produced a
  misleading remote diagnosis. Suspected origin of the large failure
  counts (`baf` 25, `en` 178) that made projects look network-cursed.

- **Deadline:** none — but this is a **confirmed field regression**,
  needs ranking.
- **Waiting on:** Nothing.

## The watchdog dump the item asked for — 2026-07-31, NDEMLI

This item said to deploy and read the watchdog's stack dumps first,
because they name the actual lock holder. They now have:

```
[watchdog] STALL DETECTED: project lock '9a6971668a565870.lock'
           held 131s by thread 'wan-drain'

_drain_pending_push → _attempt_push → _push_repo
  → repo.py:5405 push_repo → _push_repo_locked
  → repo.py:5501 _push_step_locked
  → repo.py:9406 porcelain.push → client.send_pack
  → urllib3 → http.client.send → ssl.write
```

So: **`_push_step_locked`, holding `project_lock` for the entire TLS
upload** — 131 s on that occasion, and only that short because `nml`
was 169 commits rather than 925. The lock is released when the upload
finishes, not when the local work does.

Note the contrast in the same log: `phase-A begins WITHOUT the project
lock (0.55.84) — local work stays responsive while this uploads`. The
topic-push path already got the phase split. **`_push_step_locked` —
the direct-push route — did not.** That narrows the work: the pattern
exists and is proven, it just was not applied here.

Also confirms the cost is real rather than theoretical: any save
arriving in that 131 s window blocks or returns BUSY, which is the
"azt clients freeze when wifi internet is on" report.

## Field wedge with a silent log (2026-07-27)

Four Android devices tethered to the dev desktop. Symptoms, in the
order Kent hit them:

1. OS "'Python (v3.13)' Is Not Responding / Force Quit" on the daemon
   settings UI (main-thread RPCs — fixed 0.54.86, but the *reason*
   they blocked is this item).
2. A dozen `SERVICE_RESTARTED (connection failed: timed out)` lines in
   minutes — which turned out to be a false signal (fixed 0.54.87:
   the daemon was alive and answering `/v1/health`, so nothing had
   restarted; loopback calls were simply timing out).
3. **None of the four phones listed the computer**, despite every one
   of them having a live USB link and multiple addresses.

**The log is the tell: it stops dead at 08:24:57** and records nothing
afterwards, while the UI polled every 5 s and timed out. Immediately
before the silence, a phone at 192.168.31.187 fetched `/baf.git` and
`/nml.git` successfully — so the listener was serving normally right up
to the wedge.

Diagnosis: the daemon process is **alive and answering the lock-free
`/v1/health`, but wedged for everything else** — including its
zeroconf advertise/browse threads, which live in the same process.
That single fact explains all three symptoms at once, and it explains
why phones can still *fetch* (they hold a cached endpoint) while no
longer *discovering* (no advertisement is going out).

Watch for on the next occurrence: whether the log silence and the
timeouts start at the same instant (one wedge) or the log dies first
(the separate 2026-07-08 "tee starvation" failure, where writes stopped
~80 s after startup while serving continued). Distinguishing them
decides whether logging needs its own fix.

**Progression observed, and it names a mechanism.** The client errors
changed shape as the evening went on: first `timed out`, later
`Remote end closed connection without response`. Accepted-then-dropped
is not slowness — it's what `ThreadingHTTPServer` does when it can no
longer serve: a thread per request, blocked handlers accumulating,
until `Thread.start()` fails or fds run out (cf. 0.54.1 EMFILE) and new
connections are accepted and dropped with no reply. So the likely chain
is **blocked handlers → thread/fd exhaustion → total wedge**, with the
initial block being this item's lock-across-network-I/O (or a
GIL-saturating whole-LIFT merge).

### Why the addresses were stale (answered 2026-07-27)

Kent: *"why would the USB addresses be stale? are those that haven't
completed connection?"* No — they're the peers we've heard **no mDNS
announcement from this session**. Grepping the day's log,
`74453504` and `841d43a8` appear ONLY in outbound dials, never in an
`add` line, while the two reachable phones appear in `add` lines
repeatedly with stable ports. So the only address held for them is
the one persisted from a previous session, and nothing refreshed it.

The two failures are diagnostically different, and the distinction is
worth keeping:

- **connect timeout** (`10.143.126.7:41455`) — that IS a live tether
  subnet (this desktop is `10.143.126.171`), so the route exists and
  nothing answered: the phone is cabled but its daemon isn't listening
  there. Candidates: daemon not running, or it re-bound to a different
  ephemeral port. **Watch item:** every phone showed an ephemeral port
  (40975 / 38141 / 41455 / 43009) rather than 34501, so if the
  `lan_listener_port` memo doesn't persist on Android, every phone
  daemon restart invalidates every cached endpoint.
- **`No route to host`** (`192.168.31.240:40975`) — the device isn't on
  that subnet at all. Cheap to discover (~3 s) versus the timeout's
  ~15–30 s.

### STATUS 2026-07-28: LAN half shipped, WAN half open

- **LAN path — DONE (0.55.24).** `_merge_then_push` fetches in
  `_fetch_peer_objects_unlocked` with no lock held; `project_lock` now
  covers only merge + ref update + reset + push. This was the holder the
  watchdog caught (147 s in `ssl.read`). Verify per the greps below.
- **WAN path — NOT DONE, and it is the original repro.**
  `repo.sync_repo` takes `project_lock(project_dir)` with **no timeout**
  and calls `_push_step_locked` (github fetch + push) plus
  `_push_extras_step` inside it. A dead github connection therefore holds
  the project for the full network timeout while every local operation
  queues — which is exactly the 2026-07-22 "AZT freezes on bad network"
  evidence this item was opened for.

  Split shape (same as LAN, one more stage): commit **locked** → fetch
  **unlocked** → merge **locked** → push **unlocked**, against a head
  captured under the lock → update tracking refs **locked** (cheap).
  Check `push_repo` / the drain path for the same pattern.

**Verifying the LAN half** — two devices, diverged project, one peer on a
dead or slow link. Pass = no `post-receive reset … lock busy (5s
timeout)` storm, no `[watchdog] STALL DETECTED … held Ns by thread
'lan-reverse-deliver'`, and `[lan-merge]` still reaching a conclusion.
`[watchdog] stall cleared` on 2026-07-27 shows recovery, not absence — a
clean run with a deliberately unreachable peer is the real test.

### PROVEN 2026-07-27 22:07 — the stack, at last

```
[watchdog] STALL DETECTED: project lock '60206912458536ae.lock'
           held 147s by thread 'lan-reverse-deliver'
  lan_listener.py:808   _work
  lan_push.py:355       _push_to_peer
  lan_push.py:871       _merge_then_push
  lan_push.py:986       _merge_then_push_locked
  dulwich/client.py:1671 fetch
  …  _handle_upload_pack_tail → _read_side_band64k_data
  …  urllib3/response.py:1005 _fp_read → ssl.py:1167 read
```

**`_merge_then_push_locked` holds `project_lock` across a network
fetch** and sat in `ssl.read` for 147 s. No longer a hypothesis — this
is the exact regression this item was opened for, with a stack. It is
also the direct cause of the `post-receive reset 'nml': lock busy (5s
timeout)` line repeating every 20–40 s for minutes on every device
tonight: inbound data lands and can never be absorbed because an
outbound merge is parked on the lock inside a socket read.

Fix shape (unchanged from the design sketch, now confirmed as the right
target): split the phases so the lock covers only local object/ref/
working-tree mutation, never the fetch or the push. Fetch into the
object store unlocked (objects are content-addressed, so a concurrent
fetch is safe), then take the lock for merge + ref update + reset.

**Second finding in the same dump — `_has_internet` blocks the watcher
in DNS:**

```
  scheduler.py:981  _watcher_loop
  net.py:399        _has_internet
  socket.py:827     create_connection
  socket.py:962     getaddrinfo        ← blocked here
```

With no upstream, `getaddrinfo` can block for many seconds on Android,
and it is doing so **on the watcher thread** — the same loop that runs
`_drain_pending_push` and `drain_pending_resets`. So the offline case
stalls the very loop that retries the lock-busy resets. 0.55.23 stopped
the *RPC* from paying this, but the poll still does. Per
[[feedback_offline_is_a_supported_state]] the offline path must be the
cheap one: probe a numeric IP (no DNS) and/or set an explicit short
socket timeout. Small, independent of the lock work, and worth doing
first.

### Wedge detector + self-recovery (BUILT 0.54.89–0.54.90)

Shipped 0.54.89 on `/v1/health`: `threads`, `fds`, `locks_held`
(holder + held_s, from `locks.py` at depth 1), `heartbeats`.

Shipped 0.54.90: `azt_collabd/watchdog.py` — diagnose then recover.
Stall (heartbeat or held lock past `watchdog.warn_s`, default 120 s) →
one summary line + thread/fd counts +
`faulthandler.dump_traceback(all_threads=True)`, once per episode.
Persisting past `watchdog.restart_s` (default 600 s) → restart via the
admin-restart mechanism, rate-limited, with a 90 s startup grace.
`iface-watch` heartbeats at ~3 s (the most sensitive signal). Wired
into `server.serve` AND `server_apk/service.py:main`.

**First field catch, 2026-07-27 13:24 — detector fired, dump didn't
(FIXED 0.55.12).** On the tablet:

```
[watchdog] STALL DETECTED (first detection): loop 'watcher' last ticked 120s ago
[watchdog] threads=10 fds=277
[watchdog] --- all thread stacks follow ---
[watchdog] traceback dump failed: AttributeError("'ProcessingStream' object has no attribute 'fileno'")
```

`faulthandler.dump_traceback(file=…)` needs a real fd; Android's
`sys.stderr` is Kivy's `ProcessingStream`. So the one platform where we
have no shell access was also the one where the dump could never print.
0.55.12 replaces it with `_dump_all_thread_stacks()` —
`sys._current_frames()` + `traceback.format_stack`, printed line-by-line
through the normal log path (which also gets it into the shared
per-day log).

**So this item's precondition is not yet met: there is still no stack
dump to read.** It needs one stall on a build ≥ 0.55.12. Surrounding
log context from that episode, for whenever the dump arrives:

- 13:22:47 push to `8f19208f` → `[Errno 113] No route to host`;
  sweep `0/2 delivered`.
- 13:22:52 hello to `192.168.31.71:54154` → connect timeout (5 s);
  13:22:58 share-offer to the same → connect timeout; sweep aborts
  remaining projects (0.55.x unreachable-abort behaving as designed).
- 13:24:01 stall detected — `watcher` last ticked 120 s ago, i.e. the
  tick before ~13:22:00, straddling that run of 5 s connect timeouts.
- 13:24:28 a post-receive reset for `nml` completes and the loop
  resumes; so this episode self-cleared well inside `restart_s`.

That timing is consistent with the hypothesis this item exists for —
serialized network waits inside the watcher's critical section — but
consistent-with is not evidence-of, and the dump is what would name it.

**Answering "are all causes visible?" — no. Remaining blind spots:**

- **No heartbeat** on the listener/serve loop, the advertise thread, or
  the log writer. A stall confined to one of those is invisible unless
  it also blocks a lock or another loop. The listener is the awkward
  one: `serve_forever` blocks in `select`, so "alive" can't be bumped
  from inside the loop — it needs either a per-request stamp (which
  goes stale legitimately when idle) or a synthetic self-probe.
- **The log writer can't monitor itself.** If the tee dies (2026-07-08
  precedent), the watchdog's own output dies with it — the failure
  would present as total silence, indistinguishable from a dead
  process. An out-of-band marker file touched per tick would cover it.
- **GIL saturation** from an O(whole-LIFT) merge presents as slowness,
  not a stall: loops keep ticking, just late. Needs per-operation
  duration logging to separate "working hard" from "stuck".
- **`_push_to_peer`'s internal peek retry** is still unbounded (two
  connect timeouts per dial), so 0.54.89's one-dial-per-sweep is
  really ~2 timeouts per sweep.

### Original design notes (2026-07-27)

Kent: *"Can we determine a wedge programmatically? Any idea why and how
to prevent it?"* A wedge is by definition "the lock-free path answers,
the working paths don't", so the detector is a comparison:

- **Loop heartbeats in `/v1/health`** — monotonic timestamps bumped by
  the watcher tick, scheduler tick, listener bind, last advertise, last
  log write. Health then means "these loops ticked N s ago" rather than
  "the socket accepted", and a stale heartbeat beside a live HTTP
  thread IS the signature.
- **`threading.active_count()` + open-fd count** — the two cheapest
  fields, and either one would have identified the 07-27 exhaustion
  immediately.
- **Lock table** — `locks.py` is the single acquisition point, so
  recording holder + held-duration per `project_lock` and exposing it
  names WHICH operation is stuck.
- **Internal watchdog thread** — on any loop going stale past a
  threshold, log one line plus `faulthandler.dump_traceback()` (stdlib,
  all thread stacks). This is how the culprit gets identified without a
  debugger, on a machine we don't have.
- **Client half already exists:** `service_health()` is lock-free while
  a real RPC isn't; disagreement is reported as `SERVICE_SLOW` /
  `SERVICE_DROPPED` (0.54.87/.88).

Prevention, in payoff order: the phase-split this item specifies;
a timeout on EVERY lock acquisition (a deadlock degrades to typed BUSY
instead of a hang); connect+read timeouts on every socket path; a cap on
concurrent request threads with 503 beyond it, so one stuck operation
can't consume the pool. Stopgap while that lands: let the watchdog
restart the daemon itself when a loop goes stale — restart is already
designed safe (jobs → `JOB_INTERRUPTED`, transfers retried, listener
re-binds its port), the same reasoning as the 0.54.77 local
auto-restart, turned inward.

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

  - **FIXED 0.54.91.** Three things were side-dependent, not one:
    wall-clock time, parent order (`merge_heads` puts our HEAD first,
    so A wrote `[A,B]` and B `[B,A]`), and the message itself
    ("Local commits:" flips between peers). All three now canonical —
    stamp from max(parent times), sorted parents, and
    `build_canonical_merge_message`. Cost accepted: first-parent
    convention is sacrificed (`git log --first-parent` may follow the
    other device's line); ancestry and all coverage walkers consider
    every parent, so correctness is unaffected. Written via an explicit
    `Commit` object since the worktree API can't express sorted
    parents, with fallback to the old path on any unexpected dulwich
    shape.
  - **Reprioritisation that followed (Kent 2026-07-27):** with merges
    no longer manufactured by two devices merely meeting, the
    both-sides-changed merge only runs when two people genuinely edit
    concurrently — so the O(whole-LIFT) incremental rewrite (the
    riskiest change on this item, touching the truncation guards and
    the duplicate/gloss rules) drops well down the list. Do it only if
    field evidence shows real concurrent editing making it hurt.
  - **Original diagnosis, kept:** `_merge_diverged`
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
