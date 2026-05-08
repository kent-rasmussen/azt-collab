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
    ``on_submit(clone_url, langcode)`` when the user submits;
    ``langcode`` is the daemon's auto-derived value by default,
    overridable through the inline *change code* affordance.

    Two mutually-exclusive edit modes (only one input is active at
    a time, so the on-screen keyboard never argues with itself):

    Mode A — URL active (default):
        ┌─ ``code: <derived>``                    [change code] ┐
        ├─ URL TextInput (active, auto-focus)                   ┤

    Mode B — code active (after tapping *change code*):
        ┌─ TextInput (code, auto-focus)                    [OK] ┐
        ├─ URL TextInput (disabled, displays the typed value)   ┤

    Tapping **change code** captures the current derived code into
    the code field and disables the URL field. Tapping **OK**
    commits the typed code, re-enables the URL field, and swaps
    Mode A back in — but the readout now shows the user's value
    and stops syncing with further URL edits (once the user takes
    control, they keep it). Submit (Clone) works in either mode."""
    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))
    content.add_widget(Label(
        text=_tr('Clone a git repository containing a LIFT file:'),
        size_hint_y=None, height=dp(30),
        font_size=sp(13), color=theme.TEXT, font_name=font_name,
    ))

    # Mode-A widgets (preview + change_btn).
    code_label = Label(
        text=_tr('code: ') + '—',
        size_hint_x=1,
        font_size=sp(13), color=theme.TEXT_DIM, font_name=font_name,
        halign='left', valign='middle',
    )
    code_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
    change_btn = Button(
        text=_tr('change code'),
        size_hint_x=None, width=dp(120),
        font_size=sp(12), font_name=font_name,
    )
    # Mode-B widgets (editable code + OK).
    code_input = TextInput(
        text='',
        hint_text=_tr('e.g. en-x-pilot'),
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

    url_input = TextInput(
        text='',
        hint_text=_tr('Paste the repository URL here'),
        multiline=False, size_hint_y=None, height=dp(48),
        font_size=sp(14), font_name=font_name,
    )
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
        size_hint=(0.9, None), height=dp(290),
        auto_dismiss=True,
    )

    state = {
        'mode': 'A',           # 'A' = URL active, 'B' = code active
        'overridden': False,   # user has typed an explicit code at
                               # least once; URL changes stop syncing
        'user_code': '',
    }

    def _refresh_label_from_url(*_):
        if state['overridden'] or state['mode'] == 'B':
            return
        derived = _derive_langcode_from_url(url_input.text) or '—'
        code_label.text = _tr('code: ') + derived

    def _enter_mode_b(*_):
        # Capture whatever's currently shown as the starting point.
        if state['overridden']:
            seed = state['user_code']
        else:
            seed = _derive_langcode_from_url(url_input.text)
        code_input.text = seed
        # Swap Mode-A widgets out, Mode-B widgets in. URL goes
        # disabled so only one input has focus.
        code_row.clear_widgets()
        code_row.add_widget(code_input)
        code_row.add_widget(ok_btn)
        url_input.disabled = True
        state['mode'] = 'B'
        code_input.focus = True

    def _enter_mode_a(*_):
        # Commit whatever the user typed (empty falls back to the
        # current derivation — same as if they never entered the
        # code-edit mode, but with overridden cleared so URL syncs
        # resume).
        typed = code_input.text.strip()
        if typed:
            state['user_code'] = typed
            state['overridden'] = True
            code_label.text = _tr('code: ') + typed
        else:
            state['overridden'] = False
            _refresh_label_from_url()
        code_row.clear_widgets()
        code_row.add_widget(code_label)
        code_row.add_widget(change_btn)
        url_input.disabled = False
        state['mode'] = 'A'
        url_input.focus = True

    def _resolve_langcode():
        # Mode B at submit time: take the live code-input value.
        # Mode A: respect override or fall back to derived.
        if state['mode'] == 'B':
            return code_input.text.strip() \
                or _derive_langcode_from_url(url_input.text)
        if state['overridden']:
            return state['user_code']
        return _derive_langcode_from_url(url_input.text)

    def _do_clone(*_args):
        clone_url = url_input.text.strip()
        popup.dismiss()
        if not clone_url:
            return
        if not clone_url.endswith('.git'):
            clone_url += '.git'
        langcode = _resolve_langcode()
        try:
            on_submit(clone_url, langcode)
        except Exception as ex:
            print(f'[clone_url_popup] on_submit raised: {ex}')

    url_input.bind(text=_refresh_label_from_url)
    change_btn.bind(on_release=_enter_mode_b)
    ok_btn.bind(on_release=_enter_mode_a)
    cancel_btn.bind(on_release=popup.dismiss)
    clone_btn.bind(on_release=_do_clone)
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
        text=_tr('Open install page'),
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

    # Bind text_size to size so labels wrap inside their button
    # bounds rather than spilling / clipping.
    def _bind_wrap(b):
        b.bind(size=lambda w, _v: setattr(w, 'text_size', w.size))
    for b in (install_btn, open_page_btn, quit_btn):
        _bind_wrap(b)
    if retry_btn is not None:
        _bind_wrap(retry_btn)

    btn_row.add_widget(quit_btn)
    if retry_btn is not None:
        btn_row.add_widget(retry_btn)
    btn_row.add_widget(open_page_btn)
    btn_row.add_widget(install_btn)
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
        # Surface progress in the popup body so the user always sees
        # what's happening even if the host's status sink isn't
        # currently visible. Keeps the lead context line so the
        # user keeps the "what's this about" anchor.
        body_label.text = (msg + '\n\n' + (text or '')) if msg else (
            text or '')
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
    popup.open()
    return popup
