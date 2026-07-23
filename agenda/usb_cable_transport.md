# USB-cable transport: phone ↔ computer over a cable, same sync stack

- **Scope & relationships:** azt-collab (daemon LAN stack, pairing,
  discovery). Sibling of, NOT the same as,
  agenda/usb_backup_transport.md — that item is offline sneakernet
  (git bundles on USB drives, no live link); this item is a LIVE
  link over a cable. Kent's constraint 2026-07-22: "the same
  algorithms (not another storage method) … whether phones are
  connected by wifi, github, or USB cable."
- **Vision / done-criteria:** at a venue with unusable wifi, a phone
  plugged into a computer by USB cable pairs and syncs baf-style
  projects using the EXISTING LAN machinery (listener, pairing,
  FF-guard, merge, coverage) with no separate storage path; a
  one-page workshop procedure exists.
- **Deadline:** ASAP (workshop in progress)
- **Waiting on:** Nothing

## Plans

### The design answer: USB tethering = IP link → zero new algorithms

Android **USB tethering** (Settings → Hotspot & tethering → USB
tethering; appears when a cable is plugged; no root, no developer
mode, no adb) turns the cable into a network interface: the phone
becomes a tiny router, the computer gets a DHCP address on a
private subnet over usb0/RNDIS/NCM. That is exactly "a data
connection between servers": the daemon's LAN listener already
binds 0.0.0.0, so every existing algorithm — TLS listener,
fingerprint pinning, pairing, share allowlist, FF-guard, LIFT
merge, coverage accounting — runs over the cable UNCHANGED. The
"USB drive" framing (MTP/mass storage) is rejected: MTP can't see
the server APK's private filesDir, is programmatically unreliable,
and would be a second storage method — precisely what Kent ruled
out. (Bundle-on-a-drive remains the separate sneakernet item.)

### Phase 0 — drill with zero code (do first, ~30 min with devices)

1. Phone: server APK running, LAN sync ON, wifi OFF or on —
   irrelevant; mobile data OFF (so the computer can't accidentally
   route internet through the phone's data plan).
2. Plug cable; enable USB tethering on the phone.
3. Computer: confirm the new interface + address
   (`ip addr` — look for usb0/enx…/192.168.x.x on Linux; Windows
   gets an "Ethernet" adapter — see Windows caveat below).
4. Watch both daemon logs: does mDNS cross the tether link?
   (Android's NsdManager + MulticastLock are wifi-centric; expect
   NO.) If not, pair/dial via the machinery that already exists for
   exactly this: QR pairing + `static_endpoints` (the hotspot-host
   fallback). The computer dials the phone's tether-subnet address
   directly.
5. Share a project, commit, watch `[lan-push]`/`[lan-listener]`
   traffic over the cable.

### Phase 1 — the one known code gap (bounded, likely small)

QR/static-endpoint advertising picks ONE address via
`_outward_ip_guess()` (default-route interface first) — on a
tethering phone the default route is wifi/mobile-data, so the QR
would advertise the WRONG address for the cable link. This is the
already-tracked "advertise all addresses in the QR" gap
(local_lan_sync_stub.md § Pairing; code comment in
lan_listener._outward_ip_guess). Fix = advertise all candidate
interface IPv4s (QR payload + hello endpoint announce), receiver
tries each. Possible second gap: the phone-side listener re-bind /
announce on tether-interface appearance (plug event) — verify in
the drill before building anything.

**SHIPPED 0.54.35 (QR side) + 0.54.34 (plug event).** Confirmed the
QR was still single-address (the fix had never landed). Now
`_h_lan_pair_qr` emits an `endpoints` list = every local IPv4
(`bound_endpoints_all()`) × bound port; `_h_lan_pair_accept` records
them all and reverse-hellos each until one connects; `endpoint` stays
for pre-0.54.35 scanners (back-compat, no floor bump). The plug-event
re-bind/announce is covered by 0.54.34's link-up nudge (restart_browse
+ burst on interface change). Live QR refresh SHIPPED 0.54.36:
`share_pairing_qr_popup` re-fetches `lan_pair_qr` every 4 s while open
and re-renders when the endpoint set changes, so a QR displayed BEFORE
plugging in picks up `usb0` on its own. **Phase 1 is now closed** for
the mDNS-working path; the only residual is the no-mDNS OEM corner
(some tether stacks filter multicast — untested), where the
multi-address QR + static_endpoints fallback carry pairing.

