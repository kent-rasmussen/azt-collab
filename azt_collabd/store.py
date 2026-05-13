"""
Credentials and host-selection store, backed by ``$AZT_HOME/credentials.json``.

Schema:
    {
      "collab_host": "github" | "gitlab",
      "github": {
        "access_token": "...",
        "refresh_token": "...",
        "token_time": 1712345678.0,
        "username": "...",
        "app_installed": true
      },
      "gitlab": {
        "username": "...",
        "token": "...",
        "confirmed": true
      }
    }

    "confirmed" is set true on each block when a live test against
    the host's API succeeds (gitlab.com/api/v4/user for GitLab,
    api.github.com/user for GitHub). It is reset to False whenever
    the underlying credentials change (token save / app-install flag
    flip / disconnect), so a stale "verified" badge never outlives
    the credentials it was vouching for.

All fields are optional. The file is written atomically with mode 0600
on POSIX. A one-shot migration helper copies legacy keys out of the
recorder's prefs.json.
"""

import json
import os
import tempfile
import time

from .paths import azt_home


_CREDS_FILENAME = 'credentials.json'


def credentials_path():
    return os.path.join(azt_home(), _CREDS_FILENAME)


# ── load / save ─────────────────────────────────────────────────────────────

def load():
    """Return the credentials dict (empty dict if the file doesn't exist)."""
    path = credentials_path()
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        print(f'[collab.store] load failed: {ex}')
        return {}


def save(data):
    """Write credentials atomically with mode 0600."""
    path = credentials_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix='.credentials.', suffix='.tmp',
        dir=os.path.dirname(path))
    try:
        try:
            os.fchmod(tmp_fd, 0o600)
        except (AttributeError, OSError):
            pass  # Windows / non-POSIX
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _update(mutator):
    d = load()
    mutator(d)
    save(d)


# ── host selection ──────────────────────────────────────────────────────────

def get_collab_host():
    return load().get('collab_host', 'github')


def set_collab_host(host):
    if host not in ('github', 'gitlab'):
        raise ValueError(f'invalid collab_host: {host!r}')
    _update(lambda d: d.__setitem__('collab_host', host))


# ── GitHub ──────────────────────────────────────────────────────────────────

def get_github():
    return dict(load().get('github', {}))


def set_github_tokens(*, access_token, refresh_token='', username='',
                     token_time=None):
    """Replace the GitHub token block. Resets ``confirmed`` to False —
    fresh credentials must pass a live test before the UI re-shows
    the verified badge. Clears ``refresh_broken`` and its diagnostic
    fields: fresh tokens were just minted, so the prior
    refresh-failure state no longer applies."""
    def mut(d):
        block = dict(d.get('github', {}))
        block['access_token'] = access_token
        block['refresh_token'] = refresh_token or block.get('refresh_token', '')
        block['token_time'] = (time.time() if token_time is None
                               else float(token_time))
        if username:
            block['username'] = username
        block['confirmed'] = False
        block.pop('refresh_broken', None)
        block.pop('refresh_error', None)
        block.pop('refresh_checked_at', None)
        d['github'] = block
    _update(mut)


def _set_github_refresh_broken(error):
    """Record that the most recent refresh attempt failed with
    ``error``. The access token is still in the store and (until its
    8h-from-issuance expiry) still works, but the refresh path
    cannot mint a replacement — the user must re-run the device
    flow. Surfaced to peers via ``get_status()`` and via the
    ``AUTH_REFRESH_STALE`` status code on every sync result that
    touched ``get_valid_github_token`` afterwards."""
    def mut(d):
        block = dict(d.get('github', {}))
        block['refresh_broken'] = True
        block['refresh_error'] = str(error)
        block['refresh_checked_at'] = time.time()
        d['github'] = block
    _update(mut)


def _clear_github_refresh_broken():
    def mut(d):
        block = dict(d.get('github', {}))
        if not block.get('refresh_broken'):
            return
        block.pop('refresh_broken', None)
        block.pop('refresh_error', None)
        block['refresh_checked_at'] = time.time()
        d['github'] = block
    _update(mut)


