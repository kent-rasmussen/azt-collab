# Changelog

Two packages live here. Versions move together for now (the client
embeds `MIN_SERVER_VERSION`, so when the wire format changes we bump
both); patch-level bumps in one without the other are fine.

- **azt_collabd** — server / daemon. Source of truth: `azt_collabd.__version__` (re-imported by `server.py` as `_VERSION` for the wire response).
- **azt_collab_client** — client library. Source of truth: `azt_collab_client.__version__`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely.

## [Unreleased]

### azt_collabd 0.28.18 + azt_collab_client 0.28.18 — move CLIENT_INTEGRATION.md into the symlinked package
- ``CLIENT_INTEGRATION.md`` moved from ``docs/`` (canonical-repo
  only) to ``azt_collab_client/`` (symlinked into every peer).
  Peers now see the integration contract through their existing
  symlink without needing a separate ``azt-collab/`` checkout.
  Old ``docs/CLIENT_INTEGRATION.md`` is reduced to a one-line
  redirect for anyone with the old path bookmarked.
- Added missing section on ``on_done`` semantics (introduced in
  0.28.5 but the doc never reflected it). Renumbered subsections
  to fix a duplicate ``## 6`` heading. Added the
  ``install_apk_from_url`` entry to the "what the suite does for
  you" list (added in 0.28.10, doc never updated).
- ``azt_collab_client/CLAUDE.md`` updated to point at the new
  in-package location.

### azt_collabd 0.28.17 + azt_collab_client 0.28.17 — peer self-update gets the same progress UI as server install
- **Peer self-update now uses the same popup as the server case**
  with progress visible in the body. Previously
  ``_prompt_self_update`` showed a Yes/No popup that dismissed on
  Update tap, then ran ``install_apk_from_url`` "in the background"
  with status flowing only to the host's ``on_status`` sink — which
  meant the user saw nothing until the install finished. Now
  bootstrap calls ``install_server_apk_popup`` (parameterized for
  the peer's own APK) so the same body-label progress (downloading
  %, retrying status, "Installing…", "Installed.") is on screen
  through the entire flow. Closes the user-reported "looks like
  it's stuck — Update just means OK" symptom.
- **``install_server_apk_popup`` is now a generic install/update
  popup**. New parameters:
  - ``direct_url`` — overrides the composed download URL.
  - ``asset_filename`` — overrides the on-disk staging name +
    MediaStore display name.
  - ``open_page_url`` — overrides the "Open install page" target
    so self-update points at the peer's release page instead of
    the server's.
  - ``dismiss_label`` — overrides the dismiss button label.
  - ``dismiss_action`` — ``'quit'`` (default; closes app) for the
    server case; ``'dismiss'`` for self-update where declining
    means "stick with current version, peer keeps running".
  - ``install_target_package=''`` — explicit "no polling" sentinel
    for self-update where the install kills the running peer
    process.
- **Self-update decline records the version only on Not-now tap.**
  Previously the decline was recorded synchronously in
  ``_prompt_self_update``'s decline handler. The new popup-based
  flow records on dismiss, but only when no install was started
  — if the user tapped Update and the install kicked off, no
  decline gets recorded (the next launch will detect the new
  version naturally instead).
- ``_yes_no`` helper removed; no callers left after the refactor.
- Lock-step bump 0.28.16 → 0.28.17 with ``MIN_SERVER_VERSION``
  raised to match (continues the user's "test the server-update
  path" workflow — every iteration of the bump fires the
  too-old-server prompt for a peer that bundles the new client).

### azt_collabd 0.28.16 + azt_collab_client 0.28.16 — bump MIN_SERVER_VERSION to test server-update path
- Lock-step debug bump 0.28.15 → 0.28.16 across both packages.
- ``azt_collab_client.MIN_SERVER_VERSION`` raised 0.27.0 → 0.28.16.
  Forces a rebuilt peer (which bundles client 0.28.16) to refuse
  any server APK older than 0.28.16. Test path: install peer with
  bundled 0.28.16, leave the older server APK (0.28.15 or earlier)
  on the device, launch peer → ``check_server_compat`` returns
  ``server_too_old`` → bootstrap fires
  ``_prompt_server_update`` → install popup shows the
  "Update AZT Collaboration?" body with the "Update" button label
  and pre-filled current_server_version (so the daemon doesn't
  redownload an identical release if there isn't actually a
  newer one published).
- ``MIN_CLIENT_VERSION`` (in azt_collabd) stays at 0.27.0 — this
  bump is for testing the server-too-old path, not client-too-old.

### azt_collabd 0.28.15 + azt_collab_client 0.28.15 — fix Install button stuck after "unknown apps" detour
- **"Tap Update again" message corrected** to use the actual
  install-button label. The popup's button is "Install" (or
  whatever ``install_label`` the caller passed), not "Update", so
  the previous message ("…then tap Update again") was wrong for
  every caller except the settings-screen Update buttons. Now uses
  ``{label}`` substitution and the popup passes its own button
  text.
- **New ``on_user_action_needed`` callback** in both
  ``install_apk_from_url`` and ``check_for_update``. Fires when
  the install path stalls because Android needs the user to flip
  "Install unknown apps" for this peer in Settings. Without this,
  the popup's Install button stayed disabled forever after we
  routed the user to settings — only Quit was active. The popup
  now wires this callback to re-enable Install + Open install
  page so the user can come back from Settings and retry.
- ``install_label`` parameter added to both functions so callers
  can override the label used in the "tap {label} again"
  message. Defaults to "Install" for ``install_apk_from_url``,
  "Update" for ``check_for_update`` (matching their
  conventional UX context).

### azt_collabd 0.28.14 + azt_collab_client 0.28.14 — fix language-toggle inertness + URL overflow in error
- **Language toggle in `install_server_apk_popup` now actually
  switches.** The handler called ``popup.dismiss()`` then
  ``install_server_apk_popup(...)`` synchronously from inside a
  Button.on_release — Kivy silently no-ops that re-entrance in
  some versions because the original popup is mid-dismiss. Fix:
  defer dismiss + relaunch via ``Clock.schedule_once(..., 0)`` so
  the touch handler returns first. Also added stderr logging at
  every step (``[install_popup] language switch: fr``,
  ``[install_popup] dismiss raised:`` …) so any future failure is
  diagnosable via ``adb logcat``.
- **Long URLs in error messages now wrap inside the popup.**
  ``_download``'s 404-with-URL surface for a 60-character GitHub
  asset URL was running off the body label because Kivy Labels
  only break at whitespace and URLs have none. New
  ``_wrappable_url(url)`` helper in ``update.py`` inserts a real
  ``\n`` after each ``/`` (only when the URL is over 50 chars) so
  the URL renders across multiple lines inside the popup body.
  Display is uglier but legible — readable URLs trump pretty
  ones for diagnosis.

### azt_collabd 0.28.13 + azt_collab_client 0.28.13 — diagnostic logging + browser-like headers in download
- ``_download`` now logs to stderr (visible in ``adb logcat``) at
  every meaningful step: the URL it's about to GET, the redirect
  target if any, the HTTP status, and the URL the server actually
  served the 404 from (``HTTPError.url``). Disambiguates
  "github.com returned 404" from "github.com 302'd to the CDN, CDN
  returned 404" — different diagnoses (asset truly missing vs.
  bot-pattern rejection on the CDN edge or expired-token edge
  case).
