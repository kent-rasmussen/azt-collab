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
from azt_collab_client.ui import (
    check_for_update, icon_path, register_charis, share_running_apk,
    theme,
)

import azt_collabd
from azt_collabd.status import AuthError
from azt_collab_client import (
    S,
    get_contributor,
    get_credentials_status,
    github_app_install_url,
    init_project,
    is_online,
    last_project,
    mark_github_app_installed,
    open_project,
    project_status,
    save_github_tokens,
    save_gitlab_credentials,
    test_github_credentials,
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
#:set SHARE_ICON '{share_icon}'

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
                # ── Share this app ─────────────────────────────────────
                # No-op on desktop (share_running_apk surfaces a
                # translated "Android only" message via on_error); on
                # Android this shares the running server APK so a user
                # can hand it to a teammate that needs the daemon.
                RecBtn:
                    text: _('Share this app')
                    halign: 'left'
                    padding: [dp(52), 0]
                    text_size: self.size
                    valign: 'middle'
                    normal_color: T.SURFACE
                    on_release: app.share_apk()
                    Image:
                        source: SHARE_ICON
                        size_hint: None, None
                        size: dp(24), dp(24)
                        x: self.parent.x + dp(16)
                        center_y: self.parent.center_y
                # ── Update this app ────────────────────────────────────
                # Polls the configured GitHub repo for a newer release
                # asset and triggers the system installer on Android.
                # Status messages flow through update_msg below.
                RecBtn:
                    text: _('Update this app')
                    halign: 'left'
                    padding: [dp(52), 0]
                    text_size: self.size
                    valign: 'middle'
                    normal_color: T.SURFACE
                    on_release: app.update_app()
                BodyLabel:
                    id: update_msg
                    text: ''
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: dp(20)
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
    # State-aware: ``on_pre_enter`` reads credentials_status and shows
    # only the controls relevant to the current state. Three shapes:
    #   not connected           → device-flow box visible, manage hidden;
    #                             begin() auto-fires.
    #   connected, not confirmed→ manage box visible with Test + Install
    #                             (if not app_installed) + Re-auth +
    #                             Disconnect; device-flow box hidden;
    #                             nothing auto-fires.
    #   connected, confirmed    → same as above plus a verified badge in
    #                             the status line.
    # All show/hide goes through the Kivy hide/show pattern (height: 0,
    # opacity: 0) so a hidden section neither paints nor steals touch.
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
                    text: ''
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                    text_size: self.width, None
                # ── Device-flow section ────────────────────────────────
                # Visible while the user is mid-flow (showing the code,
                # the Copy button, and the Begin / re-fire button).
                # Hidden once we already have a token (the manage box
                # below takes over).
                BoxLayout:
                    id: gh_device_flow_box
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(14)
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
                # ── Manage section ─────────────────────────────────────
                # Visible only when a token is already on file. Each
                # button is itself shown/hidden by Python state so the
                # "Install GitHub App" CTA is only there if the user
                # hasn't installed it yet.
                BoxLayout:
                    id: gh_manage_box
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(10)
                    RecBtn:
                        id: gh_test_btn
                        text: _('Test connection')
                        normal_color: T.GREEN
                        on_release: root.test()
                    RecBtn:
                        id: gh_install_app_btn
                        text: _('Install GitHub App')
                        normal_color: T.ACCENT
                        size_hint_y: None
                        height: 0
                        opacity: 0
                        disabled: True
                        on_release: root.install_app()
                    NavBtn:
                        text: _('Re-authenticate')
                        on_release: root.reauthenticate()
                    NavBtn:
                        text: _('Disconnect')
                        on_release: root.disconnect()
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
    Builder.load_string(KV_TEMPLATE.format(
        font_name=font_name,
        share_icon=icon_path('share_dark'),
    ))
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

        Picks the candidate project in this priority order:

        1. ``last_project()`` (suite-wide "last opened" key). The
           recorder updates it via ``set_last_project`` when a project
           is chosen through the picker.
        2. If that's empty or doesn't resolve to a registered project,
           fall back to scanning ``list_projects()`` and picking the
           one with the largest ``last_sync`` (most recently active).
           This covers users on older recorder versions that load a
           project without going through the picker — without this
           fallback the publish row would silently stay hidden even
           though the daemon clearly has a project on file.

        The row is visible only when a candidate is found AND that
        project has no remote yet. The button is enabled iff at least
        one host's credentials are confirmed."""
        row = self.ids.get('publish_row')
        btn = self.ids.get('publish_btn')
        msg = self.ids.get('publish_msg')
        if row is None or btn is None:
            return
        # Reset to hidden by default; the rest flips it back on only
        # if every condition holds.
        row.height = 0
        row.opacity = 0
        if msg is not None:
            msg.text = ''
        project = self._pick_publish_candidate()
        if project is None:
            return
        # Authoritative remote_url comes from project_status, which
        # reads the live git config. The Project's remote_url is the
        # cached projects.json value — pre-0.20.1 daemons forgot to
        # write it back on a successful init_project, so existing
        # published projects can have an empty cached remote_url even
        # though their working_dir's `.git/config` lists origin
        # correctly. Trusting Project.remote_url alone would re-show
        # the publish button on those repos forever.
        live_remote_url = (project.remote_url or '').strip()
        try:
            ps = project_status(project.langcode)
        except Exception:
            ps = None
        if ps is not None and (ps.remote_url or '').strip():
            live_remote_url = ps.remote_url.strip()
        if live_remote_url:
            return
        langcode = project.langcode
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
        # Heights: button + message (BodyLabel is dp(20)) + spacing.
        row.height = dp(52) + dp(8) + dp(20)
        row.opacity = 1

    def _pick_publish_candidate(self):
        """Return the Project the daemon last touched, or ``None`` if
        there isn't one (no project ever opened on this device, server
        unreachable, or stale registry).

        ``last_project()`` is server-tracked from azt_collabd 0.19+ /
        client 0.23+: every langcode-bound RPC auto-stamps via
        ``server._touch_project``, so the langcode that comes back is
        always the most recently active project regardless of which
        peer touched it. No fallback scanning is needed — if no
        project has ever been touched, hiding the publish row is the
        correct UX."""
        import sys
        langcode = (last_project() or '').strip()
        if not langcode:
            print('[settings] last_project: empty (no project '
                  'touched on this device yet)',
                  file=sys.stderr, flush=True)
            return None
        try:
            project = open_project(langcode)
        except Exception as ex:
            print(f'[settings] open_project({langcode!r}) raised: '
                  f'{ex}', file=sys.stderr, flush=True)
            return None
        if project is None or not project.lift_exists:
            print(f'[settings] last_project={langcode!r} no longer '
                  f'resolves to a live project',
                  file=sys.stderr, flush=True)
            return None
        print(f'[settings] publish candidate: {langcode!r} '
              f'(remote_url={project.remote_url!r})',
              file=sys.stderr, flush=True)
        return project

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
        """Navigate to the GitHub connect/manage screen. The screen's
        own ``on_pre_enter`` reads ``credentials_status`` and decides
        whether to fire the device flow: only when no token is on
        file. With a token already saved (verified or not) the
        screen renders the manage view (Test / Re-authenticate /
        Disconnect / Install GitHub App) and waits for the user —
        we don't re-prompt every time they land here."""
        App.get_running_app().go('github')

    def publish(self):
        """Create a remote repo on a confirmed git host for the
        most-recently-used project and push the local working tree up.

        Mirrors the recorder's ``do_publish`` flow (main.py:2884) but
        runs from the daemon's settings UI so any peer that hands the
        user into the gear can publish without owning the publish UI.
        Host pick: single-confirmed → use it; both → overlay."""
        # Re-pick rather than trusting a captured langcode — the user
        # may have changed projects in another peer between the row
        # rendering and this click.
        project = self._pick_publish_candidate()
        if project is None:
            return  # button shouldn't have been visible
        langcode = project.langcode
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
        import sys
        print(f'[publish] init_project working_dir={working_dir!r} '
              f'remote_url={remote_url!r} contributor={contributor!r}',
              file=sys.stderr, flush=True)
        try:
            result = init_project(working_dir, remote_url,
                                  branch='main', contributor=contributor)
            codes = result.codes()
            print(f'[publish] init_project done: codes={codes!r}',
                  file=sys.stderr, flush=True)
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
            print(f'[publish] init_project raised: '
                  f'{type(ex).__name__}: {ex}',
                  file=sys.stderr, flush=True)
            text = _tr('Publish failed: {error}').format(error=str(ex))
            ok = False
        Clock.schedule_once(
            lambda dt: self._publish_done(text, ok), 0)

    def _publish_done(self, msg, ok):
        # Refresh first so a successful publish (which now leaves the
        # project with a populated remote_url) hides the row. Set the
        # outcome message AFTER refresh — _refresh_publish_row's first
        # act is to clear msg.text, so doing it the other way around
        # silently wipes the outcome the user needs to see.
        self.refresh()
        self._set_publish_msg(msg)
        if not ok:
            # Failure: row is still visible (remote_url stayed empty);
            # re-enable the button so the user can retry once they've
            # fixed whatever the message describes.
            btn = self.ids.get('publish_btn')
            if btn is not None:
                btn.disabled = False

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
    """State-aware connect/manage screen for GitHub.

    Three shapes, picked from ``credentials_status['github']`` in
    ``on_pre_enter``:

    - ``not connected`` (no access token on file) → device-flow box
      visible, manage box hidden, ``begin()`` auto-fires so the user
      doesn't have to tap twice. The previous implementation
      auto-fired on the explicit "Connect to GitHub" tap from the
      settings screen instead; that's gone — settings now only
      *navigates* to this screen and lets us decide.
    - ``connected, not confirmed`` → manage box visible (Test +
      Install GitHub App if not installed + Re-authenticate +
      Disconnect); device-flow box hidden; nothing auto-fires.
      The user already has a token; they shouldn't be re-prompted
      unconditionally — they'll either Test (which sets confirmed)
      or Re-authenticate (which forces another device flow).
    - ``connected, confirmed`` → same controls as above plus a
      "(verified)" badge in the status line.

    All show/hide goes through the Kivy hide/show pattern (height: 0,
    opacity: 0). Refer to ~/.claude-sil/CLAUDE.md for the cookbook —
    in particular the rule about not relying on ``minimum_height``
    when a BoxLayout starts at 0."""

    _user_code = ''
    _heights = None  # captured target heights for the show/hide boxes

    def on_pre_enter(self):
        status = self._safe_status()
        gh = (status or {}).get('github', {}) or {}
        connected = bool(gh.get('connected'))
        confirmed = bool(gh.get('confirmed'))
        app_installed = bool(gh.get('app_installed'))
        username = gh.get('username', '') or ''

        self.ids.gh_user_code.text = ''
        self.ids.gh_begin_btn.disabled = False
        self._user_code = ''

        if not connected:
            self._show_device_flow()
            self._hide_manage()
            self.ids.gh_message.text = _tr('Starting device flow...')
            # Explicit user gesture is implied by reaching this screen
            # with no token — settings only navigates here on a tap.
            self.begin()
            return

        # Connected: skip the device flow, lay out the manage view.
        self._hide_device_flow()
        self._show_manage(app_installed=app_installed)
        if confirmed:
            self.ids.gh_message.text = _tr(
                'Connected as {username} (verified).'
            ).format(username=username or '?')
        elif app_installed:
            self.ids.gh_message.text = _tr(
                'Connected as {username}. Tap Test connection to '
                'verify.'
            ).format(username=username or '?')
        else:
            self.ids.gh_message.text = _tr(
                'Connected as {username}. Install the GitHub App so '
                'the daemon can push your project, then tap Test '
                'connection.'
            ).format(username=username or '?')

    def _safe_status(self):
        try:
            return get_credentials_status()
        except Exception as ex:
            print(f'[github-connect] status fetch failed: {ex}')
            return {}

    # ── show / hide pattern ───────────────────────────────────────────

    def _show_device_flow(self):
        # Section heights: SectionLabel(32) + dp(72) + Widget(8) +
        # RecBtn(52) + 4×spacing(14) = 220.
        box = self.ids.gh_device_flow_box
        box.height = dp(220)
        box.opacity = 1
        box.disabled = False

    def _hide_device_flow(self):
        box = self.ids.gh_device_flow_box
        box.height = 0
        box.opacity = 0
        box.disabled = True

    def _show_manage(self, *, app_installed):
        # Always show: Test (52) + 2×NavBtn(48) + 3×spacing(10) = 178.
        # Add Install GitHub App (52 + spacing 10 = 62) when not
        # installed.
        install_btn = self.ids.gh_install_app_btn
        if app_installed:
            install_btn.height = 0
            install_btn.opacity = 0
            install_btn.disabled = True
            extra = 0
        else:
            install_btn.height = dp(52)
            install_btn.opacity = 1
            install_btn.disabled = False
            extra = dp(52) + dp(10)
        box = self.ids.gh_manage_box
        box.height = dp(178) + extra
        box.opacity = 1
        box.disabled = False

    def _hide_manage(self):
        box = self.ids.gh_manage_box
        box.height = 0
        box.opacity = 0
        box.disabled = True

    # ── device-flow path ─────────────────────────────────────────────

    def begin(self):
        # Make sure the device-flow widgets are visible — re-fired by
        # ``reauthenticate()`` from the manage view, which had them
        # hidden.
        self._show_device_flow()
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
            app_installed = False
            try:
                info = check_app_installed(access_token)
                app_installed = bool(info.get('installed'))
                if app_installed:
                    mark_github_app_installed(True)
            except Exception:
                pass

            def _done(dt, _u=username, _ai=app_installed):
                # The fresh token just had ``confirmed`` reset to
                # False by ``set_github_tokens``; flip the screen to
                # the manage view so the user can Test (or install
                # the app first if needed) without re-firing the
                # device flow.
                self._hide_device_flow()
                self._show_manage(app_installed=_ai)
                if _ai:
                    self.ids.gh_message.text = _tr(
                        'Connected as {username}. Tap Test '
                        'connection to verify.').format(username=_u)
                else:
                    self.ids.gh_message.text = _tr(
                        'Connected as {username}. Install the GitHub '
                        'App so the daemon can push your project, '
                        'then tap Test connection.').format(username=_u)
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

    # ── manage path ──────────────────────────────────────────────────

    def test(self):
        """Run the live test against ``api.github.com/user``. The
        daemon also refreshes ``app_installed`` while it has a valid
        token in hand; we re-render the manage view so the
        Install GitHub App row drops away if the probe found it."""
        self.ids.gh_test_btn.disabled = True
        self.ids.gh_message.text = _tr('Testing...')
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        info = test_github_credentials()
        Clock.schedule_once(lambda dt: self._test_done(info), 0)

    def _test_done(self, info):
        self.ids.gh_test_btn.disabled = False
        try:
            status = self._safe_status()
            gh = (status or {}).get('github', {}) or {}
            self._show_manage(app_installed=bool(gh.get('app_installed')))
        except Exception:
            pass
        if not info.get('ok'):
            self.ids.gh_message.text = _tr(
                'Server unavailable: {error}').format(
                    error=info.get('error', '?'))
            return
        if info.get('valid'):
            user = info.get('server_username', '') or '?'
            if info.get('app_installed'):
                self.ids.gh_message.text = _tr(
                    'Connected as {username} (verified). Credentials '
                    'and app install confirmed.').format(username=user)
            else:
                self.ids.gh_message.text = _tr(
                    'Connected as {username} (verified). Install the '
                    'GitHub App to enable push.').format(username=user)
            return
        err = info.get('error', '') or 'unknown'
        if err == 'invalid_token':
            self.ids.gh_message.text = _tr(
                'Token rejected by GitHub. Tap Re-authenticate.')
            return
        if err.startswith('network_error'):
            self.ids.gh_message.text = _tr(
                'Network error — check your connection.')
            return
        self.ids.gh_message.text = _tr(
            'Test failed: {error}').format(error=err)

    def install_app(self):
        """Open the GitHub App install page in the user's browser.
        After they grant access on GitHub they return here and tap
        Test connection — the daemon's test path refreshes the
        ``app_installed`` flag automatically on success."""
        try:
            url = github_app_install_url()
        except Exception as ex:
            self.ids.gh_message.text = _tr(
                'Could not open install page: {error}').format(error=ex)
            return
        try:
            webbrowser.open(url)
            self.ids.gh_message.text = _tr(
                'Opening {uri}\nWhen you finish on GitHub, return '
                'here and tap Test connection.').format(uri=url)
        except Exception as ex:
            self.ids.gh_message.text = _tr(
                'Could not open install page: {error}').format(error=ex)

    def reauthenticate(self):
        """User explicitly asked to re-run the device flow (token
        expired / revoked / wrong account). Drop back into the
        device-flow view and start it."""
        self._hide_manage()
        self.begin()

    def disconnect(self):
        """Wipe the GitHub credentials block. Same call shape as the
        settings screen's Disconnect, but lives here so the
        manage view is fully self-contained."""
        try:
            save_github_tokens({'access_token': '', 'refresh_token': ''},
                               username='')
            mark_github_app_installed(False)
        except Exception as ex:
            self.ids.gh_message.text = _tr(
                'Error: {error}').format(error=ex)
            return
        # After disconnect the screen state flips back to the
        # not-connected shape — re-run on_pre_enter to render it.
        self.on_pre_enter()


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

    def share_apk(self):
        """Hand the running server APK to Android's share sheet so the
        user can send it to a teammate. No-op (with a translated
        error popup) on desktop — there's no APK to share."""
        share_running_apk(filename='aztcollab.apk',
                          on_error=self._show_error)

    def update_app(self):
        """Poll the configured GitHub repo for a newer server APK and,
        if found, download + trigger Android's system installer.
        Repo / asset filename are sourced from ``azt_collabd.config``
        so the same code path serves any sister app that wires its own
        ``update_repo`` via ``configure(update_repo=...)``."""
        from azt_collabd.config import update_repo
        check_for_update(
            repo=update_repo(),
            current_version=azt_collabd.__version__,
            asset_filename='aztcollab.apk',
            on_status=self._set_update_msg,
            on_no_update=lambda: self._set_update_msg(_tr('Up to date.')),
            on_error=self._show_error,
        )

    def _set_update_msg(self, text):
        try:
            sm = self.sm
            screen = sm.get_screen('settings') if sm.has_screen(
                'settings') else None
            msg = screen.ids.get('update_msg') if screen is not None \
                else None
        except Exception:
            msg = None
        if msg is not None:
            msg.text = text or ''

    def _show_error(self, msg):
        """Minimal error popup for share_apk. The settings screen has
        no inline status surface for app-level errors; a popup is loud
        enough to not be missed and dismissable in one tap."""
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        Popup(
            title=_tr('Error'),
            content=Label(text=str(msg), color=theme.TEXT,
                          font_size=sp(14)),
            size_hint=(0.85, None), height=dp(220),
        ).open()


def main():
    azt_collabd.configure()
    CollabUIApp().run()


if __name__ == '__main__':
    main()
