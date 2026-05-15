"""Translate Status/Result objects into user-visible strings.

The client owns its own i18n (``azt_collab_client.i18n``), so picker
UI, popups, and status messages render translated even when no host
override is set. Hosts with their own catalogs (the recorder, with
``aztrecorder.po``) call ``set_translator(host_tr)`` to override —
``tr()`` then tries the host translator first and falls back to the
client catalog, so client-owned strings still render translated even
when the host's catalog doesn't carry them.

Pre-suite hosts that imported a top-level ``i18n`` module continue to
work: ``set_translator`` accepts any callable.
"""

from . import status as S
from . import i18n as _client_i18n


def _client_tr(msg):
    return _client_i18n._(msg)


# Default translator: the client's own catalog. Override with
# ``set_translator`` from a host that has additional strings.
_tr = _client_tr


def set_translator(fn):
    """Override the translator. Callers pass a function taking a str and
    returning a translated str. Reset to the client default by passing
    ``None``."""
    global _tr
    _tr = fn if fn is not None else _client_tr


def tr(msg):
    """Translate ``msg`` through the host translator first, then fall
    back to the client catalog. Useful for KV ``#:import`` so
    subsequent ``set_translator`` calls take effect (KV imports bind
    once; importing this wrapper instead of ``_tr`` makes the
    indirection explicit).

    The fallback layer means an embedded peer does not need to
    duplicate client strings into its own catalog: any string the host
    catalog leaves unchanged falls through to the client's catalog,
    which owns the picker/popup/status translations."""
    if _tr is _client_tr:
        return _tr(msg)
    translated = _tr(msg)
    if translated == msg:
        return _client_tr(msg)
    return translated


