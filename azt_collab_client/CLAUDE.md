# CLAUDE.md — azt_collab_client

> **Scope of this file.** This is the philosophy / rationale /
> architecture reference for `azt_collab_client` — why the
> design is the way it is, what architectural invariants the
> client owes the daemon and vice versa, deeper API surface for
> someone working *inside* the package. **The peer conformity
> contract — what peers must do to be conformant — lives in
> `CLIENT_INTEGRATION.md` next to this file.** If you're a
> peer maintainer trying to figure out "what do I have to call,
> what do I have to honor, what's the migration checklist",
> read that file first. Read this one if you want to understand
> *why*. Action-oriented content does not belong here; if you
> find yourself writing a "do X / don't do Y" rule, put it in
> `CLIENT_INTEGRATION.md` and link forward instead of
> duplicating.

Guidance for Claude Code (claude.ai/code) when working with the
`azt_collab_client` package, including from sister apps that consume
it as a symlink (`../azt-collab/azt_collab_client` → `./azt_collab_client`).

This file is intentionally **self-contained**: when this directory is
symlinked into a sister app, you should not need to read the canonical
`azt-collab/CLAUDE.md` to understand the client or its connection to
the daemon.

## What this package is

A thin, **platform-agnostic**, **decode-only** client library for the
`azt_collabd` daemon. Suite apps (recorder, future viewer, ...) call
into here; this package owns:

- the public API surface (`azt_collab_client.__init__`)
- the transport facade (`rpc.call` → `transports.pick_transport`)
- decode-only mirrors of `Status` / `Result` / `Project` / `ProjectStatus`
- a translator hook (`translate.set_translator`) plus the client-owned
  gettext catalog (`azt_collab_client.i18n` / `locales/`)
- a shared Kivy picker UI (`azt_collab_client.ui`)

## Hard rules

1. **No dulwich, no git operations.** The daemon is the only thing
   that touches a repo. Clients write files into the working tree (or
   stream through the Android ContentProvider) and ask the daemon to
   commit / sync. If you find yourself reaching for `dulwich` or
   `subprocess(['git', ...])` in this package, stop — add (or use) a
   server endpoint instead.

