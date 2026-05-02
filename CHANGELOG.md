# Changelog

Two packages live here. Versions move together for now (the client
embeds `MIN_SERVER_VERSION`, so when the wire format changes we bump
both); patch-level bumps in one without the other are fine.

- **azt_collabd** — server / daemon. Source of truth: `azt_collabd.__version__` (re-imported by `server.py` as `_VERSION` for the wire response).
- **azt_collab_client** — client library. Source of truth: `azt_collab_client.__version__`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely.

## [Unreleased]

### Suite naming convention
- Adopted a single rule for surfacing the "azt collab" / "azt
  recorder" names across systems with incompatible identifier rules
  (Python identifiers forbid `-`, Android package segments forbid
  `-`, GitHub App slugs forbid `_`). Documented in `CLAUDE.md`. Net:
  `_` is the default for code identifiers, env vars, repo dirs, and
  Python packages; the dropped form (`aztcollab`) is the Android
  package; the hyphenated form (`azt-collaboration`) is the GitHub
  App slug; the human-facing form is "AZT Collaboration" (same word
  in French and English, which keeps i18n natural for the SIL user
  base). The internal token "collab" stays in code (`azt_collabd`,
  `AZT_COLLAB_ACCESS`, `org.atoznback.aztcollab`) — only the
  human-facing surfaces and the GitHub App slug expand to
  "Collaboration" / "azt-collaboration".
