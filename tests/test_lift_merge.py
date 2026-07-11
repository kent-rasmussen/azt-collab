"""Regression tests for ``azt_collabd.lift_merge.three_way_merge``.

History the test suite locks in:

- **Document-order preservation** (0.34.1, field repro on
  ``sw-US-x-kent``): pre-0.34.1 the merge walked
  ``sorted(all_guids)`` and rewrote the file in guid-alphabetical
  order on every merge.
- **Truncation guard** (0.35.1, field repro 2026-05-12): if ``ours``
  arrives with dramatically fewer entries than ``theirs`` and
  ``base`` (peer-side write race / partial commit / sandbox
  hiccup), refuse the destructive merge and keep the larger side
  intact.
- **Parse-error guard** (0.35.2): if ours or theirs failed to
  parse (mid-write truncation that breaks XML, etc.), refuse the
  destructive merge entirely.
- **Empty-side guard for small projects** (0.35.2): one side
  going to zero entries while base is non-empty trips the guard
  regardless of project size.
- **Field-level conflict resolution** (0.35.2, the v3 recursive
  merge): conflicts are expressed at the narrowest LIFT-multi
  level containing them. A same-lang text conflict produces
  two same-lang ``<form>`` siblings, not two whole entries.
"""

import xml.etree.ElementTree as ET

from azt_collabd import lift_merge as lm


def _lift(entries_xml):
    """Wrap a list of ``<entry>`` XML strings in a minimal LIFT
    document."""
    body = ''.join(entries_xml)
    return f'<lift version="0.13">{body}</lift>'.encode('utf-8')


def _entry(guid, citation='cat'):
    return (f'<entry guid="{guid}" id="x_{guid}">'
            f'<lexical-unit><form lang="en">'
            f'<text>{citation}</text></form></lexical-unit>'
            f'</entry>')


def _entry_with(guid, *, lex='cat', extra=''):
    return (f'<entry guid="{guid}" id="x_{guid}">'
            f'<lexical-unit><form lang="en">'
            f'<text>{lex}</text></form></lexical-unit>'
            f'{extra}'
            f'</entry>')


# ── Document order preservation ──────────────────────────────────────────

def test_three_way_merge_preserves_ours_document_order():
    base = _lift([_entry('aaa'), _entry('bbb'), _entry('ccc')])
    ours = _lift([_entry('aaa'), _entry('bbb'), _entry('ccc')])
    theirs = _lift([_entry('aaa'), _entry('bbb'), _entry('ccc')])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    guids = [e.attrib['guid'] for e in root.findall('entry')]
    assert guids == ['aaa', 'bbb', 'ccc']


def test_three_way_merge_anchors_on_ours_order_not_alphabetical():
    """If ``ours`` orders by zzz/aaa/mmm and there are no changes,
    the merge result MUST preserve that order, not re-sort."""
    base = _lift([_entry('aaa'), _entry('mmm'), _entry('zzz')])
    ours = _lift([_entry('zzz'), _entry('aaa'), _entry('mmm')])
    theirs = _lift([_entry('aaa'), _entry('mmm'), _entry('zzz')])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    guids = [e.attrib['guid'] for e in root.findall('entry')]
    assert guids == ['zzz', 'aaa', 'mmm']


# ── Truncation guard ──────────────────────────────────────────────────────

def test_truncation_guard_keeps_larger_side_intact():
    """``ours`` arrives with 1 entry, ``theirs`` and ``base`` have
    60. The merge MUST NOT honour the 'deletion' and produce a
    1-entry result — it must keep ``theirs`` intact and surface a
    Conflict so the peer can route to the user."""
    big_entries = [_entry(f'{i:040x}') for i in range(60)]
    base = _lift(big_entries)
    theirs = _lift(big_entries)
    ours = _lift([_entry(f'{0:040x}')])     # truncated to 1 entry
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    guids = [e.attrib['guid'] for e in root.findall('entry')]
    assert len(guids) == 60
    assert len(out.conflicts) == 1
    c = out.conflicts[0]
    assert c.kind == 'truncation-suspected'
    assert any('truncated' in f for f in c.fields)
    assert any('theirs-kept-intact' in f for f in c.fields)


