# Update a daemon at a distance

- **Scope & relationships:** `azt-collab/lan-sync`. Split off
  `remote_settings_over_lan.md` on 2026-07-30 when that item closed: the admin
  channel is verified, but *updating* the machine you're administering is a
  separate capability with its own bootstrap problem. Rides on the same
  nonce-signed tunnel.
- **Vision / done-criteria:** from one seat, update a colleague's collab server
  and restart it, with both halves of the action landing on the same machine.
  Done when a remote update has been observed end to end: `UPDATED` from the
  remote daemon, its version strip showing the new number afterwards, and no
  ambiguity about which checkout was pulled.
- **Deadline:** none
- **Waiting on:** a free computer — one peer manually brought to ≥ 0.55.161 so
  it can serve `/v1/admin/update_self`. Idjop's desktop is in daily use and was
  left undisturbed at 0.55.156.

## Plans

Bootstrap is inherent and cannot be engineered away: the endpoint that makes
remote update possible is exactly what a too-old daemon is missing. So the
sequence per machine is always **one local update, then remote forever after**.

Verification steps once a machine is free:

1. Bring it to ≥ 0.55.161 at its own keyboard.
2. From another machine, retarget to it and press Update. Expect `UPDATED` (or
   `Up to date.`), never `TOO_OLD`.
3. Restart it from the same page, then read its version strip — the number must
   come from *its* daemon (the strip's server half does).
4. Confirm the administering machine's own checkout did **not** move.

Step 4 is the one that matters: the original defect was that Update pulled
locally while Restart went remote.

## Notes

**2026-07-30 — built, 0.55.161–164.**

- `POST /v1/admin/update_self` runs `self_update.git_pull_self()` inside the
  daemon and returns its codes (`UPDATED` / `UP_TO_DATE` / `NOT_A_CHECKOUT` /
  `NO_GIT` / `TIMEOUT` / `FAILED`). No strings; the caller translates.
- Client wrapper `update_self()` → `(code, detail)`, never raises.
- The desktop Update button no longer imports `self_update` in-process. It used
  to, which meant it pulled whichever checkout the *UI* was running from — while
  the `restart_server()` right after it went through `pick_transport()` and did
  honour the retarget. With the page reading `EDITING <their device>`, Update
  pulled the local machine and restarted the remote one.
- Falls back to the in-process pull **only when not retargeted**, so a new UI
  against an old local daemon still works (they drift routinely on desktop) —
  but never while the page claims to be editing someone else.
- A 404 from a too-old peer returns `TOO_OLD`, not `no daemon reachable`. The
  first wording sent the field diagnosis at the network over a working link.

**Open question, deliberately unresolved.** A colleague's daemon reached
0.55.156 during a remote session on 2026-07-30 and I cannot account for it.
`git_pull_self` has exactly two callers — the settings UI and the new endpoint —
so there is no boot-time self-update to explain it, and the pre-0.55.161 UI path
pulled locally. Either something else updated that machine, or the mechanism is
one I haven't found. Worth resolving before trusting remote update, because it
implies a code path that moves a checkout without either known caller.

## Research
