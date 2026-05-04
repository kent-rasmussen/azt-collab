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


def install_server_apk_popup(on_status=None, font_name='Roboto'):
    """Show a popup explaining the server APK is missing, with a button
    that opens the install URL.

    On Android the button dispatches ``Intent.ACTION_VIEW`` to
    ``SERVER_APK_INSTALL_URL`` (the GitHub release page); on desktop
    the same URL is opened via ``webbrowser.open`` so the helper is
    safe to call from either platform.

    ``on_status(msg)`` is called with a status string when the install
    page can't be opened (jnius unavailable on Android, no browser on
    desktop). Hosts can wire it into their status bar.

    Returns the Popup so callers can hold a ref if they want to
    programmatically dismiss it.
    """
    from .. import SERVER_APK_INSTALL_URL
    msg = (
        _tr('The AZT collaboration service (server APK) is not installed.')
        + '\n\n'
        + _tr('Install it to enable sync, then reopen this app.')
        + '\n\n' + SERVER_APK_INSTALL_URL
    )
    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))
    content.add_widget(Label(
        text=msg, halign='left', valign='top',
        font_size=sp(13), color=theme.TEXT, font_name=font_name,
    ))
    btn_row = BoxLayout(
        size_hint_y=None, height=dp(48), spacing=dp(12))
    open_btn = Button(
        text=_tr('Open install page'),
        font_size=sp(14), font_name=font_name,
        background_color=theme.ACCENT,
    )
    close_btn = Button(
        text=_tr('Dismiss'),
        font_size=sp(14), font_name=font_name,
    )
    btn_row.add_widget(open_btn)
    btn_row.add_widget(close_btn)
    content.add_widget(btn_row)

    popup = Popup(
        title=_tr('AZT collaboration service required'),
        content=content,
        size_hint=(0.85, None), height=dp(280),
        auto_dismiss=True,
    )

    def _open(*_):
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
            if on_status:
                on_status(f'could not open install page — {ex}')
        popup.dismiss()

    open_btn.bind(on_release=_open)
    close_btn.bind(on_release=popup.dismiss)
    popup.open()
    return popup