def test_truncation_guard_small_project_legitimate_delete_does_not_trip():
    """A 5-entry project that legitimately drops to 3 entries
    must NOT trip the guard — only dramatic asymmetry does."""
    five = [_entry(f'{i:040x}') for i in range(5)]
    three = five[:3]
    base = _lift(five)
    ours = _lift(three)              # legitimate delete of 2
    theirs = _lift(five)
    out = lm.three_way_merge(base, ours, theirs)
    assert not any(c.kind == 'truncation-suspected'
                   for c in out.conflicts)


def test_truncation_guard_small_project_empty_side_does_trip():
    """A small project where ours goes to ZERO entries while
    theirs+base have entries DOES trip the guard — the
    empty-side case is too suspicious to ignore even at small
    project sizes."""
    base = _lift([_entry('aaa'), _entry('bbb')])
    ours = _lift([])                  # truncated to empty
    theirs = _lift([_entry('aaa'), _entry('bbb')])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    guids = [e.attrib['guid'] for e in root.findall('entry')]
    assert guids == ['aaa', 'bbb']     # theirs kept intact
    assert len(out.conflicts) == 1
    assert out.conflicts[0].kind == 'truncation-suspected'


# ── Parse-error guard ────────────────────────────────────────────────────

def test_parse_error_in_ours_keeps_theirs():
    """A mid-write truncation that leaves invalid XML in ``ours``
    must be detected at parse time, not silently masked into an
    empty doc that gets merged destructively."""
    base = _lift([_entry('aaa'), _entry('bbb')])
    # Truncated: tag started but never closed.
    ours = b'<lift version="0.13"><entry guid="aaa"><lexical-'
    theirs = _lift([_entry('aaa'), _entry('bbb')])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    guids = [e.attrib['guid'] for e in root.findall('entry')]
    assert guids == ['aaa', 'bbb']
    assert len(out.conflicts) == 1
    c = out.conflicts[0]
    assert c.kind == 'parse-error'
    assert any('XML parse error' in f for f in c.fields)
    assert any('theirs-kept-intact' in f for f in c.fields)


def test_parse_error_in_theirs_keeps_ours():
    base = _lift([_entry('aaa')])
    ours = _lift([_entry('aaa'), _entry('bbb')])    # we added bbb
    theirs = b'<lift version="0.13"><entry guid='   # truncated
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    guids = [e.attrib['guid'] for e in root.findall('entry')]
    assert guids == ['aaa', 'bbb']                  # ours kept
    assert out.conflicts[0].kind == 'parse-error'
    assert any('ours-kept-intact' in f for f in out.conflicts[0].fields)


# ── v3 field-level conflict resolution ───────────────────────────────────

def test_same_lang_lexical_text_conflict_duplicates_at_form_level():
    """A same-lang ``<text>`` conflict (the deepest practical
    case) escalates through the form's parent (singleton
    lexical-unit) and lands at the FORM level — which IS
    multi-allowed inside lexical-unit. Result: ONE entry, one
    lexical-unit, two same-lang form siblings each carrying its
    own text and an annotation."""
    base = _lift([_entry('aaa', citation='cat')])
    ours = _lift([_entry('aaa', citation='kat')])
    theirs = _lift([_entry('aaa', citation='qat')])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    entries = root.findall('entry')
    assert len(entries) == 1, \
        f'expected single entry, got {len(entries)}'
    entry = entries[0]
    assert entry.attrib['guid'] == 'aaa', \
        'guid must be preserved (no synthetic suffix in v3)'
    lex_units = entry.findall('lexical-unit')
    assert len(lex_units) == 1, \
        'lexical-unit is singleton; must not duplicate'
    forms = lex_units[0].findall('form')
    assert len(forms) == 2, \
        f'conflict expressed at form level, got {len(forms)} forms'
    # Both forms are same-lang.
    assert all(f.attrib['lang'] == 'en' for f in forms)
    # Each carries an annotation marking its side.
    sides = sorted(f.find('annotation').attrib['value']
                   for f in forms)
    assert sides == ['ours', 'theirs']
    # Texts are the conflicting values.
    texts = sorted(f.find('text').text for f in forms)
    assert texts == ['kat', 'qat']


