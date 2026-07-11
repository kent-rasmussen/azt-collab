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

1. **One daemon per device, two transports, two lifetime models.**
   *Desktop:* daemon auto-spawned via `python -m azt_collabd` on first
   client call (disable with `AZT_CLIENT_AUTOSPAWN=0`); runs as a
   detached child for the session. *Android:* daemon lives in the
   standalone server APK (`org.atoznback.aztcollab`), reached via
   `AZTCollabProvider`. The APK runs a sticky-bound service
   (`AZTServiceProviderhost`) that pins the Python process so URI
   grants survive picker dismissal and any peer that received a
   `content://` URI can still call `openFileDescriptor` on it. The
   service auto-stops after 5 min idle (no bound peers + no provider
   activity); under memory pressure Android may kill earlier, in
   which case the next peer ContentResolver call lazy-spawns the
   host (Android's unconditional contract for ContentProvider
   authorities) and `Service.onCreate` re-runs
   `scheduler.reconcile_on_startup()` to flip in-flight jobs to
   `JOB_INTERRUPTED` for peer-side retry. State at `$AZT_HOME`
   (Linux: `~/.local/share/azt/`; macOS:
   `~/Library/Application Support/azt/`; Android: the server APK's
   private `filesDir`).

2. **Discovery is `$AZT_HOME/server.json`** (`{port, token, pid, version}`). Every endpoint except `GET /v1/health` requires `Authorization: Bearer <token>`. The daemon holds a flock on `server.lock` for its lifetime; that's how a second daemon detects an existing one. (Loopback transport only — Android peers reach the daemon via ContentProvider, not via server.json.)

3. **Daemon is the only thing that touches dulwich.** Clients write files into the working tree (or stream through the Android ContentProvider) and ask the daemon to commit. Don't add git operations to the client.

4. **Structured `Result`s, never log strings.** Drive business logic with `Result.has(S.CODE)`. Substring matching on translated text is a regression — fix it. Status codes are uppercase strings (`azt_collabd/status.py` is the source; `azt_collab_client/status.py` is decode-only). Translation lives in `azt_collab_client/translate.py`.

5. **Two transports, one facade.** `azt_collab_client.rpc.call()` delegates to `pick_transport()` in `azt_collab_client/transports/__init__.py`. On Android, prefer the ContentProvider; fall back to loopback. Add new transports by implementing the `Transport` ABC and slotting into `pick_transport()`.

6. **Per-project advisory locks.** `azt_collabd/locks.py` provides reentrant `flock`-backed locks keyed by working_dir. Re-entry within the same process is required so helpers like `commit_audio_and_sync` can call `sync_repo` without deadlocking.

7. **Commit / push split (0.43.0).** Peers fire `commit_project(langcode)` per group of related changes — debounced (default 500 ms) so bursts collapse to one commit. The RPC is commit-only: stage + `porcelain.commit`, never fetch / merge / push. Push is driven by the scheduler's drain loop (`_drain_pending_push` in `_watcher_loop`) which gates on: online (`is_online_cached`) + post-online grace (`sync.post_online_grace_s`, default 60 s — avoids burning a brief tether's MB) + `sync.work_offline` off (user-controlled daemon-wide toggle). The user-gestured Sync button (`sync_project`) still does commit + push under one lock and is the only path that surfaces `S.WORK_OFFLINE_ENABLED`. Async commit jobs are mirrored to `$AZT_HOME/jobs.json` on every state transition; `scheduler.reconcile_on_startup()` marks `PENDING` / `RUNNING` entries as `DONE` + `JOB_INTERRUPTED` after a daemon respawn so peers polling on a stale `job_id` receive a typed transient-failure result instead of silence. (Pre-0.43 this was one RPC named `request_sync` that did both halves; that name is kept as a peer-side alias for backwards compat.)

