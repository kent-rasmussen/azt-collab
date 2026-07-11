# LAN push dials a STALE peer address despite fresh announcements

- **Scope & relationships:** azt-collab/lan-sync — daemon peer-address
  selection. Kin of the fixed 0.53.6 (hotspot `0.0.0.0` guess) and 0.53.7
  (mDNS `127.0.1.1` advertisement) address bugs — next variant in the family.
- **Vision / done-criteria:** a push always uses the peer's MOST RECENT
  announcement address; a stale record (from a previous network life, e.g.
  hotspot) never outlives a fresh arrival. Regression test if the sweep's
  address source permits one.
- **Deadline:** 2026-07-15 — it recurred MIRRORED within the hour (phone side,
  see below), and LAN sync is the workshop transport.
- **Waiting on:** Nothing — DONE 2026-07-11 (drill passed: port memo held
  34501 across restart, debounce caught the duplicate sweep, one-cycle
  recovery both rounds; phone APK rebuild rides normal build cadence).
  Reopen if a stale-address repro survives 0.54.3 — first suspect then is
  the zeroconf multi-A-record watch-item below.

## Research (fix shipped 0.54.3, 2026-07-11 — covers all three numbered asks)

Where the stale address came from, per variant:

- **Desktop→phone hotspot ghost (17:09).** `lan_push._resolve_endpoint`
  reads: live mDNS cache (5-min TTL) → `static_endpoints` head → legacy
  `endpoints`. Fresh arrivals DID overwrite the live cache; the ghost came
  from the **static fallback** after the cache entry expired. The static
  head was frozen at the hotspot value because `_persist_resolved_endpoint`
  (drifts the head forward on every resolve) was wired only into the
  Android NSD path — the desktop zeroconf `_record` never called it.
  → **Fixed (ask 1):** both discovery paths persist identically now.
  Matches the verified workaround: restart repopulated the live cache,
  which outranks the stale static head.
- **ConnectTimeout bypass (compounding).** The stale-subnet signature is
  `ConnectTimeoutError`, which the failure handler didn't recognize (only
  refused/`NewConnectionError`) — no invalidation, no fast-fail record, no
  escalation; the ghost was re-dialed every fan-out. → **Fixed:**
  connect-phase timeouts (not read timeouts) take the recovery path, and
  new `peers.demote_static_endpoint` moves a failing address to the tail
  of both fallback lists (skipped on ENETUNREACH — our own network's
  fault).
- **Phone→desktop negative-cache loop (17:19).** The arrival path has
  cleared the fast-fail gate since 0.50.49 — but a re-announcement at an
  already-held endpoint is NOT an arrival, and NsdManager often never
  fires a fresh resolve for a same-name rebind, so the gate stayed set
  while the desktop announced its new port. → **Fixed (ask 2):**
  `_clear_unreachable_on_announcement` — ANY mDNS announcement from a
  gated peer clears the gate, on both discovery paths. Worst case
  (announcing daemon, wedged listener) is one real connect attempt per
  fan-out instead of a microsecond skip; demotion bounds the damage.
- **Port churn at the source.** → **Fixed (ask 3):** the listener memoizes
  its bound port (`$AZT_HOME/lan_listener_port`) and re-binds it when free
  (ephemeral fallback when taken), so a daemon respawn no longer
  invalidates every peer's cached/persisted endpoint at once.
- **Doubled sweep lines (17:14:23):** real duplicate work, not double
  logging — the listener-bind sweep and the first mDNS-arrival sweep raced
  (no in-flight guard). → **Fixed:** `sweep_peer` per-peer 8 s debounce.
- Watch-item (not implemented): zeroconf `_record` takes
  `info.addresses[0]` — a peer announcing multiple A records (two active
  interfaces) could still record a non-routable address. No field evidence
  yet; revisit if a stale-address repro survives 0.54.3.

Tests: `tests/test_lan_stale_endpoint.py` (demotion, resolution-order
regression, persist drift/idempotence, gate-clear on announcement, port
memo).

## Notes

Evidence (2026-07-11, ~17:09, desktop daemon 8f19208f, log
`daemon-8f19208f-2026-07-11_log.txt`):

- Phone ('3a0285ec') announcements at the time: `192.168.10.23:39391`
  (current office LAN; earlier same day `192.168.10.23:34917`).
- Push attempt dialed `10.42.0.100:40425` — the NetworkManager
  connection-sharing subnet, i.e. an address from an earlier hotspot pairing —
  and hung to ConnectTimeout:
  `[lan-push] push to '3a0285ec' at 10.42.0.100:40425 failed: …ConnectTimeoutError… 'Connection to 10.42.0.100 timed out.'`
- Symptom: `lan-unshared = 3` sat undelivered with the recorder OPEN on the
  phone the whole time; user saw no convergence.
- Question for the fix: where does the sweep get its address — peers.json
  `last_seen` vs the live discovery table — and why did the fresh arrival not
  overwrite the hotspot record (multiple records per peer? per-network keying?).
- Workaround VERIFIED (same day, 17:14): daemon restart (0.54.2,
  /v1/admin/restart) → phone's fresh announcement (192.168.10.23:39391) used
  immediately → `advanced '3a0285ec' main '824f6f' → '5b1b9a'`, 1/1 delivered,
  lan-unshared and at-risk both → 0. Confirms the sweep was reading a stale
  peer record while the live discovery table was correct.
- Minor observation for the same fix: the push/sweep lines at 17:14:23 appear
  DOUBLED (two identical `advanced` + two `1/1 delivered` lines) — dual sweep
  paths racing, or double logging? Check while in there.

**MIRRORED on the phone, same day ~17:19 (adds the negative-cache twist):**
the desktop daemon's 17:14 restart changed its LAN listener PORT (35205 →
33115 — it's ephemeral per process). The phone's record for '8f19208f' went
dead, pushes failed, and the phone negative-cached it:
`[lan-push] '8f19208f' recently unreachable; skipping (fast-fail)` — then
SKIPPED every subsequent push while lan_unshared climbed 11 → 12+, even though
the desktop was announcing its new port the whole time. So the full fix needs:
1. fresh announcement (new addr/port) must UPDATE the peer record (both sides,
   original finding), AND
2. fresh announcement must CLEAR the recently-unreachable fast-fail marker
   (new address = new evidence), AND/OR
3. a STABLE listener port per peer (persist the port in lan_state) so daemon
   restarts stop invalidating every peer's records in the first place.
Workaround: restart the phone's collab server app (or wait out the fast-fail
cooldown after the phone re-hears the desktop).
