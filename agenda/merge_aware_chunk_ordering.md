# Merge-aware chunk ordering (a merge commit is not a small chunk)

- **Scope & relationships:** `azt-collab` — `repo._pick_intermediate_sha`
  and `_push_chunked_to_ref`. Distinct from
  [[daemon_lock_across_network_io]] (which is about how long the lock is
  HELD) though they share a victim: the same 8.9 GB unit both blocks the
  lock for hours and cannot be transferred. Mitigated but not solved by
  0.55.79 (pre-seed before a measured-hopeless push).
- **Vision / done-criteria:** no single push unit exceeds the byte budget
  when a smaller one is structurally possible. A merge commit's
  second-parent side is chunked and banked as its own sequence BEFORE the
  merge is pushed, so the merge itself carries only its trees and commit
  object. `remaining` decreases monotonically across a large first push;
  an interrupted link costs one chunk, never the whole transfer.
- **Deadline:** none
- **Waiting on:** Nothing

## Plans

The defect: `_pick_intermediate_sha` picks boundaries along the
**first-parent spine** (and its fallback walks `include=[tip],
exclude=[base]` then filters by `_is_ancestor`). Either way, commits on a
merge's **second-parent** side never become chunk boundaries — they are
only ever transferred *attached to the merge*. So chunk-halving converges
to "one merge commit" and stops helping, because one merge can carry
thousands of objects.

Field 2026-07-28, `nml`: `chunk_n=50` → est 8.9 GB → shrink to
`chunk_n=1` at merge `eed545d6` → still 2,330 objects / 8.9 GB. No
smaller unit exists in the current scheme.

Two mechanisms reach the same end state (the server already holds the
objects, so the merge push is small). They are NOT alternatives in the
design sense — one is a byte-shovel, the other is the actual fix:

1. **Pre-seed the blobs** (`azt-blob-seed-*`). Ships raw blobs on
   synthetic refs; no history advances; leaves ref litter (501 refs
   observed on `nml`, pruned only when reachable from `origin/main`).
   Already exists; 0.55.79 makes it fire *before* the doomed push
   instead of after a failure that may never come. Good enough to
   unblock, not a design fix.
2. **Order the walk topologically** so both parents are banked before
   the merge. Real commits, real history, progress banked on the topic
   ref, no synthetic refs and no cleanup debt. This is the fix.

**Design (2), worked out 2026-07-28 — "topological order" alone is NOT
sufficient, and the reason is a correctness constraint:**

Every push to a ref must be a **fast-forward on that ref**. Therefore
**a single ref cannot express an arbitrary ancestor-closed subset** of
the DAG. A topological order of a merge region gives `A, B, M` where A
and B are parallel; pushing A then B to the same topic ref is not a FF.
Relying on receive-pack accepting a non-FF (it does — `set_if_equals` is
a CAS, not an FF check, see [[feedback_dulwich_set_if_equals_not_ff]])
would leave A's objects unreferenced on the server and eligible for
github's gc. So progress would silently un-bank.

**THIRD REVISION, same session — side refs are the PRIMARY mechanism.
Read this before the two designs below; they are both wrong for the
observed case.**

I proposed chunking the topic ref along P2's line (`base → … → P2 → M`,
FF at every step). That fails here, and the reason is decisive: the
existing fallback in `_pick_intermediate_sha` **already** considers
second-parent commits — it takes the oldest delta commit *that has base
as an ancestor*. It chose the merge itself, which means **no other delta
commit qualifies**, which means **P2's line diverged before base**. Those
commits are not descendants of `origin/main`, so none of them can be
pushed to the topic ref as a fast-forward.

So the merge is genuinely the first FF-valid boundary from base, and
8.9 GB is the true size of that step. No path selection fixes it.

What breaks the deadlock: **a fresh ref has no FF constraint on its first
push.** `azt-side-<sha8>` can be planted anywhere on P2's line and then
chunked forward along it, banking each step. Once that side is on the
server, the merge's own delta is trees + commit.

This is also not an exotic or bootstrap-only shape (Kent, correcting my
"initial push" framing): a peer records offline for hours, merges in, and
the accumulated work goes up as one merge. It recurs every time that
happens. The code already knew — `_pick_intermediate_sha`'s docstring
cites *"device aztobt2-ui, base=old origin/main off the merge spine,
estimate 9.3 GB — CHANGELOG 0.52.31"*: same phenomenon, different device,
mitigated then, never solved.

Note the existing `azt-blob-seed-*` machinery is the crude form of this
idea — synthetic commits carrying blobs, precisely to get bytes onto the
server without an FF-valid commit path. Side refs are the same trick with
real commits, which is why they need no separate cleanup contract.

