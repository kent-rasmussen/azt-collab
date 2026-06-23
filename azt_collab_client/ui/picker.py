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
from kivy.metrics import dp
from kivy.uix.screenmanager import Screen

from .icons import icon_path


_KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T azt_collab_client.ui.theme
#:import _ azt_collab_client.translate.tr
#:import LAN_POPUPS azt_collab_client.ui.lan_popups
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
                        # Hidden in 0.45.0 — the open-file pathway is
                        # currently rough on Android (SAF picker
                        # returns a content:// URI the daemon can't
                        # walk back to a directory). Kept in the KV
                        # tree so the wiring is one prop flip away
                        # when we decide to re-enable it. Per the
                        # Kivy hide/show pattern in
                        # `~/.claude-sil/CLAUDE.md`: explicit
                        # ``height: 0`` + ``opacity: 0`` + disabled
                        # so the button has no hit area and can't
                        # steal focus.
                        size_hint_y: None
                        height: 0
                        opacity: 0
                        disabled: True
                        text: _('I have one on my phone')
                        normal_color: T.ACCENT
                        on_release: app.open_file()
                    # Notice shown when contributor is unset. Both
                    # ``clone_dialog`` (for private repos that need
                    # an authed user) and ``receive_from_phone``
                    # (which goes through ``lan_pair_accept``, which
                    # refuses on ``CONTRIBUTOR_UNSET``) require the
                    # contributor field to be set first. Rather than
                    # let the user tap into either path only to be
                    # bounced silently, we hide both buttons (height
                    # / opacity / disabled tri-set, per the Kivy
                    # hide/show pattern) and surface a red notice
                    # pointing the user at settings. Refreshed each
                    # ``on_enter`` via ``_refresh_contributor_state``.
                    #
                    # Uses plain ``Label`` rather than ``BodyLabel``
                    # so the rule resolves regardless of whether the
                    # host has loaded app.py's settings KV first —
                    # peer-app contexts (recorder, viewer) don't.
                    Label:
                        id: contributor_notice
                        text: _('To clone from a private repo, or to get a project from a local phone, go to settings and add your name first.')
                        color: T.RED
                        bold: True
                        font_size: sp(13)
                        font_name: FONT
                        halign: 'center'
                        valign: 'middle'
                        size_hint_y: None
                        height: 0
                        opacity: 0
                        text_size: self.width, None
                        padding: dp(8), dp(4)
                    RecBtn:
                        id: clone_internet_btn
                        text: _('Clone Internet Repository')
                        normal_color: T.BTN_INACTIVE
                        on_release: app.clone_dialog()
                    RecBtn:
                        id: receive_from_phone_btn
                        text: _('Receive a project from another phone')
                        normal_color: T.BTN_INACTIVE
                        # Opens the pending-offers chooser: shows
                        # share offers waiting from already-paired
                        # phones AND a "Scan QR code" fallthrough
                        # for first-time pair-with-a-new-phone.
                        # ``root.receive_from_phone()`` wraps the
                        # popup with an ``on_done`` that emits the
                        # freshly-cloned project to the host App so
                        # the user lands inside it immediately (same
                        # behaviour as tapping an existing project
                        # button below), instead of being dropped
                        # back onto the picker with no visible new
                        # project until they exit and re-enter.
                        on_release: root.receive_from_phone()
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
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: dp(56)
                spacing: dp(2)
                Label:
                    text: app.version_string
                    font_size: sp(13)
                    font_name: FONT
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: dp(22)
                    halign: 'center'
                    text_size: self.size
                Button:
                    # Diagnostic affordance. Always visible — the
                    # user we're targeting here is the one stuck on
                    # an empty picker who can't reach the gear-→
                    # settings → Share-daemon-log path (recorder
                    # picker's gear navigates to the recorder's own
                    # settings, which doesn't host that button; the
                    # server-APK picker's gear does but only if the
                    # daemon-log-to-file toggle is already on). This
                    # button ships a daemon-built registry/filesystem
                    # snapshot every time, plus the log file when
                    # it exists.
                    id: share_diag_btn
                    text: _('Share diagnostics')
                    size_hint_y: None
                    height: dp(28)
                    font_size: sp(12)
                    font_name: FONT
                    background_normal: ''
                    background_down: ''
                    background_color: T.TRANSPARENT
                    color: T.TEXT_DIM
                    on_release: root.share_diagnostics()
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
        from .._debug import first_try_log
        import time as _time
        self._pick_t0 = _time.monotonic()
        first_try_log('picker.on_enter',
                      sm_current=(self.manager.current
                                  if self.manager else None),
                      ids_ready=bool(self.ids))
        # Defer one frame: Kivy >= 2.3 fires on_enter before KV ids
        # have attached on the first screen entry, so a synchronous
        # ``self.ids.get('project_list')`` returns None and the
        # populate path bails silently — the symptom user-visible
        # was "previously cloned projects don't appear in the
        # existing-projects list". Same fix the settings UI uses.
        Clock.schedule_once(lambda *_: self._populate_projects(), 0)
        Clock.schedule_once(
            lambda *_: self._refresh_contributor_state(), 0)

    def _refresh_contributor_state(self):
        """Hide / show identity-gated actions based on whether a
        usable contributor name is set. Scope (refined 2026-05-30):

        - "Receive a project from another phone" — hidden when
          unset. ``lan_pair_accept`` refuses with
          ``CONTRIBUTOR_UNSET`` up-front, so the scan would
          silently fail; cleanest UX is to not offer it.
        - "Clone Internet Repository" — **stays visible**. Public
          repos don't require an authed user, and the clone path
          (init followed by ``git`` writes) can land without a
          contributor — only the first commit needs one, and the
          downstream sync flow surfaces ``CONTRIBUTOR_UNSET`` at
          that point. Hiding it would block legitimate public-repo
          clones.

        The contributor counts as "set" only when
        ``store.is_valid_contributor`` would return True — i.e. at
        least one alphanumeric character. Junk values like ``)``
        that satisfy a simple non-empty truthiness test (see
        0.50.20 audit) are treated as unset here so the gate
        doesn't unlock on garbage.

        Called from ``on_enter`` so a user who set their name from
        settings and came back sees the receive button re-appear
        without leaving the picker."""
        from .. import get_contributor
        try:
            contributor = get_contributor() or ''
        except Exception:
            contributor = ''
        # Treat any value lacking an alphanumeric character as
        # unset — same predicate as ``store.is_valid_contributor``
        # on the daemon side, duplicated here so the picker stays
        # platform-agnostic.
        stripped = contributor.strip()
        unset = not stripped or not any(c.isalnum() for c in stripped)
        notice = self.ids.get('contributor_notice')
        receive_btn = self.ids.get('receive_from_phone_btn')
        if notice is not None:
            if unset:
                notice.opacity = 1
                # Auto-grow to fit wrapped text. Set width first;
                # measure required height; assign.
                notice.text_size = (notice.width, None)
                notice.texture_update()
                notice.height = max(notice.texture_size[1] + dp(12),
                                    dp(48))
            else:
                notice.opacity = 0
                notice.height = 0
        if receive_btn is not None:
            if unset:
                receive_btn.opacity = 0
                receive_btn.height = 0
                receive_btn.disabled = True
            else:
                receive_btn.opacity = 1
                receive_btn.disabled = False
                # RecBtn's KV rule sets ``height: dp(52)`` —
                # match that here so the restored button is the
                # same size as the always-visible "Start New" /
                # project-list rows.
                receive_btn.height = dp(52)

    def _populate_projects(self):
        from .._debug import first_try_log
        import time as _time
        dt = _time.monotonic() - getattr(self, '_pick_t0',
                                          _time.monotonic())
        first_try_log('picker.populate_projects',
                      dt_since_enter=f'{dt:.3f}s',
                      ids_ready=bool(self.ids))
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
        first_try_log('picker.list_projects_returned',
                      n=len(projects),
                      dt_total=f'{_time.monotonic() - self._pick_t0:.3f}s')
        print(f'[picker] _populate_projects: rendering '
              f'{len(projects)} button(s)', flush=True)
        if not projects:
            return
        # Diagnostic for the intermittent picker bug (see
        # NOTES_TO_DAEMON.md history). Plain-English log lines so a
        # reader trawling logcat can answer two questions without
        # any inference:
        #
        #   1. "Which button did the user tap?"
        #      → look for the ``USER TAPPED '<langcode>'`` line.
        #   2. "What did the picker actually emit back to the peer?"
        #      → the same line ends ``emitting langcode='<X>'``.
        #
        # The daemon's own ``[recent] _touch_project(<lc>)`` lines
        # in the same logcat window are NOT the tap signal — those
        # are the previously-loaded project's unload-touch, fired
        # by whatever cleanup RPC the peer makes when switching
        # projects. The "USER TAPPED" line is the only authoritative
        # source of truth for which button received the press.
        #
        # If the BUG line below ever prints, the picker IS
        # substituting the wrong project — that's the smoking gun
        # for the NOTES_TO_DAEMON.md report.
        total = len(projects)

        def _on_release(b):
            tapped_text = getattr(b, 'text', '?')
            tapped_lc = getattr(b, 'langcode', '')
            tapped_path = getattr(b, 'lift_path', '')
            if tapped_text != tapped_lc:
                print(f"[picker] BUG: button labeled "
                      f"{tapped_text!r} has stored "
                      f"langcode={tapped_lc!r} (mismatch!) "
                      f"lift_path={tapped_path!r}",
                      flush=True)
            else:
                print(f"[picker] USER TAPPED {tapped_text!r} "
                      f"→ emitting langcode={tapped_lc!r} "
                      f"path={tapped_path!r}",
                      flush=True)
            app.load_lift(tapped_path, tapped_lc)

        for i, (name, path) in enumerate(projects, 1):
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
            print(f"[picker] button {i}/{total}: label={name!r} "
                  f"→ will emit langcode={btn.langcode!r} "
                  f"path={btn.lift_path!r}",
                  flush=True)
            btn.bind(on_release=_on_release)
            box.add_widget(btn)

    def share_diagnostics(self):
        """Picker affordance for the canonical share-diagnostics
        action. Always visible — the user we're targeting here is
        one who can't navigate past the picker, so we can't gate
        the affordance on selecting a project.

        The bundle composition + share dispatch lives in
        ``azt_collab_client.ui.share.share_diagnostics_action``
        so this surface and the daemon-settings ``Share
        diagnostics`` button are guaranteed to ship the same
        payload."""
        from .share import share_diagnostics_action
        from ..translate import tr as _tr
        share_diagnostics_action(
            on_error=lambda msg: self._show_popup(
                _tr('Diagnostics'), msg))

    def _show_popup(self, title, msg):
        """Minimal popup for share-diagnostics error feedback. The
        picker doesn't have a shared error modal (popups.py is the
        normal channel but expects a host-app reference); keeping
        this inline avoids a host-app dependency for what is
        essentially a one-shot error toast."""
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        p = Popup(title=title,
                  content=Label(text=msg or '', halign='center',
                                valign='middle'),
                  size_hint=(0.8, 0.4))
        p.open()
        return p

    def receive_from_phone(self):
        """Open the pending-offers popup with an ``on_done`` that
        emits the freshly-cloned project to the host App as if the
        user had tapped its button in the projects list.

        Without the ``on_done`` wiring (pre-0.45.43 behaviour was
        ``LAN_POPUPS.pending_offers_popup()`` with no callback) the
        popup just dismissed after a successful clone and the user
        was left on the picker with the project list still showing
        the pre-clone snapshot — they had to back out of the picker
        and come back in for the new project's button to appear.
        Both ``accept_offer`` and ``scan_to_pair`` route through
        this popup, so this wiring covers both paths.
        """
        from .lan_popups import pending_offers_popup
        from .. import S as _S

        def _on_done(result):
            # Only the cloned / re-opened branches deliver a
            # project we can pick. Anything else (decline, scan-
            # only-pair, transport failure) just dismisses the
            # popup without picking.
            if not result.has_any(
                    _S.LAN_PROJECT_CLONED,
                    _S.LAN_PROJECT_REOPENED):
                # Re-populate in case the user accepted-and-decline
                # mixed gestures changed the pending list shape.
                Clock.schedule_once(
                    lambda *_: self._populate_projects(), 0)
                return
            # Pull the langcode off the Status params (lan_clone
            # stamps both LAN_PROJECT_CLONED and LAN_PROJECT_REOPENED
            # with ``langcode=``).
            langcode = ''
            for s in result.statuses:
                if s.code in (_S.LAN_PROJECT_CLONED,
                              _S.LAN_PROJECT_REOPENED):
                    langcode = str(
                        (s.params or {}).get('langcode', '') or '')
                    if langcode:
                        break
            if not langcode:
                # Belt-and-braces: server-side ``_h_lan_accept_offer``
                # also stamps last_project on success. Fall through
                # to a refresh so the new project at least shows in
                # the list rather than dropping the user on a stale
                # snapshot.
                Clock.schedule_once(
                    lambda *_: self._populate_projects(), 0)
                return
            # Resolve the cloned project's lift_path via the
            # registry. The daemon registered it during the LAN
            # clone, so ``open_project(langcode)`` carries the path
            # in the same shape as the list-projects rows.
            from .. import open_project
            try:
                project = open_project(langcode)
            except Exception:
                project = None
            path = (getattr(project, 'lift_path', '')
                    or getattr(project, 'working_dir', '')) \
                if project is not None else ''
            app = App.get_running_app()
            if not path or not hasattr(app, 'load_lift'):
                # Host can't or won't emit — at least refresh so the
                # new project's button is visible.
                Clock.schedule_once(
                    lambda *_: self._populate_projects(), 0)
                return
            print(f"[picker] LAN clone delivered "
                  f"langcode={langcode!r} path={path!r} — "
                  f"emitting to host",
                  flush=True)
            # Host's ``load_lift`` may raise (file-system errors,
            # XML parse failure, host-side schema mismatch on a
            # fresh-clone LIFT). Without this guard the exception
            # propagates out of the Clock-scheduled finisher,
            # Kivy logs it, and the user is left on the picker
            # with the popup dismissed and no project actually
            # opened — undefined state. Wrap so a host raise
            # leaves the picker in a clean state (project list
            # refreshed so the new project's row is visible
            # even though we couldn't auto-open it).
            try:
                app.load_lift(path, langcode)
            except Exception as ex:
                print(f"[picker] host load_lift raised for "
                      f"langcode={langcode!r}: {ex!r} — falling "
                      f"back to project-list refresh",
                      flush=True)
                Clock.schedule_once(
                    lambda *_: self._populate_projects(), 0)

        pending_offers_popup(on_done=_on_done)
