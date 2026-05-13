"""
Loopback HTTP/JSON server.

Binds 127.0.0.1 on an OS-assigned port and writes
``$AZT_HOME/server.json`` with ``{port, token, pid, version}`` so clients
can discover the endpoint. Every request (except ``GET /v1/health``)
requires ``Authorization: Bearer <token>``.

Endpoints:
    GET  /v1/health                           unauthenticated liveness probe
    GET  /v1/online                           wraps net._has_internet
    GET  /v1/credentials/status               describes what's configured
    POST /v1/credentials/host                 {host}
    POST /v1/credentials/github/tokens        {access_token, refresh_token,
                                               username, token_time?}
    POST /v1/credentials/github/app_installed {installed}
    POST /v1/credentials/gitlab               {username, token}
    POST /v1/credentials/gitlab/test          {username?, token?}
                                              — falls back to stored creds
                                                if body fields are absent
    POST /v1/credentials/migrate_from_prefs   {prefs_path}
    GET  /v1/recent/last_project              → {langcode}
    POST /v1/recent/last_project              {langcode}
                                              — explicit override; every
                                                langcode-bound endpoint
                                                already auto-stamps via
                                                _touch_project
    POST /v1/sync                             {project_dir, contributor}
                                              — creds come from the store
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import signal
import socketserver
import sys
import threading

from . import auth
from . import cawl as _cawl
from . import config as _config
from . import projects
from . import scheduler
from . import store
from .locks import project_lock
from .net import _has_internet
from .paths import azt_home, server_info_path
from .repo import sync_repo as _sync_repo, repo_status_summary as _repo_status
from .status import Result, Status
from . import status as S
from . import __version__ as _VERSION
from . import MIN_CLIENT_VERSION as _MIN_CLIENT_VERSION

# Kept alive for the server's lifetime so the flock on server.lock stays held.
_server_lock_fd = None
_started_at = 0.0


_AZTCOLLAB_AUTHORITY = 'org.atoznback.aztcollab'


def _on_android():
    """Detect Android via jnius availability. Used by the API-response
    URI conversion below — the daemon's projects.json stores
    filesystem paths (the daemon needs them for dulwich), but peer
    apps on Android can't open() those paths because they're inside
    the server APK's private filesDir. So API responses convert
    lift_path to a content:// URI on Android, which peers feed
    directly to LiftHandle (which transparently handles both
    filesystem paths and content URIs)."""
    try:
        import jnius  # noqa: F401
        return True
    except ImportError:
        return False


def _project_for_api(p):
    """Convert a Project to its API-facing dict.

    Two adaptations:

    * On Android, replaces ``lift_path`` (a filesystem path inside
      the server APK's sandbox, useless to peer apps in other
      packages) with the equivalent content URI under our
      ContentProvider authority. The URI shape mirrors what the
      picker emits in its result Intent —
      ``content://org.atoznback.aztcollab/<lang>/<basename>`` — so
      peers that called LiftHandle on the picker URI can do the same
      with the Project from list_projects / open_project on later
      runs without knowing about the path-vs-URI distinction.

    * Adds a ``lift_exists`` boolean computed against the actual
      filesystem path. The daemon's projects.json can outlive the
      LIFT file (the file may be deleted out-of-band: user wipe,
      external rm, sync conflict resolution, etc.). Peers that resolve
      a recent / favourite project to a Project record need to know
      whether the file is still openable BEFORE they hand the URI to
      LiftHandle and crash on a not-found. UI can hide / mark / offer
      re-clone based on this flag.
    """
    d = p.to_dict()
    fs_path = p.lift_path  # always the filesystem path, pre-URI
    d['lift_exists'] = bool(fs_path and os.path.isfile(fs_path))
    if _on_android() and d.get('lift_path'):
        # If lift_path is already a URI (e.g. registered by a future
        # caller that did its own conversion), don't double-wrap.
        if not d['lift_path'].startswith('content://'):
            basename = os.path.basename(d['lift_path'])
            d['lift_path'] = (
                f'content://{_AZTCOLLAB_AUTHORITY}/'
                f'{p.langcode}/{basename}')
    return d


def _state_dir():
    p = os.path.join(azt_home(), 'state')
    os.makedirs(p, exist_ok=True)
    return p


def _crash_log_path():
    return os.path.join(_state_dir(), 'crash.log')


def _started_path():
    return os.path.join(_state_dir(), 'started.json')


def _last_crash_summary():
    path = _crash_log_path()
    try:
        with open(path) as f:
            data = f.read()
    except FileNotFoundError:
        return None
    if not data:
        return None
    # Each crash is appended as a JSON line; take the last one
    last = ''
    for line in data.splitlines():
        if line.strip():
            last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except Exception:
        return {'raw': last[-200:]}


def _last_started_summary():
    try:
        with open(_started_path()) as f:
            return json.load(f)
    except Exception:
        return None


def _record_started():
    global _started_at
    import time
    _started_at = time.time()
    try:
        with open(_started_path(), 'w') as f:
            json.dump({'pid': os.getpid(), 'ts': _started_at,
                       'version': _VERSION}, f)
    except OSError:
        pass


def _record_crash(exc, where=''):
    import time
    import traceback
    try:
        with open(_crash_log_path(), 'a') as f:
            f.write(json.dumps({
                'ts': time.time(),
                'pid': os.getpid(),
                'version': _VERSION,
                'where': where,
                'type': type(exc).__name__,
                'message': str(exc)[:500],
                'tb': ''.join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__))[-2000:],
            }) + '\n')
    except OSError:
        pass


def _acquire_server_lock(lock_path):
    """Take an exclusive flock on *lock_path* so only one azt_collabd per
    AZT_HOME runs at a time. Returns the file descriptor on success, or
    None if another instance holds the lock. On platforms without fcntl
    (Windows), returns the fd without real locking (first-come wins by
    server.json existence instead)."""
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        import fcntl as _fcntl
    except ImportError:
        return fd
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.truncate(fd, 0)
        os.write(fd, f'{os.getpid()}\n'.encode())
    except OSError:
        pass
    return fd


# ---------------------------------------------------------------------------
# Transport-agnostic dispatch table
#
# Both the HTTP handler below and the future Android ContentProvider call
# dispatch(method, path, body) → (status: int, response: dict). Auth is
# the caller's responsibility; the dispatcher trusts that anything reaching
# it has been authorized. /v1/health is the only path the dispatcher handles
# specially: callers may invoke it without auth.
# ---------------------------------------------------------------------------

UNAUTHENTICATED_PATHS = ('/v1/health',)


def _h_health(_body):
    # Test hook: if ``$AZT_HOME/_debug_force_503`` exists, return
    # 503 so the bootstrap can exercise the "AZT Collaboration not
    # responding" popup that fires after warm-up retries exhaust.
    # Toggle without restarting the daemon — the file presence is
    # checked per-request, so create / remove takes effect on the
    # next /v1/health probe.
    #
    # On Android (where ``$AZT_HOME`` is the server APK's private
    # filesDir, not user-writable from outside the app), create
    # the sentinel via adb:
    #
    #     adb shell run-as org.atoznback.aztcollab \
    #         touch files/azt/_debug_force_503
    #
    # Remove with ``rm files/azt/_debug_force_503`` to restore
    # normal behaviour. On desktop the sentinel lives at
    # ``~/.local/share/azt/_debug_force_503``.
    if os.path.exists(os.path.join(azt_home(), '_debug_force_503')):
        return 503, {"ok": False, "error": "daemon_not_ready",
                     "debug": "sentinel file present"}
    payload = {
        "ok": True, "version": _VERSION,
        "min_client_version": _MIN_CLIENT_VERSION,
        "pid": os.getpid(),
        "started_at": _started_at,
    }
    crash = _last_crash_summary()
    if crash is not None:
        payload['last_crash'] = crash
    return 200, payload


def _h_online(_body):
    return 200, {"ok": True, "online": _has_internet()}


def _h_credentials_status(_body):
    return 200, {"ok": True, **store.get_status()}


def _h_get_contributor(_body):
    return 200, {"ok": True, "contributor": store.get_contributor()}


def _h_set_contributor(body):
    name = body.get('contributor', '')
    store.set_contributor(name)
    return 200, {"ok": True, "contributor": store.get_contributor()}


def _h_get_ui_language(_body):
    """Return the daemon-side UI language persisted in
    ``$AZT_HOME/config.json::ui.language``.

    Why this needs to be a daemon endpoint: ``$AZT_HOME`` resolves
    to the calling process's *private* filesDir on Android (server
    APK has its own, each peer has its own). The language picker
    lives in the server APK's settings UI, which writes to the
    *server's* config.json. Peers reading their own config.json
    never see that value, so peer-side dialogs (bootstrap popups,
    in-particular) stay in English even when the user has picked
    French in the server UI. This endpoint exposes the canonical
    server-side preference so peers can mirror at startup."""
    import json
    import os
    from . import paths
    cfg_path = os.path.join(paths.azt_home(), 'config.json')
    try:
        with open(cfg_path) as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        return 200, {"ok": True, "language": ''}
    lang = ((cfg.get('ui') or {}).get('language', '') or '')
    return 200, {"ok": True, "language": str(lang)}


def _h_cawl_index(langcode, _body):
    """``GET /v1/projects/<lang>/cawl/index`` — serve the
    daemon-owned CAWL image-URL index for ``langcode``'s repo.

    Resolves the project's ``cawl_image_repo`` (per-project field,
    falling back to the daemon-global default for projects without
    an override), then returns the cached/fetched index dict for
    that repo. Two projects pointing at the same repo share a
    single cache directory under
    ``$AZT_HOME/cawl/<owner>/<repo>/index.json``, so the dedup
    happens transparently without the peer needing to know the
    repo slug.

    Why this lives on the daemon: peers used to hit GitHub
    directly on every project load and the unauthenticated 60/hr
    API rate limit got exhausted by even modest development /
    multi-peer use. The daemon caches once per device per TTL
    window (24h default), per repo, and a network failure falls
    back to the stale cache rather than returning empty so peers
    can resolve illustrations even when GitHub is unreachable.

    Returns an empty ``index`` dict when no repo is configured for
    this project — same shape peers got pre-migration from an
    empty resolver, so no new failure branch is required."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    repo = _cawl.resolve_image_repo(langcode)
    try:
        index = _cawl.get_index(repo)
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    return 200, {"ok": True, "index": index, "image_repo": repo}


