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
- **Waiting on:** Nothing (grep output from the affected machine will
  confirm the trigger; the fix legs are already actionable)

## Plans

Fix legs, in likely order of payoff:

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
