"""
LIFT-aware three-way merge.

Merges by entry ``guid``. For each guid present in any of the three
inputs (base, ours, theirs):

* If only one side has it (and it didn't exist in base), keep it.
* If one side modified the entry and the other didn't touch it, take
  the modified side.
* If both sides modified the entry to the same canonical XML, take it.
* If both sides modified the entry differently, recursively merge
  the entry's sub-elements, expressing the conflict at the
  **narrowest** multi-allowed LIFT level that contains it. A
  same-lang text conflict, for example, produces two same-lang
  ``<form>`` siblings each carrying its own ``<text>`` plus an
  ``<annotation name="azt-lift-conflict" value="ours|theirs"/>``
  marker — instead of two whole ``<entry>`` copies. See
  ``_merge_pair`` and the ``_MULTI`` policy table for the
  resolution shape.
* Modify-vs-delete is treated as a conflict at the entry level:
  keep the modified side with the conflict annotation.

Pre-0.35.2 the merge duplicated whole entries with a synthetic
``-theirs`` guid suffix on conflicts. That worked but produced
~unresolvable conflict states — a user could never realistically
scan both 1700-line entries to find the actual divergence. v3
narrows the duplication to the smallest LIFT element that can
carry a multi-of-itself sibling without breaking the schema.
"""

import ast
import collections
import hashlib
import os
import secrets
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone


CONFLICT_ANNOTATION_NAME = 'azt-lift-conflict'
# Trait name on the conflict annotation that carries the list of
# slash-delimited element paths where conflicts live in the merged
# entry. Lets a peer-side merge UI focus the user on the parts of
# the entry that actually need attention instead of forcing them
# to diff the whole entry by eye. Example value:
# ``lexical-unit/form[lang=en],sense[id=A]/gloss[lang=en]``.
CONFLICT_FIELDS_TRAIT = f'{CONFLICT_ANNOTATION_NAME}-fields'

# Truncation-detection thresholds. Triggered when one side arrives
# at the merge with dramatically fewer entries than the other (and
# base) — strongly suggesting upstream truncation. See
# ``_looks_truncated`` for the exact rules.
TRUNCATION_MIN_LARGER_SIDE = 50
TRUNCATION_RATIO = 50
# Output-side guard thresholds. Triggered when the merged result
# has dramatically fewer entries than both inputs — implying an
# algorithmic loss INSIDE the merger, not an input truncation.
# See ``_looks_catastrophic_output`` for rationale.
CATASTROPHIC_OUTPUT_RATIO = 4

# LIFT multiplicity policy: (parent_tag, child_tag) → True if the
# child can appear multiple times inside the parent per the LIFT
# 0.13/0.15 schema, False if singleton. Used by the recursive
# merge to decide where to express a conflict: at the narrowest
# multi-allowed enclosing level. Unknown pairs default to True
# (multi) — safer to over-allow than under-allow, since
# under-allowing forces conflict expression at a coarser level
# (closer to whole-entry duplication, which is what we're trying
# to avoid). Add entries here as new LIFT element relationships
# become relevant.
_MULTI = {
    # Inside <lift>
    ('lift', 'entry'):              True,
    # Inside <entry>
    ('entry', 'lexical-unit'):      False,
    ('entry', 'citation'):          False,
    ('entry', 'pronunciation'):     True,
    ('entry', 'variant'):           True,
    ('entry', 'sense'):             True,
    ('entry', 'note'):              True,
    ('entry', 'relation'):          True,
    ('entry', 'etymology'):         True,
    ('entry', 'field'):             True,
    ('entry', 'trait'):             True,
    ('entry', 'annotation'):        True,
    # Inside multitext containers (form is 1..* per the multitext type)
    ('lexical-unit', 'form'):       True,
    ('lexical-unit', 'annotation'): True,
    ('lexical-unit', 'trait'):      True,
    ('citation', 'form'):           True,
    ('citation', 'annotation'):     True,
    ('citation', 'trait'):          True,
    ('definition', 'form'):         True,
    ('definition', 'annotation'):   True,
    ('example', 'form'):            True,
    ('example', 'translation'):     True,
    ('example', 'note'):            True,
    ('example', 'annotation'):      True,
    ('translation', 'form'):        True,
    ('media', 'form'):              True,
    ('illustration', 'form'):       True,
    ('label', 'form'):              True,
    ('value', 'form'):              True,
    # Inside <form>
    ('form', 'text'):               False,
    ('form', 'annotation'):         True,
    # Inside <sense>
    ('sense', 'grammatical-info'):  False,
    ('sense', 'gloss'):             True,
    ('sense', 'definition'):        True,
    ('sense', 'example'):           True,
    ('sense', 'note'):              True,
    ('sense', 'relation'):          True,
    ('sense', 'subsense'):          True,
    ('sense', 'illustration'):      True,
    ('sense', 'reversal'):          True,
    ('sense', 'trait'):             True,
    ('sense', 'field'):             True,
    ('sense', 'annotation'):        True,
    # Inside <gloss> — has lang on itself, NOT a multitext, so no <form>.
    ('gloss', 'text'):              False,
    ('gloss', 'annotation'):        True,
    # Inside <pronunciation>
    ('pronunciation', 'form'):      True,
    ('pronunciation', 'media'):     True,
    ('pronunciation', 'annotation'): True,
    # Inside <field>
    ('field', 'form'):              True,
    ('field', 'trait'):             True,
    ('field', 'annotation'):        True,
    # Inside <note>
    ('note', 'form'):               True,
    ('note', 'annotation'):         True,
    # Inside <relation>
    ('relation', 'usage'):          False,
    ('relation', 'field'):          True,
    ('relation', 'trait'):          True,
    ('relation', 'annotation'):     True,
}


def _is_multi(parent_tag, child_tag):
    """Whether <child_tag> can appear multiple times inside
    <parent_tag>, per the LIFT schema. Unknown pairs default to
    True — see ``_MULTI`` rationale."""
    return _MULTI.get((parent_tag, child_tag), True)


@dataclass
class Conflict:
    path: str = ''
    guid: str = ''
    # Kind values:
    #   'modify-modify'              both sides changed the same entry differently
    #   'add-add'                    both sides added the same guid with different content
    #   'modify-delete'              we changed; theirs deleted
    #   'delete-modify'              we deleted; theirs changed
    #   'truncation-suspected'       one side arrived too small to trust (INPUT guard)
    #   'parse-error'                one side's XML failed to parse (INPUT guard)
    #   'catastrophic-merge-output'  merger lost data internally despite healthy inputs (OUTPUT guard)
    kind: str = ''
    fields: list = field(default_factory=list)

    def to_dict(self):
        return {'path': self.path, 'guid': self.guid,
                'kind': self.kind, 'fields': list(self.fields)}


@dataclass
class MergeResult:
    merged_bytes: bytes = b''
    conflicts: list = field(default_factory=list)
    # Count of post-merge invariant repairs applied by
    # ``_normalize_entry`` (duplicate same-lang forms dropped or
    # unioned, missing conflict annotations added). Informational —
    # repairs are self-healing, not conflicts.
    repairs: int = 0


def _parse(xml_bytes, label=''):
    """Parse LIFT bytes. Returns ``(root, error_message)``. The
    error message is empty on success; on failure it's a short
    description suitable for embedding in a Conflict's ``fields``
    diagnostic, and the returned root is an empty LIFT doc so
    callers don't have to special-case None.

    Pre-0.35.2 ``_parse`` silently masked ``ET.ParseError`` by
    returning the empty doc — which combined with the merge body
    treating "absent from ours" as "ours deleted" to produce
    catastrophically destructive merges when the input was
    truncated mid-write. The error return surfaces the parse
    failure so ``three_way_merge`` can refuse the merge entirely
    instead of acting on a phantom empty side."""
    if not xml_bytes:
        return ET.fromstring(b'<lift version="0.13"></lift>'), ''
    try:
        return ET.fromstring(xml_bytes), ''
    except ET.ParseError as ex:
        prefix = f'{label}: ' if label else ''
        return (ET.fromstring(b'<lift version="0.13"></lift>'),
                f'{prefix}XML parse error: {ex}')


