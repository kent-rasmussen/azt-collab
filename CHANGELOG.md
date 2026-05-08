# Changelog

Two packages live here. Versions move together for now (the client
embeds `MIN_SERVER_VERSION`, so when the wire format changes we bump
both); patch-level bumps in one without the other are fine.

- **azt_collabd** — server / daemon. Source of truth: `azt_collabd.__version__` (re-imported by `server.py` as `_VERSION` for the wire response).
- **azt_collab_client** — client library. Source of truth: `azt_collab_client.__version__`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely.

## [Unreleased]

### azt_collabd 0.31.0 + azt_collab_client 0.31.0 — minor bump for pre-distribution test
- Lock-step minor bump on both packages, with both floors moved
  to 0.31.0 (``MIN_CLIENT_VERSION`` on the daemon,
  ``MIN_SERVER_VERSION`` on the client). Folds in everything
  since 0.30.0: server-APK boot-on-lazy-spawn, sticky-bound
  ``:provider`` host, self-update auto-exit, GitHub-connect UX
  rewrite (state-machine + suspended-install detection +
  Verify-setup re-test affordance), pre-install APK validation
  (parse + signature + asset.digest cache freshness), bootstrap
  flow with mandatory vs voluntary distinction, server→peer
  language mirror, blocked-popup mailto link + Check-again,
  same-tag re-upload detection via ``last_seen_digest``, install
  poll lifecycle, suite-wide CONFIRMED-vs-CONNECTED gating, and
  the various translation additions and Kivy touch-routing fixes.
  Anything older talking to a 0.31 peer (or vice versa) gets a
  clean ``client_too_old`` / ``server_too_old`` and is routed
  through the bootstrap update flow.

### azt_collab_client 0.30.47 — mandatory-mode probe always forces prompt
- **Regression from 0.30.46.** Recording ``gh_digest`` on
  Update-tap fixed the voluntary loop, but introduced a hole on
  the mandatory path: if the install failed (user cancelled at
  the Android installer screen, signature mismatch, etc.), the
  next ``_probe`` saw ``last_seen == gh_digest`` →
  ``digest_changed=False`` → ``needs_update=False`` →
  ``_show_no_newer_release`` fired even though GitHub actually
  has the right bytes (we just failed to install them).
- **Fix.** Replaced ``legacy_mandatory_force`` (only handled
  the first-run unknown-baseline case) with ``mandatory_force``
  (always forces the prompt in mandatory mode when the release
  feed returned something to download). Digest comparison is for
  "is there something new to offer?" — irrelevant when the daemon
  has already declared the client too old.
- **Clarification.** The 0.30.46 changelog framing "stuck at
  old version with no prompt" was sloppy. Voluntary install fail
  is benign — the client was already compatible (otherwise the
  daemon would have returned ``client_too_old`` and the mandatory
  path would have been used). The user runs a working older
  build and can retry via the peer's in-app Update button.
  Mandatory path is enforced terminal — no chance of leaking the
  user into project loading without a working daemon.

### azt_collab_client 0.30.46 — record gh_digest as last_seen on Update tap
- **Bug.** User report: "I click update on a voluntary screen,
  and it doesn't download… because it seems to be running from
  cache, this window keeps coming up since the digest is still
  different from gh." Trace:
  ``last_seen='3687f3…' gh_digest='efb3ae…'
  version_newer=False digest_changed=True``.
  Cache check correctly skipped the download (bytes already at
  ``efb3ae…``), install fired, but ``_record_last_seen_digest``
  was never called outside the first-run baseline branch — so
  ``last_seen`` stayed at ``3687f3…`` forever. Next bootstrap
  saw ``digest_changed=True`` and re-prompted.
- **Fix.** ``_prompt_self_update`` now receives ``gh_digest``
  from ``_probe`` and records it as the new ``last_seen`` baseline
  on Update-button tap. Recording at tap time (vs. install-complete)
  is the practical compromise: same-tag re-uploads don't flip
  versionName, and self-installs kill our process during install,
  so we have no reliable in-process completion signal. If the
  install ultimately fails the user is stuck at the old version
  with no further prompt — recoverable via the in-settings
  Update button or a manual reinstall.

### azt_collabd 0.30.45 + azt_collab_client 0.30.45 — re-bump for mandatory-update test pass
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.45** to force the
  ``client_too_old`` path on any peer bundling an older client
  (continuing test pass for the digest-change decline fix).

### azt_collabd 0.30.44 + azt_collab_client 0.30.44 — decline-memory ignores same-tag re-uploads
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.44** to force the
  ``client_too_old`` path on any peer bundling an older client
  (test pass for the digest-change decline fix).
- **Bug.** User report: "Check again doesn't currently show a new
  apk online, despite a different sha256." Trace confirmed the
  probe was correctly setting ``digest_changed=True``
  (``last_seen='3687f35493c3'`` ≠ ``gh_digest='cceb3fc2ba05'``),
  yet no prompt appeared. Cause: the decline-memory check in
  ``_peer_update_with_confirm._probe`` only compared the version
  tag — a previous "Not now" tap against ``1.37.24`` masked
  every subsequent re-upload at the same tag, regardless of
  digest. The original comment ("a re-upload of a declined
  version still plausibly came with the user's prior decline
  intact") turned out to be wrong: a re-upload IS a different
  build the user has not been asked about.
- **Fix.** Decline check now skips when ``digest_changed`` is
  True, treating the new bytes as a fresh release. Also
  belt-and-braces gates the check on ``not mandatory`` so the
  ``client_too_old`` path can't be silenced by a stray decline
  entry from an earlier voluntary cycle.

### azt_collabd 0.30.43 + azt_collab_client 0.30.43 — mirror daemon UI language to peer + clearer mandatory-update wording
- **Language sync server → peer.** User report: switching the
  server APK's settings UI to French didn't translate any
  bootstrap-side popups in the peer (recorder / viewer).
  Cause: ``$AZT_HOME/config.json::ui.language`` is the
  persistence path, but on Android ``$AZT_HOME`` is per-process
  private (server APK has its own filesDir, each peer has its
  own), so file-system writes from the server's settings UI
  never reached peer disk. Fix:
  - New daemon endpoint ``GET /v1/config/ui_language`` returns
    the server-side persisted language (handler
    ``_h_get_ui_language`` in ``azt_collabd/server.py``).
  - New client wrapper ``get_server_ui_language()`` in
    ``azt_collab_client/__init__.py``.
  - ``bootstrap._sync_ui_language_with_daemon()`` runs at
    ``_check_server`` entry (immediately after a successful
    ``check_server_compat``) and applies the daemon's language
    via ``i18n.set_language`` so all peer-side popups + status
    text track. Best-effort: silent on RPC failure, peer
    keeps its local pref.
- **Mandatory-update wording.** User report: viewer at 0.8.2
  saw "AZT Viewer 0.8.2 is required" — confusing when 0.8.2
  is the current version (same-tag re-upload case where
  digest changed). Now branches in
  ``_prompt_self_update``:
  - ``latest_version != peer_version`` (genuinely newer
    release): "{name} {peer_v} is too old for the AZT
    Collaboration service. Tap Update to install {name}
    {latest}, or Quit to close this app." Both versions named.
  - ``latest_version == peer_version`` (same-tag re-upload —
    digest_changed=True triggered the prompt): "A new build
    of {name} {version} is available. The current build is
    too old for the AZT Collaboration service. Tap Update to
    install the new build, or Quit to close this app." No
    longer phrases the same version as both "what you have"
    and "what's required".
- Email-link-in-the-update-popup ask is deferred —
  ``install_server_apk_popup``'s body Label doesn't have
  ``markup=True`` yet, and the change isn't a one-liner.
  Tracked separately.

### azt_collabd 0.30.42 + azt_collab_client 0.30.42 — bump MIN_CLIENT_VERSION to 0.30.42
- Final test-pass bump on the daemon (``MIN_CLIENT_VERSION =
  "0.30.42"``) to exercise the mandatory-self-update flow now
  that ``_prompt_self_update`` calls ``App.stop()`` on
  install completion. Server APK rebuild required; peer at
  0.30.41 trips ``client_too_old`` and gets the mandatory
  Update / Quit popup.
- Drop ``MIN_CLIENT_VERSION`` back to a real-world floor
  before any release that ships in the public update
  channel. Same goes for ``MIN_SERVER_VERSION`` on the client
  side.

### azt_collabd 0.30.41 + azt_collab_client 0.30.41 — close peer cleanly after a self-install
- User report: "When I clicked update, it downloaded and
  installed fine, but then I found myself back at the same
  popup. I closed and restarted fine, but users shouldn't have
  to do that." Android usually kills the running peer during a
  self-install, but not always — on some devices the peer
  survives, comes back to foreground after the system
  installer dismisses, and the popup is right where it was.
  No way to recover without a manual restart.
- Fix: ``_prompt_self_update`` now passes the peer's own
  package name as ``install_target_package`` (read from
  ``PythonActivity.mActivity.getPackageName()``) so
  ``install_apk_from_url`` runs the post-install poll loop
  on it, and ``on_install_complete`` calls
  ``App.get_running_app().stop()`` so the running peer exits
  the moment ``PackageManager`` reports the new versionName.
  Next user launch lands on the new APK; no manual restart.
- Safe both ways: if Android kills us during install (the
  common case), the poll thread dies with us — no leak. If
  Android doesn't kill us, the poll fires, we self-stop. The
  pre-0.30.41 comment about "polling our own package would
  block forever" was wrong: while the system installer is in
  foreground the Kivy Clock pauses, but it resumes when we
  come back, the poll detects the change, and we exit.

### azt_collabd 0.30.40 + azt_collab_client 0.30.40 — distinguish mandatory peer self-update from voluntary
- ``_prompt_self_update`` was using the same body + dismiss
  action for both the voluntary "newer version available" path
  and the mandatory ``client_too_old`` path. Declining a
  mandatory update via "Not now" silently fell through to
  ``on_done`` and dropped the user into the peer with a daemon
  that wouldn't talk to them.
- New ``mandatory`` parameter on ``_prompt_self_update``
  (forwarded from ``_check_self`` via
  ``_peer_update_with_confirm``):
  - **Voluntary** (``mandatory=False``, default): existing body
    "A newer version of this app ({version}) is available." —
    Update button + Not now (dismiss). Decline memory still
    applies so we don't re-prompt for the same version.
  - **Mandatory** (``mandatory=True``, the ``client_too_old`` +
    newer-version-exists path): body "{name} {version} is
    required to use the AZT Collaboration service. Tap Update
    to download and install it, or Quit to close this app." —
    Update button + Quit (action=``'quit'``, peer
    ``App.stop()``). No decline memory: a mandatory update
    can't usefully be remembered as "declined".
- Net effect: declining a mandatory update closes the app,
  matching the ``_show_release_too_old`` /
  ``_show_no_newer_release`` Quit semantics. Symmetric with
  ``_prompt_server_update``'s "Update" button + the existing
  install popup's Quit button on the server-side path.

### azt_collabd 0.30.39 + azt_collab_client 0.30.39 — bump MIN_CLIENT_VERSION to 0.30.39
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.39)
  so a peer at 0.30.38 paired with this server APK trips
  ``client_too_old``. Continues exercising the new
  digest-aware probe + version-anchor popup from 0.30.37/.38.
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.38 to actually trigger client_too_old.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.38 + azt_collab_client 0.30.38 — detect same-tag re-uploads via asset digest persistence
- Per maintainer ask: the version-tuple "any newer release"
  probe in ``_peer_update_with_confirm._probe`` couldn't see a
  re-uploaded asset on the same release tag. Common in dev
  iteration (push fix, keep tag), and unavoidable when a tag
  is hot-fixed in place. The new branch catches it via a
  digest-change check.
