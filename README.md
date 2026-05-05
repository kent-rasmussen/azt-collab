# azt-collab

Shared collaboration backend for the A-Z+T suite of linguistic tools. To use, install https://github.com/apps/azt-collaboration in your GitHub account and authorize

A single local daemon (`azt_collabd`) per device manages git
collaboration for LIFT projects: GitHub/GitLab credentials, project
registry, debounced push/pull with LIFT-aware three-way merge,
per-project locking, and crash recovery. Suite apps consume it through
a thin client library (`azt_collab_client`) that auto-discovers the
daemon and is platform-agnostic.

This repo is the **canonical source** for the daemon, the client, the
shared Kivy picker UI, the standalone Android server APK, and the
sister-app example. AZT suite apps consume it as sibling-directory
symlinks; the recorder lives in `../azt_recorder/`.

## What's in here

```
azt-collab/
  azt_collabd/                  # daemon: dulwich, scheduler, dispatch
    __init__.py                 #   re-exports + configure()
    __main__.py                 #   `python -m azt_collabd [ui|help]`
    server.py                   #   loopback HTTP transport + dispatch
    repo.py                     #   git ops, merge integration
    lift_merge.py               #   LIFT-aware three-way merge
    merge_commit.py             #   merge commit message format
    scheduler.py                #   debouncer + connectivity watcher
    store.py                    #   credentials.json
    projects.py                 #   project registry (projects.json)
    settings.py                 #   config.json runtime knobs
    locks.py                    #   per-project flock
    paths.py                    #   $AZT_HOME resolution
    config.py                   #   GitHub App identity
    status.py                   #   Status/Result + AuthError + codes
    net.py                      #   SSL patching + is_online
    auth.py                     #   GitHub device flow, GitLab API
    android_cp/service.py       #   pyjnius shim for ContentProvider
    ui/app.py                   #   standalone Kivy settings UI
  azt_collab_client/            # thin client used by every suite app
    __init__.py                 #   public API, version constants
    _spawn.py                   #   PYTHONPATH helpers for subprocess spawn
    rpc.py                      #   facade over transports
    transports/loopback.py      #   localhost HTTP transport
    transports/android_cp.py    #   ContentProvider transport
    translate.py                #   Status code → user string
    paths.py                    #   mirror of azt_collabd/paths.py
    status.py                   #   decode-only Status/Result
    projects.py                 #   decode-only Project/ProjectStatus
    ui/                         #   shared Kivy picker UI for sister apps
      theme.py                  #     palette presets + role-based colors
      langpicker.py             #     LangPickerScreen (BCP-47 picker)
      picker.py                 #     ProjectPickerScreen
      popups.py                 #     clone-from-URL prompt, etc.
    test_peer.sh                #   peer-side manifest plumbing check
                                #     (run after installing a peer APK)
  android/
    SUITE_FINGERPRINT           # SHA-256 of the suite signing key
    manifest_extras_peer.xml    # canonical peer <queries> block
                                #   (peers symlink as manifest_extras.xml)
    src/main/java/.../AZTCollabProvider.java
  server_apk/                   # standalone Android server APK source
    buildozer.spec              #   targets org.atoznback.aztcollab
    manifest_extras.xml         #   top-level <permission> only;
                                #     <provider> injected at build time
    main.py                     #   APK entrypoint: configure +
                                #     install_callbacks + dispatch to
                                #     settings UI or picker (Intent-based)
    service.py                  #   reserved (foreground-service path)
    setup.sh                    #   idempotent: creates the
                                #     azt_collabd/ + azt_collab_client/
                                #     symlinks needed for packaging
    test_install.sh             #   on-device server APK verification
                                #     (run after install + adb deploy)
    README_NewClient.txt        #   peer-app integration guide
  examples/sister_app.py        # runnable demo for a new suite app
  CHANGELOG.md                  # versioned change history (client + daemon)
  CLAUDE.md                     # guidance for Claude Code in this repo
```

The original `azt_collabd_plan.xml` and `azt_collabd_cleanup_drafts.xml`
are no longer in the tree; their history is preserved in CHANGELOG
entries 0.6.0 → 0.8.0.

