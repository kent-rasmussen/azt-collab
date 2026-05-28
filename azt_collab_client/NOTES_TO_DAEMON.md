# Notes to the daemon

**Live queue only.** Outstanding items peers have noticed and want
the `azt_collabd` / server-APK side to fix. Filed here (inside
`azt_collab_client/`) rather than the per-peer CHANGELOG so:

- the symlink propagates them into every sister app's tree
- the daemon team sees them in one canonical place
- the note moves with the package if the canonical home ever
  changes

**When you act on an item, delete it from this file** — the
CHANGELOG is the historical record. This file holds only the
queue.

**Standing rules / architectural invariants do NOT belong here.**
Promote them to `CLAUDE.md` (daemon-side or client-side
architecture / rationale) or `CLIENT_INTEGRATION.md` (peer-facing
conformity contract) instead. A standing item dressed as a queue
item silently turns this file into a reference shelf, which
defeats the "live queue" property. Previous standing items moved
to:

- "Daemon is the sole authoritative source" → `CLAUDE.md`
  hard rule #8 + "Daemon-owned state" section.
- "Project-bound surfaces in daemon UI (Phase 3)" →
  `CLIENT_INTEGRATION.md` § 12b "Project-bound actions live
  in the daemon settings UI."
- "Suite-wide package-upgrade handling" →
  `CLIENT_INTEGRATION.md` § 19 "Package-replacement handling"
  (acted on 2026-05-15 — daemon 0.41.28 / client 0.41.26
  ship `SuiteSelfReplaceReceiver` + the peer-side backstop
  helper).
- "`request_sync` skips the commit step while offline" →
  rebuilt as the commit/push split (acted on 2026-05-15 —
  daemon 0.43.0 / client 0.43.0). Debounced
  `commit_project` runs commit-only and never blocks on
  network; push is driven by the scheduler's drain loop
  (online + `sync.post_online_grace_s` +
  `sync.work_offline`). See `CLIENT_INTEGRATION.md` § 17 for
  the new routing.
- "`digest_changed` prompts after `adb install -r` of a fresh
  release" → at-version-parity guard (acted on 2026-05-15 —
  client 0.43.1). `digest_changed` now requires
  `peer_version < latest`; the parity-with-stale-baseline case
  folds into the existing silent re-baseline branch alongside
  `unknown_baseline`. Same-tag re-uploads at strict parity no
  longer pop; out-of-band sideloads (adb install -r) silently
  refresh the baseline on next probe.
- "Bootstrap update popup: only `'More info'` renders
  translated" → peer-side i18n re-sync hook (acted on
  2026-05-16 — client 0.43.1). Root cause was the peer's
  `add_fallback` target capturing the client `_current` at
  peer startup, never refreshed when
  `_sync_ui_language_with_daemon` swapped the client catalog
  underneath it. Strings absent from the peer's .po walked
  the chain to the frozen English `NullTranslations` and
  came back as msgids; the `translate.tr` second-chance
  retry to `_client_tr` was the only path that found
  French, and it depended on the host catalog returning the
  msgid unchanged (which it did for 'More info' but not
  for the four bootstrap-side strings — the host catalog
  likely had English msgstrs for them as leftovers from a
  pre-dedup pass, so `translated != msg` and the retry
  didn't fire). Fix shape: `client.i18n.subscribe_language_change`
  callback API (peer re-creates its own gettext.translation
  in the new lang and re-`add_fallback`s on every client
  re-language); `translate.tr` second-chance retry dropped
  (peers must `add_fallback` correctly per the contract).
  See `CLIENT_INTEGRATION.md` § 6 for the peer-side wiring.
- "Project-shared KV synced across phones (with atomic
  slot-claim)" → shipped in 0.47.9. `project_kv_get` /
  `project_kv_set` / `list_slots` / `claim_slot` /
  `release_slot` client wrappers + the daemon-side merge
  driver that picks the later `claimed_at` for slot
  conflicts. Peer contract + locked semantics +
  recommended flow live in `CLIENT_INTEGRATION.md` § 21.
