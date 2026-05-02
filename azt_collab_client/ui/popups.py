"""Shared Kivy popups for the picker flows.

Currently provides the clone-from-URL prompt; later picker steps will
move template-confirm and other modals here too. Translations route
through ``azt_collab_client.translate.tr``; theme lives alongside this
module at ``azt_collab_client.ui.theme``.
"""

from . import theme

from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput

from ..translate import tr as _tr


def clone_url_popup(on_submit, font_name='Roboto'):
    """Show a popup asking for a git repository URL. Calls
    ``on_submit(clone_url)`` (with .git suffix appended if missing) when
    the user confirms a non-empty URL; the popup dismisses itself first.
    Cancel just dismisses. Returns the Popup so callers can hold a ref
    if they want to programmatically close it."""
    content = BoxLayout(
        orientation='vertical', spacing=dp(10), padding=dp(12))
    content.add_widget(Label(
        text=_tr('Clone a git repository containing a LIFT file:'),
        size_hint_y=None, height=dp(30),
        font_size=sp(13), color=theme.TEXT, font_name=font_name,
    ))
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
        size_hint=(0.9, None), height=dp(240),
        auto_dismiss=True,
    )

    def _do_clone(*_args):
        clone_url = url_input.text.strip()
        popup.dismiss()
        if not clone_url:
            return
        if not clone_url.endswith('.git'):
            clone_url += '.git'
        try:
            on_submit(clone_url)
        except Exception as ex:
            print(f'[clone_url_popup] on_submit raised: {ex}')

    cancel_btn.bind(on_release=popup.dismiss)
    clone_btn.bind(on_release=_do_clone)
    popup.open()
    return popup