def github_refresh_state():
    """Return ``{'broken': bool, 'error': str, 'expires_at': float}``
    describing the persisted refresh-token health.

    ``expires_at`` is ``token_time + 8h`` — GitHub access tokens
    are 8h from issuance, and ``token_time`` is stamped by every
    successful issue (device-flow exchange OR refresh). Zero when
    no token has ever been stored. Peers translate to a relative
    deadline phrase via ``translate._format_deadline``.

    ``broken`` flips True on the first refresh failure and only
    clears when fresh tokens are written via ``set_github_tokens``
    (or, defensively, when a subsequent refresh attempt succeeds —
    handled inside ``get_valid_github_token``)."""
    block = get_github()
    token_time = float(block.get('token_time', 0) or 0)
    expires_at = (token_time + 8 * 3600) if token_time else 0.0
    return {
        'broken': bool(block.get('refresh_broken', False)),
        'error': block.get('refresh_error', '') or '',
        'expires_at': expires_at,
    }


def set_github_app_installed(installed):
    """Persist whether the GitHub App is installed for this user.
    Treated as a settings-change: resets ``confirmed`` so the user is
    prompted to re-test once the app-install state has flipped."""
    def mut(d):
        block = dict(d.get('github', {}))
        block['app_installed'] = bool(installed)
        block['confirmed'] = False
        d['github'] = block
    _update(mut)


def set_github_confirmed(confirmed):
    """Persist the result of a successful (or failed) live test against
    ``api.github.com``. Called by ``_h_test_github``. Mirrors
    ``set_gitlab_confirmed`` so the two host blocks have the same
    confirmation lifecycle."""
    def mut(d):
        block = dict(d.get('github', {}))
        block['confirmed'] = bool(confirmed)
        d['github'] = block
    _update(mut)


def clear_github():
    def mut(d):
        d.pop('github', None)
    _update(mut)


def get_valid_github_token():
    """Return (username, access_token) with automatic refresh if near expiry.
    Returns ('', '') if no token stored or refresh fails."""
    from .auth import refresh_access_token

    block = get_github()
    token = block.get('access_token', '')
    refresh = block.get('refresh_token', '')
    username = block.get('username', '')
    token_time = block.get('token_time', 0)
    if not token:
        return '', ''
    # Access tokens last 8 hours; refresh proactively at 7h.
    #
    # On success: ``set_github_tokens`` re-stamps ``token_time`` and
    # clears any prior ``refresh_broken`` flag, so a transient refresh
    # failure that later resolves silently clears the warning.
    #
    # On failure: keep the existing access token in play (it's still
    # valid until its 8h cliff), but record ``refresh_broken`` so
    # peers' user-initiated sync surfaces an ``AUTH_REFRESH_STALE``
    # toast with the deadline. The user-actionable path is re-running
    # device flow at the Connect screen.
    if time.time() - token_time > 7 * 3600 and refresh:
        try:
            new_data = refresh_access_token(refresh)
            set_github_tokens(
                access_token=new_data['access_token'],
                refresh_token=new_data.get('refresh_token', refresh),
                username=username,
            )
            token = new_data['access_token']
        except Exception as ex:
            print(f'[collab.store] github refresh failed: {ex}')
            _set_github_refresh_broken(ex)
            # Return the old token — it might still work until cliff.
    return username, token


# ── GitLab ──────────────────────────────────────────────────────────────────

def get_gitlab():
    block = load().get('gitlab', {}) or {}
    return block.get('username', ''), block.get('token', '')


def set_gitlab(username, token):
    def mut(d):
        # A bare save resets the verified flag; the user must re-test
        # before the daemon will treat these creds as known-working.
        d['gitlab'] = {'username': username, 'token': token,
                       'confirmed': False}
    _update(mut)


def set_gitlab_confirmed(confirmed):
    """Persist the result of a successful (or failed) live test against
    ``gitlab.com``. Called by ``_h_test_gitlab``."""
    def mut(d):
        block = dict(d.get('gitlab', {}))
        block['confirmed'] = bool(confirmed)
        d['gitlab'] = block
    _update(mut)


def clear_gitlab():
    def mut(d):
        d.pop('gitlab', None)
    _update(mut)


# ── sync credential selection ───────────────────────────────────────────────

