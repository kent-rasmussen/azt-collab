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

## LANOK rendering is asymmetric across LAN-paired devices

**RESOLVED in v0.47.0** — the rendering model was redesigned
around three independent counts (`wan_unshared` /
`lan_unshared` / `at_risk`) and a 5-state label set
(`OK` / `LAN-N` / `WAN-N` / `WAN-x_LAN-y` / `WAN-x LAN-y`)
with per-channel red colouring per § 17b. The asymmetric
behaviour described below is gone: both originator and
LAN-cloned devices now compute `lan_unshared` against
per-peer observed `last_seen_main`, which is symmetric.
LAN-only projects (no origin URL) render as `WAN-N` with
the whole history as a friction signal — intentional
("no github backup, growing data-at-risk" per
[[lanok-n-is-intentional-friction]] in agent memory).
The original note is preserved below for archaeology.

---

**Files**: § 17b rendering recipe; § 20 hard rule 4;
daemon-side `commits_ahead` computation for `remote_url=''`
projects.
**Filed**: 2026-05-26 by recorder peer team (supersedes
an earlier draft that overstated the case as
"commits_ahead always 0 for `remote_url=''`" — phone-B
evidence below disproves that).

**Symptom**: two phones LAN-paired on the same project,
both with `remote_url=''` + `work_offline=on` +
`lan_allow_sync=on`, render DIFFERENT badges for the same
synced state:

- **Phone B** (project originator): badge shows
  ``publish to back up (LANOK +6) · LAN-only`` —
  `commits_ahead=6`, `unshared_commits=0`. LANOK lands per
  § 17b.
- **Phone A** (LAN-cloned from B): badge shows
  ``<prefix> <red>+1</red> · LAN-only`` —
  `commits_ahead=0` even immediately after
  `[lan-push] advanced '<B-peer-id>' main: '<old>' →
  '<new>'` confirms A's local commit reached B. § 17b's
  LANOK branch never fires because the gate is
  `commits_ahead > 0`.

Both renderings follow § 17b's formula correctly given the
daemon's reported state. The contract isn't being violated;
the asymmetry is in `commits_ahead` itself.

**Plausible root cause (peer-side speculation; daemon team
to confirm)**: the daemon appears to track a per-project
"received-up-to-from-peer" ref on LAN-cloned devices (set
by `lan_clone`, advanced on both LAN-receive AND
LAN-push edges) that doesn't exist on the project
originator. Phone A's `commits_ahead` is then "commits
HEAD is past that ref", which drops to 0 after every
successful LAN push. Phone B has no equivalent ref so
`commits_ahead` counts everything back to the empty tree.

If that's right, the daemon IS keeping the equivalent of a
tracked remote for LAN-cloned projects — just not exposing
it via `project_status.remote_url`. The asymmetric
rendering is then a real consequence of the two devices
having genuinely different git state, not a bug in the
formula.

**The actual UX gap**: § 20 hard rule 4 promises "A LAN-
only project shows a LANOK badge." That promise is
honored on the ORIGINATOR side but not on the LAN-cloned
side. A user looking at the LAN-cloned device can't tell
from the badge that their data is LAN-safe — even though
it is, with the paired peer holding everything.

**Field evidence (2026-05-26)** — same project
`en-001-x-kent`, simultaneous observations:

Phone A (LAN-cloned, paired with peer `841d43a8`):
- 09:14:47 `[settings] publish candidate:
  'en-001-x-kent' (remote_url='')`
- 13:12:21 `[commit] 'en-001-x-kent' done:
  codes=['COMMITTED_LOCAL']` — A made one local commit.
- 13:12:22 `[lan-push] advanced '841d43a8' main:
  'c942c34c4899' → 'ce321b76b48b'` — pushed to B.
- 13:12:22+ `[project_status] 'en-001-x-kent' n_changes=1
  commits_ahead=0` — and stays at 0.
- User-visible badge: ``<prefix> <red>+1</red> ·
  LAN-only``.

Phone B (paired with peer being A above):
- Same project, same time: badge reads
  ``publish to back up (LANOK +6) · LAN-only``.

**Proposed fix shapes** (daemon team picks):

1. **Daemon-side: surface the LAN-tracking ref.** Add a
   parallel `commits_ahead_of_peers` field on
   `ProjectStatus` counting HEAD commits not yet delivered
   to ANY paired peer. On the originator this coincides
   with the existing `commits_ahead`. On the LAN-cloned
   device this would also drop to 0 after a successful
   push. Then the recipe can render "you're ahead of
   nobody — fully LAN-shared" on either side.

2. **Recipe-side: render LANOK even when
   `commits_ahead==0`** for `remote_url==''` projects that
   have `head_sha` non-empty AND `lan_pushed_sha ==
   head_sha`. Meaning: "this device has commits, and the
   most recent one has been LAN-delivered." No count
   carried (`commits_ahead` is 0 under current daemon
   semantics on the cloned side), but conveys the safety
   state.

3. **Daemon-side: align originator and cloned semantics.**
   Pick one definition of `commits_ahead` for
   `remote_url==''` projects and apply it on both sides
   so both phones render the same badge for the same
   project state.

Fix 2 is smallest scope (recipe-only). Fix 3 is the
cleanest long-term. Peer team will not ship a peer-side
workaround — recipe is the single source of truth per
§ 17b and forking it on peers would drift the suite.