- ``_last_seen_digest(repo)`` and
  ``_record_last_seen_digest(repo, digest)`` persist the
  GitHub ``asset.digest`` per peer repo via the existing
  ``peer_pref`` / ``set_peer_pref`` store
  (``peer_prefs.last_seen_digests`` dict, keyed by
  ``owner/repo``).
- ``_probe`` now reads ``asset.digest`` from the latest
  release JSON, compares against the persisted last-seen, and
  treats EITHER a newer version tag OR a changed digest as
  "newer available". A trace line ``[bootstrap] _probe …
  version_newer=… digest_changed=…`` prints both signals so
  flaky cases are diagnosable from logcat.
- First-run baseline: if no digest is on file for ``repo``,
  the current GitHub digest gets recorded as the starting
  point so subsequent probes can detect change. Misses the
  perverse "re-uploaded between install and first launch"
  edge case but covers the dev-iteration use case cleanly.
- Storage scope: ``peer_prefs`` writes to whatever
  ``$AZT_HOME/config.json`` resolves to in the calling peer's
  process (peer-private on Android, shared on desktop), which
  is correct — each peer tracks its own repo independently.

### azt_collabd 0.30.37 + azt_collab_client 0.30.37 — fix client_too_old popup: version anchors + drop nonsensical pre-flight + Check-again tracing
- User report: "Recorder is too old" popup gave no version
  information either on screen or in the email body, and Check
  again seemed to do nothing.
- **Drop the pre-flight comparison in ``_check_self`` for the
  client_too_old path.** ``required_min`` from the daemon refers
  to the **client library** version (``azt_collab_client.
  __version__``), not the peer-app version. The recorder peer
  bumps its own version (1.34.0, …) independently of the client
  lib, so comparing recorder release tags to client-lib version
  numbers is meaningless — recorder 1.34.0 vs lib 0.30.36 has no
  defined order. The pre-flight either trivially passed or
  trivially failed depending on which way the major-version
  numbers happened to land. Replaced with the simpler "is there
  ANY newer peer release available" check that
  ``_peer_update_with_confirm`` already performs. We can't
  inspect a remote APK's bundled client-lib version without
  downloading it, so any-newer-version is the only honest signal.
- **Version anchors in the popup body and email.**
  ``_show_no_newer_release`` now takes ``required_client_lib``
  and ``bundled_client_lib`` and surfaces all four version
  values: peer name + peer version, bundled client lib (this
  build), and required client lib (from the daemon's compat
  handshake). Body reads e.g. "Recorder 1.34.0 is too old for
  the AZT Collaboration service. This build bundles client
  library 0.30.35; the service requires 0.30.36 or newer. No
  newer Recorder release is published yet." Email body lists
  the same anchors as labelled lines so the maintainer has the
  full mismatch in one read.
- **Check again tracing.** ``[bootstrap] Check again pressed —
  invalidating release cache + re-entering _check_server`` now
  prints when the button fires; ``_check_server`` is wrapped in
  try/except so any exception during the retry surfaces in
  logcat instead of silently dying in the worker thread. Lets
  us tell apart "Check again ran but result is the same"
  (visible re-render) from "Check again failed silently" (no
  trace lines after).

### azt_collabd 0.30.36 + azt_collab_client 0.30.36 — bump MIN_CLIENT_VERSION to 0.30.36
- ``MIN_CLIENT_VERSION`` raised to 0.30.36 (peer wandered to
  0.30.35, so we need to stay one ahead). Server APK rebuild
  required to pick up the new floor; peer at 0.30.35 trips
  ``client_too_old`` against this daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.35 + azt_collab_client 0.30.35 — bump MIN_CLIENT_VERSION to 0.30.35
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.35)
  so a peer at 0.30.34 paired with this server APK trips
  ``client_too_old``. Lets us exercise the new
  ``_show_no_newer_release`` popup (parity with
  ``_show_release_too_old``: Check again + mailto + Quit, no
  fall-through to ``on_done``).
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.34 so the test triggers cleanly.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.34 + azt_collab_client 0.30.34 — Update-needed popup parity + don't drop into client on dismiss
- ``_show_info`` (the "Update needed" single-button popup that
  fired on the ``client_too_old`` + no-newer-release branch) had
  two problems:
  1. UI didn't match ``_show_release_too_old``: only an OK
     button, no mailto link, no Check-again.
  2. After the user tapped OK, ``_check_self`` fell through to
     ``_on_done_and_release`` — host loaded a project, daemon
     refused subsequent RPCs, user stuck in a half-broken UI.
- Refactor: extracted the popup body into
  ``_show_update_blocked_popup(ctx, body_text, mailto_subject,
  mailto_body)``. Two callers — the existing
  ``_show_release_too_old`` and a new
  ``_show_no_newer_release`` — share the same Check-again /
  Quit / ``[ref=email]`` mailto-link UI vocabulary. The "Update
  needed" / OK / fall-through popup is gone.
- ``_check_self``'s on_no_update force_prompt branch now
  surfaces ``_show_no_newer_release`` and **does not** call
  ``_on_done_and_release``. Popup is terminal — Quit stops the
  app via ``App.stop()`` (no half-broken UI), Check again
  invalidates the release cache + re-runs ``_check_server``.

### azt_collabd 0.30.33 + azt_collab_client 0.30.33 — bump MIN_CLIENT_VERSION to 0.30.33
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.33)
  so any peer at 0.30.32 or earlier paired with a 0.30.33 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Continues exercising the symmetric self-update flow now that
  the bootstrap install-cache fix is in.
- Server APK rebuild required to pick up the new floor; peer
  can stay at 0.30.32 (or older) to actually trigger
  client_too_old when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.32 + azt_collab_client 0.30.32 — install_apk_from_url: validate cache against GitHub digest
- User report: bootstrap reused a cached 0.30.28 server APK
  instead of downloading the published 0.30.29, then Android
  rejected the install with "package appears to be invalid"
  (downgrade attempt). Root cause: ``install_apk_from_url``
  is the direct-URL path (called from
  ``install_server_apk_popup`` which bootstrap dispatches),
  with no GitHub-API cross-check. ``_has_fresh_download`` ran
  in sidecar mode — cached file matched its sidecar SHA, so
  reused — but sidecar mode can't tell "intact bytes" from
  "intact-but-stale-version bytes". ``check_for_update`` got
  the digest-mode fix in 0.30.22; ``install_apk_from_url``
  was still on the old path.
- Fix: new optional ``repo`` parameter on
  ``install_apk_from_url``. When supplied, the worker fetches
  the GitHub release JSON, finds the matching asset's
  ``digest`` (sha256:hex), and threads it through to
  ``_has_fresh_download`` as ``expected_sha256``. Stale
  caches with the right SHA-vs-sidecar but wrong SHA-vs-
  GitHub now fall through to a fresh download. Failure of
  the metadata fetch (network glitch) falls back to sidecar
  mode rather than blocking the install.