## Architecture in 30 seconds

- **One daemon per device.** On desktop, auto-spawned by the client via
  `python -m azt_collabd` on first call. On Android, the daemon lives
  inside the standalone server APK (`org.atoznback.aztcollab`); peer
  APKs never bundle `azt_collabd`.
- **Single source of truth on disk** at `$AZT_HOME` (default
  `~/.local/share/azt/` on Linux, `~/Library/Application Support/azt/`
  on macOS): credentials, project registry, lock files, crash log.
- **Two transports.** Loopback HTTP (desktop) and Android
  ContentProvider (Android — bound to the canonical server-APK
  authority `org.atoznback.aztcollab`). On Android there is **no
  loopback fallback**: if the server APK isn't installed, the client
  surfaces `ServerUnavailable('server_apk_not_installed')` so peers can
  show an install prompt.
- **Sync flow.** Client calls `request_sync(langcode, contributor)`,
  daemon debounces (default 500 ms) and runs commit-first → fetch →
  fast-forward / merge / push, with `merge_retry_max` race retries.
- **LIFT-aware merge.** `<entry guid="...">` is the merge key.
  Conflicts get `<annotation name="azt-lift-conflict">` markers;
  divergent versions are kept side by side.
- **Version handshake.** Clients carry a `MIN_SERVER_VERSION` constant.
  `check_server_compat()` is the one-shot probe that tells a peer
  whether to surface "Please update the AZT Collaboration service".

## Setting up a new sister app

Assumes you have a sibling directory `../my-sister-app/` already
holding your app's source.

### 1. Symlink the shared modules

```bash
cd ../my-sister-app
for x in azt_collab_client examples android; do
    ln -s "../azt-collab/$x" "$x"
done

# Android peer manifest extras (suite-wide <queries> block — see §4):
ln -s ../azt-collab/android/manifest_extras_peer.xml manifest_extras.xml
```

