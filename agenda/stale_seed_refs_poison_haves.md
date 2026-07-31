# Stale seed tracking refs are offered as `haves`, so thin pushes are rejected

- **Scope & relationships:** `azt-collab` — the preseed sweep in
  `repo.py` (~7371–7600), `_estimate_delta_size` / `_push_chunked_to_ref`
  and whatever assembles the `haves` set for a push. Explains the
  `unpack index-pack failed` recurrence that
  [[daemon_submit_timing]] parked as a watch-item, and is a likely
  blocker for [[merge_aware_chunk_ordering]] — a project that cannot
  bank a chunk cannot converge by any ordering.
- **Vision / done-criteria:** A push offers only `haves` the server
  actually holds. Thin pushes are accepted, `baf` converges, and the
  sweep stops asking github to delete branches that are already gone.
- **Deadline:** none
- **Waiting on:** Nothing.

## Confirmed (2026-07-31)

`github.com/audioword-ui/baf` has **four** branches: `main`,
`azt-blob-seed-chain`, `azt-side-b3c1fb4f`, and the topic ref
`azt-pending-baf-Mbogno_Celeste___BACKTRAN-OBT-NUBACA`. `nml` has
**two**: `main` and `azt-blob-seed-chain`.

There are **no** `azt-blob-seed-<hex>` branches on either. They are
consolidated into the chain and removed. But NUBACA holds 38 local
`refs/remotes/origin/azt-blob-seed-<hex>` tracking refs, every one of
them pointing at a branch that no longer exists — because the sweep
only drops the local tracking ref *after* a successful server-side
delete, and that delete can never succeed against a ref that is
already gone.

## CONFIRMED IN THE FIELD — 2026-07-31 21:23, NUBACA, 0.55.187

The prune shipped and the numbers moved the same evening:

## THE PRUNE MADE IT WORSE — REVERSE BEFORE BUILDING ANYTHING ELSE HERE

**Result 2026-07-31 21:23–21:44: twenty minutes inside
`_estimate_delta_size` with no output, then abandoned.** Compare
19:51, with 40 haves: the estimate completed promptly and reported
`18 push-time delta(s) + 121 reusable, of 188 object(s)`. With 2
haves it never finished.

**The premise was wrong.** Those tracking refs were not poisoning the
push. A `have` tells the estimator what it does not need to send, and
github evidently still held those objects even though the branches
were deleted — deleting a branch does not immediately remove its
objects, and the seed chain references many of them. Removing 38
haves did not remove lies; it removed knowledge, and enlarged the
object set from 188 to whatever 905 commits imply.

Note the corroborating number that was visible all along and I did not
weigh: `preseed-sweep: 54 blob(s) held by the seed chain also count as
covered` on `baf` — only 54, against 1422 on `nml`. On this project
the chain was covering almost nothing, so the individual seed refs
were carrying the coverage. That is exactly why pruning them hurt here
and would likely not have hurt on `nml`.

**Not undoable by reverting the code.** The 38 tracking refs are gone
locally and cannot be re-fetched, because the branches really are
absent from github. Recovery, if wanted, is from
`.git/logs/refs/remotes/origin/…` reflogs, or by rebuilding coverage
into the seed chain.

**So `unpack index-pack failed` remains unexplained**, and `chain_max`
goes back on the table.

### What this item should become

Not "prune stale refs". The real question is: **what does the server
actually still hold, and how do we know?** A branch's absence is not
evidence its objects are absent. Any future work here needs a way to
verify object presence rather than infer it from ref presence — and
the seed chain is the mechanism that is supposed to make that
knowable. On `baf` it wasn't carrying the coverage it should have
been; that is the thing to look at.

## Superseded — what the prune did do

**Directly attributable to the prune:**

| | before | after |
|---|---|---|
| `offering N remote-tracking ref(s) as haves` | **40** | **2** |
| preseed-sweep delete attempts | 38 → `HangupException` ×38 | none; nothing left to delete |

