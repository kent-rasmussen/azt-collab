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

## Diagnostic-log capture — `daemon_log_to_file`

The daemon's stderr goes to logcat by default. Logcat is
fine for local development but useless when a remote tester
reproduces a bug on a device the developer doesn't have
adb access to — the diagnostic output is lost.

`set_daemon_log_to_file(True)` (POST
`/v1/logging/daemon_log_to_file`) flips a config toggle AND
installs a `sys.stderr` tee in the running daemon process.
Stderr now goes to BOTH logcat AND
`$AZT_HOME/daemon.log`. `get_daemon_log()` (GET
`/v1/logging/daemon_log`) returns the file contents, the
current toggle state, and the file path.

**Hot-toggle.** The original draft of this feature required a
daemon restart for the tee to take effect. That's the wrong
shape when the user just enabled the toggle because they
want to capture the *next* event — they don't want to also
restart the daemon and lose the state they were
investigating. Module-level state
(`_stderr_tee_installed`, `_stderr_tee_original`,
`_stderr_tee_file`) holds the swap so the toggle can flip
either way without restart.

**Truncation policy.** `open(path, 'w')` truncates on each
install — one log file per "session" the user opens. Trade:
shorter capture window vs. file-size growth without bounds.
Testers typically want "the log around the crash I just had",
which is the truncate-on-enable shape. If a longer history
becomes needed, the seam to add periodic rotation is the
file open in `install_stderr_tee`.

**Off by default.** No daemon.log is created until the user
flips the toggle. Most installs never want this — it's a
diagnostic-only surface, opt-in.

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
