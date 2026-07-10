# Error situations & resulting status codes

Audit of error situations the daemon and client surface, paired with the
status code emitted and (where applicable) the translated user-visible
string.

- **Daemon-side definitions:** `azt_collabd/status.py` (rationale lives
  in the inline comments there).
- **Client mirror:** `azt_collab_client/status.py` (must stay in sync —
  hard rule #3).
- **Translations:** `azt_collab_client/translate.py` (`_HANDLERS` table).
- **Unmapped code fallback:** `translate_status` returns
  `[CODE] {params}` — see `translate.py:305`.

Logic must route on `Result.has(S.CODE)` / `Result.has_any(...)`, never
substring-match on the translated string. Translated text exists for
display only.

## Repo / config errors (user actionable)

| Situation | Code | String |
|---|---|---|
| Working dir isn't a git repo | `NOT_A_REPO` | "Not a git repository. Publish the project first." |
| No remote configured | `NO_REMOTE` | "No remote configured. Publish the project first." |
| Sync called with Work-Offline on (user-initiated path only) | `WORK_OFFLINE_ENABLED` | "Work-offline mode is on. Turn it off in sync settings to push." |
| Contributor name unset (commit refused) | `CONTRIBUTOR_UNSET` | "Please set your name in the sync settings before publishing or syncing." |
| Concurrent sync attempt | `BUSY` | "Another sync is in progress. Try again in a moment." |
| Merge conflicts left in tree | `CONFLICTS` | "Merge conflicts in {paths}" / "Merge conflicts; review the entries flagged azt-lift-conflict." |

## Commit / push failures (data-at-risk class)

| Situation | Code | String |
|---|---|---|
| dulwich commit threw | `COMMIT_FAILED` | "Commit: {error}" |
| 2+ successive COMMIT_FAILED — data accumulating off-history | `COMMIT_REPEATEDLY_FAILED` | "Saving to git has failed {count} times in a row ({error}). Your recordings are still on the device but aren't being backed up. …" |
| Peer wrote outside audio/images/.lift filter — never staged | `DATA_LOSS_RISK` | "Data-loss risk: {count} file(s) written to your project aren't being backed up. …" |
| Push failed (generic) | `PUSH_FAILED` | "Push failed: {error}" |
| Pull failed | `PULL_FAILED` | "Pull failed: {error}" |
| Clone failed | `CLONE_FAILED` | "Clone failed: {error}" |
| Clone hit private repo without creds | `CLONE_AUTH_REQUIRED` | "Clone failed — repository not found. This may be a private repository.\n\nAre you authenticated to {host}?" |
| Branch state wrong | `BRANCH_ERROR` | "Branch error: {error}" |
| GitHub repo creation failed | `REMOTE_CREATE_FAILED` | "Create repo failed: {error}" |

## Network / transient (auto-sync silences these)

| Situation | Code | String |
|---|---|---|
| Both system DNS *and* DoH failed for sync host | `DNS_RESOLUTION_FAILED` | "Network reachable, but the sync host could not be resolved. Sync will retry automatically when this clears. …" |
| Push budget (default 300s) hit before drain | `SYNC_GIVING_UP_TRANSIENT` | "Sync gave up after {budget_s}s on a flaky network. {commits_pending} commit(s) still pending — they will go out on the next sync attempt." |
| `MemAvailable` < `sync.min_free_mem_mb` (default 200 MB) at merge time | `INSUFFICIENT_MEMORY_FOR_MERGE` | "Not enough memory to merge right now ({mem_available_mb} MB available, {min_required_mb} MB needed). Close other apps and the next sync will retry." |
| Daemon respawned with PENDING/RUNNING job → flipped DONE | `JOB_INTERRUPTED` | "Sync was interrupted; please retry." |
| Daemon restarted mid-session (informational stdout signal) | `SERVICE_RESTARTED` | (no translation handler — stdout marker only) |
| Daemon accepted `/admin/restart` | `RESTARTING` | "Sync service is restarting…" |

## Push-architecture failures (0.44.x topic-branch path)

| Situation | Code | String |
|---|---|---|
| Topic-branch ref `azt-pending-<lang>-<device>` clashes with another device's | `TOPIC_BRANCH_CONFLICT` | "Another device is using the same device name and our staging branch ({topic_branch}) collided with theirs (server tip {server_tip}). Change this device's name…" |
| `chunk_n=1` exceeds 3 MB budget or 408s twice | `COMMIT_PACK_EXCEEDS_NETWORK_BUDGET` | "Could not push to GitHub: the server kept rejecting our push attempts (single commit {commit_sha}, {raw_bytes:,} bytes). …" |
| Commit contains file > `large_audio_byte_threshold` (default 500 KB) | `LARGE_AUDIO_FILE_DETECTED` | "Unusually large file recorded: {path} ({bytes:,} bytes). The recorder is for word-list elicitation — please check whether this was a recording mistake." |
| Secondary "extra remote" rejected (per-URL, after primary) | `EXTRA_REMOTE_PUSH_FAILED` | "Additional remote {url} rejected the push: {error}" |

## 403 / auth diagnosis (GitHub)

| Situation | Code | String |
|---|---|---|
| Generic "no token" | `AUTH_REQUIRED` | "Not connected to GitHub. Go to Setup > Connect to GitHub." |
| GitHub App not installed on org/user | `APP_NOT_INSTALLED` | "App not installed. Visit {url} and select \"All repositories\"." |
| Installation suspended | `APP_SUSPENDED` | "GitHub App installation is suspended at {url}. Open it, scroll to the bottom, and tap 'Unsuspend'." |
| App installed but not granted on this repo | `REPO_NOT_AUTHORIZED` | "App not authorized for {owner_repo}. Add it at {url}" |
| Plain 403 fallback | `ACCESS_DENIED` | "Access denied (403). Check app permissions at {url}" |
| Refresh token broken; access still valid until `expires_at` | `AUTH_REFRESH_STALE` | "GitHub session needs re-authentication — current access expires {deadline}. Open GitHub Connect and tap Re-authenticate." |

## Device-flow (login attempt)

| Situation | Code | String |
|---|---|---|
| User code expired before approval | `AUTH_EXPIRED` | "Authorization expired. Please try again." |
| User clicked Cancel on GitHub | `AUTH_DENIED` | "Authorization denied by user." |
| Polling exceeded cap | `AUTH_TIMEOUT` | "Authorization timed out." |

## Collaborator-grant flow

| Situation | Code | String |
|---|---|---|
| Invite sent (201) | `COLLABORATOR_INVITED` | "Invited {username} as a collaborator on {owner_repo}. They must accept the invitation on GitHub …" |
| 204/422 — already collaborator or pending | `COLLABORATOR_ALREADY` | "{username} already has access to {owner_repo} (or a pending invitation)." |
| Invite call failed | `COLLABORATOR_INVITE_FAILED` | "Could not invite {username} to {owner_repo}: {error}" |
| Empty / malformed username field | `INVALID_USERNAME` | "Enter a GitHub username." |
| Project's remote isn't on github.com | `NOT_GITHUB_REMOTE` | "This project is not hosted on GitHub ({remote_url}). Collaborator invites are only supported …" |

## LAN sync errors

| Situation | Code | String |
|---|---|---|
| Tried every endpoint, none responded | `LAN_PEER_UNREACHABLE` | "Paired device is not reachable on this network." |
| Cert fingerprint differs from `peers.json` (reinstall or MITM) | `LAN_FP_MISMATCH` | "A paired device presented an unexpected security fingerprint. It may have been reinstalled; re-pair from sync settings." |
| LAN op while toggle is off | `LAN_TOGGLE_OFF` | "Local-network sharing is off. Turn it on in sync settings." |
| LAN clone TCP/TLS up, packfile stalled past `_LAN_CLONE_TIMEOUT_S` | `LAN_CLONE_TIMEOUT` | "Copying the project timed out. Is the other phone still nearby and on the same Wi-Fi? …" |
| Receiver already has different project with same langcode | `LAN_PROJECT_COLLISION_UNRELATED` | "A different project named {langcode} already exists. Rename or remove it first." |
| Both peers have a remote_url, they differ | `LAN_REMOTE_CONFLICT` | "Project {langcode}: {device_name} uses {incoming_url}, you use {existing_url}." |
| Receiver declined share offer | `LAN_SHARE_DECLINED` | "Share for {langcode} was declined." |
| Receiver declined pair request | `LAN_PAIR_REQUEST_DECLINED` | "{device_name} declined the pair request." |
| 5-min cap on outbound pair request elapsed | `LAN_PAIR_REQUEST_TIMEOUT` | "Pair request to {device_name} timed out." |

## Client-synthesized (transport-failure branches)

These never come from the daemon — `rpc.call` / wrappers produce them
when the daemon's unreachable or returns garbage. See "Public API
surface" in `azt_collab_client/CLAUDE.md`.

| Situation | Code | String |
|---|---|---|
| Daemon unreachable (loopback or CP) | `SERVER_UNAVAILABLE` | "Sync service unavailable: {error}" |
| Daemon responded with non-2xx / decode failure | `SERVER_ERROR` | "Sync service error: {error}" |

## RESOLVED 0.54.2: French translation coverage gap (31 msgids, found 2026-07-10)

All 27 unique msgids (31 callsites) got real French entries in 0.54.2
(2026-07-10); `test_python_translation_coverage` green again. French
pending a native-eye review pass (notably the two one-word section
headers «Appairés» / «Non appairés»). Original finding kept below for
context.

`tests/test_translation_coverage.py::test_python_translation_coverage`
fails: 31 Python `_(...)`/`_tr(...)` msgids are missing from
`azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`.
Clusters (see the test output for the full list):

- `translate.py` LAN share/offer strings ('Offer sent.', 'Already in
  sync.', 'Sent — waiting for the other phone to accept.', 'That phone
  is not paired with this one anymore.', 'Could not reach the other
  phone (status {post_status}).', …)
- GitHub collaborator-access strings ('No access to {owner_repo}. Ask
  the owner to add you as a collaborator…', 'Accepted the repository
  invitation — syncing now.')
- `ui/popups.py` repository-access popup ('Open GitHub', 'Repository
  access needed', 'Ask the repository owner to add you as a
  collaborator…', 'No access to {repo} with your GitHub account.',
  'this repository')
- `ui/share.py` link fallbacks ('Open this link on the device: {url}',
  'Could not open the browser. Link: {url}')
- `ui/lan_popups.py` ('This phone', + 10 more)

Reminder: never half-ship `msgstr ""` — an empty msgstr renders as an
EMPTY string, not the msgid fallback (buttons go invisible). Add real
French or don't add the entry.

## Adding a new error code

Per `azt_collab_client/CLAUDE.md` § "When adding a new client API call":

1. Add the constant to `azt_collabd/status.py` **and** mirror it into
   `azt_collab_client/status.py`.
2. Add a translation row to `_HANDLERS` in
   `azt_collab_client/translate.py`. Without a row, peers fall back to
   the unmapped `[CODE] {params}` rendering — fine for diagnostics, not
   fine in front of users.
3. Add a row to this file under the matching category, so the
   "what does this mean to the user" answer doesn't drift from the
   daemon-side comment.