def _h_cawl_image_bytes(langcode, basename):
    """Return ``(status, content_type, data_bytes)`` for the cached
    CAWL image binary, fetching it lazily if not yet on disk.

    Not part of the JSON dispatch table because it returns binary
    bytes, not a JSON dict. The HTTP handler routes the path
    directly to this function (bypassing ``dispatch``) and emits
    via ``_send_bytes``. The ContentProvider transport gets the
    same bytes via ``openFile`` → ``_resolve_path`` returning the
    cached file's absolute path, which calls into this function
    indirectly through ``cawl.get_image_path``.

    Status codes:
        200 — bytes available (cache hit or successful fetch).
        404 — project unknown, basename rejected, OR fetch failed
              and no cached copy exists. Logged on stderr; the
              peer should fall through to its no-image rendering.
        500 — unexpected internal error.

    No 502 distinct from 404: peers don't distinguish "image not
    in repo" from "couldn't reach repo" — both end in "no
    illustration for this entry", and the daemon's stale-cache
    fallback already covers the recoverable case."""
    p = projects.get(langcode)
    if p is None:
        return 404, 'application/json', \
            b'{"ok":false,"error":"project_not_found"}'
    _touch_project(langcode)
    repo = _cawl.resolve_image_repo(langcode)
    if not repo:
        return 404, 'application/json', \
            b'{"ok":false,"error":"no_image_repo_configured"}'
    target = _cawl.get_image_path(repo, basename)
    if target is None:
        return 404, 'application/json', \
            b'{"ok":false,"error":"image_unavailable"}'
    try:
        with open(target, 'rb') as f:
            data = f.read()
    except OSError as ex:
        return 500, 'application/json', \
            json.dumps({"ok": False,
                        "error": f'cache_read: {ex}'}).encode()
    return 200, _content_type_for(basename), data


_CONTENT_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.svg': 'image/svg+xml',
}


def _content_type_for(basename):
    ext = os.path.splitext(basename)[1].lower()
    return _CONTENT_TYPES.get(ext, 'application/octet-stream')


def _h_set_cawl_image_repo(langcode, body):
    """``POST /v1/projects/<lang>/cawl_image_repo`` — persist the
    per-project image_repo override. Body: ``{cawl_image_repo:
    'owner/repo'}``. Empty string explicitly clears the override,
    falling the project back to the daemon-global default."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    repo = body.get('cawl_image_repo')
    if repo is None or not isinstance(repo, str):
        return 400, {"ok": False, "error": "missing_cawl_image_repo"}
    projects.set_cawl_image_repo(langcode, repo.strip())
    return 200, {"ok": True, "project": _project_for_api(
        projects.get(langcode))}


def _h_set_repo_slug(langcode, body):
    """``POST /v1/projects/<lang>/repo_slug`` — persist the
    per-project GitHub-repo-name override for the publish path.
    Body: ``{repo_slug: 'my-vanity-name'}``. Empty string
    explicitly clears the override; callers should treat unset
    / empty as equal to ``langcode``.

    Used by peers (recorder ``CollabScreen.do_publish``) to
    persist a textbox value the user typed before publishing.
    The pre-1.41.3 recorder kept this as a suite-wide
    ``peer_pref`` scalar; per the no-daemon-owned-caches rule
    that was wrong (per-project data in a global pref, and
    project-identity data on the peer side at all). This
    endpoint is the canonical home."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    slug = body.get('repo_slug')
    if slug is None or not isinstance(slug, str):
        return 400, {"ok": False, "error": "missing_repo_slug"}
    projects.set_repo_slug(langcode, slug.strip())
    return 200, {"ok": True, "project": _project_for_api(
        projects.get(langcode))}


def _h_github_install_url(_body):
    """Return the configured GitHub App install URL (canonical source is
    azt_collabd.config). Peers use this so they don't have to import
    daemon internals just to send the user to install the App."""
    from . import config as _config
    try:
        url = _config.install_url()
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    return 200, {"ok": True, "url": url}


