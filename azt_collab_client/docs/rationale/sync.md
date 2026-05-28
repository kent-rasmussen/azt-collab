# Sync rationale — commit/push split, stuck-commit retry, auto-sync silence

> **Conformity contract** for peers — `commit_project` vs.
> `sync_project` routing, work-offline badge,
> `COMMIT_REPEATEDLY_FAILED` handling, auto-sync routing per
> status code — is in `CLIENT_INTEGRATION.md` §§ 17 + 17a + 17b.
> This file is the *why*.

## Why auto-sync must be silent (status-code routing)

`sync_project` / `request_sync` results reach the peer from two
triggers and need different responses:

- **Auto-sync** (peer-initiated; project-select, post-edit
  debounce, background periodic) must be silent on
  configuration-class failures. The user is mid-flow doing
  something else; a popup or forced settings navigation derails
  that flow, sometimes visibly enough to look like project
  selection itself "failed."
- **User-initiated sync** (the user tapped Sync) IS the gesture
  and routes to whatever fixes the problem.

The daemon sees only one shape — `RPC: sync` — so the auto/user
distinction has to live peer-side as distinguishing methods.

**Pre-0.34.1 anti-pattern, closed by this contract.** Treating
every sync failure as a user-facing error in the auto-sync path
manifested as "I selected project B but ended up back on project
A": auto-sync on project-load returned `NOT_A_REPO`, the peer's
error path bailed the project-load flow mid-transition, and the
user landed back on the previously-displayed project. Silent
auto-sync failures keep the user in the project they actually
selected.

## Commit / push split (0.43.0)

Pre-0.43 one RPC (`request_sync`) did commit+push. That
coupling produced (1) a commit-dropping bug — the debounced
path early-returned `COMMITTED_OFFLINE` when offline
*without committing*, so offline swipes piled up dirty files —
and (2) MB-burning eager push on every brief cellular tether.
0.43.0 splits the two concerns:

- **Commits are peer-driven** (`commit_project`). Peer knows
  which edits cohere; debounce collapses bursts; stage +
  commit; no network ever; fire-and-forget.
- **Push is daemon-driven** (scheduler drain). Daemon owns
  "online?" (cached watcher state, not per-call probe — TCP
  probe costs up to 6s offline), "online long enough?"
  (`sync.post_online_grace_s`, default 60s — brief tethers
  don't trigger a push storm), "metered?"
  (`sync.work_offline`, daemon-wide bool — metered state is
  device-scoped, not per-project). Peers read
  `project_status.wan_unshared` + `.lan_unshared` + `.at_risk`
  + `.work_offline` for the badge (renamed from
  `commits_ahead` + `unshared_commits` in v0.47.0).

**Sync button keeps commit+push.** `commit_project` is the
primary commit path; `sync_project` is the user-gestured
"do everything now" rescue for peers that fell behind on
commits. Sync respects the work-offline toggle (refuses with
`S.WORK_OFFLINE_ENABLED`, routes to settings) — the manual
escape hatch is "turn the toggle off", which fires an
immediate drain. Bypassing the toggle from Sync would make the
toggle's effect invisible to the user when they press Sync.

Sync button bypasses the post-online grace (pressing Sync IS
the "I'm intentionally online" gesture) and gets a fresh
`_has_internet` probe (a deliberate user action, 3s wait is
expected).

## Stuck-commit retry

The daemon's scheduler retries failed commits in the background
(doubling backoff: 30s, 60s, 120s, … capped at 1 hour) so a
transient cause (briefly locked index, transient I/O hiccup)
clears without bugging the user. A persistent cause produces
two failed commit attempts, at which point `COMMIT_REPEATEDLY_FAILED`
lands on the next peer-driven sync's result and the peer
toasts. Threshold 2 is the smallest that excludes one-shot
flukes while firing soon enough to matter; same alarm surfaces
on the next peer gesture either way, so the scheduler's value
is solely the transient-recovery case (false alarms avoided).

No separate peer-side poll surface — the counter persists
between gestures, so the next peer sync naturally carries the
alarm without polling. `ProjectStatus` fields
(`commit_failure_count` et al.) stay for diagnostic UI; the
alarm flows through `result.statuses` only. Retry assumes the
daemon is alive; daemon-resurrection rides the sticky-bound
service contract (next peer RPC lazy-spawns and
`reconcile_on_startup` flips in-flight jobs to
`JOB_INTERRUPTED`).

Deep dive of the abandoned alternatives (same-call wait-and-retry,
peer-side poll surface, 1s/3-failure thresholds) lives in
CHANGELOG 0.41.27 + 0.41.29.