- ``install_server_apk_popup`` accepts and forwards the
  ``repo`` parameter; bootstrap's four call sites
  (_prompt_server_install / _prompt_server_update /
  _prompt_server_unresponsive / _prompt_self_update) all
  pass ``ctx.server_repo`` or ``ctx.peer_repo`` as
  appropriate.
- Net: the same digest-driven cache-freshness story that
  applies to settings-screen "Update this app" now also
  applies to bootstrap's "Install / Update AZT
  Collaboration" popup and to peer self-update from the
  bootstrap path.

### azt_collabd 0.30.31 + azt_collab_client 0.30.31 — log SHA check result in _has_fresh_download
- ``_has_fresh_download`` ran the cache integrity check
  (digest-mode against GitHub's asset digest, sidecar mode
  otherwise) but did so silently — user reported "no SHA log
  line, is this a regression?" against 0.30.28. Test was
  running, just invisible.
- Added a single ``[update] _has_fresh_download: ...`` print
  per call that names the mode (digest / sidecar), shows the
  truncated file SHA + expected SHA, and the boolean match
  result. Also prints early-exit reasons (file missing, hash
  failed, sidecar missing, sidecar empty, sidecar read
  error) so a False return tells you *why*. No behavior
  change.

### azt_collabd 0.30.30 + azt_collab_client 0.30.30 — bump MIN_CLIENT_VERSION to 0.30.30
- ``MIN_CLIENT_VERSION`` raised to 0.30.30 on the daemon so any
  peer running 0.30.29 or earlier paired with a 0.30.30 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Lets us exercise the symmetric ``_check_self`` /
  release-too-old / Check-again flow on the *peer* side
  (which previously we'd only proven for the server-too-old
  direction).
- Server APK rebuild required to pick up this floor change;
  peer can stay at 0.30.29 (or older) to actually trigger the
  client_too_old branch when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.29 + azt_collab_client 0.30.29 — bump MIN_SERVER_VERSION to 0.30.28
- ``MIN_SERVER_VERSION`` raised to 0.30.28 so the latest peer
  build keeps triggering ``server_too_old`` against any server
  APK at 0.30.27 or earlier (continuing the testing thread for
  the release-too-old / Check-again flow).
- Drop back to a real-world floor before any release that
  ships in the public update channel.

### azt_collabd 0.30.28 + azt_collab_client 0.30.28 — debug rebuild
- No code change; bump for a fresh peer APK to retest the
  Check-again cache invalidation on the device.

### azt_collabd 0.30.27 + azt_collab_client 0.30.27 — invalidate release cache on Check again
- 0.30.26's Check again button re-ran ``_check_server`` but the
  per-process release-JSON cache (5-minute TTL on
  ``_release_cache``) returned the stale "too old" entry, so
  the popup re-rendered immediately against the same data and
  the user saw no change. Closing/reopening the peer wiped the
  cache and worked.
- Fix: new ``invalidate_release_cache(repo=None)`` helper in
  ``azt_collab_client/ui/update.py``. Bootstrap's Check-again
  handler drops both ``ctx.server_repo`` and ``ctx.peer_repo``
  entries before re-running ``_check_server`` so the next
  probe re-fetches GitHub.

