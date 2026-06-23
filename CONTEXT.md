# AZT Collab

The collaboration daemon (`azt_collabd`) plus its thin client
(`azt_collab_client`). Owns git, dulwich, credentials, the project
registry, locks, LIFT-aware merge, and LAN peer-to-peer sync. The
vocabulary owned here is the suite-wide identity and integration
surface; every behavioural sub-repo consumes it via the client.

The model in one sentence: a **device** (with `device_id` and
`device_name`) runs a **client** and relates to paired and/or
nearby **peers**, which are also devices running clients.

## Language

### Project identity

**project_id**:
A free-form string identifying a project in the daemon's registry.
Often a BCP-47 code (when the [analang](../azt/CONTEXT.md) doubles
as the project identifier — the common case), but not required to
be: users with historically-named projects (Bible translation names,
autonym-based names, etc.) use prose. Derived by the daemon from
(1) the git remote repo name, (2) the LIFT filename stem,
(3) the `working_dir` basename. The current code field name is
`langcode`; see Flagged ambiguities.
_Avoid_: langcode, project name (when precision matters)

**project**:
A registered LIFT lexicon in the daemon's registry. A tuple of
`project_id`, `working_dir`, `lift_path`, `remote_url`, an optional
explicit [analang](../azt/CONTEXT.md), optional `repo_slug`, and
optional `cawl_image_repo`. Persisted in `$AZT_HOME/projects.json`.

**working_dir**:
The on-disk directory the daemon treats as the git working tree for
a project. Holds the `.lift` file, the `audio/` directory, and the
`.git/` directory. Per-project.

**lift_path**:
Absolute path to the `.lift` XML file inside a `working_dir`. May
not exist on disk yet (newly cloned, awaiting first checkout);
peers check `Project.lift_exists` before reading.

### Suite integration

**client**:
An application that talks to its local daemon via
`azt_collab_client`. The clients in the suite are AZT desktop, the
recorder, and the viewer. Each client follows the
[client contract](#client-contract).
_Mild overload_: "client" also names the `azt_collab_client`
package. Disambiguate in prose by saying "the client library" /
"the `azt_collab_client` package" for the package, "a client" /
"the recorder client" for an app.
_Avoid_: sister app (legacy term; see Flagged ambiguities)

**client on a peer**:
A client running on a remote (LAN-paired) device — i.e. talking
to that device's local daemon, not ours. Useful for cross-device
flows where the qualification matters.

**client contract**:
The integration rules in `azt_collab_client/CLIENT_INTEGRATION.md`
that every client must follow to be a conformant consumer of the
daemon (RPC surface, status-code routing, identity, decline-memory,
`on_resume` reconciliation, etc.).

**suite signature**:
The shared Android signing identity carried by
`~/bin/azt-suite.keystore`; its SHA-256 lives at
`android/SUITE_FINGERPRINT`. All suite APKs sign with it. The
`org.atoznback.AZT_COLLAB_ACCESS` Android permission is
`protectionLevel="signature"`; a mismatch silently makes peer→daemon
ContentProvider calls fall back to no-server.

**$AZT_HOME**:
The daemon's state directory. Desktop: `$XDG_DATA_HOME/azt/` (or
`~/.local/share/azt/` if `XDG_DATA_HOME` is unset; macOS:
`~/Library/Application Support/azt/`). Android: the server APK's
private filesDir. Holds `server.json`, `config.json`,
`projects.json`, `jobs.json`, `peer_id`, `peer.crt`, `peers.json`.

### RPC vocabulary

**status code**:
An uppercase identifier (e.g. `PUSHED`, `AUTH_REQUIRED`,
`CONTRIBUTOR_UNSET`) returned by the daemon to drive client logic.
The canonical list is `azt_collabd/status.py`; the client mirrors
it in `azt_collab_client/status.py` (decode-only, no imports of the
daemon package). Translation is the display path only — business
logic uses `Result.has(S.CODE)`; substring-matching translated text
is a regression.

**ui_language**:
The locale the app's UI renders in. Often coincides with one of
the [glosslangs](../azt/CONTEXT.md) in a given user's session but
isn't required to. Stored suite-wide in `$AZT_HOME/config.json`
and changed via `azt_collab_client.i18n.set_language`. The matching
getter is the code-named `current_language()`; see Flagged
ambiguities.

### Device identity

**device_id**:
This daemon's own ed25519 identity — the SHA-256 hex of its
public key. Immutable per device; changing it breaks pairing
trust. Persisted at `$AZT_HOME/peer_id` (file-name drift; see
Flagged ambiguities). Other daemons pin our identity by this
value and reach us by advertising it in mDNS TXT.
_Avoid_: peer_id (when meaning *our own* id)

**device_name**:
Human-readable name for this device. Free-form, mutable,
user-overridable via the daemon settings UI; auto-populated from
the OS on first read. Two uses: (1) the commit-author
disambiguator (suite-wide identity, in
`$AZT_HOME/config.json :: device_name`); (2) what shows in the
"Nearby (unpaired)" UI list and on paired-peer cards of other
devices that have us in mDNS view.

### LAN sync

**nearby peer** (a.k.a. **unpaired peer**):
A peer this daemon has seen via mDNS but hasn't paired with.
Surfaces in the UI as "Nearby (unpaired)". Info on nearby peers
is **entirely ephemeral** — held in in-memory caches with a 5-min
TTL; never persisted.
_Avoid_: discovered peer (code drift; "nearby" is canonical)

