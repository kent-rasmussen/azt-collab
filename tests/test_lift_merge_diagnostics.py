"""Tests for the forensic-diagnostic surface in ``azt_collabd.lift_merge``.

What's covered:

- ``is_guard_kind`` recognises the three guard-trip conflict kinds and
  rejects others.
- ``diagnostic_filename`` produces a parseable, unique-enough filename
  format.
- ``build_diagnostic_xml`` emits well-formed v2 XML with every section
  the post-hoc analyst expects.
- The ``recent-trace`` section captures messages emitted via
  ``lift_merge.trace(...)`` immediately before the dump.
- The ``filesystem-state`` section reflects the real on-disk state of
  the lift path under ``working_dir`` (including a tempfile sidecar if
  one is present).
- Parse-error / catastrophic-output guard inputs surface their
  diagnostic shape (``parse-error`` attribute, byte/sha/entry-count
  attributes).

Why this exists: the dump path was added late in the 0.35.4 cycle to
make every future guard trip reconstructable from the data repo
without bothering the user. Pre-test, no automated assertion proved
the schema actually serialises — the first proof was always going to
be a real field guard firing. These tests make the dump path itself
exercise-able from CI.
"""

import xml.etree.ElementTree as ET

from azt_collabd import lift_merge as lm


# ── is_guard_kind ─────────────────────────────────────────────────────────


def test_is_guard_kind_recognises_each_guard():
    assert lm.is_guard_kind('parse-error') is True
    assert lm.is_guard_kind('truncation-suspected') is True
    assert lm.is_guard_kind('catastrophic-merge-output') is True


def test_is_guard_kind_rejects_normal_conflict_kinds():
    assert lm.is_guard_kind('modify-modify') is False
    assert lm.is_guard_kind('non-lift-modify-modify') is False
    assert lm.is_guard_kind('') is False
    assert lm.is_guard_kind('parse-error ') is False  # trailing space


# ── diagnostic_filename ───────────────────────────────────────────────────


def test_diagnostic_filename_contains_kind_and_is_xml():
    name = lm.diagnostic_filename('parse-error')
    assert name.endswith('.xml')
    assert 'parse-error' in name


def test_diagnostic_filename_is_unique_per_call():
    # Two back-to-back calls in the same second still differ because
    # of the random nonce — concurrent guard trips on the same project
    # can't collide on the filesystem.
    names = {lm.diagnostic_filename('parse-error') for _ in range(20)}
    assert len(names) == 20


# ── build_diagnostic_xml — basic well-formedness and shape ────────────────


def _build(**kw):
    """Convenience: build a dump with defaults filled in."""
    defaults = dict(
        guard_kind='parse-error',
        lift_path='baf.lift',
        local_sha='a' * 40, remote_sha='b' * 40, base_sha='c' * 40,
        base_bytes=b'<lift version="0.13"/>',
        ours_bytes=b'<lift version="0.13"/>',
        theirs_bytes=b'<lift version="0.13"/>',
        merged_bytes=b'',
        conflict_fields=['something happened'],
        daemon_version='0.35.4',
        working_dir='',
    )
    defaults.update(kw)
    return lm.build_diagnostic_xml(**defaults)


def test_dump_is_well_formed_xml_with_v2_root_attrs():
    xml_bytes = _build()
    root = ET.fromstring(xml_bytes)
    assert root.tag == 'azt-collab-diagnostic'
    assert root.get('version') == '2'
    assert root.get('daemon-version') == '0.35.4'
    assert root.get('guard') == 'parse-error'
    # timestamp-utc is set to an ISO-formatted UTC time
    assert root.get('timestamp-utc', '').endswith('+00:00')


def test_dump_includes_every_v2_section():
    root = ET.fromstring(_build())
    # The schema documents these as guaranteed-present sections.
    assert root.find('merge-context') is not None
    assert root.find('process') is not None
    assert root.find('thread') is not None
    assert root.find('caller-stack') is not None
    assert root.find('inputs') is not None
    assert root.find('conflict-fields') is not None
    assert root.find('recent-trace') is not None