def host_for_url(url):
    """Classify a remote URL by host. Returns 'github' | 'gitlab' | None.
    None means: can't tell from the URL alone (self-hosted etc.) — caller
    should fall back to the user's saved ``collab_host``."""
    if not url:
        return None
    u = url.lower()
    if 'github.com' in u:
        return 'github'
    if 'gitlab.com' in u:
        return 'gitlab'
    return None


def get_sync_credentials(url=''):
    """Return (git_user, token) for the host best suited to *url*. Falls
    back to the user's saved ``collab_host`` when the URL is unrecognized
    (self-hosted, missing, etc.). Auto-refreshes GitHub tokens. Returns
    ('', '') if no credentials are stored for the chosen host."""
    host = host_for_url(url) or get_collab_host()
    if host == 'gitlab':
        return get_gitlab()
    _, token = get_valid_github_token()
    return 'x-access-token', token


def get_status():
    """Return a dict describing what's configured. Safe to hand to the UI;
    never contains raw tokens.

    Per host:
        ``connected``  — settings present (a token is on file).
        ``confirmed``  — settings tested OK against the host's live API.
                         Cleared on any settings change so the UI never
                         shows a stale verified badge.
    GitHub additionally exposes ``app_installed`` for the
    "Install GitHub App" CTA on the connect screen."""
    data = load()
    gh = data.get('github', {}) or {}
    gl = data.get('gitlab', {}) or {}
    gh_connected = bool(gh.get('access_token'))
    gh_app_installed = bool(gh.get('app_installed', False))
    gh_token_time = float(gh.get('token_time', 0) or 0)
    gh_expires_at = (gh_token_time + 8 * 3600) if gh_token_time else 0.0
    return {
        'host': data.get('collab_host', 'github'),
        'contributor': get_contributor(),
        'github': {
            'connected': gh_connected,
            'username': gh.get('username', ''),
            'app_installed': gh_app_installed,
            'confirmed': bool(gh.get('confirmed', False)),
            # Refresh-token health, surfaced so peers can show a
            # "Please re-authenticate by <deadline>" banner / toast
            # without polling a separate endpoint. Both fields are
            # always present (False / 0 when no token is stored)
            # so peers don't need defensive .get() everywhere.
            'refresh_broken': bool(gh.get('refresh_broken', False)),
            'access_token_expires_at': gh_expires_at,
        },
        'gitlab': {
            'connected': bool(gl.get('token')),
            'username': gl.get('username', ''),
            'confirmed': bool(gl.get('confirmed', False)),
        },
    }


# ── contributor (commit-author display name) ────────────────────────────────
#
# The user's display name as it appears in ``git log``. Stored in
# ``$AZT_HOME/config.json :: collab.contributor`` (sibling to
# ``ui.language``); this is suite-wide settings, not credentials, so
# it lives in config.json rather than credentials.json. Single source
# of truth: every peer reads this from the server instead of carrying
# its own ``"Your name"`` prefs row. Sync/init endpoints fall back to
# this value when the calling peer passes an empty contributor.

def _config_path():
    return os.path.join(azt_home(), 'config.json')


def _load_config_file():
    try:
        with open(_config_path()) as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_config_file(d):
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def get_contributor():
    """Stored display name for ``git log``. Empty string if unset."""
    return (_load_config_file().get('collab') or {}).get('contributor', '')


def set_contributor(name):
    """Persist the user's display name. Strips whitespace; an empty
    string clears the field (sync flows then revert to the
    ``'Recorder'`` default)."""
    cfg = _load_config_file()
    cfg.setdefault('collab', {})['contributor'] = (name or '').strip()
    _save_config_file(cfg)


def resolve_contributor(passed):
    """Pick the right contributor for a sync/commit op: caller's
    explicit value wins, then the stored display name, then the
    fallback ``'Recorder'``. Used by ``_h_project_sync`` /
    ``_h_init_project`` / ``scheduler._run_sync``."""
    return (passed or '').strip() or get_contributor() or 'Recorder'


# ── recent project (server-canonical) ───────────────────────────────────────
#
# Single source of truth for "what project did this device most recently
# touch?" Lives in ``$AZT_HOME/config.json :: recent.last_langcode`` so
# every peer (recorder, viewer, settings UI) reads the same value
# regardless of which platform/sandbox it runs in. Stamped server-side
# on every langcode-bound endpoint (``server._touch_project``) — peers
# don't have to remember to call ``set_last_project`` from the right
# load path; just touching the project via any RPC marks it recent.

