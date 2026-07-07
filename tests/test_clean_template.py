"""Validates ``azt_collabd.projects._clean_template`` — the server-side
per-language pruning applied once on template→project import
(``create_from_template``, right after ``_mint_fresh_guids``).

Host-decided rules (see the function docstring / CHANGELOG 0.52.32 +
0.52.33):

1. lexical-unit — keep only ``<form lang=vernlang>``; drop other-lang
   forms, with a no-loss move into ``<gloss>`` and an empty-headword add.
2. glosses — drop empty, keep populated.
3. definition — drop empty ``<form>`` children; keep populated + keep
   the ``<definition>`` parent even when formless.
4. citation — mirror rule 1: keep only ``<form lang=vernlang>``, drop
   every other-language form (empty or populated); keep the
   ``<citation>`` parent even when formless.

Contract: bytes→bytes, full-tag vernlang match, order-preserving,
parse-failure / no-change → return input unchanged.
"""

import xml.etree.ElementTree as ET

from azt_collabd.projects import _clean_template


def _first_entry(xml_bytes):
    return ET.fromstring(xml_bytes).find('entry')


def _form_langs(holder):
    """[(lang, text)] for the <form> children of an element."""
    out = []
    for f in holder.findall('form'):
        t = f.find('text')
        out.append((f.get('lang'), None if t is None else t.text))
    return out


# --- Rule 1: lexical-unit ------------------------------------------------

def test_lexunit_keeps_only_vernlang_form():
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1"><lexical-unit>'
           b'<form lang="nml"><text>headword</text></form>'
           b'<form lang="en"><text>ignored</text></form>'
           b'</lexical-unit><sense><gloss lang="en"><text>x</text></gloss>'
           b'</sense></entry></lift>')
    out = _clean_template(src, 'nml')
    lu = _first_entry(out).find('lexical-unit')
    assert _form_langs(lu) == [('nml', 'headword')]


def test_lexunit_noloss_moves_source_word_to_gloss():
    """A populated other-lang lexical-unit form whose language has no
    non-empty gloss is moved into a gloss before being dropped."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1"><lexical-unit>'
           b'<form lang="en"><text>dog</text></form>'
           b'</lexical-unit></entry></lift>')
    out = _clean_template(src, 'nml')
    entry = _first_entry(out)
    # No vernlang form existed → an empty headword slot was added.
    assert _form_langs(entry.find('lexical-unit')) == [('nml', None)]
    # The source word survived as an en gloss.
    glosses = [(g.get('lang'), g.find('text').text)
               for g in entry.find('sense').findall('gloss')]
    assert ('en', 'dog') in glosses


def test_lexunit_adds_empty_vernlang_headword_when_absent():
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1"><lexical-unit>'
           b'<form lang="fr"><text/></form>'
           b'</lexical-unit></entry></lift>')
    out = _clean_template(src, 'nml')
    lu = _first_entry(out).find('lexical-unit')
    assert _form_langs(lu) == [('nml', None)]


# --- Rule 2: glosses -----------------------------------------------------

def test_empty_glosses_dropped_populated_kept():
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1">'
           b'<lexical-unit><form lang="nml"><text>w</text></form></lexical-unit>'
           b'<sense>'
           b'<gloss lang="en"><text>keep</text></gloss>'
           b'<gloss lang="fr"><text/></gloss>'
           b'<gloss lang="pt"></gloss>'
           b'</sense></entry></lift>')
    out = _clean_template(src, 'nml')
    glosses = [(g.get('lang'), g.find('text').text if g.find('text') is not None else None)
               for g in _first_entry(out).find('sense').findall('gloss')]
    assert glosses == [('en', 'keep')]


# --- Rule 3: definition --------------------------------------------------

def test_definition_drops_empty_forms_keeps_populated():
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1">'
           b'<lexical-unit><form lang="nml"><text>w</text></form></lexical-unit>'
           b'<sense><definition>'
           b'<form lang="en"><text>a meaning</text></form>'
           b'<form lang="fr"><text/></form>'
           b'<form lang="pt"></form>'
           b'</definition></sense></entry></lift>')
    out = _clean_template(src, 'nml')
    defn = _first_entry(out).find('sense/definition')
    # Populated form (any language) survives; empty ones are gone.
    assert _form_langs(defn) == [('en', 'a meaning')]


def test_definition_parent_kept_even_when_all_forms_empty():
    """The <definition> element stays for user familiarity even after
    every form inside it is pruned."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1">'
           b'<lexical-unit><form lang="nml"><text>w</text></form></lexical-unit>'
           b'<sense><definition>'
           b'<form lang="en"><text/></form>'
           b'<form lang="fr"></form>'
           b'</definition></sense></entry></lift>')
    out = _clean_template(src, 'nml')
    defn = _first_entry(out).find('sense/definition')
    assert defn is not None
    assert defn.findall('form') == []


