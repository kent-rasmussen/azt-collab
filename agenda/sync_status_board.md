# Sync status board: see projects × peers, heads, and what's left

- **Scope & relationships:** azt-collab daemon UI first (the
  settings/collab app both desktop and server-APK run), client
  picker later. Surfaces facts the daemon ALREADY holds — nothing
  new computed. Overlaps project_identity_beyond_langcode.md Tier 1
  (provenance columns) and answers the diagnosis questions this
  2026-07-22 session kept hitting.
- **Vision / done-criteria:** at a glance the user can answer:
  is a sync/merge/clone running right now? what have I got that a
  peer doesn't (and vice versa, where known)? is convergence done
  (all ±0, idle) or still in progress? did a merge go wrong? what
  commit/head is each project at, and where is it bound (dir +
  remote)? — WITHOUT reading daemon logs.
- **Deadline:** none (but demand is proven — kept being the missing
  thing all through the 2026-07-22 workshop)
- **Waiting on:** Nothing

## Status

Tier A shipped 0.54.38; ANR fix 0.54.45; **2a shipped 0.54.46** —
daemon caches the board and re-walks git only on a change event
(commit / LAN delivery / pairing; `repo.invalidate_peer_sync`), with a
30 s staleness backstop; the UI poll is a cheap cached read. This is
the "changes arrive with the changes" model, server-side.

