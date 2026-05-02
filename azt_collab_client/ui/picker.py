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
from kivy.lang import Builder
from kivy.uix.screenmanager import Screen


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
                    source: 'icons/gear.png'
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
                size: dp(200), dp(200)
                pos_hint: {{'center_x': 0.5}}
            Label:
                text: app.title
                font_size: sp(28)
                font_name: FONT
                bold: True
                color: T.ACCENT
                size_hint_y: None
                height: dp(40)
            Label:
                text: app.subtitle
                font_size: sp(16)
                font_name: FONT
                color: T.TEXT_DIM
                size_hint_y: None
                height: dp(24)
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
                font_size: sp(11)
                font_name: FONT
                color: T.TEXT_FAINT
                size_hint_y: None
                height: dp(20)
                halign: 'center'
                text_size: self.size
'''


def register_kv(font_name='Roboto', hide_settings_gear=False):
    """Load the picker KV with the host's font. Call after the host's
    main KV is loaded so the ``RecBtn`` rule is already in scope.

    Set ``hide_settings_gear=True`` for hosts that have no settings
    screen of their own (the standalone picker subprocess uses this)."""
    Builder.load_string(_KV_TEMPLATE.format(
        font_name=font_name,
        show_gear='True' if not hide_settings_gear else 'False',
    ))


class ProjectPickerScreen(Screen):
    """Existing-project list + 'open / clone / new' buttons. Defers to
    host App methods (see module docstring for the contract)."""

    def on_enter(self):
        self._populate_projects()

    def _populate_projects(self):
        box = self.ids.get('project_list')
        if not box:
            return
        box.clear_widgets()
        app = App.get_running_app()
        if not hasattr(app, 'list_projects'):
            return
        projects = app.list_projects() or []
        if not projects:
            return
        for name, path in projects:
            btn = Builder.load_string(
                'RecBtn:\n'
                f'    text: {name!r}\n'
                '    normal_color: T.GREEN\n'
            )
            btn.lift_path = path
            btn.bind(on_release=lambda b: app.load_lift(b.lift_path))
            box.add_widget(btn)
