# Share-offer popup: Accept/Decline appear dead, user stuck

- **Scope & relationships:** azt-collab/client-ui (`azt_collab_client/ui/decisions.py`, `ui/lan_popups.py`) — decision popups run blocking daemon RPCs on the Kivy main thread and only dismiss after the RPC returns. Composes with (but is distinct from) the daemon hold-`project_lock`-across-network-I/O regression (→ daemon_lock_across_network_io.md). Surfaced on the recorder peer.
- **Vision / done-criteria:** tapping Accept gives immediate feedback AND live progress (job-based accept_offer exposing bytes-or-phase, rendered in the shared popup) with the clone on a worker; Decline always lands even when the daemon is wedged; a dismissed popup can never strand `showing_id`. Verified by re-running the field scenario (offer → accept on a big project over the field router).
- **Deadline:** none
- **Waiting on:** daemon team acting on the NOTES_TO_DAEMON.md entry "BUG (field, user stuck): share-offer popup Accept AND Decline appear dead" (filed 2026-07-23, evidence inline)

## Plans

Peer-side: nothing to edit (all cited code is client-owned). Verify the fix on a device when it ships via the symlink.

## Notes

- Field report 2026-07-23 (Kent): clone-offer Accept does nothing, Decline does nothing, stuck on the popup (`auto_dismiss=False`).
- Same day, later: Kent reports it is NOT wedging anymore (client 0.54.36, popup code unchanged) — intermittent, manifests only when the RPC is slow. Likely the wedged-daemon leg cleared (lock-regression fix or just not triggered). The structural main-thread fix + progress ask stand as filed.
- Mechanism: `lan_accept_offer` is synchronous end-to-end (the POST carries the whole LAN clone); called on the main thread, so the UI freezes with zero feedback and later taps (incl. Decline) queue behind it. Neither RPC can raise, so a Decline that visibly does nothing means the handler never ran — main thread already parked.
- Workaround: force-stop + relaunch the peer (decision persists daemon-side, re-surfaces via `lan_pending`); if it recurs immediately, restart the server APK too (wedged-daemon signature).

## Research
