"""Periodic repository maintenance — keep pushes small (0.55.147).

Why this exists
---------------

Field 2026-07-30, project ``nml``: every commit was a 4-to-6 line edit of
``nml.lift``, and every push cost **16 MB**. 239 queued commits meant 3.8 GB
on the wire to deliver a few MB of text, at ~66 s per commit on a ~2 Mbps
link — over four hours.

Git stores whole blobs, not diffs. A six-line edit to a 16 MB file produces a
new 16 MB object. The saving normally comes from *delta compression*, and
dulwich already knows how to use it: ``PackBasedObjectStore.generate_pack_data``
calls ``generate_unpacked_objects(..., other_haves=remote_has)``, and
``find_reusable_deltas`` (``pack.py:3238``) will reuse a delta whose base is
either in the same pack **or already on the remote** — a thin pack. So even at
one commit per push, blob N should ship as a small delta against blob N-1,
which went up in the previous push.

The catch is in the name: *reusable*. It reuses deltas that already exist in
local **pack files**. Freshly committed objects are **loose**, and loose
objects carry no deltas — so there is nothing to reuse and dulwich falls
through to ``full_unpacked_object`` for every one. That is the entire 16 MB.

Packing the repo locally is therefore not housekeeping; it is what makes the
push small. After ``git repack -adf`` on nml: 22399 objects, **15805 deltas**.

Why not dulwich
---------------

``dulwich.gc.garbage_collect`` / ``DiskObjectStore.repack`` cannot be used:

* ``PackBasedObjectStore.add_objects`` (``object_store.py:1576``) writes
  ``full_unpacked_object(o)`` for every object — **no deltification at all**.
  Running it would *destroy* the deltas we are trying to create.
* ``repack``'s own docstring: "this implementation is fairly naive and
  currently keeps all objects in memory while it repacks." On a multi-GB
  project that is an OOM.
* ``pack_objects_to_data`` defaults ``deltify=False`` with the upstream
  comment "the python implementation is *much* too slow at the moment."

So we shell out to ``git``, and only when a ``git`` binary is actually
present. No git → we log why once and do nothing; nothing breaks, pushes are
merely as large as they were before.

**Android has no git binary**, so this does not run there. That is a real gap,
not an oversight: the fix for Android is thin-delta generation inside the push
path, which is surgery on the code that carries user data and wants its own
release. Recorded here so the gap is visible rather than assumed covered.
"""

import os
import shutil
import subprocess
import sys
import threading
import time

from . import lift_merge


# Repack when loose objects have accumulated enough to matter. TWO
# thresholds because the two failure shapes are different: many small loose
# objects (ordinary commit churn) and few enormous ones (a handful of edits
# to a 16 MB LIFT — the case that motivated this file, where a count-only
# trigger would never fire).
_LOOSE_COUNT_TRIGGER = 40
_LOOSE_BYTES_TRIGGER = 100 * 1024 * 1024

# How often the scheduler asks. Cheap: a directory walk of .git/objects/??/.
_SWEEP_INTERVAL_S = 1800.0

# git repack on a multi-GB repo is minutes of CPU, not seconds.
_REPACK_TIMEOUT_S = 3600

# Bounded, so a project mid-push defers instead of blocking the loop.
_LOCK_TIMEOUT_S = 5.0

_lock = threading.Lock()
_last_sweep_at = 0.0
_no_git_logged = False

# Post-repack loose-object floor, per project (0.55.150).
#
# ``git repack -a -d`` packs objects REACHABLE FROM REFS. Unreachable loose
# objects — the debris of topic-ref churn, temp refs and failed pushes — are
# not packed and not deleted; only a prune would remove them, and pruning
# automatically is a different risk decision. Field 2026-07-30, nml:
# ``loose 2441 → 2431 object(s), 6.1 → 6.1 MB``. Ten objects, no bytes.
#
# Without a floor the raw count stays above the trigger permanently, so the
# sweep repacks every 30 minutes forever, achieves nothing, and prints a
# line saying it is what makes pushes small. Remember where each project
# settled and require real growth ON TOP of that before going again.
_post_repack_floor = {}

