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


def get_daemon_log_to_file():
    """True when the daemon should mirror its stderr to
    ``$AZT_HOME/daemon.log``.

    Configured via the daemon settings UI's "Save daemon log to
    file" toggle. The on-disk file accumulates daemon-side
    diagnostic output (``[boot-trace-daemon]``, ``[cawl]``,
    ``[recent]``, ``[first-try]`` from the daemon UI / picker
    subprocess) that would otherwise only land in logcat. Useful
    when a remote tester reproduces a bug on a device that
    doesn't have adb access — the daemon UI's "Share daemon log"
    button can dispatch the file through any sharing app.

    Default: False. Off until the user explicitly turns it on,
    so we don't accumulate a log file on devices that don't
    need it."""
    cfg = _load_config_file()
    val = (cfg.get('logging') or {}).get('daemon_log_to_file', False)
    return bool(val)


def set_daemon_log_to_file(enabled):
    """Persist the daemon-log-to-file toggle. Takes effect on the
    next daemon process start — the stderr tee can't be installed
    or removed from outside the daemon's own process."""
    cfg = _load_config_file()
    cfg.setdefault('logging', {})['daemon_log_to_file'] = bool(enabled)
    _save_config_file(cfg)


def get_contributor():
    """Stored display name for ``git log``. Empty string if unset.

    Peers consume via ``GET /v1/config/contributor`` (the
    ``azt_collab_client.get_contributor()`` wrapper). The daemon
    uses this directly for every commit / sync / init op —
    callers do NOT pass a contributor through the wire; the
    ``'sole authoritative source'`` rule (NOTES_TO_DAEMON.md)
    means the daemon owns it without peer override. If empty,
    commit-issuing endpoints refuse with ``S.CONTRIBUTOR_UNSET``
    rather than fall back to a placeholder string."""
    return (_load_config_file().get('collab') or {}).get('contributor', '')


def set_contributor(name):
    """Persist the user's display name. Strips whitespace; an empty
    string clears the field (commit ops then refuse with
    ``S.CONTRIBUTOR_UNSET`` until a name is set again)."""
    cfg = _load_config_file()
    cfg.setdefault('collab', {})['contributor'] = (name or '').strip()
    _save_config_file(cfg)


# ── device name (commit identity disambiguator) ─────────────────────────────
#
# A short human-or-machine-readable label that disambiguates which
# device authored a commit when the same human ``contributor`` is
# active on multiple installs. Stored in
# ``$AZT_HOME/config.json :: collab.device_name`` next to
# ``contributor``. The daemon composes the git author email slot
# from it (``<safe_contributor>@<safe_device_name>``) so GitHub's
# author-aggregation still groups by human, while ``git log
# --format='%ae'`` differentiates by device when needed.
#
# Auto-populated on first read if unset: Android picks up
# ``Settings.Global.DEVICE_NAME`` (the user-customised "Marie's
# Tablet" name when set), falling back to
# ``Build.MANUFACTURER + " " + Build.MODEL`` (factory string).
# Desktop falls back to ``socket.gethostname()``. Last-resort
# fallback is the literal ``'unknown-device'`` — obviously a
# placeholder, not pretending to be real.


def _autodetect_device_name():
    """Best-effort device name when none is stored. See module
    docstring for the resolution order."""
    # Android first (jnius is the platform tell).
    try:
        from jnius import autoclass  # type: ignore[import-not-found]
        try:
            Settings_Global = autoclass('android.provider.Settings$Global')
            ActivityThread = autoclass('android.app.ActivityThread')
            app = ActivityThread.currentApplication()
            if app is not None:
                resolver = app.getContentResolver()
                # DEVICE_NAME is the user-customised "Marie's Tablet"
                # string. May be null / empty on factory builds.
                name = Settings_Global.getString(resolver, 'device_name')
                if name and name.strip():
                    return name.strip()
        except Exception:
            pass
        try:
            Build = autoclass('android.os.Build')
            mfr = (Build.MANUFACTURER or '').strip()
            model = (Build.MODEL or '').strip()
            if mfr or model:
                return f'{mfr} {model}'.strip()
        except Exception:
            pass
    except ImportError:
        pass
    # Desktop fallback.
    try:
        import socket
        name = (socket.gethostname() or '').strip()
        if name:
            return name
    except Exception:
        pass
    return 'unknown-device'


