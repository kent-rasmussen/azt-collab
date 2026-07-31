# Audit log lines for announce-at-point-of-action

- **Scope & relationships:** `azt-collab` — every `print(...)` in
  `azt_collabd/` (and the daemon-facing parts of `azt_collab_client/`).
  Enforces invariant #15 in `azt-collab/CLAUDE.md`, added 2026-07-28.
  Related: `agenda/daemon_lock_across_network_io.md` (its WAN half is
  still open, and the 147 s stack that motivated it was found *because*
  a log line was honest about what it held).
- **Vision / done-criteria:** every log line in the daemon either (a) is
  emitted at the last statement before the action it describes, (b)
  reports an outcome after the fact, or (c) is worded as a candidate /
  attempt rather than an act. No line asserts a value it cannot know
  (a timeout, a cause, a peer's state). No unconditional intent line
  sits beside a rate-limited failure line. Done when the sweep has
  visited every print site and each one is in one of those three
  categories.
- **Deadline:** none
- **Waiting on:** Nothing

## Plans

Mechanical sweep, file by file, highest-traffic first — the ones that
cost real diagnosis time this week:

1. `scheduler.py` — drain loops, run-to-completion, iface watcher.
   0.55.58 + 0.55.64 fixed the github line; the rest is unaudited.
2. `lan_push.py` — dial / peek / merge / fan-out lines. 0.55.60–0.55.62
   fixed the peek classification and added the moving-head line; the
   `advanced <peer> main:` and `no-op` lines are unaudited.
3. `lan_listener.py` — absorb, reset, ACL, presence. 0.55.54 fixed the
   lock-busy timeout; 0.55.63 added the unservable-head line.
4. `repo.py` — sync / push / merge steps, the `wan-unshared` /
   `lan-unshared` / `at-risk` triples.
5. `ui/app.py`, `server.py` — the settings-page and RPC lines.

Two checks per line:

- **Truth-at-emission:** what must hold for this sentence to be
  accurate, and has the code established it *at this point*?
- **Pairing:** if the failure counterpart is rate-limited or deduped,
  is this line suppressed with it? An unconditional intent beside a
  suppressed failure reads as success.

## Notes

Established 2026-07-28 after five instances in one field day. The full
list, with the specific damage each caused, is in `CLAUDE.md`
invariant #15 — that is the canonical statement of the rule; this file
is the work of applying it to existing code.

Worth noting for whoever does the sweep: two of the five were
introduced *while fixing a previous instance* (0.55.58's wording was
written to fix the third instance; 0.55.54's hardcoded "5s" was written
in the same pass that made the timeout a parameter). Re-read each edit
against the rule before moving on.

## New instances found 2026-07-31 (field day)

Six more, all the same shape. Three are fixed; three are open.

**Fixed in 0.55.173–0.55.186:**

1. `[azt_collabd] listening on <host>:<port>` was printed at bind,
   ~100 lines before `serve_forever()`. A daemon stalled in boot
   therefore read as healthy. Now `bound to … — not serving yet`, with
   a separate `serving on …` where serving actually starts.
2. `[maintenance] repack …` carried a comment asserting "lock held" as
   an already-settled precondition — 0.55.171 had removed the lock and
   left the claim. A comment can violate this invariant too.
3. `[lan-admin] … it is changing THIS device: GET '/v1/lan/pending'`.
   A GET changes nothing. Reads are now rolled up and only writes make
   that claim.

**Open:**

4. `[sync-trace] push done (advanced 169 commits)` printed **twice**,
   20:11:42 and 20:12:02, `codes=['PUSHED']` both times — the second
   after `wan_unshared` already read 0. Run-to-completion's second
   attempt advanced nothing and said it advanced 169.
5. `prepare_share_bundle` returned bare `None` for both "unreachable"
   and "the daemon answered and refused", so a live session reported
   "Could not reach the AZT Collab daemon". Fixed in the client
   (0.55.179/180), but the shape — one value standing for two answers
   with opposite remedies — is the same defect in return-value form,
   and `get_daemon_log` had it too.
6. `Sharing is only available on Android` shown on a desktop
   diagnostics share: `share_text`'s platform refusal replacing the
   message it was handed. Fixed 0.55.178.

The pattern across all six: the line is written where the *intent*
lives, not where the *outcome* is known. Items 5 and 6 extend it —
a return value or a substituted string can assert something false
just as a log line can.

## Research