# --- Rule 4: citation ----------------------------------------------------

def test_citation_keeps_only_vernlang_drops_others():
    """Mirror of rule 1: only the vernlang citation form survives; a
    populated *other-language* form is dropped (not moved anywhere),
    and empty forms are dropped too."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1">'
           b'<lexical-unit><form lang="nml"><text>w</text></form></lexical-unit>'
           b'<citation>'
           b'<form lang="nml"><text>citform</text></form>'
           b'<form lang="en"><text>english</text></form>'
           b'<form lang="fr"><text/></form>'
           b'</citation></entry></lift>')
    out = _clean_template(src, 'nml')
    cit = _first_entry(out).find('citation')
    assert _form_langs(cit) == [('nml', 'citform')]


def test_citation_parent_kept_even_when_no_vernlang_form():
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1">'
           b'<lexical-unit><form lang="nml"><text>w</text></form></lexical-unit>'
           b'<citation>'
           b'<form lang="en"><text>english</text></form>'
           b'</citation></entry></lift>')
    out = _clean_template(src, 'nml')
    cit = _first_entry(out).find('citation')
    assert cit is not None
    assert cit.findall('form') == []


# --- Contract: full-tag match, passthrough, order ------------------------

def test_vernlang_matched_as_full_tag_not_bare_subtag():
    """``ba-x-dialect`` must not match a bare ``ba`` form and vice
    versa — comparison is on the full assembled BCP-47 tag."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1"><lexical-unit>'
           b'<form lang="ba"><text>bare</text></form>'
           b'<form lang="ba-x-dialect"><text>tagged</text></form>'
           b'</lexical-unit></entry></lift>')
    out = _clean_template(src, 'ba-x-dialect')
    lu = _first_entry(out).find('lexical-unit')
    assert _form_langs(lu) == [('ba-x-dialect', 'tagged')]


def test_parse_failure_passes_through_unchanged():
    src = b'<lift><entry guid="e1"><lexical-unit>'  # truncated
    assert _clean_template(src, 'nml') == src


def test_no_change_returns_input_bytes_unchanged():
    """Nothing to prune → the exact input bytes come back (identity),
    not a re-serialised near-copy."""
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13"><entry guid="e1">'
           b'<lexical-unit><form lang="nml"><text>w</text></form></lexical-unit>'
           b'<sense><gloss lang="en"><text>g</text></gloss></sense>'
           b'</entry></lift>')
    assert _clean_template(src, 'nml') is src or _clean_template(src, 'nml') == src


def test_entry_order_preserved():
    src = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<lift version="0.13">'
           b'<entry guid="a"><lexical-unit>'
           b'<form lang="nml"><text>one</text></form>'
           b'<form lang="en"><text>x</text></form></lexical-unit></entry>'
           b'<entry guid="b"><lexical-unit>'
           b'<form lang="nml"><text>two</text></form></lexical-unit></entry>'
           b'<entry guid="c"><lexical-unit>'
           b'<form lang="nml"><text>three</text></form></lexical-unit></entry>'
           b'</lift>')
    out = _clean_template(src, 'nml')
    guids = [e.get('guid') for e in ET.fromstring(out).iter('entry')]
    assert guids == ['a', 'b', 'c']
