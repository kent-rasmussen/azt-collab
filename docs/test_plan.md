# Test plan — install / update / bootstrap workflows

Captures every failure mode we know of for the
`bootstrap` → `check_for_update` → install path. Tagged for
**Auto** (CI-runnable, no device), **Mocked-Auto** (CI-runnable
with jnius / kivy.utils.platform monkey-patched), and **Manual**
(needs an Android device). Re-evaluate against
`research_notes_2026-05.md` before each release; the platform is
moving fast.

Tests live in `azt-collab/tests/` (added in v0.28.1). Run with
`pytest tests/` from the repo root. CI hookup is left to the
caller — no GitHub Actions yet.

## 1. Network

| # | Case | Expected | Tag |
|---|---|---|---|
| 1.1 | Fully offline at startup | `check_server_compat` raises → bootstrap routes to `_check_self`; self-probe also fails → `on_done` fires | Auto |
| 1.2 | Online but `api.github.com` unreachable | "Update check failed: …" via on_status; peer continues | Auto |
| 1.3 | GitHub rate-limited (60/hr unauthed) | 403 → "Update check failed: http_403"; peer continues | Auto |
| 1.4 | Captive portal returns HTML | `json.load` raises → routed to on_error, peer continues | Auto |
| 1.5 | TLS handshake fails (corp MITM) | URLError; peer continues | Auto |
| 1.6 | Connection drops mid-download | `_download` raises; `.part` file left behind | Auto + manual cleanup verify |
| 1.7 | Slow connection (50 KB/s, 10 MB APK) | Progress label updates roughly every 1.3s; user can back out | Manual |
| 1.8 | IPv6-only network | urllib chooses correctly | Manual |
| 1.9 | DNS failure | URLError; peer continues | Auto |

**Automation:** `unittest.mock.patch('urllib.request.urlopen')` with
canned `release.json` payloads. See `tests/test_check_for_update.py`.

## 2. Server APK presence + version

| # | Case | Expected | Tag |
|---|---|---|---|
| 2.1 | Fresh device, server APK absent | `server_unreachable` → "Install AZT Collaboration?" | Manual + Mocked-Auto |
| 2.2 | Server installed at current version | Skip to self-check | Manual |
| 2.3 | Server installed at < `MIN_SERVER_VERSION` | `server_too_old` → update prompt | Manual + Mocked-Auto |
| 2.4 | Server signed with wrong keystore | `<uses-permission>` denied at install → ContentProvider unreachable → looks like absent → install prompt → install attempt → Android refuses (sig conflict). User must uninstall first | Manual; **document UX explicitly** |
| 2.5 | Server APK present but Python crashed at import | Provider authority claimable; `ping` fails; treated as `server_unreachable`. Existing behavior; OK | Manual |
| 2.6 | Server uninstalled mid-session | Next RPC raises `ServerUnavailable`; transport `reset()` re-discovers; bootstrap not re-run | Manual; **gap — see §10.5** |
| 2.7 | Server *newer* than peer's `MIN_SERVER_VERSION` | Compat ok; skip to self-check | Manual |
| 2.8 | Two peers run bootstrap simultaneously, server absent | Two prompts, two downloads (race) | Manual; **gap — see §10.6** |

## 3. Peer (client) version

| # | Case | Expected | Tag |
|---|---|---|---|
| 3.1 | Peer at latest release | `_peer_update_with_confirm` → no_update → on_done | Auto |
| 3.2 | Peer N versions behind | "Update {name}?" prompt | Auto |
| 3.3 | Peer *newer* than GitHub's latest (dev build) | no_update; **must not** offer downgrade | Auto |
| 3.4 | Empty / malformed `__version__` | `_version_tuple` returns (0,0,0) → prompts; acceptable but flag | Auto |
| 3.5 | Bad `peer_repo` (404) | "Update check failed: http_404" → on_done | Auto |
| 3.6 | Asset filename doesn't match release assets | "no {file} in release {tag}" → on_done | Auto |
| 3.7 | Date-tagged release (`v2026-05-06`) | `_version_tuple` chunk-strips → (2026, 5, 6); works incidentally | Auto |
| 3.8 | Release marked `prerelease=true` | **v0.28.0 ships them**; v0.28.1 filters them | Auto (regression) |

