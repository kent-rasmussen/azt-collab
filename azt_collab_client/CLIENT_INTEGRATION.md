# Client integration checklist

> **Scope of this file.** This is the **conformity contract**
> every AZT-suite peer follows — and nothing else. Action-
> oriented sections: API peers must call, hard rules they must
> honor, migration checklists, verification blocks. No
> philosophy, no architectural rationale, no "why this design"
> — those live in ``azt_collab_client/CLAUDE.md``. If you find
> yourself wanting to add a "why" paragraph here, put it there
> instead and link forward. The contract is the part peers
> have to obey; the rationale is the part they may want to
> read to understand.

Every AZT-suite peer is a thin ``azt_collab_client`` consumer.
This doc is the **single contract** each peer follows so the
suite stays coherent. Silent drift from the contract is what
produced the v0.28.x bugs ("multiple stacked popups", "settings
page reachable when no server", "no progress indicator",
"Dismiss didn't quit when it should have").

If you're starting a brand-new peer, work through every section
in order. If you're updating an existing peer, treat each
section heading as a checklist item and confirm.

This file lives alongside ``azt_collab_client`` so peers see it
through their symlink — there's no need to clone the canonical
``azt-collab`` repo to read it.

## 1. Symlinks

From the peer's repo root:

```bash
for x in azt_collab_client examples android; do
    ln -s "../azt-collab/$x" "$x"
done
ln -s ../azt-collab/android/manifest_extras_peer.xml manifest_extras.xml
```

The peer never imports ``azt_collabd`` directly. Edits to the
symlinked client land in ``azt-collab/`` automatically.

## 2. buildozer.spec

Use ``buildozer.spec.tmpl`` + the suite's shared ``~/bin/build.sh``
renderer. Required env vars (``P4A_HOOK``, ``P4A_LOCAL_RECIPES``,
``AZT_BUILDOZER_BUILDDIR``) are validated by ``build.sh``; release
signing uses ``P4A_RELEASE_KEYSTORE`` etc. (the spec's
``android.signing.*`` and ``p4a.sign`` keys are dead config — don't
add them).

**Required permissions:**

```ini
android.permissions = INTERNET, RECORD_AUDIO, …, REQUEST_INSTALL_PACKAGES, org.atoznback.AZT_COLLAB_ACCESS
```

- ``REQUEST_INSTALL_PACKAGES`` — without it, the bootstrap install
  intent silently no-ops and the user is stuck on the prompt.
- ``org.atoznback.AZT_COLLAB_ACCESS`` — signature-protected; the
  server APK declares it, peers ``<uses-permission>`` it.

Peers do NOT need to declare ``KILL_BACKGROUND_PROCESSES``. The
package-replacement-handling work (§ 19) used to ask peers to
declare it for a peer-side `killBackgroundProcesses` backstop;
that backstop is gone as of daemon 0.42 / client 0.42 —
the server APK's own ``SuiteSelfReplaceReceiver`` reaps any
old-code daemon process from inside the new APK using its own
permission, so peers no longer need a permission to explain.

**Manifest extras** — symlink the canonical peer extras:

```ini
android.extra_manifest_xml = %(source.dir)s/manifest_extras.xml
```

(``manifest_extras.xml`` is the symlink to
``azt-collab/android/manifest_extras_peer.xml``. Carries the
``<queries>`` block Android 11+ needs for PackageManager visibility
of the server APK.)

**Java sources from the suite** — required since 0.33.0 so the
peer-side ``AZTServiceConnector`` (Phase B2 bindService for OOM
priority + Android 15 freezer mitigation) compiles into the peer
APK:

```ini
android.add_src = android/src/main/java
```

The ``android/`` symlink already points at ``azt-collab/android/``,
so this resolves to the canonical Java tree. Buildozer ``realpath``s
the value before handing it to p4a — confirm the symlink target
exists (a dangling symlink → empty srcDir → silent compile of zero
classes). Without this line peers see ``[android_cp]
AZTServiceConnector.ensureBound failed: ClassNotFoundException``
in logcat on every cold start, the bind never happens, and the
freezer issue is unmitigated.

**Sign with the suite keystore.** APKs signed with a different key
install fine but the install-time grant for
``AZT_COLLAB_ACCESS`` is denied, and provider calls silently fail.
Verify with:

```bash
keytool -printcert -jarfile bin/<peer>-*.apk | grep SHA256
```

against ``azt-collab/android/SUITE_FINGERPRINT``.

## 3. Bootstrap (this is the load-bearing part)

**On every launch**, before doing anything that touches the daemon
(opening the picker, loading a project, accessing settings), the
peer must run ``bootstrap()``:

```python
# in your App subclass:
def on_start(self):
    super().on_start()
    Clock.schedule_once(lambda dt: self._run_bootstrap(), 0)

def _run_bootstrap(self):
    from azt_collab_client.ui import bootstrap
    bootstrap(
        peer_repo='kent-rasmussen/azt-recorder',     # ← your repo
        peer_version=__version__,                    # main.py __version__
        # peer_asset_filename omitted — derived at runtime from the
        # running peer's Android package name (= aztrecorder.apk
        # for org.atoznback.aztrecorder). Pass explicitly only if
        # your fork publishes under a different scheme.
        peer_display_name='AZT Recorder',            # ← your app name
        on_status=self._set_status,                  # progress sink
        on_error=self._set_status,                   # failure surface
        on_done=self._begin_normal_startup,          # healthy path
        font_name=self._font_name,
    )

def _set_status(self, message):
    # Surface to your in-app log + stderr (logcat on Android).
    print(f'[bootstrap] {message}', file=sys.stderr)
    try:
        self.root.ids.sm.get_screen('home')._set_log(message)
    except Exception:
        pass

def _begin_normal_startup(self):
    # Only fires when bootstrap finishes a healthy terminal state
    # (server reachable, peer current, or self-update declined).
    # Safe to do daemon RPCs from here.
    pass
```

The four caller invariants ``bootstrap()`` enforces — and that you
must honor for the workflow to actually work — are documented at
the top of ``azt_collab_client/ui/bootstrap.py``. Briefly:

1. ``peer_asset_filename`` matches the GitHub release asset name
   exactly (no fuzzy match).
2. The release ``tag_name`` is parseable as a version (``v1.2.3``,
   ``1.2.3``, ``2026-05-06`` all work; ``latest`` doesn't).
3. ``prerelease=true`` releases are skipped; users only get stable.
4. The peer's ``buildozer.spec`` lists ``REQUEST_INSTALL_PACKAGES``.

**``on_done`` semantics (0.28.5+):** fires only on healthy paths
(server reachable + peer at latest, or self-update declined). The
no-server / server-too-old branches do NOT fire ``on_done`` —
they show a modal popup that's the terminal state. So your
``on_done`` callback can safely assume the daemon is reachable
and the peer is current; no defensive try/except needed.

### Automatic since 0.33.0: peer holds a service bind

The client transport's ``discover()`` automatically holds a
``bindService`` against the server APK's ``AZTServiceProviderhost``
for the rest of the peer's process lifetime. This is what
keeps the daemon's ``:provider`` process un-frozen on Android
15 and warm-cached across calls within a session. No peer
code change is required — importing ``azt_collab_client`` and
making any RPC call (which ``bootstrap()`` does on startup) is
enough to trigger the bind on first contact.

If you see ``[android_cp] AZTServiceConnector.ensureBound
failed: ClassNotFoundException`` in your peer's logcat, the
peer's ``buildozer.spec`` is missing
``android.add_src = android/src/main/java`` (see § 2 above).
Without that, the connector ``.java`` doesn't get merged into
the dist's source set and the class isn't on the runtime
classpath. ``buildozer android clean`` after adding the line is
required — buildozer caches dist trees and won't repopulate
``src/`` on its own.

### Required: pre-warm in ``App.build()``

Cold-start on Android serializes peer Kivy boot, then daemon
lazy-spawn (via the bind from B2), then the peer's first
compat probe. ``bootstrap.prewarm()`` overlaps the daemon's
lazy-spawn with the peer's own Kivy init by firing the bind
early.

**Wire it into every peer's ``App.build()``:**

```python
class MyApp(App):
    def build(self):
        from azt_collab_client.ui.bootstrap import prewarm
        prewarm()
        return self._build_root_widget()
```

Idempotent; no-op on non-Android.

**Why required.** Measured on R500-class slow tablet
(2026-05-09 post-Phase-B2): daemon Python boot is ~600ms
steady-state, ~1.1s on first cold start. Prewarm overlap
window is ~1.9s. With prewarm, daemon boot fits inside Kivy
init, peer wait is **~50–60ms**. Without prewarm, peer wait
would be the full ~600–1000ms — the difference between "feels
instant" and "feels sluggish." Fast phones see the wait
shrink to sub-second either way, but the cost of always
calling prewarm is essentially zero, so there's no reason
not to.

Tradeoff to be aware of: ``prewarm`` initialises pyjnius /
the ContentProvider transport earlier than the rest of the
peer might expect. If your ``build()`` is already the first
place that touches Android Java surfaces (the recorder is),
this is free. If your peer touches Android elsewhere first,
prewarm may shift the cost rather than reduce it — measure
with the harness in § 16 if your peer's structure is unusual.

Toggle for measurement runs (no rebuild needed):
- ``$AZT_HOME/_no_prewarm`` sentinel file → opts the call out.
- ``AZT_BOOT_PREWARM=0`` env var (where reachable) → also opts out.

The ``measure_boot.sh`` harness drops/clears the sentinel via
``adb shell run-as`` so peers built with debug-keystore can
flip scenarios without rebuilding.

## 4. **Do NOT roll your own "server is missing" UI.**

This is the single most common bug. Symptoms:

- Multiple popups stack on first launch ("AZT collab service not
  installed" *and* "Install AZT Collaboration?").
- A pre-bootstrap error popup ("Could not open project picker:
  server_apk_not_installed") appears alongside the bootstrap
  popup.
- The user reaches a settings or picker screen showing an error
  about the missing server, despite bootstrap also being on
  screen.

All of these come from peer code that catches a "server unreachable"
result and shows its own popup. **Don't.** ``bootstrap()`` shows the
canonical ``install_server_apk_popup``, and that popup is *modal*
(``auto_dismiss=False``), so the user can't reach the rest of your
UI until they install or quit. Your own try/except blocks around
client RPCs should *log the failure and continue*, not pop their
own dialog. The bootstrap popup is the single source of truth for
this state.

If you find this kind of code in your peer, delete it. The recorder
had three sites (``main.py:2696``, ``:4562``, ``:4599``); the viewer
had two (``main.py:360``, ``:461``). Audit yours.

## 5. Handle picker cancel as "close the app" on first setup

When the user backs out of ``pick_project()`` without having a
``last_project()`` to fall back to (typical first-launch scenario:
brand-new install, picker shows, user backs out), the picker
subprocess emits ``RESULT_CANCELED`` and your peer's result handler
gets an empty path. Don't drop the user on an empty / broken main
screen — close the app. Next launch is a clean retry, and that's a
better UX than "the app loaded but I can't do anything".

### 5a. Non-Kivy desktop hosts: pass ``python_exe`` (0.54.6+)

The desktop picker (``python -m azt_collabd projects``) is a Kivy
app. A Kivy host (recorder, viewer) calls ``pick_project()`` bare and
nothing changes for it — the parameter is optional and the default
(``sys.executable``) is the pre-0.54.6 behavior, so existing peers
need no code change. A **non-Kivy** desktop host (tkinter A-Z+T)
must pass a Kivy-capable interpreter, exactly as with
``open_server_ui(python_exe=…)`` (0.53.4):

```python
result = pick_project(python_exe=kivy_python)   # 0.54.6+
```

Try candidates in order (env override, own interpreter, suite venvs)
and fall back on ``TypeError`` for clients older than 0.54.6 — see
``azt/backend/core/collab.py::pick_team_project`` for the reference
loop. Android is unaffected (the Intent path ignores the parameter).

Pattern for your peer's picker result handler (recorder, viewer,
future peers — adapt to your screen names):

```python
def _on_picker_result(self, result):
    path = (result or {}).get('path', '')
    if not path:
        # Picker emitted RESULT_CANCELED.
        #
        # The picker subprocess only does this when it has no
        # last_project to auto-resume to — if there *was* one,
        # the picker would have emitted that path instead, so
        # we'd never reach here on a returning-user flow.
        #
        # Therefore this branch == first-setup-and-user-backed-
        # out. The peer is in an unusable state (no project
        # loaded, no daemon work to do); closing the app is the
        # right action.
        from azt_collab_client import last_project
        from kivy.app import App
        if not last_project():
            App.get_running_app().stop()
            return
        # Defensive: if last_project() returns truthy here, the
        # picker shouldn't have given us an empty path. Log it
        # but otherwise fall through — the peer will surface
        # whatever empty-state it normally shows.
        import sys as _sys
        print('[peer] picker cancel with last_project='
              f'{last_project()!r}; unexpected, see picker_app',
              file=_sys.stderr, flush=True)
        return
    # ... normal load path ...
```

The picker subprocess (in ``azt_collabd/ui/picker_app.py``) already
does the work to distinguish these two cases —
``_exit_to_last_project_or_cancel`` resolves ``last_project`` and
emits its path on auto-resume, ``_emit_cancel_and_quit`` only on
true cancel. The peer just has to honor the contract.

## 6. Translation

If your peer ships its own ``.po`` catalog, chain the client
catalog as a fallback so client-owned strings translate without
duplication, AND subscribe to language-change events so the
chain stays fresh when the daemon's language toggle fires.

```python
import gettext, azt_collab_client
from azt_collab_client import i18n as collab_i18n

def set_app_language(lang):
    if lang == 'en':
        peer_t = gettext.NullTranslations()
    else:
        collab_i18n.ensure_mo(PEER_LOCALES, '<peer-domain>', lang)
        peer_t = gettext.translation(
            '<peer-domain>', localedir=PEER_LOCALES,
            languages=[lang], fallback=True)
    collab_i18n.set_language(lang)
    peer_t.add_fallback(collab_i18n.gettext_translation())
    azt_collab_client.set_translator(peer_t.gettext)

# Required since client 0.43.1: re-run the chain whenever the
# client catalog re-languages. Bootstrap's
# ``_sync_ui_language_with_daemon`` calls ``collab_i18n.set_language``
# with the daemon's language pref, which fires this subscriber so
# the peer's gettext.translation is re-created in the new language
# and its add_fallback target gets re-captured against the freshly-
# swapped client._current. Without this hook the peer's catalog stays
# frozen at startup language while the client catalog re-languages —
# producing the "only client-owned strings translate, and only when
# fallback-retried" split closed in client 0.43.1.
collab_i18n.subscribe_language_change(set_app_language)
```

The subscriber call is idempotent — peers that import this module
multiple times don't end up with duplicate callbacks.

If you don't have your own catalog (small peer): nothing to do.
The client catalog applies automatically when a language is
selected from the daemon's settings UI. ``client.translate.tr``
delegates straight to whatever ``set_translator`` was last given
(or the client's own catalog if none); as of 0.43.1 there is NO
second-chance retry to the client catalog when the host
translator returns the msgid unchanged. Peers that ship a catalog
but skip the ``add_fallback`` step will see client-owned strings
render as English msgids. Wire the fallback per the snippet above.

## 7. App.title

The bootstrap popup's Quit button reads ``"Quit {App.title}"``
(e.g. "Quit AZT Recorder"). Set ``title`` on your App subclass:

```python
class RecorderApp(App):
    title = 'AZT Recorder'
```

If you don't, the button just says "Quit" — functional but less
obvious.

## 8. LIFT file access

Use ``LiftHandle`` from ``azt_collab_client``. The picker emits
either a filesystem path (desktop / open-file flow) or a
``content://`` URI (Android clone / template flow); ``LiftHandle``
papers over the difference:

```python
from azt_collab_client import LiftHandle
handle = LiftHandle(path_or_uri_from_picker)
with handle.open_read() as f:
    tree = ElementTree.parse(f)
```

**Don't** ``open(path, 'rb')`` directly — content URIs aren't
filesystem paths. **Don't** cache to a peer-side directory and
work from the cache; that breaks the single-source-of-truth
contract.

**Atomic writes for LIFT.** Use ``handle.atomic_open_write()``
(not ``open_write``) for any LIFT save that may race a sync's
merge-output write or another peer:

```python
with handle.atomic_open_write() as f:
    tree.write(f, encoding='utf-8', xml_declaration=True)
```

The wrapper handles routing internally: filesystem paths use a
sibling-tempfile + ``os.replace``; ``content://`` URIs use the
daemon's two-phase FD + finalize protocol (peer ships bytes
through a ``ContentResolver.openFileDescriptor`` write to a
per-token scratch file, then calls ``atomic_finalize`` to rename
under ``project_lock``). Peer code stays the same on both paths.

**Rebuild bundles the FD-write path.** Pre-0.41.7 clients
shipped bytes via base64 in the JSON-RPC body, which on Android
hit Binder's ~1 MB per-transaction cap and silently failed for
LIFT files larger than ~700 KB (the save-audio-recording flow
is the most common trip). Peers rebuilding against 0.41.7+
pick up the new two-phase write transparently — no code
change, just rebuild. Pre-0.41.7 daemons get the legacy
single-RPC path as a fallback (works for small payloads).

## 8a. New-project template cleanup is the daemon's job (since 0.52.32)

When a peer starts a new project from the language picker's
new-from-template flow, the daemon downloads the wordlist template and
**prunes it to the chosen vernacular server-side**, inside
``create_from_template``, before the project is registered. The project
the peer loads is already cleaned: `<lexical-unit>` holds only the
vernacular headword form (with a no-loss move of any source word into a
gloss), empty glosses are dropped, and `SILCAWL` / `grammatical-info` /
`semantic-domain` / `illustration` / `trait` are preserved.

**Don't** run your own template cleanup / pruning / "strip the other
languages" pass over a freshly-created project. It's redundant, and a
second cleaner with different rules re-introduces exactly the
cross-peer drift the server-side single-sourcing exists to prevent.
**A peer that shipped its own template cleaner before 0.52.32 must
remove it** — historically it ran only on one creation path and missed
picker-created projects entirely (the bug that motivated moving this
into the daemon), so deleting it loses nothing and stops double-work.

Just ``load_lift`` what the picker/daemon hands back. The vernacular
tag it was cleaned against is the full assembled BCP-47 tag the picker
produced (e.g. ``nml``, ``ba-x-dialect``, ``en-US-x-Kent``) — the same
value the daemon owns as the project's langcode.

## 8b. Whole-file editor contract (`submit_file`, since 0.53.0; desktop-only)

The contract for a peer that serializes and saves the **entire LIFT**
per edit (desktop A-Z+T) instead of using § 9a's surgical writes.
Design + rationale: `azt-collab/agenda/azt_persistence_server_sync.md`.

A whole-file editor MUST NOT plain-overwrite the working-tree LIFT: the
daemon merges peer changes into that file (WAN sync, LAN receive), and
an overwrite based on a stale in-memory model silently reverts them at
the content level. Instead, every save is **base-aware**:

```python
from azt_collab_client import submit_file, project_status, S

# At load: remember the base you are editing on.
base = project_status(langcode).head_sha    # '' pre-first-commit

# At save: serialize the WHOLE file to a staged sibling (same
# directory as the target — the daemon does a same-filesystem
# os.replace), then hand it off:
result = submit_file(langcode, 'xyz.lift', staged_path, base)
if result.has(S.MERGED_WITH_LOCAL):
    # Peer changes landed since ``base`` and were three-way-merged
    # with your bytes. NOTHING WAS LOST — but your in-memory model
    # is stale. Reload before accepting further edits (§ 14 smooth
    # reload).
    ...
if result.has(S.COMMITTED_LOCAL):
    base = result.head_sha                  # your next base
elif result.has(S.CONTRIBUTOR_UNSET):
    # Bytes landed on disk (durability never waits on identity);
    # only the commit was refused. Route to the set-your-name
    # screen; keep base unchanged.
    ...
elif result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR, S.BUSY,
                    S.COMMIT_FAILED):
    # Bytes may not have landed — fall back to your direct
    # atomic write so the user's save NEVER fails, and retry the
    # daemon path later. (A 'not_found' SERVER_ERROR means the
    # daemon predates 0.53.0.)
    ...
```

Obligations:

1. **Never bypass.** All LIFT writes in collab mode go through
   ``submit_file`` (or, on daemon-unavailable, the documented direct
   fallback + a later ``commit_project`` to pick the bytes up). Never
   write the working-tree LIFT while also holding a stale base — the
   base-aware handoff is the no-clobber guarantee.
2. **Reload on `MERGED_WITH_LOCAL`** before accepting further edits;
   until the reload completes, defer/queue saves.
3. **§ 17b still applies.** Poll ``project_status`` (5–15 s) and
   reload on ``head_sha`` change — ``submit_file`` protects your
   *writes*; the poll bounds how long you *display* stale peer data.
4. **Commit cadence.** ``submit_file`` commits synchronously per save.
   Non-LIFT artifacts (settings files, audio, chart output) are picked
   up by whole-tree staging on the next commit — call
   ``commit_project`` at task boundaries / shutdown for those; don't
   call it per keystroke.
5. **Desktop only.** On Android there is no staged-file handoff
   through the ContentProvider; use § 9a surgical writes there.

Registration of a desktop project (adopt-in-place) also appends azt's
artifact ignore patterns to the project ``.gitignore`` (daemon-side,
idempotent, content-preserving) and refuses a second langcode over an
already-registered working_dir with HTTP 409
``working_dir_already_registered`` + ``existing_langcode``.

## 9. Audio / image references

For audio recording:

```python
from azt_collab_client.lift_io import audio_uri_for, MediaHandle
handle = MediaHandle(audio_uri_for(lift_path_or_uri, basename),
                     kind='audio')
with handle.open_write() as f:
    # …record into f.fileno()
    pass
```

For images, the same shape — both reads and writes — since the
0.35.2 client. ``MediaHandle(image_uri_for(lift_path_or_uri,
basename), kind='image')`` opens for read; ``.open_write()``
attaches new image bytes. Use ``LiftHandle.open_write`` to
update the ``<illustration href=…>`` reference in the LIFT
itself in the same flow (two-write pattern: media bytes +
LIFT-side ref).

## 9a. Surgical LIFT field writes (since 0.50.29)

> **For the rationale** — why a parallel write path exists, why
> per-field endpoints rather than generic `set_form`, why splice
> rather than full-file rewrite even on the daemon side — see
> `docs/rationale/lift_access.md` § "Surgical field writes
> (0.50.29)". This section is the contract.

For LIFT writes that touch a single sub-element of a single entry —
the canonical case is "save the audio filename for entry X" or
"save the illustration href for entry X" — prefer the surgical
RPCs in `azt_collab_client` over rebuilding the full DOM and
serialising it back. Two endpoints today:

```python
from azt_collab_client import set_audio, set_illustration, S

result = set_audio(langcode, entry_guid,
                   audio_lang,         # e.g. 'en-Zxxx-x-audio'
                   audio_filename)     # e.g. '1143_d6aa935c_bee.m4a'

result = set_illustration(langcode, entry_guid,
                          href)        # e.g. '0165_wave.png'
```

Daemon-side these route through `azt_collabd.lift_surgery`. The
peer never sees the LIFT bytes; the call returns a typed `Result`.

### Guarantees the daemon provides

1. **Byte-stable outside the target entry's bytes.** Every byte
   outside `<entry guid="X">…</entry>` equals the input file's
   bytes at the same offset. `git diff` shows only that entry's
   lines as changed.
2. **Other sub-elements inside the entry untouched.** `set_audio`
   leaves vernacular `<form>` siblings inside `<citation>` alone;
   `set_illustration` leaves other senses (and any sibling
   `<illustration>` elements in the target sense, beyond the
   first) alone.
3. **Well-formedness validation, mandatory.** Daemon SAX-parses
   the spliced bytes before the atomic rename; a splice that
   produced invalid XML is refused, original bytes stay on disk.
4. **Atomic write.** Sibling-tempfile + `os.replace`, under
   `project_lock`. A crash mid-write leaves the previous bytes
   intact.
5. **`notifyStatusChanged` fires** on success so peers'
   `ContentObserver` wake within ~10 ms.
6. **Debounced auto-commit fires** on success by default — same
   shape as `atomic_commit_bytes`. The peer does not need to call
   `commit_project` after. **Opt-out (since 0.50.51):** pass
   `commit_after=False` to `set_audio` / `set_illustration` /
   `atomic_commit_bytes` / `atomic_finalize_pending` when the
   peer owns the commit boundary itself (e.g. recorder's
   swipe = "I accept this take"; writes during preview /
   re-record must not commit). When suppressed, the peer is
   responsible for calling `commit_project(langcode)` at the
   boundary. The atomic write + `notify_project_changed` still
   happen unchanged.

### Targets

- `set_audio(langcode, guid, lang, filename, commit_after=True)` →
  `<entry guid="X">/<citation>/<form lang="{lang}">/<text>{filename}</text></form>`.
  Creates `<citation>` if absent.
- `set_illustration(langcode, guid, href, commit_after=True)` →
  `<entry guid="X">/<sense>/<illustration href="{href}"/>`. Uses
  the first `<sense>` (creates one if absent); within it, updates
  the first `<illustration>` (creates one if absent).

Same `commit_after=True` default on
`atomic_commit_bytes(langcode, rel_path, data, commit_after=True)`
and
`atomic_finalize_pending(langcode, rel_path, token, commit_after=True)`.
Pass `False` to suppress the auto-commit (see step 6 above);
peer is then responsible for calling `commit_project(langcode)`
at its own boundary. Requires daemon 0.50.51+ — enforced by the
client's `MIN_SERVER_VERSION` floor, so a peer running on an
older daemon hits the bootstrap update prompt before the
silent-ignored-flag case can fire.

If the field doesn't fit one of these two paths (e.g.,
`<pronunciation>` audio, `<gloss>` text, sense-indexed
illustration), the peer still needs DOM-rewrite — file a
NOTES_TO_DAEMON entry naming the field and the daemon team can
extend the shape.

### Status codes the peer must route

| Status code | When | What the peer does |
|---|---|---|
| `S.AUDIO_SET` | First-time write or text replaced. | Optimistic in-memory `entries` dict update (if the peer keeps one); refresh UI to show the new filename. |
| `S.AUDIO_SET_NO_CHANGE` | The audio-lang form's text already equalled the filename. | No UI feedback needed — peer was likely re-saving the same value (e.g., re-tap of a recording button). |
| `S.ILLUSTRATION_SET` | First-time write or href replaced. | Same as `AUDIO_SET`. |
| `S.ILLUSTRATION_SET_NO_CHANGE` | The illustration's href already equalled the value. | Same as `AUDIO_SET_NO_CHANGE`. |
| `S.ENTRY_NOT_FOUND` | No `<entry guid="X">` in the LIFT. | Real error: the entry was deleted under the peer, or the peer's in-memory list drifted from disk. Surface a toast; reload the entries list. Carries `guid` param. |
| `S.LIFT_INVALID` | Source file missing, source parse failure, or post-splice well-formedness check failed. | Data-quality-class. Surface the translated toast (carries `error` detail); the daemon refused to persist; the file on disk is still the previous well-formed version. |
| `S.BUSY` | `project_lock` couldn't be acquired in time. | Silent; the peer's next gesture re-tries naturally. Same envelope as auto-sync `S.BUSY`. |
| `S.SERVER_UNAVAILABLE` / `S.SERVER_ERROR` | Transport failure. | Silent in auto paths; transient-error toast in user-gesture paths. Per the standard wrapper contract. |

### Migration recipe (DOM-rewrite save → surgical RPC)

```python
# Before (recorder 1.55.x style, ~25 MB working set on 4 MB LIFT):
def set_audio(self, guid, filename):
    entry_el = self._find_entry(guid)
    # ... walks <citation><form lang=audiolang><text>
    text_el.text = filename
    self._save()              # ← this builds + serialises the DOM

def _save(self):
    self._ensure_dom()        # ~5× source-size allocation
    if self._indent_dirty:
        self._indent(self._root)
    with self.handle.atomic_open_write() as f:
        self._tree.write(f, encoding='utf-8', xml_declaration=True)

# After (~5 MB working set on 4 MB LIFT):
def set_audio(self, guid, filename):
    # Optimistic in-memory update of the peer's entries dict so the
    # UI updates immediately; the RPC persists in parallel.
    self._entries[guid].audio = filename
    result = set_audio(self._langcode, guid,
                      self._audio_lang, filename)
    if result.has_any(S.AUDIO_SET, S.AUDIO_SET_NO_CHANGE):
        return
    # ENTRY_NOT_FOUND / LIFT_INVALID / SERVER_*  — route per the table
    # above; revert the optimistic entries dict update if needed.
    self._handle_save_failure(guid, result)
```

`_ensure_dom`, `self._tree`, `self._root`, and the per-save
`_indent` go away **for fields covered by surgical RPCs**. If the
peer still has other write paths that touch fields not covered by
`set_audio` / `set_illustration` (e.g., headword edits, sense
reordering), those paths still need the DOM until matching RPCs
land.

### First-edit-per-entry diff caveat

The daemon re-emits the touched entry via `ET.tostring` after
`ET.indent` at the file's detected indent unit. If the file was
previously written by a peer's `_indent` with a different
whitespace style, the first surgical edit per entry produces a
larger-than-minimal diff for that entry (reformatted to
`ET.tostring`'s convention). Subsequent edits of the same entry
are stable — the entry's bytes are now in the daemon's canonical
form. A one-time normalize sweep (touch every entry with a no-op
`set_audio` call carrying its current value) at recorder startup
locks the format in across the project; not required for
correctness, only for cosmetics.

## 10. CAWL image access

The CAWL → image-URL map and the CAWL image binaries are
suite-scoped resources served by the daemon. Peers consume
them through ``cawl_index(langcode)`` and
``CAWLHandle(langcode, basename)`` — not by hitting GitHub
directly.

**Hard rules.**

- Don't hit ``api.github.com`` from the peer for the CAWL
  tree listing. Call ``cawl_index(langcode)`` instead.
- Don't hit ``raw.githubusercontent.com`` from the peer for
  CAWL image binaries. Use ``CAWLHandle(langcode, basename).
  open_read()`` instead.
- Don't keep a peer-side **on-disk** cache of CAWL image
  bytes. The daemon's cache (``$AZT_HOME/cawl/<owner>/<repo>/
  images/<basename>``) is the durable copy and is shared
  across every peer on the device. An in-memory hot cache
  (LRU keyed by basename in the display loop) is fine — only
  durable peer-side storage is forbidden.
- Don't follow ``cawl_index().files[].url`` from peer code
  once Stage 2 is done. That field is informational only —
  the URL is the upstream the daemon would fetch from. A peer
  that follows it bypasses the cache layer and is back to the
  Stage 2 anti-pattern.
- Don't mirror the project's ``cawl_image_repo`` slug in peer
  prefs; read it through ``open_project`` /
  ``project_status`` / ``list_projects`` each time. See § 11.

**Required API surface.**

- ``cawl_index(langcode) -> dict`` — returns
  ``{repo, branch, fetched_at, files: [{path, url}, …]}`` or
  ``{}``. Peer code maps ``files[].path`` (basename) to CAWL
  identifiers per its own convention. Treat ``{}`` as "no
  images known" — same shape pre-migration peers got from an
  empty resolver dict; no separate daemon-error branch needed.

- ``CAWLHandle(langcode, basename).open_read() -> file-like`` —
  binary file-like, usable as a context manager. Returns a
  ContentProvider FD on Android (zero-copy from the daemon's
  cache), an ``io.BytesIO`` on desktop (HTTP-loopback into the
  same cache). Raises ``FileNotFoundError`` on 404 / fetch
  failure with no cached copy; raises ``ServerUnavailable``
  on transport failure. Both are recoverable — fall through
  to the peer's no-image rendering and try again next session.

  Read-only; ``open_write`` isn't supported (the
  ContentProvider rejects write modes for CAWL paths).

