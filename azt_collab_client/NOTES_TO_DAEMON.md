# Notes to the daemon

Outstanding items peers have noticed and want the `azt_collabd` /
server-APK side to fix. Filed here (inside `azt_collab_client/`)
rather than in the per-peer CHANGELOG so:

- the symlink propagates them into every sister app's tree
- the daemon team sees them in one canonical place
- the note moves with the package if the canonical home ever
  changes

When you act on an item, delete it from this file (the daemon
CHANGELOG is the historical record; this file is the live queue).

---

## Daemon is now the *sole* authoritative source — no peer-side fallbacks

**Filed:** 2026-05-12, by azt_recorder peer (1.41.3). **Standing
notice — do not delete; this is an architectural invariant the
daemon must honor on every release.**

The recorder peer used to keep "just-in-case" local mirrors of
daemon-owned state (`peer_pref('vernlang')`,
`peer_pref('collab_langcode')`, a defunct
`App.list_projects` that scanned the peer's own sandbox). Those
mirrors are gone as of 1.41.3 per the no-daemon-owned-caches
rule (peer-side memory: `feedback_no_daemon_owned_caches.md`).

That means: **if the daemon returns a wrong, stale, or empty
value for any of the fields below, the peer has no fallback and
the user-visible behavior breaks.** Please treat the
correctness of these as load-bearing.

| Field | Endpoint(s) | What breaks on wrong/empty |
|---|---|---|
| Project langcode (== LIFT vernlang) | `last_project`, `open_project`, `register_project`, `derive_langcode`, `project_status` | LIFT writes use the wrong `lang=` attribute; `progress_text` reads the wrong field; audio filenames are mis-tagged. |
| Recent project (`last_project`) | `GET/POST /v1/recent/last_project` | Auto-resume on startup either skips a valid project or resumes a wrong one. The peer has no local "last opened" mirror anymore. |
| Contributor name | `get_contributor` / `set_contributor` | Commit-issuing endpoints refuse with `CONTRIBUTOR_UNSET`; sync / init blocked. Shipped 0.40.0 as strict daemon-owned (peers no longer pass it on the wire). Don't mirror in peer prefs. |
| Device name (commit author disambiguator) | `GET/POST /v1/config/device_name` | Git commit author email slot falls back to `@unknown` instead of disambiguating multi-device commits. Auto-populates from OS on first read; user-overridable via daemon settings UI. Shipped 0.40.0. |
| UI language | `azt_collab_client.i18n.current_language()` / `set_language()` | UI lands on the wrong locale on every launch — no peer-side cache. |
| Credentials (GitHub/GitLab/host) | `/v1/credentials/*` | Publish/sync silently fails; the peer cannot fall back to a local token store. |
| Project registry (working_dir, lift_path, remote_url) | `list_projects`, `open_project`, `register_project` | Picker can't find the project; publish has no working_dir to push from. |
| Repo slug (per-project override) | `Project.repo_slug` via `open_project`/`list_projects`/`project_status`; setter `POST /v1/projects/<lang>/repo_slug` | Override silently degrades to using `langcode` as the repo name (the typical case anyway). Shipped 0.39.0. Don't mirror the slug into peer prefs. |
| CAWL image_repo (per-project) | `Project.cawl_image_repo` via `open_project`/`list_projects`/`project_status`; setter `POST /v1/projects/<lang>/cawl_image_repo` | Per-project image-set override silently degrades to the daemon-global default. Shipped 0.38.0; peer migration documented in `azt_collab_client/CLAUDE.md` "CAWL image access" section. Don't mirror the slug into peer prefs. |

### Specific obligations

1. **No silent empty.** If a getter can't answer (server starting,
   transient I/O failure), return a clear error, not an empty
   string — the peer treats empty as "user hasn't set it" and
   degrades accordingly. Today this is mostly correct; flagging
   it because we're now relying on it.
2. **Setter durability.** Every setter that writes to
   `$AZT_HOME/config.json` (or its Android-CP equivalent) must
   land on disk before returning OK. Crash-during-write that
   loses the value will surface as user-visible data loss with
   no peer-side copy to recover from.
3. **Project-langcode immutability without a rename RPC.** Peers
   cache the langcode in-memory as `_current_langcode` for the
   life of the load. If the daemon decides to change a project's
   langcode out from under a loaded peer (e.g. during a merge),
   the peer's in-memory copy goes stale and future writes go to
   the old tag. If renames are supported, surface them through
   `rename_project` and have the daemon notify open peers (or at
   minimum make the next `project_status` reflect the new value
   so the peer can refresh on its periodic poll).
