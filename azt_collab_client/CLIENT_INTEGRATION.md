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

## 14b. Sharing text / email / log-file helpers

Peers that need to dispatch a string or log file through
Android's share sheet — diagnostic dump, status report, log
attachment, etc. — should use the shared helpers in
``azt_collab_client.ui.share`` rather than inlining
``ACTION_SEND`` JNI plumbing per peer.

Three flavours:

```python
from azt_collab_client.ui.share import (
    share_text, email_text, share_log_file)

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
#    kilobyte limit; large payloads should prefer share_log_file.
email_text(
    text=some_short_dump,
    to='',
    subject=_('Diagnostic snapshot'),
    on_error=self._show_error,
)

# 3. File-based log share with optional previous-session
#    bundling. Inserts into MediaStore Downloads to get a real
#    content:// URI, attaches as EXTRA_STREAM. Receivers can
#    save as a file rather than read inline. Use for log
#    sharing where the payload may exceed text-extras limits.
share_log_file(
    log_path='/sdcard/azt_recorder.log',
    prev_path='/sdcard/azt_recorder.log.prev',  # optional
    on_error=self._show_error,
    # display_name='azt_log_20260513.log',     # optional
)
```

All three are Android-only — non-Android platforms invoke
``on_error`` with a translated message; same shape as
``share_running_apk``. All return ``bool`` indicating dispatch
success.

**Picking between the three.** Short text (< 100 KB) → either
``share_text`` (broad picker) or ``email_text`` (email only).
Larger payloads or anything you want to bundle with a previous
session log → ``share_log_file``. The daemon UI uses
``share_log_file`` to ship its ``$AZT_HOME/daemon.log`` blob.

**Bundled blob shape (``share_log_file``).**::

    === previous session (<prev_path>) ===
    <prev contents>

    === current session (<log_path>) ===
    <current contents>

Section breaks let the receiver scroll to the relevant
session.

