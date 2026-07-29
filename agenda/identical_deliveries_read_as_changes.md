# Content-identical deliveries and repair-only merges must not read as changes

- **Scope & relationships:** azt-collab daemon (+ a small azt follow-on for leg
  5). Split out of `spurious_team_update_prompts.md` on 2026-07-29 (Kent), which
  keeps the azt-side prompt logic; these two legs are daemon-lane work and were
  the only ones left there. Neighbour: `daemon_lock_across_network_io.md` — fewer
  and shorter lock windows mean fewer BUSY→fallback triggers feeding this, but it
  does not remove these two mechanisms.
- **Vision / done-criteria:** a delivery or merge that changes no lexicon content
  leaves no trace that any client can mistake for a change — no mtime bump on
  byte-identical files, and a repair-only merge is classifiable as content-equal.
  Done when a LAN-active machine receiving identical bytes never shows a reload
  prompt, and the azt side has a signal for canon-equal merges.
- **Deadline:** none
- **Waiting on:** Nothing

## Legs (numbering kept from the parent item, so field notes still line up)

### Leg 3 — content-aware post-receive reset (the concrete one)
`_reset_working_tree_after_receive` (`azt_collabd/lan_listener.py:1193`) does a
HARD reset, which rewrites every tracked file whether or not its blob changed.
That bumps mtimes on content-identical deliveries, so any machine with LAN
delivery active can read its own unchanged files as changed. Fix: skip rewriting
a file whose on-disk content already equals the target blob.

Predicted on 2026-07-09 (`azt_persistence_server_sync.md` ~line 260): mtime bumps
"will produce the same false dialog whenever LAN delivery is active." Observed
live 2026-07-25 13:10 on Kent's machine: a pure history-join merge
(`writes_done=0`, conflicts=0) — HEAD advanced over byte-identical content.

Note the azt side no longer depends on this for correctness — `poll_remote_change`
compares the LIFT blob at HEAD against its base blob, and self-heals a latch by
content identity rather than by stat. Leg 3 is still worth doing: it stops the
daemon manufacturing the confusing signal in the first place, and it saves
rewriting every file on every delivery.

### Leg 5 — canon-equal classification for repair-only merges (design needed)
Expose a canonicalized-content SHA (post `_canon_clean`) in `project_status`, so a
merge that only repaired formatting/ordering is recognisable as content-equal.
Then azt can treat canon-equal as benign — a small follow-on in
`CollabSession.poll_remote_change`, alongside the four suppression branches it
already has.

Only worth building once the residue is real: with leg 3 in and the azt-side
suppression shipped (2026-07-29), there may be nothing left for this to catch.
Check before designing.

## Notes
- Field mechanism for the whole family, from the parent item: save hits
  BUSY/unavailable → `'fallback'` direct write → daemon later commits the tree →
  HEAD advances over our own bytes. The azt-side legs of that are done; what
  remains here is the daemon making byte-identical work look like work.

## Research
