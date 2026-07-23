# LIFT merge robustness: no duplicated forms, form-aware and correct — before Cameroon

- **Scope & relationships:** azt-collab/sync — the daemon's LIFT merge
  (`azt_collabd`). Evidence: 'wife' entry (9ae43c82) accumulated 29 duplicate
  `<form lang="en-x-py">` nodes in one `CVC lc verification` field — one
  `['V1=ai','C2=f']` + 28× `['V1=ai','C1=wh']` — ALL ON ONE COMPUTER (Kent
  2026-07-10; no second device involved). azt-side guard already shipped the
  same day: `Field.consolidate_forms_by_lang` (io_put/lift.py) unions duplicate
  same-lang verification forms on read/write, drops conflicting checks. That
  guard makes the corruption survivable; THIS item removes the source.
- **Vision / done-criteria:**
  1. **Explain the multiplication.** Why does merge-after-merge produce MORE
     copies of the same form? (First hypothesis to test: each merge re-resolves
     the same divergence against a base that lacks the previous resolution, so
     "keep both sides" appends again every time. Confirm in the wife entry's
     data-repo history: `git log -p` — duplicates should enter at merge
     commits.) Also explain how ONE computer produced the divergent pair
     (save-vs-origin race? merge of stale local state?).
  2. **XML-aware post-merge invariant:** a merged LIFT must never contain
     duplicate same-lang `<form>` nodes within one field/multitext where the
     consumer expects single-form-per-lang. Enforce after every merge —
     repair (union) + log, or refuse and surface.
  3. **Form-aware union for single-form fields:** where both sides changed the
     same single-form field, merge the CONTENT (python-list union for
     verification code lists; same-check conflicts annotated or dropped), not
     the nodes. Mirror azt's consolidate_forms_by_lang semantics so both layers
     agree.
  4. **Coverage — all fields where this applies** (from io_put/lift.py getlang
     single-form ftypes and the "no-lang fieldvalue is safe ONLY while
     single-form" docstrings):
     - `<profile> <ftype> verification` (segmental codes; the wife case)
     - `<ftype> primitive verification` (#C/C#/syls codes)
     - `whole-word <ftype> verification`
     - `cvprofile_<ftype>` (both `…-x-cvprofile` and `…-x-cvprofile_MT` forms)
     - `tone` (human + `_MT` forms; entry and example level)
     - `location` (example level)
     - `SILCAWL`
     Also decide semantics for glosses/definitions/citation (true multitext —
     duplicates within one lang still illegal).
  5. **Merge idempotence test:** merging the same states twice yields the same
     result as once; add to the daemon test suite (which is green 20/20 as of
     07-07 — extend it).
- **Deadline:** 2026-07-15 (Kent leaves for Cameroon; HARD stop 07-17)
- **Waiting on:** Nothing — DONE 2026-07-10; follow-ups from the 07-11 A3
  drills CLOSED 2026-07-11 with the matched-version drill on 0.54.4:
  both directions `conflicts=0, repairs=302` (desktop merge `000ecf9c`,
  phone merge mirrored), delivery `1/1`, no DivergedBranches, no growth.
  The classification fix (0.54.4) is what turned the perpetual
  `conflicts=301` into honestly-reported repairs. Reopen on any new
  duplicate-form sighting or a diamond that fails to settle. Residuals
  filed elsewhere: merge-repair log chattiness (~300 lines per merge on a
  polluted file) → log-cleanup candidate; honest LAN-unshared fallback →
  own agenda item.

## Field evidence 2026-07-11 ~17:42 (A3 no-clobber drill, wifi-off divergence) — for reopen judgment

Both sides refused force-overwrite and merged ✓ (no data loss observed). But:
1. **Concurrent-merge diamond:** phone merged local=18618b/peer=5218fd →
   4d287c4c (conflicts=0) and pushed it; desktop merged local=5218fd/
   peer=18618b → 6e45a17e (conflicts=301); desktop's post-merge push failed
   `DivergedBranches(4d287c4c, 6e45a17e)`. Verify the NEXT round converges
   (merge-of-merges) and can't livelock with both sides re-merging forever.
2. **Asymmetric outcomes on the same pair:** conflicts=0 one direction, 301
   the other. The desktop side's merge-repair annotated **302 divergent
   same-lang gloss copies as conflict** across ~290 entries (mostly swh, some
   es/fr) — reads as the LEGACY union-merge duplicate-gloss corruption
   (wife-entry disease, database-wide in Demo_en glosses) surfacing through
   the shipped repair. Questions: why one direction only; where do these
   conflict annotations surface for user review in azt; is a one-time scrub
   of legacy duplicates (outside merge) wanted before the workshop.
3. Transient `MissingCommitError` in the lan-unshared walk during the window
   (peer head not yet fetched) — handled (→0) but noisy.

## Second round 17:57–17:59 — analysis (settles the reopen judgment)

What the round proves, good news first:

- **No multiplication.** Copies stay at 2–3 per entry across repeated merges,
  even mixed-version. The ×29 growth engine is dead. No data loss either
  round.
- **Asymmetry cause CONFIRMED: the phone runs pre-0.54.0 merge code.** Its
  17:58:36 merge emitted zero `[merge-repair]` lines on the same polluted
  entries (impossible on 0.54.x) → conflicts=0. Not a new-merge bug.

The mechanism of the repeating 301 (the "annotation ping-pong"):

- Desktop 0.54.x annotates the ~290 legacy gloss-dupe entries →
  `conflicts=301`. That state reaches the phone; the phone's OLD merge's
  canon-equal path (0.45.34 self-heal) STRIPS the annotations as
  false-positives → desktop's next merge re-annotates the same 301 → repeat
  every A3 round. Diamond persisted this round
  (`DivergedBranches(0dc22f87, 3a336fca)`); desktop HEAD stayed 3a336fca —
  the phone's push didn't clobber (wire CAS refused), though the phone
  LOGGED "pushed merged … 1/1 delivered" — a false-success report worth a
  look (phone-side, may vanish with the rebuild).
- **Matched 0.54.x versions will converge in representation** (both sides
  deterministically produce the annotated tree: canon-equal comparison
  strips, in-merge normalize re-annotates → same bytes) — BUT the per-merge
  conflict scan will still COUNT those re-annotations, so every merge of the
  polluted database reports `conflicts=301` forever until the dupes are
  actually resolved. Two candidate fixes for the follow-up:
  1. **Classification:** invariant-sweep annotations on entries that were
     canon-EQUAL between the two sides should count as `repairs`, not
     `conflicts` (they represent pre-existing pollution, not divergence
     between these two devices). Stops the perpetual "301 conflicts" noise.
  2. **The scrub:** a one-time daemon-side (or azt-side) pass that resolves
     the legacy gloss duplicates for real (they're mostly 2–3 divergent swh
     glosses per entry — needs a policy: keep-first? human review?). This is
     the actual cure; #1 is the honest reporting while they exist.

## Field re-confirmation + scrub policy decided — 2026-07-22 (CABTAL workshop)

Reproduced live at scale during the nml recovery: an itservices→nml
three-way merge reported `conflicts=395` on `gloss[lang=swh]`
("2 divergent same-lang copies; annotated 2 as conflict") with
`repairs=329` pre-existing, and the merged `nml.lift` carried
**1284 azt-lift-conflict markers** — most riding in with the crew's
own file from routine multi-device merging, before Kent's accident.
Same disease as 07-11, now database-wide across the workshop's
shared nml and compounding per sync.

**ROOT CAUSE — decisive input (Kent 2026-07-22): multiple same-@lang
`<gloss>` nodes are VALID LIFT and ship that way in the bare CAWL
template.** Example (untouched template entry): two `<gloss lang="es">`
(`abdomen`, `barriga`) and the swh equivalent (`tumbo / matumbo`) —
distinct senses/synonyms of one headword, each its own gloss node,
mirrored by the `<definition>`'s semicolon-separated senses. This is
GOOD DATA, not a merge artifact.

Therefore the "conflicts" are our merge MISINTERPRETING legitimate
multi-sense glosses. The invariant-#13 family assumes one-node-per-
lang for glosses ("duplicates within one lang still illegal" — the
line in Vision item 4 above; that assumption is WRONG for glosses/
definitions/citation, which are true multitext with legitimately
repeatable same-lang nodes). So the merge either collapses two valid
synonym glosses to one (DATA LOSS) or annotates them as an
azt-lift-conflict pair (false conflict) — producing the 395/1284
counts on data no human diverged.

**The 07-22 "scrub" idea is RETRACTED — it would have destroyed
data** (collapsing `abdomen`+`barriga` → one gloss deletes a real
synonym). There is nothing to scrub; the DB is correct.

**The fix is in the merge, not the data.** Gloss / definition /
citation (multitext) same-lang multiplicity is valid: match gloss
nodes by (lang, normalized-text) IDENTITY, three-way like any other
child — a node present in base and unchanged is kept; genuine
add/remove handled normally; NEVER collapse two different-text
same-lang glosses and NEVER annotate same-lang multiplicity as a
conflict merely for being multiple. Keep the single-node-per-lang
collapse ONLY for the fields that truly are single-form-per-lang
(the verification/`_MT`/tone/etc. list in Vision item 4 — the wife
case, where multiplicity IS corruption). The bug is applying the
single-form rule to true-multitext fields.

CONFIRMED in code 2026-07-22: `lift_merge.py:756-757` groups
`<gloss>` children of `<sense>` by `(tag, lang)` — treating glosses
as single-per-lang like `<form>`. A ≥2 group that isn't byte-
identical falls to step 3 (`:815-833`) and gets `azt-lift-conflict`
annotations. The section comment `:658` states the wrong premise
outright ("one `<gloss>` per lang inside one sense"). This fires on
every legit multi-sense entry (abdomen/barriga).

NON-DESTRUCTIVE — verified: step-1 collapse (`:764-782`) removes only
byte-IDENTICAL duplicates (`abdomen`≠`barriga` → never dropped);
union (step 2) is verification-only; glosses reach only step 3,
which KEEPS both nodes and just annotates. So NO gloss/synonym data
has been lost, in the itservices merge or the crew's file — the
1284 markers are false annotations over intact data.

Fix (core-merge; design + test before shipping):
  * Exempt true-multitext nodes from the single-per-lang
    collapse/annotate: `<gloss>` (under `<sense>`), and `<form>`
    under `<definition>`/`<citation>`. For these, dedup
    byte-identical only; NEVER annotate distinct same-lang nodes.
  * Keep the single-form rule for verification/`_MT`/tone/etc.
    (the wife-case corruption is real there).
  * Tests: merge-idempotence on a multi-sense-gloss fixture
    (abdomen/barriga must survive unchanged, zero conflicts);
    the existing verification-union tests must stay green.
  * Follow-on cleanup (now SAFE — strips false ANNOTATIONS, not
    data): a pass removing `azt-lift-conflict` annotations from
    same-lang gloss multiplicities. Opposite of the retracted
    scrub. Can run on the shared repo to clear the accumulated
    1284 markers once the merge fix stops regenerating them.

### Audio-form merge policy — SHIPPED 0.54.24 (1A); 1B done ad-hoc; NOW-refine 0.54.26

STATUS: 1A (merge resolves last-wins) shipped 0.54.24 —
`_audio_recency_resolver` in repo._merge_diverged + audio branch in
lift_merge._normalize_entry, resolve-everywhere, cheap-no-op, tests.
1B (historical one-shot) done via resolve_audio_conflicts.py, Kent
verified.

DONE 0.54.26 (was the "OPEN FOLLOW-UP"): the local pre-commit merge
sites no longer annotate-and-defer. `_audio_recency_resolver` grew a
`work_dir` param — a referenced audio file not in committed history
but present on disk resolves to `float('inf')` = NOW ("undefined is
NOW", Kent 2026-07-22), so a just-recorded take wins with no spurious
annotation. Wired at `_submit_file_locked`,
`integrate_head_into_working_tree` (post-receive, repo.py ~3652), and
`reapply_snapshot_after_merge` (LAN reapply). `_merge_diverged` keeps
`work_dir=None` → pure most-recent-commit-time, deterministic across
devices; NOW is inherently local and re-derives deterministically at
convergence. Still lazy+cached (on-disk scan only on a real audio
conflict).

OPEN: the Android `dateModified` stamping bug (below) is independent
and still open.

### Audio-form merge policy — DESIGN (Kent 2026-07-22)

Discovered during the itservices→nml re-run: after the gloss fix,
142 conflicts remained, all `form[lang=nml-Zxxx-x-audio]` — two
divergent audio recordings per entry (Kent's side vs itservices).

Domain facts (Kent):
  * Audio is SINGLE-VALUE per entry: replace-per-take, past takes
    unrecoverable. So divergent audio forms ARE real conflicts (not
    multi-value like glosses — do NOT exempt them the way glosses
    were exempted).
  * Filename is reused intentionally (avoid FS churn); the extension
    tracks the record-time format setting. So same-base +
    different-extension = DIFFERENT recordings, NOT format churn.
    (My "same base = same take" collapse idea was WRONG — dropped.)
  * Resolution rule: **last-wins.** "Overwrite is a cost of
    collaboration." The hard case (two teams record the same word
    then merge) is accepted as last-wins too.

Cheap implementation (no per-form git blame):
  * Compare the two branch TIPS' `commit_time` ONCE
    (`repo[local_sha]` vs `repo[remote_sha]` in `_merge_diverged`);
    newer side wins for all audio conflicts in that merge. One
    comparison, not per-form. Coarse but matches the rule.

The ASR-preservation refinement (protects real work — Kent's
concern): audio forms carry substantial computed ASR/transcription
annotations (`neurlang/ipa-whisper-base`, `facebook/mms-1b-all
(xxx!)` ×many, `md5`, …). Last-wins drops the losing side's ASR
with its form. But the `md5` annotation is the audio CONTENT hash:
  * **Same md5** on both conflicting forms → audio didn't actually
    change (filename/format only) → keep winner's form but UNION the
    ASR annotations (by model-name key) so no ASR work is lost.
  * **Different md5** → genuinely different take → last-wins whole
    form incl. its ASR; re-ASR on the winning recording is the
    inherent, accepted cost.

Non-destructive stopgap (current behavior, 0.54.20): audio
divergence is ANNOTATED (both forms + all ASR preserved as a
conflict pair) — nothing lost, just unresolved and messy for a
crew with no conflict UI. The resolver above replaces the annotate
path for audio forms.

Tests to add with the build: (a) different-md5 audio → last-wins by
tip recency, loser dropped; (b) same-md5/different-filename audio →
one form kept, ASR annotations unioned; (c) idempotence.

PERF constraint (Kent 2026-07-22 — merges run many times a day, must
not repeat expensive work every few minutes): the merge sweep runs on
all ~1700 entries every merge, so per-entry work MUST be a cheap
no-op on the clean path. `_reconcile_entry_marker` got an early-out in
0.54.23 for exactly this. When 1A (last-wins by most-recent COMMIT —
Kent's chosen recency, 2026-07-22) is built, the git commit-date
lookup (a subprocess/log call, genuinely expensive) must run ONLY for
entries that actually hold an audio conflict — never per-entry. A
merge with zero audio conflicts must do zero git-date calls. Cache
per-file dates within a merge. 1B (resolve_audio_conflicts.py,
one-shot) pays the git-log cost once, off the merge path — that's the
right place for the historical cleanup, not the hot merge loop.
Load/read time is the WRONG place to move cleanup to: reads happen
far more often than merges; merge-time-with-cheap-no-ops is correct.

RECENCY BASIS decided (Kent 2026-07-22): **per-file most-recent
COMMIT, NOT branch-tip.** Branch-tip is wrong for the normal field
pattern, not just an edge: a phone dark for a month, one recording
today, then merged — its tip is "today," so branch-tip would let all
its month-old files beat another phone's yesterday work. Per-file is
required. Affordable because it runs ONLY for actual audio-conflict
forms (a handful per merge, → 0 after cleanup), never per-entry — so
it does not violate the cheap-no-op rule. Lookup via dulwich (walk
the lineage for the file's last-touching commit; Android has no git
CLI). Caveat: commit times are device-clock-based, so a skewed clock
can mis-order — inherent to any time-based last-wins, accepted.

NO PLATFORM SPLIT — RESOLVE EVERYWHERE (Kent 2026-07-22, after
challenging the split idea). An earlier draft had "desktop resolves,
Android annotates-and-defers." REJECTED: it breaks deterministic
merge output. The merge must produce the same tree on any device
from the same inputs (the convergence property; the idempotence
test guards it). If Android annotates the same conflict desktop
resolves, the two produce DIFFERENT trees → they diverge → more
merges → possibly never converge on those entries. The split saves
almost nothing (per-conflict dulwich walks, a handful per merge,
sub-second on a phone) and costs convergence. So: resolve on ALL
platforms. Per-file-commit is deterministic across devices (git
history is identical, so "the file's last commit" is the same
answer everywhere), so resolving everywhere keeps convergence AND is
correct. One code path, no platform gate.

### FOLLOW-UP: Android not stamping dateModified (2026-07-22)
Surfaced while evaluating `<entry dateModified>` as a recency signal:
the Android daemon/recorder apparently does NOT update `dateModified`
on writes (audio, etc.). That's why dateModified was unusable for
last-wins, and it's a latent bug on its own — anything that trusts
LIFT timestamps (sort-by-recent, "what changed", future merge
heuristics) is blind on Android-authored edits. Fix: stamp
`dateModified` (and `dateCreated` on new entries) on every
Android-side LIFT write. Separate from the audio-merge work; tracked
here so it isn't lost.

### FUTURE (revisit when gloss editing ships) — Kent 2026-07-22

The 0.54.20 gloss carve-out ("distinct same-lang glosses are never
a conflict") is TRUE ONLY for the current read-only-gloss workflow.
Once users can edit glosses, two different same-lang glosses become
ambiguous:
  - NEW synonym added on one side → keep both (not a conflict), or
  - one gloss EDITED to a different value on each side → REAL
    conflict that must surface.
Structurally indistinguishable without per-gloss IDENTITY. The fix
then: pair glosses three-way by a stable identity (base-match by
content/position, or a gloss `id`/`order` attribute) via the same
`_pair_same_key` machinery keyed children already use — a gloss
present-and-edited on both sides conflicts; a gloss added on one
side is a new sense. Do NOT ship gloss-editing without this, or the
merge will silently drop divergent gloss edits. Cross-ref:
project_identity_beyond_langcode.md (same "identity, not
label/position" theme).

Next steps, in order: ~~implement #1 (repair-vs-conflict classification)~~
DONE 0.54.4 (canon-equal sides skip the conflict scan; sweep annotations
count as `repairs`; test `test_shared_pollution_is_repair_not_conflict`) →
rebuild phone server APK (kills the ping-pong, the diamond asymmetry, and
the phone's false "pushed/delivered" success report) → matched-version A3
re-drill expecting `conflicts=0, repairs≈301`, diamond settling in one
merge-of-merges round. Scrub (#2) MOOT for Demo_en — Kent 2026-07-11:
"testing scrap"; revisit only if a real project shows legacy gloss dupes.

## Plans

1. ~~Read `azt_collabd`'s merge implementation~~ DONE 2026-07-10: custom
   XML-aware 3-way (`lift_merge.three_way_merge`), guid-keyed entries,
   recursive narrowest-multi conflict expression. Bases: WAN/LAN
   `_merge_diverged` uses a real merge-base; but `atomic_recovery` merges
   orphans with `base=b''` and `reapply_snapshot_after_merge` uses pre-merge
   HEAD — the weak-base paths that run on ONE computer.
2. ~~Reproduce~~ DONE: encoded as unit repro
   (`test_wife_multiplication_stays_bounded_and_converges`); pre-fix pairing
   demonstrably adds one copy per pass.
3. ~~Implement invariant + form-aware union + tests~~ DONE, shipped 0.54.0.

## Notes

- 2026-07-10: "n conflict(s) annotated" wording in collab.py suggests the merge
  already has some conflict-annotation machinery — find it; the fix may belong
  there. → Found: `azt-lift-conflict` annotations in `lift_merge.py`; fix
  landed there as predicted.
- Kent: "let's make lift merging robust (and correct) before I leave for
  Cameroon."
- **Status 2026-07-10 (0.54.0, uncommitted): implementation + tests done;
  awaiting Kent's pytest run + field verification.**

## Research

### The ×29 multiplication, explained (done-criterion 1)

Two cooperating defects in `azt_collabd/lift_merge.py`, both fixed in 0.54.0:

1. **Positional same-key pairing** (`_walk_children`). Children sharing a key
   (`form[lang=en-x-py]`) were paired by list index; length-overhang was kept
   unconditionally. `ours=[A,B]` vs `theirs=[B]`: index 0 pairs A-with-B →
   phantom modify-modify → annotated pair `[A,B]`; index 1 keeps overhang B →
   result `[A,B,B]`. Each merge of the still-divergent pair appends exactly
   one more copy → **linear growth; 28 copies ≈ 28 merge passes**, matching
   1× `['V1=ai','C2=f']` + 28× `['V1=ai','C1=wh']`.
2. **One-sided-child resurrection** (`_merge_pair`). A child missing on one
   side was kept from the other WITHOUT consulting base — so any repair that
   deleted duplicates was undone by the next merge against a stale branch,
   and deleted glosses/forms could resurrect generally.

**How one computer produced the divergent pair:** no second device needed —
two weak-base merge paths run locally: (a) `atomic_recovery` merges
atomic-write orphans with `base=b''`, so every same-lang difference reads as
both-changed → conflict pair (the seed A/B pair: two different checks
verified at different moments, one landing via an orphan); (b)
`reapply_snapshot_after_merge` merges azt's working-tree writes against merge
results mid-sync. Repeated commit/drain cycles then ran engine (1) once per
merge.

Optional git confirmation on the wife project's data repo (duplicates should
enter at merge/recovery commits, one per commit):
`for c in $(git -C ~/.local/share/azt/projects/LANG rev-list --all -- LANG.lift | head -40); do echo "$c $(git -C ~/.local/share/azt/projects/LANG show $c:LANG.lift | grep -o "C1=wh" | wc -l)"; done`

### What shipped (0.54.0, done-criteria 2–5)

- **Content-first pairing** (`_pair_same_key`): identical-on-both-sides
  children pair with each other (clean, no conflict); one-sided leftovers
  matching base are honored as deletes; only true divergences pair
  positionally.
- **Base-honored child deletes** in `_merge_pair` (present side unchanged
  since base + missing on other side → deleted, not resurrected).
- **Verification union** (`_union_verification_texts`): duplicate same-lang
  forms in any `<field type="…verification…">` union their code lists —
  byte-identical semantics to azt's `Field.consolidate_forms_by_lang`
  (first-seen order, same-check value conflicts DROPPED → re-verify). A
  both-changed verification conflict auto-resolves to the union; no conflict
  pair, no Conflict emitted (deterministic, convergent).
- **Post-merge invariant** (`_normalize_entry`, every entry, every merge
  output, all call sites — WAN `_merge_diverged`, LAN, snapshot-reapply,
  atomic-recovery): identical same-lang duplicates collapse to document-first;
  verification dupes union; any other same-lang multiplicity is forced into
  the annotated `azt-lift-conflict` pair shape (visible, never silent).
  Repairs logged as `[merge-repair] …`; counted in new `MergeResult.repairs`.
- **Coverage decision (criterion 4):** union applies exactly where azt
  consolidates — verification fields (all: profile/primitive/whole-word/
  alphabet, they all contain 'verification' in @type). cvprofile/tone/
  location/SILCAWL and true multitexts (glosses/definitions/citation) get the
  invariant (dedupe identical; divergent → annotated pair — their values are
  scalars, so content union isn't defined; conflicts stay visible for humans).
  `<gloss>` handled at sense level alongside `<form>`.
- **Idempotence (criterion 5):** re-merge reaches a byte-stable fixed point;
  locked in by `test_wife_multiplication_stays_bounded_and_converges` and
  `test_conflict_pair_remerge_converges_no_growth`. 9 new tests total in
  `tests/test_lift_merge.py`.

Run: `cd ~/bin/AZT/azt-collab && pytest tests/ -q`
