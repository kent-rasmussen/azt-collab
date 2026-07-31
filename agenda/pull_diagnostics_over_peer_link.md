# Pull diagnostics over the peer link (cable/LAN), with wedge recovery

- **Scope & relationships:** azt-collab (daemon LAN listener + client
  UI). Builds on the paired-peer TLS channel
  (azt-collab/agenda/usb_cable_transport.md — verified over cable
  2026-07-22) and the existing diagnostics bundle
  (`prepare_share_bundle`, shared format helper
  `azt_collab_client.diagnostics`). Unblocks verification asks on
  azt-collab/agenda/spurious_team_update_prompts.md and
  desktop_save_cost_reduction.md Phase 0 — replaces every "run this
  grep on their machine" with "plug in, pull, move on."
  Wedge-recovery leg relates to
  azt-collab/agenda/daemon_lock_across_network_io.md (the wedge
  class it works around).
- **Vision / done-criteria:** field tech plugs their own phone (or
  laptop) into an affected device's link, taps ONE thing on their
  OWN device, walks away with the diagnostics bundle — the owner
  keeps working, their server UI is never opened, even when their
  daemon is wedged. Bundle is then forwardable from the tech's
  device via the existing share affordances.
- **Deadline:** none
- **Waiting on:** Nothing

## Status — DONE 2026-07-31, both halves confirmed

Legs 1–4 shipped 0.54.74 (2026-07-25); both verification asks are now
closed:

- **Diagnostic pull-through — CONFIRMED WORKING 2026-07-29.** Plug in,
  Manage paired device, "Get diagnostics from this device" → bundle
  lands on the puller and the share sheet offers it.
- **Wedge recovery — CONFIRMED 2026-07-31 (Kent): present and working
  in peer settings.** The remote-restart escape hatch (leg 3) is
  reachable from the puller's own device, which was the last thing
  the item was holding for.

Done-criteria met: a tech pulls diagnostics, and can restart a wedged
peer daemon, without the owner's UI ever being opened. The DEAD-daemon
case remains out of scope by design (no listener, nothing to answer —
see leg 3's honest limits below).

## Decisions (Kent, 2026-07-25)

- **Pull, not push.** The affected device normally does NOT have the
  server UI up; push would mean booting it first. Pull = zero
  gestures on the owner's device. Pairing (a prior QR gesture on
  their device) is the consent boundary that authorizes a paired
  peer to pull logs.
- **Recovery must not require their UI either.** Field incident
  2026-07-25: "check data link" hung (20 s watchdog fired); local
  "Restart server" resolved it. Neither opening their settings UI
  nor asking the owner to do so is acceptable in the pull workflow.

## Plans

### Leg 1 — pull RPC on the peer listener
- New listener route (paired-peer authenticated, same identity model
  as the sync routes): request → daemon runs the
  `prepare_share_bundle` collection and STREAMS the tar.gz back.
- **Lock hygiene (load-bearing):** the collection path must not
  take `project_lock` (logs + config + snapshot reads only) — the
  prime use case is pulling from a WEDGED daemon whose scheduler
  threads hold locks; the listener thread can still serve lock-free
  routes.
- Puller side: bundle lands under the puller's
  `$AZT_HOME/.shares/pulled/<peer>/`, and the UI immediately offers
  the existing share sheet (Android `share_files` single-item
  ACTION_SEND path) so it can be forwarded from the tech's own
  device. Standard new-API checklist (endpoint, wrapper, status
  codes both mirrors, FR translation).

### Leg 2 — puller UI
- "Pull diagnostics" on the Manage Paired Device screen (and/or the
  device row) — visible on every peer app since it lives in the
  shared picker UI. Progress + typed failure statuses; on success,
  offer share/forward.

### Leg 3 — wedge recovery, remote
- Paired-peer listener route: restart daemon (reuses the existing
  cooperative-restart + kill-by-PID machinery from the local Restart
  button). Serves the WEDGED-ALIVE class: listener thread responds
  while scheduler threads are stuck on `project_lock`/network — the
  common field wedge. Pull flow on timeout offers "restart their
  server and retry" from the puller's device.
- **What it cannot fix (document honestly):** a fully DEAD daemon
  has no listener; nothing arrives over the link. Mitigations per
  platform, no new code: desktop — the owner's running azt polls
  auto-respawn the daemon (SERVICE_RESTARTED); Android — any local
  peer call lazy-spawns via the provider contract, and the FGS keeps
  it alive while LAN sync is on.

