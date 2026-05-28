"""
Auto-recovery of orphaned ``.azt_atomic_pending/<token>`` files.

The ``atomic_open_write`` protocol is two-phase: peer streams full
LIFT bytes to ``<working_dir>/.azt_atomic_pending/<token>``, then
the daemon's ``atomic_finalize`` step renames that scratch file
over the real LIFT path. A peer crash, daemon kill, or transport
break between the two phases leaves the scratch on disk —
complete, well-formed LIFT, but never landed.

Recovery contract — single auto-merge surface, no user gesture:

  - Hash-equal to current LIFT → delete in place (confirmable
    garbage; this orphan never had anything new to offer).
  - All shared guids byte-identical in canonical XML AND no
    orphan-only entries → delete in place (subset; same
    information already present in current).
  - Otherwise → run ``lift_merge.three_way_merge`` with
    base=b'' (no shared history), ours=current, theirs=orphan.
    Write merged bytes atomically over the LIFT path, commit
    as "Recovered orphan from <iso-timestamp>". Conflicts get
    the existing ``<annotation name="azt-lift-conflict">``
    treatment — peers / viewers that already surface those
    naturally show recovery conflicts without any new code
    path. Delete the orphan on successful commit.
  - Merge raises (corrupt XML, broken byte stream from an
    interrupted Phase 1 write that *looked* > 60 s old) →
    move the orphan to ``.azt_atomic_orphans/unmergeable/
    <token>.lift`` for manual inspection. Logged loudly.

The "no user gesture" design is deliberate: in a no-delete-of-
LIFT-entries world, merge is unambiguously lossless. Adding a
"Merge or Discard?" prompt would ask users a question most
aren't competent to answer, and the safe answer ("merge") is
the only reasonable default anyway.

Throttled: callers (the scheduler's watcher loop) call
``recover_project_orphans`` cheaply on every tick; the function
itself returns immediately if no orphans are present (single
``os.listdir`` on a typically-empty directory).

This module is the daemon-side equivalent of the diagnostic
walker described in ``NOTES_TO_DAEMON.md``'s
atomic-orphan-recovery proposal — implemented inside the
daemon so the user never has to run a separate tool.
"""

import os
import sys
import tempfile
import time
from xml.etree import ElementTree as ET

from . import projects as _projects
from . import merge_commit as _merge_commit
from .locks import project_lock, LockTimeout
from .lift_merge import three_way_merge


# Skip files younger than this — could still be Phase 1 in progress.
# 60 s is well beyond any realistic Phase 1 wall-clock (max LIFT
# size in the field is ~10 MB; even a slow Android sandbox writes
# that in single-digit seconds).
_MIN_AGE_S = 60.0


def _canon_entry_xml(elem):
    """Canonical text form of a single ``<entry>`` element, for
    byte-level equivalence comparison. Strips trailing whitespace
    so cosmetic re-serialization differences don't masquerade as
    content changes. Returns bytes."""
    return ET.tostring(elem, encoding='utf-8').strip()


def _is_garbage(orphan_bytes, current_bytes):
    """Return True if ``orphan_bytes`` is provably equivalent to
    ``current_bytes`` (hash-equal, or subset by canonical XML per
    shared guid AND no orphan-only entries). False otherwise — the
    caller should run a merge."""
    if orphan_bytes == current_bytes:
        return True
    try:
        orphan_root = ET.fromstring(orphan_bytes)
        current_root = ET.fromstring(current_bytes)
    except ET.ParseError:
        # Can't classify — let the merge path try, and it'll
        # surface as unmergeable if both sides are bad.
        return False
    orphan_entries = {e.get('guid') or '': e
                      for e in orphan_root.findall('entry')}
    current_entries = {e.get('guid') or '': e
                       for e in current_root.findall('entry')}
    # Orphan has an entry current lacks → not garbage.
    if any(g and g not in current_entries for g in orphan_entries):
        return False
    # Every shared guid byte-identical → garbage.
    for guid, orphan_entry in orphan_entries.items():
        if not guid:
            continue
        if _canon_entry_xml(orphan_entry) != _canon_entry_xml(
                current_entries[guid]):
            return False
    return True


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _stash_unmergeable(project_dir, token, orphan_path):
    """Move an orphan we couldn't auto-recover to a parallel
    ``.azt_atomic_orphans/unmergeable/<token>.lift`` directory so
    the bytes stay on disk for manual inspection but stop
    cluttering ``.azt_atomic_pending/``."""
    dest_dir = os.path.join(project_dir, '.azt_atomic_orphans',
                            'unmergeable')
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f'{token}.lift')
        os.replace(orphan_path, dest)
        print(f'[atomic-recovery] stashed unmergeable orphan '
              f'{token!r} → {dest!r}',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[atomic-recovery] failed to stash unmergeable '
              f'orphan {token!r}: {ex!r}',
              file=sys.stderr, flush=True)


