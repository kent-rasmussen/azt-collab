# Spurious "team updated your data" reload prompts on no-op / trivial updates

- **Scope & relationships:** azt-collab daemon/client seam + azt desktop
  write seam (`azt/backend/core/collab.py`, `azt/io_put/lift.py`).
  Reopens the deferred tail of azt-collab/agenda/azt_persistence_server_sync.md
  (F4(c), provisionally closed 2026-07-09 with "reopen if the window
  returns" — it returned 2026-07-25). Upstream trigger is likely the
  active item azt-collab/agenda/daemon_lock_across_network_io.md
  (BUSY saves) — which also carries the write/merge-volume work
  (merge is O(whole LIFT) not O(changed); per-entry incremental
  follow-on; ASR writer through debounced `commit_project` instead
  of whole-file `submit_file` per transcription). Less writing =
  fewer/shorter lock windows = fewer BUSY→fallback triggers here,
  but it does not remove the false-prompt logic bugs — the two
  items are complementary. (azt's whole-LIFT-per-save editor model
  itself is the § 8b contract; no standalone item exists for
  save-side per-entry writes.) Also resolves the
  `"update warning" identity` watch item on
  azt/agenda/azt_run_with_server.md.
- **Vision / done-criteria:** a solo worker on an offline computer
  never sees a reload prompt (poll-latch "Team changes detected" or
  save-path "Merged your changes with updates from your team") unless
  the LIFT content genuinely changed beyond what they themselves
  wrote. Prompts on real peer content remain.
- **Deadline:** none
- **Waiting on:** VERIFICATION of the self-authored-range suppression built
  2026-07-29 (below). On the machine that was asked to take its own single
  commit, the log line to look for on the next save is
  `Not prompting: HEAD advanced by N change(s), all written by us (…)`.
  If a prompt still appears, that message names why — a second author, or a
  capped walk.
- **Split off 2026-07-29 (Kent):** the two daemon-lane legs (3 = content-aware
  post-receive reset, 5 = canon-equal classification) moved to
  `identical_deliveries_read_as_changes.md`. This item is now the azt-side
  prompt logic only.

## Plans

### Justification, not just suppression (AZT team spec, 2026-07-27)

The prompt kept appearing "without any justification for it" — and a
prompt that can't say what changed is indistinguishable from a
spurious one, so users learn to dismiss both. AZT team's spec:
*"project_status would have to report the commits between azt's base
and HEAD — count plus distinct author names — since azt in collab mode
never touches .git itself and has no other way to know. That's a
small, well-shaped endpoint change (the daemon already walks that
range for the board), then a one-line format change in
collab_offer_reload."*

**Daemon half SHIPPED 0.54.92:** `repo.changes_since()` +
`GET /v1/projects/<lang>/status/<base_sha>` +
`project_status(langcode, since_sha=…)` →
`ProjectStatus.changes_since` = `{known, count, capped, authors}`.
Contract in CLIENT_INTEGRATION.md § 8b obligation 3a. Base is a path
segment (the dispatcher matches exact segments, so `?since=` would
miss the route). Merge bot excluded from authors; `known: False` means
can't-tell and must never render as nothing-changed; the walk is
capped for poll safety, with `capped: True` making the count a floor.

**azt half SHIPPED:** `CollabSession.changes_summary()` renders it
(`"{count} change(s) from {who}"`, `"(couldn’t tell what changed)"` for
`known: False`), appended outside the existing msgid so the five
catalogs keep their translation.

## STATUS CORRECTION 2026-07-29 — this doc was stale

Legs 1 and 2b, and more besides, are IN. `poll_remote_change` now
suppresses four ways before it will latch:
- HEAD moved but the LIFT on disk is unchanged → `'benign'`, base
  adopted (leg 1's purpose, plus peer changes to non-LIFT files);
- every commit in the range is a daemon merge (`bot_count`, no human
  commits) → `'benign'`;
- a latch already set self-heals when the LIFT blob at HEAD equals the
  blob at base and disk still matches what we wrote (F2, by CONTENT
  identity rather than stat);
- **NEW 2026-07-29: every human commit in the range is OURS** —
  see below.
And leg 1 itself (`record_lift_stat` after the fallback `os.replace`) is
implemented at `azt/io_put/lift.py:1265-1276`.

### The hole that remained (Kent 2026-07-29, field)
A computer updated that morning asked to take a "team update" citing
ONE change **from that same computer**. A commit authored by our own
contributor is a human commit like any other, so it latched — and the
obl. 3a summary then named us as the author. The citation naming the
same machine IS the diagnosis, not a coincidence.

**FIXED (azt, awaiting live verify):** `CollabSession._only_our_own_commits`
+ a fourth suppression branch in `poll_remote_change` — adopt HEAD and
return `'benign'` when every author in the range is our own contributor
(`_client.get_contributor()`). Deliberately strict, because a false
positive silently swallows real incoming work: requires `known`, NOT
`capped` (a capped walk hides authors past the cap), a non-empty author
list, and a set contributor name matching every author (casefolded,
stripped).
**Known limit, logged when it bites:** two machines sharing ONE
contributor name are indistinguishable here. Device names differ in
practice; a shared name belongs to
`project_identity_beyond_langcode.md`, not to a guess here.

Fix legs, in likely order of payoff (leg 1 done; leg 2b superseded by
the four branches above):

1. **azt-side (one-liner): re-record the lift stat after the legacy
   fallback replace.** `submit()` returning `'fallback'`
   (`collab.py:284`) is the ONLY outcome branch that never calls
   `record_lift_stat()` — it can't, because the caller
   (`io_put/lift.py:1247` seam) does the `os.replace` after it
   returns. The caller must record the stat post-replace. Without
   it, azt's own direct write reads as a foreign change forever
   (stat-based `_lift_changed_on_disk`, collab.py:184).
2. **Daemon-side: trivial-merge signal on `MERGED_WITH_LOCAL` —
   SHIPPED 0.54.73 (daemon half).** `_submit_file_locked` (repo.py)
   carries `merged_identical=True` in the Result params when the
   merged bytes equal the submitted bytes; `[submit_file]` log line
   carries `identical_to_submitted=`. azt-side consumption (adopt
   head as base, no latch/prompt) is leg 2b below, azt repo.
3. **Daemon-side F4(c): content-aware post-receive reset.**
   `_reset_working_tree_after_receive` (lan_listener.py:1193) does a
   hard reset that rewrites every tracked file whether or not its
   blob changed → mtime bump on content-identical deliveries →
   false 'changed' on any machine with LAN delivery. Skip rewriting
   files whose on-disk content already equals the target blob.
4. **azt-side: content-hash fallback in `_lift_changed_on_disk`**
   when the stat differs (hash the file, compare to what we last
   wrote / the base blob) — lets the F2 self-heal actually clear
   mtime-bump latches instead of being gated on the same stat check.
5. **(design needed) canon-equal classification for repair-only
   merges** — daemon exposes a canonicalized-content SHA (post
   `_canon_clean`) in `project_status`; azt treats canon-equal as
   benign. Only matters once 1–4 are in and the residue is real.

Lane note: legs 2, 3, 5 are azt-collab (this repo); legs 1, 4 are
azt-side (do from an azt session, or on explicit instruction).

## Notes

- Field report (Kent 2026-07-25): on an **offline** computer (no
  wifi, no LAN sweeping, nobody else working), azt repeatedly
  prompts "you have updated the data — reload", on no-op and
  trivial merge updates.
- Mechanism (no network needed): save hits BUSY/unavailable →
  `'fallback'` direct write → stat record stale for azt's OWN bytes
  → daemon later commits the tree (respawn reconcile / debounced
  commit_project / next save's whole-tree staging) → HEAD advances →
  (a) poll: HEAD≠base + stat-changed → "Team changes detected"
  latch; F2 self-heal blocked (gated on the same stat check);
  (b) next save: stale base_sha → divergent path →
  `MERGED_WITH_LOCAL` ("Merged your changes with updates from your
  team", conflicts=0, ours-merged-with-ours).
- Supporting evidence from THIS machine's log
  (daemon-8f19208f-2026-07-23_log.txt): `submit_file 'nml'` returned
  `codes=['BUSY']` every ~3 min for hours overnight, HEAD frozen at
  fa025e21ab5e — the lock-across-network-I/O shape. On a no-network
  machine with lan.allow_sync on + recorded peers, fan-out dials
  unreachable endpoints holding project_lock → same BUSY storm.
- On THIS machine (LAN active) a sibling class was observed live
  2026-07-25 13:10: pure history-join merge (`writes_done=0`,
  conflicts=0) — HEAD advance over byte-identical content (leg 3/5
  territory).
- 2026-07-09 prediction on record
  (azt_persistence_server_sync.md ~line 260): mtime bumps "will
  produce the same false dialog whenever LAN delivery is active."

## Research

- Discriminators still wanted from the affected machine:
  1. Exact popup wording ("Team changes detected/available" = poll
     latch vs "Merged your changes with updates from your team" =
     save path).
  2. Daemon-log grep (run on that machine, one line):
     `grep -h -E "submit_file|MERGED_WITH_LOCAL|legacy mode|drain push|lan-push|lan-merge" ~/.local/share/azt/daemon-*_log.txt | tail -n 200`
     Expected if hypothesis holds: `codes=['BUSY']` runs and/or
     `MERGED_WITH_LOCAL` with base != HEAD where both SHAs are that
     machine's own commits.
