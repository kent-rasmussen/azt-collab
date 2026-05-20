# Local LAN / device-to-device sync — design spec

**Status:** parked. Spec drafted 2026-05-19 from research into
mDNS-on-Android, Android 14+ foreground-service rules, dulwich's
HTTP smart-protocol server, and offline-first peer-to-peer git
patterns (Syncthing, Radicle, git-annex, Dat). Not yet started.

## Problem

Two field linguists in the same office each have a phone running
the suite, and want to share each other's commits **without
either device being able to reach github.com**. Today the only
sync path is github (or some other configured remote); when the
internet is down or restricted, both devices are isolated even
though they're a metre apart.

## Architecture overview

Eight load-bearing decisions, in roughly the order an
implementation has to commit to them:

1. **Topology.** GitHub-authoritative star + opportunistic LAN
   fan-out. A LAN push is a hint, not a substitute — receiving
   peers still owe github a push and drain it on the next
   online window. No peer-graph state.
2. **Pairing.** Explicit per device pair, no transitive
   discovery (Alice does not auto-learn about Carol through Bob).
   One-way QR exchange: A shows, B scans, A's daemon
   auto-reverse-records B on B's first authenticated fetch.
3. **Identity.** Per-device ed25519 keypair generated on first
   daemon start where it's missing; stored at `$AZT_HOME/peer_id`.
   Separate from the suite signing-keystore fingerprint — pairing
   is per-device-pair, not per-suite-installed, so a stolen phone
   can be unpaired without re-keying the whole suite.
4. **Discovery.** Android `NsdManager` via pyjnius, called with
   `DiscoveryRequest.FLAG_SHOW_PICKER` so the system handles the
   device picker — dodges the Android 17 `ACCESS_LOCAL_NETWORK`
   runtime permission. `python-zeroconf` on desktop. Static-
   endpoint fallback (pairing-QR endpoint + manual IP) covers
   only the fixed-IP hotspot-host case; AP-isolated networks
   with DHCP churn are out of scope for v1 (see §Discovery →
   Hotspot / restricted-Wi-Fi: scope).
5. **Listener.** `dulwich.web.HTTPGitApplication` + `ThreadingMixIn`
   + a custom WSGI middleware for cert-derived peer-id auth, with
   TLS via `ssl.SSLContext.wrap_socket(srv.socket)`. Hosted inside
   the existing `:provider` process — same Python interpreter,
   same `azt_collabd` state, same per-project flock. Promoted to a
   foreground service of type `specialUse` while a daemon-wide
   "Allow LAN sync" toggle is on; demoted back to sticky-bound on
   toggle off.
6. **Auth.** LAN: pinned TLS cert. The handshake itself proves
   "this is peer-id X" (pubkey extracted from the verified cert).
   Loopback transport keeps its bearer token unchanged. Two
   middleware paths, one per transport.
7. **Project sharing.** Per-direction allowlist after pairing.
   A picks "Share project Y with paired phone B" from the daemon
   settings UI; A's `peers.json` records `peers[B].shared_projects
   += [Y]`. When B's daemon authenticates against A's listener,
   A advertises only the projects in `shared_projects` for B.
   B has its own peers.json with its own allowlist — sharing is
   symmetric in UX but stored per direction.
8. **Fan-out.** Every drain pass of the scheduler's
   `_drain_pending_push` pushes to github (when online) and to
   every reachable paired peer in the same pass. `lan-{peer_id}`
   remotes are in-memory only — registered via dulwich
   `remote_add` at the start of a sync attempt, removed after.
   Never persisted in `.git/config`.

The current architecture supports this with **minimal restructuring**.
Each device's daemon already owns a dulwich-backed git repo and
already speaks git's HTTP smart-protocol via dulwich's porcelain.
The unanswered work is **discovery + cert auth + a LAN-bound
listener on Android** — not the sync semantics themselves.

## Discovery

### Android — NsdManager via pyjnius

Service type `_aztcollab._tcp.local.`. Instance name = `device_name`
(daemon-owned, human-readable). TXT records:

| Key | Value | Why |
|---|---|---|
| `peer_id` | hex of ed25519 pubkey (64 chars) | Stable wire identifier |
| `fp` | sha256 cert fingerprint (64 chars) | TLS pin (out-of-band copy of cert) |
| `v` | protocol version int | Forward-compat |

Well under the 255-byte TXT cap. The cert fingerprint is
advertised so a paired peer can sanity-check before initiating
TLS — the QR-pinned fingerprint is the authoritative one, but
matching at discovery time catches "this is a different phone
claiming the same peer-id" early.

Call sequence (Android side, sketched):

```python
# pyjnius — runs on the :provider service's main thread (pre-warmed
# per the existing jnius helper-warming convention in server_apk/main.py).
NsdManager = autoclass("android.net.nsd.NsdManager")
NsdServiceInfo = autoclass("android.net.nsd.NsdServiceInfo")
DiscoveryRequest = autoclass("android.net.nsd.DiscoveryRequest$Builder")
# register: own listener service
info = NsdServiceInfo()
info.setServiceName(device_name)
info.setServiceType("_aztcollab._tcp.")
info.setPort(listener_port)
info.setAttribute("peer_id", peer_id_hex)
info.setAttribute("fp", cert_fp_hex)
info.setAttribute("v", "1")
nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, register_listener)
# discover: peers on the same LAN
req = DiscoveryRequest().setServiceType("_aztcollab._tcp.") \
                        .setDiscoveryMode(...).build()
