"""Translate Status/Result objects into user-visible strings.

Uses the recorder's i18n module (``from i18n import _``). Other apps in
the suite that embed this client may set their own translator via
``set_translator(fn)``.
"""

from . import status as S

try:
    from i18n import _ as _default_tr
except Exception:  # pragma: no cover — client used outside the recorder
    def _default_tr(s):
        return s

_tr = _default_tr


def set_translator(fn):
    """Override the translator. Callers pass a function taking a str and
    returning a translated str."""
    global _tr
    _tr = fn


def tr(msg):
    """Module-level wrapper that always delegates to the current ``_tr``.

    Useful for KV ``#:import`` so subsequent ``set_translator`` calls
    take effect (KV imports bind once; importing this wrapper instead
    of ``_tr`` makes the indirection explicit)."""
    return _tr(msg)


def _fmt(template, params):
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


# Each entry: code → function(params) → translated string. Using a
# function keeps the _tr call lazy so translations pick up the current
# language at render time, not at import time.
_HANDLERS = {
    S.INITIALIZED:            lambda p: _tr('Initialized git repository.'),
    S.ALREADY_INITIALIZED:    lambda p: _tr('Repository already initialized.'),
    S.GITIGNORE_CREATED:      lambda p: _tr('Created .gitignore.'),
    S.COMMITTED:              lambda p: (_fmt(_tr('Committed ({sha}).'), p)
                                         if p.get('sha') else _tr('Committed.')),
    S.COMMITTED_LOCAL:        lambda p: _tr('Committed local changes.'),
    S.COMMITTED_OFFLINE:      lambda p: _tr('Committed locally (offline)'),
    S.COMMITTED_NO_REMOTE:    lambda p: _tr('Committed (no remote configured)'),
    S.COMMITTED_AND_PUSHED:   lambda p: _fmt(_tr('Committed and pushed {n} file(s)'), p),
    S.NOTHING_TO_COMMIT:      lambda p: _tr('Nothing new to commit.'),
    S.REMOTE_SET:             lambda p: _fmt(_tr('Remote set to {url}'), p),
    S.REMOTE_UPDATED:         lambda p: _fmt(_tr('Remote updated to {url}'), p),
    S.REMOTE_UNCHANGED:       lambda p: _fmt(_tr('Remote: {url}'), p),
    S.REMOTE_REPO_CREATED:    lambda p: _fmt(_tr('Created remote repository {owner_repo}.'), p),
    S.PUSHED:                 lambda p: (_fmt(_tr('Pushed to {url} (branch: {branch}).'), p)
                                         if 'url' in p else
                                         _fmt(_tr('Pushed to {branch}.'), p)
                                         if 'branch' in p else _tr('Pushed.')),
    S.PULLED:                 lambda p: _tr('Pulled latest changes.'),
    S.CLONED:                 lambda p: _fmt(_tr('Cloned to {dir}'), p),
    S.LIFT_FOUND:             lambda p: _fmt(_tr('Found: {file}'), p),
    S.LIFT_NOT_FOUND:         lambda p: _tr('No .lift file found in cloned repository.'),
    S.ON_BRANCH:              lambda p: _fmt(_tr('On branch {branch}.'), p),
    S.STAGED_ALL:             lambda p: _tr('Staged all changes.'),
    S.OPEN_PR:                lambda p: _tr('Open your git host to create a pull request.'),
    S.NO_AUDIO:               lambda p: _tr('No new audio'),
    S.NO_REPO:                lambda p: _tr('No repo'),

    S.NOT_A_REPO:             lambda p: _tr('Not a git repository. Publish the project first.'),
    S.NO_REMOTE:              lambda p: _tr('No remote configured. Publish the project first.'),
    S.COMMIT_FAILED:          lambda p: _fmt(_tr('Commit: {error}'), p),
    S.PUSH_FAILED:            lambda p: _fmt(_tr('Push failed: {error}'), p),
    S.PULL_FAILED:            lambda p: _fmt(_tr('Pull failed: {error}'), p),
    S.CLONE_FAILED:           lambda p: _fmt(_tr('Clone failed: {error}'), p),
    S.BRANCH_ERROR:           lambda p: _fmt(_tr('Branch error: {error}'), p),
    S.REMOTE_CREATE_FAILED:   lambda p: _fmt(_tr('Create repo failed: {error}'), p),

    S.AUTH_REQUIRED:          lambda p: _tr('Not connected to GitHub. Go to Setup > Connect to GitHub.'),
    S.APP_NOT_INSTALLED:      lambda p: _fmt(_tr('App not installed. Visit {url} and select "All repositories".'), p),
    S.REPO_NOT_AUTHORIZED:    lambda p: _fmt(_tr('App not authorized for {owner_repo}. Add it at {url}'), p),
    S.ACCESS_DENIED:          lambda p: _fmt(_tr('Access denied (403). Check app permissions at {url}'), p),

    S.AUTH_EXPIRED:           lambda p: _tr('Authorization expired. Please try again.'),
    S.AUTH_DENIED:            lambda p: _tr('Authorization denied by user.'),
    S.AUTH_TIMEOUT:           lambda p: _tr('Authorization timed out.'),

    S.BUSY:                   lambda p: _tr('Another sync is in progress. Try again in a moment.'),
    S.CONFLICTS:              lambda p: (_fmt(_tr('Merge conflicts in {paths}'), p)
                                          if p.get('paths') else
                                          _tr('Merge conflicts; review the entries flagged azt-lift-conflict.')),

    # Transport-layer synthetics from the client (not emitted by the backend)
    'SERVER_UNAVAILABLE':     lambda p: _fmt(_tr('Sync service unavailable: {error}'), p),
    'SERVER_ERROR':           lambda p: _fmt(_tr('Sync service error: {error}'), p),
}


def translate_status(status):
    """Translate a single Status to a user-visible string."""
    fn = _HANDLERS.get(status.code)
    if fn is None:
        return f'[{status.code}] {status.params!r}'
    return fn(status.params or {})


def translate_result(result):
    """Translate a Result (list of Status) into a joined log string."""
    return '\n'.join(translate_status(s) for s in result.statuses)
