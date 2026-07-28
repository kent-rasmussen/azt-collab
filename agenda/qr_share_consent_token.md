# QR share consent: a token in the QR, not a display window

- **Scope & relationships:** azt-collab daemon (QR payload, hello
  handler, grant storage) + the shared picker UI. Supersedes the
  "valid while displayed" keepalive from 0.52.26. Related:
  azt-collab/agenda/lan_field_robustness_audit.md (field failures that
  presented as "unreachable").
- **Vision / done-criteria:** generating a project's share QR IS the
  act of willingness. A device that scans it gets the share whenever
  its hello arrives — not only inside a 30-second window — and no user
  ever has to know the rule "keep the QR on screen."
- **Deadline:** 2026-08-06
- **Waiting on:** Nothing

## Why (Kent, 2026-07-27)

*"Can we make a QR code for a project we aren't willing to share? That
shouldn't be."* Correct. Today the grant is gated on
`qr_offer_active(langcode)` — a heartbeat with a ~30 s window, kept
alive only while the QR screen is displayed. So consent depends on
TIMING rather than on the QR, and a scan sixteen seconds late is
refused silently.

The gate exists for a real reason: the hello handler trusts identity
claimed in the body (TLS is deliberately `CERT_NONE`), so without a
gate any device on the LAN could POST a hello claiming langcode `nml`
and grant itself read access — project names are guessable. The
display window is the current proof that a human is actively offering.
0.52.26 introduced it, itself replacing something worse (single-use +
10-minute timer).

## Plan

**Put a random token in the QR and have the hello echo it.**

- Owner, on drawing the QR for project X: store `{token → langcode}`
  durably (`$AZT_HOME`), generous expiry (hours, not seconds),
  multi-use — the workshop case wants one QR scanned by several
  phones.
- Scanner: carry the token through `lan_pair_accept` into the hello.
- Owner's hello handler: token matches a stored grant → `add_shared_project`
  regardless of whether the QR is still on screen.
- Keep the display-window path as fallback for pre-change scanners
  (an old build sends no token), and keep refusing when neither
  applies.

Security property preserved: a LAN attacker guessing `nml` has no
token. Fragility removed: the grant no longer depends on when the
hello lands.

**Consequences to accept explicitly:**
- Protocol change — both ends need the new build to benefit.
- The grant survives the QR screen closing. That is the point, but it
  IS a widening of what a QR authorizes, so it wants Kent's explicit
  sign-off (given 2026-07-27, in principle, for later
  implementation).

## Notes

Distinct from the 0.55.3 work, which only REPORTS a refusal
(`LAN_SHARE_QR_EXPIRED`) rather than preventing it. That reporting
stays useful for the fallback path and for pre-change peers.

Also note while working here: the listener's ACL is **union of all
paired peers' `shared_projects`**, not per-peer (see
`lan_listener.open_repository` — "Future-harden with signed-message
body auth to gate per-peer rather than union-of-all-peers"). So a
project shared with ANY paired peer is fetchable by EVERY paired peer.
Worth deciding whether per-peer gating lands with this change or
separately.