nsd.discoverServices(req, FLAG_SHOW_PICKER, discover_listener)
```

`FLAG_SHOW_PICKER` is the load-bearing flag — without it, raw
mDNS sockets need the new `ACCESS_LOCAL_NETWORK` runtime
permission on Android 17+, which is exactly the kind of
store-review-explainable permission the suite is trying to avoid.
With the picker flag, the system presents a "choose a phone"
dialog when discovery starts, and the app only gets
`NsdServiceInfo` objects for the user's selection.

`WifiManager.MulticastLock` is **still required** on every
supported Android version when actively advertising or browsing.
Acquire on listener start, release on listener stop. Manifest
must declare `CHANGE_WIFI_MULTICAST_STATE` in the server APK
(peer apps don't need it — they never advertise themselves).

### Desktop — python-zeroconf

Pure-Python, no native binaries, works under the existing
desktop daemon as-is. Same service type, same TXT records.

### Hotspot / restricted-Wi-Fi: scope

mDNS silently fails in two field-relevant scenarios:

- **Phone-to-phone hotspot** — separate `wlan0`/`ap0`
  interface, multicast dropped between them.
- **Enterprise / captive Wi-Fi with AP isolation** —
  multicast blocked between clients.

There is no general app-level workaround. v1 supports **only**
the narrow case below; everything else is out of scope, and
the user falls back to github sync.

#### Supported: hotspot host with a fixed subnet IP

When one phone is the hotspot host, its IP on the hotspot
subnet is set by Android's hotspot stack (typically
`192.168.43.1`), not by DHCP. A pairing-QR endpoint captured
during a hotspot session stays valid indefinitely as long as
the *same* phone is always the host. This is the one durably
useful fallback.

Endpoint resolution order on every sync attempt:

1. **mDNS** — current IP this session (best, when not blocked).
2. **Static endpoints** — manual entries added via the
   settings UI.
3. **Pairing-QR endpoint** — recorded into `peers.json` at
   pair time; treated as the oldest, weakest hint.

mDNS `.local` hostname resolution through `getaddrinfo` is
unreliable on Android — do the lookup in-daemon and
substitute a raw IP into the dulwich URL.

#### Not supported in v1: AP-isolated networks with DHCP churn

The common "two phones on enterprise/captive Wi-Fi with AP
isolation" case is **not** rescued by static endpoints. The
QR endpoint is current for hours and stale by the next lease
cycle; the manual-IP UI degenerates into "look up the current
IP every morning, retype it," which no user will sustain past
day two.

The honest v1 contract: **mDNS works → LAN sync works; mDNS
blocked + DHCP churn → not supported, user falls back to
github sync**. Out-of-band "current endpoint" exchange would
need either the `ACCESS_LOCAL_NETWORK` permission this design
is avoiding, or an internet round-trip that defeats the
offline use case. Park.

## Pairing

### One-time QR exchange

Daemon settings UI on device A: "Pair a phone" button → shows a
QR encoding (segno generator already in 0.41.0):

```json
{
  "v": 1,
  "peer_id": "<hex ed25519 pubkey>",
  "fp": "<sha256 of A's TLS cert>",
  "endpoint": "192.168.1.42:8443",
  "device_name": "Alice's phone"
}
```

Picker on device B: "Scan to pair" button → reuses the existing
zxing-android-embedded scanner from 0.41.0. After scan, B
displays "Alice's phone wants to pair. Accept?" with the
device_name + the peer-id prefix (first 8 chars) visible for
verbal confirmation across the table.

On accept, B's daemon:

1. Generates B's own ed25519 keypair + TLS cert if missing.
2. Records A in `$AZT_HOME/peers.json`:
   `{peer_id, fp, device_name, endpoints: [from_qr], shared_projects: []}`.
3. Initiates a TLS handshake against A's endpoint, pinning A's
   fingerprint. Authenticates with B's own client cert.
4. Sends a `POST /v1/lan/hello {peer_id, fp, device_name}` so A
   learns B's identity from the first authenticated request.

A's daemon, on receiving the `hello`:

5. Verifies the handshake cert's fingerprint matches the body's
   `fp`. Records B in A's `peers.json` symmetrically. **This is
   the auto-reverse-record** — no separate QR scan in the other
   direction.

Both sides now have each other in their paired list. Neither
side has shared any project yet — that's a separate gesture.

### Cert generation

On first daemon start where `$AZT_HOME/peer_id` is missing:

1. Generate ed25519 keypair.
2. Generate a self-signed X.509 cert with the ed25519 pubkey as
   the subject pubkey and a 100-year validity (we're identity-
   pinning the fingerprint, not relying on CA validity).
3. Store key + cert as `$AZT_HOME/peer_id` (key) + `$AZT_HOME/peer.crt`.
4. Compute and cache the SHA-256 fingerprint.

No rotation path in v1. If a device is compromised, the recovery
is "wipe and re-pair" — same as Syncthing.

### Paired-peers list (`$AZT_HOME/peers.json`)

Daemon-owned, written atomically per the existing
`atomic_open_write` convention:

```json
{
  "peers": {
    "<peer_id_hex>": {
      "device_name": "Alice's phone",
      "fp": "<their sha256>",
      "endpoints": ["192.168.1.42:8443"],
      "shared_projects": ["fra", "tpi"],
      "paired_at": "2026-05-19T14:30:00Z",
      "last_seen_at": "2026-05-19T16:45:12Z"
    }
  }
}
```

`endpoints` is a static fallback list, populated from the QR and
extended via the manual-IP UI. mDNS discoveries are not persisted
here — they're a per-session in-memory cache.

## Listener

### Server seam

```python
from socketserver import ThreadingMixIn
from dulwich.web import HTTPGitApplication, make_wsgi_chain, HTTPGitServer
from dulwich.server import DictBackend
import ssl