2. **No reading project state from the local filesystem either.**
   This is a corollary of (1) but worth its own line because the
   failure mode is silent on desktop and only surfaces on Android.
   Anything that opens the project's working_dir to inspect it —
   `dulwich.Repo(working_dir).get_config()` to check
   `remote.origin.url`, `os.path.exists(os.path.join(working_dir,
   '.git'))`, walking the audio dir for backup detection, etc. —
   must instead go through `project_status(langcode)` (or a new
   server endpoint if the field you need isn't there). Reason: on
   Android the daemon's working_dir lives in the standalone server
   APK's private filesDir; peer processes (recorder, viewer, ...)
   have no UID-level read on it, so any local-filesystem check
   raises or silently returns the empty/false answer regardless of
   the actual project state. On desktop both processes share
   $AZT_HOME and the local check happens to work, which makes this
   an easy bug to merge without noticing.

   The blast radius is wider than just the misleading warning. Any
   peer flow that *gates* on a local-filesystem state check (e.g.
   "only auto-sync if the project has a remote") will silently
   skip the gated work on Android — symptom: the recorder
   correctly publishes the repo and the daemon correctly receives
   commit RPCs, but no `[sync]` / `[sync-rpc]` lines ever appear in
   logcat because the recorder never asked.

   The recorder's `_project_has_remote()` (`main.py:3865`) is the
   canonical anti-pattern: it runs `dulwich.Repo(self.recorder.db.dir)`
   to answer "is this project backed up?", which fails on Android
   even after a successful publish and falsely shows the
   "data isn't being backed up" warning. The fix shape (use this
   pattern verbatim for any similar check):

   ```python
   def _project_has_remote(self):
       if not self.recorder:
           return False
       langcode = getattr(self, '_current_langcode', '')
       if not langcode:
           return False
       try:
           from azt_collab_client import project_status
           ps = project_status(langcode)
       except Exception:
           return False
       return bool(ps and (ps.remote_url or '').strip())
   ```

   The same shape works for `last_commit` / `last_sync` /
   `commits_ahead` queries — `project_status` carries them all and
   the daemon already touches the project as recent on every call,
   so peers don't have to manually mark recency.

3. **No `azt_collabd` import.** This package must keep working when
   the daemon is running in a separate process or a different APK.
   `paths.py`, `status.py`, and `projects.py` are duplicated on
   purpose so the client doesn't depend on the server package.

4. **No Kivy at import time at the package root.** `azt_collab_client.ui`
   imports Kivy; `azt_collab_client` itself does not. Sister apps may
   import the top-level module from non-Kivy contexts (CLI helpers,
   tests). Keep it that way — guard any Kivy imports inside `ui/` or
   inside functions that are only called from a Kivy host.

5. **Structured `Result`s drive logic; translated text is for humans.**
   Use `result.has(S.PUSHED)` / `result.has_any(S.AUTH_REQUIRED, ...)`
   — never substring-match on translated strings. `S` is
   `azt_collab_client.status` (re-exported as `S` from the package
   root). Translation lives in `translate.py` and runs through
   whatever callable was last passed to `set_translator(fn)`; the
   **default** is the client-owned catalog at
   `azt_collab_client.i18n._`, and `tr()` falls back to that catalog
   when a host translator returns the msgid unchanged. See the
   *Internationalization* section below.

6. **The client owns its own translatable strings.** Anything in
   `picker.py` / `popups.py` / `langpicker.py` / `translate.py` /
   the daemon's `ui/app.py` that's user-visible goes in
   `azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po`.
   Don't expect a host catalog (e.g. `aztrecorder.po`) to carry these;
   the recorder is one consumer of the client among several.

7. **Two `configure()` calls in the suite, both keyword-only and
   idempotent.** Host apps call:
   - `azt_collab_client.configure(app_id=...)` — currently a no-op,
     reserved for app-identity / provider-routing later. Safe to
     leave it as a no-op; don't add side effects without a reason.
   - `azt_collabd.configure(app_slug=..., client_id=..., collaborator=...)`
     — server-side GitHub App identity (lives in the daemon package).

## How the client reaches the daemon

`rpc.call(method, path, body=None, timeout=300)` is the single entry
point. It delegates to `transports.pick_transport()`, which is cached
after the first call and re-discovered on `ServerUnavailable`.

### Loopback (desktop, and Android peer apps if no server APK)

- Reads `$AZT_HOME/server.json` → `{port, token, pid, version}`.
  `$AZT_HOME` resolves via `azt_collab_client.paths.azt_home()`:
  honors `$AZT_HOME` env, else macOS
  `~/Library/Application Support/azt`, else
  `${XDG_DATA_HOME:-~/.local/share}/azt`.
- Every endpoint except `GET /v1/health` requires
  `Authorization: Bearer <token>`.
- **Auto-spawn**: on a missing/stale `server.json`, the loopback
  transport launches `python -m azt_collabd` detached (helpers in
  `_spawn.build_spawn_env` prepend `PYTHONPATH` so a sibling-symlinked
  `azt_collabd` is importable from the child). Disable with
  `AZT_CLIENT_AUTOSPAWN=0`.
- `_MAX_ATTEMPTS = 3` retries; a successful respawn after a failure
  prints a single `SERVICE_RESTARTED` line to stdout — this is the
  canonical signal a daemon restart happened mid-session.

### Android ContentProvider (when the standalone server APK is installed)

- Discovery probes the canonical authority `org.atoznback.aztcollab`.
  If the server APK isn't installed (or refuses `ping`), `discover()`
  returns `None` and `pick_transport()` raises `ServerUnavailable` —
  there is **no loopback fallback on Android** (no Python interpreter
  to spawn).
- Auth is Android signature-level `<permission
  name="org.atoznback.AZT_COLLAB_ACCESS"
  protectionLevel="signature">`. The expected keystore SHA-256 is in
  `android/SUITE_FINGERPRINT` of the canonical repo. Mismatched
  signing → install-time grant denied → calls fail with
  `ServerUnavailable`.

### Recovery semantics

`rpc.call` catches `ServerUnavailable`, calls `transports.reset()`
(which `close()`s the cached transport), and re-picks once. That's
how a freshly-installed server APK gets picked up after a session
that started with no daemon, and how a client recovers when the APK
hosting the ContentProvider is killed/uninstalled mid-session.

## Public API surface (what to call from a sister app)

All exposed by `azt_collab_client.__all__`. Patterns:

- **Health / version handshake.** Call `check_server_compat()` once
  at startup — returns `{ok: True, server_version}` or
  `{ok: False, error: 'server_too_old'|'server_unreachable', ...}`.
  Subsequent rpc calls do not re-check (compat doesn't drift
  mid-run). `MIN_SERVER_VERSION` is the floor the client supports.
- **Connectivity.** `is_online()` asks the server (so all peers
  share one connectivity oracle).
- **Settings UI.** `open_server_ui()` spawns the standalone Kivy
  settings UI on desktop (no-op-ish on Android until the server APK
  exposes a launcher Intent).
- **Credentials.** `get_credentials_status()`, `set_collab_host(host)`,
  `github_app_install_url()`, `github_app_client_id()`,
  `github_device_flow_start()` / `_status(job_id)`,
  `save_github_tokens(...)`, `mark_github_app_installed(...)`,
  `save_gitlab_credentials(...)`, `migrate_from_prefs(prefs_path)`.
  Tokens are server-owned — these wrappers never return raw tokens.
- **Projects.** `list_projects()` → `[Project]`,
  `open_project(langcode)`, `register_project(...)`,
  `derive_langcode(working_dir, lift_path='')`,
  `init_project(working_dir, remote_url, branch='main')` → `Result`,
  `create_project_from_template(vernlang, dest_dir, template_url='')`,
  `clone_project(remote_url, dest_dir, on_progress=None, ...)` (synchronous;
  use `clone_project_start` + `clone_project_status` for a
  Clock-driven progress loop),
  `project_status(langcode)` → `ProjectStatus | None`.
  Each `Project` carries a `lift_exists` boolean — the daemon's stat
  result against the project's LIFT path at API-response time. Peers
  resolving a `last_project()` / favourite langcode to a `Project`
  for auto-resume should check `lift_exists` before handing
  `lift_path` to `LiftHandle`; a False value means the file was
  deleted out-of-band (user wipe, external rm) and the peer should
  fall through to the picker rather than crashing on a not-found.
  The picker's host-side `list_projects()` filters missing entries
  out automatically.
- **Sync.** `sync_project(langcode)` blocks; returns `Result`.
  `request_sync(langcode)` is fire-and-forget (returns a
  `job_id`, debounced server-side). `poll_job(job_id)` returns
  `{state, result, ...}` where `state` is
  `'PENDING' | 'RUNNING' | 'DONE'`. Commit-author identity
  is daemon-resolved (0.40+): set once via `set_contributor`
  (typically through the daemon settings UI); peers don't pass
  it per-call. If unset, the result carries
  `S.CONTRIBUTOR_UNSET`.
- **Bookkeeping.** `record_project_sync_time(langcode, timestamp=None)`.

Wrappers must:

- **Always** translate transport failure into a typed return: a
  `Result` with `Status('SERVER_UNAVAILABLE'|'SERVER_ERROR', {...})`
  for ops that nominally return `Result`, an empty/None equivalent
  for ops that return data, never a raw `ServerUnavailable` to the
  caller (except where documented, e.g. `rpc.call` itself).
- **Never** raise on transport failure from a query-shaped wrapper.
  UI code should be able to call `list_projects()` while offline and
  get `[]`, not an exception.

## When adding a new client API call

1. Add the endpoint to `azt_collabd/server.py` (dispatch table),
   returning `{ok: True, ...}` or `{ok: False, error: ...}`.
2. Add a thin wrapper in `azt_collab_client/__init__.py` that calls
   `rpc.call()` and decodes into the right dataclass / `Result`,
   following the failure-translation rules above.
3. Re-export from `__all__` in `azt_collab_client/__init__.py`.
4. Add status codes (if any) to `azt_collabd/status.py` **and**
   `azt_collab_client/status.py` (mirror them; the comment at the
   top of the client copy documents this), and a translation in
   `azt_collab_client/translate.py`.

If you're working from a sister app and `azt_collabd/` isn't
accessible in your tree, the daemon-side change has to happen in the
canonical `azt-collab` repo first — don't try to fake the endpoint
from inside the client.

## Status codes & translation

- `azt_collab_client.status` is a **mirror**, not a re-export, of
  `azt_collabd/status.py`. Adding a code requires editing both.
- Codes are uppercase strings. Wire format is
  `{'code': 'PUSHED', 'params': {...}}` per status, and `Result.from_dict({'statuses': [...]})`
  decodes a list of them.
- `Result.has(code)`, `Result.has_any(*codes)`, `Result.codes()` are
  the supported predicates.
- `translate.tr(msg)` is a module-level wrapper that always delegates
  to the current `_tr`, useful for KV `#:import` so subsequent
  `set_translator` calls take effect.

### Peer contract: routing on sync results

This section codifies how peers MUST respond to `sync_project` /
`request_sync` result codes, so future peers (viewer, next sister
app) match the recorder's existing behaviour without
reverse-engineering it from the recorder source.

**Two contexts, two contracts.** The same `Result` reaches the
peer from two different triggers and they need different
responses:

1. **Auto-sync** — the peer fires `request_sync` itself, without
   a user gesture: project-select, project-load, background
   periodic, post-edit debounce. The user did NOT ask to sync.
2. **User-initiated sync** — the user tapped a sync icon /
   "Sync now" button / similar deliberate gesture. The user
   explicitly asked.

Auto-sync MUST be silent on configuration-class failures. The
user is in the middle of doing something else (opening a project,
finishing an edit); a sync popup / forced settings navigation /
modal error in that moment is a regression: it derails the flow
the user was in, sometimes visibly enough to look like project
selection itself "failed" or "fell back to the old project." Log
to stderr/logcat for diagnostics; don't surface to the user; let
whatever the user was doing complete.

User-initiated sync, by contrast, IS the gesture — the user
asked to sync and they want to know what happened. If the
project isn't publishable yet (`NOT_A_REPO`, `NO_REMOTE`) or auth
is broken (`AUTH_REQUIRED`, etc.), route them to the place
where they can fix it.

| Status code         | Auto-sync                                      | User-initiated sync                                                                                                                |
|---------------------|------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------|
| `S.NOT_A_REPO`      | **Silent.** Log; project keeps working.        | Route to **publish / collaboration settings** for this project. ("Not a git repo. Publish first.") |
| `S.NO_REMOTE`       | **Silent.** Log; project keeps working.        | Same — route to publish settings.                                                                                                  |
| `S.AUTH_REQUIRED`   | **Silent.** Log; sync just doesn't happen until creds are configured. | Route to **GitHub Connect** flow.                                                                            |
| `S.APP_NOT_INSTALLED` / `S.APP_SUSPENDED` / `S.REPO_NOT_AUTHORIZED` | **Silent.** Log; sync didn't go through but the project is still usable. | Open the `url` param the Status carries.                                  |
| `S.JOB_INTERRUPTED` | Retry once silently; if still failing, log and move on. | Retry; surface a transient-error toast if retry also fails.                                                                |
| `S.SERVER_UNAVAILABLE` / `S.SERVER_ERROR` | **Silent.** Log; daemon will be reachable again next time. | Surface a transient-error toast ("Service unavailable, try again in a moment"). DO NOT route to settings — there's no user-fixable config here, just a daemon that's currently down. |
| `S.AUTH_REFRESH_STALE` | **Silent.** Log; the access token is still valid until its 8h cliff, so don't derail the auto-sync flow. (Peers MAY show a non-intrusive banner on a settings-screen entry — this code is also visible via `get_credentials_status()` → `github.refresh_broken`.) | Surface the translated toast — `translate_status` renders "GitHub session needs re-authentication — current access expires {deadline}. Open GitHub Connect and tap Re-authenticate." `params['expires_at']` is the unix timestamp at which the access token expires (token_time + 8h); `translate._format_deadline` converts it to a relative phrase ("in 47 minutes", "in 3 hours") so the user knows how much runway they have. DO NOT route to settings here — the toast text already names GitHub Connect as the next step; routing would steal control from a user who's mid-sync. |
| Everything else (`PUSHED`, `PULLED`, `NOTHING_TO_COMMIT`, `CONFLICTS`, …) | Translate to status line. | Translate to status line.                                                                            |

The peer is the only party that knows which trigger fired the
sync — the daemon sees an `RPC: sync` and answers truthfully
either way. So the auto/user distinction lives on the peer
side, typically as a flag passed alongside the contributor
name or distinguishing methods (`do_sync()` vs.
`_auto_sync_on_load()`).

**Pre-0.34.1 anti-pattern, fixed by following this contract.**
Treating every sync failure as a user-facing error in the
auto-sync path manifested intermittently as "I selected
project B but ended up back on project A": the auto-sync on
project-load returned `NOT_A_REPO`, the peer's error path
caused the project-load flow to bail / revert / show a
dialog the user couldn't see while a screen transition was
mid-animation, and the user landed on whatever project the
peer had been showing before. This is a peer-side bug, not a
daemon or picker bug — but it has a daemon-side mitigation
*by contract* (this section). Silent auto-sync failures keep
the user in the project they actually selected.

Status-code meanings, for the table above:

- `S.NOT_A_REPO` — project working dir is not a git repo
  (never published).
- `S.NO_REMOTE` — working dir is a git repo but has no
  `remote.origin.url`.
- `S.AUTH_REQUIRED` — no GitHub / GitLab credentials configured
  for this remote host.
- `S.APP_NOT_INSTALLED` / `S.APP_SUSPENDED` /
  `S.REPO_NOT_AUTHORIZED` — credentials present but the
  GitHub-side install is missing / suspended / doesn't cover
  this repo. Each carries a `url` param pointing at the page
  that fixes it.
- `S.JOB_INTERRUPTED` — async job's worker thread died
  (daemon respawned mid-job).
- `S.SERVER_UNAVAILABLE` / `S.SERVER_ERROR` — daemon was
  unreachable or returned a transport-level error. Wrappers
  translate every transport failure (`ServerUnavailable`,
  non-`ok` response) into one of these codes, so peers never
  see a raw exception from a query-shaped wrapper. Distinct
  from the config-class codes: there's no user-facing
  configuration that fixes a daemon that's currently down.
- `S.AUTH_REFRESH_STALE` — the daemon's last attempt to refresh
  the GitHub access token failed (typically
  `incorrect_client_credentials` from the OAuth endpoint). The
  current access token is still valid until its 8h-from-issuance
  expiry, so sync keeps working in this session — but the
  refresh path can't mint a replacement, so once the access
  token expires every authenticated git op will start failing
  with no user-visible warning unless this code is surfaced.
  Carries `params['expires_at']` (unix timestamp of the access
  token cliff) so `translate_status` can render the relative
  deadline ("in 47 minutes", "in 3 hours") in the toast. Also
  surfaced via `get_credentials_status()` →
  `github.refresh_broken` / `github.access_token_expires_at`
  for peers that want a startup-time banner. Cleared when the
  user completes a fresh device flow at GitHub Connect (which
  calls `set_github_tokens` daemon-side; that function clears
  `refresh_broken` atomically with the token write).

The general shape — both contexts:

```python
# Auto-sync (post-load, post-edit, background) — silent on
# configuration-class AND transport-class failures; never derail
# whatever the user was doing.
def _auto_sync(self, langcode):
    result = sync_project(langcode)
    if result.has_any(S.NOT_A_REPO, S.NO_REMOTE,
                      S.AUTH_REQUIRED, S.CONTRIBUTOR_UNSET,
                      S.APP_NOT_INSTALLED, S.APP_SUSPENDED,
                      S.REPO_NOT_AUTHORIZED,
                      S.SERVER_UNAVAILABLE, S.SERVER_ERROR,
                      S.AUTH_REFRESH_STALE):
        # Log only; sync just didn't happen (or it did but a
        # warning piggybacked), project keeps working, user
        # keeps working. The AUTH_REFRESH_STALE deadline warning
        # is surfaced in do_sync below, not here.
        print(f'[auto-sync] {langcode}: '
              f'{result.codes()!r} (silenced)',
              file=sys.stderr)
        return
    if result.has(S.JOB_INTERRUPTED):
        # One silent retry; on second failure, log and move on.
        return self._auto_sync_retry_once(langcode)
    self.show_status(translate_result(result))  # PUSHED, etc.

# User-initiated sync — the user just tapped Sync; route to
# whatever fixes the problem, or surface the success line.
def do_sync(self, langcode):
    result = sync_project(langcode)

    # AUTH_REFRESH_STALE piggybacks on whatever primary code the
    # sync returned (PUSHED + STALE while the access token is
    # still valid; NOT_A_REPO + STALE if the user has both a
    # publish gap AND a broken refresh, etc.). Always surface
    # it BEFORE the routing branches consume the result, so the
    # deadline warning isn't silently dropped on the way to
    # the publish-settings page. ``translate_status`` renders
    # just the AUTH_REFRESH_STALE message — the primary code is
    # handled separately below.
    stale = next((s for s in result.statuses
                  if s.code == S.AUTH_REFRESH_STALE), None)
    if stale is not None:
        self.show_toast(translate_status(stale))

    if result.has_any(S.NOT_A_REPO, S.NO_REMOTE):
        self.open_publish_settings(langcode)
    elif result.has(S.CONTRIBUTOR_UNSET):
        # User hasn't entered their name yet; route to the
        # daemon settings UI's contributor field. Same shape
        # as AUTH_REQUIRED — actionable on user-initiated sync,
        # silent on auto-sync (handled above).
        self.open_sync_settings()
    elif result.has(S.AUTH_REQUIRED):
        self.open_github_connect()
    elif result.has_any(S.APP_NOT_INSTALLED, S.APP_SUSPENDED,
                        S.REPO_NOT_AUTHORIZED):
        # Each of these statuses carries the actionable URL in
        # ``params['url']``. Pull from the first matching status.
        url = next((s.params.get('url', '') for s in result.statuses
                    if s.code in (S.APP_NOT_INSTALLED,
                                  S.APP_SUSPENDED,
                                  S.REPO_NOT_AUTHORIZED)),
                   '')
        self.open_url(url) if url else self.open_github_connect()
    elif result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR):
        # Transient — no user-fixable config here; just say so
        # and let the user retry.
        self.show_toast(translate_result(result))
    elif result.has(S.JOB_INTERRUPTED):
        # Retry; surface a transient-error toast if retry fails.
        ...
    else:
        # PUSHED / PULLED / NOTHING_TO_COMMIT / CONFLICTS / etc.
        self.show_status(translate_result(result))
```

This is the part of the daemon → peer contract that "status code,
not translated string" exists for. Substring-matching the
translated message ("if 'publish' in msg: route_to_settings") is
the regression to avoid.

## Internationalization (i18n)

The client owns gettext domain `azt_collab_client`. Catalogs live at
`azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po`
(plus the `.mo` that `i18n._ensure_mo` auto-compiles on first
`set_language` — there is no external `msgfmt` build dep).

### What auto-runs

- On import, `azt_collab_client.i18n` reads
  `$AZT_HOME/config.json → ui.language` and applies that language.
  Peer apps that touch `azt_collab_client` get the right initial
  language without any setup.
- `azt_collab_client.translate.tr(msg)` defaults to the client
  catalog. KV templates that already do
  `#:import _ azt_collab_client.translate.tr` keep working.

### Public API (`azt_collab_client.i18n`)

```python
from azt_collab_client import i18n

i18n.set_language('fr')                # switch + persist to config.json
i18n.language_pref()                   # read persisted language ('en' default)
i18n.current_language()                # active language (after fallback)
i18n.available_languages()             # [(code, display_name), ...]
i18n.display_name('fr')                # 'Français' (single source of truth)
i18n.scan_catalog_languages(dir, dom)  # peer-side catalog discovery helper
i18n.ensure_mo(dir, dom, lang)         # compile .po → .mo for an arbitrary
                                       # peer catalog (lazy; no msgfmt dep)
i18n._('Cancel')                       # translate via the client catalog
i18n.gettext_translation()             # underlying gettext.NullTranslations
                                       # subclass — for add_fallback chains
```

### Peer integration

**Peer with no own catalog** (a small viewer, the standalone picker
subprocess): nothing to do. The default translator is the client
catalog; UI strings translate automatically once a language is
selected from the daemon's settings UI (`open_server_ui()`).

**Peer with its own catalog** (the recorder, with `aztrecorder.po`):
chain via gettext's native `add_fallback` so peer-owned strings
resolve in the peer catalog and client-owned strings fall through
to ours. Call `i18n.ensure_mo(...)` first so the peer can ship
`.po`-only just like the client does — no external `msgfmt` build
step:

```python
import gettext
import azt_collab_client
from azt_collab_client import i18n as collab_i18n

def set_recorder_language(lang):
    if lang == 'en':
        recorder_t = gettext.NullTranslations()
    else:
        collab_i18n.ensure_mo(RECORDER_LOCALES, 'aztrecorder', lang)
        recorder_t = gettext.translation(
            'aztrecorder', localedir=RECORDER_LOCALES, languages=[lang],
            fallback=True)
    collab_i18n.set_language(lang)             # client catalog + persist
    recorder_t.add_fallback(collab_i18n.gettext_translation())
    azt_collab_client.set_translator(recorder_t.gettext)
```

`ensure_mo` writes the `.mo` next to the `.po`. On Android that's
inside the APK's private filesDir (where p4a extracts Python source
on first run — writable, so the lazy compile works the same way for
peers as for the client).

