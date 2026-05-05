"""
Standalone Kivy app for the A-Z+T collab server.

Three screens:
    settings  — credentials status + host toggle + Connect/Disconnect.
    github    — GitHub device flow: shows user_code, opens browser,
                polls until authorized, saves tokens through the
                client (server owns the credentials store).
    gitlab    — GitLab username + PAT form.
    projects  — read-only list of registered projects with last-sync.

Launched with::

    python -m azt_collabd ui

The UI process talks to the daemon through azt_collab_client; the
daemon is auto-spawned on first call.
"""

import datetime
import os
import threading
import webbrowser

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import StringProperty
from kivy.uix.button import Button
from kivy.uix.screenmanager import (
    NoTransition, Screen, ScreenManager, SlideTransition,
)

import azt_collab_client
from azt_collab_client import i18n as _client_i18n
from azt_collab_client.ui import register_charis, theme

import azt_collabd
from azt_collabd.status import AuthError
from azt_collab_client import (
    S,
    get_contributor,
    get_credentials_status,
    init_project,
    is_online,
    last_project,
    mark_github_app_installed,
    open_project,
    project_status,
    save_github_tokens,
    save_gitlab_credentials,
    test_gitlab_credentials,
    set_contributor,
    translate_result,
    translate_status,
)


_tr = _client_i18n._


_AZT_ICON = os.path.join(
    os.path.dirname(azt_collab_client.__file__), 'azt.png')


KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T azt_collab_client.ui.theme
#:import _ azt_collab_client.translate.tr
#:set FONT '{font_name}'

<RootSM>:
    SettingsScreen:
        name: 'settings'
    GitHubConnectScreen:
        name: 'github'
    GitLabFormScreen:
        name: 'gitlab'

<RecBtn@Button>:
    normal_color: T.ACCENT
    size_hint_y: None
    height: dp(52)
    background_color: T.TRANSPARENT
    background_normal: ''
    canvas.before:
        Color:
            rgba: self.normal_color or T.ACCENT
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: 1, 1, 1, 1
    font_size: sp(16)
    font_name: FONT
    bold: True

<NavBtn@Button>:
    size_hint_y: None
    height: dp(48)
    background_color: T.TRANSPARENT
    background_normal: ''
    canvas.before:
        Color:
            rgba: T.SURFACE
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: T.ACCENT
    font_size: sp(15)
    font_name: FONT
    bold: True

<IconBtn@Button>:
    background_color: T.TRANSPARENT
    background_normal: ''
    color: T.TEXT_DIM
    font_size: sp(20)
    font_name: FONT

<SectionLabel@Label>:
    size_hint_y: None
    height: dp(32)
    font_size: sp(13)
    font_name: FONT
    bold: True
    color: T.ACCENT
    halign: 'left'
    valign: 'middle'
    text_size: self.size

<HeaderLabel@Label>:
    font_name: FONT
    bold: True
    color: T.ACCENT
    font_size: sp(17)
    size_hint_y: None
    height: dp(40)
    halign: 'left'
    valign: 'middle'
    text_size: self.size

<BodyLabel@Label>:
    font_name: FONT
    color: T.TEXT
    font_size: sp(14)
    # Width-bound, height-free wrap. Several BodyLabel instances size
    # themselves with ``height: self.texture_size[1] + dp(8)``, which
    # forms a do_layout / texture_update feedback loop with the
    # previous ``text_size: self.size``. ``(self.width, None)`` keeps
    # wrapping at the widget width but lets texture_size[1] flow
    # from content alone — no cycle.
    text_size: self.width, None
    halign: 'left'
    valign: 'top'

<DimLabel@Label>:
    font_name: FONT
    color: T.TEXT_DIM
    font_size: sp(13)
    text_size: self.size
    halign: 'left'
    valign: 'middle'

<ThemedInput@TextInput>:
    multiline: False
    size_hint_y: None
    height: dp(48)
    font_size: sp(15)
    font_name: FONT
    background_color: T.SURFACE
    foreground_color: T.TEXT
    cursor_color: T.ACCENT
    hint_text_color: T.HINT

