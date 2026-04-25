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
import threading
import webbrowser

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition

import azt_collabd
from azt_collabd.status import AuthError
from azt_collab_client import (
    get_credentials_status,
    is_online,
    list_projects,
    mark_github_app_installed,
    save_github_tokens,
    save_gitlab_credentials,
    set_collab_host,
    translate_status,
)


KV = '''
<RootSM>:
    transition: SlideTransition()
    SettingsScreen:
        name: 'settings'
    GitHubConnectScreen:
        name: 'github'
    GitLabFormScreen:
        name: 'gitlab'
    ProjectsScreen:
        name: 'projects'

<NavBar@BoxLayout>:
    size_hint_y: None
    height: dp(48)
    spacing: dp(10)

<HeaderLabel@Label>:
    font_size: sp(20)
    size_hint_y: None
    height: dp(40)

<BodyLabel@Label>:
    text_size: self.size
    halign: 'left'
    valign: 'top'
    font_size: sp(14)

<SettingsScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(20)
        spacing: dp(10)
        HeaderLabel:
            text: 'A-Z+T Collab — Settings'
        BodyLabel:
            id: status_label
            text: 'Loading...'
        Label:
            text: 'Host'
            size_hint_y: None
            height: dp(28)
            font_size: sp(14)
        BoxLayout:
            size_hint_y: None
            height: dp(40)
            spacing: dp(10)
            Button:
                id: host_github_btn
                text: 'GitHub'
                on_release: root.choose_host('github')
            Button:
                id: host_gitlab_btn
                text: 'GitLab'
                on_release: root.choose_host('gitlab')
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            spacing: dp(10)
            Button:
                text: 'Connect GitHub'
                on_release: app.go('github')
            Button:
                text: 'Set GitLab creds'
                on_release: app.go('gitlab')
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            spacing: dp(10)
            Button:
                text: 'Disconnect GitHub'
                on_release: root.disconnect_github()
            Button:
                text: 'Disconnect GitLab'
                on_release: root.disconnect_gitlab()
        NavBar:
            Button:
                text: 'Refresh'
                on_release: root.refresh()
            Button:
                text: 'Projects'
                on_release: app.go('projects')

<GitHubConnectScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(20)
        spacing: dp(10)
        HeaderLabel:
            text: 'Connect to GitHub'
        BodyLabel:
            id: gh_message
            text: 'Click "Begin" to start.'
        BoxLayout:
            size_hint_y: None
            height: dp(64)
            spacing: dp(10)
            Label:
                id: gh_user_code
                text: ''
                font_size: sp(28)
                bold: True
            Button:
                size_hint_x: None
                width: dp(80)
                text: 'Copy'
                on_release: root.copy_code()
        NavBar:
            Button:
                id: gh_begin_btn
                text: 'Begin'
                on_release: root.begin()
            Button:
                text: 'Back'
                on_release: app.go('settings')

<GitLabFormScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(20)
        spacing: dp(10)
        HeaderLabel:
            text: 'GitLab credentials'
        BodyLabel:
            text: 'Enter your GitLab username and a personal access token (read/write to repos).'
            size_hint_y: None
            height: dp(60)
        Label:
            text: 'Username'
            size_hint_y: None
            height: dp(24)
            font_size: sp(13)
        TextInput:
            id: gl_user
            multiline: False
            size_hint_y: None
            height: dp(40)
        Label:
            text: 'Token'
            size_hint_y: None
            height: dp(24)
            font_size: sp(13)
        TextInput:
            id: gl_token
            password: True
            multiline: False
            size_hint_y: None
            height: dp(40)
        BodyLabel:
            id: gl_msg
            text: ''
            size_hint_y: None
            height: dp(40)
        NavBar:
            Button:
                text: 'Save'
                on_release: root.save()
            Button:
                text: 'Back'
                on_release: app.go('settings')

<ProjectsScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(20)
        spacing: dp(10)
        HeaderLabel:
            text: 'Projects'
        BodyLabel:
            id: list_label
            text: 'Loading...'
        NavBar:
            Button:
                text: 'Refresh'
                on_release: root.refresh()
            Button:
                text: 'Settings'
                on_release: app.go('settings')
'''


class RootSM(ScreenManager):
    pass


# ── Settings ────────────────────────────────────────────────────────────────

