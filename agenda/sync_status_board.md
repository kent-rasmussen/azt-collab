# Sync status board: see projects × peers, heads, and what's left

- **Scope & relationships:** azt-collab daemon UI first (the
  settings/collab app both desktop and server-APK run), client
  picker later. Surfaces facts the daemon ALREADY holds — nothing
  new computed. Overlaps project_identity_beyond_langcode.md Tier 1
  (provenance columns) and answers the diagnosis questions this
  2026-07-22 session kept hitting.
- **Vision / done-criteria:** at a glance the user can answer:
  is a sync/merge/clone running right now? what have I got that a
  peer doesn't (and vice versa, where known)? is convergence done
  (all ±0, idle) or still in progress? did a merge go wrong? what
  commit/head is each project at, and where is it bound (dir +
  remote)? — WITHOUT reading daemon logs.
- **Deadline:** none (but demand is proven — kept being the missing
  thing all through the 2026-07-22 workshop)
- **Waiting on:** Nothing

## Plans

### Rows: project × paired-peer
Per project: name, working_dir tail, remote (stored spelling),
current head (short SHA) + entry/commit count, wan_unshared,
last_commit time. Per shared peer under it: shared y/n, their
last_seen_main vs ours (ahead/behind/level where known),
covered_local, last successful delivery time, and LIVE state
(idle / dialing / merging / cloning — the scheduler + lan_push
already know this).

### "Done" must be legible
The core ask (Kent, 2026-07-22: "I don't know when azt-collab
thinks it's done, but I clearly see there's a problem"): a project
is visibly settled when every peer row is ±0 and no job is
running. A running merge/clone/push shows as such, so "still in
progress" is never mistaken for "broken" or "done."

### Head/identity visibility (2026-07-22 driver)
No current UI shows a project's head SHA on any device — so the
disentangle couldn't be verified on the phone ("is its nml still
at the merge tip, nothing recorded after?"). The board must show
head per project per device so a user can confirm state without
git on the command line.

### Data sources (all already present)
`project_status` (wan/lan_unshared, at_risk, n_changes,
last_commit, last_sync_error), `peers.json`
(shared_projects, last_seen_main, last_covered_local,
static_endpoints), scheduler job state + lan_push in-flight flags,
repo head. No new computation — pure surfacing.

## Notes
Origin: 2026-07-22 workshop. Three independent asks converged on
this: "is the sync done / was there a bad merge?"; "which directory
is 'nml' bound to?" (the cross-merge incident); "what head is this
project at?" (couldn't verify the disentangle on the phone).

## Research