### azt_collabd 0.30.26 + azt_collab_client 0.30.26 — release-too-old popup: mailto link + Check again
- "Or build from source" was unhelpful for the SIL field-
  linguist user base — they're not going to. Replaced with
  "or [send the developer an Email]" rendered as a Kivy-
  markup ``[ref=email]`` link styled with underline + accent
  blue. Tapping it opens the user's MUA via ``mailto:`` with
  a pre-filled subject ("{name}: required version not yet
  released") and body containing the version mismatch info.
  No-MUA degradation is graceful — Android shows the standard
  "no app to handle this" toast.
- New ``azt_collab_client.MAINTAINER_EMAIL`` constant
  (``kent_rasmussen@sil.org``) so forks can override in their
  own client build (no env-var hook yet — change at the
  source line, rebuild). Matches the address in
  ``SECURITY.md``.
- Added a "Check again" button alongside Quit. Dismisses the
  popup and re-enters ``_check_server`` on a worker thread —
  same code path as bootstrap's compat probe, so a
  freshly-published release that meets the floor flows
  through to the install popup without the user having to
  restart the peer.

### azt_collabd 0.30.25 + azt_collab_client 0.30.25 — pre-flight release version vs required minimum + clearer body text
- User reported: bootstrap got server_too_old, popped the
  "Update AZT Collaboration?" dialog, the user tapped Update,
  and the latest published release was *also* too old to
  satisfy ``MIN_SERVER_VERSION``. Result: download succeeds,
  install succeeds, peer hits ``server_too_old`` again. Wasted
  bandwidth + a confused user.
- Fix in ``azt_collab_client/ui/bootstrap.py``:
  - ``_release_meets_minimum(repo, required_min)`` fetches
    GitHub's latest tag for the given release feed and
    compares to the required floor. Returns ``(ok, latest,
    error)``.
  - ``_show_release_too_old(...)`` is a new one-button popup
    that surfaces "{name} {required} or newer is required, but
    the latest available release is {latest}. Wait for an
    update or build from source." with a Quit affordance.
    Replaces the install popup when the upstream release feed
    can't satisfy the floor.
  - ``_prompt_server_update`` now takes ``min_required``,
    pre-flights the server APK release feed, and surfaces the
    "release too old" popup instead of opening the install
    popup if upstream can't help. Body text on the install
    popup itself now reads "{name} {required} or newer is
    required (you have {current})" — replaces the redundant
    "AZT Collaboration service (AZT Collaboration)" wording
    the user flagged.
  - Symmetric pre-flight on the ``client_too_old`` path:
    ``_check_self`` accepts ``required_min`` (forwarded from
    the daemon's compat response) and runs the same release-
    feed check before going into ``_peer_update_with_confirm``.
    A peer running ahead of GitHub's latest publish gets the
    same "wait for an update" popup instead of a useless
    download.
- Client-only rebuild needed to exercise this — bootstrap
  runs in the peer's process. Server APK can stay at whatever
  older version is on the device (that's what triggers
  ``server_too_old`` in the first place).

### azt_collabd 0.30.24 + azt_collab_client 0.30.24 — debug rebuild + MIN_SERVER_VERSION = 0.30.24 to exercise old-server flow
- ``MIN_SERVER_VERSION`` bumped to 0.30.24 deliberately, so a
  peer carrying this client paired with a server APK at 0.30.23
  or earlier hits ``check_server_compat()`` →
  ``server_too_old``. Lets us walk the bootstrap "Update AZT
  Collaboration?" prompt end-to-end without backdating any real
  wire-format change. **Drop back to a real-world floor before
  any release that ships in the public update channel.**
- No other code change; daemon and client are otherwise identical
  to 0.30.23.

### azt_collabd 0.30.23 + azt_collab_client 0.30.23 — debug rebuild
- No code change; bump for a fresh APK to retest the
  ``asset.digest``-driven cache-freshness check on the device.

### azt_collabd 0.30.22 + azt_collab_client 0.30.22 — verify cached APK against GitHub's authoritative ``asset.digest``
- 0.30.21 caught stale-version caches via versionName parsing.
  Per maintainer suggestion: GitHub's REST release-asset
  metadata exposes a SHA-256 ``digest`` field
  (``"digest": "sha256:<hex>"``, added 2025-06-03), so we can
  do an authoritative cache-freshness check without paying for
  a re-download.
- ``_has_fresh_download`` now takes an optional
  ``expected_sha256`` arg. When supplied (asset has a digest),
  it's the strong check — file SHA must equal the GitHub
  digest to reuse. When not (legacy assets pre-2025-06), it
  falls back to the existing sidecar self-consistency check.
- ``_worker`` parses ``asset.digest`` (strips the ``sha256:``
  prefix) and threads it through. Catches three failure modes
  in one place:
  - same-version-different-bytes (re-uploaded asset),
  - corrupted on-disk cache,
  - stale-version cache from a previous Update cycle.
  The versionName check from 0.30.21 is kept as a belt-and-
  braces layer for legacy assets where the digest is null and
  the SHA fallback can't tell same-bytes-different-version
  from a normal cache hit.

### azt_collabd 0.30.21 + azt_collab_client 0.30.21 — invalidate cached APK when versionName ≠ latest
- User repro: tapped Update on 0.30.19 → cache reused → install
  ran but version stayed at 0.30.19 (the cached APK was from
  the previous update cycle). ``_has_fresh_download`` only
  validates that the cached file's bytes match the sidecar's
  SHA — i.e. on-disk integrity. It does NOT check that the
  cached file is the version we're now trying to install.
- Fix: in ``check_for_update``'s ``_worker``, after the
  fresh-download check passes, ``_apk_parse_info`` reads the
  cached APK's ``versionName`` and compares it to ``latest``.
  Mismatch → remove the cached file + sidecar so the
  download branch runs and we fetch the right version.
- Diagnostic line ``[update] cache stale: cached_version=...
  != latest=...; discarding ...`` prints when the discard
  fires, so future repros are visible in logcat.
- Why not also fix this in ``install_apk_from_url``: that
  path doesn't know the "expected version" (URL is
  redirect-style and opaque); peers using it typically run
  install-once via bootstrap, so cache staleness is much less
  of a problem.

### azt_collabd 0.30.20 + azt_collab_client 0.30.20 — log parse_info + signature_matches result
- 0.30.18's pre-install validation didn't tell us which check
  outcome we got — user reports "same invalid response" against
  0.30.18 + 0.30.19. Did the parse pass? Did signature compare
  match? Returned None? We can't tell from logcat without an
  explicit print at each point.
- Added two trace lines:
  - ``[update] parse_info: ok pkg=... versionName=... path=...``
    (or ``parse_info: None path=...``) right after
    ``_apk_parse_info`` runs. Surfaces the APK's actual package
    name and version so we can spot manifest-level mismatches
    (e.g. wrong ``packageName``).
  - ``[update] signature_matches_installed: True/False/None``
    right after ``_signature_matches_installed`` runs. ``True``
    = match (install should succeed signature-wise),
    ``False`` = mismatch (we'd surface our error and abort),
    ``None`` = couldn't determine (off Android, app not
    installed, jnius unavailable, exception path).
- No behavior change.

### azt_collabd 0.30.19 + azt_collab_client 0.30.19 — debug rebuild
- No code change; bump for a fresh APK to retest 0.30.18's
  pre-install validation + suspended-messaging on the device.

### azt_collabd 0.30.18 + azt_collab_client 0.30.18 — pre-install validation + step-by-step suspended messaging
- **Pre-install APK validation in ``check_for_update`` and
  ``install_apk_from_url``.** Before firing the install Intent
  we now run two checks via ``PackageManager``:
  - ``getPackageArchiveInfo(dest, GET_SIGNATURES)`` to confirm
    the downloaded APK is parseable. A null result means the
    download was truncated / corrupted; the cached file +
    sidecar are removed and the user gets a "could not be
    parsed; try again to re-download" error rather than the
    bare Android "package appears to be invalid" complaint
    after dispatching the Intent.
  - ``signature_matches_installed(dest, package)`` —
    compares the APK's signing certificate against the
    currently-installed app's certificate. On mismatch we
    surface "Downloaded APK is signed with a different key
    than the installed app... Uninstall first, then tap
    Update again — or rebuild from source with the matching
    keystore." That replaces the cryptic "App not installed
    as package appears to be invalid" Android error and
    points the user at both fixes.
  - Helpers (``_apk_parse_info``,
    ``_signature_matches_installed``,
    ``_installed_version_name``,
    ``_android_package_manager``) live alongside the existing
    install-Intent code in ``update.py``. All three return
    ``None`` off Android / when pyjnius is unavailable so the
    desktop / non-Android paths short-circuit cleanly.
- **Diagnostic line** at the start of ``_install_on_ui``:
  ``[update] pre-install check: pkg=... installed_version=...
  running_version=... latest=...``. Tells us whether the
  device-installed version diverges from the running code's
  ``__version__``. They should match in normal use; diverging
  values are a hot-patch / dev workflow signal.
- **Suspended-state messaging.** Old "Resume it at {url}"
  was unhelpful — gave a URL but no idea what to do on the
  page. ``GitHubConnectScreen`` now stashes the
  ``installation_id`` from the Verify-setup probe and:
  - ``install_app`` opens
    ``settings/installations/<installation_id>`` (the
    install's configure page directly) instead of the
    generic install URL.
  - The accompanying message reads "Tap 'Install GitHub App'
    below to open the install's configure page on GitHub,
    then scroll to the bottom and tap 'Unsuspend'." —
    walks the user through the actual GitHub UI flow.
  - ``_render_message`` and ``_test_done`` both branch on
    ``_suspended_installation_id`` so the message survives
    re-renders (language change, screen re-entry).
- ``S.APP_SUSPENDED`` translation (used by sync 403 path,
  which doesn't have the in-screen "Install GitHub App
  below" affordance) updated to the same self-contained
  step-by-step shape: "GitHub App installation is suspended
  at {url}. Open it, scroll to the bottom, and tap
  'Unsuspend'."

### azt_collabd 0.30.17 + azt_collab_client 0.30.17 — restore Share icon
- 0.30.11's Share/Update simplification dropped the
  share-icon Image. Restoring per user preference: the
  ``SHARE_ICON`` KV macro and ``icon_path('share_dark')``
  format-arg are back, with the Image positioned as a
  left-overlay (``x: self.parent.x + dp(12)``) inside the
  half-width Share button. Text "Share" stays centered; no
  ``padding: [dp(52), 0]`` needed since a single-word label
  doesn't collide with the icon.

### azt_collabd 0.30.16 + azt_collab_client 0.30.16 — Android back button pops sub-screens to settings
- ``CollabUIApp`` (the standalone server APK settings host)
  didn't bind Android's hardware back button, so a back-press
  from GitHubConnectScreen / GitLabFormScreen fell through to
  ``App.stop`` and closed the app — losing the user's settings
  session mid-setup. Picker_app already had this hook;
  CollabUIApp didn't.
- Added ``CollabUIApp.on_start`` to bind
  ``Window.on_keyboard`` and ``_on_back_button`` to consume
  key 27 by popping ``sm.current = 'settings'`` from any
  sub-screen. Settings-screen back returns False to let Kivy
  / Android close the app the normal way.

### azt_collabd 0.30.15 + azt_collab_client 0.30.15 — swap primary button to "Verify setup" after install_app
- User report: at step 2, ``install_app`` opens the browser
  with a message that says "return here and tap Verify
  setup", but the primary button still reads "Install GitHub
  App" — there's no Verify setup button to tap. The user
  comes back from GitHub, reads the instruction, can't find
  the button it names, gets stuck.
- Fix: ``install_app()`` now flips ``gh_primary_btn``'s
  ``text`` to "Verify setup" and ``_action`` to ``verify``
  right after opening the browser, so the affordance the
  message promises actually exists. If the user returns
  without installing (cancelled, navigated away),
  ``test_github_credentials`` reports
  ``app_installed=False`` and ``_test_done`` re-runs
  ``on_pre_enter`` which puts the button back to "Install
  GitHub App" — they can retry without screen drift.
- Auto-polling for install completion was considered and
  rejected for v1: poll cadence vs. GitHub-API quota is a
  real trade-off, and the swap-and-tap workflow is bounded
  by user attention anyway. Easy to add later if needed.

### azt_collabd 0.30.14 + azt_collab_client 0.30.14 — log account.login on /user/installations + per-account install matching
- 0.30.13's trace showed three azt-collaboration installs in
  ``/user/installations`` while the user said they only ever
  saw one at ``github.com/settings/installations``. We don't
  yet know who owns the other two — could be orgs the user
  belongs to, could be stale state, could be something else
  GitHub is doing. ``check_app_installed`` now logs
  ``account.login`` alongside the existing fields so the next
  probe answers "whose installs are these?" directly.
- ``check_app_installed`` gained an optional
  ``account_login`` parameter that narrows the match to the
  installation whose ``account.login`` matches (case-
  insensitive). When omitted, the legacy "first match by
  app_slug" behavior is preserved.
- ``test_github_credentials`` now passes
  ``server_username`` (the user's own GitHub login) as
  ``account_login`` so Verify setup checks for the install
  on the user's own account, not "any install we can see."
  This fixes a real bug observed: user uninstalled their
  personal install but was still a member of orgs that had
  ``azt-collaboration`` installed; the unscoped match
  reported ``installed=True`` against an org install and
  the screen continued to show "Setup complete." With this
  change, Verify setup correctly returns
  ``installed=False`` once the personal install is gone,
  and the screen regresses to step 2.
- ``diagnose_403`` (sync 403 path) does NOT yet take the
  repo's owner into account; that's the next change once we
  see the per-account data and confirm the matching strategy
  is right. Currently still uses the legacy unscoped
  ``check_app_installed``, so this release narrows just the
  Verify-setup path.

### azt_collabd 0.30.13 + azt_collab_client 0.30.13 — log raw /user/installations to diagnose stuck suspended state
- 0.30.12 still showed "Setup Complete" against a suspended
  install on the user's device, even though both server APK
  and client are 0.30.12. So the suspended-detection code is
  running but either ``inst.suspended_at`` isn't being set on
  the install we're looking at, or the slug match isn't
  hitting the entry. Need data to disambiguate.
- ``check_app_installed`` now logs:
  - All ``(app_slug, id, suspended_at)`` tuples returned by
    ``/user/installations`` so we can see whether the user's
    install is in the list at all and what GitHub is reporting
    for ``suspended_at``.
  - The matched entry (if any) with its suspended_at and
    repository_selection.
  - The final ``result`` dict.
  - HTTPError / general Exception (was silently caught).
- ``_h_test_github`` now logs the test_github_credentials
  return value (valid / app_installed / app_suspended /
  installation_id) plus what it actually wrote to the store.
- Pure tracing — no behavior change. Build, retry Verify
  setup against the suspended install, and the next logcat
  will tell us whether the suspended detection is firing or
  whether GitHub is reporting the install as not-suspended
  for some reason.

### azt_collabd 0.30.12 + azt_collab_client 0.30.12 — fix suspended-state message overwrite race
- 0.30.11 set the suspended-message ``gh_message.text``
  immediately in ``_test_done``, but ``self.on_pre_enter()``
  on the line above schedules ``_refresh_state`` for the next
  frame, and ``_render_message`` there overwrites the field
  with the step-N default ("Setup complete..." / "Now install
  the GitHub App..."). User report: Verify setup against a
  suspended install kept showing "Setup Complete, connected
  as ...".
- Same defer-past-render dance the AuthError handler uses.
  Suspended message now goes through a second
  ``Clock.schedule_once`` so it lands after ``_refresh_state``
  completes.
- Note: this fix requires the server APK to ALSO be running
  0.30.11+ (or this 0.30.12). Older daemons' ``check_app_installed``
  matches solely on ``app_slug`` and reports
  ``installed=True`` for any installation, so a suspended
  install never gets the ``app_suspended=True`` flag and the
  client never enters the suspended-message branch. If you
  see "Setup Complete" after rebuilding the recorder but not
  the server APK, that's why — rebuild + reinstall the server
  APK and Verify setup again.

### azt_collabd 0.30.11 + azt_collab_client 0.30.11 — detect suspended GitHub App installs + simpler Share/Update buttons
- **Suspended-install detection.** User repro: paused the
  azt-collaboration App installation in their GitHub settings,
  the connect screen still showed "Setup complete (App
  installed)", and sync silently failed with codes
  ``['NOTHING_TO_COMMIT', 'REPO_NOT_AUTHORIZED']`` because the
  daemon's ``check_app_installed`` only matched on
  ``app_slug`` and reported ``installed=True`` for any
  installation including suspended ones. Fix:
  ``check_app_installed`` now reads ``inst.suspended_at`` —
  ``installed=True`` requires the install to be active, and a
  new ``suspended=True`` field is set when the App is on file
  but paused. ``installation_id`` is also returned in the
  suspended case so the UI can construct the resume URL
  (``settings/installations/<id>``) instead of the generic
  install page.
- **New status code ``S.APP_SUSPENDED``** plus translation:
  "GitHub App installation is suspended. Resume it at {url}."
  ``diagnose_403`` returns this for sync 403s when the install
  is suspended; ``test_github_credentials`` exposes
  ``app_suspended`` + ``installation_id`` alongside
  ``app_installed`` so the connect screen's Verify setup path
  surfaces a precise message and link instead of regressing
  silently to the step-2 "Install" prompt.
- **Connect screen ``_test_done`` handles suspended.** When
  Verify setup runs against a suspended install, the message
  becomes "GitHub App installation is suspended. Resume it at
  github.com/settings/installations/<id>." — the user can tap
  the URL or open it manually, resume on GitHub, and re-run
  Verify setup to confirm.
- **Wire format change.**
  ``POST /v1/credentials/github/test`` response now carries
  ``app_suspended`` and ``installation_id`` alongside the
  existing ``app_installed`` field. Older clients ignore the
  extras; newer clients against older daemons see ``False`` /
  ``None`` defaults (which is the correct
  "no-suspended-state-known" reading).
- **Settings: simpler Share / Update.** Two stacked
  full-width "Share this app" / "Update this app" buttons
  collapsed into one half-width row labelled just "Share" and
  "Update". Drops the share icon (one less asset to ship and
  less visual noise; the action is obvious from the label).
  ``SHARE_ICON`` KV macro and the ``share_icon=icon_path(...)``
  format-arg removed; ``icon_path`` import dropped.

### azt_collabd 0.30.10 + azt_collab_client 0.30.10 — keep "Verify setup" available after setup completes
- Once the user reached step 4 (everything verified),
  ``_render_primary`` was hiding ``gh_primary_btn`` entirely —
  forcing them to Re-authenticate (which means re-typing the
  8-field code) just to confirm the connection is still
  healthy. User asked for a non-destructive "test settings"
  affordance from the verified state.
- Fix: ``_render_primary`` now keeps the button visible at step
  4 with the same ``Verify setup`` label and ``verify`` action
  that step 3 uses. ``test()`` is idempotent (just hits
  ``api.github.com/user`` with the saved token); a successful
  re-test stays at step 4, a failure surfaces by regressing the
  screen state to step 2 / step 1 / "Token rejected" — a single
  tap that doubles as a diagnostic.
- Step-4 message updated to "Setup complete. Connected as
  {username}. Tap Verify setup any time to re-test." so users
  notice the affordance is intentional.

### azt_collabd 0.30.9 + azt_collab_client 0.30.9 — detach publish_row children to free the GitLab button
- User report: "GitLab button still doesn't respond until 10-12
  clicks" while the adjacent GitHub button works fine. Same root
  cause as the earlier ``gh_primary_btn`` issue: ``publish_row``
  is the next BoxLayout below ``gl_action_btn`` and stays at
  ``height=0`` for users with no project. BoxLayout's
  ``_do_layout`` still positions ``publish_btn`` (a RecBtn with
  ``on_press``) at its explicit ``dp(52)`` height under the
  collapsed parent, and Kivy's dispatch loop visits every child
  regardless. The combination intermittently swallows touches
  near gl_action_btn. (Why "intermittent" rather than "always":
  the touch points hover near the bottom edge of gl_action_btn /
  spacing area, where Kivy's hit-test math is sensitive to the
  exact tap coordinate.)
- Fix: ``SettingsScreen._refresh_publish_row`` now detaches
  ``publish_row``'s children when hiding the row (via
  ``_detach_publish_children`` / ``_reattach_publish_children``,
  matching the pattern used in ``GitHubConnectScreen`` for
  ``gh_manage_box`` / ``gh_device_flow_box``). A parent with no
  children cannot dispatch on_touch_down to anything, so the
  hidden publish_btn can no longer eat taps meant for the
  GitLab button. Idempotent on both sides.
- The detach is keyed off the same condition that hides the
  row, so users with an active publishable project (the
  "should-show-publish" case) keep the row's full functionality.

### azt_collabd 0.30.8 + azt_collab_client 0.30.8 — Connect-button gating + Disconnect inside settings + web-flow plan
- **Settings GitHub/GitLab buttons gated on ``confirmed``, not
  ``connected``.** User reported the canonical footgun: install
  failed midway, gh.connected stayed True (token was saved),
  refresh() flipped the button to "Disconnect GitHub", and
  the only tap available was the one that wiped the partial
  work. ``refresh()`` now reads ``gh.confirmed`` /
  ``gl.confirmed`` and renders:
  - Not verified → ``Connect to GitHub`` / ``Connect to GitLab``
    in green; tap navigates to the connect screen which
    auto-resumes from the user's current step (server state is
    the source of truth).
  - Verified → ``GitHub Settings`` / ``GitLab Settings`` in the
    neutral surface color; tap opens the same screen, now
    showing the manage view.
- **Disconnect moved inside each screen.** GitHubConnectScreen
  already had Disconnect in its manage box;
  ``GitLabFormScreen`` gained one in this release (visible via
  ``gl_manage_box`` only when a token is on file). Rationale: a
  fat-finger Disconnect from the main settings has a real cost —
  re-auth on GitHub means re-typing the 8-field code, re-auth
  on GitLab means re-pasting a PAT. Audit doc #6 + #7 updated
  to reflect this.
- **Removed** ``SettingsScreen.gh_action`` /
  ``connect_github`` / ``disconnect_github`` /
  ``disconnect_gitlab``. The KV buttons call ``app.go(...)``
  directly; the disconnect helpers live on each respective
  screen instead.
- **Web-flow migration plan** drafted at
  ``docs/web_flow_migration_plan.md``. Research finding: GitHub
  Apps' OAuth web flow accepts PKCE but still requires
  ``client_secret`` on the token exchange (per
  github.blog/changelog 2025-07-14 + community/discussions
  #15752), so a pure-PKCE mobile-safe flow is not legal. The
  plan documents (a) a Phase-1 ``tests/probe_pkce.py`` that
  validates this finding against the live API, (b) a web-flow
  architecture using embedded ``client_secret`` in the server
  APK only, with PKCE as defense-in-depth and device flow as
  the universal fallback, and (c) the open decision points
  (embed-secret tradeoff, fork story, sunset window for device
  flow). Not yet approved for implementation.
- **PKCE probe script.** ``tests/probe_pkce.py`` (intentionally
  not auto-collected by pytest — no ``test_`` prefix). Walks
  the user through up to three browser authorizations and
  validates the four cases laid out in the plan: PKCE param
  acceptance, PKCE-no-secret rejection, PKCE-with-secret
  success, secret-only-no-PKCE success. Exits non-zero on any
  deviation, so it doubles as a regression check if GitHub
  later changes its stance.

### azt_collabd 0.30.7 + azt_collab_client 0.30.7 — revert bogus URL prefill (audit doc #1 was based on a false premise)
- Audit doc #1 assumed GitHub's OAuth Device Flow returns
  ``verification_uri_complete`` (RFC 8628 §3.2) or at least
  honors ``https://github.com/login/device?user_code=ABCD-1234``
  to prefill the code field. After actually researching this:
  - GitHub's documented response is exactly
    ``device_code, user_code, verification_uri, expires_in,
    interval`` — ``verification_uri_complete`` is OPTIONAL per
    the spec and GitHub omits it. The canonical ``cli/oauth``
    Go reference impl and ``octokit/auth-oauth-device.js``
    both parse the field defensively for spec compliance but
    receive empty strings against github.com.
  - GitHub's ``/login/device`` page silently ignores the
    ``?user_code=...`` query parameter. No prefill happens.
  - A Jan-2024 GitHub change adds a "select Continue on an
    account" confirmation step in front of the code form
    unconditionally, even for single-account users. The user
    confirmed seeing this with one account.
- Reverted 0.30.5's URL-suffix construction. The fallback chain
  is now defensive only: use ``verification_uri_complete`` if a
  future GitHub change starts returning it, otherwise the bare
  ``verification_uri``. No more constructed query strings.
- ``docs/github_connect_ux_audit.md`` #1 updated with the
  research finding and links to the canonical references so the
  next person doesn't rediscover the false premise.
- The user-visible flow against the current GitHub: the user
  taps Begin → user_code displayed in our app + auto-copied to
  clipboard → bare ``/login/device`` URL opens in browser →
  GitHub's account-confirmation step → 8-field code form (no
  paste support either) → user types each digit → authorize.
  Polling worker picks up the resulting authorization within
  ~5s. Not a great UX, but it's the only path GitHub provides.

### azt_collabd 0.30.6 + azt_collab_client 0.30.6 — worker tracing + fix message overwrite
- Detach fix from 0.30.4 worked: ``gh_primary_btn`` now receives
  touches and ``primary_action: action='begin'`` fires correctly.
- New diagnostics in ``_worker``: trace device_flow_start,
  device_flow_poll completion, save_github_tokens success,
  app_installed probe result, _done firing, and AuthError /
  Exception paths. Intent: pin down why the screen doesn't
  advance to step 2 after the user authorizes — currently we
  can't tell if polling stalled, save failed silently, or _done
  ran but credentials_status didn't reflect.
- Bug fix: error handlers (AuthError / generic Exception) called
  ``self.on_pre_enter()`` (which schedules ``_refresh_state``)
  *then* set ``gh_message.text = 'Failed: ...'``. The deferred
  ``_refresh_state`` ran on the next frame and overwrote the
  message with step-1's "Tap Begin..." default. Now the
  Failed message is set via a second ``Clock.schedule_once``
  so it lands after ``_refresh_state`` and survives. Implication
  for the user: when polling actually times out, they'll
  finally see why instead of silently bouncing to step 1.

### azt_collabd 0.30.5 + azt_collab_client 0.30.5 — build the prefilled device-flow URL ourselves
- Audit doc #1 (the "Manual code-copy step in browser" win) was
  betting on GitHub returning ``verification_uri_complete`` in the
  device-flow response. Per RFC 8628 that field is OPTIONAL and
  GitHub elects to omit it for OAuth Device Flow — confirmed
  by the user landing on the bare code-entry page after Begin.
- Fix: when ``verification_uri_complete`` isn't in the response,
  build the prefilled URL ourselves by appending
  ``?user_code=<user_code>`` to the bare URL. GitHub's
  ``/login/device`` page reads the query parameter and prefills
  the code field, so the user still lands on "Authorize?"
  directly. If a future GitHub change starts returning
  ``verification_uri_complete`` we use it as-is and skip the
  suffix.

### azt_collabd 0.30.4 + azt_collab_client 0.30.4 — detach hidden box children so they can't intercept touches
- 0.30.3's diagnostics confirmed it: when the user taps inside
  ``gh_primary_btn``'s content y-range (Window y=1013-1070
  mapped to content y=305-362, well inside the button's
  pos=275 / top=405 range), the Window touch fires but
  ``gh_primary_btn``'s ``on_touch_down`` probe never does. So a
  sibling earlier in dispatch order is silently consuming the
  touch before the Begin button gets a chance — even though
  ``Widget.on_touch_down`` should have short-circuited via
  ``self.disabled and self.collide_point(...)`` on the hidden
  ``gh_manage_box`` / ``gh_device_flow_box``.
- Suspect: ``BoxLayout._do_layout`` keeps positioning children at
  their explicit heights even when the parent's ``height=0``, so
  Re-auth NavBtn (in the hidden manage box) lives at content
  y=85-205 with disabled=True. The disabled-eats-touch contract
  is supposed to handle this, but in this layout it didn't —
  some children were intercepting touches and others weren't,
  inconsistently. The mechanism's failure mode is opaque enough
  that fighting it with more flags (``disabled``, ``opacity``,
  ``height``) just shifts which configurations break.
- Fix: hide-by-detach. ``_hide_device_flow`` / ``_hide_manage``
  now call ``remove_widget`` on each child of the box; show
  re-adds them in original order from a per-box snapshot. A
  parent with no children cannot dispatch ``on_touch_down`` to
  anything, so there's no way for a hidden manage/device-flow
  child to intercept touches that should reach Begin /
  Install GitHub App / Verify setup. The snapshot stays
  strong-ref'd while detached so widgets don't GC.
- Idempotent: re-detach is a no-op if already detached;
  re-attach is a no-op if the snapshot is empty.

### azt_collabd 0.30.3 + azt_collab_client 0.30.3 — deeper Begin button diagnostics
- 0.30.2 told us the button is at ``pos=[50, 275]
  size=[980, 130] disabled=False opacity=1.0`` — sane — but
  ``state`` never flips on tap, so touch_down isn't reaching the
  button. Sibling NavBtns (Back, Create-account) in the same
  ScrollView work fine, so this isn't a global ScrollView /
  ButtonBehavior thing.
- Added two more probes:
  - ``btn.bind(on_touch_down=...)`` on the primary button so we
    log whether the button receives the dispatched event from
    its parent BoxLayout (with ``inside=collide_point(touch)``).
  - ``Window.bind(on_touch_down=...)`` so we log every raw touch
    Kivy receives, with the touch's reported position. Lets us
    correlate "where the user actually touched" against the
    button's pos and confirm whether the touch even arrives at
    the Window level.
- Expected next-run output on a tap:
  ``WINDOW touch_down: pos=(X, Y) inside_primary_btn=True/False``
  followed (or not) by ``gh_primary_btn on_touch_down: ...
  inside=...``. The combination tells us whether the touch
  arrives at all, whether it's at the right position, and
  whether the parent dispatches it to the button.

### azt_collabd 0.30.2 + azt_collab_client 0.30.2 — Begin button diagnostics
- 0.30.1's ``on_press`` switch did not help the Begin button:
  user reports ``_refresh_state`` still fires but neither
  ``primary_action`` nor any state log appears on tap, while the
  sibling Back / Create-account buttons (also inside the same
  ScrollView) work fine. So it isn't ScrollView vs. on_release —
  it's something specific to ``gh_primary_btn``.
- Added two diagnostics that will fire on the next attempt:
  - ``_render_primary`` logs the button's resolved ``pos`` /
    ``size`` / ``disabled`` / ``opacity`` after the render
    finishes. We need this to confirm the button is at the
    coordinates the user is tapping.
  - One-shot bind on the button's ``state`` property so every
    'normal' → 'down' transition surfaces in logcat. Button's
    state machine flips on touch_down regardless of whether the
    on_press / on_release events fire — so this lets us tell
    apart the two failure modes:
      * State changes but no ``primary_action`` log → event
        dispatch broken (binding lost?).
      * State never changes → touches aren't reaching the button
        (layout / hit-test problem; e.g. a hidden sibling box
        overlaps the touch zone).

### azt_collabd 0.30.1 + azt_collab_client 0.30.1 — switch settings/connect-screen action buttons to on_press
- User-reported: Begin (device-flow start) "still does nothing"
  even after the layout / id-resolution fixes in 0.29.1. The
  ``[github-connect] _refresh_state`` trace fires on screen
  entry, confirming the button is rendered with ``_action='begin'``
  and ``disabled=False``, but no ``primary_action`` log appears
  on tap.
- Diagnosis: classic Kivy ScrollView vs. Button issue. ScrollView
  records every touch_down for scroll-distance evaluation; if the
  user's finger jiggles even ~dp(20) during the press (easy on a
  touchscreen), ScrollView grabs the touch on touch_up and the
  child Button's state machine never fires ``on_release``. The
  user's previous complaint that the GitLab settings button
  "resists pressing in most cases. if I hit it randomly for
  awhile, it does eventually activate" is the same root cause —
  the rare clean tap was the one without enough movement.
