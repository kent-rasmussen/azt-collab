# UI submodule rationale — `azt_collab_client.ui`

> **Rules live in `azt_collab_client/CLAUDE.md`** (rule 4 — no
> Kivy at package root; rule 6 — client owns its translatable
> strings). **Conformity contract** for share helpers + bootstrap
> entry point — is in `CLIENT_INTEGRATION.md` §§ 3 + 14b. This file
> is the *why*.

## What the submodule contains

Shared Kivy screens (`LangPickerScreen`, `ProjectPickerScreen`) and
helpers (`clone_url_popup`). Peers register these into their own
`ScreenManager`. Translations route through
`azt_collab_client.translate`; a peer with its own catalog calls
`set_translator(...)` once at startup. Kivy imports are confined
to `ui/` so the top-level package stays importable from non-Kivy
contexts (CLI helpers, tests) — see hard rule #4 in CLAUDE.md.

## Shared assets — client-first model

Anything *shared in shape* across the suite (gear, sync, share)
lives at `azt_collab_client/ui/assets/icons/<name>.png` so every
peer that imports the client gets it for free. Resolve via
`icon_path('gear')` — returns an absolute path. Standalone
subprocesses (picker, settings UI) and peers can't use relative
paths; their cwd isn't the host's repo.

Peer-specific icons (peer's own app-icon variants, single-consumer
UX-specific icons) stay in the peer. Peers that want to override a
shared icon pass an explicit override path to the consumer that
takes one (e.g. `register_picker_kv(gear_icon=...)`); there is no
implicit cwd-based search.

When in doubt, **default to client-first**. It's easier to
override locally later than to deduplicate parallel copies once
they've drifted. The client-first rule applies to *new*
shared-shape assets and to any asset whose move-to-shared is
forced by a second consumer.

## Share helpers — `share.py`

**Why centralised.** Each share dispatch is ~30 lines of jnius
autoclass + Intent construction + error translation. Multiple
surfaces (daemon UI log share, peer diagnostic share, future
viewer) would otherwise re-derive the same code; a peer-side
divergence breaks the share flow on that peer alone, which is
hard to notice until a user reports "share button does nothing."

**Why ACTION_SEND vs. ACTION_SENDTO.** `ACTION_SEND` opens the
generic share sheet — every text-handling app accepts.
`ACTION_SENDTO` with a `mailto:` URI scopes the picker to email
apps only. The "Email log" button uses the latter because the
user's intent is specifically "send to a developer"; the generic
share sheet would clutter with non-email targets to navigate past.

**Size constraints, not enforced here.** Android Intent extras
have a practical ~1 MB ceiling per transaction; callers sharing
> 256 KB should truncate first. Not asserted in the helper
because the helper doesn't know the caller's payload semantics
(truncate head? tail? sample?). The daemon-log producer
truncates at source (`_h_get_daemon_log`).

## Diagnostic-log capture — always-on, per-day, retained

The daemon's stderr goes to logcat by default. Logcat is
fine for local development but useless when a remote tester
reproduces a bug on a device the developer doesn't have adb
access to — the diagnostic output is lost.

Since 0.52.7, the daemon's `sys.stdout` and `sys.stderr` are
always teed to a per-day file at `$AZT_HOME/daemon-<tag>-
YYYY-MM-DD_log.txt` (since 0.52.20; pre-0.52.20 the suffix
was `.log` — renamed because some text editors don't
auto-recognise `.log` as text). `_LogSession` (in
`azt_collabd/server.py`) owns the file handle, rotates lazily
on the first write past local midnight, and
`_prune_daemon_log_retention` removes files older than
`settings.log_retention_days()` (default 3, min 1).
`get_daemon_log_files()` (GET
`/v1/logging/daemon_log_files`) returns the entire window in
one RPC; the picker's `Share diagnostics` ships a single
zip archive containing the snapshot plus every per-day log
inside the window (see "Why single-file ACTION_SEND" below).

**Why always-on.** Pre-0.52.7 the daemon had a user-facing
"Log server activity" toggle. Support asks for the log
*after* a failure — with a toggle, the diagnostic is gone
exactly when it would have been useful. Half of all testers
leave the toggle off; the other half leave it on and
accumulate single-file logs that grew without bound (field
log 2026-06-20 caught a 40 MB file spanning weeks). Always-
on plus retention bounds disk cost AND guarantees the next
crash is captured.