def get_last_langcode():
    return (_load_config_file().get('recent') or {}).get('last_langcode', '')


def set_last_langcode(langcode):
    cfg = _load_config_file()
    cfg.setdefault('recent', {})['last_langcode'] = (langcode or '').strip()
    _save_config_file(cfg)


# ── migration from recorder's legacy prefs.json ─────────────────────────────

_LEGACY_GITHUB = {
    'gh_access_token': 'access_token',
    'gh_refresh_token': 'refresh_token',
    'gh_token_time': 'token_time',
    'gh_username': 'username',
    'gh_app_installed': 'app_installed',
}
_LEGACY_GITLAB = {
    'gl_username': 'username',
    'gl_token': 'token',
}
_LEGACY_HOST = 'collab_host'
# Obsolete / dead keys to also scrub from prefs.
# last_sync moved to projects.json in step 7 (per-project).
_LEGACY_DEAD = ('collab_username', 'collab_token', 'last_sync')


def migrate_from_prefs(prefs_path):
    """One-shot migration from an older recorder's prefs.json. Returns a
    dict describing what happened: {migrated: bool, copied: [...],
    stripped: [...]}. Idempotent — running twice is a no-op."""
    if not prefs_path or not os.path.isfile(prefs_path):
        return {'migrated': False, 'reason': 'prefs_not_found',
                'copied': [], 'stripped': []}
    try:
        with open(prefs_path) as f:
            prefs = json.load(f)
    except Exception as ex:
        return {'migrated': False, 'reason': f'prefs_read_error: {ex}',
                'copied': [], 'stripped': []}

    copied = []
    stripped = []
    creds = load()
    gh = dict(creds.get('github', {}))
    gl = dict(creds.get('gitlab', {}))

    # GitHub block
    for pref_key, creds_key in _LEGACY_GITHUB.items():
        if pref_key in prefs:
            # Creds wins if we've already migrated; otherwise copy.
            if creds_key not in gh:
                gh[creds_key] = prefs[pref_key]
                copied.append(pref_key)
            stripped.append(pref_key)

    # GitLab block
    for pref_key, creds_key in _LEGACY_GITLAB.items():
        if pref_key in prefs:
            if creds_key not in gl:
                gl[creds_key] = prefs[pref_key]
                copied.append(pref_key)
            stripped.append(pref_key)

    # Host selector
    if _LEGACY_HOST in prefs:
        if 'collab_host' not in creds:
            creds['collab_host'] = prefs[_LEGACY_HOST]
            copied.append(_LEGACY_HOST)
        stripped.append(_LEGACY_HOST)

    # Dead keys — just strip
    for k in _LEGACY_DEAD:
        if k in prefs:
            stripped.append(k)

    if not (copied or stripped):
        return {'migrated': False, 'reason': 'nothing_to_migrate',
                'copied': [], 'stripped': []}

    if gh:
        creds['github'] = gh
    if gl:
        creds['gitlab'] = gl
    save(creds)

    # Now strip from prefs and rewrite
    for k in stripped:
        prefs.pop(k, None)
    try:
        with open(prefs_path, 'w') as f:
            json.dump(prefs, f)
    except Exception as ex:
        print(f'[collab.store] prefs rewrite failed (creds saved anyway): {ex}')

    return {'migrated': True, 'copied': copied, 'stripped': stripped}


# ── Legacy compatibility shims ─────────────────────────────────────────────
# Old callers used save_tokens(prefs_path, token_data, username) and
# get_valid_token(prefs_path). Route to the new store so anything that
# still imports these keeps working.

def save_tokens(prefs_path, token_data, username=''):
    """Deprecated: use set_github_tokens. ``prefs_path`` is ignored — tokens
    now live in $AZT_HOME/credentials.json."""
    set_github_tokens(
        access_token=token_data.get('access_token', ''),
        refresh_token=token_data.get('refresh_token', ''),
        username=username,
    )


def get_valid_token(prefs_path=None):
    """Deprecated: use get_valid_github_token. ``prefs_path`` is ignored."""
    return get_valid_github_token()