- Fix: switch every action button inside a scrolled region from
  ``on_release`` to ``on_press`` so the dispatch fires on
  touch_down, before ScrollView decides whether to claim the
  touch. Affected sites: SettingsScreen Back / Share / Update /
  Publish / GitHub action / GitLab action / Refresh / Debug-503;
  GitHubConnectScreen primary / signup / Copy / Re-authenticate /
  Disconnect / Back; GitLabFormScreen Verify / Back; the dynamic
  language-selector buttons. Trade-off: actions can no longer be
  cancelled by sliding off the button before lifting the finger —
  fine for these recoverable flows (every button either
  navigates, opens the browser, or kicks off an RPC the daemon
  treats idempotently). Popups and modals are not affected;
  they're not inside a ScrollView.

### azt_collabd 0.30.0 + azt_collab_client 0.30.0 — boot Python on ContentProvider lazy-spawn; auto-exit on self-update
- **Server APK couldn't be reached after fresh install unless the
  user opened it manually.** ``AZTCollabProvider.onCreate()`` was a
  one-line ``return true`` — Android's ContentProvider lazy-spawn
  brought up the ``:provider`` host process and instantiated the
  Provider, but did not start any Service in that process, so
  Python never booted, ``install_callbacks()`` never ran, and the
  ``sDispatch`` slot stayed null. Every ``call()`` then fell
  through to the existing ``daemon_not_ready`` 503 fallback. The
  user's repro: install server APK → open recorder → recorder
  can't reach daemon → user opens server APK launcher → daemon
  finally boots → recorder works on the next attempt.
  ``onCreate()`` now calls ``AZTServiceProviderhost.start(ctx,
  "")`` so PythonService is started alongside the Provider on the
  same lazy-spawn. The boot is async (Python service thread
  spawns separately); the very first peer call may still race
  the boot and surface ``daemon_not_ready``, but ``rpc.call``'s
  existing transport-level retry sees the populated callbacks on
  the second attempt — the user no longer has to babysit the
  install.
