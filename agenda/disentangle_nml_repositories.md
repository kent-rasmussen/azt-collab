# Disentangle the two nml repositories (2026-07-22 cross-merge)

- **Scope & relationships:** karlap's `nml` registration
  (`~/Assignment/Tools/WeSay/nml-x-test` — Kent's test data,
  registry-labeled bare `nml`) got shared to the workshop phone
  "itservices-hue — Audio Words Collect 3" (Kent clicked "share
  nml" believing it was the real project — the UI showed no
  binding). The phone had its own `nml`; the two lineages are
  github-fork relatives (common ancestor exists), so the LAN merge
  machinery joined them cleanly and repeatedly (merges at 09:07→
  90923021, 09:23 → pushed, 09:56 → 7d3eb14b/08fbdcf3 era), and
  the crew then recorded ON TOP of the joined history. Prevention
  is the sibling item → project_identity_beyond_langcode.md.
- **Vision / done-criteria:** each side's project contains exactly
  its own lineage: the phone's nml = its pre-merge history + all
  crew work since (with any of Kent's post-fork test entries
  removed); karlap's WeSay nml = its pre-merge state (crew entries
  removed or absent). No further cross-traffic (unshared) until
  identity work ships.
- **Deadline:** ASAP (crew is actively recording on the joined
  history — every hour adds commits on top)
- **Waiting on:** phone-side deletion + canonical-home decision

### STATUS 2026-07-22: karlap side DONE, clean
- Unshared both sides (confirmed on phone).
- Daemon stopped for the surgery (no live-watcher race).
- Graph + daemon log both confirmed the topology: two strands
  joined only at merges c49e3343 / 08fbdcf3; phone tip 27973030,
  karlap pre-merge tip 90923021 (parent 33f757c6).
- `git branch phone-lineage-premerge 27973030` (phone lineage
  preserved) + `git reset --hard 90923021`.
- Verified: HEAD=90923021, no merge commits, pure karlap lineage
  (33f757c6, 06fc123a "A-Z+T edit by Kent" below). Kept the 09:07
  auto-commit (Kent's own working-tree edits, not crew data).
- The itservices lineage is NOT lost — parked on branch
  ``phone-lineage-premerge`` in the WeSay repo.

### Remaining
- **Phone side:** delete the `nml` project on itservices-hue
  (its clean pre-merge lineage is safe on the parked branch; no
  in-place backwards reset / force-push needed). Kent's read:
  nothing good was added to the phone since the merge, and it
  wasn't talking to the other phone, so deletion loses nothing.
- **Canonical home decision (no urgency):** where the itservices
  lineage permanently lives — re-clone to the phone from a
  canonical repo, keep as the archival branch, or promote to its
  own properly-named project once the Tier-2 naming rule ships
  (project_identity_beyond_langcode.md).
- Bring the daemon back up (auto-spawns on next client call; it
  re-reads the now-clean ref from disk).

## Plans

### Step 0 — containment (do first, no analysis needed)
- Unshare `nml` from peer 95223b00 on karlap (symmetric unshare
  mirrors the removal) — stops further merges. Verify no new
  `[lan-merge]`/`[lan-push] ... for 'nml'` lines after.
- Do NOT publish/push karlap's nml anywhere meanwhile.

### Step 1 — evidence (before touching any ref)
- `git -C ~/Assignment/Tools/WeSay/nml-x-test log --oneline -15`
  (authorship shows the interleave; identify pre-merge tip —
  expected 33f757c68d04, with 90923021b19f as the auto-commit of
  pending working-tree edits made 09:07).
- Phone side pre-merge tip: 279730302178 (from the 09:07/09:23
  logs). Crew commits since are those between 279730302178's
  lineage joins and current head.
- Confirm with the crew what the phone project actually is (real
  Ndemli data vs another test copy) — determines how much care
  the phone-side surgery deserves.

### Step 2 — surgery sketch (refine after Step 1)
Everything pre-merge exists in both histories; nothing is lost.
Two independent halves:
- **karlap/WeSay side:** reset main to the pre-merge tip
  (33f757c68d04 or 90923021b19f — decide whether the 09:07
  auto-committed working-tree edits are wanted) + working tree
  reset. Crew commits vanish from this copy (they live on the
  phone).
- **phone side (the delicate half):** crew work sits ON TOP of
  joined history. Options, pick after Step 1:
  (a) LIFT-level extraction: current LIFT minus the entry-guids
  that exist in karlap's pre-merge LIFT but NOT in the fork base
  nor the phone's pre-merge LIFT (= Kent's post-fork test entries);
  commit that as a cleanup commit on the phone's current history.
  History stays joined but CONTENT is clean — cheapest, and
  audio files (named per entry) follow the same guid filter.
  (b) Replay: new branch from 279730302178, replay crew's
  post-merge commits' LIFT deltas — clean history, more work,
  needs the phone's repo accessible (cable makes that feasible:
  fetch it to karlap, do surgery locally, push back FF... push
  back is a non-FF rewrite → requires explicit force decision on
  the phone. Riskier; only if (a)'s joined-history residue is
  unacceptable.)
- Recommendation pending evidence: (a).

## Notes
Origin: 2026-07-22 morning, USB-cable drill. Kent: "two high
priority agenda items: 1. disentangle the two repositories, 2. keep
that from ever happening again."

## Research
