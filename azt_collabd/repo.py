"""
Dulwich operations: init, clone, pull, push, commit, sync, and auto-commit
of audio + LIFT changes. All network ops call net._ensure_ssl() first.

Every public op returns a ``Result`` (status codes + params) — no i18n
inside the backend. Exception paths emit failure codes inside the Result
rather than raising; that matches the existing log-append style.
"""

import contextlib
import io
import json
import os
import re
import socket
import sys
import threading
import time

from . import config as _config
from . import status as S
from .status import Result, Status
from .locks import project_lock, LockTimeout
from .net import _ensure_ssl, _has_internet
from .auth import (add_collaborator, diagnose_403, diagnose_no_access,
                   try_accept_repo_invitation)
from . import lift_merge
from . import merge_commit
from . import settings as _settings


def _busy_result(working_dir):
    return Result().add(S.BUSY, working_dir=os.path.abspath(working_dir))


# Default per-call wall-clock budgets for dulwich network ops. Without
# these a stalled SSL pack-upload can hold ``project_lock`` for the
# full TCP keepalive window — observed in the field as a 25-minute
# hang on a single push attempt (19:11→19:36 baf session 2026-05-19,
# ending in SSLEOFError) that blocked every other client RPC with
# ``BUSY``. ``socket.setdefaulttimeout`` applies to sockets opened
# during the call; urllib3 starts fresh connections on pool exhaustion
# (the "Starting new HTTPS connection (N)" trace lines confirm this)
# so the timeout reliably bounds the network I/O. DoH calls in
# ``net.py`` pass explicit ``timeout=`` to ``urlopen`` which override
# this, so the DoH path stays at its 2.5 s budget. Numbers are
# generous enough that legitimate slow uploads of an adaptive-batched
# chunk complete, tight enough that a hung TCP connection can't lock
# the project indefinitely.
_FETCH_TIMEOUT_S = 60.0
_PUSH_TIMEOUT_S = 180.0


# Per-project memory of the chunk_n that *failed* on the previous
# push attempt. Used so the scheduler's drain loop converges on a
# working chunk size across calls instead of restarting at full tip
# every retry. Persisted only in-process (resets on daemon restart;
# the bundle survives because the failing chunk was demonstrably too
# big and trying again at the same size wastes another budget).
# Cleared on full-tip push success.
#
# Field log baf 2026-05-21 09:38–11:05: device had 419 unpushed
# commits, each drain cycle restarted at chunk_n=419, hit 408
# ``unexpected http resp`` after ~12 minutes, ``push budget exceeded
# (300s) — giving up``, requeued, repeated for hours. Halving inside
# a single call existed (302 → 151 → … → 1 in the 0.43.18 era) but
# its progress was discarded on the next ``_push_step_locked``
# invocation. The fix is to remember the failed chunk_n across calls
# so the next attempt starts smaller.
_LAST_FAILED_CHUNK_N = {}


def _hint_chunk_n(project_dir):
    """Return the remembered chunk_n hint for *project_dir*, or
    ``None`` if no prior failure recorded. The hint is the chunk_n
    that was demonstrably too large on the previous attempt — the
    caller should use it as an upper bound on the new attempt's
    initial chunk size."""
    return _LAST_FAILED_CHUNK_N.get(project_dir)


