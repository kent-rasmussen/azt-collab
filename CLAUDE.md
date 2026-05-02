# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

This repo holds two cooperating Python packages and is the **canonical source** consumed by sibling AZT suite apps via symlinks (notably `../azt_recorder/`).

- `azt_collabd/` — the local daemon. Owns dulwich, the scheduler, credentials, project registry, locks, and the loopback HTTP server. **No Kivy and no i18n imports.** UI marshaling and translation are the host app's job.
- `azt_collab_client/` — thin client library every suite app uses. Decode-only mirrors of `Status`/`Result`/`Project`/`ProjectStatus`, plus the transport facade. Must remain platform-agnostic. **Client-specific guidance lives in `@azt_collab_client/CLAUDE.md`** (which travels with the package when sister apps symlink it in).
- `examples/sister_app.py` — runnable demo of the sister-app integration pattern.
- `android/` — Java ContentProvider glue + `SUITE_FINGERPRINT`.
- `azt_collabd_plan.xml` (done) and `azt_collabd_cleanup_drafts.xml` (outstanding) — original plan + follow-ups.

@azt_collab_client/CLAUDE.md

## Naming conventions

The suite has one underlying name per component (e.g. "azt collab",
"azt recorder"), but it must surface across systems with incompatible
identifier rules. The convention:

| Where | Transform | Example ("azt collab") | Example ("azt recorder") |
|---|---|---|---|
| Repo / dir / Python pkg / env var / permission constant | spaces → `_` | `azt_collab`, `azt_collabd`, `AZT_HOME`, `AZT_COLLAB_ACCESS` | `azt_recorder`, `AZT_RECORDER_*` |
| **Android package segment** | spaces dropped | `org.atoznback.aztcollab` | `org.atoznback.aztrecorder` |
| **GitHub App slug** | spaces → `-` | `azt-collaboration` | `azt-recorder` |
| Human-facing title (launcher icon, prose) | Title Case with spaces | "AZT Collaboration" | "AZT Recorder" |

Two systems force the dropped/hyphenated transforms — Android package
segments forbid `-` (so `_` or nothing), and GitHub App slugs forbid
`_` (so `-` only). Everywhere else, `_` is the suite default.

The internal name "collab" stays for code identifiers (`azt_collabd`,
`AZT_COLLAB_ACCESS`, `org.atoznback.aztcollab`). The human-facing name
expands to "Collaboration" — same word in French and English, which
keeps i18n natural for the SIL user base. The GitHub App slug
(`azt-collaboration`) follows the human-facing form so the URL on
github.com/apps/ reads as the published service name.

When adding a new suite component:

1. Pick the underlying multi-word name (e.g. "azt scripture toolkit").
2. Apply the table above for each system.
3. If the dropped form would be eye-soup (`aztscripturetoolkit`),
   reconsider the name — the dropped form is the Android package and
   appears in package manager listings.

## Architecture invariants (read before changing things)

1. **One daemon per device.** Auto-spawned by the client via `python -m azt_collabd` on first call. Disable with `AZT_CLIENT_AUTOSPAWN=0`. State lives at `$AZT_HOME` (Linux: `~/.local/share/azt/`; macOS: `~/Library/Application Support/azt/`).

2. **Discovery is `$AZT_HOME/server.json`** (`{port, token, pid, version}`). Every endpoint except `GET /v1/health` requires `Authorization: Bearer <token>`. The daemon holds a flock on `server.lock` for its lifetime; that's how a second daemon detects an existing one.

3. **Daemon is the only thing that touches dulwich.** Clients write files into the working tree (or stream through the Android ContentProvider) and ask the daemon to commit. Don't add git operations to the client.

4. **Structured `Result`s, never log strings.** Drive business logic with `Result.has(S.CODE)`. Substring matching on translated text is a regression — fix it. Status codes are uppercase strings (`azt_collabd/status.py` is the source; `azt_collab_client/status.py` is decode-only). Translation lives in `azt_collab_client/translate.py`.

5. **Two transports, one facade.** `azt_collab_client.rpc.call()` delegates to `pick_transport()` in `azt_collab_client/transports/__init__.py`. On Android, prefer the ContentProvider; fall back to loopback. Add new transports by implementing the `Transport` ABC and slotting into `pick_transport()`.

6. **Per-project advisory locks.** `azt_collabd/locks.py` provides reentrant `flock`-backed locks keyed by working_dir. Re-entry within the same process is required so helpers like `commit_audio_and_sync` can call `sync_repo` without deadlocking.

7. **Sync flow: commit-first → fetch → ff/merge → push,** with `merge_retry_max` race retries. Debounced (default 500 ms) when called via `request_sync` so bursts of edits collapse into one commit.