8. **LIFT-aware merge by `<entry guid="...">`.** Per-entry, not per-field, in v1. Conflicts get `<annotation name="azt-lift-conflict" value="ours|theirs">`; both versions are kept side by side. The "theirs" copy gets a synthetic guid suffix to keep the document valid. See `azt_collabd/lift_merge.py`.

9. **Two `configure()` calls, both keyword-only and idempotent.** Host app calls `azt_collabd.configure(app_slug=..., client_id=..., collaborator=...)` for GitHub App identity, and `azt_collab_client.configure(app_id=...)` for client-side identity. Defaults match the recorder. Env vars (`AZT_GITHUB_APP_CLIENT_ID`, `AZT_GITHUB_APP_SLUG`, `AZT_GITHUB_COLLABORATOR`) work when launched standalone.

10. **LAN sync is opportunistic peer-to-peer; github stays authoritative (0.45.0).** Each daemon binds a TLS HTTPS listener when `lan.allow_sync` is on (hot-applied — toggle flip doesn't restart the daemon); since 0.54.3 it re-binds its previous port when free (`$AZT_HOME/lan_listener_port` memo, ephemeral fallback) so a respawn doesn't invalidate every peer's cached/persisted endpoint. Per-device ed25519 keypair + self-signed X.509 cert in `$AZT_HOME/peer_id` / `peer.crt`; peers pin each other by SHA-256 fingerprint via `peers.json`. TLS client-cert validation is **deliberately CERT_NONE** — stdlib `ssl` has no "request cert + skip CA chain" mode, so identity is asserted in the body (signed claim against the recorded fp) and bound to user gesture (QR display) before any auto-share. **LAN delivery never clears `pending_push`** — only a successful github push does (per the "github convergence" property in the parked spec). **LAN fan-out is per-commit, per-project, event-driven** — `scheduler._run_commit` fires `lan_push.fan_out(project)` after each `COMMITTED_LOCAL` for the project just committed, and `_h_publish_*` fires it after a successful publish (per the 0.50.x sync rebuild). The scheduler's `_drain_pending_push` is **WAN-only** (`scheduler.py:894`); it does not fire LAN fan-out, because the design treats LAN as opportunistic and github as the convergence safety net. Since 0.50.45 the post-commit burst is gated by `lan_backoff` (per-project commit-count curve, bursts at 1, 2, 4, 8, … commits since last successful delivery) so a lone worker doesn't burn the radio. `fan_out` now also calls `sweep_peer(peer_id, exclude_langcode=this)` per candidate to opportunistically catch the peer up on *every* shared project (past work on other projects converges without needing a commit on them). mDNS arrival of a paired peer fires `sweep_peer` automatically (transition detection in `lan_discovery._record` / `onServiceResolved`). Lifecycle gestures (picker `on_resume`, `lan_burst_now` RPC, listener-bind) fire bursts independently of the commit-curve. **Daemon lifecycle is not user intent**: respawn, OOM, APK self-update do NOT reset `wan_backoff` or `lan_backoff` curves; only successful delivery (`record_success`) and user-tap Sync (`nudge`) do. The peer's currently-loaded project is **not** a gate — fan-out is governed by the per-peer `shared_projects` allowlist in `peers.json`, not by what's open in the UI. Symmetric unshare (0.50.44): user-tap "unshare" fires `send_share_unshared` so the peer mirrors the allowlist removal and stops auto-fanout in the reverse direction. Discovery: desktop uses python-zeroconf; Android uses `NsdManager` with `static_endpoints` as the hotspot-host fallback. The fan-out and the merge path go through the same `repo._merge_diverged` as github sync; the LAN merge holds `project_lock` for the fetch + merge + push (see `lan_push._merge_then_push`). See `azt_collab_client/docs/rationale/sync.md` for the per-commit-vs-drain rationale.

11. **`.git/config` writes hold `project_lock`.** The `_h_project_status` retroactive `strip_lan_origin_if_present` fires on every status poll; concurrent `init_repo` / Publish / `_h_lan_adopt_origin` are the race. Treat any new code path that calls `config.write_to_path()` the same way. Bounded 2 s timeout so picker-poll callers defer rather than block when the lock is busy.

12. **LIFT-aware merge truncation guards.** `_looks_truncated` (input-side, refuses if one side <1/50 of the larger ≥50-entry side, or empty) and `_looks_catastrophic_output` (output-side, refuses if merged <1/4 of smaller input). Both keep the larger healthy side and emit a typed `Conflict`. Don't disable these guards — field repros showed merges going 1700 entries → 1 field on malformed inputs. Self-heal (0.45.34): `_canon_clean` strips false-positive `azt-lift-conflict` annotations + normalizes inter-element whitespace before equality compare, so polluted LIFTs converge over successive recovery passes. Single source of truth for `atomic_recovery._MIN_AGE_S` (60 s); `lan_listener._reset_working_tree_after_receive` imports it for cross-module consistency.

13. **No duplicate same-lang forms in merge output — ever (0.54.0).** `_normalize_entry` runs on every entry of every `three_way_merge` output (all call sites): identical same-lang `<form>`/`<gloss>` duplicates collapse to the document-first node; duplicates inside a `<field type="…verification…">` union their python-list code content with **byte-identical semantics to azt's `Field.consolidate_forms_by_lang`** (first-seen order; a check verified to different values on the two sides is dropped — it must re-verify) — if you change one layer's union semantics you must change both; any other same-lang multiplicity survives only as an annotated `azt-lift-conflict` pair. Same-key children pair by content (`_pair_same_key`), not position, and one-sided children unchanged since base are honored as deletes — pre-0.54.0 positional pairing + base-blind keeps multiplied one duplicate per merge (field repro 2026-07-10: 'wife' entry, 29 same-lang forms in one verification field, one computer).

## Common commands

```bash
# Run the daemon manually (normally auto-spawned)
python -m azt_collabd
python -m azt_collabd ui          # standalone Kivy settings UI
python -m azt_collabd help

# Read-only sister-app survey (everything the client gets from the daemon;
# p/s open the picker / settings UI subprocesses)
python examples/sister_app.py
```

## Tests

A pytest scaffold lives at `azt-collab/tests/` (established v0.28.1) covering the install/update path: `_version_tuple` corner cases, GitHub-API mocks for `check_for_update`, bootstrap dispatch + idempotence, the `github.confirmed` lifecycle, and a translation-coverage drift detector that AST-walks every `_(...)` / `_tr(...)` site and asserts the msgid is in the French .po. Run with `pytest tests/ -q` from this repo root. No CI is wired up; the manual matrix in `docs/test_plan.md` §8 covers what the unit tests can't (real Android, real keystore, real network).

`docs/test_plan.md` is the canonical failure-mode list — refer to it before shipping any release that touches the install/update or credential paths. `docs/research_notes_2026-05.md` captures the platform state of the art (Android 16 sideloading lockdown, ACTION_VIEW deprecation, etc.); refresh before each major release.

Daemon-touching changes still get a manual smoke against `examples/sister_app.py` from a sibling app's venv:

```bash
cd ../azt_recorder
source env/bin/activate
python ../azt-collab/examples/sister_app.py
```

## Runtime config

`$AZT_HOME/config.json` holds runtime knobs; env vars override:

| Key | Env var | Default |
|---|---|---|
| `sync.debounce_ms` | `AZT_SYNC_DEBOUNCE_MS` | 500 |
| `sync.merge_retry_max` | `AZT_SYNC_MERGE_RETRY_MAX` | 3 |
| `sync.connectivity_poll_s` | `AZT_SYNC_CONNECTIVITY_POLL_S` | 30 |
| `sync.work_offline` | (UI toggle) | off |
| `sync.commit_pack_byte_budget` | — | 3 MB (0.44.12; was 10 MB) |
| `lan.allow_sync` | (UI toggle) | off |
| dir override | `AZT_HOME` | platform default |
| disable auto-spawn | `AZT_CLIENT_AUTOSPAWN=0` | enabled |

## Android specifics

- Suite signature permission is `org.atoznback.AZT_COLLAB_ACCESS` (`protectionLevel="signature"`). The standalone server APK (`org.atoznback.aztcollab`) is the **only** app that declares the `<permission>` (in `server_apk/manifest_extras.xml`) and exports the `<provider>` at authority `org.atoznback.aztcollab` (injected post-render by `p4a_hook.py:_inject_aztcollab_provider`, gated on `dist_name == 'aztcollab'`). Peer apps consume it via `<uses-permission>` (from `android.permissions` in their spec) plus a `<queries><package .../></queries>` block (from `android/manifest_extras_peer.xml`, symlinked into each peer as `manifest_extras.xml`).
- Expected keystore SHA-256 is in `android/SUITE_FINGERPRINT`. Mismatched signing → install-time permission grant denied → ContentProvider transport silently falls back to loopback.
- Suite apps must call `azt_collabd.android_cp.service.install_callbacks()` in their startup hook (no-op on desktop).
- **Sticky-bound service in a separate process.** The server APK declares `<service android:name="…AZTServiceProviderhost" android:exported="true" android:permission="org.atoznback.AZT_COLLAB_ACCESS" android:process=":provider" />` and the `<provider>` injection carries the same `android:process=":provider"`, so the daemon Python interpreter, the service, and the provider all live in **a different process from the picker Activity**. This isolation is load-bearing: p4a runs Python on the SDLThread inside PythonActivity, so the Activity's process already has one Python interpreter going. Putting Service+Provider in the same process would mean two Python interpreters fighting one GIL, which crashes (`PyImport_AddModule` SIGSEGV in `libpython3.11.so`) on Activity teardown — `dumpsys activity services` shows `crashCount` climbing. With `:provider`, the Activity's Kivy can finish however it wants (clean exit or Python teardown crash) and the daemon's process is unaffected; peer URI grants and openFileDescriptor calls keep working. Service body is `server_apk/service.py`; manifest entries injected post-render by `p4a_hook.py:_inject_aztcollab_service` and `_inject_aztcollab_provider` (both gated on `dist_name == 'aztcollab'`). No foreground notification — the design is "transient when idle, pinned while in use" via bind-priority OOM hint + `START_STICKY` respawn, and the idle-stop policy in `service.py` (5 min default) keeps the process from running indefinitely. Peer apps do NOT declare the service; they may bind to it from the client transport (future work) but no peer code change is required for the lifetime fix to take effect.
- **Recovery semantics.** Under memory pressure the host process can still be killed; the next peer ContentResolver call lazy-spawns it via Android's unconditional ContentProvider contract. `Service.onCreate` re-runs `service.py` which calls `reconcile_on_startup()` to mark in-flight jobs `JOB_INTERRUPTED`. URI grants from the picker are scoped to the receiving Activity's lifetime (not the source process), so they survive source kills. Detached FDs are kernel-managed and survive too. The only ungraceful surface is the typed `JOB_INTERRUPTED` `Result` peers should treat as retryable.
- **LAN sync foreground service.** While `lan.allow_sync` is on, `android_cp/lan_fgs.py` promotes the `:provider` service to a specialUse foreground service and acquires `WIFI_MODE_FULL_HIGH_PERF` WifiLock + `MulticastLock` so mDNS keeps working with the screen off. Manifest gets `<service android:foregroundServiceType="specialUse"/>` + the inner `<property android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" android:value="lan-peer-git-sync" />` injected by `p4a_hook.py:_inject_aztcollab_service`. Required permissions on the server APK: `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `CHANGE_WIFI_MULTICAST_STATE`, `ACCESS_WIFI_STATE`. Toggle off ⇒ all three are released (`apply_toggle` in `lan_listener.py`). Daemon respawn must re-run `apply_toggle` to re-arm everything (server APK `service.py:main()` does this; if you ever wire a new entry point that doesn't, the listener silently doesn't come back up — see CHANGELOG 0.45.25).

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