**NOT attributable — corrected same evening.** `wan_unshared` 925 → 905
and the topic tip moving `b3c1fb4f` → `0b0aa60d` both happened about
half an hour BEFORE the 0.55.187 restart. First read here as evidence
the prune worked; it wasn't. `baf` was never fully frozen — it was
crawling at roughly **20 commits/hour**, which for 905 is ~two days of
uninterrupted running it will not get.

**The actual test is the RATE after 21:23**, not the count. Materially
faster than 20/hour ⇒ the poisoned haves were the bottleneck.
Still ~20/hour ⇒ they weren't, and the cost is elsewhere — most likely
unconsolidated packs making every delta estimate crawl
([[windows_repack_blocked_by_readers]]).

Also still unobserved either way: whether `thin push REJECTED … unpack
index-pack failed` still occurs now that only resolvable haves are
offered. That line's absence or presence is the cleanest single answer.

## The hypothesis this made (now supported)

The push offers remote-tracking refs as `haves`:

```
[sync-trace] pack-size estimate: offering 40 remote-tracking ref(s) as haves
```

A `have` asserts *the server holds these objects, so I may send deltas
against them*. If those branches are gone from github, the objects
behind them may be gone too — so the thin pack arrives delta'd against
bases the server cannot resolve:

```
thin push REJECTED with 29 computed delta(s), 421 reused, 167 whole;
  deepest same-path chain 10
thin push failed (SendPackError: b'unpack index-pack failed')
  — retrying the old way
```

If that chain holds, stale refs are not cosmetic: they are why thin
pushes fail, which is why every push falls back to whole-pack, which
on `baf` means 289 MB over a ~63 KB/s link instead of 201 KB.

**What confirms or kills it:** compare the 40 offered refs against
github's branch list. If most are absent there, confirmed.

## Why it matters more than the wasted seconds

`baf` is **not converging at all**. Evidence 2026-07-31:

- `main` on github last updated *the previous day*.
- The topic ref reads **0 ahead** of main — nothing banked.
- `remaining=925` at 19:54 and still `remaining=925` at 20:07;
  `server_topic_tip='b3c1fb4f'` unchanged across attempts.

So each attempt restarts from 925. That is precisely the failure the
chunked topic-ref design exists to prevent, and 925 commits of one
machine's work stay on that machine.

## Plans

1. **Testing by hand is NOT as easy as it looks.** `git remote prune
   origin` contacts the remote to ask which branches exist, and the
   field machines have no git-CLI credentials — the daemon holds a
   GitHub App installation token, git does not, and these repos are
   private. Proposed 2026-07-31 and withdrawn for that reason.

   The offline substitute: the branch list is already known from
   github's web UI, so every `refs/remotes/origin/azt-blob-seed-<hex>`
   can be deleted locally with no auth. Listing them is safe
   (`git for-each-ref --format="%(refname)"
   refs/remotes/origin/`); a 38-ref delete loop in cmd is not worth
   hand-running, and must not take `azt-blob-seed-chain` with it.

2. **Prune as routine maintenance, in the daemon.** This is ordinary
   git hygiene (`fetch --prune`) and belongs in `maintenance.py`
   beside the repack, where credentials exist and the advertisement is
   cheap to fetch. It replaces the push-delete path of 0.55.186
   outright — that path is trying to reach the same end state by
   asking github to delete branches that are already gone, which
   cannot work and never self-corrects.
2. If confirmed, do the same automatically: reconcile tracking refs
   against the server's advertisement and delete the stale ones
   **locally**. No push, so nothing to hang up on. This replaces the
   batched-delete path of 0.55.186 rather than extending it.
3. Only offer `haves` for refs the advertisement confirms.
4. Re-check whether `chain_max` work is still needed once thin pushes
   are accepted — it may have been chasing this.

## Notes

Split out 2026-07-31 from a day of field diagnosis. 0.55.186 made the
sweep cheap (one batched push, 30 min cooldown) but did NOT fix the
failure — batching was tried on the theory that 38 rapid connections
were being rate-limited, and the single batched delete was refused
identically. That theory is dead; the missing-ref one replaced it.

## Research