## 4. Android permissions (on Android 16 — current stable as of 2026-05)

| # | Case | Expected | Tag |
|---|---|---|---|
| 4.1 | `REQUEST_INSTALL_PACKAGES` missing from peer's buildozer.spec | Install Intent silent no-op; status stuck | Manual; **build-time guard recommended** |
| 4.2 | Permission declared, "Install unknown apps" toggle OFF | `canRequestPackageInstalls()` False → routes to `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` | Manual |
| 4.3 | Toggle ON, **Android 8–15** | Install proceeds | Manual |
| 4.4 | Toggle ON, **Android 16** | Install proceeds *iff* developer-verification check passes (post-March-2026 enforcement). Without enrollment, system installer rejects with verification-failed error | Manual; **see research_notes §1 — distribution-blocking** |
| 4.5 | Android 7 or older | `canRequestPackageInstalls` raises (API < 26); helper falls through | Manual on legacy emulator |
| 4.6 | User revokes permission mid-session | Next install attempt routes back to settings page | Manual |

## 5. Storage / device

| # | Case | Expected | Tag |
|---|---|---|---|
| 5.1 | Less free space than APK size | Download fails partway; `.part` left | Auto with tmpfs quota; or manual |
| 5.2 | `$AZT_HOME` not writable | `os.makedirs` raises → "Could not create download dir" | Auto (chmod 0 fixture) |
| 5.3 | MediaStore insert refused (Q+ scoped storage edge cases) | "Install failed: MediaStore insert refused" | Manual |
| 5.4 | Two simultaneous downloads writing same dest | `os.replace` atomicity holds; one wins | Auto |

## 6. User input

| # | Case | Expected | Tag |
|---|---|---|---|
| 6.1 | "Not now" on server install | `on_done` fires; peer continues with reduced functionality | Mocked-Auto |
| 6.2 | "Not now" on server *update* | Peer continues; first RPC against old server returns informative Result, not stack trace | Manual |
| 6.3 | Tap outside popup | `auto_dismiss=False`: popup stays open | Mocked-Auto |
| 6.4 | Android Back while popup open | Popup absorbs back press | Manual |
| 6.5 | Double-tap "Install" | Second tap on dismissed popup is no-op | Manual |
| 6.6 | User backgrounds peer mid-download | Worker continues; install Intent fires on resume | Manual |
| 6.7 | User Backs out of Android system installer | Status stays "Installing…" forever; **gap — see §10.1** | Manual |
| 6.8 | User force-stops peer mid-install | Install continues; peer reopens fresh | Manual |
| 6.9 | User declines self-update repeatedly | v0.28.0: re-prompts every launch (annoying). v0.28.1: skipped after explicit decline of that version | Auto regression |

## 7. Process / concurrency

| # | Case | Expected | Tag |
|---|---|---|---|
| 7.1 | bootstrap() called twice in one session | v0.28.0: two parallel workflows. v0.28.1: idempotence guard suppresses second call | Auto |
| 7.2 | Server APK killed by Android OOM mid-`check_server_compat` | RPC retries, `ServerUnavailable`, install prompt fires for an installed server (false positive). v0.28.1 distinguishes via `getPackageInfo` | Manual + Mocked-Auto |
| 7.3 | Bootstrap mid-flow + user opens picker | RPC timeouts likely; surface as transient | Manual |
| 7.4 | Language change while popup open | Popup text doesn't live-retranslate. Acceptable | Manual |
| 7.5 | Two peers install server simultaneously | Android serializes; second user lands on "package replaced" | Manual; rare |

## 8. End-to-end matrices (each release)

