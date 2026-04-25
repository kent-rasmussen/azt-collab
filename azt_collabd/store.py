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
        "token": "..."
      }
    }

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
    """Replace the GitHub token block."""
    def mut(d):
        block = dict(d.get('github', {}))
        block['access_token'] = access_token
        block['refresh_token'] = refresh_token or block.get('refresh_token', '')
        block['token_time'] = (time.time() if token_time is None
                               else float(token_time))
        if username:
            block['username'] = username
        d['github'] = block
    _update(mut)


def set_github_app_installed(installed):
    def mut(d):
        block = dict(d.get('github', {}))
        block['app_installed'] = bool(installed)
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
    # Access tokens last 8 hours; refresh proactively at 7h
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
            # Return the old token — it might still work
    return username, token


# ── GitLab ──────────────────────────────────────────────────────────────────

def get_gitlab():
    block = load().get('gitlab', {}) or {}
    return block.get('username', ''), block.get('token', '')


def set_gitlab(username, token):
    def mut(d):
        d['gitlab'] = {'username': username, 'token': token}
    _update(mut)


def clear_gitlab():
    def mut(d):
        d.pop('gitlab', None)
    _update(mut)


# ── sync credential selection ───────────────────────────────────────────────

def get_sync_credentials():
    """Return (git_user, token) for the currently selected host. Auto-refreshes
    GitHub tokens. Returns ('', '') if not configured."""
    host = get_collab_host()
    if host == 'gitlab':
        user, token = get_gitlab()
        return user, token
    _, token = get_valid_github_token()
    return 'x-access-token', token


def get_status():
    """Return a dict describing what's configured. Safe to hand to the UI;
    never contains raw tokens."""
    data = load()
    gh = data.get('github', {}) or {}
    gl = data.get('gitlab', {}) or {}
    return {
        'host': data.get('collab_host', 'github'),
        'github': {
            'connected': bool(gh.get('access_token')),
            'username': gh.get('username', ''),
            'app_installed': bool(gh.get('app_installed', False)),
        },
        'gitlab': {
            'connected': bool(gl.get('token')),
            'username': gl.get('username', ''),
        },
    }


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
