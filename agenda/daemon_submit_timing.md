# Save/submit timing: paired azt + daemon capture on real repos

> **Merged 2026-07-31.** This item absorbed
> `azt/agenda/timing_on_real_repos.md` (the azt half of the same capture). The two
> described one grep on one machine: that doc's "is there a gap between azt's
> `submit` and the daemon's total?" was this doc's step 1, and its "does
> `stage[...]` grow with the tree?" was this doc's step 2. One item now owns both
> sides; the surviving azt-side content is folded in below.

- **Scope & relationships:** azt-collab (`azt_collabd/repo.py`, `[submit cost]`,
  shipped 0.55.170) **and** azt (`io_put/lift.py::_log_save_cost`, `save cost:`).
  Both halves are instrumented; no client or contract change is in scope — this is
  a measurement, not a build. Split off from `desktop_save_cost_reduction.md` (Kent
  2026-07-30: "give it to the daemon team"), which is BLOCKED on the numbers this
  produces: it cannot choose between Phase 1 (stage only the submitted path) and
  Phase 3 (surgical per-entry submit, a contract change) without knowing which phase
  of the daemon's work the time goes to. Same split pattern as the client-timeout
  half of `busy_is_not_unavailable`. A large azt-vs-daemon gap hands the problem to
  `daemon_lock_across_network_io.md` instead.
- **Vision / done-criteria:** paired `save cost:` + `[submit cost]` lines captured
  from at least one working machine on a REAL project (`nml`: 16 MB LIFT, 1868 audio
  files), with the daemon version recorded alongside, while somebody is actually
  sorting. Done when `desktop_save_cost_reduction.md` can say "build Phase 1 / build
  Phase 3 / neither, the premise doesn't reproduce" from evidence rather than
  extrapolation.
- **Deadline:** 2026-07-31 — instrumentation SHIPPED and verified 2026-07-30
  (0.55.170); what remains is capturing it where it matters
- **Waiting on:** Nothing — needs a bigger repo and an older machine, which is
  tomorrow's plan (Kent 2026-07-30: "I'll do those tomorrow, on bigger repos on
  older machines")

## What to grep

Both logs, same machine, overlapping time window:

```
grep -E "(submit|save) cost|daemon_version" <azt log> <daemon log>
```

`daemon_version` is in there deliberately: a capture is worthless if the daemon
predates 0.55.170 (no `[submit cost]` line at all) or azt predates the 2026-07-30
label fix (`(no daemon)` conflated "not attached" with "backup write").

**Only lines whose outcome is `ok` count.** `(collab not attached)` and
`(backup/other file — not the hot path)` are not the save path under study — that
distinction is the whole reason the labels were split on 2026-07-30.

```
save cost: 5.6 MB | indent 0.09s + serialize 0.28s + submit 0.39s + replace — = 0.76s (thread) | outcome ok
[submit cost] Demo_en | land 0.01s + stage[1 files] 0.16s + commit 0.21s = 0.38s | codes=['COMMITTED_LOCAL']
```

## The capture (2026-07-31) — on real hardware

Everything below is a measurement, not a build. All of it is unverified as of
2026-07-30 night.

**1. `[submit cost]` on `nml`, and on the slow machine.** The 2026-07-30 numbers
are `Demo_en` on a fast desktop: ~0.4 s total, two tree scans, `stage[0–1
files]`. `nml` has 1868 audio files and a 16 MB LIFT, so both scans and the blob
hash are larger. The shape should hold; the constant probably won't.

  `grep "submit cost" ~/.local/share/azt/daemon-*.txt`

  Read alongside azt's `save cost:` lines. Two outcomes with different fixes:

  - daemon total ≈ azt's `submit` → same "two tree scans" story, **Phase 1**
    (stage only the submitted path) is the whole answer, worth ~0.2 s.
  - azt's `submit` >> daemon total → the gap is **lock contention**, not work,
    and it belongs to `daemon_lock_across_network_io.md` instead.

**2. `stage[N files]` on `nml` specifically.** If still 0–1, staging is a fixed
scan everywhere and Phase 1 is cheap and sufficient. If large on `nml`, the
"~3050 files" folklore was right about *that* project and wrong about `Demo_en` —
worth knowing before narrowing staging.

**2b. Does `commit` scale with the 16 MB blob?** (from the merged azt-side item.)
`commit` was 0.14–0.21 s for `Demo_en`'s 5.6 MB. If it dominates on `nml`, that is
the irreducible floor neither planned phase removes, and the lever is storage
granularity (the parked entry-per-file option) — not Phase 1 and not Phase 3.

Alongside it, the constant question: ~0.6 s per save is not a problem. If `nml` on
a slow machine lands at several seconds per save, the premise reproduces and Phase 1
is worth doing; if it doesn't, close `desktop_save_cost_reduction.md` as
measured-and-fine rather than building speculatively.

