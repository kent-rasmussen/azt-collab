"""Shared Kivy popups for sister-app flows.

Currently provides the clone-from-URL prompt and the install-prompt
that fires when a peer can't reach the server APK on Android. Later
picker steps will move template-confirm and other modals here too.
Translations route through ``azt_collab_client.translate.tr``; theme
lives alongside this module at ``azt_collab_client.ui.theme``.
"""

from . import theme

from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput

from ..translate import tr as _tr


def confirm_langcode_popup(initial, on_submit, font_name='Roboto'):
    """Show a popup that displays the project's auto-derived langcode
    in an editable field and asks the user to confirm or correct it.

    Used by the picker's clone and open-file flows. The daemon
    derives a langcode from the LIFT filename / repo URL, but it
    isn't always right — a clone of ``foo.git`` whose LIFT file is
    ``en-x-pilot.lift`` derives ``en-x-pilot``, but the user may
    actually be working on a different vernacular and wants the
    project keyed differently in ``projects.json``. The "Start
    new" flow already collects an explicit langcode via the BCP-47
    picker; clone and open-file get this lighter-touch confirm.

    ``on_submit(confirmed_langcode)`` fires when the user taps
    Confirm. Cancel falls back to ``on_submit(initial)`` so the
    flow always resolves — empty langcode is invalid (the picker
    needs one to stamp the result Intent), so we never call back
    with an empty string."""
    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))
    content.add_widget(Label(
        text=_tr('Project language code'),
        size_hint_y=None, height=dp(28),
        font_size=sp(14), bold=True, color=theme.ACCENT,
        font_name=font_name,
    ))
    content.add_widget(Label(
        text=_tr(
            "Confirm or edit. The daemon derived this from the file "
            "or URL — change it if it isn't right for your project."),
        size_hint_y=None, height=dp(40),
        font_size=sp(12), color=theme.TEXT_DIM, font_name=font_name,
        text_size=(None, None), halign='left', valign='top',
    ))
    code_input = TextInput(
        text=initial,
        hint_text=_tr('e.g. en-x-pilot'),
        multiline=False, size_hint_y=None, height=dp(48),
        font_size=sp(14), font_name=font_name,
    )
    content.add_widget(code_input)

    btn_row = BoxLayout(
        size_hint_y=None, height=dp(48), spacing=dp(12))
    cancel_btn = Button(
        text=_tr('Cancel'), font_size=sp(14), font_name=font_name)
    confirm_btn = Button(
        text=_tr('Confirm'), font_size=sp(14), font_name=font_name,
        background_color=theme.ACCENT)
    btn_row.add_widget(cancel_btn)
    btn_row.add_widget(confirm_btn)
    content.add_widget(btn_row)

    popup = Popup(
        title=_tr('Confirm project language code'),
        content=content,
        size_hint=(0.9, None), height=dp(280),
        auto_dismiss=False,
    )

    def _confirm(*_):
        chosen = code_input.text.strip() or initial
        popup.dismiss()
        try:
            on_submit(chosen)
        except Exception as ex:
            print(f'[confirm_langcode_popup] on_submit raised: {ex}')

    def _cancel(*_):
        popup.dismiss()
        try:
            on_submit(initial)
        except Exception as ex:
            print(f'[confirm_langcode_popup] on_submit raised: {ex}')

    cancel_btn.bind(on_release=_cancel)
    confirm_btn.bind(on_release=_confirm)
    popup.open()
    return popup