- Default `app_slug` in `azt_collabd/config.py` is now
  `azt-collaboration` (was `azt-recorder`, an artifact of the
  recorder's earlier ownership of the GitHub App). Override via
  `AZT_GITHUB_APP_SLUG=` if your suite ships under a different
  registration. The same default is mirrored in
  `server_apk/main.py` and the README env-var table.
- Buildozer title ("AZT Collaboration") and prose mentions ("AZT
  Collaboration service") capitalized consistently across the tree.

### Android manifest assembly
- **Bug fix:** `buildozer.spec`'s manifest-extras key is
  `android.extra_manifest_xml`, NOT `android.manifest_extra_xml`
  (different word order). Buildozer silently ignores unknown keys,
  so before this fix every peer's extras file was being dropped on
  the floor — including the recorder's `<queries>` block, which on
  Android 11+ is required for the client's `discover()` probe to
  even see the server APK.
- New canonical peer manifest extras at
  `android/manifest_extras_peer.xml` (just the suite `<queries>`
  block; meant to be symlinked into each peer as
  `manifest_extras.xml` and referenced from buildozer.spec).
- `server_apk/manifest_extras.xml` rewritten to top-level-only
  content (just the signature `<permission>`). p4a's
  `extra_manifest_xml` injects at top level under `<manifest>`,
  before `<application>` — `<application>` wrappers there end up as
  duplicate-application errors.
- New `before_apk_assemble` step in
  `buildozer_tweaks/p4a_hook.py`:
  `_inject_aztcollab_provider`. Patches the rendered
  `AndroidManifest.xml` post-template-render to inject the
  `<provider>` declaration inside `<application>`. Gated on
  `dist_name == 'aztcollab'` so peer APKs don't accidentally
  inherit the provider declaration. (p4a's SDL2 bootstrap manifest
  template only exposes top-level injection, so the provider — which
  must live inside `<application>` — has no spec-level injection
  point.)

### Build infrastructure (NDK r29 compatibility)
- New local recipe override `recipes/sdl2_ttf/__init__.py` in
  `buildozer_tweaks/`: patches harfbuzz's `Android.mk` to add
  `-DHB_NO_PRAGMA_GCC_DIAGNOSTIC_ERROR -Wno-error=cast-function-type-strict`.
  SDL2_ttf 2.20.2 ships an old harfbuzz whose `hb.hh` promotes
  `-Wcast-function-type` to error via `#pragma GCC diagnostic`, and
  NDK r29's clang lumps the `-strict` variant into that group;
  `hb-ft.cc`'s `(FT_Generic_Finalizer)` casts then fail.
- New `recipes/kivy/__init__.py`: (a) gates kivy's
  `merge(flags, sdl2_flags)` on `not kivy_sdl2_path` so host
  pkg-config doesn't leak `-I/usr/include/SDL2` (and via that
  `sys/cdefs.h`) into the cross-compile; (b) adds
  `-Wno-error=incompatible-function-pointer-types` to CFLAGS so
  kivy 2.3.0's `cgl_gl.c` glShaderSource const-mismatch doesn't
  fail the build.
- All build patches now run from `prebuild_arch` / `before_apk_*`
  hooks — the previous `before_apk_build` placement of the harfbuzz
  and kivy patches in `p4a_hook.py` was dead code (it fires after
  `build_recipes`, way too late). Those legacy `_patch_*` functions
  in the hook are kept as harmless no-ops; safe to remove on a
  future cleanup.

### Server APK packaging
- New `server_apk/setup.sh`: idempotent symlink creator for
  `azt_collabd` and `azt_collab_client` from the parent repo into
  the server APK source dir. Run once after a fresh checkout so
  buildozer can find the daemon code. Replaces the dangling
  `../setup_from_nuke.sh` reference that used to live in
  `main.py`'s comments.
- `server_apk/buildozer.spec` now points at the proper
  `extra_manifest_xml` key and includes icon assets
  (`icon.filename`, `icon.adaptive_*`) so the launcher icon isn't
  the default Kivy logo.

### azt_collabd 0.10.3 — manifest dual-patch + on-device verification

- `_inject_aztcollab_provider` and `_inject_aztcollab_pick_intent`
  in `buildozer_tweaks/p4a_hook.py` now patch BOTH
  `AndroidManifest.xml` (the dist root) AND
  `src/main/AndroidManifest.xml` (the file gradle's default
  sourceSets actually reads). Previously patched only the dist
  root, so gradle ran against the unpatched copy and the resulting
  APK had no `<provider>` despite the dist-root manifest on disk
  looking correct. Symptom: dumpsys showed no provider yet
  `aapt dump xmltree` of the *dist root* manifest confirmed it —
  diverging because gradle's input was a different file.
- New `server_apk/test_install.sh` — 15-check on-device
  verification of the server APK: install, `<permission>`
  declaration, signature self-grant, `<provider>` registration
  (multi-source: per-package dumpsys, system-wide provider table,
  `pm dump`), direct `content query`, bundled
  `azt_collabd`/`azt_collab_client` Python modules, launcher icon
  vs. default Kivy logo, activity launches without crash, source
  symlinks, dist manifest sentinel, hook traces, installed-vs-bin
  APK md5 match, APK's own manifest, all dist manifests' patch
  status, gradle manifest config.
- New `azt_collab_client/test_peer.sh` — peer-side verification:
  walks each `org.atoznback.*` package on the device, confirms
  each requests `AZT_COLLAB_ACCESS`, was granted (signature match),
  declares the suite `<queries>` block, and signs against the
  fingerprint in `android/SUITE_FINGERPRINT`.

### azt_collabd 0.10.1 — naming default + build/manifest plumbing
- Default `_SLUG_DEFAULT` in `config.py` flipped from `'azt-recorder'`
  to `'azt-collaboration'` to match the renamed GitHub App slug.
  Same default mirrored in `server_apk/main.py`.
- All cross-cutting build / manifest / naming work above
  (NDK r29 patches, manifest assembly fix,
  `_inject_aztcollab_provider` hook, `setup.sh`) lands in this
  patch level — no daemon API change, but the server APK packaging
  pipeline becomes reliable for the first time.

### azt_collabd 0.10.0 — picker helper subprocess
- New `python -m azt_collabd projects` entry point in `__main__.py`.
  Runs `azt_collabd.ui.picker_app.PickerApp`, a single-purpose Kivy
  app that hosts the shared `ProjectPickerScreen` +
  `LangPickerScreen` and implements the create-flow callbacks
  (`open_file` / `clone_dialog` / `show_start_over` /
  `new_from_template`) internally. Every successful flow ends in
  `_emit_and_quit(path, langcode='')`, which writes
  `AZT_PICK\t<path>\t<langcode>\n` on stdout and exits 0
  (or sets the Activity result on Android — see server APK below).
  Cancel / window-close exits 1.
- New `azt_collabd/ui/picker_app.py`. Hides the gear icon on the
  shared picker (no settings of its own), mounts the langpicker for
  Start New, drives `clone_project` and `create_project_from_template`
  on worker threads, surfaces errors via an in-window modal overlay
  (window stays open for retry).
- `azt_collabd/ui/app.py` (settings UI) trimmed: no more "Projects"
  NavBar button, no `ProjectPickerScreen` mount, no host-contract
  stubs. App now uses `_AZT_ICON` from `azt_collab_client/azt.png`.
- `server_apk/main.py` reads the launching Intent action; if it's
  `org.atoznback.aztcollab.PICK_PROJECT`, mounts the picker app
  instead of the settings UI. Same `PythonActivity` handles both —
  no second Activity declaration in the manifest required (the
  Intent action is matched by the existing PythonActivity entry +
  the new `<intent-filter>` line described in
  `azt_recorder/docs/p4a_hook_picker_intent.diff`).

### azt_collab_client 0.13.1 — picker resilience
- `_pick_project_android`: pre-check Intent resolvability via
  `PackageManager.resolveActivity(intent, 0)` before
  `startActivityForResult`. Returns `server_apk_not_installed`
  immediately when no Activity matches, instead of relying on
  `ActivityNotFoundException` propagation through pyjnius (some
  OEM Android builds silently no-op the call instead of throwing).
- `done.wait()` capped at 10 minutes by default (was infinite). A
  launched-but-hung picker Activity can no longer wedge the caller
  forever; callers can still pass a smaller `timeout_seconds`.

### azt_collab_client 0.13.0 — pick_project()
- New `pick_project(timeout_seconds=None)` in `__init__.py`. Same
  shape as `open_server_ui()`: subprocess spawn on desktop
  (parses `AZT_PICK\t<path>\t<langcode>` from stdout), Intent
  dispatch on Android (uses `android.activity.bind` to wait on
  `onActivityResult`; falls back to
  `{'ok': False, 'error': 'server_apk_not_installed'}` if the
  server APK isn't installed). Sister apps in any toolkit drive
  project selection through this single helper.
- `azt_collab_client/ui/picker.py`: `register_kv` (a.k.a.
  `register_picker_kv`) gains a `hide_settings_gear` kwarg. When
  True the gear icon, its hit area, and the row containing it
  collapse to zero height — used by the standalone picker app
  which has no settings of its own.
- `azt_collab_client/azt.png` — suite icon shipped alongside the
  client. Both standalone Kivy apps (`ui` + `projects`) reference
  it via `os.path.dirname(azt_collab_client.__file__) + '/azt.png'`.
- `azt_collab_client/ui/assets/langtags_mini.json.gz` — moved from
  `azt_recorder/` (deferred item from step 2). The langpicker reads
  it from this default path, so sister apps no longer need to pass
  `langtags_path=` explicitly.

### Documentation
- New `examples/non_kivy_pick.py` — tiny demo of how a non-Kivy host
  drives project selection via `subprocess.run` + the AZT_PICK
  stdout protocol. Proves the cross-toolkit contract.

### Version constants unified
- `_VERSION` was duplicated as a hard-coded
  string in `server.py` (0.9.0) while `azt_collabd.__version__` lagged
  at 0.8.0. `__version__` is now the single source of truth at 0.9.0;
  `server.py` does `from . import __version__ as _VERSION` and all
  five wire-response references (server.json, started.json,
  /v1/health body, HTTP `Server:` header) flow from there.

### Documentation
- Added `azt_collab_client/CLAUDE.md` — a self-contained guide that
  travels with the client when sister apps symlink it in, so Claude
  Code working from a sister app's tree has full client / transport
  / API guidance without needing access to the canonical
  `azt-collab/CLAUDE.md`. The top-level `CLAUDE.md` now `@`-imports
  it to avoid duplication.
- README.md audited and rewritten to match the actual tree:
  removed references to the deleted `azt_collabd_plan.xml` /
  `azt_collabd_cleanup_drafts.xml`; added `server_apk/`,
  `azt_collab_client/ui/`, and `azt_collab_client/_spawn.py` to the
  layout; updated the sister-app symlink list to peer-only
  (`azt_collab_client` + `examples` + `android`, no `azt_collabd`);
  removed the stale "fall back to loopback" Android language and the
  per-peer `<provider>` instructions; added sections for the server
  APK workflow, the picker UI re-use story, the version handshake,
  and the new client API surface (`check_server_compat`,
  `init_project`, `derive_langcode`, `create_project_from_template`,
  `clone_project*`, GitHub install URL / device flow helpers, etc.).

## [0.8.0] — 2026-04-28 — `standalone_server_apk` cleanup-draft #3 (scaffolding)

Lays down the *scaffolding* for the standalone server APK
(`org.atoznback.aztcollab`) and the client-side changes that go
with it. The APK still has to be built and signed against real
devices; what is in this commit is the source tree, manifest, and
the client-side discovery + handshake the new architecture needs.

Per the user's answers in `azt_collabd_cleanup_drafts.xml`:

- q1: peer APKs symlink `azt_collab_client` only; `azt_collabd`
  lives only in the server APK and on desktop installs. The new
  `server_apk/README_NewClient.txt` documents the symlink set.
- q3: the server APK is the *only* component that calls
  `azt_collabd.configure(app_slug=..., client_id=...,
  collaborator=...)` — peers do not. The `server_apk/main.py` boot
  reads identity from env vars with the recorder defaults.
- q4: no persistent foreground-service notification by default;
  the server APK is allowed to be transient and respawns on the
  next peer query. `server_apk/service.py` keeps the always-on
  path behind `AZT_FOREGROUND_SERVICE=1` for opt-in.
- q5: client now exposes `check_server_compat()` and a
  `MIN_SERVER_VERSION` constant. Sister apps call it once at
  startup; an old server returns `{'ok': False, 'error':
  'server_too_old'}` so the peer can surface "Please update the
  AZT Collaboration service".

### New: `server_apk/`
- `buildozer.spec` — single-purpose APK targeting
  `org.atoznback.aztcollab`, requesting the suite signature
  permission, bundling the daemon and the Java provider glue.
- `manifest_extras.xml` — `<permission>`, `<provider>`, `<service>`
  declarations spliced into the generated manifest.
- `main.py` — Kivy entrypoint: configures GitHub App identity,
  registers the ContentProvider callbacks, opens the existing
  `azt_collabd.ui.app` settings UI as the launcher activity.
- `service.py` — opt-in foreground-service stub (off by default).
- `README_NewClient.txt` — peer-app integration guide (no
  `azt_collabd` symlink, no peer `<provider>` declaration,
  signature requirement, install-prompt + min-server-version
  flow).

### azt_collab_client 0.8.0
- `transports.android_cp.discover()` probes only the canonical
  server-APK authority `org.atoznback.aztcollab`. No `.aztcollab`
  suffix fallback (no peer-hosted daemons exist; we're building).
- `pick_transport()` on Android raises
  `ServerUnavailable('server_apk_not_installed')` when the server
  APK isn't reachable, instead of silently falling back to loopback
  (which can't work on Android — no Python interpreter to spawn).
- New `check_server_compat()` helper. Returns structured outcomes
  (`server_too_old` / `server_unreachable` / `ok`) suitable for
  driving an "update / install the AZT Collaboration service" UI
  affordance.
- `MIN_SERVER_VERSION` raised to `0.7.0`.

### azt_collabd 0.8.0
- Version constant aligned with the wider 0.8 baseline. No
  wire-format changes; older clients still talk to this server.
  The version bump is the signal a peer's `check_server_compat()`
  reads when it surfaces an upgrade prompt.

## [0.7.1] — 2026-04-28 — `wire_open_server_ui_button` cleanup-draft #2

The button-wiring itself lives in each sister app's `main.py`
(`../azt_recorder/main.py` for the recorder), which is outside the
canonical-source tree. What this repo can ship is the reusable
helper + documentation so each sister app's button is a one-liner.

### azt_collab_client 0.7.1
- New `open_server_ui()` helper. Desktop: spawns
  `python -m azt_collabd ui` detached and returns
  `{'ok': True, 'pid': ...}`. Android: returns
  `{'ok': False, 'error': 'desktop_only'}` until the standalone
  server APK lands and we can dispatch via Intent. Sister-app button
  code calls this helper, not subprocess directly, so the platform
  branching only lives here.
- Re-exported from `__all__`. `__version__` and `MIN_SERVER_VERSION`
  also re-exported.

### Documentation
- New "Wiring a sync-settings button" section in `README.md` with
  the KV + Python snippet sister apps can paste.
- Quick-reference snippet updated to mention `open_server_ui`.

### azt_collabd
- Unchanged at 0.7.0.

## [0.7.0] — 2026-04-28 — `android_contentprovider_transport` cleanup-draft #1

Closes the loose ends called out in
`azt_collabd_cleanup_drafts.xml` for the ContentProvider transport.
The transport classes, Java glue, and dispatch extraction were
already in place at 0.6.0; 0.7.0 hardens behavior when providers
come and go.

### azt_collab_client 0.7.0
- `rpc.call()` and `rpc.health()` now reset the cached transport and
  re-pick on `ServerUnavailable`. A provider host that gets killed
  mid-session falls through to loopback on the next call without
  the host having to restart. Symmetrically, a provider appearing
  after a loopback startup will be picked up the next time the
  client's transport cache is invalidated.
- `transports.current_transport_name()` exposes which transport is
  in use (``loopback`` / ``android_cp``) for diagnostic surfaces.
- `__version__` and `MIN_SERVER_VERSION` are now defined at the
  package root for sister apps to read.

### azt_collabd 0.7.0
- Version constant aligned with client. `_VERSION` bumped to 0.7.0
  in `azt_collabd/server.py` and a matching `__version__` exposed
  from the package.
- No wire-format changes; clients < 0.7.0 keep working unchanged.

## [0.6.0] — pre-cleanup baseline

Snapshot of the state at the end of the 16-step migration plan
(`azt_collabd_plan.xml`). Cleanup drafts pick up from here.

### azt_collabd 0.6.0
- Loopback HTTP server with bearer token + flock single-instance guard.
- Transport-agnostic `dispatch(method, path, body)` in `server.py`.
- Connectivity watcher and debounced `request_sync` job queue.
- LIFT-aware three-way merge by `<entry guid>` with side-by-side
  conflict preservation.
- Per-project advisory `flock` locking, reentrant within a process.
- pyjnius shim (`azt_collabd/android_cp/service.py`) routing
  ContentProvider calls into the dispatch table; `openFile` streaming
  scoped to `$AZT_HOME/projects/`.
- Crash-log tail returned in `/v1/health`.

### azt_collab_client 0.6.0
- Pluggable `Transport` ABC; `pick_transport()` chooses Android
  ContentProvider when reachable, else loopback.
- Loopback transport spawns the daemon on demand, retries on
  `SERVICE_RESTARTED`.
- Decode-only `Status` / `Result` / `Project` / `ProjectStatus`.
- `translate_status` / `translate_result` for UI display; default
  English + French maps.