**3. The `submit_file` fail-fast (0.55.172), never yet exercised.** It needs a
push holding `project_lock`, so `work_offline` must be **off**. Turning it off
also starts `en`'s 442 parked commits.

  - daemon: `answering BUSY now instead of waiting`
  - azt: `submit ~2s … outcome fallback`, instead of the 42.12 s it cost on
    2026-07-30 (4 × 10 s lock waits + 3 × 0.7 s azt retries)

  A 42 s reading means the fail-fast didn't engage; a ~2 s reading with a frozen
  window means the freeze is azt's modal wait (`Going to wait for
  .!taskwindow…`), which is the no-UI half of `busy_is_not_unavailable`.

**4. Watch `chain_max` if `unpack index-pack failed` recurs.** One rejection in
~50 pushes on 2026-07-30, cause unknown, fallback covered it. 0.55.167 logs
`thin push REJECTED with … deepest same-path chain N` — a large N clustering with
failures means in-pack delta chaining (0.55.165) needs a depth cap. Now more
likely, since 0.55.169 raised `chunk_n` from 1 to 69 and longer chunks mean
longer chains.

## Why azt cannot measure this itself

azt already logs its own side (`io_put/lift.py::_log_save_cost`, grep `save cost:`):

```
save cost: 5.6 MB | indent 0.09s + serialize 0.28s + submit 0.00s + replace 0.00s = 0.37s (thread) | outcome ok
```

`submit` there is the whole RPC round-trip — one opaque number. It can say *whether*
the daemon dominates, never *which part* of the daemon does. And the two answers point
at opposite fixes:

- **tree-proportional** (`_stage_all` = whole-tree `add -A`, ~3050 files per save;
  commit overhead) → **Phase 1** fixes it cheaply, and Phase 3's contract change
  would buy almost nothing.
- **payload-proportional** (receive + parse + splice + write 16 MB) → **Phase 3**
  (per-entry submission) is the main event, as that item's re-weighting argues.
- **irreducible floor** (write a coherent 16 MB file, git-hash a 16 MB blob — the
  same cost for a 2 KB payload as a 16 MB one) → NEITHER phase helps, and the lever
  is storage granularity (the parked entry-per-file option).

Three different decisions, indistinguishable from one round-trip number.

## Plans

Instrument `_submit_file_locked` (`azt_collabd/repo.py:4498`) — there is no
`perf_counter` anywhere on the submit path today. Phases worth separating, in the
order they'd be blamed:

1. **receive/land** the staged file (bytes in → working tree).
2. **`_stage_all`** (`repo.py:2855`) — the suspected tree-proportional cost. Log the
   file count it walked alongside the seconds; the count is what makes the
   "~3050 files" claim checkable rather than folklore.
3. **blob hash + `porcelain.commit`** — the floor. Separate them if cheap to do;
   the hash is payload-proportional and the commit is not.
4. **divergent path**: full parse + merge when HEAD moved. Should be rare after the
   reload-prompt fixes; if it shows up per-save, that is the finding, and it swamps
   everything else (the 2026-07-25 arc).

Follow `_log_save_cost`'s shape deliberately, so the two halves can be read side by
side and grepped the same way:

- one line per submit, one `log.info`, no debug flag — the machine that needs
  measuring is a field machine nobody can attach to;
- the phase names as literal `+`-separated terms summing to a total;
- wrapped so a measurement failure can never break a save (`_log_save_cost` catches
  broadly and logs "save cost line failed").

## Notes

- **Capture while somebody is actually sorting** (from the merged azt-side item) —
  the cadence matters as much as the per-save cost, and the failure mode reported in
  the field is "can't keep up", i.e. saves arriving faster than they complete.
- **Record which machine and which project on every capture.** The 2026-07-30
  numbers lost time precisely because it wasn't obvious what they were measuring.
- Correlating the two logs: azt's `submit` should ≈ the daemon's total plus transport.
  A large gap is itself a finding (queueing behind the daemon lock — see
  `daemon_lock_across_network_io.md`).
- Do NOT change staging behaviour as part of this. Measuring first is the whole
  point; Phase 1 is a separate change that this item's output justifies or kills.
- Watch for the trap azt just hit: an outcome label that conflates unrelated states.
  azt's `(no daemon)` covered both "not attached" and "backup write, never submitted",
  which made three captures look like a downed daemon when the daemon was up and
  simply hadn't been asked. Labels that name a CAUSE must be mutually exclusive.

## Research

### 2026-07-30 — DONE, and the numbers refute the premise

Shipped 0.55.170, verified the same night on `Demo_en` (5.6 MB LIFT):

```
[submit cost] Demo_en | land 0.00s + stage[0 files] 0.25s + commit 0.14s = 0.39s | codes=['NOTHING_TO_COMMIT']
[submit cost] Demo_en | land 0.01s + stage[1 files] 0.16s + commit 0.21s = 0.38s | codes=['COMMITTED_LOCAL']
```

azt's own line for the same saves: `submit 0.39s` / `0.50s`. The two agree to
within ~0.1 s, so there is **no queueing** in the uncontended case and transport
is negligible.

**`stage[0 files]` / `stage[1 files]` — not ~3050.** `_stage_all` adds zero or one
path per save. The ~0.2 s is `porcelain.status()` walking the tree to *discover*
that, not per-file staging work. The "tree-proportional because we stage 3050
files" premise is dead; the file count is exactly what killed it, which is why it
was worth logging.

**Not payload-proportional either.** `land` is 0.00 s — it is an `os.replace` of
an already-staged file, zero-copy — and `commit` is 0.14–0.21 s for a 5.6 MB
blob.

**So it is a floor of ~0.4 s, and it is two whole-tree scans:** the status scan
inside `_stage_all`, and the index work inside the commit.

Consequences for `desktop_save_cost_reduction.md`:

- **Phase 1 (stage only the submitted path)** is the only phase with anything to
  win — worth roughly the 0.2 s status scan.
- **Phase 3 (surgical per-entry submit, a contract change)** is not justified by
  these numbers. The payload is not the cost.
- Total save on this machine is ~0.6 s (azt's indent + serialize + this). That is
  not a problem; the item's slow-CPU premise needs re-measuring on the slow
  machine before either phase is worth building.

**Qualification, deliberately:** measured on `Demo_en`. `nml` has 1868 audio files
and a 16 MB LIFT, so both tree scans and the blob hash are larger there. Capture
the same line on `nml` before generalising — the shape may hold while the
constant does not.