**paired peer**:
A peer whose ed25519 fingerprint is recorded in
`$AZT_HOME/peers.json`. Pairing is established by mutual
acceptance of a `pair_request`. The daemon attempts to reach
paired peers on a backoff schedule; per-attempt failure surfaces
as `LAN_PEER_UNREACHABLE`.

**pair request**:
The protocol message that elevates a nearby peer into a paired
peer. Sent via POST to the receiver's `/v1/lan/pair_request`; the
receiver stashes a pending decision (`KIND_PAIR_REQUEST`) awaiting
user acceptance.

**fingerprint** / **fp**:
SHA-256 of a peer's ed25519 public key — i.e. its `device_id`.
The identity pin: peers trust each other by fingerprint, not by
certificate-chain verification (TLS client validation is
deliberately `CERT_NONE`).

**endpoint** (LAN):
A reachable `(host, port)` for a paired peer. mDNS discovers
endpoints dynamically; `lan_set_static_endpoints` configures
fallbacks for hotspot-host topologies where mDNS isn't usable.

**shared_projects** (per-peer allowlist):
The list of `project_id`s a daemon has agreed to share with a
specific paired peer, stored on each peer's record in `peers.json`.
LAN fan-out of a local commit on project X reaches only peers
whose allowlist includes X. The currently-loaded project in any
client is NOT a gate; the allowlist is the only governor.

## Flagged ambiguities

**`Project.langcode` (code field) vs `project_id` (concept)**:
The data-model field is named `langcode` for historical reasons —
it dates from when the project identifier was presumed to always
equal a BCP-47 language code. Today the daemon supports non-code
project names via `Project.vernlang` as the explicit
[analang](../azt/CONTEXT.md). The conceptually correct field name
is `project_id`. Documented as drift; refactor deferred (271+
occurrences in `azt_collabd` alone). New external surfaces (RPC
params, doc prose) should use `project_id`.

**`Project.vernlang` (code field) vs `analang` (concept)**:
Drift introduced during the Android-suite rollout: the field
storing the explicit BCP-47 code for the analyzed language was
named `vernlang` (vernacular). The suite-canonical term is
[analang](../azt/CONTEXT.md), per the desktop AZT tradition.
Documented as drift; refactor deferred (13 files in azt-collab).
New code should prefer `analang`.

**`current_language()` (code) vs `current_ui_language()` (concept)**:
`azt_collab_client.i18n.current_language()` returns the active
`ui_language`. The bare name elides the qualifier and could be
misread as "current language of the documented project" or
similar. The conceptually correct name is `current_ui_language()`.
Documented as drift; refactor deferred.

**`peer_id` / `peer.crt` (code) vs `device_id` (concept)**:
The files `$AZT_HOME/peer_id` and `$AZT_HOME/peer.crt`, and the
local `self_peer_id` variable (`server.py:723`), all name *this*
daemon's own ed25519 identity. Conceptually it is a `device_id`
— it identifies the device. The code name `peer_id` reflects the
role this id plays when the daemon interacts with other daemons
(we present as a peer to them), not what it *is*. Documented as
drift; refactor deferred per ADR 0001.

**"sister app" (legacy) → "client" (canonical)**:
Older code, the umbrella `azt-collab/CLAUDE.md`, and
`CLIENT_INTEGRATION.md` use "sister app" (and bare "peer") for
what is now canonically a [client](#client). The drift dates from
when the term originated with this package's peer-integration
contract; "client contract" was always the matching phrase, but
the noun didn't follow. Documented as drift; refactor deferred
per ADR 0001.

**Bare "peer" forbidden as a noun in new prose**:
In new code and prose, "peer" appears only as part of
`nearby peer` / `unpaired peer` / `paired peer`. Bare "peer" was
historically overloaded between sister-app sense
(`CLIENT_INTEGRATION.md` and umbrella prose) and LAN-peer sense
(`azt_collabd/peers.py`); the bare noun is retired suite-wide to
end the ambiguity. Persistent peer state (`peers.json`,
`peer_id`, `peer.crt`) is paired-only or self-only by
construction; in-memory caches in `lan_discovery.py` hold nearby
peers and are ephemeral.

**"discovered" (drift) → "nearby" (canonical)**:
Two names appear in the code for the same lifecycle state — a
peer seen via mDNS but not yet paired. Canonical is **nearby
peer**; "discovered" is drift to be retired in new prose.

## Example dialogue

> Recorder dev: "How do I tell the daemon which project I'm
> recording for?"
> Collab dev: "Pass the `project_id` — that's the project's
> registry key."
> Recorder dev: "Is that the language code?"
> Collab dev: "Often, but not always. If the user named their
> project something prose-y, the `project_id` might be `KentBible`
> and the analang (the BCP-47 code for what's IN the lexicon) is a
> separate value stored on the same project. Use `project_id` for
> all daemon RPCs; use the explicit analang from `project_status`
> for LIFT `lang=` attributes."

LAN sync, clients vs paired peer:

> Viewer dev: "I just committed in the viewer. Will the recorder
> see it?"
> Collab dev: "On the same device, yes — both apps are clients
> of the same daemon, so the commit is visible the moment it
> lands. You don't need LAN for that."
> Viewer dev: "What about the other phone?"
> Collab dev: "Different device, so LAN comes in. If that
> phone's daemon is a paired peer of this one and this
> project_id is in its `shared_projects` allowlist, the commit
> gets fanned out over LAN immediately. If LAN isn't on or the
> peer is unreachable, the other phone catches up via github
> once both come online."