def _entries(root):
    out = {}
    for e in list(root.findall('entry')):
        guid = e.attrib.get('guid', '')
        if guid:
            out[guid] = e
    return out


def _canon(elem):
    return ET.tostring(elem, encoding='utf-8')


def _strip_conflict_annotations(elem):
    """Recursively remove every ``<annotation name="azt-lift-conflict"
    ...>`` child from *elem* in place. Used by both the canonical-
    comparison path (so stale annotations from previous merges don't
    cause false-positive conflict detection) and by the canon-equal
    output path (so semantically-identical elements emerge clean,
    without inherited cruft).

    Field log baf 2026-05-22 showed entries accumulating up to 1700+
    ``azt-lift-conflict`` markers across recovery cycles, even on
    forms whose `<text>` content was byte-identical on both sides.
    Each merge saw the prior round's annotations as "ours has them,
    theirs doesn't" → fresh conflict → more annotations. This strip
    breaks that cycle: the merge sees the underlying content and
    recognizes the equality."""
    for child in list(elem):
        if (child.tag == 'annotation'
                and child.attrib.get('name') == CONFLICT_ANNOTATION_NAME):
            elem.remove(child)
        else:
            _strip_conflict_annotations(child)


def _strip_indent_whitespace(elem):
    """Normalize inter-element whitespace by clearing text/tail
    when they contain only whitespace and the element has
    children. Two elements that differ only in indentation (e.g.,
    one parsed from a pretty-printed file, one from a compact
    source) then compare equal under ``_canon_clean`` below."""
    if list(elem):
        if elem.text is not None and not elem.text.strip():
            elem.text = None
    if elem.tail is not None and not elem.tail.strip():
        elem.tail = None
    for child in elem:
        _strip_indent_whitespace(child)


def _canon_clean(elem):
    """Canonical bytes for *elem* with all ``azt-lift-conflict``
    annotation children removed and inter-element whitespace
    normalized. The version of ``_canon`` to use for conflict
    *detection* — answers "do these two elements represent the
    same semantic content?" rather than "are these two elements
    byte-identical XML?".

    Used by ``_merge_pair`` instead of bare ``_canon`` so that
    stale conflict markers (left over from previous merges that
    mistakenly flagged identical content) don't perpetuate fresh
    conflicts every time the merge runs. See
    ``_strip_conflict_annotations`` docstring for the field
    incident this fixes."""
    copy = ET.fromstring(_canon(elem))
    _strip_conflict_annotations(copy)
    _strip_indent_whitespace(copy)
    return ET.tostring(copy, encoding='utf-8')


def _strip_entries(root):
    """Return a copy of *root* with all <entry> children removed."""
    copy = ET.fromstring(_canon(root))
    for e in list(copy.findall('entry')):
        copy.remove(e)
    return copy


def _child_key(child):
    """Stable string key for an element, used by the recursive
    merge to pair corresponding children across the three sides.
    Senses use ``id``, fields/relations/notes/traits use ``type``
    or ``name``, variants use ``ref``, forms/glosses use ``lang``.
    Bare tag name when no keying attribute applies (and the parent
    schema-singleton'd this element anyway)."""
    a = child.attrib
    if 'id' in a:
        return f'{child.tag}[id={a["id"]}]'
    if 'type' in a:
        return f'{child.tag}[type={a["type"]}]'
    if 'ref' in a:
        return f'{child.tag}[ref={a["ref"]}]'
    if 'lang' in a:
        return f'{child.tag}[lang={a["lang"]}]'
    if 'name' in a:
        return f'{child.tag}[name={a["name"]}]'
    return child.tag


def _children_by_key(elem):
    """Return ``(by_key, keys_in_order)``. ``by_key`` maps each key
    to the list of children with that key (in document order).
    ``keys_in_order`` is the deduplicated key sequence in document
    order — used to preserve ours's layout in the merged result."""
    by_key = {}
    keys_in_order = []
    seen = set()
    for child in elem:
        k = _child_key(child)
        by_key.setdefault(k, []).append(child)
        if k not in seen:
            keys_in_order.append(k)
            seen.add(k)
    return by_key, keys_in_order


def _same_shape(o, t):
    """Whether two elements are corresponding instances of the
    same logical thing — same tag and same keying attribs. Used
    by ``_merge_pair`` to decide whether to recurse into children
    or fall back to ``two annotated copies'' at the parent's level."""
    if o.tag != t.tag:
        return False
    return _child_key(o) == _child_key(t)


def _clone(elem):
    """Deep clone of an element via canonical round-trip."""
    return ET.fromstring(_canon(elem))


def _annotated_clone(elem, side):
    """Clone *elem* and append a
    ``<annotation name="azt-lift-conflict" value="ours|theirs"/>``
    child. The annotation marks which side this copy came from so a
    peer-side resolver UI can identify it without re-walking the
    git history. Adding the annotation as a *child* keeps it
    scoped to this element specifically — if recursion landed a
    conflict at, say, the ``<form>`` level inside an otherwise
    cleanly-merged entry, only the conflicting forms carry the
    annotation; the entry itself doesn't pretend to be wholly
    conflicted."""
    copy = _clone(elem)
    a = ET.SubElement(copy, 'annotation')
    a.set('name', CONFLICT_ANNOTATION_NAME)
    a.set('value', side)
    return copy


class _Escalate:
    """Sentinel return from ``_merge_pair`` meaning: I have a
    conflict here, I tried to recurse to express it deeper, and
    nothing inside me could resolve it AND my parent's schema
    forbids two of me. The caller (my parent's ``_walk_children``)
    must propagate this up to the nearest ancestor whose parent
    DOES allow multi of that ancestor, and duplicate at THAT
    level.

    For LIFT this terminates inside ``<entry>`` because every
    chain of singletons has a multi-allowed ancestor below entry
    (``<form>`` inside ``<lexical-unit>``, ``<gloss>`` inside
    ``<sense>``, etc.). If the recursion makes it all the way to
    the entry-level call without finding a multi-allowed ancestor,
    ``three_way_merge``'s caller-level uses the entries-are-multi-
    in-lift fallback (the historical synthetic-guid behaviour) —
    rare and unavoidable for entry-attribute-only conflicts."""

    __slots__ = ('o', 't')

    def __init__(self, o, t):
        self.o = o
        self.t = t