- **Auto-exit on self-update.**
  ``azt_collabd.android_cp.service`` now snapshots the package's
  ``PackageManager.lastUpdateTime`` at ``install_callbacks()``
  time and re-reads it after every dispatch. If it has advanced,
  we schedule an ``os._exit(0)`` 500 ms later, so the in-flight
  binder reply has time to land and the next peer
  ContentResolver call lazy-spawns the freshly-installed code.
  Belt-and-braces: Android's package installer normally kills
  the upgraded process for us, but custom-ROM battery savers and
  ``adb pm install -r`` can leave the old daemon running with
  stale code while the new APK is on disk — that produced the
  "after updating, peer connects to old daemon until I
  force-stop the server APK" symptom. Adds one PackageManager
  call per dispatch (cheap; cached by Android in the same
  process).
- Lock-step minor bump because the Provider Java change requires
  a full server-APK rebuild — peers that update without the
  matching server-APK rebuild will still hit the daemon-boot
  race on lazy-spawn and have to open the server APK manually.

### azt_collabd 0.29.2 + azt_collab_client 0.29.2 — fix server-APK crash on KV format
- ``register_kv`` was raising ``KeyError: 'uri'`` from
  ``KV_TEMPLATE.format(font_name=..., share_icon=...)`` because a
  comment I added in 0.29.1 quoted ``"Opening {uri}\n..."`` as
  prose inside the KV string. Python's ``str.format`` reads
  ``{uri}`` as a substitution placeholder regardless of whether
  it sits inside a KV comment. The peer's "Could not open
  project picker: unexpected_cancel" + ``KeyError: 'uri'`` in
  ``register_kv`` traceback is the symptom — the server APK
  crashes during start-up, the picker activity returns
  RESULT_CANCELED with data, peer treats it as the picker
  anomaly retry path and gives up.
