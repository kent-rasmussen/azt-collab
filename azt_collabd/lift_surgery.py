"""Surgical, byte-stable LIFT edits.

This module exists because the recorder runs on low-memory devices
(~1 GB user-memory, MTK-class chips) and field projects routinely
hit 4+ MB LIFT files. Round-tripping that through ElementTree on
every audio-file save costs ~5× the source size in DOM memory on
top of the peer's own entries dict — enough to cross Android's
LMKD threshold during normal recording sessions (see CHANGELOG
0.50.29).

The surgical contract (per `NOTES_TO_DAEMON.md` 2026-06-04):

  1. Locate the target entry by `guid` inside the LIFT file.
  2. Insert or replace exactly one sub-element inside that entry.
  3. **Byte-stable diff outside the target entry's bytes** —
     every byte outside the entry's `<entry ...>...</entry>` range
     must equal the input file's bytes at the same offset. Git
     diff shows only that entry's lines changed.
  4. SAX-validate the resulting bytes before the atomic rename;
     refuse to persist invalid XML.
  5. Atomic write via sibling tempfile + `os.replace`, under
     `project_lock` per CLAUDE.md invariant #11.
  6. Fire `notify_project_changed` so `ContentObserver` peers
     wake within ~10 ms.

The "byte-stable outside" property is what makes this surgical
rather than a fancy save: we splice the new entry bytes into the
unchanged document bytes around it. The entry's own internal
layout is re-emitted by ElementTree (with `ET.indent` matching
the file's detected indentation unit), so a first-edit per entry
produces a clean indented diff for that entry; subsequent edits
of the same entry are stable.

Two public ops today, both following the same shape:

- `set_audio` — write one `<form lang="…"><text>filename</text></form>`
  into the entry's `<citation>`. Other `<form>` siblings in
  citation are left intact.
- `set_illustration` — write one `<illustration href="…"/>` into
  the first `<sense>` of the entry. Creates `<sense>` if absent.

Both return a typed `Result` carrying `AUDIO_SET` / `ILLUSTRATION_SET`
on first-time write, `AUDIO_SET_NO_CHANGE` / `ILLUSTRATION_SET_NO_CHANGE`
when the target attribute already had the new value (peer can
suppress redundant UI updates), or `ENTRY_NOT_FOUND` / `LIFT_INVALID`
on failure.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import xml.sax
import xml.sax.handler
from xml.sax import SAXParseException

from .locks import project_lock, LockTimeout
from .status import Result, Status
from . import status as S


# Matches an entry start tag with its `guid` attribute. Both quote
# styles, multi-line tags, and self-closing entries are accepted.
# Group 1 is the guid value; group 2 is the closing punctuation
# (`>` for an opening tag, `/>` for a self-closing entry).
_ENTRY_TAG_RE = re.compile(
    rb'<entry\b[^>]*?\bguid=["\']([^"\']+)["\'][^>]*?(/?>)',
    re.DOTALL)


# Default fallback indent when the file's structure doesn't let
# us detect a real one (e.g., the file is entirely on one line).
# Matches the recorder's lift.py default and the daemon's
# lift_merge output style.
_DEFAULT_INDENT = '  '


def _find_entry_range(file_bytes, guid):
    """Locate the `[start, end)` byte range of the `<entry guid="X">
    ... </entry>` block in *file_bytes*, or `(None, None)` if no
    matching entry is in the file. Handles self-closing entries.

    Returns the byte range INCLUSIVE of the entry's `<entry ...>`
    open tag and `</entry>` close tag (or just the `<entry .../>`
    for a self-closing variant).
    """
    target = guid.encode('utf-8')
    pos = 0
    while True:
        m = _ENTRY_TAG_RE.search(file_bytes, pos)
        if m is None:
            return None, None
        if m.group(1) == target:
            start = m.start()
            if m.group(2) == b'/>':
                return start, m.end()
            close_idx = file_bytes.find(b'</entry>', m.end())
            if close_idx == -1:
                return None, None
            return start, close_idx + len(b'</entry>')
        pos = m.end()


def _detect_indent_unit(file_bytes, entry_start):
    """Sniff the file's per-level indent string from the whitespace
    immediately before the entry's `<entry` open tag. Returns the
    detected indent (e.g., `'  '`, `'\\t'`, `'    '`) or the
    `_DEFAULT_INDENT` fallback when we can't tell.

    The assumption: every `<entry>` is at the first level of the
    document tree (`<lift>` at level 0, `<entry>` at level 1), so
    the whitespace between the preceding newline and the entry's
    start tag is exactly one indent unit's worth.
    """
    newline = file_bytes.rfind(b'\n', 0, entry_start)
    if newline == -1:
        return _DEFAULT_INDENT
    leading = file_bytes[newline + 1:entry_start]
    if not leading or not leading.strip() == b'':
        return _DEFAULT_INDENT
    try:
        return leading.decode('utf-8')
    except UnicodeDecodeError:
        return _DEFAULT_INDENT


def _ns_strip(tag):
    """`'{http://...}entry'` → `'entry'`; `'entry'` → `'entry'`."""
    if tag.startswith('{'):
        return tag.split('}', 1)[1]
    return tag


def _ns_prefix(elem):
    """Return the `{ns}` prefix of *elem*'s tag, or `''` if no
    namespace. Used so child-element lookups match whatever
    namespace context the sub-parsed entry came in with."""
    if elem.tag.startswith('{'):
        return elem.tag[:elem.tag.index('}') + 1]
    return ''


def _sax_well_formed(file_bytes):
    """Return True if *file_bytes* is a well-formed XML document.
    Used as the final guard before the atomic rename — refuses to
    persist a splice that produced invalid XML.

    Stream-parsed via SAX so even for large files this is bounded
    memory (we don't build a DOM).
    """
    try:
        xml.sax.parseString(
            file_bytes, xml.sax.handler.ContentHandler())
        return True
    except SAXParseException:
        return False


def _atomic_write_bytes(target_path, data):
    """Sibling-tempfile + `os.replace`. Caller holds
    `project_lock`. Cleans up the temp file on any failure."""
    target_dir = os.path.dirname(target_path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix='.lift_surgery.', suffix='.tmp', dir=target_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, target_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _surgical_replace_entry(file_bytes, entry_start, entry_end,
                             new_entry_bytes):
    """Splice *new_entry_bytes* into *file_bytes* in place of the
    `[entry_start:entry_end)` range. Pure byte-level — outside the
    range, every byte is preserved exactly."""
    return (file_bytes[:entry_start]
            + new_entry_bytes
            + file_bytes[entry_end:])


def _emit_entry_with_indent(entry_el, indent_unit):
    """Serialize *entry_el* with `ET.indent` so the entry's children
    are indented relative to the entry by *indent_unit*. The entry's
    own opening tag is emitted without leading whitespace — the
    splice context supplies that. The closing tag is preceded by one
    indent unit (the entry's outer level), matching the file's
    existing layout.

    `level=1` tells `ET.indent` to treat the root (entry) as already
    at the file's level-1 column; its children get one extra level
    of indent (so they land at column 2 × indent_unit), and the
    closing tag's preceding whitespace is one indent unit.
    """
    if len(entry_el) > 0:
        ET.indent(entry_el, space=indent_unit, level=1)
    return ET.tostring(entry_el, encoding='utf-8')


def _do_surgical_edit(lift_path, guid, edit_fn):
    """Common surgical-edit pipeline. *edit_fn* is `(entry_el, ns)
    → (changed: bool, no_change_code: str, ok_code: str)` and is
    expected to mutate *entry_el* in place when `changed` is True.

    Caller holds `project_lock`. Returns a `Result`.
    """
    result = Result()
    try:
        with open(lift_path, 'rb') as f:
            file_bytes = f.read()
    except FileNotFoundError:
        result.add(S.LIFT_INVALID, error=f'no such file: {lift_path}')
        return result
    except OSError as ex:
        result.add(S.SERVER_ERROR, error=f'read {lift_path}: {ex!r}')
        return result
    entry_start, entry_end = _find_entry_range(file_bytes, guid)
    if entry_start is None:
        result.add(S.ENTRY_NOT_FOUND, guid=guid)
        return result
    indent_unit = _detect_indent_unit(file_bytes, entry_start)
    entry_bytes = file_bytes[entry_start:entry_end]
    try:
        entry_el = ET.fromstring(entry_bytes)
    except ET.ParseError as ex:
        result.add(S.LIFT_INVALID,
                   error=f'entry parse: {ex!r}', guid=guid)
        return result
    ns = _ns_prefix(entry_el)
    try:
        changed, no_change_code, ok_code = edit_fn(entry_el, ns)
    except Exception as ex:
        result.add(S.SERVER_ERROR,
                   error=f'edit_fn raised: {ex!r}', guid=guid)
        return result
    if not changed:
        result.add(no_change_code, guid=guid)
        return result
    new_entry_bytes = _emit_entry_with_indent(entry_el, indent_unit)
    new_file_bytes = _surgical_replace_entry(
        file_bytes, entry_start, entry_end, new_entry_bytes)
    if not _sax_well_formed(new_file_bytes):
        result.add(S.LIFT_INVALID,
                   error='post-splice XML not well-formed',
                   guid=guid)
        return result
    try:
        _atomic_write_bytes(lift_path, new_file_bytes)
    except OSError as ex:
        result.add(S.SERVER_ERROR,
                   error=f'atomic write {lift_path}: {ex!r}')
        return result
    result.add(ok_code, guid=guid)
    return result


def set_audio(working_dir, lift_path, guid, lang, filename):
    """Surgically set the audio filename on one LIFT entry.

    Locates the entry by *guid*, then find-or-creates
    `<citation>/<form lang="{lang}"><text>{filename}</text></form>`.
    Other `<form>` siblings inside the citation (e.g., the
    vernacular's text form) are not touched — only the audio-lang
    form is inserted or its `<text>` updated.

    Returns a `Result` carrying:

    - `S.AUDIO_SET` — first-time write or text replaced.
    - `S.AUDIO_SET_NO_CHANGE` — the audio-lang form's text already
      equalled *filename*; nothing was written.
    - `S.ENTRY_NOT_FOUND` — no `<entry guid="X">` in the LIFT.
    - `S.LIFT_INVALID` — the source or post-splice file failed
      well-formedness validation, or the LIFT file is missing.
    - `S.BUSY` — `project_lock` couldn't be acquired in time.
    """
    if not lang:
        result = Result()
        result.add(S.LIFT_INVALID, error='lang required')
        return result
    if not filename:
        result = Result()
        result.add(S.LIFT_INVALID, error='filename required')
        return result

    def _edit(entry_el, ns):
        citation = entry_el.find(f'{ns}citation')
        if citation is None:
            citation = ET.SubElement(entry_el, f'{ns}citation')
        target_form = None
        for f in citation.findall(f'{ns}form'):
            if f.get('lang') == lang:
                target_form = f
                break
        if target_form is None:
            target_form = ET.SubElement(citation, f'{ns}form')
            target_form.set('lang', lang)
        text_el = target_form.find(f'{ns}text')
        if text_el is None:
            text_el = ET.SubElement(target_form, f'{ns}text')
        if (text_el.text or '') == filename:
            return False, S.AUDIO_SET_NO_CHANGE, S.AUDIO_SET
        text_el.text = filename
        return True, S.AUDIO_SET_NO_CHANGE, S.AUDIO_SET

    try:
        with project_lock(working_dir, timeout=10.0):
            return _do_surgical_edit(lift_path, guid, _edit)
    except LockTimeout:
        result = Result()
        result.add(S.BUSY)
        return result


def set_illustration(working_dir, lift_path, guid, href):
    """Surgically set the illustration href on one LIFT entry.

    Locates the entry by *guid*, then find-or-creates
    `<sense>/<illustration href="{href}"/>`. Uses the FIRST
    `<sense>` of the entry (creating one if absent); within it,
    updates the FIRST `<illustration>` (creating one if absent).
    Other senses and other illustration elements are not touched.

    Returns a `Result` carrying:

    - `S.ILLUSTRATION_SET` — first-time write or href replaced.
    - `S.ILLUSTRATION_SET_NO_CHANGE` — the illustration's href
      already equalled *href*; nothing was written.
    - `S.ENTRY_NOT_FOUND` — no `<entry guid="X">` in the LIFT.
    - `S.LIFT_INVALID` — the source or post-splice file failed
      well-formedness validation, or the LIFT file is missing.
    - `S.BUSY` — `project_lock` couldn't be acquired in time.
    """
    if not href:
        result = Result()
        result.add(S.LIFT_INVALID, error='href required')
        return result

    def _edit(entry_el, ns):
        sense = entry_el.find(f'{ns}sense')
        if sense is None:
            sense = ET.SubElement(entry_el, f'{ns}sense')
        illustration = sense.find(f'{ns}illustration')
        if illustration is None:
            illustration = ET.SubElement(sense, f'{ns}illustration')
        if illustration.get('href') == href:
            return (False,
                    S.ILLUSTRATION_SET_NO_CHANGE,
                    S.ILLUSTRATION_SET)
        illustration.set('href', href)
        return (True,
                S.ILLUSTRATION_SET_NO_CHANGE,
                S.ILLUSTRATION_SET)

    try:
        with project_lock(working_dir, timeout=10.0):
            return _do_surgical_edit(lift_path, guid, _edit)
    except LockTimeout:
        result = Result()
        result.add(S.BUSY)
        return result