After this, `_(msg)` in recorder code resolves recorder strings
first, then falls through to the client catalog. No string
duplication; both catalogs stay single-source.

`translate.tr` *also* has a software fallback in case the host
forgets `add_fallback`: if `set_translator(host_tr)` is registered
and `host_tr(msg)` returns `msg` unchanged, `tr` retries via the
client catalog. So a misconfigured host still gets client strings
translated for KV-rendered text. Don't rely on this in lieu of
`add_fallback` — but it makes the failure mode "missing translation
for client string" instead of "untranslated forever".

### Live retranslation

The daemon's settings UI (`python -m azt_collabd ui` /
`open_server_ui()`) ships a language selector that calls
`i18n.set_language(code)` and rebuilds its own ScreenManager so KV
`text: _('...')` bindings re-evaluate.

For peers that want to live-retranslate while running (so a user
flipping language in a settings subprocess updates the peer's open
window without restart), poll `$AZT_HOME/config.json` mtime on a
`Clock.schedule_interval(..., 1.0)` and rebuild the relevant
screens. Pattern in `azt_collabd/ui/picker_app.py:_check_language_change`:

```python
def _check_language_change(self, _dt):
    new_mtime = self._get_config_mtime()
    if new_mtime == self._config_mtime:
        return
    self._config_mtime = new_mtime
    persisted = collab_i18n.language_pref()
    if persisted == collab_i18n.current_language():
        return
    set_recorder_language(persisted)   # the chain shown above
    # Then rebuild your ScreenManager (clear_widgets + re-add) so
    # KV `text: _('...')` bindings re-evaluate.
```

