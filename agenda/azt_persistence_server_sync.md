# AZT persistence ↔ server: sync the read/write contract + expand daemon capabilities

- **Scope & relationships:** azt-collab/daemon (+ cross-cutting design into azt). The
  **server-side half** of the AZT integration: the persistence-sync contract, plus the
  daemon capabilities that contract requires. Pairs with [[azt_run_with_server]] (the
  azt-app-side wiring). Sibling: [[usb_backup_transport]] (transport parity; DECIDED
  2026-07-06 on bare-repo-on-drive, Phase 1 ≈ 1 day). Do this item's daemon work *before or
  alongside* azt_run_with_server.
- **Vision / done-criteria:** a documented persistence-sync contract for AZT, the new
  daemon endpoint(s) it requires shipped and covered by tests, the contract folded into
  `CLIENT_INTEGRATION.md`. AZT's edits persist and converge through the daemon without
  data loss under concurrent merges.
- **Deadline:** before [[azt_run_with_server]] ships (prerequisite).
- **Waiting on:** Nothing

## Decisions (Phase 0 output, locked with Kent 2026-07-06)

- **D1 Project home — hybrid.** Existing projects are **adopted in place** (registry
  already takes arbitrary `working_dir`; `_init_repo_locked` is safe on a dir that already
  has a `.git` — emits `ALREADY_INITIALIZED`, preserves history). Projects created after
  the conversion default to daemon-owned working trees under `$AZT_HOME` (recorder model).
- **D2 Legacy VCS.** Projects on Mercurial or non-GitHub git remotes never opt into collab
  mode; azt's legacy vcs.py path keeps serving them until they retire. **Mercurial is
  dropped** from the future entirely (Kent: never used, never will). Daemon stays
  GitHub(+LAN+USB) for now; broader git-remote support is planned server-side later.
- **D3 Cutover — per-project explicit opt-in.** A "Connect this project to Collaboration"
  action registers the project and flips a per-project setting. Everything else runs the
  legacy path bit-for-bit. Rollback = flip the setting back.
- **D4 Mid-session merges — check-before-write + reload.** Every azt save is **base-aware**:
  it declares the `head_sha` it was loaded/last-synced against. Fast path (HEAD unchanged)
  commits directly; divergent path reuses the existing LIFT three-way merge so peer data is
  never clobbered, then azt reloads the merged result. Requires one new endpoint (below) —
  the survey confirmed **no CAS/expected-HEAD mechanism exists anywhere** in the daemon.
- **D5 Backup/compressed variants** (`writebackup` daily `.txt`; `writegzip`/`writelzma`
  have **no live callers** — survey): keep them writable (users email them) but
  **gitignored**. The daemon's generated `.gitignore` does NOT currently cover them and
  `_stage_all` is whole-tree `git add -A` — gap G3 below closes this.
- **D6 Non-LIFT artifacts.** Keep azt's existing deliberate add-to-git paths (alphabet
  chart/report `force=True` adds); derived outputs (PDFs, exports, reports/, XLP temp)
  stay ignored, mirroring azt's own `Git.ignorelist()`.
- **D7 langcode.** Settings-authoritative (azt already persists `analang` in the project
  settings JSON after first-run inference). Passed once at `register_project`; never
  re-inferred while registered. Same model as Android.