Peers symlink **only `azt_collab_client`** (plus the example and the
shared Java glue if they're shipping Android). The daemon
(`azt_collabd`) lives in the server APK on Android and in desktop
installs; peer apps don't import it and shouldn't symlink it.

After this, `import azt_collab_client` works from your app's source.

### 2. Identify the app at startup

```python
import azt_collab_client
azt_collab_client.configure(app_id='azt-my-sister-app')
```

That's the only `configure()` a peer makes. The GitHub App identity
(slug, client_id, collaborator) lives in the server APK; peers never
call `azt_collabd.configure`. `configure()` is idempotent and
keyword-only.

### 3. Compatibility check at startup

Before the first real RPC:

```python
from azt_collab_client import check_server_compat, SERVER_APK_INSTALL_URL

compat = check_server_compat()
if not compat['ok']:
    if compat['error'] == 'server_too_old':
        # Surface "Please update the AZT Collaboration service"
        ...
    elif compat['error'] == 'server_unreachable':
        # On Android: server APK not installed → install prompt
        # On desktop: daemon failed to spawn → surface compat['detail']
        ...
```

Bumping `azt_collab_client.MIN_SERVER_VERSION` is how an old server
APK gets obsoleted without coordinating a release across peers.

### 4. (Android only) Manifest

In `buildozer.spec`:

```
android.permissions = INTERNET, ..., org.atoznback.AZT_COLLAB_ACCESS
android.extra_manifest_xml = %(source.dir)s/manifest_extras.xml
```

The first line generates the `<uses-permission>`. The second line
points at the symlinked `manifest_extras.xml` (created in §1) which
contributes the suite-wide `<queries>` block. The `<queries>` element
is required on Android 11+ so the client's discovery probe can see
the server APK's provider.

(The buildozer key is `extra_manifest_xml` — the variant
`manifest_extra_xml` is silently ignored. Easy to miss; if your peer
seems unable to see the server APK, check the key spelling first.)

**Don't declare a `<provider>` of your own.** The server APK exports
the only one (injected at packaging time by
`buildozer_tweaks/p4a_hook.py:_inject_aztcollab_provider`, gated on
`dist_name == 'aztcollab'`). Peers are pure ContentResolver
consumers.

### 5. (Android only) Sign with the suite keystore

The custom permission is `protectionLevel="signature"`. Peers signed
with a different key install fine but the install-time grant is denied
and provider calls silently fail.

The expected SHA-256 fingerprint is in `android/SUITE_FINGERPRINT`.
Verify your build matches:

```bash
keytool -printcert -jarfile bin/my-sister-app-*-unsigned.apk \
    | grep SHA256
```

Sign with `jarsigner` (JDK) or `apksigner` (build-tools):

```bash
jarsigner -verbose -sigalg SHA256withRSA -digestalg SHA-256 \
    -keystore /path/to/azt-suite.keystore \
    bin/my-sister-app-*-unsigned.apk <alias>
```

Don't commit the keystore. Reference its path via `buildozer.spec`
`android.signing.keystore` or pass at build time.

A more terse version of this peer-app guide ships at
`server_apk/README_NewClient.txt`.

## The standalone server APK (`server_apk/`)

`server_apk/` is the Android packaging shell for the server APK
(`org.atoznback.aztcollab`). It contains no business logic — just
`buildozer.spec`, manifest extras, a Kivy entrypoint, and a
foreground-service stub. The actual daemon code is bundled from the
sibling `azt_collabd/` package at packaging time.

Workflow when shipping to Android:

1. Edit `azt_collabd/...` or `azt_collab_client/...` here.
2. Test on desktop (`python -m azt_collabd ui`,
   `python examples/sister_app.py ...`).
3. `bash server_apk/setup.sh` — idempotent symlink creator for
   `azt_collabd/` and `azt_collab_client/` into `server_apk/`. Run
   once after a fresh checkout so buildozer can find the daemon
   code. Re-running is safe.
4. `cd server_apk && buildozer android debug` — packages the current
   source into the APK. The build relies on local recipe overrides
   under `/home/kentr/bin/raspy/buildozer_tweaks/recipes/` (NDK r29
   compatibility for harfbuzz / kivy) and on the
   `_inject_aztcollab_provider` step in
   `buildozer_tweaks/p4a_hook.py` (which patches the rendered
   `AndroidManifest.xml` to declare the `<provider>` inside
   `<application>`, since p4a's manifest template only exposes
   top-level injection).
5. Build each peer APK separately; peers symlink only
   `azt_collab_client` plus `manifest_extras.xml`.
6. After install, verify the round-trip:

   ```bash
   adb install -r server_apk/bin/aztcollab-*-debug.apk
   bash server_apk/test_install.sh        # server-side: 15 checks
   bash azt_collab_client/test_peer.sh    # peer-side: per-peer checks
   ```

   `test_install.sh` covers manifest integrity, provider registration,
   bundled modules, icon, activity startup, dist consistency, and
   installed-vs-bin APK match. `test_peer.sh` walks every installed
   peer under `org.atoznback.*`, confirming each declares
   `AZT_COLLAB_ACCESS`, was granted (signature match), declares the
   `<queries>` block, and matches `android/SUITE_FINGERPRINT`.

The server APK is allowed to be transient: there's no persistent
foreground-service notification by default. When peers go idle, Android
may stop the process; the next `ContentResolver.call` wakes it back up.
Set `AZT_FOREGROUND_SERVICE=1` to opt into the always-on path.

## Wiring a sync-settings button

Sister apps that have a Collab/Sync screen should expose a button that
opens the daemon's standalone settings UI (so users can connect to
GitHub, switch hosts, view sync logs without leaving the suite). Use
the bundled helper so the platform branching stays in one place:

```python
# Python
from azt_collab_client import open_server_ui

class MyApp(App):
    def open_server_ui(self):
        result = open_server_ui()
        if not result['ok']:
            self.collab_screen.set_log(
                f'Could not open sync settings: {result["error"]}')
```