def get_device_name():
    """Stored device-name label. Auto-populates (and persists) on
    first read if unset, so callers never see an empty string
    after the first call. Empty input to ``set_device_name`` later
    *also* triggers re-autodetection on next read.

    The auto-populated value is just a best-effort default — the
    user can override via the settings UI for clarity / privacy
    ("Marie's tablet" instead of "SM-T580")."""
    stored = (_load_config_file().get('collab') or {}).get(
        'device_name', '')
    if stored:
        return stored
    detected = _autodetect_device_name()
    # Persist so subsequent calls (and other peers on this device)
    # see a stable value without re-running the autodetect probes.
    cfg = _load_config_file()
    cfg.setdefault('collab', {})['device_name'] = detected
    try:
        _save_config_file(cfg)
    except OSError:
        # Persist failure is not fatal — caller still gets the
        # autodetected value; next call will re-detect. Logging
        # would be noisy in tests.
        pass
    return detected


def set_device_name(name):
    """Persist the user's device-name override. Strips whitespace;
    empty string clears, causing ``get_device_name`` to re-detect
    on next read."""
    cfg = _load_config_file()
    cfg.setdefault('collab', {})['device_name'] = (name or '').strip()
    _save_config_file(cfg)


# ── recent project (server-canonical) ───────────────────────────────────────
#
# Single source of truth for "what project did this device most recently
# touch?" Lives in ``$AZT_HOME/config.json :: recent.last_langcode`` so
# every peer (recorder, viewer, settings UI) reads the same value
# regardless of which platform/sandbox it runs in. Stamped server-side
# on every langcode-bound endpoint (``server._touch_project``) — peers
# don't have to remember to call ``set_last_project`` from the right
# load path; just touching the project via any RPC marks it recent.

# In-memory cache of ``recent.last_langcode``. Hot endpoints
# (``_touch_project`` from cawl_image / get_audio / project_status)
# read+write this value tens of times per second; on Android internal
# storage every hit would otherwise pay an atomic-rename of
# ``config.json``. ``None`` = not yet loaded; ``''`` = loaded, no
# project ever touched (a valid persistent state).
_last_langcode_cache = None


def get_last_langcode():
    global _last_langcode_cache
    if _last_langcode_cache is None:
        _last_langcode_cache = (
            (_load_config_file().get('recent') or {}).get('last_langcode', ''))
    return _last_langcode_cache


def set_last_langcode(langcode):
    """Stamp *langcode* as the most-recently-touched project. Empty /
    whitespace-only values are **refused** here as a defensive
    invariant: nothing in the daemon should ever land ``''`` on disk
    for ``recent.last_langcode``. The only legitimate empty state is
    "key absent" (first boot, no project ever touched), and
    ``get_last_langcode()`` returns ``''`` for that case naturally.
    Picker-cancel does **not** write anything — it's a no-op
    server-side, and the peer's ``on_resume`` comparison handles it
    for free (peer's ``_current_langcode`` equals the daemon's
    unchanged ``last_langcode``, so no reload fires).

    No-op when *langcode* already matches the in-memory cache — no
    disk write, no log. Server-side ``_touch_project`` also checks
    before calling, so the redundant-write path is doubly guarded."""
    global _last_langcode_cache
    val = (langcode or '').strip()
    if not val:
        import sys as _sys
        print('[recent] set_last_langcode refused empty value '
              '(no legitimate caller passes empty; treating as no-op)',
              file=_sys.stderr, flush=True)
        return
    if _last_langcode_cache is None:
        _last_langcode_cache = (
            (_load_config_file().get('recent') or {}).get('last_langcode', ''))
    if val == _last_langcode_cache:
        return
    cfg = _load_config_file()
    cfg.setdefault('recent', {})['last_langcode'] = val
    _save_config_file(cfg)
    _last_langcode_cache = val


# ── CAWL prefetch policy ────────────────────────────────────────────────────
#
# ``$AZT_HOME/config.json :: cawl.prefetch_all_variants`` controls how many
# images per CAWL id the daemon warms. The canonical CAWL repo carries
# multiple variants per id (line-art, colour, simplified, …) named with a
# ``__`` marker convention; the default policy is "one variant per id"
# (the file containing ``__`` in its basename), which keeps the warm
# bounded to ~one-per-line. Switching to ``True`` warms every variant —
# heavier on network and disk but useful when bandwidth is cheap and
# the user wants the broader image set available offline.

def get_cawl_prefetch_all_variants():
    """Read the prefetch policy. Default False (preferred-only)."""
    return bool(
        (_load_config_file().get('cawl') or {}).get(
            'prefetch_all_variants', False))


def set_cawl_prefetch_all_variants(enabled):
    """Persist the prefetch policy."""
    cfg = _load_config_file()
    cfg.setdefault('cawl', {})['prefetch_all_variants'] = bool(enabled)
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
