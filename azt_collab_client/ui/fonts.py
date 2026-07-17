"""Shared CharisSIL font registration.

The recorder, the standalone picker, and the settings UI all want
the same typography. This helper does the discovery once and returns
the LabelBase name to feed into ``font_name:`` in KV (or
``font_name=`` in Python widgets).

Search order:

  1. ``<client_dir>/../fonts/<filename>`` — source-tree-relative.
     On Android (p4a packs the source tree flat at
     ``<files>/app/``) this resolves to
     ``<files>/app/fonts/<filename>`` regardless of which suite
     APK is running — recorder, server APK, viewer all carry the
     same shape because they're all built from a tree where
     ``azt_collab_client/`` sits next to ``fonts/`` (peer ships
     CharisSIL TTFs in a top-level ``fonts/`` dir). On desktop
     with the recorder/symlink layout this resolves to
     ``<recorder>/fonts/<filename>``. Doesn't depend on the
     peer's Android package name. (Added per
     ``NOTES_TO_DAEMON.md`` 2026-05-26: the prior Android
     hard-coded candidate referenced a package name
     ``org.atoznback.azt_recorder`` that never existed — the
     recorder ships as ``org.atoznback.aztrecorder``, no
     underscore — and was removed as dead code.)
  2. ``<azt_collab_client>/fonts/CharisSIL-*.ttf`` — the canonical
     location once fonts ship with the client. Empty today; reserved
     for the move that consolidates assets next to ``azt.png``.
  3. ``<sibling>/azt_recorder/fonts/`` — desktop dev layout where
     azt-collab and azt_recorder are sibling repos (directory
     name, not Android package name).
  4. System font dirs (``/usr/share/fonts/...`` etc.).

If nothing turns up we return ``'Roboto'`` and print a single-line
warning. Calling this twice is safe — Kivy's ``LabelBase.register``
is idempotent for the same name + paths.
"""

import os
import sys


_CACHED_FONT_NAME = None
_LAST_SEARCHED_PATHS = []   # populated by _find_font for diagnostic fallback log


def _client_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_font(filename):
    client_dir = _client_root()
    sibling_recorder = os.path.normpath(
        os.path.join(client_dir, '..', '..', 'azt_recorder', 'fonts'))
    candidates = [
        # Source-tree-relative — resolves on every peer that ships
        # the TTFs in a top-level ``fonts/`` dir (the suite
        # convention) regardless of Android package name. On
        # desktop with the recorder/symlink layout this is
        # ``<recorder>/fonts/<filename>``; on Android (p4a's flat
        # source tree at ``<files>/app/``) this is
        # ``<files>/app/fonts/<filename>``. This is what actually
        # resolves on Android. The previous hard-coded path
        # ``/data/user/0/org.atoznback.azt_recorder/files/app/fonts/``
        # was dead code — that Android package never existed
        # (the recorder ships as ``org.atoznback.aztrecorder``,
        # no underscore) and cross-UID reads would have been
        # denied even if it had — removed.
        os.path.normpath(
            os.path.join(client_dir, '..', 'fonts', filename)),
        os.path.join(client_dir, 'fonts', filename),
        os.path.join(sibling_recorder, filename),
        os.path.join('/usr/share/fonts/truetype/fonts-sil-charis', filename),
        os.path.join('/usr/share/fonts/opentype/charis', filename),
        os.path.join(os.path.expanduser('~'), '.fonts', filename),
        os.path.join(os.path.expanduser('~'), '.local/share/fonts', filename),
        # Windows: machine-wide and per-user font installs (2026-07-16)
        os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts',
                     filename),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft',
                     'Windows', 'Fonts', filename),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    if os.path.isdir('/usr/share/fonts'):
        for root, _dirs, files in os.walk('/usr/share/fonts'):
            if filename in files:
                return os.path.join(root, filename)
    # Stash for the fallback diagnostic in ``register_charis``.
    _LAST_SEARCHED_PATHS[:] = candidates
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
        # Diagnostic per NOTES_TO_DAEMON 2026-05-26: the silent
        # 'Roboto' fallback was previously invisible in field
        # logs and only surfaced as misrendered Lingala glyphs.
        # One stderr line on fallback lets the next field log
        # name which paths were tried, without the user having
        # to infer the issue from boxes-instead-of-tone-marks.
        try:
            searched = ', '.join(_LAST_SEARCHED_PATHS[:3]) + (
                f' (+{len(_LAST_SEARCHED_PATHS) - 3} more)'
                if len(_LAST_SEARCHED_PATHS) > 3 else '')
        except Exception:
            searched = '(internal error formatting candidates)'
        print(f'[charis] CharisSIL not found — falling back to '
              f'Roboto. Searched: {searched}',
              file=sys.stderr, flush=True)
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