<TopBar@BoxLayout>:
    title: ''
    size_hint_y: None
    height: dp(52)
    padding: dp(8), dp(6)
    canvas.before:
        Color:
            rgba: T.SURFACE
        Rectangle:
            pos: self.pos
            size: self.size
    Label:
        text: root.title
        font_name: FONT
        bold: True
        color: T.ACCENT
        font_size: sp(17)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        padding_x: dp(8)

<SettingsScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        TopBar:
            title: _('AZT Collaboration — Settings')
        ScrollView:
            do_scroll_x: False
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                # Back button — only present when this SettingsScreen
                # is reached from another screen (e.g. the picker's
                # gear sets ``back_to: 'picker'`` in the picker_app
                # _PickerRoot KV). Hidden / disabled in the standalone
                # settings host where back has no meaning.
                NavBtn:
                    # ``«`` (U+00AB) instead of ``←`` (U+2190) because
                    # the latter isn't in CharisSIL's glyph table —
                    # would render as tofu under the linguistic font.
                    # Guillemet is in every Latin font, reads as a
                    # back-pointer, and Title-cases nicely in French.
                    text: '«  ' + _('Back')
                    size_hint_y: None
                    height: dp(48) if root.back_to else 0
                    opacity: 1 if root.back_to else 0
                    disabled: not root.back_to
                    on_release: app.go(root.back_to) if root.back_to else None
                SectionLabel:
                    text: _('Interface language')
                BoxLayout:
                    id: lang_selector_row
                    size_hint_y: None
                    height: dp(40)
                    spacing: dp(8)
                SectionLabel:
                    text: _('Your name (appears in commits)')
                ThemedInput:
                    id: contributor_input
                    hint_text: _('e.g. Kent Rasmussen')
                    on_focus: root.save_contributor() if not self.focus else None
                BodyLabel:
                    id: contributor_msg
                    text: ''
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: dp(20)
                # GitHub: Connect + Disconnect on one row, no header.
                # Active colour swaps with connection state (Connect
                # is green when not connected, Disconnect is green
                # when connected) — the visually-prominent button
                # matches the user's likely next action. Set in
                # ``refresh()`` rather than KV so the same control
                # path that fetches credentials_status drives the
                # colour.
                BoxLayout:
                    size_hint_y: None
                    height: dp(52)
                    spacing: dp(10)
                    RecBtn:
                        id: gh_connect_btn
                        text: _('Connect to GitHub')
                        normal_color: T.GREEN
                        on_release: root.connect_github()
                    RecBtn:
                        id: gh_disconnect_btn
                        text: _('Disconnect GitHub')
                        normal_color: T.BTN_INACTIVE
                        on_release: root.disconnect_github()
                # GitLab: same shape.
                BoxLayout:
                    size_hint_y: None
                    height: dp(52)
                    spacing: dp(10)
                    RecBtn:
                        id: gl_connect_btn
                        text: _('Connect to GitLab')
                        normal_color: T.GREEN
                        on_release: app.go('gitlab')
                    RecBtn:
                        id: gl_disconnect_btn
                        text: _('Disconnect GitLab')
                        normal_color: T.BTN_INACTIVE
                        on_release: root.disconnect_gitlab()
                # Publish — visible only for the most-recent project
                # when it has no remote yet AND at least one host's
                # credentials have been confirmed. ``refresh()`` flips
                # the height/opacity to hide the entire row when not
                # applicable (the Kivy hide/show pattern from
                # ~/.claude-sil/CLAUDE.md).
                BoxLayout:
                    id: publish_row
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(8)
                    RecBtn:
                        id: publish_btn
                        text: _('Publish data')
                        normal_color: T.GREEN
                        on_release: root.publish()
                    BodyLabel:
                        id: publish_msg
                        text: ''
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(20)
                # Status — read-only credential / online state, parked
                # at the bottom now that the actionable rows live up
                # top. Refresh Status sits directly under it so the
                # button affordance for "I changed something elsewhere,
                # pull it again" is right next to the data it updates.
                SectionLabel:
                    text: _('Status')
                BodyLabel:
                    id: status_label
                    text: _('Loading...')
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                NavBtn:
                    text: _('Refresh Status')
                    on_release: root.refresh()
                Widget:
                    size_hint_y: None
                    height: dp(8)
                Label:
                    text: app.version_string
                    font_name: FONT
                    color: T.TEXT_DIM
                    font_size: sp(13)
                    size_hint_y: None
                    height: dp(22)
                    halign: 'center'
                    text_size: self.size

<GitHubConnectScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        TopBar:
            title: _('Connect to GitHub')
        ScrollView:
            do_scroll_x: False
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                BodyLabel:
                    id: gh_message
                    text: _('Tap "Begin" to start the device-flow login.')
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                    text_size: self.width, None
                SectionLabel:
                    text: _('Your one-time code')
                BoxLayout:
                    size_hint_y: None
                    height: dp(72)
                    spacing: dp(10)
                    Label:
                        id: gh_user_code
                        text: ''
                        font_name: FONT
                        bold: True
                        color: T.ACCENT
                        font_size: sp(28)
                        halign: 'center'
                        valign: 'middle'
                        text_size: self.size
                    NavBtn:
                        size_hint_x: None
                        width: dp(96)
                        text: _('Copy')
                        on_release: root.copy_code()
                Widget:
                    size_hint_y: None
                    height: dp(8)
                RecBtn:
                    id: gh_begin_btn
                    text: _('Begin')
                    normal_color: T.GREEN
                    on_release: root.begin()
                NavBtn:
                    text: _('Back')
                    on_release: app.go('settings')

<GitLabFormScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        TopBar:
            title: _('Connect to GitLab')
        ScrollView:
            do_scroll_x: False
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                BodyLabel:
                    text: _('Enter your GitLab username and a personal access token (read/write to repos).')
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                    text_size: self.width, None
                DimLabel:
                    text: _('Username')
                    size_hint_y: None
                    height: dp(24)
                ThemedInput:
                    id: gl_user
                DimLabel:
                    text: _('Personal access token')
                    size_hint_y: None
                    height: dp(24)
                ThemedInput:
                    id: gl_token
                    password: True
                BodyLabel:
                    id: gl_msg
                    text: ''
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: dp(40)
                Widget:
                    size_hint_y: None
                    height: dp(8)
                RecBtn:
                    id: gl_test_btn
                    text: _('Test connection')
                    normal_color: T.GREEN
                    on_release: root.test()
                NavBtn:
                    text: _('Back')
                    on_release: app.go('settings')
