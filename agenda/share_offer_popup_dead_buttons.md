# Share-offer popup: Accept/Decline appear dead, user stuck

- **Scope & relationships:** azt-collab/client-ui (`azt_collab_client/ui/decisions.py`, `ui/lan_popups.py`) — decision popups run blocking daemon RPCs on the Kivy main thread and only dismiss after the RPC returns. Composes with (but is distinct from) the daemon hold-`project_lock`-across-network-I/O regression (→ daemon_lock_across_network_io.md). Surfaced on the recorder peer.
- **Vision / done-criteria:** tapping Accept gives immediate feedback AND live progress (job-based accept_offer exposing bytes-or-phase, rendered in the shared popup) with the clone on a worker; Decline always lands even when the daemon is wedged; a dismissed popup can never strand `showing_id`. Verified by re-running the field scenario (offer → accept on a big project over the field router).
- **Deadline:** none
- **Waiting on:** Nothing (the awaited action shipped 2026-07-24 as 0.54.52–0.54.57 — see Notes)

## Plans

Peer-side: nothing to edit (all cited code is client-owned). Verify the fix on a device when it ships via the symlink.

## Notes

- Field report 2026-07-23 (Kent): clone-offer Accept does nothing, Decline does nothing, stuck on the popup (`auto_dismiss=False`).
- Same day, later: Kent reports it is NOT wedging anymore (client 0.54.36, popup code unchanged) — intermittent, manifests only when the RPC is slow. Likely the wedged-daemon leg cleared (lock-regression fix or just not triggered). The structural main-thread fix + progress ask stand as filed.
- Mechanism: `lan_accept_offer` is synchronous end-to-end (the POST carries the whole LAN clone); called on the main thread, so the UI freezes with zero feedback and later taps (incl. Decline) queue behind it. Neither RPC can raise, so a Decline that visibly does nothing means the handler never ran — main thread already parked.
- Workaround: force-stop + relaunch the peer (decision persists daemon-side, re-surfaces via `lan_pending`); if it recurs immediately, restart the server APK too (wedged-daemon signature).

- 2026-07-24: the awaited daemon/client action landed (0.54.52–.57):
  the auto-popup that stranded the user is RETIRED (decisions watcher
  skips share_offer, 0.54.54); offers surface passively on the peer
  screens; the new `_offer_confirm_popup` runs accept on a worker
  thread with live `clone_progress` + real failure text (0.54.53);
  peer-absent accepts get a typed "kept — ask again when nearby"
  (0.54.52). **Residual before closing:** (a) the manual
  "Receive a project" path (`pending_offers_popup._accept`) still
  calls `lan_accept_offer` synchronously on the Kivy main thread —
  same wedge shape if used; thread it like the confirm popup. (b) the
  other decision popups (pair-request / adopt-origin / conflict) also
  run RPCs on the main thread. (c) field re-verify per done-criteria.
- 2026-07-24 (0.54.65): same disease found + fixed in the Nearby
  "Pair" button — `_on_pair` ran the pair-request send synchronously
  on the main thread (screen froze; "button won't push"). Threaded.
  ALSO: decision watcher now installed in the daemon's own UI apps
  (picker_app + CollabUIApp) — inbound pair requests previously
  surfaced nowhere unless the recorder was open. Residuals (a)
  `pending_offers_popup._accept` and (b) decisions.py per-kind popup
  RPCs remain synchronous on the main thread.

## Research
