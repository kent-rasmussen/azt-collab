# Delta-aware chunk sizing: stop paying the round trip per commit

- **Scope & relationships:** `azt-collab/daemon`, `repo.py`. Direct follow-up to
  thin-pack push (0.55.149 / .165 / .167, verified live 2026-07-30). Sits beside
  `merge_aware_chunk_ordering.md` (that one is about *which* commits go in a
  chunk; this is about *how many*).
- **Vision / done-criteria:** `chunk_n` rises above 1 on its own, because the
  pre-flight estimate reflects what a thin pack will actually send. Done when a
  backlog of N commits drains in roughly N/chunk pushes instead of N — observed
  in the field — with no increase in `unpack index-pack failed`.
- **Deadline:** 2026-07-31 (morning)
- **Waiting on:** Nothing

## Plans

### The problem, precisely

Thin-pack push works: ~50 pushes on 2026-07-30, one rejection, `15.4–15.7 MB not
transferred` on most. But per-unit time stayed at **~10 s, identical whether the
line reports 15.7 MB saved or 0.0 MB** — payload has stopped being the cost, and
what remains is negotiation + GitHub's receive-pack processing.

That 10 s is paid **per commit**, because `chunk_n` is pinned at 1:

```
topic-push pre-shrink chunk_n 50→1 (est 1,240,081,629 > budget 3,145,728)
```

`_estimate_delta_size` prices each blob at `raw_length()`. A 16 MB LIFT with a
six-line diff is priced at 16 MB and ships as a few KB. So the estimate that
gates chunk sizing is describing a pack we no longer send.

Fixing it is worth roughly a 10× on any backlog: 40 commits becomes ~4 pushes.

### Approaches considered

1. **Estimate by heuristic** — if a blob has an available base, price it at some
   fraction of its size. Cheap, and wrong in the dangerous direction: an
   underestimate lets an oversized chunk through, and if the thin push then
   *fails*, the `porcelain.push` fallback ships the full pack — hundreds of MB on
   a 2 Mbps link.
2. **Compute real deltas in the estimator** — exact, but does the delta work
   twice (once to size, once to send), and `create_delta` over a 16 MB pair per
   blob per candidate chunk size is not free.
3. **Compute once, use twice** — factor out a planner that finds bases and
   builds the deltas, returns `(count, bytes, prepared_objects)`, and have both
   the estimator and `_push_thin` consume it. Exact estimate, single
   computation.

**Take (3).** It also removes the current duplication: `_push_thin` and
`_reusable_delta_sizes` already both do base-finding, by different routes.

### Sketch

- `_plan_thin_pack(repo, have, want)` → `(object_count, wire_bytes, [UnpackedObject])`.
  Moves the `path → blob` base map (from the `have` commits' trees), the
  `find_reusable_deltas` pass, and the `create_delta` loop out of
  `_push_thin.generate_pack_data`.
- `_estimate_delta_size` calls it and sums `wire_bytes`.
- `_push_thin` calls it and yields the prepared objects.
- Memory is the constraint: holding prepared deltas for a 50-commit chunk means
  holding 50 deltas (small) plus whatever `create_delta` peaks at (two 16 MB
  buffers at a time). Bound the planner by a byte budget and let it return
  "planned k of n commits" so the caller can pick the chunk that fits, rather
  than planning the whole thing and discarding it.

### Also fold in

- **Cap same-path chain depth.** 0.55.167 added `chain_max` to the rejection
  log precisely to catch this; if the one observed `unpack index-pack failed`
  correlates with a deep chain, cap it (git's own default depth is 50, but a
  thin pack against a remote is a different case).
- **Re-check the budget's meaning.** `sync.commit_pack_byte_budget` (3 MB) was
  calibrated in 0.44.x against GitHub returning 408 on ~7 MB packs over a slow
  link. With wire bytes now measured rather than guessed, the number may want
  raising — but only once the estimate is honest.

## Notes

**2026-07-30 — why this is the last big lever.** Chain of costs removed today:
ref advertisement (1000 seed refs, ~57 s/push → gone), loose-object bloat
(repack, 66 s → 18 s), whole-blob payloads (thin pack, 18 s → ~10 s). What is
left is one fixed round trip per push, so the only remaining move is to make each
push carry more.

Measured floor: ~10 s per push on the CABTAL field link, independent of payload.

## Research