- Comment now spells the placeholder as "URL" without braces; the
  same risk applies to any ``{name}``-shaped token in KV
  comments, which is documented in the comment for the next
  editor.

### azt_collabd 0.29.1 + azt_collab_client 0.29.1 — fix unresponsive Begin / GitLab buttons after 0.29.0 restructure
- **GitHubConnectScreen "Begin" did nothing.** Two compounding
  causes, both shipped fixes:
  1. ``primary_action`` was re-fetching credentials_status to pick
     a step every tap. If the freshly-rendered button label said
     "Begin" but the daemon's status said the user was already at
     step 4 (stale from a prior session), the dispatcher fell
     through every elif and silently no-op'd. ``_render_primary``
     now stamps an ``_action`` attribute (``begin`` / ``install`` /
     ``verify``) on the button each time it renders; the
     dispatcher uses that, with a label-based fallback, and
     finally defaults to ``begin()`` so a button labelled "Begin"
     always begins.
  2. ``on_pre_enter`` accessed ``self.ids.gh_user_code`` and
     similar directly. On Kivy ≥ 2.3 the rule's nested children
     can lag a frame after the screen is added, so the early
     accesses raised an ``ObservableDict`` AttributeError mid-
     setup, leaving the screen in KV-default state with no
     ``_action`` tagged. ``on_pre_enter`` now defers via
     ``Clock.schedule_once`` (matches what ``SettingsScreen`` was
     already doing); every helper uses ``self.ids.get(...)`` so a
     genuinely-missing widget no longer takes the whole pass
     down.
- **GitLab/GitHub action buttons "resisted pressing"** in the
  settings screen: tapping the sibling RecBtn fired
  ``contributor_input.on_focus``, which called
  ``save_contributor()`` synchronously — the RPC blocked the UI
  thread for a few hundred ms during which Kivy still received
  the touch but couldn't dispatch the on_release until the RPC
  returned. ``save_contributor`` now runs the ``set_contributor``
  call on a worker thread; the "Saved." flash flips through
  ``Clock.schedule_once`` on the UI thread.
- **Layout-shift fix.** The new ``gh_preflight`` and ``gh_message``
  ``BodyLabel`` widgets used the
  ``height: self.texture_size[1] + dp(8)`` growing pattern; on first
  paint the texture is computed against width=0, so the label
  starts ~30 dp tall and grows as the layout settles, pushing
  every button below it down. Replaced with explicit
  ``height: dp(80)`` so the BoxLayout's ``minimum_height`` is
  stable from frame 0 — no more "tap where the button used to
  be" misses.
- **Tracing.** Added ``[github-connect]`` print lines on
  ``primary_action`` / ``begin`` / ``_refresh_state`` so a flaky
  field repro can be diagnosed from logcat without rebuilding
  with extra logging. Cheap.

### azt_collabd 0.29.0 + azt_collab_client 0.29.0 — GitHub connect-flow UX restructure (audit doc #1–#7)
- **#1 ``verification_uri_complete``**:
  ``GitHubConnectScreen._worker`` now prefers GitHub's
  ``verification_uri_complete`` (URL with the user_code prefilled)
  over the bare ``verification_uri``. Users land on GitHub's
  Authorize? page directly instead of the code-entry detour.
  Falls back to ``verification_uri`` then the bare URL if
  GitHub's response shape ever changes.
- **#2 + #3 step-indicator + pre-flight + no auto-fire**: the
  GitHub connect screen is now organised as three explicit
  stages — *1. Authorize this device* → *2. Install GitHub App*
  → *3. Verify setup* — rendered as a colour/bold-coded
  indicator. A single state-aware "primary" button presents
  only the next required action; ``on_pre_enter`` derives the
  current step from server flags
  (``connected`` / ``app_installed`` / ``confirmed``), so a
  partial setup that picks back up later resumes from where it
  stopped (lost network, browser bail-out, app close all
  recoverable). Pre-flight body text explains what GitHub is and
  that a free account is required; the device flow is never
  auto-fired — the user always taps *Begin* / *Install GitHub
  App* / *Verify setup* explicitly. ``_render_message`` /
  ``_render_steps`` / ``_render_primary`` / ``_render_manage``
  handle the four screen shapes.
- **#4 "Verify setup" rename**: both GitHub and GitLab
  "Test connection" buttons are relabelled "Verify setup" — the
  old label sounded like an optional diagnostic but is actually
  the gate that flips ``confirmed=True``. Status messages
  referencing "Test connection" are updated to match.
- **#5 create-account link**: a "Create a GitHub account
  (free)" NavBtn just below the pre-flight panel opens
  ``https://github.com/signup`` in the user's browser. Pre-flight
  body text also names the account-required precondition so the
  user isn't surprised by GitHub's sign-in/up page.
- **#6 simplified host buttons**: ``SettingsScreen`` now shows a
  single state-aware GitHub button (label flips Connect ↔
  Disconnect from ``credentials_status``) instead of two
  parallel buttons, and a single ``GitLab`` button that opens
  the GitLab settings form. Connection details for both hosts
  remain in the Status block below.
- **#7 declined**: no "Are you sure?" disconnect popup —
  per-maintainer preference; with #1 landed an accidental
  Disconnect costs one tap to redo, and the GitHub App on the
  GitHub account is untouched, so re-Authorize is the only step
  needed. Audit doc records the rationale.
- French .po updated with the new strings; old "Test
  connection" / "Tap Test connection" entries left in place
  (translation-coverage drift detector only flags missing
  msgids, not orphans).
- Lock-step minor bump because the connect-flow restructure
  changes a daemon-side UI subprocess that peers spawn through
  ``open_server_ui()`` — version-string display in the settings
  footer flags the cut.

### azt_collabd 0.28.27 + azt_collab_client 0.28.27 — 60s warmup budget + "Try again" affordance
- ``_DAEMON_WARMUP_RETRIES`` raised again, 15 → 30 (30s → 60s).
  User-reported: 30s wasn't enough on their device — "next boot
  fails, the following one succeeds" — confirming Java-side cold
  spawn time can exceed 30s after ``pm clear`` or fresh install.
- New ``on_retry`` parameter on
  ``install_server_apk_popup``; when supplied, adds a "Try again"
  button to the popup. Bootstrap's
  ``_prompt_server_unresponsive`` now passes
  ``on_retry=_post_install_continuation`` so users on truly slow
  hardware can keep waiting past the 60s budget without having
  to download fresh (Install) or close the app (Quit). Tap →
  popup dismisses → 2s warm-up wait → fresh compat probe.
- Side effect: layouts the popup with up to 4 buttons in the
  action row (Quit | Try again | Open install page | Install).
  Each is text-wrap-bound so labels fit even on narrow screens.

### azt_collabd 0.28.26 + azt_collab_client 0.28.26 — SHA-256 reuse check, drop the time window
- Replaced 0.28.25's mtime-window heuristic with definitive
  SHA-256 verification. After a successful download, ``update.py``
  writes the file's SHA-256 to a sidecar
  ``<asset>.sha256``. ``_has_fresh_download(path)`` now reuses
  the staged file iff (a) the file exists, (b) the sidecar
  exists, (c) recomputing the file's SHA matches the sidecar.
- Eliminates the "10 minutes might not be enough for everyone"
  concern — reuse works regardless of how long the user spent in
  the "Install unknown apps" Settings detour, and regardless of
  device speed.