- Added ``Accept: */*`` and updated the User-Agent string to
  ``'azt-collab-updater/1 (+curl-compat)'``. Some GitHub CDN edges
  return 404 to bare-pattern UAs; mimicking curl removes that
  variable.
- Diagnostic-only round; the underlying 404 puzzle (browser works,
  ``gh release view`` confirms the asset, but Python urllib gets
  404 three times) is still being investigated. The new logging
  should make the next reproduction definitively diagnosable.

### azt_collabd 0.28.12 + azt_collab_client 0.28.12 — popup polish: language toggle, version footer, URL in error
- **Discrete language toggle** at the top of
  ``install_server_apk_popup``. First-install users whose device
  locale is French (or any non-English) had no way to switch
  language since the popup blocks the settings UI; now there's a
  small row of language buttons (current bolded, others tappable)
  that dismisses + re-opens the popup with the chosen language.
  Only shown when ``i18n.available_languages()`` returns more than
  one — desktop hosts running an English-only build won't see it.
- **Version footer** at the bottom of the popup
  (``client X.Y.Z``). Subtle / dim, mirrors the version strip
  pattern from the daemon settings UI. Helps diagnose which
  client build is actually live when reproducing UI bugs across
  versions.
- **Download error includes the URL we tried.** When the asset
  download fails (404 from the well-known direct URL, or any
  other transport error), the surfaced message now appends the
  URL on a new line. Lets the user eyeball the URL against what
  their browser successfully fetches — most 404s on this path
  come from an asset-name mismatch on the GitHub release, not a
  transport bug.

### azt_collabd 0.28.11 + azt_collab_client 0.28.11 — popup button wrapping, URL composition cleanup
- **Install popup button text wraps now.** Previously "Open install
  page" and "Quit AZT Recorder" / "Quit AZT Viewer" got clipped on
  narrower screens because Kivy Buttons don't wrap by default.
  ``text_size`` is now bound to button size on all three buttons
  (``halign='center'`` + ``valign='middle'``), and the button row
  height bumped to dp(60) to allow two-line wraps. Popup overall
  height also bumped from dp(280) to dp(300) to compensate.
- **Direct-URL composition consolidated** into the install popup's
  ``_do_install``. The hardcoded ``_DIRECT_DOWNLOAD_URL`` constant
  in ``update.py`` is gone; the popup now composes
  ``f'https://github.com/{_SERVER_REPO_DEFAULT}/releases/latest/download/{_SERVER_ASSET_DEFAULT}'``
  from the same constants the package-presence probe uses
  (``bootstrap._SERVER_REPO_DEFAULT``,
  ``bootstrap._SERVER_ASSET_DEFAULT``). Single source of truth: a
  fork that wants to point its server-install at a different
  release feed only edits the bootstrap constants.

### azt_collabd 0.28.10 + azt_collab_client 0.28.10 — direct-URL install for popup + peer self-update
- **New ``install_apk_from_url(url, asset_filename, ...)``** in
  ``azt_collab_client.ui.update``. Direct-URL alternative to
  ``check_for_update``: GETs the URL, streams to disk, dispatches
  Android's installer, optionally polls for completion via change-
  detection. No GitHub API call, no JSON parsing, no asset name
  matching, no listing-endpoint quirks. For when the caller has a
  stable redirect URL like
  ``releases/latest/download/<asset>`` and doesn't need version
  comparison.
- **`install_server_apk_popup`** now uses ``install_apk_from_url``
  for the Install button. Closes the user-reported "install
  comes back 404" symptom — the API path's ``_pick_asset`` step
  was failing on edge cases (asset-name mismatch, listing
  endpoint quirks). Direct URL bypasses the entire failure
  surface.
- **`bootstrap._do_self_install`** also migrated. Composes
  ``f'https://github.com/{peer_repo}/releases/latest/download/{peer_asset_filename}'``
  from the args bootstrap already takes. Version comparison
  still happens earlier in ``_peer_update_with_confirm`` (which
  needs the API for the small tag-name lookup); by the time we
  reach the install action, the user has confirmed the prompt
  and we just need to install whatever's at the URL. No
  ``install_target_package`` because self-install replaces the
  running peer process.
- **`_start_install_poll`** now supports two modes — pinned-
  version (used by ``check_for_update`` when it knows what
  version it just downloaded) and change-detection (used by
  ``install_apk_from_url`` which doesn't have version metadata).
  Change-detection snapshots the current installed versionName at
  start, then polls for any difference; trivially handles the
  uninstalled→installed case.
- **`_download`** now reads ``Content-Length`` from the response
  headers when the caller doesn't pre-supply ``total_bytes``, so
  progress percentages work for the direct-URL path too.
- **Settings-screen "Update this app" buttons** (CollabUIApp +
  PickerApp) stay on ``check_for_update`` because they want the
  "Up to date" message when the user taps without a newer version
  available — that's the value of the API path there.

### azt_collabd 0.28.9 + azt_collab_client 0.28.9 — restore "Open install page" semantics
- ``SERVER_APK_INSTALL_URL`` reverts to the **release page** URL
  (``https://github.com/kent-rasmussen/azt-collab/releases/latest``),
  not the direct-download asset URL. The popup's "Open install
  page" button is for users who want to read release notes or
  browse the project before installing — the page is what serves
  that purpose. The "Install" button in the same popup is the
  one-tap-to-install path; it discovers the asset URL via the
  GitHub API at runtime (asset['browser_download_url']) rather
  than from this constant.
- Effectively reverts the 0.28.3 URL-direction change. Sole
  consumer of ``SERVER_APK_INSTALL_URL`` is
  ``install_server_apk_popup._open_page``; everything else (the
  bootstrap workflow, ``check_for_update``) computes the
  direct-download URL itself.

### azt_collabd 0.28.8 + azt_collab_client 0.28.8 — fix SSL on Android urlopen
- p4a doesn't ship system CA certs into the Android Python
  runtime, so the new client-side ``urllib.request.urlopen`` calls
  in ``azt_collab_client.ui.update`` (release listing, asset
  download) fail with "unable to get local issuer certificate".
  The daemon side has had ``azt_collabd/net.py:_ensure_ssl()`` for
  this since forever, but the client can't import it (Hard rule 3:
  no daemon import; the two run in different processes on Android
  anyway).
- New ``azt_collab_client/net.py`` mirrors the daemon's SSL patch,
  slimmed for the client (no urllib3.PoolManager surface — the
  client doesn't speak dulwich). ``_ensure_ssl()`` is idempotent
  and called at the top of every urlopen site in ``update.py``.
  Finds certifi's bundle (preferred), falls back to extracting it
  from the bundled zip into ``$ANDROID_PRIVATE/cacert.pem``, then
  to common system locations, then to a verification-disabled
  context as a last resort.
- Symptom this fixes: post-popup-open SSL error on the bootstrap
  install flow's GitHub release probe — the "is a newer release
  available" call (``_fetch_latest``) and the asset-binary stream
  (``_download``) both bypassed SSL setup before this fix.

### azt_collabd 0.28.7 + azt_collab_client 0.28.7 — fix ModuleNotFoundError on popup open
- One-character relative-import bug introduced in the 0.28.4 popup
  refactor: ``azt_collab_client/ui/popups.py:369`` had
  ``from ..bootstrap import …`` (resolves to
  ``azt_collab_client.bootstrap`` — doesn't exist) instead of
  ``from .bootstrap import …`` (the correct
  ``azt_collab_client.ui.bootstrap``). The error fired the moment
  ``install_server_apk_popup`` was opened on a peer with no server
  installed, which raised inside Kivy's main loop and took the
  peer down — visible as "presplash, brief app screen, close".
  The user-reported symptom from 0.28.4 onward; the 0.28.5 / 0.28.6
  bootstrap fixes never had a chance to run because the popup
  itself couldn't load.

### azt_collabd 0.28.6 + azt_collab_client 0.28.6 — auto-resume after server install
- **Post-install continuation.** Once the install-completion poll
  watchdog confirms the new server APK is live, the popup auto-
  dismisses (after a 1-second visual confirmation showing
  "Installed.") and bootstrap re-enters its compat check. Daemon
  is now reachable, so the healthy path takes over and on_done
  fires, letting the host continue normal startup. No manual
  Quit + relaunch needed.
- New ``check_for_update(on_install_complete=...)`` parameter,
  threaded through ``_start_install_poll``. Fires only on
  confirmed completion (versionName flipped), not on the
  watchdog timeout — that branch still leaves the user with the
  "Install pending" message and the popup up.
- New ``install_server_apk_popup(on_install_complete=...)``
  parameter wires the upstream callback to (a) dismiss the popup
  after a 1s delay and (b) call the host's continuation. Bootstrap
  passes a continuation that schedules a 2-second daemon-warm-up
  pause (Android lazy-spawns the ContentProvider host on first
  call) before re-running ``_check_server``.
- If Android kills the peer process during install (memory
  pressure, system installer dominating), the popup + its
  continuation chain are gone too. Re-launch triggers a fresh
  bootstrap, which finds the daemon reachable and flows through
  the healthy path — same outcome, just one extra user action.

### azt_collabd 0.28.5 + azt_collab_client 0.28.5 — fix flash-then-die regression in 0.28.4
- **Bootstrap no longer fires on_done before the no-server popup
  opens.** In 0.28.4 the prompt branches called
  ``_on_done_and_release(ctx)`` immediately, then opened the popup
  on the next UI tick. Hosts whose ``on_done`` is wired to
  "continue normal startup" then attempted RPCs against a daemon
  that wasn't there yet, the failure cascaded into App.stop() (or
  similar in the host's error handling), and the popup that was
  about to open was killed alongside it — visible as a screen flash
  then peer shutdown.
- New ``_release_running()`` helper splits the guard release from
  the on_done notification. ``_on_done_and_release`` keeps both
  for the healthy terminal paths (server compat OK, no self-
  update needed). The two no-server branches —
  ``_prompt_server_install`` and ``_prompt_server_update`` —
  release the guard but don't fire on_done, so the host stays
  parked at whatever screen was up when ``bootstrap()`` was
  scheduled (typically a splash). Once the user installs the
  server APK and the peer relaunches, bootstrap re-fires from a
  fresh process, finds the daemon reachable, and on_done flows
  through ``_check_self`` along the healthy path.
- The already-declined-this-version branch in
  ``_prompt_server_update`` does still fire on_done (with the
  caveat that the host's first RPC will surface the daemon's
  compat error). The user explicitly chose this state by
  declining earlier; the host should handle it gracefully.

### azt_collabd 0.28.4 + azt_collab_client 0.28.4 — single canonical "no server" popup, modal blocking, Quit button, doc consolidation
- **Single popup for "no server" / "server too old" cases.** Bootstrap's
  `_prompt_server_install` and `_prompt_server_update` now both
  delegate to `install_server_apk_popup` (instead of the older
  generic Yes/No `_yes_no` helper). Result: one visual surface,
  one set of buttons, one progress sink, one decline path. Closes
  the user-reported bug where two popups stacked on first launch
  ("Could not open project picker: server_apk_not_installed" + the
  bootstrap Yes/No, OR the bootstrap Yes/No + the older
  "AZT collaboration service required" widget).
- **Popup is now modal-blocking** (`auto_dismiss=False`). The user
  can't tap past it to reach a settings screen or picker that
  would itself fail with "server_apk_not_installed". Resolves the
  user-reported "in the client settings page, asking to Select
  Project resulted in widget 1" — once bootstrap fires, settings is
  unreachable until the user installs or quits.
- **Quit button replaces "Dismiss".** Label is "Quit {App.title}"
  (e.g. "Quit AZT Recorder") — falls back to plain "Quit" if the
  host hasn't set a title. Tapping it dismisses the popup AND
  calls `App.get_running_app().stop()`. Without the server APK
  the peer can't function, so leaving it running was the wrong UX.
- **Install button shows live progress in the popup body.** While
  `check_for_update` runs, the body label updates with
  "Downloading 45%…", "Release in progress — retrying in 5s…",
  "Installing…", "Installed." (or "Install pending. Reopen this
  app when finished." on the polling-timeout branch). The popup
  stays open through the whole flow — no more "I tapped Install
  and nothing happened" because the popup dismisses-and-routes-
  elsewhere. Buttons disable while the worker runs to prevent
  double-taps.
- **`install_server_apk_popup` gained context parameters**
  (`body_message`, `current_server_version`, `install_target_package`,
  `install_label`, `title`) so the same popup serves both the
  missing-server case (default) and the too-old-server case
  (passed by `_prompt_server_update`). Different body text and
  Install-button label, same machinery.
- **Bootstrap dead code pruned.** `_do_server_install` and
  `_quit_app` removed — both were one-call helpers absorbed into
  the popup refactor. `_yes_no` survives for the self-update
  prompt (different decision: peer can keep running on decline).
- **`docs/CLIENT_INTEGRATION.md`** added — the canonical "what every
  client must do" checklist. Sections: symlinks, buildozer.spec
  permissions / signing / manifest extras, **bootstrap wiring +
  the four caller invariants**, **don't roll your own server-missing
  UI** (the source of the user-reported bugs), translation chain,
  `App.title` for the Quit button, LIFT / audio / image access,
  recovery, testing. `azt_collab_client/CLAUDE.md` now points to
  it as the canonical reference.
- Translations (fr): "Quit", "Quit {app}", and the longer
  bootstrap-prompt body text used by the popup
  (`This app needs the AZT Collaboration service ({name}) to sync
  your data. Tap Install to download and install it. Android will
  ask you to confirm before the install starts.`).

### azt_collabd 0.28.3 + azt_collab_client 0.28.3 — install-popup auto-download, asset filename fix, install-completion polling, release cache, bookkeeping
- **Asset filename fix.** Every codebase reference to the server APK
  asset was ``azt_collab.apk`` (with underscore), but the Android
  ``package.name = aztcollab`` in server_apk/buildozer.spec.tmpl
  drops separators per the suite naming table — actual published
  asset is ``aztcollab.apk``. The 5 hardcoded references —
  ``bootstrap._SERVER_ASSET_DEFAULT``, ``CollabUIApp.share_apk`` /
  ``update_app``, ``PickerApp.share_apk`` / ``update_app`` — all
  fixed. The bootstrap workflow's GitHub-API asset lookup was
  returning "no aztcollab.apk in release" 404s because of this; now
  matches the actual release feed.
- **`SERVER_APK_INSTALL_URL` is now a direct-download URL**
  pointing at
  ``https://github.com/kent-rasmussen/azt-collab/releases/latest/download/aztcollab.apk``.
  GitHub's stable redirect serves the most recent matching asset,
  so the URL doesn't need updating per release.
- **`install_server_apk_popup` triggers auto-install** instead of
  only opening the browser. Tap Install → ``check_for_update``
  fetches the latest ``aztcollab.apk`` asset, streams it to
  ``$AZT_HOME/updates/``, and dispatches Android's system
  installer. The popup's "Open install page" affordance is
  retained as a fallback for users whose Android can't trigger the
  install intent. Progress strings flow back through the popup
  body and the host's ``on_status`` sink.
- **Install-completion polling** for cross-package installs
  (server-from-peer). New ``check_for_update(install_target_package=...)``
  parameter. After dispatching the install intent, the helper
  polls ``PackageManager.getPackageInfo`` every 5s for up to 5min,
  fires ``on_status('Installed.')`` when the version flips to the
  freshly-downloaded one, and ``on_status('Install pending.
  Reopen this app when finished.')`` on timeout. Closes the
  long-standing UX wart where status hung at "Installing…" forever
  after the user backed out of the system installer or the install
  finished out-of-foreground. Self-installs (peer-from-peer) skip
  polling — the install replaces the running peer, so polling
  would block forever. Bootstrap passes the server's package name
  on its server-install path; the same path is taken when the
  ``install_server_apk_popup`` Install button fires.
- **Per-process release cache** for ``_fetch_latest``. 5-minute TTL,
  keyed by repo slug. Closes the rate-limit hazard where a
  bootstrap launch + a settings-screen Update tap + multiple peers
  behind one NAT could collectively drain the GitHub anonymous
  60/hour budget; subsequent calls within the TTL hit the cache.
- **Caller invariants** — the four contracts the bootstrap caller
  must honor (asset name match, parseable tag, prerelease flag,
  ``REQUEST_INSTALL_PACKAGES`` permission) are now consolidated as
  a top-level "Caller invariants" section in
  ``azt_collab_client/ui/bootstrap.py``. Each was scattered across
  the function docstring + the client CLAUDE.md recipe + update.py
  comments before; now a single canonical list.
- **`p4a.sign = True` removed** from the suite's
  ``server_apk/buildozer.spec.tmpl`` (separately from the earlier
  ``android.signing.*`` cleanup). Confirmed empirically: this spec
  key is also dead config; signing depends solely on the
  ``P4A_RELEASE_KEYSTORE`` env vars. Memory feedback updated.
- **Daemon version bump 0.27.0 → 0.28.3** (lock-step with client)
  to signal that this round touched both packages. No wire-format
  change; cross-floors (``MIN_CLIENT_VERSION`` /
  ``MIN_SERVER_VERSION``) stay at 0.27.0 — older clients/servers
  still talk to this daemon/client without issue.
- **`docs/p4a_hook_picker_intent.md` path-leak scrub.**
  ``/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py`` →
  ``$P4A_HOOK`` (the env-var-resolved path) for public-repo
  consumption.
- **"Not now" on server install closes the peer app.** Without
  the server APK the peer can't function (no daemon → no sync, no
  project picker), so dropping the user into a broken state is
  worse than asking them to relaunch. Server-*update* decline
  doesn't quit (peer can still work against the older server,
  bound by ``MIN_SERVER_VERSION``); self-update decline doesn't
  quit either (peer is fine at current version). New
  ``_quit_app`` helper in bootstrap.py.
- **Download retry on transient HTTP statuses** (``404, 429, 500,
  502, 503, 504``). Load-bearing case: GitHub publishes the release
  JSON before the asset binary finishes uploading, so
  ``browser_download_url`` briefly 404s. Three attempts with linear
  backoff (5s, 10s, 15s ≈ 30s total). Between attempts, the user
  sees translated "Release in progress — retrying in Ns…" so a
  hung worker thread is no longer a confusion. New
  ``on_status`` parameter on ``_download``.
- **Translations (fr)** for the new state strings: "Installed.",
  "Install pending. Reopen this app when finished.",
  "Release in progress — retrying in {s}s…", and
  "Tap Install to download and install it. Android will ask you to
  confirm before the install starts."

### azt_collab_client 0.28.1 — bootstrap hardening + first automated tests
- **Filter prereleases** from the latest-release probe in
  `update.py:_fetch_latest`. Walks `/releases?per_page=20` for the
  first stable entry; falls back to `/releases/latest` if every
  recent release is a prerelease or the listing endpoint refused.
  Closes the v0.28.0 bug where a project pushing a `vN-rc` tag would
  silently auto-install onto every peer.
- **bootstrap idempotence guard.** A second `bootstrap()` call within
  the same process now no-ops. Prevents double-prompting when an
  on_start hook fires twice during a Kivy reload or two startup
  hooks both wire the helper.
- **Decline memory.** When the user taps "Not now" on a prompt, the
  declined version is persisted to
  `$AZT_HOME/config.json :: bootstrap.declined.<repo>=<version>`.
  Subsequent launches skip the prompt for that exact version; a
  new upstream release moves us off the recorded value
  automatically (string compare, not semver).
- **Disambiguate "server APK absent" from "daemon unreachable"** by
  probing `PackageManager.getPackageInfo('org.atoznback.aztcollab')`
  before issuing the install prompt. If the package is installed but
  the daemon happens to be down (no network, OOM-killed mid-call),
  the helper now skips the install prompt and continues to the
  self-check instead of asking the user to install something that's
  already there. New status string
  "AZT Collaboration installed but unreachable. Continuing offline."
- **First automated test scaffold.** New `azt-collab/tests/` directory
  with pytest fixtures (per-test `$AZT_HOME` redirection, jnius stub,
  Kivy headless flags, platform monkeypatch) and five test modules
  covering version-tuple corner cases, GitHub-API mocks for
  `check_for_update`, bootstrap dispatch + idempotence + decline
  memory + package-presence disambiguation, the `github.confirmed`
  store lifecycle, and a translation-coverage drift detector.
  Run with `pytest tests/ -q`. CLAUDE.md updated to retire the
  "no automated test suite anywhere in the suite" claim.
- **`docs/research_notes_2026-05.md`** captures the state of the
  art for the technologies we depend on (Android 16, the March-2026
  sideloading lockdown, ACTION_VIEW deprecation in favor of
  PackageInstaller, buildozer/Kivy versions, GitHub API behavior).
  Action items are owned in the file. Refresh before each major
  release.
- **`docs/test_plan.md`** is the canonical failure-mode matrix. Every
  bug found in the bootstrap workflow lands here as a row in §10
  before it gets fixed.
- One new translation: "AZT Collaboration installed but unreachable.
  Continuing offline."

### azt_collab_client 0.28.0 — bootstrap() one-call peer entry point
- New `azt_collab_client.ui.bootstrap(...)` helper. Peers call it
  once on `App.on_start` and the helper handles, in this order:
  1. `check_server_compat()`. On `server_unreachable` →
     "Install AZT Collaboration?" Yes/No popup → `check_for_update`
     against `kent-rasmussen/azt-collab` → Android system installer.
     `server_too_old` runs the analogous "Update AZT Collaboration?"
     prompt. `client_too_old` jumps to step 2.
  2. Probe peer's own latest release on GitHub. If newer →
     "Update <peer>?" Yes/No → download+install the peer's APK.
  3. `on_done` — every up-to-date / declined / completed-install
     branch lands here so the host's normal startup always
     resumes.
  Suite UX rule encoded by this helper: **the user installs one
  APK** (the peer they opened); the standalone server APK and all
  subsequent updates are provisioned by the peer itself on first
  run. Spawns a worker thread for the version probes so first
  paint isn't blocked; popups marshal back to the Kivy UI thread.
- Android-only effects. Desktop hosts call `on_done` immediately.
- Buildozer requirement documented (`REQUEST_INSTALL_PACKAGES` in
  the peer's `android.permissions`); without it the install intent
  silently no-ops.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  the recorder, viewer, and any future peer can wire one
  ten-line `App.on_start` call and let the helper take it from
  there.
- New translations (fr): "AZT Collaboration", "Checking
  installation…", "Install AZT Collaboration?", "Update AZT
  Collaboration?", "Update {name}?", body strings for each prompt,
  "Install" / "Update" / "Not now" buttons, "Update needed" info
  popup, "Updating {name}…", "AZT Collaboration is up to date.",
  and the rare client-too-old-no-newer-release fallback message.

### azt_collabd 0.27.0 + azt_collab_client 0.27.0 — symmetric host credential flow
- **`github.confirmed` is now a stored flag**, not derived. Mirrors
  the existing GitLab semantics: set true by a successful live test,
  reset to false on any settings change (token save, app-install
  flag flip, disconnect). Per-host shape is now uniform — both
  GitHub and GitLab expose `connected` ("settings present") and
  `confirmed` ("tested OK against the host's API").
- New endpoint `POST /v1/credentials/github/test` (handler
  `_h_test_github`) and matching client wrapper
  `azt_collab_client.test_github_credentials()`. Hits
  `api.github.com/user` with the stored access token; on success
  also probes `api.github.com/user/installations` so the same Test
  button refreshes both `confirmed` and `app_installed` in one
  user gesture, matching the GitLab Test pattern.
- Auth helper `azt_collabd.auth.test_github_credentials(token)`
  added alongside the existing `test_gitlab_credentials` —
  consistent shape, same return dict (`{valid, server_username,
  app_installed, error}`).
- **`GitHubConnectScreen` is now state-aware.** Three shapes picked
  in `on_pre_enter` from `credentials_status['github']`:
  * not connected → device-flow box visible, manage hidden,
    `begin()` auto-fires.
  * connected, not confirmed → manage view (Test + Install GitHub
    App if not installed + Re-authenticate + Disconnect); device
    flow hidden; nothing auto-fires.
  * connected, confirmed → same controls plus a "(verified)"
    badge in the status line.
  Show/hide uses the Kivy hide/show pattern (height: 0, opacity: 0)
  per `~/.claude-sil/CLAUDE.md`. The screen is fully self-contained:
  Disconnect / Re-authenticate / Install-app no longer require the
  user to bounce back to settings.
- `SettingsScreen.connect_github()` reduced to a one-liner navigate.
  Auto-firing `begin()` on every entry to the screen is gone — the
  user with a token already on file isn't re-prompted for device
  flow; they get the manage view and pick Test or Re-authenticate
  themselves.
- Lock-step bump to 0.27.0 with cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.27.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.27.0. New wire endpoint
  + bundled-client peer APKs need the floor bump or version
  mismatches stay silent (ref. memory note on
  MIN_CLIENT_VERSION discipline).
- Translations (fr) for the new state-aware strings:
  "Re-authenticate", "Disconnect", "Install GitHub App",
  "Connected as {username} (verified).", the not-yet-tested and
  app-not-installed variants, "Token rejected by GitHub. Tap
  Re-authenticate.", "Could not open install page: {error}", and
  the "Opening {uri}\nWhen you finish on GitHub..." prompt.

### azt_collabd 0.26.0 + azt_collab_client 0.26.0 — in-app self-update
- New `azt_collab_client.ui.check_for_update(repo, current_version,
  asset_filename, on_status, ...)` reusable updater. Spawns a worker
  thread, polls `GET /repos/{repo}/releases/latest`, compares the
  release tag to the caller's `__version__` as a semver tuple, and on
  a newer release downloads the matching asset and dispatches
  `Intent.ACTION_VIEW` with the APK MIME type so Android's system
  installer takes over. All callbacks marshal back to the Kivy UI
  thread; non-Android hosts get a translated
  "APK install is only available on Android." through `on_error`.
- `REQUEST_INSTALL_PACKAGES` added to
  `server_apk/buildozer.spec → android.permissions`. The helper
  detects Android 8+ "Install unknown apps" gating via
  `PackageManager.canRequestPackageInstalls()` and routes the user to
  `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` on first use.
- `azt_collabd.configure(update_repo=...)` + `AZT_UPDATE_REPO` env var
  + `azt_collabd.config.update_repo()` accessor; default is
  `kent-rasmussen/azt-collab` (the canonical release feed at
  https://github.com/kent-rasmussen/azt-collab/releases/latest).
- "Update this app" button + status `BodyLabel` added to
  `<SettingsScreen>` directly under the existing "Share this app"
  row. Hosted by both `CollabUIApp.update_app` (standalone settings
  subprocess) and `PickerApp.update_app` (in-process settings reached
  from the picker's gear). Same KV `app.update_app()` resolves on
  whichever App owns the screen.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  peers (recorder targeting `kent-rasmussen/azt-recorder`, future
  viewer, …) can wire the same button into their own settings screens
  by passing their own `repo` / `__version__` / `asset_filename`.
- Translations added (fr): "Update this app", "Up to date.",
  "Checking for updates…", "Downloading {pct}%…",
  "Preparing install…", "Installing…",
  "APK install is only available on Android.", and the failure
  variants ("Update check failed: {error}", missing-tag /
  missing-asset / missing-URL detail strings, "Download failed",
  "Install failed", "Could not create download dir", and the
  Install-unknown-apps prompt).
- Lock-step bump to 0.26.0 across `azt_collabd` and `azt_collab_client`
  (no wire-format change; just keeping the cross-floors aligned now
  that the client gained shared UI surface peers will rely on).

### azt_collabd 0.25.2 — "Share this app" on the settings screen
- Added a Share-this-app row to `<SettingsScreen>` (`azt_collabd/ui/app.py`),
  positioned right under the Back NavBtn so it leads the scrollable
  body the same way the recorder's settings screen does. Hands the
  running server APK to Android's share sheet via the existing
  `azt_collab_client.ui.share_running_apk` helper — useful for
  onboarding teammates to the collab service. Hosted by both the
  standalone `python -m azt_collabd ui` (`CollabUIApp.share_apk`) and
  the in-process settings reached from the picker's gear
  (`PickerApp.share_apk`); the KV's `app.share_apk()` resolves on
  whichever App owns the screen at runtime.
- Icon (`share_dark.png`) sourced via `azt_collab_client.ui.icon_path`
  and threaded into the KV through `register_kv` next to the existing
  font-name substitution. Desktop hosts get the button too; tapping
  it surfaces the translated "APK sharing is only available on
  Android." message via the helper's `on_error` callback.
- French translations added for `Share this app`, `Share app`, `Error`,
  and the three `share_running_apk` failure messages
  (`APK sharing is only available on Android.`, the MediaStore-insert
  failure, the generic `Could not share APK:` wrapper).

### azt_collab_client 0.25.2 — public `ensure_mo` for peers
- `azt_collab_client.i18n.ensure_mo(locale_dir, domain, lang)` exposes
  the lazy `.po` → `.mo` compile path peers were previously missing.
  Peer i18n modules call it before `gettext.translation(...)` so they
  can ship `.po`-only and skip the external `msgfmt` build step the
  same way the client does. Writes the `.mo` next to the `.po`; on
  Android that's inside the APK's private filesDir, which is
  writable. See the *Internationalization (i18n)* section of
  `azt_collab_client/CLAUDE.md` for the integration recipe.
- `_ensure_mo(lang)` is now a thin wrapper around `ensure_mo` for the
  client's own domain — no behaviour change for the client itself.

### azt_collabd 0.25.1 + azt_collab_client 0.25.1 — French catalog catch-up
- Added 28 missing French translations to
  `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  covering the popup confirm-langcode flow, the clone-URL popup's
  inline `code: ` / `change code` / `OK` affordances, the daemon
  settings UI's publish row + GitLab Test screen, and the picker's
  empty-result fallback dialog. Catalog Project-Id-Version follows
  the package bump.
- Wrapped a stray ``'Open settings'`` literal in
  `azt_collabd/ui/picker_app.py:852` (clone-failure auth modal) with
  `_tr` so the translation already in the catalog actually fires.

### azt_collabd 0.25.0 + azt_collab_client 0.25.0 — synchronized release
- Lock-step version bump of both packages plus their cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.25.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.25.0. Intended to flush
  every peer APK through a rebuild so the cumulative work since the
  prior synchronization point lands in lockstep across the suite.
  The user-visible content of this release is the union of every
  entry below, plus the four-month gap of additive changes that
  preceded them. After this synchronization any peer running an
  older bundled client will surface `client_too_old` from
  `check_server_compat()` and any client talking to an older daemon
  will surface `server_too_old`, prompting an update on either side
  rather than silently degrading.
- Notable behaviour requiring lock-step:
  - `last_project` server-tracked via `/v1/recent/last_project`
    (older clients keep using their own sandbox, breaking on
    Android).
  - GitLab Test button drives `_h_test_gitlab` and the new per-host
    `confirmed` flag.
  - `Project.last_commit`, `ProjectStatus.commits_ahead` exposed on
    the wire; `_h_init_project` writes `last_sync` / `last_commit`
    back to projects.json on a successful publish.
  - `_resolve_path` reads `working_dir` from the registry; URIs
    decouple from on-disk dir naming.
  - `:provider` stdio bridge so daemon `print(..., file=sys.stderr)`
    actually reaches logcat.
  - dulwich `repo.refs[name]` (with `KeyError`) replaces the
    incorrect `.get()` call that was failing every sync silently;
    post-push remote-mirror update fixes `(+N)` indicator stickiness.

### azt_collabd 0.21.4 + azt_collab_client 0.24.0 — back from picker also returns to last project
- 0.21.3 only special-cased the BCP-47 langpicker; back from the
  *project picker screen itself* still hit the `if self.sm.current
  == 'picker': return False` early exit and let Android close the
  Activity with `RESULT_CANCELED`. The user's case from logcat —
  picker screen opens, back press, no `[picker_app]` trace at all,
  recorder receives an empty cancel — is exactly that path. New
  `_exit_to_last_project_or_cancel` helper centralises the
  emit-resumable-or-cancel shape, called from both the `picker` and
  `langpicker` branches of `_navigate_back`. Either exits the
  picker subprocess in one back-press, with the recorder receiving
  either the resumable project or a clean cancel.

### azt_collabd 0.21.3 + azt_collab_client 0.24.0 — back-from-langpicker always exits subprocess
- 0.21.1 added the langpicker→last_project special case but only
  when `last_project()` resolved; otherwise it fell through to the
  default `'picker'` target, which left the user on the project
  picker screen requiring a second back-press to actually exit.
  Now the langpicker back-press always exits the picker subprocess
  in one step: emit `last_project` if it resolves (recorder
  auto-resumes), or emit a clean cancel if it doesn't (recorder's
  `_handle_pick` silently returns to whatever it was showing).
  Either way, the user lands on the recorder, never on the project
  picker. New `_emit_cancel_and_quit` helper factors the
  `setResult(RESULT_CANCELED) + finish` shape out of
  `on_request_close` so it's reachable from anywhere.

### azt_collabd 0.21.2 + azt_collab_client 0.24.0 — URI resolution decoupled from on-disk dir name
- **Cloned project's LIFT URI returned `FileNotFoundException`.**
  `_resolve_path` in the ContentProvider was building
  `$AZT_HOME/projects/<langcode>/...` from the URI's first segment,
  but the picker_app's clone worker uses the URL's repo basename for
  `dest_dir` (e.g. `en_Demo.git` → `projects/en_Demo/`), while the
  URI handed back to peers uses the user-chosen langcode (e.g.
  `content://.../en/SILCAWL.lift`). Mismatch → resolver looked under
  the wrong directory → file-not-found → recorder's "lift namespace
  scan failed: forbidden: /en/SILCAWL.lift" log line. `_resolve_path`
  now consults `projects.get(langcode).working_dir` so URI resolution
  is independent of how the on-disk directory was named at clone
  time. Falls back to the legacy `<projects>/<langcode>` path when
  the project isn't in the registry, preserving pre-registry URIs.

### azt_collabd 0.21.1 + azt_collab_client 0.24.0 — back-from-langpicker resumes last project
- `_navigate_back` in `picker_app.py` now special-cases the
  `langpicker` screen: a user who reached Start-New and then changed
  their mind almost always wants to return to whatever project they
  had open before, not be re-parked at the project list. When
  `last_project()` resolves to a live registered project, back from
  `langpicker` emits that project's path and exits the picker
  subprocess (recorder auto-resumes). Cold-start fallback (no
  recorded last project) still goes to the picker screen so the
  user has somewhere to land.

### azt_collabd 0.21.0 + azt_collab_client 0.24.0 — sync was failing every cycle; remote-mirror not bumped after push
- **Sync was raising `AttributeError` on every cycle.** Pre-0.21.0
  `_sync_repo_locked` did `repo.refs.get(branch_ref) or repo.head()`,
  but `dulwich.refs.DiskRefsContainer` has no `.get()` method — only
  `__getitem__` (raises `KeyError`) and `read_ref()`. The call
  raised `AttributeError` post-fetch on every sync, propagated to
  `scheduler._fire`'s catch-all, and the job was marked
  `PUSH_FAILED`. Symptom: commits piled up locally but never
  pushed; "I see Audio recordings by Recorder commits showing up on
  GitHub minutes apart" was the queue draining whenever a build
  with the diagnostic try/except (added in 0.20.9) happened to be
  running. Replaced with a small `_read_ref(name)` helper that
  uses `__getitem__` + `KeyError`. Both `branch_ref` and
  `remote_ref` reads, plus the retry-loop's post-fetch read, now
  use it.
- **`commits_ahead` stuck at `(+N)` after a successful push.**
  Symptom: `[sync] done: codes=['NOTHING_TO_COMMIT', 'PUSHED']` ran
  cleanly but the recorder's indicator kept showing `(+10)`. Cause:
  `porcelain.push` advances the remote on GitHub but doesn't update
  the *local mirror* `refs/remotes/origin/<branch>` —
  `_count_commits_ahead` then compares the just-pushed `local_sha`
  against the pre-push mirror and reports the count of just-pushed
  commits as still-pending. Bumping the mirror explicitly after a
  successful push (`repo.refs[remote_ref] = local_sha`) reflects
  what CLI `git push` does and lets `commits_ahead` read 0 on the
  next status poll.

### azt_collabd 0.20.9 + azt_collab_client 0.24.0 — sync-hang trace, post-fetch ref reads
- The 0.20.8 trace narrowed the hang to the `local_sha = repo.refs.get(...)
  or repo.head()` line, so 0.20.9 splits that into separate
  `[sync-trace]` prints around `repo.refs.get(branch_ref)`,
  `repo.head()`, and `repo.refs.get(remote_ref)`. Whichever call
  doesn't produce its trailing print is where dulwich is wedging.
  Each is wrapped in try/except to surface a raise vs a hang
  cleanly.

### azt_collabd 0.20.8 + azt_collab_client 0.24.0 — sync-hang trace
- `[sync-trace]` prints between every step in `_sync_repo_locked`
  after the fetch (`fetch begin/done`, `local_sha`, `remote_sha`,
  `needs_merge`, `fast-forward`/`local ahead`/`merge_diverged
  begin/done`, `push loop begin`, `push attempt`, `push done`/`push
  raised`). Pinpoints exactly which step wedges between fetch
  returning and the function exiting — your latest log shows
  `fetch` returning HTTP 200 followed by silence until the next
  status-poll, so the hang is somewhere between `local_sha = ...`
  and the `push` HTTPS call.

### azt_collabd 0.20.7 + azt_collab_client 0.24.0 — last_sync on publish + `:provider` stdio bridge
- **Publish stamps `last_sync` / `last_commit`.** `_h_init_project`
  ran the publish (commit + push) and updated `remote_url` in
  `projects.json`, but did **not** stamp `last_sync` from the result
  codes. Sister handlers in `scheduler._run_sync` and
  `_h_project_sync` already did. Symptom: a successful publish left
  `Project.last_sync == 0`, the recorder's UI read that as "never
  synced" and kept showing the "data isn't being backed up" warning
  forever — even though the repo was sitting on github.com fully
  pushed. After this fix, a publish that returns `PUSHED` (or
  `COMMITTED_AND_PUSHED`) immediately stamps `last_sync` so the
  indicator flips on the next refresh.
- **Daemon prints now reach logcat.** `server_apk/service.py` calls
  a new `_bridge_stdio_to_logcat()` at module top that pipes
  `sys.stdout` / `sys.stderr` through `android.util.Log` under tag
  `python`. p4a auto-redirects stdio for `PythonActivity` (the
  Activity process where the picker / settings UI / recorder Kivy
  apps run), but not for `PythonService` — and the daemon
  (server.py, scheduler.py, repo.py) lives in the `:provider`
  process driven by PythonService. Pre-0.20.6 every `print` from
  daemon code went to a black hole, so functionally correct sync
  flows (RPC returns a job_id, the timer fires, the repo gets
  pushed) appeared in logcat as if nothing happened. After this fix
  the `[sync-async]` / `[sync-debounce]` / `[sync-fire]` / `[sync]`
  traces actually show up.

### azt_collabd 0.20.5 + azt_collab_client 0.24.0 — picker failure → notify + exit
- After `_pick_project_android`'s one-retry loop exhausts on
  `unexpected_cancel`, the client now schedules a Kivy modal on the
  UI thread: "The project picker failed to return a result. The app
  will now close — please reopen it." with a single OK button that
  calls `App.get_running_app().stop()` (and `os._exit(0)` as a belt-
  and-braces). Lives in the client so every peer using
  `pick_project()` gets the same fallback without per-host wiring;
  documents in the function docstring that `unexpected_cancel` is
  now a terminal status from the caller's perspective (the user
  will see the modal and exit before the caller observes the
  return value). Triggered by the user reporting empty-recorder
  windows three times in one debug session.

### azt_collabd 0.20.5 + azt_collab_client 0.23.6 — client-side request_sync trace
- `[sync-client] request_sync(<lang>, ...)` printed at every entry,
  with success/failure/transport branches. Closes the visibility gap
  between "the recorder called the RPC" and "the daemon received it"
  — if the client log is silent, the recorder never tried; if the
  client log shows a send but the daemon's `[sync-async]` is silent,
  the RPC is being dropped at the transport layer.

### azt_collabd 0.20.5 + azt_collab_client 0.23.5 — picker auto-retry on RESULT_CANCELED-with-data
- `_pick_project_android` now wraps the launch+wait in a one-retry
  loop. The picker contract is RESULT_OK→data /
  RESULT_CANCELED→no-data; the combo "non-OK + data attached"
  shouldn't be reachable normally (Android can synthesize it on
  back-press during `setResult`, or with OEM launcher tampering).
  Pre-0.23.5 we'd silently swallow that case as `'cancelled'` and
  drop the user on a recorder window with no project. Now the
  client classifies it as `'unexpected_cancel'` and re-launches the
  picker once, so the user gets another shot at choosing.
- The retry-loop refactor extracts the inner launch/wait logic into
  `_pick_project_android_once`; `attempt=` is included in the
  `[pick_project] _on_result: ...` trace so each invocation is
  identifiable in logcat.

### azt_collabd 0.20.5 + azt_collab_client 0.23.4 — full sync chain trace
- Three new diagnostics so a request → debounce → fire → run path is
  visible end-to-end in logcat: `[sync-async] <lang>` at RPC arrival
  in `_h_project_sync_async`, `[sync-debounce] <lang>` from
  `scheduler.request_sync` (so we see whether the recorder's
  `_auto_commit_sync` reached the queue), and `[sync-fire] <lang>`
  from `_fire` (so we see whether the debounce timer actually fired
  before the daemon process got recycled).

### azt_collabd 0.20.4 + azt_collab_client 0.23.4 — empty-registry disk scan
- When `_h_list_projects` returns zero entries, also print what
  `$AZT_HOME/projects/` actually contains on disk. Distinguishes
  "registry wiped but working trees survived" (recoverable: a
  future endpoint can scan + auto-register) from "filesDir gone"
  (server APK clean-installed; nothing to recover).

### azt_collabd 0.20.3 + azt_collab_client 0.23.4 — recent-state stderr trace
- `azt_collab_client/CLAUDE.md` rule #2 expanded to cover the
  *gating* failure mode (peers silently skip auto-sync on Android
  because their local filesystem check returns False) and to
  include the verbatim fix-shape snippet for `_project_has_remote`,
  so future Claude sessions touching a peer don't have to re-derive
  the daemon-served replacement.

### azt_collabd 0.20.3 + azt_collab_client 0.23.3 — recent-state stderr trace
- Diagnostic prints around every read/write of `last_langcode`:
  daemon-side `[recent] _touch_project(...) → /path/to/config.json`,
  `[recent] GET /v1/recent/last_project → 'lang' (from /path/...)`,
  and matching `POST` line; client-side
  `[recent] last_project → 'lang'` / `set_last_project(...) sent` /
  `ServerUnavailable: ...`. Pairs the path being written with the
  path being read so a divergent-`$AZT_HOME` bug shows up in logcat
  side-by-side instead of having to be inferred. No behaviour change.

### azt_collabd 0.20.2 + azt_collab_client 0.23.2 — MIN_CLIENT_VERSION floor for the recent.py RPC migration
- **`MIN_CLIENT_VERSION` raised to 0.23.0.** Pre-0.23 clients keep
  reading `$AZT_HOME/config.json::recent.last_langcode` from their
  own package's filesDir, which on Android sits in a different
  sandbox from the daemon's. The daemon stamps last_project on
  every langcode-bound RPC, but the peer's bundled client never sees
  it — recorder auto-resume falls through to the picker on every
  restart. Bumping the floor makes `check_server_compat()` return
  `client_too_old` instead of silently degrading, so the recorder
  surfaces the "please update" warning. Saved-memory note
  `feedback_min_client_version.md` documents this exact failure mode.

### azt_collabd 0.20.1 + azt_collab_client 0.23.2 — publish-row sticking after success
- **`_h_init_project` writes remote_url back to `projects.json`.**
  Symptom: a publish that returned `PUSHED` left the project's
  `Project.remote_url` empty in the registry, so the settings UI
  immediately re-rendered the publish row asking the user to publish
  again. `_init_repo` updates the *local* git config but the
  registry is a separate datastore; the back-write was missing. Now
  the daemon walks `projects.list_all()`, finds the entry whose
  `working_dir` matches, and writes the URL via
  `projects.set_remote_url`.
- **`_pick_publish_candidate` consults the live git remote.** Even
  with the back-write fix, projects published before 0.20.1 carry
  an empty cached `remote_url`. The settings UI now also reads the
  authoritative value via `project_status(langcode).remote_url`
  (which checks `.git/config`); the row hides if either source
  reports a remote. Defensive belt-and-braces so existing published
  repos behave correctly without a manual reconcile.

### azt_collabd 0.20.0 + azt_collab_client 0.23.1 — `commits_ahead` on ProjectStatus
- **`commits_ahead: int` on `ProjectStatus`.** Filed by recorder
  1.37.6 in `NOTES_TO_DAEMON.md`: the recorder's sync indicator
  needs the count of local commits not yet pushed to the remote so
  it can render `(+n)` instead of an opaque `*` marker.
  `repo_status_summary` now returns a 4-tuple
  `(branch, remote_url, n_changes, commits_ahead)`; `_h_project_status`
  forwards `commits_ahead` on the wire. Computed locally from
  `refs/heads/<branch>` vs. `refs/remotes/origin/<branch>` (no
  network round-trip), so a stale cache may under-report — the
  recorder's UX contract is "OK on uncertainty," so under-reporting
  is the right failure mode. Returns 0 whenever the local cache
  doesn't have a remote ref to compare against (no remote
  configured / never pushed). Client dataclass already had the
  field with `default 0` for forward-compat. NOTES_TO_DAEMON entry
  cleared.

### azt_collabd 0.19.1 + azt_collab_client 0.23.1 — server-canonical recent state + last_commit
- **`azt_collab_client/CLAUDE.md` rule #2 added** — "no reading
  project state from the local filesystem either," with the
  recorder's `_project_has_remote()` (dulwich.Repo on the working
  dir) called out as the canonical anti-pattern. Reads silently work
  on desktop and silently fail on Android because the daemon's
  working_dir lives in the server APK's private filesDir. Future
  peers must use `project_status(langcode)` for state-shaped checks.
- **Publish outcome message no longer clobbered by refresh.**
  `_publish_done` was setting the message *then* calling `refresh()`,
  which started by clearing `msg.text`, so the user only saw
  "Publishing..." and never the result. Reorder: refresh first, then
  set the outcome. Re-enables the button on failure.
- **`[publish]` stderr trace** in `_publish_worker`: prints the
  arguments going into `init_project` and the resulting `Result.codes()`
  (or the exception). Pairs with the `[sync-rpc]` / `[sync]` traces
  added in 0.18.2 so a publish failure has a logcat trail.

### azt_collabd 0.19.0 + azt_collab_client 0.23.0 — server-canonical recent state + last_commit
- **`last_project` is now server-tracked.** Was: each peer wrote
  `$AZT_HOME/config.json::recent.last_langcode` directly, which broke
  on Android where every peer's sandbox holds its own config.json
  (the recorder's write and the settings-UI subprocess's read landed
  in different files), and broke on desktop whenever a load path
  forgot to call `set_last_project`. Now: every langcode-bound RPC
  (`open_project`, `project_status`, `sync`, `sync_async`, `register`,
  `init`, `clone`, `from_template`, `rename`) auto-stamps via the new
  `server._touch_project` helper, and `last_project()` /
  `set_last_project()` are thin wrappers around new endpoints
  `GET`/`POST /v1/recent/last_project`. Single source of truth across
  peers and platforms; peers don't have to remember to call
  `set_last_project` from any specific load path.
- **Publish picker simplified.** With server-canonical
  `last_project`, the unpublished-projects-preference fallback in
  `SettingsScreen._pick_publish_candidate` (added in 0.18.1 to work
  around stale recorder-written state) is gone. The settings UI now
  resolves `last_project()` straight to the candidate Project; if
  that doesn't return a live project, the publish row hides — which
  is the correct UX, because nothing has been touched.
- **`Project.last_commit` field, separate from `last_sync`.** Filed
  by the recorder team in `azt_collab_client/NOTES_TO_DAEMON.md`:
  peer sync indicators couldn't distinguish "committed locally but
  not pushed" from "silently broken" because `last_sync` only
  stamped on `PUSHED` / `PULLED` / `COMMITTED_AND_PUSHED`. Daemon
  now also stamps `last_commit` on `COMMITTED_LOCAL` /
  `COMMITTED_NO_REMOTE` / `COMMITTED_AND_PUSHED` (any path where a
  commit object hit the working tree). Both fields ride on
  `Project` and `ProjectStatus`; pre-0.19 daemons that don't emit
  `last_commit` get a 0.0 default in the client dataclass for
  forward-compat. `NOTES_TO_DAEMON.md` entry deleted per its own
  instructions.
- **Sync trace lines** retained from 0.18.2: `[sync]` lines from
  `scheduler._run_sync` and `[sync-rpc]` from `_h_project_sync` so
  successful syncs show up in `adb logcat -s python`.

### azt_collabd 0.18.2 + azt_collab_client 0.22.1 — settings UX cleanup
- Sync trace lines on stderr (visible via `adb logcat -s python` on
  Android): `[sync] <lang> ... starting` / `... done: codes=[...]` from
  `scheduler._run_sync` and `[sync-rpc] ...` from `_h_project_sync`.
  Previously a successful sync emitted nothing — the structured
  `Result` carried the outcome but there was no trail in logcat to
  confirm the daemon had even seen the request.
- Publish-candidate fallback also prefers unpublished projects (the
  filtered `list_projects()` search would otherwise pick a
  more-recently-synced sibling that was already published, hiding
  the publish row even though a sibling project still needed
  publishing).
- Publish candidate falls back from `last_project()` to the
  highest-`last_sync` entry in `list_projects()` when the suite-wide
  "last opened" key is empty (older recorder load paths don't always
  write it). Diagnostic stderr lines from `_pick_publish_candidate`
  surface why the row stayed hidden.


- **GitLab "Connect" + Test button.** The settings screen's GitLab
  affordance is now labelled "Connect to GitLab" (was "Set GitLab
  credentials") to match GitHub's wording. The form screen replaces
  the bare "Save" button with a single "Test connection" button: the
  daemon-side `_h_test_gitlab` endpoint runs a live check against
  `gitlab.com/api/v4/user`, and only on success does it persist the
  credentials and stamp `gitlab.confirmed=True` in the store, so the
  user can't end up with a stored bad token. New endpoint
  `POST /v1/credentials/gitlab/test` (falls back to stored creds if
  body fields are empty) and client wrapper
  `test_gitlab_credentials(username, token)`.
- **Per-host `confirmed` flag.** `get_credentials_status()` now
  reports `github.confirmed` (derived: `connected AND app_installed`)
  and `gitlab.confirmed` (persisted; cleared on save, set on a
  successful Test). There is no longer a single "active host" — both
  hosts can be confirmed independently, and consumers (publish flow
  below, future sync flows) pick one based on context.
- **"Publish &lt;langcode&gt; data" button on the settings screen.**
  Visible only when `last_project()` resolves to a langcode whose
  project doesn't already have a remote; enabled when at least one
  host is `confirmed`. On click, single-confirmed hosts publish
  directly via `init_project(working_dir, remote_url, ...)`; both
  confirmed surfaces a small overlay so the user picks GitHub or
  GitLab. Mirrors the recorder's `do_publish` flow but moves it into
  the daemon UI, so any peer that exposes the gear can publish
  without owning the publish UI.
- **GitHub device flow no longer auto-fires on screen rebuild.**
  `GitHubConnectScreen.on_pre_enter` previously kicked the device
  flow on every entry, which meant a language-change rebuild — which
  clears + re-adds every screen — re-launched device flow even though
  the user was nowhere near the GitHub screen. The auto-start now
  lives on the explicit "Connect to GitHub" button via the new
  `SettingsScreen.connect_github()`, so language changes (and any
  other rebuild) leave the GitHub screen quiet.

### azt_collabd 0.16.0 + azt_collab_client 0.20.0 — sticky-bound server APK service + persistent scheduler jobs
- **Server APK lifetime fix.** The picker Activity now leaves the
  Python process running on Android instead of calling `App.stop()` /
  `sys.exit()`. A new sticky-bound service
  (`AZTServiceProviderhost`, `android/src/main/java/.../`) pins the
  host so `AZTCollabProvider.openFileDescriptor` can still serve the
  URI grant the picker just emitted. Pre-0.16.0 the server APK
  process exited as soon as the picker Activity finished, taking the
  provider with it and triggering Android's "depends on provider in
  dying proc" cascade SIGKILL of any peer that had received a
  `content://` URI from the picker.
- Service is sticky-bound (no foreground notification): peers get
  the bind-priority OOM hint while they're using the provider, and
  `START_STICKY` asks Android to recreate the service after a
  memory-pressure kill. Idle-stop policy (5 min of zero peers bound
  + zero provider activity) tears the service down so the design's
  "transient when idle, pinned while in use" intent is preserved.
  Manifest entry injected by `_inject_aztcollab_service` in
  `p4a_hook.py`, gated on `dist_name == 'aztcollab'`.
- **Scheduler jobs persisted to `$AZT_HOME/jobs.json`** so peer
  `poll_job(job_id)` calls survive a daemon respawn. `_store_job` and
  `_fire` write atomically on every state transition. New
  `scheduler.reconcile_on_startup()` runs from the loopback HTTP
  daemon entry (`server.run`) and the Android service entry
  (`server_apk/service.py`); marks any `PENDING` / `RUNNING` jobs
  found at startup as `DONE` + `JOB_INTERRUPTED` because their
  worker threads died with the previous process. Old `DONE`
  entries are GC'd past 1h at the same pass.
- New status code `JOB_INTERRUPTED` (`azt_collabd/status.py` and
  `azt_collab_client/status.py`, mirror) plus translation in
  `azt_collab_client/translate.py`. Peers should treat it identically
  to `SERVER_UNAVAILABLE`: transient, retryable.
- `MIN_CLIENT_VERSION` bumped to 0.20.0 — pre-0.20 clients don't have
  the `JOB_INTERRUPTED` translation and would surface the raw
  uppercase code in their UI.
- `MIN_SERVER_VERSION` bumped to 0.16.0 — pre-0.16 daemons don't
  persist jobs.json, so `poll_job` returns None for any job_id whose
  daemon has been respawned, indistinguishable from "never existed."
- Activity tracking added to `azt_collabd/android_cp/service.py`:
  `touch()`, `seconds_since_last_touch()`, `bound_client_count()`
  used by the service idle-stop loop. Every dispatch / openFile call
  bumps the touch timestamp.
- Picker app gains `on_pause` returning True so Kivy doesn't fight
  the missing GL surface after the Activity finishes.
- New `server_apk/test_install.py` (sibling of the existing adb-driven
  `test_install.sh`): 8-section desktop integration test for the
  kill-recovery flow — auto-spawn detection, jobs.json persistence,
  reconcile_on_startup, JOB_INTERRUPTED end-to-end. Run from the
  azt-collab repo root: `python server_apk/test_install.py`.

### azt_collab_client 0.19.2 — pick_project unbinds its activity-result handler after each call
- ``pick_project()`` registered a closure on
  ``android_activity.bind(on_activity_result=…)`` and never
  unbound it, so each invocation in a host session left a dangling
  handler that fired on every subsequent activity result. Logs
  showed N copies of ``[pick_project] _on_result …`` after N
  picks. Each closure wrote to its own long-since-stale
  ``holder`` so behaviour was correct for the most recent caller,
  but the JNI cost grew linearly with picks.
- New ``_unbind_handler`` helper called from inside ``_on_result``
  after ``done.set()`` (single-shot pattern) and from the timeout
  path so a much-later activity result for our request code can't
  write to a stale holder. Tracks ``bind_state['bound']`` to avoid
  unbinding a never-bound handler when ``_setup_on_ui`` failed
  early. Tolerates older Kivy / python-for-android versions that
  exposed ``bind`` without ``unbind``.

### azt_collab_client 0.19.1 — fix vanishing project list: defer ProjectPickerScreen.on_enter populate by one frame
- ``projects.json`` had the cloned project, the daemon's
  ``_h_list_projects`` would have returned it — but the picker
  never asked. ``ProjectPickerScreen.on_enter`` called
  ``_populate_projects`` synchronously, and Kivy >= 2.3 fires
  ``on_enter`` before KV-defined ids attach on the first screen
  entry. So ``self.ids.get('project_list')`` returned None, the
  populate function bailed silently, and the existing-projects
  list rendered empty — "cloned projects don't show up on next
  open". Same race the settings UI already worked around with
  ``Clock.schedule_once``; applied the same fix here.
- Added two diagnostic prints inside ``_populate_projects`` so
  any future bail (still-no-id even after the defer, or missing
  host ``list_projects`` method) surfaces in logcat instead of
  manifesting as a silent empty list.

### azt_collab_client 0.19.0 — suite-wide last-opened-project state (`recent.last_project`)
- New ``azt_collab_client.recent`` module with ``last_project()`` and
  ``set_last_project(langcode)``, persisted to
  ``$AZT_HOME/config.json`` under ``recent.last_langcode``. Re-exported
  from the package root.
- Same store as ``i18n``'s ``ui.language`` — no daemon RPC, just a
  file the client reads/writes; peers converge through the shared
  config without an explicit coordination channel. Recorder writes
  the langcode after every successful pick; the next peer launch
  (recorder, viewer, future apps) reads it at startup and lands on
  the same project.
- Resolve langcode → current path/URI via the existing
  ``open_project(langcode)`` (returns the daemon's authoritative
  ``Project`` record). ``recent.last_project()`` deliberately returns
  just the langcode, not a path — paths/URIs can shift across syncs;
  the langcode is stable.
- The "one store — suite-wide prefs AND state" rule generalises: the
  recorder's ``prefs['last_lift']`` was the second peer-private
  cache to fall under the rule (after ``prefs['ui_language']`` in
  0.16.0). Future cross-peer signals (last entry within project,
  contributor name) follow the same model.

### azt_collabd 0.14.5 — list_projects path/count diagnostic
- ``_h_list_projects`` now prints the resolved
  ``projects.json`` path it just read from plus the count and
  langcodes returned. Combined with the 0.14.4
  ``clone registered langcode=… → <path>`` print on the write
  side, the two log lines pin down whether the
  vanishing-projects bug is a write-vs-read path mismatch
  (different ``$AZT_HOME`` resolution between the two call
  sites) or a write-failed-silently issue. The actual on-disk
  content can be verified with ``adb shell run-as
  org.atoznback.aztcollab cat files/azt/projects.json``.

### azt_collabd 0.14.4 — inline langcode preview in clone popup; diagnostic prints for missing-after-relaunch projects
- Clone-URL popup gained an inline ``code: <derived>`` readout
  with an inline **change code** button right above the URL
  field. The readout updates live as the user types the URL;
  no separate confirmation step. Tapping **change code** swaps
  the readout for a small editable field — once the user takes
  control there's no auto-revert.
- Open-file flow uses the LIFT-filename-stem-derived langcode
  silently (no popup); user can rename later through whatever
  rename affordance lands.
- Diagnostic prints added in two spots so the user-reported
  "previously cloned projects don't show up on next open"
  actually points somewhere:
  - ``_clone_worker`` after ``projects.register``, prints the
    langcode and the resolved ``projects.json`` path. If the
    print appears, the registry write thinks it succeeded.
  - ``picker_app.list_projects`` (host method) prints how many
    projects came back from the daemon and which langcodes.
    If this prints 0 right after a successful clone, persistence
    or path-resolution is the issue (likely an ``$AZT_HOME``
    that differs between the clone-time process and the picker
    relaunch).

### azt_collabd 0.14.3 — clone accepts user-chosen langcode on input; rename_project endpoint
- ``POST /v1/projects/clone`` body gains optional ``langcode``;
  the picker collects an explicit value via the
  ``confirm_langcode_popup`` (client 0.18.2) before kicking the
  clone, so the project lands in ``projects.json`` keyed on the
  user's choice from the moment the daemon first sees it. No
  rename-after-the-fact in the registry. Empty ``langcode`` falls
  back to the daemon's auto-derivation from the LIFT filename /
  repo URL — matches the legacy desktop / scripted-call shape.
- ``_clone_worker`` gained an ``override_langcode`` kwarg; the
  registration step prefers it over ``derive_langcode``.
- New ``POST /v1/projects/<langcode>/rename`` endpoint
  (``_h_rename_project``) and ``projects.rename(old, new)``
  helper. Not used by the picker (which sets-on-create instead),
  but exposed for future flows that might let the user re-key a
  project after the fact (e.g., a settings-screen "rename
  project" affordance).

### azt_collabd 0.14.2 — clone job response carries canonical langcode
- Closes the azt-viewer 0.5.1 TODO ("picker should emit canonical
  langcode, not just leave the URI to be parsed"). The clone job
  response now includes ``langcode`` alongside ``lift_path`` —
  the same value the daemon just keyed the projects.json entry
  with on auto-register. ``_clone_worker`` captures it, the
  ``DONE`` job-state stash records it, ``_h_clone_status``
  passes it through.
- Source-of-truth chain (none of these need to derive from the
  URI on the peer side any more):
  - clone → daemon ``projects.derive_langcode`` → clone job →
    client ``clone_project`` returns ``langcode`` → picker stamps
    Intent extra.
  - open-file → ``register_project`` returns ``Project.langcode``
    → picker stamps Intent extra.
  - existing-project tap → ``picker.py`` populates the button
    with ``btn.langcode = projects_list_entry.langcode`` →
    ``load_lift(path, langcode)`` → picker stamps Intent extra.
  - template flow → user-typed BCP-47 (``_pending_vernlang``)
    already stamps the Intent extra.
- Backward-compatible. Old clients ignore the extra ``langcode``
  field on ``_h_clone_status`` responses; new clients hitting old
  daemons see ``langcode == ''`` and the peer's URI-parse
  fallback (defence-in-depth) still kicks in.

### azt_collab_client 0.18.3 — clone popup: only one input field active at a time
- ``clone_url_popup`` reworked so the URL field and the code field
  are mutually exclusive — only one is enabled at any moment, so
  the on-screen keyboard never argues with itself between two
  text inputs.
  - Mode A (default): code-preview row shows ``code: <derived> [change code]``;
    URL field is the active input.
  - Mode B (after tapping **change code**): code-preview row swaps
    in an editable code field with an ``[OK]`` button; the URL
    field is set to ``disabled=True`` (grays out, displays the
    typed URL, no input focus).
  - Tapping **OK** commits the typed code, re-enables the URL
    field, and swaps Mode A back in. Empty typed code clears the
    override so URL→code syncing resumes; non-empty pins the
    user's value through subsequent URL edits.
- Submit (Clone) works in either mode: takes the live code input
  in Mode B, the saved override in Mode A (post-OK), or the
  current URL-derived value otherwise. URL still required.

### azt_collab_client 0.18.2 — confirm-langcode popup: set on creation, not afterwards
- New ``ui.popups.confirm_langcode_popup(initial, on_submit)``:
  shows the auto-derived langcode in an editable field, asks the
  user to confirm or correct it. ``on_submit(chosen)`` fires on
  Confirm (and on Cancel with the original ``initial``, so the
  flow always resolves). Re-exported from ``azt_collab_client.ui``.
- ``picker_app.clone_dialog`` now derives a tentative langcode
  from the URL repo basename, runs ``confirm_langcode_popup``
  immediately after the URL submit, and only kicks the clone
  once the user confirms — passes the chosen value to
  ``clone_project(url, dest, langcode=chosen)``. The daemon
  registers under that exact key (no post-hoc rename). Helper
  ``_tentative_langcode_from_url`` mirrors the daemon's
  derivation order without requiring a filesystem path.
- ``picker_app.open_file._on_chosen`` runs
  ``confirm_langcode_popup`` after the file is picked but
  before ``register_project``; the chosen value goes to the
  registration call directly. Helper
  ``_tentative_langcode_from_lift`` strips the ``.lift``
  extension off the basename for the prefill.
- ``clone_project()`` / ``clone_project_start()`` accept an
  optional ``langcode=''`` kwarg routed into the request body;
  empty preserves the legacy auto-derivation behaviour for
  desktop scripted callers.
- New ``rename_project(old_langcode, new_langcode)`` wrapper for
  the daemon's ``/v1/projects/<langcode>/rename`` endpoint.
  Currently unused by the picker (set-on-create supersedes it),
  but exposed in ``__all__`` for peer apps that want to surface
  a "rename this project" affordance later.

### azt_collab_client 0.18.1 — clone_project carries langcode; project-list buttons stash langcode for load_lift
- ``clone_project()`` now returns ``langcode`` alongside
  ``lift_path`` / ``result`` / ``error`` on the success branch
  (DONE state). Daemon-side companion in 0.14.2.
- ``picker.py`` existing-project list now stores the project's
  canonical langcode on each button at populate time (the
  ``name`` half of the host's ``list_projects()`` tuple is already
  the langcode by contract) and passes it through on tap:
  ``app.load_lift(b.lift_path, getattr(b, 'langcode', ''))``. Host
  ``load_lift`` signature gains an optional ``langcode``
  parameter; default keeps existing single-arg callers working.

### azt_collab_client 0.18.0 — MediaHandle + audio_uri_for / image_uri_for
- New ``audio_uri_for(lift_path_or_uri, basename)`` and
  ``image_uri_for(lift_path_or_uri, basename)`` composer helpers.
  Given the picker-emitted LIFT path/URI plus a basename, return
  the sibling resource's URI (on Android-content URIs) or
  filesystem path (desktop) — so callers stay agnostic about the
  path/URI distinction. URI form composes
  ``content://<auth>/<lang>/{audio|images}/<basename>``, mirroring
  the daemon's ``_resolve_path`` whitelist. Filesystem form is
  ``os.path.join(os.path.dirname(lift_path), {audio|images}, basename)``.
- New ``MediaHandle(path_or_uri, kind='audio'|'image')`` —
  ``LiftHandle`` subclass with a ``kind`` for log lines / error
  messages, and a write-mode gate: ``open_write()`` on
  ``kind='image'`` raises ``PermissionError`` (images are
  read-only from peers; the daemon owns image additions).
- Re-exported from the package root: ``from azt_collab_client
  import MediaHandle, audio_uri_for, image_uri_for``.
- Together with ``LiftHandle`` (0.17.0), this is the full Tier 3
  cross-package toolkit the recorder migration documented in
  ``CLAUDE.md`` needs to land audio recording and image rendering
  on the new Android server-APK model.

### azt_collab_client 0.17.1 — client-first asset model: new icon_path helper, gear bundled
- New ``azt_collab_client.ui.icons`` module with public
  ``icon_path(name)`` — returns the absolute path to a bundled icon
  under ``azt_collab_client/ui/assets/icons/<name>.png`` (canonical
  location), falling back to ``assets/<name>.png`` for the legacy
  flat layout where ``gear.png`` currently lives. Returns ``''`` if
  the asset isn't bundled. Re-exported from
  ``azt_collab_client.ui.icon_path``.
- ``picker.py`` now resolves its default gear icon through
  ``icon_path('gear')`` (was a private ``_BUNDLED_GEAR`` constant
  with the same effect) so the discovery seam is reused for the
  next batch of shared icons.
- ``CLAUDE.md`` UI section documents the **client-first asset model**:
  shared-shape assets (gear, sync, share, future back/close glyphs)
  default to ``azt_collab_client/ui/assets/icons/`` so sister apps
  inherit them for free; peer-specific icons (recorder microphone /
  redo / app-icon variants) stay in the peer; existing recorder KV
  references with relative ``icons/<name>.png`` paths still work in
  the recorder's own cwd and don't need migrating.
- **Asset migration done:** ``sync_dark`` / ``sync_light`` /
  ``share_dark`` / ``share_light`` / ``gear_dark`` / ``gear_light``
  copied from ``azt_recorder/icons/`` into
  ``azt_collab_client/ui/assets/icons/``. ``icon_path('sync_dark')``
  etc. now resolve to the bundled package paths; the next sister-app
  peer (viewer, future clients) gets them with zero per-app work.
  The recorder's existing relative ``icons/<name>.png`` references
  still resolve in its own cwd, so this change is non-breaking;
  recorder-side migration to ``icon_path()`` can be opportunistic.

### azt_collab_client 0.17.0 — LiftHandle: cross-package LIFT-file access for peer apps
- New ``azt_collab_client.lift_io`` module exporting ``LiftHandle``
  and ``is_content_uri``. ``LiftHandle(path_or_uri).open_read()`` /
  ``.open_write()`` returns a binary file-like usable with
  ``ElementTree.parse`` / ``ElementTree.write`` regardless of
  whether the picker's emitted ``path`` is a filesystem path
  (desktop) or a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI (Android, new model). On the URI branch, opens via
  ``ContentResolver.openFileDescriptor`` and ``os.fdopen`` on the
  detached FD; the file owns the FD lifetime (close-on-exit
  through the context-manager protocol).
- Re-exported from the package root: ``from azt_collab_client import
  LiftHandle, is_content_uri``. Added to ``__all__``.
- **No caching layer** — every read/write hits the daemon's
  canonical copy through the provider. Lost-update protection
  relies on the daemon's serialization. The new
  ``azt_collab_client/CLAUDE.md`` "LIFT-file access" section
  spells out the migration checklist for peers (recorder first,
  viewer next): replace every ``open(lift_path)`` with
  ``LiftHandle(p).open_read() / .open_write()``; do not introduce
  a peer-side cache; do not compute sibling paths via
  ``os.path.dirname`` on a URI. Also documents the patterns NOT
  to use.

### azt_collab_client 0.16.0 — single source of truth for UI language; new public display_name + scan_catalog_languages
- ``set_language(lang)`` no longer takes a ``persist`` keyword argument.
  There is no transient mode any more — one preference, one store
  (``$AZT_HOME/config.json :: ui.language``), sticks everywhere until
  the next change. Internal apply-without-persist behaviour is now a
  private ``_apply(lang)`` used only by the auto-init-on-import path.
  Breaking change for callers that passed ``persist=False`` to apply a
  preference without rewriting the file; the new pattern is just
  ``set_language(language_pref())`` (idempotent re-write).
- New public ``i18n.display_name(code)`` — single source of truth for
  the language-code → human-name table. Peers that previously kept a
  parallel ``_DISPLAY_NAMES`` dict (the recorder's ``i18n.py`` did)
  now import this instead, eliminating the drift risk.
- New public ``i18n.scan_catalog_languages(locale_dir, domain)`` —
  walks ``<locale_dir>/<lang>/LC_MESSAGES/<domain>.{po,mo}`` and
  returns ``[(code, display_name), ...]``. Both the client's own
  ``available_languages()`` and the recorder's wrapper now share this
  shape, so peer catalogs and the client catalog enumerate
  identically.
- Updated callers: ``azt_collabd/ui/picker_app.py`` (build-time apply
  + mtime watcher) drop ``persist=False``; both calls just write the
  same value back, harmless because the mtime watcher's
  ``persisted == current_language()`` short-circuit prevents loops.

### azt_collabd 0.14.0 — content:// URIs across the picker boundary; clone auto-registers; MIN_CLIENT_VERSION → 0.17.0
- The picker (when running in the standalone server APK on Android)
  now emits a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI from ``_emit_and_quit``, instead of an absolute filesystem
  path inside the server APK's private ``filesDir``. The Intent
  carries the URI on its ``data`` field and adds
  ``FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION``
  so the calling peer can open the URI via
  ``ContentResolver.openFileDescriptor`` for the result delivery's
  lifetime.
- This removes a cross-package access bug (recorder's
  ``ElementTree.parse(path)`` raised ``[Errno 2]`` on an absolute
  path inside ``/data/user/0/org.atoznback.aztcollab/files/``,
  which the recorder's UID can't read). The provider's existing
  ``openFile`` callback (``_resolve_path`` under
  ``$AZT_HOME/projects/``) handles the URIs without changes —
  except for a leading-slash strip on ``Uri.getPath()`` so the
  path composes correctly. The single canonical copy in the
  daemon's ``$AZT_HOME`` stays the source of truth; peers don't
  cache.
- Successful clones now auto-register via
  ``projects.register(langcode, dest_dir, lift_path, remote_url)``.
  Previously a clone left the working tree on disk but no entry in
  ``projects.json``, so subsequent ``list_projects()`` /
  ``sync_project(langcode, …)`` calls couldn't find it. Failure
  is logged but doesn't fail the clone job — caller can re-register
  explicitly.
- ``MIN_CLIENT_VERSION`` raised to ``0.17.0`` because the URI shape
  is a hard contract: a peer bundling a pre-LiftHandle client would
  try to ``open()`` the URI as a path and crash on the spot. Old
  peers now get ``client_too_old`` from ``check_server_compat()``
  at startup with a clear "update this app" prompt.

### azt_collabd 0.13.21 — Bundle-based result extras to fix cross-package no_path loss
- Logcat showed the picker emitting a real lift_path
  (``/data/user/0/.../foo.lift``) via ``_emit_and_quit``, but the
  calling recorder still reported ``no_path`` — meaning the
  Intent's ``path`` extra was being lost across Android's IPC
  delivery to the peer process.
- Two likely culprits, both addressed by the same patch:
  1. ``Intent()`` with no action has been observed to drop extras
     on cross-package result delivery in some Android versions.
     The result Intent now carries the same action the recorder
     used for the request (``org.atoznback.aztcollab.PICK_PROJECT``).
  2. ``Intent.putExtra(String, String)`` is one of ~15 overloads;
     jnius's overload resolution can silently bind to a non-String
     overload when both args are CPython strings, leaving
     ``getStringExtra`` returning null on the peer side. Switched
     to the explicit, single-signature
     ``Bundle().putString('path', ...)`` →
     ``Intent.putExtras(Bundle)``.
- Added a diagnostic round-trip: the picker now reads the path
  back out of the result Intent (via ``getStringExtra``) right
  before ``setResult`` and prints it to logcat. If the verify
  print shows the path correctly but the recorder still gets
  ``no_path``, the loss is in Android's binder layer (genuinely
  rare); if the verify print is empty, the typed-Bundle approach
  also failed and we know to look further upstream.

### azt_collabd 0.13.20 — clone-flow diagnostic prints
- Added prints (to stderr → logcat ``python`` tag) at every step
  of the clone flow so the next ``no_path`` reproduction tells us
  exactly where the empty-path emission originates: ``clone worker
  starting``, ``clone returned`` (with ok / lift_path / error), the
  exception path, ``_after_clone_ok``, ``_after_clone_fail`` (with
  result codes), and ``load_lift`` (existing-project tap).
- Fixed a duplicate ``load_lift`` definition: a diagnostic-printing
  version was added without removing the original, and Python's
  last-def-wins on class bodies meant the diagnostic version was
  silently shadowed. Removed the second definition.

### azt_collabd 0.13.19 — debug bump for no_path triage
- Version-only bump so the user can verify on the picker's bottom
  strip (``server 0.13.19``) that the deployed build includes the
  ``_emit_and_quit`` empty-path guard from 0.13.15 and the
  Connect/Disconnect colour reactivity from 0.13.18.

### azt_collabd 0.13.18 — Connect/Disconnect button colour tracks connection state
- The four host action buttons (Connect / Disconnect GitHub,
  Set GitLab credentials / Disconnect GitLab) used to be statically
  green for Connect and dim for Disconnect. The visually-prominent
  button now matches the user's likely next action: Connect is
  green when not connected, Disconnect is green when connected.
  Dim button stays clickable so reconnect-to-refresh-tokens and
  similar flows still work — colour is a hint, not a gate.
- The colour swap is driven by ``refresh()`` reading
  ``credentials_status``, so the same round-trip that updates the
  status block also updates these buttons. Re-renders every time
  the screen is entered or the user taps Refresh Status.

### azt_collabd 0.13.17 — Refresh button rename + reposition
- ``Refresh`` button renamed to ``Refresh Status`` (more honest
  about what it does — it only re-pulls the credential / online
  read-out, doesn't do a sync) and moved to sit directly under the
  status block. Affordance for "I changed something in another
  window, pull the updated state" is now immediately adjacent to
  the data it refreshes, with no spacer in between.

### azt_collabd 0.13.16 — settings layout: status to the bottom, host rows compacted
- ``SettingsScreen`` reorder: actionable rows (interface language,
  contributor name, GitHub/GitLab connect+disconnect) at the top;
  the read-only ``Status`` block moved to the bottom. Users land
  here to do something, not to inspect — surfacing the controls
  first matches the visit pattern.
- GitHub and GitLab each collapsed from "section header + Connect
  row + Disconnect row" (3 rows of vertical real estate) into a
  single row with ``Connect…`` and ``Disconnect`` side-by-side.
  Brand name is implicit in the button text. About 100dp of
  vertical space recovered.

### azt_collabd 0.13.15 — server-owned contributor; auto-start device flow; loading-overlay wrap; empty-path guard
- New server-owned contributor field. ``store.get_contributor()`` /
  ``set_contributor(name)`` persist a display name to
  ``$AZT_HOME/config.json :: collab.contributor`` (sibling to
  ``ui.language`` — config, not credentials). New endpoints
  ``GET /v1/config/contributor`` and ``POST /v1/config/contributor``;
  client wrappers ``azt_collab_client.get_contributor`` and
  ``set_contributor``. ``store.get_status()`` now includes
  ``contributor`` so the settings UI gets it on the existing
  credentials-status round-trip. ``_h_project_sync``,
  ``_h_init_project``, ``_h_project_sync_async``, and
  ``scheduler._run_sync`` all route through new
  ``store.resolve_contributor(passed)`` which prefers the caller's
  explicit value, then the stored display name, then the
  ``'Recorder'`` fallback. Peers can stop carrying their own "Your
  name" preference; the suite has one source of truth on the server.
- ``SettingsScreen`` got a "Your name (appears in commits)"
  ``ThemedInput`` field with a transient "Saved." confirmation; the
  field auto-saves on focus loss. Refresh repopulates it from the
  server only when the user isn't actively editing.
- ``GitHubConnectScreen`` auto-starts the device flow on screen
  entry — no more "tap Begin to start" friction. The Begin button
  stays around as a Retry surface, re-enabled by the worker's
  failure path.
- ``picker_app._show_loading_overlay`` Label now wraps on width
  (same fix as ``_show_error``); long ``Cloning <url>...``
  messages no longer overflow both edges.
- ``picker_app._emit_and_quit`` refuses to emit an empty path. On
  Android an empty path lands at the peer's
  ``pick_project_android`` handler as ``RESULT_OK`` with no extra
  and surfaces as ``no_path``. If anything upstream tries it now,
  we log a stack trace to logcat and show an "Internal error:
  tried to return an empty path" modal so the user can pick again
  instead of bouncing back to the recorder with a cryptic failure.

### azt_collabd 0.13.14 — error-modal text wraps; auto-copy GitHub user_code
- ``picker_app._show_error`` Label was constructed with
  ``text_size=(None, None)``, which disables wrapping — long
  messages overflowed both edges of the modal. Now binds
  ``text_size`` to the Label's width so text wraps inside the
  modal panel; height stays free so the texture grows vertically
  as needed (modal's fixed height clips the bottom for very long
  messages, acceptable for typical 2–3-line errors).
- GitHubConnectScreen used to auto-copy the device-flow
  ``user_code`` to the clipboard so users could paste it into the
  GitHub device page without an extra tap; this regressed during
  the settings UI restyle. Restored the auto-copy and append the
  existing ``(code copied)`` translated suffix to the on-screen
  message when the copy succeeds (silently no-ops if Clipboard is
  unavailable, e.g. on a headless device).

### azt_collabd 0.13.13 — Android-aware ``azt_home()`` (and azt_collab_client mirror)
- ``[Errno 13] Permission denied: '/data/.local/share/azt/...'`` on
  every file op (template download, sync, etc.). p4a does not set
  ``$HOME``, so ``os.path.expanduser('~')`` resolved to ``/data`` —
  the Android system-data root, owned by ``root``, not writable by
  the app's UID. ``azt_home()`` then composed a path no app can
  write to.
- ``paths.py`` (both ``azt_collabd/`` and ``azt_collab_client/`` —
  duplicated by design) gained a ``_android_files_dir()`` helper
  that calls ``PythonActivity.mActivity.getFilesDir()`` via jnius
  and returns the app's private writable filesDir
  (``/data/user/0/<pkg>/files``). ``azt_home()`` checks that first
  on Android (after ``$AZT_HOME``, before XDG fallbacks). Desktop
  unchanged. The ``$AZT_HOME`` env-var override still wins for
  test rigs.

### azt_collabd 0.13.12 — settings Back button uses a glyph CharisSIL has
- ``SettingsScreen``'s "← Back" button used U+2190 (LEFTWARDS
  ARROW) which isn't in the CharisSIL glyph table — rendered as
  tofu under the project's default linguistic font. Swapped for
  ``«`` (U+00AB, left guillemet): present in every Latin font,
  reads as a back-pointer, and is the natural French equivalent
  too.

### azt_collabd 0.13.11 — preserve ``back_to`` across language-toggle screen rebuild
- ``_set_ui_language`` (settings UI) and ``_check_language_change``
  (picker subprocess) rebuilt the ScreenManager by recreating each
  screen with ``cls(name=name)``. That recipe loses any property
  the parent KV rule set on the *instance* (not the class) —
  notably ``back_to: 'picker'`` on ``SettingsScreen`` in
  ``picker_app._PickerRoot``. Symptom: the in-screen "← Back"
  button vanished the first time the user toggled language and
  didn't come back when toggling to English.
- Both rebuild loops now capture ``back_to`` per-screen before the
  ``clear_widgets`` and re-apply after instantiation. Generic
  enough to extend to other instance-level properties later.

### azt_collabd 0.13.10 — Android back button on picker subscreens
- Hardware back / gesture on Android does not flow through
  ``App.on_request_close`` (which only fires for the desktop X
  button). It surfaces as ``key 27`` on ``Window.on_keyboard``.
  Without an explicit binding, Kivy's default for an unhandled key
  is ``App.stop`` — so back from settings / github / gitlab /
  langpicker was closing the picker activity entirely.
- ``PickerApp.on_start`` now binds ``Window.on_keyboard`` to a new
  ``_on_back_button`` handler that delegates to ``_navigate_back``
  (extracted from the existing ``on_request_close`` logic). On a
  non-picker screen, back navigates to the screen's ``back_to``
  property (or ``'picker'`` by default); on the picker itself, back
  falls through to the normal ``RESULT_CANCELED + finish()`` exit.
  Same screen-pop dance the recorder uses.

### azt_collabd 0.13.9 — full traceback on template-download failures
- ``_h_create_project_from_template`` was masking the original
  failure type by catching ``Exception`` and returning only
  ``str(ex)``. On the device a ``PermissionError`` surfaced as
  ``provider HTTP 500: [Errno 13] Permission denied`` with no path
  or call site. Now logs the full traceback to stderr/logcat
  (``adb logcat | grep -i from_template``) and includes
  ``traceback`` and ``ExceptionType`` in the response body so the
  caller (picker / recorder) can surface them.
- Confirms in code comments that the template download is an
  anonymous public HTTPS GET — no GitHub credentials consulted.

### azt_collabd 0.13.8 — drop "Active host" toggle from settings UI
- ``SettingsScreen`` no longer renders the "Active host" SectionLabel
  + GitHub/GitLab two-button row. URL-based credential routing
  (``store.get_sync_credentials(url)`` → ``host_for_url(url)``)
  has handled every common case since 0.12.0; the toggle was
  vestigial. ``choose_host`` method dropped from ``SettingsScreen``.
  ``set_collab_host`` server endpoint and client wrapper stay around
  for wire compat (peers still calling them are safe; the value
  silently affects only the self-hosted/unknown-URL fallback path
  through ``get_collab_host()``).
- The eventual "Publish" flow for new local-only projects will
  pick credentials by inspecting which hosts have stored creds,
  prompting the user only when more than one is configured —
  rather than reading a global "active host" preference. Captured
  here so future-me doesn't reintroduce the toggle.

### azt_collabd 0.13.7 — locale files packaged in server APK
- ``server_apk/buildozer.spec`` ``source.include_exts`` was
  ``py,xml,gz,png``, silently dropping the ``.po``/``.mo`` files
  under ``azt_collab_client/locales/`` at packaging time. On the
  device, ``available_languages()`` walked an empty locale tree and
  the settings UI's language toggle only offered English regardless
  of which catalogs lived in the source tree. Added ``po,mo`` to
  the extension list. Pre-compile any language's ``.mo`` before
  rebuilding so the catalog ships pre-baked (faster first paint;
  also dodges any APK-readonly issue with the runtime
  ``_ensure_mo``):
  ``python -c "from azt_collab_client.i18n import _ensure_mo;
  _ensure_mo('fr')"``.

### azt_collabd 0.13.6 — typing_extensions in APK requirements; BodyLabel recursion fixed at class level
- Added ``typing_extensions`` to the server APK's ``requirements``
  in ``server_apk/buildozer.spec``. dulwich (and a few of its
  transitive imports) reach for ``typing_extensions`` at runtime;
  on Android it isn't pulled in by default. Previously a clone
  attempt would fail with ``ImportError: no module named
  typing_extensions`` at the moment dulwich tried to do its first
  network operation. Adding the recipe to requirements puts it on
  the APK's PYTHONPATH. **Requires a clean build**
  (``buildozer android clean && buildozer android debug deploy``)
  for p4a to pick up the new recipe.
- Promoted the ``text_size: self.width, None`` fix from
  per-instance overrides on three ``BodyLabel`` uses to the
  ``<BodyLabel@Label>`` class rule itself. Any ``BodyLabel`` whose
  ``height: self.texture_size[1] + dp(8)`` would otherwise loop
  with ``text_size: self.size`` is now safe by default. The earlier
  per-instance overrides remain (redundant but harmless).

### azt_collabd 0.13.5 — picker version-probe diagnostics
- ``picker_app._probe_server_version`` now surfaces *why* the probe
  failed when it can't show a real server version. Instead of a bare
  ``server ?``, the bottom strip renders one of
  ``server ? (server_unreachable)`` /
  ``server ? (server_too_old)`` /
  ``server ? (client_too_old)`` /
  ``server ? (<ExceptionType>: ...)``. Distinguishes transport down
  vs. version-handshake reject vs. RPC exception without needing
  ``adb logcat``. Also prints a one-line diagnostic to stderr/logcat
  so the post-mortem is in both places.

### azt_collabd 0.13.4 — debug version bump
- No code change. Version-only bump so a freshly-rebuilt server APK
  reports a different ``__version__`` from the previous build,
  letting the user verify on the picker's bottom strip
  (``client X · server Y``) that the device is actually running the
  new build vs a cached install.

### azt_collabd 0.13.3 — picker shows both versions, auth-fallback for clone failures
- Picker bottom strip now shows ``client X · server Y``. The server
  half is fetched off the UI thread via
  ``check_server_compat()``; renders ``server ?`` if the daemon is
  unreachable. Was: client only.
- ``_after_clone_fail`` got a fallback path: when the daemon's
  worker didn't run far enough to attach ``CLONE_AUTH_REQUIRED``
  (e.g. the clone-job kickoff itself failed and the result is None)
  but the error string smells like auth (401 / 403 / 404 /
  unauthorized / forbidden / not found / authentication /
  credential), the auth-prompt modal still appears with the **Open
  settings** button. Same heuristic the daemon uses, mirrored
  client-side for the result-is-None case.
- Auth-modal "Open settings" button now calls ``self.go_config()``
  (in-process screen swap) instead of the removed
  ``open_server_ui`` import. The Android Intent dance is gone from
  this flow entirely.

### azt_collabd 0.13.2 — picker hosts settings screens in-process
- Picker's gear used to call ``azt_collab_client.open_server_ui()``,
  which on Android fires ``getLaunchIntentForPackage`` on the server
  APK. Because the server APK has a single ``PythonActivity`` already
  running the picker, Android collapsed the task back to the calling
  peer (the recorder) instead of switching to settings — there was
  no path forward from the picker.
- ``azt_collabd/ui/app.py`` now exposes a top-level
  ``register_kv(font_name)`` (idempotent) that loads the settings/
  GitHub/GitLab KV. ``CollabUIApp.build`` calls it; the picker_app
  also calls it before its own KV so all class rules are in scope.
- ``picker_app._PickerRoot`` ScreenManager now carries
  ``SettingsScreen`` (with ``back_to: 'picker'``),
  ``GitHubConnectScreen``, and ``GitLabFormScreen`` alongside the
  existing ``ProjectPickerScreen`` / ``LangPickerScreen``. ``go_config()``
  is now ``self.sm.current = 'settings'`` — no Intent, no subprocess.
  Same code path on desktop and Android.
- New ``go(name)`` method on ``PickerApp`` mirrors ``CollabUIApp.go``
  so the existing settings-side KV (``app.go('github')``,
  ``app.go('gitlab')``, ``app.go('settings')``) just works in both
  hosts.
- New ``back_to`` ``StringProperty`` on ``SettingsScreen``. When set
  (the picker_app ``_PickerRoot`` KV sets it to ``'picker'``) the
  screen renders an additional **← Back** ``NavBtn`` at the top of
  the layout. Hidden / disabled in the standalone settings host
  where back has no meaning. The Android back gesture / window-close
  on non-picker screens is also intercepted by ``on_request_close``
  to navigate back instead of exiting.
- Known limitation: a peer calling ``open_server_ui()`` *while a
  picker is already up* still hits the Android launch-flag bug
  (settings doesn't appear; task may collapse to the peer). Rare in
  practice and not worth a separate ``SettingsActivity`` declaration
  yet — tracked as future work.

### azt_collabd 0.13.1 — settings UI Clock-iteration warning fix
- ``BodyLabel`` instances that combined ``text_size: self.size``
  (inherited) with ``height: self.texture_size[1] + dp(8)`` were
  forming a feedback loop on Android: texture_update changes height
  → parent BoxLayout do_layout → child resize → text_size changes →
  texture_update fires. Tolerable before; pushed past Kivy's
  per-frame Clock iteration limit by the new language-toggle row
  and the wrapping of every settings-UI string in ``_(...)``.
  Surgical fix on the three offending BodyLabels (status_label,
  gh_message, the gitlab-form intro): override
  ``text_size: self.width, None`` so the wrap width is bound but
  height flows from content alone, breaking the cycle.

### azt_collabd 0.13.0 — settings UI translatable, language toggle, picker live retranslation
- ``SettingsScreen`` gained an **Interface language** row at the top
  with one button per ``azt_collab_client.i18n.available_languages()``.
  Selecting a language calls ``i18n.set_language(code)`` (which
  persists to ``$AZT_HOME/config.json`` under ``ui.language``) and
  rebuilds every screen in the manager so KV ``text: _('...')``
  bindings re-evaluate against the new catalog. Same dance the
  recorder's ``ConfigScreen`` uses.
- Every visible string in ``azt_collabd/ui/app.py`` (Settings, GitHub
  device-flow, GitLab form) is now wrapped in ``_(...)``. KV imports
  ``_ azt_collab_client.translate.tr`` so subsequent
  ``set_translator``/language switches take effect.
- ``picker_app.py`` watches ``$AZT_HOME/config.json`` mtime once a
  second (Clock interval). When the persisted language changes — for
  example because the user just toggled it in a settings subprocess
  opened from the gear — the picker rebuilds its screens in place.
  The user sees the picker live-retranslate without restart.
- Apply persisted language at picker / settings startup so first
  paint is in the right language.

### azt_collabd 0.12.1 — picker gear wired to settings, both versions on settings page
- Standalone picker (``python -m azt_collabd projects``) now shows
  the settings gear in the top-right and wires it to the daemon's
  settings UI via ``open_server_ui()`` instead of the previous no-op
  stub. Rationale: first-time users land on the picker and need a
  visible path to authentication; previously they had to fail a
  clone before the auth-prompt modal offered one.
- Settings UI (``python -m azt_collabd ui``) now displays both the
  client and server versions in the bottom version strip:
  ``client 0.14.1  ·  server 0.12.1`` — used to show only the
  daemon version. The settings UI subprocess imports
  ``azt_collab_client`` for ``__version__``.
- Both version labels (settings + picker) bumped from
  ``font_size: sp(11) / color: T.TEXT_FAINT`` to
  ``sp(13) / T.TEXT_DIM`` so they're legible without straining.

### azt_collabd 0.12.0 — URL-based credential routing, CLONE_AUTH_REQUIRED, MIN_CLIENT_VERSION handshake
- New ``azt_collabd.MIN_CLIENT_VERSION = '0.14.0'`` — floor on the
  ``azt_collab_client`` version this daemon will talk to. ``/v1/health``
  now publishes ``min_client_version`` alongside ``version``. Symmetric
  to the existing client-side ``MIN_SERVER_VERSION``: when a peer ships
  a too-old client, the peer's startup handshake gets ``client_too_old``
  and can prompt the user to update. Bump this constant in lockstep
  with any wire-format addition that older clients can't decode (e.g.
  the new ``CLONE_AUTH_REQUIRED`` status added in this release).
- ``store.get_sync_credentials()`` now takes an optional remote URL and
  picks credentials by host (``github.com`` → GitHub creds,
  ``gitlab.com`` → GitLab creds), falling back to the user's saved
  ``collab_host`` only when the URL is unrecognized (self-hosted /
  empty). All call sites — ``_h_init_project``, ``_h_clone_project``,
  ``_h_project_sync`` in ``server.py`` and ``_run_sync`` in
  ``scheduler.py`` — pass the remote URL. Symptom this fixes: the user
  picks a LIFT project without first visiting Settings; the daemon
  used to send GitHub creds to a GitLab remote (or vice-versa) just
  because ``collab_host`` was the wrong value.
- New helper ``store.host_for_url(url)`` exposes the URL → host
  classifier.
- New status code ``CLONE_AUTH_REQUIRED`` (with ``host`` param). The
  clone worker now appends it after final failure when either (a) no
  token was available for the URL's host, or (b) the dulwich error
  contains 401/403/404/auth keywords. The picker UI uses this to
  branch into an auth-prompt modal instead of a generic error.
- The auth-shaped retry already in ``_clone_worker`` was extracted to
  ``_clone_error_looks_like_auth(result)`` and now also recognises
  ``not found`` / 404 (private-repo case) — previously only matched
  ``credential`` / ``auth``, so a 404-bearing failure skipped the
  anonymous retry.

### azt_collab_client 0.15.2 — Android ContentProvider path delivery fix
- **Critical bug fix.** The Android transport
  (``azt_collab_client/transports/android_cp.py``) built a URI like
  ``content://<authority><path>`` and called
  ``ContentResolver.call(uri, method, None, extras)``. But the
  ``call(Uri, method, arg, extras)`` overload only delivers
  ``method``, ``arg``, and ``extras`` to
  ``ContentProvider.call(method, arg, extras)`` — the URI's path is
  consumed by provider routing and never reaches the dispatch.
- AZTCollabProvider.java reads ``arg`` as the path
  (``cb.dispatch(method, arg != null ? arg : "", body)``), so on the
  daemon side every RPC was being dispatched with ``path=""``,
  producing ``{ok: False, error: 'not_found'}``. User-visible
  symptom: every clone / list / sync / credential RPC silently
  routed to the dispatcher's catch-all 404 branch.
- Fix: pass the dispatch path as ``arg`` instead of None. The URI
  shrinks to just the authority (no path component) since it's only
  used for provider routing now. One-line change at the call site.
- This was a long-standing bug that likely went unnoticed because
  legacy peers symlinked ``azt_collabd`` and used the loopback
  transport (Python interpreter in-process). Peers on the new
  ContentProvider-only model would have hit it on every non-ping
  call.

### azt_collab_client 0.15.1 — picker gear icon bundled in package
- Picker KV used to reference the gear icon as a relative
  ``'icons/gear.png'`` path, which only resolves when the host's
  cwd happens to contain that file (worked from the recorder repo
  root, broke everywhere else — most visibly in the standalone
  picker subprocess, where Kivy fell back to its missing-image
  texture and the gear rendered as a white square).
- The icon is now an absolute path computed from the package
  location: ``azt_collab_client/ui/assets/gear.png``. The KV
  template injects it at ``register_kv`` time alongside ``font_name``.
- ``register_kv`` (a.k.a. ``register_picker_kv``) gained an optional
  ``gear_icon=`` kwarg so hosts that want a custom icon can pass
  one explicitly (the recorder still ships its own at
  ``azt_recorder/icons/gear.png``); default falls back to the
  package-bundled file.
- **Important**: the binary file
  ``azt_collab_client/ui/assets/gear.png`` is not committed by this
  change; copy from any peer that already has one
  (e.g. ``cp /home/kentr/bin/AZT/azt_recorder/icons/gear.png
  azt_collab_client/ui/assets/gear.png``).

### azt_collab_client 0.15.0 — own i18n domain (azt_collab_client.po), pure-Python msgfmt, fallback chain in translate.tr
- New module ``azt_collab_client.i18n`` owns gettext domain
  ``azt_collab_client``. Public API:
  ``set_language(lang, persist=True)``, ``current_language()``,
  ``available_languages()``, ``language_pref()``, ``_(msg)``,
  ``gettext_translation()``. Persists the active language to
  ``$AZT_HOME/config.json`` under ``ui.language``; auto-applies that
  preference at import so all suite subprocesses converge on the same
  language without a coordination channel.
- New locale tree
  ``azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po``
  with French translations of all client-owned strings: picker UI,
  langpicker, popups, ``translate.py`` status messages (the full
  ``S.*`` set), and the settings-UI strings now owned by the client.
- ``i18n.py`` ships a pure-Python PO→MO compiler (msgfmt-lite — single
  magic, sorted msgid array, two parallel offset tables packed via
  ``struct``). Runs lazily on first ``set_language`` whenever the
  ``.mo`` is missing or older than the ``.po``. So peers that ship
  only the ``.po`` (or contributors editing translations in-place) do
  not need a build-time ``msgfmt`` step.
- ``translate.py`` default translator changed from "try
  ``from i18n import _`` (recorder)" to "use the client catalog
  directly". ``set_translator(host_tr)`` overrides as before, but
  ``tr(msg)`` now falls **back** to the client catalog whenever the
  host translator returns ``msg`` unchanged. The fallback layer means
  embedded peers (the recorder) do not need to duplicate client
  strings into ``aztrecorder.po``: a string the recorder catalog
  doesn't know falls through to ``azt_collab_client.po``. Owns its
  own strings, no duplication, gettext-canonical.
- Behavior change to be aware of: hosts that previously relied on
  the implicit ``from i18n import _`` fallback now get the client
  catalog first. Hosts with their own catalogs should keep calling
  ``set_translator(host._)`` at startup; the new fallback in
  ``tr()`` handles client strings transparently.

### azt_collab_client 0.14.1 — picker version label clarified
- Picker bottom-strip version label changed from ``collab X.Y.Z`` to
  ``client X.Y.Z`` so users can tell client and server versions
  apart at a glance (the settings page shows both).
- ``ProjectPickerScreen`` version label sized up:
  ``font_size: sp(13)`` / ``color: T.TEXT_DIM`` (was
  ``sp(11)`` / ``T.TEXT_FAINT``). Same change applied in
  ``azt_collabd/ui/app.py`` for consistency.

### azt_collab_client 0.14.0 — auth-prompt modal on clone failure, client_too_old handshake
- ``check_server_compat()`` gained a third branch: when the server's
  ``min_client_version`` is greater than this client's ``__version__``,
  the function now returns
  ``{'ok': False, 'error': 'client_too_old', 'client_version', 'server_version', 'min_required'}``.
  Mirrors the existing ``server_too_old`` shape so peer apps can branch
  on the same dict. Forward-compatible with pre-0.12.0 daemons that
  don't publish ``min_client_version`` (treated as "no floor").
- New status mirror ``S.CLONE_AUTH_REQUIRED`` and translation:
  *"Clone failed — repository not found. This may be a private
  repository. Are you authenticated to {host}?"* (host is rendered
  Title-cased: GitHub / Gitlab).
- ``azt_collabd/ui/picker_app.py`` clone-fail flow now threads the
  daemon's ``Result`` through ``_after_clone_fail``. When the result
  carries ``CLONE_AUTH_REQUIRED``, the modal renders the translated
  prompt and an extra **Open settings** button that calls
  ``azt_collab_client.open_server_ui()``. Previously the user saw a
  bare *"Clone failed: not found"* and had no path forward — they
  would not have visited Settings before picking a project, so we
  lead them there.
- ``_show_error`` grew an optional ``extra_button=(label, callback)``
  argument so the same modal helper can host either a single Dismiss
  button (existing behavior) or a two-button row.

### azt_collabd 0.11.0 — settings UI restyle, picker typography fix
- The standalone settings UI (``python -m azt_collabd ui`` /
  launcher activity in the server APK) was using stock Kivy buttons
  with no theme, no ``font_name``, and no top bar. It looked
  unrelated to the recorder. Restyled to mirror the recorder's
  ``CollabScreen``: themed top bar (``T.SURFACE`` background,
  ``T.ACCENT`` bold title), ``BG``-painted screens, ``SectionLabel``
  / ``HeaderLabel`` / ``BodyLabel`` / ``DimLabel`` dynamic classes,
  ``ThemedInput`` for text fields, ``RecBtn`` for primary actions,
  ``NavBtn`` for secondary navigation. The host toggle now highlights
  the active host with ``T.GREEN`` (was: a disabled stock button).
- The standalone picker (``picker_app.py``) was rendering its
  ``RecBtn`` with raw ``font_size: 16`` (un-scaled pixels — tiny on
  hi-dpi phones), no ``font_name``, and a hardcoded blue
  ``(0.2, 0.6, 1, 1)`` instead of a theme colour. Replaced with the
  recorder's idiom: ``font_size: sp(16)``, ``font_name: FONT``,
  ``normal_color: T.ACCENT``. The error / loading modal overlays
  were also unstyled stock widgets; now ``T.SURFACE`` rounded
  panels with ``T.TEXT`` labels and a themed dismiss button.
- Both apps now call ``register_charis()`` from the new shared
  helper at startup. If the CharisSIL TTFs can be located (the
  recorder's ``fonts/`` dir during desktop dev, system font dirs on
  Linux, or a future ``azt_collab_client/fonts/`` location) they
  register under LabelBase name ``CharisSIL``; otherwise the apps
  fall back to ``Roboto`` silently. The standalone server APK
  doesn't currently bundle the TTFs (~20 MB), so on-device it falls
  back to Roboto — sizes and theme are aligned, glyphs are not.
  Bundling the fonts in the server APK is a follow-up.

### azt_collab_client 0.13.6 — shared CharisSIL helper, larger picker logo
- New ``azt_collab_client.ui.fonts.register_charis()`` (re-exported
  as ``azt_collab_client.ui.register_charis``). Discovers
  CharisSIL-Regular/Bold/Italic/BoldItalic TTFs across a small list
  of likely locations (canonical client ``fonts/`` slot, sibling
  recorder ``fonts/`` dir, system font dirs) and registers them
  under LabelBase name ``CharisSIL``. Returns the LabelBase name to
  use (``'CharisSIL'`` or ``'Roboto'``); idempotent.
- ``ProjectPickerScreen`` typography: logo grew from ``dp(200)`` to
  ``dp(240)``, title from ``sp(28)`` to ``sp(32)``, subtitle from
  ``sp(16)`` to ``sp(18)``. Logo gets explicit
  ``allow_stretch / keep_ratio: True`` so the larger size doesn't
  pixelate. Title / subtitle now centred with explicit
  ``halign: 'center'`` + ``text_size: self.size`` so they don't
  drift left in narrow layouts.

### azt_collab_client 0.13.5 — open_server_ui dispatches on Android + shared install popup
- ``open_server_ui()``'s docstring has long promised that on Android
  it would dispatch an Intent to the server APK's launcher activity.
  It now does. New ``_open_server_ui_android`` resolves
  ``PackageManager.getLaunchIntentForPackage('org.atoznback.aztcollab')``
  and starts the activity. Returns
  ``{'ok': True, 'launched': 'android-apk'}`` on success.
- If the APK isn't installed, the helper opens a new install-prompt
  popup (``ui.popups.install_server_apk_popup``) and returns
  ``{'ok': False, 'error': 'server_apk_not_installed', 'prompted': True}``
  so the caller knows the popup is on screen. The popup itself
  routes through ``Intent.ACTION_VIEW`` on Android and
  ``webbrowser.open`` on desktop, both pointed at
  ``SERVER_APK_INSTALL_URL``.
- New optional ``on_status`` callback on ``open_server_ui`` /
  ``install_server_apk_popup``: the popup uses it to surface
  "could not open install page — …" failures into the host's
  status bar without the host having to reach into the popup
  internals. Sister apps that don't pass ``on_status`` still work.
- ``ui.popups`` and ``ui/__init__`` export
  ``install_server_apk_popup`` so peers can also call it directly
  (e.g. from a startup-time ``ServerUnavailable``-handling path,
  not just from the settings button).
- Decouples sister apps from per-app reimplementations of
  "launch APK / show install prompt" and lets the viewer collapse
  ~60 lines of jnius / popup boilerplate.

### azt_collabd 0.10.6 — pin AZTCollabProvider callback proxies
- **Bug fix:** ``android_cp/service.install_callbacks`` was passing
  freshly-constructed ``_Dispatch()`` / ``_OpenFile()`` PythonJavaClass
  instances inline to ``Provider.registerCallbacks``. Java held refs;
  Python did not. After a GC cycle (typically within seconds of the
  picker Activity launching) the proxy instances were freed, and the
  next binder-thread call from a peer's ContentResolver into
  ``AZTCollabProvider.call`` dereferenced the dead type object. Net
  effect on hardware: ``Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR),
  fault addr 0x143`` on Thread-3, with backtrace through
  ``NativeInvocationHandler.invoke`` → ``_PyObject_GenericGetAttrWithDict``
  → ``_PyType_Lookup``. From the peer's perspective the picker
  Activity vanished and the recorder logged
  ``[activity-result] code=18247 result=0`` (default RESULT_CANCELED
  from the killed Activity).
- Fix: store strong refs to both proxies at module scope before
  handing them to Java. Validate with a clean rebuild
  (``buildozer android clean && buildozer android debug``).

### azt_collab_client 0.13.4 — gear icon source conditional
- The picker KV had an unconditional ``source: 'icons/gear.png'`` even
  when the host passed ``hide_settings_gear=True`` (size goes to 0×0
  in that case but Kivy still tries to resolve the source). The
  standalone picker app and any sister app that hides the gear was
  logging ``[ERROR ] [Image ] Not found <icons/gear.png>`` on every
  picker open. Cosmetic, not the cause of the segfault — but the
  standalone server APK has no ``icons/`` dir of its own so the line
  was particularly visible there.
- Fix: gate the source on the same ``{show_gear}`` template flag the
  size already uses (``source: 'icons/gear.png' if {show_gear} else ''``
  in ``azt_collab_client/ui/picker.py``).

### azt_collabd 0.10.5 — server APK ships non-Python assets
- **Bug fix:** `server_apk/buildozer.spec` had `source.include_exts =
  py,xml`, which silently stripped `azt_collab_client/ui/assets/
  langtags_mini.json.gz` and `azt_collab_client/azt.png` from the
  packaged APK. Net effect: tapping **Start New** in the picker
  Activity raised `FileNotFoundError: ... langtags_mini.json.gz` from
  `LangPickerScreen._load_langtags`, which crashed the Activity; the
  Activity then finished without an explicit `setResult(-1, ...)` so
  peers saw `[activity-result] code=18247 result=0` (RESULT_CANCELED
  for `_AZT_PICK_REQ_CODE`). The same was true for any flow that
  reaches the langpicker, including the path between Clone-Internet
  and the post-clone confirmation when the user backs out via the
  langpicker.
- Fix: extend `source.include_exts` to `py,xml,gz,png`. `gz` covers
  the langtags blob; `png` covers the suite icon used by `App.icon`
  in `azt_collabd/ui/picker_app.py`. Validate with a clean rebuild
  (`buildozer android clean && buildozer android debug`) — hot
  patches into `.buildozer/.../build/` only prove the patch *content*
  is right, not that the spec change actually fires.

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

### azt_collab_client 0.13.3 — pick_project UI-thread JNI dispatch
- Bug fix: `_pick_project_android` was building the
  `ActivityResultListener` proxy on the worker thread that called
  `pick_project()`. Worker threads attached by jnius' thread hook
  don't carry the app `ClassLoader`, so
  `find_javaclass('org/kivy/android/PythonActivity$ActivityResultListener')`
  fell back to the system loader (which has no app inner classes)
  and raised `JavaException: ClassNotFoundException`. Net effect:
  the recorder fired the `PICK_PROJECT` Intent, the worker thread
  died before `startActivityForResult` ever ran, the recorder's
  blocking modal stayed up forever showing only "Pick a project to
  continue. Cancel" with no picker Activity behind it.
- Fix: dispatch all JNI work (autoclass lookups, Intent build,
  `resolveActivity`, `bind(on_activity_result=...)`,
  `startActivityForResult`) to Kivy's main thread via
  `Clock.schedule_once`. The Kivy main thread is the Android UI
  thread, where the app `ClassLoader` is in scope and inner-class
  resolution works. The caller's thread only blocks on the result
  `Event`. Setup itself is bounded by a 10-second `Event.wait()` so
  a wedged UI thread can't hang the caller indefinitely.
- No API change; the function still returns the same dict shapes.

### azt_collab_client 0.13.2 — test_peer.sh fingerprint extraction
- `test_peer.sh` extends signing-fingerprint detection beyond
  keytool/dumpsys (both miss v2/v3-only signed APKs) with apksigner
  + openssl-on-META-INF fallbacks. Auto-discovers apksigner under
  `ANDROID_HOME` / `ANDROID_SDK_ROOT` / buildozer's bundled SDK at
  `~/.buildozer/android/platform/android-sdk/build-tools/*/apksigner`.
- Diagnostic chain now reports every tool that was tried so a WARN
  line tells you exactly which step failed (was: a single misleading
  message that reflected only the first failure).
- Strips `SHA[- ]?(256|1):` labels before regex extraction so the
  fingerprint match can't latch onto the `56` inside `SHA256:` and
  produce off-by-one bytes (was happening on the SUITE_FINGERPRINT
  file itself).
- Detects `CN=Android Debug` in apksigner output and reports
  `peer is signed with the Android Debug keystore; SUITE_FINGERPRINT
  check skipped (only meaningful for release builds)` instead of
  failing with a spurious mismatch. Debug builds of all suite peers
  share the same default Android debug keystore, so cross-app
  signature-permission gates still work; the suite-fingerprint check
  was only ever meant for release builds.

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
