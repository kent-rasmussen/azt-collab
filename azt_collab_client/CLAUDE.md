# CLAUDE.md — azt_collab_client

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
- a translator hook (`translate.set_translator`)
- a shared Kivy picker UI (`azt_collab_client.ui`)

## Hard rules

1. **No dulwich, no git operations.** The daemon is the only thing
   that touches a repo. Clients write files into the working tree (or
   stream through the Android ContentProvider) and ask the daemon to
   commit / sync. If you find yourself reaching for `dulwich` or
   `subprocess(['git', ...])` in this package, stop — add (or use) a
   server endpoint instead.

2. **No `azt_collabd` import.** This package must keep working when
   the daemon is running in a separate process or a different APK.
   `paths.py`, `status.py`, and `projects.py` are duplicated on
   purpose so the client doesn't depend on the server package.

3. **No Kivy at import time at the package root.** `azt_collab_client.ui`
   imports Kivy; `azt_collab_client` itself does not. Sister apps may
   import the top-level module from non-Kivy contexts (CLI helpers,
   tests). Keep it that way — guard any Kivy imports inside `ui/` or
   inside functions that are only called from a Kivy host.

4. **Structured `Result`s drive logic; translated text is for humans.**
   Use `result.has(S.PUSHED)` / `result.has_any(S.AUTH_REQUIRED, ...)`
   — never substring-match on translated strings. `S` is
   `azt_collab_client.status` (re-exported as `S` from the package
   root). Translation lives in `translate.py` and runs through
   whatever callable was last passed to `set_translator(fn)`; the
   default tries `from i18n import _` (recorder's translator) and
   falls back to identity.

5. **Two `configure()` calls in the suite, both keyword-only and
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
  `init_project(working_dir, remote_url, branch='main', contributor=...)` → `Result`,
  `create_project_from_template(vernlang, dest_dir, template_url='')`,
  `clone_project(remote_url, dest_dir, on_progress=None, ...)` (synchronous;
  use `clone_project_start` + `clone_project_status` for a
  Clock-driven progress loop),
  `project_status(langcode)` → `ProjectStatus | None`.
- **Sync.** `sync_project(langcode, contributor)` blocks; returns
  `Result`. `request_sync(langcode, contributor)` is fire-and-forget
  (returns a `job_id`, debounced server-side). `poll_job(job_id)`
  returns `{state, result, ...}` where `state` is
  `'PENDING' | 'RUNNING' | 'DONE'`.
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

## UI submodule (`azt_collab_client.ui`)

Shared Kivy screens (`LangPickerScreen`, `ProjectPickerScreen`) and
helpers (`clone_url_popup`). Sister apps register these into their
own `ScreenManager`. Translations route through
`azt_collab_client.translate` — call `set_translator(...)` once at
startup if your host has its own i18n module (the recorder does).

Don't add Kivy imports outside `ui/`; see hard rule #3.

## Sister-app integration recap

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
azt_collab_client.set_translator(your_i18n._)   # if you have one
compat = azt_collab_client.check_server_compat()
# branch on compat['ok'] / compat['error'] for install / update UX
```

If the sister app embeds the daemon (i.e., has a sibling
`azt_collabd/` symlink), it should also call
`azt_collabd.configure(app_slug=..., client_id=..., collaborator=...)`
and, on Android,
`azt_collabd.android_cp.service.install_callbacks()` in its startup
hook (no-op on desktop).
