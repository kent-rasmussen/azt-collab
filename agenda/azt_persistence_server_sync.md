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
