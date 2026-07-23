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

### BUG (field, user stuck): share-offer popup Accept AND Decline appear dead

Field report 2026-07-23 (Kent, recorder peer): the "Receive a
project" decision popup's clone-offer Accept button "doesn't seem
to do anything, nor decline, so I'm just stuck there."
`auto_dismiss=False`, so the user has no exit.

Smoking gun — both button paths run a **blocking daemon RPC on
the Kivy main thread, and only dismiss the popup after the RPC
returns** (`ui/decisions.py::_open_share_offer_popup`, `_accept`
/ `_decline`, ~lines 273–295):

- `_accept` → `lan_accept_offer` (`__init__.py` ~1238) is
  synchronous end-to-end: `POST /v1/lan/accept_offer` performs
  the whole LAN clone daemon-side before responding. On a big
  project (audio!) or a stalled transfer, that's seconds-to-
  minutes of frozen UI with zero feedback — the tap looks like
  a no-op, the popup stays, and every later tap (including
  Decline) queues behind the wedged main thread. This composes
  with the confirmed hold-`project_lock`-across-network-I/O
  regression (agenda/daemon_lock_across_network_io.md): a
  daemon wedged on network I/O under the lock blocks even the
  quick `lan_decline_offer` round-trip indefinitely.
- Neither helper can raise (both swallow `ServerUnavailable`),
  so this is NOT an exception path — it's pure blocking. A
  Decline tap that *does* execute always dismisses; a Decline
  that visibly does nothing means the handler never ran =
  main thread already parked.

Same shape in the picker-side `ui/lan_popups.py::
pending_offers_popup` (`_accept`/`_decline`, ~1786–1809) and
`adopt_origin_popup._resolve` (~1704) — all synchronous RPCs on
the main thread.

Asks:
1. `_accept`: dismiss (or swap to a "Receiving…" progress state)
   immediately, run `lan_accept_offer` on a worker, marshal the
   Result back via `Clock`. Same for the other decision popups'
   RPCs — decline/adopt are quick *when the daemon is healthy*,
   but nothing on the main thread should wait on the daemon.
   (Peers already follow this rule for their own RPCs per § 17c
   Rule 7 — the recorder's status poll runs on a worker.)
2. Make `/v1/lan/accept_offer` job-based (return job id, peer
   polls) like commit_project, so no HTTP/provider call carries
   a whole clone — and have the job expose **live progress**
   (received bytes / total if the transport knows it, else a
   coarse phase: connecting → receiving → unpacking → done) so
   the popup's "Receiving…" state can show real movement. A
   static label over a minutes-long field transfer reads as
   frozen — which is exactly the report that opened this note.
   Progress belongs in the shared popup (decisions.py), so every
   peer gets it for free.
3. Hardening: `_make_popup` never binds `on_dismiss` →
   `_on_popup_dismiss`; any dismissal that bypasses the button
   handlers leaves `showing_id` set and no future decision ever
   surfaces until app restart. Bind the cleanup on the popup
   itself.

Unstick workaround given to the user meanwhile: force-stop and
relaunch the peer (decision persists daemon-side and re-surfaces
via `lan_pending`); if it recurs immediately, restart the server
APK too — that's the wedged-daemon signature.

Owner: peer team (recorder). Evidence inline above; ping for a
repro session if needed — two paired phones on the field router.

### INVESTIGATE: merge churn from bulk ASR-draft annotations

The desktop AZT app is gaining a **bulk ASR** stage that writes machine-
transcription drafts into LIFT as `<annotation>` children of the
`<form lang="{analang}-x-audio">` node (the form that already holds the recorded
audio filename) — one annotation per ASR model/lane:

```xml
<form lang="gnd-x-audio">
  <text>dive.wav</text>
  <annotation name="allosaurus"     value="dìve"/>
  <annotation name="whisper-base"   value="dive"/>
  <annotation name="tone-katyayego" value="HL"/>
  <annotation name="md5"            value="<md5 of dive.wav>"/>
</form>
```

At project scale this is ~1700 entries × up to ~8 models = 10k+ small annotation
nodes, written incrementally during a long batch and rewritten whenever audio is
re-recorded (md5 mismatch wipes + rewrites that form's ASR annotations).

Check against the merge path:
1. Sub-element diff granularity — do per-annotation additions merge cleanly, or
   does a touched `<form>`/`<entry>` merge as a blob (spurious conflicts when two
   machines draft different models)?
2. Bulk runs change many entries per session — does the desktop `maybewrite`
   batching cadence interact badly with the daemon's change detection / lock window?
3. md5-mismatch wipe-and-rewrite deletes annotation nodes — confirm deletions
   merge sanely and don't resurrect stale drafts.
4. Worth marking ASR-draft annotations as machine-generated so merge policy can
   treat them as regenerable (prefer-latest / drop-on-conflict) vs hand-merge?

Owner: desktop ASR-split work. Resolve before bulk ASR ships to multi-machine
collab projects.
