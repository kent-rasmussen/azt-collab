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
- **Waiting on:** the repro below, on the fix shipped in 0.55.193.
  Nothing else blocks; the remaining questions are all observations
  that repro produces.

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

- **Why does the UI exit at all?** — **ANSWERED 2026-08-04, fixed in
  0.55.193 (code-read only, not reproduced).** It was the third of the
  three candidates: a Kivy exception escaping a Clock callback.
  `_desktop_git_update._work` did `from azt_collab_client import
  transports as _tr` (`ui/app.py:5806`). `_tr` is the module-level
  translator (`ui/app.py:83`), and one assignment anywhere in a
  function makes the name local to the *whole* function. On
  `code == 'UPDATED'` — every real update — the `FAILED` branch never
  runs, so the local `_tr` is never bound; the Clock callback scheduled
  at `:5828` evaluates `_tr('Updated — restarting the service…')` on
  the Kivy main thread, raises, and the default `ExceptionManager`
  policy takes the app down. `_work` is a `daemon=True` thread, so the
  dying process can also take `restart_server()` at `:5832` with it —
  which accounts for step 3 (reopen, daemon still on the old version)
  without needing a second explanation. On `FAILED` the name *is*
  bound, to the module, so `:5854` would raise `TypeError: 'module'
  object is not callable` rather than reporting the failure. Renamed to
  `_transports`.
- **Why doesn't `update_self` leave the new version running?** —
  **ANSWERED by design, no work needed.** `server.py:5892` says so
  explicitly: the endpoint pulls and deliberately does not restart,
  because the caller decides and already has a restart that follows the
  same transport. The UI does call it — `ui/app.py:5832`, on `UPDATED`
  only. So the "one gesture" target may already be met once the crash
  is gone; that is what the repro decides.
- **Which process is being updated, and is that clear?** Still open.
  Pressing Update in a retargeted window updates the REMOTE daemon. The
  local client code is untouched, so a version mismatch afterwards is
  expected and should be stated rather than discovered.
- **The bootstrap limit still applies.** A daemon too old to have
  `/v1/admin/update_self` cannot be updated this way at all; that is
  inherent and already recorded on the parent item. Make sure the
  failure here is distinguishable from that (it should surface as
  `TOO_OLD`, not `FAILED`).

## Repro (what 0.55.193 is waiting on)

### Why it needs a recipe at all

The crash is in the **UI process**, not the daemon, so **none of it
reaches the daemon log.** If the settings window is launched the normal
way — from the picker, as a subprocess — its stderr goes nowhere and the
traceback that identifies the failure is lost. That is the single detail
that makes this repro succeed or waste a run.

### Setup

1. **Two ends.** Either two machines, or one machine driving itself
   over the LAN transport; the retargeted case is the one that matters
   because it is where the field sequence happened. Both must be paired
   already, and the target's peer row must have the admin grant (see
   [[setup_etienne_admin_settings]] / [[remote_settings_over_lan]] for
   how the grant is given).
2. **Put the target daemon's checkout BEHIND its remote** — otherwise
   `git_pull_self` answers `UP_TO_DATE` and the crashing path is never
   entered. On the target's checkout:
   `git fetch && git reset --hard HEAD~1`. Confirm it is behind, not
   dirty; a dirty tree gives `FAILED`, which is a *different* branch of
   this bug.
3. **Launch the admin UI from a terminal and keep the terminal
   visible** — this is the load-bearing step:
   `python -m azt_collabd ui --peer <peer_id>`
   (`__main__.py:5`, `:62`). Do **not** launch it from the picker.
4. **Open the target's daemon log too**, so the pull and the restart
   are visible from the other side: the UI's own Log button, or
   `$AZT_HOME/daemon-<peer>-YYYY-MM-DD_log.txt` on the target.
5. Note the version each end is running before you start
   (`$AZT_HOME/server.json` → `version`), so "which one got updated"
   is answerable afterwards rather than argued about.

### Steps

1. Confirm the settings page header reads `EDITING <their device>`. If
   it does not, the run is not testing the retargeted case.
2. Press **Update**. Do not touch anything else.
3. Record, in order: what the status line says, whether the window is
   still alive, and anything printed to the terminal.
4. If the window survived, wait for the restart and then re-read both
   ends' `server.json` version.
5. Whatever happens, keep going to the end of the field sequence — the
   **second** window death (step 4 of the observed sequence) is still
   unexplained and this is the run that can catch it.

### What each outcome means

- **Terminal shows `UnboundLocalError` / `NameError` on `_tr`** — you
  are running pre-0.55.193 code. Confirms the diagnosis; update and
  re-run.
- **Window survives, status goes `Updating…` →
  `Updated — restarting the service…`, target comes back on the new
  version** — the one-line fix was the whole bug. Close the first two
  bullets above, and the only residue is stating the local/remote
  version mismatch.
- **Window survives but the target is still on the old version** —
  `restart_server()` is not surviving the retarget. That is a distinct
  bug, in the restart path rather than the update path.
- **Window survives, target updates, but a later gesture kills the
  window again** — that is the second death, and it is its own defect.
  Capture the terminal output at the moment it happens.
- **`Update failed: …` with a real message** — the `FAILED` branch now
  works (pre-fix it would have raised `TypeError` instead of showing
  this). Read the detail; distinguish `TOO_OLD` from a genuine failure.

### Regression guard

`py_compile` cannot see a shadowed name and there is no UI test, so
nothing in the suite would have caught this. `pyflakes`/`flake8` over
`azt_collabd/ui/app.py` does. Worth running once over the whole UI
module while here — `:3759` is a second `_tr` shadow, benign because it
binds a callable translator at the top of its function before any use,
but it shows the pattern is not a one-off.

## Plans

1. ~~Reproduce with the daemon log open on both ends~~ → superseded by
   the **Repro** section above, which adds the part that was missing:
   the UI's stderr has to be captured too, so it must be launched from
   a terminal.
2. **DONE (0.55.193, unverified) — fix the UI exit first.** It went the
   other way round in the end: the cause was found by reading the code,
   and the repro is now the *verification* rather than the diagnosis.
   The reasoning for doing this half first still holds — a window that
   survives makes steps 3-4 observable, and nothing below can be
   trusted until it does.
3. Then make the whole gesture finish the job: `update_self` pulls and
   the caller restarts (that split is deliberate, see above), so what
   is left is confirming the restart survives a retarget and
   **reporting the version actually running afterwards**.
4. Re-check the count of gestures. Target is one. May already be met —
   the repro decides.

## Notes

Added 2026-07-31, from field use during a day of remote administration
of two Cameroon machines.

## Research
