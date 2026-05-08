"""
GitHub App device flow, token refresh, app install / repo access checks,
GitLab collaborator add. Uses a GitHub App with device flow — only the
public client_id is embedded in the app.
"""

import json
import time

from . import config as _config
from . import status as S
from .status import Status, AuthError
from .net import _ensure_ssl

# ── GitHub App configuration ─────────────────────────────────────────────────
# Values live in azt_collabd.config. Host apps call
# ``azt_collabd.configure(app_slug=..., client_id=..., collaborator=...)``
# once at startup; defaults match the recorder.
#
# For backwards compatibility with legacy attribute access
# (``from collab import GITHUB_APP_CLIENT_ID`` etc.), this module
# exposes module-level ``__getattr__`` below so the four historical
# constants always reflect the current config.


def __getattr__(name):
    if name == 'GITHUB_APP_CLIENT_ID':
        return _config.get()['client_id']
    if name == 'GITHUB_APP_NAME':
        return _config.get()['app_slug']
    if name == 'GITHUB_COLLABORATOR':
        return _config.get()['collaborator']
    if name == 'GITHUB_APP_INSTALL_URL':
        return _config.install_url()
    raise AttributeError(
        f'module {__name__!r} has no attribute {name!r}')


# ---------------------------------------------------------------------------
# GitHub App Device Flow authentication
# ---------------------------------------------------------------------------

def device_flow_start():
    """Begin device flow. Returns dict with 'user_code', 'verification_uri',
    'device_code', 'interval', 'expires_in' — or raises on error."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    req = Request(
        'https://github.com/login/device/code',
        data=f'client_id={_config.get()["client_id"]}&scope=repo'.encode(),
        headers={'Accept': 'application/json'},
        method='POST',
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def device_flow_poll(device_code, interval=5, expires_in=900):
    """Poll until user authorizes or timeout. Returns token dict or raises.

    Token dict keys: access_token, refresh_token, token_type, etc.
    Blocks the calling thread (run in background).
    """
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        data = (
            f'client_id={_config.get()["client_id"]}'
            f'&device_code={device_code}'
            f'&grant_type=urn:ietf:params:oauth:grant-type:device_code'
        ).encode()
        req = Request(
            'https://github.com/login/oauth/access_token',
            data=data,
            headers={'Accept': 'application/json'},
            method='POST',
        )
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except HTTPError:
            continue
        except OSError:
            # Network glitch (e.g. ECONNREFUSED) — retry
            continue
        if 'access_token' in result:
            return result
        error = result.get('error', '')
        if error == 'authorization_pending':
            continue
        elif error == 'slow_down':
            interval = result.get('interval', interval + 5)
            continue
        elif error == 'expired_token':
            raise AuthError(Status(S.AUTH_EXPIRED))
        elif error == 'access_denied':
            raise AuthError(Status(S.AUTH_DENIED))
        else:
            raise RuntimeError(f'Device flow error: {error}')
    raise AuthError(Status(S.AUTH_TIMEOUT))


def refresh_access_token(refresh_token):
    """Refresh an expired access token. Returns new token dict."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    data = (
        f'client_id={_config.get()["client_id"]}'
        f'&grant_type=refresh_token'
        f'&refresh_token={refresh_token}'
    ).encode()
    req = Request(
        'https://github.com/login/oauth/access_token',
        data=data,
        headers={'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except Exception as ex:
        raise RuntimeError(f'Token refresh network error: {ex}')
    if 'access_token' in result:
        return result
    raise RuntimeError(f'Token refresh failed: {result.get("error", "unknown")}')


def get_github_username(token):
    """Return the authenticated user's GitHub username."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    req = Request(
        'https://api.github.com/user',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get('login', '')
    except Exception as ex:
        print(f'[collab] get_github_username failed: {ex}')
        return ''


def check_app_installed(token, account_login=None):
    """Check the GitHub App's status, optionally for a specific
    account (user or org).

    Returns ``{'installed': bool, 'installation_id': int|None,
    'all_repos': bool, 'suspended': bool}``.

    ``account_login`` (case-insensitive) narrows the match to the
    installation whose ``account.login`` matches. This is essential
    when the user is in multiple orgs that also have the App
    installed — ``/user/installations`` returns ALL of them, not just
    the user's personal account. Without account-matching, the first
    match wins and a Verify-setup against the user's personal install
    can silently report success when actually the matched install is
    on some unrelated org. Regression that bit a real user:
    uninstalled the personal install, kept the org installs by
    accident, the screen continued to show "Setup complete" because
    we found one of the org installs first.

    Pass ``account_login=None`` (default) to keep the legacy
    "first install with matching app_slug" behavior — only useful
    for diagnostics where any install is enough to proceed.

    ``installed`` is True only if the App is **installed AND active**
    for the matched account. A suspended installation reports
    ``installed=False`` (because git operations against it 403) with
    ``suspended=True`` so the UI can route the user to the resume
    page instead of the install page. ``installation_id`` is
    populated whenever the match hits, regardless of suspension
    state."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    result = {
        'installed': False,
        'installation_id': None,
        'all_repos': False,
        'suspended': False,
    }
    req = Request(
        'https://api.github.com/user/installations',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        },
    )
    import sys
    target = (account_login or '').lower()
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        app_slug = _config.get()['app_slug']
        all_entries = [
            (inst.get('app_slug'),
             (inst.get('account') or {}).get('login'),
             inst.get('id'),
             inst.get('suspended_at'))
            for inst in data.get('installations', [])
        ]
        print(f'[check_app_installed] looking for app_slug={app_slug!r} '
              f'account_login={account_login!r}; '
              f'/user/installations returned {len(all_entries)} entries: '
              f'{all_entries!r}',
              file=sys.stderr, flush=True)
        for inst in data.get('installations', []):
            if inst.get('app_slug') != app_slug:
                continue
            if target:
                inst_account = (
                    (inst.get('account') or {}).get('login') or '')
                if inst_account.lower() != target:
                    continue
            result['installation_id'] = inst.get('id')
            # 'all' means all repos, 'selected' means specific repos
            result['all_repos'] = (
                inst.get('repository_selection') == 'all')
            suspended_at = inst.get('suspended_at')
            inst_account = (
                (inst.get('account') or {}).get('login') or '?')
            print(f'[check_app_installed] match: id={inst.get("id")} '
                  f'account={inst_account!r} '
                  f'suspended_at={suspended_at!r} '
                  f'repository_selection={inst.get("repository_selection")!r}',
                  file=sys.stderr, flush=True)
            if suspended_at:
                result['suspended'] = True
            else:
                result['installed'] = True
            break
        print(f'[check_app_installed] result={result!r}',
              file=sys.stderr, flush=True)
    except HTTPError as ex:
        print(f'[check_app_installed] HTTPError {ex.code}: {ex.reason}',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[check_app_installed] {type(ex).__name__}: {ex}',
              file=sys.stderr, flush=True)
    return result


