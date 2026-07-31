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

## RESOLVED for Windows — 0.55.192

The `msvcrt.locking` guard is in and field-validated. NUBACA ran
0.55.190 (which carried it) for over an hour and booted normally;
`server.lock` went from 0 bytes to **7** — the `lseek(1)` skip, a
five-digit pid, a newline. Only that branch writes it.

Withdrawn in 0.55.191 out of caution, restored in 0.55.192 once the
evidence arrived. Kent's point is why it could not be left withdrawn:
the machine was already running it, so the revert would have silently
removed a working guard at the next pull.

What remains of this item is the **port from azt's single-instance
module** — still worth doing, because azt's version handles the
restart-after-update hand-off ([[restart_trips_duplicate_guard]])
that this one has not been tested against. Lower priority now.

## Why 0.55.189 was withdrawn (superseded)

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

Also visible in that sample: **2 UI processes where Kent launched one**
— his observation, repeated after I offered an explanation for it.
Separate leak, see plan item 4.

**CONFIRMED A LEAK.** `wmic` showed **no `--peer` on either** — two
identical `python -m azt_collabd ui` processes from one launch. So it
is not the deliberate second window for driving a peer
(`"start a second window rather than retargeting a running one"`),
which was the benign reading offered twice and wrong both times.

`__main__.py` does not spawn a second UI: `ui` calls `ui_main()`
in-process. So the duplicate originates elsewhere.

**PIDs say they didn't start together.** UIs at ~18k and ~22k;
daemons at ~4k and ~6k. Windows allocates roughly ascending, so:

- the daemons started **well before** the UIs and were therefore not
  spawned by them — they are older survivors;
- the two UIs are ~4000 PIDs apart, i.e. a lot of process creation
  between them, so they started at **different times**.

That reads less like "one launch spawns two" and more like **an older
UI process that never exited** — which is
[[update_via_admin_double_restart]]: the window dies, the process
does not. 2026-07-31 involved many update/restart cycles on that
machine, which would accumulate exactly this. Suggestive, not proof:
Windows reuses PIDs.

If that is right, the fix is "make the UI process exit when its window
closes", not "stop double-spawning" — and the two stale daemons are
independently explained by the pre-0.55.191 spawn storm rather than by
anything the UIs did.

**Our code has no path that produces a `--peer`-less duplicate.**
Both places checked 2026-07-31:

- `__main__.py` — `ui` calls `ui_main()` in-process; no spawn.
- `ui/app.py:1878` — the only `Popen` of a UI, and it **always**
  passes `--peer`.

Neither observed process carries `--peer`, so neither came from us.
That points at the launcher (shortcut / `.bat` / alias) firing twice,
or something else outside this repo.

Also ruled out by Kent, in order, each after I proposed it: the
deliberate `--peer` second window; an older UI surviving an update
cycle; Git Bash failing to kill a Ctrl-C'd child. He had shut
everything down via Task Manager multiple times and launched once.

**It is not specific to the collab UI.** Kent, same evening: the same
`wmic` count shows **4 processes on azt load as well**. Combined with
"no path in this repo produces a `--peer`-less duplicate", the common
factor is the launch environment both apps share on that Windows
machine, not azt-collab.

Where the thread continues (deliberately not chased from this repo):
azt re-execs itself into its venv (`ensure_venv` / `sysrestart`),
which produces a parent and a child by design — and
[[restart_trips_duplicate_guard]] is already about that hand-off
misbehaving on Windows.

**Next datum, uncollected:** parent PID. It separates "the UI spawns a
copy of itself" from "the launcher fires twice", which have completely
different fixes:

```
wmic process where "name='python.exe'" get processid,parentprocessid,commandline /format:list
```

If one UI's parent is the other UI → self-spawn, look inside the app's
startup (bootstrap / self-update / `open_server_ui`). If both share a
shell/explorer parent → the shortcut or launcher script is starting two.

**This reorders the item.** If one launch reliably yields two clients,
then "only run one client" is not an available discipline, and the
daemon-side single-instance guard stops being a backstop and becomes
the load-bearing fix.

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