- **D8 Contributor/device_name.** Seeded once at opt-in from the project's git config
  (azt's current author source), then daemon-owned (`set_contributor`/`set_device_name`).
  Daemon composes the author; azt stops injecting `-c user.name=…` in collab mode.
- **D9 Daemon-absent posture.** Robust daemon is the goal, but a field tool must never
  lose the ability to save: if the daemon can't spawn at open, azt offers a clearly-badged
  legacy-mode session for that project. Zero regressions in recorder↔azt data flow is the
  acceptance bar.

## The persistence contract (draft v0 — to land in CLIENT_INTEGRATION.md as the "desktop whole-file editor" section)

1. **Registration.** Opt-in calls `register_project(langcode, working_dir, lift_path)`
   with langcode from azt settings and working_dir = the LIFT's directory (D1). The
   daemon adds azt's ignore patterns to `.gitignore` at adopt time (G3).
2. **Read.** One-shot at startup via `LiftHandle.open_read()`. azt caches the project's
   `head_sha` at load — this is its **base**.
3. **Write (the new core).** azt keeps its existing atomic discipline — serialize whole
   LIFT to a staged sibling file (`.part`) — then, instead of `os.replace` + its own git
   commit, calls the new **`submit_file`** RPC (G1) with `{path, staged_path, base_sha}`.
   Daemon, under `project_lock`:
   - `HEAD == base_sha` (normal case): `os.replace` staged→dest, commit, return new
     `head_sha` (+ `COMMITTED_LOCAL`). azt updates its base. No merge, no copy — same
     filesystem, zero-cost handoff.
   - `HEAD != base_sha` (a merge landed since azt's base): three-way LIFT merge — base =
     the `.lift` blob at `base_sha`, ours = blob at HEAD, theirs = staged bytes — via the
     existing `lift_merge.three_way_merge` (+ truncation guards), write merged tree, merge
     commit, return new `head_sha` + a new status code (`MERGED_WITH_LOCAL`). azt marks its
     in-memory db stale, defers further writes, and reloads at the next safe point.
   This closes the clobber race *by construction*: correctness never depends on how fresh
   azt's poll is. It also removes azt's unlocked direct writes into a working tree the
   daemon may hard-reset after a LAN receive (the two current guards in
   `_reset_working_tree_after_receive` become a second line of defense, not the only one).
4. **Commit cadence.** LIFT commits ride `submit_file` (one commit per save; azt-side
   coalescing keeps sort-session churn sane — azt half). Non-LIFT artifacts (settings
   JSONs, audio, chart adds) are picked up by whole-tree staging at the next commit, or
   explicitly via `commit_project` at task boundaries/shutdown. Server debounce (500 ms,
   same job_id per burst) stays as is.
5. **Re-read obligation (§17b applied to desktop).** azt polls `project_status(langcode)`
   every 5–15 s; when `head_sha` ≠ cached base → schedule an in-place reload at the next
   safe point, restore the user's anchor. Poll staleness only delays *seeing* peer edits —
   it can never lose them (see 3).
6. **Push.** Daemon-owned: scheduler drain loop + user Sync gesture (`sync_project`) +
   shutdown sync replacing legacy `share()`. azt never touches `.git` in collab mode.
7. **Identity.** Per D7/D8.
8. **Resilience.** Mid-session `SERVICE_RESTARTED` / `JOB_INTERRUPTED` are retryable —
   re-issue once, then surface. Daemon-unavailable at open → D9 legacy-mode session.

### Impacts of the poll-(not-push)-based re-read decision (Q2 follow-up, as requested)

- **Chosen: poll.** Desktop loopback has no push channel (ContentObserver is
  Android-only; nothing notifies a loopback peer). `head_sha` is read straight off
  dulwich refs in `_h_project_status` — no status walk, so a 5–15 s poll is cheap.
- **Correctness impact: none.** Because every write is base-aware (contract §3), a stale
  poll can't cause data loss; the merge happens server-side regardless. The poll interval
  only bounds how long azt displays stale peer data before reloading.
- **Cost:** one HTTP GET per interval per open project (negligible); note
  `_h_project_status` also runs `strip_lan_origin_if_present` under a 2 s-bounded lock on
  every poll — already the recorder's cadence, no new load profile.
- **Rejected: notify/long-poll endpoint.** Would add a daemon capability + a client
  thread to save at most one poll interval of display staleness. Revisit only if field
  use shows the staleness matters.

## Plans

**Phase 1 — daemon capability additions** (each via the "When adding a new client API
call" checklist: server.py dispatch → client wrapper → status codes in both mirrors →
translation → `MIN_CLIENT_VERSION` bump for wire-format adds):

- **G1 `submit_file` — base-aware atomic whole-file commit** (the one genuinely new
  endpoint). `POST /v1/projects/<lang>/submit_file {path, staged_path, base_sha}`,
  semantics per contract §3. Reuses `_resolve_atomic_commit_path` whitelist +
  `project_lock` + `lift_merge.three_way_merge` + truncation guards. New codes:
  `MERGED_WITH_LOCAL` (divergent path taken), reuse `COMMITTED_LOCAL` /
  `NOTHING_TO_COMMIT` / `CONTRIBUTOR_UNSET`. **Result must carry the new `head_sha`** so
  azt updates its base without an extra status poll. Desktop-first (staged_path is a
  filesystem handoff); Android peers don't need it (surgical writes).
- **G2 `head_sha` in commit/sync Results.** Today no commit-shaped Result reports the new
  HEAD (survey). Add `head_sha` param to the terminal status of `commit_project` jobs and
  `sync_project` Results so any peer can maintain its base cheaply.
- **G3 Adopt-time `.gitignore` hardening.** On `register_project` (or a small
  `adopt_ignores` step in it), append azt's patterns if absent: `*.gz`, `*.7z`,
  `*lift*txt` (daily backups), `reports/`, `exports/`, `XLingPaperPDFTemp/`, `*.pdf`,
  `userlogs/`, `excess/`, `images/archive/`, `images/scaled/`, `*backupBeforeLx2LcConversion`
  (source: azt `vcs.py Git.ignorelist()` + D5/D6). Without this, whole-tree `add -A`
  commits every emailed-backup variant.
- **G4 `register()` duplicate-working_dir guard.** Survey: `register()` has NO
  same-working_dir/different-langcode collision check (the ~294 check is in `rename()`),
  and `find_langcode_by_working_dir` returns first-hit — nondeterministic under a dup.
  Refuse (typed error) a second langcode for an already-registered working_dir.
- **G5 (confirm-only, likely no code)** — `atomic_open_write` filesystem branch already
  matches azt's `.part`→`os.replace` discipline; `open_project`/`register_project`/
  `derive_langcode` cover D7; `get/set_contributor` + `device_name` cover D8. Pruned from
  the old candidate list: no new endpoint needed for re-read (poll `head_sha`), none for
  bulk (debounce + azt-side coalescing + G1), none for variants/artifacts (G3 covers).

**Phase 2 — consistency tests** (`azt-collab/tests/`, LIFT fixtures + temp `$AZT_HOME` +
spawned loopback daemon — none exist yet, build from scratch):
1. `submit_file` fast path: bytes on disk == submitted; one commit; Result carries head_sha.
2. `submit_file` divergent path: seed base commit → simulate peer merge advancing HEAD →
   submit stale-based edit → assert merged output contains BOTH sides (not a clobber),
   `MERGED_WITH_LOCAL` returned, truncation guards intact.
3. Burst collapse: N rapid `commit_project` calls → one commit (debounce, same job_id).
4. Adopt-in-place: register a dir with existing `.git` + history → `ALREADY_INITIALIZED`,
   history preserved, `.gitignore` augmented (G3), backup variants not staged.
5. Duplicate working_dir registration refused (G4).
6. Power-cut simulation: staged `.part` present but `submit_file` never called → next
   commit stages the last `os.replace`'d content; no torn file ever visible.

**Phase 3 — contract into `CLIENT_INTEGRATION.md`.** New section: "Desktop whole-file
editor contract" (the draft above, finalized), same discipline as §17b.

## Build log
- **2026-07-06/07 (overnight): Phases 1–3 BUILT, pending morning verification.**
  Shipped as azt-collab **0.53.0** (CHANGELOG has the full entry):
  - G1 `submit_file`: `repo.submit_file`/`_submit_file_locked` (repo.py, after
    `_commit_repo_locked`), handler `server._h_project_submit_file` + dispatch route,
    client wrapper `azt_collab_client.submit_file` (+ `__all__`), new code
    `MERGED_WITH_LOCAL` in both mirrors + EN/FR translation. Post-commit side effects
    shared with the debounced worker via new `scheduler.after_committed_local()`.
  - G2: `head_sha` param on `COMMITTED_LOCAL` (`_commit_step_locked`, which also grew a
    `message=` kwarg), top-level `head_sha` on `/sync` responses (attached client-side as
    `result.head_sha`), `Result.param()` accessor on both mirrors, `repo.head_sha_of()`.
  - G3: `repo.ensure_ignore_patterns` (+ `AZT_DESKTOP_IGNORES`), called from
    `_h_register_project`.
  - G4: `projects.WorkingDirAlreadyRegistered` raised by `register()`; handler → HTTP
    409 `working_dir_already_registered` + `existing_langcode`.
  - Phase 2 tests: `tests/test_submit_file.py` (fast path, divergent no-clobber,
    empty-base merge, auto-init, contributor-unset durability, staged validation,
    ignore idempotence, 409 guard, debounce collapse, mirror drift). Run:
    `cd azt-collab && <python-with-dulwich+pytest> -m pytest tests/ -q`.
  - Phase 3 contract: landed as CLIENT_INTEGRATION.md **§ 8b** (whole-file editor).
  - azt-side wiring also started — see [[azt_run_with_server]] build log.

## Notes
- azt's model (survey 2026-07-06, full detail in [[azt_run_with_server]]): whole-file
  atomic write already (`.part`→`os.replace`, lift.py:1246); autosave threaded via
  `maybewrite`/`_write` with `writeeverynwrites=1`; several direct `db.write()` calls
  bypass `maybewrite` → azt-side seam must be `Lift.write()` itself; commit fires after
  every successful autosave (`check_if_write_done` → `repo_commit`, commit-only); push
  only at shutdown (`share()`). Bulk ops already collapse to one write at the end.
- Whole-tree staging + adopt-in-place means azt's settings JSONs get committed — fine
  (they're per-user/per-host named, so cross-device modify-modify is rare; non-LIFT
  both-changed keeps ours + typed Conflict).
- The daemon writes its own dotdirs into adopted trees (`.azt/`, `.azt_atomic_pending/`,
  `.azt-collab/diagnostics/`) — harmless, mostly ignored; document in the contract.

## Watch items (not blockers, decide during/after Phase 1)
- **Contract §5 gap (azt-side, belongs on azt_run_with_server):** the reload offer
  currently RESTARTS azt; §5 promises in-place reload at a safe point + anchor
  restore. Not implemented anywhere yet (F6 only deduped the dialog windows). Route:
  reuse azt's changedatabase flow to re-open the same database (teardown-before-launch
  discipline), fire only at task boundaries, restore task/check/list-position anchor.
  Priority softens once F2+F8 land (dialog then fires only on genuine team content).
- **Pack budget vs desktop audio.** `commit_pack_byte_budget` = 3 MB gates *push*; azt
  projects carry uncompressed `audio/*.wav` that whole-tree commits will include →
  `BLOB_EXCEEDS_BUDGET`/`COMMIT_PACK_EXCEEDS_NETWORK_BUDGET` likely on desktop WAN pushes.
  Options when it bites: per-transport budget (desktop wifi ≫ Android data), wav→m4a, or
  chunked drain. Test 4 should include a wav to observe behavior.
- **Bulk-ASR merge churn** (NOTES_TO_DAEMON.md): per-entry (not per-field) merge means two
  machines drafting annotations inside the same entry collide as modify-modify. Existing
  concern, not made worse by this contract; the `submit_file` merge path inherits it.
- **Commit cadence default** for sort sessions (hundreds of per-decision saves/hour = one
  commit each if >500 ms apart). Mitigation lives azt-side (coalesce; see sibling item);
  if field data shows git bloat, add a daemon max-debounce knob later.

## Open questions — all answered 2026-07-06 (kept for the record)
1. **Concurrency model?** → Effectively single-writer from clients on desktop, but bring
   the Android atomic-write model over; solved robustly by D4/G1 (base-aware writes).
2. **Re-read trigger?** → Poll; impacts documented above.
3. **Bulk-edit → commit mapping?** → Atomic writes give power-outage robustness; commits
   can be coarser than writes; debounce + G1 + azt-side coalescing. (The alternative — azt
   keeps its own git repo and pushes to the server — rejected: two repos to reconcile,
   daemon already owns the merge machinery.)
4. **Compressed/backup variants?** → Keep (emailed), gitignore (G3).
5. **Non-LIFT artifacts?** → Keep existing add-to-git paths; derived outputs ignored (D6).
6. **langcode source of truth?** → Settings (already persisted); Android model (D7).
7. **Contributor migration?** → Mirror git config once at opt-in (D8).
8. **Daemon-absent resilience?** → D9; zero recorder↔azt regressions is the bar.

## Research

### Spurious "Team changes available" with no peer work — DIAGNOSED 2026-07-08

**Symptom (Kent):** desktop azt pops "Team changes available" though no one worked on any
peer; declining it re-nags; accepting forces a pointless full reload/reboot of azt.

**Confirmed root-cause chain (live repro on Demo_en, 2026-07-08):**
1. 16:05 azt save → daemon `submit_file` fast-path consumed the staged file and
   `os.replace`d the LIFT (mtime changed), then the **commit step raised** → azt got
   `SERVER_ERROR` with the staged file gone → azt's consumed-but-errored branch
   (azt `backend/core/collab.py:236-242`) logged "save is safe, history may catch up"
   and returned `'ok'` **without calling `record_lift_stat()`**. azt's recorded LIFT
   stat is now stale *against its own save*. ← THE BUG
2. The save's bytes sat as an unstaged working-tree mod (daemon log: `unstaged_head=
   [..., 'Demo_en.lift']`, `n_changes=5`, all day). When any later commit absorbed it
   (or would have), HEAD advances → azt's 10 s poll (`poll_remote_change`,
   collab.py:253) sees HEAD≠base **and** LIFT stat-diff → `'changed'` → dialog. The
   stat check is `(mtime_ns, size)` only (collab.py:168-183) — it cannot distinguish
   "my own save, uncommitted" from team content.
3. Once `stale=True` latches, **every** poll returns `'changed'` unconditionally
   (collab.py:269-284); `reload_offer_due` re-nags every 5 min until a full reload.
   Quirk: the first dialog stores `_offered_head` while `_last_detected_head` is still
   empty; the next stale-branch poll fills the real head, which reads as "genuinely
   new" and bypasses the snooze → **exactly two dialogs in a row**, then 5-min cadence
   (observed). There is no un-latch path even when the latch was a false positive.

**"I'm here" syncs: exonerated.** Presence alone generates no git traffic —
`fan_out`/`sweep_peer` no-op when the peer is already at our HEAD (`lan_push.py:243`,
`:827`; observed live 17:28: phone arrival → `already at cba4a99abc0c — no-op`).

**Secondary (dormant here, real elsewhere): LAN post-receive hard reset defeats the
benign path.** Any receive that advances HEAD runs `porcelain.reset(mode='hard')`
(`lan_listener.py:1303`; also `integrate_head_into_working_tree`, `:1185`), rewriting
tracked files whether or not content changed → LIFT mtime bumps → artifact-only or
content-identical deliveries classify as `'changed'` instead of `'benign'`. Didn't cause
this week's popups (LAN receives produce log lines; none present) but will produce the
same false dialog whenever LAN delivery is active.

**Fix plan (agreed 2026-07-08). STATUS 2026-07-09 — IMPLEMENTED + VERSIONED
(provisional close 2026-07-09: Kent restarted the daemon, "drop it unless the window
returns"; reopen if it recurs):**
- **azt repo — shipped in 1.8.5** (CHANGELOG done): **F1, F2, F5, F6** + the
  `presenttosort` wait_window crash guard. ⚠ REQUIRES AN AZT RESTART to load (daemon
  restart alone doesn't).
- **azt-collab repo — shipped in 0.53.8** (CHANGELOG done): **F3, F4(a), F4(b), F7,
  post-commit-hook off-thread, Kivy-in-daemon, F8 layer-1** ✓ (rides alongside the
  concurrent session's LAN-ancestry fix already in 0.53.8).
- **DEFERRED / not user-facing:** F4(c) (risky receive-path change; F2 already kills its
  symptom); LAN (i) finite connect-timeout + (iii) stale-endpoint pruning (now
  background hygiene — off-thread fix removed the user-facing hang).
- **Field action (not code):** update the stale tablet (pre-0.46.5) — the empty-merge
  generator behind F8.
- **Elsewhere:** A5 (true in-place reload vs full restart) → belongs on
  [[azt_run_with_server]]; A1–A4 = F1/F2/F3/F5, A6 = F6 (all done).

Daemon RESTART (from a clean tree) needed for the azt-collab fixes; the azt fixes
degrade gracefully without it.

**Session-end 2026-07-09:** Kent restarted the daemon (~17:00) → 0.53.8 daemon fixes
live. A popup recurred at 17:31, but it was a `MERGED_WITH_LOCAL` offer with NO
"latch cleared" line ⇒ the azt APP still running the pre-fix build (azt-side F1/F2/F6
load only on an azt restart, which hadn't happened). So NOT a fix failure — the
daemon half is confirmed live; the azt half awaits an azt restart (tracked on
[[azt_run_with_server]]). Both trees still have uncommitted work (mine + the
concurrent session's LAN-ancestry 0.53.8) — wants a clean commit.
- **Post-commit hook (azt-collab, from probe findings) — DONE.** `submit_file`'s
  `COMMITTED_LOCAL` side effects (`server.py`) were unwrapped and ran on the REQUEST
  thread: `after_committed_local`'s LAN fan-out can block for minutes on a dead-endpoint
  fetch (infinite connect timeout), delaying/losing the reply though the commit landed
  (the "commit lands, response empty → azt SERVER_ERROR" bug). Fix: keep the cheap
  `_set_pending_push` synchronous (wrapped), run `after_committed_local` on a daemon
  thread (mirrors the scheduler's own commit-fire). The (already-durable) reply now
  returns immediately; a fan-out hang/raise is confined to the background thread.
- **F6 (azt, UX) — DONE.** `App.collab_offer_reload` guards on a single open offer
  window (`_collab_offer_win.winfo_exists()`); `notify_error` now returns the notice
  instance. No more stacked "Team changes available" windows.
- **F7 (azt-collab, diagnosability) — DONE.** `_record_started` / `_record_crash`
  (`server.py`) no longer swallow `OSError` — they log the errno + path so the
  months-long silent `state/` write failure can finally be diagnosed. (`_state_dir`
  already `makedirs(exist_ok=True)`, so the cause is NOT a missing dir — likely
  permissions / fd exhaustion / disk; the logged errno will say which.)
- **Kivy-in-daemon (azt-collab) — DONE 2026-07-09** (see the mechanisms section: env
  check in `notify.py`, cached desktop negative).
- **STILL OPEN (background hygiene, not user-facing):** (i) finite LAN connect-timeout +
  per-endpoint backoff and (iii) prune stale `static_endpoints`. The off-thread
  post-commit fix already removes their USER-facing impact (a dead-endpoint fetch no
  longer blocks a save), so a hung fan-out now just wastes one background daemon thread
  for ~6 min. Worth doing for cleanliness; not blocking. F4(c) likewise deferred (its
  symptom is covered by F2's content-identity classification).
- **F1 (azt repo, root cause, one line):** call `record_lift_stat()` in the
  consumed-but-errored branch of `CollabSession.submit` (collab.py:236-242). With this,
  the whole scenario produces no dialog: the later HEAD advance takes the silent benign
  path.
- **F2 (azt repo, self-healing latch):** `poll_remote_change`'s stale branch must
  re-verify before nagging — if the on-disk LIFT matches our last write and HEAD's LIFT
  blob equals base's, un-latch `stale` instead of returning `'changed'`. A falsely
  poisoned session then heals without a reload. Also fixes the double-dialog quirk
  (initialize `_offered_head` semantics or gate the snooze bypass on a *non-empty*
  previous offer).
- **F3 (this repo, seam for F2):** expose the LIFT **blob SHA at HEAD** (and echo the
  one at any requested base) in `project_status`, so azt classifies benign/changed by
  content identity, not file stat. Fits contract §5 polling.
- **F4 (this repo, diagnosability):** (a) `_StdioTee`/log-session must retry opening on
  the next write instead of permanently dropping after one failed rotation
  (`server.py:5124-5126`); (b) `_h_project_submit_file`'s 400-rejection paths
  (`server.py:3506-3530`) return without any log line — violates always-emit;
  (c) post-receive reset: prefer targeted checkout of blob-diff paths over whole-tree
  hard reset (kills the secondary cause and preserves mtimes).

- **F5 (azt repo, one line, diagnosability):** azt's consumed-but-errored warning logs
  `result.codes()` only, but the client wrapper preserves the daemon's exception text as
  `Status('SERVER_ERROR', {'error': str(ex)})` — log `result.param('SERVER_ERROR',
  'error')` too. Both incidents' root exception reached azt and was discarded.

- **F6 (azt repo, UX):** reload offers stack — six+ "Team changes available" windows
  observed open at once (screenshot 2026-07-08 ~18:xx). Guard: one open offer at a time;
  a new head while an offer is open updates it, not a new window.
- **F7 (this repo, old + independent):** `$AZT_HOME/state/` writes have failed silently
  for months — dir mtime 2026-04-29, no `started.json` ever, though `_record_started()`
  demonstrably runs (its neighboring "listening" print lands). `except OSError: pass`
  hides it (`server.py:182/201`). Consequence today: `_record_crash` can't tell us what
  handler crashes are. Find why (permissions? fd exhaustion? something else) and emit a
  summary line on failure.

**Probe findings (2026-07-08 ~18:0x, probe_submit.sh):** fast-path `submit_file` with
base=HEAD: the **commit lands** (refs/heads/main advanced, verified) but the HTTP reply
is empty — the handler crashes AFTER `repo_mod.submit_file` returns, i.e. in the
unwrapped post-commit calls `scheduler._set_pending_push` / `scheduler.after_committed_local`
(`server.py:3550-3551`; the handler's try only wraps the submit itself). Wrap these +
log. Every commit ALSO re-triggers a "Team changes" popup on a poisoned azt session
(probe replaces the LIFT byte-identically → mtime bump).

**Scheduler thread death, timed:** `[scheduler] drain` cadence is 30 s — lines at
17:27:32 and 17:28:02, then never again (17:28:32 missing). The log-session freeze is
the same window (last write 17:28:52). One incident took out the scheduler thread and
the `_LogSession` together; every later commit crashes its caller's post-commit hook.

**Foreground-daemon session (2026-07-08 evening) — mechanisms identified:**
- **The "crash" is a HANG.** LAN layer fetches `git-upload-pack` against stale peer
  endpoints (observed: `10.42.0.100:40425`, hotspot-range; same port was
  `192.168.150.83:40425` on 07-07 — dead static/cached addresses for the tablet) with
  urllib3 `connect timeout=None` × Retry(total=3) → ~2 min kernel connect timeout per
  attempt, 6+ min per dead endpoint. Runs inside post-commit fan-out/sweep ON THE
  REQUEST THREAD → `submit_file` response delayed past any client's patience → azt's
  `SERVER_ERROR` / probe's empty reply, while the commit itself lands. Same blocking
  explains missing scheduler drain ticks. **Fixes:** (i) finite connect timeout +
  per-endpoint failure backoff in LAN fetch/push; (ii) move post-commit side effects
  (`server.py:3550-3551`) off the request thread onto the scheduler; (iii) prune/expire
  stale static_endpoints (peers.json grew 562→984 bytes; check for the 10.42.0.100
  entry).
- **Kivy IS loaded inside the daemon process** (desktop, plain `python -m azt_collabd`):
  Kivy banner + urllib3 warnings rendered in Kivy's `[WARNING] [...]` log format ⇒
  Kivy's logger took over root logging/stdio ⇒ the file-tee starvation in detached
  daemons (everything → /dev/null). Violates the no-Kivy-in-daemon invariant. **Importer FOUND
  (2026-07-09, importtime run + code read):** `azt_collabd/android_cp/notify.py:37-42` —
  `_is_android()` does `from kivy.utils import platform` on every call, desktop included;
  importing `kivy.utils` initializes all of Kivy (logger, config, ~/.kivy log file).
  Called from `notify_project_changed` after EVERY commit (`repo.py` `_commit_step_locked`
  post-commit block) and every post-receive (`lan_listener.py` ~:1316) — matches the
  first-commit/receive freeze timing on both afflicted daemons. Compounding:
  `_get_provider_class` caches only non-None, so desktop re-checks forever. **Committed
  code, not WIP.** Only desktop Kivy vector in the daemon (lan_push / lan_listener
  grep-clean; `azt_collab_client/__init__.py:434` picker path is jnius-guarded).
  **Fix — DONE 2026-07-09:** `_is_android()` now uses a dependency-free env check
  (`ANDROID_ARGUMENT`/`KIVY_BUILD`/`P4A_BOOTSTRAP`), no `kivy.utils` import; and
  `_get_provider_class` caches the desktop negative (`_not_android`) so it stops
  re-checking, while an Android autoclass failure stays uncached to allow retry. Other
  daemon kivy imports verified UI-only (`ui/app.py`, `ui/picker_app.py` = the
  `-m azt_collabd ui` app) or Android-gated function-local (`android_cp/service.py`).
  **Open nuance:**
  in the 07-09 foreground run the tee SURVIVED the Kivy load (post-Kivy `[submit_file]`
  print landed in the day file), so the detached-daemon log death is Kivy-correlated but
  its precise mechanism (env-dependent Kivy logger behavior without a tty?) is unproven —
  moot once Kivy stops entering the daemon. Android is unaffected (`_is_android()` there
  is true and Kivy is already the host).
- **Return-push live repro:** probe commit → fan-out advanced the phone → phone pushed
  back its own merge (`ff78f4ca`) → `post-receive reset` → LIFT mtime bump — the popup
  generator observed end-to-end.

**Frankenbuild caveat (may supersede all daemon-side analysis):** the running daemon
imports `azt_collabd` from this working tree, which is under **active concurrent
modification** by another session (git status: `lan_discovery.py`, `lan_listener.py`,
`azt_collab_client/__init__.py`, `_spawn.py` modified). A daemon spawned mid-edit runs
an inconsistent code mix; both afflicted daemons (07-07 17:57, 07-08 17:27) were spawned
from this tree during the edit window. Before deeper daemon archaeology: wait for the
WIP to land, respawn the daemon from a consistent tree, re-run the probe. If the crash
vanishes, the daemon-side incidents were the WIP; the azt-side fixes (F1/F2/F5/F6) and
the latent design flaws (mtime classifier, unwrapped post-commit hooks, F4, F7) remain
real regardless.

- **F8 (daemon, distinct defect, 2026-07-09 evening):** empty-merge ping-pong on
  LAN-only 'en'. Tablet (841d43a8) minted four content-identical
  "Merge origin/main into main" bot commits in ~30 min (20:52–21:24Z,
  7851617c..f30f794b, no files touched) and delivered them on LAN arrival → desktop
  HEAD advance + hard-reset LIFT rewrite → legitimate-looking reload popups for
  non-changes; wan_unshared 78→82, at_risk 39 and climbing — pure history bloat, no
  data wrong. Same family as the 0.46.x merge-loop chain; desktop has the reattach
  fix, the GENERATOR is tablet-side (version unknown — need its daemon version + log
  via Share daemon log; if < 0.52.30 the known fixes may simply be missing there).
  **Fix layer 1 (source) — DONE 2026-07-09:** the LAN merge path
  (`lan_push._merge_then_push`) already FF/no-op'd on ancestry (heads-equal /
  peer-in-local FF-push / local-in-peer wait); the GAP was divergent history with
  IDENTICAL trees (two empty-merge heads, neither an ancestor) — that fell through to
  `_merge_diverged` (which has no equal-tree short-circuit) and minted another empty
  merge. Added a tree-equality no-op before the merge: content identical + no ancestry ⇒
  don't merge, don't push (can't FF either way); heads stay divergent-but-identical and
  the next real edit converges them. Makes THIS daemon immune to propagating/amplifying
  the loop. **Fix layer 2 (immunity):** F2's blob-identity classify makes the desktop
  ignore empty merges regardless (done). **Generator fix:** still update the stale
  tablet (its pre-0.46.5 daemon is the source that mints them).
  **Fix-version answer (2026-07-09):** this exact loop was broken in **0.46.5**
  (HEAD re-attach after receive; 0.46.4 FF-check is its prerequisite) and hardened in
  **0.50.15** (silent fall-through closed; emits `[data-quality]
  head-detached-no-reattach`). Tablet minting these today ⇒ almost certainly
  pre-0.46.5; action = update tablet to current release (0.52.32-line), confirm via
  its daemon-log header (`mirroring stdio … (daemon X.Y.Z)`) — not the settings-screen
  number (process drift).

## Recommendations for the azt (desktop-app) team — consolidated handoff (2026-07-09)

Everything below is azt-repo work (`/home/kentr/bin/AZT/azt/`), surfaced by the
"Team changes available" popup investigation this week. File:line anchors are from
`azt/backend/core/collab.py` as observed 2026-07-08; verify against current before
editing. Ordered by value. The daemon-side companions (F3 blob-SHA in project_status,
the empty-merge fix in `lan_push.py`, tee/state-dir diagnosability) are done or in
flight on the azt-collab side — these are the azt half.

**A1 (root cause, one line) — `record_lift_stat()` on the consumed-but-errored save
branch.** In `CollabSession.submit` (collab.py ~236-242), the branch that handles
"daemon replaced the file then the commit step failed" (`SERVER_ERROR`, staged file
already consumed) returns `'ok'` **without** re-recording the LIFT stat. Result: azt's
`lift_stat` goes stale against azt's *own* save, so when any later commit advances HEAD
the 10 s poll sees "HEAD moved + LIFT changed on disk" and pops a reload dialog for
non-changes. Add `self.record_lift_stat()` in that branch (matching the sibling
branches at :207/:217/:220 which already do it). This alone eliminates the whole
observed popup class. **Confirmed live 2026-07-08** as the trigger.

**A2 (self-healing latch) — un-latch `stale` when it was a false positive.** Once
`self.stale` flips True, every `poll_remote_change` returns `'changed'` unconditionally
(collab.py ~269-284) and `reload_offer_due` re-nags every 5 min until a reload; there is
NO recovery path even when the latch was spurious. In the stale branch, before
returning `'changed'`, re-verify: if the on-disk LIFT matches what we last wrote AND (via
A3's daemon data) HEAD's LIFT blob equals our base's blob, clear `stale` and return
`'none'`. Also fixes the observed **double-dialog** quirk (first dialog stores
`_offered_head` while `_last_detected_head` is still empty; next poll fills the real head
→ reads as "genuinely new" → bypasses the 5-min snooze → two dialogs back-to-back). Gate
the snooze-bypass on a *non-empty* previous offer.

**A3 (content-identity classifier) — classify by LIFT blob SHA, not file mtime.** The
current `_lift_changed_on_disk` (collab.py ~178-183) compares `(mtime_ns, size)`, which
a byte-identical rewrite trips — and the daemon's LAN post-receive `porcelain.reset
(mode='hard')` rewrites tracked files regardless of content, and empty-merge commits
from un-updated peers advance HEAD with an identical tree. The daemon side is adding the
LIFT blob SHA at HEAD to `project_status` (companion change); when it lands, azt should
compare that blob SHA against its base's blob SHA instead of file stat. Immune to
rewrites, empty merges, and artifact-only commits by construction — makes the dialog
fire ONLY on genuine LIFT content change. Supersedes A2's mtime heuristic once available.

**A4 (diagnosability, one line) — log the daemon's error text on the safe-save path.**
The consumed-but-errored warning (collab.py ~236-242) logs `result.codes()` only, but
the client wrapper preserves the daemon's exception string as `Status('SERVER_ERROR',
{'error': str(ex)})`. Also log `result.param('SERVER_ERROR', 'error')`. Both of this
week's incidents' root exceptions reached azt and were discarded — this would have saved
a day of blind hunting.

**A5 (UX, contract §5) — in-place reload instead of full app restart.** The reload
offer's "Load now (restart)" genuinely restarts A-Z+T; the persistence contract §5
promises an *in-place reload at the next safe point with anchor restore*, which is not
implemented anywhere (the recent F6 only stopped the dialog windows from *stacking*).
Route: reuse azt's existing **changedatabase** flow to re-open the same database
(teardown-before-launch discipline the sort work already established), fire only at a
task boundary (never mid-verify), and restore the anchor (same task/check/profile/list
position). Urgency drops sharply once A1+A3 land and the empty-merge daemon fix ships,
because the dialog then appears only for genuine team content — so this becomes polish
for real events, not a defense against spam. Belongs on the azt_run_with_server item as
the D-tail companion.

**A6 (UX) — coalesce stacked reload offers.** If not already done on the azt side:
guard on a single open offer window (`_collab_offer_win.winfo_exists()`), and have a new
head while an offer is open *update* that window rather than open another. (Field
2026-07-08 showed 6+ stacked "Team changes available" windows before this was addressed.)

**Open mysteries (watch, don't block on):**
- The `submit_file` SERVER_ERROR is **persistent, not a one-off**: recurred 17:47 (second
  save) through the freshly restarted daemon → state- or code-based, not a wedged
  process. Meanwhile HEAD advanced cba4a99→71962a95 (~17:3x–17:42, invisible: log dead)
  — so the debounced/other commit path (or a LAN receive; tablet was present) works
  while submit_file's does not. `_commit_step_locked`'s commit is try-wrapped (typed
  `COMMIT_FAILED`, no raise), so the escape is in the unwrapped calls: `_stage_all`,
  `_detect_uncommittable`, `porcelain.status`, `_default_author`/`_app_committer`, or
  `head_sha_of`. Capture the text via the probe script (scratchpad `probe_submit.sh`:
  byte-identical staged copy + base=current HEAD → fast path; response body carries
  `{"error": str(ex)}`) or via F5 once azt is editable.
- Tee starvation trigger — **now reproducible, 2-for-2, and blocking diagnosis**: the
  17:57 (07-07) daemon's tee received zero writes from 17:57:06 until teardown, and the
  fresh 17:27 (07-08) daemon's log froze at 17:28:52 — **~80 s after startup** — while
  the daemon kept serving azt's polls (server.json rewritten with same pid + new port ⇒
  restart preserves the pid, likely re-exec). `uninstall_stdio_tee` has no callers.
  Both deaths came seconds after a **LAN discovery arrival + sweep** (07-07: arrivals
  17:57:04, dead by :06 with `[lan-listener] stopped`; 07-08: phone arrival 17:28:40 +
  sweep, dead by ~:52). NOTE: the running daemon was spawned from a working tree with
  **uncommitted modifications to `lan_discovery.py` / `lan_listener.py`** (concurrent
  WIP by another session) — prime suspect for the tee death being new behavior in the
  same subsystem the deaths correlate with.
- The popup ALSO recurred after the daemon restart (reported ~17:30+); its trigger is
  invisible because the tee was already dead. Discriminator needed from the azt-side
  log: another "Collaboration server reported a problem" (SERVER_ERROR persists per
  save on this project) vs "Teammates' changes were merged" (MERGED_WITH_LOCAL) vs
  "Team changes detected" (poll-path latch).

Survey pointers (2026-07-06, agent survey; file:line in azt-collab/):
- Dispatch ladder `server.py:4418-4743`; `_h_register_project:2680` (abspaths, arbitrary
  working_dir OK); `_h_project_status:2760` returns `head_sha` (:2815-2853) read off refs.
- `LiftHandle.atomic_open_write` filesystem branch `lift_io.py:150-211` (sibling tmp +
  `os.replace`); URI branch buffers via `/atomic_commit` (`server.py:3368`, base64 in RAM
  — wasteful for multi-MB desktop files, hence G1's staged_path handoff instead).
- Debounce: `scheduler.py:418-499` (same job_id per burst); staging: `repo.py:1576-1627`
  `_stage_all` = whole-tree `add -A` minus `.azt_atomic_pending/`.
- Merge: `repo._merge_diverged:553` (WAN `repo.py:~4810`, LAN `lan_push.py:854`);
  `lift_merge.three_way_merge:1078` per-entry-guid; guards `:560/:625`; LAN hard-reset +
  its two uncommitted-edit guards `lan_listener.py:1047-1146`.
- No CAS anywhere (grep clean); commit Results carry no sha (`status.py:475-512`) → G2.
- `init_repo` on existing `.git` → `ALREADY_INITIALIZED` (`repo.py:2332-2356`); daemon
  `.gitignore` (only if absent) lacks azt patterns → G3; `register()` lacks dup guard → G4.
- USB sibling item: decided bare-repo-on-drive; Phase 1 = local-remote credential bypass
  (`repo.py:4534-4603`) + `usb_backup` RPC + gesture-gated (never a plain extra_remote).