If a peer doesn't want live retranslation, it can skip the watcher;
the persisted language is picked up at next launch via the auto-init
at import.

### Adding a translation

1. Wrap the user-visible string in `_(...)` (KV) or
   `azt_collab_client.translate.tr(...)` / `i18n._(...)` (Python).
2. Add the msgid + translation to the relevant
   `azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po`.
3. Delete the stale `.mo` next to it (or just edit the `.po` and
   trust mtime — `_ensure_mo` recompiles whenever the `.mo` is older
   than the `.po`).
4. No daemon-side change needed; translations are client-only.

### Adding a new language

1. Create `azt_collab_client/locales/<code>/LC_MESSAGES/azt_collab_client.po`
   (copy `fr/.../azt_collab_client.po` as a template).
2. Add the display name to `i18n._DISPLAY_NAMES` if the BCP-47 code
   isn't already there.
3. `available_languages()` discovers it on next call; the settings UI
   adds a button automatically.

## LIFT-file access — peers MUST go through `LiftHandle`

The daemon owns the canonical copy of every project's LIFT file
under `$AZT_HOME/projects/<lang>/<file>.lift`. On the new Android
model the daemon lives in the standalone server APK
(`org.atoznback.aztcollab`) and that path is in the server APK's
private `filesDir` — peer packages **cannot** `open()` it (sandbox
denies; you'll see `[Errno 2] No such file or directory` even when
the file exists, because the recorder process is `org.atoznback.<peer>`
and has no UID-level read on `/data/user/0/org.atoznback.aztcollab/`).

### URI grants and provider availability are stable across server kills

From client 0.20.0 / server APK 0.16.0+, the server APK is pinned by
a sticky-bound service (`AZTServiceProviderhost`). Picker dismissal
no longer brings the process down, so the URI grant the picker emits
in its result Intent is reachable for as long as the receiving
Activity is alive (Android scopes the grant to the receiver, not to
the source process). Under memory pressure Android may still kill
the host; the next peer ContentResolver call (read **or** write)
auto-spawns it via Android's unconditional ContentProvider contract.
Detached FDs survive the source kill (kernel-managed inode).