def test_entry_level_marker_lists_conflict_paths():
    """An entry-level azt-lift-conflict annotation summarises
    where the conflicts live, so a peer-side resolver can jump
    to them without re-walking."""
    base = _lift([_entry('aaa', citation='cat')])
    ours = _lift([_entry('aaa', citation='kat')])
    theirs = _lift([_entry('aaa', citation='qat')])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    entry = root.find('entry')
    # Entry-level marker.
    markers = [a for a in entry.findall('annotation')
               if a.attrib.get('name') == lm.CONFLICT_ANNOTATION_NAME]
    assert markers, 'entry-level conflict marker missing'
    marker = markers[0]
    assert marker.attrib['value'] == 'conflict'
    trait = marker.find('trait')
    assert trait is not None, 'conflict-fields trait missing'
    assert trait.attrib['name'] == lm.CONFLICT_FIELDS_TRAIT
    # Path should point at the form level where the conflict
    # actually landed.
    value = trait.attrib['value']
    assert 'lexical-unit' in value
    assert 'form' in value


def test_one_sided_change_no_conflict():
    """Only ours changed; theirs identical to base. Result: take
    ours's version cleanly, no conflict marker, no duplication."""
    base = _lift([_entry('aaa', citation='cat')])
    ours = _lift([_entry('aaa', citation='kat')])    # only ours
    theirs = _lift([_entry('aaa', citation='cat')])  # = base
    out = lm.three_way_merge(base, ours, theirs)
    assert out.conflicts == []
    root = ET.fromstring(out.merged_bytes)
    entry = root.find('entry')
    forms = entry.findall('lexical-unit/form')
    assert len(forms) == 1
    assert forms[0].find('text').text == 'kat'


def test_sense_gloss_conflict_duplicates_at_gloss_level():
    """Both peers added the same sense id with different gloss
    text. Narrowest-multi rule: sense id is preserved (one sense),
    gloss is multi-in-sense → duplicate at gloss level. Result:
    one entry, one sense with the original id, two same-lang
    glosses each annotated."""
    extra_ours = ('<sense id="s1"><gloss lang="en">'
                  '<text>cat</text></gloss></sense>')
    extra_theirs = ('<sense id="s1"><gloss lang="en">'
                    '<text>feline</text></gloss></sense>')
    base = _lift([_entry_with('aaa')])
    ours = _lift([_entry_with('aaa', extra=extra_ours)])
    theirs = _lift([_entry_with('aaa', extra=extra_theirs)])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    entries = root.findall('entry')
    assert len(entries) == 1
    entry = entries[0]
    senses = entry.findall('sense')
    assert len(senses) == 1
    sense = senses[0]
    assert sense.attrib['id'] == 's1', \
        'sense id should be preserved (no synthetic suffix)'
    glosses = sense.findall('gloss')
    assert len(glosses) == 2, \
        'conflict expressed at gloss level (multi in sense)'
    assert all(g.attrib['lang'] == 'en' for g in glosses)
    sides = sorted(g.find('annotation').attrib['value']
                   for g in glosses)
    assert sides == ['ours', 'theirs']
    texts = sorted(g.find('text').text for g in glosses)
    assert texts == ['cat', 'feline']


def test_media_conflict_duplicates_at_media_level():
    """Same-position media inside pronunciation differs in href.
    Pronunciation has no distinguishing attrib (so it positionally
    pairs across sides); media is multi-in-pronunciation per the
    LIFT schema. Result: one pronunciation, two media siblings each
    annotated. Demonstrates the rule applies even to add-add cases
    where the same positional bucket sees different content from
    both peers."""
    extra_ours = '<pronunciation><media href="audio/X.wav"/></pronunciation>'
    extra_theirs = '<pronunciation><media href="audio/Y.wav"/></pronunciation>'
    base = _lift([_entry_with('aaa')])
    ours = _lift([_entry_with('aaa', extra=extra_ours)])
    theirs = _lift([_entry_with('aaa', extra=extra_theirs)])
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    entries = root.findall('entry')
    assert len(entries) == 1
    entry = entries[0]
    prons = entry.findall('pronunciation')
    assert len(prons) == 1, \
        'one pronunciation; conflict narrowed to media inside'
    medias = prons[0].findall('media')
    assert len(medias) == 2
    sides = sorted(m.find('annotation').attrib['value']
                   for m in medias)
    assert sides == ['ours', 'theirs']
    hrefs = sorted(m.attrib['href'] for m in medias)
    assert hrefs == ['audio/X.wav', 'audio/Y.wav']