```kv
# Kivy KV — drop at the bottom of your collab/sync screen
<CollabScreen>:
    # ...existing children...
    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(10)
        padding: [dp(20), dp(8)]
        Label:
            text: _tr('Advanced')
            size_hint_x: None
            width: dp(80)
            color: T.TEXT_DIM
        Button:
            text: _tr('Open Sync Settings')
            on_release: app.open_server_ui()
```

On Android the helper currently returns `{'ok': False, 'error':
'desktop_only'}` (subprocess.Popen has no Python interpreter to fork
on Android). Once the server APK gains an Intent-dispatch entrypoint
the same helper will route to it; sister-app button code never has to
change.

## Reusing the picker UI

Sister apps can drop the recorder's project/language picker screens
straight into their own ScreenManager via `azt_collab_client.ui`:

```python
from azt_collab_client.ui import (
    LangPickerScreen, register_langpicker_kv,
    ProjectPickerScreen, register_picker_kv,
    clone_url_popup,
)

# After your KV is built
register_picker_kv(font_name='Roboto')
register_langpicker_kv(font_name='Roboto')
```

The host App must implement the contract documented at the top of
`azt_collab_client/ui/picker.py` (icon, title/subtitle properties,
`open_file()`, `clone_dialog()`, `list_projects()`, `load_lift(path)`,
etc.). Theme colors live at `azt_collab_client.ui.theme` — KV files
import them via `#:import T azt_collab_client.ui.theme`.

## Client API quick reference

```python
from azt_collab_client import (
    # Lifecycle
    configure, is_online, open_server_ui, ServerUnavailable,
    check_server_compat, MIN_SERVER_VERSION, __version__,
    SERVER_APK_INSTALL_URL,

    # Credentials (server-owned credentials.json)
    get_credentials_status, set_collab_host,
    github_app_install_url, github_app_client_id,
    github_device_flow_start, github_device_flow_status,
    save_github_tokens, mark_github_app_installed,
    save_gitlab_credentials, migrate_from_prefs,

    # Projects (server-owned projects.json)
    list_projects, open_project, register_project,
    derive_langcode, init_project,
    create_project_from_template,
    clone_project, clone_project_start, clone_project_status,
    project_status, record_project_sync_time,

    # Sync
    sync_project,        # synchronous, returns Result
    request_sync,        # debounced, returns job_id
    poll_job,            # poll a request_sync job

    # Translation
    translate_status, translate_result, set_translator,

    # Status codes + dataclasses
    S, Status, Result, Project, ProjectStatus,
)
```

A typical sister-app sync flow:

```python
register_project('fra', '/path/to/working_tree', '/path/.../fra.lift')
job_id = request_sync('fra', contributor='Kent')
# ...later, after debounce_ms...
status = poll_job(job_id)
if status['state'] == 'DONE':
    print(translate_result(status['result']))
```

See `examples/sister_app.py` for an end-to-end demo runnable with:

```bash
python examples/sister_app.py /path/to/some_project_dir
```

## Status codes worth checking

Drive business logic with `Result.has(S.CODE)`, not by parsing
translated strings.

| Code | Meaning |
|---|---|
| `PUSHED`, `PULLED`, `COMMITTED_AND_PUSHED` | sync made network progress |
| `COMMITTED_LOCAL`, `COMMITTED_OFFLINE` | local-only commit landed |
| `NOTHING_TO_COMMIT` | working tree was clean |
| `CLONED`, `LIFT_FOUND`, `LIFT_NOT_FOUND` | clone outcomes |
| `INITIALIZED`, `ALREADY_INITIALIZED`, `REMOTE_SET`, `REMOTE_UPDATED` | init outcomes |
| `NOT_A_REPO`, `NO_REMOTE` | project setup incomplete |
| `AUTH_REQUIRED`, `APP_NOT_INSTALLED`, `REPO_NOT_AUTHORIZED`, `ACCESS_DENIED` | credentials problem (translate for the user) |
| `AUTH_EXPIRED`, `AUTH_DENIED`, `AUTH_TIMEOUT` | device-flow outcomes |
| `CONFLICTS` | merge had conflicts; entries flagged with `<annotation name="azt-lift-conflict">`. `result.has(S.CONFLICTS)` carries `paths` param. |
| `BUSY` | another op holds the per-project lock |
| `SERVICE_RESTARTED` | the daemon respawned mid-call (the client retries automatically) |

