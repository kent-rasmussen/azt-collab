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
   `commits_ahead`, and the daemon touches the project as recent
   on every call.

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
above; the peer treats the typed error as the answer, not as a
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
`CLIENT_INTEGRATION.md` § 17.

### Peer contract: why auto-sync must be silent

> **The routing contract** (per-code rules + code shape) is in
> `CLIENT_INTEGRATION.md` § 17. This section is the *why*.

`sync_project` / `request_sync` results reach the peer from two
triggers and need different responses:

- **Auto-sync** (peer-initiated; project-select, post-edit
  debounce, background periodic) must be silent on
  configuration-class failures. The user is mid-flow doing
  something else; a popup or forced settings navigation derails
  that flow, sometimes visibly enough to look like project
  selection itself "failed."
- **User-initiated sync** (the user tapped Sync) IS the gesture
  and routes to whatever fixes the problem.

The daemon sees only one shape — `RPC: sync` — so the auto/user
distinction has to live peer-side as distinguishing methods.

**Pre-0.34.1 anti-pattern, closed by this contract.** Treating
every sync failure as a user-facing error in the auto-sync path
manifested as "I selected project B but ended up back on project
A": auto-sync on project-load returned `NOT_A_REPO`, the peer's
error path bailed the project-load flow mid-transition, and the
user landed back on the previously-displayed project. Silent
auto-sync failures keep the user in the project they actually
selected.

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

Peer with no own catalog: nothing to do — the default translator
is the client catalog. Peer with its own catalog chains via
gettext's `add_fallback`; the code shape is in
`CLIENT_INTEGRATION.md` § 6.

`translate.tr` has a software fallback in case the host forgets
`add_fallback`: if the host translator returns the msgid unchanged,
`tr` retries via the client catalog. So a misconfigured host still
gets client strings translated for KV-rendered text. The failure
mode becomes "missing translation for client string" instead of
"untranslated forever" — but rely on `add_fallback` for the
non-degraded path.

### Live retranslation

The daemon's settings UI calls `i18n.set_language(code)` and
rebuilds its ScreenManager so KV `text: _('...')` bindings
re-evaluate. Peers that want to live-retranslate while running
poll `$AZT_HOME/config.json` mtime on a 1-second `Clock`
interval and rebuild the relevant screens — pattern lives in
`azt_collabd/ui/picker_app.py:_check_language_change`. Peers
that skip the watcher just pick up the persisted language at
next launch via the auto-init at import.

### Adding strings / languages

- New translatable string: wrap in `_(...)` / `tr(...)`, add the
  msgid to each `locales/<lang>/LC_MESSAGES/azt_collab_client.po`.
  `_ensure_mo` recompiles `.mo` on mtime, so no `msgfmt` step.
- New language: create the `.po` (copy `fr/` as template), add the
  display name to `i18n._DISPLAY_NAMES` if BCP-47 doesn't already
  cover it. `available_languages()` discovers it on next call.

## LIFT-file access

> **For the conformity contract** — `LiftHandle` /
> `atomic_open_write` usage code — see `CLIENT_INTEGRATION.md`
> § 8. This section is the *why*.