**2b — DEFERRED (this item's remaining push work).** Replace the
residual cheap cache-poll with real push so the UI never asks:
- **Android:** `ContentResolver.notifyChange(uri)` from the `:provider`
  daemon + `registerContentObserver` in the Activity — the native
  cross-process push, a clean fit since the daemon is already a
  ContentProvider. This is the worthwhile half.
- **Desktop:** loopback HTTP has no push channel — would need SSE/
  long-poll or a file-watch (itself a small poll). Low value: a
  cache-read poll there is already ~free. Probably skip desktop 2b.
Kent 2026-07-23: "do 1 & 2a, store 2b." Significantly more work than
2a (per-platform push machinery + two implementations) for the last
increment (kill the near-free poll); pick up when Android push is worth
the plumbing.

## Plans

### Rows: project × paired-peer
Per project: name, working_dir tail, remote (stored spelling),
current head (short SHA) + entry/commit count, wan_unshared,
last_commit time. Per shared peer under it: shared y/n, their
last_seen_main vs ours (ahead/behind/level where known),
covered_local, last successful delivery time, and LIVE state
(idle / dialing / merging / cloning — the scheduler + lan_push
already know this).

### "Done" must be legible
The core ask (Kent, 2026-07-22: "I don't know when azt-collab
thinks it's done, but I clearly see there's a problem"): a project
is visibly settled when every peer row is ±0 and no job is
running. A running merge/clone/push shows as such, so "still in
progress" is never mistaken for "broken" or "done."

### Head/identity visibility (2026-07-22 driver)
No current UI shows a project's head SHA on any device — so the
disentangle couldn't be verified on the phone ("is its nml still
at the merge tip, nothing recorded after?"). The board must show
head per project per device so a user can confirm state without
git on the command line.

### Data sources (all already present)
`project_status` (wan/lan_unshared, at_risk, n_changes,
last_commit, last_sync_error), `peers.json`
(shared_projects, last_seen_main, last_covered_local,
static_endpoints), scheduler job state + lan_push in-flight flags,
repo head. No new computation — pure surfacing.

## Notes
Origin: 2026-07-22 workshop. Three independent asks converged on
this: "is the sync done / was there a bad merge?"; "which directory
is 'nml' bound to?" (the cross-merge incident); "what head is this
project at?" (couldn't verify the disentangle on the phone).

## Pending offers: surface, don't nag (design 2026-07-23)

Same "surface facts on the peer screen, don't nag" philosophy as this
board; belongs here. Trigger: a stale accepted clone offer for `nml`
from a vanished phone (`db033cd4…` @ 192.168.31.76, unreachable subnet)
retried forever — `[lan-clone] … pending kept for retry` on every
discovery tick + log spam. Kent: "any principled reason to have these
nagging messages at all? we have a peer screen."

**Model (agreed):**
- An incoming share/clone offer is a **durable, passive intent**, not a
  background retry loop. No blind re-dial of a cached/stale address; no
  log-spam.
- **Consent is two-sided and explicit:** the offerer offered (their
  approval); the receiver **taps once to affirm** ("clone/share when we
  next meet"). Nothing pulls data before that tap.
- **After the tap, completion is automatic "under the hood"** the next
  time the two are genuinely together (real mDNS arrival with a
  reachable endpoint — we already fire `sweep_peer` on arrival). NOT
  per-meeting tapping. This preserves hands-free convergence.
- **Stale / peer-absent:** instead of retry, a plain typed status —
  *"{peer} not connected; ask for clone/share again when they're
  around."* New status code (peer-side, e.g. `LAN_OFFER_PEER_ABSENT`),
  displayed, not looped.

**UI surfacing (two places):**
1. **Nearby & Paired devices** (the list): at the END of a device's
   project list, a red **"{project} pending"** entry, clickable to
   affirm — so the invitation + which device it's under is visible
   WITHOUT drilling into Manage.
2. **Manage Paired Device** (per-device detail): pending offers listed
   ABOVE "shared projects", same affirm affordance.

**Supersedes** the retry-budget/expiry idea floated 2026-07-23 — surface
-not-retry is the cleaner answer; drop the expiry approach.

**Build order:** daemon lifecycle first (stop blind retry; persist
offer as affirm-pending; complete on real arrival post-affirm; typed
absent-status), then a list/affirm RPC + client wrapper, then the two
UI surfaces. Own version(s); freeze discipline.

**BUG found 2026-07-23 — Decline doesn't stick under asymmetric
reachability.** `_h_lan_decline_offer` (server.py) removes the pending
decision, then best-effort nacks the sender. But the inbound handler
`_handle_share_offer` (lan_listener.py:607) **re-stashes the offer
unconditionally** on every sender POST. So: a connected sender gets the
nack, rolls back its `shared_projects`, stops offering → stays cleared;
but a sender we can't reach back (one-way reachability — it reaches us,
we can't reach it: the same failure that blocks the clone) never gets
the nack, keeps re-offering on its bursts, and the decline is undone on
the next inbound POST → the offer recurs. (Accept recurs too, by
design — clone fails, kept.) Field: the `nml`/`db033cd4` offer recurred
on both accept AND decline; a different, connected phone's offer
declined cleanly.
**Fix:** persist a local "declined" suppression keyed (peer_id,
langcode); in `_handle_share_offer`, if a re-arriving offer matches a
suppression, DON'T re-stash — silently drop and re-attempt the nack
(clear the suppression once the sender acks / stops, or on a TTL). Makes
Decline stick even when the nack can't be delivered. Land AFTER the
Stage 2/3 agent's server.py edits (avoid clobber); own version.

## Clone/offer completion papercuts (2026-07-23 pm, field)

Cluster found after 0.54.53/.54 shipped the offer surfacing. Land as
one version so the recorder is rebuilt once.

1. **DONE 0.54.56 — adopt-origin surfaces in-flow.**
   `_offer_confirm_popup` chains into
   `_resolve_adopt_origin_then_done` on `LAN_ADOPT_ORIGIN_NEEDED`,
   same device, same gesture.
2. **DONE 0.54.55 — "awaiting first sync" right after cloning.**
   `lan_clone.clone_from_peer` records
   `peers.set_peer_last_seen_main(peer_id, langcode, <cloned HEAD>)`
   after register → board shows "up to date" post-clone.
3. **DONE 0.54.56 — offer affordance self-clears.** 3 s change-only
   `lan_pending` watcher in `paired_phones_popup` rebuilds rows when
   the offer set changes; daemon already keeps the decision until
   delivered; errors show in the confirm popup ("3 covers 1").
4. **DONE 0.54.56 — Retry reports outcome.** `retry_peer` returns
   sweep outcomes; button '…' → 'OK'/'!' + forced board re-poll.
5. **DONE 0.54.56 — red buttons → red text links** (`_link_button`),
   both Nearby "{project} pending" and Manage "Review".
6. **DONE 0.54.57 — truncated inbound push logs one clean line**
   (caught in `_generator`; "peer disconnected mid-transfer; sender
   will retry").
7. **DONE 0.54.57 — "Checking cable link…" can't strand.** Real cause
   was daemon-side: the handler ran restart_browse + burst INLINE
   before responding (seconds-to-minutes). Nudge now backgrounded;
   response immediate; UI adds a 20 s watchdog as belt-and-braces.
8. **`SERVICE_RESTARTED` repeating in the server UI log + earlier
   `AZTServiceConnector.ensureBound … ClassNotFoundException`.** The
   settings-UI client keeps finding the daemon connection dropped and
   respawning it — i.e. the daemon isn't staying alive between calls.
   Likely root: the sticky-bind service never binds (ClassNotFound), so
   the `:provider` daemon is killed when idle and every UI call
   lazy-respawns it. This is probably the source of much of the
   session's flakiness (dropped transfers, "did nothing" taps). HIGHER
   priority than the cosmetics — chase the ClassNotFoundException
   (dex/manifest injection of `AZTServiceConnector` in the current
   build) first. NOT yet diagnosed.

## Research