**Privacy.** The on-disk log lives in the daemon's private
filesDir on Android (peer apps cannot read it across UIDs).
The only way for content to leave the device is the explicit
`Share diagnostics` gesture the user already controls. No
external exposure without consent.

**Lazy rotation.** A wall-clock timer would fire only if the
daemon process was awake at midnight. Lazy rotation —
"check today's date at every write, swap the file if it
changed" — works regardless of whether the daemon was idle
across the boundary. Reentrant lock so the post-rotation
`_dump_lan_debug_snapshot()` (which calls `print` → tee →
session.write) can re-enter the section without deadlocking.

**Start-of-day anchoring.** Every per-day file opens with
the `lan_debug` snapshot (registry, HEAD SHAs, tracking refs,
all per-project state) so a triager reading the bundle top-
to-bottom has the baseline before the day's events. Fires on
the first install when the file is empty (fresh-of-day or
fresh install) and after each midnight rotation.

**Why single-file ACTION_SEND (zip), not
ACTION_SEND_MULTIPLE.** 0.52.6 through 0.52.17 shipped the
diagnostic bundle as separate URIs via
`ACTION_SEND_MULTIPLE` — one per per-day log plus the
snapshot. Field-tested against Signal in early 0.52.x;
Signal silently rejected the share. Multiple debugging
rounds (0.52.10 pre-grants, 0.52.13 ContentProvider
authority migration, 0.52.14 getType/query Java
implementations) eliminated every plausible upstream cause
without fixing the rejection. The actual cause turned out
to be inside Signal: `ShareRepository.kt` for
`ACTION_SEND_MULTIPLE` runtime-filters per-URI MIMEs to
image-or-video only, regardless of what the manifest claims
to accept. Verbatim, from `main` 2026-06-22:

```kotlin
.filterValues {
  MediaUtil.isImageType(it) || MediaUtil.isVideoType(it)
}
```

`ACTION_SEND` (single attachment) has no such filter — any
URI goes through to the blob path. So 0.52.19 switched the
diagnostic bundle to a single archive dispatched via
`ACTION_SEND`. 0.52.23 changed the container from zip to
gzipped-tar (`azt_diagnostics_<stamp>.tar.gz`, MIME
`application/gzip`) after a field mail server (Dome) was
found to silently strip `.zip` attachments — gzip's magic
bytes aren't in the zip family, so it clears both extension
and content-sniffing filters. Files stay separate inside the
archive, so support triage is `tar xzf && grep` rather than
scrolling through a concatenated text blob (the 0.52.18
intermediate). APKs already travel through Signal as a single
`application/*` URI, which is the precedent.

`share_files` retains the multi-item path for peer apps
that ship image/video bundles — `len(items) > 1` routes to
`ACTION_SEND_MULTIPLE`. Only the diagnostic-share composer
downshifts to single + zip. Peer apps doing
Signal-compatible multi-text shares should pre-zip on their
side.

## Self-update — `check_for_update`

`azt_collab_client.ui.check_for_update` is a reusable GitHub-
Releases-driven updater that every suite APK plugs into its
settings screen. Identity is fully parametric (`repo`,
`current_version`, `asset_filename`) so the same helper serves
the server APK and every peer — no duplicated download/install
plumbing per app.

**No SHA verification in v1.** TLS to GitHub plus Android's
signature-match install check (suite keystore enforced everywhere)
are the integrity layers. A future hardening pass can add a
`.sha256` companion asset if the release process publishes one.

## Bootstrap — `bootstrap()`

Suite invariant: **the user installs one APK** — the peer they
opened. The standalone server APK and any subsequent updates are
provisioned by the peer itself on first run. `bootstrap()` is the
single entry point that implements this; the call shape is in
`CLIENT_INTEGRATION.md` § 3.

**Why `on_done` only fires on the healthy path.** The
`server_unreachable` / `server_too_old` prompts are terminal: the
install popup is modal (`auto_dismiss=False`) and the user can't
reach the rest of the peer's UI until they install or quit; a
fresh bootstrap re-enters from the install-completion chain. So
if `on_done` fires, the daemon is reachable — peer code wired to
`on_done` doesn't need a defensive try/except for "daemon not
there yet." Before 0.28.5 this contract didn't hold and peers
hand-rolled defensive guards that obscured real bugs.

**Desktop hosts** call `on_done` immediately — there's no APK to
install, so bootstrap is a no-op outside Android.
