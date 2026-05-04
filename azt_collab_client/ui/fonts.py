"""Shared CharisSIL font registration.

The recorder, the standalone picker, and the settings UI all want
the same typography. This helper does the discovery once and returns
the LabelBase name to feed into ``font_name:`` in KV (or
``font_name=`` in Python widgets).

Search order:

  1. ``<azt_collab_client>/fonts/CharisSIL-*.ttf`` — the canonical
     location once fonts ship with the client. Empty today; reserved
     for the move that consolidates assets next to ``azt.png``.
  2. ``<sibling>/azt_recorder/fonts/`` — desktop dev layout where
     azt-collab and azt_recorder are sibling repos.
  3. The recorder APK's app dir on Android
     (``/data/user/0/org.atoznback.azt_recorder/files/app/fonts/``).
     Will only resolve if the recorder is installed AND the OS lets
     us read its files (typically only with the same UID, so this is
     mostly a no-op for the standalone server APK; we keep it for
     symmetry with the recorder's own search path).
  4. System font dirs (``/usr/share/fonts/...`` etc.).

If nothing turns up we return ``'Roboto'`` and print a single-line
warning. Calling this twice is safe — Kivy's ``LabelBase.register``
is idempotent for the same name + paths.
"""

import os


_CACHED_FONT_NAME = None


def _client_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_font(filename):
    client_dir = _client_root()
    sibling_recorder = os.path.normpath(
        os.path.join(client_dir, '..', '..', 'azt_recorder', 'fonts'))
    candidates = [
        os.path.join(client_dir, 'fonts', filename),
        os.path.join(sibling_recorder, filename),
        os.path.join(
            '/data/user/0/org.atoznback.azt_recorder/files/app/fonts',
            filename),
        os.path.join('/usr/share/fonts/truetype/fonts-sil-charis', filename),
        os.path.join('/usr/share/fonts/opentype/charis', filename),
        os.path.join(os.path.expanduser('~'), '.fonts', filename),
        os.path.join(os.path.expanduser('~'), '.local/share/fonts', filename),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    if os.path.isdir('/usr/share/fonts'):
        for root, _dirs, files in os.walk('/usr/share/fonts'):
            if filename in files:
                return os.path.join(root, filename)
    return None


def register_charis():
    """Register CharisSIL if the TTFs can be found; return the
    LabelBase name to use (``'CharisSIL'`` or ``'Roboto'``).

    Result is cached after the first call.
    """
    global _CACHED_FONT_NAME
    if _CACHED_FONT_NAME is not None:
        return _CACHED_FONT_NAME
    try:
        from kivy.core.text import LabelBase
    except Exception:
        _CACHED_FONT_NAME = 'Roboto'
        return _CACHED_FONT_NAME
    regular = (_find_font('CharisSIL-Regular.ttf')
               or _find_font('CharisSIL.ttf')
               or _find_font('charissil.ttf')
               or _find_font('CharisSIL-R.ttf'))
    if regular is None:
        _CACHED_FONT_NAME = 'Roboto'
        return _CACHED_FONT_NAME
    bold = (_find_font('CharisSIL-Bold.ttf')
            or _find_font('CharisSIL-B.ttf') or regular)
    italic = (_find_font('CharisSIL-Italic.ttf')
              or _find_font('CharisSIL-I.ttf') or regular)
    boldita = (_find_font('CharisSIL-BoldItalic.ttf')
               or _find_font('CharisSIL-BI.ttf') or bold)
    LabelBase.register(
        name='CharisSIL',
        fn_regular=regular,
        fn_bold=bold,
        fn_italic=italic,
        fn_bolditalic=boldita,
    )
    _CACHED_FONT_NAME = 'CharisSIL'
    return _CACHED_FONT_NAME