A. **Cold install path:** wipe device → install peer APK → first launch → server install prompt → install → re-open peer → normal startup.
B. **Stale peer:** install peer N-1 → publish N → launch → self-update prompt.
C. **Stale server:** server old, peer current → bump server `MIN_CLIENT_VERSION` → publish → launch → server update prompt.
D. **Both stale:** stale peer + stale server → server prompt → server install → peer reopens → self-update prompt. Currently breaks at step 2 — see §10.2.
E. **Decline path:** every prompt → "Not now" → reaches `on_done` and doesn't crash.
F. **Cross-version Android matrix:** repeat A on Android 8 (oldest supported, hardest permission UX), Android 14 (mid), and Android 16 (newest, restricted-settings + verification dance).

## 9. Internationalization

| # | Case | Expected | Tag |
|---|---|---|---|
| 9.1 | Device locale `fr_FR` | All bootstrap prompts in French | Manual smoke |
| 9.2 | Locale `de_DE` (no catalog) | Falls through to English msgids | Auto |
| 9.3 | msgid drift (source string not in .po) | Falls through to msgid | Auto: AST-walk source for `_(...)` and `_tr(...)` callsites and assert each is in the .po |

## 10. Known gaps + status

Owned items, ranked by user-visible severity. Numbered for cross-
reference from the tables above.

1. **No "after install completes" callback.** Peer dispatches install
   Intent; status sticks at "Installing…" if user Backs out of the
   system installer or the install fails silently. **Fix planned for
   v0.28.2:** poll `PackageManager.getPackageInfo` for the new
   versionCode every 2s for ~60s, fire `on_done` when seen.
2. **"Not now" on server install bypasses self-update.** Acceptable
   for now (peer can't run without server anyway), but a stale peer
   wanting to self-update first is currently stuck. Re-evaluate.
3. **No bootstrap-fired-twice guard.** Fix in v0.28.1 — module-level
   `_running` flag.
4. **No version-already-declined memory.** Fix in v0.28.1 —
   `$AZT_HOME/config.json :: bootstrap.declined.<repo>=<version>`.
5. **`server_unreachable` ≠ "server APK absent".** Fix in v0.28.1 —
   `PackageManager.getPackageInfo('org.atoznback.aztcollab')` probe
   before prompting; only show install prompt if package missing.
   Falls back to `server_unreachable` semantics when neither network
   nor package is the problem.
6. **No SHA verification** of the downloaded APK. Documented; defer
   to v2 if a `.sha256` companion asset becomes part of the release
   pipeline.
7. **Pre-releases auto-installed.** Fix in v0.28.1 — filter
   `prerelease=true` and walk `/releases` until a stable one is
   found.
8. **Build-time guard for `REQUEST_INSTALL_PACKAGES`.** Add a grep
   in `setup_from_nuke.sh` that fails the build if any peer's
   buildozer.spec lacks the permission.
9. **`ACTION_VIEW`-based install is "frowned upon" on Android 16
   and may break without notice.** Migrate `_trigger_install` to
   `PackageInstaller` — v2 work, see research_notes §1.
10. **Sideloading lockdown — March 2026.** Suite is not enrolled
    in Google's developer verification. Distribution blocked for
    affected users post-enforcement. **This is not a code fix; it's
    a process change** owned outside the codebase. See research_notes §1.

## 11. Test scaffolding (v0.28.1)

`azt-collab/tests/`:

```
conftest.py                  # tmp_path-based $AZT_HOME, kivy.utils.platform monkeypatch
test_version_tuple.py        # _version_tuple corner cases
test_translation_coverage.py # AST-walk source for _(...)/tr(...), assert in .po
test_store_confirmed.py      # github.confirmed lifecycle (set/clear-on-change)
test_check_for_update.py     # mocked urlopen, jnius, MediaStore
test_bootstrap.py            # bootstrap dispatch + idempotence + decline-memory
```

Network mocks: `unittest.mock.patch('urllib.request.urlopen')`
returning prebuilt fake `urlopen` context managers with canned
JSON. jnius mocks: a `tests/_jnius_stub.py` shim that registers a
fake `jnius` module in `sys.modules` before the import-under-test
runs. Kivy popup tests use the headless dispatch path
(set `KIVY_NO_ARGS=1`, `KIVY_NO_FILELOG=1` env vars in conftest).

Run with `pytest tests/ -q`. No CI configured yet — caller wires
this up.
