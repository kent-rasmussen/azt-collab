"""
Picker helper subprocess for `python -m azt_collabd projects`.

Hosts ProjectPickerScreen + LangPickerScreen. All create flows
(open / clone / start-new) run inside this process and converge on
emitting the chosen project path through the same exit channel:

    Desktop: writes ``AZT_PICK\\t<absolute-lift-path>\\n`` on stdout,
    exits 0. Cancel / window-close exits 1 with no AZT_PICK line.

    Android: setResult(RESULT_OK, intent.putExtra('path', ...));
    finish(). Cancel calls setResult(RESULT_CANCELED) before finish.

Sister apps don't run this directly — they call
``azt_collab_client.pick_project()`` which spawns this on desktop or
fires an Intent to the server APK on Android.
"""

import os
import sys

# Quiet Kivy console logging so stdout stays clean for the AZT_PICK
# sentinel line. Must be set before importing kivy.
os.environ.setdefault('KIVY_NO_CONSOLELOG', '1')

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.screenmanager import (
    NoTransition, ScreenManager, SlideTransition,
)
from kivy.utils import platform

import azt_collab_client
from azt_collab_client import (
    S, clone_project, create_project_from_template, list_projects,
    register_project, translate_status,
)
from azt_collab_client import i18n as _client_i18n
from azt_collab_client.translate import tr as _tr
from azt_collab_client.ui import (
    LangPickerScreen, ProjectPickerScreen,
    clone_url_popup, confirm_langcode_popup, register_charis,
    register_langpicker_kv, register_picker_kv, theme,
)
# Settings live in azt_collabd.ui.app; host them in-process so the
# picker's gear navigates within our own ScreenManager instead of
# launching an Intent / subprocess. The Android server APK has a
# single PythonActivity, so an Intent that asks Android to "launch
# the settings UI" of a package whose activity is currently running
# the picker just collapses the task back to the calling app.
from azt_collabd.ui.app import (
    GitHubConnectScreen, GitLabFormScreen, SettingsScreen,
    register_kv as register_settings_kv,
)


_AZT_ICON = os.path.join(
    os.path.dirname(azt_collab_client.__file__), 'azt.png')


# AZTCollabProvider authority — keep in sync with the value declared
# in azt_collab_client/transports/android_cp.py and in
# server_apk/manifest_extras.xml / p4a_hook.py.
_PROVIDER_AUTHORITY = 'org.atoznback.aztcollab'


def _tentative_langcode_from_url(url):
    """Cheap derivation of the candidate langcode from a clone URL,
    used to prefill the confirm-langcode popup before the clone
    fetches anything. Mirrors the daemon-side
    ``projects.derive_langcode`` priority order (URL repo basename
    minus ``.git``); the user can override in the popup."""
    name = (url or '').rstrip('/').split('/')[-1]
    if name.endswith('.git'):
        name = name[:-4]
    return name or 'project'


def _tentative_langcode_from_lift(path):
    """Cheap derivation from a LIFT filename, used to prefill the
    confirm-langcode popup for the open-file flow before
    registration commits."""
    base = os.path.basename(path or '')
    if base.lower().endswith('.lift'):
        base = base[:-5]
    return base or 'project'


def _to_content_uri(abs_path, home_dir):
    """Translate an absolute lift-file path under
    ``$AZT_HOME/projects/`` to a ``content://<authority>/<rel>`` URI
    served by ``AZTCollabProvider.openFile`` (which routes back to
    ``$AZT_HOME/projects/<rel>`` via ``_resolve_path``). Returns the
    original path unchanged when it sits outside the ``projects/``
    subtree — that path is for desktop / non-Android use, where peers
    share the daemon's filesystem and ``open()`` works directly."""
    projects_root = os.path.realpath(os.path.join(home_dir, 'projects'))
    real_path = os.path.realpath(abs_path)
    try:
        # commonpath raises ValueError on cross-volume paths; treat
        # any failure as "outside the projects subtree".
        if os.path.commonpath([projects_root, real_path]) != projects_root:
            return abs_path
    except ValueError:
        return abs_path
    rel = os.path.relpath(real_path, projects_root)
    return f'content://{_PROVIDER_AUTHORITY}/{rel}'


_KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T azt_collab_client.ui.theme
#:set FONT '{font_name}'

# RecBtn / NavBtn / SectionLabel / TopBar etc. come from
# azt_collabd.ui.app.register_kv (called from build() before this
# template loads), so the picker doesn't redefine them.