def test_merge_context_carries_supplied_shas():
    root = ET.fromstring(_build(
        local_sha='deadbeef' * 5,
        remote_sha='cafef00d' * 5,
        base_sha='12345678' * 5,
    ))
    ctx = root.find('merge-context')
    assert ctx.get('local-sha') == 'deadbeef' * 5
    assert ctx.get('remote-sha') == 'cafef00d' * 5
    assert ctx.get('base-sha') == '12345678' * 5
    assert ctx.get('lift-path') == 'baf.lift'


def test_process_section_carries_pid_and_python():
    import os
    root = ET.fromstring(_build())
    proc = root.find('process')
    assert proc.get('pid') == str(os.getpid())
    assert proc.get('python')  # non-empty


def test_thread_section_carries_running_thread_identity():
    import threading
    root = ET.fromstring(_build())
    th = root.find('thread')
    assert th.get('id') == str(threading.current_thread().ident)
    assert th.get('name') == threading.current_thread().name


# ── Inputs and merged byte summaries ──────────────────────────────────────


def test_input_byte_lengths_and_entry_counts_match_supplied_bytes():
    base = b'<lift><entry guid="1"/><entry guid="2"/></lift>'
    ours = b'<lift><entry guid="1"/></lift>'
    theirs = b'<lift><entry guid="1"/><entry guid="2"/><entry guid="3"/></lift>'
    root = ET.fromstring(_build(
        base_bytes=base, ours_bytes=ours, theirs_bytes=theirs))
    inputs = {i.get('side'): i for i in root.find('inputs')}
    assert inputs['base'].get('byte-length') == str(len(base))
    assert inputs['base'].get('parsed-entry-count') == '2'
    assert inputs['ours'].get('parsed-entry-count') == '1'
    assert inputs['theirs'].get('parsed-entry-count') == '3'
    # sha256s are 64 hex chars and differ across distinct payloads.
    shas = {inputs[s].get('sha256') for s in ('base', 'ours', 'theirs')}
    assert all(len(s) == 64 for s in shas)
    assert len(shas) == 3


def test_parse_error_attribute_present_on_malformed_input():
    # Truncated mid-tag — ET.fromstring raises ParseError.
    malformed = b'<lift><entry guid="1"><lexical-unit'
    root = ET.fromstring(_build(ours_bytes=malformed))
    ours = next(
        i for i in root.find('inputs') if i.get('side') == 'ours')
    assert ours.get('parse-error')  # non-empty
    assert ours.get('parsed-entry-count') == '0'


def test_merged_section_omitted_when_no_merged_bytes():
    root = ET.fromstring(_build(merged_bytes=b''))
    assert root.find('merged') is None


def test_merged_section_present_when_merged_bytes_provided():
    merged = b'<lift><entry guid="1"/></lift>'
    root = ET.fromstring(_build(merged_bytes=merged))
    m = root.find('merged')
    assert m is not None
    assert m.get('byte-length') == str(len(merged))
    assert m.get('entry-count') == '1'


# ── Conflict-fields ───────────────────────────────────────────────────────


def test_conflict_fields_renders_each_supplied_string():
    root = ET.fromstring(_build(
        conflict_fields=['ours: parse-error at line 1',
                         'theirs-kept-intact']))
    fields = [f.text for f in root.find('conflict-fields')]
    assert fields == ['ours: parse-error at line 1',
                      'theirs-kept-intact']


# ── recent-trace: ring-buffer round-trip ──────────────────────────────────


def test_recent_trace_captures_messages_emitted_via_trace():
    sentinel = f'[test-trace] sentinel-{id(test_recent_trace_captures_messages_emitted_via_trace)}'
    lm.trace(sentinel)
    root = ET.fromstring(_build())
    rt = root.find('recent-trace')
    assert rt is not None
    messages = [ev.text for ev in rt.findall('event')]
    assert sentinel in messages