def test_modify_delete_keeps_ours_as_entry_annotation():
    """Entry-level conflict where ours changed and theirs
    deleted (one specific entry, not the whole project — the
    whole-project-deletion case trips the empty-side guard and
    is tested separately). Single entry survives, annotated as
    'ours'. No sub-element duplication (the conflict is
    whole-entry, not field-decomposable)."""
    base = _lift([_entry('aaa', citation='cat'),
                  _entry('bbb', citation='dog')])
    ours = _lift([_entry('aaa', citation='cat'),
                  _entry('bbb', citation='puppy')])   # we modified bbb
    theirs = _lift([_entry('aaa', citation='cat')])   # they deleted bbb
    out = lm.three_way_merge(base, ours, theirs)
    modify_delete = [c for c in out.conflicts
                     if c.kind == 'modify-delete']
    assert len(modify_delete) == 1
    assert modify_delete[0].guid == 'bbb'
    assert modify_delete[0].fields == []


def test_add_add_with_same_content_no_duplication():
    """Both peers added the same guid with identical bytes — happy
    path, no conflict, single entry survives."""
    base = _lift([])
    ours = _lift([_entry('aaa', citation='cat')])
    theirs = _lift([_entry('aaa', citation='cat')])
    out = lm.three_way_merge(base, ours, theirs)
    assert out.conflicts == []
    root = ET.fromstring(out.merged_bytes)
    assert len(root.findall('entry')) == 1


# ── The baf-style 1700+1700→1 case (reopened bug, 2026-05-12) ────────────

def test_full_sides_one_entry_differs_keeps_all_entries():
    """The bug shape that reopened the closed merge note: two
    healthy sides (~1700 entries each), only ONE entry differs
    between them. Pre-fix daemons produced a 1-entry merge result
    (somehow dropping every entry that was unchanged on both
    sides). The current merger MUST produce all 100 entries
    (1 with a conflict annotation, 99 unchanged).

    The number 100 vs the field 1700 is a test convenience; the
    algorithm doesn't care about scale, and 100 keeps the test
    fixture readable. The output-side guard
    (``_looks_catastrophic_output``) catches the 1-entry result
    regardless of scale anyway."""
    n = 100
    entries_base = [_entry(f'{i:040x}', citation=f'word{i}')
                    for i in range(n)]
    entries_ours = list(entries_base)
    # Modify entry 50 in ours.
    entries_ours[50] = _entry(f'{50:040x}', citation='word50-ours')
    entries_theirs = list(entries_base)
    # Modify entry 50 differently in theirs.
    entries_theirs[50] = _entry(f'{50:040x}', citation='word50-theirs')

    base = _lift(entries_base)
    ours = _lift(entries_ours)
    theirs = _lift(entries_theirs)

    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    out_entries = root.findall('entry')
    # All 100 entries must survive. The conflicting one is now
    # field-level inside (single entry, two forms inside), so total
    # entries == base count.
    assert len(out_entries) == n, \
        f'expected {n} entries, got {len(out_entries)} — the '\
        f'reopened-bug shape that prompted the output guard'
    # Confirm there IS a conflict somewhere (sanity).
    assert any(c.kind == 'modify-modify' for c in out.conflicts)


def test_catastrophic_output_guard_fires_directly():
    """Direct test of ``_looks_catastrophic_output``. Independent
    of whether the rest of the merger can produce this shape —
    the guard exists as a defense even against unknown future
    bugs that drop entries."""
    # Healthy inputs, catastrophic output: trip.
    diag = lm._looks_catastrophic_output(
        base_count=1700, ours_count=1702, theirs_count=1700,
        merged_count=1)
    assert diag != ''
    assert '1 entries' in diag

    # Healthy inputs, healthy output: skip.
    assert lm._looks_catastrophic_output(
        base_count=1700, ours_count=1700, theirs_count=1700,
        merged_count=1700) == ''

    # Half-of-base output (legitimate 50% delete): skip.
    assert lm._looks_catastrophic_output(
        base_count=1700, ours_count=1700, theirs_count=1700,
        merged_count=850) == ''

    # Tiny project: skip (small-scale changes have wide variance).
    assert lm._looks_catastrophic_output(
        base_count=5, ours_count=5, theirs_count=5,
        merged_count=1) == ''

    # An input was already tiny (input-side guard's territory):
    # skip output guard to avoid double-attribution.
    assert lm._looks_catastrophic_output(
        base_count=1700, ours_count=1, theirs_count=1700,
        merged_count=1) == ''


