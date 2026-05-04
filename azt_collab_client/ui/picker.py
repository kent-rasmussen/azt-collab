"""ProjectPickerScreen — pick existing or create a new project.

Step 5 of azt_collab_picker_migration.xml. Replaces the recorder's
WelcomeScreen so sister apps reuse the same entry surface.

Host contract (the running App must implement):
    app.icon (StringProperty)         path to app icon image
    app.title, app.subtitle           heading / sub-heading strings
    app.version_string                "version X.Y.Z" line
    app.open_file()                   native file picker → load
    app.clone_dialog()                URL-prompt clone (use clone_url_popup)
    app.show_start_over()             confirm-and-create-from-template
    app.go_config()                   open settings gear
    app.list_projects()               -> [(display_name, path), ...]
    app.load_lift(path)               open a project's LIFT path

After the host's main KV is loaded, call ``register_kv(font_name)``
(also exposed as ``register_picker_kv``) and add ``ProjectPickerScreen``
to your ScreenManager:

    ScreenManager:
        ProjectPickerScreen:
            name: 'picker'
        ... your other screens ...
"""

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.uix.screenmanager import Screen

from .icons import icon_path


_KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T azt_collab_client.ui.theme
#:import _ azt_collab_client.translate.tr
#:set FONT '{font_name}'

<ProjectPickerScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        BoxLayout:
            size_hint_y: None
            height: dp(44) if {show_gear} else 0
            padding: 0, dp(4), dp(8), 0
            opacity: 1 if {show_gear} else 0
            disabled: not {show_gear}
            Widget:
            Button:
                size_hint: None, None
                size: (dp(44), dp(44)) if {show_gear} else (0, 0)
                background_color: T.TRANSPARENT
                background_normal: ''
                on_release: app.go_config()
                Image:
                    source: '{gear_icon}' if {show_gear} else ''
                    size: (dp(28), dp(28)) if {show_gear} else (0, 0)
                    size_hint: None, None
                    center: self.parent.center
                    allow_stretch: True
                    keep_ratio: True
        BoxLayout:
            orientation: 'vertical'
            padding: dp(40), 0, dp(40), dp(20)
            spacing: dp(12)
            Image:
                source: app.icon
                size_hint: None, None
                size: dp(240), dp(240)
                pos_hint: {{'center_x': 0.5}}
                allow_stretch: True
                keep_ratio: True
            Label:
                text: app.title
                font_size: sp(32)
                font_name: FONT
                bold: True
                color: T.ACCENT
                size_hint_y: None
                height: dp(44)
                halign: 'center'
                text_size: self.size
            Label:
                text: app.subtitle
                font_size: sp(18)
                font_name: FONT
                color: T.TEXT_DIM
                size_hint_y: None
                height: dp(28)
                halign: 'center'
                text_size: self.size
            Widget:
                size_hint_y: None
                height: dp(8)
            ScrollView:
                size_hint_y: 1
                do_scroll_x: False
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(20)
                    RecBtn:
                        text: _('I have one on my phone')
                        normal_color: T.ACCENT
                        on_release: app.open_file()
                    RecBtn:
                        text: _('Clone Internet Repository')
                        normal_color: T.BTN_INACTIVE
                        on_release: app.clone_dialog()
                    RecBtn:
                        text: _('Start New')
                        normal_color: T.BTN_INACTIVE
                        on_release: app.show_start_over()
                    BoxLayout:
                        id: project_list
                        orientation: 'vertical'
                        size_hint_y: None
                        height: self.minimum_height
                        spacing: dp(6)
            Label:
                text: app.version_string
                font_size: sp(13)
                font_name: FONT
                color: T.TEXT_DIM
                size_hint_y: None
                height: dp(22)
                halign: 'center'
                text_size: self.size
'''


def register_kv(font_name='Roboto', hide_settings_gear=False,
                gear_icon=None):
    """Load the picker KV with the host's font. Call after the host's
    main KV is loaded so the ``RecBtn`` rule is already in scope.

    Set ``hide_settings_gear=True`` for hosts that have no settings
    screen of their own.

    ``gear_icon`` is an absolute path to a PNG; defaults to the
    package-bundled ``ui/assets/gear.png``. Hosts that want a custom
    icon (the recorder ships its own at ``azt_recorder/icons/gear.png``)
    pass it explicitly — relative paths break in the standalone picker
    subprocess where cwd isn't the host's repo root."""
    Builder.load_string(_KV_TEMPLATE.format(
        font_name=font_name,
        show_gear='True' if not hide_settings_gear else 'False',
        gear_icon=(gear_icon or icon_path('gear')),
    ))


class ProjectPickerScreen(Screen):
    """Existing-project list + 'open / clone / new' buttons. Defers to
    host App methods (see module docstring for the contract)."""

    def on_enter(self):
        # Defer one frame: Kivy >= 2.3 fires on_enter before KV ids
        # have attached on the first screen entry, so a synchronous
        # ``self.ids.get('project_list')`` returns None and the
        # populate path bails silently — the symptom user-visible
        # was "previously cloned projects don't appear in the
        # existing-projects list". Same fix the settings UI uses.
        Clock.schedule_once(lambda *_: self._populate_projects(), 0)

    def _populate_projects(self):
        box = self.ids.get('project_list')
        if not box:
            print('[picker] _populate_projects: project_list id '
                  'still not attached after defer; bailing',
                  flush=True)
            return
        box.clear_widgets()
        app = App.get_running_app()
        if not hasattr(app, 'list_projects'):
            print('[picker] _populate_projects: app missing '
                  'list_projects host method; bailing',
                  flush=True)
            return
        projects = app.list_projects() or []
        print(f'[picker] _populate_projects: rendering '
              f'{len(projects)} button(s)', flush=True)
        if not projects:
            return
        for name, path in projects:
            btn = Builder.load_string(
                'RecBtn:\n'
                f'    text: {name!r}\n'
                '    normal_color: T.GREEN\n'
            )
            btn.lift_path = path
            # ``name`` from ``app.list_projects()`` is the canonical
            # langcode (the projects.json key — see the host
            # contract in this module's docstring). Stash it on the
            # button so the host's ``load_lift`` can stamp the
            # result Intent's ``langcode`` extra without having to
            # query the daemon a second time.
            btn.langcode = name
            btn.bind(on_release=lambda b: app.load_lift(
                b.lift_path, getattr(b, 'langcode', '')))
            box.add_widget(btn)
