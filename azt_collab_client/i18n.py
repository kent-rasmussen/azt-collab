"""Client-owned i18n.

Owns the gettext domain ``azt_collab_client``. Strings the client
package owns (picker UI, popups, status translations, settings UI)
live in ``azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po``.

Suite apps pick a fallback chain that suits them:

- The standalone picker / settings subprocesses use this module
  directly — ``set_translator(azt_collab_client.i18n._)`` at startup.
- A peer with its own catalog (the recorder, with ``aztrecorder.po``)
  builds a ``gettext.translation`` for its own domain, then calls
  ``add_fallback(_client_translation())`` so its ``_()`` resolves
  recorder strings first and falls back to client strings.

UI language is persisted in ``$AZT_HOME/config.json`` under
``ui.language``. ``set_language`` saves the choice; constructors of
peer i18n modules read it on startup so all suite apps agree on the
language without an extra coordination channel.

PO → MO compilation runs lazily on first ``set_language`` so peers
shipping only ``.po`` files (or sister apps editing translations
in-place) work without external ``msgfmt`` tooling. Implementation is
the GNU MO format spec (single magic, sorted msgid array, two
parallel offset tables) packed with ``struct``.
"""

import gettext
import json
import os
import struct
import sys

from .paths import azt_home


_DOMAIN = 'azt_collab_client'
_LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'locales')

_current: gettext.NullTranslations = gettext.NullTranslations()
_current_lang: str = 'en'

_DISPLAY_NAMES = {
    'en': 'English',
    'fr': 'Français',
    'es': 'Español',
    'pt': 'Português',
    'de': 'Deutsch',
    'sw': 'Kiswahili',
    'zh': '中文',
    'ar': 'العربية',
}


# ── config persistence ─────────────────────────────────────────────────────

def _config_path():
    return os.path.join(azt_home(), 'config.json')


def _load_config():
    try:
        with open(_config_path()) as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_config(d):
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def language_pref():
    """Read the persisted UI language from config.json. Defaults to 'en'."""
    return (_load_config().get('ui') or {}).get('language', 'en')


def _save_language_pref(lang):
    cfg = _load_config()
    cfg.setdefault('ui', {})['language'] = lang
    _save_config(cfg)


# ── pure-Python PO → MO compile (msgfmt-lite) ──────────────────────────────

_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r',
            '"': '"', '\\': '\\', '0': '\0'}


def _po_unquote(s):
    """Strip enclosing quotes and process backslash escapes. Manual
    walk so non-ASCII msgstr content (zh / ar / etc.) round-trips
    cleanly — Python's ``unicode_escape`` codec via ``latin-1`` would
    UnicodeEncodeError on chars > U+00FF."""
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            esc = s[i + 1]
            out.append(_ESCAPES.get(esc, esc))
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _parse_po(path):
    """Parse a .po file into a {msgid: msgstr} dict. Handles plain
    msgid/msgstr pairs (the only kind the client uses); ignores
    msgctxt and plural forms."""
    messages = {}
    msgid = msgstr = None
    state = None
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.rstrip('\n')
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                if msgid is not None and msgstr is not None:
                    messages[msgid] = msgstr
                    msgid = msgstr = None
                state = None
                continue
            if line.startswith('msgid '):
                if msgid is not None and msgstr is not None:
                    messages[msgid] = msgstr
                msgid = _po_unquote(line[6:])
                msgstr = None
                state = 'msgid'
            elif line.startswith('msgstr '):
                msgstr = _po_unquote(line[7:])
                state = 'msgstr'
            elif line.startswith('"'):
                if state == 'msgid':
                    msgid += _po_unquote(line)
                elif state == 'msgstr':
                    msgstr += _po_unquote(line)
    if msgid is not None and msgstr is not None:
        messages[msgid] = msgstr
    return messages


def _compile_mo(po_path, mo_path):
    """Write a GNU MO file derived from po_path. Drops empty
    translations so untranslated msgids fall through to the msgid as
    gettext expects."""
    msgs = _parse_po(po_path)
    # Keep the empty-msgid header entry (carries Content-Type etc.)
    # plus every translated entry.
    keys = sorted(k for k in msgs if k == '' or msgs[k])
    n = len(keys)
    header_size = 28
    table_orig_off = header_size
    table_trans_off = table_orig_off + n * 8
    blob_orig_off = table_trans_off + n * 8

    orig_blob = b''
    trans_blob = b''
    orig_table = []
    trans_table = []
    for k in keys:
        kb = k.encode('utf-8')
        vb = msgs[k].encode('utf-8')
        orig_table.append((len(kb), len(orig_blob)))
        trans_table.append((len(vb), len(trans_blob)))
        orig_blob += kb + b'\0'
        trans_blob += vb + b'\0'
    blob_trans_off = blob_orig_off + len(orig_blob)

    out = struct.pack('<IIIIIII',
                      0x950412DE, 0, n,
                      table_orig_off, table_trans_off, 0, 0)
    for length, off in orig_table:
        out += struct.pack('<II', length, blob_orig_off + off)
    for length, off in trans_table:
        out += struct.pack('<II', length, blob_trans_off + off)
    out += orig_blob + trans_blob

    tmp = mo_path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(out)
    os.replace(tmp, mo_path)