def grant_collaborator_popup(langcode, on_done=None, font_name='Roboto'):
    """Popup for inviting a GitHub user as a collaborator on the repo
    backing ``langcode``. Project disambiguation is the load-bearing
    piece of the UX: the popup looks the project up via
    ``project_status(langcode)`` and shows the langcode + remote URL
    prominently so the user can confirm they're acting on the right
    repo before tapping Invite.

    Peer integration shape (recorder / viewer / future peers):

        from azt_collab_client.ui import grant_collaborator_popup
        grant_collaborator_popup(
            langcode=self._current_langcode,
            font_name=_FONT_NAME,
        )

    The button that opens this popup belongs in the peer's per-
    project settings surface — *not* a global setting — because the
    operation is meaningless without a specific project context.

    On success / "already a collaborator" the popup auto-dismisses
    after 2 s so the user sees the confirmation message; on errors
    it stays up for retry. ``on_done(result)`` fires after the
    popup dismisses (success path only) so the host can refresh
    any local UI that displays collaborators.

    Returns the Popup object."""
    from kivy.clock import Clock
    from .. import grant_collaborator, project_status, S
    from ..translate import translate_result

    ps = project_status(langcode)
    remote_url = (ps.remote_url if ps else '') or ''

    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))

    # Project disambiguation header — make it impossible for the
    # user to misread which repo they're acting on. The popup's
    # title bar already says "Invite collaborator"; we just need
    # to show *which* project. Bold langcode + dim remote URL.
    proj_label = Label(
        text=str(langcode),
        size_hint_y=None, height=dp(28),
        font_size=sp(15), bold=True, color=theme.ACCENT,
        font_name=font_name,
        halign='left', valign='middle',
    )
    proj_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
    content.add_widget(proj_label)
    url_label = Label(
        text=remote_url or _tr('(no remote configured)'),
        size_hint_y=None, height=dp(20),
        font_size=sp(11), color=theme.TEXT_DIM, font_name=font_name,
        halign='left', valign='middle',
    )
    url_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
    content.add_widget(url_label)

    content.add_widget(Label(
        text=_tr("Enter the collaborator's GitHub username:"),
        size_hint_y=None, height=dp(24),
        font_size=sp(12), color=theme.TEXT_DIM, font_name=font_name,
        halign='left', valign='middle',
    ))
    user_input = TextInput(
        text='',
        hint_text=_tr('e.g. octocat'),
        multiline=False, size_hint_y=None, height=dp(48),
        font_size=sp(14), font_name=font_name,
    )
    content.add_widget(user_input)

    # Status / outcome line. Reused for both errors and the
    # transient success message so the user always has one place
    # to look.
    status_label = Label(
        text='',
        size_hint_y=None, height=dp(60),
        font_size=sp(12), color=theme.TEXT, font_name=font_name,
        halign='left', valign='top',
    )
    status_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
    content.add_widget(status_label)

    btn_row = BoxLayout(
        size_hint_y=None, height=dp(48), spacing=dp(12))
    cancel_btn = Button(
        text=_tr('Cancel'), font_size=sp(14), font_name=font_name)
    invite_btn = Button(
        text=_tr('Invite'), font_size=sp(14), font_name=font_name,
        background_color=theme.ACCENT)
    btn_row.add_widget(cancel_btn)
    btn_row.add_widget(invite_btn)
    content.add_widget(btn_row)

    popup = Popup(
        title=_tr('Invite collaborator'),
        content=content,
        size_hint=(0.9, None), height=dp(360),
        auto_dismiss=False,
    )

    def _set_status(text, error=False):
        status_label.text = text or ''
        status_label.color = (theme.RED if error else theme.TEXT)

    def _do_invite(*_):
        username = user_input.text.strip()
        if not username:
            _set_status(_tr('Enter a GitHub username.'), error=True)
            return
        invite_btn.disabled = True
        cancel_btn.disabled = True
        _set_status(_tr('Sending invitation…'))

        def _worker(_dt):
            # Run the RPC on a Clock callback (small) — the call is
            # blocking but typically sub-second; the disabled
            # buttons + "Sending…" status cover the wait visually.
            try:
                result = grant_collaborator(langcode, username)
            except Exception as ex:
                _set_status(_tr(
                    'Invite failed: {error}'
                ).format(error=ex), error=True)
                invite_btn.disabled = False
                cancel_btn.disabled = False
                return
            text = translate_result(result) or _tr('Done.')
            success = result.has_any(
                S.COLLABORATOR_INVITED, S.COLLABORATOR_ALREADY)
            _set_status(text, error=not success)
            if success:
                # Brief pause so the user sees the confirmation,
                # then dismiss + fire on_done. Peer can refresh
                # any collaborator-list UI from on_done.
                def _finish(_dt2):
                    try:
                        popup.dismiss()
                    except Exception:
                        pass
                    if on_done is not None:
                        try:
                            on_done(result)
                        except Exception as ex:
                            print('[grant_collaborator_popup] on_done '
                                  f'raised: {ex}')
                Clock.schedule_once(_finish, 2.0)
            else:
                invite_btn.disabled = False
                cancel_btn.disabled = False

        Clock.schedule_once(_worker, 0)

    cancel_btn.bind(on_release=lambda *_: popup.dismiss())
    invite_btn.bind(on_release=_do_invite)
    popup.open()
    return popup


def _derive_langcode_from_url(url):
    """Cheap default langcode for a clone URL: repo basename minus
    ``.git``. Mirrors the daemon's ``projects.derive_langcode`` URL
    branch so the displayed default matches what the daemon would
    pick on its own."""
    name = (url or '').rstrip('/').split('/')[-1]
    if name.endswith('.git'):
        name = name[:-4]
    return name