# The floor is PERSISTED, not just in-memory (0.55.170). It was a module dict,
# so every daemon restart reset it to zero and the same unreachable residue
# tripped the trigger again: field 2026-07-30, `2458 → 2433 object(s), 6.3 →
# 6.1 MB` in 11 s, on a fresh start, having done exactly this on the previous
# start. A dozen restarts in a day meant a dozen 11-second no-ops.
#
# Stored beside the repo it describes rather than in a central state file:
# it is a fact about that git directory, it travels with it, and a lost or
# corrupt marker simply means one extra repack.
_FLOOR_FILE = 'azt-repack-floor'


def _floor_path(working_dir):
    return os.path.join(working_dir, '.git', _FLOOR_FILE)


def _read_floor(working_dir):
    """``(count, bytes)`` the last repack settled at, or ``(0, 0)``."""
    key = os.path.abspath(working_dir)
    hit = _post_repack_floor.get(key)
    if hit is not None:
        return hit
    try:
        with open(_floor_path(working_dir)) as fh:
            parts = fh.read().split()
        value = (int(parts[0]), int(parts[1]))
    except Exception:
        value = (0, 0)
    _post_repack_floor[key] = value
    return value


def _write_floor(working_dir, count, total):
    _post_repack_floor[os.path.abspath(working_dir)] = (count, total)
    try:
        with open(_floor_path(working_dir), 'w') as fh:
            fh.write(f'{int(count)} {int(total)}\n')
    except OSError as ex:
        print(f'[maintenance] could not persist the repack floor '
              f'({ex!r}) — the next daemon start will repack once needlessly',
              file=sys.stderr, flush=True)


def _loose_object_stats(git_dir):
    """``(count, bytes)`` of loose objects under ``<git_dir>/objects``.

    Only the 2-hex-digit fanout directories; ``pack/`` and ``info/`` are
    skipped. Returns ``(0, 0)`` on any error — a maintenance trigger must
    never be the thing that breaks a sync.
    """
    count = 0
    total = 0
    try:
        objects = os.path.join(git_dir, 'objects')
        for name in os.listdir(objects):
            if len(name) != 2:
                continue
            try:
                int(name, 16)
            except ValueError:
                continue
            sub = os.path.join(objects, name)
            try:
                entries = os.listdir(sub)
            except OSError:
                continue
            for entry in entries:
                try:
                    total += os.path.getsize(os.path.join(sub, entry))
                    count += 1
                except OSError:
                    continue
    except Exception:
        return 0, 0
    return count, total


# Where Git for Windows puts itself when it is NOT added to PATH. The
# installer offers "Use Git from Git Bash only", which leaves git.exe
# installed and perfectly usable but invisible to ``shutil.which`` from a
# normal Windows Python — the daemon is not running inside Git Bash. Field
# machines are set up by whoever set them up, so probe the standard
# locations before concluding there is no git (0.55.148).
#
# Built from the environment rather than hardcoded ``C:\Program Files``:
# Windows is installed on other drives, ``Program Files`` is localized in
# some builds, and ``ProgramW6432`` is the only reliable way to reach the
# 64-bit tree from a 32-bit process. Same coverage, fewer literals.
_WINDOWS_GIT_ROOT_VARS = ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432')
_WINDOWS_GIT_SUBPATHS = (('Git', 'cmd', 'git.exe'), ('Git', 'bin', 'git.exe'))


def _git_binary():
    """Path to ``git``, or None. Logs the absence exactly once."""
    global _no_git_logged
    # Honors PATHEXT on Windows, so 'git' resolves to git.exe.
    found = shutil.which('git')
    if found:
        return found
    if os.name == 'nt':
        roots = [os.environ.get(var) for var in _WINDOWS_GIT_ROOT_VARS]
        # Per-user install (no admin rights) lands under LOCALAPPDATA.
        local = os.environ.get('LOCALAPPDATA')
        if local:
            roots.append(os.path.join(local, 'Programs'))
        for root in roots:
            if not root:
                continue
            for sub in _WINDOWS_GIT_SUBPATHS:
                candidate = os.path.join(root, *sub)
                try:
                    if os.path.isfile(candidate):
                        return candidate
                except Exception:
                    continue
    if not _no_git_logged:
        _no_git_logged = True
        print('[maintenance] no git binary on PATH — repository repacking '
              'is disabled, so pushes will carry whole blobs rather than '
              'deltas (expected on Android)',
              file=sys.stderr, flush=True)
    return None


