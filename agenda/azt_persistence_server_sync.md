# AZT persistence ↔ server: sync the read/write contract + expand daemon capabilities

- **Scope & relationships:** azt-collab/daemon (+ cross-cutting design into azt). The
  **server-side half** of the AZT integration: evaluate how azt's read/write model must
  synchronize with the daemon, and **build the daemon functions AZT needs that don't exist
  yet.** Pairs with [[azt_run_with_server]] (that item is the azt-app-side wiring — client
  calls; this item is the daemon-side capability + the persistence-sync design that spans
  both). Sibling: [[usb_backup_transport]] (transport parity). Do the evaluate/plan here
  *before or alongside* azt_run_with_server — azt can't run against the server until the
  server has what AZT needs.
- **Vision / done-criteria:** a documented persistence-sync contract for AZT (how its
  reads, whole-file writes, bulk rewrites, and re-reads map onto daemon RPCs), plus any new
  daemon endpoints that contract requires, shipped and covered by tests. AZT's edits
  persist and converge through the daemon without data loss under concurrent merges.
- **Deadline:** before [[azt_run_with_server]] ships (prerequisite — the server functions
  must exist before azt is wired to them).

## Plans

Deliverables of THIS item (server half): (a) a written persistence-sync **contract** for
AZT, (b) whatever **new daemon endpoints** that contract requires, (c) **tests**, (d) the
contract folded into `CLIENT_INTEGRATION.md`. The azt-app-side wiring that consumes all this
is the separate [[azt_run_with_server]] item.

**Phase 0 — Evaluate + decide (design, blocks everything).**
- Diff azt's persistence semantics (azt survey, 2026-07-06) against the existing
  client/daemon surface (`CLIENT_INTEGRATION.md`); confirm/prune the candidate-gap list
  below into a definite list of missing daemon capabilities.
- **Decide the concurrency model** — the crux. Two options:
  - *Single-writer MVP:* desktop azt is the sole editor of its projects; re-read only at
    open; daemon still commits/syncs. Cheap, but unsafe if any other peer edits concurrently.
  - *Full reconcile:* azt re-reads + reloads in-memory state when the daemon reports HEAD
    advanced (merge landed). Correct under concurrency; expensive (mid-session reload with
    task windows open — lives mostly in [[azt_run_with_server]], but the daemon must expose
    the signal).
  This decision sizes both items. Output a one-page contract + the endpoint list.

**Phase 1 — Server capability additions (daemon).** For each confirmed gap, follow the
"When adding a new client API call" checklist in `azt-collab/CLAUDE.md` (server.py dispatch
→ client wrapper → status codes in both mirrors → translation → `MIN_CLIENT_VERSION` bump if
the wire format changes). Bias hard toward **reusing** existing RPCs; only add what the
contract proves missing. Candidate additions (confirm in Phase 0, see gap list):
re-read/change signal for desktop; a bulk-edit → single-commit affordance; open-by-path for
the desktop case.

**Phase 2 — Consistency tests (daemon-side).** Build on `azt-collab/tests/`: assert
write-through-daemon == on-disk LIFT; a bulk of edits collapses to the expected commit(s);
a concurrent merge, then re-read, yields the merged content (not a clobber). Needs LIFT
fixtures + a temp `$AZT_HOME` + spawned/loopback daemon.

**Phase 3 — Contract into `CLIENT_INTEGRATION.md`.** Document the AZT persistence contract
(reads, whole-file writes, bulk cadence, re-read-on-HEAD-advance obligation, langcode,
contributor) so azt and any future desktop peer follow one spec — same discipline as the
existing §17 background-refresh contract.

## Notes
- AZT's model (survey): load whole LIFT once at startup, mutate in-memory ElementTree,
  autosave the **whole file** on nearly every edit, **never re-read**; bulk operations
  (sort/verify) rewrite large swaths; also writes gzip/lzma variants + backup files; has
  non-LIFT artifacts (xlp, exports, alphabet PDFs, images). This differs sharply from the
  recorder (surgical per-field writes), so the daemon likely needs additions.

## Research — candidate server-capability gaps to evaluate
Starting list (confirm/prune during the evaluate pass):
1. **Re-read after HEAD advance (the crux).** azt never re-reads; the daemon merges peers'
   changes and moves HEAD. Need a clean "content changed → reload these bytes" path AZT can
   act on. May be *no new endpoint* (poll `project_status.last_commit`, re-open
   `LiftHandle`), or may want a merge-output/reload signal. Decide.
2. **Whole-file atomic write for a filesystem path** — `LiftHandle.atomic_open_write`
   already exists; confirm it fully covers azt's `Lift.write()` (atomic `.part` → replace)
   with no gap.