def _remember_failed_chunk_n(project_dir, n):
    """Persist a failed chunk_n so the next drain cycle for this
    project starts smaller. Stored as ``max(1, n // 2)`` so the
    next cycle attempts half the failed size — same shrinkage the
    in-call halving would do, just preserved across calls."""
    try:
        _LAST_FAILED_CHUNK_N[project_dir] = max(1, int(n) // 2)
    except Exception:
        pass


def _clear_failed_chunk_n(project_dir):
    """Drop the remembered chunk_n for *project_dir*. Called on a
    full successful push — the queue is empty, no constraint to
    remember for next time."""
    _LAST_FAILED_CHUNK_N.pop(project_dir, None)


@contextlib.contextmanager
def _socket_timeout(seconds):
    """Set ``socket.setdefaulttimeout`` for the body. Restores prior
    value on exit. See ``_FETCH_TIMEOUT_S`` / ``_PUSH_TIMEOUT_S`` for
    the rationale."""
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(prev)


def _sha_str(sha):
    """Coerce a sha (bytes / str / None) to a string suitable for
    storage in a diagnostic XML attribute. Empty when the input
    is empty/None."""
    if not sha:
        return ''
    if isinstance(sha, bytes):
        try:
            return sha.decode('ascii')
        except UnicodeDecodeError:
            return sha.hex()
    return str(sha)


def _write_merge_diagnostic(project_dir, *, guard_kind, lift_path,
                             local_sha, remote_sha, base_sha,
                             base_bytes, ours_bytes, theirs_bytes,
                             merged_bytes, conflict_fields):
    """Persist a forensic-data XML dump under
    ``<working_dir>/.azt-collab/diagnostics/<timestamp>-<kind>-<nonce>.xml``
    when a daemon-side merge guard fires. The file gets staged
    into the merge commit by ``_stage_all`` and pushed alongside
    the rest of the merge, so a later analysis can ``git log
    .azt-collab/diagnostics/`` to find when guards fired and
    ``git show <commit>:.azt-collab/diagnostics/<file>.xml`` to
    read the dump.

    Best-effort: failures here are logged to stderr and
    swallowed so a diagnostic-write hiccup doesn't block the
    merge commit itself."""
    try:
        import azt_collabd
        diag_dir = os.path.join(project_dir, *lift_merge.DIAGNOSTICS_SUBDIR)
        os.makedirs(diag_dir, exist_ok=True)
        diag_filename = lift_merge.diagnostic_filename(guard_kind)
        diag_path = os.path.join(diag_dir, diag_filename)
        diag_bytes = lift_merge.build_diagnostic_xml(
            guard_kind=guard_kind,
            lift_path=lift_path,
            local_sha=local_sha,
            remote_sha=remote_sha,
            base_sha=base_sha,
            base_bytes=base_bytes,
            ours_bytes=ours_bytes,
            theirs_bytes=theirs_bytes,
            merged_bytes=merged_bytes,
            conflict_fields=conflict_fields,
            daemon_version=getattr(azt_collabd, '__version__', ''),
            working_dir=project_dir,
        )
        # Atomic write: don't risk staging a half-written
        # diagnostic into the merge commit.
        tmp_path = diag_path + '.tmp'
        with open(tmp_path, 'wb') as f:
            f.write(diag_bytes)
        os.replace(tmp_path, diag_path)
        lift_merge.trace(
            f'[merge-diag] guard={guard_kind} dumped to '
            f'{diag_path!r} ({len(diag_bytes)} bytes)')
    except Exception as ex:
        lift_merge.trace(
            f'[merge-diag] failed to write diagnostic: '
            f'{type(ex).__name__}: {ex}')


# ``\b403\b`` matches HTTP 403 in messages like
# ``"unexpected http resp 403 for https://..."`` (dulwich's
# GitProtocolError format) but NOT inside a 40-char hex SHA — hex is
# all word chars, so the ``\b`` anchors don't fire between adjacent
# digits. Plain ``'403' in str(exc)`` false-positives on any
# ``DivergedBranches`` whose hex SHAs happen to contain ``403`` as a
# substring (probability ~1 in 250 per push: 4 digits × ~10⁻³ for the
# trigraph in a 40-char string), and the false positive routes a
# diverged-branch failure through ``diagnose_403`` which then reports
# a bogus ``REPO_NOT_AUTHORIZED`` — observed in the field, blocked
# real sync for ~25 minutes of a user's session.
_HTTP_403_RE = re.compile(r'\b403\b')


def _is_http_403(exc):
    return bool(_HTTP_403_RE.search(str(exc)))


# 404 / no-access from a git op. GitHub hides any repo the token can't see
# behind a 404 (private-not-shared / not-a-collaborator / app-not-granted /
# wrong name), which dulwich surfaces as ``NotGitRepository`` on the HTTP
# transport. Detect by the exception TYPE first (reliable); the ``\b404\b``
# fallback is word-bounded so it doesn't false-match a hex SHA that happens
# to contain "404" (see feedback_substring_check_on_dulwich_exceptions).
_HTTP_404_RE = re.compile(r'\b404\b')


def _is_repo_not_found(exc):
    if type(exc).__name__ == 'NotGitRepository':
        return True
    return bool(_HTTP_404_RE.search(str(exc)))


def _handle_no_access(token, remote_url, result, cleanup=None):
    """Shared 404/no-access handler for the fetch and push paths (0.52.24).

    Opportunistically auto-accepts a matching PENDING GitHub invitation —
    the 404 IS the trigger, no in-app invite flow needed. If one is found
    and accepted, emits ``INVITE_ACCEPTED`` (transient → caller retries).
    Otherwise emits the honest ``diagnose_no_access`` verdict
    (``REPO_NO_ACCESS`` / ``REPO_NOT_AUTHORIZED`` / ``APP_SUSPENDED``) and
    returns — the caller short-circuits instead of churning the retry loop
    on an error that won't change. ``cleanup`` runs first (push path drops
    its temp ref)."""
    if cleanup is not None:
        cleanup()
    if try_accept_repo_invitation(token, remote_url):
        lift_merge.trace(
            '[sync-trace] 404 → accepted pending invite, will retry')
        result.add(S.INVITE_ACCEPTED)
        return result
    lift_merge.trace('[sync-trace] 404 / NotGitRepository → no access')
    result.statuses.append(diagnose_no_access(token, remote_url))
    return result


# ---------------------------------------------------------------------------
# Merge helpers (LIFT-aware three-way)
# ---------------------------------------------------------------------------

def _apply_tree_to_workdir(repo, project_dir, old_sha, new_sha):
    """Update the working tree + index from ``old_sha`` to ``new_sha``.

    Used by the fast-forward branch of ``_sync_repo_locked``: just
    moving the branch ref forward updates git's view of HEAD but
    leaves the on-disk files at the old version, so peers reading
    the LIFT file (via ``LiftHandle``) get stale bytes and the
    "phone B doesn't see phone A's changes" symptom appears even
    when the daemon's logs show ``S.PULLED`` and ``S.PUSHED``.

    Diff-driven (write only what changed, delete only what's gone)
    rather than nuking + re-extracting the whole tree, so we don't
    touch unrelated untracked files in the working dir (audio
    recordings the user just made and hasn't committed yet, etc.).

    The index is reset to ``new_sha``'s tree at the end so the next
    ``porcelain.status`` call doesn't see the gap between old-index
    / new-HEAD as a giant diff. Best-effort: if the dulwich version
    here doesn't expose ``repo.reset_index`` we rebuild the index
    via ``_stage_all`` instead. ``_stage_all`` is heavier but
    correct."""
    if not new_sha:
        return
    if old_sha:
        try:
            old_commit = repo[old_sha]
            old_blobs = _walk_tree(repo, old_commit.tree)
        except KeyError:
            old_blobs = {}
    else:
        old_blobs = {}
    try:
        new_commit = repo[new_sha]
    except KeyError:
        return
    new_blobs = _walk_tree(repo, new_commit.tree)

    # Write changed / added files. ``_walk_tree`` returns SHA-only;
    # most paths short-circuit on SHA equality with no byte read.
    for path, blob_sha in new_blobs.items():
        if old_blobs.get(path) == blob_sha:
            continue
        content = _blob_bytes(repo, blob_sha)
        if content is None:
            continue
        full = os.path.join(project_dir, path)
        parent = os.path.dirname(full) or project_dir
        os.makedirs(parent, exist_ok=True)
        with open(full, 'wb') as f:
            f.write(content)

    # Remove files in old tree but not new tree.
    for path in old_blobs:
        if path in new_blobs:
            continue
        full = os.path.join(project_dir, path)
        try:
            os.remove(full)
        except OSError:
            pass

    # Reset the index to the new tree so subsequent status / commit
    # calls don't think the user has a giant diff to commit.
    # ``repo.reset_index`` existed through dulwich 0.2x; dulwich
    # ≥1.2 removed it — ``dulwich.index.build_index_from_tree`` is
    # the long-stable replacement, and it also CHECKS OUT the full
    # tree, which repairs working-tree paths this function's
    # old→new diff writer skipped when the tree wasn't actually at
    # ``old_sha`` (e.g. a queued post-receive reset never ran).
    #
    # NEVER fall back to ``_stage_all`` here: the working tree is
    # only guaranteed to match ``new_sha`` for the paths just
    # written; staging everything else captures whatever stale
    # bytes are lying around, and the NEXT commit then pushes
    # superseded content over the converged tip. Field repro
    # 2026-07-21 (desktop, 38b7326→3ce45180 fast-forward on
    # dulwich 1.2.11): the old fallback staged 79 stale files —
    # one commit away from reverting the day's convergence. A
    # loud trace + honest giant status diff beats that.
    try:
        repo.reset_index(new_commit.tree)
        return
    except AttributeError:
        pass  # dulwich ≥1.2 — use the index API below.
    except Exception as ex:
        lift_merge.trace(
            f'[sync-trace] _apply_tree_to_workdir reset_index '
            f'failed: {ex!r}; trying build_index_from_tree')
    try:
        from dulwich.index import build_index_from_tree
        build_index_from_tree(
            repo.path, repo.index_path(),
            repo.object_store, new_commit.tree)
    except Exception as ex:
        lift_merge.trace(
            f'[sync-trace] _apply_tree_to_workdir '
            f'build_index_from_tree failed: {ex!r}; index left '
            f'stale — status will show a large diff until the '
            f'next successful reset (NOT staging as fallback; '
            f'see comment)')


def _walk_tree(repo, tree_sha, prefix=b''):
    """Return dict[path-as-str → blob sha (bytes)] for every file under
    *tree_sha*. SHA-only — callers load blob bytes lazily via
    ``_blob_bytes`` only for paths that actually need content.

    Returning bytes eagerly (pre-0.44.4) loaded every audio file in
    every snapshot into Python dicts; with ``_merge_diverged`` calling
    this three times for base/head/remote on a 1700-entry baf project,
    peak allocation was ~1 GB and Android's ``:provider`` service got
    OOM-killed before the first ``[merge-trace]`` line. Comparing by
    SHA (identical content → identical sha is git's contract) lets
    most paths short-circuit with no byte allocation at all."""
    out = {}
    if not tree_sha:
        return out
    try:
        tree = repo[tree_sha]
    except KeyError:
        return out
    for name, mode, sha in tree.items():
        full = prefix + b'/' + name if prefix else name
        # Mode 0o040000 = subtree
        if mode & 0o040000:
            out.update(_walk_tree(repo, sha, full))
        else:
            out[full.decode('utf-8', errors='replace')] = sha
    return out


def _blob_bytes(repo, sha):
    """Return the bytes for blob *sha*, or None if missing. Companion
    to ``_walk_tree``'s SHA-only return — call this only for paths
    you actually need to read content for."""
    if sha is None:
        return None
    try:
        return repo[sha].data
    except KeyError:
        return None


def _mem_available_mb():
    """Return ``MemAvailable`` from /proc/meminfo in MB, or None when
    the file isn't readable (non-Linux desktop, sandbox). ``MemAvailable``
    is the kernel's estimate of memory that can be allocated without
    swapping — what we need for "can this LIFT merge finish without
    OOM-kill". Cheaper and more accurate than ``MemFree``."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    # Format: "MemAvailable:   123456 kB"
                    kb = int(line.split()[1])
                    return kb // 1024
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        pass
    return None


def _check_memory_for_merge():
    """Pre-flight memory check for ``_merge_diverged``. Returns a
    ``Status('INSUFFICIENT_MEMORY_FOR_MERGE', ...)`` if free memory
    is below ``sync.min_free_mem_mb_for_merge``, else None. Callers
    add the returned Status to their Result and skip the merge —
    the next drain cycle re-reads memory and proceeds when it
    recovers. On platforms where ``/proc/meminfo`` isn't readable
    we treat the check as passing (returns None) — desktop / sandbox
    won't OOM-kill the way Android's ``:provider`` does."""
    min_mb = _settings.min_free_mem_mb_for_merge()
    if min_mb <= 0:
        return None
    available = _mem_available_mb()
    if available is None:
        return None
    if available >= min_mb:
        return None
    return Status(
        S.INSUFFICIENT_MEMORY_FOR_MERGE,
        mem_available_mb=available,
        min_required_mb=min_mb,
    )


def _find_merge_base(repo, a_sha, b_sha):
    """Return one merge-base sha for commits *a_sha* and *b_sha*, or None."""
    try:
        from dulwich.graph import find_merge_base
    except ImportError:
        find_merge_base = None
    if find_merge_base is not None:
        try:
            bases = find_merge_base(repo, [a_sha, b_sha])
            return bases[0] if bases else None
        except Exception:
            pass
    # Fallback: BFS over a's ancestors, then walk b's ancestors looking for one in the set.
    ancestors_a = set()
    queue = [a_sha]
    while queue:
        sha = queue.pop()
        if sha in ancestors_a:
            continue
        ancestors_a.add(sha)
        try:
            commit = repo[sha]
        except KeyError:
            continue
        queue.extend(commit.parents)
    queue = [b_sha]
    seen = set()
    while queue:
        sha = queue.pop()
        if sha in seen:
            continue
        seen.add(sha)
        if sha in ancestors_a:
            return sha
        try:
            commit = repo[sha]
        except KeyError:
            continue
        queue.extend(commit.parents)
    return None


def _commits_between(repo, base_sha, tip_sha, limit=20):
    """List commits reachable from *tip_sha* but not *base_sha*. Returns
    list of dicts {sha, message, author} (newest first)."""
    out = []
    queue = [tip_sha]
    seen = set()
    while queue and len(out) < limit:
        sha = queue.pop(0)
        if sha == base_sha or sha in seen:
            continue
        seen.add(sha)
        try:
            commit = repo[sha]
        except KeyError:
            continue
        out.append({
            'sha': sha,
            'message': commit.message,
            'author': commit.author,
        })
        for p in commit.parents:
            if p != base_sha and p not in seen:
                queue.append(p)
    return out


def _count_commits_ahead(repo, exclude_sha, head_sha, cap=100000):
    """Count commits reachable from *head_sha* but NOT *exclude_sha*,
    via a walker — no per-commit dict allocation (unlike
    ``_commits_between``), so an exact count of thousands is cheap. Used
    by the peer-sync board's 'N to send'. *exclude_sha* may be diverged
    from head (counts the outbound delta). Capped at *cap* (returns cap
    on hitting the ceiling — realistically never for field data)."""
    n = 0
    try:
        walker = repo.get_walker(include=[head_sha], exclude=[exclude_sha])
        for _ in walker:
            n += 1
            if n >= cap:
                break
    except Exception:
        return 0
    return n


def _is_ancestor(repo, ancestor_sha, descendant_sha):
    """Return True if *ancestor_sha* is reachable from *descendant_sha*."""
    if ancestor_sha == descendant_sha:
        return True
    queue = [descendant_sha]
    seen = set()
    while queue:
        sha = queue.pop()
        if sha in seen:
            continue
        seen.add(sha)
        try:
            commit = repo[sha]
        except KeyError:
            continue
        if ancestor_sha in commit.parents:
            return True
        queue.extend(p for p in commit.parents if p not in seen)
    return False


def _all_commits_descend_from(repo, ancestor_sha, descendant_sha):
    """Return True iff every commit reachable from *descendant_sha*
    but not from *ancestor_sha* has *ancestor_sha* as one of its
    ancestors. False if any commit in the delta is on a parallel
    branch that diverged before *ancestor_sha* (typical post-merge
    state: the local-side parent chain of the merge commit doesn't
    descend from the current remote).

    Routing decision for push:
    - True: direct-push + chunk-halving against *ancestor_sha*
      (the remote ref) is structurally viable — every intermediate
      the chunk picker can choose has *ancestor_sha* as an
      ancestor, so the server accepts the FF.
    - False: at least one intermediate is on a diverged branch
      → direct push of any intermediate gets ``DivergedBranches``
      at the server → route through the topic-branch path
      (Phase A → B → C) so the diverged commits land on an
      ours-only ref first.

    Algorithm: O(N) one-pass over the delta. Walk all commits
    reachable from descendant but not ancestor; check that every
    parent of every such commit is either *ancestor_sha* itself
    (the FF root) or another commit in the delta (still on our
    line). Any parent that's neither means we touched a third
    branch — i.e., we have a merge whose other parent isn't the
    ancestor we're pushing to."""
    if not ancestor_sha or not descendant_sha:
        return False
    if ancestor_sha == descendant_sha:
        return True
    try:
        walker = repo.get_walker(
            include=[descendant_sha], exclude=[ancestor_sha])
    except Exception:
        # Walker errors are rare (missing object, etc.); safest
        # default is "not FF" so we route through topic-branch,
        # which is the more robust path.
        return False
    delta = set()
    commits = []
    for entry in walker:
        delta.add(entry.commit.id)
        commits.append(entry.commit)
    if not commits:
        # Empty delta — descendant equals ancestor or is unreachable.
        # Equal case handled above; unreachable means we have no
        # commits to push, treat as vacuously True (caller's
        # "nothing to push" branch will handle it).
        return True
    for commit in commits:
        for parent_sha in commit.parents:
            if parent_sha == ancestor_sha:
                continue
            if parent_sha in delta:
                continue
            # Parent is neither the FF root nor in our delta — it
            # lives on a third branch we don't reach by walking
            # from descendant_sha. The current commit is on a
            # diverged side of a merge.
            return False
    return True


class UnrelatedHistoriesError(Exception):
    """``_merge_diverged`` refusal: ours and theirs share NO common
    ancestor. A legitimately shared project always has one (clone /
    LAN-clone / publish all propagate history), so no-merge-base
    means two INDEPENDENT projects that happen to share a langcode
    label. Pre-guard, the merge silently ran with an EMPTY base and
    pushed the union to both sides. Auto-merging unrelated projects
    is data pollution in both directions; a union should only ever
    be an explicit user decision (not yet built — refusal is the
    safe floor).

    KNOWN LIMIT (field 2026-07-22, the incident that motivated
    this): projects FORKED from one another (e.g. a `-x-test` fork
    meant to stay separate) DO share an ancestor, so this guard
    does not fire for them — keeping forks apart requires a project
    identity distinct from the langcode (tracked in
    agenda/project_identity_beyond_langcode.md); until that ships,
    the only protection for forks is not sharing them to the same
    peers."""


def _audio_recency_resolver(repo, local_sha, remote_sha, work_dir=None):
    """Return ``fn(filename) -> recency|None``: how recent that audio
    file's content is, for last-wins resolution of divergent single-
    value audio takes. Fed to
    ``lift_merge.three_way_merge(audio_recency=...)``.

    Recency values:
      - a committed file → the most-recent commit time touching its
        path across the supplied lineage tips (a float, comparable);
      - a file NOT in committed history but present on disk under
        *work_dir* → ``float('inf')`` = **NOW**. A file that exists
        only in the working tree was just produced on this device, so
        it is the newest thing there is and must beat any committed
        take. This is the "undefined is NOW" rule: not every merge has
        two committed lineages, and the uncommitted side is NOW
        (Kent 2026-07-22). Only enabled when *work_dir* is passed;
        the convergence merge (``_merge_diverged``) passes ``None`` so
        it stays pure commit-time and deterministic across devices
        (its inputs are committed trees, so NOW never legitimately
        applies there anyway).
      - a filename that resolves nowhere (not committed, not on disk)
        → ``None``: ambiguous, so ``_normalize_entry`` keeps the
        document-first survivor rather than dropping blindly.

    NOW is only ever true on the one device holding the uncommitted
    file; once committed and converged, ``_merge_diverged`` re-derives
    the winner deterministically by commit time — so resolving NOW
    locally is safe and just spares the user a spurious conflict
    annotation that would resolve away on the next computer anyway.

    Lazy + cached, and — critically — **one history walk per tip**,
    not one per file. The first ``resolve`` call walks each tip's
    history newest-first ONCE, recording basename → most-recent commit
    time for every path as it goes, and stops early once every file in
    the tip tree has been dated (files are usually touched recently, so
    this exits well before the root commit). Subsequent calls are dict
    lookups. The pre-0.54.31 version ran a ``get_walker(paths=[path])``
    per filename per tip — O(files × history) — which turned a merge
    with a few hundred divergent audio takes into a ~1-hour crawl
    (field 2026-07-22, nml recovery: 234 takes → 55 min). A
    conflict-free merge still pays nothing (the resolver isn't called
    unless the LIFT merge hits an audio conflict — the cheap-no-op
    rule; merges run many times a day)."""
    tips = [s for s in (local_sha, remote_sha) if s]
    state = {'times': None, 'ondisk': None}

    def _times():
        # basename -> most-recent commit_time across both tips.
        if state['times'] is not None:
            return state['times']
        m = {}
        for tip in tips:
            # Files we still need to date for this tip — everything in
            # its tree. Lets us stop the walk once all are dated instead
            # of walking to the root commit every time.
            want = set()
            try:
                commit = repo[tip]
                for e in repo.object_store.iter_tree_contents(commit.tree):
                    want.add(os.path.basename(
                        e.path.decode('utf-8', 'replace')))
            except Exception:
                want = set()
            seen = set()
            try:
                for entry in repo.get_walker(include=[tip]):
                    ct = entry.commit.commit_time
                    try:
                        raw = entry.changes()
                    except Exception:
                        raw = []
                    # A merge commit's changes() is a list-of-lists
                    # (one per parent); a normal commit's is a flat
                    # list of TreeChange. Flatten defensively.
                    for ch in raw:
                        for c in (ch if isinstance(ch, list) else [ch]):
                            for side in (getattr(c, 'new', None),
                                         getattr(c, 'old', None)):
                                p = getattr(side, 'path', None)
                                if not p:
                                    continue
                                base = os.path.basename(
                                    p.decode('utf-8', 'replace'))
                                # newest-first walk → keep the max time
                                # (also correct when merging both tips).
                                if base not in m or ct > m[base]:
                                    m[base] = ct
                                seen.add(base)
                    if want and want <= seen:
                        break   # every file in this tip's tree is dated
            except Exception:
                pass
        state['times'] = m
        return m

    def _ondisk():
        # Set of basenames present in the working tree (minus .git).
        # Built once, only when a filename misses committed history —
        # i.e. only on a real audio conflict involving an uncommitted
        # file.
        if state['ondisk'] is None:
            names = set()
            if work_dir:
                for root, dirs, files in os.walk(work_dir):
                    if '.git' in dirs:
                        dirs.remove('.git')
                    for fn in files:
                        names.add(fn)
            state['ondisk'] = names
        return state['ondisk']

    def resolve(filename):
        if not filename:
            return None
        t = _times().get(filename)
        if t is not None:
            return t
        if work_dir and filename in _ondisk():
            # Uncommitted but on disk → NOW (undefined is NOW).
            return float('inf')
        return None

    return resolve


def _merge_diverged(repo, project_dir, branch, local_sha, remote_sha):
    """Three-way merge ours (local_sha) and theirs (remote_sha) into the
    working tree. .lift files merge via lift_merge; other paths use
    take-changed-side-or-ours semantics. Creates a merge commit with
    both parents. Returns (commit_sha, conflicts_list).

    Divergent single-value AUDIO forms resolve last-wins by most-recent
    commit (``_audio_recency_resolver``) — this is the convergence
    merge, so resolving here is deterministic across devices; other
    merge sites annotate and defer to the next merge here.

    Raises ``UnrelatedHistoriesError`` when the two tips have no
    common ancestor — see the class docstring; never merge with an
    empty base."""
    from dulwich import porcelain

    base_sha = _find_merge_base(repo, local_sha, remote_sha)
    if not base_sha:
        raise UnrelatedHistoriesError(
            f'no common ancestor between local '
            f'{_sha_str(local_sha)[:12]} and remote '
            f'{_sha_str(remote_sha)[:12]} — refusing to merge '
            f'unrelated histories (two different projects with '
            f'the same language code?)')
    head_commit = repo[local_sha]
    remote_commit = repo[remote_sha]
    base_commit = repo[base_sha] if base_sha else None

    # ``_walk_tree`` returns ``path → blob sha`` (SHA-only since
    # 0.44.4) so building these three dicts costs ~tens of KB each,
    # not hundreds of MB. Bytes for any individual path are fetched
    # lazily via ``_blob_bytes`` and only for paths that actually
    # need merging or writing.
    base_blobs = _walk_tree(repo, base_commit.tree) if base_commit else {}
    head_blobs = _walk_tree(repo, head_commit.tree)
    remote_blobs = _walk_tree(repo, remote_commit.tree)
    lift_merge.trace(
        f'[merge-trace] _walk_tree done '
        f'base={len(base_blobs)} head={len(head_blobs)} '
        f'remote={len(remote_blobs)}')

    all_paths = set(base_blobs) | set(head_blobs) | set(remote_blobs)
    conflicts = []
    # path → ('sha', sha) for content already in git, or
    # ('bytes', bytes) for content from a LIFT merge that isn't a
    # blob yet. Holding (kind, value) tuples keeps this dict tiny —
    # in the pre-0.44.4 layout ``merged_writes`` held raw bytes for
    # every audio file, replicating what ``_walk_tree`` already
    # over-allocated.
    merged_writes = {}
    deletes = []          # paths to remove

    # Last-wins resolver for divergent single-value audio takes.
    # Lazy: does no work unless a .lift merge below actually hits an
    # audio conflict and calls it.
    audio_rec = _audio_recency_resolver(repo, local_sha, remote_sha)

    for path in sorted(all_paths):
        b = base_blobs.get(path)
        o = head_blobs.get(path)
        t = remote_blobs.get(path)

        if o is None and t is None:
            deletes.append(path)
            continue

        # Cheap-no-op fast paths — MUST precede the heavy special-case
        # branches (slots / kv / .lift) below. If a path is identical
        # on both sides, or changed on only ONE side vs the base,
        # resolve it here by reusing the existing blob: no bytes load,
        # no LIFT parse, no three_way_merge, no per-entry normalize.
        # Pre-0.54.32 these checks sat AFTER the .lift branch and were
        # unreachable for .lift, so a merge where only one side touched
        # the lexicon still parsed + normalized all ~1700 entries — the
        # dominant per-merge cost (merges run many times a day). After
        # these three `continue`s, the special-case branches below only
        # ever see the genuine both-sides-changed-differently case
        # (o != t and o != b and t != b), which is when their conflict
        # logic is actually needed.
        if o == t:
            # Identical on both sides (both-None already handled above).
            if o is None:
                deletes.append(path)
            else:
                merged_writes[path] = ('sha', o)
            continue
        if o == b:
            # Only theirs changed — take theirs (or accept their delete)
            if t is None:
                deletes.append(path)
            else:
                merged_writes[path] = ('sha', t)
            continue
        if t == b:
            # Only ours changed — take ours (or accept our delete)
            if o is None:
                deletes.append(path)
            else:
                merged_writes[path] = ('sha', o)
            continue

        # Slot claims (.azt/slots/<slot>.txt): two devices may
        # have claimed the same slot simultaneously. Pick the
        # one whose embedded claimed_at is later. Convergent
        # atomicity per the project_kv contract (NOTES_TO_DAEMON.md
        # amendment, 2026-05-28).
        if (path.startswith('.azt/slots/') and path.endswith('.txt')
                and o is not None and t is not None and o != t):
            from . import project_kv as _pkv
            o_bytes = _blob_bytes(repo, o) or b''
            t_bytes = _blob_bytes(repo, t) or b''
            ours = _pkv._parse_text(o_bytes.decode('utf-8', 'replace'))
            theirs = _pkv._parse_text(t_bytes.decode('utf-8', 'replace'))
            winner = _pkv._later_claim(ours, theirs) or ours
            body = _pkv._format_slot_file(
                winner.get('peer_id', ''),
                winner.get('device_name', ''),
                claimed_at=winner.get('claimed_at') or '',
            )
            merged_writes[path] = ('bytes', body.encode('utf-8'))
            continue
        # Scalar project KV (.azt/kv/<key>.txt): lexicographic
        # winner (deterministic + cheap; rare conflicts since
        # these are single-write-mostly settings like team_size).
        if (path.startswith('.azt/kv/') and path.endswith('.txt')
                and o is not None and t is not None and o != t):
            o_bytes = _blob_bytes(repo, o) or b''
            t_bytes = _blob_bytes(repo, t) or b''
            o_text = o_bytes.decode('utf-8', 'replace')
            t_text = t_bytes.decode('utf-8', 'replace')
            winner_text = max(o_text, t_text)
            if not winner_text.endswith('\n'):
                winner_text += '\n'
            merged_writes[path] = ('bytes',
                                   winner_text.encode('utf-8'))
            continue
        if path.endswith('.lift') and o is not None and t is not None and o != t:
            # Heavy path — load bytes only here. Free them as soon
            # as the merge call returns so the dict-of-bytes peak
            # stays at "one LIFT merge worth", not "every audio
            # file in every snapshot".
            b_bytes = _blob_bytes(repo, b) if b is not None else b''
            o_bytes = _blob_bytes(repo, o) or b''
            t_bytes = _blob_bytes(repo, t) or b''
            mr = lift_merge.three_way_merge(
                b_bytes or b'', o_bytes, t_bytes, path=path,
                audio_recency=audio_rec)
            merged_writes[path] = ('bytes', mr.merged_bytes)
            conflicts.extend(mr.conflicts)
            # Persist a forensic dump for any guard trip. The dump
            # lands under ``<working_dir>/.azt-collab/diagnostics/``
            # and gets staged into the merge commit by the
            # ``_stage_all`` below — pushed to the remote as a
            # normal file, retrievable post-hoc from any clone.
            # User isn't bothered; daemon team / future LLM can
            # diff git history to find the dumps.
            for _c in mr.conflicts:
                if lift_merge.is_guard_kind(_c.kind):
                    _write_merge_diagnostic(
                        project_dir, guard_kind=_c.kind, lift_path=path,
                        local_sha=_sha_str(local_sha),
                        remote_sha=_sha_str(remote_sha),
                        base_sha=_sha_str(base_sha) if base_sha else '',
                        base_bytes=b_bytes or b'', ours_bytes=o_bytes,
                        theirs_bytes=t_bytes,
                        merged_bytes=mr.merged_bytes,
                        conflict_fields=_c.fields)
                    break   # one dump per merged file is enough
            del b_bytes, o_bytes, t_bytes
            continue

        # Both changed differently for a non-LIFT file → take ours,
        # surface the conflict for the commit message.
        if o is not None:
            merged_writes[path] = ('sha', o)
        elif t is not None:
            merged_writes[path] = ('sha', t)
        conflicts.append(lift_merge.Conflict(
            path=path, guid='', kind='non-lift-modify-modify'))

    lift_merge.trace(
        f'[merge-trace] resolution done '
        f'writes={len(merged_writes)} deletes={len(deletes)} '
        f'conflicts={len(conflicts)}')

    # Apply to the working tree. For ``'sha'`` entries, skip the
    # write when the target SHA already matches HEAD — the working
    # tree presumably has that content already, so re-reading the
    # blob just to overwrite identical bytes is pure heap pressure.
    # For ``'bytes'`` entries (LIFT merge output) we always write.
    writes_done = 0
    for path, (kind, value) in merged_writes.items():
        if kind == 'sha':
            if head_blobs.get(path) == value:
                continue
            content = _blob_bytes(repo, value)
            if content is None:
                continue
        else:
            content = value
        full = os.path.join(project_dir, path)
        os.makedirs(os.path.dirname(full) or project_dir, exist_ok=True)
        with open(full, 'wb') as f:
            f.write(content)
        writes_done += 1
    for path in deletes:
        full = os.path.join(project_dir, path)
        try:
            os.remove(full)
        except OSError:
            pass
    lift_merge.trace(
        f'[merge-trace] apply done writes_done={writes_done} '
        f'deletes={len(deletes)}')

    _stage_all(repo, project_dir)

    local_log = _commits_between(repo, base_sha, local_sha) if base_sha else []
    remote_log = _commits_between(repo, base_sha, remote_sha) if base_sha else []
    msg_str = merge_commit.build_merge_message(
        branch=branch, local_commits=local_log, remote_commits=remote_log,
        conflicts=conflicts)
    msg = msg_str.encode('utf-8')

    bot = _enc(merge_commit.bot_identity())
    # ``porcelain.commit`` does NOT expose ``merge_heads`` as a public
    # kwarg (it's used internally only for ``amend``). Passing it
    # raises ``TypeError`` on dulwich 1.2.x, and the older grafting
    # fallback (mutate ``commit.parents`` post-hoc + re-add to object
    # store) silently produces a commit whose stored parents are
    # ``[local_sha]`` only — the next push then rejects with
    # ``DivergedBranches`` because the merge commit doesn't actually
    # contain ``remote_sha``. Drop down to the worktree-level commit
    # API, which DOES accept ``merge_heads`` and sets
    # ``c.parents = [old_head, *merge_heads]`` atomically before
    # writing the object + advancing the ref.
    sha = repo.get_worktree().commit(
        message=msg, author=bot, committer=bot,
        merge_heads=[remote_sha],
    )
    return sha, conflicts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Ignore patterns for artifacts the desktop A-Z+T app drops beside its
# LIFT file (mirrors azt's own ``vcs.py Git.ignorelist()``, minus
# azt-source-repo leftovers). ``_stage_all`` is whole-tree ``add -A``,
# so without these an adopted desktop project would commit every
# emailed-backup variant, report, and PDF. Appended (idempotently) to
# the project's ``.gitignore`` at registration — see
# ``ensure_ignore_patterns`` and the AZT persistence contract
# (azt-collab/agenda/azt_persistence_server_sync.md, G3/D5/D6).
AZT_DESKTOP_IGNORES = (
    '*.lift*txt',            # daily crash-safety backups (kept, emailed)
    '*.gz',                  # writegzip variants
    '*.7z',                  # writelzma variants
    '*.zip',
    '*.pdf',                 # chart/report output enters git only via
    '*.xcf',                 # azt's deliberate force-add paths
    'XLingPaperPDFTemp/**',
    'reports/**',
    'exports/**',
    'userlogs/**',
    'excess/**',
    'images/archive/**',
    'images/scaled/**',
    '*backupBeforeLx2LcConversion',
    '*~',
    '*.ChorusNotes',         # WeSay/Chorus sidecars from legacy sync
    '*.WeSayUserMemory',
    '*.WeSayConfig*',
    '*.WeSayUserConfig',
    '*.ChorusRescuedFile',
)

_AZT_IGNORE_HEADER = ('# A-Z+T desktop artifacts '
                      '(appended by azt_collabd at registration)')


def ensure_ignore_patterns(project_dir, patterns=AZT_DESKTOP_IGNORES):
    """Idempotently append *patterns* to ``<project_dir>/.gitignore``.

    Creates the file if absent. Existing content is never rewritten
    or reordered — only patterns not already present (as a whole
    line, comments ignored) are appended, under a marker header.
    Returns the list of patterns actually added ([] when everything
    was already covered). Best-effort: any OSError is swallowed
    after logging, since a missing ignore rule is recoverable noise
    while a failed registration is not."""
    path = os.path.join(project_dir, '.gitignore')
    try:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                existing_lines = [ln.strip() for ln in fh.readlines()]
        except FileNotFoundError:
            existing_lines = []
        have = {ln for ln in existing_lines
                if ln and not ln.startswith('#')}
        missing = [p for p in patterns if p not in have]
        if not missing:
            return []
        block = ''
        if existing_lines and existing_lines[-1] != '':
            block += '\n'
        if _AZT_IGNORE_HEADER not in existing_lines:
            block += _AZT_IGNORE_HEADER + '\n'
        block += '\n'.join(missing) + '\n'
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(block)
        return missing
    except OSError as ex:
        print(f'[register] ensure_ignore_patterns({project_dir!r}) '
              f'failed: {ex!r}', file=sys.stderr, flush=True)
        return []


def _enc(s):
    return s.encode('utf-8') if isinstance(s, str) else s


def _bytes_path(p):
    return p if isinstance(p, bytes) else os.fsencode(p)


def _find_lift(directory):
    """Return path to the first .lift file found (BFS, skips hidden dirs)."""
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in files:
            if name.endswith('.lift'):
                return os.path.join(root, name)
    return None


def _worktree_has_files(directory):
    """True when the working tree holds any non-hidden file at all —
    separates "repo with content but no .lift" (LIFT_NOT_FOUND) from
    "empty repo" (REPO_EMPTY) after a clone."""
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        if any(not f.startswith('.') for f in files):
            return True
    return False


# ── Repo fd hygiene ──────────────────────────────────────────────────────
#
# A dulwich ``Repo`` holds open file descriptors (pack files, index)
# until ``.close()`` — and reference cycles inside dulwich mean GC
# does NOT reliably release them. Field incident 2026-07-10 (karlap
# desktop, agenda/daemon_fd_leak_emfile_hardening.md): un-closed
# Repos from the ~10 s status poll + per-gesture commit paths
# exhausted the process fd table (EMFILE) in under a day, wedging
# the LAN listener, the drain loop, and even ``/v1/health``.
#
# Mechanism: the ``_track_opened_repos()`` scope collects every Repo
# ``_get_repo`` hands out on the current thread and closes them all
# on scope exit. Each public entry point below (commit_repo,
# push_repo, sync_repo, submit_file, init_repo, …) wraps its locked
# body in one scope, so nested helpers get closed too, without
# threading a repo handle through every signature. Double-close is
# safe (dulwich tolerates it — ``head_sha_of`` closes its own).
# New entry points that open repos MUST either close them or run
# inside a tracking scope.

_repo_tracking = threading.local()


@contextlib.contextmanager
def _track_opened_repos():
    prev = getattr(_repo_tracking, 'repos', None)
    _repo_tracking.repos = []
    try:
        yield
    finally:
        opened = _repo_tracking.repos
        _repo_tracking.repos = prev
        for r in opened:
            try:
                r.close()
            except Exception:
                pass


def _get_repo(project_dir):
    """Return a dulwich Repo or None. Inside a
    ``_track_opened_repos()`` scope the Repo is auto-closed at
    scope exit; outside one, the CALLER owns closing it."""
    try:
        from dulwich.repo import Repo
        r = Repo(project_dir)
    except Exception as ex:
        # NotGitRepository is the normal "no .git here" answer and
        # stays silent. An OSError (EMFILE, EIO, EACCES) is NOT
        # "not a repo" — callers will type this None as NOT_A_REPO
        # and (in the drain path) feed wan_backoff with it, so at
        # minimum the log must show what really happened
        # (2026-07-10: fd exhaustion earned nml/en a bogus 24 h
        # backoff labelled NOT_A_REPO).
        if isinstance(ex, OSError):
            print(f'[repo-open] {project_dir!r}: {ex!r} — returning '
                  f'None; callers will read this as NOT_A_REPO',
                  file=sys.stderr, flush=True)
        return None
    lst = getattr(_repo_tracking, 'repos', None)
    if lst is not None:
        lst.append(r)
    return r


def head_sha_of(project_dir):
    """Current HEAD sha (hex str) of *project_dir*, or '' when the
    dir isn't a repo / has no commits. The cheap change probe peers
    compare across polls (CLIENT_INTEGRATION § 17b)."""
    r = _get_repo(project_dir)
    if r is None:
        return ''
    try:
        h = r.refs[b'HEAD']
        return h.decode('ascii', 'replace') if isinstance(h, bytes) \
            else str(h)
    except Exception:
        return ''
    finally:
        try:
            r.close()
        except Exception:
            pass


def _is_private_ip_url(url):
    """Return True if *url* points to a private/local IP host
    (RFC 1918, loopback, link-local). Used to detect a peer-LAN
    origin URL that should be stripped — those endpoints are
    ephemeral (the peer's daemon binds a new port every start)
    and aren't meaningful as a persistent ``origin``."""
    try:
        from urllib.parse import urlparse
        import ipaddress
        host = (urlparse(url).hostname or '').strip()
        if not host:
            return False
        if host in ('localhost',):
            return True
        try:
            addr = ipaddress.ip_address(host)
            return (addr.is_private or addr.is_loopback
                    or addr.is_link_local)
        except ValueError:
            # hostname (DNS name) — not a numeric IP, can't be
            # a private-IP URL by our definition. github.com and
            # gitlab.com fall here and stay untouched.
            return False
    except Exception:
        return False


def _host_matches_known_lan_peer(host):
    """Return True if *host* appears in any paired peer's
    ``endpoints`` or ``static_endpoints`` list. Used to scope the
    retroactive ``strip_lan_origin_if_present`` fix to URLs that
    were genuinely set by ``lan_clone`` — without this check, a
    user pointing publish at ``https://192.168.0.5/gitea/repo.git``
    (legitimate self-hosted Gitea on a private IP) would have
    their origin silently wiped on every ``project_status`` poll.

    Empty / None host returns False.
    """
    if not host:
        return False
    host = host.strip().lower()
    try:
        from . import peers as _peers
    except Exception:
        return False
    try:
        all_peers = _peers.list_peers()
    except Exception:
        return False
    for entry in all_peers or []:
        for source in ('endpoints', 'static_endpoints'):
            for raw in (entry.get(source) or []):
                try:
                    h = raw.rsplit(':', 1)[0].strip().lower()
                except (AttributeError, ValueError):
                    continue
                if h and h == host:
                    return True
    return False


def wan_url(url):
    """Return *url* in the https form the daemon's WAN git ops need.

    The daemon authenticates to github/gitlab with a token over HTTPS
    (dulwich ``username``/``password``); it holds no SSH keys, and
    dulwich's ``SubprocessSSHVendor`` cannot take a password — so an
    ssh-shaped origin kills every WAN fetch/pull/push with
    ``NotImplementedError('Setting password not supported...')``
    (field repro 2026-07-21: baf, ``git@github.com:audioword-ui/
    baf.git``). Users legitimately keep ssh remotes for their own
    command-line auth, so STORED URLs (``.git/config``,
    projects.json) are deliberately left untouched; every network
    call site converts through here at use time instead.

      git@github.com:owner/repo.git   → https://github.com/owner/repo.git
      ssh://git@host[:port]/o/r.git   → https://host/o/r.git
      git+ssh://git@host/o/r.git      → https://host/o/r.git

    Anything else — https/http (LAN listener URLs included), empty,
    local paths, scheme-less strings without a ``user@`` part —
    passes through unchanged.
    """
    if not url:
        return url
    u = url.strip()
    low = u.lower()
    if low.startswith('ssh://') or low.startswith('git+ssh://'):
        from urllib.parse import urlparse
        p = urlparse(u[4:] if low.startswith('git+') else u)
        host = p.hostname or ''
        path = (p.path or '').lstrip('/')
        if host and path:
            return f'https://{host}/{path}'
        return u
    if '://' not in u and '@' in u and ':' in u:
        # scp-style ``user@host:path``. Require the ``@`` so plain
        # ``host:path`` / windows-drive strings stay untouched.
        userhost, _, path = u.partition(':')
        if '@' in userhost and '/' not in userhost and path:
            host = userhost.rpartition('@')[2]
            if host:
                return f'https://{host}/{path.lstrip("/")}'
    return u


def _import_origin_heads(repo, refs):
    """Mirror ``refs/heads/*`` from a fetch's ref advertisement into
    ``refs/remotes/origin/*``. This is what dulwich's
    ``porcelain.fetch`` does itself — but only when fetching by
    remote NAME (``_import_remote_refs`` is gated on
    ``remote_name is not None``), so the ssh-shaped-origin path in
    ``_fetch_origin``, which must fetch by URL, does it manually.
    Returns the number of refs imported."""
    n = 0
    prefix = b'refs/heads/'
    for ref, sha in (refs or {}).items():
        if isinstance(ref, bytes) and ref.startswith(prefix) and sha:
            try:
                repo.refs[b'refs/remotes/origin/' + ref[len(prefix):]] = sha
                n += 1
            except Exception:
                pass
    return n


def _origin_config_url(repo):
    """Raw ``remote.origin.url`` from ``.git/config``, or ''."""
    try:
        return repo.get_config().get(
            (b'remote', b'origin'), b'url').decode('utf-8').strip()
    except KeyError:
        return ''


def _fetch_origin(repo, username, token):
    """Fetch from origin, tolerating an ssh-shaped stored URL.

    https-form origin: fetch by remote NAME so dulwich itself updates
    ``refs/remotes/origin/*`` (see the ``_push_step_locked`` comment
    for why the name matters). ssh-shaped origin: dulwich would route
    the URL to ``SubprocessSSHVendor`` (no password support) — fetch
    by the ``wan_url()`` https form instead and import the tracking
    refs manually. Caller wraps with ``_socket_timeout`` and handles
    exceptions."""
    from dulwich import porcelain
    raw = _origin_config_url(repo)
    wan = wan_url(raw)
    if wan == raw:
        porcelain.fetch(
            repo, 'origin',
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        return
    fr = porcelain.fetch(
        repo, wan,
        username=username, password=token,
        errstream=io.BytesIO(),
    )
    n = _import_origin_heads(repo, getattr(fr, 'refs', None))
    lift_merge.trace(
        f'[sync-trace] fetch used wan-normalized {wan!r} '
        f'(ssh-shaped origin kept in config); '
        f'imported {n} tracking ref(s)')


def _pull_origin(repo, username, token):
    """``porcelain.pull`` from origin, tolerating an ssh-shaped
    stored URL — same routing rule as ``_fetch_origin``. In the
    ssh-shaped case the tracking mirror is NOT updated (dulwich only
    imports refs for named remotes, and ``pull`` returns nothing to
    import from) — that leaves ``refs/remotes/origin/*`` stale-
    BEHIND, which is the safe direction: the peek in
    ``_push_step_locked`` then can't skip the next fetch, and
    ``_fetch_origin`` heals the mirror on the next sync/drain.
    Exceptions propagate to the caller."""
    from dulwich import porcelain
    raw = _origin_config_url(repo)
    wan = wan_url(raw)
    target = 'origin'
    if wan != raw:
        target = wan
        lift_merge.trace(
            f'[sync-trace] pull using wan-normalized {wan!r} '
            f'(ssh-shaped origin kept in config)')
    porcelain.pull(
        repo, target,
        username=username, password=token,
        errstream=io.BytesIO(),
    )


def set_remote_origin_url(working_dir, url):
    """Set / replace ``remote.origin.url`` in ``<working_dir>/.git/config``.
    Returns True on success, False otherwise. Idempotent (writes only
    when the value actually changes).

    Used by the adopt-origin / remote-conflict decision-acceptance
    paths so the ``.git/config`` URL matches the registry's
    ``Project.remote_url`` after a peer's URL is adopted. Pre-0.50.27
    those handlers only updated ``projects.json``; the working-tree's
    git config stayed empty, so the next push had no remote to send
    to.

    Holds ``project_lock`` per CLAUDE.md invariant #11: any code path
    that writes ``.git/config`` must serialize against init / sync /
    strip-lan-origin / etc. Bounded timeout matches the
    ``strip_lan_origin_if_present`` siblings so we defer rather than
    block when another writer holds the lock — caller can retry.
    """
    if not working_dir or not url:
        return False
    try:
        with project_lock(working_dir, timeout=5.0):
            repo = _get_repo(working_dir)
            if repo is None:
                return False
            try:
                config = repo.get_config()
                try:
                    existing = config.get(
                        (b'remote', b'origin'), b'url').decode(
                            'utf-8', errors='replace')
                except KeyError:
                    existing = ''
                if existing == url:
                    return True
                config.set((b'remote', b'origin'), b'url',
                           _enc(url))
                config.write_to_path()
                return True
            finally:
                try:
                    repo.close()
                except Exception:
                    pass
    except LockTimeout:
        print(f'[set-origin-url] lock busy on {working_dir!r}; '
              f'caller should retry',
              file=sys.stderr, flush=True)
        return False
    except Exception as ex:
        print(f'[set-origin-url] {working_dir!r} failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False


def forget_project(langcode, delete_files=False):
    """Forget *langcode*: remove it from the registry, tombstone its
    working_dir so the auto-repair scan won't resurrect it, and drop
    the langcode from every paired peer's share allowlist (so a peer
    can't re-offer / re-sync it). With *delete_files*, also delete the
    working tree from disk.

    Two user-facing gestures map here (0.54.19+, audit F16 / the
    2026-07-22 cross-merge cleanup):
      - "Remove from device": ``delete_files=False`` — non-destructive,
        the folder stays on disk; a deliberate re-add (open-file /
        re-clone) lifts the tombstone. This is what survives the
        diag-repair rescan (the whole reason the tombstone exists).
      - "Delete data too": ``delete_files=True`` — for when the data is
        known bad; the tree is removed. Friction is the caller's job
        (risk-gated confirm in the UI); this function just executes.

    Returns a ``Result`` with ``PROJECT_FORGOTTEN`` (params: langcode,
    deleted) or ``NOT_A_PROJECT``. Holds ``project_lock`` for the whole
    operation so a concurrent commit / sweep / merge can't race the
    unregister + rmtree."""
    from . import projects as _projects
    result = Result()
    p = _projects.get(langcode)
    if p is None:
        result.add(S.NOT_A_PROJECT, langcode=langcode)
        return result
    working_dir = (p.working_dir or '').strip()

    def _do():
        # Unshare from every paired peer first — a peer holding this
        # langcode in shared_projects could otherwise re-offer or
        # (LAN) keep dialing for it after we forget locally.
        try:
            from . import peers as _peers
            for entry in _peers.list_peers() or []:
                pid = entry.get('peer_id', '') or ''
                if pid and langcode in (entry.get('shared_projects') or []):
                    _peers.remove_shared_project(pid, langcode)
        except Exception as ex:
            print(f'[forget] {langcode!r}: peer-unshare raised '
                  f'(continuing): {ex!r}', file=sys.stderr, flush=True)
        _projects.unregister(langcode)
        if working_dir:
            _projects.add_forgotten(working_dir)
        deleted = False
        if delete_files and working_dir:
            import shutil
            try:
                shutil.rmtree(working_dir)
                deleted = True
            except FileNotFoundError:
                deleted = True  # already gone — treat as success
            except Exception as ex:
                print(f'[forget] {langcode!r}: rmtree({working_dir!r}) '
                      f'raised: {ex!r}', file=sys.stderr, flush=True)
                # Registry entry is already removed + tombstoned, so
                # the project is "forgotten" even though bytes remain.
                result.add(S.PROJECT_FORGOTTEN, langcode=langcode,
                           deleted=False)
                result.add(S.SERVER_ERROR,
                           error=f'files not deleted: {ex!r}')
                return
        print(f'[forget] {langcode!r} forgotten '
              f'(delete_files={delete_files}, deleted={deleted}, '
              f'dir={working_dir!r})', file=sys.stderr, flush=True)
        result.add(S.PROJECT_FORGOTTEN, langcode=langcode,
                   deleted=deleted)

    if working_dir:
        try:
            with project_lock(working_dir, timeout=10.0):
                _do()
        except LockTimeout:
            return _busy_result(working_dir)
    else:
        # No working_dir on record — just drop the registry entry.
        _do()
    return result


def _resolve_ref_or_sha(repo, ref):
    """Resolve *ref* (a full 40-char hex SHA or a ref name) to a commit
    SHA present in *repo*, or ``None``. Ref names are tried verbatim
    and under refs/remotes/, refs/heads/, refs/tags/ — so
    ``xtest/phone-lineage-premerge`` resolves to
    ``refs/remotes/xtest/phone-lineage-premerge``."""
    ref = (ref or '').strip()
    if not ref:
        return None
    low = ref.lower()
    if len(low) == 40 and all(c in '0123456789abcdef' for c in low):
        try:
            repo[low.encode('ascii')]      # object present?
            return low.encode('ascii')
        except KeyError:
            return None
    rb = ref.encode('utf-8')
    for cand in (rb, b'refs/remotes/' + rb, b'refs/heads/' + rb,
                 b'refs/tags/' + rb):
        try:
            return repo.refs[cand]
        except KeyError:
            continue
    return None


def merge_ref_into_project(langcode, ref):
    """Merge an in-repo ref/SHA into *langcode* with the convergence
    engine (``_merge_diverged``): LIFT-aware union by guid + audio
    last-wins, the same merge a normal sync runs when two related
    lineages diverge.

    COMMIT-ONLY: creates the merge commit locally and marks the
    project ``pending_push`` so the scheduler's drain pushes it when
    the daemon is online (and ``work_offline`` is off). Never pushes
    inline — so an operator can stay disconnected, inspect the merge,
    and ``git reset --hard`` the pre-merge HEAD to undo, then reconnect
    to let the push happen. Holds ``project_lock``.

    *ref* is a full SHA or a resolvable ref name (see
    ``_resolve_ref_or_sha``). Returns a ``Result``: ``MERGED_REF``
    (params langcode / sha / n_conflicts) on success,
    ``MERGE_UNRELATED_HISTORIES`` if the two tips share no ancestor,
    ``NOT_A_PROJECT`` / ``NOT_A_REPO`` / ``SERVER_ERROR`` otherwise.

    This is the engine behind the one-shot ``sha_to_merge`` key in
    projects.json, consumed on daemon launch by
    ``consume_pending_merges`` — see docs/merge_ref_recovery.md."""
    from dulwich import porcelain
    from . import projects as _projects
    result = Result()
    p = _projects.get(langcode)
    if p is None:
        result.add(S.NOT_A_PROJECT, langcode=langcode)
        return result
    working_dir = (p.working_dir or '').strip()
    if not working_dir:
        result.add(S.NOT_A_REPO)
        return result

    def _do():
        repo = _get_repo(working_dir)
        if repo is None:
            result.add(S.NOT_A_REPO)
            return
        target = _resolve_ref_or_sha(repo, ref)
        if target is None:
            result.add(S.SERVER_ERROR, error=f'ref not found: {ref!r}')
            return
        try:
            local_sha = repo.head()
        except KeyError:
            result.add(S.NOT_A_REPO)
            return
        try:
            branch = porcelain.active_branch(repo).decode(
                'utf-8', errors='replace')
        except Exception:
            branch = 'main'
        print(f'[merge-ref] {langcode!r} merging {_sha_str(target)[:12]} '
              f'into {branch} (HEAD {_sha_str(local_sha)[:12]}); '
              f'undo with: git -C {working_dir} reset --hard '
              f'{_sha_str(local_sha)}', file=sys.stderr, flush=True)
        try:
            merge_sha, conflicts = _merge_diverged(
                repo, working_dir, branch, local_sha, target)
        except UnrelatedHistoriesError as ex:
            print(f'[merge-ref] {langcode!r} refused: {ex}',
                  file=sys.stderr, flush=True)
            result.add(S.MERGE_UNRELATED_HISTORIES, error=str(ex))
            return
        try:
            from . import scheduler as _scheduler
            _scheduler._set_pending_push(langcode, True)
        except Exception as ex:
            print(f'[merge-ref] {langcode!r} set_pending_push raised '
                  f'(merge committed; push deferred): {ex!r}',
                  file=sys.stderr, flush=True)
        print(f'[merge-ref] {langcode!r} merged -> '
              f'{_sha_str(merge_sha)[:12]} ({len(conflicts)} conflicts); '
              f'pending_push set (pushes when online)',
              file=sys.stderr, flush=True)
        result.add(S.MERGED_REF, langcode=langcode,
                   sha=_sha_str(merge_sha)[:12], n_conflicts=len(conflicts))

    try:
        with project_lock(working_dir, timeout=30.0):
            _do()
    except LockTimeout:
        return _busy_result(working_dir)
    return result


def consume_pending_merges():
    """Run any one-shot merges requested via a ``sha_to_merge`` key in
    projects.json, then clear each key. Called once per daemon launch
    from BOTH startup paths (desktop ``server.serve`` and Android
    ``server_apk/service.py:main``). Behind-the-scenes by design: the
    operator adds the key, relaunches, and reads the daemon log for the
    outcome — no RPC, no UI. Never raises; a bad entry logs and is
    skipped. See docs/merge_ref_recovery.md.

    One-shot regardless of outcome: the key is cleared even on
    failure, so a doomed merge (unrelated histories, missing SHA)
    can't retry every launch, and a SUCCESSFUL merge can't pile up an
    empty merge commit on each subsequent launch (``_merge_diverged``
    always writes a commit). Re-add the key to try again."""
    from . import projects as _projects
    try:
        pending = _projects.pending_merges()
    except Exception as ex:
        print(f'[merge-ref] pending scan failed: {ex!r}',
              file=sys.stderr, flush=True)
        return
    if not pending:
        return
    print(f'[merge-ref] {len(pending)} pending merge(s) from '
          f'projects.json: {sorted(pending)}',
          file=sys.stderr, flush=True)
    for langcode, sha in pending.items():
        try:
            result = merge_ref_into_project(langcode, sha)
            print(f'[merge-ref] {langcode!r} outcome: {result.codes()}',
                  file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[merge-ref] {langcode!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
        finally:
            try:
                _projects.clear_sha_to_merge(langcode)
            except Exception as ex:
                print(f'[merge-ref] {langcode!r} clear key failed: '
                      f'{ex!r}', file=sys.stderr, flush=True)


def strip_lan_origin_if_present(working_dir,
                                scope_to_paired_peers=True):
    """If ``<working_dir>/.git/config`` has
    ``remote.origin.url`` pointing at a paired-LAN-peer's listener
    URL, remove the entire ``[remote "origin"]`` section. Returns
    True iff a strip occurred (idempotent).

    Why: ``lan_clone`` sets ``origin`` to whatever URL we
    cloned FROM, which on LAN is
    ``https://192.168.x.y:port/<langcode>.git`` — the peer's
    listener. That URL is useless as a persistent origin (port
    changes per peer restart, and fan-out uses live mDNS rather
    than this URL). Worse, ``project_status`` reads it as
    ``remote_url`` and the publish-row gate ("hide Publish iff
    remote_url is non-empty") treats the LAN URL as "this
    project has been backed up." Stripping is the fix.

    *scope_to_paired_peers* (default True): only strip URLs whose
    host matches a paired peer's known endpoint host. Protects
    users who deliberately set origin to a private-IP URL (e.g.
    self-hosted Gitea on 192.168.0.5). The ``lan_clone`` forward-
    fix path passes ``False`` — at that point we KNOW the origin
    came from a LAN clone we just did. The ``_h_project_status``
    retroactive-fix path keeps the default ``True`` so it can
    only nuke URLs we can trace back to a paired peer.

    Runs under ``project_lock`` because ``config.write_to_path``
    rewrites ``.git/config`` on disk and the retroactive caller
    fires on every status poll — a concurrent ``init_repo`` /
    Publish / ``adopt_origin`` is the race. Bounded 2 s timeout
    so the picker-poll hot path doesn't stall; defer to next
    poll if busy.

    Called by ``lan_clone.clone_from_peer`` on fresh-clone
    (forward-fix, ``scope_to_paired_peers=False``) and by
    ``_h_project_status`` on every status poll (retroactive-fix
    for projects cloned before 0.45.37, ``scope_to_paired_peers=True``).
    """
    try:
        repo = _get_repo(working_dir)
        if repo is None:
            return False
        # Pre-check (lock-free): decide whether to bother taking
        # the lock. Two reasons to proceed:
        #   1. There's a paired-peer LAN-origin URL in
        #      ``.git/config`` that needs removing.
        #   2. There are orphan ``refs/remotes/origin/*`` tracking
        #      refs left over from a prior strip (config-section
        #      removed by an earlier release but refs not — pre-
        #      0.46.2 the strip only touched config). These
        #      produce the asymmetric ``commits_ahead`` rendering
        #      between originator and LAN-cloned peers.
        # Picker polls status every few seconds; steady-state has
        # neither, so most calls return False here without locking.
        try:
            config = repo.get_config()
            url_needs_strip = False
            try:
                origin_url = config.get(
                    (b'remote', b'origin'), b'url').decode(
                    'utf-8', errors='replace')
                if _is_private_ip_url(origin_url):
                    if scope_to_paired_peers:
                        from urllib.parse import urlparse
                        host = (
                            urlparse(origin_url).hostname or '').strip()
                        if _host_matches_known_lan_peer(host):
                            url_needs_strip = True
                    else:
                        url_needs_strip = True
            except KeyError:
                # No URL — possibly stripped by earlier release.
                pass
            orphan_refs_present = False
            if not url_needs_strip:
                # Only bother walking refs when we wouldn't already
                # be taking the lock. (When we ARE stripping the
                # URL, the locked path walks refs anyway.)
                prefix = b'refs/remotes/origin/'
                try:
                    for ref_name in repo.refs.allkeys():
                        if isinstance(ref_name, bytes) \
                                and ref_name.startswith(prefix):
                            orphan_refs_present = True
                            break
                except Exception:
                    pass
            if not url_needs_strip and not orphan_refs_present:
                return False
        finally:
            try:
                repo.close()
            except Exception:
                pass
    except Exception as ex:
        print(f'[lan-origin-strip] {working_dir!r} pre-check '
              f'failed: {ex!r}', file=sys.stderr, flush=True)
        return False
    # Pre-check said "yes, strip this." Now take the lock and
    # re-read defensively before mutating — another writer could
    # have changed origin between our pre-check and lock
    # acquisition.
    try:
        with project_lock(working_dir, timeout=2.0):
            return _strip_lan_origin_locked(
                working_dir, scope_to_paired_peers)
    except LockTimeout:
        # Hot path — defer to next status poll.
        return False
    except Exception as ex:
        print(f'[lan-origin-strip] {working_dir!r} failed: {ex!r}',
              file=sys.stderr, flush=True)
        return False


def _strip_lan_origin_locked(working_dir, scope_to_paired_peers):
    repo = _get_repo(working_dir)
    if repo is None:
        return False
    try:
        did_strip = False
        config = repo.get_config()
        try:
            origin_url = config.get(
                (b'remote', b'origin'), b'url').decode(
                'utf-8', errors='replace')
        except KeyError:
            origin_url = ''
        # Strip URL pass: only when a private-IP URL is present and
        # (when scoped) traces to a paired peer.
        if origin_url and _is_private_ip_url(origin_url):
            host_ok = True
            if scope_to_paired_peers:
                from urllib.parse import urlparse
                host = (urlparse(origin_url).hostname or '').strip()
                host_ok = _host_matches_known_lan_peer(host)
            if host_ok:
                try:
                    config.remove_section((b'remote', b'origin'))
                except (KeyError, AttributeError):
                    # Older dulwich: no remove_section. Best-
                    # effort by clearing the url key — leaves an
                    # empty section, but project_status.remote_url
                    # will read empty (which is what the UI gate
                    # cares about).
                    try:
                        config.set(
                            (b'remote', b'origin'), b'url', b'')
                    except Exception:
                        return False
                config.write_to_path()
                print(f'[lan-origin-strip] {working_dir!r}: '
                      f'removed paired-peer origin '
                      f'url={origin_url!r}',
                      file=sys.stderr, flush=True)
                did_strip = True
        # Strip tracking-ref pass: ``porcelain.clone`` writes
        # ``refs/remotes/origin/<branch>`` at clone time; the URL
        # strip above (or a prior version of it) doesn't touch
        # those refs, so they linger at clone-time SHA forever
        # with nothing maintaining them. ``_count_commits_ahead``
        # then walks against a stale phantom, producing the
        # asymmetric LANOK rendering the recorder team reported.
        # Strip whenever URL has just been removed OR (config
        # already URL-less + tracking refs orphaned). The orphan
        # check covers projects stripped by earlier daemon
        # versions that left the refs behind.
        # Read the URL value, not just key existence (0.50.48).
        # Pre-0.50.48 we treated "url key exists" as
        # has_url_now=True even if the value was empty (``url = ``
        # in config) — which is the half-stripped state left
        # behind by older daemons that hit the
        # ``config.remove_section`` AttributeError fallback above.
        # That state blocked the orphan-tracking-ref cleanup
        # because has_url_now stayed True. Field repro 2026-06-05:
        # phone had `[remote "origin"]\n\turl = ` plus three
        # orphan ``refs/remotes/origin/*`` refs that no cleanup
        # pass ever removed because has_url_now read True every
        # time. Treat empty URL value as no URL.
        url_now_value = ''
        try:
            try:
                raw = config.get((b'remote', b'origin'), b'url')
                try:
                    url_now_value = raw.decode(
                        'utf-8', errors='replace').strip()
                except Exception:
                    url_now_value = ''
            except KeyError:
                url_now_value = ''
        except Exception:
            url_now_value = ''
        has_url_now = bool(url_now_value)
        # If we just stripped, URL is gone. If URL was already
        # absent (or empty), this is an orphan-cleanup pass. In
        # either case tracking refs under
        # ``refs/remotes/origin/`` shouldn't exist on a properly-
        # cleaned LAN-cloned project.
        if not has_url_now:
            prefix = b'refs/remotes/origin/'
            removed = []
            try:
                ref_keys = list(repo.refs.allkeys())
            except Exception as ex:
                ref_keys = []
                print(f'[lan-origin-strip] {working_dir!r}: '
                      f'allkeys raised {ex!r}', file=sys.stderr,
                      flush=True)
            for ref_name in ref_keys:
                if not isinstance(ref_name, bytes):
                    continue
                if not ref_name.startswith(prefix):
                    continue
                try:
                    del repo.refs[ref_name]
                    removed.append(
                        ref_name.decode('utf-8', 'replace'))
                except Exception as ex:
                    print(f'[lan-origin-strip] {working_dir!r}: '
                          f'del {ref_name!r} raised {ex!r}',
                          file=sys.stderr, flush=True)
            if removed:
                print(f'[lan-origin-strip] {working_dir!r}: '
                      f'removed orphan tracking refs '
                      f'{removed!r}', file=sys.stderr, flush=True)
                did_strip = True
        return did_strip
    finally:
        try:
            repo.close()
        except Exception:
            pass


def reconcile_publish_state_on_startup():
    """One-shot auto-retry for the pre-0.50.52 Publish bug.

    Walks every registered project and looks for the specific
    fingerprint of the bug: ``.git/config`` has
    ``remote.origin.url`` set but the registry's
    ``Project.remote_url`` is empty. That combination can only
    arise from the pre-0.50.52 silent-failure path, where
    ``_init_repo_locked`` wrote the URL into ``.git/config`` but
    the subsequent registry update in ``_h_init_project`` didn't
    land (older daemon serving the RPC, or the for-loop missed
    the project). Post-0.50.52 paths keep both sides in sync:
    both-set on success, both-empty on REMOTE_CREATE_FAILED.

    For each project that matches, re-fire the publish the user
    already committed to — call ``init_repo`` with the captured
    URL (and ``rollback_origin_on_create_fail=False`` so a
    transient offline / outage / missing-creds boot doesn't
    expose a phantom Publish button on a project the user has
    already chosen to publish). State transitions only on a
    successful PUSHED:

    - PUSHED → registry gets ``remote_url`` / ``last_sync`` /
      ``last_commit`` populated; next boot finds no mismatch;
      publish-fanout fires so paired peers learn the URL.
    - Anything else (REMOTE_CREATE_FAILED on outage, AUTH_REQUIRED
      on missing creds, BUSY on lock contention, etc.) → log
      and move on; the next daemon startup retries silently.

    Safety: the mismatch fingerprint is unreachable from any
    post-0.50.52 code path, so this never runs against a healthy
    project. A transient github outage during a manual Publish
    produces REMOTE_CREATE_FAILED, which the picker-initiated
    rollback clears *both* sides — leaving both-empty, not
    ``.git/config``-set + registry-empty. So this reconciliation
    can't accidentally fire on outage-cleanup.

    Called from the daemon's ``serve()`` startup right after
    ``scheduler.reconcile_on_startup()``."""
    from . import projects as _projects_mod
    from . import store as _store
    succeeded = []
    skipped = []
    n_walked = 0
    n_mismatch = 0
    for p in _list_projects_safely(_projects_mod):
        n_walked += 1
        working_dir = (p.working_dir or '').strip()
        registry_url = (p.remote_url or '').strip()
        if not working_dir or registry_url:
            continue
        captured_url = _read_origin_url(working_dir)
        if not captured_url:
            continue
        # Mismatch fingerprint hit. Auto-retry the publish.
        n_mismatch += 1
        git_user, token = _store.get_sync_credentials(captured_url)
        contributor = _store.get_contributor()
        if not token:
            skipped.append((p.langcode, 'no_credentials'))
            continue
        if not contributor:
            skipped.append((p.langcode, 'no_contributor'))
            continue
        try:
            result = init_repo(
                working_dir, captured_url, git_user, token,
                branch='main', contributor_name=contributor,
                rollback_origin_on_create_fail=False)
        except Exception as ex:
            skipped.append((p.langcode, f'init_repo raised: {ex!r}'))
            continue
        codes = result.codes()
        if 'PUSHED' in codes or 'COMMITTED_AND_PUSHED' in codes:
            try:
                _projects_mod.set_remote_url(p.langcode, captured_url)
                _projects_mod.set_last_sync(p.langcode)
                _projects_mod.set_last_commit(p.langcode)
            except Exception as ex:
                # Registry write failed but the github push
                # succeeded. Log and move on — next boot will
                # see the mismatch again and re-fire publish,
                # which will hit ``REMOTE_UNCHANGED`` and the
                # 422 "already exists" path on github,
                # eventually re-PUSH and write the registry.
                print(f'[publish-reconcile] {p.langcode!r}: '
                      f'PUSHED but registry write raised {ex!r}',
                      file=sys.stderr, flush=True)
            succeeded.append((p.langcode, captured_url, list(codes)))
            # Fan out the URL to paired peers (deferred to
            # avoid circular import: ``server`` imports ``repo``).
            try:
                from . import server as _server
                _server._spawn_publish_fanout(p.langcode, captured_url)
            except Exception as ex:
                print(f'[publish-reconcile] {p.langcode!r}: '
                      f'fanout spawn raised {ex!r}',
                      file=sys.stderr, flush=True)
        else:
            skipped.append((p.langcode, list(codes)))
    # Always emit a summary so the daemon log shows proof this
    # function ran, even when there's nothing to do. Without this,
    # the all-healthy-projects case is indistinguishable from
    # "function silently didn't fire" in the log — which is
    # exactly the diagnostic gap that hid the missing Android
    # callsite for two iterations of 0.50.x.
    print(f'[publish-reconcile] walked={n_walked} '
          f'mismatch={n_mismatch} succeeded={len(succeeded)} '
          f'deferred={len(skipped)}',
          file=sys.stderr, flush=True)
    if succeeded:
        print(f'[publish-reconcile] auto-retry SUCCEEDED for '
              f'{len(succeeded)} project(s): {succeeded!r}',
              file=sys.stderr, flush=True)
    if skipped:
        print(f'[publish-reconcile] auto-retry deferred for '
              f'{len(skipped)} project(s) (state unchanged, '
              f'next boot will retry): {skipped!r}',
              file=sys.stderr, flush=True)


def diagnose_and_repair_registry_on_startup():
    """Boot-time diagnostic + auto-repair pass.

    Two things happen here, in order:

    1. **Log a registry/filesystem snapshot.** Every line of the
       text produced by ``server._build_diagnostic_snapshot`` is
       written to stderr (so it ends up in the daemon log if
       file-logging is enabled). Captures ``projects.json`` state,
       on-disk subdirs, ``.git`` / LIFT presence per subdir, and
       which subdirs are registered. The snapshot is what the
       picker's Share-diagnostics button ships when the user is
       stuck on an empty picker; including it in the boot log
       means we have a record from every startup even when the
       user never thinks to grab it manually.

    2. **Re-register orphan working_dirs.** A subdir of
       ``$AZT_HOME`` is an "orphan" if it contains both ``.git/``
       and a ``*.lift`` file but is not keyed in ``projects.json``.
       Common causes: ``projects.json`` zeroed by a crash mid-write;
       partial restore that recovered the working tree but not
       the index; daemon respawn after a registry write was
       refused by ``_LoadFailed``. Each orphan is passed to
       ``projects.register`` with the directory name as langcode,
       the first ``*.lift`` file found as ``lift_path``, and
       ``remote.origin.url`` from ``.git/config`` as ``remote_url``.

    **Safety.** Never removes registry entries; never alters
    working-tree contents; never overwrites a corrupt non-empty
    ``projects.json`` (``projects.register`` refuses with
    ``RuntimeError`` when ``_LoadFailed`` is in play, so a parse
    failure halts the repair pass rather than clobbering the file).

    Always emits a summary line, even on the no-orphans-found
    path (per ``feedback_always_emit_summary``). Called from both
    desktop ``serve()`` and Android ``server_apk/service.py:main()``
    startup (per ``feedback_dual_entry_path_startup_hooks``).
    """
    from . import projects as _projects_mod
    from .paths import azt_home as _azt_home

    try:
        from . import server as _server
        snap = _server._build_diagnostic_snapshot()
        for line in snap.splitlines():
            print(f'[diag] {line}', file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[diag] snapshot generation raised {ex!r}',
              file=sys.stderr, flush=True)

    try:
        home = _azt_home()
    except Exception as ex:
        print(f'[diag-repair] azt_home() raised {ex!r}; '
              f'skipping repair pass',
              file=sys.stderr, flush=True)
        return

    # ``projects.json`` corruption blocks the repair pass before it
    # starts: ``_load_raw`` returns ``_LoadFailed``, every
    # ``register()`` raises ``RuntimeError``. Detection is "does the
    # file parse?" rather than "is it zero bytes?" — field repro
    # (db033cd4 device, 0.52.1 boot log) showed an 839-byte file
    # whose first byte was non-JSON (almost certainly the ext4
    # power-fail / write-fail pattern: inode size landed, data
    # blocks didn't, file looks the right size but is full of null
    # bytes). 0.52.1's zero-byte-only guard didn't catch it.
    #
    # Rescue strategy: move the corrupt file aside to a
    # ``.corrupt-<timestamp>`` sibling. The next ``_load_raw``
    # returns ``{}`` (file is missing) and the orphan-scan below
    # re-seeds from the on-disk working_dirs. The forensic copy
    # preserves the original content for manual inspection — if
    # something WAS salvageable, support can recover it; nothing
    # is destroyed silently.
    #
    # Safety: this is reachable only when ``json.load`` already
    # failed. A healthy ``projects.json`` flows through ``list_all``
    # → ``registered_dirs`` populated → repair pass adds only what
    # genuinely isn't registered. No risk of moving aside a
    # readable-but-stale registry.
    pj_path = _projects_mod.projects_path()
    try:
        if os.path.exists(pj_path):
            with open(pj_path, 'r', encoding='utf-8') as _pj:
                import json as _json
                _json.load(_pj)
            registry_parseable = True
        else:
            registry_parseable = True
    except Exception as ex:
        registry_parseable = False
        try:
            import time as _t
            stamp = _t.strftime('%Y%m%d_%H%M%S', _t.localtime())
            backup = f'{pj_path}.corrupt-{stamp}'
            os.rename(pj_path, backup)
            print(f'[diag-repair] projects.json failed to parse '
                  f'({ex!r}); moved aside to {backup!r} so '
                  f'register() can re-seed. Original preserved for '
                  f'forensic inspection — do NOT delete unless you '
                  f'have verified there is nothing salvageable.',
                  file=sys.stderr, flush=True)
        except OSError as mv_ex:
            print(f'[diag-repair] projects.json unparseable AND could '
                  f'not be moved aside ({mv_ex!r}); halting repair. '
                  f'Manual recovery needed.',
                  file=sys.stderr, flush=True)
            return

    registered_dirs = set()
    try:
        for p in _projects_mod.list_all():
            wd = (p.working_dir or '').strip()
            if wd:
                try:
                    registered_dirs.add(os.path.realpath(wd))
                except Exception:
                    pass
    except Exception as ex:
        print(f'[diag-repair] list_all raised {ex!r}; skipping repair '
              f'(treat as corrupt registry — manual recovery required)',
              file=sys.stderr, flush=True)
        return

    # Project working_dirs live under ``$AZT_HOME/projects/<langcode>/``
    # (see ``lan_clone._project_dir`` for the canonical convention,
    # and ``server._h_list_projects`` for the existing empty-registry
    # diagnostic that scans the same directory). Older layouts that
    # put working_dirs directly under ``$AZT_HOME`` are not in use
    # in the field — scanning only the canonical location avoids
    # false positives (CAWL repos, logs dirs, peer.crt directories,
    # etc. sitting at the home root).
    projects_dir = os.path.join(home, 'projects')
    if not os.path.isdir(projects_dir):
        print(f'[diag-repair] no projects directory at '
              f'{projects_dir!r}; nothing to scan',
              file=sys.stderr, flush=True)
        print(f'[diag-repair] scanned=0 candidates=0 '
              f'repaired=0 failed=0',
              file=sys.stderr, flush=True)
        return

    try:
        names = sorted(os.listdir(projects_dir))
    except OSError as ex:
        print(f'[diag-repair] listdir({projects_dir}) raised {ex!r}; '
              f'skipping repair',
              file=sys.stderr, flush=True)
        return

    # Projects the user EXPLICITLY forgot must not be resurrected by
    # this auto-scan (Kent 2026-07-22: re-adding is a deliberate act).
    try:
        forgotten = _projects_mod.forgotten_dirs()
    except Exception as ex:
        print(f'[diag-repair] forgotten_dirs raised {ex!r}; '
              f'proceeding with empty tombstone',
              file=sys.stderr, flush=True)
        forgotten = set()

    candidates = []
    for n in names:
        p = os.path.join(projects_dir, n)
        if not os.path.isdir(p) or n.startswith('.'):
            continue
        try:
            real_p = os.path.realpath(p)
        except Exception:
            continue
        if real_p in registered_dirs:
            continue
        if real_p in forgotten:
            print(f'[diag-repair] skipping {n!r}: explicitly '
                  f'forgotten (tombstone) — re-add is manual',
                  file=sys.stderr, flush=True)
            continue
        if not os.path.isdir(os.path.join(p, '.git')):
            continue
        lift_path = ''
        try:
            for fn in sorted(os.listdir(p)):
                if fn.lower().endswith('.lift'):
                    lift_path = os.path.join(p, fn)
                    break
        except OSError:
            continue
        if not lift_path:
            continue
        remote_url = _read_origin_url(p)
        candidates.append((n, p, lift_path, remote_url))

    repaired = []
    failed = []
    for (langcode, working_dir, lift_path, remote_url) in candidates:
        try:
            _projects_mod.register(
                langcode=langcode,
                working_dir=working_dir,
                lift_path=lift_path,
                remote_url=remote_url,
            )
            repaired.append((langcode, working_dir, remote_url))
            print(f'[diag-repair] registered orphan {langcode!r} '
                  f'working_dir={working_dir!r} '
                  f'lift_path={lift_path!r} '
                  f'remote_url={remote_url!r}',
                  file=sys.stderr, flush=True)
        except RuntimeError as ex:
            failed.append((langcode, str(ex)))
            print(f'[diag-repair] {langcode!r}: register refused '
                  f'({ex}); halting repair pass — manual recovery '
                  f'of projects.json needed',
                  file=sys.stderr, flush=True)
            break
        except Exception as ex:
            failed.append((langcode, repr(ex)))
            print(f'[diag-repair] {langcode!r}: register raised {ex!r}',
                  file=sys.stderr, flush=True)

    print(f'[diag-repair] scanned={len(names)} '
          f'candidates={len(candidates)} '
          f'repaired={len(repaired)} failed={len(failed)}',
          file=sys.stderr, flush=True)


def _list_projects_safely(projects_mod):
    """Wrap ``projects_mod.list_all()`` so a registry-read
    failure in the reconciliation pass doesn't take down the
    daemon's startup."""
    try:
        return list(projects_mod.list_all())
    except Exception as ex:
        print(f'[publish-reconcile] list_all failed: {ex!r}',
              file=sys.stderr, flush=True)
        return []


def _read_origin_url(working_dir):
    """Return the ``remote.origin.url`` value from
    ``<working_dir>/.git/config``, or ``''`` if the repo isn't
    initialized or the key is absent. Caller doesn't need the
    project_lock for this — it's a stale read used only to
    decide whether to take the lock and act."""
    try:
        repo = _get_repo(working_dir)
        if repo is None:
            return ''
        try:
            config = repo.get_config()
            try:
                raw = config.get((b'remote', b'origin'), b'url')
                return raw.decode('utf-8', errors='replace').strip()
            except KeyError:
                return ''
        finally:
            try:
                repo.close()
            except Exception:
                pass
    except Exception:
        return ''


def _ensure_atomic_pending_self_heal(repo, project_dir):
    """Self-healing migration for pre-0.45.35 projects whose
    ``.gitignore`` didn't list ``.azt_atomic_pending/`` and where
    earlier code paths had already tracked scratch tokens into
    the repo. Two pieces:

    1. Append ``.azt_atomic_pending/`` and ``.azt_atomic_orphans/``
       to ``.gitignore`` if missing.
    2. ``git rm --cached`` any tracked file under
       ``.azt_atomic_pending/`` — leaves the file on disk so
       ``atomic_recovery`` can still process it on its next scan,
       but removes it from the index so it stops showing up as
       ``staged_mod`` and bloating every subsequent commit.

    Field log baf 2026-05-22: a tester had 8 tokens tracked,
    each ~3.36 MB, contributing 27 MB of scratch content to
    every commit and persisting as ``n_changes >= 8`` even when
    no edits happened. Recovery couldn't help because by the
    time it scanned the directory the files were already
    tracked-as-modified rather than untracked orphans.

    Idempotent: a no-op once the state is correct."""
    # Part 1: .gitignore append.
    gitignore = os.path.join(project_dir, '.gitignore')
    needed = ('.azt_atomic_pending/', '.azt_atomic_orphans/')
    have = ''
    try:
        with open(gitignore) as fh:
            have = fh.read()
    except (IOError, OSError):
        pass
    to_add = [n for n in needed if n not in have]
    if to_add:
        try:
            with open(gitignore, 'a') as fh:
                if have and not have.endswith('\n'):
                    fh.write('\n')
                for n in to_add:
                    fh.write(n + '\n')
            print(f'[gitignore-migrate] {project_dir!r}: appended '
                  f'{to_add!r}',
                  file=sys.stderr, flush=True)
        except (IOError, OSError) as ex:
            print(f'[gitignore-migrate] {project_dir!r}: append '
                  f'failed: {ex!r}',
                  file=sys.stderr, flush=True)

    # Part 2: untrack any currently-indexed atomic-pending tokens.
    try:
        index = repo.open_index()
        prefix = b'.azt_atomic_pending/'
        to_remove = [p for p in list(index) if p.startswith(prefix)]
        if to_remove:
            for path in to_remove:
                del index[path]
            index.write()
            print(f'[gitignore-migrate] {project_dir!r}: untracked '
                  f'{len(to_remove)} stale atomic-pending token(s) '
                  f'(files preserved on disk for recovery to process)',
                  file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[gitignore-migrate] {project_dir!r}: index '
              f'cleanup failed: {ex!r}',
              file=sys.stderr, flush=True)


def _stage_all(repo, project_dir):
    """Stage all modified and untracked files (equivalent to git add -A),
    EXCEPT the daemon-internal scratch dir ``.azt_atomic_pending/``.

    The scratch dir holds in-flight ``atomic_open_write`` files between
    phase 1 (peer wrote bytes via FD) and phase 2 (daemon renames to
    final). A peer crash / network failure between the two phases leaves
    the scratch file orphaned. ``_stage_all`` used to pick those up and
    commit them — the symptom was ``.azt_atomic_pending/<token>`` blobs
    landing in the GitHub repo instead of the audio/LIFT files they
    were supposed to become. Filtering them here is the
    belt-and-braces alongside the ``.gitignore`` entry on repo init.

    Pre-staging migration runs each call: see
    ``_ensure_atomic_pending_self_heal``. It self-heals any project
    whose .gitignore predates the ``.azt_atomic_pending/`` rule and/or
    whose index contains tracked scratch tokens from earlier code paths.
    """
    _ensure_atomic_pending_self_heal(repo, project_dir)
    from dulwich import porcelain
    status = porcelain.status(repo)
    paths = []

    def _should_stage(rel):
        s = rel if isinstance(rel, str) else rel.decode(
            'utf-8', errors='replace')
        return not (s == '.azt_atomic_pending'
                    or s.startswith('.azt_atomic_pending/'))

    for f in status.unstaged:
        if _should_stage(f):
            paths.append(_bytes_path(f))

    for f in status.untracked:
        rel = f if isinstance(f, str) else f.decode('utf-8', errors='replace')
        if not _should_stage(rel):
            continue
        full = os.path.join(project_dir, rel)
        if os.path.isfile(full):
            paths.append(_bytes_path(rel))
        elif os.path.isdir(full):
            # dulwich reports untracked dirs as a single entry;
            # walk them to find the actual files
            for root, _dirs, files in os.walk(full):
                for name in files:
                    fp = os.path.join(root, name)
                    rp = os.path.relpath(fp, project_dir)
                    if _should_stage(rp):
                        paths.append(_bytes_path(rp))

    if paths:
        porcelain.add(repo, paths=paths)


def _safe_email_segment(s):
    """Make ``s`` safe for the local-part / domain-part of an
    email-shaped string. Lowercase; spaces → ``_``; anything not
    in ``a-z0-9_-`` dropped. Empty input becomes ``'unknown'``."""
    out = []
    for ch in (s or '').lower():
        if ch.isalnum() or ch in ('_', '-'):
            out.append(ch)
        elif ch == ' ':
            out.append('_')
        # else drop
    cleaned = ''.join(out).strip('-_')
    return cleaned or 'unknown'


def _default_author(contributor_name, device_name=None):
    """Compose the git author identity for a commit.

    Author NAME = the contributor's display name verbatim, so
    GitHub's author-aggregation groups commits by person.
    Author EMAIL = ``<safe_contributor>@<safe_device>`` so
    ``git log --format='%ae'`` differentiates by device when one
    person commits from multiple devices.

    The email is non-routable (the suite doesn't have email
    infrastructure for users); it's an identifier, not an
    address. GitHub-side, the commit shows up under the
    contributor's name; email shows in the raw commit metadata
    and disambiguates between devices.

    ``device_name=None`` (the default for in-tree callers)
    triggers a lazy lookup of the daemon's stored device name
    via ``store.get_device_name()``, which auto-populates from
    the OS on first read. Explicit empty string skips the lookup
    and produces ``@unknown`` — useful for tests that want
    deterministic output without touching the store.

    Pre-0.40 the email was always ``<safe>@device`` (literal
    string ``device``), which left no useful disambiguation."""
    safe_name = _safe_email_segment(contributor_name)
    if device_name is None:
        from . import store as _store
        device_name = _store.get_device_name()
    safe_device = _safe_email_segment(device_name)
    return _enc(f'{contributor_name} <{safe_name}@{safe_device}>')


def _app_committer():
    """Return committer identity for the host app (bot identity)."""
    slug = _config.get()['app_slug']
    return _enc(f'{slug}[bot] <{slug}[bot]@users.noreply.github.com>')


def _ensure_remote_repo(remote_url, username, token):
    """Create the remote repo on GitHub/GitLab if it doesn't exist yet.
    On GitHub, also adds GITHUB_COLLABORATOR to the repo.

    Returns (ok, Status|None). The Status describes creation/failure if
    it applies; when the repo already existed no Status is returned.
    """
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    # Idempotent — current callers already do this, but a future
    # caller missing it would reproduce the SSL: CERTIFICATE_VERIFY_FAILED
    # bug we just fixed in projects.create_from_template.
    _ensure_ssl()

    # scp-form ``git@host:owner/repo`` has no scheme, so urlparse
    # would put everything in ``path`` and the owner/repo split
    # below would produce garbage — normalize first (0.54.11).
    remote_url = wan_url(remote_url)

    from urllib.parse import urlparse
    parsed = urlparse(remote_url)
    host = parsed.hostname or ''
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    is_known_host = 'github.com' in host or 'gitlab' in host
    if len(parts) < 2:
        if is_known_host:
            print(f'[publish] remote-create FAILED: cannot parse '
                  f'owner/repo from {remote_url!r}',
                  file=sys.stderr, flush=True)
            return False, Status(S.REMOTE_CREATE_FAILED,
                                 {'error': f'cannot parse owner/repo from {remote_url}'})
        # Unknown host (gitea / forgejo / local dulwich.web / LAN
        # git-daemon) with no owner/repo path component — nothing
        # to auto-create, let push proceed. Pre-0.43.22 this
        # returned REMOTE_CREATE_FAILED unconditionally, which
        # blocked every non-github sync against a flat-root URL
        # (every test_local_git_remote case).
        print(f'[publish] skip remote-create: unknown host, flat '
              f'path host={host!r} url={remote_url!r}',
              file=sys.stderr, flush=True)
        return True, None
    owner, repo_name = parts[0], parts[1]

    # If the URL points at a namespace we're not the authenticated
    # user of (peer's repo we adopted via LAN share, github org we
    # belong to, etc.), do not POST /user/repos — that endpoint
    # creates under the *authenticated* user's namespace regardless
    # of what owner the URL says, producing an orphan ``B/<repo>``
    # repo when B publishes to ``A/<repo>``. Skip create and let the
    # push reveal the real outcome: 200 if we're a collaborator,
    # 403 if not. Added 0.50.27.
    #
    # 0.50.55: skip this heuristic entirely when using GitHub App
    # auth (``username == 'x-access-token'``, the literal
    # placeholder GitHub uses for HTTP basic-auth with installation
    # tokens). The check was designed for PAT auth where
    # ``username`` is the user's GitHub login; with App auth the
    # username is always ``x-access-token``, which never matches
    # any URL owner, so the heuristic was firing a false positive
    # on every App-authenticated publish and blocking the POST.
    # POST /user/repos with an installation token is scoped to the
    # installation's account — there's no risk of wrong-namespace
    # creation, so the protection isn't needed. If the
    # installation isn't on the URL's owner, github returns 403/422
    # and ``[publish] remote-create FAILED owner/repo: <code>``
    # surfaces the real cause.
    is_github_app_auth = (str(username or '').lower() == 'x-access-token')
    if (is_known_host
            and username
            and not is_github_app_auth
            and owner.lower() != str(username).lower()):
        print(f'[publish] skip remote-create: owner mismatch '
              f'owner={owner!r} username={username!r} '
              f'url={remote_url!r}',
              file=sys.stderr, flush=True)
        return True, Status(
            S.REMOTE_OWNER_MISMATCH_SKIP_CREATE,
            {'owner': owner,
             'username': username,
             'url': remote_url})

    if 'github.com' in host:
        api_url = 'https://api.github.com/user/repos'
        payload = json.dumps({
            'name': repo_name,
            'private': True,
            'auto_init': False,
        }).encode()
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
        }
    elif 'gitlab' in host:
        api_url = f'https://{host}/api/v4/projects'
        payload = json.dumps({
            'name': repo_name,
            'visibility': 'private',
            'initialize_with_readme': False,
        }).encode()
        headers = {
            'PRIVATE-TOKEN': token,
            'Content-Type': 'application/json',
        }
    else:
        print(f'[publish] skip remote-create: unknown host '
              f'host={host!r} url={remote_url!r}',
              file=sys.stderr, flush=True)
        return True, None   # Unknown host — assume repo exists, let push fail

    print(f'[publish] POST {api_url} owner={owner!r} repo={repo_name!r}',
          file=sys.stderr, flush=True)
    created = False
    try:
        req = Request(api_url, data=payload, headers=headers, method='POST')
        urlopen(req, timeout=30)
        created = True
        print(f'[publish] remote-create OK: created {owner}/{repo_name}',
              file=sys.stderr, flush=True)
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        # 422 = already exists (GitHub), 400 = already exists (GitLab)
        if e.code in (422, 400) and 'already' in body.lower():
            print(f'[publish] remote-create: {owner}/{repo_name} '
                  f'already exists ({e.code})',
                  file=sys.stderr, flush=True)
            pass   # Already exists — fine
        else:
            print(f'[publish] remote-create FAILED '
                  f'{owner}/{repo_name}: {e.code} {body[:200]!r}',
                  file=sys.stderr, flush=True)
            return False, Status(S.REMOTE_CREATE_FAILED,
                                 {'error': f'{e.code}: {body[:200]}'})
    except (URLError, OSError) as e:
        print(f'[publish] remote-create FAILED '
              f'{owner}/{repo_name}: {e!r}',
              file=sys.stderr, flush=True)
        return False, Status(S.REMOTE_CREATE_FAILED, {'error': str(e)})

    # Add collaborator on GitHub repos. Always log the outcome
    # (success or failure) so the daemon trail carries proof the
    # call was attempted — same always-emit-summary lesson as
    # the publish-reconcile fix in 0.50.55. ``add_collaborator``
    # returns 'invited' (201) or 'already' (204 / 422 / owner
    # adding themselves), and raises on real failures.
    collaborator = _config.get()['collaborator']
    if 'github.com' in host and collaborator:
        try:
            outcome = add_collaborator(
                owner, repo_name, collaborator, token)
            print(f'[collab] add_collaborator owner={owner!r} '
                  f'repo={repo_name!r} '
                  f'collaborator={collaborator!r} → {outcome}',
                  file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[collab] add_collaborator owner={owner!r} '
                  f'repo={repo_name!r} '
                  f'collaborator={collaborator!r} '
                  f'FAILED: {ex!r}',
                  file=sys.stderr, flush=True)

    if created:
        return True, Status(S.REMOTE_REPO_CREATED,
                            {'owner_repo': f'{owner}/{repo_name}'})
    return True, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def repo_status_summary(project_dir):
    """
    Return (branch, remote_url, n_changes, wan_unshared) describing
    the project directory, or None if it is not a git repository.
    (Not a Result — this is a raw accessor for UI status indicators.)

    ``wan_unshared`` is the number of commits on the current branch
    not yet on ``refs/remotes/origin/<branch>``. Computed from the
    *local* cache of the remote ref (no network round-trip) so the
    value is whatever is true given the last fetch / push:

    - No origin remote configured → walk-from-HEAD (every commit
      is unpublished by definition; LAN-only project case since
      0.46.1, signals "no github backup")
    - Origin configured but never pushed → 0 (no remote ref to
      compare against; the indicator should read OK rather than
      double-counting the unpushed initial commit as "behind")
    - Local commits since last push → N>0 (peer-rendered as the
      ``WAN-N`` count per CLIENT_INTEGRATION.md § 17b)

    A stale cache (peer was offline since last commit, didn't fetch)
    can under-report; that's acceptable per the recorder's UX
    contract — the indicator falls back to OK rather than guessing
    against unobserved remote state. Filed by azt_recorder 1.37.6 in
    ``azt_collab_client/NOTES_TO_DAEMON.md``.

    Pre-0.47.0 this field was named ``commits_ahead``; renamed
    in lockstep with the wire-format split (wan_unshared,
    lan_unshared, at_risk). ``lan_unshared`` and ``at_risk`` are
    computed separately via ``_lan_unshared`` and ``_at_risk``
    (they need the langcode for peer lookup; this function does not).
    """
    repo = None
    try:
        from dulwich import porcelain
        repo = _get_repo(project_dir)
        if repo is None:
            return None

        try:
            branch = porcelain.active_branch(repo).decode('utf-8', errors='replace')
        except Exception:
            refs = repo.refs.get_symrefs()
            head_ref = refs.get(b'HEAD', b'')
            branch = head_ref.decode('utf-8').replace('refs/heads/', '') or '(detached)'

        try:
            remote_url = repo.get_config().get(
                (b'remote', b'origin'), b'url'
            ).decode('utf-8')
        except KeyError:
            remote_url = ''

        try:
            st = porcelain.status(repo)
            n = (len(st.staged.get('add', [])) +
                 len(st.staged.get('modify', [])) +
                 len(st.staged.get('delete', [])) +
                 len(st.unstaged) +
                 len(st.untracked))
            # Diagnostic: when n is large enough to be interesting,
            # dump the actual file paths each bucket contains. Field
            # symptom (0.45.x) was ``n_changes`` jumping from 1 to
            # 71 with no peer-side write activity in the daemon log
            # — we couldn't tell whether the count was real
            # untracked files (and which ones) or a counting quirk.
            # One line per call lets a tester sharing the daemon log
            # answer "what are those 70 files?" without rebuilding.
            # Threshold of 5 keeps the line out of healthy-project
            # noise. 0.45.30.
            if n >= 5:
                def _names(items, k=8):
                    out = []
                    for it in items[:k]:
                        if isinstance(it, bytes):
                            out.append(it.decode('utf-8', errors='replace'))
                        else:
                            out.append(str(it))
                    if len(items) > k:
                        out.append(f'… +{len(items) - k} more')
                    return out
                print(
                    f'[repo-status] n={n} '
                    f'staged_add={len(st.staged.get("add", []))} '
                    f'staged_mod={len(st.staged.get("modify", []))} '
                    f'staged_del={len(st.staged.get("delete", []))} '
                    f'unstaged={len(st.unstaged)} '
                    f'untracked={len(st.untracked)} '
                    f'untracked_head={_names(list(st.untracked))!r} '
                    f'unstaged_head={_names(list(st.unstaged))!r}',
                    file=sys.stderr, flush=True)
        except Exception:
            n = 0

        wan_unshared = _wan_unshared(repo, branch)

        return branch, remote_url, n, wan_unshared
    except Exception:
        return None
    finally:
        # This runs on EVERY status poll (~10 s cadence per open
        # peer). Pre-0.54.1 the Repo was never closed; each call
        # left the project's pack/index fds open until GC got
        # around to it, which on the 2026-07-10 karlap desktop
        # exhausted the fd table in under a day (EMFILE incident —
        # see agenda/daemon_fd_leak_emfile_hardening.md).
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass


_walk_count_last_logged = {}


def _walk_count_log(repo, tag, msg):
    """Rate-limited diagnostic emit for the three sync-status walker
    helpers (``_wan_unshared``, ``_lan_unshared``, ``_at_risk``).
    Picker polls status every few seconds; without rate-limiting
    we'd log a dozen lines per minute per project at steady state.
    Cache last-emitted text per (working_dir, tag); print only on
    change. The ``tag`` argument distinguishes the three callers
    (``wan-unshared`` / ``lan-unshared`` / ``at-risk``).
    """
    try:
        key = (repo.path, tag)
    except Exception:
        key = ('', tag)
    if _walk_count_last_logged.get(key) == msg:
        return
    _walk_count_last_logged[key] = msg
    print(f'[{tag}] {key[0]!r}: {msg}',
          file=sys.stderr, flush=True)


def _origin_topic_ref_tips(repo):
    """SHA tips of every locally-cached
    ``refs/remotes/origin/azt-pending-*`` ref — the per-device topic
    branches a chunked push uploads history to. Commits reachable from
    these tips have their bytes ON github (durable) even before the
    final merge into main, so the sync counters treat them as shared.
    The local mirror of our own topic ref is advanced after every
    successful chunk (``_push_chunked_to_ref``), which is what makes
    ``_wan_unshared`` tick down during an in-flight upload. Returns a
    list of SHA bytes; empty on any failure (OK-on-uncertainty)."""
    tips = []
    try:
        prefix = b'refs/remotes/origin/azt-pending-'
        for ref, sha in repo.get_refs().items():
            if ref.startswith(prefix) and sha:
                tips.append(sha)
    except Exception:
        pass
    return tips


def _main_merged(repo, branch):
    """True when the local branch tip is fully contained in
    ``refs/remotes/origin/<branch>`` — every local commit is on
    github's main, so the project is genuinely backed up, not merely
    uploaded to a topic ref awaiting merge. This is the gate for the
    "OK" sync state: a project whose bytes are all on a topic ref but
    not yet merged reads ``WAN-0`` (nothing left to upload) but is NOT
    "OK". Returns False when no origin URL is configured (LAN-only —
    never github-merged), when main was never fetched, or on any
    uncertainty (never falsely claims "merged")."""
    try:
        url_str = ''
        try:
            url = repo.get_config().get((b'remote', b'origin'), b'url')
            url_str = url.decode('utf-8', 'replace').strip()
        except Exception:
            url_str = ''
        if not url_str:
            return False
        local_ref = b'refs/heads/' + branch.encode()
        remote_ref = b'refs/remotes/origin/' + branch.encode()
        try:
            local_sha = repo.refs[local_ref]
            remote_sha = repo.refs[remote_ref]
        except KeyError:
            return False
        if local_sha == remote_sha:
            return True
        return bool(_is_ancestor(repo, local_sha, remote_sha))
    except Exception:
        return False


def _wan_unshared(repo, branch):
    """Count commits on ``refs/heads/<branch>`` whose bytes are NOT
    yet on github — i.e. not reachable from
    ``refs/remotes/origin/<branch>`` NOR from any per-device topic ref
    (``refs/remotes/origin/azt-pending-*``). Counting topic refs as
    "on github" (0.53.3) lets the count tick DOWN as a chunked
    topic-push uploads history, instead of staying pinned at the full
    divergence until the final merge. A commit is durable on github
    the moment its chunk lands on the topic ref; the subsequent merge
    to main is a cheap last step gated separately by ``_main_merged``
    (so the count can reach 0 — "nothing left to upload" — while the
    project is not yet "OK"/merged). Any failure (detached HEAD, no
    remote ref cached, walker error) returns 0 — the indicator's
    contract is "OK on uncertainty."

    Special case for LAN-only projects (since 0.46.1): when there
    is NO ``origin`` remote configured at all (vs. "configured but
    never pushed"), count all commits reachable from HEAD. Every
    local commit IS unpublished by definition when github isn't a
    backup target. Powers the ``WAN-N`` rendering for LAN-only
    projects per ``CLIENT_INTEGRATION.md`` § 17b — without this,
    ``wan_unshared`` stayed 0 forever and the friction signal for
    "no github backup" never appeared.

    The "no origin remote" branch is distinguishable from "origin
    configured but never pushed" by reading ``.git/config``: if
    ``remote.origin.url`` is absent (or empty), we know there's
    nowhere to push, so HEAD ancestry IS the count. Since 0.52.3,
    the "url exists but tracking ref doesn't" case also walks from
    HEAD — pre-0.52.3 it returned 0 (OK-on-uncertainty), which
    masked the "every fetch fails (404 / auth missing)" subcase as
    "all caught up" on the picker even when every commit was at
    risk via github.

    Renamed from ``_count_commits_ahead`` in v0.47.0 to match the
    new wire field name. Semantics unchanged.

    Each call emits one rate-limited ``[wan-unshared]`` line
    showing which branch fired and the SHAs involved; the line
    only prints when the output changes from the last value.
    """
    try:
        local_ref = b'refs/heads/' + branch.encode()
        remote_ref = b'refs/remotes/origin/' + branch.encode()
        try:
            local_sha = repo.refs[local_ref]
        except KeyError:
            _walk_count_log(repo, 'wan-unshared',
                f'branch={branch!r}: no local ref → 0')
            return 0
        # URL check first (0.50.48). Pre-0.50.48 we branched on the
        # tracking-ref existence and only checked the URL when the
        # tracking ref was absent. That misclassified the orphan-
        # tracking-ref case: a project that previously had origin
        # configured, had the URL stripped (e.g., by
        # ``strip_lan_origin_if_present``), but kept its
        # ``refs/remotes/origin/<branch>`` ref. Without an origin
        # URL the tracking ref is meaningless — it points at where
        # the origin USED to be — yet ``_wan_unshared`` would walk
        # excluding it and report a low count, making a LAN-only
        # project look "mostly synced to github" when github isn't
        # configured at all. Field-reported 2026-06-05: tablet
        # said WAN-302, phone said WAN-17 for the same SHA on the
        # same project because phone had an orphan
        # ``refs/remotes/origin/main`` from a previous LAN clone.
        # Read URL first; empty URL = case (b) regardless of
        # what tracking refs are lying around.
        url_str = ''
        try:
            url = repo.get_config().get(
                (b'remote', b'origin'), b'url')
            try:
                url_str = url.decode('utf-8', 'replace').strip()
            except Exception:
                url_str = ''
        except KeyError:
            url_str = ''
        if not url_str:
            # Case (b): no origin URL (or empty URL = "half-
            # stripped", treat the same). Count from HEAD. Any
            # ``refs/remotes/origin/*`` refs present are orphans
            # of a previously-configured origin and must be
            # ignored — see ``strip_lan_origin_if_present`` for
            # the cleanup that prevents future accumulation.
            try:
                walker = repo.get_walker(include=[local_sha])
                n = sum(1 for _ in walker)
                _walk_count_log(repo, 'wan-unshared',
                    f'branch={branch!r} '
                    f'local={local_sha[:12]!r}: '
                    f'no origin URL (LAN-only) → '
                    f'walk-from-HEAD = {n}')
                return n
            except Exception as ex:
                _walk_count_log(repo, 'wan-unshared',
                    f'branch={branch!r} '
                    f'local={local_sha[:12]!r}: '
                    f'walk-from-HEAD raised {ex!r} → 0')
                return 0
        # Origin URL exists. Build the "known on github" exclude set:
        # the main tracking ref (when present) PLUS every per-device
        # topic ref. Commits reachable from a topic ref are already
        # uploaded to github (durable) even though they haven't merged
        # into main yet — excluding the topic tips is what lets the
        # count tick down during a chunked topic-push. 0.53.3.
        exclude = []
        try:
            exclude.append(repo.refs[remote_ref])
        except KeyError:
            # Case (a): origin configured but no main tracking ref
            # (never-fetched, or every fetch fails — 404 / auth). Any
            # topic refs from our own uploads still count below, so a
            # partial upload keeps ticking down; if there are none
            # either, we fall through to the honest walk-from-HEAD.
            pass
        exclude.extend(_origin_topic_ref_tips(repo))
        if not exclude:
            # Nothing known on github at all — honest walk-from-HEAD
            # (the whole history is unpublished; the transient
            # never-fetched case self-heals once the first fetch or
            # topic-push lands a ref). Pre-0.52.3 collapsed this to 0
            # (OK-on-uncertainty), masking the always-failing case as
            # "OK +N" on the picker.
            try:
                walker = repo.get_walker(include=[local_sha])
                n = sum(1 for _ in walker)
                _walk_count_log(repo, 'wan-unshared',
                    f'branch={branch!r} local={local_sha[:12]!r}: '
                    f'origin URL configured ({url_str[:48]}…) + no '
                    f'main/topic refs → walk-from-HEAD = {n}')
                return n
            except Exception as ex:
                _walk_count_log(repo, 'wan-unshared',
                    f'branch={branch!r} local={local_sha[:12]!r}: '
                    f'walk-from-HEAD raised {ex!r} → 0')
                return 0
        if local_sha in exclude:
            _walk_count_log(repo, 'wan-unshared',
                f'branch={branch!r} local={local_sha[:12]!r}: '
                f'local tip already on github → 0')
            return 0
        try:
            walker = repo.get_walker(
                include=[local_sha], exclude=exclude)
            n = sum(1 for _ in walker)
            _walk_count_log(repo, 'wan-unshared',
                f'branch={branch!r} local={local_sha[:12]!r} '
                f'exclude={len(exclude)} github ref(s) → {n}')
            return n
        except Exception as ex:
            _walk_count_log(repo, 'wan-unshared',
                f'branch={branch!r}: exclude-walk raised '
                f'{ex!r} → 0')
            return 0
    except Exception as ex:
        try:
            _walk_count_log(repo, 'wan-unshared',
                f'outer raised {ex!r} → 0')
        except Exception:
            pass
        return 0


def _peer_sync_row(repo, head, langcode, peer_entry, count_limit):
    """One status row for (this peer, this project). Returns a dict:
    ``{peer_id, device_name, langcode, to_send, to_send_known,
    capped, incoming}``. See ``lan_peer_sync_rows`` for the contract.
    Cheap on the steady path: coverage == HEAD short-circuits to
    to_send=0 / incoming=False with no walk."""
    pid = peer_entry.get('peer_id', '') or ''
    name = peer_entry.get('device_name', '') or ''
    main_hex = (peer_entry.get('last_seen_main') or {}).get(langcode, '')
    cov_hex = (peer_entry.get('last_covered_local') or {}).get(langcode, '')

    def _held(hexsha):
        if not hexsha:
            return None
        try:
            b = hexsha.encode('ascii')
        except Exception:
            return None
        return b if b in repo.object_store else None

    # Outbound: commits from our HEAD the peer doesn't cover. Coverage
    # = their main when we hold it, else the last commit we confirmed
    # delivered. Neither held ⇒ we can't count (to_send_known False).
    coverage = _held(main_hex) or _held(cov_hex)
    to_send, to_send_known, capped = 0, True, False
    if coverage is None:
        to_send_known = False
    elif coverage != head:
        to_send = _count_commits_ahead(repo, coverage, head, cap=count_limit)
        if to_send >= count_limit:
            capped = True

    # Inbound: their main isn't reachable from our HEAD ⇒ they hold
    # commits we don't. Count unknown by design (we may not hold them).
    # Two data-safety-distinct cases (0.54.50):
    #   incoming_held=True  — their tip IS in our object store, just not
    #     merged into HEAD ('to merge'). Their bytes are already safe on
    #     our disk; the only outstanding thing is OUR merge work. Not a
    #     "need the peer to talk to us" state.
    #   incoming_held=False — their tip is NOT in our store: data that
    #     lives only on their device (their unmerged commits, or a merge
    #     commit THEY made) and that we must still receive to be safe.
    # Whoever merges first produces a commit the other must fetch, so a
    # held/unmerged pair flips to not-held the instant the peer merges.
    incoming = False
    incoming_held = False
    if main_hex:
        try:
            main_b = main_hex.encode('ascii')
        except Exception:
            main_b = None
        if main_b and main_b != head:
            if main_b in repo.object_store:
                incoming = not _is_ancestor(repo, main_b, head)
                incoming_held = incoming   # held, just not merged
            else:
                incoming = True   # we don't even hold their tip

    return {
        'peer_id': pid,
        'device_name': name,
        'langcode': langcode,
        'to_send': to_send,
        'to_send_known': to_send_known,
        'capped': capped,
        'incoming': incoming,
        # True ⇒ 'awaiting merge' (we hold their bytes, unmerged);
        # False + incoming ⇒ 'incoming' (data still only on the peer).
        'incoming_held': incoming_held,
        # When we last authenticated a handshake with this peer — the
        # "as of" for an 'up to date' judgment, which is a recorded
        # memory, not a live confirmation (0.54.49).
        'last_seen_at': peer_entry.get('last_seen_at', '') or '',
    }


_peer_sync_cache = {'rows': [], 'computed_at': 0.0, 'dirty': True}
_PEER_SYNC_MIN_RECOMPUTE_S = 5.0    # don't re-walk more often than this
_PEER_SYNC_MAX_STALE_S = 30.0       # re-walk at least this often (safety)


def invalidate_peer_sync():
    """Mark the peer-sync board stale so the NEXT read recomputes it.
    Called at the events that change the board — a commit, a LAN
    delivery, a pairing — so the expensive git walk runs on CHANGE, not
    on the UI's timer (the poll then just reads the cached value). See
    ``lan_peer_sync_rows``. Cheap; safe to over-call."""
    _peer_sync_cache['dirty'] = True


def lan_peer_sync_rows(count_limit=100000):
    """Cached read of the peer-sync board (Tier A, 2a). Recomputes the
    per-peer git walk ONLY when invalidated by a change event
    (``invalidate_peer_sync``) — and then at most once per
    ``_PEER_SYNC_MIN_RECOMPUTE_S`` (so a commit storm can't spin it) —
    with a ``_PEER_SYNC_MAX_STALE_S`` backstop so a missed invalidation
    still refreshes within ~30 s. The UI polls this freely; when nothing
    changed it's a dict read, no git work (the fix for the 2.5 s poll
    that ANR'd the daemon; and it matches the 'changes arrive with the
    changes' model — the walk is event-driven, not clock-driven)."""
    import time
    now = time.time()
    st = _peer_sync_cache
    age = now - st['computed_at']
    if (st['dirty'] and age >= _PEER_SYNC_MIN_RECOMPUTE_S) \
            or age >= _PEER_SYNC_MAX_STALE_S:
        st['rows'] = _compute_peer_sync_rows(count_limit)
        st['computed_at'] = now
        st['dirty'] = False
    return list(st['rows'])


def _compute_peer_sync_rows(count_limit=100000):
    """Per-peer × per-shared-project sync status for the settings
    overlay (the "where do I stand with my peers" board, Tier A).

    One dict per (paired peer, project that peer shares):
        peer_id, device_name, langcode,
        to_send        — commits our HEAD has that the peer doesn't
                          cover (0 = nothing to send); capped at
                          *count_limit*,
        to_send_known  — False when we can't compute it (no usable
                          coverage commit for the peer) → UI shows '?',
        capped         — True if to_send hit the cap (show 'N+'),
        incoming       — True when the peer holds commits we don't
                          (count unknown by design).

    Read-only, never raises (→ [] on failure), and fd-safe: every repo
    is opened inside ``_track_opened_repos`` and auto-closed at scope
    exit. Cheap on the steady path — a peer whose coverage == our HEAD
    does no walk."""
    from . import projects as _projects
    from . import peers as _peers
    from dulwich import porcelain
    rows = []
    try:
        peer_list = _peers.list_peers() or []
    except Exception:
        peer_list = []
    if not peer_list:
        return rows
    try:
        all_projects = _projects.list_all() or []
    except Exception:
        return rows
    with _track_opened_repos():
        for p in all_projects:
            langcode = (getattr(p, 'langcode', '') or '').strip()
            wd = (getattr(p, 'working_dir', '') or '').strip()
            if not langcode or not wd:
                continue
            sharers = [pe for pe in peer_list
                       if langcode in (pe.get('shared_projects') or [])]
            if not sharers:
                continue
            repo = _get_repo(wd)
            if repo is None:
                continue
            try:
                branch = porcelain.active_branch(repo).decode(
                    'utf-8', errors='replace')
            except Exception:
                branch = 'main'
            try:
                head = repo.refs[b'refs/heads/' + branch.encode()]
            except Exception:
                try:
                    head = repo.head()
                except Exception:
                    continue
            for pe in sharers:
                try:
                    rows.append(_peer_sync_row(
                        repo, head, langcode, pe, count_limit))
                except Exception as ex:
                    print(f'[peer-sync] row failed for '
                          f'{pe.get("peer_id","")[:8]!r}/{langcode!r}: '
                          f'{ex!r}', file=sys.stderr, flush=True)
    # Sort so each peer's projects sit together (device name, then
    # langcode); unnamed peers last.
    rows.sort(key=lambda r: ((r['device_name'] or '~').lower(),
                             r['langcode']))
    return rows


def _peer_exclude_shas(repo, langcode):
    """Build the walker exclude list from paired peers' coverage
    records: each peer's ``last_seen_main`` when we HOLD that
    commit, else its ``last_covered_local`` (one of our own
    commits, verified delivered — always holdable). Returns
    ``(excludes, n_pairs, n_fallback, n_unusable)``.

    Pre-0.54.5 the walkers excluded ``last_seen_main`` blindly; a
    peer head we never fetched made ``repo.get_walker`` raise
    ``MissingCommitError`` and the helpers returned 0 —
    OK-on-uncertainty inverted into "all shared" over pending
    local commits (field catch 2026-07-11: phone offline with an
    unfetched head, six fresh desktop commits, indicator LANOK).
    With the fallback, that case reads "N commits since the last
    CONFIRMED coverage" instead."""
    from . import peers as _peers
    pairs = _peers.peer_coverage_for(langcode)
    excludes = []
    n_fallback = 0
    n_unusable = 0
    for main_hex, covered_hex in pairs:
        try:
            main_b = main_hex.encode('ascii') if main_hex else None
        except Exception:
            main_b = None
        if main_b is not None and main_b in repo.object_store:
            excludes.append(main_b)
            continue
        try:
            cov_b = (covered_hex.encode('ascii')
                     if covered_hex else None)
        except Exception:
            cov_b = None
        if cov_b is not None and cov_b in repo.object_store:
            excludes.append(cov_b)
            n_fallback += 1
        else:
            n_unusable += 1
    return excludes, len(pairs), n_fallback, n_unusable


def _lan_unshared(repo, branch, langcode):
    """Count commits reachable from ``refs/heads/<branch>`` that
    are NOT reachable from any paired-and-sharing peer's
    ``last_seen_main`` for *langcode*. Returns 0 when no peers
    are paired ("nothing to be behind on" — see
    [[sync-status-state-frequencies]] for the convention).

    Added in v0.47.0 as the LAN-side counterpart to ``_wan_unshared``.
    Together with ``_at_risk`` they drive the 5-state sync
    indicator per CLIENT_INTEGRATION.md § 17b.

    Failure modes return 0 (OK-on-uncertainty), matching the
    sibling helpers."""
    try:
        local_ref = b'refs/heads/' + branch.encode()
        try:
            local_sha = repo.refs[local_ref]
        except KeyError:
            _walk_count_log(repo, 'lan-unshared',
                f'branch={branch!r}: no local ref → 0')
            return 0
        try:
            excludes, n_pairs, n_fallback, n_unusable = \
                _peer_exclude_shas(repo, langcode)
        except Exception as ex:
            _walk_count_log(repo, 'lan-unshared',
                f'branch={branch!r}: peer coverage raised '
                f'{ex!r} → 0')
            return 0
        if n_pairs == 0:
            # No paired peers with any observation for this project
            # — the LAN channel has no "expected destination" to be
            # behind on. Convention: 0, so the renderer doesn't
            # show "LAN-N" for a user who hasn't paired anyone yet.
            _walk_count_log(repo, 'lan-unshared',
                f'branch={branch!r} local={local_sha[:12]!r}: '
                f'no paired peers → 0')
            return 0
        try:
            # ``excludes`` may be EMPTY here (peers exist but none
            # of their heads is in our store and no delivery was
            # ever confirmed): the honest answer is the full
            # walk-from-HEAD count — nothing is confirmed shared —
            # NOT the old OK-on-uncertainty 0.
            walker = repo.get_walker(
                include=[local_sha], exclude=excludes)
            n = sum(1 for _ in walker)
            _walk_count_log(repo, 'lan-unshared',
                f'branch={branch!r} local={local_sha[:12]!r} '
                f'peers={n_pairs}: walk excluding '
                f'{len(excludes)} covered'
                + (f' ({n_fallback} via covered-local fallback)'
                   if n_fallback else '')
                + (f' ({n_unusable} peer(s) unusable — counting '
                   f'as uncovered)' if n_unusable else '')
                + f' → {n}')
            return n
        except Exception as ex:
            _walk_count_log(repo, 'lan-unshared',
                f'branch={branch!r}: walk raised {ex!r} → 0')
            return 0
    except Exception as ex:
        try:
            _walk_count_log(repo, 'lan-unshared',
                f'outer raised {ex!r} → 0')
        except Exception:
            pass
        return 0


def _at_risk(repo, branch, langcode):
    """Count commits reachable from ``refs/heads/<branch>`` that
    are NOT reachable from EITHER origin tracking refs OR any
    paired peer's ``last_seen_main`` — i.e., the set-intersection
    of ``_wan_unshared`` and ``_lan_unshared`` as commit sets.

    Returns 0 when no peers are paired (matches the
    ``_lan_unshared`` convention: at_risk ≤ lan_unshared, so
    lan=0 forces at_risk=0). This prevents projects with no
    paired peers from rendering in state E (both behind, at-risk)
    just because they have no LAN destinations to be behind on.

    Added in v0.47.0. Zero in all states except state E (both
    channels behind on the same commits). See
    CLIENT_INTEGRATION.md § 17b."""
    try:
        local_ref = b'refs/heads/' + branch.encode()
        try:
            local_sha = repo.refs[local_ref]
        except KeyError:
            return 0
        # Peer check first: if no peers, at_risk = 0 by convention.
        try:
            peer_excludes, n_pairs, n_fallback, n_unusable = \
                _peer_exclude_shas(repo, langcode)
        except Exception:
            peer_excludes, n_pairs, n_fallback, n_unusable = \
                [], 0, 0, 0
        if n_pairs == 0:
            _walk_count_log(repo, 'at-risk',
                f'branch={branch!r} local={local_sha[:12]!r}: '
                f'no paired peers → 0 (lan_unshared convention)')
            return 0
        # Combine peer coverage SHAs and origin tracking refs into
        # one exclude set. The walker counts commits not reachable
        # from ANY of them. Same covered-local fallback semantics
        # as ``_lan_unshared`` — an unfetched peer head degrades to
        # its last confirmed coverage, never to "covers everything".
        excludes = list(peer_excludes)
        for ref in (b'refs/remotes/origin/main',
                    b'refs/remotes/origin/master'):
            try:
                excludes.append(repo.refs[ref])
            except KeyError:
                continue
        # Topic refs are on github too (0.53.3): commits parked on a
        # per-device topic branch during a chunked push are durable on
        # github, so they are not "at risk" even before merge. Mirrors
        # the same exclusion in ``_wan_unshared``.
        excludes.extend(_origin_topic_ref_tips(repo))
        try:
            walker = repo.get_walker(
                include=[local_sha], exclude=excludes)
            n = sum(1 for _ in walker)
            _walk_count_log(repo, 'at-risk',
                f'branch={branch!r} local={local_sha[:12]!r} '
                f'excludes={len(excludes)} '
                f'(peers={n_pairs}'
                + (f', {n_fallback} covered-local fallback'
                   if n_fallback else '')
                + (f', {n_unusable} unusable' if n_unusable else '')
                + f'): walk excluding union → {n}')
            return n
        except Exception as ex:
            _walk_count_log(repo, 'at-risk',
                f'branch={branch!r}: walk raised {ex!r} → 0')
            return 0
    except Exception as ex:
        try:
            _walk_count_log(repo, 'at-risk',
                f'outer raised {ex!r} → 0')
        except Exception:
            pass
        return 0


def init_repo(project_dir, remote_url, username, token,
              branch='main', contributor_name='',
              rollback_origin_on_create_fail=True):
    """Initialize a git repo, commit everything, set remote, push.
    Returns a Result.

    Pre-0.40 ``contributor_name`` defaulted to the literal
    ``'Recorder'`` so a missing arg silently produced
    ``Recorder <recorder@device>`` commits. As of 0.40 the daemon's
    endpoints refuse the call upstream when contributor is unset
    (``S.CONTRIBUTOR_UNSET``); the default here is empty for
    test convenience but production callers always pass a real
    name.

    *rollback_origin_on_create_fail* (default True): when
    ``_ensure_remote_repo`` fails, strip
    ``[remote "origin"]`` from ``.git/config`` so the picker's
    publish-row gate sees no remote and shows the Publish button
    for a manual retry. The picker-initiated path keeps this on
    (the user clicking Publish wants the button to come back if
    the attempt fails). The auto-retry path
    (``reconcile_publish_state_on_startup``) passes ``False`` so
    a transient offline / outage / missing-creds situation at
    boot doesn't expose a phantom Publish button on a project
    the user already committed to. State changes only on a
    successful ``PUSHED``; otherwise the working tree is left
    exactly as the user last saw it, and the next daemon
    startup retries silently. See ``docs/Publish_errors.md`` for
    the rationale."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            with _track_opened_repos():
                return _init_repo_locked(
                    project_dir, remote_url, username, token,
                    branch, contributor_name,
                    rollback_origin_on_create_fail=rollback_origin_on_create_fail)
    except LockTimeout:
        print(f'[publish] init_repo BUSY: project_lock timeout on '
              f'{project_dir!r} — another writer (sync drain, lan '
              f'merge) holds the lock; peer will see BUSY result',
              file=sys.stderr, flush=True)
        return _busy_result(project_dir)


def _strip_origin_section(repo, project_dir):
    """Remove ``[remote "origin"]`` from ``<project_dir>/.git/config``
    on a publish that aborted before push. Caller holds
    ``project_lock``. Idempotent — no-op when there's no origin.

    Mirror of the post-failure rollback in ``_init_repo_locked``:
    keeps the picker's ``_refresh_publish_row`` gate honest by
    reverting the local-side mutation that happens just before
    the failing ``_ensure_remote_repo`` call. See 0.50.52."""
    config = repo.get_config()
    try:
        config.get((b'remote', b'origin'), b'url')
    except KeyError:
        return
    try:
        config.remove_section((b'remote', b'origin'))
    except (KeyError, AttributeError):
        # Older dulwich: no remove_section. Clearing the URL is
        # enough for the picker gate, which reads the URL value.
        try:
            config.set((b'remote', b'origin'), b'url', b'')
        except Exception:
            return
    config.write_to_path()
    print(f'[publish] stripped .git/config [remote "origin"] '
          f'from {project_dir!r}',
          file=sys.stderr, flush=True)


def _init_repo_locked(project_dir, remote_url, username, token,
                      branch, contributor_name,
                      rollback_origin_on_create_fail=True):
    from dulwich import porcelain
    result = Result()

    print(f'[publish] init_repo begin dir={project_dir!r} '
          f'remote={remote_url!r} branch={branch!r} '
          f'username={username!r}',
          file=sys.stderr, flush=True)

    repo = _get_repo(project_dir)
    if repo is None:
        repo = porcelain.init(project_dir)
        result.add(S.INITIALIZED)
    else:
        result.add(S.ALREADY_INITIALIZED)

    gitignore = os.path.join(project_dir, '.gitignore')
    if not os.path.exists(gitignore):
        with open(gitignore, 'w') as fh:
            fh.write('__pycache__/\n*.pyc\n.buildozer/\nenv/\n.DS_Store\n'
                     'image_cache/\n.azt_atomic_pending/\n'
                     '.azt_atomic_orphans/\n')
        result.add(S.GITIGNORE_CREATED)

    _stage_all(repo, project_dir)
    # Mirror ``_commit_step_locked``'s has_staged guard so re-clicking
    # Publish on a quiescent project (everything already committed
    # via the normal commit flow) doesn't trip ``porcelain.commit``'s
    # "nothing to commit" exception, route through
    # ``_surface_commit_failure``, and falsely bump the persistent
    # ``commit_failure_count`` (eventually surfacing a misleading
    # ``COMMIT_REPEATEDLY_FAILED`` data-loss-class toast). Publish
    # has to be safely re-runnable for the user-clicks-again retry
    # path to work — see 0.50.52 rationale.
    st = porcelain.status(repo)
    has_staged = any(st.staged.get(k) for k in ('add', 'modify', 'delete'))
    if has_staged:
        author = _default_author(contributor_name)
        committer = _app_committer()
        try:
            sha = porcelain.commit(
                repo,
                message=_enc(f'Initial commit by {contributor_name}'),
                author=author, committer=committer,
            )
            sha_str = sha[:8].decode() if isinstance(sha, bytes) else str(sha)[:8]
            result.add(S.COMMITTED, sha=sha_str)
            _clear_commit_failure_count(project_dir)
        except Exception as exc:
            _surface_commit_failure(result, project_dir, exc)
    else:
        result.add(S.NOTHING_TO_COMMIT)

    try:
        existing = repo.get_config().get((b'remote', b'origin'), b'url').decode()
        if existing != remote_url:
            config = repo.get_config()
            config.set((b'remote', b'origin'), b'url', _enc(remote_url))
            config.write_to_path()
            result.add(S.REMOTE_UPDATED, url=remote_url)
        else:
            result.add(S.REMOTE_UNCHANGED, url=existing)
    except KeyError:
        porcelain.remote_add(repo, 'origin', remote_url)
        result.add(S.REMOTE_SET, url=remote_url)

    desired_ref = _enc(f'refs/heads/{branch}')
    try:
        head_ref = repo.refs.get_symrefs().get(b'HEAD', b'')
        if head_ref != desired_ref:
            head_sha = repo.head()
            repo.refs[desired_ref] = head_sha
            repo.refs.set_symbolic_ref(b'HEAD', desired_ref)
    except Exception:
        pass

    ok, create_status = _ensure_remote_repo(remote_url, username, token)
    if create_status is not None:
        result.statuses.append(create_status)
    if not ok:
        # Rollback: the github-side create failed, so we have no
        # remote — but ``.git/config`` got an ``[remote "origin"]``
        # section pointing at a non-existent URL. Leave it in place
        # and ``project_status.remote_url`` reads non-empty, the
        # picker's ``_refresh_publish_row`` gate hides Publish, and
        # the user can't retry. Strip the section so the next
        # picker poll sees no remote and shows Publish again. The
        # registry mirror is cleared by ``_h_init_project`` on the
        # same REMOTE_CREATE_FAILED signal. Only on hard create
        # failure — on OWNER_MISMATCH (bet on collaborator access)
        # or push failure (remote exists, scheduler drain will
        # retry) we keep the URL.
        #
        # Auto-retry mode (``rollback_origin_on_create_fail=False``,
        # set by ``reconcile_publish_state_on_startup``) skips the
        # strip. The rationale: an offline / outage / missing-creds
        # boot would otherwise expose a phantom Publish button on a
        # project the user already committed to. Leaving the
        # working tree unchanged means the next daemon startup
        # retries the publish silently; only a successful PUSHED
        # transitions the project out of the mismatch state.
        if rollback_origin_on_create_fail:
            try:
                _strip_origin_section(repo, project_dir)
            except Exception as ex:
                print(f'[publish] origin-strip after create-fail '
                      f'raised {ex!r}',
                      file=sys.stderr, flush=True)
            print(f'[publish] init_repo aborting before push: '
                  f'codes={result.codes()}; stripped .git/config '
                  f'origin for retry',
                  file=sys.stderr, flush=True)
        else:
            print(f'[publish] init_repo aborting before push: '
                  f'codes={result.codes()}; .git/config origin '
                  f'preserved (auto-retry mode)',
                  file=sys.stderr, flush=True)
        return result

    try:
        porcelain.push(
            repo, wan_url(remote_url),
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        result.add(S.PUSHED, url=remote_url, branch=branch)
    except Exception as exc:
        print(f'[publish] push to {remote_url!r} failed: {exc!r}',
              file=sys.stderr, flush=True)
        _add_push_failure(result, exc)

    print(f'[publish] init_repo done: codes={result.codes()}',
          file=sys.stderr, flush=True)
    return result


class _ProgressStream(io.RawIOBase):
    """Captures git protocol progress lines and forwards to a callback.

    Dulwich writes progress messages like ``Receiving objects:  75% (30/40)\\r``
    to *errstream*. This stream buffers them and calls *on_progress(line)*
    on each complete line (delimited by ``\\r`` or ``\\n``).
    """

    def __init__(self, on_progress=None):
        self._callback = on_progress
        self._buf = b''

    def write(self, data):
        if not data:
            return 0
        self._buf += data
        while b'\r' in self._buf or b'\n' in self._buf:
            ri = self._buf.find(b'\r')
            ni = self._buf.find(b'\n')
            if ri == -1:
                idx = ni
            elif ni == -1:
                idx = ri
            else:
                idx = min(ri, ni)
            line = self._buf[:idx].decode('utf-8', errors='replace').strip()
            self._buf = self._buf[idx + 1:]
            if line and self._callback:
                if ':' in line:
                    phase, _, detail = line.partition(':')
                    line = f'{phase}:\n{detail.strip()}'
                self._callback(line)
        return len(data)

    def writable(self):
        return True


def clone_repo(remote_url, dest_dir, username, token, on_progress=None):
    """
    Clone remote_url into dest_dir.
    Returns (lift_path_or_None, Result).
    *on_progress* is called with raw status strings from the git protocol.
    """
    _ensure_ssl()
    try:
        with project_lock(dest_dir):
            return _clone_repo_locked(remote_url, dest_dir,
                                      username, token, on_progress)
    except LockTimeout:
        return None, _busy_result(dest_dir)


def _clone_repo_locked(remote_url, dest_dir, username, token, on_progress):
    from dulwich import porcelain
    result = Result()

    errstream = _ProgressStream(on_progress) if on_progress else io.BytesIO()

    if os.path.exists(dest_dir):
        # A real prior clone (has .git AND a .lift): reopen it rather
        # than wipe — re-cloning a URL that was already here silently
        # discarded any unpushed local work (field 2026-07-17). Users
        # who truly want a fresh download delete the folder first.
        # A .git WITHOUT a .lift is debris (failed or empty-repo
        # clone) and must stay wipeable, or retrying after the owner
        # publishes could never succeed.
        if os.path.isdir(os.path.join(dest_dir, '.git')):
            lift_path = _find_lift(dest_dir)
            if lift_path:
                result.add(S.CLONE_REUSED_EXISTING, dir=dest_dir)
                result.add(S.LIFT_FOUND,
                           file=os.path.basename(lift_path))
                return lift_path, result
        import shutil
        shutil.rmtree(dest_dir)
    wan = wan_url(remote_url)
    if wan != remote_url:
        print(f'[clone] ssh-shaped URL {remote_url!r}; '
              f'cloning via {wan!r} (daemon auth is https-only)',
              file=sys.stderr, flush=True)
        remote_url = wan
    try:
        os.makedirs(dest_dir, exist_ok=True)
        porcelain.clone(
            remote_url, dest_dir,
            username=username, password=token,
            errstream=errstream,
        )
        result.add(S.CLONED, dir=dest_dir)
    except Exception as exc:
        result.add(S.CLONE_FAILED, error=str(exc))
        return None, result

    lift_path = _find_lift(dest_dir)
    if lift_path:
        result.add(S.LIFT_FOUND, file=os.path.basename(lift_path))
    elif _worktree_has_files(dest_dir):
        result.add(S.LIFT_NOT_FOUND)
    else:
        # Empty clone ≠ bad content: the usual story is the project's
        # first upload never completed (often a permissions failure on
        # the owner's side), so don't tell the user their repo lacks a
        # .lift when it lacks EVERYTHING (field, 2026-07-17).
        result.add(S.REPO_EMPTY)
    return lift_path, result


def pull_repo(project_dir, username, token):
    """Pull (fetch + fast-forward) from origin. Returns Result."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            return _pull_repo_locked(project_dir, username, token)
    except LockTimeout:
        return _busy_result(project_dir)


def _pull_repo_locked(project_dir, username, token):
    from dulwich import porcelain
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        result.add(S.NOT_A_REPO)
        return result
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        result.add(S.NO_REMOTE)
        return result
    try:
        # Remote NAME when possible (see _sync_repo_locked for why),
        # wan-normalized URL when the stored origin is ssh-shaped.
        _pull_origin(repo, username, token)
        result.add(S.PULLED)
    except Exception as exc:
        result.add(S.PULL_FAILED, error=str(exc))
    return result


def commit_and_push_branch(project_dir, username, token, contributor_name):
    """Stage, commit, and push to contrib/<contributor_name>. Returns Result."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            return _commit_and_push_branch_locked(
                project_dir, username, token, contributor_name)
    except LockTimeout:
        return _busy_result(project_dir)


def _commit_and_push_branch_locked(project_dir, username, token,
                                    contributor_name):
    from dulwich import porcelain
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        result.add(S.NOT_A_REPO)
        return result
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        result.add(S.NO_REMOTE)
        return result
    remote_url = wan_url(remote_url)

    safe = (contributor_name.lower()
            .replace(' ', '_').replace('/', '_') or 'contributor')
    branch_name = f'contrib/{safe}'
    branch_ref = _enc(f'refs/heads/{branch_name}')

    try:
        if branch_ref not in repo.refs:
            repo.refs[branch_ref] = repo.head()
        repo.refs.set_symbolic_ref(b'HEAD', branch_ref)
        result.add(S.ON_BRANCH, branch=branch_name)
    except Exception as exc:
        result.add(S.BRANCH_ERROR, error=str(exc))

    _stage_all(repo, project_dir)
    result.add(S.STAGED_ALL)

    author = _default_author(contributor_name)
    committer = _app_committer()
    try:
        porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=committer,
        )
        result.add(S.COMMITTED)
        _clear_commit_failure_count(project_dir)
    except Exception as exc:
        msg = str(exc).lower()
        if 'nothing' in msg or 'empty' in msg or 'no changes' in msg:
            result.add(S.NOTHING_TO_COMMIT)
        else:
            _surface_commit_failure(result, project_dir, exc)

    refspec = _enc(f'refs/heads/{branch_name}:refs/heads/{branch_name}')
    try:
        porcelain.push(
            repo, remote_url, refspec,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        result.add(S.PUSHED, branch=branch_name)
        result.add(S.OPEN_PR)
    except Exception as exc:
        _add_push_failure(result, exc)

    return result


def commit_repo(project_dir, contributor_name):
    """Stage + commit any working-tree changes for *project_dir*.
    No network — push must be requested separately via push_repo
    or sync_repo. Returns a Result with COMMITTED_LOCAL /
    NOTHING_TO_COMMIT / COMMIT_FAILED / COMMIT_REPEATEDLY_FAILED /
    DATA_LOSS_RISK / NOT_A_REPO.

    This is the daemon-side primitive the new commit_project RPC
    routes through. The split from sync_repo lets a peer call
    'commit-on-every-change' cheaply without engaging the network
    layer; push is driven by the connectivity watcher's drain
    instead. See azt_collab_client/CLAUDE.md "Sync flow"."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            with _track_opened_repos():
                return _commit_repo_locked(project_dir, contributor_name)
    except LockTimeout:
        return _busy_result(project_dir)


def _commit_repo_locked(project_dir, contributor_name):
    from dulwich import porcelain
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        # Auto-init recovery: ``create_from_template`` /
        # ``register_project`` pre-0.45.42 created the working_dir
        # without ever running ``porcelain.init``, so every commit
        # NOT_A_REPO'd silently and the user's audio + LIFT writes
        # never entered git history. Detect that state here and
        # initialize on the fly so the next commit succeeds. We hold
        # ``project_lock`` (caller acquired it), so the init can't
        # race with another writer. Safe even on a working_dir that
        # has files: ``porcelain.init`` only creates ``.git/`` next
        # to whatever's there, then ``_stage_all`` below picks up
        # the existing content and commits it as the initial commit.
        if not project_dir or not os.path.isdir(project_dir):
            result.add(S.NOT_A_REPO)
            return result
        try:
            repo = porcelain.init(project_dir)
            result.add(S.INITIALIZED)
            print(f'[commit] {project_dir!r}: auto-init recovery — '
                  f'project had no .git/, created one',
                  file=sys.stderr, flush=True)
        except Exception as ex:
            result.add(S.NOT_A_REPO)
            print(f'[commit] {project_dir!r}: auto-init failed: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            return result
    # Pre-commit absorb of any pending post-receive reset for this
    # project. The lan_listener's reset path queues the langcode if
    # it couldn't acquire ``project_lock`` within 5 s (typically
    # because our own outgoing merge was holding the lock). Without
    # this absorb, ``_stage_all`` below would see the merge files
    # as "missing from working tree" (they're in HEAD but the reset
    # never wrote them to disk) and produce a commit that DELETES
    # them — silently undoing the incoming merge. We hold
    # ``project_lock`` here (reentrant flock — caller acquired it),
    # so we can safely run the same hard-reset-to-HEAD the lan
    # listener would have. See ``lan_listener.has_pending_reset``
    # + ``_add_pending_reset`` / ``_remove_pending_reset`` for the
    # queue mechanics.
    try:
        from . import projects as _projects_mod
        from . import lan_listener as _lan_listener
        langcode = _projects_mod.find_langcode_by_working_dir(
            project_dir)
        if langcode and _lan_listener.has_pending_reset(langcode):
            try:
                head_sha = repo.refs[b'HEAD']
                porcelain.reset(repo, mode='hard',
                                treeish=head_sha)
                _lan_listener._remove_pending_reset(langcode)
                print(f'[commit] {langcode!r}: absorbed pending '
                      f'post-receive reset → HEAD '
                      f'({head_sha[:12].decode()}) before staging',
                      file=sys.stderr, flush=True)
            except Exception as ex:
                # Don't fail the commit on absorb error — log and
                # proceed. Worst case is the ``n_changes`` mismatch
                # surfaces in the commit, which is no worse than
                # the pre-absorb behavior.
                print(f'[commit] {langcode!r}: pending-reset absorb '
                      f'raised {ex!r} — proceeding to stage anyway',
                      file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[commit] pending-reset absorb check raised: {ex!r}',
              file=sys.stderr, flush=True)
    _commit_step_locked(repo, project_dir, contributor_name, result)
    return result


def submit_file(project_dir, rel_path, staged_path, base_sha,
                contributor_name, message=None):
    """Base-aware whole-file write + commit — the desktop A-Z+T save
    primitive (0.53.0; contract in
    azt-collab/agenda/azt_persistence_server_sync.md → to land in
    CLIENT_INTEGRATION.md).

    The caller has serialized the full file to *staged_path* (a
    sibling of the target, so ``os.replace`` stays same-filesystem
    atomic) and declares *base_sha* — the HEAD it loaded / last
    wrote against. Under ``project_lock``:

    - HEAD == base_sha (or no commits yet): ``os.replace`` staged →
      target, commit. The normal case; zero-copy handoff.
    - HEAD != base_sha (a merge landed since the caller's base):
      three-way merge — base = the file's blob at *base_sha*, ours
      = the blob at HEAD, theirs = the staged bytes — via
      ``lift_merge.three_way_merge`` for ``.lift`` targets (theirs
      wins for non-LIFT paths, same last-write-wins the plain
      atomic write had). Merged bytes land atomically; the commit
      is linear (single parent) because "theirs" was never a
      commit, just a working-file state — content converges, and
      the caller must reload before further edits.

    Empty/unknown *base_sha* against an existing HEAD takes the
    divergent path with an empty base (add-add semantics — the
    guards in lift_merge still apply). The staged file is consumed
    (unlinked) on every success path.

    Returns ``(Result, head_sha)``. Codes: ``COMMITTED_LOCAL``
    (with ``head_sha`` param) / ``MERGED_WITH_LOCAL`` (divergent
    path taken; params ``n_conflicts``, ``base_sha``) /
    ``NOTHING_TO_COMMIT`` / ``CONTRIBUTOR_UNSET`` (file bytes still
    land — durability never waits on identity) / ``COMMIT_FAILED``
    / ``BUSY``. Contributor-unset and commit-failure still leave
    the submitted content on disk; the next successful commit
    stages it (same containment as a power cut)."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            with _track_opened_repos():
                return _submit_file_locked(
                    project_dir, rel_path, staged_path, base_sha,
                    contributor_name, message)
    except LockTimeout:
        return _busy_result(project_dir), head_sha_of(project_dir)


def _submit_file_locked(project_dir, rel_path, staged_path, base_sha,
                        contributor_name, message):
    from dulwich import porcelain
    result = Result()
    target = os.path.join(project_dir, rel_path)
    repo = _get_repo(project_dir)
    if repo is None:
        # Same auto-init recovery as ``_commit_repo_locked`` — a
        # registered-but-never-initialized dir must not lose writes.
        try:
            repo = porcelain.init(project_dir)
            result.add(S.INITIALIZED)
        except Exception as ex:
            print(f'[submit_file] {project_dir!r}: auto-init failed: '
                  f'{ex!r}', file=sys.stderr, flush=True)
            # Bytes first, even with no repo: land the content, give
            # up on history for this call.
            os.replace(staged_path, target)
            result.add(S.NOT_A_REPO)
            return result, ''
    try:
        head = repo.refs[b'HEAD']
    except KeyError:
        head = None
    head_str = _sha_str(head) if head else ''
    base_str = (base_sha or '').strip()

    if head is None or (base_str and base_str == head_str):
        # Fast path — nothing landed since the caller's base.
        os.replace(staged_path, target)
    else:
        # Divergent path — HEAD moved past the caller's base.
        theirs_bytes = None
        try:
            with open(staged_path, 'rb') as fh:
                theirs_bytes = fh.read()
        except OSError as ex:
            result.add(S.COMMIT_FAILED, error=f'staged read: {ex}')
            return result, head_str
        ours_sha = _walk_tree(repo, repo[head].tree).get(rel_path)
        if ours_sha is None:
            # HEAD doesn't have the file (renamed away / first write
            # on an adopted tree): fall back to the working tree.
            try:
                with open(target, 'rb') as fh:
                    ours_bytes = fh.read()
            except OSError:
                ours_bytes = b''
        else:
            ours_bytes = _blob_bytes(repo, ours_sha) or b''
        base_bytes = b''
        if base_str:
            try:
                base_commit = repo[base_str.encode('ascii')]
                base_blob_sha = _walk_tree(
                    repo, base_commit.tree).get(rel_path)
                if base_blob_sha is not None:
                    base_bytes = _blob_bytes(repo, base_blob_sha) or b''
            except KeyError:
                # Unknown base (re-clone, GC'd history): empty base →
                # add-add semantics; the truncation guards still hold.
                print(f'[submit_file] {project_dir!r}: base '
                      f'{base_str[:12]!r} not in repo; merging with '
                      f'empty base', file=sys.stderr, flush=True)
        if rel_path.lower().endswith('.lift'):
            # theirs = the just-submitted working file; a new take it
            # references may be on disk but uncommitted → NOW wins
            # (work_dir enables the on-disk plausibility check).
            audio_rec = _audio_recency_resolver(
                repo, head, None, work_dir=project_dir)
            mr = lift_merge.three_way_merge(
                base_bytes, ours_bytes, theirs_bytes, path=rel_path,
                audio_recency=audio_rec)
            merged = mr.merged_bytes
            n_conflicts = len(mr.conflicts)
            for _c in mr.conflicts:
                if lift_merge.is_guard_kind(_c.kind):
                    _write_merge_diagnostic(
                        project_dir, guard_kind=_c.kind,
                        lift_path=rel_path,
                        local_sha=head_str, remote_sha='<staged>',
                        base_sha=base_str,
                        base_bytes=base_bytes, ours_bytes=ours_bytes,
                        theirs_bytes=theirs_bytes,
                        merged_bytes=merged,
                        conflict_fields=_c.fields)
                    break
        else:
            # Non-LIFT: no entry-level merge semantics — theirs wins,
            # exactly what a plain atomic write would have done.
            merged = theirs_bytes
            n_conflicts = 0
        tmp = f'{target}.tmp.{os.getpid()}.{_rand_hex8()}'
        try:
            with open(tmp, 'wb') as fh:
                fh.write(merged)
            os.replace(tmp, target)
        except OSError as ex:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            result.add(S.COMMIT_FAILED, error=f'merged write: {ex}')
            return result, head_str
        try:
            os.unlink(staged_path)
        except OSError:
            pass
        result.add(S.MERGED_WITH_LOCAL,
                   n_conflicts=n_conflicts, base_sha=base_str)
        print(f'[submit_file] {project_dir!r}: base '
              f'{base_str[:12] or "<none>"!r} != HEAD '
              f'{head_str[:12]!r} — merged ({n_conflicts} '
              f'conflict(s))', file=sys.stderr, flush=True)

    if not contributor_name:
        # Bytes are durable on disk; only history waits. Same code
        # the other commit-issuing endpoints use, so the peer's
        # routing (→ set-your-name screen) is uniform.
        result.add(S.CONTRIBUTOR_UNSET)
        return result, head_str
    _commit_step_locked(
        repo, project_dir, contributor_name, result,
        message=message or f'A-Z+T edit by {contributor_name}')
    return result, head_sha_of(project_dir)


def _rand_hex8():
    import secrets
    return secrets.token_hex(8)


def integrate_head_into_working_tree(repo, project_dir):
    """Three-way file-level merge after an incoming receive-pack
    moved HEAD past our pre-pack base, while our working_tree
    holds pending local edits.

    Inputs:
      - base  = HEAD's parent tree  (what we were sitting on top
                of before the receive-pack)
      - ours  = working_tree
      - theirs= HEAD's tree (the just-arrived peer commit)

    Per file:
      - Unchanged in HEAD vs base → leave working_tree alone.
      - Working_tree already matches HEAD → no-op (already
        integrated, e.g. both phones recorded the same audio).
      - Working_tree == base (no local edit) → take HEAD's
        version: write the blob to working_tree.
      - Both sides changed:
        * ``.lift`` paths → ``lift_merge.three_way_merge``;
          merged bytes land in working_tree.
        * Other paths (binary audio, images) → keep ours, log
          loudly. In normal use audio filenames carry a guid +
          timestamp so two phones' recordings don't collide; if
          they DO collide on the same path the safer choice is
          to keep what the local user just produced and let the
          peer re-pull.

    Deletions:
      - Path in base + working_tree but absent in HEAD AND
        working_tree == base → honor the deletion.
      - Path in base + working_tree but absent in HEAD AND
        working_tree != base → keep ours (we modified it after
        theirs deleted; the safe play is to preserve user work).

    Leaves the index untouched. The next ``commit_project``
    stages the merged working_tree on top of HEAD; the resulting
    commit preserves both sides' changes.

    Holds no additional lock — caller (post-receive middleware
    or the commit path) already holds ``project_lock``. Returns
    ``(applied: bool, n_conflicts: int)``. ``applied=False`` means
    the merge bailed (e.g., HEAD has no parent — first commit
    case); caller should fall back to the original behaviour
    (defer or reset-hard).
    """
    from dulwich.object_store import iter_tree_contents
    head_sha = repo.refs[b'HEAD']
    head_commit = repo[head_sha]
    head_tree_sha = head_commit.tree
    if not head_commit.parents:
        # First commit on the branch — nothing to merge against.
        # Caller should fall through to its non-merge branch.
        return False, 0
    base_tree_sha = repo[head_commit.parents[0]].tree
    head_files = {}
    for entry in iter_tree_contents(repo.object_store, head_tree_sha):
        head_files[entry.path] = entry
    base_files = {}
    for entry in iter_tree_contents(repo.object_store, base_tree_sha):
        base_files[entry.path] = entry
    n_conflicts = 0
    n_taken_theirs = 0
    n_merged_lift = 0
    n_kept_ours = 0
    n_deleted_honored = 0
    n_deleted_overridden = 0
    # ours = working tree; a take it references may be uncommitted →
    # NOW wins over theirs' committed take (work_dir on-disk check).
    audio_rec = _audio_recency_resolver(
        repo, head_sha, None, work_dir=project_dir)
    for path, head_entry in head_files.items():
        base_entry = base_files.get(path)
        if base_entry is not None \
                and head_entry.sha == base_entry.sha:
            # Unchanged in HEAD vs base; nothing to integrate.
            continue
        try:
            wt_path = os.path.join(
                project_dir, path.decode('utf-8'))
        except UnicodeDecodeError:
            continue
        try:
            with open(wt_path, 'rb') as fh:
                wt_bytes = fh.read()
            wt_exists = True
        except (FileNotFoundError, IsADirectoryError):
            wt_bytes = b''
            wt_exists = False
        try:
            head_bytes = repo.object_store[head_entry.sha].data
        except KeyError:
            # Pack inconsistency; skip the file rather than
            # crash the merge.
            continue
        base_bytes = b''
        if base_entry is not None:
            try:
                base_bytes = repo.object_store[
                    base_entry.sha].data
            except KeyError:
                base_bytes = b''
        if wt_exists and wt_bytes == head_bytes:
            # Working_tree already matches HEAD; no-op.
            continue
        if not wt_exists or wt_bytes == base_bytes:
            # Local has no edits on this file (or working_tree
            # absent); take theirs.
            try:
                os.makedirs(os.path.dirname(wt_path) or '.',
                            exist_ok=True)
                with open(wt_path, 'wb') as fh:
                    fh.write(head_bytes)
                n_taken_theirs += 1
            except OSError as ex:
                print(f'[post-receive-merge] write {wt_path!r} '
                      f'failed: {ex!r}',
                      file=sys.stderr, flush=True)
            continue
        # Both sides changed.
        if path.endswith(b'.lift'):
            try:
                mr = lift_merge.three_way_merge(
                    base_bytes, wt_bytes, head_bytes,
                    path=path.decode('utf-8', 'replace'),
                    audio_recency=audio_rec)
                with open(wt_path, 'wb') as fh:
                    fh.write(mr.merged_bytes)
                n_merged_lift += 1
                n_conflicts += len(mr.conflicts)
            except Exception as ex:
                print(f'[post-receive-merge] lift_merge on '
                      f'{path!r} raised {ex!r}; keeping ours',
                      file=sys.stderr, flush=True)
                n_kept_ours += 1
        else:
            print(f'[post-receive-merge] both sides changed '
                  f'{path!r} (non-LIFT); keeping ours',
                  file=sys.stderr, flush=True)
            n_kept_ours += 1
    # Deletions: in base+wt but not in HEAD.
    for path, base_entry in base_files.items():
        if path in head_files:
            continue
        try:
            wt_path = os.path.join(
                project_dir, path.decode('utf-8'))
        except UnicodeDecodeError:
            continue
        try:
            with open(wt_path, 'rb') as fh:
                wt_bytes = fh.read()
        except (FileNotFoundError, IsADirectoryError):
            continue
        try:
            base_bytes = repo.object_store[base_entry.sha].data
        except KeyError:
            base_bytes = b''
        if wt_bytes == base_bytes:
            try:
                os.remove(wt_path)
                n_deleted_honored += 1
            except OSError:
                pass
        else:
            n_deleted_overridden += 1
    print(f'[post-receive-merge] {project_dir!r}: '
          f'taken_theirs={n_taken_theirs} '
          f'merged_lift={n_merged_lift} (conflicts={n_conflicts}) '
          f'kept_ours={n_kept_ours} '
          f'deleted_honored={n_deleted_honored} '
          f'deleted_overridden={n_deleted_overridden}',
          file=sys.stderr, flush=True)
    return True, n_conflicts


def snapshot_unstaged_paths(repo, project_dir):
    """Read working-tree bytes for every unstaged-mod path (except
    daemon-internal scratch dirs) into an in-memory dict. Used by
    ``lan_push._merge_then_push_locked`` as a recovery snapshot
    before pre-commit + merge — if the pre-commit silently
    fails to stage (porcelain.add no-op edge cases observed in
    the field as "red +N hanging around after a swipe"),
    ``_merge_diverged`` would otherwise overwrite the working
    tree with committed state and lose the user's edits.

    Returns ``dict[path_bytes, bytes]``. Empty on a clean
    working tree or transient failure (caller handles empty
    same as "no snapshot needed").
    """
    from dulwich import porcelain
    pending_prefix = b'.azt_atomic_pending/'
    orphan_prefix = b'.azt_atomic_orphans/'
    snapshot = {}
    try:
        st = porcelain.status(repo, untracked_files='no')
    except Exception as ex:
        print(f'[snapshot] status raised {ex!r}; returning empty',
              file=sys.stderr, flush=True)
        return snapshot
    for rel in (st.unstaged or []):
        if rel.startswith(pending_prefix) \
                or rel.startswith(orphan_prefix):
            continue
        try:
            rel_str = rel.decode('utf-8', 'replace')
        except Exception:
            continue
        full = os.path.join(project_dir, rel_str)
        try:
            with open(full, 'rb') as fh:
                snapshot[rel] = fh.read()
        except OSError:
            pass
    return snapshot


def reapply_snapshot_after_merge(repo, project_dir, snapshot,
                                  base_sha):
    """Reapply a working-tree snapshot after ``_merge_diverged``
    has overwritten paths. For ``.lift`` files, three-way merge
    via ``lift_merge.three_way_merge`` with base = the snapshot's
    pre-merge HEAD blob, ours = snapshot, theirs = current working
    tree (the merge result). For non-LIFT paths, overwrite with
    the snapshot (user's edit wins — same policy
    ``_merge_diverged`` itself uses for non-LIFT modify/modify).

    Caller (``_merge_then_push_locked``) holds ``project_lock``;
    no inner lock acquisition.

    *base_sha*: the local HEAD SHA at the time the snapshot was
    taken (i.e. before the merge ran). May be empty/None if the
    repo had no commits then; lift_merge handles empty base.

    Returns ``(applied_paths, conflicts)`` — the count of paths
    reapplied and the total lift_merge conflicts across all
    reapplied LIFTs.
    """
    if not snapshot:
        return 0, 0
    base_blobs = {}
    if base_sha:
        try:
            base_commit = repo[base_sha]
            base_blobs = _walk_tree(repo, base_commit.tree)
        except Exception:
            base_blobs = {}
    applied = 0
    total_conflicts = 0
    # ours = the pre-merge snapshot (user's working-tree edits); a take
    # it references may be uncommitted → NOW wins (work_dir on-disk).
    try:
        _head_now = repo.refs[b'HEAD']
    except Exception:
        _head_now = None
    audio_rec = _audio_recency_resolver(
        repo, _head_now, None, work_dir=project_dir)
    for path_bytes, snap_bytes in snapshot.items():
        try:
            rel = path_bytes.decode('utf-8', 'replace')
        except Exception:
            continue
        full = os.path.join(project_dir, rel)
        try:
            with open(full, 'rb') as fh:
                post_merge_bytes = fh.read()
        except OSError:
            post_merge_bytes = b''
        if snap_bytes == post_merge_bytes:
            continue
        if path_bytes.endswith(b'.lift'):
            base_sha_blob = base_blobs.get(path_bytes)
            base_bytes = b''
            if base_sha_blob:
                try:
                    base_bytes = _blob_bytes(
                        repo, base_sha_blob) or b''
                except Exception:
                    base_bytes = b''
            try:
                mr = lift_merge.three_way_merge(
                    base_bytes, snap_bytes, post_merge_bytes,
                    path=rel, audio_recency=audio_rec)
                with open(full, 'wb') as fh:
                    fh.write(mr.merged_bytes)
                applied += 1
                total_conflicts += len(mr.conflicts)
                print(f'[lan-merge-reapply] lift_merge {rel!r}: '
                      f'merged ({len(mr.conflicts)} conflicts)',
                      file=sys.stderr, flush=True)
            except Exception as ex:
                # Merge raised — restore snapshot to preserve
                # user data (last-resort: keep ours).
                try:
                    with open(full, 'wb') as fh:
                        fh.write(snap_bytes)
                    applied += 1
                    print(f'[lan-merge-reapply] lift_merge raised '
                          f'{ex!r}; restored snapshot for {rel!r}',
                          file=sys.stderr, flush=True)
                except OSError as wex:
                    print(f'[lan-merge-reapply] restore for '
                          f'{rel!r} failed: {wex!r}',
                          file=sys.stderr, flush=True)
        else:
            try:
                with open(full, 'wb') as fh:
                    fh.write(snap_bytes)
                applied += 1
                print(f'[lan-merge-reapply] restored snapshot for '
                      f'{rel!r} ({len(snap_bytes)} bytes)',
                      file=sys.stderr, flush=True)
            except OSError as ex:
                print(f'[lan-merge-reapply] write {full!r} failed: '
                      f'{ex!r}', file=sys.stderr, flush=True)
    return applied, total_conflicts


def ensure_initial_commit(project_dir, contributor_name='AZT'):
    """Idempotent ``porcelain.init`` + initial commit of whatever's
    on disk in *project_dir*. Called from ``projects.create_from_template``
    so a freshly-created project has a usable git state (``.git/``
    plus a born HEAD) before the user's first record fires. Without
    this, every ``commit_project`` would NOT_A_REPO until the user
    eventually tapped Publish, and the project couldn't be shared
    over LAN (listener returns 404 with no refs to serve).

    Returns a ``Result`` carrying ``INITIALIZED`` /
    ``ALREADY_INITIALIZED`` / ``COMMITTED_LOCAL`` /
    ``NOTHING_TO_COMMIT``. Holds ``project_lock`` (so concurrent
    callers serialize). On lock contention returns a busy result;
    the caller treats this as transient.
    """
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            with _track_opened_repos():
                return _ensure_initial_commit_locked(
                    project_dir, contributor_name)
    except LockTimeout:
        return _busy_result(project_dir)


def _ensure_initial_commit_locked(project_dir, contributor_name):
    from dulwich import porcelain
    result = Result()
    if not project_dir or not os.path.isdir(project_dir):
        result.add(S.NOT_A_REPO)
        return result
    repo = _get_repo(project_dir)
    if repo is None:
        try:
            repo = porcelain.init(project_dir)
            result.add(S.INITIALIZED)
        except Exception as ex:
            result.add(S.NOT_A_REPO)
            print(f'[ensure-initial] {project_dir!r}: '
                  f'porcelain.init failed: {ex!r}',
                  file=sys.stderr, flush=True)
            return result
    else:
        result.add(S.ALREADY_INITIALIZED)
    gitignore = os.path.join(project_dir, '.gitignore')
    if not os.path.exists(gitignore):
        try:
            with open(gitignore, 'w') as fh:
                fh.write('__pycache__/\n*.pyc\n.buildozer/\nenv/\n'
                         '.DS_Store\nimage_cache/\n'
                         '.azt_atomic_pending/\n'
                         '.azt_atomic_orphans/\n')
        except OSError:
            pass
    # Re-use the normal commit step so we get the same DATA_LOSS_RISK
    # / large-file diagnostics / debounce-counter clearing as a
    # regular commit. Adds COMMITTED_LOCAL or NOTHING_TO_COMMIT
    # depending on whether the working tree has any content.
    _commit_step_locked(repo, project_dir, contributor_name, result)
    return result


def _commit_step_locked(repo, project_dir, contributor_name, result,
                        message=None):
    """Stage + commit on an already-opened repo. Mutates *result*
    in place (adds COMMITTED_LOCAL / NOTHING_TO_COMMIT / etc.).
    Caller holds the project lock. ``message`` overrides the default
    commit message (used by ``submit_file`` for desktop LIFT edits;
    the default names the recorder's audio-recording case)."""
    from dulwich import porcelain
    _stage_all(repo, project_dir)
    # Diagnostic: walk for any file outside the staging filter
    # (peer-write-to-unexpected-location class). The walk runs
    # both here and inside ``_stage_audio`` because either entry
    # point can be the one a peer hits.
    #
    # 0.53.2: the whitelist walk was written for the recorder's
    # tidy project shape; a desktop-azt project legitimately
    # carries settings JSONs, WritingSystems/*.ldml, dated
    # backups, reports/ … which the walk flags — but on THIS
    # path staging is whole-tree ``add -A``, so every such file
    # was either just staged (in the index → backed up; the old
    # message was simply false) or is .gitignore-matched (a
    # deliberate exclusion per the AZT persistence contract
    # D5/D6, not data loss). Filter to the genuinely-at-risk
    # remainder; field repro was a false DATA_LOSS_RISK — a
    # never-silenced sticky banner on peers — on the first
    # desktop-azt commit (nml, 2026-07-07).
    uncommittable = _detect_uncommittable(project_dir)
    if uncommittable:
        try:
            idx = repo.open_index()
            try:
                from dulwich.ignore import IgnoreFilterManager
                ign = IgnoreFilterManager.from_repo(repo)
            except Exception:
                ign = None

            def _genuinely_at_risk(rel):
                relp = rel.replace('\\', '/')
                if os.fsencode(relp) in idx:
                    return False        # staged → backed up
                if ign is not None:
                    try:
                        if ign.is_ignored(relp):
                            return False  # deliberate exclusion
                    except Exception:
                        pass
                return True

            uncommittable = [r for r in uncommittable
                             if _genuinely_at_risk(r)]
        except Exception as ex:
            print(f'[data-loss-risk] at-risk filter raised {ex!r}; '
                  f'keeping unfiltered list',
                  file=sys.stderr, flush=True)
    if uncommittable:
        for rel in uncommittable[:20]:
            print(f'[data-loss-risk] uncommittable file in '
                  f'project_dir: {rel!r}',
                  file=sys.stderr, flush=True)
        result.add(S.DATA_LOSS_RISK,
                   count=len(uncommittable),
                   sample=uncommittable[:5])
    st = porcelain.status(repo)
    has_staged = any(st.staged.get(k) for k in ('add', 'modify', 'delete'))
    if has_staged:
        author = _default_author(contributor_name)
        committer = _app_committer()
        try:
            sha = porcelain.commit(
                repo,
                message=_enc(message
                             or f'Audio recordings by {contributor_name}'),
                author=author, committer=committer,
            )
            # ``head_sha`` param (0.53.0): lets a peer that just
            # caused this commit update its cached base without an
            # extra ``project_status`` poll. Extra params are
            # invisible to older clients (translations only format
            # the params they name), so no MIN_CLIENT_VERSION bump.
            result.add(S.COMMITTED_LOCAL, head_sha=_sha_str(sha))
            _clear_commit_failure_count(project_dir)
            # Push-notify any peer observing this project's status URI.
            # HEAD just advanced; subscribed peers wake up + re-poll.
            # No-op off Android (peers fall back to polling there).
            try:
                from . import projects as _projects_mod
                from .android_cp import notify as _notify
                langcode = _projects_mod.find_langcode_by_working_dir(
                    project_dir)
                if langcode:
                    _notify.notify_project_changed(langcode)
            except Exception:
                pass
            # Data-quality flag: oversize **audio** files in the
            # just-made commit. Recorder is for word-list elicitation;
            # multi-MB audio files probably mean someone recorded a
            # phrase/text by mistake. Trace + typed status. Doesn't
            # block the commit (file's already in history) — just
            # surfaces for review.
            #
            # Restricted to ``audio/`` (0.50.63) because CAWL ships
            # photographic images in the 1–2 MB range that match the
            # audio threshold but are legitimate content, not user
            # error. Pre-0.50.63 the check fired on any large file in
            # the commit and surfaced ``LARGE_AUDIO_FILE_DETECTED`` for
            # normal CAWL images — misleading and noisy. The image
            # case has no analogous user-error mode worth flagging
            # (peers consume CAWL, don't create it), so there's no
            # corresponding image check; we just gate this one to
            # audio.
            threshold = _settings.large_audio_byte_threshold()
            large = _check_large_files_in_commit(repo, sha, threshold)
            for path, size in large:
                if not path.startswith('audio/'):
                    continue
                sha_str = _sha_str(sha)[:8]
                print(f'[data-quality] large audio in commit '
                      f'{sha_str}: {path!r} '
                      f'({size:,} bytes; threshold {threshold:,})',
                      file=sys.stderr, flush=True)
                result.add(
                    S.LARGE_AUDIO_FILE_DETECTED,
                    path=path,
                    bytes=size,
                    threshold=threshold,
                    commit_sha=sha_str,
                )
        except Exception as exc:
            _surface_commit_failure(result, project_dir, exc)
    else:
        result.add(S.NOTHING_TO_COMMIT)
        # Index is clean — whatever caused a prior stuck-commit
        # state has resolved itself (e.g. a peer recovered the
        # commit on another path). Clear the counter so the
        # scheduler's retry loop and project_status polling stop
        # alarming.
        _clear_commit_failure_count(project_dir)


def push_repo(project_dir, username, token):
    """Fetch + merge + push for *project_dir*. No commit step —
    caller is responsible for having committed local changes (via
    commit_repo or commit_audio_and_sync). Returns a Result with
    PUSHED / PULLED / PULL_FAILED / PUSH_FAILED / NO_REMOTE / etc.

    This is the daemon-side primitive the scheduler's drain loop
    calls when conditions allow (online + post-online grace +
    work_offline=False). Caller is also responsible for checking
    is_online — push_repo will attempt the network operation
    unconditionally so user-gestured 'try anyway' paths work."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            with _track_opened_repos():
                return _push_repo_locked(project_dir, username, token)
    except LockTimeout:
        return _busy_result(project_dir)


def _push_repo_locked(project_dir, username, token):
    from dulwich import porcelain
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        result.add(S.NOT_A_REPO)
        return result
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8').strip()
    except KeyError:
        result.add(S.NO_REMOTE)
        return result
    # Half-stripped origin (``[remote "origin"]`` with ``url = ``
    # empty) is semantically the same as no origin: there's nowhere
    # to push. Pre-this-fix the empty URL flowed into
    # ``_push_step_locked`` and dulwich raised ``NotGitRepository``
    # on every drain tick, ~4 times per cycle. Same shape as the
    # 0.46.8 fix in ``_count_commits_ahead`` for the same root cause.
    if not remote_url:
        result.add(S.NO_REMOTE)
        return result
    wan = wan_url(remote_url)
    if wan != remote_url:
        lift_merge.trace(
            f'[sync-trace] origin is ssh-shaped ({remote_url!r}); '
            f'using {wan!r} for WAN ops (stored URL untouched)')
        remote_url = wan
    _push_step_locked(repo, project_dir, username, token, remote_url, result)
    _push_extras_step(repo, project_dir, result)
    return result


def sync_repo(project_dir, username, token, contributor_name):
    """Combined commit + push under a single project lock.

    Legacy entry point — kept for callers that want both halves
    atomically (e.g. ``commit_audio_and_sync``, the user-Sync
    button before the commit-driven model fully lands). New code
    paths should call ``commit_repo`` and ``push_repo``
    separately so the commit cadence stays decoupled from push
    cadence (the connectivity watcher drives push)."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            with _track_opened_repos():
                return _sync_repo_locked(project_dir, username, token,
                                         contributor_name)
    except LockTimeout:
        return _busy_result(project_dir)


def _sync_repo_locked(project_dir, username, token, contributor_name):
    from dulwich import porcelain
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        result.add(S.NOT_A_REPO)
        return result
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8').strip()
    except KeyError:
        result.add(S.NO_REMOTE)
        return result
    # Half-stripped origin (url="") = no remote. Same fix shape as
    # ``_push_repo_locked`` and ``_count_commits_ahead`` (0.46.8).
    if not remote_url:
        result.add(S.NO_REMOTE)
        return result
    wan = wan_url(remote_url)
    if wan != remote_url:
        lift_merge.trace(
            f'[sync-trace] origin is ssh-shaped ({remote_url!r}); '
            f'using {wan!r} for WAN ops (stored URL untouched)')
        remote_url = wan

    # Stage + commit local changes BEFORE the merge so they're a
    # proper commit on local <branch>, not just dirty working tree.
    _commit_step_locked(repo, project_dir, contributor_name, result)
    _push_step_locked(repo, project_dir, username, token, remote_url, result)
    _push_extras_step(repo, project_dir, result)
    return result


# Markers for the network-class push failures that adaptive
# batching can recover from. Anything matching here triggers
# halve-and-retry; anything else (auth, repo gone, etc.) does not.
# Field-observed strings — extend as new patterns surface.
_PUSH_NETWORK_MARKERS = (
    'connection aborted',
    'remotedisconnected',
    'incompleteread',
    'nameresolutionerror',
    'no address associated',
    'timeout',
    'connection reset',
    'broken pipe',
    'eof occurred',
    # GitHub's git-receive-pack returns an unexpected HTTP status
    # (typically 4xx) when a slow / large upload exceeds its
    # server-side timeout. Looks like a protocol error but the
    # underlying cause is "this pack didn't fit through this pipe
    # in the allotted time" — exactly the case adaptive batching
    # exists to fix. Field log 2026-05-18 showed
    # ``unexpected http resp 400`` from this path.
    'unexpected http resp',
)


def _is_network_push_failure(exc):
    """Return True if *exc* looks like a transient network failure
    eligible for adaptive batch shrinking. False on auth / definitive
    server errors — those should not loop."""
    if _is_http_403(exc):
        return False
    s = str(exc).lower()
    return any(m in s for m in _PUSH_NETWORK_MARKERS)


# DNS-specific subset of the network markers. Hitting any of these
# means the push didn't fail for any of the usual transport reasons
# (timeout / connection-reset / pack-too-big) — it failed because the
# resolver couldn't translate the host. After 0.43.5's DoH fallback
# (``net.py:_patch_resolver``), reaching here means *both* the system
# resolver and Cloudflare DoH-via-1.1.1.1 failed for the same name.
# Emitted on failure paths alongside ``PUSH_FAILED`` so peers can
# route to a distinct, auto-sync-silent ``DNS_RESOLUTION_FAILED``
# toast on the user-initiated path.
_DNS_FAILURE_MARKERS = (
    'nameresolutionerror',
    'no address associated',
    'failed to resolve',
    'name or service not known',
    'temporary failure in name resolution',
)


def _is_dns_resolution_failure(exc):
    """Return True if *exc* looks like a DNS resolution failure."""
    s = str(exc).lower()
    return any(m in s for m in _DNS_FAILURE_MARKERS)


# Auth-class failures the daemon sees when an access token has gone
# stale after a failed refresh (typical sequence: refresh attempt
# during sync setup fails with a network error → access token rides
# on past its 8h cliff → server returns 401 on git-receive-pack). The
# pre-0.43.22 push loop never recognised this shape: 401 doesn't match
# ``_is_http_403`` (so the diagnose_403 short-circuit doesn't fire)
# AND 401 doesn't match any of the network markers either (so
# adaptive batching gives up via consecutive_failures rather than
# AUTH_REQUIRED). Result: a 35-minute chunk-halving storm on a flaky
# tether where the underlying problem was a stale token discoverable
# in one HTTPS request.
_HTTP_401_RE = re.compile(r'\b401\b')
_HTTPUNAUTHORIZED_MARKERS = (
    'httpunauthorized',
    'no valid credentials provided',
)


def _is_http_401(exc):
    """Return True if *exc* looks like an HTTP 401 from a git endpoint.
    Matches both dulwich's typed ``HTTPUnauthorized`` and the bare
    ``unexpected http resp 401`` shape some transport paths surface."""
    if _HTTP_401_RE.search(str(exc)):
        return True
    s = str(exc).lower()
    return any(m in s for m in _HTTPUNAUTHORIZED_MARKERS)


def _extract_diverged_remote(exc):
    """If *exc* is a dulwich ``DivergedBranches``, return the SHA the
    server says its ref currently holds (the ``old`` side of the
    rejected ref update). Else return None.

    ``DivergedBranches.args`` is ``(current_sha, new_sha)``: the
    server's current value of the ref, and the value our push wanted
    to set. The server's view is authoritative — more reliable than
    re-fetching ``refs/remotes/origin/<branch>`` when DNS is flapping
    and ``porcelain.fetch`` raises ``IncompleteRead`` so the local
    tracking ref stays frozen at clone-time. Field log baf 2026-05-20
    showed the retry loop bouncing on a stale ``remote_sha`` for
    minutes while ``DivergedBranches`` was carrying the truth.

    Returns ``bytes`` to match the rest of the ref-handling code in
    this module."""
    try:
        from dulwich.errors import DivergedBranches
    except ImportError:
        return None
    if not isinstance(exc, DivergedBranches):
        return None
    args = getattr(exc, 'args', ()) or ()
    if len(args) >= 1 and isinstance(args[0], (bytes, bytearray)):
        return bytes(args[0])
    return None


def _format_push_error(exc):
    """Best-effort human-readable string from a push exception.

    Handles the dulwich case where ``str(exc)`` yields a
    tuple-of-bytes repr like ``(b'810ef46…', b'd7d4c0b…')``: that's
    ``UpdateRefsError``'s default ``__str__`` when ``args`` is a
    tuple of two byte SHAs (the (old_sha, new_sha) pair the server
    rejected) and the exception has no override. The repr is
    useless to a user and hides the real cause — in the field
    observed (0.43.x baf testing) this shape is almost always a
    non-fast-forward rejection. Rewrite to something a user /
    maintainer can actually act on.

    Falls through to ``str(exc)`` for shapes we recognise as
    informative (network errors, HTTP 4xx, etc.)."""
    raw = str(exc)
    if raw.startswith("(b'") or raw.startswith('(b"'):
        return ('remote rejected ref update (likely non-fast-forward — '
                'remote has commits not present locally): ' + raw)
    return raw


def _is_non_ff_rejection(exc):
    """Detect a non-fast-forward push rejection. Surfaces in two
    shapes in the field:

    1. ``str(exc)`` is a bytes-tuple repr (dulwich
       ``UpdateRefsError`` with bytes-tuple ``args`` and no
       ``__str__`` override — the (old_sha, new_sha) pair the
       server reported as rejected).
    2. ``str(exc)`` contains an explicit non-FF marker — git
       servers (and dulwich layers above the raw status report)
       use a handful of phrases.

    Used by the push retry loop to force re-fetch + reconcile
    even when our local cache of ``refs/remotes/origin/<branch>``
    matches the post-rejection fetch result. The rejection itself
    proves the server has something we don't; the existing race
    gate (``new_remote != remote_sha``) is too conservative for
    that case."""
    raw = str(exc)
    if raw.startswith("(b'") or raw.startswith('(b"'):
        return True
    s = raw.lower()
    return ('non-fast-forward' in s
            or 'fetch first' in s
            or 'ref update rejected' in s)


def _add_push_failure(result, exc):
    """Append PUSH_FAILED to *result*, and DNS_RESOLUTION_FAILED first
    if the cause is resolver-class. Both go on the result so peers
    can route on either with ``result.has(S.DNS_RESOLUTION_FAILED)``
    (preferred for the auto/user routing distinction) or fall back
    to ``result.has(S.PUSH_FAILED)`` for the unspecified-failure
    bucket."""
    if _is_dns_resolution_failure(exc):
        result.add(S.DNS_RESOLUTION_FAILED)
    result.add(S.PUSH_FAILED, error=_format_push_error(exc))


def _count_commits_between(repo, ancestor_sha, descendant_sha):
    """Number of commits on ``descendant_sha`` not reachable from
    ``ancestor_sha``. 0 on equality or any walker failure."""
    if not ancestor_sha or not descendant_sha:
        return 0
    if ancestor_sha == descendant_sha:
        return 0
    try:
        walker = repo.get_walker(
            include=[descendant_sha], exclude=[ancestor_sha])
        return sum(1 for _ in walker)
    except Exception:
        return 0


def _pick_intermediate_sha(repo, base_sha, tip_sha, n):
    """Return a partial-advance target *n* commits forward from
    ``base_sha`` toward ``tip_sha`` (1-indexed) for the adaptive push
    chunker. Any walker error returns ``tip_sha`` (safest fallback —
    pushing the tip just retries the original full transaction).

    **The result is always a descendant of ``base_sha``** so that a
    fast-forward push ``base_sha → result`` succeeds. The naive
    ``get_walker(include=[tip], exclude=[base])`` yields commits from
    *both* parent lines of any merge in tip's ancestry; picking one off
    the sibling line gives a commit that is an ancestor of tip but NOT
    a descendant of base, so the FF push is rejected with
    ``DivergedBranches`` and the chunker halves → re-picks the same DAG
    → re-diverges, wedging convergence permanently (field: nml, device
    aztobt1-sudo stuck ~5 h at remaining=861 after a LAN merge moved
    HEAD onto a merge commit — CHANGELOG 0.52.30).

    Two paths:

    - **Fast path — first-parent spine.** Walk tip's first parents back
      to base. When base is on that spine (the common linear /
      already-chunking case) every spine commit is a first-parent
      descendant of base; pick the n-th. O(spine length), no per-commit
      ancestry test.
    - **Fallback — ancestor filter.** When base is NOT on the spine
      (merged in via a second parent, or the pre-divergence root of a
      merge tip), return the n-th commit in oldest-first order that has
      base as an ancestor. This still chunks — returning the tip
      outright would degenerate a fresh topic ref to a single
      whole-history push (field: device aztobt2-ui, base=old
      origin/main off the merge spine, estimate 9.3 GB — CHANGELOG
      0.52.31). Bounded by early-exit at n."""
    if not base_sha or not tip_sha or base_sha == tip_sha or n <= 0:
        return tip_sha
    # Fast path: first-parent spine tip → … → (child of base), newest
    # first. Bounded by history length; `seen` guards a malformed cycle.
    spine = []
    seen = set()
    cur = tip_sha
    try:
        while cur and cur != base_sha and cur not in seen:
            seen.add(cur)
            spine.append(cur)
            parents = repo[cur].parents
            if not parents:
                break
            cur = parents[0]
    except Exception:
        return tip_sha
    if cur == base_sha and spine:
        # spine[-1] is the immediate first-parent child of base; reverse
        # so index 0 is that child and index n-1 is n commits forward.
        spine.reverse()
        return spine[min(n, len(spine)) - 1]
    # Fallback: base is off tip's first-parent spine. Return the n-th
    # oldest commit reachable from tip that descends from base (base→C
    # is a valid fast-forward). Excludes sibling parent-line commits
    # (which would DivergedBranches) while still advancing in chunks.
    try:
        walker = repo.get_walker(include=[tip_sha], exclude=[base_sha])
        delta = [entry.commit.id for entry in walker]
    except Exception:
        return tip_sha
    if not delta:
        return tip_sha
    delta.reverse()  # oldest first
    picked = None
    count = 0
    for c in delta:
        if _is_ancestor(repo, base_sha, c):
            picked = c
            count += 1
            if count >= n:
                break
    return picked if picked is not None else tip_sha


def _check_large_files_in_commit(repo, commit_sha, threshold_bytes):
    """Walk a commit's diff vs its first parent and return a list of
    ``(path, size_bytes)`` for any file added or modified at size
    ``>= threshold_bytes``. Best-effort: empty list on any walker
    failure or if threshold is 0.

    Used as a data-quality flag after a successful commit — the suite
    recorder is for word-list elicitation, so multi-MB audio files
    almost always mean a recording mistake worth surfacing."""
    if threshold_bytes <= 0 or not commit_sha:
        return []
    try:
        from dulwich.diff_tree import tree_changes
        new_commit = repo[commit_sha]
        new_tree_id = new_commit.tree
        parent_tree_id = None
        if new_commit.parents:
            parent_commit = repo[new_commit.parents[0]]
            parent_tree_id = parent_commit.tree
        large = []
        for change in tree_changes(
                repo.object_store, parent_tree_id, new_tree_id):
            new = getattr(change, 'new', None)
            if not new or new.sha is None or new.path is None:
                continue
            try:
                size = repo.object_store[new.sha].raw_length()
            except Exception:
                continue
            if size >= threshold_bytes:
                try:
                    path_str = new.path.decode('utf-8', errors='replace')
                except Exception:
                    path_str = repr(new.path)
                large.append((path_str, size))
        return large
    except Exception as exc:
        lift_merge.trace(
            f'[data-quality] large-file scan failed: {exc!r}')
        return []


def _estimate_delta_size(repo, have_sha, want_sha):
    """Walk the missing-objects set for a ``(have_sha → want_sha)``
    delta and return ``(object_count, raw_bytes)``.

    Bytes are pre-compression upper bound (sums each object's
    ``raw_length()``); the pack negotiated by dulwich for the actual
    push will be smaller after deltas + zlib. Good enough for a
    diagnostic — the per-request budget on slow field links measured
    in raw_bytes is the right gauge for 'will this fit in the server's
    receive-pack timeout window?'

    Returns ``(0, 0)`` on any walk failure (treated by callers as
    'estimate unavailable, don't gate on it')."""
    if not want_sha:
        return (0, 0)
    try:
        from dulwich.object_store import MissingObjectFinder
        finder = MissingObjectFinder(
            repo.object_store,
            haves=[have_sha] if have_sha else [],
            wants=[want_sha],
        )
        count = 0
        total_bytes = 0
        for sha, _hint in finder:
            count += 1
            try:
                total_bytes += repo.object_store[sha].raw_length()
            except Exception:
                pass
        return (count, total_bytes)
    except Exception as exc:
        lift_merge.trace(
            f'[sync-trace] pack-size estimate failed: {exc!r}')
        return (0, 0)


# Per-daemon-lifetime memo of (project_dir,) we've already swept
# orphan topic-branches for. Janitor runs once per project per
# daemon spawn — finding stragglers from earlier daemon lives or
# from prior versions that didn't have Phase D delete is a one-
# time cost, not a per-drain one.
_JANITOR_SWEPT_PROJECTS = set()


def _delete_remote_topic_branch(
    repo, remote_url, username, token, topic_ref_name,
):
    """Best-effort delete of ``refs/heads/<topic_ref_name>`` on
    origin via dulwich's delete-refspec push (``':refs/heads/...'``).
    Also drops the local mirror at
    ``refs/remotes/origin/<topic_ref_name>`` on success. Failures
    are logged and swallowed — the janitor (or a later run) will
    sweep stragglers."""
    from dulwich import porcelain
    refspec = _enc(f':refs/heads/{topic_ref_name}')
    try:
        with _socket_timeout(_PUSH_TIMEOUT_S):
            porcelain.push(
                repo, remote_url, refspec,
                username=username, password=token,
                errstream=io.BytesIO(),
            )
        try:
            del repo.refs[_enc(f'refs/remotes/origin/{topic_ref_name}')]
        except Exception:
            pass
        lift_merge.trace(
            f'[sync-trace] topic-branch deleted: '
            f'{topic_ref_name!r}')
        return True
    except Exception as exc:
        lift_merge.trace(
            f'[sync-trace] topic-branch delete failed (non-fatal) '
            f'for {topic_ref_name!r}: {exc!r}')
        return False


def _count_foreign_topic_orphans(repo):
    """Count ``refs/remotes/origin/azt-pending-*-<device>`` refs
    whose device-name suffix isn't ours.

    Used by ``project_status`` since 0.50.15 (audit finding #3)
    to surface visibility of cross-device orphans the janitor
    can't safely sweep. Returns 0 on any error — this is a
    diagnostic counter, not a control-flow input.
    """
    try:
        from . import store as _store
        device_name = _store.get_device_name() or 'unset'
        safe_dev = re.sub(r'[^A-Za-z0-9._-]', '_', device_name)
        suffix = b'-' + safe_dev.encode('utf-8')
        prefix = b'refs/remotes/origin/azt-pending-'
        count = 0
        for ref in list(repo.refs.allkeys()):
            if ref.startswith(prefix) and not ref.endswith(suffix):
                count += 1
        return count
    except Exception:
        return 0


def _janitor_sweep_topic_branches(
    repo, project_dir, username, token, remote_url, branch,
):
    """One-shot startup sweep of our own merged topic-branches on
    the remote. For each ``refs/remotes/origin/azt-pending-*-<our_device>``
    whose tip is reachable from ``refs/remotes/origin/<branch>``,
    push a delete refspec.

    Conservative scope:
    - **Only our own device's refs** (suffix match on the
      sanitised device_name). Other devices' orphans stay; their
      owning device's next sync sweeps them. Refusing to touch
      anyone else's ref avoids any false-positive delete from a
      reachability check that may not be authoritative (e.g.,
      another device's topic-branch reachable from main but
      they're still mid-Phase-A from a stale checkpoint).
    - **Only merged-into-main refs.** Reachability from main is
      git's contract for "safe to drop without losing history" —
      every commit reachable from the topic is also reachable
      from main, so we can't lose work.

    Called at most once per (project_dir, daemon lifetime) from
    ``_push_step_locked``. Network cost is one round-trip per
    deletion; in steady state (Phase D ran on the last success)
    this finds zero refs to sweep."""
    from . import store as _store

    device_name = _store.get_device_name() or 'unset'
    safe_dev = re.sub(r'[^A-Za-z0-9._-]', '_', device_name)
    suffix = b'-' + safe_dev.encode('utf-8')

    main_remote_ref = _enc(f'refs/remotes/origin/{branch}')
    try:
        main_tip = repo.refs[main_remote_ref]
    except KeyError:
        return  # nothing to compare against; bail.

    prefix = b'refs/remotes/origin/azt-pending-'
    candidates = []
    try:
        for ref_name in list(repo.refs.allkeys()):
            if not ref_name.startswith(prefix):
                continue
            if not ref_name.endswith(suffix):
                continue
            try:
                topic_tip = repo.refs[ref_name]
            except KeyError:
                continue
            if not _is_ancestor(repo, topic_tip, main_tip):
                continue
            candidates.append((ref_name, topic_tip))
    except Exception as exc:
        lift_merge.trace(
            f'[sync-trace] janitor: ref enumeration failed: {exc!r}')
        return

    if not candidates:
        return

    lift_merge.trace(
        f'[sync-trace] janitor: sweeping {len(candidates)} merged '
        f'topic-branch(es)')
    for ref_name, _topic_tip in candidates:
        server_ref = ref_name[len(b'refs/remotes/origin/'):].decode(
            'utf-8', errors='replace')
        _delete_remote_topic_branch(
            repo, remote_url, username, token, server_ref)


def _maybe_run_janitor(
    repo, project_dir, username, token, remote_url, branch,
):
    """Idempotent wrapper around the once-per-lifetime remote-ref
    sweeps, keyed by ``project_dir``: merged ``azt-pending-*``
    topic-branches, then orphaned ``azt-blob-seed-*`` side refs.

    The preseed sweep also runs at topic-push entry, but a project
    that has *converged* never topic-pushes again — so without
    this call, the side refs from its last chunked upload sit on
    the server forever (field: nml left ~45 ``azt-blob-seed-*``
    branches on github after the 0.53.9 convergence). Running it
    here means any ordinary push after convergence cleans them up.
    Same conservative contract as at topic-push entry: only refs
    whose every blob is reachable from ``origin/<branch>`` are
    deleted, so another device mid-Phase-A keeps its refs."""
    if project_dir in _JANITOR_SWEPT_PROJECTS:
        return
    _JANITOR_SWEPT_PROJECTS.add(project_dir)
    _janitor_sweep_topic_branches(
        repo, project_dir, username, token, remote_url, branch)
    try:
        _sweep_orphan_preseed_refs(
            repo, remote_url, username, token, branch)
    except Exception as exc:
        lift_merge.trace(
            f'[sync-trace] janitor: preseed sweep failed '
            f'(non-fatal): {exc!r}')


_PRESEED_REF_PREFIX = 'azt-blob-seed-'
_PRESEED_TRACK_PREFIX = b'refs/remotes/origin/' + _PRESEED_REF_PREFIX.encode()
_PRESEED_OVERHEAD_PER_BLOB = 64  # tree entry + pack obj overhead
_PRESEED_OVERHEAD_PER_COMMIT = 300  # commit obj + empty tree skeleton
_PRESEED_FILL_RATIO = 0.7  # conservative — leaves headroom for compression variance


def _enumerate_new_blobs(repo, chunk_base_sha, target_sha):
    """Return list of ``(sha, size)`` for every blob reachable from
    *target_sha* but not from *chunk_base_sha* OR from any local
    ``refs/remotes/origin/azt-blob-seed-*`` tracking ref (our prior
    pre-seed runs that already landed on the server).

    Mirrors ``_estimate_delta_size``'s reachability walk but
    filters to blob objects only — those dominate the pack on
    audio-heavy commits, and they're the objects pre-seeding can
    relocate to side refs.
    """
    if not target_sha:
        return []
    try:
        from dulwich.object_store import MissingObjectFinder
        haves = []
        if chunk_base_sha:
            haves.append(chunk_base_sha)
        for ref in list(repo.refs.allkeys()):
            if ref.startswith(_PRESEED_TRACK_PREFIX):
                try:
                    haves.append(repo.refs[ref])
                except Exception:
                    continue
        finder = MissingObjectFinder(
            repo.object_store,
            haves=haves,
            wants=[target_sha],
        )
        out = []
        for sha, _hint in finder:
            try:
                obj = repo.object_store[sha]
                if obj.type_name == b'blob':
                    out.append((sha, obj.raw_length()))
            except Exception:
                continue
        return out
    except Exception as exc:
        lift_merge.trace(
            f'[sync-trace] preseed: blob enumeration failed: {exc!r}')
        return []


def _preseed_oversize_blobs(
    repo, chunk_base_sha, target_sha, remote_url,
    username, token, budget_bytes,
):
    """Pre-seed blobs reachable from *target_sha* (but not from
    *chunk_base_sha* or any prior side ref) onto the server via
    synthetic-commit side refs at
    ``refs/heads/azt-blob-seed-<synthetic-sha-16>``. After this
    returns ``(True, None)`` the subsequent push of *target_sha*
    needs only commit + tree in the pack — the server already has
    every blob the commit references, and the pack-builder
    deduplicates against the side-ref tracking refs we just
    updated locally.

    Batched: greedy-fills batches until the estimated pack size
    approaches ``budget_bytes * _PRESEED_FILL_RATIO``. Each batch
    is one synthetic commit + one synthetic tree + the batch's
    blobs in one push.

    Deterministic: blobs are sorted by SHA before batching, and
    the synthetic commit's author / timestamp / message are
    fixed. A re-run after partial completion (daemon respawn,
    network drop mid-batch) computes the same batches → same
    synthetic-commit SHAs → same side-ref names → idempotent
    against github (pushing a ref the server already has at the
    same SHA is a zero-byte no-op).

    Returns ``(success: bool, status: Status | None)``:

    - ``(True, None)``: all blobs are on the server. Caller
      should retry the original chunk-push (pack will now be
      tiny).
    - ``(False, Status(BLOB_EXCEEDS_BUDGET, …))``: a single blob
      is larger than *budget_bytes*. Terminal — no batching can
      reach it. Surface upward.
    - ``(False, Status(AUTH_REQUIRED) | 403-diagnosis)``:
      authentication failed. Terminal.
    - ``(False, None)``: transient network failure on a batch
      push. Caller falls through to the existing
      ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` bail; next drain's
      topic-push re-enters pre-seeding and the deterministic
      batching + side-ref tracking ensure already-landed batches
      are skipped.
    """
    from dulwich import porcelain
    from dulwich.objects import Tree, Commit

    blobs = _enumerate_new_blobs(repo, chunk_base_sha, target_sha)
    if not blobs:
        lift_merge.trace(
            '[sync-trace] preseed: nothing to seed '
            '(all referenced blobs already reachable from server refs)')
        return True, None

    # Determinism: sort by SHA so batching shape is identical
    # across runs.
    blobs.sort(key=lambda t: t[0])

    # A blob is an ATOMIC object — it cannot be split, so it is always
    # pushed (in its own single-blob batch below), regardless of the
    # byte budget. Do NOT refuse here: the budget governs *batching*
    # (how many blobs to group), never whether an unavoidable object is
    # allowed. Field proof (nml, 0.52.28): audio files ~4.3 MB > the
    # 3 MB budget push fine on their own — pushing the blob alone as a
    # side ref, then the commit pack is tiny (commit + tree only). The
    # old early-refuse converted a transient 408 on such a blob into a
    # permanent BLOB_EXCEEDS_BUDGET + 24 h backoff, stalling any commit
    # whose audio exceeded the budget. See CHANGELOG 0.52.29.
    oversize = [
        (sha, size) for sha, size in blobs
        if budget_bytes > 0 and (
            size + _PRESEED_OVERHEAD_PER_BLOB
            + _PRESEED_OVERHEAD_PER_COMMIT) > budget_bytes]
    if oversize:
        biggest = max(sz for _, sz in oversize)
        lift_merge.trace(
            f'[sync-trace] preseed: {len(oversize)} blob(s) exceed '
            f'budget {budget_bytes:,} (biggest {biggest:,}); each will '
            f'be pushed alone in its own batch (atomic — cannot split)')

    # Greedy-fill batches up to a conservative fraction of budget
    # so post-compression variance doesn't push any batch over the
    # wire-receive timeout window.
    fill_target = max(
        int(budget_bytes * _PRESEED_FILL_RATIO),
        _PRESEED_OVERHEAD_PER_COMMIT + 1024)
    batches = []
    cur = []
    cur_bytes = _PRESEED_OVERHEAD_PER_COMMIT
    for sha, size in blobs:
        item_bytes = size + _PRESEED_OVERHEAD_PER_BLOB
        if cur and (cur_bytes + item_bytes) > fill_target:
            batches.append(cur)
            cur = []
            cur_bytes = _PRESEED_OVERHEAD_PER_COMMIT
        cur.append(sha)
        cur_bytes += item_bytes
    if cur:
        batches.append(cur)

    total_bytes = sum(s for _, s in blobs)
    lift_merge.trace(
        f'[sync-trace] preseed: {len(blobs)} blob(s), '
        f'{total_bytes:,} bytes → {len(batches)} batch(es) '
        f'(budget={budget_bytes:,}, fill_target={fill_target:,})')

    TEMP_REF = b'refs/azt-collab/preseed_temp'

    def _cleanup_temp_ref():
        try:
            del repo.refs[TEMP_REF]
        except KeyError:
            pass

    for batch_i, batch in enumerate(batches):
        # Synthesise tree: filename = blob SHA (deterministic,
        # collision-free).
        tree = Tree()
        for sha in batch:
            tree.add(sha, 0o100644, sha)
        repo.object_store.add_object(tree)

        # Synthesise commit: fixed author / timestamp so re-running
        # produces the same commit SHA → same side-ref name.
        commit = Commit()
        commit.tree = tree.id
        commit.parents = []
        commit.author = b'AZT blob-seed <noreply@aztcollab.invalid>'
        commit.committer = commit.author
        commit.author_time = 0
        commit.commit_time = 0
        commit.author_timezone = 0
        commit.commit_timezone = 0
        commit.encoding = b'UTF-8'
        commit.message = b'azt-collab pre-seed'
        repo.object_store.add_object(commit)

        # 16 hex chars from the synthetic commit SHA gives 2**64
        # name space — collision-free at any plausible scale and
        # keeps the github branch list tidy.
        suffix = _PRESEED_REF_PREFIX.encode() + commit.id[:16]
        server_ref = b'refs/heads/' + suffix
        tracking_ref = b'refs/remotes/origin/' + suffix

        _cleanup_temp_ref()
        repo.refs[TEMP_REF] = commit.id
        refspec = TEMP_REF + b':' + server_ref

        batch_blob_bytes = sum(
            repo.object_store[sha].raw_length() for sha in batch)
        lift_merge.trace(
            f'[sync-trace] preseed batch {batch_i + 1}/'
            f'{len(batches)}: {len(batch)} blob(s) '
            f'~{batch_blob_bytes + _PRESEED_OVERHEAD_PER_COMMIT + len(batch) * _PRESEED_OVERHEAD_PER_BLOB:,} bytes '
            f'→ {commit.id[:8].decode()}')

        try:
            with _socket_timeout(_PUSH_TIMEOUT_S):
                porcelain.push(
                    repo, remote_url, refspec,
                    username=username, password=token,
                    errstream=io.BytesIO(),
                )
            try:
                repo.refs[tracking_ref] = commit.id
            except Exception as ex:
                lift_merge.trace(
                    f'[sync-trace] preseed: tracking ref update '
                    f'failed (non-fatal): {ex!r}')
        except Exception as exc:
            _cleanup_temp_ref()
            lift_merge.trace(
                f'[sync-trace] preseed batch {batch_i + 1} push '
                f'failed: {exc!r}')
            if _is_http_403(exc):
                return False, diagnose_403(token, remote_url)
            if _is_http_401(exc):
                return False, Status(S.AUTH_REQUIRED)
            return False, None

    _cleanup_temp_ref()
    lift_merge.trace(
        f'[sync-trace] preseed: all {len(batches)} batch(es) landed; '
        f'ready to retry main push')
    return True, None


def _sweep_orphan_preseed_refs(
    repo, remote_url, username, token, branch,
):
    """Delete ``refs/heads/azt-blob-seed-*`` on the server when
    every blob the side ref references is also reachable from
    ``refs/remotes/origin/<branch>`` (i.e., main caught up and the
    side ref is now garbage).

    Lazy crash-tolerant cleanup model: each topic-push call sweeps
    orphans from prior runs before doing anything else. If a prior
    Phase A pre-seeded blobs and Phase C succeeded, the side refs
    are orphaned on the server until this sweep — but they're
    harmless until then, and this scheme survives daemon kills
    between Phase C and any eager-cleanup attempt. Since 0.53.10
    the once-per-lifetime janitor (``_maybe_run_janitor``) also
    runs this sweep, because a *converged* project never enters
    topic-push again and its final upload's side refs would
    otherwise linger on the server forever.

    Conservative: a side ref whose blobs are NOT all in main's
    tree is kept (it's still useful as a "have" for the current
    Phase A's pre-seed enumeration). Best-effort: any delete that
    raises is logged and the next sweep retries.
    """
    from dulwich import porcelain
    from dulwich.object_store import iter_tree_contents

    candidates = [
        r for r in list(repo.refs.allkeys())
        if r.startswith(_PRESEED_TRACK_PREFIX)
    ]
    if not candidates:
        return

    main_ref = _enc(f'refs/remotes/origin/{branch}')
    try:
        main_sha = repo.refs[main_ref]
        main_tree = repo.object_store[main_sha].tree
        # We rely on the AZT invariant that audio blobs are
        # additive (never deleted from history) — every blob in
        # main's HEAD tree IS reachable from main, so the HEAD
        # tree walk is a sufficient proof of reachability without
        # walking every ancestor commit.
        main_blobs = set()
        for entry in iter_tree_contents(
                repo.object_store, main_tree):
            main_blobs.add(entry.sha)
    except Exception as exc:
        lift_merge.trace(
            f'[sync-trace] preseed-sweep: collect main blobs '
            f'failed (skipping sweep): {exc!r}')
        return

    deletable = []
    for tracking_ref in candidates:
        try:
            side_commit_sha = repo.refs[tracking_ref]
            side_commit = repo.object_store[side_commit_sha]
            side_tree = side_commit.tree
            all_in_main = True
            for entry in iter_tree_contents(
                    repo.object_store, side_tree):
                if entry.sha not in main_blobs:
                    all_in_main = False
                    break
            if all_in_main:
                deletable.append(tracking_ref)
        except Exception:
            continue

    if not deletable:
        return

    lift_merge.trace(
        f'[sync-trace] preseed-sweep: {len(deletable)}/'
        f'{len(candidates)} side ref(s) orphaned by main; deleting')
    for tracking_ref in deletable:
        suffix = tracking_ref[len(b'refs/remotes/origin/'):]
        server_ref = b'refs/heads/' + suffix
        refspec = b':' + server_ref
        try:
            with _socket_timeout(_PUSH_TIMEOUT_S):
                porcelain.push(
                    repo, remote_url, refspec,
                    username=username, password=token,
                    errstream=io.BytesIO(),
                )
            try:
                del repo.refs[tracking_ref]
            except Exception:
                pass
        except Exception as exc:
            lift_merge.trace(
                f'[sync-trace] preseed-sweep: delete '
                f'{tracking_ref!r} failed (non-fatal, will retry '
                f'next sweep): {exc!r}')


def _topic_branch_name(langcode, device_name):
    """Return the topic-branch ref name (without ``refs/heads/`` prefix)
    for chunked uploads from this device of this project. Format:
    ``azt-pending-<sanitized-langcode>-<sanitized-device>``.

    Sanitization replaces anything outside ``[A-Za-z0-9._-]`` with ``_``
    so the name is a valid git ref segment regardless of how loose the
    langcode or device_name validation is upstream."""
    safe_lang = re.sub(r'[^A-Za-z0-9._-]', '_', langcode or 'unset')
    safe_dev = re.sub(r'[^A-Za-z0-9._-]', '_', device_name or 'unset')
    return f'azt-pending-{safe_lang}-{safe_dev}'


def _push_chunked_to_ref(
    repo, project_dir, username, token, remote_url,
    target_sha, topic_ref_name, branch_for_main,
):
    """Phase A of the topic-branch push: push *target_sha* to
    ``refs/heads/<topic_ref_name>`` on the remote in adaptive chunks
    so each chunk's pack fits inside the server's per-request timeout.
    The topic-branch is ours alone (per-device naming) and every
    intermediate is a first-parent descendant of the current tip (see
    ``_pick_intermediate_sha``), so each chunk fast-forwards our own
    previous progress. A ``DivergedBranches`` therefore only means the
    server ref moved under us (concurrent advance / earlier partial
    run); the loop re-anchors on the server's authoritative tip and
    continues, bounded (see ``MAX_DIVERGED_RESYNCS``). Pre-0.52.30 a
    merge-commit target could make the picker return a sibling
    parent-line commit — an ancestor of target but not a descendant of
    the tip — which wedged the chunker in a permanent divergence loop.

    Used when ``_all_commits_descend_from(remote_sha, local_sha)``
    returned False (typical post-merge state) and a direct push to
    main would force the entire ~150 MB pack across one HTTP
    request. The topic-branch lets the blobs land in 5–20 MB chunks
    first; the subsequent push of ``target_sha`` to main becomes a
    tiny pack since the server already has every reachable object.

    Returns a tuple ``(success: bool, status: Status | None,
    last_pushed_sha: bytes | None)``:

    - ``success=True``: server's topic-branch ref is now at
      *target_sha*. Caller proceeds to push to main.
    - ``success=False`` + ``status=Status('TOPIC_BRANCH_CONFLICT', …)``:
      topic-branch already exists on server with content that isn't
      our ancestor (another device sharing our ``device_name``).
      Caller surfaces the status; user must change device_name.
    - ``success=False`` + ``status=None``: per-chunk failures
      exhausted the consecutive-failures cap. Caller treats as
      ``PUSH_FAILED`` transient; next drain cycle re-reads the
      server's topic-branch tip and resumes from there.

    Resume across daemon respawns: no on-disk state — the server's
    topic-branch ref IS the progress record. Each successful chunk
    push lands on the server; on next drain the fetch repopulates
    ``refs/remotes/origin/<topic_ref_name>`` and this helper picks
    up where the last run left off."""
    from dulwich import porcelain

    TEMP_REF = b'refs/azt-collab/topic_partial_push'

    def _cleanup_temp_ref():
        try:
            del repo.refs[TEMP_REF]
        except KeyError:
            pass

    # 0. Lazy sweep of orphaned side refs from prior runs. Per the
    # crash-tolerance model (see ``_sweep_orphan_preseed_refs``),
    # every topic-push call cleans up first. Cheap when nothing's
    # orphaned; bounded delete work when something is.
    _sweep_orphan_preseed_refs(
        repo, remote_url, username, token, branch_for_main)

    # 1. Probe server-side state via the local mirror that ``fetch``
    # populated earlier in ``_push_step_locked``. ``KeyError`` means
    # the topic-branch doesn't exist on server yet — the first
    # chunk push will create it.
    topic_remote_ref = _enc(f'refs/remotes/origin/{topic_ref_name}')
    main_remote_ref = _enc(f'refs/remotes/origin/{branch_for_main}')
    try:
        server_topic_tip = repo.refs[topic_remote_ref]
    except KeyError:
        server_topic_tip = None

    target_label = _sha_str(target_sha)[:8] if target_sha else '?'
    lift_merge.trace(
        f'[sync-trace] topic-push begin ref={topic_ref_name!r} '
        f'target={target_label} '
        f'server_topic_tip={_sha_str(server_topic_tip)[:8] if server_topic_tip else "(none)"!r}')

    # 2. Already done? Nothing to push.
    if server_topic_tip == target_sha:
        lift_merge.trace(
            f'[sync-trace] topic-push: server already at target; skip')
        return True, None, target_sha

    # 3. Foreign content check. If the topic-branch exists on the
    # server and isn't an ancestor of our target, we're stepping on
    # someone else's ref (another device sharing device_name). Refuse.
    if server_topic_tip is not None and not _is_ancestor(
            repo, server_topic_tip, target_sha):
        lift_merge.trace(
            f'[sync-trace] topic-push: server tip '
            f'{_sha_str(server_topic_tip)[:8]!r} is NOT an ancestor of '
            f'target {target_label!r} — foreign content, refusing')
        return False, Status(
            S.TOPIC_BRANCH_CONFLICT,
            {'topic_branch': topic_ref_name,
             'server_tip': _sha_str(server_topic_tip)[:8]}
        ), server_topic_tip

    # 4. Pick the chunk base. If server already has some of our
    # work on this branch, walk from there; otherwise walk from
    # main_remote_ref (every commit in our delta past main is
    # potentially new to the server). Falling back to ``None`` is
    # safe — ``_pick_intermediate_sha`` returns the tip in that
    # case and the chunk-halving still drives the loop.
    if server_topic_tip is not None:
        chunk_base = server_topic_tip
    else:
        try:
            chunk_base = repo.refs[main_remote_ref]
        except KeyError:
            chunk_base = None

    # 5. Chunk-halving loop. Reuse the constants and shape of the
    # main push loop but without the DivergedBranches branch — the
    # topic-branch is FF-clean by construction (per-device naming
    # + we just verified ancestry).
    MAX_CONSECUTIVE_FAILURES = 12
    consecutive_failures = 0
    # Bail after the *second* chunk_n=1 failure regardless of pack
    # size — the field shows chunk_n=1 408s are persistent on
    # too-slow connections, not transient. First failure could still
    # be a network blip; second is a pattern. (The size-based gate
    # bails on the first failure when bytes > budget — see below.)
    chunk_n_1_failures = 0
    # One pre-seed attempt per topic-push call. If pre-seed runs
    # and the subsequent chunk_n=1 push still fails (size or
    # network), bail rather than loop — the next drain re-enters
    # this function and the deterministic-batch / side-ref-aware
    # enumeration in pre-seed picks up where we left off.
    preseed_attempted = False
    # Bounded re-sync on DivergedBranches. With FF-clean intermediates
    # (see _pick_intermediate_sha) our own picks never diverge; a
    # DivergedBranches here means the server topic ref genuinely moved
    # under us (a concurrent process / earlier partial run). Re-anchor
    # chunk_base on the server's authoritative tip and continue rather
    # than halving forever. Bounded so a truly pathological ref can't
    # spin. Since 0.52.30.
    diverged_resyncs = 0
    MAX_DIVERGED_RESYNCS = 4
    backoff_s = 1.0
    initial_n = _settings.topic_branch_chunk_size()
    budget = _settings.commit_pack_byte_budget()
    working_n = None
    chunk_n = initial_n

    while consecutive_failures < MAX_CONSECUTIVE_FAILURES:
        # Refresh chunk_base each iteration in case a previous
        # chunk advanced the server-side ref. Our local mirror is
        # updated below on each successful push.
        try:
            chunk_base = repo.refs[topic_remote_ref]
        except KeyError:
            pass  # keep prior (main or None)
        remaining = _count_commits_between(repo, chunk_base, target_sha) \
            if chunk_base else None
        if remaining == 0:
            # Server caught up to target via the last chunk push.
            _cleanup_temp_ref()
            lift_merge.trace(
                f'[sync-trace] topic-push: done '
                f'(server topic-branch at target)')
            return True, None, target_sha
        if remaining is not None and remaining <= chunk_n:
            # Last chunk — push the tip directly.
            intermediate = target_sha
        else:
            n = working_n if working_n is not None else chunk_n
            intermediate = _pick_intermediate_sha(
                repo, chunk_base, target_sha, n)
        try:
            label = _sha_str(intermediate)[:8]
        except Exception:
            label = '?'
        lift_merge.trace(
            f'[sync-trace] topic-push attempt target={label} '
            f'chunk_n={chunk_n} remaining={remaining} '
            f'consecutive_failures={consecutive_failures}')

        # Pre-flight pack-size estimate. Pre-compression upper bound;
        # the wire pack will be smaller, but raw_bytes is the right
        # gauge for 'fits in the receive-pack timeout window.' Used
        # below for the chunk_n=1 budget-bail diagnosis.
        obj_count, raw_bytes = _estimate_delta_size(
            repo, chunk_base, intermediate)
        lift_merge.trace(
            f'[sync-trace] topic-push pack-size: {obj_count} objects, '
            f'{raw_bytes:,} bytes (uncompressed upper bound)')

        # Pre-shrink from the estimate instead of attempting a doomed
        # oversize push. Only while we've never had a success at this
        # size (working_n is None) and there's a smaller multi-commit
        # size to try. The estimate is an uncompressed upper bound, so
        # this is conservative; a single commit (chunk_n==1) is atomic
        # and always attempted below (the oversize path + blob pre-seed
        # handle it). Skips burning a multi-minute 408 on
        # chunk_n=50/25/… every daemon lifetime (field: nml, ~194 MB at
        # chunk_n=50 shrinks straight to chunk_n=1).
        if (working_n is None and chunk_n > 1 and budget > 0
                and raw_bytes > budget):
            shrunk = max(1, int(chunk_n * budget / raw_bytes))
            if shrunk < chunk_n:
                lift_merge.trace(
                    f'[sync-trace] topic-push pre-shrink chunk_n '
                    f'{chunk_n}→{shrunk} (est {raw_bytes:,} > '
                    f'budget {budget:,})')
                chunk_n = shrunk
                continue

        # Park the intermediate sha under TEMP_REF so dulwich can
        # resolve the lhs of the refspec.
        _cleanup_temp_ref()
        repo.refs[TEMP_REF] = intermediate
        refspec = _enc(
            f'{TEMP_REF.decode()}:refs/heads/{topic_ref_name}')
        try:
            with _socket_timeout(_PUSH_TIMEOUT_S):
                porcelain.push(
                    repo, remote_url, refspec,
                    username=username, password=token,
                    errstream=io.BytesIO(),
                )
            lift_merge.trace(
                f'[sync-trace] topic-push chunk OK '
                f'(advanced to {label})')
            # Advance local mirror.
            try:
                repo.refs[topic_remote_ref] = intermediate
            except Exception as ex:
                lift_merge.trace(
                    f'[sync-trace] topic-push: local mirror update '
                    f'failed: {ex!r}')
            consecutive_failures = 0
            backoff_s = 1.0
            if working_n is None:
                working_n = chunk_n
                lift_merge.trace(
                    f'[sync-trace] topic-push batch size '
                    f'locked at {working_n}')
            if intermediate == target_sha:
                _cleanup_temp_ref()
                return True, None, target_sha
            continue
        except Exception as exc:
            lift_merge.trace(
                f'[sync-trace] topic-push raised: {exc!r}')
            if _is_http_403(exc):
                _cleanup_temp_ref()
                return False, diagnose_403(token, remote_url), None
            if _is_http_401(exc):
                _cleanup_temp_ref()
                return False, Status(S.AUTH_REQUIRED), None
            # Server topic ref moved under us. With FF-clean picks this
            # is not our fault (concurrent advance / earlier partial
            # run) — re-anchor on the server's authoritative tip (from
            # the exception itself, more reliable than re-fetching when
            # DNS is flapping) and continue without counting a failure
            # or halving. Bounded to avoid a pathological spin.
            diverged_tip = _extract_diverged_remote(exc)
            if diverged_tip is not None and diverged_resyncs < MAX_DIVERGED_RESYNCS:
                diverged_resyncs += 1
                try:
                    repo.refs[topic_remote_ref] = diverged_tip
                except Exception:
                    pass
                _cleanup_temp_ref()
                lift_merge.trace(
                    f'[sync-trace] topic-push: DivergedBranches — '
                    f're-anchoring on server tip '
                    f'{_sha_str(diverged_tip)[:8]!r} '
                    f'(resync {diverged_resyncs}/{MAX_DIVERGED_RESYNCS}); '
                    f'continuing')
                # If the server tip is already our target, we're done.
                if diverged_tip == target_sha:
                    return True, None, target_sha
                # If it's not even an ancestor of target, HEAD changed
                # under us; bail transient so the next drain re-reads
                # target and rebuilds the chain from scratch.
                if not _is_ancestor(repo, diverged_tip, target_sha):
                    lift_merge.trace(
                        '[sync-trace] topic-push: server tip is not an '
                        'ancestor of our target — HEAD moved; bailing '
                        'transient (next drain rebuilds)')
                    return False, None, diverged_tip
                backoff_s = 1.0
                continue
            # Two OR'd bails at chunk_n=1 (no smaller unit to fall back
            # to). Either trips → surface S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET.
            #   1. Size gate: pre-flight estimate > budget — we already
            #      measured the unit as too big, no point retrying.
            #   2. Persistence gate: second chunk_n=1 failure regardless
            #      of size — the field shows chunk_n=1 408s are persistent
            #      on too-slow connections, not transient.
            if chunk_n == 1:
                chunk_n_1_failures += 1
            budget = _settings.commit_pack_byte_budget()
            oversize = (
                chunk_n == 1 and budget > 0 and raw_bytes > budget)
            exhausted = (chunk_n == 1 and chunk_n_1_failures >= 2)
            if oversize or exhausted:
                # Before bailing, try pre-seeding the commit's
                # blobs onto the server via side refs. Once
                # they're there, the chunk_n=1 push's pack
                # contains only commit + tree (KB, not MB) and
                # should fit any network window. Only one attempt
                # per topic-push call — if the post-pre-seed push
                # still fails, fall through to bail and let the
                # next drain re-enter (deterministic batching
                # makes that idempotent).
                if not preseed_attempted:
                    preseed_attempted = True
                    lift_merge.trace(
                        f'[sync-trace] topic-push: chunk_n=1 bail '
                        f'imminent ({"oversize" if oversize else "exhausted"}); '
                        f'attempting blob pre-seed before surfacing failure')
                    seed_ok, seed_status = _preseed_oversize_blobs(
                        repo, chunk_base, intermediate,
                        remote_url, username, token, budget,
                    )
                    if seed_ok:
                        lift_merge.trace(
                            '[sync-trace] topic-push: pre-seed '
                            'complete; retrying chunk_n=1 push '
                            '(pack should now be tiny — blobs on server)')
                        # Don't increment consecutive_failures or
                        # chunk_n_1_failures here — pre-seed was
                        # extra work, not a failed push attempt
                        # at this chunk size. Reset chunk_n_1_failures
                        # so the retry has both bail-counters reset.
                        chunk_n_1_failures = 0
                        backoff_s = 1.0
                        continue
                    if seed_status is not None:
                        _cleanup_temp_ref()
                        lift_merge.trace(
                            f'[sync-trace] topic-push: pre-seed '
                            f'surfaced terminal status '
                            f'{seed_status.code!r}; bailing')
                        return False, seed_status, chunk_base
                    lift_merge.trace(
                        '[sync-trace] topic-push: pre-seed had '
                        'transient failure; falling through to '
                        'bail (next drain will retry)')

                _cleanup_temp_ref()
                reason = 'oversize' if oversize else 'exhausted'
                # TRANSIENT, not terminal. A single commit is the atomic
                # unit — its pack exceeding the byte budget (oversize) or
                # timing out twice (exhausted) is NOT a permanent
                # condition: field logs show identical ~4.3 MB chunk_n=1
                # packs pushing fine moments later (nml, 0.52.28). The old
                # terminal S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET here drove
                # a 24 h wan_backoff and parked convergence for a day at
                # every oversize audio file. Return a plain transient so
                # the next drain / escalation resumes from the server
                # topic tip (progress already banked is preserved). See
                # CHANGELOG 0.52.29.
                lift_merge.trace(
                    f'[sync-trace] topic-push: chunk_n=1 {reason} '
                    f'(pack={raw_bytes:,} bytes budget={budget:,} '
                    f'failures={chunk_n_1_failures}) — transient, '
                    f'resuming next drain')
                return False, None, chunk_base
            consecutive_failures += 1
            # Halve the chunk size and try again. Floor at 1 — a
            # single-commit chunk is the smallest unit; if even
            # that fails repeatedly we'll exit on the consecutive-
            # failures cap (or the budget bail above).
            if working_n is None:
                # Never had a successful push at this size — halve
                # the next attempt's size directly.
                chunk_n = max(1, chunk_n // 2)
            else:
                # We had earlier success at working_n; halve from
                # there. (Don't grow chunk_n past what worked.)
                chunk_n = max(1, working_n // 2)
                working_n = None  # need to re-discover working size
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 16.0)
            continue

    _cleanup_temp_ref()
    lift_merge.trace(
        f'[sync-trace] topic-push giving up — consecutive failures '
        f'exceeded cap ({MAX_CONSECUTIVE_FAILURES}); will resume '
        f'next drain')
    return False, None, None


def _push_extras_step(repo, project_dir, result):
    """Best-effort push of the local branch tip to each URL in
    ``Project.extra_remotes``. Mutates *result* in place; never
    raises. Caller holds the project lock.

    No fetch, no merge: the primary (``origin``) is the authoritative
    integration point. Secondaries are publish-only "also send to
    these URLs" targets. If a secondary rejects with non-FF, the
    user has diverged the secondary host's state and needs to
    reconcile it manually — the daemon won't fetch from secondaries
    or attempt to merge their tips.

    Per-URL outcome:
      - success → ``S.EXTRA_REMOTE_PUSHED`` (params: url, branch).
      - failure → ``S.EXTRA_REMOTE_PUSH_FAILED`` (params: url, error).

    Tries every URL each call, independent of the primary's
    success or failure. Credentials are looked up per-URL via
    ``get_sync_credentials`` (so an extra on a different host than
    the primary uses the right token).
    """
    from . import projects as _projects
    from .store import get_sync_credentials
    from dulwich import porcelain

    langcode = _projects.find_langcode_by_working_dir(project_dir)
    if not langcode:
        return
    p = _projects.get(langcode)
    if p is None:
        return
    extras = list(p.extra_remotes or [])
    if not extras:
        return
    try:
        branch = porcelain.active_branch(repo).decode(
            'utf-8', errors='replace')
    except Exception:
        branch = 'main'
    refspec = _enc(f'refs/heads/{branch}:refs/heads/{branch}')

    # Remote identity is wan_url-normalized (invariant #14): an
    # extra that is the SAME repo as origin in another spelling is
    # not a second remote — pushing it again is pure duplicate work
    # (field 2026-07-21: baf carried its own ssh spelling as an
    # extra after a dual_publish decision; every sync pushed twice).
    # Skip at use so stale stored duplicates are inert without any
    # user-side cleanup; add_extra_remote refuses new ones.
    origin_wan = wan_url(_origin_config_url(repo))
    pushed_wan = set()
    for extra_url in extras:
        extra_url = (extra_url or '').strip()
        if not extra_url:
            continue
        # Live-convert ssh-shaped extras like the primary; the stored
        # URL (and the one shown in statuses) stays as configured.
        push_url = wan_url(extra_url)
        if (origin_wan and push_url == origin_wan) \
                or push_url in pushed_wan:
            print(f'[sync-extras] skipping {extra_url!r}: same repo '
                  f'as origin/another extra (different spelling)',
                  file=sys.stderr, flush=True)
            continue
        pushed_wan.add(push_url)
        git_user, token = get_sync_credentials(push_url)
        if not token:
            result.add(S.EXTRA_REMOTE_PUSH_FAILED,
                       url=extra_url,
                       error='no credentials configured for host')
            continue
        try:
            with _socket_timeout(_PUSH_TIMEOUT_S):
                porcelain.push(
                    repo, push_url, refspec,
                    username=git_user, password=token,
                    errstream=io.BytesIO(),
                )
            result.add(S.EXTRA_REMOTE_PUSHED,
                       url=extra_url, branch=branch)
            lift_merge.trace(
                f'[sync-trace] extra-remote push done: {extra_url!r}')
        except Exception as exc:
            lift_merge.trace(
                f'[sync-trace] extra-remote push failed: '
                f'{extra_url!r}: {exc!r}')
            result.add(S.EXTRA_REMOTE_PUSH_FAILED,
                       url=extra_url,
                       error=_format_push_error(exc))


def _ls_remote_main_tip(remote_url, username, token, branch):
    """Bounded single-request probe of the remote's ``<branch>`` tip
    via a git ref advertisement (one ``GET info/refs``). Returns the
    sha bytes, or ``None`` on any failure / absence.

    Much cheaper than ``porcelain.fetch``: one HTTP GET, no
    negotiation, no pack download, no local graph walk — so it can't
    inherit the fetch's unbounded-hang failure mode. Used by
    ``_push_step_locked`` to skip an unnecessary fetch when the remote
    hasn't advanced past our tracking mirror.

    Why this matters (field, nml on aztobt2-ui, 0.52.x): the remote
    had never advanced (no push ever succeeded), yet every escalated
    drain ran a full ``porcelain.fetch`` that never returned inside a
    daemon lifetime — holding ``project_lock`` for its whole run and
    starving user Sync with ``BUSY`` across 30+ restarts.
    ``socket.setdefaulttimeout`` is per-``recv``, not wall-clock, so it
    does NOT bound a slow/negotiating fetch; skipping the fetch when
    there is provably nothing to pull is the actual bound, and it lets
    the resumable chunked push proceed."""
    try:
        from dulwich.client import HttpGitClient
        from urllib.parse import urlparse
        parsed = urlparse(remote_url)
        base = f'{parsed.scheme}://{parsed.netloc}'
        path = parsed.path or '/'
        client = HttpGitClient(base, username=username, password=token)
        with _socket_timeout(_FETCH_TIMEOUT_S):
            refs = client.get_refs(path)
        if hasattr(refs, 'refs'):
            refs = refs.refs
        tip = refs.get(_enc(f'refs/heads/{branch}'))
        if isinstance(tip, bytes):
            return tip
        return None
    except Exception as exc:
        lift_merge.trace(f'[sync-trace] ls-remote peek failed: {exc!r}')
        return None


def _push_step_locked(repo, project_dir, username, token, remote_url, result):
    """Fetch + merge + push on an already-opened repo. Mutates
    *result* in place. Caller holds the project lock and has
    already validated remote_url (NO_REMOTE check)."""
    from dulwich import porcelain
    try:
        branch = porcelain.active_branch(repo).decode('utf-8', errors='replace')
    except Exception:
        branch = 'main'
    branch_ref = _enc(f'refs/heads/{branch}')
    remote_ref = _enc(f'refs/remotes/origin/{branch}')

    # ``repo.refs[name]`` raises ``KeyError`` on missing; mirror the
    # ``dict.get`` semantic the old code intended. ``DiskRefsContainer``
    # has no ``.get`` method — assuming it did was the bug that made
    # every sync silently fail post-fetch with ``AttributeError``,
    # caught by ``scheduler._fire``'s catch-all as ``PUSH_FAILED``.

    def _read_ref(name):
        try:
            return repo.refs[name]
        except KeyError:
            return None

    # Fetch (no merge yet) — via ``_fetch_origin``, which passes the
    # remote NAME except when the stored origin is ssh-shaped (then:
    # wan-normalized URL + manual tracking-ref import).
    #
    # Why the NAME matters (pre-helper history): dulwich's
    # ``porcelain.fetch`` only writes ``refs/remotes/<name>/<branch>``
    # when ``get_remote_repo`` can resolve the first positional arg
    # back to a configured remote section (``porcelain/__init__.py``
    # gates ``_import_remote_refs`` on ``remote_name is not None``).
    # Passing the URL leaves ``remote_name=None``, so the fetch
    # downloads new objects but the local tracking ref stays frozen
    # at whatever ``porcelain.clone`` wrote at project-create time.
    # Symptom in the field: ``remote_sha`` read from
    # ``refs/remotes/origin/main`` was the *clone-time* SHA forever;
    # the merge kept reconciling against a phantom remote tip, every
    # push attempt lost the same race, and 3 retries later we gave
    # up with ``PUSH_FAILED``. Dulwich resolves ``'origin'`` to the
    # URL via the config we already populated in
    # ``_init_repo_locked`` / ``_clone_repo_locked``, so the URL we
    # read above is only used for diagnostics and error reporting.
    # Cheap remote-tip probe before the (potentially very expensive,
    # non-resumable, and — per ``_ls_remote_main_tip`` — unbounded)
    # fetch. When the remote's branch tip still matches our tracking
    # mirror there is nothing to pull, so skip the fetch and go
    # straight to the resumable chunked push. Only skip on a confident
    # equality: any peek failure (``None``) or a missing mirror falls
    # through to the normal fetch so first-ever pushes and genuinely
    # advanced remotes still reconcile.
    mirror_before = _read_ref(remote_ref)
    peek_tip = _ls_remote_main_tip(remote_url, username, token, branch)
    skip_fetch = (
        peek_tip is not None
        and mirror_before is not None
        and peek_tip == mirror_before)
    if skip_fetch:
        lift_merge.trace(
            f'[sync-trace] fetch skipped: remote tip '
            f'{_sha_str(peek_tip)[:8]!r} == mirror; nothing to pull')
    lift_merge.trace(f'[sync-trace] fetch begin remote={remote_url!r}')
    try:
        if skip_fetch:
            lift_merge.trace('[sync-trace] fetch done (skipped)')
        else:
            with _socket_timeout(_FETCH_TIMEOUT_S):
                # Remote NAME when possible, wan-normalized URL +
                # manual tracking-ref import when the stored origin
                # is ssh-shaped — see _fetch_origin.
                _fetch_origin(repo, username, token)
            lift_merge.trace('[sync-trace] fetch done')
    except Exception as exc:
        if _is_http_403(exc):
            result.statuses.append(diagnose_403(token, remote_url))
            return result
        if _is_http_401(exc):
            # Stale access token (refresh failed earlier, current
            # token has expired). Short-circuit before push so we
            # don't burn the chunk-halving budget on doomed retries.
            # ``_annotate_with_auth_health`` upstream adds
            # AUTH_REFRESH_STALE based on the store's refresh-broken
            # flag; AUTH_REQUIRED here triggers the credential-fix
            # flow on the peer regardless of whether the broken-flag
            # was set in time.
            lift_merge.trace(
                '[sync-trace] fetch failed with 401 — '
                'aborting before push')
            result.add(S.AUTH_REQUIRED)
            return result
        if _is_repo_not_found(exc):
            # 404 / NotGitRepository: auto-accept a pending invite (the
            # 404 is the trigger) or surface the honest no-access verdict,
            # then short-circuit — don't fall through to the push loop,
            # which would churn the same NotGitRepository 11× (device-1
            # field repro, 0.52.23).
            return _handle_no_access(token, remote_url, result)
        # Non-fatal: maybe remote is empty or temporarily unreachable.
        # Trace explicitly so a stale ``refs/remotes/origin/*`` read
        # downstream isn't misread as authoritative — pre-0.43.19
        # this path logged ``fetch done`` even on Max-retries-exceeded
        # and the trace looked indistinguishable from a healthy
        # fetch.
        lift_merge.trace(f'[sync-trace] fetch failed: {exc!r}')
        result.add(S.PULL_FAILED, error=_format_push_error(exc))

    # ``repo.refs`` is ``DiskRefsContainer`` which does NOT define a
    # dict-style ``.get()`` — only ``__getitem__`` (raises ``KeyError``
    # on missing) and ``read_ref()``. The pre-0.20.9 code used
    # ``.get()`` and silently raised ``AttributeError`` on every sync,
    # which propagated to ``scheduler._fire``'s catch-all and marked
    # the job ``PUSH_FAILED`` — every sync committed locally but
    # never pushed, until enough commits piled up that a later
    # accidentally-fixed cycle flushed the queue. Use the proper
    # dulwich API: subscript + ``KeyError`` for missing.
    local_sha = _read_ref(branch_ref) or repo.head()
    remote_sha = _read_ref(remote_ref)
    lift_merge.trace(
        f'[sync-trace] local_sha={local_sha!r} remote_sha={remote_sha!r}')

    # One-shot janitor: sweep merged ``azt-pending-*-<our_device>``
    # refs and orphaned ``azt-blob-seed-*`` side refs on the
    # server. Idempotent per (project, daemon lifetime). Runs
    # after fetch so it sees fresh server refs and after
    # remote_sha is read so it can validate ancestry. Best-effort —
    # any failure logs and returns; sync proceeds.
    _maybe_run_janitor(
        repo, project_dir, username, token, remote_url, branch)

    retries_remaining = max(1, _settings.merge_retry_max())
    needs_merge = remote_sha is not None and remote_sha != local_sha
    lift_merge.trace(f'[sync-trace] needs_merge={needs_merge}')

    if needs_merge and _is_ancestor(repo, local_sha, remote_sha):
        # Fast-forward
        lift_merge.trace('[sync-trace] fast-forward')
        prev_local_sha = local_sha
        repo.refs[branch_ref] = remote_sha
        # Materialise the new tree to the working directory + index.
        # Bumping the ref alone leaves on-disk files at the old
        # bytes — ``LiftHandle`` then serves stale content to peers
        # and the user-visible symptom is "Phone B never sees Phone
        # A's changes." ``_apply_tree_to_workdir`` writes the diff
        # between prev_local_sha and remote_sha and resets the
        # index so the next ``porcelain.status`` is clean.
        _apply_tree_to_workdir(
            repo, project_dir, prev_local_sha, remote_sha)
        local_sha = remote_sha
        result.add(S.PULLED)
    elif needs_merge and _is_ancestor(repo, remote_sha, local_sha):
        # We're already ahead — nothing to merge
        lift_merge.trace('[sync-trace] local ahead of remote')
        pass
    elif needs_merge:
        mem_block = _check_memory_for_merge()
        if mem_block is not None:
            lift_merge.trace(
                f'[sync-trace] merge_diverged skipped '
                f'(mem_available={mem_block.params.get("mem_available_mb")}MB '
                f'< min={mem_block.params.get("min_required_mb")}MB)')
            result.statuses.append(mem_block)
            result.add(S.PULL_FAILED, error='insufficient memory for merge')
            return result

        lift_merge.trace('[sync-trace] merge_diverged begin')
        try:
            merged_sha, conflicts = _merge_diverged(
                repo, project_dir, branch, local_sha, remote_sha)
            local_sha = merged_sha
            result.add(S.PULLED)
            if conflicts:
                result.add('CONFLICTS',
                           paths=[c.path for c in conflicts][:50])
            lift_merge.trace(
                f'[sync-trace] merge_diverged done '
                f'conflicts={len(conflicts)}')
        except UnrelatedHistoriesError as exc:
            lift_merge.trace(f'[sync-trace] merge REFUSED: {exc}')
            result.add(S.MERGE_UNRELATED_HISTORIES, error=str(exc))
            return result
        except Exception as exc:
            lift_merge.trace(f'[sync-trace] merge_diverged FAILED: {exc}')
            result.add(S.PULL_FAILED,
                       error=f'merge failed: {_format_push_error(exc)}')
            return result

    # Routing decision for push: can we direct-push to ``branch`` with
    # chunk-halving, or do we need the topic-branch path to handle a
    # diverged history?
    #
    # Direct push + chunk-halving works iff every commit between
    # ``remote_sha`` and ``local_sha`` descends from ``remote_sha`` —
    # i.e., the local chain is a fast-forward of the server. The
    # canonical failure mode (this rule's reason for existing) is the
    # post-merge state where local has a merge commit at the tip; the
    # merge commit itself descends from remote, but the ~hundreds of
    # pre-merge commits on the local side don't, and any intermediate
    # the chunk picker selects from that side gets ``DivergedBranches``
    # from the server. See ``_all_commits_descend_from`` for the
    # algorithm.
    #
    # When the routing rule says "topic-branch", run Phase A
    # (chunked push to a per-device topic ref) before letting the
    # existing direct-push loop run. After Phase A, every blob
    # reachable from ``local_sha`` is on the server (under the
    # topic-branch ref); the subsequent direct push to ``main``
    # negotiates a tiny pack (just the merge commit + tree + merged
    # LIFT bytes) and completes inside the server's per-request
    # timeout. Phase B (re-fetch + conditional re-merge on race) is
    # handled implicitly by the existing post-non-FF retry loop;
    # Phase C (promote to main) is the existing direct push itself;
    # Phase D (cleanup of topic-branch) is deferred to a later
    # release — leaving the topic ref on the server is purely
    # cosmetic until the janitor lands.
    if remote_sha and local_sha != remote_sha:
        can_direct_push = _all_commits_descend_from(
            repo, remote_sha, local_sha)
        lift_merge.trace(
            f'[sync-trace] route: '
            f'{"direct-push" if can_direct_push else "topic-branch"}')
        if not can_direct_push:
            from . import store as _store
            from . import projects as _projects
            langcode = _projects.find_langcode_by_working_dir(
                project_dir) or 'unset'
            device_name = _store.get_device_name() or 'unset'
            topic_ref_name = _topic_branch_name(langcode, device_name)
            success, conflict_status, _last = _push_chunked_to_ref(
                repo, project_dir, username, token, remote_url,
                local_sha, topic_ref_name, branch)
            if success:
                # Phase A complete. Topic-branch on server now
                # contains every reachable object from ``local_sha``.
                # Run explicit Phase B (re-fetch + conditional
                # re-merge if ``main`` moved during Phase A's long
                # upload) and Phase C (push merge commit to main).
                # Each promote attempt loops back to Phase B on
                # ``DivergedBranches`` from Phase C — bounded by
                # MAX_PROMOTE_RETRIES so a hot race window can't
                # spin forever. Return directly when Phase C
                # succeeds (or fails terminally) instead of
                # falling through to the existing direct-push
                # loop; the direct path is for FF cases only and
                # we've structurally proven we're not FF.
                MAX_PROMOTE_RETRIES = 5
                for promote_attempt in range(MAX_PROMOTE_RETRIES):
                    # ── Phase B: re-fetch main; re-merge on race ──
                    lift_merge.trace(
                        f'[sync-trace] phase-b begin '
                        f'(attempt {promote_attempt + 1}/'
                        f'{MAX_PROMOTE_RETRIES})')
                    # Cheap remote-tip peek before the (unbounded,
                    # non-resumable) fetch — the SAME guard the
                    # pre-sync path already uses (see
                    # ``_ls_remote_main_tip``, whose docstring cites
                    # this exact device). ``main`` has typically NOT
                    # moved during Phase A's long upload, so skipping
                    # the fetch is what actually lets the promote
                    # finish. Field (nml/aztobt2-ui): phase-b's
                    # UNconditional ``porcelain.fetch`` never returned
                    # inside a daemon lifetime — the log stopped at
                    # "phase-b begin", the promote never reached
                    # Phase C, and github ``main`` stayed behind across
                    # dozens of process restarts. ``socket.setdefault
                    # timeout`` is per-``recv``, not wall-clock, so it
                    # does NOT bound a negotiating fetch; skipping it
                    # when there is provably nothing to pull is the
                    # real bound. Only skip on a confident equality;
                    # any peek failure / missing mirror falls through
                    # to the normal fetch so genuinely-advanced remotes
                    # still reconcile.
                    pb_mirror = _read_ref(remote_ref)
                    pb_peek = _ls_remote_main_tip(
                        remote_url, username, token, branch)
                    pb_skip = (
                        pb_peek is not None
                        and pb_mirror is not None
                        and pb_peek == pb_mirror)
                    try:
                        if pb_skip:
                            lift_merge.trace(
                                f'[sync-trace] phase-b: fetch skipped '
                                f'— remote tip '
                                f'{_sha_str(pb_peek)[:8]!r} == mirror; '
                                f'nothing to pull')
                        else:
                            with _socket_timeout(_FETCH_TIMEOUT_S):
                                _fetch_origin(repo, username, token)
                    except Exception as fexc:
                        if _is_http_403(fexc):
                            result.statuses.append(
                                diagnose_403(token, remote_url))
                            return result
                        if _is_http_401(fexc):
                            result.add(S.AUTH_REQUIRED)
                            return result
                        # Non-auth fetch failure: log + proceed
                        # with stale local mirror. Phase C may
                        # succeed (no race) or fail (we'll retry
                        # next drain).
                        lift_merge.trace(
                            f'[sync-trace] phase-b: fetch failed '
                            f'(continuing with stale mirror): '
                            f'{fexc!r}')

                    new_remote_sha = _read_ref(remote_ref) or remote_sha
                    if new_remote_sha != remote_sha:
                        lift_merge.trace(
                            f'[sync-trace] phase-b: main moved during '
                            f'Phase A: {_sha_str(remote_sha)[:8]} → '
                            f'{_sha_str(new_remote_sha)[:8]}')
                        if _is_ancestor(repo, new_remote_sha, local_sha):
                            # Our merge already includes the new
                            # remote tip — no action needed.
                            lift_merge.trace(
                                '[sync-trace] phase-b: existing '
                                'merge already includes new remote; '
                                'no re-merge')
                        elif _is_ancestor(repo, local_sha, new_remote_sha):
                            # Remote moved past us and includes
                            # everything we had. Fast-forward
                            # local — nothing to push.
                            lift_merge.trace(
                                '[sync-trace] phase-b: remote '
                                'advanced past us; FF local — '
                                'nothing to push')
                            repo.refs[branch_ref] = new_remote_sha
                            try:
                                repo.refs[remote_ref] = new_remote_sha
                            except Exception:
                                pass
                            result.add(S.PULLED)
                            return result
                        else:
                            # Diverged again — re-merge.
                            mem_block = _check_memory_for_merge()
                            if mem_block is not None:
                                lift_merge.trace(
                                    f'[sync-trace] phase-b: re-merge '
                                    f'skipped — insufficient memory '
                                    f'(have '
                                    f'{mem_block.params.get("mem_available_mb")}MB, '
                                    f'need '
                                    f'{mem_block.params.get("min_required_mb")}MB)')
                                result.statuses.append(mem_block)
                                result.add(S.PULL_FAILED,
                                           error='insufficient memory '
                                                 'for re-merge')
                                return result
                            lift_merge.trace(
                                '[sync-trace] phase-b: re-merging '
                                'against new remote tip')
                            try:
                                merged_sha, re_conflicts = _merge_diverged(
                                    repo, project_dir, branch,
                                    local_sha, new_remote_sha)
                                local_sha = merged_sha
                                if re_conflicts:
                                    result.add(
                                        'CONFLICTS',
                                        paths=[c.path for c in re_conflicts][:50])
                                lift_merge.trace(
                                    f'[sync-trace] phase-b: re-merge '
                                    f'done '
                                    f'conflicts={len(re_conflicts)}')
                            except UnrelatedHistoriesError as mexc:
                                lift_merge.trace(
                                    f'[sync-trace] phase-b: re-merge '
                                    f'REFUSED: {mexc}')
                                result.add(S.MERGE_UNRELATED_HISTORIES,
                                           error=str(mexc))
                                return result
                            except Exception as mexc:
                                lift_merge.trace(
                                    f'[sync-trace] phase-b: re-merge '
                                    f'FAILED: {mexc!r}')
                                result.add(
                                    S.PULL_FAILED,
                                    error=f'phase-b re-merge: '
                                          f'{_format_push_error(mexc)}')
                                return result
                        remote_sha = new_remote_sha

                    # ── Phase C: promote merge commit to main ──
                    # Make sure the local branch ref points at the
                    # (possibly re-merged) local_sha before push.
                    try:
                        repo.refs[branch_ref] = local_sha
                    except Exception as rexc:
                        lift_merge.trace(
                            f'[sync-trace] phase-c: branch_ref set '
                            f'failed: {rexc!r}')
                    refspec = _enc(
                        f'refs/heads/{branch}:refs/heads/{branch}')
                    lift_merge.trace(
                        f'[sync-trace] phase-c: pushing '
                        f'{_sha_str(local_sha)[:8]} → '
                        f'refs/heads/{branch}')
                    try:
                        with _socket_timeout(_PUSH_TIMEOUT_S):
                            porcelain.push(
                                repo, remote_url, refspec,
                                username=username, password=token,
                                errstream=io.BytesIO(),
                            )
                        # Success. Advance local mirror, clear
                        # any stale chunk_n hint, signal PUSHED.
                        try:
                            repo.refs[remote_ref] = local_sha
                        except Exception:
                            pass
                        _clear_failed_chunk_n(project_dir)
                        lift_merge.trace(
                            '[sync-trace] phase-c: push done')
                        # Phase D: delete the now-unneeded topic-
                        # branch on the server (best-effort; if it
                        # fails, the next-startup janitor catches
                        # the orphan since the topic's tip is now
                        # reachable from main).
                        _delete_remote_topic_branch(
                            repo, remote_url, username, token,
                            topic_ref_name)
                        result.add(S.PUSHED, branch=branch)
                        return result
                    except Exception as pexc:
                        if _is_http_403(pexc):
                            result.statuses.append(
                                diagnose_403(token, remote_url))
                            return result
                        if _is_http_401(pexc):
                            result.add(S.AUTH_REQUIRED)
                            return result
                        if _is_non_ff_rejection(pexc):
                            # Main moved between Phase B and
                            # Phase C — loop back through Phase B
                            # to re-fetch and re-merge.
                            lift_merge.trace(
                                f'[sync-trace] phase-c: non-FF '
                                f'rejection; looping to phase-b')
                            continue
                        # Other (network, server transient).
                        # Bail; next drain re-runs Phase A which
                        # short-circuits on already-uploaded
                        # objects, then Phase B + C again.
                        lift_merge.trace(
                            f'[sync-trace] phase-c: push raised '
                            f'{pexc!r}; bailing for next-drain retry')
                        result.add(
                            S.PUSH_FAILED,
                            error=f'phase-c: '
                                  f'{_format_push_error(pexc)}')
                        return result
                # Promote-retry cap exceeded — main keeps moving
                # under us. Bail; next drain tries again.
                lift_merge.trace(
                    f'[sync-trace] phase-c: exceeded '
                    f'{MAX_PROMOTE_RETRIES} promote-retries — '
                    f'main keeps racing')
                result.add(
                    S.PUSH_FAILED,
                    error='topic-branch promote: too many races')
                return result
            elif conflict_status is not None:
                # Typed status from the topic-push helper. Four
                # shapes:
                # - TOPIC_BRANCH_CONFLICT: foreign content on our
                #   topic-branch ref. Add PUSH_FAILED too for
                #   peers without specific routing on the new
                #   code; user fix is a device_name change.
                # - COMMIT_PACK_EXCEEDS_NETWORK_BUDGET: chunk_n=1 and
                #   the single-commit pack is bigger than the
                #   per-attempt budget. Add PUSH_FAILED for the same
                #   reason; the underlying fix is a bigger pipe or a
                #   structural change to where audio lives.
                # - AUTH_REQUIRED (401): bail without PUSH_FAILED
                #   per the existing 401 convention.
                # - 403 diagnosis: bail with the diagnosis but
                #   without PUSH_FAILED (matches the existing
                #   direct-push behaviour).
                result.statuses.append(conflict_status)
                if conflict_status.code == S.TOPIC_BRANCH_CONFLICT:
                    result.add(S.PUSH_FAILED,
                               error='topic-branch conflict — '
                                     'device_name collision')
                elif conflict_status.code == \
                        S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET:
                    result.add(S.PUSH_FAILED,
                               error='single-commit pack exceeds '
                                     'per-attempt budget')
                elif conflict_status.code == \
                        S.BLOB_EXCEEDS_BUDGET:
                    # Pre-seeding bottomed out: one blob alone is
                    # bigger than the budget. Same PUSH_FAILED
                    # routing as the commit-pack case so existing
                    # peer code paths handle it uniformly; the
                    # specific blob_sha / blob_bytes are in the
                    # accompanying BLOB_EXCEEDS_BUDGET status's
                    # params for the UI to render.
                    result.add(S.PUSH_FAILED,
                               error='single blob exceeds '
                                     'per-attempt budget')
                return result
            else:
                # Per-chunk failures exhausted the cap. The server
                # has whatever chunks did succeed; next drain
                # resumes from the new topic-branch tip (the
                # server's ref is the progress record). Surface as
                # PUSH_FAILED; the existing peer routing treats
                # PUSH_FAILED as transient + retryable.
                lift_merge.trace(
                    '[sync-trace] topic-push could not complete '
                    'this run; will resume next drain')
                result.add(S.PUSH_FAILED,
                           error='topic-branch chunked upload '
                                 'incomplete — will resume')
                return result

    # Adaptive-batching push loop.
    #
    # Default behaviour: push the whole local in one transaction
    # (one HTTP POST carrying the full pack). That's the historical
    # shape and the right call when the network and remote can
    # both handle it.
    #
    # Adaptation: on a network-class failure (large pack timed out
    # mid-upload, connection dropped, DNS blipped, GitHub's
    # git-receive-pack returned 4xx because we didn't finish in
    # time), halve the number of commits in the next attempt.
    # Push an intermediate SHA as ``<sha>:refs/heads/<branch>``
    # instead of the local tip — smaller pack, finishes in less
    # wall-clock time, more likely to fit through. Once a batch
    # size succeeds, lock it in (``working_batch_n``) and use it
    # for the remaining chunks until the queue drains. Inter-
    # attempt exponential backoff (1 s → capped at 16 s) gives
    # the network a chance to recover.
    #
    # We do NOT batch on the first attempt — only after a network-
    # class failure on a multi-commit push. The fast path (small
    # queue, healthy network) keeps its single-transaction shape
    # and pays no extra round-trips.
    #
    # The merge-on-race retry (someone pushed between our fetch
    # and our push) is preserved as a parallel branch: if the
    # re-fetch shows the remote moved, we run the four-case
    # ancestor logic — fast-forward / no-merge-needed / merge —
    # then resume the adaptive loop against the (possibly new)
    # local tip.
    #
    # Bounded by ``consecutive_failures``: each successful push
    # resets the counter, so we keep going as long as we're
    # making progress. Capped at 12 (≥ log2 of any plausible
    # queue size, with headroom for transient retries within
    # each chunk).
    target_sha = local_sha
    working_batch_n = None
    backoff_s = 1.0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 12

    # Smallest chunk_n we attempted during this push call. Used to
    # persist a hint across drain cycles that reflects what the
    # network actually couldn't handle, not what the last attempt
    # happened to be using. Without this, the in-call "revert to
    # full local tip" path (raised when DivergedBranches at a
    # smaller chunk forces escalation) makes the budget-exceeded
    # path remember the post-revert full size — so the next cycle
    # starts at half-full and immediately re-enters the same revert
    # cycle. Field log baf 2026-05-21 14:45 → 14:58: cycle started
    # at hint=211, hit DivergedBranches, reverted to 422, hit 408
    # at 422, "remembered chunk_n=422" → next cycle hint=211 →
    # loop. Tracking the smallest attempted instead means next
    # cycle hint becomes 211/2 = 105, making actual progress.
    smallest_attempted_n = None
    # Counter for forced-merge attempts triggered by the pathological
    # case where the server rejects a push as non-fast-forward AND
    # the immediately-following re-fetch shows the tracking ref
    # hasn't moved. Server says we don't descend, local ancestor
    # walk says we do — somebody's wrong. Pre-0.43.20 this looped
    # forever, "local still ahead" each iteration, re-attempting
    # the same target_sha + chunk_n with no path to recovery
    # (~hour-long sessions burning the user's data plan until the
    # daemon was killed). Escalation: (1) revert intermediate
    # target to the full local tip — the temp-ref pack-
    # negotiation path may be the culprit, (2) force a
    # ``_merge_diverged`` against the current remote_sha — trust
    # the server's view over our ancestor walk, (3) on the second
    # such attempt, bail with PUSH_FAILED. Reset to 0 on any push
    # success.
    nonff_forced_merges = 0

    initial_to_push = _count_commits_between(repo, remote_sha, local_sha)
    lift_merge.trace(
        f'[sync-trace] push loop begin commits_to_push={initial_to_push}')

    # If the previous drain cycle (or any prior push attempt for this
    # project in this daemon process) failed at a particular chunk_n,
    # use that as a hint for the initial target_sha. Without this, a
    # flaky-network device with a large backlog (419 commits) keeps
    # retrying at full size every cycle, hits the push budget, gives
    # up, repeats forever — exactly the field log baf 2026-05-21
    # symptom that motivated 0.44.2.
    #
    # The hint is half the previously-failed chunk_n (set by
    # ``_remember_failed_chunk_n``), so each cycle effectively
    # continues the halving the previous cycle was forced to abandon.
    # Eventually converges on a size the network can handle inside
    # ``push_budget_s``.
    hint_chunk_n = _hint_chunk_n(project_dir)
    if hint_chunk_n is not None and hint_chunk_n < initial_to_push:
        try:
            target_sha = _pick_intermediate_sha(
                repo, remote_sha, local_sha, hint_chunk_n)
            lift_merge.trace(
                f'[sync-trace] resuming with hint chunk_n='
                f'{hint_chunk_n} (prior cycle failed at larger '
                f'size; remembered across drain calls)')
        except Exception as ex:
            # _pick_intermediate_sha can raise on malformed refs;
            # fall back to full tip rather than crash the push.
            lift_merge.trace(
                f'[sync-trace] hint chunk_n={hint_chunk_n} '
                f'unusable ({ex!r}); reverting to full tip')

    # Wall-clock budget for the whole push loop. Hits SYNC_GIVING_UP_
    # TRANSIENT and bails when exceeded — see the in-loop check below.
    # 0 disables (preserves pre-0.43.22 behaviour).
    push_start_s = time.monotonic()
    push_budget_s = _settings.push_budget_s()

    # Pre-flight: if the persisted refresh state is broken AND a quick
    # probe against api.github.com confirms the access token is
    # rejected, abort BEFORE the retry loop. Skipped when refresh is
    # healthy (the probe is a network round-trip we don't want to pay
    # on every sync) and skipped for non-GitHub remotes (the probe is
    # GitHub-specific). The post-hoc ``_annotate_with_auth_health``
    # still appends AUTH_REFRESH_STALE; this just short-circuits the
    # 30-minute chunk-halving storm that field log baf 2026-05-20
    # demonstrated. The cliff for an 8h GitHub token after a 7h
    # proactive-refresh attempt is short, so any sync that hits this
    # gate is one the user needs to act on (re-run device flow).
    try:
        from . import store as _store
        from .auth import test_github_credentials
        if _store.github_refresh_state().get('broken') \
                and 'github.com' in (remote_url or ''):
            probe = test_github_credentials(token)
            if not probe.get('valid'):
                lift_merge.trace(
                    f'[sync-trace] pre-flight: refresh broken + '
                    f'token probe rejected '
                    f'({probe.get("error", "")!r}) — '
                    f'aborting push')
                result.add(S.AUTH_REQUIRED)
                return result
    except Exception as ex:
        # Pre-flight is best-effort. Any failure (import, store I/O,
        # probe timeout) falls through to the normal loop, which still
        # has the in-loop 401 short-circuit as a backstop.
        lift_merge.trace(
            f'[sync-trace] pre-flight probe skipped: {ex!r}')

    # Temp ref used to push an intermediate SHA. dulwich's
    # ``porcelain.push`` resolves the refspec's left-hand side via
    # ``repo.refs[lh]`` — a raw hex SHA on the lhs ``KeyError``s.
    # Workaround: drop the SHA into a temp ref, push that ref to
    # the remote branch, clean up. We hold ``project_lock`` for
    # the whole loop so no concurrent caller can observe the
    # temp ref.
    TEMP_REF = b'refs/azt-collab/partial_push'

    def _cleanup_temp_ref():
        try:
            del repo.refs[TEMP_REF]
        except KeyError:
            pass

    while consecutive_failures < MAX_CONSECUTIVE_FAILURES:
        chunk_n = _count_commits_between(repo, remote_sha, target_sha)
        # Track the smallest chunk_n we attempted across the loop;
        # used by ``_remember_failed_chunk_n`` on budget/cap exit so
        # the next drain cycle's hint reflects the floor, not the
        # post-revert ceiling. See ``smallest_attempted_n`` rationale
        # above the loop.
        if chunk_n > 0:
            if smallest_attempted_n is None:
                smallest_attempted_n = chunk_n
            else:
                smallest_attempted_n = min(smallest_attempted_n, chunk_n)
        try:
            _target_label = target_sha[:8].decode('ascii')
        except Exception:
            _target_label = repr(target_sha)[:10]
        lift_merge.trace(
            f'[sync-trace] push attempt target={_target_label} '
            f'chunk_n={chunk_n} '
            f'consecutive_failures={consecutive_failures}')
        # Clean any leftover temp ref from a prior iteration before
        # we possibly write a new one (idempotent on missing ref).
        # Then compose the refspec: full local tip uses the local
        # branch ref directly (the historical shape); an
        # intermediate SHA gets parked under the temp ref so
        # dulwich can resolve the lhs.
        _cleanup_temp_ref()
        if target_sha == local_sha:
            refspec = _enc(f'refs/heads/{branch}:refs/heads/{branch}')
        else:
            repo.refs[TEMP_REF] = target_sha
            refspec = _enc(f'{TEMP_REF.decode()}:refs/heads/{branch}')
        try:
            with _socket_timeout(_PUSH_TIMEOUT_S):
                porcelain.push(
                    repo, remote_url, refspec,
                    username=username, password=token,
                    errstream=io.BytesIO(),
                )
            lift_merge.trace(
                f'[sync-trace] push done (advanced {chunk_n} commits)')
            # Advance the local mirror to the SHA we just pushed.
            try:
                repo.refs[remote_ref] = target_sha
            except Exception as ex:
                lift_merge.trace(
                    f'[sync-trace] post-push remote-mirror '
                    f'update failed: {ex!r}')
            remote_sha = target_sha
            consecutive_failures = 0
            backoff_s = 1.0
            nonff_forced_merges = 0
            if target_sha == local_sha:
                # Queue cleared. Drop any remembered failed chunk_n
                # — the network just demonstrated it can handle a
                # full push at the current size, so there's no
                # constraint to carry into future cycles.
                _cleanup_temp_ref()
                _clear_failed_chunk_n(project_dir)
                result.add(S.PUSHED, branch=branch)
                return result
            # More commits to push. Lock in the batch size that
            # worked (first successful chunk sets ``working_batch_n``;
            # subsequent chunks reuse it). Pick the next target.
            if working_batch_n is None:
                working_batch_n = chunk_n
                lift_merge.trace(
                    f'[sync-trace] batch size locked at '
                    f'{working_batch_n}')
            target_sha = _pick_intermediate_sha(
                repo, remote_sha, local_sha, working_batch_n)
            continue
        except Exception as exc:
            lift_merge.trace(f'[sync-trace] push raised: {exc!r}')
            if _is_http_403(exc):
                _cleanup_temp_ref()
                result.statuses.append(diagnose_403(token, remote_url))
                return result
            if _is_http_401(exc):
                # Stale access token — see fetch-path comment. Bail
                # without consuming the chunk-halving budget.
                lift_merge.trace(
                    '[sync-trace] push raised 401 — '
                    'aborting (stale credentials)')
                _cleanup_temp_ref()
                result.add(S.AUTH_REQUIRED)
                return result
            if _is_repo_not_found(exc):
                # 404 / NotGitRepository on push: auto-accept a pending
                # invite or surface no-access, and bail without burning
                # the consecutive-failures budget on a doomed retry.
                return _handle_no_access(
                    token, remote_url, result, cleanup=_cleanup_temp_ref)
            # Wall-clock budget: when DNS / TLS / connection-reset
            # storms exhaust the network for minutes, the logical-
            # attempts cap (``consecutive_failures``) can take 30+
            # minutes to bottom out because each "halve and retry"
            # makes nominal progress while pre-fetch + per-attempt
            # urllib3 retries multiply the wall time. Cap separately
            # so a wedged session frees the project lock for the
            # next sync run.
            if push_budget_s > 0 and (
                    time.monotonic() - push_start_s) > push_budget_s:
                # Remember the SMALLEST chunk_n attempted this call —
                # not the last, which may have been a revert-to-
                # full-tip escalation. If we managed to try chunk_n=
                # 211 once before reverting to 422 and exceeding the
                # budget, the next cycle should start at 211/2=105,
                # not at 422/2=211 (which would loop us right back
                # into the same revert).
                remember_n = smallest_attempted_n or chunk_n
                _remember_failed_chunk_n(project_dir, remember_n)
                lift_merge.trace(
                    f'[sync-trace] push budget exceeded '
                    f'({push_budget_s}s) — giving up; '
                    f'pending commits requeued for next sync '
                    f'(remembered chunk_n={remember_n}; next drain '
                    f'cycle will start at {_hint_chunk_n(project_dir)})')
                _cleanup_temp_ref()
                result.add(S.SYNC_GIVING_UP_TRANSIENT,
                           budget_s=push_budget_s,
                           commits_pending=_count_commits_between(
                               repo, remote_sha, local_sha))
                _add_push_failure(result, exc)
                return result
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Persist the smallest chunk_n attempted this call so
                # the next drain cycle's hint reflects the actual
                # floor, not the post-revert ceiling. Same rationale
                # as the budget-exceeded path above.
                remember_n = smallest_attempted_n or chunk_n
                _remember_failed_chunk_n(project_dir, remember_n)
                lift_merge.trace(
                    f'[sync-trace] consecutive_failures cap reached '
                    f'(remembered chunk_n={remember_n}; next drain '
                    f'cycle will start at {_hint_chunk_n(project_dir)})')
                _cleanup_temp_ref()
                _add_push_failure(result, exc)
                return result
            # Re-fetch first — if the remote actually moved,
            # this isn't a network-class failure but a race with
            # another peer's push. Different recovery path.
            try:
                with _socket_timeout(_FETCH_TIMEOUT_S):
                    _fetch_origin(repo, username, token)
                new_remote = _read_ref(remote_ref)
            except Exception as ex:
                lift_merge.trace(
                    f'[sync-trace] retry fetch failed: {ex!r}')
                new_remote = remote_sha
            # If the push raised DivergedBranches, the exception itself
            # carries the server's current view of the ref — more
            # reliable than the re-fetch result when DNS is flapping
            # and ``porcelain.fetch`` raised IncompleteRead so the
            # local tracking ref stayed frozen at clone-time. Adopt
            # the server's value, both for this iteration's ancestor
            # logic and for the on-disk mirror so the next iteration
            # doesn't re-discover it.
            diverged_old = _extract_diverged_remote(exc)
            if diverged_old and diverged_old != new_remote:
                lift_merge.trace(
                    f'[sync-trace] DivergedBranches reports '
                    f'remote tip={diverged_old[:8]!r}; '
                    f'using over fetch result {(new_remote or b"")[:8]!r}')
                new_remote = diverged_old
                try:
                    repo.refs[remote_ref] = diverged_old
                except Exception as ex:
                    lift_merge.trace(
                        f'[sync-trace] DivergedBranches mirror '
                        f'update failed: {ex!r}')
            lift_merge.trace(
                f'[sync-trace] retry fetch new_remote={new_remote!r} '
                f'remote_sha={remote_sha!r} local_sha={local_sha!r}')
            non_ff = _is_non_ff_rejection(exc)
            # Reconcile when EITHER the fetch revealed a moved remote
            # (race with a concurrent pusher) OR the rejection itself
            # is a non-fast-forward (the server has something we
            # don't, regardless of whether our refs/remotes mirror
            # caught up yet — fetch may have already brought the
            # new tip down on a prior iteration). Require
            # ``new_remote`` truthy in both branches so the
            # ancestor checks below have a real SHA to walk — the
            # ``or non_ff`` extension would otherwise let us
            # enter with ``new_remote=None`` on a freshly-cloned
            # repo with no tracking ref written yet, and the
            # ancestor walker would raise.
            if new_remote and ((new_remote != remote_sha) or non_ff):
                # Four cases — disjoint structure:
                # remote==local nothing to do, remote-is-ancestor
                # local still ahead, local-is-ancestor remote
                # advanced (fast-forward), otherwise diverged
                # (merge). Field log 2026-05-18 showed that
                # missing the second ancestor check produced no-op
                # merge commits cluttering history.
                #
                # The equal-local-new_remote branch claims PUSHED
                # for BOTH race and non-FF triggers: in both cases
                # the server's view of ``branch`` matches our local
                # tip, so we're in sync regardless of how we got
                # here. (0.43.13 had a "bail with PUSH_FAILED if
                # non_ff" guard here that turned a real adaptive-
                # batching recovery — target_sha was an ancestor
                # of a server that had concurrently advanced to
                # local_sha — into a spurious failure. Removed in
                # 0.43.16.)
                if local_sha == new_remote:
                    # Already in sync — the failed push was
                    # spurious (e.g. server saw it succeed
                    # then dropped our connection before ack)
                    # OR adaptive-batching pushed an ancestor of
                    # what the server already holds.
                    _cleanup_temp_ref()
                    repo.refs[remote_ref] = new_remote
                    remote_sha = new_remote
                    result.add(S.PUSHED, branch=branch)
                    return result
                # Detect the pathological case before the ancestor
                # fan-out: server rejected the push as non-FF AND the
                # re-fetch saw no remote movement. The server's
                # rejection is authoritative — it has something we
                # can't see (or our intermediate-target pack didn't
                # include the full ancestry to verify FF). The local
                # ancestor walk below will say "local still ahead"
                # because nothing in local state has changed, and
                # without escalation we'd retry the same target
                # forever.
                nonff_no_progress = (
                    non_ff and new_remote == remote_sha)
                if _is_ancestor(repo, new_remote, local_sha):
                    lift_merge.trace(
                        '[sync-trace] retry: local still ahead — '
                        'push retry only')
                    remote_sha = new_remote
                    if nonff_no_progress:
                        # Server disagrees with our ancestor check.
                        # Escalate stepwise: try the full local tip
                        # first, then a forced merge, then bail.
                        if target_sha != local_sha:
                            # Pushing an intermediate via temp ref.
                            # Server may have refused because the
                            # pack didn't demonstrate the FF chain.
                            # Try the full local tip via the
                            # standard refs/heads/<branch> refspec,
                            # which bypasses the temp-ref pack-
                            # negotiation path.
                            lift_merge.trace(
                                '[sync-trace] retry: non-FF with no '
                                'remote movement on intermediate '
                                'target — reverting to full local '
                                'tip')
                            target_sha = local_sha
                            working_batch_n = None
                            continue
                        if nonff_forced_merges >= 1:
                            # We already merged once against this
                            # same remote_sha and the server still
                            # rejects with no fetch movement.
                            # Cannot reconcile from here — surface
                            # PUSH_FAILED. (One escalation is
                            # enough: if the merge produced a
                            # descendant of remote_sha and the
                            # server still says no, the server's
                            # view of its ref disagrees with the
                            # ref-advertisement it gave us. A peer
                            # branch-protection rule or hosted-
                            # repo policy is in play; retrying
                            # won't help.)
                            lift_merge.trace(
                                '[sync-trace] retry: non-FF persists '
                                'after forced merge — giving up')
                            _cleanup_temp_ref()
                            _add_push_failure(result, exc)
                            return result
                        # First non-FF-no-progress hit on the full
                        # local tip: trust the server's rejection
                        # over our ancestor check and force a
                        # merge against the remote we have on
                        # hand. ``_merge_diverged`` walks both
                        # histories from a common base — if our
                        # local already descends from remote_sha
                        # it'll produce essentially the same tree,
                        # and the resulting merge commit at least
                        # changes target_sha so we're not pushing
                        # the same SHA the server already said
                        # no to.
                        lift_merge.trace(
                            '[sync-trace] retry: non-FF on full '
                            'local tip — forcing merge against '
                            'remote_sha')
                        mem_block = _check_memory_for_merge()
                        if mem_block is not None:
                            lift_merge.trace(
                                f'[sync-trace] forced merge skipped '
                                f'(mem_available='
                                f'{mem_block.params.get("mem_available_mb")}MB '
                                f'< min='
                                f'{mem_block.params.get("min_required_mb")}MB)')
                            _cleanup_temp_ref()
                            result.statuses.append(mem_block)
                            _add_push_failure(result, exc)
                            return result
                        try:
                            merged_sha, _ = _merge_diverged(
                                repo, project_dir, branch,
                                local_sha, remote_sha)
                            local_sha = merged_sha
                            target_sha = local_sha
                            working_batch_n = None
                            backoff_s = 1.0
                            nonff_forced_merges += 1
                        except Exception as ex:
                            lift_merge.trace(
                                f'[sync-trace] forced merge '
                                f'failed: {ex!r}')
                            _cleanup_temp_ref()
                            _add_push_failure(result, ex)
                            return result
                        continue
                    # Normal "still ahead" case (remote did move,
                    # but to something our local descends from):
                    # keep target_sha + working_batch_n. Throwing
                    # them away resets adaptive-batching progress
                    # on every concurrent peer push — observed in
                    # field log baf 2026-05-19 where chunk_n=89
                    # repeatedly walked back to chunk_n=719 every
                    # retry cycle.
                    continue
                elif _is_ancestor(repo, local_sha, new_remote):
                    lift_merge.trace(
                        '[sync-trace] retry: remote advanced; '
                        'fast-forward local')
                    _cleanup_temp_ref()
                    prev_local_sha = local_sha
                    repo.refs[branch_ref] = new_remote
                    _apply_tree_to_workdir(
                        repo, project_dir, prev_local_sha, new_remote)
                    local_sha = new_remote
                    remote_sha = new_remote
                    result.add(S.PUSHED, branch=branch)
                    return result
                else:
                    lift_merge.trace(
                        '[sync-trace] retry: diverged; merging')
                    mem_block = _check_memory_for_merge()
                    if mem_block is not None:
                        lift_merge.trace(
                            f'[sync-trace] retry merge skipped '
                            f'(mem_available='
                            f'{mem_block.params.get("mem_available_mb")}MB '
                            f'< min='
                            f'{mem_block.params.get("min_required_mb")}MB)')
                        _cleanup_temp_ref()
                        result.statuses.append(mem_block)
                        _add_push_failure(result, exc)
                        return result
                    try:
                        merged_sha, _ = _merge_diverged(
                            repo, project_dir, branch,
                            local_sha, new_remote)
                        local_sha = merged_sha
                        remote_sha = new_remote
                        lift_merge.trace(
                            f'[sync-trace] retry merge done '
                            f'merged_sha={merged_sha!r}')
                    except Exception as ex:
                        lift_merge.trace(
                            f'[sync-trace] retry merge failed: {ex!r}')
                        _cleanup_temp_ref()
                        _add_push_failure(result, ex)
                        return result
                # Diverged-and-merged path: local chain changed,
                # so reset adaptive-batching state. The FF and
                # still-ahead branches handle their own state
                # (FF returns; still-ahead preserves).
                # ``nonff_forced_merges`` also resets here: this
                # was real reconciliation progress (remote moved
                # AND we merged it in), distinct from the still-
                # ahead nonff-no-progress escalation path that
                # bumps the counter.
                target_sha = local_sha
                working_batch_n = None
                backoff_s = 1.0
                nonff_forced_merges = 0
                continue
            # Remote unchanged. Genuine network-class failure (or
            # unfamiliar exception). Back off, then decide whether
            # to halve.
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 16.0)
            if not _is_network_push_failure(exc):
                # Unfamiliar exception — don't halve (might mask
                # a real bug); retry at the same target after the
                # backoff. If it keeps failing,
                # ``consecutive_failures`` bottoms out and we bail.
                lift_merge.trace(
                    '[sync-trace] retry at same target_sha '
                    '(non-network exception)')
                continue
            if _is_dns_resolution_failure(exc):
                # DNS-class failure. Halving ``chunk_n`` is the wrong
                # response: pack size has zero effect on whether the
                # resolver returns an address. Field log baf 2026-
                # 05-20 showed 302 → 151 → … → 1 walked all the way
                # down on pure ``NameResolutionError`` while the real
                # answer was "wait for DNS to come back." Hold target
                # + ``working_batch_n`` so when DNS recovers we
                # resume on the chunk size that previously worked.
                lift_merge.trace(
                    '[sync-trace] retry at same target_sha '
                    '(DNS resolution failure — no halving)')
                continue
            if chunk_n <= 1:
                # Already pushing one commit at a time and it
                # still doesn't fit through. No more shrinking
                # possible — fail out.
                lift_merge.trace(
                    '[sync-trace] retry: at minimum chunk_n=1, '
                    'network failure persists — giving up')
                _cleanup_temp_ref()
                _add_push_failure(result, exc)
                return result
            new_n = max(1, chunk_n // 2)
            lift_merge.trace(
                f'[sync-trace] retry: halving chunk_n '
                f'{chunk_n} → {new_n}')
            target_sha = _pick_intermediate_sha(
                repo, remote_sha, local_sha, new_n)
            # Until we find a working size, don't lock it in.
            working_batch_n = None
            continue
    _cleanup_temp_ref()
    return result


_KNOWN_PATH_PREFIXES = (
    'audio/', 'audio\\',
    'images/', 'images\\',
    '.git/', '.git\\',
    '.azt_atomic_pending/', '.azt_atomic_pending\\',
    '.azt-collab/', '.azt-collab\\',
    # .azt/ is the project-shared KV + slot-claim subtree
    # (project_kv.py, 2026-05-28). _stage_all commits everything
    # under it; per-path merge resolvers at the top of repo.py
    # expect .azt/slots/*.txt and .azt/kv/*.txt to be in the
    # working tree. Omitting it here made every commit pass spam
    # [data-loss-risk] and fire S.DATA_LOSS_RISK for files that
    # are in fact being backed up — fixed 0.50.25.
    '.azt/', '.azt\\',
)
_KNOWN_TOPLEVEL_FILES = frozenset((
    '.gitignore', 'README', 'README.md', '.gitattributes',
))


_COMMIT_REPEATEDLY_FAILED_THRESHOLD = 2


def _bump_commit_failure_count(project_dir, error_msg=''):
    """Increment the persisted commit-failure counter for the project
    registered at ``project_dir``.

    Also stamps ``last_commit_failure_at`` (unix timestamp) and
    ``last_commit_error`` (the dulwich message) so the scheduler's
    retry loop can backoff-throttle re-attempts and peers polling
    ``project_status`` can surface a useful explanation without
    parsing the daemon log.

    Lives in ``projects.json :: <langcode>.commit_failure_count``
    so the count survives daemon restarts. The reverse lookup
    keeps the helper callable from the working_dir-keyed APIs
    (``sync_repo``, ``commit_audio_and_sync``) without
    threading langcode through every signature. Returns the
    post-increment value (or 0 when the project isn't registered
    yet — typical on first publish, where ``init_repo`` runs
    before ``register``).
    """
    from . import projects
    import time
    langcode = projects.find_langcode_by_working_dir(project_dir)
    if not langcode:
        return 0
    try:
        data = projects._load_raw()
    except Exception:
        return 1   # be loud rather than swallow — caller will surface
    entry = dict(data.get(langcode, {}))
    n = int(entry.get('commit_failure_count', 0)) + 1
    entry['commit_failure_count'] = n
    entry['last_commit_failure_at'] = time.time()
    if error_msg:
        entry['last_commit_error'] = error_msg
    data[langcode] = entry
    try:
        projects._save_raw(data)
    except Exception:
        pass
    return n


def _clear_commit_failure_count(project_dir):
    """Reset the persisted commit-failure counter (and its
    accompanying timestamp + error message) on a successful
    commit. Safe to call when the counter is already zero or the
    project isn't registered."""
    from . import projects
    langcode = projects.find_langcode_by_working_dir(project_dir)
    if not langcode:
        return
    try:
        data = projects._load_raw()
    except Exception:
        return
    entry = dict(data.get(langcode, {}))
    changed = False
    for key in ('commit_failure_count', 'last_commit_failure_at',
                'last_commit_error'):
        if entry.pop(key, None) is not None:
            changed = True
    if not changed:
        return
    data[langcode] = entry
    try:
        projects._save_raw(data)
    except Exception:
        pass


def _surface_commit_failure(result, project_dir, exc):
    """Bookkeep a COMMIT_FAILED on ``result`` plus the persisted
    counter. After ``_COMMIT_REPEATEDLY_FAILED_THRESHOLD`` (2)
    successive failures, ALSO add ``S.COMMIT_REPEATEDLY_FAILED``
    so the peer's UI surfaces a data-loss-class toast rather
    than the more routine single-attempt ``COMMIT_FAILED`` line.
    Note: there is no in-process retry on commit failure; the
    next commit attempt arrives whenever the peer next calls
    ``commit_audio_and_sync`` (typically after the next recording
    or sync gesture).
    The catchup-commit pattern (one big commit after a long
    failure streak — N stranded recordings landing as a single
    blob) is exactly what the threshold catches: each prior
    failed attempt bumps the counter, and the second-or-later
    failure surfaces the loud status so the user is told to
    investigate before more files pile up uncommitted.
    """
    err_str = str(exc)
    result.add(S.COMMIT_FAILED, error=err_str)
    n = _bump_commit_failure_count(project_dir, error_msg=err_str)
    if n >= _COMMIT_REPEATEDLY_FAILED_THRESHOLD:
        result.add(S.COMMIT_REPEATEDLY_FAILED,
                   count=n, error=err_str)


def _surface_uncommittable(result, repo):
    """Read the uncommittable list ``_stage_audio`` stashed on the
    repo object and convert it to a ``DATA_LOSS_RISK`` status on
    ``result``. No-op when the list is empty / missing.

    ``count`` and ``sample`` (up to 5 paths) are carried as
    params so the peer's renderer can produce a useful toast /
    banner without parsing the daemon log."""
    uncommittable = getattr(repo, '_azt_uncommittable', None) or []
    if uncommittable:
        result.add(S.DATA_LOSS_RISK,
                   count=len(uncommittable),
                   sample=uncommittable[:5])


def _detect_uncommittable(project_dir):
    """Walk project_dir for files that won't get staged by
    _stage_all / _stage_audio because they sit outside the
    known directories (audio/, images/, *.lift, .git/, etc.).

    Returns a list of relative paths. Empty list is the common
    case — a peer that uses ``LiftHandle`` / ``MediaHandle``
    correctly always writes under ``audio/`` or ``images/`` or
    the LIFT file itself. A non-empty list means a peer wrote
    to an unexpected location and the file will silently never
    be backed up — a data-loss-class risk the daemon must
    surface loudly.
    """
    out = []
    for root, _dirs, files in os.walk(project_dir):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, project_dir)
            # Normalise to forward-slash for prefix checks.
            rel_check = rel.replace('\\', '/')
            if rel.endswith('.lift') and '/' not in rel_check:
                continue
            if any(rel_check.startswith(p.replace('\\', '/'))
                   for p in _KNOWN_PATH_PREFIXES):
                continue
            if '/' not in rel_check and name in _KNOWN_TOPLEVEL_FILES:
                continue
            out.append(rel)
    return out


def _stage_audio(repo, project_dir):
    """Stage only new/modified audio files (audio/ + images/ + .lift).

    Verbose-logs counts so remote-tester reports with only the
    daemon log file can disambiguate "user recorded 1000 but only
    146 committed" between:

    - peer write path dropped bytes (on-disk count low),
    - ``porcelain.status`` truncates large untracked sets
      (on-disk count ≫ status.untracked count),
    - sync ran too rarely / files sat untracked between syncs
      (consistent gap across multiple sync passes).

    Also flags any file in project_dir that isn't under our known
    directories — that's a peer writing to an unexpected location
    and is a data-loss class risk (the file will never reach git).
    Emits ``[data-loss-risk] <rel_path>`` per file plus a
    summary status code peers can surface to the user.
    """
    from dulwich import porcelain
    status = porcelain.status(repo)
    paths = []

    def _is_audio_or_lift(p):
        s = p if isinstance(p, str) else p.decode('utf-8', errors='replace')
        return (s.startswith('audio/') or s.startswith('images/')
                or s == 'audio' or s == 'images'
                or s.endswith('.lift'))

    for f in status.unstaged:
        if _is_audio_or_lift(f):
            paths.append(_bytes_path(f))

    for f in status.untracked:
        rel = f if isinstance(f, str) else f.decode('utf-8', errors='replace')
        if not _is_audio_or_lift(rel):
            continue
        full = os.path.join(project_dir, rel)
        if os.path.isfile(full):
            paths.append(_bytes_path(rel))
        elif os.path.isdir(full):
            for root, _dirs, files in os.walk(full):
                for name in files:
                    fp = os.path.join(root, name)
                    rp = os.path.relpath(fp, project_dir)
                    paths.append(_bytes_path(rp))

    # Independent on-disk walk for diagnostic comparison vs.
    # status.untracked. If these diverge substantially, dulwich
    # is missing files (its status walk truncated / cached out of
    # date / index corruption), not our filter.
    audio_dir = os.path.join(project_dir, 'audio')
    images_dir = os.path.join(project_dir, 'images')
    on_disk_audio = sum(
        1 for _root, _dirs, files in os.walk(audio_dir) for _ in files
    ) if os.path.isdir(audio_dir) else 0
    on_disk_images = sum(
        1 for _root, _dirs, files in os.walk(images_dir) for _ in files
    ) if os.path.isdir(images_dir) else 0
    status_unstaged = len(status.unstaged)
    status_untracked = len(status.untracked)
    import sys

    # Theory-2 detection: anything under project_dir that isn't
    # in a known directory and isn't the LIFT itself is a peer
    # writing to an unexpected location — file will never be
    # committed (won't be backed up). Log per-file at high
    # severity AND attach a one-line summary to the diagnostic
    # status line, so a daemon log shared by the tester contains
    # both the count and the specific paths a maintainer can act
    # on. Suppress per-file logging if there are many (cap at
    # the first 20) to avoid drowning the log.
    uncommittable = _detect_uncommittable(project_dir)
    if uncommittable:
        for rel in uncommittable[:20]:
            print(f'[data-loss-risk] uncommittable file in '
                  f'project_dir: {rel!r}',
                  file=sys.stderr, flush=True)
        if len(uncommittable) > 20:
            print(f'[data-loss-risk] ... and '
                  f'{len(uncommittable) - 20} more',
                  file=sys.stderr, flush=True)

    print(f'[stage-audio] project_dir={project_dir!r} '
          f'on_disk_audio={on_disk_audio} '
          f'on_disk_images={on_disk_images} '
          f'status.unstaged={status_unstaged} '
          f'status.untracked={status_untracked} '
          f'paths_to_add={len(paths)} '
          f'uncommittable={len(uncommittable)}',
          file=sys.stderr, flush=True)

    if paths:
        porcelain.add(repo, paths=paths)
    # Stash the uncommittable list on the repo object for the
    # caller (``_commit_audio_and_sync_locked`` /
    # ``_sync_repo_locked``) to read and surface as a Result
    # status. Repo objects are short-lived (one per sync call),
    # so attaching is safe.
    repo._azt_uncommittable = uncommittable
    return len(paths)


def commit_audio_and_sync(project_dir, contributor_name, username, token):
    """Stage + commit audio files, then sync if internet is available.
    Returns Result."""
    try:
        with project_lock(project_dir):
            with _track_opened_repos():
                return _commit_audio_and_sync_locked(
                    project_dir, contributor_name, username, token)
    except LockTimeout:
        return _busy_result(project_dir)


def _commit_audio_and_sync_locked(project_dir, contributor_name,
                                  username, token):
    from dulwich import porcelain
    import sys
    print(f'[commit-audio] start project_dir={project_dir!r} '
          f'contributor={contributor_name!r}',
          file=sys.stderr, flush=True)
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        print(f'[commit-audio] NO_REPO project_dir={project_dir!r}',
              file=sys.stderr, flush=True)
        result.add(S.NO_REPO)
        return result

    n = _stage_audio(repo, project_dir)
    _surface_uncommittable(result, repo)
    print(f'[commit-audio] _stage_audio returned n={n}',
          file=sys.stderr, flush=True)
    if n == 0:
        print(f'[commit-audio] NO_AUDIO — nothing new to commit',
              file=sys.stderr, flush=True)
        # Nothing new to commit; still try to sync if online
        if _has_internet():
            try:
                _ensure_ssl()
                repo.get_config().get(
                    (b'remote', b'origin'), b'url'
                ).decode('utf-8')
                return sync_repo(project_dir, username, token, contributor_name)
            except Exception:
                pass
        result.add(S.NO_AUDIO)
        return result

    # Commit
    author = _default_author(contributor_name)
    committer = _app_committer()
    try:
        commit_sha = porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=committer,
        )
        try:
            sha_str = commit_sha.decode('ascii', errors='replace')[:12]
        except Exception:
            sha_str = repr(commit_sha)[:14]
        print(f'[commit-audio] committed n={n} sha={sha_str}',
              file=sys.stderr, flush=True)
        _clear_commit_failure_count(project_dir)
    except Exception as exc:
        print(f'[commit-audio] COMMIT_FAILED error={exc!r}',
              file=sys.stderr, flush=True)
        _surface_commit_failure(result, project_dir, exc)
        return result

    if not _has_internet():
        result.add(S.COMMITTED_OFFLINE)
        return result

    try:
        _ensure_ssl()
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        result.add(S.COMMITTED_NO_REMOTE)
        return result
    remote_url = wan_url(remote_url)

    try:
        branch = porcelain.active_branch(repo).decode('utf-8', errors='replace')
    except Exception:
        branch = 'main'

    # Pull first (fetch + merge) so push won't be rejected. Remote
    # NAME when possible, wan-normalized URL when the stored origin
    # is ssh-shaped — see _pull_origin.
    try:
        _pull_origin(repo, username, token)
    except Exception as exc:
        if _is_http_403(exc):
            # Local commit is safe; surface the access issue
            result.statuses.append(diagnose_403(token, remote_url))
            return result
        # Non-fatal — local commit is safe, push may still work
        print(f'[auto-sync] pull warning: {exc}')

    # Push
    refspec = _enc(f'refs/heads/{branch}:refs/heads/{branch}')
    try:
        porcelain.push(
            repo, remote_url, refspec,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        result.add(S.COMMITTED_AND_PUSHED, n=n)
    except Exception as exc:
        if _is_http_403(exc):
            result.statuses.append(diagnose_403(token, remote_url))
        else:
            _add_push_failure(result, exc)

    return result
