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
from kivy.uix.screenmanager import ScreenManager, SlideTransition
from kivy.utils import platform

import azt_collab_client
from azt_collab_client import (
    clone_project, create_project_from_template, list_projects,
    register_project,
)
from azt_collab_client.ui import (
    LangPickerScreen, ProjectPickerScreen,
    clone_url_popup, register_langpicker_kv, register_picker_kv, theme,
)


_AZT_ICON = os.path.join(
    os.path.dirname(azt_collab_client.__file__), 'azt.png')


_KV = '''
#:import dp kivy.metrics.dp

<RecBtn@Button>:
    normal_color: 0.2, 0.6, 1, 1
    size_hint_y: None
    height: dp(52)
    background_color: 0, 0, 0, 0
    background_normal: ''
    canvas.before:
        Color:
            rgba: self.normal_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: 1, 1, 1, 1
    font_size: 16
    bold: True

<_PickerRoot>:
    ProjectPickerScreen:
        name: 'picker'
    LangPickerScreen:
        name: 'langpicker'
'''


class _PickerRoot(ScreenManager):
    pass


class PickerApp(App):
    """Standalone picker app. Implements every host-contract callback
    the shared screens require; every successful flow ends in
    ``_emit_and_quit(path)``."""

    title = 'A-Z+T — Pick a project'
    subtitle = StringProperty('Pick a project')
    icon = StringProperty(_AZT_ICON)
    version_string = StringProperty(f'collab {azt_collab_client.__version__}')

    # Process exit code; flipped to 0 on a successful emit. main()
    # reads this after App.run() returns.
    _exit_code = 1

    # Loading overlay (LangPickerScreen calls _show_loading_overlay
    # before new_from_template; we dismiss in the worker callback).
    _loading_overlay = None

    # Set by LangPickerScreen on Continue.
    _pending_vernlang = ''

    # ── Lifecycle ─────────────────────────────────────────────────────
    def build(self):
        theme.set_theme('Ocean')
        Builder.load_string(_KV)
        register_picker_kv(font_name='Roboto', hide_settings_gear=True)
        register_langpicker_kv(font_name='Roboto')
        self.sm = _PickerRoot(transition=SlideTransition())
        return self.sm

    # ── Result emission ───────────────────────────────────────────────
    def _emit_and_quit(self, path, langcode=''):
        """Write the chosen path (and optionally a langcode for
        from-template flows) and stop the app. On Android sets the
        Activity result instead of writing stdout.

        Protocol: ``AZT_PICK\\t<path>\\t<langcode>\\n``. langcode is
        empty for existing / clone / open flows; populated only when
        the project came from a fresh template download (the recorder
        uses it to drive set_vernlang + clean_template on load)."""
        if platform == 'android':
            try:
                from jnius import autoclass
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                Intent = autoclass('android.content.Intent')
                activity = PythonActivity.mActivity
                data = Intent()
                data.putExtra('path', str(path))
                if langcode:
                    data.putExtra('langcode', str(langcode))
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
        self.stop()

    def on_request_close(self, *args, **kwargs):
        """Window-close (the X button). Treat as cancel; default exit
        code stays 1 unless a flow already flipped it."""
        if platform == 'android':
            try:
                from jnius import autoclass
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                activity.setResult(0)  # RESULT_CANCELED
                activity.finish()
            except Exception:
                pass
        return False  # let Kivy stop normally

    # ── Error / loading overlays ──────────────────────────────────────
    def _show_error(self, msg):
        """Modal overlay inside the picker window. Window stays open
        after dismiss; user can retry or close."""
        view = ModalView(size_hint=(0.85, None), height=dp(180),
                         auto_dismiss=False)
        box = BoxLayout(orientation='vertical', padding=dp(12),
                        spacing=dp(10))
        box.add_widget(Label(text=str(msg), halign='center',
                             valign='middle'))
        btn = Button(text='Dismiss', size_hint_y=None, height=dp(44))
        btn.bind(on_release=view.dismiss)
        box.add_widget(btn)
        view.add_widget(box)
        view.open()

    def _show_loading_overlay(self, msg):
        """Called by LangPickerScreen before new_from_template kicks
        off. Auto-dismissable=False so a stuck job doesn't accidentally
        close it; dismiss happens in the worker callback."""
        self._dismiss_loading_overlay()
        view = ModalView(size_hint=(0.7, None), height=dp(120),
                         auto_dismiss=False)
        view.add_widget(Label(text=str(msg), halign='center',
                              valign='middle'))
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
        except Exception:
            ps = []
        return [(p.langcode, p.lift_path or p.working_dir) for p in ps]

    def load_lift(self, path):
        """Existing-project tap: emit and exit."""
        self._emit_and_quit(path)

    def go_config(self):
        # No settings gear in this app; the gear is hidden via
        # register_picker_kv(hide_settings_gear=True). This stub
        # exists in case the contract is invoked anyway.
        pass

    # ── Create flow: "I have one on my phone" ─────────────────────────
    def open_file(self):
        """Native file chooser → best-effort register → emit path."""
        try:
            from plyer import filechooser
        except Exception as ex:
            self._show_error(f'File chooser unavailable: {ex}')
            return

        def _on_chosen(selection):
            if not selection:
                return  # user cancelled chooser; stay on picker
            path = selection[0]

            def _register_and_emit():
                try:
                    # working_dir = parent of the .lift; langcode is
                    # left blank so the daemon derives it.
                    register_project(
                        langcode='',
                        working_dir=os.path.dirname(path),
                        lift_path=path,
                    )
                except Exception:
                    # Registration is best-effort; the recorder can
                    # still load the path.
                    pass
                Clock.schedule_once(
                    lambda dt: self._emit_and_quit(path), 0)

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
            self._show_error(f'Could not open file chooser: {ex}')

    # ── Create flow: "Clone Internet Repository" ──────────────────────
    def clone_dialog(self):
        """URL prompt → daemon clone → emit cloned path."""
        def _on_submit(url):
            self._show_loading_overlay(f'Cloning {url}...')
            import threading

            def _worker():
                try:
                    import azt_collabd
                    repo_name = (url.rstrip('/').split('/')[-1]
                                 .replace('.git', ''))
                    dest = os.path.join(
                        azt_collabd.paths.azt_home(),
                        'projects', repo_name)
                    resp = clone_project(url, dest)
                except Exception as ex:
                    Clock.schedule_once(
                        lambda dt: self._after_clone_fail(str(ex)), 0)
                    return
                if resp.get('ok') and resp.get('lift_path'):
                    Clock.schedule_once(
                        lambda dt: self._after_clone_ok(
                            resp['lift_path']), 0)
                else:
                    Clock.schedule_once(
                        lambda dt: self._after_clone_fail(
                            resp.get('error', 'unknown')), 0)
            threading.Thread(target=_worker, daemon=True).start()

        clone_url_popup(_on_submit)

    def _after_clone_ok(self, lift_path):
        self._dismiss_loading_overlay()
        self._emit_and_quit(lift_path)

    def _after_clone_fail(self, err):
        self._dismiss_loading_overlay()
        self._show_error(f'Clone failed: {err}')

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
        self._show_error(f'Could not create project: {err}')


def main():
    PickerApp().run()
    sys.exit(PickerApp._exit_code)


if __name__ == '__main__':
    main()