<_PickerRoot>:
    ProjectPickerScreen:
        name: 'picker'
    LangPickerScreen:
        name: 'langpicker'
    SettingsScreen:
        name: 'settings'
        back_to: 'picker'
    GitHubConnectScreen:
        name: 'github'
    GitLabFormScreen:
        name: 'gitlab'
'''


class _PickerRoot(ScreenManager):
    pass


class PickerApp(App):
    """Standalone picker app. Implements every host-contract callback
    the shared screens require; every successful flow ends in
    ``_emit_and_quit(path)``."""

    # Title / subtitle are populated in build() so they pick up the
    # active translation; refreshed by _check_language_change after a
    # live language switch.
    title = 'A-Z+T'
    subtitle = StringProperty('')
    icon = StringProperty(_AZT_ICON)
    # Initialized in on_start() to ``client X · server Y`` once the
    # server's version is known. Shown as ``client X · server ?`` if
    # the daemon is unreachable.
    version_string = StringProperty(f'client {azt_collab_client.__version__}')

    # Process exit code; flipped to 0 on a successful emit. main()
    # reads this after App.run() returns.
    _exit_code = 1

    # Loading overlay (LangPickerScreen calls _show_loading_overlay
    # before new_from_template; we dismiss in the worker callback).
    _loading_overlay = None

    # Set by LangPickerScreen on Continue.
    _pending_vernlang = ''

    # Resolved by build(); 'CharisSIL' if the TTFs were found,
    # otherwise 'Roboto'. Read by the modal overlays.
    _font_name = 'Roboto'

    # ── Lifecycle ─────────────────────────────────────────────────────
    def build(self):
        theme.set_theme('Ocean')
        self._font_name = register_charis()
        # Apply persisted UI language before anything renders so the
        # initial paint is in the right language. azt_collab_client.i18n
        # auto-applies on import, but reapplying here is harmless and
        # keeps the contract explicit.
        _client_i18n.set_language(_client_i18n.language_pref())
        self.subtitle = _tr('Pick a project')
        # Order matters: settings KV defines TopBar / NavBtn / SectionLabel /
        # BodyLabel etc., which the SettingsScreen rule references. Load
        # before our own KV so the _PickerRoot rule (which instantiates
        # SettingsScreen) finds those class rules in scope.
        register_settings_kv(font_name=self._font_name)
        Builder.load_string(_KV_TEMPLATE.format(font_name=self._font_name))
        register_picker_kv(font_name=self._font_name, hide_settings_gear=False)
        register_langpicker_kv(font_name=self._font_name)
        self.sm = _PickerRoot(transition=SlideTransition())
        return self.sm

    def go(self, name):
        """Used by SettingsScreen / GitHubConnectScreen / GitLabFormScreen
        KV (``app.go('github')`` etc.) to navigate within our
        ``ScreenManager``. Mirrors ``CollabUIApp.go`` so the same KV
        bindings work in either host."""
        if self.sm.has_screen(name):
            self.sm.current = name

    def on_pause(self):
        """Permit Kivy to pause when the Activity backgrounds. Returning
        True tells Kivy to suspend its run loop instead of treating
        pause as a fatal stop. Important on Android because the picker
        Activity finishes (via setResult/finish) without bringing down
        the host process — the AZTServiceProviderhost service keeps
        running, and so does this Kivy app in a paused state.
        Without this hook Kivy default-fails on the missing GL surface
        and stops the app, which would propagate to sys.exit and take
        the provider down with it."""
        return True

    def on_start(self):
        """Once the app is running, watch ``$AZT_HOME/config.json`` so
        a language change made in a settings subprocess (opened via
        the gear) live-rebuilds the picker. Mtime polling at 1 Hz is
        cheap and avoids platform-specific inotify."""
        import azt_collabd
        self._config_path = os.path.join(
            azt_collabd.paths.azt_home(), 'config.json')
        self._config_mtime = self._get_config_mtime()
        Clock.schedule_interval(self._check_language_change, 1.0)
        # Android hardware back button does not flow through
        # App.on_request_close; it surfaces as a key 27 event on
        # Window.on_keyboard. Bind the same back-nav logic there so
        # the OS back button matches the in-screen "← Back" button:
        # non-picker → picker; picker → finish (RESULT_CANCELED).
        from kivy.core.window import Window
        Window.bind(on_keyboard=self._on_back_button)
        # Probe the server version once at startup so the bottom strip
        # shows both halves. Done off the UI thread so a slow daemon
        # doesn't block first paint.
        import threading
        threading.Thread(target=self._probe_server_version,
                         daemon=True).start()

    def _probe_server_version(self):
        try:
            compat = azt_collab_client.check_server_compat()
            err = ''
        except Exception as ex:
            compat = {}
            err = f'{type(ex).__name__}: {ex}'
        # Diagnostic surfacing: when the probe doesn't return a real
        # server version, render *why* directly into the version strip
        # instead of a bare ``?``. ``check_server_compat`` returns
        # one of: {ok: True, server_version} on success;
        # {ok: False, error: 'server_unreachable'|'server_too_old'|
        # 'client_too_old', server_version: ?, ...} otherwise.
        # An exception path (rare; rpc/transport bugs) falls into
        # ``err``.
        if isinstance(compat, dict):
            sv = compat.get('server_version') or ''
            ce = compat.get('error') or ''
            cd = compat.get('detail') or ''
        else:
            sv = ''
            ce = ''
            cd = ''
        if sv:
            label = f'server {sv}'
        elif err:
            label = f'server ? ({err[:60]})'
        elif ce:
            label = (f'server ? ({ce}: {cd[:60]})' if cd
                     else f'server ? ({ce})')
        else:
            label = 'server ?'
        print(f'[picker] _probe_server_version: compat={compat!r} '
              f'err={err!r}', flush=True)
        Clock.schedule_once(
            lambda dt: setattr(
                self, 'version_string',
                f'client {azt_collab_client.__version__}'
                f'  ·  '
                f'{label}'),
            0)

    def _get_config_mtime(self):
        try:
            return os.path.getmtime(self._config_path)
        except OSError:
            return 0.0

    def _check_language_change(self, _dt):
        new_mtime = self._get_config_mtime()
        if new_mtime == self._config_mtime:
            return
        self._config_mtime = new_mtime
        persisted = _client_i18n.language_pref()
        if persisted == _client_i18n.current_language():
            return
        _client_i18n.set_language(persisted)
        self.subtitle = _tr('Pick a project')
        sm = self.sm
        old_t = sm.transition
        sm.transition = NoTransition()
        # See azt_collabd/ui/app.py:_set_ui_language for why we
        # capture back_to: properties set by the parent KV rule
        # (e.g. ``back_to: 'picker'`` on SettingsScreen in
        # ``_PickerRoot``) live on the *instance*. Re-instantiating
        # from the class alone loses them.
        screens_info = [
            {'name': s.name, 'cls': type(s),
             'back_to': getattr(s, 'back_to', '')}
            for s in list(sm.screens)
        ]
        current = sm.current
        sm.clear_widgets()
        for info in screens_info:
            screen = info['cls'](name=info['name'])
            if info['back_to']:
                screen.back_to = info['back_to']
            sm.add_widget(screen)
        if current in [info['name'] for info in screens_info]:
            sm.current = current
        Clock.schedule_once(
            lambda dt: setattr(sm, 'transition', old_t), 0.1)

    # ── Result emission ───────────────────────────────────────────────
    def load_lift(self, path, langcode=''):
        """Existing-project tap. Routed here from the picker's project
        list. The list builder stores the project's canonical
        langcode on each button (the same value keying the
        daemon's projects.json), so peers don't have to reverse
        the URI. Logs and refuses empty paths so a stale
        registration with no lift_path can't silently land at the
        recorder as ``no_path``."""
        print(f'[picker_app] load_lift(path={path!r} '
              f'langcode={langcode!r})',
              file=sys.stderr, flush=True)
        self._emit_and_quit(path, langcode=langcode)

    def _emit_and_quit(self, path, langcode=''):
        """Write the chosen path and the canonical langcode and stop
        the app. On Android sets the Activity result instead of
        writing stdout.

        Protocol: ``AZT_PICK\\t<path>\\t<langcode>\\n``. ``langcode``
        is the daemon's ``projects.json`` key for the project — the
        single source of truth across the suite. Every emit-path
        threads it through:
        - clone-flow: ``_after_clone_ok`` reads it from the daemon's
          clone-job response (which was set on auto-register).
        - open-file flow: derived by ``register_project`` (we capture
          the returned ``Project.langcode``).
        - existing-project tap: stored on the button at populate time.
        - template-download flow: the user-typed BCP-47 tag.

        Peers should prefer this extra over re-deriving from the
        URI's first path segment — the URI form happens to use the
        langcode as the directory name today, but nothing in the
        contract says it must.

        Refuses to emit an empty path: on Android that lands at the
        peer's ``pick_project_android`` handler as ``RESULT_OK`` with
        no extra and surfaces as ``no_path``. If we got here with no
        path, something upstream is broken — log it loudly and stay
        in the picker so the user can pick again."""
        if not path:
            print(f'[picker_app] _emit_and_quit refused: empty path '
                  f'(langcode={langcode!r}). Stack trace follows.',
                  file=sys.stderr, flush=True)
            import traceback
            traceback.print_stack(file=sys.stderr)
            self._show_error(_tr(
                'Internal error: tried to return an empty path. '
                'Please pick again or report this.'))
            return
        if platform == 'android':
            try:
                from jnius import autoclass
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                Intent = autoclass('android.content.Intent')
                Uri = autoclass('android.net.Uri')
                Bundle = autoclass('android.os.Bundle')
                activity = PythonActivity.mActivity
                # Translate the daemon-private filesystem path to a
                # content:// URI under our AZTCollabProvider. Peers
                # must NOT open the absolute path directly: it lives
                # in the server APK's sandbox
                # (/data/user/0/org.atoznback.aztcollab/files/...),
                # which other packages have no UID-level read on.
                # Going through the URI uses the provider's openFile
                # callback (resolveAbsPath) — single source of truth,
                # serialized by the daemon, no peer-side caching.
                import azt_collabd
                emitted_path = _to_content_uri(
                    str(path), azt_collabd.paths.azt_home())
                extras = Bundle()
                extras.putString('path', emitted_path)
                if langcode:
                    extras.putString('langcode', str(langcode))
                data = Intent('org.atoznback.aztcollab.PICK_PROJECT')
                data.putExtras(extras)
                # If we produced a content URI, surface it on
                # Intent.data and grant the recipient temporary
                # read+write so its ContentResolver.openFileDescriptor
                # call goes through. Without these flags the peer
                # gets SecurityException despite holding the
                # signature-level AZT_COLLAB_ACCESS permission for
                # other provider operations.
                if emitted_path.startswith('content://'):
                    data.setData(Uri.parse(emitted_path))
                    data.addFlags(
                        Intent.FLAG_GRANT_READ_URI_PERMISSION
                        | Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                # Diagnostic round-trip: read the value back out
                # before sending so a regression in Bundle/Intent
                # plumbing is loud in logcat instead of surfacing as
                # the peer reporting "no_path" with no clue why.
                verify_path = data.getStringExtra('path')
                print(f'[picker_app] result intent verify: '
                      f'path={verify_path!r}',
                      file=sys.stderr, flush=True)
                activity.setResult(-1, data)  # RESULT_OK = -1
                activity.finish()
            except Exception as ex:
                # Fall through to stdout so a misconfigured Android
                # build doesn't silently lose the result.
                sys.stderr.write(
                    f'[picker_app] android setResult failed: {ex}\n')
                sys.stdout.write(f'AZT_PICK\t{path}\t{langcode}\n')
                sys.stdout.flush()
        else:
            sys.stdout.write(f'AZT_PICK\t{path}\t{langcode}\n')
            sys.stdout.flush()
        self._exit_code = 0
        if platform == 'android':
            # Activity is finishing, but the AZTServiceProviderhost
            # sticky-bound service must keep the host process alive so
            # AZTCollabProvider can still serve openFileDescriptor()
            # to the peer that just received the URI grant. Calling
            # self.stop() here ends Kivy's run loop, which causes the
            # picker_app.main() sys.exit() at the bottom of this file
            # to terminate the process — taking the provider with it
            # and triggering Android's "depends on provider in dying
            # proc" cascade SIGKILL of the peer. So on Android we
            # leave Kivy running headless; the service idle-stop
            # policy decides when the process actually exits.
            return
        self.stop()

    def _navigate_back(self):
        """Handle a back-press inside the picker subprocess.

        Three classes of screen, three behaviours:

        * Sub-screens with somewhere to go (settings / github / gitlab
          when reached from the picker) — pop back to the screen named
          by ``back_to`` (default ``'picker'``).
        * The project-picker screen itself and the langpicker — exit
          the picker subprocess and return the user to the recorder.
          When ``last_project()`` resolves to a live registered
          project we emit it so the recorder auto-resumes; otherwise
          we emit a clean cancel (the recorder's ``_handle_pick``
          silently returns to whatever it was showing). Either way
          the user lands on the recorder, never on a stale picker
          screen with one more back-press needed to actually exit.
        * Anything else with no ``back_to`` and no matching screen
          — return False so Kivy / Android default-close fires."""
        if not hasattr(self, 'sm'):
            return False
        if self.sm.current in ('picker', 'langpicker'):
            self._exit_to_last_project_or_cancel(
                from_screen=self.sm.current)
            return True
        cur = (self.sm.get_screen(self.sm.current)
               if self.sm.has_screen(self.sm.current) else None)
        target = getattr(cur, 'back_to', '') or 'picker'
        if self.sm.has_screen(target):
            self.sm.current = target
            return True
        return False

    def _exit_to_last_project_or_cancel(self, from_screen):
        """Emit ``last_project`` if it resolves; cancel otherwise.
        Centralised so both back-from-picker and back-from-langpicker
        produce the same exit shape."""
        try:
            from azt_collab_client import last_project, open_project
            langcode = (last_project() or '').strip()
            project = open_project(langcode) if langcode else None
        except Exception:
            project = None
        if project is not None and project.lift_exists \
                and project.lift_path:
            print(f'[picker_app] {from_screen} back → resuming '
                  f'last_project={langcode!r}',
                  file=sys.stderr, flush=True)
            self._emit_and_quit(project.lift_path, langcode=langcode)
        else:
            print(f'[picker_app] {from_screen} back → no resumable '
                  f'last_project; emitting cancel',
                  file=sys.stderr, flush=True)
            self._emit_cancel_and_quit()

    def _on_back_button(self, _window, key, *_args):
        """Window keyboard hook for Android hardware back (key 27).
        Mirrors the recorder's ``CollabApp._on_back_button`` pattern.
        Returns True to consume the event."""
        if key != 27:
            return False
        return self._navigate_back()

    def on_request_close(self, *args, **kwargs):
        """Desktop window close (the X button). Android back doesn't
        fire this — it goes through Window.on_keyboard, see
        ``_on_back_button``."""
        if self._navigate_back():
            return True
        self._emit_cancel_and_quit()
        return False  # let Kivy stop normally

    def _emit_cancel_and_quit(self):
        """Set RESULT_CANCELED on the picker Activity and finish it.
        Mirrors the cancel branch of ``on_request_close`` so the same
        teardown shape is reachable from anywhere (e.g. a back-press
        from langpicker that doesn't have a ``last_project`` to
        resume). On desktop, just sets the exit code; ``main()`` reads
        ``_exit_code`` after ``App.run`` returns."""
        if platform == 'android':
            try:
                from jnius import autoclass
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                activity.setResult(0)  # RESULT_CANCELED
                activity.finish()
            except Exception as ex:
                print(f'[picker_app] _emit_cancel_and_quit android '
                      f'finish failed: {ex}',
                      file=sys.stderr, flush=True)
            return
        # Desktop: signal cancel and stop the Kivy run loop so main()
        # exits with the cancel code (1).
        self._exit_code = 1
        self.stop()

    # ── Error / loading overlays ──────────────────────────────────────
    def _show_error(self, msg, extra_button=None):
        """Modal overlay inside the picker window. Window stays open
        after dismiss; user can retry or close.

        ``extra_button`` is an optional ``(label, callback)`` tuple. When
        provided, the modal renders that button to the left of Dismiss;
        the callback runs after the modal dismisses."""
        from kivy.metrics import sp
        from kivy.graphics import Color, RoundedRectangle
        height = dp(240) if extra_button else dp(200)
        view = ModalView(size_hint=(0.85, None), height=height,
                         auto_dismiss=False,
                         background_color=theme.OVERLAY_DARK)
        box = BoxLayout(orientation='vertical', padding=dp(16),
                        spacing=dp(12))
        with box.canvas.before:
            Color(*theme.SURFACE)
            box._bg = RoundedRectangle(pos=box.pos, size=box.size,
                                       radius=[dp(8)])
        box.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
                 size=lambda w, s: setattr(w._bg, 'size', s))
        msg_label = Label(
            text=str(msg), halign='center', valign='middle',
            color=theme.TEXT, font_size=sp(15),
            font_name=self._font_name)
        # Bind text_size to width so long messages wrap inside the
        # modal instead of overflowing both edges. Height is left
        # ``None`` so wrap is width-bound only — texture grows
        # vertically as needed; the modal's fixed height clips at
        # the bottom for very long content (acceptable; typical
        # error / auth-prompt messages are 2-3 lines).
        msg_label.bind(width=lambda w, val: setattr(
            w, 'text_size', (val, None)))
        box.add_widget(msg_label)

        def _make_btn(label, fill, on_release):
            btn = Button(
                text=_tr(label), size_hint_y=None, height=dp(48),
                background_color=theme.TRANSPARENT, background_normal='',
                color=(1, 1, 1, 1), font_size=sp(16),
                font_name=self._font_name, bold=True)
            with btn.canvas.before:
                Color(*fill)
                btn._bg = RoundedRectangle(pos=btn.pos, size=btn.size,
                                           radius=[dp(8)])
            btn.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
                     size=lambda w, s: setattr(w._bg, 'size', s))
            btn.bind(on_release=on_release)
            return btn

        if extra_button is not None:
            extra_label, extra_cb = extra_button
            row = BoxLayout(orientation='horizontal',
                            size_hint_y=None, height=dp(48),
                            spacing=dp(12))

            def _on_extra(_btn):
                view.dismiss()
                try:
                    extra_cb()
                except Exception as ex:
                    print(f'[picker_app] extra-button cb raised: {ex}')

            row.add_widget(_make_btn(extra_label, theme.ACCENT, _on_extra))
            row.add_widget(_make_btn(
                'Dismiss', theme.SURFACE, lambda *_: view.dismiss()))
            box.add_widget(row)
        else:
            box.add_widget(_make_btn(
                'Dismiss', theme.ACCENT, lambda *_: view.dismiss()))
        view.add_widget(box)
        view.open()

    def _show_loading_overlay(self, msg):
        """Called by LangPickerScreen before new_from_template kicks
        off. Auto-dismissable=False so a stuck job doesn't accidentally
        close it; dismiss happens in the worker callback."""
        from kivy.metrics import sp
        from kivy.graphics import Color, RoundedRectangle
        self._dismiss_loading_overlay()
        view = ModalView(size_hint=(0.7, None), height=dp(140),
                         auto_dismiss=False,
                         background_color=theme.OVERLAY_DARK)
        box = BoxLayout(orientation='vertical', padding=dp(16))
        with box.canvas.before:
            Color(*theme.SURFACE)
            box._bg = RoundedRectangle(pos=box.pos, size=box.size,
                                       radius=[dp(8)])
        box.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
                 size=lambda w, s: setattr(w._bg, 'size', s))
        loading_label = Label(
            text=str(msg), halign='center', valign='middle',
            color=theme.TEXT, font_size=sp(15),
            font_name=self._font_name)
        # Wrap on width so long messages (e.g. ``Cloning <long-url>...``)
        # don't run off both edges.
        loading_label.bind(width=lambda w, val: setattr(
            w, 'text_size', (val, None)))
        box.add_widget(loading_label)
        view.add_widget(box)
        view.open()
        self._loading_overlay = view

    def _dismiss_loading_overlay(self):
        if self._loading_overlay is not None:
            try:
                self._loading_overlay.dismiss()
            except Exception:
                pass
            self._loading_overlay = None

    # ── ProjectPickerScreen host contract ─────────────────────────────
    def list_projects(self):
        try:
            ps = list_projects() or []
        except Exception as ex:
            print(f'[picker_app] list_projects RPC raised: '
                  f'{type(ex).__name__}: {ex}',
                  file=sys.stderr, flush=True)
            ps = []
        # Filter out projects whose LIFT file the daemon couldn't stat
        # (deleted out-of-band: user wipe, external rm, sync conflict
        # resolution, ...). Showing them in the picker would only let
        # the user tap and crash on a later open with a not-found.
        # If the user wants to re-establish, they can re-clone or
        # re-create with the same vernlang. ``lift_exists`` defaults
        # True so a pre-0.16 daemon (which doesn't emit the flag)
        # behaves as before.
        live = [p for p in ps if p.lift_exists]
        if len(live) != len(ps):
            missing = [p.langcode for p in ps if not p.lift_exists]
            print(f'[picker_app] hiding {len(missing)} project(s) '
                  f'with missing LIFT: {missing!r}',
                  file=sys.stderr, flush=True)
        # Diagnostic: confirm previously-cloned projects survive
        # across picker launches. If this prints 0 right after a
        # successful clone, the registry write didn't persist (bad
        # AZT_HOME, write permission, etc.).
        print(f'[picker_app] list_projects: {len(live)} project(s) '
              f'from registry: '
              f'{[p.langcode for p in live]!r}',
              file=sys.stderr, flush=True)
        return [(p.langcode, p.lift_path or p.working_dir) for p in live]

    # load_lift is defined earlier with a diagnostic print; the
    # second definition is removed so the diagnostic version actually
    # binds (Python's last-def-wins on class bodies would otherwise
    # silently shadow it).

    def go_config(self):
        """Tap the gear: switch to the in-process settings screen.
        The picker remains in the same activity / window; ``SettingsScreen``
        renders its "Back" button (because ``back_to: 'picker'`` is set
        in the _PickerRoot KV) so the user can return."""
        self.sm.current = 'settings'

    def share_apk(self):
        """Settings screen's "Share this app" button. Same shape as
        ``CollabUIApp.share_apk`` — both host the SettingsScreen, so
        the KV's ``app.share_apk()`` resolves on either app. Reuses
        the existing ``_show_error`` modal for failures."""
        from azt_collab_client.ui import share_running_apk
        share_running_apk(filename='azt_collab.apk',
                          on_error=self._show_error)

    # ── Create flow: "I have one on my phone" ─────────────────────────
    def open_file(self):
        """Native file chooser → best-effort register → emit path."""
        try:
            from plyer import filechooser
        except Exception as ex:
            self._show_error(
                _tr('File chooser unavailable: {error}').format(error=ex))
            return

        def _on_chosen(selection):
            if not selection:
                return  # user cancelled chooser; stay on picker
            path = selection[0]

            # No popup for the open-file flow — the auto-derived
            # langcode (LIFT filename stem minus ``.lift``) is what
            # the user picked when they named the file. They can
            # rename later via the clone path or a future
            # rename-project affordance.
            derived_langcode = _tentative_langcode_from_lift(path)

            def _register_and_emit():
                try:
                    register_project(
                        langcode=derived_langcode,
                        working_dir=os.path.dirname(path),
                        lift_path=path,
                    )
                except Exception:
                    # Registration is best-effort; the recorder can
                    # still load the path. Derived langcode still
                    # goes on the result Intent so peers stamp the
                    # right value.
                    pass
                Clock.schedule_once(
                    lambda dt: self._emit_and_quit(
                        path, langcode=derived_langcode), 0)

            import threading
            threading.Thread(target=_register_and_emit,
                             daemon=True).start()

        try:
            filechooser.open_file(
                title='Pick a .lift file',
                filters=[['LIFT', '*.lift']],
                on_selection=_on_chosen,
            )
        except Exception as ex:
            self._show_error(
                _tr('Could not open file chooser: {error}').format(error=ex))

    # ── Create flow: "Clone Internet Repository" ──────────────────────
    def clone_dialog(self):
        """URL prompt → daemon clone → emit. The clone-url popup
        already collects an explicit ``langcode`` (defaulting to the
        URL-derived value, with an inline **change code** affordance
        if the user wants to override). No separate confirmation
        step — the picker just kicks the clone with whatever the
        popup returned."""
        def _on_submit(url, langcode):
            self._start_clone(url, langcode)

        clone_url_popup(_on_submit)

    def _start_clone(self, url, chosen_langcode):
        """Kick the clone with the user-confirmed langcode. The
        daemon's ``_clone_worker`` registers under this exact key;
        ``_emit_and_quit`` later stamps the same value on the
        result Intent's ``langcode`` extra."""
        self._show_loading_overlay(
            _tr('Cloning {url}...').format(url=url))
        import threading

        def _worker():
            print(f'[picker_app] clone worker starting url={url!r} '
                  f'langcode={chosen_langcode!r}',
                  file=sys.stderr, flush=True)
            try:
                import azt_collabd
                repo_name = (url.rstrip('/').split('/')[-1]
                             .replace('.git', ''))
                dest = os.path.join(
                    azt_collabd.paths.azt_home(),
                    'projects', repo_name)
                resp = clone_project(url, dest, langcode=chosen_langcode)
            except Exception as ex:
                err_str = str(ex)
                print(f'[picker_app] clone exception: {err_str}',
                      file=sys.stderr, flush=True)
                Clock.schedule_once(
                    lambda dt: self._after_clone_fail(err_str, None), 0)
                return
            print(f'[picker_app] clone returned: '
                  f'ok={resp.get("ok")!r} '
                  f'lift_path={resp.get("lift_path")!r} '
                  f'error={resp.get("error")!r}',
                  file=sys.stderr, flush=True)
            if resp.get('ok') and resp.get('lift_path'):
                Clock.schedule_once(
                    lambda dt: self._after_clone_ok(
                        resp['lift_path'],
                        resp.get('langcode', chosen_langcode)), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._after_clone_fail(
                        resp.get('error', 'unknown'),
                        resp.get('result')), 0)

        threading.Thread(target=_worker, daemon=True).start()

    def _after_clone_ok(self, lift_path, langcode=''):
        print(f'[picker_app] _after_clone_ok lift_path={lift_path!r} '
              f'langcode={langcode!r}',
              file=sys.stderr, flush=True)
        self._dismiss_loading_overlay()
        self._emit_and_quit(lift_path, langcode=langcode)

    def _after_clone_fail(self, err, result):
        print(f'[picker_app] _after_clone_fail err={err!r} '
              f'result_codes={getattr(result, "codes", lambda: None)()!r}',
              file=sys.stderr, flush=True)
        self._dismiss_loading_overlay()
        # If the daemon flagged the failure as auth-shaped (private repo
        # / 401 / 403 / 404), show the auth-prompt modal with a button
        # straight to settings. Users typically pick a project without
        # visiting settings first, so we lead them there.
        auth_status = None
        if result is not None:
            for st in getattr(result, 'statuses', []):
                if st.code == S.CLONE_AUTH_REQUIRED:
                    auth_status = st
                    break
        # Fallback: when the daemon's worker didn't run far enough to
        # attach CLONE_AUTH_REQUIRED (result is None) but the error
        # string itself smells like auth/not-found, still surface the
        # auth modal — the user's likely problem is the same.
        if auth_status is None and result is None:
            msg = (err or '').lower()
            if any(k in msg for k in (
                    '401', '403', '404',
                    'unauthorized', 'forbidden', 'not found',
                    'authentication', 'credential')):
                auth_status = S.Status(S.CLONE_AUTH_REQUIRED, {'host': ''})
        if auth_status is not None:
            msg = translate_status(auth_status)
            self._show_error(
                msg,
                extra_button=(_tr('Open settings'),
                              lambda: self.go_config()))
            return
        self._show_error(_tr('Clone failed: {error}').format(error=err))

    # ── Create flow: "Start New" ──────────────────────────────────────
    def show_start_over(self):
        """Navigate to LangPickerScreen; on Continue it sets
        ``self._pending_vernlang`` and calls ``new_from_template``."""
        self._pending_vernlang = ''
        self.sm.transition = SlideTransition(direction='left')
        self.sm.current = 'langpicker'

    def new_from_template(self):
        """Driven by LangPickerScreen._on_continue. Reads
        ``_pending_vernlang``, asks the daemon to download the SILCAWL
        template, emits the resulting LIFT path."""
        vernlang = getattr(self, '_pending_vernlang', '')
        if not vernlang:
            self._dismiss_loading_overlay()
            return  # shouldn't happen; LangPickerScreen sets it first

        import azt_collabd
        dest_dir = os.path.join(
            azt_collabd.paths.azt_home(), 'projects', vernlang)

        def _worker():
            err = ''
            project = None
            try:
                ret = create_project_from_template(
                    vernlang=vernlang, dest_dir=dest_dir)
            except Exception as ex:
                err = f'exception: {ex}'
            else:
                if isinstance(ret, tuple):
                    project, err = ret
                else:
                    project = ret
            if project and project.lift_path:
                Clock.schedule_once(
                    lambda dt: self._after_template_ok(
                        project.lift_path), 0)
                return
            Clock.schedule_once(
                lambda dt: self._after_template_fail(
                    err or 'unknown'), 0)

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _after_template_ok(self, lift_path):
        self._dismiss_loading_overlay()
        # Pass the vernlang so the host can run set_vernlang +
        # clean_template on load (template files come down with
        # extra non-vernlang forms that need pruning).
        self._emit_and_quit(lift_path,
                            langcode=getattr(self, '_pending_vernlang', ''))

    def _after_template_fail(self, err):
        self._dismiss_loading_overlay()
        self._show_error(
            _tr('Could not create project: {error}').format(error=err))


def main():
    PickerApp().run()
    if platform != 'android':
        sys.exit(PickerApp._exit_code)
    # On Android we never reach here while the Activity is alive
    # because _emit_and_quit early-returns instead of calling
    # self.stop(). If we do reach here (e.g. an unexpected stop()
    # call elsewhere), DO NOT sys.exit — the
    # AZTServiceProviderhost service is still pinning the process
    # for in-flight URI grants. Just return; the JVM stays up.


if __name__ == '__main__':
    main()
