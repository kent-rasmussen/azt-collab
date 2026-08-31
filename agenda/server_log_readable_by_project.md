# Server log readable by project

- **Scope & relationships:** azt-collab/daemon (logging). Two complaints, one surface:
  1. **Attribution** — the daemon serves many projects, but project-specific activity isn't clearly marked as belonging to a project, so reading the log means inferring which project a line is about.
  2. **Flooding** — low-value lines drown the ones that matter. Kent's named example: `"ingoring this trigger"` [sic — verify the actual spelling in the source; if the log really is misspelled, that's its own small fix].

  Related items:
  - **Audit log lines for announce-at-point-of-action** (CLAUDE.md invariant #15) — → `azt-collab/agenda/log_lines_announce_at_point_of_action.md`. Same log, different axis: that item is about *when* a line is emitted (announce at the point of action, not before/after); this one is about *what* is emitted and how it's attributed. Likely worked in the same pass.
  - **Modernize logging: real rotation, survive a self-restart** — → `azt/agenda/modernize_logging_rotation.md`. That one is **azt only**, not the daemon — but if this item introduces per-project log routing, the retention question lands in the same place.
  - **Button in azt to store the log to the server's shared files** — → `azt/agenda/log_to_server_button.md`. Kent's note there: the *daemon* logs he can already get from the daemon. A log that's readable by project is what makes that retrieval worth doing.

- **Vision / done-criteria:** Reading the daemon log, it is immediately obvious which project each project-specific line belongs to, and the routine/no-op lines don't bury the ones that report real work. Kent can answer "what happened to project X today?" by reading, not by grepping and inferring.

- **Deadline:** none (placed after "Return to work", 2026-08-17)

- **Waiting on:** Nothing

## Plans

Not designed. Open questions before any build:
- **Attribution mechanism:** a consistent `[project]` prefix on every project-scoped line, a logging adapter/context that carries the current project, or genuinely separate per-project log files? A prefix is cheapest and keeps one chronological stream; separate files break the interleaved story of a sweep across projects.
- **Which lines are project-scoped at all?** Some daemon work is global (boot, listener, peer discovery), some is per-project (sync, merge, repack, submit). The split needs establishing before a prefix can be applied.
- **What flooding to cut vs. demote:** precedent from azt's log-cleanup pass (done 2026-07-15) was *demote, don't delete* — DEBUG loglevel restores the detail. Same rule probably applies here.
- **The trigger line specifically:** find the emitter, and decide whether the fix is demotion, deduplication (N identical triggers ignored → one line), or a rate limit.

## Notes

- Filed 2026-08-01 from Kent, verbatim: "clean up server log, so activity is more clear by project, when project specific. and so less relevant lines don't flood ('ingoring this trigger')".
- NOT investigated — nothing here has been checked against the code.

## Research