def check_repo_in_installation(token, installation_id, owner, repo_name):
    """Check if a specific repo is accessible to the app installation.
    Returns True if accessible, False otherwise."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    # List repos accessible to the installation (paginated, check first page)
    req = Request(
        f'https://api.github.com/user/installations/{installation_id}'
        f'/repositories?per_page=100',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for r in data.get('repositories', []):
            if r.get('full_name', '').lower() == f'{owner}/{repo_name}'.lower():
                return True
        return False
    except HTTPError:
        return False


def app_install_url(installation_id=None):
    """Return the URL to install or configure the GitHub App."""
    if installation_id:
        return f'https://github.com/settings/installations/{installation_id}'
    return _config.install_url()


def diagnose_403(token, remote_url):
    """Diagnose a 403 push/pull failure. Returns a Status carrying the
    code (AUTH_REQUIRED / APP_SUSPENDED / APP_NOT_INSTALLED /
    REPO_NOT_AUTHORIZED / ACCESS_DENIED) and any params the UI needs
    to show a link.

    APP_SUSPENDED is surfaced when the App is installed but paused
    via GitHub's UI — the 403 is from the install being suspended,
    not from a missing install or repo permission. The user fixes
    this by going to ``settings/installations/<id>`` and resuming;
    the Status carries that URL."""
    if not token:
        return Status(S.AUTH_REQUIRED)
    info = check_app_installed(token)
    install_id = info['installation_id']
    if info['suspended']:
        return Status(S.APP_SUSPENDED,
                      {'url': app_install_url(install_id)})
    if not info['installed']:
        return Status(S.APP_NOT_INSTALLED,
                      {'url': _config.install_url()})
    if not info['all_repos']:
        import re
        m = re.search(r'github\.com[/:]([^/]+)/([^/.]+)', remote_url)
        if m:
            owner, repo_name = m.group(1), m.group(2)
            if not check_repo_in_installation(token, install_id, owner, repo_name):
                settings_url = app_install_url(install_id)
                return Status(S.REPO_NOT_AUTHORIZED,
                              {'owner_repo': f'{owner}/{repo_name}',
                               'url': settings_url})
    return Status(S.ACCESS_DENIED,
                  {'url': app_install_url(install_id)})


# Backward-compatible name for any remaining internal callers (deleted
# later in this migration).
_diagnose_403 = diagnose_403


def test_github_credentials(token):
    """Hit ``api.github.com/user`` with the supplied access token.
    Returns ``{'valid': bool, 'server_username': str,
    'app_installed': bool, 'error': str}`` — callers translate to
    user-visible text. ``app_installed`` is best-effort: we run
    ``check_app_installed`` opportunistically so the same Test button
    refreshes both flags at once. Mirror of
    ``test_gitlab_credentials`` so the UI's per-host Test buttons have
    a uniform shape."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    if not token:
        return {'valid': False, 'server_username': '',
                'app_installed': False, 'app_suspended': False,
                'installation_id': None, 'error': 'missing_token'}
    req = Request(
        'https://api.github.com/user',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        if e.code in (401, 403):
            return {'valid': False, 'server_username': '',
                    'app_installed': False, 'app_suspended': False,
                    'installation_id': None, 'error': 'invalid_token'}
        return {'valid': False, 'server_username': '',
                'app_installed': False, 'app_suspended': False,
                'installation_id': None, 'error': f'http_{e.code}'}
    except URLError as e:
        return {'valid': False, 'server_username': '',
                'app_installed': False, 'app_suspended': False,
                'installation_id': None,
                'error': f'network_error: {e.reason}'}
    except Exception as e:
        return {'valid': False, 'server_username': '',
                'app_installed': False, 'app_suspended': False,
                'installation_id': None,
                'error': f'{type(e).__name__}: {e}'}
    server_username = data.get('login', '') or ''
    # Best-effort app-install probe — same token, separate endpoint.
    # A failure here is non-fatal: the credential test still passes.
    # ``app_suspended`` and ``installation_id`` are surfaced
    # alongside ``app_installed`` so the UI can route a suspended
    # user to the resume page rather than the generic install page.
    #
    # Match the installation against the user's own GitHub login —
    # /user/installations also returns installs on orgs the user
    # belongs to, and we don't want a member-of-org install
    # masquerading as the user's personal install. If the user
    # uninstalled the personal one but stayed in an org that
    # happens to have azt-collaboration installed, an unscoped
    # check would still report ``installed=True`` and the screen
    # would lie about being set up. Real bug, observed.
    try:
        info = check_app_installed(token, account_login=server_username)
        app_installed = bool(info.get('installed'))
        app_suspended = bool(info.get('suspended'))
        installation_id = info.get('installation_id')
    except Exception:
        app_installed = False
        app_suspended = False
        installation_id = None
    return {
        'valid': True,
        'server_username': server_username,
        'app_installed': app_installed,
        'app_suspended': app_suspended,
        'installation_id': installation_id,
        'error': '',
    }


def test_gitlab_credentials(username, token):
    """Hit GitLab's ``/api/v4/user`` with the supplied PAT and confirm
    the returned ``username`` matches. Returns
    ``{'valid': bool, 'server_username': str, 'error': str}`` —
    callers translate to user-visible text."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    if not username or not token:
        return {'valid': False, 'server_username': '',
                'error': 'missing_username_or_token'}
    req = Request(
        'https://gitlab.com/api/v4/user',
        headers={'PRIVATE-TOKEN': token},
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        if e.code in (401, 403):
            return {'valid': False, 'server_username': '',
                    'error': 'invalid_token'}
        return {'valid': False, 'server_username': '',
                'error': f'http_{e.code}'}
    except URLError as e:
        return {'valid': False, 'server_username': '',
                'error': f'network_error: {e.reason}'}
    except Exception as e:
        return {'valid': False, 'server_username': '',
                'error': f'{type(e).__name__}: {e}'}
    server_username = data.get('username', '') or ''
    if server_username.lower() != username.lower():
        return {'valid': False, 'server_username': server_username,
                'error': 'username_mismatch'}
    return {'valid': True, 'server_username': server_username, 'error': ''}


def add_collaborator(owner, repo_name, collaborator, token):
    """Add *collaborator* to *owner/repo_name* on GitHub. Silently succeeds if
    already a collaborator or if the invitation was already sent."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    url = f'https://api.github.com/repos/{owner}/{repo_name}/collaborators/{collaborator}'
    req = Request(
        url,
        data=json.dumps({'permission': 'push'}).encode(),
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
        },
        method='PUT',
    )
    try:
        with urlopen(req, timeout=15) as resp:
            pass  # 201 = invited, 204 = already collaborator
    except HTTPError as e:
        if e.code not in (204, 422):  # 422 = already invited
            print(f'[collab] add collaborator failed ({e.code}): {e.read()[:200]}')
