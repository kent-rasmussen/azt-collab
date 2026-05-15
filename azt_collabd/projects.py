"""
Project registry, backed by ``$AZT_HOME/projects.json``.

A "project" is a working tree containing one .lift file plus its audio/
and images/ directories. The recorder registers the path it already has
(register-in-place); the backend remembers (langcode → path) so clients
can request ops by langcode instead of passing working_dir each time.

Schema (``$AZT_HOME/projects.json``):
    {
      "<langcode>": {
        "working_dir": "/abs/path/to/tree",
        "lift_path":   "/abs/path/to/tree/langcode.lift",
        "remote_url":  "https://github.com/owner/langcode.git",
        "last_commit": 1712345600.0,
        "last_sync":   1712345678.0,
        "created_at":  1700000000.0
      },
      ...
    }

``last_commit`` and ``last_sync`` are deliberately separate. The
former stamps any "the daemon committed work locally" outcome
(``COMMITTED_LOCAL``, ``COMMITTED_NO_REMOTE``,
``COMMITTED_AND_PUSHED``). The latter only stamps when the daemon
successfully reached the remote (``PUSHED``, ``PULLED``,
``COMMITTED_AND_PUSHED``). Peers can render the more recent of the
two with a marker so the user sees "13:45* committed but not yet
pushed" vs. "13:45 backed up". Filed by azt_recorder 1.37.3 in
``azt_collab_client/NOTES_TO_DAEMON.md``.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass, field

from .paths import azt_home


_PROJECTS_FILENAME = 'projects.json'


def projects_path():
    return os.path.join(azt_home(), _PROJECTS_FILENAME)


@dataclass
class Project:
    langcode: str
    working_dir: str
    lift_path: str = ''
    remote_url: str = ''
    last_commit: float = 0.0
    last_sync: float = 0.0
    created_at: float = 0.0
    # Per-project CAWL image source. Empty → fall back to the daemon's
    # global ``config.cawl_image_repo()`` value (smoothes the recorder
    # migration so unmigrated projects don't have to be touched). When
    # set, the daemon serves CAWL index / image bytes for this project
    # from ``$AZT_HOME/cawl/<owner>/<repo>/...`` — multiple projects
    # pointing at the same repo share that one cache directory.
    cawl_image_repo: str = ''
    # Per-project override for the GitHub repo *name* (last segment of
    # the remote URL) used by the publish path. Empty → callers treat
    # as equal to ``langcode`` (no override; the typical case).
    # Non-empty values let the user keep a vanity / project-style /
    # collision-avoiding repo name while the LIFT ``<form lang="…">``
    # tag (== ``langcode``) stays canonical. Recorder 1.41.3 removed
    # its peer-side ``collab_langcode`` peer_pref under the
    # no-daemon-owned-caches rule; this field is the canonical home
    # for that data.
    repo_slug: str = ''

    def to_dict(self):
        return {
            'langcode': self.langcode,
            'working_dir': self.working_dir,
            'lift_path': self.lift_path,
            'remote_url': self.remote_url,
            'last_commit': self.last_commit,
            'last_sync': self.last_sync,
            'created_at': self.created_at,
            'cawl_image_repo': self.cawl_image_repo,
            'repo_slug': self.repo_slug,
        }

    @classmethod
    def from_entry(cls, langcode, d):
        return cls(
            langcode=langcode,
            working_dir=d.get('working_dir', ''),
            lift_path=d.get('lift_path', ''),
            remote_url=d.get('remote_url', ''),
            last_commit=float(d.get('last_commit', 0.0)),
            last_sync=float(d.get('last_sync', 0.0)),
            created_at=float(d.get('created_at', 0.0)),
            cawl_image_repo=d.get('cawl_image_repo', ''),
            repo_slug=d.get('repo_slug', ''),
        )


# ── load / save ─────────────────────────────────────────────────────────────

def _load_raw():
    try:
        with open(projects_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        print(f'[collab.projects] load failed: {ex}')
        return {}


def _save_raw(data):
    path = projects_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.projects.', suffix='.tmp',
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _update(mutator):
    d = _load_raw()
    mutator(d)
    _save_raw(d)


# ── public API ──────────────────────────────────────────────────────────────

def register(langcode, working_dir, lift_path='', remote_url='',
             cawl_image_repo=None, repo_slug=None):
    """Register or update a project. Returns the resulting Project.

    ``cawl_image_repo`` / ``repo_slug`` accept None (don't touch
    the field; preserves any previously-set value across
    re-registration), empty string (explicitly clear; the
    project falls back to default behaviour — daemon-global
    CAWL repo for ``cawl_image_repo``, ``langcode`` itself for
    ``repo_slug``), or a non-empty string."""
    if not langcode:
        raise ValueError('langcode required')
    if not working_dir:
        raise ValueError('working_dir required')
    data = _load_raw()
    entry = dict(data.get(langcode, {}))
    entry['working_dir'] = working_dir
    if lift_path:
        entry['lift_path'] = lift_path
    if remote_url:
        entry['remote_url'] = remote_url
    if cawl_image_repo is not None:
        entry['cawl_image_repo'] = cawl_image_repo
    if repo_slug is not None:
        entry['repo_slug'] = repo_slug
    entry.setdefault('last_sync', 0.0)
    entry.setdefault('created_at', time.time())
    data[langcode] = entry
    _save_raw(data)
    return Project.from_entry(langcode, entry)


def set_cawl_image_repo(langcode, repo):
    """Persist a per-project CAWL image repo slug. Empty string is a
    valid value — clears the override so the project falls back to the
    daemon-global default."""
    def mut(d):
        if langcode in d:
            d[langcode]['cawl_image_repo'] = repo
    _update(mut)


def set_repo_slug(langcode, slug):
    """Persist a per-project GitHub-repo-name override for the
    publish path. Empty string is a valid value — clears the
    override so callers fall back to ``langcode`` (the typical
    case)."""
    def mut(d):
        if langcode in d:
            d[langcode]['repo_slug'] = slug
    _update(mut)


def unregister(langcode):
    def mut(d):
        d.pop(langcode, None)
    _update(mut)


def rename(old_langcode, new_langcode):
    """Rename a project's key in ``projects.json`` while preserving
    its working_dir / lift_path / remote_url / created_at /
    last_sync. Returns the resulting Project under the new key, or
    None if ``old_langcode`` isn't registered. Raises ``ValueError``
    if ``new_langcode`` is empty or already names a different
    project.

    Used by the picker's "confirm langcode" flow: the daemon
    auto-derives a langcode from the LIFT filename / URL on clone
    or open-file, but the user may want to override it before the
    project is handed back to the recorder. Same-name rename is a
    no-op."""
    if not new_langcode:
        raise ValueError('new_langcode required')
    if old_langcode == new_langcode:
        return get(old_langcode)
    data = _load_raw()
    entry = data.get(old_langcode)
    if entry is None:
        return None
    if new_langcode in data:
        raise ValueError(
            f'{new_langcode!r} is already registered to a different '
            f'working_dir; pick a different langcode')
    def mut(d):
        d[new_langcode] = dict(entry)
        d.pop(old_langcode, None)
    _update(mut)
    return Project.from_entry(new_langcode, entry)


def get(langcode):
    entry = _load_raw().get(langcode)
    if entry is None:
        return None
    return Project.from_entry(langcode, entry)


def list_all():
    return [Project.from_entry(code, entry)
            for code, entry in _load_raw().items()]


def find_langcode_by_working_dir(working_dir):
    """Return the registered langcode whose ``working_dir`` matches
    ``working_dir``, or '' if none is registered. Used by helpers
    that operate on ``project_dir`` (sync, commit-audio-and-sync,
    init) but need the langcode to update langcode-keyed state
    (e.g. ``commit_failure_count``)."""
    if not working_dir:
        return ''
    try:
        target = os.path.abspath(working_dir)
    except Exception:
        target = working_dir
    for code, entry in _load_raw().items():
        wd = entry.get('working_dir', '')
        if not wd:
            continue
        try:
            if os.path.abspath(wd) == target:
                return code
        except Exception:
            if wd == working_dir:
                return code
    return ''


def set_last_sync(langcode, ts=None):
    if ts is None:
        ts = time.time()
    def mut(d):
        if langcode in d:
            d[langcode]['last_sync'] = float(ts)
    _update(mut)


def set_last_commit(langcode, ts=None):
    """Stamp the timestamp of the most recent local commit. Set on
    ``COMMITTED_LOCAL`` / ``COMMITTED_NO_REMOTE`` /
    ``COMMITTED_AND_PUSHED`` outcomes — any path where the daemon
    actually wrote a commit object to the working tree, push or no
    push. Peers render this alongside ``last_sync`` so the
    "committed but not yet pushed" state has a real timestamp."""
    if ts is None:
        ts = time.time()
    def mut(d):
        if langcode in d:
            d[langcode]['last_commit'] = float(ts)
    _update(mut)


def set_remote_url(langcode, url):
    def mut(d):
        if langcode in d:
            d[langcode]['remote_url'] = url
    _update(mut)


# ── derivation helpers (used for auto-registration) ─────────────────────────

def derive_remote_url(working_dir):
    """Return the origin URL from the git config, or ''."""
    try:
        from dulwich.repo import Repo
        repo = Repo(working_dir)
        try:
            return repo.get_config().get(
                (b'remote', b'origin'), b'url').decode('utf-8')
        except KeyError:
            return ''
    except Exception:
        return ''


def create_from_template(template_url, vernlang, dest_dir,
                         timeout=60, size_cap=10 * 1024 * 1024):
    """Download a LIFT template and register it as a project.

    Returns the resulting Project. ``size_cap`` (default 10 MiB) defends
    against accidentally pulling a giant repo via a misconfigured URL —
    the SILCAWL template is ~200 KB, so this is plenty of head-room.

    Raises ``ValueError`` for missing args, ``RuntimeError`` for download
    failures.
    """
    import urllib.request
    from .net import _ensure_ssl

    if not template_url:
        raise ValueError('template_url required')
    if not vernlang:
        raise ValueError('vernlang required')
    if not dest_dir:
        raise ValueError('dest_dir required')

    project_dir = os.path.abspath(dest_dir)
    os.makedirs(project_dir, exist_ok=True)
    lift_path = os.path.join(project_dir, f'{vernlang}.lift')

    # On Android p4a doesn't ship system CA certs; without this patch
    # urlopen fails with SSL: CERTIFICATE_VERIFY_FAILED. Every other
    # network-touching function in azt_collabd calls this first; this
    # site was missed.
    _ensure_ssl()
    req = urllib.request.Request(template_url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = resp.read(size_cap + 1)
    if len(content) > size_cap:
        raise RuntimeError(
            f'template exceeds size cap ({size_cap} bytes)')
    if len(content) < 50:
        raise RuntimeError(
            f'template download too small ({len(content)} bytes)')
    fd, tmp = tempfile.mkstemp(prefix='.template.', suffix='.lift',
                               dir=project_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        os.replace(tmp, lift_path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

    return register(vernlang, project_dir, lift_path=lift_path)


def derive_langcode(working_dir, lift_path=''):
    """Pick a langcode for a working_dir by this priority:
        1. git remote repo name (last path segment, .git stripped)
        2. .lift filename stem
        3. working_dir basename
    """
    url = derive_remote_url(working_dir)
    if url:
        name = url.rstrip('/').rsplit('/', 1)[-1]
        if name.endswith('.git'):
            name = name[:-4]
        if name:
            return name
    if lift_path:
        base = os.path.basename(lift_path)
        if base.endswith('.lift'):
            base = base[:-5]
        if base:
            return base
    base = os.path.basename(os.path.normpath(working_dir))
    return base or 'project'