def clone_url_popup(on_submit, font_name='Roboto'):
    """Show a popup asking for a git repository URL. Calls
    ``on_submit(clone_url, langcode, vernlang)`` when the user
    submits.

    Since 0.45.0 the popup distinguishes two daemon-owned values
    that used to be conflated:

      - ``langcode`` is the **project name**, auto-derived from
        the github repo slug (the part of the URL after the last
        ``/``, with any ``.git`` suffix stripped). Not user-
        editable here — repo renames happen on github, not in
        this app.
      - ``vernlang`` is the **linguistic language code** the LIFT
        will tag entries with (``<form lang="…">``). User-
        supplied via the "change" affordance described below.
        Defaults to the repo slug as a starting hint when the
        user hasn't said otherwise.

    Two mutually-exclusive edit modes (only one input is active
    at a time, so the on-screen keyboard never argues with
    itself):

    Mode A — URL active (default):
        ┌─ project: <slug> · language: <vernlang>   [change] ┐
        ├─ URL TextInput (active, auto-focus)                ┤

    Mode B — language active (after tapping *change*):
        ┌─ TextInput (language, auto-focus)              [OK] ┐
        ├─ URL TextInput (disabled)                           ┤

    Tapping **change** captures the current language hint into
    the editable field and disables URL editing. Tapping **OK**
    commits the typed language code and swaps Mode A back in;
    the URL field re-enables. Submit (Clone) works in either
    mode."""
    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))
    content.add_widget(Label(
        text=_tr('Clone a git repository containing a LIFT file:'),
        size_hint_y=None, height=dp(30),
        font_size=sp(13), color=theme.TEXT, font_name=font_name,
    ))

    # Mode-A widgets: read-only "project · language" preview +
    # the "change" button that drops into Mode B for editing the
    # language code. The project name auto-derives from the URL
    # slug and is NOT editable here.
    code_label = Label(
        text=_tr('project: ') + '— · ' + _tr('language: ') + '—',
        size_hint_x=1,
        font_size=sp(13), color=theme.TEXT_DIM, font_name=font_name,
        halign='left', valign='middle',
    )
    code_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
    change_btn = Button(
        text=_tr('change'),
        size_hint_x=None, width=dp(100),
        font_size=sp(12), font_name=font_name,
    )
    # Mode-B widgets (editable language code + OK).
    code_input = TextInput(
        text='',
        hint_text=_tr('e.g. en, fra, sw-x-pilot'),
        multiline=False,
        size_hint_x=1, size_hint_y=None, height=dp(40),
        font_size=sp(14), font_name=font_name,
    )
    ok_btn = Button(
        text=_tr('OK'),
        size_hint_x=None, width=dp(80),
        font_size=sp(13), font_name=font_name,
        background_color=theme.ACCENT,
    )

    code_row = BoxLayout(
        orientation='horizontal',
        size_hint_y=None, height=dp(40), spacing=dp(8))
    code_row.add_widget(code_label)
    code_row.add_widget(change_btn)
    content.add_widget(code_row)

    # Pre-populate the host prefix so users on a phone keyboard
    # only have to type the ``owner/repo`` segment instead of the
    # full ``https://github.com/`` URL — a common SIL-field paper-
    # cut. Cursor lands at end of text via the on-open hook below
    # so typing immediately appends. Pasting a full URL works
    # too: the user can long-press → Paste over the prefix or
    # select-all and overwrite it.
    _CLONE_URL_PREFIX = 'https://github.com/'
    # On Android with zxing-android-embedded bundled, a "Scan QR"
    # button sits next to the URL input — the user can flash the
    # other device's "Share repo" QR (rendered by the daemon UI's
    # ProjectScreen) and have the URL pre-filled. On desktop /
    # Android-without-ZXing, the button is hidden and the user
    # pastes the URL as before. ``qr_scan.available()`` is the
    # gate.
    from . import qr_scan as _qr_scan
    _qr_available = _qr_scan.available()

    if _qr_available:
        url_row = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(48), spacing=dp(8))
        url_input = TextInput(
            text=_CLONE_URL_PREFIX,
            hint_text=_tr('owner/repository'),
            multiline=False, size_hint_y=None, height=dp(48),
            font_size=sp(14), font_name=font_name,
            size_hint_x=1,
        )
        scan_btn = Button(
            text=_tr('Scan QR'),
            size_hint_x=None, width=dp(110),
            size_hint_y=None, height=dp(48),
            font_size=sp(13), font_name=font_name,
            background_color=theme.ACCENT,
        )
        url_row.add_widget(url_input)
        url_row.add_widget(scan_btn)
        content.add_widget(url_row)
    else:
        url_input = TextInput(
            text=_CLONE_URL_PREFIX,
            hint_text=_tr('owner/repository'),
            multiline=False, size_hint_y=None, height=dp(48),
            font_size=sp(14), font_name=font_name,
        )
        scan_btn = None
        content.add_widget(url_input)

    btn_row = BoxLayout(
        size_hint_y=None, height=dp(48), spacing=dp(12))
    cancel_btn = Button(
        text=_tr('Cancel'), font_size=sp(14), font_name=font_name)
    clone_btn = Button(
        text=_tr('Clone'), font_size=sp(14), font_name=font_name,
        background_color=theme.ACCENT)
    btn_row.add_widget(cancel_btn)
    btn_row.add_widget(clone_btn)
    content.add_widget(btn_row)

    popup = Popup(
        title=_tr('Clone Repository'),
        content=content,
        # Slightly taller when the Scan button row adds vertical
        # weight on Android. On desktop the original height is
        # fine.
        size_hint=(0.9, None),
        height=dp(290) if scan_btn is None else dp(308),
        auto_dismiss=True,
    )

    state = {
        'mode': 'A',           # 'A' = URL active, 'B' = code active
        'overridden': False,   # user has typed an explicit vernlang
                               # at least once; URL changes stop
                               # syncing the language hint
        'user_vernlang': '',
    }

    def _project_name_from_url(url):
        return _derive_langcode_from_url(url) or '—'

    def _refresh_label_from_url(*_):
        if state['mode'] == 'B':
            return
        proj = _project_name_from_url(url_input.text)
        if state['overridden']:
            vern = state['user_vernlang']
        else:
            # Pre-clone the language hint defaults to the same
            # slug as the project name. It's just a starting
            # value the user can change — the daemon validates
            # against actual LIFT contents after clone.
            vern = proj if proj != '—' else '—'
        code_label.text = (_tr('project: ') + proj + ' · '
                           + _tr('language: ') + vern)

    def _enter_mode_b(*_):
        # Seed the edit field with the current language hint (the
        # user's prior typed value if any, else the slug). The
        # project name is not editable — only the language code.
        if state['overridden']:
            seed = state['user_vernlang']
        else:
            seed = _derive_langcode_from_url(url_input.text)
        code_input.text = seed
        code_row.clear_widgets()
        code_row.add_widget(code_input)
        code_row.add_widget(ok_btn)
        url_input.disabled = True
        state['mode'] = 'B'
        code_input.focus = True

    def _enter_mode_a(*_):
        # Commit whatever the user typed as the chosen vernlang.
        # Empty falls back to "no override" — the label resyncs
        # to the URL-derived slug as the hint.
        typed = code_input.text.strip()
        if typed:
            state['user_vernlang'] = typed
            state['overridden'] = True
        else:
            state['overridden'] = False
        code_row.clear_widgets()
        code_row.add_widget(code_label)
        code_row.add_widget(change_btn)
        url_input.disabled = False
        state['mode'] = 'A'
        url_input.focus = True
        _refresh_label_from_url()

    def _resolve_pair():
        """Return ``(langcode, vernlang)`` to pass to on_submit.
        ``langcode`` is always the URL-derived project slug;
        ``vernlang`` is the user's typed value (Mode B), the
        previously-committed override, or — failing both — the
        slug as a starting default."""
        slug = _derive_langcode_from_url(url_input.text)
        if state['mode'] == 'B':
            typed = code_input.text.strip()
            return slug, (typed or slug)
        if state['overridden']:
            return slug, state['user_vernlang']
        return slug, slug

    def _do_clone(*_args):
        clone_url = url_input.text.strip()
        # Empty / prefix-only — user opened the popup, didn't type
        # anything actionable. Dismiss without firing on_submit.
        if not clone_url or clone_url == _CLONE_URL_PREFIX:
            popup.dismiss()
            return
        # Handle prefix-doubling: if the user pasted a full URL
        # *after* the pre-populated prefix instead of selecting +
        # overwriting, the field reads
        # ``https://github.com/https://github.com/owner/repo``.
        # Take the rightmost protocol marker as the real start.
        for marker in ('https://', 'http://', 'git@'):
            idx = clone_url.rfind(marker)
            if idx > 0:
                clone_url = clone_url[idx:]
                break
        popup.dismiss()
        if not clone_url.endswith('.git'):
            clone_url += '.git'
        langcode, vernlang = _resolve_pair()
        try:
            on_submit(clone_url, langcode, vernlang)
        except TypeError:
            # Back-compat: older host code passes a 2-arg callback.
            # Pass the vernlang as the legacy single "code" so the
            # old conflation still works (the daemon will register
            # with langcode=vernlang as before). New host code
            # should accept three args.
            try:
                on_submit(clone_url, vernlang)
            except Exception as ex:
                print(f'[clone_url_popup] on_submit raised: {ex}')
        except Exception as ex:
            print(f'[clone_url_popup] on_submit raised: {ex}')

    url_input.bind(text=_refresh_label_from_url)
    change_btn.bind(on_release=_enter_mode_b)
    ok_btn.bind(on_release=_enter_mode_a)
    cancel_btn.bind(on_release=popup.dismiss)
    clone_btn.bind(on_release=_do_clone)

    if scan_btn is not None:
        def _on_scan_result(scanned_text):
            # Trust the user's eyes: they pointed the camera at a
            # QR the daemon UI generated, so the payload is the
            # clone URL. We replace the textbox content entirely
            # (no string-massaging) and let the existing
            # ``_refresh_label_from_url`` derive the langcode from
            # the new value.
            url_input.text = (scanned_text or '').strip()
            # Bring focus back to the URL so the user can edit /
            # confirm before tapping Clone — same UX as a manual
            # paste leaving the cursor in the field.
            url_input.cursor = (len(url_input.text), 0)
            url_input.focus = True

        def _on_scan_cancel():
            # No-op — popup stays as-is, user can retry or paste
            # manually. Logged on the qr_scan side already.
            pass

        def _on_scan_tap(*_):
            # Diagnostic: confirm the button is wired through.
            # The 0.41.0 "scan button does nothing, no logcat"
            # report (NOTES_TO_DAEMON.md) had several candidate
            # causes; this print + the entry log in
            # ``qr_scan.scan_qr`` close the diagnostic gap so a
            # future regression is visible from logcat alone.
            import sys
            print('[clone_url_popup] Scan QR tapped',
                  file=sys.stderr, flush=True)
            # Exceptions inside Kivy event handlers are caught
            # higher up but can be swallowed silently depending
            # on Kivy version. Wrap so we always see *something*
            # in logcat if scan_qr blows up.
            try:
                _qr_scan.scan_qr(
                    on_result=_on_scan_result,
                    on_cancel=_on_scan_cancel,
                    prompt=_tr('Point camera at a repo QR code'),
                )
            except Exception as ex:
                print(f'[clone_url_popup] scan_qr raised: '
                      f'{type(ex).__name__}: {ex}',
                      file=sys.stderr, flush=True)
                # Fall through silently — user can paste URL.

        scan_btn.bind(on_release=_on_scan_tap)

    # Position the cursor at the end of the pre-populated prefix
    # so typing immediately appends (the common case). Schedule on
    # the next frame so Kivy's TextInput initialisation completes
    # first; setting cursor in the constructor doesn't stick.
    def _focus_url(_dt):
        url_input.cursor = (len(url_input.text), 0)
        url_input.focus = True
    from kivy.clock import Clock as _Clock
    _Clock.schedule_once(_focus_url, 0)

    popup.open()
    return popup