What this means in practice:

- You may safely defer `LiftHandle(uri).open_read()` to a
  `Clock.schedule_once` callback or any later user gesture. Pre-0.16
  the source process exited synchronously with the picker, leaving a
  ~600ms race window before Android cascade-SIGKILL'd the peer.
- The "no caching" rule still stands. Reopen on every access not
  because the URI expires (it doesn't, on 0.16+) but because the
  daemon's copy is the single source of truth and another peer may
  have written to it.
- If a peer holds an audio FD across a long user interaction (60-s
  recording), that's now safe. Pre-0.16 it depended on whether the
  user re-opened the picker in the meantime.
- If you observe a peer being SIGKILL'd by `appDiedLocked` and the
  server APK is 0.16.0+, that's a regression — file it.

### The one peer-visible recovery surface: `Result.has(S.JOB_INTERRUPTED)`

If your peer uses `request_sync` + `poll_job` (the fire-and-forget
path), you may receive a `JOB_INTERRUPTED` status if the daemon was
killed mid-job (OOM, kill -9, container restart) and respawned.
Treat it identically to `S.SERVER_UNAVAILABLE`: transient, retryable.
Synchronous `sync_project` callers never see this code — a dead
binder mid-call surfaces as `ServerUnavailable` and the client
transport's existing retry loop handles it transparently.

```python
result = poll_job(job_id)['result']
if result and result.has(S.JOB_INTERRUPTED):
    # retry the underlying operation; the daemon respawned and
    # forgot the worker thread that was running this job_id.
    new_id = request_sync(langcode)
```

### Don't cache. Use `ContentResolver` every time.

A peer-side cache (download to `<peer>/filesDir/lift_cache/...`,
edit it, push back) breaks the architecture's promise of a single
source of truth. Two peers (or the same peer across two sessions)
that read at T0 and write at T1 / T2 will race; the later writer
clobbers the earlier writer's edits and the daemon happily commits
+ pushes the corrupted state. **Read and write through the
provider every time.**

### `azt_collab_client.LiftHandle`

The picker emits one of two shapes from `pick_project()['path']`:

- **Filesystem path** (desktop, or any platform's open-file flow)
  → peer uses regular `open(path, 'rb')`.
- **`content://org.atoznback.aztcollab/<lang>/<file>.lift`** (Android
  clone / template flow on the new model) → peer must use
  `ContentResolver.openFileDescriptor(uri, 'r')` then
  `os.fdopen(detached_fd, 'rb')`.

`LiftHandle` papers over the difference so peer code stays uniform:

```python
from azt_collab_client import LiftHandle
from xml.etree import ElementTree

handle = LiftHandle(path_or_uri_from_picker)
with handle.open_read() as f:
    tree = ElementTree.parse(f)        # accepts file-like
...
with handle.open_write() as f:
    tree.write(f, encoding='utf-8', xml_declaration=True)
```

Both `open_read()` and `open_write()` return binary file-likes
usable as context managers. The picker side adds
`FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION`
to the result Intent so the URI grant is in place by the time
`pick_project()` returns to the peer.

### `atomic_open_write` — when peers need cross-process atomicity

`LiftHandle.atomic_open_write()` (and `MediaHandle`) gives the
peer an atomic-replace contract on both transports:

- **Filesystem path**: writes a sibling tempfile and renames over
  the destination via `os.replace`. Peer-process tempfile, peer-
  process rename.
- **`content://` URI** (daemon 0.36.0+): buffers bytes in memory
  and ships them to `POST /v1/projects/<lang>/atomic_commit`. The
  daemon writes a tempfile in its own process and renames; the
  write is serialized via `project_lock` against the daemon's
  own merge-output writes and any concurrent atomic_commit from
  another peer.

On exception, the destination is untouched in both cases. Two
concurrent `atomic_open_write` calls on the same destination are
safe: whichever rename runs last wins, and the destination is
always a complete copy of one version, never torn.

```python
from azt_collab_client import LiftHandle

handle = LiftHandle(path_or_uri_from_picker)
with handle.atomic_open_write() as f:
    tree.write(f, encoding='utf-8', xml_declaration=True)
# On clean exit: destination is now the new bytes, atomically.
# On exception during the with block: destination unchanged.
```

**When to use which.** Use `atomic_open_write` whenever the
write needs to be safe against concurrent observers — most LIFT
writes do, because the daemon's merge-output write can land at
any moment. `open_write` is the older path-lock-only contract;
it's safe for same-process serialization but not for cross-process
races. Pre-0.36.0 the URI form of `atomic_open_write` fell back
to `open_write` because there was no daemon-side RPC for the
atomic-rename half of the contract; that gap is closed now and
peers should prefer `atomic_open_write` for any URI write that
could race.

**Memory cost on URIs.** The peer holds ~1.33× the file size
during base64 encoding + send (the request body is base64 inside
the JSON envelope). For LIFT (≤ tens of MB at worst) this is
fine. A future chunked-upload endpoint could shrink the working
set if a much larger payload ever ships.

### Recorder migration checklist

When migrating a peer (the recorder is the first; viewer / future
peers follow the same pattern):

1. **Replace every `open(lift_path, ...)` site** with
   ``with LiftHandle(p).open_read()/open_write() as f: ...``.
   Particularly:
   - `lift_api.LiftDoc.__init__` — `ElementTree.parse(path)` →
     `with handle.open_read() as f: ElementTree.parse(f)`.
   - The save / write-back path — `tree.write(path, ...)` →
     `with handle.open_write() as f: tree.write(f, ...)`.
   - Any `Path(lift_path).read_text()` etc.
2. **Stop trying to compute auxiliary paths from the LIFT path.**
   On a `content://` URI, ``os.path.dirname`` is meaningless.
   Audio files / image references / any sibling resources should
   also be resolved through the provider — either via their own
   `content://` URIs or via the daemon's audio endpoints. If your
   recorder branches on `os.path.exists(some_sibling)` keyed on
   the LIFT path's directory, that branch goes wrong on Android.
3. **Pass the original `path_or_uri` string to
   `register_project(...)` / `derive_langcode(...)`** — the
   daemon's wrappers already accept either shape (the daemon's
   server-side `derive_langcode` handles relative-URI parsing too;
   `register_project` stores it as-is in `projects.json`).
4. **Don't introduce a `local_lift_cache_path` field.** If you find
   yourself reaching for one, reread "Don't cache" above. The
   write goes to the daemon's copy via `LiftHandle.open_write()`;
   that *is* the canonical edit.
5. **For binary auxiliary files (audio)** that the daemon also
   serves through the same provider, mirror the same pattern with
   a `LiftHandle`-equivalent (`LiftHandle` is named for the LIFT
   case but doesn't validate file content; you can use it for any
   provider-served file). A future helper `MediaHandle` may
   formalize the audio case if it sprouts its own concerns.

### What NOT to do

- ❌ `shutil.copy(lift_path, local_cache)` followed by working from
  the copy. Lost-update guaranteed.
- ❌ `open(handle.path_or_uri, 'rb')` — the `content://` form is
  not a filesystem path.
- ❌ Treating the URI as a file via `pathlib.Path(uri)` then
  calling its file methods. They'll silently produce nonsense.
- ❌ Caching results of `LiftHandle(p).open_read()` past the
  context-manager exit. The handle is cheap; reopen on each access.

### Audio + image cross-package access — shipped

**Daemon side:** `AZTCollabProvider` serves sibling files under the
same authority as the LIFT URI:

```
content://org.atoznback.aztcollab/<lang>/audio/<basename>
content://org.atoznback.aztcollab/<lang>/images/<basename>
```

Provider auto-creates `audio/` and `images/` on first write
(`openFile(mode='w')` mkdir-p's the parent — see
`azt_collabd/android_cp/service.py:_resolve_path`'s
`_ALLOWED_MEDIA_DIRS = ('audio', 'images')` whitelist). Both
audio and images are read+write from peers as of 0.35.2 (0.18.0
through 0.35.1 gated image writes behind a `PermissionError`
under an "daemon owns image additions" rule; tracing the history
showed no concern actually driving that gate, and symmetry with
audio is cleaner). The picker's result-Intent grant flags
(`FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION`)
cover same-authority sibling URIs without per-file grants.

**Client side (`azt_collab_client.lift_io`):**

- `MediaHandle(path_or_uri, kind='audio'|'image')` — `LiftHandle`
  subclass with a `kind` field used in log lines / error messages
  only. Both kinds are read+write; the kind label doesn't gate
  anything functionally.
- `audio_uri_for(lift_path_or_uri, basename)` /
  `image_uri_for(lift_path_or_uri, basename)` — compose the sibling
  URI / filesystem path so callers stay blind to the path-vs-URI
  distinction.

**Recorder side (1.32.0):**

- `LIFTDatabase.audio_target(basename)` /
  `LIFTDatabase.image_target(basename)` — thin wrappers over
  `audio_uri_for` / `image_uri_for`.
- `LIFTDatabase._resolve_uri_image(href)` pulls a sibling image
  from the daemon's provider into the peer's image cache dir
  (`<image_cache_dir>/_uri_images/<href>`) the first time it's
  rendered, so `AsyncImage` can render by path. Memoised per
  LIFTDatabase instance.
- `_start_android_recording` opens an audio URI through
  `ContentResolver.openFileDescriptor('w')` and hands the Java
  `FileDescriptor` straight to `MediaRecorder.setOutputFile(fd)`.
  The pfd is held on `self._record_pfd` until stop+release, then
  closed (Java owns the FD lifetime — we do *not* `detachFd()`).
- `play_audio` resolves to `audio_uri_for(...)` on URI projects and
  uses `MediaPlayer.setDataSource(ctx, Uri.parse(uri))` instead of
  the path string overload.
- Image *additions* go through `MediaHandle('content://…/images/<basename>',
  'image').open_write()` since 0.35.2 — same shape as audio. Pre-0.35.2
  the peer gated off image writes on URI projects under a now-removed
  "daemon owns image additions" policy; the URL/cache fallback for
  displaying images still works the same way (it has its own merits
  for offline rendering), but the local-copy-into-`images/` step
  now runs on URI projects too. Two-write race semantics on the
  illustration ref (LIFT-side update via `LiftHandle.open_write`)
  mirror audio's pre-existing pattern; binary-conflict resolution
  on basename collisions surfaces as `non-lift-modify-modify`
  Conflict per `repo._merge_diverged`.

**Discovery:** no `list_audio` / `list_images` RPCs needed — both
sets of basenames are already encoded in the LIFT XML (audio in
`<citation><form>` audiolang text, images in
`<illustration href=…/>`). If a future admin UI wants directory
listing, add `/v1/projects/<lang>/list_images` separately.

## CAWL image access — architectural rationale

> **For the conformity contract** — what peers must call, what
> they must not do, the two-stage migration checklist, the
> verification block — see ``CLIENT_INTEGRATION.md`` § 10 (CAWL
> image access) and § 11 (per-project overrides). This section
> is the *why*; that file is the *what*.

### Why CAWL lives on the daemon

The CAWL → image-URL index and the CAWL image binaries are
**suite-scoped infrastructure**: the same data is correct for
every project, every peer, every device on a given install.
The suite's mental buckets:

- **Project-scoped resources** (LIFT, audio, project images):
  daemon owns them, peers consume via provider URIs.
- **Suite-scoped resources** (CAWL index, CAWL image
  binaries — same for every project, every peer, every
  device): also daemon-owned. "Shared by everyone on this
  device" is exactly what one-daemon-per-device gives you.
- **Peer-scoped resources** (UI state, theme, in-memory hot
  caches for render perf): peer-owned. Only category that
  genuinely belongs in a peer's ``filesDir``.

Pre-0.37 CAWL was peer-scoped — a vestige from when the suite
had only one peer. That produced three structural failures:

1. **Rate limit.** GitHub's unauthenticated REST cap is
   60/hour/IP; tight rebuild loops, CI, or multi-peer
   devices exhausted it and the resolver returned empty for
   the rest of the session.
2. **Per-peer disk duplication.** N peers × ~100–300 MB of
   image binaries each, sandbox-isolated on Android so they
   couldn't share even if you wanted them to.
3. **Install-day-no-network.** Fresh install with no
   connectivity had no way to bootstrap.

Daemon ownership fixes all three. Cache lives at
``$AZT_HOME/cawl/<owner>/<repo>/{index.json, images/<basename>}``,
keyed by repo slug — so N projects sharing one image_repo share
*one* on-disk cache directory.

### Why per-project `cawl_image_repo`

CAWL repo selection is a per-project setting, not a daemon-
global or peer-global one. Different projects can legitimately
point at different image sets (fork, culturally specific
imagery, internal mirror). The ``Project`` record carries the
field; the daemon-global default is fallback.

Resolution precedence:

1. ``Project.cawl_image_repo`` — per-project override.
2. ``azt_collabd.config.cawl_image_repo()`` — daemon-global
   fallback. Default
   ``_CAWL_IMAGE_REPO_DEFAULT`` lives in ``azt_collabd/
   config.py`` and is the single source of truth for the
   suite-canonical CAWL repo.
3. Empty everywhere → daemon serves ``{}`` /
   ``FileNotFoundError`` without any network call.

The cache layer doesn't know whether the resolved slug came
from per-project or from the global default. Both paths land
on the same on-disk cache file when they resolve to the same
slug — the dedup property is preserved across the
configuration surface.

### Why we don't bundle the image binaries

The APK ships a bundled **index** seed (``azt_collabd/data/
cawl/<owner>/<repo>/index.json``, ~50 KB) so install-day-no-
network devices have *something* to serve. The image binaries
themselves are deliberately not bundled: 1701 images × 50–200
KB ≈ 100–300 MB shipping in every APK release is the wrong
trade — slow install, mobile-data hostile, and the daemon-side
lazy cache covers the steady state without bundling.
Decision logged 2026-05-12.

If a future session proposes "bundle the whole CAWL image set
in the APK", refuse — it's a re-litigation of a closed
decision, not a fresh question.

### Why the two-stage migration matters

Stage 1 (peer swaps its index fetch for ``cawl_index``) removes
the user-visible rate-limit symptom (the 403). It feels like
the migration is done. It isn't — Stage 2 (peer swaps its
binary fetches for ``CAWLHandle``) is where the architectural
wins land:

- Cross-peer dedup: one cache, N peers reading from it.
- Survives peer uninstall: the daemon's cache outlives any
  individual peer.
- Removes the per-peer 100–300 MB on-disk cost.

A half-migrated peer (Stage 1 only) loses none of these wins
to the rate-limit *symptom* — but it still pays the
architectural cost. The contract says both stages because both
are needed for the architecture to be correct, not just for
the surface symptom to go away. ``CLIENT_INTEGRATION.md`` § 10
spells out the action items + verification.

### Wire-shape note

``Project.cawl_image_repo`` and ``Project.repo_slug`` are
plain string fields on the project record, returned by every
``open_project`` / ``project_status`` / ``list_projects``
response. The client-side ``Project`` dataclass mirrors them
with empty-string defaults so pre-0.39 daemons that don't emit
the fields still decode cleanly. No code changes are required
on the client side to *read* the fields; the setters
(``set_cawl_image_repo``, ``set_repo_slug``) are documented as
part of the public API surface in ``CLIENT_INTEGRATION.md``
§ 11.

## Commit identity — architectural rationale

> **For the conformity contract** — what peers must call, hard
> rules, refusal-status handling — see ``CLIENT_INTEGRATION.md``
> § 12 (Commit identity). This section is the *why*.

The git commit author identity has two slots: NAME (human
display name, used by GitHub for author-aggregation) and
EMAIL (a stable per-identity string, used by git tooling for
disambiguation). As of 0.40.0 the suite uses these as:

