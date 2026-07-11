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
