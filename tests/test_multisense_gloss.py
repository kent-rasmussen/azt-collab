"""Multi-sense glosses are valid LIFT and must survive the merge's
post-normalize untouched (2026-07-22 CABTAL root cause).

The bare CAWL template ships multiple same-@lang <gloss> nodes per
entry — distinct senses/synonyms (e.g. two <gloss lang="es">:
abdomen, barriga). ``_normalize_entry`` used to treat glosses as
single-per-lang like <form>, falsely annotating every such entry
azt-lift-conflict (1284 false markers in the workshop nml). Glosses
are read-only (no edit UI), so a same-lang gloss "conflict" can
never be a real divergence.
"""

import xml.etree.ElementTree as ET

from azt_collabd.lift_merge import (_normalize_entry, _reconcile_entry_marker,
                                     CONFLICT_ANNOTATION_NAME)


def _entry_two_es_glosses(with_false_annotations=False):
    entry = ET.Element('entry', {'guid': 'g1'})
    sense = ET.SubElement(entry, 'sense')
    for txt, side in (('abdomen', 'ours'), ('barriga', 'theirs')):
        g = ET.SubElement(sense, 'gloss', {'lang': 'es'})
        ET.SubElement(g, 'text').text = txt
        if with_false_annotations:
            a = ET.SubElement(g, 'annotation')
            a.set('name', CONFLICT_ANNOTATION_NAME)
            a.set('value', side)
    return entry


def _gloss_texts(entry):
    return sorted(g.find('text').text for g in entry.iter('gloss'))


def _conflict_count(entry):
    return sum(1 for a in entry.iter('annotation')
               if a.attrib.get('name') == CONFLICT_ANNOTATION_NAME)


def test_distinct_same_lang_glosses_survive_unannotated():
    entry = _entry_two_es_glosses()
    _normalize_entry(entry)
    # Both senses kept; NO conflict annotation manufactured.
    assert _gloss_texts(entry) == ['abdomen', 'barriga']
    assert _conflict_count(entry) == 0


def test_false_gloss_annotations_are_stripped():
    entry = _entry_two_es_glosses(with_false_annotations=True)
    repairs = _normalize_entry(entry)
    assert _gloss_texts(entry) == ['abdomen', 'barriga']
    assert _conflict_count(entry) == 0        # laundered
    assert repairs >= 1                        # reported the repair


def test_idempotent():
    entry = _entry_two_es_glosses(with_false_annotations=True)
    _normalize_entry(entry)
    again = _normalize_entry(entry)            # second pass: clean
    assert again == 0
    assert _gloss_texts(entry) == ['abdomen', 'barriga']
    assert _conflict_count(entry) == 0


def _entry_level_marker(entry, fields):
    a = ET.SubElement(entry, 'annotation')
    a.set('name', CONFLICT_ANNOTATION_NAME)
    a.set('value', 'conflict')
    tr = ET.SubElement(a, 'trait')
    tr.set('name', 'azt-lift-conflict-fields')
    tr.set('value', fields)


def test_orphaned_gloss_entry_marker_removed():
    # gloss-only conflict: after normalize strips the gloss node
    # markers, the entry-level marker naming gloss[...] is orphaned
    # and must be removed (0.54.21).
    entry = _entry_two_es_glosses(with_false_annotations=True)
    _entry_level_marker(entry, 'sense[id=x]/gloss[lang=es]')
    _normalize_entry(entry)
    removed = _reconcile_entry_marker(entry)
    assert removed == 1
    assert _conflict_count(entry) == 0        # nothing left anywhere


def test_entry_marker_kept_when_audio_conflict_survives():
    # An audio node conflict survives normalize → entry-level marker
    # must stay.
    entry = ET.Element('entry', {'guid': 'g3'})
    cit = ET.SubElement(entry, 'citation')
    for fn, side in (('w.m4a', 'ours'), ('w.aac', 'theirs')):
        f = ET.SubElement(cit, 'form', {'lang': 'nml-Zxxx-x-audio'})
        ET.SubElement(f, 'text').text = fn
        a = ET.SubElement(f, 'annotation')
        a.set('name', CONFLICT_ANNOTATION_NAME)
        a.set('value', side)
    _entry_level_marker(entry, 'citation/form[lang=nml-Zxxx-x-audio]')
    _normalize_entry(entry)
    assert _reconcile_entry_marker(entry) == 0   # kept
    assert any(a.attrib.get('value') == 'conflict'
               for a in entry.findall('annotation'))


def _entry_trait_value(entry):
    for a in entry.findall('annotation'):
        if (a.attrib.get('name') == CONFLICT_ANNOTATION_NAME
                and a.attrib.get('value') == 'conflict'):
            for t in a.findall('trait'):
                if t.attrib.get('name') == 'azt-lift-conflict-fields':
                    return t.attrib.get('value', '')
    return None


