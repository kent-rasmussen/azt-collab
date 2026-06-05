# CLAUDE.md — azt_collab_client

> **Scope of this file.** This is the **rules / invariants /
> hard contracts** for `azt_collab_client` — what the package
> owes the daemon, what the daemon owes the client, what the
> public API surface looks like, what *must not* be done
> inside this package. Reading just this file should be
> enough to avoid breaking things.
>
> **The peer conformity contract — what peers must do to be
> conformant — lives in `CLIENT_INTEGRATION.md` next to this
> file.** If you're a peer maintainer trying to figure out
> "what do I have to call, what do I have to honor, what's
> the migration checklist", read that file first.
>
> **Architectural rationale — the *why* behind each rule —
> lives in `docs/rationale/<topic>.md`.** Read those when you
> need to understand the historical context behind a rule, or
> when you're working inside a specific subsystem. The index
> at the bottom of this file lists every rationale file.

Guidance for Claude Code (claude.ai/code) when working with the
`azt_collab_client` package, including from sister apps that consume
it as a symlink (`../azt-collab/azt_collab_client` → `./azt_collab_client`).

This file is intentionally **self-contained for rules**: when this
directory is symlinked into a sister app, you should not need to
read the canonical `azt-collab/CLAUDE.md` to understand the client's
contract with the daemon. The rationale files (`docs/rationale/`)
travel along the same symlink.

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
   Corollary of (1), worth its own line because the failure mode
   is silent on desktop. On Android the daemon's working_dir lives
   in the server APK's private filesDir; peer processes have no
   UID-level read on it. Any `dulwich.Repo(working_dir)` /
   `os.path.exists(working_dir + '/.git')` / audio-dir walk silently
   returns false on Android regardless of actual state, so any peer
   flow gated on such a check (e.g. "only auto-sync if remote exists")
   silently skips the gated work. On desktop both processes share
   $AZT_HOME and the local check happens to work — easy to merge
   without noticing. Go through `project_status(langcode)` instead;
   it carries `remote_url`, `last_commit`, `last_sync`,
   `wan_unshared` / `lan_unshared` / `at_risk`, and the daemon
   touches the project as recent on every call.

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
   `azt_collab_client.i18n._`. Peers with their own catalog chain via
   gettext `add_fallback` (see `CLIENT_INTEGRATION.md` § 6 — and
   `docs/rationale/i18n.md` for why the old `tr()` retry was removed
   in 0.43.1).

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

