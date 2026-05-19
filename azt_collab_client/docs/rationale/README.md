# `azt_collab_client` — rationale files

Each file here is the *why* for one subsystem. Rules and
invariants are NOT in these files; they live in
`../../CLAUDE.md` (architectural rules) and
`../../CLIENT_INTEGRATION.md` (peer conformity contract).

Read the relevant rationale file when you need to understand
*why* a rule exists, or when you're working inside that
subsystem and want the historical context the rule alone
doesn't convey.

| File | Subsystem |
|---|---|
| `sync.md` | commit/push split, stuck-commit retry, auto-sync silence routing |
| `lift_access.md` | LIFT file + audio + image cross-package access; `atomic_open_write` |
| `cawl.md` | CAWL image cache, suite-scoped daemon ownership, per-project image_repo |
| `i18n.md` | gettext catalog, auto-init, `add_fallback` chain, live retranslation |
| `identity.md` | contributor + device_name (commit author identity) |
| `ui.md` | UI submodule, shared assets, share helpers, daemon-log capture, self-update, bootstrap |
| `lowpower.md` | automatic device tiering, build-time vs runtime work, presplash baseline |
| `project_switch.md` | `on_resume` reconciliation; why no daemon push |

If you find yourself writing a "do X / don't do Y" rule in
one of these files, stop. Move the rule to `CLAUDE.md` (or
`CLIENT_INTEGRATION.md` if it's a peer contract) and keep
only the justification here.