def _fmt(template, params):
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def _format_deadline(expires_at):
    """Render an ``expires_at`` unix timestamp as a human-facing
    deadline phrase.

    ``S.AUTH_REFRESH_STALE`` carries the absolute unix timestamp at
    which the running access token expires. The user-visible toast
    wants something digestible — "in 47 minutes", "in 3 hours", or
    "already expired" — without dragging in timezone/locale
    machinery for a one-shot phrase. Returns the translated phrase
    directly; the surrounding message template embeds it as
    ``{deadline}``.

    Empty / missing / non-numeric ``expires_at`` returns the
    translated "soon" fallback so the surrounding template still
    reads gracefully."""
    import time
    try:
        deadline_ts = float(expires_at or 0)
    except (TypeError, ValueError):
        return _tr('soon')
    if deadline_ts <= 0:
        return _tr('soon')
    remaining_s = deadline_ts - time.time()
    if remaining_s <= 0:
        return _tr('now (already expired)')
    minutes = int(remaining_s // 60)
    if minutes < 60:
        return _fmt(_tr('in {n} minute(s)'), {'n': minutes})
    hours = remaining_s / 3600
    # One decimal under 10h, integer above — "in 2.4 hours" is more
    # accurate than "in 2 hours" when the user has limited time;
    # "in 14 hours" is fine without the .x precision.
    if hours < 10:
        return _fmt(_tr('in {n} hour(s)'),
                    {'n': f'{hours:.1f}'.rstrip('0').rstrip('.')})
    return _fmt(_tr('in {n} hour(s)'), {'n': int(hours)})


def _refresh_stale_message(params):
    """Compose the AUTH_REFRESH_STALE toast: action + deadline.

    The daemon supplies ``expires_at`` (unix timestamp); we render
    the relative-time phrase via ``_format_deadline`` and embed it
    in a translated action template. The action template stays
    translation-friendly (one sentence, one placeholder)."""
    deadline = _format_deadline((params or {}).get('expires_at'))
    return _fmt(
        _tr(
            'GitHub session needs re-authentication — current '
            'access expires {deadline}. Open GitHub Connect and '
            'tap Re-authenticate.'),
        {'deadline': deadline})


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
    S.DATA_LOSS_RISK:         lambda p: _fmt(_tr(
        'Data-loss risk: {count} file(s) written to your project '
        "aren't being backed up. Please enable Settings → "
        'Diagnostic log → Log server activity = yes, then Share '
        'daemon log so we can investigate.'), p),
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
    S.COMMIT_REPEATEDLY_FAILED: lambda p: _fmt(_tr(
        'Saving to git has failed {count} times in a row '
        '({error}). Your recordings are still on the device '
        "but aren't being backed up. Please enable Settings → "
        'Diagnostic log → Log server activity = yes, then Share '
        'daemon log so we can investigate.'), p),
    S.PUSH_FAILED:            lambda p: _fmt(_tr('Push failed: {error}'), p),
    S.PULL_FAILED:            lambda p: _fmt(_tr('Pull failed: {error}'), p),
    S.CLONE_FAILED:           lambda p: _fmt(_tr('Clone failed: {error}'), p),
    S.CLONE_AUTH_REQUIRED:    lambda p: _fmt(_tr(
        'Clone failed — repository not found. This may be a private '
        'repository.\n\nAre you authenticated to {host}?'),
        {'host': (p.get('host') or '').capitalize() or 'GitHub'}),
    S.BRANCH_ERROR:           lambda p: _fmt(_tr('Branch error: {error}'), p),
    S.REMOTE_CREATE_FAILED:   lambda p: _fmt(_tr('Create repo failed: {error}'), p),

    S.AUTH_REQUIRED:          lambda p: _tr('Not connected to GitHub. Go to Setup > Connect to GitHub.'),
    S.CONTRIBUTOR_UNSET:      lambda p: _tr(
        'Please set your name in the sync settings before publishing or syncing.'),
    S.WORK_OFFLINE_ENABLED:   lambda p: _tr(
        'Work-offline mode is on. Turn it off in sync settings to push.'),
    S.APP_NOT_INSTALLED:      lambda p: _fmt(_tr('App not installed. Visit {url} and select "All repositories".'), p),
    S.APP_SUSPENDED:          lambda p: _fmt(_tr("GitHub App installation is suspended at {url}. Open it, scroll to the bottom, and tap 'Unsuspend'."), p),
    S.REPO_NOT_AUTHORIZED:    lambda p: _fmt(_tr('App not authorized for {owner_repo}. Add it at {url}'), p),
    S.ACCESS_DENIED:          lambda p: _fmt(_tr('Access denied (403). Check app permissions at {url}'), p),
    S.AUTH_REFRESH_STALE:     lambda p: _refresh_stale_message(p),

    S.AUTH_EXPIRED:           lambda p: _tr('Authorization expired. Please try again.'),
    S.AUTH_DENIED:            lambda p: _tr('Authorization denied by user.'),
    S.AUTH_TIMEOUT:           lambda p: _tr('Authorization timed out.'),

    S.COLLABORATOR_INVITED:   lambda p: _fmt(_tr(
        'Invited {username} as a collaborator on {owner_repo}. '
        'They must accept the invitation on GitHub before they '
        'can clone or sync.'), p),
    S.COLLABORATOR_ALREADY:   lambda p: _fmt(_tr(
        '{username} already has access to {owner_repo} '
        '(or a pending invitation).'), p),
    S.COLLABORATOR_INVITE_FAILED: lambda p: _fmt(_tr(
        'Could not invite {username} to {owner_repo}: {error}'), p),
    S.INVALID_USERNAME:       lambda p: _tr(
        'Enter a GitHub username.'),
    S.NOT_GITHUB_REMOTE:      lambda p: _fmt(_tr(
        'This project is not hosted on GitHub ({remote_url}). '
        'Collaborator invites are only supported for GitHub '
        'repositories.'), p),

    S.BUSY:                   lambda p: _tr('Another sync is in progress. Try again in a moment.'),
    S.CONFLICTS:              lambda p: (_fmt(_tr('Merge conflicts in {paths}'), p)
                                          if p.get('paths') else
                                          _tr('Merge conflicts; review the entries flagged azt-lift-conflict.')),
    S.JOB_INTERRUPTED:        lambda p: _tr(
        'Sync was interrupted; please retry.'),

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