**Migration is two stages — both are required.**

A peer that's done Stage 1 only is **not conformant**. The
user-visible rate-limit symptom (the 403 from
``api.github.com``) is gone after Stage 1, which is
satisfying, but Stage 2 (image binaries) is the lift that
eliminates per-peer disk duplication and enables cross-peer
dedup. Any one of these symptoms means the migration is not
complete:

- Peer code still calls
  ``urlopen('https://raw.githubusercontent.com/…')`` for CAWL
  images.
- A peer-side disk cache directory of CAWL bytes (any name —
  ``image_cache_dir/``, ``user_data_dir/image_cache/``,
  similar) exists and accumulates files.
- The daemon's cache at ``$AZT_HOME/cawl/<owner>/<repo>/
  images/`` stays empty even though entries are rendering
  (bytes are flowing somewhere they shouldn't).

**Migration checklist.**

1. **Stage 1 — replace direct ``api.github.com`` calls.**
   Anywhere the peer hits
   ``urlopen('https://api.github.com/repos/<repo>/git/trees/HEAD?recursive=1')``
   to build a CAWL resolver, swap for
   ``cawl_index(langcode)``. The peer's filename → identifier
   mapping stays peer-side; only the URL listing moves.

2. **Stage 2 — replace direct ``raw.githubusercontent.com``
   calls.** Anywhere the peer does
   ``urlopen('https://raw.githubusercontent.com/<repo>/HEAD/<basename>')``
   followed by ``write_to_peer_cache(basename, bytes)``, swap
   for:

   ```python
   from azt_collab_client import CAWLHandle
   with CAWLHandle(langcode, basename).open_read() as f:
       bytes_for_display = f.read()
   ```

   Drop the peer-side cache-write step entirely. Any resolver
   code that read ``files[].url`` from ``cawl_index()`` for an
   HTTP fetch should no longer reference that field at all.

3. **Delete the peer-side CAWL on-disk cache directory.**
   That whole code path (``image_cache_dir/``,
   ``user_data_dir/image_cache/``, any sibling) goes away. An
   in-memory hot cache for rendering perf (LRU keyed by
   basename) is fine to keep — Stage 2 only kills *durable*
   peer-side storage of CAWL bytes. Migrating peers can
   ``shutil.rmtree(..., ignore_errors=True)`` the old
   directory on startup as a one-shot cleanup of
   pre-Stage-2 installs.

4. **Decide whether the peer needs a per-project
   ``cawl_image_repo`` UI** (see § 11). Most peers can skip
   this — the daemon-global default covers the common case.

**Verification.** After Stage 2 is in, the peer must pass all
of:

- ``grep -r 'raw.githubusercontent.com' <peer>/`` returns
  zero hits in CAWL-related code.
- The peer's pre-migration CAWL cache directory doesn't
  exist on disk after a fresh install, and isn't created on
  first run.
- On Android, ``dumpsys diskstats`` shows the daemon's
  ``$AZT_HOME/cawl/`` populating with image files as entries
  render, while the peer's ``filesDir`` stays small.
- No peer code references ``cawl_index().files[].url``.
  (``files[].path`` is the basename peers map to CAWL
  identifiers and remains valid.)

### Daemon-driven prefetch + progress indicator (required when prefetching)

Image-cache warming is now **daemon-owned** (since 0.41.21).
The peer MUST surface a user-visible progress indicator while
the warm runs but does NOT need to trigger it — the daemon
fires its own ``auto_prefetch`` from ``_touch_project`` on
every langcode-bound endpoint, so the very act of opening a
project warms its CAWL image cache. Without the indicator,
users naturally disconnect Wi-Fi between gestures and end up
with a half-warm cache that then blocks on demand-fetch for
every uncached image — so the visual cue stays mandatory even
though the trigger doesn't.

**Why daemon-owned.** Pre-0.41.21 the peer iterated its
working-set itself (``CAWLHandle(...).open_read`` per image,
or an explicit ``cawl_prefetch`` POST). That left the daemon
ignorant of the working-set size, gated correctness on the
peer doing the right thing on every boot, and made the
progress indicator unreliable when the peer's chosen variants
didn't match the index's variants 1:1. Daemon-driven puts
iteration on the party that does the actual fetching: progress
is accurate, retry on connectivity edge is automatic, and the
peer drops a code path it never benefited from owning.

The split (since 0.41.21):

- **Bulk warming** → automatic. The daemon's ``_touch_project``
  (every langcode-bound endpoint already fires it) calls
  ``cawl.auto_prefetch(repo)`` which iterates the full index in
  a background thread. The peer just polls ``cache_status``
  for progress.
- **Optional explicit override** → ``cawl_prefetch(langcode,
  paths)``. Peers that want to warm a subset different from
  the full index (e.g. a tighter working-set keyed to the
  user's current view) may still POST this. Idempotent
  against the daemon-driven prefetch; lock-coalescing per
  target means no double-fetch.
- **On-demand fetch** → ``CAWLHandle(...).open_read`` for any
  individual image the peer needs to display *right now*.
  Daemon-served from cache or fetched if missing — same
  backing store the bulk warm populates.

#### Wiring (typical peer)

```python
from kivy.clock import Clock
from azt_collab_client import cawl_cache_status

def _start_cawl_warm(self, langcode):
    """Start the progress indicator after a project load.
    The daemon already auto-triggered the warm via
    ``_touch_project``; we just render its progress."""
    self._cache_status_langcode = langcode
    self._cache_status_last = None
    self._cache_status_event = Clock.schedule_interval(
        lambda _dt: self._tick_cache_status(), 1.0)
    self._tick_cache_status()

def _tick_cache_status(self):
    status = cawl_cache_status(self._cache_status_langcode)
    cached, total = status['cached'], status['total']
    offline = status.get('offline', False)
    circuit_open = status.get('circuit_open', False)
    # Log ONLY on state change so a 1 Hz poll doesn't fill
    # logcat with identical lines.
    key = (cached, total, offline, circuit_open)
    if key != self._cache_status_last:
        print(f'[cache-status] {cached}/{total} '
              f'offline={offline} circuit_open={circuit_open}',
              file=sys.stderr)
        self._cache_status_last = key
    if total == 0:
        self._hide_cache_indicator()
        return
    if cached >= total:
        self._hide_cache_indicator()
        self._cache_status_event.cancel()
        return
    if offline:
        # Worker bailed before iterating; banner stays polling
        # so we auto-update when the daemon's scheduler edge
        # fires on_online_edge → re-fires auto_prefetch. The
        # 1 Hz cost is in-memory dict lookups; the [first-try]
        # probe for this path is already suppressed in the
        # transport.
        self._show_cache_indicator(
            _('Image cache: {cached} / {total} '
              '(offline — will resume when online)').format(
                  cached=cached, total=total))
    elif circuit_open:
        # Mid-prefetch connectivity loss. Same auto-resume
        # path via the daemon's scheduler edge.
        self._show_cache_indicator(
            _('Image cache: {cached} / {total} '
              '(paused — connectivity lost)').format(
                  cached=cached, total=total))
    else:
        self._show_cache_indicator(
            _('Caching images: {cached} / {total} '
              '(network in use — please stay online)').format(
                  cached=cached, total=total))
```

**The `offline` / `circuit_open` / `finished` flags are
additive (since 0.41.21).** Old peers reading only ``cached``
and ``total`` keep working — when the daemon offline-skipped a
prefetch, ``cached`` falls back to the actually-on-disk count
so the displayed numbers stay honest. Peers that read the new
flags can additionally badge the banner "offline" / "paused"
instead of showing what looks like stuck progress.

### Per-source telemetry (since 0.50.21) — surface LAN vs Internet

`cache_status` carries four additional fields so peers can show
the user **where bytes are coming from**. The NOTES #3 LAN-share
path (since 0.50.14) tries paired peers' caches before going to
GitHub; without surfacing the source, the user can't tell whether
it's actually working — they just see "Caching images: 45/1700"
and don't know if those bytes are cellular (expensive on metered
links) or LAN (free).

| Field | Type | Meaning |
|---|---|---|
| ``from_cache`` | int | Count of cache hits this prefetch session — file was already on disk, no fetch needed. |
| ``from_lan`` | int | Count of bytes pulled from a paired LAN peer's cache via ``/v1/lan/cawl_fetch``. |
| ``from_upstream`` | int | Count of bytes pulled from GitHub. The expensive one. |
| ``last_source`` | str | Source of the most-recent successful fetch: ``'cache'``, ``'lan'``, ``'upstream'``, or ``''`` (no successful fetch yet). For a one-glance "what's serving right now" tag. |

All four are zero when no prefetch is running for this repo (the
fallback branch of ``cache_status``). Pre-0.50.21 daemons don't
emit these keys; peer code should default-zero on
``status.get('from_lan', 0)`` etc.

#### Required: read every field verbatim from the response

Every cache_status field listed above (``from_cache``,
``from_lan``, ``from_upstream``, ``last_source``) **is daemon-
owned wire content**. Peer-side compliance requires:

1. **Read** the field from the response — ``status.get(...)`` —
   on every poll.
2. **Log raw, render flexibly.** If your peer emits a debug log
   line that names a cache_status field (the typical
   ``[cache-status] ... last_source='X' from_cache=N ...``
   shape), the logged value MUST be the unmodified
   ``status.get(...)`` value. Render code may map
   ``last_source='cache'`` to an empty display tag for the
   user (fine), but the diagnostic log line must still show
   the raw ``'cache'``. Otherwise the log becomes a misleading
   diagnostic that points investigators at the wrong layer.
3. **Delta tracking, if any, must update its baseline.** If the
   peer computes per-poll deltas (``Δcache = from_cache -
   prev_from_cache``) it MUST advance ``prev_from_cache`` on
   each poll. A peer that perpetually reports ``Δcache=0`` while
   the daemon's ``from_cache`` is observably climbing has broken
   delta tracking.

These three rules describe peer-side hygiene; satisfying them
doesn't fix a daemon-side bug. The
``cawl_cache_status`` wrapper in
``azt_collab_client/__init__.py`` (daemon-team-owned) is
responsible for forwarding every field the daemon emits — pre-
0.50.37 it stripped the per-source fields silently and ALL
peers saw zeros / empty regardless of compliance. If you observe
the per-source telemetry empty while running a 0.50.21+ daemon,
check ``__version__`` of the bundled ``azt_collab_client``
first; 0.50.37+ is required for the fields to actually reach
peer code.

#### Required peer rendering (when prefetching)

The progress indicator the peer already shows for the
``cached / total`` count gains a one-line source breakdown so the
user can verify the LAN-share is producing hits. Two render
shapes work; pick the one that fits your status surface:

**Inline source tag** — minimal, fits any one-line banner:

```python
def _tick_cache_status(self):
    status = cawl_cache_status(self._cache_status_langcode)
    cached, total = status['cached'], status['total']
    from_lan = status.get('from_lan', 0)
    from_upstream = status.get('from_upstream', 0)
    last_source = status.get('last_source', '')
    # ...existing offline/circuit_open branches...
    # When a fetch is in progress, append "via LAN" / "via Internet"
    # so the user sees which channel served the most recent byte.
    tag = ''
    if last_source == 'lan':
        tag = _('  · via LAN')
    elif last_source == 'upstream':
        tag = _('  · via Internet')
    self._show_cache_indicator(
        _('Caching images: {cached} / {total}{tag}').format(
            cached=cached, total=total, tag=tag))
```

**Breakdown line** — more detail, useful for diagnostic /
field-tester screens:

```python
def _tick_cache_status(self):
    status = cawl_cache_status(self._cache_status_langcode)
    # ... existing setup ...
    from_lan = status.get('from_lan', 0)
    from_upstream = status.get('from_upstream', 0)
    from_cache = status.get('from_cache', 0)
    if from_lan or from_upstream or from_cache:
        breakdown = _(
            '{lan} from LAN · {wan} from Internet · {cache} already cached'
        ).format(lan=from_lan, wan=from_upstream, cache=from_cache)
        self._show_cache_breakdown(breakdown)
```

#### What good and bad look like (so the user can read the tag)

- **`from_lan` climbing, `from_upstream` flat** — paired LAN peer
  has the bytes cached and is serving them. No metered-link cost.
  This is what the NOTES #3 share path is supposed to produce in
  the "one phone seeded the cache, the next phone is filling its
  own" case.
- **`from_upstream` climbing, `from_lan` flat** — either no
  paired peer has the bytes yet (both phones starting from
  cold), or the LAN peer isn't reachable (different Wi-Fi,
  paired-peer record stale). Bytes are pulled over WAN — the
  user is paying cellular if that's the active connection.