def install_server_apk_popup(on_status=None, font_name='Roboto',
                             body_message=None,
                             current_server_version='0.0.0',
                             install_target_package=None,
                             install_label=None,
                             title=None,
                             on_install_complete=None,
                             direct_url=None,
                             asset_filename=None,
                             open_page_url=None,
                             dismiss_label=None,
                             dismiss_action='quit',
                             on_retry=None,
                             on_open_app=None,
                             open_app_label=None,
                             on_restart_server=None,
                             repo=None):
    """Single canonical popup for "the suite needs the server APK
    (or a newer one) before this app can do anything useful". Used
    for both the server-missing case and the server-too-old case
    by varying ``body_message`` + ``current_server_version``.

    Three buttons:

    - **Quit** — closes the running peer app via
      ``App.get_running_app().stop()``. Without the server APK the
      peer can't function (no daemon → no sync, no project picker),
      so dismissing without installing == quitting. The button
      label is "Quit {app}" where ``{app}`` comes from the running
      App's ``title`` (falls through to the literal "Quit" if the
      host hasn't set a title).
    - **Open install page** — opens ``SERVER_APK_INSTALL_URL`` in
      the browser as a fallback for environments where the in-app
      install Intent can't fire (locked-down corporate Android,
      rooted ROM, etc.). Most users won't need it.
    - **Install** — runs ``check_for_update`` against the suite's
      release feed: downloads the latest ``aztcollab.apk``, streams
      it to ``$AZT_HOME/updates/``, dispatches Android's system
      installer, then polls ``PackageManager.getPackageInfo`` for
      install completion (status flips to "Installed." when the
      new package is detected).

    Progress strings flow into the popup body label *and* (if
    ``on_status`` is given) the host's status sink — the popup body
    is the user-visible surface of record because the host's
    progress label might not be on-screen.

    Parameters
    ----------
    on_status : callable(str) | None
        Optional sink for progress / state strings, in addition to
        the popup body label. Hosts wire this into their existing
        log surface so progress is visible there too.
    font_name : str
        CharisSIL or Roboto, per the host's ``register_charis()``.
    body_message : str | None
        Override the default "the AZT collaboration service is not
        installed" body. Used by the bootstrap workflow's
        server-too-old path to swap in a different lead message
        ("a newer version is required") while keeping the same
        button shape.
    current_server_version : str
        Passed through to ``check_for_update`` as ``current_version``.
        ``'0.0.0'`` (default) for the server-missing case forces
        "newer found" against any release. For the server-too-old
        case, pass the daemon's actual version so a no-op release
        feed reports "up to date" instead of double-installing.
    install_target_package : str | None
        Android package name to poll for install completion.
        Defaults to ``'org.atoznback.aztcollab'`` (the canonical
        server APK package); override only if pointing at a fork.
    install_label : str | None
        Override the "Install" button label (e.g. "Update" for the
        server-too-old path).
    title : str | None
        Override the popup title bar.
    on_install_complete : callable() | None
        Optional callback fired once the polling watchdog confirms
        the target package's version flipped to the just-installed
        one. The popup itself dismisses ~1s after the watchdog
        fires (so the user sees "Installed." briefly), then invokes
        the callback. Bootstrap passes a callback that re-runs its
        compat check; without it, the popup stays open showing
        "Installed." and the user has to tap Quit then relaunch.
    direct_url : str | None
        Override the download URL. By default we compose
        ``https://github.com/<server-repo>/releases/latest/download/<server-asset>``
        from the bootstrap constants — appropriate for the server
        install / update case. Peer self-update passes its own
        composed URL for the peer's APK. (When provided, also
        provide ``asset_filename`` so the file lands on disk with
        the right name.)
    asset_filename : str | None
        APK filename used for both the on-disk staging path and the
        MediaStore display name. Defaults to the server APK's
        canonical ``aztcollab.apk``. Self-update passes the peer's
        own asset filename.
    open_page_url : str | None
        Override the URL the "Open install page" button opens.
        Defaults to ``SERVER_APK_INSTALL_URL`` (the server's
        release page). Self-update passes the peer's release page
        so the user can browse release notes for the peer.
    dismiss_label : str | None
        Override the dismiss button label. Defaults to
        ``"Quit {app}"`` for the server case. Self-update passes
        ``"Not now"`` (and pairs with ``dismiss_action='dismiss'``).
    dismiss_action : str
        ``'quit'`` (default): tapping dismiss closes the popup AND
        the host app. Right for the server case where the peer
        can't function without the daemon. ``'dismiss'``: just
        closes the popup, peer keeps running. Right for self-
        update where declining means "stick with the current
        version".
    on_restart_server : callable() | None
        When set, render an extra "Restart server" button that
        invokes the callback. Used by the server-too-old branch
        so the user can ask the daemon to exit (cooperative
        ``/v1/admin/restart``) without downloading a fresh APK —
        Android's ContentProvider auto-spawn revives the daemon
        on the next peer call, which on the typical "I already
        installed the new APK but the old :provider process is
        still alive" case results in the newer code loading.
        Callback is responsible for showing progress and re-
        running the compat check; the popup just dismisses on
        invocation.

    Returns the Popup so callers can hold a ref. ``auto_dismiss``
    is False — the user must explicitly choose Quit / Open page /
    Install. That's the modal-blocking behaviour the suite UX
    contract wants ("user can't reach settings or picker if there's
    no server").
    """
    from .. import SERVER_APK_INSTALL_URL
    from .bootstrap import (
        _SERVER_REPO_DEFAULT, _SERVER_ASSET_DEFAULT,
        _SERVER_PACKAGE_NAME,
    )

    # Resolve the URL / asset / target-package / open-page-url
    # defaults to the server APK if the caller didn't override.
    # Self-update flow overrides every one with peer-specific
    # values.
    if asset_filename is None:
        asset_filename = _SERVER_ASSET_DEFAULT
    if direct_url is None:
        direct_url = (
            f'https://github.com/{_SERVER_REPO_DEFAULT}/'
            f'releases/latest/download/{_SERVER_ASSET_DEFAULT}'
        )
    if open_page_url is None:
        open_page_url = SERVER_APK_INSTALL_URL
    # ``install_target_package=''`` (empty string, distinguishable
    # from None) means "explicitly no polling" — used by self-
    # update where the install kills the running peer process and
    # polling our own package would block forever. ``None``
    # (default) means "use the server APK package".
    if install_target_package is None:
        install_target_package = _SERVER_PACKAGE_NAME
    elif install_target_package == '':
        install_target_package = None

    default_body = (
        _tr('The AZT collaboration service (server APK) is not installed.')
        + '\n\n'
        + _tr('Tap Install to download and install it. Android will '
              'ask you to confirm before the install starts.')
    )
    msg = body_message or default_body

    # Dismiss-button label. Default for the server case is
    # "Quit {app}" because the peer can't function without the
    # daemon. Self-update passes its own ``dismiss_label`` (and
    # ``dismiss_action='dismiss'``) so the button reads "Not now"
    # and just closes the popup.
    if dismiss_label is None:
        try:
            from kivy.app import App
            app = App.get_running_app()
            app_title = getattr(app, 'title', '') or ''
        except Exception:
            app_title = ''
        dismiss_label = (_tr('Quit {app}').format(app=app_title)
                         if app_title else _tr('Quit'))

    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))

    # Language toggle row at the top — subtle, but discoverable. On
    # first install the user has no other way to switch language
    # (settings UI lives behind this very popup). Tap a code →
    # ``set_language`` → dismiss and re-invoke this popup with the
    # same args so all translated strings refresh. Only shown when
    # there's more than one language available.
    from .. import i18n as _i18n
    available = _i18n.available_languages()
    current_lang = _i18n.current_language()

    # Snapshot kwargs so the language toggle can re-invoke us. We
    # store the *resolved* values (after defaults applied) so the
    # rebuild produces an identical popup in every dimension except
    # language.
    _relaunch_kwargs = dict(
        on_status=on_status,
        font_name=font_name,
        body_message=body_message,
        current_server_version=current_server_version,
        install_target_package=install_target_package or '',
        install_label=install_label,
        title=title,
        on_install_complete=on_install_complete,
        direct_url=direct_url,
        asset_filename=asset_filename,
        open_page_url=open_page_url,
        dismiss_label=dismiss_label,
        dismiss_action=dismiss_action,
        on_restart_server=on_restart_server,
    )

    if len(available) > 1:
        lang_row = BoxLayout(
            orientation='horizontal',
            size_hint_y=None, height=dp(24),
            spacing=dp(8))
        # Stretch on the left so the language buttons sit at the
        # right (subtle: doesn't compete with the main message).
        lang_row.add_widget(Label(size_hint_x=1, text=''))

        def _make_lang_btn(code, display, is_current):
            lb = Button(
                text=display,
                size_hint_x=None, width=dp(80),
                size_hint_y=None, height=dp(24),
                font_size=sp(11), font_name=font_name,
                halign='center', valign='middle',
                bold=is_current,
                color=((1, 1, 1, 1) if is_current
                       else theme.TEXT_DIM),
                background_color=(theme.ACCENT if is_current
                                  else theme.TRANSPARENT),
                background_normal='',
            )
            if not is_current:
                def _switch(_btn, c=code):
                    # Defer the dismiss + relaunch via Clock so the
                    # current button's on_release returns first.
                    # Otherwise we re-enter Popup machinery from
                    # inside its own touch handler, which Kivy
                    # silently no-ops in some versions.
                    import sys as _sys
                    print(f'[install_popup] language switch: {c}',
                          file=_sys.stderr, flush=True)

                    def _do_relaunch(_dt):
                        try:
                            popup.dismiss()
                        except Exception as ex:
                            print(f'[install_popup] dismiss raised: '
                                  f'{ex}', file=_sys.stderr, flush=True)
                        try:
                            _i18n.set_language(c)
                        except Exception as ex:
                            print(f'[install_popup] set_language raised:'
                                  f' {ex}', file=_sys.stderr, flush=True)
                        try:
                            install_server_apk_popup(**_relaunch_kwargs)
                        except Exception as ex:
                            print(f'[install_popup] relaunch raised: '
                                  f'{ex}', file=_sys.stderr, flush=True)
                    from kivy.clock import Clock as _Clock
                    _Clock.schedule_once(_do_relaunch, 0)
                lb.bind(on_release=_switch)
            return lb

        for code, display in available:
            lang_row.add_widget(_make_lang_btn(
                code, display, code == current_lang))
        content.add_widget(lang_row)

    body_label = Label(
        text=msg, halign='left', valign='top',
        font_size=sp(13), color=theme.TEXT, font_name=font_name,
    )
    body_label.bind(width=lambda w, val: setattr(
        w, 'text_size', (val, None)))
    content.add_widget(body_label)
    # Dedicated status line for transient updates ("Downloading…",
    # "Tap install again to confirm", "Install failed: …"). Visually
    # distinct from ``body_label`` (larger, ACCENT colour, bold) so
    # a fresh message reads as the most-current call-to-action
    # rather than vanishing into the wall of explanatory text above.
    status_label = Label(
        text='', halign='left', valign='top',
        font_size=sp(15), color=theme.ACCENT, font_name=font_name,
        bold=True, size_hint_y=None,
    )
    status_label.bind(
        width=lambda w, val: setattr(w, 'text_size', (val, None)),
        texture_size=lambda w, ts: setattr(w, 'height', ts[1]))
    content.add_widget(status_label)
    # Tall enough to hold two lines of wrapped button text on
    # narrow screens — "Open install page" and "Quit AZT Recorder"
    # both want to wrap. Without text_size binding (below) Kivy
    # Buttons just clip long labels.
    btn_row = BoxLayout(
        size_hint_y=None, height=dp(60), spacing=dp(8))
    install_btn = Button(
        text=install_label or _tr('Install'),
        font_size=sp(14), font_name=font_name,
        halign='center', valign='middle',
        background_color=theme.ACCENT,
    )
    open_page_btn = Button(
        text=_tr('More info'),
        font_size=sp(13), font_name=font_name,
        halign='center', valign='middle',
    )
    quit_btn = Button(
        text=dismiss_label,
        font_size=sp(14), font_name=font_name,
        halign='center', valign='middle',
    )
    # Optional Retry button — only shown when caller passes
    # ``on_retry``. Used by the unresponsive-server flow so the
    # user can wait longer than the 60s budget without having to
    # tap Install (which would download fresh) or Quit. Tap →
    # popup dismisses → caller's ``on_retry`` re-runs whatever
    # check fired the popup originally.
    retry_btn = None
    if on_retry is not None:
        retry_btn = Button(
            text=_tr('Try again'),
            font_size=sp(13), font_name=font_name,
            halign='center', valign='middle',
        )

    # Optional "Open <server app>" button — Android-15 process-
    # freezer workaround. The :provider process gets frozen by the
    # OS while the peer is foregrounded, so peer-driven lazy-spawn
    # times out. Launching the server APK's launcher activity
    # un-freezes the package's processes. Caller wires
    # ``on_open_app`` to a function that fires the launch Intent +
    # restarts the compat-probe retry cycle.
    open_app_btn = None
    if on_open_app is not None:
        open_app_btn = Button(
            text=open_app_label or _tr('Open AZT Collaboration'),
            font_size=sp(13), font_name=font_name,
            halign='center', valign='middle',
        )

    # Optional "Restart server" button — server-too-old workflow.
    # When the user has already installed the new server APK but
    # Android kept the old daemon process alive, cooperative
    # /v1/admin/restart lets it exit so the next ContentProvider
    # call lazy-spawns the new code. Cheaper than re-downloading
    # the APK.
    restart_btn = None
    if on_restart_server is not None:
        restart_btn = Button(
            text=_tr('Restart server'),
            font_size=sp(13), font_name=font_name,
            halign='center', valign='middle',
        )

    # Bind text_size to size so labels wrap inside their button
    # bounds rather than spilling / clipping.
    def _bind_wrap(b):
        b.bind(size=lambda w, _v: setattr(w, 'text_size', w.size))
    for b in (install_btn, open_page_btn, quit_btn):
        _bind_wrap(b)
    if retry_btn is not None:
        _bind_wrap(retry_btn)
    if open_app_btn is not None:
        _bind_wrap(open_app_btn)
    if restart_btn is not None:
        _bind_wrap(restart_btn)

    btn_row.add_widget(quit_btn)
    if retry_btn is not None:
        btn_row.add_widget(retry_btn)
    if open_app_btn is not None:
        btn_row.add_widget(open_app_btn)
    if restart_btn is not None:
        btn_row.add_widget(restart_btn)
    btn_row.add_widget(install_btn)
    btn_row.add_widget(open_page_btn)
    content.add_widget(btn_row)

    # Version footer — discrete, dim, helps diagnose which client
    # build is actually live when reproducing UI bugs across
    # versions. Mirror of the daemon settings UI's version strip.
    from .. import __version__ as _client_version
    content.add_widget(Label(
        text=f'client {_client_version}',
        size_hint_y=None, height=dp(18),
        font_size=sp(11), font_name=font_name,
        color=theme.TEXT_DIM,
        halign='center', valign='middle',
    ))

    popup = Popup(
        title=title or _tr('AZT collaboration service required'),
        content=content,
        size_hint=(0.9, None), height=dp(360),
        auto_dismiss=False,
    )

    def _route_status(text):
        # Surface progress on the dedicated status_label below the
        # main message. body_label keeps the original explanatory
        # ``msg`` unchanged so the user retains the "what's this
        # about" anchor; status_label carries the current call-to-
        # action ("Downloading…", "Tap install again to confirm",
        # …) in the ACCENT bold style so it doesn't get lost in
        # the wall of text above.
        status_label.text = text or ''
        if on_status:
            try:
                on_status(text)
            except Exception:
                pass

    def _do_install(*_):
        # Direct-URL download path: fetch the well-known
        # ``releases/latest/download/<asset>`` redirect rather than
        # going through the GitHub API (which tripped over edge
        # cases — asset-name mismatch, listing endpoint quirks,
        # etc.). No version comparison, but the calling flow has
        # already decided we want to install (server missing /
        # server too old / peer self-update accepted).
        install_btn.disabled = True
        open_page_btn.disabled = True
        from .update import install_apk_from_url
        from kivy.clock import Clock

        def _on_error(err):
            _route_status(_tr('Install failed: {error}').format(error=err))
            install_btn.disabled = False
            open_page_btn.disabled = False

        def _on_user_action_needed():
            # The install path stalled because the user has to flip
            # "Install unknown apps" on for this peer in Android
            # Settings. The status message tells them what to do;
            # re-enable the buttons so the user can come back and
            # retry without restarting the popup.
            install_btn.disabled = False
            open_page_btn.disabled = False

        def _on_complete():
            # Polling confirmed the new server version is live. Show
            # "Installed." briefly so the user sees what happened,
            # then dismiss the popup and call the host's
            # continuation (typically: re-run bootstrap's compat
            # check so normal startup proceeds).
            def _finish(_dt):
                try:
                    popup.dismiss()
                except Exception:
                    pass
                if on_install_complete is not None:
                    try:
                        on_install_complete()
                    except Exception as ex:
                        print('[install_popup] on_install_complete '
                              f'raised: {ex}')
            Clock.schedule_once(_finish, 1.0)

        install_apk_from_url(
            url=direct_url,
            asset_filename=asset_filename,
            on_status=_route_status,
            on_error=_on_error,
            on_user_action_needed=_on_user_action_needed,
            install_target_package=install_target_package,
            install_label=install_label or _tr('Install'),
            on_install_complete=_on_complete,
            # Forwarded so install_apk_from_url can fetch the
            # release's authoritative ``asset.digest`` for cache
            # validation. Without this, a stale cached APK from
            # a previous Update cycle is silently reused.
            repo=repo,
        )

    def _open_page(*_):
        try:
            from kivy.utils import platform
        except Exception:
            platform = ''
        try:
            if platform == 'android':
                from jnius import autoclass, cast
                Intent = autoclass('android.content.Intent')
                Uri = autoclass('android.net.Uri')
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                intent = Intent(Intent.ACTION_VIEW,
                                Uri.parse(open_page_url))
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                cast('android.content.Context',
                     PythonActivity.mActivity).startActivity(intent)
            else:
                import webbrowser
                webbrowser.open(open_page_url)
        except Exception as ex:
            _route_status(_tr(
                'Could not open install page: {error}').format(error=ex))
            return
        # Don't dismiss: the user may be going to read release
        # notes / install via the browser and come back; we want
        # to remain modal so they can't proceed in the broken peer
        # state (server case) or wander off without confirming
        # (self-update case).

    def _quit(*_):
        popup.dismiss()
        if dismiss_action == 'quit':
            # Server case: peer can't function, close the app.
            try:
                from kivy.app import App
                app = App.get_running_app()
                if app is not None:
                    app.stop()
            except Exception as ex:
                print(f'[install_popup] App.stop() raised: {ex}')
        # else dismiss_action == 'dismiss': popup just closes,
        # peer keeps running. Right for self-update where
        # declining means "stick with current version".

    install_btn.bind(on_release=_do_install)
    open_page_btn.bind(on_release=_open_page)
    quit_btn.bind(on_release=_quit)
    if retry_btn is not None:
        def _retry(*_):
            popup.dismiss()
            try:
                on_retry()
            except Exception as ex:
                print(f'[install_popup] on_retry raised: {ex}')
        retry_btn.bind(on_release=_retry)
    if open_app_btn is not None:
        def _open_app(*_):
            popup.dismiss()
            try:
                on_open_app()
            except Exception as ex:
                print(f'[install_popup] on_open_app raised: {ex}')
        open_app_btn.bind(on_release=_open_app)
    if restart_btn is not None:
        def _restart(*_):
            popup.dismiss()
            try:
                on_restart_server()
            except Exception as ex:
                print(f'[install_popup] on_restart_server raised: '
                      f'{ex}')
        restart_btn.bind(on_release=_restart)
    popup.open()
    return popup
