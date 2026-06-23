# Publish errors — diagnostic trail & idempotency

Companion to `error_codes_audit.md`, focused on the Publish flow
(`_h_init_project` → `_init_repo` → `_init_repo_locked` →
`_ensure_remote_repo` → `porcelain.push`).

Established in 0.50.52 in response to a field repro where a user
clicked Publish, the local-side mutations all ran (rename
`master` → `main`, set `remote.origin.url`), the `publish-fanout`
worker even fired a share-offer to a paired peer — but the project
never appeared on github.com. Every subsequent `[sync-trace] fetch`
returned dulwich's `NotGitRepository()` (GitHub serving the 404 page
rather than a git response), and the daemon log had no trace of what
happened inside `_ensure_remote_repo`.

## Two principles

1. **Every path through Publish emits at least one `[publish]` line.**
   A tester sharing the daemon-log file should be able to reconstruct
   the flow without rebuilding, attaching adb, or running `run-as`.

2. **Publish is safe to re-click.** If something fails, the local
   state is rolled back to "publish not attempted" so the picker's
   `_refresh_publish_row` gate shows the Publish button again. A
   successful Publish makes a re-click a no-op (no spurious commits,
   no counter bumps, no double-push).

## Diagnostic coverage matrix

Every branch in the publish flow:

| Branch | `[publish]` log line |
|---|---|
| Lock-timeout BUSY | `init_repo BUSY: project_lock timeout on <dir> — another writer (sync drain, lan merge) holds the lock; peer will see BUSY result` |
| Every successful entry | `init_repo begin dir=<dir> remote=<url> branch=<branch> username=<user>` |
| CONTRIBUTOR_UNSET pre-check | `init_project refused: CONTRIBUTOR_UNSET (working_dir=… remote=…)` |
| Missing working_dir / remote_url | `init_project bad request: working_dir=… remote=…` |
| AUTH_REQUIRED pre-check (no stored token) | `init_project AUTH_REQUIRED: no stored token for remote=<url> (git_user=<user>)` |
| `_init_repo` raised at call-site | `init_project raised: <ExcType>: <msg>` |
| Owner-mismatch skip (peer-adopted URL) | `skip remote-create: owner mismatch owner=… username=… url=…` |
| Unknown host (path parses) | `skip remote-create: unknown host host=<host> url=<url>` |
| Unknown host (flat path) | `skip remote-create: unknown host, flat path host=<host> url=<url>` |
| Cannot parse owner/repo on known host | `remote-create FAILED: cannot parse owner/repo from <url>` |
| Every github/gitlab POST attempt | `POST <api_url> owner=<owner> repo=<repo>` |
| Create success | `remote-create OK: created owner/repo` |
| Already exists (422/400) | `remote-create: owner/repo already exists (422/400)` |
| HTTPError (any other code) | `remote-create FAILED owner/repo: <code> <body>` |
| URLError / OSError on POST | `remote-create FAILED owner/repo: <urlerror>` |
| Rollback after create-fail | `stripped .git/config [remote "origin"] from <dir>` + `init_repo aborting before push: codes=[…]; stripped .git/config origin for retry` |
| Push fail | `push to <url> failed: <exc>` |
| Every exit | `init_repo done: codes=[…]` |

Branches with no dedicated line — INITIALIZED / ALREADY_INITIALIZED,
REMOTE_UPDATED / REMOTE_UNCHANGED / REMOTE_SET, NOTHING_TO_COMMIT /
COMMITTED — are all visible in the closing `codes=[…]` line, so the
user's path through the flow is reconstructible from just the entry +
exit lines.

The post-create collaborator-add step (`add_collaborator`) keeps its
existing `[collab] add collaborator warning: <ex>` log, which covers
the "token can add repos but not collaborators" subcase.

## Credentials failure matrix

Three layers of "github credentials problem," each covered:

| Mode | Logged as |
|---|---|
| No stored token | `init_project AUTH_REQUIRED: no stored token for remote=…` |
| Token invalid / expired | `remote-create FAILED owner/repo: 401 <body>` |
| Token lacks repo-create scope | `remote-create FAILED owner/repo: 403 <body>` |
| Token OK, target not found | `remote-create FAILED owner/repo: 404 <body>` |
| Token revoked / GitHub App uninstalled | depends on github's response, but always lands in the HTTPError branch |
| Token OK, network glitch on POST | `remote-create FAILED owner/repo: <urlerror>` |
| Owner mismatch (peer-adopted URL) | `skip remote-create: owner mismatch …` |

## Idempotency

Two design considerations that make Publish safe to re-click:

### 1. No-op re-click on a successful prior publish

