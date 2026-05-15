# Notes to the daemon

**Live queue only.** Outstanding items peers have noticed and want
the `azt_collabd` / server-APK side to fix. Filed here (inside
`azt_collab_client/`) rather than the per-peer CHANGELOG so:

- the symlink propagates them into every sister app's tree
- the daemon team sees them in one canonical place
- the note moves with the package if the canonical home ever
  changes

**When you act on an item, delete it from this file** — the
CHANGELOG is the historical record. This file holds only the
queue.

**Standing rules / architectural invariants do NOT belong here.**
Promote them to `CLAUDE.md` (daemon-side or client-side
architecture / rationale) or `CLIENT_INTEGRATION.md` (peer-facing
conformity contract) instead. A standing item dressed as a queue
item silently turns this file into a reference shelf, which
defeats the "live queue" property. Previous standing items moved
to:

- "Daemon is the sole authoritative source" → `CLAUDE.md`
  hard rule #8 + "Daemon-owned state" section.
- "Project-bound surfaces in daemon UI (Phase 3)" →
  `CLIENT_INTEGRATION.md` § 12b "Project-bound actions live
  in the daemon settings UI."
- "Suite-wide package-upgrade handling" →
  `CLIENT_INTEGRATION.md` § 19 "Package-replacement handling"
  (acted on 2026-05-15 — daemon 0.41.28 / client 0.41.26
  ship `SuiteSelfReplaceReceiver` + the peer-side backstop
  helper).
- "`request_sync` skips the commit step while offline" →
  rebuilt as the commit/push split (acted on 2026-05-15 —
  daemon 0.43.0 / client 0.43.0). Debounced
  `commit_project` runs commit-only and never blocks on
  network; push is driven by the scheduler's drain loop
  (online + `sync.post_online_grace_s` +
  `sync.work_offline`). See `CLIENT_INTEGRATION.md` § 17 for
  the new routing.