### Phase 2 — ops

One-page bilingual procedure for the workshop: cable, tethering
toggle, pair, the "mobile data off" rule, and what the sync-status
letters look like over cable. Windows caveat: Win10/11 support USB
tethering natively, but Microsoft removed RNDIS from Win11 24H2 —
modern Android (NCM) is fine; OLD phones + new Windows may not
enumerate. Test the actual workshop hardware in Phase 0.

### Explicitly rejected

- MTP / mass-storage "USB drive" mode (second storage method; can't
  reach app-private storage; flaky APIs).
- adb forward/reverse as the primary path (needs developer mode +
  adb on every field computer + per-phone authorization prompts;
  keep as fallback knowledge only).

## Notes

### Phase 0 result 2026-07-22: WORKS, zero code, better than designed

Drill (karlap ↔ "itservices-hue — Audio Words Collect 3", USB
tethering, phone at 10.143.126.7, karlap at 10.143.126.171):

- **mDNS CROSSES the tether link.** Both sides discovered each other
  over the cable (`add '95223b00' → 10.143.126.7:33291`; karlap's
  own announce visible at 10.143.126.171). The static-endpoints
  fallback — and the multi-address QR fix — were NOT needed for
  this pairing: discovery + both-direction dialing work as on wifi.
- End-to-end: `dialing '95223b00' at 10.143.126.7:33291 for 'nml'`
  → peek → no-op-at-head → `1/1 delivered`, coverage recorded.
  Peek round-trip 28 ms (venue wifi: seconds-to-timeouts).
- Ping RTT over cable 0.4 ms; inbound to the phone NOT firewalled
  by Android tethering (RST, not filtered, on a closed port).
- Prereqs verified: user-facing tethering toggle only (no root, no
  adb, no developer mode); phone listener binds all interfaces ✓.
- Papercuts observed: (a) listener port drifts across daemon
  restarts (38141 → 33291) — irrelevant when mDNS works, matters
  only for manual dialing; (b) sweep spends 10 s+ per stale wifi
  peer before reaching the cable peer (audit F2); (c) each side's
  UI shows only ONE address (the outward-guess), which confused
  the drill — cosmetic now that discovery self-solves.

### Real-load pass: CONFIRMED 2026-07-22 morning (production data)

The karlap log shows the full production cycle ran over the cable
repeatedly while the crew recorded: 09:23 three-way LIFT merge
(local 90923021 vs peer 27973030) → `pushed merged 'nml'` in ~50 s;
09:56 the phone arrived with NEW recordings (2a232444) → karlap
auto-committed pending edits, fetched, merged, pushed back in
~63 s; subsequent sweeps no-op at the advancing shared head. That
is bidirectional real data (phone's recordings → karlap via the
merge path's fetch; merged results → phone via push), unattended,
at USB latency. Cable transport is FIELD-COMPLETE for the
share-and-sync case.

### Remaining before "workshop-ready"
2. Phase 2 ops one-pager (FR+EN): cable, tethering toggle, mobile
   data OFF, pair by QR once, what LANOK looks like.
3. Comfort code (can ride normal cadence): F2 endpoint ordering so
   cable peers don't queue behind dead wifi addresses; multi-
   address display/QR remains desirable for the no-mDNS corner
   (some OEM tether stacks may filter multicast — only Xiaomi/moto
   class tested today).

### SHIPPED 0.54.34 — nudge on link-up ("plug in and go")
The watcher now fingerprints network interfaces each tick
(`scheduler._net_signature`: `/sys/class/net` + default-route IP) and,
on a change while LAN sync is on, calls `lan_discovery.restart_browse()`
+ `lan_burst.start_burst()` — so a phone plugged in with USB tethering
is discovered + synced with no gesture (the zeroconf browser, started
at boot before usb0 existed, wasn't browsing it; this re-arms it). Poll
capped at 15 s while LAN-on so link-up is caught within ~15 s. Closes
the "who re-scans when the cable appears" gap; F2 ordering + multi-
address QR still open for the no-mDNS OEM corner.

2026-07-22: venue LAN "horrible" — trigger for the reprioritization.
github WAN still works when any device gets a data connection, so
the convergence safety net stays github per invariant #10; the
cable is an opportunistic LAN link like any other.

## Research