def _merge_pair(b, o, t, parent_allows_multi):
    """Recursively merge corresponding elements from base / ours /
    theirs. ``parent_allows_multi`` tells us whether the element
    enclosing this pair will accept TWO of us as siblings (i.e.,
    whether the merge can express our conflict at our own level).

    Returns either:

      * A list of 0, 1, or 2 elements ready for the caller to
        append. Length 2 only when ``parent_allows_multi`` is True
        AND we couldn't narrow the conflict further.
      * An ``_Escalate`` sentinel when we have an irreducible
        conflict and our parent's schema forbids two of us — the
        caller must propagate up.
    """
    if o is None and t is None:
        return []
    if o is None or t is None:
        # One side is missing this child. Consult the base before
        # keeping: if the present side is UNCHANGED since base, the
        # other side deleted it — honor the delete. Pre-0.53.x this
        # returned the present side unconditionally, which
        # resurrected deleted children on every merge against a
        # stale branch; combined with the old positional pairing it
        # was one of the two engines behind the 'wife' ×29
        # duplicate-form accumulation (2026-07-10): once a repair
        # consolidated the duplicates, any merge against a
        # pre-repair branch brought them all back.
        present = o if o is not None else t
        if b is not None and _canon_clean(b) == _canon_clean(present):
            return []
        # Changed-then-deleted (or no base): keep the present side —
        # same keep-the-data bias as the entry-level modify/delete
        # rule.
        return [_clone(present)]

    # Use ``_canon_clean`` for *detection* — strips stale
    # ``azt-lift-conflict`` annotations and inter-element
    # whitespace before comparing. Pre-0.45.34 the comparison was
    # raw ``_canon``, which treated semantically-identical
    # elements as conflicting whenever one side carried left-over
    # annotations from a previous merge. That fed a vicious cycle:
    # every recovery added more spurious annotations, which made
    # the next recovery generate even more, ballooning to 1700+
    # markers on truly-identical content in the field.
    oc_clean = _canon_clean(o)
    tc_clean = _canon_clean(t)
    if oc_clean == tc_clean:
        # Canon-equal ⇒ any existing ``azt-lift-conflict``
        # annotations on either side are by definition
        # false-positives (the underlying content matches), so
        # we emit a *stripped* clone. The merge thereby
        # self-heals previously-polluted LIFTs as it walks them:
        # one pass over a 1700-marker entry collapses all the
        # spurious annotations back to nothing, leaving only
        # markers on genuinely-divergent content.
        cleaned = _clone(o)
        _strip_conflict_annotations(cleaned)
        return [cleaned]

    # Both present and differ semantically. Check the base —
    # only-one-side-changed cleanly takes the changed side
    # without any conflict marker. Base comparison also uses
    # ``_canon_clean`` so a peer whose LIFT carries stale
    # annotations relative to its own committed base doesn't
    # synthesize a phantom "I changed something" signal.
    if b is not None:
        bc_clean = _canon_clean(b)
        if oc_clean == bc_clean:
            return [_clone(t)]   # only theirs changed
        if tc_clean == bc_clean:
            return [_clone(o)]   # only ours changed

    # Both changed differently (or no shared base). Try to recurse
    # to narrow the conflict to a smaller sub-element.
    has_recursable_children = len(list(o)) > 0 or len(list(t)) > 0
    if _same_shape(o, t) and has_recursable_children:
        shell = ET.Element(o.tag, dict(o.attrib))
        shell.text = o.text
        shell.tail = o.tail
        escalated = _walk_children(shell, b, o, t)
        if not escalated:
            return [shell]
        # A descendant escalated past the deepest multi-allowed
        # level inside us. Fall through to expressing the conflict
        # at OUR level (if our parent allows) or escalating further.

    if parent_allows_multi:
        return [_annotated_clone(o, 'ours'),
                _annotated_clone(t, 'theirs')]
    return _Escalate(o, t)


def _pair_same_key(b_elems, o_elems, t_elems):
    """Pair up same-key children across base / ours / theirs into
    ``(b, o, t)`` triples for ``_merge_pair``.

    Pre-0.53.x this pairing was purely positional (index i of ours
    against index i of theirs), which mispaired whenever the two
    sides held same-key lists of different lengths or orders:
    ``ours=[A,B] vs theirs=[B]`` paired A-with-B (a phantom
    conflict producing an annotated duplicate pair) and kept the
    overhang B unconditionally — net result ``[A,B,B]``, one extra
    copy per merge. That linear growth is the primary engine
    behind the 'wife' entry accumulating 28 copies of the same
    verification form (field repro 2026-07-10).

    The content-first strategy:

    1. Elements semantically identical on both sides
       (``_canon_clean``) pair with each other (and with a
       content-matching base element when present) — these merge
       clean, no conflict, no duplication.
    2. A leftover one-sided element whose content matches an
       unused base element pairs with base alone — ``_merge_pair``
       then honors the other side's delete instead of
       resurrecting.
    3. Remaining leftovers pair positionally (genuinely changed
       on both sides, or true adds) — the historical behavior,
       now reached only when content actually diverged.
    """
    o_clean = [_canon_clean(e) for e in o_elems]
    t_clean = [_canon_clean(e) for e in t_elems]
    b_clean = [_canon_clean(e) for e in b_elems]
    o_used, t_used, b_used = set(), set(), set()

    def _take(clean_list, used, content):
        for j, c in enumerate(clean_list):
            if j not in used and c == content:
                used.add(j)
                return j
        return None

    triples = []
    # Phase 1: identical on both sides.
    for i, oc in enumerate(o_clean):
        tj = _take(t_clean, t_used, oc)
        if tj is None:
            continue
        o_used.add(i)
        bj = _take(b_clean, b_used, oc)
        triples.append((b_elems[bj] if bj is not None else None,
                        o_elems[i], t_elems[tj]))
    # Phase 2: one-sided leftovers that match base — deletes.
    for i in range(len(o_elems)):
        if i in o_used:
            continue
        bj = _take(b_clean, b_used, o_clean[i])
        if bj is not None:
            o_used.add(i)
            triples.append((b_elems[bj], o_elems[i], None))
    for j in range(len(t_elems)):
        if j in t_used:
            continue
        bj = _take(b_clean, b_used, t_clean[j])
        if bj is not None:
            t_used.add(j)
            triples.append((b_elems[bj], None, t_elems[j]))
    # Phase 3: positional pairing for the true divergences.
    o_left = [e for i, e in enumerate(o_elems) if i not in o_used]
    t_left = [e for j, e in enumerate(t_elems) if j not in t_used]
    b_left = [e for k, e in enumerate(b_elems) if k not in b_used]
    for i in range(max(len(o_left), len(t_left), len(b_left))):
        o_e = o_left[i] if i < len(o_left) else None
        t_e = t_left[i] if i < len(t_left) else None
        b_e = b_left[i] if i < len(b_left) else None
        if o_e is None and t_e is None:
            # Base-only remainder: deleted from both sides (or
            # never carried forward) — nothing to emit.
            continue
        triples.append((b_e, o_e, t_e))
    return triples


def _walk_children(merged_parent, b, o, t):
    """Walk corresponding children of ``o`` and ``t`` (matching by
    key, preserving ours's document order with theirs-only keys
    appended), recursively merging each into ``merged_parent``.

    Returns True if a descendant escalated past every internal
    multi-allowed level — the caller (``_merge_pair`` for
    ``merged_parent``) should DISCARD merged_parent and try to
    express the conflict at ITS level. Returns False on clean
    merge (all conflicts expressed at the narrowest level)."""
    parent_tag = merged_parent.tag

    o_by_key, o_keys_order = _children_by_key(o)
    t_by_key, t_keys_order = _children_by_key(t)
    b_by_key = _children_by_key(b)[0] if b is not None else {}

    # Document-order union: ours first, theirs-only appended.
    seen = set()
    keys_in_order = []
    for k in o_keys_order:
        if k not in seen:
            keys_in_order.append(k)
            seen.add(k)
    for k in t_keys_order:
        if k not in seen:
            keys_in_order.append(k)
            seen.add(k)

    for key in keys_in_order:
        o_elems = o_by_key.get(key, [])
        t_elems = t_by_key.get(key, [])
        b_elems = b_by_key.get(key, [])
        # Pair by semantic content first, positionally for the
        # remainder — see ``_pair_same_key`` for why pure
        # positional pairing multiplied duplicate forms.
        for b_e, o_e, t_e in _pair_same_key(b_elems, o_elems, t_elems):
            # The child's tag (for the multi check) comes from
            # whichever side has the element. Use ``is not None``
            # explicitly: a leaf Element (no sub-children) is falsy
            # under ElementTree's legacy truthiness contract, so
            # ``o_e or t_e or b_e`` silently falls through to None
            # whenever the present side is a leaf. That bug surfaced
            # in the field as ``AttributeError: 'NoneType' object has
            # no attribute 'tag'`` on a one-sided <media> add.
            sample = (o_e if o_e is not None
                      else t_e if t_e is not None
                      else b_e)
            child_tag = sample.tag
            parent_accepts_multi_of_child = _is_multi(parent_tag, child_tag)

            result = _merge_pair(b_e, o_e, t_e, parent_accepts_multi_of_child)

            if isinstance(result, _Escalate):
                # Child wants two of itself at our level. We can do
                # that iff our schema allows multi of child_tag.
                if parent_accepts_multi_of_child:
                    merged_parent.append(_annotated_clone(result.o, 'ours'))
                    merged_parent.append(_annotated_clone(result.t, 'theirs'))
                else:
                    # We can't either. Caller discards merged_parent.
                    return True
            else:
                for r in result:
                    merged_parent.append(r)

    return False