# ── Duplicate same-lang forms: the 'wife' ×29 case (2026-07-10) ──────────
#
# Field repro: entry 9ae43c82 accumulated 29 duplicate
# ``<form lang="en-x-py">`` nodes inside ONE verification field —
# one ['V1=ai','C2=f'] plus 28 identical ['V1=ai','C1=wh'] — all
# on one computer. Two engines: (1) positional same-key pairing
# mispaired lists of different lengths and kept the overhang
# unconditionally (one extra copy per merge); (2) one-sided-missing
# children were kept without consulting base, resurrecting deleted
# duplicates on any merge against a stale branch. The tests below
# lock in the fixes: content-first pairing, base-honored deletes,
# the azt-mirroring verification union, and the post-merge
# invariant (no un-annotated duplicate same-lang forms, ever).

_VFTYPE = 'CVC lc verification'


def _vform(codes_repr, lang='en-x-py'):
    return f'<form lang="{lang}"><text>{codes_repr}</text></form>'


def _ventry(guid, forms_xml, ftype=_VFTYPE, extra=''):
    return (f'<entry guid="{guid}" id="x_{guid}">'
            f'<lexical-unit><form lang="en">'
            f'<text>wife</text></form></lexical-unit>'
            f'<field type="{ftype}">{forms_xml}</field>'
            f'{extra}'
            f'</entry>')


def _vfield_forms(merged_bytes, ftype=_VFTYPE):
    root = ET.fromstring(merged_bytes)
    return root.findall(f'entry/field[@type="{ftype}"]/form')


def test_verification_both_changed_unions_content():
    """Both sides changed the same single-form verification field:
    the merge unions the CODE LISTS (mirroring azt
    ``Field.consolidate_forms_by_lang``) instead of keeping two
    same-lang form nodes. No conflict survives — the union is a
    deterministic auto-resolution."""
    base = _lift([_ventry('aaa', _vform("['V1=ai']"))])
    ours = _lift([_ventry('aaa', _vform("['V1=ai', 'C2=f']"))])
    theirs = _lift([_ventry('aaa', _vform("['V1=ai', 'C1=wh']"))])
    out = lm.three_way_merge(base, ours, theirs)
    forms = _vfield_forms(out.merged_bytes)
    assert len(forms) == 1, \
        f'expected ONE unioned form, got {len(forms)}'
    assert forms[0].find('text').text == \
        str(['V1=ai', 'C2=f', 'C1=wh'])
    # Union resolved the divergence — nothing left to conflict.
    assert out.conflicts == []
    assert out.repairs >= 1
    # No conflict annotations anywhere in the result.
    root = ET.fromstring(out.merged_bytes)
    assert not [a for a in root.iter('annotation')
                if a.attrib.get('name') == lm.CONFLICT_ANNOTATION_NAME]


def test_verification_conflicting_check_dropped():
    """A check verified to DIFFERENT values on the two sides is
    dropped entirely (must re-verify) — same semantics as azt's
    consolidate_forms_by_lang, so the two layers converge on
    identical bytes."""
    base = _lift([_entry_with('aaa', lex='wife')])
    ours = _lift([_ventry('aaa', _vform("['V1=ai', 'C2=f']"))])
    theirs = _lift([_ventry('aaa', _vform("['V1=e', 'C2=f']"))])
    out = lm.three_way_merge(base, ours, theirs)
    forms = _vfield_forms(out.merged_bytes)
    assert len(forms) == 1
    assert forms[0].find('text').text == str(['C2=f'])


