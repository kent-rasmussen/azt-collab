# Client integration checklist

Every AZT-suite peer (recorder, viewer, future apps) is a thin
``azt_collab_client`` consumer. This doc is the **single contract**
each peer follows so the suite stays coherent. Re-read it whenever
you bump the bundled client; the contract evolves with the client
and silent drift is what produced the v0.28.x bugs the user reported
("multiple stacked popups", "settings page reachable when no
server", "no progress indicator", "Dismiss didn't quit when it should
have").

If you're starting a brand-new peer, work through every section in
order. If you're updating an existing peer, treat each section
heading as a checklist item and confirm.

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
        peer_asset_filename='azt_recorder.apk',      # ← your asset name
        peer_display_name='AZT Recorder',            # ← your app name
        on_status=self._set_status,                  # progress sink
        on_error=self._set_status,                   # failure surface
        font_name=self._font_name,
    )

def _set_status(self, message):
    # Surface to your in-app log + stderr (logcat on Android).
    print(f'[bootstrap] {message}', file=sys.stderr)
    try:
        self.root.ids.sm.get_screen('home')._set_log(message)
    except Exception:
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

## 5. Translation

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

## 6. App.title

The bootstrap popup's Quit button reads ``"Quit {App.title}"``
(e.g. "Quit AZT Recorder"). Set ``title`` on your App subclass:

```python
class RecorderApp(App):
    title = 'AZT Recorder'
```

If you don't, the button just says "Quit" — functional but less
obvious.

## 7. LIFT file access

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

## 8. Audio / image references

For audio recording:

```python
from azt_collab_client.lift_io import audio_uri_for, MediaHandle
handle = MediaHandle(audio_uri_for(lift_path_or_uri, basename),
                     kind='audio')
with handle.open_write() as f:
    # …record into f.fileno()
    pass
```

For image rendering: ``MediaHandle(..., kind='image')`` for read
only — peers cannot write images (the daemon owns image additions).

## 9. Recovery

The single peer-visible recovery surface is
``Result.has(S.JOB_INTERRUPTED)`` from ``request_sync`` /
``poll_job``. Treat it as transient and retry. Synchronous
``sync_project`` callers don't see this code — the transport
retries internally.

## 10. Testing

The suite has a pytest scaffold in ``azt-collab/tests/``. Run
``pytest tests/ -q`` from the canonical repo before publishing a
client-bundling peer. Manual matrix is in
``azt-collab/docs/test_plan.md`` § 8.

## What the suite does *for* you (keep code shareable)

The pieces below live in ``azt_collab_client`` and serve every
peer; don't duplicate them in your peer code:

- ``azt_collab_client.ui.bootstrap`` — startup install/update
  workflow (you call it; you don't reimplement it).
- ``azt_collab_client.ui.popups.install_server_apk_popup`` —
  canonical "no server" popup; ``bootstrap()`` uses it; your
  code shouldn't.
- ``azt_collab_client.ui.update.check_for_update`` — generic GitHub
  Releases-driven downloader/installer; bootstrap uses it; your
  settings-screen "Update this app" button can use it directly.
- ``azt_collab_client.ui.share.share_running_apk`` — Android share
  sheet for the running APK.
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
