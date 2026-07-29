# Drive another device's settings UI over the LAN

- **Scope & relationships:** `azt-collab` — `lan_listener` (identity),
  `peers.json` (grant), `azt_collab_client/transports/` (new transport),
  `azt_collabd/ui/app.py` (launch + peer row). Supersedes most of
  [[pull_diagnostics_over_peer_link]]: once every RPC works remotely,
  `get_daemon_log` / `get_daemon_log_files` return the peer's logs
  straight to the operator's screen, and the tar.gz + Signal + email
  apparatus stops being the only path back.
- **Vision / done-criteria:** Kent can fix a setting or read a log on the
  Ndemli desktop from his own laptop, over the LAN, **without anyone
  there stopping work or opening a window they don't understand**. Two UI
  windows side by side — one local, one remote — each unmistakably
  labelled. The grant is given once, physically, at setup.
- **Deadline:** none
- **Waiting on:** Nothing.

## Design (settled 2026-07-29 in conversation)

### Why not what I first proposed

I suggested forwarding a *whitelist* of config RPCs through a new
endpoint. Kent: *"if it's all the same, I'd rather have the same
functions, just to keep it simple."* He's right, and it's less work: the
client already routes everything through `rpc.call()` →
`pick_transport()`, and the contract invites this — *"Add new transports
by implementing the `Transport` ABC and slotting into
`pick_transport()`."* **One transport ⇒ every RPC works, existing UI
unmodified.** No whitelist, no per-endpoint forwarding, no new screens.

I also suggested retargeting one UI with a "back to this computer"
action. Kent: *"I'm not excited about this; I'm likely to get confused at
some point. Can we not start another ui?"* Correct, and simpler: target
fixed at process launch, never changes — no reset, no mode to lose track
of, nothing to forget you're in.

And I suggested a **time-boxed** grant. Kent: *"I don't want this
time-bound. The very problem we have is asking people to stop what
they're doing to change settings in a window they don't understand. If I
have to ask them to turn this on, it's useless."* The grant is persistent,
set once at setup while he is holding the machine. That physical presence
IS the gesture; there is no later interaction.

### The prerequisite: identity is currently forgeable

**Checked, and it is weaker than I claimed in conversation.** From
`lan_listener.py`:

```python
"""Body-auth variant … peer_id from body, no cert cross-check."""
    peer_id = str(payload.get('peer_id', '') or '')
    if len(peer_id) != 64:
```

No signature, no nonce, no challenge. The caller states a 64-hex peer_id
in the request body; the daemon length-checks it and looks it up in
`peers.json`. Possession of the **private key is never demonstrated** —
and peer_id is not a secret, since it is how peers find each other over
mDNS. Effective access control today: *be on the same LAN and know a
paired fingerprint.*

For opportunistic data sync that is a considered tradeoff, and it is
bounded — the FF check and the merge guards stand between a hostile
neighbour and damage. **Writing settings on someone's machine is a
different class**, so admin must not ride on it.

Both sides already hold the ed25519 keypair and each other's cert
(`$AZT_HOME/peer_id`, `peer.crt`). What is missing is only the exchange:
server issues a nonce → caller signs it → server verifies against the
recorded cert. **This hardens the existing data sync too, so it is worth
doing on its own merits even if the rest is never built.**

### Phases

1. **Nonce-signed identity.** Challenge/response using the existing
   keys. Gate: no phase below ships without this.
2. **Per-peer `admin` flag** in `peers.json`, default off, plus a toggle
   in the peer row ("allow this device to change my settings"). This is
   the one piece of UI that MUST exist — it is what Kent taps in Ndemli.
   Visible on the granting machine, which is correct; a peer can revoke
   it, which is their right.
3. **LAN `Transport` subclass** — speaks to a peer's listener instead of
   loopback. Refuses to construct unless the peer granted admin.
4. **`python -m azt_collabd ui --peer <id>`** — separate process,
   fixed target, **device name in the window title**. Without a permanent
   unmissable indicator of which machine you are driving, you will
   eventually toggle `work_offline` on the wrong daemon.
5. **Launch button in the peer row** (Kent: *"let's do the button anyway;
   I'll probably use it"*) — sits above "get diagnostics for this
   device", spawns the second UI. Rendered **only where it would work**:
   the per-peer LAN poll (already running every 5 s) carries a
   `grants_admin` bit, so the affordance is invisible for peers who have
   not granted. Answers *"I don't want 'manage this device' visible to
   most people."*

### Known consequences of "every RPC works"

- **Anything path-shaped is the remote machine's path.** A flow that
  opens a file browser browses the *operator's* disk. Confusing, not
  dangerous.
- **Credential entry lands on the remote machine** — a feature: fixing a
  bad GitHub token remotely is exactly the kind of thing this is for.
- **`/v1/admin/restart` would restart *their* daemon.** Useful for
  support. Open question: refuse it through the remote transport, or
  allow with a confirm.
- **The picker would list *their* projects.** Right for support,
  startling the first time.

## Plans

Order is the phase list above. 1 before everything.

## Notes

Started 2026-07-29 as top priority, immediately after the 0.55.100
zeroconf-cancel fix.

## Research
