"""
Dulwich operations: init, clone, pull, push, commit, sync, and auto-commit
of audio + LIFT changes. All network ops call net._ensure_ssl() first.

Every public op returns a ``Result`` (status codes + params) — no i18n
inside the backend. Exception paths emit failure codes inside the Result
rather than raising; that matches the existing log-append style.
"""

import io
import json
import os
import re
import sys

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
    """Stage all modified and untracked files (equivalent to git add -A)."""
    from dulwich import porcelain
    status = porcelain.status(repo)
    paths = []

    for f in status.unstaged:
        paths.append(_bytes_path(f))

    for f in status.untracked:
        rel = f if isinstance(f, str) else f.decode('utf-8', errors='replace')
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
                    paths.append(_bytes_path(rp))

    if paths:
        porcelain.add(repo, paths=paths)


def _default_author(contributor_name):
    safe = contributor_name.lower().replace(' ', '_')
    return _enc(f'{contributor_name} <{safe}@device>')


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
              branch='main', contributor_name='Recorder'):
    """Initialize a git repo, commit everything, set remote, push.
    Returns a Result."""
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
    except Exception as exc:
        result.add(S.COMMIT_FAILED, error=str(exc))

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
        result.add(S.PUSH_FAILED, error=str(exc))

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
    except Exception as exc:
        msg = str(exc).lower()
        if 'nothing' in msg or 'empty' in msg or 'no changes' in msg:
            result.add(S.NOTHING_TO_COMMIT)
        else:
            result.add(S.COMMIT_FAILED, error=str(exc))

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
        result.add(S.PUSH_FAILED, error=str(exc))

    return result


def sync_repo(project_dir, username, token, contributor_name):
    """Pull + commit + push. Returns Result."""
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
    _stage_all(repo, project_dir)
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
        except Exception as exc:
            result.add(S.COMMIT_FAILED, error=str(exc))
    else:
        result.add(S.NOTHING_TO_COMMIT)

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
        porcelain.fetch(
            repo, 'origin',
            username=username, password=token,
            errstream=io.BytesIO(),
        )
    except Exception as exc:
        if _is_http_403(exc):
            result.statuses.append(diagnose_403(token, remote_url))
            return result
        # Non-fatal: maybe remote is empty or temporarily unreachable.
        result.add(S.PULL_FAILED, error=str(exc))
    lift_merge.trace('[sync-trace] fetch done')

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
            result.add(S.PULL_FAILED, error=f'merge failed: {exc}')
            return result

    # Push, with retry loop for races (someone pushed between our
    # fetch and our push).
    refspec = _enc(f'refs/heads/{branch}:refs/heads/{branch}')
    lift_merge.trace(
        f'[sync-trace] push loop begin retries={retries_remaining}')
    while retries_remaining > 0:
        retries_remaining -= 1
        lift_merge.trace(
            f'[sync-trace] push attempt '
            f'(retries_remaining_after={retries_remaining})')
        try:
            porcelain.push(
                repo, remote_url, refspec,
                username=username, password=token,
                errstream=io.BytesIO(),
            )
            lift_merge.trace('[sync-trace] push done')
            # Push advances the remote on GitHub but doesn't update the
            # *local mirror* of refs/remotes/origin/<branch>. Without
            # this set, ``_count_commits_ahead`` keeps comparing the
            # just-pushed local SHA against the pre-push remote mirror
            # and reports ``(+N)`` to the recorder's sync indicator
            # even though we're fully in sync. Bumping the mirror
            # explicitly is what ``git push`` does in CLI git too.
            try:
                repo.refs[remote_ref] = local_sha
            except Exception as ex:
                lift_merge.trace(
                    f'[sync-trace] post-push remote-mirror '
                    f'update failed: {ex!r}')
            result.add(S.PUSHED, branch=branch)
            return result
        except Exception as exc:
            lift_merge.trace(f'[sync-trace] push raised: {exc!r}')
            if _is_http_403(exc):
                result.statuses.append(diagnose_403(token, remote_url))
                return result
            if retries_remaining <= 0:
                result.add(S.PUSH_FAILED, error=str(exc))
                return result
            # Try to fetch + remerge once more. Use the remote NAME
            # so dulwich updates ``refs/remotes/origin/<branch>`` —
            # see the rationale above the initial fetch call.
            try:
                porcelain.fetch(
                    repo, 'origin',
                    username=username, password=token,
                    errstream=io.BytesIO(),
                )
                new_remote = _read_ref(remote_ref)
                lift_merge.trace(
                    f'[sync-trace] retry fetch new_remote={new_remote!r} '
                    f'local_sha={local_sha!r}')
                if new_remote and new_remote != local_sha and \
                        not _is_ancestor(repo, local_sha, new_remote):
                    merged_sha, _ = _merge_diverged(
                        repo, project_dir, branch, local_sha, new_remote)
                    lift_merge.trace(
                        f'[sync-trace] retry merge done '
                        f'merged_sha={merged_sha!r}')
                    local_sha = merged_sha
            except Exception as ex:
                lift_merge.trace(
                    f'[sync-trace] retry fetch/merge failed: {ex!r}')
    return result


def _stage_audio(repo, project_dir):
    """Stage only new/modified audio files (audio/ + images/ + .lift)."""
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

    if paths:
        porcelain.add(repo, paths=paths)
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
    result = Result()
    repo = _get_repo(project_dir)
    if repo is None:
        result.add(S.NO_REPO)
        return result

    n = _stage_audio(repo, project_dir)
    if n == 0:
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
        porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=committer,
        )
    except Exception as exc:
        result.add(S.COMMIT_FAILED, error=str(exc))
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
            result.add(S.PUSH_FAILED, error=str(exc))

    return result
