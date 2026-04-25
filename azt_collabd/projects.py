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
        "last_sync":   1712345678.0,
        "created_at":  1700000000.0
      },
      ...
    }
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
    last_sync: float = 0.0
    created_at: float = 0.0

    def to_dict(self):
        return {
            'langcode': self.langcode,
            'working_dir': self.working_dir,
            'lift_path': self.lift_path,
            'remote_url': self.remote_url,
            'last_sync': self.last_sync,
            'created_at': self.created_at,
        }

    @classmethod
    def from_entry(cls, langcode, d):
        return cls(
            langcode=langcode,
            working_dir=d.get('working_dir', ''),
            lift_path=d.get('lift_path', ''),
            remote_url=d.get('remote_url', ''),
            last_sync=float(d.get('last_sync', 0.0)),
            created_at=float(d.get('created_at', 0.0)),
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

def register(langcode, working_dir, lift_path='', remote_url=''):
    """Register or update a project. Returns the resulting Project."""
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
    entry.setdefault('last_sync', 0.0)
    entry.setdefault('created_at', time.time())
    data[langcode] = entry
    _save_raw(data)
    return Project.from_entry(langcode, entry)


def unregister(langcode):
    def mut(d):
        d.pop(langcode, None)
    _update(mut)


def get(langcode):
    entry = _load_raw().get(langcode)
    if entry is None:
        return None
    return Project.from_entry(langcode, entry)


def list_all():
    return [Project.from_entry(code, entry)
            for code, entry in _load_raw().items()]


def set_last_sync(langcode, ts=None):
    if ts is None:
        ts = time.time()
    def mut(d):
        if langcode in d:
            d[langcode]['last_sync'] = float(ts)
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