- NAME = the user's display name verbatim. GitHub groups
  commits by NAME, so one person committing from multiple
  devices appears as one author in the project's contributor
  list.
- EMAIL = ``<safe_name>@<safe_device>``. ``git log
  --format='%ae'`` differentiates by device when the same
  human commits from a phone vs. a tablet vs. a laptop. The
  email is non-routable; it's an identifier, not an address.

**Why daemon-owned, no peer pass-through.** Pre-0.40 the
contributor name lived in two places — peers passed it on
every sync/init RPC, and the daemon also kept a stored
fallback. The peer's pass-through won by default. That meant
a user who typed their name in the daemon UI but had a peer
hard-coding ``contributor='Recorder'`` got commits attributed
to "Recorder" anyway, with no visible cause. 0.40 removes the
wire surface entirely (peer wrappers drop the kwarg, daemon
endpoints ignore body['contributor']) and forces unset state
to surface explicitly as ``S.CONTRIBUTOR_UNSET`` rather than
silently substituting a placeholder. Same architectural rule
as the rest of the per-user state in NOTES_TO_DAEMON.md's
sole-authoritative-source table.

**Why device_name auto-populates.** Reading
``Settings.Global.DEVICE_NAME`` (Android) or
``socket.gethostname()`` (desktop) on first read gives a
useful default without forcing the user through a settings
screen on day one. The OS value is a known label (the user
named their phone, or the manufacturer slug like
``"SM-T580"`` is at least diagnosable). User can override via
the settings UI for clarity / privacy. Empty stored value
re-triggers detection on next read — a "reset to OS default"
affordance.

**Why no ``@unknown`` fallback in production.** The
``unknown-device`` last-resort is the explicit "nothing
worked" sentinel for the rare case where all autodetect
probes fail (de-Googled Android with no settings provider, a
chroot without ``socket.gethostname``). It's visibly a
placeholder — the same philosophy as removing the
``'Recorder'`` literal: if the system can't identify the
device, the commit author should make that obvious, not
pretend.

**Decision log: why two fields, not one composed string.**
Storing ``name + device`` as a single user-typed string
("Marie Dubois (tablet)") would seem simpler but conflates
two things — and GitHub's author-aggregation can't group by
"Marie Dubois" if some commits arrive as
"Marie Dubois (tablet)" and others as "Marie Dubois
(laptop)". Splitting into NAME and EMAIL leverages git's
native distinction; the cost is two store fields, the win is
correct GitHub UX.

## UI submodule (`azt_collab_client.ui`)

Shared Kivy screens (`LangPickerScreen`, `ProjectPickerScreen`) and
helpers (`clone_url_popup`). Sister apps register these into their
own `ScreenManager`. Translations route through
`azt_collab_client.translate` — call `set_translator(...)` once at
startup if your host has its own i18n module (the recorder does).

Don't add Kivy imports outside `ui/`; see hard rule #3.

### Shared assets — client-first model

Anything *shared in shape* across the suite (gear, sync, share, future
back/close arrows) lives at
`azt_collab_client/ui/assets/icons/<name>.png` so every sister app
that imports the client gets it for free. Resolve via
`from azt_collab_client.ui import icon_path; icon_path('gear')` — the
helper returns an absolute path (or `''` if not bundled). Standalone
subprocess (picker, settings UI) and sister apps cannot use relative
paths; their cwd isn't the host's repo.

Peer-specific icons stay in the peer (the recorder's
`icons/microphone.png`, `icons/redo.png`, `icons/icon*.png` app-icon
variants — these have no plausible second consumer and encode
recorder-specific UX). Peers that want to override a shared icon with
their own theming pass an explicit override path to whatever consumer
takes one (e.g. `register_picker_kv(gear_icon=...)`); there is no
implicit cwd-based search.

When in doubt, **default to client-first**. It's easier to override
locally later than to deduplicate parallel copies once they've drifted.
The recorder's KV currently uses relative `'icons/<name>.png'` strings
that resolve in its own cwd — that's fine, no migration required; the
client-first rule applies to *new* shared-shape assets and to any
asset whose move-to-shared is forced by a second consumer.

### Self-update — `check_for_update`

`azt_collab_client.ui.check_for_update(...)` is a reusable updater
each suite APK plugs into its settings screen. Identity is fully
parametric so the same helper serves the server APK and every peer.

**Helper contract.** Spawns a worker thread, marshals callbacks back
to the Kivy UI thread via `Clock.schedule_once`, and on Android
downloads + dispatches `ACTION_VIEW` with the
`application/vnd.android.package-archive` MIME type so the system
installer takes over. Non-Android hosts get a translated
"APK install is only available on Android." through `on_error`.

Required args (all keyword-only):

- `repo` — `'owner/repo'` on GitHub. Hits
  `GET /repos/<owner>/<repo>/releases/latest`.
- `current_version` — caller's `__version__`; compared as a semver
  tuple against the release's `tag_name` (`v` / `V` prefix tolerated).
- `asset_filename` — exact name of the release asset to fetch. Each
  app names its own (`azt_collab.apk`, `azt_recorder.apk`, …).
- `on_status` — `callable(str)`; receives translated state strings
  ("Checking for updates…", "Downloading {pct}%…", "Installing…").

Optional: `on_no_update`, `on_error`, `download_dir` (defaults to
`$AZT_HOME/updates`).

**Server-APK adapter** (already shipped):

```python
# azt_collabd/ui/app.py — CollabUIApp.update_app
from azt_collabd.config import update_repo  # 'kent-rasmussen/azt-collab'
check_for_update(
    repo=update_repo(),
    current_version=azt_collabd.__version__,
    asset_filename='azt_collab.apk',
    on_status=self._set_update_msg,
    on_no_update=lambda: self._set_update_msg(_tr('Up to date.')),
    on_error=self._show_error,
)
```

The repo defaults to `kent-rasmussen/azt-collab` and is overridable
via `azt_collabd.configure(update_repo=...)` or the `AZT_UPDATE_REPO`
env var, so a fork can ship the same code aimed at a different
release feed.

**Peer adapter pattern** (recorder, viewer, …):