def test_wife_multiplication_stays_bounded_and_converges():
    """The ×29 engine: repeated weak-base merges of the same
    divergent pair (the atomic-recovery shape, base=b'') must NOT
    grow the form count — pre-fix each pass appended one more
    copy. The output must also converge: after the first merge,
    re-merging yields byte-identical output (idempotence)."""
    theirs = _lift([_ventry('aaa', _vform("['V1=ai', 'C1=wh']"))])
    state = _lift([_ventry('aaa', _vform("['V1=ai', 'C2=f']"))])
    seen_states = []
    for _ in range(6):
        out = lm.three_way_merge(b'', state, theirs)
        state = out.merged_bytes
        seen_states.append(state)
        forms = _vfield_forms(state)
        assert len(forms) == 1, \
            f'form count grew to {len(forms)} — the ×29 bug shape'
        assert forms[0].find('text').text == \
            str(['V1=ai', 'C2=f', 'C1=wh'])
    # Idempotence: from the second pass on, output is stable.
    assert seen_states[1] == seen_states[2] == seen_states[-1]


def test_polluted_input_self_heals_on_any_merge():
    """A LIFT that ALREADY carries the wife-style pollution (1 + 28
    identical duplicate forms, no annotations) is repaired by the
    post-merge invariant sweep even when the merge itself has
    nothing to do (both sides identical)."""
    forms = _vform("['V1=ai', 'C2=f']") \
        + _vform("['V1=ai', 'C1=wh']") * 28
    polluted = _lift([_ventry('aaa', forms)])
    out = lm.three_way_merge(b'', polluted, polluted)
    merged_forms = _vfield_forms(out.merged_bytes)
    assert len(merged_forms) == 1, \
        f'29 duplicate forms must heal to 1, got {len(merged_forms)}'
    assert merged_forms[0].find('text').text == \
        str(['V1=ai', 'C2=f', 'C1=wh'])
    assert out.repairs >= 1


def test_child_level_delete_is_honored():
    """Ours deleted a gloss that theirs left untouched (while
    theirs edited another gloss, forcing the recursive walk). The
    deleted gloss must NOT be resurrected — pre-fix, one-sided
    children were kept without consulting base."""
    two_glosses = ('<sense id="s1">'
                   '<gloss lang="en"><text>cat</text></gloss>'
                   '<gloss lang="fr"><text>chat</text></gloss>'
                   '</sense>')
    en_only = ('<sense id="s1">'
               '<gloss lang="en"><text>cat</text></gloss>'
               '</sense>')
    edited = ('<sense id="s1">'
              '<gloss lang="en"><text>feline</text></gloss>'
              '<gloss lang="fr"><text>chat</text></gloss>'
              '</sense>')
    base = _lift([_entry_with('aaa', extra=two_glosses)])
    ours = _lift([_entry_with('aaa', extra=en_only)])      # deleted fr
    theirs = _lift([_entry_with('aaa', extra=edited)])     # edited en
    out = lm.three_way_merge(base, ours, theirs)
    root = ET.fromstring(out.merged_bytes)
    glosses = root.findall('entry/sense/gloss')
    langs = [g.attrib['lang'] for g in glosses]
    assert langs == ['en'], \
        f'fr gloss was deleted by ours and unchanged in theirs — ' \
        f'must stay deleted, got {langs}'
    assert glosses[0].find('text').text == 'feline'


def test_consolidated_side_wins_against_stale_polluted_branch():
    """After a repair consolidated the duplicates (ours), a merge
    against a stale still-polluted branch (theirs == base except
    for an unrelated edit) must keep the single consolidated form
    — not resurrect the duplicate pile."""
    polluted_forms = (_vform("['V1=ai', 'C2=f']")
                      + _vform("['V1=ai', 'C1=wh']") * 2)
    union_form = _vform(str(['V1=ai', 'C2=f', 'C1=wh']))
    base = _lift([_ventry('aaa', polluted_forms)])
    ours = _lift([_ventry('aaa', union_form)])
    # theirs: same polluted field, but changed the lexical-unit so
    # the entry differs and the recursive walk actually runs.
    theirs = _lift([_ventry('aaa', polluted_forms)
                    .replace('wife', 'woman')])
    out = lm.three_way_merge(base, ours, theirs)
    forms = _vfield_forms(out.merged_bytes)
    assert len(forms) == 1, \
        f'stale branch resurrected duplicates: {len(forms)} forms'
    assert forms[0].find('text').text == \
        str(['V1=ai', 'C2=f', 'C1=wh'])
    # Theirs's unrelated edit was taken.
    root = ET.fromstring(out.merged_bytes)
    assert root.find('entry/lexical-unit/form/text').text == 'woman'