- Cost: SHA-256 of a typical APK takes ~1–3 seconds on phone
  hardware. Negligible compared to the 10–30 seconds of
  re-download it replaces, especially on slow connections.
- Side benefit: catches APK corruption between download and
  install. If the file gets damaged somehow (rare, but possible
  on flaky storage), the sidecar mismatch forces a re-download
  rather than dispatching a corrupted install Intent.
- ``_save_download_sha(path)`` is non-fatal on failure: a
  missing sidecar just means the next reuse check returns False
  and we redownload, same as before this change.

### azt_collabd 0.28.25 + azt_collab_client 0.28.25 — reuse a recent download instead of re-fetching
- User reported the install flow was downloading the APK twice
  when Android required "Install unknown apps" permission: first
  download → permission detour → user grants → re-tap Install →
  re-download (10–30s wasted).
- New ``_has_fresh_download(path)`` helper checks whether the
  staged file at ``$AZT_HOME/updates/<asset>`` was last modified
  within ``_REUSE_DOWNLOAD_AGE_S`` (10 minutes). Both
  ``install_apk_from_url`` and ``check_for_update``'s download
  paths now skip the download when the file is fresh enough,
  surfacing ``Using already-downloaded file…`` status in place
  of the percentage progress.
- 10-minute window is conservative: long enough to cover the
  typical "user popped to Settings and came back" duration even
  on slow devices; short enough that a stale APK from a
  previous session (yesterday's launch, etc.) won't be
  installed when there's a newer release available.
- ``_download`` writes to ``<path>.part`` and only renames on
  success, so a present ``<path>`` is always a complete
  download — the freshness check doesn't need to validate
  partial-file recovery.

### azt_collabd 0.28.24 + azt_collab_client 0.28.24 — extend daemon-warm-up budget for cold starts
- ``_DAEMON_WARMUP_RETRIES`` raised from 5 to 15 (10s → 30s
  budget). The 503 ``daemon_not_ready`` response that fires
  during cold starts comes from ``AZTCollabProvider.java``'s
  ``sDispatch == null`` check, NOT from my Python sentinel
  hook — it's the genuine "Python interpreter not yet loaded"
  state. After ``pm clear`` or a fresh install the cold start
  can run 15–25 seconds because the dex cache needs rebuilding,
  and the previous 10s budget was tripping users into the
  "AZT Collaboration not responding" popup unnecessarily.
- Warm-cache normal launches still exit the retry loop on the
  first compat-probe success (1–3s typical), so the longer
  budget isn't user-visible in steady state. Cold-start users
  see up to 30s of "Connecting to AZT Collaboration service…"
  popup before falling through.
- Diagnostic clarification noted in the comment block: the 503
  has two possible sources (the test sentinel + the Java
  provider's startup gate); when troubleshooting, check that
  the daemon process is actually alive
  (``adb shell ps -A | grep aztcollab``) — if not, it's the
  Java side and the only fix is waiting longer or warming up
  the dex cache.

### azt_collabd 0.28.23 + azt_collab_client 0.28.23 — visible "Connecting…" popup during retries
- 0.28.19 added the daemon-warm-up retry loop, but the
  ``Connecting to AZT Collaboration service…`` status only flowed
  to ``ctx.on_status`` (host's status sink, often invisible).
  Result: 10s of empty-state peer UI with no feedback while the
  retries ran.
- New modal ``_show_connecting_popup`` opens on the first retry
  with a centred "Connecting to AZT Collaboration service…"
  message. ``auto_dismiss=False`` so the user can't tap past it.
  Dismissed on every terminal branch — ``compat ok``,
  ``server_too_old``, ``client_too_old``, retries-exhausted, or
  raised exception — before the next branch's UI fires (so the
  unresponsive popup doesn't stack on top of the connecting one).
- Mutable ``connecting_popup`` slot added to ``_Ctx`` so the
  show / dismiss helpers can find each other across the worker-
  thread boundary without a module-level dict. Idempotent: if a
  popup is already up, ``_show_connecting_popup`` is a no-op.

### azt_collabd 0.28.22 + azt_collab_client 0.28.22 — toggle the debug-503 sentinel from settings UI
- 0.28.21's sentinel could only be created via
  ``adb shell run-as``, which fails on release-signed APKs
  (``run-as`` requires the package to be debuggable).
- New "Debug (testing)" section in the daemon settings UI
  (``SettingsScreen``) with a toggle button that creates / removes
  ``$AZT_HOME/_debug_force_503``. State indicator below shows
  whether ``/v1/health`` is currently forced to 503 or responding
  normally. Always visible — production users tapping it just see
  "service unavailable" until they tap again.
- Test workflow: tap the server APK launcher icon (it's an
  installed app with its own icon) → settings UI opens directly
  (bypasses bootstrap; settings calls don't go through
  ``/v1/health``) → toggle Debug → close → launch peer →
  bootstrap retries 5×2s → unresponsive popup fires. Re-open
  server APK to toggle off when done.

### azt_collabd 0.28.21 + azt_collab_client 0.28.21 — debug hook to test the "not responding" popup
- Daemon's ``/v1/health`` (the compat handshake endpoint) now
  returns ``503 daemon_not_ready`` when
  ``$AZT_HOME/_debug_force_503`` exists. Toggle without restarting
  the daemon — the file presence is checked per-request. Create
  via ``adb shell run-as org.atoznback.aztcollab touch
  files/azt/_debug_force_503``; remove with the equivalent ``rm``.
- Lets the bootstrap workflow's daemon-warm-up retry path
  (``_DAEMON_WARMUP_RETRIES = 5`` × 2s = 10s) exhaust deterministically,
  exercising the "AZT Collaboration not responding" popup added
  in 0.28.20. Without this, manually triggering the unresponsive
  state required either killing the daemon mid-spawn (race-prone)
  or breaking the install (signature mismatch — heavy).

### azt_collabd 0.28.20 + azt_collab_client 0.28.20 — modal recovery popup when daemon stays unresponsive
- 0.28.19's retry-with-backoff fixed the common case (daemon
  warming up settles within 1–3s) but still fell through to
  ``_check_self`` → ``on_done`` after the retry budget exhausted.
  Result: the same bouncing-out behaviour the user originally
  reported, just delayed by 10s. ``on_done`` fired, peer's
  startup tried daemon RPCs, hit ``ServerUnavailable``, picker
  fired and failed, picker emitted CANCEL, peer closed via the
  picker-cancel rule.
- **Real fix**: when the warm-up retries exhaust without daemon
  response, bootstrap now shows a modal popup
  (``_prompt_server_unresponsive``) — same canonical
  ``install_server_apk_popup`` as the missing-server case, with
  a body reading "AZT Collaboration is installed but did not
  respond. It may still be starting up; wait a moment, then tap
  Install to reinstall it, or Quit to close this app and try
  again later." Title: "AZT Collaboration not responding".
- The popup gives the user three explicit recovery options
  (Reinstall, Open install page, Quit) instead of silently
  bouncing them out of the app. Modal blocking means the peer
  stays in the foreground until the user makes a choice, and
  ``on_done`` is not fired (so the peer doesn't run its
  post-bootstrap startup against a daemon that isn't there).
- Reinstall path: standard download+install via
  ``install_apk_from_url``; on completion the post-install
  continuation re-runs ``_check_server`` and on_done fires from
  the healthy path. Quit path: closes the peer cleanly so the
  next launch starts fresh.

### azt_collabd 0.28.19 + azt_collab_client 0.28.19 — retry on daemon-warm-up race at startup
- **Symptom user reported:** opening peer A, log shows
  ``[bootstrap] AZT Collaboration installed but unreachable.
  Continuing offline.`` followed by ``[recent] last_project:
  ServerUnavailable: provider HTTP 503: daemon_not_ready``, then
  Android brings the previously-foregrounded peer B to the
  front. Sequence: bootstrap fires ``on_done`` thinking everything
  is fine because the server APK is installed; peer's normal
  startup tries ``last_project()`` while the daemon is still
  warming up; the daemon's ContentProvider returns 503; peer's
  picker logic kicks in, fails with the same 503; picker emits
  ``RESULT_CANCELED``; the picker-cancel rule from
  ``CLIENT_INTEGRATION.md`` § 5 closes the peer; Android brings
  the most-recent task forward.
- **Fix:** ``_check_server`` now retries the compat probe with
  backoff (``_DAEMON_WARMUP_RETRIES=5``,
  ``_DAEMON_WARMUP_INTERVAL_S=2.0`` → 10s budget total) when the
  server APK is installed but unreachable. Status flips to
  ``Connecting to AZT Collaboration service…`` during retries.
  Android lazy-spawns the server APK's Python interpreter on the
  first ContentResolver call; this typically settles within
  1–3 seconds, well under the budget.
- If the retries exhaust, we still fall through to
  ``Continuing offline.`` and ``_check_self`` (which fires
  ``on_done``). At that point the daemon is genuinely unreachable
  (crashed, hardware glitch, signature mismatch denied us access)
  and bootstrap can't fix it; the peer's host code is responsible
  for handling ``ServerUnavailable`` on its post-on_done RPCs.
  Recommend defensive try/except around the first 1–2 RPCs the
  host makes after ``on_done`` — already in
  ``CLIENT_INTEGRATION.md`` § 4 ("log the failure and continue,
  not pop their own dialog").

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