4. **Cross-peer convergence.** Setters from one peer must be
   visible to every other peer's getter within "next RPC" time.
   The Android ContentProvider already gives us this; flagging
   so a future daemon refactor doesn't accidentally introduce a
   per-process cache that breaks it.

If you're adding a new field that the peer needs to know, the
default placement is daemon-side, accessed by RPC each time —
do not invite the peer to cache it.

---

## For the peer team: project-bound surfaces now in daemon UI (Phase 3)

**Filed:** 2026-05-12 (recorder 1.41.4 filed Phase 1; daemon
shipped 0.41.0 fulfilling it).

The daemon settings UI (`open_server_ui()` / `python -m
azt_collabd ui`) now hosts the project-bound Publish + Grant
collaborator + Share-repo surfaces, bound to the daemon's
`last_project()` tracking. Peers can strip their per-project
sub-screens in their own CollabScreens and replace with a
single "Open Sync Settings" button that calls
`open_server_ui()` — same pattern peers already use for
GitHub Connect / GitLab credentials.

What's available daemon-side in 0.41.0:

- **Publish** — was already on the daemon UI; updated for the
  0.40.0 wire (peers no longer pass `contributor=` to
  `init_project`).
- **Grant collaborator access** — invokes
  `grant_collaborator_popup(langcode=last_project())`.
- **Share this repo (QR)** — renders the remote URL as a QR
  for pairing with another device. Pairs with the picker's
  new "Scan QR" affordance on the clone flow (same
  release).

Don't combine the peer strip-out with the same release that
ships these (Phase 1 / Phase 3 sequencing per the original
filing) — a peer that strips before the daemon UI is widely
deployed loses the feature for users still on the old
server APK.

---

## Picker "Scan QR" button does nothing (daemon-side investigation)

**Filed:** 2026-05-12, by azt_recorder peer (1.41.5).
**Diagnostic logging added daemon-side in 0.41.1**; if the
button still does nothing, the logcat lines below disambiguate.

The "Scan QR" affordance in the clone-URL popup is visible but
tapping it has no observable effect — no screen transition, no
camera permission prompt, no logcat output. The affordance
lives in the daemon-hosted picker subprocess; peers have no
wiring for it.

Logcat lines to watch for on the next tap (added in 0.41.1):

- `[clone_url_popup] Scan QR tapped` — button event fires.
  If absent: the bind didn't take or Kivy is intercepting the
  touch. Check whether the URL textbox's focus eats the tap;
  try switching `on_release` to `on_press` in
  ``popups.py:clone_url_popup`` if so.
- `[clone_url_popup] scan_qr raised: ...` — exception inside
  ``qr_scan.scan_qr`` was swallowed silently before. Now
  surfaced. The exception type + message tells the rest.
- `[qr_scan] scan_qr called` — entry into the helper.
  If absent and `[clone_url_popup] Scan QR tapped` was
  present: the import of `qr_scan` failed at lambda-build
  time (rare).
- `[qr_scan] not available on this platform; ...` — `available()`
  returned False (no jnius / non-android). Shouldn't happen
  on Android.
- `[qr_scan] cannot import android.activity: ...` — p4a
  ``android`` module missing from the APK. APK build is
  broken.
- `[qr_scan] ZXing classes unresolvable (zxing-android-embedded
  missing from APK?): ...` — Gradle dep not pulled. Check
  ``server_apk/buildozer.spec`` has
  ``android.gradle_dependencies = com.journeyapps:zxing-android-embedded:4.3.0``
  AND that `buildozer android clean && buildozer android debug`
  was run after adding it (the dist tree caches Gradle
  resolution).
- `[qr_scan] PythonActivity.mActivity is None` — picker
  process has no Activity context at the moment of the tap.
- `[qr_scan] launching IntentIntegrator.initiateScan` — about
  to fire the Intent. If this line appears and nothing
  happens visually, the issue is downstream of Python
  (ZXing's CaptureActivity not finding camera permission at
  runtime, etc.) — check unfiltered logcat at the same
  moment for `AndroidRuntime` / `ActivityManager` lines.

User retest after the next build will tell us which line
surfaces. Daemon team: if the line shape doesn't match any of
the above, file a follow-up rather than guess.
