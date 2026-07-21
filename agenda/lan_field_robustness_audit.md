# LAN field-robustness audit — 2026-07-21 session

- **Scope & relationships:** azt-collab/daemon (lan_push, lan_discovery,
  lan_listener, peers allowlist). Findings from a live phone↔desktop
  diagnosis session (baf, moto g power / karlap) on a changing-network
  day: field router → home wifi, VPN up/down mid-session. Sibling of
  the 0.50.x sync-rebuild audit pattern: each finding carries evidence,
  a proposed rule, and a status; a finding closes by shipping (CHANGELOG
  + CLAUDE.md invariant update where the rule is load-bearing) or by
  explicit wontfix with reason.
- **Vision / done-criteria:** every finding below is CLOSED (shipped or
  wontfix-with-reason). Rules that survive implementation get folded
  into azt-collab/CLAUDE.md invariant #10 text, not left here.
- **Deadline:** none
- **Waiting on:** Nothing

## Context (why these matter for our users)

Field reality this audit assumes: devices hop networks (field router,
home wifi, hotspots), take VPNs up and down, sleep mid-sweep, and
accumulate months of stale peer endpoints. LAN sync must degrade to
"cheap, bounded, honest no-op" on every one of those, because the same
radio and the same worker threads also carry WAN convergence.

## Findings

### F1 — Unbounded connect timeout on LAN dials  [FIX SHIPPED 0.54.12 — verify in field]
2026-07-21: push pm now `Timeout(connect=5, read=180)`, sweep-peek pm
`(connect=5, read=10)` — the only two unbounded pms in lan_push.py
(hello/signalling already passed per-request bounds). NOT yet
audited: lan_clone.py's pm (clone has its own wall-clock guard;
check its connect phase before closing this finding).
Every LAN HTTP op shows `connect timeout=None`; a dead endpoint holds
the OS-level TCP connect (~2 min) × `Retry(total=2)` ≈ **6 min of a
worker thread per dead endpoint**. Made every trigger in the session
look like it "did nothing" — effects surfaced minutes after causes.
- Evidence: 07:04:07/07:04:11/07:05:31/07:05:35/07:06:04/07:06:08
  ConnectTimeoutError to 192.168.10.101:44671 (~2–2.5 min after their
  fan-outs, matched by thread id); again 07:35:19 to 10.124.
- Proposed rule: every LAN dial gets a bounded connect timeout (order
  3–5 s) distinct from WAN budgets; LAN is opportunistic, so failing
  fast IS the correct behavior.

### F2 — Stale endpoints from previous networks are dialed  [OPEN]
Sweeps dialed 192.168.10.101, 192.168.150.140, 192.168.10.124,
192.168.10.143 (three previous networks) while the peer was live and
freshly announced at 192.168.31.60. Combined with F1 this burns many
minutes before any live endpoint is tried.
- Evidence: 07:22:06 sweep block — four stale hosts dialed, all
  ENETUNREACH/timeout.
