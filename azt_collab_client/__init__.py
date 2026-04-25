"""
azt_collab_client — thin client library for azt_collabd.

Ops that go through the server return a ``Result`` (structured status
codes + params); the caller calls ``translate_result(result)`` for
display. ``Result.has(S.PUSHED)`` etc. is the way to drive business
logic — no more substring matching on log strings.
"""

from . import status as S
from .status import Status, Result
from .projects import Project, ProjectStatus
from .translate import translate_status, translate_result, set_translator
from .rpc import call, health, ServerUnavailable


def configure(app_id: str):
    """Reserved for later migration steps (app identity for logging /
    provider routing). Currently a no-op."""
    return None


def is_online():
    """Ask the server whether it has internet access."""
    try:
        resp = call('GET', '/v1/online')
    except ServerUnavailable:
        return False
    return bool(resp.get('online'))


# ── Credentials API (server-owned credentials.json) ────────────────────────

def get_credentials_status():
    """Return a dict describing what's configured:
        {host, github: {connected, username, app_installed},
         gitlab: {connected, username}}
    Never contains raw tokens. On transport failure returns an empty
    status so the UI degrades gracefully."""
    try:
        resp = call('GET', '/v1/credentials/status')
    except ServerUnavailable:
        return {'host': 'github',
                'github': {'connected': False, 'username': '',
                           'app_installed': False},
                'gitlab': {'connected': False, 'username': ''}}
    if resp.get('ok'):
        return {k: v for k, v in resp.items() if k != 'ok'}
    return {}


def set_collab_host(host):
    """Persist the user's host selection (github|gitlab)."""
    try:
        call('POST', '/v1/credentials/host', {'host': host})
    except ServerUnavailable:
        pass


def save_github_tokens(token_data, username=''):
    """Persist a device-flow token response + (optional) username."""
    call('POST', '/v1/credentials/github/tokens', {
        'access_token': token_data.get('access_token', ''),
        'refresh_token': token_data.get('refresh_token', ''),
        'username': username,
    })


def mark_github_app_installed(installed=True):
    try:
        call('POST', '/v1/credentials/github/app_installed',
             {'installed': bool(installed)})
    except ServerUnavailable:
        pass


def save_gitlab_credentials(username, token):
    call('POST', '/v1/credentials/gitlab',
         {'username': username, 'token': token})


def migrate_from_prefs(prefs_path):
    """One-shot (idempotent) migration from a legacy prefs.json. The
    server moves gh_*/gl_*/collab_host keys into credentials.json and
    strips them from prefs.json."""
    try:
        resp = call('POST', '/v1/credentials/migrate_from_prefs',
                    {'prefs_path': prefs_path})
    except ServerUnavailable:
        return {'migrated': False, 'reason': 'server_unavailable'}
    return {k: v for k, v in resp.items() if k != 'ok'}


# ── Projects API ────────────────────────────────────────────────────────────

def list_projects():
    """Return a list of registered Projects."""
    try:
        resp = call('GET', '/v1/projects')
    except ServerUnavailable:
        return []
    if not resp.get('ok'):
        return []
    return [Project.from_dict(p) for p in resp.get('projects', [])]


def open_project(langcode):
    """Return the registered Project for *langcode*, or None."""
    try:
        resp = call('GET', f'/v1/projects/{langcode}')
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return Project.from_dict(resp.get('project', {}))


def register_project(langcode, working_dir, lift_path='', remote_url=''):
    """Tell the server about an existing project. Returns the Project."""
    resp = call('POST', '/v1/projects/register', {
        'langcode': langcode,
        'working_dir': working_dir,
        'lift_path': lift_path,
        'remote_url': remote_url,
    })
    if not resp.get('ok'):
        return None
    return Project.from_dict(resp.get('project', {}))


def project_status(langcode):
    """Return a ProjectStatus for *langcode*, or None."""
    try:
        resp = call('GET', f'/v1/projects/{langcode}/status')
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return ProjectStatus.from_dict(resp)


def sync_project(langcode, contributor):
    """Synchronous sync. Returns Result. Blocks until the server's sync
    pass returns. Use ``request_sync`` for fire-and-forget edits where
    the UI doesn't wait."""
    try:
        resp = call('POST', f'/v1/projects/{langcode}/sync',
                    {'contributor': contributor})
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result.from_dict(resp.get('result') or {})
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


def request_sync(langcode, contributor):
    """Schedule a debounced sync server-side. Returns a job_id (str) or
    None on transport failure. Multiple calls within the debounce
    window collapse into one run; the server commits and pushes once."""
    try:
        resp = call('POST', f'/v1/projects/{langcode}/sync_async',
                    {'contributor': contributor})
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return resp.get('job_id')


def poll_job(job_id):
    """Return the current state of a job: dict with keys ``state``
    ('PENDING' | 'RUNNING' | 'DONE'), ``langcode``, ``result`` (Result
    or None), ``created_at``, ``started_at``, ``finished_at``. Returns
    None if the job is unknown or unreachable."""
    try:
        resp = call('GET', f'/v1/jobs/{job_id}')
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    raw_result = resp.get('result')
    decoded_result = (Result.from_dict(raw_result)
                      if raw_result is not None else None)
    return {
        'job_id': resp.get('job_id'),
        'langcode': resp.get('langcode'),
        'state': resp.get('state'),
        'result': decoded_result,
        'created_at': resp.get('created_at', 0.0),
        'started_at': resp.get('started_at', 0.0),
        'finished_at': resp.get('finished_at', 0.0),
    }


def record_project_sync_time(langcode, timestamp=None):
    body = {}
    if timestamp is not None:
        body['timestamp'] = float(timestamp)
    try:
        call('POST', f'/v1/projects/{langcode}/last_sync', body)
    except ServerUnavailable:
        pass


__all__ = [
    'configure', 'is_online',
    'get_credentials_status', 'set_collab_host',
    'save_github_tokens', 'mark_github_app_installed',
    'save_gitlab_credentials', 'migrate_from_prefs',
    'list_projects', 'open_project', 'register_project',
    'project_status', 'sync_project', 'request_sync', 'poll_job',
    'record_project_sync_time',
    'Status', 'Result', 'S', 'Project', 'ProjectStatus',
    'translate_status', 'translate_result', 'set_translator',
    'ServerUnavailable',
]