def _collect_conflict_paths(merged_entry):
    """Walk ``merged_entry`` and return a list of slash-delimited
    paths from the entry root to each ``<annotation
    name="azt-lift-conflict">`` marker, with the leaf key included.
    The trait on the entry-level annotation embeds this list so a
    peer-side resolver can jump straight to the conflicting
    sub-elements without re-walking the merged tree.

    Example output for a same-lang text conflict in a lexical-unit:
    ``['lexical-unit/form[lang=en]']`` — pointing at the form
    pair (the level where the conflict was expressed)."""
    paths = set()

    def _walk(elem, prefix):
        for child in elem:
            child_path = (f'{prefix}/{_child_key(child)}'
                          if prefix else _child_key(child))
            if (child.tag == 'annotation'
                    and child.attrib.get('name') == CONFLICT_ANNOTATION_NAME):
                # The conflict is on the PARENT of this annotation,
                # not on the annotation itself.
                paths.add(prefix or _child_key(elem))
                continue
            _walk(child, child_path)

    _walk(merged_entry, '')
    return sorted(paths)


# ── Post-merge invariant: no duplicate same-lang forms ──────────────────
#
# A LIFT multitext carries at most ONE <form> per lang inside one
# parent (and one <gloss> per lang inside one sense) — consumers
# read/write "the" form for a lang, so silent duplicates shadow
# real data. The only sanctioned same-lang multiplicity is the
# merge's own conflict representation: sibling copies each
# carrying an ``azt-lift-conflict`` annotation. ``_normalize_entry``
# enforces this after every merge:
#
#   * semantically-identical duplicates collapse to the
#     document-first node;
#   * duplicates inside a *verification* field union their code
#     lists (mirroring azt's ``Field.consolidate_forms_by_lang``
#     exactly, so the two layers converge on the same bytes:
#     first-seen order, a check whose value conflicts across
#     copies is dropped entirely — it must re-verify);
#   * any other surviving same-lang multiplicity is forced into
#     the annotated-conflict-pair shape so it is visible instead
#     of silent.
#
# Field repro 2026-07-10 ('wife', guid 9ae43c82): one field held
# 29 same-lang forms (1 + 28 identical copies) accumulated across
# merges on a single computer.

# A field is verification-shaped when its ``type`` contains this
# token — matches azt's ``verificationkey`` naming ('<profile>
# <ftype> verification', '<ftype> primitive verification',
# 'alphabet verification', ...).
_VERIFICATION_TOKEN = 'verification'


def _is_verification_field(parent):
    return (parent.tag == 'field'
            and _VERIFICATION_TOKEN in (parent.attrib.get('type') or ''))


def _form_text(form):
    t = form.find('text')
    return (t.text or '') if t is not None else ''


def _union_verification_texts(texts):
    """Union verification-code lists across duplicate form texts.
    Mirrors azt ``Field.consolidate_forms_by_lang``: each text is a
    python-list repr like ``"['V1=ai', 'C1=wh']"``; codes key on
    the check name (everything before the LAST ``=``); first-seen
    order wins; a check whose value CONFLICTS across copies is
    dropped entirely (it must re-verify). Texts that don't parse
    as a list contribute nothing (same as azt).

    Returns ``(union_repr, dropped_checks, parsed_any)`` —
    ``parsed_any`` False means no text was list-shaped and the
    caller should fall back to conflict-pair handling instead of
    destroying content."""
    merged = {}
    order = []
    conflicted = set()
    parsed_any = False
    for raw in texts:
        codes = None
        if raw:
            try:
                codes = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                codes = None
        if not isinstance(codes, list):
            continue
        parsed_any = True
        for code in codes:
            parts = str(code).split('=')
            c, v = '='.join(parts[:-1]), parts[-1]
            if c in merged and merged[c] != v:
                conflicted.add(c)
            elif c not in merged:
                merged[c] = v
                order.append(c)
    keep = ['{}={}'.format(c, merged[c]) for c in order
            if c not in conflicted]
    return str(keep), sorted(conflicted), parsed_any


def _conflict_side(elem):
    """The ``azt-lift-conflict`` side value carried by *elem*'s
    direct annotation children, or ``''`` if none."""
    for a in elem.findall('annotation'):
        if a.attrib.get('name') == CONFLICT_ANNOTATION_NAME:
            return a.attrib.get('value', '')
    return ''


def _normalize_entry(entry, path=''):
    """Enforce the no-duplicate-same-lang-forms invariant on
    *entry* in place (see the section comment above). Idempotent;
    cheap when there are no duplicates. Returns the number of
    repairs applied (0 on the overwhelmingly common clean path)."""
    guid = entry.attrib.get('guid', '')
    repaired = 0
    for parent in entry.iter():
        groups = {}
        for child in parent:
            if child.tag == 'form' or (child.tag == 'gloss'
                                       and parent.tag == 'sense'):
                groups.setdefault(
                    (child.tag, child.attrib.get('lang', '')),
                    []).append(child)
        for (tag, lang), group in groups.items():
            if len(group) < 2:
                continue
            # 1) Collapse semantically-identical duplicates onto
            #    the document-first node.
            survivors = []
            seen = set()
            dropped_identical = 0
            for f in group:
                c = _canon_clean(f)
                if c in seen:
                    parent.remove(f)
                    dropped_identical += 1
                else:
                    seen.add(c)
                    survivors.append(f)
            if dropped_identical:
                repaired += 1
                trace(f'[merge-repair] path={path!r} guid={guid} '
                      f'{tag}[lang={lang}]: dropped '
                      f'{dropped_identical} identical duplicate '
                      f'form(s)')
            if len(survivors) == 1:
                if dropped_identical:
                    # Duplicates proved agreement — any conflict
                    # markers left on the survivor are false
                    # positives.
                    _strip_conflict_annotations(survivors[0])
                continue
            # 2) Verification fields: union the code content into
            #    one form instead of keeping conflict copies.
            if tag == 'form' and _is_verification_field(parent):
                texts = [_form_text(f) for f in survivors]
                union_repr, dropped_checks, parsed_any = \
                    _union_verification_texts(texts)
                if parsed_any:
                    keeper = survivors[0]
                    for f in survivors[1:]:
                        parent.remove(f)
                    _strip_conflict_annotations(keeper)
                    tnode = keeper.find('text')
                    if tnode is None:
                        tnode = ET.SubElement(keeper, 'text')
                    tnode.text = union_repr
                    repaired += 1
                    trace(f'[merge-repair] path={path!r} guid={guid} '
                          f'field[type='
                          f'{parent.attrib.get("type", "")!r}] '
                          f'lang={lang}: unioned {len(survivors)} '
                          f'forms -> {union_repr}'
                          + (f' (dropped conflicting checks '
                             f'{dropped_checks})'
                             if dropped_checks else ''))
                    continue
            # 3) Anything else survives only as an annotated
            #    conflict pair — make silent duplicates visible.
            existing = {_conflict_side(f) for f in survivors} - {''}
            added = 0
            for f in survivors:
                if _conflict_side(f):
                    continue
                side = 'ours' if 'ours' not in existing else 'theirs'
                existing.add(side)
                a = ET.SubElement(f, 'annotation')
                a.set('name', CONFLICT_ANNOTATION_NAME)
                a.set('value', side)
                added += 1
            if added:
                repaired += 1
                trace(f'[merge-repair] path={path!r} guid={guid} '
                      f'{tag}[lang={lang}]: {len(survivors)} '
                      f'divergent same-lang copies; annotated '
                      f'{added} as conflict')
    return repaired


