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
   **default** is the client-owned catalog at
   `azt_collab_client.i18n._`, and `tr()` falls back to that catalog
   when a host translator returns the msgid unchanged. See the
   *Internationalization* section below.

5. **The client owns its own translatable strings.** Anything in
   `picker.py` / `popups.py` / `langpicker.py` / `translate.py` /
   the daemon's `ui/app.py` that's user-visible goes in
   `azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po`.
   Don't expect a host catalog (e.g. `aztrecorder.po`) to carry these;
   the recorder is one consumer of the client among several.

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
to ours:

```python
import gettext
import azt_collab_client
from azt_collab_client import i18n as collab_i18n

def set_recorder_language(lang):
    if lang == 'en':
        recorder_t = gettext.NullTranslations()
    else:
        recorder_t = gettext.translation(
            'aztrecorder', localedir=RECORDER_LOCALES, languages=[lang],
            fallback=True)
    collab_i18n.set_language(lang)             # client catalog + persist
    recorder_t.add_fallback(collab_i18n.gettext_translation())
    azt_collab_client.set_translator(recorder_t.gettext)
```

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

Provider auto-creates `audio/` on first write (`openFile(mode='w')`
mkdir-p's the parent). Image writes from peers are not supported —
the daemon owns image additions; peers only read. The picker's
result-Intent grant flags
(`FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION`)
cover same-authority sibling URIs without per-file grants.

**Client side (`azt_collab_client.lift_io`):**

- `MediaHandle(path_or_uri, kind='audio'|'image')` — `LiftHandle`
  subclass with a `kind` field. `open_write()` raises
  `PermissionError` for `kind='image'` (the architectural rule:
  daemon owns image additions). Read-side identical to `LiftHandle`.
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
- Image *additions* still gate off on URI projects — the URL/cache
  fallback in `lift.py` keeps the displayed image rendering, only
  the local-copy-into-`images/` step is skipped because the daemon
  owns adds.

**Discovery:** no `list_audio` / `list_images` RPCs needed — both
sets of basenames are already encoded in the LIFT XML (audio in
`<citation><form>` audiolang text, images in
`<illustration href=…/>`). If a future admin UI wants directory
listing, add `/v1/projects/<lang>/list_images` separately.

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