def cert_peer_id_mw(app, peers_db):
    def wrapped(environ, start_response):
        cert = environ.get("SSL_CLIENT_CERT")  # injected by TLS layer
        peer_id = derive_peer_id_from_cert(cert)
        if peer_id not in peers_db.peers:
            start_response("403 Forbidden", [])
            return [b""]
        environ["aztcollab.peer_id"] = peer_id  # downstream uses for ACL
        return app(environ, start_response)
    return wrapped

backend = DictBackend({
    f"/{lang}.git".encode(): repo_for(lang)
    for lang in projects_shared_with_any_peer()
})
git_app = make_wsgi_chain(backend)   # GunzipFilter + LimitedInputFilter
app = cert_peer_id_mw(git_app, peers_db)

class ThreadingHTTPGitServer(ThreadingMixIn, HTTPGitServer):
    daemon_threads = True

srv = ThreadingHTTPGitServer(("0.0.0.0", 0), backend)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(cert_path, key_path)
ctx.verify_mode = ssl.CERT_REQUIRED          # require client cert
ctx.set_verify(ssl.VERIFY_PEER, accept_any_peer_cert)  # we pin per peer
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.RequestHandlerClass.application = app
srv.serve_forever()
```

Notes:

- `make_wsgi_chain` adds `GunzipFilter` (required for
  `git-receive-pack` — git clients gzip POST bodies) and
  `LimitedInputFilter` (enforces `Content-Length`).
- `DictBackend` is rebuilt whenever `peers.json` or
  `shared_projects` changes — `/{lang}.git` is exposed only for
  projects shared with at least one peer. Per-request, the
  middleware can further restrict the URL set to projects shared
  with the specific peer-id (otherwise a paired peer who isn't on
  the project's allowlist could still fetch it by guessing the URL).
- Threading is required: two paired phones in the same room can
  both fetch simultaneously. `wsgiref.simple_server.WSGIServer` is
  single-threaded by default.
- Concurrency on write (receive-pack) is serialized at the
  daemon entry by the existing per-project flock in
  `azt_collabd/locks.py` — dulwich's own ref locking is the
  backstop, not the primary guard, because the daemon may have
  other writers (the scheduler) hitting the same repo.

### Foreground service + power

Server APK `manifest_extras.xml` gains:

```xml
<service
    android:name="…AZTServiceProviderhost"
    android:exported="true"
    android:permission="org.atoznback.AZT_COLLAB_ACCESS"
    android:process=":provider"
    android:foregroundServiceType="specialUse">
  <property
      android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
      android:value="lan-peer-git-sync" />