def _h_github_client_id(_body):
    """Return the configured GitHub App client_id."""
    from . import config as _config
    try:
        client_id = _config.get().get('client_id', '')
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    return 200, {"ok": True, "client_id": client_id}


# ── GitHub App device flow (server-driven) ────────────────────────────
#
# Peers used to import ``device_flow_start`` / ``device_flow_poll`` from
# ``azt_collabd.auth`` and run the whole flow in their own process. Now
# the server owns the flow: the peer kicks it off, polls a job-style
# status endpoint, and the server (a) hands back the user_code right
# away, then (b) polls GitHub itself, and on success writes tokens
# into the credentials store. The peer never touches a token.

_device_flow_jobs = {}   # job_id -> dict
_device_flow_lock = threading.Lock()


def _device_flow_finalize(job_id, token_data):
    """Save tokens to the store, look up the username, check whether
    the GitHub App is installed, and mark the job DONE."""
    from . import auth as _auth
    access_token = token_data.get('access_token', '')
    username = ''
    app_installed = False
    try:
        username = _auth.get_github_username(access_token) or ''
    except Exception:
        username = ''
    try:
        store.set_github_tokens(
            access_token=access_token,
            refresh_token=token_data.get('refresh_token', ''),
            username=username,
        )
    except Exception:
        pass
    try:
        app_installed = bool(
            _auth.check_app_installed(access_token).get('installed', False))
        store.set_github_app_installed(app_installed)
    except Exception:
        app_installed = False
    with _device_flow_lock:
        job = _device_flow_jobs.get(job_id)
        if job is not None:
            job.update({
                'state': 'DONE',
                'username': username,
                'app_installed': app_installed,
            })


def _device_flow_worker(job_id, device_code, interval, expires_in):
    from . import auth as _auth
    try:
        token_data = _auth.device_flow_poll(
            device_code, interval=interval, expires_in=expires_in)
    except _auth.AuthError as ex:
        with _device_flow_lock:
            job = _device_flow_jobs.get(job_id)
            if job is not None:
                job.update({'state': 'FAILED',
                            'error': ex.status.code,
                            'error_params': ex.status.params})
        return
    except Exception as ex:
        with _device_flow_lock:
            job = _device_flow_jobs.get(job_id)
            if job is not None:
                job.update({'state': 'FAILED', 'error': str(ex)})
        return
    _device_flow_finalize(job_id, token_data)


def _h_github_device_flow_start(_body):
    """Begin device flow. Returns the user_code to display, the
    verification URI to open, an expiry, and a job_id the peer should
    poll."""
    from . import auth as _auth
    try:
        resp = _auth.device_flow_start()
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    job_id = secrets.token_hex(8)
    interval = int(resp.get('interval', 5))
    expires_in = int(resp.get('expires_in', 900))
    with _device_flow_lock:
        _device_flow_jobs[job_id] = {
            'state': 'POLLING',
            'username': '',
            'app_installed': False,
            'error': '',
            'error_params': {},
        }
    t = threading.Thread(
        target=_device_flow_worker,
        args=(job_id, resp.get('device_code', ''), interval, expires_in),
        daemon=True,
    )
    t.start()
    return 200, {
        "ok": True,
        "job_id": job_id,
        "user_code": resp.get('user_code', ''),
        "verification_uri": resp.get(
            'verification_uri', 'https://github.com/login/device'),
        "expires_in": expires_in,
        "interval": interval,
    }


def _h_github_device_flow_status(job_id, _body):
    with _device_flow_lock:
        job = _device_flow_jobs.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": "job_not_found"}
        return 200, {"ok": True, **job}


def _h_set_host(body):
    host = body.get('host', '')
    if host not in ('github', 'gitlab'):
        return 400, {"ok": False, "error": "invalid_host"}
    store.set_collab_host(host)
    return 200, {"ok": True}


def _h_set_github_tokens(body):
    access_token = body.get('access_token', '')
    if not access_token:
        return 400, {"ok": False, "error": "missing_access_token"}
    store.set_github_tokens(
        access_token=access_token,
        refresh_token=body.get('refresh_token', ''),
        username=body.get('username', ''),
        token_time=body.get('token_time'),
    )
    return 200, {"ok": True}


def _h_set_github_app_installed(body):
    store.set_github_app_installed(bool(body.get('installed', False)))
    return 200, {"ok": True}


def _h_set_gitlab(body):
    username = body.get('username', '')
    token = body.get('token', '')
    if not username or not token:
        return 400, {"ok": False, "error": "missing_username_or_token"}
    store.set_gitlab(username, token)
    return 200, {"ok": True}


def _h_test_github(body):
    """Validate the stored GitHub access token by hitting
    ``api.github.com/user``. Mirrors ``_h_test_gitlab`` so the UI's
    per-host Test buttons share a shape.

    Refresh path: the token may be near expiry. We pull through
    ``store.get_valid_github_token`` which proactively refreshes when
    the access token is older than 7 hours, so a successful test
    after a refresh persists the new token automatically (set on
    refresh by ``store.set_github_tokens``, which also clears
    ``confirmed`` — we re-set it on the success branch below).

    On a successful test we also persist the freshly-probed
    ``app_installed`` flag (the GitHub probe is on the same token,
    one extra HTTP round-trip already done inside
    ``test_github_credentials``) so the connect screen's "Install
    GitHub App" CTA shows up only when warranted."""
    from . import auth as _auth
    username, token = store.get_valid_github_token()
    if not token:
        store.set_github_confirmed(False)
        return 200, {"ok": True, "valid": False,
                     "server_username": "",
                     "app_installed": False,
                     "error": "missing_token"}
    info = _auth.test_github_credentials(token)
    import sys
    print(f'[_h_test_github] test_github_credentials returned: '
          f'valid={info.get("valid")} '
          f'app_installed={info.get("app_installed")} '
          f'app_suspended={info.get("app_suspended")} '
          f'installation_id={info.get("installation_id")}',
          file=sys.stderr, flush=True)
    if info.get('valid'):
        # Refresh username if the user renamed on GitHub since last
        # connect; harmless on the typical "name unchanged" path.
        server_user = info.get('server_username', '') or username
        if server_user and server_user != username:
            store.set_github_tokens(
                access_token=token,
                username=server_user,
            )
        store.set_github_app_installed(bool(info.get('app_installed')))
        store.set_github_confirmed(True)
        print(f'[_h_test_github] saved: app_installed='
              f'{bool(info.get("app_installed"))!r} confirmed=True',
              file=sys.stderr, flush=True)
    else:
        store.set_github_confirmed(False)
    return 200, {"ok": True, **info}


def _h_test_gitlab(body):
    """Validate GitLab credentials by hitting ``/api/v4/user``. If
    ``username`` / ``token`` are absent in the body, fall back to the
    stored values so callers can re-test what's already saved without
    having the user retype the PAT.

    On a successful test the credentials are persisted and the
    ``confirmed`` flag is set, so the UI's single Test button covers
    both save and verify in one user gesture."""
    username = body.get('username', '') or ''
    token = body.get('token', '') or ''
    if not username or not token:
        stored_user, stored_token = store.get_gitlab()
        username = username or stored_user
        token = token or stored_token
    from . import auth as _auth
    info = _auth.test_gitlab_credentials(username, token)
    if info.get('valid'):
        store.set_gitlab(username, token)
        store.set_gitlab_confirmed(True)
    return 200, {"ok": True, **info}