def _looks_truncated(base_entries, ours_entries, theirs_entries):
    """Return a non-empty diagnostic string when the input sides
    look implausibly asymmetric — strongly suggesting one side
    arrived at the merge truncated by an upstream bug (peer-write
    race, partial commit, sandbox sync hiccup, etc.). Returns ``''``
    when the inputs look healthy.

    Two cases trip the guard:

    1. **Empty side with non-empty base**: ``ours`` (or ``theirs``)
       has 0 entries while base has any entries and the other side
       does too. Triggers regardless of project size, because an
       entire side going to zero is a far stronger signal than a
       proportional asymmetry (and a 5-entry project gets caught
       here even though the ratio test below ignores it). False
       positive only when the user *intentionally* clears every
       entry in a project, which doesn't happen in this suite's
       peer flows.
    2. **Large ratio asymmetry**: larger side has ≥50 entries AND
       smaller side has <1/50 of the larger. Catches the
       partially-truncated case (e.g., ~50% loss) for big projects
       where the absolute count makes a 98% deletion implausible.

    Field-reported scenarios the guard catches:
    - 2026-05-12 ``baf``: ours arrived with 1 entry, base+theirs
      had 1701. Tripped the ratio case.
    - Hypothetical 5-entry project where ours's LIFT got
      ``ftruncate(0)``'d mid-write: tripped the empty-side case."""
    bn = len(base_entries)
    on = len(ours_entries)
    tn = len(theirs_entries)
    if bn == 0:
        # No shared history. Either side legitimately empty
        # (initial publish, add-only). No detection.
        return ''
    # Empty-side case fires regardless of project size.
    if on == 0 and tn > 0:
        return (f'ours has 0 entries while base has {bn} and theirs '
                f'has {tn} — ours appears truncated or wiped; '
                f'refusing the destructive merge')
    if tn == 0 and on > 0:
        return (f'theirs has 0 entries while base has {bn} and ours '
                f'has {on} — theirs appears truncated or wiped; '
                f'refusing the destructive merge')
    larger = max(on, tn)
    smaller = min(on, tn)
    if larger < TRUNCATION_MIN_LARGER_SIDE:
        # Small project; the ratio test would false-positive on
        # legitimate small-scale edits.
        return ''
    if smaller * TRUNCATION_RATIO >= larger:
        # Healthy asymmetry — one side smaller but within an
        # order of magnitude of the other.
        # Integer multiplication form (vs ``larger // RATIO``)
        # avoids the floor-rounding gap that previously allowed
        # ``smaller=1, larger=60`` (60//50 = 1, 1 >= 1) to read
        # as healthy even though 1/60 is well below the 1/50
        # threshold the docstring promises. Same pattern as
        # ``_looks_catastrophic_output``.
        return ''
    return (f'ours={on} entries vs theirs={tn} entries '
            f'(base={bn}); one side appears truncated — '
            f'refusing the destructive merge')


def _looks_catastrophic_output(base_count, ours_count,
                                theirs_count, merged_count):
    """Return a non-empty diagnostic when the merge OUTPUT
    collapsed dramatically vs the inputs — even though
    ``_looks_truncated`` accepted the inputs as healthy. Defends
    against **algorithmic** loss inside the merger (the merge
    algorithm itself producing an undersized output) — separate
    from input-side truncation, which the upstream guard already
    catches.

    **Why this guard exists separately from the input guard.**
    Field-reported 2026-05-12 (NOTES_TO_DAEMON.md, reopen after
    0.35.1 cut): the ``baf`` project's merge at git commit
    ``679c102`` synthesized 1 entry from inputs of 1702 and 1700
    entries (base ~1700). Both inputs were full at the moment they
    were committed to git — the input-side truncation guard
    cannot have fired (smaller=1700, ratio test sees
    larger//50=34, 1700>=34, healthy). Yet the daemon committed
    a 1-entry merge result. The proximate cause for the bug
    (whatever made the merger's internal ``ours_entries`` view
    near-empty at the moment of the merge — peer write race,
    snapshot timing, mid-write commit, etc.) is undetermined.
    The output-side guard catches the SYMPTOM unambiguously, no
    matter what caused the input view inside the merger to lose
    its body. Defense-in-depth.

    Thresholds (intentionally conservative — only the obviously
    catastrophic case trips):

    - **Skip small projects.** ``base_count <
      TRUNCATION_MIN_LARGER_SIDE`` returns clean. Tiny projects
      legitimately have wide proportional swings; the ratio
      doesn't generalize.
    - **Skip if an input itself was tinier than base.** If
      ``ours_count`` or ``theirs_count`` is already less than
      half of base, the **input guard** had jurisdiction
      (whether or not it chose to fire). Don't double-attribute
      output loss to the algorithm when an input was already
      suspicious.
    - **Trip when the output is < 1/4 the smaller input.** With
      both inputs healthy (each ≥ base/2) and the output below a
      quarter of the smaller, the merger has lost data
      internally. Legitimate ours- or theirs-side mass-deletes
      of 75%+ would have failed the previous check (the deleting
      side would be tinier than base/2). So this gate only fires
      on the actual bug shape.

    Trips when triggered: caller refuses the merge, keeps the
    larger of ours/theirs intact verbatim, emits a
    ``catastrophic-merge-output`` Conflict carrying the full
    count diagnostic.
    """
    if base_count < TRUNCATION_MIN_LARGER_SIDE:
        return ''
    # If an input was tiny relative to base, the input guard
    # owned this case. The output-side guard concerns itself
    # with healthy inputs only.
    if ours_count * 2 < base_count or theirs_count * 2 < base_count:
        return ''
    smaller_input = min(ours_count, theirs_count)
    if merged_count * CATASTROPHIC_OUTPUT_RATIO >= smaller_input:
        return ''
    return (f'merge produced {merged_count} entries from '
            f'base={base_count} ours={ours_count} '
            f'theirs={theirs_count}; both inputs were healthy '
            f'so the loss is INSIDE the merger — refusing to '
            f'commit')


# ── Ring-buffer trace (forensic context for guard trips) ────────────────
#
# Daemon stderr on Android is logcat, which is ephemeral and not
# retrievable post-hoc (the field-reported ``baf`` collapse at
# 13:17:40 UTC 2026-05-12 was forensically opaque because the
# minute's logs were already gone by the time anyone could ask
# for them). The ring buffer below captures the last N
# trace events in-memory so that when a guard fires and produces
# a forensic XML dump, the recent operations the daemon was
# performing — fetches, merges, commits, lock acquisitions, the
# `[merge-trace]` shape lines — can be embedded into the dump
# alongside the byte hashes. A reader of the dump then has the
# same time-precise log slice the daemon would have shown on
# stderr if anyone had been looking.
#
# Size: 500 entries is roughly the last few minutes of activity
# on a peer that's actively syncing. Larger would risk bloating
# the forensic XML; smaller would miss the relevant pre-merge
# operations.

_TRACE_RING_SIZE = 500
_trace_ring = collections.deque(maxlen=_TRACE_RING_SIZE)
_trace_ring_lock = threading.Lock()


def trace(message):
    """Append *message* to the in-process trace ring buffer AND
    print it to stderr (so the existing logcat flow keeps
    working). Ring-buffer entries are timestamped + tagged with
    the calling thread name so a post-hoc reader can reconstruct
    concurrent threading.

    Existing ``print(..., file=sys.stderr, flush=True)`` call
    sites in the daemon that want to be captured into forensic
    dumps should migrate to ``lift_merge.trace(...)``. Sites
    that haven't been migrated still print to stderr the same
    way; they just don't survive in the ring buffer."""
    ts = time.time()
    tname = threading.current_thread().name
    with _trace_ring_lock:
        _trace_ring.append((ts, tname, message))
    print(message, file=sys.stderr, flush=True)


