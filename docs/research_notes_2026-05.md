# Research notes — May 2026

State-of-the-art for the technologies the suite depends on, captured
for the bootstrap / install-update rollout. Each section ends with
**implications for us**. Re-do this pass before each major release;
the moving pieces below have all shifted in the last six months.

## 1. Android platform

- **Android 16 is current stable.** API level 36 (we already target
  this; `android.api = 36`, `android.minapi = 26` in every
  buildozer.spec).
- **Sideloading lockdown — March 2026.** Google rolled out mandatory
  developer verification for *all* APKs across consumer devices.
  Sideloading an APK whose developer/package isn't verified through
  Google's verification system results in installer refusal on
  affected devices — *no exceptions*, including custom app stores.
  Enforcement scope is being expanded gradually by region and device
  class; some users in our SIL field-linguist target audience are on
  devices that haven't received the enforcement yet (older OEMs in
  low-connectivity regions), but new devices ship with it on by
  default.
- **`REQUEST_INSTALL_PACKAGES` is now a "restricted setting".** The
  permission still exists, but Android 16's CDD lists it as
  `AppOpsManager.OPSTR_REQUEST_INSTALL_PACKAGES` with restricted-
  settings flow. Effect on us: the user must (a) flip the
  per-source "Install unknown apps" toggle (existing requirement,
  Android 8+), AND (b) confirm a system-level dialog the first time
  the OS observes our install-attempt path on Android 16. Our
  current `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` deep link
  still works but no longer covers the whole flow.
- **`Intent.ACTION_VIEW` for APK install is "frowned upon" and
  unreliable on 16.** `ACTION_INSTALL_PACKAGE` was deprecated in
  API 29; `ACTION_VIEW` with the APK MIME type is the legacy
  fallback we currently use in `update.py:_trigger_install`.
  Modern recommendation is `android.content.pm.PackageInstaller` —
  more code, more robust, supports split APKs, and the path Google
  considers blessed.

**Implications for us:**

1. **Blocking gap for the field-linguist target audience: we are
   not enrolled in Google's developer verification.** Without
   enrollment, our users on freshly-bought devices won't be able to
   sideload `azt_collab.apk` or any peer APK after March 2026.
   Action items, ranked:
   - Investigate enrolling the suite under a single developer
     identity (likely tied to SIL's existing Google identity — same
     account that signs the suite keystore).
   - Until enrollment, document the workaround for affected users
     (developer options → toggle off-device verification, where
     still possible) AND the read-the-tea-leaves caveat that this
     loophole won't last.
   - Long-term: move suite distribution to a verified channel
     (Play Store or F-Droid) — F-Droid breaks our suite signature
     contract (they re-sign), so Play Store is the realistic path.
     Cost: opens a separate review surface but removes the
     sideloading wrinkle entirely.
2. **Migrate `update.py` from `ACTION_VIEW` → `PackageInstaller`.**
   Not blocking for v1 (ACTION_VIEW still works on 16, just
   deprecated and prone to unannounced tightening). Plan for v2.
3. **Update test plan** to include Android 16 + the restricted-
   settings dance. Our previous "Android 14 newest" assumption is
   wrong by one major version.

## 2. Buildozer / python-for-android

- **Buildozer current: 1.5.x** (master + 1.5.1.dev0 in docs).
  Repo last pushed within the last week.
- **Python support:** master branch supports Python ≤ 3.12 stable;
  develop branch requires Python 3.14 (pre-release as of May 2026).
  We currently use Python 3.13 in the desktop venv and let
  buildozer pick its own bundled Python for the APK build (default
  3.11 in our `.buildozer/`).
- **Target API:** new buildozers default to API 31 to match Play
  Store minimums. We override to API 36 for Android 16 access.

**Implications for us:**

1. Pin buildozer to a specific tag in `setup_from_nuke.sh` rather
   than tracking master. The "1 week since last push" cadence
   means master can move under us between rebuilds.
2. Validate that p4a's master branch (which we use via
   `p4a.branch = master`) still produces working APKs against
   Python 3.13 host.

## 3. Kivy

- **Kivy current stable: 2.3.1.** Compatible with Cython 3.0.
  Standard for Android builds.
- No major API breaks affecting our screens / popups /
  ScreenManager / Clock plumbing.

**Implications for us:** none right now. Pin the Kivy version
explicitly in each peer's `requirements` line in buildozer.spec
(currently we let p4a pick, which is fine but not reproducible).

## 4. GitHub API (releases)

- `/repos/{owner}/{repo}/releases/latest` semantics unchanged:
  excludes drafts; **includes prereleases** (we filter for these
  in v0.28.x).
- Anonymous rate limit: 60 requests / hour / IP. Hits us when a
  single user has multiple peers all calling bootstrap on launch
  in quick succession from the same NAT — could trip the limit.
- `User-Agent` header is required (we set it).
- New (2026): `release.discussion_url`, `release.is_immutable`,
  `release.uploader_login` — informational, no impact.

**Implications for us:**

1. Filter `prerelease=true` releases from the bootstrap path —
   suite users shouldn't be opted into betas without intent.
2. Cache the latest-release probe result per session (per process)
   so a peer that's run repeatedly doesn't drain rate-limits.
3. Document optional `Authorization: Bearer <PAT>` header for
   higher-volume installations (5000/hour authenticated).

## 5. dulwich / urllib

- **dulwich** continues to be the only Python-pure git
  implementation; no major changes affecting our use. We pin
  version via p4a recipe.
- **urllib** stdlib: no API changes affecting us. TLS 1.3 is
  default; the older TLS handshake fallback we had to think about
  in 2023 is moot.

## 6. ContentProvider / MediaStore (Android-side surface)

- `MediaStore$Downloads.EXTERNAL_CONTENT_URI` insert + write
  pattern continues to be the cleanest way to get a content URI
  for a downloaded file without configuring a FileProvider.
  Unchanged on Android 16.
- `FLAG_GRANT_READ_URI_PERMISSION` semantics: unchanged.
- The post-Android-11 `<queries>` package-visibility requirement
  (already in our peer manifest) is still correct.

## 7. Other notable industry shifts (May 2026)

- **Google Play Console** now requires per-region content rating
  re-attestation annually (irrelevant to us, but flag for any
  Play Store distribution path).
- **Android emulators** ship Android 16 as default since
  Q1 2026 — useful for the test matrix, see test_plan.md.

---

## Action items captured here, owned across the codebase

(Each links to the section that motivates it.)

1. **[blocking before March-2026 affected devices]** Enroll the
   suite in Google's developer verification — see §1. Without
   this, the bootstrap workflow's whole download-and-install path
   is impotent on enforced devices.
2. **[v2]** Migrate `_trigger_install` to `PackageInstaller` — §1.
3. **[v0.28.1]** Filter `prerelease=true` from the latest-release
   probe in `bootstrap.py` and `update.py` — §4.
4. **[v0.28.1]** Per-process cache of the version-probe result so
   bootstrap doesn't re-hit the GitHub API on every relaunch in
   the same process — §4.
5. **[v0.28.1]** Persist a "user declined version X" key in
   `$AZT_HOME/config.json` so we don't keep prompting for the
   same release after an explicit "Not now" — see test_plan.md.
6. **[v0.28.1]** Distinguish "server APK absent" from "no
   network" by probing `PackageManager.getPackageInfo(
   'org.atoznback.aztcollab')` before issuing the install prompt.
7. **[continuous]** Refresh these notes before every major
   release. The 2026 Android sideloading shift in particular is
   moving fast.
