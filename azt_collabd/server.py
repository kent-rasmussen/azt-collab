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
    POST /v1/credentials/migrate_from_prefs   {prefs_path}
    POST /v1/sync                             {project_dir, contributor}
                                              — creds come from the store
"""

import http.server
import json
import os
import secrets
import signal
import socketserver
import sys
import threading

from . import projects
from . import scheduler
from . import store
from .net import _has_internet
from .paths import azt_home, server_info_path
from .repo import sync_repo as _sync_repo, repo_status_summary as _repo_status
from .status import Result, Status
from . import status as S

_VERSION = "0.6.0"

# Kept alive for the server's lifetime so the flock on server.lock stays held.
_server_lock_fd = None
_started_at = 0.0


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
    payload = {
        "ok": True, "version": _VERSION, "pid": os.getpid(),
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


def _h_migrate_from_prefs(body):
    prefs_path = body.get('prefs_path', '')
    if not prefs_path:
        return 400, {"ok": False, "error": "missing_prefs_path"}
    summary = store.migrate_from_prefs(prefs_path)
    return 200, {"ok": True, **summary}


def _h_list_projects(_body):
    items = [p.to_dict() for p in projects.list_all()]
    return 200, {"ok": True, "projects": items}


def _h_get_project(langcode, _body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    return 200, {"ok": True, "project": p.to_dict()}


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
    return 200, {"ok": True, "project": p.to_dict()}


def _h_project_sync(langcode, body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    contributor = body.get('contributor', 'Recorder')
    git_user, token = store.get_sync_credentials()
    if not token:
        res = Result().add(S.AUTH_REQUIRED)
        return 200, {"ok": True, "result": res.to_dict()}
    try:
        res = _sync_repo(p.working_dir, git_user, token, contributor)
    except Exception as ex:
        return 500, {"ok": False, "error": str(ex)}
    codes = res.codes()
    if ('PUSHED' in codes or 'PULLED' in codes
            or 'COMMITTED_AND_PUSHED' in codes):
        projects.set_last_sync(langcode)
    return 200, {"ok": True, "result": res.to_dict()}


def _h_project_status(langcode, _body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    summary = _repo_status(p.working_dir)
    branch, remote_url, n_changes = ('', '', 0)
    if summary is not None:
        branch, remote_url, n_changes = summary
    return 200, {
        "ok": True,
        "langcode": langcode,
        "branch": branch,
        "remote_url": remote_url or p.remote_url,
        "n_changes": n_changes,
        "last_sync": p.last_sync,
        "working_dir": p.working_dir,
        "lift_path": p.lift_path,
    }


def _h_set_project_last_sync(langcode, body):
    ts = body.get('timestamp')
    projects.set_last_sync(langcode, ts)
    return 200, {"ok": True}


def _h_project_sync_async(langcode, body):
    p = projects.get(langcode)
    if p is None:
        return 404, {"ok": False, "error": "project_not_found"}
    contributor = body.get('contributor', 'Recorder')
    job_id = scheduler.request_sync(langcode, contributor)
    return 200, {"ok": True, "job_id": job_id}


def _h_get_job(job_id, _body):
    job = scheduler.get_job(job_id)
    if job is None:
        return 404, {"ok": False, "error": "job_not_found"}
    return 200, {"ok": True, **job.to_dict()}


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
        if path == '/v1/projects':
            return _h_list_projects(body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 4 and parts[3]:
                return _h_get_project(parts[3], body)
            if len(parts) == 5 and parts[4] == 'status':
                return _h_project_status(parts[3], body)
        if path.startswith('/v1/jobs/'):
            parts = path.split('/')
            if len(parts) == 4 and parts[3]:
                return _h_get_job(parts[3], body)
        return 404, {"ok": False, "error": "not_found"}

    if method == 'POST':
        if path == '/v1/credentials/host':
            return _h_set_host(body)
        if path == '/v1/credentials/github/tokens':
            return _h_set_github_tokens(body)
        if path == '/v1/credentials/github/app_installed':
            return _h_set_github_app_installed(body)
        if path == '/v1/credentials/gitlab':
            return _h_set_gitlab(body)
        if path == '/v1/credentials/migrate_from_prefs':
            return _h_migrate_from_prefs(body)
        if path == '/v1/projects/register':
            return _h_register_project(body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            if len(parts) == 5 and parts[4] == 'sync':
                return _h_project_sync(parts[3], body)
            if len(parts) == 5 and parts[4] == 'sync_async':
                return _h_project_sync_async(parts[3], body)
            if len(parts) == 5 and parts[4] == 'last_sync':
                return _h_set_project_last_sync(parts[3], body)
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