</service>
```

Plus permissions:

- `FOREGROUND_SERVICE` (base)
- `FOREGROUND_SERVICE_SPECIAL_USE` (Android 14+)
- `CHANGE_WIFI_MULTICAST_STATE` (mDNS advertise + browse)
- `ACCESS_WIFI_STATE` (interface enumeration for the listener bind)

`INTERNET` is already declared; no new permission needed for the
`ServerSocket` itself.

State machine inside `azt_collabd.android_cp.service`:

- **Toggle OFF**: existing sticky-bound model, transient-when-idle.
- **Toggle ON**: call `startForeground(notification_id, build_notification())`,
  acquire `WifiManager.WIFI_MODE_FULL_HIGH_PERF` lock, start the
  listener thread + the NsdManager advertise/discover.
- **Toggle OFF again**: stop listener, release WifiLock, unregister
  NsdManager, `stopForeground()`. Process drops back to
  sticky-bound (idle-stop after 5 min still applies).

`dataSync` was considered and rejected — Android 15+ applies a
6-hour cumulative cap per 24h to `dataSync` services targeting
API 35+, which is a footgun for an always-on toggle.
`connectedDevice` is semantically peripheral-coupled (Bluetooth,
USB, CDM-paired devices). `specialUse` is the catch-all reviewed
case-by-case; the subtype string is the spec-compliant way to
declare intent. Note: Play Console may push back on this if the
suite ever distributes through Play; sideloaded today, but worth
tracking.

### Notification copy

While the toggle is on, the foreground notification reads:

> **AZT Collaboration: sharing with nearby devices**
> Tap to manage paired phones.

Tapping opens the daemon settings UI on the "Paired phones" page.
Long-press → app info, the standard "stop" gesture per Play policy.

### Cleartext NSC (peer side)

The listener serves HTTPS (cert-pinned), so no plaintext over LAN.
Peer apps fetching from the listener don't need to whitelist
cleartext in their `network_security_config.xml`. If for any
reason we drop TLS in a future revision, peer apps need an
RFC1918 cleartext block — but only on the **fetching** side; the
listener doesn't enforce NSC for its own bound socket.

## Topology and fan-out

`_drain_pending_push` in `azt_collabd.scheduler`'s `_watcher_loop`
is the existing seam. New iteration shape (sketch):

```python
def _drain_pending_push(project):
    targets = []
    if is_online_cached() and not work_offline:
        targets.append(("origin", github_url(project)))
    for peer in reachable_paired_peers(project):  # mDNS + endpoints
        targets.append((f"lan-{peer.peer_id}", lan_url(peer, project)))
    if not targets:
        return  # nothing reachable, retry later
    with project_lock(project):
        for name, url in targets:
            with in_memory_remote(project, name, url):
                porcelain.push(project.repo, name, refspec=...)
                # record success per target
