# Suite status & doc index

One-page index of what's **shipped** across the docs in this
directory, plus the reference-doc index. **Open work is tracked
in the agenda, not here.** Re-read alongside `CHANGELOG.md` (the
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
- 2026-06-29: open-work section removed (now tracked in the
  agenda); file moved to `agenda/` and reframed as a
  status + doc-index page.