- **`from_cache` climbing alone, both `from_lan` and
  `from_upstream` near zero** — most bytes were already on disk
  from a prior session. Normal on a re-open of a previously-
  warmed project.
- **`last_source == ''`** — no fetch has succeeded yet this
  session. Either the prefetch hasn't started serving (initial
  state) or every attempt has failed (look at the ``circuit_open``
  flag for the failure mode).

#### Polling considerations

Same 1 Hz cadence as the existing fields; the new counters are
all monotonic-increasing during a single prefetch session and
all reset to zero when a fresh ``start_prefetch`` is fired. Log
on state change as before — the source counters move fast during
an active prefetch, so log the *deltas* (``from_lan - prev_lan``,
etc.) rather than absolute values to keep the log readable.

#### Pre-0.50.21 fallback

Default to zero on every counter so the peer keeps working
against an older daemon. If you want a visible "this daemon
predates source telemetry" indicator, gate on the daemon's
reported version (compat probe gives you ``server_version``) and
skip the source tag entirely below 0.50.21 — but the simpler
zero-fallback is fine for production.

Where the indicator lives is peer-specific (collab screen
status line, persistent toast, banner above the main
content) — what matters is that it's visible during the
natural waiting moments AND the wording makes clear that
network is being used. "Loading…" doesn't cut it; the user
already assumes loading. The phrase to convey is "don't
disconnect."

**Polling cadence.** 1 Hz is the right interval — feels live,
and the daemon's ``cache_status`` is O(1) (in-memory dict
lookups). Cancel the ``Clock.schedule_interval`` once
``cached >= total`` so an idle peer doesn't keep waking the
daemon for a completed prefetch. **Keep polling while
``offline`` or ``circuit_open`` is true** — the daemon's
scheduler watcher fires ``cawl.on_online_edge()`` on the
offline → online edge (within
``connectivity_poll_s`` ≈ 30 s) which re-triggers
``auto_prefetch``; the running 1 Hz poll is what lets the
banner flip from "offline — will resume" to live progress
automatically when that happens. Log only on state change;
a fixed 1 Hz log of unchanged values is just noise.

**Auto-resume from offline.** No peer action required when
the device goes from offline back to online. The daemon's
``scheduler._watcher_loop`` detects the edge, calls
``cawl.on_online_edge()`` which clears the auto_prefetch
throttle for every repo in ``skipped_offline`` /
``circuit_open`` state and re-fires ``auto_prefetch``. Worst-
case latency is one ``connectivity_poll_s`` (default 30 s).
Look for ``[cawl] online-edge retry: repo=...`` in logcat to
confirm the edge fired.

**On-demand still works.** Peers don't have to wait for the
prefetch to finish before opening individual images. The
``CAWLHandle(...).open_read`` path serves from cache or
fetches on demand; if the prefetch worker hasn't reached a
specific image yet but the user navigates to it, the
on-demand request fetches it directly (and the worker will
skip it later via the cache-hit fast path).

**Backward compatibility.**

- **Pre-0.41.21 daemon** doesn't auto-trigger via
  ``_touch_project``. Peers that ship the new "no explicit
  prefetch call" wiring against a pre-0.41.21 daemon will
  see a stuck indicator at the index's no-prefetch fallback
  counts (``files on disk`` vs. ``index image-shaped entries``).
  Until your install base is on 0.41.21+, keep an explicit
  ``cawl_prefetch(langcode, paths)`` call on project-load as a
  fallback — it's idempotent against the daemon-driven
  prefetch.
- **Pre-0.41.21 daemon, ``cache_status`` flags.** ``offline``
  / ``circuit_open`` / ``finished`` are absent in pre-0.41.21
  responses. The peer pattern uses ``status.get(...)`` with
  ``False`` defaults so the rendering degrades to the "active
  progress" branch when the flags are missing — correct
  behavior for pre-0.41.21 daemons (their iteration didn't
  short-circuit on offline, so the active-progress branch is
  what you want).
- **Pre-0.41.11 daemon** returns ``not_found`` for
  ``cawl_prefetch``; the wrapper returns
  ``{requested: 0, completed: 0, finished: True}`` and the
  peer's progress poll sees the no-prefetch fallback semantics
  of ``cache_status``.
- **Pre-0.41.9 daemon** returns ``not_found`` for
  ``cache_status`` too; the wrapper returns
  ``{cached: 0, total: 0}``, which trips the "nothing to show"
  branch and hides the indicator.

Either way: no peer-side version pin needed; call sites
degrade gracefully.

## 11. Per-project overrides (`cawl_image_repo`, `repo_slug`)

Two per-project string fields live on the daemon's project
record. Both default to empty; non-empty values override
default behaviour.

**Read access.** Both fields surface on every project record
the daemon serves — ``open_project(langcode)``,
``project_status(langcode)``, ``list_projects()`` — so peers
read them in the same RPC that fetches anything else about a
project. The mirrored ``Project`` dataclass on the client
side carries both as ``str`` fields defaulting to ``''`` for
forward-compat with pre-0.39 daemons.

**Setters.**

```python
from azt_collab_client import set_cawl_image_repo, set_repo_slug

set_cawl_image_repo(langcode, 'owner/repo')   # or '' to clear
set_repo_slug(langcode, 'my-vanity-name')      # or '' to clear
```

Both return the updated ``Project`` (so a UI can confirm) or
``None`` on transport failure / unknown langcode.

**``cawl_image_repo`` — per-project CAWL image set.** Empty
means the project uses the daemon-global default
(suite-canonical CAWL repo). Set it when a project needs a
fork or culturally specific image set distinct from the
suite default. Read access is usually internal —
``cawl_index(langcode)`` and ``CAWLHandle`` resolve it for
you; peers only need to read the field if they want to
display the slug in a settings UI.

**``repo_slug`` — per-project publish-repo override.** Empty
means callers should treat as equal to ``langcode`` (the
typical case: publish to a repo named after the langcode).
Non-empty means the user explicitly chose a different repo
name. The publish-path UI should seed any
"GitHub repo name" textbox from ``project.repo_slug or
project.langcode`` and persist user edits via
``set_repo_slug`` so the override survives relaunch.

**Don't mirror these in peer prefs.** The daemon is the
single authoritative source. A peer that caches the slug in
its own prefs has to keep two copies in sync, and they will
drift. Read each time.

## 12. Commit identity (contributor + device_name)

As of 0.40.0 the daemon owns the git commit author identity
end-to-end. Peers DO NOT pass a contributor name on the wire;
the daemon resolves from its store every time it issues a
commit.

**Required calls.**

```python
from azt_collab_client import (
    get_contributor, set_contributor,
    get_device_name, set_device_name,
)
```

- ``set_contributor(name)`` — persist the user's display name
  (the human visible in ``git log``). Empty string clears.
- ``get_contributor()`` — read it back. Empty string means
  unset; the user has not entered a name yet.
- ``get_device_name()`` — read the device-name label. Auto-
  populates from the OS on first call (Android:
  ``Settings.Global.DEVICE_NAME`` → ``Build.MANUFACTURER +
  MODEL``; desktop: ``socket.gethostname()``), so a non-empty
  string comes back on a fresh install.
- ``set_device_name(name)`` — override the autodetected
  label. Empty clears and re-triggers OS autodetect on next
  read.

The daemon composes the git author identity as
``<name> <safe_contributor>@<safe_device>`` — author name
groups GitHub commits by human, email distinguishes commits
across the same human's devices.

**Hard rules.**

- **Don't pass ``contributor=`` to any RPC wrapper.** The
  signature is gone from ``init_project``, ``sync_project``,
  ``commit_project`` (and its legacy ``request_sync`` alias)
  as of 0.40.0. Pre-migration peer code that still passes it
  in the body will have the value silently ignored by the
  daemon — but you should remove the call-site arg as part of
  the upgrade so the code matches the wire.
- **Don't mirror ``contributor`` or ``device_name`` in peer
  prefs / config / globals.** Same single-source-of-truth
  rule as the per-project settings in § 11. Read each time
  via ``get_contributor()`` / ``get_device_name()``.
- **Handle ``S.CONTRIBUTOR_UNSET``** as a routing-class
  failure on user-initiated sync: surface a toast / dialog
  with "Please set your name in sync settings" + a button to
  ``open_server_ui()`` (which lands the user on the daemon
  settings UI where ``set_contributor`` is wired). On
  auto-sync, silence (same shape as other config-class
  failures per the daemon-client contract).

**Sync-result routing.** ``CONTRIBUTOR_UNSET`` fits the
existing routing table from the daemon-client contract:
treat it like ``AUTH_REQUIRED`` / ``NOT_A_REPO`` — silent on
auto-sync, actionable + route-to-settings on user-initiated
sync. The status is returned by:

- ``init_project(...)`` synchronously (publish flow).
- ``sync_project(langcode)`` synchronously.
- ``poll_job(job_id)`` for an async ``commit_project`` that the
  scheduler refused at exec time (defence-in-depth).

**UI for setting these.** Both fields should live on the
daemon settings UI's "User identity" surface — the daemon
settings UI is the canonical home (peer apps delegate via
``open_server_ui()``). A peer with its own first-run flow
MAY prompt for the contributor name inline to spare the user
the round-trip to settings on day one, but the persist call
goes through ``set_contributor`` exactly the same way; the
data lives on the daemon.

## 12b. Project-bound actions live in the daemon settings UI

Since daemon 0.41.0, three project-bound actions are hosted in
the daemon's settings UI (bound to ``last_project()``):

- **Publish** — initialise the project's git repo and push to
  a freshly-created GitHub repo. Was the original daemon-UI
  resident; remains there.
- **Grant collaborator access** — invite a GitHub user to the
  current project (details in § 13).
- **Share this repo (QR)** — render the remote URL as a QR for
  pairing with another device. Paired with the picker's clone-
  flow "Scan QR" affordance.

Peers expose **one** "Open Sync Settings" button instead of
maintaining per-peer sub-screens for these:

```python
from azt_collab_client import open_server_ui

def _on_sync_settings_btn(self, *_):
    open_server_ui(on_status=self._set_log)
```

The user lands on the SettingsScreen with the right
project's langcode + remote URL already on display — no
peer-side disambiguation needed.

### Phase 1 / Phase 3 sequencing constraint

**Do NOT ship a peer release that strips its old per-project
sub-screens in the same version that first adopts the daemon-
UI delegation.** A user still running the old server APK on
the same device would lose the feature entirely — their peer
no longer offers the action, and the daemon UI they could
fall back on hasn't been deployed yet.

The safe rollout is:

- *Phase 1:* peer adopts ``open_server_ui()`` for the action
  while keeping the legacy per-project sub-screen wired as
  fallback.
- *Phase 3:* one peer release later (after most users have
  updated the server APK), strip the legacy sub-screen.

If your peer hasn't shipped a Phase-1 release yet, do that
first; combining the two into one release is the regression.

## 13. Granting collaborator access

**Recommended (azt_collabd 0.41.0+): delegate to the daemon
settings UI.** The daemon now hosts a "Grant collaborator
access" button on the SettingsScreen, bound to
``last_project()`` — peers expose a single "Open Sync
Settings" button (``open_server_ui()``) and the user does the
invite in the daemon UI. No per-peer UI to maintain; the
"user can't be confused about which repo" disambiguation
guarantee is carried by the daemon UI's project-identity
display.

```python
from azt_collab_client import open_server_ui

def _on_invite_btn(self, *_):
    # Delegate; the daemon UI shows the current project's
    # langcode + remote URL and the invite UI inline.
    open_server_ui(on_status=self._set_log)
```

**Direct invocation (if your peer hosts its own per-project
surface):** wire ``grant_collaborator_popup`` from the shared
client UI module. Same contract — langcode-driven, daemon
owns the project lookup — just hosted on the peer instead of
the daemon.

```python
from azt_collab_client.ui import grant_collaborator_popup

def _on_invite_btn(self, *_):
    if not self._current_langcode:
        return
    grant_collaborator_popup(
        langcode=self._current_langcode,
        font_name=self._font_name,
        on_done=lambda result: self._refresh_after_invite(),
    )
```

The popup looks the project up server-side via
``project_status(langcode)`` and displays the langcode + remote
URL prominently before the user types a username. Project
disambiguation is the load-bearing UX guarantee here — peers
must NOT pre-resolve the URL or owner/repo and pass them in
themselves; the daemon owns the lookup so the wrong-repo failure
mode is unreachable.

Underneath, the popup calls
``azt_collab_client.grant_collaborator(langcode, username,
level='push')`` against ``POST /v1/projects/<lang>/collaborators``.
Direct callers (e.g. a CLI helper, or a peer that wants to roll
its own UI rather than use the popup) get a ``Result``
carrying one of:

- ``S.COLLABORATOR_INVITED`` — invitation issued; the user must
  still accept it on GitHub before they can clone or sync.
- ``S.COLLABORATOR_ALREADY`` — already a collaborator (or has a
  pending invite); no new state on GitHub.