```

Properties this gives us:

- **Cheap redundancy.** Git's ref-advertisement exchange gates
  the packfile transfer — pushing to a peer who already has the
  refs costs the protocol round-trip and ~zero bytes of data.
- **Partial transitive propagation.** Alice → Bob (LAN) → Carol
  (LAN, next time Bob syncs) without any gossip code or
  peer-graph state. The propagation graph lives implicitly in
  the per-device paired lists.
- **GitHub convergence.** Every push that succeeded on LAN still
  owes github. Per-target success is recorded so a transient
  github outage doesn't lose track of refs that landed on LAN.
- **Existing debounce.** Bursts of commits collapse into one
  drain pass — LAN peers get one fan-out per burst, not one
  per commit.

## Conflict semantics

No new merge code. `azt_collabd/lift_merge.py` handles divergent
histories identically regardless of which remote the divergence
came from. Same `<annotation name="azt-lift-conflict" …>` shape.

One wrinkle worth a manual smoke before shipping: LAN-mediated
merges run against the LAN peer's HEAD, not `origin/HEAD`. When
the result is later pushed to github, both the LAN-merged commit
and any github-only commits get merged again at the github side.
The LIFT merge must be **idempotent under replay** — already
believed true (the merge produces the same `<lift>` doc given the
same three inputs), but explicitly worth verifying in a manual
test with the two-phones-+-github-three-way scenario before this
ships.

## Lifetime and recovery

The `:provider` process can die under memory pressure mid-`receive-pack`.
Existing semantics already cover this:

- Android lazy-respawns the process on the next ContentResolver
  call (Android's unconditional ContentProvider contract).
- `Service.onCreate` re-runs `service.py`, which calls
  `scheduler.reconcile_on_startup()` and marks in-flight jobs as
  `JOB_INTERRUPTED` per the 0.43.0 status contract.
- Peer (the one pushing) sees an EOF / connection reset from
  dulwich's client side, retries on next drain pass — same code
  path as a transient github failure.

The LAN listener inherits this for free. No new recovery code.

## Touchpoints when implementing

| File | Change |
|---|---|
| `azt_collabd/peer_id.py` (new) | ed25519 keypair + cert generation; `peer_id_hex`, `cert_fp_hex`, atomic load/save |
| `azt_collabd/peers.py` (new) | `peers.json` read/write; `record_pair(peer_id, fp, device_name, endpoint)`; `set_shared_projects(peer_id, [lang, ...])` |
| `azt_collabd/lan_listener.py` (new) | dulwich.web wrap, cert middleware, threaded server, start/stop hooks |
| `azt_collabd/lan_discovery.py` (new) | NsdManager wrap (Android) or python-zeroconf (desktop); endpoint resolution |
| `azt_collabd/scheduler.py` | extend `_drain_pending_push` to fan-out across reachable LAN peers |
| `azt_collabd/repo.py` | helper for in-memory remote add/remove around a push attempt |
| `azt_collabd/server.py` | new endpoints: `/v1/lan/{toggle, pair_qr, hello, list_peers, share_project, unshare_project}` |
| `azt_collab_client/__init__.py` | thin wrappers around the new endpoints (`lan_toggle`, `lan_pair_qr`, `lan_share_project`, `lan_unshare_project`, `lan_list_peers`) |
| `azt_collab_client/status.py` + `azt_collabd/status.py` | new codes: `LAN_PAIRED`, `LAN_UNPAIRED`, `LAN_PEER_UNREACHABLE`, `LAN_FP_MISMATCH`, `LAN_TOGGLE_OFF` |
| `azt_collab_client/translate.py` | translations for the new codes |
| `azt_collab_client/ui/picker.py` | "Scan to pair" entry point; reuse zxing scanner |
| `azt_collabd/ui/app.py` | "Pair a phone" QR generator; "Paired phones" page; per-peer "share project" picker |
| `server_apk/manifest_extras.xml` (and `.tmpl`) | `foregroundServiceType="specialUse"`; new permissions |
| `server_apk/main.py` | jnius pre-warm for NsdManager + WifiManager (memory: jnius lazy-init from worker thread SEGVs) |
| `android/manifest_extras_peer.xml` | unchanged — peers don't run a listener |

## What's still open

Things that can't be settled without prototyping or that depend
on a downstream decision:

- **Notification copy** — the wording above is a first draft.
  Play Console reviewers (if/when we ship through Play) prefer
  "data sync" / "active session" framing over "background
  service" framing. Worth iterating once we have a working
  build.
- **Per-project vs per-peer locking granularity** for fan-out.
  Today the per-project flock holds during a github push. With
  LAN fan-out, the lock is held for the whole multi-target
  loop — which means a slow LAN peer holds up the github push
  for the same project. If this becomes a UX problem, split
  the loop and acquire/release per target. Defer until we
  observe it.
- **mDNS instance-name collisions** — if two phones share
  the same `device_name` ("Linguist's phone"), NsdManager will
  rename one of them with a numeric suffix. The displayed name
  in the picker stays unique; the underlying `peer_id` is what
  the daemon uses. Worth surfacing in the pairing UI so the
  user knows which phone they actually picked.
- **Cert rotation** — no path in v1. If a phone is lost, the
  user re-installs and re-pairs from scratch. Whether that's
  enough depends on field experience.
- **Wi-Fi Direct / Wi-Fi Aware fallback when no AP exists.**
  Out of scope for v1 (the office case is the priority).
  Direct is non-deprecated and viable; Aware is hardware-gated
  (Pixel/Samsung/Xiaomi flagships only) so opportunistic at
  best. Park.
- **Idempotence smoke test** — LIFT-merge-replay under the LAN
  → github sequence (covered in "Conflict semantics"). Manual
  pre-ship test, not automated.

## Onramp for picking this up fresh

This section is for a Claude (or human) arriving without the
2026-05-19/20 design-session context. The spec above is the
*design*; this section is *how to start work without backsliding
on settled questions*.

### Read these first, in this order

1. `/home/kentr/bin/AZT/CLAUDE.md` — suite-level invariants
   (one canonical collab impl, shared `.buildozer/`, suite-wide
   signing keystore, naming conventions).
2. `azt-collab/CLAUDE.md` — daemon/client split, status codes,
   Android `:provider` process isolation rationale (load-bearing
   for decision #5 in the architecture overview), 0.43.0
   commit/push split, `JOB_INTERRUPTED` contract.
3. `azt_collab_client/CLAUDE.md` — hard rules. Most relevant
   here: no dulwich in client (#1), daemon-owned-state contract
   (#8) — paired-peers list will join that table.
4. `azt_collab_client/docs/rationale/sync.md` — `_drain_pending_push`
   shape; LAN fan-out hooks into the existing drain loop.
5. `azt_collab_client/docs/rationale/identity.md` — contributor +
   device_name. `peer_id` lives adjacent to these but is
   semantically distinct (commit-author identity vs
   pairing-handshake identity).
6. `MEMORY.md` index (auto-memory). Entries specifically
   relevant to this work, by name:
   - `feedback_jnius_prewarm_main_thread` — pre-warm NsdManager
     + WifiManager in `server_apk/main.py` step 2a before phase 5.
   - `feedback_aar_implementation_deps` — if a Java-side helper
     is added for NsdManager glue, list transitive AndroidX
     deps explicitly.
   - `feedback_avoid_explainable_android_permissions` — load-
     bearing for the `FLAG_SHOW_PICKER` choice in discovery.
   - `feedback_hot_toggle_not_restart` — "Allow LAN sync"
     toggle must hot-apply, not require a daemon restart.
   - `feedback_typed_status_over_polling` — peer flows on
     pairing/sharing iterate the typed `Status` stream, no
     parallel polling layer.
   - `feedback_share_helpers_centralized` — QR share UI goes
     through `azt_collab_client/ui/share.py`, not inline jnius.
   - `feedback_no_canonical_peer_examples` — the spec describes
     "a peer," not "the recorder."
   - `feedback_stay_in_repo_lane` — when invoked from
     `azt-collab`, design at the canonical-repo seams; don't
     grep into sibling AZT-suite repos.
   - `feedback_min_client_version` — wire-format additions
     (the LAN/* endpoints) require a `MIN_CLIENT_VERSION` bump.
   - `feedback_buildozer_signing_env_vars` — keystore via
     `P4A_RELEASE_KEYSTORE` et al, not spec keys.
   - `project_build_cache_contamination` — if peers misbehave
     during this work, suspect shared `.buildozer/` cache first.

### Rejected alternatives (do not relitigate)

Grouped by what was on the table. One-line "why not" each — the
spec above carries the positive case.

**Topology / state shape:**

1. *Full-mesh peer-graph + gossip* (Radicle's namespace-per-peer
   refs). Star + redundant fan-out gives implicit propagation
   via git's ref-advertisement dedup, with no peer-graph state.
2. *Transitive discovery* (Alice auto-learns Carol from Bob).
   Explicit pairing is safer (only sync with phones you knew
   about) and skips the gossip plumbing.
3. *Per-project pairing* (each shared project = its own QR
   pair). Tedious; per-device pair + project-share gesture is
   the same expressive power with one scan.
4. *Mutual QR scan* (A shows, B scans, **then** B shows, A
   scans). Auto-reverse-record on B's first authenticated
   fetch makes the second scan unnecessary.
5. *Auto-share-all after pairing.* Too coarse — one phone often
   carries multiple unrelated language projects.
6. *QR for the project-share step.* Duplicates the QR vocabulary
   pairing already used; in-UI pick from the paired list is
   cleaner.
7. *Per-project "Allow LAN sync" toggle.* More UI surface, more
   notifications. Daemon-wide is one switch, one notification.
8. *Persisting `lan-{peer_id}` remotes in `.git/config`.*
   Endpoint is volatile per session; in-memory `remote_add` +
   `remote_remove` per sync attempt is the standard P2P pattern.
9. *Per-commit LAN fan-out.* Bursts of commits would mean N
   fan-outs per peer. Ride the existing debounce → one fan-out
   per drain pass.
10. *Bearer token + cert on LAN.* Cert handshake already proves
    peer-id; bearer is redundant. Two middleware paths (cert
    on LAN, bearer on loopback) is simpler than one.

**Service shape:**

11. *Separate service for the LAN listener.* Two Python
    interpreters in one process = GIL fight = crash. The
    `:provider` isolation exists for exactly this reason —
    don't reintroduce it.
12. *`dataSync` foreground-service type.* Android 15+ caps it
    at 6h/24h; "Allow LAN sync left on overnight" hits the cap
    and silently drops the listener.
13. *`connectedDevice` FGS type.* Semantically Bluetooth / USB /
    CDM-paired peripherals; reviewer intent is narrower than
    same-Wi-Fi phones, even though the docs are permissive.

**Transport / discovery:**

14. *`python-zeroconf` on Android.* Android 17 (apps targeting
    SDK 37) gates raw mDNS sockets behind a new runtime
    permission (`ACCESS_LOCAL_NETWORK`). `NsdManager` +
    `FLAG_SHOW_PICKER` dodges it. `python-zeroconf` stays on
    desktop.
15. *Plain HTTP over LAN.* Cert-pinned HTTPS gives identity
    proof in the handshake and avoids forcing peer apps to
    whitelist cleartext RFC1918 in `network_security_config.xml`.
16. *mDNS `.local` hostname resolution via `getaddrinfo`* on
    Android. Unreliable; daemon does mDNS lookup itself and
    substitutes a raw IP into the dulwich URL.
17. *Wi-Fi Direct as primary transport.* Rough edges, async
    callback verbosity. Deferred — keep as a maybe-fallback
    for "no AP at all" scenarios.
18. *Wi-Fi Aware (NAN) as primary.* Hardware-gated; only some
    Pixel/Samsung/Xiaomi flagships ship it. Opportunistic at
    best, can't rely on it for the field-linguist user base.
19. *Bluetooth Classic SPP / BLE.* Too slow (~2 Mbps / ~125
    kbps practical) for a real LIFT pack.

**Identity:**

20. *Suite signing-keystore fingerprint as the peer-id.* No
    per-device revocation; lost on device wipe; ties peer
    identity to suite identity. Per-device ed25519 keypair gives
    finer-grained pairing.

**Server implementation:**

21. *`git-daemon` / `git http-backend` (CGI).* No system git on
    Android; reintroduces a binary dep the suite avoided.
22. *`pygit2`.* `libgit2` native binary not in the suite's
    `recipes/` overlay; not worth adding for a feature dulwich
    already handles.
23. *SSH transport.* `dulwich` has no SSH server; paramiko-shim
    is non-trivial. HTTP is the easier path peer-to-peer.

### Suggested implementation phases

Eight phases, each independently smokable. Bumps versions as
they land — debug bumps until phase 8 (which is the first peer-
visible release).

1. **Identity + paired list, no transport yet.**
   `azt_collabd/peer_id.py` (keypair + self-signed cert + cached
   sha256 fp; atomic load/save). `azt_collabd/peers.py`
   (`peers.json` via `atomic_open_write`). Server endpoints:
   `GET /v1/lan/peer_id`, `GET /v1/lan/peers`. Client wrappers:
   `lan_peer_id()`, `lan_list_peers()`.
   *Smoke:* daemon starts, `peer_id` + `peer.crt` created in
   `$AZT_HOME`, `peers.json` empty.

2. **Pairing QR + accept flow (no listener yet, so no
   auto-reverse-record).** `POST /v1/lan/pair/qr` returns the
   JSON payload to QR-encode (segno does the imaging in the
   daemon UI). `POST /v1/lan/pair/accept {payload}` records the
   peer into `peers.json`. Daemon UI "Pair a phone" page.
   Picker "Scan to pair" entry point reusing the existing
   zxing-android-embedded scan. Status: `LAN_PAIRED`,
   `LAN_FP_MISMATCH`.
   *Smoke:* two daemons (two `$AZT_HOME` dirs on one desktop is
   fine), A's QR → B's accept → both `peers.json` show the
   other side. Auto-reverse-record is verified later in phase 4.

3. **Project-share gesture.** `POST /v1/lan/share/{lang}/{peer_id}`,
   `DELETE /v1/lan/share/{lang}/{peer_id}`. Daemon UI: per-
   project "Share with paired phone" picker. `peers.json`
   `shared_projects` field used; no listener yet, so this is
   just bookkeeping.
   *Smoke:* round-trip the share; verify `peers.json` reflects
   the per-direction allowlist.

4. **Listener + cert auth (no discovery yet — use static
   endpoint from the QR).** `azt_collabd/lan_listener.py` —
   `dulwich.web` + `ThreadingMixIn` + cert middleware + TLS
   wrap_socket. `POST /v1/lan/toggle {on: bool}` flips the
   daemon-wide toggle and starts/stops the listener thread.
   Android-side: promote to `specialUse` FGS on toggle on,
   acquire `WIFI_MODE_FULL_HIGH_PERF` WifiLock, demote on
   toggle off. Manifest changes (FGS type + permissions). Auto-
   reverse-record on the listener's first authenticated request.
   Status: `LAN_TOGGLE_OFF`, `LAN_PEER_UNREACHABLE`.
   *Smoke (desktop):* two-`$AZT_HOME` setup, A's listener on a
   loopback port, B fetches `lan-{A_peer_id}` over HTTPS using
   A's IP+port from the QR payload + pinned fingerprint.
   *Smoke (Android):* toggle on → FGS notification appears;
   toggle off → notification clears.

5. **Discovery — `NsdManager` (Android) + `python-zeroconf`
   (desktop).** `azt_collabd/lan_discovery.py`. Android-side
   advertise + browse with `FLAG_SHOW_PICKER`. `peer_id`-keyed
   endpoint cache in memory. Pre-warm `NsdManager` + `WifiManager`
   in `server_apk/main.py` step 2a (jnius main-thread rule).
   Manifest gains `CHANGE_WIFI_MULTICAST_STATE`. `MulticastLock`
   acquire/release tied to advertise/browse lifecycle.
   *Smoke:* two phones same Wi-Fi, A's listener up, B's daemon
   sees A in `lan_list_peers()` discovery output, fetches work
   without the QR-provided endpoint (mDNS-resolved IP used).

6. **Scheduler fan-out.** Extend `_drain_pending_push` in
   `azt_collabd/scheduler.py` to iterate over reachable paired
   peers per project. `in_memory_remote` context manager in
   `azt_collabd/repo.py`. Per-target success recorded so a
   github outage doesn't lose track of refs that landed on LAN.
   *Smoke:* commit on A → drain pass pushes to github + B in
   one debounce window; B's view of A's project updates within
   ~1s on LAN.

7. **Hotspot fallback + manual-IP UI.** Static endpoints in
   `peers.json`. Daemon settings UI affordance to add a manual
   IP for a paired peer. Endpoint resolution order: mDNS →
   static endpoints → QR-hint endpoint as last resort.
   *Smoke:* phone A hotspots, phone B is the client (mDNS will
   fail across the AP boundary), manual IP entry → fetch works.

8. **Translations + cross-doc updates + `MIN_CLIENT_VERSION`
   bump.** All `LAN_*` status codes get French translations.
   `azt_collab_client/CLAUDE.md` "Daemon-owned state" table
   gains rows for `peer_id`, `peers.json`, LAN toggle.
   `azt_collab_client/CLIENT_INTEGRATION.md` adds the new
   client surface. `MIN_CLIENT_VERSION` floor bumped per
   `feedback_min_client_version`.
   This is the first **minor** version bump (peer-visible new
   surface). Manual matrix in `docs/test_plan.md` extended with
   the two-device LAN scenarios.

### Status codes to add

Both `azt_collabd/status.py` and `azt_collab_client/status.py`
(mirror, hard rule #5):

| Code | Params | When |
|---|---|---|
| `LAN_PAIRED` | `{peer_id, device_name}` | New peer recorded in peers.json |
| `LAN_UNPAIRED` | `{peer_id}` | Peer removed (settings UI or auto-prune) |
| `LAN_PEER_UNREACHABLE` | `{peer_id}` | Tried all endpoints, none responded |
| `LAN_FP_MISMATCH` | `{peer_id, expected_fp, got_fp}` | TLS cert fingerprint differs from pinned value |
| `LAN_TOGGLE_OFF` | `{}` | Operation requires the daemon-wide toggle on |

Plus translations in `azt_collab_client/translate.py` and the
French `.po` (translation-coverage drift detector will fail CI
— well, the `pytest tests/` smoke — until they land).

### Verifying without two real Android devices

Most phases are smokable desktop-first:

- **Phases 1-3 (identity, pairing, share):** two `$AZT_HOME`
  dirs on one machine. `AZT_HOME=/tmp/A python -m azt_collabd`
  and `AZT_HOME=/tmp/B python -m azt_collabd` in two terminals.
  Different ports auto-assigned via `server.json`.
- **Phase 4 (listener + cert auth):** same two-`$AZT_HOME` trick.
  Fetch via `https://127.0.0.1:<port>/<lang>.git` with pinned
  fingerprint.