def ensure_mo(locale_dir, domain, lang):
    """Compile ``<locale_dir>/<lang>/LC_MESSAGES/<domain>.po`` → ``.mo``
    if the ``.mo`` is missing or older than the ``.po``. No-op for
    English (no catalog needed) or when the ``.po`` is absent.

    Peer i18n modules call this before ``gettext.translation(...)`` so
    they can ship ``.po``-only and skip the external ``msgfmt`` build
    step the same way the client does. Writes the ``.mo`` next to the
    ``.po``; on Android that's inside the APK's private filesDir
    (writable, since p4a extracts Python source there at first run).
    Errors are logged and swallowed — gettext falls back to msgid."""
    if lang == 'en':
        return
    base = os.path.join(locale_dir, lang, 'LC_MESSAGES', domain)
    po, mo = base + '.po', base + '.mo'
    if not os.path.isfile(po):
        return
    if (os.path.isfile(mo)
            and os.path.getmtime(mo) >= os.path.getmtime(po)):
        return
    try:
        _compile_mo(po, mo)
    except Exception as ex:
        print(f'[client.i18n] compile {po}: {ex}', file=sys.stderr)


def _ensure_mo(lang):
    ensure_mo(_LOCALE_DIR, _DOMAIN, lang)


# ── public API ─────────────────────────────────────────────────────────────

def display_name(code):
    """Human-readable display name for a language code. Returns the code
    itself for unknown codes so the picker still has *something* to
    show."""
    return _DISPLAY_NAMES.get(code, code)


def scan_catalog_languages(locale_dir, domain):
    """Enumerate ``[(code, display_name), ...]`` for English plus every
    ``<lang>/LC_MESSAGES/<domain>.{po,mo}`` under ``locale_dir``. Sorted
    with English first; each peer's i18n module uses this to discover
    its own catalog languages with the same shape as the client's."""
    out = [('en', display_name('en'))]
    if os.path.isdir(locale_dir):
        for entry in sorted(os.listdir(locale_dir)):
            base = os.path.join(locale_dir, entry, 'LC_MESSAGES', domain)
            if os.path.isfile(base + '.po') or os.path.isfile(base + '.mo'):
                out.append((entry, display_name(entry)))
    return out


def _apply(lang):
    """Load the catalog for ``lang`` into module state. No persistence —
    callers that should write the choice through call ``set_language``
    instead. Falls back to English if the catalog can't be loaded."""
    global _current, _current_lang
    _ensure_mo(lang)
    _current_lang = lang
    if lang == 'en':
        _current = gettext.NullTranslations()
        return _current_lang
    try:
        _current = gettext.translation(
            _DOMAIN, localedir=_LOCALE_DIR, languages=[lang])
    except FileNotFoundError:
        print(f'[client.i18n] no catalog for {lang!r}, '
              f'falling back to English', file=sys.stderr)
        _current = gettext.NullTranslations()
        _current_lang = 'en'
    return _current_lang


def set_language(lang):
    """Switch the active UI language and persist the choice to
    ``$AZT_HOME/config.json``. There is no transient mode — one
    preference, one store, sticks everywhere until next changed.
    Returns the language actually set (falls back to ``'en'`` if the
    catalog can't be loaded)."""
    applied = _apply(lang)
    try:
        _save_language_pref(applied)
    except OSError as ex:
        print(f'[client.i18n] could not persist language: {ex}',
              file=sys.stderr)
    return applied


def current_language():
    return _current_lang


def available_languages():
    """[(code, display_name), ...] for English plus every <lang>
    directory under our locale tree that has a .po or .mo for our
    domain. Sorted with English first."""
    return scan_catalog_languages(_LOCALE_DIR, _DOMAIN)


def _(msg):
    """Translate using the active client catalog."""
    return _current.gettext(msg)


def gettext_translation():
    """Return the underlying ``gettext.NullTranslations``-or-subclass for
    the active language, so peer i18n modules can ``add_fallback`` to
    chain client strings under their own domain."""
    return _current


# ── auto-init ──────────────────────────────────────────────────────────────
# Apply persisted language at import. Failure here is not fatal —
# ``_current`` stays as ``NullTranslations`` and the picker falls back
# to English.
try:
    _apply(language_pref())
except Exception as ex:
    print(f'[client.i18n] init failed: {ex}', file=sys.stderr)
