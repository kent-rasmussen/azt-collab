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
import time

from . import config as _config
from . import status as S
from .status import Result, Status
from .locks import project_lock, LockTimeout
from .net import _ensure_ssl, _has_internet
from .auth import add_collaborator, diagnose_403
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

    # Write changed / added files.
    for path, content in new_blobs.items():
        if old_blobs.get(path) == content:
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
    # ``repo.reset_index`` is the canonical dulwich API; if it
    # raises (older dulwich, IO error), fall back to re-staging
    # the working tree.
    try:
        repo.reset_index(new_commit.tree)
        return
    except Exception as ex:
        lift_merge.trace(
            f'[sync-trace] _apply_tree_to_workdir reset_index '
            f'failed, falling back to _stage_all: {ex!r}')
    try:
        _stage_all(repo, project_dir)
    except Exception as ex:
        lift_merge.trace(
            f'[sync-trace] _apply_tree_to_workdir index fallback '
            f'failed: {ex!r}')


def _walk_tree(repo, tree_sha, prefix=b''):
    """Return dict[path-as-str → blob bytes] for every file under *tree_sha*."""
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
            try:
                blob = repo[sha]
                out[full.decode('utf-8', errors='replace')] = blob.data
            except KeyError:
                pass
    return out


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


def _merge_diverged(repo, project_dir, branch, local_sha, remote_sha):
    """Three-way merge ours (local_sha) and theirs (remote_sha) into the
    working tree. .lift files merge via lift_merge; other paths use
    take-changed-side-or-ours semantics. Creates a merge commit with
    both parents. Returns (commit_sha, conflicts_list)."""
    from dulwich import porcelain

    base_sha = _find_merge_base(repo, local_sha, remote_sha)
    head_commit = repo[local_sha]
    remote_commit = repo[remote_sha]
    base_commit = repo[base_sha] if base_sha else None

    base_blobs = _walk_tree(repo, base_commit.tree) if base_commit else {}
    head_blobs = _walk_tree(repo, head_commit.tree)
    remote_blobs = _walk_tree(repo, remote_commit.tree)

    all_paths = set(base_blobs) | set(head_blobs) | set(remote_blobs)
    conflicts = []
    merged_writes = {}    # path → bytes
    deletes = []          # paths to remove

    for path in sorted(all_paths):
        b = base_blobs.get(path)
        o = head_blobs.get(path)
        t = remote_blobs.get(path)

        if o is None and t is None:
            deletes.append(path)
            continue

        if path.endswith('.lift') and o is not None and t is not None and o != t:
            mr = lift_merge.three_way_merge(b or b'', o, t, path=path)
            merged_writes[path] = mr.merged_bytes
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
                        base_bytes=b or b'', ours_bytes=o,
                        theirs_bytes=t,
                        merged_bytes=mr.merged_bytes,
                        conflict_fields=_c.fields)
                    break   # one dump per merged file is enough
            continue

        if o == t:
            merged_writes[path] = (o if o is not None else t)
            if merged_writes[path] is None:
                merged_writes.pop(path, None)
                deletes.append(path)
            continue
        if o == b:
            # Only theirs changed — take theirs (or accept their delete)
            if t is None:
                deletes.append(path)
            else:
                merged_writes[path] = t
            continue
        if t == b:
            # Only ours changed — take ours
            if o is None:
                deletes.append(path)
            else:
                merged_writes[path] = o
            continue
        # Both changed differently for a non-LIFT file → take ours,
        # surface the conflict for the commit message.
        if o is not None:
            merged_writes[path] = o
        elif t is not None:
            merged_writes[path] = t
        conflicts.append(lift_merge.Conflict(
            path=path, guid='', kind='non-lift-modify-modify'))

    # Apply to the working tree
    for path, content in merged_writes.items():
        full = os.path.join(project_dir, path)
        os.makedirs(os.path.dirname(full) or project_dir, exist_ok=True)
        with open(full, 'wb') as f:
            f.write(content)
    for path in deletes:
        full = os.path.join(project_dir, path)
        try:
            os.remove(full)
        except OSError:
            pass

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