### Leg 4 — local auto-restart on the cable-check watchdog
- The existing "Checking cable link…" 20 s watchdog (0.54.57)
  currently just unsticks the UI. On timeout: health-probe own
  daemon; if dead or unresponsive → auto-restart via the existing
  restart machinery, then re-run the check once; surface honestly
  ("server was unresponsive; restarted"). Health probe first — a
  slow-but-working daemon must not be killed on pattern-match
  (evidence before state-change). Small, independent; can ship
  ahead of legs 1–3.

**Restart cost/risk analysis (Kent asked 2026-07-25):** the restart
itself is cheap by design — in-flight jobs flip to typed
`JOB_INTERRUPTED` (peers retry), interrupted transfers are one clean
log line + sender retry, uncommitted bytes are power-cut-contained
(next commit stages them), flocks release on process death, the
listener re-binds its previous port (0.54.3) so peer endpoints stay
valid, and backoff curves deliberately survive restarts (no burst
storm). Two real risks, both in the AUTO trigger, not the restart:
1. **Killing a healthy-but-busy daemon** — e.g. mid-40 s merge on a
   slow machine. The merge is crash-safe (no commit → recomputed) but
   the work restarts from zero; on a slow machine that can livelock:
   slow merge → watchdog → restart → merge from scratch → watchdog…
   Mitigation: auto-restart ONLY when `/v1/health` itself fails —
   health is lock-free and never raises (0.54.1), so a daemon wedged
   on `project_lock` still answers it; "health OK but check timed
   out" means the problem is elsewhere → report, don't kill. Plus a
   rate limit (max one auto-restart per ~10 min).
2. **kill-by-PID mid-git-write** — cooperative shutdown first;
   SIGKILL fallback can orphan tmp files in `.git` (benign; object
   writes are tmp+rename, ref updates atomic) — no corruption class
   known, but keep kill as the fallback, never the first resort.

### Health-first (Kent 2026-07-25: "any reason health wouldn't
### answer if the daemon was up? start there")

Adopted: the cable check now probes `/v1/health` FIRST and leads
every state of the flow (waiting / result / did-not-answer) with a
one-line service verdict; the 5 s settings board tick feeds the same
verdict into the LAN status line on BOTH platforms (`lan_toggle` now
returns `version` + a client-side `alive` flag — pre-0.54.74 a dead
daemon and "LAN sync off" decoded identically, so the line could
show a stale "Listening on … · pid N" over a dead service).

**Why health is the right evidence:** `/v1/health` is
unauthenticated (`UNAUTHENTICATED_PATHS`), lock-free, never raises
(0.54.1), and desktop serves it on its own thread
(`_ThreadingHTTPServer`) — so a daemon merely wedged on
`project_lock` still answers. Health-OK-but-check-timed-out
therefore means "up but busy" → report, don't kill (killing a
40 s merge restarts its work from zero and can livelock a slow
machine).

**Known cases where health can be silent though the process lives**
(all still best-remedied by a restart, which is why auto-restart on
silence is sound — but they mean "silent" ≠ "dead"):
1. Desktop: stale / missing / wrong `server.json` — the client
   can't FIND a live daemon (discovery failure, not liveness).
   Restart rewrites it.
2. Desktop: fd exhaustion (EMFILE) — `accept()` fails while the
   process lives (cf. the 0.54.1 fd-leak incident). Restart clears
   the fds.
3. Android: the provider dispatch runs on binder threads under one
   GIL; a long CPU-bound merge can starve health (unlike desktop's
   independent thread). Restart is the same remedy.
4. Process alive but still in boot phase before `serve()` — restart
   is neutral here; the rate limit keeps it from looping. (Related
   parked item: daemon boot-phase state in 503.)

## Notes
- **The target's LAN sharing must be ON.** The pull route lives on
  the TLS listener, and the listener only runs continuously while
  sharing is on (see the toggle analysis in
  [[usb_cable_transport]]). A device with sharing off is unreachable
  except inside a 30 s burst window, so "Get diagnostics from this
  device" against it will report not-answering. Relevant to the
  courier flow: the phone needs sharing on for a computer to pull
  from it.
- Consent consideration (raised + resolved): logs are chattier than
  project data, but pairing is a deliberate physical-presence QR
  gesture and the field alternative (opening the owner's UI,
  interrupting work) is worse. If this ever needs tightening, add a
  per-peer "may pull diagnostics" flag in peers.json rather than a
  per-pull gesture.
- 2026-07-25 incident detail: "check data link" non-response was
  presumed daemon-dead-didn't-respawn; "restart server" fixed it.
  Wedged-alive (lock held) fits the same symptom — leg 4's health
  probe handles both without diagnosing which.

## Research