- ``S.INVALID_USERNAME`` — empty / whitespace username.
- ``S.NO_REMOTE`` — project has no remote URL configured.
- ``S.NOT_GITHUB_REMOTE`` — remote isn't a github.com URL
  (GitLab + self-hosted aren't supported by this endpoint yet).
- ``S.AUTH_REQUIRED`` — no GitHub token on file for the host;
  user needs to connect via the server APK's settings UI first.
- ``S.COLLABORATOR_INVITE_FAILED`` — GitHub returned an
  unexpected error; ``error`` param carries the underlying
  message.

Translations live in ``azt_collab_client/translate.py``; if you
roll your own UI, run results through ``translate_result()``
rather than substring-matching on codes.

**Scope** (v1):

- GitHub-only. GitLab has different invite semantics and is not
  yet wired through. The popup surfaces ``NOT_GITHUB_REMOTE``
  cleanly when the user tries it on a non-GitHub project.
- Invite-only. No list-existing or revoke yet; the popup is
  designed to grow either later without restructuring.
- Default permission level ``push`` (matches typical SIL
  collaborator flow). Override via the ``level`` arg if you need
  something else; valid GitHub values are ``pull`` /
  ``triage`` / ``push`` / ``maintain`` / ``admin``.

## 14. Smooth UI across reloads

A peer's view of project data has two backing stores: the
canonical bytes on disk (owned by the daemon, refreshed by
sync / merge), and whatever the peer caches in memory to
render its UI. Anything that updates the on-disk bytes —
``sync_project`` returning ``S.PULLED``, a future
``MERGED_REMOTE`` after a remote-driven update, a peer-
triggered re-clone — invalidates the peer's in-memory view.

**Principle.** When the on-disk bytes change underneath the
peer, the user's current view should *refresh in place*:

- **Same context.** The user stays on whatever screen / entry /
  scroll position they were on. Sync is not a navigation event.
  Don't reset to the picker, don't jump to a different
  langcode, don't scroll to top, don't close an open detail
  panel.
- **Visible changes are evident.** If the data the user is
  *looking at* changed (an entry someone else edited, a new
  field, a freshly attached audio reference), the peer renders
  the new values so the change is observable. "Same context"
  doesn't mean "freeze the pixels" — it means "same anchor,
  fresh content." Real upstream deletions also propagate
  normally — if the entry the user is viewing was actually
  deleted in the LIFT, that's worth showing. (LIFT workflows
  rarely delete entries, so this case is mostly theoretical
  but shouldn't be papered over.)
- **No other navigation changes.** New entries elsewhere in
  the dataset, deletions of entries the user isn't viewing,
  reorderings — all reflected in lists / counts / search
  results, but the user's focus point doesn't move.
- **Suspend client-side filters that would hide the current
  view.** Peers commonly filter the dataset by some UI toggle
  ("show past data on/off", "audio recorded only", "this
  contributor's entries"). If a refresh would cause the
  currently-viewed entry to fall outside the active filter
  (e.g. an entry that's now older than the freshly-arrived
  cutoff), drop or relax the filter so the entry stays
  visible. The trigger is *the user's current anchor*, not
  the filter's intent — the user shouldn't watch their entry
  disappear because the data clock advanced. (Recorder
  example: a "don't show past data" toggle that would
  exclude an entry the user is mid-edit — disable the toggle
  for this view rather than swap the entry out.)

How a peer implements this depends on its model layer (the
recorder has a per-entry KV view + a DOM-style LIFT model;
a future viewer might have a virtualised list; a CLI helper
has none of this). The contract is the principle, not the
recipe.

```python
# Example shape (adapt to your model):
from azt_collab_client import sync_project, LiftHandle, S

def _on_sync_tap(self, *_):
    result = sync_project(self._current_langcode)
    if result.has(S.PULLED):
        self._refresh_in_place()
    # ... surface other status codes through translate_result ...

def _refresh_in_place(self):
    anchor = self._snapshot_view()           # guid + scroll + tab
    self._reload_model_from_disk()
    entry = self._model.find_by_guid(anchor.guid)
    if entry is None:
        # Real upstream deletion — let the natural empty-state
        # render so the change is visible.
        self._render_empty_after_delete(anchor)
        return
    if self._active_filter_would_hide(entry):
        # Filter would exclude the current view post-refresh
        # (e.g. "show past data" toggle now excludes this
        # entry's date). Suspend the filter for this view so
        # the user's anchor stays present, even though the
        # data clock moved.
        self._suspend_filter_for_current_view()
    self._render_entry(entry, scroll_y=anchor.scroll_y)
```

If your peer caches derived state (a sorted entry list, a
search index, a recent-projects ribbon), recompute it from
the freshly-parsed model before re-rendering — the cache is
yours to invalidate.

The same principle applies any time the peer detects external
mutation: an Android picker re-entry that returned the same
project, a daemon-restart notice, a future `MERGED_REMOTE`
status. The user's anchor stays; the content under it
refreshes; nothing else moves.

## 14a. Reconciling project switches that happened elsewhere

The user may switch projects from outside the peer — most
commonly via the daemon settings UI's "Switch project"
button (server-APK side, see daemon CHANGELOG 0.41.x), but
also any future path that ends in
``daemon.set_last_langcode(new)`` while the peer's
``_current_langcode`` is still ``old`` in memory. Without a
reconciliation hook, the peer comes back to the foreground
still rendering ``old`` — opposite of what the user just
asked for.

**Peer contract** (since daemon 0.41.x):

On ``App.on_resume``, the peer MUST:

1. Read ``last_project()``.
2. Compare against the peer's currently-loaded langcode.
3. If they differ, reload the project the same way the
   peer would on initial load (close current LIFT, open
   the new one, refresh UI to the entry list / picker
   resume point).

```python
from azt_collab_client import last_project

class YourApp(App):
    def on_resume(self):
        try:
            server_langcode = (last_project() or '').strip()
        except Exception:
            return  # transport failure — leave current view alone
        if not server_langcode:
            return
        peer_langcode = (self._current_langcode or '').strip()
        if server_langcode == peer_langcode:
            return
        # User switched projects elsewhere. Reload to match.
        self._load_project(server_langcode)
```

The "same way as initial load" matters — every peer already
has a `_load_project(langcode)` / equivalent for opening a
project from the picker; reuse that path. The reconciliation
is a *trigger*, not a separate code path.

**What "differ" means.** Compare the daemon's stamped
langcode to whatever the peer treats as its loaded-project
identity (typically ``_current_langcode``, the langcode
returned by the last ``open_project`` / picker result). A
fresh server-side switch always lands ``last_project()`` on
a value before the peer resumes, so the comparison is
authoritative.

**What an empty return means.** ``last_project()`` legitimately
returns ``''`` exactly once in a device's lifetime: on first
boot, before any project has ever been touched. The key in
``$AZT_HOME/config.json :: recent.last_langcode`` doesn't
exist yet, so the getter returns ``''``.

**Picker-cancel writes nothing.** When the user opens the
picker and backs out without choosing, the picker issues no
``POST /v1/recent/last_project`` and no other write. The
daemon's ``last_langcode`` is whatever it was before the
picker opened; the peer's ``_current_langcode`` is whatever
it was before the picker opened; comparison is equal; no
reload. The "I changed my mind" gesture is naturally a no-op
end-to-end. Don't add a "clear" RPC for it.

**Exception — first-boot picker-cancel: `App.stop()` is
correct.** When a fresh install opens the bootstrap picker
(no project has ever been touched, peer's
``_current_langcode`` is unset, daemon's ``last_langcode``
key is absent so ``last_project()`` returns ``''``) and the
user hits OS back without picking anything, the peer has
literally nothing to display and the user has signaled "I
don't want this app open right now." ``App.stop()`` is the
right gesture here. The discriminator is the peer's own
state — ``_current_langcode is None`` *and* picker came back
without a selection — not the empty return from
``last_project()``. This is the **only** circumstance where
``App.stop()`` should fire during a picker / on_resume flow;
see "What NOT to do" below for the contrast.

**Daemon-side invariants (since 0.43.5).** ``store.set_last_langcode()``
refuses empty input (warns and no-ops). ``POST /v1/recent/last_project``
with empty body returns ``400 empty_langcode``. So a
transient bug — mid-rename, mid-merge, malformed RPC, a peer
that accidentally sends empty — cannot land ``''`` as a
stored value. The only path to empty from ``last_project()``
is the first-boot-key-absent case; if you see empty in any
other circumstance, it's a daemon-side regression.

**What NOT to do:**

- ❌ **Call ``App.get_running_app().stop()`` / ``sys.exit()``
  / any other process-exit path as the "reload" mechanism
  when the peer has a loaded project.** The
  first-boot-no-project case carved out above is the lone
  exception; in any other state — including the more
  dangerous "we had a project loaded and now ``last_project()``
  returned something we didn't expect" — ``stop()`` loses the
  user's place and looks like a crash. Tempting because it
  gets the user back to a fresh state cheaply, but Android
  does NOT auto-restart a peer that exits via ``App.stop()``
  — the user just sees the app close and has to relaunch
  from the home screen. Field symptom (2026-05-18): user
  toggled daemon logging on, asked to switch projects, peer
  GET ``/v1/recent/last_project`` → 2 ms later Kivy logged
  ``[INFO] [Base] Leaving application in progress... Python
  for android ended.`` — daemon process kept running and
  finished the in-flight ``[commit]`` 250 ms later, but the
  user perceived the
  whole thing as "app crashed when I switched projects."
  Reload state in-place via the same code path your initial
  project-load uses; don't exit the process.
- ❌ Re-read on every cache_status poll or other 1 Hz tick.
  The check is an Activity-resume event, not a constant
  poll. The user's "switch happened" gesture is bounded by
  Activity lifecycle.
- ❌ Block ``on_resume`` on a slow RPC. ``last_project()``
  is cheap (in-memory dict lookup daemon-side), but always
  wrap in try / except — the daemon could be down or
  the URI grant stale.
- ❌ Silently swap the loaded project without reloading the
  UI. The user's anchor (current entry, scroll, open
  panels) is in a different project's coordinate space; a
  full reload to the new project's natural entry-list
  start is correct.
- ❌ Ignore the case where the user switched in *and back
  out* before resume. If they ended on ``old``, no reload
  needed — the comparison handles this for free.

**Why peer-side, not daemon-pushed.** The peer's loaded
project is peer-side state (in-memory model, open LIFT
handle, view caches). The daemon has no channel to push
"reload now" — and even if it did, the peer's reload-from-
disk path needs to run on the peer's UI thread. Hooking
``on_resume`` is the natural seam. See ``CLAUDE.md``
"Project-switch reconciliation" for the rationale.

**Backward compatibility.** Peers that don't yet ship the
``on_resume`` hook keep working the way they did pre-0.41.x —
the user's "Switch project" tap silently fails to take
effect, exactly the bad case the daemon-side "Switch
project" button warns about. Daemon-side button is a no-op
without peer-side adoption; ship both halves of the
migration.

## 14b. Sharing text / email / files helpers

Peers that need to dispatch a string or one-or-more files
through Android's share sheet — diagnostic dump, status
report, log attachment, multi-day log bundle, etc. — should
use the shared helpers in ``azt_collab_client.ui.share``
rather than inlining ``ACTION_SEND`` / ``ACTION_SEND_MULTIPLE``
JNI plumbing per peer.

### 14b-i. The two load-bearing constraints

Skip this section once and Signal-compatible shares will
silently fail. Both constraints are receiver-side; we can't
patch around them.

**(1) URI authority.** Privacy-restricted receivers (Signal
in particular) refuse URIs whose authority isn't the
sender's own. MediaStore URIs (``content://media/...``) are
fine for Gmail and most other receivers but Signal drops
them silently. The supported authority for AZT-suite shares
is ``org.atoznback.aztcollab`` (served by
``AZTCollabProvider``). Peer code MUST NOT pass MediaStore
URIs into ``share_files`` for receivers where Signal might
be picked. The legacy ``share_log_file`` is the exception —
it still uses MediaStore for its single-blob path and is
kept only for the recorder team's existing button.

**(2) Action choice by content type.** Signal's
``ACTION_SEND_MULTIPLE`` resolver runtime-filters per-URI
MIMEs to image and video only:

```kotlin
// ShareRepository.kt, verbatim:
.filterValues {
  MediaUtil.isImageType(it) || MediaUtil.isVideoType(it)
}
```

Anything else (text, application/zip, audio outside
music-share contexts) is silently dropped. Signal's
manifest advertises ``text/*`` SEND_MULTIPLE; the runtime
ignores its own manifest. Verified verbatim against the
``main`` branch on 2026-06-22.

Consequence: for non-image/non-video content, USE
``ACTION_SEND`` (single attachment), NOT
``ACTION_SEND_MULTIPLE``. If you have multiple files of
text/log/binary content to share via Signal, bundle them
into a single zip and ship the zip. APKs already travel
through Signal this way (single ``application/*`` URI);
the diagnostic bundle uses the same shape.

``share_files(items=[...])`` does the action routing
automatically: single item → ``ACTION_SEND``, multi-item →
``ACTION_SEND_MULTIPLE``. **Peer code that wants
Signal-compatible multi-file share MUST hand
``share_files`` exactly one item — typically a zip URI.**
The daemon's ``prepare_share_bundle`` returns one zip item
for this reason.

### 14b-ii. Helpers

```python
from azt_collab_client.ui.share import (
    share_text, email_text, share_files,
    share_diagnostics_action, share_log_file)

# 1. Generic text/plain share sheet (email, messaging, cloud-
#    paste, file-saver all accept). EXTRA_TEXT body — limited
#    to Android Intent's ~1 MB extras ceiling. Best for short
#    diagnostic dumps and snapshots.
share_text(
    text=some_short_dump,
    subject=_('Diagnostic snapshot'),
    chooser_title=_('Share snapshot'),
    on_error=self._show_error,
)

# 2. Email-only picker (mailto: URI scheme; ACTION_SENDTO).
#    Restricts the share sheet to email apps — better UX when
#    the user's intent is specifically "send this to the
#    developer". ``to=''`` lets the user pick a recipient.
#    Body lives in the URI's ``body`` query — practical
#    kilobyte limit; large payloads should prefer share_files.
email_text(
    text=some_short_dump,
    to='',
    subject=_('Diagnostic snapshot'),
    on_error=self._show_error,
)

# 3. File-or-URI share via Android share sheet. Each item is
#    ONE of three shapes:
#
#    (a) {'uri': '<content://… URI>', 'display_name': str}
#        — pre-staged URI from a ContentProvider you control.
#        Used for the diagnostic bundle (URIs served by the
#        server APK's own AZTCollabProvider). The supported
#        authority for AZT-suite shares is
#        ``org.atoznback.aztcollab``.
#
#    (b) {'path': str, 'display_name': str}
#        — peer-owned file on disk. share_files copies the
#        bytes into MediaStore Downloads (with IS_PENDING=0
#        cleared in the same call). Signal-incompatible
#        because the resulting URI is MediaStore. Use for
#        non-Signal receivers only.
#
#    (c) {'content': str|bytes, 'display_name': str}
#        — in-memory blob. share_files writes the bytes into
#        a MediaStore Downloads entry. Same Signal caveat as
#        (b).
#
#    Action routing: len(items) == 1 → ACTION_SEND;
#    len(items) > 1 → ACTION_SEND_MULTIPLE.
#
#    For Signal compatibility with non-image/non-video
#    content, use exactly one item with an archive URI from
#    your own ContentProvider. Multi-item with text content
#    will be silently rejected by Signal (per § 14b-i above).
share_files(
    items=[
        {'uri': 'content://org.atoznback.aztcollab/'
                '_shares/abc123/azt_diagnostics_20260622.tar.gz',
         'display_name': 'azt_diagnostics_20260622.tar.gz'},
    ],
    mime_type='application/gzip',
    chooser_title=_('Share diagnostics'),
    on_error=self._show_error,
)

# 4. Canonical "share diagnostics" composition. Single source
#    of truth for the server-APK settings button, the picker
#    button, and any peer app that wants to ship the daemon's
#    diagnostic bundle. Internally: prepare_share_bundle()
#    RPC returns a single .tar.gz URI, share_files dispatches
#    via ACTION_SEND. The peer just chooses an `on_error`
#    callback for its error-display channel.
share_diagnostics_action(on_error=self._show_error)

# 5. Legacy single-file log share (kept for the recorder
#    peer's existing button). Bundles ``log_path`` and an
#    optional ``prev_path`` into one concatenated text/plain
#    blob with === section === headers, then ships via
#    MediaStore + ACTION_SEND. Signal-incompatible (MediaStore
#    URI). New code should prefer share_diagnostics_action
#    (which uses our own provider's authority).
share_log_file(
    log_path='/sdcard/azt_recorder.log',
    prev_path='/sdcard/azt_recorder.log.prev',  # optional
    on_error=self._show_error,
    # display_name='azt_log_20260513.log',     # optional
)
```

All are Android-only — non-Android platforms invoke
``on_error`` with a translated message; same shape as
``share_running_apk``. All return ``bool`` indicating dispatch
success.

**Picking between them.** Short text (< 100 KB) → either
``share_text`` (broad picker) or ``email_text`` (email only).
Diagnostic bundle → ``share_diagnostics_action``. Peer-built
archive bundles destined for Signal-compatibility →
``share_files`` with one URI item from your own provider's
authority (use a non-zip container — e.g. ``.tar.gz`` — if the
recipient's mail server strips ``.zip``). Image/video bundles where Signal-compat doesn't
matter (or where you're OK with Signal dropping it) →
``share_files`` with multiple URI items. Legacy single-blob
text bundles where Signal-compat doesn't matter →
``share_log_file``.

### 14b-iii. Daemon-side share-staging (since 0.52.13)

The authoritative way to get the diagnostic bundle into a
shareable shape is the daemon RPC. Peer code SHOULD NOT
write share files itself — see § 14b-i constraint (1).

```python
from azt_collab_client import prepare_share_bundle
from azt_collab_client.transports.android_cp import CANONICAL_AUTHORITY
from azt_collab_client.ui.share import share_files

bundle = prepare_share_bundle()
# Single-file bundle since 0.52.19; a .tar.gz since 0.52.23
# (a field mail server strips .zip — gzip magic dodges the
# filter). Shape:
# {'token': '<hex>',
#  'items': [
#    {'display_name': 'azt_diagnostics_20260622_143052.tar.gz',
#     'uri_path': '_shares/<token>/azt_diagnostics_20260622_143052.tar.gz'},
#  ]}
# The archive contains the snapshot and per-day daemon logs
# (each as a separate entry) — files stay separate inside
# so support's first action is untar+grep.

items = [
    {'uri': f'content://{CANONICAL_AUTHORITY}/{it["uri_path"]}',
     'display_name': it['display_name']}
    for it in bundle.get('items') or []
]
share_files(items, mime_type='application/gzip', on_error=...)
```

Most peers should just call ``share_diagnostics_action``
which does this composition automatically.

**Peers shipping their own files alongside the daemon
bundle.** If you want to include peer-side files (e.g. the
recorder's own log) in the same share, you need to combine
them into your own zip first (so the share is still one
ACTION_SEND-shaped attachment that Signal will accept) — or
declare your own FileProvider for the peer-owned file and
ship a multi-item ``share_files`` knowing Signal will reject
the share but other receivers will accept it. The simpler
pattern: build your own archive containing both daemon-bundle
contents (read via ``get_daemon_log_files``) and peer-owned
content, save under your FileProvider authority, ship via
``share_files`` with one URI item.

**Use the shared format helper — don't hand-roll the tar
(since 0.52.27).** A peer building its own diagnostics bundle
MUST use ``azt_collab_client.diagnostics`` so the container
format can't drift from the daemon's (the zip→tar.gz change
shipped stale in the recorder for a build because the format
was duplicated). Collection/staging/dispatch stay yours; only
the format is shared::

    from azt_collab_client.diagnostics import (
        build_diagnostics_targz, diagnostics_archive_name,
        DIAGNOSTICS_MIME)

    name = diagnostics_archive_name(slug='recorder', stamp=stamp)
    build_diagnostics_targz(
        dest_path,
        file_items=[(n, os.path.join(log_dir, n)) for n in my_logs],
        content_items=[(e['filename'], e['content'])
                       for e in get_daemon_log_files().get('files', [])])
    share_files([{...uri..., 'display_name': name}],
                mime_type=DIAGNOSTICS_MIME)

**Daemon log file access** (for peers that need raw content
rather than URIs) — ``get_daemon_log_files()`` returns the
per-day daemon logs as text strings (each
daemon-side-truncated to ~256 KB). Useful for in-app log
viewers, console previews, peer-side zip-building. NOT for
direct share dispatch as ``'content'`` items, since those
go through MediaStore which Signal rejects.

**TTL on the share bundle.** ``prepare_share_bundle`` sweeps
stale ``$AZT_HOME/.shares/<token>/`` directories older than
1 hour on every call. The TTL is generous because some
receivers (Signal especially) hold the URI in a compose
draft and don't read the file until the user actually sends
the message — minutes after the chooser closes. Don't keep
a ``token`` cached across an app restart; call
``prepare_share_bundle`` again to get a fresh one.

**On-disk daemon log filenames.** Per-day daemon log files
are named ``daemon-<peer>-YYYY-MM-DD_log.txt`` (since
0.52.20; pre-0.52.20 was ``.log``). The ``_log.txt`` suffix
keeps "log" in the basename for grep / triage while making
the actual extension ``.txt`` so text editors with
extension-based syntax detection treat the file as text.
Inside the diagnostic zip the same filenames are used as zip
entry names; receivers unpacking the archive see ``.txt``
files their editor opens directly.

### 14b-iv. Legacy share_log_file blob shape

```
=== previous session (<prev_path>) ===
<prev contents>

=== current session (<log_path>) ===
<current contents>
```

The daemon-side ``<path>.prev`` rotation mechanism was
removed in 0.52.5 — passing ``prev_path`` is still legal
(the helper silently skips a nonexistent path) but the
canonical daemon log path no longer has a ``.prev`` sibling.

### 14b-v. Why peer-shared

Multiple surfaces need to dispatch through the share sheet
(recorder log, daemon log bundle, recorder status snapshot,
picker Share-diagnostics, server-APK settings Share-
diagnostics). Each was about to re-derive ~30-50 lines of
jnius autoclass + Intent construction + MediaStore plumbing
+ error translation, plus discover Signal's manifest-vs-
runtime mismatch independently. Extracted into ``ui/share.py``
alongside ``share_running_apk`` so every peer + the daemon UI
uses the same code path, and a future tightening of Android
share APIs (or another receiver-specific quirk) is one fix
instead of N.

## 15. Recovery

The single peer-visible recovery surface is
``Result.has(S.JOB_INTERRUPTED)`` from ``request_sync`` /
``poll_job``. Treat it as transient and retry. Synchronous
``sync_project`` callers don't see this code — the transport
retries internally.

## 16. Testing

The suite has a pytest scaffold in the canonical
``azt-collab/tests/`` directory. Run ``pytest tests/ -q`` from the
canonical repo before publishing a client-bundling peer. Manual
matrix is in ``azt-collab/docs/test_plan.md`` § 8 (canonical-only;
not symlinked into peers).

### Boot-trace instrumentation

Both peers and the daemon emit ``[boot-trace-peer] phase=…
t=…`` and ``[boot-trace-daemon] phase=… t=…`` lines to
stderr → logcat at every cold-start cost-center. These are
cheap (≤ 10 lines per launch) and shipped enabled. **Don't strip
them** if you have a logcat filter — the harness depends on
them to compute timing tables, and they're load-bearing for
diagnosing slow-tablet field reports.

Phases the peer emits (from ``azt_collab_client/ui/bootstrap.py``):
``bootstrap_called``, ``compat_probe attempt=N``, ``compat_ok``,
``bootstrap_done``, plus ``prewarm_*`` if ``prewarm()`` is wired
in.

Phases the daemon emits (from ``server_apk/service.py``):
``module_loaded``, ``main_entered``,
``before_import_azt_collabd`` / ``after_import_azt_collabd``,
``configured``, ``before_install_callbacks`` /
``after_install_callbacks``, ``before_reconcile`` /
``after_reconcile``, ``entering_idle_loop``.

### Cold-start measurement harness

``tests/integration/measure_boot.sh`` drives a real Android
device through ``baseline``, ``doze``, ``prewarm``, and
``doze+prewarm`` scenarios. Run it on each device class your
peer's users have (a fast phone + a slow tablet covers the
useful range) before deciding whether to wire ``prewarm()`` into
``App.build()``. The harness's README at
``tests/integration/README.md`` covers prerequisites, scenario
semantics, and how to interpret the per-iteration summaries.

Useful intervals to read off the summary:

- **peer wait until daemon answered** — what the user actually
  feels; the number you're trying to drive down.
- **daemon Python boot to dispatcher live** — where most of the
  time goes on slow tablets; ``import azt_collabd`` is the long
  pole.
- **prewarm overlap window** — only present if your peer calls
  ``prewarm()``; the gap between that and ``compat_ok`` tells
  you whether the overlap actually saved anything.

If you ship a new peer, run the harness once before tagging
your first release; if its `peer wait` interval is consistently
> 5 s on the slow-tablet target, wire ``prewarm()`` and re-measure
before tagging.

## 17. Routing on sync results

How peers MUST respond to ``sync_project`` / ``commit_project``
result codes. Future peers (viewer, next sister app) match the
existing behaviour by following this section — without it, every
peer reverse-engineers the routing from peer source and the
behaviours drift.

> **Naming note (0.43.0).** ``request_sync`` was renamed to
> ``commit_project`` and narrowed: it is commit-only. Push is
> driven entirely by the daemon's scheduler-drain loop (online +
> ``sync.post_online_grace_s`` + ``sync.work_offline``). The old
> ``request_sync`` name still works as a backwards-compat alias
> in the client, but result-handling that polls for ``PUSHED``
> on this path is now incorrect — ``commit_project`` never
> emits ``PUSHED``. Peers MUST migrate their post-commit logic
> off "did we push?" and onto the daemon's drain state via
> ``project_status.wan_unshared`` / ``project_status.work_offline``.

> **For the rationale** (what each code *means*, why
> auto-sync must be silent, the pre-0.34.1 anti-pattern this
> contract closes) — see ``CLAUDE.md`` "Peer contract: routing
> on sync results". This section is the *contract*.

**Two contexts, two contracts.** Two different RPCs surface
``Result``-shaped responses, and even within one RPC the same
``Result`` reaches the peer from two different triggers:

1. **Auto-commit** — the peer fires ``commit_project`` itself,
   without a user gesture: project-select, project-load,
   background periodic, post-edit debounce. The user did NOT
   ask to push. The result NEVER carries ``PUSHED`` — push is
   the daemon scheduler's job — and configuration-class
   failures MUST be silent.
2. **User-initiated sync** — the user tapped a sync icon /
   "Sync now" button. The user explicitly asked. The peer
   calls ``sync_project`` (commit + push). Configuration-class
   failures route to whatever fixes the problem.

Auto-commit MUST be silent on configuration-class failures.
User-initiated sync routes. New in 0.43.0: ``S.WORK_OFFLINE_ENABLED``
is a user-initiated-only refusal — the peer routes the user to
the sync settings screen anchored on the work-offline toggle
(same pattern as ``AUTH_REQUIRED`` → credentials).

### Routing table

| Status code | Auto-sync | User-initiated sync |
|---|---|---|
| ``S.NOT_A_REPO`` | **Silent.** Log; project keeps working. | Route to publish / collaboration settings. |
| ``S.NO_REMOTE`` | **Silent.** Log; project keeps working. | Same — route to publish settings. |
| ``S.AUTH_REQUIRED`` | **Silent.** Log; sync doesn't happen until creds configured. | Route to GitHub Connect flow. |
| ``S.APP_NOT_INSTALLED`` / ``S.APP_SUSPENDED`` / ``S.REPO_NOT_AUTHORIZED`` | **Silent.** Log; project still usable. | Open the ``url`` param the Status carries. |
| ``S.REPO_NO_ACCESS`` | **Silent.** Log; also persisted to ``project_status.last_sync_error`` so a persistent banner can show it. The daemon already tried to auto-accept a pending invite before emitting this. | ``ui.popups.repo_access_popup(owner_repo, url)`` — offers **Open GitHub** (→ the repo/invitations page via ``ui.open_url``) so the user can accept an invite or request access. Params: ``owner_repo``, ``url``. (0.52.24+.) |
| ``S.INVITE_ACCEPTED`` | **Silent.** Transient/retryable — the daemon auto-accepted a pending GitHub invitation for this repo; access should now work and the next drain retries. | **Silent** (or a brief "access granted, syncing" toast). Not an error. (0.52.24+.) |
| ``S.CONTRIBUTOR_UNSET`` | **Silent.** Log; sync refused until name set. | Route to daemon settings UI's contributor field. |
| ``S.WORK_OFFLINE_ENABLED`` | n/a — auto-commit doesn't see this (only ``sync_project`` emits it). | Toast "Work-offline mode is on" + ``open_server_ui()`` to the sync settings screen. The user explicitly turned the toggle on; the Sync button is the only path that surfaces the refusal. (0.43.0+.) |
| ``S.BUSY`` | **Silent.** Daemon's project_lock is held by another caller (almost always *this peer's* prior in-flight sync). Lock clears in milliseconds; the next regular tick covers it. Auto-sync surfaces nothing. | **Silent.** Even on user-gesture: showing "Another sync is in progress" toasts back-to-back is just punishing the user for the peer's missing in-flight guard. Optionally: debounce the Sync button so a fast double-tap fires once. See § 17c for the load-shedding rules that prevent ``S.BUSY`` in the first place. |
| ``S.JOB_INTERRUPTED`` | Retry once silently; if still failing, log and move on. | Retry; surface a transient-error toast if retry also fails. |
| ``S.INSUFFICIENT_MEMORY_FOR_MERGE`` | **Silent.** Daemon refused the merge because device free memory was below ``sync.min_free_mem_mb_for_merge`` (default 200 MB). Next drain cycle re-checks and proceeds when memory recovers — nothing the user can do mid-recording, and toasting "not enough memory" while they're working is just noise. Params: ``mem_available_mb`` (int), ``min_required_mb`` (int). 0.44.4+. | Translated toast naming the numeric headroom — the user explicitly asked, so they get the "close other apps, I'll retry" message. Translation already covers the wording. DO NOT route to settings — no per-project knob fixes RAM pressure. |
| ``S.SERVER_UNAVAILABLE`` / ``S.SERVER_ERROR`` | **Silent.** Log; daemon will be reachable next time. | Transient-error toast. DO NOT route to settings — no user-fixable config here. |
| ``S.DNS_RESOLUTION_FAILED`` | **Silent.** Network class — same envelope as ``SERVER_UNAVAILABLE``; daemon will resolve next time when DNS is back. (0.44.6+.) | Transient-error toast. DO NOT route to settings. |
| ``S.SYNC_GIVING_UP_TRANSIENT`` | **Silent.** Daemon exhausted its in-process retries for now; the scheduler will pick it back up. (0.44.6+.) | Transient-error toast. |
| ``S.TOPIC_BRANCH_CONFLICT`` | **Silent.** Topic-branch path collided with concurrent state; next drain retries. (0.44.9+.) | Transient-error toast naming the topic-branch dance; no user action. |
| ``S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` | **Silent.** Daemon gave up after the per-attempt budget kept tripping. User can't act mid-flight; next drain re-tries on a different network. (0.44.12+.) | Surface the translated toast (carries reason=oversize/exhausted and a hint about retrying on a different network). |
| ``S.LARGE_AUDIO_FILE_DETECTED`` | **SURFACE (informational).** Daemon flagged a >threshold audio file during commit. Render a banner: "A multi-MB file was committed — was a phrase recorded by mistake?" Doesn't block the commit; purely an audit nudge. Params: ``path``, ``size``, ``threshold``. (0.44.12+.) | Same as auto. |
| ``S.AUTH_REFRESH_STALE`` | **Silent.** (Peers MAY show a non-intrusive settings banner via ``get_credentials_status()`` → ``github.refresh_broken``.) | Surface the translated toast — names GitHub Connect as the next step. DO NOT route, the toast text covers it. |
| ``S.DATA_LOSS_RISK`` | **SURFACE (not silenced).** This is a data-loss-class signal — files written by a peer aren't reaching git. The auto/user distinction does NOT apply: ALWAYS render the translated toast / banner with the maintainer-contact wording. Params: ``count`` (int), ``sample`` (up to 5 paths). | Same surface as auto-sync. |
| ``S.COMMIT_REPEATEDLY_FAILED`` | **SURFACE (not silenced).** Two-or-more successive ``COMMIT_FAILED`` for this project. Same data-loss-class severity as ``DATA_LOSS_RISK``: recordings are accumulating on the device but not entering git history. The catchup-commit pattern (one fat commit landing N stranded recordings after a long failure streak) is exactly what this catches — each prior failed attempt bumps the counter, and a second-or-later failure surfaces the loud status so the user is told to investigate before more files pile up uncommitted. Params: ``count`` (int, running streak), ``error`` (str, last dulwich message). Counter clears on the next successful commit. (The daemon also retries stuck commits in the background with exponential backoff, so the running ``count`` and the ``COMMIT_REPEATEDLY_FAILED`` your peer sees on the next sync attempt may reflect failures the peer never directly triggered. Peers don't need to do anything different — the existing result-iteration handles it.) | Same surface as auto-sync. |
| ``S.AUDIO_SET`` / ``S.ILLUSTRATION_SET`` / ``S.AUDIO_SET_NO_CHANGE`` / ``S.ILLUSTRATION_SET_NO_CHANGE`` | n/a — these are from ``set_audio`` / ``set_illustration``, not sync. | Surgical-edit success / no-op. Update peer UI; suppress feedback on ``_NO_CHANGE``. See § 9a. (0.50.29+.) |
| ``S.ENTRY_NOT_FOUND`` | n/a — surgical-edit only. | Toast (carries ``guid``); reload the peer's entries list — the entry was deleted under us or the in-memory list drifted. See § 9a. (0.50.29+.) |
| ``S.LIFT_INVALID`` | n/a — surgical-edit only. | Translated toast (carries ``error`` detail). Data-quality-class; daemon refused to persist invalid XML and the previous bytes remain on disk. See § 9a. (0.50.29+.) |
| Everything else (``PUSHED``, ``PULLED``, ``NOTHING_TO_COMMIT``, ``CONFLICTS``, …) | Translate to status line. | Translate to status line. |

### Code shape — both contexts

```python
# Auto-commit (post-load, post-edit, background) — silent on
# configuration-class AND transport-class failures; never derail
# whatever the user was doing. Uses commit_project (debounced,
# async, no push), NOT sync_project — auto-paths MUST NOT push
# (see § 17c Rule 2). Push is the daemon scheduler's job.
def _auto_commit(self, langcode):
    job_id = commit_project(langcode)
    if not job_id:
        return  # transport failure already wrapped; silent
    result = poll_job(job_id)
    # DATA_LOSS_RISK / COMMIT_REPEATEDLY_FAILED are NEVER
    # silenced — surface either before any other branch consumes
    # the result. They're the two canaries for "data is
    # accumulating on the device but not entering git history";
    # silencing them would hide active data loss.
    for s in result.statuses:
        if s.code in (S.DATA_LOSS_RISK, S.COMMIT_REPEATEDLY_FAILED):
            self.show_toast(translate_status(s))
    if result.has_any(S.NOT_A_REPO, S.NO_REMOTE,
                      S.AUTH_REQUIRED, S.CONTRIBUTOR_UNSET,
                      S.APP_NOT_INSTALLED, S.APP_SUSPENDED,
                      S.REPO_NOT_AUTHORIZED,
                      S.BUSY,
                      S.INSUFFICIENT_MEMORY_FOR_MERGE,
                      S.SERVER_UNAVAILABLE, S.SERVER_ERROR,
                      S.AUTH_REFRESH_STALE):
        print(f'[auto-commit] {langcode}: '
              f'{result.codes()!r} (silenced)',
              file=sys.stderr)
        return
    if result.has(S.JOB_INTERRUPTED):
        return self._auto_commit_retry_once(langcode)
    self.show_status(translate_result(result))  # COMMITTED_LOCAL, etc.


# User-initiated sync — the user just tapped Sync; route to
# whatever fixes the problem, or surface the success line.
def do_sync(self, langcode):
    result = sync_project(langcode)

    # AUTH_REFRESH_STALE piggybacks on whatever primary code the
    # sync returned. Surface it BEFORE the routing branches
    # consume the result so the deadline warning isn't dropped
    # on the way to a settings page.
    stale = next((s for s in result.statuses
                  if s.code == S.AUTH_REFRESH_STALE), None)
    if stale is not None:
        self.show_toast(translate_status(stale))

    if result.has_any(S.NOT_A_REPO, S.NO_REMOTE):
        self.open_publish_settings(langcode)
    elif result.has(S.CONTRIBUTOR_UNSET):
        self.open_sync_settings()
    elif result.has(S.WORK_OFFLINE_ENABLED):
        self.show_toast(translate_result(result))
        self.open_sync_settings()
    elif result.has(S.AUTH_REQUIRED):
        self.open_github_connect()
    elif result.has_any(S.APP_NOT_INSTALLED, S.APP_SUSPENDED,
                        S.REPO_NOT_AUTHORIZED):
        url = next((s.params.get('url', '')
                    for s in result.statuses
                    if s.code in (S.APP_NOT_INSTALLED,
                                  S.APP_SUSPENDED,
                                  S.REPO_NOT_AUTHORIZED)),
                   '')
        self.open_url(url) if url else self.open_github_connect()
    elif result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR,
                        S.INSUFFICIENT_MEMORY_FOR_MERGE):
        # Transient — no user-fixable config; just say so.
        # INSUFFICIENT_MEMORY_FOR_MERGE: translation carries the
        # numeric headroom + retry promise.
        self.show_toast(translate_result(result))
    elif result.has(S.JOB_INTERRUPTED):
        ...  # retry; transient-error toast if retry fails
    else:
        # PUSHED / PULLED / NOTHING_TO_COMMIT / CONFLICTS / etc.
        self.show_status(translate_result(result))
```

### Constants

The status codes referenced above are module-level constants
in ``azt_collab_client.status`` (re-exported as ``S`` from the
package root). All available since 0.41.13:

```python
from azt_collab_client import S
S.NOT_A_REPO              # 'NOT_A_REPO'
S.NO_REMOTE               # 'NO_REMOTE'
S.AUTH_REQUIRED           # 'AUTH_REQUIRED'
S.AUTH_REFRESH_STALE      # 'AUTH_REFRESH_STALE'
S.DATA_LOSS_RISK          # 'DATA_LOSS_RISK'         ← never silenced
S.COMMIT_REPEATEDLY_FAILED # 'COMMIT_REPEATEDLY_FAILED' ← never silenced
S.APP_NOT_INSTALLED       # 'APP_NOT_INSTALLED'
S.APP_SUSPENDED           # 'APP_SUSPENDED'
S.REPO_NOT_AUTHORIZED     # 'REPO_NOT_AUTHORIZED'
S.REPO_NO_ACCESS          # 'REPO_NO_ACCESS'       (0.52.24+)
S.INVITE_ACCEPTED         # 'INVITE_ACCEPTED'      (0.52.24+, transient)
S.CONTRIBUTOR_UNSET       # 'CONTRIBUTOR_UNSET'
S.JOB_INTERRUPTED         # 'JOB_INTERRUPTED'
S.INSUFFICIENT_MEMORY_FOR_MERGE  # ← 0.44.4+, daemon refused merge under memory pressure
S.WORK_OFFLINE_ENABLED    # 'WORK_OFFLINE_ENABLED' ← 0.43.0+, sync_project only
S.BUSY                    # 'BUSY'                ← project_lock held; silent (§ 17c)
S.SERVER_UNAVAILABLE      # 'SERVER_UNAVAILABLE'  ← 0.41.13+
S.SERVER_ERROR            # 'SERVER_ERROR'        ← 0.41.13+
S.AUDIO_SET               # 'AUDIO_SET'                ← 0.50.29+, § 9a
S.AUDIO_SET_NO_CHANGE     # 'AUDIO_SET_NO_CHANGE'      ← 0.50.29+, § 9a
S.ILLUSTRATION_SET        # 'ILLUSTRATION_SET'         ← 0.50.29+, § 9a
S.ILLUSTRATION_SET_NO_CHANGE  # 'ILLUSTRATION_SET_NO_CHANGE' ← 0.50.29+, § 9a
S.ENTRY_NOT_FOUND         # 'ENTRY_NOT_FOUND'          ← 0.50.29+, § 9a
S.LIFT_INVALID            # 'LIFT_INVALID'             ← 0.50.29+, § 9a
S.PUSHED / S.PULLED / S.NOTHING_TO_COMMIT / S.CONFLICTS / ...
```

Use the constants, not string literals. Substring-matching the
translated message (``if 'publish' in msg: route_to_settings``)
is the regression this section exists to avoid.

## 17b. Commit / push split + work-offline (since 0.43.0)

> **For the rationale** — why commits are peer-driven but
> push is daemon-driven, why the post-online grace exists —
> see ``CLAUDE.md`` "Sync flow: commit / push split". This
> section is the contract.

Peers decide where to cut a commit. The daemon decides when
(and whether) to push.

### Two RPCs

```python
from azt_collab_client import (
    commit_project,     # debounced, commit-only, async
    sync_project,       # synchronous, user-Sync-button only
    get_work_offline,   # read daemon-wide toggle
    set_work_offline,   # write daemon-wide toggle
)
```

- ``commit_project(langcode)`` — what peers fire per group of
  related changes (post-edit debounce, post-record, etc.).
  Debounced server-side; bursts collapse into one commit.
  Returns a ``job_id`` or ``None`` on transport failure.
  Result codes peers poll via ``poll_job(job_id)``:
  ``COMMITTED_LOCAL`` / ``NOTHING_TO_COMMIT`` /
  ``COMMIT_FAILED`` / ``DATA_LOSS_RISK`` /
  ``COMMIT_REPEATEDLY_FAILED`` / ``CONTRIBUTOR_UNSET`` /
  ``NO_REPO``. **Never carries ``PUSHED``** — push happens on
  the daemon's drain loop. Pre-0.43 peer code that polls for
  ``PUSHED`` after ``request_sync`` will sit waiting forever;
  migrate that logic onto ``project_status.wan_unshared``.
- ``sync_project(langcode)`` — the user-gestured "push pending
  commits now" RPC, bound to the Sync button. Does commit +
  push under one project lock. Returns ``Result``
  synchronously. New refusal: ``S.WORK_OFFLINE_ENABLED`` (see
  routing in § 17).

### What the daemon does behind the curtain

The scheduler's connectivity watcher tracks
``_online_since`` on offline→online edges. Every tick (default
30 s), if online for ``≥ sync.post_online_grace_s`` (default
60 s) AND ``sync.work_offline`` is off, projects with
``pending_push`` get pushed. Peers don't poll this — the
``project_status`` response carries the state peers need
(``wan_unshared``, ``lan_unshared``, ``at_risk``, ``work_offline``).

### Work-offline toggle

Daemon-wide bool. When on:

- The watcher's drain is a no-op.
- ``sync_project`` returns ``Result().add(S.WORK_OFFLINE_ENABLED)``
  without attempting any push.
- ``commit_project`` is unaffected — local commits keep
  happening per peer gesture.

Toggling OFF (via ``set_work_offline(False)`` or the daemon
settings UI) fires an immediate push-drain pass so the user
doesn't wait a full ``connectivity_poll_s`` tick.

```python
# v0.47.0 RENDERING MODEL — please read this whole comment block
# before changing anything; the rules are interrelated.
#
# project_status carries five ProjectStatus fields that drive
# the sync indicator. Three are independent count axes (each
# is its own walk over the local commit graph); two are the
# settings toggles that gate auto-resolution.
#
#   1. wan_unshared  — commits whose bytes are not yet on github
#                       — not on the main tracking ref NOR on any
#                       per-device topic ref. Since 0.53.3 this
#                       counts DOWN as a chunked topic-push uploads
#                       history (was: pinned at the full divergence
#                       until the final merge). Special case:
#                       LAN-only projects with no origin URL
#                       walk from HEAD, so wan_unshared equals
#                       the whole history. Intentional friction
#                       signal for "no github backup."
#   2. lan_unshared  — commits not on any paired-and-sharing
#                       peer's last_seen_main. Returns 0 when
#                       no peers are paired (nothing to be
#                       behind on).
#   3. at_risk        — commits on neither channel (intersection
#                       of wan_unshared and lan_unshared as
#                       commit sets). Zero except in state E.
#                       Since 0.53.3 also excludes topic refs
#                       (bytes on a topic ref are on github, so
#                       not at risk) — matches wan_unshared.
#   4. main_merged    — bool, since 0.53.3. True only when the
#                       local tip is fully on github's main. The
#                       gate for "OK": because wan_unshared can
#                       reach 0 while bytes sit on a topic ref
#                       awaiting merge, wan==0 alone no longer
#                       means backed up. Pre-0.53 daemons omit
#                       it; the mirror defaults it True.
#   5. n_changes      — uncommitted working-tree changes.
#                       Drives the always-red R(+n) badge.
#   6. work_offline   — daemon-wide toggle. When on, scheduler
#                       drain does not push to github.
#   7. lan_allow_sync — daemon-wide toggle. When on, LAN
#                       listener / fan-out is armed.
#
# Sync-status labels (wan_done := wan==0 AND main_merged):
#
#   wan_done, lan=0                       → "OK"
#   wan==0, not main_merged, lan=0        → "WAN-0" (finishing:
#                                           all bytes uploaded to a
#                                           topic ref, final merge to
#                                           main pending)
#   wan>0, lan=0                          → "WAN-{wan}"
#   wan_done, lan>0                       → "LAN-{lan}"
#   wan>0, lan>0, at_risk=0  (rare)       → "WAN-{wan}_LAN-{lan}"
#                                           (split-brain — different
#                                            commits on each channel
#                                            with no overlap; requires
#                                            divergent history)
#   wan>0, lan>0, at_risk>0  (routine)    → "WAN-{wan} LAN-{lan}"
#                                           (both behind on the same
#                                            commits, normal transient
#                                            state right after a fresh
#                                            commit; underscore vs
#                                            space distinguishes from
#                                            the rare split-brain case)
#
# Frequency in normal workflow: OK > WAN-N / LAN-N > WAN+LAN both
# behind > split-brain. State E (both-behind-routine) is the
# transient that drops to LAN-N or WAN-N as one channel catches
# up. See [[sync-status-state-frequencies]].
#
# RED COLOUR RULE — "settings allow this to be stored, but it
# isn't stored yet." Transient red = normal automation; persistent
# red = something's broken; black = "settings preclude this
# resolution; you accepted it by design (phone in the forest)."
#
#   WAN-{wan}    red iff work_offline is OFF
#   LAN-{lan}    red iff lan_allow_sync is ON
#   R(+n)        always red (auto-commit always runs)
#
# Each part of the WAN-x_LAN-y / WAN-x LAN-y compound labels gets
# its own red treatment per the rule above. The separator
# (underscore or space) stays black.
#
# SUFFIX TABLE — only "· offline" actually surfaces; the other
# two toggle states are implied (the user can see them elsewhere
# in the UI; no need to call them out alongside every sync
# status).
#
#   work_offline=off, lan=off → no suffix (default state)
#   work_offline=off, lan=on  → no suffix ("· LAN" is implied)
#   work_offline=on,  lan=off → " · offline"
#   work_offline=on,  lan=on  → no suffix ("· LAN-only" is implied)
#
# OK · LAN BAN — the only label/suffix combo that's explicitly
# disallowed: an "OK" label with "· LAN" suffix is collapsed
# to bare "OK". The label asserts a clean-state claim and "·
# LAN" is a mode tag; composing them reads ambiguously. Only
# applies to bare "OK" — "LANOK · LAN-only" etc. are fine.
ps = project_status(langcode)
wan = ps.wan_unshared
lan = ps.lan_unshared
ar  = ps.at_risk
mm  = ps.main_merged   # since 0.53.3; pre-0.53 daemons → True
n   = ps.n_changes
wo  = ps.work_offline
lt  = ps.lan_allow_sync

# Per-channel red rule.
def red(text):  return mark_red(text)        # peer-specific colouring
def plain(text):return text

wan_part = red(f"WAN-{wan}") if not wo else plain(f"WAN-{wan}")
lan_part = red(f"LAN-{lan}") if lt        else plain(f"LAN-{lan}")

# WAN "done" gate (since 0.53.3). ``wan_unshared`` now counts DOWN as
# a chunked topic-push uploads history and reaches 0 once all bytes
# are on github but BEFORE the final merge into main. So wan==0 no
# longer means "backed up" — the WAN channel is done only when there
# is nothing left to upload (wan==0) AND the merge landed
# (main_merged). The wan==0-but-not-merged window renders as "WAN-0"
# ("finishing"), never "OK". Pre-0.53 daemons omit main_merged; the
# mirror defaults it True, so this collapses to the old wan==0 rule.
wan_done = (wan == 0 and mm)

# State label.
if wan_done and lan == 0:
    label = "OK"
elif wan_done:
    label = lan_part
elif lan == 0:
    label = wan_part          # "WAN-0" during the finishing/merging window
elif ar == 0:
    # State D (rare): split-brain — underscore separator.
    label = wan_part + "_" + lan_part
else:
    # State E (routine): both behind on the same commits — space.
    label = wan_part + " " + lan_part

# Uncommitted-changes badge — literal text is just ``+N``, drawn
# in red as a separate visual element next to the label. (Earlier
# design notes used the shorthand ``R(+N)`` to denote "the red
# uncommitted badge with value N" — that's notation, not output.
# Peers render ``+1`` / ``+3`` etc., not the literal string "R(+1)".)
badge = red(f"+{n}") if n > 0 else ""

# Suffix (only · offline surfaces; · LAN / · LAN-only are implied).
suffix = " · offline" if (wo and not lt) else ""

# OK · LAN ban (no longer reachable since · LAN is implied to "",
# but kept as documentation): bare "OK" never composes with a
# "· LAN" suffix.
if label == "OK" and suffix == " · LAN":
    suffix = ""

sync_status.text = (label + (" " + badge if badge else "") +
                    suffix).strip()
```

The compound labels (`WAN-x_LAN-y`, `WAN-x LAN-y`) are best
rendered with adjacent inline elements so the per-part red rule
can colour `WAN-x` and `LAN-y` independently. Most Kivy/Toolkit
peers do this with two `Label` widgets in a `BoxLayout` (or one
`Label` with `markup=True` colour tags); the recipe above is
illustrative pseudocode, not a single-widget literal.

### Background refresh obligation — peer MUST re-poll AND re-read content on HEAD advance

The daemon has no channel to push state changes into a running
peer; ``project_status`` is the only surface, and it's pull-only.
That covers two distinct things peers must keep in sync:

- **Badge state** — ``wan_unshared`` / ``lan_unshared`` /
  ``at_risk`` / ``work_offline`` etc. (see § 17b rendering
  recipe). Drives the sync-status indicator.
- **Content state** — the LIFT (and audio / image) bytes
  rendered in the peer's UI. Changes when the daemon's HEAD
  advances: local commit, **incoming LAN receive-pack from a
  paired peer**, post-receive merge commit, scheduler-driven
  github pull (future ``MERGED_REMOTE``).

Peers MUST re-call ``project_status(langcode)`` and act on
**both** dimensions:

1. **In the result handler of every sync gesture.** Whether
   the gesture was the user-pressed Sync button
   (``sync_project``), a debounced background commit
   (``commit_project`` + ``poll_job``), or a peer-side
   work-offline toggle (``set_work_offline``). Don't read the
   gesture's own ``Result`` for the new ``wan_unshared`` —
   ``Result`` carries status codes, not state. Always re-poll
   ``project_status``.
2. **On a low-rate background tick.** Daemon-driven push
   happens on the scheduler's drain loop without a peer
   gesture; LAN-incoming receive-packs from a paired peer
   land entirely outside the peer's loop. Without a background
   poll the badge would stay at the last-gesture-time value
   indefinitely AND the LIFT view would stay frozen at the
   pre-receive content. 5-15 s is the right range — fast enough
   that the user sees the badge drop after a background push
   AND sees a paired peer's recording within ~10 s, slow
   enough that the RPC cost stays trivial.
3. **On ``on_resume`` / activity-foreground.** Same hook used
   for project-switch reconciliation (§ 14a). The daemon may
   have drained — or absorbed a peer's LAN push — while the
   peer was backgrounded.

#### The signal — ``ProjectStatus.head_sha``

Track the daemon's current HEAD across polls. Since 0.45.45,
``project_status`` carries ``head_sha`` — the SHA hex of
``refs/HEAD`` on the daemon side. It bumps on every event that
moves HEAD: local commits the peer initiated, local commits
the daemon's scheduler ran (stuck-commit retry), and (the case
this section exists for) **incoming receive-pack from a paired
peer over LAN**.

The recipe:

```python
def _on_status_poll(self, ps):
    # Badge dimension: redraw regardless. Cheap.
    self._refresh_sync_indicator(ps)
    self._refresh_uncommitted_badge(ps)
    # Content dimension: only fire the expensive in-place
    # reload when HEAD actually moved underneath us. First
    # poll establishes the baseline; subsequent polls compare.
    last = getattr(self, '_last_head_sha', None)
    if ps.head_sha and ps.head_sha != last:
        if last is not None:
            # Not the first poll — HEAD really did advance.
            # Re-read the LIFT and re-render with the user's
            # anchor preserved per § 14.
            self._refresh_in_place()
        self._last_head_sha = ps.head_sha
```

Empty ``head_sha`` (legacy daemon or pre-first-commit project)
disables the content-reload branch — peers fall back to the
pre-0.45.45 behaviour of "only refresh on user-initiated sync"
without losing badge correctness.

The peer's ``_refresh_in_place`` implementation lives in
§ 14 (the LIFT-aware "anchor stays, content under it
refreshes" recipe).

#### Polling cadence and content-reload cost

The background poll is light (in-memory dict lookups +
HEAD-SHA read on the daemon side). The content reload is
heavier — re-parse the LIFT, rebuild any derived indices, re-
render the current view. That's why the recipe gates the
reload on ``head_sha`` change rather than firing every tick.

If your peer has a slow LIFT parse (large project), consider:
- Cache the last-rendered HEAD locally per loaded view so a
  back-tap doesn't re-parse needlessly.
- Don't reload the LIFT model on tick if the user's currently
  recording an entry. Stash the new ``head_sha`` as
  ``_pending_head_sha`` and apply on next idle / save
  boundary. The merge-on-receive (since 0.45.44) means the
  daemon already preserved your in-flight edit, so deferring
  the reload is just polish-pass UX.

#### Push notifications — ContentObserver subscription (v0.47.0+)

The "no addressable channel into a peer's UI thread" rationale
above held through 0.46.x. **v0.47.0 adds one** via Android's
standard ``ContentResolver.notifyChange`` /
``registerContentObserver`` pair, scoped to the existing
``AZTCollabProvider`` authority. Peers can now subscribe to
per-project status URIs and get sub-second wakeups instead of
waiting for the next polling tick.

The polling cadence drops correspondingly:

- **Subscribed peer (Android, has the suite-signature permission)**:
  Background tick at 60-120 s (sanity backstop for missed
  notifications, observer churn, etc.). Otherwise, re-poll
  on every observer fire + on ``on_resume``. ~10× less RPC
  traffic than the polling-only model.
- **Non-Android peer (desktop / loopback / unsigned)**:
  Subscription API returns ``None`` → peer falls back to the
  5-15 s polling tick described above. The subscribe / unsubscribe
  calls are silent no-ops; peer code is the same shape either
  way.

**API**:

```python
from azt_collab_client import (
    subscribe_project_changes, subscribe_global_changes, unsubscribe,
)

# Subscribe to one project (active in the recorder, viewer, etc.)
def _on_status_change(uri):
    # Don't fetch state here directly — debounce + dispatch to
    # the UI thread. Multiple rapid wakeups during a sync cascade
    # collapse to one re-poll.
    Clock.schedule_once(lambda dt: self._refresh_badge(), 0)

self._sub_token = subscribe_project_changes(langcode, _on_status_change)
# self._sub_token is None on non-Android or if registration failed —
# the peer's polling fallback handles that case.

# Tear down (on project switch, on_pause, app exit):
unsubscribe(self._sub_token)
self._sub_token = None
```

**For project-list / picker UIs** that render multiple projects,
use ``subscribe_global_changes`` instead — one subscription on
the parent URI catches per-project notifications across every
project (Android's ``notifyForDescendants=True`` semantics):

```python
self._global_token = subscribe_global_changes(_on_any_change)
```

**Daemon-side firing**: ``notify_project_changed`` is called by
the daemon at every state-change site:

- After successful local commit (HEAD advance)
- After successful incoming receive-pack + working-tree reset
- After successful LAN push + peer-observation record update
- After commit-time post-receive absorb
- For toggle flips (``work_offline``, ``lan_allow_sync``) — fires
  the global URI, which descendants-mode observers also receive

The observer callback fires on the binder thread that delivered
the notification (no Handler passed to ``ContentObserver``).
Peers needing UI-thread access (Kivy widget mutation, etc.)
should marshal in the callback — ``Clock.schedule_once`` on
Kivy is the standard idiom.

**Lifetime + threading**: pyjnius proxies must survive Python
GC between events; the client library holds them strong-ref'd
in module state, keyed by the token. Pass that token to
``unsubscribe`` to release. Failing to unsubscribe leaks the
observer for the lifetime of the Python process — not catastrophic
(observers are cheap) but undisciplined. The token is opaque to
the peer; treat it as a handle.

#### Why peer-side polling is still the floor

Even with notifications wired, polling remains the contract
floor for three reasons:

- Non-Android peers / desktop transport have no ContentProvider.
- Observer wakeups can be missed (process killed and re-spawned,
  registration churn, binder thread saturation).
- ``on_resume`` always re-polls once — the process may have
  been backgrounded long enough that wakeups landed and went
  un-handled if the peer didn't subscribe before pausing.

So the recipe is "subscribe when foregrounded, poll as a
heartbeat, re-poll on every wakeup and on resume."

#### Field symptoms this section closes

- 2026-05-18: UI badge sticky at ``(+160)`` while daemon log
  showed ``[sync-rpc] 'baf' done: codes=['NOTHING_TO_COMMIT',
  'PUSHED']`` — daemon was at zero wan_unshared, peer never
  re-polled.
- 2026-05-26: two phones LAN-paired and sharing one project,
  phone A's recording landed on phone B's working tree via
  ``[lan-push] advanced ... → ...`` but phone B's recorder
  kept rendering the pre-receive LIFT until the user manually
  re-entered the project. Closed by the ``head_sha`` signal
  here AND the merge-on-receive in 0.45.44 (which makes the
  working tree actually reflect the merge result rather than
  the deferred-reset stale state).

### Migration checklist (from pre-0.43 peer)

1. Swap ``request_sync(langcode)`` for ``commit_project(langcode)``
   (or keep ``request_sync`` — kept as an alias). Both go
   through the new commit-only path.
2. Strip any post-RPC code that polls for ``PUSHED`` /
   ``COMMITTED_AND_PUSHED`` on the ``request_sync`` result.
   Replace with periodic ``project_status`` reads of
   ``wan_unshared`` / ``lan_unshared`` / ``at_risk``.
3. Add ``S.WORK_OFFLINE_ENABLED`` to the user-initiated sync
   routing table — toast + route to ``open_server_ui()``.
4. Render the work-offline badge from
   ``ProjectStatus.work_offline``.
5. **Wire the badge to re-poll after every sync gesture, on
   a 5-15 s background tick, and on ``on_resume``.** See
   "Badge refresh obligation" above. Pre-0.43 peers got away
   without an explicit re-poll because the gesture's own
   ``Result`` carried ``PUSHED`` and the peer could update
   from there; in the split-commit world the ``Result`` no
   longer encodes push state, so a missing re-poll leaves
   the badge stuck at the last-gesture-time value.
6. (Optional) Surface ``get_work_offline()`` /
   ``set_work_offline()`` in a peer-side quick-toggle if your
   UX wants the switch outside the daemon settings screen.
   Most peers won't need this — the daemon settings UI hosts
   the canonical toggle.

## 17a. Stuck-commit + atomic-recovery telemetry on `ProjectStatus` (informational)

Four fields on ``ProjectStatus`` expose daemon-side
bookkeeping that peers MAY surface in diagnostic UI but are
not load-bearing for any alarm path:

```python
ps = project_status(langcode)
ps.commit_failure_count    # int — successive failed commit attempts (0.41.27+)
ps.last_commit_failure_at  # float — unix timestamp of latest failure (0.41.27+)
ps.last_commit_error       # str  — last dulwich error message (0.41.27+)
ps.last_sync_error         # str  — access-class code blocking WAN sync,
                           #        e.g. 'REPO_NO_ACCESS' / 'AUTH_REQUIRED';
                           #        '' when none. Cleared on next success.
                           #        Route 'REPO_NO_ACCESS' → repo_access_popup. (0.52.24+)
ps.last_sync_error_at      # float — unix ts of that failure (0.52.24+)
ps.n_recovered_today       # int — orphan LIFT scratches auto-merged today (0.41.29+)
```

These fields are **diagnostic**, not load-bearing for any
alarm path. The canonical surface for
``COMMIT_REPEATEDLY_FAILED`` is the auto-sync result iteration
described in § 17: the daemon emits the status on any sync
attempt (peer-driven or scheduler-driven) where ``count >= 2``,
and a peer-driven sync after a background failure naturally
sees the elevated counter and carries the status on its
result. Peers do not need to poll for the alarm.

When to read these fields:

- A diagnostic / settings screen rendering "last commit error:
  foo" alongside other project state.
- A status badge that wants to show "syncing has been failing"
  without firing a modal toast (e.g. a small "!" next to the
  last_commit timestamp).
- A "we rescued some unsaved work today" badge driven by
  ``n_recovered_today`` — purely a "you might notice some
  azt-lift-conflict annotations in your data, here's why"
  diagnostic. The recovery itself happens daemon-side without
  any user gesture; conflicts (if any) flow through the
  existing ``<annotation name="azt-lift-conflict">`` channel,
  same as cross-peer merge conflicts.

What NOT to do: don't synthesize ``COMMIT_REPEATEDLY_FAILED``
toasts off the polled count. The auto-sync result iteration
already fires the toast on the next sync attempt; a polling-
based duplicate path complicates de-duplication for marginal
UX gain. Same shape for ``n_recovered_today`` — it's a counter,
not an event; rendering it as a count is fine, popping a modal
"we rescued 3 files" toast is not (the user didn't ask to be
told, and the data is already on disk where they expect it).

## 17c. Don't overload the server — peer concurrency obligations

The daemon's protections (per-project ``flock``-backed
``project_lock``, server-side commit debounce, scheduler
serialization) exist to keep its own state coherent — not to
absorb a peer that fires N parallel RPCs per gesture. When a
peer skips the obligations below, the **daemon stays fine** but
the **peer pays in user-visible noise**: stacks of toasts for
``S.BUSY``, redundant ``project_status`` polls that move the
badge nowhere, and (worst) flapping ``wan_unshared`` numbers
because two in-flight syncs see different snapshots of the
project tree.

Field log signature this section closes (2026-05-18): six
``POST /v1/projects/<lang>/sync`` "pre" lines without their
matching "post", followed by four back-to-back ``[do_sync]
Une autre synchronisation est en cours`` (= ``S.BUSY``) toasts.
Peer was firing parallel ``sync_project`` calls per gesture;
the daemon's project_lock did the right thing by refusing the
later ones, but the user saw a wall of toasts.

### Rule 1 — Single in-flight guard per (RPC, project)

NEVER fire ``sync_project`` (or ``commit_project``, or any
other mutating RPC) when a prior call for the same project is
still in flight. Maintain a peer-side ``_sync_in_flight``
flag (or, more idiomatically, a per-project ``asyncio.Lock`` /
threading.Lock) set on the "pre" branch and cleared on the
"post" branch (success or failure, in a ``finally``). Drop new
triggers while the flag is held; **do not queue** them
(queuing converts user mashing into a chain that runs after
the user has moved on).

```python
def request_sync(self, langcode):
    if self._sync_in_flight.get(langcode):
        # Drop — user mash, double-tap, racing background
        # trigger. The currently-running sync will cover
        # whatever this one would have done.
        return
    self._sync_in_flight[langcode] = True
    try:
        result = sync_project(langcode)   # synchronous
        self._handle_sync_result(result)  # § 17 routing
    finally:
        self._sync_in_flight[langcode] = False
```

The daemon's ``project_lock`` is a correctness backstop, not
the peer's primary concurrency control. Without the peer-side
guard, ``S.BUSY`` toasts pile up and the daemon does the same
work multiple times in serial.

### Rule 2 — Auto-paths use ``commit_project``, not ``sync_project``

``sync_project`` is the user-Sync-button RPC: it does commit
+ push synchronously under one lock. Auto-paths
(project-select, project-load, post-edit debounce, background
periodic) MUST use ``commit_project``, which is debounced
server-side and returns immediately with a ``job_id``. Push
happens on the daemon's drain loop, no peer involvement.

Mixing the two for the same gesture is what produced the
field-log pattern — the peer fired ``commit_project`` *and*
``sync_project`` for the same edit event, and the ``sync_project``
calls collided with each other on the lock.

### Rule 3 — Debounce user gestures peer-side too

The Sync button SHOULD debounce on the peer side (200-500 ms)
even though the daemon also debounces ``commit_project``.
Reason: ``sync_project`` is NOT debounced server-side (it's
the explicit "do it now" gesture). A fast double-tap fires
two parallel ``sync_project`` calls; Rule 1 drops the second
silently, but a 250 ms peer-side debounce avoids even queuing
the call.

### Rule 4 — Background polls have a budget

``project_status`` is the only legitimate background poll
surface. Rate it conservatively:

- **5-15 s** for the active project's sync badge. Faster
  wastes RPC cost; slower makes the badge feel sticky after a
  daemon-driven push.
- **30 s+** for any per-project iteration (e.g. dashboard
  showing all registered projects). Multiply by ``N`` projects
  before deciding the interval — 10 projects × 5 s = 2 RPCs/s,
  which is wasteful for state that changes minutes apart.
- **Stop polling when the activity is backgrounded.** Resume
  on ``on_resume`` with one fresh fetch (see § 14a).

NEVER fire ``project_status`` from multiple peer-side handlers
for the same UI event (e.g. one from the sync result handler
*and* one from a poll tick that just happened to land). Pick
one owner per polling axis.

### Rule 5 — ``S.BUSY`` is "back off", not "retry"

When ``S.BUSY`` lands on a result, the right response is to do
nothing. The next regular tick / next user gesture covers it.
DO NOT:

- retry in a tight loop (you'll just hit ``S.BUSY`` again
  until the lock holder finishes — which would have happened
  on its own);
- show a toast (it's a daemon implementation detail leaking
  through; the user can't act on it);
- queue the call for "later" (Rule 1 — drop, don't queue).

### Rule 6 — Per-event triggers, not per-status-change triggers

Wire sync / commit gestures to **user-events** (edit saved,
button pressed, screen left) — never to **state observations**
(``wan_unshared > 0`` watcher firing a sync). State-based
triggers create feedback loops where the daemon's own state
update kicks off the next round of RPCs.

### Rule 7 — RPC calls MUST NOT run on the main UI thread

Every call into ``azt_collab_client`` that hits the daemon
(``call(...)`` directly, or any wrapper that does — including
``migrate_from_prefs``, ``check_server_compat``,
``list_projects``, ``project_status``, ``sync_project``,
``commit_project``, all LAN endpoints, all credentials
endpoints) MUST run on a worker thread.

Failure mode this rule closes (field-observed, 0.50.5):

- Server APK installed but its private ``files/app/_python_bundle/``
  is missing (fresh install before the user opened the server
  APK; cleared app data; uninstall-without-data-wipe variant).
- ``ContentResolver.call`` returns null because the daemon's
  Python crashed before installing the provider callbacks.
- The transport retries on null bundle with adaptive backoff
  (see ``transports/android_cp.py:_NULL_BUNDLE_RETRY_BACKOFF_S``).
  Cumulative sleep can be several seconds.
- If the call ran on the main UI thread, **Android's ANR
  watchdog kills the peer** before bootstrap renders any
  recovery UI. Splash → 3 s → process death. The peer's user
  has zero feedback and zero recovery path.

The transport's retry budget is sized to absorb legitimate
cold-spawn races (daemon ``:provider`` idle-stopped seconds
ago, Python interpreter mid-respawn). The daemon CANNOT
guarantee any upper bound on call latency: a clone, push, or
LIFT merge legitimately takes seconds-to-minutes. The peer is
the only side that knows which calls are user-facing and
which can block.

What "main UI thread" means on Android: the thread Kivy
schedules ``Clock`` callbacks on; the thread that fires
``Button.on_release``; the thread that built and dispatches
``App.build()``. p4a routes all of these through the SDL main
thread. Any synchronous RPC scheduled in that flow blocks
frame rendering — and beyond 1-2 s of unrendered frames,
Android starts the ANR clock.

Required peer-side shape:

```python
def do_user_sync(self, langcode):
    threading.Thread(
        target=self._sync_worker, args=(langcode,),
        daemon=True, name='sync-worker').start()

def _sync_worker(self, langcode):
    result = sync_project(langcode)        # safe on worker
    Clock.schedule_once(
        lambda dt: self._handle(result), 0) # marshal to main
```

The "marshal back to main via ``Clock.schedule_once``" half
is what lets the worker safely mutate Kivy widgets after the
call returns.

**Startup-time RPCs are NOT exempt.** ``migrate_from_prefs``,
the initial ``check_server_compat``, any "preload last
project" call — all of these run before the user can do
anything, and so the temptation is to call them inline.
Don't. Spawn a worker (the bootstrap module already has the
pattern in ``_check_server`` — see also the recorder's
``prewarm_*`` pattern in 0.50.6+).

The transport will not silently shorten its retry budget to
absorb peers that violate this rule — that produces worse
behaviour on the common cold-spawn case to defend against a
peer-side bug.

### What the daemon does on its own (so peers don't need to)

So peer maintainers know what *not* to reinvent:

- **Server-side debounce on ``commit_project``** (default 500
  ms). Bursts of edit-events that fire ``commit_project``
  repeatedly collapse into one commit. Peers don't need their
  own commit debounce beyond what their event source produces.
- **Push drain on the scheduler tick** (default 30 s online
  check + 60 s post-online grace). Peers don't need to push;
  they just commit and the daemon pushes when network is
  ready.
- **Stuck-commit retry with exponential backoff** (30 s, 60 s,
  120 s, … capped 1 h). Peers don't need to retry failed
  commits.
- **Project lock with reentrant flock**. Peers don't need
  their own cross-RPC serialization.
- **Auto-spawn / lazy respawn** of the daemon process. Peers
  don't need to ping for liveness before each RPC.

If a "smart" peer-side feature you're considering overlaps
with any of the above, default to NOT adding it. The daemon
is the single source of truth; peer cleverness that duplicates
its work loses every disagreement.

## 17d. Routing on LAN-sync status codes

The LAN sync surface (0.45.0+) returns ``Result``s carrying
LAN-specific status codes from ``lan_clone`` /
``lan_pair_accept`` / ``lan_pending`` / the settings-side
share gestures. Routing rules:

| Status code | When emitted | Peer response |
|---|---|---|
| ``S.LAN_PAIRED`` | ``lan_pair_accept`` succeeded. | Toast / log; show updated paired-devices list. |
| ``S.LAN_UNPAIRED`` | ``lan_unpair`` succeeded. | Same. |
| ``S.LAN_PEER_UNREACHABLE`` | ``lan_clone`` / fan-out couldn't resolve an endpoint or the connection genuinely failed at TCP / network level. Since 0.54.6 this NO LONGER covers two cases that used to fall through to it: a local TLS-identity fault (→ ``LAN_LOCAL_TLS_ERROR``) and the peer answering but refusing the repo (→ ``LAN_PROJECT_NOT_SHARED``). Params: ``peer_id``, optional ``detail``. | Translated toast ("Couldn't reach <device> over local network — make sure they're both on the same Wi-Fi"). |
| ``S.LAN_LOCAL_TLS_ERROR`` | THIS device's TLS layer failed on a missing/unreadable local file (LAN-identity ``peer_id``/``peer.crt``) before any network exchange (0.54.6). Params: ``peer_id``, ``detail``. | Popup that blames THIS device, not the network: "restart the collaboration service and try again; share diagnostics if it persists." Do NOT show the unreachable wording. |
| ``S.LAN_PROJECT_NOT_SHARED`` | The peer ANSWERED but its listener refused to serve the repo — not in its share allowlist for any paired peer, or not registered there (0.54.6). Params: ``peer_id``, ``langcode``, ``detail``. | Popup: "the other device answered but is not offering this project — share it on the other device, then try again." Do NOT show the unreachable wording. |
| ``S.LAN_FP_MISMATCH`` | A paired peer's TLS-cert fingerprint differs from the value recorded in ``peers.json`` — possible MITM or device re-pair. Currently logged daemon-side; emitted as a typed Result code in a future tightening pass (CHANGELOG 0.45.0 § Known gaps). | When peers see it surface in a ``Result``: surface a SECURITY-FLAVOURED toast — "<device>'s identity changed; re-pair to confirm." Do NOT silently auto-rotate the stored fingerprint. |
| ``S.LAN_TOGGLE_OFF`` | The user invoked a LAN op (``lan_clone``, ``send_share_offer``, …) with the daemon-wide LAN toggle off. (0.45.39+.) | Toast "Turn LAN sync on first" + ``open_server_ui()`` so the user lands on the toggle. |
| ``S.LAN_PROJECT_CLONED`` | ``lan_clone`` performed a fresh clone from a peer. Params: ``langcode``, ``peer_id``, ``device_name``. | Translate to status line; pickup follows — the daemon stamps ``last_project`` so the peer's next picker resume lands in the new project. |
| ``S.LAN_PROJECT_REOPENED`` | ``lan_clone`` found a related local copy already; bookkeeping recorded the LAN pair without re-cloning. | Translate to status line. |
| ``S.LAN_PROJECT_COLLISION_UNRELATED`` | Local project with this langcode exists but shares no commits with the peer's — refuse rather than overwrite. Params: ``langcode``. | Surface translated toast naming the langcode + "rename or remove first" instruction. |
| ``S.LAN_ADOPT_ORIGIN_NEEDED`` | A LAN-clone delivered a project whose ``origin`` is unset; the peer's QR included a ``remote_url`` we'd like to adopt. Stashed as a pending decision (kind=``adopt_origin``). | Open ``adopt_origin_popup`` (``azt_collab_client.ui.lan_popups``) to confirm; or wait for the user to discover it via the "Decisions waiting (N)" surface. |
| ``S.LAN_REMOTE_CONFLICT`` | Local project has one ``origin``, the peer is offering a different one. Stashed as a pending decision (kind=``remote_conflict``). | Open ``adopt_origin_popup`` with three options (use theirs / keep mine / dual-publish). |
| ``S.LAN_PROJECT_ADOPTED_REMOTE`` | The user accepted an adopt-origin decision; ``origin`` was set. | Translate to status line; refresh ``project_status`` so Publish disappears (now has a remote). |
| ``S.LAN_SHARE_OFFER`` / ``S.LAN_SHARE_DECLINED`` / ``S.LAN_OFFER_ACCEPTED`` | Reserved for future direct emission. The current code path uses the typed ``pending_decisions`` ``kind`` strings (``share_offer`` / ``adopt_origin`` / ``remote_conflict``) returned by ``lan_pending()``; peer UIs dispatch on ``kind``. If your peer wants to render translated text per decision, run ``translate_status(Status(<S.LAN_SHARE_OFFER...>, params))`` keyed off the kind. | Peers driving the pending-decisions surface should iterate ``lan_pending()`` directly and dispatch on ``kind``; the typed codes are wired forward for future-strict ``Result`` emission. |

### Constants — LAN

```python
from azt_collab_client import S
S.LAN_PAIRED / S.LAN_UNPAIRED
S.LAN_PEER_UNREACHABLE / S.LAN_FP_MISMATCH / S.LAN_TOGGLE_OFF
S.LAN_LOCAL_TLS_ERROR / S.LAN_PROJECT_NOT_SHARED
S.LAN_PROJECT_CLONED / S.LAN_PROJECT_REOPENED
S.LAN_PROJECT_COLLISION_UNRELATED
S.LAN_ADOPT_ORIGIN_NEEDED / S.LAN_PROJECT_ADOPTED_REMOTE
S.LAN_REMOTE_CONFLICT
S.LAN_SHARE_OFFER / S.LAN_SHARE_DECLINED / S.LAN_OFFER_ACCEPTED
```

## 18. Low-power adaptive policy

Devices in the field span flagship phones / tablets down to
2–3 GB budget hardware. The conformity contract is: **adapt
resource decisions to the device automatically; only ask the
user about content / workflow choices**.

The principle:

> When the device has memory + network headroom, **be eager**:
> pre-warm caches, render full-resolution, hold persistent
> background polls. When it doesn't, **degrade transparently**:
> skip the bulk warm, downsample at display time, suspend
> polling, ship pre-built variants instead of generating them
> at boot. The user should not see a "low-power mode" toggle —
> the peer reads OS signals and does the right thing.

> **For the rationale** (why build-time work belongs in the
> build, the runtime-rescale anti-pattern, what we explicitly
> reject) — see ``CLAUDE.md`` "Low-power adaptive policy".

### Three rules

1. **Detect via OS signals, not user choice.** Android exposes
   ``ActivityManager.MemoryInfo.lowMemory`` and ``availMem`` /
   ``totalMem``. Trust the OS. A user-facing "low-power mode"
   asks the user to know things about their hardware they
   shouldn't have to.

2. **Resource decisions are automatic; content / workflow
   decisions remain user settings.** Distinguishing question:
   "is this about what the device CAN do, or about what the
   user WANTS?" Cache sizes, prefetch eagerness, prewarm
   gating, poll cadence → automatic. Display-mode toggles,
   sync-timing preferences → user-facing.

3. **Pre-built variants > runtime regeneration.** When the
   build can predict what the device will need, do that work
   in the build. Examples: ``drawable-<bucket>/`` Android
   resource buckets (Android picks at install time);
   pre-bundled gettext ``.mo`` files; daemon-side pre-rendered
   CAWL cache. Asking a budget device to PIL-rescale assets
   during the splash — before Python is warm — is the
   anti-pattern.

### Inventory: gate vs. don't gate

| Adaptation | Auto-gate? | Typical signal |
|---|---|---|
| Image cache size | yes | ``totalMem`` tiers (e.g. ≤3 GB / 3–6 GB / >6 GB) |
| CAWL prefetch eager vs skip | yes | ``lowMemory`` ∨ ``availMem``/``totalMem`` ratio ∨ metered network |
| Boot-time prewarm | yes | ``totalMem`` ≤ 3 GB → skip |
| Image display rescale | yes | ``lowMemory`` → downsample (e.g. 720 px max) |
| Cache-status poll cadence | yes | last-touch timestamp |
| Sync-status poll lifecycle | yes (universal) | ``on_pause`` / ``on_resume`` |
| Multi-density splash | n/a (install-time) | Android resource resolver |
| Max-visible UI items | no | content choice — user setting |
| Auto-sync-on-swipe | no | workflow choice — user setting |

Use ``azt_collab_client.lowpower`` (since 0.41.21) — single
source of truth for the JNI plumbing and the thresholds:

```python
from azt_collab_client.lowpower import (
    total_ram_mb,            # one-shot, cached
    memory_state,            # fresh: (low_memory, avail_ratio, avail_mb)
    is_low_memory,           # combined predicate
    is_metered_network,
    have_room_for_prefetch,  # not low_memory and not metered
    ram_tier,                # 'low' | 'mid' | 'high'
    densityDpi,              # for diagnostic logging
    dpi_to_bucket,
)
```

Thresholds are module-level constants — override before first
call if your peer has field data motivating a change:

```python
import azt_collab_client.lowpower as lp
lp.RAM_TIER_LOW_MB = 4096    # treat 4 GB devices as low tier
```

For local testing of the gated paths on any platform, set
``AZT_FORCE_LOW_MEMORY=1`` in the environment — every signal
flips to its budget-device value (low_memory=True, ratio=0.05,
metered=True, ram_tier='low').

**Permission requirement: ACCESS_NETWORK_STATE.**
``is_metered_network`` (and therefore ``have_room_for_prefetch``,
which combines it with memory state) calls Android's
``ConnectivityManager.isActiveNetworkMetered()``, which
requires ``ACCESS_NETWORK_STATE``. Without the permission
declared in the peer's ``buildozer.spec``, the JNI call raises
``SecurityException: Neither user N nor current process has
android.permission.ACCESS_NETWORK_STATE`` and the helper
silently returns ``False`` (biasing toward "eager" — safer
default than skipping work the user wanted, but masks the
fact that we never checked). Add to peer's
``android.permissions``:

```ini
android.permissions = INTERNET, ACCESS_NETWORK_STATE, ..., org.atoznback.AZT_COLLAB_ACCESS
```

This is a normal-protection-level permission (no runtime
grant prompt; manifest entry alone is sufficient).

### Multi-density assets

For images that get a runtime-meaningful size (splash, large
illustrations), ship one PNG per density bucket under
``presplash_variants/drawable-<bucket>/<name>.png``. Scales
relative to mdpi (1.0×): ldpi 0.75×, hdpi 1.5×, xhdpi 2×,
xxhdpi 3×, xxxhdpi 4×.

Wire them through ``android.add_resources`` in
``buildozer.spec``:

```ini
android.add_resources =
    %(source.dir)s/presplash_variants/drawable-ldpi/presplash.png:drawable-ldpi/presplash.png,
    %(source.dir)s/presplash_variants/drawable-mdpi/presplash.png:drawable-mdpi/presplash.png,
    %(source.dir)s/presplash_variants/drawable-hdpi/presplash.png:drawable-hdpi/presplash.png,
    %(source.dir)s/presplash_variants/drawable-xhdpi/presplash.png:drawable-xhdpi/presplash.png,
    %(source.dir)s/presplash_variants/drawable-xxhdpi/presplash.png:drawable-xxhdpi/presplash.png,
    %(source.dir)s/presplash_variants/drawable-xxxhdpi/presplash.png:drawable-xxxhdpi/presplash.png
```

Keep ``presplash.filename = ...png`` as the rare-fallback for
devices that match no bucket. Android prefers a qualifier
match over the unqualified drawable.

### Diagnostic logging — verify which bucket landed

Android doesn't surface the chosen drawable bucket in
logcat. Use the shared helper at peer startup:

```python
from azt_collab_client.lowpower import log_presplash_variant

# Distinct tag per APK so a combined logcat is grep-able.
log_presplash_variant(tag='presplash')           # peer
log_presplash_variant(tag='presplash:server')    # server APK
```

Sample line:

```
[presplash:server] device densityDpi=420 (xxhdpi); native
960x1599 source=480dpi (xxhdpi variant)
```

For a non-presplash multi-density asset (icons, large
illustrations), pass a custom ``bucket_table``:

```python
log_presplash_variant(
    tag='icon', resource_name='icon',
    bucket_table={'mdpi': (160, 48), 'hdpi': (240, 72),
                  'xhdpi': (320, 96), 'xxhdpi': (480, 144),
                  'xxxhdpi': (640, 192)},
)
```

#### Why this recipe and not the obvious one

Two simpler recipes that **don't work** — don't copy them:

- ``Drawable.getIntrinsicWidth/Height()`` returns device-scaled
  pixels, so every bucket collapses to the same number on any
  given device.
- ``BitmapDrawable.getBitmap().getDensity()`` /
  ``.getWidth()`` reports post-scaling state — Android has
  already pre-scaled the bitmap to the device target density
  by the time ``getDrawable`` returns, and the bitmap's
  ``density`` field is reset to match.

The helper uses ``BitmapFactory.decodeResource`` with
``inJustDecodeBounds=true`` (skips bitmap allocation) and
``inScaled=false`` (the load-bearing flag — without it
``outWidth`` would be pre-scaled like the broken recipes).
``opts.outWidth`` then carries the native pixel width of the
resource file Android actually picked, and ``opts.inDensity``
the source folder's density. Cross-referencing both against
the known bucket table identifies the variant unambiguously.

### Verification

After shipping the multi-density splash:

- ``unzip -l <peer>.apk | grep drawable-`` lists exactly one
  ``presplash.png`` per declared bucket.
- The ``[presplash]`` log line at startup names the bucket
  matching the device's ``densityDpi``.
- The splash is sharp at native resolution on a device of
  each tier you care about (no on-device PIL-resize).

After gating ``lowMemory`` adaptations:

- Force ``lowMemory`` true via the Android Debug Bridge
  (``adb shell am send-trim-memory`` or
  ``Activity.onTrimMemory``) and confirm gated paths take
  the degraded branch.
- Verify ``on_pause`` / ``on_resume`` actually suspend /
  re-arm background polls (no battery drain in
  ``adb shell dumpsys batterystats`` while paused).

## 19. Package-replacement handling

APK install ≠ process upgrade on Android. When a package is
reinstalled — ``adb install -r``, file-manager sideload, browser
``ACTION_INSTALL_PACKAGE``, Play Store update — Android may keep
the old process alive serving from the old code until something
kills it. Lazy ContentProviders are the worst case: once a
process is up serving the provider, it stays until killed
(memory pressure, device reboot, explicit
``killBackgroundProcesses``). Lazy services are the same.

The user-visible symptom is "I installed the new server APK,
but the peer still says the server is too old." The peer's
``check_server_compat`` is talking to the old running server
process, which is still reporting the old version. "Wait for
an update" is the wrong instruction — the update is right
there on disk, what's missing is a process restart.

### Contract

Every suite APK (server + every peer) MUST handle its own
replacement. **No peer-side coordination required and no peer
permission needed.**

1. **Manifest receiver, NOT runtime.** Declare a receiver for
   ``android.intent.action.MY_PACKAGE_REPLACED`` in
   ``AndroidManifest.xml``. Runtime-registered receivers require
   the process to be alive at broadcast time; some Android
   versions / OEMs kill the old process as part of the replace
   and there's no live receiver to deliver to. Manifest-declared
   receivers cold-start the process to deliver — exactly what's
   needed: spawn the NEW APK's code, run the handler, exit.

2. **Reap surviving old-code processes, then self-kill.** The
   receiver calls
   ``ActivityManager.killBackgroundProcesses(getPackageName())``
   first to reap any old-code process Android kept alive across
   the install (sticky bindings, lazy provider clients), then
   ``Process.killProcess(Process.myPid())`` on its own fresh
   process. The reap step is load-bearing on OEMs that don't
   auto-kill on replace; it requires
   ``KILL_BACKGROUND_PROCESSES`` declared on the APK *whose
   receiver is firing* — i.e. the freshly-installed APK reaps
   its own old-code daemon. No cross-package killing happens, so
   peers never need this permission to fix the server.

3. **Receiver + permission are suite-provided.** The class
   ``org.atoznback.aztcollab.SuiteSelfReplaceReceiver`` lives in
   ``azt-collab/android/src/main/java/`` (compiled into every APK
   via ``android.add_src``); the manifest ``<receiver>`` AND the
   ``<uses-permission>`` for ``KILL_BACKGROUND_PROCESSES`` are
   both injected by
   ``p4a_hook.py:_inject_self_replace_receiver`` for every APK
   in the suite (not gated on ``dist_name``). Peers don't add
   the receiver, the permission, or any peer-side backstop —
   symlinking ``android/`` from the canonical repo plus using
   the shared p4a hook brings it all in automatically.

### What peers MUST NOT do

Peers MUST NOT assume the server APK on disk matches the server
process currently serving requests. Pre-0.41.28 the assumption
was implicit — the user would install a new server, peer's next
compat probe talked to the OLD running process, and the popup
told them their just-finished install had "no newer release."
Every suite APK self-handling its own replacement (0.41.28+)
closes that gap; the receiver's in-APK reap step (0.42+)
closes the remaining OEM-doesn't-auto-kill-on-replace case.

Peers MUST NOT add ``KILL_BACKGROUND_PROCESSES`` to their own
``android.permissions``. The pre-0.42 peer-side backstop
(``bootstrap._kill_server_background``) is gone; declaring the
permission peer-side does nothing useful now and gives Google
Play (or any future store reviewer) an extra permission to ask
about.

### Rollout window

Until every field server APK ships daemon 0.42+, the in-receiver
reap step isn't available for those legacy installs. The user's
recourse for a stuck pre-0.42 install is to reboot the device
(the most reliable way to clear a sticky-bound process). Peers
on client 0.42+ surface this automatically:
``_prompt_server_update`` reads the installed-on-disk server APK
version via ``PackageManager.getPackageInfo`` and compares it
to whatever /v1/health reports; if installed > running,
``_prompt_server_reboot_to_apply`` substitutes a "you have X
installed but the running process is Y — reboot to switch"
popup for the usual download-and-install popup. Once every
field daemon is at 0.42+ the comparison should never trigger
(the receiver auto-reaps during install) and the helper is
effectively dead code.

## 20. LAN sync peer surface (since 0.45.0)

The LAN sync transport lets two AZT-suite devices on the same
Wi-Fi (or hotspot) push commits to each other without going
through github. The peer's responsibility is small: render the
toggle / pair / share / pending-decisions affordances, call
the right wrappers, and route the typed Results.

> **For the rationale** (why LAN is opportunistic redundancy,
> not a github replacement; why identity is body-claimed
> rather than TLS-pinned; why auto-share is gated on a QR
> display) — see ``CLAUDE.md`` "LAN sync architecture
> invariants" in the canonical repo. This section is the
> contract.

### Hard rules

1. **LAN is opportunistic. github is authoritative.** A
   successful LAN push does NOT clear ``pending_push``; the
   daemon still pushes to github when network and toggles
   allow. Peers MUST NOT treat LAN-only delivery as "synced."
   The ``WAN-N`` / ``LAN-N`` / ``WAN-x LAN-y`` distinctions in
   § 17b's rendering recipe are the user-visible expression of
   this — never conflate them.
2. **Don't poll ``lan_pending`` from peer code at all.** Use
   the shared decisions watcher (§ 20a) — it owns the poll.
   The watcher polls at 1 s by default which is fine because
   it's the *only* poller; an additional peer-side poll on
   the same endpoint races the watcher and double-pops
   decisions. (The old "≥5s" rule existed when each peer
   polled independently; it's superseded by the watcher
   contract.)
3. **Never store the peer's ed25519 key, fingerprint, or
   endpoint outside ``peers.json``.** The daemon owns the
   paired-peers registry. Peer-side caches drift — fingerprint
   mismatches go undetected, endpoints stale-pin to a dead
   port, ``shared_projects`` allowlist desyncs between two
   peers reading the same daemon. Read via ``lan_list_peers``
   each time.
4. **A LAN-only project shows a ``WAN-N`` badge (red iff
   work_offline=off) but Publish must still be available.**
   ``lan_clone`` strips the peer's listener URL from local
   ``.git/config`` (0.45.37+) so ``project_status.remote_url``
   reads empty and the publish-row gate doesn't hide Publish.
   Don't second-guess the empty ``remote_url`` — the project
   IS unpublished (no github backup yet, hence the ``WAN-N``
   walk-from-HEAD intentional-friction count grows with each
   commit), even though it's reachable on the LAN.

### Public API surface

All wrappers are in ``azt_collab_client`` (re-exported from
``__all__``). Wrappers translate transport failure into the
empty / no-op shape (``[]`` / ``{}`` / ``Result(SERVER_UNAVAILABLE)``);
peers don't raise.

```python
from azt_collab_client import (
    # Identity (read-only)
    lan_peer_id,
    # Paired-peers registry
    lan_list_peers, lan_pair_qr, lan_pair_accept, lan_unpair,
    lan_pair_qr_keepalive, lan_pair_qr_close,
    lan_set_static_endpoints,
    # Per-peer share allowlist + outbound courtesy notify
    lan_share_project, lan_unshare_project,
    # Receiver-side LAN clone + pending decisions
    lan_clone, lan_pending,
    lan_accept_offer, lan_decline_offer,
    lan_adopt_origin, lan_resolve_conflict,
    # Daemon-wide toggle (hot-applied)
    lan_toggle, lan_set_toggle,
)
```

Field shapes worth knowing:

- ``lan_peer_id()`` → ``{peer_id, fp, device_name}`` — your
  daemon's identity. Empty dict on transport failure.
- ``lan_list_peers()`` → list of ``{peer_id, fp, device_name,
  endpoints, static_endpoints, shared_projects, paired_at,
  last_seen_at}``. Empty list on transport failure.
- ``lan_pair_qr(langcode='')`` → dict to render via ``segno``.
  When ``langcode`` is non-empty, the QR carries the
  project's ``remote_url`` + ``vernlang`` so a single scan
  does pair + share + clone. Empty dict on transport failure.
  **Side effect:** it arms a project auto-share offer for
  ``langcode`` so a scanning peer's hello-back auto-shares that
  langcode (and only that one). Without an active offer, a hello
  records the pair but refuses auto-share.
- **"Valid while displayed" contract (since 0.52.26).** The
  auto-share offer is armed **only while the QR is actually on
  screen**, and is **multi-use** (one shown QR can be scanned by
  several peers — the workshop case). A peer that renders its own
  share-QR screen (rather than the shared
  ``ui.lan_popups.share_pairing_qr_popup``, which already does this)
  MUST, for a non-empty ``langcode``:
  - call ``lan_pair_qr_keepalive(langcode)`` on open and every ~10 s
    while displayed (daemon keepalive window is 30 s), and
  - call ``lan_pair_qr_close(langcode)`` on dismiss.
  Skipping the heartbeat lets the offer lapse within ~30 s (share
  stops working mid-display); skipping ``close`` just means the offer
  self-expires ~30 s after the screen goes away instead of instantly.
  Both are no-ops for an empty langcode (pair-only QR) and return
  ``bool``. This replaced the pre-0.52.26 single-use + 10-minute
  timer.
- ``lan_pair_accept({payload})`` → ``Result`` carrying
  ``LAN_PAIRED`` + the recorded peer entry.
- ``lan_clone(peer_id, langcode, remote_url='', vernlang='')``
  → ``Result`` (see § 17d for codes).
- ``lan_clone_progress()`` → ``{active, langcode, text, ts}``
  (empty dict on transport failure) — last git sideband progress
  line of the clone the daemon is running right now (0.54.6).
  Poll it (~1 s) from the receive UI while ``lan_clone`` runs on
  a worker thread, so a multi-minute first copy shows movement;
  ``active`` False means show your static hint instead.
- ``lan_pending()`` → list of ``{id, kind, params, created_at}``.
  ``kind`` is one of ``share_offer`` / ``adopt_origin`` /
  ``remote_conflict``.
- ``lan_toggle()`` → ``{on, endpoint}``; ``lan_set_toggle(on)``
  → same. Hot-applied; listener thread + (Android) FGS + WifiLock
  are re-armed atomically.

### Reference UI

``azt_collab_client.ui.lan_popups`` ships the canonical UI for
this surface — peers SHOULD reuse it rather than reimplement:

```python
from azt_collab_client.ui.lan_popups import (
    share_project_popup,        # owner-side: per-langcode share
                                # (paired-phones list + QR
                                # + github-invite)
    paired_phones_popup,        # all-projects paired-phones list
                                # with per-row Manage + Unpair
    pending_offers_popup,       # receiver-side: pending share-
                                # offers + Scan-QR fallback
    scan_to_pair,               # picker-side scanner entry
    adopt_origin_popup,         # confirm adopt-origin / resolve
                                # remote-conflict
)
```

All five honor the daemon's translated status codes via
``translate_result``; peers don't need to repeat the
translation.

### Status-line rendering

See § 17b — the WAN-N / LAN-N / R(+n) status elements are
independent (each per-channel red rule applies separately).
The recipe there is the single source of truth; don't re-
derive the badge logic. Pre-0.47.0 peers worked from a
conflated ``unshared_commits`` count that hid the "github
fine but LAN behind" case entirely — the WANOK label and
the per-channel red rule from § 17b are how this is now
made visible.

### Migration checklist — peer adopting LAN

For a peer that wants to expose LAN sync (most peers should —
the user-visible value is high):

1. **Expose the toggle.** A "Local Wi-Fi sync" entry in
   settings that reads ``lan_toggle()`` and writes via
   ``lan_set_toggle``. Use ``open_server_ui()`` to delegate
   to the daemon's settings page if your peer doesn't host
   project-bound settings.
2. **Wire the picker "Scan QR" entry.** The picker's "Pair
   with another phone" / "Receive a project" entry calls
   ``LAN_POPUPS.scan_to_pair()``; the popup handles the
   pair + clone + adopt-origin flow inline.
3. **Render the pending-decisions count on the picker.**
   ``len(lan_pending())`` → "(N waiting)" suffix on the
   receive-button label. Refresh on ``on_resume`` + after
   any pending-decisions gesture.
4. **Wire the share affordance.** A per-project "Share this
   project" button in the project context menu calls
   ``share_project_popup(langcode)``.
5. **Honor the new routing codes.** See § 17d. Critical: LAN
   codes are surfaced; don't drop them in the "everything
   else" catch-all.
6. **Re-poll ``project_status`` after pending-decisions
   gestures.** Accepting a share-offer triggers a LAN clone
   that changes ``last_project``; ``on_resume`` reconciliation
   (§ 14a) handles the resulting project switch, but only if
   the peer polls.

A peer that ships none of these still works — LAN sync runs
daemon-side and is observable through the existing
``project_status`` fields. The peer just won't be able to
PAIR / SHARE / CLONE without the daemon's settings UI.

### Security model

The LAN listener uses TLS with ``CERT_NONE`` deliberately
(stdlib ssl can't request a client cert without validating its
CA chain; peers' certs are self-signed and pinned by
fingerprint via ``peers.json``). Identity is body-claimed
under encrypted transport plus bound to user gesture (QR
display) for any auto-share. Practical implications:

- **Trust your LAN.** The threat model presumes the user
  controls the Wi-Fi they're on. A public-AP-shared LAN with
  attackers is *not* the design target; TLS still encrypts
  the transfer but identity can't be verified to the level a
  real PKI would give.
- **Don't expose the LAN listener over a tunnel / port-forward.**
  The auto-share gating is local-network-only safe; bridging
  it through an SSH tunnel or a VPN exit opens the surface to
  anyone who can reach the listener port.
- **Fingerprint mismatches are SECURITY events, not bugs.**
  ``LAN_FP_MISMATCH`` means a paired peer's cert changed
  out from under you — could be a legitimate re-pair, could
  be MITM. Surface, don't auto-rotate. Re-scan their QR to
  re-pair with the new fingerprint after verbal confirm.

## 20a. Shared decisions watcher (since 0.47.x)

Every pending decision the daemon stashes (share offers,
pair requests, adopt-origin / remote-conflict prompts) is
rendered by **a single shared client UI** —
``azt_collab_client.ui.decisions``. Peers must not reimplement
these popups, must not poll ``lan_pending`` themselves, and
must not own these surfaces.

### Hard rules

1. **Install the watcher exactly once at startup.** Call
   ``install_decision_watcher()`` from your App's ``on_start``
   (after ``bootstrap()`` returns). The watcher is a singleton;
   a second call replaces the interval / callback without
   spawning a second poll loop, but you should only need to
   call it once per process.
2. **Do not call ``lan_pending()`` from peer code.** The
   watcher owns that endpoint. A second poller racing the
   watcher will pop decisions twice (popup, accept, second
   pop on stale data).
3. **Do not render your own popups for the four kinds.** The
   watcher owns ``KIND_SHARE_OFFER`` / ``KIND_PAIR_REQUEST``
   / ``KIND_ADOPT_ORIGIN`` / ``KIND_REMOTE_CONFLICT``. Peer
   code that previously called ``pending_offers_popup`` /
   ``adopt_origin_popup`` directly should drop those calls —
   the watcher surfaces them automatically.
4. **Do not auto-load a newly-received project.** When the
   user accepts a ``KIND_SHARE_OFFER``, the clone is
   **passive** (``user_initiated=False``): the project lands
   in your list but ``last_project`` is unchanged. Your
   ``on_resolved`` callback may refresh the project list; it
   must NOT switch the loaded project. The user explicitly
   opens it later if they want.

### Public surface

```python
from azt_collab_client.ui import install_decision_watcher

class MyApp(App):
    def on_start(self):
        super().on_start()
        bootstrap(...)
        install_decision_watcher(
            poll_interval_s=1.0,    # default; clamp [0.5, 5.0]
            on_resolved=self._on_decision_resolved,
        )

    def _on_decision_resolved(self, kind, action, decision):
        # kind: 'share_offer' | 'pair_request' |
        #       'adopt_origin' | 'remote_conflict'
        # action: 'accept' | 'decline' | 'keep_mine' |
        #         'use_theirs' | 'both'
        # decision: the original {id, kind, params, created_at}
        if kind == 'share_offer' and action == 'accept':
            self.refresh_project_list()
        if kind == 'pair_request' and action == 'accept':
            self.refresh_peer_roster()
```

### Decision kinds

| Kind | Body params | Popup shows | Resolves via |
|---|---|---|---|
| ``share_offer`` | ``peer_id, device_name, langcode, repo_url, vernlang`` | "{device_name} wants to share project '{langcode}' with you." | ``lan_accept_offer`` (passive clone) / ``lan_decline_offer`` |
| ``pair_request`` *(new in 0.47.x)* | ``peer_id, fp, device_name, endpoint, langcode`` | "{device_name} wants to pair with this device." | ``lan_pair_request_resolve`` |
| ``adopt_origin`` | ``peer_id, device_name, langcode, url`` | "Back up project '{langcode}' to the Internet at {url}?" | ``lan_adopt_origin`` |
| ``remote_conflict`` | ``peer_id, device_name, langcode, existing_url, incoming_url`` | "Two Internet locations for project '{langcode}'. Pick which to use." | ``lan_resolve_conflict`` (modes: ``keep_mine``, ``use_theirs``, ``dual_publish`` — peer chooses via three-button row) |

Long ``device_name`` / ``langcode`` / URL values wrap inside
the popup rather than clipping. Don't pass these through a
shortener in your refresh hook; the watcher handles
presentation.

### Status codes routed through the watcher

- ``S.LAN_SHARE_OFFER`` — daemon emits when a paired peer's
  outbound share lands. Watcher renders.
- ``S.LAN_PAIR_REQUEST_PENDING`` — sender-side ack after the
  user taps Pair in the Nearby-unpaired list. Watcher does
  NOT render this — it's an informational status the host
  app may toast / spinner-render however it likes ("waiting
  for {device_name}…"). Auto-clears on accept / decline /
  timeout.
- ``S.LAN_PAIR_REQUEST_ACCEPTED`` / ``..._DECLINED`` /
  ``..._TIMEOUT`` — emitted by the sender's daemon when the
  receiver responds (or 5-min cap fires). Surface as a toast
  (translated via ``translate_status``).
- ``S.LAN_ADOPT_ORIGIN_NEEDED`` — overlaid on a clone Result;
  the receiver's pending-decisions list gains a corresponding
  ``adopt_origin`` entry. Watcher renders.
- ``S.LAN_REMOTE_CONFLICT`` — same pattern; watcher renders.

### Sender-side Nearby-pair flow (peer responsibilities)

The watcher handles the **receiver** side of pair requests.
The **sender** side is the Nearby-unpaired list in the peer
UI:

1. Read ``lan_nearby_unpaired()`` → list of mDNS-discovered
   devices not in our ``peers.json``. Each entry has
   ``peer_id``, ``fp``, ``device_name``, ``endpoint``.
2. Render with a "Pair…" button per row. On tap, call
   ``lan_pair_request_send(peer_id, langcode=current_project)``.
3. On the returned ``Result`` carrying
   ``S.LAN_PAIR_REQUEST_PENDING``, render a non-modal
   spinner / "waiting for {device_name}…" toast. The
   sender's daemon polls the receiver and emits
   ``S.LAN_PAIR_REQUEST_ACCEPTED`` / ``..._DECLINED`` /
   ``..._TIMEOUT`` on the next status fetch.
4. Refresh ``lan_list_peers()`` on accept; clear the spinner
   on any of the three terminal statuses.

### Migration checklist — pre-0.47 peers

For a peer that already ships LAN sync (§ 20):

1. Add the ``install_decision_watcher()`` call to
   ``on_start``.
2. Delete any peer-side polling of ``lan_pending``.
3. Delete any peer-side direct calls to
   ``pending_offers_popup`` / ``adopt_origin_popup`` (the
   watcher surfaces them automatically). The "Receive a
   project" picker button can stay for the
   first-pair-via-QR fallthrough, but it should NOT poll
   share-offers — those land via the watcher now.
4. If your peer kept its own "what to do when a project
   arrives" logic (e.g. auto-loading the new project), drop
   it. Passive clone is the contract; the user explicitly
   opens new projects.
5. Add a Nearby-unpaired section to your peer roster /
   settings screen that calls ``lan_nearby_unpaired`` and
   ``lan_pair_request_send`` per the sender-flow checklist
   above.

### What the watcher does NOT do

- It does not render the **outbound** "waiting for response"
  state — that's the host's job (the surface depends on
  whether it's a toast, a row spinner, or a modal). The
  watcher only renders **inbound** decisions.
- It does not handle QR-scan pairing — that path uses
  ``ui.lan_popups.scan_to_pair`` and runs synchronously in-
  flow. The watcher is for the asynchronous "someone else
  initiated" case.
- It does not surface ``S.CONTRIBUTOR_UNSET`` or other
  gating statuses — those belong on the affected gestures'
  return paths (toast + navigate to settings).

## 21. Project-shared KV and slot claims (since 0.47.9)

Cross-phone agreement on per-project state that doesn't fit
in the LIFT file: ``team_size``, "who's on which recording
slot", project-wide UI preferences, etc. Stored as plain
files under the project's working tree (``.azt/kv/`` and
``.azt/slots/``), committed and synced through the existing
LIFT pipeline, with conflict resolution wired into the
daemon's merge driver.

### Public API surface

```python
from azt_collab_client import (
    project_kv_get, project_kv_set, project_kv_list,
    list_slots, claim_slot, release_slot, rebind_slot,
)

# Scalar KV — every phone agrees on this value.
project_kv_set(langcode, 'team_size', 4)
team_size = project_kv_get(langcode, 'team_size', default=None)

# Slot claim — who's recording which range of the wordlist.
claim_slot(langcode, '2')           # claim slot 2 for this device
slots = list_slots(langcode)         # {'2': {peer_id, claimed_at,
                                     #        device_name}, ...}
release_slot(langcode)               # drop every slot held by us

# Identity-recovery: when our peer_id changed (server-APK
# reinstall regenerated crypto) but the user knows the slot is
# still theirs, rebind the existing claim to our current
# peer_id + device_name. Gate this behind a confirm popup that
# matches the user's contributor name against the existing
# claim's device_name (since 0.50.9).
rebind_slot(langcode, '2')
```

Notes on each:

- **``project_kv_get(langcode, key, default=None)``** — reads
  ``.azt/kv/<key>.txt``. Returns *default* on transport
  failure, unknown project, or unset key — peer code can
  treat empty / missing uniformly.
- **``project_kv_set(langcode, key, value)``** — writes
  ``.azt/kv/<key>.txt`` and fires a debounced
  ``commit_project`` so the change propagates via the
  existing sync pipeline. ``value`` is coerced to string;
  callers parse on read.
- **``list_slots(langcode)``** — returns
  ``{slot: {peer_id, claimed_at, device_name}}`` — the
  authoritative roster of who's on which slot. Empty dict
  if no claims exist yet.
- **``claim_slot(langcode, slot)``** — atomic (locally)
  displace-on-claim: any prior claim by this device on a
  *different* slot is dropped before the new file is
  written. The "one device, one slot" invariant holds
  per-device without coordination.
- **``release_slot(langcode)``** — removes every slot held
  by this device. Idempotent.
- **``rebind_slot(langcode, slot)``** — rewrites the
  ``peer_id`` + ``device_name`` of an existing claim to this
  daemon's current values, and refreshes ``claimed_at`` to now
  so the rebind wins any concurrent claim by another peer in
  the merge. Returns ``True`` on success, ``False`` if the
  slot doesn't exist (rebind only retags existing claims;
  use ``claim_slot`` for "claim or replace"). Use as the
  user-driven recovery path when ``list_slots`` shows a
  ``device_name`` matching the user's contributor but a
  ``peer_id`` that doesn't match this device's
  ``lan_peer_id()`` — likely a server-APK reinstall
  regenerated the crypto identity but the slot is still
  semantically the user's. Gate behind a confirm popup; the
  daemon doesn't ask any questions. Since 0.50.9.

### Locked semantics

The four design decisions from the 2026-05-28 architecture
discussion (anchored here so peer code can reason about
behaviour without spelunking the CHANGELOG):

1. **Convergent atomicity, not real-time.** Two phones can
   simultaneously claim the same slot — both commits land,
   both push, the merge driver picks one winner (per #4),
   the loser sees on next ``list_slots`` that they're not
   in the roster. Peer UI must re-prompt the user to pick
   again when ``list_slots`` excludes this device. There is
   no leader / quorum / coordinator; convergence happens at
   sync time, in-room latency ~60 s.

2. **Canonical key is ``peer_id``.** Slot records carry the
   device's ed25519 pubkey hex (64 chars) as the key.
   ``device_name`` is a display label only — it can change
   without invalidating any claim (e.g. when the user
   renames their contributor). Peer UI displays
   ``device_name``; peer logic compares ``peer_id``.

   **``lan_peer_id()`` guarantee (since 0.50.9).** The daemon
   eager-initialises the ed25519 keypair on every startup, so
   ``lan_peer_id().get('peer_id', '')`` is guaranteed to return
   a non-empty 64-char hex string on any daemon at 0.50.9+ with
   the ``cryptography`` package present (the build dependency
   that signs the cert). Suite APKs ship cryptography
   unconditionally; the guarantee is effectively unconditional
   in the field. Peers may safely drop legacy ``device_name``
   fallbacks for peer-identity matching against a 0.50.9+
   daemon. Pre-0.50.9 instances — and the rare host build
   without cryptography — return ``''`` with a logged warning;
   those are end-user upgrade prompts, not peer-side workaround
   territory.

3. **One file per slot.** ``.azt/slots/<slot>.txt`` content:

   ```
   <peer_id>
   <claimed_at_iso>
   <device_name>
   ```

   Simultaneous claims produce a natural git merge conflict
   that the daemon's merge driver resolves automatically
   (per #4). Peer code does NOT see conflict markers — it
   only sees the post-resolution state.

4. **Tiebreak: later ``claimed_at`` wins.** Merge picks
   the version whose embedded ISO timestamp is later
   (lexicographic compare = chronological for UTC ISO
   format). Ties on equal timestamps cascade through a
   stable chain so two NTP-synced phones claiming the same
   slot in the same second still converge on one winner
   (audit-#9 fix, 0.50.9):

   1. ``peer_id`` lexicographic (preferred).
      Since 0.50.9 the daemon eager-inits ``peer_id`` on
      startup, so any 0.50.9+ claim has a real 64-char hex
      pubkey here.
   2. ``device_name`` lexicographic (fallback for legacy
      claims with empty ``peer_id`` written by pre-0.50.9
      daemons).

   The tiebreak chain is a property of the claim itself
   (not of which side of the merge it landed on), so peer
   A and peer B both compute the same winner. Implementation
   detail — if field data ever shows misbehaviour, the daemon
   can switch tiebreak rules without changing the wire format.

### Hard rules

1. **Identity comes from the daemon, not the peer.** Don't
   pass ``peer_id`` or ``device_name`` to ``claim_slot``;
   the daemon uses its own. Same pattern as commit
   identity — peers don't pass ``contributor`` over the
   wire either.
2. **Claim refuses with ``CONTRIBUTOR_UNSET`` when no
   contributor name is set.** Route the user to the
   contributor field before retrying — same routing as the
   existing GH-publish path. Read paths
   (``project_kv_get`` / ``list_slots``) work without a
   contributor; only the claim gesture is gated.
3. **Peer-side UI must re-detect displacement.** If a peer
   was at slot 2 and a later ``list_slots`` doesn't include
   the peer's ``peer_id`` anywhere, prompt the user to
   pick again. There is no daemon-side notification for
   displacement — the absence in ``list_slots`` is the
   signal.
4. **Slot keys are safe filenames.** ``[A-Za-z0-9_]`` start,
   ``[A-Za-z0-9_.-]`` body, ≤64 chars. The daemon rejects
   anything else (``bad_slot``). Use simple numeric strings
   (``'1'``, ``'2'``, …) unless you have a specific reason.
5. **One slot per device.** ``claim_slot('2')`` while
   already holding slot 5 silently drops the prior claim.
   This is the displace-on-claim invariant. Peer UI may
   want to confirm before reclaiming if the user previously
   tapped a different slot.

### Recommended peer flow (word-list splitting use case)

The motivating case is splitting a SILCAWL recording across
a 3-person team. Each phone reads ``team_size`` + their
slot number to compute a CAWL range filter:

```python
team_size = int(project_kv_get(langcode, 'team_size',
                               default='0') or '0')
my_peer_id = lan_peer_id().get('peer_id', '')
slots = list_slots(langcode)
my_slot = next((s for s, claim in slots.items()
                if claim.get('peer_id') == my_peer_id),
               None)

if team_size and not my_slot:
    show_pick_slot_dialog(team_size, taken=set(slots))

if team_size and my_slot:
    apply_cawl_filter(compute_range(team_size, int(my_slot)))
```

- Read ``team_size`` + ``list_slots`` at project-open and
  on every sync completion.
- ``show_pick_slot_dialog`` displays slot buttons; tapped
  slot fires ``claim_slot(langcode, slot)``; the
  ``list_slots`` roster updates on next sync.
- ``apply_cawl_filter`` is the peer's existing per-range
  filter (recorder's ``apply_cawl`` path).

### Migration checklist — peer adopting the KV/slot feature

1. **Read ``team_size`` at project-open.** If unset,
   nothing to do (single-device project; existing manual
   filter behavior). If set, fall into the slot flow.
2. **Render a slot picker** when ``team_size`` is set AND
   this device isn't in ``list_slots``. Show only unclaimed
   slots by default with a "show all" override for the
   dead-phone-replacement case.
3. **Wire ``claim_slot`` to the picker tap.** Refresh
   ``list_slots`` after the call to confirm the claim
   landed.
4. **Recompute the CAWL filter on every sync completion
   when the source is "split".** Add a peer-side flag
   (``cawl_filter_source`` ∈ ``{'split', 'manual', None}``)
   so manual edits in the existing range textbox don't
   get clobbered.
5. **Route ``CONTRIBUTOR_UNSET`` from ``claim_slot``** to
   the contributor field. Existing GH-publish handling does
   the right thing here; route it the same way.

## What the suite does *for* you (keep code shareable)

The pieces below live in ``azt_collab_client`` and serve every
peer; don't duplicate them in your peer code:

- ``azt_collab_client.ui.bootstrap`` — startup install/update
  workflow (you call it; you don't reimplement it).
- ``azt_collab_client.ui.popups.install_server_apk_popup`` —
  canonical install/update popup, generic across server install,
  server update, and peer self-update; ``bootstrap()`` uses it;
  your code shouldn't invoke it directly.
- ``azt_collab_client.ui.update.check_for_update`` — generic GitHub
  Releases-driven downloader/installer with version comparison;
  your settings-screen "Update this app" button can use it
  directly when you want "Up to date" feedback.
- ``azt_collab_client.ui.update.install_apk_from_url`` — direct-URL
  download/install (no GitHub API); for stable redirect URLs.
- ``azt_collab_client.ui.share.share_running_apk`` — Android share
  sheet for the running APK.
- ``azt_collab_client.ui.popups.grant_collaborator_popup`` —
  per-project "invite a GitHub collaborator" popup; wire it from
  your project-context settings (see § 13).
- ``azt_collab_client.ui.popups.repo_access_popup(owner_repo, url)``
  — fallback for a ``REPO_NO_ACCESS`` result / ``last_sync_error``:
  explains the cause + offers **Open GitHub** to accept an invite or
  request access. (0.52.24+)
- ``azt_collab_client.ui.open_url(url)`` — ``ACTION_VIEW`` to the
  device browser (used by ``repo_access_popup``; also usable
  directly). (0.52.24+)
- ``azt_collab_client.ui.LangPickerScreen`` /
  ``ProjectPickerScreen`` / ``LiftHandle`` / ``MediaHandle`` — see
  ``azt_collab_client/CLAUDE.md`` for the longer rundown.
- ``azt_collab_client.translate.tr`` + the gettext catalog — every
  user-visible string the client owns is translated automatically.

If you find yourself writing peer code that touches GitHub
Releases, dispatches Android install Intents, or shows a "service
required" popup, stop and check whether the client already does
it. If it does, use the client. If it doesn't, either add it to
the client (so every peer benefits) or open a discussion about
whether it should live there.