def _get_repo(project_dir):
    """Return a dulwich Repo or None."""
    try:
        from dulwich.repo import Repo
        return Repo(project_dir)
    except Exception:
        return None


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
    """
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

    from urllib.parse import urlparse
    parsed = urlparse(remote_url)
    host = parsed.hostname or ''
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if len(parts) < 2:
        return False, Status(S.REMOTE_CREATE_FAILED,
                             {'error': f'cannot parse owner/repo from {remote_url}'})
    owner, repo_name = parts[0], parts[1]

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
        return True, None   # Unknown host — assume repo exists, let push fail

    created = False
    try:
        req = Request(api_url, data=payload, headers=headers, method='POST')
        urlopen(req, timeout=30)
        created = True
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        # 422 = already exists (GitHub), 400 = already exists (GitLab)
        if e.code in (422, 400) and 'already' in body.lower():
            pass   # Already exists — fine
        else:
            return False, Status(S.REMOTE_CREATE_FAILED,
                                 {'error': f'{e.code}: {body[:200]}'})
    except (URLError, OSError) as e:
        return False, Status(S.REMOTE_CREATE_FAILED, {'error': str(e)})

    # Add collaborator on GitHub repos
    collaborator = _config.get()['collaborator']
    if 'github.com' in host and collaborator:
        try:
            add_collaborator(owner, repo_name, collaborator, token)
        except Exception as ex:
            print(f'[collab] add collaborator warning: {ex}')

    if created:
        return True, Status(S.REMOTE_REPO_CREATED,
                            {'owner_repo': f'{owner}/{repo_name}'})
    return True, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def repo_status_summary(project_dir):
    """
    Return (branch, remote_url, n_changes, commits_ahead) describing
    the project directory, or None if it is not a git repository.
    (Not a Result — this is a raw accessor for UI status indicators.)

    ``commits_ahead`` is the number of commits on the current branch
    not yet pushed to ``refs/remotes/origin/<branch>``. Computed from
    the *local* cache of the remote ref (no network round-trip) so
    the value is whatever is true given the last fetch / push:

    - No origin remote configured → 0 (publish hasn't happened)
    - Origin configured but never pushed → 0 (no remote ref to
      compare against; the indicator should read OK rather than
      double-counting the unpushed initial commit as "behind")
    - Local commits since last push → N>0 (the case peers display
      as ``(+n)``)

    A stale cache (peer was offline since last commit, didn't fetch)
    can under-report; that's acceptable per the recorder's UX
    contract — the indicator falls back to OK rather than guessing
    against unobserved remote state. Filed by azt_recorder 1.37.6 in
    ``azt_collab_client/NOTES_TO_DAEMON.md``.
    """
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
        except Exception:
            n = 0

        commits_ahead = _count_commits_ahead(repo, branch)

        return branch, remote_url, n, commits_ahead
    except Exception:
        return None


def _count_commits_ahead(repo, branch):
    """Count commits on ``refs/heads/<branch>`` not yet on
    ``refs/remotes/origin/<branch>`` using the local ref cache. Any
    failure (detached HEAD, no remote ref cached, walker error)
    returns 0 — the indicator's contract is "OK on uncertainty."""
    try:
        local_ref = b'refs/heads/' + branch.encode()
        remote_ref = b'refs/remotes/origin/' + branch.encode()
        try:
            local_sha = repo.refs[local_ref]
        except KeyError:
            return 0
        try:
            remote_sha = repo.refs[remote_ref]
        except KeyError:
            return 0
        if local_sha == remote_sha:
            return 0
        walker = repo.get_walker(
            include=[local_sha], exclude=[remote_sha])
        return sum(1 for _ in walker)
    except Exception:
        return 0


def init_repo(project_dir, remote_url, username, token,
              branch='main', contributor_name=''):
    """Initialize a git repo, commit everything, set remote, push.
    Returns a Result.

    Pre-0.40 ``contributor_name`` defaulted to the literal
    ``'Recorder'`` so a missing arg silently produced
    ``Recorder <recorder@device>`` commits. As of 0.40 the daemon's
    endpoints refuse the call upstream when contributor is unset
    (``S.CONTRIBUTOR_UNSET``); the default here is empty for
    test convenience but production callers always pass a real
    name."""
    _ensure_ssl()
    try:
        with project_lock(project_dir):
            return _init_repo_locked(project_dir, remote_url, username,
                                     token, branch, contributor_name)
    except LockTimeout:
        return _busy_result(project_dir)


def _init_repo_locked(project_dir, remote_url, username, token,
                      branch, contributor_name):
    from dulwich import porcelain
    result = Result()

    repo = _get_repo(project_dir)
    if repo is None:
        repo = porcelain.init(project_dir)
        result.add(S.INITIALIZED)
    else:
        result.add(S.ALREADY_INITIALIZED)

    gitignore = os.path.join(project_dir, '.gitignore')
    if not os.path.exists(gitignore):
        with open(gitignore, 'w') as fh:
            fh.write('__pycache__/\n*.pyc\n.buildozer/\nenv/\n.DS_Store\nimage_cache/\n')
        result.add(S.GITIGNORE_CREATED)

    _stage_all(repo, project_dir)
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
        return result

    try:
        porcelain.push(
            repo, remote_url,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        result.add(S.PUSHED, url=remote_url, branch=branch)
    except Exception as exc:
        _add_push_failure(result, exc)

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
        import shutil
        shutil.rmtree(dest_dir)
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
    else:
        result.add(S.LIFT_NOT_FOUND)
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
        # Remote NAME, not URL — see _sync_repo_locked for why.
        porcelain.pull(
            repo, 'origin',
            username=username, password=token,
            errstream=io.BytesIO(),
        )
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
            return _commit_repo_locked(project_dir, contributor_name)
    except LockTimeout:
        return _busy_result(project_dir)


def _commit_repo_locked(project_dir, contributor_name):
    from dulwich import porcelain
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        result.add(S.NOT_A_REPO)
        return result
    _commit_step_locked(repo, project_dir, contributor_name, result)
    return result


def _commit_step_locked(repo, project_dir, contributor_name, result):
    """Stage + commit on an already-opened repo. Mutates *result*
    in place (adds COMMITTED_LOCAL / NOTHING_TO_COMMIT / etc.).
    Caller holds the project lock."""
    from dulwich import porcelain
    _stage_all(repo, project_dir)
    # Diagnostic: walk for any file outside the staging filter
    # (peer-write-to-unexpected-location class). The walk runs
    # both here and inside ``_stage_audio`` because either entry
    # point can be the one a peer hits.
    uncommittable = _detect_uncommittable(project_dir)
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
            porcelain.commit(
                repo,
                message=_enc(f'Audio recordings by {contributor_name}'),
                author=author, committer=committer,
            )
            result.add(S.COMMITTED_LOCAL)
            _clear_commit_failure_count(project_dir)
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
        ).decode('utf-8')
    except KeyError:
        result.add(S.NO_REMOTE)
        return result
    _push_step_locked(repo, project_dir, username, token, remote_url, result)
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
        ).decode('utf-8')
    except KeyError:
        result.add(S.NO_REMOTE)
        return result

    # Stage + commit local changes BEFORE the merge so they're a
    # proper commit on local <branch>, not just dirty working tree.
    _commit_step_locked(repo, project_dir, contributor_name, result)
    _push_step_locked(repo, project_dir, username, token, remote_url, result)
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
    """Return the SHA *n* commits forward from ``base_sha`` along the
    chain to ``tip_sha`` (1-indexed). Used by adaptive push batching
    to pick a partial-advance target when the full push didn't fit
    through the network. ``n >= total_commits`` returns ``tip_sha``.
    Any walker error returns ``tip_sha`` (safest fallback — pushing
    the tip just retries the original full transaction)."""
    if not base_sha or not tip_sha or base_sha == tip_sha or n <= 0:
        return tip_sha
    try:
        walker = repo.get_walker(
            include=[tip_sha], exclude=[base_sha])
        # Walker yields newest-first; reverse so commits[0] is the
        # immediate child of base_sha.
        commits = [entry.commit.id for entry in walker]
    except Exception:
        return tip_sha
    if not commits:
        return tip_sha
    commits.reverse()
    return commits[min(n, len(commits)) - 1]


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

    # Fetch (no merge yet).
    #
    # Pass the remote NAME (``'origin'``), not the URL. Dulwich's
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
    lift_merge.trace(f'[sync-trace] fetch begin remote={remote_url!r}')
    try:
        with _socket_timeout(_FETCH_TIMEOUT_S):
            porcelain.fetch(
                repo, 'origin',
                username=username, password=token,
                errstream=io.BytesIO(),
            )
        lift_merge.trace('[sync-trace] fetch done')
    except Exception as exc:
        if _is_http_403(exc):
            result.statuses.append(diagnose_403(token, remote_url))
            return result
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
        except Exception as exc:
            lift_merge.trace(f'[sync-trace] merge_diverged FAILED: {exc}')
            result.add(S.PULL_FAILED,
                       error=f'merge failed: {_format_push_error(exc)}')
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
                # Queue cleared.
                _cleanup_temp_ref()
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
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                _cleanup_temp_ref()
                _add_push_failure(result, exc)
                return result
            # Re-fetch first — if the remote actually moved,
            # this isn't a network-class failure but a race with
            # another peer's push. Different recovery path.
            try:
                with _socket_timeout(_FETCH_TIMEOUT_S):
                    porcelain.fetch(
                        repo, 'origin',
                        username=username, password=token,
                        errstream=io.BytesIO(),
                    )
                new_remote = _read_ref(remote_ref)
            except Exception as ex:
                lift_merge.trace(
                    f'[sync-trace] retry fetch failed: {ex!r}')
                new_remote = remote_sha
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

    try:
        branch = porcelain.active_branch(repo).decode('utf-8', errors='replace')
    except Exception:
        branch = 'main'

    # Pull first (fetch + merge) so push won't be rejected. Remote
    # NAME, not URL — see _sync_repo_locked for why.
    try:
        porcelain.pull(
            repo, 'origin',
            username=username, password=token,
            errstream=io.BytesIO(),
        )
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