8. **Daemon is the sole authoritative source for the state listed
   in "Daemon-owned state" below — no peer-side fallback.** Peers
   used to keep "just-in-case" mirrors (`peer_pref('vernlang')`,
   defunct `App.list_projects` that scanned the peer's own sandbox);
   those are gone, so a wrong/stale/empty daemon answer breaks the
   user-visible flow with no peer copy to recover from. Client
   wrappers surface daemon errors as typed errors (`SERVER_ERROR`,
   typed `Status`) — never synthesize values, never substitute
   placeholders. See the next section for the table and the four
   daemon-side obligations that flow from this.

9. **Rules live here; rationale lives in `docs/rationale/`.** This
   file is the contract; `docs/rationale/<topic>.md` is the *why*
   behind each rule. **Do not put rules into rationale files.** If a
   "do X / don't do Y" emerges from a why-discussion, it goes here
   (or into `CLIENT_INTEGRATION.md` if it's a peer contract); the
   rationale file then justifies it with a forward link. Reading
   just CLAUDE.md must remain sufficient to avoid breaking things —
   the rationale files exist so an engineer working inside one
   subsystem has the historical context, not so rules can hide
   there. The index at the bottom of this file lists every rationale
   file.

## Daemon-owned state

The daemon owns every field below. The client wraps the
endpoint, decodes into a typed shape, and surfaces errors —
nothing more. If a getter returns empty, the peer treats it as
"user hasn't set it", so a daemon that silently returns empty
on transient I/O failure manifests as user-visible data loss.

| Field | Endpoint(s) | What breaks on wrong/empty |
|---|---|---|
| Project langcode (== LIFT vernlang) | `last_project`, `open_project`, `register_project`, `derive_langcode`, `project_status` | LIFT writes use the wrong `lang=` attribute; `progress_text` reads the wrong field; audio filenames are mis-tagged. |
| Recent project (`last_project`) | `GET/POST /v1/recent/last_project` | Auto-resume on startup either skips a valid project or resumes a wrong one. |
| Contributor name | `get_contributor` / `set_contributor` | Commit-issuing endpoints refuse with `CONTRIBUTOR_UNSET`; sync / init blocked. Strict daemon-owned since 0.40.0 (peers no longer pass it on the wire). |
| Device name (commit author disambiguator) | `GET/POST /v1/config/device_name` | Git commit author email slot falls back to `@unknown` instead of disambiguating multi-device commits. Auto-populates from OS on first read; user-overridable via daemon settings UI. Since 0.40.0. |
| UI language | `azt_collab_client.i18n.current_language()` / `set_language()` | UI lands on the wrong locale on every launch — no peer-side cache. |
| Credentials (GitHub/GitLab/host) | `/v1/credentials/*` | Publish/sync silently fails; the peer cannot fall back to a local token store. |
| Project registry (`working_dir`, `lift_path`, `remote_url`) | `list_projects`, `open_project`, `register_project` | Picker can't find the project; publish has no working_dir to push from. |
| Repo slug (per-project override) | `Project.repo_slug` via `open_project` / `list_projects` / `project_status`; setter `POST /v1/projects/<lang>/repo_slug` | Override silently degrades to using `langcode` as the repo name. Since 0.39.0. |
| CAWL image_repo (per-project) | `Project.cawl_image_repo` via `open_project` / `list_projects` / `project_status`; setter `POST /v1/projects/<lang>/cawl_image_repo` | Per-project image-set override silently degrades to the daemon-global default. Since 0.38.0. |
| Work-offline mode (daemon-wide) | `get_work_offline` / `set_work_offline` (`/v1/config/work_offline`); also mirrored on `project_status.work_offline` | Push gets suppressed silently or run unintentionally on metered data. Since 0.43.0. |
| LAN peer identity (per-device ed25519 keypair + X.509 cert + sha256 fingerprint) | `lan_peer_id` (`GET /v1/lan/peer_id`) | Pairing QR can't be generated; peers can't authenticate against this daemon's LAN listener. **Since 0.50.9 the daemon eager-inits this on startup** (was: lazy on first LAN op) so slot claims always have a stable identity to anchor on. Persisted as `$AZT_HOME/peer_id` + `$AZT_HOME/peer.crt`; survives daemon respawn but NOT an app-data wipe. Builds without the `cryptography` package fall back to empty peer_id with a logged warning. Since 0.45.0, eager-init since 0.50.9. |
| Paired peers (`peers.json`) | `lan_list_peers`, `lan_pair_accept`, `lan_share_project`, `lan_unshare_project`, `lan_unpair`, `lan_set_static_endpoints` | Listener accepts a previously-paired phone or rejects with 403; share allowlist determines which projects each peer can fetch; static endpoints carry the hotspot-host fallback. Since 0.45.0. |
| LAN-sync toggle (daemon-wide) | `lan_toggle` / `lan_set_toggle` (`GET/POST /v1/lan/toggle`) | Listener thread + (Android) FGS promotion + WifiLock are hot-applied to this bit. Wrong value: LAN sync silently doesn't happen, or the FGS notification stays up draining battery. Since 0.45.0. |

### Daemon obligations (load-bearing)

These four invariants must hold on every release. They're not
defensive nice-to-haves — the peer no longer has fallbacks for
when they fail.

1. **No silent empty.** If a getter can't answer (server
   starting, transient I/O failure), return a clear error — not
   an empty string. The peer reads empty as "user hasn't set
   it" and degrades accordingly.

2. **Setter durability.** Every setter that writes to
   `$AZT_HOME/config.json` (or its Android-CP equivalent) must
   land on disk before returning OK. Crash-during-write that
   loses the value surfaces as user-visible data loss.

3. **Project-langcode immutability without a rename RPC.** Peers
   cache the langcode in-memory as `_current_langcode` for the
   life of the load. If the daemon changes a project's langcode
   under a loaded peer (e.g. during a merge), the peer's in-
   memory copy goes stale and future writes go to the old tag.
   If rename ever ships, surface it through `rename_project` and
   notify open peers (or at minimum make the next
   `project_status` reflect the new value so the peer can
   refresh on its periodic poll).

4. **Cross-peer convergence.** Setters from one peer must be
   visible to every other peer's getter within "next RPC" time.
   The Android ContentProvider gives us this for free today;
   flagging so a future daemon refactor doesn't accidentally
   introduce a per-process cache that breaks it.

### When adding a new daemon-owned field

Default placement: daemon-side, accessed by RPC each time. Do
not invite the peer to cache it. If you add a new row to the
table above, the client wrapper translates transport failure
into a typed `Result` per the rules in "Public API surface"
below; the peer treats the typed error as the answer, not as a
prompt to synthesize.

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

## Public API surface

The full surface peers call into is enumerated in
`CLIENT_INTEGRATION.md` (§§ 3–17). For someone working inside the
client, two design rules apply to every wrapper added to
`azt_collab_client/__init__.py`:

- **Translate transport failure into a typed return.** Ops that
  nominally return `Result` get a `Result` carrying
  `Status('SERVER_UNAVAILABLE'|'SERVER_ERROR', …)`. Ops that return
  data get the empty/None equivalent. Never let `ServerUnavailable`
  reach the caller from a wrapper (except `rpc.call` itself).
- **Never raise from a query-shaped wrapper.** UI must be able to
  call `list_projects()` offline and get `[]`, not an exception.

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

## Status codes

`azt_collab_client.status` is a **mirror**, not a re-export, of
`azt_collabd/status.py` — adding a code requires editing both, so
the client doesn't depend on the daemon package (hard rule #3).
Wire format is `{'code': 'PUSHED', 'params': {...}}` per status;
`Result.has(code)` / `Result.has_any(*codes)` / `Result.codes()`
drive logic. Translation lives in `translate.py` and is the
*display* path — never substring-match on translated text;
translations change per locale, codes don't.

The per-code routing table and constant reference live in
`CLIENT_INTEGRATION.md` § 17. The auto-sync vs. user-sync
silence contract — and why it matters — lives in
`docs/rationale/sync.md`.

## Sister-app integration

See [`CLIENT_INTEGRATION.md`](CLIENT_INTEGRATION.md) — the single
contract every peer follows.

## Where to find rationale (the *why*)

Rules live in this file; rationale lives in `docs/rationale/`.
Each file is the historical context behind one subsystem's rules;
none of them contain rules themselves (per hard rule #9).

- [`docs/rationale/sync.md`](docs/rationale/sync.md) — commit/push
  split, stuck-commit retry, auto-sync silence routing.
- [`docs/rationale/lift_access.md`](docs/rationale/lift_access.md)
  — LIFT file + audio + image cross-package access;
  `atomic_open_write` semantics; surgical field-write RPCs
  (`set_audio` / `set_illustration`, the why behind the parallel
  write path for low-memory devices).
- [`docs/rationale/cawl.md`](docs/rationale/cawl.md) — CAWL image
  cache, suite-scoped daemon ownership, per-project image_repo.
- [`docs/rationale/i18n.md`](docs/rationale/i18n.md) — gettext
  catalog, auto-init, `add_fallback` chain, live retranslation.
- [`docs/rationale/identity.md`](docs/rationale/identity.md) —
  contributor + device_name (commit author identity).
- [`docs/rationale/ui.md`](docs/rationale/ui.md) — UI submodule,
  shared assets, share helpers, daemon-log capture, self-update,
  bootstrap.
- [`docs/rationale/lowpower.md`](docs/rationale/lowpower.md) —
  automatic device tiering, build-time vs runtime work, presplash
  baseline.
- [`docs/rationale/project_switch.md`](docs/rationale/project_switch.md)
  — `on_resume` reconciliation; why no daemon push.