def _h_migrate_from_prefs(body):
    prefs_path = body.get('prefs_path', '')
    if not prefs_path:
        return 400, {"ok": False, "error": "missing_prefs_path"}
    summary = store.migrate_from_prefs(prefs_path)
    return 200, {"ok": True, **summary}


def _h_list_projects(_body):
    items = [_project_for_api(p) for p in projects.list_all()]
    # Diagnostic: confirms the path the dispatcher reads from and
    # the resulting count. If the count is 0 here right after a
    # successful clone-register elsewhere in the same logcat, we
    # have an AZT_HOME mismatch between the two call sites
    # (different process? jnius Activity null at one point?).
    print(f'[server] list_projects: {len(items)} entries from '
          f'{projects.projects_path()!r} → '
          f'{[i["langcode"] for i in items]!r}',
          file=sys.stderr, flush=True)
    # Empty registry → also report what's on disk under
    # ``$AZT_HOME/projects/`` so we can distinguish "registry wiped
    # but the working trees survived" (recoverable: scan + register)
    # from "the entire filesDir is gone" (e.g. server APK clean-
    # install). Only run when the registry says zero — the directory
    # listing is cheap but pointless when we already have an answer.
    if not items:
        try:
            projects_dir = os.path.join(azt_home(), 'projects')
            if os.path.isdir(projects_dir):
                disk_dirs = sorted(os.listdir(projects_dir))
            else:
                disk_dirs = None
            print(f'[server] list_projects: registry empty; '
                  f'projects_dir={projects_dir!r} '
                  f'on_disk={disk_dirs!r}',
                  file=sys.stderr, flush=True)
        except Exception as ex:
            print(f'[server] list_projects: disk-scan failed: {ex}',
                  file=sys.stderr, flush=True)
    return 200, {"ok": True, "projects": items}


def _annotate_with_auth_health(res):
    """Append ``S.AUTH_REFRESH_STALE`` to *res* when the persisted
    GitHub refresh state says the refresh path is broken.

    Called on every sync result so peers running the user-initiated
    sync path (per the auto/user contract in
    ``azt_collab_client/CLAUDE.md``) can surface a deadline-aware
    toast — the user has ~1h between the failed refresh attempt
    and the access-token cliff, and the only useful action is
    re-running the device flow at GitHub Connect.

    Idempotent: re-syncs while the state is broken keep emitting
    the status; ``set_github_tokens`` after a successful device
    flow clears ``refresh_broken`` and the status stops appearing.

    Auto-sync peers MUST silence this code (see the same contract);
    nothing breaks if they don't, but the user gets a popup at an
    inopportune moment."""
    state = store.github_refresh_state()
    if not state['broken']:
        return
    res.add(S.AUTH_REFRESH_STALE,
            expires_at=state['expires_at'],
            error=state['error'])


def _touch_project(langcode):
    """Stamp *langcode* as the device's most-recently-touched project.

    Called from every langcode-bound endpoint so peers don't have to
    remember to write ``set_last_project``; opening a project to read
    it (``_h_get_project``), checking its sync state
    (``_h_project_status``), or syncing it (``_h_project_sync``)
    naturally marks it recent. Single source of truth across peers
    and platforms — fixes the Android-sandbox split where the
    recorder's $AZT_HOME and the daemon's $AZT_HOME are different
    files."""
    if not langcode:
        return
    try:
        store.set_last_langcode(langcode)
        print(f'[recent] _touch_project({langcode!r}) → '
              f'{store._config_path()!r}',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[recent] _touch_project({langcode!r}) failed: {ex}',
              file=sys.stderr, flush=True)


def _h_get_last_project(_body):
    val = store.get_last_langcode()
    print(f'[recent] GET /v1/recent/last_project → {val!r} '
          f'(from {store._config_path()!r})',
          file=sys.stderr, flush=True)
    return 200, {"ok": True, "langcode": val}


def _h_set_last_project(body):
    """Explicit override. Most peers shouldn't need to call this —
    every langcode-bound endpoint already stamps via ``_touch_project``
    — but the wrapper exists so peers that genuinely want to *clear*
    the recent slot (or pin a different project than the one they
    just touched) have an affordance."""
    val = body.get('langcode', '') or ''
    store.set_last_langcode(val)
    print(f'[recent] POST /v1/recent/last_project ← {val!r} '
          f'(to {store._config_path()!r})',
          file=sys.stderr, flush=True)
    return 200, {"ok": True}


