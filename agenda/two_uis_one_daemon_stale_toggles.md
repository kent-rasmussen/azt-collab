# Two UIs, one daemon: stale toggles can send the wrong value

- **Scope & relationships:** `azt-collab` — `azt_collabd/ui/app.py`'s settings
  page and every daemon-owned control on it (`work_offline`, LAN sync,
  contributor, device name, per-project repo_slug / image_repo). Made routine by
  [[remote_settings_over_lan]]: a remote session means two UIs against one
  daemon as the normal case rather than an oddity.
- **Vision / done-criteria:** a control never displays a value the daemon
  disagrees with for more than a few seconds, and **never writes based on a
  stale reading**. Two people (or one person on two devices) can hold the same
  settings page open without either being able to set the opposite of what they
  intended.
- **Deadline:** none
- **Waiting on:** Nothing.

## The observation

Field 2026-07-29, phone driving a tablet: *"looks like we have work offline
toggled, but it didn't show in the UI in front of me. Is it sensitive to server
changes that it didn't initiate? there are TWO UIs talking to the server at this
point."*

No, it isn't. Daemon-owned values are read on `refresh()` — screen entry,
Refresh Status, the credentials ladder — and only a handful of things poll (peer
board 5 s, CAWL cache, GitHub-backup line). `work_offline` is not among them, so
a change made elsewhere is invisible until something re-reads.

## Why it's worse than a stale label

**A stale toggle can write the wrong value.** These controls flip from what they
are *displaying*, not from what the daemon currently holds. So with the screen
showing OFF while the daemon is ON:

- the user taps intending "turn it on"
- the UI computes `not displayed` → sends ON
- nothing changes, and the control appears broken

and symmetrically, a user intending to turn something OFF can send ON. On
`work_offline` that decides whether a metered link gets used; on LAN sync it
decides whether the listener runs.

Before remote settings this needed two people at two machines. Now one person
with a remote window open is the ordinary case.

## Plans

1. **Write absolutely, not relatively.** A toggle must read the current value
   and send an explicit target, or send "toggle" as an intent the DAEMON
   resolves. Never `not displayed_state`. This is the correctness half and is
   worth doing even without any polling change.
2. **Re-read on interaction.** Before acting on a tap, fetch the value; if it
   differs from what was shown, update the display and let the user re-decide
   rather than silently acting on the new reading.
3. **Poll the cheap daemon-owned bits** on the interval that already exists
   (`_tick_peer_sync`, 5 s) — `work_offline`, LAN toggle, contributor. They are
   dict reads on the daemon side; the coalescing from 0.55.111/113 means a poll
   costs one computation regardless of how many surfaces ask.
4. **Consider a change counter.** A monotonically-increasing `config_version`
   on `project_status` / a health field would let a UI notice "something else
   wrote" in one cheap read instead of polling each value.

## Notes

Created 2026-07-29. Kent's question exposed it while testing remote settings;
the underlying flaw predates that feature but was hard to hit before it.

## Research