Which locates the real defect precisely: `_pick_intermediate_sha` walks
the **first-parent spine**. For `nml`, `eed545d6`'s first parent is on
the main line github already holds, while the second parent carries the
2,330 new objects — so along the first-parent spine there are NO
boundaries between base and the merge, and the merge becomes the first
candidate with all 8.9 GB attached. Chunk along P2's line instead and the
same destination is reached in hundreds of small FF steps, each banked.

So the work is:

1. **Pick the path that carries the delta**, not the first-parent spine.
   Choose a path base → target through the commits that actually
   introduce missing objects, and chunk along it. Fixes `nml` outright.
2. **Side refs only when BOTH parents' lines carry large deltas** — the
   recursion below, demoted from primary mechanism to fallback.

The recursive form, retained for case 2:

```
push_chain(ref, base, target, budget):
    spine = first-parent path base → target
    for each commit C along spine, oldest first:
        if C is a merge and delta(second-parent side) is non-trivial:
            # bank the side branch on ITS OWN ref first
            push_chain('azt-side-<sha8(P2)>', merge_base(base, P2), P2, budget)
        advance ref toward C in budget-sized steps
```

Properties this gives us, all of which the current scheme lacks:

- **Every step is ≤ budget**, because a merge is only pushed once both
  its sides are already on the server, so its own delta is trees +
  commit.
- **Every step is banked.** receive-pack is atomic per push, so the unit
  of loss is one chunk, not the whole transfer. This is what makes it
  survive a field link unattended.
- **FF is preserved on every ref** — the main topic ref advances along
  the first-parent spine only; each side branch advances along its own.
- **No synthetic commits.** Side refs point at real commits, so the
  existing orphan sweep retires them once they are reachable from
  `main` — same contract as the blob seeds, without the blob-seed
  litter.
- **One walk, not N² ancestry tests.** Computing the spine once and
  indexing into it removes the `_is_ancestor`-per-candidate cost that
  holds `project_lock` for minutes (see
  [[daemon_lock_across_network_io]]).

Implementation notes / hazards:

- Recursion depth: bound it. Nested merges are normal in this history
  (`Merge main (converged)` appears repeatedly); a depth cap with a
  fall-through to the blob-seed path keeps a pathological shape from
  hanging.
- `merge_base(base, P2)` may be far back; if the side branch's delta is
  itself oversize, the recursion handles it, but the base choice
  determines how much is re-sent. Prefer the newest commit already known
  on the server that is an ancestor of P2 — i.e. consult the remote
  tracking refs, the same set 0.55.80 taught the estimator about.
- Idempotence: a re-entered push must not redo banked work. Side refs
  are named by their own sha, so their presence in
  `refs/remotes/origin/azt-side-*` is the resume marker.
- The estimate must be per-step, and must count remote-tracking refs as
  haves (0.55.80) or every step will look oversize.

Related, cheaper, and possibly decisive on its own:

3. **The estimate ignores what the server already holds.**
   `_estimate_delta_size(repo, chunk_base, intermediate)` measures
   against the local chunk base. `_enumerate_new_blobs` correctly
   excludes blobs already covered by `refs/remotes/origin/azt-blob-seed-*`.
   With 3,425 commits and 501 seed refs already on the remote, a large
   share of those 2,330 objects is plausibly up there — meaning the real
   pack may be a fraction of 8.9 GB and the oversize bail is firing on a
   measurement that ignores prior uploads. **Check this first**: it is a
   few lines, and if true it dissolves the problem without touching the
   walk.

## Notes

Why this is worth doing rather than living with: receive-pack is
**atomic per push**. A unit that doesn't fit doesn't partially land —
github shows nothing, banks nothing, and an interrupted link discards
everything transferred. So an oversize unit isn't slow, it's
*impossible* on any link that can't hold up for the whole duration,
which in the field is most of them.

Order of work: (3) measure, then (2) if still needed. (1) is already
shipped as mitigation.

## Open question for Kent (stored 2026-07-28, not acted on)

**LAN serving competes with the WAN push for the same link.** The watchdog
dump during the nml push showed two `process_request_thread` stacks in
`write_pack_from_container` → `ssl.send` — serving upload-pack to peers —
running concurrently with the WAN transfer, all sharing a link measured at
~63 KB/s.

The daemon was racing itself: peers pulling from us slowed the push that
gets data off-site. Whether that's wrong is a policy call:

- leave it (peers converge sooner, github takes longer);
- yield LAN serving while a WAN push is in flight;
- yield only for the escalated run-to-completion push.

Not implemented. Relevant because the payload-scaled timeout (0.55.84)
assumes a floor rate, and self-contention is one of the things that pushes
the real rate below it.

## Research