- **Phase 5 (NsdManager):** Android-only. `python-zeroconf` on
  the desktop side will discover *itself* (single-process
  loopback) but the Android NsdManager path needs a real device
  (or an emulator with Wi-Fi multicast — unreliable).
- **Phases 6-8:** two-phone manual smoke is the only honest
  test.

`examples/sister_app.py` extended to print `lan_list_peers()`
once those endpoints exist is the cheapest read-only check from
a sibling app's venv.

### One-paragraph self-test before merging anything

If after a /clear you can answer all of these in under a minute
by reading just this file + the four CLAUDE.md files in the
"Read these first" list, you have enough context:

1. Why isn't this a peer-graph / gossip design?
2. Why `specialUse` instead of `dataSync`?
3. Why `NsdManager + FLAG_SHOW_PICKER` instead of `python-zeroconf`
   on Android?
4. Why is `peer_id` a separate keypair from the suite signing
   fingerprint?
5. Why does the listener live in the existing `:provider`
   process, not a new service?
6. Why is there no bearer token on the LAN transport?
7. Why are `lan-{peer_id}` remotes in-memory only?
8. Why does the user need to see a persistent foreground-service
   notification while LAN sync is on?

If any of these feel unobvious, the rejected-alternatives list
above carries the one-line answer.

## Why not now

The github-mediated sync path is what 0.43.x is stabilising
(non-FF reconciliation, DoH fallback, stale-unpack remediation,
daemon-log rotation). LAN sync expands the matrix substantially
— new transport, new auth model, new foreground service shape,
new Android permissions, new pairing UX — and would push out the
"push actually works for everyone, every time" milestone.

Parked here so the design isn't lost when github sync is solid.
The research is done; the next thing this needs is a working
session against a real second device.