**Why peer-shared.** Four+ surfaces need to dispatch through
the share sheet (recorder log, daemon log, recorder status
snapshot, future viewer diagnostic). Each was about to
re-derive ~30-50 lines of jnius autoclass + Intent
construction + MediaStore plumbing + error translation.
Extracted into ``ui/share.py`` alongside ``share_running_apk``
so every peer + the daemon UI uses the same code path, and a
future tightening of Android share APIs is one fix instead of N.

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
> ``project_status.commits_ahead`` / ``project_status.work_offline``.

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
| ``S.CONTRIBUTOR_UNSET`` | **Silent.** Log; sync refused until name set. | Route to daemon settings UI's contributor field. |
| ``S.WORK_OFFLINE_ENABLED`` | n/a — auto-commit doesn't see this (only ``sync_project`` emits it). | Toast "Work-offline mode is on" + ``open_server_ui()`` to the sync settings screen. The user explicitly turned the toggle on; the Sync button is the only path that surfaces the refusal. (0.43.0+.) |
| ``S.BUSY`` | **Silent.** Daemon's project_lock is held by another caller (almost always *this peer's* prior in-flight sync). Lock clears in milliseconds; the next regular tick covers it. Auto-sync surfaces nothing. | **Silent.** Even on user-gesture: showing "Another sync is in progress" toasts back-to-back is just punishing the user for the peer's missing in-flight guard. Optionally: debounce the Sync button so a fast double-tap fires once. See § 17c for the load-shedding rules that prevent ``S.BUSY`` in the first place. |
| ``S.JOB_INTERRUPTED`` | Retry once silently; if still failing, log and move on. | Retry; surface a transient-error toast if retry also fails. |
| ``S.INSUFFICIENT_MEMORY_FOR_MERGE`` | **Silent.** Daemon refused the merge because device free memory was below ``sync.min_free_mem_mb_for_merge`` (default 200 MB). Next drain cycle re-checks and proceeds when memory recovers — nothing the user can do mid-recording, and toasting "not enough memory" while they're working is just noise. Params: ``mem_available_mb`` (int), ``min_required_mb`` (int). 0.44.4+. | Translated toast naming the numeric headroom — the user explicitly asked, so they get the "close other apps, I'll retry" message. Translation already covers the wording. DO NOT route to settings — no per-project knob fixes RAM pressure. |
| ``S.SERVER_UNAVAILABLE`` / ``S.SERVER_ERROR`` | **Silent.** Log; daemon will be reachable next time. | Transient-error toast. DO NOT route to settings — no user-fixable config here. |
| ``S.AUTH_REFRESH_STALE`` | **Silent.** (Peers MAY show a non-intrusive settings banner via ``get_credentials_status()`` → ``github.refresh_broken``.) | Surface the translated toast — names GitHub Connect as the next step. DO NOT route, the toast text covers it. |
| ``S.DATA_LOSS_RISK`` | **SURFACE (not silenced).** This is a data-loss-class signal — files written by a peer aren't reaching git. The auto/user distinction does NOT apply: ALWAYS render the translated toast / banner with the maintainer-contact wording. Params: ``count`` (int), ``sample`` (up to 5 paths). | Same surface as auto-sync. |
| ``S.COMMIT_REPEATEDLY_FAILED`` | **SURFACE (not silenced).** Two-or-more successive ``COMMIT_FAILED`` for this project. Same data-loss-class severity as ``DATA_LOSS_RISK``: recordings are accumulating on the device but not entering git history. The catchup-commit pattern (one fat commit landing N stranded recordings after a long failure streak) is exactly what this catches — each prior failed attempt bumps the counter, and a second-or-later failure surfaces the loud status so the user is told to investigate before more files pile up uncommitted. Params: ``count`` (int, running streak), ``error`` (str, last dulwich message). Counter clears on the next successful commit. (The daemon also retries stuck commits in the background with exponential backoff, so the running ``count`` and the ``COMMIT_REPEATEDLY_FAILED`` your peer sees on the next sync attempt may reflect failures the peer never directly triggered. Peers don't need to do anything different — the existing result-iteration handles it.) | Same surface as auto-sync. |
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
S.CONTRIBUTOR_UNSET       # 'CONTRIBUTOR_UNSET'
S.JOB_INTERRUPTED         # 'JOB_INTERRUPTED'
S.INSUFFICIENT_MEMORY_FOR_MERGE  # ← 0.44.4+, daemon refused merge under memory pressure
S.WORK_OFFLINE_ENABLED    # 'WORK_OFFLINE_ENABLED' ← 0.43.0+, sync_project only
S.BUSY                    # 'BUSY'                ← project_lock held; silent (§ 17c)
S.SERVER_UNAVAILABLE      # 'SERVER_UNAVAILABLE'  ← 0.41.13+
S.SERVER_ERROR            # 'SERVER_ERROR'        ← 0.41.13+
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
  migrate that logic onto ``project_status.commits_ahead``.
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
(``commits_ahead``, ``work_offline``).

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
# Render the badge from project_status without a second RPC.
# Since 0.45.0 the indicator encodes two orthogonal axes:
#
#   - commits_ahead vs github (existing axis since 0.43)
#   - unshared_commits vs ANY remote (github OR any LAN peer)
#
# unshared_commits=0 + commits_ahead>0 means "5 ahead of github
# but all 5 exist on at least one paired phone" → render as
# ``LANOK +5``: the user can't lose data even if this phone dies.
#
# unshared_commits>0 means at least one local commit lives nowhere
# else → render as ``+{unshared}/{ahead}``: data-loss-risk that
# count of commits are wholly local. The slash is the read: 1 of 5.
#
# work_offline + lan_allow_sync combos still affect the suffix:
#
#   work_offline=off, lan=off  → no suffix (github-mediated)
#   work_offline=off, lan=on   → no suffix (github + LAN both push)
#   work_offline=on,  lan=off  → "offline"
#   work_offline=on,  lan=on   → "LAN-only"
ps = project_status(langcode)

if ps.work_offline and ps.lan_allow_sync:
    mode_suffix = " · LAN-only"
elif ps.work_offline:
    mode_suffix = " · offline"
else:
    mode_suffix = ""

if ps.commits_ahead == 0:
    sync_indicator.text = ""  # all up to date with github
elif ps.unshared_commits == 0:
    # Every local commit is on github OR a paired phone.
    sync_indicator.text = f"LANOK +{ps.commits_ahead}{mode_suffix}"
else:
    # ``+unshared/ahead`` — N commits live nowhere else.
    sync_indicator.text = (
        f"+{ps.unshared_commits}/{ps.commits_ahead}{mode_suffix}")
```

### Badge refresh obligation — peer MUST re-poll after every sync gesture

The daemon has no channel to push state changes into a running
peer; ``project_status`` is the only surface, and it's pull-only.
That means every gesture that mutates ``commits_ahead`` /
``work_offline`` on the daemon side leaves the peer's last-
seen ``ProjectStatus`` snapshot stale until the peer re-fetches.
Peers MUST re-call ``project_status(langcode)`` and re-bind the
badge:

1. **In the result handler of every sync gesture.** Whether
   the gesture was the user-pressed Sync button
   (``sync_project``), a debounced background commit
   (``commit_project`` + ``poll_job``), or a peer-side
   work-offline toggle (``set_work_offline``). Don't read the
   gesture's own ``Result`` for the new ``commits_ahead`` —
   ``Result`` carries status codes, not state. Always re-poll
   ``project_status``.
2. **On a low-rate background tick.** Daemon-driven push
   happens on the scheduler's drain loop without a peer
   gesture; without a background poll the badge would stay at
   the last-gesture-time value indefinitely. 5-15 s is the
   right range — fast enough that the user sees the
   ``commits_ahead`` drop after a background push, slow enough
   that the RPC cost stays trivial.
3. **On ``on_resume`` / activity-foreground.** Same hook used
   for project-switch reconciliation (§ 14a). The daemon may
   have drained while the peer was backgrounded.

Field symptom this section closes (2026-05-18): UI badge
sticky at ``(+160)`` while daemon log showed successive
``[sync-rpc] 'baf' done: codes=['NOTHING_TO_COMMIT', 'PUSHED']``
— daemon was at zero commits_ahead, peer never re-polled.

### Migration checklist (from pre-0.43 peer)

1. Swap ``request_sync(langcode)`` for ``commit_project(langcode)``
   (or keep ``request_sync`` — kept as an alias). Both go
   through the new commit-only path.
2. Strip any post-RPC code that polls for ``PUSHED`` /
   ``COMMITTED_AND_PUSHED`` on the ``request_sync`` result.
   Replace with periodic ``project_status`` reads of
   ``commits_ahead``.
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
badge nowhere, and (worst) flapping ``commits_ahead`` numbers
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
(``commits_ahead > 0`` watcher firing a sync). State-based
triggers create feedback loops where the daemon's own state
update kicks off the next round of RPCs.

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