def repack_project(working_dir, langcode=''):
    """Repack one project. Returns True if git ran and succeeded.

    ``-a -d`` consolidates into a single pack and drops what became
    redundant. Deliberately **not** ``-f``: that recomputes every delta from
    scratch, which is right for a one-off rescue and far too expensive as
    routine maintenance. Without it, existing deltas are reused and only the
    newly-packed loose objects need delta search.
    """
    git = _git_binary()
    if git is None:
        return False
    label = langcode or os.path.basename(os.path.abspath(working_dir))
    count, total = _loose_object_stats(os.path.join(working_dir, '.git'))
    started = time.monotonic()
    # ANNOUNCE AT THE POINT OF ACTION (invariant #15): everything that
    # decides whether this happens — git present, lock held, thresholds — is
    # already settled above, and the subprocess call is the next statement.
    print(f'[maintenance] repacking {label!r}: {count} loose object(s), '
          f'{total / (1024 * 1024):.1f} MB — this is what lets pushes ship '
          f'deltas instead of whole files', file=sys.stderr, flush=True)
    # No console flash on Windows. Without this a background daemon pops a
    # black window every time it repacks, which reads as a malfunction to
    # the person whose screen it appears on.
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(
            subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    try:
        proc = subprocess.run(
            [git, '-C', working_dir, 'repack', '-a', '-d',
             '--window=50', '--depth=50', '--window-memory=1g'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=_REPACK_TIMEOUT_S, **kwargs)
    except subprocess.TimeoutExpired:
        print(f'[maintenance] repack {label!r}: timed out after '
              f'{_REPACK_TIMEOUT_S}s — left as-is, will retry next sweep',
              file=sys.stderr, flush=True)
        return False
    except Exception as ex:
        print(f'[maintenance] repack {label!r}: could not run git '
              f'({type(ex).__name__}: {ex})', file=sys.stderr, flush=True)
        return False
    took = time.monotonic() - started
    if proc.returncode != 0:
        tail = (proc.stdout or b'').decode('utf-8', 'replace')[-400:]
        print(f'[maintenance] repack {label!r}: git exited '
              f'{proc.returncode} after {took:.0f}s — {tail}',
              file=sys.stderr, flush=True)
        return False
    after_count, after_total = _loose_object_stats(
        os.path.join(working_dir, '.git'))
    # Remember where it settled, so the next trigger needs real growth on
    # top of this rather than re-firing on residue we cannot pack.
    _write_floor(working_dir, after_count, after_total)
    # SAY WHAT CHANGED, NOT JUST THAT IT RAN. A repack that packed nothing
    # and one that packed 700 MB are the same line otherwise, and the whole
    # point of the operation is the size of the difference.
    print(f'[maintenance] repack {label!r}: done in {took:.0f}s — loose '
          f'{count} → {after_count} object(s), '
          f'{total / (1024 * 1024):.1f} → '
          f'{after_total / (1024 * 1024):.1f} MB',
          file=sys.stderr, flush=True)
    if after_count and after_count > count * 0.9:
        # Name it rather than leaving the numbers to be read closely. These
        # are unreachable objects; repacking cannot touch them, so the line
        # above must not be mistaken for "nothing needed doing".
        print(f'[maintenance] repack {label!r}: {after_count} object(s) '
              f'stayed loose — unreachable from any ref, so repack cannot '
              f'pack them (only a prune would remove them). Not a failure, '
              f'and not retried until new loose objects accumulate',
              file=sys.stderr, flush=True)
    return True


def _needs_repack(working_dir):
    """``(needed, count, bytes)`` for one project.

    Thresholds are measured ABOVE the floor this project settled at after its
    last repack, not against zero — otherwise unreachable residue that no
    repack can remove keeps the raw count permanently over the trigger, and
    the sweep runs forever for nothing (0.55.150).
    """
    git_dir = os.path.join(working_dir, '.git')
    if not os.path.isdir(git_dir):
        return False, 0, 0
    count, total = _loose_object_stats(git_dir)
    floor_count, floor_total = _read_floor(working_dir)
    # A shrinking count means something else pruned or the repo was replaced;
    # drop the floor so we don't sit above a stale high-water mark.
    if count < floor_count or total < floor_total:
        floor_count, floor_total = 0, 0
        _write_floor(working_dir, 0, 0)
    return ((count - floor_count) >= _LOOSE_COUNT_TRIGGER
            or (total - floor_total) >= _LOOSE_BYTES_TRIGGER), count, total


def due():
    """True when a sweep is worth spawning.

    Kept separate from ``sweep`` so the watcher loop can check a clock
    instead of starting a thread every tick. ``sweep`` re-checks anyway —
    this is the cheap guard, not the authority."""
    return (time.monotonic() - _last_sweep_at) >= _SWEEP_INTERVAL_S


def sweep(force=False):
    """Repack every project that has accumulated enough loose objects.

    Called from the scheduler's watcher loop; self-throttling, so the caller
    can invoke it every tick. ``force`` skips the interval check (a user
    gesture), never the per-project thresholds — repacking a repo that
    doesn't need it is pure cost.
    """
    global _last_sweep_at
    now = time.monotonic()
    if not force and (now - _last_sweep_at) < _SWEEP_INTERVAL_S:
        return
    if not _lock.acquire(blocking=False):
        return          # a sweep is already running; never stack them
    try:
        _last_sweep_at = now
        if _git_binary() is None:
            return
        try:
            from . import projects as _projects
            # ``list_all()`` → [Project], NOT a dict. 0.55.147 shipped with
            # ``projects.load()``, which does not exist; the whole sweep was
            # a logged AttributeError on every tick.
            entries = _projects.list_all()
        except Exception as ex:
            print(f'[maintenance] sweep: could not read the project '
                  f'registry ({ex!r})', file=sys.stderr, flush=True)
            return
        considered = 0
        repacked = 0
        skipped_busy = []
        for project in (entries or []):
            langcode = getattr(project, 'langcode', '') or ''
            working_dir = getattr(project, 'working_dir', '') or ''
            if not working_dir or not os.path.isdir(working_dir):
                continue
            considered += 1
            needed, count, total = _needs_repack(working_dir)
            if not needed:
                continue
            # NO project_lock (0.55.171).
            #
            # Taking it was over-caution and it starved the user: field
            # 2026-07-30, `repack 'en': done in 49s` while every save came
            # back ``BUSY`` and azt fell back to writing the file with no
            # commit. A maintenance task must never be able to do that.
            #
            # Safe without it. ``git repack`` takes git's own locks and is
            # designed to run against a live repository; deleting a pack on
            # POSIX leaves open descriptors valid, and dulwich already
            # rescans when a pack vanishes between snapshot and open (its
            # own comment cites concurrent repack as the reason). Worst case
            # is a reader retrying, not a corrupted tree.
            try:
                if repack_project(working_dir, langcode):
                    repacked += 1
            except Exception as ex:
                skipped_busy.append(f'{langcode}({count} loose, '
                                    f'{total / (1024 * 1024):.0f} MB: '
                                    f'{type(ex).__name__})')
                continue
        # ALWAYS EMIT A SUMMARY, including the nothing-to-do case — a silent
        # function is indistinguishable from one that never ran.
        _busy = (f'; deferred (busy): {", ".join(skipped_busy)}'
                 if skipped_busy else '')
        print(f'[maintenance] sweep: {considered} project(s) checked, '
              f'{repacked} repacked{_busy}', file=sys.stderr, flush=True)
        if repacked:
            lift_merge.trace(
                f'[sync-trace] maintenance: repacked {repacked} project(s); '
                f'subsequent pushes can reuse deltas instead of sending '
                f'whole blobs')
    finally:
        _lock.release()
