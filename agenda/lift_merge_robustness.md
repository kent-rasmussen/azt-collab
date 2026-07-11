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
- **Waiting on:** Nothing — DONE 2026-07-10 (Kent: "call those done… until
  a bug shows up"). pytest green (27/27 merge tests); field-verified live in
  the 13:24 karlap↔phone merge. Reopen on any new duplicate-form sighting.

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
