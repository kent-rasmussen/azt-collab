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
- **Waiting on:** Nothing. The daemon-side timing
  (`agenda/daemon_submit_timing.md`, handed over 2026-07-30) shipped the
  same day in 0.55.170 and reported — see PHASE 0 VERDICT below. What
  remains is one measurement pass on a real repo, tracked as
  `azt/agenda/timing_on_real_repos.md` (2026-07-31).

## PHASE 0 VERDICT (2026-07-30, both halves instrumented)

`Demo_en`, 5.6 MB, azt + daemon lines paired:

```
save cost:    5.6 MB | indent 0.09s + serialize 0.28s + submit 0.39s = 0.76s | ok
[submit cost] Demo_en | land 0.01s + stage[1 files] 0.16s + commit 0.21s = 0.38s
```

**Both premises this item was built on are dead:**

- NOT tree-proportional in the way assumed. `_stage_all` stages **0 or 1** paths per
  save, not ~3050. Its ~0.2 s is `porcelain.status()` walking the tree to DISCOVER
  that, not per-file staging. Logging the file count is what killed the assumption.
- NOT payload-proportional. `land` is 0.00 s (an `os.replace` of an already-staged
  file), and `commit` is 0.14–0.21 s for a 5.6 MB blob.
- The two logs agree to ~0.1 s, so no queueing and negligible transport.

**What the daemon's 0.4 s actually is: a floor of two whole-tree scans** — the status
scan inside `_stage_all`, and the index work inside the commit.

Consequences:

- **Phase 1 (stage only the submitted path)** is the only phase with anything to win
  — roughly the 0.2 s status scan.
- **Phase 3 (surgical per-entry submit)** is NOT justified. It is a contract change
  that would shrink a payload that isn't the cost. Do not build it on these numbers.
- Total ~0.76 s per save on a fast box. **The item's own premise — slow machines
  can't keep up — now needs re-establishing before either phase is built**, on the
  real repo (`nml`: 16 MB LIFT, 1868 audio files, both scans larger) and on a slow
  CPU. That is what `timing_on_real_repos.md` is for; it may close this item as
  measured-and-fine.

## RE-WEIGHTED 2026-07-30 (Kent) — Phase 2's premise was WRONG

**The misreading:** Phase 2 called dirty-flag skip "likely the single biggest win"
on the strength of "azt autosaves unchanged content routinely". Kent: *"sure they
can [both be true]. autosaving is through maybewrite. 'routinely' here does not
mean 'more often than changes are made', but rather 'triggered by each change'."*
So `maybewrite` IS the autosave, fired per change, and the content genuinely
differs on every save. There is little or nothing to skip — **dirty-skip is not
the win.** Phase 2's serializer-cost half stands; its skip half does not.

**The actual problem, in Kent's words:** *"the LIFT file is easily 16MB, and the
whole thing is written when only a single line of text has changed."* Four
O(file) passes per one-line change:

1. `xmlfns.indent(self.nodes)` — full-tree reindent, mutating every element
   (`azt/io_put/lift.py:1225`);
2. `ElementTree.write` to the `.part` sibling — serialize + write 16 MB;
3. `submit_file` hands the daemon the whole 16 MB;
4. daemon: `_stage_all` (~3050-file walk), LIFT blob rehash, commit.

**So Phase 3 (surgical entry submit) is the main event, not Phase 2.** Phase 1
(stage only the submitted path) stays a cheap complement worth doing regardless.

### Precision: stop at per-entry (decided 2026-07-30)
Kent asked what finer precision would cost — often only
`entry/sense/citation/form`, or a form's annotation, has changed. Answer: several
times the work of per-entry, for almost nothing.

- After per-entry the payload is no longer the bottleneck. The daemon must still
  splice and write a coherent 16 MB file and git must hash a 16 MB blob — a floor
  identical for a 2 KB entry or a 100 B form. Entry→form optimises ~0.01% of the
  original cost, sitting behind fixed costs that don't move.
- Per-field work is in ADDRESSING and MERGE, not the write: a path like
  `sense[2]/citation/form[@lang='baf']` must survive a peer reordering senses
  between base and HEAD (positional addressing is what the 2026-07-10 duplicate-form
  fix had to remove), and field-level edits against a moved base need conflict
  rules per field type. azt-side tracking grows from dirty guids to
  (guid, path, lang, name).
- It doesn't win the concurrency argument either: the daemon's merge takes the
  entry as its unit but descends into forms/langs within it, so entry-level
  submission already survives a peer editing a different field of the same entry.
- If a specific field ever needs it, `lift_surgery.py` is the precedent for adding
  one case at a time (as `set_audio`/`set_illustration` were) rather than
  generalising the write path.

Kent: *"I'll take your recommendation, and we can look at more precision later, if
we decide it might help at that point."*