The daemon owns the canonical copy of every project's LIFT file
under `$AZT_HOME/projects/<lang>/<file>.lift`. On Android the
daemon lives in the server APK and that path sits inside its
private `filesDir` — peer packages **cannot** `open()` it (sandbox
denies; `[Errno 2] No such file or directory` even when the file
exists, because the peer process has no UID-level read on the
server APK's filesDir). The provider URI is the only legitimate
read/write seam.

**Provider lifetime is stable across server kills.** The server
APK is pinned by a sticky-bound service (`AZTServiceProviderhost`),
so the URI grant the picker emits is reachable for as long as the
receiving Activity is alive — Android scopes the grant to the
receiver, not the source process. Under memory pressure Android
may still kill the host; the next peer ContentResolver call
auto-spawns it via the unconditional ContentProvider contract.
Detached FDs survive the source kill (kernel-managed inode). Peers
may safely defer `LiftHandle(uri).open_read()` to a later user
gesture, and audio FDs may be held across a long recording —
neither requires the picker to still be in view.

**Don't cache.** A peer-side cache (download → edit → push back)
breaks the single-source-of-truth promise. Two peers reading at
T0 and writing at T1 / T2 race; the later writer clobbers the
earlier writer's edits and the daemon commits + pushes the
corrupted state. Read and write through the provider every time;
`LiftHandle` is cheap.

**The one peer-visible recovery surface** is
`Result.has(S.JOB_INTERRUPTED)` from `request_sync` + `poll_job`
— transient, retryable; treat as `S.SERVER_UNAVAILABLE`.
Synchronous `sync_project` callers never see this code (the
transport's retry loop absorbs a dead binder mid-call).

**`atomic_open_write` vs. `open_write`.** Use
`atomic_open_write` for any LIFT save that may race a sync's
merge-output write or another peer; the wrapper uses
sibling-tempfile + `os.replace` on filesystem paths and the
daemon's two-phase FD + finalize protocol on URIs. Two concurrent
writes are safe: whichever rename runs last wins, and the
destination is always a complete copy of one version, never torn.
`open_write` is the older path-lock-only contract — fine for
same-process serialization, unsafe for cross-process races.

### Audio + image cross-package access

`AZTCollabProvider` serves sibling files under the same authority
as the LIFT URI:

```
content://org.atoznback.aztcollab/<lang>/audio/<basename>
content://org.atoznback.aztcollab/<lang>/images/<basename>
```

Provider auto-creates `audio/` and `images/` on first write
(whitelist `_ALLOWED_MEDIA_DIRS = ('audio', 'images')` in
`azt_collabd/android_cp/service.py:_resolve_path`). Both kinds are
read+write from peers; the picker's result-Intent grant flags
(`FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION`)
cover same-authority sibling URIs without per-file grants.

Client API: `MediaHandle(path_or_uri, kind='audio'|'image')` is a
`LiftHandle` subclass — the `kind` is a log-line label, not a
functional gate. `audio_uri_for(lift_path_or_uri, basename)` /
`image_uri_for(...)` compose the sibling URI / filesystem path so
callers stay blind to the path-vs-URI distinction.

No `list_audio` / `list_images` RPCs needed — both sets of
basenames are already encoded in the LIFT XML itself (audio in
`<citation><form>` audiolang text, images in `<illustration
href=…/>`).

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

CAWL repo selection is per-project, not daemon-global or
peer-global. Different projects can legitimately point at
different image sets (fork, culturally specific imagery, internal
mirror). Resolution: per-project override → daemon-global default
(`_CAWL_IMAGE_REPO_DEFAULT` in `azt_collabd/config.py`) → empty
(daemon serves `{}` / `FileNotFoundError` with no network call).
The cache layer is slug-keyed and doesn't know which level
resolved the slug — dedup is preserved across the configuration
surface.

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

Stage 1 (peer swaps index fetch for `cawl_index`) removes the
rate-limit symptom (the 403). It feels like the migration is done.
It isn't — Stage 2 (peer swaps binary fetches for `CAWLHandle`) is
where the architectural wins land: cross-peer dedup, surviving
peer uninstall, removing the per-peer 100–300 MB on-disk cost.
The contract says both stages because both are needed for the
architecture to be correct, not just for the surface symptom to
go away.

### Why daemon-driven prefetch + offline-aware (0.41.21)

The same "daemon owns CAWL state" rule applies to the *trigger*
for filling the cache, not just the cache itself. Pre-0.41.21 the
peer iterated its working-set on each project-load and POSTed
``cawl/prefetch`` — the daemon owned the bytes but the peer owned
the decision of which bytes to warm and when. Two failure modes
followed:

1. **Peer drift.** Each peer picks "one variant per CAWL id" with
   its own heuristic. Working-sets diverge across peers; the
   on-disk cache splinters; progress indicators read against an
   index-image-count baseline the working-set never matches.
2. **Offline boot spam.** Peer-driven iteration in the old per-
   image model had its own circuit breaker, but 0.41.11 moved
   the loop to the daemon's ``_prefetch_worker`` and the peer-
   side breaker silently stopped applying. An offline boot
   produced ~1700 ``[cawl] image fetch failed`` lines in
   ~40 ms intervals.

0.41.21 closes both: ``_touch_project`` calls
``cawl.auto_prefetch(repo)`` (throttled to once per repo per
30 s) which warms the full index in a background thread.
``_prefetch_worker`` gates on ``_has_internet()`` at start
(``skipped_offline=True``) and circuit-breaks after 3
consecutive failures (``circuit_open=True``). The
``cache_status`` HTTP response carries both flags so peers
render an "offline — will resume" badge instead of misleading
"0 / N" progress.

**Why the scheduler edge fires the retry.** The connectivity
watcher in ``scheduler._watcher_loop`` was already detecting
offline → online edges for sync drain. Hooking
``cawl.on_online_edge()`` into that same edge keeps the
"recover when network returns" logic in one place — single
authority for "did connectivity flip?", no second watcher
polling. Recovery latency = one ``connectivity_poll_s`` poll
interval, default 30 s.

**Why the peer keeps polling at 1 Hz even while offline.** Two
reasons. First, the auto-resume path needs the peer's banner
to observe the next ``cache_status`` response to flip from
"offline" to live progress; stopping the poll loses that
observation. Second, after the ``[first-try]`` probe
suppression for the cache_status path, the per-poll cost is
in-memory dict lookups on the daemon side and one
ContentResolver.call on the peer side — well under any
threshold worth optimising. The "keep polling" choice trades
a near-zero cost for a UX win.

**Why two-stage migration for the trigger (Stage A / Stage
B).** Same shape as the original CAWL Stage 1 / Stage 2: Stage
A is additive on the daemon (auto_prefetch runs, peer's
explicit ``cawl_prefetch`` still works, no breakage). Stage B
removes the peer's explicit call once Stage A is widely
deployed. Peers that strip their POST against a pre-0.41.21
daemon get a stuck "no prefetch ever fires" — the migration
checklist in ``CLIENT_INTEGRATION.md`` § 10 calls this out.

## Sync flow: commit / push split — architectural rationale

> **For the conformity contract** — peer-side migration,
> ``commit_project`` vs. ``sync_project`` routing, work-offline
> badge rendering — see ``CLIENT_INTEGRATION.md`` § 17b. This
> section is the *why*.

### Why split commit from push

Pre-0.43 the same RPC (``request_sync`` → ``_run_sync``) did
both halves. That conflation produced two distinct failure
modes:

1. **The bug from NOTES_TO_DAEMON.md (2026-05-15).** The
   debounced path early-returned ``COMMITTED_OFFLINE``
   when ``_has_internet()`` was False — *without ever calling
   the commit step*. A field session of swipes piled up dirty
   files; ``commits_ahead`` stayed at zero while ``n_changes``
   climbed. The synchronous ``sync_project`` path was unaffected
   because it didn't have the early return, so the Sync button
   "worked" while auto-sync silently dropped commits on the
   floor. The structural fix isn't "move the gate after the
   commit step" (a one-line repair); it's removing the implied
   coupling so the two concerns are gated independently.

2. **MB-burning eager push.** Even when the offline-skip bug
   wasn't biting, every debounced ``request_sync`` attempted a
   push. A user on metered data who tethered briefly to look up
   a phone number would burn part of their MB on every queued
   commit's push — they had no signal that a non-trivial
   network event was about to happen, and no toggle to stop it.

The 0.43.0 model treats them as the two concerns they are:

- **Commits are peer-driven.** The peer knows which edits
  cohere ("I just recorded an audio clip and updated the LIFT
  for the same entry — that's one logical change"). The
  debounce collapses bursts; the rest is "stage + commit".
  No network. ``commit_project`` is fire-and-forget from the
  peer's perspective and never blocks on connectivity.
- **Push is daemon-driven.** The daemon owns "are we online?"
  (via the watcher's cached state), "have we been online long
  enough that this isn't a flicker?" (post-online grace), and
  "did the user say they're paying for data right now?"
  (``sync.work_offline``). Peers don't reason about any of
  this — they read ``project_status.commits_ahead`` and
  ``project_status.work_offline`` to render a badge.

### Why the post-online grace exists

A user may flip cellular data on briefly for some other
purpose (look up directions, send one SMS). Without a grace
period the watcher's first tick after the online edge would
fire every queued push — exactly the MB-burning surprise we
want to avoid. The grace is a small social contract: "if
you're online for a sustained moment, we infer you intended
to be online and start pushing." Default 60 s; configurable
via ``sync.post_online_grace_s``.

The grace is **only** for the automatic drain. The user-
gestured Sync button bypasses the grace — pressing Sync IS
the gesture that says "yes I'm intentionally online now."

### Why work-offline is a daemon-wide bool

The toggle isn't per-project. A user who's on metered data
is on metered data for *all* projects they're touching this
session; a per-project toggle would have them flipping it
project-by-project before every edit, which defeats the
point. Daemon-wide also makes the UI obvious — one section
in the settings screen, one toggle, one piece of state to
reason about.

Peer-side badges read from ``project_status.work_offline``,
which carries the daemon-wide bool on every per-project
status response. The duplication is intentional: peers get
the badge without a second round-trip.

### Why the Sync button respects the toggle (and what to do instead)

The Sync button refuses with ``S.WORK_OFFLINE_ENABLED`` when
work-offline is on. The user-flow that fixes this matches
every other typed-refusal route in the suite: toast + open
the daemon settings screen where the toggle lives. Pressing
the toggle OFF fires an immediate drain server-side
(``drain_pushes_now()``), so the user's next gesture in the
sync settings is the one that pushes.

The alternative — "Sync button bypasses the toggle as a
manual escape hatch" — was considered and rejected: the user
already has that escape hatch (turn off the toggle). Making
Sync bypass would mean the toggle's effect is unobservable
from the user's perspective when they press Sync, which is a
confusing UX. Better to fail with a clear refusal and route
to the fix.

### Why ``sync_project`` keeps doing commit + push, not push-only

By the time the user taps Sync, ``commit_project`` has been
firing on every gesture, so there's normally nothing left to
commit. But the suite has many entry points (recorder, future
viewer, future tools), and not all of them necessarily call
``commit_project`` aggressively. A user-gestured "do everything
now" should be robust to a peer that fell behind on commits.
``sync_project`` keeping the combined commit+push behavior is
the belt-and-suspenders: ``commit_project`` is the primary
commit path, and ``sync_project`` is the rescue.

### Why we don't probe ``_has_internet`` on the hot path

The TCP probe to GitHub costs ~50–200 ms when online and
up to 6 s when offline (two 3-s timeouts, plus DNS slop).
Per-commit probing would make ``commit_project`` feel
sluggish at best and unusable at worst on a flaky cellular
connection. The scheduler watcher polls once per
``connectivity_poll_s`` (default 30 s) and parks the result
in ``_last_online_state``; ``is_online_cached()`` exposes
that bool to internal callers. The drain reads the cached
state, not a fresh probe. The user-gestured Sync button is
the one place where a fresh probe is acceptable — that's a
deliberate user action, and a 3-second wait is exactly what
they'd expect from "force a sync now."

## Stuck-commit retry — architectural rationale

> **For the conformity contract** — peer-side handling of
> ``COMMIT_REPEATEDLY_FAILED`` and the diagnostic-only
> ``ProjectStatus`` fields — see ``CLIENT_INTEGRATION.md``
> §§ 17 + 17a. This section is the *why*.

### Why scheduler-driven retry alongside the auto-sync surface

The peer-driven path was sufficient to *report* the alarm —
each call to ``sync_project`` / ``request_sync`` already
iterates ``result.statuses`` and would catch
``COMMIT_REPEATEDLY_FAILED`` whenever the counter hit 2 on a
peer-initiated attempt. What the peer-driven path can't do is
*recover* from a transient cause: if commit fails at T0 and the
user doesn't gesture again until T+5 min, the broken state
persists silently for those 5 min and accumulates more uncomm-
itted recordings. The scheduler retry runs in the background
between user gestures so the daemon takes a second look on its
own clock.

The counter is shared between the two surfaces. Both increment
on failure and clear on success via the same helpers in
``repo.py``. Realistic timelines:

- *Transient cause, scheduler succeeds.* Peer commit fails →
  count=1. Scheduler retry 30 s later → succeeds → count=0.
  Next peer commit → succeeds. User never sees the alarm,
  correctly — there's nothing to alarm about.
- *Persistent cause.* Peer commit fails → count=1. Scheduler
  retry → fails → count=2 (alarm lands in the background
  result, logged not transmitted). Peer commits next → fails
  → count=3, ``COMMIT_REPEATEDLY_FAILED`` lands on the peer's
  result. Toast fires.

Compared to no scheduler retry, the user-visible alarm timing
in the persistent case is the same (next peer gesture). The
scheduler retry's value is solely in the transient-recovery
case — it lets the daemon clear a stuck state without bugging
the user, so we don't raise false alarms on issues that
self-heal.

### Why no separate peer-side poll surface

An earlier draft of this feature also asked peers to poll
``project_status`` and synthesize the alarm off
``commit_failure_count >= 2``, so a foregrounded-idle peer
would alarm without a gesture. That requirement is gone — the
counter persists between gestures, so the very next peer-driven
sync naturally carries the alarm. The polling layer was a
second source of truth for the same fact (was the count ≥ 2
when we last polled?) plus a peer-side de-duplication state to
keep the 1 Hz poll from re-popping the toast every tick. The
marginal UX gain ("see the alarm while idle and foregrounded")
didn't justify the second-source-of-truth complexity. The
``ProjectStatus`` fields stayed for diagnostic surfaces (a
settings screen showing "last commit error: ...") but the
alarm flows through ``result.statuses`` only.

### Why exponential backoff (not fixed 1s retry)

The first draft was "wait 1s and retry inside the same
``commit_audio_and_sync`` call". Two problems:

1. **Transient causes are rare in dulwich.** ``porcelain.commit``
   essentially only raises on persistent conditions (index
   corruption, refs problem, disk full, broken repo state). A
   wait-and-retry-once inside the same call would catch the
   *vanishingly rare* "index briefly locked by a concurrent
   read" case at the cost of doubling commit latency on every
   actual failure. The same-call latency hit is paid every
   time; the rescue benefit applies maybe once a year.

2. **The retry surface needs to outlive the calling RPC.** The
   real problem isn't "this commit took 1.1 s instead of 1.0 s";
   it's "this commit failed and no future commit was even
   attempted." A second-attempt mechanism that lives inside the
   originating RPC can't catch a peer that crashed mid-flight,
   or a daemon process that respawned (sticky-bound service
   killed by OOM, picker-Activity Python teardown). The scheduler
   already owns the cross-RPC lifetime; that's the right home.

Doubling backoff (30 s, 60 s, 120 s, … capped at 1 hour) gives
fast retries for the easy cases (file briefly locked, transient
disk issue) and tapers to "once an hour" for a genuinely-stuck
repo — enough to catch self-healing (disk freed, lock
released, daemon restarted by user) without spamming the log
forever.

### Why the threshold is 2, not 1 or 3

One failure could be a fluke. Three would be *very* sure but
delays the user-visible alarm by another poll cycle — the
recorder may have written hundreds more files by then. Two is
the smallest count that excludes one-shot transients while
still firing the alarm fast enough to matter. Same rationale
as the daemon-side threshold; the peer-side polling shape uses
the same number for the same reason.

### What this doesn't catch

A daemon that's dead (no process, no scheduler ticks) won't
retry stuck commits. That's fine — the next peer RPC
lazy-spawns the daemon (sticky-bound service contract), and
the spawn runs `scheduler.reconcile_on_startup()` to mark
in-flight jobs as `JOB_INTERRUPTED`. The drain then resumes on
the next watcher tick. The retry loop assumes the daemon is
alive; daemon-resurrection lives elsewhere.

## Commit identity — architectural rationale

> **For the conformity contract** — `set_contributor` /
> `set_device_name` API + refusal-status handling — see
> `CLIENT_INTEGRATION.md` § 12. This section is the *why*.

The git commit author identity has two slots used as:

- NAME = the user's display name verbatim. GitHub groups commits
  by NAME, so one person committing from multiple devices appears
  as one author in the project's contributor list.
- EMAIL = `<safe_name>@<safe_device>`. `git log --format='%ae'`
  differentiates the same human's commits across phone / tablet /
  laptop. The email is non-routable; it's an identifier.

**Why two fields, not one composed string.** "Marie Dubois
(tablet)" would seem simpler but conflates two things — GitHub's
author-aggregation can't group by "Marie Dubois" if some commits
arrive as "Marie Dubois (tablet)" and others as "Marie Dubois
(laptop)". Splitting into NAME and EMAIL leverages git's native
distinction; correct GitHub UX is worth two store fields.

**Why daemon-owned, no peer pass-through.** Pre-0.40 the
contributor name lived in two places — peers passed it on every
sync/init RPC, and the daemon also kept a stored fallback. The
peer's pass-through won by default. That meant a user who typed
their name in the daemon UI but had a peer hard-coding a
placeholder got commits attributed to the placeholder anyway,
with no visible cause. 0.40 removes the wire surface entirely
and forces unset state to surface explicitly as
`S.CONTRIBUTOR_UNSET` rather than silently substituting. Same
sole-authoritative-source rule as the rest of the per-user state
in `NOTES_TO_DAEMON.md`.

**Why device_name auto-populates.** Reading
`Settings.Global.DEVICE_NAME` (Android) or `socket.gethostname()`
(desktop) on first read gives a useful default without forcing
the user through settings on day one — the OS value is at least
diagnosable (user named the phone, or the manufacturer slug like
`"SM-T580"`). User can override; empty stored value re-triggers
detection on next read, a "reset to OS default" affordance.

**Why no `@unknown` fallback in production.** The `unknown-device`
last-resort is the explicit "nothing worked" sentinel for the
rare case where all autodetect probes fail (de-Googled Android,
chroot without `socket.gethostname`). It's *visibly* a
placeholder — same philosophy as removing the `'Recorder'`
literal: if the system can't identify the device, the commit
author should make that obvious, not pretend.

## UI submodule (`azt_collab_client.ui`)

Shared Kivy screens (`LangPickerScreen`, `ProjectPickerScreen`) and
helpers (`clone_url_popup`). Peers register these into their own
`ScreenManager`. Translations route through
`azt_collab_client.translate`; a peer with its own catalog calls
`set_translator(...)` once at startup. Don't add Kivy imports
outside `ui/` (hard rule #4).

### Shared assets — client-first model

Anything *shared in shape* across the suite (gear, sync, share)
lives at `azt_collab_client/ui/assets/icons/<name>.png` so every
peer that imports the client gets it for free. Resolve via
`icon_path('gear')` — returns an absolute path. Standalone
subprocesses (picker, settings UI) and peers can't use relative
paths; their cwd isn't the host's repo.

Peer-specific icons (peer's own app-icon variants, single-consumer
UX-specific icons) stay in the peer. Peers that want to override a
shared icon pass an explicit override path to the consumer that
takes one (e.g. `register_picker_kv(gear_icon=...)`); there is no
implicit cwd-based search.

When in doubt, **default to client-first**. It's easier to
override locally later than to deduplicate parallel copies once
they've drifted. The client-first rule applies to *new*
shared-shape assets and to any asset whose move-to-shared is
forced by a second consumer.

### Share helpers — `share.py`

> **For the conformity contract** — helper signatures —
> see `CLIENT_INTEGRATION.md` § 14b. This section is the *why*.

**Why centralised.** Each share dispatch is ~30 lines of jnius
autoclass + Intent construction + error translation. Multiple
surfaces (daemon UI log share, peer diagnostic share, future
viewer) would otherwise re-derive the same code; a peer-side
divergence breaks the share flow on that peer alone, which is
hard to notice until a user reports "share button does nothing."

**Why ACTION_SEND vs. ACTION_SENDTO.** `ACTION_SEND` opens the
generic share sheet — every text-handling app accepts.
`ACTION_SENDTO` with a `mailto:` URI scopes the picker to email
apps only. The "Email log" button uses the latter because the
user's intent is specifically "send to a developer"; the generic
share sheet would clutter with non-email targets to navigate past.

**Size constraints, not enforced here.** Android Intent extras
have a practical ~1 MB ceiling per transaction; callers sharing
> 256 KB should truncate first. Not asserted in the helper
because the helper doesn't know the caller's payload semantics
(truncate head? tail? sample?). The daemon-log producer
truncates at source (`_h_get_daemon_log`).

### Diagnostic-log capture — `daemon_log_to_file`

The daemon's stderr goes to logcat by default. Logcat is
fine for local development but useless when a remote tester
reproduces a bug on a device the developer doesn't have
adb access to — the diagnostic output is lost.

``set_daemon_log_to_file(True)`` (POST
``/v1/logging/daemon_log_to_file``) flips a config toggle AND
installs a ``sys.stderr`` tee in the running daemon process.
Stderr now goes to BOTH logcat AND
``$AZT_HOME/daemon.log``. ``get_daemon_log()`` (GET
``/v1/logging/daemon_log``) returns the file contents, the
current toggle state, and the file path.

**Hot-toggle.** The original draft of this feature required a
daemon restart for the tee to take effect. That's the wrong
shape when the user just enabled the toggle because they
want to capture the *next* event — they don't want to also
restart the daemon and lose the state they were
investigating. Module-level state
(``_stderr_tee_installed``, ``_stderr_tee_original``,
``_stderr_tee_file``) holds the swap so the toggle can flip
either way without restart.

**Truncation policy.** ``open(path, 'w')`` truncates on each
install — one log file per "session" the user opens. Trade:
shorter capture window vs. file-size growth without bounds.
Testers typically want "the log around the crash I just had",
which is the truncate-on-enable shape. If a longer history
becomes needed, the seam to add periodic rotation is the
file open in ``install_stderr_tee``.

**Off by default.** No daemon.log is created until the user
flips the toggle. Most installs never want this — it's a
diagnostic-only surface, opt-in.

### Self-update — `check_for_update`

`azt_collab_client.ui.check_for_update` is a reusable GitHub-
Releases-driven updater that every suite APK plugs into its
settings screen. Identity is fully parametric (`repo`,
`current_version`, `asset_filename`) so the same helper serves
the server APK and every peer — no duplicated download/install
plumbing per app.

**No SHA verification in v1.** TLS to GitHub plus Android's
signature-match install check (suite keystore enforced everywhere)
are the integrity layers. A future hardening pass can add a
`.sha256` companion asset if the release process publishes one.

### Bootstrap — `bootstrap()`

Suite invariant: **the user installs one APK** — the peer they
opened. The standalone server APK and any subsequent updates are
provisioned by the peer itself on first run. `bootstrap()` is the
single entry point that implements this; the call shape is in
`CLIENT_INTEGRATION.md` § 3.

**Why `on_done` only fires on the healthy path.** The
`server_unreachable` / `server_too_old` prompts are terminal: the
install popup is modal (`auto_dismiss=False`) and the user can't
reach the rest of the peer's UI until they install or quit; a
fresh bootstrap re-enters from the install-completion chain. So
if `on_done` fires, the daemon is reachable — peer code wired to
`on_done` doesn't need a defensive try/except for "daemon not
there yet." Before 0.28.5 this contract didn't hold and peers
hand-rolled defensive guards that obscured real bugs.

**Desktop hosts** call `on_done` immediately — there's no APK to
install, so bootstrap is a no-op outside Android.

## Low-power adaptive policy

> **For the conformity contract** — three rules, the gate-vs-
> don't-gate inventory, the multi-density splash + diagnostic
> logging recipe, verification steps — see
> `CLIENT_INTEGRATION.md` § 18. This section is the *why*.

### Why automatic, not user-toggleable

Devices in the field span flagships to 2–3 GB budgets. A
user-facing "low-power mode" toggle pushes the burden of
device introspection onto users who don't know (and
shouldn't have to learn) what `availMem / totalMem` means.
Android already classifies the device through
`ActivityManager.MemoryInfo.lowMemory`, `availMem`,
`totalMem`, and `ConnectivityManager.isActiveNetworkMetered()`.
Use those signals; let the user steer content and workflow.

The split — *resource decisions automatic, content/workflow
decisions user-facing* — falls out of one question:

> Is this about what the device CAN do, or about what the
> user WANTS?

Image cache size, prefetch eagerness, prewarm gating, poll
cadence: "can". Gloss-count display, sync-on-swipe: "wants".
Mixing the two means the user has to think about both, which
either crashes their budget phone or shows them controls they
shouldn't care about.

### Why build-time work belongs in the build

The anti-pattern we're explicitly rejecting: ship one
high-resolution asset and ask the device to downscale at
runtime. PIL-resize the presplash on first boot; regenerate
density buckets in `App.build`; recompile gettext `.mo` on
cold start. Each moves work onto the *least capable* devices
at the *worst possible moment* (splash screen, before Python
is warm; first-launch when the user is forming their first
impression).

The discipline:

> The build is the right place to do work that depends on
> the build artefact. The device is the right place to do
> work that depends on runtime state.

Density buckets, gettext compilation, CAWL pre-rendering —
all build-artefact-dependent. They belong in the build (or,
for CAWL, in the daemon, which is the suite's "build for
runtime data"). The device handles the runtime-state work:
which project is loaded, which language the user just
selected, which audio file was just recorded.

Same logic forbids "regenerate the CAWL cache from a tarball
on first launch" (the daemon already ships it, pre-rendered)
and "recompute the Charis SIL fallback on every cold start"
(the `.mo` files are pre-bundled). When a tempting
implementation has the shape "do build-artefact work at
runtime on each device", the cost is exactly the
distribution of work it implies — N devices × the per-device
cost — and the build is doing it once.

### Why `lowpower` helpers belong in the client, not each peer

The JNI plumbing for `ActivityManager.MemoryInfo` and
`ConnectivityManager.isActiveNetworkMetered()` plus the
thresholds (`< 0.15` of total memory, `≤ 3072 MB` total, etc.)
would drift between peer codebases if each peer re-derived
them. `azt_collab_client.lowpower` (shipped 0.41.21) is the
single source of truth: one tested jnius dance, one set of
threshold constants overridable per peer if field data
motivates. The diagnostic recipe (`identify_drawable_variant`
/ `log_presplash_variant`) also lives here so a future
correction (the kind that already happened to the first-pass
`Drawable.getIntrinsicWidth/Height()` and
`BitmapDrawable.getBitmap().getDensity()` recipes) ships in
one place rather than waiting for N peers to update
independently.

### Why we sized the suite's presplash baseline at mdpi 320×533

Android's resource resolver picks the bucket whose qualifier
matches the device's `densityDpi`. For physical-size
consistency across the suite, all peers + the server APK use
mdpi 320×533 as the 1.0× baseline; bucket sizes scale by
the standard Android factors (ldpi 0.75×, hdpi 1.5×,
xhdpi 2×, xxhdpi 3×, xxxhdpi 4×). xxhdpi (the most common
modern phone bucket) thus lands at 960×1599, matching the
diagnostic line peers log at startup. A suite-wide baseline
keeps splash visual sizing predictable across the recorder /
viewer / server-APK boundary.

## Project-switch reconciliation

> **For the conformity contract** — when peers MUST reload,
> exact ``on_resume`` shape, what comparison to make — see
> `CLIENT_INTEGRATION.md` § 14a. This section is the *why*.

The daemon owns ``last_project()`` (see "Daemon-owned state"
above). Any RPC path that mutates a project's identity in a
user-visible way — picker submission, future rename, future
delete-then-pick-next — writes the new langcode to
``$AZT_HOME/config.json :: recent.last_langcode`` server-side.
Peers polling ``last_project()`` get the authoritative answer.

What the daemon CANNOT do: push that change to the peer's
loaded UI. The peer's view is built from the LIFT bytes plus
peer-side caches (entry list, scroll position, open panels,
filter state); only the peer can tear that down and rebuild
against the new project's bytes. There's no Android channel
the daemon can use to invoke a method on a Kivy App that
happens to be in the background.

So the contract has to live peer-side. The peer's `on_resume`
is the natural hook: Android raises it whenever the peer
Activity comes back to the foreground after another Activity
(picker, daemon settings UI, other app) took focus. The peer
reads `last_project()`, compares to its in-memory
`_current_langcode`, and reloads if they differ. Same code
path as the initial project-load; just a different trigger.

### Why not poll on every cache_status tick

The cache_status banner already polls at 1 Hz. Adding "and
also reconcile project langcode" to that tick would work
mechanically, but the user-facing "switch happened" gesture
is bounded by Activity lifecycle — Android suspends the
peer when another Activity takes the foreground, resumes
when it leaves. Hooking lifecycle is cheaper than polling,
and the daemon-side state is consistent by the time
on_resume fires (the picker exit + last_project write are
both on the picker Activity's main thread, completing
before the picker finishes).

A misbehaving peer that polls works too, just with more
wakeups. on_resume is the *right* hook; polling is the
permissible degradation.

### Why the two-stage migration (peer contract first, daemon
button second)

Same shape as CAWL Stage A / Stage B. Until peers ship the
``on_resume`` hook, a daemon-side "Switch project" button
silently fails to take effect on resume — user lands back in
the previous project. Documenting the contract first lets
peer maintainers adopt the hook in their next release; the
daemon-side button lights up cleanly once enough peers are
on the new contract.

Peers that miss the contract get exactly the old behaviour —
the button is a no-op on resume but not destructive. The
daemon doesn't lose data; the user just sees the previous
project and learns to launch the picker the old way.

## Sister-app integration

See [`CLIENT_INTEGRATION.md`](CLIENT_INTEGRATION.md) — the single
contract every peer follows.