`_init_repo_locked` mirrors `_commit_step_locked`'s `has_staged`
guard: inspect `porcelain.status(repo)` first, only call
`porcelain.commit` when something's actually staged, otherwise add
`S.NOTHING_TO_COMMIT`. Without this guard, a re-click on a
quiescent project (everything already committed via the normal
commit flow) would raise "nothing to commit" inside
`porcelain.commit` → `_surface_commit_failure` → `commit_failure_count
+= 1` → eventually surfaces `COMMIT_REPEATEDLY_FAILED` as a
data-loss-class toast for a non-failure. Fixed in 0.50.52.

The rest of `_init_repo_locked` was already idempotent at the
git-config level:
- `porcelain.init` runs only when `_get_repo` returns None
  (otherwise `ALREADY_INITIALIZED`)
- `.gitignore` only written when absent
- `config.set((b'remote', b'origin'), b'url', …)` compares before
  writing (`REMOTE_UNCHANGED` if same)
- HEAD symref compares before setting
- `_ensure_remote_repo` treats 422 / 400 "already exists" as success
- `porcelain.push` is naturally idempotent (already-up-to-date)

### 2. Rollback on `_ensure_remote_repo` failure

When `_ensure_remote_repo` returns `ok=False` (the genuine
`REMOTE_CREATE_FAILED` path), `_strip_origin_section(repo,
project_dir)` removes the `[remote "origin"]` block from
`.git/config`. Mirrored in the registry by clearing
`projects.set_remote_url(langcode, '')` in `_h_init_project` when
the result carries `REMOTE_CREATE_FAILED`. Both sides need to be
cleared because the picker's `_refresh_publish_row` prefers
`project_status.remote_url` (live `.git/config`) but falls back to
`Project.remote_url` (registry).

Rollback fires only on hard create-failure. Other failure modes
keep the URL intentionally:

| Failure | Strip URL? | Why |
|---|---|---|
| `REMOTE_CREATE_FAILED` (genuine) | yes | No remote exists; user needs Publish button back for retry |
| `REMOTE_OWNER_MISMATCH_SKIP_CREATE` | no | Betting on collaborator access on existing repo; push will reveal 200 or 403 |
| Push failure (remote exists) | no | Scheduler drain will retry the push naturally |
| Push success | no | All done |

After both fixes, a re-click on a previously-failed publish:
- Working tree is unchanged → no spurious commit
- `.git/config` was stripped → `[remote "origin"]` re-added cleanly
- `_ensure_remote_repo` retries the github-API call (422 if the repo
  appeared between attempts)
- `porcelain.push` runs naturally

## Retroactive auto-fire (0.50.53)

Users upgrading from `<0.50.52` daemons may have stuck projects
where `.git/config` has an origin URL but the registry's
`Project.remote_url` is empty — the fingerprint of a publish
attempt under the old silent-failure path. Because the install of
a new server APK doesn't kill the running daemon (
[[project_client_server_version_drift]] /
[[feedback_restart_must_work_against_old_daemon]]), there's a
window where post-install clicks still hit the legacy path and
leave the mismatch behind.

`repo.reconcile_publish_state_on_startup()` runs on every daemon
startup, walks `projects.list_all()`, and for each project with
the mismatch fingerprint **auto-fires the publish the user
already committed to** — reading the URL from `.git/config`,
looking up credentials, calling `init_repo` with
`rollback_origin_on_create_fail=False` (so failure leaves state
untouched), and on `PUSHED` writing the registry side-effects +
firing the publish-fanout. On any other outcome (offline boot,
outage, missing creds, BUSY, OWNER_MISMATCH + push fail), the
working tree is left exactly as the user last saw it, and the
next daemon startup retries silently.

Log lines:

- `[publish-reconcile] auto-retry SUCCEEDED for N project(s):
  [(langcode, url, codes), …]`
- `[publish-reconcile] auto-retry deferred for N project(s)
  (state unchanged, next boot will retry):
  [(langcode, reason | codes), …]`

Why auto-fire instead of stripping `.git/config` to expose the
Publish button:

- Offline / outage / missing-creds boots are normal occurrences
  on field devices. Stripping in those cases would expose a
  phantom Publish button on a project the user already chose to
  publish — wrong UX.
- The user already pressed Publish in the past, so re-firing is
  re-consent to the same intent. No new judgment call needed.
- On success, the registry catches up and the mismatch is gone
  for good. On failure, the next daemon startup retries
  automatically — no user action needed.