### Interaction with the git/delta work (asked 2026-07-30): NONE
Per-entry submission changes only the azt→daemon wire. What gets COMMITTED is
identical — same working file, same blob, same history — and git's delta
compression operates on committed objects, not on the wire format. So none of the
recent push/delta/chunk-ordering work is affected either way.

The distinction to keep: **wire granularity** buys azt latency/CPU;
**storage granularity** is what would touch deltas. Today every save adds a fresh
~16 MB blob; they delta well against each other (consecutive versions differ by one
entry), but COMPUTING those deltas over 16 MB blobs is exactly what packing and
pushing spend CPU on, and dulwich is not C git. Per-entry submission does not
reduce that at all; the parked entry-per-file option would (one small blob per
change, other files' blobs untouched). If object churn per save later proves to be
the constraint, the lever is storage layout — not finer RPCs.

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
- **Dirty-flag skip — DEMOTED, see the re-weighting above.** The premise
  ("azt autosaves unchanged content routinely") was a misreading:
  `maybewrite` IS the autosave, fired per change, so the content
  genuinely differs on nearly every save and there is little to skip.
  Keep only as a cheap guard against the few genuinely-clean saves;
  do NOT budget it as the win.
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

### First Phase-0 numbers, 2026-07-30 (Kent's box, NO daemon, 5.6 MB file)

```
17:39:49  5.6 MB | indent 0.05s + serialize 1.62s + submit 0.00s            = 1.67s   | (no daemon)
18:12:16  5.6 MB | indent 0.09s + serialize 0.28s + submit 0.00s + replace 0.00s = 0.37s (thread) | (no daemon)
18:36:35  5.6 MB | indent 0.08s + serialize 0.24s + submit 0.00s + replace 0.00s = 0.32s (thread) | (no daemon)
```

(The 17:39 line predates the `replace`/`(thread)` fields, so an update landed
between it and 18:12.)

**Settled: `indent` is not the cost.** The full-tree reindent — one of the four
O(file) passes the re-weighting names — is 0.05–0.09 s, i.e. 5–25% of azt-side time
and ~2% of the earlier worst case. Optimising `xmlfns.indent` is not worth doing.

**azt-side cost is serialize, and it is ~0.045 s/MB** on this machine (0.24–0.28 s
for 5.6 MB). Extrapolating linearly to the 16 MB field file: ~0.7 s serialize +
~0.25 s indent ≈ **1 s of azt-side work per save on a FAST box** — so on a field CPU
several times slower this is plausibly 3–5 s per save on its own, before the daemon
does anything. That is consistent with "slow machines can't keep up" having an
azt-side component, which Phase 3 (smaller payload) would cut and Phase 1 would not.

**Unexplained: the 1.62 s serialize at 17:39, ~6× the other two for the same file.**
First-save-of-session cold cost, a different build, or something real about that
save. Do not average it away — if it recurs it matters more than the median.

**These numbers CANNOT decide Phase 2 vs Phase 3**, and the reason is NOT that the
daemon was down. Kent 2026-07-30: the daemon was up. `(no daemon)` was a bad label
of mine — `lift.py:1287` set it as the initial value and it survived for two
unrelated reasons: `collab_submit` not attached, OR `filename != self.filename`,
i.e. **a backup/template write, which by design never goes through the daemon**
(`writebackup()` → `self.write(self.backupfilename)`). A daemon that IS asked and
fails logs `fallback`, so neither case ever meant "the server broke".

**Consequence: these three lines may not describe the hot path at all.** If they are
`writebackup` saves, their serialize cost is real but their `submit 0.00s` is by
design, and no conclusion about save cadence follows from them. The label now
distinguishes `(collab not attached)` from
`(backup/other file — not the hot path)`, so the next capture is self-describing.

Still needed, in priority order:

1. A capture whose outcome is `ok` — i.e. the real collab save path — on any
   machine. That alone gives `submit` wall-clock and settles decision (1) below.
2. Daemon-side timing inside `_submit_file_locked` to split that number
   (→ handed to the daemon team, `agenda/daemon_submit_timing.md`).
3. The slow field machine, 16 MB file, on 1.13.3+ (the reload-prompt fixes are the
   stated prerequisite — without them the stale-base loop makes every save a full
   merge and swamps the measurement).

### What each measurement decides
- **(1) Is daemon cost significant?** If `submit` ≲ serialize (~0.25 s at 5.6 MB),
  the item collapses to azt-side serialize + Phase 1 as a freebie; no contract change.
- **(2) Payload-proportional or tree-proportional?** Tree (`_stage_all`'s ~3050-file
  walk, commit overhead) → Phase 1 suffices, Phase 3 buys almost nothing. Payload
  (parse/splice/write 16 MB) → Phase 3 is the main event.
- **(3) Irreducible floor?** Writing a coherent 16 MB file and hashing a 16 MB blob
  cost the same for a 2 KB payload. If that floor dominates, NEITHER phase helps and
  the lever is storage granularity (parked entry-per-file) — the same reasoning the
  per-field precision decision used, one level up.