def _trace_snapshot(seconds_back=120):
    """Return ring-buffer entries from the last *seconds_back*
    seconds as a list of ``(ts, thread_name, message)`` tuples."""
    cutoff = time.time() - seconds_back
    with _trace_ring_lock:
        return [t for t in _trace_ring if t[0] >= cutoff]


# ── Forensic diagnostic dumps ────────────────────────────────────────────
#
# When any of the daemon-side guards trips (parse-error,
# truncation-suspected, catastrophic-merge-output), we want the
# circumstances captured to git in a structured format the daemon
# team can retrieve later. The user shouldn't be bothered by it
# directly — the file lives under a hidden ``.azt-collab/diagnostics/``
# directory in the project working tree, gets staged and pushed
# alongside the normal merge commit, and remains available for
# post-hoc analysis from any clone of the repo.
#
# Format: a single ``<azt-collab-diagnostic>`` XML element with
# attribute-style metadata and small child elements for inputs
# (counts, sizes, hashes, parse errors). Full input bytes are NOT
# embedded — they're reachable via the included git SHAs. Stays
# small (a few KB per dump) so even repeated trips don't bloat
# the repo.

# Diagnostic-file directory under the project working tree.
DIAGNOSTICS_SUBDIR = ('.azt-collab', 'diagnostics')

# Kinds we consider "guard trips" and want to dump on.
_GUARD_KINDS = frozenset({
    'parse-error',
    'truncation-suspected',
    'catastrophic-merge-output',
})


def is_guard_kind(kind):
    """True if *kind* is one of the daemon-side guard-trip
    Conflict kinds — i.e., one that should produce a forensic
    diagnostic file."""
    return kind in _GUARD_KINDS


def diagnostic_filename(guard_kind):
    """Generate a unique filename for a diagnostic dump under
    ``<working_dir>/.azt-collab/diagnostics/``. Includes UTC
    timestamp and a random nonce so concurrent guard trips on the
    same project don't collide."""
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    nonce = secrets.token_hex(4)
    return f'{ts}-{guard_kind}-{nonce}.xml'


def build_diagnostic_xml(*, guard_kind, lift_path='',
                          local_sha='', remote_sha='', base_sha='',
                          base_bytes=b'', ours_bytes=b'',
                          theirs_bytes=b'', merged_bytes=b'',
                          conflict_fields=None,
                          daemon_version='',
                          working_dir='',
                          trace_seconds_back=120):
    """Build a forensic-data XML document describing why a guard
    fired. Returns the XML bytes ready to write.

    Schema (informal; ignore unknown fields)::

        <azt-collab-diagnostic version="2"
                               daemon-version="0.35.x"
                               timestamp-utc="..."
                               guard="parse-error|...|...">
          <merge-context lift-path="..."
                         local-sha="..."
                         remote-sha="..."
                         base-sha="..." />

          <process pid="..." python="..." platform="..."
                   process-start-utc="..." />

          <thread id="..." name="..."
                  enumeration count="..."  <!-- threads in process -->
                  > ... list of <other-thread name="..." id="..."/>
                    children ... </thread>

          <caller-stack>
            <frame file="..." line="..." function="..." />
            ...
          </caller-stack>

          <filesystem-state>
            <file rel-path="baf.lift"
                  exists="true"
                  size-bytes="..."
                  mtime-utc="..."
                  size-vs-ours-blob-bytes="..."  <!-- diff between disk
                                                       and merger's ours -->
                  recently-modified="true|false" />
            ... siblings like ``baf.lift.tmp.*`` if present ...
          </filesystem-state>

          <inputs>
            <input side="base|ours|theirs"
                   byte-length="N"
                   sha256="..."
                   parsed-entry-count="N"
                   parse-error="..." />  <!-- present only when parse fails -->
          </inputs>

          <merged byte-length="N"
                  sha256="..."
                  entry-count="N"
                  parse-error="..." />  <!-- present only when output exists -->

          <conflict-fields>
            <field>...</field>
          </conflict-fields>

          <recent-trace seconds-back="120" event-count="...">
            <event ts="..." thread="..." message="..." />
            ... (last ~minutes of daemon operations)
          </recent-trace>
        </azt-collab-diagnostic>

    Full input bytes are NOT embedded — they're reachable via
    ``git show <sha>:<lift-path>`` from any clone using the
    embedded SHAs. The structural / runtime context (process,
    thread, caller stack, filesystem state at the moment of the
    dump, and the recent-trace ring-buffer slice) IS embedded
    because that's the information not recoverable from git
    afterward. The 2026-05-12 ``baf`` analysis was forensically
    opaque precisely because we had only post-hoc bytes and no
    runtime context; the v2 schema fixes that gap.

    ``working_dir`` is needed for the ``filesystem-state``
    section's stat() calls; pass the project's on-disk
    directory. Empty means the section is omitted.
    """

    def _hash(b):
        return hashlib.sha256(b).hexdigest() if b else ''

    def _parse_summary(b):
        """Return ``(parsed_entry_count, parse_error_str)``. Both
        empty when bytes are empty; ``parse_error_str`` empty on
        success."""
        if not b:
            return '0', ''
        try:
            root = ET.fromstring(b)
        except ET.ParseError as ex:
            return '0', str(ex)
        try:
            n = len(root.findall('entry'))
        except Exception:
            n = 0
        return str(n), ''

    now_utc = datetime.now(timezone.utc)
    root = ET.Element('azt-collab-diagnostic')
    root.set('version', '2')
    root.set('daemon-version', daemon_version or '')
    root.set('timestamp-utc', now_utc.isoformat())
    root.set('guard', guard_kind)

    ctx = ET.SubElement(root, 'merge-context')
    ctx.set('lift-path', lift_path or '')
    ctx.set('local-sha', local_sha or '')
    ctx.set('remote-sha', remote_sha or '')
    ctx.set('base-sha', base_sha or '')

    # ── Process and thread context ──────────────────────────────
    # Caller PID / Python interpreter info + the calling thread's
    # identity. If a future investigation finds two diagnostics
    # with the same PID but different thread IDs close together,
    # that's a same-process concurrent-merge signature; same PID
    # + same thread is a single-thread sequence; different PIDs
    # are two daemon processes (which shouldn't happen but is
    # worth knowing).
    proc = ET.SubElement(root, 'process')
    proc.set('pid', str(os.getpid()))
    proc.set('python', sys.version.replace('\n', ' '))
    proc.set('platform', sys.platform)
    try:
        # Some platforms expose process start time via /proc;
        # best-effort.
        import psutil  # type: ignore
        proc.set('process-start-utc',
                 datetime.fromtimestamp(
                     psutil.Process().create_time(),
                     tz=timezone.utc).isoformat())
    except Exception:
        pass

    cur_thread = threading.current_thread()
    th = ET.SubElement(root, 'thread')
    th.set('id', str(cur_thread.ident))
    th.set('name', cur_thread.name)
    th.set('daemon', '1' if cur_thread.daemon else '0')
    # Enumerate all live threads so the dump captures "what else
    # was running" — a concurrent sync from another peer or the
    # scheduler shows up here as another live thread distinct
    # from the merging one.
    all_threads = threading.enumerate()
    th.set('enumeration-count', str(len(all_threads)))
    for other in all_threads:
        if other is cur_thread:
            continue
        ot = ET.SubElement(th, 'other-thread')
        ot.set('id', str(other.ident))
        ot.set('name', other.name)
        ot.set('daemon', '1' if other.daemon else '0')
        ot.set('alive', '1' if other.is_alive() else '0')

    # ── Caller stack at the moment of the dump ──────────────────
    # Lets a post-hoc reader see which sync flow triggered this
    # merge: scheduler vs request_sync vs commit_audio_and_sync
    # etc. Includes file paths and line numbers so call-site
    # archaeology against the daemon source is trivial.
    cs = ET.SubElement(root, 'caller-stack')
    for frame in traceback.extract_stack():
        fr = ET.SubElement(cs, 'frame')
        fr.set('file', frame.filename)
        fr.set('line', str(frame.lineno))
        fr.set('function', frame.name or '')
        if frame.line:
            fr.text = frame.line

    # ── Filesystem-state snapshot ───────────────────────────────
    # Captures what's on disk RIGHT NOW vs what the merger
    # received as input bytes. If ``ours_bytes`` was a 50-byte
    # truncated XML at parse time but the file on disk is now
    # 500 KB, a peer finished writing the LIFT between when the
    # merger read it (from a git blob) and when this diagnostic
    # ran. That's diagnostic gold.
    #
    # Also lists ``.tmp.*`` sidecars under the lift's directory —
    # a tempfile sticking around suggests an atomic-write that
    # didn't complete (and might be related to whatever wrote
    # the bad bytes).
    if working_dir and lift_path:
        fs = ET.SubElement(root, 'filesystem-state')
        _add_fs_entry(fs, working_dir, lift_path, ours_bytes)
        # Sidecars near the lift file
        lift_full = os.path.join(working_dir, lift_path)
        lift_dir = os.path.dirname(lift_full) or working_dir
        try:
            for name in os.listdir(lift_dir):
                full = os.path.join(lift_dir, name)
                if name.startswith(os.path.basename(lift_full) + '.tmp'):
                    _add_fs_entry(fs, working_dir,
                                  os.path.relpath(full, working_dir),
                                  None, hint='tempfile-sidecar')
        except OSError:
            pass

    # ── Input byte summaries ────────────────────────────────────
    inputs_elem = ET.SubElement(root, 'inputs')
    for side, b in (('base', base_bytes),
                    ('ours', ours_bytes),
                    ('theirs', theirs_bytes)):
        inp = ET.SubElement(inputs_elem, 'input')
        inp.set('side', side)
        inp.set('byte-length', str(len(b)))
        inp.set('sha256', _hash(b))
        parsed_n, err = _parse_summary(b)
        inp.set('parsed-entry-count', parsed_n)
        if err:
            inp.set('parse-error', err)

    # ── Merged output summary (if produced) ─────────────────────
    if merged_bytes:
        m = ET.SubElement(root, 'merged')
        m.set('byte-length', str(len(merged_bytes)))
        m.set('sha256', _hash(merged_bytes))
        parsed_n, err = _parse_summary(merged_bytes)
        m.set('entry-count', parsed_n)
        if err:
            m.set('parse-error', err)

    # ── Conflict-field diagnostic strings ───────────────────────
    if conflict_fields:
        fields_elem = ET.SubElement(root, 'conflict-fields')
        for f in conflict_fields:
            fe = ET.SubElement(fields_elem, 'field')
            fe.text = str(f)

    # ── Recent trace ring buffer slice ──────────────────────────
    # The single most useful bit when nothing else explains it.
    # Captures the daemon's recent operations — fetches, merges,
    # commits, sync-rpc starts, etc. — so a post-hoc reader sees
    # the timeline of what was happening when the guard fired.
    # Only entries emitted via ``lift_merge.trace(...)`` show up
    # here; bare ``print(..., file=sys.stderr, ...)`` calls don't.
    # Migration is gradual; this section will become more useful
    # as more daemon trace sites move to ``trace()``.
    snap = _trace_snapshot(seconds_back=trace_seconds_back)
    rt = ET.SubElement(root, 'recent-trace')
    rt.set('seconds-back', str(trace_seconds_back))
    rt.set('event-count', str(len(snap)))
    for (ts, tname, msg) in snap:
        ev = ET.SubElement(rt, 'event')
        ev.set('ts',
               datetime.fromtimestamp(ts, tz=timezone.utc).isoformat())
        ev.set('thread', tname)
        ev.text = msg

    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _add_fs_entry(parent, working_dir, rel_path,
                   reference_bytes=None, hint=''):
    """Append a ``<file>`` element describing the filesystem state
    of ``rel_path`` under ``working_dir`` at the moment of the
    dump. ``reference_bytes`` (if provided) is compared by length
    to the on-disk size; mismatch is highlighted as a hint that
    something has rewritten the file between the merger's read
    and the diagnostic dump."""
    full = os.path.join(working_dir, rel_path)
    f = ET.SubElement(parent, 'file')
    f.set('rel-path', rel_path)
    if hint:
        f.set('hint', hint)
    try:
        st = os.stat(full)
    except OSError as ex:
        f.set('exists', 'false')
        f.set('stat-error', str(ex))
        return
    f.set('exists', 'true')
    f.set('size-bytes', str(st.st_size))
    f.set('mtime-utc',
          datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat())
    # Recent-modification flag — within 60s of the dump.
    f.set('recently-modified',
          'true' if (time.time() - st.st_mtime) < 60 else 'false')
    if reference_bytes is not None:
        f.set('size-vs-reference-bytes',
              str(st.st_size - len(reference_bytes)))