class SettingsScreen(Screen):
    def on_enter(self):
        self.refresh()

    def refresh(self):
        try:
            status = get_credentials_status()
            online = is_online()
        except Exception as ex:
            self.ids.status_label.text = f'Error: {ex}'
            return
        gh = status.get('github', {})
        gl = status.get('gitlab', {})
        host = status.get('host', 'github')
        self.ids.host_github_btn.disabled = (host == 'github')
        self.ids.host_gitlab_btn.disabled = (host == 'gitlab')
        lines = [
            f"Online:   {'yes' if online else 'no'}",
            "",
            "GitHub",
            f"  Connected:     {'yes' if gh.get('connected') else 'no'}",
            f"  Username:      {gh.get('username', '') or '-'}",
            f"  App installed: {'yes' if gh.get('app_installed') else 'no'}",
            "",
            "GitLab",
            f"  Connected: {'yes' if gl.get('connected') else 'no'}",
            f"  Username:  {gl.get('username', '') or '-'}",
        ]
        self.ids.status_label.text = '\n'.join(lines)

    def choose_host(self, host):
        try:
            set_collab_host(host)
        except Exception as ex:
            self.ids.status_label.text = f'Error setting host: {ex}'
            return
        self.refresh()

    def disconnect_github(self):
        # Wipe by overwriting with empty token (server.store.clear_github
        # would be cleaner; expose later).
        try:
            save_github_tokens({'access_token': '', 'refresh_token': ''},
                               username='')
            mark_github_app_installed(False)
        except Exception as ex:
            self.ids.status_label.text = f'Error: {ex}'
            return
        self.refresh()

    def disconnect_gitlab(self):
        try:
            save_gitlab_credentials('', '')
        except Exception as ex:
            self.ids.status_label.text = f'Error: {ex}'
            return
        self.refresh()


# ── GitHub device flow ──────────────────────────────────────────────────────

class GitHubConnectScreen(Screen):
    def on_pre_enter(self):
        self.ids.gh_message.text = 'Click "Begin" to start.'
        self.ids.gh_user_code.text = ''
        self.ids.gh_begin_btn.disabled = False
        self._user_code = ''

    def begin(self):
        self.ids.gh_begin_btn.disabled = True
        self.ids.gh_message.text = 'Starting device flow...'
        threading.Thread(target=self._worker, daemon=True).start()

    def copy_code(self):
        if not self._user_code:
            return
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(self._user_code)
            self.ids.gh_message.text = (self.ids.gh_message.text +
                                        '\n(code copied)')
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
                self.ids.gh_message.text = (
                    f'Opening {_uri}\nEnter the code on the GitHub page.')
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
                self.ids.gh_message.text = f'Connected as {_u}.'
                self.ids.gh_begin_btn.disabled = False
            Clock.schedule_once(_done, 0)

        except AuthError as ex:
            msg = translate_status(ex.status)
            def _err(dt, _m=msg):
                self.ids.gh_message.text = f'Failed: {_m}'
                self.ids.gh_begin_btn.disabled = False
            Clock.schedule_once(_err, 0)
        except Exception as ex:
            def _err(dt, _e=str(ex)):
                self.ids.gh_message.text = f'Failed: {_e}'
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

    def save(self):
        u = self.ids.gl_user.text.strip()
        t = self.ids.gl_token.text.strip()
        if not u or not t:
            self.ids.gl_msg.text = 'Enter both username and token.'
            return
        try:
            save_gitlab_credentials(u, t)
        except Exception as ex:
            self.ids.gl_msg.text = f'Error: {ex}'
            return
        self.ids.gl_msg.text = f'Saved for {u}.'


# ── Projects ────────────────────────────────────────────────────────────────

class ProjectsScreen(Screen):
    def on_enter(self):
        self.refresh()

    def refresh(self):
        try:
            projects = list_projects()
        except Exception as ex:
            self.ids.list_label.text = f'Error: {ex}'
            return
        if not projects:
            self.ids.list_label.text = 'No projects registered yet.'
            return
        rows = []
        for p in projects:
            rows.append(f'{p.langcode}')
            rows.append(f'  path:   {p.working_dir}')
            if p.remote_url:
                rows.append(f'  remote: {p.remote_url}')
            if p.last_sync:
                ts = datetime.datetime.fromtimestamp(p.last_sync)
                rows.append(f'  last sync: {ts:%Y-%m-%d %H:%M}')
            rows.append('')
        self.ids.list_label.text = '\n'.join(rows).rstrip()


# ── App ─────────────────────────────────────────────────────────────────────

class CollabUIApp(App):
    title = 'A-Z+T Collab'

    def build(self):
        Builder.load_string(KV)
        self.sm = RootSM()
        return self.sm

    def go(self, name):
        self.sm.current = name


def main():
    azt_collabd.configure()
    CollabUIApp().run()


if __name__ == '__main__':
    main()
