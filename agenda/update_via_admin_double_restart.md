# Updating via admin costs two UI shutdowns and a manual server restart

- **Scope & relationships:** `azt-collab` — `/v1/admin/update_self`
  (0.55.161–164), the Update button in `azt_collabd/ui/app.py`, and what
  the UI process does when the daemon it is driving goes away.
  Follow-up defect on [[update_daemon_at_a_distance]], which built the
  mechanism; this is what using it actually feels like. Related to
  [[desktop_collab_unavailable_visible]] (a UI that loses its daemon
  should say so and re-attach, not exit) and to
  [[two_uis_one_daemon_stale_toggles]] (same "two processes, one
  daemon" seam).
- **Vision / done-criteria:** One gesture. Press Update, and when it
  finishes the daemon is running the new version and the window is
  still usable — or it says plainly what is left to do. No manual
  server restart, and no second window death to get there.
- **Deadline:** none
- **Waiting on:** Nothing.

## The observed sequence (Kent, 2026-07-31)

1. Press Update in the admin (retargeted) settings UI.
2. **The UI shuts down.**
3. Reopen it, and the daemon is still on the old version — so restart
   the server by hand.
4. **The UI shuts down again.**
5. Only now is the new version actually running.

Two window deaths and a manual restart for one update. Every step after
(1) is something the user has to know to do, which is exactly the
property remote administration was supposed to remove — the point of
the feature is not asking someone at the far end to perform a sequence
they do not understand.

## What to work out first

Unverified; do not build against these until checked.

- **Why does the UI exit at all?** It lives in a separate process from
  the daemon (that is the whole reason Restart is safe from there). A
  transport failure should surface as a status line, not a process
  exit. Find whether it is an unhandled `ServerUnavailable` on a poll,
  a Kivy exception escaping a Clock callback, or a deliberate quit.
- **Why doesn't `update_self` leave the new version running?** If the
  pull lands but the daemon keeps executing the old image, the endpoint
  is doing half its job — the name says update *self*, so re-exec
  should be part of it, not a separate gesture. Check whether it
  already tries and fails, or never tries.
- **Which process is being updated, and is that clear?** Pressing
  Update in a retargeted window updates the REMOTE daemon. The local
  client code is untouched, so a version mismatch afterwards is
  expected and should be stated rather than discovered.
- **The bootstrap limit still applies.** A daemon too old to have
  `/v1/admin/update_self` cannot be updated this way at all; that is
  inherent and already recorded on the parent item. Make sure the
  failure here is distinguishable from that.

## Plans

1. Reproduce with the daemon log open on both ends — the UI's own exit
   will not be in the daemon's log, so capture both.
2. Fix the UI exit first. It is the part that makes the flow feel
   broken, and a window that survives makes everything else
   observable.
3. Then make `update_self` finish the job: pull, re-exec, and report
   the version actually running afterwards.
4. Re-check the count of gestures. Target is one.

## Notes

Added 2026-07-31, from field use during a day of remote administration
of two Cameroon machines.

## Research
