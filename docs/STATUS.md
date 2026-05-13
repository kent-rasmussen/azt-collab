# Suite status & roadmap

One-page index of what's shipped vs. what's open across the docs
in this directory. Re-read alongside `CHANGELOG.md` (the
source of truth for what shipped when) before each release pass.

## Recently shipped (2026-05)

- **Phase A daemon-warmup** (`azt_collab_client 0.32.x`) —
  adaptive backoff, kind-tagged `ServerUnavailable`, diagnostic
  surface in connecting + unresponsive popups, fail-fast on
  `null_bundle`.
- **Phase B2 freezer + background-start fix**
  (`azt_collab_client 0.33.x` + `azt_collabd 0.33.0`) — peer
  holds `bindService` for OOM priority; server APK's
  `Service.onCreate` self-bootstraps Python so `bindService`
  alone is sufficient (no Android-12+ background-start
  refusals). Measured outcome on R500-class slow tablet: peer
  wait dropped from 60 s timeout to ~50 ms steady-state.
- **Boot-trace harness** at `tests/integration/measure_boot.sh`
  + `parse_boot_traces.py` for cold-start measurement.
  Reference data captured 2026-05-09.
- **GitHub-connect UX rewrite** (items #1–#7 from audit) —
  3-step indicator, state-aware primary button, pre-flight
  explanation, "Verify setup" relabel, create-account link,
  Connect/Settings button gating. See
  `github_connect_ux_audit.md` for the audit trail.
- **Grant-collaborator UI** — peer can invite GitHub
  collaborators per-project via reusable popup
  (`grant_collaborator_popup`). See
  `azt_collab_client/CLIENT_INTEGRATION.md` § 10.
- **Smooth-UI-across-reloads principle** documented for peers
  — sync refreshes content under the user's anchor without
  navigating; client-side filters that would hide the current
  view get suspended. See
  `azt_collab_client/CLIENT_INTEGRATION.md` § 11.

## Open work, prioritised

Highest-leverage first. None are blocking; pick up when the
relevant trigger surfaces (user report, related work in the
area, release-prep pass).

### High

1. **Google developer-verification enrollment for SIL** — the
   load-bearing distribution piece per
   `research_notes_2026-05.md` § 1 + saved memory
   `project_android_2026_sideload_lockdown.md`. Without it, new
   Android 16 devices in the field can't sideload the suite at
   all. SIL-side process; tracked outside this codebase.
2. **GitHub-connect UX item #9 (device-flow timeout
   countdown + Start over)** — visible papercut in the "user
   sets phone down" case. See `github_connect_ux_audit.md`.

### Medium

3. **GitHub-connect UX items #10–#12** — plain-language
   status messages, more prominent "Setup complete" moment,
   pre-explain OAuth scope grant. See
   `github_connect_ux_audit.md`.
4. **Web Flow + Device Flow fallback** — drafted, on hold
   pending decision on embedding `client_secret` (PKCE-only
   isn't legal for GitHub Apps as of 2026-05). See
   `web_flow_migration_plan.md`. Eliminates the 8-character
   device-flow papercut; tradeoff is a non-secret secret in
   the APK.
5. **Recorder-side reload-on-PULLED implementation** — the
   peer-side half of the smooth-UI-across-reloads principle.
   Daemon already delivers the fresh bytes after sync (fixed
   in `azt_collabd 0.31.2`); the recorder still needs to
   reload its in-memory LIFT model + re-render the user's
   current entry on `S.PULLED`. Not in this repo (lives in
   `azt_recorder/`); flagged here so it doesn't get lost.

### Low / design call

6. **GitHub-connect UX item #8** — Re-authenticate /
   Disconnect prominence. Per maintainer preference (#7) no
   confirmation popups; the move is reducing visual weight
   without changing behaviour.
7. **GitHub-connect UX item #13** — "Skip for now / use
   without backup" path. Design call; defer until raised by a
   real user.
8. **Phase B1 daemon-state field in 503** — would surface
   phase-grained boot progress in the connecting popup. Phase
   B2 dropped peer wait to ~50 ms so this delivers
   diminishing returns; ship only if a future device class
   makes the wait visible again. See `daemon_boot_plan.md`.
9. **Loopback transport `kind`** — desktop-side
   `ServerUnavailable.kind` is unspecified (Android side has
   the diagnostic categories). Address only if a desktop user
   hits a long-wait symptom that benefits.

### Not worth shipping (kept for context)

- **Phase C daemon-side lazy imports** — measured
  `import azt_collabd` cost is ~120 ms on the slow tablet,
  invisible behind the bind+overlap. Don't ship unless a
  regression makes the import the long pole again. See
  `daemon_boot_plan.md`.

## Reference docs (not work items)

- `research_notes_2026-05.md` — platform state of the art.
  Re-do this pass before each major release.
- `test_plan.md` — canonical failure-mode list for the
  install/update/bootstrap path. Manual matrix in § 8.
- `azt_collab_client/CLIENT_INTEGRATION.md` — peer-integration
  contract. Single source of truth for every peer.
- `CHANGELOG.md` (repo root) — what shipped when.

## Cleanup history

- 2026-05-09: `daemon_boot_plan.md` pruned to status-first form.
  `github_connect_ux_audit.md` implementation-order list
  refreshed (1–7 done). `p4a_hook_picker_intent.md` reduced to
  a redirect stub (work shipped in v0.28.x). This file
  created.