def _h_get_project(langcode, _body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_derive_langcode(body):
    """Compute a langcode for a working dir / LIFT path. Pure function;
    just exposes ``projects.derive_langcode`` to peers so they don't
    have to import daemon internals to register a project."""
    working_dir = body.get('working_dir', '')
    lift_path = body.get('lift_path', '')
    if not working_dir:
        return 400, {"ok": False, "error": "missing_working_dir"}
    return 200, {"ok": True,
                 "langcode": projects.derive_langcode(
                     working_dir, lift_path)}


def _h_init_project(body):
    """Initialize a git repo at *working_dir*, set the remote, and push.
    Server uses store-resident credentials — peers do not pass tokens."""
    from .repo import init_repo as _init_repo
    working_dir = body.get('working_dir', '')
    remote_url = body.get('remote_url', '')
    branch = body.get('branch', 'main')
    contributor = store.resolve_contributor(body.get('contributor', ''))
    if not working_dir or not remote_url:
        return 400, {"ok": False,
                     "error": "missing_working_dir_or_remote_url"}
    git_user, token = store.get_sync_credentials(remote_url)
    if not token:
        return 200, {"ok": True,
                     "result": Result().add(S.AUTH_REQUIRED).to_dict()}
    try:
        result = _init_repo(working_dir, remote_url, git_user, token,
                            branch, contributor)
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    # On success update the registry to reflect the new state:
    #
    #   * ``_touch_project`` — publish is active use; mark recent.
    #   * ``set_remote_url``  — _init_repo writes the local git config
    #     but the registry is a separate datastore; without this
    #     back-write the publish row stays visible after a successful
    #     publish.
    #   * ``set_last_sync`` on PUSHED — without it the recorder's
    #     "not backed up" warning persists forever after a successful
    #     publish, because the indicator reads ``last_sync == 0`` as
    #     "never synced." Sister handlers in ``scheduler._run_sync`` /
    #     ``_h_project_sync`` already stamp this; init_project was the
    #     odd one out.
    #   * ``set_last_commit`` on COMMITTED / COMMITTED_AND_PUSHED —
    #     same idea for the commit timestamp peers display alongside.
    codes = result.codes()
    try:
        for p in projects.list_all():
            if os.path.abspath(p.working_dir) == os.path.abspath(working_dir):
                _touch_project(p.langcode)
                if remote_url and p.remote_url != remote_url:
                    projects.set_remote_url(p.langcode, remote_url)
                if ('PUSHED' in codes
                        or 'COMMITTED_AND_PUSHED' in codes):
                    projects.set_last_sync(p.langcode)
                if ('COMMITTED' in codes
                        or 'COMMITTED_AND_PUSHED' in codes
                        or 'COMMITTED_LOCAL' in codes
                        or 'COMMITTED_NO_REMOTE' in codes):
                    projects.set_last_commit(p.langcode)
                break
    except Exception:
        pass
    return 200, {"ok": True, "result": result.to_dict()}


# Clone is run as a job so the peer can poll progress lines without the
# HTTP request hanging for the whole download.
_clone_jobs = {}
_clone_lock = threading.Lock()


_AUTH_ERROR_KEYWORDS = (
    '401', '403', '404',
    'unauthorized', 'forbidden', 'not found',
    'authentication', 'credential',
)


def _clone_error_looks_like_auth(result):
    """True if any CLONE_FAILED status carries an error message that
    smells like 401/403/404 / auth-related. Used to decide whether to
    retry anonymously and (after final failure) whether to surface
    CLONE_AUTH_REQUIRED to the UI."""
    for st in result.statuses:
        if st.code != 'CLONE_FAILED':
            continue
        msg = (st.params.get('error', '') or '').lower()
        if any(k in msg for k in _AUTH_ERROR_KEYWORDS):
            return True
    return False


def _clone_worker(job_id, remote_url, dest_dir, username, token,
                  retry_anonymous_on_auth_fail=True,
                  override_langcode=''):
    from .repo import clone_repo as _clone_repo

    def _on_progress(line):
        with _clone_lock:
            job = _clone_jobs.get(job_id)
            if job is not None:
                job['progress'].append(line)
                # Cap history so a chatty repo doesn't grow unbounded.
                if len(job['progress']) > 500:
                    del job['progress'][:-500]

    try:
        had_token = bool(token)
        lift_path, result = _clone_repo(
            remote_url, dest_dir, username, token, on_progress=_on_progress)
        if not lift_path and retry_anonymous_on_auth_fail \
                and _clone_error_looks_like_auth(result):
            lift_path, result = _clone_repo(
                remote_url, dest_dir, '', '',
                on_progress=_on_progress)

        # Final-failure auth diagnosis: if both attempts failed with
        # auth-shaped errors, OR we never had creds for the URL's host,
        # tell the UI so it can prompt the user to authenticate.
        if not lift_path and result.has(S.CLONE_FAILED):
            host = store.host_for_url(remote_url) or store.get_collab_host()
            if (not had_token) or _clone_error_looks_like_auth(result):
                result.add(S.CLONE_AUTH_REQUIRED, host=host)

        # Auto-register on success so later list_projects /
        # sync_project / project_status calls find this clone in the
        # registry. Best-effort: a registry write failure shouldn't
        # mark the whole clone as failed (the working tree is on
        # disk; a future explicit register_project call recovers).
        # Capture the canonical langcode for the job response so
        # peers don't have to parse it back out of the LIFT URI
        # (azt-viewer 0.5.1 filed the TODO; the URI parse worked
        # only by coincidence — when working dir basename == langcode).
        # Picker collects an explicit langcode from the user before
        # kicking the clone (confirm-langcode popup); pass it through
        # via ``override_langcode`` so the project is keyed on the
        # user's choice from the moment ``projects.json`` first sees
        # it. Empty override falls back to the filesystem-derived
        # default — matches the desktop / scripted-call behaviour.
        job_langcode = ''
        if lift_path:
            try:
                job_langcode = (
                    (override_langcode or '').strip()
                    or projects.derive_langcode(dest_dir, lift_path))
                projects.register(job_langcode, dest_dir,
                                  lift_path=lift_path,
                                  remote_url=remote_url)
                _touch_project(job_langcode)
                # Confirm the registry write hit disk (the user
                # reported previously-cloned projects not showing
                # up on next open — most likely the auto-register
                # is silently failing). projects_path() should be
                # ``$AZT_HOME/projects.json`` — if it's missing
                # after this print, persistence is the issue.
                print(f'[server] clone registered langcode='
                      f'{job_langcode!r} → {projects.projects_path()!r}',
                      file=sys.stderr, flush=True)
            except Exception as ex:
                print(f'[server] clone auto-register failed: '
                      f'{type(ex).__name__}: {ex}',
                      file=sys.stderr, flush=True)

        with _clone_lock:
            job = _clone_jobs.get(job_id)
            if job is not None:
                job.update({
                    'state': 'DONE',
                    'lift_path': lift_path or '',
                    'langcode': job_langcode,
                    'result': result.to_dict(),
                })
    except Exception as ex:
        with _clone_lock:
            job = _clone_jobs.get(job_id)
            if job is not None:
                job.update({'state': 'FAILED', 'error': str(ex)})


def _h_create_project_from_template(body):
    template_url = (body.get('template_url') or '').strip()
    vernlang = (body.get('vernlang') or '').strip()
    dest_dir = (body.get('dest_dir') or '').strip()
    if not vernlang or not dest_dir:
        return 400, {"ok": False,
                     "error": "missing_vernlang_or_dest_dir"}
    if not template_url:
        template_url = _config.default_template_url()
    try:
        p = projects.create_from_template(
            template_url, vernlang, dest_dir)
        _touch_project(p.langcode)
    except (ValueError, RuntimeError) as ex:
        return 400, {"ok": False, "error": str(ex)}
    except Exception as ex:
        # Log a full traceback to stderr/logcat so the failure mode is
        # diagnosable without a debugger. The Android log tag is
        # "python" — find it with
        # ``adb logcat | grep -E '\[server\] from_template'``.
        import traceback
        tb = traceback.format_exc()
        print(f'[server] from_template failed: {type(ex).__name__}: {ex}\n'
              f'  template_url={template_url!r}\n'
              f'  vernlang={vernlang!r}\n'
              f'  dest_dir={dest_dir!r}\n'
              f'{tb}',
              file=sys.stderr, flush=True)
        return 500, {"ok": False,
                     "error": f'{type(ex).__name__}: {ex}',
                     "traceback": tb}
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_clone_project(body):
    remote_url = body.get('remote_url', '')
    dest_dir = body.get('dest_dir', '')
    override_langcode = (body.get('langcode') or '').strip()
    if not remote_url or not dest_dir:
        return 400, {"ok": False,
                     "error": "missing_remote_url_or_dest_dir"}
    git_user, token = store.get_sync_credentials(remote_url)
    job_id = secrets.token_hex(8)
    with _clone_lock:
        _clone_jobs[job_id] = {
            'state': 'CLONING',
            'progress': [],
            'lift_path': '',
            'result': None,
            'error': '',
        }
    t = threading.Thread(
        target=_clone_worker,
        args=(job_id, remote_url, dest_dir, git_user, token),
        kwargs={'override_langcode': override_langcode},
        daemon=True,
    )
    t.start()
    return 200, {"ok": True, "job_id": job_id}


def _h_clone_status(job_id, body):
    """Return the current state plus *new* progress lines since
    ``last_index`` (caller-tracked). Defaults to all-so-far on first call."""
    last_index = 0
    try:
        last_index = int(body.get('last_index', 0)) if body else 0
    except (TypeError, ValueError):
        last_index = 0
    with _clone_lock:
        job = _clone_jobs.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": "job_not_found"}
        progress = job['progress']
        new_lines = progress[last_index:] if last_index < len(progress) \
            else []
        # Convert lift_path to a content URI on Android so peers can
        # feed it directly to LiftHandle (same logic as
        # _project_for_api). Clone-job state stores filesystem paths
        # because the daemon needs them for dulwich; the API boundary
        # is where we adapt to peer-visibility rules.
        lift_path = job.get('lift_path', '')
        langcode = job.get('langcode', '')
        if (lift_path and langcode and _on_android()
                and not lift_path.startswith('content://')):
            basename = os.path.basename(lift_path)
            lift_path = (
                f'content://{_AZTCOLLAB_AUTHORITY}/{langcode}/{basename}')
        return 200, {
            "ok": True,
            "state": job.get('state', 'CLONING'),
            "progress": new_lines,
            "next_index": len(progress),
            "lift_path": lift_path,
            "langcode": langcode,
            "result": job.get('result'),
            "error": job.get('error', ''),
        }


def _h_rename_project(langcode, body):
    """Rename a project's langcode key in projects.json. Used by the
    picker's confirm-langcode popup when the user overrides the
    auto-derived value for a clone / open-file project."""
    new_langcode = (body.get('new_langcode') or '').strip()
    if not new_langcode:
        return 400, {"ok": False, "error": "missing_new_langcode"}
    try:
        p = projects.rename(langcode, new_langcode)
    except ValueError as ex:
        return 400, {"ok": False, "error": str(ex)}
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    # The just-renamed project becomes recent — both because the user
    # is actively interacting with it and because if it *was* the
    # recent one, last_project still points at the old langcode and
    # peers would silently resolve nothing.
    _touch_project(new_langcode)
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_register_project(body):
    langcode = body.get('langcode', '')
    working_dir = body.get('working_dir', '')
    lift_path = body.get('lift_path', '')
    remote_url = body.get('remote_url', '')
    if not langcode or not working_dir:
        return 400, {"ok": False,
                     "error": "missing_langcode_or_working_dir"}
    working_dir = os.path.abspath(working_dir)
    if lift_path:
        lift_path = os.path.abspath(lift_path)
    if not remote_url:
        remote_url = projects.derive_remote_url(working_dir)
    p = projects.register(langcode, working_dir, lift_path, remote_url)
    _touch_project(langcode)
    return 200, {"ok": True, "project": _project_for_api(p)}


def _h_project_sync(langcode, body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    contributor = store.resolve_contributor(body.get('contributor', ''))
    print(f'[sync-rpc] {langcode!r} contributor={contributor!r} '
          f'remote_url={p.remote_url!r}',
          file=sys.stderr, flush=True)
    git_user, token = store.get_sync_credentials(p.remote_url)
    if not token:
        print(f'[sync-rpc] {langcode!r} → AUTH_REQUIRED',
              file=sys.stderr, flush=True)
        res = Result().add(S.AUTH_REQUIRED)
        return 200, {"ok": True, "result": res.to_dict()}
    try:
        res = _sync_repo(p.working_dir, git_user, token, contributor)
    except Exception as ex:
        print(f'[sync-rpc] {langcode!r} raised: {ex}',
              file=sys.stderr, flush=True)
        return 500, {"ok": False, "error": str(ex)}
    _annotate_with_auth_health(res)
    codes = res.codes()
    print(f'[sync-rpc] {langcode!r} done: codes={codes!r}',
          file=sys.stderr, flush=True)
    if ('PUSHED' in codes or 'PULLED' in codes
            or 'COMMITTED_AND_PUSHED' in codes):
        projects.set_last_sync(langcode)
    if ('COMMITTED_LOCAL' in codes or 'COMMITTED_NO_REMOTE' in codes
            or 'COMMITTED_AND_PUSHED' in codes):
        projects.set_last_commit(langcode)
    return 200, {"ok": True, "result": res.to_dict()}


def _h_project_status(langcode, _body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    summary = _repo_status(p.working_dir)
    branch, remote_url, n_changes, commits_ahead = ('', '', 0, 0)
    if summary is not None:
        branch, remote_url, n_changes, commits_ahead = summary
    api = _project_for_api(p)
    return 200, {
        "ok": True,
        "langcode": langcode,
        "branch": branch,
        "remote_url": remote_url or p.remote_url,
        "n_changes": n_changes,
        "commits_ahead": commits_ahead,
        "last_commit": p.last_commit,
        "last_sync": p.last_sync,
        "working_dir": p.working_dir,
        "lift_path": api['lift_path'],
        # Per-project metadata that peers occasionally need on the
        # status response without a separate ``open_project`` call.
        # Pre-0.39 callers ignore unknown keys.
        "repo_slug": p.repo_slug,
        "cawl_image_repo": p.cawl_image_repo,
    }


def _h_set_project_last_sync(langcode, body):
    ts = body.get('timestamp')
    projects.set_last_sync(langcode, ts)
    return 200, {"ok": True}


def _parse_github_owner_repo(remote_url):
    """Extract ``(owner, repo)`` from a GitHub remote URL. Returns
    ``None`` if the URL isn't a recognisable GitHub remote.

    Accepted shapes:
      - ``https://github.com/<owner>/<repo>`` (with or without ``.git``)
      - ``http://github.com/<owner>/<repo>``
      - ``git@github.com:<owner>/<repo>``"""
    if not remote_url:
        return None
    import re
    url = remote_url.strip().rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    m = re.match(r'^https?://github\.com/([^/]+)/([^/]+)$', url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r'^git@github\.com:([^/]+)/([^/]+)$', url)
    if m:
        return m.group(1), m.group(2)
    return None


def _h_grant_collaborator(langcode, body):
    """``POST /v1/projects/<lang>/collaborators``. Invite a GitHub
    user as a collaborator on the repo backing ``langcode``.

    Looks the repo up via the project's ``remote_url`` so the peer
    only has to pass a langcode — the server-side lookup eliminates
    any "wrong project" risk from peer-side URL handling. Returns
    a Result-shaped response with one of:
    ``COLLABORATOR_INVITED``, ``COLLABORATOR_ALREADY``,
    ``INVALID_USERNAME``, ``NO_REMOTE``, ``NOT_GITHUB_REMOTE``,
    ``AUTH_REQUIRED``, or ``COLLABORATOR_INVITE_FAILED``.

    Permission level defaults to ``push`` (matches typical SIL
    collaborator flow); callers can override via ``body['level']``."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    username = (body.get('username') or '').strip()
    permission = (body.get('level') or 'push').strip() or 'push'
    if not username:
        res = Result().add(S.INVALID_USERNAME)
        return 200, {"ok": True, "result": res.to_dict()}
    if not p.remote_url:
        res = Result().add(S.NO_REMOTE)
        return 200, {"ok": True, "result": res.to_dict()}
    parsed = _parse_github_owner_repo(p.remote_url)
    if parsed is None:
        res = Result().add(S.NOT_GITHUB_REMOTE,
                           remote_url=p.remote_url)
        return 200, {"ok": True, "result": res.to_dict()}
    owner, repo_name = parsed
    _git_user, token = store.get_sync_credentials(p.remote_url)
    if not token:
        res = Result().add(S.AUTH_REQUIRED)
        return 200, {"ok": True, "result": res.to_dict()}
    try:
        outcome = auth.add_collaborator(
            owner, repo_name, username, token, permission=permission)
    except Exception as ex:
        print(f'[collab-grant] {langcode!r} {owner}/{repo_name} '
              f'{username!r} → {type(ex).__name__}: {ex}',
              file=sys.stderr, flush=True)
        res = Result().add(
            S.COLLABORATOR_INVITE_FAILED, error=str(ex),
            owner_repo=f'{owner}/{repo_name}', username=username)
        return 200, {"ok": True, "result": res.to_dict()}
    res = Result()
    if outcome == 'already':
        res.add(S.COLLABORATOR_ALREADY, username=username,
                owner_repo=f'{owner}/{repo_name}')
    else:
        res.add(S.COLLABORATOR_INVITED, username=username,
                owner_repo=f'{owner}/{repo_name}',
                permission=permission)
    return 200, {"ok": True, "result": res.to_dict()}


def _h_project_sync_async(langcode, body):
    p = projects.get(langcode)
    if p is None:
        print(f'[sync-async] {langcode!r} → project_not_found',
              file=sys.stderr, flush=True)
        return 404, {"ok": False, "error": "project_not_found"}
    _touch_project(langcode)
    contributor = store.resolve_contributor(body.get('contributor', ''))
    job_id = scheduler.request_sync(langcode, contributor)
    print(f'[sync-async] {langcode!r} contributor={contributor!r} '
          f'enqueued job_id={job_id!r}',
          file=sys.stderr, flush=True)
    return 200, {"ok": True, "job_id": job_id}


def _h_get_job(job_id, _body):
    job = scheduler.get_job(job_id)
    if job is None:
        return 404, {"ok": False, "error": "job_not_found"}
    return 200, {"ok": True, **job.to_dict()}


# ── Atomic file write ─────────────────────────────────────────────────────
#
# Peers on Android reach the daemon's filesystem via the
# ContentProvider's openFileDescriptor — they receive an FD into a
# file under the daemon's private filesDir. ``ftruncate(fd, 0)`` +
# write through that FD is NOT atomic from the perspective of any
# other reader (another peer, the daemon's own merge-output writer):
# a concurrent observer can see torn bytes mid-write. The
# 2026-05-12 ``baf`` repro is one realization of this — two LIFT
# serializations interleaved through the FD path and produced
# malformed XML.
#
# The endpoint below closes the gap: the peer sends the full file
# bytes inline (base64 in the JSON body), the daemon serializes
# the write through the project lock, and writes via tempfile +
# os.replace. The destination is always a complete copy of one
# version, never a torn mix.

_ATOMIC_COMMIT_ALLOWED_DIRS = ('audio', 'images')


def _resolve_atomic_commit_path(working_dir, rel):
    """Validate ``rel`` against the atomic-commit whitelist and
    return the absolute path under *working_dir*, or None if the
    shape is rejected.

    Allowed shapes (mirror of the ContentProvider's
    ``_resolve_path`` whitelist, scoped to a known working_dir):

    - ``<file>.lift``           — top-level LIFT file
    - ``audio/<file>``          — sibling audio
    - ``images/<file>``         — sibling image

    Any other shape — empty segments, ``..``, three-level paths —
    is rejected before any filesystem touch. A final
    ``os.path.commonpath`` check guards against symlink-based
    escapes."""
    if not rel or not isinstance(rel, str):
        return None
    rel = rel.lstrip('/')
    parts = rel.split('/')
    if any(p in ('', '..', '.') for p in parts):
        return None
    if len(parts) == 1:
        if not parts[0].lower().endswith('.lift'):
            return None
    elif len(parts) == 2:
        if parts[0] not in _ATOMIC_COMMIT_ALLOWED_DIRS:
            return None
    else:
        return None
    base = os.path.realpath(working_dir)
    target = os.path.realpath(os.path.join(base, *parts))
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    return target


def _h_project_atomic_commit(langcode, body):
    """POST /v1/projects/<lang>/atomic_commit

    Request body::

        {"path": "<rel_path>", "data_b64": "<base64-encoded-bytes>"}

    Writes the bytes atomically to ``<working_dir>/<rel_path>``.
    The write goes through a sibling tempfile + ``os.replace``,
    serialized via ``project_lock`` so it can't overlap with a
    sync's merge-output write or another atomic_commit. The
    destination is never torn.

    Returns ``ATOMIC_COMMITTED`` (with ``bytes_written`` and
    ``sha256``) on success. Path-validation failures return 400;
    filesystem failures return 500."""
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid_body"}
    rel = body.get('path') or ''
    data_b64 = body.get('data_b64') or ''
    if not isinstance(rel, str) or not rel:
        return 400, {"ok": False, "error": "missing_path"}
    if not isinstance(data_b64, str):
        return 400, {"ok": False, "error": "invalid_data"}
    try:
        data = base64.b64decode(data_b64, validate=True)
    except Exception as ex:
        return 400, {"ok": False, "error": f"base64_decode: {ex}"}
    target = _resolve_atomic_commit_path(p.working_dir, rel)
    if target is None:
        return 400, {"ok": False, "error": "path_rejected"}
    _touch_project(langcode)
    tmp = f'{target}.tmp.{os.getpid()}.{secrets.token_hex(8)}'
    try:
        with project_lock(p.working_dir):
            os.makedirs(os.path.dirname(target) or '.', exist_ok=True)
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, target)
    except Exception as ex:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        return 500, {"ok": False, "error": str(ex)}
    res = Result().add(S.ATOMIC_COMMITTED,
                       bytes_written=len(data),
                       sha256=hashlib.sha256(data).hexdigest())
    return 200, {"ok": True, "result": res.to_dict()}


def _match_cawl_image_path(path):
    """If ``path`` is ``/v1/projects/<lang>/cawl/images/<basename>``,
    return ``(langcode, basename)``. Else ``None``. Strict shape:
    exactly six segments, ``cawl`` + ``images`` in positions 4/5,
    no trailing slashes."""
    if not path.startswith('/v1/projects/'):
        return None
    parts = path.split('/')
    if len(parts) != 7:
        return None
    if parts[4] != 'cawl' or parts[5] != 'images':
        return None
    langcode = parts[3]
    basename = parts[6]
    if not langcode or not basename:
        return None
    return langcode, basename


def dispatch(method, path, body):
    """Route a request. Returns ``(status: int, response: dict)``.

    Auth is the *caller's* responsibility — bearer-token check on HTTP,
    UID check on Android ContentProvider — except for paths in
    ``UNAUTHENTICATED_PATHS``, which any caller may use as a liveness
    probe.
    """
    if method == 'GET':
        if path == '/v1/health':
            return _h_health(body)
        if path == '/v1/online':
            return _h_online(body)
        if path == '/v1/credentials/status':
            return _h_credentials_status(body)
        if path == '/v1/config/contributor':
            return _h_get_contributor(body)
        if path == '/v1/credentials/github/install_url':
            return _h_github_install_url(body)
        if path == '/v1/credentials/github/client_id':
            return _h_github_client_id(body)
        if path.startswith('/v1/credentials/github/device_flow/'):
            parts = path.split('/')
            if len(parts) == 6 and parts[5]:
                return _h_github_device_flow_status(parts[5], body)
        if path == '/v1/recent/last_project':
            return _h_get_last_project(body)
        if path == '/v1/config/ui_language':
            return _h_get_ui_language(body)
        if path == '/v1/projects':
            return _h_list_projects(body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 4 and parts[3]:
                return _h_get_project(parts[3], body)
            if len(parts) == 5 and parts[4] == 'status':
                return _h_project_status(parts[3], body)
            if len(parts) == 6 and parts[4] == 'cawl' \
                    and parts[5] == 'index':
                return _h_cawl_index(parts[3], body)
        if path.startswith('/v1/jobs/'):
            parts = path.split('/')
            if len(parts) == 4 and parts[3]:
                return _h_get_job(parts[3], body)
        return 404, {"ok": False, "error": "not_found"}

    if method == 'POST':
        if path == '/v1/credentials/host':
            return _h_set_host(body)
        if path == '/v1/config/contributor':
            return _h_set_contributor(body)
        if path == '/v1/credentials/github/device_flow/start':
            return _h_github_device_flow_start(body)
        if path == '/v1/credentials/github/tokens':
            return _h_set_github_tokens(body)
        if path == '/v1/credentials/github/app_installed':
            return _h_set_github_app_installed(body)
        if path == '/v1/credentials/gitlab':
            return _h_set_gitlab(body)
        if path == '/v1/credentials/gitlab/test':
            return _h_test_gitlab(body)
        if path == '/v1/credentials/github/test':
            return _h_test_github(body)
        if path == '/v1/credentials/migrate_from_prefs':
            return _h_migrate_from_prefs(body)
        if path == '/v1/recent/last_project':
            return _h_set_last_project(body)
        if path == '/v1/projects/register':
            return _h_register_project(body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 5 and parts[4] == 'rename':
                return _h_rename_project(parts[3], body)
        if path == '/v1/projects/derive_langcode':
            return _h_derive_langcode(body)
        if path == '/v1/projects/init':
            return _h_init_project(body)
        if path == '/v1/projects/from_template':
            return _h_create_project_from_template(body)
        if path == '/v1/projects/clone':
            return _h_clone_project(body)
        if path.startswith('/v1/projects/clone/'):
            parts = path.split('/')
            if len(parts) == 5 and parts[4]:
                return _h_clone_status(parts[4], body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 5 and parts[4] == 'sync':
                return _h_project_sync(parts[3], body)
            if len(parts) == 5 and parts[4] == 'sync_async':
                return _h_project_sync_async(parts[3], body)
            if len(parts) == 5 and parts[4] == 'atomic_commit':
                return _h_project_atomic_commit(parts[3], body)
            if len(parts) == 5 and parts[4] == 'last_sync':
                return _h_set_project_last_sync(parts[3], body)
            if len(parts) == 5 and parts[4] == 'collaborators':
                return _h_grant_collaborator(parts[3], body)
            if len(parts) == 5 and parts[4] == 'cawl_image_repo':
                return _h_set_cawl_image_repo(parts[3], body)
            if len(parts) == 5 and parts[4] == 'repo_slug':
                return _h_set_repo_slug(parts[3], body)
        return 404, {"ok": False, "error": "not_found"}

    return 405, {"ok": False, "error": "method_not_allowed"}


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"azt_collabd/{_VERSION}"
    _token: str = ""   # populated by run()

    def log_message(self, fmt, *args):
        pass

    def _auth_ok(self):
        hdr = self.headers.get('Authorization', '')
        prefix = 'Bearer '
        if not hdr.startswith(prefix):
            return False
        return secrets.compare_digest(hdr[len(prefix):], type(self)._token)

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, status, content_type, data):
        """Binary response. Used by the CAWL-image endpoint; the
        dispatch table is JSON-only so this is the only non-JSON
        path in the server. Adopted to avoid base64-wrapping
        image payloads, which would inflate them ~1.33× for no
        useful purpose."""
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        n = int(self.headers.get('Content-Length', '0') or '0')
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return None

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except Exception as ex:
            _record_crash(ex, where='handle_one_request')
            raise

    def _serve(self, method, body):
        if self.path not in UNAUTHENTICATED_PATHS and not self._auth_ok():
            return self._send_json(401,
                                   {"ok": False, "error": "unauthorized"})
        status, response = dispatch(method, self.path, body)
        self._send_json(status, response)

    def do_GET(self):
        # Binary endpoint: route directly so we can stream raw
        # image bytes (the dispatch table is JSON-only).
        binary_match = _match_cawl_image_path(self.path)
        if binary_match is not None:
            if not self._auth_ok():
                return self._send_json(
                    401, {"ok": False, "error": "unauthorized"})
            langcode, basename = binary_match
            try:
                status, content_type, data = _h_cawl_image_bytes(
                    langcode, basename)
            except Exception as ex:
                return self._send_json(
                    500, {"ok": False, "error": str(ex)})
            return self._send_bytes(status, content_type, data)
        self._serve('GET', None)

    def do_POST(self):
        body = self._read_json()
        if body is None:
            return self._send_json(400,
                                   {"ok": False, "error": "bad_json"})
        self._serve('POST', body)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn,
                           http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(host='127.0.0.1', port=0):
    """Start the server. Blocks until interrupted. Writes server.json on
    bind and removes it on shutdown. Exits non-zero if another
    azt_collabd is already running against the same $AZT_HOME."""
    global _server_lock_fd
    home = azt_home()
    os.makedirs(home, exist_ok=True)

    # Single-instance guard — flock on $AZT_HOME/server.lock
    lock_path = os.path.join(home, 'server.lock')
    _server_lock_fd = _acquire_server_lock(lock_path)
    if _server_lock_fd is None:
        print(f'[azt_collabd] another instance already holds '
              f'{lock_path}', flush=True)
        sys.exit(1)

    token = secrets.token_urlsafe(32)
    _Handler._token = token
    httpd = _ThreadingHTTPServer((host, port), _Handler)
    bound_port = httpd.server_address[1]
    info = {
        "port": bound_port,
        "token": token,
        "pid": os.getpid(),
        "version": _VERSION,
    }
    info_path = server_info_path()
    fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(info, f)
    print(f'[azt_collabd] listening on {host}:{bound_port} '
          f'(home={home})', flush=True)
    _record_started()

    # Crash hook for any unhandled exception that escapes a request handler
    # or the watcher thread.
    def _excepthook(exc_type, exc, tb):
        try:
            _record_crash(exc, where='excepthook')
        finally:
            sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    # Mark any in-flight jobs left over from a previous daemon process
    # (kill -9, OOM, container restart) as JOB_INTERRUPTED so peers
    # polling on stale job_ids get a typed transient-failure result.
    # Must run BEFORE start_watcher so the watcher sees a consistent
    # job table.
    scheduler.reconcile_on_startup()

    # Start the connectivity watcher so projects with pending_push get
    # drained on offline→online transitions.
    scheduler.start_watcher()

    def _graceful(signum, frame):
        print(f'[azt_collabd] signal {signum}, shutting down', flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('[azt_collabd] interrupted', flush=True)
    finally:
        scheduler.stop_watcher()
        try:
            os.remove(info_path)
        except OSError:
            pass
        httpd.server_close()
