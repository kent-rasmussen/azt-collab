"""Phase: NOTES item #4 — fresh GUIDs on template→project import.

Validates ``azt_collabd.projects._mint_fresh_guids``:
- Every ``<entry guid="...">`` gets a fresh UUID-4.
- Every ``ref="..."`` attribute that pointed at one of the
  rewritten guids is updated; refs to non-entry values are left
  alone.
- Non-LIFT / parse-failure input flows through unchanged.
- Templates without guid'd entries flow through unchanged.
"""

import re
import xml.etree.ElementTree as ET

from azt_collabd.projects import _mint_fresh_guids


_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$')


def _entry_guids(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [e.get('guid') for e in root.iter('entry')]


def _ref_values(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [e.get('ref') for e in root.iter() if e.get('ref')]


def test_entry_guids_get_freshened():
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13">'
           b'<entry guid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa">'
           b'<lexical-unit/></entry>'
           b'<entry guid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb">'
           b'<lexical-unit/></entry>'
           b'</lift>')
    out = _mint_fresh_guids(src)
    guids = _entry_guids(out)
    assert len(guids) == 2
    assert guids[0] != 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    assert guids[1] != 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    # Each fresh guid is a syntactically valid UUID.
    for g in guids:
        assert _UUID_RE.match(g), f'not a UUID-4: {g!r}'
    # Two distinct entries get two distinct guids — not the same one
    # blasted across.
    assert guids[0] != guids[1]


def test_relation_refs_follow_renamed_guids():
    """A ``<relation ref="...">`` whose value matches an old entry
    guid must be rewritten to the matching new guid. Critical: a
    template's intra-document relations must remain valid after
    the rename."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13">'
           b'<entry guid="AAA-old"><lexical-unit/>'
           b'<relation type="see-also" ref="BBB-old"/>'
           b'</entry>'
           b'<entry guid="BBB-old"><lexical-unit/></entry>'
           b'</lift>')
    out = _mint_fresh_guids(out_root := src)
    # Build mapping from old -> new by entry order.
    new_guids = _entry_guids(out)
    refs = _ref_values(out)
    # AAA's relation should now point at BBB's NEW guid.
    assert refs == [new_guids[1]]


def test_ref_with_non_entry_value_is_not_rewritten():
    """``ref="sense-id-7"`` is a sense reference, not an entry-guid
    reference. The rewriter must not touch it."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13">'
           b'<entry guid="AAA-old"><lexical-unit/>'
           b'<sense id="sense-id-7"/>'
           b'<note ref="sense-id-7">see this sense</note>'
           b'</entry>'
           b'</lift>')
    out = _mint_fresh_guids(src)
    refs = _ref_values(out)
    # The non-guid ref is preserved verbatim.
    assert refs == ['sense-id-7']


def test_non_lift_template_passes_through_unchanged():
    """A template that's not LIFT (no <entry guid="..."> elements)
    isn't a parse failure but has nothing to rewrite. Output should
    equal input — caller's downstream consumer sees what it would
    have seen pre-0.50.8."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><header><description/></header>'
           b'</lift>')
    out = _mint_fresh_guids(src)
    assert out == src


def test_malformed_xml_passes_through_unchanged():
    """A template that won't parse (truncated XML, served 200 OK
    on an error page) must not raise — caller logs and falls
    through to the original bytes so the downstream LIFT reader
    can produce a more specific error."""
    src = b'<lift><entry guid="AAA"><lexical-unit/></entry>'  # unclosed
    out = _mint_fresh_guids(src)
    assert out == src


def test_idempotence_within_run_produces_distinct_guids():
    """Calling the transform twice on the same source produces
    two distinct sets of new guids — UUID-4 collisions are
    astronomically unlikely. This catches the case where a future
    refactor accidentally seeds the RNG with a stable value."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13">'
           b'<entry guid="AAA"><lexical-unit/></entry>'
           b'</lift>')
    out1 = _mint_fresh_guids(src)
    out2 = _mint_fresh_guids(src)
    assert _entry_guids(out1) != _entry_guids(out2)


def test_large_template_all_entries_freshened():
    """SILCAWL has ~1700 entries; verify the transform scales and
    produces 1700 distinct new guids (no collisions in the run)."""
    n = 200  # smaller for test speed; same shape
    parts = [b'<?xml version="1.0" encoding="UTF-8"?>',
             b'<lift version="0.13">']
    for i in range(n):
        parts.append(
            f'<entry guid="src-{i:04d}"><lexical-unit/></entry>'
            .encode())
    parts.append(b'</lift>')
    src = b''.join(parts)
    out = _mint_fresh_guids(src)
    guids = _entry_guids(out)
    assert len(guids) == n
    # All distinct, all UUID-4 shaped.
    assert len(set(guids)) == n
    for g in guids:
        assert _UUID_RE.match(g)
