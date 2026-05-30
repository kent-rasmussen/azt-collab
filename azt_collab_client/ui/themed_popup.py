"""
Theme-aware Popup subclass used across the LAN-sync UI.

Kivy's stdlib ``Popup`` ships with a fixed grey 9-patch background
and white title text — fine in isolation but jarring against the
suite's themed surfaces (Earth / Ocean / Forest / Slate palettes
defined in ``theme.py``). Every popup in
``azt_collab_client/ui/lan_popups.py``,
``azt_collab_client/ui/decisions.py``, and the share /
adopt-origin / install flows wraps its content in this class so
the visual treatment matches the rest of the app.

How it works:

- ``background = ''`` drops the default 9-patch image.
- ``background_color = theme.BG`` tints what's left.
- A ``canvas.before`` painter on the popup itself fills the
  underlying rectangle with ``theme.BG`` for crisp edges
  (without this, some Kivy backends bleed the parent's
  background through the rounded corners).
- ``title_color = theme.TEXT`` matches the body text colour.
- ``separator_color = theme.ACCENT`` puts a thin per-palette
  accent line between the title and the content.
- ``title_size`` is bumped slightly so the title reads as a
  header against the body.

Callers continue to use the standard ``Popup`` constructor
keywords (``title``, ``content``, ``size_hint``, ``height``,
``auto_dismiss``). No callsite changes needed beyond swapping
the import.
"""

from __future__ import annotations

from kivy.graphics import Color, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.button import Button
from kivy.uix.popup import Popup

from . import theme


# Corner radius shared across every themed primitive. Matches the
# picker's ``RecBtn`` / ``NavBtn`` KV rules in
# ``azt_collabd/ui/app.py``.
_RADIUS = dp(8)


class ThemedPopup(Popup):
    """Drop-in replacement for ``kivy.uix.popup.Popup`` that
    follows the suite's active theme palette. See module
    docstring for the colour roles consumed."""

    def __init__(self, **kwargs):
        # Drop the stdlib 9-patch image — we paint the
        # background ourselves via canvas.before so the surface
        # tracks ``theme.BG`` exactly, with no tinted-grey bleed
        # at the rounded corners.
        kwargs.setdefault('background', '')
        # Setting background_color keeps Kivy happy (the default
        # background_color of (1,1,1,1) would multiply against
        # any future background image), but the canvas.before
        # paint is what's actually visible.
        kwargs.setdefault('background_color', theme.BG)
        kwargs.setdefault('title_color', theme.TEXT)
        kwargs.setdefault('separator_color', theme.ACCENT)
        kwargs.setdefault('title_size', sp(16))
        super().__init__(**kwargs)
        with self.canvas.before:
            self._bg_color = Color(*theme.BG)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync_bg, size=self._sync_bg)

    def _sync_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size


class _ThemedButtonBase(Button):
    """Common painter for ``ThemedButton`` / ``ThemedAccentButton``.
    Subclasses set ``_fill_role`` (theme attribute name to paint as
    the rounded background) and ``_text_role`` (theme attribute
    for the foreground text)."""

    _fill_role = 'SURFACE'
    _text_role = 'ACCENT'

    def __init__(self, **kwargs):
        # If the caller explicitly passed ``background_color``
        # (legacy pattern from lan_popups.py / decisions.py:
        # ``Button(..., background_color=theme.ACCENT)`` to mark
        # primary actions), use it as the rounded fill colour and
        # auto-switch the text colour to white. Otherwise fall
        # back to the subclass's ``_fill_role`` / ``_text_role``.
        explicit_fill = kwargs.pop('background_color', None)
        if explicit_fill is not None and explicit_fill != theme.TRANSPARENT:
            fill_rgba = explicit_fill
            # White text only against the ACCENT fill (the
            # "primary action" pattern callers use today). Any
            # other explicit fill keeps ``_text_role`` so SURFACE
            # / SURFACE_ALT fills don't unintentionally render
            # invisible white-on-light text in palettes where
            # SURFACE is lighter than ACCENT.
            if tuple(explicit_fill) == tuple(theme.ACCENT):
                text_default = (1, 1, 1, 1)
            else:
                text_default = getattr(theme, self._text_role)
        else:
            fill_rgba = getattr(theme, self._fill_role)
            text_default = getattr(theme, self._text_role)
        # Strip the stdlib bevelled 9-patch — RoundedRectangle in
        # canvas.before is what's visible.
        kwargs.setdefault('background_normal', '')
        kwargs.setdefault('background_down', '')
        kwargs.setdefault('background_disabled_normal', '')
        kwargs.setdefault('background_disabled_down', '')
        # Match the picker's RecBtn / NavBtn typography.
        kwargs.setdefault('color', text_default)
        kwargs.setdefault('font_size', sp(15))
        kwargs.setdefault('bold', True)
        super().__init__(background_color=theme.TRANSPARENT, **kwargs)
        with self.canvas.before:
            self._fill_color = Color(*fill_rgba)
            self._fill_rect = RoundedRectangle(
                pos=self.pos, size=self.size, radius=[_RADIUS])
        self.bind(pos=self._sync_fill, size=self._sync_fill)

    def _sync_fill(self, *_):
        self._fill_rect.pos = self.pos
        self._fill_rect.size = self.size


class ThemedButton(_ThemedButtonBase):
    """Drop-in replacement for ``kivy.uix.button.Button`` that
    matches the picker's ``NavBtn`` / ``RecBtn`` KV rules:

    - **Secondary** (no ``background_color`` kwarg) — fills
      ``theme.SURFACE``, text ``theme.ACCENT``, 8 dp rounded
      corners. Use for Cancel / Close / Decline / Manage etc.
    - **Primary / accent** (caller passes
      ``background_color=theme.ACCENT``) — fills with the
      passed colour, text auto-switches to white. Existing
      callsites that already mark primary buttons with
      ``background_color=theme.ACCENT`` get the accent shape
      for free, no callsite churn.

    Both flavours strip the stdlib 9-patch and paint a
    ``RoundedRectangle`` so the visual matches the picker."""
    _fill_role = 'SURFACE'
    _text_role = 'ACCENT'