```python
# In your App subclass (e.g. CollabApp.update_app for the recorder):
def update_app(self):
    from azt_collab_client.ui import check_for_update
    check_for_update(
        repo='kent-rasmussen/azt-recorder',   # or your fork
        current_version=__version__,           # peer's own __version__
        # asset_filename omitted — derived at runtime from the
        # peer's own Android package name (e.g. aztrecorder.apk
        # for org.atoznback.aztrecorder). Pass explicitly only if
        # the fork publishes under a non-default scheme.
        on_status=self._set_update_msg,
        on_no_update=lambda: self._set_update_msg(_('Up to date.')),
        on_error=self._show_error,
    )
```

Wire the button into the peer's settings KV alongside the existing
"Share this app" affordance — same shape, different `on_release`.

**Manifest cost.** Each APK that exposes the button needs
`REQUEST_INSTALL_PACKAGES` in `buildozer.spec → android.permissions`.
Android 8+ also requires the user to flip the per-source
"Install unknown apps" toggle the first time; the helper detects
this via `PackageManager.canRequestPackageInstalls()` and routes to
`Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` so the user lands on the
right page in one tap.

**No SHA verification in v1.** TLS to GitHub plus Android's
signature-match install check (suite keystore enforced everywhere)
are the integrity layers. A future hardening pass can add a `.sha256`
companion asset if the release process publishes one.

### Bootstrap — `bootstrap()`

Suite invariant: **the user installs one APK** — the peer they
opened. The standalone server APK and any subsequent updates are
provisioned by the peer itself on first run.

`azt_collab_client.ui.bootstrap(...)` is the single entry point that
implements this. Each peer calls it once, early in startup
(`App.on_start` is the natural seam). Recommended shape: a thin
wrapper method on your `App` subclass that supplies identity, and a
status sink that routes progress strings into your existing logging
surface:

```python
# in your App class
def on_start(self):
    ...
    # Schedule for next frame so the UI is up before any popup fires.
    Clock.schedule_once(lambda dt: self._run_bootstrap(), 0)

def _run_bootstrap(self):
    # Deferred import keeps bootstrap.py + its Kivy/jnius deps out of
    # the import graph until the peer actually fires it.
    from azt_collab_client.ui import bootstrap
    from appinfo import APP_NAME
    bootstrap(
        peer_repo='kent-rasmussen/azt-recorder',
        peer_version=__version__,
        # peer_asset_filename omitted — derived at runtime from the
        # peer's own Android package name.
        peer_display_name=APP_NAME,
        on_status=self._log_bootstrap_status,
        on_done=self._auto_load_last_project,
        on_error=self._log_bootstrap_status,
        font_name=_FONT_NAME,
    )

def _log_bootstrap_status(self, message):
    """Surface bootstrap progress / errors through the peer's existing
    in-app status channel. The recorder routes to its collab-screen
    log; a viewer would route to its equivalent."""
    print(f'[bootstrap] {message}', file=sys.stderr)
    try:
        sm = self.root.ids.sm
        collab = sm.get_screen('collab')
        collab._set_log(message)
    except Exception:
        pass

def _auto_load_last_project(self):
    """Wired as bootstrap()'s on_done. Client 0.28.5+ guarantees
    on_done fires only when the daemon is reachable, so the first
    daemon-touching RPC needs no defensive try/except."""
    from azt_collab_client import last_project, open_project
    langcode = last_project()
    ...
```

The recorder (`azt_recorder/main.py: _run_bootstrap`,
`_log_bootstrap_status`) is the canonical reference for this
pattern — clone it verbatim and substitute your own
`peer_repo` / `peer_asset_filename` / `peer_display_name` /
status-screen lookup.

The helper:

1. Calls `check_server_compat()`. On `server_unreachable` it
   prompts "Install AZT Collaboration?" and on Yes downloads
   `azt_collab.apk` from `kent-rasmussen/azt-collab/releases/latest`
   via `check_for_update`. `server_too_old` shows the matching
   "Update AZT Collaboration?" prompt. `client_too_old` jumps to
   step 2.
2. Probes the peer's own latest release. If newer, prompts
   "Update <peer name>?" and on Yes downloads + installs the peer's
   own APK.
3. Calls `on_done` **only on the healthy path** (server reachable +
   peer up to date or self-update declined/no-op). Client 0.28.5+
   contract: the `server_unreachable` / `server_too_old` prompts
   are terminal and do **not** fire `on_done`; the install popup
   stays modal until the user installs (or quits), and a fresh
   bootstrap re-enters from the install-completion chain. So
   if `on_done` fires, the daemon is reachable — the first RPC
   wired to `on_done` doesn't need a defensive try/except for
   "daemon not there yet."

**Buildozer requirement.** The peer's `buildozer.spec` must list
`REQUEST_INSTALL_PACKAGES` in `android.permissions` so the install
intent fires. Without it the install silently no-ops. Android 8+
also requires the user to flip the per-source "Install unknown
apps" toggle the first time; the underlying `check_for_update`
detects this and routes to `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES`
with our package pre-scoped.

**Defaults.** `server_repo` defaults to `kent-rasmussen/azt-collab`
and `server_asset_filename` defaults to `azt_collab.apk`. A fork
that ships its own service can override both.

**Desktop hosts** call `on_done` immediately — there's no APK to
install, so the bootstrap is a no-op outside Android.

## Sister-app integration recap

> **Canonical client-integration checklist:**
> [`CLIENT_INTEGRATION.md`](CLIENT_INTEGRATION.md) — the
> single contract every peer follows. Read that first; this section
> is the older / shorter overview kept for context.

Setup is symlink-based, not pip-installed. From a sibling app's
repo root (relative to `azt-collab/`):

```bash
for x in azt_collab_client examples android; do
    ln -s "../azt-collab/$x" "$x"
done
# azt_collabd is also symlinked when the sister app embeds the daemon
# (desktop & legacy Android); on the new Android model it lives in the
# standalone server APK and is reached via the ContentProvider transport.

# AndroidManifest <queries> block needed on Android 11+ so the peer
# can see the standalone server APK via PackageManager:
ln -s ../azt-collab/android/manifest_extras_peer.xml manifest_extras.xml
```

In the peer's `buildozer.spec`:

```
android.extra_manifest_xml = %(source.dir)s/manifest_extras.xml
```

The key is `extra_manifest_xml` — `manifest_extra_xml` (different word
order) is silently ignored by buildozer.

At startup, before the first client call:

```python
import azt_collab_client
azt_collab_client.configure(app_id='<your-app-id>')

# i18n: skip this block entirely if you don't have your own catalog —
# azt_collab_client.i18n auto-applies the persisted UI language on
# import, so the client catalog Just Works.
#
# If you DO have your own catalog (recorder pattern), chain it:
#   import gettext
#   from azt_collab_client import i18n as collab_i18n
#   recorder_t = gettext.translation('aztrecorder', ...)
#   recorder_t.add_fallback(collab_i18n.gettext_translation())
#   azt_collab_client.set_translator(recorder_t.gettext)
# See the "Internationalization (i18n)" section above.

compat = azt_collab_client.check_server_compat()
# branch on compat['ok'] / compat['error'] for install / update UX.
# New error in 0.15.0+: 'client_too_old' — the server requires a
# newer client than this peer ships. Surface "Please update this app".
```

If the sister app embeds the daemon (i.e., has a sibling
`azt_collabd/` symlink), it should also call
`azt_collabd.configure(app_slug=..., client_id=..., collaborator=...)`
and, on Android,
`azt_collabd.android_cp.service.install_callbacks()` in its startup
hook (no-op on desktop).