def test_divergent_unannotated_duplicates_get_annotated():
    """Non-verification same-lang duplicates with DIFFERENT content
    (illegal multitext state from historical pollution) are forced
    into the visible annotated-conflict-pair shape rather than
    left silently shadowing each other."""
    dup = ('<entry guid="aaa" id="x_aaa"><lexical-unit>'
           '<form lang="en"><text>kat</text></form>'
           '<form lang="en"><text>qat</text></form>'
           '</lexical-unit></entry>')
    doc = _lift([dup])
    out = lm.three_way_merge(b'', doc, doc)
    root = ET.fromstring(out.merged_bytes)
    forms = root.findall('entry/lexical-unit/form')
    assert len(forms) == 2
    sides = sorted(f.find('annotation').attrib['value']
                   for f in forms)
    assert sides == ['ours', 'theirs']


def test_identical_unannotated_duplicate_glosses_dedupe():
    """Identical same-lang duplicates (the wife shape, but for a
    sense gloss) collapse to the document-first node."""
    dup = ('<sense id="s1">'
           '<gloss lang="en"><text>cat</text></gloss>'
           '<gloss lang="en"><text>cat</text></gloss>'
           '</sense>')
    doc = _lift([_entry_with('aaa', extra=dup)])
    out = lm.three_way_merge(b'', doc, doc)
    root = ET.fromstring(out.merged_bytes)
    glosses = root.findall('entry/sense/gloss')
    assert len(glosses) == 1
    assert glosses[0].find('text').text == 'cat'


def test_shared_pollution_is_repair_not_conflict():
    """Entries that are IDENTICAL on both sides but carry legacy
    duplicate same-lang glosses get sweep-annotated as REPAIRS,
    not conflicts — nothing diverged between these two devices.
    Pre-0.54.4 every merge of such a database reported
    ``conflicts=~301`` forever (field repro 2026-07-11), and would
    have kept doing so on matched versions because the canon-equal
    path strips the prior round's annotations before the sweep
    re-adds them."""
    dup = ('<sense id="s1">'
           '<gloss lang="swh"><text>mke</text></gloss>'
           '<gloss lang="swh"><text>mwanamke</text></gloss>'
           '</sense>')
    base = _lift([_entry_with('aaa')])
    doc = _lift([_entry_with('aaa', extra=dup)])
    out = lm.three_way_merge(base, doc, doc)
    assert out.conflicts == [], \
        'identical sides cannot conflict — shared pollution is a repair'
    assert out.repairs >= 1
    root = ET.fromstring(out.merged_bytes)
    glosses = root.findall('entry/sense/gloss')
    assert len(glosses) == 2, \
        'divergent copies stay (annotated) — repair is visible, not lossy'
    # Sanity: a GENUINE divergence on the same polluted entry still
    # reports a conflict.
    ours = _lift([_entry_with('aaa', lex='kat', extra=dup)])
    theirs = _lift([_entry_with('aaa', lex='qat', extra=dup)])
    out2 = lm.three_way_merge(base, ours, theirs)
    assert any(c.kind == 'modify-modify' for c in out2.conflicts)


def test_conflict_pair_remerge_converges_no_growth():
    """Re-merging a result that carries an annotated conflict pair
    against the same theirs must not grow the pair (no third
    copy), and must reach a stable fixed point."""
    base = _lift([_entry('aaa', citation='cat')])
    ours = _lift([_entry('aaa', citation='kat')])
    theirs = _lift([_entry('aaa', citation='qat')])
    m1 = lm.three_way_merge(base, ours, theirs).merged_bytes
    m2 = lm.three_way_merge(base, m1, theirs).merged_bytes
    m3 = lm.three_way_merge(base, m2, theirs).merged_bytes
    for label, m in (('m1', m1), ('m2', m2), ('m3', m3)):
        forms = ET.fromstring(m).findall('entry/lexical-unit/form')
        assert len(forms) == 2, \
            f'{label}: conflict pair grew to {len(forms)} forms'
        texts = sorted(f.find('text').text for f in forms)
        assert texts == ['kat', 'qat']
    assert m2 == m3, 're-merge must reach a fixed point'
