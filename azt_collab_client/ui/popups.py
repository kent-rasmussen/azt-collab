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
                             on_install_complete=None):
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

    Returns the Popup so callers can hold a ref. ``auto_dismiss``
    is False — the user must explicitly choose Quit / Open page /
    Install. That's the modal-blocking behaviour the suite UX
    contract wants ("user can't reach settings or picker if there's
    no server").
    """
    from .. import SERVER_APK_INSTALL_URL
    from ..bootstrap import (
        _SERVER_REPO_DEFAULT, _SERVER_ASSET_DEFAULT,
        _SERVER_PACKAGE_NAME,
    )

    if install_target_package is None:
        install_target_package = _SERVER_PACKAGE_NAME

    default_body = (
        _tr('The AZT collaboration service (server APK) is not installed.')
        + '\n\n'
        + _tr('Tap Install to download and install it. Android will '
              'ask you to confirm before the install starts.')
    )
    msg = body_message or default_body

    # Quit-button label includes the host's app name so the user
    # can see what's about to close. App.title is what the recorder
    # / viewer set on their App subclass; falls back to a generic
    # "Quit" if the host hasn't set one.
    try:
        from kivy.app import App
        app = App.get_running_app()
        app_title = getattr(app, 'title', '') or ''
    except Exception:
        app_title = ''
    quit_label = (_tr('Quit {app}').format(app=app_title)
                  if app_title else _tr('Quit'))

    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))
    body_label = Label(
        text=msg, halign='left', valign='top',
        font_size=sp(13), color=theme.TEXT, font_name=font_name,
    )
    body_label.bind(width=lambda w, val: setattr(
        w, 'text_size', (val, None)))
    content.add_widget(body_label)
    btn_row = BoxLayout(
        size_hint_y=None, height=dp(48), spacing=dp(12))
    install_btn = Button(
        text=install_label or _tr('Install'),
        font_size=sp(14), font_name=font_name,
        background_color=theme.ACCENT,
    )
    open_page_btn = Button(
        text=_tr('Open install page'),
        font_size=sp(13), font_name=font_name,
    )
    quit_btn = Button(
        text=quit_label,
        font_size=sp(14), font_name=font_name,
    )
    btn_row.add_widget(quit_btn)
    btn_row.add_widget(open_page_btn)
    btn_row.add_widget(install_btn)
    content.add_widget(btn_row)

    popup = Popup(
        title=title or _tr('AZT collaboration service required'),
        content=content,
        size_hint=(0.9, None), height=dp(280),
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
        # Reuse the suite-shared updater. Disabling the buttons
        # while the worker runs prevents accidental double-taps.
        install_btn.disabled = True
        open_page_btn.disabled = True
        from .update import check_for_update
        from kivy.clock import Clock

        def _on_no_update():
            _route_status(_tr('AZT Collaboration is up to date.'))
            install_btn.disabled = False
            open_page_btn.disabled = False

        def _on_error(err):
            _route_status(_tr('Install failed: {error}').format(error=err))
            install_btn.disabled = False
            open_page_btn.disabled = False

        def _on_complete():
            # Polling confirmed the new server version is live. Show
            # the "Installed." status briefly so the user sees what
            # happened, then dismiss the popup and call the host's
            # continuation (typically: re-run bootstrap's compat
            # check and fire on_done so normal startup proceeds).
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

        check_for_update(
            repo=_SERVER_REPO_DEFAULT,
            current_version=current_server_version,
            asset_filename=_SERVER_ASSET_DEFAULT,
            on_status=_route_status,
            on_no_update=_on_no_update,
            on_error=_on_error,
            install_target_package=install_target_package,
            on_install_complete=_on_complete,
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
                                Uri.parse(SERVER_APK_INSTALL_URL))
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                cast('android.content.Context',
                     PythonActivity.mActivity).startActivity(intent)
            else:
                import webbrowser
                webbrowser.open(SERVER_APK_INSTALL_URL)
        except Exception as ex:
            _route_status(_tr(
                'Could not open install page: {error}').format(error=ex))
            return
        # Don't dismiss: the user is going to install via the
        # browser and come back; we want to remain modal so they
        # can't proceed in the broken peer state.

    def _quit(*_):
        popup.dismiss()
        try:
            from kivy.app import App
            app = App.get_running_app()
            if app is not None:
                app.stop()
        except Exception as ex:
            print(f'[install_popup] App.stop() raised: {ex}')

    install_btn.bind(on_release=_do_install)
    open_page_btn.bind(on_release=_open_page)
    quit_btn.bind(on_release=_quit)
    popup.open()
    return popup
