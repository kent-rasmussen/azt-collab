# The daemon should say what it is doing, so "busy" ≠ "dead"

- **Scope & relationships:** `azt-collab` — a cheap activity/heartbeat surface
  the daemon maintains and anything outside can read without waiting on the
  work itself. Consumers: `azt_collab_client/transports/loopback.py`
  (`SERVICE_WEDGED` / `SERVICE_SLOW` wording), `azt_collabd/watchdog.py`
  (restart decisions), the settings UI, and any future auto-recovery.
  Prerequisite for automating recovery of a stuck daemon; related to
  [[token_expiry_kills_long_push]] (a long push is exactly the state that
  looks dead) and to the coalescing work in 0.55.111/113.
- **Vision / done-criteria:** anything that can reach the daemon can tell
  **"busy with X"** from **"not running"** without blocking. `SERVICE_WEDGED`
  is only ever emitted when the daemon genuinely is not working, and a message
  that reaches a user says what it is waiting on. Only then is automatic
  restart safe to build.
- **Deadline:** none
- **Waiting on:** Nothing.

## Why

Kent 2026-07-29, on a Windows desktop showing `SERVICE_WEDGED (pid 1564 is
running but not answering /v1/health) … restart that process`: *"is this
recoverable? and can we do it for them? I don't want people to figure this out
themselves."*

The instinct is right, and acting on it with today's information would destroy
work. `SERVICE_WEDGED` means only *"running but not answering /v1/health"* —
which is exactly what a daemon looks like while it is **busy with real work**:

- 2026-07-29, phone: a config read took **4 minutes 19 seconds** on a daemon
  saturated by duplicate status walks (0.55.111/113/115). Anything that had
  killed it would have interrupted a merge and a push mid-flight.
- The same day, watchdog dumps repeatedly caught 20+ threads inside single
  filesystem/history walks, plus a LAN `upload-pack` mid-transfer and a peer
  fetch blocked in an SSL read.

**We cannot currently distinguish "wedged" from "busy" from outside**, so every
automatic remedy is a coin flip against in-flight data. That is why 0.55.116
deliberately did NOT add an auto-restart while it did automate the
`staged_missing_retryable` case, where the safe action is knowable.

## What it needs to be

1. **Written from outside the work, not by it.** The point is that a thread
   deep in a walk or a socket read cannot report; the signal has to be
   maintained where it is still cheap to touch — a small mmap/JSON heartbeat
   updated at phase boundaries (`push:nml phase-A chunk 41/378`,
   `merge:baf`, `upload-pack:→841d43a8`, `idle`), with a monotonic stamp.
2. **Readable without an RPC.** If reading it requires the request path that is
   already blocked, it answers nothing. `$AZT_HOME` file, or a header on the
   one endpoint that never touches git.
3. **Bounded staleness, honestly worded.** "last progress 3 s ago, doing X" is
   actionable; "alive" is not. A stamp that stops advancing IS the wedge
   signal — that is the thing we currently cannot detect.
4. **Then, and only then:** `SERVICE_WEDGED` becomes "no progress for N s while
   claiming to do X", the watchdog can restart on *stalled progress* rather
   than on *unanswered health*, and the UI can say "waiting on a 17 MB upload"
   instead of telling a linguist to restart a process.

Note `azt_collabd/watchdog.py` already has per-loop `heartbeat_interval` /
`heartbeat_expectations` (0.55.x) — that is the same idea for internal loops
and is probably the seam to generalise rather than a new mechanism.

## Plans

Sketch only; not designed yet.

1. Heartbeat file + phase stamps at the boundaries that already log.
2. Teach `loopback.py` to read it before wording `SERVICE_WEDGED`.
3. Teach the watchdog to restart on stalled-progress instead of
   unanswered-health.
4. Surface "what it is doing" in the settings UI.
5. Revisit auto-restart, which is the whole point of the item.

## Notes

Created 2026-07-29 out of the `staged_rejected` discussion (0.55.116).

## Research