Full list: `azt_collab_client/status.py`.

## Configuration

`$AZT_HOME/config.json` — runtime knobs, env-var overrides:

```json
{
  "sync.debounce_ms": 500,
  "sync.merge_retry_max": 3,
  "sync.connectivity_poll_s": 30
}
```

| Key | Env var | Default |
|---|---|---|
| `sync.debounce_ms` | `AZT_SYNC_DEBOUNCE_MS` | 500 |
| `sync.merge_retry_max` | `AZT_SYNC_MERGE_RETRY_MAX` | 3 |
| `sync.connectivity_poll_s` | `AZT_SYNC_CONNECTIVITY_POLL_S` | 30 |
| `AZT_HOME` (dir override) | `AZT_HOME` | platform default |
| Disable auto-spawn | `AZT_CLIENT_AUTOSPAWN=0` | enabled |
| Foreground service (server APK) | `AZT_FOREGROUND_SERVICE=1` | off |

GitHub App identity (used for the device-flow client_id and bot
committer name) is set by the host app via `azt_collabd.configure` — on
Android, that means the server APK's `main.py`. Env vars also work:

| Env var | Default |
|---|---|
| `AZT_GITHUB_APP_CLIENT_ID` | `Iv23li66Fo9MBReatv6i` |
| `AZT_GITHUB_APP_SLUG` | `azt-collaboration` |
| `AZT_GITHUB_COLLABORATOR` | `kent-rasmussen` |
| `AZT_DEFAULT_TEMPLATE_URL` | SILCAWL on GitHub |

## Daemon CLI

```bash
python -m azt_collabd          # start the daemon (foreground)
python -m azt_collabd ui       # standalone Kivy settings UI
python -m azt_collabd help     # entrypoint listing
```

The daemon is auto-spawned by the client library on desktop; running
manually is mostly useful for development or debugging. On Android the
daemon process is the server APK itself, launched by Android's package
manager when peers query its provider.

## Versioning

Two packages live here, versions move together for now (the client
embeds `MIN_SERVER_VERSION`, so when the wire format changes both
bump). Patch-level bumps in one without the other are fine.

- **azt_collabd** — daemon. Source of truth: `azt_collabd.__version__`
  (re-imported as `_VERSION` inside `server.py` for the wire response).
- **azt_collab_client** — client library. Source of truth:
  `azt_collab_client.__version__` and `MIN_SERVER_VERSION`.

See `CHANGELOG.md` for the full history.

## Testing

There is **no local test suite in this repo**. The canonical
step-by-step verification scripts live at `../azt_recorder/tests/stepN.sh`
and run with the recorder's venv:

```bash
cd ../azt_recorder
bash tests/step12.sh   # LIFT merge driver
bash tests/step16.sh   # sister-app example
```

When adding a feature here, validate it by running (or extending) the
relevant `stepN.sh` over there. Sister apps can copy + adapt these
patterns; nothing in `tests/` needs to be sister-app-specific.

## Conventions

- The backend has **no Kivy and no i18n imports**. UI marshaling and
  translation are the host app's job. (The shared Kivy UI under
  `azt_collab_client/ui/` is part of the client, not the daemon.)
- All ops return structured `Result`s, not log strings. Substring
  matching on translated text is a regression and should be replaced
  with `Result.has(S.CODE)`.
- The daemon is the only thing that talks to dulwich. Clients write
  files into the working tree (or stream through the ContentProvider
  on Android) and ask the daemon to commit.
- Per-project advisory locking via `flock` on POSIX. Operations that
  cross projects are independent.
- `azt_collabd.configure(...)` for GitHub App identity, called once at
  daemon startup (server APK on Android, host app on desktop).
  `azt_collab_client.configure(app_id=...)` for client-side identity.
  Defaults match the recorder.