8. **LIFT-aware merge by `<entry guid="...">`.** Per-entry, not per-field, in v1. Conflicts get `<annotation name="azt-lift-conflict" value="ours|theirs">`; both versions are kept side by side. The "theirs" copy gets a synthetic guid suffix to keep the document valid. See `azt_collabd/lift_merge.py`.

9. **Two `configure()` calls, both keyword-only and idempotent.** Host app calls `azt_collabd.configure(app_slug=..., client_id=..., collaborator=...)` for GitHub App identity, and `azt_collab_client.configure(app_id=...)` for client-side identity. Defaults match the recorder. Env vars (`AZT_GITHUB_APP_CLIENT_ID`, `AZT_GITHUB_APP_SLUG`, `AZT_GITHUB_COLLABORATOR`) work when launched standalone.

## Common commands

```bash
# Run the daemon manually (normally auto-spawned)
python -m azt_collabd
python -m azt_collabd ui          # standalone Kivy settings UI
python -m azt_collabd help

# Run the sister-app demo end-to-end
python examples/sister_app.py /path/to/some_lift_project
```

## Tests

There is **no local test suite in this repo**. Canonical step-by-step verification scripts live at `../azt_recorder/tests/stepN.sh` and run with the recorder's venv:

```bash
cd ../azt_recorder
bash tests/step12.sh   # LIFT merge driver
bash tests/step16.sh   # sister-app example
```

When adding a feature here, validate it by running (or extending) the relevant `stepN.sh` over there.

## Runtime config

`$AZT_HOME/config.json` holds runtime knobs; env vars override:

| Key | Env var | Default |
|---|---|---|
| `sync.debounce_ms` | `AZT_SYNC_DEBOUNCE_MS` | 500 |
| `sync.merge_retry_max` | `AZT_SYNC_MERGE_RETRY_MAX` | 3 |
| `sync.connectivity_poll_s` | `AZT_SYNC_CONNECTIVITY_POLL_S` | 30 |
| dir override | `AZT_HOME` | platform default |
| disable auto-spawn | `AZT_CLIENT_AUTOSPAWN=0` | enabled |

## Android specifics

- Suite signature permission is `org.atoznback.AZT_COLLAB_ACCESS` (`protectionLevel="signature"`). The standalone server APK (`org.atoznback.aztcollab`) is the **only** app that declares the `<permission>` (in `server_apk/manifest_extras.xml`) and exports the `<provider>` at authority `org.atoznback.aztcollab` (injected post-render by `p4a_hook.py:_inject_aztcollab_provider`, gated on `dist_name == 'aztcollab'`). Peer apps consume it via `<uses-permission>` (from `android.permissions` in their spec) plus a `<queries><package .../></queries>` block (from `android/manifest_extras_peer.xml`, symlinked into each peer as `manifest_extras.xml`).
- Expected keystore SHA-256 is in `android/SUITE_FINGERPRINT`. Mismatched signing → install-time permission grant denied → ContentProvider transport silently falls back to loopback.
- Suite apps must call `azt_collabd.android_cp.service.install_callbacks()` in their startup hook (no-op on desktop).

## When adding a new client API call

1. Add the endpoint to `azt_collabd/server.py` (dispatch table), returning `{ok: True, ...}` or `{ok: False, error: ...}`.
2. Add a thin wrapper in `azt_collab_client/__init__.py` that calls `rpc.call()` and decodes into the right dataclass / `Result`.
3. Re-export from `__all__` in `azt_collab_client/__init__.py`.
4. Add status codes (if any) to `azt_collabd/status.py` AND `azt_collab_client/status.py`, and a translation in `azt_collab_client/translate.py`.

## Sister-app integration (when asked)

Setup is symlink-based, not pip-installed. From a sibling app's repo root:

```bash
# top-level shared dirs (some apps embed the daemon, hence azt_collabd):
for x in azt_collabd azt_collab_client examples android; do
    ln -s "../azt-collab/$x" "$x"
done

# peer manifest extras — references the canonical file in android/
# so all peers stay in sync (Android 11+ <queries> visibility):
ln -s ../azt-collab/android/manifest_extras_peer.xml manifest_extras.xml
```

Then in `buildozer.spec`, point at the symlink:

```
android.extra_manifest_xml = %(source.dir)s/manifest_extras.xml
```

(Note the key is `extra_manifest_xml`, NOT `manifest_extra_xml` — the
latter is silently ignored by buildozer.)

Then both `azt_collabd.configure(app_slug='...')` and `azt_collab_client.configure(app_id='...')` once at startup, before the first client call.