Safety: the mismatch fingerprint is unreachable from any
post-0.50.52 code path (the 0.50.52 rollback keeps both sides
synchronized: both-set on `PUSHED`, both-empty on
`REMOTE_CREATE_FAILED`), so the reconciliation never runs against
a healthy project. A transient github outage during a *manual*
Publish produces `REMOTE_CREATE_FAILED` and the picker-initiated
rollback clears both sides — leaving both-empty, not the
half-state the reconciliation targets.

## Publish-fanout gate (0.50.53)

`_h_init_project`'s `_fanout_worker` (which sends `share_offer`
over LAN to paired peers, carrying the project's `remote_url`)
is now gated on `'PUSHED' in codes` (or `COMMITTED_AND_PUSHED`).
Pre-0.50.53 it fired whenever `published_langcode and remote_url`
were non-empty — both populated the moment the RPC arrived, so a
publish that failed at `_ensure_remote_repo` or `push` would
still tell peers about the URL. Peers would then accept and adopt
a URL pointing at a non-existent or empty github repo,
propagating the stuck state across the LAN cohort.

Open follow-up: the scheduler's WAN-drain doesn't fire
`send_share_offer` after a successful retry-push, so a publish
that fails on first attempt but succeeds via later drain leaves
peers ignorant of the URL. The 0.50.x sync-rebuild design splits
LAN fan-out (per-commit) from WAN drain (WAN-only); reconnecting
them for the post-drain share-offer case needs separate work.

## Lessons from the 0.50.52–0.50.56 journey

Five versions to get publish right end-to-end. The diagnostic
work each one needed surfaced patterns worth carrying forward:

### Always emit a summary line — even on the no-op path

Multiple functions in this flow used to log only on
side-effecting paths (action taken, exception raised). When the
function legitimately did nothing — no mismatches found, no
collaborator to add, no commit needed — the daemon log was
silent, which in the diagnostic phase looked identical to "the
function never ran." That ambiguity hid the missing Android
callsite for two versions of 0.50.x.

Rule: every function on the publish flow emits at least one
log line per invocation. `[publish-reconcile]` now emits
`walked=N mismatch=M succeeded=S deferred=D` unconditionally;
`[collab] add_collaborator` now logs on success too. Future
additions follow suit.

### Dual-entry-path startup hooks

The daemon has **two** startup paths that both need any new
boot-time hook:

- **Desktop:** `azt_collabd.server.serve()` — runs when the
  loopback transport launches the daemon via `python -m azt_collabd`
- **Android:** `server_apk/service.py:main()` — runs when the
  `:provider` service starts under p4a

The reconciliation function was wired into `serve()` but not
into `service.py`, so it never ran on Android. The `_boot_trace`
calls live in `service.py` — that's the file to grep for
"what fires on Android startup" — but the actual hooks
historically went into the desktop file by default.

Rule: any startup hook (scheduler.reconcile_on_startup, the
new publish-reconcile, future LAN-listener-apply-toggle, …)
must be invoked in **both** files. Cross-check by searching for
`scheduler.reconcile_on_startup` — it lives in both today, and
any new sibling should match that pattern.

### GitHub App auth ≠ PAT auth on the `username` field

`store.get_sync_credentials(remote_url)` returns
`(username, token)`. For PAT auth, `username` is the user's
GitHub login (`kent-rasmussen`). For **GitHub App auth**,
`username` is the literal string `x-access-token` — github's
documented placeholder for HTTP basic-auth with installation
tokens. The token does the real authentication; the username
is meaningless.

Any code that compares `username` against a github login (e.g.
the owner-mismatch heuristic in `_ensure_remote_repo`) will
false-positive on every App-authenticated publish, because
`'x-access-token'` never matches anything. POST `/user/repos`
with an installation token is already namespace-scoped to the
installation account, so heuristics that try to second-guess
the namespace from `username` are also pointless for App auth.

Rule: when checking authentication identity in a code path
that needs to reason about *who* is publishing, either query
`/installation/repositories` (App) or `/user` (PAT) and use
the API-returned login. Don't compare against the credentials
store's `username` field directly.

## Related follow-ups

- **Sync indicator masking** — *shipped in 0.52.3.* Resolved by
  walking from HEAD instead of needing a reachability gate. When
  the origin URL is set but no tracking ref exists, both subcases
  (never-fetched-yet and every-fetch-fails) collapse to the same
  honest answer: "report the local commit count as unshared."
  Never-fetched-yet self-corrects on the first successful fetch
  (the tracking ref appears and subsequent calls fall through to
  the walk-excluding-tracking branch); never-can-fetch keeps
  reporting the real backlog until the user fixes the access
  problem (wrong-account GitHub App, revoked credentials, etc.).
  The original deferral rationale — "adds an Internet-reachability
  gate on every status poll" — turned out to be unnecessary: a
  local walk is enough, no network probe required.
