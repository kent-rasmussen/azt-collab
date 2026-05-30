## Audit 2026-05-29 — comms / data-loss / convergence

Scope: `/home/kentr/bin/AZT/azt-collab` only (canonical daemon +
client). Three lenses: low-power / weak-internet behaviour;
data-loss risks; cross-device convergence speed. Items already
documented as accepted tradeoffs (see memory `project_oom_followups_after_0.44.4.md`,
`feedback_lanok_n_is_intentional_friction.md`,
`project_lan_sync_0.45.x_field_state.md`) are not relitigated here.

### Status legend

- `[done]` — shipped.
- `[open-high]` — load-bearing for the three lenses; not yet
  scheduled.
- `[open-med]` — degrades gracefully but worth fixing; pick up
  when the surrounding area gets work.
- `[open-low]` — defensive; leave as is unless a future change
  brings the file open.

### Roll-up (as of 2026-05-30, audit-sweep pass through 0.50.14)

| # | Title | Status |
|---|---|---|
| 1 | `extra_remotes` "Use both" dropped secondary pushes | done (0.49.2) |
| 2 | Atomic LIFT commit on URI projects | done (daemon, pre-0.50) |
| 3 | Topic-branch orphans cross-device | done (0.50.15) |
| 4 | HEAD re-attach ancestry guard | done (0.50.15 — observability only) |
| 5 | Connectivity poll fixed 30 s | done (0.50.15) |
| 6 | LAN endpoint cache no TTL | done (0.50.15) |
| 7 | Slot-claim tiebreaker 1-s granularity | done (0.50.9) |
| 8 | `apply_toggle` returns OK before bind | open-low |
| 9 | `_pending_resets` no backoff | open-low |
| 10 | `_count_commits_ahead` lock-timeout silent | open-low |
| 11 | LAN-shared CAWL cache (NOTES #3) | done (0.50.14) |
| 12 | Fresh GUIDs from template (NOTES #4) | done (0.50.8) |
| ↓ | Half-strip `.git/config` cleanup | open-low |

**Open items remaining**: 0 open-med + 4 open-low. No open-high.

---

### 1. `extra_remotes` "Use both" silently dropped secondary pushes

**Status:** `[done]` — shipped 0.49.2 (2026-05-29).

`server.py:_h_lan_resolve_conflict` recorded the secondary via
`projects.add_extra_remote` but `repo.py` never read
`extra_remotes` anywhere. Wired via new `_push_extras_step`
called from both `_push_repo_locked` and `_sync_repo_locked`
after the primary `_push_step_locked`. Publish-only (no fetch,
no merge); per-URL credentials via `get_sync_credentials`;
per-URL typed status `EXTRA_REMOTE_PUSHED` /
`EXTRA_REMOTE_PUSH_FAILED`; tries every URL every call
independent of primary outcome.

Files: `azt_collabd/{repo.py, status.py, server.py}`,
`azt_collab_client/{__init__.py, status.py, translate.py,
locales/fr/LC_MESSAGES/azt_collab_client.po}`, `CHANGELOG.md`.

---

### 2. Atomic LIFT commit on URI projects (peer-visible data-loss)

**Status:** `[done]` — daemon side. RPCs
`/v1/projects/<lang>/atomic_commit` (`_h_project_atomic_commit`
at `server.py:2812`) and `/v1/projects/<lang>/atomic_finalize`
(`server.py:3124`) are shipped, routed at `server.py:3459-3461`.
Client wrappers `atomic_commit_bytes` and
`atomic_finalize_pending` exposed in
`azt_collab_client/__init__.py`. Tests in
`tests/test_atomic_commit.py`. Confirmed live during the
2026-05-30 NOTES audit — the NOTES entry was stale because the
peer's `lift_io._save` docstring still says "filed; not
shipped." Peer-side migration from the `open_write` fallback
to `atomic_commit_bytes` is a peer-side change, not a
daemon-side gap.

---

### 3. Topic-branch orphans accumulate cross-device

**Status:** `[open-med]`.

`repo._maybe_run_janitor` (calls `_janitor_sweep_topic_branches`
once per daemon-lifetime per project) deletes only
`azt-pending-*-<our_device_name>` refs. Long-running daemon
that never restarts + another device that pushed a topic
branch and then went away ⇒ orphan stays on remote
indefinitely. Memory `project_topic_branch_push.md` documents
the deliberate "no cross-device delete" stance to avoid false
positives.

Suggested minimal next step: surface orphans in
`project_status` (don't auto-delete) so a user looking at
"why is this project's remote so heavy" can see them.

Files: `azt_collabd/repo.py` (status payload),
`azt_collab_client/__init__.py` (decode).

---

### 4. HEAD re-attach ancestry guard can leave detached HEAD

**Status:** `[open-med]`.

`lan_listener.py:909-979`. After a LAN receive the re-attach
only fires when `head_is_ancestor(HEAD, main)` is true. If
main is NOT a descendant of HEAD (legitimate when local has
unmerged work) or the walker raises (collapsed into "not
ancestor" by the bare `except Exception:` at 960-961), HEAD
stays detached and the comment block at 909-933 describes a
merge-loop that only terminates via this re-attach.

Suggested minimal next step: emit a typed `Status` when
ancestry check returns false so the case is detectable rather
than living in a log line.

---

### 5. Connectivity poll: fixed 30 s, no adaptive backoff

**Status:** `[open-med]` — partially addressed by the 0.50.0
WAN-backoff rebuild but the underlying probe is still fixed.

The WAN-push side now exponentially backs off to a 24 h cap
(`wan_backoff.py`, 0.50.0), so an offline-for-hours device
isn't repeatedly attempting pushes. The Phase 6 online-edge
hook (0.50.2) fires `wan_backoff.nudge` + `lan_burst.start_burst`
when the watcher detects offline → online, so recovery is
prompt without any persistent radio cost.

**Still open**: the underlying connectivity probe itself
(`_has_internet` called every `connectivity_poll_s`, default
30 s) hasn't grown. On a phone idle in a pocket all day the
probe wakes the radio every 30 s even when no push is queued
and no peer is being browsed. That's the genuinely-wasteful
case the original finding called out.

Suggested: probe at 30 s only when something needs the result
(pending pushes, recent commit activity, watcher just started).
Otherwise grow the probe interval on consecutive same-state
ticks. Reset on user-gestured nudge.

Files: `azt_collabd/scheduler.py` (watcher loop tick),
`azt_collabd/settings.py` (settings shape).

---

### 6. LAN endpoint cache has no TTL

**Status:** `[open-med]` — discovery thrash on peer restart.

`lan_discovery.get_endpoint(peer_id_hex)` holds peer endpoints
until `invalidate_endpoint()` is called. A peer that restarts
on a new ephemeral port is unreachable until a manual LAN
toggle flip. Restart-discovery threshold of 3 consecutive
failures (~90 s) is the only auto-clearing path today.

Suggested: 5-min TTL on the cache; entry refreshes on each
mDNS announce.

Files: `azt_collabd/lan_discovery.py`.

---

### 7. Slot-claim tiebreaker: 1-second timestamp granularity

**Status:** `[done]` — shipped in 0.50.9 alongside the NOTES #2
stable-identity work. `_later_claim` in `project_kv.py` now
cascades `claimed_at` → `peer_id` (non-empty beats empty;
lexicographic between two non-empty) → `device_name`
(lexicographic fallback for legacy claims with empty
peer_id). The chain is a property of the claim itself, so
peer A and peer B both compute the same winner regardless of
which side of the merge is "ours." 0.50.9 also eager-inits
`peer_id` at daemon startup (was lazy) so 0.50.9+ claims
always have a real 64-char hex pubkey in the primary tiebreak
slot. Tests in `tests/test_slot_identity.py`.

---

### 8. `apply_toggle` returns OK before listener socket binds

**Status:** `[open-low]`.

`lan_listener.py:140-165`. Thread spawn is async; bind failure
(port conflict, Android NSD permission) happens after the
toggle handler returns success. UI shows "LAN on" while
nothing is listening.

Suggested: synchronous bind attempt in the handler; only return
OK once the socket is bound. Push the listener loop to a
post-bind thread.

---

### 9. `_pending_resets` queue has no backoff or age-out

**Status:** `[open-low]`.

`lan_listener.py:608-622`. Post-receive resets that hit
`LockTimeout` get queued for the next drain tick (30 s). A
project deadlocked on `project_lock` produces forever-retries
with no backoff; working tree stays ahead of HEAD; other peers
keep pushing expecting integration.

Suggested: per-entry retry counter + exponential backoff;
after N retries surface a typed status (or log loudly) so the
deadlock becomes visible.

---

### 10. `_count_commits_ahead` 5 s lock-timeout silently degrades

**Status:** `[open-low]`.

`lan_push.fan_out` calls `_count_commits_ahead` inside a 5 s
`project_lock` timeout. If a concurrent merge holds the lock,
the call returns immediately with an indistinguishable "0 / no
data" result. Caller doesn't tell timeout from success;
badges can show stale counts.

Suggested: return an Optional sentinel (`None`) on lock timeout
and let the caller treat that as "unknown" (skip the badge
update rather than render zero).

---

### 11. NOTES item 3 — LAN-shared CAWL cache between paired peers

**Status:** `[done]` — shipped in 0.50.14. New listener
endpoint `POST /v1/lan/cawl_fetch`
(`_handle_cawl_fetch_bodyauth`) serves cached CAWL bytes to
paired peers. `_fetch_image_bytes_from_lan_peer` in `cawl.py`
tries paired peers before GitHub in `get_image_path`. Plus
the user-flagged extension: prefetch worker grabs ALL variants
from LAN even when `cawl.prefetch_all_variants=False` (the
WAN-side variant filter doesn't apply to free LAN bytes). New
`get_image_path_lan_only` and `lan_extras` parameter on
`start_prefetch`. Tests in `tests/test_cawl_lan_share.py`.
Daemon-only; no peer rebuild required.

---

### 12. NOTES item 4 — Fresh GUIDs when creating from template

**Status:** `[done]` — shipped in 0.50.8.
`_mint_fresh_guids(xml_bytes)` in `azt_collabd/projects.py`
walks `<entry guid="...">` elements and rewrites each to a
fresh UUID-4, plus rewrites every `ref="..."` attribute
pointing at a freshened guid. Called from
`create_from_template` between download and settle. Tests in
`tests/test_mint_fresh_guids.py`.

---

### Half-strip `.git/config` cleanup

**Status:** `[open-low]` — cosmetic, documented in memory
`project_lan_sync_0.45.x_field_state.md` as "harmless to
`project_status` after 0.46.8. Future pass could write
`.git/config` directly." Not load-bearing.