'''


_kv_loaded = False


def register_kv(font_name='Roboto'):
    """Load the settings/connect/form screen KV with the host's font.

    Idempotent: safe to call from both ``CollabUIApp.build()`` (the
    standalone settings subprocess) and ``picker_app.PickerApp.build()``
    (the in-process settings hosted inside the picker, reachable via
    the gear). Subsequent calls are no-ops so a second host doesn't
    redefine class rules.

    Hosts that want the settings screens in their own ``ScreenManager``
    can add the screen instances after calling this — class rules
    (``<SettingsScreen>``, ``<GitHubConnectScreen>``,
    ``<GitLabFormScreen>``, plus the shared ``<RecBtn>``, ``<NavBtn>``,
    ``<SectionLabel>``, ``<TopBar>`` etc.) will already be in scope."""
    global _kv_loaded
    if _kv_loaded:
        return
    Builder.load_string(KV_TEMPLATE.format(font_name=font_name))
    _kv_loaded = True


class RootSM(ScreenManager):
    pass


# ── Settings ────────────────────────────────────────────────────────────────

class SettingsScreen(Screen):
    # Set by the host KV when this screen is reachable from somewhere
    # else (e.g. ``back_to: 'picker'`` in ``picker_app._PickerRoot``).
    # Empty string → no back button (standalone settings host).
    back_to = StringProperty('')

    def on_enter(self):
        # Defer to next frame: on_enter can fire before the KV rule's
        # nested BoxLayout children have all been added to ``self.ids``
        # on Kivy >= 2.3, which raises a confusing
        # "'super' object has no attribute '__getattr__'" from
        # ObservableDict when a key is missing.
        def _ready(*_):
            self._build_lang_selector()
            self.refresh()
        Clock.schedule_once(_ready, 0)

    def _build_lang_selector(self):
        """Populate the language selector row with one button per
        catalog discovered under ``azt_collab_client/locales/`` plus
        English. Selected language is highlighted; tapping a different
        button calls ``_set_ui_language`` which rebuilds every screen
        in the manager so translations take effect live."""
        row = self.ids.get('lang_selector_row')
        if row is None:
            return
        row.clear_widgets()
        cur = _client_i18n.current_language()
        for code, name in _client_i18n.available_languages():
            btn = Button(
                text=name,
                font_size=sp(14),
                size_hint_x=1,
                background_color=(
                    theme.ACCENT if code == cur else theme.SURFACE),
                background_normal='',
                color=theme.TEXT,
            )
            btn.bind(on_release=lambda b, c=code: self._set_ui_language(c))
            row.add_widget(btn)

    def _set_ui_language(self, lang_code):
        if lang_code == _client_i18n.current_language():
            return
        _client_i18n.set_language(lang_code)
        # Rebuild all screens so KV ``text: _('...')`` bindings
        # re-evaluate against the new catalog. Same dance the
        # recorder's ConfigScreen uses. Capture host-set properties
        # (currently just ``back_to``) before clearing — they live on
        # the *instance*, not the class, so recreating from the class
        # alone loses them and (e.g.) the picker host's "← Back"
        # button vanishes after a language toggle.
        app = App.get_running_app()
        sm = app.sm
        old_transition = sm.transition
        sm.transition = NoTransition()
        screens_info = [
            {'name': s.name, 'cls': type(s),
             'back_to': getattr(s, 'back_to', '')}
            for s in list(sm.screens)
        ]
        sm.clear_widgets()
        for info in screens_info:
            screen = info['cls'](name=info['name'])
            if info['back_to']:
                screen.back_to = info['back_to']
            sm.add_widget(screen)
        sm.current = 'settings'
        Clock.schedule_once(
            lambda dt: setattr(sm, 'transition', old_transition), 0.1)

    def refresh(self):
        try:
            status = get_credentials_status()
            online = is_online()
        except Exception as ex:
            label = self.ids.get('status_label')
            if label is not None:
                label.text = _tr('Error: {error}').format(error=ex)
            return
        gh = status.get('github', {})
        gl = status.get('gitlab', {})
        # Highlight the action the user is most likely to want:
        # Connect when not connected, Disconnect when connected.
        # The other button stays clickable (disconnected ↔ reconnect
        # for token refresh remains a valid flow), just dimmed.
        gh_connected = bool(gh.get('connected'))
        gl_connected = bool(gl.get('connected'))
        for btn_id, fill in (
            ('gh_connect_btn',
             theme.BTN_INACTIVE if gh_connected else theme.GREEN),
            ('gh_disconnect_btn',
             theme.GREEN if gh_connected else theme.BTN_INACTIVE),
            ('gl_connect_btn',
             theme.BTN_INACTIVE if gl_connected else theme.GREEN),
            ('gl_disconnect_btn',
             theme.GREEN if gl_connected else theme.BTN_INACTIVE),
        ):
            btn = self.ids.get(btn_id)
            if btn is not None:
                btn.normal_color = fill
        # Contributor field — only repopulate when the user isn't
        # actively editing it, so a refresh during typing doesn't
        # clobber in-progress input.
        contrib_input = self.ids.get('contributor_input')
        if contrib_input is not None and not contrib_input.focus:
            contrib_input.text = status.get('contributor', '') or ''
        yes = _tr('yes')
        no = _tr('no')
        lines = [
            f"{_tr('Online:')}   {yes if online else no}",
            "",
            "GitHub",
            f"  {_tr('Connected:')}     {yes if gh.get('connected') else no}",
            f"  {_tr('Username:')}      {gh.get('username', '') or '-'}",
            f"  {_tr('App installed:')} "
            f"{yes if gh.get('app_installed') else no}",
            f"  {_tr('Confirmed:')}     {yes if gh.get('confirmed') else no}",
            "",
            "GitLab",
            f"  {_tr('Connected:')} {yes if gl.get('connected') else no}",
            f"  {_tr('Username:')}  {gl.get('username', '') or '-'}",
            f"  {_tr('Confirmed:')} {yes if gl.get('confirmed') else no}",
        ]
        self.ids.status_label.text = '\n'.join(lines)
        self._refresh_publish_row(status)

    def _refresh_publish_row(self, status):
        """Show / hide / enable the "Publish <langcode> data" row.

        Visible only when (a) ``last_project()`` resolves to a langcode
        and (b) that project does not already have a remote URL. When
        visible, the button is enabled iff at least one host's
        credentials are confirmed (GitHub: connected + app installed;
        GitLab: a successful Test). The actual host pick happens at
        click time — single-confirmed hosts publish directly, both
        confirmed pops the host-chooser overlay."""
        row = self.ids.get('publish_row')
        btn = self.ids.get('publish_btn')
        msg = self.ids.get('publish_msg')
        if row is None or btn is None:
            return
        # Reset to hidden by default; the rest of this function flips
        # back on if every condition holds.
        row.height = 0
        row.opacity = 0
        if msg is not None:
            msg.text = ''
        langcode = ''
        try:
            langcode = (last_project() or '').strip()
        except Exception:
            return
        if not langcode:
            return
        try:
            ps = project_status(langcode)
        except Exception:
            ps = None
        if ps is None or (ps.remote_url or '').strip():
            # Either the project's gone (deleted out-of-band) or it
            # already has a remote — nothing to publish.
            return
        gh_confirmed = bool(status.get('github', {}).get('confirmed'))
        gl_confirmed = bool(status.get('gitlab', {}).get('confirmed'))
        n_confirmed = int(gh_confirmed) + int(gl_confirmed)
        btn.text = _tr('Publish {langcode} data').format(langcode=langcode)
        btn.disabled = (n_confirmed == 0)
        btn.normal_color = (
            theme.GREEN if n_confirmed else theme.BTN_INACTIVE)
        if msg is not None and n_confirmed == 0:
            msg.text = _tr(
                'Connect to GitHub or GitLab first to enable publish.')
        # Heights: button + message (the BodyLabel inside is dp(20))
        # plus the BoxLayout's internal spacing (dp(8)).
        row.height = dp(52) + dp(8) + dp(20)
        row.opacity = 1

    def save_contributor(self):
        """Called on the contributor input losing focus. Persists the
        trimmed value to the server (config.json :: collab.contributor)
        and shows a transient confirmation."""
        inp = self.ids.get('contributor_input')
        msg = self.ids.get('contributor_msg')
        if inp is None:
            return
        name = (inp.text or '').strip()
        try:
            set_contributor(name)
        except Exception as ex:
            if msg is not None:
                msg.text = _tr('Error: {error}').format(error=ex)
            return
        if msg is not None:
            msg.text = _tr('Saved.')
            Clock.schedule_once(
                lambda dt: setattr(msg, 'text', ''), 2.0)

    def connect_github(self):
        """Navigate to the GitHub device-flow screen and kick the flow.
        Putting the auto-start here (rather than in
        ``GitHubConnectScreen.on_pre_enter``) ties it to the explicit
        user gesture — language-change rebuilds re-instantiate the
        screen tree without firing this path."""
        app = App.get_running_app()
        app.go('github')
        sm = app.sm
        if sm.has_screen('github'):
            screen = sm.get_screen('github')
            Clock.schedule_once(lambda dt: screen.begin(), 0)

    def publish(self):
        """Create a remote repo on a confirmed git host for the
        most-recently-used project and push the local working tree up.

        Mirrors the recorder's ``do_publish`` flow (main.py:2884) but
        runs from the daemon's settings UI so any peer that hands the
        user into the gear can publish without owning the publish UI.
        Host pick: single-confirmed → use it; both → overlay."""
        langcode = (last_project() or '').strip()
        if not langcode:
            return  # button shouldn't have been visible
        status = get_credentials_status()
        confirmed = []
        if status.get('github', {}).get('confirmed'):
            confirmed.append('github')
        if status.get('gitlab', {}).get('confirmed'):
            confirmed.append('gitlab')
        if not confirmed:
            self._set_publish_msg(_tr(
                'Connect to GitHub or GitLab first to enable publish.'))
            return
        if len(confirmed) == 1:
            self._do_publish(langcode, confirmed[0], status)
            return
        self._show_host_picker(langcode, status)

    def _show_host_picker(self, langcode, status):
        """Modal with one button per confirmed host, plus Cancel."""
        from kivy.graphics import Color, RoundedRectangle
        from kivy.uix.modalview import ModalView
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        view = ModalView(size_hint=(0.85, None), height=dp(280),
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
        prompt = Label(
            text=_tr('Publish to which service?'),
            color=theme.TEXT, font_size=sp(15),
            size_hint_y=None, height=dp(48),
            halign='center', valign='middle')
        prompt.bind(size=lambda w, s: setattr(w, 'text_size', s))
        box.add_widget(prompt)

        def _make_btn(label, fill, on_release):
            btn = Button(
                text=label, size_hint_y=None, height=dp(48),
                background_color=theme.TRANSPARENT, background_normal='',
                color=(1, 1, 1, 1), font_size=sp(16), bold=True)
            with btn.canvas.before:
                Color(*fill)
                btn._bg = RoundedRectangle(pos=btn.pos, size=btn.size,
                                           radius=[dp(8)])
            btn.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
                     size=lambda w, s: setattr(w._bg, 'size', s))
            btn.bind(on_release=on_release)
            return btn

        def _pick(host):
            view.dismiss()
            self._do_publish(langcode, host, status)

        box.add_widget(_make_btn(
            'GitHub', theme.GREEN, lambda *_: _pick('github')))
        box.add_widget(_make_btn(
            'GitLab', theme.GREEN, lambda *_: _pick('gitlab')))
        box.add_widget(_make_btn(
            _tr('Cancel'), theme.SURFACE, lambda *_: view.dismiss()))
        view.add_widget(box)
        view.open()

    def _do_publish(self, langcode, host, status):
        """Resolve project + URL, kick the worker thread."""
        block = status.get(host, {}) or {}
        user = (block.get('username', '') or '').strip()
        if not user:
            self._set_publish_msg(_tr(
                'No {host} username on file.').format(host=host))
            return
        project = open_project(langcode)
        if project is None or not project.working_dir:
            self._set_publish_msg(_tr(
                'Project {langcode} not found.').format(langcode=langcode))
            return
        domain = 'gitlab.com' if host == 'gitlab' else 'github.com'
        remote_url = f'https://{domain}/{user}/{langcode}.git'
        contributor = (status.get('contributor', '') or '').strip() or 'Recorder'
        btn = self.ids.get('publish_btn')
        if btn is not None:
            btn.disabled = True
        self._set_publish_msg(_tr('Publishing to {url}...').format(
            url=remote_url))
        threading.Thread(
            target=self._publish_worker,
            args=(project.working_dir, remote_url, contributor),
            daemon=True).start()

    def _publish_worker(self, working_dir, remote_url, contributor):
        try:
            result = init_project(working_dir, remote_url,
                                  branch='main', contributor=contributor)
            text = translate_result(result) or _tr('Done.')
            # SERVER_UNAVAILABLE / SERVER_ERROR are wire-only codes
            # (no S.* alias); rpc-failure wrappers stamp them as
            # bare-string statuses, so check by string. The auth
            # codes do have S.* aliases.
            ok = not result.has_any(
                'SERVER_UNAVAILABLE', 'SERVER_ERROR',
                S.AUTH_REQUIRED, S.APP_NOT_INSTALLED,
                S.REPO_NOT_AUTHORIZED, S.ACCESS_DENIED)
        except Exception as ex:
            text = _tr('Publish failed: {error}').format(error=str(ex))
            ok = False
        Clock.schedule_once(
            lambda dt: self._publish_done(text, ok), 0)

    def _publish_done(self, msg, ok):
        self._set_publish_msg(msg)
        # On success the project now has a remote_url, so a refresh
        # naturally hides the publish row. On failure, re-enable so
        # the user can retry.
        self.refresh()

    def _set_publish_msg(self, text):
        msg = self.ids.get('publish_msg')
        if msg is not None:
            msg.text = text or ''

    def disconnect_github(self):
        # Wipe by overwriting with empty token (server.store.clear_github
        # would be cleaner; expose later).
        try:
            save_github_tokens({'access_token': '', 'refresh_token': ''},
                               username='')
            mark_github_app_installed(False)
        except Exception as ex:
            self.ids.status_label.text = _tr(
                'Error: {error}').format(error=ex)
            return
        self.refresh()

    def disconnect_gitlab(self):
        try:
            save_gitlab_credentials('', '')
        except Exception as ex:
            self.ids.status_label.text = _tr(
                'Error: {error}').format(error=ex)
            return
        self.refresh()


# ── GitHub device flow ──────────────────────────────────────────────────────

class GitHubConnectScreen(Screen):
    def on_pre_enter(self):
        # Reset visible state on entry, but do NOT auto-fire begin().
        # The "Connect to GitHub" button on the settings screen calls
        # ``begin()`` explicitly after navigating here, so users still
        # get one-tap entry into the device flow. Auto-firing here
        # would re-trigger the flow whenever the screen tree was
        # rebuilt for an unrelated reason — most visibly on every
        # language change, since ``_set_ui_language`` clears + re-adds
        # all screens.
        self.ids.gh_message.text = _tr('Starting device flow...')
        self.ids.gh_user_code.text = ''
        self.ids.gh_begin_btn.disabled = False
        self._user_code = ''

    def begin(self):
        self.ids.gh_begin_btn.disabled = True
        self.ids.gh_message.text = _tr('Starting device flow...')
        threading.Thread(target=self._worker, daemon=True).start()

    def copy_code(self):
        if not self._user_code:
            return
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(self._user_code)
            self.ids.gh_message.text = (self.ids.gh_message.text + '\n'
                                        + _tr('(code copied)'))
        except Exception:
            pass

    def _worker(self):
        # Direct-import — UI process runs in the same package as the daemon
        from azt_collabd.auth import (
            device_flow_start, device_flow_poll,
            get_github_username, check_app_installed,
        )
        try:
            resp = device_flow_start()
            user_code = resp['user_code']
            device_code = resp['device_code']
            verify_uri = resp.get('verification_uri',
                                  'https://github.com/login/device')
            interval = resp.get('interval', 5)
            expires_in = resp.get('expires_in', 900)

            def _show(dt, _code=user_code, _uri=verify_uri):
                self._user_code = _code
                self.ids.gh_user_code.text = _code
                # Auto-copy the user_code so the user can paste it
                # straight into the GitHub device page without
                # needing to tap the Copy button. Best-effort: a
                # clipboard failure (Android headless test devices,
                # X11 missing on a CI box, etc.) is silent.
                copied_ok = False
                try:
                    from kivy.core.clipboard import Clipboard
                    Clipboard.copy(_code)
                    copied_ok = True
                except Exception:
                    pass
                msg = _tr(
                    'Opening {uri}\nEnter the code on the GitHub page.'
                ).format(uri=_uri)
                if copied_ok:
                    msg += '\n' + _tr('(code copied)')
                self.ids.gh_message.text = msg
            Clock.schedule_once(_show, 0)
            try:
                webbrowser.open(verify_uri)
            except Exception:
                pass

            token_data = device_flow_poll(device_code, interval, expires_in)
            access_token = token_data['access_token']
            username = get_github_username(access_token) or 'unknown'
            save_github_tokens(token_data, username)

            # Best-effort: read app-install state
            try:
                info = check_app_installed(access_token)
                if info.get('installed'):
                    mark_github_app_installed(True)
            except Exception:
                pass

            def _done(dt, _u=username):
                self.ids.gh_message.text = _tr(
                    'Connected as {username}.').format(username=_u)
                self.ids.gh_begin_btn.disabled = False
            Clock.schedule_once(_done, 0)

        except AuthError as ex:
            msg = translate_status(ex.status)
            def _err(dt, _m=msg):
                self.ids.gh_message.text = _tr(
                    'Failed: {error}').format(error=_m)
                self.ids.gh_begin_btn.disabled = False
            Clock.schedule_once(_err, 0)
        except Exception as ex:
            def _err(dt, _e=str(ex)):
                self.ids.gh_message.text = _tr(
                    'Failed: {error}').format(error=_e)
                self.ids.gh_begin_btn.disabled = False
            Clock.schedule_once(_err, 0)


# ── GitLab PAT form ─────────────────────────────────────────────────────────

class GitLabFormScreen(Screen):
    def on_pre_enter(self):
        try:
            status = get_credentials_status()
            self.ids.gl_user.text = status.get('gitlab', {}).get('username', '')
        except Exception:
            pass
        self.ids.gl_token.text = ''
        self.ids.gl_msg.text = ''

    def test(self):
        """Save the entered credentials and immediately validate them
        against gitlab.com. The single-button affordance avoids the
        "did I press Save before Test?" footgun — Test is the only
        operation, and on success the credentials are persisted."""
        u = self.ids.gl_user.text.strip()
        t = self.ids.gl_token.text.strip()
        if not u or not t:
            self.ids.gl_msg.text = _tr('Enter both username and token.')
            return
        self.ids.gl_test_btn.disabled = True
        self.ids.gl_msg.text = _tr('Testing...')
        threading.Thread(
            target=self._test_worker, args=(u, t), daemon=True).start()

    def _test_worker(self, username, token):
        # Server-side: a successful test persists credentials and sets
        # gitlab.confirmed=True in one shot. Failed tests neither save
        # nor mark confirmed, so the user can correct the PAT and retry
        # without first having to disconnect.
        info = test_gitlab_credentials(username, token)
        msg = self._format_test_result(info, username)
        Clock.schedule_once(lambda dt: self._test_done(msg), 0)

    def _format_test_result(self, info, username):
        if not info.get('ok'):
            return _tr('Server unavailable: {error}').format(
                error=info.get('error', '') or '?')
        if info.get('valid'):
            return (_tr('Connected as {username}. Credentials saved.')
                    .format(username=info.get('server_username', '')
                            or username))
        err = info.get('error', '') or 'unknown'
        if err == 'invalid_token':
            return _tr('Invalid token. Check your personal access token.')
        if err == 'username_mismatch':
            return _tr(
                'Token is valid, but belongs to {server_username}, '
                'not {username}.').format(
                    server_username=info.get('server_username', '?'),
                    username=username)
        if err.startswith('network_error'):
            return _tr('Network error — check your connection.')
        return _tr('Test failed: {error}').format(error=err)

    def _test_done(self, msg):
        self.ids.gl_msg.text = msg
        self.ids.gl_test_btn.disabled = False


# ── App ─────────────────────────────────────────────────────────────────────

class CollabUIApp(App):
    """Standalone collab settings UI. Credentials, host toggle, and
    GitHub/GitLab connect screens. Project picking lives in its own
    helper subprocess (`python -m azt_collabd projects`); see
    azt_collabd/ui/picker_app.py."""

    title = 'A-Z+T Collab'
    subtitle = StringProperty('Settings')
    icon = StringProperty(_AZT_ICON)
    version_string = StringProperty(
        f'client {azt_collab_client.__version__}'
        f'  ·  '
        f'server {azt_collabd.__version__ if hasattr(azt_collabd, "__version__") else ""}'
    )

    def build(self):
        theme.set_theme('Ocean')
        font_name = register_charis()
        register_kv(font_name)
        self.sm = RootSM(transition=SlideTransition())
        return self.sm

    def go(self, name):
        self.sm.current = name


def main():
    azt_collabd.configure()
    CollabUIApp().run()


if __name__ == '__main__':
    main()
