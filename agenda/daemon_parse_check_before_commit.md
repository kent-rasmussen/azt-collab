# The daemon's merge output is never parse-checked before it is committed

- **Scope & relationships:** azt-collab/daemon — `lift_merge.three_way_merge` and the
  commit path that follows it.
  - Counterpart of the azt-side gate shipped in azt 1.14.0: `Lift._written_file_sane`
    now parses every file azt writes BEFORE it replaces the user's data or reaches
    `submit()`, and refuses the save otherwise.
- **Vision / done-criteria:** nothing that fails to parse can be committed, whichever
  side produced it.
- **Deadline:** none
- **Waiting on:** Nothing

## The gap

azt validates the `.part` it hands over. The daemon then does a base-aware merge and
writes a **different** file — the merged result — which nothing on the azt side has seen.
So the azt gate, by construction, cannot cover what actually lands in the working tree
and gets committed.

## What is already there, and what it doesn't cover

`lift_merge` has real guards, added after a field repro where a merge went 1700 entries
to 1 field:

- `_looks_truncated` (input-side: refuses if one side is <1/50 of the larger ≥50-entry
  side, or empty);
- `_looks_catastrophic_output` (output-side: refuses if merged <1/4 of the smaller input).

Both are RATIO guards — they catch collapse, not malformedness. A well-formedness break
of the kind azt hit (an attribute value carrying an unescaped `"`) would sail through: the
file is complete, correctly terminated, and full-sized.

Mitigating, and worth confirming rather than assuming: merge output is built with
`ET.tostring` (`lift_merge.py:219, 277, 1474, 1575, 1597, 1798, 1805`), and a tree that
was parsed and re-serialised is well-formed by construction — PROVIDED every attribute
value is a `str`. That proviso is exactly what failed on the azt side: ElementTree's
`_escape_attrib` silently passes non-strings through (`"&" in tuple` is a membership test
that returns False rather than raising), and the serializer then writes `str(value)` —
a Python repr, quotes and all, unescaped. So "we build it with ET" is not by itself a
guarantee.

## Plan

1. Parse the merged bytes before committing them — `ET.fromstring(merged_bytes)` is one
   line and the merge already holds them in memory, so there is no extra I/O.
2. On failure, refuse the merge as the existing guards do (typed `Conflict`, keep the
   healthy side) rather than committing something unparseable.
3. Consider the same check on the post-receive reset path, which writes the working tree
   from git.

**While adding a parse here, note what it is parsing.** Merged bytes on the daemon derive
from a PEER's file, so this is untrusted input, and stdlib ElementTree is open to XXE and
billion-laughs by default. `defusedxml` is the standard answer. There is already a
deferred "XXE hardening" note on `investigate_template_problems.md`; adding a parse to a
peer-facing path is the moment to stop deferring it, or at least to decide deliberately
not to.

## Notes

- Filed 2026-08-26, split out of azt's `lift_corruption_rollback.md`. Written from the
  azt side; the line references are from reading, not from running.

## Research
