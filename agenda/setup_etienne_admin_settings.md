# Set up Etienne on admin settings

- **Scope & relationships:** `azt-collab` — the *field-setup* half of
  [[remote_settings_over_lan]] (done 2026-07-30, phases 1–5 shipped in
  0.55.150s). That item built the mechanism; this one performs the
  one-time physical grant on Etienne's machine and proves it works
  end to end. By design the grant is **persistent and given in person**
  — Kent holds the machine, taps "allow this device to change my
  settings" in the peer row, and after that never has to ask Etienne to
  open a window he doesn't understand. That physical presence IS the
  gesture, which is exactly why this item cannot proceed without him.
- **Vision / done-criteria:** From his own laptop, on the same LAN, Kent
  can open a second settings window titled with Etienne's device name,
  read that daemon's log, and change one of its settings — with Etienne
  doing nothing. Confirmed by actually changing something innocuous and
  seeing it take on Etienne's machine, not Kent's.
- **Deadline:** none
- **Waiting on:** Etienne's availability (must be in person, with his
  device, on the same LAN).

## Plans

Prerequisites to have ready *before* he's available, so the session
itself is short:

1. Both machines on the same LAN, both daemons ≥ the version that
   carries the admin grant + nonce-signed identity (0.55.164).
2. Devices already **paired** — admin is a flag on an existing
   `peers.json` entry, not a substitute for pairing.
3. Etienne's device has a recognisable **device name** set; it is what
   labels the remote window, and an unlabelled second UI is the one way
   this feature bites you.

Session steps:

4. On Etienne's machine: peer row for Kent's laptop → toggle "allow this
   device to change my settings".
5. On Kent's laptop: the launch button appears in Etienne's peer row
   (it renders only where `grants_admin` is true). Open it.
6. Verify the window title names Etienne's device; pull his daemon log
   through it; change one setting and confirm it landed on his machine.
7. Note whether the LAN-off admin door also works from his side (a
   device with LAN sync switched off is still supposed to be reachable
   to switch it back on).

## Notes

Added to the agenda 2026-07-31 as top priority, marked WAITING.

## Research