def _atomic_write_bytes(target_path, data):
    """Write ``data`` to ``target_path`` via sibling-tempfile +
    ``os.replace``. Same shape as ``atomic_open_write``'s
    filesystem-path branch, just inlined here so we don't need
    to involve the ContentProvider FD machinery for a daemon-
    internal write."""
    target_dir = os.path.dirname(target_path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.atomic_recovery.',
                               suffix='.tmp', dir=target_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, target_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _commit_recovery(repo, langcode, lift_rel, conflicts,
                     orphan_mtime):
    """Stage the recovered LIFT file + commit. Author + committer
    are both the suite bot (same identity used for cross-peer
    merge commits); this is a daemon-internal recovery, not a
    user gesture, so the bot author is correct."""
    from dulwich import porcelain
    porcelain.add(repo, paths=[lift_rel.encode('utf-8')])
    bot = _merge_commit.bot_identity().encode('utf-8')
    ts = time.strftime('%Y-%m-%dT%H:%M:%S',
                       time.localtime(orphan_mtime))
    body = [f'Recovered orphan from {ts}']
    if conflicts:
        body.append('')
        body.append(f'Conflicts ({len(conflicts)}; azt-lift-conflict '
                    f'markers added):')
        for c in conflicts[:20]:
            body.append(f'  {c.path}: {c.guid} {c.kind}')
        if len(conflicts) > 20:
            body.append(f'  … {len(conflicts) - 20} more')
    message = '\n'.join(body).encode('utf-8')
    porcelain.commit(repo, message=message, author=bot,
                     committer=bot)
    print(f'[atomic-recovery] committed recovery for '
          f'{langcode!r}: conflicts={len(conflicts)}',
          file=sys.stderr, flush=True)


def _bump_recovered_today(langcode):
    """Increment ``projects.json :: <langcode>.recovered_today``.
    Diagnostic counter surfaced on ``ProjectStatus``; resets
    naturally on each scan-day boundary via ``last_recovery_day``."""
    try:
        data = _projects._load_raw()
    except Exception:
        return
    entry = dict(data.get(langcode, {}))
    today = time.strftime('%Y-%m-%d')
    if entry.get('last_recovery_day') != today:
        entry['recovered_today'] = 0
        entry['last_recovery_day'] = today
    entry['recovered_today'] = int(entry.get('recovered_today', 0)) + 1
    data[langcode] = entry
    try:
        _projects._save_raw(data)
    except Exception:
        pass


def recover_project_orphans(project_dir, lift_path, langcode=''):
    """Walk ``<project_dir>/.azt_atomic_pending/`` and dispose of
    each orphan per the contract at the top of this module.
    Returns a summary dict for the caller to log; raises only on
    catastrophic conditions the caller can't reasonably handle.

    Safe to call from any context (the scheduler tick is the
    canonical caller, but the bootstrap-startup path also calls
    once on daemon spawn). Acquires ``project_lock`` for the
    duration of any merge+commit so it can't overlap a sync's
    merge-output write or another peer's atomic_finalize.
    Returns early with ``status='busy'`` if the lock is held.
    """
    summary = {
        'scanned': 0, 'deleted_garbage': 0, 'recovered': 0,
        'unmergeable': 0, 'skipped_young': 0,
        'skipped_low_memory': 0,
        'errors': 0,
    }
    if not project_dir or not os.path.isdir(project_dir):
        return summary
    pending_dir = os.path.join(project_dir, '.azt_atomic_pending')
    if not os.path.isdir(pending_dir):
        return summary
    try:
        names = os.listdir(pending_dir)
    except OSError:
        return summary
    if not names:
        return summary
    now = time.time()

    # Directory-scan trace. Field log baf 2026-05-22 showed
    # n_changes stuck at 1424 with 8 atomic-pending tokens on
    # disk while no ``[atomic-recovery]`` lines appeared for
    # those specific tokens — couldn't tell whether the sweep
    # had even considered them. This line fires once per
    # ``recover_project_orphans`` call when the directory is
    # non-empty, dumping the names + ages so the tester reading
    # the daemon log can answer "did the sweep see X?" without
    # rebuilding.
    try:
        ages = []
        for n in names:
            p = os.path.join(pending_dir, n)
            try:
                ages.append((n, int(now - os.stat(p).st_mtime)))
            except OSError:
                ages.append((n, -1))
        head = ages[:6]
        tail_count = max(0, len(ages) - len(head))
        head_str = ', '.join(f'{n}@{a}s' for (n, a) in head)
        if tail_count:
            head_str += f', … +{tail_count} more'
        print(f'[atomic-recovery] scanning {pending_dir!r}: '
              f'{len(names)} entr{"y" if len(names) == 1 else "ies"} '
              f'(min_age={_MIN_AGE_S:.0f}s): [{head_str}]',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[atomic-recovery] scan trace failed: {ex!r}',
              file=sys.stderr, flush=True)

    # Read current LIFT bytes once; we'll diff every orphan against
    # this snapshot. Missing lift_path is rare but possible (project
    # registered without a LIFT yet) — treat orphan as straight
    # take-it, which falls out of the merge with ours=b''.
    try:
        with open(lift_path, 'rb') as f:
            current_bytes = f.read()
    except FileNotFoundError:
        current_bytes = b''
    except Exception as ex:
        print(f'[atomic-recovery] {project_dir!r} read current LIFT '
              f'failed: {ex!r}', file=sys.stderr, flush=True)
        summary['errors'] += 1
        return summary

    # Try once; if the lock is busy, defer to the next tick.
    try:
        with project_lock(project_dir):
            _recover_under_lock(
                project_dir, lift_path, langcode, pending_dir,
                names, now, current_bytes, summary)
    except LockTimeout:
        summary['status'] = 'busy'
        return summary
    return summary


def _recover_under_lock(project_dir, lift_path, langcode,
                        pending_dir, names, now, current_bytes,
                        summary):
    """Inner loop: caller holds ``project_lock``. Iterates the
    pending dir's entries and disposes of each. Mutates
    ``summary`` in place."""
    from .repo import _get_repo, _check_memory_for_merge
    for name in names:
        orphan_path = os.path.join(pending_dir, name)
        if not os.path.isfile(orphan_path):
            continue
        try:
            st = os.stat(orphan_path)
        except OSError:
            continue
        age = now - st.st_mtime
        if age < _MIN_AGE_S:
            summary['skipped_young'] += 1
            continue
        summary['scanned'] += 1
        try:
            with open(orphan_path, 'rb') as f:
                orphan_bytes = f.read()
        except Exception as ex:
            print(f'[atomic-recovery] read orphan {name!r} failed: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            summary['errors'] += 1
            continue

        if _is_garbage(orphan_bytes, current_bytes):
            _safe_unlink(orphan_path)
            summary['deleted_garbage'] += 1
            print(f'[atomic-recovery] {name!r}: garbage '
                  f'(equivalent to current); deleted',
                  file=sys.stderr, flush=True)
            continue

        # Empty-current shortcut. If the LIFT file doesn't exist
        # yet (e.g. peer wrote to the pending dir before
        # init_repo had a chance to lay down the project's LIFT),
        # skip the merge path — three_way_merge with ours=b''
        # would trip its parse-error guard and stash the orphan
        # as unmergeable, when actually we just want to adopt
        # the orphan as the project's LIFT. Take it wholesale,
        # commit if a repo exists, delete the orphan.
        if not current_bytes:
            try:
                _atomic_write_bytes(lift_path, orphan_bytes)
            except Exception as ex:
                print(f'[atomic-recovery] adopt empty-current '
                      f'orphan {name!r} failed: {ex!r}',
                      file=sys.stderr, flush=True)
                summary['errors'] += 1
                continue
            repo = _get_repo(project_dir)
            if repo is not None:
                lift_rel = os.path.relpath(lift_path, project_dir)
                try:
                    _commit_recovery(repo, langcode, lift_rel,
                                     [], st.st_mtime)
                except Exception as ex:
                    print(f'[atomic-recovery] commit (empty-'
                          f'current) failed for orphan {name!r}: '
                          f'{ex!r}', file=sys.stderr, flush=True)
            _safe_unlink(orphan_path)
            summary['recovered'] += 1
            if langcode:
                _bump_recovered_today(langcode)
            current_bytes = orphan_bytes
            print(f'[atomic-recovery] {name!r}: adopted '
                  f'(no prior LIFT)',
                  file=sys.stderr, flush=True)
            continue

        # Merge path. ``base=b''`` per three_way_merge's
        # "no shared history" semantics — orphan and current
        # diverged from an unknown point.
        #
        # Memory pre-flight: parsing two LIFT XMLs side-by-side
        # peaks at ~100–150 MB on a 1700-entry project. Daemon
        # startup is exactly when memory may be tight (picker
        # activity also spawning), and a silent OOM-kill here
        # would lose the entire recovery batch with no signal.
        # Refuse the merge cleanly: leave the orphan on disk
        # (it stays valid; next startup retries when memory has
        # recovered) and bail the rest of the batch — if we
        # couldn't fit this merge, we won't fit the next either.
        mem_block = _check_memory_for_merge()
        if mem_block is not None:
            print(f'[atomic-recovery] orphan {name!r}: skipping '
                  f'merge — only '
                  f'{mem_block.params.get("mem_available_mb")} MB '
                  f'free, need '
                  f'{mem_block.params.get("min_required_mb")} MB. '
                  f'Orphan stays on disk; next startup with more '
                  f'memory will recover it.',
                  file=sys.stderr, flush=True)
            summary['skipped_low_memory'] += 1
            return
        try:
            mr = three_way_merge(
                base_bytes=b'', ours_bytes=current_bytes,
                theirs_bytes=orphan_bytes, path=lift_path)
        except Exception as ex:
            print(f'[atomic-recovery] merge raised for orphan '
                  f'{name!r}: {ex!r}', file=sys.stderr, flush=True)
            _stash_unmergeable(project_dir, name, orphan_path)
            summary['unmergeable'] += 1
            continue

        # ``MergeResult.conflicts`` carries the parse-error /
        # truncation / catastrophic-loss guard kinds too. If any
        # of those fire, the merger keeps one side intact and
        # signals via a conflict entry — that's still a
        # recoverable outcome (we land the kept side; the orphan
        # is preserved as unmergeable for inspection).
        guard_kinds = {'parse-error', 'truncation-suspected',
                       'catastrophic-merge-output'}
        if any(c.kind in guard_kinds for c in mr.conflicts):
            print(f'[atomic-recovery] guard fired for orphan '
                  f'{name!r}: '
                  f'{[c.kind for c in mr.conflicts]!r}; '
                  f'stashing as unmergeable',
                  file=sys.stderr, flush=True)
            _stash_unmergeable(project_dir, name, orphan_path)
            summary['unmergeable'] += 1
            continue

        # LIFT delta trace — verifies the 0.45.34 merge fix in
        # the field. After 0.45.34, recovery on a polluted LIFT
        # should produce a *smaller* result with a *lower*
        # ``azt-lift-conflict`` count, because the canon-equal
        # path strips stale false-positive annotations from
        # semantically-identical content. Pre-0.45.34 the trend
        # was the opposite: every cycle added more spurious
        # annotations. The tester reads this line from the
        # daemon log via the Share button; no on-device git
        # tooling needed (working_dir lives in the server APK's
        # private filesDir, not adb-accessible on release
        # builds). ``count(b'azt-lift-conflict')`` is a substring
        # heuristic — each annotation contributes one occurrence
        # in the ``name="azt-lift-conflict"`` attribute, and the
        # token doesn't appear elsewhere in well-formed LIFT.
        old_size = len(current_bytes)
        new_size = len(mr.merged_bytes)
        old_annot = current_bytes.count(b'azt-lift-conflict')
        new_annot = mr.merged_bytes.count(b'azt-lift-conflict')
        print(f'[atomic-recovery] {name!r} merge delta: '
              f'lift_bytes {old_size:,} → {new_size:,} '
              f'({new_size - old_size:+,}), '
              f'conflict_annotations {old_annot} → {new_annot} '
              f'({new_annot - old_annot:+})',
              file=sys.stderr, flush=True)

        try:
            _atomic_write_bytes(lift_path, mr.merged_bytes)
        except Exception as ex:
            print(f'[atomic-recovery] write merged result failed '
                  f'for orphan {name!r}: {ex!r}',
                  file=sys.stderr, flush=True)
            summary['errors'] += 1
            continue

        # Commit (best-effort — if the repo isn't initialized
        # yet, the file is on disk and will be picked up by the
        # next sync's _stage_all).
        repo = _get_repo(project_dir)
        if repo is not None:
            lift_rel = os.path.relpath(lift_path, project_dir)
            try:
                _commit_recovery(repo, langcode, lift_rel,
                                 mr.conflicts, st.st_mtime)
            except Exception as ex:
                print(f'[atomic-recovery] commit failed for '
                      f'orphan {name!r}: {ex!r}',
                      file=sys.stderr, flush=True)
                # File is on disk; the next sync will pick it up.

        _safe_unlink(orphan_path)
        summary['recovered'] += 1
        if langcode:
            _bump_recovered_today(langcode)
        # Refresh current_bytes so subsequent orphans see the
        # post-recovery state (avoids re-merging the same edits
        # if the pending dir held multiple historical scratches).
        current_bytes = mr.merged_bytes
        print(f'[atomic-recovery] {name!r}: recovered '
              f'(conflicts={len(mr.conflicts)})',
              file=sys.stderr, flush=True)