- Proposed rule: freshly-resolved mDNS endpoints are dialed first;
  remembered endpoints are demoted/tail-capped (0.54.3 demotes on
  failure but doesn't stop the first expensive attempt); consider
  pruning remembered endpoints whose subnet doesn't match any current
  interface. Careful: `static_endpoints` (hotspot-host fallback) is
  deliberate and must survive.

### F3 — Fast-fail gate is only cleared by a caught announcement  [OPEN]
The unreachable-mark on a peer is cleared solely by an mDNS arrival the
device happens to catch. Blackhole period (F6) re-armed the desktop's
gate; the 07:49:06 picker burst ran a 30 s window with **zero** dial to
the known-good desktop endpoint — the gate silently outlived the
condition that set it.
- Evidence: 07:22:06 marked unreachable → 07:22:17 arrival cleared →
  sweep died in blackhole (re-armed) → 07:49:06–07:49:36 burst window
  with no 8f19208f dial.
- Proposed rule: gate carries a TTL, and/or own-network-change +
  lifecycle gestures (burst, listener re-bind) clear gates before
  sweeping. A gesture-driven burst that skips a gated peer must at
  least log the skip (see F7).

### F4 — Allowlist contains projects with no local repo  [OPEN]
Phone sweeps push `en`, `en-001-x-kent`, `en-BR-x-kent`,
`en-TH-x-anna`, `sw-US-x-kent` — none exist locally. Each becomes:
peek peer tip → local reads `''` → "would be force-overwrite" → merge
route → `NotGitRepository`. Desktop symmetrically peeks `/en.git` on
the phone and gets NotGitRepository over and over.
- Evidence: 07:22:06–07:22:07 sweep block; 07:47:42 lan-merge open
  failed; 07:48:26/07:49:20 listener en peeks from desktop.
- Proposed rule: sweep skips allowlisted projects with no local repo
  (one summary log line, not one per dial); listener answers a typed
  "not here" cheaply. Decide separately whether the allowlist should
  auto-prune or whether "shared but not yet cloned" is a state we keep
  (it is meaningful: peer may clone later — so probably skip, don't
  prune).

### F5 — Large LAN push dies: SSLEOFError on git-receive-pack  [PARTIAL FIX 0.54.12 — abort trigger still unknown]
ROOT CAUSE (desktop log, lines 1399–1489): attempt 1 (POST 07:47:17.9)
was **fully ingested by the desktop** — dulwich 1.2.11 dechunked and
processed the whole pack, reached `_report_status`, and died only
WRITING the response (`BrokenPipeError`: the phone had hung up ~8.5 s
into the exchange, before the desktop's 11.5 s ingestion finished).
The three `Invalid pkt-line` tracebacks are urllib3 retries resending
the remainder of the non-rewindable generator body. Two consequences:
the push may partially succeed server-side while the client records
failure (ref update itself likely rejected on stale old-sha — desktop
wan_unshared stayed 0 — so the designed merge-routing would have
engaged had the response arrived); and every transient hiccup became
3 corrupted requests + buried evidence.
SHIPPED 0.54.12: `retries=False` on the push pm (retry layer is the
sweep/merge machinery); read=180 so the client outwaits ingestion.
STILL OPEN — abort trigger, now better characterized (11:11 repro):
second incident shows the same frame with a SMALL pack — phone
arrival 11:11:10.3, POST ~11:11:11, desktop fully ingested and hit
BrokenPipe at the FIRST response byte at 11:11:11.8 (<1 s). First
incident aborted at ~8.5 s ≈ end of a big upload; this one at
~0.9 s ≈ end of a small upload. The abort tracks UPLOAD COMPLETION,
not wall-clock → not a timeout: the phone-side client appears to
tear down the connection immediately after sending the pack body,
without waiting for report-status. Suspect the phone's
dulwich-1.2.7 + urllib3-2.x pairing on generator/chunked POST
bodies (an exception raised between end-of-body and response-read
would abort the socket exactly there). Needed: phone logcat
11:11:05–11:11:20 for the client-side exception string; confirm
which APK build the phone runs. Also 11:11:07 shows a
ConnectionResetError on a connection that died before even sending
its request line (separate connection, same teardown flavor).
Silver lining: if the desktop APPLIED the ref update before the
response write failed, the phone's next peek sees desktop-at-our-
head → records coverage → converges despite the lost response
(verify via desktop `git log` + phone lan_unshared).
First real phone→desktop contact after recovery: baf push (~147
commits, audio-heavy) died ×3; phone saw SSLEOF, desktop saw
`GitProtocolError: Invalid pkt-line length prefix` with pack-interior
bytes (`b'x\x9cm\x8f'` = zlib header; two more attempts random bytes).

Mechanics established 2026-07-21:
- Phone bundle now ships **dulwich 1.2.7** (P4A upgrade rode in);
  its `send_pack` streams the request body as a GENERATOR, so
  urllib3 sends `Transfer-Encoding: chunked` (no Content-Length).
  Pre-upgrade dulwich buffered bytes + Content-Length — that's why
  LAN pushes worked in the 0.54.4 drills. Desktop env has dulwich
  1.2.11, whose `handle_service_request` DOES dechunk
  (`HTTP_TRANSFER_ENCODING == 'chunked'` → ChunkReader), so plain
  "wsgiref can't dechunk" does NOT explain the first attempt.
- **urllib3 Retry(total=2) cannot rewind a generator body**: after
  attempt 1 fails, each retry sends only the REMAINING generator
  output — the observed three `Invalid pkt-line` tracebacks with
  different random offsets are corrupted RETRIES, not the original
  failure. Client-side rule regardless of root cause: never let
  urllib3 retry a non-rewindable POST body (corruption + 3× log
  noise per failure).
- The desktop `post-receive reset lock busy (5s)` line fires from the
  middleware `finally` AFTER the protocol error — dulwich's smart
  handler streams status 200 before parsing, so the middleware's
  `status.startswith('200')` gate treats failed pushes as successes
  and runs the reset anyway (split out as F8).
- Original first-attempt error still unretrieved from the desktop
  log (window before 07:47:31); requested.
- Fix directions on the table once root cause is confirmed:
  (a) client: rewindable push bodies (spool pack to disk / bytes →
  Content-Length, retry-safe, works against older listeners too);
  (b) client: disable retries on push POSTs; (c) server: verify the
  ChunkReader path engages under our wsgiref + middleware chain
  (add a regression test with a chunked receive-pack body).

### F8 — Post-receive reset fires on FAILED pushes  [OPEN]
`_post_receive_pack_middleware` gates the working-tree reset on the
captured status being 200 — but smart-HTTP receive-pack always
streams 200 before the body is parsed, so a push that dies mid-parse
still triggers the reset (and its 5 s lock wait, and the queued
retry). Amplifies noise and lock contention under F5-style failures.
- Evidence: every 07:47 traceback preceded by
  `post-receive reset 'baf': lock busy (5s timeout)`.
- Proposed rule: derive success from the dulwich exchange itself
  (report-status reached / no exception), not from the streamed
  HTTP status line.

### F6 — VPN lockdown blackhole: diagnosis signature  [DOCUMENT-ONLY]
Android "Always-on VPN + Block connections without VPN" after VPN
disconnect produces: outbound ENETUNREACH to everything + DNS dead
(`No address associated with hostname`) + `offline=True`, while
inbound listener and mDNS multicast still work. Phone showed VPN
tunnel address 10.5.0.2 while the wifi was 192.168.31.179.
- Evidence: 07:22:06–07:22:11 block.
- Action: the daemon's ENETUNREACH message already says "check WiFi /
  airplane mode" — extend it to name the VPN-lockdown case, since
  that's the field-likely cause when wifi is demonstrably associated.
  Then this finding is a support-doc fact, not a code change.

### F7 — Sweeps are silent at dial time  [FIX SHIPPED 0.54.12 — verify in field]
2026-07-21: `_push_to_peer` logs `dialing <peer> at <host>:<port> for
<lang>` at attempt start. Outcome lines already existed
(`0/N delivered`, per-failure warnings). Residual: the standalone
sweep-peek path (`sweep_peer`'s ls-remote) still logs only on
failure — acceptable now that F1 bounds make failures fast; revisit
only if diagnosis stalls again.
A sweep's only log output is the eventual urllib3 warning (minutes
late under F1) or the final `0/N delivered`. During the session this
made "did my trigger do anything?" unanswerable without thread-id
archaeology. Violates the always-emit-summary rule
(feedback_always_emit_summary).
- Proposed rule: one line at dial time per peer (`sweeping <peer> at
  <endpoint>, N project(s)`), one at outcome. Bounded, greppable.

## Plans

Order roughly F5 (data delivery blocked) → F1/F7 (cheap, unblock
diagnosis) → F3 → F2 → F4 → F6. Reassess after the desktop-side log
for F5 arrives.

## Notes

2026-07-21: session context — same day as the ssh-origin fix
(ssh_origin_push_failure.md, 0.54.11); WAN convergence for baf was
healthy throughout (wan_unshared 140 → 87 over the session), so LAN
findings never put data at risk. That property (WAN as safety net)
held exactly as designed and is worth preserving in every fix above.

## Research
