# Merging a stray ref into a project (`sha_to_merge`)

A recovery/maintenance gesture for the case where a commit or branch
you want lives **in a project's repo** but not on its current branch —
e.g. a fork or a peer lineage you fetched in as `refs/remotes/<x>/…`,
or a phone's pre-disaster commits — and you want it folded into the
project using collabd's normal convergence merge (LIFT-aware union by
`guid` + audio last-wins), not a hand-written merge.

It is deliberately **behind-the-scenes**: no RPC, no button, no
browser. You declare the merge in `projects.json`; collabd performs it
on its next launch and logs the outcome.

## How to use


0. **Isolate the computer** (no phones / LAN / internet)

1. **Fetch the ref into the project's repo** if it isn't already there
   (a local remote + `git fetch` is fine — e.g. `refs/remotes/xtest/…`).
   The commit's objects must be present in the repo's object store.

2. **Add one key** to that project's entry in
   `$AZT_HOME/projects.json` (`~/.local/share/azt/projects.json` on
   Linux):
   
   ```json
   "nml": {
     "working_dir": "/home/you/…/nml",
     "remote_url": "git@github.com:aztobt2-ui/nml.git",
     "sha_to_merge": "2797303021782cf1e304d193793960f841e3c13b"
   }
   ```
   If the project isn't in projects.json, you'll need to register it 
   with azt-collab first
   
   `sha_to_merge` is a **full 40-char commit SHA** or a resolvable ref
   name (`xtest/phone-lineage-premerge`, `some-branch`, a tag).

3. **Relaunch the daemon** (or the app that hosts it). On startup
   collabd:
   - merges that ref into the project's current branch with
     `_merge_diverged` — the same engine a normal sync uses;
   - creates **one merge commit** (parents = your branch tip + the ref);
     it does **not** push;
   - marks the project `pending_push`, so the scheduler's drain pushes
     it once the daemon is online (and `work_offline` is off);
   - **clears `sha_to_merge`** (one-shot, on any outcome).

4. **Read the daemon log.** Grep for `[merge-ref]`:

   ```
   [merge-ref] 1 pending merge(s) from projects.json: ['nml']
   [merge-ref] 'nml' merging 279730302178 into main (HEAD abc123…);
               undo with: git -C /home/you/…/nml reset --hard <HEAD>
   [merge-ref] 'nml' merged -> def456789012 (0 conflicts);
               pending_push set (pushes when online)
   [merge-ref] 'nml' outcome: ['MERGED_REF']
   ```

## Verifying / isolating before it goes anywhere

The merge is **commit-only**, so you control when (or whether) it
reaches the remote by controlling connectivity:

1. **Disconnect** the machine from phones / LAN / internet.
2. Add the key, relaunch, watch the `[merge-ref]` log lines.
3. Inspect the merged working tree (in WeSay / your editor).
4. If it's wrong, `git -C <working_dir> reset --hard <pre-merge HEAD>`
   (the SHA is printed in the log line above). Nothing was pushed.
5. When you're happy, **reconnect** — the drain pushes the merge.

## One-shot semantics (why the key is always cleared)

The key is removed after the attempt **regardless of success**:

- A **successful** merge must not repeat — `_merge_diverged` writes a
  fresh commit every call, so re-running each launch would pile up
  empty merge commits.
- A **failed** merge (unrelated histories → `MERGE_UNRELATED_HISTORIES`,
  or an unresolvable / absent SHA → `SERVER_ERROR`) must not retry on
  every launch. The log says why; fix it and **re-add the key** to try
  again.

## Notes

- Requires a **common ancestor** between your branch and the ref. If
  they're truly unrelated (two different projects that happen to share
  a language code), collabd refuses with `MERGE_UNRELATED_HISTORIES`
  and changes nothing — that guard is intentional (invariant #14 /
  0.54.19). "Related" includes a fork of the same repo.
- Pushing uses collabd's GitHub credentials over https (ssh-shaped
  origins are wan-normalized at push time; your personal ssh key is
  not used by the daemon). If the daemon has no push credentials for
  the remote, the merge just stays `pending_push` and logs it —
  nothing is lost; add credentials or push once by hand.
- Engine: `azt_collabd.repo.merge_ref_into_project` /
  `consume_pending_merges`; registry helpers
  `projects.pending_merges` / `projects.clear_sha_to_merge`. Consumed
  from both daemon startup paths (`server.serve`,
  `server_apk/service.py:main`).
