"""
LIFT-aware three-way merge.

Merges by entry ``guid``. For each guid present in any of the three
inputs (base, ours, theirs):

* If only one side has it (and it didn't exist in base), keep it.
* If one side modified the entry and the other didn't touch it, take
  the modified side.
* If both sides modified the entry to the same canonical XML, take it.
* If both sides modified the entry differently, surface a conflict:
  keep both versions, each tagged with
  ``<annotation name="azt-lift-conflict" value="ours|theirs">``.
  The "theirs" copy gets a synthetic guid suffix so the resulting
  document is still valid.
* Modify-vs-delete is treated as a conflict: keep the modified side
  with the conflict annotation.

Granularity is per-entry, not per-field, in v1. Per-field merging is
a future improvement; the interface (Conflict + MergeResult) won't
change.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


CONFLICT_ANNOTATION_NAME = 'azt-lift-conflict'


@dataclass
class Conflict:
    path: str = ''
    guid: str = ''
    kind: str = ''   # 'modify-modify' | 'modify-delete' | 'delete-modify' | 'add-add'

    def to_dict(self):
        return {'path': self.path, 'guid': self.guid, 'kind': self.kind}


@dataclass
class MergeResult:
    merged_bytes: bytes = b''
    conflicts: list = field(default_factory=list)


def _parse(xml_bytes):
    if not xml_bytes:
        return ET.fromstring(b'<lift version="0.13"></lift>')
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError:
        # Best-effort empty doc on malformed input
        return ET.fromstring(b'<lift version="0.13"></lift>')


def _entries(root):
    out = {}
    for e in list(root.findall('entry')):
        guid = e.attrib.get('guid', '')
        if guid:
            out[guid] = e
    return out


def _canon(elem):
    return ET.tostring(elem, encoding='utf-8')


def _strip_entries(root):
    """Return a copy of *root* with all <entry> children removed."""
    copy = ET.fromstring(_canon(root))
    for e in list(copy.findall('entry')):
        copy.remove(e)
    return copy


def _annotated_copy(entry, side):
    copy = ET.fromstring(_canon(entry))
    a = ET.SubElement(copy, 'annotation')
    a.set('name', CONFLICT_ANNOTATION_NAME)
    a.set('value', side)
    return copy


def three_way_merge(base_bytes, ours_bytes, theirs_bytes, path=''):
    """Merge LIFT XML by entry guid. Returns ``MergeResult`` with the
    merged bytes and a list of Conflicts (each carrying the entry
    guid + kind). The base may be empty (b''); the algorithm treats
    that as "no shared history" and falls back to add-add semantics."""
    base = _parse(base_bytes)
    ours = _parse(ours_bytes)
    theirs = _parse(theirs_bytes)

    base_entries = _entries(base)
    ours_entries = _entries(ours)
    theirs_entries = _entries(theirs)

    # Start the merged doc from the structure of "ours" (header,
    # ranges, etc.) but drop all entries.
    merged_root = _strip_entries(ours)

    conflicts = []
    all_guids = set(base_entries) | set(ours_entries) | set(theirs_entries)

    for guid in sorted(all_guids):
        b = base_entries.get(guid)
        o = ours_entries.get(guid)
        t = theirs_entries.get(guid)

        if o is None and t is None:
            # Deleted on both sides (or never existed in either)
            continue

        # ── theirs deleted ──────────────────────────────────────────
        if o is not None and t is None:
            if b is None:
                # Ours added; theirs hasn't seen it. Keep.
                merged_root.append(ET.fromstring(_canon(o)))
                continue
            if _canon(b) == _canon(o):
                # We didn't change it; theirs deleted it. Honour delete.
                continue
            # We changed; theirs deleted → conflict. Keep ours, marked.
            merged_root.append(_annotated_copy(o, 'ours'))
            conflicts.append(Conflict(
                path=path, guid=guid, kind='modify-delete'))
            continue

        # ── ours deleted ────────────────────────────────────────────
        if o is None and t is not None:
            if b is None:
                merged_root.append(ET.fromstring(_canon(t)))
                continue
            if _canon(b) == _canon(t):
                continue   # they didn't change it; we deleted
            merged_root.append(_annotated_copy(t, 'theirs'))
            conflicts.append(Conflict(
                path=path, guid=guid, kind='delete-modify'))
            continue

        # ── both present ────────────────────────────────────────────
        oc = _canon(o)
        tc = _canon(t)
        if b is None:
            # Both added independently
            if oc == tc:
                merged_root.append(ET.fromstring(oc))
                continue
            merged_root.append(_annotated_copy(o, 'ours'))
            theirs_marked = _annotated_copy(t, 'theirs')
            theirs_marked.set('guid', f'{guid}-theirs')
            merged_root.append(theirs_marked)
            conflicts.append(Conflict(
                path=path, guid=guid, kind='add-add'))
            continue

        bc = _canon(b)
        if oc == tc:
            merged_root.append(ET.fromstring(oc))
        elif oc == bc:
            # Only theirs changed — take theirs
            merged_root.append(ET.fromstring(tc))
        elif tc == bc:
            # Only ours changed — take ours
            merged_root.append(ET.fromstring(oc))
        else:
            # Both changed differently → conflict
            merged_root.append(_annotated_copy(o, 'ours'))
            theirs_marked = _annotated_copy(t, 'theirs')
            theirs_marked.set('guid', f'{guid}-theirs')
            merged_root.append(theirs_marked)
            conflicts.append(Conflict(
                path=path, guid=guid, kind='modify-modify'))

    merged_bytes = ET.tostring(
        merged_root, encoding='utf-8', xml_declaration=True)
    return MergeResult(merged_bytes=merged_bytes, conflicts=conflicts)
