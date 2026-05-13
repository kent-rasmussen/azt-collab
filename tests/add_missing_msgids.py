#!/usr/bin/env python3
"""Append every missing translatable msgid to
``azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po``
with an empty ``msgstr``. Idempotent: running it on a fully-
covered catalog is a no-op.

The companion test ``test_translation_coverage.py`` AST-walks
every ``_(...)`` / ``_tr(...)`` / ``tr(...)`` call in the
codebase and asserts each msgid is present in the .po. When new
translatable strings get added without corresponding .po
entries (the common drift pattern — string tweaked in source,
.po not touched), the test fails. This script closes the gap
mechanically: it adds the missing msgids with empty
``msgstr ""`` so gettext falls back to the English at runtime
(same behaviour the strings had before the addition — empty
msgstr is functionally identical to "no entry" for a French
user, but the catalog now has a place for the maintainer to
type a real translation later).

Run from the azt-collab repo root::

    python tests/add_missing_msgids.py

Prints the count appended and the .po path written to. Re-run
the coverage test after to confirm zero drift.

Why this is a script rather than a test fixture: the runtime
fix is to *write* msgstrs (translation work), not auto-emit
them on the test path. The test should stay strict; the script
is the deliberate maintainer action that says "I'm acknowledging
these drifted, leaving placeholders, and will translate them
when I get to French QA."
"""

import os
import sys

# Reuse the test's AST-walk + .po parser so this script and the
# test always agree on what counts as a translatable msgid and
# what counts as "already in the catalog."
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from test_translation_coverage import (  # noqa: E402
    _load_po_msgids,
    _walk_python_callsites,
    _walk_kv_callsites,
    PO_PATH,
)


def _po_quote(s):
    """Escape ``s`` into a single-line .po-quoted form. The .po
    format permits multi-line string continuations, but for new
    entries we emit a single line — gettext handles long strings
    fine, and single-line keeps the diff readable."""
    out = ['"']
    for ch in s:
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\t':
            out.append('\\t')
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def main():
    msgids_in_po = _load_po_msgids(PO_PATH)
    missing = {}
    for path, lineno, msgid in _walk_python_callsites():
        if msgid in msgids_in_po:
            continue
        missing.setdefault(msgid, (path, lineno))
    for path, lineno, msgid in _walk_kv_callsites():
        if msgid in msgids_in_po:
            continue
        missing.setdefault(msgid, (path, lineno))
    if not missing:
        print(f'no missing msgids — {os.path.basename(PO_PATH)} '
              f'is fully covered')
        return 0
    # Append a marked block so the maintainer can find them
    # quickly. Sort by first-seen file:line for a stable order.
    entries = sorted(missing.items(),
                     key=lambda kv: (kv[1][0], kv[1][1]))
    block = ['',
             '# ── Drift placeholders appended by '
             'tests/add_missing_msgids.py ──',
             '# Translate the msgstr values when ready; '
             'empty msgstr falls back to the msgid '
             '(English) at runtime, same as the pre-add behaviour.',
             '']
    for msgid, (path, lineno) in entries:
        rel = os.path.relpath(path, os.path.dirname(_HERE))
        block.append(f'#: {rel}:{lineno}')
        block.append(f'msgid {_po_quote(msgid)}')
        block.append('msgstr ""')
        block.append('')
    with open(PO_PATH, 'a', encoding='utf-8') as f:
        f.write('\n'.join(block))
    print(f'appended {len(missing)} placeholder entr'
          f'{"y" if len(missing) == 1 else "ies"} to '
          f'{PO_PATH}')
    print('run `pytest tests/test_translation_coverage.py -q` '
          'to confirm zero drift')
    return 0


if __name__ == '__main__':
    sys.exit(main())
