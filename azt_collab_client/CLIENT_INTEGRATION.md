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
suite stays coherent. Re-read it whenever you bump the bundled
client; the contract evolves with the client and silent drift
is what produced the v0.28.x bugs ("multiple stacked popups",
"settings page reachable when no server", "no progress
indicator", "Dismiss didn't quit when it should have").

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
APK. **Point at the canonical filesystem path, not through your
``android/`` symlink** — buildozer's ``android.add_src`` doesn't
reliably follow symlinks across versions:

```ini
# Mirror what server_apk/buildozer.spec.tmpl does — relative path
# that hits the canonical azt-collab tree directly.
android.add_src = ../azt-collab/android/src/main/java
```

Adjust the ``..`` count if your peer doesn't sit as a sibling of
``azt-collab/``. Without this line peers see ``[android_cp]
AZTServiceConnector.ensureBound failed: ClassNotFoundException``
in logcat on every cold start, the bind never happens, and the
freezer issue is unmitigated.

If that still fails (older buildozer / path-policing edge case),
fall back to copying the file directly into the peer repo:

```bash
mkdir -p java_src/org/atoznback/aztcollab
cp ../azt-collab/android/src/main/java/org/atoznback/aztcollab/\
AZTServiceConnector.java java_src/org/atoznback/aztcollab/
# in buildozer.spec:
# android.add_src = java_src
```

Brittle (the copy goes stale when the canonical changes); only
use if the relative-path approach doesn't work.

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
duplication:

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
```

If you don't have your own catalog (small peer): nothing to do.
The client catalog applies automatically when a language is
selected from the daemon's settings UI.

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

If the peer warms a working-set of CAWL images on project
load, it MUST do so via the daemon's ``cawl_prefetch``
endpoint AND surface a user-visible progress indicator while
the warm runs. Without the indicator, users have no way to
tell the daemon is using network in the background; they
naturally disconnect Wi-Fi between gestures and end up with
a half-warm cache that then blocks on demand-fetch for every
uncached image.

**Daemon-driven, not peer-driven.** The peer used to iterate
its working-set itself, calling ``CAWLHandle(...).open_read``
once per image. That worked but left the daemon ignorant of
how many images the peer was warming — its progress
indicator could only count "files on disk vs. all image
entries in the index", and the canonical CAWL repo has 2-4
image variants per CAWL identifier so the index's total
over-counts what the peer actually fetches. Result: a
progress bar that plateaus far short of "100%" with no way
for the user to tell whether it's done. Daemon-driven puts
the iteration on the party that does the actual fetching,
so progress is accurate by construction.

The split:

- **Bulk warming** → ``cawl_prefetch(langcode, paths)``. Peer
  hands the daemon its working-set once; daemon iterates in a
  background thread and reports progress.
- **On-demand fetch** → ``CAWLHandle(...).open_read`` for any
  individual image the peer needs to display *right now*
  (current swipe target, etc.). Still daemon-served from
  cache or fetched if missing — same backing store the bulk
  warm populates.

#### Wiring

```python
from kivy.clock import Clock
from azt_collab_client import (
    cawl_index, cawl_prefetch, cawl_cache_status)

def _start_cawl_warm(self, langcode):
    """Kick off the daemon-driven prefetch + the progress
    indicator. Call once per project load."""
    # 1. Compute the working set this peer cares about. The
    # canonical CAWL repo has multiple variants per identifier;
    # pick the one your UI will actually render.
    index = cawl_index(langcode)
    paths = self._choose_one_variant_per_cawl_id(
        index.get('files') or [])
    if not paths:
        return  # no image_repo, nothing to do
    # 2. Hand the list to the daemon. Returns immediately;
    # warm runs in the daemon's background thread.
    cawl_prefetch(langcode, paths)
    # 3. Start the progress poll at 1 Hz.
    self._cache_status_langcode = langcode
    self._cache_status_last = (-1, -1)
    self._cache_status_event = Clock.schedule_interval(
        lambda _dt: self._tick_cache_status(), 1.0)
    self._tick_cache_status()

def _tick_cache_status(self):
    status = cawl_cache_status(self._cache_status_langcode)
    cached, total = status['cached'], status['total']
    # Log ONLY on state change so a 1 Hz poll doesn't fill
    # logcat with identical lines.
    if (cached, total) != self._cache_status_last:
        print(f'[cache-status] {cached}/{total}',
              file=sys.stderr)
        self._cache_status_last = (cached, total)
    if total == 0:
        self._hide_cache_indicator()
        return
    if cached >= total:
        self._hide_cache_indicator()
        self._cache_status_event.cancel()
        return
    self._show_cache_indicator(
        _('Caching images: {cached} / {total} '
          '(network in use — please stay online)').format(
              cached=cached, total=total))
```

Where the indicator lives is peer-specific (collab screen
status line, persistent toast, banner above the main
content) — what matters is that it's visible during the
natural waiting moments AND the wording makes clear that
network is being used. "Loading…" doesn't cut it; the user
already assumes loading. The phrase to convey is "don't
disconnect."

**Polling cadence.** 1 Hz is the right interval — feels live,
and the daemon's ``cache_status`` is O(1) (counter increment
per fetch, no per-poll filesystem scan). Stop polling once
``cached >= total`` so an idle peer doesn't keep waking the
daemon. Log only on state change; a fixed 1 Hz log of
unchanged values is just noise.

**On-demand still works.** Peers don't have to wait for the
prefetch to finish before opening individual images. The
``CAWLHandle(...).open_read`` path serves from cache or
fetches on demand; if the prefetch worker hasn't reached a
specific image yet but the user navigates to it, the
on-demand request fetches it directly (and the worker will
skip it later via the cache-hit fast path).

**Backward compatibility.** Pre-0.41.11 daemons return
``not_found`` for ``cawl_prefetch``; the wrapper returns
``{requested: 0, completed: 0, finished: True}`` and the
peer's progress poll sees the no-prefetch fallback semantics
of ``cache_status``. Pre-0.41.9 daemons return ``not_found``
for ``cache_status`` too; the wrapper returns
``{cached: 0, total: 0}``, which trips the "nothing to show"
branch and hides the indicator. Either way: no peer-side
version pin needed; call sites degrade gracefully.

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
  ``request_sync`` as of 0.40.0. Pre-migration peer code that
  still passes it in the body will have the value silently
  ignored by the daemon — but you should remove the call-site
  arg as part of the upgrade so the code matches the wire.
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
- ``poll_job(job_id)`` for an async ``request_sync`` that the
  scheduler refused at exec time (defence-in-depth).

**UI for setting these.** Both fields should live on the
daemon settings UI's "User identity" surface — the daemon
settings UI is the canonical home (peer apps delegate via
``open_server_ui()``). A peer with its own first-run flow
MAY prompt for the contributor name inline to spare the user
the round-trip to settings on day one, but the persist call
goes through ``set_contributor`` exactly the same way; the
data lives on the daemon.

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
