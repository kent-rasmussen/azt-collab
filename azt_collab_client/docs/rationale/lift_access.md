# LIFT-file access rationale

> **Rules live in `azt_collab_client/CLAUDE.md`** (rule 2 — no
> reading project state from local filesystem; daemon owns the
> bytes). **Conformity contract** — `LiftHandle` / `atomic_open_write`
> usage code — is in `CLIENT_INTEGRATION.md` § 8. This file is the
> *why*.

The daemon owns the canonical copy of every project's LIFT file
under `$AZT_HOME/projects/<lang>/<file>.lift`. On Android the
daemon lives in the server APK and that path sits inside its
private `filesDir` — peer packages **cannot** `open()` it (sandbox
denies; `[Errno 2] No such file or directory` even when the file
exists, because the peer process has no UID-level read on the
server APK's filesDir). The provider URI is the only legitimate
read/write seam.

**Provider lifetime is stable across server kills.** The server
APK is pinned by a sticky-bound service (`AZTServiceProviderhost`),
so the URI grant the picker emits is reachable for as long as the
receiving Activity is alive — Android scopes the grant to the
receiver, not the source process. Under memory pressure Android
may still kill the host; the next peer ContentResolver call
auto-spawns it via the unconditional ContentProvider contract.
Detached FDs survive the source kill (kernel-managed inode). Peers
may safely defer `LiftHandle(uri).open_read()` to a later user
gesture, and audio FDs may be held across a long recording —
neither requires the picker to still be in view.

**Don't cache.** A peer-side cache (download → edit → push back)
breaks the single-source-of-truth promise. Two peers reading at
T0 and writing at T1 / T2 race; the later writer clobbers the
earlier writer's edits and the daemon commits + pushes the
corrupted state. Read and write through the provider every time;
`LiftHandle` is cheap.

**The one peer-visible recovery surface** is
`Result.has(S.JOB_INTERRUPTED)` from `request_sync` + `poll_job`
— transient, retryable; treat as `S.SERVER_UNAVAILABLE`.
Synchronous `sync_project` callers never see this code (the
transport's retry loop absorbs a dead binder mid-call).

**`atomic_open_write` vs. `open_write`.** Use
`atomic_open_write` for any LIFT save that may race a sync's
merge-output write or another peer; the wrapper uses
sibling-tempfile + `os.replace` on filesystem paths and the
daemon's two-phase FD + finalize protocol on URIs. Two concurrent
writes are safe: whichever rename runs last wins, and the
destination is always a complete copy of one version, never torn.
`open_write` is the older path-lock-only contract — fine for
same-process serialization, unsafe for cross-process races.

## Audio + image cross-package access

`AZTCollabProvider` serves sibling files under the same authority
as the LIFT URI:

```
content://org.atoznback.aztcollab/<lang>/audio/<basename>
content://org.atoznback.aztcollab/<lang>/images/<basename>
```

Provider auto-creates `audio/` and `images/` on first write
(whitelist `_ALLOWED_MEDIA_DIRS = ('audio', 'images')` in
`azt_collabd/android_cp/service.py:_resolve_path`). Both kinds are
read+write from peers; the picker's result-Intent grant flags
(`FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION`)
cover same-authority sibling URIs without per-file grants.

Client API: `MediaHandle(path_or_uri, kind='audio'|'image')` is a
`LiftHandle` subclass — the `kind` is a log-line label, not a
functional gate. `audio_uri_for(lift_path_or_uri, basename)` /
`image_uri_for(...)` compose the sibling URI / filesystem path so
callers stay blind to the path-vs-URI distinction.

No `list_audio` / `list_images` RPCs needed — both sets of
basenames are already encoded in the LIFT XML itself (audio in
`<citation><form>` audiolang text, images in `<illustration
href=…/>`).
