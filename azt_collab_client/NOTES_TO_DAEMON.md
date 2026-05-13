# Notes to the daemon

Outstanding items peers have noticed and want the `azt_collabd` /
server-APK side to fix. Filed here (inside `azt_collab_client/`)
rather than in the per-peer CHANGELOG so:

- the symlink propagates them into every sister app's tree
- the daemon team sees them in one canonical place
- the note moves with the package if the canonical home ever
  changes

When you act on an item, delete it from this file (the daemon
CHANGELOG is the historical record; this file is the live queue).

---

## Daemon is now the *sole* authoritative source — no peer-side fallbacks

**Filed:** 2026-05-12, by azt_recorder peer (1.41.3). **Standing
notice — do not delete; this is an architectural invariant the
daemon must honor on every release.**

The recorder peer used to keep "just-in-case" local mirrors of
daemon-owned state (`peer_pref('vernlang')`,
`peer_pref('collab_langcode')`, a defunct
`App.list_projects` that scanned the peer's own sandbox). Those
mirrors are gone as of 1.41.3 per the no-daemon-owned-caches
rule (peer-side memory: `feedback_no_daemon_owned_caches.md`).

That means: **if the daemon returns a wrong, stale, or empty
value for any of the fields below, the peer has no fallback and
the user-visible behavior breaks.** Please treat the
correctness of these as load-bearing.

| Field | Endpoint(s) | What breaks on wrong/empty |
|---|---|---|
| Project langcode (== LIFT vernlang) | `last_project`, `open_project`, `register_project`, `derive_langcode`, `project_status` | LIFT writes use the wrong `lang=` attribute; `progress_text` reads the wrong field; audio filenames are mis-tagged. |
| Recent project (`last_project`) | `GET/POST /v1/recent/last_project` | Auto-resume on startup either skips a valid project or resumes a wrong one. The peer has no local "last opened" mirror anymore. |
| Contributor name | `get_contributor` / `set_contributor` | Git commits attributed to "Recorder" instead of the real author. |
| UI language | `azt_collab_client.i18n.current_language()` / `set_language()` | UI lands on the wrong locale on every launch — no peer-side cache. |
| Credentials (GitHub/GitLab/host) | `/v1/credentials/*` | Publish/sync silently fails; the peer cannot fall back to a local token store. |
| Project registry (working_dir, lift_path, remote_url) | `list_projects`, `open_project`, `register_project` | Picker can't find the project; publish has no working_dir to push from. |
| Repo slug (per-project override) | `Project.repo_slug` via `open_project`/`list_projects`/`project_status`; setter `POST /v1/projects/<lang>/repo_slug` | Override silently degrades to using `langcode` as the repo name (the typical case anyway). Shipped 0.39.0. Don't mirror the slug into peer prefs. |
| CAWL image_repo (per-project) | `Project.cawl_image_repo` via `open_project`/`list_projects`/`project_status`; setter `POST /v1/projects/<lang>/cawl_image_repo` | Per-project image-set override silently degrades to the daemon-global default. Shipped 0.38.0; peer migration documented in `azt_collab_client/CLAUDE.md` "CAWL image access" section. Don't mirror the slug into peer prefs. |

### Specific obligations

1. **No silent empty.** If a getter can't answer (server starting,
   transient I/O failure), return a clear error, not an empty
   string — the peer treats empty as "user hasn't set it" and
   degrades accordingly. Today this is mostly correct; flagging
   it because we're now relying on it.
2. **Setter durability.** Every setter that writes to
   `$AZT_HOME/config.json` (or its Android-CP equivalent) must
   land on disk before returning OK. Crash-during-write that
   loses the value will surface as user-visible data loss with
   no peer-side copy to recover from.
3. **Project-langcode immutability without a rename RPC.** Peers
   cache the langcode in-memory as `_current_langcode` for the
   life of the load. If the daemon decides to change a project's
   langcode out from under a loaded peer (e.g. during a merge),
   the peer's in-memory copy goes stale and future writes go to
   the old tag. If renames are supported, surface them through
   `rename_project` and have the daemon notify open peers (or at
   minimum make the next `project_status` reflect the new value
   so the peer can refresh on its periodic poll).
4. **Cross-peer convergence.** Setters from one peer must be
   visible to every other peer's getter within "next RPC" time.
   The Android ContentProvider already gives us this; flagging
   so a future daemon refactor doesn't accidentally introduce a
   per-process cache that breaks it.

If you're adding a new field that the peer needs to know, the
default placement is daemon-side, accessed by RPC each time —
do not invite the peer to cache it.