def test_recent_trace_ignores_messages_older_than_window():
    # Push an event with a forged timestamp from 1h ago directly into
    # the ring buffer (bypassing trace()), then build a dump with a
    # 60-second window. The forged event should NOT appear.
    import time as _time
    old_msg = f'[test-trace] old-{id(test_recent_trace_ignores_messages_older_than_window)}'
    new_msg = f'[test-trace] new-{id(test_recent_trace_ignores_messages_older_than_window)}'
    with lm._trace_ring_lock:
        lm._trace_ring.append(
            (_time.time() - 3600, 'TestThread', old_msg))
    lm.trace(new_msg)
    root = ET.fromstring(_build(trace_seconds_back=60))
    msgs = {ev.text for ev in root.find('recent-trace').findall('event')}
    assert new_msg in msgs
    assert old_msg not in msgs


def test_recent_trace_event_carries_iso_timestamp_and_thread_name():
    msg = f'[test-trace] iso-{id(test_recent_trace_event_carries_iso_timestamp_and_thread_name)}'
    lm.trace(msg)
    root = ET.fromstring(_build())
    rt = root.find('recent-trace')
    ev = next(e for e in rt.findall('event') if e.text == msg)
    # ISO-UTC: ends with '+00:00'
    assert ev.get('ts', '').endswith('+00:00')
    assert ev.get('thread')  # non-empty


# ── filesystem-state: real on-disk inspection ─────────────────────────────


def test_filesystem_state_reflects_real_lift_size(tmp_path):
    # Write a small "lift" file to a temp working dir, run the dump
    # with working_dir + lift_path pointed at it, and confirm the
    # filesystem-state entry reports the actual size.
    lift_bytes = b'<lift version="0.13"><entry guid="1"/></lift>'
    (tmp_path / 'baf.lift').write_bytes(lift_bytes)
    root = ET.fromstring(_build(
        working_dir=str(tmp_path),
        lift_path='baf.lift',
    ))
    fs = root.find('filesystem-state')
    assert fs is not None
    files = list(fs.findall('file'))
    # Main lift entry should exist with the right byte length.
    main = next(f for f in files if f.get('rel-path') == 'baf.lift')
    assert main.get('exists') == 'true'
    assert main.get('size-bytes') == str(len(lift_bytes))


def test_filesystem_state_reports_missing_when_lift_absent(tmp_path):
    # working_dir exists but the lift_path doesn't; dump should
    # mark exists=false rather than crash.
    root = ET.fromstring(_build(
        working_dir=str(tmp_path),
        lift_path='nonexistent.lift',
    ))
    fs = root.find('filesystem-state')
    files = list(fs.findall('file'))
    main = next(f for f in files if f.get('rel-path') == 'nonexistent.lift')
    assert main.get('exists') == 'false'


def test_filesystem_state_lists_tempfile_sidecar(tmp_path):
    # An atomic_open_write that didn't complete leaves a
    # ``baf.lift.tmp.<pid>.<nonce>`` sidecar; the dump should
    # surface it so the analyst can correlate.
    (tmp_path / 'baf.lift').write_bytes(b'<lift/>')
    (tmp_path / 'baf.lift.tmp.12345.deadbeef').write_bytes(b'partial')
    root = ET.fromstring(_build(
        working_dir=str(tmp_path),
        lift_path='baf.lift',
    ))
    fs = root.find('filesystem-state')
    rels = {f.get('rel-path') for f in fs.findall('file')}
    assert 'baf.lift' in rels
    assert 'baf.lift.tmp.12345.deadbeef' in rels


def test_filesystem_state_omitted_without_working_dir():
    # The contract is: empty working_dir means "no filesystem-state
    # section." Synthetic / unit-test callers that don't have a
    # real project dir get a smaller dump rather than a crash.
    root = ET.fromstring(_build(working_dir=''))
    assert root.find('filesystem-state') is None
