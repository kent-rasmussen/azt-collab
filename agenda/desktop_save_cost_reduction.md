# Desktop save cost: write/submit less than the whole LIFT per save

- **Scope & relationships:** azt-collab (daemon + client contract) +
  azt (editor-side). The § 8b whole-file model
  (azt-collab/agenda/azt_persistence_server_sync.md) deliberately
  made whole-file saves SAFE; this item makes them CHEAP ENOUGH for
  slow CPUs — field observation 2026-07-25: slow machines can't keep
  up with the save cadence. Complementary to
  azt-collab/agenda/daemon_lock_across_network_io.md (which owns
  merge-side per-entry work + the lock phase-split) and downstream
  of azt-collab/agenda/spurious_team_update_prompts.md (whose
  stale-base loop makes every save a full merge — deploy those fixes
  BEFORE measuring here, or the numbers are dominated by the bug).
  **Constraint (Kent, 2026-07-25): no commit coalescing** — small
  per-save commits are the unit of incremental push progress
  (azt_persistence_server_sync.md watch-items). "Save less often" is
  off the table; only "pay less per save" remains.
- **Vision / done-criteria:** on the slow field machine, a save
  round-trip (decision → committed) keeps pace with per-decision
  save cadence in a sort session: no BUSY backlog, no UI stall, no
  growing queue of unsaved work. Whole-file interchange semantics
  preserved (the working-tree .lift stays one valid document).
- **Deadline:** none
- **Waiting on:** Nothing (Phase 0 needs the spurious-prompts fixes
  deployed on the slow machine first)

## Plans

### Phase 0 — measure before building (gate for everything below)
Deploy 0.54.73 + the azt-side legs on the slow machine, then profile
where a save actually spends its time there:
- azt-side: whole-DOM serialize to `.part` (O(file size), per save —
  even for no-op autosaves).
- daemon-side fast path: `_stage_all` (whole-tree `add -A`, walks
  ~3050 files per save), LIFT blob rehash, `porcelain.commit`.
- daemon-side divergent path: full parse + merge (should now be rare;
  if the log still shows `MERGED_WITH_LOCAL` per save, fix that
  first — it swamps everything else).
Add timing lines (daemon: around `_stage_all` / commit in
`_submit_file_locked`; azt: around serialize) if the logs can't
already answer. Decide Phase 2 vs Phase 3 from the numbers.

### Phase 1 — daemon cheap win, no contract change: stage only the
submitted file
`submit_file`'s commit step currently rides `_stage_all` = whole-tree
staging. The endpoint knows exactly which rel_path changed; stage
only that path on the submit_file path. Non-LIFT artifacts keep
riding the next `commit_project` (already the § 8b obligation-4
contract — "don't call it per keystroke"). Cuts a 3050-file walk per
save to O(1). Care: keep the auto-init/recovery paths (which DO want
whole-tree absorption) on `_stage_all`.

### Phase 2 — azt-side cheap wins, no contract change
- **Dirty-flag skip:** azt autosaves unchanged content routinely
  (NOTHING_TO_COMMIT is documented as routine) — each no-op save
  still pays full serialize + submit + daemon hash. Track a dirty
  bit (or dirty-entry count) and skip serialize+submit entirely when
  clean. Likely the single biggest win on slow machines.
- **Serializer cost:** measure azt's DOM→bytes serialize; if it
  dominates, optimize in place (azt repo) before any contract change.

### Phase 3 — contract change, only if Phase 0/1/2 aren't enough:
surgical entry submit
Per-entry base-aware RPC, precedented by the existing
`lift_surgery.py` path (set_audio/set_illustration already do
targeted single-field writes daemon-side without the peer holding
the DOM):
- `submit_entries(langcode, [{guid, entry_xml}], base_sha)` — daemon
  patches the named entries into the working-tree LIFT by guid
  (text-surgical like lift_surgery, not full-DOM), commits.
- azt tracks dirty entry guids per save (it knows which entry a
  decision touched) and submits only those; whole-file `submit_file`
  remains the fallback for bulk ops / structural changes / doubt.
- Base-awareness per entry: if HEAD moved since base, the daemon
  merges per entry (the existing per-entry-guid merge machinery is
  the same code path, scoped to the submitted guids).
- Contract: new § alongside 8b; whole-file editors may mix both
  (surgical for hot-path saves, whole-file at task boundaries).
Adds daemon endpoint + client wrapper + status codes per the
"adding a new client API call" checklist.

### Explicitly out / parked
- Commit coalescing (decided against, 2026-07-25 — see constraint).
- Entry-per-file on-disk storage (git-native granularity): endgame
  option if even Phase 3 can't keep up; breaks "working tree holds
  one valid .lift" for other tools; would need assemble-at-boundary
  design. Not v1.

## Notes
- Origin: Kent 2026-07-25, "on slow CPU, it seems like they can't
  keep up", following the spurious-prompts diagnosis (the stale-base
  loop made every save a 20–40 s full merge on the affected class of
  machine — the two costs compound via the BUSY→fallback feedback
  loop; see daemon_lock_across_network_io.md).
- azt_persistence_server_sync.md never considered write granularity
  for desktop — it standardized whole-file-but-safe (G1/§ 8b);
  surgical writes existed only for Android peers (§ 9a). This item
  brings the surgical option to the desktop hot path if measurements
  justify it.

## Research