3. **Bulk-write / commit-debounce behavior** under azt's write-on-every-mutation cadence —
   validate against the same merge-churn concern already filed for bulk ASR
   (`NOTES_TO_DAEMON.md`); may need a "batch of edits → one commit" affordance.
4. **Compressed/backup variants** (`writegzip`/`writelzma`/`writebackup`) — do these stay
   local (out of the daemon's working tree) or get a home? Host decision.
5. **Non-LIFT artifacts** (xlp/export/PDF/images) — derived outputs: keep local vs manage.
   Likely local; confirm so the daemon isn't asked to version them.
6. **langcode/project** — `register_project`/`open_project`/`derive_langcode` exist; confirm
   they cover azt inferring langcode from LIFT content and not persisting it today.
7. **Contributor** — `get/set_contributor` exists; confirm it replaces azt's git-config
   author cleanly (daemon refuses commits with `CONTRIBUTOR_UNSET`).
- Anything genuinely new lands via the "When adding a new client API call" checklist in
  `azt-collab/CLAUDE.md` (endpoint + client wrapper + status codes + translation) and, if
  it's a wire-format add, a `MIN_CLIENT_VERSION` bump.

## Open questions
1. **Concurrency model — single-writer MVP vs full reconcile?** (biggest; gates scope of
   both this item and azt_run_with_server). Sub-question: in the field, is a desktop azt
   project ever edited by another peer/device concurrently, or is desktop effectively
   single-writer? The honest answer scopes the crux. Answer: realistically, a desktop server is not going to be taking writes from multiple clients, since other clients run on Android. But if we can do the atomic write we've been using in Android on the desktop safely and robustly, it is probably worth bringing over to the desktop.
2. **Re-read trigger on desktop.** Android has `notifyStatusChanged`; desktop loopback has
   no push channel. Does azt poll `project_status.last_commit` and reload on change, or do
   we add a notify/long-poll? (Leaning: poll — no new endpoint — but confirm it's enough.) Answer: fill in this item with information detailing the impacts of this decision
3. **Bulk-edit → commit mapping.** azt autosaves on nearly every mutation
   (`writeeverynwrites=1`); sort/verify rewrite large swaths. Does that flood
   `commit_project`? Does the existing 500 ms debounce absorb it, or do we need an explicit
   "begin/end bulk → one commit" window? Interacts with the bulk-ASR merge-churn concern
   already in `NOTES_TO_DAEMON.md`. Answer: I think the two sensible paths, given that we must stay rubust across power outages, is to move to atomic writes, or else keep a git repo for azt to wrote to, and push to the server. This is inefficient on Android, but maybe less importantly so on a desktop. If this is not clearly OK, do some research and make a proposal.
4. **Compressed/backup variants.** azt writes gzip/lzma + `writebackup` copies for
   crash-safety. Keep local (outside the working tree) or drop? Does the daemon's atomic
   write + git history make them redundant, or did field users rely on them? Answer: these are sent by Email at times, so keep them, but maybe .gitignore them.
5. **Non-LIFT artifacts** (xlp / export / PDF / images). Local-only vs daemon-managed?
   CAWL images are already daemon-owned; what about azt's other image/derived outputs?
   Likely local — confirm so the daemon isn't asked to version derived files. Answer: some of these artifacts already have paths to adding to git; keep those paths (so users choose when an output is fully drafted). We are already using a git repo, so use that to model the next.
6. **langcode source of truth.** azt infers langcode from LIFT content per-startup and
   doesn't persist it; the daemon wants an authoritative langcode at `register_project`.
   Who wins, and does azt's inferred value always parse as a BCP-47 tag the daemon accepts
   (incl. dialect/region/private-use)? Answer: Practically, this is in settings already, because this cannot reliably be inferred from LIFT --especially as we look forward to multilingual dictionaries. So the same model used on Android is fine.
7. **Contributor migration.** azt uses the git-config author; daemon uses
   `get/set_contributor` (+ `device_name`). Migrate silently from git config, or prompt
   once? What's `device_name` on desktop? Answer: As above, this is already set for local .git use; mirror that.
8. **Daemon-absent resilience.** On desktop the client auto-spawns the daemon, but if it
   can't start, does azt hard-fail or degrade to local-file writes? (A field tool losing
   the ability to save because a daemon won't boot is a serious regression — decide the
   fallback posture.) Answer: I think we want the daemon to work robustly. That said, this is one of my larger concerns in this conversion; we need to be able to get data from recorder to azt and back without any regressions whatsoever.

## Research
