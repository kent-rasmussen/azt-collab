# Changelog

Two packages live here. Versions move together for now (the client
embeds `MIN_SERVER_VERSION`, so when the wire format changes we bump
both); patch-level bumps in one without the other are fine.

- **azt_collabd** — server / daemon. Source of truth: `azt_collabd.__version__` (re-imported by `server.py` as `_VERSION` for the wire response).
- **azt_collab_client** — client library. Source of truth: `azt_collab_client.__version__`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely.

## 0.53.3 — sync: WAN count ticks down during chunked push; GitHub-backup progress in server settings UI

Field gap (deblock-sync item): during a big chunked topic-push (nml, 2754 commits), nothing
showed the user how far the trickle-up had left to go. `wan_unshared` compared local `main`
against `origin/main`, so it stayed **pinned at 2754** the whole upload and only dropped to 0
at the final merge — no visible progress. The live decreasing number (`remaining=…`) existed
only in trace logs.

Fix — redefine the count against what's actually on github, and surface it:

- **`_wan_unshared` (repo.py)** now counts commits not reachable from `origin/main` **∪ any
  `origin/azt-pending-*` topic ref**. A commit's bytes are durable on github the moment its
  chunk lands on the topic ref (whose local tracking ref advances per chunk), so the count
  now **ticks down** as the push progresses instead of staying pinned.
- **`_at_risk`** excludes those topic tips too — commits parked on github (pre-merge) are not
  at risk.
- **`_main_merged` (new)** — True only when the local tip is fully on `origin/main`. This is
  the gate for the "OK/backed-up" state: because the count can reach 0 while bytes sit on a
  topic ref awaiting merge, **`wan_unshared == 0` no longer means backed up**. That window is
  "WAN-0 / finishing"; "OK" requires the merge to have landed. (Per the explicit contract:
  no OK until it's merged; if everything's uploaded but not merged, the count stays at 0.)
- **Wire**: `project_status` gains `main_merged` (bool). Client `ProjectStatus` mirror adds it
  **defaulting True**, so a pre-0.53.3 daemon reproduces the old "OK when wan==0" behaviour
  rather than sticking at WAN-0. Additive + backward-tolerant → no `MIN_*` floor bump.
- **§ 17b** rendering recipe updated: `wan_done := wan==0 AND main_merged`; the wan==0-not-
  merged window renders `WAN-0`.
- **Server settings UI** (`azt_collabd/ui/app.py`, the daemon-owned settings screen): the
  "Current project" block now shows a GitHub-backup line — `✓ backed up` / `finishing
  (merging)…` / `{n} commit(s) to go` (+ `paused — work offline`). French strings added to
  the client catalog. This is server-UI-only; nothing new was added to the peer sync indicator.

LAN-only projects (no origin URL) are unchanged — no topic ref, so the whole-history "WAN-+N"
friction signal stands.

## 0.53.2 — daemon: DATA_LOSS_RISK no longer false-alarms on desktop-azt project shapes

Field repro (first desktop-azt commit on nml, 2026-07-07): `submit_file`'s
commit returned `['DATA_LOSS_RISK', 'COMMITTED_LOCAL']`. The
`_detect_uncommittable` walk in `_commit_step_locked` checks a recorder-shaped
whitelist (`audio/`, `images/`, top-level `.lift`), so a desktop project's
settings JSONs / `WritingSystems/*.ldml` / `reports/` / dated `.lift_*.txt`
backups all get flagged as "will silently never be backed up" — which is false
on this path: staging is whole-tree `add -A`, so every such file was either
just staged (backed up) or is `.gitignore`-matched (deliberate exclusion per
the AZT persistence contract D5/D6). Since peers route `DATA_LOSS_RISK` as a
never-silenced sticky banner, the false alarm is user-facing and loud.

Fix: after staging, filter the walk's candidates to the genuinely-at-risk
remainder — not in the index AND not ignore-matched (via
`dulwich.ignore.IgnoreFilterManager`); filter failure keeps the unfiltered
list (fail-alarming, not fail-silent). The `_stage_audio` copy of the walk is
untouched — staging there is genuinely selective, so its warning stays
truthful. Test: `test_desktop_project_shapes_do_not_trip_data_loss_risk`.

## 0.53.1 — client: Kivy-free platform probes on all non-UI paths

Field repro (desktop azt, 2026-07-07): the first RPC from a non-Kivy host
imported Kivy — `transports._on_android` (and five sibling non-UI sites) did a
function-local `from kivy.utils import platform` — and Kivy's import-time argv
parser rejected the host's own `--restart` flag: `Core: option --restart not
recognized` → hard exit before azt could even load. Kivy hosts (recorder,
viewer) never saw this because Kivy was already imported with argv they own.

Fix: new `azt_collab_client/_platform.py` (env/sys.platform mirror of
`kivy.utils.platform`, identical answers on all suite platforms); all non-UI
probes now use it (`transports/__init__._on_android`, `__init__.py`
open_server_ui / pick_project / CAWL-index route, `lift_io.LiftHandle.
open_read`, `notify._is_android`, `lowpower._is_android`). `ui/` modules may
still import Kivy — they require a Kivy host anyway (hard rule #4 updated).
Regression test `tests/test_no_kivy_on_desktop_paths.py` runs the desktop path
in a clean subprocess with a hostile `--restart` argv and asserts `kivy` never
enters `sys.modules`. Desktop azt also sets `KIVY_NO_ARGS=1` defensively
before importing the client (azt 1.6.0), so any future leak degrades to a
harmless import instead of a dead app. No wire change; no version-floor bumps.

## 0.53.0 — desktop AZT persistence: base-aware `submit_file` + adopt-in-place hardening (G1–G4)

The daemon half of the AZT persistence contract
(`agenda/azt_persistence_server_sync.md`; azt-side wiring is the sibling
`azt/agenda/azt_run_with_server.md` item). Desktop A-Z+T autosaves the whole
LIFT on nearly every edit and never re-reads; without a base-aware write, its
first save after a daemon-side merge would content-clobber peer work. New
capability set:

- **G1 `POST /v1/projects/<lang>/submit_file`** (`{path, staged_path,
  base_sha, message?}`): the caller serializes its full file to a **staged
  sibling** (azt's existing `.part` discipline) and declares the HEAD it
  edited against. Under `project_lock`: HEAD == base → zero-copy
  `os.replace` + synchronous commit; HEAD moved → three-way LIFT merge
  (blob@base, blob@HEAD, staged bytes) via the existing `lift_merge` +
  truncation guards + forensic diagnostics, then commit. New status code
  **`MERGED_WITH_LOCAL`** (both mirrors + FR translation) tells the caller
  its in-memory state is stale and must reload. Empty/unknown base against
  an existing HEAD merges with empty base (add-add) — never a plain replace.
  Bytes-durability never waits on identity: contributor-unset still lands
  the file, refuses only the commit. Post-commit side effects (pending-push,
  `last_commit` stamp, LAN backoff/burst + fan-out) shared with the debounced
  commit worker via the new `scheduler.after_committed_local()`. Client
  wrapper `azt_collab_client.submit_file(...)` returns a `Result` with
  `.head_sha` (the caller's next base); against a pre-0.53 daemon it
  surfaces `SERVER_ERROR error='not_found'` → caller falls back to direct
  write. Desktop/loopback only by design (Android peers keep surgical
  writes; no cross-process staged-file handoff via ContentProvider).
- **G2 `head_sha` in results.** `COMMITTED_LOCAL` now carries a `head_sha`
  param (extra params are invisible to older clients — no
  `MIN_CLIENT_VERSION` bump); `/sync` responses carry a top-level
  `head_sha` the client attaches as `result.head_sha`. New `Result.param()`
  accessor on both status mirrors.
- **G3 adopt-time `.gitignore` hardening.** `register_project` now appends
  (idempotently, content-preserving) azt's desktop artifact patterns
  (`*.lift*txt` daily backups, `*.gz`/`*.7z` emailed variants, `reports/**`,
  `exports/**`, `*.pdf`, WeSay/Chorus sidecars, …) via
  `repo.ensure_ignore_patterns` — `_stage_all` is whole-tree `add -A`, so
  an adopted desktop tree would otherwise commit all of it. Harmless no-op
  on recorder projects.
- **G4 duplicate-working_dir guard.** `projects.register()` refuses a
  second langcode over an already-registered working_dir
  (`WorkingDirAlreadyRegistered` → HTTP 409 `working_dir_already_registered`
  + `existing_langcode`); previously `find_langcode_by_working_dir`
  (first-hit scan) went nondeterministic under a duplicate. Re-registering
  the same langcode (the normal update path) is unchanged.

Tests: `tests/test_submit_file.py` — fast path, divergent no-clobber merge
(both sides' entry edits survive), empty-base merge, auto-init recovery,
contributor-unset durability, staged-path validation, ignore idempotence,
409 guard, debounce burst-collapse, mirror drift, `Result.param`.

No `MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` bump — all additions are
new endpoints, new params, or new top-level response keys that older
peers never read; the one new status code is only ever returned by the
new endpoint.

## 0.52.33 — new-project template: prune definition + citation forms (rules 3 & 4)

Follow-up to 0.52.32, which deliberately left `<definition>` and `<citation>` "as-is".
That over-simplified the host-decided intent: the "leave as-is" was about the **parent
elements** (keep them for user familiarity / `set_audio` tolerance), not about preserving
the junk `<form>` children inside them. `_clean_template` now prunes those forms too,
keeping the parent element even if it ends up formless:

- **definition** (rule 3) — drop empty `<form>` children; keep every populated one, keep
  the `<definition>` parent. Iterates all senses.
- **citation** (rule 4) — mirror the lexical-unit rule: keep only `<form lang=vernlang>`,
  drop every other-language form (empty or populated), keep the `<citation>` parent.

Same bytes→bytes / parse-fail-fallback / order-preserving contract; daemon only, no
wire/client change. `SILCAWL` field, `grammatical-info`, `semantic-domain`, `illustration`,
`trait`, glosses (rule 2), and lexical-unit (rule 1) behaviour unchanged.

## 0.52.32 — new-project template: prune to vernacular server-side, single-sourced

Field bug (azt-recorder triage, 2026-07-04): projects created via the language picker
came up carrying the **full multilingual SILCAWL template** — `<lexical-unit>` with `en`/
`fr`/`pt` source words in the vernacular headword slot, plus empty gloss/definition/citation
forms in ~8 languages. Root cause: `azt_collabd/projects.py::create_from_template` wrote the
downloaded template **verbatim** (only `_mint_fresh_guids`); the intended cleaner was a peer-
side `clean_template` in the recorder that never fired on the picker path (picker-created
projects arrive as `_current_langcode`/authoritative and deliberately do NOT set
`_pending_vernlang`, the only path that ran the cleaner). So no cleanup ran anywhere.

Fix: single-source the cleanup in the daemon. New `_clean_template(xml_bytes, vernlang)`
runs in `create_from_template` right after `_mint_fresh_guids`, same bytes→bytes contract
(parse-fail → return input unchanged). Host-decided rules:

- **lexical-unit** — keep only `<form lang=vernlang>`; drop other-language forms. No-loss
  guard: a populated other-language form whose language has no non-empty `<gloss>` is moved
  into a gloss first (empty gloss treated as absent; runs before the empty-gloss prune), so
  no source word is lost. Add an empty `<form lang=vernlang><text/></form>` if none exists.
- **glosses** — drop empty, keep populated.
- **definition** / **citation** — left as-is (kept for familiarity; `set_audio` tolerates
  citation present or absent).
- vernlang matched as the full assembled BCP-47 tag, exactly (`nml`, `ba-x-dialect`,
  `en-US-x-Kent`) — never a bare subtag. Order-preserving; leaves `SILCAWL` field,
  `grammatical-info`, `semantic-domain`, `illustration`, `trait` untouched.

Daemon only; no wire/client change. Follow-on (separate peer task): retire the recorder's
now-dead `clean_template` on this path. Also incidental — the recorder's `clean_template`,
even when it did run, only walked citation/definition (never lexical-unit or `<gloss>`), a
second reason it couldn't have produced clean output; moot now the daemon owns it.

## 0.52.31 — topic-push: chunk-pick fallback for off-spine base (don't degenerate to whole-history)

Field follow-up to 0.52.30 (nml, both phones now on .30). **0.52.30 worked** where it
mattered: device aztobt1-sudo broke the DivergedBranches wedge and started converging —
`topic-push chunk OK (advanced to 9641a2a7)` then a steady one-commit-per-~15s march,
`remaining` 861 → 860 → 859 → …. That device's topic ref already held `913fedc4`, which
sits on the merge tip's first-parent spine, so the .30 first-parent picker advanced cleanly.

But the same first-parent-only rule **regressed device aztobt2-ui**, whose topic ref is
empty (`server_topic_tip='(none)'`) so its chunk base is the *old* `origin/main`
(`7c42ae48`). That commit is not on the merge tip's first-parent spine, so
`_pick_intermediate_sha` returned the tip itself:

```
topic-push attempt target=3cefc3e0 chunk_n=50 …
topic-push pack-size: 11900 objects, 9,360,658,319 bytes
topic-push pre-shrink chunk_n 50→1 …
topic-push attempt target=3cefc3e0 chunk_n=1   ← still the whole 9.3 GB tip
```

i.e. it degenerated a fresh topic ref to a single ~9.3 GB push that can never chunk
(pre-0.52.30 it at least picked an intermediate). First-parent-only was too strict.

Fix: keep the first-parent spine as the fast path (device aztobt1-sudo's cheap, common
case — unchanged), but when base is off the spine, fall back to the general rule: return
the n-th commit in oldest-first order that has base as an **ancestor** (`base→C` is a valid
fast-forward). That excludes sibling parent-line commits (no divergence — device
aztobt1-sudo still picks `9641a2a7`) *and* still chunks when base is the pre-merge root
(device aztobt2-ui advances in ~200 MB steps again instead of one 9.3 GB brick). Bounded by
early-exit at n. Linear histories are unaffected (spine == the old walk).

Note aztobt2-ui's radio 408s even on a single ~4 MB commit, so it likely won't finish its
own WAN push regardless — but it doesn't need to: once aztobt1-sudo's topic ref completes
and `main` fast-forwards, aztobt2-ui converges for free on its next fetch. Both phones stay
LAN-converged at `3cefc3e0`, `at_risk=0` throughout. Daemon only; no wire/client change.

## 0.52.30 — topic-push: FF-clean chunk picks (fix the post-merge divergence wedge)

Field follow-up to 0.52.28 (nml, aztobt1-sudo/aztobt2-ui). The 0.52.28 phones ran
all day and device aztobt1-sudo made large real progress — its
`azt-pending-nml-aztobt1-sudo` topic ref advanced ~1700 commits (`remaining`
2573 → 861). Then a LAN merge moved its HEAD onto the merge commit `3cefc3e0`,
and the topic-push **wedged permanently** — ~5 h stuck at `remaining=861`,
`server_topic_tip=913fedc4`, every chunk raising `DivergedBranches`:

```
topic-push begin … target=3cefc3e0 server_topic_tip='913fedc4'
topic-push attempt target=3305a38e chunk_n=50 …
topic-push raised: DivergedBranches(b'913fedc4…', b'3305a38e…')
… (halve → re-pick → re-diverge, forever)
```

Root cause: `_pick_intermediate_sha` walked `get_walker(include=[tip],
exclude=[base])`, which for a **merge-commit target** yields commits from *both*
parent lines. Picking one off the sibling line gives a commit that is an
ancestor of the target but **not a descendant of the current topic tip**, so the
fast-forward push is rejected with `DivergedBranches`. The chunk loop had no
`DivergedBranches` handling (the docstring asserted it "can't happen"), so it
halved → re-picked the same DAG → re-diverged; `chunk_n=1` diverged too; then it
bailed transient and the next drain re-entered the identical wedge.

Fix:
- **`_pick_intermediate_sha` walks first-parent only.** Every intermediate is now
  a first-parent descendant of `base`, so `base → intermediate` is always a valid
  fast-forward. If `base` is off the tip's first-parent spine (merged in via a
  second parent), it returns the tip directly — still a valid FF, since the caller
  already verified `base is-ancestor-of tip`; the pack-size estimate + blob
  pre-seed handle the larger direct push. Linear histories are unaffected
  (first-parent == the old walk).
- **Explicit bounded `DivergedBranches` handling in the topic loop.** With
  FF-clean picks our own pushes never diverge; a `DivergedBranches` now means the
  server ref genuinely moved under us. Re-anchor `chunk_base` on the server's
  authoritative tip (from the exception, via `_extract_diverged_remote`) and
  continue — without counting a failure or halving — bounded by
  `MAX_DIVERGED_RESYNCS`. If that tip isn't an ancestor of our target (HEAD moved
  under us), bail transient so the next drain rebuilds the chain from scratch.

Stacks on 0.52.29 (oversize-blob atomic push) and 0.52.28 (fetch-skip). Daemon
only; no wire/client change. Deploy to both phones.

## 0.52.29 — topic-push: never let the byte budget veto an atomic object

Field follow-up to 0.52.28 (nml, aztobt1-sudo/aztobt2-ui). With the fetch-skip
in place the topic-branch chunked push finally ran and **converged in bursts** —
the `azt-pending-nml-…` ref advanced on github (`e25c192 → 0a04558`, hundreds of
objects). But it kept stalling at a hard wall:

```
topic-push attempt … chunk_n=1 … pack-size: 5 objects, 4,319,254 bytes
topic-push raised: 408 … git-receive-pack
preseed: blob 15a738fb79b5 is 4,272,261 bytes; alone exceeds budget 3,145,728
pre-seed surfaced terminal status 'BLOB_EXCEEDS_BUDGET'; bailing
drain push 'nml' codes=['BLOB_EXCEEDS_BUDGET','PUSH_FAILED']   → wan_backoff ~24h
```

nml audio blobs run ~4.3 MB; `sync.commit_pack_byte_budget` is 3 MB. On a
transient `408`, the chunk_n=1 path pre-seeded, and `_preseed_oversize_blobs`
**refused** the >budget blob as terminal `BLOB_EXCEEDS_BUDGET` → 24 h backoff →
stuck at the first oversize file until an app restart re-escalated. Proof it was
a false veto: identical ~4.3 MB chunk_n=1 packs pushed fine seconds earlier
(`chunk OK … 4,316,364 bytes`). The 408 is a slow-link timeout, not a size
ceiling; a single blob is atomic and cannot be split, so the budget must never
forbid it.

Fixes (daemon-only; no wire-format / client-contract change):
- **`repo._preseed_oversize_blobs` no longer refuses an oversize blob.** The
  early terminal `BLOB_EXCEEDS_BUDGET` return is gone; an oversize blob is pushed
  alone in its own single-blob batch (side ref). Once it lands, the retried
  chunk_n=1 commit pack is tiny (commit + tree). The budget governs *batching*,
  never whether an unavoidable object is allowed.
- **chunk_n=1 bail is now transient, not terminal.** `oversize`/`exhausted` at
  the atomic unit returns a plain `(False, None, …)` → `PUSH_FAILED` "will
  resume" instead of `COMMIT_PACK_EXCEEDS_NETWORK_BUDGET` — so escalation resumes
  from the server topic tip (banked progress preserved) rather than parking a
  day. (The old `COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`/`BLOB_EXCEEDS_BUDGET` caller
  branches are now dead but harmless; statuses left defined.)
- **Estimate-based initial chunk size.** `_push_chunked_to_ref` pre-shrinks
  `chunk_n` from the pack-size estimate (`chunk_n * budget / raw_bytes`) before
  attempting, so a fat-history/thin-pipe project goes straight to a fitting size
  instead of burning a multi-minute 408 on chunk_n=50→25→12→… every daemon
  lifetime (nml: ~194 MB @ n=50 shrinks straight to n=1).

Net: the github backup now grinds continuously past oversize audio instead of
stalling ~24 h at each one and only advancing when the app is restarted.

## 0.52.28 — deblock stuck WAN sync: skip needless fetch, cap escalation lock hold

Field diagnosis from two paired phones (nml, aztobt2-ui / db033cd4):
sync was wedged for days despite valid GitHub credentials (`app_installed=True`,
`confirmed=True`) and healthy LAN convergence (`at_risk=0`, both phones at the
merged HEAD). Root cause was **not** access — it was a hung fetch:

- Every escalated drain ran a full `porcelain.fetch`. The log showed
  `[sync-trace] fetch begin` with **no** matching `fetch done`/`fetch failed`
  — once for **85 minutes** in a single daemon lifetime — while
  `[service] idle-stop deferred: WAN sync in flight` fired throughout.
- `_FETCH_TIMEOUT_S` is applied via `socket.setdefaulttimeout`, which is a
  **per-`recv` timeout, not a wall-clock deadline** — a slow/negotiating fetch
  never trips it. Because the single fetch call never returned, the escalation's
  own giveup valve (`_ESCALATE_MAX_VISITS`) was downstream of a call that never
  returned → unreachable.
- The fetch held `project_lock` its whole run, so user-tapped Sync returned
  `BUSY` (confirmed: two `[sync-rpc] 'nml' … done: codes=['BUSY']` at 17:53 and
  18:16). The resumable chunked *push* — which could actually make progress —
  was never reached.
- The remote had **never advanced** (no push ever succeeded; only two phones
  push this repo), so the fetch was pulling nothing useful.

Fixes (daemon-only; no wire-format change, no client-contract change):
- **`repo._push_step_locked` skips the fetch when the remote tip hasn't moved.**
  New `repo._ls_remote_main_tip` does one bounded `GET info/refs` ref
  advertisement (no negotiation, no pack, no local graph walk) and compares the
  remote's branch tip to our tracking mirror. On a confident equality the fetch
  is skipped and the resumable chunked push proceeds. Any peek failure (`None`)
  or a missing mirror falls through to the normal fetch, so first-ever pushes
  and genuinely advanced remotes still reconcile.
- **`scheduler._run_to_completion` gets a wall-clock visit ceiling**
  (`_RUN_TO_COMPLETION_DEADLINE_S = 120s`, checked between iterations) so an
  escalated visit yields `project_lock` after ~one in-flight chunk instead of
  holding it for the whole 8-iteration budget. A user Sync tapped during
  escalation now waits ≤ one chunk (`_PUSH_TIMEOUT_S`), not minutes. The push is
  resumable, so yielding loses no progress.

## 0.52.27 — diagnostics-archive format is a shared client helper

Resolves the `NOTES_TO_DAEMON` "REFACTOR" item (filed from
azt-recorder 1.58.6). The `.tar.gz` diagnostics format was implemented
**twice** — daemon `_h_prepare_share_bundle` and a peer-owned recorder
builder — so the 0.52.19→0.52.23 zip→tar.gz change had to be applied in
both, and the recorder shipped stale `.zip` for a build. No test caught
the drift.

New `azt_collab_client/diagnostics.py` is the single source of the
container **format** (import direction allows it: daemon imports client,
never the reverse; serves daemon + every peer):
- `DIAGNOSTICS_MIME = 'application/gzip'`
- `diagnostics_archive_name(slug='', stamp='')` → `azt_diagnostics_…tar.gz`
  (daemon) / `azt_recorder_diagnostics_…tar.gz` (slug), charset-guarded.
- `build_diagnostics_targz(dest, *, file_items, content_items)` — files
  (per-file `OSError` skipped) + in-memory blobs (`TarInfo`+`addfile`);
  raises only if the archive itself can't be written; returns entry count.

Daemon `_h_prepare_share_bundle` and client `share_diagnostics_action`
now use it. **Collection/staging/dispatch stay per-process** (each side
has files the other can't see, in separate Android sandboxes) — only the
format is shared. No wire-contract change (token + `items[].uri_path` +
`display_name` unchanged) → no `MIN_CLIENT_VERSION` bump. The recorder
peer self-serves onto the helper and deletes its hand-rolled tar block.

Files: `azt_collab_client/diagnostics.py` (new),
`azt_collabd/server.py`, `azt_collab_client/ui/share.py`,
`azt_collab_client/CLIENT_INTEGRATION.md`, `azt_collab_client/CLAUDE.md`.

## 0.52.26 — LAN share QR: valid while displayed, multi-use

Two fixes to the project share-QR consent model:

- **Multi-use while shown.** `qr_offer_active` no longer consumes
  (pops) the offer, so **one displayed QR can be scanned by several
  peers** and each gets the share (the workshop "show it to the room"
  case). Previously the first scanner consumed a single-use offer and
  everyone after got no share unless the owner re-displayed the QR.
- **Validity is driven by the QR being on screen, not a 10-minute
  timer.** The share popup (`share_pairing_qr_popup`) heartbeats
  `lan_pair_qr_keepalive(langcode)` every 10 s while displayed and calls
  `lan_pair_qr_close(langcode)` on dismiss. Daemon keepalive window is
  30 s (tolerates one missed beat). So the offer is armed exactly while
  the QR is up — held indefinitely if the user keeps it open, and
  self-revoked within seconds of closing it (or the app dying), instead
  of staying armed for 10 minutes after a glance. More intuitive
  refresh/retry: just reopen the QR.

Security unchanged in spirit: auto-share still requires an *active*
user-displayed QR for that langcode (the only consent signal the
CERT_NONE LAN TLS gives us) — it's just scoped to "while shown" rather
than "single-use within 10 min."

New RPCs `POST /v1/lan/pair/qr/keepalive` + `/close`; client wrappers
`lan_pair_qr_keepalive` / `lan_pair_qr_close`. `consume_qr_offer` →
`qr_offer_active` (+ `clear_qr_offer`).

Files: `azt_collabd/lan_listener.py`, `azt_collabd/server.py`,
`azt_collab_client/__init__.py`, `azt_collab_client/ui/lan_popups.py`.

## 0.52.25 — repo-access browser fallback + completes the invite flow

Debug cut that finishes the 0.52.24 invite work (the 0.52.24 field build
predated the completed wiring). Adds the **browser-open fallback** for
when the daemon can't auto-accept an invitation (none pending yet, or the
app token can't accept it):

- `azt_collab_client.ui.open_url(url)` — `ACTION_VIEW` to the device
  browser (no-op with `on_error` off-Android).
- `azt_collab_client.ui.repo_access_popup(owner_repo, url)` — a peer-
  facing popup for a `REPO_NO_ACCESS` result / `last_sync_error`: explains
  the cause and offers **Open GitHub** (→ the repo/invitations page to
  accept or request access) + Close. Peers route a `S.REPO_NO_ACCESS`
  status (or the `project_status.last_sync_error` banner) here, passing
  the status params' `owner_repo` + `url`.

This is the *fallback*; the common path is still silent auto-accept
(0.52.24) — the browser only opens when there's nothing to auto-accept.

Files: `azt_collab_client/ui/share.py`, `azt_collab_client/ui/popups.py`,
`azt_collab_client/ui/__init__.py`.

## 0.52.24 — auto-accept GitHub invitations; honest no-access diagnosis

A project pointing at someone else's GitHub repo (e.g. a peer cloned
`aztobt2-ui/nml`) 404s forever until the invitee **accepts** the
collaborator invitation on GitHub — a step field users never find.
And the daemon churned on that 404: dulwich raises `NotGitRepository`,
which wasn't recognized as no-access, so the push loop retried it 11×
(holding the lock, BUSY-ing commits) before backing off 24 h — with no
user-visible reason.

- **Auto-accept (the 404 is the trigger).** On a 404 / `NotGitRepository`
  from a fetch or push, the daemon calls `GET /user/repository_invitations`
  and, if one matches this exact repo, `PATCH`-accepts it and retries
  (`INVITE_ACCEPTED`). No in-app invite flow, no browser, nothing to tap —
  aztobt2 grants collaborator, and aztobt1's next sync accepts its own
  invite and proceeds. Scoped to the repo being synced (never accepts
  unrelated invites); gated by `wan_backoff` so it doesn't re-poll.
  `auth.list_repo_invitations` / `accept_repo_invitation` /
  `try_accept_repo_invitation`.
- **Honest no-access verdict.** When there's no matching invite, the 404
  is surfaced as new `REPO_NO_ACCESS` (owner/repo + repo URL) — which
  enumerates the real causes (private-not-shared / not-a-collaborator /
  app-not-granted / wrong name) instead of falsely asserting
  `APP_NOT_INSTALLED`. GitHub returns 404 for all of these and gives us
  no way to disambiguate from the caller's side; `check_app_installed`
  run with the caller's token can't even confirm "installed", so we no
  longer claim it. Only a *positive* API result refines to
  `REPO_NOT_AUTHORIZED` / `APP_SUSPENDED`. (`auth.diagnose_no_access`.)
- **Short-circuit the churn.** `repo._is_repo_not_found` + `_handle_no_access`
  bail out of the fetch/push paths on no-access instead of running the
  11× retry loop.
- **Surface it, don't silently die (req 1.1).** Access-class failures
  persist as `projects.last_sync_error` (a typed code) and ride out on
  `project_status.last_sync_error` so the peer can show a persistent
  "sync blocked: <reason>" banner. Cleared on the next successful sync or
  an auto-accepted invite. The only silent case remains "no credentials
  at all" (not set up yet).

- **End the wait on an event, not a blind timer.** A blocked push stays
  on the normal (up-to-24 h) backoff — that's what protects the radio.
  But the *access* condition is re-checked cheaply and independently:
  - **Event-nudge (local fixes):** saving GitHub credentials
    (`_h_set_github_tokens`) nudges every access-blocked project;
    a successful **Grant collaborator** nudges that project.
  - **Cheap re-probe (remote fixes):** `_drain_access_reprobe` runs one
    small call per blocked project (throttled 5 min, decoupled from the
    push backoff) — `try_accept_repo_invitation`, else
    `auth.probe_repo_access` (`GET /repos/{owner}/{repo}` → existence +
    `permissions.push`). When it flips to OK (collaborator grant /
    permission upgrade / invite appeared), it clears the error and
    `wan_backoff.nudge`s the real push. So sync resumes within minutes of
    an out-of-band grant instead of waiting out the 24 h curve.
  - Codes re-probed: `REPO_NO_ACCESS`, `REPO_NOT_AUTHORIZED`,
    `APP_NOT_INSTALLED`, `APP_SUSPENDED`, `ACCESS_DENIED`. `NOT_A_REPO`
    is excluded (local / publish-flow, not a remote grant).

Files: `azt_collabd/auth.py`, `azt_collabd/repo.py`,
`azt_collabd/scheduler.py`, `azt_collabd/projects.py`,
`azt_collabd/server.py`, `azt_collabd/status.py`,
`azt_collab_client/status.py`, `azt_collab_client/translate.py`.

Still peer-side (not in this cut): opening the browser to the
invitations page as the *fallback* when there's no pending invite to
auto-accept, and prompting at LAN-accept / project-receive.

## 0.52.23 — diagnostic share archive is now .tar.gz (Dome strips .zip)

The field's Dome email server silently **strips `.zip` attachments**,
so a shared diagnostic bundle never arrived. Switched the archive
from zip to gzipped-tar. gzip's magic bytes (`1f 8b`) aren't in the
zip family (`PK\x03\x04`, which also fingerprints .docx/.jar/.apk),
so `.tar.gz` clears both extension-based and content-sniffing
attachment filters. Still a single attachment via `ACTION_SEND`, so
Signal's ACTION_SEND_MULTIPLE image/video-only filter (the reason
0.52.19 bundled to one file) is still avoided; `application/gzip`
clears Signal's `application/*` manifest entry. stdlib `tarfile` —
no new deps, works under p4a.

Three sides, in lockstep:
- **Daemon** (`server.py:_h_prepare_share_bundle`): builds
  `azt_diagnostics_<stamp>.tar.gz` with `tarfile.open('w:gz')` (the
  snapshot goes in via `TarInfo`+`addfile` from memory, per-day logs
  via `tf.add`).
- **Client log-share fn** (`ui/share.py:share_diagnostics_action`):
  dispatches with `mime_type='application/gzip'`. This is the fn the
  picker's and the daemon UI's "Share diagnostics" buttons call, so
  every in-repo client log-share path gets the new format.
- **ContentProvider** (`AZTCollabProvider.mimeForPath`): `gz`/`tgz`
  → `application/gzip` so receivers that consult `getType` route it
  as a binary attachment matching the intent.

Legacy `share_log_file` (no in-repo callers; ships a single
`text/plain` `.log`, not a zip) is unaffected by the strip and left
as-is.

Files: `azt_collabd/server.py`, `azt_collab_client/ui/share.py`,
`android/src/main/java/org/atoznback/aztcollab/AZTCollabProvider.java`.

## 0.52.21 — push a stuck, diverged history through instead of dying mid-fetch

Field repro (nml, two phones): a project's local history diverged
from its github remote by thousands of commits and **never
converged**. On the repo owner's phone the WAN push started
(`[sync-trace] fetch begin`) and was killed before it could finish
— every daemon lifetime restarted from scratch. Root cause: the
Android `:provider` idle-stop loop
(`server_apk/service.py`) stops the service after
`IDLE_TIMEOUT_SECONDS` of no ContentProvider touches + no bound
peers, and that measure is **blind to a WAN fetch/merge/push
running in a scheduler thread**. Close the UI mid-push → 300 s
later the process is killed → the resumable chunked push
(`repo._push_chunked_to_ref`) never banks progress. (On the other
phone the same project also can't sync, for an unrelated reason —
the GitHub App isn't installed on that repo's owner account; that
is a separate config issue, not addressed here.)

Three layers, all daemon-side (no wire-format change, no client
bump):

- **Layer 1 — don't kill an in-flight sync.** New
  `azt_collabd/sync_flight.py` holds an in-memory "a WAN sync is
  running" counter. The scheduler wraps every push attempt
  (`_attempt_push`) in `sync_flight.guard()`; the `service.py`
  idle loop refuses to `stopSelf()` while the count is nonzero
  (`idle-stop deferred: WAN sync in flight`). Same `:provider`
  process, so the flag is genuinely shared.

- **Layer 2 — notice *this* is happening (not just "no internet").**
  `wan_backoff.py` gains `push_inflight_since` /
  `interrupted_count`: a push that starts sets the marker, a push
  that *finishes* (success or explicit failure) clears it. If it
  survives to the next daemon startup, the previous attempt was
  killed → `note_interrupted_on_startup` (called from
  `reconcile_on_startup`) bumps `interrupted_count`. A high count
  while online is the escalation trigger; offline stays quiet.

- **Layer 3 — do whatever it takes.** When online AND
  `interrupted_count >= 2`, the drain routes to
  `_run_to_completion`: promote to an Android foreground service +
  WifiLock (`lan_fgs.arm_for_transfer`) so the process stays alive
  and the radio stays high-perf, then loop the resumable chunked
  push, bypassing the radio-friendly backoff curve, until it
  converges. Bounded per visit (`_RUN_TO_COMPLETION_MAX_ITERS`);
  resumable across ticks; permanent failures (no-access / not-a-repo)
  and a battery giveup valve (`_ESCALATE_MAX_VISITS`) revert it to
  normal backoff.

The user-gestured Sync path (`server.py:_h_project_sync`) is also
wrapped in the Layer-1 guard, so tapping Sync then closing the app
can't get the sync killed mid-flight either.

Files: `azt_collabd/sync_flight.py` (new), `azt_collabd/wan_backoff.py`,
`azt_collabd/scheduler.py`, `azt_collabd/server.py`,
`server_apk/service.py`.

**Build fix (server APK):** added `filetype` to
`server_apk/buildozer.spec.tmpl` requirements. Kivy 2.3.0's
`kivy/core/image/__init__.py` imports `filetype` at load (it
replaced the stdlib `imghdr`), so a rebuilt APK without it crashed
on startup with `ModuleNotFoundError: No module named 'filetype'`
before any daemon code ran. Peer APKs that rebuild on Kivy 2.3.0
need the same requirement.



Field feedback 2026-06-22: when the support engineer unzips a
diagnostic share and opens the daemon log files, some text
editors don't auto-recognise ``.log`` as a text format —
syntax-highlighting falls back to "plain bytes", some viewers
refuse to open the file at all. Renaming the suffix to
``_log.txt`` keeps "log" in the basename (so ``ls`` /
filename-grep still finds them by the "log" token) while
making the actual extension ``.txt``, which every text editor
on every platform recognises.

**Filename pattern change** in
``azt_collabd/server.py:_daemon_log_path_for``:

Before: ``daemon-<8hex>-YYYY-MM-DD.log``
After:  ``daemon-<8hex>-YYYY-MM-DD_log.txt``

Both tagged (with peer_id prefix) and untagged
(bootstrap-fresh) forms get the new suffix.

**Retention regex updated** in
``_DAEMON_LOG_DATE_RE`` to match BOTH the new ``_log.txt``
suffix and the legacy ``.log`` suffix:

```python
re.compile(
    r'^daemon-(?:[0-9a-f]{8}-)?(\d{4}-\d{2}-\d{2})'
    r'(?:_log\.txt|\.log)$')
```

Side effects:

- **Upgrade continuity**: pre-0.52.20 ``.log`` files on disk
  continue to participate in retention (still get pruned
  after the retention window, still get included in
  ``get_daemon_log_files`` / share-bundle responses if
  they're inside the window) until they naturally age out.
  No one-shot migration pass required.
- **Cross-format coexistence**: a device upgraded mid-day
  will have today's ``.log`` AND today's ``_log.txt`` if the
  daemon respawned at the version boundary. Retention treats
  them as separate days only if their date strings differ —
  same-day files both pass through with their distinct
  content. Edge case is cosmetic.

**Java provider unchanged.** ``AZTCollabProvider.mimeForPath``
already maps ``.txt`` → ``text/plain``; the new filenames'
extension is ``.txt`` so the existing rule applies. No
``.log`` extension handler needed; the entry is kept for
files explicitly named with that extension (none in the
suite today).

Build: daemon Python change only — no Java change, no
server-APK rebuild required if 0.52.19's Java is already
installed.

## 0.52.19 — diagnostic share is a zip archive (files stay separate) via ACTION_SEND

Refining 0.52.18's "single combined file" approach. APKs
travel via Android share intents fine — they're a single
file of MIME ``application/vnd.android.package-archive``
that's a zip under the hood. The same shape works for the
diagnostic bundle: zip up the snapshot + per-day daemon
logs into one ``application/zip`` and dispatch via
ACTION_SEND. Files stay separate inside the archive so
triage is one-grep-per-file, and large text logs compress
well (~5–10× for typical daemon log content).

**Daemon-side** (``_h_prepare_share_bundle``): replaces the
0.52.18 text concatenation. Writes a single
``azt_diagnostics_<stamp>.zip`` to
``$AZT_HOME/.shares/<token>/`` using ``zipfile.ZipFile`` +
``ZIP_DEFLATED`` (level 6). Archive contains:

- ``azt_snapshot_<stamp>.txt`` — the diagnostic snapshot.
- ``daemon-<tag>-YYYY-MM-DD.log`` — one entry per per-day
  log inside the retention window.

Returns ``{token, items:[{display_name, uri_path}]}`` with a
single item pointing at the .zip.

**Provider** (``AZTCollabProvider.mimeForPath``): adds
``.zip`` → ``application/zip``. Other extensions unchanged.

**Client** (``share_diagnostics_action``): now passes
``mime_type='application/zip'`` to ``share_files``. Signal's
ACTION_SEND filter advertises ``application/*`` in the
manifest, which covers ``application/zip``.

**Why ACTION_SEND, not ACTION_SEND_MULTIPLE.** Signal's
``ShareRepository.kt`` runtime resolver for SEND_MULTIPLE
hard-filters URIs to image/video MIMEs only — text and
zip are silently dropped. ACTION_SEND has no such filter.
Source verbatim-confirmed 2026-06-22:

```kotlin
.filterValues {
  MediaUtil.isImageType(it) || MediaUtil.isVideoType(it)
}
```

The single-vs-multi item routing in ``share_files`` (added
in 0.52.18) stays — single-item passes through ACTION_SEND,
multi-item through ACTION_SEND_MULTIPLE. Peer apps that ship
image/video bundles via SEND_MULTIPLE still get that path;
only the diagnostic-share composer downshifts to single +
zip.

**Trade-off vs 0.52.18's concat-text approach.** Concat-
text was opened inline by the receiver (text viewer); zip
requires a download + extract step. The win is that files
stay separate, so the support engineer's first action is
``unzip && grep`` instead of ``grep <section header>``. For
long retention windows (3+ days of logs) the zip is also
materially smaller on the wire.

**Build**: daemon Python change + AZTCollabProvider Java
change (one extension added). Java change needs server-APK
rebuild; Python rides incremental install.

## 0.52.18 — diagnostic share is one concatenated file via ACTION_SEND

Field-diagnosed via Signal's source code on 2026-06-22. The
0.52.13–0.52.17 thrash through URI authority, MIME types,
ClipData, pre-grants, and Java provider metadata was chasing
the wrong layer. The actual reason Signal kept rejecting the
share is in ``ShareRepository.kt``:

```kotlin
.filterValues {
  MediaUtil.isImageType(it) || MediaUtil.isVideoType(it)
}
```

**Signal's ``ACTION_SEND_MULTIPLE`` resolver filters per-URI
MIME types to images and videos only**, regardless of what
its manifest's intent-filter advertises (manifest says
``text/*`` SEND_MULTIPLE is fine, runtime says no). Every
diagnostic ``.log`` / ``.txt`` URI we ship via SEND_MULTIPLE
is silently dropped at this filter. Empty list →
``ResolvedShareData.Failure`` → ``ShareActivity.finish()``.
No amount of fiddling with our URI authority, MIME hints, or
ClipData shape can change this — the filter operates on the
receiver side after our intent is in their process.

Signal's ``ACTION_SEND`` (single attachment) resolver has
NO such filter; it accepts any URI as a generic file
attachment. So the fix is:

**Daemon-side**: ``_h_prepare_share_bundle`` now produces a
single combined ``azt_diagnostics_<stamp>.txt`` file that
concatenates the snapshot + per-day daemon logs with
``=== section ===`` headers (same shape the legacy
``share_log_file`` used). One file per share, served from
the same ``_shares/<token>/`` ContentProvider path.

**Client-side**: ``share_files`` now uses ``ACTION_SEND``
when ``items`` has exactly one entry, ``ACTION_SEND_MULTIPLE``
when more. ``share_diagnostics_action`` passes the single
combined URI → lands in ACTION_SEND branch.

**Reverted from 0.52.17**:

- ``AZTCollabProvider.getType`` returns ``text/plain`` again
  for ``.log`` / ``.txt`` (was speculative
  ``application/octet-stream``). The single-file route works
  fine with text/plain.
- ``share_diagnostics_action`` no longer passes
  ``mime_type='*/*'`` — defaults to text/plain which is what
  the single file actually is.

**Trade-off**: receivers (Gmail, etc.) that previously got
N distinct attachments now get one concatenated file. The
content is identical; triage requires scrolling to the right
``=== section ===`` header instead of opening a specific
attachment. For diagnostic-bundle use this is fine — a
support engineer typically greps the whole thing anyway.

The multi-attachment path (``share_files`` with >1 item) is
preserved in the code for peer apps that want to ship
image/video bundles where the SEND_MULTIPLE filter is
honoured.

**Sources** (from web research before this change):
- [signalapp/Signal-Android `app/src/main/AndroidManifest.xml`](https://github.com/signalapp/Signal-Android)
  — ShareActivity SEND_MULTIPLE filter advertises ``image/*``,
  ``video/*``, ``text/*``.
- [signalapp/Signal-Android `app/src/main/java/org/thoughtcrime/securesms/sharing/v2/ShareRepository.kt`](https://github.com/signalapp/Signal-Android)
  — the ``filterValues { isImageType || isVideoType }``
  runtime filter that rejects everything else.

## 0.52.17 — share-diagnostics MIME tuned for Signal's classifier

Field log 2026-06-22 17:58:44 on 0.52.16: Signal received our
ACTION_SEND_MULTIPLE with content URIs from
``AZTCollabProvider``, called ``getType`` on each URI, got
``text/plain`` back, then ``finish()``-ed without ever
calling ``query`` or ``openFile``. Pattern matches "Signal
routes per-URI text/plain to its in-message-text-snippet
branch, finds no EXTRA_TEXT, bails." Confirms the issue is
how Signal's classifier interprets ``text/plain`` content
URIs in a multi-file share, not anything about our URI
authority or grant chain.

Two co-ordinated tweaks for receivers (Signal) that classify
ACTION_SEND_MULTIPLE attachments by per-URI MIME:

**`AZTCollabProvider.getType` returns
``application/octet-stream`` for `.log` and `.txt`**
extensions (was `text/plain`). Other extensions unchanged
— JSON, XML, LIFT, images, audio still report their proper
text/structured/binary MIME. The diagnostic share files
benefit from being classified as "generic binary
attachments" so receivers' attachment-pipelines route them
correctly. Receivers that want to preview the files as text
still can (the bytes are valid UTF-8); the MIME hint
controls routing, not capability.

**``share_diagnostics_action`` dispatches with
``mime_type='*/*'`` at the intent level** (was
``text/plain`` by default). Mixed-attachment bundles
conventionally use ``*/*`` for the intent type;
per-attachment MIMEs come from ``ContentResolver.getType``
on each URI separately. Without this, receivers can route
the entire bundle to a "text-share" handler based on the
intent-level type, ignoring per-URI specifics.

**Java change requires a server-APK rebuild.** The
``getType`` change is in ``AZTCollabProvider.java``;
``adb install -r`` of a Python-only updated APK won't pick
it up. ``share_diagnostics_action`` is Python and rides the
normal install.

Expected behaviour after the rebuild + install lands:

- ``[share_files] entry: ... mime_type='*/*'`` instead of
  ``text/plain``.
- ``AZTCollabProvider: getType() ... mime=application/octet-stream``
  for both files.
- Signal accepts the share and shows the attachments in its
  compose draft.

If Signal STILL flashes-and-back with these new values, the
hypothesis is wrong and we need to look at what other
classifier checks Signal does — likely each per-URI's
extension or the URI path shape.

## 0.52.16 — debug bump (no code change)

Version bump only — forces a fresh rebuild + install so the
0.52.15 Java instrumentation (Log.i in
``AZTCollabProvider.getType / query / openFile``) is
guaranteed to be in the running APK for the next diagnostic
capture. No behaviour change beyond that.

## 0.52.15 — drop the Parcelable cast on EXTRA_STREAM URIs; instrument the Java provider

Two-part diagnostic patch on top of 0.52.14, after the field
log on 2026-06-22 16:24 showed Signal's ``ShareActivity``
opening and ``finish()``-ing itself within ~210-310ms before
displaying the compose UI. Per Signal source research:

> ``getUnresolvedShareData()`` for ``ACTION_SEND_MULTIPLE``
> calls ``intent.getParcelableArrayListExtraCompat(EXTRA_STREAM,
> Uri::class.java)`` and bails with ``IntentError.SEND_MULTIPLE_STREAM``
> when the result is null. The activity then ``finish()``-es
> from ``onCreate``. ShareActivity does NOT query our
> ContentProvider — it rejects the intent before getting to
> the file-loading stage.

That's the *exact* symptom we see. So 0.52.14's
``getType`` / ``query`` Java implementations may never have
been called — the failure is upstream of them, in how
``EXTRA_STREAM`` round-trips through the intent.

**Drop the Parcelable cast on ArrayList.add().** Pre-0.52.15
``share_files`` did:

```python
uris.add(cast('android.os.Parcelable', uri))
```

The ``cast`` was a jnius dispatching hint — the underlying
Java object is still ``Uri``, but the cast affects how jnius
records the apparent type for subsequent dispatch decisions.
On Android 13+, ``getParcelableArrayListExtraCompat(name,
Uri::class.java)`` does a runtime class check against the
parcel's recorded element type. If the parcel recorded the
items as ``Parcelable`` (via our cast) rather than ``Uri``,
the typed read filters them out and returns null — which is
exactly the IntentError.SEND_MULTIPLE_STREAM trigger.

Fix: just ``uris.add(uri)``. ArrayList.add(Object) accepts
the native Uri, the parcel records it as Uri, Signal's
typed read accepts it. Applied to both the URI-item branch
(the ``share_diagnostics_action`` path) and the legacy
MediaStore branch.

**Add ``Log.i`` lines to AZTCollabProvider.** ``getType``,
``query``, and ``openFile`` now each log their entry,
arguments, and outcome. Three things this confirms on the
next field test:

1. **Does Signal actually call our provider?** If we see
   ``[AZTCollabProvider] getType() uri=…`` after the share
   dispatch, Signal got past the EXTRA_STREAM parsing and
   reached the URI-validation stage. If we see *no*
   AZTCollabProvider logs, Signal bailed in
   ``ShareActivity.onCreate`` and never touched us.
2. **Does our getType return the right MIME?** The log
   includes the returned MIME so we can verify ``text/plain``
   for the .log and .txt files.
3. **Does Signal proceed to openFile?** If ``openFile`` is
   called, the URI grant chain is fully working and Signal
   has accepted everything up to and including our file
   metadata.

**Expected outcome on 0.52.15:**

- If the cast was the (only) problem: Signal's ShareActivity
  stays open with the two attachments displayed in the
  compose draft. AZTCollabProvider logs will show getType +
  query + openFile being called.
- If something else is also wrong: Signal still
  flashes-and-back, but now we know whether to investigate
  upstream (Signal didn't query provider — EXTRA_STREAM still
  broken) or downstream (Signal queried but rejected what
  we returned).

**Diagnostic command for next test:**

```
adb -s ZY22HFZR78 logcat | grep -iE \
  'share_files|share-bundle|AZTCollabProvider|securesms|IntentError'
```

Java change requires a server-APK rebuild for the Log.i lines
+ the cast-removal to land. Python change in ``share_files``
goes out via the normal incremental install.

## 0.52.14 — AZTCollabProvider implements getType + query so Signal validates the URIs

Field log 2026-06-22 on 0.52.13 (device ``3a0285ec``):

```
[share-bundle] prepared token='0c9ddd…' items=2
[share_files] item[0] using pre-staged uri='content://org.atoznback.aztcollab/_shares/…/azt_snapshot_…txt'
[share_files] item[0] landed via pre-staged uri
…
[share_files] startActivity returned (chooser dispatched)
```

Same Signal flash-and-back. URIs from our own authority, but
still rejected. 0.52.13's hypothesis ("same-authority is
sufficient") was wrong — Signal does more than authority
checking. It calls ``ContentResolver.getType(uri)`` and
``.query(uri, OpenableColumns, …)`` to validate the
attachment's MIME type and metadata before opening it.
``AZTCollabProvider`` returned null for both. Null type =
"unknown file" = Signal silent reject.

**Java fix in ``AZTCollabProvider.java``**: implement
``getType()`` and ``query()`` properly. Both route through
the same ``resolveAbsPath`` callback ``openFile`` already
uses, so a URI either has full metadata + is openable, or
all three fail consistently.

- ``getType(uri)`` maps the URI's path extension to a MIME
  type. Covers the file types this provider serves: text
  (``.txt`` / ``.log`` → ``text/plain``), structured data
  (``.json``, ``.lift``, ``.xml``), images (``.png``,
  ``.jpg``, ``.webp``, ``.gif``), and audio (``.wav``,
  ``.mp3``, ``.ogg``, ``.m4a``, ``.opus``). Unrecognised
  extensions still return null.
- ``query(uri, projection, …)`` resolves the URI's abs
  path, then returns a one-row ``MatrixCursor`` with
  ``OpenableColumns.DISPLAY_NAME`` and
  ``OpenableColumns.SIZE``. Honours the requested
  projection; defaults to ``[DISPLAY_NAME, SIZE]`` when the
  caller asks for "everything".

**Imports added**: ``android.database.MatrixCursor``,
``android.provider.OpenableColumns``.

**Requires a server-APK rebuild.** This is a Java change, not
a Python change — incremental Python install (``adb install
-r``) doesn't pick it up. Peer APKs are unaffected (they
don't host the provider).

**Expected behaviour on 0.52.14:**

- Tap Share diagnostics → Signal → recipient picker → Signal
  reads the metadata, queries getType, gets ``text/plain``,
  queries query, gets ``{display_name, size}``, opens the
  file via ``openFile``, attaches it to the compose draft.
- No flash-and-back.

**If Signal still rejects on 0.52.14**, the trace is
unchanged but the relevant logcat now includes Signal's own
validation logs — grep for ``MimeTypeMap`` or
``securesms`` around the share dispatch to see what it's
checking for next.

## 0.52.13 — Share diagnostics ships URIs from our own ContentProvider so Signal accepts them

Field log 2026-06-22 on 0.52.12 (device ``3a0285ec``) localised
the Signal flash-and-back precisely:

```
START u0 {act=android.intent.action.SEND_MULTIPLE typ=text/plain
  flg=0xb080001 cmp=org.thoughtcrime.securesms/.sharing.v2.ShareActivity
  clip={text/plain hasLabel(15) 2 items: {U(content)} {U(content)}}
  (has extras)} with LAUNCH_MULTIPLE from uid 10613
Displayed org.thoughtcrime.securesms/.sharing.v2.ShareActivity
  for user 0: +285ms
RestartModeController: determineRescueAppAfterAppFinishItself@1
  null pkgName=org.thoughtcrime.securesms
```

Signal's ``ShareActivity`` launched cleanly with our ClipData,
displayed for 285 ms, then ``finish()``-ed itself without
reading the URIs and without logging a permission error. This
is Signal's documented receiver-side security policy: its
attachment subsystem refuses MediaStore Downloads URIs and
only accepts URIs from the sender's own ContentProvider
authority (so a malicious app can't trick Signal into sending
arbitrary files via shared URIs). Gmail accepts MediaStore
URIs (different security model); Signal refuses them — that's
why ``Gmail accepts the bundle, Signal doesn't`` was the
diagnostic signal.

The fix re-routes share files through our own
``AZTCollabProvider`` (which already serves the daemon's LIFT
+ audio + CAWL files to peer apps via ``openFile``) so the
URIs the chooser dispatches carry the same authority that
initiated the share.

**New daemon RPC** ``POST /v1/diagnostics/prepare_share_bundle``:
- Creates ``$AZT_HOME/.shares/<token>/`` (token = 32-hex from
  ``secrets.token_hex(16)``).
- Writes the diagnostic snapshot as
  ``azt_snapshot_<stamp>.txt``.
- Copies each per-day daemon log inside the retention window
  (oldest-first ordering, matches ``get_daemon_log_files``).
- Sweeps stale share dirs (>1 h old) on every call.
- Returns ``{token, items:[{display_name, uri_path}]}``.

**``AZTCollabProvider`` route**: ``_resolve_share_path`` in
``android_cp/service.py`` accepts
``_shares/<token>/<filename>``  paths and maps them to
``$AZT_HOME/.shares/<token>/<filename>``. Read-only (peers
consuming the share intent only need to read). Token validated
with the same regex used for atomic-commit tokens; filename
validated to ``[A-Za-z0-9._-]{1,128}`` so a daemon bug can't
let a hostile filename slip through.

**``share_files`` extended** to accept items of shape
``{'uri': str, 'display_name': str}`` (in addition to the
existing ``path`` and ``content`` shapes). URI items skip
MediaStore entirely: the URI is parsed into a Java ``Uri``
and added directly to the intent's ``EXTRA_STREAM``
ArrayList and ClipData.

**``share_diagnostics_action`` rewired**: instead of building
``content`` items from ``get_diagnostic_snapshot`` +
``get_daemon_log_files`` (the 0.52.6–0.52.12 path), now calls
``prepare_share_bundle`` and builds URI items pointing at
``content://org.atoznback.aztcollab/_shares/<token>/...``.
``AZTCollabProvider`` is already declared with
``android:grantUriPermissions="true"`` in the server APK's
manifest (via ``p4a_hook.py:_inject_aztcollab_provider``), so
URI grants propagate correctly to the receiver.

**Defensive cleanups**:

- **``is_pending`` clear now works.** 0.52.12's
  ``values.put('is_pending', 0)`` silently failed with
  ``JavaException: No methods called put matching your
  arguments`` — Python ``int`` doesn't auto-box to
  ``java.lang.Integer``. Fixed with explicit
  ``Integer(0)``. The MediaStore path is no longer the
  default for share-diagnostics (URI items skip it), but
  callers using the legacy ``path``/``content`` shapes now
  get the proper clear.
- **``share_files`` instrumentation extended.** A new
  ``item[idx] using pre-staged uri=...`` line covers the
  URI branch end-to-end.

**Why this works for both surfaces.** ``share_files`` is the
single underlying function. The server-APK settings ``Share
diagnostics`` button, the picker's ``Share diagnostics``
button, AND any peer app's share helper all funnel through
the same code path. Fixing once fixes all three.

**Receiver compatibility expectations**:

- Signal: should accept (URIs from sender's own provider).
- Gmail: continues to accept (it accepts URIs broadly).
- WhatsApp / Telegram / etc.: should accept (same model).
- Bluetooth / Files / Drive: should accept (all read via
  ContentResolver; the provider serves the file just like
  MediaStore would).

If a future receiver rejects too, the next debugging step is
to look at the receiver-side logs around the time the URI
read fires. The ``[share_files]`` and ``[share-bundle]``
trace lines are end-to-end so the daemon log captures
everything from prep through dispatch.

## 0.52.12 — share_files clears MediaStore is_pending; _dlog also prints to logcat

Field log 2026-06-22 (device ``3a0285ec``, logcat capture of
the 0.52.11 instrumentation) localised the Signal flash-and-
back:

```
[share_files] item[0] MediaStore uri=content://media/external/downloads/1000017469
[share_files] item[0] landed: written=2389 bytes
[share_files] item[1] MediaStore uri=content://media/external/downloads/1000017470
[share_files] item[1] landed: written=87113 bytes
[share_files] insert phase done: landed=2 of 2
[share_files] pre-grant: queryIntentActivities returned n_targets=2
[share_files] pre-grant: granted to 2 packages: com.android.bluetooth,com.google.android.gms
[share_files] chooser: built; ClipData + grant flag attached
[share_files] startActivity returned (chooser dispatched)
```

Our side ran clean end-to-end. The flash-and-back happened
*after* ``startActivity returned`` — on Signal's process,
where we can't see. Two findings:

1. **``queryIntentActivities`` returned only 2 packages
   (Bluetooth + Google Play Services).** Signal isn't visible
   to our APK because we don't declare a ``<queries>`` block
   for ``ACTION_SEND_MULTIPLE``. The system chooser bypasses
   that visibility restriction (which is why Signal still
   appears in the chooser sheet), but our pre-grant pass
   never reaches Signal's package. That's a separate
   long-tail issue — see "Follow-up" below.

2. **Receiver-side silent read failure.** MediaStore Downloads
   URIs on Android Q+ default to ``is_pending=1`` —
   "owned by inserter, invisible to others." Until the writer
   clears the flag via ``resolver.update(uri, {is_pending:
   0}, ...)``, any other app's
   ``ContentResolver.openInputStream(uri)`` returns null /
   throws. Signal received the URI in ``EXTRA_STREAM``, tried
   to read, got nothing, flashed-and-back. This is the
   primary fix.

**Fix in ``share_files``.** Post-write per-item
``resolver.update(uri, {is_pending: 0}, null, null)`` with a
``_dlog`` trace line so the next bundle confirms the clear
landed. Canonical Android-docs pattern; matches the way every
MediaStore-write sample under
``developer.android.com/training/data-storage/shared/media``
ends the write.

**``_dlog`` also prints to logcat.** Previously
``_dlog`` only fired the ``log_diagnostic`` RPC (visible to
testers without adb after the next successful share). Now it
*also* calls ``print(..., flush=True)`` so a developer with
adb sees the trace in real time as the share dispatches — no
need to wait for a successful share to capture it. Sub-ms
cost per call; failure of either channel is swallowed.

**Follow-up to consider** (not in this patch): adding a
``<queries>`` block to the server APK's manifest_extras for
``ACTION_SEND`` and ``ACTION_SEND_MULTIPLE``, so
``queryIntentActivities`` returns the real share-target list
and the per-package pre-grant actually covers Signal /
Gmail / WhatsApp / etc. Bounded scope: declare what we
might share with, not "see all apps." Worth doing if the
IS_PENDING fix alone doesn't get us to a working share —
which the next field test will tell us.

## 0.52.11 — peer→daemon-log RPC + share_files trace instrumentation

Two field reports in a row (0.52.9, 0.52.10) showed
``Share diagnostics`` → Signal still flashing-and-back with no
visible explanation. The picker's ``print()`` calls land in
logcat, which is invisible on testers' devices without adb;
the always-on daemon log only captures the daemon process's
own output. So when the share path silently fails in the
picker process, there's nothing to look at.

**New RPC ``POST /v1/logging/append``.** Body ``{"tag": str,
"line": str}``. Server-side handler appends
``[<tag>] <line>`` to the daemon log via the existing
always-on tee. Lines capped at 1024 chars; longer payloads
truncated with a ``…[truncated]`` marker. Always returns
``ok=True`` so peer code paths aren't derailed by a stalled
write.

**New client wrapper ``log_diagnostic(tag, line)``** in
``azt_collab_client/__init__.py``. Wraps the RPC with
swallow-on-failure semantics — best-effort by design;
``ServerUnavailable`` returns ``False`` without raising.

**``share_files`` instrumented end-to-end.** ~14 ``_dlog``
call sites cover:

- entry (``item_count``, ``mime_type``, ``chooser_title``)
- platform check result
- jnius autoclass + activity + resolver readiness
- per-item insert outcome (display_name, has_path,
  content_bytes, MediaStore URI string, written byte count)
- bail when ``landed=0``
- intent built / extras attached
- ClipData build + attach outcome
- ``queryIntentActivities`` n_targets + per-package grant
  summary (first 16 listed, count of extras appended)
- chooser build + dispatch / startActivity return
- outer catch with the raising exception

Trace lines land in the daemon log alongside every other
``[<tag>]`` line. A tester's next successful share — or an
``adb pull`` of the daemon log — surfaces exactly where the
share path stalled or what receiver list / URI grants
actually happened.

No user-facing change. The instrumentation adds ~14 RPC calls
per ``Share diagnostics`` tap; each RPC is a loopback HTTP /
ContentProvider call (~sub-100ms), so total added latency on
the tap is roughly one second on Android — acceptable for a
debug build that immediately tells us what's wrong. If the
share fix actually works in 0.52.11 + later, the
instrumentation stays; it's cheap and the diagnostic value
is high.

**``log_diagnostic`` is general-purpose.** Anywhere in
``azt_collab_client`` (or sibling app code) that wants a
trace line in the daemon log can call it. Subsystem tag
goes in ``tag``, payload in ``line``. Replaces the
``_debug.first_try_log`` pattern when the trace needs to be
visible without adb.

## 0.52.10 — share_files: belt-and-suspenders URI-grant propagation for chooser + multi-URI

Field report 2026-06-22 on top of 0.52.9: tapping Share
diagnostics → Signal still flashed Signal's recipient picker
and returned to AZT immediately, with no draft created. The
0.52.9 ClipData-on-inner-intent fix wasn't enough on this
Android build — Signal's URI grant still didn't propagate
through ``Intent.createChooser`` to the receiver.

0.52.10 layers in the canonical Android-documented workaround
on top of the 0.52.9 ClipData fix:

1. **ClipData on the chooser wrapper** (in addition to the
   inner intent). Some Android builds don't forward the inner
   intent's ClipData to the wrapper.
2. **``FLAG_GRANT_READ_URI_PERMISSION`` on the chooser
   wrapper** (in addition to the inner intent). Same reason.
3. **Explicit per-package
   ``context.grantUriPermission(...)``** to every Activity
   whose intent-filter accepts the inner intent. Catches
   receivers that ignore the chooser-forwarded ClipData grant
   entirely. Bounded scope: only the URIs we created, only
   read permission, only packages whose filter matches the
   intent's MIME type — so this isn't a broad-spectrum grant.

The pre-grant query goes through
``PackageManager.queryIntentActivities(intent,
MATCH_DEFAULT_ONLY)`` so packages without DEFAULT category
support don't get spurious grants. Failure of any of these
defensive steps is non-fatal — the ClipData + flag chain may
still work for cooperating receivers, and a print to stderr
captures the failure mode for the next diagnostic share.

If after 0.52.10 the share-to-Signal flow still flashes-and-
back without a draft, the next debugging step is to add a
daemon-side ``POST /v1/logging/append`` RPC so the picker can
record its share-time trace into the always-on daemon log
(currently the picker's stderr → logcat → invisible to
testers without adb). Out of scope for this patch.

## 0.52.9 — share_files now attaches ClipData so multi-attachment shares actually reach the receiver

Field report 2026-06-22: tapping ``Share diagnostics`` →
``Signal`` flashed Signal's recipient picker and returned to
AZT immediately. No draft created. No daemon-log entry —
because the dispatch itself succeeded; the failure was on
the receiver side.

Root cause: ``Intent.ACTION_SEND_MULTIPLE`` wrapped in
``Intent.createChooser`` drops URI grants for many receivers
(Signal, modern Gmail) unless the URIs are also attached as
``ClipData``. The ``FLAG_GRANT_READ_URI_PERMISSION`` flag
covers ``EXTRA_STREAM`` URIs for some receivers but not all —
ClipData carrying the same URIs makes the grant propagate
through the chooser to the chosen target. Documented Android
quirk; affects multi-URI shares more than single-URI ones,
which is why the legacy single-blob ``share_log_file`` was
unaffected and the 0.52.6 multi-attachment ``share_files``
hit it.

Fix in ``azt_collab_client/ui/share.py:share_files``: track
the Python-side raw URI handles in a parallel list, build a
``ClipData`` from them (``ClipData.newUri`` for the first +
``ClipData.Item`` for each subsequent URI), set it on the
intent before wrapping in the chooser. Same MIME type as the
intent; label is a chooser-side hint and doesn't reach the
receiver.

No call-site change required — picker's ``Share diagnostics``
and daemon-settings's ``Share diagnostics`` both delegate to
``share_diagnostics_action`` which dispatches via
``share_files``. After this fix, Signal accepts the bundle
and shows N attachments in the compose draft as expected.

## 0.52.8 — Share diagnostics button restored to daemon settings (one action, two affordances)

0.52.7 removed the daemon-settings ``Share daemon log`` button
along with the logging toggle, on the argument that the
picker's ``Share diagnostics`` already covers the case. Field
review immediately caught the gap: when the user is already in
settings (just changed credentials, just toggled work-offline,
investigating something they configured here) and wants to
share what just happened, having to back out to the picker is
two extra taps and a context switch. The picker button is the
right always-visible affordance; ``the picker is the *only*
way`` is weaker.

0.52.8 puts a ``Share diagnostics`` button back into daemon
settings — same label as the picker's button, same
underlying action — and collapses the implementation so
"same label" actually means "same code path" instead of
"two functions that happen to look similar today and could
drift apart tomorrow."

**New helper.** ``azt_collab_client.ui.share.share_diagnostics_action(
on_error=…)`` owns the entire share-diagnostics composition:

1. Pull the snapshot (``get_diagnostic_snapshot``).
2. Pull the per-day daemon-log retention bundle
   (``get_daemon_log_files``).
3. Build the items list (snapshot first, then per-day logs
   with the daemon-supplied filename as ``display_name``).
4. Dispatch via ``share_files`` (``ACTION_SEND_MULTIPLE``).
5. Fallbacks: ``share_text`` for "daemon unreachable" (no
   snapshot AND no bundle) and "empty bundle" (daemon
   reachable but no log files yet).

The caller is just an ``on_error`` callback so each surface
can route the user-facing message to its own display channel
(popup for the picker, status label for daemon settings).

**Picker change.** ``ProjectPickerScreen.share_diagnostics``
now delegates to ``share_diagnostics_action`` — ~10 lines
instead of ~70, and structurally guaranteed to ship the same
payload as the daemon-settings button. No user-visible
behaviour change.

**Daemon-settings change.** A new ``RecBtn`` ``id:
share_diagnostics_btn`` sits below ``Restart server`` in the
``Collaboration AZT — Settings`` view, with
``SettingsScreen.share_diagnostics`` as a one-line delegation
to the same helper. Errors land in the existing
``service_status`` label (already used for restart feedback).

**KV comment refreshed.** The block introducing the
service-control row now reads "Service-control row… Share
diagnostics ships the same multi-attachment bundle the
picker's button ships — same label, same underlying
``share_files`` action, two affordances." instead of the
0.52.7 wording that argued for the button's removal.

No wire format change. No status code change. Picker users
see exactly the same payload as before. Settings users gain
the affordance back without the toggle (which stays gone).

## 0.52.7 — always-on logging; toggle and Share-daemon-log button removed

Phase 3 of the logging consolidation (closes the umbrella
``NOTES_TO_DAEMON.md`` 2026-06-20 from the recorder team).
The user-facing "Log server activity" toggle is gone — daemon
log capture is unconditional, anchored by the per-day rotation
+ 3-day retention from 0.52.5 and the multi-file Share path
from 0.52.6.

**Why the toggle was anti-diagnostic by design.** Support asks
for the daemon log *after* a failure. With a toggle, the
diagnostic is gone exactly when it would have been useful. The
privacy argument (don't accumulate a log file on devices that
don't need it) was mitigated by the explicit Share gesture
under the user's control. The disk-cost argument is now
mitigated by the 3-day retention. Field-validated cost: the
40 MB ``daemon-<tag>.log`` filed 2026-06-20 existed because
the user happened to leave the toggle on; half of all testers
do not.

**Daemon side (``azt_collabd/server.py``).**

- ``maybe_install_stdio_tee`` now installs unconditionally
  (the ``maybe_`` prefix is historical; preserved for ABI
  compatibility with ``server_apk/service.py`` and out-of-tree
  desktop launchers that call it by name).
- ``install_stdio_tee`` fires the start-of-day
  ``_dump_lan_debug_snapshot()`` when the file is empty pre-
  open (fresh-of-day or fresh install). ``_LogSession._rotate
  _locked`` fires it post-rotation on the new day's file.
  Together: every per-day file is anchored with a baseline-
  state snapshot. The lock becomes ``threading.RLock`` so the
  snapshot's ``print`` calls can re-enter the session safely.
- ``POST /v1/logging/daemon_log_to_file`` returns HTTP 410
  Gone with a typed body
  (``{"error": "endpoint_removed", ...}``) so any pre-0.52.7
  peer that calls it gets an explicit signal rather than a
  silent misbehaviour. Scheduled for outright deletion in a
  later release once peer apps have caught up.
- ``store.get_daemon_log_to_file`` / ``set_daemon_log_to_file``
  deleted — no remaining callers in-repo.
- ``_h_get_daemon_log`` / ``_h_get_daemon_log_files`` continue
  to return an ``"enabled": true`` field for client-wrapper
  shape compatibility; the value is now hard-coded since
  logging is always on.

**Client side (``azt_collab_client/__init__.py``).**

- ``set_daemon_log_to_file`` wrapper deleted from the public
  surface (also dropped from ``__all__``). Pre-0.52.7 callers
  hitting the 410 daemon endpoint will see ``None`` returned
  from any stale wrapper they have bundled — same shape as a
  transport failure, which their existing error path already
  handles.

**Settings UI (``azt_collabd/ui/app.py``).**

- "Diagnostic log" SectionLabel, "Log server activity:
  yes/no" buttons, "Share daemon log" button all removed.
  The picker's "Share diagnostics" supersedes the latter
  (full retention bundle as distinct attachments, vs the old
  single-blob with section breaks).
- The status label below "Restart server" survives (renamed
  ``daemon_log_status`` → ``service_status``) — restart
  feedback still uses it.
- Five orphaned methods deleted (``set_daemon_log_mode``,
  ``_daemon_log_enabled_state``, ``_refresh_daemon_log_state``,
  ``_refresh_daemon_log_buttons``, ``share_daemon_log``) plus
  the ``_refresh_daemon_log_state()`` call from ``refresh()``.

**Status-code help strings updated.** Two auto-sync help
strings in ``translate.py`` pointed users at the now-removed
``Settings → Diagnostic log → Log server activity = yes,
then Share daemon log`` path. Rewritten to ``Please tap
Share diagnostics on the project picker``. French
translations in ``fr.po`` updated to match. Affected codes:

- ``S.DATA_LOSS_RISK`` — peer wrote a file that won't be
  backed up.
- ``S.COMMIT_REPEATEDLY_FAILED`` — commit failed N times in a
  row.

Both target the same triage flow (user shares diagnostics so
support can investigate); the picker route is shorter
(2 taps vs. 4) and works the moment the failure happens
(no preceding toggle gesture required, since logging is
always-on).

**Recorder team integration.** The legacy
``azt_collab_client.ui.share.share_log_file`` shim is kept for
the recorder peer's existing share button. Recorder can
migrate to ``share_files`` at their leisure — the symlinked
client path makes the call site swap mechanical. Once the
recorder is on ``share_files``, the shim becomes deletable in
a future release.

**Unified logging umbrella closed.** All six sub-items of the
recorder team's 2026-06-20 ``NOTES_TO_DAEMON.md`` request now
shipped (prefix format + ms in 0.52.5, daily rotation +
retention in 0.52.5, multi-day fetch RPC in 0.52.6, multi-file
share dispatcher in 0.52.6, picker-button consolidation in
0.52.6, always-on logging + toggle removal in 0.52.7). The
``NOTES_TO_DAEMON.md`` queue is empty.

## 0.52.6 — multi-day daemon log RPC + multi-file share dispatcher; picker ships the full retention bundle

Phase 2 of the logging consolidation (continued from
``CHANGELOG`` 0.52.5 and the recorder team's
``NOTES_TO_DAEMON.md`` 2026-06-20). 0.52.5 made the daemon
write per-day files inside a retention window; 0.52.6 makes
peers able to *bundle and ship* the whole window in one
gesture.

**New RPC** ``GET /v1/logging/daemon_log_files`` returns the
per-day daemon logs inside the retention window:

```json
{
  "ok": true,
  "files": [
    {"date": "2026-06-18", "filename": "daemon-7aeb3fac-2026-06-18.log",
     "content": "…", "bytes": 4880736},
    {"date": "2026-06-19", "filename": "daemon-7aeb3fac-2026-06-19.log",
     "content": "…", "bytes": 6112048},
    {"date": "2026-06-20", "filename": "daemon-7aeb3fac-2026-06-20.log",
     "content": "…", "bytes": 1024}
  ],
  "retention_days": 3,
  "enabled": true
}
```

Ordered oldest-first so a tester reading top-to-bottom gets
chronological flow. Each ``content`` is daemon-side
tail-truncated to the last 256 KB (same cap as the legacy
single-file ``GET /v1/logging/daemon_log``). Files outside the
retention window are dropped from the response even if still on
disk (covers a race where retention was lowered between the
prune sweep and the call).

**New client wrapper** ``get_daemon_log_files()`` in
``azt_collab_client/__init__.py``. Same shape, ``None`` on
transport failure. Empty ``files`` list when the toggle has
never been enabled / no per-day file exists yet.

**New share helper** ``share_files(items, …)`` in
``azt_collab_client/ui/share.py``. Inserts each item into
MediaStore Downloads, gathers the resulting URIs into a
``java.util.ArrayList<Uri>``, and dispatches an
``Intent.ACTION_SEND_MULTIPLE``. Items can carry either
``{'path': str, 'display_name': str}`` (streamed in 64 KB
chunks from disk) or ``{'content': str|bytes, 'display_name':
str}``. Partial coverage is fine — a single missing path /
MediaStore-insert failure logs a warning and the share
continues with the remaining items, so a stale retention sweep
racing the share doesn't lose the rest of the bundle.

**Picker rewires.** ``ProjectPickerScreen.share_diagnostics``
now:

1. Pulls the diagnostic snapshot (``get_diagnostic_snapshot``).
2. Pulls the retention bundle (``get_daemon_log_files``).
3. Builds the items list: one item per snapshot + one item per
   per-day log file with the daemon's basename
   (``daemon-<tag>-YYYY-MM-DD.log``) as the display name.
4. ``share_files(items)`` — single ``ACTION_SEND_MULTIPLE``
   dispatch with one attachment per file.

User-visible benefit: a tester sharing the bundle now gets N
distinct attachments instead of one mega-blob with section
breaks. Email and Signal both handle ``ACTION_SEND_MULTIPLE``
cleanly — opening the bundle is "tap each attachment" instead
of "find the section marker."

Daemon-unreachable fallback path stays — if the snapshot AND
``get_daemon_log_files`` both fail, the same ``share_text``
operator message fires as before. New empty-bundle path
("daemon reachable, but no files inside the window yet") emits
a typed "reproduce the issue first" message instead of silently
opening a zero-attachment chooser.

**``share_log_file`` is kept** as a legacy entry point for the
daemon-settings ``Share daemon log`` button. Behaviour
unchanged; it still bundles the single concatenated blob with
``=== current session ===`` headers, still skips the now-
nonexistent ``<path>.prev`` silently. Phase 3 removes the
settings button (along with the toggle); at that point the
shim can be deleted.

**Recorder-team integration.** The new ``share_files`` and
``get_daemon_log_files`` are the API surface the recorder team
asked for in ``NOTES_TO_DAEMON.md``. Recorder's
peer-side bundle can now call ``get_daemon_log_files`` from
this client (already symlinked into their tree) and assemble a
combined recorder + daemon items list passed to
``share_files``.

**Translation coverage.** Five new msgids added to ``translate.py``
and the French ``.po`` (``Files: {names}``, ``No files to share.``,
``Could not share files:\n{error}``, ``AZT diagnostics``, and
``No diagnostics available yet. …``). The translation-coverage
test continues to pass for these — preexisting drift on other
strings is tracked separately.

## 0.52.5 — daemon log files rotate daily, retained for 3 days, with ISO-ms line prefix

Field log nml 2026-06-20 caught a 40 MB daemon log file —
``daemon-<tag>.log`` had been growing without bound across
weeks of daemon respawns. Single-file naming + append-on-respawn
+ a user-controlled "Log server activity" toggle that nobody
remembered to turn off meant the file accumulated as long as
the toggle stayed on. Even the existing ``.prev`` rotation only
fired on a toggle-on gesture, which never happens in a long
collection trip.

Phase 1 of the logging consolidation (per peer recorder team's
``NOTES_TO_DAEMON.md`` 2026-06-20):

**Per-day filename.** The daemon log file is now
``daemon-<tag>-YYYY-MM-DD.log`` (or ``daemon-YYYY-MM-DD.log``
when peer_id isn't readable yet). The date is resolved at write
time from the local clock, so a write at 23:59:59 lands on
yesterday's file and the next write at 00:00:00 lands on
today's. Rotation is lazy — checked on every write, costs one
``_time.strftime`` per write batch when no rotation is needed,
one ``close`` + ``open`` + retention sweep when the date
changed.

**3-day retention** (configurable via ``logging.retention_days``,
default 3, min 1). On every daemon-startup tee install AND on
every midnight rotation, files older than the window are
unlinked. Today's file is never pruned — the retention math
keeps the last *N* distinct dates including today, with a
belt-and-suspenders explicit guard so a clock-skew edge case
can't wipe the live file.

**ISO-with-ms line prefix.** Per-line stamp format changes
from ``[HH:MM:SS <tag>] `` to
``[YYYY-MM-DD HH:MM:SS,mmm <tag>] `` — matches Python
``logging``'s default fractional-second format, and aligns with
the recorder team's parallel format change so a unified
``grep`` across recorder + daemon files in a triage bundle
takes one expression instead of two.

**``.prev`` rotation mechanism gone.** Per-day naming
supersedes it. The picker / settings UI Share buttons still
pass ``log_path + '.prev'`` to ``share_log_file``; that file
won't exist, but ``_bundle_log_blob`` silently skips a missing
``prev_path``. Phase 2 will replace those calls with a
multi-day bundle helper.

**Code shape change.** ``_StdioTee`` no longer owns the file
handle directly. A process-wide ``_LogSession`` owns it, both
stdio tees delegate writes through the session, and the
session handles rotation under its own lock so concurrent
stdout+stderr writes can't both observe a date change and both
rotate. The shared start-of-line state (formerly a list-of-one
``sol_state``) becomes a single session attribute.

What this **doesn't** touch (deferred to Phase 2 / Phase 3):

- The user-facing toggle is still here. ``install_stdio_tee``
  still respects ``truncate=`` (now informational only — the
  truncation step is obsolete) and ``_h_set_daemon_log_to_file``
  still works.
- ``daemon_log_path()`` still returns *today's* path, so the
  read RPC and the existing Share buttons keep working
  unchanged. Phase 2 adds ``get_daemon_log_files()`` for the
  multi-day case.
- Stranded pre-0.52.5 ``daemon-<tag>.log`` / ``.log.prev``
  files from previous releases are NOT auto-deleted —
  conservative: those may contain diagnostic content a user
  wants; manual removal is fine.

## 0.52.4 — topic-push pre-seeds oversize blobs to side refs; scheduler logs wan_backoff skips

Field log nml 2026-06-18 (both REDMI phones, sha
``fcd30318c03b`` LAN-converged) showed the topic-push reaching
its architectural floor: 2035 commits ahead of github, chunk
ladder going 50 → 25 → 12 → 6 → 3 → 1 commits, every rung 408ing,
and the ``chunk_n=1`` rung's pack at 4.3 MB — bigger than the
3 MB ``sync.commit_pack_byte_budget``. Existing code surfaced
``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` and gave up because the
ladder has no rung below "one commit." For an audio-heavy
project a single recording-burst commit can dwarf the budget
indefinitely, and a faster connection / LFS migration aren't
always available to the field worker.

Fix: extend the ladder by one tier *below* the commit. When the
``chunk_n=1`` bail is about to trip, the topic-push now
pre-seeds the commit's blobs onto the server via synthetic
single-purpose commits at
``refs/heads/azt-blob-seed-<sha-prefix>``, sized-gated against
the same budget. Once the blobs are on the server, the original
``chunk_n=1`` push only needs to carry the commit + tree in its
pack (KB, not MB) — git's pack negotiation deduplicates against
any reachable server ref, so updating
``refs/remotes/origin/azt-blob-seed-*`` locally after each
batch push automatically excludes those blobs from the next
pack.

Properties of the new sub-commit tier:

- **Deterministic batching.** Blobs sorted by SHA, then
  greedy-packed against ``budget × 0.7`` per batch (leaves
  headroom for compression variance). The synthetic commit's
  author / committer / timestamp / message are all fixed
  constants, so a re-run after partial completion (daemon
  respawn, mid-batch network drop) produces the same commit
  SHAs → same side-ref names. Re-pushing a ref the server
  already has at the same SHA is a zero-byte no-op on github,
  so the partial state on the server is a feature: idempotent
  recovery without external state.
- **One pre-seed attempt per topic-push call.** If pre-seed
  succeeds and the subsequent ``chunk_n=1`` push still fails,
  the call bails — the next drain re-enters the topic-push and
  the side-ref-aware blob enumeration picks up where we left
  off (already-uploaded blobs are excluded). No infinite loop
  when the network is fundamentally too slow even for tiny
  packs.
- **Lazy crash-tolerant cleanup.** Side refs are not deleted
  inside the same sync RPC. At the top of every topic-push
  call, ``_sweep_orphan_preseed_refs`` walks
  ``refs/remotes/origin/azt-blob-seed-*`` and deletes server-side
  any side ref whose blobs are all reachable from main's tree
  (the audio-files-are-additive invariant makes "in HEAD's
  tree" sufficient — no ancestor walk needed). If the daemon
  is killed between Phase C success and the cleanup sweep, the
  next topic-push run sweeps any orphans on its way in. Net
  effect: side refs accumulate at most until the next push,
  then drop out; the steady-state github branch list stays
  clean.
- **New terminal status ``BLOB_EXCEEDS_BUDGET``.** Fires only
  when an individual blob (uncompressed) is larger than the
  budget — pre-seeding can't split a single blob across batches.
  Params: ``blob_sha`` (hex prefix), ``blob_bytes``,
  ``budget_bytes``. Routes to ``PUSH_FAILED`` like the
  whole-commit case, so peers without specific routing for the
  new code handle it uniformly; the specific offending file
  surfaces in the daemon log and the picker translation for
  the rare case where it does fire.

The existing ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` becomes
much rarer — it now only fires when pre-seeding had a
transient network failure (so we fall through to bail and
retry next drain), or when the post-pre-seed ``chunk_n=1``
push exhausts its persistence budget. The audio-overcommit
case that motivated this work is structurally fixed.

### Companion: ``[scheduler] drain skipped`` log line

The same field log showed the other side of the same problem:
``7aeb3fac``'s drain loop fired ``[scheduler] drain pushes:
['nml']`` every few minutes for two hours, but no
``sync-trace`` lines followed any of them, because that
device's ``wan_backoff`` curve (from earlier-day
``NotGitRepository`` failures) was suppressing the actual
push attempts. The drain loop was silently doing the right
thing; the silence made it look broken.

``scheduler.py`` now emits a rate-limited ``[scheduler] drain
skipped: <lang> wan_backoff next=<iso> (in <duration>,
<n> consecutive failure(s))`` line for each project the curve
suppresses, cached on ``(langcode, next_due_at)`` so a stable
backoff state logs once and re-logs only when the curve moves.
Brings drain-loop diagnostics in line with the existing
``[wan-unshared]`` / ``[lan-unshared]`` rate-limited emissions.

No wire-format change for either: new ``BLOB_EXCEEDS_BUDGET``
is opt-in (peers without routing for it fall through to
``PUSH_FAILED``), and the scheduler log line is daemon-side
only.

## 0.52.3 — wan_unshared walks from HEAD when origin is set but never reachable

Field log nml 2026-06-18 (devices ``7aeb3fac`` aztobt1-sudo and
``db033cd4`` aztobt2-ui, both LAN-converged at SHA
``fcd30318c03b``) exposed the
[masking sync-indicator follow-up](docs/Publish_errors.md) that
was deferred out of the 0.50.x sync rebuild. ``7aeb3fac``'s
GitHub App is installed on ``aztobt1-sudo``, not ``aztobt2-ui``,
so every fetch against the ``aztobt2-ui/nml.git`` origin returns
``NotGitRepository`` — no tracking ref ever lands. Pre-0.52.3,
``_wan_unshared`` hit ``KeyError`` on the missing tracking ref
and returned ``0 (OK-on-uncertainty)``, so the picker rendered
"OK +2" while 2035 commits were stuck on the device with no
github backup possible.

Fix in ``azt_collabd/repo.py``: when the origin URL is configured
but no tracking ref exists, walk the local tree from HEAD and
return the real count — the same answer the LAN-only branch
already gave. The honest reading of "no tracking ref despite
configured origin" is "we have no evidence anything reached
github." Two subcases collapse to the same answer and that's
fine: a never-fetched-yet project self-corrects on the first
successful fetch (the tracking ref appears and the next call
falls through to the existing walk-excluding-tracking branch),
and a never-can-fetch project (wrong-account App install,
revoked credentials) keeps reporting the real backlog until the
user fixes the access problem.

The diagnostic line also changes — the new ``[wan-unshared]``
emission distinguishes itself from the LAN-only / no-origin case
by reporting both the URL and the never-fetched-or-fetch-always-
failed condition:

```
[wan-unshared] '/…/nml': branch='main' local='fcd30318c03b':
  origin URL configured (https://github.com/aztobt2-ui/nml.git…) +
  no tracking ref (never-fetched or fetch-always-failed) →
  walk-from-HEAD = 2035
```

so picker support can still tell "all caught up" (returns 0
through the tracking-ref-equals-local branch) from "no github
backup" (returns N via this new branch) just by reading the log.

No wire-format change; ``project_status.wan_unshared`` is still
an int. Picker rendering is unchanged — the same "+N" / "OK"
contract that already worked for LAN-only projects now also
works for github-configured-but-unreachable projects.

Resolves the not-yet-shipped follow-up tracked in
[``docs/Publish_errors.md``](docs/Publish_errors.md) §
"Related follow-ups" and the deferred fix noted in the 0.50.x
CHANGELOG under "The masking sync-indicator
(OK-on-uncertainty firing on NotGitRepository) is a separate
fix to plan."

## 0.52.2 — corrupt projects.json moves aside (not just zero-byte)

0.52.1 cleared **zero-byte** ``projects.json`` so ``register()``
could re-seed it. Field repro on 0.52.1 (db033cd4, boot
2026-06-17 17:47) caught the next failure mode: an **839-byte**
``projects.json`` whose first byte is non-JSON — almost
certainly the ext4 power-fail / write-fail pattern where the
inode size update lands but the data blocks don't (file is the
right size, contents are zero bytes or garbage). 0.52.1's
size-only guard skipped this case, the repair pass hit
``register refused — projects.json could not be parsed`` and
halted with ``scanned=2 candidates=2 repaired=0 failed=1``.
User's two on-disk projects (``baf`` with 1670 audio files,
``nml`` with 71) stayed orphaned despite intact working trees.

Fix: detect corruption by **attempting the parse**, not by
inspecting the byte count. When ``json.load`` raises, move the
corrupt file aside to ``projects.json.corrupt-<YYYYMMDD_HHMMSS>``
and let the next ``_load_raw`` return ``{}`` (file missing →
safe empty-dict baseline). The orphan re-registration below
then re-seeds ``projects.json`` from the on-disk working_dirs.

The move-aside (instead of delete) preserves the original
content for forensic inspection — if anything WAS salvageable
in the 839 bytes (it almost certainly isn't, for ext4 power-
fail patterns, but the option is there), support can recover
it from the ``.corrupt-*`` sibling. The new file is built
purely from the filesystem scan, so per-project metadata that
lived only in the registry (last_sync, last_commit, repo_slug,
vernlang) is lost — fields default per ``projects.Project``,
and the user's first sync will repopulate ``last_sync`` /
``last_commit`` from the actual git state.

## 0.52.1 — orphan-repair scans the right directory + clears zero-byte projects.json

Field log baf 2026-06-17 (device ``db033cd4``, AZT Collab 0.51.0)
caught two flaws in the 0.52.0 repair pass before it ever ran in
production:

1. **Wrong scan directory.** 0.52.0 scanned ``$AZT_HOME/*/`` and
   explicitly skipped ``projects/`` as a "system dir". But
   working_dirs live under ``$AZT_HOME/projects/<langcode>/``
   (per ``lan_clone._project_dir``; confirmed by the existing
   ``[server] list_projects: registry empty;
   projects_dir=...``  diagnostic). On the canonical layout the
   0.52.0 scan would find zero orphans even when there were
   two right there. Fix: scan ``$AZT_HOME/projects/`` instead;
   home-root listing is preserved in the diagnostic snapshot as
   context but no longer scanned for orphans (avoids false
   positives on ``cawl/``, ``peer.crt``, etc.).

2. **Zero-byte projects.json blocks register().** The
   ``[collab.projects] load failed: Expecting value: line 1
   column 1 (char 0)`` line in the field log = ``json.load`` on
   an empty file → ``_load_raw`` returns ``_LoadFailed`` →
   ``projects.register`` refuses with ``RuntimeError``. The
   repair pass would crash out at the first orphan. Fix: delete
   a zero-byte ``projects.json`` at the start of the repair
   pass — that's unambiguously a crash-mid-write artifact (no
   daemon code path ever writes an empty file deliberately;
   ``_save_raw`` atomically renames a fully serialised dict),
   so removing it lets the next ``_load_raw`` return ``{}`` and
   ``register()`` re-seed the file from the orphan scan.

   Non-zero corrupt ``projects.json`` files are still left
   untouched — those could be truncated-but-recoverable, and
   the repair halts loudly with ``manual recovery needed``
   rather than risk destroying salvageable content.

Same field log also documents the use case: zero-byte
``projects.json``, two on-disk working_dirs (``baf``, ``nml``),
``[collab.projects] load failed`` lines repeating ~25 / minute
as every status poll re-hits the same parse failure. After
0.52.1 boot: the empty file is removed, both projects get
re-registered with their langcode + lift_path + `.git/config`
remote_url, the spam stops, the picker shows two projects, and
the user's collected data is reachable again.

## 0.52.0 — picker diagnostics + boot-time orphan-project auto-repair

Field report: a user ends up on the picker (recorder OR server APK)
with no projects to choose from, while the AZT Collab APK reports
2.7 GB of storage in use and the user is certain they had a
project with collected data. The existing Share-daemon-log
affordance is gated behind project selection on the recorder side
(its gear navigates to recorder settings, which doesn't host the
button) and on the server-APK side requires a daemon-log-to-file
toggle that's typically off until support has walked the user
through enabling it. Net: the diagnostic path is unreachable
exactly when it's most needed.

Two surfaces address this:

**Picker "Share diagnostics" button.** Always-visible affordance
at the bottom of ``ProjectPickerScreen`` (lands in every host via
the symlinked client). Ships a daemon-built registry/filesystem
snapshot — ``$AZT_HOME`` path, ``projects.json`` size / mtime /
parsed entries (per-entry: ``working_dir`` exists, ``lift_path``
exists, ``remote_url``), on-disk subdir scan with ``.git`` / LIFT
/ audio presence per dir, which subdirs are registered vs orphan,
``lan.allow_sync`` setting, peer-id short tag. Backed by a new
``GET /v1/diagnostics/snapshot`` endpoint (text response, always
succeeds — per-section try/except so a daemon issue surfaces as
an inline marker rather than a 500). The snapshot prepends
whatever's in the daemon log file via a new ``prefix_text``
parameter on ``share_log_file``, so the snapshot is the
diagnostic payload even when the log-to-file toggle was never
enabled.

**Boot-time auto-repair of orphan working directories.** New
``diagnose_and_repair_registry_on_startup`` (in ``repo.py``,
wired into both desktop ``serve()`` and Android
``server_apk/service.py:main()`` startup paths per the
dual-entry-path lesson). Two passes:

1. Log the diagnostic snapshot to stderr — ``[diag]``-prefixed
   line per snapshot line. Every daemon startup now leaves a
   snapshot trail in the log, so we have a record even when the
   user never grabs one manually.

2. Scan ``$AZT_HOME`` for subdirectories that contain both
   ``.git/`` and a ``*.lift`` file but aren't keyed in
   ``projects.json``. Each orphan is auto-registered via
   ``projects.register`` with the directory name as langcode,
   the first ``.lift`` file as ``lift_path``, and
   ``remote.origin.url`` from ``.git/config`` as ``remote_url``.
   The orphan working tree is untouched.

**Strict no-removal invariant.** Repair is append-only — the
boot pass *adds* missing registry entries; it never removes,
relocates, or rewrites existing ones. A corrupt non-empty
``projects.json`` halts the repair pass (``projects.register``
already refuses with ``RuntimeError`` when ``_LoadFailed`` is
in play, so we can't clobber recoverable corruption), and the
function logs a loud ``manual recovery needed`` line so the
user/support has a clear pointer for next-step recovery. The
``[diag-repair]`` summary line always emits per
``feedback_always_emit_summary``.

Client surface (``azt_collab_client.get_diagnostic_snapshot``)
re-exports through ``__all__``. French msgstrs land for the new
button + error strings so the picker doesn't render blank
labels in fr locale.

## 0.51.0 — bloated-m4a anomaly traced to recorder MOOV-duplication bug

Field investigation of 42 audio files clustering at exactly
1,944,633 bytes traced the cause to a malformed MP4 container,
not a runaway recording or a collab-side write path. ffprobe on
a representative file (`0633_spill.m4a`):

```
[mov,mp4,m4a,3gp,3g2,mj2 ...] Found duplicated MOOV Atom. Skipped it
Duration: 00:00:01.64, … bitrate: 9470 kb/s
Stream #0:0[0x1](eng): Audio: aac (LC) (...), 48000 Hz, mono, fltp, 255 kb/s
```

The actual audio stream is ~52 KB (255 kb/s × 1.64 s); the
remaining ~1.9 MB is duplicated container metadata. The bug
affects both mono and stereo recordings — same byte-exact
footprint — which points at a **shared file-finalization path**
in `azt_recorder` writing MOOV twice (or flushing a fixed-size
pre-allocation buffer verbatim regardless of audio length).

Not an `azt-collab` fix; flagged on the recorder backlog. The
0.50.63 `LARGE_AUDIO_FILE_DETECTED` (now gated to `audio/`)
remains the right collab-side safety net — it will continue to
flag any new instances as they appear in commits.

Bulk cleanup is `ffmpeg -c copy -map 0:a` per file, which
re-muxes the audio stream into a fresh container, dropping the
duplicated MOOV and any padding (1.94 MB → ~52 KB per file).

No code change in this release; version stamp closes out the
diagnostic session.

## 0.50.63 — LARGE_AUDIO_FILE_DETECTED gated to audio/ paths

The data-quality check in `_commit_step_locked` fires
`LARGE_AUDIO_FILE_DETECTED` for any file in the just-made commit
above `data_quality.large_audio_byte_threshold` (default 512 KB).
The threshold is sized for audio — designed to catch "user
recorded a phrase by mistake" in a word-list-elicitation context
where typical recordings top out around 250 KB.

Field repro: `images/0607_smoked.png` (1.6 MB) tripped the
warning. That's a perfectly normal CAWL photograph — CAWL ships
~110 files in the 1–1.5 MB range and ~35 in the 1.5–2 MB range,
all legitimate content. The status code firing on them was
misleading (it's not an audio file, and there's nothing wrong
with the image) and noisy in the daemon log.

Fix: gate the check on `path.startswith('audio/')`. Status code
keeps its name (`LARGE_AUDIO_FILE_DETECTED` is now accurate; pre-
0.50.63 it was a misnomer). Image-side check intentionally not
added — there's no analogous user-error mode for images because
peers consume CAWL rather than create it on the device.

Log line also relabeled from `[data-quality] large file in
commit` to `[data-quality] large audio in commit` for symmetry.

## 0.50.62 — notify on auto-adopt so settings UI refreshes

Field repro of 0.50.59 + 0.50.60 working end-to-end revealed a
UI-refresh gap: the daemon's `_handle_share_offer` synchronously
updated `Project.remote_url` (registry) and `.git/config`'s
`[remote "origin"]` (working tree), but the settings UI on the
peer device kept showing the cached `publish candidate:
remote_url=''` until the user closed and reopened the app.

Cause: the settings UI's `_refresh_publish_row` reads `Project`
and `project_status` snapshots from its own cache. Daemon-side
changes are only visible after the UI re-polls — which the
picker does on a notify-status-changed event or on
screen-navigation. The auto-adopt path didn't fire any
notification.

Fix: after a successful synchronous adopt-origin, fire
`android_cp.notify.notify_project_changed(langcode)`. Matches
the existing pattern in `lan_listener`'s post-receive path
(line 1293) and `_commit_step_locked`. Observers re-poll
immediately; the settings UI shows the adopted URL within one
poll cycle.

## 0.50.61 — revert 0.50.58's PUSHED-fanout (superseded by 0.50.60)

URL sharing belongs to two events, not three:

- **URL creation/change** → Publish (`_h_init_project` PUSHED, since 0.50.52)
- **Peer becomes reachable** → mDNS arrival (`_fire_arrival`, since 0.50.60)

0.50.58 also fired `_spawn_publish_fanout` from `_h_project_sync`
and the scheduler drain on PUSHED, on the theory that "every
successful push is a fresh opportunity to broadcast the URL."
That was wrong — the URL doesn't change between publishes, so
re-firing on every push duplicates whatever publish-fanout
already accomplished (or didn't):

| Case | Publish (0.50.52) | Arrival (0.50.60) | Sync/drain push (0.50.58) |
|---|---|---|---|
| Publish, peer online & arrived | ✅ delivers | (no transition) | duplicate of publish-fanout |
| Publish, peer offline | misfires | ✅ delivers on reconnect | also misfires |
| Sync (URL unchanged), peer online | (URL unchanged) | (no transition) | redundant "ping" |
| Daemon respawn race | misfires | ✅ recovers | misfires (same root cause) |

In the steady-state happy path the URL was already announced at
publish time; nothing has changed to re-announce. In the missed-
publish case arrival-fanout already handles the recovery. The
only marginal value of sync/drain-fanout is the
brief-network-glitch-during-publish case where mDNS didn't
register a departure (sub-TTL blip) — and even there, clicking
Publish again is a working recovery path.

The cost wasn't free: on every daemon respawn, the scheduler's
first drain hits PUSHED before mDNS has refreshed
`static_endpoints`, the fanout misfires against stale entries,
and the log gets a flurry of `[lan-push] POST https://<stale_ip>
share_offer failed` lines that aren't actionable. Removing those
clears the log noise without losing functionality.

Removes:
- `_spawn_publish_fanout` call in `_h_project_sync` (server.py)
- `_spawn_publish_fanout` call in scheduler drain success path
  (scheduler.py)

Kept:
- `_h_init_project`'s call (Publish fanout — the URL-creation
  event)
- `_fire_arrival`'s call (arrival fanout — the peer-becomes-
  reachable event)

## 0.50.60 — share_offer on peer arrival (fanout-vs-mDNS race)

0.50.58 made `_spawn_publish_fanout` fire on every successful
push; 0.50.59 made the receiver auto-adopt. Field repro today
showed those are necessary but not sufficient — there's a race
between fanout timing and mDNS resolution:

```
[scheduler] drain push 'en-TH-x-anna' codes=['PUSHED']                 ← fanout fires
[lan-push] POST https://192.168.150.159:40025/v1/lan/share_offer        ← stale port
           failed: Connection refused
                  ─── 3 seconds later ───
[lan-discovery] nsd resolved '841d43a8' → 192.168.150.159:42503 [arrival]
[lan-push] '841d43a8' already at '5368e45b96e9' — no-op                 ← endpoint fresh now
                                                                          but fanout already gave up
```

On a daemon respawn, the scheduler drain hits PUSHED immediately,
fanout fires against `static_endpoints` loaded from disk (which
hold the peer's *previous* bind), every POST 404s/refuses, and
no retry is queued. mDNS catches up seconds later, `_record`
updates `static_endpoints` to the current port — but the
share_offer is already lost. The git-object sweep that fires on
arrival (`_fire_arrival` → `sweep_peer`) works correctly with
the now-fresh endpoint, but git objects don't carry URL
metadata.

Fix: extend `_fire_arrival` to send a `share_offer` for every
shared project that has a `remote_url`, immediately after
`sweep_peer`. Arrival is the precise moment we *know* the
endpoint is current — piggybacking the share_offer there makes
URL convergence reliable.

```python
# _fire_arrival worker (new tail after sweep_peer):
for langcode in entry.shared_projects:
    project = projects.get(langcode)
    if project and project.remote_url:
        send_share_offer(peer_id, langcode, project.remote_url,
                         vernlang=project.effective_vernlang())
```

Peer-side dedup keeps this cheap: `_handle_share_offer` no-ops
when URLs match (already-known fact), so a peer that already has
the URL just logs `dispatch='noop'`. A peer that doesn't (the
case this fixes) auto-adopts via the 0.50.59 logic. Either way,
no user-visible churn from the extra announcements.

Log line confirming on the sender side:

```
[lan-discovery] arrival announced N URL(s) to <peer_id>
```

## 0.50.59 — auto-accept ADOPT_ORIGIN

Pairs with 0.50.58. Now that `send_share_offer` re-fires on every
successful push, paired peers receive the URL more reliably — but
the receive side still stashed a `KIND_ADOPT_ORIGIN` pending
decision and waited for the user to tap "accept" in the picker.
For the **unambiguous case** (peer has the project locally via
LAN clone, has no `remote_url`, and an incoming offer carries
one), that's pointless friction: the user already consented to
the share by pairing, and the URL is the only natural completion
of that pairing.

Fix: `_handle_share_offer`'s "local has project, local URL
empty, incoming URL present" branch now applies the adopt-origin
synchronously — `set_remote_url(langcode, url)` plus
`set_remote_origin_url(working_dir, url)` — and reports back
`dispatch='adopted'` to the sender. Logs as:

```
[lan-listener] share-offer from <peer> for <langcode> auto-adopted origin <url>
```

If the apply fails (registry write error, lock contention,
working_dir missing), falls back to the pre-0.50.59 stash so the
user has a manual recovery path via the picker's pending-decision
flow.

**`KIND_REMOTE_CONFLICT` stays a user decision.** When the local
project has a *different* URL than the incoming one (two
devices each published independently while apart, now
reconverging on LAN), the daemon genuinely can't tell which
github repo is canonical — only the user knows. The
three-button picker (`Keep mine` / `Use both` / `Switch to
theirs`, with `Use both` highlighted as the data-preserving
default) handles this case. `Use both` engages
`dual_publish` mode, which adds the incoming URL to
`Project.extra_remotes`; every sync then pushes to both
upstreams.

## 0.50.58 — share_offer re-fires on every successful push, not just publish

Closes the follow-up flagged in 0.50.53's CHANGELOG: until now
`_spawn_publish_fanout` was only called from `_h_init_project`
on `PUSHED`. If a paired peer was offline / not-yet-discovered at
the moment the user clicked Publish, the share_offer failed
silently and **the URL never reached that peer** — there was no
other code path that carried `remote_url` between peers
(per-commit LAN fan-out moves git objects only, not metadata).

Field repro this morning: tablet has en-TH-x-anna content via
LAN clone (LAN sync between phone and tablet is fully working —
commits, three-way merges, all converging). But tablet's
`.git/config` has no origin URL and registry says
`remote_url=''`. Result: tablet can sync over LAN but every WAN
drain hits `NO_REMOTE`. The publish-fanout had fired yesterday
when the phone auto-recovered; the tablet just wasn't reachable
at that exact moment.

Fix: also fire `_spawn_publish_fanout(langcode, remote_url)` from:

- **`_h_project_sync`** on `PUSHED` / `COMMITTED_AND_PUSHED` — every
  user-clicked Sync that lands on github now re-announces the
  URL to paired peers. Includes the zero-commit case (just a
  push ack), which is exactly the pattern when a user clicks
  Sync to "force a refresh."
- **`scheduler._drain_pending_push`** on `PUSHED` — every successful
  background drain re-announces too. Lazy import to avoid the
  `server ← scheduler` circular dependency.

`_spawn_publish_fanout` itself is unchanged. Receiver side
(`_handle_share_offer`) already dedupes: peers that already have
the URL no-op; peers that don't get a `KIND_ADOPT_ORIGIN`
pending decision for the user to accept via the picker. So
firing on every successful push is harmless spam at worst,
and the URL converges naturally.

Open: peer-side auto-accept of `KIND_ADOPT_ORIGIN` when the
project is already paired and has local content but no URL.
The current flow requires the user to tap "accept" in the
picker — which is correct for "first-encounter" share offers
but unnecessary friction for "URL drift" cases where the peer
clearly already wants this project. Not in this version.

## 0.50.57 — [collab] success log + lessons captured

Closing out the 0.50.52–0.50.56 publish journey:

**`[collab] add_collaborator` success-path log.** Was logging
only on exception, so a daemon-log post-mortem couldn't tell
whether the call ran at all on a successful publish. Same
always-emit-summary lesson as the publish-reconcile fix.
Now logs every invocation:

```
[collab] add_collaborator owner='kent-rasmussen' repo='en-TH-x-anna' collaborator='kent-rasmussen' → already
[collab] add_collaborator owner='aztobt2-ui' repo='baf' collaborator='kent-rasmussen' → invited
[collab] add_collaborator owner='…' repo='…' collaborator='…' FAILED: <exception>
```

For Kent's own publishes the call is a no-op (you can't add
yourself as a collaborator on your own repo — github returns
422 which `add_collaborator` maps to `'already'`). For other
users' daemons, it sends an invitation to `kent-rasmussen` (or
whatever `AZT_GITHUB_COLLABORATOR` / `configure(collaborator=…)`
overrides). The log line is the only proof in the daemon trail
that this happened.

**`docs/Publish_errors.md` — Lessons section.** Captures three
patterns from this journey for future reference:

1. Always emit a summary line, even on no-op paths
2. Dual-entry-path startup hooks (`serve()` *and*
   `server_apk/service.py:main()`)
3. GitHub App auth ≠ PAT auth on the `username` field
   (`'x-access-token'` is github's placeholder, never a real
   login — don't compare against it)

Memory entries also saved for cross-session continuity.

## 0.50.56 — owner-mismatch heuristic broken for GitHub App auth

0.50.55 finally got the reconciliation running on Android, which
exposed two bugs in `_ensure_remote_repo`'s owner-mismatch path
that the earlier silent-failure days had hidden:

**1. Crash on construction.** The owner-mismatch return path
built `Status(S.REMOTE_OWNER_MISMATCH_SKIP_CREATE, owner=…,
username=…, url=…)` — passing kwargs. But `Status` is a
dataclass with exactly two fields (`code`, `params: dict`); it
doesn't accept `**kwargs`. Any time the heuristic fired, the
publish raised `TypeError: Status.__init__() got an unexpected
keyword argument 'owner'`. Fix: pass a dict as the second
positional, matching the convention used everywhere else in
`_ensure_remote_repo`.

**2. False positive on every GitHub App publish.** The check
compared the URL's owner against the credentials store's
`username`. For PAT auth, `username` is the user's GitHub login,
so the comparison is meaningful. For **GitHub App auth**,
`username` is the literal string `x-access-token` — the
placeholder GitHub uses for HTTP basic-auth with installation
tokens. `'x-access-token' != 'kent-rasmussen'` (or any other URL
owner), so the heuristic fires every time, blocks the POST, and
falls through to `S.REMOTE_OWNER_MISMATCH_SKIP_CREATE`. Result:
no github publish via App auth has ever worked through this code
path. The protection was designed for the duplicate-namespace
problem when authenticating as user A but pushing to URL owned by
user B; with App auth, POST `/user/repos` is already scoped to
the installation's account, so there's no risk of
wrong-namespace creation and the heuristic isn't needed.

Fix: skip the heuristic when `username.lower() == 'x-access-token'`.
If the installation isn't on the URL's owner, github returns
403/422 and `[publish] remote-create FAILED owner/repo: <code>`
surfaces the real cause via the HTTPError branch.

Combined effect for the user with stale `en-TH-x-anna`: next
daemon respawn (0.50.55 still got bug 2; 0.50.56 needed) should
show `[publish] POST https://api.github.com/user/repos
owner='kent-rasmussen' repo='en-TH-x-anna'` followed by either
`remote-create OK: created kent-rasmussen/en-TH-x-anna` (if the
App installation has the right permissions) or `remote-create
FAILED ... 403/422 <body>` (telling us exactly what permission
is missing).

## 0.50.55 — reconciliation actually wired into the Android entry path

0.50.53 and 0.50.54 added `reconcile_publish_state_on_startup` and
its call site — but only in `azt_collabd.server.serve()`, which is
the **desktop** daemon entry path. On Android the daemon is
launched by `server_apk/service.py`, which has its own startup
sequence and doesn't call `serve()`. Result: the reconciliation
never ran on Android, and any user with the pre-0.50.52 stale
state stayed stuck. Field-confirmed by a 1ms gap between
`_boot_trace('after_reconcile')` and `_boot_trace('before_lan_listener')`
in the daemon log with zero `[publish-reconcile]` lines anywhere.

Fix: add the same `_repo.reconcile_publish_state_on_startup()`
call to `server_apk/service.py`, between `_boot_trace('after_reconcile')`
and `_boot_trace('before_lan_listener')` — the exact gap the
desktop callsite occupies. Both daemon startup paths now invoke
the auto-fire.

Also: `reconcile_publish_state_on_startup` now emits an
unconditional summary line on every run, even when there's
nothing to do:

```
[publish-reconcile] walked=N mismatch=M succeeded=S deferred=D
```

Previously the function only logged when `succeeded` or `skipped`
had entries — which made the all-healthy-projects case
indistinguishable from "function didn't fire" in the daemon log.
That diagnostic gap is exactly what hid the missing Android
callsite for two iterations of 0.50.x.

## 0.50.54 — reconciliation switched from strip-only to auto-fire

Revision of 0.50.53's `repo.reconcile_publish_state_on_startup`.
The originally-shipped 0.50.53 design stripped `[remote "origin"]`
from `.git/config` so the picker's `_refresh_publish_row` would
show the Publish button for a manual retry. That had a defect on
field devices: an offline / outage / missing-creds boot would
strip state and expose a phantom Publish button on a project the
user already chose to publish — wrong UX.

0.50.54 keeps `.git/config` intact and instead **auto-fires the
publish the user already committed to**:

1. Find projects with the mismatch (`.git/config` URL set,
   registry empty).
2. Look up credentials + contributor; if either missing, log
   `auto-retry deferred …(no_credentials | no_contributor)` and
   move on — state untouched.
3. Call `init_repo(captured_url, …,
   rollback_origin_on_create_fail=False)`. The new kwarg
   disables 0.50.52's picker-path rollback that would otherwise
   strip `.git/config` on `REMOTE_CREATE_FAILED` — in auto-retry
   mode we want to leave the working tree alone on failure so
   the next daemon startup retries silently.
4. On `PUSHED`: write `set_remote_url` / `set_last_sync` /
   `set_last_commit` directly, then fire
   `server._spawn_publish_fanout` (extracted from
   `_h_init_project`'s inline `_fanout_worker` so reconciliation
   can reuse it) to tell paired peers about the now-working URL.
5. On anything else: log `auto-retry deferred …(codes=[…])` and
   move on — state untouched.

Behaviour summary by boot scenario:

| At-boot condition | 0.50.53 strip-only | 0.50.54 auto-fire |
|---|---|---|
| Online + valid creds | Strip, user clicks Publish | Auto-pushes, project recovered |
| Offline | Strip, **phantom button** | No-op, next boot retries |
| Github outage | Strip, **phantom button** | No-op, next boot retries |
| Missing creds | Strip, **phantom button** | No-op, next boot retries |
| User changed mind | Strip, button appears | Auto-publishes anyway (consent argument: they already pressed Publish) |

`init_repo` / `_init_repo_locked` gained the
`rollback_origin_on_create_fail=True` kwarg (default unchanged
for picker callers). Picker manual-click path keeps the rollback
so a failed click still surfaces the button for a deliberate
retry; the difference is purely in the auto-retry callsite.

## 0.50.53 — retroactive cleanup + publish-fanout gate

Follow-up to 0.50.52, addressing two situations its forward path
couldn't:

### Stale publish state from pre-0.50.52 daemons

A user upgrading from `<0.50.52` may have a project where
`.git/config` has an origin URL pointing at a github repo that
doesn't exist, while the registry's `Project.remote_url` is empty.
That fingerprint isn't reachable from any 0.50.52+ code path —
post-0.50.52 the rollback paths keep both sides in sync
(both-set on `PUSHED`, both-empty on `REMOTE_CREATE_FAILED`). But
during the upgrade window itself, an install of a new server APK
doesn't kill the running daemon (the recurring suite issue, see
[[project_client_server_version_drift]] /
[[feedback_restart_must_work_against_old_daemon]]), so clicks
that land while the OLD daemon is still serving RPCs hit the
legacy silent failure and leave the mismatch behind.

In that stuck state the picker's `_refresh_publish_row` hides
Publish (live `.git/config` URL is non-empty), so the idempotent
Publish button is out of reach and the scheduler hammers
`NotGitRepository()` on every drain.

Fix: `repo.reconcile_publish_state_on_startup()` walks
`projects.list_all()` on every daemon startup. For each project
with the mismatch fingerprint, it **auto-fires the publish the
user already committed to** rather than stripping `.git/config`
to make the Publish button reappear. The strip-and-show-button
approach (briefly implemented earlier in this version) had a
defect: an offline / outage / missing-creds boot would strip
state and expose a phantom Publish button on a project the user
had already chosen to publish, which is wrong UX. Auto-fire keeps
state untouched on failure so those boots are silent no-ops.

Auto-fire flow:

1. Mismatch detected (`.git/config` URL set, registry empty).
2. Look up credentials + contributor from the store. If either
   is missing, log the skip and move on — no state change.
3. Call `init_repo(working_dir, captured_url, …,
   rollback_origin_on_create_fail=False)`. The new kwarg
   disables the 0.50.52 picker-path rollback that would
   normally strip `.git/config` on `REMOTE_CREATE_FAILED`. In
   auto-retry mode we *want* to leave the working tree alone
   on failure — next daemon startup retries silently.
4. On `PUSHED`: write the registry side-effects
   (`set_remote_url`, `set_last_sync`, `set_last_commit`) and
   spawn the publish-fanout to paired peers (extracted into
   `server._spawn_publish_fanout` so the reconciliation can
   reuse it).
5. On anything else: log
   `[publish-reconcile] auto-retry deferred for N project(s)
   (state unchanged, next boot will retry): [...]` and move on.

Safety arguments:

- The mismatch fingerprint is unreachable from any post-0.50.52
  code path, so the reconciliation never runs against a healthy
  project. A transient github outage during a *manual* Publish
  produces `REMOTE_CREATE_FAILED` and the picker-initiated
  rollback clears *both* sides — leaving both-empty, not the
  half-state this reconciliation targets.
- Per the user's framing: if they pressed Publish in the past,
  re-firing it now is a no-op confirmation of the same intent.
  No new judgment call required from the user.
- Offline / outage / missing-creds at boot is a normal
  occurrence and must not produce a Publish button on a project
  the user already committed to publish. Auto-fire achieves this
  by leaving state unchanged on failure.

### Publish-fanout fired regardless of success

`_h_init_project`'s `_fanout_worker` (which sends `share_offer`
to every paired peer who has the project on their allow-list,
carrying the `remote_url`) was gated only on
`if published_langcode and remote_url:` — both of which are
populated the moment the RPC arrives, before `_init_repo` even
runs. So a publish that failed at `_ensure_remote_repo` or `push`
would *still* tell peers about the URL, and peers accepting the
share offer would adopt a URL pointing at a non-existent or
empty github repo. They'd then hit `NotGitRepository()` on every
drain, propagating the stuck state.

Fix: add `publish_landed = 'PUSHED' in codes or 'COMMITTED_AND_PUSHED' in codes`
and gate the thread spawn on it. Peers only learn about working
remotes.

Open: the scheduler's WAN-drain doesn't fire `send_share_offer`
after a successful retry-push, so if the *first* publish fails
but a *later* drain succeeds, paired peers won't auto-learn the
URL. That's a 0.50.x sync-rebuild design decision (per CLAUDE.md
"LAN fan-out per-commit, WAN drain WAN-only") and needs a
follow-up to surface a share-offer on drain success. Tracked
separately; not in this version.

## 0.50.52 — publish trail in daemon log + idempotent Publish

Field repro (today): user clicked Publish in the picker, the local-side
mutations of `_init_repo_locked` (rename `master` → `main`, set
`remote.origin.url`) all ran, the `publish-fanout` worker even fired a
share-offer to a paired peer — but the project never appeared on
github.com. Every subsequent `[sync-trace] fetch` returned dulwich's
`NotGitRepository()` (GitHub serving the 404 page rather than a git
response). The daemon log had no trace of what happened inside
`_ensure_remote_repo` — every branch in there was silent:

- success (created): no log
- already-exists 422/400: no log
- HTTPError create-failed: no log (Result-only)
- URL/OSError network failure: no log (Result-only)
- skip-create owner-mismatch: no log
- unknown-host skip: no log

The peer received a typed `Result` carrying the failure status, but
when the user can't or won't share a logcat / daemon-log screenshot,
diagnosis is blind — and the picker's "OK-on-uncertainty" sync
indicator masked the failure too (rendered `wan_unshared=0` while
every commit was at risk because the remote didn't exist).

Fix: instrument `_init_repo_locked` and `_ensure_remote_repo` so the
daemon log carries the full publish trail. New lines, all tagged
`[publish]`:

- `init_repo begin dir=… remote=… branch=… username=…` — every entry
- `POST <api_url> owner=… repo=…` — every github/gitlab create attempt
- `remote-create OK: created owner/repo` — on success
- `remote-create: owner/repo already exists (422/400)` — idempotent path
- `remote-create FAILED owner/repo: <code> <body>` — HTTPError path
- `remote-create FAILED owner/repo: <urlerror>` — URL/OS error path
- `skip remote-create: owner mismatch …` — adopted-URL path
- `skip remote-create: unknown host …` — gitea / forgejo / LAN
- `push to <url> failed: <exc>` — when the post-create push raises
- `init_repo aborting before push: codes=[…]` — when create gates push
- `init_repo done: codes=[…]` — every exit

Paired with the existing `[publish-fanout]` thread tag (server.py:2312),
"the user clicked Publish" is now a self-contained trail a tester can
quote from a Share-daemon-log file.

`_h_init_project`'s four pre-check early-outs (`CONTRIBUTOR_UNSET`,
`missing_working_dir_or_remote_url`, `AUTH_REQUIRED` for no stored
token, and the catch-all `_init_repo raised`) used to return their
typed `Result` to the peer without logging anything daemon-side —
same blind spot as the github-API layer, one level up. All four now
emit a `[publish]` line so a daemon-log share covers the no-token
case (where `_init_repo_locked` is never reached and would otherwise
leave zero `[publish]` traces).

### Idempotent Publish

Once we knew the user had clicked Publish, the secondary question was
"why can't they just click it again to retry?" Two reasons:

1. **`_init_repo_locked`'s commit step would falsely bump the persistent
   `commit_failure_count`.** On the re-click, the working tree has no
   pending staged changes (the original Publish's initial commit is
   already in history). `porcelain.commit` raised "nothing to commit"
   → `_surface_commit_failure` → counter +=1 → eventually surfaces
   `COMMIT_REPEATEDLY_FAILED` as a data-loss-class toast for a
   non-failure. Mirror of `_commit_step_locked`'s has_staged guard:
   inspect `porcelain.status(repo)` first, only call `porcelain.commit`
   when something's actually staged, otherwise add `S.NOTHING_TO_COMMIT`.

2. **The picker hid the Publish button entirely.** `_refresh_publish_row`
   gates on `live_remote_url` non-empty, and the failed Publish left a
   stale `remote.origin.url` in `.git/config` pointing at a non-existent
   github repo. Fix: roll back the local-side mutation on
   `_ensure_remote_repo` failure (`ok=False`) — strip the
   `[remote "origin"]` section via the new `_strip_origin_section`
   helper, and mirror the rollback in the registry by clearing
   `projects.set_remote_url(langcode, '')` in `_h_init_project` when
   the result carries `REMOTE_CREATE_FAILED`. Only on hard
   create-failure — `REMOTE_OWNER_MISMATCH_SKIP_CREATE` (collaborator
   bet) and push failure (remote exists, scheduler drain will retry)
   both keep the URL.

After both fixes, the Publish button is fully safe to re-click:
- nothing changed on the working tree → no spurious commit, no counter
  bump, no `COMMITTED` line
- existing origin URL matches → `REMOTE_UNCHANGED`
- HEAD already on `main` → no-op
- `_ensure_remote_repo` retries the github-API call (422 if the repo
  appeared between attempts)
- `porcelain.push` retries naturally

The masking sync-indicator (OK-on-uncertainty firing on
`NotGitRepository`) is a separate fix to plan — `_wan_unshared` should
distinguish "fetch hit a 404 page" from "fetch never ran" before
collapsing both to `wan_unshared=0`. Not in this version.

## 0.50.51 — commit_after opt-out + lan_peer_id guarantee documented; NOTES queue cleared

Both items closed from `azt_collab_client/NOTES_TO_DAEMON.md`.

### `commit_after` opt-out on write RPCs (Option A from NOTE #2)

Filed by azt-recorder 1.55.21 (2026-06-06). The atomic_commit /
set_audio / set_illustration / atomic_finalize endpoints schedule
a debounced commit on every successful write with no opt-out,
which breaks the recorder's commit-boundary model (commits land
before the user accepts the take; re-records ship every bad
take into history).

Fix: all four write endpoints now accept an optional
`commit_after: bool = True` body parameter. Default `True`
preserves current behavior. When `False`, the daemon performs
the atomic write + `notify_project_changed` exactly as before
but skips the `commit_project` scheduling. The peer is then
responsible for calling `commit_project(langcode)` at its own
boundary.

Client wrappers updated to surface the flag:
- `atomic_commit_bytes(langcode, rel_path, data, commit_after=True)`
- `set_audio(langcode, guid, lang, filename, commit_after=True)`
- `set_illustration(langcode, guid, href, commit_after=True)`
- `atomic_finalize_pending(langcode, rel_path, token, commit_after=True)`

`CLIENT_INTEGRATION.md` § 9a (Targets + step 6) and § 21
updated with the opt-out contract.

### `MIN_SERVER_VERSION` 0.49.1 → 0.50.51

Forced floor bump (same pattern as 0.47.0). Older daemons
silently ignore unknown body fields, so a 0.50.51+ peer passing
`commit_after=False` to a pre-0.50.51 daemon would have the
auto-commit fire anyway with no signal back to the peer — the
silent-failure case version floors exist to prevent. With the
bump in place, any 0.50.51+ peer client refuses to talk to a
pre-0.50.51 daemon and the bootstrap install/update popup
prompts the user to refresh the server APK before the peer
trusts the opt-out.

`MIN_CLIENT_VERSION` (daemon-side floor on clients) stays at
0.50.0 — the new feature is purely additive on the daemon, so
old clients that don't pass the field still work correctly
(the daemon's default of `True` matches their assumption).

### `lan_peer_id()` non-empty guarantee documented (NOTE #1)

Filed by azt-recorder before 1.50.2. Daemon eager-initialises
the per-device ed25519 keypair on every startup since 0.50.9,
so `lan_peer_id()` is guaranteed non-empty on any 0.50.9+
daemon with the `cryptography` package present (suite APKs
ship cryptography unconditionally). No daemon code change —
the guarantee has been in place for releases; what was missing
was the explicit statement in CLIENT_INTEGRATION's locked
semantics. Now added to Locked Semantic #2.

Peers may safely drop legacy `device_name` fallbacks for
peer-identity matching against a 0.50.9+ daemon. Pre-0.50.9
instances are end-user upgrade prompts, not peer-side
workaround territory.

## 0.50.50 — Receiver-side last_seen_main refresh after receive-pack (LAN-1 phantom fix)

User reported: A commits, A fan-outs to B. A shows LANOK (its
sweep updated `last_seen_main[B][lang]` to the new SHA). But B
shows LAN-1 even though B just received that commit from the
only paired peer.

Root cause: when A's push lands on B's listener, the smart-
protocol receive-pack advances B's `refs/heads/main` and B's
`_reset_working_tree_after_receive` syncs the working tree, but
**nothing on B's side updates `last_seen_main` for the paired
peer who pushed**. So B's `_lan_unshared` walks excluding stale
peer SHAs (or no peer SHAs at all on a first push) and reports
the just-received commit as "unshared." LAN-1 on a project the
two phones are actually in sync on.

### Fix

New `lan_push.peek_peer_head(peer_id, langcode)` — peek-only
ls-remote against a peer's listener. Resolves endpoint + builds
TLS-pinned pool + reads peer's main SHA. Returns hex string or
None. Honors the 0.50.49 fast-fail gate.

New `lan_listener._refresh_peer_last_seen_after_receive(langcode,
new_head_sha_hex)` — fires from
`_reset_working_tree_after_receive` after the successful hard
reset, in a background thread off the WSGI worker path. Walks
every paired peer whose `shared_projects` contains *langcode*,
peeks their main, and updates `last_seen_main` for those at our
new HEAD. The pusher is guaranteed to match (they couldn't push
a SHA they don't have); other paired peers may or may not.

After this lands:

- A commits → A pushes to B → A updates `last_seen_main[B][lang]`
  (existing behaviour) → A shows LANOK.
- B's listener accepts the push → B resets working tree → B
  peeks A's main → A is at the same SHA → B updates
  `last_seen_main[A][lang]` → B shows LANOK too.

Cost: one ls-remote per paired peer sharing the project on each
incoming push. Cheap (protocol round-trip only, no packfile),
and the fast-fail gate makes recently-unreachable peers a free
skip.

### Why we don't update peers whose main is behind our HEAD

A paired peer whose ls-remote returns a SHA earlier than our new
HEAD might genuinely be behind, OR they may simply not have
observed our push yet. "OK on uncertainty" says leave their
`last_seen_main` alone — the next sweep that successfully pushes
to them will update it.

## 0.50.49 — Daemon-startup orphan sweep + fast-fail for unreachable peers

### Startup orphan-tracking-ref sweep

0.50.48 fixed the orphan-cleanup logic (`has_url_now` checks the
decoded URL value, not just key existence) but cleanup still only
ran when `_h_project_status` polled a specific project. Projects
the user wasn't currently looking at kept their stale
`refs/remotes/origin/*` refs visible in `lan_debug` dumps even
though the readings were correct.

Now: scheduler startup walks every project in `projects.json`
and calls `strip_lan_origin_if_present(scope_to_paired_peers=True)`
for each. One-time housekeeping that fires once per daemon
startup; steady-state cleanup is still the `_h_project_status`
path. After the next deploy + first daemon respawn, all four
projects on both phones should report `remote_refs_present: []`
(when `has_origin_url: false`) without the user having to open
each one.

### Fast-fail for recently-unreachable peers

Paired-but-absent peers were costing ~23 seconds per burst per
peer. The phone has three paired peers and only one in the room;
the absent two (`77a1384f` at 192.168.10.101, `aa970b36` at
192.168.10.143) each ate three urllib3 retries × ~2.3 s connect
timeout × two projects in the sweep. The sweep summary line
showed `0/N delivered` after the dust settled. Net effect: every
burst spent most of its 30 s window waiting on dead peers.

New per-peer fast-fail gate:

- `_unreachable_at[peer_id_hex]` (in-memory monotonic timestamp)
- `_UNREACHABLE_COOLDOWN_S = 60.0`
- `_recently_unreachable(peer_id)` — predicate; True while in
  cooldown
- `_record_unreachable(peer_id)` / `_record_reachable(peer_id)` —
  set / clear

Wiring:

- `_push_to_peer` checks the gate at entry, returns False
  immediately (logged as `recently unreachable; skipping
  (fast-fail)`) when set.
- `_https_post_to_peer` checks the gate at entry, returns
  `(0, b'')` immediately when set.
- The network-error paths in both helpers call
  `_record_unreachable(peer_id)`. Success paths call
  `_record_reachable(peer_id)`.
- `lan_discovery._fire_arrival` clears the gate before firing
  `sweep_peer` — mDNS arrival is a strong "this peer is back"
  signal and shouldn't be blocked by the previous-burst
  observation.

Net effect for a 3-peer-paired, 1-peer-in-room sweep:
- First absent-peer attempt: ~7 s timeout (same as before),
  records unreachable.
- Every subsequent attempt within 60 s: ~µs skip.
- mDNS arrival of the in-room peer: gate clears, sweep proceeds
  normally.

### Stale-endpoint race (task #31 in the session log) — resolved by fast-fail

The "tablet tried 35581 instead of the just-resolved 36587"
behavior the previous logs surfaced was actually mDNS not having
resolved yet at sync time, so `_resolve_endpoint` fell back to
the previous-session `static_endpoints` port. The fast-fail gate
above absorbs the retry storm from the stale port; the mDNS
arrival callback then clears the gate and the sweep retries with
the fresh endpoint. No separate code change needed for this
specific case.

## 0.50.48 — Orphan-tracking-ref bugs: wan_unshared honored stale refs, cleanup pass missed half-stripped state

Field repro 2026-06-05: tablet showed WAN-302, phone showed WAN-17
for the same project, same HEAD SHA, same ancestor depth (both 302
from HEAD). The diagnostic snapshot from the 0.50.47 daemon-log
dump nailed it:

- Tablet: `has_origin_url: false`, `remote_refs_present:
  [origin/HEAD, origin/master]`, `wan_unshared: 302`.
- Phone: `has_origin_url: false`, `remote_refs_present:
  [origin/HEAD, origin/main, origin/master]`, `wan_unshared: 17`.

The phone had stale `refs/remotes/origin/main` (and friends) left
over from a previous origin that had since been stripped — but
`_wan_unshared` happily walked excluding that orphan, producing 17.
Two interlocking bugs:

### Bug 1: `_wan_unshared` branched on tracking-ref existence, not URL

Pre-fix the helper checked "does `refs/remotes/origin/<branch>`
exist?" first, only consulting the URL when the ref was absent. So
a project with no origin URL but lingering tracking refs got
walked as if those refs were real upstream state, producing
nonsense counts. New shape: read URL first. If empty, it's a
LAN-only project — walk from HEAD regardless of what refs are
hanging around. Tracking ref only matters when there's an actual
URL behind it.

### Bug 2: orphan-tracking-ref cleanup blocked by half-stripped config

The orphan cleanup in `_strip_lan_origin_locked` was supposed to
remove these refs after URL strip, but its `has_url_now` check
treated "key exists, empty value" the same as "URL present" —
which is exactly the state the older `config.set(... url, b'')`
fallback (for dulwich versions without `remove_section`) leaves
behind. The phone's project was in that half-stripped state, so
every cleanup pass since the orphan handler shipped (0.46.2 era)
skipped over it. Now `has_url_now` checks the decoded value, not
just key existence — empty string = no URL = run the cleanup.

### Bug 3: `lan_debug.head_branch` always empty

`refs.read_ref(b'HEAD')` follows the symbolic ref and returns the
resolved SHA, not `b'refs/heads/<branch>'`. Switched to
`refs.get_symrefs()` which returns the symbolic-ref mapping
directly. Diagnostic dumps from 0.50.48+ will carry the real
branch name.

## 0.50.47 — lan_debug snapshot lands in daemon log on toggle-on

0.50.46 added the `lan_debug` RPC but only via the python client
wrapper. That's useless when the tester only has the device UI
and the Share daemon log button — no python shell to call the
wrapper. 0.50.47 fixes the delivery path:

When the user turns the daemon-log toggle ON (the gesture that
starts a fresh capture before sharing), `_h_set_daemon_log_to_file`
now immediately writes a `[lan-debug] snapshot start … snapshot
end` block to stderr, one JSON line per registered project with
the full `lan_debug` payload. The user then taps Share daemon log
and the diagnostic baseline is already in the file.

No new UI button — the gesture you already do (toggle on → share)
now carries the diagnostic. Per the existing "remote-tester
diagnostics need a user-visible delivery path" rule from session
memory.

## 0.50.46 — Diagnostic RPC for WAN-N disparity (GET /v1/projects/<lang>/lan_debug)

Read-only diagnostic dump for chasing the "tablet says WAN-302,
phone says WAN-17, but the sweep proves they're at the same SHA"
class of issue. Hit it from each phone for the same project and
compare fields directly: HEAD branch + SHA, ancestor count from
HEAD, origin URL, tracking ref SHA, full list of local branches
and remote refs, current ``wan_unshared`` reading. Returns:

```json
{"ok": true,
 "langcode": "en-TH-x-anna",
 "head_branch": "main",
 "head_sha": "abc123…",
 "ancestor_count_from_head": 302,
 "has_origin_url": false,
 "origin_url": "",
 "tracking_ref_sha": null,
 "remote_refs_present": [],
 "branches_present": ["refs/heads/main"],
 "wan_unshared": 302}
```

Per-field errors surface as ``<field>_error`` rather than
silently zeroing — different from the production helpers which
follow "OK on uncertainty" because diagnostics need the real
reason. Client wrapper: ``lan_debug(langcode)`` returns the raw
dict (no Result wrapping, since this is structural info, not a
status-coded op).

## 0.50.45 — LAN sync convergence: sweep, backoff, lifecycle bursts

Six changes that close the "two phones on the same LAN don't always
talk to each other" gap, while keeping the no-periodic-polling
power model intact. See `CLAUDE.md` invariant #10 for the updated
contract.

### 1. `sweep_peer(peer_id, exclude_langcode='')` helper in `lan_push.py`

Walks every shared project with a peer and pushes only the ones
they're behind on (relies on `_push_to_peer`'s pre-flight
ls-remote no-op short-circuit). Past work on projects that
nobody has committed to recently catches up the next time
*anyone* is in the room with each other.

### 2. `fan_out` folds in a sweep tail

After pushing the originating project to each peer, `fan_out`
fires `sweep_peer(peer_id, exclude_langcode=this_project)` for
that peer. The radio is already up and the TLS handshake is
warm — pushing the OTHER shared projects in the same window is
nearly free. "We're already talking to B; tell them about Y
too."

### 3. mDNS arrival detection → arrival sweep

`lan_discovery._record` (zeroconf) and `onServiceResolved`
(NsdManager) now compare the new endpoint against the cached
one. A peer is "arriving" when:
- no prior endpoint, OR
- prior endpoint is past `_ENDPOINT_TTL_S` (5 min), OR
- host/port changed (Wi-Fi flap, peer rebound to a new port).

On arrival of a *paired* peer, `_fire_arrival` spawns a worker
that runs `sweep_peer(peer_id)`. So when B walks into the room
with A (or rejoins after a brief drop), A's mDNS notices, A
sweeps every shared project with B, and B catches up. No
explicit user gesture required, no periodic radio activity.

### 4. Lifecycle burst triggers — `lan_burst_now()` + picker hook + listener-bind sweep

New `POST /v1/lan/burst` endpoint (and client wrapper
`lan_burst_now()`) that fires `start_burst()` without any WAN
drain or fan-out. Lightweight "bring the radio up for 30s so
mDNS can find someone" gesture. Picker's `on_resume` calls it
so opening the app brings the room into sync without requiring
a Sync tap. Peer apps SHOULD call it from their Activity.onResume
hooks too (one-line addition, no wire format constraint).

Daemon-side, `lan_listener.apply_toggle` fires a sweep of every
paired peer after a fresh listener bind (worker thread, doesn't
block the binder). So a daemon respawn followed by listener
re-bind opportunistically catches up everyone reachable.

### 5. `lan_backoff` — persisted commit-count curve

New module `azt_collabd/lan_backoff.py` with state at
`$AZT_HOME/lan_state.json`. Per-project counter of
`commits_since_lan_success`. Post-commit burst fires only when
the counter is a power of two: 1, 2, 4, 8, 16, 32, 64, …

A lone worker doing 100 commits in a session gets 7 bursts (1,
2, 4, 8, 16, 32, 64) instead of 100. Radio cost asymptotes
toward zero. The counter resets on `record_success` (≥1 peer
received the fan-out) or `nudge` (user pressed Sync). Daemon
respawn does NOT reset — same rule as the WAN fix in (6).

Sync, online-edge, and lifecycle bursts (item 4) bypass the
gate. The backoff is specifically on the *routine post-commit
burst*, not on intent-bearing or one-shot triggers.

### 6. WAN backoff: drop `reset_due_times_on_startup` call

Pre-0.50.45 every daemon respawn cleared every project's
`next_attempt_at` to 0, giving a free WAN retry. On Android,
respawn-frequency (OOM, sticky-service restart, APK update)
was high enough that the 24h cap in the docstring was
effectively unreachable — the curve was bounded by respawn
cadence, not by the doubling math.

Now: daemon lifecycle is not a reset signal. Only
`record_success` (actual push succeeded) and `nudge` (user
pressed Sync) clear the curve. `reset_due_times_on_startup`
remains in the module as a no-op for any external caller, with
an updated docstring.

### Updated invariant in `CLAUDE.md`

Note added to invariant #10: lifecycle events (daemon respawn,
OOM, APK self-update) are not equivalent to user intent and
do NOT reset backoff curves. Successful delivery and user-tap
Sync are the only two paths that reset.

## 0.50.44 — Symmetric unshare, CAWL warm-cache short-circuit, LAN-fanout doc

### Symmetric unshare wire flow

Unshare was asymmetric: phone A unsharing project X with phone B
removed B from A's allowlist (so A's outbound fan-out skipped B
going forward), but B's allowlist for A was untouched. B's
subsequent commits still auto-fanned-out X to A, which A's
listener then no-op'd with a logged
`share-offer from … carries no repo_url; no-op (already have project)`
line. Visible to the user as "we just keep talking about this
project even though I told it to stop." Closed by 0.50.44:

- New wire endpoint `POST /v1/lan/share_unshared` on every
  listener — body-auth, body shape `{peer_id, fp, langcode}`.
- New handler `_handle_share_unshared` in `lan_listener.py`
  removes the *sender* from the local `shared_projects` allowlist
  for `langcode`, idempotent.
- New sender `send_share_unshared(peer_id, langcode)` in
  `lan_push.py`.
- `server._h_lan_unshare_project` now fires the courtesy POST
  after the local allowlist removal. Best-effort — failure to
  reach the peer doesn't roll back the local removal; the peer
  catches up the next time their daemon is reachable (manually
  re-unshare or accept the stale offer; for now no auto-retry).

### CAWL auto_prefetch warm-cache short-circuit

`auto_prefetch(repo)` now calls `cache_status(repo)` before
spawning a worker. If `cached >= total > 0` (every WAN-policy
path already on disk), it returns without starting the walk. Pre-
fix the worker re-walked the entire image index every ~30 s
(throttle window) even when nothing was missing — the user's
daemon log showed a `[cawl] worker first bump …` line every
30–40 s indefinitely. Now: zero worker activity when the cache
is fully warm. The diagnostic line stays in for one more release
so the user can confirm the spam is gone before deleting it
(0.50.45 work).

Note: the trigger (`_touch_project` calling `auto_prefetch` from
every project-bound RPC) is unchanged — the warm-cache check
absorbs the cost of frequent calls into a single cheap
`cache_status` lookup, but the longer-term right answer is moving
the trigger out of `_touch_project` and onto true project-load
events. Parked behind verification of this short-circuit.

### CLAUDE.md — LAN fan-out semantics

Stale text in invariant #10 said "the watcher's drain loop fans
out periodically." This stopped being true in the 0.50.x sync
rebuild (`scheduler.drain_pushes stays WAN-only`). Updated to
spell out the actual contract: LAN fan-out is per-commit, per-
project, event-driven; missed deliveries don't retry; github is
the convergence safety net; the peer's currently-loaded project
is **not** a gate — only the per-peer `shared_projects`
allowlist is. Forward-link to `sync.md` for the rationale.

## 0.50.43 — Re-offer share has feedback, dedup, whole-column tap target, sender-side log

The "Offer share again" affordance added in 0.50.42 had three
testability problems:

1. The sender daemon was silent on the happy path — only the
   receiver's daemon log showed anything. With the daemon-log
   toggle running, the user had no way to confirm from their own
   logs that the courtesy POST had even left the device.
2. Tapping the small "Offer share again" link required precise
   finger placement; if a tap missed the link's hit box, nothing
   visible happened (the "Shared" label above it was inert), so
   users repeatedly stabbed at the area.
3. Rapid double-taps fired two HTTPS POSTs back-to-back.

### Daemon log line (sender-side)

`_h_lan_send_share_offer` now prints one line per call, success
or fail:

```
[server] send_share_offer peer='abcd1234' lang='baf' \
    post_status=200 dispatch='noop'
```

`post_status` is the HTTPS code the receiver returned (`0` on
transport failure). `dispatch` is the receiver's per-state
classification (see next section), `''` when the receiver is
pre-0.50.43 or the call didn't reach them.

### Receiver dispatch field (additive wire-format change)

`_handle_share_offer` in `lan_listener.py` was returning a bare
`{ok: True}` regardless of what it did with the payload. Now it
returns `{ok: True, dispatch: <state>}` with one of:

- `noop` — receiver already has the project at the same
  `remote_url` — no pending decision stashed.
- `no_url` — receiver already has the project; sender carried no
  `remote_url` so there's nothing to learn.
- `stashed_share` — receiver didn't have the project at all;
  clone-offer stashed as a pending decision.
- `stashed_adopt_origin` — receiver had the project but no
  `remote_url`; URL-adopt prompt stashed.
- `stashed_conflict` — both sides had a `remote_url` and they
  differ; URL-conflict prompt stashed.

Additive — old senders ignore the field; new senders treat a 2xx
without `dispatch` as "delivered, outcome unknown."

`lan_push.send_share_offer` now returns `(status, dispatch)`
instead of `bool` so the server handler can put both into the
client-facing RPC response.

### Typed `Result` from `lan_share_project`

Wrapper used to return the updated peer dict on success, `{}` on
failure — errors silent, no way for the UI to show why. Now
returns a `Result` carrying one of:

- `LAN_OFFER_DELIVERED` with `dispatch` + `post_status` — picks
  the per-dispatch flash text from `translate.py`:
  - `noop` → "Already in sync."
  - `no_url` → "Other phone already has this project."
  - `stashed_share` → "Sent — waiting for the other phone to
    accept."
  - `stashed_adopt_origin` → "Sent — other phone will be asked
    to adopt the GitHub URL."
  - `stashed_conflict` → "Sent — other phone has a different
    GitHub URL."
- `LAN_OFFER_NOT_DELIVERED` with `post_status` — "Could not
  reach the other phone."
- `LAN_TOGGLE_OFF`, `CONTRIBUTOR_UNSET`, `PROJECT_NOT_INITIALISED`,
  `PROJECT_UNBORN`, `PEER_UNKNOWN`, `SERVER_ERROR` —
  gate-failure messages with corrective guidance.

New constants mirrored in both `azt_collabd/status.py` and
`azt_collab_client/status.py`: `LAN_OFFER_DELIVERED`,
`LAN_OFFER_NOT_DELIVERED`, `PROJECT_NOT_INITIALISED`,
`PROJECT_UNBORN`, `PEER_UNKNOWN`. Translations live in
`azt_collab_client/translate.py`; `_dispatch_msg` helper picks
the per-dispatch text for `LAN_OFFER_DELIVERED`.

### Whole-column tap target (`_TapBox`)

The two-Label right column in the "shared" state is now a single
`_TapBox` (a `ButtonBehavior + BoxLayout` mixin) so tapping
anywhere in the column registers as a re-offer. Visual layout is
unchanged — bold "Shared" on top, smaller underlined "Tap to
offer share again" hint below — but the hit area is the whole
column instead of just the link text. Right-column width bumped
to `dp(140)` to fit the longer dispatch flash messages without
truncation.

### Client-side debounce

Each row tracks `in_flight` (a tap is being processed; the
synchronous HTTPS POST takes up to 15 s in the worst case) and
`cooldown` (3 s after the flash text appears). Taps during
either window are absorbed without making a second RPC. The
synchronous `lan_share_project` call now happens on a worker
thread so a slow peer doesn't freeze the UI; the result comes
back via `Clock.schedule_once`.

## 0.50.42 — Share-row "Shared" state is now a label + re-offer link, not a button

User report on the new share_project_popup paired rows: the toggle
button reading "Shared" suggested a tappable affordance that didn't
do anything visible (tap re-fires ``lan_share_project``, which is a
no-op on the daemon allowlist and silently re-POSTs the courtesy
offer). Two-state semantics now match the actual behaviour:

- **Not shared yet** — a `Share` button. Tap fires
  ``lan_share_project`` and the column rerenders into the shared
  state.
- **Already shared** — a static `Shared` label (no border, accent
  colour) with an `Offer share again` link beneath it (rendered as
  a Kivy Label with `[ref][u]…[/u][/ref]` markup so it reads as a
  link, not a button). Tapping the link re-fires
  ``lan_share_project``, which on the daemon side re-POSTs the
  courtesy offer to the peer's listener (useful when the first
  offer was missed because the peer's listener wasn't up yet).
  Brief `Sent` confirmation flashes in place of the link for 1.5 s
  so the tap registers visually.

### `_build_full_row` — new `right_widget` slot

Right column was previously a button stack only. The Shared/re-
offer composite needs a Label on top of a markup Label, which
doesn't fit the buttons stack contract (button column has fixed
`dp(100)` width + filler logic). New optional `right_widget=` arg
takes a pre-built widget that's added to the row as-is; caller
owns its sizing. ``buttons=`` keeps working for callers that just
want a vertical button stack (Manage/Unpair, Pair, …).

## 0.50.41 — Wider LAN-popup rows with full peer details, three named sections

User report on the "Nearby & paired devices" popup:

- the title was rendered twice (once by the popup chrome, once by an
  inner Label),
- peer rows were aligned to a second column with capped text,
- only a peer-id prefix (`xxxxxxxx…`) was shown — no IP, no projects,
  no way for two phones in a room to verify they were looking at the
  same device,
- the "Nearby (unpaired)" / "Paired" sublabels appeared only when
  their section had content, so the user couldn't tell whether an
  empty popup meant "nothing detected" or "section hidden."

### `paired_phones_popup`

- Dropped the inner duplicate-title Label; the popup chrome's title
  bar is now the single source.
- Three section headers (`This phone` / `Unpaired` / `Paired`)
  always render, with a dim placeholder line when the section is
  empty (e.g. "No nearby phones detected. Tap Refresh after a few
  seconds.").
- Each row uses the new `_build_full_row` helper: bold name on its
  own line (wraps only if it can't fit popup width), full peer_id
  on the next, endpoint (`ip:port`) on the next, shared-projects
  list on the next. Labels are width-bound so they get the full
  popup width minus the button column.
- This-phone row at the top mirrors the layout (name / full uid /
  bound listener endpoint / all-projects list) so a third party in
  the room can read off the same fields they see for other phones.
- Removed `_build_peer_row` (replaced by `_build_full_row`);
  inlined the paired-row button construction in `_refresh` so each
  row's Manage/Unpair closes over its own `peer`.
- `_resize_to_content` updated: the inner title is gone, so the
  popup height calc no longer counts a `_title_h` row or the spacing
  to it.

### `share_project_popup`

- Same treatment: dropped the duplicate inner title, added a "This
  phone" section header + row at the top, the paired-peers list is
  now a `Paired` section with the same wide row layout (name / full
  uid / IP / shared-projects + a Share/Shared toggle button), and
  the QR section sits under its own "Pair a new phone" header.
- QR rendered larger: `scale` 6 → 8 (each module easier to lock on
  at arm's length), display height 200 → 320 dp.
- Body wrapped in a `ScrollView` so the taller content doesn't
  clip the Close button on short screens; Close stays anchored to
  the bottom of the popup.

### Helpers added (private, in `azt_collab_client.ui.lan_popups`)

- `_section_header(text, font_name)` — bold accent-coloured
  width-bound section header used by both popups.
- `_build_full_row(*, name, peer_id, endpoint, projects,
  buttons, font_name)` — the new wide row layout. `buttons=None`
  produces an info-only row (used by the This-phone header rows).
- `_peer_endpoint_str(peer)` — joins `endpoints` + `static_endpoints`
  into a single 'ip:port[, …]' string for display.

## 0.50.40 — Shrink popups to content (paired_phones + share_project)

User report: two popups parked at a near-full-screen size while
showing four lines of content, ~35% screen wasted.

### `paired_phones_popup`

Was `size_hint=(0.95, 0.9)` — 90% of screen height regardless of
how many nearby / paired devices were actually present. Now uses
`size_hint=(0.95, None)` with `popup.height` computed from
content + chrome and capped at `Window.height * 0.9`. The scroll
view inside is bound to `list_box.height` (with a `dp(80)`
floor so the empty-state label doesn't get clipped). Window
resize re-fires the layout calc; the binding is removed on
popup dismiss so the closed popup doesn't keep a reference to
the global Window.

### `share_project_popup` paired-phones scroll

The "Share with a paired phone" ScrollView had `height=dp(140)`
hard-coded — fits ~3 rows, leaves the inside blank when there's
only one paired peer. Now binds to `peers_box.height` capped at
`4 × (row_h + spacing)`. A single paired phone takes a single
row's worth of vertical space; a long list still scrolls.

### Why this wasn't size_hint_y=None from the start

The original popups were sized for "list might be long enough
to need a big scroll area." The cap-with-content-bound pattern
gets the same scroll behavior for long lists *and* shrinks for
short ones, which is what the user actually wanted. The
remaining size_hint=(0.95, …) popups in this file mostly have
multi-section content (QR + share + collaborator etc.) where
a fixed height is genuinely the right shape; only these two
needed adjustment.

### Files

- `azt_collab_client/ui/lan_popups.py`:
  - `from kivy.core.window import Window` import.
  - `paired_phones_popup` — dynamic-height + content-bound scroll
    + Window-resize re-layout + cleanup-on-dismiss.
  - `share_project_popup` — `peers_scroll` height binds to
    `peers_box.height` with a 4-row cap.
- `azt_collab_client/__init__.py` — `__version__` 0.50.39 → 0.50.40.

### Compatibility

UI-only client change. No wire format implications. Pre-0.50.40
peers continue to work; the popup that opens is just smaller
when content is small.

## 0.50.39 — Nearby (unpaired) list: filter self + surface device_name

Two UX bugs in the 0.50.28 "Nearby (unpaired)" popup. Both
daemon-side.

### Self showing up in the unpaired list

`_h_lan_nearby_unpaired` filtered out paired peers but never
filtered out the daemon's own `peer_id`. The daemon's own mDNS
advertisement is picked up by the local discovery callback (NSD
and zeroconf both surface the local registration), so the
running device's own peer_id ended up in `known_endpoints()`
and then in the Nearby list — confusing UX (users tap "Pair"
on themselves; the request goes nowhere).

Fixed by treating `peer_id.ensure()['peer_id']` as already-known
alongside the paired-peer set.

### Empty / cryptic name on unpaired entries

Pre-0.50.39, the daemon's mDNS TXT records carried only
`peer_id` / `fp` / `v`. `device_name` was not advertised, so
the discovery side had no way to know an unpaired peer's name
and `_h_lan_nearby_unpaired` returned `device_name=''` on every
entry. The peer popup then fell back to `"Device {peer_id[:8]}…"`
— accurate but useless for picking your colleague's phone out
of a list.

Fixed by adding `device_name` to TXT on both advertise paths
(zeroconf + Android NSD) and extracting it on both discovery
callbacks. A new `_device_names` sibling cache parallels
`_endpoints` to keep the existing 3-tuple shape of
`known_endpoints()` stable (the scheduler's fan-out planner
depends on it).

New `known_device_names()` accessor returns
`{peer_id: device_name}`; `_h_lan_nearby_unpaired` looks each
discovered peer up in it and surfaces a non-empty
`device_name` when available. Pre-0.50.39 peers still
advertise without `device_name` — those entries fall through
to the peer_id-prefix fallback in the UI, same as today.

### Why a sibling cache instead of extending `_endpoints`

`known_endpoints()` returns `{peer_id: (host, port)}` and is
consumed by the fan-out planner / scheduler at multiple sites
that unpack the 2-tuple inline. Extending to a 3-tuple would
require a coordinated update across all callers; a sibling
`known_device_names()` keeps the existing shape stable and
puts the optional device_name on its own accessor — easier to
add, easier to remove if we ever consolidate.

### Files

- `azt_collabd/lan_discovery.py` — `_device_names` cache, TXT
  `device_name` in both advertise paths, extraction in both
  discovery callbacks, new `known_device_names()` accessor.
- `azt_collabd/server.py` — `_h_lan_nearby_unpaired` filters
  self, surfaces device_name from `known_device_names()`.
- `azt_collab_client/__init__.py` — `__version__` 0.50.38 → 0.50.39.

### Compatibility

- mDNS TXT is additive: pre-0.50.39 peers don't advertise
  `device_name` and discovery just sees the field as empty.
- 0.50.39 peers advertise it; older peers reading 0.50.39's
  TXT just ignore the unknown key.
- No `MIN_CLIENT_VERSION` bump needed — the new field is
  daemon-internal (TXT + RPC response), no peer code change
  required to read it (the popup already reads
  `entry.get('device_name')` from the response).

## 0.50.38 — Server UI banner shows LAN vs Internet source

The server UI's prefetch progress banner (`_tick_cawl_cache_status`
in `azt_collabd/ui/app.py`) only rendered "Caching images: X / Y
(network in use — please stay online)" regardless of where the
bytes were coming from. Now that 0.50.37 fixes the wrapper bug
that was hiding the per-source telemetry, the server UI can
actually use it:

- `last_source == 'lan'` → "Caching images: X / Y · via LAN"
- `last_source == 'upstream'` → "Caching images: X / Y · via Internet (please stay online)"
- `last_source == 'cache'` / `'unknown'` / `''` → fall back to the
  existing generic "network in use" line. Cache hits don't
  justify a "via" tag (no current network is serving anything);
  the `'unknown'` / empty cases indicate initial state or a bug
  already loud in the daemon log via `[cawl] cache_status bug:`.

Same display rule the recorder team's `_apply_cache_status` is
documented to follow in `CLIENT_INTEGRATION.md` § 10. The server
UI was the laggard.

The two new sentence-shaped msgids
(`'Caching images: {cached} / {total} · via LAN'` and
`'Caching images: {cached} / {total} · via Internet (please stay online)'`)
already shipped to `azt_collab_client.po` in 0.50.30 along with
the French translations, so this is a pure code wiring change —
no catalog additions required.

### Files

- `azt_collabd/ui/app.py` — `_tick_cawl_cache_status` reads
  `last_source` and branches on it for the active-fetch banner.
- `azt_collab_client/__init__.py` — `__version__` 0.50.37 → 0.50.38.

### Compatibility

- Server-UI-only display change. Requires daemon at 0.50.21+
  (per-source telemetry) AND a daemon-side bundle that includes
  the fixed `cawl_cache_status` wrapper (0.50.37+); both ship in
  the same APK build, so just rebuild + redeploy normally.

## 0.50.37 — Fix the wrapper bug that 0.50.30-0.50.36 chased in the wrong place

The user found the actual bug. It was in
`azt_collab_client/__init__.py:2200-2204`, in the
`cawl_cache_status` wrapper:

```python
return {
    'image_repo': resp.get('image_repo') or '',
    'cached':     int(resp.get('cached') or 0),
    'total':      int(resp.get('total') or 0),
}
```

The wrapper forwarded **only three fields** to peer callers,
silently dropping every other field the daemon emitted —
including all four per-source telemetry fields
(`last_source`, `from_cache`, `from_lan`, `from_upstream`) and
the three state flags (`offline`, `circuit_open`, `finished`).
Peers that followed the `CLIENT_INTEGRATION.md` § 10 recipe
and read `status.get('last_source', '')` got `''` not because
the recorder logged it wrong, not because the daemon set it
wrong, but because **the wrapper never delivered it in the
first place**. Every release from 0.50.21 (when per-source
telemetry shipped daemon-side) through 0.50.36 has had this
wrapper bug.

The 0.50.30 → 0.50.35 investigation thread chased a phantom
daemon-side bug for several iterations because of this. 0.50.30
refactored `get_image_path` and the worker to enforce coupling
that was already enforced. 0.50.31-0.50.34 built fingerprint
diagnostics for stale-deploy detection that was real but
unrelated. 0.50.35 added daemon-side response logging that
finally confirmed the daemon was emitting the right values —
but the wrapper bug only surfaced when the user noticed the
wrapper code itself, not from the diagnostic logs.

### What 0.50.37 fixes

- **`cawl_cache_status` wrapper forwards every field** the
  daemon emits, with safe defaults for older daemons:
  `offline`, `circuit_open`, `finished`, `from_cache`,
  `from_lan`, `from_upstream`, `last_source` — all the per-
  source telemetry the contract documents.
- **Empty fallback dict matches the success-case shape** so
  peers can safely call `.get(field, default)` without
  branching on transport failure.
- **Docstring updated** to list every field and call out the
  pre-0.50.37 bug explicitly, so a future debugger seeing the
  symptom on an older bundle has a pointer.

### CLIENT_INTEGRATION.md § 10 contract reframe

The 0.50.36 contract tightening framed the failure as a peer-
side regression. That framing is wrong — the rules themselves
(read every field, log raw, advance delta baselines) are still
reasonable peer-side hygiene, but compliance with them was
*impossible* against pre-0.50.37 bundles because the wrapper
stripped the fields before peer code could read them. The
section now says so explicitly, points at this wrapper as the
historical cause, and tells peers to check the bundled
`azt_collab_client.__version__` first when observing empty
per-source telemetry.

### Files

- `azt_collab_client/__init__.py` — `cawl_cache_status`
  forwards all fields; empty fallback matches; docstring lists
  the full shape; `__version__` 0.50.36 → 0.50.37.
- `azt_collab_client/CLIENT_INTEGRATION.md` § 10 — "Required:
  read every field verbatim" section reframed to acknowledge
  the wrapper was the actual cause.

### Compatibility

- Wire format unchanged; this fix is purely client-side.
- **Peer rebuild required** to pick up the wrapper fix. The
  daemon side is correct from 0.50.21 onward; what needs to
  ship is a peer APK rebuilt against client 0.50.37+ so the
  fields actually reach peer code.
- The diagnostic logs from 0.50.35
  (`[cawl] cache_status response:`, `[cawl] worker first bump:`,
  `[cache-status] (server-ui)`) stay in for this release while
  the user's peer rebuild propagates. Once a peer at 0.50.37+
  confirms it sees the per-source values reaching its
  `[cache-status]` log line, the daemon-side noise can come
  out in a follow-up.

### Mea culpa

I owe the user an apology for several rounds of confidently
misdirected investigation. The "daemon code is correct, must
be deploy" hedge was wrong, and even when the daemon was
exonerated by the 0.50.35 diagnostic, my next instinct was
"recorder bug" rather than "look at the wrapper that sits
between daemon and recorder." The user found it by reading the
wrapper code directly while I was still proposing diagnostics.

## 0.50.36 — Tighten the cache_status compliance contract

0.50.35's diagnostic log proved the daemon emits `last_source` /
`from_cache` / `from_lan` / `from_upstream` correctly on the
wire, and the recorder's `[cache-status]` debug line was logging
post-render values instead of the raw response values. Three
release iterations chased a non-bug in the daemon because the
peer's diagnostic shape didn't surface the wire truth.

To prevent the same investigation shape recurring, `CLIENT_INTEGRATION.md`
§ 10 "Per-source telemetry" now carries an explicit "Required:
read every field verbatim from the response" subsection with
three numbered rules:

1. Read the field — `status.get(...)` — on every poll.
2. Log raw, render flexibly. Peer diagnostic log lines MUST emit
   the unmodified response value. Render code may map
   `last_source='cache'` to an empty display tag (fine), but the
   *log line* must still show `'cache'`.
3. If peer-side delta tracking is computed across polls, the
   baseline MUST advance on each poll. Perpetual `Δcache=0`
   while the daemon's `from_cache` is climbing is broken delta
   tracking, not absent telemetry.

The "what good and bad look like" subsection below remains the
peer-team's display guide; the new "Required" subsection is the
contract surface peers must satisfy. Linked from the canonical
0.50.30 → 0.50.35 investigation thread so future readers can see
the failure shape that motivated the rule.

### Files

- `azt_collab_client/CLIENT_INTEGRATION.md` § 10 — new
  "Required: read every field verbatim from the response"
  subsection.
- `azt_collab_client/__init__.py` — `__version__` 0.50.35 → 0.50.36.

### Compatibility

Doc-only daemon-side. The diagnostic logs from 0.50.35
(`[cawl] cache_status response:`, `[cawl] worker first bump:`,
`[cache-status] (server-ui)`) stay in until the recorder
team resolves their side of the bug; will be removed or rate-
limited in a follow-up release.

## 0.50.35 — Diagnostic: log cache_status outbound response + worker first bump

0.50.34 confirmed via load-marker that the new cawl.py IS in the
deployed bundle. Yet the empty-`last_source` symptom persists with
no `[cawl] cache_status bug:` line firing. The contract code at
lines 884-888 cannot fail to fire on `last_source == '' and
cached > 0`. The worker code cannot bump `completed` without
bumping `last_source`. Both are observably contradicted. Two
remaining possibilities:

1. The daemon really IS setting `last_source='cache'` correctly,
   and the peer's `[cache-status]` log line drops or rewrites the
   value before printing — making the bug post-daemon.
2. The daemon IS sending `''` for some reason I can't see from
   reading the source.

This release adds three triangulation diagnostics (the recorder's
existing `[cache-status]` log is the fourth point):

- **`[cawl] worker first bump: source='cache' state['last_source']='cache' …`** —
  one-shot per worker session, fires inside `_prefetch_worker`'s
  bump block right after `state['last_source'] = source`. Proves
  the worker reached that line AND the assignment landed.
- **`[cawl] cache_status response: …last_source='cache' from_cache=N …`** —
  printed unconditionally from `cache_status` (in the daemon's
  `:provider` process) immediately before return. Shows the
  actual outbound dict.
- **`[cache-status] (server-ui) cached=N total=M …last_source='cache' …`** —
  printed by the server UI's `_tick_cawl_cache_status` (in the
  picker_app process) when it receives a response from the
  daemon. Shows what the picker_app process saw on the other
  side of the ContentProvider transport, separate from what the
  recorder saw.

Together, these let us triangulate which process boundary drops
`last_source`:

| Where empty appears |  Diagnosis  |
|---|---|
| `[cawl] cache_status response:` shows `''` | Daemon-side bug — code didn't write `last_source` despite the contract. |
| Daemon log shows `'cache'`, `[cache-status] (server-ui)` shows `''` | ContentProvider transport drops the field crossing the process boundary. |
| Daemon + server-ui both show `'cache'`, recorder shows `''` | Recorder-side render/log rewrite. |

After redeploy + reproduction:

- If daemon log shows `last_source='cache'` on the wire but the
  peer's `[cache-status]` log shows `last_source=''` → bug is on
  the peer side (recorder's render code drops the value).
- If daemon log shows `last_source=''` on the wire → bug is on the
  daemon side after all, and I've been misreading code somewhere.

The `cache_status response:` log fires on every poll. That's
deliberate noise for now while we're investigating; will rate-
limit or remove once we have the answer.

### Files

- `azt_collabd/cawl.py` — worker first-bump diagnostic;
  `cache_status` always-on response log.
- `azt_collabd/ui/app.py` — server UI cache-status tick logs
  the received response (parallel to the recorder's log).
- `azt_collab_client/__init__.py` — `__version__` 0.50.34 → 0.50.35.

### Compatibility

No wire format change. The new logs are stderr only.

## 0.50.34 — Per-module fingerprints + cawl.py load marker

Driver: the user verified 0.50.32 fingerprint mechanism works
(`a322…` → `26b7…` between two consecutive rebuilds), but the
underlying `last_source=''` bug still persists on the deployed
daemon. Conclusion: the combined fingerprint changing only proves
*something* changed in the bundle — typically the `__version__`
literal in `azt_collab_client/__init__.py`. It does NOT prove
that the SPECIFIC file carrying the bug fix (`cawl.py`, in this
case) was actually updated. A single-file deploy gap is exactly
the blind spot the combined hash has.

This release closes that gap two ways.

### Per-module fingerprint breakdown

- **`module_fingerprints()`** in `_fingerprint.py` — returns a
  sorted dict mapping each `.py`/`.pyc` file (key:
  `<pkg_name>/<rel_module>.<ext>`) to its individual 16-char
  hash. Same hashing inputs as the combined fingerprint, just
  not folded together.
- **`/v1/health.modules`** carries the dict on every health
  probe. Peers / diagnostic scripts can pull it without log
  scraping.
- **`python -m azt_collabd fingerprint --modules`** prints one
  `<hash>  <module>` line per file from the source tree.
  Output is grep-able and diff-able against the deployed
  daemon's `modules` dict. Diverging entries point at exactly
  the files that didn't update.

### cawl.py load marker

- A one-shot `sys.stderr.write` at the top of `cawl.py` prints:
  ```
  [cawl] module loaded; v0.50.34+ — tuple-returning get_image_path; worker bumps source under _cache_status_lock alongside state['completed'] += 1
  ```
- If you don't see this line in the daemon log after a deploy
  that's *supposed* to carry 0.50.34, `cawl.py` specifically
  didn't update — even if the combined fingerprint shifted.
- Removing or editing the marker requires editing `cawl.py`
  itself, so the marker can't survive a stale-unpack.

### Diagnostic recipe (use this when "the fix should be deployed
but the bug persists")

```
# 1. Snapshot what the source tree expects:
python -m azt_collabd fingerprint --modules > /tmp/source.txt

# 2. Pull the daemon's modules dict (via /v1/health or however
# you have access — desktop loopback can curl, Android requires
# the in-app surface):
# ...→ /tmp/deployed.txt

# 3. Diff:
diff /tmp/source.txt <(jq -r '.modules|to_entries[]|"\(.value)  \(.key)"' /tmp/deployed.txt|sort)
```

Cross-format caveat unchanged from 0.50.32: source-`.py` hashes
won't match deployed-`.pyc` hashes for the same module. The
deployed-vs-deployed comparison (before redeploy / after redeploy)
is the most useful — diverging entries are exactly the files that
DID update in this deploy cycle.

### Files

- `azt_collabd/_fingerprint.py` — new `module_fingerprints()`
  function.
- `azt_collabd/server.py` — `/v1/health` payload includes
  `modules` dict.
- `azt_collabd/__main__.py` — `fingerprint` CLI gains `--modules`
  flag.
- `azt_collabd/cawl.py` — load-time marker print at the top.
- `azt_collab_client/__init__.py` — `__version__` 0.50.32 →
  0.50.34. (0.50.33 was a one-line no-op bump the user shipped
  to verify the fingerprint mechanism shifts between deploys;
  no separate CHANGELOG entry for it.)

### Compatibility

- Additive on the wire — `modules` field is new; older peers
  that don't read it see no change.
- No `MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` bump.

## 0.50.32 — Fingerprint walker handles .pyc-only bundles (Android)

0.50.31's fingerprint mechanism only walked `.py` files. On Android,
p4a strips `.py` and ships only `.pyc` — so the walker found zero
modules and returned `SHA-256(b'') = e3b0c44298fc1c14…` regardless
of what code was actually deployed. (Caught by the user's first
test: source-tree CLI returned `ef4aa50…` against a real source
tree; daemon returned the empty-input hash.)

### What changed

- **`_collect_modules`** walks both `.py` and `.pyc` files. Handles
  three on-disk layouts uniformly:
  - `azt_collabd/cawl.py` (source tree)
  - `azt_collabd/cawl.pyc` (p4a / legacy `--no-source` layout)
  - `azt_collabd/__pycache__/cawl.cpython-311.pyc` (default
    Python compile cache)
- **`.cpython-XYZ[.opt-N].pyc` suffix stripping** so a module's
  identity is independent of the Python version that compiled it.
- **`__pycache__/` segment is folded out** of the rel-module
  path — that directory is a storage detail, not part of the
  module identity.
- **`.py` wins over `.pyc`** when both are present for the same
  module, so a developer machine with populated `__pycache__/`
  produces a source-tree-only fingerprint regardless of `.pyc`
  freshness.
- **PEP-552 header stripped** from `.pyc` content before hashing
  (first 16 bytes = magic + flags + timestamp/hash + source size),
  so two rebuilds of identical source on the same Python version
  produce identical fingerprints.

### Diagnostic boost in the boot line

- `[fingerprint] daemon=<hex> modules=<N>` — module count is now
  in the boot-time line. An empty fingerprint with `modules=0`
  is a configuration error (walker found no files), distinct
  from a real hash that happens to start with `e3b0c4…`. Helps
  the next time a similar layout mismatch surfaces.
- **`[daemon-log] mirroring stdio` line includes the fingerprint**
  on every install (fresh and respawn). So every daemon respawn
  prints the fingerprint visibly, not just first-import:
  ```
  [daemon-log] mirroring stdio to '/path/daemon.log' (appending —
  daemon 0.50.32 fingerprint=abcd123456789012 respawn)
  ```

### Cross-format comparison caveat

The deployed daemon hashes `.pyc` bytecode bytes; the source-tree
CLI hashes `.py` source bytes. **These differ in absolute value
even for the same logical content.** The useful comparisons:

- **Source-vs-source** (CLI run on two different checkouts):
  identical iff source matches.
- **Deployed-vs-deployed** (fingerprint before and after a
  redeploy): changes iff the deployment actually picked up new
  bytes. This is the "did my deploy take?" check.
- **Source-vs-deployed**: not directly comparable across formats.
  The diagnostic value is "the deployed fingerprint changed after
  the redeploy that was supposed to change it" rather than "the
  deployed fingerprint matches the source-tree hash."

### Files

- `azt_collabd/_fingerprint.py` — walker rewritten; helpers
  `_normalize_pyc_stem`, `_collect_modules`, `_file_hash_content`,
  `_count_module_files`; PEP-552 header handling.
- `azt_collabd/server.py` — `install_stdio_tee` includes
  `fingerprint=<hex>` in the boot log line.
- `azt_collab_client/__init__.py` — `__version__` 0.50.31 → 0.50.32.

### Compatibility

- Wire format unchanged. `/v1/health` still emits `fingerprint`
  the same way.
- No `MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` bump.
- The hash *value* for a given source tree changes between
  0.50.31 and 0.50.32 because of the `.pyc`-aware walker and
  `__pycache__/` folding — that's expected and one-time.

## 0.50.31 — Daemon content fingerprint: definitive "is the deployed code current?"

Driver: 0.50.30 shipped a daemon-side fix that should have made
`last_source=''` impossible while `cached > 0` was reported. The
device deployed 0.50.30 (confirmed via `/v1/health`), the daemon
log mirror line said `daemon 0.50.30 respawn`, and the source
tree confirmed our edits were in `cawl.py` (the diagnostic
`grep -c 'completed without source' azt_collabd/cawl.py` returned
`3`). Yet the bug pattern persisted: empty `last_source`, no
`[cawl] cache_status bug:` log, no `[cawl] bug: completed without
source` log. The daemon was reporting version 0.50.30 while running
older `.py` bytes — p4a's stale-unpack issue (`feedback_p4a_stale_unpack_on_apk_update`)
again. `__version__` is a single string that updates with one
file edit; the rest of the bundle can ship stale and we'd never
know from version probes alone.

This release adds the missing signal.

### Content fingerprint

- **New `azt_collabd/_fingerprint.py`.** Walks every `.py` file in
  `azt_collabd/` and `azt_collab_client/`, hashes `rel_path\0
  file_bytes\0` per file in sorted order, returns the first 16
  hex chars of the SHA-256 (64-bit prefix — collision-resistant
  enough in practice, short enough to eyeball-compare).
- **Computed once per daemon process at module load**, cached
  forever. Imported eagerly from `azt_collabd/__init__.py` so the
  first-call diagnostic print lands in the daemon log between the
  `before_import_azt_collabd` and `after_import_azt_collabd`
  boot-trace lines:
  ```
  [fingerprint] daemon=ab12cd34ef567890 (sha256 prefix; full=...)
  ```
- **Surfaced on `/v1/health`** as the `fingerprint` field
  alongside `version`. Peers reading `health()` see it
  unchanged-the-same-call.

### CLI helper

```
python -m azt_collabd fingerprint
```

Walks the *source tree* (azt_collab/ checkout) using the same
hash algorithm and prints the expected fingerprint. Compare
against the deployed daemon's `/v1/health.fingerprint`:

- **Same** → deploy took, code on device matches your source.
- **Different** → bundle is stale, deploy didn't actually pick
  up the latest bytes. Force a clean rebuild
  (`buildozer android clean` + flash again, per
  `feedback_p4a_stale_unpack_on_apk_update`).

### Why both inputs are hashed (daemon + client)

Both `azt_collabd/` and `azt_collab_client/` ship in the same
daemon-side bundle on Android. A stale unpack can leave either
or both packages with old bytes. Hashing both gives one number
that covers both surfaces; debugging "which package is stale"
isn't usually the question — "did anything change" is.

### When to look at it

- After every redeploy: confirm the fingerprint changed.
- After every "this should fix it but didn't" bug: rule out
  stale unpack as the cause before chasing logic bugs.
- Before reporting a daemon-side regression: confirm the
  deployed code matches the source tree you're reading.

### Files

- `azt_collabd/_fingerprint.py` — new module, the hashing logic.
- `azt_collabd/__init__.py` — eager-import of fingerprint at
  module load, exposes via `__fingerprint__`.
- `azt_collabd/server.py` — `/v1/health` payload includes
  `fingerprint`.
- `azt_collabd/__main__.py` — new `fingerprint` CLI subcommand.
- `azt_collab_client/__init__.py` — `__version__` 0.50.30 →
  0.50.31.

### Compatibility

- Additive change: `fingerprint` field on `/v1/health` is new;
  older peers that don't read it see no change in behaviour.
- No `MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` bump.
- The `health()` wrapper in `azt_collab_client.rpc` already
  returns the raw response dict, so peers read the new field
  via `health().get('fingerprint', '')` with no client rebuild
  needed.

### Independent: 0.50.30 fix still ships in this bundle

Once the user does the clean rebuild that this release encourages,
the 0.50.30 telemetry-correctness fix (refactored `get_image_path`
returning `(target, source)`, worker bumps source under the same
lock as `completed`, contract tightening on `cache_status`) is
also in the deployed bundle. The fingerprint helps confirm both
land.

## 0.50.30 — CAWL source telemetry: never-empty contract + catalog drop

Driver: field log of CAWL prefetch showing `cached=319 → 323` over
a 7 s window while `last_source` stayed empty and Δ-counters for
all three sources (`cache`/`lan`/`upstream`) reported 0. The
user's question was the right one: if bytes are landing, they came
from *somewhere* — the indicator should always say which. Empty
`last_source` while `completed > 0` is a contract violation; the
user can't tell whether LAN sync is doing its job, which was the
whole point of the per-source telemetry shipped in 0.50.21.

### Root cause shape

Pre-0.50.30, source bumping lived **inside** `get_image_path`
(`cawl.py:1442/1448/1505`), while `state['completed'] += 1` lived
**outside** in the prefetch worker. The two writes targeted the
same dict under the same lock but via different code paths;
nothing in the codebase enforced the coupling. Any code path that
returned a non-None target without going through one of the
three internal `_bump_source_counter` calls would let `completed`
move while `last_source` stayed `''`. The code I read doesn't
have such a path, but the contract was fragile by structure
rather than failsafe by structure.

### Structural fix

- **`get_image_path` and `get_image_path_lan_only` now return
  `(target, source)`** where `source` is one of `'cache'` /
  `'lan'` / `'upstream'` / `''` (only `''` when `target is None`).
  All internal `_bump_source_counter` calls removed.
- **`_prefetch_worker` bumps explicitly** in the same
  `with _cache_status_lock:` block that increments `completed`.
  The increment and the bump are now atomic under one lock by
  construction — they cannot drift apart.
- **`_SOURCE_FIELD`** mapping promoted to a module-level constant
  for shared use.
- **`lan_extras` pass** also rewritten to unpack the tuple and
  bump explicitly; matches the main loop's discipline.
- **`_bump_source_counter` stays public** for on-demand callers
  (loopback `_h_cawl_image`, Android ContentProvider's image
  open) so user-driven fetches during an active prefetch still
  contribute to the source counters — same pre-refactor
  behaviour from the user's perspective. Both call sites
  updated to unpack the tuple and call the helper explicitly.

### Defensive log + never-empty wire contract

Even after the refactor makes "completed without source"
impossible by construction, two layers of defense catch future
regressions:

- **Worker-side breadcrumb** — if the worker ever observes
  `target is not None and source == ''`, it logs
  `[cawl] bug: completed without source for <repo>/<path>`
  to stderr. Catches reintroduction of a return-non-None path
  that skips source tagging.
- **`cache_status` contract** — if `cached > 0 and last_source
  == ''`, the daemon logs `[cawl] cache_status bug: cached=N
  but last_source is empty` AND reports `last_source='unknown'`
  on the wire so peers never render an empty indicator while
  files are landing. Empty stays valid for the genuine
  "no fetch this session yet" initial state.

### Catalog drop (NOTES follow-up)

Two sentence-shaped msgids the recorder's 1.51.1 build uses for
the per-source progress indicator landed in
`azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
with French translations:

- `'Caching images: {cached} / {total} · via LAN'` →
  `'Mise en cache des images : {cached} / {total} · via LAN'`
- `'Caching images: {cached} / {total} · via Internet (please stay online)'` →
  `'Mise en cache des images : {cached} / {total} · via Internet (restez connecté)'`

No empty `msgstr` per `feedback_empty_msgstr_renders_blank`.
NOTES_TO_DAEMON.md entry for this item deleted per the
"delete on action" convention.

### Files

- `azt_collabd/cawl.py` — `get_image_path` /
  `get_image_path_lan_only` return tuple; `_prefetch_worker` /
  lan_extras pass bump explicitly; `_SOURCE_FIELD` constant;
  `cache_status` contract tightened.
- `azt_collabd/server.py` — `_h_cawl_image` unpacks tuple, calls
  `_bump_source_counter` for on-demand fetches.
- `azt_collabd/android_cp/service.py` — ContentProvider image
  open path same treatment.
- `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  — two new msgids with French translations.
- `azt_collab_client/__init__.py` — `__version__` 0.50.29 →
  0.50.30.
- `azt_collab_client/NOTES_TO_DAEMON.md` — cache-indicator
  msgids entry deleted.

### Compatibility

- No wire-format changes that break older peers. Older clients
  reading `cache_status` ignore unknown fields; the new
  `last_source='unknown'` value is a string they'll either show
  or ignore depending on their per-source rendering branch.
- No `MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` bump.
- The internal `get_image_path` signature changes inside the
  daemon only; no peer-facing function changed shape.

## 0.50.29 — Surgical LIFT edits: set_audio + set_illustration

Driver: `NOTES_TO_DAEMON.md` 2026-06-04 — the recorder runs on
low-memory devices (PowerVR Rogue GE8300, ~1 GB user-memory) and
field projects have crossed 4 MB LIFT (en-TH-x-anna hit 4,263,377
bytes on 2026-06-04 with the daemon's `[data-quality]` large-file
warning firing). The peer's current pattern holds the LIFT
`entries` dict (~1× source) plus the full ElementTree DOM (~5×
source) in memory just to serialise the tree back on every audio
save. Steady-state working set was ~25 MB on this project — enough
that Android's LMKD started reaping the peer process during normal
recording sessions, surfacing as "back to launcher icon" with no
Python traceback (kernel SIGKILL gives the runtime no chance to
log). Today's release lifts the DOM requirement from the peer by
moving the byte-level surgery into the daemon.

### What landed

- **`azt_collabd/lift_surgery.py`** — new module. Two public
  functions: `set_audio(working_dir, lift_path, guid, lang,
  filename)` and `set_illustration(working_dir, lift_path, guid,
  href)`. Shared pipeline:

  1. Read the LIFT as bytes.
  2. Locate `<entry guid="X">…</entry>` (or `<entry guid="X"/>`)
     via `_ENTRY_TAG_RE` over the file bytes; LIFT doesn't nest
     entries so a single forward scan suffices. Returns
     `[start, end)` byte range.
  3. Sub-parse just the entry bytes with `ET.fromstring` — a tiny
     in-memory tree, not the document.
  4. Edit: find-or-create the target sub-element. For audio:
     `<citation>/<form lang={lang}>/<text>`; other forms in the
     citation untouched. For illustration:
     `<sense>/<illustration href=...>` on the first sense.
  5. `ET.indent` at the file's detected indent unit (sniffs the
     whitespace before the entry's open tag — handles 2-space,
     tab, 4-space styles uniformly).
  6. `ET.tostring`, splice into the original file bytes by simple
     concatenation around `[start, end)`. Bytes outside the
     entry's range are preserved exactly.
  7. SAX-parse the spliced bytes to validate well-formedness;
     refuse to persist invalid XML.
  8. Sibling-tempfile + `os.replace`, holding `project_lock` per
     CLAUDE.md invariant #11.

- **`POST /v1/projects/<lang>/set_audio`** /
  **`POST /v1/projects/<lang>/set_illustration`** — daemon dispatch
  in `server.py`. On success: auto-fire `scheduler.commit_project`
  (debounced) and `android_cp.notify.notify_project_changed` so
  `ContentObserver` peers wake within ~10 ms (same shape as
  `_h_project_atomic_commit`).

- **Client wrappers** `set_audio(langcode, guid, lang, filename)`
  and `set_illustration(langcode, guid, href)` in
  `azt_collab_client/__init__.py`; added to `__all__`. Transport
  failures translate to `SERVER_UNAVAILABLE` / `SERVER_ERROR` per
  the wrapper contract.

- **Six new status codes** mirrored in both `status.py` files:
  `AUDIO_SET`, `AUDIO_SET_NO_CHANGE`, `ILLUSTRATION_SET`,
  `ILLUSTRATION_SET_NO_CHANGE`, `ENTRY_NOT_FOUND`, `LIFT_INVALID`.
  The `NO_CHANGE` variants let peers suppress redundant UI updates
  when a re-save of the same value lands. English + French
  translations added; no half-shipped empty `msgstr`.

### Guarantees provided to peers

Per the surgical contract:

1. **Byte-stable outside the target entry's bytes.** Every byte
   outside `[entry_start, entry_end)` equals the input file's
   bytes at the same offset. `git diff` shows only the one
   entry's lines as changed.
2. **Other forms in `<citation>` untouched.** The vernacular's
   `<form lang="seh">…` (or whatever the project uses for text)
   sits beside the audio form; we touch only `<form lang="{audio
   lang}">`.
3. **Well-formedness validation, mandatory.** A failed splice
   never persists; the original bytes remain on disk.
4. **Atomic write.** Sibling tempfile + `os.replace`. A crash mid-
   write leaves the previous file intact.
5. **`project_lock` held throughout.** Serializes against the
   daemon's own merge-output writes and any other `atomic_commit`
   from peers.
6. **`notifyStatusChanged` fires** on success so observer peers
   refresh fast.

### What the peer saves

Per the NOTES entry's math: the recorder's `_save()` no longer
needs to build the ElementTree DOM (`self._ensure_dom()` becomes a
no-op for these write paths). Peer steady-state LIFT memory drops
from `entries + DOM` (~6× source) to `entries`-only (~1× source) —
roughly 25 MB → 5 MB on the 4 MB en-TH-x-anna project that drove
the report. On smaller projects the absolute saving is smaller
but the relative cliff disappears: every recording session no
longer pushes a fresh ~20 MB allocation through the JVM heap.

### Files

- `azt_collabd/lift_surgery.py` — new module.
- `azt_collabd/server.py` — `_h_set_audio` /
  `_h_set_illustration` handlers + dispatch in the projects
  router.
- `azt_collab_client/__init__.py` — `set_audio` /
  `set_illustration` wrappers; `__all__` updated;
  `__version__` 0.50.28 → 0.50.29.
- `azt_collabd/status.py` + `azt_collab_client/status.py` — six
  new codes mirrored.
- `azt_collab_client/translate.py` — handlers for the new codes.
- `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  — French translations.
- `azt_collab_client/NOTES_TO_DAEMON.md` — surgical-set_audio
  entry deleted per the file's "delete on action" convention.

### Compatibility

- No `MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` change. Peers
  that don't call the new wrappers keep working with the old
  DOM-based save path; they only need to migrate when they want
  the memory relief.
- Wire format: two new endpoints, no breaking changes to existing
  ones.
- Peer migration: drop `_ensure_dom` / `self._tree` / `self._root`
  in the recorder's `lift.py`; rewrite `set_audio` and the
  illustration save path to call the new wrappers. Optimistic
  in-memory `entries` dict updates stay; the DOM goes away.

### Known follow-ups (not blocking)

- A peer-side normalize pass at recorder startup (one-time DOM
  build + write per entry, to lock in the daemon's `ET.indent`
  format) would make subsequent surgical edits produce minimal
  per-entry diffs from day one rather than spreading the
  reformat across many sessions. Not required for correctness.

## 0.50.28 — Surface mDNS-discovered unpaired peers in the UI

Discovery audit driver: pairing today is QR-only because the
"Nearby (unpaired)" sender flow documented in
`CLIENT_INTEGRATION.md` § 20 was never wired into any UI.
`lan_nearby_unpaired()` and `lan_pair_request_send()` shipped as
client wrappers; nothing called them. Users discovered each other
exclusively via QR scan, and the `KIND_PAIR_REQUEST` receive
path could never fire because no sender existed.

### What landed

- **`paired_phones_popup` (`azt_collab_client/ui/lan_popups.py:769`)
  rebuilt with two sections.** Top section "Nearby (unpaired)"
  calls `lan_nearby_unpaired()` and renders one row per mDNS-
  discovered device that is not in `peers.json`. Each row has a
  Pair button that fires `lan_pair_request_send(peer_id, '')`
  and replaces itself with "Waiting…" while a 2 s Clock poll
  drives `lan_pair_request_status(peer_id)` through `pending` →
  `accepted` / `declined` / `timeout`. On accept the row
  migrates into the Paired section on the next refresh. Bottom
  section is the existing paired-peers list (same `_build_peer_row`,
  same Manage / Unpair affordances).

- **Title and empty-state copy updated.** Popup title is now
  "Nearby & paired devices"; the empty state ("No nearby or
  paired devices yet…") nudges Refresh + QR scan as the two
  available routes.

- **New `lan_pair_request_status(peer_id)` client wrapper
  (`azt_collab_client/__init__.py`).** Thin wrapper over the
  existing `POST /v1/lan/pair_request_status` endpoint —
  returns `'pending'` / `'accepted'` / `'declined'` /
  `'timeout'` / `'none'`. Terminal states clear on read per the
  daemon contract in `_h_lan_pair_request_status`; the UI's
  poll loop sees the terminal state exactly once. Added to
  `__all__`.

- **Clock-event hygiene.** Every in-flight pair-request poll
  registers in `_active_polls[peer_id] = Clock event` and gets
  cancelled on popup dismiss / Refresh tap. Closing the popup
  mid-poll no longer leaks Clock callbacks.

### Why this seam (not the picker)

`paired_phones_popup` is the documented "peer roster /
settings screen" per `CLIENT_INTEGRATION.md:3044`. It is also
where users go to manage pairing already — adding the
discovery surface here keeps the entry points colocated.
The picker remains the home for QR-scan pairing (the in-
person first-pair flow) and for "open / clone a project."
Mixing nearby-peer discovery into the picker would smear
two different mental models.

### Files

- `azt_collab_client/__init__.py` — new
  `lan_pair_request_status` wrapper; `__all__` updated;
  `__version__` 0.50.27 → 0.50.28.
- `azt_collab_client/ui/lan_popups.py` — `paired_phones_popup`
  restructured into two sections; new helpers
  `_build_nearby_row`, `_cancel_all_polls`; shared
  `_active_polls` tracking.
- `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  — French translations for the 10 new msgids (no half-shipped
  empty `msgstr ""` per `feedback_empty_msgstr_renders_blank`).

### Compatibility

- No wire-format change. New wrapper hits an endpoint that has
  existed since 0.45.0; the UI uses returns that were already
  defined.
- No `MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` change.

## 0.50.27 — Stop creating duplicate GitHub repos on peer publish

Audit driver: a user-reported scenario where device A publishes a
project to GitHub, device B clones from A over LAN, then B taps
the Publish button. Pre-0.50.27 outcome: B silently creates a
second GitHub repo under B's namespace even when A's repo was
already known to the daemon. Two contributing bugs and three
propagation gaps, all closed in this release.

### Bug fixes

- **`_ensure_remote_repo` (`azt_collabd/repo.py:1200`) no longer
  creates a repo when the URL's parsed owner is not the
  authenticated user.** Pre-fix: `POST /user/repos` always
  created under the authenticated user, ignoring the URL's
  owner — so publishing to `A/<repo>` while authenticated as B
  produced an orphan `B/<repo>` as a side effect, then pushed
  to `A/<repo>` (which only succeeded if B was a collaborator).
  Post-fix: when `owner != username`, skip creation; the push
  succeeds if B is a collaborator on A's repo, otherwise the
  daemon's normal push-failure routing surfaces a clear error.
  New `S.REMOTE_OWNER_MISMATCH_SKIP_CREATE` typed status (mirrored
  in `azt_collab_client/status.py`, translated) so the user sees
  "Pushing to {owner}'s repo at {url} as collaborator…"
  instead of silence.

- **`_h_lan_adopt_origin` / `_h_lan_resolve_conflict` now write
  `.git/config` (`azt_collabd/server.py:917, 945`).** Pre-fix:
  accepting the adopt-origin pending decision updated
  `projects.json` (the registry) but left `.git/config` empty;
  the registry said "remote_url=X" while the working tree had
  no origin, so the next push had no remote to send to. Post-
  fix: both handlers also call `repo.set_remote_origin_url(...)`
  (new helper, holds `project_lock` per CLAUDE.md invariant
  #11) so adopted URLs land on the working tree's `.git/config`
  immediately.

### Propagation: `remote_url` as first-class LAN metadata

- **Post-publish fan-out (`server.py:1944`).** After
  `init_project` succeeds, the daemon now iterates every paired
  peer whose `shared_projects` allow-list contains this langcode
  and sends `lan_push.send_share_offer(peer_id, langcode,
  remote_url, vernlang)` to each. Best-effort, per-peer failures
  don't block the response. New helper
  `peers.peers_sharing_project(langcode)` does the iteration.
  Effect: A publishes → B (already paired and sharing this
  project) learns A's GitHub URL → B's `_do_publish` adopts the
  URL instead of inventing a duplicate.

- **Receive-side share-offer dispatch
  (`lan_listener._handle_share_offer`).** Pre-0.50.27 every
  incoming share-offer stashed `KIND_SHARE_OFFER`, even for
  projects the receiver already had registered. Post-fix the
  handler dispatches by local state:

  - Project not registered locally → `KIND_SHARE_OFFER`
    (today's "want to clone?" path).
  - Project registered, incoming `repo_url` empty → log + no-op.
  - Project registered, URLs match → log + no-op (steady-state).
  - Project registered, local `remote_url` empty, incoming
    non-empty → `KIND_ADOPT_ORIGIN` (peer is telling us where
    GitHub origin lives; user can opt in).
  - Project registered, URLs differ → `KIND_REMOTE_CONFLICT`
    (fork case; user resolves via the existing
    `_h_lan_resolve_conflict` UI).

  Client UI handlers for both `KIND_ADOPT_ORIGIN` and
  `KIND_REMOTE_CONFLICT` already exist (`decisions.py:141, 144`)
  so no peer rebuild is required — older peers see the new
  decision kinds and route through the existing popups.

### Publish UI safeguard

- **`_do_publish` (`azt_collabd/ui/app.py:2589`) refuses to
  invent a URL when one is already known.** Defensive guard:
  `_refresh_publish_row` already hides the row when
  `project_status.remote_url` is populated, but a stale-bound
  `on_release` could still land in `_do_publish` after
  acceptance of an adopt-origin decision elsewhere on the
  screen. Post-fix the handler re-checks `project_status` /
  `Project.remote_url` and shows "Project {langcode} is already
  published at {url}." instead of building a fresh
  `<user>/<langcode>` URL. Combined with the orphan-repo guard
  in `_ensure_remote_repo`, even a worst-case stale-bind no
  longer produces a duplicate repo.

### Known residual: concurrent-publish race

If A and B both tap Publish within the same second (mutual LAN
share, neither has published yet), neither has a pending
adopt-origin decision and both will create separate
`<self>/<langcode>` repos before the fan-out can propagate. The
next fan-out from each then lands on the other as a
`KIND_REMOTE_CONFLICT` and the user picks via the existing
resolve-conflict UI. Rare enough to leave as a known mode; not
solved by daemon-side election in this release.

### Files

- `azt_collabd/repo.py` — `_ensure_remote_repo` owner check;
  new `set_remote_origin_url(working_dir, url)` helper holding
  `project_lock`.
- `azt_collabd/peers.py` — new `peers_sharing_project(langcode)`
  iterator.
- `azt_collabd/server.py` — `_h_init_project` post-success fan-
  out; `_h_lan_adopt_origin` / `_h_lan_resolve_conflict` mirror
  `set_remote_url` to `.git/config`.
- `azt_collabd/lan_listener.py` — `_handle_share_offer`
  dispatches by local-state quadrant.
- `azt_collabd/ui/app.py` — `_do_publish` defensive guard.
- `azt_collabd/status.py` + `azt_collab_client/status.py` —
  `REMOTE_OWNER_MISMATCH_SKIP_CREATE` typed status.
- `azt_collab_client/translate.py` — translation for the new
  status.

### Compatibility

- Wire format unchanged. Share-offer payload still carries
  `peer_id`, `fp`, `device_name`, `langcode`, `repo_url`,
  `vernlang` — the change is the receive-side dispatch.
- No `MIN_CLIENT_VERSION` bump. Older peer builds (0.50.0+)
  already render the new decision kinds via existing
  `decisions.py` handlers.
- An older daemon won't fan out on publish; new peers gracefully
  degrade to today's behaviour (no propagation).

## 0.50.26 — Throttle urllib3 logger to INFO

dulwich was already capped at WARNING in `azt_collabd/net.py:18`,
but urllib3 was on its default level and kept emitting DEBUG
records on every HTTPS setup (`Starting new HTTPS connection`,
`Converted retries value`, …). Kivy's root handler caught them,
rendered with the `[DEBUG  ]` prefix and shipped to logcat
stderr (priority `E`). Pure noise during routine clone/fetch/
push, drowning real log content.

`logging.getLogger('urllib3').setLevel(logging.INFO)` drops the
DEBUG chatter while still letting real WARN/ERROR records
through. If we later decide urllib3 should be silent unless
something is wrong, ratchet to WARNING to match dulwich.

## 0.50.25 — `.azt/` no longer trips the data-loss-risk detector

The slot-claim / project-KV subsystem (`project_kv.py`, added
2026-05-28) stores per-device coordination state at
`.azt/kv/<key>.txt` and `.azt/slots/<slot>.txt`. `_stage_all`
commits these correctly — they're the inputs to the per-path
merge resolvers at the top of `repo.py` (lines 559-593).

The `_detect_uncommittable` allow-list (`_KNOWN_PATH_PREFIXES`,
`repo.py:4398`) was updated when `.azt-collab/` (diagnostics)
and `.azt_atomic_pending/` (intentionally excluded from git)
landed, but missed `.azt/` when the slot-claim subsystem
arrived. Result: every commit pass spammed
`[data-loss-risk] uncommittable file in project_dir: '.azt/...'`
to the daemon log and fired `S.DATA_LOSS_RISK` to peers,
routing user-side as the loud "your recordings aren't being
backed up" banner — for files that were in fact landing in
git just fine.

Fix: add `.azt/` (both posix and Windows separator forms) to
`_KNOWN_PATH_PREFIXES`. Mirrors how `.azt-collab/` is allowed
broadly — `_stage_all` already commits the whole subtree, so
the allow-list should reflect that.

Peers running pre-0.50.25 daemons will continue to see the
spurious banner until the daemon updates; this is a daemon-
side fix only, no client wire change.

## 0.50.24 — Quiet the Android-CP first-try transport probe

Two `[first-try] transport.call.pre` / `.post` log lines were
landing on every Android ContentProvider RPC — at boot the picker
fires ~10 RPCs in under half a second, so /sdcard logs were
drowning in ``bundle_null=False null_retries=0`` lines that carry
no information.

The probes were added in 0.41.16 to diagnose a "first-try-fails,
second-try-works" crash on a remote tester's Tecno KN4 who
couldn't run logcat — always-on emit was the only way to capture
the trail. That diagnosis fed into the null-Bundle retry loop
landed in 0.43.9, which is now the *fix* for the cold-spawn race
the probe was detecting. So in the steady state every routine
call ships the same `bundle_null=False null_retries=0` post-line
and a useless pre-line.

This release:

- Drops the pre-probe entirely. It carried no info beyond the
  post (same method+path are echoed back).
- Emits the post-probe only when something abnormal happened:
  `bundle is None` (structural denial) OR `attempt > 0`
  (cold-spawn retry actually fired).

Routine traffic stays silent; any re-occurrence of the null-
Bundle race still leaves a `transport.call.post` trail with
`bundle_null` / `null_retries` populated. The
`/cawl/cache_status` suppression branch is gone — the new
abnormal-only gate already covers the polling-loop noise it
existed to suppress.

Other `first_try_log` call sites (`lift_io.openFileDescriptor`,
`picker.on_enter`, `picker_app.main_entry`, settings-screen
ticks) are one-shot or low-frequency lifecycle events; left
unchanged.

## 0.50.23 — Confirm popup before boot-update download + skip-on-older

Closes the 0.50.20 / 0.50.21 thread on the boot-update flow.
The user reported seeing "Downloading n%…" appear unprompted
on every settings open — the old `_kick_boot_update_check`
called `check_for_update` directly, which starts downloading
without confirmation. Worse, a separate field case had the
flow trying to download an OLDER version than what was
installed.

### Confirm popup (was the immediate request)

New `_show_update_confirm_popup(latest_version)` on
``PickerApp``. Two buttons: "Update" and "Not now". Plain
inline popup (doesn't reuse ``install_server_apk_popup``
because that's shaped around the "server APK not installed"
case; this is the different "server APK is running, newer
release available" moment, with different copy and button
shape).

- **Update**: dismisses the popup and fires
  ``check_for_update`` — same download/install path as the
  in-settings Update button.
- **Not now**: just dismisses. The ``last_popped_tag`` stamp
  was already set BEFORE the popup fired, so the next
  settings open with the same latest tag just renders the
  badge without re-prompting. A newer release moves the
  comparison off this stamp.

### Version-sanity check (user-requested)

The existing ``latest <= running → skip`` gate is augmented
with an **explicit ``running > latest`` log line** so a dev
build sideloaded ahead of the published release surfaces in
the daemon log rather than being silently skipped. Quote:

```
[picker_app] boot update probe: running 0.51.0-dev is NEWER
than latest release 0.50.21 — skipping (sideloaded dev
build?)
```

Pre-0.50.23 also skipped this case (the ``<=`` comparison
catches it), but the silent-skip made it hard to confirm
from a log whether the version-tuple parse had gone weird in
a field-observed "downloading an older version" scenario.
The explicit log + the popup gate together close the
"unprompted download" surface.

### French translations

Added for the new popup strings ("Update available", body
template with {version}, "Update", "Not now"). The "Not now"
string already existed in the catalog for an unrelated
context.

## 0.50.22 — Document CAWL source telemetry in CLIENT_INTEGRATION.md

Documentation-only. `CLIENT_INTEGRATION.md` § 10
"CAWL image access / Daemon-driven prefetch" gains a new
sub-section **"Per-source telemetry (since 0.50.21) — surface
LAN vs Internet"** that:

- Lists the four new ``cache_status`` fields (``from_cache``,
  ``from_lan``, ``from_upstream``, ``last_source``).
- Shows two peer-side rendering patterns: minimal inline tag
  ("Caching images: 45/1700  · via LAN") and detailed
  breakdown ("12 from LAN · 33 from Internet · 0 already
  cached").
- Documents what good vs bad LAN-share signatures look like
  so a field tester reading the banner can tell whether the
  paired-peer cache is actually serving bytes.
- Pre-0.50.21 fallback note (default-zero on the unknown keys).

The actual telemetry shipped in 0.50.21; this release just
makes the peer contract explicit so recorder/viewer
maintainers know what fields to read and how to render them.

(Skipped 0.50.21's pending boot-update-auto-download fix —
user re-prioritised to this doc task first. The auto-download
question stays open; revisit in a separate release.)

## 0.50.21 — Boot-update popup throttled + CAWL prefetch source telemetry

Two unrelated items in one release.

### Boot-update popup fires once per release, not per settings open

User reports: opening the server APK's settings (launcher tap)
auto-pops the "newer release available" modal every time. The
0.41.x behaviour was "popup-on-boot if newer," intended as a
discovery surface — but it's intrusive when the user opens
settings frequently without intending to update.

Fix:

- New `store.get_last_popped_update_tag` /
  `set_last_popped_update_tag` track the most-recent release
  tag we've already surfaced as a popup.
- `_kick_boot_update_check` is restructured: always probes
  GitHub once per launch and badges if newer than running
  (unchanged silent-badge behaviour). Pops the modal only if
  the latest tag is **strictly newer** than `last_popped_tag`.
- After the modal fires we stamp `last_popped_tag = latest`,
  so the next settings open with the same latest tag just
  badges. A newer release lands → modal re-fires (one per
  release, which is the intent of the discovery surface).
- `'peer'` launch source is still badge-only (unchanged).

### CAWL prefetch source telemetry — LAN vs Internet

User asked: "Can we indicate which worked last (LAN vs
upstream) in the update text so we know which is being used?"
The NOTES #3 LAN-share path (0.50.14) is hard to confirm is
actually working without surfacing per-fetch source.

New fields on `cache_status(repo)`:

- `from_cache: int` — count of cache hits this prefetch
  session (file was already on disk).
- `from_lan: int` — count of bytes pulled from a paired LAN
  peer's cache.
- `from_upstream: int` — count of bytes pulled from
  GitHub.
- `last_source: 'cache' | 'lan' | 'upstream' | ''` — source
  of the most-recently successful fetch, for a one-glance
  "what's serving right now" indicator.

Wired via new `_bump_source_counter(repo, source)` helper.
Fetch paths in `get_image_path` and `get_image_path_lan_only`
call it on each successful resolve. Peer-side progress
display can now show e.g. "1245 from LAN · 12 from
Internet · 3 from cache (last: LAN)" so the user can see
whether the paired-peer share is doing its job.

## 0.50.20 — Contributor validity + clone-button visibility + auto-launch reliability

Four threads, batched.

### Contributor validity check

Field smoke surfaced the value ``)`` stored as a contributor —
passed the non-empty truthiness check everywhere but isn't a
usable display name. The settings UI's save path accepted it
because ``inp.text.strip() = ')'`` is truthy. Then every
downstream gate (picker visibility, lan_pair_accept, etc.)
treated it as "set" while the user thought their name wasn't
filled in.

- New ``store.is_valid_contributor(name)`` — requires at least
  one alphanumeric character (Unicode letter or digit).
  Rejects ``)``, ``!!!``, whitespace-only, etc. Empty string
  is the legitimate "clear" path and is NOT rejected.
- ``store.set_contributor`` now returns ``False`` and refuses
  to store an invalid input. Empty / whitespace-only still
  succeeds (= clear).
- ``_h_set_contributor`` surfaces the refusal as
  ``{ok: False, error: 'invalid_contributor', detail: ...}``
  so the peer can route to a clear error message.
- The picker's ``_refresh_contributor_state`` treats any
  alphanumeric-less value as unset for gating — junk in store
  no longer unlocks the receive button.

### Clone Internet Repository stays visible

0.50.18 hid the clone button when contributor unset; user
correctly pushed back — public-repo clones don't need a
contributor. Only the "Receive a project from another phone"
button is gated now. The notice text still mentions private
repos as a heads-up since hitting Clone on a private repo will
fail downstream, but the button stays clickable.

### Auto-launch reliability for null_bundle on a peer

User reports the 0.50.5 auto-launch isn't firing on a freshly-
cleared server. Two upgrades:

1. **Streak counts ``transport_error`` too**, not just
   ``null_bundle`` exact. A daemon crash-looping on a missing
   ``_python_bundle/`` can surface EITHER kind depending on
   whether the provider's Java side has registered yet when
   each peer call lands; 0.50.5 only counted one variant and
   the streak never accumulated to 3 in mixed runs. Now both
   advance the counter. ``daemon_not_ready`` still resets
   (it's the deliberate "boot in progress, just wait"
   signal). Unknown kinds neither advance nor reset.

2. **``_open_server_apk_launcher`` adds diagnostic logging at
   every failure seam** and a context fallback. Previously a
   single ``return False`` covered ``mActivity is None``,
   ``getLaunchIntentForPackage`` returning None, the
   ``startActivity`` call itself raising, and several other
   cases — peer-side logs gave no hint which one. Each path
   now emits its own
   ``[bootstrap] _open_server_apk_launcher: <step> <reason>``
   line. When ``PythonActivity.mActivity`` is briefly null
   (Activity recreation race), falls back to
   ``ActivityThread.currentApplication`` for the package
   manager + startActivity. Both context shapes accept the
   ``FLAG_ACTIVITY_NEW_TASK`` flag we set.

### Use plain `Label` for the picker's contributor-unset notice (was 0.50.19)

(Folded into this release.) 0.50.18 used ``BodyLabel:`` in the
picker KV, which depends on app.py's settings KV being loaded
first. Peer-app hosts (recorder, viewer) don't load that KV.
Replaced with a plain ``Label:`` carrying styling inline so
the picker KV is self-contained.

## 0.50.19 — Use plain `Label` for the picker's contributor-unset notice

0.50.18 used `BodyLabel:` in the picker KV. `BodyLabel` is a
dynamic-class rule (`<BodyLabel@Label>:`) defined inside
`azt_collabd/ui/app.py`'s settings KV. The PickerApp host loads
that KV via `register_settings_kv` *before* the picker KV
(`picker_app.py:223-225`), so the rule resolves there — but
peer-app hosts (recorder, viewer) don't load the daemon-side
settings KV at all, so the picker KV's reference to
`BodyLabel` either fails to instantiate or silently falls back
to a generic `Label` without the bold / dim styling the rule
provides.

Replaced with a plain `Label:` carrying the styling inline
(`color: T.RED`, `bold: True`, explicit `font_size` and
`font_name`). The change is self-contained — works in every
host that already loads the picker KV.

## 0.50.18 — Picker hides identity-gated actions when contributor unset

Per-user-request UX shape. When the contributor name isn't set,
two of the picker's three "add a project" buttons are downstream-
gated on identity:

- **Clone Internet Repository** — private-repo clone needs the
  authed user; git author falls back to ``@unknown`` and the
  daemon's init path refuses with ``CONTRIBUTOR_UNSET``.
- **Receive a project from another phone** — ``lan_pair_accept``
  refuses up-front with ``CONTRIBUTOR_UNSET``; the QR scan
  silently produces no pair (which is what shipped through
  0.50.10/0.50.17 trying to handle after the fact).

Cleanest UX is to **not offer the gesture at all** when it
can't succeed. Implementation:

- New KV ``BodyLabel id: contributor_notice`` at the top of
  the action stack, hidden by default (height=0, opacity=0).
- IDs added to ``clone_internet_btn`` and
  ``receive_from_phone_btn``.
- New ``ProjectPickerScreen._refresh_contributor_state`` reads
  ``get_contributor()`` and toggles all three widgets per the
  Kivy hide/show pattern in ``~/.claude-sil/CLAUDE.md`` —
  height+opacity+disabled set together so the buttons have no
  hit area and can't steal focus when hidden.
- Called from ``on_enter`` so the user who set their name in
  settings and came back sees the buttons reappear without
  leaving the picker.

Notice text: *"To clone from a private repo, or to get a project
from a local phone, go to settings and add your name first."*
French translation added.

The 0.50.10/0.50.12/0.50.17 routing logic on the scan-flow side
stays in place as a defense-in-depth — older builds, peers
that haven't refreshed their KV, etc. Belt and suspenders.

## 0.50.17 — Route `CONTRIBUTOR_UNSET` on QR scan to settings even when host is the server APK

0.50.12 routed the `CONTRIBUTOR_UNSET` branch of
`scan_to_pair._finish_on_main` to `open_server_ui()`, which on
Android fires a launcher intent against the server APK package.
That worked when the QR was scanned from a peer app
(recorder, viewer) — the intent flipped to the server APK's
picker_app with `launch_mode='internal'`, landing the user
directly on the settings screen.

Broken case: user scans the QR from the **server APK's own
picker** (the unified `picker_app` since 0.41.22). Firing a
launcher intent against the package the user is already in
just brings the picker Activity to the front — Android doesn't
restart it, so the `launch_mode='internal'` flag never gets a
chance to route to settings. User lands back on the (empty)
picker with no indication of what went wrong.

Fix in `lan_popups._finish_on_main`: detect whether the
current Kivy `App` has an in-process `go(name)` navigator + a
registered 'settings' screen. If yes (server APK's
`PickerApp`), call `app.go('settings')` to navigate within the
same Activity. If no (peer apps), fall through to the
cross-process `open_server_ui()` intent. The status toast
("Set your name on the next screen, then scan the QR again.")
fires either way.

## 0.50.16 — Close out audit-doc open-low items as wontfix

Doc-only. The four `[open-low]` items in
`.scratch/audit-2026-05-29-comms-data-loss-convergence/findings.md`
were evaluated for cost (project complexity) vs value: each
fix introduces some shape of new complexity (tri-state listener
state, persistent retry counters with UI surface, an Optional
in a value used by the sync badge, a new lock-holding write
site) for either zero value or value already covered by an
existing self-healing mechanism. Two items
(#10 `_count_commits_ahead` and #9 `_pending_resets` cap) also
actively conflict with intentional design choices documented
in memory and CLIENT_INTEGRATION.md.

Moved to `[wontfix]` with per-item rationale: #8 apply_toggle
async bind, #9 _pending_resets backoff, #10
_count_commits_ahead lock-timeout, "Half-strip `.git/config`
cleanup."

Audit closed: 8 done + 4 wontfix, no open items. The doc
stays as the historical record; if a future field report
re-opens any item with new evidence, append a `Re-opened
YYYY-MM-DD` line under the rationale and move it back to
`[open-…]`.

## 0.50.15 — Audit open-medium burn-down: #3 + #4 + #5 + #6

Closes the four open-medium items from
`.scratch/audit-2026-05-29-comms-data-loss-convergence/findings.md`.
After this release the audit roll-up shows **0 open-medium**;
only 4 open-low defensive nitpicks remain.

### #3 — Topic-branch orphan visibility

`repo._janitor_sweep_topic_branches` only sweeps refs with our
own device-name suffix, so cross-device orphans
(`refs/remotes/origin/azt-pending-fr-otherphone`) accumulate
indefinitely. Adding a count to `project_status` so a user
troubleshooting a heavy remote can see them.

- New `repo._count_foreign_topic_orphans(repo)` — walks
  `repo.refs.allkeys()` and counts `azt-pending-*` refs whose
  suffix isn't our `device_name`.
- New `ProjectStatus.foreign_topic_orphan_count` field (default
  0). Wired through `_h_project_status` in `server.py` and
  `ProjectStatus.from_dict` in `azt_collab_client/projects.py`.
- Informational only — peers can render a "remote has leftover
  branches" indicator. Not a sync-blocking condition.

### #4 — HEAD detached + ancestry guard refused — observability

`lan_listener.py` re-attaches HEAD to `refs/heads/main` after a
post-receive reset only when `head_is_ancestor(HEAD, main)` is
true. When ancestry legitimately fails (main NOT a descendant
of HEAD — local has unmerged work) the re-attach is unsafe and
gets skipped — but pre-0.50.15 that skip was silent. The
merge-loop the original 0.46.5 guard was supposed to break
could resume on the next receive.

Now emits a structured `[data-quality] head-detached-no-reattach
langcode=… head=… main=… reason=main-not-descendant-of-head`
log line whenever the ancestry check returns false. Greppable
from `adb logcat` or daemon-log share. No functional fix —
the safe action when ancestry fails is genuinely "do nothing"
(re-attaching to main would lose work at HEAD); the
observability gap is what the audit called out and what this
closes.

### #5 — Connectivity-probe adaptive backoff

The watcher's `_has_internet` probe ran at fixed
`connectivity_poll_s` (default 30 s) regardless of activity. On
an idle phone in a pocket all day that wakes the radio every
30 s for nothing. Pre-0.50.15 the WAN-backoff curve I shipped
in 0.50.0 covered the PUSH side but the underlying probe
itself wasn't adaptive.

- New module-state `_probe_idle_streak` + helpers
  `_adaptive_probe_interval(base)` / `_reset_probe_backoff()` /
  `_bump_probe_backoff()` in `scheduler.py`.
- Watcher loop increments the streak when state didn't change
  this tick; resets it when the probed state flips. Sleep
  doubles per step (30s → 60s → 2m → 4m → cap 5m).
- `drain_pushes_now` (user-nudge entry) resets the streak so a
  user gesture wakes the next probe promptly. The online-edge
  branch already resets via the state-change path.

### #6 — LAN endpoint cache TTL

`lan_discovery._endpoints` held resolved peers indefinitely. A
peer that restarted on a new ephemeral port stayed unreachable
until either the 3-failure restart-browse threshold tripped
(~90 s of "connection refused" hammering) or the user manually
flipped the LAN toggle.

- `_endpoints` value shape changed from `(host, port)` to
  `(host, port, monotonic_ts)`.
- `get_endpoint` returns None and drops the entry when older
  than `_ENDPOINT_TTL_S = 300.0` (5 min — covers ~5 mDNS
  re-announce cycles of headroom under standard zeroconf
  defaults).
- `known_endpoints` filters expired entries.
- Both writers (zeroconf `_record`, NsdManager
  `onServiceResolved`) stamp `time.monotonic()` on insert.

### Tests

`tests/test_audit_open_med.py` — 12 cases:

- #3: empty repo returns 0; own-suffix excluded; foreign
  suffixes counted; unset device-name edge case.
- #5: streak math doubles per step; caps at 300s; reset to 0;
  no-op reset.
- #6: fresh entry returns; expired returns None + drops;
  `known_endpoints` filters expired; unknown peer returns None.

#4 is observability-only (a log line) and isn't unit-tested —
its branch lives inside a deeply-nested LAN-receive handler
that needs a full mock receive-pack pipeline to reach. Field
matrix covers it via greppable `[data-quality]` tag.

## 0.50.14 — NOTES #3: LAN-shared CAWL cache between paired peers (with all-variants pull)

Closes the last open NOTES item. Daemon-only; **no peer rebuild
required** — `CAWLHandle.open_read` semantics don't change.

### Problem

Two phones sharing the same project each independently download
the full CAWL image set from the upstream repo (~1700 images
for SILCAWL). On a metered field link that's wasted bandwidth
twice over, plus contention between the two parallel prefetches
starves `auto_sync`.

### Fix

`get_image_path` in `azt_collabd/cawl.py` now tries paired LAN
peers' caches before reaching for GitHub:

1. Local daemon cache (unchanged).
2. LAN-discoverable paired peers' caches (NEW).
3. GitHub fetch via `cawl_image_repo` (unchanged fallback).

Plumbing:

- New listener endpoint `POST /v1/lan/cawl_fetch` in
  `lan_listener.py` (`_handle_cawl_fetch_bodyauth`). Body-auth
  via ``{peer_id, fp, owner, repo, rel_path}`` — same shape as
  other signalling endpoints. Response: 200
  `application/octet-stream` with the bytes if cached, 404 JSON
  if not, 403 JSON if peer_id/fp don't match `peers.json`.
  Accepts both nested rel_paths
  (`0001_body/foo.png` — preferred; disambiguates
  same-basename variants) and flat basenames (canonicalized via
  local index, back-compat).
- New requester `_fetch_image_bytes_from_lan_peer(repo, rel_path)`
  in `cawl.py`. Iterates paired peers (mDNS-resolved or
  static-endpoint), fires the POST against each, returns the
  first 200. Quietly returns None on no-peers / no-endpoints /
  all-404 so the GitHub fetch is unchanged.
- TLS-pinning shape matches `lan_clone._build_pool_manager` —
  self-signed cert trusted by fingerprint, not CA chain.

### LAN ignores the WAN variant-policy filter

`cawl.prefetch_all_variants=False` (the default) restricts WAN
prefetch to the preferred variant per CAWL id to save metered
bandwidth. **The LAN side doesn't honor this filter**: peer
bytes are free, so when one phone on the team has already
downloaded every variant, others get them all over LAN
regardless of their own variant-policy setting. The user's
phrasing: *"once one person on a team has downloaded all
images, others get them all for free, even if they set just
get one image/line."*

Implementation:

- New `_index_image_paths_all(repo)` — unfiltered full index.
- New `get_image_path_lan_only(repo, rel_path)` — LAN fetch
  without GitHub fallback; writes bytes to local cache on hit,
  returns None on miss.
- `start_prefetch(repo, paths, lan_extras=None)` takes a second
  list of paths to opportunistically pull from LAN only. These
  don't count toward `requested` / `completed` / `failed` in
  the cache-status state — they're bonus.
- `auto_prefetch` builds both lists: WAN-eligible = filtered
  (`_index_image_paths`); LAN extras = `all_paths - wan_paths`.
- `_prefetch_worker` runs WAN-eligible paths through
  `get_image_path` (LAN-then-WAN) as before, then runs LAN
  extras through `get_image_path_lan_only`. Skips lan_extras
  paths already on disk to avoid burning the per-peer
  iteration for no reason.

Cost shape: each lookup is one TLS handshake + one LAN
round-trip (~tens of ms). For a 1700-image prefetch where peer
A has the bytes cached and peer B is the requester, that's ~30
seconds of LAN work vs. minutes-to-hours of upstream cellular
download.

### Cases this doesn't fix

- Two peers prefetching simultaneously from cold caches: neither
  has the bytes to share yet. Both end up fetching from upstream
  (same as pre-0.50.14). The win only materializes once one peer
  has cached something.
- Peer B asks peer A, peer A has the byte but is currently
  offline / on a different Wi-Fi: 404 / connection failure
  surfaces, peer B falls through to GitHub. Same outcome as
  pre-0.50.14 for that image; not a regression.

### Tests

`tests/test_cawl_lan_share.py` — 12 cases covering the listener
endpoint shape (peer-auth gating, fp mismatch, 404 on missing,
200 on cached, basename canonicalization via index, traversal
refusal on basename / owner / repo) and the requester helper
(empty paired list / unresolvable endpoints / bad slug / bad
basename → None; multi-peer iterate with second-peer hit;
first-peer hit short-circuits).

### NOTES status

`NOTES_TO_DAEMON.md` live queue is now empty. The file stays
as the canonical queue for future peer-to-daemon items; it's
just at zero right now.

## 0.50.13 — Drop stale NOTES item #4 (fresh GUIDs shipped in 0.50.8)

NOTES housekeeping: "Fresh GUIDs when creating a project from
a template" shipped in 0.50.8 (`_mint_fresh_guids` in
`projects.py`, called from `create_from_template`), but the
NOTES entry wasn't deleted at the time. Per the NOTES rule
("When you act on an item, delete it from this file") the
CHANGELOG is the historical record; the live queue should only
hold open items.

Remaining open NOTES item after this cleanup: **LAN-shared
CAWL cache between paired peers** (bandwidth/battery win on
metered links when two phones share a project).

## 0.50.12 — Auto-route `CONTRIBUTOR_UNSET` on QR scan to server settings

0.50.11 added a popup for the `CONTRIBUTOR_UNSET` case, but the
established suite convention (per
`CLIENT_INTEGRATION.md` § 17 — *"toast + open_server_ui()"*) is
to skip the popup and route the user straight to the settings
page where they can type their name. Aligning the scan-flow
with that pattern.

`_finish_on_main`'s `CONTRIBUTOR_UNSET` branch now:

- Emits a status line via `_emit_status` (the picker's status
  bar) explaining "Set your name on the next screen, then scan
  the QR again."
- Calls `open_server_ui(on_status=_emit_status)` — Android
  fires the server APK's launch intent, desktop spawns
  `python -m azt_collabd ui`. User lands on the settings page
  directly, sets their name, returns to the peer to re-scan.
- No popup ceremony.

The `SERVER_ERROR` / `SERVER_UNAVAILABLE` and paired-but-no-
project branches from 0.50.11 keep their popups — those carry
detail the user (or a maintainer) needs to read.

## 0.50.11 — Surface `CONTRIBUTOR_UNSET` + `SERVER_ERROR` on QR scan

0.50.10 misdiagnosed: I claimed the user's scan failed because
the QR was pair-only (no langcode). Wrong — the QR was from
the "Share project (QR)" path and carried langcode just fine.
The actual cause: `_h_lan_pair_accept` calls
`_refuse_if_contributor_unset()` first, and when the receiving
phone has no contributor set the call returns 200/ok with
`Result(CONTRIBUTOR_UNSET)` — NOT `LAN_PAIRED`. The receiver's
`_on_result` worker checks `if w_result.has(S.LAN_PAIRED)` to
gate the clone phase, so an unset contributor silently skips
clone. The user saw the "LAN is on" popup (auto-enable fires
*before* pair_accept), landed on an empty picker, and pair
didn't actually land in `peers.json`.

Fix in `_finish_on_main`: route `CONTRIBUTOR_UNSET` to a clear
popup *"Set your name first"* explaining how to fix it. Also
route `SERVER_ERROR` / `SERVER_UNAVAILABLE` (bad QR payload,
transport failure) to a popup with the daemon's detail string,
so future pair refusals aren't silent either. The
paired-only-but-no-project branch from 0.50.10 stays — covers
the case where pair_accept succeeded but the daemon didn't
clone (langcode missing, share-allowlist refusal, etc.); body
copy slightly broadened.

The auto-enabled-LAN-toggle leak (we turn LAN on in
`_auto_enable_lan` BEFORE pair_accept's contributor check
fires) is a separate issue worth noting: a pair refusal still
leaves LAN on and prompts the "Keep on / Turn off" popup. The
right shape is to defer auto-enable until after pair_accept
returns LAN_PAIRED, but that's a bigger refactor and out of
scope for this fix.

## 0.50.10 — Surface "paired-only" outcome on QR scan

Field smoke on 0.50.9: user tapped "Receive a project", scanned
a QR, saw the "LAN is on" popup, landed back on the picker with
no project. From the log: `POST /v1/lan/pair/accept` succeeded;
no `/v1/lan/clone` call followed.

Diagnosis: the scanned QR was generated via "Pair a phone" on
the sender's side rather than "Share project (QR)", so its
payload had an empty `langcode`. The receiver's
`lan_popups._on_result` (since 0.49.3) gates the clone phase on
`if (langcode and peer_id and w_result.has(S.LAN_PAIRED))` —
which silently skips the clone when the QR didn't carry a
project. User saw no feedback, no error, no project.

Fix in `_finish_on_main`: surface a clear paired-only popup
when the result has `LAN_PAIRED` without `LAN_PROJECT_CLONED`
or `LAN_PROJECT_REOPENED`. Title: *"Paired, but no project
came with this QR"*. Body explains the next step: on the other
phone, open the project to share, tap **Share project (QR)**,
scan that QR. French translation added.

Doesn't fire on the collision case
(`LAN_PROJECT_COLLISION_UNRELATED`) which has its own routing.

## 0.50.9 — NOTES #2 closed: eager peer_id + claim tiebreaker + rebind_slot

Three-phase landing of NOTES item #2 (stable device identity
for slot-fallback matching), plus the audit-#9 tiebreaker
fix that lives in the same code path. NOTES #2 removed.

### Phase 1 — Eager-init `peer_id` on daemon startup

`reconcile_on_startup` in `scheduler.py` now calls
`peer_id.ensure()` unconditionally. Pre-0.50.9 the ed25519
keypair + self-signed X.509 cert were generated lazily — only
when LAN sync was enabled or the QR generator opened. On
builds that never enabled LAN, `lan_peer_id()` returned `''`,
slot claims got empty-peer_id entries, and peer-side fallback
matching chained to `device_name` (which can change during
the project's lifetime: server-APK reinstall, factory reset,
user edit). Eager-init makes `peer_id` reliably available so
it's the stable identity for slot claims, future per-device
state, and the audit-#9 tiebreaker.

Defensive: a build without the `cryptography` package falls
through with a logged warning rather than refusing daemon
startup. Same empty-peer_id behaviour as pre-0.50.9 for that
edge case.

Contract update in `azt_collab_client/CLAUDE.md`'s
"Daemon-owned state" table: `lan_peer_id` is now eager-init
since 0.50.9; persists across daemon respawn but NOT app-data
wipe.

### Phase 2 — Cross-peer-deterministic slot tiebreaker (audit #9)

Two NTP-synced phones claiming the same slot in the same
second tie on `claimed_at` (ISO-8601 second granularity).
Pre-0.50.9 the `_later_claim` tiebreaker fell back to
`peer_id` lexicographic — but with lazy peer_id init, both
sides often had empty `peer_id`, and the tiebreak returned
`a` (which is "ours" on whichever peer's merge is running).
Different peers picked different winners; the merge diverged.

`_later_claim` in `project_kv.py` now cascades:

1. `claimed_at` (later wins).
2. `peer_id` lexicographic (non-empty beats empty).
3. `device_name` lexicographic (legacy fallback when both
   peer_ids are empty).

The chain is a property of the claim itself, not of which
side of the merge it landed on — so peer A and peer B
compute the same winner. With Phase 1 eager-init, the
`device_name` tier only matters for transitional pre-0.50.9
claims.

### Phase 3 — `slot_rebind` RPC for identity recovery

New `POST /v1/projects/<lang>/slots/<slot>/rebind` endpoint
+ `rebind_slot(langcode, slot)` client wrapper. Rewrites an
existing claim's `peer_id` + `device_name` to the daemon's
current values and refreshes `claimed_at` to now.

Use case: this device's `peer_id` changed (server-APK
reinstall regenerated crypto; user cleared app data) but the
user knows the slot is still theirs. Peer-side guard rail is
a confirm popup driven by a contributor-name match against
the existing claim's `device_name`; this RPC is just the
persistence half. Daemon doesn't gate on anything beyond
input validation.

Returns `True` on success, `False` if the slot doesn't exist
(rebind only retags existing claims; for "claim or replace"
use `claim_slot`). Backed by new `project_kv.slot_rebind`.

Doc: `CLIENT_INTEGRATION.md` § 21 — added to the slot API
surface + the tiebreak section.

### Tests

`tests/test_slot_identity.py` covers: timestamp ordering,
non-empty-peer_id beats empty, device_name fallback for
legacy collisions, pathological-all-blank termination,
rebind rewrites identity, rebind refuses missing/invalid
slots, rebind preserves one-slot-per-peer invariant.

### NOTES status

Two open items remain: #3 LAN-shared CAWL cache between
paired peers, #4 (NOTES #4 was deleted in 0.50.8 — fresh
GUIDs at template import shipped). Original NOTES #1
(atomic_commit RPC) shipped pre-0.50, removed from queue
in 0.50.8.

## 0.50.8 — NOTES cleanup: drop #1 (atomic_commit), implement #4 (fresh template GUIDs)

NOTES audit pass after 0.50.7. One item already shipped on
the daemon side and was just stale in the file; another is
small + isolated and lands here.

### Dropped from `NOTES_TO_DAEMON.md` (was #1)

"Atomic LIFT commit on URI projects" — the
`/v1/projects/<lang>/atomic_commit` and
`/v1/projects/<lang>/atomic_finalize` RPCs are shipped (see
`server.py:_h_project_atomic_commit` / `_h_project_atomic_finalize`
and the dispatch at `server.py:3459-3461`). Client wrappers
`atomic_commit_bytes` / `atomic_finalize_pending` are exposed
in `azt_collab_client/__init__.py`. Tests in
`tests/test_atomic_commit.py`. The peer-side switch from
`open_write` fallback to these wrappers is a peer-side
migration, not a daemon-side gap, so the item is removed from
the daemon NOTES queue.

### NOTES #4 — Fresh GUIDs when creating from a template

`<entry guid="...">` values are only required to be unique
within one LIFT file, but templates currently propagate their
guids verbatim into every project derived from them — two
SILCAWL-derived projects share 1700+ identical guids
entry-for-entry. Any peer-side state keyed by guid alone
(caches, retry queues, future shared-clipboard features) ends
up ambiguous across project switches sharing a template
lineage.

Fix:

- New `azt_collabd/projects.py:_mint_fresh_guids(xml_bytes)` —
  walks all `<entry guid="...">` elements, rewrites each to a
  fresh UUID-4, then walks every `ref="..."` attribute and
  rewrites those whose value matches one of the just-rewritten
  guids (so intra-template `<relation>` links survive the
  rename). Conservative: refs whose value is NOT one of the
  rewritten guids (sense ids, etc.) are left alone.
- `create_from_template` calls it on the downloaded bytes
  before settling them at `<vernlang>.lift`. Defensive: any
  transform failure logs and falls back to the original bytes
  so a non-LIFT / malformed template doesn't break the
  download path (the downstream LIFT reader will produce a
  more specific error).
- One-shot at template→project conversion time; **existing
  projects keep their guids unchanged** — not a migration.

Tests in `tests/test_mint_fresh_guids.py`: guid freshness,
relation-ref follow-rename, non-entry refs preserved, non-LIFT
flow-through, malformed-XML flow-through, distinct guids per
run, 200-entry scale check.

## 0.50.7 — Make peer threading explicit in the contract; restore retry budget

Reverts the 0.50.6 budget shortening and moves the
responsibility to where it belongs: the peer contract.

The 0.50.5 splash-then-crash was a peer-side bug — the
recorder calls ``migrate_from_prefs`` on the main UI thread
at startup, so a 3 s null-bundle retry blocks frame rendering
past Android's ANR threshold. 0.50.6 shortened the transport
budget to 0.7 s as a workaround. That degrades the common
cold-spawn case (legitimate 1.9 s import: peers waiting one
extra bootstrap warmup tick) just to absorb a peer-side bug.

Better shape:

- **New § 17c Rule 7 in CLIENT_INTEGRATION.md**: *RPC calls
  MUST NOT run on the main UI thread.* Documents the failure
  mode (ContentResolver returns null on missing bundle →
  transport retries → main thread blocked → ANR) and the
  required peer shape (worker thread + ``Clock.schedule_once``
  marshal back). Covers startup-time RPCs explicitly —
  ``migrate_from_prefs``, ``check_server_compat``, last-project
  load — all of which are common offenders.
- **Transport budget restored** to
  ``_NULL_BUNDLE_RETRY_BACKOFF_S = (0.1, 0.2, 0.4, 0.8, 1.6)``
  (3.1 s cumulative). Same as pre-0.50.6. Comment updated to
  cite the new contract rule.
- Peer maintainers must move their startup RPCs off the main
  thread; this is now a hard contract violation, not a
  defensive workaround.

The 0.50.5 auto-launch + 0.50.4 adaptive popup still ship and
will fire normally once peers comply with Rule 7.

## 0.50.6 — Shorten null-bundle retry to keep main thread under ANR threshold

Field smoke on 0.50.5 surfaced a critical gap: on a freshly
cleared / freshly installed server APK, the peer just shows
its splash for ~3 s then **crashes outright** with no popup
and no recovery. None of the 0.50.4 / 0.50.5 logic (adaptive
popup, auto-launch on null-bundle streak) ever fires because
the peer process is already dead.

Root cause: `azt_collab_client/transports/android_cp.py`'s
null-bundle retry sleeps cumulatively 0.1+0.2+0.4+0.8+1.6 =
**3.1 s on whatever thread called it**. The recorder's
startup calls `migrate_from_prefs` synchronously on the main
UI thread before bootstrap takes over. With a missing daemon
bundle (`_python_bundle does not exist`), every retry returns
null, the budget burns through, and Android's ANR watchdog
sees a UI thread that hasn't drawn a frame in 3+ seconds →
ART kills the peer.

Fix: shorten `_NULL_BUNDLE_RETRY_BACKOFF_S` from
`(0.1, 0.2, 0.4, 0.8, 1.6)` to `(0.1, 0.2, 0.4)` —
cumulative 0.7 s, comfortably under any ANR threshold. The
remaining budget still absorbs hot-cold races (daemon
:provider idle-stopped seconds ago, Python respawn mid-import)
where 0.7 s is sufficient. Legitimate cold-spawn imports
(~1.9 s on mid-range Android) will surface `null_bundle`
sooner; bootstrap's adaptive warmup loop catches them on
attempt 2 or 3, which is one extra retry on the bootstrap
path vs one ANR-killed peer.

Architectural note: any peer-side RPC made from the main UI
thread is now main-thread-blocking for at most 0.7 s. The
proper long-term fix is for the peer to not make RPCs from
the main thread at all — that's a peer-side refactor, not
fixable at the canonical seam. The 0.7 s budget is a defense-
in-depth so even a worst-case startup-call-on-main-thread
peer survives.

## 0.50.5 — Auto-launch AZT Collaboration on `null_bundle` (no popup)

User pointed out that the existing stale-code recovery
(`_prompt_server_reboot_to_apply`) already auto-fires
`_open_server_apk_launcher` without making the user tap a
popup button — it just shows a toast. The `null_bundle` case
(cache-clear / fresh install / bundle missing) should follow
the same shape.

Change in `bootstrap.py`'s null-bundle fast-fail branch
(``_check_server``, the ``streak >= _NULL_BUNDLE_FAIL_FAST``
arm): instead of immediately showing the unresponsive popup,
the first time we hit the streak we auto-launch AZT
Collaboration with a toast (*"Setting up the sync service for
the first time — tap back when AZT Collaboration finishes
loading."*) and schedule a re-probe via
`_post_install_continuation`. New loop guard
`ctx._null_bundle_autolaunch_attempted` ensures a second
null-bundle streak after the auto-launch falls through to the
popup as before — i.e. if the user dismissed the server APK
before extract finished, or if the launcher intent itself
failed.

Reset of `ctx.null_bundle_streak = 0` on auto-launch so the
re-probe enters a fresh warmup loop rather than fast-failing
again on the very next null.

Net user experience for the fresh-install / cleared-cache
case: open peer → daemon crash-loops for a few seconds →
peer auto-flips to AZT Collaboration with a toast → user
sees picker UI loading → user switches back → daemon
extracted + ready, peer continues.

The 0.50.4 adaptive popup body still ships as the
last-resort message when the auto-launch couldn't recover
(launcher intent failure or user-dismissed-during-extract).

## 0.50.4 — Adaptive unresponsive-popup body for `null_bundle` case

Smoke surfaced the canonical post-cache-clear failure: the
server APK's `:provider` process crash-loops on `_python_bundle
does not exist`, because clearing app data wipes the unpacked
bundle but Python bootstrap inside `:provider` can't re-extract
itself (only PythonActivity does). Same failure shape after a
fresh install before the user opens the server APK.

Existing recovery (open server APK → its PythonActivity
triggers p4a bundle extract → return to peer) already worked,
but the peer's "AZT Collaboration not responding" popup
recommended "Restart server" first — which can't help when the
daemon's code isn't on disk to run.

Fix in `azt_collab_client/ui/bootstrap.py:_prompt_server_unresponsive`:
when `ctx.last_error_kind == 'null_bundle'`, swap the body to
lead with **Open {name}** as the right action and explicitly
note that **Restart server won't help in this case**. The
non-null-bundle path keeps the original wording. French
translation added.

User-visible flow after this change: open peer on a freshly-
cleared / freshly-installed device → daemon crash-loops for a
few seconds → bootstrap fast-fails at `null_bundle_streak >= 3`
→ adaptive popup tells user to tap Open. After Open: server APK
launches, p4a extracts the bundle, picker UI shows. Switch back
to peer → first compat probe may catch the daemon mid-startup
(returns daemon-not-ready or another null) → adaptive backoff
retries → second probe succeeds. "Fails once, then works" —
expected behaviour given bundle-extract + first-import timing.

## 0.50.3 — Contributor-name UX fixes from 0.50.2 smoke

Two bugs surfaced on first hardware smoke of the contributor
name field in the daemon settings UI:

- **Stale "Required: …" message**: still named "commit
  authorship" as the only reason to set a name. With LAN sync
  shipping the same field is also the peer label other phones
  see. Reworded to: *"Required: your name is used to label
  your work on both Internet sync (GitHub commits) and
  local-network sync (peer label other phones see). Sync
  refuses until this is set."* French translation updated to
  match.
- **"Saved." on empty input**: tapping outside an empty
  contributor field fired the focus-loss save handler, which
  ran `set_contributor('')` (a no-op-shaped success) and
  rendered "Saved." in dim text — overwriting the red
  Required-… error and making the empty state look accepted.
  `save_contributor` now early-returns when the trimmed input
  is empty, leaving whatever message was up in place. The
  Required-… error stays visible until the user actually
  types a name.

## 0.50.2 — Burst-mode LAN discovery + online-edge auto-recovery

Closes the two phases the 0.50.0 release deferred: burst-mode
mDNS (Phase 3) and the online-edge auto-recovery hook (Phase 6).
The `lan.autodiscovery` default also flips to **False** now that
burst mode has the LAN do useful work when it's off.

### Phase 3 — Burst-mode LAN discovery

New `azt_collabd/lan_burst.py`: `start_burst(window_s=30.0)`
arms a discovery window by incrementing the `lan_fgs` discovery
ref + calling `lan_listener.apply_toggle()`. Worker thread
sleeps until expiry, then disarms. Multiple concurrent
`start_burst` calls share the same worker — *latest expiry
wins*, never shortens a longer in-flight window.

`lan_listener.apply_toggle()` now reads the union of
`lan.autodiscovery` *and* the `lan_fgs` discovery ref count, so:

- `autodiscovery=True` → continuous LAN (today's behaviour).
- `autodiscovery=False, no burst` → LAN fully down. Saves
  battery; needs a user gesture to rendezvous.
- `autodiscovery=False, burst active` → listener + mDNS + locks
  come up for the window; tear down after.

Burst triggers wired:
- `_h_sync_nudge` (user tapped sync icon).
- `_run_commit`'s `COMMITTED_LOCAL` branch (so a fresh commit
  re-arms LAN even with autodiscovery off).
- The connectivity watcher's online-edge handler (Phase 6).

### Phase 6 — Online-edge auto-recovery

Cheap alternative to a real `ConnectivityManager.NetworkCallback`
(which would need PythonJavaClass glue, ACCESS_NETWORK_STATE,
and manifest work). The existing connectivity watcher already
fires `_has_internet()` every `connectivity_poll_s` (default
30 s); we hook its offline → online transition to:

1. `wan_backoff.nudge(langcode)` for every pending-push project
   — next drain tick fires immediately instead of waiting out a
   24 h curve point.
2. `lan_burst.start_burst()` — paired peers freshly on the same
   Wi-Fi can rendezvous without a user gesture.

Latency: up to one connectivity-poll tick (~30 s) vs a real
NetworkCallback's near-instant delivery. Acceptable trade for
not adding Java glue + a manifest permission.

### `lan.autodiscovery` default flipped to False

Per the 0.50.0 plan: now that burst mode covers the
`autodiscovery=False` case, the default flips. Existing users
with persisted `lan.allow_sync=True` keep autodiscovery on
(migration unchanged). Fresh installs default to burst-only —
the battery win the 0.50 rework targeted.

### Tests

`tests/test_lan_burst.py` covers: ref-arm on `start_burst`,
disarm after window, concurrent-extend math (latest expiry
wins, refs don't double), and that `apply_toggle` brings the
listener up when a burst is armed even with
`autodiscovery=False`.

## 0.50.1 — Naming + test plumbing fixups after 0.50.0

Small follow-ups after 0.50.0 shipped its first round of edits;
no behaviour change beyond the rename.

- **Settings key rename**: `lan.passive_discovery` →
  `lan.autodiscovery`. Reads from the user's perspective:
  `True` = the device discovers automatically (user passive),
  `False` = the user has to nudge. The accessor
  (`settings.lan_autodiscovery` / `set_lan_autodiscovery`) and the
  env var (`AZT_LAN_AUTODISCOVERY`) move with it; the back-compat
  shims `lan_allow_sync` / `set_lan_allow_sync` still delegate
  correctly. Migration from pre-0.50 `lan.allow_sync` is
  preserved.
- **Project-root `conftest.py`** added so `pytest tests/…` (and
  not just `python -m pytest`) resolves `azt_collabd` / 
  `azt_collab_client` imports. No `pip install -e .` needed.
- **Per-test `$AZT_HOME` isolation**: `tests/conftest.py:azt_home`
  now clears `paths._AZT_HOME_CACHE` so each test's tmp dir
  actually takes effect. Without this, the first test seeded
  the module-global cache and every subsequent test wrote to
  *that* dir — a silent state-leak that masked stale tests as
  passing.
- **Stale `test_contributor.py` device-name tests rewritten**
  against the 0.49.0 derive-from-contributor contract.
  Pre-0.49.0 they tested an independent persisted `device_name`
  field with its own autodetect + setter; 0.49.0 made it derived
  and `set_device_name` a no-op. The old tests were silently
  passing only via the cache leak above; the rewrites pin the
  current contract.
- **French translation drift fixups**: 12 missing msgids filled
  in with plausible French — memory-merge warning, paired-device
  manage popup wording, the `'Share [{{langcode}}] project'`
  KV-template variant, etc. Refine in a future pass if a French
  reviewer flags anything off.

## 0.50.0 — Power-driven sync rebuild: WAN backoff + LAN lifecycle

End-to-end rework of how the daemon manages connectivity attempts
and the LAN radio, driven by the 2026-05-29 design conversation
on power cost. Replaces "fire every 30 s while toggle is on" with
"fire on user gesture + exponential backoff for failure recovery."

`MIN_CLIENT_VERSION` bumped to 0.50.0 because the semantics of
`sync.work_offline` change and a new `sync_nudge` RPC is the
canonical sync gesture; pre-0.50 peers calling deprecated paths
may be confused about toggle state.

### Phase 1 — Persistent WAN backoff

New `azt_collabd/wan_backoff.py`: per-project exponential backoff,
30 s → 1 m → 2 m → ... → cap at 24 h. State persists to
`$AZT_HOME/wan_state.json` because a 24 h backoff is meaningless
if every Android OOM-respawn resets to "try now." On daemon
restart `reset_due_times_on_startup` clears `next_attempt_at` for
a free immediate retry while preserving `consecutive_failures`,
so a fresh failure re-enters the curve at the right step rather
than starting fresh.

The 24 h cap is sized for field workflows where the phone may be
offline for 14 days at a time: at the cap we probe once a day,
~365× less radio chatter than the pre-0.50 30 s cadence, with no
loss of eventual recovery — the user can always tap sync to break
out of the curve. Tests in `tests/test_wan_backoff.py` pin the
math and the persistence contract.

`scheduler._drain_pending_push` now gates per-project on
`wan_backoff.is_due(langcode)`; the `work_offline` gate and the
`post_online_grace_s` gate are removed (the curve does both jobs
more cleanly). The watcher loop's per-tick cost on an offline-
for-hours project drops to one comparison-against-timestamp.

### Phase 2 — Unified `sync_nudge` RPC

New `POST /v1/sync/nudge` endpoint + `sync_nudge(langcode='')`
client wrapper. Resets WAN backoff for one project (or all
projects if empty langcode), fires an immediate WAN push attempt,
and fires LAN fan-out. Same semantics as the sync icon: "try
everything now, ignore backoff."

Per-project gesture: `sync_nudge(langcode='fr')`. Daemon-wide
sync icon: `sync_nudge()`. The old `sync_project(langcode)` is
kept for callers that need the per-project synchronous
push-and-return contract (publish flush, etc.); for the user-tap
case, prefer `sync_nudge`.

### Phase 3-5 — LAN autodiscovery toggle migration

Replaces `lan.allow_sync` with `lan.autodiscovery`:

- **Default `True` in 0.50.0** (was: `False` for
  `lan.allow_sync`). Reason: the burst-mode discovery that makes
  `autodiscovery=False` useful is deferred to 0.50.1 — in
  0.50.0, False means LAN is entirely off. To avoid a UX
  regression for fresh installs, the default stays at "always
  discoverable" until burst-discovery lands; the default will
  flip to False with 0.50.1.
- **Migration**: existing users with `lan.allow_sync=True` keep
  autodiscovery on (no behaviour change). Existing users with
  `lan.allow_sync=False` keep it off.
- **`sync.work_offline` is deprecated** and always returns
  `False` regardless of persisted value. The exponential backoff
  curve replaces the user-toggle for the offline case; the
  "working but metered network" case (the one real use the
  toggle covered) is logged for future per-network policy.

New ref-counted lock lifecycle in `android_cp/lan_fgs.py`:
`arm_for_discovery` / `disarm_for_discovery` (MulticastLock + FGS
for the burst), `arm_for_transfer` / `disarm_for_transfer`
(WifiLock + FGS for the active push). Operations increment;
completion decrements; at zero refs with `autodiscovery=False`
everything goes down. Validates on desktop without jnius —
platform-side calls early-return, ref math runs anyway. Tests
in `tests/test_lan_fgs_refcount.py`.

### What's not in this release (deferred)

- **Burst-mode mDNS discovery** — the briefly-on rendezvous
  window that gives `autodiscovery=False` any meaning. The
  lifecycle plumbing (ref-counted `arm_for_discovery`) is in
  place but the `burst_discovery(window_s)` orchestration that
  ties `sync_nudge` into a temporary lights-on window is a
  follow-up. In 0.50.0, `autodiscovery=False` effectively
  means "LAN entirely off" — paired peers can't find each other
  without flipping the toggle back on. That's why the 0.50.0
  default is True (preserving today's behaviour); the default
  flips to False once burst-mode lands in 0.50.1.
- **`ConnectivityManager.NetworkCallback` for auto-recovery on
  Wi-Fi (re)connect** — only matters when
  `autodiscovery=True`; deferred.

### Smoke tests included

- `tests/test_wan_backoff.py` — curve math, persistence, nudge
  semantics, restart behaviour, corruption handling.
- `tests/test_lan_fgs_refcount.py` — ref-count balance, nesting,
  thread safety, back-compat for pre-0.50 callers.

Run with `pytest tests/ -q`.

## 0.49.4 — LAN clone timeout + post-clone host-load guard

Two follow-ups to the 0.49.3 QR pair-clone fix.

### Daemon: bounded `dulwich.porcelain.clone` in `lan_clone`

`azt_collabd/lan_clone.py:_do_lan_clone` ran `porcelain.clone`
with no socket timeout. A wedged peer held the RPC open until
the client's default `rpc.call` timeout (300 s) gave up, after
which the daemon's caller still didn't know whether to retry
or report the failure differently.

- New `_LAN_CLONE_TIMEOUT_S = 180.0` (under the 300 s RPC
  timeout so the daemon can return a typed status before the
  client times out the call).
- `_socket_timeout` context manager (mirror of
  `repo._socket_timeout`; duplicated rather than cross-imported
  to keep `lan_clone` independent of the heavier `repo`
  module).
- New typed status `LAN_CLONE_TIMEOUT` (`peer_id`, `langcode`,
  `timeout_s`, `detail`) emitted from `clone_from_peer` when
  `_do_lan_clone` tags the failure with the `clone_timed_out:`
  sentinel. Generic clone failures still route to
  `LAN_PEER_UNREACHABLE` (which already covers "couldn't
  reach an endpoint"). `_looks_like_timeout` walks the
  exception cause/context chain so dulwich/urllib3 wrapping
  shapes don't slip through.

### Client: user-visible toast on the failure shapes

`azt_collab_client/ui/lan_popups.py:_finish_on_main` previously
silently fell through when the QR clone Result didn't carry
`LAN_PROJECT_CLONED` / `LAN_PROJECT_REOPENED`. Now:

- New `_show_lan_failure_popup(title, message)` — one-button
  info popup, dismissable. Inlined in `lan_popups.py` rather
  than promoted to `popups.py` because the only callers are
  the two LAN clone failure shapes; promote later if a third
  site needs the same shape.
- `_finish_on_main` checks for `LAN_CLONE_TIMEOUT` and
  `LAN_PEER_UNREACHABLE` and renders the right popup — but
  only when the result doesn't ALSO carry a success code
  (avoids a scary timeout toast over a usable project).
- New `LAN_CLONE_TIMEOUT` translation handler in
  `translate.py`. French strings added.

### Picker: guard `app.load_lift` against host-raised exceptions

`azt_collab_client/ui/picker.py:receive_from_phone:_on_done`
called `app.load_lift(path, langcode)` (peer host's callback)
without exception handling. A raise inside the host (FS error,
XML parse failure on a fresh-clone LIFT, schema mismatch)
propagated out of the Clock-scheduled finisher; Kivy logged
the traceback and the user was left on the picker with the
popup dismissed and no project actually opened. Now wrapped:
on failure, log the host's exception and `_populate_projects`
so the new project row is at least visible.

The host's data-loss handling is still the host's
responsibility — this guard only prevents the picker from
ending up in undefined state.

## 0.49.3 — Progress popup + threaded RPCs on QR pair-clone

Field report: new phone, no existing project, QR scan succeeded
but then "went to a black screen." Root cause: the picker's
"Receive from another phone" → "Scan QR code" path dismissed its
own popup at `lan_popups.py:_scan` (line 1055) **before**
`scan_to_pair` was called. After the QR-scanner activity returned
to Kivy, `_on_result` invoked `lan_pair_accept` + `lan_clone`
**synchronously on the Kivy main thread**. First-contact LAN
clone over TLS is 10 s — minutes; for the duration the main
thread was blocked and the SDL surface stayed black with no
widget to draw.

Fix in `azt_collab_client/ui/lan_popups.py:_on_result`:

- Mount a non-dismissable "Receiving project" popup immediately
  after the JSON payload validates. Two-line content: live phase
  label ("Pairing with the other phone…" → "Copying project to
  this phone…") + a sub-line warning that first-time copy can
  take a minute or two.
- Run `lan_pair_accept` + `lan_clone` on a daemon worker thread
  so the Kivy event loop keeps rendering frames. The popup we
  just mounted only gets drawn if the main thread can run a
  paint pass before the RPC starts — without threading, the
  popup is invisible too.
- Phase-label updates marshal back to the main thread via
  `Clock.schedule_once`. Completion likewise — `progress_popup.
  dismiss()` runs on main, then routes into the existing
  `_resolve_adopt_origin_then_done` / `_final_done` chain.
- Exceptions in the worker collapse to a typed `SERVER_ERROR`
  status carrying `error='pair/clone raised: …'` so the
  picker's `on_done` path still has a Result to dispatch on
  instead of the user seeing a stuck popup.

New translatable strings (added in `translate.py` is not needed —
they're rendered via `_tr` directly from `lan_popups.py`; French
entries added to `locales/fr/LC_MESSAGES/azt_collab_client.po`):
"Receiving project", "Pairing with the other phone…", "Copying
project to this phone…", "First-time copy over the local network
can take a minute or two. Please keep both phones close
together."

Independent follow-ups not landed in this release (logged in
`.scratch/audit-2026-05-29-comms-data-loss-convergence/findings.md`):

- `azt_collabd/lan_clone.py` has no client-side socket timeout
  around `dulwich.porcelain.clone()`. A wedged peer holds the
  RPC open for the full transport-layer default. Worth adding a
  bounded timeout + typed `LAN_CLONE_TIMEOUT` status so the
  worker thread can route an "is the other phone still nearby?"
  toast instead of waiting forever.
- No exception routing if post-clone `open_project` /
  `load_lift` raises. Belongs in the picker's `on_done` consumer,
  not in this popup.

## 0.49.2 — Wire `extra_remotes` through the push path

0.49.0 documented (CHANGELOG section 4) that the push iteration over
`Project.extra_remotes` was not yet wired through `repo.py` — the
data model captured the user's "Use both" preference but only the
primary `remote_url` was actually pushed. Closed in this release:

- New `_push_extras_step(repo, project_dir, result)` in
  `azt_collabd/repo.py`. Iterates each entry in `extra_remotes` and
  publishes the local branch tip with a publish-only refspec (no
  fetch, no merge). Per-URL credentials via
  `get_sync_credentials(extra_url)` so an extra on a different host
  uses the right token.
- Called after the primary `_push_step_locked` in both
  `_push_repo_locked` (scheduler drain loop) and `_sync_repo_locked`
  (user-Sync button). **Tries every URL each call, independent of
  the primary's success/failure** — a transient primary failure
  doesn't suppress secondary publishes.
- New typed status codes `EXTRA_REMOTE_PUSHED` (params: `url`,
  `branch`) and `EXTRA_REMOTE_PUSH_FAILED` (params: `url`, `error`),
  mirrored in `azt_collab_client/status.py`. Translations added to
  the client's English handlers and the French .po. Auto-sync paths
  should route silent on the failure code; user-Sync may surface a
  per-remote breakdown.
- Misleading comment in `server.py:_h_lan_resolve_conflict` rewritten
  to name `_push_extras_step` (was: `repo._push_to_remote_url`, which
  doesn't exist).

No `MIN_CLIENT_VERSION` bump — old peers that don't decode the new
codes will render them via the unknown-code fallback (`[CODE]
{params!r}`) until they update. Same wire shape, additive only.

Behaviour change peers should be aware of: a project on "Use both"
that previously appeared to be silently dropping commits to the
secondary will now actually deliver them. If the secondary host
diverged during the gap, the first push pass after upgrade will
surface `EXTRA_REMOTE_PUSH_FAILED` with a non-FF rejection — the
user reconciles the secondary by hand (the daemon never fetches
from secondaries).

## 0.49.0 — Peer-interaction round: nearby pairing, shared decisions watcher, KV / slot claims

End-to-end work driven by the 2026-05-28 architecture
discussion on "peer interaction for 3+ devices on the same
project." Consolidates what was iteratively built up as
0.47.7 / 0.47.8 / 0.47.9 during the session.

### Theme

When the suite was two phones, every peer relationship
fit into a QR scan. With three phones and an existing
pair-mesh, the rough edges showed: opaque auto-generated
peer labels, no way to find a peer who's in the room but
unpaired, asymmetric shares ("I share with you, you don't
share back"), no convergence guarantee when the same word-
list slot is claimed twice. This release closes those gaps.

### 1. Nearby-pair flow (KIND_PAIR_REQUEST)

mDNS already surfaces unpaired devices on the LAN. New:

- ``lan_nearby_unpaired()`` client wrapper returns the
  discovered-but-not-yet-paired devices.
- ``lan_pair_request_send(peer_id, langcode='')`` POSTs an
  outbound pair request to a discovered peer with our
  current-project langcode as pair context. Receiver sees
  the shared decisions watcher popup (see #2).
- ``lan_pair_request_resolve(decision_id, accept)`` on the
  receiver's side. Accept records the pair + sends
  hello-back (standard flow records the pair on sender
  side); decline POSTs ``pair_response{accept:false}`` so
  sender clears the spinner.
- Sender-side outbound state lives in
  ``azt_collabd/lan_pair_requests.py`` (in-memory, 5-min
  timeout). Status codes
  ``LAN_PAIR_REQUEST_PENDING|ACCEPTED|DECLINED|TIMEOUT``.
- Listener endpoints ``/v1/lan/pair_request`` (receiver)
  and ``/v1/lan/pair_response`` (sender) use the new
  ``_https_post_signalling`` helper — TLS encrypted but
  fp not pinnable yet (peer not paired). Same threat
  model as ``hello_to_peer``.

QR scanning still works; this is the *additional* path for
"we're in the same room, both already running the suite,
neither has a QR to scan."

### 2. Shared decisions watcher (`ui.decisions`)

Every pending decision the daemon stashes (share offers,
pair requests, adopt-origin, remote-conflict) is now
rendered by **a single shared client UI** —
``azt_collab_client.ui.decisions``. Peers replace ad-hoc
polling of ``lan_pending`` with one
``install_decision_watcher()`` call in ``on_start``; the
watcher renders the modal popups, calls the existing
per-kind resolve RPCs, and fires an
``on_resolved(kind, action, decision)`` callback so the
peer can refresh its own state.

- Wrap-friendly labels (long device names, langcodes, URLs
  word-wrap instead of clipping).
- "Internet" wording for origin URLs (the daemon supports
  GitHub, GitLab, and self-hosted; no GitHub branding in
  popups).
- ``KIND_REMOTE_CONFLICT`` is three-way: Keep mine /
  Switch to theirs / Use both. "Use both" appends to
  ``Project.extra_remotes`` (see #4).
- ``KIND_SHARE_OFFER`` accept is **passive**: the new
  project lands in the registry but ``last_project`` is
  not touched. Peer's ``on_resolved`` may refresh the
  project list; must NOT auto-load. (The QR-scan and
  Nearby-pair paths remain *active* — they DO move
  ``last_project``.)

Contract details + migration checklist in
``CLIENT_INTEGRATION.md`` § 20a.

### 3. Peer identity gated on ``contributor``

``store.get_device_name()`` is now derived state:
``f'{contributor} — {autodetect}'`` when contributor is
set, empty when not. ``set_device_name`` becomes a no-op
(kept callable for pre-0.48 client compat). User-facing
setting is the contributor field — one input, two derived
outputs (git author name + peer label).

``CONTRIBUTOR_UNSET`` now gates pair-accept, pair-request-
send, pair-request-resolve (accept), send-share-offer,
slot-claim, and lan_set_toggle (turn-on). Peer UIs already
route this status (the GH publish path uses it today), so
existing handling does the right thing.

Net effect: a user who hasn't set their contributor name
can't accidentally advertise an anonymous ``moto g - 2025``
to other peers; ``Kent — moto g - 2025`` shows up after
they fill in the contributor field once.

### 4. ``KIND_REMOTE_CONFLICT`` "Use both" — data model

``projects.Project.extra_remotes`` (list of additional
Internet-hosted remote URLs) with ``add_extra_remote`` /
``remove_extra_remote`` setters.
``_h_lan_resolve_conflict`` mode ``'dual_publish'`` now
appends ``incoming_url`` to ``extra_remotes`` (was a
no-op flag pre-0.48).

**Gap, documented honestly**: the push iteration over
``extra_remotes`` is NOT yet wired through the ~8
``porcelain.push`` sites in ``repo.py``. The data model
captures the user's "Use both" preference but only the
primary ``remote_url`` is pushed today. Follow-up release
will add the iteration. Acceptable interim because the
user judged this case "infrequent" in the architecture
discussion and the data model is the durable part.

### 5. Project-shared KV + atomic slot claims

Cross-phone agreement on per-project state that doesn't
fit in the LIFT file (``team_size``, "who's on which
recording slot", etc.). Files in the working tree under
``.azt/kv/`` and ``.azt/slots/``, synced through the
existing LIFT pipeline.

- ``project_kv_get/set/list`` for scalars
  (``.azt/kv/<key>.txt``).
- ``list_slots`` / ``claim_slot`` / ``release_slot`` for
  slot claims (``.azt/slots/<slot>.txt`` with content
  ``<peer_id>\n<claimed_at_iso>\n<device_name>``).
- ``slot_claim`` atomically (locally) displaces any prior
  claim by this device on a different slot — the one-
  slot-per-peer invariant.
- Convergent across phones: two simultaneous claims of
  the same slot both land; the daemon's merge driver
  (extended ``repo._merge_diverged`` with path-prefix
  branches for ``.azt/slots/*.txt`` and
  ``.azt/kv/*.txt``) picks the version whose embedded
  ``claimed_at`` is later. Loser sees on next sync that
  they're not in ``list_slots`` and is re-prompted by the
  peer UI.
- KV / slot writes hold ``project_lock`` during the
  on-disk write, the same lock
  ``_reset_working_tree_after_receive`` takes during its
  status check + integrate-or-reset decision. Closes the
  receive-races-a-pending-write hole: the receive's
  ``porcelain.status`` runs either fully before our write
  (no unstaged path to clobber; hard reset is safe) or
  fully after (sees our file as unstaged; runs
  ``integrate_head_into_working_tree`` through the
  slot/KV merge branches instead of reset). No window
  where status reads clean and ``reset --hard`` fires
  over a fresh write.

Peer contract + locked semantics + recommended flow in
``CLIENT_INTEGRATION.md`` § 21. NOTES_TO_DAEMON.md item
deleted (acted on).

### 6. Mutuality contract for shares

``KIND_SHARE_OFFER`` accept already auto-mirrors via the
clone path (existing flow). New: receiver-side popup is
the only place share offers surface — no more "I tapped
Share, the other phone never sees it" because the
watcher polls 1 s and the popup is near-instant on same
LAN. The pre-0.48 § 20 hard rule 2 ("don't poll
lan_pending faster than 5 s") is superseded — the
watcher is the only poller now.

### Files

New modules:

- ``azt_collab_client/ui/decisions.py``
- ``azt_collabd/lan_pair_requests.py``
- ``azt_collabd/project_kv.py``

Touched:

- ``azt_collab_client/__init__.py`` — 9 new wrappers
  (``lan_pair_request_send`` / ``_resolve`` /
  ``lan_nearby_unpaired`` / ``project_kv_get`` / ``_set`` /
  ``_list`` / ``list_slots`` / ``claim_slot`` /
  ``release_slot``), ``lan_clone`` gains
  ``user_initiated`` kwarg, ``__all__`` updated,
  ``__version__`` → ``0.49.0``,
  ``MIN_SERVER_VERSION`` → ``0.49.0``.
- ``azt_collab_client/ui/__init__.py`` — re-exports
  ``install_decision_watcher``.
- ``azt_collab_client/status.py`` — adds
  ``LAN_PAIR_REQUEST_PENDING|ACCEPTED|DECLINED|TIMEOUT``.
- ``azt_collab_client/translate.py`` — translations for
  the four new status codes.
- ``azt_collab_client/CLIENT_INTEGRATION.md`` — new
  § 20a "Shared decisions watcher" and § 21 "Project-
  shared KV and slot claims" with peer migration
  checklists. § 20 hard rule 2 superseded.
- ``azt_collab_client/NOTES_TO_DAEMON.md`` — KV item
  deleted (acted on); LANOK item also deleted (resolved
  in 0.47.0).
- ``azt_collabd/__init__.py`` —
  ``MIN_CLIENT_VERSION`` → ``0.49.0``.
- ``azt_collabd/status.py`` — mirror of new pair-request
  codes.
- ``azt_collabd/pending_decisions.py`` — adds
  ``KIND_PAIR_REQUEST`` constant.
- ``azt_collabd/lan_listener.py`` — adds
  ``_handle_pair_request`` + ``_handle_pair_response``
  body-auth handlers + dispatch.
- ``azt_collabd/lan_push.py`` — adds
  ``_our_endpoint_str`` and
  ``_https_post_signalling`` (unpaired-peer signalling).
- ``azt_collabd/server.py`` — 10 new RPC handlers:
  4 for the pair-request flow,
  6 for KV/slot. ``_h_lan_clone`` reads
  ``user_initiated``; ``_h_lan_accept_offer`` drops the
  ``last_project`` set; ``_h_lan_resolve_conflict``'s
  ``dual_publish`` mode writes ``extra_remotes``.
  ``_refuse_if_contributor_unset`` helper gates 5 LAN
  endpoints.
- ``azt_collabd/projects.py`` —
  ``Project.extra_remotes`` field +
  ``add_extra_remote`` / ``remove_extra_remote``.
- ``azt_collabd/repo.py`` — ``_merge_diverged`` gains
  slot/KV merge branches.
- ``azt_collabd/store.py`` — ``get_device_name`` derived
  from contributor; ``set_device_name`` no-op.

### Wire format

Additive. New endpoints listed under each section above.
``MIN_CLIENT_VERSION`` and ``MIN_SERVER_VERSION`` both
bumped to ``0.49.0`` — peers that predate the new
pending-decision kind mis-render incoming
``KIND_PAIR_REQUEST`` entries, and old daemons can't
emit them; the floor bump means cross-version mismatches
show the upgrade popup instead of partial behavior.

### 7. Bootstrap recovery: Restart server + safe wording

Two UX fixes on the "server is installed but the daemon
isn't responding" path:

- The unresponsive-popup
  (``_prompt_server_unresponsive``) now wires an
  ``on_restart_server`` callback through to the canonical
  install popup, so the "Restart server" button appears.
  Same code path the sync-settings page uses
  (cooperative ``POST /v1/admin/restart``); empirically
  the most reliable recovery when the ``:provider``
  process is wedged / frozen / stale-bundle. Body text
  promotes Restart server as the primary action ahead of
  Open / Quit. New
  ``_restart_server_unresponsive`` helper falls back to
  ``_prompt_server_unresponsive`` on failure (instead of
  the server-too-old popup, which is the wrong identity
  for this flow). The previously-rendered "Try again"
  button is dropped from this popup — Restart server
  subsumes it (cooperative restart re-probes compat on
  acceptance, which is what Try-again did).
- The stale-bundle path
  (``_prompt_server_reboot_to_apply``) no longer shows
  a popup at all. The previous popup recommended
  "uninstall and reinstall AZT Collaboration" (data
  loss — uninstall wipes ``$AZT_HOME`` including
  projects, credentials, jobs;
  [[never-suggest-uninstall-apk]]) and offered a
  Restart server button that didn't actually help. Both
  were wrong: per ``server_apk/service.py``'s comment
  block on ``_maybe_reextract_python_bundle``,
  cooperative restart of ``:provider`` cannot fix a
  stale bundle because the proper unpack does
  ``recursiveDelete(files/app/)`` first, wiping the
  very code ``:provider`` is running.

  The actual fix is to launch the server APK's
  ``PythonActivity`` (which runs in a separate
  process), where p4a's bootstrap sees the
  ``.version`` markers that ``:provider`` invalidated
  on its last spawn and re-extracts the bundle
  cleanly. The new path fires
  ``_open_server_apk_launcher()`` on detection: shows
  a toast "Refreshing the sync service code — tap back
  when AZT Collaboration finishes loading", sends the
  launcher intent, schedules a compat re-probe.
  Android brings AZT Collaboration to the foreground;
  PythonActivity re-extracts; user navigates back; the
  next ``:provider`` lazy-spawn loads the fresh code.
  Loop guard prevents re-firing if the user dismisses
  AZT Collaboration before the extract completes.
  Fallback popup retained only for the case where
  ``getLaunchIntentForPackage`` returns null (jnius /
  PackageManager unhealthy on the peer); body text
  there tells the user to open AZT Collaboration from
  the launcher manually.

  ``_show_update_blocked_popup``'s previously-added
  ``on_restart_server`` parameter is removed since no
  caller now uses it.

### 8. Uniform theming for popups + their buttons

Every Python-built popup in the LAN / peer flows
(Paired devices, Receive a project, Pair request,
Adopt origin, Remote conflict, share-project, scan-to-
pair, install-server, …) used Kivy's stdlib ``Popup``
and ``Button`` — neither of which follows the suite's
active palette. Result: bevelled grey buttons on a grey
9-patch backdrop, two visual languages on the same
screen as the themed picker.

New ``azt_collab_client/ui/themed_popup.py``:

- ``ThemedPopup`` — paints ``theme.BG`` into the popup
  backdrop, ``theme.TEXT`` title text,
  ``theme.ACCENT`` separator. Drop-in for
  ``kivy.uix.popup.Popup``.
- ``ThemedButton`` — replaces ``kivy.uix.button.Button``
  with the picker's ``RecBtn`` / ``NavBtn`` visual
  (8 dp rounded corners, themed fill + text colours,
  9-patch bevel stripped). Default flavour is the
  picker's secondary ``NavBtn`` (``theme.SURFACE``
  fill, ``theme.ACCENT`` text). Callers that already
  pass ``background_color=theme.ACCENT`` to mark a
  primary action auto-switch to the ``RecBtn`` flavour
  (``theme.ACCENT`` fill, white text) without any
  callsite change.

Imports swapped in ``lan_popups.py``, ``decisions.py``,
``popups.py``, ``bootstrap.py``. The bootstrap
``_show_update_blocked_popup`` ModalView gains the
same canvas.before paint inline (one callsite, not
worth a separate themed subclass). Theme tracking is
live — toggling palette via ``theme.set_theme`` would
re-render the next-opened popup against the new
palette without code changes.

## 0.47.6 — LangPicker re-pick: restore region_scroll visibility

### Diagnosis

After 0.47.5, picking a region collapsed the picker by setting
`region_scroll.opacity = 0` and `disabled = True`. When the
user then re-typed in the search field and picked a *different*
language, `_select_language` rebuilt the buttons inside
`region_box` but never reset `region_scroll`'s opacity/disabled
back to visible/enabled. The KV-bound height followed
`region_box.minimum_height` correctly (hence the
"appropriately sized blank spot") but the contents stayed
invisible.

### Fix

`_select_language` now resets `region_scroll.opacity = 1`,
`region_scroll.disabled = False`, and (in the multi-region
branch) `region_title.disabled = False` before populating the
buttons. `_change_region` already did the equivalent on its
path; this just brings the second entry point into line.

### Files

- ``azt_collab_client/ui/langpicker.py`` — restore
  `region_scroll`/`region_title` visibility in `_select_language`.

### Wire format

None. UI-only.

## 0.47.5 — LangPicker tightens results cap; region collapses after pick

### Fix

1. The results-list cap was `dp(520)` (≈ 10 rows), but the
   soft-keyboard covers roughly the bottom half of the screen
   when the search field has focus, so the last few matches
   were hidden behind it. Cap is now `dp(416)` (8 rows of
   `dp(48)` + spacing) so the visible list stays above the
   keyboard on a standard phone.

2. After the user picks a region, the picker (title +
   ScrollView of region buttons) now collapses to a single
   line — `<region name> (rc)` plus a small `Change` button.
   Tapping `Change` re-expands the picker (and clears
   `_selected_region`). State per step is now:
   1. language name (`selected_label`)
   2. region name + `Change` (collapsed picker)
   3. dialect checkbox + optional variant input
   4. assembled language code
   5. Continue

   When no region has been picked yet, the picker stays open
   exactly as before. `_hide_selection` and `_select_language`
   both reset the chosen-region one-liner so navigating away
   and back lands on a clean state.

### Files

- ``azt_collab_client/ui/langpicker.py`` — `results_scroll`
  cap `dp(520)` → `dp(416)`; new `region_chosen` BoxLayout
  in `_SELECTION_KV` (Label + `Change` button); new
  `_change_region` method; `_select_region` / `_hide_selection`
  / `_select_language` toggle picker vs. chosen-one-liner
  visibility.

### Wire format

None. UI-only.

## 0.47.4 — LangPicker results list cap + reset on re-search

### Fix

Two related fixes to LangPickerScreen:

1. The language-search results list (`results_box`) had
   `size_hint_y: 1.0` default, so it grew to fill all remaining
   vertical space even with only a handful of matches. Now
   capped at `dp(520)` (≈ 10 buttons of `dp(48)` + spacing) and
   only scrolls past that — same shape as the region list in
   0.47.3.

2. After the user picked a language, editing the search field
   again populated `results_box` *below* the inserted selection
   panel (so new matches appeared beneath the Continue button).
   `_on_search_text` now treats any non-empty edit while a
   language is already selected as "start over": clears
   `_selected` / `_selected_region` / `_dialect_code` and tears
   down the selection panel before scheduling the new search.

### Files

- ``azt_collab_client/ui/langpicker.py`` — cap `results_scroll`
  at `dp(520)`; reset selection state in `_on_search_text` when
  a language was already chosen and the search field is edited
  to non-empty text.

### Wire format

None. UI-only.

## 0.47.3 — LangPicker region list scrolls past ~10 entries

### Fix

The "Select region:" panel in LangPickerScreen rendered every
region as a `dp(38)` button stacked in a non-scrolling BoxLayout.
For languages with many regions (e.g. en, fr, ar), the panel
ran off the bottom of the screen and the Continue button was
unreachable. The region list now lives inside a `ScrollView`
capped at `dp(420)` (≈ 10 buttons) — the list grows freely up
to that cap, then scrolls.

### Files

- ``azt_collab_client/ui/langpicker.py`` — wrap `region_box` in
  a `ScrollView` with `height: min(region_box.minimum_height,
  dp(420))` so it collapses to 0 when empty and caps at ~10
  rows when full.

### Wire format

None. UI-only.

## 0.47.2 — Auto-commit after atomic_finalize / atomic_commit

### Diagnosis

Field session 2026-05-27 (post-0.47.1 build): phone recorded
an entry, badge showed ``+1 red`` (n_changes=1) indefinitely
until the user recorded another entry. Root cause: the daemon's
``commit_project`` debounce timer fired before a final
``atomic_finalize`` landed — the commit captured the
pre-finalize state, then the ``project_lock``-serialized
atomic_finalize ran *after* the commit released the lock,
leaving the just-finalized LIFT (and any post-commit
``[image-save]``) as uncommitted bytes on disk. The recorder
fired ``commit_project`` only once for the whole save sequence;
since the daemon's job was already in-flight, no further commit
was scheduled.

### Fix

``_h_project_atomic_finalize`` and ``_h_project_atomic_commit``
now call ``scheduler.commit_project(langcode)`` after the rename
succeeds. The scheduler's 500 ms debounce coalesces bursts —
typical record-then-save sequences with the recorder also firing
commit_project collapse to one commit run; the daemon auto-fire
only matters when the peer-side commit_project trigger is
missing or dropped. Cheap (sets/resets a timer);
``NOTHING_TO_COMMIT`` if nothing changed.

### Files

- ``azt_collabd/server.py`` — ``_h_project_atomic_finalize``
  + ``_h_project_atomic_commit`` schedule a debounced commit
  after successful rename.

### Wire format

None. Pure Python additive — no clean rebuild needed
(``buildozer android debug`` is enough).

## 0.47.1 — Push notifications (per-project ContentProvider URIs)

### Diagnosis

Polling-only project_status updates make the sync-indicator
badge feel sluggish during sync cascades. A typical merge cycle
takes 5-15 seconds; with a 10 s polling tick the badge can lag
real daemon state by up to one tick — visible to the user as
"why is it still red" after sync visibly completed in logs.

The previous architecture (§ 17b "Why peer-side polling and not
daemon-pushed notifications") rejected push because adding a
reverse channel "would require a peer-declared service +
permission + lifetime management — substantial Android-API
surface." That assessment overlooked Android's existing
``ContentResolver.notifyChange`` / ``registerContentObserver``
pair, which uses the same ContentProvider that already carries
all our RPC traffic — zero new permission, zero new service,
suite signature permission already gates registration.

### Fix

Per-project status URIs scoped under the existing
``org.atoznback.aztcollab`` authority:

- ``content://org.atoznback.aztcollab/status/<langcode>``
  fires for HEAD advance, peer observation update, post-
  receive reset, absorb-reset on that one project.
- ``content://org.atoznback.aztcollab/status`` fires for
  daemon-wide changes (toggle flips). Observers registered
  with ``notifyForDescendants=true`` on this URI also catch
  every per-project notification — so a project-list /
  picker UI subscribes once.

### Daemon side

- **Java** (``AZTCollabProvider.java``): static method
  ``notifyStatusChanged(String langcode)`` that calls
  ``getContentResolver().notifyChange``. ``onCreate`` captures
  the application context into a static volatile field so the
  notify method can be called from any thread without an instance
  handle.
- **Java** (``AZTStatusObserver.java``, new): ``ContentObserver``
  subclass that delegates to a Python-implemented
  ``OnChangeCallback`` interface. Pyjnius can implement Java
  interfaces but not subclass concrete classes, so this thin
  bridge is necessary.
- **Python** (``azt_collabd/android_cp/notify.py``, new):
  jnius wrapper exporting ``notify_project_changed(langcode)``
  and ``notify_global_changed()``. No-ops off Android (loopback
  daemon has no ContentProvider; peers fall back to polling).
  Lazy-loads + caches the provider class on first call.
- **Call sites**:
  - ``repo._commit_step_locked`` after ``COMMITTED_LOCAL``
  - ``lan_listener._reset_working_tree_after_receive`` after
    successful hard-reset
  - ``lan_push._push_to_peer`` after successful peer
    observation update (lan_unshared / at_risk drop for the
    pushing device)
  - ``server._h_lan_set_toggle`` (global URI)
  - ``server._h_set_work_offline`` (global URI)

### Client side

- **Python** (``azt_collab_client/notify.py``, new):
  ``subscribe_project_changes(langcode, callback) → token``,
  ``subscribe_global_changes(callback) → token``,
  ``unsubscribe(token)``. Module state holds strong-refs to
  the pyjnius proxy AND the Java observer so they survive
  Python GC between events. Returns ``None`` for the token
  off Android — peers should treat that as "fall back to
  polling" rather than an error.
- **Re-exported** from ``azt_collab_client/__init__.py``.
- **CLIENT_INTEGRATION.md § 17b** rewrites the "Why peer-side
  polling" section into "Push notifications — ContentObserver
  subscription (v0.47.0+)" with the API + recommended polling
  cadence (subscribe + 60-120 s heartbeat instead of 5-15 s
  poll-only).

### Wire format

No JSON wire format change. The notification API is an
additional surface on the existing ContentProvider authority,
gated by the same suite-signature permission. ``MIN_*_VERSION``
unchanged from 0.47.0.

### Build impact

**Java changes — requires a clean Android rebuild**
(``buildozer android clean && buildozer android debug``) for the
server APK, per ``feedback_buildozer_clean_only_for_native``.
Pure-Python peer apps don't need to clean; incremental build
picks up the client-side ``notify.py`` and the re-exports.

### Files

- ``android/src/main/java/org/atoznback/aztcollab/AZTCollabProvider.java``
  — adds ``notifyStatusChanged`` static method + ``sContext``
  capture in ``onCreate``.
- ``android/src/main/java/org/atoznback/aztcollab/AZTStatusObserver.java``
  — new ContentObserver subclass with Python-implementable
  ``OnChangeCallback`` interface.
- ``azt_collabd/android_cp/notify.py`` — new daemon-side jnius
  wrapper.
- ``azt_collabd/repo.py`` — fire notify on COMMITTED_LOCAL.
- ``azt_collabd/lan_listener.py`` — fire notify on successful
  post-receive reset.
- ``azt_collabd/lan_push.py`` — fire notify on verified peer
  push (peer_main_shas update).
- ``azt_collabd/server.py`` — fire global notify on toggle flips
  (work_offline + lan_allow_sync).
- ``azt_collab_client/notify.py`` — new client-side subscription
  API.
- ``azt_collab_client/__init__.py`` — re-export, version → 0.47.1.
- ``azt_collab_client/CLIENT_INTEGRATION.md`` — rewrite § 17b's
  "Why peer-side polling" sub-section into the push-notification
  contract.

## 0.47.0 — Split sync-status into independent WAN / LAN / at_risk counts

### Wire-format break

`ProjectStatus.commits_ahead` and `ProjectStatus.unshared_commits`
are gone, replaced by three independent count fields:

- ``wan_unshared`` — commits on local HEAD not on
  ``refs/remotes/origin/{main,master}`` (was ``commits_ahead``;
  same computation, renamed). Special-case for LAN-only projects
  (no origin URL): walks from HEAD, surfacing the whole history
  as intentional friction for "no github backup."
- ``lan_unshared`` — commits on local HEAD not reachable from
  any paired-and-sharing peer's ``last_seen_main`` for this
  langcode. Returns 0 when no peers are paired (the "nothing to
  be behind on" convention).
- ``at_risk`` — commits reachable from HEAD from neither origin
  tracking refs NOR any paired peer. Set-intersection of
  ``wan_unshared`` and ``lan_unshared`` as commit sets. Zero
  in every state except state E ("both behind on the same
  commits"), which is the routine transient right after a
  fresh commit.

`MIN_CLIENT_VERSION` and `MIN_SERVER_VERSION` both bump to
``0.47.0``. Old peers paired with a new daemon (or vice versa)
fail compatibility check with a clear error and route to the
self-update flow — no silent mis-render. Clean rename, no
deprecation aliases.

### Rendering recipe (§ 17b)

Pre-0.47 the recipe had three label branches:

```
OK / LANOK +N / +unshared/ahead
```

Post-0.47 there are five:

```
OK
LAN-{lan}                    (only LAN behind)
WAN-{wan}                    (only WAN behind)
WAN-{wan}_LAN-{lan}          (split-brain — different commits each
                              channel, no overlap; rare/anomalous)
WAN-{wan} LAN-{lan}          (both behind on the same commits;
                              routine transient after a commit)
```

Per-channel red rule: ``WAN-{wan}`` is red iff
``work_offline=off``; ``LAN-{lan}`` is red iff
``lan_allow_sync=on``; the uncommitted-changes badge (literal
text ``+n``, drawn in red) is always red. (Earlier design notes
used the shorthand ``R(+n)`` to denote the red uncommitted badge
— that's notation, not literal output. Peers render ``+1`` /
``+3`` etc.)
Red semantically means "settings allow this storage, but it
hasn't happened yet" — transient red is normal automation,
persistent red signals a broken sync. Black means "settings
preclude this resolution; you accepted it by design" (e.g.,
phone in the forest, offline mode).

Suffix table simplified: only ``· offline`` actually surfaces.
The ``· LAN`` and ``· LAN-only`` suffixes are implied (the user
can see the mode elsewhere in the UI; no need to call it out
alongside every sync status). The ``OK · LAN`` ban from earlier
drafts is preserved as a defensive collapse.

State frequencies in normal workflow are ``A > B/C > E > D``:
state E (both-behind, at-risk) is the routine transient right
after a commit (one commit not on either channel yet); state D
(split-brain) is rare and usually pathological (requires
divergent history). This corrects the pre-0.47 framing that
treated D as "safe-common" and E as "alarming-rare."

### Files

- ``azt_collabd/repo.py`` — ``_count_commits_ahead`` renamed to
  ``_wan_unshared``; new ``_lan_unshared`` and ``_at_risk``
  helpers (both take ``langcode`` for peer lookup); shared
  ``_walk_count_log`` rate-limit cache keyed by ``(working_dir,
  tag)`` so the three callers don't clobber each other's emit
  state.
- ``azt_collabd/server.py`` — ``_h_project_status`` emits the
  three new fields, drops the old two. Computes
  ``lan_unshared`` and ``at_risk`` inline (they need
  ``langcode``); ``repo_status_summary`` still returns the
  4-tuple with ``wan_unshared`` as its fourth element. Old
  ``_unshared_commit_count`` helper removed (replaced by
  ``repo._lan_unshared`` + ``repo._at_risk``).
- ``azt_collabd/__init__.py`` — ``MIN_CLIENT_VERSION`` → 0.47.0.
- ``azt_collabd/lan_push.py`` — diagnostic call updated to
  ``_wan_unshared``; comment references the new helper names.
  ALSO: the ``[lan-push] '<pid>' at <host>:<port> refused /
  unreachable`` log line now classifies which errno triggered
  the failure — ENETUNREACH (errno 101, this device has no
  network), EHOSTUNREACH (errno 113, peer not on this network),
  or ECONNREFUSED (errno 111, peer listener down on a known
  endpoint). Triages field reports without an adb round-trip:
  the log itself says whether the local device or the remote
  device is the culprit. Examples:
    ``refused / unreachable: this device has no network route
    (ENETUNREACH) — check WiFi / airplane mode on THIS device``
    ``refused / unreachable: no route to 192.168.10.110 on
    this network (EHOSTUNREACH) — peer device is likely
    offline or on a different network``
    ``refused / unreachable: 192.168.10.23:34863 refused the
    connection (ECONNREFUSED) — peer daemon / listener is down
    or rebound to a different port``
- ``azt_collabd/peers.py`` — comments reference the new helper
  names and field names.
- ``azt_collabd/scheduler.py`` — comments reference
  ``wan_unshared`` / ``lan_unshared``.
- ``azt_collab_client/__init__.py`` — ``__version__`` →
  ``0.47.0``; ``MIN_SERVER_VERSION`` → ``0.47.0``.
- ``azt_collab_client/projects.py`` — ``ProjectStatus`` drops
  ``commits_ahead`` and ``unshared_commits``; adds
  ``wan_unshared`` / ``lan_unshared`` / ``at_risk``.
- ``azt_collab_client/CLIENT_INTEGRATION.md`` — § 17b rendering
  recipe rewritten; § 17c/§ 20 references updated.
- ``azt_collab_client/CLAUDE.md`` — daemon-owned state listing
  references new field names.
- ``azt_collab_client/docs/rationale/sync.md`` — reference
  update.
- ``azt_collab_client/NOTES_TO_DAEMON.md`` — closes the
  ``LANOK rendering asymmetric`` entry as RESOLVED by this
  release (new symmetric model removes the originator-vs-cloned
  asymmetry).
- ``examples/sister_app.py`` — prints the three new fields.

### Migration

Peer rebuild required. Old peers connected to a 0.47.0 daemon
will fail the ``check_server_compat()`` check and route to the
install/update popup with the "Update the client" branch.
The recipe in § 17b is a five-branch model; peers re-implement
their sync-indicator rendering to match.

### Bundled: post-receive reset retry + commit-time absorb

Field session 2026-05-27 surfaced a data-loss path the new
WAN/LAN/at_risk badges exposed: tablet showed persistent red
``+4`` (n_changes) for ~10 minutes after a phone push merged
into its HEAD, because ``_reset_working_tree_after_receive``
hit ``LockTimeout`` (the tablet's own outgoing
``_merge_then_push`` held ``project_lock`` for >5 s during its
own three-way merge), and the fallback comment "next
commit_project will absorb the mismatch" was wrong in the
worst way: if a ``commit_project`` ever did fire, ``_stage_all``
would have seen the merge files as "deleted from working tree"
and produced a commit that erased them.

Two-part fix:

- **Deferred-reset queue** (``azt_collabd/lan_listener.py``).
  ``_reset_working_tree_after_receive`` now adds the langcode
  to ``_pending_post_receive_resets`` on ``LockTimeout`` and
  removes it on success. The set is persisted to
  ``$AZT_HOME/pending_resets.json`` (atomic write — same shape
  as the 0.46.9 settings fix), so a daemon restart while a
  reset is queued doesn't lose track. The scheduler watcher
  drains the queue every tick (~30 s default) via
  ``drain_pending_resets``; ``reconcile_on_startup`` loads the
  persisted set on daemon boot.

- **Pre-commit absorb** (``azt_collabd/repo.py``). At the top
  of ``_commit_repo_locked``, check
  ``lan_listener.has_pending_reset(langcode)``; if set, run the
  hard-reset-to-HEAD under our already-held ``project_lock``
  and remove the queue entry before ``_commit_step_locked``
  stages. This means even if the scheduler drain hasn't run
  yet, the next user-driven commit absorbs the pending reset
  silently — no silent delete of merge files possible.

### Bundled-files

- ``azt_collabd/lan_listener.py`` — adds the
  ``_pending_post_receive_resets`` set + persistence +
  ``drain_pending_resets`` + ``has_pending_reset`` /
  ``load_pending_resets_from_disk`` public API.
  ``_reset_working_tree_after_receive`` enqueues on
  ``LockTimeout`` and dequeues on success.
- ``azt_collabd/scheduler.py`` — ``_watcher_loop`` calls
  ``lan_listener.drain_pending_resets()`` each tick;
  ``reconcile_on_startup`` calls
  ``lan_listener.load_pending_resets_from_disk()``.
- ``azt_collabd/repo.py`` — ``_commit_repo_locked`` checks
  ``has_pending_reset`` and absorbs the reset under the held
  lock before staging.

## 0.46.9 — Atomic config.json + LAN-listener self-heal + LAN-only drain bail

### Diagnosis

Field session 2026-05-26 surfaced four overlapping issues after a
server APK update:

1. **LAN sync silently turned off after APK update.** Persisted
   ``lan.allow_sync`` flipped from True to False across the update,
   visible as the toggle rendering "no" in the daemon UI. Root cause:
   ``settings._save_raw`` wrote config.json in place (open ``'w'`` +
   ``json.dump``), so a process kill mid-write (APK update, OOM)
   could leave a truncated file. Next boot's ``_load_raw`` caught
   ``json.JSONDecodeError``, logged once, and returned ``{}`` — every
   setting silently reverted to its default. Any subsequent ``set_()``
   call then wrote a fresh dict containing only the one key being
   set, wiping every *other* persisted setting on disk. One
   APK-update mishap = lan.allow_sync off, sync.work_offline off,
   device_name reset, etc., with one log line buried among thousands.

2. **Tablet drain loop hammered ``NotGitRepository`` every 2s.**
   LAN-cloned project had ``.git/config`` ``[remote "origin"]`` with
   ``url = `` (empty) — leftover from older dulwich's failed
   ``strip_lan_origin_if_present``. ``_push_repo_locked`` decoded
   the empty URL, didn't KeyError, and fed ``''`` to
   ``_push_step_locked`` which retried fetch/push 4× per drain cycle.
   Same shape as the 0.46.8 ``_count_commits_ahead`` fix; just
   another consumer of the same half-stripped state.

3. **Drain-loop ``[count-ahead]`` diagnostic emitted for the wrong
   branch.** 0.46.7 hardcoded the diagnostic call to
   ``_count_commits_ahead(repo, 'main')``. A project whose HEAD is
   on ``refs/heads/master`` (LAN-cloned from a peer whose source
   git config defaulted to master, or any user-renamed branch)
   showed two ``[count-ahead]`` lines per drain — one against the
   orphan ``refs/heads/main`` (stuck at clone-time SHA, never
   advanced) and one against the real HEAD branch. Confusing
   double-emission; harmless beyond log noise.

4. **``apply_toggle`` startup failures attribution-less.** When
   the LAN listener failed to start on a daemon respawn (FGS
   denied, WifiLock denied, socket bind fail), one log line said
   ``[lan-listener] start failed: <ex>`` with no indication of
   *which* step raised. Hard to attribute in field logs.

### Fixes

- **Atomic ``settings._save_raw``.** Write to ``config.json.tmp.<pid>``,
  ``fsync``, ``os.replace`` — crash-during-write never leaves a
  truncated file. ``_load_raw`` now distinguishes
  "file missing" (returns ``{}`` — clean install, fine) from
  "file exists but unparseable" (returns ``_LoadFailed(exc)``).
  ``set_()`` refuses to write when ``_LoadFailed`` is returned, so
  a corrupt config.json is preserved on disk instead of being
  overwritten with a one-key dict. Loud log line on the unreadable
  case so the failure mode is attributable in field logs.

- **LAN-listener split-brain self-heal.** Scheduler watcher tick
  (every ``sync.connectivity_poll_s``, default 30s) calls
  ``lan_listener.apply_toggle()`` unconditionally. Idempotent when
  state matches; restarts the listener when persisted=True but
  ``is_running()=False``. Repairs the (1) APK-update-killed,
  (2) ``apply_toggle``-raised-during-boot, and
  (3) listener-died-mid-session split-brain cases without a user
  gesture. Pre-this-fix the only way to recover was opening the
  pair flow (which force-re-applies via ``_auto_enable_lan``).

- **``apply_toggle`` per-step error attribution.** Split the
  single ``try/except`` around ``acquire_wifi_locks`` +
  ``start_fgs`` + ``start`` into three. Each phase logs its own
  ``[lan-listener] {step} failed`` line so a field log says
  exactly which seam failed. No behaviour change beyond the log
  line.

- **Empty origin URL = NO_REMOTE.** ``_push_repo_locked`` and
  ``_sync_repo_locked`` now ``.strip()`` the decoded URL and
  short-circuit to ``S.NO_REMOTE`` when the result is empty. Drain
  loop bails immediately instead of fanning out the
  ``NotGitRepository`` storm. User-gestured Sync gets the typed
  NO_REMOTE error so the UI can route to a "publish this project"
  prompt instead of a generic failure.

- **``lan_push.fan_out`` diagnostic uses HEAD branch.** Reads
  ``HEAD`` symref, decodes the branch name, falls back to
  ``'main'`` on detached or unreadable HEAD. The drain-loop
  ``[count-ahead]`` line now matches the ``_h_project_status``
  one for the same project.

### Files

- ``azt_collabd/settings.py`` — atomic ``_save_raw``;
  ``_LoadFailed`` sentinel; ``set_()`` refuses to write on load
  failure; loud log on unreadable config.
- ``azt_collabd/lan_listener.py`` — ``apply_toggle`` splits the
  start sequence into three attributable try/except blocks.
- ``azt_collabd/scheduler.py`` — ``_watcher_loop`` calls
  ``lan_listener.apply_toggle`` every tick (before the drain
  gate) so split-brain self-heals.
- ``azt_collabd/repo.py`` — ``_push_repo_locked`` /
  ``_sync_repo_locked`` short-circuit ``S.NO_REMOTE`` on empty
  URL.
- ``azt_collabd/lan_push.py`` — ``fan_out`` reads HEAD symref
  for the ``_count_commits_ahead`` diagnostic call instead of
  hardcoding ``'main'``.

### Wire format

None. All daemon-internal correctness + diagnostics.

## 0.46.8 — Treat empty origin URL same as no URL in `_count_commits_ahead`

### Diagnosis

0.46.6's ``[count-ahead]`` diagnostic surfaced the
asymmetric-badge cause: tablet's repo had
``[remote "origin"]`` in ``.git/config`` with an **empty
``url`` value** (literal ``url = `` with no value). This is the
fallback state from ``strip_lan_origin_if_present`` when the
older dulwich on the device lacks ``config.remove_section``:

```python
try:
    config.remove_section((b'remote', b'origin'))
except (KeyError, AttributeError):
    try:
        config.set((b'remote', b'origin'), b'url', b'')
    except Exception:
        return False
```

The fallback writes ``url = `` (empty) instead of removing the
section. My 0.46.1 ``_count_commits_ahead`` then read the
config and took the "origin URL configured → 0 (OK-on-
uncertainty)" branch even though the URL is empty (= nowhere
to push) — masking the LAN-only walk and producing
``OK · LAN-only`` (no count) instead of ``LANOK +N``.

Phone happened to not have the half-strip state (its dulwich
honoured ``remove_section`` cleanly), so phone hit the
``no tracking ref + no origin URL (LAN-only) → walk-from-HEAD``
branch and rendered ``LANOK +N`` correctly. Asymmetry sourced
to a per-dulwich-version difference in how the strip
completed.

### Fix

In ``_count_commits_ahead``'s no-tracking-ref branch, treat an
empty / whitespace-only URL the same as no URL at all. After
``.decode().strip()``, if the result is empty, fall through to
the walk-from-HEAD branch.

After this fix on tablet, the same poll that previously
returned 0 will return the actual local-commit count. Recipe
renders ``LANOK +N`` matching phone. Symmetry restored without
needing to repair the half-stripped ``.git/config`` on disk.

The on-disk half-strip state (``[remote "origin"]`` section
with empty ``url = ``) persists — it's cosmetic git-state
clutter, not user-visible, and harmless to ``project_status``
once this consumer-side fix is in. A future pass could repair
it via a non-dulwich path (write ``.git/config`` directly with
the section removed), but no need to land that here.

### Files

- ``azt_collabd/repo.py`` —
  ``_count_commits_ahead``: empty-URL handling in the
  no-tracking-ref branch.

### Wire format

None. Pure daemon-internal correctness fix.

## 0.46.7 — Fire `[count-ahead]` diagnostic from the drain loop too

0.46.6 added the ``[count-ahead]`` line in ``_count_commits_ahead``
but that only fires when ``_h_project_status`` is called — which
only happens when a peer (recorder / picker) is foregrounded
and polling. Field test on a device where the server APK is
the only thing open after a Restart server tap produced 3+
minutes of drain cycles with no diagnostic emit. Picker
wasn't running; nothing called status; nothing fired the
diagnostic.

Fix: ``lan_push.fan_out`` now also calls
``_count_commits_ahead(repo, 'main')`` once per drain
(every ~30 s). The rate-limit on ``_count_ahead_log``
(output-change-only) still applies, so steady-state drains
emit nothing once the state has been seen. The first drain
after a daemon restart, or whenever the value changes,
surfaces the line — regardless of peer activity.

### Files

- ``azt_collabd/lan_push.py`` —
  ``fan_out`` opens repo + calls ``_count_commits_ahead`` for
  diagnostic side-effect; ignores return value.

### Wire format

None. Diagnostic-only.

## 0.46.6 — `[count-ahead]` diagnostic in `_count_commits_ahead`

After 0.46.5 broke the merge loop and both phones converged on
the same HEAD SHA, a residual badge asymmetry showed up: phone
rendered ``LANOK +N`` (commits_ahead=N from walk-from-HEAD)
while tablet rendered ``OK`` (commits_ahead=0). Both phones on
0.46.5, both at the same data — so the asymmetry comes from
each device's local ref state, not different code.

Most likely cause (suspect, not yet confirmed): one phone has
``refs/remotes/origin/main`` from clone time that's somehow at
the converged SHA, the other has no tracking ref. The 0.46.1
walk-from-HEAD branch fires only on the second; the first
returns 0 via the "tracking-ref-equals-local → 0" branch.

Rather than guess and patch blindly, this release adds a single
rate-limited ``[count-ahead]`` diagnostic line per call showing
which branch fired and the SHAs involved:

```
[count-ahead] '<project_dir>': branch='main' local='<sha>':
  no tracking ref + no origin URL (LAN-only) →
  walk-from-HEAD = 55
[count-ahead] '<project_dir>': branch='main' local='<sha>':
  tracking ref equals local → 0
```

Rate-limited by output-change (in-memory dict per working_dir):
the line only emits when the result differs from the last one.
Steady-state polls produce no log noise; transitions are
visible. Will reveal the actual ref state on both phones in
the next field run.

### Files

- ``azt_collabd/repo.py`` —
  ``_count_commits_ahead`` adds branch-tagged
  ``[count-ahead]`` emit via new ``_count_ahead_log`` helper
  (in-memory rate-limit cache).

### Wire format

None. Diagnostic-only.

## 0.46.5 — Re-attach HEAD to refs/heads/main after receive-pack, breaking the merge loop

### Field evidence (right after 0.46.4 deploy)

Two phones with the 0.46.4 FF-check enabled now correctly route
through ``_merge_then_push`` on every drain, but the loop never
terminates:

```
[lan-push] '<peer>': peer at 'e566...' is NOT ancestor of
  local 'a614...' — would be force-overwrite; routing through
  merge instead
[lan-merge] running three-way merge
[merge-trace] resolution done writes=20 deletes=0 conflicts=0
[merge-trace] apply done writes_done=0 deletes=0
[lan-merge] merged → 'c504...' (conflicts=0)
[lan-push] pushed merged → peer
```

Every cycle: ``writes_done=0`` (file content identical) +
brand-new merge commit + push. Next cycle: same shape, new
SHAs. Infinite.

### Why

``writes_done=0`` is the key signal: every merge produces a
commit whose TREE is identical to one of its parents. So the
data is fine; both phones have the same content. The histories
just keep growing parallel chains of empty merge commits.

The decoupling cause:

- ``_merge_diverged`` calls
  ``worktree.commit(merge_heads=[remote_sha])``. This advances
  HEAD's pointer (whatever it is — symref or detached). In some
  flows, HEAD ends up detached at "our last merge SHA."
- Incoming receive-pack on the other side updates **only**
  ``refs/heads/main`` via ``set_if_equals`` — never touches
  HEAD. So the receiver's HEAD (detached at its last merge)
  stays put while main advances.
- Each side: HEAD = own last merge, main = peer's last push.
  ls-remote of peer (prefers HEAD per 0.46.4) returns peer's
  detached HEAD; FF check sees divergence; merge fires;
  pushes; receiver's main advances; receiver's HEAD doesn't.
  Repeat.

### Fix

In ``_reset_working_tree_after_receive``'s clean-working-tree
branch, after the receive-pack lands and BEFORE resetting,
check if HEAD is a symref to ``refs/heads/main``. If not
(detached or symref to a different ref), and main's value is
a descendant of HEAD's current value (so HEAD's content is
fully reachable from main → safe to discard HEAD's pointer),
**re-attach HEAD as a symref to refs/heads/main**.

Implementation:

1. ``repo.refs.get_symrefs()`` to check current HEAD target.
2. If ≠ ``refs/heads/main``, walk main's ancestry looking for
   HEAD's SHA. Safe to re-attach iff found (HEAD's content is
   in main's history).
3. ``repo.refs.set_symbolic_ref(b'HEAD', b'refs/heads/main')``.
4. Reset working tree to (now-realigned) HEAD value.

After this on BOTH sides: HEAD tracks main. Next drain's
ls-remote returns the same SHA on both sides (= main = HEAD).
FF check is satisfied; no merge fires; no new commits.
Convergence.

### Why this is safe

The ancestry check is the load-bearing safety:

- **HEAD's value is an ancestor of main**: HEAD's commit is in
  main's history. Re-attaching HEAD to main moves HEAD's
  effective value to main's tip; HEAD's old value is still
  reachable via main's ancestry. No data loss. ✓
- **HEAD's value is NOT an ancestor of main** (e.g., genuinely
  diverged committed states): re-attaching would lose HEAD's
  content. The check refuses; HEAD stays decoupled. The next
  ``_merge_then_push`` from the local side will fold HEAD's
  content into a merge commit on top of main, after which
  HEAD's content IS reachable from main and the next
  re-attach can fire. Two-cycle convergence; still terminates.
- **HEAD == main**: no-op (both pre-check and re-attach are
  no-ops in this case).

### Files

- ``azt_collabd/lan_listener.py`` —
  ``_reset_working_tree_after_receive`` adds the HEAD re-attach
  pass before ``porcelain.reset``.

### Wire format

None. Pure daemon-internal correctness fix. The CHANGELOG of
0.46.x is a fairly long string of these — each closes a
specific axis of the field-observed convergence brittleness;
together they reach honest LAN sync.

## 0.46.4 — Client-side fast-forward check + prefer HEAD in LAN ls-remote

### Why

0.46.3 made ``unshared_commits`` honest by tracking per-peer
observed main. But field logs from the recorder team showed
something more fundamental was broken: two phones at LANOK+9
and LANOK+10, both reporting no-ops with each phone seeing
peer's ``refs/heads/main`` equal to its own HEAD — even though
the phones' actual HEADs differed.

Trace:

- dulwich's smart-protocol receive-pack uses
  ``RefsContainer.set_if_equals(ref, expected_old, new)`` for
  every ref update. That's a **stale-write guard** ("update
  only if current matches what I last saw"), NOT a fast-
  forward check ("update only if new descends from current").
- ``porcelain.push`` sends ``expected_old`` = peer's
  ``refs/heads/main`` from its own ls-remote, ``new`` = our
  HEAD. dulwich's receive-pack accepts ANY value as ``new``
  as long as ``expected_old`` matches — including non-FF.
- Result: every push silently force-overwrites the receiver's
  ``refs/heads/main``. The receiver's own commits stay in the
  object store but the ref no longer points at them; HEAD
  (still a symref or detached at the receiver's actual latest)
  decouples from main.
- Both phones end up with: ``HEAD`` = own real latest,
  ``refs/heads/main`` = peer's last-pushed value.
- ls-remote (which currently prefers ``refs/heads/main``)
  returns the clobbered value. Pre-flight no-op condition
  ``local_head == pre_peer_head`` fires because our HEAD ==
  what we last force-pushed (= peer's main now). No merge
  triggers. Histories stay diverged forever.
- Pre-0.46.3 ``last_lan_pushed_sha`` was also set to our HEAD
  on every push success, so ``unshared = walk(HEAD, exclude=
  HEAD) = 0`` → false-positive LANOK. 0.46.3 made unshared
  per-peer-observed but the underlying observation
  (peer's main) was the WRONG ref to read.

### Fix

Two halves of a client-side correction, both small:

1. **``_peek_peer_main`` and ``_merge_then_push``'s fetch-
   result read prefer ``HEAD`` over ``refs/heads/main``.**
   A peer whose main has been force-clobbered still has the
   real latest at HEAD (detached). Reading HEAD gives us the
   peer's actual current state. For repos where HEAD is a
   normal symref to main, both refs yield the same SHA;
   behaviour unchanged.

2. **Pre-flight fast-forward check in ``_push_to_peer``.** New
   helper ``_peer_is_ancestor_of_local`` walks our HEAD's
   ancestry looking for the peer's HEAD SHA (cap 10k commits
   — safety net for pathological histories, but typical AZT
   field projects are well under 1k). If the peer's current
   HEAD isn't an ancestor of our local HEAD, that's a
   divergence — route directly to ``_merge_then_push`` instead
   of letting ``porcelain.push`` do the silent force-overwrite.

   This is the pre-flight complement to the 0.45.46 post-
   flight verify: post-flight detected silent NAKs (HTTP
   success, protocol rejection); pre-flight here prevents
   the silent overwrite case (protocol success that we
   shouldn't have asked for).

### Convergence for already-stuck devices

Existing diverged-HEAD-decoupled state on the user's two
phones unwinds over a couple of drain cycles:

1. Phone fanout fires. ls-remote tablet → tablet's HEAD
   (peer's real latest, NOT the force-clobbered main). Differs
   from phone's HEAD.
2. FF check: tablet's HEAD not in phone's ancestry. → merge.
3. ``_merge_then_push`` fetches, three-way merges (lift-aware),
   creates merge commit M with both HEADs as parents. Push M
   to tablet — FF from tablet's main (one of M's parents).
   Tablet's main advances to M.
4. Tablet fanout. ls-remote phone → phone's HEAD = M (now).
   FF check: M descends from tablet's HEAD (tablet's HEAD was
   the other parent). Walking tablet's HEAD ancestry finds
   tablet's own commit but not M (M is a descendant). →
   merge. Tablet's ``_merge_diverged`` produces M' which has
   tablet's HEAD as parent. Push M' to phone — FF.
5. Each subsequent drain cycle generates one degenerate merge
   commit until the per-peer observed-main SHAs (0.46.3)
   stabilize on the same value. Unshared drops to 0 on both
   sides; LANOK becomes honest.

The decoupled-HEAD-vs-main state on each phone persists (HEAD
keeps advancing on local commits; main lags behind by what the
peer last pushed). That's an internal git-state oddity, not
user-visible — the push refspec ``HEAD:refs/heads/main``
carries our HEAD's content regardless, and the convergence
above pushes the merge commits all the way through.

### Files

- ``azt_collabd/lan_push.py`` —
  - ``_peek_peer_main``: prefer ``HEAD`` over
    ``refs/heads/main``.
  - ``_merge_then_push_locked``: same preference for
    ``fetch_result.refs``.
  - ``_peer_is_ancestor_of_local``: new helper.
  - ``_push_to_peer``: pre-flight FF check; on non-ancestor,
    routes to ``_merge_then_push``.

### Wire format

None. Pure daemon-internal correctness fix. Existing peer
daemons still accept the force-overwrite (we can't fix that
side without coordinated rollout), but the pushing side now
declines to issue one — so any pair where at least ONE phone
has 0.46.4 stops force-pushing in that direction. Both phones
on 0.46.4 means neither side force-pushes.

## 0.46.3 — `unshared_commits` keyed on per-peer observed main, not what we pushed

### Why

Field report: phone at ``LANOK +10`` next to tablet at
``LANOK +9``, only talking to each other, both offline=yes.
Both can't be honest LANOK simultaneously — if every commit
on each device existed on the other, the histories couldn't
differ. Recorder peer team's diagnosis (correct): the daemon
is computing ``unshared_commits`` from "have I successfully
pushed recently?" rather than "is this exact commit on the
peer?" — false-positive LANOK on diverged histories.

### Bug

``_unshared_commit_count`` excluded ``last_lan_pushed_sha``
from the HEAD walk — a project-wide single SHA set to OUR
HEAD on every successful push or no-op confirmation. After
non-FF rejection / asymmetric merge / convergence failure,
both phones end up at their own HEAD with
``last_lan_pushed_sha == own_head``, so
``walk(own_head, exclude=own_head) == 0`` on both → false-
positive LANOK on diverged histories. Symptom: each phone
told the user "your data is safe on LAN" while they were
actually carrying commits the other didn't have.

### Fix

Replace project-wide ``last_lan_pushed_sha`` with **per-peer
``last_seen_main``** in ``peers.json`` — keyed by
``(peer_id, langcode)`` — recorded from actual observations:

- Every successful ``_peek_peer_main`` (ls-remote) updates the
  observed peer's main for the langcode.
- Post-flight verify after ``porcelain.push`` (0.45.46) updates
  the observed peer's main to our local HEAD when the peer is
  confirmed to be at it.
- No-op short-circuit (peer already at our HEAD per pre-flight)
  also updates.

``_unshared_commit_count`` now excludes the UNION of every
paired peer's observed-main SHA for this langcode (via new
``peers.peer_main_shas_for(langcode)``), alongside the existing
``refs/remotes/origin/main``. ``unshared == 0`` only when every
commit reachable from HEAD is also reachable from at least one
observed peer's main — i.e. **provably** somewhere besides this
phone, not just "we tried to push something recently."

### Diverged-histories case after this fix

Phone A at ``X``, phone B at ``Y``, ``X != Y`` (neither
descends from the other):
- A's ``last_seen_main[B] = Y`` (observed via ls-remote).
- A's walk: ``include=[X], exclude=[Y]``. Y doesn't share
  ancestry with X past their merge-base; walk yields the
  commits unique to X. ``unshared > 0``. Recipe renders
  ``+unshared/+ahead`` (data-loss-risk indicator), NOT LANOK.
- Symmetric on B: ``unshared > 0``, also no LANOK.

Both phones now correctly flag the divergence instead of
both falsely showing LANOK.

### Convergence case after this fix

Both phones at SHA ``M`` (after a successful merge round-trip):
- A's ``last_seen_main[B] = M`` (observed). A's walk:
  ``include=[M], exclude=[M]`` → 0. LANOK.
- Same on B. Both correctly LANOK with identical
  ``commits_ahead`` counts.

### Back-compat

``last_lan_pushed_sha`` (project-wide field in ``projects.json``)
is kept as a diagnostic record — ``lan_push`` continues to
update it on success — but it's no longer used to compute
``unshared_commits``. Pre-0.46.3 clients reading
``ProjectStatus.lan_pushed_sha`` still see a value (last SHA we
delivered to any peer), it just doesn't drive the indicator
anymore.

Existing ``peers.json`` entries without ``last_seen_main`` are
normalized to empty dict on read — pre-0.46.3 peers shed no
observed-main data; the next fan-out / no-op / ls-remote
populates it. Cold-start daemons render conservatively
(``unshared > 0`` until first peer observation lands) which is
honest, not a regression.

### Files

- ``azt_collabd/peers.py`` —
  - ``_normalize_entry``: ``last_seen_main`` (dict[langcode →
    sha]) added.
  - ``set_peer_last_seen_main`` / ``peer_main_shas_for``: new
    setter / aggregator.
- ``azt_collabd/lan_push.py`` —
  ``_push_to_peer``: records peer's main from every
  ``_peek_peer_main``, no-op branch, and post-flight-verified
  push.
- ``azt_collabd/server.py`` —
  ``_unshared_commit_count``: union of paired peers'
  observed-main SHAs replaces ``last_lan_pushed_sha`` in the
  exclude set.

### Wire format

No new wire fields. ``ProjectStatus.lan_pushed_sha`` keeps the
project-wide diagnostic field; ``unshared_commits`` value
changes meaning slightly (more honest). Pre-0.46.3 peers
reading ``unshared_commits`` get more accurate values; no
behavior change required on the peer side.

## 0.46.2 — Strip tracking refs alongside the LAN origin URL

### Why

0.45.37's ``strip_lan_origin_if_present`` removes the
``[remote "origin"]`` section from ``.git/config`` for LAN-
cloned projects (the URL is ephemeral — B's listener port
changes per restart — so persisting it would also wrongly
hide the Publish row). But it left ``refs/remotes/origin/*``
in place, unreferenced and unmaintained. The recorder peer
team's 2026-05-26 NOTES update flagged the user-visible
fallout: phone A (LAN-cloned, orphan tracking ref at
clone-time SHA) and phone B (originator, no tracking ref)
render different ``commits_ahead`` for the same project
state, producing asymmetric LANOK badges.

### Fix

``_strip_lan_origin_locked`` now does both strips in one
pass: ``config.remove_section((b'remote', b'origin'))`` AND
``del repo.refs[b'refs/remotes/origin/...']`` for every
matching tracking ref. The pre-check
(``strip_lan_origin_if_present``) recognizes the two
conditions for taking the lock:

1. Paired-peer URL present in config (existing trigger), or
2. Orphan ``refs/remotes/origin/*`` ref present with NO URL
   (new trigger — cleans up projects already stripped by
   prior daemon versions that left the refs behind).

On the first ``_h_project_status`` poll after 0.46.2 lands,
existing LAN-cloned projects shed their orphan refs. After
that, both phones (A and B) see the same "no origin remote
at all" state, the 0.46.1 walk-from-HEAD branch fires
uniformly, and ``commits_ahead`` is symmetric.

### Files

- ``azt_collabd/repo.py`` —
  - ``_strip_lan_origin_locked``: extended to strip
    ``refs/remotes/origin/*`` alongside the config section;
    orphan-only sweep (no URL but refs present) now runs too.
  - ``strip_lan_origin_if_present``: pre-check recognizes
    orphan tracking refs as a reason to take the lock.

### Wire format

None. Pure daemon-internal correctness fix.

## 0.46.1 — `commits_ahead` counts from HEAD when no origin remote

Per NOTES_TO_DAEMON 2026-05-26 (recorder peer team): a project
LAN-cloned via ``lan_clone`` (no github remote;
``remote_url=''``) with all commits LAN-delivered to a paired
peer was never rendering LANOK. The § 17b recipe gates LANOK
on ``commits_ahead > 0``, but ``_count_commits_ahead`` returns
0 when no ``refs/remotes/origin/<branch>`` exists — so a
LAN-only project always showed ``OK`` (or ``+u/0``) instead
of the documented ``LANOK +N``.

§ 20 hard rule 4 explicitly promises LANOK works for LAN-only
projects; § 17b's recipe didn't. This release reconciles them
on the daemon side, with no peer change required.

### Fix

``_count_commits_ahead`` distinguishes two cases when the
tracking ref is absent:

- **Origin configured, never pushed** (``remote.origin.url``
  exists in ``.git/config``): return 0. Pre-0.46.1 behaviour
  — "OK on uncertainty" avoids double-counting the unpushed
  initial commit as "behind."
- **No origin remote at all** (``KeyError`` reading
  ``remote.origin.url``): walk from HEAD and count. Every
  local commit IS unpublished by definition — there's
  nowhere to push.

For LAN-only projects whose commits have reached a peer,
``commits_ahead`` now equals N (total local commits),
``unshared_commits`` equals 0 (peer has the SHA), and the
existing recipe renders ``LANOK +N`` correctly. No peer
update needed — the daemon's response just becomes truthful
for the LAN-only case.

### Cost

The "no origin" branch walks the full commit history from
HEAD, which is O(n_commits). For AZT field projects this is
typically a few hundred commits — negligible. The walk only
fires for projects with no github remote at all; published
projects keep the bounded "commits since last push" walk.

### Files

- ``azt_collabd/repo.py`` —
  ``_count_commits_ahead`` adds the no-origin branch.

### Wire format

None — same ``commits_ahead`` key, more truthful value when
no remote is configured. Pre-0.46.1 peers reading this field
get the same accuracy improvement; the existing § 17b recipe
renders correctly without modification.

## 0.46.0 — LAN sync correctness milestone

Closing the iteration that ran from 0.45.39 through 0.45.48.
Marks the suite reaching a coherent state for peer-to-peer
LAN sync after a sustained run of field-driven fixes:

- **Data paths under proper locks** (0.45.39): LAN merge path,
  ``.git/config`` writes, `strip_lan_origin` scoping, smart
  post-receive guard.
- **Security gate on auto-share** (0.45.39): QR-display
  binding so auto-share can't fire without the user's
  consent gesture.
- **Auto-init on project create + NOT_A_REPO recovery**
  (0.45.42): every project has a usable ``.git/`` from day
  one; existing broken projects recover on the next commit.
- **Picker emits the cloned project immediately** (0.45.43):
  no more "exit and re-enter the picker" after accepting a
  share offer.
- **Post-receive: merge HEAD into working tree** (0.45.44):
  three-way merge instead of overwriting unstaged edits when
  an incoming push lands while the user is editing.
- **``ProjectStatus.head_sha`` + content-refresh contract**
  (0.45.45): peers get a uniform HEAD-advance signal in
  ``project_status``; ``CLIENT_INTEGRATION.md`` § 17b
  generalized from "Badge refresh obligation" to "Background
  refresh obligation" covering both badge and content. Fonts
  fix from NOTES (Candidate #1 source-tree-relative).
- **Detect silent non-FF rejection** (0.45.46): post-flight
  ls-remote after ``porcelain.push`` so divergent histories
  trigger ``_merge_then_push`` instead of staying diverged
  forever.
- **Pre-commit pending edits before LAN merge** (0.45.47):
  ``_merge_diverged`` no longer overwrites unstaged
  working-tree edits — they go into a real commit first as
  one of the merge's parents.
- **Stash + reapply fallback** (0.45.48): when the
  pre-commit silently no-ops (porcelain.add edge cases the
  user observed as "red +N hanging around"), held snapshot
  is lift-aware-reapplied after ``_merge_diverged``;
  second commit attempt lands it on top of the merge.
  Worst case: edits stay uncommitted in working tree, never
  lost.

The behavior matrix that's now closed:

| Receiver state | Diverged committed history? | Path |
|---|---|---|
| Clean WT | No (FF) | ``reset --hard HEAD`` |
| Clean WT | Yes | ``_merge_then_push`` |
| Unstaged edits | No (FF) | ``integrate_head_into_working_tree`` (merge into WT) |
| Unstaged edits | Yes | ``_merge_then_push`` (auto-commit + stash + reapply) |

No new wire format in 0.46.0 beyond what 0.45.45 added
(``head_sha``). Peers built against 0.45.40+ keep working;
peers built against 0.46.0+ pick up the content-refresh wiring
in § 17b.

## 0.45.48 — Stash + reapply fallback for failed pre-commit before LAN merge

### Why

0.45.47's pre-commit-before-merge assumes ``_commit_step_locked``
will reliably capture the user's working-tree edits into a real
commit. The user flagged that this isn't always true:

> "I see red numbers hanging around after swipes sometimes. If
> there's a write that wasn't asked to commit, what you say is
> fine: just commit it now. But what about if there's a problem
> committing now, for whatever reason?"

Field-observed edge case: ``porcelain.add`` silently no-ops in
some condition we haven't fully traced (file-permission race,
dulwich internal state, …). ``_commit_step_locked`` then sees
``has_staged = False`` and returns ``NOTHING_TO_COMMIT`` even
though working tree has unstaged_mods. The 0.45.47 pre-commit
treats that as "clean working tree, drop snapshot" — and then
``_merge_diverged`` overwrites the (still-uncommitted) edits.

### Fix — stash + reapply

Three-step protection in ``_merge_then_push_locked``:

1. **Snapshot first.** ``repo.snapshot_unstaged_paths(repo,
   project_dir)`` reads working-tree bytes for every
   unstaged-mod path (except daemon-internal scratch dirs)
   into an in-memory ``dict[path_bytes, bytes]`` BEFORE the
   pre-commit runs. Capture the pre-merge HEAD SHA at the
   same point — needed as the merge base for the reapply.

2. **Try pre-commit.** If it returns ``COMMITTED_LOCAL``, the
   edits are now in a real commit (one of the upcoming merge's
   parents); drop the snapshot. If ``NOTHING_TO_COMMIT``, the
   working tree was actually clean; drop the snapshot. If
   anything else (``COMMIT_FAILED``, ``COMMIT_REPEATEDLY_FAILED``,
   raised exception), **keep** the snapshot — these are the
   cases where the user's red ``+N`` lingers despite a commit
   attempt.

3. **Reapply after merge.**
   ``repo.reapply_snapshot_after_merge(repo, project_dir,
   snapshot, pre_merge_head_sha)`` walks the held snapshot:

   - For ``.lift`` paths: three-way merge via
     ``lift_merge.three_way_merge``. base = the snapshot's
     pre-merge HEAD blob for that path, ours = the snapshot
     bytes (user's unstaged edits), theirs = the working-tree
     content the merge just wrote. Writes merged result back.
     If ``lift_merge`` raises, restore the snapshot bytes
     verbatim (last-resort: keep ours).
   - For non-LIFT paths: overwrite the working-tree file with
     the snapshot bytes. Same "keep ours on conflict" policy
     ``_merge_diverged`` itself uses for non-LIFT modify/modify.

   Then a second ``_commit_step_locked`` pass attempts to
   commit the reapplied content on top of the merge commit so
   the resulting push carries the user's edits. If THAT commit
   also fails to capture (the same root cause that tripped
   step 2), the snapshot is at least on disk in working_tree;
   the next drain's ``commit_project`` retries, and the user's
   edits never leave the device — they just stay uncommitted a
   little longer.

### Worst case

If both commit attempts (pre-merge and post-reapply) silently
no-op for whatever environmental reason, the user's edits are
still in working_tree. They aren't lost — just stuck in
unstaged limbo until the underlying commit issue clears. The
merge commit pushed to the peer reflects committed state
only; LAN convergence happens for the committed parts. The
user's pending edits commit on the next successful drain
cycle and propagate then.

This is strictly better than pre-0.45.48 (which would have
silently overwritten the unstaged edits with the committed
merge result — true data loss). Worst-case 0.45.48 is "edits
stay uncommitted longer"; not "edits disappear."

### Files

- ``azt_collabd/repo.py`` —
  - ``snapshot_unstaged_paths(repo, project_dir)`` (new):
    in-memory snapshot of unstaged tracked paths.
  - ``reapply_snapshot_after_merge(repo, project_dir, snapshot,
    base_sha)`` (new): lift-aware reapply (lift_merge for
    ``.lift``, overwrite for binary).
- ``azt_collabd/lan_push.py`` —
  ``_merge_then_push_locked``: snapshot before pre-commit;
  drop on commit-success/clean; reapply + second-commit after
  ``_merge_diverged`` when snapshot held.

### Wire format

None. Pure daemon-internal correctness fix.

## 0.45.47 — Pre-commit pending edits before LAN merge runs

### Why

0.45.46 made ``_push_to_peer`` fall through to ``_merge_then_push``
on silent non-FF rejection. ``_merge_then_push`` calls
``_merge_diverged``, which walks the three committed trees
(``base``, ``head_commit.tree``, ``remote_commit.tree``) and
writes the merged result directly to the working tree —
**bypassing any uncommitted user edits in the working tree.**
``_stage_all`` then runs and the merge commit captures the
overwritten state. Net effect on a peer with a non-empty red
``+N``: in-flight edits clobbered.

User flagged this as a third axis after the 0.45.46 ship:
"I see red +N from time to time, and I don't want us to have to
wait for that to clear, nor to overwrite instead of waiting."

### Fix

Under ``project_lock`` (already held by
``_merge_then_push_locked``), before the fetch and merge run
``_commit_step_locked`` to bundle any pending working-tree
edits into a real commit. The user's edits become one of the
merge's parents — preserved, not overwritten. ``local_head``
is re-read after the pre-commit so the merge uses the fresh
SHA.

When the working tree is clean, ``_commit_step_locked``
returns ``NOTHING_TO_COMMIT`` and the merge proceeds
unchanged — single ``porcelain.status`` walk overhead. When
the pre-commit raises (rare; partial-disk-write etc.), the
merge falls through to the committed-state-only path with a
log line for the field; that's the pre-0.45.47 behaviour, no
worse than before.

### The three axes, fully covered

| Receiver state | Diverged committed history? | Path | Where |
|---|---|---|---|
| Clean working tree | No (FF) | reset --hard to HEAD | lan_listener |
| Clean working tree | Yes | _merge_then_push (auto-commit no-op + merge) | lan_push |
| Unstaged edits | No (FF) | integrate_head_into_working_tree (merge into WT) | lan_listener (0.45.44) |
| Unstaged edits | Yes | _merge_then_push (auto-commit pending + merge) | lan_push (this release) |

The push-flight detection (0.45.46 post-flight ls-remote) is
what bridges "yes" rows on the push side; the receive-flight
detection (0.45.44 status walk in ``_reset_working_tree_after_receive``)
is what bridges them on the receive side.

### Files

- ``azt_collabd/lan_push.py`` —
  ``_merge_then_push_locked``: ``_commit_step_locked`` runs
  before fetch + merge to flush pending edits.

### Wire format

None. Pure daemon-internal correctness fix.

## 0.45.46 — `lan_push`: detect silent non-FF rejection, fall through to merge

### Field repro (2026-05-26)

Two phones, same project, concurrent record-then-commit. Both
phones logged ``[lan-push] advanced <peer> main: A → B`` lines
suggesting successful sync. SHAs in the logs revealed reality:

- Phone's HEAD stayed at ``d22cd047b5c5`` across two drain
  cycles and a "post-receive reset → HEAD (d22cd047b5c5)" line
  that fired AFTER the tablet's push completed — i.e. the push
  didn't actually advance phone's HEAD.
- Tablet's HEAD stayed at ``0e79d3cdd912`` across the same
  window for the symmetric reason — phone's push didn't
  advance tablet's HEAD either.
- End state: phone at ``c942c34c4899``, tablet at
  ``0e79d3cdd912``. Diverged. Both phones' recordings only
  exist on their own device.

### Cause

dulwich's smart-protocol receive-pack handler rejects a
non-fast-forward ref update by NAKing in the protocol body —
HTTP 200, no exception, just a ``ng <refname> non-fast-forward``
line embedded in the response. ``porcelain.push`` returns
normally; our code in ``lan_push._push_to_peer`` then logs
``[lan-push] advanced ...`` based purely on the absence of an
exception. We never look at the response body and never check
whether the peer actually moved.

The existing ``DivergedBranches``-handling path in
``_push_to_peer`` (which falls through to ``_merge_then_push``,
the lift-aware three-way fetch + merge + push) is the right
recovery for this scenario, but only triggers if dulwich raises
that specific exception — which it doesn't for body-NAKs.

### Fix

After ``porcelain.push`` returns without exception, re-ls-remote
the peer and compare ``refs/heads/main`` to our local HEAD. If
they differ, the push was silently rejected — fall through to
``_merge_then_push``. Costs one extra ls-remote round-trip per
successful push attempt (negligible compared to the receive-
pack work that just ran).

### Convergence after the fix (sequential record test)

1. Tablet records → commits → fans out to phone.
2. Tablet's push body-NAKs because phone has its own divergent
   commit. Post-flight ls-remote sees phone still at its prior
   SHA. Tablet falls through to ``_merge_then_push``.
3. Tablet fetches phone's commit, runs ``_merge_diverged``
   (lift-aware three-way), creates a merge commit on tablet
   with both peers' HEADs as parents, pushes the merge to
   phone. By construction this push is FF on phone (peer's
   HEAD is one of the parents). Phone's main → tablet's
   merge.
4. Phone records → commits on top of the merge → fans out to
   tablet. FF; tablet's main → phone's new commit. Both
   peers converged.

Worst case for true-simultaneous merges (both phones merge in
the same drain): both pushes succeed-and-NAK each other once
more; second drain cycle re-merges; convergence in ~2 drain
cycles instead of one. Self-correcting.

### What this means for 0.45.44

The 0.45.44 ``[post-receive-merge]`` path was the correct fix
for "incoming push lands while I have an unstaged working-tree
edit" — a different axis. The current bug is "incoming push
gets silently NAKed because we already have our own commit."
Both paths now exist; together they cover the matrix of
contended-edit scenarios.

### Files

- ``azt_collabd/lan_push.py`` —
  ``_push_to_peer``: post-flight ls-remote after
  ``porcelain.push``; on SHA-mismatch fall through to
  ``_merge_then_push``.

### Wire format

None. Pure daemon-internal correctness fix.

## 0.45.45 — `ProjectStatus.head_sha` + peer content-refresh obligation

### Why

After 0.45.44 made the working tree actually reflect incoming
LAN content (via merge-on-receive instead of deferred-reset),
peers still have no documented way to detect that HEAD moved.
``project_status.last_commit`` only bumps on *local* commits;
``commits_ahead`` is github-relative; ``unshared_commits`` is
peer-relative — none change reliably on a receive-pack from
a paired peer. The peer's recorder kept rendering the pre-
receive LIFT until the user manually re-entered the project.

### Daemon-side

``ProjectStatus.head_sha`` (new field; additive) — SHA hex of
``refs/HEAD``. Bumps on every HEAD advance: local commit,
incoming LAN receive-pack, post-receive merge. Empty string for
projects with no commits yet (pre-init / pre-first-commit).

``_h_project_status`` reads HEAD once per poll (in-memory ref
lookup; no full status walk). Cost is negligible — same
poll cadence works.

### Client-side

``azt_collab_client.projects.ProjectStatus`` decodes the new
field with empty-string default for forward-compat with
pre-0.45.45 daemons.

### Contract update

``CLIENT_INTEGRATION.md § 17b`` generalized from
"Badge refresh obligation" to "Background refresh obligation."
Now mandates:

- Peers MUST poll ``project_status`` on a 5–15 s background
  tick (unchanged).
- Peers MUST track the last-seen ``head_sha`` and, when it
  changes between polls, call ``_refresh_in_place`` (from § 14)
  to re-read the LIFT and re-render the current view.
- Empty ``head_sha`` (legacy daemon or pre-first-commit project)
  disables the content-reload branch without breaking badge
  correctness.

Recipe and rationale in § 17b. § 14's "external mutation"
catch-all stays as the generic principle; this is the explicit
LAN-driven instance.

### Field flow this closes

Phone A records → commits → fans out to phone B → phone B's
post-receive merges (0.45.44) → phone B's ``head_sha`` advances
→ phone B's next background poll detects the change → recorder
reloads in place → user sees phone A's entry without re-entering
the project or tapping Sync. Round-trip latency is bounded by
phone B's poll cadence (typically <10 s).

### Also: `ui/fonts.py` package-name coupling fix

Per NOTES_TO_DAEMON 2026-05-26 (recorder peer team): the
hard-coded Android candidate
``/data/user/0/org.atoznback.azt_recorder/files/app/fonts/``
referenced an Android package name that **never existed** —
the recorder ships as ``org.atoznback.aztrecorder`` (no
underscore). Pure dead code; would also have failed
cross-UID even if the package name had been right. The
documented "Android peer's app dir" fallback was a no-op on
every device, ever. Field symptom: all Android peers silently
fell back to Roboto, producing boxes for Lingala ``ɛ`` /
``ɔ`` + combining tone marks.

Fix: new Candidate #1 ``<client_dir>/../fonts/<filename>``
resolves to ``<files>/app/fonts/<filename>`` on Android
regardless of peer package name (p4a packs source flat under
``<files>/app/``) AND to ``<recorder>/fonts/<filename>`` on
desktop with the recorder/symlink layout. The dead hard-coded
``org.atoznback.azt_recorder`` candidate is removed entirely.
Also: single-line stderr diagnostic on the ``Roboto``
fallback path so a future silent miss surfaces in field logs
without inferring from glyph behaviour.

Once on 0.45.45+, the recorder can drop its peer-side workaround
(search for "Workaround for an upstream bug in
azt_collab_client/ui/fonts.py" in recorder ``main.py``).
NOTES entry deleted per the file's "live queue only" rule.

### Files

- ``azt_collabd/server.py`` — ``_h_project_status`` reads HEAD,
  emits ``head_sha`` in the response.
- ``azt_collab_client/projects.py`` — ``ProjectStatus.head_sha``
  field + ``from_dict`` decode.
- ``azt_collab_client/CLIENT_INTEGRATION.md`` — § 17b
  generalized to cover content refresh.
- ``azt_collab_client/ui/fonts.py`` — source-tree-relative
  Candidate #1; fallback diagnostic on Roboto path.
- ``azt_collab_client/NOTES_TO_DAEMON.md`` — fonts entry
  removed (resolved here).

### Wire format

Additive only — new key on ``project_status`` response. Pre-
0.45.45 peers ignore the key; their content-reload branch
no-ops (no head_sha to compare). Pre-0.45.45 daemons return no
``head_sha``; 0.45.45+ peers see empty string and don't fire
the reload — degrades to "only refresh on gesture / on_resume"
which is the pre-this-release behaviour.

## 0.45.44 — Post-receive: merge HEAD into working tree, don't just defer

### Field-reported bug

User's flow: two phones recording into the same LAN-shared
project. Sequential record-then-sync should let the second
phone see the first's data without a manual sync gesture.

Logs from 10:47–10:48 showed the surface signals working —
commits propagating both ways, ``[lan-push] advanced … →`` lines
landing on the peer — but a structural correctness gap when
both phones happened to be editing while a peer push arrived.

### Cause

After 0.45.39's deferred post-receive reset:

1. Phone B pushes commit ``B1`` to phone A.
2. dulwich's receive-pack handler advances HEAD to ``B1`` —
   but does **not** touch phone A's index or working tree
   (refs-only update).
3. Phone A has unstaged edits, so the 0.45.39 guard defers
   ``porcelain.reset(--hard)``. Working tree keeps phone A's
   edits; index stays at phone A's prior HEAD; HEAD now points
   at ``B1`` (which contains phone B's content X that's neither
   in phone A's index nor working tree).
4. Phone A's next ``commit_project`` runs ``_stage_all`` →
   index now reflects working tree (phone A's edits, no X) →
   ``porcelain.commit`` creates a commit with parent=``B1`` and
   tree=(phone A's content only). The new commit's diff vs ``B1``
   is ``-X + Y`` — i.e. it **silently reverts X** while adding
   phone A's own change Y.
5. Fan-out fast-forwards phone B from ``B1`` to phone A's new
   commit. Phone B's working tree, after its own post-receive
   reset, no longer has X.

Net: phone B's recording is preserved in git history (the
``B1`` commit is still reachable) but absent from HEAD and from
phone B's working tree. The user perceives this as data loss.

### Fix

Replace the deferred-reset path with a real three-way merge.

New helper ``repo.integrate_head_into_working_tree(repo,
project_dir)``. Walks HEAD's tree and the base tree
(``HEAD.parents[0]`` — the pre-pack HEAD on a fast-forward),
and for each path:

- **Unchanged in HEAD vs base** → working tree untouched.
- **Working tree already matches HEAD** → no-op (both phones
  produced the same content, e.g. both recorded the same
  CAWL row's audio).
- **Working tree == base** (no local edit on this file) →
  take theirs: write HEAD's blob to working tree.
- **Both sides changed**:
  * ``.lift`` paths use ``lift_merge.three_way_merge``
    (entry-aware, conflict-annotated); merged bytes land in
    working tree.
  * Other paths (binary audio, images) — keep ours and log
    loudly. Audio filenames carry guid + timestamp so
    cross-peer collisions are rare; if they do collide, the
    safer choice is to preserve what the local user just
    produced.
- **Deleted in HEAD vs base, working tree == base** → honor
  the deletion.
- **Deleted in HEAD vs base, working tree edited** → keep
  ours (preserve user's mid-edit work).

Leaves the index untouched. The next ``commit_project`` stages
the merged working tree on top of HEAD; the resulting commit
preserves both sides. The fan-out push that follows is then
correct — phone B fast-forwards to a tree containing both
phone A's and phone B's recordings.

``_reset_working_tree_after_receive``'s deferred branch (the
``unstaged_mod`` path) now calls
``integrate_head_into_working_tree`` instead of returning. On
merge bailout (e.g. first commit, no parent to compute base
from) or exception, falls through to the legacy deferred
behaviour as a conservative backstop.

The idle-receiver path (no unstaged mods) continues to use
``porcelain.reset(mode='hard')`` — unchanged. That's the common
case (one phone records, the other is idle) and the reset is
correct there.

### What the user gets

The "one user records, the other sees it without a manual sync"
UX works in both the common case (idle receiver) and the
contended case (both phones editing). Phone A's recordings
propagate to phone B's working tree on receive-pack, with
phone B's in-flight edits preserved via entry-level LIFT merge.

### Files

- ``azt_collabd/repo.py`` —
  ``integrate_head_into_working_tree(repo, project_dir)``: new
  three-way file merge helper.
- ``azt_collabd/lan_listener.py`` —
  ``_reset_working_tree_after_receive``: deferred-on-unstaged
  branch now calls ``integrate_head_into_working_tree`` and
  reports merge counts in stderr.

### Wire format

None. Pure daemon-internal correctness fix.

## 0.45.43 — Picker emits LAN-cloned project to host immediately

### Field-reported UX bug

After accepting a LAN share-offer (or scanning a pair+share QR)
in the picker, the clone succeeded but the popup just dismissed,
leaving the user on the picker with the project list still
showing the pre-clone snapshot. The new project was only visible
after backing out of the picker and re-entering. From the user's
perspective: "I tapped Accept, the popup closed, and nothing
happened."

### Cause

``picker.py``'s KV invoked the popup as
``on_release: LAN_POPUPS.pending_offers_popup()`` with no
``on_done``. The popup itself supports a callback (the daemon's
``_h_lan_accept_offer`` stamps ``last_project`` server-side on
success, and the popup hands a typed ``Result`` to ``on_done``)
but nothing was wired to consume it. The clone happened, daemon
state updated, popup dismissed — and the picker's project list
remained whatever it was on entry, never re-populated.

### Fix

New screen method ``ProjectPickerScreen.receive_from_phone()``
wraps ``pending_offers_popup`` with an ``on_done`` that:

1. Filters for ``LAN_PROJECT_CLONED`` / ``LAN_PROJECT_REOPENED``
   on the result (other branches just refresh the picker list).
2. Extracts the cloned project's ``langcode`` from the Status
   params.
3. Resolves the project's ``lift_path`` via
   ``open_project(langcode)`` (the same registry the existing
   buttons populate from).
4. Calls ``app.load_lift(path, langcode)`` — the same exit gesture
   the existing project-list buttons use. The host's
   ``load_lift`` routes to ``_emit_and_quit`` on the server APK
   (Activity result → peer), or to whatever the host wants on a
   non-external launch.

Same wiring covers the QR-scan path (``pending_offers_popup``'s
"Scan QR code" fall-through bubbles its result back through the
same ``on_done``).

Net effect: the user lands inside the freshly-cloned project the
moment the clone completes, without an extra "exit then re-enter
picker" gesture.

### Files

- ``azt_collab_client/ui/picker.py`` —
  KV's "Receive a project from another phone" button now calls
  ``root.receive_from_phone()`` instead of
  ``LAN_POPUPS.pending_offers_popup()`` directly;
  ``ProjectPickerScreen.receive_from_phone`` is the new method.

### Wire format

None. Pure client-side UI wiring; daemon state was already
correct (last_project stamped, project registered).

## 0.45.42 — Auto-init git at project creation + NOT_A_REPO recovery

### Root cause for the field report

A user reported:
- Started a new project on phone A via "Start a new project."
- Shared it to phone B over LAN.
- Phone B saw the offer, accepted, and got nothing — project never
  appeared, no error message.
- Phone A's daemon log: ``[commit] 'en-001-x-kent' done:
  codes=['NOT_A_REPO']`` on every commit attempt.

The recordings were landing on disk (atomic-finalize wrote audio +
LIFT bytes successfully) but **every commit was failing silently**
because the project had no ``.git/`` directory.

``projects.create_from_template`` (the "Start a new project"
backend) downloaded the template LIFT, wrote it to disk, and
called ``register`` — but **never ran ``porcelain.init``**. The
working_dir existed; the LIFT existed; the registry entry
existed; but ``.git/`` did not. Every subsequent
``commit_project`` saw ``_get_repo`` return None and returned
``NOT_A_REPO`` to the peer, which was filtered by the auto-sync
silence contract (§ 17 routing). The peer never knew.

Knock-on consequences:
- No git history of recordings. Crashes lose work.
- LAN listener returns 404 for the project (no ``.git/`` to
  serve refs from). Share-offers from this phone are
  un-cloneable.
- Publish-to-GitHub eventually masks the bug (``init_repo`` does
  the init at that point), but a user who never publishes never
  enters git history.

This has been latent since ``create_from_template`` shipped.

### Fix, two layers

1. **Create-time init.** ``projects.create_from_template`` now
   calls a new helper ``repo.ensure_initial_commit(project_dir,
   contributor_name='AZT')`` after writing the template + registering.
   The helper runs ``porcelain.init`` (idempotent — re-init is
   safe), seeds ``.gitignore`` if missing, and reuses
   ``_commit_step_locked`` so the first commit captures the
   template LIFT and any other files in the working_dir. After
   this, every new project has a born HEAD and is immediately
   usable by ``commit_project`` and the LAN listener. Best-effort
   from ``create_from_template``'s perspective — a failure here
   doesn't fail the project create (the auto-init-on-commit
   recovery branch below is the safety net).

2. **Commit-time auto-init recovery.** ``_commit_repo_locked``
   now detects the legacy state where ``_get_repo`` returns None
   but ``project_dir`` is a valid directory, and runs
   ``porcelain.init`` inline before continuing to
   ``_commit_step_locked``. Holds ``project_lock`` for the
   duration, so no race with other writers. Recovers any project
   in the field that's currently stuck in the NOT_A_REPO state —
   user records into it, debounced commit fires, the auto-init
   path creates ``.git/`` + commits the accumulated content as
   the first commit. No user gesture required; next commit
   surfaces ``COMMITTED_LOCAL`` instead of ``NOT_A_REPO``, and
   the LAN listener can serve from the freshly-initialised repo.

### Files

- ``azt_collabd/repo.py`` —
  - ``ensure_initial_commit`` / ``_ensure_initial_commit_locked``
    (new) — idempotent init + initial commit helper.
  - ``_commit_repo_locked`` — auto-init recovery branch when
    ``_get_repo`` returns None on a valid project_dir.
- ``azt_collabd/projects.py`` —
  ``create_from_template`` calls ``ensure_initial_commit`` after
  ``register``. Best-effort; failure logged but doesn't fail the
  create.

### Wire format

None. Pure daemon-internal correctness fix. Existing peers see
``COMMITTED_LOCAL`` instead of ``NOT_A_REPO`` on the recovered
project's next commit — strictly an improvement; no peer change
required.

### Recovery for users already stuck

Affected users open the broken project and record one entry.
The debounce fires within 500 ms; ``_commit_repo_locked`` runs
the auto-init recovery; the project's accumulated audio +
LIFT enter git history as the first commit; future commits
work normally. LAN sharing then works.

## 0.45.41 — Share LAN-only projects + pre-flight share state + retryable accept-offer

Field-reported bug cluster: user shared an unpublished project to
a peer via LAN, peer accepted the offer, project never appeared,
and the daemon UI's Share button disappeared after the share so
they couldn't retry / re-show the QR / share to a third phone.
Root causes were two separate-but-aligned bugs in the share flow.

### Bug 1: Share button gated on GitHub publish

The daemon settings UI's "Share [{langcode}] project" button (and
the whole project-actions row containing it) was hidden when the
project had no ``remote_url`` set. That gate dated to when this
row carried the github-only "Grant collaborator access" button;
collapsing the three sharing modes into one popup (0.45.0) made
the gate wrong — LAN-only sharing doesn't need a github remote.

**Fix.** Drop the ``if not live_remote_url: return`` gate in
``SyncSettingsScreen._refresh_project_actions_row``. The row is
now shown whenever a project is selected; the info label says
"(not published to GitHub — share over local network only)"
when there's no remote.

Inside ``share_project_popup``, the github-invite section
(section 3) now only renders when the project HAS a github
remote — tapping it without one would have NO_REMOTE-errored
anyway. Section 1 (paired phones) and section 2 (QR) work
without a remote and always render.

### Bug 2: Owner-side accept-offer silently fails on uninitialised project

When the owner shared a project that lacked a usable
``.git/HEAD`` (typical for a freshly-created project the user
hadn't recorded into yet), the share-offer queued on the
receiver's side fine — but the receiver's accept-offer LAN
clone hit the owner's listener and got a generic 404 because
dulwich couldn't open a repo with no commits. The receiver's
accept-offer handler then removed the pending decision so the
user had no path to retry.

**Fix, two halves:**

1. **Owner-side pre-flight in ``_h_lan_send_share_offer``.**
   Refuses the share with a typed error when the project's
   working_dir is missing a ``.git/`` directory or HEAD ref is
   unborn. Error key ``project_not_initialised`` /
   ``project_unborn`` / ``project_unreadable``; ``detail``
   explains "record at least one entry first" so the user
   sees an actionable message immediately rather than silent
   failure on the recipient minutes later.

2. **Receiver-side retainment in ``_h_lan_accept_offer``.**
   Removes the pending decision only when the LAN clone
   actually delivered (``LAN_PROJECT_CLONED`` /
   ``LAN_PROJECT_REOPENED``). On failure the decision stays
   addressable so the user can retry once the owner-side issue
   clears, without re-asking the owner to re-share. The retained
   decision shows up on the next ``lan_pending()`` poll.

### Files

- ``azt_collabd/ui/app.py`` —
  ``_refresh_project_actions_row`` no longer hides the row when
  ``remote_url`` is empty; info label adapts text.
- ``azt_collab_client/ui/lan_popups.py`` —
  ``share_project_popup`` gates section 3 on
  ``project_status(langcode).remote_url``.
- ``azt_collabd/server.py`` —
  ``_h_lan_send_share_offer`` adds the working-dir / HEAD
  pre-flight (typed errors); ``_h_lan_accept_offer`` retains the
  pending decision when the clone didn't deliver.

### Wire format

Additive — new error string values from ``_h_lan_send_share_offer``
(``project_not_initialised`` / ``project_unborn`` /
``project_unreadable``). Old clients reading the legacy `error`
strings fall through to a generic failure path; new clients
(0.45.41+) can route to a "record something first" message.

## 0.45.39 — LAN-sync hardening sweep + doc refresh

Audit-driven follow-up to the 0.45.0–0.45.38 LAN-sync iteration.
Five tightenings to data paths the audit flagged as racy or
under-scoped; one security gate (QR-display binds auto-share);
dead code removed; the peer contract documentation catches up
with what shipped.

### Code

1. **LAN merge path holds ``project_lock``.**
   ``lan_push._merge_then_push`` was performing fetch + lift-
   aware three-way merge + push (working-tree writes, HEAD
   advance) without acquiring the per-project lock. Could
   interleave with a concurrent ``commit_project``,
   ``atomic_finalize``, or the post-receive working-tree reset
   (all of which already hold the lock). Wrap the whole
   sequence in ``project_lock`` with a 5 s timeout — LAN
   delivery is opportunistic, defer to the next drain pass if
   the project is busy.

2. **``.git/config`` writes serialized via ``project_lock``.**
   The 0.45.37 retroactive ``strip_lan_origin_if_present`` ran
   on every ``_h_project_status`` poll (picker fires it every
   few seconds) without locking; concurrent ``init_repo`` /
   Publish / ``_h_lan_adopt_origin`` was the race. Add a pre-
   check (lock-free read of the origin url) so we only acquire
   the lock when we'd actually mutate. New 2 s timeout; defer
   to the next poll if busy.

3. **``strip_lan_origin_if_present`` no longer wipes legitimate
   private-IP origins.** Previously stripped any
   ``192.168.x.y`` / ``10.x.y.z`` origin — including a user
   who deliberately pointed Publish at a self-hosted Gitea on
   a private IP. Added ``scope_to_paired_peers=True`` (the
   default for the retroactive ``_h_project_status`` path): only
   strip origin URLs whose host appears in a paired peer's
   ``endpoints`` / ``static_endpoints`` list. The fresh-clone
   ``lan_clone`` path passes ``scope_to_paired_peers=False``
   because it's by-construction operating on a LAN-cloned URL.

4. **Post-receive reset defers when working-tree edits are
   in flight.** 0.45.38 added a 60 s pending-age guard catching
   Phase 1 of ``atomic_open_write`` (scratch tokens under
   ``.azt_atomic_pending/``). Gap: between Phase 2
   (``os.replace`` lands bytes at the final path) and the next
   ``commit_project``, the tracked file is on disk as
   unstaged_mod with new content but old SHA in index — and
   ``reset --hard HEAD`` would silently revert that to old-HEAD
   content. New guard: if ``porcelain.status.unstaged`` lists
   any non-pending path, defer the reset; the next
   ``commit_project`` absorbs the index/HEAD mismatch the
   pre-0.45.36 way. Worst case is briefly inflated ``n_changes``
   until the next commit — strictly no worse than pre-0.45.36
   and recoverable; the alternative was silent LIFT-edit data
   loss in the Phase 2 ↔ receive-pack race window.

5. **Auto-share on hello requires a recent QR-display
   gesture.** Listener has ``CERT_NONE`` deliberately
   (stdlib ssl can't request a client cert without validating
   its CA chain, see ``_build_server``), so the body's
   ``peer_id`` / ``fp`` / ``langcode`` claims aren't TLS-bound.
   Pre-fix, an attacker on the LAN could POST
   ``/v1/lan/hello`` with any peer_id + any langcode and our
   daemon would (a) record them as paired and (b) add their
   claimed langcode to their ``shared_projects`` allowlist —
   at which point the dulwich smart-protocol handler accepts
   ``GET /<lang>.git/info/refs`` from them and the project
   exfiltrates over the LAN. New gate: ``_h_lan_pair_qr``
   records a 10-minute single-use QR-offer for the displayed
   langcode in ``lan_listener._pending_qr_offers``; the hello
   handler's auto-share branch calls ``consume_qr_offer`` and
   only fires ``add_shared_project`` when an active offer
   exists. Without a recent QR display, the hello still
   records the pair (legitimate symmetric-pairing path) but
   refuses the auto-share — user can still tap Share manually.

6. **LAN_TOGGLE_OFF wired into outbound endpoints.**
   ``_h_lan_clone`` and ``_h_lan_send_share_offer`` now refuse
   with the typed code when ``lan.allow_sync`` is off, instead
   of silently failing at connect-time with the misleading
   ``LAN_PEER_UNREACHABLE``. Peer UIs can route the user to the
   toggle.

7. **Dead ``/v1/lan/share_project`` endpoint removed.** The
   bookkeeping-only ``_h_lan_share_project`` was reachable via
   raw RPC but the client wrapper ``lan_share_project()`` calls
   the strictly-more-complete ``/v1/lan/send_share_offer``
   (which both updates the allowlist AND fires the courtesy
   notification). Removing the duplicate clears one source of
   future drift.

8. **"Restart server" button on the server-too-old popup.**
   Previously the popup offered only Update (download + install
   a fresh APK) and Quit. Common field case the new button
   handles: user already installed the new server APK from a
   side channel (file manager, browser sideload, shared APK),
   so the bytes on disk already satisfy the floor — but the
   old ``:provider`` process is still serving the old version
   because Android kept it alive across the replace. Tap →
   cooperative ``POST /v1/admin/restart`` → daemon exits →
   ContentProvider auto-spawn revives at the on-disk version →
   compat probe re-runs. On refuse / pre-0.43.20 daemon that
   doesn't know the endpoint, popup re-opens so the user can
   tap Update instead. ``install_server_apk_popup`` gains an
   ``on_restart_server`` optional callback; bootstrap's
   ``_prompt_server_update`` wires it via
   ``_restart_server_from_popup``.

### Docs

- **``azt-collab/CLAUDE.md``**: three new architecture
  invariants — LAN sync (peer-to-peer, opportunistic, github
  authoritative), ``.git/config`` writes hold ``project_lock``,
  LIFT merge truncation guards. New runtime-config table rows
  (``sync.work_offline``, ``sync.commit_pack_byte_budget``,
  ``lan.allow_sync``). New Android-specifics paragraph on the
  LAN foreground service.
- **``azt_collab_client/CLIENT_INTEGRATION.md``**: brand-new
  § 17d routing table for LAN status codes (all 14 of them);
  brand-new § 20 on the LAN peer surface (hard rules, public
  API, reference UI, migration checklist, security model);
  § 17 routing table extended with the five non-LAN codes that
  shipped earlier without making it into the contract
  (``DNS_RESOLUTION_FAILED``, ``SYNC_GIVING_UP_TRANSIENT``,
  ``TOPIC_BRANCH_CONFLICT``, ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET``,
  ``LARGE_AUDIO_FILE_DETECTED``).

### Wire format

Additive only — no new wire endpoints. New status emission
points for ``LAN_TOGGLE_OFF``. Old peers that don't route
``LAN_TOGGLE_OFF`` fall through to the "everything else
(translate to status line)" branch, which still surfaces the
translated message ("Local-network sync is off"); only the
"route to the toggle" affordance is missing.

### Files

- ``azt_collabd/lan_push.py`` — locks/import + split
  ``_merge_then_push`` into outer (lock-acquire) + inner
  (``_merge_then_push_locked``).
- ``azt_collabd/repo.py`` — ``_host_matches_known_lan_peer``
  helper; ``strip_lan_origin_if_present`` gains
  ``scope_to_paired_peers`` + ``project_lock`` wrap.
- ``azt_collabd/lan_clone.py`` — pass
  ``scope_to_paired_peers=False`` from the fresh-clone path.
- ``azt_collabd/lan_listener.py`` — new
  ``_pending_qr_offers`` tracker + ``record_qr_offered`` /
  ``consume_qr_offer``; ``_handle_hello_bodyauth`` consults
  the gate; ``_reset_working_tree_after_receive`` adds the
  Phase-2 unstaged-mod guard.
- ``azt_collabd/server.py`` — ``_h_lan_pair_qr`` calls
  ``record_qr_offered``; ``_h_lan_clone`` /
  ``_h_lan_send_share_offer`` gate on
  ``settings.lan_allow_sync()``; remove ``_h_lan_share_project``
  + its dispatch entry.
- ``azt_collab_client/ui/popups.py`` —
  ``install_server_apk_popup`` gains ``on_restart_server`` kwarg
  + optional "Restart server" button.
- ``azt_collab_client/ui/bootstrap.py`` —
  ``_prompt_server_update`` passes ``on_restart_server``;
  ``_restart_server_from_popup`` is the cooperative-restart
  handler (re-probes on accept, re-opens popup on refuse).
- ``CLAUDE.md`` — three new invariants + runtime-config + LAN
  FGS paragraph.
- ``azt_collab_client/CLIENT_INTEGRATION.md`` — § 17 extended;
  new § 17d + § 20.

## 0.45.38 — Defer post-receive reset while atomic_open_write is in flight

### Bug

The 0.45.36 post-receive-pack working-tree-reset middleware
races with the peer's two-phase ``atomic_open_write`` protocol:

1. Peer Phase 1: opens ``.azt_atomic_pending/<token>`` via
   ContentProvider FD, writes bytes, closes FD. **No
   ``project_lock`` held during the write itself** — the FD
   path bypasses the daemon's per-project lock.
2. **Concurrent incoming push**: daemon's
   ``_post_receive_pack_middleware`` runs after a successful
   ``POST /git-receive-pack``. It acquires ``project_lock``,
   runs ``porcelain.reset(repo, mode='hard', treeish=HEAD)``,
   releases.
3. Peer Phase 2: ``atomic_finalize_pending(token, rel_path)``
   RPC. Daemon acquires ``project_lock``, looks for
   ``.azt_atomic_pending/<token>`` → not found.

Field surfaced as ``OSError: atomic_commit(...) failed:
['SERVER_ERROR'] (SERVER_ERROR: pending_not_found)`` inside
the recorder's ``stop_recording`` flow. The LIFT save aborted,
the post-stop state transition never completed, and the
recorder UI hung in "still recording." That UI-wedge is a
recorder-side bug (a save failure shouldn't lock the UI); the
``pending_not_found`` itself is this daemon-side race.

### Fix

``_reset_working_tree_after_receive`` consults
``.azt_atomic_pending/`` *before* taking the lock. If any file
in there is younger than ``atomic_recovery._MIN_AGE_S`` (60 s)
— the same threshold ``atomic_recovery`` already uses for
exactly this reason — the reset is deferred. The next push
(or the next ``commit_project``) absorbs the index/HEAD
mismatch the old way; worst case is the pre-0.45.36
``n_changes`` behavior, strictly no worse than before.

### Files

- `azt_collabd/lan_listener.py` —
  ``_reset_working_tree_after_receive`` adds a young-pending
  guard at the top; new ``import time as _time``.

## 0.45.37 — Strip LAN origin after clone + adopt-origin recovery via Publish + per-row Unpair

### Why

After ``lan_clone`` from a peer, the cloned project's
``.git/config`` had ``remote.origin.url`` set to the peer's
LAN listener URL (``https://192.168.x.y:port/<langcode>.git``).
The publish-row's "hide Publish if remote_url present" gate
treated this as a github remote and hid Publish — leaving the
user with no path to back up to github. Re-cloning didn't help
(the existing-project branch of ``lan_clone`` doesn't re-pop
the in-flow adopt-origin popup). User-reported field symptom:
"Publish isn't there, and not at all clear how to recover."

### Change

Four pieces:

1. **Strip private-IP origin in ``lan_clone``'s fresh-clone
   path.** New helper ``repo.strip_lan_origin_if_present``
   detects an origin URL pointing at a private/loopback IP
   (RFC 1918, ``localhost``, etc.) and removes the entire
   ``[remote "origin"]`` section. Falls back to clearing just
   the ``url`` key on older dulwich.

2. **Auto-migrate on every ``_h_project_status``.**
   Retroactive-fix for projects cloned before 0.45.37 —
   strip runs on the next status poll, Publish appears
   immediately afterward. Idempotent on healthy
   github/gitlab origins.

3. **Publish adopts a pending ``adopt_origin`` URL.**
   ``_do_publish`` consults ``lan_pending()`` first; if a
   pending ``adopt_origin`` decision exists for the current
   langcode, its URL is used as the publish target (so the
   user picks up the peer's existing github repo) rather than
   inferring ``<user>/<langcode>``. Recovery for users who
   missed the in-flow adopt-origin popup at scan time —
   Publish becomes the unified entry point.

4. **Per-row Unpair in ``paired_phones_popup``.** A direct
   Unpair button alongside Manage. Same destructive
   confirmation as the existing Manage→Unpair path. Surfaces
   the common case (re-paired phone has a stale entry under
   the old ``peer_id``) without making the user drill into
   Manage.

### Why "Publish adopts existing repo" works without an explicit prompt

Publish's underlying ``_ensure_remote_repo`` already catches
``HTTP 422 / 400 already exists`` from the create-repo API and
treats it as success. ``init_repo`` then sets the local
``origin`` to that URL and pushes. For the user's case (LAN-
cloned from a tablet that's been working offline, so local
HEAD is ahead of github HEAD), the push fast-forwards
cleanly. The only edge case that wouldn't auto-resolve is a
non-fast-forward push (a third device published while the
tablet was offline) — rare enough to handle when it comes up.

### Files

- `azt_collabd/repo.py` — ``_is_private_ip_url``,
  ``strip_lan_origin_if_present``.
- `azt_collabd/lan_clone.py` — call strip in fresh-clone path
  after ``_do_lan_clone``.
- `azt_collabd/server.py` — call strip from ``_h_project_status``
  as the retroactive-fix entry point.
- `azt_collabd/ui/app.py` — ``_pending_adopt_origin_url``;
  ``_do_publish`` consults it before falling back to inferred
  URL.
- `azt_collab_client/ui/lan_popups.py` — ``_build_peer_row``
  adds Unpair button; ``paired_phones_popup`` adds
  ``_confirm_unpair`` + confirmation dialog.

### Build-system

- ``android.numeric_version`` bumped to ``1260522001`` (same
  day, second release). ``__version__`` 3-part as required:
  ``0.45.37``.

## 0.45.36 — Batch: working-tree reset, atomic-pending self-heal, mDNS fixes

### Build-system note: explicit ``android.numeric_version`` (now required)

The ``0.45.34.1`` hotfix accidentally locked the suite into
4-part versions on Android: buildozer's default encoding gave
``0.45.34.1`` a 10-digit versionCode (``1026453401``), while
any subsequent 3-part version (``0.45.35``, ``0.45.36``, …)
encodes to 8 digits (``10264536``). Android refuses the
"upgrade" as ``INSTALL_FAILED_VERSION_DOWNGRADE``, and
``adb uninstall`` wipes user data (``$AZT_HOME``: projects +
credentials + jobs). Releasing 4-part versions to users is the
wrong long-term answer (semver hygiene + UI clutter).

Fix: override ``android.numeric_version`` in
``server_apk/buildozer.spec.tmpl`` explicitly. Format
``1_YYMMDD_NNN`` (date-based, monotonic). Current release uses
``1260522000``. **Bump for every release** — there's no
auto-computation from the version string. See the comment block
in the spec for rationale.

### Code changes (unchanged from the would-be 0.45.35)

Six independent fixes bundled into one release. Each piece is
narrowly-scoped; rollback story is per-file.

### 1. Working-tree reset after incoming receive-pack

Bug: dulwich's receive-pack handler advances refs without
updating the working tree. After a peer pushes commits to us
(fast-forward), our index still reflects the old HEAD, so every
file the incoming commits touched shows as ``staged_mod``.
Field n_changes spikes of 1409 → 1424 came directly from this.

Fix: WSGI middleware around the listener's HTTPGitApplication
catches successful ``POST /<lang>.git/git-receive-pack`` and
runs ``porcelain.reset(repo, mode='hard', treeish=HEAD)`` under
``project_lock`` (5 s timeout — defer if busy). Idempotent and
safe: a receive-pack only lands as fast-forward, so HEAD's tree
*is* what the working tree should now hold.

### 2. ``.azt_atomic_pending/`` self-heal migration

Bug: ``_stage_all`` filtered the scratch directory out of new
stagings, but pre-existing repos had scratch tokens tracked by
earlier code paths. They persisted as ``unstaged_mod`` forever,
contributing ~3.36 MB each to every commit.

Fix: ``_ensure_atomic_pending_self_heal`` runs from
``_stage_all`` on every commit. Two parts:

- Append ``.azt_atomic_pending/`` and ``.azt_atomic_orphans/``
  to the project's ``.gitignore`` if missing.
- ``del index[path]`` for any tracked file under
  ``.azt_atomic_pending/``. Files remain on disk for
  ``atomic_recovery`` to process; the index just stops carrying
  them.

Idempotent — a no-op once the state is correct. Init-time
``.gitignore`` content updated to include both dirs by default
for new projects.

### 3. ``[atomic-recovery]`` directory-scan trace

Field log baf 2026-05-22 showed n_changes stuck with atomic-
pending tokens visible on disk but no ``[atomic-recovery]``
lines mentioning them — couldn't tell whether the sweep had
even considered them.

``recover_project_orphans`` now emits one line per call when
the pending dir is non-empty:

```
[atomic-recovery] scanning '/…/.azt_atomic_pending': 8 entries
    (min_age=60s): [<token>@7200s, <token>@7150s, …]
```

Shows count + per-token ages so a tester can answer "did the
sweep see X?" without rebuilding.

### 4. Discovery restart after consecutive ``refused / unreachable``

Bug: when a peer rebinds to a new port, our cached endpoint
points at the dead old port. ``invalidate_endpoint`` clears
*our* cache, but NsdManager's internal cache still has the
stale advertisement and ``resolveService`` returns the old port
(or nothing). Field workaround was manually toggling LAN
off+on, which restarted ``discoverServices``.

Fix: ``lan_push`` tracks consecutive ``refused / unreachable``
counts per peer. After ``_RESTART_DISCOVERY_THRESHOLD = 3``
failures, it calls ``lan_discovery.restart_browse()`` —
``stopServiceDiscovery`` + clear endpoint cache +
``discoverServices`` again, the mechanical equivalent of the
manual toggle. Counter resets on success or after restart so we
don't loop.

### 5. Persist resolved endpoints into ``static_endpoints``

Bug: ``peers.json::static_endpoints`` was set at pair time and
never refreshed. After a peer rebind + daemon respawn (mDNS
state empty), we fell back to the pair-time port, which is now
ancient.

Fix: ``_persist_resolved_endpoint`` runs from
``onServiceResolved`` — writes the freshly-resolved
``host:port`` to the head of ``static_endpoints`` so the static
fallback drifts forward to track the peer's current location.
Idempotent: skips the write if the head entry already matches.

### Files

- `azt_collabd/lan_listener.py` —
  ``_reset_working_tree_after_receive``,
  ``_post_receive_pack_middleware``, wired into
  ``_build_server``.
- `azt_collabd/repo.py` —
  ``_ensure_atomic_pending_self_heal``; default ``.gitignore``
  content updated.
- `azt_collabd/atomic_recovery.py` —
  ``recover_project_orphans`` directory-scan trace.
- `azt_collabd/lan_push.py` — per-peer
  ``_consec_failures`` counter; ``restart_browse`` on
  threshold; reset on success.
- `azt_collabd/lan_discovery.py` — ``restart_browse``;
  ``_persist_resolved_endpoint`` called from
  ``onServiceResolved``.

## 0.45.34.1 — Recovery-commit LIFT delta trace (verify the merge fix)

### Why

`git diff HEAD~ baf.lift` isn't accessible on the device —
working_dir lives in the server APK's private filesDir, release-
signed APKs forbid ``adb run-as``, and we don't ship a ``git``
binary. Without a way to inspect each recovery commit's effect
on the LIFT, the tester can't tell whether the 0.45.34 merge fix
is actually shrinking the spurious annotation pollution.

### Change

``_recover_under_lock`` now emits a single per-merge line:

```
[atomic-recovery] '<token>' merge delta:
    lift_bytes 26,001,840 → 24,150,330 (-1,851,510),
    conflict_annotations 1700 → 1218 (-482)
```

Tester reads it via the Share daemon-log button. With 0.45.34
in place, post-merge annotation counts should *decrease*
(canon-equal stripping) on a polluted LIFT; pre-0.45.34 they
*increased* every cycle. The byte delta tracks the same trend
indirectly. Counts are via substring-match on
``b'azt-lift-conflict'`` — well-formed LIFT only contains that
token in the annotation attribute, so the heuristic is exact in
practice.

### Files

- `azt_collabd/atomic_recovery.py` — ``_recover_under_lock``
  inserts the delta print between the merge call and the
  ``_atomic_write_bytes`` call.

## 0.45.34 — Self-healing LIFT merge (strip false-positive conflict markers)

### Bug

Field baf 2026-05-22 showed entries accumulating
``<annotation name="azt-lift-conflict">`` markers (4 → 5 → 6 …
on the same `<form>`) across recovery cycles, and duplicate
``<form>`` blocks inserted between identical existing forms.
``git diff`` example::

  <form lang="en">
      <text>body</text>
  -    <annotation azt-lift-conflict="ours" /> × 4
  +    <annotation azt-lift-conflict="ours" /> × 5
  </form>
  +<form lang="en">                  ← duplicate of next form
  +    <text>body</text>
  +    <annotation azt-lift-conflict="theirs" />
  +</form>
  <form lang="en">
      <text>body</text>
      <annotation azt-lift-conflict="theirs" />
  </form>

User: "there was never any conflict in the first place — we are
not editing this field." Both forms have ``<text>body</text>``.
Semantically identical. The merge was flagging them as
conflicting anyway.

### Root cause

``_merge_pair`` compared elements with raw ``ET.tostring`` bytes
(``_canon``). Stale ``azt-lift-conflict`` annotations from
previous merges, plus whitespace/indentation differences between
the orphan's LIFT and the current LIFT, made byte-equal
semantically-identical content compare unequal → spurious
conflict → fresh annotations appended → next merge sees even
*more* unequal-looking content → more annotations. A vicious
cycle that grew the LIFT by 1700+ markers per recovery on truly
identical content.

### Fix

Two helpers added to ``lift_merge.py``:

- ``_strip_conflict_annotations(elem)`` — recursively removes
  every ``<annotation name="azt-lift-conflict" ...>`` child in
  place.
- ``_strip_indent_whitespace(elem)`` — normalizes inter-element
  whitespace (text/tail are cleared when they contain only
  whitespace and the element has children).
- ``_canon_clean(elem)`` — canonical bytes for *detection*:
  strips conflict annotations + normalizes whitespace, then
  ``ET.tostring``. Used by ``_merge_pair`` instead of raw
  ``_canon`` for the ``o vs t`` and ``base vs t``/``base vs o``
  equality checks.

When ``_canon_clean(o) == _canon_clean(t)`` the merge emits a
*stripped* clone — annotations vanish on the canon-equal path.
This makes the merge **self-healing**: every pass over a
polluted LIFT collapses the spurious annotations on identical
content back to nothing, leaving only markers on genuinely-
divergent content. Real conflicts (where the underlying content
actually differs) are still expressed with fresh
``azt-lift-conflict`` markers as before.

### Files

- `azt_collabd/lift_merge.py` —
  - new helpers ``_strip_conflict_annotations``,
    ``_strip_indent_whitespace``, ``_canon_clean``.
  - ``_merge_pair`` uses ``_canon_clean`` for detection, emits
    annotation-stripped clones on canon-equality, including the
    base-comparison "only-one-side-changed" paths.

### Deferred to 0.45.35

The four other 0.45.34 candidates remain pending — landing the
merge fix alone keeps the rollback story clean if anything
sideways surfaces:

1. Working-tree reset on incoming push (ghost ``n_changes``
   spikes after fast-forward).
2. Exclude ``.azt_atomic_pending/`` from ``_commit_repo``
   staging (27 MB-per-commit bloat from scratch tokens).
3. One-time untrack the currently-tracked atomic-pending
   tokens.
4. ``[atomic-recovery]`` directory-scan trace.

## 0.45.33 — Rendering contract: LANOK is independent of uncommitted changes

### Why

The §17b recipe pre-0.45.33 produced a single string and gated
``LANOK`` on ``commits_ahead > 0``, conflating two orthogonal
dimensions:

- ``n_changes`` — uncommitted working-tree changes (the red
  ``+N`` badge).
- ``unshared_commits`` / ``commits_ahead`` — committed-work
  replication status (the ``OK`` / ``LANOK`` / ``+u/a`` badge).

Existing peer behavior already showed ``OK`` alongside red
``+N`` (clean commit state, dirty working tree). ``LANOK``
should behave the same way: it's about whether COMMITTED work
is replicated, independent of whether new uncommitted edits
are queued.

### Change

CLIENT_INTEGRATION.md § 17b now describes a two-element render:
a sync-status string (OK / LANOK / +u/a + mode suffix) and a
separate red ``+N`` uncommitted-changes badge that's visible
whenever ``n_changes > 0``, regardless of the sync-status
string. Both can be shown together.

This is a peer-side contract update; no daemon code changes.
Peers rendering the old recipe still work — they just won't
surface LANOK during sustained editing.

### Files

- `azt_collab_client/CLIENT_INTEGRATION.md` § 17b — rewrite the
  Python rendering snippet to split status string from
  uncommitted badge.

## 0.45.32 — Fan out to LAN peers immediately after each commit (LANOK latency)

### Why

The peer-side ``LANOK`` indicator requires
``unshared_commits == 0`` — i.e., every local commit must be an
ancestor of ``last_lan_pushed_sha`` (or ``origin/main``).
``last_lan_pushed_sha`` only advanced on the watcher loop's
30-second drain tick. Any commit landing between ticks left
``unshared_commits > 0`` until the next tick, so LANOK never
surfaced under sustained editing (typical field cadence: several
commits per minute, > 30 s cycle).

Other phone was actively fetching us and reaching our HEAD
within seconds, but our daemon didn't know until the next
outbound fanout ran ``_peek_peer_main`` and recorded the SHA.

### Change

``_run_commit`` (scheduler) now calls ``lan_push.fan_out`` after
each ``COMMITTED_LOCAL`` when ``lan.allow_sync`` is on.
``fan_out`` is idempotent (peeks each peer's ``main`` first, no-
ops if already at our HEAD), so the cost is one ls-remote
round-trip per paired peer per commit. The latency from
``COMMITTED_LOCAL`` to ``last_lan_pushed_sha`` updated drops
from up to 30 s to single-digit ms on a healthy LAN — LANOK
surfaces on the next peer ``project_status`` poll.

### Files

- `azt_collabd/scheduler.py` —
  ``_run_commit``: after the ``set_last_commit`` call on
  ``COMMITTED_LOCAL``, conditionally fire
  ``lan_push.fan_out(p)``.

## 0.45.31 — Dump untracked/unstaged paths when n_changes is large

### Why

Field symptom: ``n_changes`` jumps from 1 to 71 across a few
seconds with no peer-side write activity visible in the daemon
log (no ``[commit-rpc]`` from the recorder, no ``mode='w'``
``openFileDescriptor``). Can't tell whether the count is real
files (and which ones) or a dulwich counting quirk; rebuilding
to add a one-off trace is a multi-minute cycle.

### Change

``repo_status_summary`` now emits a single ``[repo-status]``
line when ``n_changes >= 5``, dumping a head-listing of the
``unstaged`` and ``untracked`` buckets with byte-safe decode.
Threshold avoids noise on healthy projects (the typical
single-file finalize-in-flight transient is 1–2).

### Format

```
[repo-status] n=71 staged_add=0 staged_mod=0 staged_del=0
  unstaged=1 untracked=70
  untracked_head=['.azt_atomic_pending/abcd…', ...]
  unstaged_head=['sw-US-x-kent.lift']
```

If the untracked head shows ``.azt_atomic_pending/<token>``
entries piling up, atomic-recovery is leaving orphans behind
(despite ``atomic_finalize``'s ``os.replace``). Other patterns
(e.g., 70 audio files) point to a different write source.

### Files

- `azt_collabd/repo.py` — ``repo_status_summary`` adds the
  ``[repo-status]`` diagnostic emit guarded by ``n >= 5``.

## 0.45.30 — Sync-indicator rendering: single source of truth in CLIENT_INTEGRATION.md § 17b

### Why

The sync-indicator semantics (the ``LANOK +5`` vs
``+unshared/ahead`` split, the four-cell ``work_offline ×
lan_allow_sync`` suffix matrix) were duplicated across the
peer contract in ``CLIENT_INTEGRATION.md § 17b`` and the
field-level docstrings on ``ProjectStatus.work_offline`` /
``lan_allow_sync`` / ``unshared_commits`` in
``azt_collab_client/projects.py``. Consistent today because
both were written in the same 0.45.0 pass, but they'd drift if
the rendering recipe evolved and only one side was updated.

### Change

``projects.py`` field docstrings shortened to "what is this
field" — they still describe the *semantic* (e.g.
``unshared_commits == 0`` means "shared somewhere", drives the
``LANOK`` split), but the rendering recipe (the full Python
``if/elif`` cascade with suffix construction) lives only in
``CLIENT_INTEGRATION.md § 17b`` now. Each field's docstring
ends with a forward-pointer there.

### Files

- `azt_collab_client/projects.py` — trim docstrings on
  ``work_offline``, ``lan_allow_sync``, ``unshared_commits``,
  ``lan_pushed_sha``; add "Rendering recipe:
  CLIENT_INTEGRATION.md § 17b" pointer.

## 0.45.29 — Stuck-commit triage line in project_status

### Why

Field-triaging a "red +N" sync indicator (uncommitted file
changes piling up) needs three numbers visible at once:
``n_changes``, ``commit_failure_count``, ``last_commit_error``.
On Android the daemon's only addressable from on-device peer
processes, so without a daemon-log emit a tester has no way to
read those fields unless the host app has already plumbed them
into a banner.

### Change

``_h_project_status`` now logs one short trace line per call
when *any* of ``n_changes``, ``commit_failure_count``, or
``last_commit_error`` is non-zero. Quiet on a healthy project
(picker polls every few seconds; we don't want to drown the
log on the happy path). Format:

```
[project_status] 'baf' n_changes=1424 commits_ahead=0 commit_fail=0 last_err=''
```

Reading: ``n_changes`` climbs while ``commit_fail`` stays 0 →
peer isn't calling ``commit_project``. ``commit_fail`` climbs →
real commit failures, ``last_err`` carries the dulwich message.

### Files

- `azt_collabd/server.py` —
  ``_h_project_status``: trace emit before the response dict.

## 0.45.28 — LAN listener: decode bytes path in dulwich smart-protocol POST

### Bug

With both listeners up at boot (0.45.25), pairs reached each
other for the GET ``/info/refs`` half of git smart-protocol but
the subsequent POST to ``/<repo>.git/git-upload-pack`` /
``/git-receive-pack`` died inside the receiver's
``_DynamicBackend.open_repository`` with ``TypeError: a bytes-
like object is required, not 'str'``. The receiver returned
HTTP 500; pusher logged ``[lan-merge] fetch from '<peer>'
failed: GitProtocolError('unexpected http resp 500 for
.../git-upload-pack')``.

Root cause: ``dulwich.web``'s GET handler passes the repo path
to ``backend.open_repository`` as ``str`` (sliced from
``mat.string[:mat.start()]``), but the smart-protocol POST
handler in ``dulwich.server`` (UploadPackHandler /
ReceivePackHandler ``__init__``) passes it as ``bytes`` from
the wire parser. Our backend's ``.lstrip('/')`` is a str method.

### Fix

``_DynamicBackend.open_repository`` decodes ``bytes`` to str via
``utf-8`` at entry before the existing normalization runs.
Comment expanded to capture the two-axis variance (encoding and
shape) so the next reader doesn't re-discover it.

### Files

- `azt_collabd/lan_listener.py` —
  ``_DynamicBackend.open_repository``: ``isinstance(raw, bytes)``
  → decode.

## 0.45.27 — Peer-id tag in shared-log filename

### Why

0.45.26 tagged the on-disk log filename
(``daemon-07c089f2.log``) but the Share button's display name —
the filename the tester sees when the OS save dialog opens —
was still ``azt_log_<stamp>.log`` regardless of device. Two
phones' shared logs landed in the tester's Downloads folder
with names that differed only by timestamp, defeating the
collision-avoidance the on-disk rename solved.

### Change

``share_log_file`` in ``azt_collab_client/ui/share.py`` now
calls ``lan_peer_id()`` and appends the short tag to the
display name: ``azt_log_<stamp>_07c089f2.log``. Falls back to
the un-tagged form if the daemon can't return a peer_id (e.g.
cryptography unavailable on the peer's build).

### Files

- `azt_collab_client/ui/share.py` —
  ``share_log_file`` resolves the tag once per call and appends
  it to ``display_name`` when one wasn't explicitly passed.

## 0.45.26 — Per-device peer-id tag in daemon log

### Why

Diagnosing LAN sync needs both phones' logs side by side, and
both arrive at the tester as identically-named ``daemon.log``
with identically-formatted ``[14:01:01]`` stamps — one phone's
log clobbers the other on save, and a paragraph quoted from
either is indistinguishable at a glance.

### Change

The stdio-tee now stamps every log line with the first 8 hex
chars of the daemon's ed25519 peer-id, e.g.
``[14:01:01 07c089f2] [scheduler] drain pushes: ['baf']`` — the
same short id the user already sees in ``[lan-push] '07c089f2'
...`` lines, so no new vocabulary. The on-disk filename gets
the same suffix: ``$AZT_HOME/daemon-07c089f2.log``, so two
phones' logs can sit in one folder without colliding. Both
fall back to the un-tagged form if the peer-id isn't readable
(fresh-install bootstrap, cryptography import failure).

Tag is computed once per process and cached, so log writes pay
nothing after the first call.

### Files

- `azt_collabd/server.py` —
  - `_log_peer_tag_str()` lazy resolver + module-level cache.
  - `daemon_log_path()` splices the tag into the filename.
  - `_StdioTee._write_to_file` uses the tagged stamp.

## 0.45.25 — Android service startup applies the LAN toggle

### Bug

A daemon respawn on Android left the daemon in a "toggle says yes,
listener says no" split-brain: persisted `lan.allow_sync=on` drove
the scheduler's LAN fan-out every 30 s (which then hammered each
paired peer's last-known endpoint with `Connection refused`), but
the listener thread, WifiLock, MulticastLock, FGS promotion, and
mDNS service-info were never re-armed — inbound bound nothing,
outbound advertised nothing. Field log baf 2026-05-22 caught it:
54 consecutive minutes of `[lan-push] ... refused / unreachable —
invalidated mDNS cache for re-resolve` with no
`[lan-listener] started` / `[lan-fgs] acquired` / `[lan-discovery]
nsd registered` until the user manually toggled LAN off→on.

### Fix

`server_apk/service.py:main()` now calls
`lan_listener.apply_toggle()` after `scheduler.start_watcher()`,
with the same try/except guard the desktop `server.run()` entry
path uses (line 3274). Idempotent — a no-op when the listener's
already up. Same class of bug as the pre-0.43.29 connectivity-
watcher omission (desktop wired it, Android `:provider` forgot
it); fixed now for the LAN listener.

### Files

- `server_apk/service.py` — added `before_lan_listener` /
  `after_lan_listener` boot-trace phases and the
  `apply_toggle()` call between `start_watcher()` and the idle
  loop.

## 0.45.0 — LAN sync transport (full implementation)

### Why

Field linguists in the same office today have one sync path: github.
When the internet is down or restricted, two phones a metre apart
are isolated. The parked LAN sync design
(``docs/local_lan_sync_stub.md``, 2026-05-19) was un-parked this
release to land the RPC layer + listener foundation so subsequent
peer rebuilds can pair, share, and fan-out commits across the local
network without burning metered data.

This release ships the daemon-side scaffolding and the wire surface.
It deliberately leaves UI affordances (the daemon's "Pair a phone"
page + the picker's "Scan to pair" entry point) and several
Android-side wiring steps as follow-ups — the RPC layer is enough
for a desktop two-``$AZT_HOME`` smoke and gives peer apps the
contract they'll integrate against.

### What landed

- **Per-device identity** (``azt_collabd/peer_id.py``). Generates an
  ed25519 keypair + self-signed X.509 cert on first call to a LAN
  endpoint, persisted as ``$AZT_HOME/peer_id`` (PKCS#8 PEM, mode
  0600) + ``$AZT_HOME/peer.crt`` (X.509 PEM). The hex peer-id is
  the raw ed25519 pubkey; the ``fp`` is sha256 of the cert DER.
  Lazy by design so an auto-spawned daemon doesn't pay the cost.
- **Paired-peers registry** (``azt_collabd/peers.py``). Atomic
  read/write of ``$AZT_HOME/peers.json``. Tracks ``device_name``,
  ``fp``, ``endpoints`` (QR-captured), ``static_endpoints``
  (user-managed), ``shared_projects``, ``paired_at``,
  ``last_seen_at`` per paired peer.
- **Pairing flow** (``POST /v1/lan/pair/qr`` + ``POST /v1/lan/pair/accept``).
  The QR endpoint returns the JSON payload to encode via segno
  (already in requirements); the accept endpoint records the peer
  into ``peers.json``. Auto-reverse-record on the listener's first
  authenticated request is a follow-up (see "Known gaps").
- **Project-share gesture** (``POST /v1/lan/share_project`` +
  ``unshare_project``). Per-direction allowlist; the listener will
  refuse fetches of projects outside the peer's ``shared_projects``.
- **HTTPS listener** (``azt_collabd/lan_listener.py``).
  ``dulwich.web.HTTPGitApplication`` + ``ThreadingMixIn`` + TLS via
  ``ssl.SSLContext.wrap_socket``. Custom request handler captures
  the verified client cert into the WSGI environ; WSGI middleware
  extracts the ed25519 pubkey from the DER, looks it up in
  ``peers.json``, validates the fingerprint, and confines the URL
  set to the peer's ``shared_projects`` before forwarding to
  dulwich. Hot-applied via ``POST /v1/lan/toggle {on: bool}``.
- **Foreground-service promotion** (``azt_collabd/android_cp/lan_fgs.py``).
  Acquires ``WIFI_MODE_FULL_HIGH_PERF`` WifiLock + ``MulticastLock``
  and calls ``startForeground(specialUse)`` on the ``:provider``
  service while the LAN toggle is on. No-op on desktop.
- **Discovery foundation** (``azt_collabd/lan_discovery.py``).
  Desktop: ``python-zeroconf`` advertise + browse, service type
  ``_aztcollab._tcp.local.``, TXT records ``peer_id`` / ``fp`` /
  ``v``. Android NsdManager path is stubbed (see "Known gaps").
- **Scheduler fan-out** (``azt_collabd/lan_push.py`` +
  ``scheduler._drain_pending_push``). Every drain pass also tries
  to push to each reachable paired peer that shares the project,
  with TLS pinned via urllib3's ``assert_fingerprint``. LAN
  success does NOT clear ``pending_push`` — github stays
  authoritative, LAN is opportunistic redundancy.
- **Hotspot / manual-IP fallback** (``POST /v1/lan/static_endpoints``).
  Endpoint resolution order: mDNS-cached → static → QR-hint.
  Covers only the fixed-IP hotspot-host case per the tightened
  scope in the spec; AP-isolated networks with DHCP churn are
  documented as out of scope for v1.
- **Status codes**: ``LAN_PAIRED``, ``LAN_UNPAIRED``,
  ``LAN_PEER_UNREACHABLE``, ``LAN_FP_MISMATCH``, ``LAN_TOGGLE_OFF``
  in both daemon + client status modules. English + French
  translations.
- **Client surface**: ``lan_peer_id``, ``lan_list_peers``,
  ``lan_pair_qr``, ``lan_pair_accept``, ``lan_share_project``,
  ``lan_unshare_project``, ``lan_unpair``, ``lan_toggle``,
  ``lan_set_toggle``, ``lan_set_static_endpoints`` (all in
  ``azt_collab_client``).
- **Build deps**: ``cryptography`` (ed25519 + X.509 generation) +
  ``zeroconf`` (desktop mDNS) added to
  ``server_apk/buildozer.spec.tmpl`` requirements. New Android
  permissions: ``FOREGROUND_SERVICE``,
  ``FOREGROUND_SERVICE_SPECIAL_USE``,
  ``CHANGE_WIFI_MULTICAST_STATE``, ``ACCESS_WIFI_STATE``.

### Wire format

Additive — new ``/v1/lan/*`` endpoints + new status codes. Old
peers ignore the surface entirely. ``MIN_CLIENT_VERSION`` floor
bumped to ``0.45.0`` to flush peer rebuilds through and make the
new surface available, per ``feedback_min_client_version``.

### Auto-reverse-record + Android wiring (closed here)

- **Auto-reverse-record (``POST /v1/lan/hello``)** lands as a
  short-circuit inside the listener WSGI middleware: an unpaired
  peer presenting a valid client cert can POST ``{peer_id, fp,
  device_name}`` to ``/v1/lan/hello`` and we record them
  symmetrically. ``_h_lan_pair_accept`` fires the hello call as a
  best-effort follow-up after recording the QR-scanned peer, so
  both sides land in each other's ``peers.json`` from a single
  scan.
- **``p4a_hook.py`` ``_AZTCOLLAB_SERVICE_BLOCK``** updated in
  ``~/bin/raspy/buildozer_tweaks/p4a_hook.py`` to declare
  ``android:foregroundServiceType="specialUse"`` + the inner
  ``<property android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
  android:value="lan-peer-git-sync" />`` so
  ``ServiceCompat.startForeground`` accepts the specialUse type.
- **NsdManager advertise + browse + resolve** implemented in
  ``azt_collabd/lan_discovery.py``: three ``PythonJavaClass``
  proxies (Registration, Discovery, Resolve) with strong refs
  pinned in module globals. Resolved services land in the
  ``peer_id → (host, port)`` cache that the scheduler's fan-out
  reads. Uses the legacy ``discoverServices`` call — bump to
  ``DiscoveryRequest`` + ``FLAG_SHOW_PICKER`` when the suite
  targets SDK 37.
- **jnius pre-warm** in ``server_apk/main.py`` step 2a.2:
  ``NsdManager`` + ``NsdServiceInfo`` + ``WifiManager`` classes
  are touched on the SDLThread so worker-thread lazy-init doesn't
  bootclassloader-NULL-deref later.
- **UI affordances** all land:
  - Daemon settings: "Local-network sync:" yes/no toggle +
    "Pair a phone" / "Paired devices" buttons + status line that
    shows the bound endpoint while listening.
  - Pair-QR popup (``azt_collab_client/ui/lan_popups.py``):
    renders the daemon's pairing payload as a QR via ``segno`` +
    ``CoreImage``; shows device_name + peer-id prefix for
    across-the-table verbal confirm.
  - Paired-devices popup: scrollable list with per-peer Manage
    sub-popup that toggles per-project share, edits static
    endpoints, and unpairs.
  - Picker "Pair with another phone" entry: KV calls
    ``LAN_POPUPS.scan_to_pair()`` directly so every picker host
    (server APK, recorder, viewer) gets the affordance without a
    new ``App``-method contract. Launches ZXing scanner, decodes
    the JSON payload, calls ``lan_pair_accept``, surfaces the
    translated ``Result`` to the user.

### Settings wording polish

- ``Servers`` section label → ``Servers (set up at least one)``
  to nudge users away from "I'll just open the picker without
  ever connecting GitHub or GitLab".
- ``Grant collaborator access`` button → ``Invite collaborator
  to project`` (matches the GitHub-side wording the user sees
  when accepting).
- ``Share this repo (QR)`` button → ``Share this project (QR)``
  (the suite-facing vocabulary is "project" everywhere else;
  "repo" was a leak from the GitHub side).

### Picker polish

- ``I have one on my phone`` button hidden via ``height: 0`` +
  ``opacity: 0`` + ``disabled: True`` per the Kivy hide/show
  pattern in ``~/.claude-sil/CLAUDE.md``. The handler
  (``app.open_file()``) is still wired so re-enabling is one
  prop flip away — the open-file path is currently rough on
  Android (SAF picker returns a content:// URI the daemon
  can't walk back to a working_dir).

### Translations

French msgstrs for every new UI string land in
``locales/fr/LC_MESSAGES/azt_collab_client.po`` alongside the
five LAN status codes. The translation-coverage drift detector
in ``pytest tests/`` should pass cleanly.

### Combined pair-share-clone flow (post-design-session redesign)

After the initial RPC-layer landing the design changed shape:
LAN pairing and project sharing collapse into one gesture, the
QR scan does pair + LAN-clone in a single step, and ``origin``
adoption is always behind a one-tap confirm. New surface:

**New status codes** in both daemon + client (with translations):
``LAN_PROJECT_CLONED``, ``LAN_PROJECT_REOPENED``,
``LAN_PROJECT_ADOPTED_REMOTE``, ``LAN_PROJECT_COLLISION_UNRELATED``,
``LAN_ADOPT_ORIGIN_NEEDED``, ``LAN_REMOTE_CONFLICT``,
``LAN_SHARE_OFFER``, ``LAN_SHARE_DECLINED``, ``LAN_OFFER_ACCEPTED``.

**New endpoints**:

- ``POST /v1/lan/clone {peer_id, langcode, remote_url?}`` —
  ``lan_clone.py`` does ls-remote collision detection, then
  TLS-pinned dulwich clone, then ``projects.register``. Stashes
  ``LAN_ADOPT_ORIGIN_NEEDED`` (or ``LAN_REMOTE_CONFLICT``) as a
  pending decision rather than touching ``origin`` silently.
- ``POST /v1/lan/send_share_offer {peer_id, langcode}`` —
  combined "update shared_projects allowlist + POST a courtesy
  offer to the peer's listener" call.
- ``POST /v1/lan/share_offer`` (listener side, paired-peer short-
  circuit) — receives an offer from a paired peer; stashes a
  ``LAN_SHARE_OFFER`` pending decision.
- ``POST /v1/lan/share_declined`` (listener side) — receives a
  nack; rolls back the sender's ``shared_projects`` allowlist
  for that peer + langcode.
- ``POST /v1/lan/accept_offer {decision_id}`` /
  ``POST /v1/lan/decline_offer {decision_id}`` — receiver-side
  resolution of a share-offer pending decision.
- ``POST /v1/lan/adopt_origin {decision_id, accept}`` /
  ``POST /v1/lan/resolve_conflict {decision_id, mode}`` —
  receiver-side resolution of adopt-origin / remote-conflict
  pending decisions. ``mode`` is ``use_theirs`` / ``keep_mine`` /
  ``dual_publish``.
- ``GET /v1/lan/pending`` — list pending UI decisions, for the
  settings-side "Decisions waiting (N)" surface and the picker
  "Receive a project from another phone (N waiting)" badge.

**New module** ``azt_collabd/pending_decisions.py`` — atomic
read/write of ``$AZT_HOME/pending_decisions.json``. Three kinds:
``share_offer``, ``adopt_origin``, ``remote_conflict``. Stable
``id`` per (kind, peer_id, langcode) so a re-sent offer doesn't
pile up duplicates.

**New module** ``azt_collabd/lan_clone.py`` — LAN-clone path.
Synchronous (LAN is local-network fast); the receiver's picker
gesture exits straight into the cloned project. Collision check
piggybacks on a single ``ls-remote`` round-trip, no full clone
for the decision.

**New client wrappers** ``lan_clone``, ``lan_pending``,
``lan_accept_offer``, ``lan_decline_offer``, ``lan_adopt_origin``,
``lan_resolve_conflict``. ``lan_pair_qr`` extended to accept
``langcode=`` so the QR payload carries the project + its
``remote_url`` (when set) — same scan does pair + share + clone.
``lan_share_project`` is now "share with notification"
(was bookkeeping-only) — fires the courtesy offer to the peer.

**UI restructure** (``azt_collab_client/ui/lan_popups.py``):

- ``share_project_popup(langcode)`` — settings-side three-section
  popup: paired phones list (per-row one-tap Share buttons),
  in-person QR section ("Show QR code"), github invite section
  ("Add permission by github username" → existing
  ``grant_collaborator_popup``). Replaces the old separate
  "Share repo QR" + "Grant collaborator access" buttons.
- ``scan_to_pair`` extended: dispatches on payload shape (pair-
  only / pair+langcode / unknown). Combined payloads run pair +
  clone + (inline) adopt-origin confirm in one gesture.
- ``pending_offers_popup`` — picker-side entry: shows pending
  share-offers from already-paired peers (Accept / Decline per
  row) plus a "Scan QR code" fallthrough for first-pair-with-a-
  new-phone.
- ``adopt_origin_popup`` — always-confirm prompt before
  ``origin`` registration. Reused for in-scan-flow and pending-
  decisions resolution surfaces.

**Settings page** (``azt_collabd/ui/app.py``):

- "Grant collaborator access" + "Share this project (QR)"
  buttons collapsed into one "Share {langcode} project" button
  that opens the consolidated popup. ``current_langcode_label``
  StringProperty drives the dynamic button label.
- Existing ``grant_collaborator`` method retained for callers
  (the share popup folds the same flow in via the github invite
  section).

**Picker** (``azt_collab_client/ui/picker.py``):

- "Pair with another phone" → "Receive a project from another
  phone." Tap now opens ``pending_offers_popup`` (shows pending
  offers + a scan fallback) rather than firing the scanner
  directly.
- Old "I have one on my phone" button kept in the KV tree but
  hidden via ``height: 0`` + ``opacity: 0`` + ``disabled: True``
  per the Kivy hide/show pattern (one prop flip to re-enable).

### Open follow-ups

- ``POST /v1/lan/sync_remotes`` bidirectional reconciliation: the
  recipient's response carries their view back so a single round-
  trip fixes both sides. Scheduled but not yet implemented —
  initial-pair exchange via the hello payload covers the common
  case for now; ongoing periodic sync is the gap.
- ``dual_publish`` mode in ``resolve_conflict`` currently just
  records the user's choice; the actual dual-push mechanism is
  follow-up work in the scheduler's fan-out.
- Live picker-badge count "(N waiting)" — first cut shows the
  same label regardless; pending offers appear in the
  ``pending_offers_popup`` once tapped. A live ``StringProperty``
  on the picker's button text is the polish-pass fix.

## 0.44.13 — positive cache for system-resolver hits (Starlink DNS round-trip elimination)

### Why

`azt_collabd/net.py` already had a DoH fallback (Cloudflare 1.1.1.1)
for *failed* system DNS — the "Cameroon" fix. But the fallback only
fires when ``socket.getaddrinfo`` raises ``gaierror``. On Starlink,
where system DNS works but each lookup is slow (satellite RTT +
distant resolver placement = multi-second per query), the fallback
never engages and we pay full DNS cost on every connection.

Counted against the field log: a typical drain through chunk-halving
(50 → 25 → 12 → 6 → 3 → 1, then chunk_n=1 retries) opens ~14
connections to ``github.com:443`` (2 per attempt: ``GET info/refs``
and ``POST git-receive-pack``), each preceded by a fresh system DNS
call. The periodic ``_has_internet()`` probe at
``sync.connectivity_poll_s`` (default 30 s) adds 2 more lookups per
tick. On a slow resolver every one of those is satellite-RTT.

### What landed

- ``azt_collabd/net.py``: ``_SYSTEM_CACHE`` dict + lock, 5-min TTL
  (matches DoH cache). Keyed on ``(host, port)``; populated on
  successful system-resolver returns; checked before calling
  ``_orig_getaddrinfo``. Hostname-shaped only (gated on
  ``_looks_like_hostname``) — numeric IPs / AF_UNIX paths fall
  through to the original ~O(1) path.
- New ``_RESOLVER_STATE`` value ``'system-cache'`` for cache hits.
  ``resolver_state()`` docstring updated.

### Impact

During a sync session, ``github.com`` resolves at most once per 5
min instead of ~14+ times per drain. Eliminates 13+ system-DNS
round-trips per drain pass on slow resolvers. Effect on the baf
tester's 408 pattern: TBD — DNS is one of several suspects
(server-side budget, dulwich-on-Android pump, TLS instability). If
the 408 was DNS-eating-the-budget, this fixes it; if not, we'll see
the 408s persist with the same shape and need to keep looking.

### Wire format

None. Pure internal optimisation; no new endpoints, no new
status codes, no MIN_CLIENT_VERSION bump.

## 0.44.12 — Phase A persistence gate + commit-time large-file flag + clearer bail message

### Why

0.44.11 shipped a single size-based gate (bail when the chunk_n=1
pack > 10 MB). Field logs from the baf tester on Starlink showed
the gate was the wrong shape: chunk_n=1 packs as small as 276 KB
still 408'd at GitHub's receive-pack endpoint in ~20–30 s,
consistently, indefinitely. The 10 MB gate never fires for a tiny
pack like that — so the daemon spun all the way to
MAX_CONSECUTIVE_FAILURES (12) on every drain, burning ~10 min per
cycle for nothing. And the "single commit too big for your
connection" wording in the bail message was actively misleading
on a fast pipe.

### What landed

- **Persistence gate.** ``_push_chunked_to_ref`` now tracks
  ``chunk_n_1_failures``. After the *second* chunk_n=1 failure
  (regardless of pack size), bail with
  ``S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET``. This catches the
  "server keeps rejecting our pushes for reasons we can't see"
  case the field showed.
- **Default size-gate budget lowered** from 10 MB to 3 MB
  (``sync.commit_pack_byte_budget``). The original 10 MB was
  calibrated on a guess; field data shows even ~7 MB packs 408
  reliably on observed connections, so 10 MB rarely fired even
  when it should have. 3 MB still leaves room for a healthy
  single-commit pack on a working pipe but trips faster on
  observably-bad ones. (The size gate fires on the *first*
  chunk_n=1 failure when bytes > budget; the persistence gate
  fires on the *second* failure regardless.)
- **Bail message rewritten.** The old wording said "single
  commit is too large for this connection" — wrong on Starlink
  where tiny packs also fail. New message: "Could not push to
  GitHub: the server kept rejecting our push attempts
  ({commit_sha}, {raw_bytes:,} bytes). May be a connection
  problem or a GitHub-side issue — try again later or on a
  different network." The ``Status`` params now carry
  ``reason='oversize'|'exhausted'`` so future UI / log
  consumers can differentiate.
- **Data-quality flag at commit time.** ``_commit_step_locked``
  now scans every just-made commit for files above
  ``data_quality.large_audio_byte_threshold`` (default 500 KB)
  and emits ``S.LARGE_AUDIO_FILE_DETECTED`` plus a
  ``[data-quality]`` stderr line per offender. The suite
  recorder is for word-list elicitation; multi-MB files almost
  always mean a phrase / text was recorded by mistake.
  Informational — doesn't block the commit; daemon log becomes
  the audit trail.
- **Helpers.** ``_check_large_files_in_commit(repo, commit_sha,
  threshold)`` walks ``dulwich.diff_tree.tree_changes`` for the
  new commit vs its first parent.
- **Classifier script.** ``tools/classify_pending.py`` — offline
  per-commit stats (files / total bytes / max-file) for an
  existing diverged range. Useful for triaging an already-stuck
  repo (run against a desktop clone with the full pending
  history).
- **Translations** for the new bail wording and
  ``LARGE_AUDIO_FILE_DETECTED`` (English + French).

### Wire format

Additive only — new status code ``LARGE_AUDIO_FILE_DETECTED``
and new ``reason`` param on ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET``.
Old peers fall through to the generic ``[CODE] params``
formatter or ignore the new param. No ``MIN_CLIENT_VERSION``
bump.

### What this doesn't fix

The actual cause of baf's stuck push remains unknown — the
server-side 408s at ~20–30 s on packs of any size, including
276 KB, on a fast Starlink pipe. The persistence gate just
gives up faster and tells the user something honest. Real fixes
require diagnosing whether dulwich-on-Android, GitHub's edge,
or something in between is the culprit (see "Next test"
discussion below in CHANGELOG conversation).

## 0.44.11 — Phase A pre-flight pack-size diagnostic + first-cut budget bail

### Why

Field log from a 0.44.10 tester (baf, ~150 MB / 424-commit backlog)
showed Phase A chunk-halving running all the way down to
``chunk_n=1`` and still getting HTTP 408 from
``git-receive-pack`` at ~29 s per attempt — i.e. the bytes that
need to cross the wire for a *single* commit don't fit inside the
server's per-request timeout on this connection. The architecture
(chunk-halving to fit pack size into per-request budget) bottoms
out at 1 commit per chunk; there's no smaller unit to fall back
to. Before 0.44.11 this just spun on ``MAX_CONSECUTIVE_FAILURES``
with no indication to the user that the problem isn't going to
fix itself by waiting.

This release added a pre-flight pack-size estimate to every Phase A
attempt (traced) and a typed
``S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` bail at chunk_n=1 when
the single-commit pack exceeded the per-attempt budget
(initially 10 MB). See 0.44.12 for the follow-up persistence
gate + budget retune after field evidence showed the size-only
gate was insufficient.

### What landed

- ``azt_collabd/repo.py``: ``_estimate_delta_size(repo, have_sha,
  want_sha)`` — walks ``dulwich.object_store.MissingObjectFinder``
  and sums ``raw_length()`` over the missing-objects set. One
  call per Phase A attempt, traced as
  ``[sync-trace] topic-push pack-size: N objects, X bytes``.
- ``_push_chunked_to_ref`` adds a size-only bail at chunk_n=1
  when ``raw_bytes > commit_pack_byte_budget()``.
- ``_push_step_locked`` handles the new status code alongside
  ``TOPIC_BRANCH_CONFLICT``.
- ``azt_collabd/settings.py``: new ``commit_pack_byte_budget()``
  (initially 10 MB; retuned to 3 MB in 0.44.12).
- ``azt_collabd/status.py`` + ``azt_collab_client/status.py``:
  ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` mirrored.
- ``azt_collab_client/translate.py``: initial bail message
  (replaced in 0.44.12 once field evidence showed the
  "too large for this connection" wording was wrong), plus the
  previously-missing entry for ``TOPIC_BRANCH_CONFLICT``.
- French translations.

### Wire format

Additive status code. No ``MIN_CLIENT_VERSION`` bump.

## 0.44.10 — topic-branch Phase D + startup janitor: clean up merged topic-branches

### Why

After 0.44.9 (Phases A + B + C), the topic-branch
`azt-pending-<lang>-<device>` is left on the server even after
the merge commit lands on `main`. Cosmetic clutter, not a
functional problem — but accumulates over time, and a few
versions of 0.44.x already left orphans on test repos. Phase D
deletes the ref on the success path; the janitor cleans up
stragglers (Phase D delete that didn't fire, daemon kill right
after Phase C, etc.).

### Phase D — delete topic-branch on Phase C success

After `S.PUSHED` is added in Phase C's success branch, call
`_delete_remote_topic_branch(repo, remote_url, username,
token, topic_ref_name)` which pushes a delete refspec
(`':refs/heads/<topic>'`) and drops the local mirror. Failure
is non-fatal; logged but doesn't block the sync result. Worst
case: orphan ref on the server, picked up by the janitor on the
next startup.

### Janitor — once-per-project-per-daemon-lifetime sweep

`_maybe_run_janitor(repo, project_dir, username, token,
remote_url, branch)` runs right after the fetch in
`_push_step_locked`. Memoized in `_JANITOR_SWEPT_PROJECTS` so
the sweep network cost is one-time per (project, daemon
lifetime); in steady state (Phase D ran on the last success)
the sweep finds zero refs and is essentially free.

Conservative scope:

- **Only our own device's refs.** Suffix match on the
  sanitised device_name. Other devices' orphans stay; their
  owning device's next sync sweeps them. Refusing to touch
  anyone else's ref avoids false-positive deletes — if Device
  B is mid-Phase-A from a checkpoint we don't know about,
  deleting their topic-branch would force them to restart.
- **Only merged-into-main refs.** Reachability from
  `refs/remotes/origin/<branch>` is git's safety contract for
  "every commit on this ref is also reachable from main, so
  dropping it loses nothing." The check uses our existing
  `_is_ancestor(topic_tip, main_tip)`.

### Files touched

- `azt_collabd/repo.py`:
  - `_delete_remote_topic_branch(...)` helper — Phase D's
    delete primitive.
  - `_janitor_sweep_topic_branches(...)` — the actual sweep
    loop over our own refs.
  - `_maybe_run_janitor(...)` — idempotent wrapper memoized
    by project_dir.
  - `_JANITOR_SWEPT_PROJECTS` module-level set.
  - Wired `_delete_remote_topic_branch` into Phase C's
    success branch.
  - Wired `_maybe_run_janitor` into `_push_step_locked` right
    after `remote_sha` is read (so the janitor has a valid
    main tip to validate ancestry against).

### Field-tester migration path (unchanged)

Same flow as 0.44.9 — Phase A chunks, Phase B re-checks, Phase
C promotes. Now Phase D also deletes the topic-branch on
success, and any orphan from earlier 0.44.x test builds gets
swept on the next daemon spawn (visible in the log as
`[sync-trace] janitor: sweeping N merged topic-branch(es)`).

## 0.44.9 — topic-branch Phases B + C: explicit re-fetch / re-merge / promote loop

### Why

0.44.8 shipped Phase A (chunked push to a per-device topic-branch)
and relied on the existing direct-push loop to handle Phase B
(re-fetch + conditional re-merge) and Phase C (promote merge
commit to main) implicitly. That works for the common no-race
case and for one bounded race (where the existing post-non-FF
retry re-merges once inside the direct-push loop), but has a
hole: if a re-merge inside the direct-push loop produces another
non-FF state, chunk-halving spins on `DivergedBranches`. Also
the implicit path doesn't re-enter Phase A after a re-merge —
it just retries the direct push — so a re-merge during a flaky
window could leave the merge stuck.

This release adds explicit Phase B and Phase C as a bounded
loop, replacing the "fall through to direct-push" behaviour
after a successful Phase A.

### Phase B — re-fetch + conditional re-merge

After Phase A returns success:

1. Re-fetch `origin` (auth errors short-circuit; transient
   network failures continue with stale local mirror).
2. Read the local mirror of `refs/remotes/origin/<branch>`.
3. If unchanged from when we started the run → skip to Phase C.
4. If changed:
   - **New remote is reachable from our `local_sha`**: our
     existing merge already includes the remote's new tip; no
     re-merge needed. Proceed to Phase C.
   - **Our `local_sha` is reachable from the new remote**:
     remote advanced past us during Phase A (e.g., someone else
     merged our changes upstream). Fast-forward local; surface
     `S.PULLED`; return — nothing to push.
   - **Diverged again**: re-run `_merge_diverged` against the
     new remote tip. Memory pre-flight check
     (`_check_memory_for_merge`) runs first; refuses with
     `S.INSUFFICIENT_MEMORY_FOR_MERGE` if low. New merge commit
     becomes `local_sha`. Conflicts surface as before.

### Phase C — promote merge commit to main

After Phase B leaves us with the right `local_sha`:

1. Set `refs/heads/<branch>` to `local_sha` defensively.
2. `porcelain.push(remote_url, '<branch>:<branch>')`.
   Pack negotiation sees the topic-branch ref Phase A
   populated + `main` + any other server refs; excludes every
   reachable object. The pack contains only the merge commit
   + tree + merged LIFT bytes (a few MB) and completes inside
   the server's per-request timeout.
3. On success: advance local mirror of `refs/remotes/origin/<branch>`,
   clear stale chunk_n hints, emit `S.PUSHED`, return.
4. On `_is_non_ff_rejection`: main moved between Phase B and
   Phase C (sub-second race window). Loop back to Phase B —
   re-fetch, re-evaluate, re-push.
5. On 401 / 403: surface auth-related status, return.
6. On other (network / server transient): emit `S.PUSH_FAILED`;
   next drain re-runs Phase A (which short-circuits on
   already-uploaded objects via the server's topic-branch ref)
   then Phase B + C.

### Bounded loop

Phase B + Phase C run in a loop with `MAX_PROMOTE_RETRIES = 5`
iterations. If main keeps moving under us through every retry
(extremely hot race window), we bail with `S.PUSH_FAILED`;
next drain tries again. The Phase A short-circuit on resume
means cost-of-retry across drains is small.

### Files touched

- `azt_collabd/repo.py`: replaced the "fall through to existing
  direct-push loop" comment-and-trace after Phase A success
  with the explicit Phase B + C loop. ~150 lines of new code
  inside `_push_step_locked`. The existing direct-push loop
  below is unchanged and remains the path for pure-FF cases
  (the `can_direct_push == True` branch).

### Field-tester migration path (unchanged from 0.44.8)

Same as 0.44.8 — Phase A pushes diverged history to
`azt-pending-baf-<device>` in chunks, Phase B detects whether
main moved (won't have, on the tester's repo), Phase C pushes
the existing `c115b64c` merge commit to main as a tiny pack.
Difference vs. 0.44.8: if main *did* move during Phase A's
multi-minute upload, this release handles it cleanly with the
explicit B + C loop instead of relying on the direct-push
loop's after-the-fact handling.

## 0.44.8 — topic-branch Phase A: chunked upload of diverged history

### Why

0.44.7 added the routing decision; this release acts on it. When
`_all_commits_descend_from(remote, local)` returns False — the
typical post-merge state where the local-side parent chain of the
merge commit doesn't descend from the current remote — direct push
+ chunk-halving can't help (every intermediate the chunk picker
selects gets `DivergedBranches` from the server). Phase A
sidesteps the topology problem by pushing diverged commits to a
per-device topic-branch first; the audio blobs land in 5–20 MB
chunks that fit inside GitHub's per-request timeout. Once all
chunks are on the server (under the topic-branch ref), the
existing direct-push loop runs immediately after and pushes the
merge commit to `main` — pack negotiation excludes everything
already on the server (via any ref, including topic-branch), so
the main push contains only the merge commit + tree + merged
LIFT bytes. That pack completes in seconds.

### What landed

New helpers in `azt_collabd/repo.py`:

- `_topic_branch_name(langcode, device_name)` — returns
  `azt-pending-<sanitized-lang>-<sanitized-device>`. Per-device
  naming so two devices syncing the same project simultaneously
  don't clobber each other's topic ref; per-project so one device
  working on multiple LIFT projects keeps them separate.
- `_push_chunked_to_ref(repo, project_dir, username, token,
  remote_url, target_sha, topic_ref_name, branch_for_main)` —
  adaptive chunked push to the topic-branch. Reads
  `refs/remotes/origin/<topic_ref_name>` (populated by the
  earlier fetch) for the server-side tip and resumes from there
  if a prior attempt got partway. Refuses with
  `S.TOPIC_BRANCH_CONFLICT` if the server's topic-branch tip
  isn't an ancestor of our target (another device using the same
  device_name). Reuses the existing chunk-halving heuristic on
  per-chunk network failure. No DivergedBranches handling needed
  — topic-branch is ours alone by naming convention.

New status codes:

- `S.TOPIC_BRANCH_CONFLICT` (`azt_collabd/status.py` +
  `azt_collab_client/status.py` mirror). Params:
  `topic_branch`, `server_tip`. User remedy: change device_name
  to something unique in the daemon settings UI.

New settings knob:

- `sync.topic_branch_chunk_size` (default 50) — initial chunk_n
  for Phase A. Lower for slower networks. Halves adaptively on
  per-chunk failure.

Wiring in `_push_step_locked`:

- After the merge handling, when the routing decision says
  topic-branch, invoke `_push_chunked_to_ref` with
  `target_sha=local_sha`. On success, fall through to the
  existing direct-push loop — that push of the merge commit to
  main now negotiates a tiny pack since the server already has
  every reachable object.
- On `S.TOPIC_BRANCH_CONFLICT`: surface the status + add
  `PUSH_FAILED` for peer routing; return.
- On consecutive-failures-cap exit: surface
  `S.SYNC_GIVING_UP_TRANSIENT` + `S.PUSH_FAILED`; next drain
  cycle re-reads the server's topic-branch tip and resumes.

### How resume works (no on-disk state)

The server's topic-branch ref IS the progress record. Each
successful chunk push lands on the server; on the next drain the
existing fetch repopulates `refs/remotes/origin/<topic>` and
`_push_chunked_to_ref` picks up where the last run left off
without any local state file. If the daemon was killed
mid-chunk-push, the chunk that was in flight either landed (and
the server now has it) or didn't (no partial commit, since git's
push is atomic per refspec). Either way, the next drain reads the
authoritative server-side tip and walks forward from there.

### Migration / current stuck tester

The stuck baf tester (424 commits ahead of remote, ~150 MB pack
timing out at GitHub's ~7-minute per-request ceiling) should
unstick on next sync after installing 0.44.8:

1. Routing detects non-FF (the 422 pre-merge commits don't
   descend from current remote `main`) → topic-branch route.
2. Phase A pushes the merge commit `c115b64c` to
   `azt-pending-baf-<device>` in chunks of ~50 commits each.
   Each chunk uploads ~10–25 MB. Wall-clock for the full
   sequence: tens of minutes on a slow network, but each
   individual upload survives the per-request timeout.
3. Existing direct-push runs after Phase A returns. Pushes
   `c115b64c` to `refs/heads/main`. Pack contains only the merge
   commit + tree (no audio blobs — those are now reachable via
   the topic-branch ref). Completes in seconds.

The merge commit they already produced (with 1700
azt-lift-conflict annotations) lands on main unchanged. Topic
branch stays on the server temporarily; janitor (step 5,
deferred to a later release) cleans it up.

### What's not in this release

Per the spec (step 4 of "Implementation order"):

- **Phase B** (re-fetch + conditional re-merge if `main` moved
  during the long Phase A upload) — for now handled implicitly
  by the existing post-non-FF retry inside the direct-push loop.
  Race window: if `main` moved during Phase A and the post-Phase
  direct push gets `DivergedBranches`, the existing code does a
  re-merge inside the direct-push loop. Works but doesn't
  re-enter Phase A if the re-merge produces another non-FF
  state.
- **Phase D** (cleanup — delete topic-branch ref via dulwich's
  delete-refspec push). Leaving the topic ref on the server is
  cosmetic clutter, not functional. Janitor will sweep these on
  a future release.
- **Step 6** UI status emission (`S.UPLOADING_IN_PIECES` with
  per-chunk progress). Phase A's progress is visible in
  `[sync-trace] topic-push attempt …` lines in the daemon log
  for now.

### Files touched

- `azt_collabd/repo.py`: `_topic_branch_name`,
  `_push_chunked_to_ref`, routing wiring in `_push_step_locked`.
- `azt_collabd/settings.py`: `topic_branch_chunk_size()`.
- `azt_collabd/status.py` + `azt_collab_client/status.py`:
  `TOPIC_BRANCH_CONFLICT`.

## 0.44.7 — push-routing diagnostic (step 1 of topic-branch push)

### Why

Field user 2026-05-21 is stuck pushing a 424-commit post-merge state to GitHub: the merge commit is one atomic git object, the 681 new audio blobs referenced by the merge can only travel as one ~150 MB pack, and GitHub's `git-receive-pack` is timing out at ~7 minutes before the upload completes. Chunk-halving against `main` can't help because every intermediate the chunk picker selects (one of the ~422 pre-merge commits on the local-side parent chain of the merge commit) doesn't descend from the current remote `main` — `DivergedBranches` at every smaller chunk.

The proper architectural fix is to push diverged commits to a topic branch first (the audio blobs land there in small chunks, each upload survives GitHub's per-request timeout), then promote the existing merge commit to `main` as a tiny pack since all blobs are already on the server. The local LIFT-aware merge stays on the device, unchanged.

That implementation lands in stages. This release is **step 1**: the routing decision, observable in the trace but not yet acted on.

### What changed

New helper `_all_commits_descend_from(repo, ancestor_sha, descendant_sha)` in `azt_collabd/repo.py`. O(N) one-pass walk of the delta between ancestor and descendant; returns True iff every commit in the delta has the ancestor as one of its ancestors. False if the delta touches a third branch (typical: post-merge state where the local-side parent chain of the merge commit doesn't descend from the current remote).

New trace line in `_push_step_locked` just before the existing push loop:

```
[sync-trace] route: direct-push (diagnostic — current build still takes direct path)
[sync-trace] route: topic-branch (diagnostic — current build still takes direct path)
```

No behavior change. The existing direct-push + chunk-halving path runs for every push as before. The trace lets us verify on real field data that the routing rule correctly identifies non-FF states (the stuck tester's next sync should log `topic-branch`) before Phase A is added in the next release.

### Files touched

- `azt_collabd/repo.py`: added `_all_commits_descend_from` next to `_is_ancestor`; added the diagnostic trace at the entry to the push loop.

### What ships next (step 2+)

Tracked in the topic-branch spec laid out in conversation. Phase A (chunked push to `refs/heads/azt-pending-<lang>-<device>`) is the next code drop. Then Phase B (re-fetch + conditional re-merge), Phase C (promote merge commit to `main` — tiny pack since blobs already on server), Phase D (cleanup). Sync-state persistence in `$AZT_HOME/sync_state.json` for resume across daemon respawns.

## 0.44.6 — low-memory device politeness pass: three deferred OOM headroom fixes

### Why

The 0.44.4 audit (`project_oom_followups_after_0.44.4.md`) found
three sibling OOM-prone patterns beyond the `_walk_tree` /
`_merge_diverged` fix that already shipped in 0.44.4. None were
on the critical path of the field user's stuck-merge symptom, so
they were deferred to this release. Each closes a small amount
of unnecessary RAM pressure that becomes painful on tight-heap
Android devices when stacked with other concurrent work.

### Fix 1 — `atomic_recovery._reconcile_orphan` gates on memory

`azt_collabd/atomic_recovery.py:_recover_under_lock` calls
`lift_merge.three_way_merge` to reconcile orphan-pending LIFT
files. Same ~150 MB peak as `_merge_diverged`. It runs from
`reconcile_on_startup` — exactly when memory may be tight (the
daemon process is just spawning, competing with the picker
activity for heap). Silent OOM-kill there would lose the entire
recovery batch with no signal.

Imports `_check_memory_for_merge` from `.repo` and refuses the
merge when free memory is below `sync.min_free_mem_mb_for_merge`.
On refusal:

- Orphan stays on disk (still valid; the file is byte-complete)
- New `summary['skipped_low_memory']` counter increments
- Returns from `_recover_under_lock` early — if we can't fit
  this merge, we won't fit the next either, no point burning
  time on a doomed loop

Next startup with more memory available runs the recovery.

### Fix 2 — `_h_atomic_commit` streams SHA-256 + byte count

`azt_collabd/server.py:_h_atomic_commit` was reading the entire
pending audio file (3–10 MB) into a Python bytes object just to
compute `len(data)` and `hashlib.sha256(data).hexdigest()` for
the `ATOMIC_COMMITTED` response. Replaced with the canonical
streaming-hash pattern:

```python
h = hashlib.sha256()
bytes_written = 0
with open(pending_real, 'rb') as f:
    for chunk in iter(lambda: f.read(64 * 1024), b''):
        h.update(chunk)
        bytes_written += len(chunk)
```

Peak heap: 64 KB instead of file size. Minor on its own, but
stacks badly when concurrent with a LIFT merge or push pack-
build — those can already push the heap close to its cap on
low-end devices.

### Fix 3 — loopback HTTP body cap

`azt_collabd/server.py:_read_json` was reading the full
`Content-Length` from `rfile` with no upper bound. Desktop-
only (Android peers use ContentProvider), so not Android-OOM-
relevant, but a buggy peer / test harness that sends a wrong
`Content-Length` would let the daemon allocate gigabytes
before the JSON parse failed. Added a 64 MB cap — far past
any legitimate body on this path (largest legit is a credential
blob, <1 KB) — that returns a quick refusal instead.

### Files touched

- `azt_collabd/atomic_recovery.py`: imported
  `_check_memory_for_merge` from `.repo`; added pre-flight at
  the `three_way_merge` call site; new `skipped_low_memory`
  counter on `summary`.
- `azt_collabd/server.py`: streaming hash in
  `_h_atomic_commit`; 64 MB `_MAX_BODY_BYTES` cap in
  `_read_json`.

### Not yet wired (intentional)

Defense-in-depth equivalents on the loopback transport for
other large-body endpoints (e.g. credential upload) — those
already inherit the new `_read_json` cap since they share the
helper. The 64 MB cap can be tightened later if any legitimate
path approaches it; current ceiling is the credential blob at
under 1 KB, so headroom is ~64,000× what's actually needed.

## 0.44.5 — self-update no longer locks out users with crowded Downloads folders

### Why

Field user 2026-05-21: in-app Update failed with `Install failed:
JVM exception occurred: failed to build unique file:
/storage/.../aztcollab.apk`. The user's Downloads folder had
accumulated 30+ orphan `aztcollab.apk` / `aztcollab (1).apk` /
… `aztcollab (32).apk` entries from prior install attempts,
hitting Android's `MediaProvider.buildUniqueFile()` retry cap.
Once the cap is hit there's no recovery from the Update button
— the user's only option was to open the Files app and manually
delete all those orphans.

### Fix — `azt_collab_client/ui/update.py:_media_store_uri`

Two layers of defence:

1. **Pre-insert cleanup.** New `_clear_prior_downloads()` queries
   MediaStore Downloads for rows whose `_display_name` matches
   the asset name (`aztcollab.apk`) and that this app owns, then
   deletes each. Scoped-storage permissions limit us to our own
   rows — which is exactly what we want, since the orphans are
   from our own prior install attempts. Other apps' files of the
   same name stay put.

2. **Timestamped fallback.** If the canonical-name insert still
   fails (other apps own enough same-named files to push us over
   the cap, or the scoped-storage delete refused), retry once
   with a UTC-timestamped display name
   (`aztcollab-20260521-145922.apk`). The install Intent only
   needs the `content://` URI; the system installer reads the
   APK manifest for the user-facing app identity, so the
   timestamped filename never appears in any UI.

Logs `[update] cleared N prior MediaStore Downloads row(s)
named X` when cleanup happens; logs the fallback case explicitly
when the timestamped name is used.

### Manual recovery for users already locked out

Users who are stuck on the pre-0.44.5 build can self-recover
without any rebuild:

1. Open the Files app
2. Navigate to Internal storage → Download
3. Delete every `aztcollab.apk` / `aztcollab (N).apk`
4. Re-tap Update

Then they pick up 0.44.5+ via the in-app updater and the issue
doesn't recur.

### Files touched

- `azt_collab_client/ui/update.py`: added
  `_clear_prior_downloads`; refactored `_media_store_uri` to
  pre-clear + retry with timestamped name.

## 0.44.4 — `_merge_diverged` no longer slurps every audio file into Python (OOM-kill on merge fixed); pre-flight memory check

### Why

Field log baf 2026-05-21 15:45 → 15:52 (after 0.44.3 was
installed): every drain cycle did the right thing — fetched,
saw remote had moved to `7a0e17`, logged `merge_diverged
begin` — and then the daemon process died silently at 49 s,
65 s, 127 s mid-merge with no traceback. The shrinking
intervals (127 → 65 → 49) were the giveaway: heap-fragmentation
accelerating until the OOM-killer caught a smaller spike. The
respawn boot logs never showed `reconcile_on_startup:
interrupted=N` either, because nothing in the merge path was
job-tracked.

Root cause: `_walk_tree` in `azt_collabd/repo.py` returned
`path → blob.data (bytes)` for **every file** in a commit tree.
`_merge_diverged` called it three times (base, head, remote) at
the top of the function. For a 1700-entry baf project with
audio per entry, each side pulled ~200 MB of audio bytes into a
Python dict, totalling ~600 MB peak before the LIFT XML parse
even started. Android's `:provider` service heap cap (~256–512
MB depending on device) couldn't fit that, so the process was
OOM-killed before any merge work happened. Same bug existed in
the fast-forward path (`_apply_tree_to_workdir`) — that's now
fixed by the same change.

### Fix — SHA-only tree walk, lazy byte loading

- `_walk_tree` returns `dict[path → blob sha (bytes)]` instead of
  `dict[path → blob.data]`. ~tens of KB per snapshot regardless
  of audio file count, vs hundreds of MB before.
- New `_blob_bytes(repo, sha)` helper resolves a single blob to
  bytes on demand.
- `_merge_diverged` compares by SHA (git's invariant: identical
  content → identical SHA), loads bytes only when:
  - About to call `lift_merge.three_way_merge` on a `.lift`
    file (then `del b_bytes, o_bytes, t_bytes` immediately after
    the merge returns so the peak is "one LIFT merge worth",
    not "every file in every snapshot")
  - About to write a path whose target SHA differs from the
    HEAD SHA (skips the no-op rewrite of files whose content
    didn't change — most of the working tree)
- `_apply_tree_to_workdir` (fast-forward path) gets the same
  treatment.

Peak memory budget after the change:

- 3 × `_walk_tree` SHA dicts: ~150 KB
- LIFT XML parse + merge state (the actual cost): ~100–150 MB
- One blob read at a time during apply: max ~1 MB (largest
  audio file)
- **Total: ~150–200 MB**, comfortably under Android's heap cap.

### Pre-flight memory check (defensive)

Even with the slurp gone, the LIFT XML parse + merge step still
wants ~150 MB peak. On a device where another app is hogging
RAM the merge could still OOM. Added a pre-flight check that
reads `MemAvailable` from `/proc/meminfo` and refuses the merge
if it's below `sync.min_free_mem_mb_for_merge` (default 200
MB). Returns a typed `S.INSUFFICIENT_MEMORY_FOR_MERGE` with
`mem_available_mb` and `min_required_mb` params; the next drain
cycle re-reads memory and proceeds when it recovers. Beats the
silent OOM-kill: the user (and the log) get a clear "not
enough memory right now, will retry" instead of a process
disappearance.

All three `_merge_diverged` call sites are gated:
- `sync_repo` needs_merge path
- Forced-merge after non-FF-no-progress
- Retry-merge after race-on-fetch

On non-Linux platforms `/proc/meminfo` isn't readable; the check
treats that as passing (returns None). Desktop / sandbox doesn't
OOM-kill the way Android's `:provider` does.

### New tracing

`_merge_diverged` now emits `[merge-trace] _walk_tree done
base=N head=N remote=N`, then `[merge-trace] resolution done
writes=N deletes=N conflicts=N`, then `[merge-trace] apply done
writes_done=N deletes=N`. The pre-0.44.4 log shape was just
`merge_diverged begin` followed by silence — impossible to tell
whether the death was during walk, resolution, or write.

### Files touched

- `azt_collabd/repo.py`: rewrote `_walk_tree`, added
  `_blob_bytes`, `_mem_available_mb`, `_check_memory_for_merge`;
  refactored `_merge_diverged` body around (kind, value) tuples
  in `merged_writes`; gated all three `_merge_diverged` call
  sites on the memory check.
- `azt_collabd/status.py` + `azt_collab_client/status.py`:
  added `INSUFFICIENT_MEMORY_FOR_MERGE` (with mirrored comment
  on the client side documenting the routing contract).
- `azt_collab_client/translate.py`: translation for the new
  code with `{mem_available_mb}` / `{min_required_mb}` params.
- `azt_collabd/settings.py`: `min_free_mem_mb_for_merge()`
  knob, default 200 MB.
- `azt_collab_client/CLIENT_INTEGRATION.md` §17: new row in
  the routing table (silent in auto-sync, translated toast in
  user-initiated sync), the silence list in the auto-commit
  example and the transient-toast branch in the
  user-initiated example both updated, constants list extended.

### Tuning notes

`sync.min_free_mem_mb_for_merge` is conservatively set at 200
MB. If a device is consistently triggering the pre-flight
refusal but the merge would actually succeed (post-fix it
should fit in ~150 MB), drop to 150 via
`AZT_HOME/config.json`. Setting to 0 disables the check
entirely.

## 0.44.3 — persisted chunk_n hint now uses the smallest attempted, not the last (in-call revert no longer wipes progress)

### Why

0.44.2 introduced cross-drain hint persistence but the within-call
"revert to full local tip" path defeated it. Field log baf 2026-
05-21 14:31 → 14:58 showed the loop:

```
14:45:22 resuming with hint chunk_n=211     ← good, hint worked
14:45:23 push raised: DivergedBranches at 211
14:45:25 non-FF — reverting to full local tip
14:45:25 push attempt chunk_n=422           ← back to full
14:58:15 408 timeout at chunk_n=422
14:58:15 remembered chunk_n=422             ← LAST chunk_n, not smallest
         next drain cycle will start at 211 ← back where we began
```

Result: indefinite loop at hint=211, since the budget always
caught the post-revert full attempt and persisted that value.
Net progress across cycles: zero.

### Fix

Track `smallest_attempted_n` across the entire `_push_step_locked`
call. On budget-exceeded or consecutive-failures-cap exit, persist
**that** value instead of the current `chunk_n`. Each cycle then
halves the actual floor, not the post-revert ceiling.

Expected behavior for the stuck device:

```
Cycle 1: try 422 → budget → smallest=422 → store 211
Cycle 2: hint=211 → try 211 → DivergedBranches → revert to 422 →
         budget at 422 → smallest=211 → store 105
Cycle 3: hint=105 → try 105 → … → smallest=105 → store 52
… halves about every drain cycle (5-15 min each) until a chunk
  fits the network window.
```

Each cycle makes real progress now, regardless of whether the
within-call revert kicks in.

### Files

- `azt_collabd/repo.py` — added `smallest_attempted_n` tracking;
  used in both budget-exceeded and consecutive-failures-cap
  persist sites.

### What still doesn't fix itself

The within-call "non-FF with no remote movement — reverting to
full local tip" escalation still re-enlarges chunk_n inside a
single call, burning the rest of the budget on a doomed full-tip
attempt. That's wasted time per cycle but no longer wasted
progress across cycles. A future fix could cap the revert at
`smallest_attempted_n` instead of going all the way to `local_sha`,
but that touches the diagnostic-escalation contract more
substantially — defer until we see whether the across-call
convergence alone is enough.

## 0.44.2 — push loop remembers failed `chunk_n` across drain cycles so backlogs converge on slow networks

### Why

Field log baf 2026-05-21 09:38 → 11:11 (daemon 0.44.0): device
had 419 unpushed commits accumulated over ~24 hours of flaky
connectivity. Each scheduler drain cycle:

1. `push attempt target=966b6714 chunk_n=419 consecutive_failures=0`
2. ~12 minutes later: `POST .../git-receive-pack 408` (GitHub server
   gave up on the slow upload)
3. `push budget exceeded (300s) — giving up; pending commits
   requeued for next sync`
4. 30 s later: drain cycle fires again, **starts at chunk_n=419**
5. Loop repeats indefinitely; backlog grows; backlog never drains.

The 0.43.22 chunk-halving adaptive loop *does* exist — it would
bisect 419 → 209 → … → 1 inside a single call until it finds a
size the network can sustain. But each single call typically can
only do one push attempt before the 300 s budget cuts it off, and
the next call has no memory of what just failed. Every drain
cycle wastes 5–13 minutes on a chunk_n the previous cycle already
proved was too big.

(See the 0.43.18 session at 12:46–13:21 in the same log: chunk
halving 302 → 151 → 75 → 37 → 18 → 9 → 4 → 2 → 1 worked when
given uninterrupted time. The budget — necessary to free the
project lock for other clients — defeated it once it was added.)

### What

In-process `_LAST_FAILED_CHUNK_N` dict in `azt_collabd/repo.py`
keyed by `project_dir`. Three touch points:

1. **Loop preamble:** if the dict has a hint for this project, use
   `_pick_intermediate_sha(repo, remote_sha, local_sha, hint)` as
   the initial `target_sha` instead of `local_sha`. Logged as
   `[sync-trace] resuming with hint chunk_n=N`.
2. **Budget exceeded / consecutive_failures cap:** before
   returning `SYNC_GIVING_UP_TRANSIENT` / `PUSH_FAILED`, store
   `max(1, chunk_n // 2)` for this project. Next drain cycle
   picks it up via (1).
3. **Full successful push (`PUSHED`):** clear the entry. The
   network just demonstrated it can handle the current size; no
   constraint to carry forward.

Net effect for the user's stuck device:

- Cycle 1: start at chunk_n=419, budget expires at chunk_n=419 →
  store hint=209
- Cycle 2: start at chunk_n=209, budget expires → store hint=104
- Cycle 3: start at chunk_n=104, budget expires → store hint=52
- … converges on a chunk_n the network can sustain inside 300 s
- Once a push at the converged size succeeds, the loop continues
  with `working_batch_n` locked in and drains the queue.

Across daemon restarts the dict resets — that's fine, the next
cycle is at most one 300 s budget worse off than otherwise.

### Files

- `azt_collabd/repo.py` — `_LAST_FAILED_CHUNK_N` dict;
  `_hint_chunk_n`, `_remember_failed_chunk_n`,
  `_clear_failed_chunk_n` helpers; three integration points in
  `_push_step_locked`.

### Recovery for currently-stuck field user (0.43.20 with 419-commit backlog)

Their 0.43.20 has the un-budgeted halving loop (worked over ~35
minutes per push in the 0.43.18 log) and 0.44.0/0.44.1 has the
budgeted-but-non-adaptive loop (never converges). 0.44.2 is the
first version that both bounds individual attempts AND converges
across drain cycles.

```bash
adb install -r path/to/0.44.2.apk
# tap AZT Collaboration once (Activity-side bundle refresh)
# then leave the device on a reliable network for an hour
```

The drain loop will work its way down to a working chunk_n over
the course of a few cycles (5–15 minutes per cycle); once it
finds one, it locks in and drains the 419 commits over the next
~30 minutes. No user action required between install and "drained."

## 0.44.1 — service-side bundle re-extract was extracting the WRONG asset; replace with marker invalidation

### Why

0.44.0's `_extract_bundle_from_apk` extracted
`assets/private.tar.gz` into `_python_bundle/`. But
`private.tar.gz` contains **app code** (`main.py`, `service.py`),
not the Python bundle. The Python bundle (`stdlib.zip`,
`modules/`, `site-packages/`) ships in
`lib/<abi>/libpybundle.so`, which p4a's `PythonUtil.unpackPyBundle`
extracts with prefix `"pybundle"` stripped to produce
`_python_bundle/`.

End result of the wrong extract: `_python_bundle/` was
structurally present (so bootstrap.c said "exists" and
proceeded) but functionally empty — no `stdlib.zip`, no
`modules/`, no `site-packages/`. Python's interpreter init
succeeded at the C level, but `_bridge_stdio_to_logcat` failed
to import anything (no Python stdlib reachable), prints went to
`/dev/null`, process died silently. **Exactly the silent
:provider death symptom this whole 0.43.22–0.44.0 chain was
supposed to fix.** We've been chasing the wrong root cause
since 0.43.22 — every "stale unpack" fix made things worse by
overwriting `_python_bundle/` with the wrong contents.

Field log 22:59:02 shows the loop cleanly:
1. :provider boots from a pre-swap bundle → `[boot-trace-daemon]`
   lines fire, `_maybe_reextract_python_bundle` detects stale
2. `_extract_bundle_from_apk` extracts `private.tar.gz` (app
   code) into `_python_bundle.new/`, swaps, exits
3. Next :provider spawn loads from the corrupted bundle →
   silent death → cascade-kill of peer

### Fix

`_maybe_reextract_python_bundle` no longer extracts. On stale
mtime detection it:

1. **Deletes `files/app/private.version` and
   `files/app/libpybundle.version`** — the markers
   `PythonUtil.unpackAsset` and `unpackPyBundle` use to skip
   re-extract. With them gone, the next picker Activity launch
   triggers a proper re-extract.
2. **Logs a clear warning** that the bundle is stale and the
   user should open AZT Collaboration to refresh.
3. **Stamps our own marker forward** so we don't repeat the
   invalidation on every spawn.
4. **Does NOT touch `_python_bundle/`** — daemon continues with
   the existing (possibly stale) code until the picker
   refreshes things.

`_extract_bundle_from_apk` is kept in the file as a deprecated
stub with a long comment explaining why it was wrong; it has
no callers.

### Trade-off

The "peer opens before picker after `install -r`" case now
runs the daemon on stale code until the user opens AZT
Collaboration. This is **the pre-0.43.22 default behavior**.
Worse than a hypothetical "perfect" recovery, but vastly better
than a silently-corrupted bundle.

Recovery path for `install -r` is now:

1. `adb install -r bin/aztcollab.apk` (or sideload)
2. **Tap AZT Collaboration once** before opening the peer.
   PythonActivity sees the version mismatch (which
   `private_version` advances per build), runs
   `recursiveDelete(files/app/)` + extract → fresh bundle.
3. Open the peer. `:provider` lazy-spawns from the now-fresh
   bundle.

If the peer is opened first by mistake:
- `:provider` runs on stale code (functional, just not the new
  version)
- The new service.py (once loaded) will detect the mtime
  mismatch on FIRST spawn and invalidate the markers
- Next picker launch then triggers the proper extract

### Recovery for currently-stuck device

If you have the 0.44.0 broken bundle on disk:

1. Tap AZT Collaboration on the device. PythonActivity should
   detect the mismatch and run a proper extract.
2. If that fails (which would mean PythonActivity's own path
   is also broken), `adb install -r 0.44.1.apk` first, then
   tap AZT Collaboration.

### Files

- `server_apk/service.py` — `_maybe_reextract_python_bundle`
  now invalidates markers instead of extracting;
  `_extract_bundle_from_apk` deprecated with explanatory
  comment.

## 0.44.0 — stuck-bundle recovery milestone: bz2 fix + Java recovery hatch + boot diagnostic

Consolidation release. Public 0.43.27 shipped with a latent
self-perpetuating broken state (the bz2-broken stale-unpack code
from 0.43.22 onward) that left any device which `install -r`'d
between 0.43.22 and 0.43.31 unable to refresh `_python_bundle/`
without losing `$AZT_HOME`. 0.44.0 packages the full recovery
chain into one release suitable for the public update channel.

The development history is in the 0.43.32–0.43.38 entries below;
the net surface for an end user is:

- **Stale-unpack actually works on `install -r` going forward.**
  bz2 import is optional in `_extract_bundle_from_apk`; gzip
  handles the modern `private.tar.gz` path. (was 0.43.32)
- **`azt_home()` is cached.** The OpenFile ContentProvider
  callback no longer burns 3-4 JNI calls per FD serve from the
  Binder dispatch thread — that was the 15:00:12 binder-thread
  NPE class. (was 0.43.32)
- **File-based boot diagnostic.** Service.py writes phase markers
  to `<filesDir>/service_boot.log` from the very first lines of
  module load. The last line before a process dies tells us
  exactly where it died, surviving any later stdio failure.
  `faulthandler` tracebacks land in the same file. (was 0.43.35)
- **`BundleResetReceiver` (Java).** Pure-Java BroadcastReceiver
  that wipes `files/app/_python_bundle/` AND the `.version`
  markers (`private.version` + `libpybundle.version`) — loads
  from `classes.dex`, never from `_python_bundle/`, so it fires
  even when every Python entrypoint is unrunnable. Reachable via
  `adb shell am broadcast -a org.atoznback.aztcollab.RESET_PYTHON_BUNDLE
  -p org.atoznback.aztcollab`. (was 0.43.33 + 0.43.34's marker fix)
- **`RecoveryActivity` (Java, hidden).** Class declared in the
  manifest, exported, but without a LAUNCHER intent-filter — end
  users see one launcher icon. Support / future re-enablement
  reaches it via `adb shell am start -n
  org.atoznback.aztcollab/.RecoveryActivity`. (was 0.43.36)

### Hard rule reminder

`adb uninstall` and `pm clear` are NOT recovery paths — they
wipe `$AZT_HOME` (projects, recordings, credentials, jobs.json).
`adb install -r` preserves data and now (with the 0.44.0 bz2
fix) actually refreshes the bundle on every reinstall.

For currently-stuck devices, the recovery is:

```bash
adb install -r path/to/0.44.0.apk
# tap AZT Collaboration on the device
```

The build's fresh `private_version` resource string mismatches
what's on disk → PythonActivity's UnpackFilesTask re-extracts
`_python_bundle/` from the new APK assets → daemon boots from
fresh code. `$AZT_HOME` preserved throughout.

### Unfinished

The original silent `:provider` boot-death root cause is still
unidentified. The boot diagnostic in 0.44.0 catches and locates
any future occurrence; pinpointing the existing case requires a
recurrence to read the diag from.

## 0.43.36 — hide AZT Recovery launcher icon (keep the class + receiver for later)

### Why

0.43.35's RecoveryActivity unstuck the field device, but the
second launcher icon ("AZT Recovery") sitting next to the
normal "AZT Collaboration" icon is noise for the >99% of users
who never need it. Recovery surfaces should appear only when
the underlying stuck-bundle bug class returns in the wild.

### What

Removed the `<intent-filter>` (MAIN + LAUNCHER) from the
`<activity>` declaration. The `RecoveryActivity` class,
`BundleResetReceiver`, file-based boot diagnostic, and the
"Show service boot log" button all remain in the APK. End
users see one launcher icon.

Recovery is still reachable for future use:

- **Field support with adb access:**
  ```
  adb shell am start -n org.atoznback.aztcollab/.RecoveryActivity
  ```
  Activity stays `exported="true"`, same-package addressing
  works.
- **Daemon-broadcast path:**
  ```
  adb shell am broadcast -a org.atoznback.aztcollab.RESET_PYTHON_BUNDLE -p org.atoznback.aztcollab
  ```
  The receiver is unchanged — `RESET_PYTHON_BUNDLE` action
  still wipes the bundle + `.version` markers.
- **Future release re-add:** if the stuck-bundle class
  re-emerges and we want users to self-recover without
  rebuilding, a future p4a_hook.py revision can put the
  LAUNCHER intent-filter back. The Activity code itself
  doesn't change.

### Files

- `/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py` —
  removed the LAUNCHER intent-filter block from
  `_BUNDLE_RESET_RECEIVER_BLOCK` injection. Activity is now
  self-closing without any intent-filter.

## 0.43.35 — file-based boot diagnostic for `:provider` silent death + RecoveryActivity log viewer

### Why

0.43.34 successfully re-extracted `_python_bundle/` on field rebuild,
confirming the bundle-stale recovery chain works end-to-end. But
`:provider` STILL dies within 17-65 ms of spawn with no Python
output anywhere — the original cascade-kill root cause that
predated the bundle issue. Bundle was a distraction; the real bug
is that something in service.py module load aborts Python silently.

`faulthandler.enable(file=sys.stderr)` from 0.43.32 didn't help
because PythonService doesn't redirect stderr to logcat (only
PythonActivity does). The traceback was being written, just to a
file descriptor going nowhere.

Worse: service.py dies so early that `_bridge_stdio_to_logcat`
hasn't installed yet, so even ordinary `print()` calls are
invisible. Without ANY logcat output between `Run user program`
(bootstrap.c's last line) and `Python for android ended.`
(bootstrap.c's exit line), we have no signal at all.

### What

Phase markers written to a real file from the very first lines
of service.py module load. The file:

- Lives at `<filesDir>/service_boot.log` (hardcoded path; can't
  use jnius-based resolution because jnius may be the thing
  failing).
- Survives process death (line-buffered + flushed per write).
- Receives a phase marker at every checkpoint:
  `module_load_start`, `imports_done`, `path_setup_done`,
  `faulthandler_enabled`, `thread_excepthook_set`,
  `before_bridge_stdio`, `after_bridge_stdio`, then
  `boot_trace:module_loaded`, `boot_trace:main_entered`,
  `boot_trace:before_import_azt_collabd`, etc.
- Is the file `faulthandler.enable(file=...)` writes its
  SIGSEGV tracebacks into, so a native crash also lands here.
- Rotates at 100 KB (renamed to `.prev`) so the diag doesn't
  fill flash under repeated crash loops.

The **last line** of this file before a process dies tells us
EXACTLY where it died.

### Surface

`RecoveryActivity` gets a second button — "Show service boot
log" — that reads `<filesDir>/service_boot.log` (and `.prev`)
and displays the last ~200 lines in a scrollable monospace
TextView. Pure Java, doesn't depend on Python or the daemon —
works even when everything else is broken. Field users surface
the diagnostic without adb.

### Files

- **`server_apk/service.py`** — `_diag(phase)` helper at the
  top of module load (before any import that could fail), and
  `faulthandler.enable(file=_BOOT_DIAG_FD)` pointed at the diag
  file instead of stderr.
- **`android/src/main/java/.../RecoveryActivity.java`** — added
  "Show service boot log" button + scrollable display.

### Recovery procedure for current stuck device

```bash
cd server_apk && bash build.sh
adb install -r bin/aztcollab.apk
# tap AZT Collaboration on the device
# if it doesn't boot, tap AZT Recovery → "Show service boot log"
# the LAST line before each spawn died tells us where to look
```

The build-version bump alone re-extracts `_python_bundle/`
(version mismatch between disk's `private.version` and the new
APK's `private_version` string). After that, the diag file
accumulates phase markers from each :provider spawn.

## 0.43.34 — `BundleResetReceiver` also wipes `.version` markers (0.43.33's wipe wasn't enough)

### Why

0.43.33 shipped the receiver + RecoveryActivity, and field testing
confirmed both surfaces work to remove `_python_bundle/`. But
re-extract on next Activity launch **didn't fire** — UnpackFilesTask
saw the surviving `private.version` / `libpybundle.version` markers
matched the APK's `private_version` string resource and short-
circuited the extract. Bundle stayed missing forever; field-log
showed `_python_bundle does not exist...should we expect a crash
soon?` repeating across 100+ process spawns over 2 minutes.

The relevant p4a code (in `PythonUtil.unpackAsset` /
`unpackPyBundle`) keys the extract on a version comparison, not
on directory existence. Wiping the directory alone is invisible
to that path. The previous `_python_bundle/` was extracted by
the same APK build that's on disk now, so the markers match
the APK and "no extract needed."

### Fix

`BundleResetReceiver` now also deletes:
- `files/app/private.version`
- `files/app/libpybundle.version`

Both markers gone → next UnpackFilesTask reads an empty
`diskVersion`, mismatches the APK's `private_version` →
`recursiveDelete(target)` wipes any remaining files/app/ junk
→ `extractTar` lays down fresh `_python_bundle/` from the APK.

### Recovery for the currently-stuck device

Two paths land you the same place:

**Path A (rebuild-and-reinstall, easier):**

```bash
cd server_apk && bash build.sh
adb install -r bin/aztcollab.apk
# tap AZT Collaboration on the device
```

A new build's `private_version` (timestamp-based) differs from
what's on disk → UnpackFilesTask sees the mismatch → extracts
fresh `_python_bundle/`. No need to touch AZT Recovery this
time; the version mismatch alone unsticks the install.

**Path B (use 0.43.34's fixed AZT Recovery):**

```bash
adb install -r bin/aztcollab.apk
# tap AZT Recovery → Repair sync service → Close
# tap AZT Collaboration
```

The fixed receiver wipes the markers too, so even without an
intervening APK-version bump the re-extract fires.

Both paths preserve `$AZT_HOME` (projects, recordings,
credentials, daemon.log).

## 0.43.33 — Java-only `BundleResetReceiver` + `RecoveryActivity` (second launcher icon) for stuck `_python_bundle/`

### Why

0.43.32 fixed the bz2-broken stale-unpack code, so devices going
forward can self-refresh their `_python_bundle/` on `install -r`.
But 0.43.27 already shipped publicly, and any device that
`install -r`'d between 0.43.22 and 0.43.31 is stuck — the loaded
`service.py` (from the stale bundle) still has the broken bz2
import, so the 0.43.32 fix on disk can't activate. The only ways
out of the stuck state were data-destructive (`adb uninstall`,
`pm clear` — both wipe `$AZT_HOME` with the user's projects,
credentials, jobs.json). Unacceptable for field-deployed peers
that hold weeks of recording work.

### What

A pure-Java `BroadcastReceiver` that wipes
`files/app/_python_bundle/` from inside the app's UID
(satisfying Android's UID isolation) without depending on
`_python_bundle/` (satisfying the "Python is broken" failure
mode). Java classes load from the APK's `classes.dex`, never
from the extracted Python bundle, so this works even when every
Python entrypoint is unrunnable.

After the wipe, the next picker Activity launch triggers p4a's
Activity-side extract-on-missing branch, which re-extracts from
the fresh APK assets. The `:provider` service then lazy-spawns
from the now-current bundle and the daemon recovers. `$AZT_HOME`
(`files/azt/`) is **not touched** — the wipe is scoped strictly
to `files/app/_python_bundle/`.

The 0.43.12 attempt failed because it fired auto-wipe on
`MY_PACKAGE_REPLACED` and the service-side bootstrap (at that
time) couldn't re-extract on missing dir, crash-looping under
`START_STICKY`. The 0.43.33 receiver:

- Fires **only manually** via a custom action
  (`org.atoznback.aztcollab.RESET_PYTHON_BUNDLE`), never
  automatically. Never on package replace.
- Recovery flow guides the user to open the Activity (which
  *does* extract on missing) before any peer triggers a service
  spawn, so the service always boots from a present bundle.

### Two recovery surfaces

**The receiver alone isn't enough for field deployment.** Field
machines (SIL linguists' tablets) don't have adb. They need a
purely in-app path that works when the picker Activity can't
boot — which is exactly the case when this recovery is needed
(`:provider` cascade-killing the picker means the picker won't
stay open long enough for the user to find a settings button).

So 0.43.33 ships two surfaces:

1. **`BundleResetReceiver`** — fires on the custom action
   `org.atoznback.aztcollab.RESET_PYTHON_BUNDLE`. Available via
   `adb shell am broadcast` for developers on stuck-but-USB-
   connected devices, and from inside the app via
   `sendBroadcast` from `RecoveryActivity`.
2. **`RecoveryActivity`** — a **second launcher icon** labeled
   "AZT Recovery". Pure Java, no SDL, no Kivy, no Python — so
   it launches even when every Python entrypoint in the APK is
   unrunnable. UI is built programmatically (no layout XML
   resource) so no res-pipeline changes were needed. The
   "Repair sync service" button fires the same broadcast, so
   both surfaces converge on the same wipe code.

The two icons let a field user recover from a stuck install
without leaving the device — open app drawer, tap "AZT
Recovery", tap "Repair sync service", tap "Close", reopen "AZT
Collaboration" normally.

### Files

- **New:** `android/src/main/java/org/atoznback/aztcollab/BundleResetReceiver.java`
- **New:** `android/src/main/java/org/atoznback/aztcollab/RecoveryActivity.java`
- **Touched (one-time sandbox carve-out):**
  `/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py` —
  added `_inject_bundle_reset_receiver` (gated on
  `dist_name == 'aztcollab'`), which injects both the
  `<receiver>` AND the `<activity>` with `MAIN`/`LAUNCHER`
  intent-filter, mirroring the existing
  `_inject_self_replace_receiver` pattern.

### Recovery procedure (field — no adb)

1. Open the device's app drawer.
2. Tap the **AZT Recovery** icon (second launcher icon, next to
   AZT Collaboration).
3. Tap **Repair sync service**.
4. After ~1 second, tap **Close**.
5. Tap the **AZT Collaboration** launcher icon to reopen the
   picker. p4a's Activity bootstrap re-extracts `_python_bundle/`
   from the fresh APK assets.

`$AZT_HOME` (projects, recordings, credentials, jobs.json) is
preserved throughout. The wipe is scoped strictly to
`files/app/_python_bundle/`.

### Recovery procedure (developer — with adb)

```bash
adb install -r server_apk/bin/aztcollab.apk
adb shell am broadcast \
    -a org.atoznback.aztcollab.RESET_PYTHON_BUNDLE \
    -p org.atoznback.aztcollab
# then tap AZT Collaboration on the device
```

If the broadcast doesn't take the first time (Android's
broadcast queue can drop receivers during heavy backoff), wait
~30 s and re-fire. Confirm via `adb shell dumpsys activity
services org.atoznback.aztcollab` — the `Restarting services`
backoff section should empty out once `:provider` boots cleanly.

### What this *doesn't* fix

The underlying root-cause crash (the silent `:provider` death
within 90 ms of spawn that triggered the bind/unbind cascade)
hasn't been pinpointed yet — but with a fresh bundle that
includes 0.43.32's `faulthandler.enable(all_threads=True)`, any
future crash will dump a Python traceback to logcat before the
process dies. Diagnosis-blocker removed.

## 0.43.32 — stale-unpack survives missing `_bz2`; cache `azt_home()`; faulthandler for `:provider`

### Why

Two field bugs falling out of the 2026-05-20 baf logs.

**Stale-unpack always silently failed.** `_extract_bundle_from_apk`
imported `bz2` at the top, but p4a's default build doesn't include
the `_bz2` C extension, so the import raised `ModuleNotFoundError`
before any decompression ran. Every "stale marker → re-extract"
attempt failed at the import boundary; the function returned False
to the caller's `except Exception`, the daemon kept loading old
Python code from the existing `_python_bundle/`, and the user
saw "I rebuilt and the bug is still there" on every iteration.
This made the entire 0.43.22 stale-unpack mechanism a no-op for
anyone whose p4a build matched the default — i.e. everyone.

**ContentProvider `openFile` callback path was crashing
`:provider` under sustained cawl-image traffic.** Tombstone at
pid=23550 tid=23558 (binder:23550_1) showed
`art::JNI::CallObjectMethodA` NPE in the same class as the
pre-0.43.23 dispatch-thread crash. Cause: `_resolve_path`
(the openFile callback's hot path) called `azt_home()`, which
re-fired `ActivityThread.currentApplication().getFilesDir()
.getAbsolutePath()` — 3-4 JNI invocations on the Java Binder
dispatch thread per FD serve. The 0.43.23 fix moved
`_check_self_updated` off the Dispatch path; the **OpenFile path
was untouched** and burned more JNI per call than DispatchCallback
ever did.

### Changes

1. **`bz2` is now optional in `_extract_bundle_from_apk`.** Wrapped
   in try/except; if `_bz2` is missing, the bz2 decoder is omitted
   from the decompressor list and `private.tar.gz` (gzip) still
   handles the modern p4a build path. Only the legacy
   `private.mp3` (bz2-renamed-to-dodge-asset-compression) path
   requires `_bz2`, which current p4a doesn't emit.

2. **`azt_collabd/paths.py:azt_home()` caches its result.** First
   call hits jnius (or platform fallbacks); every subsequent call
   reads a module global. Cache is safe — the Android `filesDir`
   value is UID-scoped and never changes for the lifetime of a
   process. Module-level `_AZT_HOME_CACHE` documents the field-log
   incident.

3. **`faulthandler.enable(file=sys.stderr, all_threads=True)`** at
   the top of `server_apk/service.py` module load. Next SIGSEGV
   from `:provider` will dump a Python-side traceback per thread
   into logcat under tag `python` before the process dies. With
   logcat-bridged stderr, this means the actual `*.py:line` site
   of the crash is now visible without symbols for `jnius.so`.

4. **`threading.excepthook`** installed to log uncaught worker-
   thread exceptions with the thread's `name`. Background failures
   in `azt_collabd-watcher`, `commit-fire-<lang>`,
   `cawl-prefetch-<repo>`, and the new `self-update-poll*` Timer
   threads now surface to logcat instead of silently disappearing.

5. **Name the remaining unnamed `threading.Timer` instances** in
   `azt_collabd/android_cp/service.py` —
   `self-update-poll-init`, `self-update-poll`,
   `self-update-exit`. Pairs with the existing memory note that
   every daemon thread must be named so future tombstones
   self-identify.

### Files touched

- `server_apk/service.py` — `bz2` optional; `faulthandler.enable`;
  `threading.excepthook`.
- `azt_collabd/paths.py` — `azt_home()` cache.
- `azt_collabd/android_cp/service.py` — Timer naming.
- `azt_collab_client/__init__.py` — version bump to 0.43.32.

## 0.43.22 — sync push loop: stop the 35-minute hang on flaky-DNS networks

### Why

Field log baf 2026-05-20 captured the disaster cleanly. A user
tapped Sync on a metered tether; the daemon spent **35 minutes**
losing a fight that could have ended in one HTTPS request:

1. `[12:47:32] [collab.store] github refresh failed: Token refresh
   network error: <urlopen error timed out>` — the proactive token
   refresh failed at sync setup. The daemon kept the existing
   access token in play. It was about to expire.
2. DNS to github.com was flapping at ~5 % uptime — DoH AAAA+A
   timing out, system resolver dead, brief recoveries between
   long outages.
3. The push retry loop saw `NameResolutionError` and *halved
   chunk_n*: 302 → 151 → 75 → 37 → 18 → 9 → 4 → 2 → 1. Halving
   pack size doesn't fix DNS — same number of TCP connections
   needed regardless — so the loop was bisecting on the wrong
   axis.
4. When DNS finally cooperated for one request at chunk_n=1,
   the server returned **401** — the access token had gone past
   its 8 h cliff during the storm. The daemon ate the 401, kept
   trying, and only stopped when `consecutive_failures` hit 12.
5. Final codes: `['COMMITTED_LOCAL', 'PULL_FAILED', 'PUSH_FAILED',
   'AUTH_REFRESH_STALE']`. The AUTH bit was emitted post-hoc by
   `_annotate_with_auth_health`, not because the loop noticed.

A second sync attempt on the same project the same day uploaded
the pack successfully at chunk_n=20 (server reported
`DivergedBranches(old=9ee637c..., new=580e2a49...)` — meaning the
push *landed* on the server) but the read side of the connection
dropped with `IncompleteRead(0 bytes)`. The daemon reverted to
"push full local tip" instead of trusting the server's report,
re-pushed, looped through more DivergedBranches → IncompleteRead
cycles, then got OOM-killed mid-retry at the 10-minute mark.

### Changes (push loop hardening)

Five mitigations, none of which is "DNS caching" but one of which
extends the existing DoH negative cache:

1. **Pre-flight credentials probe** in `_push_step_locked`. When
   `store.github_refresh_state()['broken']` is True AND the
   remote is a GitHub URL, run `test_github_credentials(token)`
   against `api.github.com/user` (15 s cap) before entering the
   retry loop. If the token is rejected, emit `AUTH_REQUIRED` +
   return — the loop never starts. Best-effort: any exception
   in the probe falls through to the normal loop.
2. **401 short-circuit** in both fetch and push paths. `_is_http_401`
   matches both dulwich's typed `HTTPUnauthorized` and the bare
   `\b401\b` message shape. Treating 401 as a transient network
   failure (pre-0.43.22 behaviour, because nothing matched it)
   was the proximate cause of the 12-failure halving storm.
3. **DNS-class failures no longer halve** `chunk_n`. New branch
   in the retry-after-backoff section: `if
   _is_dns_resolution_failure(exc): continue` (after the back-off
   sleep, holding target + working_batch_n). When DNS recovers
   we resume on the chunk size that previously worked. Halving
   on DNS failure was pure overhead — pack size has zero effect
   on whether the resolver returns an address.
4. **Trust `DivergedBranches`'s reported remote tip.** The
   exception's `args[0]` is the server's authoritative view of
   the ref ("current_sha"); when refetch fails (e.g.
   `IncompleteRead` mid-handshake), use the rejection's reported
   SHA as `new_remote` and write it to
   `refs/remotes/origin/<branch>` so the next iteration doesn't
   re-discover it. New helper `_extract_diverged_remote(exc)`.
5. **Wall-clock budget on the push loop.** New setting
   `sync.push_budget_s` (default 300 s, env
   `AZT_SYNC_PUSH_BUDGET_S`, 0 disables). When the loop's
   monotonic elapsed time exceeds the budget on a network-class
   failure, emit the new `SYNC_GIVING_UP_TRANSIENT` status code
   carrying `budget_s` + `commits_pending` and bail with
   `PUSH_FAILED`. The pending commits stay queued; the next
   sync run picks them up. This bounds wedged sessions so the
   project lock frees for other operations.

### Changes (DoH resolver)

6. **Exponential negative-cache TTL** in `net.py`. The existing
   5 s negative cache was poisoning sustained-outage scenarios:
   every DNS lookup paid the full 2.5 s DoH round-trip, so on a
   35-minute storm the resolver alone burned ~12 minutes of
   wall time. Now consecutive failures for the same host extend
   the negative TTL exponentially: 5 s → 10 s → 20 s → 40 s →
   60 s (`_DOH_NEGATIVE_TTL_MAX_S`). A single positive resolve
   resets the counter, so a brief outage followed by reconnect
   returns to the tight 5 s probe cadence. Cache value tuple
   widened from `(expiry, records)` to
   `(expiry, records, neg_count)`; in-process so no migration.

### Changes (status + translation)

- `S.SYNC_GIVING_UP_TRANSIENT` added in both
  `azt_collabd/status.py` and the `azt_collab_client/status.py`
  mirror. Carries `budget_s` and `commits_pending` params.
- French + English translations added; matches the auto/user
  silence contract (auto-sync silences this code, user-initiated
  Sync surfaces the toast).
- `sync.push_budget_s` documented in `settings.py`.

### Files

- `azt_collabd/repo.py` — `_is_http_401`,
  `_extract_diverged_remote`, fetch 401 short-circuit, push 401
  short-circuit, pre-flight probe, DNS-no-halve branch,
  DivergedBranches authoritative-remote, wall-clock budget.
- `azt_collabd/status.py` — `SYNC_GIVING_UP_TRANSIENT`.
- `azt_collabd/settings.py` — `sync.push_budget_s` default + env
  map + `push_budget_s()` accessor.
- `azt_collabd/net.py` — exponential negative TTL.
- `azt_collab_client/__init__.py` — version 0.43.21 → 0.43.22.
- `azt_collab_client/status.py` — mirror.
- `azt_collab_client/translate.py` — English string.
- `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  — French translation.

### Restart-server button: layout + force-kill fallback

Two follow-ups on the 0.43.20 button after a field session.

- **Stacked instead of side-by-side.** French labels are too long
  to share a row at phone widths: "Partager le journal du service"
  + "Redémarrer le service" overflow the dp(52) row regardless of
  spacing. Moved Restart to its own row below Share daemon log.
  English is unaffected.
- **Bootstrap reboot-to-apply popup auto-resolves via cooperative
  restart.** `_prompt_server_reboot_to_apply` in
  `azt_collab_client/ui/bootstrap.py` fires when the peer detects
  `installed > running` on the server APK (new APK on disk, old
  daemon still in `:provider`). Pre-0.43.22 it surfaced a
  "restart your device" popup. The peer can't `Process.killProcess`
  across UID boundaries, but it CAN ask the running daemon to
  restart itself via `POST /v1/admin/restart` (shipped 0.43.20).
  Try that first; on RESTARTING re-probe compat via
  `_post_install_continuation`. Only fall through to the popup
  when the daemon is too old to know the endpoint — the genuinely-
  stuck pre-0.43.20 case where reboot is the only way out.
- **Loop guard on the cooperative restart**, attached to ``ctx``.
  Tracks attempted `(installed, running)` pairs in
  `ctx._cooperative_restart_attempts`. If `_post_install_continuation`
  re-probes and the SAME running version comes back, the restart
  didn't actually load new code — the respawned daemon re-imported
  a stale `_python_bundle/` from filesDir. Without this guard the
  cooperative restart loops forever at ~once per 2 s, burning
  battery. With it: one attempt per pair, then fall through to
  the popup. Updated popup body to explain the stale-unpack
  cause rather than blaming Android's package-replace process
  preservation.
- **Service-side stale-unpack fix** in `server_apk/service.py`.
  The bootstrap loop guard above prevents the symptomatic cycle
  but doesn't fix the underlying issue. p4a's C bootstrap
  extracts `assets/private.*` to `_python_bundle/` only when the
  directory is missing — never on APK update — so on reinstall
  the respawned `:provider` Python interpreter imports the
  previous APK's code from disk. Documented as a TODO in
  `SuiteSelfReplaceReceiver.java`'s NOTE on stale p4a unpack
  (Activity-launch-from-receiver or service-side
  extract-on-missing; this is the service-side branch).
  - `_maybe_reextract_python_bundle()` runs in `service.py:main()`
    BEFORE `import azt_collabd`. Compares the running APK's
    mtime (via `ApplicationInfo.sourceDir`) to a marker file at
    `_python_bundle/.apk_mtime`. On mismatch: re-extracts
    `assets/private.{tar.gz,tar,mp3}` to `_python_bundle.new/`,
    atomically renames the old bundle aside, swaps the new one
    in, writes the marker, and `os._exit(0)`s. Android's
    ContentProvider auto-spawn brings up a fresh `:provider`
    that imports the new code. First-launch path stamps the
    marker without re-extracting (p4a's C code already
    extracted from this APK).
  - Handles all three p4a asset names (`.tar.gz`, `.tar`,
    `.mp3` — the renamed bz2 from older p4a that dodges
    Android's auto-compression) and detects the decompressor by
    trying gzip → bz2 → plain in order.
  - Atomic swap via rename so concurrent peer calls (which can
    trigger lazy-spawn during the swap window) either see the
    pre-update bundle or the post-update bundle, never a
    half-written one.
  - Best-effort: any failure (no jnius, can't read APK, extract
    error) falls through. Worst case the daemon runs old code
    one more cycle and the bootstrap loop guard surfaces the
    popup.
- **Version strip refreshes after restart.** The
  `client X · server Y` strip at the bottom of the settings page
  is populated by ``_probe_server_version`` at app startup and was
  never re-fetched afterwards. After a successful Restart the
  daemon's version changed but the strip stayed frozen at the old
  value, so the restart looked like it did nothing visible.
  ``SettingsScreen._refresh_version_strip_after_restart`` now
  schedules a fresh ``_probe_server_version`` call on the running
  App (works for both ``CollabUIApp`` desktop and ``PickerApp``
  Android since both expose the method) 2 s after the cooperative
  or force-kill restart fires, so the strip rerenders with the
  new daemon version. Best-effort: silent failure leaves the strip
  stale but doesn't break anything else.
- **Force-kill fallback** in `SettingsScreen.restart_server`. The
  cooperative path (`POST /v1/admin/restart`) returns
  `SERVER_ERROR` against a daemon too old to know the endpoint —
  which is the exact scenario users tap the button for ("I just
  installed a new APK, the running daemon is still the old code,
  please get rid of it"). The toast previously said "Could not
  reach the sync service to restart it", leaving the user with
  no recovery path. Now: on `SERVER_ERROR` / `SERVER_UNAVAILABLE`,
  fall through to the same kill-by-PID mechanism
  `SuiteSelfReplaceReceiver` uses on
  `ACTION_MY_PACKAGE_REPLACED`:
  - **Android**: jnius → `ActivityManager.getRunningAppProcesses`
    → `Process.killProcess(pid)` for each non-self PID (same UID,
    no permission needed, bypasses the `:provider` service's
    `IMPORTANCE_SERVICE` pin). `killBackgroundProcesses(pkg)` as
    belt-and-braces fallback for any process the enumeration
    missed.
  - **Desktop**: read `$AZT_HOME/server.json::pid`, `os.kill(pid,
    SIGTERM)`. Auto-spawn picks the daemon back up on the next
    RPC.
  Either path: settings UI process is unaffected (different
  process from the daemon on both platforms). Successful kill
  surfaces the same "Sync service is restarting…" toast the
  cooperative path uses; failure adds a parenthetical detail
  string ("no sibling processes found to kill", "no pid in
  server.json", etc.) to the original "Could not reach" toast so
  the user has a diagnostic.

### Connectivity watcher now starts on Android (auto-push restored)

Field log baf 2026-05-20 17:22:58–17:25:41: `COMMITTED_LOCAL`
fires, peer polls `/v1/projects/.../status` every 10 s for
3+ minutes, **no `[scheduler] drain pushes:` line ever appears**.
User-gestured Sync via the Sync button works (path through
`_h_project_sync`); only the auto-drain is broken.

Root cause: `server_apk/service.py` (the Android `:provider`
entry point) calls `scheduler.reconcile_on_startup()` but
**never `scheduler.start_watcher()`**. The watcher is wired only
in `server.run()` (the desktop loopback entry path). On Android
the daemon commits locally on every `commit_project` RPC,
sets `pending_push=true` in projects.json, and then ... nothing.
No thread to drive the drain loop.

Every Android peer on every version of `azt_collabd` has been in
this state since the commit/push split landed (0.43.0). The
visible symptom — "+N commits ahead of github" with no
auto-push — has been there the whole time; the Sync button
masks it because users routinely tap it.

Fix: one line in `server_apk/service.py::main()` immediately
after `reconcile_on_startup()`:

```python
scheduler.start_watcher()
```

Restores parity with the desktop daemon. Watcher ticks every
`sync.connectivity_poll_s` (30 s default), checks
`_has_internet()` + `sync.work_offline` + 60 s post-online
grace, then calls `_drain_pending_push()` which pushes any
`pending_push=true` projects.

### Sweep pre-0.37 `.cawl_image_urls.json` orphans on daemon startup

Field log baf 2026-05-20 surfaced `[data-loss-risk] uncommittable
file in project_dir: '.cawl_image_urls.json'` on every commit step
for projects that existed before the 0.37 CAWL daemon migration.

Pre-0.37, peers maintained per-project URL caches at
`<working_dir>/.cawl_image_urls.json`. 0.37 moved ownership to the
daemon (`$AZT_HOME/cawl/index.json`, 24h TTL, lock-coalesced) and
peers stopped writing the per-project file — but on devices that
crossed the migration boundary, the existing files were never
cleaned up. Nothing in current code reads or writes them (grep
returns zero hits across `azt_collabd/` and `azt_collab_client/`);
they're inert bytes that the staging filter flags every commit.

`reconcile_on_startup` now calls `_sweep_legacy_orphans()` which
iterates `projects.json::*::working_dir` and `os.remove`s any
known-stale files. Currently just `.cawl_image_urls.json`;
catalogue lives in `_LEGACY_ORPHAN_PATHS` for future migrations to
extend. Idempotent (missing files are a no-op), best-effort (per-
file failures log and continue), runs outside the scheduler lock
(touches FS, not the jobs registry). One log line per file
removed: `[scheduler] orphan sweep: removed 'en-UY-x-kent'/
.cawl_image_urls.json (pre-migration leftover)`.

### `:provider` SIGSEGV fix — self-update poll off the dispatch thread

Field log baf 2026-05-20 16:03:42 captured a `:provider` tombstone
on a 6GB device (so not memory pressure):

```
F libc : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault
        addr 0x0 in tid 8912 (Thread-3), pid 8859 (collab:provider)
F DEBUG : Cause: null pointer dereference
F DEBUG :   #00 art::InvokeVirtualOrInterfaceWithJValues
F DEBUG :   #01 art::JNI<false>::CallObjectMethodA
F DEBUG :   #02 jnius.so
F DEBUG :   #03 jnius.so
F DEBUG :   #04 libpython3.11.so (_PyObject_MakeTpCall+328)
```

The cascade was visible in the same trace:

```
ActivityManager: Killing 8874:org.atoznback.aztrecorder ... depends
on provider org.atoznback.aztcollab/.AZTCollabProvider in dying
proc org.atoznback.aztcollab:provider
```

**Root cause** — the documented jnius-on-worker pattern (see
memory `feedback_jnius_prewarm_main_thread.md`). The dispatch
callback in `azt_collabd/android_cp/service.py` called
`_check_self_updated()` on every RPC, which did two
`PackageManager` JNI calls per call. The Java-side dispatch
thread is a Python worker attached to the JVM with the
bootclassloader; sustained jnius traffic from there eventually
NPE'd `art::JNI::CallObjectMethodA`. Cawl image prefetch (~3–4
RPCs/sec) made this a near-certainty per session.

**Fix** — move the PackageManager poll off the dispatch path.
- `_self_update_poller()` runs on a main-thread Timer
  (60 s cadence). It calls `_pkg_last_update_time()`,
  compares to the snapshot, and flips a module-level bool
  on detection.
- `_check_self_updated()` now reads the bool. Zero jnius
  per RPC. Single-writer / single-reader bool — GIL covers
  the race.
- 60 s detection latency is fine: the only consumer is the
  `_schedule_exit_for_update` gate, and the user's next
  ContentResolver call after install almost always arrives
  within seconds anyway.
- Belt-and-braces: `server_apk/main.py` step 2a.1 extends
  the existing jnius prewarm to cover `PythonService`,
  `Context.getPackageManager`, and
  `PackageManager.getPackageInfo` from the main thread. Any
  future worker-thread caller of these gets the cached
  classloader bindings without paying the bootclassloader
  penalty.

**Memory budget**: the change adds ZERO new threads (the Timer
re-spawns itself with `threading.Timer` after each tick, same
as the connectivity poller). Memory delta ≈ one Python bool.
Appropriate for the 6GB target.

### Drive-by drift fixes (caught while running the 0.43.22 test suite)

- `_ensure_remote_repo` no longer returns `REMOTE_CREATE_FAILED`
  on URLs without an `owner/repo` path (e.g.
  `http://127.0.0.1:NNNN/` from `dulwich.web`-fronted local
  servers, or any LAN git host with a flat-root URL). The
  parse-failure path now falls through to the unknown-host
  branch (`True, None` — let push attempt and surface the real
  error). Unblocks all four `test_local_git_remote.py` tests
  that had been failing since 0.43.21.
- `test_local_git_remote.py::test_pull_repo_fetches_updates_from_local_server`
  and `::test_sync_repo_round_trip_via_local_server` asserted
  `result.has(S.COMMITTED)`, but `commit_repo` / `sync_repo`
  correctly emit `COMMITTED_LOCAL` (the 0.43.0 split between
  commit-and-push and commit-only). `S.COMMITTED` is only used
  by `init_repo`'s initial commit. Tests updated to assert
  `S.COMMITTED_LOCAL`.
- Translation drift: three msgids that AST-walked in source
  but were absent from the French .po — added with French
  strings. `'Network reachable, but the sync host could not
  be resolved.'` (DNS_RESOLUTION_FAILED toast),
  `'Servers'` (section label in daemon settings),
  `'Work offline:'` (toggle label in daemon settings).
- Translation drift (Restart server, 0.43.20-era):
  `'Restart server'`, `'Sync service is restarting…'`,
  `'Could not reach the sync service to restart it.'`, and
  `'Restart request returned an unexpected response.'` were
  added to the .po as `msgstr ""` placeholders when the
  Restart-server button shipped in 0.43.20, but never
  populated. In a French locale that meant `_('Restart
  server')` returned `''` — the button rendered with an
  empty label between "Partager le journal du service" and
  the end of the row, looking like a phantom-sized gap.
  Populated with French translations.

### Not done (intentional follow-ups)

- The "duplicate poller" pattern visible from the peer side
  (status flood at ~2 Hz, 2× "cache warm" log line, two
  `commit_project` calls 30 s apart returning NOTHING_TO_COMMIT)
  is on the peer, not the daemon. The daemon log shows clean
  single-commit debounce for hours of work. Fix lives in
  `azt_recorder` — out of lane for this repo.
- Pre-0.43.22 also lacked typed status emission during long
  syncs (peer polls `project_status` for minutes with no signal
  beyond "running"). `SYNC_GIVING_UP_TRANSIENT` is the first
  step; in-progress status emission (e.g.
  `SYNC_PROGRESS(chunk_n, retries)`) is a separate feature.

## 0.43.21 — startup probe gives up fast on DNS failure + local-HTTP-git-remote test coverage

### Why (startup probe)

A field session from baf (2026-05-20) showed bootstrap stalling
~20 seconds on a presplash with no visible activity when DNS
resolution of `api.github.com` failed (`[Errno 7] No address
associated with hostname`). Trace: `compat_ok t=2.389` →
`bootstrap_done t=22.553`.

Root cause: `_fetch_latest` in `azt_collab_client/ui/update.py`
used `urlopen(timeout=15)` against `/releases?per_page=20`, then
caught **any** exception and fell through to
`_fetch_latest_singleton` which did **a second**
`urlopen(timeout=15)` against `/releases/latest`. On a dead
resolver, both calls fail for the same reason — so we paid the
DNS-timeout budget twice for a doomed retry. The singleton
fallback was designed for "listing endpoint returned junk /
HTTP error" cases where the network is fine; on URLError-class
failures it's pure overhead.

A user staring at a black presplash for 20 s thinks the app has
crashed and force-quits. dd61da3 ("hopefully final fix for
Cameroon DNS errors") fixed the sync-path 25-minute push hang
but didn't touch this bootstrap path.

### Changes (startup probe)

- `_PROBE_TIMEOUT_S = 5` constant introduced in
  `azt_collab_client/ui/update.py`; replaces the hard-coded
  `timeout=15` on both `_fetch_latest` and
  `_fetch_latest_singleton`. Worst-case startup stall now
  caps at one ~5 s timeout instead of two ~15 s timeouts.
- `_fetch_latest`'s `except Exception:` split into three
  branches:
  - `urllib.error.HTTPError` → fall back to singleton (server
    refused the listing; network is fine).
  - `urllib.error.URLError` → `raise` immediately (DNS /
    connect-refused / TLS botch; singleton fails the same way).
  - Bare `Exception` → fall back to singleton (JSON parse
    failure, captive-portal HTML — connection works, body was
    junk).
- Tests added in `tests/test_check_for_update.py`:
  - `test_fetch_latest_falls_back_on_http_error` (404 listing
    → singleton tried).
  - `test_fetch_latest_raises_on_url_error_without_singleton_retry`
    (URLError → call count == 1, no doomed retry).
  - `test_fetch_latest_uses_probe_timeout` (timeout pinned to
    `_PROBE_TIMEOUT_S`).

### Why (local-HTTP-git-remote test coverage)

The daemon's remote handling has always been host-agnostic in
principle — `Project.remote_url` accepts any HTTP/HTTPS git
URL — but the only host exercised in the field is github.com.
A team running gitea / forgejo / gogs / git-daemon on a laptop
on the office LAN is a perfectly valid convergence point, and
the parked LAN-sync spec in `docs/local_lan_sync_stub.md`
builds on dulwich.web as its in-process listener (same library,
same WSGI shape). Without a CI test, a github-ism (substring
matching on a github-specific error string, host-header
assumptions in dulwich, a credentials-store lookup keyed on
`github.com`) could silently break the non-github path and we
wouldn't notice until a field user with a local gitea filed a
bug.

### Changes (local-HTTP-git-remote test coverage)

- `tests/test_local_git_remote.py` added. Fixture spins up a
  dulwich-backed HTTP git server in a background thread serving
  a bare repo. Five tests cover:
  - The fixture itself responds to `info/refs?service=git-upload-pack`.
  - `init_repo` initializes + commits + pushes; bare-side refs
    reflect the push.
  - `clone_repo` against the local server produces a working
    dir matching the seeded content.
  - `pull_repo` brings updates committed on a second working
    dir through the local server.
  - `sync_repo` round-trips two working dirs (commit + push
    under one lock) converging through the local server.
- Auth integration on non-github hosts is deliberately out of
  scope here — the fixture serves unauthenticated HTTP; the
  github-flavoured credentials paths are exercised by their
  own tests. The LAN-sync spec implementation phase will add
  cert-based auth tests on top of this scaffolding.

## 0.43.20 — sync trace honesty + push timeouts + non-FF retry escalation

### Why

Field log from baf (2026-05-19, ~5 hours of intermittent DNS) surfaced
four distinct sync-loop bugs:

1. The `[sync-trace] fetch done` line was logged unconditionally, so
   a fetch that hit `Max retries exceeded (NameResolutionError)`
   looked identical to a healthy fetch — downstream `local_sha` /
   `remote_sha` reads from the (stale) tracking ref then drove
   adaptive-batching decisions that couldn't possibly converge.
2. A single push attempt held `project_lock` for 25 minutes
   (19:11 → 19:36, ending in `SSLEOFError`) because `porcelain.push`
   was called with no timeout — urllib3's default is no read
   timeout, so a stalled SSL upload sits there until the kernel
   keepalive eventually breaks the socket. Every other client RPC
   during that window got `BUSY`.
3. A `DivergedBranches` push rejection followed by a re-fetch
   showing no remote movement entered the "local still ahead"
   recovery branch and looped: same `target_sha`, same `chunk_n`,
   same rejection on the next iteration. The server's rejection
   was authoritative — it had something we couldn't see, or our
   intermediate-target pack didn't include the full ancestry — but
   the loop trusted the local ancestor walk over the server.
4. The "local still ahead" branch (legitimate concurrent-pusher
   case where remote advanced to something our local descends
   from) reset `target_sha`, `working_batch_n`, and `backoff_s`
   to scratch, so every concurrent-push race threw away whatever
   chunk size adaptive batching had found to work. `chunk_n=89 →
   chunk_n=719` reset observed repeatedly in the same trace.

### Changes

`azt_collabd/repo.py`:

- `[sync-trace] fetch done` moved inside the success path; the
  except adds a `[sync-trace] fetch failed: <repr(exc)>` line and
  still surfaces `PULL_FAILED` on the `Result` so the downstream
  try-push-anyway behaviour is preserved.
- `_socket_timeout(seconds)` context manager wraps `porcelain.fetch`
  (60 s) and `porcelain.push` (180 s) — sets
  `socket.setdefaulttimeout` for the body, restores on exit. urllib3
  starts fresh connections on pool exhaustion (the
  `Starting new HTTPS connection (N)` trace lines confirm this), so
  the timeout reliably bounds each socket's I/O. DoH calls in
  `net.py` pass explicit `timeout=` to `urlopen` which override the
  default, so DoH stays at its 2.5 s budget.
- `nonff_no_progress_streak` tracker added to the push loop. When a
  push raises non-FF AND the re-fetch shows no remote movement:
  - First hit on an intermediate target (`target_sha != local_sha`):
    revert to the full local tip via the standard
    `refs/heads/<branch>` refspec. The temp-ref pack-negotiation
    path may be the culprit; the full-tip path bypasses it.
  - First hit on the full local tip: force a `_merge_diverged`
    against the current `remote_sha`. The server's rejection is
    authoritative; our local ancestor walk is wrong somewhere.
  - Second hit on the full local tip: bail with `PUSH_FAILED`.
    We can't reconcile from here without external input.
- The legitimate "still ahead" branch (re-fetch saw remote actually
  advance, but to something our local descends from) now `continue`s
  with `target_sha` + `working_batch_n` + `backoff_s` preserved —
  the post-reconciliation reset is confined to the diverged-merge
  branch where it belongs.

### Also in this bump: "Restart server" button

Companion fix surfacing the daemon-restart capability that the
sync-loop hardening above relies on for "give me a fresh process
right now" recovery.

- `POST /v1/admin/restart` (`azt_collabd/server.py::_h_admin_restart`):
  responds OK immediately and then, after a 0.5 s flush delay,
  terminates the daemon. Desktop loopback: `os.execv` replaces the
  process image with a fresh `python -m azt_collabd`, inheriting
  env / cwd / PYTHONPATH; the new daemon re-acquires `server.lock`
  and writes a new `server.json`. Android `:provider`:
  `os._exit(0)`, Android's ContentProvider auto-spawn revives the
  process on the next peer call and `Service.onCreate` re-runs
  `reconcile_on_startup()`.
- `azt_collab_client.restart_server()`: thin wrapper around the
  endpoint. Returns a `Result` carrying either `RESTARTING`
  (informational; daemon accepted and is in flight), or the
  standard `SERVER_UNAVAILABLE` / `SERVER_ERROR` transport-failure
  shapes. Never raises — UI handlers call without try/except.
- `RESTARTING` status code added to both
  `azt_collabd/status.py` and `azt_collab_client/status.py`
  (mirrors; client copy is decode-only) plus a translation in
  `translate.py`. params: `transport=`'desktop' | 'android' |
  'unknown'`.
- `azt_collabd/ui/app.py::SettingsScreen.restart_server` +
  KV `restart_server_btn` next to "Share daemon log" under
  Diagnostic log. Method runs the blocking RPC off the UI thread
  and shows status in the existing `daemon_log_status` label.
  Settings UI lives in a separate process on both desktop
  (`python -m azt_collabd ui`) and Android (PythonActivity vs.
  `:provider`), so triggering a daemon restart doesn't kill the
  UI in either case.

### Wire compat

Sync-loop hardening: nothing changes on the wire. Failures still
surface as `PUSH_FAILED` (with `DNS_RESOLUTION_FAILED` peer-routing
in the existing `_add_push_failure` path).

Restart-server: one new status code (`RESTARTING`) and one new
endpoint (`POST /v1/admin/restart`). Older clients calling the
new endpoint would get a 404; older daemons receiving a
`restart_server()` call would 404 and the client wrapper
surfaces `SERVER_ERROR`. No `MIN_CLIENT_VERSION` floor bump
needed — peers don't depend on the endpoint for any existing
flow.

### Test fixture: `test_local_git_remote.py` mkdir

The `local_git_server` fixture was erroring at setup against the
installed dulwich (`FileNotFoundError: '<tmp>/remote.git/branches'`
inside `Repo._init_maybe_bare`) because newer dulwich expects the
`controldir` path to exist before `init_bare`. Added an explicit
`remote_path.mkdir()` so the fixture matches the dulwich contract.
The five tests in that file (LAN-sync prep — see 0.43.19) now get
past setup; the sanity test passes, but four of them surface
latent assertion failures (push / clone / pull / sync against a
dulwich-backed local HTTP server without auth wiring). Those are
unrelated to this bump's daemon-side changes — they're testing
LAN-sync prep code that's parked anyway, and the fixture fix
only made the latent failures visible. Left for a dedicated
LAN-sync session.

## 0.43.19 — LAN sync design spec drafted (parked)

### Why

`docs/local_lan_sync_stub.md` was a sketch; expanding it to a real
spec after researching mDNS-on-Android (Android 17 will gate raw
mDNS sockets behind a new runtime permission — `NsdManager` with
`FLAG_SHOW_PICKER` is the escape hatch), Android 14+ foreground-
service rules (`specialUse` is the right type; `dataSync` has a
6h/24h cap that's a footgun for an always-on toggle), dulwich's
HTTP server seam (`HTTPGitApplication` + `make_wsgi_chain`
covers both upload-pack and receive-pack out of the box), and
offline-first peer-to-peer git patterns (Syncthing-style identity
+ Radicle's namespace separation of identity from endpoint).

### Changes

- `docs/local_lan_sync_stub.md` rewritten as a design spec with
  eight load-bearing decisions locked, concrete touchpoints
  enumerated, and a short list of items deferred to
  prototyping. Still parked — no implementation in this bump.
- Onramp section added so a fresh agent (post-/clear) can pick
  the work up without re-litigating the 23 rejected
  alternatives. Includes a phased implementation plan (8
  phases, each independently smokable), a reading list of
  upstream CLAUDE.md files + relevant memory entries, the
  status codes to add, desktop-only smoke recipes for phases
  1-4, and an eight-question self-test.

### Decisions captured (so they're not relitigated)

- Topology: GitHub-authoritative star + opportunistic LAN fan-out.
  No peer-graph / gossip state — pairing is explicit per-pair,
  propagation is implicit in git's ref-advertisement dedup.
- Identity: per-device ed25519 keypair at `$AZT_HOME/peer_id`,
  separate from the suite signing-keystore fingerprint.
- Pairing: one-way QR (A shows, B scans), auto-reverse-record on
  B's first authenticated fetch.
- Auth: pinned TLS cert on LAN (cert handshake = identity proof);
  loopback bearer token unchanged.
- Listener: `dulwich.web` hosted in the existing `:provider`
  process, promoted to a `specialUse` foreground service while a
  daemon-wide "Allow LAN sync" toggle is on.
- Discovery: Android `NsdManager` via pyjnius
  (`FLAG_SHOW_PICKER`); `python-zeroconf` on desktop; QR /
  manual-IP fallback for hotspot scenarios where mDNS is
  silently blocked.
- Project sharing: per-direction allowlist after pairing, set
  from the daemon settings UI; no QR for the second step.

## 0.43.18 — daemon log rotates on toggle-on; previous session preserved in `.prev`

### Why

The "Save daemon log to file" toggle's `truncate=True` path
opened the log file in `'w'` mode, wiping any prior content. A
remote tester flipping the toggle to start a fresh investigation
would lose the previous investigation's evidence — confirmed in
the field after the user said "the toggle just erased the sync
trace we'd been waiting to see." Remote debugging across time
zones and a language barrier doesn't have the bandwidth to
re-ask "do exactly the same thing again so I can capture the
same log."

### Change

`azt_collabd/server.py::install_stdio_tee(truncate=True)` now
rotates first:

1. If `$AZT_HOME/daemon.log` exists, rename it to
   `daemon.log.prev` (overwriting any prior `.prev`).
2. Open `daemon.log` fresh in `'w'` mode (effectively empty
   since the rename took the prior content).
3. Write the `(fresh session, daemon X.Y.Z)` banner.

Respawns (the `truncate=False` path, fired by
`maybe_install_stdio_tee` at every `:provider` boot) continue to
append to `daemon.log` unchanged — rotation only happens at the
explicit toggle-on gesture. This avoids the otherwise-pathological
case where idle-stop respawns rotate the `.prev` away every few
minutes.

`azt_collabd/ui/app.py::share_daemon_log` now passes
`prev_path=data['log_path'] + '.prev'` to `share_log_file`. The
existing `_bundle_log_blob` silently skips the previous-session
section when the path doesn't exist (first toggle-on, no prior
file to rotate), so the share bundle gracefully degrades to
just the current-session block when there's no `.prev`.

Net behaviour:

- **First toggle-on ever:** no rotation, fresh log, share shows
  one `=== current session ===` block.
- **Subsequent toggle-on:** prior log rotated to `.prev`, new
  `.log` starts fresh, share shows
  `=== previous session === / === current session ===`.
- **Daemon respawn (toggle stays on):** appends to current `.log`
  with an `(appending — daemon X.Y.Z respawn)` banner as section
  break. `.prev` is untouched.

### Notes

- Matches the peer (`azt_recorder.log` / `azt_recorder.log.prev`)
  rotate-on-launch pattern that the share helper was already
  written to support — just plumbed through to the daemon side.
- No wire-format change; no `MIN_CLIENT_VERSION` change.
- Rotation is best-effort: if the rename fails (disk full,
  permissions, whatever), we log the failure to `sys.__stderr__`
  and continue opening `.log` in `'w'` mode — so the toggle-on
  gesture still gives the user a clean log even if rotation
  couldn't preserve the prior session.

## 0.43.17 — drop the "Email daemon log" button from the settings UI

### Why

The settings UI had two side-by-side buttons for shipping the
daemon log: "Share daemon log" and "Email daemon log".

- **Share** reads the file directly from disk, includes the
  `=== current session (<path>) [daemon X.Y.Z] ===` header
  (with the daemon-version tag added in 0.43.8), and sends as
  a real file attachment via `Intent.ACTION_SEND` + MediaStore +
  `EXTRA_STREAM`. Arbitrary size, picker offers email apps
  alongside messaging / file-saver / cloud-paste.
- **Email** used `Intent.ACTION_SENDTO` with the log inlined into
  the body parameter of a `mailto:` URI. That made the log
  payload subject to (a) the daemon's 256 KB RPC truncation cap,
  (b) URI size limits in the email app and Android's URI parser,
  and (c) loss of the session-header decoration.

For "send the daemon log to the developer", **Share is strictly
better** — the user picks their email app from the chooser and
the attachment is a full-fidelity file. The Email button was
the strictly-worse path of the two; offering both was confusing
and invited testers to pick the worse one.

### Changes

`azt_collabd/ui/app.py`:

- KV layout: the `RecBtn` for `_('Email daemon log')` is gone;
  the remaining "Share daemon log" button stretches to fill the
  row.
- `email_daemon_log` method removed.
- `_dispatch_daemon_log` collapsed into `share_daemon_log`
  (single channel, no branch). Docstring rewritten to record
  the rationale so the next maintainer doesn't re-add an Email
  button without re-reading why it was removed.

`azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`:

- `msgid "Email daemon log"` entry dropped.

### Notes

- `azt_collab_client.ui.share.email_text` itself is **not**
  removed — peers may still want a "send the developer a short
  note" mailto-only path for other contexts. The helper just
  stops being called from `azt_collabd.ui.app`.
- No wire-format change. Pure UI cleanup.

## 0.43.16 — fix push regression introduced in 0.43.13's non-FF guard

### Regression

0.43.13 added a "safety" branch to the push retry loop in
`_push_step_locked`: when a non-FF rejection lands AND the
post-rejection fetch reports `new_remote == local_sha`, bail with
`PUSH_FAILED` rather than claiming `S.PUSHED`. The reasoning was
"non-FF but the server already shows our tip means we're in an
inconsistent state we can't recover from in-loop; claiming PUSHED
would silently lose data."

That reasoning was wrong. The actual common path that hits this
branch is adaptive batching: an earlier network failure halved
`target_sha` to an intermediate ancestor of `local_sha`. While
the loop is shrinking, the server (concurrent peer push, prior
in-flight commit, anything) advances to `local_sha`. The peer's
next attempt pushes `target_sha:refs/heads/<branch>`. Server
rejects: target_sha is an ancestor of the server's current tip,
so the push would move the ref backwards → non-FF rejection
shape. We re-fetch, `new_remote = local_sha` (matches local).

In that case **the server already holds `local_sha`** — we are
in sync. The pre-0.43.13 code correctly claimed `S.PUSHED` here.
0.43.13's bail turned this into a spurious failure, and a phone
that had been adaptive-batching its way through a slow link
would stop progressing.

### Fix

Removed the `if non_ff: bail` arm. The equal-local-new_remote
branch now claims `PUSHED` for *both* race and non-FF triggers —
which is correct, because in both cases the server's view of the
branch matches our local tip. Comment block at the call site
updated to record the 0.43.13 → 0.43.16 history so the next
maintainer doesn't reintroduce the same "safety" guard.

### Also: `new_remote is None` guard

0.43.13's gate change (`(new_remote and new_remote != remote_sha)
or non_ff`) could enter the reconciliation block with
`new_remote = None` when the only trigger was `non_ff` and the
local repo hadn't yet written `refs/remotes/origin/<branch>` (a
freshly-cloned-but-empty-tracking-ref state). The ancestor
checks below would then walk against `None` and raise. New gate:
`new_remote and ((new_remote != remote_sha) or non_ff)`. The
``new_remote`` truthy check now governs both arms, so `non_ff`
without a real tracking SHA falls through to the unfamiliar-
exception backoff path instead of crashing the loop.

### Notes

- No wire-format change. Pure daemon-side fix.
- The 0.43.13 `_format_push_error` rewrite (tuple-of-bytes
  → "remote rejected ref update (likely non-fast-forward …)") is
  unchanged and still in effect — that part of 0.43.13 was right
  on its own.
- A peer that had stopped syncing due to this regression should
  resume on the first user-initiated Sync gesture against a
  0.43.16+ daemon. No state to clear.

## 0.43.15 — daemon-log file captures everything (`stdout` + `stderr`), and the tee auto-installs on every `:provider` respawn

### Field report (and a finished thought)

A user with "Save daemon log to file" toggled on shared a log
whose only content was the banner line:

```
=== current session (.../daemon.log) ===
[01:51:44] [daemon-log] mirroring stderr to '…' (fresh session,
                                                 daemon 0.43.9)
```

Two distinct gaps explained that empty log; both are closed here:

1. **The tee only captured `sys.stderr`.** Boot-trace prints
   (`print(f'[boot-trace-daemon] phase=…')` in
   `server_apk/service.py` and `server.py`) go via `sys.stdout` by
   default. Most other daemon diagnostics use explicit
   `file=sys.stderr` — those *were* captured — but
   `[boot-trace-daemon]`, `[service] entering idle-stop loop`,
   and a scattering of other status prints went to logcat only.
2. **On Android, the tee only installed via the UI toggle and
   never on `:provider` boot.** `server.run()`'s
   `_maybe_install_stderr_tee` call is the loopback HTTP server
   path — never executed in the server APK's `:provider` process.
   So even with the persisted toggle on, every daemon respawn
   after an idle auto-stop started with no tee until the user
   re-touched the UI toggle.

### Changes

`azt_collabd/server.py`:

- `_StderrTee` → `_StdioTee`. Generalised to wrap any stream;
  two instances are installed in tandem (one over `sys.stdout`,
  one over `sys.stderr`) sharing a single underlying file *and* a
  shared `[bool]` start-of-line flag so per-line `[HH:MM:SS] `
  stamping stays correct when the two streams interleave.
- `install_stderr_tee` → `install_stdio_tee`: opens the log file
  once, installs both `_StdioTee` instances atomically.
- `uninstall_stderr_tee` → `uninstall_stdio_tee`: restores both
  originals and closes the file.
- `_maybe_install_stderr_tee` → `maybe_install_stdio_tee`. Public
  now (no leading underscore) because the Android service body
  calls it from outside this package.
- All four internal call sites updated; banner wording changed
  from "mirroring stderr to …" to "mirroring stdio to …".

`server_apk/service.py`:

- `main()` now calls `azt_collabd.server.maybe_install_stdio_tee()`
  immediately after `import azt_collabd`. This is the Android
  equivalent of `server.run()`'s desktop-path install. Subsequent
  `_boot_trace` lines (`configured`, `before_install_callbacks`,
  `after_install_callbacks`, `before_reconcile`, `after_reconcile`,
  `entering_idle_loop`) and every later `[recent]` / `[cawl]` /
  `[commit-*]` print land in the on-disk log alongside the existing
  stderr stream.
- Lines emitted *before* `import azt_collabd` (`module_loaded`,
  `main_entered`, `before_import_azt_collabd`) still only reach
  logcat. Capturing those would require a Python-side read of the
  toggle without `azt_collabd` imported — possible but adds
  duplication of path-resolution logic. Out of scope for this
  bump; the captured tail is the diagnostically valuable part.

### Notes

- No wire-format change; `MIN_CLIENT_VERSION` and
  `MIN_SERVER_VERSION` untouched.
- Existing `daemon.log` files keep working — the file format
  (per-line `[HH:MM:SS] ` prefix, plain text) is unchanged. Just
  more lines land in it now.
- Hot-toggle still works: flip "Save daemon log to file" off in
  the settings UI and *both* tees uninstall atomically; flip back
  on and they install together against a fresh-session log.

## 0.43.14 — revert 0.43.12 `_python_bundle/` wipe; shorten DoH negative cache

### Urgent: revert the 0.43.12 wipe

0.43.12 added a step to `SuiteSelfReplaceReceiver.onReceive` that
recursively wiped `files/app/_python_bundle/` after a package
replace, so the next spawn would force p4a to re-extract from the
new APK's assets. That theory was: any spawn that finds the dir
missing will trigger p4a's "extract from APK" branch.

**That's true for the Activity bootstrap but NOT for the
`:provider` service bootstrap.** P4a's service bootstrap expects
`_python_bundle/` to already be present and imports from it —
there is no extract-on-missing branch. After the wipe, every
lazy-respawn of `:provider` hit
`_python_bundle does not exist...should we expect a crash soon?`
in p4a's bootstrap, exited, and was respawned by `START_STICKY`
into the same broken state. Field log from a 0.43.12 install:

```
[python] _python_bundle does not exist…should we expect a crash soon?
[python] Initializing Python for Android   (next respawn, PID 1919)
[python] _python_bundle does not exist…should we expect a crash soon?
[python] Initializing Python for Android   (next respawn, PID 1944)
[python] _python_bundle does not exist…should we expect a crash soon?
```

Receiver reverted to its pre-0.43.12 three-step shape (per-PID
reap → `killBackgroundProcesses` fallback → self-kill). The
import and helper method added in 0.43.12 are gone. The class
Javadoc carries a NOTE explaining why the wipe is *not* there, so
the next person to try this re-derives the conclusion instead of
rediscovering the crash-loop.

The stale-unpack problem the wipe was meant to solve is still
real — `/v1/health` will keep reporting the old `__version__` on
a server-APK replace if only the Service has been respawned since.
A correct fix needs to also cause the Activity to run (so its
bootstrap re-extracts) or add an extract-on-missing branch on the
service side. Out of scope for this hotfix.

### Also: DoH negative-cache window shrunk 30 s → 5 s

`net.py:_DOH_NEGATIVE_TTL_S` controls how long
`_patched_getaddrinfo` remembers a failed Cloudflare-1.1.1.1
lookup before retrying. The pre-0.43.14 value was 30 s — wide
enough that a single transient DoH miss during a Starlink
satellite handover (~15 s) or a connectivity blip would silently
disable the fallback for a user-initiated push retry that arrived
within 30 s of the miss. Field reports of "DNS resolution failed"
in a loop, on networks where the user's browser kept working,
trace to this poisoning window.

5 s is the new value:

- Still long enough to absorb urllib3's in-loop retry storm
  (~3 attempts within ~2 s) so we don't pay the 2.5 s DoH timeout
  on every one.
- Short enough that any human-perceived retry (tap Sync, see
  error, wait, tap Sync again) gets a fresh DoH probe.
- Background `_has_internet()` ticks still debounce via the
  positive-cache TTL (300 s) once a probe actually lands.

Comment block updated to spell out the Starlink-handover
rationale so the next maintainer doesn't widen the window again.

### Notes

- Hotfix release; ship over 0.43.12 / 0.43.13 directly. Users
  caught in the 0.43.12 crash-loop install **this** APK and the
  next replace's receiver runs from the new code (Step 1 reaps
  the surviving `:provider`, Step 3 self-kills), then peer's next
  call lazy-spawns a clean `:provider` against the existing
  `_python_bundle/` — which is still the old version's code, but
  at least it's not crash-looping. Versions reconcile naturally
  as Activity boots happen.
- Carries the 0.43.13 work below (push error formatting +
  non-fast-forward recovery) since 0.43.13 was never released.

## 0.43.13 — diagnosable `PUSH_FAILED` messages + non-fast-forward push recovery

### Field report

Peer log on a phone running a couple versions behind:

```
Échec de l'envoi : (b'810ef46cd8378ca8e0a199a54fd2d765035d706d',
                    b'd7d4c0b95b76977b15ec39da109f77aa9917e7a0')
```

The raw `(b'old_sha', b'new_sha')` repr is dulwich
`UpdateRefsError`'s default `__str__` when its `args` is a tuple
of byte SHAs (the (old, new) pair the server reported as
rejected) and the exception has no override. `repo.py`'s
`_add_push_failure` was calling `str(exc)` directly, so the
useless tuple landed in `Result.params['error']`, was passed
through `_('Push failed: {error}')` → `Échec de l'envoi : {error}`,
and reached the user as a meaningless line. Worse, the underlying
cause (almost always non-fast-forward) was invisible to anyone
reading the log.

### Two fixes

**1. `_format_push_error(exc)`** in `azt_collabd/repo.py`. When
`str(exc)` is the bytes-tuple shape, rewrite it as `'remote
rejected ref update (likely non-fast-forward — remote has commits
not present locally): <raw>'`. Falls through to `str(exc)` for
informative shapes (network errors, HTTP 4xx). Wired into
`_add_push_failure`, the `PULL_FAILED` site after the initial
fetch, and the `_merge_diverged` failure path. Net result: every
`Result.params['error']` from a push/pull failure is now a
sentence a user (or maintainer reading a shared log) can act on.

**2. `_is_non_ff_rejection(exc)` + retry-loop gate change.** The
push retry loop already re-fetches after a failure and reconciles
when `new_remote != remote_sha` (race-with-concurrent-pusher).
The gate was too conservative for the case observed here: a
genuine non-fast-forward where our `refs/remotes/origin/<branch>`
cache happens to already match the server's tip (we fetched on a
prior iteration but the merge never happened, or we're being
adversarially-served stale fetch responses, etc.). The rejection
itself proves the server has something we don't, regardless of
what the mirror ref says.

New gate: `(new_remote and new_remote != remote_sha) or
_is_non_ff_rejection(exc)`. When non-FF is detected, the loop
enters the same four-case reconciliation
(equal / remote-ancestor / local-ancestor / diverged) used for
the race case, with one extra safety branch: in the
`local_sha == new_remote` arm, if the trigger was a non-FF
rejection (not a race), the loop bails with the formatted error
instead of claiming `S.PUSHED` — claiming success on a
genuinely-rejected push would silently lose the data on the next
round.

### Notes

- Pure daemon-side fix; no wire-format change, no peer rebuild
  required. The corrected error string travels through the
  existing `Result.params['error']` slot and the existing
  `_('Push failed: {error}')` translation site, so any peer at
  any version sees the improved message as soon as the daemon
  serving them is 0.43.13+.
- The retry loop's `MAX_CONSECUTIVE_FAILURES = 12` cap is
  unchanged; non-FF detection just shortens the cap-burning loop
  in the case where reconciliation would have succeeded if we'd
  let it run.

## 0.43.12 — `SuiteSelfReplaceReceiver` wipes the stale p4a unpack on package replace

### The replace-but-stale-Python loop, finally

Symptom: every server-APK update left `/v1/health` reporting the
*old* `__version__` from on-device Python code, even though the
new APK was demonstrably on disk and PackageManager reported its
new `versionName`. The peer's bootstrap correctly detected the
mismatch (`installed > running`) and popped the "Restart your
device" loop on every install — even after force-stopping
`:provider`, even after the receiver fired and reaped the daemon
process.

### Root cause

p4a's bootstrap unpacks the APK's bundled Python code to
`files/app/_python_bundle/` on the Activity's first launch, then
short-circuits subsequent launches whenever the dir already
exists — including launches from a different (newer) APK after a
package replace. The ContentProvider's `:provider` service
bootstrap doesn't unpack at all; it just imports from
`_python_bundle/site-packages/`. So a replace install left the
on-disk APK at version N+1 while every lazy-respawned `:provider`
kept reading version N's Python code from the stale unpack
directory, and `/v1/health` forever reported N. Force-stopping
`:provider` didn't help because the lazy-respawn loaded the same
stale dir; the receiver firing didn't help because all it did was
kill processes that were going to be respawned against the same
stale dir.

Smoking gun: aapt-dumping the installed APK's manifest at 0.43.11
returned `versionName=0.43.11`, while the running daemon's
`/v1/health` reported `0.43.9` — same UID, two different versions
of the same code.

### Fix

`SuiteSelfReplaceReceiver` now wipes `files/app/_python_bundle/`
as Step 1 of `onReceive` (before the process kills below). The
wipe runs synchronously before `onReceive` returns; existing
interpreters in memory are unaffected (their code is already
loaded), but the next lazy-spawn — Activity OR Service — finds
no bundle and falls into p4a's C bootstrap "no bundle present,
extract from APK assets" branch. Existing receiver work (per-PID
kill, `killBackgroundProcesses`, self-kill) follows unchanged,
renumbered to Steps 2–4. Receiver's Javadoc rewritten to
document the unpack semantics so the next person reading the
file understands why Step 1 exists.

`deleteRecursive` helper added; same-UID file deletion needs no
permission. Logs success/failure under the existing
`SuiteSelfReplace` tag so a tester's logcat answers "did the
wipe land" definitively.

### Notes

- No wire-format change; `MIN_CLIENT_VERSION` and
  `MIN_SERVER_VERSION` untouched (both still 0.43.11 from the
  test-scaffolding bump in 0.43.10).
- The receiver lives in every suite APK via the existing
  `add_src` path. Server APK gets the load-bearing path
  (wipe + `KILL_BACKGROUND_PROCESSES` permission); peer APKs
  get the wipe + a no-op kill fallback, which is harmless: a
  peer that updates while its own `:provider`-equivalent is
  alive doesn't exist (peers have no long-lived service
  process).
- Takes effect on the install of *this* APK: Android registers
  the new APK's components before dispatching
  `MY_PACKAGE_REPLACED`, so it's the new (wipe-enabled) receiver
  that runs during this very replace. No manual workaround
  needed for this update or any future one.

## 0.43.9 — ContentProvider transport absorbs the daemon cold-spawn race transparently

### Peer first-launch crash after server-APK reap

Field report: peer launched, "load and crash"; tapping the icon a
second time worked. Logcat for the working launch shows
`[boot-trace-daemon] phase=before_install_callbacks t=0.878` →
`phase=after_install_callbacks t=1.925` — a ~1 s window during
which Android has registered the `org.atoznback.aztcollab`
ContentProvider authority but the Python body inside `:provider`
hasn't yet installed the dispatch callback. The Java side returns
a null `Bundle` for any peer call during that window.

Bootstrap's existing fast-fail policy treats three consecutive
`null_bundle` errors as a structural signature/install failure
and renders the "AZT Collaboration is unresponsive" popup
(`_NULL_BUNDLE_FAIL_FAST = 3`, adaptive backoff
`(0.2, 0.4, 0.8) s` — total 1.4 s). That's *less* than the
~1.9 s cold spawn the new (0.43.7) `SuiteSelfReplaceReceiver`
makes more common: reliable reaping of `:provider` during the
suite-replace receiver means the next peer call lazy-spawns
from scratch instead of finding a survivor. Pre-0.43.7, the
old daemon process limped along across the APK replace and the
peer never hit the cold-spawn window — the same code path
that now fails was effectively gated behind a now-fixed bug.

Even after bootstrap, post-launch RPCs had no retry on
`null_bundle` at all — they went through `rpc.call`'s single
re-discover and raised. If the daemon idle-stopped while the
peer was open (5 min default), the next main-thread call would
crash the peer the same way.

### Fix: transparent retry inside the ContentProvider transport

`azt_collab_client/transports/android_cp.py`:

- `AndroidContentProviderTransport._raw_call` now retries on
  `bundle is None` with backoff
  `(0.1, 0.2, 0.4, 0.8, 1.6) s` — 3.1 s total budget. Covers
  the observed cold-spawn import (~1.9 s) plus margin. Safe
  for POSTs as well as GETs: a null Bundle means the Python
  dispatch never ran, so no work was done on the daemon side.
  Records `null_retries=N` on the `transport.call.post` first-
  try log line for diagnostic visibility.
- `discover()` applies the same backoff to its initial `ping`
  with delays `(0.0, 0.2, 0.4, 0.8, 1.6) s`, since the discovery
  ping is frequently the call that *causes* the lazy spawn.

`null_bundle` only escapes the transport now after the 3 s
budget is exhausted — i.e. when the failure really is
persistent (signature-grant denial, authority gone). Bootstrap's
existing fast-fail on the kind still works for that case;
docstring in `transports/__init__.py` updated to spell out
that the kind is now reserved for genuinely unrecoverable
failures.

### Notes

- Pure client-side fix; no daemon changes, no wire-format
  change, `MIN_CLIENT_VERSION` untouched.
- Peer rebuild required to pick up the fix (the symlinked
  `azt_collab_client` is consumed at peer-build time). Running
  daemon doesn't need to be updated.
- The 3 s budget is paid on the slow path only; healthy
  steady-state calls return on the first attempt with zero
  overhead.

## 0.43.8 — `_touch_project` no longer rewrites `config.json` on every hot endpoint; daemon log gets per-line timestamps and a version-tagged share header

### Phone-slowness fix

Field report: phone visibly slow while working through a CAWL pass.
Daemon log shows the cause — `_touch_project('baf')` firing 10–15× per
displayed entry (one per `cawl_image_bytes`, one per `project_status`
poll, one per `get_audio`, …), and **every** call rewrites
`$AZT_HOME/config.json` to disk (load → atomic temp + rename) and
emits a `[recent]` log line that the daemon-log mirror also writes to
disk. On Android internal storage that is the dominant per-action
cost.

The diagnostic comment at `_h_cawl_cache_status` already noted that
the 1 Hz prefetch-progress poll was intentionally excluded from
`_touch_project` for this exact reason, but every other langcode-bound
endpoint kept paying the toll.

Fix in two layers:

1. **`server._touch_project`** now reads
   `store.get_last_langcode()` first and short-circuits the disk
   write + log line when the value is already current. The cheap
   `auto_prefetch` re-trigger still fires (it has its own 30 s
   per-repo throttle and is what drives circuit-recovery when
   connectivity returns), so behaviour is unchanged for any caller
   that wasn't already in the "stamp the same value again" steady
   state.
2. **`store.set_last_langcode`** gained a module-level
   `_last_langcode_cache` that holds the last successfully-stamped
   value in memory. Writes that match the cache are a no-op (no disk
   read, no disk write); writes that don't match update the cache
   in lock-step with `_save_config_file`. `get_last_langcode` reads
   from the cache so the server-side check above is also free.

Defense in depth: either layer alone would eliminate the flooding;
both together mean any future caller (settings UI, picker, sister
app) gets the same short-circuit without remembering to apply it.

### Daemon-log diagnostic enhancements

Two readability gains for testers / maintainers reading
`daemon.log` after the heartbeat traffic went quiet:

1. **Per-line timestamps in the file mirror.** `_StderrTee` now
   prefixes each new line written to the daemon-log file with
   `[HH:MM:SS] `. Original stderr (logcat / terminal) is left
   unchanged — logcat already supplies its own time column, and
   double-stamping would just clutter `adb logcat` output.
   Implementation tracks an at-start-of-line flag across calls
   so a `print(x)` that lands as separate `write('text')` +
   `write('\n')` calls still gets exactly one stamp at the head
   of the line.

   Motivation: with `_touch_project` no longer firing on every
   RPC, the remaining log traffic doesn't supply a passage-of-
   time signal on its own. The stamp closes that gap — a tester
   can now see "5-second gap here, then a burst" without
   correlating against external state.

2. **Server version in the daemon-log banner _and_ the share
   header.** `install_stderr_tee`'s on-start banner now reads
   `(fresh session, daemon 0.43.8)` / `(appending — daemon
   0.43.8 respawn)`, so the on-disk file is self-describing.
   The `share_log_file` bundle header also reads
   `$AZT_HOME/server.json :: version` and renders the
   current-session line as
   `=== current session (<path>) [daemon X.Y.Z] ===`. Falls
   back silently to the path-only form if `server.json` isn't
   readable.

### Notes

- No wire-format change; `MIN_CLIENT_VERSION` is untouched.
- Sole on-disk effect: `config.json :: recent.last_langcode` now
  gets one write per actual project switch instead of one per
  langcode-bound RPC. The persisted value is identical.
- `[recent] _touch_project` lines in the daemon log are now a useful
  signal again — one per real change — rather than a per-call
  heartbeat.

## 0.43.7 — `SuiteSelfReplaceReceiver` actually reaps the `:provider` process; `azt_collab_client/CLAUDE.md` split into rules + `docs/rationale/`

### Doc reorganisation: rules vs. rationale

`azt_collab_client/CLAUDE.md` had grown to ~900 lines, mostly
"architectural rationale" sections that triggered "large file
will impact performance" notices on every boot. Split along the
existing rationale-section headers:

- `azt_collab_client/CLAUDE.md` (now ~315 lines) holds the
  **rules / invariants / hard contracts** — hard rules,
  daemon-owned state, transport facade, public API surface
  rules, status-code contract, the new index of rationale
  files. Reading just this file is sufficient to avoid
  breaking the client.
- `azt_collab_client/docs/rationale/<topic>.md` holds the
  *why* behind each subsystem. Eight files: `sync.md`,
  `lift_access.md`, `cawl.md`, `i18n.md`, `identity.md`,
  `ui.md`, `lowpower.md`, `project_switch.md`, plus a
  `README.md` index in the same directory.

New hard rule (#9 in CLAUDE.md): **rules live in CLAUDE.md;
rationale lives in `docs/rationale/`**. Rules never go into
rationale files. If a "do X / don't do Y" emerges from a
why-discussion, it goes up into CLAUDE.md (or
CLIENT_INTEGRATION.md if it's a peer contract) with the
rationale file providing the justification via a forward link.

Net effect: every daily session boot loads ~315 lines instead
of ~900; subsystem rationale is one Read away when needed.
Convention previously recorded in memory as "CLAUDE =
philosophy / rationale / architecture" supersedes to "CLAUDE
= rules / invariants; rationale lives in docs/rationale/".

### `SuiteSelfReplaceReceiver` actually reaps the `:provider` process

**Motivation.** Sideloading the server APK via `adb install -r` (and
every other install pathway) left the old daemon process running.
Symptom: the "Restart your device to switch to the newer version"
popup (`_prompt_server_reboot_to_apply` in
`azt_collab_client/ui/bootstrap.py`) fired on every install, despite
the receiver having been in place since 0.41.28.

**Diagnosis.** `SuiteSelfReplaceReceiver.onReceive` called
`ActivityManager.killBackgroundProcesses(getPackageName())`. That API
only kills processes whose importance is at or below the BACKGROUND
threshold. The server APK's `:provider` process hosts
`AZTServiceProviderhost` as a `START_STICKY` started service
(`AZTServiceProviderhost.java:121`), which pins the process at
`IMPORTANCE_SERVICE` — *above* the background threshold. So
`killBackgroundProcesses` was a documented no-op against the very
process the receiver was meant to reap. The new APK's bytes were on
disk, but every peer ContentResolver call routed into the still-alive
old `:provider` interpreter, which kept reporting the old
`__version__` from `/v1/health`, which tripped the installed-vs-
running mismatch in `_prompt_server_update`.

It worked occasionally — but only on OEMs that aggressively kill
all processes of a replaced package during install. That was
incidental, not contracted.

**Fix.** `SuiteSelfReplaceReceiver` now enumerates running processes
via `ActivityManager.getRunningAppProcesses()` (which always returns
the caller's own same-UID processes) and calls
`android.os.Process.killProcess(pid)` on each non-receiver PID
before falling back to `killBackgroundProcesses`. Same-UID
`killProcess` needs no permission and ignores process importance,
so a `START_STICKY`-pinned `:provider` gets reaped directly. The
existing `killBackgroundProcesses` call is kept as a belt-and-braces
fallback for anything enumeration might miss but is in a reapable
state.

**Files.**

- `android/src/main/java/org/atoznback/aztcollab/SuiteSelfReplaceReceiver.java`
  — three-step body (per-PID kill, then `killBackgroundProcesses`,
  then self-kill); top-of-file Javadoc rewritten to spell out the
  importance-state gotcha so the next person reading the file
  understands why step 1 exists.

**No wire change; `MIN_CLIENT_VERSION` unchanged.** Build-tooling
unchanged: the receiver lives at `android/src/main/java/...`,
compiled into every APK via `android.add_src`; the manifest
`<receiver>` and the server-APK-only `KILL_BACKGROUND_PROCESSES`
`<uses-permission>` injection in `p4a_hook.py:_inject_self_replace_receiver`
both stay (the permission still backs the step-2 fallback on the
server APK).

**User-visible effect.** Once every suite APK in the field is at
0.43.7+, the reboot-to-apply popup becomes unreachable on
package-replace as originally designed. Pre-0.43.7 installs still
need the transitional popup; that branch stays in
`_prompt_server_update`.

## 0.43.6 — `recent.last_langcode` empty-state invariant; first-boot picker-cancel carve-out for `App.stop()`

**Motivation.** Field report 2026-05-18 — user toggled daemon logging
on, asked to switch projects, peer GET ``/v1/recent/last_project`` →
~2 ms later Kivy logged ``[INFO] [Base] Leaving application in
progress... Python for android ended.`` The peer's ``on_resume`` had
interpreted an unexpected ``last_project() == ''`` as "no project
loaded" and called ``App.stop()`` while a sync was mid-flight. Daemon
process kept running and finished the in-flight ``[commit]`` 250 ms
later, but the user perceived the whole thing as a crash.

Diagnosis showed the empty return could in principle leak from a
transient I/O hiccup, a malformed RPC, or a peer accidentally
sending empty — and nothing in the daemon refused to land ``''`` on
disk. This release closes that escape path.

**Daemon-side invariants.**

- ``store.set_last_langcode()`` **refuses empty input** (warns to
  stderr and no-ops). No legitimate caller passes empty;
  defense-in-depth against transient bugs that could otherwise mimic
  a user gesture.
- ``POST /v1/recent/last_project`` with empty body returns
  ``400 empty_langcode``. No legitimate peer issues that POST —
  picker-cancel is a no-op end-to-end, not an RPC. If a peer is
  sending empty, surface the bug rather than silently absorbing.
- ``_h_get_last_project`` emits a one-line transition log on every
  observed change to the returned value (``'<prev>' → '<new>'``,
  sentinel ``'<unset>'`` differentiating the first call per
  process). High-frequency 1 Hz poll path stays log-free; transitions
  give us the diagnostic shape we'd want if this regression ever
  recurs in the field.

**Peer-side contract revision (`CLIENT_INTEGRATION.md` § 14a).**
``last_project()`` returns ``''`` legitimately exactly once in a
device's lifetime — first boot, before any project has ever been
touched. Picker-cancel is a no-op: the picker issues no RPC and no
write, the daemon's ``last_langcode`` is unchanged across the
gesture, and the ``on_resume`` comparison naturally no-ops because
the peer's ``_current_langcode`` is unchanged too. Don't add a
"clear" RPC for picker-cancel; don't write ``''`` from any path.

**One ``App.stop()`` case is carved out: first-boot picker-cancel.**
Fresh install, peer has no ``_current_langcode``, bootstrap opens
the picker, user backs out without picking. The peer has literally
nothing to display and the user has signaled "not now." ``stop()``
is correct *only* in that state — the discriminator is the peer's
own ``_current_langcode is None`` plus a non-selection return from
the picker, not the empty return from ``last_project()``. The
May-18 failure mode (peer calling ``stop()`` with a loaded project
on its way to a project-switch reload) remains banned; the
"What NOT to do" section in § 14a is updated to reflect the
exception explicitly.

No wire-format change. No new status codes. The transition log line
is the only new diagnostic surface; it costs one stderr write per
actual change, which on Android adds maybe ten lines per session.

### azt_collabd 0.43.5 / azt_collab_client 0.43.5 — DoH fallback resolver; DNS_RESOLUTION_FAILED status; watcher logs resolver path on online edge

**DNS-over-HTTPS fallback resolver (`azt_collabd/net.py:_patch_resolver`).**
Field reports keep showing the same pattern: the user can browse the
internet but `dulwich` raises ``NameResolutionError: Failed to resolve
'github.com' ([Errno 7] No address associated with hostname)``. The
distinguishing characteristic is that the failure mode is **app-
specific** — system browser is fine, Python's resolver path is not.
Causes range from per-app data restrictions, captive-portal Wi-Fi in
limbo, broken Private DNS, IPv6-only networks without DNS64, stale
negative caches after a brief outage. Bad-internet resilience is a
primary requirement of the suite (West / Central African field
deployments routinely hit this), and the previous behaviour was just
to surface PUSH_FAILED and hope the user knew to wait.

This release installs a fallback resolver as a side effect of
``_ensure_ssl()`` (which already ran before any dulwich op):
``socket.getaddrinfo`` is monkey-patched. The system resolver runs
**first**, unchanged — on a healthy network the patch is a passthrough
and the DoH dependency is dormant. On ``socket.gaierror``, the patch
issues one DoH JSON query to Cloudflare at the **literal IP**
``https://1.1.1.1/dns-query`` (cert SAN covers ``1.1.1.1`` so TLS
validates against the certifi roots without itself needing DNS; the
literal-IP form makes the DoH path itself loop-free because
``getaddrinfo('1.1.1.1', 443)`` is satisfied by libc without
triggering DNS). Both ``A`` and ``AAAA`` records are queried — the
synthetic ``addrinfo`` list returns AAAA records first to mirror the
order a v6-preferring stack would produce, so urllib3 / socket's
sequential iteration gets a happy-eyeballs-shaped retry path.

Results are cached for 5 minutes keyed by ``(host, port)`` so the
push-retry loop's reconnection attempts don't refire DoH on every
iteration. The lookup is gated on the host *looking like* a hostname
(numeric IPs and AF_UNIX paths bypass), and the patch never
substitutes for the system answer — if system DNS returns a valid
empty answer for a name that just doesn't exist, the DoH path doesn't
run. A new ``_RESOLVER_STATE`` module-global records which path served
the last lookup; ``azt_collabd.net.resolver_state()`` exposes it as
``'system' | 'doh' | 'fail' | 'unknown'``.

**Connectivity watcher logs the resolver path on online edges.**
``scheduler._watcher_loop``'s offline → online edge handler emits
``[watcher] online edge — resolver path: <path>`` so a developer
skimming a field log can tell whether the DoH path was load-bearing
during the session. Healthy networks always show ``system``;
``doh`` flags resolver-class fragility that's silently being routed
around. No new log volume on per-tick basis — edges only.

**New status: ``DNS_RESOLUTION_FAILED``.** Mirrored in both
``azt_collabd/status.py`` and ``azt_collab_client/status.py`` (per the
mirror discipline of hard rule #3), with a translation in
``azt_collab_client/translate.py``. Emitted from
``_add_push_failure(result, exc)`` in ``repo.py`` alongside the
existing ``PUSH_FAILED`` when the exception string matches DNS markers
(``nameresolutionerror``, ``no address associated``, ``failed to
resolve``, etc.) — meaning *both* system DNS and the DoH fallback
failed for the same hostname. Distinct from ``PUSH_FAILED`` so peers
can route silently in the auto-sync path (per the auto/user contract
in ``azt_collab_client/CLAUDE.md`` § "Peer contract: why auto-sync
must be silent"). On user-initiated Sync the peer should toast the
translated message ("Network reachable, but the sync host could not
be resolved…") without navigating — there's no settings change that
will fix this; the daemon will retry automatically when the
underlying network state clears.

**What this does not do** (documented in the README's *DNS
resilience* section): it doesn't fix networks that are genuinely
offline, doesn't bypass per-app firewalls that block 443 outbound,
and doesn't catch the case where DNS succeeds but the resulting IP
is unreachable. Those remain visible as ``PUSH_FAILED`` on the next
sync attempt.

No wire-format change. ``MIN_CLIENT_VERSION`` stays at ``0.43.4``
on the daemon; an older client receiving ``DNS_RESOLUTION_FAILED``
falls through ``translate_status``'s default branch and renders
``[DNS_RESOLUTION_FAILED] {}`` — ugly but non-fatal, and exceptional
in practice (the DoH path makes the underlying NameResolutionError
class of failure rare).

### azt_collabd 0.43.4 / azt_collab_client 0.43.4 — Push-retry no-op merge guard + FF case; settings UI shows the daemon's actual version; collab settings reorder; adaptive push batching for bad internet; one canonical version

**Single source of truth for the suite version.** The two
``__version__`` literals diverged in the field (2026-05-18:
daemon at 0.43.3, client at 0.43.1 → settings strip read
``client 0.43.1  ·  server 0.43.3`` and testers thought the
build was incomplete). Rather than keep two literals + a
"remember to bump both" discipline + a UI explanation for
why they're shown separately, the version now lives in one
place:

- Canonical literal: ``azt_collab_client/__init__.py``
  (``__version__ = "0.43.4"``).
- Re-export: ``azt_collabd/__init__.py`` does
  ``from azt_collab_client import __version__`` so anything
  reading ``azt_collabd.__version__`` keeps working
  unchanged.
- External tooling pointed at the canonical: buildozer
  (``server_apk/buildozer.spec.tmpl :: version.filename``)
  and the presplash generator (``appinfo.py ::
  FILE_W_VERSION``) both read from
  ``azt_collab_client/__init__.py`` directly via regex / file
  read.
- Import direction stays correct: daemon imports client
  (allowed); client never imports daemon (hard rule per
  ``azt_collab_client/CLAUDE.md`` — would break the
  "client works in a separate APK from the daemon" guarantee).

The previously-stated "patch-level bumps in one without the
other are fine" rule is retired — there's literally only one
``__version__`` to bump now. CHANGELOG headers continue to
list both package names (``azt_collabd X.Y.Z / azt_collab_client
X.Y.Z``) as a readability courtesy; the numbers always match.

Surfaced by a field daemon log on 2026-05-18: a transient
``RemoteDisconnected`` on push, followed by GitHub returning
HTTP 400 on the immediate retry, cascaded into **two
unnecessary "merge" commits** before the third push attempt
succeeded. The pre-push branch correctly identified
``local ahead of remote`` (so no initial merge), but the
retry-after-push-failure branch unconditionally re-merged on
every attempt regardless of ancestor relationship, producing
no-op merge commits (two parents, zero files changed) and an
extra push round-trip for each.

- **Retry path uses the same 4-case structure as pre-push.**
  ``_push_step_locked``'s push-retry block now distinguishes:
  (a) remote unchanged → push retry only; (b) local ahead of
  remote (the field log case) → push retry only; (c) remote
  advanced past local → fast-forward local before retry,
  otherwise the next push is non-FF rejected; (d) truly
  diverged → merge. Previously case (b) was guarded but case
  (c) silently fell through to "skip merge, push stale local
  SHA" which would have non-FF rejected on every subsequent
  retry. Diagnostic from the field:
  ``[sync-trace] retry merge done merged_sha=…`` appearing
  with ``base=N ours=N theirs=N`` (all equal) was the
  signature; new traces are
  ``[sync-trace] retry: remote unchanged, push retry only``,
  ``[sync-trace] retry: local still ahead of remote, push
  retry only``, ``[sync-trace] retry: remote advanced;
  fast-forward local``.
- **``CollabUIApp`` version strip now reflects the running
  daemon, not the UI subprocess's compile-time version.**
  Previously the bottom-of-settings ``client X · server Y``
  string used ``azt_collabd.__version__`` from the UI process,
  which equalled the daemon's version only on a coherent
  install — the moment a user updated one half (e.g. ran an
  in-place ``adb install -r`` on the server APK while a
  desktop settings UI was still pointed at an older daemon),
  the strip silently lied. ``on_start`` now spawns the same
  ``_probe_server_version`` worker the picker app uses (off
  the UI thread), calling ``check_server_compat()`` and
  rendering the daemon's reported version into the strip;
  ``server ?`` until the probe lands, ``server ? (reason)``
  if the daemon is unreachable / too old / too new. Field
  utility: lets a maintainer answer "what server is actually
  running?" without adb-shelling into the device.
- **Collab settings page reorganised.** Three field-driven
  changes to the in-daemon Settings page:
    1. **Interface language at the top, no section header.**
       The language-switcher row is the page's first widget
       (after the optional back button) — the row of language-
       name buttons is self-evident, the ``Interface
       language`` SectionLabel was visual noise.
       Share/Update follow, then the contributor field.
    2. **New ``Servers`` section groups everything that talks
       to the network.** GitHub button, GitLab button, the
       conditional Publish row, the ``Work offline:`` toggle,
       and the ``Cache images:`` toggle now sit together under
       one header. Previously Work offline + Cache images had
       their own SectionLabels, a "Suppress push:" prefix row,
       a "Wordlist images" header that dynamically appended
       the wordlist name, and a multiline status BodyLabel
       underneath — all collapsed to a single line each. The
       row's GREEN-vs-SURFACE highlight on yes/no continues
       to show the active state; the status BodyLabels were
       redundant.
    3. **``GitHub Settings`` button reflects daemon answer on
       first open, even on a cold daemon.** Previously the
       button label was driven by a single try-block holding
       both ``get_credentials_status()`` and ``is_online()``;
       any exception in either RPC bailed the whole refresh
       and left the KV-default ``Connect to GitHub`` label in
       place even when the daemon eventually answered
       ``confirmed=true``. Field symptom: button state on
       first open of the settings page was inconsistent
       across launches. Split into two independent try blocks
       so the buttons always update on whatever credentials
       info we have; ``is_online`` failure now degrades the
       Status block but not the action buttons. Plus: detect
       the ``ServerUnavailable`` fallback dict (no
       ``confirmed`` key under ``github``) and schedule one
       follow-up ``refresh()`` 1.5 s later so the daemon-cold-
       start race resolves itself silently.
    4. **Daemon log no longer wiped by daemon respawn.**
       ``install_stderr_tee`` opened the file in ``'w'`` mode
       on every install, including the
       ``_maybe_install_stderr_tee`` call at daemon startup
       — so an idle auto-stop (5 min) or OOM-kill of the
       server APK's ``:provider`` process followed by a
       lazy-respawn for the next peer RPC truncated the file
       and left only the freshly-printed ``[daemon-log]
       mirroring stderr to …`` line. Field symptom: testers
       reported "I turned the log on, used the app, hit
       Share — got one line." Fix: ``install_stderr_tee``
       gained a ``truncate`` kwarg (default ``False`` →
       append mode); only the explicit user toggle-on path
       (``_h_set_daemon_log_to_file(enabled=True)``) passes
       ``truncate=True`` to start a clean session. The
       mirroring-stderr line is annotated ``(fresh
       session)`` vs ``(appending — daemon respawn)`` so a
       reader can tell which install they're looking at.
       ``_h_get_daemon_log`` already caps reads to the last
       256 KB, so unbounded growth across many respawns is a
       non-issue.
    5. **Debug "Toggle service not responding" surface
       removed from the Settings page.** The
       ``$AZT_HOME/_debug_force_503`` sentinel daemon-side
       (``server.py:_h_health``) is unchanged — testers can
       still plant it via ``adb shell run-as
       org.atoznback.aztcollab touch
       files/azt/_debug_force_503`` — and the
       ``toggle_debug_503`` / ``_refresh_debug_503_state`` /
       ``_debug_503_path`` methods on ``SettingsScreen`` stay
       for REPL-callable convenience and future UI re-add.
       Just the KV widgets (SectionLabel + NavBtn + state
       BodyLabel) are gone — debug clutter the typical user
       never wanted to see.
- **Client conformity contract:
  ``CLIENT_INTEGRATION.md`` § 17b adds the "Badge refresh
  obligation".** Daemon has no peer-push channel; in the
  split-commit world peers MUST re-call
  ``project_status(langcode)`` after every sync gesture, on a
  5-15 s background tick, and on ``on_resume``. The gesture's
  own ``Result`` no longer encodes push state (push happens
  on the daemon's drain loop, not in the gesture's
  transaction), so peers that read ``commits_ahead`` only
  from the gesture result leave the badge stuck at
  last-gesture-time forever. Closes a 2026-05-18 field
  report where a peer's ``(+160)`` indicator persisted
  despite the daemon log showing successful
  ``[sync-rpc] done: codes=['NOTHING_TO_COMMIT', 'PUSHED']``.
  Migration-checklist item 5 added.
- **Adaptive push batching for bad-internet large queues.**
  ``_push_step_locked`` now starts with the historical
  single-transaction push of the local tip — no extra
  round-trips on the happy path. On a network-class push
  failure (``RemoteDisconnected`` / ``IncompleteRead`` /
  ``NameResolutionError`` / GitHub's ``unexpected http resp
  4xx`` from a server-side timeout on a slow upload), the
  loop halves the commit count it tries to push next: parks
  an intermediate SHA under ``refs/azt-collab/partial_push``
  and pushes that. Halves again on further failure. Once a
  partial push lands, locks in the working batch size and
  drains the remaining chunks at that size until the queue
  clears. Exponential backoff (1 s → 16 s cap) between
  retries. Bounded by 12 consecutive failures (resets on any
  successful push) — comfortably above ``log2(160)`` for
  queues of 160+ commits with retry headroom. The
  race-with-another-peer's-push path (re-fetch shows the
  remote moved) is preserved as a parallel branch: when
  detected, the four-case ancestor logic resolves it
  (FF / merge / no-op / already-in-sync) and the adaptive
  loop resets to push the new tip from scratch. Helpers
  added: ``_is_network_push_failure(exc)``,
  ``_count_commits_between(repo, ancestor, descendant)``,
  ``_pick_intermediate_sha(repo, base, tip, n)``. New
  trace lines: ``[sync-trace] push loop begin
  commits_to_push=N``, ``[sync-trace] push attempt
  target=<8hex> chunk_n=N``, ``[sync-trace] retry: halving
  chunk_n N → N/2``, ``[sync-trace] batch size locked at
  N``. Closes the low-power-phone-on-bad-internet failure
  mode where a 160-commit backlog produced a single ~10 MB
  pack that GitHub's git-receive-pack timed out on, with no
  recovery short of finding better Wi-Fi.
- **Client conformity contract: ``CLIENT_INTEGRATION.md``
  § 14a now explicitly prohibits ``App.stop()`` as the
  project-switch reload mechanism.** Closes a 2026-05-18
  field report: user enabled daemon logging, asked to switch
  projects, peer ``GET /v1/recent/last_project`` → 2 ms
  later Kivy logged
  ``[INFO] [Base] Leaving application in progress... Python
  for android ended.`` Daemon process kept running and
  finished the in-flight commit 250 ms after the peer
  exited; user perceived it as "the app crashed when I
  switched projects." Android does NOT auto-restart a peer
  that exits via ``App.stop()``, so the user has to
  relaunch from the home screen every time they switch. The
  contract already documented "reload via the same code
  path the picker-result handler uses"; the new explicit ❌
  bullet names the anti-pattern and the field-log signature
  so the next peer audit catches it.
- **CAWL: root-level images no longer log spurious "flat
  basename not in index".** ``_resolve_basename_via_index``
  returned the same string in two distinct cases — "matched
  but the canonical path *is* the basename" (e.g.
  ``Image-Not-Found.png`` at the top of
  ``kent-rasmussen/images_CAWL``) and "no entry matched" —
  and the caller in ``get_image_path`` couldn't distinguish
  them. Every fetch of a root-level image logged ``[cawl]
  get_image_path: flat basename not in index: …`` even
  though the asset was in the index and the subsequent fetch
  succeeded. Field log 2026-05-18 showed the spurious line
  with no follow-up ``image fetch failed``, exactly because
  the asset was present. Fix: ``_resolve_basename_via_index``
  now returns ``(resolved_path, found)``; the caller logs the
  "not in index" line only when ``found is False`` and a
  cached index exists. Root-level matches stay silent. Six rules for
  how peers must shed load instead of leaning on daemon-side
  protections: single-in-flight guard per (RPC, project);
  auto-paths use ``commit_project`` not ``sync_project``;
  peer-side debounce of the Sync button; budgeted background
  polls (5-15 s for the active project, 30 s+ for per-
  project iterations, stop when backgrounded); ``S.BUSY``
  is "back off" not "retry"; per-event triggers not
  per-status-change triggers. Closes a 2026-05-18 field
  report where a peer fired six parallel
  ``POST /v1/projects/<lang>/sync`` per gesture; the
  daemon's project_lock did its job by refusing them with
  ``S.BUSY``, but the peer surfaced each refusal as
  "Une autre synchronisation est en cours" toast — a wall
  of noise the user couldn't act on. ``S.BUSY`` added to
  the § 17 routing table (silent on both auto and user
  paths) and to the § 17 constants list. § 17 auto-sync
  code example updated to use ``commit_project`` +
  ``poll_job`` (it was still showing the pre-0.43
  ``sync_project`` shape, which is exactly the
  anti-pattern § 17c Rule 2 closes). Daemon-side
  protections summarised at the end of § 17c so future
  peer maintainers know what NOT to reinvent.

### azt_collabd 0.43.1 / azt_collab_client 0.43.1 — Polish + bootstrap parity guard

Patch follow-up to 0.43.0. One small behaviour change in the
bootstrap probe (the parity guard); everything else is pure
cleanup.

- **Bootstrap: no "newer available" popup at version parity.**
  Closes NOTES_TO_DAEMON.md filed by azt-recorder 1.45.0
  (2026-05-15). `_peer_update_with_confirm._probe` was firing
  the self-update popup with the *currently-installed* version
  in the message when (a) the user had just `adb install -r`'d
  the freshly-published release, (b) `peer_version == latest`,
  and (c) the last-seen-digest peer_pref was a leftover from
  an earlier version's session. Phantom "digest_changed=True
  at parity" signal. New `at_version_parity` flag suppresses
  `digest_changed` whenever `peer_version == latest`; the
  parity-with-stale-baseline case folds into the existing
  silent re-baseline branch (was `unknown_baseline`-only, now
  also covers `stale_baseline_at_parity`). Cost: a legitimate
  same-tag re-upload won't pop until either the maintainer
  bumps the tag or the next dev-loop install picks up the new
  bytes naturally. Benefit: dev-loop installs of just-
  published builds no longer surface a same-version-to-self
  prompt.
- **i18n: peer-side language re-sync hook +
  ``translate.tr`` fallback dropped.** Closes
  NOTES_TO_DAEMON.md filed by azt-recorder 1.45.0
  (2026-05-16) — "only `'More info'` renders translated" on
  the voluntary-update popup. Root cause was the peer's
  `add_fallback` target capturing the client `_current` at
  peer startup and never refreshing when bootstrap's
  `_sync_ui_language_with_daemon` swapped the client catalog
  out from under it. The `translate.tr` second-chance retry
  to `_client_tr` (the "if host returned msgid, try client"
  safety net) hid the issue for some strings but not
  others, depending on whether the host catalog returned the
  msgid unchanged. New `i18n.subscribe_language_change(cb)`
  API: peers register a callback that re-creates their own
  `gettext.translation` in the new language and re-calls
  `add_fallback(client_i18n.gettext_translation())` so the
  chain keeps pointing at the *current* client catalog.
  `set_language` invokes every subscriber after the swap +
  persist; failures are logged, not raised. `translate.tr`
  simplified to a straight delegation — no retry. Peer
  obligation documented in `CLIENT_INTEGRATION.md` § 6.

- **French catalog filled.** 32 previously-empty ``msgstr``
  entries translated, covering the GitHub-collaborator-invite
  flow, the bootstrap install / update / reboot prompts,
  popups (``Invite collaborator``, ``owner/repository``,
  ``Sending invitation…``, ``Invite failed: {error}``,
  ``Open AZT Collaboration``, ``More info``), update.py
  status strings (``Downloading…``, asset-filename failures),
  ``Sync was interrupted; please retry.``, and ``Please set
  your name in the sync settings before publishing or
  syncing.`` ``Project-Id-Version`` header bumped to match.
- **41 catalog orphans removed.** Stale ``msgid`` entries
  from features that were removed or rephrased: ``Active
  host``, ``Copy URL``, ``Disconnect GitHub`` / ``GitLab``,
  ``GitHub`` / ``GitLab`` standalone, ``GitLab credentials``,
  ``Refresh``, ``Save``, ``Save daemon log to file`` / ``Stop
  saving daemon log``, ``Set GitLab credentials``, ``Test
  connection``, ``Update needed`` / ``Update this app`` /
  ``Updating {name}…``, four bootstrap strings now phrased
  differently, two ``Opening {uri}…`` variants, ``Tap
  "Begin"…``, ``Connected as {username}.`` (kept the
  ``…Credentials saved.`` variant), ``Error setting host:
  {error}``, ``Saved for {username}.``, ``Copied to
  clipboard.`` / ``Could not copy: {error}``, ``Could not
  open settings: {error}``, ``e.g. Kent Rasmussen``, ``Install
  it to enable sync, then reopen this app.``, ``Paste the
  repository URL here``, and ``Share this app`` (orphan
  beside the live ``Share app``). Also deduplicated ``Share
  this app`` which appeared twice — both removed since
  neither was referenced.
- **``examples/sister_app.py`` rewritten** as a read-only
  daemon survey. Previously took a working-tree path and
  did ``register_project`` + ``commit_project`` — a
  bootstrap demo masquerading as a peer. Now takes no
  args and prints every field the client gets from the
  daemon: reachability + version compat, contributor +
  device_name + credentials, ``sync.work_offline``,
  registered project list, full ``project_status`` for
  ``last_project()`` including the new 0.43.0 fields.
  Interactive prompt with ``p`` to open the picker
  subprocess, ``s`` to open the daemon settings UI
  subprocess, ``r`` to refresh, ``q`` to quit. After
  ``p`` returns the survey re-prints so changes are
  immediately visible. References in ``README.md`` /
  ``CLAUDE.md`` updated to match the no-arg form.
- **Test fix.** ``test_scheduler_run_sync_refuses_when_
  contributor_unset`` renamed to ``test_scheduler_run_
  commit_refuses…`` and now calls ``_run_commit`` (the
  0.43.0 rename of ``_run_sync``).

### azt_collabd 0.43.0 / azt_collab_client 0.43.0 — Split commit and push; daemon-driven push policy; ``sync.work_offline`` toggle

Closes the NOTES_TO_DAEMON.md item filed by azt-recorder
1.43.1 (2026-05-15): debounced ``request_sync`` skipped the
commit step entirely while offline, so a field-session of
swipes piled up dirty files with ``commits_ahead=0,
n_changes=N`` rather than the per-swipe commits a user
expects. Synchronous ``sync_project`` (Sync button) committed
fine under the same offline conditions, proving the commit
step itself wasn't network-gated — only the debounced
pipeline was misordered.

Rather than patch the early-return inside ``_run_sync``, the
whole commit/push relationship is rethought: peers decide
where to cut a commit, the daemon decides when (and whether)
to push.

- **``commit_project(langcode)`` replaces ``request_sync``**
  (client + daemon). Same debounce / async / job_id /
  poll_job machinery — narrower contract. The RPC is now
  commit-only: it stages, commits, marks ``pending_push``,
  and returns. No fetch, no merge, no push. The old
  ``request_sync`` name kept as a backwards-compat alias in
  the client; the old ``/v1/projects/<lang>/sync_async``
  URL routes to the new ``commit`` handler on the server.
  Old peer code keeps working — the only behavioural change
  is the result no longer carries ``PUSHED``. Migrate
  result-handling that polls for ``PUSHED`` over to the
  scheduler-driven model.
- **Push moves entirely to the scheduler's drain loop**
  (``azt_collabd/scheduler.py``). The connectivity watcher
  tracks ``_online_since`` on offline→online edges and only
  fires the drain once ``now - _online_since >=
  settings.post_online_grace_s`` (default 60 s). Brief
  tethers the user enabled for something else don't burn
  their MB on pending pushes. The drain also respects the
  ``sync.work_offline`` master toggle.
- **``sync.work_offline`` toggle** —
  ``GET/POST /v1/config/work_offline``, persisted to
  ``$AZT_HOME/config.json``. When on, the watcher drain is
  a no-op and the user-gestured Sync button (``sync_project``)
  returns ``S.WORK_OFFLINE_ENABLED`` without attempting
  any push. Commits via ``commit_project`` are unaffected;
  only push is suppressed. Toggling OFF fires an immediate
  drain so the user doesn't wait a full
  ``connectivity_poll_s`` tick.
- **``S.WORK_OFFLINE_ENABLED`` status code** (mirrored
  daemon + client). Peers route the same way they handle
  ``AUTH_REQUIRED``: toast + ``open_server_ui()`` to the
  daemon settings screen anchored on the toggle.
- **Daemon settings UI**: new "Work offline" section with
  yes/no buttons, in ``azt_collabd/ui/app.py`` (above
  Diagnostic log). State refreshes on screen entry; toggling
  OFF fires the immediate drain server-side.
- **``ProjectStatus.work_offline``** carries the
  daemon-wide bool on every ``project_status`` response so
  peers can render a badge alongside ``commits_ahead`` —
  "5 commits waiting · offline mode" — without a second
  RPC.
- **``repo.py`` factored** into ``commit_repo``
  (stage + commit, no network) and ``push_repo`` (fetch +
  merge + push, no commit). ``sync_repo`` kept as the
  combined entry point for the user-gestured Sync button and
  legacy ``commit_audio_and_sync``; internally it now calls
  ``_commit_step_locked`` then ``_push_step_locked`` under
  one project lock.
- **``scheduler._drain_stuck_commits`` is now commit-only**
  (calls ``commit_repo`` instead of ``sync_repo``). Push for
  recovered commits happens via the regular drain pass.
- **``scheduler.is_online_cached()``** exposes the watcher's
  most recent observation as a module-level bool read —
  callers that don't need a fresh 3–6 s TCP probe should
  use this instead of ``net._has_internet``. Internal
  caller-only for now; not on the wire.
- **MIN_CLIENT_VERSION / MIN_SERVER_VERSION** lock-stepped
  at 0.43.0. Hard requirement: a 0.43 peer against a pre-
  0.43 daemon would still lose offline commits (the bug
  this release fixes); a pre-0.43 peer against a 0.43
  daemon would never observe ``PUSHED`` codes from
  ``commit_project`` and could mis-render its sync state.

### azt_collabd 0.42.0 / azt_collab_client 0.42.0 — Package-replacement: receiver reaps in-APK; drop peer KILL_BACKGROUND_PROCESSES; installed-vs-running reboot prompt

Follow-up to 0.41.28's suite-wide ``SuiteSelfReplaceReceiver``.
The earlier design paired a manifest receiver in every APK with a
peer-side ``killBackgroundProcesses(<server_pkg>)`` backstop on the
Check-again paths to handle OEMs that don't auto-kill the old
process during a package replace. The backstop required peers to
declare ``KILL_BACKGROUND_PROCESSES`` in their own
``android.permissions``, which is one more permission to explain
if/when the suite ever goes through a store review.

The new design moves the reap into the receiver itself: the
freshly-installed APK's receiver calls
``killBackgroundProcesses(getPackageName())`` (its own package's
old-code processes) before the self-kill. No cross-package kill;
no peer permission needed. A small peer-side reboot prompt
covers the migration window for users whose currently-installed
daemon is pre-0.42 (no in-receiver reap).

Minor bump because the receiver behaviour change is observable
across the suite and the peer permission surface area shrinks —
worth lock-stepping daemon + client at 0.42.0 to make the
"upgrade past this line" point unambiguous.

- **``SuiteSelfReplaceReceiver``** now calls
  ``ActivityManager.killBackgroundProcesses(getPackageName())``
  before ``Process.killProcess(myPid())``. ``SecurityException``
  is caught and logged — if the permission injection were to fail
  for any reason, the receiver still self-kills (i.e. degrades to
  the 0.41.28 behaviour on that APK).
- **``p4a_hook.py:_inject_self_replace_receiver``**: the
  ``<receiver>`` block stays on every suite APK; the
  ``<uses-permission android:name=
  "android.permission.KILL_BACKGROUND_PROCESSES" />`` element
  is gated on ``dist_name == 'aztcollab'`` so only the server
  APK ends up with the permission in its merged manifest. Peers
  compile the same Java receiver class but their manifest
  declares only the ``<receiver>``; at runtime the receiver's
  reap call hits ``SecurityException``, is caught, and it falls
  through to its self-kill — same net behaviour as the 0.41.28
  peer receiver, with no peer permission to explain. Anchor
  fixed to ``<application `` (with trailing space) so the
  injection doesn't land inside the explanatory comment in
  ``server_apk/manifest_extras.xml`` (whose prose mentions the
  literal ``<application>``). Idempotent via the
  ``self-replace-permission-injection`` sentinel.
  Reported by azt_recorder 1.42.29 via NOTES_TO_DAEMON.md.
- **Peer-side backstop removed.**
  ``azt_collab_client.ui.bootstrap._kill_server_background`` is
  deleted. The Check-again paths simply invalidate the release
  cache and re-enter ``_check_server`` — the next bind picks up
  the new code from the freshly-installed APK whose receiver did
  the reap during install.
- **Installed-on-disk vs. running detection.**
  ``azt_collab_client.ui.bootstrap._installed_server_version``
  reads the server APK's ``versionName`` via
  ``PackageManager.getPackageInfo``. ``_prompt_server_update``
  compares that to the version /v1/health reports; if installed
  > running, the user has sideloaded the new APK but Android
  kept the old daemon process alive (the pre-0.42 case where
  the receiver doesn't auto-reap). Instead of asking the user
  to re-download, ``_prompt_server_reboot_to_apply`` surfaces a
  "You have {installed} installed; running process is {running}.
  Restart your device to switch to the newer version." popup
  (Check again + Quit + maintainer mailto). Pure transition
  helper — once every field daemon is at 0.42 or newer the
  receiver's in-APK reap fires and the comparison should never
  trigger.
- **§ 2 (peer permissions)** updated: ``KILL_BACKGROUND_PROCESSES``
  no longer listed. The new note explicitly tells peer maintainers
  not to add it themselves.
- **§ 19 (package-replacement contract)** rewritten: the
  two-step receiver contract (reap then self-kill), the
  permission injection model, and the "peers MUST NOT add this
  permission" rule replace the prior peer-backstop section. The
  rollout-window note covers what happens for users still on
  pre-0.42 server APKs — peer surfaces the reboot prompt
  automatically.

No wire-format change. ``MIN_SERVER_VERSION`` / ``MIN_CLIENT_VERSION``
unchanged — the new behaviour is internal to the Java receiver,
the build-time manifest injection, and the peer-side bootstrap
flow.

### azt_collabd 0.41.29 / azt_collab_client 0.41.27 — Atomic-write orphan auto-recovery

Background. The ``atomic_open_write`` protocol is two-phase: peer
streams full LIFT bytes into ``<working_dir>/.azt_atomic_pending/
<token>``, then a separate ``atomic_finalize`` RPC renames the
scratch over the real LIFT. A crash, daemon kill, or transport
break between the two phases leaves the scratch on disk —
complete, well-formed LIFT, but never landed. The two orphans
field-reported in this session were exactly that: complete LIFT
files, sitting in ``.azt_atomic_pending/``, never finalized.

- **New module** ``azt_collabd/atomic_recovery.py``. Scans each
  registered project's ``.azt_atomic_pending/`` directory for
  orphan files ≥ 60 s old (skip in-flight Phase-1 writes) and
  classifies each:

  - Hash-equal to current LIFT → delete (confirmable garbage).
  - All shared guids byte-identical in canonical XML AND no
    orphan-only entries → delete (subset; no new info).
  - Otherwise → run ``lift_merge.three_way_merge(base=b'',
    ours=current, theirs=orphan)``. Write merged bytes
    atomically to the LIFT path, commit as ``"Recovered orphan
    from <iso-timestamp>"`` (author + committer = suite bot,
    same identity used for cross-peer merges). Conflicts get
    the existing ``<annotation name="azt-lift-conflict">``
    treatment — peers / viewers that already surface those
    annotations see recovery conflicts without any new code.
  - Merge raises (corrupt XML, broken byte stream from an
    interrupted Phase-1 write that *looked* > 60 s old) or
    any of lift_merge's guard kinds fire (parse-error,
    truncation-suspected, catastrophic-output) → move the
    orphan to ``.azt_atomic_orphans/unmergeable/<token>.lift``
    for manual inspection.

- **Scheduler integration.** ``scheduler._drain_atomic_orphans``
  runs every watcher tick (default 30 s) alongside the existing
  stuck-commit drain. Cheap when nothing is pending (single
  ``os.listdir`` on a typically-empty directory). Each
  non-trivial outcome logs to the daemon log.

- **ProjectStatus diagnostic.** New field
  ``n_recovered_today: int`` on the project_status response and
  the client-side ``ProjectStatus`` dataclass — purely
  informational, zero on healthy projects, positive when
  Phase-1-only writes were merged back in. Resets at the day
  boundary via ``last_recovery_day`` in projects.json.

- **No user-facing prompt.** In a no-delete-of-LIFT-entries world
  the merge is unambiguously lossless (orphan only ever has
  guids that current also has, plus potentially new field
  content); a "Merge or Discard?" prompt would ask users a
  question most aren't competent to answer, and the safe answer
  ("merge") is the only reasonable default anyway. Conflicts
  flow through the existing annotation channel.

- Versions: daemon 0.41.29 / client 0.41.27. Additive on the
  wire (new ProjectStatus field; pre-0.41.27 clients ignore
  unknown keys; pre-0.41.29 daemons emit nothing for it). No
  MIN floor bumps needed.

### azt_collabd 0.41.28 / azt_collab_client 0.41.26 — Suite-wide package-replacement handling: APK install now reaches the running process

Symptom this closes: a user sideloads the required server APK in
response to the peer's ``client_too_old`` prompt, relaunches the
peer, and still hits the same "AZT collab x.y.z or newer is
required" popup. The new APK is on disk; the OLD process is
still serving the provider with the OLD version. "Wait for an
update" is the wrong instruction — the update is right there.

- **Suite-wide ``MY_PACKAGE_REPLACED`` receiver.** New Java class
  ``org.atoznback.aztcollab.SuiteSelfReplaceReceiver`` at
  ``android/src/main/java/...`` handles the broadcast by
  self-killing the receiving process. ``p4a_hook.py`` grows
  ``_inject_self_replace_receiver`` to inject the manifest
  ``<receiver>`` into every APK in the suite (NOT gated on
  ``dist_name`` — server + every peer get it). Manifest receiver,
  NOT runtime: some Android versions / OEMs kill the old process
  as part of the replace, so a runtime-registered receiver
  wouldn't be alive to receive the broadcast; manifest receivers
  cold-start the new APK's code to deliver, which is exactly
  what we want.
- **Peer-side backstop.**
  ``azt_collab_client.ui.bootstrap._kill_server_background``
  dispatches
  ``ActivityManager.killBackgroundProcesses(<server package>)``
  from the Check-again paths in ``_do_check_again``. Belt-and-
  braces for the rollout window before every field server APK
  ships the receiver, and for the rare case where the
  ``MY_PACKAGE_REPLACED`` broadcast didn't fire. Harmless when
  the server is healthy (the next call lazy-spawns from the
  current APK either way), curative when it's stale.
- **Peer permission.** ``KILL_BACKGROUND_PROCESSES`` added to
  the required peer permissions in ``CLIENT_INTEGRATION.md``
  § 2. Normal-protection, no runtime grant prompt. Without it
  the helper raises and falls through to the legacy behaviour
  (user's next launch eventually picks up the new code once
  the OS recycles the old process for its own reasons).
- **Contract codification.** New § 19 "Package-replacement
  handling" in ``CLIENT_INTEGRATION.md`` formalises the rule:
  every suite APK MUST self-handle ``MY_PACKAGE_REPLACED``;
  peers MAY backstop with ``killBackgroundProcesses``; peers
  MUST NOT assume on-disk APK matches the running server
  process without verifying.

### azt_collabd 0.41.27 / azt_collab_client 0.41.25 — COMMIT_REPEATEDLY_FAILED + scheduler-driven retry: catch the "164 files in one commit" pattern even when the user is idle

User report: production commits arriving with ~164 files apiece, hours
or days of recording sessions, after long silent stretches where
nothing pushed at all. The pattern is "failure to commit for some
time, followed by a successful catchup commit." Until now the daemon
shipped a one-shot ``S.COMMIT_FAILED`` per failed attempt with no
across-attempts memory, so a streak of failures looked indistinguish-
able from one unlucky retry — the user kept recording, files piled up
on the device's daemon-private filesDir, and the eventual catchup
commit hid the magnitude of the gap.

- New status code ``S.COMMIT_REPEATEDLY_FAILED``: surfaced when the
  same project has hit ``S.COMMIT_FAILED`` two-or-more times in a
  row. Counter persisted at ``projects.json :: <langcode>
  .commit_failure_count``, bumped on every COMMIT_FAILED branch,
  cleared on every successful commit. Threshold = 2 because
  dulwich's ``porcelain.commit`` essentially only raises on
  persistent conditions (index corruption, refs problem, disk
  full, broken repo state); one failure can be a fluke, two means
  the underlying problem isn't self-healing. ``count`` and the
  last dulwich ``error`` ride the status params.
- **Scheduler-driven retry.** The connectivity-watcher loop now
  also drains stuck commits every tick (default 30 s) with
  exponential backoff (30, 60, 120, … s, capped at 1 hour). An
  idle device with a failed commit gets a second look without
  the user having to gesture the peer; recovery from a
  transient cause (lock released, disk freed, daemon restart)
  clears the counter automatically. Implementation:
  ``scheduler._drain_stuck_commits`` in
  ``azt_collabd/scheduler.py``.
- **``ProjectStatus`` exposes the streak (diagnostic).** The
  ``project_status`` RPC response gains
  ``commit_failure_count`` + ``last_commit_failure_at`` +
  ``last_commit_error`` for diagnostic surfaces (settings
  screens showing "last commit error: …"). The alarm itself
  still flows through ``result.statuses`` only — the counter
  persists between gestures, so the next peer-driven sync
  after a background failure naturally sees the elevated
  counter and carries ``COMMIT_REPEATEDLY_FAILED`` on its
  result. Peers do not need to synthesize the alarm from the
  polled count; § 17a in ``CLIENT_INTEGRATION.md``
  documents this explicitly.
- Routing: ``CLIENT_INTEGRATION.md`` § 17 lands the code in the
  same never-silenced bucket as ``DATA_LOSS_RISK`` — auto-sync
  still must surface it (silencing would hide active data loss
  in exactly the catchup-commit pattern the bug was filed
  against). The auto-sync code shape now iterates and surfaces
  both codes before the silencing branches consume the result.
- Translation: client catalog + French ``.po`` carry a
  data-loss-class user-visible message that names "Settings →
  Diagnostic log → Log server activity = yes, then Share daemon
  log so we can investigate" — same shape as the
  ``DATA_LOSS_RISK`` message, since the investigation surface
  is identical (the daemon log will show *why* the commits
  failed).
- ``MIN_CLIENT_VERSION`` ↑ 0.41.25, ``MIN_SERVER_VERSION``
  ↑ 0.41.27 — new status code + new ProjectStatus fields;
  pre-this-version clients have no translation and no
  poll-surface, falling back to the auto-sync result iteration
  alone.

### azt_collabd 0.41.21 / azt_collab_client 0.41.21 — Scan QR: fix IntentIntegrator autoclass path + bundle AndroidX transitively; multi-density server-APK presplash

Plus, adopting the multi-density splash pattern from
``NOTES_TO_DAEMON.md`` "be eager when you have room to" §9 for the
server APK itself:

- ``generate_presplash.py`` rewritten to emit one PNG per Android
  density bucket (ldpi 0.75x → xxxhdpi 4x, mdpi 320×533 baseline)
  under ``server_apk/presplash_variants/drawable-<bucket>/presplash.png``.
  Fonts and icon are scaled per bucket so each variant is sharp
  at its native size. The legacy hdpi-sized
  ``server_apk/presplash.png`` is also rewritten as the
  ``presplash.filename`` rare-fallback.
- ``server_apk/buildozer.spec.tmpl`` grows an
  ``android.add_resources`` listing pointing at the six bucket
  variants, so Android's resource resolver picks the right one at
  install / launch time. No runtime PIL-resize on first boot.

Run ``python generate_presplash.py`` once before each release
build to refresh the version stamp; the produced
``presplash_variants/`` is build output (gitignore candidate) but
the spec entry is permanent.

Plus the rest of the "be eager when you have room to" asks
filed under the same note:

- **``CLIENT_INTEGRATION.md`` § 18 "Low-power adaptive policy"**
  documents the three rules (OS signals not user toggle;
  automatic for resource decisions, user-facing for content /
  workflow; pre-built variants beat runtime regeneration),
  the gate-vs-don't-gate inventory, the multi-density
  ``android.add_resources`` recipe, the verification block,
  and the diagnostic-logging shape. ``CLAUDE.md`` carries the
  rationale (why automatic, why build-time-work-in-the-build).
- **``azt_collab_client.lowpower``** ships as a new module —
  the JNI plumbing peers were duplicating, plus a single source
  of truth for the thresholds (3 GB / 6 GB tier cuts, 0.15
  availMem ratio, 720 px lowMemory downsample). API:
  ``total_ram_mb()``, ``memory_state()``, ``is_low_memory()``,
  ``is_metered_network()``, ``have_room_for_prefetch()``,
  ``ram_tier()``, ``densityDpi()``, ``dpi_to_bucket()``,
  ``identify_drawable_variant()``, ``log_presplash_variant()``.
  Thresholds are module-level constants, override before first
  call. ``AZT_FORCE_LOW_MEMORY=1`` env flips every signal to its
  budget-device value for local testing.
- **Diagnostic recipe corrected.** The first-pass recipes
  (``Drawable.getIntrinsicWidth/Height()``,
  ``BitmapDrawable.getBitmap().getDensity()``) both reported
  device-scaled state and silently collapsed every bucket on
  any given device. ``identify_drawable_variant`` uses
  ``BitmapFactory.decodeResource`` with ``inJustDecodeBounds=
  true`` + ``inScaled=false`` instead — ``opts.outWidth`` /
  ``opts.inDensity`` then carry the native pixel width / source
  folder density of the file Android actually picked, so the
  bucket name can be identified unambiguously.
- **Server APK logs its own variant.** ``server_apk/main.py``
  calls ``log_presplash_variant(tag='presplash:server')`` at
  startup; sister apps log under their own distinct tag (e.g.
  ``'presplash'``) so combined logcat is grep-able.

**Daemon-driven CAWL prefetch: offline-gate + circuit breaker.**
0.41.4 added daemon-side offline backoff; 0.41.8 dropped it
because "the peer has a circuit breaker"; 0.41.11 moved iteration
into the daemon's ``_prefetch_worker`` and the peer's circuit
breaker silently stopped applying (it lived in the old per-image
peer iteration model). Net result on an offline boot: the daemon
hammered DNS for every entry in the requested paths list,
producing logcat spam shaped like ``[cawl] image fetch failed for
… URLError: <urlopen error [Errno 7] No address associated with
hostname>`` repeated N times in ~40 ms intervals.

``_prefetch_worker`` now:

1. Checks ``net._has_internet()`` once at start. If offline,
   marks state ``skipped_offline=True`` / ``finished=True`` and
   returns immediately — no iteration, no spam.
2. Tracks consecutive failures inside the loop. After
   ``_PREFETCH_CONSECUTIVE_FAIL_LIMIT`` (3) back-to-back
   ``get_image_path`` failures, marks ``circuit_open=True`` /
   ``finished=True`` and bails. Real fetches succeed in
   <500 ms; three offline-class failures bunched together mean
   the device dropped connectivity, not three individually
   missing files.

``_make_prefetch_state`` grows two fields (``skipped_offline``,
``circuit_open``) and a ``started_at`` timestamp.

**``cache_status`` surface widened.** ``cache_status(repo)`` now
returns a dict instead of a ``(cached, total)`` tuple:

```
{'cached': int, 'total': int,
 'offline': bool, 'circuit_open': bool,
 'finished': bool}
```

The ``GET /v1/projects/<lang>/cawl/cache_status`` HTTP response
gains the same three flags. When the worker was offline-skipped,
``cached`` falls back to the actually-on-disk count via
``_walk_image_count`` — so a device with prior cache shows e.g.
"1247 / 3000 (offline)" instead of "0 / 3000" each offline boot.

**Daemon settings UI banner** rendered three ways now:

- normal: ``Caching images: M / N (network in use — please stay online)``
- offline-skipped: ``Image cache: M / N (offline — will resume when online)``
- circuit-broken: ``Image cache: M / N (paused — connectivity lost)``

Old peers reading only ``cached`` / ``total`` from the response
keep working — the new flags are additive.

**Stage A: daemon-driven auto-prefetch.** The daemon now owns
the "warm the CAWL image cache" decision instead of waiting
for a peer-driven ``cawl/prefetch`` POST. ``_touch_project``
(which fires on every langcode-bound endpoint) now also calls
``cawl.auto_prefetch(repo)``. ``auto_prefetch``:

- Resolves the full index image path set via the cached
  index (no network).
- Throttles to at most one trigger per repo per 30 s, so the
  1 Hz cache-status poll doesn't re-probe ``_has_internet``
  every second.
- Defers to ``start_prefetch``'s existing idempotency. A
  running prefetch with matching paths is a no-op; a finished
  prefetch (success OR offline-skipped) restarts, which is the
  natural retry path when connectivity may have returned.

Peers may continue to POST ``cawl/prefetch`` with their own
working-set list — useful when the peer wants to warm a
subset different from the full index. The endpoint is
backward-compatible. Stage B (peer-side removal of the POST)
ships in a later peer release; today's change is additive.

**Offline → online auto-resume.** The scheduler's
connectivity watcher already fires on the offline → online
edge (every ``connectivity_poll_s``, default 30 s). On that
edge it now also calls ``cawl.on_online_edge()`` which clears
the auto_prefetch throttle for any repo whose last state was
``skipped_offline`` or ``circuit_open`` and re-fires
``auto_prefetch``. Cache warming resumes within ~30 s of
network return with no user action required.

The cache-status banner poll **stays at 1 Hz** even on
offline / circuit_open state — the response is just
in-memory dict lookups, and the ``[first-try]`` probe for the
cache_status path is already suppressed (see below). Keeping
the poll running is what makes the banner auto-update from
"offline — will resume" to live progress when
``on_online_edge`` does its work.

**CAWL prefetch policy: one variant per id (default) vs. all
variants.** New config knob
``$AZT_HOME/config.json :: cawl.prefetch_all_variants``,
default ``False`` — daemon's auto_prefetch warms one image
per CAWL id (the file whose basename contains the canonical
``__`` preferred-variant marker, falling back to the first
file in the id directory if no variant carries the marker).
Set to ``True`` to warm every image-shaped index entry —
heavier on network and disk but useful for users who want
the full set offline.

API surface:

- Daemon: ``store.get_cawl_prefetch_all_variants`` /
  ``store.set_cawl_prefetch_all_variants(bool)``.
- HTTP: ``GET / POST /v1/config/cawl_prefetch_all_variants``,
  body ``{enabled: bool}``.
- Client: ``azt_collab_client.get_cawl_prefetch_all_variants``
  / ``set_cawl_prefetch_all_variants``.
- Filter logic: ``cawl._filter_preferred_variant_per_id``
  applied inside ``_index_image_paths`` whenever
  ``prefetch_all_variants`` is False.

Flipping the policy doesn't retroactively re-warm an
in-flight worker; the next ``auto_prefetch`` trigger
(project-load, scheduler edge) picks up the new path set.
Existing on-disk cache entries are kept either way.

**Daemon SettingsScreen highlights missing contributor on
entry.** Peers that route a ``S.CONTRIBUTOR_UNSET`` sync
failure through ``open_server_ui()`` previously dropped the
user onto the settings page with no indication of *which*
field was the blocker — the peer-side translated toast
("Please set your name…") could flash for under a second
and be eaten by the screen transition. On screen entry, if
``contributor_input`` is empty and not already focused, the
input now takes focus (keyboard pops up on Android) and the
inline hint reads "Required: your name is used for commit
authorship; sync and publish refuse until this is set." in
the red status colour. Saving a non-empty value clears the
hint back to the normal "Saved." confirmation.

**``[data-loss-risk]`` detection + new ``S.DATA_LOSS_RISK``
status.** ``_stage_audio`` and ``_sync_repo_locked`` now walk
``project_dir`` for any file outside the staging filter
(``audio/`` / ``images/`` / ``*.lift`` / ``.git/`` /
``.azt_atomic_pending/`` / ``.azt-collab/`` / known top-level
files like ``.gitignore``). Anything else is a peer writing
to a path the daemon won't commit — silent data loss class.
Each finding emits ``[data-loss-risk] uncommittable file in
project_dir: <rel>`` to stderr (so a tester-shared daemon log
makes the issue obvious), and the sync ``Result`` carries
``S.DATA_LOSS_RISK`` with ``count`` and ``sample`` (up to 5
paths) params.

**Peer contract** (``CLIENT_INTEGRATION.md`` § 17): this status
is **never silenced**. Auto-sync and user-initiated sync both
surface the translated toast unconditionally, urging the user
to enable "Log server activity" and share the daemon log.
Status is bucketed separately from the config-class /
transport-class statuses that auto-sync silences, because this
one represents active data loss, not a configuration glitch.

**``[stage-audio]`` / ``[commit-audio]`` diagnostic logs.**
Field report: testers record 1000+ audio files but only ~146
land in each commit (and only 4 commits total). Without
``adb`` access to the remote testers' phones we can't run
``ls audio/`` or ``git status`` directly. Daemon-side logging
in ``_stage_audio`` now emits a one-liner per pass with the
counts that disambiguate the gap:

```
[commit-audio] start project_dir='…/projects/baf' contributor=…
[stage-audio]  project_dir='…/projects/baf'
               on_disk_audio=1042 on_disk_images=12
               status.unstaged=0 status.untracked=898
               paths_to_add=898
[commit-audio] _stage_audio returned n=898
[commit-audio] committed n=898 sha=abc123def456
```

- ``on_disk_audio`` ≫ ``status.untracked`` → ``porcelain.status``
  is truncating large untracked sets; the gap is dulwich's,
  not the peer's.
- ``on_disk_audio`` ≈ ~146 → peer write path is dropping
  bytes; gap is upstream.
- ``status.untracked`` ≈ ~146 and ``on_disk_audio`` ≈ ~146
  ≈ ``paths_to_add`` over multiple syncs → user's record
  count is overcounting attempts vs. successes.

Remote tester recipe: daemon settings UI → "Log server
activity: yes" → record + sync → "Email daemon log". ``<_PickerRoot>`` hardcodes ``back_to:
'picker'`` on the SettingsScreen instance — correct in
external mode (settings reached from picker via the gear,
back should pop to picker), but wrong in internal mode:
settings is the root the user reached from outside the
Activity (launcher tap or peer's ``open_server_ui()``), so
the KV Back button navigating to picker dumped the user on
a screen they never asked for. ``PickerApp.build`` now
clears ``back_to`` on the settings screen in internal mode,
which trips the KV's
``height: dp(48) if root.back_to else 0`` gating and hides
the button entirely. The OS back path (``_navigate_back``
internal branch) remains the only way to leave settings,
letting Android finish() the Activity and return the user
to wherever they came from.

**``Switch project`` button promoted out of the gated row.**
Previously sat alongside Grant collaborator + Share repo QR
inside ``project_actions_row``, which is hidden when the
current project has no remote. Switch is meaningful before
publish too (user may want to abandon an unpublished project
for another), so it now lives in its own always-visible RecBtn
directly under the gated row — same vertical position
relative to the rest of the screen, but unconditionally
tappable.

**``project_actions_row`` hides via detach instead of just
``height: 0``.** Same Kivy touch-intercept bug ``publish_row``
already worked around: a BoxLayout with ``height: 0, opacity:
0`` still has its children at their declared sizes in the
widget tree, so their ``on_press`` handlers receive taps at
coordinates that visually belong to buttons higher up.
Symptoms: tapping ``Connect to GitHub`` fired
``grant_collaborator()`` (the row's first button), tapping
``Publish`` (when present) fired ``switch_project()`` (the
row's third button), tapping ``Connect to GitLab`` looked
like a no-op (Share-repo-QR's ``_pick_publish_candidate``
returned ``None``). ``_refresh_project_actions_row`` now
detaches all three children when hiding and reattaches when
showing — mirror of ``_detach_publish_children`` /
``_reattach_publish_children`` already in place.

**Edge-to-edge: status bar no longer hides the picker's gear
icon.** Android 15+ enforces edge-to-edge by default — the
status bar overlays the app window unless we opt back into
the pre-API-35 reserved-inset behaviour. Top-of-screen
widgets (the picker's gear, every screen's TopBar) sat
under the status bar; bottom-anchored widgets would have
sat under the gesture bar the same way. ``PickerApp.on_start``
now calls ``WindowCompat.setDecorFitsSystemWindows(window,
True)`` on the Activity's UI thread (via p4a's
``android.runnable.run_on_ui_thread`` helper), restoring
inset reservation. Available because we already pull
``androidx.appcompat`` (which transitively brings
``androidx.core.view``).

**``PickerApp.font_name`` alias.** Settings UI code that opens
modals (``share_repo_qr``, ``grant_collaborator``) reads
``App.get_running_app().font_name`` directly — fine under the
old ``CollabUIApp`` which exposed ``font_name`` as a class
attribute, but ``PickerApp`` only had the private ``_font_name``.
Under the unified PickerApp on Android, tapping ``Share this
repo (QR)`` (and ``Grant collaborator access``) raised
``AttributeError: 'PickerApp' object has no attribute
'font_name'`` and Kivy's event-loop catch buried it — the user
saw "tap does nothing." New ``@property font_name``  on
PickerApp returns ``_font_name`` so both callsites resolve
identically across host App classes.

**UX cleanup after the picker+settings merge.**

- **Share-repo QR popup** — dropped the "Copy URL" button.
  Close is the only remaining action; the URL is visible
  above the QR for users who'd rather read it than scan it.
- **Install / update popup** — "Open install page" relabeled
  to ``More info`` and moved RIGHT of the Install button so
  the affirmative action lands where the eye expects.
- **Install / update popup status line** — split out of
  ``body_label`` into a dedicated ``status_label`` rendered
  in the ACCENT colour, bold, sp(15). "Tap install again to
  confirm" and other transient status messages now read as
  the current call-to-action instead of vanishing into the
  wall of explanatory text above.
- **Contributor input hint** — changed from a specific
  example name to ``first_name last_name`` (generic).
- **Contributor "Required" message** — ``contributor_msg``
  label now auto-grows on ``texture_size`` so the multi-line
  warning isn't truncated when the SettingsScreen surfaces
  it on entry.

**Ungraceful-shutdown detection via sentinel file.** New
``azt_collabd/crash_marker.py``: on startup, writes
``$AZT_HOME/process_running.json`` with this process's pid +
started_at, registers an ``atexit`` hook to delete it on
clean shutdown. On the NEXT startup, a leftover sentinel
means the previous process bypassed atexit (SIGSEGV, SIGKILL,
OOM-kill, ``os._exit``, kernel-level kill); a one-line
summary lands in ``$AZT_HOME/last_native_crash.json``.

``GET /v1/health`` now surfaces it alongside the existing
``last_crash``:

```
{"ok": true, ...,
 "last_native_crash": {
   "detected_at": 1747234567.123,
   "previous_pid": 12917,
   "previous_started_at": 1747234389.456,
   "signal": "",
   "thread_name": "",
   "approx_pc": "",
   "detection_source": "ungraceful-shutdown sentinel"}}
```

``last_crash`` and ``last_native_crash`` are complementary:
the former is written by the daemon's Python excepthook from
the dying process (caught exception, Python alive to write
it); the latter is detected on the *next* startup from
sentinel-file diff (signal handler bypassed Python entirely).
A peer's `[server-crash]` log helper can mirror both.

Closes NOTES_TO_DAEMON.md "Daemon-side surface for native
crashes" by the pragmatic route — no JNI sigaction handler,
no async-signal-safe C extension. ``signal`` / ``thread_name``
/ ``approx_pc`` ship as empty strings reserved for a future
sigaction-driven shape: when a real handler lands, it
populates them in the dying process before ``_exit()``, peers
see richer detail with no schema change.

**"Switch project" button on the daemon settings UI + unified
picker/settings Kivy app.** New ``Switch project`` button in
the "Current project" row, sibling to Grant collaborator and
Share-repo-QR. Tapping it navigates to the project picker
in-process — no Intent, no Activity transition — and the
picker's submit handler stamps the new langcode via
``set_last_project`` and navigates back to settings.

The unification: the server APK used to run two separate
Kivy Apps (``CollabUIApp`` for settings, ``PickerApp`` for the
picker), one chosen at startup from the launching Intent
action. With ``PythonActivity`` being ``singleTask`` (p4a
default), firing PICK_PROJECT on ourselves wouldn't spawn a
fresh Activity — Android would route through
``onNewIntent`` on the existing one. So the only path to an
in-process switch is one Kivy App that hosts both screen
sets. ``PickerApp`` (which already had ``SettingsScreen`` as
a sibling for the picker → gear → settings flow) is the
unified home; ``server_apk/main.py`` always invokes it now,
passing ``launch_mode='external'`` for PICK_PROJECT Intents
(existing peer-driven behaviour, picker is initial screen,
submit fires setResult/finish) or ``launch_mode='internal'``
otherwise (settings is initial screen, picker submit writes
``last_project`` + navigates back to settings).

``_navigate_back`` branches on ``_launch_mode``:
- external: existing behaviour (back from picker exits the
  Activity with setResult, etc.).
- internal: back from settings returns False so Android
  closes the Activity (matching pre-0.41.22 ``CollabUIApp``
  semantics); back from picker / langpicker navigates to
  settings instead of finishing the Activity.

``PickerApp.on_resume`` added: refreshes the active screen
when the Activity comes back to the foreground.

**Pairs with the peer-side ``CLIENT_INTEGRATION.md`` § 14a
contract.** The daemon-side button is a no-op for the peer's
loaded view until peers ship the ``App.on_resume`` ↔
``last_project()`` reconciliation hook documented there. Ship
the daemon button now; peers adopt the on_resume hook in
their next release; the UX is coherent end-to-end at that
point. Mismatched timing degrades gracefully — the user
lands back on the previous project (the old pre-button
behaviour), nothing destructive.

**Diagnostic log section follows the same binary-toggle
pattern.** The single ``Save daemon log to file`` /
``Stop saving daemon log`` button is replaced by a row reading
``Log server activity:`` followed by two side-by-side buttons
— ``yes`` and ``no`` — with the active state highlighted in
the GREEN accent. Status line underneath is preserved (it
shows the log file path / "log capture is off" / byte count
on screen entry). Share + Email buttons below stay disabled
while logging is off and re-enable once the user picks
``yes``. Same convention as the wordlist row, the language
selector, etc.

**Daemon settings UI exposes the toggle.** New section on the
SettingsScreen, between "Refresh Status" and "Diagnostic log".
Section label reads ``Wordlist ({name}) images`` where
``{name}`` is the active project's wordlist (derived from the
image-repo slug — ``kent-rasmussen/images_CAWL`` →
``CAWL`` — via the new ``cawl.wordlist_name`` helper). Row
underneath reads ``Cache images:`` followed by two side-by-
side buttons — ``1 per line`` and ``all`` — with the active
mode highlighted in the GREEN accent (matching the language-
selector row's convention). Label updates on each
``refresh()`` so switching projects between visits to the
SettingsScreen renames the section to match.

**``cache_status`` cached count capped at ``requested`` in
offline-skipped state.** The walk-count fallback I introduced
this release (so an offline boot with prior cache shows e.g.
"1247 / 3000 (offline)" instead of "0 / 3000") counts every
file in the on-disk cache directory, which accumulates across
working sets and past sessions. Peer-reported case had the
disk holding 2220 files while the current ``requested`` was
1661, producing a "cache warm: 2220/1661" banner that tripped
peer-side "fully warm, hide and stop polling" logic and
looked like a daemon accounting bug. ``cache_status`` now
returns ``min(walk_image_count, requested)`` in the
offline-skipped branch; the active and circuit_open branches
were already accurate.

**jnius pre-warm at server-APK startup (main thread).** A
tombstone caught during the intermittent ``:provider`` crash
showed ``art::JNI::CallObjectMethodA`` SEGV at NULL on
``Thread-4`` (an unnamed Python-spawned thread, NOT our
prefetch worker). Two daemon-side helpers — ``paths.azt_home``
and ``store._autodetect_device_name`` — do their first jnius
work lazily on whichever thread happens to need the value
first. Python-spawned threads attach to the JVM via
pyjnius's auto-attach with the bootclassloader; first-time
``CallObjectMethodA`` on app-context fields from those
threads is the leading suspect for the NULL deref (per the
0.33.x classloader-attach precedent).

``server_apk/main.py`` now calls ``azt_home()`` and
``get_device_name()`` once on the main thread, immediately
after ``install_callbacks``. Both then serve from cached
state (process memory / config.json) for every subsequent
caller on any thread — no JNI dispatch from background
workers needed.

**Named all unnamed daemon-side worker threads.** The
``Thread-4`` in the tombstone could have been any of several
unnamed ``threading.Thread`` / ``threading.Timer`` spawns in
the daemon. Naming them lets the next crash backtrace
identify the worker directly:

- ``sync-fire-<langcode>`` (Timer / immediate sync workers
  in ``scheduler.py``)
- ``gh-device-flow-<id>`` (GitHub device-flow OAuth polling)
- ``clone-<id>`` (clone-job worker)
- ``httpd-shutdown`` (graceful loopback HTTP shutdown)

The CAWL prefetch worker was already named.

**``start_prefetch`` no longer spawns a second worker while
one is already running.** Pre-fix: a different ``requested``
count between calls would replace the state dict and start a
new thread; the old worker kept iterating and writing to the
new dict via ``_prefetch_state.get(repo)``. With Stage A
shipping ``auto_prefetch`` from every ``_touch_project``
*and* pre-Stage-B peers still POSTing their own
``cawl_prefetch`` working set, two workers regularly arrived
on overlapping timelines — both doing urllib/SSL fetches +
jnius-cached class work simultaneously. Leading suspect for
a NULL-deref SIGSEGV in the daemon's ``:provider`` process
~2 s after the second prefetch POST.

New behaviour: if an unfinished worker exists for the repo,
``start_prefetch`` returns its state and does NOT start
another. Different repos still proceed independently
(``_prefetch_state`` is repo-keyed). The peer's working
subset of a daemon-warmed full index will see its targets
populated by the running worker — no semantic loss.

**Bootstrap self-update no longer proposes installing an older
release over a locally-installed newer build.** The probe used
``needs_update = version_newer OR digest_changed OR mandatory``,
where ``digest_changed`` would trip on any GitHub-side asset
change. When a developer adb-sideloads a version newer than the
latest published tag and then any GitHub release publishes a
new digest, the probe would propose downgrading. New
``local_newer`` gate suppresses ``digest_changed`` when the
installed version is strictly above the latest tag. ``mandatory``
overrides remain unchanged — server-told-too-old still prompts
regardless. Diagnostic ``[bootstrap] _probe`` log line now
includes the ``local_newer`` boolean.

**Server APK no longer ships maintainer scripts.** The
``source.include_exts = py`` glob was sweeping two
maintainer-only Python files into ``classes.dex`` /
``private.tar``:

- ``server_apk/test_install.py`` — desktop integration
  smoke-test for the kill-recovery flow; sibling to
  ``test_install.sh``.
- ``azt_collabd/data/cawl/generate_seed.py`` — script that
  regenerates the bundled CAWL index JSON from GitHub at
  release-cut time.

Neither has any runtime role; both are now in
``source.exclude_patterns`` so they stay out of the APK.

**``[first-try]`` probe suppressed for cache_status polls.**
The always-on first-try diagnostic probes added in 0.41.16
were valuable for the no-adb field tester but emitted two
lines per cache_status poll at 1 Hz — pure noise on a normal
session. Transport now suppresses the probe when
``path.endswith('/cawl/cache_status')``. "First-try"
semantically doesn't apply to the Nth call of a polling
loop; all other RPC calls remain fully instrumented.

**Docs reorg — NOTES_TO_DAEMON.md is a live queue only.** The
two "standing notice" items that had accumulated there are
promoted to canonical homes:

- "Daemon is the sole authoritative source" (daemon-owned
  state table + four daemon obligations) → ``CLAUDE.md`` hard
  rule #8 + new "Daemon-owned state" section. It's an
  architectural invariant the client architecture depends on;
  ``CLAUDE.md`` is the right shelf.
- "Project-bound surfaces now in daemon UI (Phase 3)" →
  ``CLIENT_INTEGRATION.md`` § 12b "Project-bound actions live
  in the daemon settings UI", with the Phase-1 / Phase-3
  sequencing constraint preserved. It's peer-facing direction;
  the contract is the right shelf.

NOTES preamble tightened to call out the antipattern
explicitly: standing rules belong in ``CLAUDE.md`` /
``CLIENT_INTEGRATION.md``, not in the queue file. Otherwise
the queue silently turns into a reference shelf and stops
being a queue.

---

Two coupled bugs surfaced when testing the picker's "Scan QR"
affordance against the 0.41.20 server APK. The first masked the
second; both had to be fixed to make the button work.

Two coupled bugs surfaced when testing the picker's "Scan QR"
affordance against the 0.41.20 server APK. The first masked the
second; both had to be fixed to make the button work.

**1. Autoclass path corrected** in
``azt_collab_client/ui/qr_scan.py``. Was
``com.journeyapps.barcodescanner.IntentIntegrator``; the class
actually lives at
``com.google.zxing.integration.android.IntentIntegrator`` — the
journeyapps AAR re-ships ZXing's original IntentIntegrator at its
historical package path even though the rest of the library is
under ``com.journeyapps.barcodescanner``. Module docstring updated
to call out the mismatch.

**2. AndroidX transitive deps listed explicitly** in
``server_apk/buildozer.spec.tmpl``. The zxing-android-embedded
4.3.0 POM declares its AndroidX deps (fragment, appcompat) as
``implementation`` rather than ``api``, so Gradle uses them to
compile the AAR's own classes but does NOT propagate them to the
consuming APK's classes.dex. Result: the journeyapps classes
reference ``androidx.fragment.app.Fragment`` /
``FragmentActivity`` / ``AppCompatActivity`` but the Android
verifier can't resolve those references at class-load time, and
``autoclass(...IntentIntegrator)`` raises
``NoClassDefFoundError: Landroidx/fragment/app/Fragment;`` even
with the autoclass path fix in (1).

New ``android.gradle_dependencies``:

```
com.journeyapps:zxing-android-embedded:4.3.0,
androidx.appcompat:appcompat:1.6.1,
androidx.fragment:fragment:1.6.2,
org.jetbrains.kotlin:kotlin-stdlib:1.8.20,
org.jetbrains.kotlin:kotlin-stdlib-jdk7:1.8.20,
org.jetbrains.kotlin:kotlin-stdlib-jdk8:1.8.20
```

Listing appcompat + fragment explicitly forces Gradle to pull them
into the project classpath, so the dex actually carries the
``Landroidx/...`` implementations the journeyapps code references.

The three kotlin-stdlib pins resolve a transitive-version conflict
that surfaced as ``:checkReleaseDuplicateClasses`` failing with
``Duplicate class kotlin.collections.jdk8.CollectionsJDK8Kt``:
``androidx.fragment:1.6.2 → lifecycle-runtime:2.6.2`` pulls
``kotlin-stdlib:1.8.20`` (the post-merge artifact that already
ships the JDK7/JDK8 helper classes), while the same lifecycle-
runtime transitively pulls ``kotlinx-coroutines-android:1.6.4 →
kotlin-stdlib-jdk{7,8}:1.6.21`` (the pre-merge split artifacts
that also ship them). Forcing the ``-jdk7`` / ``-jdk8`` resolution
up to 1.8.20 lands on the empty metadata-only redirect artifacts
Kotlin started shipping at 1.8 once the split was deprecated, so
the duplicate-class collision disappears with no functional
change to anything else in the build.

**Build note.** Re-run ``server_apk/build_buildozer_spec.sh`` to
regenerate ``buildozer.spec`` from the template after pulling, then
``buildozer android clean && buildozer android release`` — the dist
tree caches Gradle resolution, so a clean is required to pick up the
new dependency list.

**Floor:** no bumps. Server APK rebuild required to ship the fix
since qr_scan runs inside the picker subprocess hosted by the server
APK, and the AndroidX deps need to be in *that* APK's dex.

### azt_collabd 0.41.20 / azt_collab_client 0.41.20 — docs: routing table moves to contract; atomic_open_write note added

Docs-only release closing two long-standing gaps between
``CLAUDE.md`` (philosophy / rationale) and
``CLIENT_INTEGRATION.md`` (conformity contract). Per the
docs-separation rule, conformity material belongs in the
contract; rationale belongs in CLAUDE.md.

**Sync-result routing table moved to contract** as new
``CLIENT_INTEGRATION.md`` § 17 (Routing on sync results). Full
table of status codes × auto-sync vs. user-initiated sync
behaviour, the canonical code shape for both contexts, and an
explicit ``S.*`` constant reference noting which constants
shipped in 0.41.13 (``SERVER_UNAVAILABLE`` /
``SERVER_ERROR``). The CLAUDE.md "Peer contract: routing on
sync results" section now carries only the rationale (per-
code meanings, the pre-0.34.1 anti-pattern, why the auto/user
distinction lives peer-side) and points to the contract for
the actual table + code.

**``atomic_open_write`` FD-path documented in § 8** of the
contract. Pre-0.41.7 the URI form of ``atomic_open_write``
shipped LIFT bytes as base64 inside the JSON-RPC body and hit
Binder's ~1 MB per-transaction cap on Android — silent
failure for LIFT > ~700 KB. Peers rebuilding against 0.41.7+
pick up the two-phase FD-write + finalize protocol
transparently; the contract now notes the rebuild-for-large-
LIFTs implication so peer maintainers know it's a free
correctness win.

No code changes in this release; docs only.

### azt_collabd 0.41.19 / azt_collab_client 0.41.19 — `share_log_file` + French translations + docs

Follow-on to 0.41.18 in response to recorder 1.41.24's filing
(NOTES_TO_DAEMON.md 2026-05-13). Two changes:

**``share_log_file(log_path, prev_path=None, ...)`` helper**
added to ``azt_collab_client/ui/share.py``. Reads a log file
(plus optional previous-session log) from disk, bundles into
one ``text/plain`` blob with section breaks, inserts into
MediaStore Downloads to get a real ``content://`` URI, and
dispatches an ``Intent.ACTION_SEND`` with ``EXTRA_STREAM``.

Unlike ``share_text``, this attaches as a real file (receivers
can save it; payload size isn't bounded by Intent extras), and
unlike ``share_running_apk`` it handles two source files +
sets a sensible default ``display_name``. Mirrors the
MediaStore-insert pattern from ``share_running_apk`` so the
underlying jnius dance is shared at the call-site level.

Recorder will replace its peer-side stand-in with one
``share_log_file(log_path=_LOG_PATH, prev_path=…)`` call once
it picks up 0.41.19.

**Daemon UI's "Share daemon log" migrates to
``share_log_file``** (reading ``$AZT_HOME/daemon.log`` from
disk directly — both daemon-UI and daemon-proper processes
share filesDir on Android, so file-based access works without
an additional RPC for the bytes). Email button still uses
``email_text`` since ``ACTION_SENDTO`` with a ``mailto:`` URI
restricts the picker to email apps.

**French translations** added for all 0.41.17-0.41.19 strings:
"Diagnostic log", "Save daemon log to file", "Stop saving
daemon log", "Share daemon log", "Email daemon log", and
related status / error messages. Plus the helper-side
``Share log`` / ``AZT log`` / ``Could not share log`` /
``Log file is empty`` / ``Log file: {path}`` strings.

**Docs.** ``CLIENT_INTEGRATION.md`` § 14b now lists all three
share helpers (``share_text``, ``email_text``,
``share_log_file``) with picking-between guidance.
``azt_collab_client/CLAUDE.md`` carries the rationale for the
share-module extraction and the daemon-log toggle's
hot-toggle design.

### azt_collabd 0.41.18 / azt_collab_client 0.41.18 — share helpers extracted + email-log button

Follow-on to 0.41.17. Two changes:

**``share_text`` and ``email_text`` extracted into
``azt_collab_client/ui/share.py``** alongside the existing
``share_running_apk``. Both reusable by any peer:

- ``share_text(text, subject='', chooser_title='', on_error=None)``
  — ``Intent.ACTION_SEND`` with ``EXTRA_TEXT``. Any
  ``text/plain``-handling share target accepts it.
- ``email_text(text, to='', subject='', on_error=None)`` —
  ``Intent.ACTION_SENDTO`` with a ``mailto:`` URI. Restricts the
  picker to email apps only.

The daemon UI's "Share daemon log" button now delegates to
``share_text`` instead of inlining the JNI dance.

**"Email daemon log" button** added to the Diagnostic log
section alongside "Share daemon log". Uses ``email_text`` for
the email-only picker affordance — better UX than the generic
share sheet when the user's intent is specifically "send this
to the developer".

### azt_collabd 0.41.17 / azt_collab_client 0.41.17 — daemon-log-to-file toggle + share button

Remote tester can't run logcat, so daemon-side diagnostic
output (``[boot-trace-daemon]``, ``[cawl]``, ``[recent]``,
``[first-try]`` from the daemon UI / picker subprocess) was
unreachable. Added:

- **Config knob** ``logging.daemon_log_to_file`` in
  ``$AZT_HOME/config.json``. Default off.
- **Stderr tee** in the daemon. When the toggle is on,
  ``sys.stderr`` is wrapped to mirror writes to the original
  destination (logcat) AND to ``$AZT_HOME/daemon.log``. Tee
  is hot-installable / hot-removable — no daemon restart
  needed.
- **Endpoints.** ``POST /v1/logging/daemon_log_to_file`` (set
  toggle, install/remove tee in-process); ``GET
  /v1/logging/daemon_log`` (returns log contents + current
  toggle state + file path).
- **Settings UI.** New "Diagnostic log" section with two
  buttons: "Save daemon log to file" (toggle) and "Share
  daemon log" (Android ``Intent.ACTION_SEND`` with the log
  content as ``EXTRA_TEXT``). Status line under shows current
  state + file size.
- **Client wrappers.** ``set_daemon_log_to_file(enabled)`` /
  ``get_daemon_log()``.

The share intent uses ``EXTRA_TEXT`` (text/plain) rather than a
file-URI attachment so any text-handling share target accepts
it — email composers, messaging apps, file savers. Daemon
truncates to the last 256 KB to fit comfortably in an intent
extra; the diagnostic value lives in the tail anyway.

### azt_collabd 0.41.16 / azt_collab_client 0.41.16 — first-try probes always-on for this build

Remote tester can't run logcat (the device they have access
to isn't local). The previous env-var gate
(``AZT_DEBUG_FIRST_TRY=1``) was the wrong shape — they have
no way to set env vars on the device. Flipping the
``first_try_log`` gate to always-on for this build so the
probes write to peer stderr (which lands in
``/sdcard/azt_recorder.log``) without the tester having to
configure anything.

Restore the env gate after the crash is diagnosed; the gate's
``if not os.environ.get('AZT_DEBUG_FIRST_TRY'): return`` is
preserved in the module docstring for easy reinstatement.

### azt_collabd 0.41.15 — ContentProvider waits for Python callbacks (H5 defensive fix)

The "first-try-fails, second-try-works" pattern reported on
the Tecno KN4 (Helio G81, 4 GB RAM, Android 16) is most likely
Android killing the daemon's ``:provider`` process during a
user's brief navigation away (settings screen, etc.) and then
lazy-spawning it on the next peer call. The respawn race:
Android creates the AZTCollabProvider Java object and routes
the incoming peer call to it on a binder thread, while
Python's ``install_callbacks()`` is still initializing on
SDLThread. The provider sees ``sDispatch == null`` /
``sOpenFile == null`` and returns ``daemon_not_ready``, which
the peer surfaces as a crash. Second tap: Python is now
initialized, callbacks registered, call succeeds.

Defensive fix in ``AZTCollabProvider.java``: ``call()`` and
``openFile()`` now wait up to 3 seconds (50 ms polling) for
the Python callback to register before returning the
"daemon_not_ready" error. On a healthy respawn, Python
finishes ``install_callbacks()`` in well under a second and
the first peer call queues briefly behind it instead of
failing. On a truly-down daemon, the 3 s timeout still fires
and the failure surfaces the same as before.

Harmless if the bug wasn't H5: the wait loop only runs while
the callbacks are null, which is the respawn window only.
``ping`` requests (used by discovery probes) still bypass the
wait so transport-discovery latency is unaffected.

### azt_collabd 0.41.14 / azt_collab_client 0.41.14 — env-gated first-try-fails diagnostics

User reported a transient crash on the SettingsScreen →
"select new project" path: first try crashes, second works.
Nothing in logcat suggests a cause. Added probes for five
hypotheses, all gated behind ``AZT_DEBUG_FIRST_TRY=1`` so
they're inert when the env var isn't set. New helper
``azt_collab_client/_debug.py`` provides ``first_try_log``.
Probes:

- H1 (cache poll leaks past screen leave): in
  ``SettingsScreen._stop_cawl_cache_poll`` and
  ``_tick_cawl_cache_status`` — logs Clock event lifecycle
  + current screen at every tick.
- H2 (picker cold-start race): in
  ``ProjectPickerScreen.on_enter`` and ``_populate_projects``
  + ``picker_app.main`` — timestamps each phase.
- H3 (subprocess invocation): in ``picker_app.main`` entry
  + return — argv + dt.
- H4 (URI grant not propagated): in
  ``lift_io._open_content_uri`` — wraps
  ``openFileDescriptor`` with explicit exception logging
  so any swallowed ``SecurityException`` surfaces.
- H5 (daemon respawn drops the call): in
  ``transports.android_cp.call`` — logs bundle-null on
  return.

Enable with ``adb shell setprop … AZT_DEBUG_FIRST_TRY 1``
or by setting the env var in the launch path. When set,
``[first-try] <label> k=v ...`` lines appear in logcat at
each probe site.

### azt_collabd 0.41.13 / azt_collab_client 0.41.13 — CAWL: TTL-cached os.walk + quieter resolve logs + S.SERVER_UNAVAILABLE / S.SERVER_ERROR constants

**Cache-count undercount fixed.** 0.41.10's incremental
counter (lazy-seed + per-fetch increment) had a race that
produced an undercount in the wild (peer warmed 1661, daemon
reported 1257). Tracing didn't fully pin the race but the
failure-mode (silently wrong UI total) is bad enough that I
replaced the scheme rather than patching it. ``_walk_image_count``
is now a TTL-cached ``os.walk`` — 500 ms TTL, ~50 ms uncached
on the canonical 1700-image set, near-zero CPU at 1 Hz
polling. Accurate by construction: it counts what's actually
on disk, no event-based bookkeeping that can drift. Dropped
``_note_image_cached``, ``_cached_image_count``,
``_cached_count_seeded`` and their call sites. The TTL-cached
walk fallback is only used when no prefetch job is active for
the repo (otherwise the prefetch state still wins; same logic
as 0.41.11).

**Quieter resolution logs.** The
``[cawl] get_image_path: no index-resolution for X`` line was
firing for already-nested paths even though those paths
needed no resolution and the fetch was succeeding. Net effect
was a logcat full of scary "no index-resolution" lines for
calls that were working fine. Now: pass-through is silent;
the "flat basename not in index" case still logs because
that's a real "peer asked for something the daemon doesn't
know" situation.

**``S.SERVER_UNAVAILABLE`` / ``S.SERVER_ERROR`` constants.**
The peer-routing example in ``azt_collab_client/CLAUDE.md``
("Peer contract: routing on sync results") shows
``result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR)`` but
``status.py`` didn't actually export those constants —
``AttributeError`` at runtime for conformant peer code. The
string literals were always emitted on results
(``Status('SERVER_UNAVAILABLE', …)`` from the wrappers'
transport-failure branches); only the typing-aid constants
were missing. Added to both ``azt_collab_client/status.py``
and the mirrored ``azt_collabd/status.py``. Peer note filed
2026-05-13 against 1.41.16 was the trigger.

### azt_collabd 0.41.12 / azt_collab_client 0.41.12 — quiet the 1 Hz poll logs

Three log-noise sources fired once per second once the
cache-status poll kicked in:

- ``[recent] GET /v1/recent/last_project → 'X' (from ...)``
  (daemon-side, ``_h_get_last_project``).
- ``[recent] last_project → 'X'`` (client-side wrapper).
- Both happen because the daemon UI's poll resolved
  ``last_project()`` every tick to know which project to
  query.

Fixes:

- Drop the success log from both sites. Error paths still
  log (``ServerUnavailable``, ``not ok``); they're rare and
  useful. The setter (``_h_set_last_project``) still logs
  because it's a real state change.
- Daemon UI now resolves the langcode once at poll start
  (``_start_cawl_cache_poll``) and reuses it across ticks.
  The user doesn't switch projects while sitting on the
  settings screen, so two RPCs/sec just to re-confirm the
  same langcode was overhead with no benefit.

### azt_collabd 0.41.11 / azt_collab_client 0.41.11 — daemon-driven CAWL prefetch + accurate progress

The 0.41.9 cache-status indicator reported "files on disk vs.
all image-shaped entries in the index". For the canonical
``kent-rasmussen/images_CAWL`` set that's "959 / 3231" — the
total is the count of every image file across all variants,
but the peer typically warms one variant per CAWL identifier
(~1661 files). The banner plateaued at ~1661/3231 with no way
for the user to tell whether the system was done. Misleading
for the indicator's core purpose ("don't disconnect, you're
not done yet").

Root cause: the peer iterated its working-set and called
``get_image_path`` per entry. The daemon saw a stream of
independent requests with no "session" concept; its
progress-reporting could only count the on-disk count
against the index's total — a structural over-count.

This release flips the iteration. Peer hands the daemon a
single list of paths via ``POST /v1/projects/<lang>/cawl/
prefetch``; daemon spawns a background worker that iterates
the list, warms each path through ``get_image_path`` (cache
hit or GitHub fetch), and tracks per-job ``requested`` /
``completed`` / ``failed`` counters. ``cache_status`` now
reads from that job state when a prefetch has run, so the
progress bar reflects work the peer actually wants done.

**Endpoints.**

- ``POST /v1/projects/<lang>/cawl/prefetch`` with body
  ``{paths: [...]}`` — kicks off the worker, returns
  ``{requested, completed, finished}`` immediately. Idempotent:
  a second call with the same paths-set against an active job
  returns the existing state; a call with a different set
  replaces it.
- ``GET /v1/projects/<lang>/cawl/cache_status`` — unchanged
  wire shape (``{image_repo, cached, total}``). When a
  prefetch is active or completed for the repo, ``cached`` =
  job's ``completed`` and ``total`` = job's ``requested``,
  giving a progress bar that ends at 100%. Falls back to the
  old "on-disk vs. index" semantics when no prefetch has run.

**Client wrapper.** ``cawl_prefetch(langcode, paths)`` returns
the initial state dict; peers chain it with the existing
``cawl_cache_status(langcode)`` poll for progress display.

**On-demand path untouched.** ``CAWLHandle(...).open_read``
still works for any individual image; daemon serves from
cache or fetches on demand exactly as before. The new path
is just for bulk warming; individual reads share the same
cache.

**Daemon UI banner.** Updated to reflect the same numbers —
when a prefetch is in flight, the daemon UI's top-banner
shows "Caching images: M / N (network in use — please stay
online)" against the job's actual counts. Auto-hides when
``cached >= total``.

**Logging.** Removed ``_touch_project`` from the
``cache_status`` handler — a 1 Hz status poll isn't "user is
working on this project" signal and was flooding logcat with
``[recent] _touch_project`` lines.

**Peer adoption.** Contract updated at
``CLIENT_INTEGRATION.md`` § 10 with the new wiring shape and
the rationale ("daemon-driven, not peer-driven"). Peers
calling ``CAWLHandle.open_read`` in a loop for bulk warming
should migrate to ``cawl_prefetch`` for accurate progress;
their on-demand single-image calls don't need to change.

### azt_collabd 0.41.10 — CAWL cache-status: top-banner placement + 1 Hz poll + memoised counts

Follow-on to 0.41.9. Three iterations on the cache-status
indicator after first contact with the user:

**Banner moves to the top of the SettingsScreen.** Previously
the status line was inside the bottom Status section, which by
design is low-attention "what's going on" diagnostic info. The
caching indicator's whole purpose is the *opposite* — grab
attention so the user doesn't disconnect network. Now lives
as a pinned BoxLayout banner directly under the TopBar, above
the ScrollView, so it can't scroll out of view. Accent-coloured
background + bold text + an explicit "please stay online"
clause in the message.

**Poll interval drops to 1 Hz.** 5-second polls made the
counter look broken — 10+ images cached per refresh produced
visible-but-jumpy progress. 1 Hz feels live.

**``cache_status`` is now near-zero cost per call.** The 1 Hz
poll needed this: the previous implementation did one
``os.walk`` over the daemon-owned images dir (~50-100 ms for
the canonical 1700-image set) plus one ``_read_cached_index``
JSON parse per call. At 1 Hz that's 5-10% daemon CPU, hot
enough to notice on a phone.

Memoisation:

- ``_cached_image_count[repo]`` is an in-memory counter. Lazy
  ``os.walk`` seeds it once per repo per daemon process;
  ``_note_image_cached(repo)`` increments it on every
  successful image-fetch + cache-write. Reset on daemon
  restart (re-seeds on first call). No invalidation needed —
  the counter only grows because cached images aren't
  removed.
- ``_total_count_cache[repo]`` is mtime-keyed. The
  ``_count_index_images`` lookup parses the cached index file
  once per (repo, index mtime); subsequent calls return the
  cached count. Invalidates automatically when ``get_index``
  refreshes and rewrites the cache file.

Net: cold-start ``cache_status`` is one ``os.walk`` + one JSON
parse; steady-state is a dict lookup + an ``os.path.getmtime``.

### azt_collabd 0.41.9 / azt_collab_client 0.41.9 — CAWL cache-status endpoint

The first cold-cache prefetch on the canonical
``kent-rasmussen/images_CAWL`` repo pulls ~1660 image binaries
sequentially through the daemon; on a typical mobile connection
this takes minutes during which the user has no in-app
indication that the daemon is using their network. They might
naturally disconnect Wi-Fi between gestures and end up with a
half-warm cache (every uncached image then has to fetch on
demand, which is exactly what the prefetch was avoiding).

This adds a project-scoped cache-status endpoint peers can
poll on a short interval to drive a "Caching images: M / N"
indicator:

- Daemon: ``GET /v1/projects/<lang>/cawl/cache_status`` →
  ``{ok, image_repo, cached, total}`` where ``cached`` is the
  count of image files in the on-disk cache for the project's
  resolved image_repo and ``total`` is the image-shaped index
  entries.
- Client: ``cawl_cache_status(langcode)`` wrapper returns the
  same shape as a dict (no Result wrapper — this is a status
  query, not a state-changing op). Empty values on any
  transport / not-found failure so the peer can poll
  unconditionally without exception handling.

Cost: one ``os.walk`` over the daemon-owned images dir per
poll. Bounded by total_count (~1700 files for the canonical
set); fast enough for a 5-second poll interval. No network.

**Daemon UI mirror.** The settings screen
(``python -m azt_collabd ui`` / ``open_server_ui()``) also
surfaces this progress: a small line in the Status section
that says "Caching images: M / N (network in use)" while a
prefetch is running, auto-hides when the cache catches up.
Polled on a 5-second ``Clock.schedule_interval`` while the
SettingsScreen is visible; cancelled in ``on_leave`` so it
doesn't wake the daemon for a screen the user can't see.

**Peer-side adoption.** The recorder / future viewer should
mirror the same indicator on their own loading screen so
users see the progress without navigating into Sync Settings.
Contract documented in
``azt_collab_client/CLIENT_INTEGRATION.md`` § 10 with the
copy-paste shape.

### azt_collabd 0.41.8 — CAWL: drop daemon-side offline backoff (peer has a circuit breaker already)

0.41.4 added a daemon-side 60s offline backoff that suppressed
``[cawl] image fetch failed`` log spam when a peer iterated a
~1700-image set on a device with no network. After diagnosis
of an "55 images succeed, then 10 fail silently" pattern in
the wild, the backoff was actively making things harder to
debug: when the daemon went silent it was impossible to tell
from the peer side whether a fetch had been attempted at all
or whether the daemon had short-circuited on a stale backoff
window. The peer already has its own circuit breaker that
suppresses pulls after N consecutive failures, so the daemon
log-spam concern was solved peer-side anyway.

This release rips out the daemon-side backoff entirely.
``get_image_path`` and ``get_index`` now attempt the fetch on
every cache miss (lock-coalesced, as before) and emit a
verbose log line per failure. The peer's circuit breaker (in
``lift.py: _CAWLImageResolver._pull``) is the right place for
the "stop trying after N failures" policy.

Removed: ``_OFFLINE_BACKOFF_SECONDS``, ``_offline_state_lock``,
``_offline_until``, ``_offline_suppressed``,
``_is_in_offline_backoff``, ``_note_fetch_failure``,
``_note_fetch_success``. The ``http.client.HTTPException``
catch added in 0.41.4 stays — InvalidURL et al. still must not
escape uncaught.

### azt_collabd 0.41.7 / azt_collab_client 0.41.7 — atomic_open_write via FD + finalize (Binder cap on writes)

Same Binder per-transaction cap (~1 MB) that broke the CAWL
index read in 0.41.2 also breaks the LIFT atomic write for
projects whose LIFT exceeds ~700 KB (base64 inflates 1.33× and
the JSON envelope blows past the cap). Symptom is identical:
``ContentResolver.call`` Bundle drops on the way to the
daemon, transport raises, ``atomic_commit_bytes`` returns
``SERVER_UNAVAILABLE``, ``_UriAtomicWriteFile.commit`` raises
``IOError``, the audio-save path (or any other write through
``LiftHandle.atomic_open_write``) fails. The user-visible
break is "stopping a recording loses the entry"; the daemon
never sees the call so there's no daemon-side log.

**Two-phase write.** Bytes now cross the IPC boundary via the
ContentProvider FD path (no Binder size cap), then a tiny
RPC finalizes the atomic rename under ``project_lock``:

1. Peer generates ``token = secrets.token_hex(16)``, opens
   ``content://<auth>/<lang>/_atomic_pending/<token>`` for
   write via ``ContentResolver.openFileDescriptor``, writes
   the buffered bytes through that kernel FD. The daemon's
   ``_resolve_path`` routes ``_atomic_pending`` to
   ``<working_dir>/.azt_atomic_pending/<token>`` (new write-
   only route, token-validated against
   ``^[A-Za-z0-9_-]{1,64}$``).
2. Peer calls ``POST /v1/projects/<lang>/atomic_finalize``
   with ``{token, path}``. The daemon validates the path
   against the same whitelist
   ``atomic_commit`` uses (``<file>.lift`` /
   ``audio/<file>`` / ``images/<file>``), reads the pending
   file for size + sha256, then ``os.replace``s it under
   ``project_lock``. Returns ``ATOMIC_COMMITTED`` with the
   same params shape as ``atomic_commit_bytes``.

**Atomicity preserved.** The rename still happens under the
project lock so concurrent peer-vs-peer / peer-vs-merge
writers can't tear the destination. Phase 1 writes to a
unique per-token scratch path so two concurrent peers don't
collide on the pending file either. The only new failure
surface is "phase 1 succeeds, phase 2 fails": the scratch
file may linger under ``.azt_atomic_pending/`` on the
daemon. No automatic GC yet — operationally OK since the
total scratch volume is bounded by recent failed writes.
The daemon best-effort unlinks on rename failure.

**Backward compatibility.** ``_UriAtomicWriteFile.commit``
falls back to the legacy single-RPC ``atomic_commit_bytes``
path if phase 1 raises (pre-0.41.7 daemon that doesn't know
``_atomic_pending``) or phase 2 returns ``SERVER_ERROR``
(daemon missing the route). The legacy path still works
against any 0.36.0+ daemon for payloads under the cap, so
small-LIFT projects don't regress on a mixed-version
deployment.

**Where the bump goes.** The daemon side ships in the server
APK. The client-side rewrite of ``_UriAtomicWriteFile.commit``
+ the new ``atomic_finalize_pending`` wrapper lives in
``azt_collab_client`` — peer apps bundle this at build time,
so peers must be rebuilt to pick up the new write flow. Once
a peer is on 0.41.7+, atomic LIFT writes of any size up to
~10 MB cross cleanly.

### azt_collabd 0.41.6 — CAWL: literal-%20 in filename + drop defensive url-field re-encode

Surfaced once 0.41.5 got us past the flat-basename resolution
to a real fetch URL: a subset of canonical ``kent-rasmussen/
images_CAWL`` filenames literally contain ``%20`` as part of
the filename (not as URL encoding for a space). The actual
on-disk filenames look like
``2d%20minimalistic%20black%20and%20white%20line%20art%20of%20right%20elbow__bw.png``.

``Uri.getPath()`` on Android URL-decodes once, so the Python
side receives the literal-character filename — including the
literal ``%20`` substrings. The previous
``quote(rel_path, safe='/%')`` preserved those ``%``
characters unchanged into the URL, so GitHub decoded ``%20`` →
space and looked for a file with literal spaces, which doesn't
exist — 404.

Fix: ``quote(rel_path, safe='/')`` (drop ``%`` from safe). The
``%`` in literal-``%20`` filenames now encodes to ``%25``,
producing ``%2520`` in the URL. GitHub decodes once → literal
``%20`` → matches the actual filename on disk.

Also removed the defensive ``url``-field re-encoding in
``_h_cawl_index`` that 0.41.3 added. With the new encoding
rules, ``_fetch_index_from_github`` now emits correctly-
encoded ``url`` fields, and the defensive re-encode would
double-encode them (``%2520`` → ``%252520``) and break peers
that actually use ``entry['url']``. The current Stage-2 peers
use ``CAWLHandle`` (which goes through the daemon's fetch
path, not the per-entry ``url``), so removing the defensive
layer doesn't regress anything we ship.

**Decision log: idempotent encoding vs. canonical encoding.**
0.41.4's ``safe='/%'`` was an attempt at idempotent encoding —
"if input is already encoded, don't double-encode." That logic
is fundamentally ambiguous: ``%20`` in the input means either
"encoded space" or "literal ``%20`` in the filename" and we
can't tell from input alone. The canonical-encoding rule
(always encode ``%`` to ``%25``) gives consistent semantics:
peer-side input is always literal-character (URI decoding
gives that for free), daemon always encodes once for HTTP.
Peers that want to pass pre-encoded paths are broken under
this model — they shouldn't.

### azt_collabd 0.41.5 — CAWL: flat-basename → nested-path resolution via index

The canonical ``kent-rasmussen/images_CAWL`` repo keeps images
under category subdirs (``0001_body/<basename>.png``,
``0002_head/<basename>.png``, …). A peer parsing the index
typically extracts a CAWL identifier + a flat basename, then
calls ``CAWLHandle(langcode, basename).open_read()`` with just
the basename — it doesn't need to track the category prefix
because every category is part of the same CAWL set.

Before this fix the daemon would receive that flat basename
and ask GitHub for ``HEAD/<basename>.png`` (top level) — which
returns 404 because the file actually lives at
``HEAD/0001_body/<basename>.png``. After the offline-backoff
kicked in on the first 404, all subsequent image fetches in
the same minute also went silent → user-visible "no images."

New helper ``_resolve_basename_via_index(repo, rel_path)``:
if ``rel_path`` is a flat basename (no ``/``) and the index
has exactly that basename under some nested path, the daemon
canonicalizes to the nested path before computing the cache
target / fetch URL. Both the on-disk cache and the GitHub
request use the canonical nested path; subsequent flat-basename
requests for the same file hit the cache directly because the
canonicalization is deterministic.

If the index isn't cached yet, ``_resolve_basename_via_index``
returns ``rel_path`` unchanged so the network fetch attempt
fails honestly (rather than silently rewriting to a wrong
path). The index seed is bundled in the APK, so this only
matters in the rare case where the seed is missing and a
network fetch hasn't run yet.

### azt_collabd 0.41.4 — CAWL: SSL via certifi + URL encoding + offline-backoff coalescing

Three related image-fetch fixes that surfaced once 0.41.3's
slim index let the peer actually request binaries.

**URL encoding for paths with spaces / unsafe chars.** Both the
per-file ``url`` field in ``_fetch_index_from_github``'s emitted
index and the request URL in ``_fetch_image_bytes_from_github``
now percent-encode the path component. ``safe='/%'`` so the
encoding is idempotent (won't double-encode an already-encoded
input). CAWL filenames commonly include spaces / commas /
parens; raw URLs containing those raise
``http.client.InvalidURL`` at ``_validate_path`` time, before
the request goes out. The except clause in ``get_image_path``
also now catches ``http.client.HTTPException`` (parent of
``InvalidURL``); previously it only caught ``OSError`` /
``URLError`` and an InvalidURL escaped uncaught past the
offline-backoff handler, turning into a Java-side
``FileNotFoundException`` with no peer-visible
``[cawl] image fetch failed`` log.

The rest of this entry covers two related fixes that ride on
the same release:

Two related image-fetch fixes that surfaced once 0.41.3's slim
index let the peer actually request binaries.

**SSL bundle on Android.** ``_fetch_index_from_github`` and
``_fetch_image_bytes_from_github`` were using raw
``urllib.request.urlopen`` without calling ``net._ensure_ssl()``
first. Every other network site in the daemon does call it; the
CAWL module was the lone holdout. On p4a Android (no system CA
store) this manifested as ``SSL: CERTIFICATE_VERIFY_FAILED``
for every image fetch. Fix: both call sites now call
``_ensure_ssl()`` before the urlopen. The patch is idempotent
and globally monkey-patches ``ssl._create_default_https_context``
to use certifi's bundle, so once it has run any stdlib HTTPS
works.

**Offline backoff (per-process, shared between index + image
fetches).** When a connect-class urllib error fires
(URLError / OSError / TimeoutError), the daemon now enters a
60s cooldown during which subsequent CAWL fetch attempts
short-circuit silently. Without this, a peer iterating a
~1700-image set on a fully-offline device (or one with
broken DNS / SSL) spammed logcat with 1700 near-identical
``[cawl] image fetch failed`` lines, drowning real signal.
Coalesced semantics:

- First failure in a fresh window → one verbose log line
  identifying the repo + cause.
- Subsequent failures in the same window → silent skip
  (no network attempt, no log).
- Any successful fetch → backoff cleared immediately + one
  ``[cawl] network recovered`` log with the suppressed count.

The window is per-daemon-process module-state; restart
clears it. ``_OFFLINE_BACKOFF_SECONDS = 60`` is tuned for
"long enough that a 1700-image swipe-prefetch loop quiets
down, short enough that the user reconnecting wifi gets
images within a minute". Index lookups in the window serve
from cache (stale OK) per the existing fallback policy.

### azt_collabd 0.41.3 — slim CAWL index over JSON-RPC (image extensions only)

Server-side companion to 0.41.2's FD-route client fix. The
0.41.2 fix only helps peers that have rebuilt against the
updated ``azt_collab_client``; existing peer installs keep
calling ``cawl_index`` over the JSON-RPC path and keep
receiving an empty response because the ~1.5 MB index Bundle
exceeds the Binder per-transaction cap.

Per the recorder peer's 2026-05-13 filing
(NOTES_TO_DAEMON.md), the daemon now filters the index
response to ``.png`` / ``.jpg`` / ``.jpeg`` paths before
serializing. The canonical ``kent-rasmussen/images_CAWL`` repo
includes ~3700 non-image blobs (README, LICENSE, .gitignore)
that every peer's parser discards on receipt anyway —
filtering server-side just stops shipping bytes nobody uses.
Reduces the wire from ~5479 entries (~1.5 MB) to ~1700
entries (~470 KB), well under the Binder ceiling.

**Where the filter applies.** Only the JSON-RPC dispatch
(``_h_cawl_index``). The file-route URI
(``<lang>/cawl/index.json`` via ContentProvider, which 0.41.2
clients use on Android) still serves the raw cache file
unfiltered — file FDs have no Binder size cap, so there's no
reason to slim, and the peer self-filters on extension in
either case. So:

- Pre-0.41.2 peer + 0.41.3 daemon → JSON-RPC path, ~470 KB,
  fits, peer gets a populated index without rebuilding.
- 0.41.2+ peer + 0.41.3 daemon → FD path, full index,
  unaffected.
- Pre-0.41.2 peer + pre-0.41.3 daemon → JSON-RPC path, full
  index, Binder drops the Bundle, peer reads empty (the
  regression).

**Decision log: filter at serve, not at cache.** The on-disk
cache (``$AZT_HOME/cawl/<owner>/<repo>/index.json``) keeps
the canonical full set GitHub returned. Cache stays repo-
faithful; serve-time filter is cheap and reversible. A
future endpoint that wants the full set (admin UI, indexing
tool) can read the cache directly.

### azt_collabd 0.41.2 / azt_collab_client 0.41.2 — CAWL index over file FD on Android (Binder 1 MB cap)

Patch fix: the daemon was serving the populated CAWL index
(``files=5479`` in the success log added in 0.41.1), but the
peer's ``cawl_index(langcode)`` wrapper read ``{}`` — peer
logged ``[cawl] _load: ... repo='' files=0`` and never
requested any images. Root cause: ``ContentResolver.call``
ships responses as a ``Bundle`` over Binder, which caps
single transactions at ~1 MB. The populated index
(~1.5 MB with 5000+ entries × long GitHub raw-content URLs)
exceeds the cap; the Bundle is dropped on the way back, the
peer's transport raises (caught by the wrapper as
``ServerUnavailable``), and the wrapper returns ``{}``. The
daemon-side success log fires regardless because the handler
ran — the gap is in the IPC return trip, not the dispatch.

**Fix.** On Android, ``cawl_index`` now reads the on-disk
index file directly via the ContentProvider's existing file
route (``<lang>/cawl/index.json``). ``ContentResolver.openFile
Descriptor`` returns a kernel FD with no Binder size cap; the
peer reads the JSON bytes and parses locally. The daemon's
``_resolve_cawl_path`` already populated the cache via
``cawl.get_index`` before returning the path, so the file is
guaranteed present (seed-on-cold-cache covers the
no-network-on-install case). Desktop loopback HTTP has no
such cap and keeps the JSON-RPC path.

**Client (azt_collab_client):**

- ``cawl_index(langcode)`` now branches on platform:
  Android → file-route via new ``lift_io._cawl_index_via_fd``
  helper; desktop → existing ``GET
  /v1/projects/<lang>/cawl/index`` over loopback HTTP.
  Empty-on-failure contract preserved on both paths.
- New ``lift_io._cawl_index_via_fd(langcode)`` — opens
  ``content://<authority>/<lang>/cawl/index.json`` via
  ``_open_content_uri``, reads, parses. Same URI shape
  ``CAWLHandle`` uses for image bytes.

**Decision log.** Not changing the daemon-side wire shape;
the JSON-RPC endpoint stays correct and serves desktop. The
asymmetry (HTTP path on desktop, FD path on Android) lives
on the client side because that's where the Binder cap is
visible. A future symmetric refactor could move both peers
to the FD path uniformly, but desktop has no FD provider
available without adding a new loopback file-serving
endpoint — not worth the surface area for an IPC-layer
workaround.

### azt_collabd 0.41.1 / azt_collab_client 0.41.1 — CAWL nested paths + success-path logging

Patch fix: 0.41.0's CAWL image fetching silently rejected any
``rel_path`` containing ``/``, which is exactly the shape the
canonical ``kent-rasmussen/images_CAWL`` repo uses
(``0001_body/foo.png``-style category subdirs). Net effect:
the seed index was served fine, but every per-image request
returned silently → peers saw no images, no daemon log line
recorded the failure. This release accepts nested rel-paths
and adds success-path logging so the next similar gap is
visible from logcat alone.

**CAWL daemon (azt_collabd/cawl.py):**

- ``_looks_safe_basename`` → ``_looks_safe_rel_path``. Accepts
  ``/`` between components; rejects ``..``/``.``, absolute
  paths, backslashes, and empty components.
- ``get_image_path`` now takes ``rel_path`` (not ``basename``).
  Composes the on-disk target under
  ``<cache_root>/<repo>/images/<rel_path>``; verifies
  containment with ``realpath`` + ``commonpath`` (belt-and-
  braces against symlink tricks). Creates intermediate cache
  subdirs on first write.
- ``_fetch_image_bytes_from_github`` URL-encodes each path
  component for the raw URL (``urllib.parse.quote(path,
  safe='/')`` — keeps slashes between components intact;
  encodes spaces, commas, parens, etc. that CAWL filenames
  commonly contain).

**Transport routing:**

- ``android_cp._resolve_cawl_path`` accepts 2+ segments under
  ``images/`` (was strict ``[images, basename]``). Joins
  remaining segments back into the rel-path that
  ``cawl.get_image_path`` validates.
- ``server._match_cawl_image_path`` accepts 7+ segments; per-
  component URL-decodes via ``urllib.parse.unquote``; rejects
  post-decode traversal tricks (``%2E%2E`` → ``..`` and
  ``%2F`` → ``/`` inside a single segment).

**Client (azt_collab_client/lift_io.py):**

- ``CAWLHandle(langcode, rel_path)`` — the ``basename`` arg
  renamed to ``rel_path``. ``handle.basename`` kept as a
  read-only alias for back-compat with peer log lines.
- ``CAWLHandle.open_read`` URL-encodes the rel-path with
  ``urllib.parse.quote(safe='/')`` before composing the
  ``content://`` URI or the loopback HTTP URL. Slashes
  between components preserved; unsafe characters percent-
  encoded.

**Success-path logging — new in both endpoints:**

- ``[cawl] served index for repo=… langcode=… files=N`` on
  every successful ``_h_cawl_index``. ``files=0`` is the
  early-warning signal that something's upstream-wrong
  (empty seed, mis-resolved repo, …).
- ``[cawl] served image repo=… path=… bytes=…`` on every
  successful ``_h_cawl_image_bytes``.
- ``[cawl] image rejected: project_not_found / no_image_repo_configured``
  and ``[cawl] image unavailable: repo=… path=…`` on the
  refusal paths. The 0.41.0 bug went unseen because none of
  the success-or-rejection paths logged — only the
  network-fetch-failed path did.

**Tests:** new in ``tests/test_cawl.py``:

- ``test_get_image_path_accepts_nested_rel_path`` — regression
  test for the 0.41.0 bug.
- ``test_get_image_path_accepts_spaces_and_special_chars`` —
  CAWL filenames in the canonical repo have these.
- ``test_fetch_url_encodes_path_components`` — slashes
  preserved, unsafe chars percent-encoded.
- ``test_match_cawl_image_path_accepts_nested_path``,
  ``_url_decodes_components``,
  ``_rejects_traversal_post_decode``,
  ``_rejects_slash_in_decoded_segment``.
- ``test_h_cawl_image_bytes_serves_nested_rel_path`` —
  end-to-end through the binary handler.
- ``test_resolve_path_cawl_image_accepts_nested`` — through
  the ContentProvider routing.
- Existing path-traversal test updated to remove
  ``sub/file.jpg`` from the rejected-shapes list and add
  ``a/../b`` / ``foo//bar`` as new rejection cases.

**Floor:** patch bump. No wire-format change beyond accepting
strictly more rel-path shapes. Pre-0.41.1 daemons reject
nested paths silently; pre-0.41.1 clients that pass flat
basenames work against 0.41.1 daemons unchanged (the new
endpoints accept both).

## [0.41.0] - 2026-05-12

### azt_collabd 0.41.0 / azt_collab_client 0.41.0 — collaborator UI consolidation + QR share/scan

Project-bound actions (Grant collaborator, Share repo) move into
the daemon settings UI's SettingsScreen, bound to the
``last_project()`` the daemon already tracks. Peers shrink to
a single "Open Sync Settings" button (``open_server_ui()``) for
these flows — same pattern as the GitHub Connect / GitLab
forms already on this screen.

Plus a QR pair: the daemon UI generates a QR of the published
repo URL ("Share this repo"), and the picker's clone flow
scans QRs to pre-fill its URL textbox.

**Daemon UI (azt_collabd/ui/app.py):**

- New ``project_actions_row`` in SettingsScreen, gated on
  ``last_project()`` resolving to a project that has a
  ``remote_url``. Mutually exclusive with the existing
  ``publish_row`` (which is gated on no remote) — the user
  sees one "what can I do with this project" surface at a
  time, appropriate to the project's current state.
- ``Grant collaborator access`` button → invokes the shared
  ``grant_collaborator_popup(langcode=last_project())``. The
  popup itself already lives in
  ``azt_collab_client.ui.popups`` (no work needed there).
- ``Share this repo (QR)`` button → opens a new
  ``_show_share_repo_qr_popup`` that renders the remote URL
  as a QR via segno + a Kivy ``Image`` widget, with a "Copy
  URL" fallback that goes through ``kivy.core.clipboard``.
- ``SettingsScreen.publish()`` updated for the 0.40.0 wire —
  no longer passes ``contributor=`` to ``init_project``
  (daemon reads from store). Adds ``S.CONTRIBUTOR_UNSET`` to
  the publish-failed-codes set so the publish msg routes
  correctly when the user hasn't entered their name yet.
- ``CollabUIApp.font_name`` now an instance attribute (was
  a local in ``build()``) so screens can pass it to shared
  popups for visual consistency (CharisSIL across daemon UI
  surfaces).

**QR generation (segno):**

- New requirement: ``segno`` in
  ``server_apk/buildozer.spec`` (and ``.tmpl``). Pure-Python,
  ~50 KB, no native deps. PNG output uses Pillow which Kivy
  already pulls in.
- ``_show_share_repo_qr_popup(url, langcode, font_name)`` —
  module-level helper in ``app.py``. Generates the QR with
  ``error='M'`` (15% correction, good camera tolerance) and
  ``scale=8`` (~250 px square in the popup). Falls back to
  ``_show_segno_missing_popup`` on desktop installs where
  segno isn't pip-installed.

**QR scan (zxing-android-embedded):**

- New requirement: ``com.journeyapps:zxing-android-embedded:4.3.0``
  in ``android.gradle_dependencies`` (server APK only). ~500 KB
  AAR; pulls in the camera-preview CaptureActivity + barcode
  decoder. Android-only.
- New permission: ``CAMERA`` in ``android.permissions``. ZXing
  requests the runtime grant itself at CaptureActivity launch;
  we only need the manifest entry.
- New module ``azt_collab_client/ui/qr_scan.py``:
  ``scan_qr(on_result, on_cancel, prompt)`` launches ZXing's
  ``IntentIntegrator``, reads ``SCAN_RESULT`` from
  ``onActivityResult``, marshals the callback to the Kivy main
  thread. ``available()`` is the cheap probe peers use to gate
  the UI affordance on platforms where ZXing isn't bundled.
- ``clone_url_popup`` (the picker's clone-by-URL flow) grows a
  "Scan QR" button next to the URL textbox when
  ``qr_scan.available()`` is True. On scan success the
  textbox is filled with the decoded URL and the existing
  ``_refresh_label_from_url`` derives the langcode. Desktop
  / no-ZXing builds keep the original "paste URL" UI.

**Floor:** no bumps. Daemon UI additions are server-side only;
peer wire surfaces unchanged. Pre-0.41 daemons / clients
interoperate normally (peers just don't see the new daemon-UI
buttons, which is fine — those weren't there before either).

**Recorder follow-up (deferred to a NOTES_TO_PEERS item back
to the recorder team).** Now that the consolidated surface
exists in the daemon UI, the recorder's CollabScreen Publish +
Grant collaborator sub-screens become redundant. Strip-out
follows Phase 3 from NOTES_TO_DAEMON.md (don't combine with
this release — peer that strips before daemon grows the
replacement loses the feature entirely until both sides
converge).

### azt_collabd 0.40.0 / azt_collab_client 0.40.0 — commit author moves to daemon; device-name disambiguator

Two coordinated changes, one release:

1. **Contributor name is strictly daemon-owned now.** Peers no
   longer pass a commit-author name on the wire; daemon endpoints
   ignore any ``body['contributor']`` and read the stored value
   directly. If no name is set, commit-issuing endpoints refuse
   with the new ``S.CONTRIBUTOR_UNSET`` status — peers route the
   user to the daemon settings UI (``open_server_ui()``) to set
   their name rather than silently producing meaningless
   ``"Recorder"`` commits.
2. **New ``device_name`` field** disambiguates commits when the
   same human contributes from multiple devices. The git author
   email slot becomes ``<safe_contributor>@<safe_device>`` so
   GitHub's author-aggregation still groups by person, while
   ``git log --format='%ae'`` differentiates by device. Auto-
   populates from the OS on first read (Android:
   ``Settings.Global.DEVICE_NAME`` → ``Build.MANUFACTURER +
   MODEL``; desktop: ``socket.gethostname()``); user can override
   via the settings UI for a friendlier label.

**Why this lands together.** Both are corollaries of the
"daemon is the sole authoritative source for per-user state"
rule (NOTES_TO_DAEMON.md, recorder 1.41.3 filing). Pre-0.40 the
contributor name was duplicated across peer and daemon, with
the peer's pass-through silently winning even when the user
typed a name in the daemon UI; the literal ``"Recorder"``
default in the client wrapper turned every peer that didn't
override it into a commit signed "Recorder". 0.40 closes both
issues by removing the wire surface and replacing the
placeholder ``@device`` email slot with a real disambiguator.

**Wire changes:**

- ``POST /v1/projects/init`` — ignores ``body['contributor']``.
- ``POST /v1/projects/<lang>/sync`` — ignores ``body['contributor']``.
- ``POST /v1/projects/<lang>/sync_async`` — ignores
  ``body['contributor']``; the enqueued job runs against the
  stored contributor at exec time. If unset, scheduler returns
  ``Result(CONTRIBUTOR_UNSET)`` which peers see via
  ``poll_job(job_id)``.
- ``GET /v1/config/device_name`` — new. Returns the stored or
  auto-detected device name (always non-empty after first read).
- ``POST /v1/config/device_name`` — new. Sets / clears the
  override. Whitespace stripped; empty clears and re-triggers
  autodetect on next read.

**Client API changes:**

- ``init_project(working_dir, remote_url, branch='main')`` —
  ``contributor`` kwarg removed.
- ``sync_project(langcode)`` — ``contributor`` parameter
  removed.
- ``request_sync(langcode)`` — ``contributor`` parameter
  removed.
- New ``get_device_name()`` / ``set_device_name(name)``
  wrappers, exported in ``__all__``.
- New ``S.CONTRIBUTOR_UNSET`` status code, translation in
  ``translate.py``.

**Daemon-side changes:**

- ``store.resolve_contributor`` **removed**. Was the host of the
  ``'Recorder'`` fallback. Any in-tree caller that still imports
  it fails at import time — fail-loud.
- ``store.get_device_name`` / ``store.set_device_name`` new.
  Auto-populates on first read; persists the autodetect so
  subsequent reads are stable.
- ``repo._default_author(contributor, device_name=None)`` —
  ``device_name=None`` lazy-looks-up via ``store.get_device_name()``;
  ``''`` explicitly skips the lookup (deterministic test
  output). Email slot is ``<safe_contributor>@<safe_device>``;
  the literal ``@device`` placeholder is gone.
- ``scheduler.Job`` no longer carries a ``contributor`` field;
  ``request_sync(langcode)`` signature drops the second
  positional. Pre-0.40 ``jobs.json`` entries with the field
  decode cleanly (ignored on load).
- ``_h_init_project`` / ``_h_project_sync`` refuse upfront with
  ``S.CONTRIBUTOR_UNSET`` when ``store.get_contributor()`` is
  empty. ``_h_project_sync_async`` enqueues unconditionally;
  the scheduler's exec-time re-check at ``_run_sync`` is the
  defence-in-depth.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bumps. Pre-0.40 clients that still pass ``contributor`` in the
body get their value silently ignored (strict improvement —
the override they sent was the bug). Pre-0.40 daemons talking
to a 0.40 client work unchanged (the client just doesn't send
the field). No corruption surface, no forced cut-over.

### azt_collabd 0.39.0 / azt_collab_client 0.39.0 — per-project `repo_slug` field

Closes the recorder 1.41.3 ask from
``NOTES_TO_DAEMON.md``: the GitHub-repo-name override that the
publish path uses now lives on the daemon's project record, not
in peer prefs.

**Why this is needed.** Most projects publish to a repo named
after the project's ``langcode``, and the daemon's
``projects.json`` key is the right value to display. But a user
can legitimately want a *different* repo name (vanity slug,
project-style naming convention, collision avoidance with an
existing GitHub repo) without changing the LIFT
``<form lang="…">`` tag. Pre-1.41.3 the recorder persisted that
override as a suite-wide ``peer_pref`` scalar
(``collab_langcode``), which was wrong on two counts:
peer-prefs are global but the override is per-project, and
peer-side storage of project-identity data violates the
no-daemon-owned-caches rule (also documented in
``NOTES_TO_DAEMON.md`` "Daemon is now the sole authoritative
source"). 1.41.3 dropped the peer-side mirror; this release
gives the data its canonical daemon-side home.

**Wire shape:**

- ``Project.repo_slug`` field (string, default empty).
  Returned by ``open_project`` / ``project_status`` /
  ``list_projects`` so peers can read it without an extra
  round-trip.
- New endpoint ``POST /v1/projects/<lang>/repo_slug`` —
  body ``{repo_slug: '<name>'}``. Whitespace stripped before
  persist. Empty string explicitly clears (callers fall back
  to using ``langcode``). 404 on unknown project, 400 on
  missing field.
- New client wrapper ``set_repo_slug(langcode, slug)`` —
  returns the updated ``Project`` or ``None`` on transport
  failure / unknown project, same shape as
  ``set_cawl_image_repo``.
- ``register_project`` now accepts ``repo_slug=…`` for the
  initial-creation path (alongside the existing
  ``cawl_image_repo`` kwarg). ``None`` preserves any existing
  value; ``''`` explicitly clears.

**Default-semantics rule for callers:** unset / empty
``repo_slug`` is the typical case — callers should treat that
as equal to ``langcode``. The daemon does NOT auto-fill the
field with the langcode; the field stays empty until the user
explicitly overrides. That keeps "did the user actually choose
a different name?" decidable from the data alone.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bumps. The field is purely additive — pre-0.39 daemons don't
emit it, the client-side dataclass defaults to ``''`` for
forward-compat. A 0.39 client calling ``set_repo_slug``
against a pre-0.39 daemon gets ``None`` (the endpoint returns
404), which is the same failure shape every other setter
wrapper uses.

**Removed from NOTES_TO_DAEMON.md:** the "Per-project
repo-slug override (publish path)" entry — shipped.

### azt_collabd 0.38.1 / azt_collab_client 0.38.1 — CAWL index seed (install-day-no-network)

Closes the install-day-no-network gap that Stages 1+2 (0.38.0)
couldn't solve on their own: a freshly-installed device that has
never reached GitHub now has a bundled CAWL index to serve, so
peers can render illustrations on first launch.

**Daemon side:**

- New ``_seed_index_if_bundled(repo)`` in ``azt_collabd/cawl.py``.
  ``get_index(repo)`` calls it before going to the network when
  the on-disk cache file is missing — copies the bundled asset
  from ``azt_collabd/data/cawl/<owner>/<repo>/index.json`` into
  ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` if it exists.
- Silently no-ops when (a) no seed is shipped for the requested
  repo, (b) the cache already has data, (c) the bundled JSON is
  malformed. The first case is the common one: only the
  suite-canonical repo is typically seeded; fork / per-project-
  override repos keep their old install-day behaviour (no
  illustrations until the network fetch lands).
- The seed is treated as an ordinary cache entry once written:
  fresh-within-TTL → serve directly; past-TTL → attempt a
  network refresh and fall back to the seed if offline (the
  pre-existing stale-cache fallback). When the device first
  gets online, the next refresh overwrites the seed with current
  data.

**Bundle layout:**

```
azt_collabd/data/cawl/
    <owner>/<repo>/
        index.json
```

Subdirectory name matches the on-disk cache layout exactly.
``azt_collabd/data/cawl/generate_seed.py`` is the maintainer
script: with no args it refreshes the suite-canonical seed
(daemon-global default ``kent-rasmussen/images_CAWL``); pass
``owner/repo`` or set ``AZT_CAWL_IMAGE_REPO`` for a fork /
non-canonical image set. Uses the same
``cawl._fetch_index_from_github`` codepath the daemon does at
runtime, writing to the right directory.
``azt_collabd/data/cawl/README.md`` documents the wire shape +
when to re-run.

The daemon-global ``cawl_image_repo`` default is no longer empty
— recorder 1.41.3 removed its own hard-coded fallback under the
no-daemon-owned-caches rule, so the daemon is now the sole
source of this slug at runtime. Default set to
``kent-rasmussen/images_CAWL`` to preserve the recorder's
pre-1.41.3 behavior; fork shipping a different CAWL set should
override via ``azt_collabd.configure(cawl_image_repo=…)`` or
``AZT_CAWL_IMAGE_REPO``.

**Buildozer:**

- ``server_apk/buildozer.spec`` and ``buildozer.spec.tmpl`` add
  ``json`` to ``source.include_exts`` so the bundled seed lands
  in the APK. No other build-config changes; new seed
  directories under ``azt_collabd/data/cawl/`` are picked up
  automatically.

**What's NOT bundled, and why:**

The image binaries themselves are explicitly **not** in the
seed. 1701 images at 50–200 KB each ≈ 100–300 MB per APK
release — wrong trade for a one-time first-launch UX gain.
Image rendering on day-one without connectivity simply doesn't
happen; the user gets illustrations once the device first
reaches ``raw.githubusercontent.com``, with the daemon-side
lazy cache (shipped in 0.38.0) covering steady-state perfectly
fine. If a future session proposes "bundle the whole CAWL
image set in the APK", that's a re-litigation of a 2026-05-12
decision — answer is no.

**Floor:** patch bump (no wire-format change). The seed is
purely additive on the daemon side; pre-0.38.1 clients get
exactly the same wire shape from ``GET /v1/projects/<lang>/
cawl/index`` — they just benefit from a populated cache they
didn't have to fetch themselves.

### azt_collabd 0.38.0 / azt_collab_client 0.38.0 — CAWL Stage 2: per-project image_repo, image-binary RPC, first non-JSON endpoint

Completes the CAWL daemon-side migration that 0.37.0 started, and
corrects 0.37.0's daemon-global ``cawl_image_repo`` stopgap to a
per-project field on the Project record. CAWL is now a fully
daemon-owned suite-scoped resource: peers consume both the index
and the image binaries via the daemon, with one cache per repo
per device regardless of peer count.

**The reframing.** 0.37.0 used a daemon-global ``cawl_image_repo``
configured at daemon startup. That conflicted with the
"sole authoritative source" architectural invariant the recorder
1.41.3 just established (NOTES_TO_DAEMON.md): per-project
identity / configuration data belongs on the project record, not
in peer prefs and not in daemon-global config. Different projects
can legitimately point at different image sets (vanity fork,
culturally specific imagery, etc.) so the slug is a per-project
override with a daemon-global fallback.

**Project record (azt_collabd/projects.py):**

- New ``Project.cawl_image_repo`` field (string, default empty).
  Empty falls back to the daemon-global default; non-empty
  overrides for this project.
- ``register(..., cawl_image_repo=None)`` accepts the kwarg.
  ``None`` preserves any previously-set value across
  re-registration; empty string explicitly clears.
- New ``set_cawl_image_repo(langcode, repo)`` setter for the
  endpoint.
- Client-side ``Project`` mirror gains the field with default
  empty (forward-compat with pre-0.38 daemons that don't emit it).

**Cache module (azt_collabd/cawl.py):**

- ``get_index(repo)`` now takes a repo slug. Cache moves to
  ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` so multiple
  projects pointing at the same image_repo share one cache
  directory.
- New ``get_image_path(repo, basename)``: lazy fetch from
  ``raw.githubusercontent.com``, cache at
  ``<owner>/<repo>/images/<basename>``, return absolute
  filesystem path. Path-traversal-safe basename validation.
  ``None`` when fetch fails and no prior cached copy exists.
- New ``resolve_image_repo(langcode)``: per-project value
  preferred; daemon-global ``config.cawl_image_repo()`` is the
  fallback for projects without an override.
- Lock-coalesced fetches keyed by cache file path (not module-
  wide), so two repos can fetch in parallel without
  serializing.

**Endpoints:**

- ``GET /v1/projects/<lang>/cawl/index`` (replaces the 0.37.0
  ``GET /v1/cawl/index``). Daemon resolves the project's
  cawl_image_repo internally; response carries
  ``index_repo`` alongside ``index`` so peers can see which
  repo answered.
- ``GET /v1/projects/<lang>/cawl/images/<basename>`` returns
  **raw binary image bytes**. First non-JSON endpoint on the
  loopback HTTP server; new ``_send_bytes`` handler bypasses
  JSON dispatch (the dispatch table stays JSON-only). Content-
  type derived from the file extension
  (``image/jpeg``/``png``/``gif``/``webp`` known; falls back
  to ``application/octet-stream``).
- ``POST /v1/projects/<lang>/cawl_image_repo`` setter for the
  per-project override. Body ``{cawl_image_repo: 'owner/repo'}``;
  empty string explicitly clears.

**ContentProvider (azt_collabd/android_cp/service.py):**

- ``_resolve_path`` extended with two new shapes:
  ``<lang>/cawl/index.json`` (3-seg, triggers lazy index fetch)
  and ``<lang>/cawl/images/<basename>`` (4-seg, triggers lazy
  image fetch).
- CAWL paths resolve to ``$AZT_HOME/cawl/<owner>/<repo>/...``
  (away from the per-project working_dir) so the dedup-by-repo
  property of the cache layer is preserved on Android.
- Write modes (``w``/``a``) rejected — peers don't write CAWL
  files. Returns ``None`` so the Java side surfaces
  ``FileNotFoundException``.

**Client side:**

- ``cawl_index()`` → ``cawl_index(langcode)``. The 0.37.0
  shape is gone; pre-0.38 callers must pass a langcode.
- New ``set_cawl_image_repo(langcode, repo)`` wrapper.
- New ``CAWLHandle(langcode, basename).open_read()`` —
  binary file-like for a CAWL image. Branches transport
  internally: Android opens the ContentProvider URI
  (zero-copy via kernel FD); desktop hits the loopback HTTP
  endpoint and returns ``io.BytesIO`` wrapping the response.
  Read-only (peers don't write CAWL images). Raises
  ``FileNotFoundError`` on 404 / no cached copy / fetch
  failure; raises ``ServerUnavailable`` on transport failure.
- All exposed via ``__all__`` and re-exported.

**What's left (not in 0.38.0):**

- ⏳ APK-bundled INDEX seed (independent piece; ~50 KB asset
  in ``server_apk/assets/cawl/index.json`` so install-day-no-
  network gets a populated index without GitHub access).
  Image binaries are NOT bundled — a 100–300 MB per-release
  payload is the wrong trade; lazy daemon caching covers the
  steady state. Filed in NOTES_TO_DAEMON.md.
- ⏳ Peer migration in the recorder (swap direct
  ``urllib.request.urlopen(raw_url)`` + per-peer cache for
  ``CAWLHandle.open_read()``; UI affordance to set
  ``cawl_image_repo`` per project). Lives in the recorder
  repo; not blocked on anything here.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bump. The 0.38.0 endpoints are net-new; pre-0.38 clients
calling them get 404, the wrappers return ``{}`` / ``None``,
peers continue to use their pre-migration paths. Older clients
talking to a 0.38 daemon work unchanged — they just don't call
the new endpoints. The 0.37.0 ``GET /v1/cawl/index`` endpoint
is removed; no peer has adopted it yet (0.37.0 didn't ship a
real release).

**Bootstrap decline cadence: one-shot, not permanent.**
``bootstrap()``'s self-update decline mechanism (the "Not now"
button on the peer self-update prompt and the
already-declined branch of the server-too-old install prompt)
is now **one-shot**. Recording a decline suppresses exactly
the next launch's prompt and clears, so the cadence is
prompt → decline → skip → prompt → decline → skip → … rather
than the previous "never ask again for this version" shape.
A new upstream version still invalidates the stored value
the same way (exact-string compare).

Motivation: permanent decline-by-version painted users into
a corner where reconsidering required waiting for the next
upstream tag. One-shot gives the user a launch's breathing
room without trapping them. Implementation: new
``_consume_decline(repo, version)`` does the read-then-clear
in one step; ``_declined_version`` stays as a non-destructive
peek for tests / diagnostic use.

### azt_collabd 0.37.0 / azt_collab_client 0.37.0 — daemon-owned CAWL image-URL index cache

Moves CAWL image-URL index ownership from the peer to the daemon
to close out the 60/hr GitHub rate-limit symptom reported in
NOTES_TO_DAEMON.md (filed 2026-05-11). The fundamental reframing:
the index is *suite-scoped* shared infrastructure, not
peer-scoped, so it belongs on the daemon and peers consume it.

**Why this is needed.** Pre-0.37, each peer hit
``api.github.com/repos/<image_repo>/git/trees/HEAD?recursive=1``
directly on every project load and cached the result in a
per-peer in-memory dict. GitHub caps unauthenticated REST at
60 requests / hour / IP, which a dev rebuild loop, CI run, or
multi-peer device blows trivially. Once exhausted, the
resolver returns empty for the rest of the session and entries
without a locally-cached image render with no illustration.
Three structural failure modes flow from peer ownership:

1. **Rate limit exhaustion** — described above.
2. **Per-peer duplication** — N peers on the same device each
   do the same work; Android's sandbox prevents sharing even
   the cache file.
3. **Install-day-no-network** — a fresh install with no
   connectivity has no way to populate the index, so
   first-launch UX is "no illustrations" regardless of what's
   on disk.

The recommendation discussion (transcript 2026-05-12) ranked
three hosting fixes (bundle in APK / proxy through daemon /
sign with GitHub App token) and then re-framed: the deeper
problem is *where the cache lives*. Moving ownership to the
daemon serves the same data, removes the per-peer fan-out,
and lets the time-bounded refresh policy do the rate-limit
work once per device per day.

**Daemon side (`azt_collabd/cawl.py`):**

- New module owns ``$AZT_HOME/cawl/index.json``.
  ``get_index(force_refresh=False)`` returns the index dict,
  refreshing from GitHub on cache miss / past-TTL / explicit
  force. TTL is 24h (``_INDEX_TTL_SECONDS``).
- Lock-coalesced fetch: two peers calling ``get_index`` on a
  cold cache result in exactly one network round-trip; the
  second caller reads the freshly-written file.
- Stale-cache fallback: a network failure with a cached copy
  on disk returns the cached copy (even if past TTL). A
  network failure with no cache returns ``{}``. Peer code
  treats ``{}`` the same way it treated its pre-migration
  empty resolver dict, so there's no new "daemon failed"
  branch to write.
- Daemon stays naming-convention-agnostic. The wire shape is
  ``{repo, branch='HEAD', fetched_at, files: [{path, url}]}``.
  Peers do the filename → CAWL-identifier mapping themselves
  (the recorder has its own convention; future peers may
  differ).

**Config (`azt_collabd/config.py`):**

- New ``cawl_image_repo`` config kwarg on ``configure()``,
  with ``AZT_CAWL_IMAGE_REPO`` env-var override. Empty
  default — peers must configure it before any fetch
  happens. An unconfigured daemon short-circuits
  ``get_index()`` to ``{}`` without any network call, so a
  misconfigured launch doesn't silently hammer the wrong
  GitHub repo.

**Wire shape:**

```
GET /v1/cawl/index
→ {
    "ok": true,
    "index": {
        "repo":       "<owner>/<repo>",
        "branch":     "HEAD",
        "fetched_at": <unix-seconds>,
        "files": [
            {"path": "cawl-1234.jpg",
             "url":  "https://raw.githubusercontent.com/.../cawl-1234.jpg"},
            ...
        ]
    }
  }
```

Empty dict at ``index`` (``{}``) means "no images known" —
same shape peers got from an empty pre-migration resolver,
so no new failure branch is required.

**Client side:**

- New ``cawl_index()`` wrapper in
  ``azt_collab_client/__init__.py``. Returns the dict on
  success, ``{}`` on transport failure or empty daemon
  response. No raw ``ServerUnavailable`` reaches the caller.
- Re-exported from ``__all__``.

**What this fixes vs. what's left.**

- ✅ Index fetch no longer per-peer. One daemon-side fetch
  per device per 24h TTL.
- ✅ Rate-limit blow-up under dev rebuild / multi-peer use.
- ✅ Stale-cache survival across GitHub outages.
- ⏳ Image *binaries* still fetched by peers directly from
  ``raw.githubusercontent.com``. That endpoint is on a much
  more permissive rate-limit domain (effectively unmetered
  for normal use), so it isn't the bottleneck. Migration to
  a daemon-served provider URI for binaries is Stage 2 — same
  shape (suite-scoped resource, daemon ownership, peer
  consumes via provider) but a larger touch (every peer's
  image-resolution path). Filed as the remaining piece in
  NOTES_TO_DAEMON.md after the 0.37.0 cut.
- ⏳ Install-day-with-no-network still has no bundled
  index seed. ~50 KB index JSON shipped as a server APK
  asset would close the gap; daemon copies into
  ``$AZT_HOME/cawl/`` on first start if empty. NOT
  bundling image binaries — 100–300 MB per release is
  the wrong shape for Android distribution; daemon-side
  lazy caching (Stage 2 RPC) is how the binary
  deduplication / cross-peer-sharing wins land.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
is bumped. The endpoint is purely additive — a pre-0.37 client
calling ``cawl_index()`` gets ``{}`` from a 0.37 daemon and is
fine; a 0.37 client calling ``cawl_index()`` against a pre-0.37
daemon hits a 404, the wrapper falls back to ``{}``, and the
peer continues to resolve illustrations from its own legacy
fetch path. Peers migrate at their own pace; no hard cut-over.

### azt_collabd 0.36.0 / azt_collab_client 0.36.0 — `atomic_commit` RPC for URI atomic writes, MIN_SERVER_VERSION lifted hard to 0.36.0

Closes the last cross-process atomic-write gap: peers writing
LIFT / audio / image bytes through a ``content://`` URI on
Android now ship the full payload to the daemon, which performs
the tempfile + ``os.replace`` atomic write in its own process.

**Why this is needed.** On Android the daemon's working_dir
lives in the standalone server APK's private filesDir. Peers
write to it via ``ContentResolver.openFileDescriptor``, which
returns an FD into the daemon's filesystem. ``ftruncate(fd, 0)``
+ subsequent writes through that FD are NOT atomic from any
other observer's perspective: a concurrent peer write, or the
daemon's own merge-output write, can see torn bytes mid-write.
The 2026-05-12 ``baf`` repro showed exactly this — two peer
serializations interleaved through the FD path produced
malformed XML which the daemon then misparsed catastrophically.

The 0.35.4 client added a path-keyed lock so a single peer
process can't race with itself, but cross-peer-process and
peer-vs-daemon races stayed open. 0.36.0 closes them.

**Wire shape:**

```
POST /v1/projects/<lang>/atomic_commit
{
  "path": "<rel_path>",
  "data_b64": "<base64-encoded-bytes>"
}
```

``rel_path`` is one of ``<file>.lift``, ``audio/<file>``,
``images/<file>`` — same whitelist as the ContentProvider's
``_resolve_path``. Path-traversal and out-of-whitelist shapes
return 400 before any filesystem touch.

The daemon serializes the write through ``project_lock`` (so it
can't overlap with a sync's merge-output write or another
atomic_commit) and writes via tempfile + ``os.replace`` in its
own process. The destination is always a complete copy of one
version, never a torn mix.

Response: ``{ok: True, result: {statuses: [{code: 'ATOMIC_COMMITTED',
params: {bytes_written, sha256}}]}}``. The sha256 lets the peer
verify the bytes that landed match what it sent.

**Client side:**

- New ``atomic_commit_bytes(langcode, rel_path, data) -> Result``
  wrapper in ``azt_collab_client/__init__.py``. Transport
  failures translate to ``SERVER_UNAVAILABLE`` / ``SERVER_ERROR``
  per the existing contract; the peer never sees a raw
  ``ServerUnavailable``.
- New ``_UriAtomicWriteFile`` in ``lift_io.py`` buffers writes
  in memory and ships them on commit. Memory cost: ~1.33× the
  file size (base64 encoding) during the encode-and-send window.
  For LIFT (tens of MB at worst) this is fine.
- ``LiftHandle.atomic_open_write`` on a URI now returns
  ``_UriAtomicWriteFile`` (the 0.35.4 fallback to plain
  ``open_write`` is gone). Filesystem-path callers still get
  the local ``_AtomicWriteFile`` (tempfile + ``os.replace`` in
  the peer process).
- ``MediaHandle`` inherits the same shape — audio and image
  atomic writes also go through the RPC on URI projects.

**Floor:** ``MIN_SERVER_VERSION 0.35.4 → 0.36.0`` (hard).
Peers rebuilt against 0.36.0 clients won't pair with pre-0.36.0
daemons, so the install/update prompt fires before any sync
attempt. Pre-0.36.0 clients are still allowed against a 0.36.0
daemon — they just don't get the atomic-URI-write benefit (the
old client doesn't know about the endpoint). MIN_CLIENT_VERSION
does NOT bump for that reason: the new endpoint is additive,
not breaking.

### azt_collabd 0.35.4 / azt_collab_client 0.35.4 — atomic LIFT writes from peers, forensic dumps on every guard trip, MIN_SERVER_VERSION lifted to 0.35.4

Two complementary pieces from the same investigation:

**Client side (`azt_collab_client/lift_io.py`):**

- `LiftHandle.open_write` is now **serialized within the peer
  process via a path-keyed reentrant lock**. Two threads of the
  same peer calling `open_write` on the same target queue
  rather than race. Pre-0.35.4 a rapid-succession `open_write`
  pattern (e.g., the recorder serializing the LIFT twice in
  close succession after two audio captures) could interleave
  at the byte level — each `open_write` opens an independent FD
  and `ftruncate(0)`s the file, so two writes from offset 0
  produce malformed bytes with torn tag boundaries. The
  `baf` 2026-05-12 repro shows two same-lang `<gloss>` elements
  with one's `<text>` mid-stream embedded in the other's; that's
  the signature.
- New `LiftHandle.atomic_open_write` context manager. Writes go
  to a sibling tempfile with a random suffix; on clean exit
  (`__exit__` with no exception), `os.replace` atomically renames
  the tempfile over the destination. On exception, tempfile is
  removed and the destination is untouched. Filesystem paths get
  true atomic semantics; content:// URIs fall back to the
  lock-protected `open_write` (ContentResolver has no clean
  atomic-rename for arbitrary Provider URIs). Two concurrent
  `atomic_open_write` calls on the same destination are safe:
  each writes its own random-suffixed tempfile, and whichever
  `os.replace` runs last wins — the destination is *always* a
  complete copy of one version, never torn.
- `MediaHandle` inherits both behaviors transparently
  (audio and image writes also benefit).

**Daemon side (`azt_collabd/lift_merge.py` + `repo.py`):**

- New `build_diagnostic_xml` / `diagnostic_filename` /
  `is_guard_kind` / `DIAGNOSTICS_SUBDIR` helpers in
  `lift_merge.py`. The diagnostic XML schema captures:
  - `guard` kind, daemon version, UTC timestamp.
  - `merge-context`: lift path + the three commit SHAs
    (local / remote / base) so the bytes are reachable via
    `git show` from any clone.
  - `process`: pid, ppid, executable, cwd.
  - `thread`: name + ident of the thread that hit the guard,
    plus the names + idents of every other live thread (so
    a concurrent-call hypothesis can be tested from the
    dump).
  - `caller-stack`: the `traceback.extract_stack` slice at
    guard-fire time, file + line + function for each frame.
  - `filesystem-state`: stat results for the working-tree
    LIFT path, .git directory, and `.azt-collab/diagnostics`
    so disk-full or permission anomalies are recoverable.
  - `inputs`: per side, byte length, sha256, parsed entry
    count, parse-error message (when parsing failed).
  - `merged`: byte length, sha256, entry count, parse-error
    (when applicable).
  - `conflict-fields`: the diagnostic strings the guard
    produced (e.g., the `_looks_truncated` or
    `_looks_catastrophic_output` message).
  - `recent-trace`: a slice of the in-process ring buffer
    (`_TRACE_RING_SIZE = 500` entries, default last 120 s)
    capturing the daemon's pre-guard activity. Every
    `[sync-trace]` / `[merge-trace]` / `[merge-diag]` line
    in `azt_collabd` now routes through `lift_merge.trace()`,
    which appends to the ring AND prints to stderr — so
    dumps carry the same time-precise log slice that logcat
    would have shown if anyone had been looking.
- `repo._merge_diverged` now dumps the diagnostic to
  `<working_dir>/.azt-collab/diagnostics/<utc>-<guard>-<nonce>.xml`
  whenever a guard fires on a .lift merge. The file gets staged
  into the merge commit by the existing `_stage_all` call and
  pushed to the remote alongside the safe merge result. A
  pre-existing `_write_merge_diagnostic` helper does the write
  via tempfile + `os.replace` so a half-written diagnostic can't
  be staged.

  Best-effort: a diagnostic-write failure logs to stderr and
  the merge proceeds. We don't want the audit trail to block
  the merge if e.g. the disk is full.

  **User isn't bothered.** The file lives under a hidden
  `.azt-collab/` directory and is mentioned only in
  `[merge-diag]` log lines. The intent is forensic — when a
  guard fires (rare; ideally never), the daemon team or a
  future-LLM analysis can `git log .azt-collab/diagnostics/`
  on any clone of the repo, find the dump, and reconstruct
  exactly what the merger saw. No console prompts, no UI
  surfacing.

**Versioning + floor:**

- `azt_collabd 0.35.3 → 0.35.4`.
- `azt_collab_client 0.35.3 → 0.35.4`.
- `MIN_SERVER_VERSION 0.35.3 → 0.35.4` hard. Pre-0.35.4 daemons
  still have all the guards (those landed in 0.35.1–0.35.3) but
  log guard trips to stderr only — Android logcat is ephemeral
  and not retrievable. The user explicitly asked that every
  guard firing be recoverable from the repo for post-hoc
  analysis; pinning the floor here is the discipline that
  enforces it.

**Why this is the right shape, in one sentence:** any future
guard firing automatically leaves a small structured XML file
in git that says exactly what the merger saw — without the
user having to do anything, and without polluting their LIFT
or audio data.

### azt_collabd 0.35.3 / azt_collab_client 0.35.3 — output-side catastrophic-loss guard, MIN_SERVER_VERSION lifted hard to 0.35.3

The closed merge note (filed 2026-05-11, "merge driver reorders
entries to guid order") was reopened on 2026-05-12 with new
commit-level evidence from the `baf` project: merge commit
`679c102` produced 1 entry from inputs of 1702 and 1700 entries
(base ~1700). The input-side truncation guard added in 0.35.1
**cannot have fired** for these inputs (both well above the
threshold). Yet the daemon's `lift_merge.three_way_merge`
committed a 1-entry merge result, annotated `azt-lift-conflict
value="theirs"` on the surviving entry, with the original guid.

**Bug-shape analysis** (recorded so the institutional
knowledge survives even if the proximate cause is never
narrowed):

The surviving entry's annotation form — single entry,
`value="theirs"`, original (non-`-theirs`-suffixed) guid — is
produced by exactly one pre-v3 code path: the `delete-modify`
branch, which fires when `ours_entries.get(guid) is None`
while `theirs_entries.get(guid)` is present (with content
differing from base). For 1699 of the 1700 entries to be
dropped through that branch's sibling code (`if _canon(b) ==
_canon(t): continue   # they didn't change it; we deleted`),
**the merger's internal `ours_entries` view had to be
near-empty at the moment of the merge**. Yet `git show
dc69264:baf.lift | grep -c '<entry '` shows 1702 entries in
the committed blob.

That contradiction — committed blob is full, the merger's view
was near-empty — points at a non-deterministic ordering
problem we can't reproduce without the daemon logs from the
exact minute (`Tue May 12 13:17:40 UTC 2026`). Plausible
proximate causes ranked by likelihood:

1. **Two `_merge_diverged` calls raced.** Concurrent syncs
   (the auto-sync on project select + a manual sync, or two
   peers' sync requests on the daemon, or any debounce race)
   each independently called `_walk_tree` on a working-tree
   snapshot. If one snapshot caught a mid-write LIFT (peer's
   `MediaHandle.open_write` had `ftruncate(0)`'d the file at
   `lift_io._open_content_uri` line 211 but the subsequent
   write hadn't completed), the merger saw a truncated ours
   and committed the destructive merge. The OTHER call (with
   the full file) then committed `dc69264` on top — making
   the committed blob look healthy in retrospect.
2. **Mid-write commit, immediately rewritten.** A peer's
   write to the LIFT was interrupted (process killed, OOM,
   activity teardown) right after `ftruncate(0)` and before
   the bulk write completed. The daemon's commit_audio_and_sync
   captured the truncated state, merged, and committed
   `679c102`. The peer restarted, finished writing, and
   committed `dc69264` later — making the "before" commit
   look healthy in retrospect.
3. **A path-level cache or staging issue** in
   `_merge_diverged` returning blob bytes that don't match
   what's now at `git show dc69264:baf.lift`. Less likely
   given dulwich reads commit→tree→blob deterministically,
   but not ruled out.

Without the daemon logs of that minute, we cannot prove
which (if any) of these was the proximate cause. **What we
CAN do is make the next occurrence harmless.**

**Most likely proximate cause** (refined after the user's
follow-up observation that the surviving entry shape —
single entry, `value="theirs"`, ORIGINAL guid — uniquely
identifies the **delete-modify** branch, not modify-modify
— which forces the conclusion that `ours_entries` was empty
at merge time):

`grep -c '<entry '` is a regex line count, not an XML
validator. If `dc69264:baf.lift` had 1702 `<entry ` text
matches AND a structural XML defect somewhere (unclosed tag,
embedded null byte, bad encoding sequence, anything ET
refuses), `git show` prints the raw bytes (succeeds — git
doesn't validate) but `ET.fromstring` raises `ParseError`.
The pre-0.35.2 `_parse` caught `ParseError` and **silently
returned an empty LIFT doc with no signal back to the
caller** — so the merger's `ours_entries` came back empty,
every guid hit the delete-modify branch (1699 dropped via
`continue   # they didn't change it; we deleted`, 1 emitted
as a theirs-annotated entry).

That fits the evidence exactly without invoking races or
staging mysteries. The pre-0.35.2 silent-ParseError-masking
was already addressed by 0.35.2's `_parse` returning
`(root, error_msg)` — the merger now refuses to commit when
ours/theirs fails to parse. The output-side guard in 0.35.3
is a complementary defense: catches the symptom whatever the
proximate cause, including ones we haven't thought of.

**Forensic trace added.** `three_way_merge` now logs
`[merge-trace] path=... base=N ours=M theirs=K
ours_err='...'` at the start of every invocation. Next
time anything looks weird, the logs themselves answer "did
`_parse` mask an error, or did the merger genuinely see N
entries?" without needing forensic git archaeology.

**Output-side `_looks_catastrophic_output` guard.** Refuses
to commit a merge whose entry count is < 1/4 of the smaller
healthy input. Skips small projects (base < 50 entries; the
ratio doesn't generalize at small scale) and skips when an
input was itself tiny relative to base (input-side guard had
jurisdiction; don't double-attribute). When triggered: keeps
the larger input intact verbatim, emits a single
`catastrophic-merge-output` Conflict carrying the full count
diagnostic. Defense-in-depth: catches the symptom regardless
of which proximate cause produced the algorithmic loss inside
the merger.

For the actual `baf` numbers (1, 1702, 1700, 1700): the guard
fires unambiguously. Even if a future bug produces some other
algorithmic loss, as long as the output is dramatically
smaller than the inputs, the guard catches it.

**Why this isn't redundant with the input guard.** The input
guard (`_looks_truncated`) checks the SHAPE of the inputs —
useful when one input arrived obviously truncated. The output
guard checks the SHAPE of the result — useful when both
inputs looked healthy at parse time but the algorithm lost
data internally. They're independent layers. The bug repro
above is the canonical case where input-side guard CANNOT
fire (both 1700-ish) but output-side guard MUST fire (1).

**`MIN_SERVER_VERSION` lifted 0.35.1 → 0.35.3 (hard).** The
proximate cause for the 0.35.1 collapse is undetermined and
could recur; forcing the floor ensures every peer paired with
a daemon has the output guard. Standard discipline matching
prior hard floors (0.34.0 sync, 0.34.1 reorder, 0.35.1
input-truncation).

**Tests** (`tests/test_lift_merge.py`):

- `test_full_sides_one_entry_differs_keeps_all_entries`:
  100-entry base/ours/theirs where only one entry differs.
  Output must contain 100 entries (with a field-level
  conflict on the differing one). Locks in the v3 recursive
  merge's correctness for this case, and via the output
  guard ensures even a regression in the recursive merge
  doesn't slip past.
- `test_catastrophic_output_guard_fires_directly`: direct
  unit tests of `_looks_catastrophic_output` covering: the
  bug numbers (trip), healthy output (skip), 50%-delete
  (skip), small project (skip), already-tiny-input (skip,
  input guard's territory).

**Wire-compat.** Additive: existing conflict kinds and
result shape unchanged. New `catastrophic-merge-output`
Conflict kind shows up only if the guard fires. Existing
peers see this as a generic CONFLICTS result; new peers can
distinguish via `Conflict.kind`.

### azt_collabd 0.35.2 — LIFT merge: recursive field-level conflict resolution + parse-error guard

The v1/v2 merge produced "two whole entries with synthetic
``-theirs`` guid suffix" on every modify-modify conflict — correct
but unresolvable in practice. A 1700-line entry conflict where the
only divergence is a `<text>` byte is invisible to the user; they
won't sit and diff two thousand lines to find the one that
differs. Field reports confirmed: nobody resolves these.

**v3 recursive merge.** Conflicts now express at the **narrowest
LIFT-multi level** that contains the divergence. A same-lang
``<text>`` conflict produces two same-lang ``<form>`` siblings each
carrying its own text and a single ``<annotation
name="azt-lift-conflict" value="ours|theirs"/>`` marker — one
``<entry>``, one ``<lexical-unit>``, two ``<form>``s. A
``<pronunciation>`` conflict duplicates at pronunciation level
(entry-level otherwise stays single). A gloss-text conflict
duplicates at the ``<gloss>`` level inside a single sense. Only
when a conflict genuinely can't be narrowed (entry-attribute
differences with no element-children divergence) does the
whole-entry duplication fallback kick in — with the synthetic
guid suffix kept for that rare case.

Implementation: ``_merge_pair`` + ``_walk_children`` recursive
helpers, plus a ``_MULTI`` policy table mapping
``(parent_tag, child_tag)`` → schema multiplicity. Unknown pairs
default to multi (safer to over-allow than under-allow). The
entry-level ``<annotation name="azt-lift-conflict" value="conflict">``
marker now carries a ``<trait name="azt-lift-conflict-fields"
value="...">`` listing slash-delimited paths from the entry root
to each conflict site (e.g.,
``lexical-unit/form[lang=en],sense[id=A]/gloss[lang=en]``) — peer-
side resolvers can jump to the conflicting sub-elements without
re-walking the merged tree.

**Parse-error guard.** ``_parse`` no longer masks
``ET.ParseError`` silently. When ``ours`` or ``theirs`` fails to
parse (mid-write truncation that breaks XML, etc.), the merge
refuses entirely — keeps the side that parsed cleanly, surfaces
a ``parse-error`` Conflict in the result. Pre-0.35.2 the silent
mask + the merge body's "absent from ours = ours deleted" rule
combined to produce catastrophically destructive merges when
the input was structurally invalid. Detection is now at the
input layer where the data still tells us what's wrong.

**Empty-side guard, small-project case.** The 0.35.1 truncation
guard only triggered on ≥50-entry projects (the ratio threshold
needs absolute size to avoid false-positives on legitimate small
edits). The empty-side case — ours has 0 entries while base has
any and theirs has any — now triggers regardless of project
size. Catches a 5-entry project where one peer's write got
``ftruncate(0)``'d mid-flight. False-positive only for users
who *intentionally* clear every entry, which doesn't happen in
this suite's peer flows.

**Wire-compat.** Same shape: emits LIFT bytes, peers read them
as normal LIFT. Old peers reading the v3 output see the
duplicated forms/glosses as normal LIFT content (forms ARE
schema-multi inside multitext containers; same-lang siblings are
schema-valid even if semantically "one per writing system"
conventionally) — no crashes, just a peer that doesn't recognise
the new annotation pattern. The ``conflict-fields`` trait value
changed from flat tag names to slash-delimited paths; peers
parsing it should treat the value as opaque text or a
comma-separated path list. No ``MIN_SERVER_VERSION`` bump —
peers don't actively consume the conflict format yet.

**Tests.** ``tests/test_lift_merge.py`` adds coverage for the
same-lang text case, pronunciation case, parse-error case,
empty-side small-project case, one-sided-change-no-conflict
clean path, and the entry-level marker's path-list trait.

### azt_collab_client 0.35.2 — peers may write image bytes through the provider (gate removed)

`MediaHandle(path_or_uri, kind='image').open_write()` no longer
raises `PermissionError`. 0.18.0 through 0.35.1 raised under an
"images are read-only from peers; the daemon owns image
additions" rule, which on inspection turned out to be an
**unsubstantiated policy** — every mention of it (`lift_io.py:149-170`,
`CLAUDE.md`'s cross-package-access section, the 0.18.0 CHANGELOG
entry) asserted the rule but none cited a driving concern.
The recorder team filed a NOTES_TO_DAEMON entry (2026-05-12)
showing that the rule made the entire in-app image-selection
feature silently no-op on URI projects, with four call sites
gated off (`_download_and_set`, `_copy_and_set`,
`_save_remote_image`, and the workers under it).

**Decision:** symmetry with audio. The daemon's provider
already supports image writes (`_resolve_path`'s
`_ALLOWED_MEDIA_DIRS = ('audio', 'images')` whitelist
auto-mkdirs the parent on first write). The two-write pattern
(image bytes through `MediaHandle`, illustration ref through
`LiftHandle`) is the same shape audio uses today and has
worked correctly in the field. Binary-conflict resolution on
basename collisions falls through to `repo._merge_diverged`'s
existing `non-lift-modify-modify` branch (merging-side wins
on disk, both versions remain in git history) — same handling
audio's `.wav` files get.

**Wire-compat:** purely client-side change. Daemon-side
provider already supports image writes; no daemon code changed.
Older peers paired with 0.35.2+ daemons keep their old
`PermissionError` gate (they bundle the old client) so no
behavior change there. Newer peers paired with older daemons:
the daemon's provider has always allowed image writes through
`_resolve_path`, so this works wire-side, but pre-0.35.1
daemons have the merge-truncation gap — the existing 0.35.1
`MIN_SERVER_VERSION` hard floor blocks that pairing anyway. No
floor bump for 0.35.2.

**Peer call-site cleanup** (recorder, viewer, future peers):
drop the `is_uri: return` gates that were routing around the
removed PermissionError. Use the same `MediaHandle` shape as
audio. No new endpoint, no wrapper, no recorder-side
infrastructure work beyond removing the gates.

### azt_collabd 0.35.1 / azt_collab_client 0.35.1 — LIFT merge: truncation guard + field-level conflict annotations; MIN_SERVER_VERSION lifted hard to 0.35.1

Field-reported 2026-05-12 (NOTES_TO_DAEMON.md, closed): a peer's
post-merge LIFT shrank from ~1700 entries to 1, leaving only the
single conflicting entry annotated `azt-lift-conflict="theirs"`.
The reporter hypothesized a sibling bug to the closed reorder-by-guid
issue ("union computed wrong"); the current code actually walks
`union(ours, theirs)` correctly, so that hypothesis didn't fit. The
real shape: `ours` arrived at the merge with a near-empty entry
list while `base` and `theirs` had the full template. Every base
entry absent from `ours` and unchanged in `theirs` then took the
"they didn't change it; we deleted it" branch — correctly, given
the inputs — producing the 1-entry destructive merge. The merge
algorithm wasn't lying; the **inputs** were corrupted upstream
(peer-side write race, partial commit, or sandbox sync hiccup
between recorder and daemon — not narrowed in this session).

**Defensive guard (`_looks_truncated`).** When all three sides
have non-trivial entry counts AND one side's count is less than
1/50 of the other AND the larger side has ≥50 entries, refuse
the destructive merge. Keep the larger side intact (unchanged
bytes; whatever was in the merge commit before this fix would
have been destructive, so we bias toward preserving data), and
return a single `Conflict(kind='truncation-suspected', fields=[…])`
in the result. Upstream callers see `S.CONFLICTS` and surface
the diagnostic; nothing destructive lands in git. Thresholds
are intentionally conservative — legitimate large-scale
deletions still go through (you can delete up to 98% of a
project in one commit without tripping the guard).

**Field-level conflict info.** Per-entry conflicts (modify-modify,
add-add) now annotate the `<annotation name="azt-lift-conflict">`
element with a `<trait name="azt-lift-conflict-fields" value="…">`
sub-element listing the LIFT child-element keys that actually
diverged — `lexical-unit`, `citation`, `field[type=SILCAWL]`,
`sense[id=…]`, `pronunciation`, etc. The `Conflict` dataclass
gains a `fields: list[str]` parallel field, surfaced via
`to_dict()` for any peer-side merge UI. Lets a recorder
(re-recording audio for a sense) ignore conflicts that don't
touch `pronunciation`; lets a viewer (or future merge resolver)
focus the user on the specific sub-elements that need attention
instead of asking them to diff the whole entry by eye.

Modify-delete / delete-modify conflicts don't carry field info —
the conflict there is "entry exists vs doesn't," not
sub-element divergence.

**Wire-compat:** additive. Older peers see the new trait as
unknown LIFT content (which their LIFT readers tolerate by
design — annotations are extensible) and the new
`truncation-suspected` Conflict kind as just another conflict
they surface generically.

**`MIN_SERVER_VERSION` raised 0.35.0 → 0.35.1 (hard).** Per the
reporter's ask #5: pre-0.35.1 daemons have no truncation guard,
so a peer paired with one can still hit the destructive merge.
The floor bump prevents that pairing — sync refuses with
`server_too_old` until the user updates the server APK. Same
discipline as the 0.34.1 reorder fix.

### azt_collabd 0.35.0 / azt_collab_client 0.35.0 — surface broken GitHub refresh-token state with a deadline-aware toast; codify auto/user sync contract

Field-observed in this session's first sync trace: the daemon's
``get_valid_github_token`` had been silently swallowing
``incorrect_client_credentials`` from the OAuth refresh endpoint
("Return the old token — it might still work"). That's a humane
fallback in the short term, but it converts an 8-hour countdown
into a silent cliff: once the existing access token expires, every
authenticated git op starts failing with no user-visible warning
that the user needs to re-auth.

**Daemon side.** ``azt_collabd/store.py``:
``get_valid_github_token`` now records ``refresh_broken=True`` +
the error string + the check timestamp on refresh failure, and
clears the flag on a subsequent successful refresh.
``set_github_tokens`` (called by the device-flow completion path)
also clears the flag — fresh tokens supersede any prior
refresh-failure state. ``get_status`` exposes
``github.refresh_broken`` and ``github.access_token_expires_at``
(unix timestamp = ``token_time + 8h``) so peers can read the
state via the existing credentials-status RPC without polling a
new endpoint. New helper ``github_refresh_state()`` returns the
same fields for daemon-internal use.

**New status code:** ``S.AUTH_REFRESH_STALE`` (mirrored in
``azt_collab_client/status.py``). Carries
``params['expires_at']``. Appended to every sync result —
``_h_project_sync`` and ``scheduler._run_sync`` both call a
shared ``server._annotate_with_auth_health(res)`` after running
the sync, so the status piggybacks on whatever the underlying op
returned (typically ``PUSHED + AUTH_REFRESH_STALE`` during the
access-token's last hour of life).

**Client side.** ``azt_collab_client/translate.py`` adds a
handler for ``S.AUTH_REFRESH_STALE`` that renders
"GitHub session needs re-authentication — current access
expires {deadline}. Open GitHub Connect and tap Re-authenticate."
``_format_deadline`` converts ``expires_at`` to a relative
phrase ("in 47 minutes", "in 3 hours", "now (already expired)")
so the user reads how much runway they have without dragging
timezone / locale plumbing into a one-shot string. The
"refresh-broken" state is also visible to peers via
``get_credentials_status() → github.refresh_broken`` for
peers that want a startup banner.

**Peer contract.** Documented in
``azt_collab_client/CLAUDE.md`` § "Peer contract: routing on
sync results" — auto-sync silences this code (per the existing
auto/user contract; we don't disrupt mid-flow); user-initiated
sync surfaces ``translate_status(status)`` as a toast. No
routing — the toast text already names GitHub Connect /
Re-authenticate as the next step. The state clears when the
user completes a fresh device flow.

**Wire compatibility.** Purely additive at the wire layer:
older peers paired with a 0.35.0 daemon see the new code as
an unknown status (verbose-but-non-fatal translate fallback);
older daemons paired with a 0.35.0 peer never emit the code,
so the peer never branches on it.

**``MIN_SERVER_VERSION`` raised 0.34.1 → 0.35.0** anyway, as a
*soft* requirement (no wire incompatibility to enforce). The
real reason: the peer contract changes in CLAUDE.md (auto-sync
silencing config-class codes, user-initiated routing /
toasting, deadline-aware ``AUTH_REFRESH_STALE`` handling) need
peer rebuilds to take effect. Bumping the floor forces every
peer paired with a 0.35.0+ daemon to rebuild against the
0.35.0 client, where the contract is documented and the
``AUTH_REFRESH_STALE`` translation is wired. Without the bump,
peers can keep running their pre-0.35.0 client and silently
disrupt project flows on auto-sync — the exact symptom that
surfaced as the "selected B got A" picker complaint earlier
in the 0.34.x development cycle.

### azt_collabd 0.34.1 / azt_collab_client 0.34.1 — LIFT merge preserves document order, MIN_SERVER_VERSION lifted to 0.34.1

Field-reported by the recorder team (NOTES_TO_DAEMON.md, 2026-05-11):
the very first real merge on any project rewrites the LIFT file
into guid-alphabetical order, irreversibly destroying the project's
semantic document order (template-driven SILCAWL order for new
projects; whatever the contributor established otherwise). The
change is committed and pushed before any peer can observe it, and
ElementTree round-trips preserve whatever order they parse, so all
subsequent edits cement the scrambled order. Repro confirmed
against `kent-rasmussen/sw-US-x-kent`: the merge commit `29d1266`
puts the entry whose guid sorts first (`002b6d2c-…` → SILCAWL
1572) at the top of the file, with every entry following in strict
guid order.

**Root cause.** `azt_collabd/lift_merge.py:three_way_merge`
walked `sorted(all_guids)` and appended to `merged_root` in that
order. Deterministic, yes — but the wrong determinism.

**Fix.** Walk `ours` in document order, then theirs-only entries
in theirs's document order. Anchoring on `ours` is the
conventional "the merging side keeps the order it was already
working against" pick, and it makes merge commits diffable: only
actually-changed entries move, instead of the whole 1700-entry
file appearing to be rewritten. Base-only guids (deleted on both
sides) are naturally excluded — they were a `continue` no-op in
the old loop body anyway. Same body, new traversal.

**MIN_SERVER_VERSION raised 0.34.0 → 0.34.1.** Pre-0.34.1 daemons
will commit and push a scrambled file on the next merge, with no
peer-visible warning before the damage hits git history. Hard
gate is preferable to silent fallback. Peers paired with a 0.34.0
daemon get the standard `server_too_old` bootstrap prompt.

**Repair for already-scrambled projects is deliberately manual**,
not automated, and unlikely ever to be. The natural order is
application-meaningful (SILCAWL row for template-derived projects;
headword for free-form lexica; sometimes a contributor's
deliberate manual sequence). A unilateral "re-sort everyone's
LIFT" utility can't know which of those applies, and silently
re-ordering a contributor's intentional sequence is the same
class of damage as the original bug. Project owners who want to
restore a known template order on a scrambled project do it by
hand, as one explicit commit, with explicit understanding of
what they're choosing to lose.

### azt_collabd 0.34.0 / azt_collab_client 0.34.0 — sync correctness: three load-bearing fixes, MIN_SERVER_VERSION lifted to 0.34.0

Two-device sync between Android peers was silently broken across the
entire 0.33.x line: after the first race between two phones pushing,
the daemon entered a state where every subsequent sync attempt
acted on a phantom remote, produced malformed merge commits, lost
the same race three times, and finally surfaced `PUSH_FAILED` or
(worse) a misleading `REPO_NOT_AUTHORIZED` against an unrelated
GitHub install. Three independent bugs were stacked; fixing one only
exposed the next. They're shipped together as a single minor bump.

`azt_collab_client.MIN_SERVER_VERSION` is raised from `0.31.0` to
`0.34.0`. A peer that gets through bootstrap will refuse to talk to
any older daemon — the user is forced to install/update the server
APK before sync re-engages. This is intentional: pre-0.34 daemons
*appear* to work and quietly corrupt local repo state by accumulating
malformed merge commits and never advancing
`refs/remotes/origin/<branch>`, so silent fallback is worse than the
hard gate.

**(1) `_merge_diverged` now produces real two-parent merge commits.**
The pre-0.34 code called `porcelain.commit(repo, merge_heads=[remote_sha], ...)`,
but dulwich 1.2.1's `porcelain.commit` doesn't expose `merge_heads`
as a public kwarg (it's an internal-only path used by `amend=True`).
The call raised `TypeError`, fell into a legacy graft-the-parent-
after-the-fact fallback (commit without merge_heads, then mutate
`commit.parents` post-hoc + re-add to the object store), which
silently produced a commit whose stored parents were `[local_sha]`
only. GitHub's `git-receive-pack` correctly rejected the push as
`DivergedBranches` because the "merge" commit didn't actually
contain `remote_sha` as an ancestor. Fix: drop down to
`repo.get_worktree().commit(merge_heads=[remote_sha])` —
the worktree-level API DOES accept `merge_heads` and sets
`c.parents = [old_head, *merge_heads]` atomically before writing
the object and advancing the ref. The graft fallback is removed;
the worktree API is in dulwich's public surface since 1.0.

**(2) HTTP 403 detection no longer false-positives on hex SHAs.**
The pre-0.34 code checked `'403' in str(exc)` to decide whether a
dulwich push exception was a real auth failure. dulwich's
`DivergedBranches.__str__` expands to `"(b'<current_sha>', b'<new_sha>')"`
— two 40-char hex SHAs. Random hex contains the trigraph `'403'`
~1 push in 250 by chance; the field trace had
`e41db428f68e9f7f6334`**`037`**`345d6450...`, which matched. The
false positive routed a diverged-branch failure through
`diagnose_403`, exiting the sync flow with a bogus
`REPO_NOT_AUTHORIZED` before the retry/merge could run. Fix:
`re.search(r'\b403\b', str(exc))` via a new `_is_http_403` helper
applied to all four call sites. Word boundaries don't fire inside
an all-word-char hex SHA but do fire in dulwich's
`"unexpected http resp 403 for <url>"` `GitProtocolError` message.

**(2b) `diagnose_403` now scopes by repo owner.**
When a real 403 *does* happen, `diagnose_403` was calling
`check_app_installed(token)` without `account_login`, so it grabbed
the first install in `/user/installations` whose `app_slug` matched.
A user who's a collaborator on five orgs that each installed
azt-collaboration got the first listed (`MattGyverLee` in the field
trace, install id 121228993, `selected` repos) instead of the
repo's own owner (`kent-rasmussen`, install 130605088, `all` repos),
and the follow-on `check_repo_in_installation` correctly answered
"no" — surfacing `REPO_NOT_AUTHORIZED` for a repo the user actually
has access to via their personal install. Fix: parse the repo
owner from `remote_url` first and pass it as `account_login` so
the install inspected is the one that should host the repo.

**(3) `porcelain.fetch` / `porcelain.pull` are called with the
remote NAME, not the URL.** Dulwich's `porcelain/__init__.py:fetch`
only runs `_import_remote_refs` (which writes
`refs/remotes/<name>/<branch>`) when `get_remote_repo` could resolve
the first positional arg back to a configured `[remote "<name>"]`
section. Passing a URL always misses (no section is named
`https://...`), so `remote_name = None` and the gate at line 4550
skips the ref import. The pack transferred successfully (HTTP 200
in logs) but the local tracking ref stayed frozen at whatever
`porcelain.clone` wrote at project-create time. Every subsequent
sync read a stale `remote_sha` from `refs/remotes/origin/<branch>`
and acted on a phantom state of the world. Field trace: actual
remote tip moved to `76201a5d…` ~25 minutes before the user opened
the recorder, but the daemon kept reading
`new_remote=42535766…` (the clone-time SHA) on every retry fetch,
merged against the phantom, and lost the push race three times in
a row. Fix: pass `'origin'` to every `porcelain.fetch` / `pull`
call site in `azt_collabd/repo.py`. Dulwich resolves `'origin'` via
`[remote "origin"]` (which `_init_repo_locked` /
`_clone_repo_locked` always populate), uses the `username` /
`password` kwargs we still pass explicitly, and — critically — runs
`_import_remote_refs` so the tracking ref advances on each fetch.
Push paths are unchanged: they already advance the tracking ref
manually via `repo.refs[remote_ref] = local_sha` after a successful
push (the line landed earlier for the `(+N)`-counter regression).

**Observability.** The retry-path fetch + merge in
`_sync_repo_locked` was wrapped in a bare `except Exception: pass`
— failures inside `_merge_diverged` were swallowed. Added
`[sync-trace]` lines for the retry fetch SHA, the retry merge SHA,
and any exception so future divergence loops are diagnosable from
logcat alone. (Reading the field trace was load-bearing for
isolating bugs 2 and 3.)

**(4) Auto-update download URL now matches the actually-published
asset name — and tolerates peer literals that drift from it.** The
recorder's bootstrap was wired with
`peer_asset_filename='azt_recorder.apk'` (Python-pkg underscore
form), but the published GitHub asset for `kent-rasmussen/azt-recorder
v1.39.0` is `aztrecorder.apk` (Android-package-segment form, no
underscore — matches `buildozer.spec → package.name`). The
`releases/latest/download/<wrong-name>` redirect 404'd; users got
"Download failed: HTTP Error 404" on tapping Update. The convention
itself is consistent (published name = `package.name`); only two
call sites typed the wrong form.

Two-layer fix in the client so peer-side typos can't break this
again:

- `default_asset_filename()` helper in `azt_collab_client.ui.update`
  derives ``<activity.getPackageName().rsplit('.', 1)[-1]>.apk`` from
  the running peer at runtime. `asset_filename` /
  `peer_asset_filename` in `check_for_update` /
  `install_apk_from_url` / `bootstrap` / `share_running_apk` are
  now keyword-optional and default to that helper. The recorder's
  call sites drop their explicit literals; new peers don't need
  to pass the name at all.
- **Resilient fallback** for peers that DO pass an explicit name
  (forks, older peer code rebuilt against the new client without
  having dropped the literal): the asset-lookup paths in
  `check_for_update` and `install_apk_from_url` now retry with
  the runtime-derived name when the explicit name isn't found in
  the release. `install_apk_from_url` further sources the download
  URL from the release JSON's `browser_download_url` rather than
  the caller-baked `releases/latest/download/<name>` URL, so a
  wrong peer-baked literal can't poison the actual download.
  Logged via `[update] explicit asset … not in release; falling
  back to derived …` so the drift is visible in logcat.
- Forks publishing under a non-default scheme still pass
  `asset_filename=` and get exactly that — fallback only fires
  when the explicit name returns no match.

### azt_collab_client 0.33.7 — docs/ cleanup: prune shipped plans, organise residual work
- **``docs/daemon_boot_plan.md``** rewritten as status-first.
  Phase A and Phase B2 marked SHIPPED with measured outcomes;
  Phase B1 + Phase C trimmed to "not shipped / not worth
  shipping unless …" notes with the trigger conditions
  spelled out. Cost-model speculation replaced with measured
  numbers from R500-class slow tablet (2026-05-09 harness
  run).
- **``docs/github_connect_ux_audit.md``** —
  recommended-implementation-order list at the bottom
  refreshed: items #1–#7 are done/declined (audit-trail
  strikethroughs preserved per the doc's own rule); items
  #8–#13 re-prioritised in current order. No content removed.
- **``docs/p4a_hook_picker_intent.md``** reduced to a redirect
  stub. The PICK_PROJECT intent-filter injection it described
  shipped in v0.28.x and is now in
  ``p4a_hook.py:_inject_pick_project``.
- **``docs/STATUS.md``** added. One-page index of "what
  shipped recently" + "what's open, prioritised" across all
  the docs in this directory. Reference docs
  (``research_notes_2026-05.md``, ``test_plan.md``,
  ``CLIENT_INTEGRATION.md``) listed but not duplicated.

### azt_collab_client 0.33.6 — measurement-driven decisions documented (Q2 + Q3 answered)
- **Q2 (doze) ANSWERED.** Measured on R500 tablet
  post-Phase-B2: doze runs (peer wait 49-68ms, daemon boot
  600-770ms) statistically indistinguishable from baseline
  (45-66ms / 593-1131ms). The Android-15 issue was the
  freezer, not doze proper. Phase B2's
  ``BIND_ABOVE_CLIENT`` is sufficient; no
  foreground-service-with-type variant needed.
- **Q3 (prewarm) ANSWERED.** Daemon Python boot ~600ms
  steady-state, ~1.1s first-cold-of-session. Prewarm
  overlap window ~1.9s. With prewarm, daemon boot fits
  entirely inside Kivy init; peer wait is **~50–60ms**.
  Without prewarm, peer wait would be the full
  ~600–1000ms.
- ``CLIENT_INTEGRATION.md`` § 3 reframed: prewarm in
  ``App.build()`` is now **required** for every peer (was
  "optional, measure first"). Cost of always calling it is
  essentially zero on devices where it doesn't help, and
  it's a 10× UX improvement on slow tablets.
- ``docs/daemon_boot_plan.md`` Q2 + Q3 marked answered with
  the measured numbers; remaining content kept for context.

### azt_collabd 0.33.0 + azt_collab_client 0.33.5 — bindService alone bootstraps Python (Service.onCreate self-delivers onStartCommand)
- **0.33.4 wasn't enough.** The connector's peer-side
  ``startService`` ALSO threw
  ``BackgroundServiceStartNotAllowedException``: cold-start
  ``App.build()`` fires before the peer's UID has been
  promoted to foreground (logcat shows
  ``UidRecord{...CEM bg:+50ms}``). Android 12+ blocks even
  cross-package starts from that state.
- **Real fix: server APK side.**
  ``AZTServiceProviderhost.onCreate`` now overrides Service
  lifecycle to self-deliver ``onStartCommand`` with
  ``getDefaultIntent(this, "")``. PythonService's normal start
  path runs on every Service creation, including from
  ``bindService`` with ``BIND_AUTO_CREATE``. So peers can
  start the daemon with ``bindService`` alone — which
  Android allows from background contexts. No more
  cross-package ``startService`` needed at all.
- **Connector simplified.**
  ``AZTServiceConnector.ensureBound`` no longer constructs
  Intent extras or calls ``startService``. Just one
  ``bindService`` with ``BIND_AUTO_CREATE | BIND_ABOVE_CLIENT``.
  All the Python-startup logic lives in the server APK's
  Service ``onCreate`` override.
- **Both APKs need rebuild + reinstall.** Server APK gets the
  new ``onCreate``; peer gets the simplified connector. Server
  APK bumped to 0.33.0 (lock-step with this change set).
- **Backward compat.** Legacy callers that still call
  ``startService`` (e.g., ``AZTCollabProvider.onCreate``'s
  fallback start, foreground caller paths) hit
  ``PythonService.onStartCommand`` which short-circuits when
  ``mService != null`` — Python only starts once regardless of
  start-path mix.

### azt_collab_client 0.33.4 — connector also startService (Android 12+ background-start fix)
- **Smoking gun on R500.** Logcat showed
  ``W ActivityManager: Background start not allowed: service
  Intent { cmp=...AZTServiceProviderhost }`` followed by
  ``E AZTCollabProvider:
  BackgroundServiceStartNotAllowedException`` thrown from
  ``AZTServiceProviderhost.start`` invoked from
  ``AZTCollabProvider.onCreate``. Android 12+ blocks
  ``startService`` from background contexts; the server APK's
  ``:provider`` lazy-spawn is a background context, so the
  Provider's ``onCreate`` self-start has been silently failing
  on every cold call. Python never started → no
  ``[boot-trace-daemon]`` lines, every peer compat probe got
  ``daemon_not_ready``, every B2 test was running against a
  daemon that had never finished init.
- **Fix.** ``AZTServiceConnector.ensureBound`` now does a two-
  step startup from the peer's *foreground* context (which
  Android allows): (1) ``startService`` with the full
  PythonService Intent extras (``serviceEntrypoint=service.py``
  etc., mirrored from
  ``AZTServiceProviderhost.getDefaultIntent`` on the server APK
  side; ``createPackageContext`` resolves the server APK's
  ``filesDir`` so ``androidPrivate`` / ``pythonHome`` /
  ``pythonPath`` aim at the right tree); (2) ``bindService``
  for OOM priority + freezer mitigation as before. The
  Provider's onCreate self-start stays in place as a no-op
  fallback for non-peer callers but is no longer load-bearing.
- **Why ``Provider.onCreate``'s start fails but the connector's
  start succeeds.** The Provider's runs in the server APK's
  own background process; the connector's runs in the peer's
  foreground process. Android 12+ allows the latter.

### azt_collab_client 0.33.3 — prewarm now binds on the main thread (worker JNI classloader scope fix)
- **Symptom.** R500 logcat showed
  ``[android_cp] AZTServiceConnector.ensureBound failed:
  ClassNotFoundException`` from the prewarm worker even after
  the connector ``.java`` was confirmed compiled into the APK
  (verified by ``unzip -p classes.dex | grep AZTServiceConnector``).
  The bind eventually succeeded — but only later, from the main
  thread via some other ``rpc.call`` path. So prewarm wasn't
  actually doing its B2 job: by the time the bind landed, the
  daemon's ``:provider`` process had already started cold-spawn
  with no priority hint.
- **Cause.** ``threading.Thread`` workers on Android attach to
  the JVM with the system bootclassloader, not the app
  classloader; pyjnius's ``autoclass`` calls from those workers
  can't find app-defined classes like our connector.
- **Fix.** ``prewarm()`` now does the autoclass + ``ensureBound``
  call synchronously on the caller's thread (typically the
  peer's ``App.build()`` main-thread context) BEFORE spawning
  the worker that does ``check_server_compat``. The bind is
  active at the earliest possible cold-start moment instead of
  racing the daemon's lazy-spawn. Worker still calls
  ``check_server_compat`` so the daemon-warmup retry loop has
  something to do; its (still failing) autoclass in
  ``discover()`` is a logged no-op since the bind is already
  in place.

### azt_collab_client 0.33.2 — point peer's android.add_src at canonical path, not the symlink
- 0.33.1 said ``android.add_src = android/src/main/java`` (via
  the peer's ``android/`` symlink). User report: still
  ``ClassNotFoundException`` on rebuild. Diagnosis: buildozer's
  ``android.add_src`` does not reliably follow symlinks; the
  copy/merge step lands an empty tree (or a broken symlink) in
  the dist's ``src/main/java/``.
- ``CLIENT_INTEGRATION.md`` § 2 now mirrors the server APK's
  approach: ``android.add_src = ../azt-collab/android/src/main/java``
  — pointing at the canonical filesystem path directly,
  bypassing the symlink. Brute-force fallback (copy the file
  into the peer repo) documented but flagged as brittle.

### azt_collab_client 0.33.1 — document peer's android.add_src requirement for B2
- 0.33.0 framing said "no peer code change required" — true for
  Python source, but peers DO need a one-line ``buildozer.spec``
  addition (``android.add_src = android/src/main/java``) for the
  new Java connector to compile into their APK. Otherwise:
  ``[android_cp] AZTServiceConnector.ensureBound failed:
  ClassNotFoundException`` on every cold start, bind never
  happens, freezer mitigation degrades to pre-B2.
- ``CLIENT_INTEGRATION.md`` § 2 now lists the line as required;
  § 3's "Automatic since 0.33.0" subsection points at the spec
  line when troubleshooting the ClassNotFoundException symptom.
- Server APK already had the equivalent line
  (``android.add_src = ../android/src/main/java`` in
  ``server_apk/buildozer.spec.tmpl``); peers historically didn't
  need one because the suite's Java tree was server-internal.
  B2 makes it shared.

### azt_collab_client 0.33.0 — Phase B2: peer holds bindService for OOM priority
- **Symptom this fixes.** R500-class Android-15 tablets showed
  endless ``daemon_not_ready`` 503s on cold start: peers
  triggered ``:provider`` lazy-spawn fine (Java provider
  responded), but Python's ``install_callbacks()`` never
  completed because the OS's app freezer suspended the cached
  ``:provider`` process mid-init. User-visible "AZT
  Collaboration not responding" + the manual "Open AZT
  Collaboration" workaround that papered over it.
- **Fix.** New peer-side Java connector
  ``android/src/main/java/org/atoznback/aztcollab/
  AZTServiceConnector.java`` issues ``bindService`` against
  the server APK's existing ``AZTServiceProviderhost`` with
  ``BIND_AUTO_CREATE | BIND_ABOVE_CLIENT``. Inheriting the
  peer's foreground priority defeats the freezer; the
  ``:provider`` process stays alive and Python finishes init.
  Bonus: ``:provider`` stays warm across the peer's session,
  so 2nd / 3rd / Nth RPCs in the same session don't re-pay
  daemon-cold-start.
- **Wire-up.** ``azt_collab_client/transports/android_cp.py``'s
  ``discover()`` calls ``Connector.ensureBound(activity)`` after
  the canonical ping succeeds. Idempotent; the connector is a
  static singleton. Async — we don't wait for
  ``onServiceConnected``; the existing compat-probe retry loop
  handles the bind-vs-Python-init race naturally.
- **No peer code change required.** Every peer that imports
  the client gets the new behaviour by virtue of bumping the
  bundled client. ``CLIENT_INTEGRATION.md`` § 3 documents the
  automatic behaviour + the diagnostic surface
  (``AZTServiceConnector.isBound()`` and ``dumpsys activity
  processes`` priority bucket).
- **No server APK change required.** ``AZTServiceProviderhost.
  onBind`` was already returning a stub ``Binder`` and tracking
  ``sBoundCount`` from the original sticky-bound design;
  peers were just never binding. Server APK can stay at 0.32.1.
- **Plan-doc** (``docs/daemon_boot_plan.md``) updated to mark
  B2 shipped + record the verification commands.

### azt_collab_client 0.32.2 — document prewarm + boot-trace harness in CLIENT_INTEGRATION.md
- New § 3 sub-section "Optional: pre-warm in ``App.build()``"
  documenting the ``prewarm()`` hook, its tradeoff, and the
  sentinel / env-var toggles for measurement runs. Peers
  considering cold-start tuning find the integration shape
  here rather than having to read ``bootstrap.py`` source.
- New § 13 sub-section "Boot-trace instrumentation" listing
  the peer + daemon phase labels and warning maintainers not
  to filter ``[boot-trace-*]`` lines out of their logcat
  pipelines.
- New § 13 sub-section "Cold-start measurement harness"
  pointing at ``tests/integration/measure_boot.sh`` +
  ``tests/integration/README.md`` and giving a default
  threshold (`peer wait` > 5 s on the slow-tablet target) for
  when to wire ``prewarm()`` before tagging a peer release.
- Also: ``measure_boot.sh`` switched from ``monkey`` to
  ``am start -W`` for launching the peer (with monkey as a
  fallback). monkey reports nonzero exits in cases that are
  actually fine — its exit code reflects internal event
  counts, not just dispatch success — and the script's
  ``set -e`` was killing the run after iteration 1.
  ``am force-stop`` and ``adb logcat -c`` now also tolerate
  nonzero exits.

### azt_collabd 0.32.1 + azt_collab_client 0.32.1 — boot-trace instrumentation + prewarm hook + measurement harness
- **Daemon-side ``_boot_trace``** in ``server_apk/service.py``
  emits ``[boot-trace-daemon] phase=<label> t=<elapsed>`` at
  every cost-center: ``module_loaded``, ``main_entered``,
  ``before_import_azt_collabd`` /
  ``after_import_azt_collabd``, ``configured``,
  ``before_install_callbacks`` / ``after_install_callbacks``,
  ``before_reconcile`` / ``after_reconcile``,
  ``entering_idle_loop``. Cheap; safe to leave on (≈ 10 lines
  per cold start).
- **Peer-side ``_boot_trace``** in
  ``azt_collab_client/ui/bootstrap.py`` mirrors with
  ``[boot-trace-peer] phase=<label>``: ``bootstrap_called``,
  ``compat_probe attempt=N``, ``compat_ok``,
  ``bootstrap_done``, plus prewarm phases.
- **New ``azt_collab_client.ui.bootstrap.prewarm()`` hook**:
  peers call it from ``App.build()`` to fire a single
  ``check_server_compat`` on a background thread, overlapping
  daemon lazy-spawn with Kivy initialisation. Idempotent;
  no-op on non-Android. Toggleable for measurement via
  ``$AZT_HOME/_no_prewarm`` sentinel or ``AZT_BOOT_PREWARM=0``
  env var so the harness can compare scenarios on the same
  APK without rebuilding.
- **Measurement harness** at
  ``tests/integration/measure_boot.sh`` drives a real device
  through ``baseline``, ``doze``, ``prewarm``, and
  ``doze+prewarm`` scenarios, capturing logcat boot-trace
  lines and producing per-iteration summaries via
  ``tests/integration/parse_boot_traces.py``. Doze is forced
  via ``dumpsys deviceidle force-idle``; prewarm toggling via
  the sentinel file (peer must be debuggable for ``run-as``).
  README at ``tests/integration/README.md`` documents
  prerequisites + scenario semantics.
- **Plan-doc updated** (``docs/daemon_boot_plan.md``):
  open-questions Q2 (doze) and Q3 (prewarm) now have explicit
  measurement plans pointing at the harness; Q1 (loopback
  ``kind``) remains as a deferred Phase A loose end.

### azt_collab_client 0.32.0 — daemon-warmup Phase A: adaptive backoff, diagnostic surface, fail-fast on null bundle
- **Adaptive backoff** in ``bootstrap._check_server``'s warmup
  retry loop. Replaces the fixed 2s interval with a schedule
  that ramps short → long: 0.2s, 0.4s, 0.8s, 1.6s, then plateaus
  at 2.0s. Fast devices that have a daemon ready by attempt 2
  now land in <1s instead of paying 2s+; slow devices keep the
  same ~60s total budget.
- **Diagnostic surface in the connecting popup.** New detail
  line under the "Connecting to AZT Collaboration service…"
  header shows ``Attempt N of 30  ·  Xs elapsed  ·  <kind>``
  where ``<kind>`` is the transport's coarse failure category
  (``daemon_not_ready`` while Python boots, ``null_bundle`` on
  signature-grant denial, etc.). Updates each retry. The
  unresponsive popup also surfaces last-error kind, total wait,
  and ``PackageManager``-reported server APK versionName, so the
  user / maintainer-email loop has actionable detail without
  needing adb access.
- **Fail-fast on ``null_bundle``.** Previously every
  ``ServerUnavailable`` was retried for the full 60s budget,
  including ``ContentResolver.call`` returning ``null`` — which
  is structurally unrecoverable (signature mismatch, provider
  authority missing). After 3 consecutive ``null_bundle``
  responses (≈0.6s on the new schedule) we jump to the
  unresponsive popup so the user can act on the real problem.
  ``daemon_not_ready`` and any other progress-bearing kind reset
  the streak — those still get the full warmup.
- **``ServerUnavailable.kind``** added to
  ``azt_collab_client.transports``. Recognised values:
  ``daemon_not_ready``, ``null_bundle``,
  ``server_apk_not_installed``, ``http_5xx``, ``transport_error``,
  ``http`` (loopback), ``''`` (unspecified). Existing call sites
  that ``except ServerUnavailable`` keep working; new sites can
  read ``ex.kind`` for fail-fast vs keep-retrying decisions.
  ``check_server_compat`` threads it into the result dict
  (``compat['kind']``).
- **Phase B + C planned** in ``docs/daemon_boot_plan.md``:
  provider-state in 503 body, ``bindService`` for OOM priority,
  optional daemon-side lazy imports if the new diagnostics show
  ``import azt_collabd`` is the dominant cost on slow tablets.

### azt_collab_client 0.31.5 — reframe smooth-UI section as a principle, not a recipe
- Per maintainer ask: § "Smooth UI across reloads" in
  ``CLIENT_INTEGRATION.md`` was framed as a
  recorder-specific recipe. Rewritten to lead with the
  **principle** (peers across the suite, whatever their
  model layer): same context; visible changes evident
  (including real upstream deletions, which propagate
  normally — LIFT workflows rarely delete but the principle
  doesn't paper over it); no other navigation; **suspend
  client-side filters that would hide the current view**
  (the failure mode is e.g. a "don't show past data" toggle
  excluding an entry the user is mid-edit because the data
  clock advanced — drop the filter for this view rather
  than swap the entry out). Recorder-flavoured snippet
  retained as one concrete realisation, not the contract.

### azt_collabd 0.31.2 + azt_collab_client 0.31.4 — sync fast-forward writes working tree; clone URL prefilled
- **Bug.** User report: "collaboration between clients on a
  project on two different phones is not smooth — each phone
  tracks its own changes but is unaware of others, even
  apparently when the user clicks on sync."
- **Cause.** ``_sync_repo_locked``'s fast-forward branch
  updated ``repo.refs[branch_ref] = remote_sha`` but never
  materialised the new tree to the working directory. Phone B
  fetched Phone A's commits, fast-forwarded the branch ref,
  but the LIFT file on disk stayed at Phone B's pre-sync
  bytes. Peers reading via ``LiftHandle`` got stale content
  and the UI looked unchanged. The diverged-merge branch was
  fine (``_merge_diverged`` already writes blobs to the
  working tree); only the fast-forward branch was the
  silently-broken case.
- **Fix.** New helper
  ``azt_collabd.repo._apply_tree_to_workdir(repo, project_dir,
  old_sha, new_sha)`` walks the diff between the two trees,
  writes added/modified blobs to the working tree, removes
  files that are gone in the new tree, and resets the index
  via ``repo.reset_index(new_tree)`` (with a ``_stage_all``
  fallback for older dulwich). Called from the fast-forward
  branch after the ref update. Diff-driven so unrelated
  untracked files (audio recordings the user just made and
  hasn't committed yet) aren't disturbed.
- **Peer-side principle documented** (peer follow-up, not in
  this bump). When the on-disk bytes change underneath a
  peer (``S.PULLED`` after sync, future ``MERGED_REMOTE``,
  re-clone, etc.), the user's view refreshes *in place*:
  same screen / entry / scroll position, fresh content,
  nothing else moves. If the entry the user is viewing was
  deleted upstream, keep the in-memory copy visible with a
  non-blocking notice rather than yanking them to a blank
  state. Sync is a refresh, not a navigation event. Spelled
  out as a principle (not a recipe) in § "Smooth UI across
  reloads" of ``CLIENT_INTEGRATION.md`` so each peer
  implements it through whatever model layer it has.
- **Clone-URL popup pre-fill.** ``clone_url_popup``'s URL
  field is now pre-populated with ``https://github.com/`` so
  phone-keyboard users can paste / type just ``owner/repo``
  instead of the full URL. Cursor lands at the end on open
  via ``Clock.schedule_once``. Submit-time guards: refuse
  empty / prefix-only input; if the user pasted a full URL
  *after* the prefix without first overwriting (so the field
  reads ``https://github.com/https://github.com/owner/repo``),
  take the rightmost protocol marker as the real URL start.

### azt_collab_client 0.31.3 — document grant_collaborator in CLIENT_INTEGRATION.md
- New § 10 "Granting collaborator access" in
  ``azt_collab_client/CLIENT_INTEGRATION.md``: covers the peer
  integration pattern (per-project settings only, never global),
  the project-disambiguation guarantee (peers pass langcode, the
  daemon resolves the repo — peers must NOT pre-resolve URLs),
  the full Result-status code list, translation pointer, and
  v1 scope (GitHub-only, invite-only, default ``push``).
- ``grant_collaborator_popup`` added to the "What the suite does
  *for* you" reference list at the bottom of the contract.
- Recovery / Testing sections renumbered 11 / 12.

### azt_collabd 0.31.1 + azt_collab_client 0.31.2 — grant-collaborator endpoint + popup
- **New endpoint** ``POST /v1/projects/<lang>/collaborators`` —
  invites a GitHub user as a collaborator on the repo backing
  ``langcode``. Looks the repo up via the project's
  ``remote_url`` so peers only have to pass a langcode (project
  disambiguation guaranteed server-side; no chance of peer-side
  URL handling targeting the wrong repo). Body:
  ``{username, level='push'}``.
- **Refactored** ``auth.add_collaborator`` to return ``'invited'``
  / ``'already'`` and raise on real errors (was: silent print +
  swallowed). The internal caller in ``repo._publish_repo``
  already wraps in ``try/except`` so its fire-and-forget
  semantics are preserved.
- **Status codes added** in both ``azt_collabd/status.py`` and
  ``azt_collab_client/status.py``:
  ``COLLABORATOR_INVITED``, ``COLLABORATOR_ALREADY``,
  ``COLLABORATOR_INVITE_FAILED``, ``INVALID_USERNAME``,
  ``NOT_GITHUB_REMOTE``. Plus translations in
  ``azt_collab_client/translate.py``.
- **Client wrapper** ``azt_collab_client.grant_collaborator(
  langcode, username, level='push')`` returns a ``Result``;
  re-exported from ``__all__``.
- **Reusable popup** ``azt_collab_client.ui.grant_collaborator_popup(
  langcode, on_done=None, font_name=...)``. Opens a popup that
  displays the project's langcode + remote URL prominently
  (project disambiguation is the load-bearing UX guarantee
  here), takes a username, calls ``grant_collaborator``, and
  surfaces translated outcomes. Auto-dismisses 2 s after
  success / "already a collaborator"; stays up on failures so
  the user can retry.
- **Peer integration** is per-peer (recorder / viewer / future):
  add a button to the project-context settings surface that
  calls ``grant_collaborator_popup(langcode=<current>)``. The
  button belongs in *project* settings, not global settings —
  the operation is meaningless without a specific project.
- **v1 scope.** GitHub-only (GitLab has different invite
  semantics; can be added by extending
  ``_parse_github_owner_repo`` and the dispatch). Invite-only
  (no list-existing or revoke yet, but the popup screen leaves
  room for either if you want them later).

### azt_collab_client 0.31.1 — Android 15 process-freezer workaround
- **Symptom.** On a budget Android 15 tablet (R500_V_US),
  cold-start peers showed "Connecting to AZT Collaboration
  service…" for the full 60 s daemon-warm-up budget, then
  fell through to "AZT Collaboration not responding."
  Verified: with the server APK launcher activity in the
  foreground, the same peer reaches the daemon in 5–10 s.
- **Cause.** Android 15's app freezer keeps the server APK's
  ``:provider`` process frozen even after a peer's
  ``ContentResolver.call`` triggers lazy-spawn — Python
  callbacks never finish registering inside the warm-up
  budget. Yesterday-vs-today framing was inconclusive; this
  is plausibly always-broken on certain ROMs and only
  surfaced today.
- **Workaround.** ``install_server_apk_popup`` gains an
  ``on_open_app`` parameter; when set it adds an "Open AZT
  Collaboration" button. ``_prompt_server_unresponsive``
  wires this to a callback that fires
  ``PackageManager.getLaunchIntentForPackage`` for
  ``org.atoznback.aztcollab``, then re-enters ``_check_server``
  on a 2 s delay with a fresh retry budget + a re-shown
  connecting popup. The launcher activity foregrounding
  un-freezes the package's process group, so when the user
  switches back to the peer, the next compat probe lands.
  Cheaper recovery than reinstalling the server APK.
- **Real fix later.** Peers should ``bindService`` to
  ``AZTServiceProviderhost`` while foregrounded so OOM
  priority prevents freezer interference in the first place.
  That's a Java change; deferred.

### azt_collabd 0.31.0 + azt_collab_client 0.31.0 — minor bump for pre-distribution test
- Lock-step minor bump on both packages, with both floors moved
  to 0.31.0 (``MIN_CLIENT_VERSION`` on the daemon,
  ``MIN_SERVER_VERSION`` on the client). Folds in everything
  since 0.30.0: server-APK boot-on-lazy-spawn, sticky-bound
  ``:provider`` host, self-update auto-exit, GitHub-connect UX
  rewrite (state-machine + suspended-install detection +
  Verify-setup re-test affordance), pre-install APK validation
  (parse + signature + asset.digest cache freshness), bootstrap
  flow with mandatory vs voluntary distinction, server→peer
  language mirror, blocked-popup mailto link + Check-again,
  same-tag re-upload detection via ``last_seen_digest``, install
  poll lifecycle, suite-wide CONFIRMED-vs-CONNECTED gating, and
  the various translation additions and Kivy touch-routing fixes.
  Anything older talking to a 0.31 peer (or vice versa) gets a
  clean ``client_too_old`` / ``server_too_old`` and is routed
  through the bootstrap update flow.

### azt_collab_client 0.30.47 — mandatory-mode probe always forces prompt
- **Regression from 0.30.46.** Recording ``gh_digest`` on
  Update-tap fixed the voluntary loop, but introduced a hole on
  the mandatory path: if the install failed (user cancelled at
  the Android installer screen, signature mismatch, etc.), the
  next ``_probe`` saw ``last_seen == gh_digest`` →
  ``digest_changed=False`` → ``needs_update=False`` →
  ``_show_no_newer_release`` fired even though GitHub actually
  has the right bytes (we just failed to install them).
- **Fix.** Replaced ``legacy_mandatory_force`` (only handled
  the first-run unknown-baseline case) with ``mandatory_force``
  (always forces the prompt in mandatory mode when the release
  feed returned something to download). Digest comparison is for
  "is there something new to offer?" — irrelevant when the daemon
  has already declared the client too old.
- **Clarification.** The 0.30.46 changelog framing "stuck at
  old version with no prompt" was sloppy. Voluntary install fail
  is benign — the client was already compatible (otherwise the
  daemon would have returned ``client_too_old`` and the mandatory
  path would have been used). The user runs a working older
  build and can retry via the peer's in-app Update button.
  Mandatory path is enforced terminal — no chance of leaking the
  user into project loading without a working daemon.

### azt_collab_client 0.30.46 — record gh_digest as last_seen on Update tap
- **Bug.** User report: "I click update on a voluntary screen,
  and it doesn't download… because it seems to be running from
  cache, this window keeps coming up since the digest is still
  different from gh." Trace:
  ``last_seen='3687f3…' gh_digest='efb3ae…'
  version_newer=False digest_changed=True``.
  Cache check correctly skipped the download (bytes already at
  ``efb3ae…``), install fired, but ``_record_last_seen_digest``
  was never called outside the first-run baseline branch — so
  ``last_seen`` stayed at ``3687f3…`` forever. Next bootstrap
  saw ``digest_changed=True`` and re-prompted.
- **Fix.** ``_prompt_self_update`` now receives ``gh_digest``
  from ``_probe`` and records it as the new ``last_seen`` baseline
  on Update-button tap. Recording at tap time (vs. install-complete)
  is the practical compromise: same-tag re-uploads don't flip
  versionName, and self-installs kill our process during install,
  so we have no reliable in-process completion signal. If the
  install ultimately fails the user is stuck at the old version
  with no further prompt — recoverable via the in-settings
  Update button or a manual reinstall.

### azt_collabd 0.30.45 + azt_collab_client 0.30.45 — re-bump for mandatory-update test pass
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.45** to force the
  ``client_too_old`` path on any peer bundling an older client
  (continuing test pass for the digest-change decline fix).

### azt_collabd 0.30.44 + azt_collab_client 0.30.44 — decline-memory ignores same-tag re-uploads
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.44** to force the
  ``client_too_old`` path on any peer bundling an older client
  (test pass for the digest-change decline fix).
- **Bug.** User report: "Check again doesn't currently show a new
  apk online, despite a different sha256." Trace confirmed the
  probe was correctly setting ``digest_changed=True``
  (``last_seen='3687f35493c3'`` ≠ ``gh_digest='cceb3fc2ba05'``),
  yet no prompt appeared. Cause: the decline-memory check in
  ``_peer_update_with_confirm._probe`` only compared the version
  tag — a previous "Not now" tap against ``1.37.24`` masked
  every subsequent re-upload at the same tag, regardless of
  digest. The original comment ("a re-upload of a declined
  version still plausibly came with the user's prior decline
  intact") turned out to be wrong: a re-upload IS a different
  build the user has not been asked about.
- **Fix.** Decline check now skips when ``digest_changed`` is
  True, treating the new bytes as a fresh release. Also
  belt-and-braces gates the check on ``not mandatory`` so the
  ``client_too_old`` path can't be silenced by a stray decline
  entry from an earlier voluntary cycle.

### azt_collabd 0.30.43 + azt_collab_client 0.30.43 — mirror daemon UI language to peer + clearer mandatory-update wording
- **Language sync server → peer.** User report: switching the
  server APK's settings UI to French didn't translate any
  bootstrap-side popups in the peer (recorder / viewer).
  Cause: ``$AZT_HOME/config.json::ui.language`` is the
  persistence path, but on Android ``$AZT_HOME`` is per-process
  private (server APK has its own filesDir, each peer has its
  own), so file-system writes from the server's settings UI
  never reached peer disk. Fix:
  - New daemon endpoint ``GET /v1/config/ui_language`` returns
    the server-side persisted language (handler
    ``_h_get_ui_language`` in ``azt_collabd/server.py``).
  - New client wrapper ``get_server_ui_language()`` in
    ``azt_collab_client/__init__.py``.
  - ``bootstrap._sync_ui_language_with_daemon()`` runs at
    ``_check_server`` entry (immediately after a successful
    ``check_server_compat``) and applies the daemon's language
    via ``i18n.set_language`` so all peer-side popups + status
    text track. Best-effort: silent on RPC failure, peer
    keeps its local pref.
- **Mandatory-update wording.** User report: viewer at 0.8.2
  saw "AZT Viewer 0.8.2 is required" — confusing when 0.8.2
  is the current version (same-tag re-upload case where
  digest changed). Now branches in
  ``_prompt_self_update``:
  - ``latest_version != peer_version`` (genuinely newer
    release): "{name} {peer_v} is too old for the AZT
    Collaboration service. Tap Update to install {name}
    {latest}, or Quit to close this app." Both versions named.
  - ``latest_version == peer_version`` (same-tag re-upload —
    digest_changed=True triggered the prompt): "A new build
    of {name} {version} is available. The current build is
    too old for the AZT Collaboration service. Tap Update to
    install the new build, or Quit to close this app." No
    longer phrases the same version as both "what you have"
    and "what's required".
- Email-link-in-the-update-popup ask is deferred —
  ``install_server_apk_popup``'s body Label doesn't have
  ``markup=True`` yet, and the change isn't a one-liner.
  Tracked separately.

### azt_collabd 0.30.42 + azt_collab_client 0.30.42 — bump MIN_CLIENT_VERSION to 0.30.42
- Final test-pass bump on the daemon (``MIN_CLIENT_VERSION =
  "0.30.42"``) to exercise the mandatory-self-update flow now
  that ``_prompt_self_update`` calls ``App.stop()`` on
  install completion. Server APK rebuild required; peer at
  0.30.41 trips ``client_too_old`` and gets the mandatory
  Update / Quit popup.
- Drop ``MIN_CLIENT_VERSION`` back to a real-world floor
  before any release that ships in the public update
  channel. Same goes for ``MIN_SERVER_VERSION`` on the client
  side.

### azt_collabd 0.30.41 + azt_collab_client 0.30.41 — close peer cleanly after a self-install
- User report: "When I clicked update, it downloaded and
  installed fine, but then I found myself back at the same
  popup. I closed and restarted fine, but users shouldn't have
  to do that." Android usually kills the running peer during a
  self-install, but not always — on some devices the peer
  survives, comes back to foreground after the system
  installer dismisses, and the popup is right where it was.
  No way to recover without a manual restart.
- Fix: ``_prompt_self_update`` now passes the peer's own
  package name as ``install_target_package`` (read from
  ``PythonActivity.mActivity.getPackageName()``) so
  ``install_apk_from_url`` runs the post-install poll loop
  on it, and ``on_install_complete`` calls
  ``App.get_running_app().stop()`` so the running peer exits
  the moment ``PackageManager`` reports the new versionName.
  Next user launch lands on the new APK; no manual restart.
- Safe both ways: if Android kills us during install (the
  common case), the poll thread dies with us — no leak. If
  Android doesn't kill us, the poll fires, we self-stop. The
  pre-0.30.41 comment about "polling our own package would
  block forever" was wrong: while the system installer is in
  foreground the Kivy Clock pauses, but it resumes when we
  come back, the poll detects the change, and we exit.

### azt_collabd 0.30.40 + azt_collab_client 0.30.40 — distinguish mandatory peer self-update from voluntary
- ``_prompt_self_update`` was using the same body + dismiss
  action for both the voluntary "newer version available" path
  and the mandatory ``client_too_old`` path. Declining a
  mandatory update via "Not now" silently fell through to
  ``on_done`` and dropped the user into the peer with a daemon
  that wouldn't talk to them.
- New ``mandatory`` parameter on ``_prompt_self_update``
  (forwarded from ``_check_self`` via
  ``_peer_update_with_confirm``):
  - **Voluntary** (``mandatory=False``, default): existing body
    "A newer version of this app ({version}) is available." —
    Update button + Not now (dismiss). Decline memory still
    applies so we don't re-prompt for the same version.
  - **Mandatory** (``mandatory=True``, the ``client_too_old`` +
    newer-version-exists path): body "{name} {version} is
    required to use the AZT Collaboration service. Tap Update
    to download and install it, or Quit to close this app." —
    Update button + Quit (action=``'quit'``, peer
    ``App.stop()``). No decline memory: a mandatory update
    can't usefully be remembered as "declined".
- Net effect: declining a mandatory update closes the app,
  matching the ``_show_release_too_old`` /
  ``_show_no_newer_release`` Quit semantics. Symmetric with
  ``_prompt_server_update``'s "Update" button + the existing
  install popup's Quit button on the server-side path.

### azt_collabd 0.30.39 + azt_collab_client 0.30.39 — bump MIN_CLIENT_VERSION to 0.30.39
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.39)
  so a peer at 0.30.38 paired with this server APK trips
  ``client_too_old``. Continues exercising the new
  digest-aware probe + version-anchor popup from 0.30.37/.38.
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.38 to actually trigger client_too_old.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.38 + azt_collab_client 0.30.38 — detect same-tag re-uploads via asset digest persistence
- Per maintainer ask: the version-tuple "any newer release"
  probe in ``_peer_update_with_confirm._probe`` couldn't see a
  re-uploaded asset on the same release tag. Common in dev
  iteration (push fix, keep tag), and unavoidable when a tag
  is hot-fixed in place. The new branch catches it via a
  digest-change check.
- ``_last_seen_digest(repo)`` and
  ``_record_last_seen_digest(repo, digest)`` persist the
  GitHub ``asset.digest`` per peer repo via the existing
  ``peer_pref`` / ``set_peer_pref`` store
  (``peer_prefs.last_seen_digests`` dict, keyed by
  ``owner/repo``).
- ``_probe`` now reads ``asset.digest`` from the latest
  release JSON, compares against the persisted last-seen, and
  treats EITHER a newer version tag OR a changed digest as
  "newer available". A trace line ``[bootstrap] _probe …
  version_newer=… digest_changed=…`` prints both signals so
  flaky cases are diagnosable from logcat.
- First-run baseline: if no digest is on file for ``repo``,
  the current GitHub digest gets recorded as the starting
  point so subsequent probes can detect change. Misses the
  perverse "re-uploaded between install and first launch"
  edge case but covers the dev-iteration use case cleanly.
- Storage scope: ``peer_prefs`` writes to whatever
  ``$AZT_HOME/config.json`` resolves to in the calling peer's
  process (peer-private on Android, shared on desktop), which
  is correct — each peer tracks its own repo independently.

### azt_collabd 0.30.37 + azt_collab_client 0.30.37 — fix client_too_old popup: version anchors + drop nonsensical pre-flight + Check-again tracing
- User report: "Recorder is too old" popup gave no version
  information either on screen or in the email body, and Check
  again seemed to do nothing.
- **Drop the pre-flight comparison in ``_check_self`` for the
  client_too_old path.** ``required_min`` from the daemon refers
  to the **client library** version (``azt_collab_client.
  __version__``), not the peer-app version. The recorder peer
  bumps its own version (1.34.0, …) independently of the client
  lib, so comparing recorder release tags to client-lib version
  numbers is meaningless — recorder 1.34.0 vs lib 0.30.36 has no
  defined order. The pre-flight either trivially passed or
  trivially failed depending on which way the major-version
  numbers happened to land. Replaced with the simpler "is there
  ANY newer peer release available" check that
  ``_peer_update_with_confirm`` already performs. We can't
  inspect a remote APK's bundled client-lib version without
  downloading it, so any-newer-version is the only honest signal.
- **Version anchors in the popup body and email.**
  ``_show_no_newer_release`` now takes ``required_client_lib``
  and ``bundled_client_lib`` and surfaces all four version
  values: peer name + peer version, bundled client lib (this
  build), and required client lib (from the daemon's compat
  handshake). Body reads e.g. "Recorder 1.34.0 is too old for
  the AZT Collaboration service. This build bundles client
  library 0.30.35; the service requires 0.30.36 or newer. No
  newer Recorder release is published yet." Email body lists
  the same anchors as labelled lines so the maintainer has the
  full mismatch in one read.
- **Check again tracing.** ``[bootstrap] Check again pressed —
  invalidating release cache + re-entering _check_server`` now
  prints when the button fires; ``_check_server`` is wrapped in
  try/except so any exception during the retry surfaces in
  logcat instead of silently dying in the worker thread. Lets
  us tell apart "Check again ran but result is the same"
  (visible re-render) from "Check again failed silently" (no
  trace lines after).

### azt_collabd 0.30.36 + azt_collab_client 0.30.36 — bump MIN_CLIENT_VERSION to 0.30.36
- ``MIN_CLIENT_VERSION`` raised to 0.30.36 (peer wandered to
  0.30.35, so we need to stay one ahead). Server APK rebuild
  required to pick up the new floor; peer at 0.30.35 trips
  ``client_too_old`` against this daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.35 + azt_collab_client 0.30.35 — bump MIN_CLIENT_VERSION to 0.30.35
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.35)
  so a peer at 0.30.34 paired with this server APK trips
  ``client_too_old``. Lets us exercise the new
  ``_show_no_newer_release`` popup (parity with
  ``_show_release_too_old``: Check again + mailto + Quit, no
  fall-through to ``on_done``).
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.34 so the test triggers cleanly.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.34 + azt_collab_client 0.30.34 — Update-needed popup parity + don't drop into client on dismiss
- ``_show_info`` (the "Update needed" single-button popup that
  fired on the ``client_too_old`` + no-newer-release branch) had
  two problems:
  1. UI didn't match ``_show_release_too_old``: only an OK
     button, no mailto link, no Check-again.
  2. After the user tapped OK, ``_check_self`` fell through to
     ``_on_done_and_release`` — host loaded a project, daemon
     refused subsequent RPCs, user stuck in a half-broken UI.
- Refactor: extracted the popup body into
  ``_show_update_blocked_popup(ctx, body_text, mailto_subject,
  mailto_body)``. Two callers — the existing
  ``_show_release_too_old`` and a new
  ``_show_no_newer_release`` — share the same Check-again /
  Quit / ``[ref=email]`` mailto-link UI vocabulary. The "Update
  needed" / OK / fall-through popup is gone.
- ``_check_self``'s on_no_update force_prompt branch now
  surfaces ``_show_no_newer_release`` and **does not** call
  ``_on_done_and_release``. Popup is terminal — Quit stops the
  app via ``App.stop()`` (no half-broken UI), Check again
  invalidates the release cache + re-runs ``_check_server``.

### azt_collabd 0.30.33 + azt_collab_client 0.30.33 — bump MIN_CLIENT_VERSION to 0.30.33
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.33)
  so any peer at 0.30.32 or earlier paired with a 0.30.33 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Continues exercising the symmetric self-update flow now that
  the bootstrap install-cache fix is in.
- Server APK rebuild required to pick up the new floor; peer
  can stay at 0.30.32 (or older) to actually trigger
  client_too_old when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.32 + azt_collab_client 0.30.32 — install_apk_from_url: validate cache against GitHub digest
- User report: bootstrap reused a cached 0.30.28 server APK
  instead of downloading the published 0.30.29, then Android
  rejected the install with "package appears to be invalid"
  (downgrade attempt). Root cause: ``install_apk_from_url``
  is the direct-URL path (called from
  ``install_server_apk_popup`` which bootstrap dispatches),
  with no GitHub-API cross-check. ``_has_fresh_download`` ran
  in sidecar mode — cached file matched its sidecar SHA, so
  reused — but sidecar mode can't tell "intact bytes" from
  "intact-but-stale-version bytes". ``check_for_update`` got
  the digest-mode fix in 0.30.22; ``install_apk_from_url``
  was still on the old path.
- Fix: new optional ``repo`` parameter on
  ``install_apk_from_url``. When supplied, the worker fetches
  the GitHub release JSON, finds the matching asset's
  ``digest`` (sha256:hex), and threads it through to
  ``_has_fresh_download`` as ``expected_sha256``. Stale
  caches with the right SHA-vs-sidecar but wrong SHA-vs-
  GitHub now fall through to a fresh download. Failure of
  the metadata fetch (network glitch) falls back to sidecar
  mode rather than blocking the install.
- ``install_server_apk_popup`` accepts and forwards the
  ``repo`` parameter; bootstrap's four call sites
  (_prompt_server_install / _prompt_server_update /
  _prompt_server_unresponsive / _prompt_self_update) all
  pass ``ctx.server_repo`` or ``ctx.peer_repo`` as
  appropriate.
- Net: the same digest-driven cache-freshness story that
  applies to settings-screen "Update this app" now also
  applies to bootstrap's "Install / Update AZT
  Collaboration" popup and to peer self-update from the
  bootstrap path.

### azt_collabd 0.30.31 + azt_collab_client 0.30.31 — log SHA check result in _has_fresh_download
- ``_has_fresh_download`` ran the cache integrity check
  (digest-mode against GitHub's asset digest, sidecar mode
  otherwise) but did so silently — user reported "no SHA log
  line, is this a regression?" against 0.30.28. Test was
  running, just invisible.
- Added a single ``[update] _has_fresh_download: ...`` print
  per call that names the mode (digest / sidecar), shows the
  truncated file SHA + expected SHA, and the boolean match
  result. Also prints early-exit reasons (file missing, hash
  failed, sidecar missing, sidecar empty, sidecar read
  error) so a False return tells you *why*. No behavior
  change.

### azt_collabd 0.30.30 + azt_collab_client 0.30.30 — bump MIN_CLIENT_VERSION to 0.30.30
- ``MIN_CLIENT_VERSION`` raised to 0.30.30 on the daemon so any
  peer running 0.30.29 or earlier paired with a 0.30.30 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Lets us exercise the symmetric ``_check_self`` /
  release-too-old / Check-again flow on the *peer* side
  (which previously we'd only proven for the server-too-old
  direction).
- Server APK rebuild required to pick up this floor change;
  peer can stay at 0.30.29 (or older) to actually trigger the
  client_too_old branch when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.29 + azt_collab_client 0.30.29 — bump MIN_SERVER_VERSION to 0.30.28
- ``MIN_SERVER_VERSION`` raised to 0.30.28 so the latest peer
  build keeps triggering ``server_too_old`` against any server
  APK at 0.30.27 or earlier (continuing the testing thread for
  the release-too-old / Check-again flow).
- Drop back to a real-world floor before any release that
  ships in the public update channel.

### azt_collabd 0.30.28 + azt_collab_client 0.30.28 — debug rebuild
- No code change; bump for a fresh peer APK to retest the
  Check-again cache invalidation on the device.

### azt_collabd 0.30.27 + azt_collab_client 0.30.27 — invalidate release cache on Check again
- 0.30.26's Check again button re-ran ``_check_server`` but the
  per-process release-JSON cache (5-minute TTL on
  ``_release_cache``) returned the stale "too old" entry, so
  the popup re-rendered immediately against the same data and
  the user saw no change. Closing/reopening the peer wiped the
  cache and worked.
- Fix: new ``invalidate_release_cache(repo=None)`` helper in
  ``azt_collab_client/ui/update.py``. Bootstrap's Check-again
  handler drops both ``ctx.server_repo`` and ``ctx.peer_repo``
  entries before re-running ``_check_server`` so the next
  probe re-fetches GitHub.

### azt_collabd 0.30.26 + azt_collab_client 0.30.26 — release-too-old popup: mailto link + Check again
- "Or build from source" was unhelpful for the SIL field-
  linguist user base — they're not going to. Replaced with
  "or [send the developer an Email]" rendered as a Kivy-
  markup ``[ref=email]`` link styled with underline + accent
  blue. Tapping it opens the user's MUA via ``mailto:`` with
  a pre-filled subject ("{name}: required version not yet
  released") and body containing the version mismatch info.
  No-MUA degradation is graceful — Android shows the standard
  "no app to handle this" toast.
- New ``azt_collab_client.MAINTAINER_EMAIL`` constant
  (``kent_rasmussen@sil.org``) so forks can override in their
  own client build (no env-var hook yet — change at the
  source line, rebuild). Matches the address in
  ``SECURITY.md``.
- Added a "Check again" button alongside Quit. Dismisses the
  popup and re-enters ``_check_server`` on a worker thread —
  same code path as bootstrap's compat probe, so a
  freshly-published release that meets the floor flows
  through to the install popup without the user having to
  restart the peer.

### azt_collabd 0.30.25 + azt_collab_client 0.30.25 — pre-flight release version vs required minimum + clearer body text
- User reported: bootstrap got server_too_old, popped the
  "Update AZT Collaboration?" dialog, the user tapped Update,
  and the latest published release was *also* too old to
  satisfy ``MIN_SERVER_VERSION``. Result: download succeeds,
  install succeeds, peer hits ``server_too_old`` again. Wasted
  bandwidth + a confused user.
- Fix in ``azt_collab_client/ui/bootstrap.py``:
  - ``_release_meets_minimum(repo, required_min)`` fetches
    GitHub's latest tag for the given release feed and
    compares to the required floor. Returns ``(ok, latest,
    error)``.
  - ``_show_release_too_old(...)`` is a new one-button popup
    that surfaces "{name} {required} or newer is required, but
    the latest available release is {latest}. Wait for an
    update or build from source." with a Quit affordance.
    Replaces the install popup when the upstream release feed
    can't satisfy the floor.
  - ``_prompt_server_update`` now takes ``min_required``,
    pre-flights the server APK release feed, and surfaces the
    "release too old" popup instead of opening the install
    popup if upstream can't help. Body text on the install
    popup itself now reads "{name} {required} or newer is
    required (you have {current})" — replaces the redundant
    "AZT Collaboration service (AZT Collaboration)" wording
    the user flagged.
  - Symmetric pre-flight on the ``client_too_old`` path:
    ``_check_self`` accepts ``required_min`` (forwarded from
    the daemon's compat response) and runs the same release-
    feed check before going into ``_peer_update_with_confirm``.
    A peer running ahead of GitHub's latest publish gets the
    same "wait for an update" popup instead of a useless
    download.
- Client-only rebuild needed to exercise this — bootstrap
  runs in the peer's process. Server APK can stay at whatever
  older version is on the device (that's what triggers
  ``server_too_old`` in the first place).

### azt_collabd 0.30.24 + azt_collab_client 0.30.24 — debug rebuild + MIN_SERVER_VERSION = 0.30.24 to exercise old-server flow
- ``MIN_SERVER_VERSION`` bumped to 0.30.24 deliberately, so a
  peer carrying this client paired with a server APK at 0.30.23
  or earlier hits ``check_server_compat()`` →
  ``server_too_old``. Lets us walk the bootstrap "Update AZT
  Collaboration?" prompt end-to-end without backdating any real
  wire-format change. **Drop back to a real-world floor before
  any release that ships in the public update channel.**
- No other code change; daemon and client are otherwise identical
  to 0.30.23.

### azt_collabd 0.30.23 + azt_collab_client 0.30.23 — debug rebuild
- No code change; bump for a fresh APK to retest the
  ``asset.digest``-driven cache-freshness check on the device.

### azt_collabd 0.30.22 + azt_collab_client 0.30.22 — verify cached APK against GitHub's authoritative ``asset.digest``
- 0.30.21 caught stale-version caches via versionName parsing.
  Per maintainer suggestion: GitHub's REST release-asset
  metadata exposes a SHA-256 ``digest`` field
  (``"digest": "sha256:<hex>"``, added 2025-06-03), so we can
  do an authoritative cache-freshness check without paying for
  a re-download.
- ``_has_fresh_download`` now takes an optional
  ``expected_sha256`` arg. When supplied (asset has a digest),
  it's the strong check — file SHA must equal the GitHub
  digest to reuse. When not (legacy assets pre-2025-06), it
  falls back to the existing sidecar self-consistency check.
- ``_worker`` parses ``asset.digest`` (strips the ``sha256:``
  prefix) and threads it through. Catches three failure modes
  in one place:
  - same-version-different-bytes (re-uploaded asset),
  - corrupted on-disk cache,
  - stale-version cache from a previous Update cycle.
  The versionName check from 0.30.21 is kept as a belt-and-
  braces layer for legacy assets where the digest is null and
  the SHA fallback can't tell same-bytes-different-version
  from a normal cache hit.

### azt_collabd 0.30.21 + azt_collab_client 0.30.21 — invalidate cached APK when versionName ≠ latest
- User repro: tapped Update on 0.30.19 → cache reused → install
  ran but version stayed at 0.30.19 (the cached APK was from
  the previous update cycle). ``_has_fresh_download`` only
  validates that the cached file's bytes match the sidecar's
  SHA — i.e. on-disk integrity. It does NOT check that the
  cached file is the version we're now trying to install.
- Fix: in ``check_for_update``'s ``_worker``, after the
  fresh-download check passes, ``_apk_parse_info`` reads the
  cached APK's ``versionName`` and compares it to ``latest``.
  Mismatch → remove the cached file + sidecar so the
  download branch runs and we fetch the right version.
- Diagnostic line ``[update] cache stale: cached_version=...
  != latest=...; discarding ...`` prints when the discard
  fires, so future repros are visible in logcat.
- Why not also fix this in ``install_apk_from_url``: that
  path doesn't know the "expected version" (URL is
  redirect-style and opaque); peers using it typically run
  install-once via bootstrap, so cache staleness is much less
  of a problem.

### azt_collabd 0.30.20 + azt_collab_client 0.30.20 — log parse_info + signature_matches result
- 0.30.18's pre-install validation didn't tell us which check
  outcome we got — user reports "same invalid response" against
  0.30.18 + 0.30.19. Did the parse pass? Did signature compare
  match? Returned None? We can't tell from logcat without an
  explicit print at each point.
- Added two trace lines:
  - ``[update] parse_info: ok pkg=... versionName=... path=...``
    (or ``parse_info: None path=...``) right after
    ``_apk_parse_info`` runs. Surfaces the APK's actual package
    name and version so we can spot manifest-level mismatches
    (e.g. wrong ``packageName``).
  - ``[update] signature_matches_installed: True/False/None``
    right after ``_signature_matches_installed`` runs. ``True``
    = match (install should succeed signature-wise),
    ``False`` = mismatch (we'd surface our error and abort),
    ``None`` = couldn't determine (off Android, app not
    installed, jnius unavailable, exception path).
- No behavior change.

### azt_collabd 0.30.19 + azt_collab_client 0.30.19 — debug rebuild
- No code change; bump for a fresh APK to retest 0.30.18's
  pre-install validation + suspended-messaging on the device.

### azt_collabd 0.30.18 + azt_collab_client 0.30.18 — pre-install validation + step-by-step suspended messaging
- **Pre-install APK validation in ``check_for_update`` and
  ``install_apk_from_url``.** Before firing the install Intent
  we now run two checks via ``PackageManager``:
  - ``getPackageArchiveInfo(dest, GET_SIGNATURES)`` to confirm
    the downloaded APK is parseable. A null result means the
    download was truncated / corrupted; the cached file +
    sidecar are removed and the user gets a "could not be
    parsed; try again to re-download" error rather than the
    bare Android "package appears to be invalid" complaint
    after dispatching the Intent.
  - ``signature_matches_installed(dest, package)`` —
    compares the APK's signing certificate against the
    currently-installed app's certificate. On mismatch we
    surface "Downloaded APK is signed with a different key
    than the installed app... Uninstall first, then tap
    Update again — or rebuild from source with the matching
    keystore." That replaces the cryptic "App not installed
    as package appears to be invalid" Android error and
    points the user at both fixes.
  - Helpers (``_apk_parse_info``,
    ``_signature_matches_installed``,
    ``_installed_version_name``,
    ``_android_package_manager``) live alongside the existing
    install-Intent code in ``update.py``. All three return
    ``None`` off Android / when pyjnius is unavailable so the
    desktop / non-Android paths short-circuit cleanly.
- **Diagnostic line** at the start of ``_install_on_ui``:
  ``[update] pre-install check: pkg=... installed_version=...
  running_version=... latest=...``. Tells us whether the
  device-installed version diverges from the running code's
  ``__version__``. They should match in normal use; diverging
  values are a hot-patch / dev workflow signal.
- **Suspended-state messaging.** Old "Resume it at {url}"
  was unhelpful — gave a URL but no idea what to do on the
  page. ``GitHubConnectScreen`` now stashes the
  ``installation_id`` from the Verify-setup probe and:
  - ``install_app`` opens
    ``settings/installations/<installation_id>`` (the
    install's configure page directly) instead of the
    generic install URL.
  - The accompanying message reads "Tap 'Install GitHub App'
    below to open the install's configure page on GitHub,
    then scroll to the bottom and tap 'Unsuspend'." —
    walks the user through the actual GitHub UI flow.
  - ``_render_message`` and ``_test_done`` both branch on
    ``_suspended_installation_id`` so the message survives
    re-renders (language change, screen re-entry).
- ``S.APP_SUSPENDED`` translation (used by sync 403 path,
  which doesn't have the in-screen "Install GitHub App
  below" affordance) updated to the same self-contained
  step-by-step shape: "GitHub App installation is suspended
  at {url}. Open it, scroll to the bottom, and tap
  'Unsuspend'."

### azt_collabd 0.30.17 + azt_collab_client 0.30.17 — restore Share icon
- 0.30.11's Share/Update simplification dropped the
  share-icon Image. Restoring per user preference: the
  ``SHARE_ICON`` KV macro and ``icon_path('share_dark')``
  format-arg are back, with the Image positioned as a
  left-overlay (``x: self.parent.x + dp(12)``) inside the
  half-width Share button. Text "Share" stays centered; no
  ``padding: [dp(52), 0]`` needed since a single-word label
  doesn't collide with the icon.

### azt_collabd 0.30.16 + azt_collab_client 0.30.16 — Android back button pops sub-screens to settings
- ``CollabUIApp`` (the standalone server APK settings host)
  didn't bind Android's hardware back button, so a back-press
  from GitHubConnectScreen / GitLabFormScreen fell through to
  ``App.stop`` and closed the app — losing the user's settings
  session mid-setup. Picker_app already had this hook;
  CollabUIApp didn't.
- Added ``CollabUIApp.on_start`` to bind
  ``Window.on_keyboard`` and ``_on_back_button`` to consume
  key 27 by popping ``sm.current = 'settings'`` from any
  sub-screen. Settings-screen back returns False to let Kivy
  / Android close the app the normal way.

### azt_collabd 0.30.15 + azt_collab_client 0.30.15 — swap primary button to "Verify setup" after install_app
- User report: at step 2, ``install_app`` opens the browser
  with a message that says "return here and tap Verify
  setup", but the primary button still reads "Install GitHub
  App" — there's no Verify setup button to tap. The user
  comes back from GitHub, reads the instruction, can't find
  the button it names, gets stuck.
- Fix: ``install_app()`` now flips ``gh_primary_btn``'s
  ``text`` to "Verify setup" and ``_action`` to ``verify``
  right after opening the browser, so the affordance the
  message promises actually exists. If the user returns
  without installing (cancelled, navigated away),
  ``test_github_credentials`` reports
  ``app_installed=False`` and ``_test_done`` re-runs
  ``on_pre_enter`` which puts the button back to "Install
  GitHub App" — they can retry without screen drift.
- Auto-polling for install completion was considered and
  rejected for v1: poll cadence vs. GitHub-API quota is a
  real trade-off, and the swap-and-tap workflow is bounded
  by user attention anyway. Easy to add later if needed.

### azt_collabd 0.30.14 + azt_collab_client 0.30.14 — log account.login on /user/installations + per-account install matching
- 0.30.13's trace showed three azt-collaboration installs in
  ``/user/installations`` while the user said they only ever
  saw one at ``github.com/settings/installations``. We don't
  yet know who owns the other two — could be orgs the user
  belongs to, could be stale state, could be something else
  GitHub is doing. ``check_app_installed`` now logs
  ``account.login`` alongside the existing fields so the next
  probe answers "whose installs are these?" directly.
- ``check_app_installed`` gained an optional
  ``account_login`` parameter that narrows the match to the
  installation whose ``account.login`` matches (case-
  insensitive). When omitted, the legacy "first match by
  app_slug" behavior is preserved.
- ``test_github_credentials`` now passes
  ``server_username`` (the user's own GitHub login) as
  ``account_login`` so Verify setup checks for the install
  on the user's own account, not "any install we can see."
  This fixes a real bug observed: user uninstalled their
  personal install but was still a member of orgs that had
  ``azt-collaboration`` installed; the unscoped match
  reported ``installed=True`` against an org install and
  the screen continued to show "Setup complete." With this
  change, Verify setup correctly returns
  ``installed=False`` once the personal install is gone,
  and the screen regresses to step 2.
- ``diagnose_403`` (sync 403 path) does NOT yet take the
  repo's owner into account; that's the next change once we
  see the per-account data and confirm the matching strategy
  is right. Currently still uses the legacy unscoped
  ``check_app_installed``, so this release narrows just the
  Verify-setup path.

### azt_collabd 0.30.13 + azt_collab_client 0.30.13 — log raw /user/installations to diagnose stuck suspended state
- 0.30.12 still showed "Setup Complete" against a suspended
  install on the user's device, even though both server APK
  and client are 0.30.12. So the suspended-detection code is
  running but either ``inst.suspended_at`` isn't being set on
  the install we're looking at, or the slug match isn't
  hitting the entry. Need data to disambiguate.
- ``check_app_installed`` now logs:
  - All ``(app_slug, id, suspended_at)`` tuples returned by
    ``/user/installations`` so we can see whether the user's
    install is in the list at all and what GitHub is reporting
    for ``suspended_at``.
  - The matched entry (if any) with its suspended_at and
    repository_selection.
  - The final ``result`` dict.
  - HTTPError / general Exception (was silently caught).
- ``_h_test_github`` now logs the test_github_credentials
  return value (valid / app_installed / app_suspended /
  installation_id) plus what it actually wrote to the store.
- Pure tracing — no behavior change. Build, retry Verify
  setup against the suspended install, and the next logcat
  will tell us whether the suspended detection is firing or
  whether GitHub is reporting the install as not-suspended
  for some reason.

### azt_collabd 0.30.12 + azt_collab_client 0.30.12 — fix suspended-state message overwrite race
- 0.30.11 set the suspended-message ``gh_message.text``
  immediately in ``_test_done``, but ``self.on_pre_enter()``
  on the line above schedules ``_refresh_state`` for the next
  frame, and ``_render_message`` there overwrites the field
  with the step-N default ("Setup complete..." / "Now install
  the GitHub App..."). User report: Verify setup against a
  suspended install kept showing "Setup Complete, connected
  as ...".
- Same defer-past-render dance the AuthError handler uses.
  Suspended message now goes through a second
  ``Clock.schedule_once`` so it lands after ``_refresh_state``
  completes.
- Note: this fix requires the server APK to ALSO be running
  0.30.11+ (or this 0.30.12). Older daemons' ``check_app_installed``
  matches solely on ``app_slug`` and reports
  ``installed=True`` for any installation, so a suspended
  install never gets the ``app_suspended=True`` flag and the
  client never enters the suspended-message branch. If you
  see "Setup Complete" after rebuilding the recorder but not
  the server APK, that's why — rebuild + reinstall the server
  APK and Verify setup again.

### azt_collabd 0.30.11 + azt_collab_client 0.30.11 — detect suspended GitHub App installs + simpler Share/Update buttons
- **Suspended-install detection.** User repro: paused the
  azt-collaboration App installation in their GitHub settings,
  the connect screen still showed "Setup complete (App
  installed)", and sync silently failed with codes
  ``['NOTHING_TO_COMMIT', 'REPO_NOT_AUTHORIZED']`` because the
  daemon's ``check_app_installed`` only matched on
  ``app_slug`` and reported ``installed=True`` for any
  installation including suspended ones. Fix:
  ``check_app_installed`` now reads ``inst.suspended_at`` —
  ``installed=True`` requires the install to be active, and a
  new ``suspended=True`` field is set when the App is on file
  but paused. ``installation_id`` is also returned in the
  suspended case so the UI can construct the resume URL
  (``settings/installations/<id>``) instead of the generic
  install page.
- **New status code ``S.APP_SUSPENDED``** plus translation:
  "GitHub App installation is suspended. Resume it at {url}."
  ``diagnose_403`` returns this for sync 403s when the install
  is suspended; ``test_github_credentials`` exposes
  ``app_suspended`` + ``installation_id`` alongside
  ``app_installed`` so the connect screen's Verify setup path
  surfaces a precise message and link instead of regressing
  silently to the step-2 "Install" prompt.
- **Connect screen ``_test_done`` handles suspended.** When
  Verify setup runs against a suspended install, the message
  becomes "GitHub App installation is suspended. Resume it at
  github.com/settings/installations/<id>." — the user can tap
  the URL or open it manually, resume on GitHub, and re-run
  Verify setup to confirm.
- **Wire format change.**
  ``POST /v1/credentials/github/test`` response now carries
  ``app_suspended`` and ``installation_id`` alongside the
  existing ``app_installed`` field. Older clients ignore the
  extras; newer clients against older daemons see ``False`` /
  ``None`` defaults (which is the correct
  "no-suspended-state-known" reading).
- **Settings: simpler Share / Update.** Two stacked
  full-width "Share this app" / "Update this app" buttons
  collapsed into one half-width row labelled just "Share" and
  "Update". Drops the share icon (one less asset to ship and
  less visual noise; the action is obvious from the label).
  ``SHARE_ICON`` KV macro and the ``share_icon=icon_path(...)``
  format-arg removed; ``icon_path`` import dropped.

### azt_collabd 0.30.10 + azt_collab_client 0.30.10 — keep "Verify setup" available after setup completes
- Once the user reached step 4 (everything verified),
  ``_render_primary`` was hiding ``gh_primary_btn`` entirely —
  forcing them to Re-authenticate (which means re-typing the
  8-field code) just to confirm the connection is still
  healthy. User asked for a non-destructive "test settings"
  affordance from the verified state.
- Fix: ``_render_primary`` now keeps the button visible at step
  4 with the same ``Verify setup`` label and ``verify`` action
  that step 3 uses. ``test()`` is idempotent (just hits
  ``api.github.com/user`` with the saved token); a successful
  re-test stays at step 4, a failure surfaces by regressing the
  screen state to step 2 / step 1 / "Token rejected" — a single
  tap that doubles as a diagnostic.
- Step-4 message updated to "Setup complete. Connected as
  {username}. Tap Verify setup any time to re-test." so users
  notice the affordance is intentional.

### azt_collabd 0.30.9 + azt_collab_client 0.30.9 — detach publish_row children to free the GitLab button
- User report: "GitLab button still doesn't respond until 10-12
  clicks" while the adjacent GitHub button works fine. Same root
  cause as the earlier ``gh_primary_btn`` issue: ``publish_row``
  is the next BoxLayout below ``gl_action_btn`` and stays at
  ``height=0`` for users with no project. BoxLayout's
  ``_do_layout`` still positions ``publish_btn`` (a RecBtn with
  ``on_press``) at its explicit ``dp(52)`` height under the
  collapsed parent, and Kivy's dispatch loop visits every child
  regardless. The combination intermittently swallows touches
  near gl_action_btn. (Why "intermittent" rather than "always":
  the touch points hover near the bottom edge of gl_action_btn /
  spacing area, where Kivy's hit-test math is sensitive to the
  exact tap coordinate.)
- Fix: ``SettingsScreen._refresh_publish_row`` now detaches
  ``publish_row``'s children when hiding the row (via
  ``_detach_publish_children`` / ``_reattach_publish_children``,
  matching the pattern used in ``GitHubConnectScreen`` for
  ``gh_manage_box`` / ``gh_device_flow_box``). A parent with no
  children cannot dispatch on_touch_down to anything, so the
  hidden publish_btn can no longer eat taps meant for the
  GitLab button. Idempotent on both sides.
- The detach is keyed off the same condition that hides the
  row, so users with an active publishable project (the
  "should-show-publish" case) keep the row's full functionality.

### azt_collabd 0.30.8 + azt_collab_client 0.30.8 — Connect-button gating + Disconnect inside settings + web-flow plan
- **Settings GitHub/GitLab buttons gated on ``confirmed``, not
  ``connected``.** User reported the canonical footgun: install
  failed midway, gh.connected stayed True (token was saved),
  refresh() flipped the button to "Disconnect GitHub", and
  the only tap available was the one that wiped the partial
  work. ``refresh()`` now reads ``gh.confirmed`` /
  ``gl.confirmed`` and renders:
  - Not verified → ``Connect to GitHub`` / ``Connect to GitLab``
    in green; tap navigates to the connect screen which
    auto-resumes from the user's current step (server state is
    the source of truth).
  - Verified → ``GitHub Settings`` / ``GitLab Settings`` in the
    neutral surface color; tap opens the same screen, now
    showing the manage view.
- **Disconnect moved inside each screen.** GitHubConnectScreen
  already had Disconnect in its manage box;
  ``GitLabFormScreen`` gained one in this release (visible via
  ``gl_manage_box`` only when a token is on file). Rationale: a
  fat-finger Disconnect from the main settings has a real cost —
  re-auth on GitHub means re-typing the 8-field code, re-auth
  on GitLab means re-pasting a PAT. Audit doc #6 + #7 updated
  to reflect this.
- **Removed** ``SettingsScreen.gh_action`` /
  ``connect_github`` / ``disconnect_github`` /
  ``disconnect_gitlab``. The KV buttons call ``app.go(...)``
  directly; the disconnect helpers live on each respective
  screen instead.
- **Web-flow migration plan** drafted at
  ``docs/web_flow_migration_plan.md``. Research finding: GitHub
  Apps' OAuth web flow accepts PKCE but still requires
  ``client_secret`` on the token exchange (per
  github.blog/changelog 2025-07-14 + community/discussions
  #15752), so a pure-PKCE mobile-safe flow is not legal. The
  plan documents (a) a Phase-1 ``tests/probe_pkce.py`` that
  validates this finding against the live API, (b) a web-flow
  architecture using embedded ``client_secret`` in the server
  APK only, with PKCE as defense-in-depth and device flow as
  the universal fallback, and (c) the open decision points
  (embed-secret tradeoff, fork story, sunset window for device
  flow). Not yet approved for implementation.
- **PKCE probe script.** ``tests/probe_pkce.py`` (intentionally
  not auto-collected by pytest — no ``test_`` prefix). Walks
  the user through up to three browser authorizations and
  validates the four cases laid out in the plan: PKCE param
  acceptance, PKCE-no-secret rejection, PKCE-with-secret
  success, secret-only-no-PKCE success. Exits non-zero on any
  deviation, so it doubles as a regression check if GitHub
  later changes its stance.

### azt_collabd 0.30.7 + azt_collab_client 0.30.7 — revert bogus URL prefill (audit doc #1 was based on a false premise)
- Audit doc #1 assumed GitHub's OAuth Device Flow returns
  ``verification_uri_complete`` (RFC 8628 §3.2) or at least
  honors ``https://github.com/login/device?user_code=ABCD-1234``
  to prefill the code field. After actually researching this:
  - GitHub's documented response is exactly
    ``device_code, user_code, verification_uri, expires_in,
    interval`` — ``verification_uri_complete`` is OPTIONAL per
    the spec and GitHub omits it. The canonical ``cli/oauth``
    Go reference impl and ``octokit/auth-oauth-device.js``
    both parse the field defensively for spec compliance but
    receive empty strings against github.com.
  - GitHub's ``/login/device`` page silently ignores the
    ``?user_code=...`` query parameter. No prefill happens.
  - A Jan-2024 GitHub change adds a "select Continue on an
    account" confirmation step in front of the code form
    unconditionally, even for single-account users. The user
    confirmed seeing this with one account.
- Reverted 0.30.5's URL-suffix construction. The fallback chain
  is now defensive only: use ``verification_uri_complete`` if a
  future GitHub change starts returning it, otherwise the bare
  ``verification_uri``. No more constructed query strings.
- ``docs/github_connect_ux_audit.md`` #1 updated with the
  research finding and links to the canonical references so the
  next person doesn't rediscover the false premise.
- The user-visible flow against the current GitHub: the user
  taps Begin → user_code displayed in our app + auto-copied to
  clipboard → bare ``/login/device`` URL opens in browser →
  GitHub's account-confirmation step → 8-field code form (no
  paste support either) → user types each digit → authorize.
  Polling worker picks up the resulting authorization within
  ~5s. Not a great UX, but it's the only path GitHub provides.

### azt_collabd 0.30.6 + azt_collab_client 0.30.6 — worker tracing + fix message overwrite
- Detach fix from 0.30.4 worked: ``gh_primary_btn`` now receives
  touches and ``primary_action: action='begin'`` fires correctly.
- New diagnostics in ``_worker``: trace device_flow_start,
  device_flow_poll completion, save_github_tokens success,
  app_installed probe result, _done firing, and AuthError /
  Exception paths. Intent: pin down why the screen doesn't
  advance to step 2 after the user authorizes — currently we
  can't tell if polling stalled, save failed silently, or _done
  ran but credentials_status didn't reflect.
- Bug fix: error handlers (AuthError / generic Exception) called
  ``self.on_pre_enter()`` (which schedules ``_refresh_state``)
  *then* set ``gh_message.text = 'Failed: ...'``. The deferred
  ``_refresh_state`` ran on the next frame and overwrote the
  message with step-1's "Tap Begin..." default. Now the
  Failed message is set via a second ``Clock.schedule_once``
  so it lands after ``_refresh_state`` and survives. Implication
  for the user: when polling actually times out, they'll
  finally see why instead of silently bouncing to step 1.

### azt_collabd 0.30.5 + azt_collab_client 0.30.5 — build the prefilled device-flow URL ourselves
- Audit doc #1 (the "Manual code-copy step in browser" win) was
  betting on GitHub returning ``verification_uri_complete`` in the
  device-flow response. Per RFC 8628 that field is OPTIONAL and
  GitHub elects to omit it for OAuth Device Flow — confirmed
  by the user landing on the bare code-entry page after Begin.
- Fix: when ``verification_uri_complete`` isn't in the response,
  build the prefilled URL ourselves by appending
  ``?user_code=<user_code>`` to the bare URL. GitHub's
  ``/login/device`` page reads the query parameter and prefills
  the code field, so the user still lands on "Authorize?"
  directly. If a future GitHub change starts returning
  ``verification_uri_complete`` we use it as-is and skip the
  suffix.

### azt_collabd 0.30.4 + azt_collab_client 0.30.4 — detach hidden box children so they can't intercept touches
- 0.30.3's diagnostics confirmed it: when the user taps inside
  ``gh_primary_btn``'s content y-range (Window y=1013-1070
  mapped to content y=305-362, well inside the button's
  pos=275 / top=405 range), the Window touch fires but
  ``gh_primary_btn``'s ``on_touch_down`` probe never does. So a
  sibling earlier in dispatch order is silently consuming the
  touch before the Begin button gets a chance — even though
  ``Widget.on_touch_down`` should have short-circuited via
  ``self.disabled and self.collide_point(...)`` on the hidden
  ``gh_manage_box`` / ``gh_device_flow_box``.
- Suspect: ``BoxLayout._do_layout`` keeps positioning children at
  their explicit heights even when the parent's ``height=0``, so
  Re-auth NavBtn (in the hidden manage box) lives at content
  y=85-205 with disabled=True. The disabled-eats-touch contract
  is supposed to handle this, but in this layout it didn't —
  some children were intercepting touches and others weren't,
  inconsistently. The mechanism's failure mode is opaque enough
  that fighting it with more flags (``disabled``, ``opacity``,
  ``height``) just shifts which configurations break.
- Fix: hide-by-detach. ``_hide_device_flow`` / ``_hide_manage``
  now call ``remove_widget`` on each child of the box; show
  re-adds them in original order from a per-box snapshot. A
  parent with no children cannot dispatch ``on_touch_down`` to
  anything, so there's no way for a hidden manage/device-flow
  child to intercept touches that should reach Begin /
  Install GitHub App / Verify setup. The snapshot stays
  strong-ref'd while detached so widgets don't GC.
- Idempotent: re-detach is a no-op if already detached;
  re-attach is a no-op if the snapshot is empty.

### azt_collabd 0.30.3 + azt_collab_client 0.30.3 — deeper Begin button diagnostics
- 0.30.2 told us the button is at ``pos=[50, 275]
  size=[980, 130] disabled=False opacity=1.0`` — sane — but
  ``state`` never flips on tap, so touch_down isn't reaching the
  button. Sibling NavBtns (Back, Create-account) in the same
  ScrollView work fine, so this isn't a global ScrollView /
  ButtonBehavior thing.
- Added two more probes:
  - ``btn.bind(on_touch_down=...)`` on the primary button so we
    log whether the button receives the dispatched event from
    its parent BoxLayout (with ``inside=collide_point(touch)``).
  - ``Window.bind(on_touch_down=...)`` so we log every raw touch
    Kivy receives, with the touch's reported position. Lets us
    correlate "where the user actually touched" against the
    button's pos and confirm whether the touch even arrives at
    the Window level.
- Expected next-run output on a tap:
  ``WINDOW touch_down: pos=(X, Y) inside_primary_btn=True/False``
  followed (or not) by ``gh_primary_btn on_touch_down: ...
  inside=...``. The combination tells us whether the touch
  arrives at all, whether it's at the right position, and
  whether the parent dispatches it to the button.

### azt_collabd 0.30.2 + azt_collab_client 0.30.2 — Begin button diagnostics
- 0.30.1's ``on_press`` switch did not help the Begin button:
  user reports ``_refresh_state`` still fires but neither
  ``primary_action`` nor any state log appears on tap, while the
  sibling Back / Create-account buttons (also inside the same
  ScrollView) work fine. So it isn't ScrollView vs. on_release —
  it's something specific to ``gh_primary_btn``.
- Added two diagnostics that will fire on the next attempt:
  - ``_render_primary`` logs the button's resolved ``pos`` /
    ``size`` / ``disabled`` / ``opacity`` after the render
    finishes. We need this to confirm the button is at the
    coordinates the user is tapping.
  - One-shot bind on the button's ``state`` property so every
    'normal' → 'down' transition surfaces in logcat. Button's
    state machine flips on touch_down regardless of whether the
    on_press / on_release events fire — so this lets us tell
    apart the two failure modes:
      * State changes but no ``primary_action`` log → event
        dispatch broken (binding lost?).
      * State never changes → touches aren't reaching the button
        (layout / hit-test problem; e.g. a hidden sibling box
        overlaps the touch zone).

### azt_collabd 0.30.1 + azt_collab_client 0.30.1 — switch settings/connect-screen action buttons to on_press
- User-reported: Begin (device-flow start) "still does nothing"
  even after the layout / id-resolution fixes in 0.29.1. The
  ``[github-connect] _refresh_state`` trace fires on screen
  entry, confirming the button is rendered with ``_action='begin'``
  and ``disabled=False``, but no ``primary_action`` log appears
  on tap.
- Diagnosis: classic Kivy ScrollView vs. Button issue. ScrollView
  records every touch_down for scroll-distance evaluation; if the
  user's finger jiggles even ~dp(20) during the press (easy on a
  touchscreen), ScrollView grabs the touch on touch_up and the
  child Button's state machine never fires ``on_release``. The
  user's previous complaint that the GitLab settings button
  "resists pressing in most cases. if I hit it randomly for
  awhile, it does eventually activate" is the same root cause —
  the rare clean tap was the one without enough movement.
- Fix: switch every action button inside a scrolled region from
  ``on_release`` to ``on_press`` so the dispatch fires on
  touch_down, before ScrollView decides whether to claim the
  touch. Affected sites: SettingsScreen Back / Share / Update /
  Publish / GitHub action / GitLab action / Refresh / Debug-503;
  GitHubConnectScreen primary / signup / Copy / Re-authenticate /
  Disconnect / Back; GitLabFormScreen Verify / Back; the dynamic
  language-selector buttons. Trade-off: actions can no longer be
  cancelled by sliding off the button before lifting the finger —
  fine for these recoverable flows (every button either
  navigates, opens the browser, or kicks off an RPC the daemon
  treats idempotently). Popups and modals are not affected;
  they're not inside a ScrollView.

### azt_collabd 0.30.0 + azt_collab_client 0.30.0 — boot Python on ContentProvider lazy-spawn; auto-exit on self-update
- **Server APK couldn't be reached after fresh install unless the
  user opened it manually.** ``AZTCollabProvider.onCreate()`` was a
  one-line ``return true`` — Android's ContentProvider lazy-spawn
  brought up the ``:provider`` host process and instantiated the
  Provider, but did not start any Service in that process, so
  Python never booted, ``install_callbacks()`` never ran, and the
  ``sDispatch`` slot stayed null. Every ``call()`` then fell
  through to the existing ``daemon_not_ready`` 503 fallback. The
  user's repro: install server APK → open recorder → recorder
  can't reach daemon → user opens server APK launcher → daemon
  finally boots → recorder works on the next attempt.
  ``onCreate()`` now calls ``AZTServiceProviderhost.start(ctx,
  "")`` so PythonService is started alongside the Provider on the
  same lazy-spawn. The boot is async (Python service thread
  spawns separately); the very first peer call may still race
  the boot and surface ``daemon_not_ready``, but ``rpc.call``'s
  existing transport-level retry sees the populated callbacks on
  the second attempt — the user no longer has to babysit the
  install.
- **Auto-exit on self-update.**
  ``azt_collabd.android_cp.service`` now snapshots the package's
  ``PackageManager.lastUpdateTime`` at ``install_callbacks()``
  time and re-reads it after every dispatch. If it has advanced,
  we schedule an ``os._exit(0)`` 500 ms later, so the in-flight
  binder reply has time to land and the next peer
  ContentResolver call lazy-spawns the freshly-installed code.
  Belt-and-braces: Android's package installer normally kills
  the upgraded process for us, but custom-ROM battery savers and
  ``adb pm install -r`` can leave the old daemon running with
  stale code while the new APK is on disk — that produced the
  "after updating, peer connects to old daemon until I
  force-stop the server APK" symptom. Adds one PackageManager
  call per dispatch (cheap; cached by Android in the same
  process).
- Lock-step minor bump because the Provider Java change requires
  a full server-APK rebuild — peers that update without the
  matching server-APK rebuild will still hit the daemon-boot
  race on lazy-spawn and have to open the server APK manually.

### azt_collabd 0.29.2 + azt_collab_client 0.29.2 — fix server-APK crash on KV format
- ``register_kv`` was raising ``KeyError: 'uri'`` from
  ``KV_TEMPLATE.format(font_name=..., share_icon=...)`` because a
  comment I added in 0.29.1 quoted ``"Opening {uri}\n..."`` as
  prose inside the KV string. Python's ``str.format`` reads
  ``{uri}`` as a substitution placeholder regardless of whether
  it sits inside a KV comment. The peer's "Could not open
  project picker: unexpected_cancel" + ``KeyError: 'uri'`` in
  ``register_kv`` traceback is the symptom — the server APK
  crashes during start-up, the picker activity returns
  RESULT_CANCELED with data, peer treats it as the picker
  anomaly retry path and gives up.
- Comment now spells the placeholder as "URL" without braces; the
  same risk applies to any ``{name}``-shaped token in KV
  comments, which is documented in the comment for the next
  editor.

### azt_collabd 0.29.1 + azt_collab_client 0.29.1 — fix unresponsive Begin / GitLab buttons after 0.29.0 restructure
- **GitHubConnectScreen "Begin" did nothing.** Two compounding
  causes, both shipped fixes:
  1. ``primary_action`` was re-fetching credentials_status to pick
     a step every tap. If the freshly-rendered button label said
     "Begin" but the daemon's status said the user was already at
     step 4 (stale from a prior session), the dispatcher fell
     through every elif and silently no-op'd. ``_render_primary``
     now stamps an ``_action`` attribute (``begin`` / ``install`` /
     ``verify``) on the button each time it renders; the
     dispatcher uses that, with a label-based fallback, and
     finally defaults to ``begin()`` so a button labelled "Begin"
     always begins.
  2. ``on_pre_enter`` accessed ``self.ids.gh_user_code`` and
     similar directly. On Kivy ≥ 2.3 the rule's nested children
     can lag a frame after the screen is added, so the early
     accesses raised an ``ObservableDict`` AttributeError mid-
     setup, leaving the screen in KV-default state with no
     ``_action`` tagged. ``on_pre_enter`` now defers via
     ``Clock.schedule_once`` (matches what ``SettingsScreen`` was
     already doing); every helper uses ``self.ids.get(...)`` so a
     genuinely-missing widget no longer takes the whole pass
     down.
- **GitLab/GitHub action buttons "resisted pressing"** in the
  settings screen: tapping the sibling RecBtn fired
  ``contributor_input.on_focus``, which called
  ``save_contributor()`` synchronously — the RPC blocked the UI
  thread for a few hundred ms during which Kivy still received
  the touch but couldn't dispatch the on_release until the RPC
  returned. ``save_contributor`` now runs the ``set_contributor``
  call on a worker thread; the "Saved." flash flips through
  ``Clock.schedule_once`` on the UI thread.
- **Layout-shift fix.** The new ``gh_preflight`` and ``gh_message``
  ``BodyLabel`` widgets used the
  ``height: self.texture_size[1] + dp(8)`` growing pattern; on first
  paint the texture is computed against width=0, so the label
  starts ~30 dp tall and grows as the layout settles, pushing
  every button below it down. Replaced with explicit
  ``height: dp(80)`` so the BoxLayout's ``minimum_height`` is
  stable from frame 0 — no more "tap where the button used to
  be" misses.
- **Tracing.** Added ``[github-connect]`` print lines on
  ``primary_action`` / ``begin`` / ``_refresh_state`` so a flaky
  field repro can be diagnosed from logcat without rebuilding
  with extra logging. Cheap.

### azt_collabd 0.29.0 + azt_collab_client 0.29.0 — GitHub connect-flow UX restructure (audit doc #1–#7)
- **#1 ``verification_uri_complete``**:
  ``GitHubConnectScreen._worker`` now prefers GitHub's
  ``verification_uri_complete`` (URL with the user_code prefilled)
  over the bare ``verification_uri``. Users land on GitHub's
  Authorize? page directly instead of the code-entry detour.
  Falls back to ``verification_uri`` then the bare URL if
  GitHub's response shape ever changes.
- **#2 + #3 step-indicator + pre-flight + no auto-fire**: the
  GitHub connect screen is now organised as three explicit
  stages — *1. Authorize this device* → *2. Install GitHub App*
  → *3. Verify setup* — rendered as a colour/bold-coded
  indicator. A single state-aware "primary" button presents
  only the next required action; ``on_pre_enter`` derives the
  current step from server flags
  (``connected`` / ``app_installed`` / ``confirmed``), so a
  partial setup that picks back up later resumes from where it
  stopped (lost network, browser bail-out, app close all
  recoverable). Pre-flight body text explains what GitHub is and
  that a free account is required; the device flow is never
  auto-fired — the user always taps *Begin* / *Install GitHub
  App* / *Verify setup* explicitly. ``_render_message`` /
  ``_render_steps`` / ``_render_primary`` / ``_render_manage``
  handle the four screen shapes.
- **#4 "Verify setup" rename**: both GitHub and GitLab
  "Test connection" buttons are relabelled "Verify setup" — the
  old label sounded like an optional diagnostic but is actually
  the gate that flips ``confirmed=True``. Status messages
  referencing "Test connection" are updated to match.
- **#5 create-account link**: a "Create a GitHub account
  (free)" NavBtn just below the pre-flight panel opens
  ``https://github.com/signup`` in the user's browser. Pre-flight
  body text also names the account-required precondition so the
  user isn't surprised by GitHub's sign-in/up page.
- **#6 simplified host buttons**: ``SettingsScreen`` now shows a
  single state-aware GitHub button (label flips Connect ↔
  Disconnect from ``credentials_status``) instead of two
  parallel buttons, and a single ``GitLab`` button that opens
  the GitLab settings form. Connection details for both hosts
  remain in the Status block below.
- **#7 declined**: no "Are you sure?" disconnect popup —
  per-maintainer preference; with #1 landed an accidental
  Disconnect costs one tap to redo, and the GitHub App on the
  GitHub account is untouched, so re-Authorize is the only step
  needed. Audit doc records the rationale.
- French .po updated with the new strings; old "Test
  connection" / "Tap Test connection" entries left in place
  (translation-coverage drift detector only flags missing
  msgids, not orphans).
- Lock-step minor bump because the connect-flow restructure
  changes a daemon-side UI subprocess that peers spawn through
  ``open_server_ui()`` — version-string display in the settings
  footer flags the cut.

### azt_collabd 0.28.27 + azt_collab_client 0.28.27 — 60s warmup budget + "Try again" affordance
- ``_DAEMON_WARMUP_RETRIES`` raised again, 15 → 30 (30s → 60s).
  User-reported: 30s wasn't enough on their device — "next boot
  fails, the following one succeeds" — confirming Java-side cold
  spawn time can exceed 30s after ``pm clear`` or fresh install.
- New ``on_retry`` parameter on
  ``install_server_apk_popup``; when supplied, adds a "Try again"
  button to the popup. Bootstrap's
  ``_prompt_server_unresponsive`` now passes
  ``on_retry=_post_install_continuation`` so users on truly slow
  hardware can keep waiting past the 60s budget without having
  to download fresh (Install) or close the app (Quit). Tap →
  popup dismisses → 2s warm-up wait → fresh compat probe.
- Side effect: layouts the popup with up to 4 buttons in the
  action row (Quit | Try again | Open install page | Install).
  Each is text-wrap-bound so labels fit even on narrow screens.

### azt_collabd 0.28.26 + azt_collab_client 0.28.26 — SHA-256 reuse check, drop the time window
- Replaced 0.28.25's mtime-window heuristic with definitive
  SHA-256 verification. After a successful download, ``update.py``
  writes the file's SHA-256 to a sidecar
  ``<asset>.sha256``. ``_has_fresh_download(path)`` now reuses
  the staged file iff (a) the file exists, (b) the sidecar
  exists, (c) recomputing the file's SHA matches the sidecar.
- Eliminates the "10 minutes might not be enough for everyone"
  concern — reuse works regardless of how long the user spent in
  the "Install unknown apps" Settings detour, and regardless of
  device speed.
- Cost: SHA-256 of a typical APK takes ~1–3 seconds on phone
  hardware. Negligible compared to the 10–30 seconds of
  re-download it replaces, especially on slow connections.
- Side benefit: catches APK corruption between download and
  install. If the file gets damaged somehow (rare, but possible
  on flaky storage), the sidecar mismatch forces a re-download
  rather than dispatching a corrupted install Intent.
- ``_save_download_sha(path)`` is non-fatal on failure: a
  missing sidecar just means the next reuse check returns False
  and we redownload, same as before this change.

### azt_collabd 0.28.25 + azt_collab_client 0.28.25 — reuse a recent download instead of re-fetching
- User reported the install flow was downloading the APK twice
  when Android required "Install unknown apps" permission: first
  download → permission detour → user grants → re-tap Install →
  re-download (10–30s wasted).
- New ``_has_fresh_download(path)`` helper checks whether the
  staged file at ``$AZT_HOME/updates/<asset>`` was last modified
  within ``_REUSE_DOWNLOAD_AGE_S`` (10 minutes). Both
  ``install_apk_from_url`` and ``check_for_update``'s download
  paths now skip the download when the file is fresh enough,
  surfacing ``Using already-downloaded file…`` status in place
  of the percentage progress.
- 10-minute window is conservative: long enough to cover the
  typical "user popped to Settings and came back" duration even
  on slow devices; short enough that a stale APK from a
  previous session (yesterday's launch, etc.) won't be
  installed when there's a newer release available.
- ``_download`` writes to ``<path>.part`` and only renames on
  success, so a present ``<path>`` is always a complete
  download — the freshness check doesn't need to validate
  partial-file recovery.

### azt_collabd 0.28.24 + azt_collab_client 0.28.24 — extend daemon-warm-up budget for cold starts
- ``_DAEMON_WARMUP_RETRIES`` raised from 5 to 15 (10s → 30s
  budget). The 503 ``daemon_not_ready`` response that fires
  during cold starts comes from ``AZTCollabProvider.java``'s
  ``sDispatch == null`` check, NOT from my Python sentinel
  hook — it's the genuine "Python interpreter not yet loaded"
  state. After ``pm clear`` or a fresh install the cold start
  can run 15–25 seconds because the dex cache needs rebuilding,
  and the previous 10s budget was tripping users into the
  "AZT Collaboration not responding" popup unnecessarily.
- Warm-cache normal launches still exit the retry loop on the
  first compat-probe success (1–3s typical), so the longer
  budget isn't user-visible in steady state. Cold-start users
  see up to 30s of "Connecting to AZT Collaboration service…"
  popup before falling through.
- Diagnostic clarification noted in the comment block: the 503
  has two possible sources (the test sentinel + the Java
  provider's startup gate); when troubleshooting, check that
  the daemon process is actually alive
  (``adb shell ps -A | grep aztcollab``) — if not, it's the
  Java side and the only fix is waiting longer or warming up
  the dex cache.

### azt_collabd 0.28.23 + azt_collab_client 0.28.23 — visible "Connecting…" popup during retries
- 0.28.19 added the daemon-warm-up retry loop, but the
  ``Connecting to AZT Collaboration service…`` status only flowed
  to ``ctx.on_status`` (host's status sink, often invisible).
  Result: 10s of empty-state peer UI with no feedback while the
  retries ran.
- New modal ``_show_connecting_popup`` opens on the first retry
  with a centred "Connecting to AZT Collaboration service…"
  message. ``auto_dismiss=False`` so the user can't tap past it.
  Dismissed on every terminal branch — ``compat ok``,
  ``server_too_old``, ``client_too_old``, retries-exhausted, or
  raised exception — before the next branch's UI fires (so the
  unresponsive popup doesn't stack on top of the connecting one).
- Mutable ``connecting_popup`` slot added to ``_Ctx`` so the
  show / dismiss helpers can find each other across the worker-
  thread boundary without a module-level dict. Idempotent: if a
  popup is already up, ``_show_connecting_popup`` is a no-op.

### azt_collabd 0.28.22 + azt_collab_client 0.28.22 — toggle the debug-503 sentinel from settings UI
- 0.28.21's sentinel could only be created via
  ``adb shell run-as``, which fails on release-signed APKs
  (``run-as`` requires the package to be debuggable).
- New "Debug (testing)" section in the daemon settings UI
  (``SettingsScreen``) with a toggle button that creates / removes
  ``$AZT_HOME/_debug_force_503``. State indicator below shows
  whether ``/v1/health`` is currently forced to 503 or responding
  normally. Always visible — production users tapping it just see
  "service unavailable" until they tap again.
- Test workflow: tap the server APK launcher icon (it's an
  installed app with its own icon) → settings UI opens directly
  (bypasses bootstrap; settings calls don't go through
  ``/v1/health``) → toggle Debug → close → launch peer →
  bootstrap retries 5×2s → unresponsive popup fires. Re-open
  server APK to toggle off when done.

### azt_collabd 0.28.21 + azt_collab_client 0.28.21 — debug hook to test the "not responding" popup
- Daemon's ``/v1/health`` (the compat handshake endpoint) now
  returns ``503 daemon_not_ready`` when
  ``$AZT_HOME/_debug_force_503`` exists. Toggle without restarting
  the daemon — the file presence is checked per-request. Create
  via ``adb shell run-as org.atoznback.aztcollab touch
  files/azt/_debug_force_503``; remove with the equivalent ``rm``.
- Lets the bootstrap workflow's daemon-warm-up retry path
  (``_DAEMON_WARMUP_RETRIES = 5`` × 2s = 10s) exhaust deterministically,
  exercising the "AZT Collaboration not responding" popup added
  in 0.28.20. Without this, manually triggering the unresponsive
  state required either killing the daemon mid-spawn (race-prone)
  or breaking the install (signature mismatch — heavy).

### azt_collabd 0.28.20 + azt_collab_client 0.28.20 — modal recovery popup when daemon stays unresponsive
- 0.28.19's retry-with-backoff fixed the common case (daemon
  warming up settles within 1–3s) but still fell through to
  ``_check_self`` → ``on_done`` after the retry budget exhausted.
  Result: the same bouncing-out behaviour the user originally
  reported, just delayed by 10s. ``on_done`` fired, peer's
  startup tried daemon RPCs, hit ``ServerUnavailable``, picker
  fired and failed, picker emitted CANCEL, peer closed via the
  picker-cancel rule.
- **Real fix**: when the warm-up retries exhaust without daemon
  response, bootstrap now shows a modal popup
  (``_prompt_server_unresponsive``) — same canonical
  ``install_server_apk_popup`` as the missing-server case, with
  a body reading "AZT Collaboration is installed but did not
  respond. It may still be starting up; wait a moment, then tap
  Install to reinstall it, or Quit to close this app and try
  again later." Title: "AZT Collaboration not responding".
- The popup gives the user three explicit recovery options
  (Reinstall, Open install page, Quit) instead of silently
  bouncing them out of the app. Modal blocking means the peer
  stays in the foreground until the user makes a choice, and
  ``on_done`` is not fired (so the peer doesn't run its
  post-bootstrap startup against a daemon that isn't there).
- Reinstall path: standard download+install via
  ``install_apk_from_url``; on completion the post-install
  continuation re-runs ``_check_server`` and on_done fires from
  the healthy path. Quit path: closes the peer cleanly so the
  next launch starts fresh.

### azt_collabd 0.28.19 + azt_collab_client 0.28.19 — retry on daemon-warm-up race at startup
- **Symptom user reported:** opening peer A, log shows
  ``[bootstrap] AZT Collaboration installed but unreachable.
  Continuing offline.`` followed by ``[recent] last_project:
  ServerUnavailable: provider HTTP 503: daemon_not_ready``, then
  Android brings the previously-foregrounded peer B to the
  front. Sequence: bootstrap fires ``on_done`` thinking everything
  is fine because the server APK is installed; peer's normal
  startup tries ``last_project()`` while the daemon is still
  warming up; the daemon's ContentProvider returns 503; peer's
  picker logic kicks in, fails with the same 503; picker emits
  ``RESULT_CANCELED``; the picker-cancel rule from
  ``CLIENT_INTEGRATION.md`` § 5 closes the peer; Android brings
  the most-recent task forward.
- **Fix:** ``_check_server`` now retries the compat probe with
  backoff (``_DAEMON_WARMUP_RETRIES=5``,
  ``_DAEMON_WARMUP_INTERVAL_S=2.0`` → 10s budget total) when the
  server APK is installed but unreachable. Status flips to
  ``Connecting to AZT Collaboration service…`` during retries.
  Android lazy-spawns the server APK's Python interpreter on the
  first ContentResolver call; this typically settles within
  1–3 seconds, well under the budget.
- If the retries exhaust, we still fall through to
  ``Continuing offline.`` and ``_check_self`` (which fires
  ``on_done``). At that point the daemon is genuinely unreachable
  (crashed, hardware glitch, signature mismatch denied us access)
  and bootstrap can't fix it; the peer's host code is responsible
  for handling ``ServerUnavailable`` on its post-on_done RPCs.
  Recommend defensive try/except around the first 1–2 RPCs the
  host makes after ``on_done`` — already in
  ``CLIENT_INTEGRATION.md`` § 4 ("log the failure and continue,
  not pop their own dialog").

### azt_collabd 0.28.18 + azt_collab_client 0.28.18 — move CLIENT_INTEGRATION.md into the symlinked package
- ``CLIENT_INTEGRATION.md`` moved from ``docs/`` (canonical-repo
  only) to ``azt_collab_client/`` (symlinked into every peer).
  Peers now see the integration contract through their existing
  symlink without needing a separate ``azt-collab/`` checkout.
  Old ``docs/CLIENT_INTEGRATION.md`` is reduced to a one-line
  redirect for anyone with the old path bookmarked.
- Added missing section on ``on_done`` semantics (introduced in
  0.28.5 but the doc never reflected it). Renumbered subsections
  to fix a duplicate ``## 6`` heading. Added the
  ``install_apk_from_url`` entry to the "what the suite does for
  you" list (added in 0.28.10, doc never updated).
- ``azt_collab_client/CLAUDE.md`` updated to point at the new
  in-package location.

### azt_collabd 0.28.17 + azt_collab_client 0.28.17 — peer self-update gets the same progress UI as server install
- **Peer self-update now uses the same popup as the server case**
  with progress visible in the body. Previously
  ``_prompt_self_update`` showed a Yes/No popup that dismissed on
  Update tap, then ran ``install_apk_from_url`` "in the background"
  with status flowing only to the host's ``on_status`` sink — which
  meant the user saw nothing until the install finished. Now
  bootstrap calls ``install_server_apk_popup`` (parameterized for
  the peer's own APK) so the same body-label progress (downloading
  %, retrying status, "Installing…", "Installed.") is on screen
  through the entire flow. Closes the user-reported "looks like
  it's stuck — Update just means OK" symptom.
- **``install_server_apk_popup`` is now a generic install/update
  popup**. New parameters:
  - ``direct_url`` — overrides the composed download URL.
  - ``asset_filename`` — overrides the on-disk staging name +
    MediaStore display name.
  - ``open_page_url`` — overrides the "Open install page" target
    so self-update points at the peer's release page instead of
    the server's.
  - ``dismiss_label`` — overrides the dismiss button label.
  - ``dismiss_action`` — ``'quit'`` (default; closes app) for the
    server case; ``'dismiss'`` for self-update where declining
    means "stick with current version, peer keeps running".
  - ``install_target_package=''`` — explicit "no polling" sentinel
    for self-update where the install kills the running peer
    process.
- **Self-update decline records the version only on Not-now tap.**
  Previously the decline was recorded synchronously in
  ``_prompt_self_update``'s decline handler. The new popup-based
  flow records on dismiss, but only when no install was started
  — if the user tapped Update and the install kicked off, no
  decline gets recorded (the next launch will detect the new
  version naturally instead).
- ``_yes_no`` helper removed; no callers left after the refactor.
- Lock-step bump 0.28.16 → 0.28.17 with ``MIN_SERVER_VERSION``
  raised to match (continues the user's "test the server-update
  path" workflow — every iteration of the bump fires the
  too-old-server prompt for a peer that bundles the new client).

### azt_collabd 0.28.16 + azt_collab_client 0.28.16 — bump MIN_SERVER_VERSION to test server-update path
- Lock-step debug bump 0.28.15 → 0.28.16 across both packages.
- ``azt_collab_client.MIN_SERVER_VERSION`` raised 0.27.0 → 0.28.16.
  Forces a rebuilt peer (which bundles client 0.28.16) to refuse
  any server APK older than 0.28.16. Test path: install peer with
  bundled 0.28.16, leave the older server APK (0.28.15 or earlier)
  on the device, launch peer → ``check_server_compat`` returns
  ``server_too_old`` → bootstrap fires
  ``_prompt_server_update`` → install popup shows the
  "Update AZT Collaboration?" body with the "Update" button label
  and pre-filled current_server_version (so the daemon doesn't
  redownload an identical release if there isn't actually a
  newer one published).
- ``MIN_CLIENT_VERSION`` (in azt_collabd) stays at 0.27.0 — this
  bump is for testing the server-too-old path, not client-too-old.

### azt_collabd 0.28.15 + azt_collab_client 0.28.15 — fix Install button stuck after "unknown apps" detour
- **"Tap Update again" message corrected** to use the actual
  install-button label. The popup's button is "Install" (or
  whatever ``install_label`` the caller passed), not "Update", so
  the previous message ("…then tap Update again") was wrong for
  every caller except the settings-screen Update buttons. Now uses
  ``{label}`` substitution and the popup passes its own button
  text.
- **New ``on_user_action_needed`` callback** in both
  ``install_apk_from_url`` and ``check_for_update``. Fires when
  the install path stalls because Android needs the user to flip
  "Install unknown apps" for this peer in Settings. Without this,
  the popup's Install button stayed disabled forever after we
  routed the user to settings — only Quit was active. The popup
  now wires this callback to re-enable Install + Open install
  page so the user can come back from Settings and retry.
- ``install_label`` parameter added to both functions so callers
  can override the label used in the "tap {label} again"
  message. Defaults to "Install" for ``install_apk_from_url``,
  "Update" for ``check_for_update`` (matching their
  conventional UX context).

### azt_collabd 0.28.14 + azt_collab_client 0.28.14 — fix language-toggle inertness + URL overflow in error
- **Language toggle in `install_server_apk_popup` now actually
  switches.** The handler called ``popup.dismiss()`` then
  ``install_server_apk_popup(...)`` synchronously from inside a
  Button.on_release — Kivy silently no-ops that re-entrance in
  some versions because the original popup is mid-dismiss. Fix:
  defer dismiss + relaunch via ``Clock.schedule_once(..., 0)`` so
  the touch handler returns first. Also added stderr logging at
  every step (``[install_popup] language switch: fr``,
  ``[install_popup] dismiss raised:`` …) so any future failure is
  diagnosable via ``adb logcat``.
- **Long URLs in error messages now wrap inside the popup.**
  ``_download``'s 404-with-URL surface for a 60-character GitHub
  asset URL was running off the body label because Kivy Labels
  only break at whitespace and URLs have none. New
  ``_wrappable_url(url)`` helper in ``update.py`` inserts a real
  ``\n`` after each ``/`` (only when the URL is over 50 chars) so
  the URL renders across multiple lines inside the popup body.
  Display is uglier but legible — readable URLs trump pretty
  ones for diagnosis.

### azt_collabd 0.28.13 + azt_collab_client 0.28.13 — diagnostic logging + browser-like headers in download
- ``_download`` now logs to stderr (visible in ``adb logcat``) at
  every meaningful step: the URL it's about to GET, the redirect
  target if any, the HTTP status, and the URL the server actually
  served the 404 from (``HTTPError.url``). Disambiguates
  "github.com returned 404" from "github.com 302'd to the CDN, CDN
  returned 404" — different diagnoses (asset truly missing vs.
  bot-pattern rejection on the CDN edge or expired-token edge
  case).
- Added ``Accept: */*`` and updated the User-Agent string to
  ``'azt-collab-updater/1 (+curl-compat)'``. Some GitHub CDN edges
  return 404 to bare-pattern UAs; mimicking curl removes that
  variable.
- Diagnostic-only round; the underlying 404 puzzle (browser works,
  ``gh release view`` confirms the asset, but Python urllib gets
  404 three times) is still being investigated. The new logging
  should make the next reproduction definitively diagnosable.

### azt_collabd 0.28.12 + azt_collab_client 0.28.12 — popup polish: language toggle, version footer, URL in error
- **Discrete language toggle** at the top of
  ``install_server_apk_popup``. First-install users whose device
  locale is French (or any non-English) had no way to switch
  language since the popup blocks the settings UI; now there's a
  small row of language buttons (current bolded, others tappable)
  that dismisses + re-opens the popup with the chosen language.
  Only shown when ``i18n.available_languages()`` returns more than
  one — desktop hosts running an English-only build won't see it.
- **Version footer** at the bottom of the popup
  (``client X.Y.Z``). Subtle / dim, mirrors the version strip
  pattern from the daemon settings UI. Helps diagnose which
  client build is actually live when reproducing UI bugs across
  versions.
- **Download error includes the URL we tried.** When the asset
  download fails (404 from the well-known direct URL, or any
  other transport error), the surfaced message now appends the
  URL on a new line. Lets the user eyeball the URL against what
  their browser successfully fetches — most 404s on this path
  come from an asset-name mismatch on the GitHub release, not a
  transport bug.

### azt_collabd 0.28.11 + azt_collab_client 0.28.11 — popup button wrapping, URL composition cleanup
- **Install popup button text wraps now.** Previously "Open install
  page" and "Quit AZT Recorder" / "Quit AZT Viewer" got clipped on
  narrower screens because Kivy Buttons don't wrap by default.
  ``text_size`` is now bound to button size on all three buttons
  (``halign='center'`` + ``valign='middle'``), and the button row
  height bumped to dp(60) to allow two-line wraps. Popup overall
  height also bumped from dp(280) to dp(300) to compensate.
- **Direct-URL composition consolidated** into the install popup's
  ``_do_install``. The hardcoded ``_DIRECT_DOWNLOAD_URL`` constant
  in ``update.py`` is gone; the popup now composes
  ``f'https://github.com/{_SERVER_REPO_DEFAULT}/releases/latest/download/{_SERVER_ASSET_DEFAULT}'``
  from the same constants the package-presence probe uses
  (``bootstrap._SERVER_REPO_DEFAULT``,
  ``bootstrap._SERVER_ASSET_DEFAULT``). Single source of truth: a
  fork that wants to point its server-install at a different
  release feed only edits the bootstrap constants.

### azt_collabd 0.28.10 + azt_collab_client 0.28.10 — direct-URL install for popup + peer self-update
- **New ``install_apk_from_url(url, asset_filename, ...)``** in
  ``azt_collab_client.ui.update``. Direct-URL alternative to
  ``check_for_update``: GETs the URL, streams to disk, dispatches
  Android's installer, optionally polls for completion via change-
  detection. No GitHub API call, no JSON parsing, no asset name
  matching, no listing-endpoint quirks. For when the caller has a
  stable redirect URL like
  ``releases/latest/download/<asset>`` and doesn't need version
  comparison.
- **`install_server_apk_popup`** now uses ``install_apk_from_url``
  for the Install button. Closes the user-reported "install
  comes back 404" symptom — the API path's ``_pick_asset`` step
  was failing on edge cases (asset-name mismatch, listing
  endpoint quirks). Direct URL bypasses the entire failure
  surface.
- **`bootstrap._do_self_install`** also migrated. Composes
  ``f'https://github.com/{peer_repo}/releases/latest/download/{peer_asset_filename}'``
  from the args bootstrap already takes. Version comparison
  still happens earlier in ``_peer_update_with_confirm`` (which
  needs the API for the small tag-name lookup); by the time we
  reach the install action, the user has confirmed the prompt
  and we just need to install whatever's at the URL. No
  ``install_target_package`` because self-install replaces the
  running peer process.
- **`_start_install_poll`** now supports two modes — pinned-
  version (used by ``check_for_update`` when it knows what
  version it just downloaded) and change-detection (used by
  ``install_apk_from_url`` which doesn't have version metadata).
  Change-detection snapshots the current installed versionName at
  start, then polls for any difference; trivially handles the
  uninstalled→installed case.
- **`_download`** now reads ``Content-Length`` from the response
  headers when the caller doesn't pre-supply ``total_bytes``, so
  progress percentages work for the direct-URL path too.
- **Settings-screen "Update this app" buttons** (CollabUIApp +
  PickerApp) stay on ``check_for_update`` because they want the
  "Up to date" message when the user taps without a newer version
  available — that's the value of the API path there.

### azt_collabd 0.28.9 + azt_collab_client 0.28.9 — restore "Open install page" semantics
- ``SERVER_APK_INSTALL_URL`` reverts to the **release page** URL
  (``https://github.com/kent-rasmussen/azt-collab/releases/latest``),
  not the direct-download asset URL. The popup's "Open install
  page" button is for users who want to read release notes or
  browse the project before installing — the page is what serves
  that purpose. The "Install" button in the same popup is the
  one-tap-to-install path; it discovers the asset URL via the
  GitHub API at runtime (asset['browser_download_url']) rather
  than from this constant.
- Effectively reverts the 0.28.3 URL-direction change. Sole
  consumer of ``SERVER_APK_INSTALL_URL`` is
  ``install_server_apk_popup._open_page``; everything else (the
  bootstrap workflow, ``check_for_update``) computes the
  direct-download URL itself.

### azt_collabd 0.28.8 + azt_collab_client 0.28.8 — fix SSL on Android urlopen
- p4a doesn't ship system CA certs into the Android Python
  runtime, so the new client-side ``urllib.request.urlopen`` calls
  in ``azt_collab_client.ui.update`` (release listing, asset
  download) fail with "unable to get local issuer certificate".
  The daemon side has had ``azt_collabd/net.py:_ensure_ssl()`` for
  this since forever, but the client can't import it (Hard rule 3:
  no daemon import; the two run in different processes on Android
  anyway).
- New ``azt_collab_client/net.py`` mirrors the daemon's SSL patch,
  slimmed for the client (no urllib3.PoolManager surface — the
  client doesn't speak dulwich). ``_ensure_ssl()`` is idempotent
  and called at the top of every urlopen site in ``update.py``.
  Finds certifi's bundle (preferred), falls back to extracting it
  from the bundled zip into ``$ANDROID_PRIVATE/cacert.pem``, then
  to common system locations, then to a verification-disabled
  context as a last resort.
- Symptom this fixes: post-popup-open SSL error on the bootstrap
  install flow's GitHub release probe — the "is a newer release
  available" call (``_fetch_latest``) and the asset-binary stream
  (``_download``) both bypassed SSL setup before this fix.

### azt_collabd 0.28.7 + azt_collab_client 0.28.7 — fix ModuleNotFoundError on popup open
- One-character relative-import bug introduced in the 0.28.4 popup
  refactor: ``azt_collab_client/ui/popups.py:369`` had
  ``from ..bootstrap import …`` (resolves to
  ``azt_collab_client.bootstrap`` — doesn't exist) instead of
  ``from .bootstrap import …`` (the correct
  ``azt_collab_client.ui.bootstrap``). The error fired the moment
  ``install_server_apk_popup`` was opened on a peer with no server
  installed, which raised inside Kivy's main loop and took the
  peer down — visible as "presplash, brief app screen, close".
  The user-reported symptom from 0.28.4 onward; the 0.28.5 / 0.28.6
  bootstrap fixes never had a chance to run because the popup
  itself couldn't load.

### azt_collabd 0.28.6 + azt_collab_client 0.28.6 — auto-resume after server install
- **Post-install continuation.** Once the install-completion poll
  watchdog confirms the new server APK is live, the popup auto-
  dismisses (after a 1-second visual confirmation showing
  "Installed.") and bootstrap re-enters its compat check. Daemon
  is now reachable, so the healthy path takes over and on_done
  fires, letting the host continue normal startup. No manual
  Quit + relaunch needed.
- New ``check_for_update(on_install_complete=...)`` parameter,
  threaded through ``_start_install_poll``. Fires only on
  confirmed completion (versionName flipped), not on the
  watchdog timeout — that branch still leaves the user with the
  "Install pending" message and the popup up.
- New ``install_server_apk_popup(on_install_complete=...)``
  parameter wires the upstream callback to (a) dismiss the popup
  after a 1s delay and (b) call the host's continuation. Bootstrap
  passes a continuation that schedules a 2-second daemon-warm-up
  pause (Android lazy-spawns the ContentProvider host on first
  call) before re-running ``_check_server``.
- If Android kills the peer process during install (memory
  pressure, system installer dominating), the popup + its
  continuation chain are gone too. Re-launch triggers a fresh
  bootstrap, which finds the daemon reachable and flows through
  the healthy path — same outcome, just one extra user action.

### azt_collabd 0.28.5 + azt_collab_client 0.28.5 — fix flash-then-die regression in 0.28.4
- **Bootstrap no longer fires on_done before the no-server popup
  opens.** In 0.28.4 the prompt branches called
  ``_on_done_and_release(ctx)`` immediately, then opened the popup
  on the next UI tick. Hosts whose ``on_done`` is wired to
  "continue normal startup" then attempted RPCs against a daemon
  that wasn't there yet, the failure cascaded into App.stop() (or
  similar in the host's error handling), and the popup that was
  about to open was killed alongside it — visible as a screen flash
  then peer shutdown.
- New ``_release_running()`` helper splits the guard release from
  the on_done notification. ``_on_done_and_release`` keeps both
  for the healthy terminal paths (server compat OK, no self-
  update needed). The two no-server branches —
  ``_prompt_server_install`` and ``_prompt_server_update`` —
  release the guard but don't fire on_done, so the host stays
  parked at whatever screen was up when ``bootstrap()`` was
  scheduled (typically a splash). Once the user installs the
  server APK and the peer relaunches, bootstrap re-fires from a
  fresh process, finds the daemon reachable, and on_done flows
  through ``_check_self`` along the healthy path.
- The already-declined-this-version branch in
  ``_prompt_server_update`` does still fire on_done (with the
  caveat that the host's first RPC will surface the daemon's
  compat error). The user explicitly chose this state by
  declining earlier; the host should handle it gracefully.

### azt_collabd 0.28.4 + azt_collab_client 0.28.4 — single canonical "no server" popup, modal blocking, Quit button, doc consolidation
- **Single popup for "no server" / "server too old" cases.** Bootstrap's
  `_prompt_server_install` and `_prompt_server_update` now both
  delegate to `install_server_apk_popup` (instead of the older
  generic Yes/No `_yes_no` helper). Result: one visual surface,
  one set of buttons, one progress sink, one decline path. Closes
  the user-reported bug where two popups stacked on first launch
  ("Could not open project picker: server_apk_not_installed" + the
  bootstrap Yes/No, OR the bootstrap Yes/No + the older
  "AZT collaboration service required" widget).
- **Popup is now modal-blocking** (`auto_dismiss=False`). The user
  can't tap past it to reach a settings screen or picker that
  would itself fail with "server_apk_not_installed". Resolves the
  user-reported "in the client settings page, asking to Select
  Project resulted in widget 1" — once bootstrap fires, settings is
  unreachable until the user installs or quits.
- **Quit button replaces "Dismiss".** Label is "Quit {App.title}"
  (e.g. "Quit AZT Recorder") — falls back to plain "Quit" if the
  host hasn't set a title. Tapping it dismisses the popup AND
  calls `App.get_running_app().stop()`. Without the server APK
  the peer can't function, so leaving it running was the wrong UX.
- **Install button shows live progress in the popup body.** While
  `check_for_update` runs, the body label updates with
  "Downloading 45%…", "Release in progress — retrying in 5s…",
  "Installing…", "Installed." (or "Install pending. Reopen this
  app when finished." on the polling-timeout branch). The popup
  stays open through the whole flow — no more "I tapped Install
  and nothing happened" because the popup dismisses-and-routes-
  elsewhere. Buttons disable while the worker runs to prevent
  double-taps.
- **`install_server_apk_popup` gained context parameters**
  (`body_message`, `current_server_version`, `install_target_package`,
  `install_label`, `title`) so the same popup serves both the
  missing-server case (default) and the too-old-server case
  (passed by `_prompt_server_update`). Different body text and
  Install-button label, same machinery.
- **Bootstrap dead code pruned.** `_do_server_install` and
  `_quit_app` removed — both were one-call helpers absorbed into
  the popup refactor. `_yes_no` survives for the self-update
  prompt (different decision: peer can keep running on decline).
- **`docs/CLIENT_INTEGRATION.md`** added — the canonical "what every
  client must do" checklist. Sections: symlinks, buildozer.spec
  permissions / signing / manifest extras, **bootstrap wiring +
  the four caller invariants**, **don't roll your own server-missing
  UI** (the source of the user-reported bugs), translation chain,
  `App.title` for the Quit button, LIFT / audio / image access,
  recovery, testing. `azt_collab_client/CLAUDE.md` now points to
  it as the canonical reference.
- Translations (fr): "Quit", "Quit {app}", and the longer
  bootstrap-prompt body text used by the popup
  (`This app needs the AZT Collaboration service ({name}) to sync
  your data. Tap Install to download and install it. Android will
  ask you to confirm before the install starts.`).

### azt_collabd 0.28.3 + azt_collab_client 0.28.3 — install-popup auto-download, asset filename fix, install-completion polling, release cache, bookkeeping
- **Asset filename fix.** Every codebase reference to the server APK
  asset was ``azt_collab.apk`` (with underscore), but the Android
  ``package.name = aztcollab`` in server_apk/buildozer.spec.tmpl
  drops separators per the suite naming table — actual published
  asset is ``aztcollab.apk``. The 5 hardcoded references —
  ``bootstrap._SERVER_ASSET_DEFAULT``, ``CollabUIApp.share_apk`` /
  ``update_app``, ``PickerApp.share_apk`` / ``update_app`` — all
  fixed. The bootstrap workflow's GitHub-API asset lookup was
  returning "no aztcollab.apk in release" 404s because of this; now
  matches the actual release feed.
- **`SERVER_APK_INSTALL_URL` is now a direct-download URL**
  pointing at
  ``https://github.com/kent-rasmussen/azt-collab/releases/latest/download/aztcollab.apk``.
  GitHub's stable redirect serves the most recent matching asset,
  so the URL doesn't need updating per release.
- **`install_server_apk_popup` triggers auto-install** instead of
  only opening the browser. Tap Install → ``check_for_update``
  fetches the latest ``aztcollab.apk`` asset, streams it to
  ``$AZT_HOME/updates/``, and dispatches Android's system
  installer. The popup's "Open install page" affordance is
  retained as a fallback for users whose Android can't trigger the
  install intent. Progress strings flow back through the popup
  body and the host's ``on_status`` sink.
- **Install-completion polling** for cross-package installs
  (server-from-peer). New ``check_for_update(install_target_package=...)``
  parameter. After dispatching the install intent, the helper
  polls ``PackageManager.getPackageInfo`` every 5s for up to 5min,
  fires ``on_status('Installed.')`` when the version flips to the
  freshly-downloaded one, and ``on_status('Install pending.
  Reopen this app when finished.')`` on timeout. Closes the
  long-standing UX wart where status hung at "Installing…" forever
  after the user backed out of the system installer or the install
  finished out-of-foreground. Self-installs (peer-from-peer) skip
  polling — the install replaces the running peer, so polling
  would block forever. Bootstrap passes the server's package name
  on its server-install path; the same path is taken when the
  ``install_server_apk_popup`` Install button fires.
- **Per-process release cache** for ``_fetch_latest``. 5-minute TTL,
  keyed by repo slug. Closes the rate-limit hazard where a
  bootstrap launch + a settings-screen Update tap + multiple peers
  behind one NAT could collectively drain the GitHub anonymous
  60/hour budget; subsequent calls within the TTL hit the cache.
- **Caller invariants** — the four contracts the bootstrap caller
  must honor (asset name match, parseable tag, prerelease flag,
  ``REQUEST_INSTALL_PACKAGES`` permission) are now consolidated as
  a top-level "Caller invariants" section in
  ``azt_collab_client/ui/bootstrap.py``. Each was scattered across
  the function docstring + the client CLAUDE.md recipe + update.py
  comments before; now a single canonical list.
- **`p4a.sign = True` removed** from the suite's
  ``server_apk/buildozer.spec.tmpl`` (separately from the earlier
  ``android.signing.*`` cleanup). Confirmed empirically: this spec
  key is also dead config; signing depends solely on the
  ``P4A_RELEASE_KEYSTORE`` env vars. Memory feedback updated.
- **Daemon version bump 0.27.0 → 0.28.3** (lock-step with client)
  to signal that this round touched both packages. No wire-format
  change; cross-floors (``MIN_CLIENT_VERSION`` /
  ``MIN_SERVER_VERSION``) stay at 0.27.0 — older clients/servers
  still talk to this daemon/client without issue.
- **`docs/p4a_hook_picker_intent.md` path-leak scrub.**
  ``/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py`` →
  ``$P4A_HOOK`` (the env-var-resolved path) for public-repo
  consumption.
- **"Not now" on server install closes the peer app.** Without
  the server APK the peer can't function (no daemon → no sync, no
  project picker), so dropping the user into a broken state is
  worse than asking them to relaunch. Server-*update* decline
  doesn't quit (peer can still work against the older server,
  bound by ``MIN_SERVER_VERSION``); self-update decline doesn't
  quit either (peer is fine at current version). New
  ``_quit_app`` helper in bootstrap.py.
- **Download retry on transient HTTP statuses** (``404, 429, 500,
  502, 503, 504``). Load-bearing case: GitHub publishes the release
  JSON before the asset binary finishes uploading, so
  ``browser_download_url`` briefly 404s. Three attempts with linear
  backoff (5s, 10s, 15s ≈ 30s total). Between attempts, the user
  sees translated "Release in progress — retrying in Ns…" so a
  hung worker thread is no longer a confusion. New
  ``on_status`` parameter on ``_download``.
- **Translations (fr)** for the new state strings: "Installed.",
  "Install pending. Reopen this app when finished.",
  "Release in progress — retrying in {s}s…", and
  "Tap Install to download and install it. Android will ask you to
  confirm before the install starts."

### azt_collab_client 0.28.1 — bootstrap hardening + first automated tests
- **Filter prereleases** from the latest-release probe in
  `update.py:_fetch_latest`. Walks `/releases?per_page=20` for the
  first stable entry; falls back to `/releases/latest` if every
  recent release is a prerelease or the listing endpoint refused.
  Closes the v0.28.0 bug where a project pushing a `vN-rc` tag would
  silently auto-install onto every peer.
- **bootstrap idempotence guard.** A second `bootstrap()` call within
  the same process now no-ops. Prevents double-prompting when an
  on_start hook fires twice during a Kivy reload or two startup
  hooks both wire the helper.
- **Decline memory.** When the user taps "Not now" on a prompt, the
  declined version is persisted to
  `$AZT_HOME/config.json :: bootstrap.declined.<repo>=<version>`.
  Subsequent launches skip the prompt for that exact version; a
  new upstream release moves us off the recorded value
  automatically (string compare, not semver).
- **Disambiguate "server APK absent" from "daemon unreachable"** by
  probing `PackageManager.getPackageInfo('org.atoznback.aztcollab')`
  before issuing the install prompt. If the package is installed but
  the daemon happens to be down (no network, OOM-killed mid-call),
  the helper now skips the install prompt and continues to the
  self-check instead of asking the user to install something that's
  already there. New status string
  "AZT Collaboration installed but unreachable. Continuing offline."
- **First automated test scaffold.** New `azt-collab/tests/` directory
  with pytest fixtures (per-test `$AZT_HOME` redirection, jnius stub,
  Kivy headless flags, platform monkeypatch) and five test modules
  covering version-tuple corner cases, GitHub-API mocks for
  `check_for_update`, bootstrap dispatch + idempotence + decline
  memory + package-presence disambiguation, the `github.confirmed`
  store lifecycle, and a translation-coverage drift detector.
  Run with `pytest tests/ -q`. CLAUDE.md updated to retire the
  "no automated test suite anywhere in the suite" claim.
- **`docs/research_notes_2026-05.md`** captures the state of the
  art for the technologies we depend on (Android 16, the March-2026
  sideloading lockdown, ACTION_VIEW deprecation in favor of
  PackageInstaller, buildozer/Kivy versions, GitHub API behavior).
  Action items are owned in the file. Refresh before each major
  release.
- **`docs/test_plan.md`** is the canonical failure-mode matrix. Every
  bug found in the bootstrap workflow lands here as a row in §10
  before it gets fixed.
- One new translation: "AZT Collaboration installed but unreachable.
  Continuing offline."

### azt_collab_client 0.28.0 — bootstrap() one-call peer entry point
- New `azt_collab_client.ui.bootstrap(...)` helper. Peers call it
  once on `App.on_start` and the helper handles, in this order:
  1. `check_server_compat()`. On `server_unreachable` →
     "Install AZT Collaboration?" Yes/No popup → `check_for_update`
     against `kent-rasmussen/azt-collab` → Android system installer.
     `server_too_old` runs the analogous "Update AZT Collaboration?"
     prompt. `client_too_old` jumps to step 2.
  2. Probe peer's own latest release on GitHub. If newer →
     "Update <peer>?" Yes/No → download+install the peer's APK.
  3. `on_done` — every up-to-date / declined / completed-install
     branch lands here so the host's normal startup always
     resumes.
  Suite UX rule encoded by this helper: **the user installs one
  APK** (the peer they opened); the standalone server APK and all
  subsequent updates are provisioned by the peer itself on first
  run. Spawns a worker thread for the version probes so first
  paint isn't blocked; popups marshal back to the Kivy UI thread.
- Android-only effects. Desktop hosts call `on_done` immediately.
- Buildozer requirement documented (`REQUEST_INSTALL_PACKAGES` in
  the peer's `android.permissions`); without it the install intent
  silently no-ops.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  the recorder, viewer, and any future peer can wire one
  ten-line `App.on_start` call and let the helper take it from
  there.
- New translations (fr): "AZT Collaboration", "Checking
  installation…", "Install AZT Collaboration?", "Update AZT
  Collaboration?", "Update {name}?", body strings for each prompt,
  "Install" / "Update" / "Not now" buttons, "Update needed" info
  popup, "Updating {name}…", "AZT Collaboration is up to date.",
  and the rare client-too-old-no-newer-release fallback message.

### azt_collabd 0.27.0 + azt_collab_client 0.27.0 — symmetric host credential flow
- **`github.confirmed` is now a stored flag**, not derived. Mirrors
  the existing GitLab semantics: set true by a successful live test,
  reset to false on any settings change (token save, app-install
  flag flip, disconnect). Per-host shape is now uniform — both
  GitHub and GitLab expose `connected` ("settings present") and
  `confirmed` ("tested OK against the host's API").
- New endpoint `POST /v1/credentials/github/test` (handler
  `_h_test_github`) and matching client wrapper
  `azt_collab_client.test_github_credentials()`. Hits
  `api.github.com/user` with the stored access token; on success
  also probes `api.github.com/user/installations` so the same Test
  button refreshes both `confirmed` and `app_installed` in one
  user gesture, matching the GitLab Test pattern.
- Auth helper `azt_collabd.auth.test_github_credentials(token)`
  added alongside the existing `test_gitlab_credentials` —
  consistent shape, same return dict (`{valid, server_username,
  app_installed, error}`).
- **`GitHubConnectScreen` is now state-aware.** Three shapes picked
  in `on_pre_enter` from `credentials_status['github']`:
  * not connected → device-flow box visible, manage hidden,
    `begin()` auto-fires.
  * connected, not confirmed → manage view (Test + Install GitHub
    App if not installed + Re-authenticate + Disconnect); device
    flow hidden; nothing auto-fires.
  * connected, confirmed → same controls plus a "(verified)"
    badge in the status line.
  Show/hide uses the Kivy hide/show pattern (height: 0, opacity: 0)
  per `~/.claude-sil/CLAUDE.md`. The screen is fully self-contained:
  Disconnect / Re-authenticate / Install-app no longer require the
  user to bounce back to settings.
- `SettingsScreen.connect_github()` reduced to a one-liner navigate.
  Auto-firing `begin()` on every entry to the screen is gone — the
  user with a token already on file isn't re-prompted for device
  flow; they get the manage view and pick Test or Re-authenticate
  themselves.
- Lock-step bump to 0.27.0 with cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.27.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.27.0. New wire endpoint
  + bundled-client peer APKs need the floor bump or version
  mismatches stay silent (ref. memory note on
  MIN_CLIENT_VERSION discipline).
- Translations (fr) for the new state-aware strings:
  "Re-authenticate", "Disconnect", "Install GitHub App",
  "Connected as {username} (verified).", the not-yet-tested and
  app-not-installed variants, "Token rejected by GitHub. Tap
  Re-authenticate.", "Could not open install page: {error}", and
  the "Opening {uri}\nWhen you finish on GitHub..." prompt.

### azt_collabd 0.26.0 + azt_collab_client 0.26.0 — in-app self-update
- New `azt_collab_client.ui.check_for_update(repo, current_version,
  asset_filename, on_status, ...)` reusable updater. Spawns a worker
  thread, polls `GET /repos/{repo}/releases/latest`, compares the
  release tag to the caller's `__version__` as a semver tuple, and on
  a newer release downloads the matching asset and dispatches
  `Intent.ACTION_VIEW` with the APK MIME type so Android's system
  installer takes over. All callbacks marshal back to the Kivy UI
  thread; non-Android hosts get a translated
  "APK install is only available on Android." through `on_error`.
- `REQUEST_INSTALL_PACKAGES` added to
  `server_apk/buildozer.spec → android.permissions`. The helper
  detects Android 8+ "Install unknown apps" gating via
  `PackageManager.canRequestPackageInstalls()` and routes the user to
  `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` on first use.
- `azt_collabd.configure(update_repo=...)` + `AZT_UPDATE_REPO` env var
  + `azt_collabd.config.update_repo()` accessor; default is
  `kent-rasmussen/azt-collab` (the canonical release feed at
  https://github.com/kent-rasmussen/azt-collab/releases/latest).
- "Update this app" button + status `BodyLabel` added to
  `<SettingsScreen>` directly under the existing "Share this app"
  row. Hosted by both `CollabUIApp.update_app` (standalone settings
  subprocess) and `PickerApp.update_app` (in-process settings reached
  from the picker's gear). Same KV `app.update_app()` resolves on
  whichever App owns the screen.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  peers (recorder targeting `kent-rasmussen/azt-recorder`, future
  viewer, …) can wire the same button into their own settings screens
  by passing their own `repo` / `__version__` / `asset_filename`.
- Translations added (fr): "Update this app", "Up to date.",
  "Checking for updates…", "Downloading {pct}%…",
  "Preparing install…", "Installing…",
  "APK install is only available on Android.", and the failure
  variants ("Update check failed: {error}", missing-tag /
  missing-asset / missing-URL detail strings, "Download failed",
  "Install failed", "Could not create download dir", and the
  Install-unknown-apps prompt).
- Lock-step bump to 0.26.0 across `azt_collabd` and `azt_collab_client`
  (no wire-format change; just keeping the cross-floors aligned now
  that the client gained shared UI surface peers will rely on).

### azt_collabd 0.25.2 — "Share this app" on the settings screen
- Added a Share-this-app row to `<SettingsScreen>` (`azt_collabd/ui/app.py`),
  positioned right under the Back NavBtn so it leads the scrollable
  body the same way the recorder's settings screen does. Hands the
  running server APK to Android's share sheet via the existing
  `azt_collab_client.ui.share_running_apk` helper — useful for
  onboarding teammates to the collab service. Hosted by both the
  standalone `python -m azt_collabd ui` (`CollabUIApp.share_apk`) and
  the in-process settings reached from the picker's gear
  (`PickerApp.share_apk`); the KV's `app.share_apk()` resolves on
  whichever App owns the screen at runtime.
- Icon (`share_dark.png`) sourced via `azt_collab_client.ui.icon_path`
  and threaded into the KV through `register_kv` next to the existing
  font-name substitution. Desktop hosts get the button too; tapping
  it surfaces the translated "APK sharing is only available on
  Android." message via the helper's `on_error` callback.
- French translations added for `Share this app`, `Share app`, `Error`,
  and the three `share_running_apk` failure messages
  (`APK sharing is only available on Android.`, the MediaStore-insert
  failure, the generic `Could not share APK:` wrapper).

### azt_collab_client 0.25.2 — public `ensure_mo` for peers
- `azt_collab_client.i18n.ensure_mo(locale_dir, domain, lang)` exposes
  the lazy `.po` → `.mo` compile path peers were previously missing.
  Peer i18n modules call it before `gettext.translation(...)` so they
  can ship `.po`-only and skip the external `msgfmt` build step the
  same way the client does. Writes the `.mo` next to the `.po`; on
  Android that's inside the APK's private filesDir, which is
  writable. See the *Internationalization (i18n)* section of
  `azt_collab_client/CLAUDE.md` for the integration recipe.
- `_ensure_mo(lang)` is now a thin wrapper around `ensure_mo` for the
  client's own domain — no behaviour change for the client itself.

### azt_collabd 0.25.1 + azt_collab_client 0.25.1 — French catalog catch-up
- Added 28 missing French translations to
  `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  covering the popup confirm-langcode flow, the clone-URL popup's
  inline `code: ` / `change code` / `OK` affordances, the daemon
  settings UI's publish row + GitLab Test screen, and the picker's
  empty-result fallback dialog. Catalog Project-Id-Version follows
  the package bump.
- Wrapped a stray ``'Open settings'`` literal in
  `azt_collabd/ui/picker_app.py:852` (clone-failure auth modal) with
  `_tr` so the translation already in the catalog actually fires.

### azt_collabd 0.25.0 + azt_collab_client 0.25.0 — synchronized release
- Lock-step version bump of both packages plus their cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.25.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.25.0. Intended to flush
  every peer APK through a rebuild so the cumulative work since the
  prior synchronization point lands in lockstep across the suite.
  The user-visible content of this release is the union of every
  entry below, plus the four-month gap of additive changes that
  preceded them. After this synchronization any peer running an
  older bundled client will surface `client_too_old` from
  `check_server_compat()` and any client talking to an older daemon
  will surface `server_too_old`, prompting an update on either side
  rather than silently degrading.
- Notable behaviour requiring lock-step:
  - `last_project` server-tracked via `/v1/recent/last_project`
    (older clients keep using their own sandbox, breaking on
    Android).
  - GitLab Test button drives `_h_test_gitlab` and the new per-host
    `confirmed` flag.
  - `Project.last_commit`, `ProjectStatus.commits_ahead` exposed on
    the wire; `_h_init_project` writes `last_sync` / `last_commit`
    back to projects.json on a successful publish.
  - `_resolve_path` reads `working_dir` from the registry; URIs
    decouple from on-disk dir naming.
  - `:provider` stdio bridge so daemon `print(..., file=sys.stderr)`
    actually reaches logcat.
  - dulwich `repo.refs[name]` (with `KeyError`) replaces the
    incorrect `.get()` call that was failing every sync silently;
    post-push remote-mirror update fixes `(+N)` indicator stickiness.

### azt_collabd 0.21.4 + azt_collab_client 0.24.0 — back from picker also returns to last project
- 0.21.3 only special-cased the BCP-47 langpicker; back from the
  *project picker screen itself* still hit the `if self.sm.current
  == 'picker': return False` early exit and let Android close the
  Activity with `RESULT_CANCELED`. The user's case from logcat —
  picker screen opens, back press, no `[picker_app]` trace at all,
  recorder receives an empty cancel — is exactly that path. New
  `_exit_to_last_project_or_cancel` helper centralises the
  emit-resumable-or-cancel shape, called from both the `picker` and
  `langpicker` branches of `_navigate_back`. Either exits the
  picker subprocess in one back-press, with the recorder receiving
  either the resumable project or a clean cancel.

### azt_collabd 0.21.3 + azt_collab_client 0.24.0 — back-from-langpicker always exits subprocess
- 0.21.1 added the langpicker→last_project special case but only
  when `last_project()` resolved; otherwise it fell through to the
  default `'picker'` target, which left the user on the project
  picker screen requiring a second back-press to actually exit.
  Now the langpicker back-press always exits the picker subprocess
  in one step: emit `last_project` if it resolves (recorder
  auto-resumes), or emit a clean cancel if it doesn't (recorder's
  `_handle_pick` silently returns to whatever it was showing).
  Either way, the user lands on the recorder, never on the project
  picker. New `_emit_cancel_and_quit` helper factors the
  `setResult(RESULT_CANCELED) + finish` shape out of
  `on_request_close` so it's reachable from anywhere.

### azt_collabd 0.21.2 + azt_collab_client 0.24.0 — URI resolution decoupled from on-disk dir name
- **Cloned project's LIFT URI returned `FileNotFoundException`.**
  `_resolve_path` in the ContentProvider was building
  `$AZT_HOME/projects/<langcode>/...` from the URI's first segment,
  but the picker_app's clone worker uses the URL's repo basename for
  `dest_dir` (e.g. `en_Demo.git` → `projects/en_Demo/`), while the
  URI handed back to peers uses the user-chosen langcode (e.g.
  `content://.../en/SILCAWL.lift`). Mismatch → resolver looked under
  the wrong directory → file-not-found → recorder's "lift namespace
  scan failed: forbidden: /en/SILCAWL.lift" log line. `_resolve_path`
  now consults `projects.get(langcode).working_dir` so URI resolution
  is independent of how the on-disk directory was named at clone
  time. Falls back to the legacy `<projects>/<langcode>` path when
  the project isn't in the registry, preserving pre-registry URIs.

### azt_collabd 0.21.1 + azt_collab_client 0.24.0 — back-from-langpicker resumes last project
- `_navigate_back` in `picker_app.py` now special-cases the
  `langpicker` screen: a user who reached Start-New and then changed
  their mind almost always wants to return to whatever project they
  had open before, not be re-parked at the project list. When
  `last_project()` resolves to a live registered project, back from
  `langpicker` emits that project's path and exits the picker
  subprocess (recorder auto-resumes). Cold-start fallback (no
  recorded last project) still goes to the picker screen so the
  user has somewhere to land.

### azt_collabd 0.21.0 + azt_collab_client 0.24.0 — sync was failing every cycle; remote-mirror not bumped after push
- **Sync was raising `AttributeError` on every cycle.** Pre-0.21.0
  `_sync_repo_locked` did `repo.refs.get(branch_ref) or repo.head()`,
  but `dulwich.refs.DiskRefsContainer` has no `.get()` method — only
  `__getitem__` (raises `KeyError`) and `read_ref()`. The call
  raised `AttributeError` post-fetch on every sync, propagated to
  `scheduler._fire`'s catch-all, and the job was marked
  `PUSH_FAILED`. Symptom: commits piled up locally but never
  pushed; "I see Audio recordings by Recorder commits showing up on
  GitHub minutes apart" was the queue draining whenever a build
  with the diagnostic try/except (added in 0.20.9) happened to be
  running. Replaced with a small `_read_ref(name)` helper that
  uses `__getitem__` + `KeyError`. Both `branch_ref` and
  `remote_ref` reads, plus the retry-loop's post-fetch read, now
  use it.
- **`commits_ahead` stuck at `(+N)` after a successful push.**
  Symptom: `[sync] done: codes=['NOTHING_TO_COMMIT', 'PUSHED']` ran
  cleanly but the recorder's indicator kept showing `(+10)`. Cause:
  `porcelain.push` advances the remote on GitHub but doesn't update
  the *local mirror* `refs/remotes/origin/<branch>` —
  `_count_commits_ahead` then compares the just-pushed `local_sha`
  against the pre-push mirror and reports the count of just-pushed
  commits as still-pending. Bumping the mirror explicitly after a
  successful push (`repo.refs[remote_ref] = local_sha`) reflects
  what CLI `git push` does and lets `commits_ahead` read 0 on the
  next status poll.

### azt_collabd 0.20.9 + azt_collab_client 0.24.0 — sync-hang trace, post-fetch ref reads
- The 0.20.8 trace narrowed the hang to the `local_sha = repo.refs.get(...)
  or repo.head()` line, so 0.20.9 splits that into separate
  `[sync-trace]` prints around `repo.refs.get(branch_ref)`,
  `repo.head()`, and `repo.refs.get(remote_ref)`. Whichever call
  doesn't produce its trailing print is where dulwich is wedging.
  Each is wrapped in try/except to surface a raise vs a hang
  cleanly.

### azt_collabd 0.20.8 + azt_collab_client 0.24.0 — sync-hang trace
- `[sync-trace]` prints between every step in `_sync_repo_locked`
  after the fetch (`fetch begin/done`, `local_sha`, `remote_sha`,
  `needs_merge`, `fast-forward`/`local ahead`/`merge_diverged
  begin/done`, `push loop begin`, `push attempt`, `push done`/`push
  raised`). Pinpoints exactly which step wedges between fetch
  returning and the function exiting — your latest log shows
  `fetch` returning HTTP 200 followed by silence until the next
  status-poll, so the hang is somewhere between `local_sha = ...`
  and the `push` HTTPS call.

### azt_collabd 0.20.7 + azt_collab_client 0.24.0 — last_sync on publish + `:provider` stdio bridge
- **Publish stamps `last_sync` / `last_commit`.** `_h_init_project`
  ran the publish (commit + push) and updated `remote_url` in
  `projects.json`, but did **not** stamp `last_sync` from the result
  codes. Sister handlers in `scheduler._run_sync` and
  `_h_project_sync` already did. Symptom: a successful publish left
  `Project.last_sync == 0`, the recorder's UI read that as "never
  synced" and kept showing the "data isn't being backed up" warning
  forever — even though the repo was sitting on github.com fully
  pushed. After this fix, a publish that returns `PUSHED` (or
  `COMMITTED_AND_PUSHED`) immediately stamps `last_sync` so the
  indicator flips on the next refresh.
- **Daemon prints now reach logcat.** `server_apk/service.py` calls
  a new `_bridge_stdio_to_logcat()` at module top that pipes
  `sys.stdout` / `sys.stderr` through `android.util.Log` under tag
  `python`. p4a auto-redirects stdio for `PythonActivity` (the
  Activity process where the picker / settings UI / recorder Kivy
  apps run), but not for `PythonService` — and the daemon
  (server.py, scheduler.py, repo.py) lives in the `:provider`
  process driven by PythonService. Pre-0.20.6 every `print` from
  daemon code went to a black hole, so functionally correct sync
  flows (RPC returns a job_id, the timer fires, the repo gets
  pushed) appeared in logcat as if nothing happened. After this fix
  the `[sync-async]` / `[sync-debounce]` / `[sync-fire]` / `[sync]`
  traces actually show up.

### azt_collabd 0.20.5 + azt_collab_client 0.24.0 — picker failure → notify + exit
- After `_pick_project_android`'s one-retry loop exhausts on
  `unexpected_cancel`, the client now schedules a Kivy modal on the
  UI thread: "The project picker failed to return a result. The app
  will now close — please reopen it." with a single OK button that
  calls `App.get_running_app().stop()` (and `os._exit(0)` as a belt-
  and-braces). Lives in the client so every peer using
  `pick_project()` gets the same fallback without per-host wiring;
  documents in the function docstring that `unexpected_cancel` is
  now a terminal status from the caller's perspective (the user
  will see the modal and exit before the caller observes the
  return value). Triggered by the user reporting empty-recorder
  windows three times in one debug session.

### azt_collabd 0.20.5 + azt_collab_client 0.23.6 — client-side request_sync trace
- `[sync-client] request_sync(<lang>, ...)` printed at every entry,
  with success/failure/transport branches. Closes the visibility gap
  between "the recorder called the RPC" and "the daemon received it"
  — if the client log is silent, the recorder never tried; if the
  client log shows a send but the daemon's `[sync-async]` is silent,
  the RPC is being dropped at the transport layer.

### azt_collabd 0.20.5 + azt_collab_client 0.23.5 — picker auto-retry on RESULT_CANCELED-with-data
- `_pick_project_android` now wraps the launch+wait in a one-retry
  loop. The picker contract is RESULT_OK→data /
  RESULT_CANCELED→no-data; the combo "non-OK + data attached"
  shouldn't be reachable normally (Android can synthesize it on
  back-press during `setResult`, or with OEM launcher tampering).
  Pre-0.23.5 we'd silently swallow that case as `'cancelled'` and
  drop the user on a recorder window with no project. Now the
  client classifies it as `'unexpected_cancel'` and re-launches the
  picker once, so the user gets another shot at choosing.
- The retry-loop refactor extracts the inner launch/wait logic into
  `_pick_project_android_once`; `attempt=` is included in the
  `[pick_project] _on_result: ...` trace so each invocation is
  identifiable in logcat.

### azt_collabd 0.20.5 + azt_collab_client 0.23.4 — full sync chain trace
- Three new diagnostics so a request → debounce → fire → run path is
  visible end-to-end in logcat: `[sync-async] <lang>` at RPC arrival
  in `_h_project_sync_async`, `[sync-debounce] <lang>` from
  `scheduler.request_sync` (so we see whether the recorder's
  `_auto_commit_sync` reached the queue), and `[sync-fire] <lang>`
  from `_fire` (so we see whether the debounce timer actually fired
  before the daemon process got recycled).

### azt_collabd 0.20.4 + azt_collab_client 0.23.4 — empty-registry disk scan
- When `_h_list_projects` returns zero entries, also print what
  `$AZT_HOME/projects/` actually contains on disk. Distinguishes
  "registry wiped but working trees survived" (recoverable: a
  future endpoint can scan + auto-register) from "filesDir gone"
  (server APK clean-installed; nothing to recover).

### azt_collabd 0.20.3 + azt_collab_client 0.23.4 — recent-state stderr trace
- `azt_collab_client/CLAUDE.md` rule #2 expanded to cover the
  *gating* failure mode (peers silently skip auto-sync on Android
  because their local filesystem check returns False) and to
  include the verbatim fix-shape snippet for `_project_has_remote`,
  so future Claude sessions touching a peer don't have to re-derive
  the daemon-served replacement.

### azt_collabd 0.20.3 + azt_collab_client 0.23.3 — recent-state stderr trace
- Diagnostic prints around every read/write of `last_langcode`:
  daemon-side `[recent] _touch_project(...) → /path/to/config.json`,
  `[recent] GET /v1/recent/last_project → 'lang' (from /path/...)`,
  and matching `POST` line; client-side
  `[recent] last_project → 'lang'` / `set_last_project(...) sent` /
  `ServerUnavailable: ...`. Pairs the path being written with the
  path being read so a divergent-`$AZT_HOME` bug shows up in logcat
  side-by-side instead of having to be inferred. No behaviour change.

### azt_collabd 0.20.2 + azt_collab_client 0.23.2 — MIN_CLIENT_VERSION floor for the recent.py RPC migration
- **`MIN_CLIENT_VERSION` raised to 0.23.0.** Pre-0.23 clients keep
  reading `$AZT_HOME/config.json::recent.last_langcode` from their
  own package's filesDir, which on Android sits in a different
  sandbox from the daemon's. The daemon stamps last_project on
  every langcode-bound RPC, but the peer's bundled client never sees
  it — recorder auto-resume falls through to the picker on every
  restart. Bumping the floor makes `check_server_compat()` return
  `client_too_old` instead of silently degrading, so the recorder
  surfaces the "please update" warning. Saved-memory note
  `feedback_min_client_version.md` documents this exact failure mode.

### azt_collabd 0.20.1 + azt_collab_client 0.23.2 — publish-row sticking after success
- **`_h_init_project` writes remote_url back to `projects.json`.**
  Symptom: a publish that returned `PUSHED` left the project's
  `Project.remote_url` empty in the registry, so the settings UI
  immediately re-rendered the publish row asking the user to publish
  again. `_init_repo` updates the *local* git config but the
  registry is a separate datastore; the back-write was missing. Now
  the daemon walks `projects.list_all()`, finds the entry whose
  `working_dir` matches, and writes the URL via
  `projects.set_remote_url`.
- **`_pick_publish_candidate` consults the live git remote.** Even
  with the back-write fix, projects published before 0.20.1 carry
  an empty cached `remote_url`. The settings UI now also reads the
  authoritative value via `project_status(langcode).remote_url`
  (which checks `.git/config`); the row hides if either source
  reports a remote. Defensive belt-and-braces so existing published
  repos behave correctly without a manual reconcile.

### azt_collabd 0.20.0 + azt_collab_client 0.23.1 — `commits_ahead` on ProjectStatus
- **`commits_ahead: int` on `ProjectStatus`.** Filed by recorder
  1.37.6 in `NOTES_TO_DAEMON.md`: the recorder's sync indicator
  needs the count of local commits not yet pushed to the remote so
  it can render `(+n)` instead of an opaque `*` marker.
  `repo_status_summary` now returns a 4-tuple
  `(branch, remote_url, n_changes, commits_ahead)`; `_h_project_status`
  forwards `commits_ahead` on the wire. Computed locally from
  `refs/heads/<branch>` vs. `refs/remotes/origin/<branch>` (no
  network round-trip), so a stale cache may under-report — the
  recorder's UX contract is "OK on uncertainty," so under-reporting
  is the right failure mode. Returns 0 whenever the local cache
  doesn't have a remote ref to compare against (no remote
  configured / never pushed). Client dataclass already had the
  field with `default 0` for forward-compat. NOTES_TO_DAEMON entry
  cleared.

### azt_collabd 0.19.1 + azt_collab_client 0.23.1 — server-canonical recent state + last_commit
- **`azt_collab_client/CLAUDE.md` rule #2 added** — "no reading
  project state from the local filesystem either," with the
  recorder's `_project_has_remote()` (dulwich.Repo on the working
  dir) called out as the canonical anti-pattern. Reads silently work
  on desktop and silently fail on Android because the daemon's
  working_dir lives in the server APK's private filesDir. Future
  peers must use `project_status(langcode)` for state-shaped checks.
- **Publish outcome message no longer clobbered by refresh.**
  `_publish_done` was setting the message *then* calling `refresh()`,
  which started by clearing `msg.text`, so the user only saw
  "Publishing..." and never the result. Reorder: refresh first, then
  set the outcome. Re-enables the button on failure.
- **`[publish]` stderr trace** in `_publish_worker`: prints the
  arguments going into `init_project` and the resulting `Result.codes()`
  (or the exception). Pairs with the `[sync-rpc]` / `[sync]` traces
  added in 0.18.2 so a publish failure has a logcat trail.

### azt_collabd 0.19.0 + azt_collab_client 0.23.0 — server-canonical recent state + last_commit
- **`last_project` is now server-tracked.** Was: each peer wrote
  `$AZT_HOME/config.json::recent.last_langcode` directly, which broke
  on Android where every peer's sandbox holds its own config.json
  (the recorder's write and the settings-UI subprocess's read landed
  in different files), and broke on desktop whenever a load path
  forgot to call `set_last_project`. Now: every langcode-bound RPC
  (`open_project`, `project_status`, `sync`, `sync_async`, `register`,
  `init`, `clone`, `from_template`, `rename`) auto-stamps via the new
  `server._touch_project` helper, and `last_project()` /
  `set_last_project()` are thin wrappers around new endpoints
  `GET`/`POST /v1/recent/last_project`. Single source of truth across
  peers and platforms; peers don't have to remember to call
  `set_last_project` from any specific load path.
- **Publish picker simplified.** With server-canonical
  `last_project`, the unpublished-projects-preference fallback in
  `SettingsScreen._pick_publish_candidate` (added in 0.18.1 to work
  around stale recorder-written state) is gone. The settings UI now
  resolves `last_project()` straight to the candidate Project; if
  that doesn't return a live project, the publish row hides — which
  is the correct UX, because nothing has been touched.
- **`Project.last_commit` field, separate from `last_sync`.** Filed
  by the recorder team in `azt_collab_client/NOTES_TO_DAEMON.md`:
  peer sync indicators couldn't distinguish "committed locally but
  not pushed" from "silently broken" because `last_sync` only
  stamped on `PUSHED` / `PULLED` / `COMMITTED_AND_PUSHED`. Daemon
  now also stamps `last_commit` on `COMMITTED_LOCAL` /
  `COMMITTED_NO_REMOTE` / `COMMITTED_AND_PUSHED` (any path where a
  commit object hit the working tree). Both fields ride on
  `Project` and `ProjectStatus`; pre-0.19 daemons that don't emit
  `last_commit` get a 0.0 default in the client dataclass for
  forward-compat. `NOTES_TO_DAEMON.md` entry deleted per its own
  instructions.
- **Sync trace lines** retained from 0.18.2: `[sync]` lines from
  `scheduler._run_sync` and `[sync-rpc]` from `_h_project_sync` so
  successful syncs show up in `adb logcat -s python`.

### azt_collabd 0.18.2 + azt_collab_client 0.22.1 — settings UX cleanup
- Sync trace lines on stderr (visible via `adb logcat -s python` on
  Android): `[sync] <lang> ... starting` / `... done: codes=[...]` from
  `scheduler._run_sync` and `[sync-rpc] ...` from `_h_project_sync`.
  Previously a successful sync emitted nothing — the structured
  `Result` carried the outcome but there was no trail in logcat to
  confirm the daemon had even seen the request.
- Publish-candidate fallback also prefers unpublished projects (the
  filtered `list_projects()` search would otherwise pick a
  more-recently-synced sibling that was already published, hiding
  the publish row even though a sibling project still needed
  publishing).
- Publish candidate falls back from `last_project()` to the
  highest-`last_sync` entry in `list_projects()` when the suite-wide
  "last opened" key is empty (older recorder load paths don't always
  write it). Diagnostic stderr lines from `_pick_publish_candidate`
  surface why the row stayed hidden.


- **GitLab "Connect" + Test button.** The settings screen's GitLab
  affordance is now labelled "Connect to GitLab" (was "Set GitLab
  credentials") to match GitHub's wording. The form screen replaces
  the bare "Save" button with a single "Test connection" button: the
  daemon-side `_h_test_gitlab` endpoint runs a live check against
  `gitlab.com/api/v4/user`, and only on success does it persist the
  credentials and stamp `gitlab.confirmed=True` in the store, so the
  user can't end up with a stored bad token. New endpoint
  `POST /v1/credentials/gitlab/test` (falls back to stored creds if
  body fields are empty) and client wrapper
  `test_gitlab_credentials(username, token)`.
- **Per-host `confirmed` flag.** `get_credentials_status()` now
  reports `github.confirmed` (derived: `connected AND app_installed`)
  and `gitlab.confirmed` (persisted; cleared on save, set on a
  successful Test). There is no longer a single "active host" — both
  hosts can be confirmed independently, and consumers (publish flow
  below, future sync flows) pick one based on context.
- **"Publish &lt;langcode&gt; data" button on the settings screen.**
  Visible only when `last_project()` resolves to a langcode whose
  project doesn't already have a remote; enabled when at least one
  host is `confirmed`. On click, single-confirmed hosts publish
  directly via `init_project(working_dir, remote_url, ...)`; both
  confirmed surfaces a small overlay so the user picks GitHub or
  GitLab. Mirrors the recorder's `do_publish` flow but moves it into
  the daemon UI, so any peer that exposes the gear can publish
  without owning the publish UI.
- **GitHub device flow no longer auto-fires on screen rebuild.**
  `GitHubConnectScreen.on_pre_enter` previously kicked the device
  flow on every entry, which meant a language-change rebuild — which
  clears + re-adds every screen — re-launched device flow even though
  the user was nowhere near the GitHub screen. The auto-start now
  lives on the explicit "Connect to GitHub" button via the new
  `SettingsScreen.connect_github()`, so language changes (and any
  other rebuild) leave the GitHub screen quiet.

### azt_collabd 0.16.0 + azt_collab_client 0.20.0 — sticky-bound server APK service + persistent scheduler jobs
- **Server APK lifetime fix.** The picker Activity now leaves the
  Python process running on Android instead of calling `App.stop()` /
  `sys.exit()`. A new sticky-bound service
  (`AZTServiceProviderhost`, `android/src/main/java/.../`) pins the
  host so `AZTCollabProvider.openFileDescriptor` can still serve the
  URI grant the picker just emitted. Pre-0.16.0 the server APK
  process exited as soon as the picker Activity finished, taking the
  provider with it and triggering Android's "depends on provider in
  dying proc" cascade SIGKILL of any peer that had received a
  `content://` URI from the picker.
- Service is sticky-bound (no foreground notification): peers get
  the bind-priority OOM hint while they're using the provider, and
  `START_STICKY` asks Android to recreate the service after a
  memory-pressure kill. Idle-stop policy (5 min of zero peers bound
  + zero provider activity) tears the service down so the design's
  "transient when idle, pinned while in use" intent is preserved.
  Manifest entry injected by `_inject_aztcollab_service` in
  `p4a_hook.py`, gated on `dist_name == 'aztcollab'`.
- **Scheduler jobs persisted to `$AZT_HOME/jobs.json`** so peer
  `poll_job(job_id)` calls survive a daemon respawn. `_store_job` and
  `_fire` write atomically on every state transition. New
  `scheduler.reconcile_on_startup()` runs from the loopback HTTP
  daemon entry (`server.run`) and the Android service entry
  (`server_apk/service.py`); marks any `PENDING` / `RUNNING` jobs
  found at startup as `DONE` + `JOB_INTERRUPTED` because their
  worker threads died with the previous process. Old `DONE`
  entries are GC'd past 1h at the same pass.
- New status code `JOB_INTERRUPTED` (`azt_collabd/status.py` and
  `azt_collab_client/status.py`, mirror) plus translation in
  `azt_collab_client/translate.py`. Peers should treat it identically
  to `SERVER_UNAVAILABLE`: transient, retryable.
- `MIN_CLIENT_VERSION` bumped to 0.20.0 — pre-0.20 clients don't have
  the `JOB_INTERRUPTED` translation and would surface the raw
  uppercase code in their UI.
- `MIN_SERVER_VERSION` bumped to 0.16.0 — pre-0.16 daemons don't
  persist jobs.json, so `poll_job` returns None for any job_id whose
  daemon has been respawned, indistinguishable from "never existed."
- Activity tracking added to `azt_collabd/android_cp/service.py`:
  `touch()`, `seconds_since_last_touch()`, `bound_client_count()`
  used by the service idle-stop loop. Every dispatch / openFile call
  bumps the touch timestamp.
- Picker app gains `on_pause` returning True so Kivy doesn't fight
  the missing GL surface after the Activity finishes.
- New `server_apk/test_install.py` (sibling of the existing adb-driven
  `test_install.sh`): 8-section desktop integration test for the
  kill-recovery flow — auto-spawn detection, jobs.json persistence,
  reconcile_on_startup, JOB_INTERRUPTED end-to-end. Run from the
  azt-collab repo root: `python server_apk/test_install.py`.

### azt_collab_client 0.19.2 — pick_project unbinds its activity-result handler after each call
- ``pick_project()`` registered a closure on
  ``android_activity.bind(on_activity_result=…)`` and never
  unbound it, so each invocation in a host session left a dangling
  handler that fired on every subsequent activity result. Logs
  showed N copies of ``[pick_project] _on_result …`` after N
  picks. Each closure wrote to its own long-since-stale
  ``holder`` so behaviour was correct for the most recent caller,
  but the JNI cost grew linearly with picks.
- New ``_unbind_handler`` helper called from inside ``_on_result``
  after ``done.set()`` (single-shot pattern) and from the timeout
  path so a much-later activity result for our request code can't
  write to a stale holder. Tracks ``bind_state['bound']`` to avoid
  unbinding a never-bound handler when ``_setup_on_ui`` failed
  early. Tolerates older Kivy / python-for-android versions that
  exposed ``bind`` without ``unbind``.

### azt_collab_client 0.19.1 — fix vanishing project list: defer ProjectPickerScreen.on_enter populate by one frame
- ``projects.json`` had the cloned project, the daemon's
  ``_h_list_projects`` would have returned it — but the picker
  never asked. ``ProjectPickerScreen.on_enter`` called
  ``_populate_projects`` synchronously, and Kivy >= 2.3 fires
  ``on_enter`` before KV-defined ids attach on the first screen
  entry. So ``self.ids.get('project_list')`` returned None, the
  populate function bailed silently, and the existing-projects
  list rendered empty — "cloned projects don't show up on next
  open". Same race the settings UI already worked around with
  ``Clock.schedule_once``; applied the same fix here.
- Added two diagnostic prints inside ``_populate_projects`` so
  any future bail (still-no-id even after the defer, or missing
  host ``list_projects`` method) surfaces in logcat instead of
  manifesting as a silent empty list.

### azt_collab_client 0.19.0 — suite-wide last-opened-project state (`recent.last_project`)
- New ``azt_collab_client.recent`` module with ``last_project()`` and
  ``set_last_project(langcode)``, persisted to
  ``$AZT_HOME/config.json`` under ``recent.last_langcode``. Re-exported
  from the package root.
- Same store as ``i18n``'s ``ui.language`` — no daemon RPC, just a
  file the client reads/writes; peers converge through the shared
  config without an explicit coordination channel. Recorder writes
  the langcode after every successful pick; the next peer launch
  (recorder, viewer, future apps) reads it at startup and lands on
  the same project.
- Resolve langcode → current path/URI via the existing
  ``open_project(langcode)`` (returns the daemon's authoritative
  ``Project`` record). ``recent.last_project()`` deliberately returns
  just the langcode, not a path — paths/URIs can shift across syncs;
  the langcode is stable.
- The "one store — suite-wide prefs AND state" rule generalises: the
  recorder's ``prefs['last_lift']`` was the second peer-private
  cache to fall under the rule (after ``prefs['ui_language']`` in
  0.16.0). Future cross-peer signals (last entry within project,
  contributor name) follow the same model.

### azt_collabd 0.14.5 — list_projects path/count diagnostic
- ``_h_list_projects`` now prints the resolved
  ``projects.json`` path it just read from plus the count and
  langcodes returned. Combined with the 0.14.4
  ``clone registered langcode=… → <path>`` print on the write
  side, the two log lines pin down whether the
  vanishing-projects bug is a write-vs-read path mismatch
  (different ``$AZT_HOME`` resolution between the two call
  sites) or a write-failed-silently issue. The actual on-disk
  content can be verified with ``adb shell run-as
  org.atoznback.aztcollab cat files/azt/projects.json``.

### azt_collabd 0.14.4 — inline langcode preview in clone popup; diagnostic prints for missing-after-relaunch projects
- Clone-URL popup gained an inline ``code: <derived>`` readout
  with an inline **change code** button right above the URL
  field. The readout updates live as the user types the URL;
  no separate confirmation step. Tapping **change code** swaps
  the readout for a small editable field — once the user takes
  control there's no auto-revert.
- Open-file flow uses the LIFT-filename-stem-derived langcode
  silently (no popup); user can rename later through whatever
  rename affordance lands.
- Diagnostic prints added in two spots so the user-reported
  "previously cloned projects don't show up on next open"
  actually points somewhere:
  - ``_clone_worker`` after ``projects.register``, prints the
    langcode and the resolved ``projects.json`` path. If the
    print appears, the registry write thinks it succeeded.
  - ``picker_app.list_projects`` (host method) prints how many
    projects came back from the daemon and which langcodes.
    If this prints 0 right after a successful clone, persistence
    or path-resolution is the issue (likely an ``$AZT_HOME``
    that differs between the clone-time process and the picker
    relaunch).

### azt_collabd 0.14.3 — clone accepts user-chosen langcode on input; rename_project endpoint
- ``POST /v1/projects/clone`` body gains optional ``langcode``;
  the picker collects an explicit value via the
  ``confirm_langcode_popup`` (client 0.18.2) before kicking the
  clone, so the project lands in ``projects.json`` keyed on the
  user's choice from the moment the daemon first sees it. No
  rename-after-the-fact in the registry. Empty ``langcode`` falls
  back to the daemon's auto-derivation from the LIFT filename /
  repo URL — matches the legacy desktop / scripted-call shape.
- ``_clone_worker`` gained an ``override_langcode`` kwarg; the
  registration step prefers it over ``derive_langcode``.
- New ``POST /v1/projects/<langcode>/rename`` endpoint
  (``_h_rename_project``) and ``projects.rename(old, new)``
  helper. Not used by the picker (which sets-on-create instead),
  but exposed for future flows that might let the user re-key a
  project after the fact (e.g., a settings-screen "rename
  project" affordance).

### azt_collabd 0.14.2 — clone job response carries canonical langcode
- Closes the azt-viewer 0.5.1 TODO ("picker should emit canonical
  langcode, not just leave the URI to be parsed"). The clone job
  response now includes ``langcode`` alongside ``lift_path`` —
  the same value the daemon just keyed the projects.json entry
  with on auto-register. ``_clone_worker`` captures it, the
  ``DONE`` job-state stash records it, ``_h_clone_status``
  passes it through.
- Source-of-truth chain (none of these need to derive from the
  URI on the peer side any more):
  - clone → daemon ``projects.derive_langcode`` → clone job →
    client ``clone_project`` returns ``langcode`` → picker stamps
    Intent extra.
  - open-file → ``register_project`` returns ``Project.langcode``
    → picker stamps Intent extra.
  - existing-project tap → ``picker.py`` populates the button
    with ``btn.langcode = projects_list_entry.langcode`` →
    ``load_lift(path, langcode)`` → picker stamps Intent extra.
  - template flow → user-typed BCP-47 (``_pending_vernlang``)
    already stamps the Intent extra.
- Backward-compatible. Old clients ignore the extra ``langcode``
  field on ``_h_clone_status`` responses; new clients hitting old
  daemons see ``langcode == ''`` and the peer's URI-parse
  fallback (defence-in-depth) still kicks in.

### azt_collab_client 0.18.3 — clone popup: only one input field active at a time
- ``clone_url_popup`` reworked so the URL field and the code field
  are mutually exclusive — only one is enabled at any moment, so
  the on-screen keyboard never argues with itself between two
  text inputs.
  - Mode A (default): code-preview row shows ``code: <derived> [change code]``;
    URL field is the active input.
  - Mode B (after tapping **change code**): code-preview row swaps
    in an editable code field with an ``[OK]`` button; the URL
    field is set to ``disabled=True`` (grays out, displays the
    typed URL, no input focus).
  - Tapping **OK** commits the typed code, re-enables the URL
    field, and swaps Mode A back in. Empty typed code clears the
    override so URL→code syncing resumes; non-empty pins the
    user's value through subsequent URL edits.
- Submit (Clone) works in either mode: takes the live code input
  in Mode B, the saved override in Mode A (post-OK), or the
  current URL-derived value otherwise. URL still required.

### azt_collab_client 0.18.2 — confirm-langcode popup: set on creation, not afterwards
- New ``ui.popups.confirm_langcode_popup(initial, on_submit)``:
  shows the auto-derived langcode in an editable field, asks the
  user to confirm or correct it. ``on_submit(chosen)`` fires on
  Confirm (and on Cancel with the original ``initial``, so the
  flow always resolves). Re-exported from ``azt_collab_client.ui``.
- ``picker_app.clone_dialog`` now derives a tentative langcode
  from the URL repo basename, runs ``confirm_langcode_popup``
  immediately after the URL submit, and only kicks the clone
  once the user confirms — passes the chosen value to
  ``clone_project(url, dest, langcode=chosen)``. The daemon
  registers under that exact key (no post-hoc rename). Helper
  ``_tentative_langcode_from_url`` mirrors the daemon's
  derivation order without requiring a filesystem path.
- ``picker_app.open_file._on_chosen`` runs
  ``confirm_langcode_popup`` after the file is picked but
  before ``register_project``; the chosen value goes to the
  registration call directly. Helper
  ``_tentative_langcode_from_lift`` strips the ``.lift``
  extension off the basename for the prefill.
- ``clone_project()`` / ``clone_project_start()`` accept an
  optional ``langcode=''`` kwarg routed into the request body;
  empty preserves the legacy auto-derivation behaviour for
  desktop scripted callers.
- New ``rename_project(old_langcode, new_langcode)`` wrapper for
  the daemon's ``/v1/projects/<langcode>/rename`` endpoint.
  Currently unused by the picker (set-on-create supersedes it),
  but exposed in ``__all__`` for peer apps that want to surface
  a "rename this project" affordance later.

### azt_collab_client 0.18.1 — clone_project carries langcode; project-list buttons stash langcode for load_lift
- ``clone_project()`` now returns ``langcode`` alongside
  ``lift_path`` / ``result`` / ``error`` on the success branch
  (DONE state). Daemon-side companion in 0.14.2.
- ``picker.py`` existing-project list now stores the project's
  canonical langcode on each button at populate time (the
  ``name`` half of the host's ``list_projects()`` tuple is already
  the langcode by contract) and passes it through on tap:
  ``app.load_lift(b.lift_path, getattr(b, 'langcode', ''))``. Host
  ``load_lift`` signature gains an optional ``langcode``
  parameter; default keeps existing single-arg callers working.

### azt_collab_client 0.18.0 — MediaHandle + audio_uri_for / image_uri_for
- New ``audio_uri_for(lift_path_or_uri, basename)`` and
  ``image_uri_for(lift_path_or_uri, basename)`` composer helpers.
  Given the picker-emitted LIFT path/URI plus a basename, return
  the sibling resource's URI (on Android-content URIs) or
  filesystem path (desktop) — so callers stay agnostic about the
  path/URI distinction. URI form composes
  ``content://<auth>/<lang>/{audio|images}/<basename>``, mirroring
  the daemon's ``_resolve_path`` whitelist. Filesystem form is
  ``os.path.join(os.path.dirname(lift_path), {audio|images}, basename)``.
- New ``MediaHandle(path_or_uri, kind='audio'|'image')`` —
  ``LiftHandle`` subclass with a ``kind`` for log lines / error
  messages, and a write-mode gate: ``open_write()`` on
  ``kind='image'`` raises ``PermissionError`` (images are
  read-only from peers; the daemon owns image additions).
- Re-exported from the package root: ``from azt_collab_client
  import MediaHandle, audio_uri_for, image_uri_for``.
- Together with ``LiftHandle`` (0.17.0), this is the full Tier 3
  cross-package toolkit the recorder migration documented in
  ``CLAUDE.md`` needs to land audio recording and image rendering
  on the new Android server-APK model.

### azt_collab_client 0.17.1 — client-first asset model: new icon_path helper, gear bundled
- New ``azt_collab_client.ui.icons`` module with public
  ``icon_path(name)`` — returns the absolute path to a bundled icon
  under ``azt_collab_client/ui/assets/icons/<name>.png`` (canonical
  location), falling back to ``assets/<name>.png`` for the legacy
  flat layout where ``gear.png`` currently lives. Returns ``''`` if
  the asset isn't bundled. Re-exported from
  ``azt_collab_client.ui.icon_path``.
- ``picker.py`` now resolves its default gear icon through
  ``icon_path('gear')`` (was a private ``_BUNDLED_GEAR`` constant
  with the same effect) so the discovery seam is reused for the
  next batch of shared icons.
- ``CLAUDE.md`` UI section documents the **client-first asset model**:
  shared-shape assets (gear, sync, share, future back/close glyphs)
  default to ``azt_collab_client/ui/assets/icons/`` so sister apps
  inherit them for free; peer-specific icons (recorder microphone /
  redo / app-icon variants) stay in the peer; existing recorder KV
  references with relative ``icons/<name>.png`` paths still work in
  the recorder's own cwd and don't need migrating.
- **Asset migration done:** ``sync_dark`` / ``sync_light`` /
  ``share_dark`` / ``share_light`` / ``gear_dark`` / ``gear_light``
  copied from ``azt_recorder/icons/`` into
  ``azt_collab_client/ui/assets/icons/``. ``icon_path('sync_dark')``
  etc. now resolve to the bundled package paths; the next sister-app
  peer (viewer, future clients) gets them with zero per-app work.
  The recorder's existing relative ``icons/<name>.png`` references
  still resolve in its own cwd, so this change is non-breaking;
  recorder-side migration to ``icon_path()`` can be opportunistic.

### azt_collab_client 0.17.0 — LiftHandle: cross-package LIFT-file access for peer apps
- New ``azt_collab_client.lift_io`` module exporting ``LiftHandle``
  and ``is_content_uri``. ``LiftHandle(path_or_uri).open_read()`` /
  ``.open_write()`` returns a binary file-like usable with
  ``ElementTree.parse`` / ``ElementTree.write`` regardless of
  whether the picker's emitted ``path`` is a filesystem path
  (desktop) or a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI (Android, new model). On the URI branch, opens via
  ``ContentResolver.openFileDescriptor`` and ``os.fdopen`` on the
  detached FD; the file owns the FD lifetime (close-on-exit
  through the context-manager protocol).
- Re-exported from the package root: ``from azt_collab_client import
  LiftHandle, is_content_uri``. Added to ``__all__``.
- **No caching layer** — every read/write hits the daemon's
  canonical copy through the provider. Lost-update protection
  relies on the daemon's serialization. The new
  ``azt_collab_client/CLAUDE.md`` "LIFT-file access" section
  spells out the migration checklist for peers (recorder first,
  viewer next): replace every ``open(lift_path)`` with
  ``LiftHandle(p).open_read() / .open_write()``; do not introduce
  a peer-side cache; do not compute sibling paths via
  ``os.path.dirname`` on a URI. Also documents the patterns NOT
  to use.

### azt_collab_client 0.16.0 — single source of truth for UI language; new public display_name + scan_catalog_languages
- ``set_language(lang)`` no longer takes a ``persist`` keyword argument.
  There is no transient mode any more — one preference, one store
  (``$AZT_HOME/config.json :: ui.language``), sticks everywhere until
  the next change. Internal apply-without-persist behaviour is now a
  private ``_apply(lang)`` used only by the auto-init-on-import path.
  Breaking change for callers that passed ``persist=False`` to apply a
  preference without rewriting the file; the new pattern is just
  ``set_language(language_pref())`` (idempotent re-write).
- New public ``i18n.display_name(code)`` — single source of truth for
  the language-code → human-name table. Peers that previously kept a
  parallel ``_DISPLAY_NAMES`` dict (the recorder's ``i18n.py`` did)
  now import this instead, eliminating the drift risk.
- New public ``i18n.scan_catalog_languages(locale_dir, domain)`` —
  walks ``<locale_dir>/<lang>/LC_MESSAGES/<domain>.{po,mo}`` and
  returns ``[(code, display_name), ...]``. Both the client's own
  ``available_languages()`` and the recorder's wrapper now share this
  shape, so peer catalogs and the client catalog enumerate
  identically.
- Updated callers: ``azt_collabd/ui/picker_app.py`` (build-time apply
  + mtime watcher) drop ``persist=False``; both calls just write the
  same value back, harmless because the mtime watcher's
  ``persisted == current_language()`` short-circuit prevents loops.

### azt_collabd 0.14.0 — content:// URIs across the picker boundary; clone auto-registers; MIN_CLIENT_VERSION → 0.17.0
- The picker (when running in the standalone server APK on Android)
  now emits a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI from ``_emit_and_quit``, instead of an absolute filesystem
  path inside the server APK's private ``filesDir``. The Intent
  carries the URI on its ``data`` field and adds
  ``FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION``
  so the calling peer can open the URI via
  ``ContentResolver.openFileDescriptor`` for the result delivery's
  lifetime.
- This removes a cross-package access bug (recorder's
  ``ElementTree.parse(path)`` raised ``[Errno 2]`` on an absolute
  path inside ``/data/user/0/org.atoznback.aztcollab/files/``,
  which the recorder's UID can't read). The provider's existing
  ``openFile`` callback (``_resolve_path`` under
  ``$AZT_HOME/projects/``) handles the URIs without changes —
  except for a leading-slash strip on ``Uri.getPath()`` so the
  path composes correctly. The single canonical copy in the
  daemon's ``$AZT_HOME`` stays the source of truth; peers don't
  cache.
- Successful clones now auto-register via
  ``projects.register(langcode, dest_dir, lift_path, remote_url)``.
  Previously a clone left the working tree on disk but no entry in
  ``projects.json``, so subsequent ``list_projects()`` /
  ``sync_project(langcode, …)`` calls couldn't find it. Failure
  is logged but doesn't fail the clone job — caller can re-register
  explicitly.
- ``MIN_CLIENT_VERSION`` raised to ``0.17.0`` because the URI shape
  is a hard contract: a peer bundling a pre-LiftHandle client would
  try to ``open()`` the URI as a path and crash on the spot. Old
  peers now get ``client_too_old`` from ``check_server_compat()``
  at startup with a clear "update this app" prompt.

### azt_collabd 0.13.21 — Bundle-based result extras to fix cross-package no_path loss
- Logcat showed the picker emitting a real lift_path
  (``/data/user/0/.../foo.lift``) via ``_emit_and_quit``, but the
  calling recorder still reported ``no_path`` — meaning the
  Intent's ``path`` extra was being lost across Android's IPC
  delivery to the peer process.
- Two likely culprits, both addressed by the same patch:
  1. ``Intent()`` with no action has been observed to drop extras
     on cross-package result delivery in some Android versions.
     The result Intent now carries the same action the recorder
     used for the request (``org.atoznback.aztcollab.PICK_PROJECT``).
  2. ``Intent.putExtra(String, String)`` is one of ~15 overloads;
     jnius's overload resolution can silently bind to a non-String
     overload when both args are CPython strings, leaving
     ``getStringExtra`` returning null on the peer side. Switched
     to the explicit, single-signature
     ``Bundle().putString('path', ...)`` →
     ``Intent.putExtras(Bundle)``.
- Added a diagnostic round-trip: the picker now reads the path
  back out of the result Intent (via ``getStringExtra``) right
  before ``setResult`` and prints it to logcat. If the verify
  print shows the path correctly but the recorder still gets
  ``no_path``, the loss is in Android's binder layer (genuinely
  rare); if the verify print is empty, the typed-Bundle approach
  also failed and we know to look further upstream.

### azt_collabd 0.13.20 — clone-flow diagnostic prints
- Added prints (to stderr → logcat ``python`` tag) at every step
  of the clone flow so the next ``no_path`` reproduction tells us
  exactly where the empty-path emission originates: ``clone worker
  starting``, ``clone returned`` (with ok / lift_path / error), the
  exception path, ``_after_clone_ok``, ``_after_clone_fail`` (with
  result codes), and ``load_lift`` (existing-project tap).
- Fixed a duplicate ``load_lift`` definition: a diagnostic-printing
  version was added without removing the original, and Python's
  last-def-wins on class bodies meant the diagnostic version was
  silently shadowed. Removed the second definition.

### azt_collabd 0.13.19 — debug bump for no_path triage
- Version-only bump so the user can verify on the picker's bottom
  strip (``server 0.13.19``) that the deployed build includes the
  ``_emit_and_quit`` empty-path guard from 0.13.15 and the
  Connect/Disconnect colour reactivity from 0.13.18.

### azt_collabd 0.13.18 — Connect/Disconnect button colour tracks connection state
- The four host action buttons (Connect / Disconnect GitHub,
  Set GitLab credentials / Disconnect GitLab) used to be statically
  green for Connect and dim for Disconnect. The visually-prominent
  button now matches the user's likely next action: Connect is
  green when not connected, Disconnect is green when connected.
  Dim button stays clickable so reconnect-to-refresh-tokens and
  similar flows still work — colour is a hint, not a gate.
- The colour swap is driven by ``refresh()`` reading
  ``credentials_status``, so the same round-trip that updates the
  status block also updates these buttons. Re-renders every time
  the screen is entered or the user taps Refresh Status.

### azt_collabd 0.13.17 — Refresh button rename + reposition
- ``Refresh`` button renamed to ``Refresh Status`` (more honest
  about what it does — it only re-pulls the credential / online
  read-out, doesn't do a sync) and moved to sit directly under the
  status block. Affordance for "I changed something in another
  window, pull the updated state" is now immediately adjacent to
  the data it refreshes, with no spacer in between.

### azt_collabd 0.13.16 — settings layout: status to the bottom, host rows compacted
- ``SettingsScreen`` reorder: actionable rows (interface language,
  contributor name, GitHub/GitLab connect+disconnect) at the top;
  the read-only ``Status`` block moved to the bottom. Users land
  here to do something, not to inspect — surfacing the controls
  first matches the visit pattern.
- GitHub and GitLab each collapsed from "section header + Connect
  row + Disconnect row" (3 rows of vertical real estate) into a
  single row with ``Connect…`` and ``Disconnect`` side-by-side.
  Brand name is implicit in the button text. About 100dp of
  vertical space recovered.

### azt_collabd 0.13.15 — server-owned contributor; auto-start device flow; loading-overlay wrap; empty-path guard
- New server-owned contributor field. ``store.get_contributor()`` /
  ``set_contributor(name)`` persist a display name to
  ``$AZT_HOME/config.json :: collab.contributor`` (sibling to
  ``ui.language`` — config, not credentials). New endpoints
  ``GET /v1/config/contributor`` and ``POST /v1/config/contributor``;
  client wrappers ``azt_collab_client.get_contributor`` and
  ``set_contributor``. ``store.get_status()`` now includes
  ``contributor`` so the settings UI gets it on the existing
  credentials-status round-trip. ``_h_project_sync``,
  ``_h_init_project``, ``_h_project_sync_async``, and
  ``scheduler._run_sync`` all route through new
  ``store.resolve_contributor(passed)`` which prefers the caller's
  explicit value, then the stored display name, then the
  ``'Recorder'`` fallback. Peers can stop carrying their own "Your
  name" preference; the suite has one source of truth on the server.
- ``SettingsScreen`` got a "Your name (appears in commits)"
  ``ThemedInput`` field with a transient "Saved." confirmation; the
  field auto-saves on focus loss. Refresh repopulates it from the
  server only when the user isn't actively editing.
- ``GitHubConnectScreen`` auto-starts the device flow on screen
  entry — no more "tap Begin to start" friction. The Begin button
  stays around as a Retry surface, re-enabled by the worker's
  failure path.
- ``picker_app._show_loading_overlay`` Label now wraps on width
  (same fix as ``_show_error``); long ``Cloning <url>...``
  messages no longer overflow both edges.
- ``picker_app._emit_and_quit`` refuses to emit an empty path. On
  Android an empty path lands at the peer's
  ``pick_project_android`` handler as ``RESULT_OK`` with no extra
  and surfaces as ``no_path``. If anything upstream tries it now,
  we log a stack trace to logcat and show an "Internal error:
  tried to return an empty path" modal so the user can pick again
  instead of bouncing back to the recorder with a cryptic failure.

### azt_collabd 0.13.14 — error-modal text wraps; auto-copy GitHub user_code
- ``picker_app._show_error`` Label was constructed with
  ``text_size=(None, None)``, which disables wrapping — long
  messages overflowed both edges of the modal. Now binds
  ``text_size`` to the Label's width so text wraps inside the
  modal panel; height stays free so the texture grows vertically
  as needed (modal's fixed height clips the bottom for very long
  messages, acceptable for typical 2–3-line errors).
- GitHubConnectScreen used to auto-copy the device-flow
  ``user_code`` to the clipboard so users could paste it into the
  GitHub device page without an extra tap; this regressed during
  the settings UI restyle. Restored the auto-copy and append the
  existing ``(code copied)`` translated suffix to the on-screen
  message when the copy succeeds (silently no-ops if Clipboard is
  unavailable, e.g. on a headless device).

### azt_collabd 0.13.13 — Android-aware ``azt_home()`` (and azt_collab_client mirror)
- ``[Errno 13] Permission denied: '/data/.local/share/azt/...'`` on
  every file op (template download, sync, etc.). p4a does not set
  ``$HOME``, so ``os.path.expanduser('~')`` resolved to ``/data`` —
  the Android system-data root, owned by ``root``, not writable by
  the app's UID. ``azt_home()`` then composed a path no app can
  write to.
- ``paths.py`` (both ``azt_collabd/`` and ``azt_collab_client/`` —
  duplicated by design) gained a ``_android_files_dir()`` helper
  that calls ``PythonActivity.mActivity.getFilesDir()`` via jnius
  and returns the app's private writable filesDir
  (``/data/user/0/<pkg>/files``). ``azt_home()`` checks that first
  on Android (after ``$AZT_HOME``, before XDG fallbacks). Desktop
  unchanged. The ``$AZT_HOME`` env-var override still wins for
  test rigs.

### azt_collabd 0.13.12 — settings Back button uses a glyph CharisSIL has
- ``SettingsScreen``'s "← Back" button used U+2190 (LEFTWARDS
  ARROW) which isn't in the CharisSIL glyph table — rendered as
  tofu under the project's default linguistic font. Swapped for
  ``«`` (U+00AB, left guillemet): present in every Latin font,
  reads as a back-pointer, and is the natural French equivalent
  too.

### azt_collabd 0.13.11 — preserve ``back_to`` across language-toggle screen rebuild
- ``_set_ui_language`` (settings UI) and ``_check_language_change``
  (picker subprocess) rebuilt the ScreenManager by recreating each
  screen with ``cls(name=name)``. That recipe loses any property
  the parent KV rule set on the *instance* (not the class) —
  notably ``back_to: 'picker'`` on ``SettingsScreen`` in
  ``picker_app._PickerRoot``. Symptom: the in-screen "← Back"
  button vanished the first time the user toggled language and
  didn't come back when toggling to English.
- Both rebuild loops now capture ``back_to`` per-screen before the
  ``clear_widgets`` and re-apply after instantiation. Generic
  enough to extend to other instance-level properties later.

### azt_collabd 0.13.10 — Android back button on picker subscreens
- Hardware back / gesture on Android does not flow through
  ``App.on_request_close`` (which only fires for the desktop X
  button). It surfaces as ``key 27`` on ``Window.on_keyboard``.
  Without an explicit binding, Kivy's default for an unhandled key
  is ``App.stop`` — so back from settings / github / gitlab /
  langpicker was closing the picker activity entirely.
- ``PickerApp.on_start`` now binds ``Window.on_keyboard`` to a new
  ``_on_back_button`` handler that delegates to ``_navigate_back``
  (extracted from the existing ``on_request_close`` logic). On a
  non-picker screen, back navigates to the screen's ``back_to``
  property (or ``'picker'`` by default); on the picker itself, back
  falls through to the normal ``RESULT_CANCELED + finish()`` exit.
  Same screen-pop dance the recorder uses.

### azt_collabd 0.13.9 — full traceback on template-download failures
- ``_h_create_project_from_template`` was masking the original
  failure type by catching ``Exception`` and returning only
  ``str(ex)``. On the device a ``PermissionError`` surfaced as
  ``provider HTTP 500: [Errno 13] Permission denied`` with no path
  or call site. Now logs the full traceback to stderr/logcat
  (``adb logcat | grep -i from_template``) and includes
  ``traceback`` and ``ExceptionType`` in the response body so the
  caller (picker / recorder) can surface them.
- Confirms in code comments that the template download is an
  anonymous public HTTPS GET — no GitHub credentials consulted.

### azt_collabd 0.13.8 — drop "Active host" toggle from settings UI
- ``SettingsScreen`` no longer renders the "Active host" SectionLabel
  + GitHub/GitLab two-button row. URL-based credential routing
  (``store.get_sync_credentials(url)`` → ``host_for_url(url)``)
  has handled every common case since 0.12.0; the toggle was
  vestigial. ``choose_host`` method dropped from ``SettingsScreen``.
  ``set_collab_host`` server endpoint and client wrapper stay around
  for wire compat (peers still calling them are safe; the value
  silently affects only the self-hosted/unknown-URL fallback path
  through ``get_collab_host()``).
- The eventual "Publish" flow for new local-only projects will
  pick credentials by inspecting which hosts have stored creds,
  prompting the user only when more than one is configured —
  rather than reading a global "active host" preference. Captured
  here so future-me doesn't reintroduce the toggle.

### azt_collabd 0.13.7 — locale files packaged in server APK
- ``server_apk/buildozer.spec`` ``source.include_exts`` was
  ``py,xml,gz,png``, silently dropping the ``.po``/``.mo`` files
  under ``azt_collab_client/locales/`` at packaging time. On the
  device, ``available_languages()`` walked an empty locale tree and
  the settings UI's language toggle only offered English regardless
  of which catalogs lived in the source tree. Added ``po,mo`` to
  the extension list. Pre-compile any language's ``.mo`` before
  rebuilding so the catalog ships pre-baked (faster first paint;
  also dodges any APK-readonly issue with the runtime
  ``_ensure_mo``):
  ``python -c "from azt_collab_client.i18n import _ensure_mo;
  _ensure_mo('fr')"``.

### azt_collabd 0.13.6 — typing_extensions in APK requirements; BodyLabel recursion fixed at class level
- Added ``typing_extensions`` to the server APK's ``requirements``
  in ``server_apk/buildozer.spec``. dulwich (and a few of its
  transitive imports) reach for ``typing_extensions`` at runtime;
  on Android it isn't pulled in by default. Previously a clone
  attempt would fail with ``ImportError: no module named
  typing_extensions`` at the moment dulwich tried to do its first
  network operation. Adding the recipe to requirements puts it on
  the APK's PYTHONPATH. **Requires a clean build**
  (``buildozer android clean && buildozer android debug deploy``)
  for p4a to pick up the new recipe.
- Promoted the ``text_size: self.width, None`` fix from
  per-instance overrides on three ``BodyLabel`` uses to the
  ``<BodyLabel@Label>`` class rule itself. Any ``BodyLabel`` whose
  ``height: self.texture_size[1] + dp(8)`` would otherwise loop
  with ``text_size: self.size`` is now safe by default. The earlier
  per-instance overrides remain (redundant but harmless).

### azt_collabd 0.13.5 — picker version-probe diagnostics
- ``picker_app._probe_server_version`` now surfaces *why* the probe
  failed when it can't show a real server version. Instead of a bare
  ``server ?``, the bottom strip renders one of
  ``server ? (server_unreachable)`` /
  ``server ? (server_too_old)`` /
  ``server ? (client_too_old)`` /
  ``server ? (<ExceptionType>: ...)``. Distinguishes transport down
  vs. version-handshake reject vs. RPC exception without needing
  ``adb logcat``. Also prints a one-line diagnostic to stderr/logcat
  so the post-mortem is in both places.

### azt_collabd 0.13.4 — debug version bump
- No code change. Version-only bump so a freshly-rebuilt server APK
  reports a different ``__version__`` from the previous build,
  letting the user verify on the picker's bottom strip
  (``client X · server Y``) that the device is actually running the
  new build vs a cached install.

### azt_collabd 0.13.3 — picker shows both versions, auth-fallback for clone failures
- Picker bottom strip now shows ``client X · server Y``. The server
  half is fetched off the UI thread via
  ``check_server_compat()``; renders ``server ?`` if the daemon is
  unreachable. Was: client only.
- ``_after_clone_fail`` got a fallback path: when the daemon's
  worker didn't run far enough to attach ``CLONE_AUTH_REQUIRED``
  (e.g. the clone-job kickoff itself failed and the result is None)
  but the error string smells like auth (401 / 403 / 404 /
  unauthorized / forbidden / not found / authentication /
  credential), the auth-prompt modal still appears with the **Open
  settings** button. Same heuristic the daemon uses, mirrored
  client-side for the result-is-None case.
- Auth-modal "Open settings" button now calls ``self.go_config()``
  (in-process screen swap) instead of the removed
  ``open_server_ui`` import. The Android Intent dance is gone from
  this flow entirely.

### azt_collabd 0.13.2 — picker hosts settings screens in-process
- Picker's gear used to call ``azt_collab_client.open_server_ui()``,
  which on Android fires ``getLaunchIntentForPackage`` on the server
  APK. Because the server APK has a single ``PythonActivity`` already
  running the picker, Android collapsed the task back to the calling
  peer (the recorder) instead of switching to settings — there was
  no path forward from the picker.
- ``azt_collabd/ui/app.py`` now exposes a top-level
  ``register_kv(font_name)`` (idempotent) that loads the settings/
  GitHub/GitLab KV. ``CollabUIApp.build`` calls it; the picker_app
  also calls it before its own KV so all class rules are in scope.
- ``picker_app._PickerRoot`` ScreenManager now carries
  ``SettingsScreen`` (with ``back_to: 'picker'``),
  ``GitHubConnectScreen``, and ``GitLabFormScreen`` alongside the
  existing ``ProjectPickerScreen`` / ``LangPickerScreen``. ``go_config()``
  is now ``self.sm.current = 'settings'`` — no Intent, no subprocess.
  Same code path on desktop and Android.
- New ``go(name)`` method on ``PickerApp`` mirrors ``CollabUIApp.go``
  so the existing settings-side KV (``app.go('github')``,
  ``app.go('gitlab')``, ``app.go('settings')``) just works in both
  hosts.
- New ``back_to`` ``StringProperty`` on ``SettingsScreen``. When set
  (the picker_app ``_PickerRoot`` KV sets it to ``'picker'``) the
  screen renders an additional **← Back** ``NavBtn`` at the top of
  the layout. Hidden / disabled in the standalone settings host
  where back has no meaning. The Android back gesture / window-close
  on non-picker screens is also intercepted by ``on_request_close``
  to navigate back instead of exiting.
- Known limitation: a peer calling ``open_server_ui()`` *while a
  picker is already up* still hits the Android launch-flag bug
  (settings doesn't appear; task may collapse to the peer). Rare in
  practice and not worth a separate ``SettingsActivity`` declaration
  yet — tracked as future work.

### azt_collabd 0.13.1 — settings UI Clock-iteration warning fix
- ``BodyLabel`` instances that combined ``text_size: self.size``
  (inherited) with ``height: self.texture_size[1] + dp(8)`` were
  forming a feedback loop on Android: texture_update changes height
  → parent BoxLayout do_layout → child resize → text_size changes →
  texture_update fires. Tolerable before; pushed past Kivy's
  per-frame Clock iteration limit by the new language-toggle row
  and the wrapping of every settings-UI string in ``_(...)``.
  Surgical fix on the three offending BodyLabels (status_label,
  gh_message, the gitlab-form intro): override
  ``text_size: self.width, None`` so the wrap width is bound but
  height flows from content alone, breaking the cycle.

### azt_collabd 0.13.0 — settings UI translatable, language toggle, picker live retranslation
- ``SettingsScreen`` gained an **Interface language** row at the top
  with one button per ``azt_collab_client.i18n.available_languages()``.
  Selecting a language calls ``i18n.set_language(code)`` (which
  persists to ``$AZT_HOME/config.json`` under ``ui.language``) and
  rebuilds every screen in the manager so KV ``text: _('...')``
  bindings re-evaluate against the new catalog. Same dance the
  recorder's ``ConfigScreen`` uses.
- Every visible string in ``azt_collabd/ui/app.py`` (Settings, GitHub
  device-flow, GitLab form) is now wrapped in ``_(...)``. KV imports
  ``_ azt_collab_client.translate.tr`` so subsequent
  ``set_translator``/language switches take effect.
- ``picker_app.py`` watches ``$AZT_HOME/config.json`` mtime once a
  second (Clock interval). When the persisted language changes — for
  example because the user just toggled it in a settings subprocess
  opened from the gear — the picker rebuilds its screens in place.
  The user sees the picker live-retranslate without restart.
- Apply persisted language at picker / settings startup so first
  paint is in the right language.

### azt_collabd 0.12.1 — picker gear wired to settings, both versions on settings page
- Standalone picker (``python -m azt_collabd projects``) now shows
  the settings gear in the top-right and wires it to the daemon's
  settings UI via ``open_server_ui()`` instead of the previous no-op
  stub. Rationale: first-time users land on the picker and need a
  visible path to authentication; previously they had to fail a
  clone before the auth-prompt modal offered one.
- Settings UI (``python -m azt_collabd ui``) now displays both the
  client and server versions in the bottom version strip:
  ``client 0.14.1  ·  server 0.12.1`` — used to show only the
  daemon version. The settings UI subprocess imports
  ``azt_collab_client`` for ``__version__``.
- Both version labels (settings + picker) bumped from
  ``font_size: sp(11) / color: T.TEXT_FAINT`` to
  ``sp(13) / T.TEXT_DIM`` so they're legible without straining.

### azt_collabd 0.12.0 — URL-based credential routing, CLONE_AUTH_REQUIRED, MIN_CLIENT_VERSION handshake
- New ``azt_collabd.MIN_CLIENT_VERSION = '0.14.0'`` — floor on the
  ``azt_collab_client`` version this daemon will talk to. ``/v1/health``
  now publishes ``min_client_version`` alongside ``version``. Symmetric
  to the existing client-side ``MIN_SERVER_VERSION``: when a peer ships
  a too-old client, the peer's startup handshake gets ``client_too_old``
  and can prompt the user to update. Bump this constant in lockstep
  with any wire-format addition that older clients can't decode (e.g.
  the new ``CLONE_AUTH_REQUIRED`` status added in this release).
- ``store.get_sync_credentials()`` now takes an optional remote URL and
  picks credentials by host (``github.com`` → GitHub creds,
  ``gitlab.com`` → GitLab creds), falling back to the user's saved
  ``collab_host`` only when the URL is unrecognized (self-hosted /
  empty). All call sites — ``_h_init_project``, ``_h_clone_project``,
  ``_h_project_sync`` in ``server.py`` and ``_run_sync`` in
  ``scheduler.py`` — pass the remote URL. Symptom this fixes: the user
  picks a LIFT project without first visiting Settings; the daemon
  used to send GitHub creds to a GitLab remote (or vice-versa) just
  because ``collab_host`` was the wrong value.
- New helper ``store.host_for_url(url)`` exposes the URL → host
  classifier.
- New status code ``CLONE_AUTH_REQUIRED`` (with ``host`` param). The
  clone worker now appends it after final failure when either (a) no
  token was available for the URL's host, or (b) the dulwich error
  contains 401/403/404/auth keywords. The picker UI uses this to
  branch into an auth-prompt modal instead of a generic error.
- The auth-shaped retry already in ``_clone_worker`` was extracted to
  ``_clone_error_looks_like_auth(result)`` and now also recognises
  ``not found`` / 404 (private-repo case) — previously only matched
  ``credential`` / ``auth``, so a 404-bearing failure skipped the
  anonymous retry.

### azt_collab_client 0.15.2 — Android ContentProvider path delivery fix
- **Critical bug fix.** The Android transport
  (``azt_collab_client/transports/android_cp.py``) built a URI like
  ``content://<authority><path>`` and called
  ``ContentResolver.call(uri, method, None, extras)``. But the
  ``call(Uri, method, arg, extras)`` overload only delivers
  ``method``, ``arg``, and ``extras`` to
  ``ContentProvider.call(method, arg, extras)`` — the URI's path is
  consumed by provider routing and never reaches the dispatch.
- AZTCollabProvider.java reads ``arg`` as the path
  (``cb.dispatch(method, arg != null ? arg : "", body)``), so on the
  daemon side every RPC was being dispatched with ``path=""``,
  producing ``{ok: False, error: 'not_found'}``. User-visible
  symptom: every clone / list / sync / credential RPC silently
  routed to the dispatcher's catch-all 404 branch.
- Fix: pass the dispatch path as ``arg`` instead of None. The URI
  shrinks to just the authority (no path component) since it's only
  used for provider routing now. One-line change at the call site.
- This was a long-standing bug that likely went unnoticed because
  legacy peers symlinked ``azt_collabd`` and used the loopback
  transport (Python interpreter in-process). Peers on the new
  ContentProvider-only model would have hit it on every non-ping
  call.

### azt_collab_client 0.15.1 — picker gear icon bundled in package
- Picker KV used to reference the gear icon as a relative
  ``'icons/gear.png'`` path, which only resolves when the host's
  cwd happens to contain that file (worked from the recorder repo
  root, broke everywhere else — most visibly in the standalone
  picker subprocess, where Kivy fell back to its missing-image
  texture and the gear rendered as a white square).
- The icon is now an absolute path computed from the package
  location: ``azt_collab_client/ui/assets/gear.png``. The KV
  template injects it at ``register_kv`` time alongside ``font_name``.
- ``register_kv`` (a.k.a. ``register_picker_kv``) gained an optional
  ``gear_icon=`` kwarg so hosts that want a custom icon can pass
  one explicitly (the recorder still ships its own at
  ``azt_recorder/icons/gear.png``); default falls back to the
  package-bundled file.
- **Important**: the binary file
  ``azt_collab_client/ui/assets/gear.png`` is not committed by this
  change; copy from any peer that already has one
  (e.g. ``cp /home/kentr/bin/AZT/azt_recorder/icons/gear.png
  azt_collab_client/ui/assets/gear.png``).

### azt_collab_client 0.15.0 — own i18n domain (azt_collab_client.po), pure-Python msgfmt, fallback chain in translate.tr
- New module ``azt_collab_client.i18n`` owns gettext domain
  ``azt_collab_client``. Public API:
  ``set_language(lang, persist=True)``, ``current_language()``,
  ``available_languages()``, ``language_pref()``, ``_(msg)``,
  ``gettext_translation()``. Persists the active language to
  ``$AZT_HOME/config.json`` under ``ui.language``; auto-applies that
  preference at import so all suite subprocesses converge on the same
  language without a coordination channel.
- New locale tree
  ``azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po``
  with French translations of all client-owned strings: picker UI,
  langpicker, popups, ``translate.py`` status messages (the full
  ``S.*`` set), and the settings-UI strings now owned by the client.
- ``i18n.py`` ships a pure-Python PO→MO compiler (msgfmt-lite — single
  magic, sorted msgid array, two parallel offset tables packed via
  ``struct``). Runs lazily on first ``set_language`` whenever the
  ``.mo`` is missing or older than the ``.po``. So peers that ship
  only the ``.po`` (or contributors editing translations in-place) do
  not need a build-time ``msgfmt`` step.
- ``translate.py`` default translator changed from "try
  ``from i18n import _`` (recorder)" to "use the client catalog
  directly". ``set_translator(host_tr)`` overrides as before, but
  ``tr(msg)`` now falls **back** to the client catalog whenever the
  host translator returns ``msg`` unchanged. The fallback layer means
  embedded peers (the recorder) do not need to duplicate client
  strings into ``aztrecorder.po``: a string the recorder catalog
  doesn't know falls through to ``azt_collab_client.po``. Owns its
  own strings, no duplication, gettext-canonical.
- Behavior change to be aware of: hosts that previously relied on
  the implicit ``from i18n import _`` fallback now get the client
  catalog first. Hosts with their own catalogs should keep calling
  ``set_translator(host._)`` at startup; the new fallback in
  ``tr()`` handles client strings transparently.

### azt_collab_client 0.14.1 — picker version label clarified
- Picker bottom-strip version label changed from ``collab X.Y.Z`` to
  ``client X.Y.Z`` so users can tell client and server versions
  apart at a glance (the settings page shows both).
- ``ProjectPickerScreen`` version label sized up:
  ``font_size: sp(13)`` / ``color: T.TEXT_DIM`` (was
  ``sp(11)`` / ``T.TEXT_FAINT``). Same change applied in
  ``azt_collabd/ui/app.py`` for consistency.

### azt_collab_client 0.14.0 — auth-prompt modal on clone failure, client_too_old handshake
- ``check_server_compat()`` gained a third branch: when the server's
  ``min_client_version`` is greater than this client's ``__version__``,
  the function now returns
  ``{'ok': False, 'error': 'client_too_old', 'client_version', 'server_version', 'min_required'}``.
  Mirrors the existing ``server_too_old`` shape so peer apps can branch
  on the same dict. Forward-compatible with pre-0.12.0 daemons that
  don't publish ``min_client_version`` (treated as "no floor").
- New status mirror ``S.CLONE_AUTH_REQUIRED`` and translation:
  *"Clone failed — repository not found. This may be a private
  repository. Are you authenticated to {host}?"* (host is rendered
  Title-cased: GitHub / Gitlab).
- ``azt_collabd/ui/picker_app.py`` clone-fail flow now threads the
  daemon's ``Result`` through ``_after_clone_fail``. When the result
  carries ``CLONE_AUTH_REQUIRED``, the modal renders the translated
  prompt and an extra **Open settings** button that calls
  ``azt_collab_client.open_server_ui()``. Previously the user saw a
  bare *"Clone failed: not found"* and had no path forward — they
  would not have visited Settings before picking a project, so we
  lead them there.
- ``_show_error`` grew an optional ``extra_button=(label, callback)``
  argument so the same modal helper can host either a single Dismiss
  button (existing behavior) or a two-button row.

### azt_collabd 0.11.0 — settings UI restyle, picker typography fix
- The standalone settings UI (``python -m azt_collabd ui`` /
  launcher activity in the server APK) was using stock Kivy buttons
  with no theme, no ``font_name``, and no top bar. It looked
  unrelated to the recorder. Restyled to mirror the recorder's
  ``CollabScreen``: themed top bar (``T.SURFACE`` background,
  ``T.ACCENT`` bold title), ``BG``-painted screens, ``SectionLabel``
  / ``HeaderLabel`` / ``BodyLabel`` / ``DimLabel`` dynamic classes,
  ``ThemedInput`` for text fields, ``RecBtn`` for primary actions,
  ``NavBtn`` for secondary navigation. The host toggle now highlights
  the active host with ``T.GREEN`` (was: a disabled stock button).
- The standalone picker (``picker_app.py``) was rendering its
  ``RecBtn`` with raw ``font_size: 16`` (un-scaled pixels — tiny on
  hi-dpi phones), no ``font_name``, and a hardcoded blue
  ``(0.2, 0.6, 1, 1)`` instead of a theme colour. Replaced with the
  recorder's idiom: ``font_size: sp(16)``, ``font_name: FONT``,
  ``normal_color: T.ACCENT``. The error / loading modal overlays
  were also unstyled stock widgets; now ``T.SURFACE`` rounded
  panels with ``T.TEXT`` labels and a themed dismiss button.
- Both apps now call ``register_charis()`` from the new shared
  helper at startup. If the CharisSIL TTFs can be located (the
  recorder's ``fonts/`` dir during desktop dev, system font dirs on
  Linux, or a future ``azt_collab_client/fonts/`` location) they
  register under LabelBase name ``CharisSIL``; otherwise the apps
  fall back to ``Roboto`` silently. The standalone server APK
  doesn't currently bundle the TTFs (~20 MB), so on-device it falls
  back to Roboto — sizes and theme are aligned, glyphs are not.
  Bundling the fonts in the server APK is a follow-up.

### azt_collab_client 0.13.6 — shared CharisSIL helper, larger picker logo
- New ``azt_collab_client.ui.fonts.register_charis()`` (re-exported
  as ``azt_collab_client.ui.register_charis``). Discovers
  CharisSIL-Regular/Bold/Italic/BoldItalic TTFs across a small list
  of likely locations (canonical client ``fonts/`` slot, sibling
  recorder ``fonts/`` dir, system font dirs) and registers them
  under LabelBase name ``CharisSIL``. Returns the LabelBase name to
  use (``'CharisSIL'`` or ``'Roboto'``); idempotent.
- ``ProjectPickerScreen`` typography: logo grew from ``dp(200)`` to
  ``dp(240)``, title from ``sp(28)`` to ``sp(32)``, subtitle from
  ``sp(16)`` to ``sp(18)``. Logo gets explicit
  ``allow_stretch / keep_ratio: True`` so the larger size doesn't
  pixelate. Title / subtitle now centred with explicit
  ``halign: 'center'`` + ``text_size: self.size`` so they don't
  drift left in narrow layouts.

### azt_collab_client 0.13.5 — open_server_ui dispatches on Android + shared install popup
- ``open_server_ui()``'s docstring has long promised that on Android
  it would dispatch an Intent to the server APK's launcher activity.
  It now does. New ``_open_server_ui_android`` resolves
  ``PackageManager.getLaunchIntentForPackage('org.atoznback.aztcollab')``
  and starts the activity. Returns
  ``{'ok': True, 'launched': 'android-apk'}`` on success.
- If the APK isn't installed, the helper opens a new install-prompt
  popup (``ui.popups.install_server_apk_popup``) and returns
  ``{'ok': False, 'error': 'server_apk_not_installed', 'prompted': True}``
  so the caller knows the popup is on screen. The popup itself
  routes through ``Intent.ACTION_VIEW`` on Android and
  ``webbrowser.open`` on desktop, both pointed at
  ``SERVER_APK_INSTALL_URL``.
- New optional ``on_status`` callback on ``open_server_ui`` /
  ``install_server_apk_popup``: the popup uses it to surface
  "could not open install page — …" failures into the host's
  status bar without the host having to reach into the popup
  internals. Sister apps that don't pass ``on_status`` still work.
- ``ui.popups`` and ``ui/__init__`` export
  ``install_server_apk_popup`` so peers can also call it directly
  (e.g. from a startup-time ``ServerUnavailable``-handling path,
  not just from the settings button).
- Decouples sister apps from per-app reimplementations of
  "launch APK / show install prompt" and lets the viewer collapse
  ~60 lines of jnius / popup boilerplate.

### azt_collabd 0.10.6 — pin AZTCollabProvider callback proxies
- **Bug fix:** ``android_cp/service.install_callbacks`` was passing
  freshly-constructed ``_Dispatch()`` / ``_OpenFile()`` PythonJavaClass
  instances inline to ``Provider.registerCallbacks``. Java held refs;
  Python did not. After a GC cycle (typically within seconds of the
  picker Activity launching) the proxy instances were freed, and the
  next binder-thread call from a peer's ContentResolver into
  ``AZTCollabProvider.call`` dereferenced the dead type object. Net
  effect on hardware: ``Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR),
  fault addr 0x143`` on Thread-3, with backtrace through
  ``NativeInvocationHandler.invoke`` → ``_PyObject_GenericGetAttrWithDict``
  → ``_PyType_Lookup``. From the peer's perspective the picker
  Activity vanished and the recorder logged
  ``[activity-result] code=18247 result=0`` (default RESULT_CANCELED
  from the killed Activity).
- Fix: store strong refs to both proxies at module scope before
  handing them to Java. Validate with a clean rebuild
  (``buildozer android clean && buildozer android debug``).

### azt_collab_client 0.13.4 — gear icon source conditional
- The picker KV had an unconditional ``source: 'icons/gear.png'`` even
  when the host passed ``hide_settings_gear=True`` (size goes to 0×0
  in that case but Kivy still tries to resolve the source). The
  standalone picker app and any sister app that hides the gear was
  logging ``[ERROR ] [Image ] Not found <icons/gear.png>`` on every
  picker open. Cosmetic, not the cause of the segfault — but the
  standalone server APK has no ``icons/`` dir of its own so the line
  was particularly visible there.
- Fix: gate the source on the same ``{show_gear}`` template flag the
  size already uses (``source: 'icons/gear.png' if {show_gear} else ''``
  in ``azt_collab_client/ui/picker.py``).

### azt_collabd 0.10.5 — server APK ships non-Python assets
- **Bug fix:** `server_apk/buildozer.spec` had `source.include_exts =
  py,xml`, which silently stripped `azt_collab_client/ui/assets/
  langtags_mini.json.gz` and `azt_collab_client/azt.png` from the
  packaged APK. Net effect: tapping **Start New** in the picker
  Activity raised `FileNotFoundError: ... langtags_mini.json.gz` from
  `LangPickerScreen._load_langtags`, which crashed the Activity; the
  Activity then finished without an explicit `setResult(-1, ...)` so
  peers saw `[activity-result] code=18247 result=0` (RESULT_CANCELED
  for `_AZT_PICK_REQ_CODE`). The same was true for any flow that
  reaches the langpicker, including the path between Clone-Internet
  and the post-clone confirmation when the user backs out via the
  langpicker.
- Fix: extend `source.include_exts` to `py,xml,gz,png`. `gz` covers
  the langtags blob; `png` covers the suite icon used by `App.icon`
  in `azt_collabd/ui/picker_app.py`. Validate with a clean rebuild
  (`buildozer android clean && buildozer android debug`) — hot
  patches into `.buildozer/.../build/` only prove the patch *content*
  is right, not that the spec change actually fires.

### Suite naming convention
- Adopted a single rule for surfacing the "azt collab" / "azt
  recorder" names across systems with incompatible identifier rules
  (Python identifiers forbid `-`, Android package segments forbid
  `-`, GitHub App slugs forbid `_`). Documented in `CLAUDE.md`. Net:
  `_` is the default for code identifiers, env vars, repo dirs, and
  Python packages; the dropped form (`aztcollab`) is the Android
  package; the hyphenated form (`azt-collaboration`) is the GitHub
  App slug; the human-facing form is "AZT Collaboration" (same word
  in French and English, which keeps i18n natural for the SIL user
  base). The internal token "collab" stays in code (`azt_collabd`,
  `AZT_COLLAB_ACCESS`, `org.atoznback.aztcollab`) — only the
  human-facing surfaces and the GitHub App slug expand to
  "Collaboration" / "azt-collaboration".
- Default `app_slug` in `azt_collabd/config.py` is now
  `azt-collaboration` (was `azt-recorder`, an artifact of the
  recorder's earlier ownership of the GitHub App). Override via
  `AZT_GITHUB_APP_SLUG=` if your suite ships under a different
  registration. The same default is mirrored in
  `server_apk/main.py` and the README env-var table.
- Buildozer title ("AZT Collaboration") and prose mentions ("AZT
  Collaboration service") capitalized consistently across the tree.

### Android manifest assembly
- **Bug fix:** `buildozer.spec`'s manifest-extras key is
  `android.extra_manifest_xml`, NOT `android.manifest_extra_xml`
  (different word order). Buildozer silently ignores unknown keys,
  so before this fix every peer's extras file was being dropped on
  the floor — including the recorder's `<queries>` block, which on
  Android 11+ is required for the client's `discover()` probe to
  even see the server APK.
- New canonical peer manifest extras at
  `android/manifest_extras_peer.xml` (just the suite `<queries>`
  block; meant to be symlinked into each peer as
  `manifest_extras.xml` and referenced from buildozer.spec).
- `server_apk/manifest_extras.xml` rewritten to top-level-only
  content (just the signature `<permission>`). p4a's
  `extra_manifest_xml` injects at top level under `<manifest>`,
  before `<application>` — `<application>` wrappers there end up as
  duplicate-application errors.
- New `before_apk_assemble` step in
  `buildozer_tweaks/p4a_hook.py`:
  `_inject_aztcollab_provider`. Patches the rendered
  `AndroidManifest.xml` post-template-render to inject the
  `<provider>` declaration inside `<application>`. Gated on
  `dist_name == 'aztcollab'` so peer APKs don't accidentally
  inherit the provider declaration. (p4a's SDL2 bootstrap manifest
  template only exposes top-level injection, so the provider — which
  must live inside `<application>` — has no spec-level injection
  point.)

### Build infrastructure (NDK r29 compatibility)
- New local recipe override `recipes/sdl2_ttf/__init__.py` in
  `buildozer_tweaks/`: patches harfbuzz's `Android.mk` to add
  `-DHB_NO_PRAGMA_GCC_DIAGNOSTIC_ERROR -Wno-error=cast-function-type-strict`.
  SDL2_ttf 2.20.2 ships an old harfbuzz whose `hb.hh` promotes
  `-Wcast-function-type` to error via `#pragma GCC diagnostic`, and
  NDK r29's clang lumps the `-strict` variant into that group;
  `hb-ft.cc`'s `(FT_Generic_Finalizer)` casts then fail.
- New `recipes/kivy/__init__.py`: (a) gates kivy's
  `merge(flags, sdl2_flags)` on `not kivy_sdl2_path` so host
  pkg-config doesn't leak `-I/usr/include/SDL2` (and via that
  `sys/cdefs.h`) into the cross-compile; (b) adds
  `-Wno-error=incompatible-function-pointer-types` to CFLAGS so
  kivy 2.3.0's `cgl_gl.c` glShaderSource const-mismatch doesn't
  fail the build.
- All build patches now run from `prebuild_arch` / `before_apk_*`
  hooks — the previous `before_apk_build` placement of the harfbuzz
  and kivy patches in `p4a_hook.py` was dead code (it fires after
  `build_recipes`, way too late). Those legacy `_patch_*` functions
  in the hook are kept as harmless no-ops; safe to remove on a
  future cleanup.

### Server APK packaging
- New `server_apk/setup.sh`: idempotent symlink creator for
  `azt_collabd` and `azt_collab_client` from the parent repo into
  the server APK source dir. Run once after a fresh checkout so
  buildozer can find the daemon code. Replaces the dangling
  `../setup_from_nuke.sh` reference that used to live in
  `main.py`'s comments.
- `server_apk/buildozer.spec` now points at the proper
  `extra_manifest_xml` key and includes icon assets
  (`icon.filename`, `icon.adaptive_*`) so the launcher icon isn't
  the default Kivy logo.

### azt_collabd 0.10.3 — manifest dual-patch + on-device verification

- `_inject_aztcollab_provider` and `_inject_aztcollab_pick_intent`
  in `buildozer_tweaks/p4a_hook.py` now patch BOTH
  `AndroidManifest.xml` (the dist root) AND
  `src/main/AndroidManifest.xml` (the file gradle's default
  sourceSets actually reads). Previously patched only the dist
  root, so gradle ran against the unpatched copy and the resulting
  APK had no `<provider>` despite the dist-root manifest on disk
  looking correct. Symptom: dumpsys showed no provider yet
  `aapt dump xmltree` of the *dist root* manifest confirmed it —
  diverging because gradle's input was a different file.
- New `server_apk/test_install.sh` — 15-check on-device
  verification of the server APK: install, `<permission>`
  declaration, signature self-grant, `<provider>` registration
  (multi-source: per-package dumpsys, system-wide provider table,
  `pm dump`), direct `content query`, bundled
  `azt_collabd`/`azt_collab_client` Python modules, launcher icon
  vs. default Kivy logo, activity launches without crash, source
  symlinks, dist manifest sentinel, hook traces, installed-vs-bin
  APK md5 match, APK's own manifest, all dist manifests' patch
  status, gradle manifest config.
- New `azt_collab_client/test_peer.sh` — peer-side verification:
  walks each `org.atoznback.*` package on the device, confirms
  each requests `AZT_COLLAB_ACCESS`, was granted (signature match),
  declares the suite `<queries>` block, and signs against the
  fingerprint in `android/SUITE_FINGERPRINT`.

### azt_collabd 0.10.1 — naming default + build/manifest plumbing
- Default `_SLUG_DEFAULT` in `config.py` flipped from `'azt-recorder'`
  to `'azt-collaboration'` to match the renamed GitHub App slug.
  Same default mirrored in `server_apk/main.py`.
- All cross-cutting build / manifest / naming work above
  (NDK r29 patches, manifest assembly fix,
  `_inject_aztcollab_provider` hook, `setup.sh`) lands in this
  patch level — no daemon API change, but the server APK packaging
  pipeline becomes reliable for the first time.

### azt_collabd 0.10.0 — picker helper subprocess
- New `python -m azt_collabd projects` entry point in `__main__.py`.
  Runs `azt_collabd.ui.picker_app.PickerApp`, a single-purpose Kivy
  app that hosts the shared `ProjectPickerScreen` +
  `LangPickerScreen` and implements the create-flow callbacks
  (`open_file` / `clone_dialog` / `show_start_over` /
  `new_from_template`) internally. Every successful flow ends in
  `_emit_and_quit(path, langcode='')`, which writes
  `AZT_PICK\t<path>\t<langcode>\n` on stdout and exits 0
  (or sets the Activity result on Android — see server APK below).
  Cancel / window-close exits 1.
- New `azt_collabd/ui/picker_app.py`. Hides the gear icon on the
  shared picker (no settings of its own), mounts the langpicker for
  Start New, drives `clone_project` and `create_project_from_template`
  on worker threads, surfaces errors via an in-window modal overlay
  (window stays open for retry).
- `azt_collabd/ui/app.py` (settings UI) trimmed: no more "Projects"
  NavBar button, no `ProjectPickerScreen` mount, no host-contract
  stubs. App now uses `_AZT_ICON` from `azt_collab_client/azt.png`.
- `server_apk/main.py` reads the launching Intent action; if it's
  `org.atoznback.aztcollab.PICK_PROJECT`, mounts the picker app
  instead of the settings UI. Same `PythonActivity` handles both —
  no second Activity declaration in the manifest required (the
  Intent action is matched by the existing PythonActivity entry +
  the new `<intent-filter>` line described in
  `azt_recorder/docs/p4a_hook_picker_intent.diff`).

### azt_collab_client 0.13.3 — pick_project UI-thread JNI dispatch
- Bug fix: `_pick_project_android` was building the
  `ActivityResultListener` proxy on the worker thread that called
  `pick_project()`. Worker threads attached by jnius' thread hook
  don't carry the app `ClassLoader`, so
  `find_javaclass('org/kivy/android/PythonActivity$ActivityResultListener')`
  fell back to the system loader (which has no app inner classes)
  and raised `JavaException: ClassNotFoundException`. Net effect:
  the recorder fired the `PICK_PROJECT` Intent, the worker thread
  died before `startActivityForResult` ever ran, the recorder's
  blocking modal stayed up forever showing only "Pick a project to
  continue. Cancel" with no picker Activity behind it.
- Fix: dispatch all JNI work (autoclass lookups, Intent build,
  `resolveActivity`, `bind(on_activity_result=...)`,
  `startActivityForResult`) to Kivy's main thread via
  `Clock.schedule_once`. The Kivy main thread is the Android UI
  thread, where the app `ClassLoader` is in scope and inner-class
  resolution works. The caller's thread only blocks on the result
  `Event`. Setup itself is bounded by a 10-second `Event.wait()` so
  a wedged UI thread can't hang the caller indefinitely.
- No API change; the function still returns the same dict shapes.

### azt_collab_client 0.13.2 — test_peer.sh fingerprint extraction
- `test_peer.sh` extends signing-fingerprint detection beyond
  keytool/dumpsys (both miss v2/v3-only signed APKs) with apksigner
  + openssl-on-META-INF fallbacks. Auto-discovers apksigner under
  `ANDROID_HOME` / `ANDROID_SDK_ROOT` / buildozer's bundled SDK at
  `~/.buildozer/android/platform/android-sdk/build-tools/*/apksigner`.
- Diagnostic chain now reports every tool that was tried so a WARN
  line tells you exactly which step failed (was: a single misleading
  message that reflected only the first failure).
- Strips `SHA[- ]?(256|1):` labels before regex extraction so the
  fingerprint match can't latch onto the `56` inside `SHA256:` and
  produce off-by-one bytes (was happening on the SUITE_FINGERPRINT
  file itself).
- Detects `CN=Android Debug` in apksigner output and reports
  `peer is signed with the Android Debug keystore; SUITE_FINGERPRINT
  check skipped (only meaningful for release builds)` instead of
  failing with a spurious mismatch. Debug builds of all suite peers
  share the same default Android debug keystore, so cross-app
  signature-permission gates still work; the suite-fingerprint check
  was only ever meant for release builds.

### azt_collab_client 0.13.1 — picker resilience
- `_pick_project_android`: pre-check Intent resolvability via
  `PackageManager.resolveActivity(intent, 0)` before
  `startActivityForResult`. Returns `server_apk_not_installed`
  immediately when no Activity matches, instead of relying on
  `ActivityNotFoundException` propagation through pyjnius (some
  OEM Android builds silently no-op the call instead of throwing).
- `done.wait()` capped at 10 minutes by default (was infinite). A
  launched-but-hung picker Activity can no longer wedge the caller
  forever; callers can still pass a smaller `timeout_seconds`.

### azt_collab_client 0.13.0 — pick_project()
- New `pick_project(timeout_seconds=None)` in `__init__.py`. Same
  shape as `open_server_ui()`: subprocess spawn on desktop
  (parses `AZT_PICK\t<path>\t<langcode>` from stdout), Intent
  dispatch on Android (uses `android.activity.bind` to wait on
  `onActivityResult`; falls back to
  `{'ok': False, 'error': 'server_apk_not_installed'}` if the
  server APK isn't installed). Sister apps in any toolkit drive
  project selection through this single helper.
- `azt_collab_client/ui/picker.py`: `register_kv` (a.k.a.
  `register_picker_kv`) gains a `hide_settings_gear` kwarg. When
  True the gear icon, its hit area, and the row containing it
  collapse to zero height — used by the standalone picker app
  which has no settings of its own.
- `azt_collab_client/azt.png` — suite icon shipped alongside the
  client. Both standalone Kivy apps (`ui` + `projects`) reference
  it via `os.path.dirname(azt_collab_client.__file__) + '/azt.png'`.
- `azt_collab_client/ui/assets/langtags_mini.json.gz` — moved from
  `azt_recorder/` (deferred item from step 2). The langpicker reads
  it from this default path, so sister apps no longer need to pass
  `langtags_path=` explicitly.

### Documentation
- New `examples/non_kivy_pick.py` — tiny demo of how a non-Kivy host
  drives project selection via `subprocess.run` + the AZT_PICK
  stdout protocol. Proves the cross-toolkit contract.

### Version constants unified
- `_VERSION` was duplicated as a hard-coded
  string in `server.py` (0.9.0) while `azt_collabd.__version__` lagged
  at 0.8.0. `__version__` is now the single source of truth at 0.9.0;
  `server.py` does `from . import __version__ as _VERSION` and all
  five wire-response references (server.json, started.json,
  /v1/health body, HTTP `Server:` header) flow from there.

### Documentation
- Added `azt_collab_client/CLAUDE.md` — a self-contained guide that
  travels with the client when sister apps symlink it in, so Claude
  Code working from a sister app's tree has full client / transport
  / API guidance without needing access to the canonical
  `azt-collab/CLAUDE.md`. The top-level `CLAUDE.md` now `@`-imports
  it to avoid duplication.
- README.md audited and rewritten to match the actual tree:
  removed references to the deleted `azt_collabd_plan.xml` /
  `azt_collabd_cleanup_drafts.xml`; added `server_apk/`,
  `azt_collab_client/ui/`, and `azt_collab_client/_spawn.py` to the
  layout; updated the sister-app symlink list to peer-only
  (`azt_collab_client` + `examples` + `android`, no `azt_collabd`);
  removed the stale "fall back to loopback" Android language and the
  per-peer `<provider>` instructions; added sections for the server
  APK workflow, the picker UI re-use story, the version handshake,
  and the new client API surface (`check_server_compat`,
  `init_project`, `derive_langcode`, `create_project_from_template`,
  `clone_project*`, GitHub install URL / device flow helpers, etc.).

## [0.8.0] — 2026-04-28 — `standalone_server_apk` cleanup-draft #3 (scaffolding)

Lays down the *scaffolding* for the standalone server APK
(`org.atoznback.aztcollab`) and the client-side changes that go
with it. The APK still has to be built and signed against real
devices; what is in this commit is the source tree, manifest, and
the client-side discovery + handshake the new architecture needs.

Per the user's answers in `azt_collabd_cleanup_drafts.xml`:

- q1: peer APKs symlink `azt_collab_client` only; `azt_collabd`
  lives only in the server APK and on desktop installs. The new
  `server_apk/README_NewClient.txt` documents the symlink set.
- q3: the server APK is the *only* component that calls
  `azt_collabd.configure(app_slug=..., client_id=...,
  collaborator=...)` — peers do not. The `server_apk/main.py` boot
  reads identity from env vars with the recorder defaults.
- q4: no persistent foreground-service notification by default;
  the server APK is allowed to be transient and respawns on the
  next peer query. `server_apk/service.py` keeps the always-on
  path behind `AZT_FOREGROUND_SERVICE=1` for opt-in.
- q5: client now exposes `check_server_compat()` and a
  `MIN_SERVER_VERSION` constant. Sister apps call it once at
  startup; an old server returns `{'ok': False, 'error':
  'server_too_old'}` so the peer can surface "Please update the
  AZT Collaboration service".

### New: `server_apk/`
- `buildozer.spec` — single-purpose APK targeting
  `org.atoznback.aztcollab`, requesting the suite signature
  permission, bundling the daemon and the Java provider glue.
- `manifest_extras.xml` — `<permission>`, `<provider>`, `<service>`
  declarations spliced into the generated manifest.
- `main.py` — Kivy entrypoint: configures GitHub App identity,
  registers the ContentProvider callbacks, opens the existing
  `azt_collabd.ui.app` settings UI as the launcher activity.
- `service.py` — opt-in foreground-service stub (off by default).
- `README_NewClient.txt` — peer-app integration guide (no
  `azt_collabd` symlink, no peer `<provider>` declaration,
  signature requirement, install-prompt + min-server-version
  flow).

### azt_collab_client 0.8.0
- `transports.android_cp.discover()` probes only the canonical
  server-APK authority `org.atoznback.aztcollab`. No `.aztcollab`
  suffix fallback (no peer-hosted daemons exist; we're building).
- `pick_transport()` on Android raises
  `ServerUnavailable('server_apk_not_installed')` when the server
  APK isn't reachable, instead of silently falling back to loopback
  (which can't work on Android — no Python interpreter to spawn).
- New `check_server_compat()` helper. Returns structured outcomes
  (`server_too_old` / `server_unreachable` / `ok`) suitable for
  driving an "update / install the AZT Collaboration service" UI
  affordance.
- `MIN_SERVER_VERSION` raised to `0.7.0`.

### azt_collabd 0.8.0
- Version constant aligned with the wider 0.8 baseline. No
  wire-format changes; older clients still talk to this server.
  The version bump is the signal a peer's `check_server_compat()`
  reads when it surfaces an upgrade prompt.

## [0.7.1] — 2026-04-28 — `wire_open_server_ui_button` cleanup-draft #2

The button-wiring itself lives in each sister app's `main.py`
(`../azt_recorder/main.py` for the recorder), which is outside the
canonical-source tree. What this repo can ship is the reusable
helper + documentation so each sister app's button is a one-liner.

### azt_collab_client 0.7.1
- New `open_server_ui()` helper. Desktop: spawns
  `python -m azt_collabd ui` detached and returns
  `{'ok': True, 'pid': ...}`. Android: returns
  `{'ok': False, 'error': 'desktop_only'}` until the standalone
  server APK lands and we can dispatch via Intent. Sister-app button
  code calls this helper, not subprocess directly, so the platform
  branching only lives here.
- Re-exported from `__all__`. `__version__` and `MIN_SERVER_VERSION`
  also re-exported.

### Documentation
- New "Wiring a sync-settings button" section in `README.md` with
  the KV + Python snippet sister apps can paste.
- Quick-reference snippet updated to mention `open_server_ui`.

### azt_collabd
- Unchanged at 0.7.0.

## [0.7.0] — 2026-04-28 — `android_contentprovider_transport` cleanup-draft #1

Closes the loose ends called out in
`azt_collabd_cleanup_drafts.xml` for the ContentProvider transport.
The transport classes, Java glue, and dispatch extraction were
already in place at 0.6.0; 0.7.0 hardens behavior when providers
come and go.

### azt_collab_client 0.7.0
- `rpc.call()` and `rpc.health()` now reset the cached transport and
  re-pick on `ServerUnavailable`. A provider host that gets killed
  mid-session falls through to loopback on the next call without
  the host having to restart. Symmetrically, a provider appearing
  after a loopback startup will be picked up the next time the
  client's transport cache is invalidated.
- `transports.current_transport_name()` exposes which transport is
  in use (``loopback`` / ``android_cp``) for diagnostic surfaces.
- `__version__` and `MIN_SERVER_VERSION` are now defined at the
  package root for sister apps to read.

### azt_collabd 0.7.0
- Version constant aligned with client. `_VERSION` bumped to 0.7.0
  in `azt_collabd/server.py` and a matching `__version__` exposed
  from the package.
- No wire-format changes; clients < 0.7.0 keep working unchanged.

## [0.6.0] — pre-cleanup baseline

Snapshot of the state at the end of the 16-step migration plan
(`azt_collabd_plan.xml`). Cleanup drafts pick up from here.

### azt_collabd 0.6.0
- Loopback HTTP server with bearer token + flock single-instance guard.
- Transport-agnostic `dispatch(method, path, body)` in `server.py`.
- Connectivity watcher and debounced `request_sync` job queue.
- LIFT-aware three-way merge by `<entry guid>` with side-by-side
  conflict preservation.
- Per-project advisory `flock` locking, reentrant within a process.
- pyjnius shim (`azt_collabd/android_cp/service.py`) routing
  ContentProvider calls into the dispatch table; `openFile` streaming
  scoped to `$AZT_HOME/projects/`.
- Crash-log tail returned in `/v1/health`.

### azt_collab_client 0.6.0
- Pluggable `Transport` ABC; `pick_transport()` chooses Android
  ContentProvider when reachable, else loopback.
- Loopback transport spawns the daemon on demand, retries on
  `SERVICE_RESTARTED`.
- Decode-only `Status` / `Result` / `Project` / `ProjectStatus`.
- `translate_status` / `translate_result` for UI display; default
  English + French maps.
