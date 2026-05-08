# Plan: Web Flow + Device Flow Fallback

Goal: eliminate the 8-character device-flow papercut for users who can
reach our redirect URI, while keeping device flow as the universal
fallback for environments where the redirect can't get home (no
custom-scheme handler, locked-down browser, kiosk Android, etc.).

**Status:** drafted 2026-05-07 by Claude after user-driven research.
Not yet approved for implementation.

---

## Phase 0 — Research outcome (done)

GitHub Apps' OAuth Web Flow added PKCE on
[2025-07-14](https://github.blog/changelog/2025-07-14-pkce-support-for-oauth-and-github-app-authentication/),
but `client_secret` is **still required** on `POST /login/oauth/access_token`
([GitHub docs](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)).
PKCE on GitHub Apps is defense-in-depth, not a substitute for the secret
([community/discussions #15752](https://github.com/orgs/community/discussions/15752)).

Practical consequences:

- A pure-PKCE mobile flow (no embedded secret) is **not legal** for
  GitHub Apps as of 2026-05.
- Web Flow on mobile requires either (a) embedding `client_secret` in
  the APK, or (b) running a backend the APK can talk to that holds
  the secret.

(b) is out of scope — it defeats the standalone-suite ethos and adds
a service to operate. (a) is unsafe in the textbook sense (anyone who
unzips the APK extracts the secret), but it's how a lot of mobile
GitHub-App integrations actually ship today, and the realistic threat
is App-impersonation phishing, not data exfiltration (the secret only
lets you start an OAuth flow as `azt-collaboration`; it doesn't grant
access to anyone's repos until the user authorizes the impersonator).

**Implication for the plan:** web flow's incremental UX win is real
(no 8-field papercut), but the cost is shipping a `client_secret` that
is no longer secret. We treat it accordingly: secret rotation policy
documented, App reviewable in the registry, kept inside the server
APK only (peers never see it).

---

## Phase 1 — PKCE smoke test (validate the research)

Confirm the research finding before any code change. The test is small
enough to live in `tests/probe_pkce.py` and can be run from a desktop
venv against the live GitHub API.

### Test inputs

- App: `azt-collaboration` (`Iv23li66Fo9MBReatv6i`).
- Redirect URI: `http://127.0.0.1:9876/cb` (registered on the App).
- A throwaway browser session with a test GitHub account.

### Test cases

1. **Authorize URL accepts PKCE params.**
   - Build `code_verifier` (43-128 char URL-safe random) and
     `code_challenge = base64url(sha256(code_verifier))`.
   - Open browser to
     `https://github.com/login/oauth/authorize?client_id=...&redirect_uri=...&state=...&code_challenge=<challenge>&code_challenge_method=S256`.
   - Expected: GitHub renders the standard authorize page; no error.

2. **Token exchange WITHOUT `client_secret` but WITH `code_verifier` is rejected.**
   - Capture `code` from the redirect.
   - POST to `https://github.com/login/oauth/access_token` with
     `client_id`, `code`, `redirect_uri`, `code_verifier` — and **no**
     `client_secret`.
   - Expected: 200 with body `{"error": "incorrect_client_credentials"}`
     or similar non-token response. **This validates the research's
     "client_secret still required" claim.**

3. **Token exchange WITH `client_secret` AND `code_verifier` succeeds.**
   - Same POST, this time including `client_secret`.
   - Expected: 200 with `access_token` field.

4. **Token exchange WITH `client_secret` but WITHOUT `code_verifier` succeeds.**
   - Same POST, no `code_verifier`.
   - Expected: 200 with `access_token`. (PKCE is optional even when
     accepted — confirms it's not made mandatory by sending the
     challenge in step 1.)

### Pass/fail criteria

- If case 2 returns a token: research is wrong, mobile-safe PKCE-only
  flow IS possible. Plan pivots to PKCE-only.
- If case 2 returns an error: research confirmed. Plan proceeds with
  embedded-secret web flow + device-flow fallback.

### Output

The script prints each case's HTTP status + body and exits 0 only if
the observed behavior matches the research. Run it once before
committing to Phase 2.

---

## Phase 2 — Web Flow architecture

### Identity

Reuse the existing `azt-collaboration` GitHub App. Add to its callback
URL list (in the GitHub App settings, `github.com/settings/apps/azt-collaboration`):

- `aztcollab://github-callback` — Android.
- `http://127.0.0.1:9876/cb` — desktop loopback.

(Multiple callback URLs are allowed; GitHub will accept any of them
when the request specifies one as `redirect_uri`.)

`client_secret` is fetched from the App settings page once and pasted
into a build-time constant. **Server APK only** — peers never see it.

### Where the flow runs

The auth flow MUST run in the process that owns the redirect handler:

- **Server APK (`org.atoznback.aztcollab`)** declares the
  `<intent-filter>` for `aztcollab://github-callback`. Its main
  Activity (the standalone settings UI) handles `onNewIntent` and
  hands the `code` to Python.
- **Peers** (recorder, viewer) do not declare the intent-filter and
  do not run web flow. The existing `GitHubConnectScreen` is gated to
  the server APK; in peer apps, the "Connect to GitHub" button calls
  `open_server_ui()` and the user does the auth in the server APK.
  After the server APK saves the token, the peer's `credentials_status`
  reflects it on the next refresh.

This gating is a side effect of the redirect-URI mechanism: only one
APK can claim a custom scheme without showing a chooser, and we don't
want a chooser on every connect.

### Mobile flow (server APK on Android)

1. User taps Begin on `GitHubConnectScreen`.
2. Server APK generates `state` (random 32 bytes, base64url) and
   `code_verifier` + `code_challenge` (PKCE — defense-in-depth, not
   replacement for secret). Store `state` and `code_verifier` in
   memory keyed by `state`.
3. Server APK opens the system browser to
   `https://github.com/login/oauth/authorize?client_id=...&redirect_uri=aztcollab://github-callback&state=...&code_challenge=...&code_challenge_method=S256`.
4. User authorizes on GitHub.
5. GitHub redirects to `aztcollab://github-callback?code=...&state=...`.
6. Android dispatches the intent to our manifest filter; our Activity
   receives `onNewIntent`, extracts `code` + `state`, and calls a
   pyjnius bridge (`callback_received(state, code)`).
7. Python validates `state` matches what step 2 stored, retrieves
   the matching `code_verifier`, and POSTs to
   `/login/oauth/access_token` with `client_id`, `client_secret`,
   `code`, `redirect_uri`, `code_verifier`.
8. On success, save tokens via existing
   `save_github_tokens(...)`. Screen advances to step 2 (Install
   GitHub App).

### Desktop flow

Same shape but with a loopback HTTP server instead of an
intent-filter:

1. Bind a small `BaseHTTPServer` on `127.0.0.1:9876` listening for `/cb`.
2. Open browser to authorize URL with
   `redirect_uri=http://127.0.0.1:9876/cb`.
3. Browser hits our loopback server; we extract `code` + `state` from
   the query string, send a 200 "you can close this tab" response,
   and shut the server down.
4. Same token exchange as step 7 above.

Port 9876 is fixed because GitHub Apps don't allow port wildcards in
callback URLs. If the port is in use, fall back to device flow with a
status message ("temporary port unavailable; falling back to device
flow").

### Device-flow fallback

Web flow can fail for several reasons:

- Custom scheme not handled (server APK uninstalled mid-session).
- Browser blocks the custom-scheme intent (some Android security UIs).
- Loopback port in use (desktop).
- Network glitch during token exchange.
- User cancels the GitHub authorize page.

In each case, surface a small affordance ("Couldn't return to the app
— use the code-entry method?") that drops back to device flow. Device
flow code stays in `azt_collabd/auth.py`; the new web-flow code lives
beside it. `GitHubConnectScreen` exposes both as discoverable paths.

Default path on first attempt: web flow. Manual override: a "Use
device flow instead" link on the connect screen for users in
controlled environments where redirects are blocked.

---

## Phase 3 — Implementation steps

### 3.1 Manifest + Java glue (server APK only)

- `android/src/main/java/.../AZTCallbackActivity.java`: a tiny
  Activity declared with `<intent-filter>` for
  `aztcollab://github-callback`, `singleTask`, that overrides
  `onNewIntent`, extracts `code` + `state`, calls into a pyjnius
  callback (similar to `AZTCollabProvider.registerCallbacks`), and
  finishes itself so the user lands back in the settings UI.
- `p4a_hook.py:_inject_aztcollab_callback`: post-render manifest patch
  to add the `<activity>` declaration and `<intent-filter>` (gated on
  `dist_name == 'aztcollab'`, same pattern as
  `_inject_aztcollab_provider`).

### 3.2 Python web-flow module

- New `azt_collabd/auth_web.py`:
  - `start_web_flow()` — generates `state` + `code_verifier`, stores
    them in `_pending` dict (with TTL), returns the authorize URL.
  - `complete_web_flow(state, code)` — validates state, fetches
    `code_verifier`, POSTs to GitHub, returns token dict.
  - Error types compatible with existing `AuthError(Status(...))`
    surface so `GitHubConnectScreen` doesn't need to learn new codes.
- New `azt_collab_client` wrappers `start_github_web_flow()` /
  `complete_github_web_flow()` calling the daemon RPCs.
- New daemon endpoints `/v1/auth/web/start` and `/v1/auth/web/complete`.

### 3.3 Activity ↔ Python bridge

- `azt_collabd/android_cp/auth_callback.py` (new): registers a
  pyjnius callback that the Java `AZTCallbackActivity` invokes when
  the redirect arrives. Forwards `(state, code)` to the in-memory
  pending-flow store, then notifies the Kivy UI thread via a
  `Clock.schedule_once` so `GitHubConnectScreen` advances.

### 3.4 Desktop loopback

- `azt_collabd/auth_web.py:run_loopback_server()` — short-lived
  `http.server` thread bound to `127.0.0.1:9876`, returns
  `(state, code)` once the redirect lands. Times out after
  `expires_in` (15 min default).

### 3.5 GitHubConnectScreen integration

- `begin()` calls `start_github_web_flow()` instead of
  `device_flow_start()`. Stores returned `state` for matching.
- New screen state: "Waiting for browser…" (between Begin and
  callback).
- On callback, `_done` runs as today — the rest of the flow (Install
  App → Verify) is unchanged.
- A "Use device flow" link toggles to the existing
  `_worker`-driven path for users who can't get the redirect to
  work.

### 3.6 Peer gating

- Recorder / viewer's settings entry point: tapping "Connect to
  GitHub" calls `open_server_ui()` (Android) or just opens the
  standalone settings UI (desktop). The embedded
  `GitHubConnectScreen` in `picker_app` is removed for the GitHub
  case (kept for GitLab, which has no redirect concern).
- Peer `credentials_status` polling on screen entry catches the
  "user finished auth in server APK" case so the peer's UI updates.

### 3.7 Migration

Existing device-flow tokens are valid against `client_id` regardless
of how they were issued — the persistence shape is unchanged. Users
mid-setup at upgrade time keep their existing flow if not yet
confirmed; new connections after upgrade go through web flow by
default.

---

## Phase 4 — Testing

- Phase 1 PKCE probe (above) — run once on a desktop venv with a
  test account.
- Manual matrix on the server APK (no automated end-to-end is
  possible because Android intent dispatch + a real browser are
  in the loop):
  1. Fresh install, web flow happy path.
  2. Web flow with user cancelling on GitHub authorize page.
  3. Web flow with user closing browser before redirect.
  4. Device-flow fallback link tapped.
  5. Loopback port collision on desktop → graceful fallback.
  6. State mismatch (synthetic — call `complete_web_flow` with wrong
     state, expect typed error).
- Translation-coverage drift detector still passes after new
  user-visible strings land in the .po.

---

## Phase 5 — Open questions / decision points

Surface these to the maintainer before merging:

1. **Embed `client_secret`?** Yes/no. If no, kill this plan and stick
   with device flow + UX polish only.
2. **Secret rotation policy.** If embedded, a leaked secret means
   rotating in the GitHub App settings + new APK release. Document
   the playbook.
3. **Forks / re-skins.** Other groups taking this code base would
   need their own GitHub App + their own secret. The build needs a
   clean way to take the secret as a build arg
   (`buildozer.spec` env var or a separate file imported by
   `azt_collabd.config`).
4. **GitLab story.** Same web-flow upgrade for GitLab? GitLab
   doesn't have the 8-field papercut (its PAT entry is a single
   field), so the priority is lower. Out of scope for v1.
5. **Device-flow removal date?** Keep it indefinitely as the
   "manual" fallback, or sunset it once web flow is stable for
   12 months?

---

## Phase 6 — Rollout sequence

(If approved.)

1. Add web flow code paths beside device flow; ship as opt-in via a
   config flag (`auth.method=web|device`, default `device`).
2. Run Phase 4 manual matrix.
3. Flip the default to `web` in a follow-up release; announce in the
   changelog with the device-flow opt-out documented.
4. Monitor for failure-mode reports for one release cycle. If clean,
   keep `device` only as the explicit fallback link on the connect
   screen.
