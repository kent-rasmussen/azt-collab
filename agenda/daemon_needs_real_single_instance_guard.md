# The daemon has no single-instance guard on Windows

- **Scope & relationships:** `azt-collab` —
  `azt_collabd/server.py::_acquire_server_lock`. Backstop for the
  client-side spawn storm fixed in 0.55.190; the two cover different
  halves. Related: [[windows_repack_blocked_by_readers]] (duplicate
  daemons are what actually held the packs) and
  [[daemon_wedges_before_serving]] (its audit assumed one daemon).
- **Vision / done-criteria:** One daemon per `$AZT_HOME` on every
  platform, enforced, with a second instance exiting cleanly and
  saying why. No path by which a machine ends up with none.
- **Deadline:** none
- **Waiting on:** Nothing.

## What's there now

```python
try:
    import fcntl as _fcntl
except ImportError:
    return fd     # Windows: no lock at all
```

The docstring claims a fallback — *"first-come wins by server.json
existence"* — that does not exist: `_spawn_server` deletes
`server.json` before spawning, and the client's liveness gate was
itself broken on Windows until 0.55.177. So a Windows machine has
never been limited to one daemon.

Field 2026-07-31: **14 daemons** on one machine (16 python processes,
2 of them UIs). Proven independently by three watchdog dumps in the
same second carrying three distinct `MainThread` ids.

## Why it mattered more than it sounds

Nearly every "contention" diagnosis of that day named the wrong
contender:

- the `git repack` rename refused — 14 daemons mmap'ing the packs, not
  a status poll;
- `wan_state.json` `PermissionError` killing a push thread — 14
  processes calling `os.replace` on one file;
- a 22 KB push that looked hung — 14 estimators on one disk;
- `already in flight` six times in a millisecond — each process has
  its own `_wan_inflight` and cannot see the others.

## Why 0.55.189 was withdrawn

It added `msvcrt.locking`, which is the right primitive. It was
withdrawn in 0.55.191 without ever being deployed, for two reasons:

1. **Failure mode.** A wrong lock primitive means a daemon that never
   starts, on machines with nobody to diagnose them and no remote
   channel left (the admin channel needs the daemon). Untested code on
   that path, written the night before a 17-day absence, is the wrong
   risk.
2. **It wasn't the fix.** All 14 came from a single process's spawn
   storm — per-instance spawn lock and cooldown, both reset by
   `transports.reset()` on every failed call. That is 0.55.190, it is
   client-side, and it cannot stop a daemon starting.

## Verified 2026-07-31, same evening

0.55.191 pulled and run on that machine: **16 python processes → 4**
(2 with `ui`, 2 without). The client-side spawn fix does what it
claims — one spawn attempt per process instead of fourteen.

The 2 remaining daemons are exactly this item's scope: the spawn lock
is per *process*, so two client processes each legitimately spawn one,
and on Windows nothing stops both surviving. That residue is the
measured size of the problem this item has to solve — not 14, but 1
per concurrent client.

Also visible in that sample: **2 UI processes** where one was launched.
Separate leak, see plan item 4.

## Plans

1. **Port azt's single-instance module** rather than inventing a
   second mechanism. Kent built one for azt precisely because this is
   hard on Windows, and its failure modes are already known — including
   the restart-after-update case that trips it
   ([[restart_trips_duplicate_guard]]).
2. Second instance must exit **saying which pid holds it**, not
   silently.
3. Land it with someone watching the daemon come up on Windows. Never
   as part of a batch pulled to an unattended machine.
4. Separately: **UI processes accumulate too** (2 were live in that
   sample, and the admin-update flow kills windows without always
   ending processes). Different fix, same symptom in Task Manager —
   see [[update_via_admin_double_restart]].

## Notes

Found 2026-07-31 at the very end of a long field day, from Kent
counting processes in Task Manager after `taskkill` failed to make
duplicates stay away. His own remark — *"I had to roll my own single
process module for azt"* — is the plan.

## Research