def test_mixed_entry_marker_trimmed_not_removed():
    # Entry with BOTH a false gloss conflict (stripped by normalize)
    # and a real audio conflict (survives). The entry-level marker
    # must stay, but its fields trait must drop the stale gloss link
    # and keep only the audio (0.54.22).
    entry = ET.Element('entry', {'guid': 'g4'})
    sense = ET.SubElement(entry, 'sense')
    for txt, side in (('abdomen', 'ours'), ('barriga', 'theirs')):
        g = ET.SubElement(sense, 'gloss', {'lang': 'es'})
        ET.SubElement(g, 'text').text = txt
        a = ET.SubElement(g, 'annotation')
        a.set('name', CONFLICT_ANNOTATION_NAME)
        a.set('value', side)
    cit = ET.SubElement(entry, 'citation')
    for fn, side in (('w.m4a', 'ours'), ('w.aac', 'theirs')):
        f = ET.SubElement(cit, 'form', {'lang': 'nml-Zxxx-x-audio'})
        ET.SubElement(f, 'text').text = fn
        a = ET.SubElement(f, 'annotation')
        a.set('name', CONFLICT_ANNOTATION_NAME)
        a.set('value', side)
    _entry_level_marker(
        entry, 'sense/gloss[lang=es],citation/form[lang=nml-Zxxx-x-audio]')

    _normalize_entry(entry)          # strips the gloss node markers
    _reconcile_entry_marker(entry)

    trait = _entry_trait_value(entry)
    assert trait is not None                 # marker kept (audio survives)
    assert 'gloss' not in trait              # stale gloss link dropped
    assert 'nml-Zxxx-x-audio' in trait       # audio field retained


def _entry_two_audio(older='w.m4a', newer='w.aac'):
    entry = ET.Element('entry', {'guid': 'ga'})
    cit = ET.SubElement(entry, 'citation')
    for fn, side in ((older, 'ours'), (newer, 'theirs')):
        f = ET.SubElement(cit, 'form', {'lang': 'nml-Zxxx-x-audio'})
        ET.SubElement(f, 'text').text = fn
        a = ET.SubElement(f, 'annotation')
        a.set('name', CONFLICT_ANNOTATION_NAME)
        a.set('value', side)
    return entry


def _audio_files(entry):
    return sorted(f.find('text').text for f in entry.iter('form')
                  if f.get('lang') == 'nml-Zxxx-x-audio')


def test_audio_last_wins_with_resolver():
    entry = _entry_two_audio('w.m4a', 'w.aac')
    recency = {'w.m4a': 100, 'w.aac': 200}   # .aac is newer
    _normalize_entry(entry, audio_recency=lambda fn: recency.get(fn))
    assert _audio_files(entry) == ['w.aac']          # newest kept
    assert _conflict_count(entry) == 0               # marker stripped


def test_audio_annotates_without_resolver():
    entry = _entry_two_audio('w.m4a', 'w.aac')
    _normalize_entry(entry)                          # no resolver
    assert _audio_files(entry) == ['w.aac', 'w.m4a']  # both kept
    assert _conflict_count(entry) == 2               # annotated


def test_audio_uncommitted_now_beats_committed():
    # "Undefined is NOW" (0.54.26): the daemon's resolver returns
    # float('inf') for a working-tree take not yet committed. inf must
    # beat any committed commit-time, so the fresh take wins with the
    # marker stripped — no spurious conflict annotation.
    entry = _entry_two_audio('old_committed.m4a', 'fresh_now.aac')
    recency = {'old_committed.m4a': 100, 'fresh_now.aac': float('inf')}
    _normalize_entry(entry, audio_recency=lambda fn: recency.get(fn))
    assert _audio_files(entry) == ['fresh_now.aac']
    assert _conflict_count(entry) == 0


def test_audio_last_wins_deterministic_idempotent():
    recency = {'w.m4a': 100, 'w.aac': 200}
    r = lambda fn: recency.get(fn)
    e1 = _entry_two_audio()
    _normalize_entry(e1, audio_recency=r)
    # second pass: single form, nothing to do
    assert _normalize_entry(e1, audio_recency=r) == 0
    # older-wins case resolves to the older file, deterministically
    e2 = _entry_two_audio()
    _normalize_entry(e2, audio_recency=lambda fn: {'w.m4a': 300,
                                                   'w.aac': 200}.get(fn))
    assert _audio_files(e2) == ['w.m4a']


def test_identical_same_lang_glosses_still_collapse():
    # Byte-identical duplicates are still dedup'd (not the bug case).
    entry = ET.Element('entry', {'guid': 'g2'})
    sense = ET.SubElement(entry, 'sense')
    for _ in range(3):
        g = ET.SubElement(sense, 'gloss', {'lang': 'es'})
        ET.SubElement(g, 'text').text = 'abdomen'
    _normalize_entry(entry)
    assert _gloss_texts(entry) == ['abdomen']
    assert _conflict_count(entry) == 0
