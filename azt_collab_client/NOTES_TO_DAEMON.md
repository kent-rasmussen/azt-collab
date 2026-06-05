# Notes to the daemon

**Live queue only.** Outstanding items peers have noticed and want
the `azt_collabd` / server-APK side to fix. Filed here (inside
`azt_collab_client/`) rather than the per-peer CHANGELOG so:

- the symlink propagates them into every sister app's tree
- the daemon team sees them in one canonical place
- the note moves with the package if the canonical home ever
  changes

**When you act on an item, delete it from this file** — the
CHANGELOG is the historical record. This file holds only the
queue.

**Standing rules don't belong here.** Promote them to
`CLAUDE.md` (architecture/rationale) or `CLIENT_INTEGRATION.md`
(peer-facing contract). Shipped fixes live in the relevant
`CHANGELOG`. Anything left here is a live queue item.

---

## Live queue

### Is `lan_peer_id()` expected to be reliably non-empty now?

`CLIENT_INTEGRATION.md` § 21 Locked semantic #2 (2026-05-28)
makes `peer_id` canonical and `device_name` display-only; we
took that as licence in recorder 1.50.2 to drop the
`device_name` fallback inside `_populate_split_state_apply`
(it had been added in 1.49.1 specifically because
`lan_peer_id()` returned `''` on some builds where the
daemon's LAN identity wasn't initialised — cryptography stack
missing, LAN sync never enabled, etc.).

Field reports since then show the `[k/n]` slot indicator
disappearing from the recorder's top bar on a subset of paired
phones, which matches the original 1.49.1 symptom: `claim_slot`
succeeds (so the slot is in `list_slots` with whatever identity
the daemon had at the time), but `lan_peer_id()` from those
same phones returns `''`, so the peer's `peer_id`-only match
fails. The peer can no longer tell which slot it holds, the
filter range is dropped, and the picker fires.

Could you confirm whether the daemon now guarantees
`lan_peer_id()` returns a non-empty value on every peer that
has ever successfully completed `claim_slot`, and if so what
release that landed in? If the guarantee already exists in a
recent release, we'll chase the field instances as out-of-date
peers. If the guarantee isn't yet in place, what does the
daemon team recommend the peer code do in the meantime —
restore the `device_name` fallback as a documented bridge,
surface a "device identity unavailable" error to the user, or
something else?

Happy to file as `CLIENT_INTEGRATION.md` clarification once
the recommendation is decided.

