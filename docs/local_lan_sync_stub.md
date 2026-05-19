# Local LAN / device-to-device sync — design stub

**Status:** parked. Captured 2026-05-19. Not yet started.

## Problem

Two field linguists in the same office each have a phone running
the suite, and want to share each other's commits **without
either device being able to reach github.com**. Today the only
sync path is github (or some other configured remote); when the
internet is down or restricted, both devices are isolated even
though they're a metre apart.

## Shape of the answer

The current architecture already supports this with minimal
restructuring — each device's daemon owns a dulwich-backed git
repo and already speaks git's HTTP smart-protocol via dulwich's
porcelain. The unanswered work is **discovery + auth + an
exposed HTTP listener on Android**, not the sync semantics
themselves.

Phased plan:

1. **Transport.** Wi-Fi LAN, not Bluetooth. Bluetooth Classic SPP
   (~2 Mbps practical) and BLE (~125 kbps) are both too slow for
   a real LIFT pack. Wi-Fi LAN (same access point) covers the
   typical office case; Wi-Fi Direct as a fallback when no AP
   exists. Both expose a TCP socket on the device — Android 14+
   may need the `:provider` sticky service to host the listener,
   possibly with a foreground notification depending on idle-stop
   policy.

2. **Discovery.** mDNS service advertisement,
   `_aztcollab._tcp.local`, instance name = device_name. Peers
   browse the service to enumerate sync candidates. (mDNS works
   on Wi-Fi LAN unconditionally; on Wi-Fi Direct it works after
   the group is formed.)

3. **Auth / pairing.** Re-use the existing QR-share UI vocabulary
   (`segno`-generated QR codes on the daemon UI, `zxing` scan on
   the picker). Pairing exchange = symmetric key plus a pinned
   server-cert fingerprint. Once paired, the peer's daemon is a
   stored git remote; sync proceeds via the existing
   fetch/merge/push path with the LAN URL substituted for the
   github URL.

4. **Conflict resolution.** None new. The LIFT-aware merge
   (`azt_collabd/lift_merge.py`) handles divergent histories
   identically whether the remote was github or another device's
   daemon. Same `<annotation name="azt-lift-conflict" …>` shape.

5. **Server-side listener.** Add a new transport binding to the
   daemon's HTTP server alongside the existing loopback + Android
   ContentProvider. Bind on the device's LAN interface, gated on
   the pairing flow's symmetric key. mDNS advertises only when
   the daemon's "Allow LAN sync" toggle is on.

## What's still open

- Exposed HTTP listener vs. Android's foreground-service rules
  on 14+ — does the existing sticky-bound service shape suffice,
  or do we need a user-facing notification?
- Pairing UX shape — single-direction QR scan (one device shows,
  the other scans) vs. mutual exchange.
- Should LAN sync coexist with github sync (push to both on every
  sync gesture)? Or be an explicit mode (e.g. "Work offline"
  pivots to "Sync via LAN")?
- Conflict-of-time-ordering: if peer A and peer B both sync to
  peer C in the same office, which one wins on a divergence?
  Existing per-entry merge handles it, but the user-visible
  "what just happened" narrative needs design.

## Touchpoints when implementing

- `azt_collabd/repo.py` — new remote shape; no changes to
  `_push_step_locked` / `_pull_step_locked` if the URL substitution
  is clean.
- `azt_collabd/server.py` — new HTTP listener (or extend the
  existing one), new pairing endpoint.
- `azt_collab_client/ui/share.py` / picker scan path — new
  pairing QR variant.
- `azt_collab_client/transports/` — possibly a new
  device-to-device transport class.
- Android manifest — new `<service>` for the LAN listener?
  Possibly fold into existing `:provider` service.

## Why not now

The github-mediated sync path is what we're stabilising in 0.43.x
(non-FF reconciliation, DoH fallback, stale-unpack remediation).
LAN sync expands the matrix substantially and would push out the
push-actually-works milestone. Park here so the idea isn't lost
when github sync is solid.
