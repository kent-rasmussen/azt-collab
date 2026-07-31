# Windows can never repack a project the daemon is reading

- **Scope & relationships:** `azt-collab` — `maintenance.py`
  (`repack_project` / `sweep`), and every dulwich reader in the daemon
  that mmaps pack files. Causally upstream of
  [[daemon_wedges_before_serving]]: unconsolidated packs are what make
  the ancestry walk slow enough to have wedged boot. Needs the same
  per-project idle signal as [[daemon_activity_signal]].
- **Vision / done-criteria:** A Windows machine repacks successfully
  without a human stopping the daemon, and without maintenance
  starving saves the way holding `project_lock` did.
- **Deadline:** none
- **Waiting on:** Nothing.

## What happens

```
[maintenance] repack 'baf': git exited 128 after 524s — fatal: renaming pack
to '.git/objects/pack/pack-9c1d….pack' failed: Permission denied
```

`repack -a -d` must delete the old packs and rename the new one into
place. Windows refuses a rename over a file another handle holds open;
POSIX allows it. The daemon mmaps packs through dulwich on every status
poll, so anything watching the project keeps a reader alive for the
whole 524 s — an open settings window is enough, and during diagnosis
there is always one.

0.55.171 removed `project_lock` from the sweep, correctly: holding it
starved saves so badly that azt fell back to writing files with no
commit. But the justification was POSIX-specific — *"deleting a pack on
POSIX leaves open descriptors valid"* — and on Windows that reasoning
does not hold.

## Why it compounds

Packs never consolidate, so `_lookup_in_packs` searches every pack for
every object. The push-time delta estimator does that thousands of
times per attempt:

```
_push_chunked_to_ref → _estimate_delta_size → repo.object_store[sha]
  → object_store.get_raw → _lookup_in_packs → pack.unpack_object
```

Field 2026-07-31: the watcher loop stalled 563 s with `wan-drain` in
exactly that stack. The maintenance meant to prevent slow object access
was, by failing, guaranteeing it.

## Done in 0.55.185

A failed repack now enters a **6 h per-project cooldown** (in memory, so
a daemon restart clears it — a restart is also when the readers go
away). Previously `_write_floor` was only reached on success, so a
failed repack left its own trigger condition intact and the next sweep
ran the identical 524 s command, indefinitely. The failure also now
says it is a file lock rather than a permissions setting.

That stops the bleeding. It does not make Windows able to repack.

## Options (none chosen)

1. **Per-project idle signal**: repack only when nothing has touched
   the project for N seconds, then hold `project_lock` for the run. A
   machine left with a team overnight repacks; a machine being actively
   polled does not — which is correct. Needs the activity signal that
   [[daemon_activity_signal]] is about.
2. **Windows-only lock with a hard cap**: take `project_lock`
   non-blocking with a short repack timeout. Rejected on current
   evidence — `baf` needs >524 s, so a short cap never completes.
3. **Close readers first**: requires knowing every open dulwich `Repo`
   for the project. `_track_opened_repos` (0.54.1) exists; whether it
   can guarantee a quiescent moment is unverified.

## Workaround that works today

Stop the daemon and repack by hand — no daemon, no handles:

```
git -C "<working_dir>" repack -a -d --window=50 --depth=50
```

## Notes

Found 2026-07-31 while chasing why a push was slow. Worth doing before
any further push-performance work on Windows field machines: the
estimator's cost is downstream of this.

## Research
