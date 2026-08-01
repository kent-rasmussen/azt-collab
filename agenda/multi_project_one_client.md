# Multi-project in one azt-collab client

- **Scope & relationships:** azt-collab/client-ui. Today the client UI is bound to one project at a time, so seeing a second project's settings or sync status means launching another client. Two ways out, and the item covers deciding between them (or doing both):
  1. **Switch projects in place** — a project selector in the running client.
  2. **Show all projects at once** — settings and status surfaced per project, not just the current one.

  Related items:
  - **Sync status board (projects × peers, heads, live job state)** — → `azt-collab/agenda/sync_status_board.md`. Tier A already ships per-peer up-to-date/incoming/N-to-send in settings; the "all projects" half of this item is close to that board's missing axis. Settle whether this item *is* Tier B, or a separate surface.
  - **Two UIs, one daemon: stale toggles can send the WRONG value** — → `azt-collab/agenda/two_uis_one_daemon_stale_toggles.md`. Directly relevant: "open another client" is the workaround this item removes, and it is exactly the situation that produces stale toggles. Fixing this may shrink that item; conversely, an all-projects settings view multiplies the number of toggles that can go stale, so the read-from-daemon fix is probably a prerequisite, not a follow-up.
  - **Drive another device's settings UI over the LAN** (done 2026-07-30) made two-UIs the normal case rather than an edge case.

- **Vision / done-criteria:** From one running client, Kent can see the settings and sync status of every project the daemon knows about — and change the settings of any of them — without launching a second client or restarting. Whichever shape it takes (selector vs. all-at-once), no displayed value is stale relative to the daemon.

- **Deadline:** 2026-08-18 (placed after "Return to work", 2026-08-17)

- **Waiting on:** Nothing

## Plans

Not designed. Open questions before any build:
- Selector, all-at-once, or both? All-at-once answers "what's the state of everything"; a selector answers "let me work on that one". They serve different questions and may both be wanted.
- Does the daemon already expose a per-project status/settings enumeration, or does this need a new endpoint?
- Does the settings UI assume a single project anywhere structurally (module-level current project, single settings object), or is it just the view that's single-bound?
- Interaction with remote/admin settings over LAN: when driving *another device's* UI, does "all projects" mean that device's projects?

## Notes

- Filed 2026-08-01 from Kent, verbatim: "I need to be able to switch projects in the azt-collab ui without opening another client. or else have all settings and status visible for all projects, not just the current one."
- Originally asked for +15d (2026-08-16); repositioned to after the return from vacation.
- NOT investigated — nothing in this file has been checked against the code.

## Research