def three_way_merge(base_bytes, ours_bytes, theirs_bytes, path=''):
    """Merge LIFT XML by entry guid. Returns ``MergeResult`` with the
    merged bytes and a list of Conflicts. The base may be empty
    (``b''``); the algorithm treats that as "no shared history" and
    falls back to add-add semantics."""
    base, base_err = _parse(base_bytes, 'base')
    ours, ours_err = _parse(ours_bytes, 'ours')
    theirs, theirs_err = _parse(theirs_bytes, 'theirs')

    # Forensic trace. Records what the merger ACTUALLY saw, not
    # what's in git afterwards. Distinguishes "we got empty
    # because XML was malformed and _parse masked it" from "we
    # got empty because the input was genuinely empty" — which
    # was unanswerable in the 2026-05-12 ``baf`` repro because
    # the daemon at the time didn't log the input shape.
    #
    # Routed through ``trace()`` so the ring buffer captures
    # this for inclusion in any subsequent diagnostic dump.
    _base_n = len(base.findall('entry'))
    _ours_n = len(ours.findall('entry'))
    _theirs_n = len(theirs.findall('entry'))
    _err_summary = ''
    if base_err:
        _err_summary += f' base_err={base_err!r}'
    if ours_err:
        _err_summary += f' ours_err={ours_err!r}'
    if theirs_err:
        _err_summary += f' theirs_err={theirs_err!r}'
    trace(f'[merge-trace] path={path!r} '
          f'base={_base_n} ours={_ours_n} theirs={_theirs_n}'
          f'{_err_summary}')

    # Parse-error guard. If ours or theirs failed to parse, refuse
    # the destructive merge — keep the side that parsed cleanly.
    # ``base`` failing is a separate concern (the algorithm can
    # still work with no-base semantics); we surface it as a
    # Conflict but proceed.
    if ours_err or theirs_err:
        if ours_err and theirs_err:
            # Both sides corrupted. Keep base, but warn loudly.
            kept = base
            diag = f'both ours and theirs failed to parse: {ours_err!r}; {theirs_err!r}'
            side_label = 'base-kept-intact'
        elif ours_err:
            kept = theirs
            diag = ours_err
            side_label = 'theirs-kept-intact'
        else:
            kept = ours
            diag = theirs_err
            side_label = 'ours-kept-intact'
        return MergeResult(
            merged_bytes=ET.tostring(
                kept, encoding='utf-8', xml_declaration=True),
            conflicts=[Conflict(
                path=path, guid='',
                kind='parse-error',
                fields=[diag, side_label])])

    base_entries = _entries(base)
    ours_entries = _entries(ours)
    theirs_entries = _entries(theirs)

    # Truncation guard. See ``_looks_truncated`` for the rules.
    truncation_diag = _looks_truncated(
        base_entries, ours_entries, theirs_entries)
    if truncation_diag:
        if len(ours_entries) >= len(theirs_entries):
            kept = ours
            side_label = 'ours-kept-intact'
        else:
            kept = theirs
            side_label = 'theirs-kept-intact'
        return MergeResult(
            merged_bytes=ET.tostring(
                kept, encoding='utf-8', xml_declaration=True),
            conflicts=[Conflict(
                path=path, guid='',
                kind='truncation-suspected',
                fields=[truncation_diag, side_label])])

    # Start the merged doc from the structure of "ours" (header,
    # ranges, etc.) but drop all entries.
    merged_root = _strip_entries(ours)

    conflicts = []
    repairs = 0

    # Emit order: ``ours`` document order first, then any
    # theirs-only entries in theirs's document order. Anchoring on
    # ours keeps the merge commit diffable (only actually-changed
    # entries move). Base-only guids (deleted on both sides, or
    # never present on either side) are naturally excluded.
    seen = set()
    ours_order = []
    for _e in ours.findall('entry'):
        _g = _e.attrib.get('guid', '')
        if _g and _g not in seen:
            ours_order.append(_g)
            seen.add(_g)
    theirs_only = []
    for _e in theirs.findall('entry'):
        _g = _e.attrib.get('guid', '')
        if _g and _g not in seen:
            theirs_only.append(_g)
            seen.add(_g)

    for guid in ours_order + theirs_only:
        b = base_entries.get(guid)
        o = ours_entries.get(guid)
        t = theirs_entries.get(guid)

        if o is None and t is None:
            # Deleted on both sides (or never existed in either).
            continue

        # ── theirs deleted (entry-level) ───────────────────────────
        if o is not None and t is None:
            if b is None:
                # Ours added; theirs hasn't seen it. Keep.
                merged_root.append(_clone(o))
                continue
            if _canon(b) == _canon(o):
                # We didn't change it; theirs deleted it. Honour delete.
                continue
            # We changed; theirs deleted → conflict. Keep ours, marked.
            merged_root.append(_annotated_clone(o, 'ours'))
            conflicts.append(Conflict(
                path=path, guid=guid, kind='modify-delete'))
            continue

        # ── ours deleted (entry-level) ─────────────────────────────
        if o is None and t is not None:
            if b is None:
                merged_root.append(_clone(t))
                continue
            if _canon(b) == _canon(t):
                continue   # they didn't change it; we deleted
            merged_root.append(_annotated_clone(t, 'theirs'))
            conflicts.append(Conflict(
                path=path, guid=guid, kind='delete-modify'))
            continue

        # ── both present: recursive field-level merge ──────────────
        # Entries are "multi in lift" (a 0..* sequence), so the
        # top-level call passes parent_allows_multi=True. In the
        # rare case where the entry pair can't be narrowed at all
        # (e.g., they differ only in entry-level attributes), the
        # recursion bottoms out at the entry level and emits two
        # entries — same fallback the historical pre-0.35.2 merge
        # always used. ``_merge_pair`` handles the synthetic guid
        # suffix in that fallback to keep the document valid.
        merged = _merge_pair(b, o, t, parent_allows_multi=True)
        if isinstance(merged, _Escalate):
            # Shouldn't happen at the entry level (lift allows
            # multi entry), but defend.
            merged = [_annotated_clone(o, 'ours'),
                      _annotated_clone(t, 'theirs')]

        # Disambiguate guids if we ended up with two entries
        # carrying the same one (entry-attribute conflicts).
        if (len(merged) == 2
                and merged[0].attrib.get('guid')
                == merged[1].attrib.get('guid')):
            merged[1].set('guid', f'{guid}-theirs')

        # Post-merge invariant BEFORE the conflict scan: duplicate
        # same-lang forms either collapse (identical), union
        # (verification fields — content-level auto-resolution, so
        # no conflict survives to be scanned), or get annotated.
        for elem in merged:
            repairs += _normalize_entry(elem, path=path)

        # Did this merge produce a conflict? Look for the marker
        # annotation anywhere in the merged result.
        conflict_paths = []
        kind = ''
        for elem in merged:
            for ann in elem.iter('annotation'):
                if ann.attrib.get('name') == CONFLICT_ANNOTATION_NAME:
                    conflict_paths = _collect_conflict_paths(elem)
                    kind = 'add-add' if b is None else 'modify-modify'
                    break
            if kind:
                break

        # Add the entry-level conflict marker + trait so peers can
        # detect "this entry needs attention" without scanning the
        # whole tree. Only when we have at least one conflict
        # inside.
        if kind:
            # Attach to the first merged element (the canonical
            # ours-side entry, or the only one if narrowed inside).
            target = merged[0]
            if not any(ann.attrib.get('name') == CONFLICT_ANNOTATION_NAME
                       and ann.attrib.get('value') == 'conflict'
                       for ann in target.findall('annotation')):
                # Avoid double-marking when the entry itself was
                # the one duplicated (rare attribute-conflict case
                # where the inner annotated_clone already added a
                # marker).
                entry_marker = ET.SubElement(target, 'annotation')
                entry_marker.set('name', CONFLICT_ANNOTATION_NAME)
                entry_marker.set('value', 'conflict')
                if conflict_paths:
                    trait = ET.SubElement(entry_marker, 'trait')
                    trait.set('name', CONFLICT_FIELDS_TRAIT)
                    trait.set('value', ','.join(conflict_paths))
            conflicts.append(Conflict(
                path=path, guid=guid, kind=kind,
                fields=conflict_paths))

        for elem in merged:
            merged_root.append(elem)

    # Final invariant sweep over EVERY entry in the output —
    # entries taken cleanly from one side (unchanged, one-side-
    # changed, one-side-added) bypass ``_merge_pair`` and can carry
    # duplicate same-lang forms from polluted history; this sweep
    # self-heals them the same way the canon-equal path self-heals
    # stale conflict annotations. Idempotent, so re-visiting the
    # entries normalized in the loop above is a silent no-op.
    for e in merged_root.findall('entry'):
        repairs += _normalize_entry(e, path=path)
    if repairs:
        trace(f'[merge-repair] path={path!r} total repairs={repairs}')

    # Output-side catastrophic-loss guard. Defends against
    # algorithmic loss inside the merger itself, distinct from
    # input-side truncation (which the upstream guard catches).
    # Reopened-bug field repro: ``baf`` 2026-05-12, daemon-synth
    # merge ``679c102`` produced 1 entry from 1700+1700 healthy
    # inputs. Whatever made the merger's internal view of ours
    # look near-empty at the moment of the merge — the
    # proximate cause is undetermined; could be peer write race,
    # snapshot timing across two merge calls, mid-write commit
    # state, etc. — the output guard catches the symptom
    # regardless. NOTES_TO_DAEMON.md (closed 2026-05-12) carries
    # the commit-level evidence; this guard is the defense.
    merged_count = len(merged_root.findall('entry'))
    catastrophic = _looks_catastrophic_output(
        len(base_entries), len(ours_entries),
        len(theirs_entries), merged_count)
    if catastrophic:
        if len(ours_entries) >= len(theirs_entries):
            kept = ours
            side_label = 'ours-kept-intact'
        else:
            kept = theirs
            side_label = 'theirs-kept-intact'
        return MergeResult(
            merged_bytes=ET.tostring(
                kept, encoding='utf-8', xml_declaration=True),
            conflicts=[Conflict(
                path=path, guid='',
                kind='catastrophic-merge-output',
                fields=[catastrophic, side_label])])

    merged_bytes = ET.tostring(
        merged_root, encoding='utf-8', xml_declaration=True)
    return MergeResult(merged_bytes=merged_bytes, conflicts=conflicts,
                       repairs=repairs)
